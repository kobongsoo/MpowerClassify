//! 엠파워 v11 암호화 문서 복호화 연동 — 실제 실행 파일을 불러 계약을 본다.
//!
//! 설계: plan/엠파워v11-암호화문서-복호화연동-설계-20261012.html §11
//!
//! 개발 PC 에는 진짜 removeenc.sh 가 없으므로 `--decrypt-cmd` 에 가짜 도구
//! (examples/fake_removeenc.rs)를 끼운다. 가짜 도구는 진짜의 성질 — 틀린 열쇠에도 0 을
//! 돌려주며 같은 크기의 쓰레기를 낸다 · 종료코드를 stdout 에도 찍는다 — 을 흉내 낸다.
//!
//! 여기서 지키는 것:
//!   · 풀어서 분류한 결과의 file 은 목록의 path 그대로, hash 는 '풀린 내용'의 지문
//!   · 풀지 못했거나 풀린 결과가 수상하면 분류하지 않는다(grade=null + error.kind=decrypt_failed)
//!   · 파일 문제는 그 문서만 실패, 환경 문제는 전체 중단
//!   · 끝나면 임시 폴더가 비어 있다 · 열쇠와 임시 경로는 결과·로그 어디에도 없다

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::time::{Duration, Instant};

use serde_json::Value;
use sha2::{Digest, Sha256};

/// 가짜 도구가 아는 '맞는 열쇠'.
const GOOD: &str = "GOODKEY";

fn sha256(data: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(data);
    h.finalize().iter().map(|b| format!("{:02x}", b)).collect()
}

/// 가짜 도구의 "암호화" — 바이트마다 0x5A 를 XOR 한다(examples/fake_removeenc.rs 와 짝).
fn encrypt(plain: &[u8]) -> Vec<u8> {
    plain.iter().map(|b| b ^ 0x5A).collect()
}

/// cargo test 가 함께 지어 주는 가짜 도구의 경로(target/<profile>/examples/).
fn fake_tool() -> PathBuf {
    let exe = std::env::current_exe().unwrap();
    // …/target/debug/deps/cli_decrypt-xxxx(.exe) → …/target/debug/examples/fake_removeenc(.exe)
    let profile_dir = exe.parent().unwrap().parent().unwrap();
    let p = profile_dir.join("examples")
        .join(format!("fake_removeenc{}", std::env::consts::EXE_SUFFIX));
    // `cargo test --test cli_decrypt` 처럼 이 시험만 고르면 examples 가 지어지지 않는다.
    // 그래서 여기서 한 번 직접 짓는다(이미 최신이면 금방 끝난다). 이 시점에는 cargo 의
    // 빌드가 끝나 잠금이 풀려 있어 안에서 다시 불러도 된다.
    static BUILD: std::sync::Once = std::sync::Once::new();
    BUILD.call_once(|| {
        let cargo = std::env::var("CARGO").unwrap_or_else(|_| "cargo".into());
        let mut c = Command::new(cargo);
        c.args(["build", "--example", "fake_removeenc"])
            .current_dir(env!("CARGO_MANIFEST_DIR"));
        if profile_dir.file_name().map_or(false, |n| n == "release") {
            c.arg("--release");
        }
        let o = c.output().expect("cargo 를 부르지 못했다");
        assert!(o.status.success(), "가짜 도구를 짓지 못했다: {}", String::from_utf8_lossy(&o.stderr));
    });
    assert!(p.is_file(), "가짜 도구가 없다: {}", p.display());
    p
}

/// 시험 하나의 작업 폴더. 저장소(store) · 임시(tmp) · 로그(log)를 그 안에 둔다.
struct Env {
    dir: PathBuf,
}

