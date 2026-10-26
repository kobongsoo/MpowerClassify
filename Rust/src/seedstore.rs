//! 기준 문서 저장소(class_seed.jsonl) 쓰기 — 스키마 v3 · 파이썬 판 seedstore.py 포팅.
//!
//! [왜 두 판이 같은 파일을 쓰나]
//! 화면(Streamlit)은 파이썬 seedstore 를 쓰고, 이 판은 여기를 쓴다. 같은 파일에
//! 두 구현이 쓰는 셈이라, 한쪽이 칸을 빠뜨리거나 순서가 달라지면 두 판으로 만든
//! 기준 문서가 미묘하게 다른 모양이 되고 아무도 눈치채지 못한다. 그래서
//!   · 칸 차례는 SEED_ORDER 한 곳에만 적는다(파이썬 _ordered 와 같은 차례)
//!   · 빈 값은 아예 안 적는다(파이썬 _clean 과 같은 규약)
//!   · 시각 표기는 chrono 로 파이썬과 같은 로컬 ISO-8601(초)로 맞춘다
//! 이 세 가지가 두 판을 묶는 계약이다.
//!
//! [스키마 v3 — 평평한 한 줄]
//!   {v, file, doc_id?, hash?, grade?, doctype?[], hold?, source?, approved_by?,
//!    note?, ts, dim?, engine?, vector?[]}
//! v2 는 진실을 axes 안에 3겹으로 넣고 grade·labels 를 파생값으로 만들어 냈다.
//! v3 는 칸 하나가 곧 진실이라, 저장 직전에 함수를 반드시 통과해야만 앞뒤가
//! 맞는 형식이 아니다 — 그래서 CLI 처럼 다른 프로세스가 써도 안전하다.

use std::fs;
use std::io::Write;
use std::path::Path;
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};

/// 이 파일이 쓰는 스키마 판.
pub const SCHEMA_VERSION: u64 = 3;

/// 이 벡터를 어느 판이 만들었나. 두 판은 추출기가 달라 같은 문서라도 벡터가
/// 완전히 같지 않다 — 실측(문서 18건)에서 17건은 소수점 8자리 수준 차이였고
/// 1건이 코사인 0.980 이었다. 전파 문턱(0.950)에는 안 걸리지만, 한 파일에 두
/// 판이 쓰기 시작한 뒤에는 어느 판 것인지 되짚을 방법이 없다. 차원이 같아
/// dim 점검에도 안 걸리므로, 이 칸이 유일한 단서다.
pub const ENGINE: &str = "rust";

/// 저장소 파일이 다른 작업에 잡혀 있을 때 기다리는 상한(초). 파이썬 판과 같은 값.
pub const LOCK_WAIT_SEC: f64 = 10.0;

/// 저장할 때의 칸 차례 — '무엇인가 → 무엇으로 정했나 → 누가 언제 → 벡터'.
/// 벡터(384개 숫자)가 앞에 오면 사람이 파일을 열었을 때 아무것도 못 읽는다.
/// 파이썬 판 `_ordered()` 의 head 와 **글자까지 같아야** 한다.
const SEED_ORDER: &[&str] = &[
    "v", "file", "doc_id", "hash", "grade", "doctype", "title", "hold",
    "source", "approved_by", "note", "ts", "dim", "engine",
];

//------------------------------------------------------------------
// 지금 시각 — 파이썬 판(seedstore.TS_FMT)과 같은 모양의 문자열
//=> 같은 파일에 두 도구가 쓰는데 시각 표기가 다르면 사람이 정렬해 볼 수 없다.
//   main.rs 의 now_iso 와 같은 값이지만, 이 모듈만 떼어 시험할 수 있게 여기 둔다.
//
// -in: 없음
// -out: String = 예 "2026-09-08 15:30:00"
// -out: error = 없음
//------------------------------------------------------------------
pub fn now_iso() -> String {
    chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string()
}

//------------------------------------------------------------------
// 문서 경로를 비교용 키로 정규화
//=> 같은 문서가 "d:/sample/a.doc" 와 "D:\sample\A.doc" 처럼 다르게 적히면
//   저장소에 같은 문서가 두 줄로 앉는다(전파 때 한 문서가 두 번 투표한다).
//   비교할 때만 표기를 통일하고, 저장되는 값은 원래 표기 그대로 둔다.
//
// -in: path = 문서 경로
// -out: String = 소문자 + 슬래시로 통일한 비교용 키
// -out: error = 없음
//------------------------------------------------------------------
pub fn norm_file(path: &str) -> String {
    path.replace('\\', "/").trim_end_matches('/').to_lowercase()
}

