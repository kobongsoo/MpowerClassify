//! 사람이 정한 보안등급(--grade-overrides · --set-grade) 통합 시험 — 파이썬 판 tests/test_grade_override.py 와 같은 경우.
//! 설계: plan/사람등급지정-수동오버라이드-설계-20261020.html §10.

use serde_json::Value;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

// 규칙만으로 S 가 나오는 본문과 신호가 없는 본문 — 파이썬 시험과 같다
const BODY_S: &str = "고객 명단\n홍길동 주민등록번호 900101-1234567 연락처 010-1234-5678\n김철수 주민등록번호 850505-2345678 연락처 010-9876-5432\n";
const BODY_NONE: &str = "사내 동호회 운영 안내입니다. 동호회 활동비는 분기마다 지급합니다.\n";

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
        let dir = std::env::temp_dir().join(format!("csors_ov_{}_{}", tag, std::process::id()));
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
    fn store(&self, name: &str, data: &str) -> PathBuf {
        let p = self.dir.join("docs").join(name);
        std::fs::write(&p, data).unwrap();
        p
    }

    //--------------------------------------------------------------
    // 실행 — 규칙 파일을 함께 넘긴다
    //
    // -in: args = 명령줄 인자
    //
    // -out: Output
    // -out: error = 실행 파일을 못 부르면 panic
    //--------------------------------------------------------------
    fn run(&self, args: &[&str]) -> Output {
        let rules = Path::new(env!("CARGO_MANIFEST_DIR")).join("resources").join("policy").join("cso_rule.yaml");
        let mut a: Vec<&str> = args.to_vec();
        a.extend(["--cso-rules", rules.to_str().unwrap(), "--json-errors"]);
        Command::new(env!("CARGO_BIN_EXE_MpowerClassify-rs"))
            .args(&a)
            .env("CSOCLASSIFY_POLICY_DIR", self.dir.join("no_policy"))
            .env("CSOCLASSIFY_LOGDIR", self.dir.join("log"))
            .output()
            .expect("실행 파일을 부르지 못했다")
    }
}

impl Drop for Env {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

//------------------------------------------------------------------
// jsonl 결과 읽기 — (파일 이름별 레코드, 요약)
//
// -in: p = 결과 파일
//
// -out: (Vec<(String, Value)>, Value)
// -out: error = 없음
//------------------------------------------------------------------
fn read(p: &Path) -> (Vec<(String, Value)>, Value) {
    let mut recs = vec![];
    let mut summ = Value::Null;
    for l in std::fs::read_to_string(p).unwrap_or_default().lines() {
        if let Ok(v) = serde_json::from_str::<Value>(l) {
            if let Some(s) = v.get("summary") { summ = s.clone(); }
            else if let Some(f) = v.get("file").and_then(|x| x.as_str()) {
                let name = f.replace('\\', "/").rsplit('/').next().unwrap().to_string();
                recs.push((name, v));
            }
        }
    }
    (recs, summ)
}

//------------------------------------------------------------------
// 이름으로 레코드 찾기
//
// -in: recs = 레코드 목록 · name = 파일 이름
//
// -out: &Value
// -out: error = 없으면 panic
//------------------------------------------------------------------
fn get<'a>(recs: &'a [(String, Value)], name: &str) -> &'a Value {
    &recs.iter().find(|(n, _)| n == name).unwrap_or_else(|| panic!("{} 없음", name)).1
}

