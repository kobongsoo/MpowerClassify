//! 이 실행의 추출 설정 — docparse 라이브러리를 이 프로그램에 잇는 자리
//!
//! 설계: plan/Rust-문서파서-라이브러리분리-설계-20261110.html §4·§6
//!
//! docparse 는 전역 상태 없이 옵션을 매번 인자로 받는다. 이 프로그램은 엔진(자체
//! 파서·사이냅)을 시작할 때 한 번 정하고, 상한은 CLI > 환경변수 > 기본값으로 정한다.
//! 그 둘을 여기서 [`crate::docparse::ExtractOptions`] 로 묶어 준다. 또 문서파서
//! 라이브러리가 콜백으로 넘기는 기록을 이 프로그램의 로그 파일(class_*.log ·
//! class_err_*.log)로 옮겨 적게 단다 — 로그 문장은 분리 전과 같다.

use std::path::Path;
use std::sync::OnceLock;

use crate::docparse::{Engine, ExtractError, ExtractOptions, Fmt};

// 이번 실행의 추출 엔진. 시작할 때 handle_extractor_args 가 한 번 정한다.
// 정하지 않았으면 Builtin(자체 파서만)이다.
static ENGINE: OnceLock<Engine> = OnceLock::new();

//------------------------------------------------------------------
// 이번 실행의 추출 엔진 정하기(한 번만)
//=> 같은 배치 안에서 앞뒤 문서의 엔진이 달라지지 않게 처음 한 번만 받는다.
//
// -in: e = 추출 엔진
//
// -out: 없음
// -out: error = 없음(두 번째 부름은 무시된다)
//------------------------------------------------------------------
pub fn set_engine(e: Engine) {
    let _ = ENGINE.set(e);
}

//------------------------------------------------------------------
// 이번 실행의 추출 엔진 읽기
//
// -in: 없음
//
// -out: &Engine = 정한 엔진(안 정했으면 Builtin)
// -out: error = 없음
//------------------------------------------------------------------
pub fn engine() -> &'static Engine {
    static DEFAULT: Engine = Engine::Builtin;
    ENGINE.get().unwrap_or(&DEFAULT)
}

//------------------------------------------------------------------
// 문서 하나를 뽑을 docparse 옵션 만들기
//=> 지금 엔진 · 지금 상한 · pdfium 위치를 한데 묶는다.
//    1) 엔진은 시작할 때 정한 것
//    2) 상한은 limits 모듈 규칙(CLI > 환경변수 > 기본값)으로 정한 값
//    3) pdfium 은 CSO_PDFIUM 환경변수가 가리키는 파일(없는 파일이면 라이브러리가
//       실행파일 옆 → 시스템 순으로 찾는다 — 분리 전과 같은 차례)
//    4) 로그에 적을 이름은 label(풀린 문서면 임시 경로 대신 원래 경로)
//
// -in: label = 로그에 적을 문서 이름
//
// -out: ExtractOptions = 이 문서용 옵션(정제까지 함)
// -out: error = 없음
//------------------------------------------------------------------
pub fn options(label: &str) -> ExtractOptions {
    ExtractOptions {
        engine: engine().clone(),
        limits: crate::limits::extract_limits(),
        pdfium_lib: std::env::var_os("CSO_PDFIUM").map(std::path::PathBuf::from),
        clean: true,
        label: Some(label.to_string()),
    }
}

//------------------------------------------------------------------
// 추출 실패 → 이 프로그램이 쓰던 사유 문자열
//=> 분리 전 extract_text_with 의 Err(String) 과 같은 값을 만든다. 자체 파서 실패와
//   미지원 포맷은 빈 문자열(부른 쪽이 예전 문장 "추출 실패: … (감지=…)" 을 쓴다),
//   사이냅 모드 실패는 엔진별 사유 그대로다.
//
// -in: e = docparse 추출 오류
//
// -out: String = 사유(없으면 빈 문자열)
// -out: error = 없음
//------------------------------------------------------------------
pub fn fail_reason(e: &ExtractError) -> String {
    match e {
        ExtractError::Unsupported(_) => String::new(),
        ExtractError::Failed(why) => why.clone(),
        // 부른 쪽이 크기 상한을 먼저 보므로 보통은 오지 않는다. 오면 그 사유를 적는다.
        ExtractError::SizeLimit { .. } => e.to_string(),
    }
}

//------------------------------------------------------------------
// 본문만 뽑기 — 부가정보가 필요 없는 자리용
//=> 지금 엔진·상한으로 뽑아 본문만 돌려준다.
//
// -in: path  = 읽을 파일
// -in: fmt   = 감지한 포맷
// -in: label = 로그에 적을 문서 이름
//
// -out: Ok(String) = 정제한 본문
// -out: error = Err(사유) — 분리 전 extract_text_with 와 같은 값
//------------------------------------------------------------------
pub fn extract_text(path: &Path, fmt: Fmt, label: &str) -> Result<String, String> {
    crate::docparse::extract_as(path, fmt, &options(label)).map(|x| x.text).map_err(|e| fail_reason(&e))
}

//------------------------------------------------------------------
// 문서파서 로그 받기 달기 (시작 시 한 번)
//=> 문서파서 라이브러리(docparse.dll/.so)가 남기는 기록을 콜백으로 받아 이 프로그램의
//   로그 파일로 옮긴다(규칙은 docparse::on_log — 분리 전 log 브리지와 같다).
//   이때 라이브러리를 처음 불러오므로, 없으면 여기서 1017 extractor_missing 으로 끝난다.
//
// -in: 없음
//
// -out: 없음
// -out: error = 라이브러리를 못 불러오면 1017 로 끝남
//------------------------------------------------------------------
pub fn install_log_bridge() {
    crate::docparse::install_logger();
}
