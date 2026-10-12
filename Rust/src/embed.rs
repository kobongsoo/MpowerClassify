//! 임베딩(전파/--vector-only 용) — ONNX 임베딩 모델을 onnxruntime 으로 돌린다.
//! 모델·런타임은 바이너리에 넣지 않고 '외부 파일'로 로딩한다(pdfium 방식):
//!   · onnxruntime.dll : 환경변수 ORT_DYLIB_PATH → exe 옆 onnxruntime.dll
//!   · 모델 폴더        : 아래 `model_dir()` 차례(파이썬 판 resources._model_dir_candidates 와 같다)
//! 모델은 파이썬 판 `config.MODELS` 와 같은 세 가지(e5-small-ko · e5-small · ko-sroberta)이고
//! `--model` 로 고른다(기본 e5-small-ko). 파이프라인은 파이썬 판과 같다:
//! 모델별 프리픽스 + 슬라이딩청크 + 평균풀링 + 청크평균 + L2.

use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use ort::session::{builder::GraphOptimizationLevel, Session, SessionInputValue};
use ort::value::Tensor;
use tokenizers::Tokenizer;

// 모델 폴더의 부모 이름(exe 옆 기준). 에러 메시지(main.rs)도 같은 상수를 쓴다.
pub const MODEL_ROOT: &str = "models";

//------------------------------------------------------------------
// 임베딩 모델 1개의 스펙 — 파이썬 판 config.ModelSpec 과 같은 칸
//=> "별칭 → 폴더·차원·프리픽스·토큰 상한"을 묶어 둔 표의 한 행이다.
//   e5 계열은 문서 앞에 'passage: ' 를 붙여야 성능이 나오고, sroberta 계열은 붙이지 않는다.
//
// -필드: key              = --model 에서 쓰는 별칭
// -필드: local_dir        = models/ 아래 폴더명(model.onnx · tokenizer.json 위치)
// -필드: dim              = 출력 벡터 차원(seed 와 비교할 때 이 값이 같아야 한다)
// -필드: passage_prefix   = 문서에 붙일 프리픽스(없으면 "")
// -필드: max_tokens_model = 모델이 받는 최대 토큰(안전 상한)
//------------------------------------------------------------------
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ModelSpec {
    pub key: &'static str,
    pub local_dir: &'static str,
    pub dim: usize,
    pub passage_prefix: &'static str,
    pub max_tokens_model: usize,
}

/// 지원 모델 표 — 파이썬 판 `config.MODELS` 와 **같아야 한다**(차례·값까지).
/// `tests/test_errcodes.py` 가 두 표를 한 칸씩 대조한다. 한쪽만 고치면 같은 `--model`
/// 인데 두 판이 다른 벡터를 만든다.
pub const MODELS: &[ModelSpec] = &[
    ModelSpec { key: "e5-small-ko", local_dir: "e5-small-ko", dim: 384,
                passage_prefix: "passage: ", max_tokens_model: 512 },
    ModelSpec { key: "e5-small", local_dir: "e5-small", dim: 384,
                passage_prefix: "passage: ", max_tokens_model: 512 },
    ModelSpec { key: "ko-sroberta", local_dir: "ko-sroberta", dim: 768,
                passage_prefix: "", max_tokens_model: 512 },
];

/// 기본 모델 별칭(파이썬 판 `config.DEFAULT_MODEL`).
pub const DEFAULT_MODEL: &str = "e5-small-ko";

// 이번 실행에 쓸 모델 — 인자 해석(--model)에서 한 번 정한다. 안 정하면 기본 모델.
static SELECTED: OnceLock<&'static ModelSpec> = OnceLock::new();

//------------------------------------------------------------------
// 별칭 → 스펙
//=> 표에서 찾는다. 없으면 None — 부르는 쪽이 '알 수 없는 모델'(1003)로 알린다.
//
// -in: key = 모델 별칭
//
// -out: Option<&ModelSpec> = 찾은 스펙
// -out: error = 없음
//------------------------------------------------------------------
pub fn spec_of(key: &str) -> Option<&'static ModelSpec> {
    MODELS.iter().find(|m| m.key == key)
}

