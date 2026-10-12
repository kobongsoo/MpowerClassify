//! 상주 서버에 붙는 쪽(`--daemon`) — 파이썬 판 `daemon/client.py` 에 대응한다.
//!
//! 서버를 찾아 텍스트를 보내고 벡터를 받는다. 서버가 없으면 자동으로 띄우고,
//! 어떤 이유로든 못 쓰면 `Unavailable` 을 돌려준다 — 부른 쪽(embed::EmbedBackend)이
//! 그때 모델을 직접 읽으므로, 서버가 없다고 분류가 실패하는 일은 없다.

use std::process::{Command, Stdio};
use std::time::Instant;

use serde_json::{json, Value};

use super::registry::{self, Liveness};
use super::{ipc, EMBED_TIMEOUT, MAX_MSG, PING_TIMEOUT, PROTO, START_WAIT, VERSION};
use crate::embed::{self, ModelSpec};
use crate::linfo;

// 한 번의 실행에서 서버를 띄워 보는 횟수 상한 — 뜨자마자 죽는 서버를 끝없이 띄우지 않게.
const MAX_SPAWN: u32 = 2;

//------------------------------------------------------------------
// 임베딩 요청의 결과
//=> "벡터를 받았다 / 서버가 벡터를 못 만든다고 답했다 / 서버를 못 쓴다" 를 가른다.
//   둘째와 셋째를 섞으면 안 된다 — 둘째는 직접 로드해도 같은 결과(None)라 폴백할 이유가
//   없고, 셋째는 직접 로드로 넘어가야 한다.
//
// -필드: Vector      = 문서 임베딩 벡터
// -필드: NoVector    = 서버가 임베딩 실패를 알렸다(빈 텍스트 등 — 직접 로드의 None 과 같은 뜻)
// -필드: Unavailable = 서버를 쓸 수 없다(사유) — 부른 쪽이 직접 로드로 넘어간다
//------------------------------------------------------------------
pub enum EmbedReply {
    Vector(Vec<f32>),
    NoVector,
    Unavailable(String),
}

// 한 번의 시도가 실패한 까닭 — 다시 해 볼 만한가를 가른다.
enum Miss {
    // 서버가 없거나 통신이 깨졌다 → 찾아보고(없으면 띄우고) 다시.
    Retry(String),
    // 기록의 버전·규약·모델이 이 exe 와 다르다 → 옛 서버를 내리고 새로 띄운다.
    Mismatch(Value),
    // 다시 해도 소용없다(모델 폴더가 다름·메시지가 너무 큼) → 직접 로드.
    GiveUp(String),
}

//------------------------------------------------------------------
// 상주 서버 클라이언트
//=> 어느 모델 서버에 붙을지와, 자동으로 띄울 때 물려줄 설정을 들고 있다.
//
// -필드: spec         = 이번 실행의 모델 스펙(서버도 같은 모델이어야 한다)
// -필드: idle_timeout = 자동 기동 때 넘길 유휴 종료(초, 0=무한)
// -필드: log_path     = 서버가 남길 로그 파일(--log 를 물려준다)
// -필드: verbose      = true 면 서버도 -v 로 띄운다
// -필드: spawned      = 이번 실행에서 서버를 띄워 본 횟수
//------------------------------------------------------------------
pub struct DaemonClient {
    spec: &'static ModelSpec,
    idle_timeout: u64,
    log_path: Option<String>,
    verbose: bool,
    spawned: u32,
}

impl DaemonClient {
    //------------------------------------------------------------------
    // 클라이언트 만들기
    //=> 설정만 기억한다. 서버 접속은 실제로 임베딩이 필요해질 때 처음 한다 —
    //   규칙만으로 끝나는 실행은 서버를 건드리지도, 띄우지도 않는다.
    //
    // -in: spec         = 모델 스펙
    // -in: idle_timeout = 자동 기동 때 넘길 유휴 종료(초, 0=무한)
    // -in: log_path     = 서버에 물려줄 로그 경로
    // -in: verbose      = 서버에 -v 를 물려줄지
    //
    // -out: DaemonClient
    // -out: error = 없음
    //------------------------------------------------------------------
    pub fn new(spec: &'static ModelSpec, idle_timeout: u64, log_path: Option<String>, verbose: bool) -> Self {
        DaemonClient { spec, idle_timeout, log_path, verbose, spawned: 0 }
    }

