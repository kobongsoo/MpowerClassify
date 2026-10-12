//! 상주 서버(`--serve`) — 임베딩 모델을 한 번 올려 물고 있으면서 요청에 답한다.
//! 파이썬 판 `daemon/server.py` 에 대응한다.
//!
//! 기동 순서(설계 §7): 잠금 → **소켓 열기·기록 쓰기 → 그다음 모델 올리기**.
//! 소켓을 먼저 여는 이유는, 모델을 올리는 1~2초 동안에도 ping 에 "살아 있다,
//! 아직 준비 중"이라고 답하기 위해서다. 그래야 다른 프로세스가 이 서버를
//! 죽은 것으로 오판해 잠금을 빼앗지 않는다.
//!
//! 요청은 연결마다 스레드를 하나씩 써서 받는다. ping 은 곧바로 답하고,
//! 임베딩은 모델 하나를 차례로 쓴다(추론이 이미 코어를 다 쓰므로 겹쳐 돌릴 이유가 없다).

use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use super::{ipc, registry, PROTO, SERVER_IO_TIMEOUT, VERSION};
use crate::embed::{self, Embedder, ModelSpec};
use crate::{errcodes, linfo};

// 이 프로세스가 서버로 떠 있을 때의 모델 별칭 — Ctrl+C 처리기가 기록을 치울 때 본다.
static SERVING: OnceLock<&'static str> = OnceLock::new();

//------------------------------------------------------------------
// 요청 스레드들이 함께 보는 서버 상태
//=> 연결마다 스레드가 따로 돌기 때문에, 모델과 깃발들을 한데 묶어 나눠 쓴다.
//
// -필드: spec     = 이 서버가 맡은 모델 스펙
// -필드: token    = 임베딩·정지 요청에 요구하는 값(기록 파일에만 있다)
// -필드: embedder = 올린 모델(올리기 전에는 None). 한 번에 한 요청만 쓴다
// -필드: ready    = 모델이 올라왔는가(ping 이 이 값을 알린다)
// -필드: stop     = 정지 요청을 받았는가
// -필드: inflight = 지금 처리 중인 임베딩 수(유휴 종료가 일하는 중에 끊지 않게)
// -필드: last     = 마지막 요청 시각(유휴 시간 계산용)
//------------------------------------------------------------------
struct Shared {
    spec: &'static ModelSpec,
    token: String,
    embedder: Mutex<Option<Embedder>>,
    ready: AtomicBool,
    stop: AtomicBool,
    inflight: AtomicUsize,
    last: Mutex<Instant>,
}

//------------------------------------------------------------------
// 접속 인증용 토큰 만들기
//=> OS 난수 16바이트를 16진 글자로 바꾼다. 같은 PC 의 다른 계정이 포트만 알아서는
//   임베딩·정지를 시킬 수 없게 하는 값이다.
//
// -in: 없음
//
// -out: Ok(String) = 32글자 토큰
// -out: error = OS 난수를 못 얻으면 Err(사유)
//------------------------------------------------------------------
fn new_token() -> Result<String, String> {
    let mut b = [0u8; 16];
    getrandom::fill(&mut b).map_err(|e| format!("난수를 얻지 못했습니다: {}", e))?;
    Ok(b.iter().map(|x| format!("{:02x}", x)).collect())
}

//------------------------------------------------------------------
// Ctrl+C 를 받았을 때 기록 치우기
//=> 서버로 떠 있던 프로세스가 중단되면 기록·잠금 파일을 지운다. 남겨 두어도
//   다음 기동이 죽은 기록으로 걸러 내지만, 치워 두면 그 확인(ping 한 번)도 필요 없다.
//   서버가 아닌 실행에서는 아무 일도 하지 않는다.
//
// -in: 없음
//
// -out: 없음
// -out: error = 없음
//------------------------------------------------------------------
pub fn cleanup_on_interrupt() {
    if let Some(model) = SERVING.get() {
        registry::clear_if_owner(model);
    }
}

