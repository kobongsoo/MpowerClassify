//! 임베딩 라벨 전파(Signal B) — ko-pii classify/propagate.py 포팅.
//! seed(등급 붙은 문서 벡터)와 코사인 비교로 보류(등급 없음) 문서에 등급 '후보'를 만든다.
//! 정책: near-dup 상속 / 최근접 너무 멀면 보류 / 그 외 k-최근접 유사도 가중 다수결.

use std::fs;

use serde_json::{json, Value};

// 전파 임계값(propagate.py 상수).
const DEFAULT_K: usize = 10;
const DUP_THRESHOLD: f32 = 0.950; // 1등 sim ≥ → 그 등급 상속
const MIN_SIM: f32 = 0.55;        // 1등 sim < → 보류
const MIN_SHARE: f32 = 0.70;      // 1등 득표율 < → 보류

// doctype 축(업무분류) 전파 득표 임계 — 설계서 5-4. security 의 MIN_SHARE(0.70)를
// 그대로 쓰면 다중 라벨은 표가 여러 라벨로 갈라져 아무것도 안 걸린다. "1등만"
// 뽑는 게 아니라 "임계 넘는 라벨을 전부" 채택하는 구조라 기준을 낮춰야 한다.
// [설계 원문] "초기값은 실측 후 정한다" — 이 값은 실측 전 잠정치이며, 결과가
// 확정이 아니라 제안(Q2)이라 사람이 검토 화면에서 걸러 주는 것을 전제로 관대하게
// 잡았다. Python classify/propagate.py 의 MIN_SHARE_DOCTYPE 과 값을 맞춘다.
const MIN_SHARE_DOCTYPE: f32 = 0.25;

//------------------------------------------------------------------
// seed 파일의 벡터 차원 세기 — 파이썬 classify/propagate.py::seed_file_dims 와 같다
//=> 모델마다 벡터 차원이 다르다(e5 384 · ko-sroberta 768). 아래 cosine 은 zip 으로
//   짧은 쪽에 맞춰 잘라 곱하므로, 차원이 다른 seed 와 비교하면 오류 없이 엉뚱한
//   유사도가 나온다. 그래서 쓰기 전에 차원을 세어 본다.
//
// -in: path = seed 파일 경로
//
// -out: BTreeMap<차원, 건수> (파일이 없거나 벡터가 없으면 빈 맵)
// -out: error = 없음(읽기 실패·깨진 줄은 건너뜀)
//------------------------------------------------------------------
pub fn seed_file_dims(path: &str) -> std::collections::BTreeMap<usize, usize> {
    let mut dims = std::collections::BTreeMap::new();
    let text = match fs::read_to_string(path) { Ok(t) => t, Err(_) => return dims };
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() { continue; }
        if let Ok(v) = serde_json::from_str::<Value>(line) {
            if let Some(a) = v.get("vector").and_then(|x| x.as_array()) {
                if !a.is_empty() { *dims.entry(a.len()).or_insert(0) += 1; }
            }
        }
    }
    dims
}

/// seed 인덱스 — L2 정규화된 벡터 + 등급 + 파일경로.
pub struct SeedIndex {
    vectors: Vec<Vec<f32>>, // 각 행 L2 정규화
    grades: Vec<String>,
    files: Vec<String>,
}

impl SeedIndex {
    pub fn empty() -> Self { SeedIndex { vectors: vec![], grades: vec![], files: vec![] } }
    pub fn size(&self) -> usize { self.grades.len() }

    /// 리스트로 구성(행별 L2 정규화). 공통 내부.
    fn from_lists(mut vecs: Vec<Vec<f32>>, grades: Vec<String>, files: Vec<String>) -> Self {
        for v in vecs.iter_mut() { l2(v); }
        SeedIndex { vectors: vecs, grades, files }
    }

