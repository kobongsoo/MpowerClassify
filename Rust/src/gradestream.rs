//------------------------------------------------------------------
// 등급 스트림(:mgrade) 읽기 · 쓰기
//=> 분류가 끝난 문서의 NTFS 대체 데이터 스트림 ":mgrade" 에 C/S/O 등급을 쓴다.
//   PC 보안 에이전트(NEOPCHook)가 문서를 열 때 이 스트림을 읽어 허용·차단을 정한다.
//   형식은 NEOGradeToolApp -s 와 바이트까지 같다(12바이트: "MGRD" · 버전 1 · 등급).
//   설계: plan/등급스트림-기록-옵션-설계-20261019.html
//   파이썬 판 gradestream.py 와 같은 계약이다 — 한쪽을 고치면 다른 쪽도 고친다.
//------------------------------------------------------------------
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::collections::HashSet;

// 스트림 이름 · 크기 · 매직 · 버전 — Include/NEOPCCom.h 의 FILE_STREAM_GRADE_* 와 같다.
pub const STREAM_NAME: &str = "mgrade";
pub const INFO_SIZE: usize = 12;
pub const MAGIC: u32 = 0x4452_474D; // 'DRGM' → 디스크에는 4D 47 52 44("MGRD")
pub const VERSION_CURRENT: u32 = 1;
const GRADE_MAX: u32 = 4;

// Windows 오류 번호(툴이 따로 안내하는 셋 + 파일 없음)
const ERR_FILE_NOT_FOUND: i32 = 2;
const ERR_PATH_NOT_FOUND: i32 = 3;
const ERR_ACCESS_DENIED: i32 = 5;
const ERR_SHARING_VIOLATION: i32 = 32;
const ERR_INVALID_NAME: i32 = 123;

//------------------------------------------------------------------
// 엔진(CommonFileGrade.cpp)의 판별 상태 — 이름도 같다
//------------------------------------------------------------------
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum State {
    Marked,
    Unclassified,
    NoStream,
    UnsupportedVolume,
    ReadError,
    VersionUnsupported,
    FormatError,
}

//------------------------------------------------------------------
// 등급 이름 → 숫자
//=> NONE(0)은 '스트림은 있으나 미분류'라는 뜻이다(스트림 없음과 다르다).
//
// -in: g = "NONE" | "C" | "S" | "O"
//
// -out: u32 = 0..3 (모르는 이름은 0)
// -out: error = 없음
//------------------------------------------------------------------
pub fn grade_code(g: &str) -> u32 {
    match g { "C" => 1, "S" => 2, "O" => 3, _ => 0 }
}

//------------------------------------------------------------------
// 숫자 → 등급 이름
//
// -in: c = 0..3
//
// -out: &str = "NONE" | "C" | "S" | "O"
// -out: error = 없음
//------------------------------------------------------------------
pub fn code_grade(c: u32) -> &'static str {
    match c { 1 => "C", 2 => "S", 3 => "O", _ => "NONE" }
}

//------------------------------------------------------------------
// 이 플랫폼에서 쓸 수 있나
//=> 대체 데이터 스트림은 Windows(NTFS)에만 있다. 리눅스에서는 옵션을 거절한다.
//
// -in: 없음
//
// -out: bool = Windows 면 true
// -out: error = 없음
//------------------------------------------------------------------
pub fn supported() -> bool {
    cfg!(windows)
}

//------------------------------------------------------------------
// 긴 경로 접두어(\\?\) 붙이기
//=> 툴의 BuildGradeStreamPath 와 같은 규칙이다. 260자 제한을 피하고 엔진과 같은 경로를 쓴다.
//    1) 상대경로는 절대경로로 바꾼다
//    2) 이미 \\?\ 로 시작하면 그대로
//    3) UNC(\\server\share) 는 \\?\UNC\server\share
//    4) 드라이브 경로는 \\?\C:\...
//
// -in: path = 문서 경로
//
// -out: String = \\?\ 가 붙은 경로
// -out: error = 없음(절대경로를 못 만들면 받은 그대로 쓴다)
//------------------------------------------------------------------
pub fn long_path(path: &str) -> String {
    if path.starts_with(r"\\?\") {
        return path.to_string();
    }
    let abs = std::path::absolute(path)
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_else(|_| path.to_string());
    // 파이썬 abspath 처럼 / 를 \ 로 맞춘다 — \\?\ 경로는 / 를 구분자로 받지 않는다
    let abs = abs.replace('/', "\\");
    if let Some(rest) = abs.strip_prefix(r"\\") {
        return format!(r"\\?\UNC\{}", rest);
    }
    format!(r"\\?\{}", abs)
}

