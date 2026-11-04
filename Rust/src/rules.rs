//! 규칙 엔진 (Python classify/{rules,context,fuse}.py 포팅).
//! 규칙 '데이터'는 cso_rule.yaml 그대로 읽고, 스캔/융합 '로직'만 Rust 로 옮긴다.

use std::collections::HashSet;

use serde_json::{json, Value};

use crate::pii;

// ---- 등급 ----
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Grade { O, S, C }

impl Grade {
    pub fn from_str(s: &str) -> Option<Grade> {
        match s.trim() {
            "C" => Some(Grade::C),
            "S" => Some(Grade::S),
            "O" => Some(Grade::O),
            _ => None,
        }
    }
    /// 공백을 다듬지 않고 '정확히' 일치할 때만 인정한다(규칙셋 검증 전용).
    /// from_str 은 trim 을 하므로 "C " 같은 오타를 통과시키는데, 검증에서는 그것도
    /// 잡아야 Python 판(문자열 비교)과 판정이 같아진다.
    pub fn from_str_exact(s: &str) -> Option<Grade> {
        match s {
            "C" => Some(Grade::C),
            "S" => Some(Grade::S),
            "O" => Some(Grade::O),
            _ => None,
        }
    }
    pub fn as_str(&self) -> &'static str {
        match self { Grade::C => "C", Grade::S => "S", Grade::O => "O" }
    }
    fn rank(&self) -> i32 {
        match self { Grade::O => 0, Grade::S => 1, Grade::C => 2 }
    }
}

/// 정의된 등급을 '서열 낮은 → 높은' 순으로. 오류 메시지·검증에 쓴다.
pub const GRADES: [&str; 3] = ["O", "S", "C"];

// ---- 규칙셋 검증 (Phase 0 — fail-open 제거) ----
// yaml 의 등급 값이 정말 정의된 등급인지, bulk 상향이 뒤집히진 않았는지를 '문서를
// 한 건도 스캔하기 전에' 본다. 예전에는 Grade::from_str 이 None 을 돌려주면 그 규칙이
// 조용히 판정에서 빠져, 문서 등급이 실제보다 낮게 나왔다(보안 사고).
// Python 판 classify/rules.py 의 validate_rules_data 와 항목·메시지를 맞춘다.

/// 검증 위반 1건. code 는 "V0"(구조) · "V5"(정의 안 된 등급) · "V6"(bulk 역전).
pub struct Violation {
    pub code: &'static str,
    pub section: &'static str,
    pub rule_id: String,
    pub field: &'static str,
    pub value: String,
    pub detail: String,
    pub hint: Option<String>,
}

/// 규칙셋 로드 실패 종류. main 이 종료코드를 나눠 쓰려고 구분한다.
/// Invalid 만 종료코드 4(규칙셋 검증 실패), 나머지는 기존대로 2.
pub enum RulesError {
    /// 파일을 읽지 못함 / YAML 파싱 실패 등.
    Read(String),
    /// 파일은 읽었는데 내용(등급 값)이 규칙에 어긋남.
    Invalid(String),
}

impl std::fmt::Display for RulesError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RulesError::Read(s) | RulesError::Invalid(s) => write!(f, "{}", s),
        }
    }
}

/// 흔한 오타(소문자·앞뒤 공백)면 맞을 법한 등급을 제안한다.
fn grade_hint(value: &str) -> Option<String> {
    let cand = value.trim().to_uppercase();
    if GRADES.contains(&cand.as_str()) && cand != value { Some(cand) } else { None }
}

/// 값을 사람이 읽을 문자열로. Python 판 _show 와 글자 하나까지 같아야 한다.
///   null → "(null)" · 문자열 → 'C ' (앞뒤 공백이 보이도록 따옴표) · 그 외 → JSON 표기
/// 키 자체가 없는 경우는 호출부에서 "(없음)" 을 쓴다.
fn show(v: &Value) -> String {
    match v {
        Value::Null => "(null)".into(),
        Value::String(s) => format!("'{}'", s),
        other => other.to_string(),
    }
}

/// 등급 필드 1개 검사(V5).
/// -out: (위반 또는 None, 검증 통과한 등급 문자열 또는 None)
///   키가 아예 없으면 로더가 기본값(항상 유효)을 넣으므로 검사 대상이 아니다.
fn check_grade_field(item: &Value, field: &'static str, section: &'static str,
                     rule_id: &str) -> (Option<Violation>, Option<String>) {
    let raw = match item.get(field) {
        None => return (None, None),          // 키 없음 → 로더 기본값 사용, 정상
        Some(v) => v,
    };
    let mk = |detail: String, hint: Option<String>| Violation {
        code: "V5", section, rule_id: rule_id.to_string(), field,
        value: show(raw), detail, hint,
    };
    if raw.is_null() {
        return (Some(mk("값이 비어 있습니다(null)".into(), None)), None);
    }
    let s = match raw.as_str() {
        Some(s) => s,
        // yaml 에서 따옴표 없이 숫자를 적으면 문자열이 아니게 된다.
        None => {
            let t = if raw.is_i64() || raw.is_f64() { "number" }
                    else if raw.is_boolean() { "bool" }
                    else if raw.is_array() { "list" } else { "mapping" };
            return (Some(mk(format!("등급은 문자열이어야 하는데 {} 입니다", t), None)), None);
        }
    };
    if Grade::from_str_exact(s).is_none() {
        return (Some(mk("정의되지 않은 등급".into(), grade_hint(s))), None);
    }
    (None, Some(s.to_string()))
}

