//! 업무분류(doctype) 축 매칭 엔진 (Python classify/doctype.py 포팅).
//! doc_rule.yaml 의 규칙을 문서 하나에 걸어 "이 문서가 어떤 분류체계 노드에
//! 해당하는가" 후보를 만들고, 조상 흡수(5-2)와 축 전략(6장, conflict.rs)을
//! 적용해 최종 labels.doctype 을 만든다. security 축의 스캔 엔진(키워드 매칭·
//! 제외어)을 그대로 재사용한다(rules::exclude_spans/count_outside).
//!
//! [재설계 1단계 반영] 내용 신호를 위치별로 쪼개고 점수를 누적한다.
//!   · title     : 첫 비어있지 않은 줄       — 가장 강한 증거
//!   · head      : 앞 head_chars 자(표제부)  — 강한 증거
//!   · body      : 문서 전체 terms           — 약한 증거(단독 채택 불가)
//!   · name      : 파일명                    — 중간 증거
//!
//! [2026-09-07 제거] form·structure·path 세 신호는 규칙이 그 필드를 한 번도
//! 채우지 않아 실제로 돌지 않았다. 도는 코드와 안 도는 코드가 섞이면 읽는
//! 사람이 매번 되짚어야 해서 걷어냈다(파이썬 판과 함께).
//! 결합은 noisy-OR(1 - Π(1-c))라 근거가 쌓일수록 점수가 오른다.
//!
//! [2026-09-22 제거] 예전 채점(scoring: legacy — 최댓값 결합, 본문 1건이면 히트)은
//! 운영 규칙이 모두 자동 생성(staged)이라 걷어냈다. 채점은 위 방식 하나뿐이다.
//! Python 구현과 값·판정이 같아야 한다(설계서 14-3 교차 검증).

use std::collections::{HashMap, HashSet};

use once_cell::sync::Lazy;
use regex::Regex;
use serde_json::{json, Map, Value};

use crate::axes::Taxonomy;
use crate::conflict::resolve_doctype;
use crate::doc_rules::{ConflictSpec, Defaults, DocRuleSet, DoctypeRule};
use crate::rules::{count_outside, exclude_spans};

// ── 신호별 신뢰도(재설계 8-2, 실측 전 잠정치) ──────────────────────────
// 상대 순서(title ≳ head > name > body)가 핵심이며,
// 절대값은 검증셋 측정 후 재보정 대상이다. Python _SIG_CONF 와 같은 값.
fn sig_conf(signal: &str, weight: &str) -> f64 {
    match (signal, weight) {
        ("title", "high") => 0.80, ("title", "low") => 0.50, ("title", _) => 0.65,
        ("head", "high") => 0.70, ("head", "low") => 0.40, ("head", _) => 0.55,
        ("body", "high") => 0.25, ("body", "low") => 0.10, ("body", _) => 0.15,
        ("name", _) => 0.30,
        _ => 0.30,
    }
}

/// 신호 하나의 등급별 신뢰도. doc_rule.yaml 의 `signals:` 한 칸에 대응한다.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Cell {
    pub high: f64,
    pub medium: f64,
    pub low: f64,
}

/// 신호 이름 → 등급별 신뢰도. 정책 파일이 `signals:` 를 적었을 때만 만들어진다.
pub type SignalTable = std::collections::BTreeMap<String, Cell>;

/// 정책 파일(signals:)에서 값을 바꿀 수 있는 신호 — 이것이 전부다.
/// 차례는 판별력이 센 것부터 — 오류 문구에 이 차례 그대로 나간다.
pub const SIGNAL_NAMES: [&str; 4] = ["title", "head", "body", "name"];



/// name 은 규칙의 weight 를 보지 않고 medium 칸만 쓴다(scan_rule 참고).
/// 그래서 high 를 다르게 적어도 효과가 없다 — 정책 파일 검증에서 막는다.
pub const FLAT_SIGNALS: [&str; 1] = ["name"];

//------------------------------------------------------------------
// 신호 하나의 코드 기본값
//=> 정책 파일이 일부 칸만 적어도 되게 하려면(예: title 만 올리기) 나머지를
//   채울 기본값이 필요하다. 파서(doc_rules)와 채점이 같은 값을 보게 한다.
//
// -in: signal = 신호 이름
//
// -out: Cell = 그 신호의 high/medium/low 기본값(모르는 이름이면 0.30 셋)
// -out: error = 없음
//------------------------------------------------------------------
pub fn default_cell(signal: &str) -> Cell {
    Cell {
        high: sig_conf(signal, "high"),
        medium: sig_conf(signal, "medium"),
        low: sig_conf(signal, "low"),
    }
}

//------------------------------------------------------------------
// 이번 판정에 쓸 신호 신뢰도 하나
//=> 정책 파일의 signals: 표가 있으면 그 값을, 없으면 코드 기본값을 쓴다.
//   표에 그 신호가 없을 수도 있으므로(부분만 적은 경우) 한 칸씩 되돌아본다.
//
// -in: tbl    = doc_rule.yaml 의 signals: 표(없으면 None)
// -in: signal = 신호 이름
// -in: weight = 규칙의 weight("high"|"medium"|"low")
//
// -out: f64 = 신뢰도
// -out: error = 없음
//------------------------------------------------------------------
fn sig_conf_of(tbl: Option<&SignalTable>, signal: &str, weight: &str) -> f64 {
    if let Some(c) = tbl.and_then(|t| t.get(signal)) {
        return match weight {
            "high" => c.high,
            "low" => c.low,
            _ => c.medium,
        };
    }
    sig_conf(signal, weight)
}

/// 이 신호들은 단독으로 후보를 만들지 못한다(재설계 8-4). 반드시 다른 신호와
/// 결합해야 한다 — 이 규칙이 없으면 신뢰도를 아무리 낮춰도 conflict: all 에서
/// 약한 후보가 그대로 남아 P1(본문 단어 1건 = 확정)이 재발한다.
const WEAK_ONLY: [&str; 1] = ["body"];

/// 첫 줄이 제목이 아닌 포맷 — title 신호를 건너뛴다(재설계 6-3, 실측 근거).
///   .ppt       : 첫 줄이 제목인 비율 16.7%(도형 순서가 시각 순서와 다름)
///   .xls/.xlsx : 50%(첫 행이 데이터 행이라 제목 개념이 없음)
const TITLE_UNSAFE_EXTS: [&str; 3] = ["ppt", "xls", "xlsx"];

/// 첫 비어있지 않은 줄. 실측(354건)에서 개행이 없는 문서는 0건이었고 첫 줄
/// 길이 중앙값은 13자, 60자 이하가 91% 였다.
pub(crate) fn first_line(text: &str) -> &str {
    text.lines().find(|l| !l.trim().is_empty()).unwrap_or("")
}

/// 공백 제거 사본(제목의 '자간 벌리기' 대응). 한국어 공문·규정은 제목을
/// "출 장 여 비 규 정"처럼 한 글자씩 띄어 쓰는 관행이 있다. 실측에서 첫 줄에
/// 이 관행을 쓴 문서가 10건이었고 전부 회수 대상이었다.
/// [적용 범위] 제목·표제부·파일명처럼 '짧은 구간'에만 쓴다 — 문서 전체에 쓰면
/// 줄바꿈까지 붙어 단어 경계를 넘는 오탐이 생긴다.
fn despace(s: &str) -> String {
    s.chars().filter(|c| !c.is_whitespace()).collect()
}

/// 순수 ASCII 영숫자(+공백·하이픈·점·밑줄)로만 이루어진 말인지. 한글이 섞이면 false.
fn is_ascii_term(s: &str) -> bool {
    let mut chars = s.chars();
    match chars.next() {
        Some(c) if c.is_ascii_alphanumeric() => {}
        _ => return false,
    }
    s.chars().all(|c| c.is_ascii_alphanumeric() || " .-_".contains(c))
}

static WORD_RE_CACHE: Lazy<std::sync::Mutex<HashMap<String, Regex>>> =
    Lazy::new(|| std::sync::Mutex::new(HashMap::new()));

/// 영문 약어를 단어 경계로 센다. 한국어는 띄어쓰기가 없어 부분문자열 매칭이
/// 맞지만 영문은 정반대다 — 'erd'(개체관계도)를 부분문자열로 찾으면 "GERD"·
/// "ordered" 안에서 걸린다. 실측에서 실제로 의학 논문이 DB설계서로 분류됐다.
fn count_word(hay: &str, needle: &str, ex: &[(usize, usize)]) -> u32 {
    let pat = format!(r"\b{}\b", regex::escape(needle));
    let mut cache = match WORD_RE_CACHE.lock() { Ok(c) => c, Err(e) => e.into_inner() };
    let re = cache.entry(pat.clone()).or_insert_with(|| {
        // escape 한 문자열이라 컴파일이 실패할 수 없다. 그래도 방어적으로
        // 실패하면 아무 것도 안 맞는 패턴을 넣어 크래시를 막는다.
        Regex::new(&pat).unwrap_or_else(|_| Regex::new(r"$^").expect("빈 패턴"))
    });
    re.find_iter(hay)
        .filter(|m| !ex.iter().any(|&(s, e)| s <= m.start() && m.end() <= e))
        .count() as u32
}

