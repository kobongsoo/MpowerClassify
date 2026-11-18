//! 개인정보 검출 창구 — moaipii.dll / libmoaipii.so 를 불러 쓴다
//!
//! 설계: D:\Project\MoAI-pii\plan\MoAI-pii-개인정보검출-라이브러리분리-설계-20261117.html §6
//!
//! 개인정보 검출(ko-pii 1.15.2 이식 + 의도적 차이 A·A-2·B·G·H·I·J)은 별도 프로젝트
//! (D:\Project\MoAI-pii)가 만든 공유 라이브러리가 한다. 예전에는 이 파일에 검출기 2,677줄이
//! 있었다(+ pii_fold.rs · districts*.rs). 지금은 그 라이브러리를 실행 중에 한 번 불러(moaipii_dyn)
//! 예전과 같은 이름·모양의 함수(pii_counts · pii_records)를 내준다. 그래서 부르는 쪽(rules.rs)은
//! 바뀌지 않는다.
//!
//! 라이브러리를 찾는 순서: --moaipii-lib 인자(set_explicit_path) → 환경변수 CSO_MOAIPII
//! → 실행파일 옆 → 실행파일 폴더의 부모(cargo test). 못 찾으면 1018 pii_engine_missing 으로
//! 끝난다(PII 없이 등급을 매기면 C 등급이 소리 없이 빠지기 때문이다).

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::OnceLock;

use crate::moaipii_dyn::{MoaiPii, ScanOpts};

static LIB: OnceLock<MoaiPii> = OnceLock::new();
static EXPLICIT: OnceLock<PathBuf> = OnceLock::new();

//------------------------------------------------------------------
// 라이브러리 경로를 직접 지정 (--moaipii-lib)
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
// -out: &'static MoaiPii = 불러온 라이브러리(프로세스가 끝날 때까지 산다)
// -out: error = 못 찾거나 ABI 가 다르면 Err(사람용 사유)
//------------------------------------------------------------------
pub fn try_lib() -> Result<&'static MoaiPii, String> {
    if let Some(l) = LIB.get() {
        return Ok(l);
    }
    let l = MoaiPii::find_and_load(EXPLICIT.get().map(PathBuf::as_path), "CSO_MOAIPII")?;
    // 두 스레드가 동시에 불렀으면 먼저 넣은 쪽이 남는다(같은 파일이라 결과는 같다).
    if LIB.set(l).is_ok() {
        let l = LIB.get().expect("방금 넣었다");
        // 처음 불러온 때 한 번 — 어떤 검출기로 검사하는지 남긴다(재현할 때 판을 맞추려고).
        crate::linfo!("개인정보 검출 moaipii={} ko-pii기준={} lib={}", l.version(), l.kopii_basis(), l.path().display());
    }
    Ok(LIB.get().expect("방금 넣었다"))
}

//------------------------------------------------------------------
// 라이브러리 꺼내기 — 못 불러오면 프로그램을 끝낸다
//=> 라이브러리 없이 분류하면 모든 문서의 PII 가 0 건이 되어 C 등급이 소리 없이 빠진다.
//   그래서 분명한 문장과 1018 코드로 바로 멈춘다.
//
// -in: 없음
//
// -out: &'static MoaiPii
// -out: error = 실패 시 errcodes::fail("pii_engine_missing") — 반환하지 않음
//------------------------------------------------------------------
pub fn lib() -> &'static MoaiPii {
    match try_lib() {
        Ok(l) => l,
        Err(why) => crate::errcodes::fail("pii_engine_missing", &format!("[MpowerClassify-rs] {}", why), None),
    }
}

//------------------------------------------------------------------
// --version 에 찍을 검출기 판
//=> 라이브러리를 불러 판을 읽는다. --version 은 라이브러리가 없어도 답해야 하므로
//   못 불러오면 "없음" 으로 적는다(오류로 끝내지 않는다).
//
// -in: 없음
//
// -out: String = "moaipii 0.1.0+abc1234, ko-pii 기준 1.15.2" 또는 "moaipii 없음"
// -out: error = 없음
//------------------------------------------------------------------
pub fn version_label() -> String {
    match try_lib() {
        Ok(l) => format!("moaipii {}, ko-pii 기준 {}", l.version(), l.kopii_basis()),
        Err(_) => "moaipii 없음".to_string(),
    }
}

//------------------------------------------------------------------
// 원하는 라벨들의 건수 (예전 pii::pii_counts 와 같은 모양)
//=> 라이브러리에 '건수만'(items=false) 물어 라벨별 건수를 받는다. 겹침 해소 후 집계다.
//   모르는 라벨은 예전처럼 조용히 0 건으로 둔다(unknown_ignore) — 규칙 파일에 이 판이
//   모르는 라벨이 있어도 예전과 같은 결과가 나와야 한다.
//
// -in: text   = 본문
// -in: wanted = 볼 라벨들
//
// -out: HashMap<라벨, 건수> = 0 건 라벨은 없다
// -out: error = 라이브러리를 못 불러오면 1018 로 끝남, 라이브러리 내부 오류는 빈 결과 + 오류 로그
//------------------------------------------------------------------
pub fn pii_counts(text: &str, wanted: &HashSet<String>) -> HashMap<String, u32> {
    if text.is_empty() || wanted.is_empty() {
        return HashMap::new();
    }
    let opts = ScanOpts { items: false, values: false, offsets: "char", unknown_ignore: true, today: None };
    match lib().scan(text, wanted, &opts) {
        Ok(r) => r.counts.into_iter().collect(),
        Err(e) => {
            // 예전 코드에는 실패가 없었다 — 여기 오면 라이브러리 버그다. 문서 하나 때문에 전체를
            // 멈추지 않고, 남겨서 고칠 수 있게 한다.
            crate::errlog::err(&format!("[MpowerClassify-rs] 개인정보 검출 실패(건수) {}", e));
            HashMap::new()
        }
    }
}

//------------------------------------------------------------------
// [프라이버시 예외] 검출된 PII 원문 값 (--with-pii 전용, 예전 pii::pii_records 와 같은 모양)
//=> 반환: (label, value, start, end). 좌표는 추출·정제가 끝난 본문의 '문자' 수다
//   (파이썬 판과 같은 기준 — 받는 쪽이 이 좌표로 마스킹한다). 라이브러리에 offsets="char",
//   values=true 로 묻는다.
//
// -in: text   = 본문
// -in: wanted = 볼 라벨들
//
// -out: Vec<(라벨, 값, 시작, 끝)> = 겹침 해소가 끝난 목록(문서 순서)
// -out: error = 라이브러리를 못 불러오면 1018 로 끝남, 라이브러리 내부 오류는 빈 목록 + 오류 로그
//------------------------------------------------------------------
pub fn pii_records(text: &str, wanted: &HashSet<String>) -> Vec<(String, String, usize, usize)> {
    if text.is_empty() || wanted.is_empty() {
        return Vec::new();
    }
    let opts = ScanOpts { items: true, values: true, offsets: "char", unknown_ignore: true, today: None };
    match lib().scan(text, wanted, &opts) {
        Ok(r) => r.items.into_iter()
            .map(|i| (i.label, i.value.unwrap_or_default(), i.start, i.end))
            .collect(),
        Err(e) => {
            crate::errlog::err(&format!("[MpowerClassify-rs] 개인정보 검출 실패(원문 값) {}", e));
            Vec::new()
        }
    }
}
