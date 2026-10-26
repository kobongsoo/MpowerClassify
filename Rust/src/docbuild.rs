//! 업무분류 규칙 파일 자동 생성(Python classify/docbuild.py 포팅).
//!
//! doc_rule.yaml 을 '사람이 고치는 파일'에서 '만들어지는 결과물'로 바꾼다.
//! 입력(체계 JSON · 유의어 사전 · 본보기 · doc_rule.local.yaml)이 같으면 결과가
//! Python 판과 글자까지 같다 — 값은 한 줄 JSON 으로 적어 PyYAML·serde_yaml 의
//! 따옴표·줄바꿈 규칙 차이를 피한다.
//! 설계: plan/업무분류-규칙파일-자동생성-설계-20260922.html

use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::axes;
use crate::docvocab;
use crate::taxhead;

/// 사람이 고치는 조정 파일 이름(정책 폴더, doc_rule.yaml 옆).
pub const RULE_LOCAL: &str = "doc_rule.local.yaml";
/// 규칙의 말 칸 — clear/add/remove 가 가리킬 수 있는 칸.
pub const CELLS: [&str; 5] = ["title_terms", "head_terms", "terms", "filename", "exclude"];
/// 규칙마다 덮을 수 있는 숫자 칸.
const NUM_FIELDS: [&str; 3] = ["head_chars", "min_count", "min_distinct"];
/// 본보기에서 가져와 local 의 settings 로 덮는 전체 설정 칸.
const SETTING_SECTIONS: [&str; 5] = ["conflict", "defaults", "signals", "embed", "bm25"];
const GENERATED_BY: &str = "MpowerClassify --build-doc-rule";

/// 만들어진 파일 맨 위 안내 — Python 판 GEN_HEADER 와 글자까지 같아야 한다.
pub const GEN_HEADER: &str = "# ────────────────────────────────────────────────────────────────\n\
# 자동 생성 파일 — 고치지 마세요. 다시 만들면 고친 것이 사라집니다.\n\
# 회사별 조정은 doc_rule.local.yaml(분류별 조정·설정)과\n\
# synonyms/doc_synonyms.local.yaml(회사 말)에 적고 다시 만드세요.\n\
# 다시 만들기: MpowerClassify --build-doc-rule\n\
# ────────────────────────────────────────────────────────────────\n";

/// 생성 실패 이유. 종료코드 구분을 위해 종류를 나눈다.
pub enum BuildError {
    /// 체계 JSON 을 못 읽음·모양 오류·체계 검증 실패(export_input_invalid)
    Export(String),
    /// doc_rule.local.yaml 검증 실패(doc_rules_invalid)
    Local(String),
}

/// 위반 한 줄 — "[코드] 자리 = 값 — 설명".
fn lv(out: &mut Vec<String>, code: &str, where_: &str, value: &Value, detail: &str) {
    out.push(format!("[{}] {} = {} — {}", code, where_, value, detail));
}

fn is_str_list(v: &Value) -> bool {
    v.as_array().map(|a| a.iter().all(|x| x.is_string())).unwrap_or(false)
}

