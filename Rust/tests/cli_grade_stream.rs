//! 등급 스트림(:mgrade) 기록 통합 시험 — 파이썬 판 tests/test_grade_stream.py 와 같은 경우를 본다.
//! 설계: plan/등급스트림-기록-옵션-설계-20261019.html §11.

use serde_json::Value;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

// 규칙만으로 S 가 나오는 본문(주민번호 · 연락처)과 신호가 없는 본문 — 파이썬 시험과 같다
const BODY_S: &str = "고객 명단\n홍길동 주민등록번호 900101-1234567 연락처 010-1234-5678\n김철수 주민등록번호 850505-2345678 연락처 010-9876-5432\n";
const BODY_NONE: &str = "사내 동호회 운영 안내입니다. 동호회 활동비는 분기마다 지급합니다.\n";

//------------------------------------------------------------------
// 12바이트 기대값
//
// -in: code = 등급 숫자
//
// -out: Vec<u8> = "MGRD" · 1 · code
// -out: error = 없음
//------------------------------------------------------------------
fn payload(code: u8) -> Vec<u8> {
    vec![0x4D, 0x47, 0x52, 0x44, 1, 0, 0, 0, code, 0, 0, 0]
}

//------------------------------------------------------------------
// 시험 한 건의 작업장
//
// -필드: dir = 이 시험 전용 임시 폴더
//------------------------------------------------------------------
struct Env {
    dir: PathBuf,
}

impl Env {
    //--------------------------------------------------------------
    // 작업장 만들기
    //
    // -in: tag = 폴더 이름 꼬리
    //
    // -out: Env
    // -out: error = 폴더를 못 만들면 panic
    //--------------------------------------------------------------
    fn new(tag: &str) -> Env {
        let dir = std::env::temp_dir().join(format!("csors_gs_{}_{}", tag, std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("docs")).unwrap();
        Env { dir }
    }

    //--------------------------------------------------------------
    // 문서 하나 두기(docs/ 아래)
    //
    // -in: name = 파일 이름 · data = 내용
    //
    // -out: PathBuf = 파일 경로
    // -out: error = 못 쓰면 panic
    //--------------------------------------------------------------
    fn store(&self, name: &str, data: &[u8]) -> PathBuf {
        let p = self.dir.join("docs").join(name);
        std::fs::write(&p, data).unwrap();
        p
    }

    //--------------------------------------------------------------
    // 실행한다 — 규칙만, 결과는 jsonl 파일로
    //
    // -in: args = 명령줄 인자
    //
    // -out: (Output, 레코드 목록, 요약)
    // -out: error = 실행 파일을 못 부르면 panic
    //--------------------------------------------------------------
    fn run(&self, args: &[&str]) -> (Output, Vec<Value>, Value) {
        let out = self.dir.join("result.jsonl");
        let _ = std::fs::remove_file(&out);
        let rules = Path::new(env!("CARGO_MANIFEST_DIR")).join("resources").join("policy").join("cso_rule.yaml");
        let mut a: Vec<&str> = args.to_vec();
        a.extend(["--cso-rules", rules.to_str().unwrap(), "--rule-only", "--no-timing", "--json-errors",
                  "--format", "jsonl", "--out", out.to_str().unwrap()]);
        let o = Command::new(env!("CARGO_BIN_EXE_MpowerClassify-rs"))
            .args(&a)
            .env("CSOCLASSIFY_POLICY_DIR", self.dir.join("no_policy"))
            .env("CSOCLASSIFY_LOGDIR", self.dir.join("log"))
            .output()
            .expect("실행 파일을 부르지 못했다");
        let mut recs = vec![];
        let mut summ = Value::Null;
        for l in std::fs::read_to_string(&out).unwrap_or_default().lines() {
            if let Ok(v) = serde_json::from_str::<Value>(l) {
                if let Some(s) = v.get("summary") { summ = s.clone(); } else if v.get("file").is_some() { recs.push(v); }
            }
        }
        (o, recs, summ)
    }

    //--------------------------------------------------------------
    // 인자 오류만 보는 실행(결과 파일 없음)
    //
    // -in: args = 명령줄 인자
    //
    // -out: Output
    // -out: error = 실행 파일을 못 부르면 panic
    //--------------------------------------------------------------
    fn run_raw(&self, args: &[&str]) -> Output {
        Command::new(env!("CARGO_BIN_EXE_MpowerClassify-rs"))
            .args(args).arg("--json-errors")
            .env("CSOCLASSIFY_LOGDIR", self.dir.join("log"))
            .output().expect("실행 파일을 부르지 못했다")
    }
}

impl Drop for Env {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

//------------------------------------------------------------------
// 스트림 내용 바로 읽기
//
// -in: p = 문서 경로
//
// -out: Option<Vec<u8>> = 스트림 내용(없으면 None)
// -out: error = 없음
//------------------------------------------------------------------
fn raw(p: &Path) -> Option<Vec<u8>> {
    std::fs::read(format!("{}:mgrade", p.display())).ok()
}

//------------------------------------------------------------------
// 파일 이름으로 레코드 찾기
//
// -in: recs = 레코드 · name = 끝 이름 · archive = 압축 집계 레코드인가
//
// -out: &Value
// -out: error = 없으면 panic
//------------------------------------------------------------------
fn find<'a>(recs: &'a [Value], name: &str, archive: bool) -> &'a Value {
    recs.iter().find(|r| r["file"].as_str().unwrap().replace('\\', "/").ends_with(&format!("/{}", name))
        && r.get("archive").and_then(|v| v.as_bool()).unwrap_or(false) == archive)
        .unwrap_or_else(|| panic!("{} 레코드가 없다: {:?}", name, recs))
}

