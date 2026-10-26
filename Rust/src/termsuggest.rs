//! 업무분류 규칙의 '단어 제안' — 순수 로직(파이썬 termsuggest.py 포팅).
//!
//! 사람이 확정한 문서에서 "이 분류에만 유독 자주 나오는 말"을 뽑아 관리자에게
//! 후보로 내민다. 규칙 파일은 이 모듈이 고치지 않는다 — 고르는 것도, 넣는 것도
//! 사람의 일이다(설계서 plan/업무분류-규칙단어-제안기능-설계-20260916.html).
//!
//! [빈도가 아니라 대조] "많이 나오는가"가 아니라 "여기에만 나오는가"를 묻는다.
//! 다른 분류에서의 출현율(df_neg)을 분모 쪽에 둔다.
//!
//! [파이썬 판과 같아야 한다] 같은 재료를 주면 후보·차례·표시 값이 글자까지 같아야
//! 한다. 상수·문턱·반올림 자리까지 파이썬 termsuggest.py 를 그대로 옮겼다.
//! 화면이 쓰는 부분(고른 말을 규칙에 넣기·금지 목록 쓰기·변경 기록·문서 한 건
//! 기준 제안)은 이 판에 옮기지 않았다 — CLI 는 '보여 주기'만 한다.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::path::Path;

use once_cell::sync::Lazy;
use regex::Regex;
use serde_json::Value;

use crate::docvocab;

// ── 재료 요건 ────────────────────────────────────────────────────────
/// 이 수보다 확정 문서가 적으면 제안하지 않는다.
pub const MIN_DOCS: usize = 3;
/// 이 수를 넘어야 '근거가 단단하다'로 보고 기본 체크를 켠다.
pub const SOLID_DOCS: usize = 5;

// ── 채택 문턱 ────────────────────────────────────────────────────────
/// 근거 하한 — 그 말이 나온 문서 수와 폴더 수.
pub const MIN_TERM_DOCS: usize = 3;
pub const MIN_TERM_CLUSTERS: usize = 2;
/// df_neg: 다른 분류 문서에 얼마나 나오는가(유일한 '배타성' 문턱).
pub const DF_NEG_MAX: f64 = 0.05;

// ── 끝말 밖 묶음 ─────────────────────────────────────────────────────
pub const LOOSE_DF_NEG_MAX: f64 = 0.02;
pub const LOOSE_DF_POS_MIN: f64 = 0.50;

// ── 한 폴더 전용 묶음 ────────────────────────────────────────────────
pub const CLUSTER_ONLY_MIN_DOCS: usize = 5;
pub const CLUSTER_ONLY_DF_POS: f64 = 0.90;
pub const CLUSTER_ONLY_DF_NEG: f64 = 0.02;
pub const CLUSTER_ONLY_MIN_LEN: usize = 3;

/// 문턱을 못 넘은 말을 '참고'로 보여 줄 최대 개수. 후보도 한 폴더 전용도 하나도
/// 없을 때만 내보낸다 — 화면이 조용해야 할 자리에 긴 목록을 쏟지 않기 위해서다.
///
/// [왜 보여 주나] 확정 문서가 있는데도 "제안할 말이 없습니다" 한 줄만 나오면,
/// 관리자는 '뽑을 말이 없는 것'인지 '문턱에 걸린 것'인지 구분할 수 없다. 기계가
/// 자를 수 없는 판단(그 말이 진짜 문서 종류를 가리키는가)은 사람이 해야 한다.
pub const BELOW_MAX: usize = 15;

/// 사람이 "이 분류 아님"이라고 거절한 문서는 대조군에서 이만큼 무겁게 센다.
pub const REJECT_WEIGHT: f64 = 2.0;
/// 본문에만 나오는 말을 규칙에 넣을 때 함께 달아 줄 값.
pub const BODY_MIN_COUNT: i64 = 2;
/// 앞부분(head)으로 볼 글자 수 — doc_rule.yaml 의 defaults.head_chars 와 같다.
pub const HEAD_CHARS: usize = 400;
/// 앞부분·본문에서 뽑은 말의 최소 길이(제목·파일 이름에는 걸지 않는다).
pub const TEXT_MIN_LEN: usize = 3;

/// 조사·어미. 어절 끝에서 떼어 낸다. 긴 것부터 시도해야 '으로'가 '로'보다 먼저 걸린다.
static PARTICLES: Lazy<Vec<&'static str>> = Lazy::new(|| {
    let mut v = vec![
        "으로서", "으로써", "이라고", "에서는", "에게서", "으로", "이라", "라고", "에서",
        "에게", "한테", "부터", "까지", "처럼", "보다", "마다", "조차", "라는", "이나",
        "께서", "들의", "들을", "들이", "들은",
        "은", "는", "이", "가", "을", "를", "의", "에", "와", "과", "도", "만", "로", "랑",
        // 인용 조사 — '“동호회”라 한다' 같은 규정 문투에서 나온다.
        "라",
    ];
    // 파이썬의 sorted(key=-len) 과 같다 — 길이가 같으면 적은 차례 그대로다(안정 정렬).
    v.sort_by_key(|w| std::cmp::Reverse(w.chars().count()));
    v
});

/// 파일 이름·제목을 자를 구분자(한글 따옴표·괄호 포함).
static SPLIT_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(concat!(
        r"[\s_\-–—.,/\\()\[\]{}<>~!@#$%^&*+=:;'",
        "\"",
        r"?｜|·・“”‘’「」『』〈〉《》【】（），：；？！]+"
    ))
    .expect("SPLIT_RE")
});

/// 버릴 토큰 — 숫자·날짜·판번호처럼 문서 종류와 무관한 것.
static JUNK_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)^(v?\d+([.\-]\d+)*[가-힣a-z]*|\d{4}년?|\d+분기|\d+차|\d+회|\d+판)$")
        .expect("JUNK_RE")
});

/// 한글이 한 글자라도 들어 있는가.
static HANGUL_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"[가-힣]").expect("HANGUL_RE"));
/// 숫자가 섞인 말은 버린다(차수·연도·조항).
static DIGIT_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"\d").expect("DIGIT_RE"));

/// 파일 확장자로 흔한 것 — 파일 이름을 자르고 남는 찌꺼기를 막는다.
static EXT_WORDS: Lazy<HashSet<&'static str>> = Lazy::new(|| {
    ["doc", "docx", "hwp", "hwpx", "ppt", "pptx", "xls", "xlsx", "pdf", "txt", "md",
     "html", "htm", "jpg", "png", "zip"].into_iter().collect()
});

/// 용언 활용형의 꼬리. core 사전의 verb_endings 칸이 기준이고, 여기 값은 그 칸이
/// 없는 옛 사전이 깔렸을 때 쓰는 기본값이다.
pub const VERB_ENDINGS: &[&str] = &[
    "하면", "하고", "하며", "하여", "하는", "하지", "하기", "하도록", "하였", "해야", "해서",
    "되면", "되고", "되며", "되어", "되는", "되지", "되도록", "되어야", "돼야",
    "이며", "이고", "이는", "이면", "으면", "으며",
    "어야", "아야", "여야", "니다", "습니다", "드립니다",
    "있는", "없는", "같은", "대한", "관한", "위한", "통한", "통해", "따른", "따라",
    "면서", "지만",
    "된", "는",
];

//------------------------------------------------------------------
// 재료 문서 한 건
//=> 채점·점수 계산이 보는 칸만 담는다. 파일을 읽는 일과 말을 세는 일을 갈라
//   두어야 시험이 파일 없이 돈다.
//------------------------------------------------------------------
#[derive(Clone, Debug, Default)]
pub struct Doc {
    pub key: String,
    /// 원본 경로(재료를 되짚을 때만 쓴다 — 점수 계산은 보지 않는다).
    #[allow(dead_code)]
    pub file: String,
    pub name: String,
    pub folder: String,
    pub cluster: String,
    pub title: String,
    pub head: String,
    pub body: String,
    pub labels: BTreeSet<String>,
    pub rejected: BTreeSet<String>,
    pub has_text: bool,
}