/// doc_rule.local.yaml 내용 검증(Python validate_rule_local 과 같은 코드·같은 기준).
/// 모르는 칸·틀린 모양을 전부 모은다(첫 오류에서 멈추지 않는다).
pub fn validate_rule_local(data: &Value) -> Vec<String> {
    let mut out = vec![];
    if data.is_null() { return out; }
    let top = match data.as_object() {
        Some(m) => m,
        None => { lv(&mut out, "L0", "(최상위)", data, "매핑(mapping)이어야 합니다"); return out; }
    };
    let known = ["head", "industry", "rules", "settings", "version"];
    for (k, v) in top {
        if !known.contains(&k.as_str()) {
            lv(&mut out, "L0", k, v, &format!("모르는 칸입니다(쓸 수 있는 칸: {})", known.join(", ")));
        }
    }
    if let Some(ind) = top.get("industry") {
        if !(ind.is_string() || is_str_list(ind)) {
            lv(&mut out, "L1", "industry", ind, "업종 이름 하나(문자열) 또는 목록이어야 합니다");
        }
    }
    if let Some(st) = top.get("settings") {
        match st.as_object() {
            None => lv(&mut out, "L2", "settings", st, "매핑이어야 합니다"),
            Some(m) => for (k, v) in m {
                if !SETTING_SECTIONS.contains(&k.as_str()) {
                    lv(&mut out, "L2", &format!("settings.{}", k), v,
                       &format!("모르는 칸입니다(쓸 수 있는 칸: {})", SETTING_SECTIONS.join(", ")));
                } else if k != "conflict" && !v.is_object() {
                    lv(&mut out, "L2", &format!("settings.{}", k), v, "매핑이어야 합니다");
                }
            },
        }
    }
    let mut rule_keys: Vec<&str> = vec!["title_at_decision", "enabled", "weight", "clear", "add", "remove", "why"];
    rule_keys.extend(NUM_FIELDS.iter());
    let mut sorted_keys = rule_keys.clone();
    sorted_keys.sort();
    if let Some(rules) = top.get("rules") {
        match rules.as_object() {
            None => lv(&mut out, "L3", "rules", rules, "dc_id 를 열쇠로 하는 매핑이어야 합니다"),
            Some(m) => for (dc_id, r) in m {
                let where_ = format!("rules.{}", dc_id);
                let rm = match r.as_object() {
                    Some(x) => x,
                    None => { lv(&mut out, "L3", &where_, r, "매핑이어야 합니다"); continue; }
                };
                for (k, v) in rm {
                    if !rule_keys.contains(&k.as_str()) {
                        lv(&mut out, "L3", &format!("{}.{}", where_, k), v,
                           &format!("모르는 칸입니다(쓸 수 있는 칸: {})", sorted_keys.join(", ")));
                    }
                }
                if let Some(v) = rm.get("enabled") {
                    if !v.is_boolean() { lv(&mut out, "L3", &format!("{}.enabled", where_), v, "true 또는 false 여야 합니다"); }
                }
                for k in ["title_at_decision", "why"] {
                    if let Some(v) = rm.get(k) {
                        if !v.is_string() { lv(&mut out, "L3", &format!("{}.{}", where_, k), v, "문자열이어야 합니다"); }
                    }
                }
                if let Some(v) = rm.get("weight") {
                    if !matches!(v.as_str(), Some("high") | Some("medium") | Some("low")) {
                        lv(&mut out, "L5", &format!("{}.weight", where_), v, "high · medium · low 중 하나");
                    }
                }
                for k in NUM_FIELDS {
                    if let Some(v) = rm.get(k) {
                        let ok = v.as_i64().map(|n| n >= 1).unwrap_or(false) && !v.is_f64();
                        if !ok { lv(&mut out, "L5", &format!("{}.{}", where_, k), v, "1 이상의 정수여야 합니다"); }
                    }
                }
                if let Some(c) = rm.get("clear") {
                    if !is_str_list(c) {
                        lv(&mut out, "L4", &format!("{}.clear", where_), c, "말 칸 이름의 목록이어야 합니다");
                    } else {
                        for cell in c.as_array().unwrap() {
                            if !CELLS.contains(&cell.as_str().unwrap_or("")) {
                                lv(&mut out, "L4", &format!("{}.clear", where_), cell,
                                   &format!("모르는 말 칸입니다({})", CELLS.join(", ")));
                            }
                        }
                    }
                }
                for op in ["add", "remove"] {
                    let m = match rm.get(op) { Some(x) => x, None => continue };
                    match m.as_object() {
                        None => lv(&mut out, "L4", &format!("{}.{}", where_, op), m, "칸 이름 → 말 목록 매핑이어야 합니다"),
                        Some(mm) => for (cell, words) in mm {
                            if !CELLS.contains(&cell.as_str()) {
                                lv(&mut out, "L4", &format!("{}.{}.{}", where_, op, cell), words,
                                   &format!("모르는 말 칸입니다({})", CELLS.join(", ")));
                            } else if !is_str_list(words) {
                                lv(&mut out, "L4", &format!("{}.{}.{}", where_, op, cell), words, "말(문자열) 목록이어야 합니다");
                            }
                        },
                    }
                }
            },
        }
    }
    if let Some(head) = top.get("head") {
        match head.as_object() {
            None => lv(&mut out, "L6", "head", head, "매핑이어야 합니다"),
            Some(m) => {
                for (k, v) in m {
                    if k != "title_exclude" && k != "node_off" {
                        lv(&mut out, "L6", &format!("head.{}", k), v, "모르는 칸입니다(title_exclude · node_off)");
                    }
                }
                if let Some(te) = m.get("title_exclude") {
                    if !te.is_null() && !is_str_list(te) {
                        lv(&mut out, "L6", "head.title_exclude", te, "말(문자열) 목록이어야 합니다");
                    }
                }
                if let Some(no) = m.get("node_off") {
                    let ok = no.is_null() || no.as_array().map(|a| a.iter().all(|x|
                        x.get("node").map(|n| n.is_string()).unwrap_or(false))).unwrap_or(false);
                    if !ok {
                        lv(&mut out, "L6", "head.node_off", no, "[{node: dc_id, title_at_decision: 제목}] 목록이어야 합니다");
                    }
                }
            }
        }
    }
    out
}