#[test]
fn 보조_옵션만_주면_1003() {
    let env = Env::new("aux");
    let p = env.store("a.txt", BODY_NONE.as_bytes());
    let o = env.run_raw(&["--file", p.to_str().unwrap(), "--grade-stream-update-time"]);
    assert_eq!(o.status.code(), Some(3));
    assert!(String::from_utf8_lossy(&o.stdout).contains("bad_args"));
    // 시각 보존이 기본이 되어 keep-time 은 없앴다 — 주면 모르는 인자
    let o = env.run_raw(&["--file", p.to_str().unwrap(), "--write-grade-stream", "--grade-stream-keep-time"]);
    assert_eq!(o.status.code(), Some(3));
    assert!(String::from_utf8_lossy(&o.stdout).contains("bad_args"));
    let o = env.run_raw(&["--file", p.to_str().unwrap(), "--write-grade-stream", "--grade-stream-unclassified", "zero"]);
    assert_eq!(o.status.code(), Some(3));
    assert!(String::from_utf8_lossy(&o.stdout).contains("bad_args"));
}

#[test]
fn 분류_결과가_없는_모드와_함께면_1006() {
    let env = Env::new("mode");
    let p = env.store("a.txt", BODY_NONE.as_bytes());
    for m in ["--text-only", "--embed"] {
        let o = env.run_raw(&["--file", p.to_str().unwrap(), "--write-grade-stream", m]);
        assert_eq!(o.status.code(), Some(3), "{}", m);
        assert!(String::from_utf8_lossy(&o.stdout).contains("mode_conflict"), "{}", m);
    }
    assert!(raw(&p).is_none());
}

#[cfg(not(windows))]
#[test]
fn 윈도우가_아니면_1012() {
    let env = Env::new("os");
    let p = env.store("a.txt", BODY_NONE.as_bytes());
    let o = env.run_raw(&["--file", p.to_str().unwrap(), "--write-grade-stream"]);
    assert_eq!(o.status.code(), Some(3));
    assert!(String::from_utf8_lossy(&o.stdout).contains("unsupported_option"));
}

#[cfg(windows)]
#[test]
fn 옵션을_안_주면_아무것도_쓰지_않는다() {
    let env = Env::new("off");
    let p = env.store("s.txt", BODY_S.as_bytes());
    let (o, recs, summ) = env.run(&["--file", p.to_str().unwrap()]);
    assert_eq!(o.status.code(), Some(0), "{}", String::from_utf8_lossy(&o.stderr));
    assert!(recs[0].get("grade_stream").is_none() && summ.get("grade_stream").is_none());
    assert!(raw(&p).is_none());
}

#[cfg(windows)]
#[test]
fn 폴더_분류_대상_규칙() {
    let env = Env::new("dir");
    let s = env.store("s.txt", BODY_S.as_bytes());
    let n = env.store("none.txt", BODY_NONE.as_bytes());
    let b = env.store("broken.hwp", b"xx");
    // 압축 안에 S 문서 하나
    let zp = env.dir.join("docs").join("z.zip");
    {
        use std::io::Write;
        let mut z = zip::ZipWriter::new(std::fs::File::create(&zp).unwrap());
        z.start_file("in.txt", zip::write::FileOptions::default()).unwrap();
        z.write_all(BODY_S.as_bytes()).unwrap();
        z.finish().unwrap();
    }
    let docs = env.dir.join("docs");
    let (_, recs, summ) = env.run(&["--dir", docs.to_str().unwrap(), "--write-grade-stream"]);
    assert_eq!(find(&recs, "s.txt", false)["grade_stream"], serde_json::json!({"state": "written", "value": "S"}));
    assert_eq!(find(&recs, "none.txt", false)["grade_stream"], serde_json::json!({"state": "skipped", "reason": "unclassified"}));
    assert_eq!(find(&recs, "broken.hwp", false)["grade_stream"], serde_json::json!({"state": "skipped", "reason": "not_read"}));
    assert_eq!(find(&recs, "in.txt", false)["grade_stream"], serde_json::json!({"state": "skipped", "reason": "archive_member"}));
    assert_eq!(find(&recs, "z.zip", true)["grade_stream"], serde_json::json!({"state": "written", "value": "S"}));
    assert_eq!(summ["grade_stream"], serde_json::json!({"written": 2, "unchanged": 0, "skipped": 3, "failed": 0}));
    assert_eq!(raw(&s), Some(payload(2)));
    assert_eq!(raw(&zp), Some(payload(2)));
    assert!(raw(&n).is_none() && raw(&b).is_none());
    // 다시 돌리면 전부 그대로 — none 옵션이면 미분류에 NONE 을 쓴다
    let (_, _, summ) = env.run(&["--dir", docs.to_str().unwrap(), "--write-grade-stream", "--grade-stream-unclassified", "none"]);
    assert_eq!(summ["grade_stream"], serde_json::json!({"written": 1, "unchanged": 2, "skipped": 2, "failed": 0}));
    assert_eq!(raw(&n), Some(payload(0)));
    assert!(raw(&b).is_none());
}