//------------------------------------------------------------------
// 축 이름 → v3 의 값 칸 이름
//=> 화면·CLI 는 "security"/"doctype" 이라는 축 이름으로 말하지만, 파일에서는
//   그 값이 grade/doctype 칸에 평평하게 들어 있다. 여기서 한 번만 번역한다.
//
// -in: axis = "security" | "doctype"
// -out: &str = "grade" | "doctype"
// -out: error = 없음
//------------------------------------------------------------------
pub fn key_of(axis: &str) -> &'static str {
    if axis == "security" { "grade" } else { "doctype" }
}

//------------------------------------------------------------------
// 옛 행(v1/v2)을 v3 평평한 모양으로 끌어올리기
//=> 저장소에 어떤 판이 섞여 있어도 한 가지 모양만 다루게 한다. 파이썬 판
//   ensure_v3 와 같은 규칙이어야 한다 — 두 판이 같은 파일을 읽으므로.
//    1) v3 면 빈 칸만 걷어내고 그대로 둔다
//    2) v2 면 axes 를 풀어 grade/doctype 칸으로 올린다
//       · active → 평평한 칸에 · suspect → hold 로 옮김 · retired → 버림
//    3) axes 가 없는 v1 옛 행이면 grade/labels 가 곧 진실이다
//
// -in: e = 저장소에서 읽은 한 줄
// -out: Value = v3 모양의 새 객체
// -out: error = 없음(모양이 이상하면 빈 객체가 될 수 있다 — 호출부가 거른다)
//------------------------------------------------------------------
pub fn ensure_v3(e: &Value) -> Value {
    let mut out = Map::new();
    out.insert("v".into(), json!(SCHEMA_VERSION));
    if let Some(f) = e.get("file").and_then(|v| v.as_str()) {
        out.insert("file".into(), json!(f));
    }
    if let Some(d) = e.get("doc_id").and_then(|v| v.as_str()) {
        if !d.is_empty() { out.insert("doc_id".into(), json!(d)); }
    }
    // 원본 지문은 해시만 쓴다 — size/mtime 은 판정에 쓰이지 않아 v3 에서 뺐다.
    let h = e.get("hash").and_then(|v| v.as_str())
        .or_else(|| e.get("doc").and_then(|d| d.get("hash")).and_then(|v| v.as_str()));
    if let Some(h) = h {
        if !h.is_empty() { out.insert("hash".into(), json!(h)); }
    }

    let already_v3 = e.get("v").and_then(|v| v.as_u64()) == Some(SCHEMA_VERSION);
    if already_v3 {
        for k in ["grade", "doctype", "title", "hold", "source", "approved_by", "note",
                  "ts", "dim", "engine"] {
            if let Some(v) = e.get(k) {
                if !is_empty(v) { out.insert(k.into(), v.clone()); }
            }
        }
    } else {
        upgrade_axes(e, &mut out);
    }

    if out.get("ts").is_none() {
        out.insert("ts".into(), json!(now_iso()));
    }
    // 차원은 벡터 길이에서 되살릴 수 있다 — 옛 행에 없으면 채워 준다.
    if let Some(arr) = e.get("vector").and_then(|v| v.as_array()) {
        if !arr.is_empty() {
            if out.get("dim").is_none() { out.insert("dim".into(), json!(arr.len())); }
            out.insert("vector".into(), e["vector"].clone());
        }
    }
    // 쓸 만한 값이 아닌 축은 아예 지운다("빈 배열"과 "값 없음"을 가르지 않는다).
    if !usable(&out, "security") { out.remove("grade"); }
    if !usable(&out, "doctype") { out.remove("doctype"); }
    Value::Object(out)
}