/// 금지 목록(doc_rule_stopwords.yaml)을 읽은 결과.
#[derive(Clone, Debug, Default)]
pub struct Stopwords {
    pub global: HashSet<String>,
    pub by_node: HashMap<String, HashSet<String>>,
}

/// 후보 한 개.
#[derive(Clone, Debug)]
pub struct Cand {
    pub term: String,
    pub fields: Vec<&'static str>,
    pub min_count: Option<i64>,
    pub checked: bool,
    pub where_: &'static str,
    pub group: &'static str,
    pub df_pos: f64,
    pub df_neg: f64,
    pub score: f64,
    pub docs: usize,
    pub clusters: usize,
    pub flags: Vec<String>,
}

/// 문턱을 못 넘은 말 한 개(참고용) — 왜 떨어졌는지를 함께 들고 있다.
#[derive(Clone, Debug)]
pub struct Below {
    pub term: String,
    pub where_: &'static str,
    pub why: String,
    pub df_pos: f64,
    pub df_neg: f64,
    pub score: f64,
    pub docs: usize,
    pub clusters: usize,
    pub flags: Vec<String>,
}

/// 분류 하나에 대한 제안 결과.
#[derive(Clone, Debug, Default)]
pub struct Suggestion {
    pub node: String,
    pub docs: usize,
    pub clusters: usize,
    pub no_text: usize,
    pub reason: String,
    pub candidates: Vec<Cand>,
    pub cluster_only: Vec<Cand>,
    /// 문턱을 못 넘은 말(참고). 후보도 한 폴더 전용도 비었을 때만 채운다.
    pub below: Vec<Below>,
}

//------------------------------------------------------------------
// 경로 정규화 (비교용)
//=> 같은 문서를 가리키는 경로가 구분자(\ vs /)·대소문자만 달라도 한 문서로 잇는다.
//
// -in: path = 파일 경로
// -out: String = 비교용으로 정규화한 경로(빈 값이면 빈 문자열)
//------------------------------------------------------------------
pub fn norm_key(path: &str) -> String {
    if path.is_empty() {
        return String::new();
    }
    let s = path.replace('\\', "/");
    let s = s.trim_end_matches('/');
    if s.is_empty() { "/".to_string() } else { s.to_lowercase() }
}

//------------------------------------------------------------------
// 문서가 속한 '묶음'(폴더) 구하기
//=> 같은 폴더 문서는 대개 한꺼번에 들어온 것이라 어휘가 통째로 같다. 여러 건으로
//   세면 그 폴더의 말버릇이 규칙이 된다.
//
// -in: path = 파일 경로
// -out: String = 부모 폴더(정규화). 없으면 문서 자체를 한 묶음으로 본다
//------------------------------------------------------------------
pub fn cluster_of(path: &str) -> String {
    let key = norm_key(path);
    match key.rfind('/') {
        Some(0) => "/".to_string(),
        Some(i) => key[..i].to_string(),
        None => key,
    }
}

//------------------------------------------------------------------
// 경로에서 파일 이름·부모 폴더 떼기(원본 표기 그대로)
//=> 파이썬 os.path.basename/dirname 과 같게, 두 구분자를 모두 본다.
//
// -in: path = 파일 경로
// -out: (String, String) = (파일 이름, 부모 폴더)
//------------------------------------------------------------------
fn split_path(path: &str) -> (String, String) {
    match path.rfind(['/', '\\']) {
        Some(i) => (path[i + 1..].to_string(), path[..i].to_string()),
        None => (path.to_string(), String::new()),
    }
}

//------------------------------------------------------------------
// 기준 문서 저장소에서 확정 라벨 읽기 (class_seed.jsonl)
//=> 사람이 승인한 줄만 재료로 삼는다 — 규칙이 만든 라벨이 다시 규칙을 만드는
//   고리를 막는다.
//
// -in: path          = class_seed.jsonl 경로
// -in: approved_only = true 면 approved_by 가 있는 줄만
//
// -out: (order, map) = 읽은 차례, {정규화경로: (labels, hash, file)}
// -out: error = 파일이 없으면 빈 결과(오류가 아니다)
//------------------------------------------------------------------
type SeedRow = (BTreeSet<String>, Option<String>, String);

pub fn load_seed_labels(path: Option<&Path>, approved_only: bool)
    -> (Vec<String>, HashMap<String, SeedRow>) {
    let mut order: Vec<String> = vec![];
    let mut out: HashMap<String, SeedRow> = HashMap::new();
    let p = match path { Some(p) if p.is_file() => p, _ => return (order, out) };
    let text = match std::fs::read_to_string(p) { Ok(t) => t, Err(_) => return (order, out) };
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() { continue; }
        // 깨진 줄 하나 때문에 제안 전체가 멈추면 안 된다.
        let row: Value = match serde_json::from_str(line) { Ok(v) => v, Err(_) => continue };
        let labels: Vec<String> = row.get("doctype").and_then(|v| v.as_array())
            .map(|a| a.iter().filter_map(|x| x.as_str()).filter(|s| !s.is_empty())
                 .map(|s| s.to_string()).collect())
            .unwrap_or_default();
        if labels.is_empty() { continue; }
        if approved_only && row.get("approved_by").and_then(|v| v.as_str()).unwrap_or("").is_empty() {
            continue;
        }
        let file = row.get("file").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let key = norm_key(&file);
        if key.is_empty() { continue; }
        let hash = row.get("hash").and_then(|v| v.as_str()).map(|s| s.to_string());
        let entry = out.entry(key.clone()).or_insert_with(|| {
            order.push(key.clone());
            (BTreeSet::new(), hash.clone(), file.clone())
        });
        entry.0.extend(labels);
    }
    (order, out)
}

//------------------------------------------------------------------
// 검토 화면의 확정·거절 이력 읽기 (cso_override.jsonl 의 doctype 축)
//=> 같은 문서에 여러 번 결정했으면 마지막 것이 진실이다(append-only 파일).
//   rejected("이 분류 아님")도 가져온다 — 대조군을 강하게 만든다.
//
// -in: path = cso_override.jsonl 경로
//
// -out: (order, map) = 읽은 차례, {정규화경로: (confirmed, rejected, doc_id, file)}
// -out: error = 파일이 없으면 빈 결과
//------------------------------------------------------------------
type OvRow = (BTreeSet<String>, BTreeSet<String>, Option<String>, String);

pub fn load_override_labels(path: Option<&Path>) -> (Vec<String>, HashMap<String, OvRow>) {
    let mut order: Vec<String> = vec![];
    let mut latest: HashMap<String, OvRow> = HashMap::new();
    let p = match path { Some(p) if p.is_file() => p, _ => return (order, latest) };
    let text = match std::fs::read_to_string(p) { Ok(t) => t, Err(_) => return (order, latest) };
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() { continue; }
        let row: Value = match serde_json::from_str(line) { Ok(v) => v, Err(_) => continue };
        if row.get("axis").and_then(|v| v.as_str()) != Some("doctype") { continue; }
        let file = row.get("file").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let key = norm_key(&file);
        if key.is_empty() { continue; }
        let pick = |name: &str| -> BTreeSet<String> {
            row.get(name).and_then(|v| v.as_array())
                .map(|a| a.iter().filter_map(|x| x.as_str()).filter(|s| !s.is_empty())
                     .map(|s| s.to_string()).collect())
                .unwrap_or_default()
        };
        if !latest.contains_key(&key) { order.push(key.clone()); }
        // 나중 줄이 앞 줄을 덮는다 — 마지막 결정만 남긴다.
        latest.insert(key, (pick("confirmed"), pick("rejected"),
                            row.get("doc_id").and_then(|v| v.as_str()).map(|s| s.to_string()),
                            file));
    }
    (order, latest)
}