//------------------------------------------------------------------
// 이번 실행의 모델 정하기 (--model)
//=> 인자 해석에서 한 번 부른다. 모르는 이름이면 false — 부르는 쪽이 1003 으로 멈춘다.
//   두 번째 호출은 무시된다(한 실행에 모델은 하나다).
//
// -in: key = 모델 별칭
//
// -out: bool = 표에 있는 이름이면 true
// -out: error = 없음
//------------------------------------------------------------------
pub fn select(key: &str) -> bool {
    match spec_of(key) {
        Some(s) => { let _ = SELECTED.set(s); true }
        None => false,
    }
}

//------------------------------------------------------------------
// 이번 실행의 모델 스펙
//=> --model 로 정했으면 그것, 아니면 기본 모델.
//
// -in: 없음
//
// -out: &ModelSpec = 이번 실행에 쓰는 스펙
// -out: error = 없음(기본 모델은 표에 늘 있다)
//------------------------------------------------------------------
pub fn current() -> &'static ModelSpec {
    SELECTED.get().copied().unwrap_or_else(|| spec_of(DEFAULT_MODEL).expect("기본 모델이 표에 없다"))
}

/// 모델 별칭 목록(안내 문구용) — "e5-small-ko, e5-small, ko-sroberta".
pub fn model_keys() -> String {
    MODELS.iter().map(|m| m.key).collect::<Vec<_>>().join(", ")
}

// 청크 크기·겹침 — 파이썬 판은 --max-tokens/--overlap 으로 바꿀 수 있지만 이 판은
// 이 값으로 고정이다. main.rs 가 다른 값을 받으면 1012 로 멈추려고 공개한다.
pub const CHUNK_MAX_TOKENS: usize = 512; // DEFAULT_MAX_TOKENS
pub const CHUNK_OVERLAP: usize = 32;     // DEFAULT_OVERLAP

/// 임베더 — onnxruntime 세션 + 토크나이저 + 이번 모델 스펙.
pub struct Embedder {
    session: Session,
    tokenizer: Tokenizer,
    prefix_ids_len: usize,
    spec: &'static ModelSpec,
    // 모델이 실제로 받는 입력 이름 — 모델마다 token_type_ids 유무가 다르다(RoBERTa 는 없다).
    input_names: Vec<String>,
}

//------------------------------------------------------------------
// 모델 폴더 후보(차례대로) — 파이썬 판 _model_dir_candidates 와 같은 차례
//=> 1) CSO_MODEL (이 판 전용 — 모델 폴더를 그대로 가리킨다)
//   2) CSOCLASSIFY_MODELS_DIR/<local_dir>
//   3) 캐시 %LOCALAPPDATA%/MpowerClassify/models/<local_dir> (없으면 ~/.csoclassify/…)
//   4) exe 옆 models/<local_dir>, exe 옆 resources/models/<local_dir>
//   파이썬 판의 '번들 안 경로'와 '.tar.xz 압축 풀기'는 이 판에 없다.
//
// -in: spec = 이번 모델 스펙
//
// -out: Vec<PathBuf> = 후보 폴더(존재 여부는 확인 안 함)
// -out: error = 없음
//------------------------------------------------------------------
fn model_dir_candidates(spec: &ModelSpec) -> Vec<PathBuf> {
    let mut c = vec![];
    if let Ok(env) = std::env::var("CSOCLASSIFY_MODELS_DIR") {
        if !env.is_empty() { c.push(PathBuf::from(env).join(spec.local_dir)); }
    }
    let base = std::env::var("LOCALAPPDATA").ok().filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .or_else(|| std::env::var("USERPROFILE").or_else(|_| std::env::var("HOME")).ok()
                 .map(|h| PathBuf::from(h).join(".csoclassify")));
    if let Some(b) = base { c.push(b.join("MpowerClassify").join(MODEL_ROOT).join(spec.local_dir)); }
    if let Some(dir) = std::env::current_exe().ok().and_then(|e| e.parent().map(|p| p.to_path_buf())) {
        c.push(dir.join(MODEL_ROOT).join(spec.local_dir));
        c.push(dir.join("resources").join(MODEL_ROOT).join(spec.local_dir));
    }
    c
}