//------------------------------------------------------------------
// v2 의 axes 블록을 평평한 칸으로 편다(ensure_v3 내부)
//=> active 는 값 칸으로, suspect 는 hold 로 옮기고, retired 는 버린다.
//   retired 를 버리는 이유: v2 에서도 파생값이 지워져 엔진이 못 봤고, 두 축을
//   모두 해제하면 줄째 사라졌다 — 이미 '지운다'와 같았다. 이전 값은 감사 로그에.
//
// -in: e   = 옛 행
// -in: out = 채워 넣을 v3 객체(제자리에서 고친다)
// -out: 없음
// -out: error = 없음
//------------------------------------------------------------------
fn upgrade_axes(e: &Value, out: &mut Map<String, Value>) {
    let axes = e.get("axes");
    let mut approvers: Vec<String> = vec![];
    let mut sources: Vec<String> = vec![];
    let mut notes: Vec<String> = vec![];
    let mut times: Vec<String> = vec![];
    let mut hold = Map::new();

    for axis in ["security", "doctype"] {
        let key = key_of(axis);
        let ax = axes.and_then(|a| a.get(axis));
        let Some(ax) = ax else {
            // v1 옛 행: axes 가 없으니 파생값이 곧 진실이었다.
            let val = if axis == "security" {
                e.get("grade").cloned()
            } else {
                e.get("labels").and_then(|l| l.get("doctype")).cloned()
            };
            if let Some(v) = val {
                if !is_empty(&v) { out.insert(key.into(), v); }
            }
            continue;
        };
        let state = ax.get("state").and_then(|v| v.as_str()).unwrap_or("");
        let val = ax.get("value").cloned().unwrap_or(Value::Null);
        if state == "active" && !is_empty(&val) {
            out.insert(key.into(), val);
        } else if state == "suspect" && !is_empty(&val) {
            hold.insert(key.into(), json!({
                "value": val,
                "reason": ax.get("reason").and_then(|v| v.as_str()).unwrap_or(""),
                "ts": ax.get("ts").and_then(|v| v.as_str()).unwrap_or("").to_string(),
            }));
        }
        for (bin, k) in [(&mut approvers, "approved_by"), (&mut sources, "source"),
                         (&mut notes, "note"), (&mut times, "ts")] {
            if let Some(s) = ax.get(k).and_then(|v| v.as_str()) {
                if !s.trim().is_empty() { bin.push(s.trim().to_string()); }
            }
        }
    }
    if !hold.is_empty() { out.insert("hold".into(), Value::Object(hold)); }

    // 줄 단위 값도 후보에 넣는다(옛 행은 여기에만 있다).
    for (bin, k) in [(&mut approvers, "approved_by"), (&mut sources, "source"),
                     (&mut notes, "note"), (&mut times, "ts")] {
        if let Some(s) = e.get(k).and_then(|v| v.as_str()) {
            if !s.trim().is_empty() { bin.push(s.trim().to_string()); }
        }
    }
    if let Some(a) = approvers.first() { out.insert("approved_by".into(), json!(a)); }
    if !sources.is_empty() {
        // 축마다 출처가 다르면 뜻을 잃지 않게 이어 붙인다(예: "phase4+phase6").
        let mut seen: Vec<String> = vec![];
        for s in &sources { if !seen.contains(s) { seen.push(s.clone()); } }
        out.insert("source".into(), json!(seen.join("+")));
    }
    // 메모는 비어 있지 않은 것 중 가장 긴 것 하나를 남긴다.
    if let Some(n) = notes.iter().max_by_key(|s| s.chars().count()) {
        out.insert("note".into(), json!(n));
    }
    if let Some(t) = times.iter().max() { out.insert("ts".into(), json!(t)); }
    if let Some(d) = e.get("embed").and_then(|x| x.get("dim")) {
        out.insert("dim".into(), d.clone());
    }
    // engine 은 옛 판에 없던 칸이다. 모르는 것을 "rust" 로 채우면 거짓이 된다.
}

//------------------------------------------------------------------
// 값이 비었는가(파이썬 _clean 과 같은 규약)
//=> null · "" · [] · {} 는 "값이 없다"로 본다. 파일에 빈 칸을 적지 않으면
//   사람이 열었을 때 있는 것만 보인다.
//
// -in: v = 검사할 값
// -out: bool
// -out: error = 없음
//------------------------------------------------------------------
fn is_empty(v: &Value) -> bool {
    match v {
        Value::Null => true,
        Value::String(s) => s.is_empty(),
        Value::Array(a) => a.is_empty(),
        Value::Object(o) => o.is_empty(),
        _ => false,
    }
}