    //------------------------------------------------------------------
    // 텍스트 임베딩 요청 (핵심)
    //=> 빠른 길(기록을 읽어 곧바로 요청)을 먼저 타고, 안 되면 느린 길(서버 찾기·띄우기)로 간다.
    //    1) 빠른 길: 기록 읽기 → 접속 → embed_text. ping 을 따로 보내지 않아 왕복이 한 번이다
    //    2) 느린 길: 버전이 다르면 옛 서버 정지 → 죽은 기록 정리 → 자동 기동 → 준비까지 폴링
    //    3) 한도(20초) 안에 못 쓰면 Unavailable
    //
    // -in: text = 임베딩할 텍스트(정제는 서버의 embed_document 가 한다)
    //
    // -out: EmbedReply = 벡터 / 벡터 없음 / 서버 사용 불가
    // -out: error = 없음(모든 실패를 EmbedReply 로 돌려준다)
    //------------------------------------------------------------------
    pub fn embed_text(&mut self, text: &str) -> EmbedReply {
        let mut why = match self.try_once(text) {
            Ok(r) => return r,
            Err(Miss::GiveUp(w)) => return EmbedReply::Unavailable(w),
            Err(Miss::Mismatch(rec)) => {
                // exe 를 교체했는데 옛 서버가 남은 경우 — 내리고 새로 띄운다.
                linfo!("서버 버전·모델이 달라 교체합니다 (서버 version={} proto={})",
                       rec.get("version").map(|v| v.to_string()).unwrap_or_default(),
                       rec.get("proto").map(|v| v.to_string()).unwrap_or_default());
                self.replace_old(&rec);
                "서버 버전 불일치".to_string()
            }
            Err(Miss::Retry(w)) => w,
        };

        let deadline = Instant::now() + START_WAIT;
        while Instant::now() < deadline {
            match registry::probe(self.spec.key) {
                Liveness::Alive { ready: true, .. } => match self.try_once(text) {
                    Ok(r) => return r,
                    Err(Miss::GiveUp(w)) => return EmbedReply::Unavailable(w),
                    // 방금 답하던 서버가 또 다르다고 하면 교체가 안 되는 상황이다 — 그만둔다.
                    Err(Miss::Mismatch(_)) =>
                        return EmbedReply::Unavailable("서버 버전 불일치(교체 실패)".to_string()),
                    Err(Miss::Retry(w)) => why = w,
                },
                // 모델을 올리는 중이거나, 잠금을 막 잡은 참이다 — 조금 기다린다.
                Liveness::Alive { ready: false, .. } | Liveness::Starting => {}
                Liveness::Dead => {
                    if self.spawned >= MAX_SPAWN {
                        return EmbedReply::Unavailable(format!("서버가 뜨지 않습니다({})", why));
                    }
                    // 죽은 기록을 치우고 새로 띄운다.
                    registry::clear(self.spec.key);
                    if let Err(e) = self.spawn() {
                        return EmbedReply::Unavailable(format!("서버 기동 실패: {}", e));
                    }
                }
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
        EmbedReply::Unavailable(format!("서버가 제한시간({}초) 안에 준비되지 않음({})", START_WAIT.as_secs(), why))
    }

    //------------------------------------------------------------------
    // 요청 한 번 시도
    //=> 기록을 읽어 서버가 이 exe 와 맞는지 확인한 뒤 embed_text 를 보낸다.
    //    1) 기록의 version·proto·model·dim 이 다르면 Mismatch
    //    2) 서버가 연 모델 폴더가 내가 직접 열 폴더와 다르면 GiveUp(다른 모델일 수 있다)
    //    3) 응답 벡터의 차원이 스펙과 다르면 쓰지 않는다 — 다른 차원이 seed 와 섞이면
    //       비교가 조용히 틀어진다
    //
    // -in: text = 임베딩할 텍스트
    //
    // -out: Ok(EmbedReply) = Vector 또는 NoVector
    // -out: error = Err(Miss) — 다시 해 볼지/교체할지/그만둘지
    //------------------------------------------------------------------
    fn try_once(&self, text: &str) -> Result<EmbedReply, Miss> {
        let rec = registry::read_record(self.spec.key)
            .ok_or_else(|| Miss::Retry("서버 기록 없음".to_string()))?;
        let same = rec.get("version").and_then(Value::as_str) == Some(VERSION)
            && rec.get("proto").and_then(Value::as_u64) == Some(PROTO)
            && rec.get("model").and_then(Value::as_str) == Some(self.spec.key)
            && rec.get("dim").and_then(Value::as_u64) == Some(self.spec.dim as u64);
        if !same {
            return Err(Miss::Mismatch(rec));
        }
        // 서버와 내가 같은 모델 폴더를 보는가. 내 쪽에 모델이 없으면(None) 비교할 것이 없다.
        if let (Some(theirs), Some(mine)) = (rec.get("model_dir").and_then(Value::as_str),
                                             embed::resolved_model_dir()) {
            let mine = std::fs::canonicalize(&mine).unwrap_or(mine);
            if std::path::Path::new(theirs) != mine.as_path() {
                return Err(Miss::GiveUp(format!(
                    "서버의 모델 폴더가 다릅니다(서버 {} / 이 실행 {})", theirs, mine.display())));
            }
        }
        // 텍스트가 메시지 상한에 가까우면 보내지 않는다(JSON 이스케이프로 더 커진다).
        if text.len() > MAX_MSG / 2 {
            return Err(Miss::GiveUp("문서가 너무 커서 서버로 보낼 수 없음".to_string()));
        }
        let port = rec.get("port").and_then(Value::as_u64).filter(|p| *p > 0 && *p <= u16::MAX as u64)
            .ok_or_else(|| Miss::Retry("서버 기록에 포트 없음".to_string()))? as u16;
        let req = json!({"op": "embed_text", "token": rec.get("token"), "text": text});
        let resp = ipc::rpc(port, &req, EMBED_TIMEOUT)
            .map_err(|e| Miss::Retry(format!("서버 통신 실패: {}", e)))?;

        if resp.get("ok").and_then(Value::as_bool) == Some(true) {
            let vec: Vec<f32> = resp.get("vector")
                .and_then(|v| serde_json::from_value(v.clone()).ok())
                .ok_or_else(|| Miss::Retry("응답에 벡터 없음".to_string()))?;
            if vec.len() != self.spec.dim {
                return Err(Miss::GiveUp(format!(
                    "서버 벡터 차원이 다릅니다({} ≠ {})", vec.len(), self.spec.dim)));
            }
            return Ok(EmbedReply::Vector(vec));
        }
        match resp.get("kind").and_then(Value::as_str) {
            // 서버가 "이 텍스트는 임베딩이 안 된다"고 답했다 — 직접 해도 같으므로 폴백하지 않는다.
            Some("embed") => Ok(EmbedReply::NoVector),
            // not_ready(모델 올리는 중)·auth(기록이 그새 바뀜) 등은 다시 찾아본다.
            other => Err(Miss::Retry(format!("서버 응답: {}", other.unwrap_or("알 수 없음")))),
        }
    }

    //------------------------------------------------------------------
    // 옛 서버 내리기
    //=> 버전·모델이 다른 서버에 정지를 보내고, 물러날 때까지 잠깐 기다린 뒤 기록을 지운다.
    //   끝내 안 물러나도 기록은 지운다 — 새 서버는 다른 포트에 뜨므로 문제없다.
    //
    // -in: rec = 옛 서버의 기록
    //
    // -out: 없음
    // -out: error = 없음(실패해도 계속한다)
    //------------------------------------------------------------------
    fn replace_old(&self, rec: &Value) {
        send_stop(rec);
        let until = Instant::now() + std::time::Duration::from_secs(2);
        while Instant::now() < until && registry::read_record(self.spec.key).as_ref() == Some(rec) {
            std::thread::sleep(std::time::Duration::from_millis(50));
        }
        if registry::read_record(self.spec.key).as_ref() == Some(rec) {
            registry::clear(self.spec.key);
        }
    }

    //------------------------------------------------------------------
    // 서버 자동 기동
    //=> 자기 자신(exe)을 `--serve` 로 따로 띄운다. 표준 입출력은 모두 버리고 부모와 분리한다.
    //   동시에 여러 호출이 띄워도 서버 쪽 잠금이 하나만 남긴다.
    //
    //   [윈도우에서 내 표준 핸들의 상속을 끄는 이유] 부른 쪽(v11)이 내 stdout 을
    //   파이프로 받아 읽고 있으면, 그 파이프 핸들이 새 서버에게 물려진다. 서버는
    //   끝나지 않으므로 부른 쪽은 "출력이 아직 안 끝났다"고 보고 영영 기다린다.
    //   자식의 표준 입출력을 NUL 로 주는 것만으로는 막지 못한다 — 상속 가능한 핸들은
    //   전부 따라간다. 그래서 띄우기 전에 내 표준 핸들의 상속 표시를 끈다
    //   (내가 그 핸들로 쓰는 데는 아무 영향이 없다).
    //
    // -in: 없음
    //
    // -out: Ok(()) = 프로세스를 띄웠다(준비됐다는 뜻은 아니다)
    // -out: error = exe 경로를 모르거나 실행에 실패하면 Err(사유)
    //------------------------------------------------------------------
    fn spawn(&mut self) -> Result<(), String> {
        self.spawned += 1;
        let exe = std::env::current_exe().map_err(|e| e.to_string())?;
        let mut cmd = Command::new(exe);
        cmd.arg("--serve")
            .arg("--model").arg(self.spec.key)
            .arg("--idle-timeout").arg(self.idle_timeout.to_string());
        // 서버가 같은 로그 파일에 남기도록 경로와 -v 를 물려준다.
        if let Some(p) = &self.log_path {
            cmd.arg("--log").arg(p);
        }
        if self.verbose {
            cmd.arg("-v");
        }
        cmd.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const DETACHED_PROCESS: u32 = 0x0000_0008;
            const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            // 콘솔 창 없이, 부모의 Ctrl+C 와도 떨어뜨려 띄운다.
            cmd.creation_flags(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW);
            no_inherit_std_handles();
        }
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            // 새 프로세스 그룹 — 부른 쪽 터미널의 Ctrl+C 가 서버까지 죽이지 않게.
            cmd.process_group(0);
        }
        linfo!("서버 자동 기동 model={} idle_timeout={}", self.spec.key, self.idle_timeout);
        cmd.spawn().map(|_| ()).map_err(|e| e.to_string())
    }
}

