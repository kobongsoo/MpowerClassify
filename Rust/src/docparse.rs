//! 문서파서 창구 — docparse.dll / libdocparse.so 를 불러 쓴다
//!
//! 설계: plan/Rust-문서파서-라이브러리분리-설계-20261110.html (개정 2) §6-1
//!
//! 문서 → 본문 추출은 별도 프로젝트(D:\Project\DocParse-rust)가 만든 공유 라이브러리가 한다.
//! 이 모듈은 그 라이브러리를 실행 중에 한 번 불러(docparse_dyn) 예전 docparse 크레이트와
//! 같은 이름·모양의 함수와 타입을 내준다. 그래서 부르는 쪽(main·extractcfg·limits)은
//! `docparse::extract_as`·`detect::detect_format`·`archive::expand_paths` 를 예전처럼 쓴다.
//!
//! 라이브러리를 찾는 순서: --docparse-lib 인자(set_explicit_path) → 환경변수 CSO_DOCPARSE
//! → 실행파일 옆 → 실행파일 폴더의 부모(cargo test). 못 찾으면 문서를 읽기 전에
//! 1017 extractor_missing 으로 끝난다(문서마다 조용히 실패하지 않게).

// 예전 docparse 크레이트와 같은 모양을 내주느라, 이 프로그램이 지금 안 읽는 필드(Extracted.engine
// 등)도 그대로 둔다.
#![allow(dead_code)]

use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use std::time::Duration;

use serde_json::{json, Value};

use crate::docparse_dyn::DocParse;

/// 파서가 '부분 추출' 같은 운영 경고를 남길 때 쓰는 log target.
/// 이 target 으로 온 기록은 오류 로그(class_err_*.log)에도 적는다.
pub const ERRLOG_TARGET: &str = "docparse::errlog";

static LIB: OnceLock<DocParse> = OnceLock::new();
static EXPLICIT: OnceLock<PathBuf> = OnceLock::new();
// 로그 받기를 원하는지(install_logger 가 켠다). 라이브러리를 불러오는 순간 이 값을 본다.
static LOGGER_WANTED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

//------------------------------------------------------------------
// 라이브러리 경로를 직접 지정 (--docparse-lib)
//=> 처음 불러오기 전에 불러야 효과가 있다. 지정하면 그 경로만 본다.
//
// -in: p = 라이브러리 파일 또는 그것이 든 폴더
//
// -out: 없음
// -out: error = 없음(두 번째 지정은 무시)
//------------------------------------------------------------------
pub fn set_explicit_path(p: PathBuf) {
    let _ = EXPLICIT.set(p);
}

//------------------------------------------------------------------
// 라이브러리 불러오기(처음 한 번)
//=> 이미 불러왔으면 그것을 돌려준다. 실패를 돌려주므로 부른 쪽이 어떻게 끝낼지 정한다.
//
// -in: 없음
//
// -out: &'static DocParse = 불러온 라이브러리(프로세스가 끝날 때까지 산다)
// -out: error = 못 찾거나 ABI 가 다르면 Err(사람용 사유)
//------------------------------------------------------------------
pub fn try_lib() -> Result<&'static DocParse, String> {
    if let Some(l) = LIB.get() {
        return Ok(l);
    }
    let l = DocParse::find_and_load(EXPLICIT.get().map(PathBuf::as_path), "CSO_DOCPARSE")?;
    // 두 스레드가 동시에 불렀으면 먼저 넣은 쪽이 남는다(같은 파일이라 결과는 같다).
    if LIB.set(l).is_ok() {
        let l = LIB.get().expect("방금 넣었다");
        // 처음 불러온 때 한 번 — 로그 받기를 달고, 어떤 파서로 뽑는지 남긴다.
        if LOGGER_WANTED.load(std::sync::atomic::Ordering::Relaxed) {
            l.set_logger(Some(on_log), 3, std::ptr::null_mut());
        }
        crate::linfo!("문서파서 docparse={} lib={}", l.version(), l.path().display());
    }
    Ok(LIB.get().expect("방금 넣었다"))
}

