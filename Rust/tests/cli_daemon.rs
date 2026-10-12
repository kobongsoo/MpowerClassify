//! 임베딩 상주 서버(--serve / --daemon / --status / --stop)의 CLI 계약.
//! 설계: plan/Rust-임베딩모델-상주서비스-설계.html §13
//!
//! 실제 실행 파일을 불러 본다. 가장 중요한 약속은 하나다 —
//! **--daemon 을 붙여도 결과가 한 글자도, 벡터 한 비트도 달라지지 않는다.**
//!
//! 임베딩 모델과 onnxruntime 은 저장소에 없다(배포물). 아래 자리에서 찾고,
//! 없으면 모델이 필요한 시험은 건너뛴다(통과로 세지 않도록 stderr 에 알린다):
//!   · 모델 : MPC_TEST_MODELS_DIR → target/release/models
//!   · 런타임: ORT_DYLIB_PATH → target/release/onnxruntime.dll

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Output, Stdio};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

const MODEL: &str = "e5-small-ko";

/// 시험 하나가 쓰는 격리된 환경 — 상태 폴더가 따로라 시험마다 서버도 따로 뜬다.
/// 끝날 때(실패로 끝나도) 그 서버를 내린다.
struct Env {
    dir: PathBuf,
    models: PathBuf,
    dylib: PathBuf,
}

impl Env {
    /// 모델·런타임을 찾아 환경을 만든다. 못 찾으면 None(시험을 건너뛴다).
    fn new(tag: &str) -> Option<Env> {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("target").join("release");
        let models = std::env::var_os("MPC_TEST_MODELS_DIR").map(PathBuf::from)
            .unwrap_or_else(|| root.join("models"));
        let dylib = std::env::var_os("ORT_DYLIB_PATH").map(PathBuf::from).unwrap_or_else(|| {
            root.join(if cfg!(windows) { "onnxruntime.dll" } else { "libonnxruntime.so" })
        });
        if !models.join(MODEL).join("model.onnx").is_file() || !dylib.is_file() {
            eprintln!("[건너뜀] {} — 임베딩 모델/런타임이 없다({} · {})",
                      tag, models.display(), dylib.display());
            return None;
        }
        Some(Env::with(tag, models, dylib))
    }

    /// 모델 자리를 직접 주어 환경을 만든다(모델이 없는 배포를 흉내 낼 때).
    fn with(tag: &str, models: PathBuf, dylib: PathBuf) -> Env {
        let dir = std::env::temp_dir().join(format!("csors_daemon_{}_{}", tag, std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("state")).unwrap();
        Env { dir, models, dylib }
    }

    fn state(&self) -> PathBuf { self.dir.join("state") }
    fn record(&self) -> PathBuf { self.state().join(format!("daemon-rs-{}.json", MODEL)) }
    fn lock(&self) -> PathBuf { self.state().join(format!("daemon-rs-{}.json.lock", MODEL)) }

    /// 실행 파일 명령 — 개발자 PC 의 설정이 시험을 바꾸지 못하게 환경변수를 전부 정한다.
    fn cmd(&self, args: &[&str]) -> Command {
        let mut c = Command::new(env!("CARGO_BIN_EXE_MpowerClassify-rs"));
        c.args(args)
            .env("CSOCLASSIFY_STATE_DIR", self.state())
            .env("CSOCLASSIFY_LOGDIR", self.dir.join("log"))
            .env("CSOCLASSIFY_POLICY_DIR", self.dir.join("no_policy"))
            .env("CSOCLASSIFY_MODELS_DIR", &self.models)
            .env("ORT_DYLIB_PATH", &self.dylib)
            // 캐시 폴더(%LOCALAPPDATA%/MpowerClassify/models)에 깔린 모델을 집지 않게.
            .env("LOCALAPPDATA", self.dir.join("appdata"))
            .env_remove("CSO_MODEL");
        c
    }

    /// 실행하고 끝까지 기다린다. 출력이 끝나지 않으면(자동 기동된 서버가 내 출력
    /// 파이프를 붙잡은 경우) 영영 멈추는 대신 실패로 끝낸다 — 설계 §13 시험 6.
    fn run(&self, args: &[&str]) -> Output {
        let mut c = self.cmd(args);
        let shown = args.join(" ");
        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || { let _ = tx.send(c.output()); });
        rx.recv_timeout(Duration::from_secs(180))
            .unwrap_or_else(|_| panic!("출력이 끝나지 않는다(서버가 출력 파이프를 붙잡았나?): {}", shown))
            .expect("실행 파일을 부르지 못했다")
    }

