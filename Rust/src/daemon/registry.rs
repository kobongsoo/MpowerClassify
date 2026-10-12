//! 상주 서비스 기록과 "하나만 뜨기" — 파이썬 판 `daemon/registry.py` 에 대응한다.
//!
//! "지금 이 모델 서버가 어느 포트에 떠 있나"를 파일로 남기고, 같은 모델 서버가
//! 둘 뜨는 경쟁을 잠금 파일로 막는다. 모델마다 파일을 따로 둔다.
//!
//! 파이썬 판과 **일부러 다른 점** 두 가지(설계 §7):
//!  1) 파일 이름이 `daemon-rs-<model>.json` 이다. 파이썬 클라이언트는 버전이 다른
//!     데몬을 정지시키므로, 같은 파일을 쓰면 두 판이 서로의 서버를 죽인다.
//!  2) "살아 있는 서버인가"를 pid 가 아니라 **실제로 ping 이 돌아오는가**로 본다.
//!     pid 방식은 강제 종료·재부팅 뒤 그 pid 를 다른 프로그램이 받으면 영영
//!     "아직 살아 있다"고 오판해 새 서버가 뜨지 못한다.

use std::io::Write;
use std::path::PathBuf;

use serde_json::{json, Value};

use super::{ipc, LOCK_YOUNG, PING_TIMEOUT};

//------------------------------------------------------------------
// 서버 생존 판정 결과
//=> 기록·잠금 파일과 ping 응답을 보고 내린 결론이다.
//
// -필드: Alive    = 기록된 포트에서 그 pid 의 서버가 답했다(rec=기록, ready=모델 준비 여부)
// -필드: Starting = 답은 없지만 잠금이 막 생겼다 — 기록을 쓰기 직전의 기동 중일 수 있다
// -필드: Dead     = 서버가 없다(파일이 없거나, 남아 있어도 죽은 기록이다)
//------------------------------------------------------------------
pub enum Liveness {
    Alive { rec: Value, ready: bool },
    Starting,
    Dead,
}

//------------------------------------------------------------------
// 상태 폴더
//=> 기록·잠금 파일을 둘 폴더를 정하고 없으면 만든다.
//    1) CSOCLASSIFY_STATE_DIR — 서버를 띄운 계정과 부르는 계정이 다를 때 한곳으로 모은다
//    2) %LOCALAPPDATA%\MpowerClassify (파이썬 판과 같은 자리)
//    3) 홈 아래 .csoclassify
//
// -in: 없음
//
// -out: Option<PathBuf> = 상태 폴더(정할 수 없거나 못 만들면 None)
// -out: error = 없음
//------------------------------------------------------------------
pub fn state_dir() -> Option<PathBuf> {
    let env = |k: &str| std::env::var(k).ok().filter(|v| !v.is_empty());
    let dir = if let Some(d) = env("CSOCLASSIFY_STATE_DIR") {
        PathBuf::from(d)
    } else if let Some(base) = env("LOCALAPPDATA") {
        PathBuf::from(base).join("MpowerClassify")
    } else {
        PathBuf::from(env("USERPROFILE").or_else(|| env("HOME"))?).join(".csoclassify")
    };
    std::fs::create_dir_all(&dir).ok()?;
    Some(dir)
}

//------------------------------------------------------------------
// 모델별 기록 파일 경로
//=> daemon-rs-<model>.json. 모델 별칭에 파일 이름으로 못 쓰는 글자가 있으면 '_' 로 바꾼다.
//
// -in: model = 모델 별칭(예: "e5-small-ko")
//
// -out: Option<PathBuf> = 기록 파일 경로(상태 폴더를 못 정하면 None)
// -out: error = 없음
//------------------------------------------------------------------
pub fn record_path(model: &str) -> Option<PathBuf> {
    let safe: String = model.chars()
        .map(|c| if c.is_ascii_alphanumeric() || "-._".contains(c) { c } else { '_' })
        .collect();
    Some(state_dir()?.join(format!("daemon-rs-{}.json", safe)))
}

//------------------------------------------------------------------
// 모델별 잠금 파일 경로
//=> 기록 파일과 짝인 .lock. 이 파일을 "없을 때만 만들기"로 만든 쪽이 서버가 된다.
//
// -in: model = 모델 별칭
//
// -out: Option<PathBuf> = 잠금 파일 경로
// -out: error = 없음
//------------------------------------------------------------------
pub fn lock_path(model: &str) -> Option<PathBuf> {
    let mut p = record_path(model)?.into_os_string();
    p.push(".lock");
    Some(PathBuf::from(p))
}