//------------------------------------------------------------------
// 금지 목록 읽기 (doc_rule_stopwords.yaml)
//=> "이 말은 오탐이라 뺐다"는 지식을 파일로 남겨 같은 말이 되살아나지 않게 한다.
//
// -in: path = doc_rule_stopwords.yaml 경로(없어도 된다)
//
// -out: Stopwords = 전체 금지 말·분류별 금지 말
// -out: error = 파일 없음·파싱 실패면 빈 목록(예외 없음)
//------------------------------------------------------------------
pub fn load_stopwords(path: Option<&Path>) -> Stopwords {
    let mut out = Stopwords::default();
    let p = match path { Some(p) if p.is_file() => p, _ => return out };
    let text = match std::fs::read_to_string(p) { Ok(t) => t, Err(_) => return out };
    let data: Value = match serde_yaml::from_str(&text) { Ok(v) => v, Err(_) => return out };
    let map = match data.as_object() { Some(m) => m, None => return out };
    if let Some(a) = map.get("global").and_then(|v| v.as_array()) {
        for w in a {
            let w = value_str(w);
            if !w.is_empty() { out.global.insert(w); }
        }
    }
    if let Some(m) = map.get("by_node").and_then(|v| v.as_object()) {
        for (node, words) in m {
            if let Some(a) = words.as_array() {
                let got: HashSet<String> = a.iter().map(value_str)
                    .filter(|w| !w.is_empty()).collect();
                if !got.is_empty() { out.by_node.insert(node.to_string(), got); }
            }
        }
    }
    out
}

/// YAML/JSON 값 하나를 파이썬 str(x).strip() 과 같게 글로 바꾼다.
fn value_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.trim().to_string(),
        Value::Null => "None".to_string(),
        other => other.to_string().trim().to_string(),
    }
}

//------------------------------------------------------------------
// 저장해 둔 추출 본문 읽기 (--textsave 폴더)
//=> 파일 이름이 원본의 SHA-256 이라 결과 레코드의 hash 로 바로 찾아진다.
//   원본을 다시 열지 않는다 — 원본은 지워지거나 바뀔 수 있다.
//
// -in: text_dir = 추출 본문 폴더(None 이면 아무것도 안 읽는다)
// -in: sha      = 문서의 SHA-256
//
// -out: Option<String> = 본문(없으면 None)
//------------------------------------------------------------------
pub fn read_saved_text(text_dir: Option<&Path>, sha: Option<&str>) -> Option<String> {
    let dir = text_dir?;
    let sha = sha.filter(|s| !s.is_empty())?;
    let path = dir.join(format!("{}.txt", sha));
    if !path.is_file() { return None; }
    // 깨진 글자는 파이썬 errors="replace" 와 같게 대체 문자로 바꾼다.
    std::fs::read(&path).ok().map(|b| String::from_utf8_lossy(&b).into_owned())
}

//------------------------------------------------------------------
// 저장된 본문의 색인 읽기 (--textsave 폴더의 _index.jsonl)
//=> 확정 이력에는 hash 칸이 없다. 색인이 경로·doc_id 로 hash 를 되찾아 준다.
//
// -in: text_dir = --textsave 폴더
//
// -out: (by_path, by_doc_id) = {정규화경로: hash}, {doc_id: hash}
//------------------------------------------------------------------
pub fn load_text_index(text_dir: Option<&Path>)
    -> (HashMap<String, String>, HashMap<String, String>) {
    let (mut by_path, mut by_doc) = (HashMap::new(), HashMap::new());
    let dir = match text_dir { Some(d) => d, None => return (by_path, by_doc) };
    let path = dir.join("_index.jsonl");
    if !path.is_file() { return (by_path, by_doc); }
    let text = match std::fs::read_to_string(&path) { Ok(t) => t, Err(_) => return (by_path, by_doc) };
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() { continue; }
        let row: Value = match serde_json::from_str(line) { Ok(v) => v, Err(_) => continue };
        let sha = match row.get("hash").and_then(|v| v.as_str()).filter(|s| !s.is_empty()) {
            Some(s) => s.to_string(), None => continue,
        };
        // 같은 문서를 여러 번 저장했으면 마지막 줄이 이긴다.
        let key = norm_key(row.get("file").and_then(|v| v.as_str()).unwrap_or(""));
        if !key.is_empty() { by_path.insert(key, sha.clone()); }
        if let Some(d) = row.get("doc_id").and_then(|v| v.as_str()).filter(|s| !s.is_empty()) {
            by_doc.insert(d.to_string(), sha);
        }
    }
    (by_path, by_doc)
}

//------------------------------------------------------------------
// 문서 한 건 만들기
//=> 제목은 본문의 첫 비어 있지 않은 줄로 본다 — 엔진의 제목 추출과 같은 규약이다.
//
// -in: file     = 원본 경로
// -in: labels   = 이 문서로 확정된 dc_id 들
// -in: rejected = 이 문서에서 거절된 dc_id 들
// -in: text     = 저장된 추출 본문(None 이면 제목·파일 이름만으로 참여)
//
// -out: Doc
//------------------------------------------------------------------
pub fn make_doc(file: &str, labels: BTreeSet<String>, rejected: BTreeSet<String>,
                text: Option<&str>) -> Doc {
    let (name, folder) = split_path(file);
    let body = text.unwrap_or("").to_string();
    let title = body.lines().map(|l| l.trim()).find(|l| !l.is_empty())
        .unwrap_or("").to_string();
    // 앞부분은 엔진의 표제부와 같은 규칙(공백 묶음을 한 칸으로 본 앞 n 자)으로 자른다 —
    // 다르면 제안한 낱말이 실제 판정의 표제부에서는 안 걸린다.
    let head: String = crate::doctype::head_scope(&body, HEAD_CHARS);
    Doc {
        key: norm_key(file), file: file.to_string(), name, folder,
        cluster: cluster_of(file), title, head, body,
        labels, rejected, has_text: text.map(|t| !t.is_empty()).unwrap_or(false),
    }
}

//------------------------------------------------------------------
// 재료 모으기 — seed 와 검토 확정을 한 자리에 합친다
//=> 두 저장소를 문서 단위로 잇고, 저장된 본문을 붙여 문서 목록을 만든다.
//
// -in: seed_path     = class_seed.jsonl 경로
// -in: override_path = cso_override.jsonl 경로
// -in: text_dir      = --textsave 폴더
// -in: approved_only = seed 를 사람 승인분으로 제한할지
//
// -out: Vec<Doc> = 라벨도 거절도 없는 문서는 넣지 않는다
//------------------------------------------------------------------
pub fn load_documents(seed_path: Option<&Path>, override_path: Option<&Path>,
                      text_dir: Option<&Path>, approved_only: bool) -> Vec<Doc> {
    let (seed_order, seeds) = load_seed_labels(seed_path, approved_only);
    let (ov_order, overrides) = load_override_labels(override_path);

    struct Merged {
        file: String,
        hash: Option<String>,
        doc_id: Option<String>,
        labels: BTreeSet<String>,
        rejected: BTreeSet<String>,
    }
    let mut order: Vec<String> = vec![];
    let mut merged: HashMap<String, Merged> = HashMap::new();
    for key in seed_order {
        let row = &seeds[&key];
        order.push(key.clone());
        merged.insert(key, Merged { file: row.2.clone(), hash: row.1.clone(), doc_id: None,
                                    labels: row.0.clone(), rejected: BTreeSet::new() });
    }
    for key in ov_order {
        let row = &overrides[&key];
        let cur = merged.entry(key.clone()).or_insert_with(|| {
            order.push(key.clone());
            Merged { file: row.3.clone(), hash: None, doc_id: row.2.clone(),
                     labels: BTreeSet::new(), rejected: BTreeSet::new() }
        });
        cur.labels.extend(row.0.iter().cloned());
        cur.rejected.extend(row.1.iter().cloned());
        // 확정했다가 거절한 축은 확정에서 뺀다 — 마지막 결정이 진실이다.
        for d in &row.1 { cur.labels.remove(d); }
    }

    // 확정 이력에는 hash 가 없다. 저장된 본문의 색인으로 되찾는다.
    let (by_path, by_doc) = load_text_index(text_dir);

    let mut docs = vec![];
    for key in order {
        let row = &merged[&key];
        if row.labels.is_empty() && row.rejected.is_empty() { continue; }
        let sha = row.hash.clone()
            .or_else(|| by_path.get(&key).cloned())
            .or_else(|| row.doc_id.as_ref().and_then(|d| by_doc.get(d).cloned()));
        let text = read_saved_text(text_dir, sha.as_deref());
        docs.push(make_doc(&row.file, row.labels.clone(), row.rejected.clone(), text.as_deref()));
    }
    docs
}