    /// 서버를 직접 띄운다(--serve). 출력은 버린다.
    fn serve(&self, extra: &[&str]) -> Child {
        let mut args = vec!["--serve", "--model", MODEL];
        args.extend_from_slice(extra);
        self.cmd(&args).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null())
            .spawn().expect("서버를 띄우지 못했다")
    }

    /// --status 의 한 줄.
    fn status(&self) -> String {
        String::from_utf8_lossy(&self.run(&["--status"]).stdout).trim().to_string()
    }

    /// 서버가 준비될 때까지 기다리고 그 pid 를 돌려준다.
    fn wait_ready(&self) -> u64 {
        let until = Instant::now() + Duration::from_secs(90);
        while Instant::now() < until {
            let s = self.status();
            if s.contains("ready=true") {
                return field(&s, "pid").parse().expect("pid");
            }
            std::thread::sleep(Duration::from_millis(200));
        }
        panic!("서버가 준비되지 않는다: {}", self.status());
    }

    /// 시험 문서를 만든다.
    fn doc(&self, name: &str, text: &str) -> String {
        let d = self.dir.join("docs");
        std::fs::create_dir_all(&d).unwrap();
        let p = d.join(name);
        std::fs::write(&p, text).unwrap();
        p.to_string_lossy().into_owned()
    }

    /// 분류를 돌려 문서 레코드(벡터 포함)를 파일 이름 차례로 돌려준다.
    fn classify(&self, target: &[&str], extra: &[&str], out_name: &str) -> (Output, Vec<Value>) {
        let out = self.dir.join(out_name);
        let rules = Path::new(env!("CARGO_MANIFEST_DIR")).join("resources").join("policy").join("cso_rule.yaml");
        let mut args: Vec<&str> = target.to_vec();
        args.extend(["--cso-rules", rules.to_str().unwrap(), "--with-vector", "--out", out.to_str().unwrap()]);
        args.extend_from_slice(extra);
        let o = self.run(&args);
        let all: Vec<Value> = std::fs::read_to_string(&out).ok()
            .and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default();
        // 결과 배열에는 run(실행 정보)·summary 도 들어 있다 — 문서 레코드만 고른다.
        let mut recs: Vec<Value> = all.into_iter().filter(|r| r.get("file").is_some()).collect();
        recs.sort_by_key(|r| r["file"].as_str().unwrap_or("").to_string());
        (o, recs)
    }
}