/// YAML 파일 → JSON 값(없으면 None).
fn read_yaml(path: &Path) -> Result<Option<Value>, String> {
    if !path.is_file() { return Ok(None); }
    let text = std::fs::read_to_string(path).map_err(|e| format!("{}: {}", path.display(), e))?;
    let text = text.strip_prefix('\u{feff}').unwrap_or(&text);
    let yv: serde_yaml::Value = serde_yaml::from_str(text)
        .map_err(|e| format!("YAML 로 읽히지 않습니다: {}", e))?;
    serde_json::to_value(&yv).map(Some).map_err(|e| format!("변환 실패: {}", e))
}

/// doc_rule.local.yaml 읽기 — 없으면 빈 조정, 있는데 틀리면 오류(보고문).
pub fn load_rule_local(path: &Path) -> Result<Value, String> {
    let data = match read_yaml(path) {
        Ok(Some(v)) => v,
        Ok(None) => return Ok(json!({})),
        Err(e) => return Err(format!("업무분류 조정 파일 검증 실패: {} (1건)\n  [L0] (파일) — {}", path.display(), e)),
    };
    let v = validate_rule_local(&data);
    if !v.is_empty() {
        return Err(format!("업무분류 조정 파일 검증 실패: {} ({}건)\n  {}",
                           path.display(), v.len(), v.join("\n  ")));
    }
    Ok(if data.is_null() { json!({}) } else { data })
}

/// 파일 지문 — 줄끝(CRLF/LF)과 UTF-8 BOM 을 맞춘 뒤의 sha256(Python file_digest).
pub fn file_digest(path: &Path) -> Option<String> {
    let mut data = std::fs::read(path).ok()?;
    if data.starts_with(&[0xef, 0xbb, 0xbf]) { data.drain(0..3); }
    let mut norm = Vec::with_capacity(data.len());
    let mut i = 0;
    while i < data.len() {
        if data[i] == b'\r' && i + 1 < data.len() && data[i + 1] == b'\n' { i += 1; continue; }
        norm.push(data[i]);
        i += 1;
    }
    let mut h = Sha256::new();
    h.update(&norm);
    Some(format!("{:x}", h.finalize()))
}

fn sha_hex(text: &str) -> String {
    let mut h = Sha256::new();
    h.update(text.as_bytes());
    format!("{:x}", h.finalize())
}

/// 키 정렬·공백 없는 JSON(Python json.dumps(sort_keys=True, separators=(",", ":"))).
pub fn canon(v: &Value) -> String {
    match v {
        Value::Object(m) => {
            let mut keys: Vec<&String> = m.keys().collect();
            keys.sort();
            let parts: Vec<String> = keys.iter()
                .map(|k| format!("{}:{}", serde_json::to_string(k).unwrap(), canon(&m[*k]))).collect();
            format!("{{{}}}", parts.join(","))
        }
        Value::Array(a) => format!("[{}]", a.iter().map(canon).collect::<Vec<_>>().join(",")),
        other => serde_json::to_string(other).unwrap(),
    }
}

/// 규칙 본문 지문 — generated 칸을 뺀 나머지의 canon JSON sha256(Python body_digest).
pub fn body_digest(doc: &Value) -> String {
    let mut body = Map::new();
    if let Some(m) = doc.as_object() {
        for (k, v) in m {
            if k != "generated" { body.insert(k.clone(), v.clone()); }
        }
    }
    sha_hex(&canon(&Value::Object(body)))
}

/// 정책 폴더 안의 상대경로('/' 구분). 폴더 밖이면 파일 이름만.
fn rel(path: &Path, policy_dir: &Path) -> String {
    let ap = std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
    let pd = std::fs::canonicalize(policy_dir).unwrap_or_else(|_| policy_dir.to_path_buf());
    match ap.strip_prefix(&pd) {
        Ok(r) => r.components().map(|c| c.as_os_str().to_string_lossy().into_owned())
                   .collect::<Vec<_>>().join("/"),
        Err(_) => path.file_name().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default(),
    }
}