//------------------------------------------------------------------
// 등급 스트림 경로 만들기
//=> "<긴 경로>:mgrade" — 이 경로로 열면 본문이 아니라 대체 스트림이 열린다.
//
// -in: path = 문서 경로
//
// -out: String = 스트림 경로
// -out: error = 없음
//------------------------------------------------------------------
pub fn stream_path(path: &str) -> String {
    format!("{}:{}", long_path(path), STREAM_NAME)
}

//------------------------------------------------------------------
// 12바이트 내용 만들기
//=> 리틀엔디언 u32 3개(Magic · Version · Grade). C 구조체(#pragma pack(1))와 같은 배치다.
//
// -in: code = 등급 숫자(0..3)
//
// -out: [u8; 12]
// -out: error = 없음
//------------------------------------------------------------------
pub fn payload(code: u32) -> [u8; INFO_SIZE] {
    let mut b = [0u8; INFO_SIZE];
    b[0..4].copy_from_slice(&MAGIC.to_le_bytes());
    b[4..8].copy_from_slice(&VERSION_CURRENT.to_le_bytes());
    b[8..12].copy_from_slice(&code.to_le_bytes());
    b
}

//------------------------------------------------------------------
// 오류 번호 → 결과에 적을 reason
//=> 사람이 할 일이 다른 셋(볼륨 · 권한 · 공유)을 갈라 준다. 나머지는 os_error.
//
// -in: code = Windows 오류 번호
//
// -out: &str = reason
// -out: error = 없음
//------------------------------------------------------------------
pub fn reason_of(code: i32) -> &'static str {
    match code {
        ERR_INVALID_NAME => "unsupported_volume",
        ERR_ACCESS_DENIED => "access_denied",
        ERR_SHARING_VIOLATION => "sharing_violation",
        ERR_FILE_NOT_FOUND | ERR_PATH_NOT_FOUND => "not_found",
        _ => "os_error",
    }
}

//------------------------------------------------------------------
// 12바이트 내용 판정
//=> 엔진(NEOGetFileGrade)과 같은 순서로 검사한다.
//    1) 12바이트 미만 · 매직 불일치 = FormatError
//    2) 버전이 1..CURRENT 밖 = VersionUnsupported
//    3) 등급 ≥ MAX = FormatError
//    4) 0 이면 Unclassified, 아니면 Marked
//
// -in: data = 스트림 앞부분(최대 12바이트)
//
// -out: (State, Option<u32>) = 상태와 등급 숫자(Marked/Unclassified 일 때만)
// -out: error = 없음
//------------------------------------------------------------------
pub fn judge(data: &[u8]) -> (State, Option<u32>) {
    if data.len() < INFO_SIZE {
        return (State::FormatError, None);
    }
    let u = |i: usize| u32::from_le_bytes([data[i], data[i + 1], data[i + 2], data[i + 3]]);
    let (magic, ver, code) = (u(0), u(4), u(8));
    if magic != MAGIC {
        return (State::FormatError, None);
    }
    // 우리보다 새 버전은 뒤에 필드가 붙었을 수 있다 — 덮어쓰면 그 필드를 잃는다
    if ver < 1 || ver > VERSION_CURRENT {
        return (State::VersionUnsupported, None);
    }
    if code >= GRADE_MAX {
        return (State::FormatError, None);
    }
    (if code == 0 { State::Unclassified } else { State::Marked }, Some(code))
}

