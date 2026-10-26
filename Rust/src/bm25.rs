//! 업무분류 2차 BM25 — 제목 · 파일명 말 비교(설계서 plan/업무분류-2차-BM25-설계-20261022.html §16).
//!
//! 벡터 2차와 나란히, 지금 문서의 제목 · 파일명을 '분류별로 묶은 기준 문서'와 BM25 로
//! 견준다. 최소 점수는 기준 문서끼리 자기 제외 모의시험으로 실행마다 자동 계산한다.
//! 파이썬 판 src/csoclassify/classify/bm25.py 와 토큰 · 점수 · 문턱이 같아야 한다.

use std::collections::{BTreeSet, HashMap, HashSet};

use once_cell::sync::Lazy;
use regex::Regex;
use serde_json::Value;
use unicode_normalization::UnicodeNormalization;

use crate::doctype::{first_line, title_allowed};

/// 단어(한글 음절 · 영문 소문자 · 숫자 덩어리). 앞뒤 보기 없이 파이썬과 같은 식.
static WORD: Lazy<Regex> = Lazy::new(|| Regex::new(r"[0-9a-z가-힣]+").unwrap());
/// 파일명의 괄호 안 내용(고객사 이름 · 판 번호가 많다).
static PAREN: Lazy<Regex> = Lazy::new(|| Regex::new(r"[\(\[\{（【][^\)\]\}）】]*[\)\]\}）】]").unwrap());
/// 숫자가 한 글자라도 섞인 말(번호 · 버전 · 날짜).
static DIGIT_WORD: Lazy<Regex> = Lazy::new(|| Regex::new(r"[0-9A-Za-z가-힣]*\d[0-9A-Za-z가-힣]*").unwrap());
pub const TITLE_MAX: usize = 200;

/// BM25 설정(doc_rule.yaml 의 bm25: 블록). 블록이 없으면 enabled=false 라 예전과 똑같이 돈다.
#[derive(Debug, Clone)]
pub struct Bm25Spec {
    pub enabled: bool,
    pub tokenizer: String,
    /// 필드 무게 — 규칙 파일에 적은 순서(합산 순서를 파이썬과 맞추려고 순서를 지킨다).
    pub fields: Vec<(String, f64)>,
    /// "auto"(기준 문서 모의시험) | "fixed"(min_score)
    pub mode: String,
    pub target_precision: f64,
    pub min_pass: usize,
    pub min_margin: f64,
    pub min_score: f64,
    pub name_only_allow: bool,
    pub name_only_cap: f64,
    pub k1: f64,
    pub b: f64,
    pub idf_min: f64,
    pub idf_max: f64,
    pub min_seeds: usize,
    pub min_categories: usize,
    pub cap_alone: f64,
    pub cap_agree: f64,
    pub stopwords: HashSet<String>,
}

impl Default for Bm25Spec {
    fn default() -> Self {
        Bm25Spec {
            enabled: false,
            tokenizer: "bigram".into(),
            fields: vec![("title".into(), 1.0), ("name".into(), 1.0)],
            mode: "auto".into(),
            target_precision: 0.95,
            min_pass: 10,
            min_margin: 0.0,
            min_score: 15.0,
            name_only_allow: true,
            name_only_cap: 0.55,
            k1: 1.2,
            b: 0.75,
            idf_min: 0.1,
            idf_max: 3.0,
            min_seeds: 3,
            min_categories: 4,
            cap_alone: 0.60,
            cap_agree: 0.75,
            stopwords: ["작성자", "작성일", "페이지", "문서번호", "목차", "page", "of"]
                .iter().map(|s| s.to_string()).collect(),
        }
    }
}

