//! moaipii_dyn — moaipii.dll / libmoaipii.so 를 실행 중에 불러 쓰는 Rust 감싸개
//!
//! 정본: MoAI-pii/bindings/rust/moaipii_dyn.rs — 쓰는 프로젝트는 이 파일을 복사해 둔다(직접 고치지 않는다).
//! 필요한 의존: libloading = "0.8", serde_json = "1"
//!
//! ```no_run
//! let pii = MoaiPii::find_and_load(None, "MOAIPII_LIB").unwrap();
//! let want: std::collections::HashSet<String> = ["RRN".to_string(), "PHONE".to_string()].into();
//! let r = pii.scan("주민 900101-1234567", &want, &ScanOpts::default()).unwrap();
//! println!("{:?}", r.counts);
//! ```

// 쓰는 프로그램마다 필요한 함수가 달라, 안 쓰는 함수·필드가 있어도 경고하지 않는다.
#![allow(dead_code)]

use std::collections::{BTreeMap, HashSet};
use std::ffi::{c_char, c_int, CStr, CString};
use std::path::{Path, PathBuf};

use serde_json::{json, Value};

/// 이 감싸개가 아는 함수 모양의 버전. 라이브러리의 moaipii_abi_version() 과 같아야 한다.
pub const ABI_VERSION: u32 = 1;

type FnAbi = unsafe extern "C" fn() -> u32;
type FnStr = unsafe extern "C" fn() -> *const c_char;
type FnLabels = unsafe extern "C" fn(*mut *mut c_char) -> c_int;
type FnScan = unsafe extern "C" fn(*const u8, usize, *const c_char, *mut *mut c_char) -> c_int;
type FnFree = unsafe extern "C" fn(*mut c_char);

//------------------------------------------------------------------
// 검사 옵션 (moaipii.h 의 옵션 JSON 과 같은 뜻)
// -필드: items          = false 면 건수만(위치 목록 생략)
// -필드: values         = true 면 위치마다 원문 값도 싣는다
// -필드: offsets        = "char" | "utf16" | "byte"
// -필드: unknown_ignore = true 면 모르는 라벨을 오류 대신 무시(결과 unknown 에 알려 줌)
// -필드: today          = 미래 생년월일 판단 기준일 "YYYY-MM-DD"(None = 시스템 날짜)
//------------------------------------------------------------------
#[derive(Debug, Clone)]
pub struct ScanOpts {
    pub items: bool,
    pub values: bool,
    pub offsets: &'static str,
    pub unknown_ignore: bool,
    pub today: Option<String>,
}

impl Default for ScanOpts {
    fn default() -> Self {
        ScanOpts { items: true, values: false, offsets: "char", unknown_ignore: false, today: None }
    }
}

//------------------------------------------------------------------
// 검출 한 건
// -필드: label = 라벨
// -필드: start = 시작 좌표(옵션 offsets 단위)
// -필드: end   = 끝 좌표(포함하지 않음)
// -필드: value = 원문 값(values=true 일 때만)
//------------------------------------------------------------------
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Item {
    pub label: String,
    pub start: usize,
    pub end: usize,
    pub value: Option<String>,
}

//------------------------------------------------------------------
// 검사 결과
// -필드: chars   = 본문 코드포인트 수
// -필드: counts  = 라벨별 건수(0 건은 없음)
// -필드: items   = 겹침 해소가 끝난 위치 목록(문서 순서)
// -필드: unknown = 무시한 모르는 라벨(unknown_ignore 일 때)
//------------------------------------------------------------------
#[derive(Debug, Clone, Default)]
pub struct ScanResult {
    pub chars: usize,
    pub counts: BTreeMap<String, u32>,
    pub items: Vec<Item>,
    pub unknown: Vec<String>,
}

//------------------------------------------------------------------
// 실패 — 라이브러리 결과 JSON 의 error 그대로
// -필드: kind    = "arg" | "utf8" | "panic"
// -필드: message = 사람이 읽는 사유
//------------------------------------------------------------------
#[derive(Debug, Clone)]
pub struct PiiError {
    pub kind: String,
    pub message: String,
}