//------------------------------------------------------------------
// 라이브러리 꺼내기 — 못 불러오면 프로그램을 끝낸다
//=> 추출 함수들이 쓴다. 라이브러리 없이 문서를 읽으면 모든 문서가 '추출 실패'로 흘러
//   원인이 가려지므로, 분명한 문장과 1017 코드로 바로 멈춘다.
//
// -in: 없음
//
// -out: &'static DocParse
// -out: error = 실패 시 errcodes::fail("extractor_missing") — 반환하지 않음
//------------------------------------------------------------------
pub fn lib() -> &'static DocParse {
    match try_lib() {
        Ok(l) => l,
        Err(why) => crate::errcodes::fail("extractor_missing", &format!("[MpowerClassify-rs] {}", why), None),
    }
}

//------------------------------------------------------------------
// 형식 이름을 고정 문자열로 맞추기
//=> 라이브러리 결과 JSON 의 "fmt" 는 새 String 이라, Fmt 가 쥘 &'static str 로 바꾼다.
//
// -in: s = 형식 이름
//
// -out: &'static str = 아는 이름이면 그 이름, 모르면 "unknown"
// -out: error = 없음
//------------------------------------------------------------------
fn static_fmt(s: &str) -> &'static str {
    const NAMES: [&str; 16] = ["pdf", "hwp", "hwpx", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
                               "html", "text", "image", "ole", "zip", "binary", "unknown"];
    NAMES.iter().copied().find(|n| *n == s).unwrap_or("unknown")
}

//------------------------------------------------------------------
// 문서 형식 (예전 docparse::Fmt 자리)
//=> 라이브러리가 감지한 형식 이름을 든다. 부르는 쪽은 as_str() 만 쓴다.
// -필드: 0 = 형식 이름("pdf"·"hwp"·…·"unknown")
//------------------------------------------------------------------
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Fmt(&'static str);

impl Fmt {
    //------------------------------------------------------------------
    // 형식 이름
    //
    // -in: 없음
    //
    // -out: &'static str = "pdf"·"hwp"·…·"unknown"
    // -out: error = 없음
    //------------------------------------------------------------------
    pub fn as_str(&self) -> &'static str {
        self.0
    }
}

//------------------------------------------------------------------
// 파일 내용으로 형식 감지
//
// -in: path = 파일 경로
//
// -out: Fmt = 감지된 형식
// -out: error = 라이브러리를 못 불러오면 1017 로 끝남
//------------------------------------------------------------------
pub fn detect_format(path: &Path) -> Fmt {
    Fmt(lib().detect(path))
}

/// 예전 `docparse::detect` 모듈 자리 — 부르는 쪽이 `detect::detect_format` 으로 쓴다.
#[allow(unused_imports)]
pub mod detect {
    pub use super::{detect_format, Fmt};
}

//------------------------------------------------------------------
// 추출 엔진 — 어느 추출기로 본문을 뽑을지
// -필드: Builtin       = 자체 파서만(기본)
// -필드: SynapFallback = 자체 파서 1차, 실패·빈·짧은 결과면 사이냅(snf_exe 경로)
// -필드: SynapOnly     = 모든 문서를 사이냅으로(snf_exe 경로)
//------------------------------------------------------------------
#[derive(Debug, Clone, PartialEq)]
pub enum Engine {
    Builtin,
    SynapFallback(PathBuf),
    SynapOnly(PathBuf),
}

impl Engine {
    //------------------------------------------------------------------
    // 로그에 적을 엔진 이름
    //
    // -in: 없음
    //
    // -out: &str = "builtin" · "snf_fallback" · "snf_only"
    // -out: error = 없음
    //------------------------------------------------------------------
    pub fn name(&self) -> &'static str {
        match self {
            Engine::Builtin => "builtin",
            Engine::SynapFallback(_) => "snf_fallback",
            Engine::SynapOnly(_) => "snf_only",
        }
    }
}