//------------------------------------------------------------------
// 끝말로 끝나는가
//=> 끝말은 길이가 두세 가지뿐이라, 그 길이만큼 잘라 집합에서 찾으면 빠르다.
//
// -in: word     = 검사할 말
// -in: suffixes = 끝말 목록(비면 내장 DOC_SUFFIXES)
//
// -out: bool
//------------------------------------------------------------------
pub fn ends_with_suffix(word: &str, suffixes: &SuffixIndex) -> bool {
    let chars: Vec<char> = word.chars().collect();
    for &n in &suffixes.lens {
        if chars.len() > n {
            let tail: String = chars[chars.len() - n..].iter().collect();
            if suffixes.table.contains(&tail) { return true; }
        }
        if chars.len() == n && suffixes.table.contains(word) { return true; }
    }
    false
}

/// 끝말 목록을 '집합 + 길이들'로 바꿔 둔 것(파이썬 _suffix_index 와 같은 일).
pub struct SuffixIndex {
    pub table: HashSet<String>,
    pub lens: Vec<usize>,
}

//------------------------------------------------------------------
// 끝말 목록 준비
//=> 같은 목록으로 수만 번 묻게 되므로 한 번만 만들어 두고 다시 쓴다.
//
// -in: suffixes = 끝말 목록(비면 내장 DOC_SUFFIXES)
//
// -out: SuffixIndex
//------------------------------------------------------------------
pub fn suffix_index(suffixes: &[String]) -> SuffixIndex {
    let table: HashSet<String> = if suffixes.is_empty() {
        docvocab::DOC_SUFFIXES.iter().map(|s| s.to_string()).collect()
    } else {
        suffixes.iter().cloned().collect()
    };
    let mut lens: Vec<usize> = table.iter().map(|w| w.chars().count()).collect();
    lens.sort_unstable();
    lens.dedup();
    lens.reverse();
    SuffixIndex { table, lens }
}

//------------------------------------------------------------------
// 어절에서 조사·어미 떼기
//=> '품목보고서를' → '품목보고서'. 이미 끝말로 끝나면 건드리지 않는다.
//
// -in: word     = 어절(구두점은 미리 제거된 상태)
// -in: suffixes = 끝말 목록
//
// -out: String = 조사를 뗀 말(뗄 것이 없으면 그대로)
//------------------------------------------------------------------
pub fn strip_particle(word: &str, suffixes: &SuffixIndex) -> String {
    let word = word.trim();
    if word.is_empty() { return String::new(); }
    // '계약서'의 '서'를 조사로 오해하지 않게 한다.
    if ends_with_suffix(word, suffixes) { return word.to_string(); }
    let n = word.chars().count();
    for par in PARTICLES.iter() {
        let pn = par.chars().count();
        if word.ends_with(par) && n >= pn + 2 {
            return word.chars().take(n - pn).collect();
        }
    }
    word.to_string()
}

//------------------------------------------------------------------
// 말 한 개가 후보가 될 자격이 있는가
//=> 숫자·날짜·판번호·확장자·서술어·활용형·너무 짧은 말을 걸러낸다.
//
// -in: term    = 후보 말
// -in: endings = 활용형 꼬리 목록(비면 내장 VERB_ENDINGS)
//
// -out: bool
//------------------------------------------------------------------
pub fn is_usable(term: &str, endings: &[String]) -> bool {
    let term = term.trim();
    if term.is_empty() || JUNK_RE.is_match(term) { return false; }
    if EXT_WORDS.contains(term.to_lowercase().as_str()) { return false; }
    if DIGIT_RE.is_match(term) { return false; }
    if HANGUL_RE.is_match(term) {
        // 서술어('정한다')는 문서 종류를 가리키는 말이 아니다.
        if term.ends_with('다') { return false; }
        let hit = if endings.is_empty() {
            VERB_ENDINGS.iter().any(|e| term.ends_with(e))
        } else {
            endings.iter().any(|e| term.ends_with(e.as_str()))
        };
        if hit { return false; }
        // 한글은 한 글자면 뜻이 너무 넓다('서'·'안'·'표').
        return term.chars().count() >= 2;
    }
    term.chars().count() >= 3 && term.chars().any(|c| c.is_alphabetic())
}

//------------------------------------------------------------------
// 구분자로 잘라 말 뽑기 (파일 이름·제목용)
//
// -in: text     = 파일 이름 또는 제목
// -in: suffixes = 끝말 목록(조사 떼기에 쓴다)
// -in: drop_ext = true 면 맨 끝 확장자를 먼저 떼어 낸다
// -in: endings  = 활용형 꼬리 목록
//
// -out: BTreeSet<String> = 뽑힌 말들
//------------------------------------------------------------------
pub fn split_words(text: &str, suffixes: &SuffixIndex, drop_ext: bool,
                   endings: &[String]) -> BTreeSet<String> {
    let mut text = text.trim().to_string();
    if text.is_empty() { return BTreeSet::new(); }
    if drop_ext {
        if let Some(i) = text.rfind('.') {
            let ext = text[i + 1..].to_lowercase();
            // 파이썬 splitext 는 맨 앞 점은 확장자로 보지 않는다.
            if i > 0 && !ext.is_empty() && EXT_WORDS.contains(ext.as_str()) {
                text = text[..i].to_string();
            }
        }
    }
    let mut out = BTreeSet::new();
    for tok in SPLIT_RE.split(&text) {
        let tok = strip_particle(tok.trim(), suffixes);
        if is_usable(&tok, endings) { out.insert(tok); }
    }
    out
}

//------------------------------------------------------------------
// 줄글에서 말 뽑기 (앞부분·본문용)
//=> 끝말에 걸린 말과 그 밖의 말을 갈라 돌려준다. 끝말 밖은 버리지 않되 더 죈다.
//
// -in: text     = 줄글
// -in: suffixes = 끝말 목록
// -in: endings  = 활용형 꼬리 목록
//
// -out: (in_suffix, out_suffix)
//------------------------------------------------------------------
pub fn scan_words(text: &str, suffixes: &SuffixIndex, endings: &[String])
    -> (BTreeSet<String>, BTreeSet<String>) {
    let (mut hit, mut rest) = (BTreeSet::new(), BTreeSet::new());
    // 떼기 전에 중복을 없앤다 — 본문이 길어도 서로 다른 말은 훨씬 적다.
    let uniq: HashSet<&str> = SPLIT_RE.split(text).map(|t| t.trim()).collect();
    for tok in uniq {
        let tok = strip_particle(tok, suffixes);
        if !is_usable(&tok, endings) { continue; }
        if ends_with_suffix(&tok, suffixes) { hit.insert(tok); } else { rest.insert(tok); }
    }
    (hit, rest)
}