#[test]
fn 인자_오류() {
    let env = Env::new("args");
    let p = env.store("a.txt", BODY_NONE);
    let ps = p.to_str().unwrap();
    let docs = env.dir.join("docs");
    let ov = env.dir.join("ov.jsonl");
    let cases: Vec<(Vec<&str>, &str)> = vec![
        (vec!["--set-grade", "X", "--file", ps], "bad_args"),
        (vec!["--set-grade", "C", "--dir", docs.to_str().unwrap()], "mode_conflict"),
        (vec!["--set-grade", "C", "--file", ps, "--text-only"], "mode_conflict"),
        (vec!["--set-grade", "C", "--file", ps, "--grade-overrides", ov.to_str().unwrap()], "bad_args"),
        (vec!["--file", ps, "--reviewer", "a"], "bad_args"),
        (vec!["--file", ps, "--grade-overrides", "없음.jsonl"], "grade_overrides_missing"),
    ];
    for (a, kind) in cases {
        let o = env.run(&a);
        assert_eq!(o.status.code(), Some(3), "{:?}", a);
        assert!(String::from_utf8_lossy(&o.stdout).contains(kind), "{:?} → {}", a, String::from_utf8_lossy(&o.stdout));
    }
}

#[test]
fn set_grade_없는_파일은_3004() {
    let env = Env::new("missing");
    let p = env.dir.join("없음.txt");
    let o = env.run(&["--set-grade", "C", "--file", p.to_str().unwrap(), "--format", "jsonl"]);
    assert_eq!(o.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&o.stdout).contains("file_missing"));
}

#[cfg(windows)]
#[test]
fn set_grade_와_재분류_유지() {
    let env = Env::new("flow");
    let s = env.store("s.txt", BODY_S);
    let n = env.store("none.txt", BODY_NONE);
    let ov = env.dir.join("ov.jsonl");
    // ① 미분류 문서를 C 로 바로 정하고 목록에 남긴다
    let o = env.run(&["--set-grade", "C", "--file", n.to_str().unwrap(), "--write-grade-stream",
                      "--grade-overrides", ov.to_str().unwrap(), "--reviewer", "홍길동", "--reason", "시험", "--format", "jsonl"]);
    assert_eq!(o.status.code(), Some(0), "{}", String::from_utf8_lossy(&o.stderr));
    let first = String::from_utf8_lossy(&o.stdout).lines().next().unwrap().to_string();
    let rec: Value = serde_json::from_str(&first).unwrap();
    assert_eq!(rec["grade"], "C");
    assert_eq!(rec["why"]["security"]["method"], "manual_override");
    assert_eq!(rec["grade_stream"]["state"], "written");
    let last = std::fs::read_to_string(&ov).unwrap().lines().last().unwrap().to_string();
    let entry: Value = serde_json::from_str(&last).unwrap();
    assert_eq!(entry["new_grade"], "C");
    assert_eq!(entry["reviewer"], "홍길동");
    // ② S 문서는 내용이 바뀐 뒤의 O 결정이라 따르지 않는다(stale)
    {
        use std::io::Write;
        let mut f = std::fs::OpenOptions::new().append(true).open(&ov).unwrap();
        writeln!(f, "{}", serde_json::json!({"file": s.to_str().unwrap(), "hash": "0".repeat(64),
            "axis": "security", "kind": "change", "new_grade": "O"})).unwrap();
    }
    let out = env.dir.join("r.jsonl");
    let docs = env.dir.join("docs");
    let o = env.run(&["--dir", docs.to_str().unwrap(), "--rule-only", "--no-timing", "--format", "jsonl",
                      "--grade-overrides", ov.to_str().unwrap(), "--write-grade-stream", "--out", out.to_str().unwrap()]);
    assert_eq!(o.status.code(), Some(0), "{}", String::from_utf8_lossy(&o.stderr));
    let (recs, summ) = read(&out);
    assert_eq!(get(&recs, "none.txt")["grade"], "C");
    assert_eq!(get(&recs, "none.txt")["why"]["security"]["override"]["matched_by"], "hash");
    assert_eq!(get(&recs, "none.txt")["grade_stream"]["state"], "unchanged");
    assert_eq!(get(&recs, "s.txt")["grade"], "S");
    assert_eq!(get(&recs, "s.txt")["why"]["security"]["override"]["stale"], true);
    assert_eq!(summ["grade_overrides"], serde_json::json!({"loaded": 2, "applied": 1, "confirmed": 0, "stale": 1}));
    assert_eq!(summ["C"], 1);
    assert_eq!(summ["S"], 1);
    assert_eq!(summ["unclassified"], 0);
}