/// 규칙셋 원본(YAML→Value) 검증. 위반을 '전부' 모아 돌려준다.
/// 첫 오류에서 멈추지 않는 이유는 관리자가 한 번에 고칠 수 있게 하기 위해서다.
pub fn validate_rules_data(data: &Value) -> Vec<Violation> {
    // (섹션 키, 단일등급 필드들, (base, bulk) 쌍) — Python 의 _GRADE_SECTIONS 와 동일.
    // [2026-09-08] paths 섹션이 없어지면서, 그 하나를 위해 있던 '등급 생략 허용'
    // 자리도 함께 뺐다.
    const SECTIONS: [(&str, &[&str], Option<(&str, &str)>); 5] = [
        ("regex_pii",  &[],        Some(("base_grade", "bulk_grade"))),
        ("pii_combos", &["grade"], None),
        ("keywords",   &[],        Some(("base_grade", "bulk_grade"))),
        ("sensitive",  &["grade"], None),
        ("stamps",     &["grade"], None),
    ];

    // 없어진 최상위 블록 → 사람이 읽을 사유. 이 파서도 모르는 키를 조용히
    // 무시하므로, 알리지 않으면 배포된 규칙셋에서 등급이 소리 없이 바뀐다.
    const GONE: [(&str, &str); 1] = [(
        "paths",
        "paths 는 더 이상 쓰지 않는 블록입니다(2026-09-08 제거) — \
이 신호는 동작하지 않으므로 규칙셋에서 지우세요",
    )];
    let mut out: Vec<Violation> = Vec::new();

    for &(gone, why) in GONE.iter() {
        if let Some(v) = data.get(gone) {
            let empty = v.as_array().map(|a| a.is_empty()).unwrap_or(v.is_null());
            if !empty {
                out.push(Violation { code: "V12", section: gone, rule_id: "-".into(),
                    field: "", value: "(블록)".into(), detail: why.into(), hint: None });
            }
        }
    }

    // PII 등급 상한(2026-09-21) — C/S/O 중 하나여야 한다. 오타를 '제한 없음'으로 읽으면
    // 관리자는 막았다고 믿는데 PII 만으로 C 가 계속 나온다. Python V14 와 같은 항목.
    if let Some(cap) = data.get("defaults").and_then(|d| d.get("pii_grade_cap")) {
        let ok = cap.is_null() || cap.as_str().and_then(Grade::from_str_exact).is_some();
        if !ok {
            out.push(Violation { code: "V14", section: "defaults", rule_id: "pii_grade_cap".into(),
                field: "-", value: show(cap),
                detail: "C · S · O 중 하나여야 합니다(개인정보만으로 올릴 수 있는 최고 등급 — 제한하지 않으려면 칸을 지우세요)".into(), hint: None });
        }
    }

    // 2차 전파 임계값(embed:) — Python V15 와 같은 항목. 틀린 값을 조용히 기본값으로
    // 되돌리면 "고쳤는데 왜 안 바뀌지"가 된다.
    out.extend(check_embed(data.get("embed")));

    for &(section, single_fields, bulk_pair) in SECTIONS.iter() {
        let node = match data.get(section) { Some(v) => v, None => continue };
        if node.is_null() { continue; }
        let items = match node.as_array() {
            Some(a) => a,
            // 들여쓰기 실수로 목록이 매핑이 되면 순회 자체가 무의미하다.
            None => {
                out.push(Violation { code: "V0", section, rule_id: "-".into(), field: "",
                    value: show(node), detail: "섹션이 목록(list)이 아닙니다".into(), hint: None });
                continue;
            }
        };

        for (idx, item) in items.iter().enumerate() {
            if !item.is_object() {
                out.push(Violation { code: "V0", section, rule_id: format!("#{}", idx),
                    field: "", value: show(item),
                    detail: "항목이 매핑(mapping)이 아닙니다".into(), hint: None });
                continue;
            }
            let rule_id = item.get("id").and_then(|x| x.as_str())
                .map(|s| s.to_string()).unwrap_or_else(|| format!("#{}", idx));
            // 로더가 label 없는 regex_pii 항목을 건너뛰므로 검증도 같은 기준을 쓴다.
            if section == "regex_pii"
                && item.get("label").and_then(|x| x.as_str()).is_none()
                && item.get("kopii_label").and_then(|x| x.as_str()).is_none() {
                continue;
            }

            for &field in single_fields.iter() {
                if let (Some(v), _) = check_grade_field(item, field, section, &rule_id) {
                    out.push(v);
                }
            }

            if let Some((base_f, bulk_f)) = bulk_pair {
                let (vb, base_g) = check_grade_field(item, base_f, section, &rule_id);
                let (vk, bulk_g) = check_grade_field(item, bulk_f, section, &rule_id);
                if let Some(v) = vb { out.push(v); }
                if let Some(v) = vk { out.push(v); }
                // 둘 다 유효할 때만 방향을 본다(하나가 오타면 V5 로 이미 보고됨).
                if let (Some(b), Some(k)) = (base_g, bulk_g) {
                    let (rb, rk) = (Grade::from_str_exact(&b).unwrap().rank(),
                                    Grade::from_str_exact(&k).unwrap().rank());
                    if rk < rb {
                        out.push(Violation { code: "V6", section, rule_id: rule_id.clone(),
                            field: bulk_f, value: format!("'{}'", k),
                            detail: format!("bulk 는 상향이어야 하는데 낮습니다 (base={}({}) → bulk={}({}))",
                                            b, rb, k, rk),
                            hint: None });
                    }
                }
            }
        }
    }
    out
}

/// 위반 목록 → 사람이 읽는 보고문. Python format_violations 와 같은 모양.
pub fn format_violations(path: &str, violations: &[Violation]) -> String {
    let title = |code: &str| match code {
        "V0" => "[V0] 규칙셋 구조 오류",
        "V5" => "[V5] 정의되지 않은 등급 참조",
        "V12" => "[V12] 이제 쓰지 않는 블록이 남아 있음",
        _ => "[V6] bulk 상향 규칙 위반 (bulk_grade 가 base_grade 보다 낮음)",
    };
    let grades = GRADES.join(" < ");
    let mut lines = vec![
        format!("[규칙셋 오류] {} — 검증 실패 {}건. 분류를 시작하지 않았습니다.", path, violations.len()),
        String::new(),
        format!("  정의된 등급: {}", grades),
        String::new(),
    ];
    // 코드 순서를 고정해(V0 → V5 → V6) 실행할 때마다 보고서 모양이 흔들리지 않게 한다.
    for code in ["V0", "V5", "V6", "V12"] {
        let group: Vec<&Violation> = violations.iter().filter(|v| v.code == code).collect();
        if group.is_empty() { continue; }
        lines.push(format!("  {}", title(code)));
        for v in group {
            let mut where_ = format!("{}[{}]", v.section, v.rule_id);
            if !v.field.is_empty() { where_.push('.'); where_.push_str(v.field); }
            let hint = v.hint.as_ref().map(|h| format!("   → 혹시 {} ?", h)).unwrap_or_default();
            lines.push(format!("    {} = {}  — {}{}", where_, v.value, v.detail, hint));
        }
        lines.push(String::new());
    }
    lines.push(format!("  고치는 법: {} 를 열어 위 항목을 고치세요.", path));
    lines.push(format!("    · 등급 값은 {} 중 하나여야 합니다.", GRADES.join("/")));
    // V12 는 '등급을 고치라'는 안내만으로는 해결이 안 되므로 항목을 하나 더 붙인다.
    if violations.iter().any(|v| v.code == "V12") {
        lines.push("    · paths: 블록은 2026-09-08 에 없어졌습니다 — 통째로 지우세요. \n경로로 등급을 정하던 자리는 --failsafe 로 대신합니다.".into());
    }
    lines.join("\n")
}

/// 여러 등급 후보 중 최고 위험(C>S>O). 전부 None 이면 None.
pub fn max_grade<I: IntoIterator<Item = Option<Grade>>>(it: I) -> Option<Grade> {
    let mut best: Option<Grade> = None;
    for g in it {
        if let Some(g) = g {
            if best.map_or(true, |b| g.rank() > b.rank()) {
                best = Some(g);
            }
        }
    }
    best
}

fn grade_json(g: Option<Grade>) -> Value {
    match g { Some(g) => json!(g.as_str()), None => Value::Null }
}

// ---- 신뢰도 표 ----
#[derive(Clone)]
pub struct Conf {
    pub regex: (f64, f64, f64),      // high, medium, low
    pub keyword: (f64, f64, f64),
    pub sensitive: (f64, f64, f64),
    pub stamp: (f64, f64, f64),
    pub path: (f64, f64, f64),
    pub name: f64,
}
impl Default for Conf {
    fn default() -> Self {
        Conf {
            regex: (0.90, 0.70, 0.50),
            keyword: (0.85, 0.70, 0.50),
            sensitive: (0.85, 0.70, 0.55),
            stamp: (0.95, 0.80, 0.65),
            path: (0.90, 0.70, 0.50),
            name: 0.60,
        }
    }
}
fn pick(t: (f64, f64, f64), weight: &str) -> f64 {
    match weight { "high" => t.0, "medium" => t.1, "low" => t.2, _ => t.1 }
}

// ---- 규칙 구조체 ----
pub struct RegexRule { pub id: String, pub name: String, pub label: String, pub base_grade: String, pub bulk_grade: Option<String>, pub bulk_threshold: Option<i64>, pub weight: String, pub seed_eligible: bool }
pub struct ComboRule { pub id: String, pub name: String, pub all_of: Vec<String>, pub of: Vec<String>, pub min_types: usize, pub grade: String, pub weight: String, pub seed_eligible: bool }
/// 보안등급 키워드 규칙. terms 는 본문에서, filename 은 파일명에서 찾는다.
/// filename 을 안 적으면 그 규칙은 파일명 신호를 만들지 않는다(2026-09-08) —
/// terms 로 되돌아가는 폴백을 두지 않는다. 폴백은 '본문 단어가 파일명에 새는'
/// 오탐을 기본값으로 굳힌다(실측: 파일명 신호 99건 중 79건이 제품명 규칙 하나).
pub struct KeywordRule { pub id: String, pub name: String, pub terms: Vec<String>, pub filename: Vec<String>, pub exclude: Vec<String>, pub term_min_count: std::collections::HashMap<String, u32>, pub base_grade: String, pub bulk_grade: Option<String>, pub bulk_threshold: Option<i64>, pub weight: String, pub seed_eligible: bool }
pub struct SensitiveRule { pub id: String, pub name: String, pub category: String, pub terms: Vec<String>, pub exclude: Vec<String>, pub min_count: u32, pub grade: String, pub weight: String, pub seed_eligible: bool }
pub struct StampRule { pub id: String, pub name: String, pub terms: Vec<String>, pub exclude: Vec<String>, pub grade: String, pub weight: String, pub seed_eligible: bool, pub always: bool }