impl Drop for Env {
    fn drop(&mut self) {
        // 시험이 띄운 서버를 내린다 — 남기면 개발자 PC 에 모델을 문 프로세스가 쌓인다.
        let _ = self.cmd(&["--stop"]).output();
        std::thread::sleep(Duration::from_millis(300));
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

/// "model=… pid=123 port=…" 에서 칸 하나를 꺼낸다.
fn field(line: &str, key: &str) -> String {
    line.split_whitespace()
        .find_map(|w| w.strip_prefix(&format!("{}=", key)).map(str::to_string))
        .unwrap_or_default()
}

/// 벡터를 f32 비트열로 — "거의 같다"가 아니라 "완전히 같다"를 보려고.
fn bits(rec: &Value) -> Vec<u32> {
    rec["vector"].as_array().map(|a| a.iter()
        .map(|x| (x.as_f64().unwrap() as f32).to_bits()).collect()).unwrap_or_default()
}

/// 실행마다 달라지는 칸(생성 시각)을 뺀 레코드.
fn stable(rec: &Value) -> Value {
    let mut r = rec.clone();
    if let Some(m) = r.get_mut("meta").and_then(Value::as_object_mut) { m.remove("ts"); }
    r
}

/// 서버와 직접 말한다(길이 4바이트 + JSON) — 규약 자체를 볼 때 쓴다.
fn raw_rpc(port: u16, msg: &Value) -> Value {
    let mut s = TcpStream::connect(("127.0.0.1", port)).unwrap();
    s.set_read_timeout(Some(Duration::from_secs(30))).unwrap();
    let body = serde_json::to_vec(msg).unwrap();
    s.write_all(&(body.len() as u32).to_be_bytes()).unwrap();
    s.write_all(&body).unwrap();
    let mut head = [0u8; 4];
    s.read_exact(&mut head).unwrap();
    let mut buf = vec![0u8; u32::from_be_bytes(head) as usize];
    s.read_exact(&mut buf).unwrap();
    serde_json::from_slice(&buf).unwrap()
}

const SHORT: &str = "회의록\n\n2026년 3분기 영업 실적 검토 회의. 참석자: 영업팀 전원.\n매출 목표 대비 달성률을 논의했다.\n";

/// 여러 청크로 나뉘는 긴 문서(512토큰을 넘긴다) — 청크 평균까지 같은지 보려고.
fn long_text() -> String {
    (0..400).map(|i| format!("제{}조 계약 당사자는 본 계약의 내용을 성실히 이행하며 분기별 정산 결과를 서면으로 통보한다.\n", i))
        .collect()
}

// 시험 1·2·3 — 서버 없이 --daemon 을 부르면 자동으로 뜨고, 결과와 벡터가 직접 로드와 같다.
#[test]
fn 결과와_벡터가_직접_로드와_같다() {
    let Some(env) = Env::new("same") else { return };
    env.doc("a_short.txt", SHORT);
    env.doc("b_long.txt", &long_text());
    env.doc("c_mixed.txt", "Quarterly report 2026\r\n\r\n매출  1,200  백만원\t(전년 대비 +8%)\r\n");
    let dir = env.dir.join("docs");
    let target = ["--dir", dir.to_str().unwrap()];

    let (o, local) = env.classify(&target, &["--no-daemon"], "local.json");
    assert_eq!(o.status.code(), Some(0), "{}", String::from_utf8_lossy(&o.stderr));
    assert_eq!(local.len(), 3);
    assert!(local.iter().all(|r| bits(r).len() == 384), "직접 로드 벡터가 있어야 비교가 된다");
    assert!(env.status().contains("실행 중인 데몬 없음"), "--no-daemon 은 서버를 띄우지 않는다");

    // 첫 호출: 서버가 없으므로 자동 기동된다.
    let (o, first) = env.classify(&target, &["--daemon"], "d1.json");
    assert_eq!(o.status.code(), Some(0), "{}", String::from_utf8_lossy(&o.stderr));
    let pid = env.wait_ready();
    // 둘째 호출: 떠 있는 서버를 그대로 쓴다(새로 뜨지 않는다).
    let (o, second) = env.classify(&target, &["--daemon"], "d2.json");
    assert_eq!(o.status.code(), Some(0), "{}", String::from_utf8_lossy(&o.stderr));
    assert_eq!(env.wait_ready(), pid, "떠 있는 서버를 다시 써야 한다");
    assert!(!String::from_utf8_lossy(&o.stderr).contains("직접 읽습니다"), "폴백하면 안 된다");

    for (name, got) in [("첫 호출", &first), ("둘째 호출", &second)] {
        assert_eq!(got.len(), local.len(), "{}", name);
        for (l, d) in local.iter().zip(got.iter()) {
            assert_eq!(bits(l), bits(d), "{}: 벡터가 비트 단위로 같아야 한다 — {}", name, l["file"]);
            // assert_eq 로 두면 실패 때 384칸 벡터 두 벌이 통째로 찍힌다 — 참/거짓만 본다.
            assert!(stable(l) == stable(d), "{}: 레코드가 같아야 한다 — {}", name, l["file"]);
        }
    }
}

// 시험 9 — 임베딩이 필요 없는 실행은 --daemon 을 줘도 서버를 띄우지 않는다.
#[test]
fn 임베딩이_필요_없으면_서버를_띄우지_않는다() {
    let Some(env) = Env::new("noneed") else { return };
    let f = env.doc("a.txt", SHORT);
    let rules = Path::new(env!("CARGO_MANIFEST_DIR")).join("resources").join("policy").join("cso_rule.yaml");
    // --with-vector 도 seed 도 없으면 임베딩 범위가 'none' 이다.
    let o = env.run(&["--file", &f, "--cso-rules", rules.to_str().unwrap(), "--daemon"]);
    assert_eq!(o.status.code(), Some(0), "{}", String::from_utf8_lossy(&o.stderr));
    assert!(!env.record().exists() && !env.lock().exists(), "서버 기록이 생기면 안 된다");
    assert!(env.status().contains("실행 중인 데몬 없음"));
}

// 시험 4 — 서버가 죽고 기록만 남았어도(그 pid 를 다른 프로세스가 쓰고 있어도) 걸러 내고 새로 띄운다.
#[test]
fn 죽은_기록은_걸러_내고_새로_띄운다() {
    let Some(env) = Env::new("stale") else { return };
    // 아무도 듣지 않는 포트 + 살아 있는 pid(이 시험 프로세스) + 오래된 잠금.
    let port = { let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap(); l.local_addr().unwrap().port() };
    std::fs::write(env.record(), json!({
        "pid": std::process::id(), "host": "127.0.0.1", "port": port, "token": "x",
        "version": env!("CARGO_PKG_VERSION"), "proto": 1, "model": MODEL, "dim": 384,
    }).to_string()).unwrap();
    std::fs::write(env.lock(), std::process::id().to_string()).unwrap();
    let old = std::time::SystemTime::now() - Duration::from_secs(600);
    std::fs::OpenOptions::new().write(true).open(env.lock()).unwrap().set_modified(old).unwrap();

    assert!(env.status().contains("실행 중인 데몬 없음"), "죽은 기록을 살아 있다고 하면 안 된다");
    let f = env.doc("a.txt", SHORT);
    let (o, recs) = env.classify(&["--file", &f], &["--daemon"], "d.json");
    assert_eq!(o.status.code(), Some(0), "{}", String::from_utf8_lossy(&o.stderr));
    assert_eq!(bits(&recs[0]).len(), 384);
    assert!(!String::from_utf8_lossy(&o.stderr).contains("직접 읽습니다"),
            "기동 대기를 다 채우고 폴백하면 안 된다: {}", String::from_utf8_lossy(&o.stderr));
    assert_ne!(env.wait_ready(), std::process::id() as u64, "새 서버가 떠야 한다");
}

// 시험 5 — --serve 를 동시에 여러 개 띄워도 하나만 남는다. 규약(인증·모르는 요청)도 함께 본다.
#[test]
fn 서버는_하나만_남고_토큰_없이는_임베딩하지_않는다() {
    let Some(env) = Env::new("single") else { return };
    let mut kids: Vec<Child> = (0..4).map(|_| env.serve(&[])).collect();
    let pid = env.wait_ready();
    // 진 쪽들이 물러날 시간을 준다.
    let until = Instant::now() + Duration::from_secs(30);
    let mut alive = kids.len();
    while Instant::now() < until {
        alive = kids.iter_mut().map(|k| k.try_wait().unwrap().is_none()).filter(|a| *a).count();
        if alive == 1 { break; }
        std::thread::sleep(Duration::from_millis(200));
    }
    assert_eq!(alive, 1, "서버는 하나만 남아야 한다");
    for k in kids.iter_mut() {
        if let Some(st) = k.try_wait().unwrap() {
            assert_eq!(st.code(), Some(0), "물러난 쪽은 정상 종료(0)여야 한다");
        }
    }

    let rec: Value = serde_json::from_str(&std::fs::read_to_string(env.record()).unwrap()).unwrap();
    assert_eq!(rec["pid"].as_u64(), Some(pid));
    let port = rec["port"].as_u64().unwrap() as u16;
    let pong = raw_rpc(port, &json!({"op": "ping"}));
    assert_eq!((pong["ok"].as_bool(), pong["model_ready"].as_bool(), pong["dim"].as_u64()),
               (Some(true), Some(true), Some(384)));
    // 토큰이 틀리면 임베딩도 정지도 받지 않는다.
    for op in ["embed_text", "stop"] {
        let r = raw_rpc(port, &json!({"op": op, "token": "wrong", "text": "가"}));
        assert_eq!((r["ok"].as_bool(), r["kind"].as_str()), (Some(false), Some("auth")), "{}", op);
    }
    // 파이썬 판의 embed(파일 경로)는 이 판에 없다.
    let r = raw_rpc(port, &json!({"op": "embed", "token": rec["token"], "path": "x"}));
    assert_eq!(r["kind"].as_str(), Some("bad_op"));
    // 빈 텍스트는 '임베딩 실패'로 답한다(직접 로드의 None 과 같은 뜻).
    let r = raw_rpc(port, &json!({"op": "embed_text", "token": rec["token"], "text": "  \n"}));
    assert_eq!((r["ok"].as_bool(), r["kind"].as_str()), (Some(false), Some("embed")));
    assert_eq!(env.wait_ready(), pid, "틀린 정지 요청에 서버가 내려가면 안 된다");

    // --stop 은 서버를 내리고 기록을 지운다.
    let o = env.run(&["--stop"]);
    assert!(String::from_utf8_lossy(&o.stdout).contains("데몬 정지 요청 전송"));
    let until = Instant::now() + Duration::from_secs(10);
    while Instant::now() < until && env.record().exists() {
        std::thread::sleep(Duration::from_millis(100));
    }
    assert!(!env.record().exists() && !env.lock().exists(), "정지하면 기록·잠금이 지워져야 한다");
    assert!(String::from_utf8_lossy(&env.run(&["--stop"]).stdout).contains("정지할 데몬 없음"));
}

// 시험 8 — exe 를 교체했는데 옛 서버가 남은 경우: 옛 서버를 내리고 새로 띄운다.
#[test]
fn 버전이_다른_서버는_교체한다() {
    let Some(env) = Env::new("version") else { return };
    let mut old = env.serve(&[]);
    let old_pid = env.wait_ready();
    // 기록의 버전만 옛것으로 바꾼다 — 포트·토큰은 살아 있는 서버 그대로다.
    let mut rec: Value = serde_json::from_str(&std::fs::read_to_string(env.record()).unwrap()).unwrap();
    rec["version"] = json!("0.0.0-old");
    std::fs::write(env.record(), rec.to_string()).unwrap();

    let f = env.doc("a.txt", SHORT);
    let (o, recs) = env.classify(&["--file", &f], &["--daemon"], "d.json");
    assert_eq!(o.status.code(), Some(0), "{}", String::from_utf8_lossy(&o.stderr));
    assert_eq!(bits(&recs[0]).len(), 384);
    assert_ne!(env.wait_ready(), old_pid, "새 서버로 바뀌어야 한다");
    // 옛 서버는 정지 요청을 받고 끝나 있어야 한다.
    let until = Instant::now() + Duration::from_secs(10);
    while Instant::now() < until && old.try_wait().unwrap().is_none() {
        std::thread::sleep(Duration::from_millis(100));
    }
    assert!(old.try_wait().unwrap().is_some(), "옛 서버가 내려가야 한다");
}

// 시험 10 — 동시에 여러 호출이 와도 서로 벡터가 섞이지 않는다.
#[test]
fn 동시_호출에서도_벡터가_섞이지_않는다() {
    let Some(env) = Env::new("concurrent") else { return };
    let files: Vec<String> = (0..8).map(|i| env.doc(
        &format!("doc{}.txt", i),
        &format!("문서 {}번\n\n{}", i, "주제가 서로 다른 본문입니다. ".repeat(20 + i * 30)))).collect();
    let dir = env.dir.join("docs");
    let (o, local) = env.classify(&["--dir", dir.to_str().unwrap()], &["--no-daemon"], "local.json");
    assert_eq!(o.status.code(), Some(0), "{}", String::from_utf8_lossy(&o.stderr));
    assert_eq!(local.len(), 8);

    let mut srv = env.serve(&[]);
    env.wait_ready();
    let env_ref = &env;
    let got: Vec<Vec<u32>> = std::thread::scope(|s| {
        let hs: Vec<_> = files.iter().enumerate().map(|(i, f)| s.spawn(move || {
            let (o, recs) = env_ref.classify(&["--file", f], &["--daemon"], &format!("d{}.json", i));
            assert_eq!(o.status.code(), Some(0), "{}", String::from_utf8_lossy(&o.stderr));
            bits(&recs[0])
        })).collect();
        hs.into_iter().map(|h| h.join().unwrap()).collect()
    });
    for (i, v) in got.iter().enumerate() {
        assert_eq!(v, &bits(&local[i]), "doc{} 의 벡터가 직접 로드와 같아야 한다", i);
    }
    assert!(srv.try_wait().unwrap().is_none(), "서버는 계속 떠 있어야 한다");
}

// 시험 11 — --idle-timeout 이 지나면 스스로 끝나고 기록을 지운다.
#[test]
fn 유휴_시간이_지나면_스스로_끝난다() {
    let Some(env) = Env::new("idle") else { return };
    let mut srv = env.serve(&["--idle-timeout", "2"]);
    env.wait_ready();
    let until = Instant::now() + Duration::from_secs(20);
    while Instant::now() < until && srv.try_wait().unwrap().is_none() {
        std::thread::sleep(Duration::from_millis(200));
    }
    assert_eq!(srv.try_wait().unwrap().and_then(|s| s.code()), Some(0), "유휴 종료는 정상 종료(0)다");
    assert!(!env.record().exists() && !env.lock().exists(), "끝나면 기록·잠금이 지워져야 한다");
}

// 시험 7 — 모델이 없는 배포: --daemon 이어도 직접 로드 때와 같은 종료코드(2)로 알린다.
#[test]
fn 모델이_없으면_daemon_이어도_종료코드_2() {
    let tmp = std::env::temp_dir().join(format!("csors_daemon_nomodel_models_{}", std::process::id()));
    std::fs::create_dir_all(&tmp).unwrap();
    // 모델 폴더는 비어 있고 런타임도 없는 경로다 — 모델 파일 없이도 도는 시험이라 건너뛰지 않는다.
    let env = Env::with("nomodel", tmp.clone(), tmp.join("no_onnxruntime"));
    let f = env.doc("a.txt", SHORT);
    for flag in ["--no-daemon", "--daemon"] {
        let (o, recs) = env.classify(&["--file", &f], &[flag], "o.json");
        assert_eq!(o.status.code(), Some(2), "{}: {}", flag, String::from_utf8_lossy(&o.stderr));
        // 규칙으로 정해진 결과는 그대로 나간다(벡터만 없다).
        assert_eq!(recs.len(), 1, "{}", flag);
        assert!(bits(&recs[0]).is_empty(), "{}", flag);
    }
    assert!(env.status().contains("실행 중인 데몬 없음"), "모델을 못 올린 서버가 남으면 안 된다");
    assert!(!env.lock().exists(), "실패한 서버의 잠금이 남으면 안 된다");
    drop(env);
    let _ = std::fs::remove_dir_all(&tmp);
}
