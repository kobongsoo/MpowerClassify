//! 사이냅(snf_exe) 추출기 옵션 통합 시험 — 가짜 snf(examples/fake_snf.rs)로 흐름을 본다.
//! 설계: plan/Rust-사이냅추출기-옵션-설계-20261019.html §9.
//!
//! 진짜 사이냅이 없어도 돌도록 가짜 snf 를 임시 폴더의 synap/<os>/ 에 두고
//! CSOCLASSIFY_SYNAP_DIR 로 가리킨다. 가짜 snf 는 FAKE_SNF_MODE 로 상황을 고르고,
//! FAKE_SNF_LOG 에 불린 기록을 남긴다(불리지 않아야 하는 경우를 확인한다).

use std::path::{Path, PathBuf};
use std::process::{Command, Output};

//------------------------------------------------------------------
// 가짜 snf 실행파일 경로(없으면 한 번 짓는다)
//
// -in: 없음
//
// -out: PathBuf = target/<profile>/examples/fake_snf(.exe)
// -out: error = 짓지 못하면 panic
//------------------------------------------------------------------
fn fake_snf() -> PathBuf {
    let exe = std::env::current_exe().unwrap();
    let profile_dir = exe.parent().unwrap().parent().unwrap();
    let p = profile_dir.join("examples").join(format!("fake_snf{}", std::env::consts::EXE_SUFFIX));
    // 이 시험만 골라 돌리면 examples 가 지어지지 않는다 — 여기서 한 번 짓는다.
    static BUILD: std::sync::Once = std::sync::Once::new();
    BUILD.call_once(|| {
        let cargo = std::env::var("CARGO").unwrap_or_else(|_| "cargo".into());
        let mut c = Command::new(cargo);
        c.args(["build", "--example", "fake_snf"]).current_dir(env!("CARGO_MANIFEST_DIR"));
        if profile_dir.file_name().map_or(false, |n| n == "release") {
            c.arg("--release");
        }
        let o = c.output().expect("cargo 를 부르지 못했다");
        assert!(o.status.success(), "가짜 snf 를 짓지 못했다: {}", String::from_utf8_lossy(&o.stderr));
    });
    p
}

//------------------------------------------------------------------
// 시험 한 건의 작업장 — 임시 폴더 · 사이냅 폴더 · 로그 · 시스템 임시 폴더
//
// -필드: dir = 이 시험 전용 임시 폴더
//------------------------------------------------------------------
struct Env {
    dir: PathBuf,
}