//------------------------------------------------------------------
// 추출 상한 (예전 docparse::Limits 와 같은 필드)
// -필드: parser_timeout      = 파서 시간 상한(None = 없음)
// -필드: max_pdf_pages       = PDF 쪽수 상한(None = 없음)
// -필드: max_file_bytes      = 파일 크기 상한(None = 없음)
// -필드: max_file_bytes_text = 텍스트류 크기 상한(None = 없음)
// -필드: snf_timeout         = 사이냅 시간 상한
// -필드: min_text_len        = 사이냅 폴백 '짧은 결과' 기준 글자 수
//------------------------------------------------------------------
#[derive(Debug, Clone, PartialEq)]
pub struct Limits {
    pub parser_timeout: Option<Duration>,
    pub max_pdf_pages: Option<usize>,
    pub max_file_bytes: Option<u64>,
    pub max_file_bytes_text: Option<u64>,
    pub snf_timeout: Duration,
    pub min_text_len: usize,
}

impl Limits {
    //------------------------------------------------------------------
    // 라이브러리 옵션 JSON 의 "limits" 객체로
    //=> 값을 전부 적어 보낸다(라이브러리 기본값에 기대지 않는다). None = 0 = 상한 없음.
    //
    // -in: 없음
    //
    // -out: Value = {"parser_timeout_secs":…, …}
    // -out: error = 없음
    //------------------------------------------------------------------
    fn to_json(&self) -> Value {
        json!({
            "parser_timeout_secs": self.parser_timeout.map_or(0.0, |d| d.as_secs_f64()),
            "max_pdf_pages": self.max_pdf_pages.unwrap_or(0),
            "max_file_bytes": self.max_file_bytes.unwrap_or(0),
            "max_file_bytes_text": self.max_file_bytes_text.unwrap_or(0),
            "snf_timeout_secs": self.snf_timeout.as_secs_f64(),
            "min_text_len": self.min_text_len,
        })
    }
}

//------------------------------------------------------------------
// 추출 옵션 (예전 docparse::ExtractOptions 와 같은 필드)
// -필드: engine     = 추출 엔진
// -필드: limits     = 상한
// -필드: pdfium_lib = pdfium 경로(None = 라이브러리 폴더 → 실행파일 옆 → 시스템)
// -필드: clean      = true 면 6단계 정제까지
// -필드: label      = 로그에 적을 문서 이름
//------------------------------------------------------------------
#[derive(Debug, Clone)]
pub struct ExtractOptions {
    pub engine: Engine,
    pub limits: Limits,
    pub pdfium_lib: Option<PathBuf>,
    pub clean: bool,
    pub label: Option<String>,
}

impl ExtractOptions {
    //------------------------------------------------------------------
    // 라이브러리 옵션 JSON 으로
    //
    // -in: fmt = 이미 아는 형식(주면 라이브러리가 감지를 건너뛴다)
    //
    // -out: Value = docparse.h 의 옵션 JSON
    // -out: error = 없음
    //------------------------------------------------------------------
    fn to_json(&self, fmt: Option<Fmt>) -> Value {
        let (engine, snf) = match &self.engine {
            Engine::Builtin => ("builtin", None),
            Engine::SynapFallback(p) => ("synap_fallback", Some(p)),
            Engine::SynapOnly(p) => ("synap_only", Some(p)),
        };
        let mut v = json!({"engine": engine, "clean": self.clean, "limits": self.limits.to_json()});
        if let Some(p) = snf {
            v["synap_exe"] = json!(p.to_string_lossy());
        }
        if let Some(p) = &self.pdfium_lib {
            v["pdfium_lib"] = json!(p.to_string_lossy());
        }
        if let Some(l) = &self.label {
            v["label"] = json!(l);
        }
        if let Some(f) = fmt {
            v["fmt"] = json!(f.as_str());
        }
        v
    }
}