/// 문서 한 건에서 자리별로 뽑은 말.
pub struct DocWords {
    pub name: BTreeSet<String>,
    pub title: BTreeSet<String>,
    pub head: BTreeSet<String>,
    pub body: BTreeSet<String>,
    pub loose: BTreeSet<String>,
}

impl DocWords {
    fn get(&self, where_: &str) -> &BTreeSet<String> {
        match where_ {
            "name" => &self.name,
            "title" => &self.title,
            "head" => &self.head,
            _ => &self.body,
        }
    }
    fn any(&self, term: &str) -> bool {
        self.name.contains(term) || self.title.contains(term) || self.head.contains(term)
            || self.body.contains(term) || self.loose.contains(term)
    }
}

//------------------------------------------------------------------
// 문서 한 건에서 자리별로 말 뽑기
//=> 같은 말이라도 어디서 나왔느냐에 따라 규칙의 어느 칸에 넣을지가 달라진다.
//   제목이 이기도록 앞부분·본문에서 제목·파일 이름의 말은 빼 둔다.
//
// -in: doc      = 재료 문서
// -in: suffixes = 끝말 목록
// -in: endings  = 활용형 꼬리 목록
//
// -out: DocWords
//------------------------------------------------------------------
pub fn doc_words(doc: &Doc, suffixes: &SuffixIndex, endings: &[String]) -> DocWords {
    let name = split_words(&doc.name, suffixes, true, endings);
    let title = split_words(&doc.title, suffixes, false, endings);
    let (head_hit, head_rest) = scan_words(&doc.head, suffixes, endings);
    let (body_hit, body_rest) = scan_words(&doc.body, suffixes, endings);
    let sub = |a: BTreeSet<String>, b: &BTreeSet<String>| -> BTreeSet<String> {
        a.into_iter().filter(|w| !b.contains(w)).collect()
    };
    let head_all: BTreeSet<String> = head_hit.union(&head_rest).cloned().collect();
    let head = sub(sub(head_all, &title), &name);
    let body_all: BTreeSet<String> = body_hit.union(&body_rest).cloned().collect();
    let body = sub(sub(sub(body_all, &title), &head), &name);
    // 끝말 밖 표식은 앞부분·본문에서 뽑은 말에만 단다.
    let loose_all: BTreeSet<String> = head_rest.union(&body_rest).cloned().collect();
    let loose = sub(sub(loose_all, &title), &name);
    DocWords { name, title, head, body, loose }
}

//------------------------------------------------------------------
// 묶음(폴더) 가중치 만들기
//=> 같은 폴더 문서들이 합쳐서 1건이 되도록 1/(그 폴더의 문서 수) 를 준다.
//
// -in: docs = 문서 목록
//
// -out: (weights, clusters) = {문서 key: 가중치}, 폴더 수
//------------------------------------------------------------------
pub fn cluster_weights(docs: &[&Doc]) -> (HashMap<String, f64>, usize) {
    let mut size: HashMap<&str, usize> = HashMap::new();
    for d in docs { *size.entry(d.cluster.as_str()).or_insert(0) += 1; }
    let mut weights = HashMap::new();
    for d in docs {
        weights.insert(d.key.clone(), 1.0 / size[d.cluster.as_str()] as f64);
    }
    (weights, size.len())
}

//------------------------------------------------------------------
// 규칙 파일에서 이미 쓰고 있는 말 모으기
//=> 중복 제안을 막고, 다른 분류 규칙과 겹치는 말을 가려낸다. 대소문자만 다른
//   말도 같은 말로 본다.
//
// -in: doc = doc_rule.yaml 을 읽은 값(없으면 빈 결과)
//
// -out: (by_node, owner) = {dc_id: {소문자 말}}, {소문자 말: dc_id}(먼저 나온 쪽이 임자)
//------------------------------------------------------------------
pub fn rule_terms(doc: Option<&Value>)
    -> (HashMap<String, HashSet<String>>, HashMap<String, String>) {
    let (mut by_node, mut owner) = (HashMap::new(), HashMap::new());
    let rules = doc.and_then(|d| d.get("doctype_rules")).and_then(|v| v.as_array());
    for rule in rules.map(|a| a.as_slice()).unwrap_or(&[]) {
        let node = match rule.get("node").and_then(|v| v.as_str()).filter(|s| !s.is_empty()) {
            Some(n) => n.to_string(), None => continue,
        };
        let got: &mut HashSet<String> = by_node.entry(node.clone()).or_default();
        for cell in ["title_terms", "head_terms", "terms", "filename"] {
            for word in rule.get(cell).and_then(|v| v.as_array()).map(|a| a.as_slice()).unwrap_or(&[]) {
                let low = value_str(word).to_lowercase();
                if low.is_empty() { continue; }
                got.insert(low.clone());
                owner.entry(low).or_insert_with(|| node.clone());
            }
        }
    }
    (by_node, owner)
}

//------------------------------------------------------------------
// 분류 체계에서 '다른 분류 이름' 모으기
//=> 계약서 문서에서 '회의록'이 뽑히면 두 규칙이 서로를 먹는다.
//
// -in: tax  = {dc_id: 분류 이름}
// -in: node = 지금 제안하는 분류(이 분류 이름은 빼지 않는다)
//
// -out: HashSet<String> = 소문자로 맞춘 다른 분류 이름들
//------------------------------------------------------------------
pub fn other_titles(tax: &HashMap<String, String>, node: &str) -> HashSet<String> {
    let mut out = HashSet::new();
    for (dc_id, title) in tax {
        if dc_id == node { continue; }
        let t = title.trim();
        if !t.is_empty() { out.insert(t.to_lowercase()); }
    }
    out
}

//------------------------------------------------------------------
// 고유명사 의심 판정
//=> 회사명·제품명·인명은 대개 그 폴더에서만 통하는 말이라 과적합이 된다.
//   막지 않고 표식만 단다 — 관리자가 한 번 더 생각하게 하는 것이 목적이다.
//
// -in: term     = 후보 말
// -in: docs     = 그 분류의 확정 문서들(폴더 이름을 본다)
// -in: suffixes = 끝말 목록
//
// -out: bool
//------------------------------------------------------------------
pub fn looks_proper_noun(term: &str, docs: &[&Doc], suffixes: &SuffixIndex) -> bool {
    // 끝말로 끝나는 말은 문서 종류를 가리키는 말이다.
    if ends_with_suffix(term, suffixes) { return false; }
    // 영문 대문자만으로 된 말(EZis·SCPI)은 거의 제품·규격 이름이다.
    if !HANGUL_RE.is_match(term) && is_upper(term) { return true; }
    let low = term.to_lowercase();
    docs.iter().any(|d| d.folder.to_lowercase().contains(&low))
}

/// 파이썬 str.isupper() 와 같다 — 소문자가 하나도 없고 대문자가 하나는 있어야 한다.
fn is_upper(s: &str) -> bool {
    s.chars().any(|c| c.is_uppercase()) && !s.chars().any(|c| c.is_lowercase())
}

//------------------------------------------------------------------
// 말 하나의 자리 → 규칙의 어느 칸에 넣을지
//=> 제목·파일 이름 → title_terms + filename (기본 체크 켬)
//   앞부분에서만   → head_terms            (기본 체크 끔)
//   본문에서만     → terms + min_count 2   (기본 체크 끔)
//
// -in: where_ = 그 말이 처음 나온 자리
//
// -out: (fields, min_count, checked)
//------------------------------------------------------------------
pub fn placement(where_: &str) -> (Vec<&'static str>, Option<i64>, bool) {
    match where_ {
        "title" | "name" => (vec!["title_terms", "filename"], None, true),
        "head" => (vec!["head_terms"], None, false),
        _ => (vec!["terms"], Some(BODY_MIN_COUNT), false),
    }
}