/// YAML 파일의 version 칸(문자열로).
fn version_of(path: &Path) -> Option<String> {
    let v = read_yaml(path).ok()??;
    match v.get("version")? {
        Value::String(s) => Some(s.clone()),
        Value::Null => None,
        other => Some(other.to_string()),
    }
}

/// 입력 파일 기록 한 줄 {file, [version,] sha256}.
fn input_entry(path: &Path, policy_dir: &Path) -> Value {
    let mut e = Map::new();
    e.insert("file".into(), json!(rel(path, policy_dir)));
    if let Some(v) = version_of(path) { e.insert("version".into(), json!(v)); }
    e.insert("sha256".into(), json!(file_digest(path)));
    Value::Object(e)
}

/// 한 겹씩 덮기(묶음은 그 안까지) — Python _overlay.
fn overlay(base: Option<&Value>, over: &Value) -> Value {
    match (base, over) {
        (Some(Value::Object(b)), Value::Object(o)) => {
            let mut out = b.clone();
            for (k, v) in o {
                let merged = if b.contains_key(k) { overlay(b.get(k), v) } else { v.clone() };
                out.insert(k.clone(), merged);
            }
            Value::Object(out)
        }
        _ => over.clone(),
    }
}

fn strs(v: Option<&Value>) -> Vec<String> {
    v.and_then(|x| x.as_array()).map(|a| a.iter().filter_map(|x| x.as_str())
        .map(|x| x.to_string()).collect()).unwrap_or_default()
}

/// 규칙 한 줄에 local 조정 덧씌우기(clear → remove → add · weight · 숫자 칸).
/// 적용된 조정 표지 목록을 돌려준다(Python _apply_adjust).
fn apply_adjust(rule: &mut Map<String, Value>, adj: &Value, warns: &mut Vec<String>) -> Vec<String> {
    let mut marks = vec![];
    let node = rule.get("node").and_then(|v| v.as_str()).unwrap_or("").to_string();
    for cell in strs(adj.get("clear")) {
        rule.insert(cell.clone(), json!([]));
        marks.push(format!("clear:{}", cell));
    }
    for cell in CELLS {
        let words = strs(adj.get("remove").and_then(|m| m.get(cell)));
        if words.is_empty() { continue; }
        let have = strs(rule.get(cell));
        for w in words.iter().filter(|w| !have.contains(w)) {
            warns.push(format!("[local] {}.remove.{}: '{}' 는 이미 규칙에 없습니다 — 지워도 되는 조정일 수 있습니다",
                               node, cell, w));
        }
        let kept: Vec<String> = have.into_iter().filter(|w| !words.contains(w)).collect();
        rule.insert(cell.to_string(), json!(kept));
        marks.push(format!("remove:{}", cell));
    }
    for cell in CELLS {
        let words = strs(adj.get("add").and_then(|m| m.get(cell)));
        if words.is_empty() { continue; }
        let mut have = strs(rule.get(cell));
        for w in words { if !have.contains(&w) { have.push(w); } }
        rule.insert(cell.to_string(), json!(have));
        marks.push(format!("add:{}", cell));
    }
    if let Some(w) = adj.get("weight") {
        rule.insert("weight".into(), w.clone());
        marks.push("weight".into());
    }
    for k in NUM_FIELDS {
        if let Some(v) = adj.get(k) {
            rule.insert(k.into(), v.clone());
            marks.push(k.into());
        }
    }
    marks
}

/// 규칙 한 줄의 칸 차례(Python _RULE_ORDER).
fn order_rule(rule: Map<String, Value>) -> Value {
    let order = ["id", "node", "title", "path", "path_ids", "weight",
                 "head_chars", "min_count", "min_distinct",
                 "title_terms", "head_terms", "terms", "filename", "exclude", "heads", "local"];
    let mut out = Map::new();
    for k in order {
        if let Some(v) = rule.get(k) { out.insert(k.into(), v.clone()); }
    }
    Value::Object(out)
}