//------------------------------------------------------------------
// 그 축의 값이 기준으로 쓸 만한가
//=> 보안축은 C/S/O 셋 중 하나, 업무분류축은 비어 있지 않은 목록이어야 한다.
//
// -in: row  = v3 줄
// -in: axis = "security" | "doctype"
// -out: bool
// -out: error = 없음
//------------------------------------------------------------------
pub fn usable(row: &Map<String, Value>, axis: &str) -> bool {
    match row.get(key_of(axis)) {
        None => false,
        Some(v) => {
            if axis == "security" {
                matches!(v.as_str(), Some("C") | Some("S") | Some("O"))
            } else {
                v.as_array().map_or(false, |a| a.iter().any(|x| !is_empty(x)))
            }
        }
    }
}

//------------------------------------------------------------------
// 이 줄을 파일에 남겨 둘 이유가 있는가
//=> 지금 쓰는 축(값이 있는 것)이나 보류(hold)된 축이 하나라도 있으면 남긴다.
//   보류는 사람이 확인해 되살릴 대상이라, 사라지면 확인 자체가 불가능해진다.
//
// -in: row = v3 줄
// -out: bool
// -out: error = 없음
//------------------------------------------------------------------
pub fn keepable(row: &Value) -> bool {
    let Some(m) = row.as_object() else { return false };
    if usable(m, "security") || usable(m, "doctype") { return true; }
    m.get("hold").and_then(|h| h.as_object()).map_or(false, |h| !h.is_empty())
}

//------------------------------------------------------------------
// 저장할 때 칸 차례 맞추기
//=> SEED_ORDER 에 적힌 칸을 그 차례로 먼저 넣고, 표에 없는 칸은 뒤에 그대로
//   붙인다(새 칸이 생겼을 때 조용히 지워 버리면 가장 찾기 어려운 사고가 된다).
//   벡터는 언제나 맨 뒤다.
//
// -in: row = v3 줄
// -out: Value = 차례가 정리된 새 객체
// -out: error = 없음
//------------------------------------------------------------------
pub fn ordered(row: &Value) -> Value {
    let Some(m) = row.as_object() else { return row.clone() };
    let mut out = Map::new();
    for k in SEED_ORDER {
        if let Some(v) = m.get(*k) { out.insert((*k).to_string(), v.clone()); }
    }
    for (k, v) in m {
        if k != "vector" && !out.contains_key(k) { out.insert(k.clone(), v.clone()); }
    }
    if let Some(v) = m.get("vector") { out.insert("vector".into(), v.clone()); }
    Value::Object(out)
}

//------------------------------------------------------------------
// 저장소 읽기 — 옛 판은 올리고, 모르는 미래 판은 세어서 건너뛴다
//=> v 가 3보다 크면 이 코드가 모르는 형식이다. 추측해서 읽으면 기준 문서를
//   잘못 해석하므로 건너뛰되, 몇 줄을 건너뛰었는지 돌려준다 — 조용히 사라지면
//   "기준 문서가 왜 줄었지"를 아무도 알 수 없다.
//
// -in: path = class_seed.jsonl 경로
// -out: (rows, skipped) = v3 줄 목록, 건너뛴 줄 수
// -out: error = 없음(파일이 없으면 빈 목록)
//------------------------------------------------------------------
pub fn load(path: &str) -> (Vec<Value>, usize) {
    let mut rows = vec![];
    let mut skipped = 0usize;
    let Ok(txt) = fs::read_to_string(path) else { return (rows, 0) };
    for line in txt.lines() {
        let line = line.trim();
        if line.is_empty() { continue; }
        let Ok(e) = serde_json::from_str::<Value>(line) else { skipped += 1; continue };
        if e.get("v").and_then(|v| v.as_u64()).map_or(false, |v| v > SCHEMA_VERSION) {
            skipped += 1;
            continue;
        }
        rows.push(ensure_v3(&e));
    }
    (rows, skipped)
}