    /// 외부 seed 저장소(class_seed.jsonl): 각 줄 {file, grade, vector}. 깨진 줄은 건너뜀.
    pub fn from_seed_file(path: &str) -> Self {
        let mut vecs = vec![]; let mut grades = vec![]; let mut files = vec![];
        if let Ok(txt) = fs::read_to_string(path) {
            for line in txt.lines() {
                let line = line.trim();
                if line.is_empty() { continue; }
                let e: Value = match serde_json::from_str(line) { Ok(v) => v, Err(_) => continue };
                let g = e.get("grade").and_then(|g| g.as_str()).unwrap_or("");
                if !matches!(g, "C" | "S" | "O") { continue; }
                let v = match e.get("vector").and_then(|v| v.as_array()) { Some(a) => a, None => continue };
                let vec: Vec<f32> = v.iter().filter_map(|x| x.as_f64().map(|f| f as f32)).collect();
                if vec.is_empty() { continue; }
                vecs.push(vec);
                grades.push(g.to_string());
                files.push(e.get("file").and_then(|f| f.as_str()).unwrap_or("").to_string());
            }
        }
        Self::from_lists(vecs, grades, files)
    }

    /// 코퍼스 내부 seed: seed_eligible + 유효등급 + 벡터 존재 문서만.
    pub fn from_records(items: &[(Option<Vec<f32>>, Option<String>, bool, String)]) -> Self {
        let mut vecs = vec![]; let mut grades = vec![]; let mut files = vec![];
        for (vec, grade, seed_eligible, file) in items {
            if !seed_eligible { continue; }
            let g = match grade { Some(g) if matches!(g.as_str(), "C"|"S"|"O") => g.clone(), _ => continue };
            let v = match vec { Some(v) if !v.is_empty() => v.clone(), _ => continue };
            vecs.push(v); grades.push(g); files.push(file.clone());
        }
        Self::from_lists(vecs, grades, files)
    }

    /// 두 인덱스 병합(한쪽 비면 나머지 반환). 벡터는 이미 정규화됨.
    pub fn merge(mut a: SeedIndex, b: SeedIndex) -> SeedIndex {
        if a.size() == 0 { return b; }
        if b.size() == 0 { return a; }
        a.vectors.extend(b.vectors);
        a.grades.extend(b.grades);
        a.files.extend(b.files);
        a
    }

    /// 질의 벡터와 모든 seed 의 코사인 유사도.
    fn cosine(&self, vec: &[f32]) -> Vec<f32> {
        if self.size() == 0 { return vec![]; }
        let mut q = vec.to_vec();
        let n: f32 = q.iter().map(|x| x * x).sum::<f32>().sqrt();
        let n = if n > 0.0 { n } else { 1.0 };
        for x in q.iter_mut() { *x /= n; }
        self.vectors.iter().map(|sv| {
            sv.iter().zip(q.iter()).map(|(a, b)| a * b).sum::<f32>()
        }).collect()
    }
}

/// 전파 신호(Signal B) 결과.
pub struct EmbedSignal {
    pub grade: Option<String>,       // "C"/"S"/"O" 또는 None(보류)
    pub confidence: f32,
    pub method: String,              // dup_inherit|knn_vote|too_far|mixed|no_seeds
    pub top_sim: f32,
    pub neighbors: Vec<(String, String, f32)>, // (file, grade, sim) 상위 5
}

impl EmbedSignal {
    /// signals.embed 칸용 dict(Python EmbedSignal.as_dict 동일 스키마).
    pub fn as_dict(&self) -> Value {
        json!({
            "grade": self.grade,
            "confidence": round3(self.confidence),
            "seed_eligible": false,
            "method": self.method,
            "top_sim": round3(self.top_sim),
            "neighbors": self.neighbors.iter().map(|(f, g, s)| json!({
                "file": f, "grade": g, "sim": round3(*s)
            })).collect::<Vec<_>>(),
        })
    }
}