/// 규칙 값 만들기 — 설계서 8장 순서(Python build_doc_rule).
///  1) 체계 JSON → 분류체계(검증 포함, doc_taxonomy.yaml 을 거치지 않음)
///  2) 업종은 local(없으면 본보기) → 사전 3겹
///  3) 규칙을 만들 분류(꺼 둔 분류·서랍 제외, 전체경로 순)마다 기본 말
///  4) local 조정 덧씌우기  5) 핵어·잡음 꼬리  6) 설정  7) 지문·판
pub fn build_doc_rule(export_path: &Path, policy_dir: &Path) -> Result<(Value, Vec<String>), BuildError> {
    let mut warns: Vec<String> = vec![];

    let text = std::fs::read_to_string(export_path)
        .map_err(|e| BuildError::Export(format!("체계 JSON 을 읽지 못했습니다: {}", e)))?;
    let text = text.strip_prefix('\u{feff}').unwrap_or(&text);
    let raw: Value = serde_json::from_str(text)
        .map_err(|e| BuildError::Export(format!("체계 JSON 을 읽지 못했습니다: {}", e)))?;
    let (snapshot, conv_warns) = axes::convert_mpower_json(&raw).map_err(BuildError::Export)?;
    warns.extend(conv_warns.into_iter().map(|w| format!("[체계] {}", w)));
    let taxonomy = axes::taxonomy_from_value(&snapshot, &export_path.display().to_string())
        .map_err(|e| BuildError::Export(e.to_string()))?;

    let local_path = policy_dir.join(RULE_LOCAL);
    let local = load_rule_local(&local_path).map_err(BuildError::Local)?;
    let tpl_path = policy_dir.join("doc_rule_template.yaml");
    let tpl = read_yaml(&tpl_path).ok().flatten().filter(|v| v.is_object()).unwrap_or(json!({}));

    // 업종 — 회사가 정한 것(local)이 본보기보다 앞선다.
    let ind_raw = if local.get("industry").is_some() { local.get("industry") } else { tpl.get("industry") };
    let ind_yaml: Option<serde_yaml::Value> = ind_raw.and_then(|v| serde_yaml::to_value(v).ok());
    let industry = docvocab::clean_industry(ind_yaml.as_ref());
    let syn = docvocab::load_synonyms_with(policy_dir, &industry);

    let weight = tpl.get("new_rule").and_then(|m| m.get("weight")).cloned().unwrap_or(json!("medium"));
    let empty = json!({});
    let adjust = local.get("rules").filter(|v| v.is_object()).unwrap_or(&empty);
    let head_cfg = local.get("head").filter(|v| v.is_object()).unwrap_or(&empty);

    // 규칙을 만들 분류 — 꺼 둔 분류·서랍은 빼고, 전체경로 순(Python 판과 같은 기준·같은 차례).
    let kids: std::collections::HashSet<&str> = taxonomy.nodes.iter()
        .filter(|n| n.active()).filter_map(|n| n.parent.as_deref()).collect();
    let mut nodes: Vec<(String, String, String)> = vec![];
    for n in taxonomy.nodes.iter() {
        if !n.active() || docvocab::is_drawer(n.parent.is_some(), kids.contains(n.dc_id.as_str())) { continue; }
        let path = taxonomy.path(&n.dc_id).unwrap_or_else(|_| n.dc_id.clone());
        nodes.push((path, n.dc_id.clone(), n.title.clone()));
    }
    nodes.sort_by(|a, b| a.0.cmp(&b.0));

    // local 조정이 가리키는 분류가 있는지·이름이 바뀌었는지 먼저 알린다.
    if let Some(m) = adjust.as_object() {
        for (dc_id, adj) in m {
            let title = match nodes.iter().find(|n| &n.1 == dc_id) {
                Some(n) => n.2.clone(),
                None => {
                    warns.push(format!("[local] rules.{}: 규칙을 만드는 분류에 없습니다(삭제·꺼 둔 분류·대분류·오타) — 이 조정은 쓰이지 않습니다", dc_id));
                    continue;
                }
            };
            let was = adj.get("title_at_decision").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
            if !was.is_empty() && was.replace(' ', "") != title.replace(' ', "") {
                warns.push(format!("[local] rules.{}: 조정할 때 이름은 '{}', 지금 이름은 '{}' 입니다 — 이 조정이 계속 맞는지 확인하세요",
                                   dc_id, was, title));
            }
        }
    }

    // 핵어 — 판정 때 하던 계산을 만들 때 한 번 한다.
    let excludes = strs(head_cfg.get("title_exclude"));
    let node_off: Vec<(String, String)> = head_cfg.get("node_off").and_then(|v| v.as_array())
        .map(|a| a.iter().filter_map(|x| {
            let node = x.get("node").and_then(|v| v.as_str())?;
            let was = x.get("title_at_decision").and_then(|v| v.as_str()).unwrap_or("");
            Some((node.to_string(), was.to_string()))
        }).collect()).unwrap_or_default();
    let (lexicon, head_warns) = taxhead::build_head_lexicon(&taxonomy, &syn, &excludes, &node_off);
    warns.extend(head_warns);

    let mut rules: Vec<Value> = vec![];
    for (_, dc_id, title) in &nodes {
        let adj = adjust.get(dc_id).cloned().unwrap_or(json!({}));
        // 회사가 쓰지 않는 분류는 규칙을 만들지 않는다.
        if adj.get("enabled") == Some(&Value::Bool(false)) {
            warns.push(format!("[local] {}({}): enabled=false — 규칙을 만들지 않습니다", dc_id, title));
            continue;
        }
        let v = docvocab::rule_vocab(title, &syn, 10);
        let mut rule = Map::new();
        rule.insert("id".into(), json!(format!("dt_{}", dc_id.to_lowercase())));
        rule.insert("node".into(), json!(dc_id));
        rule.insert("title".into(), json!(title));
        rule.insert("path".into(), json!(taxonomy.path(dc_id).unwrap_or_default()));
        rule.insert("path_ids".into(), json!(taxonomy.path_ids(dc_id).unwrap_or_default()));
        rule.insert("weight".into(), weight.clone());
        rule.insert("title_terms".into(), json!(v.title_terms));
        rule.insert("head_terms".into(), json!(v.head_terms));
        rule.insert("terms".into(), json!(v.terms));
        rule.insert("filename".into(), json!(v.filename));
        rule.insert("exclude".into(), json!(v.exclude));
        let marks = apply_adjust(&mut rule, &adj, &mut warns);
        let heads: Vec<String> = lexicon.get(dc_id).map(|e| e.heads.clone()).unwrap_or_default();
        rule.insert("heads".into(), json!(heads));
        if !marks.is_empty() { rule.insert("local".into(), json!(marks)); }
        rules.push(order_rule(rule));
    }

    // 설정 — 본보기 기본값 위에 회사 설정.
    let settings = local.get("settings").cloned().unwrap_or(json!({}));
    let mut doc = Map::new();
    doc.insert("version".into(), json!(""));
    doc.insert("generated".into(), json!({}));
    for sec in SETTING_SECTIONS {
        let base = tpl.get(sec);
        let mut val = match settings.get(sec) {
            Some(over) => Some(overlay(base, over)),
            None => base.cloned(),
        };
        if sec == "conflict" && val.as_ref().map(|v| v.is_null()).unwrap_or(true) {
            val = Some(json!("all"));
        }
        if let Some(v) = val { if !v.is_null() { doc.insert(sec.into(), v); } }
    }
    doc.insert("noise_tails".into(), json!(syn.head_list("noise_tails")));
    doc.insert("doctype_rules".into(), Value::Array(rules));

    // 입력 지문 — 실행 때 '입력이 바뀌었는데 다시 안 만들었나'를 가리는 기준.
    let mut inputs: Vec<Value> = vec![json!({"file": rel(export_path, policy_dir),
                                             "sha256": file_digest(export_path)})];
    for p in &syn.layers { inputs.push(input_entry(Path::new(p), policy_dir)); }
    for p in [&tpl_path, &local_path] {
        if p.is_file() { inputs.push(input_entry(p, policy_dir)); }
    }
    let seed = canon(&json!([inputs, industry]));
    doc.insert("version".into(), json!(format!("doctype-gen-{}", &sha_hex(&seed)[..12])));
    doc.insert("generated".into(), json!({"by": GENERATED_BY, "industry": industry,
                                          "inputs": inputs, "body_sha256": ""}));
    let mut docv = Value::Object(doc);
    let bd = body_digest(&docv);
    docv["generated"]["body_sha256"] = json!(bd);
    Ok((docv, warns))
}

