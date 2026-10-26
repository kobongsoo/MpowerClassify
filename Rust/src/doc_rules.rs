//! 업무분류(doctype) 축 — doc_rule.yaml 로더 (Python classify/doc_rules.py 포팅).
//! "무엇을 계약서로 볼 것인가" 규칙(doctype_rules)과 축 전체 충돌 전략(conflict)을
//! 읽는다. cso_rule.yaml(security 축)과는 완전히 다른 파일이며 이 로더는 그
//! 파일을 전혀 건드리지 않는다. doc_rule.yaml 이 없어도 시스템은 계속 동작해야
//! 한다 — "있으면 켜지고 없으면 꺼지는 외장 자산"(설계서 4-6, T16).

use std::collections::HashSet;

use serde_json::Value;

use crate::axes::Taxonomy;
use crate::rules::{s, strvec};

/// doc_rule.yaml 이 정의할 수 있는 conflict 전략. taxonomy 축(트리)은 서열이
/// 없어 max 는 애초에 문법에 없다 — 오타로 적혀도 T2 로 막는다.
pub const CONFLICT_STRATEGIES: [&str; 2] = ["all", "top_n"];

/// 축 전체 충돌 해소 전략 설정. doctype 축에서 유일하게 파일에 적는 값이다
/// (id·kind·cardinality·required·taxonomy 경로는 전부 코드에 고정돼 사라졌다, 3-2-1).
pub struct ConflictSpec {
    pub strategy: String,
    pub n: Option<i64>,
    pub min_confidence: Option<f64>,
}

impl Default for ConflictSpec {
    fn default() -> Self {
        ConflictSpec { strategy: "all".into(), n: None, min_confidence: None }
    }
}

/// 전역 기본값 블록(doc_rule.yaml 의 defaults:). 규칙에 같은 이름 필드가 있으면
/// 그쪽이 이긴다.
/// [하위호환] min_distinct/min_count 기본값은 1 — 종전 "본문에 1건이라도 있으면
/// 히트"와 완전히 같게 동작한다. 조이는 것은 정책 파일에서 켜는 옵트인이다.
#[derive(Clone, Debug, PartialEq)]
pub struct Defaults {
    pub head_chars: usize,
    pub min_distinct: usize,
    pub min_count: u32,
    pub t_low: f64,
    pub t_seed: f64,
    pub embed_cap: f64,
}

impl Default for Defaults {
    fn default() -> Self {
        Defaults {
            head_chars: 400, min_distinct: 1, min_count: 1,
            t_low: 0.30, t_seed: 0.85, embed_cap: 0.65,
        }
    }
}

// [2026-09-22 제거] scoring(legacy/staged 스위치) — 채점은 noisy-OR(staged) 하나뿐.
// 옛 파일에 scoring 이 남아 있어도 모르는 칸이라 넘어간다(파이썬 판과 같다).

/// 임베딩 전파 설정 블록(doc_rule.yaml 의 embed:).
#[derive(Clone, Debug, PartialEq)]
pub struct EmbedSpec {
    pub enabled: bool,
    pub k: usize,
    pub dup_threshold: f64,
    pub min_sim: f64,
    pub min_share: f64,
    pub prototype: String,
}

impl Default for EmbedSpec {
    fn default() -> Self {
        EmbedSpec { enabled: false, k: 10, dup_threshold: 0.950,
                    min_sim: 0.550, min_share: 0.250, prototype: "off".into() }
    }
}

/// 업무 분류 규칙 1개. cso_rule.yaml 의 KeywordRule 과 문법은 거의 같지만
/// (terms·exclude·weight), grade 대신 node(dc_id)를 갖는다.
#[derive(Clone)]
pub struct DoctypeRule {
    /// 규칙 식별자(진단·추적용). 매칭 로직에서는 읽지 않는다.
    #[allow(dead_code)]
    pub id: String,
    pub node: String,
    pub weight: String,
    /// 첫 비어있지 않은 줄에서만 찾을 문서종류어(재설계 6-3). 가장 강한 증거.
    pub title_terms: Vec<String>,
    /// 앞 head_chars 자(표제부)에서만 찾을 문서종류어(재설계 6-2).
    pub head_terms: Vec<String>,
    /// 이 규칙의 표제부 범위. None 이면 defaults.head_chars.
    pub head_chars: Option<usize>,
    /// 서식 필드어 세트(재설계 7장). 조건이 없으면 None.
    /// 구조 신호 정규식. 보조 전용이라 단독으로는 후보를 만들지 못한다.
    /// 본문 키워드. [재설계 8-2] 약한 증거로 격하 — 단독 채택 불가.
    pub terms: Vec<String>,
    /// terms 중 서로 다른 단어 최소 종수. None 이면 defaults.
    pub min_distinct: Option<usize>,
    /// terms 총 등장 건수 하한. None 이면 defaults.
    pub min_count: Option<u32>,
    pub exclude: Vec<String>,
    pub filename: Vec<String>,
    /// '/' 정규화 + 소문자화된 경로 조각(로드 시점에 정규화 — rules::PathRule.matches 와 동일 규약).
    /// node 가 doc_taxonomy.yaml 에서 status=0(미사용)이면 false — 매칭을 시도하지 않는다(T6).
    /// taxonomy 없이 로드했으면 항상 true(교차검증을 안 했으므로 판단 불가).
    pub active: bool,
}

/// doc_rule.yaml 전체. rules::RuleSet 이 security 축에서 하는 역할을 doctype 축에서 한다.
pub struct DocRuleSet {
    pub conflict: ConflictSpec,
    pub defaults: Defaults,
    pub embed: EmbedSpec,
    /// 업무분류 2차 BM25 설정(doc_rule.yaml 의 bm25:). 없으면 enabled=false — 예전과 같다.
    pub bm25: crate::bm25::Bm25Spec,
    /// 신호별 신뢰도 표(doc_rule.yaml 의 signals:). 정책 파일이 안 건드리면
    /// None — 채점 쪽이 코드 기본값(doctype::sig_conf)을 그대로 쓴다.
    pub signals: Option<crate::doctype::SignalTable>,
    pub version: String,
    pub rules: Vec<DoctypeRule>,
    /// 로드 중 발견한 경고(T6·T12 등 — 로드를 막지 않는 문제).
    pub warnings: Vec<String>,
    /// 분류체계 제목에서 유도한 핵어 사전(설계서 7장 · 13장 D1).
    /// taxonomy 없이 로드했거나 쓸 만한 제목이 없으면 비어 있다(그 신호만 끄고 계속).
    pub head_lexicon: std::collections::HashMap<String, crate::taxhead::HeadEntry>,
    /// 핵어 자리를 보기 전에 걷어낼 잡음 꼬리 말(core 사전의 noise_tails).
    pub head_noise: Vec<String>,
    /// 자동 생성 규칙 파일이면 규칙 줄의 path·path_ids 로 다시 지은 분류체계.
    /// 옛 규칙 파일이면 None — CLI 는 설계 7단계부터 옛 규칙 파일을 읽기 전에 막는다
    /// (옛 파서는 교차검증 시험을 위해 남겨 두었다).
    pub taxonomy: Option<Taxonomy>,
}