//------------------------------------------------------------------
// 지금 스트림 상태 읽기
//=> 열기 실패도 상태로 돌려준다: 파일 없음=NoStream, 이름 오류=ADS 미지원 볼륨, 그 밖=ReadError.
//
// -in: path = 문서 경로
//
// -out: (State, Option<u32>, i32) = 상태, 등급 숫자, 원본 오류 번호
// -out: error = 없음
//------------------------------------------------------------------
pub fn read_state(path: &str) -> (State, Option<u32>, i32) {
    use std::io::Read;
    let mut f = match std::fs::File::open(stream_path(path)) {
        Ok(f) => f,
        Err(e) => {
            let w = e.raw_os_error().unwrap_or(0);
            // 스트림이 없을 때 Windows 는 '파일 없음'을 돌려준다
            return match w {
                ERR_FILE_NOT_FOUND => (State::NoStream, None, w),
                // FAT32 · exFAT 처럼 ADS 가 없는 볼륨은 "경로:이름" 을 이름 오류로 본다
                ERR_INVALID_NAME => (State::UnsupportedVolume, None, w),
                _ => (State::ReadError, None, w),
            };
        }
    };
    let mut buf = Vec::with_capacity(INFO_SIZE);
    if let Err(e) = (&mut f).take(INFO_SIZE as u64).read_to_end(&mut buf) {
        return (State::ReadError, None, e.raw_os_error().unwrap_or(0));
    }
    let (st, code) = judge(&buf);
    (st, code, 0)
}

//------------------------------------------------------------------
// 경고 한 줄 로그
//=> log 모듈에는 INFO · DEBUG 매크로만 있어 수준을 직접 준다.
//
// -in: msg = 남길 문장
//
// -out: 없음
// -out: error = 없음
//------------------------------------------------------------------
fn warn(msg: &str) {
    crate::log::line("WARNING", "gradestream", msg);
}

//------------------------------------------------------------------
// 실패 결과 하나 만들기
//
// -in: grade = 쓰려던 등급 · reason = 까닭 · winerr = 원본 오류 번호(없으면 None)
//
// -out: Value = {"state":"failed","value":..,"reason":..,["winerr":..]}
// -out: error = 없음
//------------------------------------------------------------------
fn failed(grade: &str, reason: &str, winerr: Option<i32>) -> Value {
    let mut m = Map::new();
    m.insert("state".into(), json!("failed"));
    m.insert("value".into(), json!(grade));
    m.insert("reason".into(), json!(reason));
    if let Some(w) = winerr { m.insert("winerr".into(), json!(w)); }
    Value::Object(m)
}

//------------------------------------------------------------------
// 파일 시각 저장 · 복원 (기본 — --grade-stream-update-time 이면 안 한다)
//=> 스트림에 쓰면 NTFS 가 수정 시각을 바꾼다. 툴의 -k 처럼 생성·접근·수정 시각을 되돌린다.
//   FILE_WRITE_ATTRIBUTES 로만 열어 읽기 전용 파일 · 열려 있는 문서에도 시각만 만진다.
//------------------------------------------------------------------
#[cfg(windows)]
struct Times { created: std::time::SystemTime, accessed: std::time::SystemTime, modified: std::time::SystemTime }

#[cfg(windows)]
impl Times {
    //------------------------------------------------------------------
    // 시각 셋을 읽어 둔다
    //
    // -in: path = 문서 경로
    //
    // -out: Times
    // -out: error = 메타데이터를 못 읽으면 io::Error
    //------------------------------------------------------------------
    fn read(path: &str) -> std::io::Result<Times> {
        let m = std::fs::metadata(long_path(path))?;
        Ok(Times { created: m.created()?, accessed: m.accessed()?, modified: m.modified()? })
    }

    //------------------------------------------------------------------
    // 읽어 둔 시각으로 되돌린다
    //=> 스트림 쓰기 핸들을 닫은 뒤에 불러야 한다(닫힐 때 시각이 다시 바뀔 수 있다).
    //
    // -in: path = 문서 경로
    //
    // -out: 없음
    // -out: error = 열기 · SetFileTime 실패 시 io::Error
    //------------------------------------------------------------------
    fn restore(&self, path: &str) -> std::io::Result<()> {
        use std::os::windows::fs::{FileTimesExt, OpenOptionsExt};
        // 0x100 = FILE_WRITE_ATTRIBUTES, 공유 R/W/D 모두 허용
        let f = std::fs::OpenOptions::new().access_mode(0x100).share_mode(0x7).open(long_path(path))?;
        let t = std::fs::FileTimes::new()
            .set_accessed(self.accessed)
            .set_modified(self.modified)
            .set_created(self.created);
        f.set_times(t)
    }
}