/// 값 하나 → 한 줄 JSON(Python json.dumps(ensure_ascii=False, separators=(", ", ": "))).
fn jflow(v: &Value) -> String {
    match v {
        Value::Array(a) => format!("[{}]", a.iter().map(jflow).collect::<Vec<_>>().join(", ")),
        Value::Object(m) => format!("{{{}}}", m.iter()
            .map(|(k, x)| format!("{}: {}", serde_json::to_string(k).unwrap(), jflow(x)))
            .collect::<Vec<_>>().join(", ")),
        other => serde_json::to_string(other).unwrap(),
    }
}

/// 매핑 → YAML 줄들(Python _emit_map). 매핑은 블록, 매핑 목록은 '- ' 블록 항목,
/// 그 밖은 한 줄 JSON.
fn emit_map(m: &Map<String, Value>, indent: usize, lines: &mut Vec<String>) {
    let pad = " ".repeat(indent);
    for (k, v) in m {
        match v {
            Value::Object(sub) if !sub.is_empty() => {
                lines.push(format!("{}{}:", pad, k));
                emit_map(sub, indent + 2, lines);
            }
            Value::Array(items) if !items.is_empty() && items.iter().all(|x| x.is_object()) => {
                lines.push(format!("{}{}:", pad, k));
                for item in items {
                    let mut sub = vec![];
                    emit_map(item.as_object().unwrap(), indent + 2, &mut sub);
                    if let Some(first) = sub.first_mut() {
                        *first = format!("{}- {}", pad, &first[indent + 2..]);
                    }
                    lines.extend(sub);
                }
            }
            other => lines.push(format!("{}{}: {}", pad, k, jflow(other))),
        }
    }
}