//------------------------------------------------------------------
// 서버 실행 (핵심)
//=> 잠금 → 소켓·기록 → 모델 올리기 → 정지/유휴까지 대기 → 정리.
//    1) 잠금을 못 잡으면 "이미 있음"을 알리고 0 (파이썬 판과 같다)
//    2) 소켓을 열고 접속 정보를 기록한 뒤, 요청 받는 스레드를 띄운다
//    3) 모델을 올린다. 실패하면 기록을 지우고 model_load_failed 로 끝낸다
//    4) 정지 요청이 오거나 유휴 시간이 차면 기록을 지우고 0
//
// -in: spec         = 상주시킬 모델 스펙
// -in: idle_timeout = 유휴 자동 종료(초). 0 이면 무한(자동 종료 없음)
//
// -out: i32 = 종료코드(0 정상 종료·이미 있음)
// -out: error = 잠금·소켓 실패는 internal_error, 모델 실패는 model_load_failed 로 종료
//------------------------------------------------------------------
pub fn run(spec: &'static ModelSpec, idle_timeout: u64) -> i32 {
    let model = spec.key;
    // (1) 나만 서버가 되도록 잠금. 못 잡으면 다른 서버가 주인이다.
    match registry::acquire_singleton(model) {
        Ok(true) => {}
        Ok(false) => {
            linfo!("서버 기동 취소: 이미 {} 서버가 있음", model);
            println!("model={}: 이미 실행 중인 데몬이 있습니다", model);
            return 0;
        }
        Err(why) => errcodes::fail("internal_error",
            &format!("[MpowerClassify-rs] 상주 서버를 띄울 수 없습니다 — {}", why), None),
    }
    let _ = SERVING.set(model);

    // (2) 소켓을 먼저 연다 — 모델을 올리는 동안에도 ping 에 답하려고.
    let prepared = ipc::make_server().map_err(|e| format!("소켓을 열 수 없습니다: {}", e))
        .and_then(|(srv, port)| new_token().map(|t| (srv, port, t)));
    let (srv, port, token) = match prepared {
        Ok(v) => v,
        Err(why) => {
            registry::clear_if_owner(model);
            errcodes::fail("internal_error",
                &format!("[MpowerClassify-rs] 상주 서버를 띄울 수 없습니다 — {}", why), None)
        }
    };
    // 실제로 열 모델 폴더를 기록에 남긴다 — 부르는 쪽이 자기가 쓸 모델과 같은지 본다.
    let model_dir = embed::resolved_model_dir()
        .map(|p| std::fs::canonicalize(&p).unwrap_or(p).to_string_lossy().into_owned());
    let started = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64()).unwrap_or(0.0);
    let record = json!({
        "pid": std::process::id(),
        "host": super::HOST,
        "port": port,
        "token": token,
        "version": VERSION,
        "proto": PROTO,
        "model": model,
        "dim": spec.dim,
        "idle_timeout": idle_timeout,
        "started": started,
        "exe": std::env::current_exe().ok().map(|p| p.to_string_lossy().into_owned()),
        "model_dir": model_dir,
    });
    if let Err(e) = registry::write_record(model, &record) {
        registry::clear_if_owner(model);
        errcodes::fail("internal_error",
            &format!("[MpowerClassify-rs] 상주 서버 기록을 쓸 수 없습니다 — {}", e), None);
    }
    let idle_disp = if idle_timeout == 0 { "무한(자동종료 없음)".to_string() } else { format!("{}s", idle_timeout) };
    linfo!("서버 시작 model={} pid={} port={} idle_timeout={}", model, std::process::id(), port, idle_disp);

    let shared = Arc::new(Shared {
        spec,
        token,
        embedder: Mutex::new(None),
        ready: AtomicBool::new(false),
        stop: AtomicBool::new(false),
        inflight: AtomicUsize::new(0),
        last: Mutex::new(Instant::now()),
    });
    {
        let shared = Arc::clone(&shared);
        std::thread::spawn(move || accept_loop(srv, shared));
    }

    // (3) 모델 올리기 — 이 서버의 존재 이유. 파일 확인을 먼저 한다:
    //     onnxruntime 을 못 찾은 채 load() 하면 ort 가 Err 가 아니라 패닉으로 죽는다.
    let loaded = embed::check_deployment().and_then(|()| Embedder::load().ok_or_else(|| format!(
        "모델/런타임 로드 실패({}/{}, onnxruntime.dll 확인)", embed::MODEL_ROOT, spec.local_dir)));
    match loaded {
        Ok(e) => {
            *shared.embedder.lock().unwrap() = Some(e);
            *shared.last.lock().unwrap() = Instant::now();
            shared.ready.store(true, Ordering::SeqCst);
        }
        Err(why) => {
            registry::clear_if_owner(model);
            errcodes::fail("model_load_failed",
                &format!("[MpowerClassify-rs] 상주 서버가 임베딩 모델을 올리지 못했습니다 — {}", why), None);
        }
    }
    linfo!("모델 상주 준비 완료 port={} — 요청 대기(유휴 {})", port, idle_disp);
    println!("model={} pid={} port={} 준비 완료", model, std::process::id(), port);

    // (4) 정지 요청이나 유휴 시간을 기다린다. 요청은 다른 스레드들이 받고 있다.
    loop {
        std::thread::sleep(Duration::from_millis(100));
        if shared.stop.load(Ordering::SeqCst) {
            linfo!("정지 요청 수신 → 서버 종료");
            break;
        }
        if idle_timeout > 0 && shared.inflight.load(Ordering::SeqCst) == 0 {
            let idle = shared.last.lock().map(|t| t.elapsed()).unwrap_or_default();
            if idle >= Duration::from_secs(idle_timeout) {
                linfo!("유휴 {}s 초과 → 서버 종료", idle_timeout);
                break;
            }
        }
    }
    registry::clear_if_owner(model);
    0
}