//------------------------------------------------------------------
// 부분 추출 표식
// -필드: reason = 사유 · unit = 단위 · read = 읽은 수 · total = 전체 수
//------------------------------------------------------------------
#[derive(Debug, Clone, PartialEq)]
pub struct Partial {
    pub reason: String,
    pub unit: String,
    pub read: usize,
    pub total: usize,
}

//------------------------------------------------------------------
// PDF 페이지 구성
// -필드: pages = 읽은 쪽 수 · scan_pages = 그림만 있는 쪽 수
//------------------------------------------------------------------
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Layout {
    pub pages: usize,
    pub scan_pages: usize,
}

//------------------------------------------------------------------
// 추출 결과
// -필드: text    = 본문
// -필드: engine  = 실제로 뽑은 엔진 이름
// -필드: fmt     = 형식
// -필드: partial = 부분 추출 표식
// -필드: layout  = PDF 페이지 구성
//------------------------------------------------------------------
#[derive(Debug, Clone)]
pub struct Extracted {
    pub text: String,
    pub engine: String,
    pub fmt: Fmt,
    pub partial: Option<Partial>,
    pub layout: Option<Layout>,
}

//------------------------------------------------------------------
// 추출 실패 (예전 docparse::ExtractError 와 같은 갈래)
// -필드: SizeLimit   = 크기 상한 초과(message = 라이브러리가 만든 "크기 상한 초과: …" 문장)
// -필드: Unsupported = 지원하지 않는 형식
// -필드: Failed      = 파싱 실패(사유가 빈 문자열이면 '자체 파서가 못 읽음')
//------------------------------------------------------------------
#[derive(Debug, Clone)]
pub enum ExtractError {
    SizeLimit { size: u64, limit: u64, text_kind: bool, message: String },
    Unsupported(Fmt),
    Failed(String),
}

impl std::fmt::Display for ExtractError {
    //------------------------------------------------------------------
    // 사람이 읽는 실패 문장 — 분리 전과 같은 문장
    //
    // -in: f = 출력 대상
    //
    // -out: fmt::Result
    // -out: error = 없음
    //------------------------------------------------------------------
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ExtractError::SizeLimit { message, .. } => f.write_str(message),
            ExtractError::Unsupported(fmt) => write!(f, "지원하지 않는 포맷(감지={})", fmt.as_str()),
            ExtractError::Failed(why) => f.write_str(why),
        }
    }
}

impl std::error::Error for ExtractError {}

//------------------------------------------------------------------
// 형식을 알고 있을 때의 본문 추출
//=> 라이브러리에 옵션 JSON 을 넘겨 뽑고, 결과를 예전 타입으로 옮긴다.
//   라이브러리 내부 오류(arg·panic)는 Failed(사유)로 바꿔 '추출 실패' 경로로 보낸다.
//
// -in: path = 파일 경로
// -in: fmt  = 형식
// -in: opts = 추출 옵션
//
// -out: Extracted = 추출 결과
// -out: error = 실패 시 ExtractError
//------------------------------------------------------------------
pub fn extract_as(path: &Path, fmt: Fmt, opts: &ExtractOptions) -> Result<Extracted, ExtractError> {
    use crate::docparse_dyn::ExtractError as E;
    match lib().extract(path, &opts.to_json(Some(fmt))) {
        Ok(x) => Ok(Extracted {
            text: x.text,
            engine: x.engine,
            fmt: Fmt(static_fmt(&x.fmt)),
            partial: x.partial.map(|p| Partial { reason: p.reason, unit: p.unit, read: p.read, total: p.total }),
            layout: x.layout.map(|l| Layout { pages: l.pages, scan_pages: l.scan_pages }),
        }),
        Err(E::SizeLimit { size, limit, text_kind, message }) => Err(ExtractError::SizeLimit { size, limit, text_kind, message }),
        Err(E::Unsupported { fmt, .. }) => Err(ExtractError::Unsupported(Fmt(static_fmt(&fmt)))),
        Err(E::Failed { message }) => Err(ExtractError::Failed(message)),
        Err(e @ (E::Arg { .. } | E::Panic { .. })) => Err(ExtractError::Failed(format!("문서파서 오류: {}", e))),
    }
}