impl std::fmt::Display for PiiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "moaipii {}: {}", self.kind, self.message)
    }
}

//------------------------------------------------------------------
// 불러온 라이브러리 한 벌
// -필드: path        = 불러온 파일
// -필드: version     = moaipii_version()
// -필드: kopii_basis = moaipii_kopii_basis()
//------------------------------------------------------------------
pub struct MoaiPii {
    _lib: libloading::Library,
    path: PathBuf,
    version: String,
    kopii_basis: String,
    labels: FnLabels,
    scan: FnScan,
    free: FnFree,
}

// 함수 포인터만 들고 있고 라이브러리 쪽에 전역 상태가 없으므로 스레드 간에 나눠 써도 된다.
unsafe impl Send for MoaiPii {}
unsafe impl Sync for MoaiPii {}

//------------------------------------------------------------------
// 이 OS 의 라이브러리 파일 이름
//
// -in: 없음
//
// -out: &str = "moaipii.dll" | "libmoaipii.so" | "libmoaipii.dylib"
// -out: error = 없음
//------------------------------------------------------------------
pub fn lib_file_name() -> &'static str {
    if cfg!(windows) { "moaipii.dll" } else if cfg!(target_os = "macos") { "libmoaipii.dylib" } else { "libmoaipii.so" }
}

impl MoaiPii {
    //------------------------------------------------------------------
    // 라이브러리 파일 하나를 불러 함수들을 찾는다
    //=> 이름 검색(PATH·LD_LIBRARY_PATH)을 쓰지 않고 준 경로 그대로 연다 — 엉뚱한 버전을
    //   집어 오지 않게.
    //    1) 라이브러리 열기
    //    2) moaipii_abi_version() == ABI_VERSION 확인(다르면 멈춘다)
    //    3) 나머지 함수 포인터 찾기
    //
    // -in: path = 라이브러리 파일 경로
    //
    // -out: MoaiPii = 불러온 라이브러리
    // -out: error = 못 열거나 함수가 없거나 ABI 가 다르면 Err(사람용 사유)
    //------------------------------------------------------------------
    pub fn load(path: &Path) -> Result<Self, String> {
        // 안전: moaipii 라이브러리는 불러올 때 초기화 코드를 돌리지 않는다.
        let lib = unsafe { libloading::Library::new(path) }
            .map_err(|e| format!("개인정보 검출 라이브러리를 열지 못했습니다: {} ({})", path.display(), e))?;
        // 함수 하나를 이름으로 찾아 포인터로 꺼내는 작은 도우미
        macro_rules! sym {
            ($name:literal, $t:ty) => {{
                let s: libloading::Symbol<$t> = unsafe { lib.get(concat!($name, "\0").as_bytes()) }
                    .map_err(|e| format!("개인정보 검출 라이브러리에 {} 함수가 없습니다: {} ({})", $name, path.display(), e))?;
                *s
            }};
        }
        let abi = sym!("moaipii_abi_version", FnAbi);
        let got = unsafe { abi() };
        if got != ABI_VERSION {
            return Err(format!("개인정보 검출 라이브러리의 ABI 버전이 다릅니다: 라이브러리 {} / 감싸개 {} ({})",
                               got, ABI_VERSION, path.display()));
        }
        let ver = sym!("moaipii_version", FnStr);
        let basis = sym!("moaipii_kopii_basis", FnStr);
        Ok(MoaiPii {
            path: path.to_path_buf(),
            version: unsafe { CStr::from_ptr(ver()) }.to_string_lossy().into_owned(),
            kopii_basis: unsafe { CStr::from_ptr(basis()) }.to_string_lossy().into_owned(),
            labels: sym!("moaipii_labels", FnLabels),
            scan: sym!("moaipii_scan", FnScan),
            free: sym!("moaipii_free", FnFree),
            _lib: lib,
        })
    }