pub struct RuleSet {
    pub version: String,
    pub bulk_threshold: i64,
    pub case_insensitive: bool,
    pub conf: Conf,
    pub regex_rules: Vec<RegexRule>,
    pub pii_combos: Vec<ComboRule>,
    pub keyword_rules: Vec<KeywordRule>,
    pub sensitive_rules: Vec<SensitiveRule>,
    pub stamp_rules: Vec<StampRule>,
    /// PII(L1·COMBO) 근거만으로 올릴 수 있는 최고 등급. None = 제한 없음(종전).
    /// 키워드·민감정보·스탬프·파일명이 낸 C 는 그대로다 — 막는 것은 'PII 하나로만' C.
    pub pii_grade_cap: Option<Grade>,
    /// 보안등급 2차 전파 임계값(cso_rule.yaml 의 embed:). 칸이 없으면 모듈 상수 그대로.
    pub embed: crate::propagate::SecurityParams,
}

const STAMP_HEAD_CHARS: usize = 400;
const STAMP_REPEAT_MIN: u32 = 2;

// ---- YAML 로더 ----
// pub(crate): axes.rs/doc_rules.rs 도 같은 "serde_yaml::Value → 수동 필드 추출" 관례를
// 그대로 따르므로(신규 파일이라고 다른 관례를 새로 만들지 않는다), 이 파일의 헬퍼를
// 그대로 재사용한다. 동작은 그대로 두고 가시성만 넓힌다.
pub(crate) fn s(v: &Value, k: &str, def: &str) -> String {
    v.get(k).and_then(|x| x.as_str()).unwrap_or(def).to_string()
}
pub(crate) fn os(v: &Value, k: &str) -> Option<String> {
    v.get(k).and_then(|x| x.as_str()).map(|s| s.to_string())
}
pub(crate) fn b(v: &Value, k: &str, def: bool) -> bool {
    v.get(k).and_then(|x| x.as_bool()).unwrap_or(def)
}
pub(crate) fn oi(v: &Value, k: &str) -> Option<i64> {
    v.get(k).and_then(|x| x.as_i64())
}
pub(crate) fn strvec(v: &Value, k: &str) -> Vec<String> {
    v.get(k).and_then(|x| x.as_array()).map(|a| {
        a.iter().filter_map(|e| e.as_str().map(|s| s.to_string())).collect()
    }).unwrap_or_default()
}

/// cso_rule.yaml 을 읽어 RuleSet 로. (Python load_rules 대응)
/// 파싱 직후·객체 생성 전에 등급 값을 검증한다(V5·V6). 하나라도 어긋나면 RuleSet 을
/// 만들지 않고 Invalid 로 실패한다 — 잘못된 규칙셋으로 절반쯤 분류된 결과가
/// 만들어지는 쪽이 더 위험하기 때문이다.
pub fn load_rules(path: &std::path::Path) -> Result<RuleSet, RulesError> {
    let text = std::fs::read_to_string(path)
        .map_err(|e| RulesError::Read(format!("규칙셋 읽기 실패 {}: {}", path.display(), e)))?;
    // serde_yaml::Value 로 파싱 후 serde_json::Value 로 변환해 통일된 접근.
    let yv: serde_yaml::Value = serde_yaml::from_str(&text)
        .map_err(|e| RulesError::Read(format!("YAML 파싱 실패: {}", e)))?;
    let data: Value = serde_json::to_value(&yv)
        .map_err(|e| RulesError::Read(format!("변환 실패: {}", e)))?;

    let violations = validate_rules_data(&data);
    if !violations.is_empty() {
        return Err(RulesError::Invalid(
            format_violations(&path.display().to_string(), &violations)));
    }

    let mut conf = Conf::default();
    if let Some(c) = data.get("confidence") {
        let apply = |group: &str, t: &mut (f64, f64, f64)| {
            if let Some(g) = c.get(group) {
                if let Some(h) = g.get("high").and_then(|x| x.as_f64()) { t.0 = h; }
                if let Some(m) = g.get("medium").and_then(|x| x.as_f64()) { t.1 = m; }
                if let Some(l) = g.get("low").and_then(|x| x.as_f64()) { t.2 = l; }
            }
        };
        apply("regex", &mut conf.regex);
        apply("keyword", &mut conf.keyword);
        apply("sensitive", &mut conf.sensitive);
        apply("stamp", &mut conf.stamp);
        apply("path", &mut conf.path);
        if let Some(n) = c.get("name").and_then(|x| x.as_f64()) { conf.name = n; }
    }

    let empty: Vec<Value> = vec![];
    let arr = |k: &str| data.get(k).and_then(|x| x.as_array()).cloned().unwrap_or(empty.clone());

    let regex_rules = arr("regex_pii").iter().filter_map(|r| {
        let label = os(r, "label").or_else(|| os(r, "kopii_label"))?;
        Some(RegexRule {
            id: s(r, "id", ""), name: s(r, "name", &s(r, "id", "")),
            label, base_grade: s(r, "base_grade", "S"),
            bulk_grade: os(r, "bulk_grade"), bulk_threshold: oi(r, "bulk_threshold"),
            weight: s(r, "weight", "medium"), seed_eligible: b(r, "seed_eligible", false),
        })
    }).collect();

    let pii_combos = arr("pii_combos").iter().map(|r| ComboRule {
        id: s(r, "id", ""), name: s(r, "name", &s(r, "id", "")),
        all_of: strvec(r, "all_of"), of: strvec(r, "of"),
        min_types: oi(r, "min_types").unwrap_or(0).max(0) as usize,
        grade: s(r, "grade", "C"), weight: s(r, "weight", "high"),
        seed_eligible: b(r, "seed_eligible", false),
    }).collect();

    let keyword_rules = arr("keywords").iter().map(|r| KeywordRule {
        id: s(r, "id", ""), name: s(r, "name", &s(r, "id", "")),
        terms: strvec(r, "terms"),
        // 없으면 빈 목록 — 파일명 신호 없음(terms 로 폴백하지 않는다).
        filename: strvec(r, "filename"),
        exclude: strvec(r, "exclude"),
        // 단어별 최소 등장 횟수(없거나 0·음수면 1 = 종전과 같음). 파일명 신호에는 쓰지 않는다.
        term_min_count: r.get("term_min_count").and_then(|m| m.as_object())
            .map(|m| m.iter().map(|(k, v)| {
                let n = v.as_i64().filter(|&n| n > 0).unwrap_or(1) as u32;
                (k.clone(), n)
            }).collect())
            .unwrap_or_default(),
        base_grade: s(r, "base_grade", "S"), bulk_grade: os(r, "bulk_grade"),
        bulk_threshold: oi(r, "bulk_threshold"), weight: s(r, "weight", "medium"),
        seed_eligible: b(r, "seed_eligible", false),
    }).collect();

    let sensitive_rules = arr("sensitive").iter().map(|r| SensitiveRule {
        id: s(r, "id", ""), name: s(r, "name", &s(r, "id", "")),
        category: s(r, "category", ""), terms: strvec(r, "terms"),
        exclude: strvec(r, "exclude"),
        // 이 규칙 단어들의 합계가 min_count 미만이면 히트로 치지 않는다(기본 1 = 종전과 같음).
        // 규정·법령 설명 속 단발 언급("진단서를 제출", "성생활 등 민감정보")을 걸러내기 위함.
        min_count: oi(r, "min_count").filter(|&v| v > 0).map(|v| v as u32).unwrap_or(1),
        grade: s(r, "grade", "C"),
        weight: s(r, "weight", "high"), seed_eligible: b(r, "seed_eligible", false),
    }).collect();

    let stamp_rules = arr("stamps").iter().map(|r| StampRule {
        id: s(r, "id", ""), name: s(r, "name", &s(r, "id", "")),
        terms: strvec(r, "terms"),
        // 키워드·민감정보와 같은 제외어 — "기밀성"(정보보호 용어)이 '기밀' 스탬프로 반복 집계되던 것을 막는다.
        exclude: strvec(r, "exclude"), grade: s(r, "grade", "C"),
        weight: s(r, "weight", "high"), seed_eligible: b(r, "seed_eligible", false),
        always: b(r, "always", false),
    }).collect();

    let d = data.get("defaults").cloned().unwrap_or(json!({}));
    Ok(RuleSet {
        version: s(&data, "version", "unknown"),
        bulk_threshold: oi(&d, "bulk_threshold").unwrap_or(5),
        case_insensitive: b(&d, "case_insensitive", true),
        // 칸이 없으면 None = 제한 없음 — 옛 규칙셋은 판정이 한 글자도 달라지지 않는다.
        pii_grade_cap: d.get("pii_grade_cap").and_then(|v| v.as_str()).and_then(Grade::from_str_exact),
        // 칸이 없으면 기본값 — 옛 규칙셋은 전파 판정이 한 글자도 달라지지 않는다.
        embed: load_embed(data.get("embed")),
        conf, regex_rules, pii_combos, keyword_rules, sensitive_rules, stamp_rules,
    })
}