/// bm25: 블록 검사 · 파싱(T23, 파이썬 parse_spec 과 같은 규칙). 틀린 칸은 기본값을 두고
/// (칸 이름, 값, 설명) 문제로 알린다. 블록이 없으면 enabled=false(예전 동작).
pub fn parse_spec(raw: &Value) -> (Bm25Spec, Vec<(&'static str, String, String)>) {
    let mut sp = Bm25Spec::default();
    let mut probs: Vec<(&'static str, String, String)> = vec![];
    if raw.is_null() {
        return (sp, probs);
    }
    let obj = match raw.as_object() {
        Some(o) => o,
        None => {
            probs.push(("bm25", raw.to_string(), "bm25 는 매핑(mapping)이어야 합니다".into()));
            return (sp, probs);
        }
    };
    let num = |v: &Value| if v.is_boolean() { None } else { v.as_f64() };
    let int = |v: &Value| if v.is_boolean() || v.is_f64() { None } else { v.as_i64() };
    if let Some(v) = obj.get("enabled") {
        match v.as_bool() {
            Some(b) => sp.enabled = b,
            None => probs.push(("bm25.enabled", v.to_string(), "true 또는 false 여야 합니다".into())),
        }
    }
    if let Some(v) = obj.get("tokenizer") {
        if v.as_str() == Some("bigram") {
            sp.tokenizer = "bigram".into();
        } else {
            probs.push(("bm25.tokenizer", v.to_string(), "지금은 bigram 만 쓸 수 있습니다".into()));
        }
    }
    if let Some(v) = obj.get("fields") {
        let mut fs: Vec<(String, f64)> = vec![];
        let mut ok = v.as_object().map_or(false, |m| !m.is_empty());
        if let Some(m) = v.as_object() {
            for (k, x) in m {
                match num(x) {
                    Some(w) if (k == "title" || k == "name") && w >= 0.0 => fs.push((k.clone(), w)),
                    _ => ok = false,
                }
            }
        }
        if ok && fs.iter().any(|(_, w)| *w > 0.0) {
            sp.fields = fs;
        } else {
            probs.push(("bm25.fields", v.to_string(),
                        "{title: 숫자, name: 숫자} 매핑이어야 하고 하나는 0보다 커야 합니다".into()));
        }
    }
    if let Some(th) = obj.get("threshold").filter(|t| !t.is_null()) {
        match th.as_object() {
            None => probs.push(("bm25.threshold", th.to_string(), "매핑이어야 합니다".into())),
            Some(t) => {
                if let Some(v) = t.get("mode") {
                    match v.as_str() {
                        Some(m) if m == "auto" || m == "fixed" => sp.mode = m.into(),
                        _ => probs.push(("bm25.threshold.mode", v.to_string(), "auto | fixed 중 하나여야 합니다".into())),
                    }
                }
                if let Some(v) = t.get("target_precision") {
                    match num(v) {
                        Some(x) if x > 0.0 && x <= 1.0 => sp.target_precision = x,
                        _ => probs.push(("bm25.threshold.target_precision", v.to_string(), "0~1 사이 숫자여야 합니다".into())),
                    }
                }
                if let Some(v) = t.get("min_margin") {
                    match num(v) {
                        Some(x) if (0.0..=1.0).contains(&x) => sp.min_margin = x,
                        _ => probs.push(("bm25.threshold.min_margin", v.to_string(), "0~1 사이 숫자여야 합니다".into())),
                    }
                }
                if let Some(v) = t.get("min_pass") {
                    match int(v) {
                        Some(x) if x >= 1 => sp.min_pass = x as usize,
                        _ => probs.push(("bm25.threshold.min_pass", v.to_string(), "1 이상의 정수여야 합니다".into())),
                    }
                }
                if let Some(v) = t.get("min_score") {
                    match num(v) {
                        Some(x) if x >= 0.0 => sp.min_score = x,
                        _ => probs.push(("bm25.threshold.min_score", v.to_string(), "0 이상 숫자여야 합니다".into())),
                    }
                }
            }
        }
    }
    if let Some(no) = obj.get("name_only").filter(|t| !t.is_null()) {
        match no.as_object() {
            None => probs.push(("bm25.name_only", no.to_string(), "매핑이어야 합니다".into())),
            Some(n) => {
                if let Some(v) = n.get("allow") {
                    match v.as_bool() {
                        Some(b) => sp.name_only_allow = b,
                        None => probs.push(("bm25.name_only.allow", v.to_string(), "true 또는 false 여야 합니다".into())),
                    }
                }
                if let Some(v) = n.get("cap") {
                    match num(v) {
                        Some(x) if (0.0..=1.0).contains(&x) => sp.name_only_cap = x,
                        _ => probs.push(("bm25.name_only.cap", v.to_string(), "0~1 사이 숫자여야 합니다".into())),
                    }
                }
            }
        }
    }
    for (k, lo, hi) in [("k1", 0.0, None), ("b", 0.0, Some(1.0)), ("idf_min", 0.0, None),
                        ("idf_max", 0.0, None), ("cap_alone", 0.0, Some(1.0)), ("cap_agree", 0.0, Some(1.0))] {
        if let Some(v) = obj.get(k) {
            match num(v) {
                Some(x) if x >= lo && hi.map_or(true, |h| x <= h) => match k {
                    "k1" => sp.k1 = x,
                    "b" => sp.b = x,
                    "idf_min" => sp.idf_min = x,
                    "idf_max" => sp.idf_max = x,
                    "cap_alone" => sp.cap_alone = x,
                    _ => sp.cap_agree = x,
                },
                _ => probs.push((match k { "k1" => "bm25.k1", "b" => "bm25.b", "idf_min" => "bm25.idf_min",
                                           "idf_max" => "bm25.idf_max", "cap_alone" => "bm25.cap_alone",
                                           _ => "bm25.cap_agree" },
                                 v.to_string(), "범위를 벗어난 숫자입니다".into())),
            }
        }
    }
    for (k, lo) in [("min_seeds", 1i64), ("min_categories", 2i64)] {
        if let Some(v) = obj.get(k) {
            match int(v) {
                Some(x) if x >= lo => if k == "min_seeds" { sp.min_seeds = x as usize } else { sp.min_categories = x as usize },
                _ => probs.push((if k == "min_seeds" { "bm25.min_seeds" } else { "bm25.min_categories" },
                                 v.to_string(), format!("{} 이상의 정수여야 합니다", lo))),
            }
        }
    }
    if let Some(v) = obj.get("stopwords") {
        match v.as_array() {
            Some(a) if a.iter().all(|x| x.is_string()) => {
                sp.stopwords = a.iter().filter_map(|x| x.as_str()).map(norm_text).collect();
            }
            _ => probs.push(("bm25.stopwords", v.to_string(), "문자열 목록이어야 합니다".into())),
        }
    }
    if sp.idf_min > sp.idf_max {
        probs.push(("bm25.idf_min", sp.idf_min.to_string(), "idf_min 이 idf_max 보다 큽니다".into()));
    }
    (sp, probs)
}

/// 공통 전처리 — NFC 정규화 + 소문자.
pub fn norm_text(s: &str) -> String {
    s.nfc().collect::<String>().to_lowercase()
}

fn is_syllable(c: char) -> bool {
    ('가'..='힣').contains(&c)
}

/// 자간 벌린 말 붙이기("출 장 여 비 규 정" → "출장여비규정"). 공백으로 나눈 말 가운데
/// '한글 한 글자'가 3개 이상 이어지면 붙인다(파이썬 despace_spaced 와 같은 규칙).
pub fn despace_spaced(s: &str) -> String {
    let mut out: Vec<String> = vec![];
    let mut run: Vec<String> = vec![];
    let words: Vec<&str> = s.split_whitespace().collect();
    let flush = |run: &mut Vec<String>, out: &mut Vec<String>| {
        if run.len() >= 3 {
            out.push(run.concat());
        } else {
            out.extend(run.drain(..));
        }
        run.clear();
    };
    for w in words {
        let mut cs = w.chars();
        let single = match (cs.next(), cs.next()) { (Some(c), None) => is_syllable(c), _ => false };
        if single {
            run.push(w.to_string());
            continue;
        }
        flush(&mut run, &mut out);
        out.push(w.to_string());
    }
    flush(&mut run, &mut out);
    out.join(" ")
}

/// 글자 2-gram 토큰(+ 단어). 한글이 든 단어는 "w:단어" + "g:두글자" 전부, 영문 · 숫자 단어는
/// "w:단어" 만. 숫자만 · 1글자 · 불용어는 버린다.
pub fn tokenize(s: &str, stopwords: &HashSet<String>) -> Vec<String> {
    let mut out = vec![];
    let text = despace_spaced(&norm_text(s));
    for m in WORD.find_iter(&text) {
        let w = m.as_str();
        let chars: Vec<char> = w.chars().collect();
        if chars.iter().all(|c| c.is_ascii_digit()) || chars.len() < 2 || stopwords.contains(w) {
            continue;
        }
        out.push(format!("w:{}", w));
        if chars.iter().any(|c| is_syllable(*c)) {
            for i in 0..chars.len() - 1 {
                out.push(format!("g:{}{}", chars[i], chars[i + 1]));
            }
        }
    }
    out
}

/// 경로에서 확장자 뺀 파일 이름('/' · '\' 어느 쪽이든 마지막 조각, 마지막 '.' 앞까지).
pub fn file_stem(file: &str) -> String {
    let base = file.rsplit(|c| c == '/' || c == '\\').next().unwrap_or("");
    match base.rfind('.') {
        Some(i) if i > 0 => base[..i].to_string(),
        _ => base.to_string(),
    }
}

/// 파일명 정리(강화판) — 끝 잡음 말(대소문자 무시) · 괄호 안 · 숫자가 섞인 말을 뺀다.
pub fn clean_name(name: &str, noise: &[String]) -> String {
    let mut s = name.to_string();
    for t in noise {
        if t.is_empty() {
            continue;
        }
        if let Ok(re) = Regex::new(&format!("(?i){}", regex::escape(t))) {
            s = re.replace_all(&s, " ").into_owned();
        }
    }
    let s = PAREN.replace_all(&s, " ").into_owned();
    DIGIT_WORD.replace_all(&s, " ").into_owned()
}

/// 문서 제목(1차 규칙과 같은 정의) — 첫 비어 있지 않은 줄 앞 200자. ppt · xls · xlsx 는 비운다.
pub fn title_of(text: &str, file: &str) -> String {
    if !title_allowed(file) {
        return String::new();
    }
    first_line(text).chars().take(TITLE_MAX).collect()
}

/// 비교 필드 토큰 {"title": [...], "name": [...]}.
pub fn field_tokens(title: &str, file: &str, spec: &Bm25Spec, noise: &[String])
                    -> HashMap<String, Vec<String>> {
    let mut m = HashMap::new();
    m.insert("title".to_string(), tokenize(title, &spec.stopwords));
    m.insert("name".to_string(), tokenize(&clean_name(&file_stem(file), noise), &spec.stopwords));
    m
}

/// 말 세기. 순서가 정해진 BTreeMap 대신 HashMap 을 쓰되, 합산은 언제나 정렬한 토큰 순서로 한다.
fn count(toks: &[String]) -> HashMap<String, u64> {
    let mut out = HashMap::new();
    for t in toks {
        *out.entry(t.clone()).or_insert(0) += 1;
    }
    out
}

/// BM25 순위 한 묶음 — (분류, 점수) 내림차순 · 분류별 필드 점수 · 파일명에서 겹친 단어.
#[derive(Debug, Default, Clone)]
pub struct Ranking {
    pub ranked: Vec<(String, f64)>,
    pub parts: HashMap<String, HashMap<String, f64>>,
    pub words: HashMap<String, Vec<String>>,
}

/// 기준 문서 색인(분류 단위 BM25).
pub struct Bm25Index {
    pub spec: Bm25Spec,
    labels: Vec<Vec<String>>,
    hashes: Vec<String>,
    vecs: Vec<Option<Vec<f64>>>,
    toks: Vec<HashMap<String, HashMap<String, u64>>>,
}

impl Bm25Index {
    /// 기준 문서 줄들로 색인 만들기 — 업무분류 라벨이 있는 줄만, 제목은 "title" 칸.
    pub fn from_rows(rows: &[Value], spec: &Bm25Spec, noise: &[String]) -> Self {
        let mut idx = Bm25Index { spec: spec.clone(), labels: vec![], hashes: vec![], vecs: vec![], toks: vec![] };
        for e in rows {
            let dt = e.get("doctype")
                .or_else(|| e.get("labels").and_then(|l| l.get("doctype")));
            let labs: Vec<String> = match dt.and_then(|d| d.as_array()) {
                Some(a) => a.iter().filter_map(|x| x.as_str().map(|s| s.to_string())).collect(),
                None => continue,
            };
            if dt.and_then(|d| d.as_array()).map_or(true, |a| a.is_empty()) {
                continue;
            }
            idx.labels.push(labs);
            idx.hashes.push(e.get("hash").and_then(|h| h.as_str()).unwrap_or("").to_string());
            let v: Vec<f64> = e.get("vector").and_then(|v| v.as_array())
                .map(|a| a.iter().filter_map(|x| x.as_f64()).collect()).unwrap_or_default();
            if v.is_empty() {
                idx.vecs.push(None);
            } else {
                let n = v.iter().map(|x| x * x).sum::<f64>().sqrt();
                let n = if n > 0.0 { n } else { 1.0 };
                idx.vecs.push(Some(v.iter().map(|x| x / n).collect()));
            }
            let title = e.get("title").and_then(|t| t.as_str()).unwrap_or("");
            let file = e.get("file").and_then(|t| t.as_str()).unwrap_or("");
            let f = field_tokens(title, file, spec, noise);
            let mut m = HashMap::new();
            for k in ["title", "name"] {
                m.insert(k.to_string(), count(f.get(k).map(|v| v.as_slice()).unwrap_or(&[])));
            }
            idx.toks.push(m);
        }
        idx
    }

    /// class_seed.jsonl 을 읽어 색인을 만든다. 파일이 없거나 깨진 줄은 건너뛴다.
    pub fn from_seed_file(path: &str, spec: &Bm25Spec, noise: &[String]) -> Self {
        let rows: Vec<Value> = std::fs::read_to_string(path).unwrap_or_default()
            .lines().filter(|l| !l.trim().is_empty())
            .filter_map(|l| serde_json::from_str::<Value>(l).ok()).collect();
        Self::from_rows(&rows, spec, noise)
    }

    pub fn size(&self) -> usize {
        self.labels.len()
    }

    /// 분류 단위 채점(파이썬 Bm25Index.rank 와 같은 순서 · 같은 식).
    pub fn rank(&self, q: &HashMap<String, Vec<String>>, skip: &HashSet<usize>) -> Ranking {
        let sp = &self.spec;
        let mut nseed: HashMap<String, usize> = HashMap::new();
        let mut cat: HashMap<String, HashMap<String, HashMap<String, u64>>> = HashMap::new();
        for (i, labs) in self.labels.iter().enumerate() {
            if skip.contains(&i) {
                continue;
            }
            for d in labs {
                *nseed.entry(d.clone()).or_insert(0) += 1;
                let c = cat.entry(d.clone()).or_insert_with(|| {
                    sp.fields.iter().map(|(f, _)| (f.clone(), HashMap::new())).collect()
                });
                for (f, _) in &sp.fields {
                    if let Some(src) = self.toks[i].get(f) {
                        let dst = c.get_mut(f).unwrap();
                        for (t, n) in src {
                            *dst.entry(t.clone()).or_insert(0) += n;
                        }
                    }
                }
            }
        }
        let mut cats: Vec<String> = cat.keys().filter(|d| nseed[*d] >= sp.min_seeds).cloned().collect();
        cats.sort();
        if cats.len() < sp.min_categories {
            return Ranking::default();
        }
        let n = cats.len() as f64;
        let mut df: HashMap<&str, u64> = HashMap::new();
        for d in &cats {
            let mut seen: HashSet<&str> = HashSet::new();
            for (f, _) in &sp.fields {
                for t in cat[d][f].keys() {
                    seen.insert(t.as_str());
                }
            }
            for t in seen {
                *df.entry(t).or_insert(0) += 1;
            }
        }
        let idf = |t: &str| -> f64 {
            let k = *df.get(t).unwrap_or(&0) as f64;
            (1.0 + (n - k + 0.5) / (k + 0.5)).ln().max(sp.idf_min).min(sp.idf_max)
        };
        let mut avg: HashMap<&str, f64> = HashMap::new();
        for (f, _) in &sp.fields {
            let total: u64 = cats.iter().map(|d| cat[d][f].values().sum::<u64>()).sum();
            let a = total as f64 / n;
            avg.insert(f.as_str(), if a == 0.0 { 1.0 } else { a });
        }
        let qset: HashMap<&str, Vec<&String>> = sp.fields.iter().map(|(f, _)| {
            let s: BTreeSet<&String> = q.get(f).map(|v| v.iter().collect()).unwrap_or_default();
            (f.as_str(), s.into_iter().collect())
        }).collect();
        let mut out = Ranking::default();
        for d in &cats {
            let mut total = 0.0;
            let mut by_f = HashMap::new();
            let mut hit = vec![];
            for (f, wt) in &sp.fields {
                let doc = &cat[d][f];
                let dl = doc.values().sum::<u64>() as f64;
                let mut sf = 0.0;
                for t in &qset[f.as_str()] {
                    let tf = *doc.get(*t).unwrap_or(&0) as f64;
                    if tf == 0.0 {
                        continue;
                    }
                    sf += wt * idf(t) * tf * (sp.k1 + 1.0)
                        / (tf + sp.k1 * (1.0 - sp.b + sp.b * dl / avg[f.as_str()]));
                    if f == "name" {
                        if let Some(w) = t.strip_prefix("w:") {
                            hit.push(w.to_string());
                        }
                    }
                }
                by_f.insert(f.clone(), sf);
                total += sf;
            }
            out.ranked.push((d.clone(), total));
            out.parts.insert(d.clone(), by_f);
            out.words.insert(d.clone(), hit);
        }
        out.ranked.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.0.cmp(&b.0)));
        out
    }
}