//------------------------------------------------------------------
// 연결 받기
//=> 들어오는 연결마다 스레드를 하나 띄워 맡긴다. 그래야 긴 임베딩이 도는 중에도
//   ping 이 곧바로 답한다.
//
// -in: srv    = 듣는 소켓
// -in: shared = 서버 상태
//
// -out: 없음(프로세스가 끝날 때까지 돈다)
// -out: error = 없음(연결 하나의 실패는 그 연결만 버린다)
//------------------------------------------------------------------
fn accept_loop(srv: TcpListener, shared: Arc<Shared>) {
    for conn in srv.incoming() {
        if let Ok(conn) = conn {
            let shared = Arc::clone(&shared);
            std::thread::spawn(move || handle(conn, &shared));
        }
    }
}

//------------------------------------------------------------------
// 연결 하나 처리
//=> 요청 하나를 읽고 op 에 따라 답한다. 통신이 깨지면 그 연결만 버린다.
//    - ping       : 토큰 없이 생존·준비 여부를 알린다
//    - embed_text : 토큰 확인 뒤 텍스트를 임베딩해 벡터를 돌려준다
//    - stop       : 토큰 확인 뒤 종료를 예약한다
//
// -in: conn   = 받은 연결
// -in: shared = 서버 상태
//
// -out: 없음(응답은 소켓으로 나간다)
// -out: error = 없음(통신 오류는 조용히 연결을 닫는다)
//------------------------------------------------------------------
fn handle(mut conn: TcpStream, shared: &Shared) {
    // 요청을 보내다 만 상대가 스레드를 끝없이 붙잡지 못하게 한도를 건다.
    let _ = conn.set_read_timeout(Some(SERVER_IO_TIMEOUT));
    let _ = conn.set_write_timeout(Some(SERVER_IO_TIMEOUT));
    let _ = conn.set_nodelay(true);
    let req = match ipc::recv_msg(&mut conn) {
        Ok(v) => v,
        Err(_) => return,
    };
    let op = req.get("op").and_then(Value::as_str).unwrap_or("");
    let authed = req.get("token").and_then(Value::as_str) == Some(shared.token.as_str());
    let reply = match op {
        "ping" => json!({
            "ok": true,
            "model_ready": shared.ready.load(Ordering::SeqCst),
            "model": shared.spec.key,
            "dim": shared.spec.dim,
            "version": VERSION,
            "proto": PROTO,
            "pid": std::process::id(),
        }),
        "embed_text" | "stop" if !authed =>
            json!({"ok": false, "error": "인증 실패", "kind": "auth"}),
        "embed_text" => embed_text(&req, shared),
        "stop" => {
            shared.stop.store(true, Ordering::SeqCst);
            json!({"ok": true})
        }
        // 파이썬 판의 embed(파일 경로를 받아 추출까지)는 이 판에 없다 — 추출은 늘 부른 쪽이 한다.
        other => json!({"ok": false, "error": format!("알 수 없는 op: {}", other), "kind": "bad_op"}),
    };
    let _ = ipc::send_msg(&mut conn, &reply);
}

//------------------------------------------------------------------
// embed_text 요청 처리
//=> 받은 텍스트를 상주 모델로 임베딩한다. 직접 로드할 때와 **같은 함수**
//   (Embedder::embed_document)를 부르므로 벡터가 같다 — 정제도 그 안에서 한다.
//
// -in: req    = {op, token, text}
// -in: shared = 서버 상태
//
// -out: Value = {ok:true, vector, dim, model, version} 또는 {ok:false, error, kind}
//               kind: not_ready(모델 올리는 중) · embed(빈 텍스트/추론 실패)
// -out: error = 없음(실패는 응답으로 알린다)
//------------------------------------------------------------------
fn embed_text(req: &Value, shared: &Shared) -> Value {
    if !shared.ready.load(Ordering::SeqCst) {
        return json!({"ok": false, "error": "모델을 올리는 중", "kind": "not_ready"});
    }
    let text = req.get("text").and_then(Value::as_str).unwrap_or("");
    shared.inflight.fetch_add(1, Ordering::SeqCst);
    let t0 = Instant::now();
    // 앞선 요청이 추론 중에 패닉했다면 잠금이 깨져 있다 — 그 모델은 믿을 수 없으므로
    // 서버를 내린다(다음 호출이 새로 띄운다). 이번 요청은 부른 쪽이 직접 로드로 처리한다.
    let vec = match shared.embedder.lock() {
        Ok(mut g) => g.as_mut().and_then(|e| e.embed_document(text)),
        Err(_) => {
            shared.stop.store(true, Ordering::SeqCst);
            shared.inflight.fetch_sub(1, Ordering::SeqCst);
            return json!({"ok": false, "error": "서버 내부 오류 — 재기동합니다", "kind": "not_ready"});
        }
    };
    if let Ok(mut last) = shared.last.lock() { *last = Instant::now(); }
    shared.inflight.fetch_sub(1, Ordering::SeqCst);
    // 본문은 남기지 않는다 — 글자 수와 걸린 시간만.
    linfo!("embed_text chars={} ms={} ok={}", text.chars().count(), t0.elapsed().as_millis(), vec.is_some());
    match vec {
        Some(v) => json!({"ok": true, "vector": v, "dim": shared.spec.dim,
                          "model": shared.spec.key, "version": VERSION}),
        None => json!({"ok": false, "error": "빈 텍스트이거나 임베딩 실패", "kind": "embed"}),
    }
}