//------------------------------------------------------------------
// embed: 블록 검사 (V15) — 파이썬 rules.py::_check_embed 와 같은 기준
//=> 보안등급 2차 전파 임계값이 쓸 수 있는 값인지 본다.
//    1) 블록이 없으면(null 포함) 통과 — 코드 기본값을 쓴다
//    2) 매핑이 아니면 위반
//    3) k 는 1 이상 정수, 나머지 셋은 0.0~1.0 숫자여야 한다
//
// -in: raw = yaml 의 embed 값(None 가능)
//
// -out: Vec<Violation> = 위반 목록(없으면 빈 벡터)
// -out: error = 없음
//------------------------------------------------------------------
fn check_embed(raw: Option<&Value>) -> Vec<Violation> {
    let mut out = vec![];
    let raw = match raw { Some(v) if !v.is_null() => v, _ => return out };
    if !raw.is_object() {
        out.push(Violation { code: "V15", section: "embed", rule_id: "-".into(), field: "",
            value: show(raw), detail: "embed 는 매핑(mapping)이어야 합니다".into(), hint: None });
        return out;
    }
    if let Some(v) = raw.get("k") {
        // bool 은 숫자로 읽지 않는다(true 를 1 로 받지 않게).
        let ok = !v.is_boolean() && v.as_i64().map_or(false, |x| x >= 1);
        if !ok {
            out.push(Violation { code: "V15", section: "embed", rule_id: "-".into(), field: "k",
                value: show(v), detail: "1 이상의 정수여야 합니다".into(), hint: None });
        }
    }
    for (key, field) in [("dup_threshold", "dup_threshold"), ("min_sim", "min_sim"), ("min_share", "min_share")] {
        if let Some(v) = raw.get(key) {
            let ok = !v.is_boolean() && v.as_f64().map_or(false, |x| (0.0..=1.0).contains(&x));
            if !ok {
                out.push(Violation { code: "V15", section: "embed", rule_id: "-".into(), field,
                    value: show(v), detail: "0.0~1.0 사이 숫자여야 합니다".into(), hint: None });
            }
        }
    }
    out
}

//------------------------------------------------------------------
// embed: 블록 읽기 — 파이썬 rules.py::_load_embed 와 같다
//=> 검증을 통과한 블록만 값을 덮어쓰고, 없거나 틀리면 코드 기본값을 쓴다.
//
// -in: raw = yaml 의 embed 값(None 가능)
//
// -out: SecurityParams = 2차 전파 임계값 묶음
// -out: error = 없음
//------------------------------------------------------------------
fn load_embed(raw: Option<&Value>) -> crate::propagate::SecurityParams {
    let mut p = crate::propagate::SecurityParams::default();
    let raw = match raw { Some(v) if v.is_object() => v, _ => return p };
    // 틀린 값은 검증 단계에서 이미 막힌다. 여기선 기본값으로 안전하게 둔다.
    if !check_embed(Some(raw)).is_empty() { return p; }
    if let Some(k) = raw.get("k").and_then(|v| v.as_i64()) { p.k = k as usize; }
    if let Some(x) = raw.get("dup_threshold").and_then(|v| v.as_f64()) { p.dup_threshold = x as f32; }
    if let Some(x) = raw.get("min_sim").and_then(|v| v.as_f64()) { p.min_sim = x as f32; }
    if let Some(x) = raw.get("min_share").and_then(|v| v.as_f64()) { p.min_share = x as f32; }
    p
}

// ---- 스캔 헬퍼 ----
// pub(crate): doctype.rs 의 terms/filename 매칭이 이 두 함수를 그대로 재사용한다
// (Python 쪽 rules.py._exclude_spans/_count_outside 를 doctype.py 가 재사용하는 것과 같은 관계).
/// 제외어 구간 (byte offset). 비중첩.
pub(crate) fn exclude_spans(hay: &str, excludes: &[String], ci: bool) -> Vec<(usize, usize)> {
    let mut spans = vec![];
    for p in excludes {
        if p.is_empty() { continue; }
        let needle = if ci { p.to_lowercase() } else { p.clone() };
        if needle.is_empty() { continue; }
        let mut start = 0;
        while let Some(i) = hay[start..].find(&needle) {
            let abs = start + i;
            spans.push((abs, abs + needle.len()));
            start = abs + needle.len();
        }
    }
    spans
}

/// 제외 구간에 통째로 든 매치는 빼고 needle 등장 횟수(비중첩).
pub(crate) fn count_outside(hay: &str, needle: &str, ex: &[(usize, usize)]) -> u32 {
    if needle.is_empty() { return 0; }
    let mut total = 0;
    let mut start = 0;
    let nlen = needle.len();
    while let Some(i) = hay[start..].find(needle) {
        let abs = start + i;
        let end = abs + nlen;
        let inside = ex.iter().any(|&(s, e)| s <= abs && end <= e);
        if !inside { total += 1; }
        start = end;
    }
    total
}

fn escalate(base: &str, bulk: &Option<String>, bulk_thr: Option<i64>, count: u32, default_thr: i64) -> String {
    match bulk {
        None => base.to_string(),
        Some(bg) => {
            let thr = bulk_thr.filter(|&t| t > 0).unwrap_or(default_thr);
            if count as i64 >= thr { bg.clone() } else { base.to_string() }
        }
    }
}

/// PII 히트 1건에 등급 상한을 씌운다(Python _cap_pii_hits 와 같은 규칙).
/// 상한보다 높은 등급만 상한으로 낮추고, 원래 등급을 hit JSON 의 capped_from 에 남긴다.
/// 상한보다 낮은 등급은 그대로 둔다 — 상한까지 끌어올리지 않는다.
fn cap_pii_hit(hit: &mut (Option<Grade>, f64, bool, Value), cap: Option<Grade>) {
    let (Some(c), Some(g)) = (cap, hit.0) else { return };
    if g.rank() > c.rank() {
        hit.0 = Some(c);
        if let Some(obj) = hit.3.as_object_mut() {
            obj.insert("grade".into(), json!(c.as_str()));
            obj.insert("capped_from".into(), json!(g.as_str()));
        }
    }
}

// ---- 스캔 결과(신호) ----
pub struct Sig {
    pub grade: Option<Grade>,
    pub confidence: f64,
    pub seed_eligible: bool,
    pub dict: Value,
}

/// Signal A — 내용 규칙(PII+콤보+키워드).
/// [프라이버시 예외] --with-pii 전용: 검출된 원문 PII 값 목록.
/// ko-pii collect_pii 대응 — 활성 라벨(regex_rules) 범위로 {label,value,start,end} 반환.
pub fn collect_pii(text: &str, rs: &RuleSet) -> Vec<Value> {
    let wanted: HashSet<String> = rs.regex_rules.iter().map(|r| r.label.clone()).collect();
    pii::pii_records(text, &wanted)
        .into_iter()
        .map(|(label, value, start, end)| json!({"label": label, "value": value, "start": start, "end": end}))
        .collect()
}