//------------------------------------------------------------------
// 저장소 저장(통째 덮어쓰기 · 원자적 교체)
//=> 같은 폴더의 임시파일에 다 쓴 뒤 이름을 바꾼다. 도중에 죽어도 원본은 옛
//   내용 그대로 남는다 — 기준 문서 파일이 반쪽이 되면 그 다음 분류가 통째로
//   잘못된 잣대로 돌아간다.
//   기준으로 쓸 것이 하나도 없는 줄은 빼고 쓴다(파이썬 save_seeds 와 같다).
//
// -in: path = 저장 경로
// -in: rows = v3 줄 목록
// -out: usize = 실제로 쓴 줄 수
// -out: error = 쓰기 실패 시 io::Error
//------------------------------------------------------------------
pub fn save(path: &str, rows: &[Value]) -> std::io::Result<usize> {
    let mut body = String::new();
    let mut n = 0usize;
    for r in rows {
        let e = ensure_v3(r);
        if !keepable(&e) { continue; }
        body.push_str(&serde_json::to_string(&ordered(&e)).unwrap_or_default());
        body.push('\n');
        n += 1;
    }
    let p = Path::new(path);
    if let Some(d) = p.parent() {
        if !d.as_os_str().is_empty() { fs::create_dir_all(d)?; }
    }
    // 같은 폴더에 만들어야 이름 바꾸기가 원자적이다(다른 드라이브면 복사가 된다).
    let tmp = p.with_extension(format!("tmp{}", std::process::id()));
    {
        let mut f = fs::File::create(&tmp)?;
        f.write_all(body.as_bytes())?;
        f.sync_all()?;
    }
    fs::rename(&tmp, p)?;
    Ok(n)
}

//------------------------------------------------------------------
// 저장소 파일 잠금 — 살아 있는 동안 다른 프로세스를 막는다
//=> 웹이 CLI 를 부르기 시작하면 두 요청이 겹치는 것이 정상이고, 그대로 두면
//   한쪽 등록이 아무 말 없이 사라진다(둘 다 옛 내용을 읽어 각자 전체를 다시 쓴다).
//    1) <파일>.lock 을 '이미 있으면 실패'로 만든다 — 만든 쪽이 잠금을 쥔다
//    2) LOCK_WAIT_SEC 까지 기다렸다 못 잡으면 실패한다(배치를 세우지 않는다)
//    3) Drop 에서 지운다 — 정상 종료·패닉 어느 쪽이든 풀린다
//
//   [파이썬 판과 방식이 다르다]
//   파이썬은 운영체제의 바이트 잠금(msvcrt/fcntl)을 쓴다. 그 방식을 쓰려면 이
//   판에 플랫폼 FFI 의존이 하나 늘어나는데, "정적 단일 바이너리" 목표와
//   CentOS 7 빌드 조건 때문에 의존을 늘리지 않기로 했다. 그래서 잠금 파일
//   방식을 쓴다 — **같은 판끼리는 서로를 막지만, 파이썬 판과 이 판이 동시에
//   같은 파일을 쓰는 경우는 막지 못한다.** 그 조합이 필요해지면 두 판을 같은
//   방식으로 맞춰야 한다(설계서 10장에 남겨 둔다).
//
// -in: path = 기준 문서 파일 경로(.lock 을 붙여 쓴다)
// -out: SeedLock = 살아 있는 동안 잠금이 유지되는 값
// -out: error = 상한을 넘겨도 못 잡으면 Err(사람이 읽는 한 줄)
//------------------------------------------------------------------
pub struct SeedLock {
    path: std::path::PathBuf,
}

impl SeedLock {
    pub fn acquire(path: &str) -> Result<SeedLock, String> {
        let lock = format!("{}.lock", path);
        let lp = std::path::PathBuf::from(&lock);
        if let Some(d) = lp.parent() {
            if !d.as_os_str().is_empty() { let _ = fs::create_dir_all(d); }
        }
        let deadline = Instant::now() + Duration::from_secs_f64(LOCK_WAIT_SEC);
        loop {
            match fs::OpenOptions::new().write(true).create_new(true).open(&lp) {
                Ok(_) => return Ok(SeedLock { path: lp }),
                Err(_) if Instant::now() < deadline => {
                    std::thread::sleep(Duration::from_millis(100));
                }
                Err(_) => {
                    return Err(format!(
                        "기준 문서 파일이 {:.0}초 동안 다른 작업에 잡혀 있습니다: {}",
                        LOCK_WAIT_SEC, lock));
                }
            }
        }
    }
}