impl DocRuleSet {
    /// 규칙 0건 규칙셋(시험 전용). 예전에는 doc_rule.yaml 이 없을 때 'seed 전파 전용'
    /// 모드로 축을 켜는 데 썼지만, 설계 7단계에서 그 모드를 없앴다(규칙 파일이 없으면
    /// 축을 끈다). 지금은 '규칙이 하나도 없을 때 스캔·전파가 어떻게 도나'를 시험할 때만 쓴다.
    #[cfg(test)]
    pub fn empty() -> Self {
        DocRuleSet {
            conflict: ConflictSpec::default(),
            defaults: Defaults::default(),
            embed: EmbedSpec::default(),
            bm25: crate::bm25::Bm25Spec::default(),
            signals: None,
            version: "none".into(),
            rules: Vec::new(),
            warnings: Vec::new(),
            head_lexicon: std::collections::HashMap::new(),
            taxonomy: None,
            head_noise: Vec::new(),
        }
    }

    /// 실제로 매칭에 쓸 규칙만(active=false 인 T6 규칙은 뺀다).
    pub fn active_rules(&self) -> Vec<&DoctypeRule> {
        self.rules.iter().filter(|r| r.active).collect()
    }
}

/// doc_rule.yaml 로드 실패 종류. rules::RulesError 와 같은 구분 원칙.
#[derive(Debug)]
pub enum DocRulesError {
    Read(String),
    Invalid(String),
}

impl std::fmt::Display for DocRulesError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            DocRulesError::Read(s) | DocRulesError::Invalid(s) => write!(f, "{}", s),
        }
    }
}

/// 검증 위반 1건. code 는 "T0"(구조)·"T2"(conflict 값)·"T5"(스냅샷에 없는 노드)·"T10"(top_n 설정).
pub(crate) struct DVio {
    code: &'static str,
    rule_id: Option<String>,
    field: &'static str,
    value: String,
    pub(crate) detail: String,
}

/// conflict: 값 검사(T2·T10). 정상이면 None.
/// CLI 의 --conflict 실행 시 덮어쓰기도 이 함수를 그대로 쓴다 — doc_rule.yaml 에
/// 적을 때와 실행 시 덮어쓸 때가 항상 같은 기준으로 걸러지게 하기 위해서다(6-5).
pub(crate) fn check_conflict(raw: &Value) -> Option<DVio> {
    if raw.is_null() {
        return None;
    }
    if let Some(v) = raw.as_str() {
        if v == "all" {
            return None;
        }
        if v == "top_n" {
            return Some(DVio { code: "T10", rule_id: None, field: "conflict", value: v.into(),
                detail: "top_n 은 n 값 없이 쓸 수 없습니다".into() });
        }
        return Some(DVio { code: "T2", rule_id: None, field: "conflict", value: v.into(),
            detail: format!(
                "conflict 값이 올바르지 않습니다(taxonomy 축엔 서열이 없어 max 는 쓸 수 없습니다) — {} 중 하나여야 합니다",
                CONFLICT_STRATEGIES.join(" 또는 ")) });
    }
    if raw.is_object() {
        let strategy = raw.get("strategy").and_then(|v| v.as_str()).unwrap_or("");
        if !CONFLICT_STRATEGIES.contains(&strategy) {
            return Some(DVio { code: "T2", rule_id: None, field: "conflict.strategy", value: strategy.into(),
                detail: format!("conflict.strategy 는 {} 중 하나여야 합니다", CONFLICT_STRATEGIES.join(" 또는 ")) });
        }
        if strategy == "top_n" {
            let n = raw.get("n").and_then(|v| v.as_i64());
            if n.map_or(true, |v| v < 1) {
                let shown = raw.get("n").map(|v| v.to_string()).unwrap_or_else(|| "(없음)".into());
                return Some(DVio { code: "T10", rule_id: None, field: "conflict.n", value: shown,
                    detail: "top_n 은 n 이 1 이상의 정수여야 합니다".into() });
            }
        }
        return None;
    }
    Some(DVio { code: "T2", rule_id: None, field: "conflict", value: raw.to_string(),
        detail: "conflict 값의 형식이 올바르지 않습니다".into() })
}

/// conflict: 값 파싱(검증 통과분만 들어온다는 전제).
pub(crate) fn parse_conflict(raw: &Value) -> ConflictSpec {
    if raw.is_null() {
        return ConflictSpec::default();
    }
    if let Some(v) = raw.as_str() {
        return ConflictSpec { strategy: v.to_string(), n: None, min_confidence: None };
    }
    if raw.is_object() {
        return ConflictSpec {
            strategy: raw.get("strategy").and_then(|v| v.as_str()).unwrap_or("all").to_string(),
            n: raw.get("n").and_then(|v| v.as_i64()),
            min_confidence: raw.get("min_confidence").and_then(|v| v.as_f64()),
        };
    }
    ConflictSpec::default()
}

/// defaults: 블록 검사·파싱(T20). 숫자여야 할 자리에 다른 게 오거나 범위를
/// 벗어나면 스캔 단계에서 조용히 이상해지는 대신 로드 시점에 막는다 —
/// 임계값 하나가 틀리면 전체 분류 결과가 통째로 틀어진다.
pub(crate) fn parse_defaults(raw: &Value) -> (Defaults, Vec<DVio>) {
    let mut d = Defaults::default();
    if raw.is_null() {
        return (d, vec![]);
    }
    if !raw.is_object() {
        return (d, vec![DVio { code: "T20", rule_id: None, field: "defaults",
            value: raw.to_string(), detail: "defaults 는 매핑(mapping)이어야 합니다".into() }]);
    }
    let mut out: Vec<DVio> = vec![];

    // (필드, 정수여야 하는가, 하한, 상한). 상한이 없으면 f64::INFINITY.
    // bool 은 숫자로 보지 않는다 — YAML 의 true 가 1 로 새어 들어오면 안 된다.
    fn num(raw: &Value, key: &'static str, is_int: bool, lo: f64, hi: f64,
           out: &mut Vec<DVio>) -> Option<f64> {
        let v = raw.get(key)?;
        if v.is_boolean() || !v.is_number() {
            out.push(DVio { code: "T20", rule_id: None, field: key, value: v.to_string(),
                detail: "숫자여야 합니다".into() });
            return None;
        }
        let f = v.as_f64().unwrap_or(f64::NAN);
        if is_int && f.fract() != 0.0 {
            out.push(DVio { code: "T20", rule_id: None, field: key, value: v.to_string(),
                detail: "정수여야 합니다".into() });
            return None;
        }
        if f < lo || f > hi {
            let rng = if hi.is_finite() { format!("{} 이상 {} 이하", lo, hi) }
                      else { format!("{} 이상", lo) };
            out.push(DVio { code: "T20", rule_id: None, field: key, value: v.to_string(),
                detail: format!("{}여야 합니다", rng) });
            return None;
        }
        Some(f)
    }

    if let Some(v) = num(raw, "head_chars", true, 1.0, f64::INFINITY, &mut out) { d.head_chars = v as usize; }
    if let Some(v) = num(raw, "min_distinct", true, 1.0, f64::INFINITY, &mut out) { d.min_distinct = v as usize; }
    if let Some(v) = num(raw, "min_count", true, 1.0, f64::INFINITY, &mut out) { d.min_count = v as u32; }
    if let Some(v) = num(raw, "t_low", false, 0.0, 1.0, &mut out) { d.t_low = v; }
    if let Some(v) = num(raw, "t_seed", false, 0.0, 1.0, &mut out) { d.t_seed = v; }
    if let Some(v) = num(raw, "embed_cap", false, 0.0, 1.0, &mut out) { d.embed_cap = v; }

    // 값 하나하나가 정상이어도 조합이 뒤집히면 "후보도 못 되는 점수로 기준 문서를
    // 뽑는다" 같은 모순이 생긴다.
    // [2026-09-22] 판정에 쓰이지 않던 t_high(확정 후보선)를 없애 t_low ≤ t_seed 만 본다.
    // 옛 파일에 t_high 가 남아 있어도 모르는 칸이라 읽고 넘어간다(효과 없음).
    if d.t_low > d.t_seed {
        out.push(DVio { code: "T20", rule_id: None, field: "defaults.t_seed",
            value: d.t_seed.to_string(),
            detail: format!("t_seed 는 t_low({}) 이상이어야 합니다", d.t_low) });
    }
    (d, out)
}