pub fn scan_text(text: &str, rs: &RuleSet) -> Sig {
    let mut hits: Vec<(Option<Grade>, f64, bool, Value)> = vec![]; // grade, conf, seed, hit-json

    // L1: PII (ko-pii 재구현)
    let wanted: HashSet<String> = rs.regex_rules.iter().map(|r| r.label.clone()).collect();
    let counts = pii::pii_counts(text, &wanted);
    for rule in &rs.regex_rules {
        if let Some(&c) = counts.get(&rule.label) {
            if c == 0 { continue; }
            let g = escalate(&rule.base_grade, &rule.bulk_grade, rule.bulk_threshold, c, rs.bulk_threshold);
            let conf = pick(rs.conf.regex, &rule.weight);
            hits.push((Grade::from_str(&g), conf, rule.seed_eligible, json!({
                "id": rule.id, "name": rule.name, "layer": "L1",
                "count": c, "grade": g, "terms": []
            })));
        }
    }
    // L1-combo: 결합식별성
    let present: HashSet<&String> = counts.iter().filter(|(_, &c)| c > 0).map(|(l, _)| l).collect();
    for rule in &rs.pii_combos {
        let mut has_cond = false;
        let mut ok = true;
        let mut matched: Vec<(String, u32)> = vec![];
        if !rule.all_of.is_empty() {
            has_cond = true;
            if rule.all_of.iter().all(|l| present.contains(l)) {
                for l in &rule.all_of { matched.push((l.clone(), counts[l])); }
            } else { ok = false; }
        }
        if !rule.of.is_empty() && rule.min_types > 0 {
            has_cond = true;
            let got: Vec<&String> = rule.of.iter().filter(|l| present.contains(l)).collect();
            if got.len() >= rule.min_types {
                for l in got { matched.push((l.clone(), counts[l])); }
            } else { ok = false; }
        }
        if !has_cond || !ok { continue; }
        matched.sort();
        let conf = pick(rs.conf.regex, &rule.weight);
        // Python 원본(classify/rules.py)은 {"term","count"} dict 목록을 쓴다. 여기만
        // ["라벨", 건수] 짝 배열이라 같은 결과 파일을 읽는 화면이 이 줄에서 죽었다.
        let terms: Vec<Value> = matched.iter()
            .map(|(l, c)| json!({ "term": l, "count": c })).collect();
        hits.push((Grade::from_str(&rule.grade), conf, rule.seed_eligible, json!({
            "id": rule.id, "name": rule.name, "layer": "COMBO",
            "count": matched.len(), "grade": rule.grade, "terms": terms
        })));
    }
    // PII 근거(L1·COMBO)는 정책 상한까지만 — 이 시점의 hits 는 전부 PII 히트다.
    for h in hits.iter_mut() { cap_pii_hit(h, rs.pii_grade_cap); }
    // L2: 키워드
    let hay = if rs.case_insensitive { text.to_lowercase() } else { text.to_string() };
    for rule in &rs.keyword_rules {
        let ex = exclude_spans(&hay, &rule.exclude, rs.case_insensitive);
        let mut total = 0u32;
        let mut per_term: Vec<Value> = vec![];
        for term in &rule.terms {
            if term.is_empty() { continue; }
            let needle = if rs.case_insensitive { term.to_lowercase() } else { term.clone() };
            let c = count_outside(&hay, &needle, &ex);
            // 그 단어의 최소 횟수 미달(규정 말미 경고 같은 단발 언급)은 센 것으로 치지 않는다.
            let need = rule.term_min_count.get(term).copied().unwrap_or(1);
            if c > 0 && c >= need { per_term.push(json!({"term": term, "count": c})); total += c; }
        }
        if total == 0 { continue; }
        let g = escalate(&rule.base_grade, &rule.bulk_grade, rule.bulk_threshold, total, rs.bulk_threshold);
        let conf = pick(rs.conf.keyword, &rule.weight);
        hits.push((Grade::from_str(&g), conf, rule.seed_eligible, json!({
            "id": rule.id, "name": rule.name, "layer": "L2",
            "count": total, "grade": g, "terms": per_term
        })));
    }

    finalize(hits, "rule")
}

/// Signal F — 법상 민감정보군.
pub fn scan_sensitive(text: &str, rs: &RuleSet) -> Sig {
    let hay = if rs.case_insensitive { text.to_lowercase() } else { text.to_string() };
    let mut hits: Vec<(Option<Grade>, f64, bool, Value)> = vec![];
    for rule in &rs.sensitive_rules {
        let ex = exclude_spans(&hay, &rule.exclude, rs.case_insensitive);
        let mut total = 0u32;
        let mut per_term: Vec<Value> = vec![];
        for term in &rule.terms {
            if term.is_empty() { continue; }
            let needle = if rs.case_insensitive { term.to_lowercase() } else { term.clone() };
            let c = count_outside(&hay, &needle, &ex);
            if c > 0 { per_term.push(json!({"term": term, "count": c})); total += c; }
        }
        // 최소 건수 미달(단발 언급)은 민감정보 '보유'로 보지 않는다.
        if total == 0 || total < rule.min_count { continue; }
        let conf = pick(rs.conf.sensitive, &rule.weight);
        hits.push((Grade::from_str(&rule.grade), conf, rule.seed_eligible, json!({
            "id": rule.id, "name": rule.name, "category": rule.category,
            "grade": rule.grade, "count": total, "terms": per_term,
            // 이 두 칸은 '규칙별' 값이다. 신호 전체의 confidence/seed_eligible 은
            // 채택된 히트들의 max/any 라 서로 다를 수 있다 — 파생값이 아니라 근거다.
            "confidence": round3(conf), "seed_eligible": rule.seed_eligible
        })));
    }
    finalize(hits, "sensitive")
}

/// Signal E — 보안분류 스탬프(머리/반복 게이트).
pub fn scan_stamp(text: &str, rs: &RuleSet) -> Sig {
    let ci = rs.case_insensitive;
    let hay = if ci { text.to_lowercase() } else { text.to_string() };
    let mut hits: Vec<(Option<Grade>, f64, bool, Value)> = vec![];
    for rule in &rs.stamp_rules {
        let ex = exclude_spans(&hay, &rule.exclude, ci);
        let mut total = 0u32;
        let mut head = false;
        // 근거로 남길 문구. 머리에서 걸린 것을 우선하고, 없으면 처음 걸린 것을 쓴다
        // (파이썬 판 head_term / first_term 과 같은 규칙).
        let mut head_term: Option<&str> = None;
        let mut first_term: Option<&str> = None;
        for term in &rule.terms {
            if term.is_empty() { continue; }
            let needle = if ci { term.to_lowercase() } else { term.clone() };
            // 제외어 구간 안의 매치는 세지 않는다(제외어가 없으면 종전 matches().count() 와 같다).
            let c = count_outside(&hay, &needle, &ex);
            if c == 0 { continue; }
            total += c;
            if first_term.is_none() { first_term = Some(term); }
            if !head {
                // 머리 판정도 제외어 밖에서 처음 나온 위치로 한다.
                let nlen = needle.len();
                let first_pos = hay.match_indices(needle.as_str())
                    .map(|(i, _)| i)
                    .find(|&i| !ex.iter().any(|&(s, e)| s <= i && i + nlen <= e));
                if let Some(pos) = first_pos {
                    // '머리' 판정은 문자수 기준(byte→char 근사): 앞부분 여부.
                    let char_pos = hay[..pos].chars().count();
                    if char_pos < STAMP_HEAD_CHARS {
                        head = true;
                        head_term = Some(term);
                    }
                }
            }
        }
        if total == 0 { continue; }
        let repeat = total >= STAMP_REPEAT_MIN;
        let mode = if rule.always { "always" }
            else if head && repeat { "head+repeat" }
            else if head { "head" }
            else if repeat { "repeat" }
            else { continue };
        let conf = pick(rs.conf.stamp, &rule.weight);
        hits.push((Grade::from_str(&rule.grade), conf, rule.seed_eligible, json!({
            "id": rule.id, "name": rule.name, "grade": rule.grade,
            "mode": mode, "count": total,
            // 어느 문구가 스탬프였나 — 다른 칸에서 얻을 수 없는 근거다.
            "term": head_term.or(first_term),
            // 규칙별 확신·씨앗자격(신호 전체값과 다를 수 있다 — 위 sensitive 참고).
            "confidence": round3(conf), "seed_eligible": rule.seed_eligible
        })));
    }
    finalize(hits, "stamp")
}