    //------------------------------------------------------------------
    // 후보 폴더들에서 라이브러리를 찾아 불러온다
    //=> 찾는 순서: explicit(파일 또는 폴더) → 환경변수 env_var → 실행파일 폴더
    //   → 실행파일 폴더의 부모(cargo test 실행파일은 target/<프로파일>/deps 에 있다).
    //
    // -in: explicit = 직접 지정한 파일·폴더(없으면 None)
    // -in: env_var  = 경로를 읽을 환경변수 이름(파일·폴더 모두 허용)
    //
    // -out: MoaiPii = 불러온 라이브러리
    // -out: error = 못 찾거나 못 열면 Err(찾아본 곳을 모두 적은 사유)
    //------------------------------------------------------------------
    pub fn find_and_load(explicit: Option<&Path>, env_var: &str) -> Result<Self, String> {
        let name = lib_file_name();
        let mut cands: Vec<PathBuf> = Vec::new();
        // 파일이면 그대로, 폴더면 그 안의 라이브러리 이름을 후보로
        let push = |c: &mut Vec<PathBuf>, p: PathBuf| {
            if p.is_dir() { c.push(p.join(name)) } else { c.push(p) }
        };
        if let Some(p) = explicit {
            push(&mut cands, p.to_path_buf());
        }
        if let Some(v) = std::env::var_os(env_var).filter(|v| !v.is_empty()) {
            push(&mut cands, PathBuf::from(v));
        }
        if let Some(d) = std::env::current_exe().ok().and_then(|e| e.parent().map(Path::to_path_buf)) {
            cands.push(d.join(name));
            if let Some(pd) = d.parent() {
                cands.push(pd.join(name));
            }
        }
        // 직접 지정한 것은 '없으면 실패'다 — 다른 곳의 것으로 조용히 대신하지 않는다.
        if explicit.is_some() {
            return Self::load(&cands[0]);
        }
        for c in &cands {
            if c.is_file() {
                return Self::load(c);
            }
        }
        Err(format!("개인정보 검출 라이브러리({})를 찾을 수 없습니다 — 찾아본 곳: {} (환경변수 {} 로 알려 주세요)",
                    name, cands.iter().map(|c| c.display().to_string()).collect::<Vec<_>>().join(" , "), env_var))
    }

    //------------------------------------------------------------------
    // 불러온 라이브러리 파일 경로
    //
    // -in: 없음
    //
    // -out: &Path
    // -out: error = 없음
    //------------------------------------------------------------------
    pub fn path(&self) -> &Path {
        &self.path
    }

    //------------------------------------------------------------------
    // 라이브러리 버전 ("0.1.0+abc1234")
    //
    // -in: 없음
    //
    // -out: &str
    // -out: error = 없음
    //------------------------------------------------------------------
    pub fn version(&self) -> &str {
        &self.version
    }

    //------------------------------------------------------------------
    // 검출 기준이 된 ko-pii 판 ("1.15.2")
    //
    // -in: 없음
    //
    // -out: &str
    // -out: error = 없음
    //------------------------------------------------------------------
    pub fn kopii_basis(&self) -> &str {
        &self.kopii_basis
    }

    //------------------------------------------------------------------
    // JSON 결과 함수를 불러 (코드, JSON) 으로 받고 곧바로 해제
    //
    // -in: f = out 자리를 받아 반환 코드를 주는 호출
    //
    // -out: (c_int, Value) = 반환 코드와 결과 JSON
    // -out: error = 없음(결과가 비거나 JSON 이 아니면 panic 종류의 error JSON 을 만든다)
    //------------------------------------------------------------------
    fn call_json(&self, f: impl FnOnce(*mut *mut c_char) -> c_int) -> (c_int, Value) {
        let mut out: *mut c_char = std::ptr::null_mut();
        let code = f(&mut out);
        if out.is_null() {
            return (code, json!({"error": {"kind": "panic", "message": "개인정보 검출 라이브러리가 결과를 주지 않았습니다"}}));
        }
        let s = unsafe { CStr::from_ptr(out) }.to_string_lossy().into_owned();
        // 결과에 개인정보 값이 있을 수 있어 라이브러리가 0 으로 덮고 지운다.
        unsafe { (self.free)(out) };
        let v = serde_json::from_str(&s)
            .unwrap_or_else(|e| json!({"error": {"kind": "panic", "message": format!("결과 JSON 오류: {}", e)}}));
        (code, v)
    }