/// embed: 블록 검사·파싱(T20). defaults 와 같은 원칙으로 로드 시점에 막는다.
//------------------------------------------------------------------
// signals: 블록 검사·파싱 (T20)
//=> 신호별 신뢰도 표를 정책 파일에서 덮어쓸 수 있게 한다. 그 값들은 코드 주석에
//   "실측 전 잠정치 · 검증셋 측정 후 재보정 대상"이라고 적혀 있는데, 소스에
//   박혀 있으면 재보정할 때마다 두 판을 고치고 exe 를 다시 빌드해야 한다.
//   embed: 블록을 정책 파일로 꺼낸 것과 같은 이유다.
//   파이썬 판 doc_rules._parse_signals 와 같은 규칙·같은 거절 사유를 낸다.
//
//   [부분만 적어도 된다] 적은 칸만 덮어쓰고 나머지는 코드 기본값을 쓴다.
//   [모르는 이름은 막는다] 'titel' 같은 오타를 조용히 무시하지 않는다.
//   [name·structure 는 세 값이 같아야 한다] 코드가 medium 칸만 쓰므로,
//   등급별로 다른 값을 받아 놓고 무시하는 상태를 만들지 않는다.
//
// -in: raw = data["signals"] 원본(없으면 Value::Null)
//
// -out: (Option<SignalTable>, Vec<DVio>) — 정책 파일이 안 건드렸으면 None
// -out: error = 없음
//------------------------------------------------------------------
pub(crate) fn parse_signals(raw: &Value) -> (Option<crate::doctype::SignalTable>, Vec<DVio>) {
    use crate::doctype::{default_cell, FLAT_SIGNALS, SIGNAL_NAMES};
    if raw.is_null() {
        return (None, vec![]);
    }
    if !raw.is_object() {
        return (None, vec![DVio { code: "T20", rule_id: None, field: "signals",
            value: raw.to_string(), detail: "signals 는 매핑(mapping)이어야 합니다".into() }]);
    }
    let mut out: Vec<DVio> = vec![];
    let mut tbl: crate::doctype::SignalTable = SIGNAL_NAMES
        .iter()
        .map(|n| ((*n).to_string(), default_cell(n)))
        .collect();

    let obj = match raw.as_object() {
        Some(m) => m,
        None => return (Some(tbl), out),
    };
    for (sig, cell) in obj.iter() {
        let sig = sig.as_str();
        if !SIGNAL_NAMES.contains(&sig) {
            out.push(DVio { code: "T20", rule_id: None, field: "signals",
                value: sig.to_string(),
                detail: format!("signals.{} — 모르는 신호 이름입니다. 쓸 수 있는 것: {}",
                                sig, SIGNAL_NAMES.join(" · ")) });
            continue;
        }
        if !cell.is_object() {
            out.push(DVio { code: "T20", rule_id: None, field: "signals",
                value: cell.to_string(),
                detail: format!("signals.{} — high/medium/low 를 담은 매핑이어야 합니다", sig) });
            continue;
        }
        let entry = tbl.get_mut(sig).expect("SIGNAL_NAMES 로 채워 둔 자리");
        for (w, v) in cell.as_object().expect("바로 위에서 매핑임을 확인했다").iter() {
            let w = w.as_str();
            if !["high", "medium", "low"].contains(&w) {
                out.push(DVio { code: "T20", rule_id: None, field: "signals",
                    value: w.to_string(),
                    detail: format!("signals.{}.{} — high | medium | low 중 하나여야 합니다", sig, w) });
                continue;
            }
            // bool 은 숫자로 읽히면 안 된다(파이썬 판과 같은 거절).
            let f = if v.is_boolean() { None } else { v.as_f64() };
            match f {
                Some(x) if (0.0..=1.0).contains(&x) => match w {
                    "high" => entry.high = x,
                    "low" => entry.low = x,
                    _ => entry.medium = x,
                },
                Some(_) => out.push(DVio { code: "T20", rule_id: None, field: "signals",
                    value: v.to_string(),
                    detail: format!("signals.{}.{} — 0.0 이상 1.0 이하여야 합니다", sig, w) }),
                None => out.push(DVio { code: "T20", rule_id: None, field: "signals",
                    value: v.to_string(),
                    detail: format!("signals.{}.{} — 숫자여야 합니다", sig, w) }),
            }
        }
        // 못 지킬 약속은 하지 않는다 — 등급별로 못 주는 신호는 그 자리에서 알린다.
        if FLAT_SIGNALS.contains(&sig)
            && !(entry.high == entry.medium && entry.medium == entry.low)
        {
            out.push(DVio { code: "T20", rule_id: None, field: "signals",
                value: format!("high={} medium={} low={}", entry.high, entry.medium, entry.low),
                detail: format!("signals.{} — 이 신호는 규칙의 weight 를 보지 않고 medium 값만 \
씁니다. high·medium·low 를 모두 같은 값으로 적으세요", sig) });
        }
    }
    (Some(tbl), out)
}