/// Signal D — 파일명(키워드 재사용).
pub fn scan_filename(file: &str, rs: &RuleSet) -> Sig {
    let base = std::path::Path::new(file).file_name().and_then(|x| x.to_str()).unwrap_or("").to_lowercase();
    let mut grades: Vec<Option<Grade>> = vec![];
    let mut hits: Vec<Value> = vec![];
    for rule in &rs.keyword_rules {
        // [2026-09-08] 파일명에서 찾을 말은 rule.filename 이다. 안 적었으면 이 규칙은
        // 파일명 신호를 만들지 않는다 — terms 로 되돌아가지 않는다(파이썬 판과 같다).
        if rule.filename.is_empty() { continue; }
        let ex = exclude_spans(&base, &rule.exclude, true);
        for term in &rule.filename {
            if term.is_empty() { continue; }
            if count_outside(&base, &term.to_lowercase(), &ex) > 0 {
                grades.push(Grade::from_str(&rule.base_grade));
                hits.push(json!({"id": rule.id, "name": rule.name, "grade": rule.base_grade, "term": term}));
                break;
            }
        }
    }
    if hits.is_empty() {
        return Sig { grade: None, confidence: 0.0, seed_eligible: false,
            dict: json!({"grade": Value::Null, "confidence": 0.0, "seed_eligible": false, "hits": []}) };
    }
    let final_g = max_grade(grades);
    let conf = rs.conf.name;
    Sig { grade: final_g, confidence: conf, seed_eligible: false,
        dict: json!({"grade": grade_json(final_g), "confidence": round3(conf), "seed_eligible": false, "hits": hits}) }
}

/// 히트 목록 → 신호(최고등급·그 등급의 최고신뢰도·seed). hits 리스트가 그대로 dict.hits.
fn finalize(hits: Vec<(Option<Grade>, f64, bool, Value)>, _kind: &str) -> Sig {
    if hits.is_empty() {
        return Sig { grade: None, confidence: 0.0, seed_eligible: false,
            dict: json!({"grade": Value::Null, "confidence": 0.0, "seed_eligible": false, "hits": []}) };
    }
    let final_g = max_grade(hits.iter().map(|h| h.0));
    let deciding: Vec<&(Option<Grade>, f64, bool, Value)> = hits.iter().filter(|h| h.0 == final_g).collect();
    let confidence = deciding.iter().map(|h| h.1).fold(0.0_f64, f64::max);
    let seed = deciding.iter().any(|h| h.2);
    let hit_json: Vec<Value> = hits.iter().map(|h| h.3.clone()).collect();
    Sig { grade: final_g, confidence, seed_eligible: seed,
        dict: json!({"grade": grade_json(final_g), "confidence": round3(confidence), "seed_eligible": seed, "hits": hit_json}) }
}

// ---- 융합 ----
pub struct Fused { pub grade: Option<Grade>, pub confidence: f64, pub seed_eligible: bool, pub method: String, pub decided_by: Vec<String> }

/// 보수적 최댓값 + fail-safe 융합 (Python fuse_signals).
pub fn fuse(signals: &[(&str, &Sig)], failsafe: Option<&str>) -> Fused {
    let present: Vec<&(&str, &Sig)> = signals.iter().filter(|(_, s)| s.grade.is_some()).collect();
    if !present.is_empty() {
        let final_g = max_grade(present.iter().map(|(_, s)| s.grade));
        let deciding: Vec<&&(&str, &Sig)> = present.iter().filter(|(_, s)| s.grade == final_g).collect();
        let confidence = deciding.iter().map(|(_, s)| s.confidence).fold(0.0_f64, f64::max);
        let seed = deciding.iter().any(|(_, s)| s.seed_eligible);
        let decided_by = deciding.iter().map(|(n, _)| n.to_string()).collect();
        return Fused { grade: final_g, confidence, seed_eligible: seed, method: "fusion".into(), decided_by };
    }
    // [2026-09-08] 경로 신호와 그 fail-safe(failsafe_acl)를 걷어냈다.
    //   경로가 등급을 내던 자리는 실행할 때 --failsafe 로 대신한다.
    //   설계: plan/보안등급-신호정리-paths제거-20260908.html
    if let Some(fs) = failsafe {
        if let Some(g) = Grade::from_str(fs) {
            return Fused { grade: Some(g), confidence: 0.0, seed_eligible: false, method: "failsafe_default".into(), decided_by: vec![] };
        }
    }
    Fused { grade: None, confidence: 0.0, seed_eligible: false, method: "unclassified".into(), decided_by: vec![] }
}

pub fn round3(x: f64) -> f64 { (x * 1000.0).round() / 1000.0 }

/// 저장된 신호 dict → 융합용 Sig 복원(grade/confidence/seed_eligible 만 사용).
fn sig_from_json(v: &Value) -> Sig {
    Sig {
        grade: v.get("grade").and_then(|g| g.as_str()).and_then(Grade::from_str),
        confidence: v.get("confidence").and_then(|c| c.as_f64()).unwrap_or(0.0),
        seed_eligible: v.get("seed_eligible").and_then(|b| b.as_bool()).unwrap_or(false),
        dict: Value::Null,
    }
}

/// [전파] embed 신호를 더해 재융합(Python propagate_records 의 재융합부).
/// rec 의 저장된 rule/sensitive/stamp/path/name 을 되살려 embed 와 함께 max 융합하고
/// rec["grade"] 와 rec["why"]["security"] 를 통째로 다시 만든다(2026-09-10 — 예전에는 최상위와
/// labels.security 양쪽에 같은 값을 두 벌 적어야 했고, 한쪽만 갱신하면 어긋났다).
/// -out: 갱신된 등급.
pub fn refuse_with_embed(rec: &mut Value, embed_grade: Option<&str>, embed_conf: f64,
                         embed_dict: Value, failsafe: Option<&str>) -> Option<Grade> {
    let embed = Sig {
        grade: embed_grade.and_then(Grade::from_str),
        confidence: embed_conf,
        seed_eligible: false,
        dict: Value::Null,
    };
    // 저장된 신호 복원.
    let empty = Value::Null;
    // 새 모양은 security.signals, 옛 결과 파일은 최상위 signals — 둘 다 읽는다.
    let sg = crate::record::signals(rec).cloned().unwrap_or(json!({}));
    let get = |k: &str| sig_from_json(sg.get(k).unwrap_or(&empty));
    let (rule, sens, stamp, path, name) = (get("rule"), get("sensitive"), get("stamp"), get("path"), get("name"));
    let sigs: [(&str, &Sig); 6] = [
        ("rule", &rule), ("sensitive", &sens), ("stamp", &stamp),
        ("path", &path), ("name", &name), ("embed", &embed),
    ];
    let fused = fuse(&sigs, failsafe);
    let mut merged = sg;
    if let Some(o) = merged.as_object_mut() {
        o.insert("embed".into(), embed_dict);
    }
    rec["grade"] = grade_json(fused.grade);
    if !rec.get("why").map_or(false, |w| w.is_object()) {
        rec["why"] = json!({});
    }
    rec["why"]["security"] = security_axis(&fused, merged);
    // 옛 레코드를 그대로 받았을 수 있다 — 두 모양이 한 파일에 섞이면 읽는 쪽이
    // 어느 쪽을 믿어야 할지 알 수 없으므로, 갱신한 김에 옛 칸들을 걷어낸다.
    if let Some(o) = rec.as_object_mut() {
        // grade 는 3세대에서 다시 최상위 칸이 됐으므로 지우지 않는다.
        for k in ["confidence", "method", "decided_by", "seed_eligible", "signals",
                  "security"] {
            o.remove(k);
        }
        if let Some(l) = o.get_mut("labels").and_then(|l| l.as_object_mut()) {
            l.remove("security");
        }
    }
    fused.grade
}