impl Drop for SeedLock {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

//------------------------------------------------------------------
// 변경 기록 한 줄 남기기(append-only)
//=> 누가·언제·어느 축을·무엇에서 무엇으로 바꿨는지 남긴다. CLI 로 넣은 것만
//   기록이 없으면 감사 구멍이 된다. 칸 이름·차례는 파이썬 append_seed_audit 과
//   같아야 한다 — 한 파일에 두 판이 쓴다.
//
// -in: path     = class_seed_audit.jsonl 경로
// -in: action   = "add"|"update"|"retire"|"delete"
// -in: file     = 대상 문서
// -in: axis     = "security"|"doctype"|"-"
// -in: before   = 바뀌기 전 값(없으면 Null)
// -in: after    = 바뀐 뒤 값(없으면 Null)
// -in: reviewer = 작업자
// -in: reason   = 사유 한 줄
// -out: 없음
// -out: error = 쓰기 실패 시 io::Error(호출부가 알리고 계속한다)
//------------------------------------------------------------------
#[allow(clippy::too_many_arguments)]
pub fn append_audit(path: &str, action: &str, file: &str, axis: &str,
                    before: Value, after: Value, reviewer: &str,
                    reason: &str) -> std::io::Result<()> {
    let row = json!({
        "ts": now_iso(), "action": action, "axis": axis, "file": file,
        "before": before, "after": after, "reviewer": reviewer, "reason": reason,
    });
    let mut f = fs::OpenOptions::new().create(true).append(true).open(path)?;
    writeln!(f, "{}", serde_json::to_string(&row).unwrap_or_default())
}

//------------------------------------------------------------------
// 축 하나를 확정(추가·갱신) — 저장소로 들어가는 유일한 통로
//=> "이 문서의 이 축을 이 값으로 확정한다"를 한 함수로 모은다. 줄이 없으면
//   만들고, 있으면 그 축만 갈아 끼운다(다른 축은 손대지 않는다).
//   파이썬 set_axis 와 같은 규칙이어야 한다.
//    1) 정규화 비교로 같은 문서 줄을 찾는다(표기만 다른 중복 줄을 막는다)
//    2) 사람이 다시 확정하면 그 축의 보류(hold)는 풀린다
//    3) 출처는 이어 붙인다(phase4+cli) — 어디서 들어온 씨앗인지 세려면 필요하다
//
// -in: rows     = 기존 줄 목록(제자리에서 고친다)
// -in: file     = 문서 경로
// -in: axis     = "security" | "doctype"
// -in: value    = 그 축의 값
// -in: reviewer = 확정자
// -in: source   = 출처 표시
// -in: note     = 메모(빈 문자열이면 안 적는다)
// -in: vector   = 문서 벡터(None 이면 기존 것 유지)
// -in: hash     = 원본 지문(None 이면 기존 것 유지)
// -out: 없음(rows 를 제자리에서 고친다)
// -out: error = 없음
//------------------------------------------------------------------
#[allow(clippy::too_many_arguments)]
pub fn set_axis(rows: &mut Vec<Value>, file: &str, axis: &str, value: Value,
                reviewer: &str, source: &str, note: &str,
                vector: Option<&Vec<f32>>, hash: Option<&str>) {
    let key = norm_file(file);
    let idx = rows.iter().position(|r| {
        r.get("file").and_then(|v| v.as_str()).map(norm_file).as_deref() == Some(&key)
    });
    let i = match idx {
        Some(i) => i,
        None => {
            rows.push(json!({"v": SCHEMA_VERSION, "file": file}));
            rows.len() - 1
        }
    };
    let Some(m) = rows[i].as_object_mut() else { return };
    let k = key_of(axis);
    m.insert(k.to_string(), value);
    // 사람이 다시 확정했으면 시스템이 붙여 둔 보류 표시는 의미를 잃는다.
    if let Some(h) = m.get_mut("hold").and_then(|h| h.as_object_mut()) {
        h.remove(k);
        let empty = h.is_empty();
        if empty { m.remove("hold"); }
    }
    if !source.is_empty() {
        let prev = m.get("source").and_then(|v| v.as_str()).unwrap_or("");
        let mut parts: Vec<&str> = prev.split('+').filter(|s| !s.is_empty()).collect();
        if !parts.contains(&source) { parts.push(source); }
        m.insert("source".into(), json!(parts.join("+")));
    }
    if !reviewer.is_empty() { m.insert("approved_by".into(), json!(reviewer)); }
    if !note.is_empty() { m.insert("note".into(), json!(note)); }
    if let Some(v) = vector {
        m.insert("vector".into(), json!(v));
        m.insert("dim".into(), json!(v.len()));
        // 벡터를 새로 넣을 때만 판 이름을 적는다 — 값이 그대로면 만든 판도 그대로다.
        m.insert("engine".into(), json!(ENGINE));
    }
    if let Some(h) = hash {
        if !h.is_empty() { m.insert("hash".into(), json!(h)); }
    }
    m.insert("ts".into(), json!(now_iso()));
}

#[cfg(test)]
mod tests {
    use super::*;