//------------------------------------------------------------------
// 탐색기에 '이 파일이 바뀌었다' 알리기(파이썬 gradestream.notify_shell 과 같다)
//=> 대체 스트림만 바뀌면 탐색기는 아이콘 오버레이 · 툴팁을 다시 읽지 않아 F5 를 눌러야
//   새 등급이 보인다. SHChangeNotify(SHCNE_UPDATEITEM) 로 그 파일 하나만 다시 그리게 한다.
//    1) SHCNF_PATHW | SHCNF_FLUSHNOWAIT — 경로로 알리고, 탐색기 처리를 기다리지 않는다
//    2) 긴 경로 접두(\\?\)는 셸이 알아보지 못해 보통 절대 경로로 준다
//    3) 돌려주는 값이 없는 API 라 실패를 알 수 없다 — 등급은 이미 써졌다(F5 로 보인다)
//   새 크레이트 없이 shell32 를 직접 부른다(정적 단일 바이너리 목표 유지).
//
// -in: path = 문서 경로
//
// -out: 없음
// -out: error = 없음
//------------------------------------------------------------------
#[cfg(windows)]
pub fn notify_shell(path: &str) {
    use std::os::windows::ffi::OsStrExt;
    #[link(name = "shell32")]
    extern "system" {
        fn SHChangeNotify(event: i32, flags: u32, item1: *const std::ffi::c_void, item2: *const std::ffi::c_void);
    }
    // SHCNE_UPDATEITEM=0x2000 · SHCNF_PATHW=0x0005 · SHCNF_FLUSHNOWAIT=0x3000
    let abs = std::path::absolute(path).unwrap_or_else(|_| std::path::PathBuf::from(path));
    let wide: Vec<u16> = abs.as_os_str().encode_wide().chain(std::iter::once(0)).collect();
    unsafe { SHChangeNotify(0x2000, 0x0005 | 0x3000, wide.as_ptr() as *const _, std::ptr::null()); }
}

//------------------------------------------------------------------
// (Windows 가 아닌 곳) 탐색기가 없다 — 아무것도 하지 않는다
//
// -in: path = 무시
//
// -out: 없음
// -out: error = 없음
//------------------------------------------------------------------
#[cfg(not(windows))]
pub fn notify_shell(_path: &str) {}