//------------------------------------------------------------------
// 대조 점수 (로그 오즈)
//=> "이 분류에 많이 나오는가"가 아니라 "이 분류에만 나오는가"를 숫자로 만든다.
//   문턱이 아니라 줄 세우기에만 쓴다.
//
// -in: df_pos = 이 분류 문서에서의 출현율(0~1)
// -in: df_neg = 다른 분류 문서에서의 출현율(0~1)
// -in: n_pos  = 이 분류의 묶음 무게 합
// -in: n_neg  = 대조군의 묶음 무게 합
//
// -out: f64 = 로그 오즈
//------------------------------------------------------------------
pub fn log_odds(df_pos: f64, df_neg: f64, n_pos: f64, n_neg: f64) -> f64 {
    let a = df_pos + 0.5 / n_pos.max(1.0);
    let b = df_neg + 0.5 / n_neg.max(1.0);
    (a / b).ln()
}

/// 파이썬 round(x, n) 과 같은 자리에서 끊는다(짝수 쪽으로 붙는 반올림까지 같다).
fn round_to(x: f64, n: usize) -> f64 {
    format!("{:.*}", n, x).parse().unwrap_or(x)
}

//------------------------------------------------------------------
// 분류 하나에 대한 단어 제안
//=> 확정 문서에서 말을 뽑고, 대조 점수로 거른 뒤, 넣을 칸까지 정해 후보 목록을
//   만든다. 규칙 파일은 건드리지 않는다.
//    1) 재료를 가른다 — 이 분류로 확정된 문서(D+) / 나머지와 거절분(D−)
//    2) 문서 수가 모자라면 왜 비었는지 말하고 끝낸다
//    3) 자리별로 말을 뽑고, 폴더 가중치로 출현율을 센다
//    4) 문턱을 넘은 말만 남기고, 넣으면 안 되는 말을 거른다
//    5) 한 폴더에 몰린 어휘는 '한 폴더 전용' 묶음으로 따로 담는다
//
// -in: node      = 제안할 분류의 dc_id
// -in: docs      = 재료 문서 목록
// -in: rule_doc  = doc_rule.yaml 을 읽은 값(이미 있는 말을 빼려고 본다)
// -in: stopwords = 금지 목록
// -in: tax       = {dc_id: 분류 이름}(다른 분류 이름을 빼려고 본다)
// -in: suffixes  = 끝말 목록
// -in: endings   = 활용형 꼬리 목록
// -in: min_docs  = 제안을 시작할 최소 확정 문서 수
//
// -out: Suggestion = 재료가 모자라면 candidates 가 비고 reason 에 까닭이 담긴다
//------------------------------------------------------------------
#[allow(clippy::too_many_arguments)]
pub fn suggest(node: &str, docs: &[Doc], rule_doc: Option<&Value>, stopwords: &Stopwords,
               tax: &HashMap<String, String>, suffixes: &SuffixIndex, endings: &[String],
               min_docs: usize) -> Suggestion {
    let pos: Vec<&Doc> = docs.iter().filter(|d| d.labels.contains(node)).collect();
    let neg: Vec<&Doc> = docs.iter().filter(|d| !d.labels.contains(node)).collect();
    // 사람이 "이 분류 아님"이라고 콕 집은 문서는 더 강한 반례다.
    let rejected: Vec<&Doc> = docs.iter().filter(|d| d.rejected.contains(node)).collect();

    let (pos_w, pos_clusters) = cluster_weights(&pos);
    let (mut neg_w, _) = cluster_weights(&neg);
    // 무게 합의 차례까지 파이썬과 같게 — 대조군 문서 차례 뒤에 거절분만 새로 붙는다.
    let mut neg_order: Vec<String> = neg.iter().map(|d| d.key.clone()).collect();
    neg_order.dedup();
    for d in &rejected {
        let cur = neg_w.get(&d.key).copied().unwrap_or(1.0) * REJECT_WEIGHT;
        if !neg_w.contains_key(&d.key) { neg_order.push(d.key.clone()); }
        neg_w.insert(d.key.clone(), cur);
    }

    let no_text = pos.iter().filter(|d| !d.has_text).count();
    let mut out = Suggestion {
        node: node.to_string(), docs: pos.len(), clusters: pos_clusters,
        no_text, ..Default::default()
    };
    if pos.len() < min_docs {
        out.reason = format!("확정 문서 {}건 — {}건부터 제안합니다", pos.len(), min_docs);
        return out;
    }

    let mut seen_key: HashSet<&str> = HashSet::new();
    let mut n_pos: f64 = pos.iter().filter(|d| seen_key.insert(d.key.as_str()))
        .map(|d| pos_w[&d.key]).sum();
    if n_pos == 0.0 { n_pos = 1.0; }
    let mut n_neg: f64 = neg_order.iter().map(|k| neg_w[k]).sum();
    if n_neg == 0.0 { n_neg = 1.0; }

    // ── 자리별로 말을 세어 모은다 ────────────────────────────────────
    // first_where: 그 말이 '가장 센 자리'에서 나왔는지 기억한다(제목 > 앞부분 > 본문).
    let order = ["title", "name", "head", "body"];
    let mut pos_hits: BTreeMap<String, f64> = BTreeMap::new();
    let mut first_where: HashMap<String, &'static str> = HashMap::new();
    let mut doc_hits: HashMap<String, usize> = HashMap::new();
    let mut cluster_hits: HashMap<String, BTreeSet<String>> = HashMap::new();
    // 끝말에 안 걸린 채로만 나온 말인가 — 한 문서에서라도 끝말로 걸렸으면 푼다.
    let mut loose_only: HashMap<String, bool> = HashMap::new();
    for d in &pos {
        let words = doc_words(d, suffixes, endings);
        let mut seen: HashSet<String> = HashSet::new();
        for where_ in order {
            for term in words.get(where_) {
                if !seen.insert(term.clone()) { continue; }
                *pos_hits.entry(term.clone()).or_insert(0.0) += pos_w[&d.key];
                *doc_hits.entry(term.clone()).or_insert(0) += 1;
                cluster_hits.entry(term.clone()).or_default().insert(d.cluster.clone());
                // 이미 더 센 자리에서 봤으면 덮지 않는다.
                first_where.entry(term.clone()).or_insert(where_);
                let is_loose = words.loose.contains(term);
                let cur = loose_only.get(term).copied().unwrap_or(true);
                loose_only.insert(term.clone(), cur && is_loose);
            }
        }
    }

    let mut neg_hits: HashMap<String, f64> = HashMap::new();
    for d in &neg {
        let words = doc_words(d, suffixes, endings);
        let mut all: BTreeSet<&String> = BTreeSet::new();
        for s in [&words.name, &words.title, &words.head, &words.body, &words.loose] {
            all.extend(s.iter());
        }
        for term in all {
            if pos_hits.contains_key(term) {
                *neg_hits.entry(term.clone()).or_insert(0.0) += neg_w[&d.key];
            }
        }
    }

    // ── 거르기 ──────────────────────────────────────────────────────
    let (by_node, owner) = rule_terms(rule_doc);
    let empty: HashSet<String> = HashSet::new();
    let mine = by_node.get(node).unwrap_or(&empty);
    let mut banned_low: HashSet<String> = stopwords.global.iter()
        .map(|w| w.to_lowercase()).collect();
    if let Some(ws) = stopwords.by_node.get(node) {
        banned_low.extend(ws.iter().map(|w| w.to_lowercase()));
    }
    let forbidden_titles = other_titles(tax, node);

    for (term, hit) in &pos_hits {
        let low = term.to_lowercase();
        if mine.contains(&low) { continue; }             // ① 이 규칙에 이미 있는 말
        if banned_low.contains(&low) { continue; }        // ③ 금지 목록
        if forbidden_titles.contains(&low) { continue; }  // ⑤ 다른 분류의 이름

        let df_pos = hit / n_pos;
        let df_neg = neg_hits.get(term).copied().unwrap_or(0.0) / n_neg;
        let score = log_odds(df_pos, df_neg, n_pos, n_neg);
        let where_ = first_where[term];
        let clusters = cluster_hits[term].len();
        // 제목·파일 이름에서 나온 말은 끝말 근거를 따지지 않는다.
        let loose = loose_only.get(term).copied().unwrap_or(false)
            && (where_ == "head" || where_ == "body");

        let mut flags: Vec<String> = vec![];
        if let Some(conflict) = owner.get(&low) {
            if conflict != node {                         // ② 다른 분류 규칙에 있는 말
                flags.push(format!("충돌: {} 규칙에 있음", conflict));
            }
        }
        if looks_proper_noun(term, &pos, suffixes) {      // ⑥ 고유명사 의심(막지 않는다)
            flags.push("고유명사 의심".to_string());
        }

        // 떨어진 말도 까닭을 달아 모아 둔다(참고 목록). 자르는 조건은 그대로다.
        let mk_below = |why: String, flags: &[String]| Below {
            term: term.clone(), where_, why,
            df_pos: round_to(df_pos, 4), df_neg: round_to(df_neg, 4),
            score: round_to(score, 3), docs: doc_hits[term], clusters,
            flags: flags.to_vec(),
        };

        // 앞부분·본문에서 나온 두 글자 말은 넣지 않는다.
        if (where_ == "head" || where_ == "body") && term.chars().count() < TEXT_MIN_LEN {
            out.below.push(mk_below("두 글자 — 앞부분·본문에서는 뺍니다".to_string(), &flags));
            continue;
        }

        // 근거(문서·폴더 수)와 배타성(df_neg)이 문턱이다. 점수는 줄 세우기에만 쓴다.
        let enough = doc_hits[term] >= MIN_TERM_DOCS && clusters >= MIN_TERM_CLUSTERS;
        let passed = if loose {
            enough && df_neg <= LOOSE_DF_NEG_MAX && df_pos >= LOOSE_DF_POS_MIN
        } else {
            enough && df_neg <= DF_NEG_MAX
        };

        // 한 폴더에 몰린 어휘 — 보정 때문에 문턱을 못 넘지만 버리기 아까운 자리.
        let cluster_only = !passed && clusters == 1
            && term.chars().count() >= CLUSTER_ONLY_MIN_LEN
            && doc_hits[term] >= CLUSTER_ONLY_MIN_DOCS
            && df_neg <= CLUSTER_ONLY_DF_NEG
            && in_cluster_share(term, &pos, &cluster_hits[term], suffixes, endings)
               >= CLUSTER_ONLY_DF_POS;
        if !passed && !cluster_only {
            // 왜 떨어졌는지 한 가지만 고른다 — 가장 먼저 걸린 까닭이 사람에게 쓸모 있다.
            let why = if clusters < MIN_TERM_CLUSTERS {
                format!("폴더 {}곳 — {}곳 이상이어야 합니다", clusters, MIN_TERM_CLUSTERS)
            } else if doc_hits[term] < MIN_TERM_DOCS {
                format!("근거 {}건 — {}건 이상이어야 합니다", doc_hits[term], MIN_TERM_DOCS)
            } else if loose && df_pos < LOOSE_DF_POS_MIN {
                format!("끝말 밖 — 이 분류를 {:.0}% 만 덮습니다({:.0}% 이상 필요)",
                        df_pos * 100.0, LOOSE_DF_POS_MIN * 100.0)
            } else {
                let cap = if loose { LOOSE_DF_NEG_MAX } else { DF_NEG_MAX };
                format!("다른 분류에 {:.1}% 나옵니다({:.0}% 이하여야 합니다)",
                        df_neg * 100.0, cap * 100.0)
            };
            out.below.push(mk_below(why, &flags));
            continue;
        }

        // 기본 체크는 아주 보수적으로 켠다.
        let (fields, min_count, checked) = placement(where_);
        let checked = checked && passed && !loose && clusters >= 2
            && pos.len() >= SOLID_DOCS && flags.is_empty();
        let mut flags = flags;
        if cluster_only { flags.push("한 폴더 전용".to_string()); }

        let cand = Cand {
            term: term.clone(), fields, min_count, checked, where_,
            group: if loose { "loose" } else { "strong" },
            df_pos: round_to(df_pos, 4), df_neg: round_to(df_neg, 4),
            score: round_to(score, 3), docs: doc_hits[term], clusters, flags,
        };
        if cluster_only { out.cluster_only.push(cand); } else { out.candidates.push(cand); }
    }

    // 점수가 높고 근거가 많은 말이 위로 오게 한다.
    sort_cands(&mut out.candidates);
    sort_cands(&mut out.cluster_only);
    // 참고 목록은 '보여 줄 것이 하나도 없을 때'만 남긴다 — 후보가 있는데 그 아래
    // 긴 목록이 따라붙으면 정작 후보가 묻힌다.
    if !out.candidates.is_empty() || !out.cluster_only.is_empty() {
        out.below.clear();
    } else {
        out.below.sort_by(|a, b| {
            b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal)
                .then(b.docs.cmp(&a.docs))
                .then(a.term.cmp(&b.term))
        });
        out.below.truncate(BELOW_MAX);
    }
    if pos.len() < SOLID_DOCS {
        out.reason = format!("확정 문서 {}건 — 근거가 얕습니다({}건 이상 권장)",
                             pos.len(), SOLID_DOCS);
    }
    out
}