pub(crate) fn parse_embed(raw: &Value) -> (EmbedSpec, Vec<DVio>) {
    let mut e = EmbedSpec::default();
    if raw.is_null() {
        return (e, vec![]);
    }
    if !raw.is_object() {
        return (e, vec![DVio { code: "T20", rule_id: None, field: "embed",
            value: raw.to_string(), detail: "embed 는 매핑(mapping)이어야 합니다".into() }]);
    }
    let mut out: Vec<DVio> = vec![];
    if let Some(v) = raw.get("enabled") {
        match v.as_bool() {
            Some(b) => e.enabled = b,
            None => out.push(DVio { code: "T20", rule_id: None, field: "embed.enabled",
                value: v.to_string(), detail: "true 또는 false 여야 합니다".into() }),
        }
    }
    if let Some(v) = raw.get("k") {
        let k = if v.is_boolean() { None } else { v.as_i64() };
        match k {
            Some(x) if x >= 1 => e.k = x as usize,
            _ => out.push(DVio { code: "T20", rule_id: None, field: "embed.k",
                value: v.to_string(), detail: "1 이상의 정수여야 합니다".into() }),
        }
    }
    for key in ["dup_threshold", "min_sim", "min_share"] {
        if let Some(v) = raw.get(key) {
            let f = if v.is_boolean() { None } else { v.as_f64() };
            match f {
                Some(x) if (0.0..=1.0).contains(&x) => match key {
                    "dup_threshold" => e.dup_threshold = x,
                    "min_sim" => e.min_sim = x,
                    _ => e.min_share = x,
                },
                _ => out.push(DVio { code: "T20", rule_id: None, field: "embed",
                    value: v.to_string(),
                    detail: format!("{} 은 0.0~1.0 사이 숫자여야 합니다", key) }),
            }
        }
    }
    if let Some(v) = raw.get("prototype") {
        match v.as_str() {
            Some(p) if ["off", "l1_only", "all"].contains(&p) => e.prototype = p.to_string(),
            _ => out.push(DVio { code: "T20", rule_id: None, field: "embed.prototype",
                value: v.to_string(), detail: "off | l1_only | all 중 하나여야 합니다".into() }),
        }
    }
    (e, out)
}

/// 규칙 1개의 필드 검사(T20·T21). 숫자 필드가 쓸 수 있는 값인지 보고,
/// 이제 안 쓰는 필드(form·structure·paths)가 남아 있으면 알린다.
/// 로드 시점에 규칙 id 와 함께 잡아 줘야 어느 규칙이 범인인지 안다.
fn check_rule_fields(item: &Value) -> Vec<DVio> {
    let mut out: Vec<DVio> = vec![];
    let rid = item.get("id").and_then(|v| v.as_str()).map(|x| x.to_string());

    // [2026-09-07 제거] form·structure·paths 는 어느 규칙도 쓰지 않아 신호가
    // 한 번도 안 돌았고, 도는 코드와 섞여 있으면 읽는 사람이 매번 되짚어야 해서
    // 걷어냈다. 그런데 이 파서는 모르는 필드를 조용히 무시한다 — 옛 파일에
    // 남아 있으면 신호가 소리 없이 사라지는 셈이라, 사실대로 알린다.
    for gone in ["form", "structure", "paths"] {
        if let Some(v) = item.get(gone) {
            if !v.is_null() {
                out.push(DVio { code: "T21", rule_id: rid.clone(), field: "doctype_rules",
                    value: v.to_string(),
                    detail: format!("{} 는 더 이상 쓰지 않는 필드입니다(2026-09-07 제거) \n— 이 신호는 동작하지 않으므로 규칙에서 지우세요", gone) });
            }
        }
    }
    for key in ["min_distinct", "min_count", "head_chars"] {
        if let Some(v) = item.get(key) {
            let n = if v.is_boolean() { None } else { v.as_i64() };
            if n.map_or(true, |x| x < 1) {
                out.push(DVio { code: "T20", rule_id: rid.clone(), field: "",
                    value: format!("{}={}", key, v),
                    detail: "1 이상의 정수여야 합니다".into() });
            }
        }
    }
    out
}

/// doc_rule.yaml 원본(YAML→Value) 구조 검증 — T0·T2·T10. taxonomy 교차검증(T5)은
/// 별도 함수(cross_validate_nodes)의 몫이다 — Taxonomy 가 있어야 판단 가능하므로.
fn validate_doc_rule_data(data: &Value) -> Vec<DVio> {
    let mut out = vec![];
    if let Some(v) = check_conflict(data.get("conflict").unwrap_or(&Value::Null)) {
        out.push(v);
    }

    // 전역 블록(defaults/embed)은 규칙보다 먼저 본다 — 여기가 틀리면 모든 규칙의
    // 판정이 함께 틀어지므로 규칙 오류보다 먼저 보여 주는 게 낫다.
    out.extend(parse_defaults(data.get("defaults").unwrap_or(&Value::Null)).1);
    out.extend(parse_embed(data.get("embed").unwrap_or(&Value::Null)).1);
    out.extend(crate::bm25::parse_spec(data.get("bm25").unwrap_or(&Value::Null)).1.into_iter()
        .map(|(field, value, detail)| DVio { code: "T23", rule_id: None, field, value, detail }));
    out.extend(parse_signals(data.get("signals").unwrap_or(&Value::Null)).1);

    let raw_rules = match data.get("doctype_rules") {
        None => return out,
        Some(v) if v.is_null() => return out,
        Some(v) => match v.as_array() {
            Some(a) => a,
            None => {
                out.push(DVio { code: "T0", rule_id: None, field: "doctype_rules", value: "(목록 아님)".into(),
                    detail: "'doctype_rules' 가 목록(list)이 아닙니다".into() });
                return out;
            }
        },
    };

    let mut seen_ids: HashSet<String> = HashSet::new();
    for (idx, item) in raw_rules.iter().enumerate() {
        if !item.is_object() {
            out.push(DVio { code: "T0", rule_id: None, field: "", value: format!("doctype_rules[{}]", idx),
                detail: "항목이 매핑(mapping)이 아닙니다".into() });
            continue;
        }
        let rule_id = match item.get("id").and_then(|v| v.as_str()) {
            Some(v) if !v.is_empty() => v.to_string(),
            _ => {
                out.push(DVio { code: "T0", rule_id: None, field: "id", value: format!("doctype_rules[{}]", idx),
                    detail: "id 가 비어 있거나 문자열이 아닙니다".into() });
                continue;
            }
        };
        if seen_ids.contains(&rule_id) {
            out.push(DVio { code: "T0", rule_id: Some(rule_id.clone()), field: "id", value: rule_id.clone(),
                detail: "id 가 중복됩니다".into() });
        }
        seen_ids.insert(rule_id.clone());

        let node_ok = item.get("node").and_then(|v| v.as_str()).map(|v| !v.is_empty()).unwrap_or(false);
        if !node_ok {
            let shown = item.get("node").map(|v| v.to_string()).unwrap_or_else(|| "(없음)".into());
            out.push(DVio { code: "T0", rule_id: Some(rule_id.clone()), field: "node", value: shown,
                detail: "node 가 비어 있거나 문자열이 아닙니다".into() });
        }

        out.extend(check_rule_fields(item));
    }
    out
}

