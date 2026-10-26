//------------------------------------------------------------------
// 사람이 정한 보안등급(결정 목록) 읽기 · 적용 · 덧붙이기
//=> UI 검토함이 쌓는 cso_override.jsonl 을 엔진도 그대로 읽어, 판정이 끝난 뒤
//   사람 결정이 판정을 이기게 한다(--grade-overrides). --set-grade 는 파일 하나에
//   바로 등급을 정하고, 원하면 같은 목록에 한 줄을 덧붙인다.
//   설계: plan/사람등급지정-수동오버라이드-설계-20261020.html
//   찾는 순서 · 정규화는 ui/docidkey.py 와 같다.
//   파이썬 판 gradeoverride.py 와 같은 계약이다 — 한쪽을 고치면 다른 쪽도 고친다.
//------------------------------------------------------------------
use serde_json::{json, Map, Value};
use std::collections::HashMap;

//------------------------------------------------------------------
// 등급 엄격도 — 내용이 바뀐 문서에 사람 결정을 따를지 정할 때 쓴다(§05)
//
// -in: g = "C" | "S" | "O" | None
//
// -out: u8 = C 3 · S 2 · O 1 · 없음 0
// -out: error = 없음
//------------------------------------------------------------------
fn rank(g: Option<&str>) -> u8 {
    match g { Some("C") => 3, Some("S") => 2, Some("O") => 1, _ => 0 }
}

//------------------------------------------------------------------
// 결정 값 → 등급
//=> "C"/"S"/"O" 는 그대로, "보류" · "NONE" · 빈 값 · null 은 등급 없음(None).
//
// -in: v = 결정 기록의 new_grade 값
//
// -out: Option<String>
// -out: error = 없음
//------------------------------------------------------------------
pub fn norm_grade(v: Option<&Value>) -> Option<String> {
    let s = v.and_then(|x| x.as_str()).unwrap_or("").trim().to_ascii_uppercase();
    match s.as_str() { "C" | "S" | "O" => Some(s), _ => None }
}

//------------------------------------------------------------------
// 경로 정규화 — ui/docidkey.normalize_key(파이썬 os.path.normpath) 와 같은 규칙
//=> 구분자(\ → /) · 빈 칸 · '.' · '..' 를 정리하고 드라이브 문자를 대문자로. UNC 앞 // 는 지킨다.
//
// -in: path = 파일 경로
//
// -out: String = 정규화한 키(빈 경로면 "")
// -out: error = 없음
//------------------------------------------------------------------
pub fn normalize_key(path: &str) -> String {
    let s = path.trim().replace('\\', "/");
    if s.is_empty() {
        return String::new();
    }
    let unc = s.starts_with("//");
    let body = if unc { &s[2..] } else { &s[..] };
    // 드라이브(C:) 와 맨 앞 / 를 떼어 둔다
    let (prefix, rest) = if body.len() >= 2 && body.as_bytes()[1] == b':' && body.as_bytes()[0].is_ascii_alphabetic() {
        (body[..2].to_string(), &body[2..])
    } else {
        (String::new(), body)
    };
    let rooted = rest.starts_with('/');
    let mut parts: Vec<&str> = vec![];
    for seg in rest.split('/') {
        match seg {
            "" | "." => {}
            ".." => {
                if parts.last().map_or(false, |p| *p != "..") {
                    parts.pop();
                } else if !rooted {
                    parts.push("..");
                }
            }
            x => parts.push(x),
        }
    }
    let mut out = prefix;
    if rooted {
        out.push('/');
    }
    out.push_str(&parts.join("/"));
    if out.is_empty() {
        out.push('.');
    }
    if unc {
        out = format!("//{}", out.trim_start_matches('/'));
    }
    let b = out.as_bytes();
    if b.len() >= 2 && b[1] == b':' && b[0].is_ascii_alphabetic() {
        out = format!("{}{}", (b[0] as char).to_ascii_uppercase(), &out[1..]);
    }
    out
}

//------------------------------------------------------------------
// 결정 목록 색인
//=> 파일별 최신 결정(나중 줄 우선)과, doc_id · 지문 · 정규화 경로 · 대소문자 무시 경로 찾기표.
//
// -필드: latest = {파일경로: 최신 결정}
// -필드: broken = 읽지 못한 줄 수
//------------------------------------------------------------------
pub struct OverrideIndex {
    pub latest: HashMap<String, Value>,
    pub broken: usize,
    by_doc_id: HashMap<String, String>,
    by_hash: HashMap<String, String>,
    by_key: HashMap<String, String>,
    by_fold: HashMap<String, String>,
}