/// 규칙 값 → 파일 텍스트(머리 안내 + 본문, 줄끝 LF).
pub fn dump_doc_rule(doc: &Value) -> String {
    let mut lines = vec![];
    if let Some(m) = doc.as_object() { emit_map(m, 0, &mut lines); }
    format!("{}{}\n", GEN_HEADER, lines.join("\n"))
}

/// 규칙 파일 쓰기 — 이미 있으면 .bak 으로 한 벌 남긴다.
pub fn write_doc_rule(path: &Path, doc: &Value) -> Result<(), String> {
    if let Some(dir) = path.parent() {
        if !dir.as_os_str().is_empty() {
            std::fs::create_dir_all(dir).map_err(|e| format!("폴더를 만들 수 없습니다({}): {}", dir.display(), e))?;
        }
    }
    if path.is_file() {
        let mut bak = path.as_os_str().to_owned();
        bak.push(".bak");
        let _ = std::fs::copy(path, PathBuf::from(bak));
    }
    std::fs::write(path, dump_doc_rule(doc)).map_err(|e| format!("쓰기 실패({}): {}", path.display(), e))
}

/// 자동 생성 규칙 파일인가(generated 칸이 매핑).
pub fn is_generated(data: &Value) -> bool {
    data.get("generated").map(|v| v.is_object()).unwrap_or(false)
}