//------------------------------------------------------------------
// 이번 모델의 폴더 정하기
//=> CSO_MODEL 이 폴더면 그대로 쓴다(알맹이 확인은 check_deployment 가 한다 — 빈 폴더를
//   주면 'model.onnx 없음'으로 정확히 알린다). 아니면 후보 중 model.onnx 가 있는 첫 폴더
//   (파이썬 판 resolve_model_dir 과 같은 규칙).
//
// -in: 없음(current() 스펙을 쓴다)
//
// -out: Option<PathBuf> = 모델 폴더(못 찾으면 None)
// -out: error = 없음
//------------------------------------------------------------------
fn model_dir() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("CSO_MODEL") {
        let pb = PathBuf::from(p);
        if pb.is_dir() { return Some(pb); }
    }
    model_dir_candidates(current()).into_iter().find(|d| d.join("model.onnx").is_file())
}

//------------------------------------------------------------------
// 이번 모델의 폴더(밖에서 묻는 용도)
//=> 상주 서버는 자기가 실제로 연 모델 폴더를 기록에 남기고, 부르는 쪽은 자기가
//   직접 열 폴더와 같은지 본다. 환경변수(CSO_MODEL 등)가 달라 서로 다른 모델을
//   보고 있으면, 같은 문서인데 서버를 쓸 때와 안 쓸 때 벡터가 달라지기 때문이다.
//
// -in: 없음(current() 스펙을 쓴다)
//
// -out: Option<PathBuf> = 모델 폴더(못 찾으면 None)
// -out: error = 없음
//------------------------------------------------------------------
pub fn resolved_model_dir() -> Option<PathBuf> {
    model_dir()
}

/// onnxruntime 동적 라이브러리 경로: ORT_DYLIB_PATH 유지 or exe 옆 onnxruntime.dll 지정.
fn ensure_dylib() {
    if std::env::var("ORT_DYLIB_PATH").map(|v| !v.is_empty()).unwrap_or(false) {
        return;
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            for name in ["onnxruntime.dll", "libonnxruntime.so", "libonnxruntime.dylib"] {
                let pb = dir.join(name);
                if pb.is_file() {
                    std::env::set_var("ORT_DYLIB_PATH", pb);
                    return;
                }
            }
        }
    }
}