impl OverrideIndex {
    //--------------------------------------------------------------
    // 색인 만들기
    //
    // -in: latest = {파일경로: 결정} · order = 처음 나온 순서(파이썬 dict 순서와 맞추려고)
    // -in: broken = 깨진 줄 수
    //
    // -out: OverrideIndex
    // -out: error = 없음
    //--------------------------------------------------------------
    fn build(latest: HashMap<String, Value>, order: Vec<String>, broken: usize) -> OverrideIndex {
        let mut by_doc_id = HashMap::new();
        let mut by_hash = HashMap::new();
        let mut by_key = HashMap::new();
        let mut fold: HashMap<String, Vec<String>> = HashMap::new();
        for f in &order {
            let e = &latest[f];
            if let Some(d) = e.get("doc_id").and_then(|v| v.as_str()).filter(|s| !s.is_empty()) {
                by_doc_id.entry(d.to_string()).or_insert_with(|| f.clone());
            }
            if let Some(h) = e.get("hash").and_then(|v| v.as_str()).filter(|s| !s.is_empty()) {
                by_hash.entry(h.to_string()).or_insert_with(|| f.clone());
            }
            let k = normalize_key(f);
            by_key.entry(k.clone()).or_insert_with(|| f.clone());
            fold.entry(k.to_lowercase()).or_default().push(f.clone());
        }
        // 대소문자만 다른 경로가 둘 이상이면 어느 쪽인지 모르므로 쓰지 않는다
        let by_fold = fold.into_iter().filter(|(_, v)| v.len() == 1).map(|(k, v)| (k, v[0].clone())).collect();
        OverrideIndex { latest, broken, by_doc_id, by_hash, by_key, by_fold }
    }

    //--------------------------------------------------------------
    // 레코드 → 결정 찾기
    //=> ① doc_id ② 내용 지문(옛 기록의 '지문 앞 40자' doc_id 포함) ③ 경로 정확 일치
    //   ④ 정규화 경로 ⑤ 대소문자 무시 순서.
    //
    // -in: rec = 결과 레코드
    //
    // -out: Option<(&Value, &'static str)> = 결정과 찾은 방법("doc_id"|"hash"|"path"|"case")
    // -out: error = 없음
    //--------------------------------------------------------------
    pub fn lookup(&self, rec: &Value) -> Option<(&Value, &'static str)> {
        let s = |k: &str| rec.get(k).and_then(|v| v.as_str()).filter(|x| !x.is_empty());
        if let Some(d) = s("doc_id") {
            if let Some(f) = self.by_doc_id.get(d) { return Some((&self.latest[f], "doc_id")); }
        }
        if let Some(h) = s("hash") {
            if let Some(f) = self.by_hash.get(h) { return Some((&self.latest[f], "hash")); }
            let old: String = h.chars().take(40).collect();
            if let Some(f) = self.by_doc_id.get(&old) { return Some((&self.latest[f], "hash")); }
        }
        let f = s("file").unwrap_or("");
        if let Some(e) = self.latest.get(f) { return Some((e, "path")); }
        let k = normalize_key(f);
        if let Some(f2) = self.by_key.get(&k) { return Some((&self.latest[f2], "path")); }
        if let Some(f2) = self.by_fold.get(&k.to_lowercase()) { return Some((&self.latest[f2], "case")); }
        None
    }
}

//------------------------------------------------------------------
// 결정 목록 파일 읽기
//=> axis 가 "security" 이거나 없는(옛) 줄만, 같은 문서는 나중 줄. 깨진 줄은 세고 넘어간다.
//
// -in: path = cso_override.jsonl 경로
//
// -out: Result<OverrideIndex, String> = 못 읽으면 까닭
// -out: error = 파일 열기 실패 시 Err
//------------------------------------------------------------------
pub fn load(path: &str) -> Result<OverrideIndex, String> {
    let text = std::fs::read_to_string(path).map_err(|e| e.to_string())?;
    let text = text.trim_start_matches('\u{feff}');
    let mut latest: HashMap<String, Value> = HashMap::new();
    let mut order: Vec<String> = vec![];
    let mut broken = 0usize;
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() { continue; }
        let e: Value = match serde_json::from_str::<Value>(line) {
            Ok(v) if v.is_object() => v,
            _ => { broken += 1; continue; }
        };
        // axis 가 없는 줄은 두 축이 나뉘기 전의 옛 기록 = 보안등급이다
        if e.get("axis").and_then(|v| v.as_str()).unwrap_or("security") != "security" { continue; }
        let file = match e.get("file").and_then(|v| v.as_str()).filter(|s| !s.is_empty()) {
            Some(f) => f.to_string(),
            None => { broken += 1; continue; }
        };
        if !latest.contains_key(&file) { order.push(file.clone()); }
        latest.insert(file, e);
    }
    Ok(OverrideIndex::build(latest, order, broken))
}