impl Env {
    //--------------------------------------------------------------
    // 작업장 만들기 — 가짜 snf 를 synap/<os>/snf_exe(.exe) 로 복사해 둔다
    //
    // -in: tag = 폴더 이름 꼬리
    //
    // -out: Env
    // -out: error = 파일을 못 만들면 panic
    //--------------------------------------------------------------
    fn new(tag: &str) -> Env {
        let dir = std::env::temp_dir().join(format!("csors_synap_{}_{}", tag, std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let sub = if cfg!(windows) { "windows" } else { "linux" };
        let name = if cfg!(windows) { "snf_exe.exe" } else { "snf_exe" };
        let sdir = dir.join("synap").join(sub);
        std::fs::create_dir_all(&sdir).unwrap();
        std::fs::copy(fake_snf(), sdir.join(name)).unwrap();
        std::fs::create_dir_all(dir.join("tmp")).unwrap();
        Env { dir }
    }

    //--------------------------------------------------------------
    // 문서 하나 두기
    //
    // -in: name = 파일 이름 · data = 내용
    //
    // -out: String = 파일 경로
    // -out: error = 못 쓰면 panic
    //--------------------------------------------------------------
    fn store(&self, name: &str, data: &[u8]) -> String {
        let p = self.dir.join(name);
        std::fs::write(&p, data).unwrap();
        p.to_string_lossy().into_owned()
    }

    //--------------------------------------------------------------
    // MpowerClassify-rs 를 돌린다 — 사이냅 폴더 · 가짜 snf 모드 · 로그 · 임시 폴더를 이 작업장으로
    //
    // -in: args = 명령줄 인자 · mode = FAKE_SNF_MODE(None 이면 ok)
    //
    // -out: Output
    // -out: error = 실행 못 하면 panic
    //--------------------------------------------------------------
    fn run(&self, args: &[&str], mode: Option<&str>) -> Output {
        let tmp = self.dir.join("tmp");
        let mut c = Command::new(env!("CARGO_BIN_EXE_MpowerClassify-rs"));
        c.args(args)
            .env("CSOCLASSIFY_SYNAP_DIR", self.dir.join("synap"))
            .env("FAKE_SNF_LOG", self.dir.join("snf_calls.log"))
            .env("CSOCLASSIFY_LOGDIR", self.dir.join("log"))
            .env_remove("CSOCLASSIFY_SYNAP_MODE")
            // 시스템 임시 폴더를 이 작업장으로 — 사이냅 출력파일이 남지 않았는지 보려고.
            .env("TMP", &tmp).env("TEMP", &tmp).env("TMPDIR", &tmp);
        match mode {
            Some(m) => { c.env("FAKE_SNF_MODE", m); }
            None => { c.env_remove("FAKE_SNF_MODE"); }
        }
        c.output().unwrap()
    }

    //--------------------------------------------------------------
    // 가짜 snf 가 몇 번 불렸나
    //
    // -in: 없음
    //
    // -out: usize = 불린 횟수(기록이 없으면 0)
    // -out: error = 없음
    //--------------------------------------------------------------
    fn snf_calls(&self) -> usize {
        std::fs::read_to_string(self.dir.join("snf_calls.log")).map(|s| s.lines().count()).unwrap_or(0)
    }

    //--------------------------------------------------------------
    // 시스템 임시 폴더에 사이냅 출력파일(csoclassify_*.txt)이 남았나
    //
    // -in: 없음
    //
    // -out: 없음(남았으면 panic)
    // -out: error = 남았으면 AssertionError
    //--------------------------------------------------------------
    fn assert_no_leftover(&self) {
        let left: Vec<String> = std::fs::read_dir(self.dir.join("tmp")).unwrap()
            .filter_map(|e| e.ok()).map(|e| e.file_name().to_string_lossy().into_owned())
            .filter(|n| n.starts_with("csoclassify_")).collect();
        assert!(left.is_empty(), "사이냅 출력파일이 남았다: {:?}", left);
    }
}

impl Drop for Env {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

const BODY: &str = "월간 운영 보고서입니다. 정기 점검을 예정대로 수행했고 특이사항은 없습니다.\n";
const MARK: &str = "사이냅이 읽은 본문입니다";

//------------------------------------------------------------------
// 자체 파서가 없는 포맷의 파일(이진 덩어리) 만들기
//------------------------------------------------------------------
fn binary_blob() -> Vec<u8> {
    (0..4096u32).map(|i| ((i * 37 + 11) % 256) as u8).collect()
}

fn stdout(o: &Output) -> String { String::from_utf8_lossy(&o.stdout).into_owned() }
fn stderr(o: &Output) -> String { String::from_utf8_lossy(&o.stderr).into_owned() }

#[test]
fn 기본은_사이냅이_있어도_쓰지_않는다() {
    let env = Env::new("default");
    let doc = env.store("보고서.txt", BODY.as_bytes());
    let o = env.run(&["--text-only", "--file", &doc], None);
    assert_eq!(o.status.code(), Some(0), "{}", stderr(&o));
    assert!(stdout(&o).contains("월간 운영 보고서"), "{}", stdout(&o));
    assert_eq!(env.snf_calls(), 0, "기본 모드에서 사이냅을 불렀다");
    // 자체 파서가 못 읽는 파일도 기본 모드에서는 사이냅으로 가지 않는다(지금과 같다).
    let bin = env.store("덩어리.bin", &binary_blob());
    let o = env.run(&["--text-only", "--file", &bin], None);
    assert_eq!(o.status.code(), Some(1), "{}", stderr(&o));
    assert_eq!(env.snf_calls(), 0);
}

#[test]
fn synap_only_는_모든_문서를_사이냅으로() {
    let env = Env::new("only");
    let doc = env.store("보고서.txt", BODY.as_bytes());
    let o = env.run(&["--text-only", "--file", &doc, "--synap-only"], None);
    assert_eq!(o.status.code(), Some(0), "{}", stderr(&o));
    assert!(stdout(&o).contains(MARK), "{}", stdout(&o));
    assert!(!stdout(&o).contains("월간 운영 보고서"), "자체 파서 결과가 섞였다");
    assert_eq!(env.snf_calls(), 1);
    env.assert_no_leftover();
}

#[test]
fn synap_fallback_은_자체_파서가_읽으면_사이냅을_부르지_않는다() {
    let env = Env::new("fb_ok");
    let doc = env.store("보고서.txt", BODY.as_bytes());
    let o = env.run(&["--text-only", "--file", &doc, "--synap-fallback"], None);
    assert_eq!(o.status.code(), Some(0), "{}", stderr(&o));
    assert!(stdout(&o).contains("월간 운영 보고서"));
    assert_eq!(env.snf_calls(), 0);
}

#[test]
fn synap_fallback_은_못_읽거나_짧으면_사이냅으로() {
    let env = Env::new("fb_fall");
    // ① 자체 파서가 없는 포맷 → 곧바로 사이냅
    let bin = env.store("덩어리.bin", &binary_blob());
    let o = env.run(&["--text-only", "--file", &bin, "--synap-fallback"], None);
    assert_eq!(o.status.code(), Some(0), "{}", stderr(&o));
    assert!(stdout(&o).contains(MARK));
    // ② 자체 파서 결과가 짧다(20자 미만) → 사이냅 결과 채택
    let short = env.store("짧은.txt", "가나다".as_bytes());
    let o = env.run(&["--text-only", "--file", &short, "--synap-fallback"], None);
    assert_eq!(o.status.code(), Some(0), "{}", stderr(&o));
    assert!(stdout(&o).contains(MARK), "{}", stdout(&o));
    // ③ 둘 다 짧으면 더 긴 쪽(자체 파서 '가나다라마바사' 21바이트 > 사이냅 '짧음' 6바이트)
    let short2 = env.store("짧은2.txt", "가나다라마바사".as_bytes());
    let o = env.run(&["--text-only", "--file", &short2, "--synap-fallback"], Some("short"));
    assert_eq!(o.status.code(), Some(0), "{}", stderr(&o));
    assert!(stdout(&o).contains("가나다라마바사"), "{}", stdout(&o));
    assert_eq!(env.snf_calls(), 3);
    env.assert_no_leftover();
}

#[test]
fn 사이냅이_실패하면_그_문서만_실패로_사유를_남긴다() {
    let env = Env::new("fail");
    let doc = env.store("보고서.txt", BODY.as_bytes());
    for (mode, want) in [("none", "rc=0; 출력파일 미생성"), ("empty", "rc=0; 출력파일 0바이트")] {
        let o = env.run(&["--text-only", "--file", &doc, "--synap-only"], Some(mode));
        assert_eq!(o.status.code(), Some(1), "{}", stderr(&o));
        assert!(stderr(&o).contains(want), "{} :: {}", mode, stderr(&o));
    }
    // 'none' 은 가짜 snf 가 stdout 에 남긴 말까지 사유에 붙는다(파이썬과 같은 모양).
    let o = env.run(&["--text-only", "--file", &doc, "--synap-only"], Some("none"));
    assert!(stderr(&o).contains("stdout=fake snf"), "{}", stderr(&o));
    env.assert_no_leftover();
}

#[test]
fn 사이냅_제한_시간을_넘으면_끊고_출력파일을_지운다() {
    let env = Env::new("timeout");
    let doc = env.store("보고서.txt", BODY.as_bytes());
    let t = std::time::Instant::now();
    let mut c = Command::new(env!("CARGO_BIN_EXE_MpowerClassify-rs"));
    let tmp = env.dir.join("tmp");
    let o = c.args(["--text-only", "--file", &doc, "--synap-only"])
        .env("CSOCLASSIFY_SYNAP_DIR", env.dir.join("synap"))
        .env("CSOCLASSIFY_SNF_TIMEOUT", "2")
        .env("FAKE_SNF_MODE", "sleep")
        .env("CSOCLASSIFY_LOGDIR", env.dir.join("log"))
        .env("TMP", &tmp).env("TEMP", &tmp).env("TMPDIR", &tmp)
        .output().unwrap();
    assert!(t.elapsed().as_secs() < 20, "제한 시간에 끊지 않았다");
    assert_eq!(o.status.code(), Some(1), "{}", stderr(&o));
    assert!(stderr(&o).contains("snf_exe 타임아웃(2s)"), "{}", stderr(&o));
    env.assert_no_leftover();
}

#[test]
fn 사이냅이_없거나_두_옵션을_함께_주면_읽기_전에_멈춘다() {
    let env = Env::new("missing");
    let doc = env.store("보고서.txt", BODY.as_bytes());
    let empty = env.dir.join("no_synap");
    std::fs::create_dir_all(&empty).unwrap();
    for flag in ["--synap-only", "--synap-fallback"] {
        let o = Command::new(env!("CARGO_BIN_EXE_MpowerClassify-rs"))
            .args(["--text-only", "--file", &doc, flag, "--json-errors"])
            .env("CSOCLASSIFY_SYNAP_DIR", &empty)
            .env("CSOCLASSIFY_LOGDIR", env.dir.join("log"))
            .output().unwrap();
        assert_eq!(o.status.code(), Some(3), "{} :: {}", flag, stderr(&o));
        let out = stdout(&o);
        assert!(out.contains("synap_missing") && out.contains("1016"), "{} :: {}", flag, out);
        assert!(!out.contains("월간 운영 보고서"), "문서를 읽었다");
    }
    // --synap-path 가 없는 파일을 가리켜도 같다.
    let bad = env.dir.join("없는").join("snf_exe");
    let o = env.run(&["--text-only", "--file", &doc, "--synap-only",
                      "--synap-path", bad.to_str().unwrap(), "--json-errors"], None);
    assert_eq!(o.status.code(), Some(3));
    assert!(stdout(&o).contains("synap_missing"), "{}", stdout(&o));
    // 두 옵션을 함께 주면 1006 mode_conflict.
    let o = env.run(&["--text-only", "--file", &doc, "--synap-only", "--synap-fallback", "--json-errors"], None);
    assert_eq!(o.status.code(), Some(3));
    assert!(stdout(&o).contains("mode_conflict"), "{}", stdout(&o));
}

#[test]
fn 환경변수로_모드를_고르고_인자가_이긴다() {
    let env = Env::new("envmode");
    let doc = env.store("보고서.txt", BODY.as_bytes());
    let tmp = env.dir.join("tmp");
    let run = |extra: &[&str], mode_env: &str| {
        Command::new(env!("CARGO_BIN_EXE_MpowerClassify-rs"))
            .args(["--text-only", "--file", &doc]).args(extra)
            .env("CSOCLASSIFY_SYNAP_DIR", env.dir.join("synap"))
            .env("CSOCLASSIFY_SYNAP_MODE", mode_env)
            .env("CSOCLASSIFY_LOGDIR", env.dir.join("log"))
            .env("TMP", &tmp).env("TEMP", &tmp).env("TMPDIR", &tmp)
            .output().unwrap()
    };
    // 환경변수 only → 사이냅
    assert!(stdout(&run(&[], "only")).contains(MARK));
    // 환경변수 only 여도 인자 --synap-fallback 이 이긴다 → 자체 파서가 읽는다
    assert!(stdout(&run(&["--synap-fallback"], "only")).contains("월간 운영 보고서"));
    // 틀린 값 → 1003 bad_args
    assert_eq!(run(&[], "sometimes").status.code(), Some(3));
}

#[test]
fn 복호화_문서는_사이냅_출력파일도_복호화_폴더_안에_둔다() {
    // 가짜 복호화 도구(fake_removeenc)도 짓는다 — 바이트 XOR 0x5A, 열쇠 GOODKEY 로 푼다.
    let snf = fake_snf();
    let dec_tool = snf.with_file_name(format!("fake_removeenc{}", std::env::consts::EXE_SUFFIX));
    if !dec_tool.is_file() {
        let cargo = std::env::var("CARGO").unwrap_or_else(|_| "cargo".into());
        let mut c = Command::new(cargo);
        c.args(["build", "--example", "fake_removeenc"]).current_dir(env!("CARGO_MANIFEST_DIR"));
        if snf.parent().and_then(|p| p.parent()).and_then(|p| p.file_name()).map_or(false, |n| n == "release") {
            c.arg("--release");
        }
        assert!(c.output().unwrap().status.success());
    }
    let env = Env::new("decrypt");
    let enc: Vec<u8> = BODY.as_bytes().iter().map(|b| b ^ 0x5A).collect();
    let doc = env.store("F_0001.SMF", &enc);
    let dec_tmp = env.dir.join("dec");
    std::fs::create_dir_all(&dec_tmp).unwrap();
    let o = env.run(&["--text-only", "--file", &doc, "--decrypt", "--mrk", "GOODKEY",
                      "--enc-mode", "3", "--zip-mode", "0", "--name", "보고서.txt",
                      "--decrypt-cmd", dec_tool.to_str().unwrap(),
                      "--decrypt-tmp", dec_tmp.to_str().unwrap(), "--synap-only"], None);
    assert_eq!(o.status.code(), Some(0), "{}", stderr(&o));
    assert!(stdout(&o).contains(MARK), "{}", stdout(&o));
    // 가짜 snf 가 받은 출력 경로가 복호화 임시 폴더(cso_dec_<pid>) 안이어야 한다.
    let log = std::fs::read_to_string(env.dir.join("snf_calls.log")).unwrap();
    let out_path = log.lines().next().unwrap().split('|').nth(1).unwrap().to_string();
    assert!(out_path.contains("cso_dec_"), "사이냅 출력파일이 복호화 폴더 밖에 생겼다: {}", out_path);
    // 시스템 임시 폴더에도, 복호화 폴더에도 남지 않는다.
    env.assert_no_leftover();
    let left = std::fs::read_dir(&dec_tmp).unwrap().count();
    assert_eq!(left, 0, "복호화 임시 폴더가 남았다");
}

#[test]
fn 분류_결과의_사유에_엔진별_까닭이_실린다() {
    let env = Env::new("record");
    let bin = env.store("덩어리.bin", &binary_blob());
    let rules = Path::new(env!("CARGO_MANIFEST_DIR")).join("..").join("resources").join("policy").join("cso_rule.yaml");
    let o = env.run(&["--file", &bin, "--synap-fallback", "--cso-rules", rules.to_str().unwrap(),
                      "--rule-only", "--format", "jsonl", "--nosummary", "--no-timing"], Some("none"));
    let rec: serde_json::Value = stdout(&o).lines().find(|l| l.contains("\"file\""))
        .map(|l| serde_json::from_str(l).unwrap()).expect("레코드가 없다");
    assert_eq!(rec["error"]["kind"], "extract_failed", "{}", rec);
    let why = rec["error"]["reason"].as_str().unwrap_or("");
    assert!(why.starts_with("텍스트를 추출하지 못했습니다 — 하이브리드 전엔진 실패"), "{}", why);
    assert!(why.contains("snf:rc=0; 출력파일 미생성"), "{}", why);
}