/// 소수 3자리 반올림 — **f64 로 돌려준다.**
/// f32 로 반올림한 뒤 JSON(f64)으로 넓히면 `0.913` 이 `0.9129999876022339` 로 적힌다
/// (f32 가 0.913 을 정확히 담지 못하기 때문). 먼저 f64 로 넓힌 뒤 반올림해야
/// 파이썬 `round(x, 3)` 과 같은 `0.913` 이 나온다. 판정에는 쓰지 않고 출력에만 쓴다.
fn round3(x: f32) -> f64 { ((x as f64) * 1000.0).round() / 1000.0 }
fn l2(v: &mut [f32]) {
    let n: f32 = v.iter().map(|x| x * x).sum::<f32>().sqrt();
    if n > 1e-9 { for x in v.iter_mut() { *x /= n; } }
}

/// 보안등급 전파에 쓸 임계값 묶음 — cso_rule.yaml 의 embed: 블록.
/// [왜 인자로 받는가] 예전에는 위 모듈 상수를 그대로 써서 정책 파일로 못 바꿨다.
/// 업무분류(DoctypeParams)와 같은 방식으로 받아, 칸이 없으면 Default = 위 상수를 쓴다.
#[derive(Debug, Clone, Copy)]
pub struct SecurityParams {
    pub k: usize,
    pub dup_threshold: f32,
    pub min_sim: f32,
    pub min_share: f32,
}

impl Default for SecurityParams {
    //------------------------------------------------------------------
    // 기본 임계값 — 모듈 상수 그대로
    //=> 정책 파일에 embed: 칸이 없을 때 종전과 똑같이 동작하게 한다.
    //
    // -in: 없음
    //
    // -out: SecurityParams = 모듈 상수로 채운 묶음
    // -out: error = 없음
    //------------------------------------------------------------------
    fn default() -> Self {
        SecurityParams { k: DEFAULT_K, dup_threshold: DUP_THRESHOLD,
                         min_sim: MIN_SIM, min_share: MIN_SHARE }
    }
}

/// 벡터 1개 전파 — propagate.py 판정순서 그대로.
/// 임계값은 p(cso_rule.yaml 의 embed:)를 따른다.
pub fn propagate(vec: &[f32], seeds: &SeedIndex, p: &SecurityParams) -> EmbedSignal {
    if seeds.size() == 0 {
        return EmbedSignal { grade: None, confidence: 0.0, method: "no_seeds".into(), top_sim: 0.0, neighbors: vec![] };
    }
    let sims = seeds.cosine(vec);
    // 유사도 내림차순 상위 k.
    let mut order: Vec<usize> = (0..sims.len()).collect();
    order.sort_by(|&a, &b| sims[b].partial_cmp(&sims[a]).unwrap_or(std::cmp::Ordering::Equal));
    order.truncate(p.k.max(1));
    let top: Vec<(String, String, f32)> = order.iter().map(|&i| {
        (seeds.files.get(i).cloned().unwrap_or_default(), seeds.grades[i].clone(), sims[i])
    }).collect();
    let (_, top1_grade, top1_sim) = top[0].clone();
    let neigh: Vec<(String, String, f32)> = top.iter().take(5).cloned().collect();

    // ② near-dup 상속.
    if top1_sim >= p.dup_threshold {
        return EmbedSignal { grade: Some(top1_grade), confidence: top1_sim, method: "dup_inherit".into(), top_sim: top1_sim, neighbors: neigh };
    }
    // ③ 너무 멂 → 보류.
    if top1_sim < p.min_sim {
        return EmbedSignal { grade: None, confidence: top1_sim, method: "too_far".into(), top_sim: top1_sim, neighbors: neigh };
    }
    // ④ 유사도 가중 다수결.
    let mut buckets: std::collections::HashMap<String, f32> = std::collections::HashMap::new();
    for (_, g, s) in &top {
        *buckets.entry(g.clone()).or_insert(0.0) += s.max(0.0);
    }
    let total: f32 = buckets.values().sum::<f32>().max(1e-9);
    let (winner, wsum) = buckets.iter().max_by(|a, b| a.1.partial_cmp(b.1).unwrap_or(std::cmp::Ordering::Equal)).unwrap();
    let share = wsum / total;
    if share < p.min_share {
        return EmbedSignal { grade: None, confidence: share, method: "mixed".into(), top_sim: top1_sim, neighbors: neigh };
    }
    EmbedSignal { grade: Some(winner.clone()), confidence: share, method: "knn_vote".into(), top_sim: top1_sim, neighbors: neigh }
}