//------------------------------------------------------------------
// 기록 읽기
//=> 서버 접속 정보를 파일에서 읽는다. 없거나 깨졌으면 None.
//
// -in: model = 모델 별칭
//
// -out: Option<Value> = {pid, port, token, version, proto, model, dim, …}
// -out: error = 없음(읽기 실패는 None)
//------------------------------------------------------------------
pub fn read_record(model: &str) -> Option<Value> {
    let text = std::fs::read_to_string(record_path(model)?).ok()?;
    serde_json::from_str(&text).ok()
}

//------------------------------------------------------------------
// 기록 쓰기(임시 파일 → 이름 바꾸기)
//=> 읽는 쪽이 반쯤 쓰인 파일을 보지 않도록, 임시 파일에 다 쓴 뒤 이름을 바꾼다.
//   리눅스에서는 0600 으로 만든다 — 토큰이 들어 있어 다른 계정이 읽으면 안 된다.
//
// -in: model  = 모델 별칭
// -in: record = 저장할 접속 정보
//
// -out: () = 저장됨
// -out: error = 쓰기·이름 바꾸기 실패 시 io::Error
//------------------------------------------------------------------
pub fn write_record(model: &str, record: &Value) -> std::io::Result<()> {
    let path = record_path(model)
        .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::NotFound, "상태 폴더를 정할 수 없음"))?;
    // 임시 이름에 pid 를 넣는다 — 동시에 뜬 두 프로세스가 같은 임시 파일을 밟지 않게.
    let tmp = path.with_extension(format!("json.{}.tmp", std::process::id()));
    {
        let mut opt = std::fs::OpenOptions::new();
        opt.write(true).create(true).truncate(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            opt.mode(0o600);
        }
        let mut f = opt.open(&tmp)?;
        f.write_all(record.to_string().as_bytes())?;
    }
    std::fs::rename(&tmp, &path)
}

//------------------------------------------------------------------
// 기록·잠금 지우기
//=> 죽은 기록을 치울 때 쓴다. 파일이 없어도 조용히 넘어간다.
//
// -in: model = 모델 별칭
//
// -out: 없음
// -out: error = 없음(삭제 실패 무시)
//------------------------------------------------------------------
pub fn clear(model: &str) {
    for p in [record_path(model), lock_path(model)].into_iter().flatten() {
        let _ = std::fs::remove_file(p);
    }
}

//------------------------------------------------------------------
// 내가 주인일 때만 기록·잠금 지우기
//=> 서버가 끝날 때 부른다. 잠금 파일에 적힌 pid 가 내 것일 때만 지운다 —
//   버전 교체로 내가 물러나는 사이 새 서버가 이미 자기 파일을 써 두었을 수 있고,
//   그것을 지우면 멀쩡한 새 서버가 "없는 서버"가 된다.
//
// -in: model = 모델 별칭
//
// -out: 없음
// -out: error = 없음
//------------------------------------------------------------------
pub fn clear_if_owner(model: &str) {
    let mine = std::process::id().to_string();
    let owner = lock_path(model).and_then(|p| std::fs::read_to_string(p).ok());
    if owner.map(|s| s.trim() == mine).unwrap_or(false) {
        clear(model);
    }
}

/// 잠금 파일이 막 생겼는가 — 기록을 쓰기 직전의 기동 중일 수 있는 시간대다.
fn lock_is_young(model: &str) -> bool {
    lock_path(model)
        .and_then(|p| std::fs::metadata(p).ok())
        .and_then(|m| m.modified().ok())
        .and_then(|t| t.elapsed().ok())
        .map(|age| age < LOCK_YOUNG)
        .unwrap_or(false)
}

//------------------------------------------------------------------
// 서버가 살아 있는가 (핵심)
//=> 기록된 포트에 ping 을 보내 본다.
//    1) 응답이 오고 그 pid 가 기록과 같다 → Alive (모델 준비 여부도 함께)
//    2) 아니지만 잠금이 막 생겼다 → Starting (잠금을 잡고 기록을 쓰기 직전일 수 있다)
//    3) 그 밖 → Dead (서버가 죽고 파일만 남았거나, 그 포트를 다른 프로그램이 쓴다)
//   서버는 모델을 올리는 동안에도 ping 에 답하므로(model_ready=false),
//   "느려서 답이 없다"와 "죽었다"를 시간으로 추측할 필요가 없다.
//
// -in: model = 모델 별칭
//
// -out: Liveness = 판정 결과
// -out: error = 없음(통신 실패는 전부 Starting/Dead 로 접는다)
//------------------------------------------------------------------
pub fn probe(model: &str) -> Liveness {
    if let Some(rec) = read_record(model) {
        let port = rec.get("port").and_then(Value::as_u64).unwrap_or(0);
        if port > 0 && port <= u16::MAX as u64 {
            if let Ok(pong) = ipc::rpc(port as u16, &json!({"op": "ping"}), PING_TIMEOUT) {
                // pid 까지 맞춰 본다 — 죽은 기록의 포트를 우연히 다른 서버가 받았을 수 있다.
                let same = pong.get("ok").and_then(Value::as_bool).unwrap_or(false)
                    && pong.get("pid").is_some() && pong.get("pid") == rec.get("pid");
                if same {
                    let ready = pong.get("model_ready").and_then(Value::as_bool).unwrap_or(false);
                    return Liveness::Alive { rec, ready };
                }
            }
        }
    }
    if lock_is_young(model) { Liveness::Starting } else { Liveness::Dead }
}