impl Env {
    fn new(tag: &str) -> Env {
        let dir = std::env::temp_dir().join(format!("csors_decrypt_{}_{}", tag, std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        // 저장소 폴더 이름에 공백을 넣어 둔다 — 인자가 글자 그대로 넘어가는지 함께 본다.
        std::fs::create_dir_all(dir.join("저장 소")).unwrap();
        std::fs::create_dir_all(dir.join("tmp")).unwrap();
        Env { dir }
    }

    fn tmp(&self) -> PathBuf { self.dir.join("tmp") }

    /// 저장소에 파일을 하나 쓴다. 저장 이름은 엠파워처럼 뜻 없는 이름이다.
    fn store(&self, rname: &str, bytes: &[u8]) -> String {
        let p = self.dir.join("저장 소").join(rname);
        std::fs::write(&p, bytes).unwrap();
        p.to_string_lossy().replace('\\', "/")
    }

    /// 목록 파일을 쓴다(한 줄에 JSON 하나).
    fn filelist(&self, rows: &[Value]) -> String {
        let p = self.dir.join("filelist.jsonl");
        let mut f = std::fs::File::create(&p).unwrap();
        for r in rows {
            writeln!(f, "{}", r).unwrap();
        }
        p.to_string_lossy().into_owned()
    }

    /// 실행한다. 규칙만 쓰고(--rule-only) 결과는 jsonl 파일로 받는다.
    fn run(&self, args: &[&str]) -> (Output, Vec<Value>) {
        let out = self.dir.join("result.jsonl");
        let _ = std::fs::remove_file(&out);
        let rules = Path::new(env!("CARGO_MANIFEST_DIR")).join("resources").join("policy")
            .join("cso_rule.yaml");
        let tool = fake_tool();
        let tmp = self.tmp();
        let mut a: Vec<&str> = args.to_vec();
        a.extend(["--cso-rules", rules.to_str().unwrap(), "--rule-only", "--json-errors",
                  "--format", "jsonl", "--out", out.to_str().unwrap(),
                  "--decrypt-cmd", tool.to_str().unwrap(),
                  "--decrypt-tmp", tmp.to_str().unwrap()]);
        let o = Command::new(env!("CARGO_BIN_EXE_MpowerClassify-rs"))
            .args(&a)
            .env("CSOCLASSIFY_POLICY_DIR", self.dir.join("no_policy"))
            .env("CSOCLASSIFY_LOGDIR", self.dir.join("log"))
            .env_remove("CSOCLASSIFY_MRK")
            .env_remove("CSOCLASSIFY_DECRYPT_CMD")
            .env_remove("CSOCLASSIFY_DECRYPT_TMP")
            .output()
            .expect("실행 파일을 부르지 못했다");
        let recs: Vec<Value> = std::fs::read_to_string(&out).unwrap_or_default().lines()
            .filter_map(|l| serde_json::from_str::<Value>(l).ok())
            .filter(|r| r.get("file").is_some())
            .collect();
        (o, recs)
    }

    /// 임시 폴더에 풀린 파일이 남아 있지 않은가.
    fn assert_tmp_clean(&self) {
        let left: Vec<String> = std::fs::read_dir(self.tmp()).unwrap().flatten()
            .map(|e| e.file_name().to_string_lossy().into_owned()).collect();
        assert!(left.is_empty(), "임시 폴더에 남은 것이 있다: {:?}", left);
    }

    /// 로그 폴더의 모든 글자(열쇠·임시 경로가 새지 않았는지 볼 때 쓴다).
    fn all_logs(&self) -> String {
        let mut s = String::new();
        if let Ok(rd) = std::fs::read_dir(self.dir.join("log")) {
            for e in rd.flatten() {
                s.push_str(&String::from_utf8_lossy(&std::fs::read(e.path()).unwrap_or_default()));
            }
        }
        s
    }

    fn result_text(&self) -> String {
        std::fs::read_to_string(self.dir.join("result.jsonl")).unwrap_or_default()
    }
}

impl Drop for Env {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

/// stdout 마지막 JSON 줄(상태 줄)에서 (kind, code)를 꺼낸다.
fn status(o: &Output) -> (String, i64) {
    let text = String::from_utf8_lossy(&o.stdout);
    let line = text.lines().filter(|l| l.trim_start().starts_with('{')).last()
        .unwrap_or_else(|| panic!("상태 줄이 없다. stdout={:?} stderr={}", text,
                                  String::from_utf8_lossy(&o.stderr)));
    let v: Value = serde_json::from_str(line).expect("상태 줄이 JSON 이 아니다");
    (v["error"]["kind"].as_str().unwrap_or("").to_string(), v["error"]["code"].as_i64().unwrap_or(-1))
}

fn rec_of<'a>(recs: &'a [Value], file: &str) -> &'a Value {
    recs.iter().find(|r| r["file"].as_str() == Some(file))
        .unwrap_or_else(|| panic!("결과에 {} 가 없다: {:?}", file, recs))
}

const BODY_A: &str = "분류 시험용 문서입니다. 이 문서는 엠파워 v11 복호화 연동을 시험하려고 만든 더미 본문입니다.\n";
const BODY_B: &str = "두 번째 시험 문서입니다. 내용은 아무 뜻이 없고 복호화 뒤의 지문을 견주는 데만 씁니다.\n";

#[test]
fn 목록의_열쇠로_풀어서_분류하고_임시파일을_지운다() {
    let env = Env::new("ok");
    let a = env.store("F_aaaa.SMF", &encrypt(BODY_A.as_bytes()));
    // 가짜 도구가 stdout 에 잡다한 글자를 잔뜩 찍는 건 — 결과가 깨지지 않아야 한다.
    let b = env.store("F_bbbb.SMF", &encrypt(BODY_B.as_bytes()));
    // 둘 다 0 — 암호화 안 된 채 저장된 파일. 도구를 부르지 않는다(열쇠도 없다).
    let c = env.store("F_cccc.SMF", BODY_A.as_bytes());
    // 열쇠 칸이 아예 없는 줄 — 지금까지처럼 그대로 읽는다.
    let d = env.store("plain.txt", BODY_B.as_bytes());
    let list = env.filelist(&[
        serde_json::json!({"path": a, "sfile_id": "SF_A", "mrk": GOOD, "enc_mode": 3, "zip_mode": 0,
                           "name": "시험 문서 (가).txt", "size": BODY_A.len()}),
        serde_json::json!({"path": b, "sfile_id": "SF_B", "mrk": "NOISE", "enc_mode": "3", "zip_mode": "0",
                           "name": "시험문서_나.txt", "size": BODY_B.len()}),
        serde_json::json!({"path": c, "sfile_id": "SF_C", "enc_mode": 0, "zip_mode": 0,
                           "name": "평문저장.txt"}),
        serde_json::json!({"path": d, "sfile_id": "SF_D"}),
    ]);
    let (o, recs) = env.run(&["--filelist", &list]);
    assert_eq!(o.status.code(), Some(0), "stderr={}", String::from_utf8_lossy(&o.stderr));
    assert_eq!(recs.len(), 4, "{:?}", recs);

    let ra = rec_of(&recs, &a);
    assert!(ra.get("error").is_none(), "{}", ra);
    // 지문은 암호문이 아니라 '풀린 내용'의 것이어야 한다.
    assert_eq!(ra["hash"].as_str().unwrap(), sha256(BODY_A.as_bytes()));
    assert_eq!(ra["doc_id"], "SF_A");
    assert_eq!(ra["name"], "시험 문서 (가).txt");
    assert_eq!(ra["meta"]["decrypted"], true);

    let rb = rec_of(&recs, &b);
    assert_eq!(rb["hash"].as_str().unwrap(), sha256(BODY_B.as_bytes()));

    // 둘 다 0 인 줄은 도구를 부르지 않았다 — '풀었다'는 표식이 없다.
    let rc = rec_of(&recs, &c);
    assert!(rc.get("error").is_none(), "{}", rc);
    assert_eq!(rc["hash"].as_str().unwrap(), sha256(BODY_A.as_bytes()));
    assert!(rc["meta"].get("decrypted").is_none());
    assert_eq!(rc["name"], "평문저장.txt");

    let rd = rec_of(&recs, &d);
    assert!(rd.get("name").is_none());
    assert_eq!(rd["doc_id"], "SF_D");

    env.assert_tmp_clean();
    // stdout 에는 우리 상태 줄만 있어야 한다 — 가짜 도구의 출력이 섞이면 안 된다.
    let stdout = String::from_utf8_lossy(&o.stdout);
    assert!(!stdout.contains("noise"), "도구의 출력이 stdout 에 섞였다");
    assert_eq!(status(&o).0, "success");
    // 열쇠는 결과·로그 어디에도 없다. 임시 경로는 '복호화 성공' 줄에만 일부러 남긴다
    // (2026-10-15 요청) — 그 밖의 줄에는 없어야 한다.
    let mut ok_lines: Vec<String> = vec![];
    for (what, text) in [("결과", env.result_text()), ("로그", env.all_logs())] {
        assert!(!text.contains(GOOD), "{}에 열쇠가 남았다", what);
        ok_lines.extend(text.lines().filter(|l| l.contains("복호화 성공")).map(String::from));
        let others: Vec<&str> = text.lines().filter(|l| !l.contains("복호화 성공")).collect();
        assert!(!others.join("\n").contains("cso_dec_"), "{}에 임시 경로가 남았다", what);
    }
    // 성공 줄은 목록의 세 문서(a·b·c)마다 하나씩 — 모드와 임시 경로, 도구 사용 여부가 실린다.
    assert_eq!(ok_lines.len(), 3, "{:?}", ok_lines);
    let la = ok_lines.iter().find(|l| l.contains("F_aaaa.SMF")).expect("a 의 성공 줄");
    assert!(la.contains("enc_mode=3") && la.contains("zip_mode=0") && la.contains("tool=yes"), "{}", la);
    assert!(la.contains("tmp=") && la.contains("cso_dec_"), "{}", la);
    // 가짜 도구의 암호는 바이트 XOR 이라 풀린 크기와 저장 크기가 본문 길이와 같다.
    let want = format!("stored_size={0} dec_size={0}", BODY_A.len());
    assert!(la.contains(&want), "{}", la);
    // a 는 목록에 size 가 있다 — DB 원본 크기(org_size)가 함께 실린다.
    assert!(la.contains(&format!("org_size={} stored_size=", BODY_A.len())), "{}", la);
    let lc = ok_lines.iter().find(|l| l.contains("F_cccc.SMF")).expect("c 의 성공 줄");
    assert!(lc.contains("tool=no(copy)") && lc.contains("enc_mode=0"), "{}", lc);
    assert!(lc.contains(&want), "{}", lc);
    // c 는 목록에 size 가 없다 — org_size 칸 자체가 없어야 한다.
    assert!(!lc.contains("org_size="), "{}", lc);
}

#[test]
fn 틀린_열쇠로_풀린_결과는_분류하지_않는다() {
    let env = Env::new("wrongkey");
    let pdf = b"%PDF-1.4 dummy body for signature check, not a real pdf\n";
    // 도구는 틀린 열쇠에도 0 을 돌려주고 같은 크기의 쓰레기를 낸다.
    let a = env.store("F_pdf.SMF", &encrypt(pdf));
    let b = env.store("F_txt.SMF", &encrypt(BODY_A.as_bytes()));
    let list = env.filelist(&[
        serde_json::json!({"path": a, "sfile_id": "SF_PDF", "mrk": "WRONGKEY", "enc_mode": 3, "zip_mode": 0,
                           "name": "보고서.pdf", "size": pdf.len()}),
        serde_json::json!({"path": b, "sfile_id": "SF_TXT", "mrk": GOOD, "enc_mode": 3, "zip_mode": 0,
                           "name": "메모.txt"}),
    ]);
    let (o, recs) = env.run(&["--filelist", &list]);
    // 일부 실패 — 종료코드 1, 까닭은 decrypt_failed(3005).
    assert_eq!(o.status.code(), Some(1));
    assert_eq!(status(&o), ("decrypt_failed".to_string(), 3005));

    let ra = rec_of(&recs, &a);
    assert!(ra["grade"].is_null(), "등급이 나오면 안 된다: {}", ra);
    assert_eq!(ra["error"]["kind"], "decrypt_failed");
    assert_eq!(ra["error"]["detail"], "signature_mismatch");
    assert_eq!(ra["error"]["rc"], 0);
    assert_eq!(ra["doc_id"], "SF_PDF");
    // 암호문의 지문은 싣지 않는다.
    assert!(ra.get("hash").map_or(true, |h| h.is_null()), "{}", ra);

    // 다음 문서는 그대로 처리된다.
    let rb = rec_of(&recs, &b);
    assert!(rb.get("error").is_none(), "{}", rb);
    assert_eq!(rb["hash"].as_str().unwrap(), sha256(BODY_A.as_bytes()));
    env.assert_tmp_clean();
    // 실패 줄도 성공 줄과 같은 칸을 싣는다 — 틀린 열쇠라 크기는 셋 다 같고 사유만 다르다.
    let n = pdf.len();
    let logs = env.all_logs();
    let fail = logs.lines().find(|l| l.contains("복호화 실패") && l.contains("F_pdf.SMF"))
        .expect("실패 줄이 로그에 없다");
    let want = format!("name=보고서.pdf reason=signature_mismatch rc=0 enc_mode=3 zip_mode=0 \
                        org_size={0} stored_size={0} dec_size={0} elapsed=", n);
    assert!(fail.contains(&want), "{}", fail);
    // 실패한 결과물은 지웠으므로 임시 경로를 싣지 않는다.
    assert!(!fail.contains("cso_dec_"), "{}", fail);
}

#[test]
fn 열쇠가_일부만_있거나_감싼_값이면_도구를_부르지_않고_오류로_남긴다() {
    let env = Env::new("incomplete");
    let a = env.store("F_1.SMF", &encrypt(BODY_A.as_bytes()));
    let b = env.store("F_2.SMF", &encrypt(BODY_A.as_bytes()));
    let c = env.store("F_3.SMF", &encrypt(BODY_A.as_bytes()));
    let list = env.filelist(&[
        // zip_mode 가 빠졌다 — '그대로 읽기'로 넘어가면 암호문을 분류한다.
        serde_json::json!({"path": a, "sfile_id": "S1", "mrk": GOOD, "enc_mode": 3, "name": "a.txt"}),
        // KEK 로 감싼 열쇠.
        serde_json::json!({"path": b, "sfile_id": "S2", "mrk": "v2:AAAABBBB", "enc_mode": 3, "zip_mode": 0}),
        // 암호화된 파일인데 열쇠가 없다.
        serde_json::json!({"path": c, "sfile_id": "S3", "enc_mode": 3, "zip_mode": 0}),
    ]);
    let (o, recs) = env.run(&["--filelist", &list]);
    assert_eq!(o.status.code(), Some(1));
    for (file, detail) in [(&a, "info_incomplete"), (&b, "key_wrapped"), (&c, "info_incomplete")] {
        let r = rec_of(&recs, file);
        assert_eq!(r["error"]["kind"], "decrypt_failed", "{}", r);
        assert_eq!(r["error"]["detail"], detail, "{}", r);
        assert!(r["error"]["rc"].is_null(), "도구를 부르지 않았어야 한다: {}", r);
        assert!(r["grade"].is_null());
    }
    env.assert_tmp_clean();
}

#[test]
fn 도구가_실패하면_그_문서만_실패로_남기고_계속한다() {
    let env = Env::new("rc");
    let a = env.store("F_1.SMF", &encrypt(BODY_A.as_bytes()));
    let b = env.store("F_2.SMF", &encrypt(BODY_A.as_bytes()));
    let c = env.store("F_3.SMF", &encrypt(BODY_B.as_bytes()));
    let list = env.filelist(&[
        serde_json::json!({"path": a, "sfile_id": "S1", "mrk": "RC9", "enc_mode": 3, "zip_mode": 0, "name": "a.txt"}),
        serde_json::json!({"path": b, "sfile_id": "S2", "mrk": "EMPTY", "enc_mode": 3, "zip_mode": 0, "name": "b.txt"}),
        serde_json::json!({"path": c, "sfile_id": "S3", "mrk": GOOD, "enc_mode": 3, "zip_mode": 0, "name": "c.txt"}),
    ]);
    let (o, recs) = env.run(&["--filelist", &list]);
    assert_eq!(o.status.code(), Some(1));
    let ra = rec_of(&recs, &a);
    assert_eq!(ra["error"]["detail"], "removeenc_exit");
    assert_eq!(ra["error"]["rc"], 9);
    assert_eq!(rec_of(&recs, &b)["error"]["detail"], "empty_output");
    let rc = rec_of(&recs, &c);
    assert!(rc.get("error").is_none(), "{}", rc);
    assert_eq!(rc["hash"].as_str().unwrap(), sha256(BODY_B.as_bytes()));
    env.assert_tmp_clean();
}

#[test]
fn 환경_문제면_전체를_멈춘다() {
    let env = Env::new("env");
    let a = env.store("F_1.SMF", &encrypt(BODY_A.as_bytes()));
    let b = env.store("F_2.SMF", &encrypt(BODY_A.as_bytes()));
    let list = env.filelist(&[
        // 56 = PHP 확장을 못 읽었다. 이 서버에서는 어떤 파일도 풀 수 없다.
        serde_json::json!({"path": a, "sfile_id": "S1", "mrk": "RC56", "enc_mode": 3, "zip_mode": 0}),
        serde_json::json!({"path": b, "sfile_id": "S2", "mrk": "RC56", "enc_mode": 3, "zip_mode": 0}),
    ]);
    let (o, _) = env.run(&["--filelist", &list]);
    assert_eq!(o.status.code(), Some(2));
    assert_eq!(status(&o), ("decrypt_unavailable".to_string(), 3006));
    env.assert_tmp_clean();
}

#[test]
fn 도구가_없으면_한_건도_읽지_않고_끝낸다() {
    let env = Env::new("nocmd");
    let a = env.store("F_1.SMF", &encrypt(BODY_A.as_bytes()));
    let list = env.filelist(&[
        serde_json::json!({"path": a, "sfile_id": "S1", "mrk": GOOD, "enc_mode": 3, "zip_mode": 0}),
    ]);
    // run() 은 가짜 도구를 --decrypt-cmd 로 붙이므로, 여기서는 직접 불러 없는 경로를 준다.
    let missing = env.dir.join("no_such_removeenc.sh");
    let rules = Path::new(env!("CARGO_MANIFEST_DIR")).join("resources").join("policy").join("cso_rule.yaml");
    let o = Command::new(env!("CARGO_BIN_EXE_MpowerClassify-rs"))
        .args(["--filelist", &list, "--cso-rules", rules.to_str().unwrap(), "--rule-only",
               "--json-errors", "--decrypt-cmd", missing.to_str().unwrap(),
               "--decrypt-tmp", env.tmp().to_str().unwrap()])
        .env("CSOCLASSIFY_POLICY_DIR", env.dir.join("no_policy"))
        .env("CSOCLASSIFY_LOGDIR", env.dir.join("log"))
        .output().unwrap();
    assert_eq!(o.status.code(), Some(3));
    assert_eq!(status(&o), ("decrypt_cmd_missing".to_string(), 1015));
    env.assert_tmp_clean();
}

#[test]
fn 시간_제한을_넘으면_끊고_다음으로_간다() {
    let env = Env::new("timeout");
    let a = env.store("F_1.SMF", &encrypt(BODY_A.as_bytes()));
    let b = env.store("F_2.SMF", &encrypt(BODY_B.as_bytes()));
    let c = env.store("F_3.SMF", &encrypt(BODY_A.as_bytes()));
    let list = env.filelist(&[
        serde_json::json!({"path": a, "sfile_id": "S1", "mrk": "SLEEP", "enc_mode": 3, "zip_mode": 0, "name": "a.txt"}),
        serde_json::json!({"path": b, "sfile_id": "S2", "mrk": GOOD, "enc_mode": 3, "zip_mode": 0, "name": "b.txt"}),
        // 도구가 자식을 띄우고 그 자식이 자는 경우 — 진짜 removeenc.sh 가 이 구조다(bash → php).
        // 부모만 죽이면 자식이 출력 파이프를 붙잡고 남아, 부른 쪽이 30초를 다 기다린다.
        serde_json::json!({"path": c, "sfile_id": "S3", "mrk": "SLEEPCHILD", "enc_mode": 3, "zip_mode": 0, "name": "c.txt"}),
    ]);
    let t0 = Instant::now();
    let (o, recs) = env.run(&["--filelist", &list, "--decrypt-timeout", "1"]);
    // 가짜 도구는 30초를 잔다 — 제한(1초)에서 끊겼다면 훨씬 일찍 끝난다.
    assert!(t0.elapsed() < Duration::from_secs(20), "시간 제한이 듣지 않았다: {:?}", t0.elapsed());
    assert_eq!(o.status.code(), Some(1));
    assert_eq!(rec_of(&recs, &a)["error"]["detail"], "timeout");
    assert_eq!(rec_of(&recs, &c)["error"]["detail"], "timeout");
    assert!(rec_of(&recs, &b).get("error").is_none());
    env.assert_tmp_clean();
}

#[test]
fn 단일_파일은_decrypt_를_줄_때만_푼다() {
    let env = Env::new("single");
    let enc = env.store("F_single.SMF", &encrypt(BODY_A.as_bytes()));
    // 이름에 공백 · 괄호 · 셸이 해석할 만한 글자를 넣는다 — 명령으로 해석될 길이 없어야 한다.
    let name = "보고서 (최종) $(echo x) & 사본.txt";
    let (o, recs) = env.run(&["--file", &enc, "--decrypt", "--mrk", GOOD,
                              "--enc-mode", "3", "--zip-mode", "0", "--name", name]);
    assert_eq!(o.status.code(), Some(0), "stderr={}", String::from_utf8_lossy(&o.stderr));
    let r = rec_of(&recs, &enc);
    assert_eq!(r["hash"].as_str().unwrap(), sha256(BODY_A.as_bytes()));
    assert_eq!(r["name"], name);
    assert_eq!(r["meta"]["decrypted"], true);
    env.assert_tmp_clean();
    // 명령줄로 준 열쇠가 로그에 남지 않는다(실행 명령을 통째로 적는 자리가 있다).
    let logs = env.all_logs();
    assert!(logs.contains("--mrk"), "실행 명령이 로그에 있어야 한다");
    assert!(!logs.contains(GOOD), "로그에 열쇠가 남았다");

    // --decrypt 를 안 주면 지금까지처럼 그대로 읽는다(이미 풀어 둔 파일용).
    let plain = env.store("이미 풀어 둔 문서.txt", BODY_B.as_bytes());
    let (o, recs) = env.run(&["--file", &plain]);
    assert_eq!(o.status.code(), Some(0));
    let r = rec_of(&recs, &plain);
    assert_eq!(r["hash"].as_str().unwrap(), sha256(BODY_B.as_bytes()));
    assert!(r["meta"].get("decrypted").is_none());
    // 우리가 만든 파일이 아니므로 지우지 않는다.
    assert!(Path::new(&plain).is_file());
}

#[test]
fn 단일_파일의_size_는_주면_견주고_안_주면_건너뛴다() {
    let env = Env::new("single_size");
    let enc = env.store("F_size.SMF", &encrypt(BODY_A.as_bytes()));
    let ok = BODY_A.len().to_string();
    let wrong = (BODY_A.len() + 1).to_string();
    let keys = ["--file", enc.as_str(), "--decrypt", "--mrk", GOOD, "--enc-mode", "3", "--zip-mode", "0",
                "--name", "메모.txt"];
    // 맞는 크기 — 그대로 분류된다.
    let (o, recs) = env.run(&[&keys[..], &["--size", ok.as_str()]].concat());
    assert_eq!(o.status.code(), Some(0), "stderr={}", String::from_utf8_lossy(&o.stderr));
    assert!(rec_of(&recs, &enc).get("error").is_none());
    // --size 로 준 값이 성공 줄의 org_size 로 남는다.
    assert!(env.all_logs().contains(&format!("org_size={} stored_size=", ok)), "성공 줄에 org_size 가 없다");
    // 틀린 크기 — 분류하지 않고 size_mismatch 로 남긴다(잘린 결과·덜 풀린 압축을 잡는 자리).
    let (o, recs) = env.run(&[&keys[..], &["--size", wrong.as_str()]].concat());
    assert_eq!(o.status.code(), Some(1), "stderr={}", String::from_utf8_lossy(&o.stderr));
    let x = rec_of(&recs, &enc);
    assert!(x["grade"].is_null(), "{}", x);
    assert_eq!(x["error"]["kind"], "decrypt_failed");
    assert_eq!(x["error"]["detail"], "size_mismatch");
    // 실패 줄에 DB 원본 크기(org_size)와 실제 결과 크기(dec_size)가 나란히 남는다.
    let want = format!("reason=size_mismatch rc=0 enc_mode=3 zip_mode=0 \
                        org_size={} stored_size={} dec_size={} elapsed=", wrong, ok, ok);
    assert!(env.all_logs().contains(&want), "실패 줄 모양이 다르다: {}", want);
    env.assert_tmp_clean();
}

#[test]
fn 인자_조합이_틀리면_시작_전에_끝낸다() {
    let env = Env::new("args");
    let enc = env.store("F_x.SMF", &encrypt(BODY_A.as_bytes()));
    let dir = env.dir.join("저장 소").to_string_lossy().into_owned();
    let cases: Vec<Vec<&str>> = vec![
        // 열쇠만 주고 --decrypt 가 없다 — "풀릴 줄 알았는데 안 풀린" 상태를 만들지 않는다.
        vec!["--file", &enc, "--mrk", GOOD, "--enc-mode", "3", "--zip-mode", "0"],
        // --decrypt 인데 열쇠가 없다.
        vec!["--file", &enc, "--decrypt"],
        // 일부만 줬다.
        vec!["--file", &enc, "--decrypt", "--mrk", GOOD, "--enc-mode", "3"],
        // 폴더에는 파일마다 다른 열쇠를 줄 방법이 없다.
        vec!["--dir", &dir, "--decrypt", "--mrk", GOOD, "--enc-mode", "3", "--zip-mode", "0"],
        // 감싼 열쇠.
        vec!["--file", &enc, "--decrypt", "--mrk", "v2:XXXX", "--enc-mode", "3", "--zip-mode", "0"],
        vec!["--file", &enc, "--decrypt", "--no-decrypt", "--mrk", GOOD, "--enc-mode", "3", "--zip-mode", "0"],
        // --size 만 주고 --decrypt 가 없다.
        vec!["--file", &enc, "--size", "100"],
        // --size 가 정수가 아니다.
        vec!["--file", &enc, "--decrypt", "--mrk", GOOD, "--enc-mode", "3", "--zip-mode", "0", "--size", "abc"],
        vec!["--file", &enc, "--decrypt", "--mrk", GOOD, "--enc-mode", "3", "--zip-mode", "0", "--size", "1.5"],
    ];
    for args in cases {
        let (o, recs) = env.run(&args);
        assert_eq!(o.status.code(), Some(3), "{:?}", args);
        assert_eq!(status(&o), ("decrypt_args_invalid".to_string(), 1014), "{:?}", args);
        assert!(recs.is_empty(), "문서를 읽으면 안 된다: {:?}", args);
    }
}

#[test]
fn no_decrypt_면_목록에_열쇠가_있어도_풀지_않는다() {
    let env = Env::new("nodec");
    // 같은 목록으로 '이미 풀어 둔 사본'을 가리키는 경우 — 파일은 평문이다.
    let a = env.store("copy.txt", BODY_A.as_bytes());
    let list = env.filelist(&[
        serde_json::json!({"path": a, "sfile_id": "S1", "mrk": GOOD, "enc_mode": 3, "zip_mode": 0, "name": "a.txt"}),
    ]);
    let (o, recs) = env.run(&["--filelist", &list, "--no-decrypt"]);
    assert_eq!(o.status.code(), Some(0));
    let r = rec_of(&recs, &a);
    assert_eq!(r["hash"].as_str().unwrap(), sha256(BODY_A.as_bytes()));
    assert!(r["meta"].get("decrypted").is_none());
    env.assert_tmp_clean();
}

/// 내부에 txt 두 개가 든 zip 을 만든다.
fn make_zip() -> Vec<u8> {
    let mut buf = std::io::Cursor::new(Vec::new());
    {
        let mut z = zip::ZipWriter::new(&mut buf);
        let opt = zip::write::FileOptions::default()
            .compression_method(zip::CompressionMethod::Deflated);
        z.start_file("inner_a.txt", opt).unwrap();
        z.write_all(BODY_A.as_bytes()).unwrap();
        z.start_file("sub/inner_b.txt", opt).unwrap();
        z.write_all(BODY_B.as_bytes()).unwrap();
        z.finish().unwrap();
    }
    buf.into_inner()
}

#[test]
fn 풀린_압축파일은_내부_문서로_펼친다() {
    let env = Env::new("zip");
    let zip = make_zip();
    // ① 암호화된 zip ② 암호화 없이(0,0) 저장된 zip — 저장 이름에는 확장자 단서가 없다.
    let a = env.store("F_zip1.SMF", &encrypt(&zip));
    let b = env.store("F_zip2.SMF", &zip);
    let list = env.filelist(&[
        serde_json::json!({"path": a, "sfile_id": "SZ1", "mrk": GOOD, "enc_mode": 3, "zip_mode": 0,
                           "name": "묶음1.zip", "size": zip.len()}),
        serde_json::json!({"path": b, "sfile_id": "SZ2", "enc_mode": 0, "zip_mode": 0,
                           "name": "묶음2.zip", "size": zip.len()}),
    ]);
    let (o, recs) = env.run(&["--filelist", &list]);
    assert_eq!(o.status.code(), Some(0), "stderr={}\n{:?}", String::from_utf8_lossy(&o.stderr), recs);
    for top in [&a, &b] {
        // 내부 문서의 file 은 '목록 경로/내부 경로' — 임시 경로가 아니다.
        let ia = rec_of(&recs, &format!("{}/inner_a.txt", top));
        assert_eq!(ia["hash"].as_str().unwrap(), sha256(BODY_A.as_bytes()));
        let ib = rec_of(&recs, &format!("{}/sub/inner_b.txt", top));
        assert_eq!(ib["hash"].as_str().unwrap(), sha256(BODY_B.as_bytes()));
        // 압축 자체의 집계 레코드도 목록 경로로 나온다.
        let agg = rec_of(&recs, top);
        assert_eq!(agg["archive"], true, "{}", agg);
    }
    assert!(!env.result_text().contains("cso_dec_"), "결과에 임시 경로가 남았다");
    env.assert_tmp_clean();
}

// 복호화 전 크기 상한 — 풀린 뒤 상한과 같게 고른다(plan/엠파워v11-복호화-크기상한-정합-설계-20261016.html §7).
// 파이썬 판 같은 시험이 tests/test_decrypt.py 에 있다.
#[test]
fn 암호화_압축은_압축_상한으로_거른다() {
    let env = Env::new("prelimit_zip");
    let zdata = make_zip();
    let a = env.store("F_bigzip.SMF", &encrypt(&zdata));
    let list = env.filelist(&[serde_json::json!({"path": a, "sfile_id": "SZ", "mrk": GOOD, "enc_mode": 3,
                                                 "zip_mode": 0, "name": "묶음.zip", "size": zdata.len()})]);
    // 일반 상한을 '안의 문서 < 상한 < zip' 로 낮춘다 — 예전에는 zip 이 일반 상한에 걸려 풀지도 않았다.
    let lim = BODY_A.len() + 40;
    assert!(zdata.len() > lim);
    let mb = format!("{:.10}", lim as f64 / 1048576.0);
    let (o, recs) = env.run(&["--filelist", &list, "--max-file-mb", &mb]);
    assert_eq!(o.status.code(), Some(0), "stderr={}", String::from_utf8_lossy(&o.stderr));
    assert_eq!(rec_of(&recs, &format!("{}/inner_a.txt", a))["hash"].as_str().unwrap(), sha256(BODY_A.as_bytes()));
    assert_eq!(rec_of(&recs, &a)["archive"], true);
    env.assert_tmp_clean();
}

#[test]
fn 암호화_텍스트는_텍스트_상한으로_거른다() {
    let env = Env::new("prelimit_txt");
    let t = env.store("F_txt.SMF", &encrypt(BODY_A.as_bytes()));
    let h = env.store("F_hwp.SMF", &encrypt(BODY_A.as_bytes()));
    let list = env.filelist(&[
        serde_json::json!({"path": t, "sfile_id": "ST", "mrk": GOOD, "enc_mode": 3, "zip_mode": 0,
                           "name": "메모.txt", "size": BODY_A.len()}),
        serde_json::json!({"path": h, "sfile_id": "SH", "mrk": GOOD, "enc_mode": 3, "zip_mode": 0,
                           "name": "보고서.hwp", "size": BODY_A.len()}),
    ]);
    // 일반 상한은 아주 작게, 텍스트 상한은 넉넉히 — 텍스트만 통과해야 한다.
    let (_, recs) = env.run(&["--filelist", &list, "--max-file-mb", "0.00001", "--max-file-mb-text", "1"]);
    let rt = rec_of(&recs, &t);
    assert!(rt.get("error").is_none(), "{}", rt);
    assert_eq!(rt["hash"].as_str().unwrap(), sha256(BODY_A.as_bytes()));
    let rh = rec_of(&recs, &h);
    assert_eq!(rh["error"]["kind"], "size_limit", "{}", rh);
    let why = rh["error"]["reason"].as_str().unwrap_or("");
    assert!(why.contains("MAX_FILE_BYTES") && why.contains("--max-file-mb"), "{}", why);
    env.assert_tmp_clean();
}

#[test]
fn 원래_크기가_있으면_그_크기로_거르고_no_size_limit_이면_끈다() {
    let env = Env::new("prelimit_org");
    let t = env.store("F_org.SMF", &encrypt(BODY_A.as_bytes()));
    // 원래 크기를 1GB 로 적는다 — 저장본은 작아도 원래 크기가 텍스트 상한(100MB)을 넘는다.
    let list = env.filelist(&[serde_json::json!({"path": t, "sfile_id": "SO", "mrk": GOOD, "enc_mode": 3,
                                                 "zip_mode": 0, "name": "메모.txt", "size": 1_000_000_000u64})]);
    let (_, recs) = env.run(&["--filelist", &list]);
    let x = rec_of(&recs, &t);
    assert_eq!(x["error"]["kind"], "size_limit", "{}", x);
    let why = x["error"]["reason"].as_str().unwrap_or("");
    assert!(why.contains("org_size") && why.contains("MAX_FILE_BYTES_TEXT"), "{}", why);
    // --no-size-limit 이면 앞 상한이 꺼져 풀어 본다 — 그러면 크기 검사(verify)가 어긋남을 잡는다.
    let (_, recs) = env.run(&["--filelist", &list, "--no-size-limit"]);
    let x = rec_of(&recs, &t);
    assert_eq!(x["error"]["kind"], "decrypt_failed", "{}", x);
    assert_eq!(x["error"]["detail"], "size_mismatch", "{}", x);
    env.assert_tmp_clean();
}

#[test]
fn text_only_도_복호화_전_크기_상한을_본다() {
    // 예전에는 이 경로에 앞 검사가 없어 큰 문서를 다 푼 뒤 버렸다 — 이제 풀지 않고 멈춘다.
    let env = Env::new("prelimit_textonly");
    let enc = env.store("F_t.SMF", &encrypt(BODY_A.as_bytes()));
    let tool = fake_tool();
    let o = Command::new(env!("CARGO_BIN_EXE_MpowerClassify-rs"))
        .args(["--text-only", "--file", &enc, "--decrypt", "--mrk", GOOD,
               "--enc-mode", "3", "--zip-mode", "0", "--name", "보고서.hwp",
               "--max-file-mb", "0.00001",
               "--decrypt-cmd", tool.to_str().unwrap(),
               "--decrypt-tmp", env.tmp().to_str().unwrap(), "--json-errors"])
        .env("CSOCLASSIFY_LOGDIR", env.dir.join("log"))
        .output().unwrap();
    assert_eq!(o.status.code(), Some(1), "stderr={}", String::from_utf8_lossy(&o.stderr));
    assert_eq!(status(&o).0, "decrypt_failed");
    let logs = env.all_logs();
    assert!(logs.contains("reason=size_limit") && logs.contains("MAX_FILE_BYTES"), "{}", logs);
    env.assert_tmp_clean();
}

#[test]
fn text_only_도_풀어서_본문을_뽑는다() {
    let env = Env::new("textonly");
    let enc = env.store("F_t.SMF", &encrypt(BODY_A.as_bytes()));
    let tool = fake_tool();
    let o = Command::new(env!("CARGO_BIN_EXE_MpowerClassify-rs"))
        .args(["--text-only", "--file", &enc, "--decrypt", "--mrk", GOOD,
               "--enc-mode", "3", "--zip-mode", "0", "--name", "메모.txt",
               "--decrypt-cmd", tool.to_str().unwrap(),
               "--decrypt-tmp", env.tmp().to_str().unwrap()])
        .env("CSOCLASSIFY_LOGDIR", env.dir.join("log"))
        .output().unwrap();
    assert_eq!(o.status.code(), Some(0), "stderr={}", String::from_utf8_lossy(&o.stderr));
    let out = String::from_utf8_lossy(&o.stdout);
    assert!(out.contains("복호화 연동을 시험하려고"), "본문이 없다: {}", out);
    // 머리줄에는 임시 경로가 아니라 원래 경로가 나온다.
    assert!(out.contains(&format!("===== {} =====", enc)), "{}", out);
    assert!(!out.contains("cso_dec_"));
    env.assert_tmp_clean();
}