/// 배포 점검 — 세션을 만들지 않고 '파일이 자리에 있는가'만 본다(stat 몇 번).
///
/// 지연 로딩(§설계 04)이 들어가면서, 규칙만으로 끝나는 실행은 모델을 아예 읽지 않는다.
/// 그런데 이 프로세스의 모델 파일을 미리 봐 주는 명령이 따로 없어 **`load()` 시도 자체가
/// 배포 누락을 알아챌 유일한 신호**였다. 그 신호가 사라진 자리를 메우는 값싼 확인이다.
/// 여기서 세션을 만들면 아끼려던 비용을 도로 내는 셈이므로, 파일 존재까지만 본다.
///
/// 반환: Ok(()) 정상 / Err(사유) — 사유는 사람이 읽고 바로 고칠 수 있는 문장이어야 한다.
pub fn check_deployment() -> Result<(), String> {
    // ORT_DYLIB_PATH 를 채워 주는 부수효과가 있다 — 뒤에서 실제 load() 할 때도 그대로 쓰인다.
    ensure_dylib();
    let dir = match model_dir() {
        Some(d) => d,
        None => return Err(format!(
            "모델 폴더를 찾지 못했습니다(exe 옆 {}/{} 또는 CSO_MODEL·CSOCLASSIFY_MODELS_DIR)",
            MODEL_ROOT, current().local_dir)),
    };
    // 폴더만 있고 알맹이가 없는 배포가 실제로 있었다 — 파일 단위로 본다.
    for name in ["model.onnx", "tokenizer.json"] {
        let p = dir.join(name);
        if !p.is_file() {
            return Err(format!("{} 이(가) 없습니다: {}", name, p.display()));
        }
    }
    // dll 은 ensure_dylib 가 찾았거나, 사용자가 환경변수로 직접 준 것이다.
    // 환경변수가 '없는 경로'를 가리키는 경우도 여기서 걸러야 한다(설정 오타).
    match std::env::var("ORT_DYLIB_PATH") {
        Ok(p) if !p.is_empty() && Path::new(&p).is_file() => Ok(()),
        _ => Err("onnxruntime 동적 라이브러리를 찾지 못했습니다\
                  (exe 옆 onnxruntime.dll 또는 ORT_DYLIB_PATH)".to_string()),
    }
}

impl Embedder {
    /// 모델 로드. 실패(모델/런타임 없음·초기화 실패)면 None.
    /// with_optimization_level => 모델 메모리에 올리고 웨이트 계산.
    /// commit_from_file => 모델 파일을 불러옴.
    pub fn load() -> Option<Embedder> {
        ensure_dylib();
        let spec = current();
        let dir = model_dir()?;
        let tok = Tokenizer::from_file(dir.join("tokenizer.json")).ok()?;
        let session = Session::builder().ok()?
            .with_optimization_level(GraphOptimizationLevel::Level3).ok()?
            .commit_from_file(dir.join("model.onnx")).ok()?;
        // 프리픽스 토큰 길이(특수토큰 제외) — 청크 예산 계산용. 프리픽스가 없는 모델은 0.
        let prefix_ids_len = if spec.passage_prefix.is_empty() { 0 } else {
            tok.encode(spec.passage_prefix, false).map(|e| e.get_ids().len()).unwrap_or(0)
        };
        // 모델이 실제로 받는 입력 이름을 기억한다 — 없는 입력을 넣으면 추론이 실패한다.
        let input_names = session.inputs.iter().map(|i| i.name.clone()).collect();
        Some(Embedder { session, tokenizer: tok, prefix_ids_len, spec, input_names })
    }

    /// 문서 임베딩(정규화된 384벡터). 청크별 평균풀링 → 청크평균 → L2.
    ///
    /// 입력 텍스트는 먼저 `clean_for_embed` 로 다듬는다 — 파이썬은 추출 직후
    /// `clean.clean_text()` 를 거친 텍스트를 임베딩하는데, Rust 가 추출 원문을 그대로
    /// 넣으면 끝 개행 하나 때문에 같은 문서인데도 벡터가 달라진다(실측 코사인 0.98).
    /// 벡터는 `class_seed.jsonl` 과 코사인으로 비교되므로, 이 차이는 전파 판정을
    /// 조용히 흔든다. 그래서 '임베딩에 넣기 직전' 에 파이썬과 같은 모양으로 맞춘다.
    pub fn embed_document(&mut self, text: &str) -> Option<Vec<f32>> {
        let text = &clean_for_embed(text);
        if text.trim().is_empty() { return None; }
        let chunks = self.split_chunks(text);
        if chunks.is_empty() { return None; }
        // 차원은 모델마다 다르다(384·768) — 스펙 값으로 누적한다.
        let dim = self.spec.dim;
        let mut acc = vec![0f32; dim];
        let mut n = 0usize;
        for c in &chunks {
            let v = self.embed_one(c)?;
            // 모델 출력 차원이 스펙과 다르면(엉뚱한 폴더의 모델) 벡터를 만들지 않는다 —
            // 다른 차원 벡터가 seed 와 섞이면 비교가 조용히 틀어진다.
            if v.len() != dim { return None; }
            for i in 0..dim { acc[i] += v[i]; }
            n += 1;
        }
        if n == 0 { return None; }
        for i in 0..dim { acc[i] /= n as f32; }
        l2_normalize(&mut acc);
        Some(acc)
    }