//------------------------------------------------------------------
// 한 파일에 등급 쓰기
//=> 설계 §05 절차. 같은 값이면 안 쓰고, 새 버전 스트림은 지키고, 쓴 뒤 다시 읽어 확인한다.
//    1) 지금 상태를 읽는다 — 같은 등급이면 unchanged(수정 시각도 그대로)
//    2) 새 버전이면 skipped(newer_version), ADS 미지원 볼륨이면 바로 failed
//    3) keep_time 이면 시각을 저장, 읽기 전용이면 잠시 푼다
//    4) 12바이트를 쓴다(CREATE_ALWAYS)
//    5) 시각 · 읽기 전용을 되돌린다(실패는 경고만 — 등급은 이미 써졌다)
//    6) 다시 읽어 기대한 값인지 본다(가로채고 성공을 돌려주는 필터 대비)
//    7) 썼으면 탐색기에 알려 표식을 바로 다시 그리게 한다(notify_shell)
//
// -in: path      = 문서 경로
// -in: grade     = "C" | "S" | "O" | "NONE"
// -in: keep_time = true 면 시각 보존
//
// -out: Value = {"state": written|unchanged|skipped|failed, "value": 등급, ["prev"], ["reason"], ["winerr"]}
// -out: error = 없음(실패도 Value 로 돌려준다)
//------------------------------------------------------------------
#[cfg(windows)]
pub fn mark(path: &str, grade: &str, keep_time: bool) -> Value {
    use std::io::Write;
    let code = grade_code(grade);
    if !std::path::Path::new(path).is_file() {
        return failed(grade, "not_found", Some(ERR_FILE_NOT_FOUND));
    }
    let (st, cur, w) = read_state(path);
    // 이미 같은 값 — 쓰지 않아야 재실행해도 파일이 흔들리지 않는다
    if matches!(st, State::Marked | State::Unclassified) && cur == Some(code) {
        return json!({"state": "unchanged", "value": grade});
    }
    if st == State::VersionUnsupported {
        return json!({"state": "skipped", "value": grade, "reason": "newer_version"});
    }
    if st == State::UnsupportedVolume {
        return failed(grade, "unsupported_volume", Some(w));
    }
    let prev = if matches!(st, State::Marked | State::Unclassified) { cur.map(code_grade) } else { None };

    // 시각은 읽기 전용을 풀기 전에 읽어 둔다(속성 변경은 수정 시각을 안 바꾸지만 차례를 파이썬과 맞춘다)
    let times = if keep_time {
        match Times::read(path) {
            Ok(t) => Some(t),
            Err(e) => return failed(grade, reason_of(e.raw_os_error().unwrap_or(0)), e.raw_os_error()),
        }
    } else { None };
    // 읽기 전용 속성을 잠시 푼다(툴과 같다). 끝나면 반드시 되돌린다.
    let ro = std::fs::metadata(path).map(|m| m.permissions().readonly()).unwrap_or(false);
    let set_ro = |on: bool| -> std::io::Result<()> {
        let mut p = std::fs::metadata(path)?.permissions();
        p.set_readonly(on);
        std::fs::set_permissions(path, p)
    };
    let mut res: Result<(), std::io::Error> = Ok(());
    if ro {
        res = set_ro(false);
    }
    if res.is_ok() {
        // 블록 안에서 열고 써서, 시각 복원 전에 핸들이 닫히게 한다
        res = (|| {
            let mut f = std::fs::OpenOptions::new().write(true).create(true).truncate(true)
                .open(stream_path(path))?;
            f.write_all(&payload(code))
        })();
    }
    // 되돌리기 — 실패해도 등급 결과는 바꾸지 않는다
    if let Some(t) = &times {
        if let Err(e) = t.restore(path) {
            warn(&format!("등급스트림 시각 복원 실패 file={} :: {}", path, e));
        }
    }
    if ro {
        if let Err(e) = set_ro(true) {
            warn(&format!("등급스트림 읽기전용 복원 실패 file={} :: {}", path, e));
        }
    }
    if let Err(e) = res {
        let w = e.raw_os_error().unwrap_or(0);
        return failed(grade, reason_of(w), Some(w));
    }
    // 다시 읽어 확인 — 회사 미니필터가 쓰기를 삼키고 성공을 돌려주는 경우를 잡는다
    let (st2, cur2, _) = read_state(path);
    if !matches!(st2, State::Marked | State::Unclassified) || cur2 != Some(code) {
        return failed(grade, "verify_failed", None);
    }
    // 탐색기가 등급 표식(오버레이 · 툴팁)을 F5 없이 바로 다시 그리게 알린다
    notify_shell(path);
    let mut m = Map::new();
    m.insert("state".into(), json!("written"));
    m.insert("value".into(), json!(grade));
    if let Some(p) = prev { m.insert("prev".into(), json!(p)); }
    Value::Object(m)
}

//------------------------------------------------------------------
// (Windows 가 아닌 곳) 쓸 수 없다 — 인자 검사가 먼저 막으므로 실제로는 오지 않는다
//
// -in: path · grade · keep_time = 무시
//
// -out: Value = failed(os_error)
// -out: error = 없음
//------------------------------------------------------------------
#[cfg(not(windows))]
pub fn mark(_path: &str, grade: &str, _keep_time: bool) -> Value {
    failed(grade, "os_error", None)
}