/// 파이썬 sort_key = (-score, -docs, term) 과 같은 차례.
fn sort_cands(v: &mut [Cand]) {
    v.sort_by(|a, b| {
        b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal)
            .then(b.docs.cmp(&a.docs))
            .then(a.term.cmp(&b.term))
    });
}

//------------------------------------------------------------------
// 한 폴더 안에서의 출현율
//=> '한 폴더 전용' 묶음에 넣을지 판단할 때만 쓴다.
//
// -in: term     = 후보 말
// -in: pos      = 이 분류의 확정 문서들
// -in: clusters = 그 말이 나온 폴더 집합(한 개일 때만 부른다)
// -in: suffixes = 끝말 목록
// -in: endings  = 활용형 꼬리 목록
//
// -out: f64 = 그 폴더 문서 중 이 말이 나온 비율(0~1)
//------------------------------------------------------------------
pub fn in_cluster_share(term: &str, pos: &[&Doc], clusters: &BTreeSet<String>,
                        suffixes: &SuffixIndex, endings: &[String]) -> f64 {
    let same: Vec<&&Doc> = pos.iter().filter(|d| clusters.contains(&d.cluster)).collect();
    if same.is_empty() { return 0.0; }
    let mut hit = 0usize;
    for d in &same {
        if doc_words(d, suffixes, endings).any(term) { hit += 1; }
    }
    hit as f64 / same.len() as f64
}