    /// 본문을 프리픽스+특수토큰 예산에 맞춰 슬라이딩 청크(텍스트)로 분할.
    fn split_chunks(&self, text: &str) -> Vec<String> {
        let enc = match self.tokenizer.encode(text, false) { Ok(e) => e, Err(_) => return vec![] };
        let ids = enc.get_ids();
        if ids.is_empty() { return vec![]; }
        let hard_cap = CHUNK_MAX_TOKENS.min(self.spec.max_tokens_model);
        let effective = hard_cap.saturating_sub(self.prefix_ids_len + 2).max(8);
        let eff_overlap = CHUNK_OVERLAP.min(effective - 1);
        let mut out = vec![];
        for (s, e) in sliding_windows(ids.len(), effective, eff_overlap) {
            let sub = &ids[s..e];
            if let Ok(txt) = self.tokenizer.decode(sub, false) {
                out.push(txt);
            }
        }
        out
    }

    /// 청크 문자열 1개 → 평균풀링 벡터(정규화 전).
    fn embed_one(&mut self, chunk_text: &str) -> Option<Vec<f32>> {
        // 모델별 프리픽스(e5 는 'passage: ', sroberta 는 없음).
        let full = format!("{}{}", self.spec.passage_prefix, chunk_text);
        let enc = self.tokenizer.encode(full, true).ok()?;
        let mut ids: Vec<i64> = enc.get_ids().iter().map(|&x| x as i64).collect();
        let mut mask: Vec<i64> = enc.get_attention_mask().iter().map(|&x| x as i64).collect();
        let cap = self.spec.max_tokens_model;
        if ids.len() > cap { ids.truncate(cap); mask.truncate(cap); }
        let seq = ids.len();
        // 모델이 요구하는 입력만 넣는다(파이썬 판과 같다) — RoBERTa 계열은 token_type_ids 가 없다.
        let has = |n: &str| self.input_names.iter().any(|x| x == n);
        let mut feeds: Vec<(&str, SessionInputValue)> = vec![];
        if has("input_ids") {
            feeds.push(("input_ids", Tensor::from_array(([1usize, seq], ids)).ok()?.into()));
        }
        if has("attention_mask") {
            feeds.push(("attention_mask", Tensor::from_array(([1usize, seq], mask.clone())).ok()?.into()));
        }
        if has("token_type_ids") {
            feeds.push(("token_type_ids", Tensor::from_array(([1usize, seq], vec![0i64; seq])).ok()?.into()));
        }
        let outputs = self.session.run(feeds).ok()?;
        // 첫 출력을 last_hidden_state (1, seq, dim) 로 본다 — 파이썬 판과 같다(이름은 모델마다 다를 수 있다).
        let (shape, data) = outputs[0].try_extract_tensor::<f32>().ok()?;
        // shape = [1, seq, dim]
        let sq = shape[1] as usize;
        let dim = shape[2] as usize;
        // 마스크 평균 풀링.
        let mut vec = vec![0f32; dim];
        let mut denom = 0f32;
        for t in 0..sq {
            let m = mask.get(t).copied().unwrap_or(0) as f32;
            if m == 0.0 { continue; }
            denom += m;
            let base = t * dim;
            for d in 0..dim { vec[d] += data[base + d] * m; }
        }
        let denom = if denom > 1e-9 { denom } else { 1.0 };
        for d in 0..dim { vec[d] /= denom; }
        Some(vec)
    }
}