/// 실행 때 생성 규칙 점검 — 손으로 고친 흔적·입력이 바뀜을 경고한다(Python check_generated).
/// 체계 JSON(첫 줄)은 연동 임시 파일일 수 있어 없으면 조용히 넘긴다.
pub fn check_generated(data: &Value, rules_path: &Path) -> Vec<String> {
    let mut warns = vec![];
    let gen = &data["generated"];
    if let Some(want) = gen.get("body_sha256").and_then(|v| v.as_str()) {
        if !want.is_empty() && body_digest(data) != want {
            warns.push("doc_rule.yaml 이 만들어진 뒤 손으로 고쳐졌습니다 — 다음에 다시 만들면 고친 것이 사라집니다. 조정은 doc_rule.local.yaml 에 적으세요".to_string());
        }
    }
    let base = rules_path.parent().unwrap_or(Path::new("."));
    let mut stale: Vec<String> = vec![];
    if let Some(inputs) = gen.get("inputs").and_then(|v| v.as_array()) {
        for (i, e) in inputs.iter().enumerate() {
            let file = match e.get("file").and_then(|v| v.as_str()) { Some(f) if !f.is_empty() => f, _ => continue };
            let mut p = base.to_path_buf();
            for part in file.split('/') { p.push(part); }
            match file_digest(&p) {
                None => { if i > 0 { stale.push(format!("{}(없어짐)", file)); } }
                Some(now) => {
                    if Some(now.as_str()) != e.get("sha256").and_then(|v| v.as_str()) { stale.push(file.to_string()); }
                }
            }
        }
    }
    if !stale.is_empty() {
        warns.push(format!("규칙을 만든 뒤 입력이 바뀌었습니다: {} — --build-doc-rule 로 다시 만드세요", stale.join(", ")));
    }
    warns
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 시험용 정책 폴더 — 저장소 사전·본보기 + 예시 체계 JSON(Python test_docbuild 과 같은 재료).
    fn policy(tag: &str, local: Option<&str>) -> (PathBuf, PathBuf) {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("..");
        let src = root.join("resources").join("policy");
        let dir = std::env::temp_dir().join(format!("mpc_docbuild_{}_{}", tag, std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("synonyms")).unwrap();
        for e in std::fs::read_dir(src.join("synonyms")).unwrap() {
            let e = e.unwrap();
            let name = e.file_name().to_string_lossy().into_owned();
            if name == "doc_synonyms.local.yaml" { continue; }
            std::fs::copy(e.path(), dir.join("synonyms").join(&name)).unwrap();
        }
        std::fs::copy(src.join("doc_rule_template.yaml"), dir.join("doc_rule_template.yaml")).unwrap();
        let exp = dir.join("doc_classification_export.json");
        std::fs::copy(src.join("samples").join("doc_classification_export.itsec.json"), &exp).unwrap();
        if let Some(l) = local { std::fs::write(dir.join(RULE_LOCAL), l).unwrap(); }
        (dir, exp)
    }

    fn rule<'a>(doc: &'a Value, node: &str) -> Option<&'a Value> {
        doc["doctype_rules"].as_array().unwrap().iter().find(|r| r["node"] == node)
    }

    #[test]
    fn 같은_입력이면_글자까지_같고_분류체계_칸이_없다() {
        let (dir, exp) = policy("det", None);
        let (a, _) = build_doc_rule(&exp, &dir).ok().unwrap();
        let (b, _) = build_doc_rule(&exp, &dir).ok().unwrap();
        assert_eq!(dump_doc_rule(&a), dump_doc_rule(&b));
        assert!(a.get("taxonomy").is_none());
        assert!(rule(&a, "DC_002_005").is_none());
        assert!(rule(&a, "DC_001").is_none());
        let r = rule(&a, "DC_002_001_002").unwrap();
        assert_eq!(r["path"], "제품/기술 > 매뉴얼 > 관리자매뉴얼");
        assert_eq!(r["path_ids"], json!(["DC_002", "DC_002_001", "DC_002_001_002"]));
        // 방금 만든 값은 스스로 점검해도 경고가 없다.
        let p = dir.join("doc_rule.yaml");
        write_doc_rule(&p, &a).unwrap();
        let back = read_yaml(&p).unwrap().unwrap();
        assert!(check_generated(&back, &p).is_empty());
    }

    #[test]
    fn local_조정을_덧씌운다() {
        let (dir, exp) = policy("adj", Some(
            "rules:\n  DC_001_001:\n    clear: [terms]\n    add: {title_terms: [입찰제안], terms: [RFP 응답]}\n    weight: high\n    head_chars: 200\n  DC_004_005:\n    enabled: false\n"));
        let (doc, warns) = build_doc_rule(&exp, &dir).ok().unwrap();
        let r = rule(&doc, "DC_001_001").unwrap();
        assert_eq!(r["terms"], json!(["RFP 응답"]));
        assert_eq!(r["weight"], "high");
        assert_eq!(r["head_chars"], 200);
        assert_eq!(r["local"], json!(["clear:terms", "add:title_terms", "add:terms", "weight", "head_chars"]));
        assert!(rule(&doc, "DC_004_005").is_none());
        assert!(warns.iter().any(|w| w.contains("DC_004_005")));
    }

    #[test]
    fn 틀린_조정은_막는다() {
        let bad: Value = json!({"rules": {"DC_001_001": {"clear": ["body"], "weight": "big",
                                  "min_count": 0, "enabld": false}}, "setting": {}});
        let codes: Vec<String> = validate_rule_local(&bad).iter()
            .map(|s| s[1..3].to_string()).collect();
        let mut sorted = codes.clone();
        sorted.sort();
        assert_eq!(sorted, vec!["L0", "L3", "L4", "L5", "L5"]);
    }

    #[test]
    fn 손으로_고치거나_입력이_바뀌면_알린다() {
        let (dir, exp) = policy("chk", None);
        let (doc, _) = build_doc_rule(&exp, &dir).ok().unwrap();
        let p = dir.join("doc_rule.yaml");
        write_doc_rule(&p, &doc).unwrap();
        let core = dir.join("synonyms").join("doc_synonyms.core.yaml");
        let mut t = std::fs::read_to_string(&core).unwrap();
        t.push_str("\n# 바뀜\n");
        std::fs::write(&core, t).unwrap();
        let back = read_yaml(&p).unwrap().unwrap();
        assert!(check_generated(&back, &p).iter().any(|w| w.contains("입력이 바뀌었습니다")));
        let text = std::fs::read_to_string(&p).unwrap().replacen("weight: \"medium\"", "weight: \"high\"", 1);
        std::fs::write(&p, text).unwrap();
        let back = read_yaml(&p).unwrap().unwrap();
        assert!(check_generated(&back, &p).iter().any(|w| w.contains("손으로 고쳐졌습니다")));
    }
}