// ============================================================================
// doctype 축(업무분류) 전파 — Python classify/propagate.py 의 DoctypeSeedIndex/
// propagate_doctype 포팅(로드맵 D7). security 의 SeedIndex 와 같은 구조이지만
// seed 하나가 등급 하나가 아니라 dc_id "집합"을 가진다 — 확정된 문서가 여러
// 분류에 동시에 속할 수 있어서다(설계서 5-4: "사본 상속... 라벨 집합 전체 상속").
// class_seed.jsonl 의 doctype 필드를 읽는다(v3). security 의 grade 필드와는
// 다른 키라 SeedIndex 와 파일을 공유해도 서로 간섭하지 않는다("한 파일에 두 축").
// ============================================================================

/// doctype seed 인덱스 — L2 정규화된 벡터 + dc_id 집합 + 파일경로.
pub struct DoctypeSeedIndex {
    vectors: Vec<Vec<f32>>,
    label_sets: Vec<Vec<String>>,
    files: Vec<String>,
}

impl DoctypeSeedIndex {
    pub fn empty() -> Self {
        DoctypeSeedIndex { vectors: vec![], label_sets: vec![], files: vec![] }
    }
    pub fn size(&self) -> usize {
        self.label_sets.len()
    }

    /// 외부 seed 저장소(class_seed.jsonl): doctype 이 있는 줄만 담는다.
    /// grade 만 있는(security 전용) 줄이나 doctype 이 비어 있는 줄은
    /// 조용히 건너뛴다 — doctype 축을 안 쓰는 배포의 seed 파일을 그대로 읽어도 안전하다.
    /// v3 는 평평한 doctype 칸, v1/v2 는 labels.doctype — 둘 다 읽는다.
    pub fn from_seed_file(path: &str) -> Self {
        let mut vecs = vec![]; let mut label_sets = vec![]; let mut files = vec![];
        if let Ok(txt) = fs::read_to_string(path) {
            for line in txt.lines() {
                let line = line.trim();
                if line.is_empty() { continue; }
                let e: Value = match serde_json::from_str(line) { Ok(v) => v, Err(_) => continue };
                // v3 는 doctype 이 평평한 칸이다. 옛 파일(v1/v2)은 labels.doctype
                // 에 있으므로 그것도 본다 — 배포판이 여러 벌 도는 동안 둘 다 돈다.
                let dt = e.get("doctype")
                    .or_else(|| e.get("labels").and_then(|l| l.get("doctype")));
                let dc_ids: Vec<String> = match dt.and_then(|d| d.as_array()) {
                    Some(a) => a.iter().filter_map(|x| x.as_str().map(|s| s.to_string())).collect(),
                    None => continue,
                };
                if dc_ids.is_empty() { continue; }
                let v = match e.get("vector").and_then(|v| v.as_array()) { Some(a) => a, None => continue };
                let vec: Vec<f32> = v.iter().filter_map(|x| x.as_f64().map(|f| f as f32)).collect();
                if vec.is_empty() { continue; }
                vecs.push(vec);
                label_sets.push(dc_ids);
                files.push(e.get("file").and_then(|f| f.as_str()).unwrap_or("").to_string());
            }
        }
        let mut idx = DoctypeSeedIndex { vectors: vecs, label_sets, files };
        for v in idx.vectors.iter_mut() { l2(v); }
        idx
    }