/// node 참조 교차검증 — T5(스냅샷에 없는 노드). validate_doc_rule_data 를 통과한
/// (id·node 가 유효한 문자열인) 항목만 들어온다는 전제.
fn cross_validate_nodes(raw_rules: &[Value], taxonomy: &Taxonomy) -> Vec<DVio> {
    let mut out = vec![];
    for item in raw_rules {
        let id = item.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let node = item.get("node").and_then(|v| v.as_str()).unwrap_or("");
        if !taxonomy.contains(node) {
            out.push(DVio { code: "T5", rule_id: Some(id), field: "node", value: node.to_string(),
                detail: "분류체계 스냅샷(doc_taxonomy.yaml)에 없는 dc_id 입니다 — 노드가 삭제됐거나 스냅샷이 낡았을 수 있습니다".into() });
        }
    }
    out
}

/// 위반 목록 → 사람이 읽는 보고문(rules::format_violations 와 같은 형식).
fn format_doc_rule_violations(path: &str, violations: &[DVio]) -> String {
    let title = |code: &str| match code {
        "T0" => "[T0] doc_rule.yaml 구조 오류",
        "T2" => "[T2] conflict 값이 올바르지 않음",
        "T5" => "[T5] 분류체계 스냅샷에 없는 노드 참조",
        "T10" => "[T10] top_n 설정이 올바르지 않음",
        "T20" => "[T20] 임계값·수치 설정이 올바르지 않음",
        "T21" => "[T21] 이제 쓰지 않는 규칙 필드",
        _ => "[T22] (사용하지 않음)",
    };
    let mut lines = vec![
        format!("[규칙셋 오류] {} — 검증 실패 {}건. doctype 축을 로드하지 않았습니다.", path, violations.len()),
        String::new(),
    ];
    for code in ["T0", "T2", "T5", "T10", "T20", "T21", "T22"] {
        let group: Vec<&DVio> = violations.iter().filter(|v| v.code == code).collect();
        if group.is_empty() { continue; }
        lines.push(format!("  {}", title(code)));
        for v in group {
            let where_ = match &v.rule_id { Some(id) => format!("id={}", id), None => "(구조)".into() };
            let field = if v.field.is_empty() { String::new() } else { format!(".{}", v.field) };
            lines.push(format!("    {}{} = {:?}  — {}", where_, field, v.value, v.detail));
        }
        lines.push(String::new());
    }
    lines.push(format!("  고치는 법: {} 를 열어 위 항목을 고치세요.", path));
    lines.join("\n")
}

/// doc_rule.yaml 고정 파일명.
pub fn default_doc_rule_filename() -> &'static str {
    "doc_rule.yaml"
}

/// 새 doc_rule.yaml 의 머리 부분을 가져올 본보기 파일 이름(화면·CLI 공용 규약).
pub const TEMPLATE_NAME: &str = "doc_rule_template.yaml";

/// 본보기(doc_rule_template.yaml)를 읽는다. 찾는 순서는 '가까운 곳부터'다.
///   1) 만들려는 doc_rule.yaml 과 같은 폴더
///   2) exe 옆
///   3) CSOCLASSIFY_POLICY_DIR
/// 하나도 없으면 빈 매핑 — 본보기가 없다고 골격 생성이 실패하면 안 된다.
pub fn load_scaffold_template(near: Option<&std::path::Path>) -> serde_yaml::Mapping {
    let mut cands: Vec<std::path::PathBuf> = vec![];
    if let Some(n) = near {
        if let Some(dir) = n.parent() {
            cands.push(dir.join(TEMPLATE_NAME));
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            cands.push(dir.join(TEMPLATE_NAME));
        }
    }
    if let Ok(d) = std::env::var("CSOCLASSIFY_POLICY_DIR") {
        cands.push(std::path::Path::new(&d).join(TEMPLATE_NAME));
    }
    for c in cands {
        if !c.is_file() { continue; }
        let text = match std::fs::read_to_string(&c) { Ok(t) => t, Err(_) => continue };
        match serde_yaml::from_str::<serde_yaml::Value>(&text) {
            Ok(serde_yaml::Value::Mapping(m)) => return m,
            _ => continue,
        }
    }
    serde_yaml::Mapping::new()
}