//------------------------------------------------------------------
// 스캔 PDF 판정
//
// -in: pages      = 읽은 쪽 수
// -in: scan_pages = 그림만 있는 쪽 수
// -in: body_chars = 공백 뺀 본문 글자 수
//
// -out: bool = 스캔 PDF(처리제외)면 true
// -out: error = 없음
//------------------------------------------------------------------
pub fn scan_pdf_verdict(pages: usize, scan_pages: usize, body_chars: usize) -> bool {
    lib().scan_pdf_verdict(pages, scan_pages, body_chars)
}

//------------------------------------------------------------------
// 라이브러리 로그 → 이 프로그램 로그 (C 콜백)
//=> 예전 log 브리지와 같은 규칙으로 적는다.
//    1) target 이 "docparse" 로 시작하고 info 이하만
//    2) target "docparse::errlog" → errlog::err(일반 로그 ERROR + 화면 + 오류 로그)
//    3) 그 밖(info) → 일반 로그가 켜져 있을 때 "모듈:줄" 위치와 함께
//
// -in: level  = 1=error … 5=trace
// -in: target = log target
// -in: site   = "모듈경로:줄"
// -in: msg    = 문장
// -in: _user  = 쓰지 않음
//
// -out: 없음
// -out: error = 없음
//------------------------------------------------------------------
extern "C" fn on_log(level: std::ffi::c_int, target: *const std::ffi::c_char, site: *const std::ffi::c_char,
                     msg: *const std::ffi::c_char, _user: *mut std::ffi::c_void) {
    // 콜백 안에서 패닉이 나면 C 경계를 넘으므로 전부 잡는다.
    let _ = std::panic::catch_unwind(|| {
        let s = |p: *const std::ffi::c_char| if p.is_null() { String::new() }
                    else { unsafe { std::ffi::CStr::from_ptr(p) }.to_string_lossy().into_owned() };
        let (target, site, msg) = (s(target), s(site), s(msg));
        if !target.starts_with("docparse") || level > 3 {
            return;
        }
        if target == ERRLOG_TARGET {
            crate::errlog::err(&msg);
            return;
        }
        if !crate::log::is_on() {
            return;
        }
        crate::log::line("INFO", &site, &msg);
    });
}

//------------------------------------------------------------------
// 라이브러리 로그 받기 켜기
//=> 시작할 때 한 번 부른다. 라이브러리를 아직 안 불러왔으면 불러올 때 콜백을 단다
//   (문서를 읽지 않는 명령 — --set-grade·--propagate 등 — 은 라이브러리 없이도 돈다).
//
// -in: 없음
//
// -out: 없음
// -out: error = 없음
//------------------------------------------------------------------
pub fn install_logger() {
    LOGGER_WANTED.store(true, std::sync::atomic::Ordering::Relaxed);
    if let Some(l) = LIB.get() {
        l.set_logger(Some(on_log), 3, std::ptr::null_mut());
    }
}

/// 예전 `docparse::limits` 자리 — 크기 상한 검사와 글자 수 자르기.
pub mod limits {
    use super::{lib, Fmt, Limits};
    use std::path::Path;

    /// 사이냅 폴백 '짧은 결과' 기준 글자 수 기본값(라이브러리 기본값과 같다).
    pub const DEFAULT_MIN_TEXT_LEN: usize = 20;

    //------------------------------------------------------------------
    // 크기 상한에 걸렸다는 표식
    // -필드: size      = 파일 크기(바이트)
    // -필드: limit     = 걸린 상한(바이트)
    // -필드: text_kind = 텍스트류 상한이었는지
    // -필드: why       = 라이브러리가 만든 사유 문장
    //------------------------------------------------------------------
    #[derive(Debug, Clone, PartialEq)]
    pub struct SizeLimitHit {
        pub size: u64,
        pub limit: u64,
        pub text_kind: bool,
        why: String,
    }