//------------------------------------------------------------------
// 결정 하나를 레코드에 적용(설계 §03 · §05 · §06 — 파이썬 apply 와 같다)
//=> confirm 이면 표시만, 내용이 바뀌었고 사람 등급이 더 느슨하면 stale, 그 밖은 사람 등급으로.
//
// -in: rec   = 결과 레코드(제자리에서 고친다)
// -in: entry = 결정
// -in: how   = 찾은 방법
//
// -out: &'static str = "applied" | "confirmed" | "stale"
// -out: error = 없음
//------------------------------------------------------------------
pub fn apply(rec: &mut Value, entry: &Value, how: &str) -> &'static str {
    let computed = norm_grade(rec.get("grade"));
    let new = norm_grade(entry.get("new_grade"));
    let kind = entry.get("kind").and_then(|v| v.as_str()).unwrap_or("change").to_string();
    let eh = entry.get("hash").and_then(|v| v.as_str()).filter(|s| !s.is_empty());
    let rh = rec.get("hash").and_then(|v| v.as_str()).filter(|s| !s.is_empty());
    // 지문으로 찾았으면 같은 내용이다. 다른 길로 찾았는데 지문이 다르면 내용이 바뀐 것
    let stale = how != "hash" && eh.is_some() && rh.is_some() && eh != rh;
    let mut info = Map::new();
    for k in ["reviewer", "reason", "ts"] {
        info.insert(k.into(), entry.get(k).cloned().unwrap_or(Value::Null));
    }
    info.insert("matched_by".into(), json!(how));
    info.insert("kind".into(), json!(kind));
    if !rec.get("why").map_or(false, |w| w.is_object()) { rec["why"] = json!({}); }
    let sec = rec["why"].get("security").filter(|v| v.is_object()).cloned().unwrap_or_else(|| json!({}));
    if kind == "confirm" {
        info.insert("applied".into(), json!(false));
        if stale { info.insert("stale".into(), json!(true)); }
        let mut s2 = sec;
        s2["override"] = Value::Object(info);
        rec["why"]["security"] = s2;
        return "confirmed";
    }
    if stale && rank(new.as_deref()) < rank(computed.as_deref()) {
        // 바뀐 내용에 새 위험이 들어갔을 수 있다 — 사람 결정으로 등급을 낮추지 않는다
        info.insert("applied".into(), json!(false));
        info.insert("stale".into(), json!(true));
        info.insert("grade".into(), json!(new));
        let mut s2 = sec;
        s2["override"] = Value::Object(info);
        rec["why"]["security"] = s2;
        return "stale";
    }
    info.insert("applied".into(), json!(true));
    if stale { info.insert("stale".into(), json!(true)); }
    let mut computed_sec = sec;
    computed_sec["grade"] = json!(computed);
    rec["grade"] = json!(new);
    rec["why"]["security"] = json!({"method": "manual_override", "decided_by": ["reviewer"],
                                    "override": Value::Object(info), "computed": computed_sec});
    "applied"
}

//------------------------------------------------------------------
// 결정 목록에 한 줄 덧붙이기(--set-grade) — UI 의 append_override 와 같은 칸
//
// -in: path = 결정 목록 경로(없으면 만든다) · file = 문서 경로
// -in: old_grade · new_grade = 이전 · 새 등급(None 은 "보류")
// -in: reason · reviewer = 사유 · 검토자 · doc_hash = 내용 지문
//
// -out: Result<(), String>
// -out: error = 쓰기 실패 시 Err(까닭)
//------------------------------------------------------------------
pub fn append_entry(path: &str, file: &str, old_grade: Option<&str>, new_grade: Option<&str>,
                    reason: &str, reviewer: &str, doc_hash: Option<&str>) -> Result<(), String> {
    use std::io::Write;
    let entry = json!({
        "file": file, "doc_id": Value::Null, "hash": doc_hash, "axis": "security", "kind": "change",
        "old_grade": old_grade.unwrap_or("보류"), "new_grade": new_grade.unwrap_or("보류"),
        "reason": reason, "reviewer": reviewer, "ts": chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string(), "via": "cli",
    });
    if let Some(parent) = std::path::Path::new(path).parent() {
        if !parent.as_os_str().is_empty() { let _ = std::fs::create_dir_all(parent); }
    }
    let mut f = std::fs::OpenOptions::new().create(true).append(true).open(path).map_err(|e| e.to_string())?;
    writeln!(f, "{}", serde_json::to_string(&entry).unwrap()).map_err(|e| e.to_string())
}