#[cfg(windows)]
#[test]
fn 읽기_전용과_시각을_기본으로_되돌린다() {
    let env = Env::new("ro");
    let p = env.store("s.txt", BODY_S.as_bytes());
    let old = std::time::SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(1_600_000_000);
    {
        let f = std::fs::OpenOptions::new().write(true).open(&p).unwrap();
        f.set_modified(old).unwrap();
    }
    let mut perm = std::fs::metadata(&p).unwrap().permissions();
    perm.set_readonly(true);
    std::fs::set_permissions(&p, perm).unwrap();
    // 옵션 없이 — 시각 보존이 기본이다
    let (_, recs, _) = env.run(&["--file", p.to_str().unwrap(), "--write-grade-stream"]);
    assert_eq!(recs[0]["grade_stream"]["state"], "written");
    let m = std::fs::metadata(&p).unwrap();
    assert!(m.permissions().readonly(), "읽기 전용이 풀린 채다");
    assert_eq!(m.modified().unwrap(), old, "수정 시각이 바뀌었다");
    assert_eq!(raw(&p), Some(payload(2)));
    let mut perm = m.permissions();
    #[allow(clippy::permissions_set_readonly_false)]
    perm.set_readonly(false);
    std::fs::set_permissions(&p, perm).unwrap();
}

#[cfg(windows)]
#[test]
fn update_time_이면_수정_시각이_바뀐다() {
    let env = Env::new("upd");
    let p = env.store("s.txt", BODY_S.as_bytes());
    // 다른 값을 미리 넣어 둔다 — 같은 값이면 쓰지 않아 시각도 그대로다
    std::fs::write(format!("{}:mgrade", p.display()), payload(3)).unwrap();
    let old = std::time::SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(1_600_000_000);
    std::fs::OpenOptions::new().write(true).open(&p).unwrap().set_modified(old).unwrap();
    let (_, recs, _) = env.run(&["--file", p.to_str().unwrap(), "--write-grade-stream", "--grade-stream-update-time"]);
    assert_eq!(recs[0]["grade_stream"], serde_json::json!({"state": "written", "value": "S", "prev": "O"}));
    assert_ne!(std::fs::metadata(&p).unwrap().modified().unwrap(), old, "수정 시각이 그대로다");
}

#[cfg(windows)]
#[test]
fn 새_버전_스트림은_덮어쓰지_않는다() {
    let env = Env::new("ver");
    let p = env.store("s.txt", BODY_S.as_bytes());
    let newer = [0x4D, 0x47, 0x52, 0x44, 2, 0, 0, 0, 1, 0, 0, 0, 9, 9];
    std::fs::write(format!("{}:mgrade", p.display()), newer).unwrap();
    let (_, recs, _) = env.run(&["--file", p.to_str().unwrap(), "--write-grade-stream"]);
    assert_eq!(recs[0]["grade_stream"], serde_json::json!({"state": "skipped", "value": "S", "reason": "newer_version"}));
    assert_eq!(raw(&p), Some(newer.to_vec()));
}

#[cfg(windows)]
#[test]
fn 쓰기_실패는_4002_종료코드_1() {
    use std::os::windows::fs::OpenOptionsExt;
    let env = Env::new("lock");
    let p = env.store("s.txt", BODY_S.as_bytes());
    // 잠금은 스트림마다 따로다 — :mgrade 를 공유 0 으로 잡아 둔다(본문 읽기 · 분류는 된다)
    let sp = format!("{}:mgrade", p.display());
    std::fs::write(&sp, payload(3)).unwrap();
    let hold = std::fs::OpenOptions::new().read(true).share_mode(0).open(&sp).unwrap();
    let (o, recs, summ) = env.run(&["--file", p.to_str().unwrap(), "--write-grade-stream"]);
    drop(hold);
    assert_eq!(recs[0]["grade"], "S");
    assert_eq!(recs[0]["grade_stream"],
        serde_json::json!({"state": "failed", "value": "S", "reason": "sharing_violation", "winerr": 32}));
    assert_eq!(summ["grade_stream"]["failed"], 1);
    assert_eq!(o.status.code(), Some(1));
    let all = std::fs::read_to_string(env.dir.join("result.jsonl")).unwrap();
    assert!(all.contains("4002"), "{}", all);
}