    //--------------------------------------------------------------
    // 시각 표기는 파이썬 판과 같은 모양이어야 한다 <중요>
    //=> 2026-09-10 에 ISO-8601("…T17:10:56+09:00")에서 사람이 읽는 모양으로
    //   바꿨다. 같은 class_seed.jsonl 에 두 판이 쓰는데 한쪽만 바꾸면 표기가
    //   섞여 사람이 정렬해 볼 수 없다. 이건 오류를 내지 않고 조용히 어긋나므로
    //   모양 자체를 못박는다(파이썬 tests/test_seed_v3.py 와 같은 계약).
    //--------------------------------------------------------------
    #[test]
    fn 시각_표기가_정해진_모양이다() {
        let ts = now_iso();
        assert_eq!(ts.len(), 19, "YYYY-MM-DD HH:MM:SS = 19글자여야 한다: {}", ts);
        assert!(!ts.contains('T'), "ISO 의 T 가 다시 들어왔다: {}", ts);
        assert!(!ts.contains('+'), "시간대 오프셋이 다시 들어왔다: {}", ts);
        let b = ts.as_bytes();
        assert_eq!(b[4], b'-');
        assert_eq!(b[7], b'-');
        assert_eq!(b[10], b' ');
        assert_eq!(b[13], b':');
        assert_eq!(b[16], b':');
    }

    //--------------------------------------------------------------
    // v2 를 v3 로 올려도 판정에 쓰이는 값이 그대로다
    //=> 엔진이 읽는 칸은 grade 와 doctype 둘뿐이다. 이 둘과 원본 지문(hash)이
    //   그대로면 분류 결과는 바뀔 수 없다. 파이썬 판 시험과 같은 계약이다.
    //--------------------------------------------------------------
    #[test]
    fn v2를_v3로_올려도_판정값이_같다() {
        let old = json!({
            "v": 2, "file": "d:/s/a.doc", "grade": "O",
            "labels": {"security": "O", "doctype": ["DC_1"]},
            "axes": {
                "security": {"state": "active", "value": "O", "source": "phase4",
                             "approved_by": "고봉수", "ts": "2026-08-28T13:50:19+09:00"},
                "doctype": {"state": "active", "value": ["DC_1"], "source": "phase6",
                            "approved_by": "고봉수", "ts": "2026-08-28T13:50:20+09:00",
                            "note": "본보기"}
            },
            "doc": {"hash": "aa11", "size": 38400, "mtime": "2022-12-22T12:10:36+09:00"},
            "embed": {"dim": 3}, "vector": [0.1, 0.2, 0.3]
        });
        let n = ensure_v3(&old);
        assert_eq!(n["v"], 3);
        assert_eq!(n["grade"], "O");
        assert_eq!(n["doctype"], json!(["DC_1"]));
        assert_eq!(n["hash"], "aa11");
        assert_eq!(n["source"], "phase4+phase6");   // 축별 출처를 이어 붙인다
        assert_eq!(n["note"], "본보기");
        assert_eq!(n["ts"], "2026-08-28T13:50:20+09:00");  // 가장 늦은 시각
        // 중첩 칸은 남지 않는다 — 남으면 "어느 쪽이 진실인가"가 다시 생긴다.
        for k in ["axes", "labels", "doc", "embed"] {
            assert!(n.get(k).is_none(), "{} 가 남았다", k);
        }
        // size·mtime 은 읽는 코드가 없어 뺐다. engine 은 지어내지 않는다.
        assert!(n.get("size").is_none() && n.get("mtime").is_none());
        assert!(n.get("engine").is_none());
    }