/// 아무것도 못 찾은 신호(등급도 없고 걸린 규칙도 없는 것)를 빼고 signals 객체를 만든다.
/// 예전에는 네 신호를 늘 적어, 셋이 {grade:null, hits:[]} 로 "없음"만 말하는 일이 흔했다.
/// 키가 없는 것이 곧 "그 신호는 아무것도 못 찾았다" 는 뜻이다(Python _nonempty_signals 대응).
fn nonempty_signals(sigs: &[(&str, &Value)]) -> Value {
    let mut out = serde_json::Map::new();
    for (name, d) in sigs {
        let has_grade = d.get("grade").map_or(false, |g| !g.is_null());
        let has_hits = d.get("hits").and_then(|h| h.as_array()).map_or(false, |a| !a.is_empty());
        if has_grade || has_hits {
            out.insert((*name).to_string(), (*d).clone());
        }
    }
    Value::Object(out)
}

/// 융합 결과 → 레코드의 `why.security` 칸(Python `_security_axis` 대응).
///
/// [2026-09-10 ③] 등급값(grade)은 이 칸에서 뺐다. 판정은 레코드 맨 앞에 한 벌만
/// 두고(rec["grade"]), 여기에는 그 판정을 뒷받침하는 것만 남긴다.
///
/// [2026-09-10] labels 껍데기를 없애고 이 칸을 최상위로 올렸다. 예전에는 똑같은
/// 값이 최상위(grade/confidence/method/decided_by)와 labels.security 양쪽에 두 벌
/// 있었고, 전파가 한쪽만 갱신하면 두 값이 어긋날 수 있었다. 한 벌만 두면 어긋날
/// 자리가 없다. `strategy` 는 뺐다 — 등급 축은 늘 max 고정이라 문서마다 "max"
/// 라고 적어도 새로 알려 주는 것이 없다.
pub fn security_axis(fused: &Fused, signals: Value) -> Value {
    json!({
        "confidence": round3(fused.confidence),
        "method": fused.method,
        "decided_by": fused.decided_by,
        // 전파(Signal B)의 seed 수집 필터가 바로 읽는다.
        "seed_eligible": fused.seed_eligible,
        "signals": signals,
    })
}

/// 분류 레코드(JSON) 조립 — Python build_record(rule_only) 대응.
pub fn build_record(file: &str, text: &str, rs: &RuleSet, failsafe: Option<&str>) -> (Value, Option<Grade>) {
    let rule = scan_text(text, rs);
    let sens = scan_sensitive(text, rs);
    let stamp = scan_stamp(text, rs);
    let name = scan_filename(file, rs);
    let sigs = [("rule", &rule), ("sensitive", &sens), ("stamp", &stamp), ("name", &name)];
    let fused = fuse(&sigs, failsafe);
    // 축(security/doctype)을 나란한 두 칸으로 두고, 판정에 안 쓰이는 기록용
    // 값(버전·시각)은 meta 로 모은다 — 읽는 사람이 "판정"과 "장부"를 헷갈리지
    // 않게 하려는 것이다(2026-09-10, Python 판과 동일).
    let rec = json!({
        "file": file,
        // 판정을 맨 앞에 — 사람이 파일을 열면 가장 먼저 보고 싶은 값이다.
        "grade": grade_json(fused.grade),
        "why": {"security": security_axis(&fused, nonempty_signals(&[
            // 아무것도 못 찾은 신호는 적지 않는다 — 없는 키 = 아무것도 못 찾음.
            ("rule", &rule.dict), ("sensitive", &sens.dict),
            ("stamp", &stamp.dict), ("name", &name.dict),
        ]))},
        // 규칙셋 버전 세 칸은 레코드마다 적지 않는다(2026-09-10). 한 번 실행하면
        // 모든 줄이 같은 값이라, 결과 파일 맨 앞의 실행 헤더가 한 번만 적는다.
        // ts 는 줄마다 다를 수 있어(문서 하나를 처리한 시각) 여기 남는다.
        "meta": {"ts": crate::now_iso()}
    });
    (rec, fused.grade)
}


// 스탬프 제외어(exclude) — 파이썬 판 tests/test_rules.py::test_stamp_exclude_kimilsung 와 같은 사례.
#[cfg(test)]
mod stamp_exclude_tests {
    use super::*;

    fn rs() -> RuleSet {
        let p = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("resources/policy/cso_rule.yaml");
        // RulesError 는 Debug 가 없어 expect 대신 Display 로 알린다.
        load_rules(&p).unwrap_or_else(|e| panic!("cso_rule.yaml 로드 실패: {}", e))
    }

    fn stamp_hit(sig: &Sig) -> Option<Value> {
        sig.dict["hits"].as_array()?.iter().find(|h| h["id"] == "stamp_confidential").cloned()
    }

    #[test]
    fn kimilsung_repeat_is_not_stamp() {
        let rs = rs();
        let only = "데이터의 기밀성을 보장한다. 무결성, 기밀성, 가용성. 메시지 기밀성 확보";
        assert!(stamp_hit(&scan_stamp(only, &rs)).is_none());
        // 머리 밖 진짜 '기밀' 2번은 반복으로 인정되고, 건수에 '기밀성' 은 들어가지 않는다.
        let text = format!("{}{} 기밀 {} 기밀 ", only, "가".repeat(STAMP_HEAD_CHARS + 50), "나".repeat(100));
        let h = stamp_hit(&scan_stamp(&text, &rs)).expect("기밀 반복 스탬프");
        assert_eq!(h["mode"], "repeat");
        assert_eq!(h["count"], 2);
    }

    #[test]
    fn kimilsung_is_not_keyword_mark() {
        let rs = rs();
        let has = |t: &str| scan_text(t, &rs).dict["hits"].as_array().unwrap()
            .iter().any(|h| h["id"] == "mark_confidential");
        assert!(!has("자산의 기밀성, 무결성을 확보한다."));
        assert!(has("회사 기밀을 누설한 자는 징계한다."));
    }
}

// 민감정보 min_count — 파이썬 판 tests/test_rules.py::test_sensitive_min_count 와 같은 사례.
#[cfg(test)]
mod sensitive_min_count_tests {
    use super::*;

    fn rs() -> RuleSet {
        let p = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("resources/policy/cso_rule.yaml");
        load_rules(&p).unwrap_or_else(|e| panic!("cso_rule.yaml 로드 실패: {}", e))
    }

    fn cats(sig: &Sig) -> Vec<String> {
        sig.dict["hits"].as_array().unwrap().iter()
            .map(|h| h["category"].as_str().unwrap_or("").to_string()).collect()
    }

    #[test]
    fn single_mention_is_not_sensitive() {
        let rs = rs();
        assert!(!cats(&scan_sensitive("병가 시에는 의사 진단서를 제출하여야 한다.", &rs)).contains(&"건강".to_string()));
        assert!(scan_sensitive("사상·신념, 건강, 성생활, 범죄경력 등 민감정보는 동의 없이 처리할 수 없다.", &rs).grade.is_none());
        assert_eq!(scan_sensitive("진단서 원본, 진단서 사본, 진단서 발급 내역", &rs).grade, Some(Grade::C));
    }

    #[test]
    fn missing_min_count_defaults_to_one() {
        let mut rs = rs();
        rs.sensitive_rules = vec![SensitiveRule {
            id: "x".into(), name: "x".into(), category: "x".into(), terms: vec!["가".into()],
            exclude: vec![], min_count: 1, grade: "C".into(), weight: "high".into(), seed_eligible: false,
        }];
        assert_eq!(scan_sensitive("가", &rs).grade, Some(Grade::C));
    }
}

// 키워드 term_min_count — 파이썬 판 tests/test_rules.py::test_keyword_term_min_count_discipline 과 같은 사례.
#[cfg(test)]
mod keyword_term_min_count_tests {
    use super::*;

    fn rs() -> RuleSet {
        let p = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("resources/policy/cso_rule.yaml");
        load_rules(&p).unwrap_or_else(|e| panic!("cso_rule.yaml 로드 실패: {}", e))
    }

    fn hr_terms(text: &str, rs: &RuleSet) -> Option<Vec<(String, u64)>> {
        let sig = scan_text(text, rs);
        let hits = sig.dict["hits"].as_array()?.clone();
        let h = hits.into_iter().find(|h| h["id"] == "hr_payroll")?;
        Some(h["terms"].as_array().unwrap().iter()
            .map(|t| (t["term"].as_str().unwrap().to_string(), t["count"].as_u64().unwrap())).collect())
    }