/// 단어 1개 세기 — 언어에 맞는 방식을 고른다. ASCII 면 경계 기반, 그 밖(한글
/// 포함)이면 종전 부분문자열 기반.
fn count_term(hay: &str, needle: &str, ex: &[(usize, usize)]) -> u32 {
    if is_ascii_term(needle) {
        count_word(hay, needle, ex)
    } else {
        count_outside(hay, needle, ex)
    }
}

/// 지정 구간에서 단어 찾기(title/head/body/name 공통).
/// compact=true 면 공백을 지운 사본에서도 찾아 본다(자간 벌리기 대응). 건수는
/// 두 결과의 최댓값을 쓴다 — 합치면 같은 매치를 두 번 세게 된다.
fn find_terms(scope: &str, terms: &[String], exclude: &[String], compact: bool)
              -> Vec<(String, u32)> {
    if terms.is_empty() || scope.is_empty() {
        return vec![];
    }
    let hay = scope.to_lowercase();
    let ex = exclude_spans(&hay, exclude, true);

    let (hay_c, ex_c) = if compact {
        let hc = despace(&hay);
        let exc: Vec<String> = exclude.iter().map(|e| despace(e)).collect();
        let exs = exclude_spans(&hc, &exc, true);
        (hc, exs)
    } else {
        (String::new(), vec![])
    };

    let mut hits = vec![];
    for t in terms {
        if t.is_empty() {
            continue;
        }
        let needle = t.to_lowercase();
        let mut c = count_term(&hay, &needle, &ex);
        // 공백 제거 사본에서는 ASCII 단어의 경계가 무너지므로("user guide" →
        // "userguide") 이 경로는 한글 단어에만 의미가 있다.
        if compact && !is_ascii_term(&needle) {
            c = c.max(count_outside(&hay_c, &despace(&needle), &ex_c));
        }
        if c > 0 {
            hits.push((t.clone(), c));
        }
    }
    hits
}

/// 파일명 매칭. 파일명은 짧고 의도적으로 붙인 이름이라 우연 일치가 드물어
/// 재설계에서도 매칭 로직을 유지한다(비중만 재조정).
fn match_filename(file: &str, rule: &DoctypeRule) -> Vec<(String, u32)> {
    let base = std::path::Path::new(file).file_name()
        .and_then(|x| x.to_str()).unwrap_or("");
    if base.is_empty() || rule.filename.is_empty() {
        return vec![];
    }
    find_terms(base, &rule.filename, &rule.exclude, true)
}

/// noisy-OR 결합(재설계 8-1). score = 1 - Π(1-cᵢ).
/// 단순 덧셈과 달리 항상 [0,1) 안에 머물고, 근거가 늘수록 단조 증가하되
/// 포화한다 — 약한 증거를 아무리 모아도 강한 증거 하나를 넘지 못한다.
fn noisy_or(parts: &[(String, f64)]) -> f64 {
    let mut remain = 1.0_f64;
    for (_s, c) in parts {
        remain *= 1.0 - *c;
    }
    1.0 - remain
}

/// 규칙별 임계값 고르기 — 규칙에 값이 있으면 그것을, 없으면 전역 defaults.
fn thr_head_chars(rule: &DoctypeRule, d: &Defaults) -> usize {
    rule.head_chars.unwrap_or(d.head_chars)
}
fn thr_min_distinct(rule: &DoctypeRule, d: &Defaults) -> usize {
    rule.min_distinct.unwrap_or(d.min_distinct)
}
fn thr_min_count(rule: &DoctypeRule, d: &Defaults) -> u32 {
    rule.min_count.unwrap_or(d.min_count)
}

/// title 신호를 쓸 수 있는 포맷인지(재설계 6-3, 실측 3-B-3).
pub(crate) fn title_allowed(file: &str) -> bool {
    let ext = std::path::Path::new(file).extension()
        .and_then(|x| x.to_str()).unwrap_or("").to_lowercase();
    !TITLE_UNSAFE_EXTS.contains(&ext.as_str())
}