/// 1위 고르기 — 점수가 문턱 이상이고 1 · 2위 차가 margin 이상일 때만.
pub fn pick(rk: &Ranking, threshold: f64, margin: f64) -> Option<String> {
    let (d1, s1) = rk.ranked.first()?;
    if *s1 <= 0.0 {
        return None;
    }
    let s2 = rk.ranked.get(1).map(|x| x.1).unwrap_or(0.0);
    if *s1 < threshold || (s1 - s2) / s1 < margin {
        return None;
    }
    Some(d1.clone())
}

/// 자동 문턱 결과.
#[derive(Debug, Clone, Default)]
pub struct AutoThreshold {
    pub threshold: Option<f64>,
    pub passed: usize,
    pub correct: usize,
    pub scored: usize,
}

/// 자동 문턱(기준 문서끼리 자기 제외 모의시험, 설계서 §15).
pub fn auto_threshold(index: &Bm25Index, dup: f64) -> AutoThreshold {
    let sp = &index.spec;
    let mut stats: Vec<(f64, bool)> = vec![];
    for (i, labs) in index.labels.iter().enumerate() {
        let mut skip: HashSet<usize> = HashSet::new();
        skip.insert(i);
        for j in 0..index.size() {
            if j == i {
                continue;
            }
            let same_hash = !index.hashes[i].is_empty() && index.hashes[j] == index.hashes[i];
            let near = match (&index.vecs[i], &index.vecs[j]) {
                (Some(a), Some(b)) => a.iter().zip(b.iter()).map(|(x, y)| x * y).sum::<f64>() >= dup,
                _ => false,
            };
            if same_hash || near {
                skip.insert(j);
            }
        }
        let mut left: HashMap<&str, usize> = HashMap::new();
        for (j, lj) in index.labels.iter().enumerate() {
            if !skip.contains(&j) {
                for d in lj {
                    *left.entry(d.as_str()).or_insert(0) += 1;
                }
            }
        }
        if !labs.iter().any(|d| *left.get(d.as_str()).unwrap_or(&0) >= sp.min_seeds) {
            continue;
        }
        let q: HashMap<String, Vec<String>> = sp.fields.iter().map(|(f, _)| {
            (f.clone(), index.toks[i].get(f).map(|m| m.keys().cloned().collect()).unwrap_or_default())
        }).collect();
        let rk = index.rank(&q, &skip);
        if let Some((d, s)) = rk.ranked.first() {
            stats.push((*s, labs.contains(d)));
        }
    }
    let mut out = AutoThreshold { scored: stats.len(), ..Default::default() };
    let mut st = stats;
    st.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
    let mut ok = 0usize;
    for k in 1..=st.len() {
        let (score, good) = st[k - 1];
        if good {
            ok += 1;
        }
        // 같은 점수가 이어지면 그 묶음 끝에서만 판단한다(문턱은 '이상'이라 함께 통과한다).
        if k < st.len() && st[k].0 == score {
            continue;
        }
        if k >= sp.min_pass && ok as f64 / k as f64 >= sp.target_precision {
            out.threshold = Some(score);
            out.passed = k;
            out.correct = ok;
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn row(file: &str, dc: &str, title: &str, h: &str) -> Value {
        json!({"file": file, "doctype": [dc], "title": title, "hash": h})
    }

    fn rows() -> Vec<Value> {
        let mut out = vec![];
        for (dc, word) in [("M", "매뉴얼"), ("R", "보고서"), ("C", "계약서"), ("P", "제안서")] {
            for i in 0..3 {
                out.push(row(&format!("D:/x/{}_{}.docx", word, i), dc,
                             &format!("시스템 {} {}", word, i), &format!("{}{}", dc, i)));
            }
        }
        out
    }

    #[test]
    fn 토큰_2gram과_단어() {
        let none = HashSet::new();
        assert_eq!(tokenize("사용자매뉴얼", &none),
                   vec!["w:사용자매뉴얼", "g:사용", "g:용자", "g:자매", "g:매뉴", "g:뉴얼"]);
        assert_eq!(tokenize("Mpower 2023 a 목차 EZisC", &Bm25Spec::default().stopwords),
                   vec!["w:mpower", "w:ezisc"]);
    }

    #[test]
    fn 자간_벌린_말_붙이기() {
        assert_eq!(despace_spaced("출 장 여 비 규 정"), "출장여비규정");
        assert_eq!(despace_spaced("가 나 보고서"), "가 나 보고서");
        assert_eq!(tokenize("출 장 여 비 규 정", &HashSet::new())[0], "w:출장여비규정");
    }

    #[test]
    fn 파일명_정리() {
        let name = file_stem(r"D:\a\05.파일공유시스템_사용자매뉴얼(접속기)_V2.0_최종본.doc");
        assert_eq!(name, "05.파일공유시스템_사용자매뉴얼(접속기)_V2.0_최종본");
        let toks = tokenize(&clean_name(&name, &["최종본".to_string()]), &HashSet::new());
        assert_eq!(toks[0], "w:파일공유시스템");
        assert!(toks.contains(&"w:사용자매뉴얼".to_string()));
        assert!(!toks.iter().any(|t| t.contains("접속기") || t.contains("v2")));
    }

    #[test]
    fn 제목은_ppt_xls_에서_비운다() {
        assert_eq!(title_of("\n\n 첫 줄 제목\n둘째", "a.docx"), " 첫 줄 제목");
        assert_eq!(title_of("첫 줄", "a.pptx"), "첫 줄");
        assert_eq!(title_of("첫 줄", "a.ppt"), "");
        assert_eq!(title_of("첫 줄", "a.xlsx"), "");
    }

    #[test]
    fn 분류_단위_채점과_고르기() {
        let spec = Bm25Spec { enabled: true, ..Default::default() };
        let idx = Bm25Index::from_rows(&rows(), &spec, &[]);
        assert_eq!(idx.size(), 12);
        let q = field_tokens("시스템 매뉴얼 설치", "D:/y/관리자매뉴얼.docx", &spec, &[]);
        let rk = idx.rank(&q, &HashSet::new());
        assert_eq!(rk.ranked[0].0, "M");
        assert!(rk.ranked[0].1 > rk.ranked[1].1);
        assert!(rk.parts["M"]["title"] > 0.0 && rk.parts["M"]["name"] > 0.0);
        assert_eq!(pick(&rk, 0.0, 0.0).as_deref(), Some("M"));
        assert_eq!(pick(&rk, rk.ranked[0].1 + 1.0, 0.0), None);
    }

    #[test]
    fn 참여_분류가_모자라면_빈_결과() {
        let spec = Bm25Spec { enabled: true, ..Default::default() };
        let rs: Vec<Value> = rows().into_iter().filter(|r| r["doctype"][0] != "P").collect();
        let rk = Bm25Index::from_rows(&rs, &spec, &[])
            .rank(&field_tokens("매뉴얼", "a.docx", &spec, &[]), &HashSet::new());
        assert!(rk.ranked.is_empty());
    }

    fn tax() -> crate::axes::Taxonomy {
        let yaml = "taxonomy:\n  source: t\n  exported_at: '20260824000000'\n  node_count: 4\n  nodes:\n\
            \x20 - {dc_id: ROOT, parent: null, order: 1, title: 루트, status: 1}\n\
            \x20 - {dc_id: L1, parent: ROOT, order: 1, title: 잎1, status: 1}\n\
            \x20 - {dc_id: L2, parent: ROOT, order: 2, title: 잎2, status: 1}\n\
            \x20 - {dc_id: L3, parent: ROOT, order: 3, title: 잎3, status: 1}\n";
        let yv: serde_yaml::Value = serde_yaml::from_str(yaml).unwrap();
        crate::axes::taxonomy_from_value(&serde_json::to_value(&yv).unwrap(), "test").unwrap()
    }

    fn pk(dc: &str, title: f64) -> crate::doctype::Bm25Pick {
        crate::doctype::Bm25Pick { dc_id: dc.into(), score: 20.0, threshold: 15.0, title, name: 2.0,
                                   words: vec!["매뉴얼".into()] }
    }

    #[test]
    fn 합치기_네_가지_경우() {
        use crate::doctype::{merge_second_stage, Candidate};
        let t = tax();
        let spec = Bm25Spec { enabled: true, ..Default::default() };
        let cf = crate::doc_rules::ConflictSpec { strategy: "all".into(), n: None, min_confidence: None };
        let mut rule = Candidate::bare("L1".into(), t.path("L1").unwrap(), t.path_ids("L1").unwrap(), 0.8,
                                       vec!["title".into()]);
        rule.stage = "rule".into();
        let ex = vec![rule];
        let find = |sig: &crate::doctype::DoctypeSignal, d: &str| sig.values.iter().find(|v| v.dc_id == d).cloned().unwrap();
        // 1) 규칙이 이미 낸 분류 → 근거만, 확신 그대로
        let s = merge_second_stage(&ex, None, &t, &cf, None, Some((&pk("L1", 3.0), &spec)));
        let v = find(&s, "L1");
        assert_eq!(v.confidence, 0.8);
        assert!(v.from.contains(&"bm25".to_string()));
        assert_eq!(v.stage, "both");
        // 2) 벡터만 낸 분류 → 합의, cap_agree
        let emb = vec![("L2".to_string(), 0.5)];
        let s = merge_second_stage(&ex, Some((&emb, ("knn_vote", 0.8))), &t, &cf, Some(0.65), Some((&pk("L2", 3.0), &spec)));
        let v = find(&s, "L2");
        assert_eq!(v.confidence, 0.75);
        assert_eq!(v.stage, "embed+bm25");
        assert!(!v.review);
        // 3) 새 분류(제목에서도 맞음) → cap_alone · review · basis bm25
        let s = merge_second_stage(&ex, None, &t, &cf, None, Some((&pk("L3", 3.0), &spec)));
        let v = find(&s, "L3");
        assert_eq!(v.confidence, 0.6);
        assert!(v.review);
        assert_eq!(v.basis, "bm25");
        assert_eq!(s.as_dict()["values"].as_array().unwrap().iter().find(|x| x["dc_id"] == "L3").unwrap()
                   ["signals"]["bm25"]["field"], "title+name");
        // 4) 파일명에서만 맞음 → name_only_cap · basis bm25_name_only
        let s = merge_second_stage(&ex, None, &t, &cf, None, Some((&pk("L3", 0.0), &spec)));
        let v = find(&s, "L3");
        assert_eq!(v.confidence, 0.55);
        assert_eq!(v.basis, "bm25_name_only");
        let d = s.as_dict();
        let b = &d["values"].as_array().unwrap().iter().find(|x| x["dc_id"] == "L3").unwrap()["signals"]["bm25"];
        assert_eq!(b["field"], "name");
        assert_eq!(b["words"], serde_json::json!(["매뉴얼"]));
    }

    #[test]
    fn bm25_블록_검사() {
        let (sp, probs) = parse_spec(&json!({"enabled": true, "fields": {"title": 1.0, "name": 1.0},
            "threshold": {"mode": "auto", "target_precision": 0.95, "min_pass": 10, "min_margin": 0.0},
            "name_only": {"allow": true, "cap": 0.55}}));
        assert!(probs.is_empty());
        assert!(sp.enabled);
        assert_eq!(sp.fields, vec![("title".to_string(), 1.0), ("name".to_string(), 1.0)]);
        let (_, probs) = parse_spec(&json!({"tokenizer": "kiwi", "fields": {"head": 1},
            "threshold": {"mode": "x"}, "name_only": {"cap": 2}}));
        assert_eq!(probs.iter().map(|p| p.0).collect::<Vec<_>>(),
                   vec!["bm25.tokenizer", "bm25.fields", "bm25.threshold.mode", "bm25.name_only.cap"]);
        assert!(!parse_spec(&Value::Null).0.enabled);
    }

    #[test]
    fn 기준_문서_title_칸은_저장해도_남는다() {
        let dir = std::env::temp_dir().join(format!("bm25_seed_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("s.jsonl");
        let p = p.to_str().unwrap();
        crate::seedstore::save(p, &[json!({"v": 3, "file": "a.docx", "doctype": ["L1"],
                                           "title": "시스템 매뉴얼", "vector": [0.1, 0.2]})]).unwrap();
        let (rows, _) = crate::seedstore::load(p);
        assert_eq!(rows[0]["title"], "시스템 매뉴얼");
        let keys: Vec<&String> = rows[0].as_object().unwrap().keys().take(4).collect();
        assert_eq!(keys, vec!["v", "file", "doctype", "title"]);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn 자동_문턱() {
        let spec = Bm25Spec { enabled: true, min_pass: 4, ..Default::default() };
        let res = auto_threshold(&Bm25Index::from_rows(&rows(), &spec, &[]), 0.95);
        assert_eq!(res.scored, 0);
        assert!(res.threshold.is_none());
        let mut rs = rows();
        for i in 0..3 {
            rs.push(row(&format!("D:/x/매뉴얼_{}b.docx", i), "M", &format!("시스템 매뉴얼 {}b", i),
                        &format!("Mb{}", i)));
        }
        for (dc, word) in [("R", "보고서"), ("C", "계약서"), ("P", "제안서")] {
            rs.push(row(&format!("D:/x/{}_9.docx", word), dc, &format!("시스템 {} 9", word), &format!("{}9", dc)));
        }
        let res = auto_threshold(&Bm25Index::from_rows(&rs, &spec, &[]), 0.95);
        assert_eq!(res.scored, rs.len());
        assert!(res.threshold.is_some() && res.correct as f64 / res.passed as f64 >= 0.95);
    }
}