    #[test]
    fn discipline_needs_three() {
        let rs = rs();
        let hr = rs.keyword_rules.iter().find(|r| r.id == "hr_payroll").unwrap();
        assert_eq!(hr.term_min_count.get("징계"), Some(&3));
        assert!(hr_terms("불필요한 SW 사용 적발 시 징계 대상이 될 수 있다.", &rs).is_none());
        assert_eq!(hr_terms("징계의 종류는 다음과 같다. 징계위원회는 징계 사유를 심의한다.", &rs),
                   Some(vec![("징계".to_string(), 3)]));
        // 같은 규칙의 다른 단어는 최소 횟수가 없어 1회로 인정
        assert_eq!(hr_terms("첨부: 2024년 급여대장. 위반 시 징계.", &rs),
                   Some(vec![("급여대장".to_string(), 1)]));
    }
}

// PII 등급 상한(defaults.pii_grade_cap) — 파이썬 판 tests/test_rules.py::test_pii_cap_* 와 같은 사례.
#[cfg(test)]
mod pii_grade_cap_tests {
    use super::*;

    // 체크섬만 유효한 가짜 값(파이썬 시험과 같은 값) — 실제 개인정보가 아니다.
    const RRN: &str = "900101-1234568";
    const CARD: &str = "4539-5787-6362-1486";

    fn rs(cap: Option<Grade>) -> RuleSet {
        let p = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("resources/policy/cso_rule.yaml");
        let mut rs = load_rules(&p).unwrap_or_else(|e| panic!("cso_rule.yaml 로드 실패: {}", e));
        rs.pii_grade_cap = cap;
        rs
    }

    fn hit(sig: &Sig, id: &str) -> Value {
        sig.dict["hits"].as_array().unwrap().iter().find(|h| h["id"] == id).cloned()
            .unwrap_or_else(|| panic!("{} 히트 없음", id))
    }

    fn roster() -> String {
        let mut t = String::from("명단
");
        for i in 0..5 { t.push_str(&format!("{}. {}
", i, RRN)); }
        t
    }

    #[test]
    fn bulk_rrn_stops_at_s() {
        let sig = scan_text(&roster(), &rs(Some(Grade::S)));
        assert_eq!(sig.grade, Some(Grade::S));
        let h = hit(&sig, "rrn");
        assert_eq!(h["grade"], "S");
        assert_eq!(h["capped_from"], "C");
    }

    #[test]
    fn keyword_c_survives() {
        let text = format!("본 문서는 대외비입니다.
{}", roster());
        let sig = scan_text(&text, &rs(Some(Grade::S)));
        assert_eq!(sig.grade, Some(Grade::C));
        assert_eq!(hit(&sig, "rrn")["grade"], "S");
    }

    #[test]
    fn combo_is_capped() {
        let text = format!("주민번호 {} 카드번호 {}", RRN, CARD);
        let base = scan_text(&text, &rs(None));
        assert_eq!(hit(&base, "identity_card")["grade"], "C");
        assert!(hit(&base, "identity_card").get("capped_from").is_none());
        let capped = scan_text(&text, &rs(Some(Grade::S)));
        let h = hit(&capped, "identity_card");
        assert_eq!((h["layer"].as_str(), h["grade"].as_str(), h["capped_from"].as_str()),
                   (Some("COMBO"), Some("S"), Some("C")));
        assert_eq!(capped.grade, Some(Grade::S));
    }

    #[test]
    fn absent_cap_is_unchanged_and_never_raises() {
        let r = rs(None);
        assert!(r.pii_grade_cap.is_none());
        let sig = scan_text(&roster(), &r);
        assert_eq!(sig.grade, Some(Grade::C));
        assert!(sig.dict["hits"].as_array().unwrap().iter().all(|h| h.get("capped_from").is_none()));
        // 상한보다 낮은 등급은 끌어올리지 않는다.
        let one = scan_text(&format!("주민번호 {}", RRN), &rs(Some(Grade::S)));
        let h = hit(&one, "rrn");
        assert_eq!(h["grade"], "S");
        assert!(h.get("capped_from").is_none());
    }

    #[test]
    fn bad_cap_is_v14() {
        for bad in ["s", "X"] {
            let data = json!({"defaults": {"pii_grade_cap": bad}});
            assert!(validate_rules_data(&data).iter().any(|v| v.code == "V14"), "{}", bad);
        }
        let num = json!({"defaults": {"pii_grade_cap": 1}});
        assert!(validate_rules_data(&num).iter().any(|v| v.code == "V14"));
        let ok = json!({"defaults": {"pii_grade_cap": "S"}});
        assert!(!validate_rules_data(&ok).iter().any(|v| v.code == "V14"));
    }
}

// embed: 칸(보안등급 2차 전파 임계값) — 파이썬 판 tests/test_rules.py::test_cso_embed_* 와 같은 사례.
#[cfg(test)]
mod cso_embed_tests {
    use super::*;
    use crate::propagate::{SecurityParams, SeedIndex, propagate};

    //------------------------------------------------------------------
    // 배포 규칙셋 값 = 코드 기본값, 칸 없음 = 기본값, 일부만 적으면 나머지 기본값
    //=> 이 칸을 넣어도 판정이 바뀌지 않음을 보장한다.
    //
    // -in: 없음
    //
    // -out: 없음
    // -out: error = 기대와 다르면 panic
    //------------------------------------------------------------------
    #[test]
    fn shipped_equals_default_and_partial_override() {
        let p = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("resources/policy/cso_rule.yaml");
        let rs = load_rules(&p).unwrap_or_else(|e| panic!("cso_rule.yaml 로드 실패: {}", e));
        let d = SecurityParams::default();
        assert_eq!((rs.embed.k, rs.embed.dup_threshold, rs.embed.min_sim, rs.embed.min_share),
                   (d.k, d.dup_threshold, d.min_sim, d.min_share));
        let none = load_embed(None);
        assert_eq!((none.k, none.min_sim), (d.k, d.min_sim));
        let part = load_embed(Some(&json!({"min_sim": 0.7, "k": 5})));
        assert_eq!((part.k, part.min_sim, part.min_share), (5, 0.7f32, d.min_share));
    }

    //------------------------------------------------------------------
    // 틀린 값은 V15
    //=> 범위 밖·bool·문자열·목록은 모두 위반으로 잡혀야 한다.
    //
    // -in: 없음
    //
    // -out: 없음
    // -out: error = 기대와 다르면 panic
    //------------------------------------------------------------------
    #[test]
    fn bad_values_are_v15() {
        for bad in [json!({"min_sim": 1.5}), json!({"min_share": -0.1}), json!({"k": 0}),
                    json!({"k": true}), json!({"dup_threshold": "0.9"}), json!([1, 2])] {
            let data = json!({"embed": bad});
            assert!(validate_rules_data(&data).iter().any(|v| v.code == "V15"), "{}", bad);
        }
        let ok = json!({"embed": {"k": 10, "dup_threshold": 0.95, "min_sim": 0.55, "min_share": 0.7}});
        assert!(!validate_rules_data(&ok).iter().any(|v| v.code == "V15"));
    }

    //------------------------------------------------------------------
    // min_sim 이 실제 판정을 바꾸는지
    //=> 1등 유사도 0.6 인 문서는 기본값(0.55)이면 등급이 붙고, 0.70 이면 too_far.
    //
    // -in: 없음
    //
    // -out: 없음
    // -out: error = 기대와 다르면 panic
    //------------------------------------------------------------------
    #[test]
    fn min_sim_changes_decision() {
        let seeds = SeedIndex::from_records(&[(Some(vec![1.0, 0.0]), Some("C".into()), true, "a".into())]);
        let q = [0.6f32, (1.0f32 - 0.36).sqrt()];
        assert_eq!(propagate(&q, &seeds, &SecurityParams::default()).grade.as_deref(), Some("C"));
        let strict = SecurityParams { min_sim: 0.70, ..SecurityParams::default() };
        let far = propagate(&q, &seeds, &strict);
        assert!(far.grade.is_none());
        assert_eq!(far.method, "too_far");
    }
}