//------------------------------------------------------------------
// 분류 실행 하나의 등급 스트림 기록기
//=> 최종 레코드(출력 직전)를 받아 무엇에 쓸지 고르고(설계 §04), 쓰고, 결과를 레코드에 붙인다.
//
// -필드: unclassified_none = true 면 등급 없는 문서에 NONE 을 쓴다(기본 false = skip)
// -필드: keep_time         = 수정 시각 보존 여부
// -필드: counts            = written/unchanged/skipped/failed 건수
// -필드: bad_volumes       = ADS 미지원 안내를 이미 낸 드라이브
//------------------------------------------------------------------
pub struct Marker {
    pub unclassified_none: bool,
    pub keep_time: bool,
    pub counts: BTreeMap<&'static str, u64>,
    bad_volumes: HashSet<String>,
}

impl Marker {
    //------------------------------------------------------------------
    // 기록기 만들기
    //
    // -in: unclassified_none = NONE 기록 여부 · keep_time = 시각 보존
    //
    // -out: Marker
    // -out: error = 없음
    //------------------------------------------------------------------
    pub fn new(unclassified_none: bool, keep_time: bool) -> Marker {
        let mut counts = BTreeMap::new();
        for k in ["written", "unchanged", "skipped", "failed"] { counts.insert(k, 0); }
        Marker { unclassified_none, keep_time, counts, bad_volumes: HashSet::new() }
    }

    //------------------------------------------------------------------
    // 레코드 하나 처리
    //=> 설계 §04 표대로 대상을 고르고 mark() 를 부른 뒤 rec["grade_stream"] 을 단다.
    //    1) 압축 집계 레코드(archive=true) → 압축 파일 자체에 집계 등급
    //    2) 압축 안 문서(is_member) → skipped(archive_member)
    //    3) 못 읽은 문서(error · excluded) → skipped(not_read)
    //    4) 압축 미해제 · 등급 없음 → unclassified 옵션대로(skip 또는 NONE)
    //    5) 그 밖 → 최종 등급
    //
    // -in: rec       = 출력 직전의 최종 레코드(제자리에서 grade_stream 을 단다)
    // -in: is_member = 압축 안에서 나온 문서인가
    //
    // -out: 없음
    // -out: error = 없음
    //------------------------------------------------------------------
    pub fn apply(&mut self, rec: &mut Value, is_member: bool) {
        let path = rec.get("file").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let flag = |k: &str| rec.get(k).map_or(false, |v| v.as_bool().unwrap_or(false));
        let is_archive = flag("archive");
        let unexpanded = flag("archive_unexpanded");
        // 본문을 못 읽은 문서도 사람이 등급을 정했으면 그 등급을 쓴다
        let is_manual = rec.get("why").and_then(|w| w.get("security")).and_then(|s| s.get("method"))
            .and_then(|m| m.as_str()) == Some("manual_override");
        let has = |k: &str| rec.get(k).map_or(false, |v| !v.is_null());
        let res = if path.is_empty() {
            json!({"state": "skipped", "reason": "not_read"})
        } else if !is_archive && is_member {
            // 압축 안에는 ADS 가 없다 — 꺼낸 임시본에 써도 의미가 없다
            json!({"state": "skipped", "reason": "archive_member"})
        } else if !is_archive && !unexpanded && !is_manual && (has("error") || has("excluded")) {
            // 내용을 못 본 판정이다 — NONE 옵션이어도 쓰지 않는다
            json!({"state": "skipped", "reason": "not_read"})
        } else {
            // 못 펼친 압축은 안을 안 봤으니 등급이 있어 보여도 미분류로 다룬다
            let g = if unexpanded { None } else { crate::record::grade_of(rec).map(|s| s.to_string()) };
            match g {
                None if !self.unclassified_none => json!({"state": "skipped", "reason": "unclassified"}),
                g => mark(&path, g.as_deref().unwrap_or("NONE"), self.keep_time),
            }
        };
        let state = res["state"].as_str().unwrap_or("failed");
        let key = match state { "written" => "written", "unchanged" => "unchanged", "skipped" => "skipped", _ => "failed" };
        *self.counts.entry(key).or_insert(0) += 1;
        self.log(&path, &res);
        rec["grade_stream"] = res;
    }

