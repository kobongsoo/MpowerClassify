//! 문서 크기 상한(Size Gate) — 설계: plan/문서크기-상한-설계-20260904.html
//!
//! 파이썬 판 `config.py` 의 크기 상한 상수·환경변수와 **1:1로 대응**한다. 두 구현이
//! 같은 문서에서 같은 판정을 내야 하므로(설계 원칙 4), 기본값과 환경변수 이름을
//! 여기서 그대로 맞춘다. 값을 한쪽만 고치면 같은 배치를 두 구현으로 돌렸을 때
//! 결과가 갈린다.
//!
//! 적용 게이트:
//!   G1 `MAX_FILE_BYTES`      — 추출 전 원본 바이트(안전핀)
//!   G2 `MAX_FILE_BYTES_TEXT` — 텍스트 계열 전용(바이트=글자수라 더 낮게)
//!   G3 `MAX_TEXT_CHARS`      — 추출 후 글자수(본체). 자르고 표식을 단다
//!   G5 `PARSER_TIMEOUT` / `MAX_PDF_PAGES` — 파서 루프의 시간·분량 상한
//!
//!   G4 `MAX_ARCHIVE_BYTES` / `MAX_ARCHIVE_MEMBERS` — 압축 해제 누적 상한
//!
//! (2026-09-04 압축 확장을 이 판에도 포팅하면서 G4 가 실제로 동작하게 됐다.)
//!
//! [2026-11-10 docparse 분리] 판정 함수(크기 검사·시간 예산·글자수 자르기)와 부분추출
//! 표식은 docparse 라이브러리로 옮겼다. 여기 남은 것은 '상한 값을 정하는 규칙'
//! (CLI > 환경변수 > 기본값)뿐이고, `extract_limits()` · `archive_options()` 로
//! 라이브러리에 넘긴다. 설계: plan/Rust-문서파서-라이브러리분리-설계-20261110.html §4

use std::path::Path;
use std::time::Duration;

use crate::docparse::Fmt;

// 판정 함수는 docparse 라이브러리로 옮겼다 — 이 프로그램 안의 호출부는 예전 이름 그대로 쓴다.
pub use crate::docparse::limits::truncate_text;

//------------------------------------------------------------------
// 환경변수 → 정수
//=> 값이 없거나 숫자가 아니면 기본값을 쓴다. 파이썬 판이
//   `int(os.environ.get(...), 기본값)` 로 읽는 것과 같은 동작이다.
//
// -in: name    = 환경변수 이름
// -in: default = 미설정/해석불가일 때 쓸 값
//
// -out: u64 = 해석된 값
// -out: error = 없음(항상 값을 돌려준다)
//------------------------------------------------------------------
fn env_u64(name: &str, default: u64) -> u64 {
    std::env::var(name).ok().and_then(|v| v.trim().parse().ok()).unwrap_or(default)
}

//------------------------------------------------------------------
// CLI 로 덮어쓴 상한값 모음
//=> 우선순위를 파이썬 판과 같게 'CLI 플래그 > 환경변수 > 코드 기본값' 으로 맞추기
//   위한 자리다. 아래 getter 들이 이 값을 먼저 보고, 없을 때만 환경변수를 읽는다.
//
//   [왜 전역인가] 상한을 실제로 보는 곳은 추출기·PDF 파서처럼 CLI 인자를 볼 수
//   없는 깊은 자리다. 거기까지 값을 실어 나르면 함수 시그니처 대여섯 개가 상한
//   하나 때문에 바뀐다. 파이썬 판이 config 모듈 값을 덮어쓰는 것과 같은 방식이다.
//   [왜 OnceLock 인가] 시작할 때 한 번만 정해지고 그 뒤로는 읽기만 하는 값이다.
//   Mutex 는 매 페이지마다 잠금을 걸어야 하고, 가변 static 은 unsafe 다.
//
// -필드: 각 상한의 덮어쓴 값. None 이면 '지정 안 함'(환경변수/기본값을 쓴다)
//------------------------------------------------------------------
#[derive(Default, Clone, Copy)]
pub struct Overrides {
    pub max_file_bytes: Option<u64>,
    pub max_file_bytes_text: Option<u64>,
    pub max_text_chars: Option<u64>,
    pub parser_timeout: Option<u64>,
    pub max_pdf_pages: Option<u64>,
    pub max_archive_bytes: Option<u64>,
    pub max_archive_members: Option<u64>,
}