//------------------------------------------------------------------
// 임베딩 수단 — 상주 서버 우선, 안 되면 직접 로드
//=> 분류 루프는 "텍스트를 주면 벡터가 온다"만 알면 된다. 벡터가 상주 서버에서 왔는지
//   이 프로세스가 모델을 직접 읽어 만들었는지는 여기서 감춘다.
//   서버를 못 쓰게 되면 그 실행의 남은 문서는 전부 직접 로드로 처리한다(문서마다
//   다시 20초씩 서버를 기다리지 않게).
//
// -필드: client   = 상주 서버 클라이언트(--daemon 이 아니거나 서버를 포기했으면 None)
// -필드: local    = 직접 올린 모델(처음 필요할 때 한 번 올린다)
// -필드: local_ok = 직접 로드를 시도해도 되는가(배포 점검 통과 여부). 점검이 실패했는데
//                   load() 를 부르면 ort 가 패닉할 수 있어, false 면 아예 부르지 않는다
// -필드: failed   = 서버도 직접 로드도 못 써서 벡터를 못 만든 적이 있는가
//------------------------------------------------------------------
pub struct EmbedBackend {
    client: Option<crate::daemon::client::DaemonClient>,
    local: Option<Embedder>,
    local_ok: bool,
    failed: bool,
}

impl EmbedBackend {
    //------------------------------------------------------------------
    // 임베딩 수단 열기
    //=> 상주 서버를 쓰는 실행이면 클라이언트만 들고 시작한다(접속은 첫 문서 때).
    //   아니면 지금까지처럼 모델을 바로 올린다.
    //
    // -in: client   = 상주 서버 클라이언트(--daemon 일 때만 Some)
    // -in: local_ok = 배포 점검(check_deployment) 통과 여부
    //
    // -out: Option<EmbedBackend> = 쓸 수 있는 수단. 서버도 안 쓰고 직접 로드도 실패하면 None
    // -out: error = 없음
    //------------------------------------------------------------------
    pub fn open(client: Option<crate::daemon::client::DaemonClient>, local_ok: bool) -> Option<EmbedBackend> {
        if client.is_some() {
            return Some(EmbedBackend { client, local: None, local_ok, failed: false });
        }
        if !local_ok { return None; }
        let local = Embedder::load()?;
        Some(EmbedBackend { client: None, local: Some(local), local_ok, failed: false })
    }

    //------------------------------------------------------------------
    // 문서 임베딩
    //=> 1) 상주 서버가 있으면 거기에 맡긴다
    //   2) 서버를 못 쓰면 한 줄 알리고, 이후로는 직접 로드로 간다
    //   3) 직접 로드도 안 되면 failed 를 세우고 None
    //
    // -in: text = 문서 본문(정제는 embed_document 안에서 한다 — 서버든 직접이든 같다)
    //
    // -out: Option<Vec<f32>> = 정규화된 문서 벡터. 빈 텍스트·임베딩 실패·수단 없음이면 None
    // -out: error = 없음(수단을 못 쓴 경우는 failed() 로 따로 묻는다)
    //------------------------------------------------------------------
    pub fn embed_document(&mut self, text: &str) -> Option<Vec<f32>> {
        use crate::daemon::client::EmbedReply;
        if let Some(c) = self.client.as_mut() {
            match c.embed_text(text) {
                EmbedReply::Vector(v) => return Some(v),
                EmbedReply::NoVector => return None,
                EmbedReply::Unavailable(why) => {
                    // 결과는 같고 속도만 다르다 — 오류가 아니라 알림으로 남긴다.
                    crate::note!("[MpowerClassify-rs] 상주 서버를 쓸 수 없어 모델을 직접 읽습니다 — {}", why);
                    crate::linfo!("상주 서버 불가 → 직접 로드 :: {}", why);
                    self.client = None;
                }
            }
        }
        if self.failed { return None; }
        if self.local.is_none() {
            // 배포 점검이 실패했으면 load() 를 부르지 않는다(ort 패닉 방지).
            self.local = if self.local_ok { Embedder::load() } else { None };
            if self.local.is_none() {
                self.failed = true;
                return None;
            }
        }
        self.local.as_mut().and_then(|e| e.embed_document(text))
    }