/// 앞에서부터 n '글자'(char)만 잘라낸다. 바이트로 자르면 한글이 깨진다.
fn take_chars(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

//------------------------------------------------------------------
// 표제부(head) 범위 뽑기 — 공백 묶음을 한 칸으로 본 앞 n 자
//=> 설계: plan/업무분류-표제부-공백정합-설계-20261019.html
//   예전에는 원문 앞 n 자를 잘랐다. 그러면 빈 줄 · 줄 앞 공백 · 탭을 다르게 남기는
//   파서끼리(자체 파서 ↔ 사이냅) 같은 문서에서 낱말이 경계 안팎으로 밀려 업무분류가
//   갈렸다(실측: '취업규칙'이 405번째 ↔ 390번째).
//    1) 공백 문자의 연속(공백 · 탭 · 줄바꿈 · 빈 줄)을 공백 한 칸으로 바꾼다
//    2) 맨 앞 공백은 버린다
//    3) 앞 n 자를 자른다(평가셋 500건에서 판정 변화 0건 — 창 크기 보정 불필요)
//   [파이썬 판과의 관계] classify/doctype.py 의 head_scope 와 같은 결과여야 한다.
//   제목 · 본문 · 파일명 신호와 보안등급 · PII 는 이 함수를 쓰지 않는다(원문 그대로).
//
// -in: text = 정제된 문서 텍스트
// -in: n    = 표제부 글자 수(head_chars)
//
// -out: String = 표제부로 볼 글
// -out: error = 없음
//------------------------------------------------------------------
pub fn head_scope(text: &str, n: usize) -> String {
    let mut out = String::new();
    let mut cnt = 0usize;
    let mut in_ws = false;
    for c in text.chars() {
        // 필요한 만큼 모이면 멈춘다 — 긴 문서 전체를 훑을 이유가 없다.
        if cnt >= n {
            break;
        }
        if c.is_whitespace() {
            // 맨 앞 공백은 버리고, 공백이 이어지면 한 칸만 남긴다.
            if !in_ws && cnt > 0 {
                out.push(' ');
                cnt += 1;
            }
            in_ws = true;
        } else {
            out.push(c);
            cnt += 1;
            in_ws = false;
        }
    }
    out
}

/// 규칙 1개 스캔 결과.
struct ScanHit {
    score: f64,
    sources: Vec<String>,
    evidence: Map<String, Value>,
    parts: Vec<(String, f64)>,
}

fn terms_json(hits: &[(String, u32)]) -> Value {
    Value::Array(hits.iter()
        .map(|(t, c)| json!({ "term": t, "count": c }))
        .collect())
}

/// 규칙 1개 스캔 — 신호원 수집 + 점수화(핵심).
/// noisy-OR 로 합치고, 근거(evidence)를 남긴다 — 근거 미보존 해소는 계측의 전제조건이다.
fn scan_rule(text: &str, file: &str, rule: &DoctypeRule,
             defaults: &Defaults, allow_title: bool,
             tbl: Option<&SignalTable>) -> Option<ScanHit> {
    let mut evidence = Map::new();
    let mut parts: Vec<(String, f64)> = vec![];

    // title: 첫 비어있지 않은 줄
    if allow_title && !rule.title_terms.is_empty() {
        let hits = find_terms(first_line(text), &rule.title_terms, &rule.exclude, true);
        if !hits.is_empty() {
            evidence.insert("title".into(), json!({ "terms": terms_json(&hits) }));
            parts.push(("title".into(), sig_conf_of(tbl, "title", &rule.weight)));
        }
    }

    // head: 앞 head_chars 자(공백 묶음은 한 칸으로 센다)
    if !rule.head_terms.is_empty() {
        let n = thr_head_chars(rule, defaults);
        // 파서마다 다른 빈 줄·줄 앞 공백이 경계를 밀지 않게 head_scope 로 범위를 정한다.
        let scope = head_scope(text, n);
        let hits = find_terms(&scope, &rule.head_terms, &rule.exclude, true);
        if !hits.is_empty() {
            evidence.insert("head".into(),
                json!({ "terms": terms_json(&hits), "window": n }));
            parts.push(("head".into(), sig_conf_of(tbl, "head", &rule.weight)));
        }
    }

    // body: 문서 전체 terms
    if !rule.terms.is_empty() {
        let hits = find_terms(text, &rule.terms, &rule.exclude, false);
        let total: u32 = hits.iter().map(|(_t, c)| *c).sum();
        let distinct = hits.len();
        let need_d = thr_min_distinct(rule, defaults);
        let need_c = thr_min_count(rule, defaults);
        if !hits.is_empty() && distinct >= need_d && total >= need_c {
            let conf = sig_conf_of(tbl, "body", &rule.weight);
            evidence.insert("body".into(), json!({
                "terms": terms_json(&hits), "distinct": distinct, "total": total,
                "min_distinct": need_d, "min_count": need_c,
            }));
            parts.push(("body".into(), conf));
        }
    }

    // name: 파일명
    let nm = match_filename(file, rule);
    if !nm.is_empty() {
        let conf = sig_conf_of(tbl, "name", &rule.weight);
        let words: Vec<String> = nm.iter().map(|(t, _c)| t.clone()).collect();
        evidence.insert("name".into(), json!({ "terms": words }));
        parts.push(("name".into(), conf));
    }


    if parts.is_empty() {
        return None;
    }
    let sources: Vec<String> = parts.iter().map(|(s, _c)| s.clone()).collect();

    // 보조 신호만으로는 후보를 만들지 않는다(8-4).
    if sources.iter().all(|s| WEAK_ONLY.contains(&s.as_str())) {
        return None;
    }
    let score = noisy_or(&parts);
    // 약한 후보 임계 미만은 후보에서 뺀다(8-3). 이 컷이 없으면 conflict: all
    // 에서 약한 증거가 그대로 라벨로 남아 다중 라벨이 다시 늘어난다.
    if score < defaults.t_low {
        return None;
    }
    Some(ScanHit { score, sources, evidence, parts })
}

/// 조상 흡수(설계서 5-2). 같은 가지의 조상-자손이 함께 걸리면 조상은 버리고
/// 자손만 남긴다 — 자손의 path_ids 에 조상이 이미 들어 있으므로 중복 라벨을
/// 낼 이유가 없다. 서로 다른 가지끼리 걸린 경우(계약서+제안서)는 그대로 둘 다 남는다.
fn absorb_ancestors(dc_ids: &HashSet<String>, taxonomy: &Taxonomy) -> HashSet<String> {
    let mut survivors = dc_ids.clone();
    for dc_id in dc_ids {
        if let Ok(path_ids) = taxonomy.path_ids(dc_id) {
            let ancestors = &path_ids[..path_ids.len().saturating_sub(1)];
            for ancestor in ancestors {
                if dc_ids.contains(ancestor) {
                    survivors.remove(ancestor);
                }
            }
        }
    }
    survivors
}

/// doctype 축 후보 1개(설계서 7-1 labels.doctype.values[] 대응).
#[derive(Clone)]
pub struct Candidate {
    pub dc_id: String,
    pub path: String,
    pub path_ids: Vec<String>,
    pub confidence: f64,
    pub from: Vec<String>,
    /// "rule" | "embed" | "both" — 이 후보가 어디서 왔는지(재설계 13-2).
    pub stage: String,
    /// 신호별 근거. 규칙에 정의된 단어와 건수만 담는다(원문 조각 금지).
    pub evidence: Map<String, Value>,
    /// noisy-OR 구성요소. 임계값을 바꿨을 때를 재실행 없이 시뮬레이션하는 데 쓴다.
    pub score_parts: Vec<(String, f64)>,
    /// 파일명 핵어로만 붙은 약한 라벨인가(설계서 13장 D1 ②). true 면 받는 쪽이
    /// 자동 확정하지 않고 사람 앞에 올린다.
    pub review: bool,
    /// review 가 true 일 때 그 근거 이름("name_head").
    pub basis: String,
}

impl Candidate {
    /// 근거 없이 후보만 만들 때(테스트·전파 경로). Python 쪽 기본값과 맞춘다.
    /// 현재 호출부가 테스트 코드뿐이라 릴리스 빌드에서는 dead 로 잡힌다.
    #[allow(dead_code)]
    pub fn bare(dc_id: String, path: String, path_ids: Vec<String>,
                confidence: f64, from: Vec<String>) -> Self {
        Candidate { dc_id, path, path_ids, confidence, from,
                    stage: "rule".into(), evidence: Map::new(), score_parts: vec![],
                    review: false, basis: String::new() }
    }
}

/// 근거 두 칸(evidence·score_parts)을 신호별 한 덩어리로 합친다(Python _merge_signals 대응).
///
/// 예전에는 후보 하나에 근거가 두 군데로 흩어져 있었다 —
///   evidence    = {"head": {무슨 말이 몇 번}, ...}
///   score_parts = [("head", 0.55), ...]
/// 둘 다 열쇠가 '신호 이름'으로 같아서, 읽는 쪽은 매번 {이름:점수} 표를 만들어
/// 이름으로 맞춰 붙여야 했다(화면이 실제로 그렇게 했다).
///
/// [c 가 없을 수 있다] 전파(embed)가 이미 있는 후보에 근거만 더하는 경로가 있다.
/// 그런 신호는 "c" 키를 만들지 않는다 — 0.0 으로 채우면 "0점 기여"라는 없는
/// 사실을 말하게 된다.
///
/// 순서는 evidence 가 만들어진 순서(title→head→body→name→embed)를 그대로 따른다.
fn merge_signals(evidence: &Map<String, Value>, parts: &[(String, f64)]) -> Map<String, Value> {
    let mut out = Map::new();
    for (name, block) in evidence.iter() {
        let mut one = Map::new();
        // 점수를 앞에 둔다 — "얼마나 셌나"를 먼저 읽고 "왜"를 뒤에 읽는다.
        if let Some((_, c)) = parts.iter().find(|(s, _)| s == name) {
            one.insert("c".into(), json!(round3(*c)));
        }
        if let Some(o) = block.as_object() {
            for (k, val) in o.iter() { one.insert(k.clone(), val.clone()); }
        }
        out.insert(name.clone(), Value::Object(one));
    }
    // 근거 없이 점수만 있는 신호는 없어야 하지만, 있으면 잃지 않고 담는다.
    for (name, c) in parts.iter() {
        if !out.contains_key(name) {
            out.insert(name.clone(), json!({ "c": round3(*c) }));
        }
    }
    out
}

/// 합쳐진 signals 를 다시 evidence·score_parts 로 되돌린다(Python _split_signals 대응).
/// 엔진 속 계산은 여전히 두 칸으로 다룬다 — 레코드에 적는 모양만 바꿨기 때문이다.
/// [옛 레코드] signals 가 없으면 예전 두 칸을 그대로 읽는다.
pub fn split_signals(c: &Value) -> (Map<String, Value>, Vec<(String, f64)>) {
    let sigs = match c.get("signals").and_then(|x| x.as_object()) {
        Some(m) => m,
        None => {
            let ev = c.get("evidence").and_then(|x| x.as_object()).cloned().unwrap_or_default();
            let parts = c.get("score_parts").and_then(|x| x.as_array())
                .map(|a| a.iter().filter_map(|p| {
                    Some((p.get("signal")?.as_str()?.to_string(), p.get("c")?.as_f64()?))
                }).collect())
                .unwrap_or_default();
            return (ev, parts);
        }
    };
    let (mut evidence, mut parts) = (Map::new(), vec![]);
    for (name, one) in sigs.iter() {
        let mut block = one.as_object().cloned().unwrap_or_default();
        if let Some(c) = block.remove("c").and_then(|x| x.as_f64()) {
            parts.push((name.clone(), c));
        }
        evidence.insert(name.clone(), Value::Object(block));
    }
    (evidence, parts)
}

/// doctype 축 최종 결과(labels.doctype).
pub struct DoctypeSignal {
    pub values: Vec<Candidate>,
    pub strategy: String,
    pub status: String,
    pub truncated: usize,
    /// 계층 정합성 경고(재설계 11-3). 자동으로 후보를 버리지 않고 검토 큐
    /// 우선순위 신호로만 쓴다.
    pub conflicts: Vec<Value>,
}

impl DoctypeSignal {
    /// 분류 레코드의 labels.doctype 칸에 넣을 순수 Value.
    /// [프라이버시] evidence 에는 규칙에 정의된 단어와 건수만 담는다 —
    /// 문서 원문 조각(스니펫)은 절대 넣지 않는다.
    pub fn as_dict(&self) -> Value {
        let mut out = json!({
            "values": self.values.iter().map(|v| {
                let mut m = Map::new();
                m.insert("dc_id".into(), json!(v.dc_id));
                m.insert("path".into(), json!(v.path));
                m.insert("path_ids".into(), json!(v.path_ids));
                m.insert("confidence".into(), json!(round3(v.confidence)));
                m.insert("from".into(), json!(v.from));
                // 아래 두 칸은 없으면 키 자체를 만들지 않는다 — 소비자가
                // "안 씀"과 "비었음"을 구분할 수 있어야 한다.
                if !v.stage.is_empty() {
                    m.insert("stage".into(), json!(v.stage));
                }
                // 파일명 핵어로만 붙은 약한 라벨은 '검토 대상'으로 표시한다(13장 D1 ②).
                // 값이 있을 때만 적는다 — 없으면 "그냥 보통 라벨"이라는 뜻이다.
                if v.review {
                    m.insert("review".into(), json!(true));
                    m.insert("basis".into(), json!(if v.basis.is_empty() { "name_head" }
                                                   else { v.basis.as_str() }));
                }
                let sigs = merge_signals(&v.evidence, &v.score_parts);
                if !sigs.is_empty() {
                    m.insert("signals".into(), Value::Object(sigs));
                }
                Value::Object(m)
            }).collect::<Vec<_>>(),
        });
        // [2026-09-10] 값이 하나뿐이던 칸들을 걷어냈다(파이썬 판과 같은 규칙).
        //  · status  : 엔진은 "proposed" 말고 다른 값을 낼 수 없다. 확정은 검토
        //    화면의 몫이고 그 결정은 cso_override.jsonl 에 쌓인다.
        //  · truncated: 안 잘렸으면 0이다. 없으면 '아무것도 안 잘림'이고,
        //    **잘렸을 때는 반드시 보인다** — 무엇이 왜 빠졌는지는 숨기면 안 된다.
        //  · conflicts: 비었으면 적지 않는다(같은 이유).
        //  · strategy : 규칙 파일 값을 그대로 옮기던 칸이라, 새 정보는 '실행 시
        //    --conflict 로 덮어썼다'는 사실뿐이다. 그때만 main 이 각인한다.
        if let Some(m) = out.as_object_mut() {
            if self.truncated > 0 {
                m.insert("truncated".into(), json!(self.truncated));
            }
            if !self.conflicts.is_empty() {
                m.insert("conflicts".into(), json!(self.conflicts));
            }
        }
        out
    }
}

fn round3(x: f64) -> f64 {
    (x * 1000.0).round() / 1000.0
}

/// 노드별 병합 중간 상태.
struct Merged {
    confidence: f64,
    from: HashSet<String>,
    evidence: Map<String, Value>,
    parts: Vec<(String, f64)>,
    /// 파일명 핵어로만 붙은 약한 라벨 표시(13장 D1 ②).
    review: bool,
    /// review 의 근거 이름("name_head" · "bm25" · "bm25_name_only"). 비어 있으면 name_head.
    basis: String,
}

impl Merged {
    fn empty() -> Self {
        Merged { confidence: 0.0, from: HashSet::new(), evidence: Map::new(), parts: vec![],
                 review: false, basis: String::new() }
    }
}

/// 계층 정합성 검사(재설계 11-3). 1계층(주제)과 2계층(양식)은 서로 다른 엔진이
/// 결정하므로 모순이 생길 수 있다. 서로 다른 1계층 가지에 걸리면 플래그만
/// 남긴다 — 자동으로 하나를 버리지 않는다(자동 판정은 확정하지 않는다).
fn check_hierarchy(dc_ids: &HashSet<String>, taxonomy: &Taxonomy) -> Vec<Value> {
    if dc_ids.len() < 2 {
        return vec![];
    }
    let mut ids: Vec<String> = dc_ids.iter().cloned().collect();
    ids.sort();
    let mut roots: HashSet<String> = HashSet::new();
    for id in &ids {
        if let Ok(p) = taxonomy.path_ids(id) {
            if let Some(root) = p.first() {
                roots.insert(root.clone());
            }
        }
    }
    if roots.len() < 2 {
        return vec![];
    }
    vec![json!({
        "type": "cross_branch",
        "dc_ids": ids,
        "detail": "서로 다른 1계층 가지의 후보가 동시에 제안됐습니다 — 검토 우선순위 상향",
    })]
}

/// 노드별 병합 상태 → 최종 DoctypeSignal(scan_doctype·merge_embed_candidates 공유).
fn finalize(merged: HashMap<String, Merged>, taxonomy: &Taxonomy, conflict: &ConflictSpec)
            -> DoctypeSignal {
    if merged.is_empty() {
        return DoctypeSignal { values: vec![], strategy: conflict.strategy.clone(),
                               status: "proposed".into(), truncated: 0, conflicts: vec![] };
    }
    let ids: HashSet<String> = merged.keys().cloned().collect();
    let survivors = absorb_ancestors(&ids, taxonomy);
    let conflicts = check_hierarchy(&survivors, taxonomy);

    let candidates: Vec<Candidate> = survivors.into_iter().filter_map(|dc_id| {
        let path_ids = taxonomy.path_ids(&dc_id).ok()?;
        let path = taxonomy.path(&dc_id).ok()?;
        let m = merged.get(&dc_id)?;
        let mut from: Vec<String> = m.from.iter().cloned().collect();
        from.sort();
        // stage: 규칙에서 왔는지, 2차(벡터 · BM25)에서 왔는지, 둘 다인지(파이썬 _finalize 와 같은 규칙).
        let has_embed = m.from.contains("embed");
        let has_bm25 = m.from.contains("bm25");
        let rule_src = m.from.iter().any(|f| f != "embed" && f != "bm25");
        let stage = if !m.from.is_empty() && !rule_src {
            match (has_embed, has_bm25) { (true, false) => "embed", (false, true) => "bm25", _ => "embed+bm25" }
        } else if has_embed || has_bm25 { "both" } else { "rule" };
        Some(Candidate {
            dc_id, path, path_ids, confidence: m.confidence, from,
            stage: stage.into(), evidence: m.evidence.clone(), score_parts: m.parts.clone(),
            // 약한 라벨 표시는 노드 단위로 따라다닌다 — 받는 쪽이 자동 확정하지
            // 않게 하는 것이 유일한 목적이다.
            review: m.review,
            basis: if !m.review { String::new() }
                   else if m.basis.is_empty() { "name_head".to_string() } else { m.basis.clone() },
        })
    }).collect();

    let (values, truncated) = resolve_doctype(candidates, conflict);
    DoctypeSignal { values, strategy: conflict.strategy.clone(),
                    status: "proposed".into(), truncated, conflicts }
}

/// doctype 축 스캔(핵심 진입점). 문서 하나를 doc_rule.yaml 규칙 전체에 걸어
/// 최종 DoctypeSignal 을 만든다.
pub fn scan_doctype(text: &str, file: &str, doc_rule_set: &DocRuleSet, taxonomy: &Taxonomy)
                    -> DoctypeSignal {
    let defaults = &doc_rule_set.defaults;
    let allow_title = title_allowed(file);
    // 신호별 신뢰도는 정책 파일에서 덮어쓸 수 있다(doc_rule.yaml 의 signals:).
    // 없으면 None 이라 scan_rule 이 코드 기본값으로 돈다.
    let tbl = doc_rule_set.signals.as_ref();
    let mut merged: HashMap<String, Merged> = HashMap::new();

    for rule in doc_rule_set.active_rules() {
        let hit = match scan_rule(text, file, rule, defaults, allow_title, tbl) {
            Some(h) => h,
            None => continue,
        };
        // T5 가 로드 시점에 이미 이런 규칙을 막지만, taxonomy 없이 로드된
        // doc_rule_set(교차검증 생략)이 넘어올 수도 있으니 여기서도 방어한다.
        if taxonomy.get(&rule.node).is_none() {
            continue;
        }
        let entry = merged.entry(rule.node.clone()).or_insert_with(Merged::empty);
        for src in &hit.sources {
            entry.from.insert(src.clone());
        }
        // 같은 노드를 두 규칙이 맞혔다면 더 강하게 맞힌 쪽의 근거를 남긴다 —
        // 근거와 점수가 서로 다른 규칙에서 나오면 감사 때 설명이 안 된다.
        if hit.score > entry.confidence {
            entry.confidence = hit.score;
            entry.evidence = hit.evidence;
            entry.parts = hit.parts;
        }
    }

    // ── 1층 핵어(13장 D1) — 다른 후보가 하나도 없을 때만 ───────────
    // 파일명은 바뀔 수 있는 문자열이라 이것만으로 확정하지 않는다. 그래서
    // ① 규칙이 아무 후보도 못 만든 문서에만 작동하고 ② 검토 대상으로 표시하며
    // ③ 임베딩 씨앗으로는 쓰지 않는다(seedcli 의 씨앗 자격 검사).
    if merged.is_empty() {
        add_name_head(&mut merged, file, doc_rule_set, taxonomy);
    }
    finalize(merged, taxonomy, &doc_rule_set.conflict)
}

/// 파일명 끝자리 핵어로 약한 후보 하나 만들기(13장 D1 · Python _add_name_head).
///
/// 분류체계 제목에서 유도한 핵어 사전을 파일 이름의 끝자리에 걸어 본다. 걸리면
/// 문턱값(t_low)짜리 후보 하나를 만든다 — "파일 이름 어딘가에 그 말이 있다"
/// (name 0.30, 문턱 미달)와 달리 '핵어 자리'에 왔으므로 문턱은 넘기되, 확정이
/// 아니라 검토 대상으로 둔다. 분류체계 파일이 없으면 사전이 비어 아무 일도 안 한다.
fn add_name_head(merged: &mut HashMap<String, Merged>, file: &str,
                 doc_rule_set: &DocRuleSet, taxonomy: &Taxonomy) {
    let hit = match crate::taxhead::match_filename_head(
        file, &doc_rule_set.head_lexicon, &doc_rule_set.head_noise) {
        Some(h) => h, None => return,
    };
    let (dc_id, word, title) = hit;
    // 스냅샷이 갱신돼 사전이 낡았을 수 있다 — 없는 노드면 조용히 버린다.
    if taxonomy.get(&dc_id).is_none() { return; }
    let conf = doc_rule_set.defaults.t_low;
    let mut evidence = Map::new();
    // 유도된 말은 파일 어디에도 안 적혀 있으므로 출처를 남긴다(7장 ②).
    evidence.insert("name_head".into(), json!({
        "terms": [word],
        "taxonomy_title": title,
        "source": "doc_taxonomy",
    }));
    merged.insert(dc_id, Merged {
        confidence: conf,
        from: HashSet::from(["name_head".to_string()]),
        evidence,
        parts: vec![("name_head".into(), conf)],
        review: true,
        basis: "name_head".into(),
    });
}

/// 임베딩 전파 후보를 규칙 후보와 병합(D7, 설계서 5-4·재설계 11-1).
/// embed 는 후보를 "추가"만 한다 — 이미 있던 규칙 기반 후보를 빼앗지 않는다.
/// [상한] 벡터 '단독' 후보는 embed_cap 을 넘지 못한다(재설계 9-3a) — 득표율은
/// "이웃 중 몇 %인가"이지 "이 문서가 그 라벨일 확률"이 아니라서, 근거를 제시할
/// 수 있는 규칙 히트보다 세지면 안 된다.
///
/// -in: existing     = 병합 전 최종 후보(scan_doctype 결과의 values)
/// -in: embed_values = propagate::propagate_doctype() 이 찾은 (dc_id, confidence) 목록
/// -in: embed_cap    = 벡터 단독 후보 신뢰도 상한. None 이면 상한 없음(종전 동작)
/// -in: embed_meta   = (판정 방법, 1등 유사도) — 근거 칸에 그대로 적는다
///
/// [embed_meta 를 왜 받나] 근거 칸에 `share` 만 적고 있었다. 파이썬은 같은 자리에
/// `method`(dup_inherit/knn_vote)와 `top_sim` 까지 적는다. 이 둘이 없으면 "사본을
/// 그대로 물려받은 것"과 "이웃 여럿이 투표한 것"이 결과에서 구분되지 않아, 검토
/// 화면에서 같은 0.9 를 놓고 얼마나 믿을지 판단할 근거가 사라진다.
pub fn merge_embed_candidates(existing: &[Candidate], embed_values: &[(String, f64)],
                              taxonomy: &Taxonomy, conflict: &ConflictSpec,
                              embed_cap: Option<f64>,
                              embed_meta: (&str, f64)) -> DoctypeSignal {
    merge_second_stage(existing, Some((embed_values, embed_meta)), taxonomy, conflict, embed_cap, None)
}

/// BM25 가 고른 1위(합치기 재료). 파이썬 merge_second_stage 의 bm25_pick dict 와 같은 칸.
#[derive(Debug, Clone)]
pub struct Bm25Pick {
    pub dc_id: String,
    pub score: f64,
    pub threshold: f64,
    /// 제목 · 파일명 필드에서 얻은 점수
    pub title: f64,
    pub name: f64,
    /// 파일명에서 겹친 단어(근거 표시용)
    pub words: Vec<String>,
}

/// 2차 후보(벡터 · BM25)를 1차 후보와 한 번에 병합(파이썬 merge_second_stage 와 같은 규칙).
/// 둘을 따로 마무리하면 top_n 이 먼저 잘라 BM25 가 벡터와 합의했는지 볼 후보가 사라질 수 있어
/// 같은 중간 상태에 함께 얹고 한 번만 finalize 한다.
///
/// -in: existing = 1차 최종 후보
/// -in: embed    = (벡터 후보, (판정 방법, 1등 유사도)) — 벡터를 못 만들었으면 None
/// -in: bm25     = (BM25 1위, 설정) — 안 골랐으면 None
pub fn merge_second_stage(existing: &[Candidate], embed: Option<(&[(String, f64)], (&str, f64))>,
                          taxonomy: &Taxonomy, conflict: &ConflictSpec, embed_cap: Option<f64>,
                          bm25: Option<(&Bm25Pick, &crate::bm25::Bm25Spec)>) -> DoctypeSignal {
    let mut merged: HashMap<String, Merged> = HashMap::new();
    for v in existing {
        merged.insert(v.dc_id.clone(), Merged {
            confidence: v.confidence,
            from: v.from.iter().cloned().collect(),
            evidence: v.evidence.clone(),
            parts: v.score_parts.clone(),
            review: v.review,
            basis: v.basis.clone(),
        });
    }
    if let Some((embed_values, (embed_method, embed_top_sim))) = embed {
        // 근거 칸의 모양은 파이썬 merge_embed_candidates 의 ev_block 과 키 순서까지 같다.
        let ev_block = |conf: f64| json!({
            "method": embed_method,
            "top_sim": round3(embed_top_sim),
            "share": round3(conf),
        });
        for (dc_id, conf) in embed_values {
            if taxonomy.get(dc_id).is_none() {
                continue; // 삭제된 노드를 가리키는 낡은 seed — 조용히 무시
            }
            match merged.get_mut(dc_id) {
                Some(entry) => {
                    entry.from.insert("embed".into());
                    entry.evidence.insert("embed".into(), ev_block(*conf));
                    if *conf > entry.confidence {
                        entry.confidence = *conf;
                    }
                }
                None => {
                    // 벡터 단독 후보 — 상한을 건다.
                    let capped = match embed_cap { Some(c) => conf.min(c), None => *conf };
                    let mut ev = Map::new();
                    ev.insert("embed".into(), ev_block(*conf));
                    merged.insert(dc_id.clone(), Merged {
                        confidence: capped,
                        from: HashSet::from(["embed".to_string()]),
                        evidence: ev,
                        parts: vec![("embed".into(), capped)],
                        review: false,
                        basis: String::new(),
                    });
                }
            }
        }
    }
    if let Some((pick, spec)) = bm25 {
        if taxonomy.get(&pick.dc_id).is_some() {
            add_bm25(&mut merged, pick, spec);
        }
    }
    finalize(merged, taxonomy, conflict)
}

/// BM25 1위를 병합 중간 상태에 얹기(설계서 §08 · §14-4, 파이썬 _add_bm25 와 같은 규칙).
///  · 1차 규칙 근거가 있는 노드 → 근거만 더한다
///  · 벡터만 낸 노드 → 합의, 확신을 cap_agree 까지
///  · 새 노드 → 파일명에서만 맞았으면 name_only_cap, 아니면 cap_alone. review 표시
fn add_bm25(merged: &mut HashMap<String, Merged>, pick: &Bm25Pick, spec: &crate::bm25::Bm25Spec) {
    let (in_title, in_name) = (pick.title > 0.0, pick.name > 0.0);
    let mut ev = Map::new();
    ev.insert("score".into(), json!(round3(pick.score)));
    ev.insert("threshold".into(), json!(round3(pick.threshold)));
    ev.insert("field".into(), json!(if in_title && in_name { "title+name" }
                                     else if in_title { "title" } else { "name" }));
    if !pick.words.is_empty() {
        ev.insert("words".into(), json!(pick.words.iter().take(5).collect::<Vec<_>>()));
    }
    if let Some(entry) = merged.get_mut(&pick.dc_id) {
        if entry.from.iter().any(|f| f != "embed" && f != "bm25") {
            // 1차 규칙이 이미 붙인 분류 — 근거만 늘 뿐 확신은 그대로
            entry.from.insert("bm25".into());
            entry.evidence.insert("bm25".into(), Value::Object(ev));
            return;
        }
        // 벡터와 같은 분류를 냈다 — 두 근거가 합의하면 벡터 단독보다 높게
        let conf = entry.confidence.max(spec.cap_agree);
        entry.confidence = conf;
        entry.from.insert("bm25".into());
        entry.evidence.insert("bm25".into(), Value::Object(ev));
        entry.parts.push(("bm25".into(), conf));
        return;
    }
    let name_only = !in_title;
    let conf = if name_only { spec.name_only_cap } else { spec.cap_alone };
    let mut evm = Map::new();
    evm.insert("bm25".into(), Value::Object(ev));
    merged.insert(pick.dc_id.clone(), Merged {
        confidence: conf,
        from: HashSet::from(["bm25".to_string()]),
        evidence: evm,
        parts: vec![("bm25".into(), conf)],
        review: true,
        basis: if name_only { "bm25_name_only".into() } else { "bm25".into() },
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::doc_rules::Defaults;

    fn mk_taxonomy() -> Taxonomy {
        let yaml = "taxonomy:\n  source: t\n  exported_at: '20260824000000'\n  node_count: 7\n  nodes:\n\
            \x20 - {dc_id: LEGAL, parent: null, order: 1, title: 법무/규정, status: 1}\n\
            \x20 - {dc_id: CONTRACT, parent: LEGAL, order: 1, title: 계약서, status: 1}\n\
            \x20 - {dc_id: SALES, parent: null, order: 2, title: 영업/마케팅, status: 1}\n\
            \x20 - {dc_id: PROPOSAL, parent: SALES, order: 1, title: 제안서, status: 1}\n\
            \x20 - {dc_id: TECH, parent: null, order: 3, title: 기술/개발, status: 1}\n\
            \x20 - {dc_id: DESIGN, parent: TECH, order: 1, title: 설계문서, status: 1}\n\
            \x20 - {dc_id: REQSPEC, parent: DESIGN, order: 1, title: 요구사항정의서, status: 1}\n";
        // 파일을 거치지 않고 스냅샷 값에서 바로 짓는다(doc_taxonomy.yaml 을 읽는 길은 없앴다).
        let yv: serde_yaml::Value = serde_yaml::from_str(yaml).unwrap();
        crate::axes::taxonomy_from_value(&serde_json::to_value(&yv).unwrap(), "test").unwrap()
    }

    /// 빈 규칙 하나. 필요한 칸만 뒤에서 채워 쓴다.
    fn blank(id: &str, node: &str, weight: &str) -> DoctypeRule {
        DoctypeRule {
            id: id.into(), node: node.into(), weight: weight.into(),
            title_terms: vec![], head_terms: vec![], head_chars: None,
            terms: vec![],
            min_distinct: None, min_count: None,
            exclude: vec![], filename: vec![], active: true,
        }
    }

    fn sv(xs: &[&str]) -> Vec<String> {
        xs.iter().map(|s| s.to_string()).collect()
    }

    fn rule(id: &str, node: &str, weight: &str, terms: &[&str], exclude: &[&str],
            filename: &[&str]) -> DoctypeRule {
        let mut r = blank(id, node, weight);
        r.terms = sv(terms);
        r.exclude = sv(exclude);
        r.filename = sv(filename);
        r
    }

    fn mk_set(rules: Vec<DoctypeRule>, defaults: Defaults) -> DocRuleSet {
        DocRuleSet {
            taxonomy: None,
            conflict: ConflictSpec { strategy: "all".into(), n: None, min_confidence: None },
            defaults,
            embed: crate::doc_rules::EmbedSpec::default(),
            bm25: crate::bm25::Bm25Spec::default(),
            // 이 시험들은 코드 기본값으로 도는 것을 확인한다(정책 파일 미개입).
            signals: None,
            version: "t".into(),
            rules,
            warnings: vec![],
            // 핵어 층은 분류체계 제목에서 유도한 사전이 있어야 돈다. 아래 시험들은
            // 사전 없이(=핵어 층이 꺼진 채) 점수제만 본다 — 핵어 층 자체는
            // taxhead.rs 의 시험과 핵어_사전이_있으면… 시험이 따로 본다.
            head_lexicon: std::collections::HashMap::new(),
            head_noise: vec![],
        }
    }

    // 채점은 noisy-OR 하나뿐이라(2026-09-22 legacy 제거) 코드 기본값과 같다.
    // 이름은 재설계 시험들이 읽기 쉽게 남겨 둔다.
    fn staged() -> Defaults {
        Defaults::default()
    }

    // mk_ruleset 과 같되 본문 말을 앞부분 칸에도 넣는다. 본문만으로는 후보가 되지
    // 않으므로(8-4), 조상 흡수·충돌·top_n 처럼 '후보가 둘 이상 서야' 보이는 동작을
    // 시험할 때 쓴다.
    fn mk_ruleset_head() -> DocRuleSet {
        let mut rs = mk_ruleset();
        for r in rs.rules.iter_mut() {
            r.head_terms = r.terms.clone();
        }
        rs
    }

    fn mk_ruleset() -> DocRuleSet {
        mk_set(vec![
            rule("dt_contract", "CONTRACT", "high", &["계약서", "용역계약", "갑과 을"], &["계약서 양식"], &["계약서"]),
            rule("dt_proposal", "PROPOSAL", "high", &["제안서", "제안 내용"], &[], &["제안서"]),
            rule("dt_reqspec", "REQSPEC", "high", &["요구사항정의서", "요구사항 명세"], &[], &[]),
            rule("dt_design", "DESIGN", "medium", &["설계문서"], &[], &[]),
            rule("dt_legal_root", "LEGAL", "low", &["법무"], &[], &[]),
        ], Defaults::default())
    }

    // ── 기본 매칭 ─────────────────────────────────────────────────

    #[test]
    fn 매칭없으면_빈결과() {
        let sig = scan_doctype("아무 관련 없는 회의록입니다.", "D:/x/회의록.hwp", &mk_ruleset(), &mk_taxonomy());
        assert!(sig.values.is_empty());
    }

    #[test]
    fn exclude된_매치는_제외된다() {
        let sig = scan_doctype("이 파일은 계약서 양식일 뿐이다.", "D:/x/서식.hwp", &mk_ruleset(), &mk_taxonomy());
        assert!(sig.values.is_empty());
    }

    #[test]
    fn filename만으로도_매칭된다() {
        let sig = scan_doctype("", "D:/x/계약서_초안.hwp", &mk_ruleset(), &mk_taxonomy());
        let hit = sig.values.iter().find(|v| v.dc_id == "CONTRACT").expect("CONTRACT 매칭돼야 함");
        assert_eq!(hit.from, vec!["name".to_string()]);
    }

    #[test]
    fn terms와_filename이_함께_걸리면_from_합쳐진다() {
        let sig = scan_doctype("본 계약서는 용역계약을 다룬다.", "D:/x/계약서_2026.hwp", &mk_ruleset(), &mk_taxonomy());
        let hit = sig.values.iter().find(|v| v.dc_id == "CONTRACT").unwrap();
        let mut from = hit.from.clone();
        from.sort();
        // 재설계로 from 어휘가 신호원별로 세분화됐다: 본문 terms 는 "body".
        assert_eq!(from, vec!["body".to_string(), "name".to_string()]);
    }

    #[test]
    fn 조상_자손_동시매칭시_자손만_남는다() {
        let sig = scan_doctype("이 문서는 설계문서 중 요구사항정의서에 해당한다.", "D:/x/문서.hwp",
                               &mk_ruleset_head(), &mk_taxonomy());
        let ids: Vec<&str> = sig.values.iter().map(|v| v.dc_id.as_str()).collect();
        assert_eq!(ids, vec!["REQSPEC"]);
    }

    #[test]
    fn 서로_다른_가지는_둘_다_남는다() {
        let sig = scan_doctype("이 계약서에는 제안 내용이 별첨돼 있다.", "D:/x/문서.hwp",
                               &mk_ruleset_head(), &mk_taxonomy());
        let mut ids: Vec<&str> = sig.values.iter().map(|v| v.dc_id.as_str()).collect();
        ids.sort();
        assert_eq!(ids, vec!["CONTRACT", "PROPOSAL"]);
        // 서로 다른 1계층 가지 → 모순 플래그(11-3)
        assert_eq!(sig.conflicts.len(), 1);
    }

    #[test]
    fn top_n_전략은_잘라내고_truncated_보고() {
        let mut rs = mk_ruleset_head();
        rs.conflict = ConflictSpec { strategy: "top_n".into(), n: Some(1), min_confidence: None };
        let sig = scan_doctype("이 계약서에는 제안 내용이 별첨돼 있다.", "D:/x/문서.hwp", &rs, &mk_taxonomy());
        assert_eq!(sig.values.len(), 1);
        assert_eq!(sig.truncated, 1);
        assert_eq!(sig.strategy, "top_n");
    }

    // ── staged(재설계 1단계) ──────────────────────────────────────────

    #[test]
    fn staged에서_본문만으로는_후보가_되지_않는다() {
        let rs = mk_set(vec![rule("r", "CONTRACT", "medium", &["계약서"], &[], &[])], staged());
        let sig = scan_doctype("이 문서는 계약서를 첨부합니다.", "D:/x/공문.hwp", &rs, &mk_taxonomy());
        assert!(sig.values.is_empty());
    }

    #[test]
    fn 표제부_밖의_언급은_head신호가_아니다() {
        let mut r = blank("r", "CONTRACT", "medium");
        r.head_terms = sv(&["계약서"]);
        r.head_chars = Some(20);
        let text = format!("제목 없는 공문입니다.\n{}\n첨부: 계약서를 참조하십시오.", "가".repeat(200));
        let sig = scan_doctype(&text, "D:/x/공문.hwp", &mk_set(vec![r], staged()), &mk_taxonomy());
        assert!(sig.values.is_empty());
    }

    // 표제부 범위는 공백 묶음을 한 칸으로 센다(2026-10-19).
    // 파이썬 판 test_표제부_범위는_공백_묶음을_한_칸으로_센다 와 같은 표다 — 두 판이 같아야 한다.
    #[test]
    fn 표제부_범위_공백_묶음() {
        let cases: &[(&str, usize, &str)] = &[
            ("  가\n\n 나\t다  ", 10, "가 나 다 "),
            ("", 5, ""),
            ("가나다", 2, "가나"),
            ("\u{a0}가   나", 5, "가 나"),
            ("a\r\n\r\nb", 5, "a b"),
            ("가\n\n\n\n나다라", 3, "가 나"),
        ];
        for (text, n, want) in cases {
            assert_eq!(head_scope(text, *n), *want, "{:?}", text);
        }
    }

    // 원문으로는 경계 밖이어도 공백을 줄이면 안이면 head 신호가 잡힌다(실측 사례를 작게 줄인 것).
    #[test]
    fn 빈_줄과_줄앞_공백은_표제부_경계를_밀지_않는다() {
        let mut r = blank("r", "CONTRACT", "medium");
        r.head_terms = sv(&["계약서"]);
        r.head_chars = Some(20);
        let text = format!("가{}{}계약서 첨부", "\n".repeat(10), " ".repeat(15));
        let sig = scan_doctype(&text, "D:/x/a.hwp", &mk_set(vec![r], staged()), &mk_taxonomy());
        assert_eq!(sig.values.len(), 1);
        assert!(sig.values[0].from.contains(&"head".to_string()));
    }

    #[test]
    fn 표제부_안이면_head신호가_잡힌다() {
        let mut r = blank("r", "CONTRACT", "medium");
        r.head_terms = sv(&["계약서"]);
        r.head_chars = Some(50);
        let sig = scan_doctype("2026년 용역 계약서\n갑과 을은...", "D:/x/a.hwp",
                               &mk_set(vec![r], staged()), &mk_taxonomy());
        assert_eq!(sig.values.len(), 1);
        assert!(sig.values[0].from.contains(&"head".to_string()));
    }

    #[test]
    fn 첫줄이_제목이_아닌_포맷은_title신호를_건너뛴다() {
        let mut r = blank("r", "CONTRACT", "medium");
        r.title_terms = sv(&["계약서"]);
        for (ext, expect) in [("hwp", true), ("docx", true), ("ppt", false), ("xlsx", false)] {
            let file = format!("D:/x/a.{}", ext);
            let sig = scan_doctype("계약서\n본문", &file, &mk_set(vec![r.clone()], staged()), &mk_taxonomy());
            assert_eq!(!sig.values.is_empty(), expect, "ext={}", ext);
        }
    }

    #[test]
    fn 자간_벌리기_제목을_잡는다() {
        let mut r = blank("r", "CONTRACT", "medium");
        r.title_terms = sv(&["계약서"]);
        let sig = scan_doctype("계 약 서\n갑과 을", "D:/x/a.hwp",
                               &mk_set(vec![r], staged()), &mk_taxonomy());
        assert!(sig.values[0].from.contains(&"title".to_string()));
    }

    #[test]
    fn 영문약어는_단어_경계로만_걸린다() {
        let mut r = blank("r", "REQSPEC", "medium");
        r.title_terms = sv(&["erd"]);
        let t = mk_taxonomy();
        let ng = scan_doctype("Gastroesophageal Reflux Disease (GERD)", "D:/x/a.pdf",
                              &mk_set(vec![r.clone()], staged()), &t);
        assert!(ng.values.is_empty());
        let ok = scan_doctype("ERD 및 테이블 정의", "D:/x/a.pdf", &mk_set(vec![r], staged()), &t);
        assert!(!ok.values.is_empty());
    }

    #[test]
    fn 근거가_쌓이면_점수가_오른다() {
        let t = mk_taxonomy();
        let mut one = blank("r", "CONTRACT", "medium");
        one.head_terms = sv(&["계약서"]);
        let mut many = blank("r", "CONTRACT", "medium");
        many.head_terms = sv(&["계약서"]);
        many.filename = sv(&["계약서"]);
        many.terms = sv(&["계약서"]);
        let s1 = scan_doctype("계약서", "D:/x/문서.hwp", &mk_set(vec![one], staged()), &t);
        let s2 = scan_doctype("계약서", "D:/x/계약서.hwp", &mk_set(vec![many], staged()), &t);
        assert!(s2.values[0].confidence > s1.values[0].confidence);
    }

    #[test]
    fn noisy_or_계산값() {
        let got = noisy_or(&[("a".into(), 0.7), ("b".into(), 0.5), ("c".into(), 0.3)]);
        assert!((got - (1.0 - 0.3 * 0.5 * 0.7)).abs() < 1e-12);
        // 아무리 쌓아도 1.0 을 넘지 않는다.
        let many: Vec<(String, f64)> = (0..10).map(|i| (format!("s{}", i), 0.9)).collect();
        let big = noisy_or(&many);
        assert!(big < 1.0 && big > 0.99);
    }

    #[test]
    fn 동점_후보_정렬은_재현가능하다() {
        let t = mk_taxonomy();
        let rules = vec![
            { let mut r = blank("a", "CONTRACT", "medium"); r.filename = sv(&["문서"]); r },
            { let mut r = blank("b", "PROPOSAL", "medium"); r.filename = sv(&["문서"]); r },
            { let mut r = blank("c", "REQSPEC", "medium"); r.filename = sv(&["문서"]); r },
        ];
        let mut seen: HashSet<Vec<String>> = HashSet::new();
        for _ in 0..20 {
            let sig = scan_doctype("본문", "D:/x/문서.hwp", &mk_set(rules.clone(), staged()), &t);
            seen.insert(sig.values.iter().map(|v| v.dc_id.clone()).collect());
        }
        assert_eq!(seen.len(), 1);
        // 동점 → dc_id 오름차순
        assert_eq!(seen.into_iter().next().unwrap(),
                   vec!["CONTRACT".to_string(), "PROPOSAL".to_string(), "REQSPEC".to_string()]);
    }

    #[test]
    fn 근거블록이_남는다() {
        let mut r = blank("r", "CONTRACT", "high");
        r.title_terms = sv(&["계약서"]);
        r.terms = sv(&["계약서"]);
        r.filename = sv(&["계약서"]);
        let sig = scan_doctype("계약서\n본 계약서는", "D:/x/용역_계약서.hwp",
                               &mk_set(vec![r], staged()), &mk_taxonomy());
        let d = sig.as_dict();
        let v = &d["values"][0];
        assert_eq!(v["stage"], "rule");
        // [2026-09-10] evidence 와 score_parts 를 signals 한 칸으로 합쳤다 —
        // 둘 다 열쇠가 '신호 이름'이라 읽는 쪽이 매번 이름으로 맞춰 붙여야 했다.
        // 검사 의도(어느 신호가 무엇을 근거로, 몇 점으로 걸렸나)는 그대로다.
        assert!(v["signals"]["title"]["terms"][0]["term"] == "계약서");
        let sigs = v["signals"].as_object().unwrap();
        assert!(sigs.len() == 3);
        // 점수 몫(c)도 같은 덩어리 안에 있다 — 따로 짜맞출 것이 없다.
        assert!(sigs.values().all(|x| x.get("c").is_some()));
        assert!(v.get("evidence").is_none() && v.get("score_parts").is_none());
    }

    /// signals 로 적힌 근거를 되읽으면 엔진 속 두 칸이 그대로 복원된다.
    /// 이게 깨지면 전파를 한 번 거친 문서가 "왜 이 라벨인지"를 통째로 잃는다.
    #[test]
    fn split_signals는_합친_근거를_되돌린다() {
        let c = json!({"signals": {
            "title": {"c": 0.65, "terms": [{"term": "규정", "count": 1}]},
            "embed": {"method": "knn_vote", "top_sim": 0.88}}});
        let (ev, parts) = split_signals(&c);
        assert_eq!(ev["title"]["terms"][0]["term"], "규정");
        assert!(ev["title"].get("c").is_none());          // c 는 근거가 아니다
        assert_eq!(ev["embed"]["method"], "knn_vote");
        // 점수 몫이 없는 신호(embed)는 parts 에 들어가지 않는다.
        assert_eq!(parts, vec![("title".to_string(), 0.65)]);
    }

    /// 옛 결과 파일(evidence·score_parts 두 칸)도 그대로 읽힌다.
    #[test]
    fn split_signals는_옛_두칸도_읽는다() {
        let c = json!({"evidence": {"title": {"terms": []}},
                       "score_parts": [{"signal": "title", "c": 0.8}]});
        let (ev, parts) = split_signals(&c);
        assert!(ev.contains_key("title"));
        assert_eq!(parts, vec![("title".to_string(), 0.8)]);
    }

    // ── embed 병합 ────────────────────────────────────────────────────

    #[test]
    fn merge_embed는_새_후보를_추가한다() {
        let t = mk_taxonomy();
        let conflict = ConflictSpec::default();
        let existing = vec![Candidate::bare("CONTRACT".into(), t.path("CONTRACT").unwrap(),
                                            t.path_ids("CONTRACT").unwrap(), 0.8, vec!["body".into()])];
        let sig = merge_embed_candidates(&existing, &[("PROPOSAL".to_string(), 0.4)], &t, &conflict, None,
                                       ("knn_vote", 0.8));
        let mut ids: Vec<&str> = sig.values.iter().map(|v| v.dc_id.as_str()).collect();
        ids.sort();
        assert_eq!(ids, vec!["CONTRACT", "PROPOSAL"]);
        let p = sig.values.iter().find(|v| v.dc_id == "PROPOSAL").unwrap();
        assert_eq!(p.from, vec!["embed".to_string()]);
        assert_eq!(p.stage, "embed");
    }

    #[test]
    fn merge_embed는_같은_노드를_강화한다() {
        let t = mk_taxonomy();
        let existing = vec![Candidate::bare("CONTRACT".into(), t.path("CONTRACT").unwrap(),
                                            t.path_ids("CONTRACT").unwrap(), 0.5, vec!["body".into()])];
        let sig = merge_embed_candidates(&existing, &[("CONTRACT".to_string(), 0.9)],
                                         &t, &ConflictSpec::default(), None,
                                         ("dup_inherit", 0.97));
        assert_eq!(sig.values.len(), 1);
        assert_eq!(sig.values[0].confidence, 0.9);
        assert_eq!(sig.values[0].stage, "both");
        // 근거 칸은 파이썬과 같은 세 칸(method/top_sim/share)이어야 한다.
        let ev = &sig.values[0].evidence["embed"];
        assert_eq!(ev["method"], "dup_inherit");
        assert_eq!(ev["top_sim"], 0.97);
        assert_eq!(ev["share"], 0.9);
    }

    #[test]
    fn 벡터_단독_후보는_상한을_넘지_못한다() {
        let t = mk_taxonomy();
        let sig = merge_embed_candidates(&[], &[("CONTRACT".to_string(), 0.95)],
                                         &t, &ConflictSpec::default(), Some(0.65),
                                         ("knn_vote", 0.88));
        assert!((sig.values[0].confidence - 0.65).abs() < 1e-9);
        // 상한은 confidence 에만 걸린다 — share 는 원래 득표율 그대로(파이썬과 동일).
        assert_eq!(sig.values[0].evidence["embed"]["share"], 0.95);
        assert_eq!(sig.values[0].evidence["embed"]["method"], "knn_vote");
    }

    #[test]
    fn merge_embed_후에도_조상_흡수가_적용된다() {
        let t = mk_taxonomy();
        let existing = vec![Candidate::bare("CONTRACT".into(), t.path("CONTRACT").unwrap(),
                                            t.path_ids("CONTRACT").unwrap(), 0.8, vec!["body".into()])];
        let sig = merge_embed_candidates(&existing, &[("LEGAL".to_string(), 0.4)],
                                         &t, &ConflictSpec::default(), None,
                                         ("knn_vote", 0.75));
        let ids: Vec<&str> = sig.values.iter().map(|v| v.dc_id.as_str()).collect();
        assert_eq!(ids, vec!["CONTRACT"]);
    }

    #[test]
    fn taxonomy에_없는_embed_후보는_무시() {
        let t = mk_taxonomy();
        let sig = merge_embed_candidates(&[], &[("삭제된노드".to_string(), 0.9)],
                                         &t, &ConflictSpec::default(), None,
                                         ("dup_inherit", 0.99));
        assert!(sig.values.is_empty());
    }

    // 규칙이 0건이면(--doctype-vector-only 로 비운 배치) 1차 스캔이 반드시 빈손이어야
    // 한다 — 그래야 "규칙으로는 못 정했다"가 되고 전파가 유일한 분류 수단이 된다.
    //--------------------------------------------------------------
    // 핵어 층(13장 D1) — 파일명 끝자리 핵어는 '다른 후보가 없을 때만' 돈다
    //=> 세 가지를 한꺼번에 본다: 붙는다 · 검토 대상으로 표시된다 ·
    //   다른 후보가 있으면 안 돈다(①). 셋 중 하나만 깨져도 D1 의 전제가 무너진다.
    //--------------------------------------------------------------
    #[test]
    fn 핵어_층은_후보가_없을_때만_약한_라벨을_붙인다() {
        let t = mk_taxonomy();
        // 본문 단어만 있는 규칙은 단독으로 후보가 안 되므로(8-4) 제목 신호를 쓴다.
        let mut c_rule = blank("dt_c", "CONTRACT", "medium");
        c_rule.title_terms = sv(&["계약서"]);
        let mut drs = mk_set(vec![c_rule], staged());
        drs.head_lexicon.insert("PROPOSAL".to_string(), crate::taxhead::HeadEntry {
            title: "제안서".into(), heads: vec!["제안서".to_string()] });

        // 다른 신호가 전혀 없는 문서 — 파일명 끝자리 핵어로 약한 라벨이 붙는다.
        let sig = scan_doctype("내용 없음", r"D:\x\보안관제_제안서_최종_v2.hwp", &drs, &t);
        assert_eq!(sig.values.len(), 1);
        assert_eq!(sig.values[0].dc_id, "PROPOSAL");
        assert_eq!(sig.values[0].from, vec!["name_head".to_string()]);
        assert!((sig.values[0].confidence - drs.defaults.t_low).abs() < 1e-9);
        assert!(sig.values[0].review);
        let v = sig.as_dict();
        assert_eq!(v["values"][0]["review"], serde_json::json!(true));
        assert_eq!(v["values"][0]["basis"], serde_json::json!("name_head"));
        // 왜 이 라벨인지 — 파일 어디에도 안 적힌 말이므로 출처를 남긴다(7장 ②).
        assert_eq!(v["values"][0]["signals"]["name_head"]["source"],
                   serde_json::json!("doc_taxonomy"));

        // 규칙이 이미 후보를 만든 문서에는 돌지 않는다(안전장치 ①).
        let sig2 = scan_doctype("용역 계약서\n갑과 을은…", r"D:\x\보안관제_제안서.hwp", &drs, &t);
        let ids: Vec<&str> = sig2.values.iter().map(|c| c.dc_id.as_str()).collect();
        assert_eq!(ids, vec!["CONTRACT"]);
        assert!(!sig2.values[0].review);

        // 끝자리가 아니면 안 붙는다 — '제안서_검토_회의록' 을 제안서로 보내지 않는다.
        let sig3 = scan_doctype("내용 없음", r"D:\x\제안서_검토_회의록.hwp", &drs, &t);
        assert!(sig3.values.is_empty());
    }

    #[test]
    fn 규칙0건_스캔은_아무것도_못맞힌다() {
        let drs = crate::doc_rules::DocRuleSet::empty();
        let sig = scan_doctype("계약서 내용입니다", "계약서.txt", &drs, &mk_taxonomy());
        assert!(sig.values.is_empty());
        assert_eq!(sig.truncated, 0);
    }

    #[test]
    fn 규칙0건이어도_embed후보는_라벨이_된다() {
        let t = mk_taxonomy();
        let drs = crate::doc_rules::DocRuleSet::empty();
        let sig = merge_embed_candidates(&[], &[("REQSPEC".to_string(), 0.8)], &t, &drs.conflict, None,
                                       ("knn_vote", 0.82));
        assert_eq!(sig.values.len(), 1);
        assert_eq!(sig.values[0].path, "기술/개발 > 설계문서 > 요구사항정의서");
        assert_eq!(sig.values[0].from, vec!["embed".to_string()]);
    }
}