//------------------------------------------------------------------
// 한 실행의 적용기 — 건수를 센다
//
// -필드: index  = OverrideIndex
// -필드: counts = loaded / applied / confirmed / stale
//------------------------------------------------------------------
pub struct Applier {
    pub index: OverrideIndex,
    pub loaded: usize,
    pub applied: usize,
    pub confirmed: usize,
    pub stale: usize,
}

impl Applier {
    //--------------------------------------------------------------
    // 적용기 만들기
    //
    // -in: index = OverrideIndex
    //
    // -out: Applier
    // -out: error = 없음
    //--------------------------------------------------------------
    pub fn new(index: OverrideIndex) -> Applier {
        let loaded = index.latest.len();
        Applier { index, loaded, applied: 0, confirmed: 0, stale: 0 }
    }

    //--------------------------------------------------------------
    // 레코드 하나에 적용 — 결정이 없으면 아무것도 안 한다
    //
    // -in: rec = 결과 레코드
    //
    // -out: bool = 등급을 사람 등급으로 바꿨으면 true
    // -out: error = 없음
    //--------------------------------------------------------------
    pub fn apply(&mut self, rec: &mut Value) -> bool {
        let found = self.index.lookup(rec).map(|(e, how)| (e.clone(), how));
        if let Some((entry, how)) = found {
            match apply(rec, &entry, how) {
                "applied" => { self.applied += 1; return true; }
                "confirmed" => self.confirmed += 1,
                _ => self.stale += 1,
            }
        }
        false
    }

    //--------------------------------------------------------------
    // 요약에 실을 집계(파이썬 판과 같은 차례)
    //
    // -in: 없음
    //
    // -out: Value
    // -out: error = 없음
    //--------------------------------------------------------------
    pub fn summary(&self) -> Value {
        json!({"loaded": self.loaded, "applied": self.applied, "confirmed": self.confirmed, "stale": self.stale})
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // 파이썬 os.path.normpath 와 같은 결과여야 UI 기록과 이어진다
    #[test]
    fn normalize_like_python() {
        assert_eq!(normalize_key(r"c:\a\b\..\c.docx"), "C:/a/c.docx");
        assert_eq!(normalize_key("D:/x//y/./z.txt"), "D:/x/y/z.txt");
        assert_eq!(normalize_key(r"\\srv\share\a.doc"), "//srv/share/a.doc");
        assert_eq!(normalize_key(""), "");
    }

    // 확인 · 적용 · 내용 바뀜(더 느슨하면 거절, 더 엄격하면 따름)
    #[test]
    fn apply_rules() {
        let mut r = json!({"file": "a", "grade": "O", "hash": "h1", "why": {"security": {"method": "rule"}}});
        assert_eq!(apply(&mut r, &json!({"new_grade": "C", "hash": "h1"}), "hash"), "applied");
        assert_eq!(r["grade"], "C");
        assert_eq!(r["why"]["security"]["computed"]["grade"], "O");
        let mut r = json!({"file": "a", "grade": "C", "hash": "h2"});
        assert_eq!(apply(&mut r, &json!({"new_grade": "O", "hash": "h1"}), "path"), "stale");
        assert_eq!(r["grade"], "C");
        let mut r = json!({"file": "a", "grade": "O", "hash": "h2"});
        assert_eq!(apply(&mut r, &json!({"new_grade": "C", "hash": "h1"}), "path"), "applied");
        assert_eq!(r["grade"], "C");
        let mut r = json!({"file": "a", "grade": "S"});
        assert_eq!(apply(&mut r, &json!({"new_grade": "S", "kind": "confirm"}), "path"), "confirmed");
        assert_eq!(r["grade"], "S");
        let mut r = json!({"file": "a", "grade": "S"});
        apply(&mut r, &json!({"new_grade": "보류"}), "path");
        assert!(r["grade"].is_null());
    }
}