static OVERRIDES: std::sync::OnceLock<Overrides> = std::sync::OnceLock::new();

//------------------------------------------------------------------
// CLI 덮어쓰기 등록 (시작 시 한 번)
//=> parse_args 직후에 부른다. 두 번째 호출은 조용히 무시된다 — 상한이 실행 도중
//   바뀌면 같은 배치 안에서 앞뒤 문서의 판정 기준이 달라지기 때문이다.
//
// -in: o = 덮어쓸 값 모음(지정 안 한 항목은 None)
//
// -out: 없음
// -out: error = 없음(이미 정해져 있으면 무시)
//------------------------------------------------------------------
pub fn set_overrides(o: Overrides) {
    let _ = OVERRIDES.set(o);
}

//------------------------------------------------------------------
// 덮어쓴 값 꺼내기(내부 헬퍼)
//=> 등록된 것이 없거나 그 항목이 None 이면 None 을 준다.
//
// -in: pick = Overrides 에서 볼 항목을 고르는 클로저
//
// -out: Option<u64> = 덮어쓴 값
// -out: error = 없음
//------------------------------------------------------------------
fn overridden(pick: impl Fn(&Overrides) -> Option<u64>) -> Option<u64> {
    OVERRIDES.get().and_then(pick)
}

// 기본값 상수 — 파이썬 판 config.py 와 같은 값이어야 한다(설계 원칙 4).
// getter 와 테스트가 '같은 하나'를 보게 상수로 뽑아 둔다. 숫자를 양쪽에 적어 두면
// 한쪽만 고쳐도 테스트가 통과해 버려, 정합을 지키라는 테스트가 무력해진다.
pub const DEFAULT_MAX_FILE_BYTES: u64 = 100 * 1024 * 1024;
pub const DEFAULT_MAX_FILE_BYTES_TEXT: u64 = 100 * 1024 * 1024;
pub const DEFAULT_MAX_TEXT_CHARS: u64 = 2_000_000;
pub const DEFAULT_PARSER_TIMEOUT: u64 = 60;
pub const DEFAULT_MAX_PDF_PAGES: u64 = 3000;
pub const DEFAULT_MAX_ARCHIVE_BYTES: u64 = 500 * 1024 * 1024;
pub const DEFAULT_MAX_ARCHIVE_MEMBERS: u64 = 5000;