    /// 서버도 직접 로드도 못 써서 벡터를 못 만든 적이 있는가.
    pub fn failed(&self) -> bool {
        self.failed
    }
}

/// 임베딩 입력 정제 — 파이썬 `csoclassify/clean.py::clean_text()` 와 같은 규칙.
///
/// 순서까지 파이썬과 맞춰야 결과가 같아진다:
///   1) `..PAGE:N` 만 있는 줄 제거(사이냅 페이지 마커 — 본문이 아니다)
///   2) 제어문자 제거(탭·개행은 남긴다 — 문서 구조를 지워버리면 안 되므로)
///   3) CRLF·CR → LF
///   4) 줄 안의 연속 공백/탭 → 공백 1개
///   5) 각 줄 끝 공백 제거
///   6) 빈 줄 3개 이상 → 2개(문단 경계는 남긴다)
///   7) 양끝 공백 제거  ← 실측된 차이의 대부분이 여기(파일 끝 개행)에서 났다
///
/// 파이썬의 (1) NFC 유니코드 정규화만 빠져 있다 — 새 크레이트를 들이지 않는다는
/// 제약 때문이다. 추출기들이 이미 완성형 한글을 내놓아 실측 18건에서는 차이가
/// 없었지만, 자모가 분리된 문서가 들어오면 그 문서만 벡터가 달라질 수 있다.
pub fn clean_for_embed(text: &str) -> String {
    if text.is_empty() { return String::new(); }

    // (1) 페이지 마커 라인 제거. 정규식 대신 줄 단위로 직접 본다 — 규칙이 단순하고
    //     (공백* "..PAGE:" 숫자+ 공백*) 정규식 크레이트를 여기까지 끌고 올 이유가 없다.
    let mut s = String::with_capacity(text.len());
    for (i, line) in text.split('\n').enumerate() {
        if i > 0 { s.push('\n'); }
        if !is_page_marker(line) { s.push_str(line); }
    }

    // (2)(3) 제어문자 제거 + 개행 통일.
    //     CRLF 는 개행 하나여야 하므로 CR 을 볼 때 바로 뒤 LF 를 삼킨다(peek).
    //     단독 CR(옛 맥 개행)은 그 자리에서 LF 로 바꾼다.
    let mut t = String::with_capacity(s.len());
    let mut it = s.chars().peekable();
    while let Some(ch) = it.next() {
        match ch {
            '\r' => {
                if it.peek() == Some(&'\n') { it.next(); }   // CRLF → 개행 하나
                t.push('\n');
            }
            '\t' | '\n' => t.push(ch),
            c if (c as u32) < 0x20 || (c as u32) == 0x7f => {}   // 나머지 제어문자는 버린다
            c => t.push(c),
        }
    }

    // (4)(5) 줄 안 연속 공백 축약 + 줄 끝 공백 제거.
    let mut u = String::with_capacity(t.len());
    for (i, line) in t.split('\n').enumerate() {
        if i > 0 { u.push('\n'); }
        let mut prev_ws = false;
        let mut line_out = String::with_capacity(line.len());
        for c in line.chars() {
            if c == ' ' || c == '\t' {
                // 연속 공백/탭은 공백 하나로 — 파이썬 [ \t]{2,} → " " 와 같다.
                if !prev_ws { line_out.push(' '); }
                prev_ws = true;
            } else {
                prev_ws = false;
                line_out.push(c);
            }
        }
        u.push_str(line_out.trim_end());
    }

    // (6) 빈 줄 3개 이상 → 2개. 줄 수를 세며 한 번에 처리한다.
    let mut out = String::with_capacity(u.len());
    let mut nl = 0usize;
    for c in u.chars() {
        if c == '\n' {
            nl += 1;
            if nl <= 2 { out.push('\n'); }
        } else {
            nl = 0;
            out.push(c);
        }
    }

    // (7) 양끝 공백 제거.
    out.trim().to_string()
}