    fn cosine(&self, vec: &[f32]) -> Vec<f32> {
        if self.size() == 0 { return vec![]; }
        let mut q = vec.to_vec();
        let n: f32 = q.iter().map(|x| x * x).sum::<f32>().sqrt();
        let n = if n > 0.0 { n } else { 1.0 };
        for x in q.iter_mut() { *x /= n; }
        self.vectors.iter().map(|sv| sv.iter().zip(q.iter()).map(|(a, b)| a * b).sum::<f32>()).collect()
    }
}

/// doctype 축 전파 신호(Signal B) 결과. security 의 EmbedSignal 과 같은 역할이지만
/// "값 하나"가 아니라 "채택된 후보 여러 개"를 담는다(설계서 5-4 — 다중 라벨은
/// 1등을 뽑는 게 아니라 임계값을 넘는 라벨을 전부 채택). seed_eligible 개념 자체가
/// 없다 — 전파로 붙은 doctype 라벨은 애초에 "제안(proposed)"일 뿐이다.
pub struct DoctypeEmbedSignal {
    pub values: Vec<(String, f32)>, // (dc_id, confidence)
    pub method: String,             // dup_inherit|knn_vote|too_far|no_seeds
    pub top_sim: f32,
    pub neighbors: Vec<(String, Vec<String>, f32)>, // (file, dc_ids, sim) 상위 5
}

impl DoctypeEmbedSignal {
    /// signals.doctype_embed 칸용 dict.
    pub fn as_dict(&self) -> Value {
        json!({
            "values": self.values.iter().map(|(id, c)| json!({"dc_id": id, "confidence": round3(*c)})).collect::<Vec<_>>(),
            "method": self.method,
            "top_sim": round3(self.top_sim),
            "neighbors": self.neighbors.iter().map(|(f, ids, s)| json!({
                "file": f, "dc_ids": ids, "sim": round3(*s)
            })).collect::<Vec<_>>(),
        })
    }
}

/// 벡터 1개 doctype 전파 — propagate() 와 판정 순서(①~④)는 같지만, ④가 "1등
/// 하나"가 아니라 "임계값을 넘는 라벨을 전부" 채택한다는 점만 다르다.
/// doctype 전파에 쓸 임계값 묶음.
/// [왜 인자로 받는가] 예전에는 모듈 상수를 그대로 썼다. 그래서 doc_rule.yaml 의
/// embed: 블록에 값을 적어도 Rust 는 무시했고, 같은 정책 파일을 읽는 Python 과
/// 다른 라벨을 낼 수 있었다. 실측 재보정에서 min_sim 을 0.55 → 0.70 으로 올린
/// 것이 정밀도를 가장 크게 좌우했으므로, 반영되지 않으면 결과가 눈에 띄게 갈린다.
#[derive(Debug, Clone, Copy)]
pub struct DoctypeParams {
    pub k: usize,
    pub dup_threshold: f32,
    pub min_sim: f32,
    pub min_share: f32,
}

impl Default for DoctypeParams {
    fn default() -> Self {
        DoctypeParams { k: DEFAULT_K, dup_threshold: DUP_THRESHOLD,
                        min_sim: MIN_SIM, min_share: MIN_SHARE_DOCTYPE }
    }
}