/// G1 — 추출 전 원본 파일 바이트 상한(기본 100MB).
/// [값의 근거] D:\분류함 2,254파일 실측에서 100MB 초과는 2건(둘 다 학습 코퍼스 tsv)뿐.
/// 50MB 로 낮추면 정상 업무문서인 55.3MB 회사소개서가 걸리므로 낮추면 안 된다.
pub fn max_file_bytes() -> u64 {
    overridden(|o| o.max_file_bytes)
        .unwrap_or_else(|| env_u64("CSOCLASSIFY_MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES))
}

/// G2 — 텍스트 계열(txt/csv/tsv/json/html) 전용 상한(기본 100MB = G1 과 같은 값).
/// [왜 20MB 에서 올렸나] 이 게이트는 문서를 **버린다**. 그런데 바로 뒤의 G3 는 같은
/// 상황에서 버리지 않고 앞부분만 읽고 '일부만 봤다' 표식을 단다. 텍스트만 20MB 에서
/// 통째로 버리면 설계 원칙("진짜 상한은 G3, 바이트는 느슨한 안전핀")과 어긋난다.
/// 실측(D:\분류함 305건)에서 46~82MB 짜리 tsv·json 4건이 본문 한 글자도 못 남기고
/// 빠졌는데, 사이냅은 같은 파일을 읽어냈다 — 상한이 파서 실력처럼 보이던 자리다.
/// [비용이 늘지 않는 이유] PII 정규식·분류가 실제로 훑는 길이는 G3(200만 자)가
/// 잡는다. 올려서 늘어나는 일은 디스크에서 더 읽고 디코드하는 몫뿐이다.
pub fn max_file_bytes_text() -> u64 {
    overridden(|o| o.max_file_bytes_text)
        .unwrap_or_else(|| env_u64("CSOCLASSIFY_MAX_FILE_BYTES_TEXT", DEFAULT_MAX_FILE_BYTES_TEXT))
}

/// G3 — 추출 후 글자수 상한(기본 200만 자). 다섯 게이트 중 본체.
/// 실측 854자/페이지 기준 약 2,340페이지로, 최악 케이스(581p)의 4배라 정상 문서를
/// 자를 위험이 없다.
pub fn max_text_chars() -> usize {
    overridden(|o| o.max_text_chars)
        .unwrap_or_else(|| env_u64("CSOCLASSIFY_MAX_TEXT_CHARS", DEFAULT_MAX_TEXT_CHARS)) as usize
}

/// G4 — 압축 1건당 해제 누적 바이트 상한(기본 500MB). 0 이면 무제한.
/// [왜 압축파일 크기가 아닌가] 7KB zip 이 1.4MB 로, 악성 zip 은 수만 배로 부푼다.
/// 압축된 크기로는 위험을 알 수 없어, '풀었을 때 누적'을 센다.
pub fn max_archive_bytes() -> u64 {
    overridden(|o| o.max_archive_bytes)
        .unwrap_or_else(|| env_u64("CSOCLASSIFY_MAX_ARCHIVE_BYTES", DEFAULT_MAX_ARCHIVE_BYTES))
}

/// G4 — 압축 1건당 내부 파일 개수 상한(기본 5000). 0 이면 무제한.
/// 0바이트 파일 수백만 개짜리 폭탄은 용량으로는 안 걸려 개수를 따로 센다.
pub fn max_archive_members() -> u64 {
    overridden(|o| o.max_archive_members)
        .unwrap_or_else(|| env_u64("CSOCLASSIFY_MAX_ARCHIVE_MEMBERS", DEFAULT_MAX_ARCHIVE_MEMBERS))
}

/// G5 — 파서 루프 시간 상한(초, 기본 60). 0 이면 무제한.
pub fn parser_timeout_secs() -> u64 {
    overridden(|o| o.parser_timeout)
        .unwrap_or_else(|| env_u64("CSOCLASSIFY_PARSER_TIMEOUT", DEFAULT_PARSER_TIMEOUT))
}

/// G5 — PDF 페이지 수 상한(기본 3000). 0 이면 무제한.
pub fn max_pdf_pages() -> usize {
    overridden(|o| o.max_pdf_pages)
        .unwrap_or_else(|| env_u64("CSOCLASSIFY_MAX_PDF_PAGES", DEFAULT_MAX_PDF_PAGES)) as usize
}

//------------------------------------------------------------------
// 지금 상한값 → docparse 추출 상한
//=> 이 프로그램은 상한을 CLI 인자 > 환경변수 > 기본값 차례로 정한다(위 getter 들).
//   docparse 라이브러리는 그 규칙을 모르고 값만 받으므로, 여기서 한 번에 옮겨 담는다.
//   0(무제한)은 라이브러리의 None 으로 바꾼다.
//    1) 크기·시간·페이지 상한 — getter 값을 그대로(0 → None)
//    2) 사이냅 제한 시간 — CSOCLASSIFY_SNF_TIMEOUT(기본 60초)
//    3) 짧은 결과 기준 — CSOCLASSIFY_MIN_TEXT_LEN(기본 20). 분리 전 추출기가 직접
//       읽던 환경변수를 같은 규칙(앞뒤 공백 그대로 parse)으로 여기서 읽는다
//
// -in: 없음
//
// -out: docparse::Limits = 이번 실행의 추출 상한
// -out: error = 없음(환경변수가 숫자가 아니면 기본값)
//------------------------------------------------------------------
pub fn extract_limits() -> crate::docparse::Limits {
    // 0 은 '상한 없음' — 라이브러리 표기(None)로 바꾼다.
    let opt = |n: u64| if n == 0 { None } else { Some(n) };
    let secs = parser_timeout_secs();
    crate::docparse::Limits {
        parser_timeout: opt(secs).map(Duration::from_secs),
        max_pdf_pages: opt(max_pdf_pages() as u64).map(|n| n as usize),
        max_file_bytes: opt(max_file_bytes()),
        max_file_bytes_text: opt(max_file_bytes_text()),
        snf_timeout: Duration::from_secs(crate::synap::snf_timeout_secs()),
        min_text_len: std::env::var("CSOCLASSIFY_MIN_TEXT_LEN")
            .ok().and_then(|v| v.parse().ok()).unwrap_or(crate::docparse::limits::DEFAULT_MIN_TEXT_LEN),
    }
}

//------------------------------------------------------------------
// 지금 상한값 → docparse 압축 펼치기 옵션
//=> 압축 상한(G4)도 같은 규칙(CLI > 환경변수 > 기본값)으로 정해 라이브러리에 넘긴다.
//
// -in: 없음
//
// -out: docparse::archive::ArchiveOptions = 깊이 3 · 지금 압축 상한
// -out: error = 없음
//------------------------------------------------------------------
pub fn archive_options() -> crate::docparse::archive::ArchiveOptions {
    let opt = |n: u64| if n == 0 { None } else { Some(n) };
    crate::docparse::archive::ArchiveOptions {
        max_depth: crate::docparse::archive::DEFAULT_MAX_DEPTH,
        max_bytes: opt(max_archive_bytes()),
        max_members: opt(max_archive_members()),
    }
}

//------------------------------------------------------------------
// 원본 파일 크기 상한 검사 (G1·G2) — 이 프로그램의 상한으로
//=> 판정은 docparse 가 한다. 여기서는 지금 상한값을 넘기고, 걸렸으면 분리 전과
//   같은 사유 문장("크기 상한 초과: … (MAX_FILE_BYTES)")으로 돌려준다.
//
// -in: path = 검사할 원본 경로
// -in: fmt  = 감지된 포맷(텍스트 계열 판정에 쓴다)
//
// -out: Option<String> = 상한 초과 시 사유 문자열, 통과면 None
// -out: error = 없음(크기를 못 재면 None = 통과)
//------------------------------------------------------------------
pub fn check_size_limit(path: &Path, fmt: Fmt) -> Option<String> {
    crate::docparse::limits::check_size_limit(path, fmt, &extract_limits()).map(|h| h.reason())
}

#[cfg(test)]
mod tests {
    use super::*;

    //------------------------------------------------------------------
    // 기본값이 파이썬 판과 같은지 — 두 구현의 결과가 갈리지 않게 고정한다
    //=> 한쪽만 고치면 같은 배치에서 다른 판정이 나온다(설계 원칙 4).
    //------------------------------------------------------------------
    #[test]
    fn defaults_match_python() {
        // 상수를 직접 본다 — getter 를 부르면 환경변수나 다른 테스트가 등록한
        // CLI 덮어쓰기(OVERRIDES 는 프로세스에 하나뿐)에 값이 흔들려, 정합을
        // 지키라는 이 테스트가 실행 순서에 따라 통과/실패하는 신뢰 못 할
        // 테스트가 된다(실제로 그렇게 깨졌다).
        assert_eq!(DEFAULT_MAX_FILE_BYTES, 100 * 1024 * 1024);
        assert_eq!(DEFAULT_MAX_FILE_BYTES_TEXT, 100 * 1024 * 1024);
        assert_eq!(DEFAULT_MAX_TEXT_CHARS, 2_000_000);
        assert_eq!(DEFAULT_MAX_PDF_PAGES, 3000);
        assert_eq!(DEFAULT_PARSER_TIMEOUT, 60);
    }

    //------------------------------------------------------------------
    // CLI 덮어쓰기가 환경변수·기본값을 이긴다
    //=> 우선순위(CLI > 환경변수 > 기본값)가 파이썬 판과 같아야 한다. OnceLock 은
    //   프로세스에 한 번만 설정되므로, 이 테스트가 등록에 성공하든 다른 테스트가
    //   먼저 등록했든 '읽은 값이 등록된 값과 같다'만 확인한다.
    //------------------------------------------------------------------
    #[test]
    fn cli_overrides_win() {
        set_overrides(Overrides {
            max_file_bytes: Some(7 * 1_048_576),
            max_pdf_pages: Some(123),
            ..Default::default()
        });
        // 등록된 값이 있으면 그 값이 나와야 한다.
        let ov = OVERRIDES.get().expect("등록됨");
        if let Some(n) = ov.max_file_bytes {
            assert_eq!(max_file_bytes(), n);
        }
        if let Some(n) = ov.max_pdf_pages {
            assert_eq!(max_pdf_pages(), n as usize);
        }
        // 지정하지 않은 항목은 환경변수/기본값으로 내려간다.
        if ov.max_text_chars.is_none() && std::env::var("CSOCLASSIFY_MAX_TEXT_CHARS").is_err() {
            assert_eq!(max_text_chars(), 2_000_000);
        }
    }
}