/// `..PAGE:12` 처럼 페이지 마커만 있는 줄인가(앞뒤 공백 허용).
fn is_page_marker(line: &str) -> bool {
    let t = line.trim();
    match t.strip_prefix("..PAGE:") {
        Some(rest) => !rest.is_empty() && rest.chars().all(|c| c.is_ascii_digit()),
        None => false,
    }
}

/// L2 정규화(0벡터는 그대로).
pub fn l2_normalize(v: &mut [f32]) {
    let n: f32 = v.iter().map(|x| x * x).sum::<f32>().sqrt();
    if n > 1e-9 { for x in v.iter_mut() { *x /= n; } }
}

/// 슬라이딩 윈도우 경계(ko-pii chunk.sliding_windows 동일).
fn sliding_windows(seq_len: usize, max_tokens: usize, overlap: usize) -> Vec<(usize, usize)> {
    if seq_len == 0 { return vec![]; }
    if seq_len <= max_tokens { return vec![(0, seq_len)]; }
    let step = max_tokens - overlap;
    let mut spans = vec![];
    let mut start = 0;
    while start < seq_len {
        let end = (start + max_tokens).min(seq_len);
        spans.push((start, end));
        if end >= seq_len { break; }
        start += step;
    }
    spans
}

#[cfg(test)]
mod tests {
    use super::clean_for_embed;

    // 파이썬 clean.clean_text() 와 같은 결과가 나오는지 — 실측 차이의 원인이었던
    // '파일 끝 개행'이 핵심이다. 이게 남으면 같은 문서인데 벡터가 달라진다.
    #[test]
    fn trims_trailing_newline() {
        assert_eq!(clean_for_embed("hello world test document\n"), "hello world test document");
        assert_eq!(clean_for_embed("\n\n  본문  \n\n"), "본문");
    }

    #[test]
    fn removes_page_markers() {
        assert_eq!(clean_for_embed("가\n..PAGE:12\n나"), "가\n\n나");
        assert_eq!(clean_for_embed("가\n  ..PAGE:3  \n나"), "가\n\n나");
        // 숫자가 아니면 마커가 아니다 — 본문을 지우면 안 된다.
        assert_eq!(clean_for_embed("..PAGE:abc"), "..PAGE:abc");
    }

    #[test]
    fn normalizes_newlines_and_controls() {
        // CRLF 는 개행 하나여야 한다(둘로 늘어나면 빈 줄이 생겨 토큰이 달라진다).
        assert_eq!(clean_for_embed("가\r\n나"), "가\n나");
        // 단독 CR(옛 맥 개행)도 개행 하나.
        assert_eq!(clean_for_embed("가\r나"), "가\n나");
        // 탭·개행 외 제어문자는 버린다.
        assert_eq!(clean_for_embed("가\u{0}\u{1}나"), "가나");
        // 탭은 남되 연속 공백 축약 규칙을 탄다.
        assert_eq!(clean_for_embed("가\t나"), "가 나");
    }

    #[test]
    fn collapses_spaces_and_blank_lines() {
        assert_eq!(clean_for_embed("가   나"), "가 나");
        assert_eq!(clean_for_embed("가  \t 나"), "가 나");
        // 줄 끝 공백 제거.
        assert_eq!(clean_for_embed("가   \n나"), "가\n나");
        // 빈 줄 3개 이상 → 2개(문단 경계는 남긴다).
        assert_eq!(clean_for_embed("가\n\n\n\n\n나"), "가\n\n나");
        assert_eq!(clean_for_embed("가\n\n나"), "가\n\n나");
    }

    #[test]
    fn empty_stays_empty() {
        assert_eq!(clean_for_embed(""), "");
        assert_eq!(clean_for_embed("   \n\t\n  "), "");
    }
}