/// doc_rule.yaml 을 읽어 DocRuleSet 으로. taxonomy 를 주면 T5·T6·T12 교차검증까지
/// 함께 한다(없으면(None) node 값을 그대로 믿고 로드 — Python load_doc_rules 와 동일 규약).
pub fn load_doc_rules(path: &std::path::Path, taxonomy: Option<&Taxonomy>) -> Result<DocRuleSet, DocRulesError> {
    let text = std::fs::read_to_string(path).map_err(|e| {
        DocRulesError::Read(format!(
            "업무분류 규칙셋({})을 찾을 수 없습니다: {} ({})",
            default_doc_rule_filename(), path.display(), e))
    })?;
    let yv: serde_yaml::Value = serde_yaml::from_str(&text)
        .map_err(|e| DocRulesError::Read(format!("YAML 파싱 실패: {}", e)))?;
    let data: Value = serde_json::to_value(&yv)
        .map_err(|e| DocRulesError::Read(format!("변환 실패: {}", e)))?;

    let raw_rules: Vec<Value> = data.get("doctype_rules").and_then(|v| v.as_array()).cloned().unwrap_or_default();

    // 자동 생성 규칙 파일이면 분류체계를 규칙 줄에서 다시 짓는다 — doc_taxonomy.yaml 을
    // 따로 읽지 않는다(설계서 9장). 받은 taxonomy 가 있어도 규칙 쪽을 믿는다.
    let generated = crate::docbuild::is_generated(&data);
    let gen_tax: Option<Taxonomy> = if generated {
        let rules_obj: Vec<Value> = raw_rules.iter().filter(|r| r.is_object()).cloned().collect();
        match crate::axes::taxonomy_from_rules(&rules_obj, &s(&data, "version", "")) {
            Ok(t) => Some(t),
            Err(e) => return Err(DocRulesError::Invalid(format!(
                "[규칙셋 오류] {} — 검증 실패 1건. doctype 축을 로드하지 않았습니다.\n\n  [G1] 자동 생성 규칙의 경로(path·path_ids)가 맞지 않음\n    {}",
                path.display(), e))),
        }
    } else { None };
    let taxonomy: Option<&Taxonomy> = if generated { gen_tax.as_ref() } else { taxonomy };

    let mut violations = validate_doc_rule_data(&data);
    if violations.is_empty() {
        if let Some(t) = taxonomy {
            violations.extend(cross_validate_nodes(&raw_rules, t));
        }
    }
    if !violations.is_empty() {
        return Err(DocRulesError::Invalid(
            format_doc_rule_violations(&path.display().to_string(), &violations)));
    }

    let conflict = parse_conflict(data.get("conflict").unwrap_or(&Value::Null));
    // 검증을 이미 통과했으므로 여기서 나오는 위반 목록은 버린다(값만 필요).
    let defaults = parse_defaults(data.get("defaults").unwrap_or(&Value::Null)).0;
    let embed = parse_embed(data.get("embed").unwrap_or(&Value::Null)).0;
    let bm25 = crate::bm25::parse_spec(data.get("bm25").unwrap_or(&Value::Null)).0;
    let signals = parse_signals(data.get("signals").unwrap_or(&Value::Null)).0;

    let mut warnings = vec![];
    let mut rules = vec![];
    let mut referenced: HashSet<String> = HashSet::new();
    for item in &raw_rules {
        let id = s(item, "id", "");
        let node = s(item, "node", "");
        let mut active = true;
        if let Some(t) = taxonomy {
            if let Some(tn) = t.get(&node) {
                referenced.insert(node.clone());
                if !tn.active() {
                    active = false;
                    warnings.push(format!(
                        "[T6] doctype_rules[{}].node={:?} ({}) 는 미사용(status=0) 노드입니다 — 이 규칙은 비활성 처리됩니다",
                        id, node, t.path(&node).unwrap_or_default()));
                }
            }
        }
        rules.push(DoctypeRule {
            id, node,
            weight: s(item, "weight", "medium"),
            title_terms: strvec(item, "title_terms"),
            head_terms: strvec(item, "head_terms"),
            head_chars: item.get("head_chars").and_then(|v| v.as_u64()).map(|v| v as usize),
            min_distinct: item.get("min_distinct").and_then(|v| v.as_u64()).map(|v| v as usize),
            min_count: item.get("min_count").and_then(|v| v.as_u64()).map(|v| v as u32),
            terms: strvec(item, "terms"),
            exclude: strvec(item, "exclude"),
            filename: strvec(item, "filename"),
            active,
        });
    }

    // 생성 규칙은 규칙 줄에서 체계를 지었으므로 '규칙 없는 분류'(T12)가 있을 수 없다.
    if let Some(t) = taxonomy.filter(|_| !generated) {
        for node in t.active_nodes() {
            if referenced.contains(&node.dc_id) { continue; }
            // '서랍'(최상위이면서 자식이 있는 노드)에는 규칙을 걸지 않는 것이 설계다.
            // 규칙을 만드는 쪽(docvocab)과 반드시 같은 기준이어야 한다 — 어긋나면
            // "규칙을 만들어 주지도 않으면서 경고만 하는" 상태가 된다(Python 판과 동일).
            let has_kids = t.children_of(Some(&node.dc_id)).iter().any(|c| c.active());
            if crate::docvocab::is_drawer(node.parent.is_some(), has_kids) { continue; }
            warnings.push(format!(
                "[T12] {}({}) 를 참조하는 규칙이 없습니다 — 이 분류로는 자동분류되는 문서가 없습니다",
                node.dc_id, t.path(&node.dc_id).unwrap_or_default()));
        }
    }
    if rules.is_empty() {
        warnings.push("doctype_rules 가 비어 있어 doctype 축이 아무 문서도 분류하지 않습니다".into());
    }

    // 핵어 사전은 로드할 때 한 번만 만든다(문서 수만큼 다시 만들면 비싸다).
    // 분류체계 파일이 없으면 이 신호만 끄고 계속한다(설계서 13-4 결정 5).
    let mut head_lexicon = std::collections::HashMap::new();
    let mut head_noise: Vec<String> = vec![];
    if generated {
        // 핵어는 만들 때 계산해 규칙마다 적어 두었다 — 판정 때 사전을 읽지 않는다.
        for item in &raw_rules {
            let heads: Vec<String> = item.get("heads").and_then(|v| v.as_array())
                .map(|a| a.iter().filter_map(|x| x.as_str()).map(|x| x.to_string()).collect())
                .unwrap_or_default();
            if heads.is_empty() { continue; }
            head_lexicon.insert(s(item, "node", ""), crate::taxhead::HeadEntry {
                title: s(item, "title", "").replace(' ', ""), heads });
        }
        head_noise = data.get("noise_tails").and_then(|v| v.as_array())
            .map(|a| a.iter().filter_map(|x| x.as_str()).map(|x| x.to_string()).collect())
            .unwrap_or_default();
        // 손으로 고친 흔적·입력이 바뀐 것을 알린다(판정은 막지 않는다).
        warnings.extend(crate::docbuild::check_generated(&data, path));
    } else if let Some(t) = taxonomy {
        let syn = crate::docvocab::load_synonyms(path);
        // ⓑ 말 단위 제외 · ⓒ 노드 단위 끄기 — 둘 다 doc_rule.yaml 에 적는다.
        let excludes: Vec<String> = data.get("taxonomy_title_exclude")
            .and_then(|v| v.as_array()).map(|a| a.iter()
                .filter_map(|x| x.as_str()).map(|x| x.to_string()).collect())
            .unwrap_or_default();
        let node_off: Vec<(String, String)> = data.get("taxonomy_node_off")
            .and_then(|v| v.as_array()).map(|a| a.iter().filter_map(|x| {
                let node = x.get("node").and_then(|v| v.as_str())?;
                let was = x.get("title_at_decision").and_then(|v| v.as_str()).unwrap_or("");
                Some((node.to_string(), was.to_string()))
            }).collect()).unwrap_or_default();
        let (lex, head_warnings) =
            crate::taxhead::build_head_lexicon(t, &syn, &excludes, &node_off);
        head_lexicon = lex;
        head_noise = syn.head_list("noise_tails");
        warnings.extend(head_warnings);
    }

    Ok(DocRuleSet { conflict, defaults, embed, bm25, signals,
                    version: s(&data, "version", "unknown"), rules, warnings,
                    head_lexicon, head_noise, taxonomy: gen_tax })
}

#[cfg(test)]
mod tests {
    use super::*;

    //------------------------------------------------------------------
    // signals: 블록 — 파이썬 판과 같은 규칙으로 받아들이고 같은 것을 막는다
    //=> 이 표가 두 판에서 갈리면 같은 문서에 다른 점수가 나오는데, 오류도
    //   경고도 없이 결과만 달라져 가장 알아채기 어렵다.
    //------------------------------------------------------------------
    //------------------------------------------------------------------
    // defaults 순서 검사 — t_low ≤ t_seed 만 본다(2026-09-22 t_high 제거)
    //=> t_high 는 판정에 쓰이지 않아 없앴다. 옛 파일에 남아 있어도 막지 않고,
    //   t_low 를 0.7 위로 올려도 t_seed 아래면 통과해야 한다. 파이썬 판과 같다.
    //------------------------------------------------------------------
    #[test]
    fn defaults_순서는_t_low_와_t_seed_만_본다() {
        // 옛 칸(t_high)이 남아 있어도, t_low 가 옛 t_high(0.7)보다 커도 통과
        let (d, vio) = parse_defaults(&serde_json::json!({"t_low": 0.75, "t_high": 0.7, "t_seed": 0.85}));
        assert!(vio.is_empty(), "{}", vio.len());
        assert_eq!(d.t_low, 0.75);
        // t_low 가 t_seed 보다 크면 T20
        let (_, vio) = parse_defaults(&serde_json::json!({"t_low": 0.9, "t_seed": 0.85}));
        assert_eq!(vio.len(), 1);
        assert_eq!(vio[0].code, "T20");
        assert_eq!(vio[0].field, "defaults.t_seed");
    }