    //--------------------------------------------------------------
    // v2 의 suspect 축은 hold 로 옮겨져 엔진에 안 보인다
    //=> 값 칸이 비면 엔진이 자동으로 그 축을 기준에서 뺀다 — 엔진을 고칠 필요가
    //   없다는 것이 v3 hold 방식의 핵심이다.
    //--------------------------------------------------------------
    #[test]
    fn suspect_축은_hold_로_옮겨진다() {
        let old = json!({
            "v": 2, "file": "a.doc",
            "axes": {"security": {"state": "suspect", "value": "C", "reason": "stale"}},
            "vector": [0.1]
        });
        let n = ensure_v3(&old);
        assert!(n.get("grade").is_none());          // 엔진이 못 본다
        assert_eq!(n["hold"]["grade"]["value"], "C");   // 값은 남아 있다
        assert_eq!(n["hold"]["grade"]["reason"], "stale");
        assert!(keepable(&n));                       // 줄은 남긴다(사람이 확인해야 한다)
    }

    //--------------------------------------------------------------
    // v2 의 retired 축은 버린다 — v3 에서 해제는 곧 삭제다
    //=> 이전 값은 class_seed_audit.jsonl 이 갖고 있다.
    //--------------------------------------------------------------
    #[test]
    fn retired_축은_버려진다() {
        let old = json!({
            "v": 2, "file": "a.doc",
            "axes": {"security": {"state": "retired", "value": "C"}},
            "vector": [0.1]
        });
        let n = ensure_v3(&old);
        assert!(n.get("grade").is_none());
        assert!(!keepable(&n));      // 남길 이유가 없는 줄이다
    }

    //--------------------------------------------------------------
    // 칸 차례가 파이썬 판과 같다
    //=> 두 판이 같은 파일에 쓰므로, 차례가 갈리면 사람이 두 줄을 나란히 놓고
    //   비교할 수 없고 회귀 시험도 못 한다.
    //--------------------------------------------------------------
    #[test]
    fn 칸_차례가_정해진_대로다() {
        let mut rows = vec![];
        set_axis(&mut rows, "a.doc", "security", json!("C"), "고봉수",
                 "cli", "메모", Some(&vec![1.0f32, 0.0]), Some("h1"));
        let o = ordered(&rows[0]);
        let keys: Vec<&String> = o.as_object().unwrap().keys().collect();
        assert_eq!(keys.iter().map(|s| s.as_str()).collect::<Vec<_>>(),
                   vec!["v", "file", "hash", "grade", "source", "approved_by",
                        "note", "ts", "dim", "engine", "vector"]);
        assert_eq!(o["engine"], "rust");
    }

    //--------------------------------------------------------------
    // 축을 두 번 확정하면 출처가 이어 붙고 값이 갈아 끼워진다
    //=> "어디서 들어온 씨앗인가"를 나중에 세려면 출처가 살아 있어야 한다.
    //--------------------------------------------------------------
    #[test]
    fn 출처가_이어_붙는다() {
        let mut rows = vec![json!({"v": 3, "file": "a.doc", "grade": "O",
                                   "source": "phase4"})];
        set_axis(&mut rows, "a.doc", "doctype", json!(["DC_1"]), "x",
                 "cli", "", None, None);
        assert_eq!(rows[0]["source"], "phase4+cli");
        assert_eq!(rows[0]["grade"], "O");       // 다른 축은 그대로
        assert_eq!(rows[0]["doctype"], json!(["DC_1"]));
        // 같은 출처를 또 줘도 중복되지 않는다.
        set_axis(&mut rows, "a.doc", "security", json!("C"), "x", "cli", "", None, None);
        assert_eq!(rows[0]["source"], "phase4+cli");
    }

    //--------------------------------------------------------------
    // 모르는 미래 판은 건너뛰고 세어서 알린다
    //=> 추측해서 읽으면 기준 문서를 잘못 해석한다. 그렇다고 조용히 건너뛰면
    //   "기준 문서가 왜 줄었지"를 아무도 알 수 없다.
    //--------------------------------------------------------------
    #[test]
    fn 모르는_미래판은_건너뛰고_센다() {
        let dir = std::env::temp_dir().join(format!("cso_seed_t{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let p = dir.join("class_seed.jsonl");
        let body = "{\"v\":3,\"file\":\"a.doc\",\"grade\":\"C\",\"vector\":[1.0]}\n\
                    {\"v\":9,\"file\":\"b.doc\",\"grade\":\"S\"}\n\
                    {깨진 줄\n";
        fs::write(&p, body).unwrap();
        let (rows, skipped) = load(p.to_str().unwrap());
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["file"], "a.doc");
        assert_eq!(skipped, 2);
        let _ = fs::remove_dir_all(&dir);
    }
}