//------------------------------------------------------------------
// 사전에서 활용형 꼬리 읽기
//=> core 사전의 verb_endings 가 기준이고 업종·local 은 더하기만 한다(끝말 목록과
//   같은 규약). 어느 사전에도 없으면 빈 목록을 돌려준다 — 부르는 쪽이 내장
//   VERB_ENDINGS 를 쓴다.
//
// -in: layers = 유의어 사전 파일 경로들(얹는 차례)
//
// -out: Vec<String> = 꼬리 목록(빈 말·공백 든 말·중복은 버린다)
//------------------------------------------------------------------
pub fn load_endings(layers: &[String]) -> Vec<String> {
    let mut out: Vec<String> = vec![];
    for path in layers {
        let text = match std::fs::read_to_string(path) { Ok(t) => t, Err(_) => continue };
        let data: Value = match serde_yaml::from_str(&text) { Ok(v) => v, Err(_) => continue };
        let arr = match data.get("verb_endings").and_then(|v| v.as_array()) {
            Some(a) => a, None => continue,
        };
        for w in arr {
            let w = value_str(w);
            // 공백이 든 말은 어절 끝 비교에 쓸 수 없다.
            if w.is_empty() || w.chars().any(|c| c.is_whitespace()) { continue; }
            if !out.contains(&w) { out.push(w); }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn idx() -> SuffixIndex { suffix_index(&[]) }

    fn doc(file: &str, labels: &[&str], text: &str) -> Doc {
        make_doc(file, labels.iter().map(|s| s.to_string()).collect(), BTreeSet::new(),
                 if text.is_empty() { None } else { Some(text) })
    }

    //--------------------------------------------------------------
    // 조사를 떼되 끝말은 건드리지 않는다
    //--------------------------------------------------------------
    #[test]
    fn 조사떼기() {
        let s = idx();
        assert_eq!(strip_particle("품목보고서를", &s), "품목보고서");
        // '계약서'의 '서'는 끝말이라 조사로 보지 않는다.
        assert_eq!(strip_particle("계약서", &s), "계약서");
        // 떼고 나서 두 글자가 안 남으면 떼지 않는다.
        assert_eq!(strip_particle("나라", &s), "나라");
    }

    //--------------------------------------------------------------
    // 숫자·확장자·서술어·활용형은 후보가 못 된다
    //--------------------------------------------------------------
    #[test]
    fn 쓸만한말_거르기() {
        assert!(is_usable("품목보고서", &[]));
        assert!(!is_usable("2026년", &[]));
        assert!(!is_usable("v1.2", &[]));
        assert!(!is_usable("pdf", &[]));
        assert!(!is_usable("정한다", &[]));
        assert!(!is_usable("클릭하면", &[]));
        assert!(!is_usable("서", &[]));
        assert!(!is_usable("AI", &[]));   // 영문 두 글자는 오탐이 잦다
        assert!(is_usable("RFP", &[]));
    }

    //--------------------------------------------------------------
    // 자리를 가른다 — 제목의 말은 앞부분·본문에서 빠진다
    //--------------------------------------------------------------
    #[test]
    fn 자리별로_말을_가른다() {
        let d = doc("D:/a/품목보고서_2026.hwp", &["DC_001"], "품목보고서\n본문에 현황분석 이 있다");
        let w = doc_words(&d, &idx(), &[]);
        assert!(w.name.contains("품목보고서"));
        assert!(w.title.contains("품목보고서"));
        assert!(!w.head.contains("품목보고서"));
        assert!(w.head.contains("현황분석") || w.body.contains("현황분석"));
    }

    //--------------------------------------------------------------
    // 확정 문서가 적으면 제안하지 않고 까닭을 남긴다
    //--------------------------------------------------------------
    #[test]
    fn 재료가_적으면_까닭만() {
        let docs = vec![doc("D:/a/1.hwp", &["DC_001"], "품목보고서")];
        let r = suggest("DC_001", &docs, None, &Stopwords::default(), &HashMap::new(),
                        &idx(), &[], MIN_DOCS);
        assert!(r.candidates.is_empty());
        assert!(r.reason.contains("3건부터"));
    }

    //--------------------------------------------------------------
    // 이 분류에만 나오는 말이 후보로 오른다
    //=> 폴더 둘·문서 셋을 넘겨야 문턱을 넘는다(MIN_TERM_DOCS·MIN_TERM_CLUSTERS).
    //--------------------------------------------------------------
    #[test]
    fn 이_분류에만_나오는_말이_오른다() {
        let mut docs = vec![];
        for (i, folder) in ["D:/a", "D:/b", "D:/c"].iter().enumerate() {
            docs.push(doc(&format!("{}/품목보고서_{}.hwp", folder, i), &["DC_001"], "품목보고서\n내용"));
        }
        for (i, folder) in ["D:/x", "D:/y"].iter().enumerate() {
            docs.push(doc(&format!("{}/회의록_{}.hwp", folder, i), &["DC_002"], "회의록\n내용"));
        }
        let r = suggest("DC_001", &docs, None, &Stopwords::default(), &HashMap::new(),
                        &idx(), &[], MIN_DOCS);
        let terms: Vec<&str> = r.candidates.iter().map(|c| c.term.as_str()).collect();
        assert!(terms.contains(&"품목보고서"), "{:?}", terms);
        let cand = r.candidates.iter().find(|c| c.term == "품목보고서").unwrap();
        // 제목·파일 이름에서 나왔으므로 두 칸에 넣는다.
        assert_eq!(cand.fields, vec!["title_terms", "filename"]);
        assert_eq!(cand.df_neg, 0.0);
    }

    //--------------------------------------------------------------
    // 이미 규칙에 있는 말·금지 목록·다른 분류 이름은 빠진다
    //--------------------------------------------------------------
    #[test]
    fn 이미_있는_말은_빠진다() {
        let mut docs = vec![];
        for (i, folder) in ["D:/a", "D:/b", "D:/c"].iter().enumerate() {
            docs.push(doc(&format!("{}/품목보고서_{}.hwp", folder, i), &["DC_001"], "품목보고서\n내용"));
        }
        let rules = serde_json::json!({"doctype_rules": [
            {"node": "DC_001", "title_terms": ["품목보고서"]}]});
        let r = suggest("DC_001", &docs, Some(&rules), &Stopwords::default(),
                        &HashMap::new(), &idx(), &[], MIN_DOCS);
        assert!(r.candidates.iter().all(|c| c.term != "품목보고서"));

        let mut stop = Stopwords::default();
        stop.global.insert("품목보고서".to_string());
        let r2 = suggest("DC_001", &docs, None, &stop, &HashMap::new(), &idx(), &[], MIN_DOCS);
        assert!(r2.candidates.iter().all(|c| c.term != "품목보고서"));

        let mut tax = HashMap::new();
        tax.insert("DC_002".to_string(), "품목보고서".to_string());
        let r3 = suggest("DC_001", &docs, None, &Stopwords::default(), &tax, &idx(), &[], MIN_DOCS);
        assert!(r3.candidates.iter().all(|c| c.term != "품목보고서"));
    }

    //--------------------------------------------------------------
    // 같은 폴더 문서는 합쳐서 1건으로 센다
    //--------------------------------------------------------------
    #[test]
    fn 같은_폴더는_한_건으로_센다() {
        let docs: Vec<Doc> = (0..4).map(|i|
            doc(&format!("D:/a/{}.hwp", i), &["DC_001"], "도움말")).collect();
        let refs: Vec<&Doc> = docs.iter().collect();
        let (w, clusters) = cluster_weights(&refs);
        assert_eq!(clusters, 1);
        assert!((w.values().sum::<f64>() - 1.0).abs() < 1e-9);
    }

    //--------------------------------------------------------------
    // 로그 오즈는 다른 분류에 많이 나올수록 낮아진다
    //--------------------------------------------------------------
    #[test]
    fn 로그오즈_대조() {
        let only_here = log_odds(0.5, 0.0, 10.0, 10.0);
        let everywhere = log_odds(0.5, 0.5, 10.0, 10.0);
        assert!(only_here > everywhere);
    }
}