pub fn propagate_doctype(vec: &[f32], seeds: &DoctypeSeedIndex, p: DoctypeParams) -> DoctypeEmbedSignal {
    if seeds.size() == 0 {
        return DoctypeEmbedSignal { values: vec![], method: "no_seeds".into(), top_sim: 0.0, neighbors: vec![] };
    }
    let sims = seeds.cosine(vec);
    let mut order: Vec<usize> = (0..sims.len()).collect();
    order.sort_by(|&a, &b| sims[b].partial_cmp(&sims[a]).unwrap_or(std::cmp::Ordering::Equal));
    order.truncate(p.k);
    let top: Vec<(String, Vec<String>, f32)> = order.iter().map(|&i| {
        (seeds.files.get(i).cloned().unwrap_or_default(), seeds.label_sets[i].clone(), sims[i])
    }).collect();
    let top1_sim = top[0].2;
    let neigh: Vec<(String, Vec<String>, f32)> = top.iter().take(5).cloned().collect();

    // ② 1등이 사본 수준이면 그 seed 의 dc_id "전체"를 그대로 상속.
    if top1_sim >= p.dup_threshold {
        let values = top[0].1.iter().map(|id| (id.clone(), top1_sim)).collect();
        return DoctypeEmbedSignal { values, method: "dup_inherit".into(), top_sim: top1_sim, neighbors: neigh };
    }
    // ③ 1등조차 너무 멀면 믿을 이웃이 없다.
    if top1_sim < p.min_sim {
        return DoctypeEmbedSignal { values: vec![], method: "too_far".into(), top_sim: top1_sim, neighbors: neigh };
    }
    // ④ 라벨별 독립 점수 — "1등을 뽑는" 대신 임계 넘는 라벨을 전부 채택한다.
    let total: f32 = top.iter().map(|(_, _, s)| s.max(0.0)).sum::<f32>().max(1e-9);
    let mut scores: std::collections::HashMap<String, f32> = std::collections::HashMap::new();
    for (_, labels, s) in &top {
        for dc_id in labels {
            *scores.entry(dc_id.clone()).or_insert(0.0) += s.max(0.0);
        }
    }
    let values: Vec<(String, f32)> = scores.into_iter()
        .map(|(id, sum)| (id, sum / total))
        .filter(|(_, share)| *share >= p.min_share)
        .collect();
    DoctypeEmbedSignal { values, method: "knn_vote".into(), top_sim: top1_sim, neighbors: neigh }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mk_index() -> DoctypeSeedIndex {
        // A 군집(축0)·B 군집(축1)·A+B 동시(축0·1 중간).
        let entries = [
            ("a1", vec!["A"], vec![1.0_f32, 0.0, 0.0, 0.0]),
            ("a2", vec!["A"], vec![0.95, 0.05, 0.0, 0.0]),
            ("b1", vec!["B"], vec![0.0, 1.0, 0.0, 0.0]),
            ("b2", vec!["B"], vec![0.0, 0.95, 0.05, 0.0]),
            ("ab1", vec!["A", "B"], vec![0.707, 0.707, 0.0, 0.0]),
        ];
        let mut idx = DoctypeSeedIndex::empty();
        for (file, labels, vec) in entries {
            idx.vectors.push(vec);
            idx.label_sets.push(labels.into_iter().map(|s| s.to_string()).collect());
            idx.files.push(file.to_string());
        }
        for v in idx.vectors.iter_mut() { l2(v); }
        idx
    }

    #[test]
    fn seed_없으면_no_seeds() {
        let idx = DoctypeSeedIndex::empty();
        let sig = propagate_doctype(&[1.0, 0.0, 0.0, 0.0], &idx, DoctypeParams::default());
        assert!(sig.values.is_empty());
        assert_eq!(sig.method, "no_seeds");
    }

    #[test]
    fn 너무_멀면_too_far() {
        let idx = mk_index();
        let sig = propagate_doctype(&[0.0, 0.0, 0.0, 1.0], &idx, DoctypeParams::default());
        assert!(sig.values.is_empty());
        assert_eq!(sig.method, "too_far");
    }

    #[test]
    fn near_dup은_라벨_집합_전체_상속() {
        let idx = mk_index();
        let sig = propagate_doctype(&[0.707, 0.707, 0.0, 0.0], &idx, DoctypeParams::default());
        assert_eq!(sig.method, "dup_inherit");
        let mut ids: Vec<&str> = sig.values.iter().map(|(id, _)| id.as_str()).collect();
        ids.sort();
        assert_eq!(ids, vec!["A", "B"]);
    }

    #[test]
    fn 한쪽_라벨_근처는_그_라벨만() {
        let mut idx = DoctypeSeedIndex::empty();
        for (file, labels, mut vec) in [
            ("a1", vec!["A".to_string()], vec![1.0_f32, 0.0, 0.0, 0.0]),
            ("a2", vec!["A".to_string()], vec![0.9, 0.1, 0.0, 0.0]),
            ("b1", vec!["B".to_string()], vec![0.0, 0.0, 1.0, 0.0]),
        ] {
            l2(&mut vec);
            idx.vectors.push(vec);
            idx.label_sets.push(labels);
            idx.files.push(file.to_string());
        }
        let sig = propagate_doctype(&[0.8, 0.2, 0.0, 0.0], &idx, DoctypeParams::default());
        let ids: Vec<&str> = sig.values.iter().map(|(id, _)| id.as_str()).collect();
        assert_eq!(ids, vec!["A"]);
    }

    #[test]
    fn 라벨별_독립점수_둘다_넘으면_둘다_채택() {
        // ab1 이 섞인 mk_index() 는 대각선 방향 벡터라 "중간 지점" 질의가 항상
        // ab1 과 near-dup(sim≥0.95)이 돼 버린다(코사인은 각도만 보므로). 그래서
        // A·B 단일라벨 seed 두 개만으로 "둘 다 어느 정도 가깝지만 사본은 아닌"
        // 상황을 별도로 구성한다.
        let mut idx = DoctypeSeedIndex::empty();
        for (file, labels, mut vec) in [
            ("a1", vec!["A".to_string()], vec![1.0_f32, 0.0, 0.0, 0.0]),
            ("b1", vec!["B".to_string()], vec![0.0, 1.0, 0.0, 0.0]),
        ] {
            l2(&mut vec);
            idx.vectors.push(vec);
            idx.label_sets.push(labels);
            idx.files.push(file.to_string());
        }
        let sig = propagate_doctype(&[0.6, 0.5, 0.0, 0.0], &idx, DoctypeParams::default());
        assert_eq!(sig.method, "knn_vote");
        let ids: Vec<&str> = sig.values.iter().map(|(id, _)| id.as_str()).collect();
        assert!(ids.contains(&"A") && ids.contains(&"B"), "둘 다 채택돼야 함: {:?}", ids);
        for (_, conf) in &sig.values {
            assert!(*conf >= MIN_SHARE_DOCTYPE && *conf <= 1.0);
        }
    }

    #[test]
    fn from_seed_file은_doctype_라벨만_담는다() {
        let tmp = std::env::temp_dir().join(format!("cso_test_dtseed_{}.jsonl", std::process::id()));
        let lines = [
            r#"{"file":"a","labels":{"doctype":["DC1"]},"vector":[1,0,0,0]}"#,
            r#"{"file":"b","grade":"S","vector":[0,1,0,0]}"#,
            r#"{"file":"c","labels":{"doctype":[]},"vector":[0,0,1,0]}"#,
            r#"{"file":"d","labels":{"doctype":["DC2"]}}"#,
        ];
        std::fs::write(&tmp, lines.join("\n")).unwrap();
        let idx = DoctypeSeedIndex::from_seed_file(tmp.to_str().unwrap());
        let _ = std::fs::remove_file(&tmp);
        assert_eq!(idx.size(), 1);
        assert_eq!(idx.label_sets[0], vec!["DC1".to_string()]);
    }

    #[test]
    fn 파일없으면_빈인덱스() {
        let idx = DoctypeSeedIndex::from_seed_file("D:/no/such/file.jsonl");
        assert_eq!(idx.size(), 0);
    }
}