    //------------------------------------------------------------------
    // 파일당 한 줄 로그
    //=> ADS 미지원 볼륨은 같은 드라이브에서 한 번만 안내를 덧붙인다.
    //
    // -in: path = 문서 경로 · res = mark() 결과
    //
    // -out: 없음
    // -out: error = 없음
    //------------------------------------------------------------------
    fn log(&mut self, path: &str, res: &Value) {
        let extra: Vec<String> = ["value", "prev", "reason", "winerr"].iter()
            .filter_map(|k| res.get(*k).map(|v| format!("{}={}", k, v.as_str().map(|s| s.to_string()).unwrap_or_else(|| v.to_string()))))
            .collect();
        let state = res["state"].as_str().unwrap_or("");
        let line = format!("등급스트림 state={} file={} {}", state, path, extra.join(" "));
        // 실패는 WARNING 으로 남긴다(파이썬 판 log.warning 과 같은 수준)
        if state == "failed" { warn(line.trim_end()); } else { crate::linfo!("{}", line.trim_end()); }
        if res.get("reason").and_then(|v| v.as_str()) == Some("unsupported_volume") {
            // 드라이브 부분(C: 또는 \\server\share)만 떼어 한 번만 안내
            let drive: String = long_path(path).trim_start_matches(r"\\?\").chars().take(2).collect();
            if self.bad_volumes.insert(drive.clone()) {
                warn(&format!("등급스트림: {} 볼륨은 대체 데이터 스트림(ADS)을 지원하지 않습니다(FAT32 · exFAT 등) — 이 볼륨의 문서에는 등급을 쓸 수 없습니다", drive));
            }
        }
    }

    //------------------------------------------------------------------
    // 요약에 실을 집계
    //
    // -in: 없음
    //
    // -out: Value = {"written":..,"unchanged":..,"skipped":..,"failed":..}(파이썬 판과 같은 차례)
    // -out: error = 없음
    //------------------------------------------------------------------
    pub fn summary(&self) -> Value {
        json!({"written": self.counts["written"], "unchanged": self.counts["unchanged"],
               "skipped": self.counts["skipped"], "failed": self.counts["failed"]})
    }

    //------------------------------------------------------------------
    // 실패 건수
    //
    // -in: 없음
    //
    // -out: u64 = failed 건수
    // -out: error = 없음
    //------------------------------------------------------------------
    pub fn failed(&self) -> u64 {
        self.counts["failed"]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // 12바이트 배치가 툴 · 엔진과 같아야 한다 — "MGRD" · 버전 1 · 등급
    #[test]
    fn payload_bytes() {
        assert_eq!(payload(1), [0x4D, 0x47, 0x52, 0x44, 1, 0, 0, 0, 1, 0, 0, 0]);
        assert_eq!(payload(0)[8..], [0, 0, 0, 0]);
        assert_eq!(judge(&payload(3)), (State::Marked, Some(3)));
        assert_eq!(judge(&payload(0)), (State::Unclassified, Some(0)));
    }

    // 엔진과 같은 검사 순서 — 크기 · 매직 · 버전 · 범위
    #[test]
    fn judge_rules() {
        assert_eq!(judge(&[0u8; 5]).0, State::FormatError);
        let mut b = payload(1);
        b[0] = 0;
        assert_eq!(judge(&b).0, State::FormatError);
        let mut b = payload(1);
        b[4] = 2;
        assert_eq!(judge(&b).0, State::VersionUnsupported);
        let mut b = payload(1);
        b[8] = 4;
        assert_eq!(judge(&b).0, State::FormatError);
    }

    // 경로 규칙 — 툴의 BuildGradeStreamPath 와 같다
    #[cfg(windows)]
    #[test]
    fn stream_paths() {
        assert_eq!(stream_path(r"C:\a\b.docx"), r"\\?\C:\a\b.docx:mgrade");
        assert_eq!(stream_path(r"\\srv\sh\b.docx"), r"\\?\UNC\srv\sh\b.docx:mgrade");
        assert_eq!(stream_path(r"\\?\C:\a\b.docx"), r"\\?\C:\a\b.docx:mgrade");
    }
}