    impl SizeLimitHit {
        //------------------------------------------------------------------
        // 사람이 읽는 사유 ("크기 상한 초과: 328.8MB > 200.0MB (MAX_FILE_BYTES)")
        //
        // -in: 없음
        //
        // -out: String
        // -out: error = 없음
        //------------------------------------------------------------------
        pub fn reason(&self) -> String {
            self.why.clone()
        }
    }

    //------------------------------------------------------------------
    // 크기 상한 검사만 (추출하지 않음)
    //=> 추출 전에 분할 압축 조각 판정과 순서를 맞춰야 해서 따로 부른다.
    //
    // -in: path   = 파일 경로
    // -in: fmt    = 형식
    // -in: limits = 상한
    //
    // -out: Option<SizeLimitHit> = 걸렸으면 Some
    // -out: error = 라이브러리 인자 오류는 '안 걸림'(None)으로 본다
    //------------------------------------------------------------------
    pub fn check_size_limit(path: &Path, fmt: Fmt, limits: &Limits) -> Option<SizeLimitHit> {
        let opts = serde_json::json!({"limits": limits.to_json()});
        lib().check_size(path, Some(fmt.as_str()), &opts).ok().flatten()
            .map(|(size, limit, text_kind, why)| SizeLimitHit { size, limit, text_kind, why })
    }

    //------------------------------------------------------------------
    // 본문 글자 수 상한 자르기
    //=> 분류 쪽 정책(본문을 얼마나 볼지)이라 이 프로그램이 직접 한다. 라이브러리의 같은
    //   이름 함수와 같은 규칙 — '글자' 단위로 자른다(바이트로 자르면 한글 중간이 잘린다).
    //
    // -in: text  = 본문
    // -in: limit = 남길 최대 글자 수(0 이면 제한 없음)
    //
    // -out: (String, usize, bool) = 잘린(또는 그대로인) 본문, 자르기 전 글자 수, 잘렸는지
    // -out: error = 없음
    //------------------------------------------------------------------
    pub fn truncate_text(text: String, limit: usize) -> (String, usize, bool) {
        if limit == 0 {
            // 상한 해제 — 글자 수를 셀 이유도 없다(대용량에서 이 계산 자체가 비싸다).
            return (text, 0, false);
        }
        // limit 번째 글자의 바이트 위치. 없으면 = 상한 이하 = 그대로 통과.
        let cut = match text.char_indices().nth(limit) {
            None => return (text, 0, false),
            Some((byte_idx, _)) => byte_idx,
        };
        // 실제로 잘릴 때만 전체 글자 수를 센다(한 번만 훑는다).
        let n_orig = text.chars().count();
        let mut t = text;
        t.truncate(cut);
        (t, n_orig, true)
    }
}

/// 예전 `docparse::archive` 자리 — 압축 펼치기.
pub mod archive {
    use super::lib;
    use std::path::{Path, PathBuf};

    /// 압축 안의 압축을 몇 겹까지 펼칠지 기본값(라이브러리 기본값과 같다).
    pub const DEFAULT_MAX_DEPTH: usize = 3;

    //------------------------------------------------------------------
    // 압축 펼치기 옵션
    // -필드: max_depth   = 중첩 깊이 상한
    // -필드: max_bytes   = 풀린 총 크기 상한(None = 없음)
    // -필드: max_members = 항목 수 상한(None = 없음)
    //------------------------------------------------------------------
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct ArchiveOptions {
        pub max_depth: usize,
        pub max_bytes: Option<u64>,
        pub max_members: Option<u64>,
    }

    //------------------------------------------------------------------
    // 추출할 작업 하나
    // -필드: src    = 실제로 읽을 파일(압축 안 문서면 임시 폴더의 파일)
    // -필드: label  = 결과에 보일 이름("a.zip/보고서.docx")
    // -필드: origin = 원래 압축 파일(압축 밖 문서면 None)
    //------------------------------------------------------------------
    #[derive(Debug, Clone)]
    pub struct Job {
        pub src: PathBuf,
        pub label: String,
        pub origin: Option<String>,
    }