//------------------------------------------------------------------
// 하나만 뜨게 하는 잠금 잡기
//=> 잠금 파일을 "없을 때만 만들기"로 만든다. 만들었으면 내가 유일한 서버다.
//    1) 만들기 성공 → 내 pid 를 적고 true
//    2) 이미 있음 + 서버가 살아 있거나 기동 중 → false (나는 물러난다)
//    3) 이미 있음 + 죽은 기록 → 지우고 한 번 더
//
// -in: model = 모델 별칭
//
// -out: Ok(bool) = 잠금을 잡았으면 true, 다른 서버가 주인이면 false
// -out: error = 상태 폴더를 못 쓰는 등 잠금 자체를 시도할 수 없으면 Err(사유)
//------------------------------------------------------------------
pub fn acquire_singleton(model: &str) -> Result<bool, String> {
    let lp = lock_path(model).ok_or_else(|| "상태 폴더를 정할 수 없습니다".to_string())?;
    for _ in 0..2 {
        match std::fs::OpenOptions::new().write(true).create_new(true).open(&lp) {
            Ok(mut f) => {
                let _ = f.write_all(std::process::id().to_string().as_bytes());
                return Ok(true);
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                match probe(model) {
                    Liveness::Alive { .. } | Liveness::Starting => return Ok(false),
                    // 죽은 기록 — 치우고 다시 만든다.
                    Liveness::Dead => clear(model),
                }
            }
            Err(e) => return Err(format!("잠금 파일을 만들 수 없습니다: {} ({})", lp.display(), e)),
        }
    }
    Ok(false)
}

#[cfg(test)]
mod tests {
    use super::*;

    // 이 모듈의 시험은 환경변수(CSOCLASSIFY_STATE_DIR)를 건드린다 — cargo test 는
    // 스레드를 나눠 돌리므로, 서로 밟지 않게 한 시험 안에서 차례로 본다.
    #[test]
    fn 잠금과_죽은_기록_판정() {
        let tmp = std::env::temp_dir().join(format!("mpc_reg_test_{}", std::process::id()));
        std::env::set_var("CSOCLASSIFY_STATE_DIR", &tmp);
        let m = "test-model";

        // (1) 아무것도 없으면 Dead, 잠금은 처음 한 번만 잡힌다.
        assert!(matches!(probe(m), Liveness::Dead));
        assert_eq!(acquire_singleton(m), Ok(true));
        // 방금 만든 잠금은 '기동 중'으로 보여, 두 번째는 물러난다.
        assert!(matches!(probe(m), Liveness::Starting));
        assert_eq!(acquire_singleton(m), Ok(false));

        // (2) 죽은 기록: 살아 있는 pid(이 시험 프로세스) + 아무도 안 듣는 포트 + 오래된 잠금.
        //     pid 만 보면 "살아 있다"고 속지만, ping 이 안 돌아오므로 Dead 여야 한다.
        let port = { let (srv, p) = ipc::make_server().unwrap(); drop(srv); p };
        write_record(m, &json!({"pid": std::process::id(), "port": port})).unwrap();
        let old = std::time::SystemTime::now() - std::time::Duration::from_secs(60);
        let f = std::fs::OpenOptions::new().write(true).open(lock_path(m).unwrap()).unwrap();
        f.set_modified(old).unwrap();
        drop(f);
        assert!(matches!(probe(m), Liveness::Dead));
        // 죽은 기록은 치우고 잠금을 다시 잡는다.
        assert_eq!(acquire_singleton(m), Ok(true));
        assert!(read_record(m).is_none(), "죽은 기록은 지워져야 한다");

        // (3) 내 pid 가 적힌 잠금이면 지우고, 남의 것이면 건드리지 않는다.
        std::fs::write(lock_path(m).unwrap(), "1").unwrap();
        clear_if_owner(m);
        assert!(lock_path(m).unwrap().is_file(), "남의 잠금은 그대로 둔다");
        std::fs::write(lock_path(m).unwrap(), std::process::id().to_string()).unwrap();
        clear_if_owner(m);
        assert!(!lock_path(m).unwrap().is_file());

        std::env::remove_var("CSOCLASSIFY_STATE_DIR");
        let _ = std::fs::remove_dir_all(&tmp);
    }
}