    #[test]
    fn signals_블록을_파이썬과_같은_규칙으로_읽는다() {
        use crate::doctype::default_cell;

        // 없으면 None — 예전 doc_rule.yaml 이 그대로 돈다(하위호환).
        assert!(parse_signals(&serde_json::Value::Null).0.is_none());

        // 일부만 적으면 나머지는 코드 기본값이 남는다.
        let raw = serde_json::json!({"title": {"high": 0.90}});
        let (tbl, vio) = parse_signals(&raw);
        assert!(vio.is_empty(), "{}", vio.len());
        let tbl = tbl.unwrap();
        assert_eq!(tbl["title"].high, 0.90);
        assert_eq!(tbl["title"].medium, default_cell("title").medium);
        assert_eq!(tbl["head"], default_cell("head"));

        // 잘못 적은 것은 막는다 — 조용히 무시하면 "고쳤는데 왜 안 바뀌지"가 된다.
        for bad in [
            serde_json::json!({"title": {"high": 1.5}}),      // 범위 밖
            serde_json::json!({"titel": {"high": 0.8}}),      // 신호 이름 오타
            serde_json::json!({"title": {"highest": 0.8}}),   // 등급 키 오타
            serde_json::json!({"title": {"high": "세게"}}),    // 숫자 아님
            serde_json::json!({"title": {"high": true}}),     // bool 은 숫자가 아니다
            serde_json::json!({"title": 0.8}),                // 매핑이 아님
        ] {
            assert!(!parse_signals(&bad).1.is_empty(), "안 막혔다: {}", bad);
        }

        // name 은 medium 칸만 쓰이므로 세 값이 같아야 한다.
        let bad = serde_json::json!({"name": {"high": 0.9}});
        assert!(!parse_signals(&bad).1.is_empty(), "name: 세 값이 달라도 통과했다");
        let ok = serde_json::json!({"name": {"high": 0.4, "medium": 0.4, "low": 0.4}});
        assert!(parse_signals(&ok).1.is_empty(), "name: 같은 값인데 막혔다");

        // form·structure·path 는 2026-09-07 에 걷어냈다 — 이제 아는 이름이
        // 아니므로 오타와 같은 갈래로 막힌다. 쓸 수 있는 목록을 함께 보여 주므로
        // 사람은 그 자리에서 무엇을 적어야 하는지 안다.
        for sig in ["form", "structure", "path"] {
            let v = serde_json::json!({ sig: {"high": 0.5, "medium": 0.5, "low": 0.5} });
            let vio = parse_signals(&v).1;
            assert_eq!(vio.len(), 1, "{}: 안 막혔다", sig);
            assert!(vio[0].detail.contains("모르는 신호"), "{}: {}", sig, vio[0].detail);
            assert!(vio[0].detail.contains("title"), "{}: 쓸 수 있는 목록이 없다", sig);
        }
    }

    //------------------------------------------------------------------
    // 옛 규칙에 남은 form·structure·paths 필드는 조용히 무시하지 않는다
    //=> 이 파서는 모르는 필드를 그냥 넘긴다. 그대로 두면 예전에 그 필드를 적어 둔
    //   파일에서 신호가 소리 없이 사라진다 — 사실대로 알려서 규칙에서 지우게 한다.
    //------------------------------------------------------------------
    #[test]
    fn 옛_규칙_필드는_사유를_알린다() {
        for (field, value) in [
            ("form", serde_json::json!({"all_of": ["갑", "을"]})),
            ("structure", serde_json::json!([r"제\s*\d+\s*조"])),
            ("paths", serde_json::json!(["/계약/"])),
        ] {
            let item = serde_json::json!({
                "id": "x", "node": "DC_001", "weight": "medium",
                "title_terms": ["가"], field: value });
            let vio = check_rule_fields(&item);
            assert_eq!(vio.len(), 1, "{}: 조용히 무시됐다", field);
            assert!(vio[0].detail.contains("더 이상 쓰지 않는 필드"),
                    "{}: {}", field, vio[0].detail);
        }
    }
    use crate::axes::{Taxonomy, TaxonomyNode};

    // cargo test 는 기본적으로 테스트를 여러 스레드에서 병렬 실행한다. 여러
    // 테스트 함수가 build_test_taxonomy 를 부르며 같은 임시파일 이름을 쓰면
    // 한 스레드의 remove_file 이 다른 스레드의 read 보다 먼저 끝나 "파일을
    // 찾을 수 없음"으로 흔들린다 — 호출마다 증가하는 카운터로 유일하게 만든다.
    static TMP_COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    fn unique_tmp(prefix: &str) -> std::path::PathBuf {
        let n = TMP_COUNTER.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        std::env::temp_dir().join(format!("{}_{}_{}.yaml", prefix, std::process::id(), n))
    }

    fn mk_data(rules: Value, conflict: Option<Value>) -> Value {
        let mut m = serde_json::Map::new();
        m.insert("doctype_rules".into(), rules);
        if let Some(c) = conflict { m.insert("conflict".into(), c); }
        Value::Object(m)
    }

    fn mk_taxonomy() -> Taxonomy {
        build_test_taxonomy(vec![
            TaxonomyNode { dc_id: "A".into(), parent: None, order: 1, title: "루트A".into(), status: 1 },
            TaxonomyNode { dc_id: "A1".into(), parent: Some("A".into()), order: 1, title: "자식A1".into(), status: 1 },
            TaxonomyNode { dc_id: "A2".into(), parent: Some("A".into()), order: 2, title: "자식A2(미사용)".into(), status: 0 },
        ])
    }

    // Taxonomy::build 는 private 이라 테스트 전용 소형 헬퍼로 대체 생성.
    fn build_test_taxonomy(nodes: Vec<TaxonomyNode>) -> Taxonomy {
        // axes.rs 에 테스트 목적의 public 생성자가 없으므로, 스냅샷 값(JSON)을 만들어
        // taxonomy_from_value 로 짓는다 — 규칙 자동 생성이 쓰는 것과 같은 길이다.
        let arr: Vec<Value> = nodes.iter().map(|n| serde_json::json!({
            "dc_id": n.dc_id, "parent": n.parent, "order": n.order,
            "title": n.title, "status": n.status })).collect();
        let data = serde_json::json!({"taxonomy": {
            "source": "t", "exported_at": "20260824000000", "node_count": arr.len(), "nodes": arr }});
        crate::axes::taxonomy_from_value(&data, "test").unwrap()
    }

    #[test]
    fn conflict_생략시_기본값_all() {
        let data = mk_data(serde_json::json!([]), None);
        assert!(validate_doc_rule_data(&data).is_empty());
    }

    #[test]
    fn doctype_rules_생략은_위반이_아니다() {
        let data: Value = serde_json::json!({});
        assert!(validate_doc_rule_data(&data).is_empty());
    }