    //------------------------------------------------------------------
    // 압축 펼치기 보고 (여러 번 펼친 결과를 쌓는다)
    // -필드: unexpanded  = 못 펼친 압축 (경로, 사유, 종류 "limit"|"split_volume"|"error")
    // -필드: unsupported = 지원하지 않는 압축 (경로, 사유)
    // -필드: partial     = 일부만 펼친 압축 (경로, 사유)
    //------------------------------------------------------------------
    #[derive(Default, Debug)]
    pub struct ExpandStats {
        pub unexpanded: Vec<(String, String, &'static str)>,
        pub unsupported: Vec<(String, String)>,
        pub partial: Vec<(String, String)>,
    }

    //------------------------------------------------------------------
    // 못 펼친 종류를 고정 문자열로
    //
    // -in: k = 라이브러리가 준 종류 이름
    //
    // -out: &'static str = "limit" | "split_volume" | "error"(그 밖은 모두 error)
    // -out: error = 없음
    //------------------------------------------------------------------
    fn static_kind(k: &str) -> &'static str {
        match k {
            "limit" => "limit",
            "split_volume" => "split_volume",
            _ => "error",
        }
    }

    //------------------------------------------------------------------
    // 경로 목록의 압축을 펼쳐 작업 목록 만들기
    //=> 라이브러리가 tmp_root 아래에 풀고 작업 목록과 보고를 준다. 보고는 stats 에 쌓는다.
    //
    // -in: paths    = 대상 경로들
    // -in: tmp_root = 풀 폴더(만들고 지우는 일은 부른 쪽이 한다)
    // -in: opts     = 펼치기 옵션
    // -in: stats    = 보고를 쌓을 곳
    //
    // -out: Vec<Job> = 추출할 작업들
    // -out: error = 라이브러리 내부 오류면 모든 경로를 '못 펼침(error)'으로 보고하고 압축이
    //               아닌 것처럼 원래 경로를 작업으로 돌려준다
    //------------------------------------------------------------------
    pub fn expand_paths(paths: &[PathBuf], tmp_root: &Path, opts: &ArchiveOptions, stats: &mut ExpandStats) -> Vec<Job> {
        let o = serde_json::json!({
            "max_depth": opts.max_depth,
            "max_bytes": opts.max_bytes.unwrap_or(0),
            "max_members": opts.max_members.unwrap_or(0),
        });
        match lib().expand(paths, tmp_root, &o) {
            Ok(x) => {
                stats.unexpanded.extend(x.unexpanded.into_iter().map(|(p, r, k)| (p, r, static_kind(&k))));
                stats.unsupported.extend(x.unsupported);
                stats.partial.extend(x.partial);
                x.jobs.into_iter().map(|(src, label, origin)| Job { src, label, origin }).collect()
            }
            Err(why) => {
                // 거의 없는 경우(라이브러리 버그) — 문서를 잃지 않게 원래 경로를 그대로 넘긴다.
                crate::errlog::err(&format!("[압축] 문서파서 오류로 펼치지 못함: {}", why));
                paths.iter().map(|p| {
                    let label = p.to_string_lossy().replace('\\', "/");
                    Job { src: p.clone(), label, origin: None }
                }).collect()
            }
        }
    }

    //------------------------------------------------------------------
    // 분할 압축 조각 안내
    //
    // -in: name = 표시용 이름
    // -in: path = 실제 경로(내용까지 볼 때)
    //
    // -out: Option<String> = 조각이면 안내 문구
    // -out: error = 없음
    //------------------------------------------------------------------
    pub fn split_volume_hint(name: &str, path: Option<&Path>) -> Option<String> {
        lib().split_volume_hint(name, path)
    }
}

/// 예전 `docparse::synap` 자리 — 사이냅 시간 상한 기본값(라이브러리 기본값과 같다).
pub mod synap {
    pub const SNF_TIMEOUT_SECS: u64 = 60;
}