    //------------------------------------------------------------------
    // 지원 라벨 목록
    //
    // -in: 없음
    //
    // -out: Vec<String> = 검출 라벨들(별칭 제외)
    // -out: error = 라이브러리 실패 시 Err
    //------------------------------------------------------------------
    pub fn labels(&self) -> Result<Vec<String>, PiiError> {
        let (code, v) = self.call_json(|o| unsafe { (self.labels)(o) });
        if code != 0 { return Err(err_of(&v)); }
        Ok(v["labels"].as_array().map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect()).unwrap_or_default())
    }

    //------------------------------------------------------------------
    // 결과 JSON 그대로 받기 (자기 JSON 처리로 읽고 싶은 쪽)
    //
    // -in: text      = 본문 바이트
    // -in: opts_json = 옵션 JSON 문자열
    //
    // -out: (반환 코드, 결과 JSON)
    // -out: error = 없음(실패도 결과 JSON 의 error 로 온다)
    //------------------------------------------------------------------
    pub fn scan_json(&self, text: &[u8], opts_json: &str) -> (i32, Value) {
        let o = CString::new(opts_json.replace('\0', "")).expect("NUL 을 지웠다");
        self.call_json(|out| unsafe { (self.scan)(text.as_ptr(), text.len(), o.as_ptr(), out) })
    }

    //------------------------------------------------------------------
    // 본문 검사
    //=> 옵션을 JSON 으로 만들어 moaipii_scan 을 부르고 결과를 구조체로 바꾼다.
    //
    // -in: text   = 본문
    // -in: labels = 켤 라벨
    // -in: opts   = 옵션(ScanOpts::default() = 위치 있음·값 없음·문자 좌표)
    //
    // -out: ScanResult
    // -out: error = 인자·옵션 오류나 내부 오류면 Err(PiiError)
    //------------------------------------------------------------------
    pub fn scan(&self, text: &str, labels: &HashSet<String>, opts: &ScanOpts) -> Result<ScanResult, PiiError> {
        // 라벨 차례를 고정해 같은 입력이면 같은 옵션 JSON 이 되게 한다.
        let mut ls: Vec<&String> = labels.iter().collect();
        ls.sort();
        let o = json!({
            "labels": ls, "items": opts.items, "values": opts.values, "offsets": opts.offsets,
            "unknown_labels": if opts.unknown_ignore { "ignore" } else { "error" },
            "today": opts.today,
        });
        let (code, v) = self.scan_json(text.as_bytes(), &o.to_string());
        if code != 0 { return Err(err_of(&v)); }
        let mut r = ScanResult { chars: v["chars"].as_u64().unwrap_or(0) as usize, ..Default::default() };
        if let Some(m) = v["counts"].as_object() {
            for (k, n) in m { r.counts.insert(k.clone(), n.as_u64().unwrap_or(0) as u32); }
        }
        if let Some(a) = v["items"].as_array() {
            for it in a {
                r.items.push(Item {
                    label: it["label"].as_str().unwrap_or("").to_string(),
                    start: it["start"].as_u64().unwrap_or(0) as usize,
                    end: it["end"].as_u64().unwrap_or(0) as usize,
                    value: it.get("value").and_then(Value::as_str).map(String::from),
                });
            }
        }
        if let Some(a) = v["unknown"].as_array() {
            r.unknown = a.iter().filter_map(|x| x.as_str().map(String::from)).collect();
        }
        Ok(r)
    }
}

//------------------------------------------------------------------
// 결과 JSON 의 error → PiiError
//
// -in: v = 결과 JSON
//
// -out: PiiError
// -out: error = 없음(모양이 틀리면 kind "panic")
//------------------------------------------------------------------
fn err_of(v: &Value) -> PiiError {
    PiiError {
        kind: v["error"]["kind"].as_str().unwrap_or("panic").to_string(),
        message: v["error"]["message"].as_str().unwrap_or("알 수 없는 오류").to_string(),
    }
}