//------------------------------------------------------------------
// 내 표준 핸들을 자식에게 물려주지 않게 하기 (윈도우 전용)
//=> stdin·stdout·stderr 핸들의 "상속 가능" 표시를 끈다. 이유는 spawn() 설명 참고.
//
// -in: 없음
//
// -out: 없음
// -out: error = 없음(핸들이 없거나 실패해도 그대로 진행)
//------------------------------------------------------------------
#[cfg(windows)]
fn no_inherit_std_handles() {
    use std::os::windows::io::AsRawHandle;
    const HANDLE_FLAG_INHERIT: u32 = 0x0000_0001;
    extern "system" {
        fn SetHandleInformation(h: *mut std::ffi::c_void, mask: u32, flags: u32) -> i32;
    }
    let handles = [std::io::stdin().as_raw_handle(), std::io::stdout().as_raw_handle(),
                   std::io::stderr().as_raw_handle()];
    for h in handles {
        if !h.is_null() {
            // SAFETY: 이 프로세스가 가진 표준 핸들의 표시 하나만 바꾼다. 닫지도 옮기지도 않는다.
            unsafe { SetHandleInformation(h as *mut std::ffi::c_void, HANDLE_FLAG_INHERIT, 0); }
        }
    }
}

/// 기록의 서버에 정지 요청을 보낸다(실패해도 조용히 넘어간다).
fn send_stop(rec: &Value) {
    if let Some(port) = rec.get("port").and_then(Value::as_u64).filter(|p| *p > 0 && *p <= u16::MAX as u64) {
        let _ = ipc::rpc(port as u16, &json!({"op": "stop", "token": rec.get("token")}), PING_TIMEOUT);
    }
}

//------------------------------------------------------------------
// 서버 상태 조회 (--status)
//=> 살아 있는 서버가 있으면 그 기록과 준비 여부를 돌려준다.
//
// -in: model = 모델 별칭
//
// -out: Option<(Value, bool)> = (기록, 모델 준비 여부). 서버가 없으면 None
// -out: error = 없음
//------------------------------------------------------------------
pub fn status(model: &str) -> Option<(Value, bool)> {
    match registry::probe(model) {
        Liveness::Alive { rec, ready } => Some((rec, ready)),
        _ => None,
    }
}

//------------------------------------------------------------------
// 서버 정지 (--stop)
//=> 살아 있는 서버를 찾아 정지 요청을 보낸다.
//
// -in: model = 모델 별칭
//
// -out: bool = 정지 요청을 보냈으면 true, 서버가 없었으면 false
// -out: error = 없음
//------------------------------------------------------------------
pub fn stop(model: &str) -> bool {
    match registry::probe(model) {
        Liveness::Alive { rec, .. } => { send_stop(&rec); true }
        _ => false,
    }
}