    #[test]
    fn doctype_rules가_목록이_아니면_t0() {
        let data: Value = serde_json::json!({"doctype_rules": {"id": "매핑"}});
        let v = validate_doc_rule_data(&data);
        assert_eq!(v.iter().map(|x| x.code).collect::<Vec<_>>(), vec!["T0"]);
    }

    #[test]
    fn id_없으면_t0() {
        let data = mk_data(serde_json::json!([{"node": "A"}]), None);
        assert_eq!(validate_doc_rule_data(&data).iter().map(|x| x.code).collect::<Vec<_>>(), vec!["T0"]);
    }

    #[test]
    fn node_없으면_t0() {
        let data = mk_data(serde_json::json!([{"id": "dt_a"}]), None);
        assert_eq!(validate_doc_rule_data(&data).iter().map(|x| x.code).collect::<Vec<_>>(), vec!["T0"]);
    }

    #[test]
    fn id_중복은_t0() {
        let data = mk_data(serde_json::json!([{"id": "dt_a", "node": "A"}, {"id": "dt_a", "node": "A1"}]), None);
        assert_eq!(validate_doc_rule_data(&data).iter().map(|x| x.code).collect::<Vec<_>>(), vec!["T0"]);
    }

    #[test]
    fn conflict_max는_t2() {
        let data = mk_data(serde_json::json!([]), Some(serde_json::json!("max")));
        assert_eq!(validate_doc_rule_data(&data).iter().map(|x| x.code).collect::<Vec<_>>(), vec!["T2"]);
    }

    #[test]
    fn top_n_n없이_쓰면_t10() {
        let data = mk_data(serde_json::json!([]), Some(serde_json::json!("top_n")));
        assert_eq!(validate_doc_rule_data(&data).iter().map(|x| x.code).collect::<Vec<_>>(), vec!["T10"]);
        let data2 = mk_data(serde_json::json!([]), Some(serde_json::json!({"strategy": "top_n"})));
        assert_eq!(validate_doc_rule_data(&data2).iter().map(|x| x.code).collect::<Vec<_>>(), vec!["T10"]);
    }

    #[test]
    fn top_n_n이_0이면_t10() {
        let data = mk_data(serde_json::json!([]), Some(serde_json::json!({"strategy": "top_n", "n": 0})));
        assert_eq!(validate_doc_rule_data(&data).iter().map(|x| x.code).collect::<Vec<_>>(), vec!["T10"]);
    }

    #[test]
    fn 유효한_top_n은_통과() {
        let data = mk_data(serde_json::json!([]), Some(serde_json::json!({"strategy": "top_n", "n": 3, "min_confidence": 0.6})));
        assert!(validate_doc_rule_data(&data).is_empty());
        let spec = parse_conflict(data.get("conflict").unwrap());
        assert_eq!(spec.strategy, "top_n");
        assert_eq!(spec.n, Some(3));
        assert_eq!(spec.min_confidence, Some(0.6));
    }

    #[test]
    fn taxonomy_없으면_존재하지_않는_node도_그대로_로드() {
        let tmp = unique_tmp("cso_test_docrule");
        std::fs::write(&tmp, "doctype_rules:\n- id: dt_a\n  node: 없는노드\n  terms: [x]\n").unwrap();
        let drs = load_doc_rules(&tmp, None).unwrap();
        let _ = std::fs::remove_file(&tmp);
        assert_eq!(drs.rules[0].node, "없는노드");
        assert!(drs.rules[0].active);
    }

    #[test]
    fn taxonomy_주면_없는_node는_t5() {
        let taxonomy = mk_taxonomy();
        let tmp = unique_tmp("cso_test_docrule2");
        std::fs::write(&tmp, "doctype_rules:\n- id: dt_a\n  node: 없는노드\n  terms: [x]\n").unwrap();
        let err = load_doc_rules(&tmp, Some(&taxonomy)).err().unwrap();
        let _ = std::fs::remove_file(&tmp);
        match err { DocRulesError::Invalid(msg) => assert!(msg.contains("T5")), _ => panic!("Invalid 여야 함") }
    }

    #[test]
    fn 미사용_노드_참조는_t6_경고와_active_false() {
        let taxonomy = mk_taxonomy();
        let tmp = unique_tmp("cso_test_docrule3");
        std::fs::write(&tmp, "doctype_rules:\n- id: dt_a1\n  node: A1\n  terms: [x]\n- id: dt_a2\n  node: A2\n  terms: [y]\n").unwrap();
        let drs = load_doc_rules(&tmp, Some(&taxonomy)).unwrap();
        let _ = std::fs::remove_file(&tmp);
        let a1 = drs.rules.iter().find(|r| r.id == "dt_a1").unwrap();
        let a2 = drs.rules.iter().find(|r| r.id == "dt_a2").unwrap();
        assert!(a1.active);
        assert!(!a2.active);
        assert_eq!(drs.active_rules().len(), 1);
        assert!(drs.warnings.iter().any(|w| w.starts_with("[T6]")));
    }

    /// 참조 안 된 '잎'만 T12 경고 — 대분류(자식 있는 노드)는 경고하지 않는다.
    /// 규칙은 가장 아래 분류에만 거는 것이 설계라, 대분류에 규칙이 없는 것은 정상이다.
    /// 여기에 경고를 내면 실행마다 고칠 수 없는 경고가 쌓여 진짜 경고를 묻는다.
    #[test]
    fn 참조되지_않는_잎만_t12_경고() {
        // 루트 A 아래 잎이 둘(A1·A3). 규칙은 A1 만 참조한다.
        let taxonomy = build_test_taxonomy(vec![
            TaxonomyNode { dc_id: "A".into(), parent: None, order: 1, title: "루트A".into(), status: 1 },
            TaxonomyNode { dc_id: "A1".into(), parent: Some("A".into()), order: 1, title: "자식A1".into(), status: 1 },
            TaxonomyNode { dc_id: "A3".into(), parent: Some("A".into()), order: 3, title: "자식A3".into(), status: 1 },
        ]);
        let tmp = unique_tmp("cso_test_docrule4");
        std::fs::write(&tmp, "doctype_rules:\n- id: dt_a1\n  node: A1\n  terms: [x]\n").unwrap();
        let drs = load_doc_rules(&tmp, Some(&taxonomy)).unwrap();
        let _ = std::fs::remove_file(&tmp);
        let t12: Vec<&String> = drs.warnings.iter().filter(|w| w.starts_with("[T12]")).collect();
        assert!(t12.iter().any(|w| w.contains("A3(")), "{:?}", t12);
        assert!(!t12.iter().any(|w| w.starts_with("[T12] A(")), "{:?}", t12);
    }

    #[test]
    fn 빈_doctype_rules는_경고를_남긴다() {
        let tmp = unique_tmp("cso_test_docrule5");
        std::fs::write(&tmp, "doctype_rules: []\n").unwrap();
        let drs = load_doc_rules(&tmp, None).unwrap();
        let _ = std::fs::remove_file(&tmp);
        assert!(drs.rules.is_empty());
        assert_eq!(drs.warnings.len(), 1);
    }
}
