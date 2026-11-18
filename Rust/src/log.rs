//! 일반 로그 — `<실행 파일 폴더>/log/class_YYYYMMDD.log` 에 남긴다.
//!
//! 왜 필요한가: 이 판(Rust)에는 오류 로그(`class_err_*.log`)만 있었다. 그래서
//! **정상으로 끝난 실행은 흔적이 하나도 남지 않았다**. "어제 그 문서를 무슨
//! 등급으로 봤나", "그때 어떤 인자로 돌렸나"를 나중에 물으면 답할 근거가 없다.
//! 파이썬 판(`MpowerClassify.exe`)은 처음부터 이 로그를 남겨 왔으므로, 두 판을
//! 같은 폴더에서 번갈아 쓰면 한쪽 기록만 뚝 끊긴 것처럼 보이기까지 했다.
//!
//! 규칙 — 파이썬 판 `logsetup.py` 와 **같은 자리·같은 이름·같은 줄 모양**이다:
//!  · 경로: `CSOCLASSIFY_LOGDIR` → 없으면 `<exe 폴더>/log/class_YYYYMMDD.log`
//!  · `--log <경로>` 를 주면 그 경로를 그대로 쓴다(파이썬 판과 같다).
//!  · 줄 모양: `2026-09-11 09:20:33 [PID 1234] INFO main.run:120: 내용`
//!  · 실행이 시작될 때마다 구분선 `-------------` 을 한 줄 먼저 남겨,
//!    이전 실행 로그와 눈으로 갈라 보이게 한다.
//!  · 오류 로그와 달리 **정상 실행에도 남긴다** — 그것이 이 로그의 목적이다.
//!  · 로그를 못 써도(권한 없음·읽기전용 폴더) **프로그램은 그대로 진행**한다.
//!    로그 때문에 분류가 멈추는 것이 더 나쁘다.

use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::OnceLock;

use crate::errlog::now_readable;

// 이번 실행에서 쓸 로그 파일 경로. init() 이 딱 한 번 정한다.
// Some(None) = "로그를 남기지 않기로 정해졌다"(경로를 못 구함).
static LOG_PATH: OnceLock<Option<PathBuf>> = OnceLock::new();

// --verbose 면 파일뿐 아니라 화면(stderr)에도 같은 줄을 낸다.
static VERBOSE: AtomicBool = AtomicBool::new(false);

//------------------------------------------------------------------
// 기본 로그 파일 경로
//=> `--log` 를 안 줬을 때 쓰는 경로를 정한다. 파이썬 판 `logsetup.log_dir()` 과
//   똑같은 규칙이어야 두 판의 로그가 한 폴더에 모인다.
//    1) 환경변수 CSOCLASSIFY_LOGDIR 이 있으면 그 폴더(여러 대를 한곳에 모을 때)
//    2) 없으면 <실행 파일이 있는 폴더>/log
//   파일 이름은 날짜별로 나눈다 — 한 파일이 무한정 커지지 않게.
//   exe 경로를 못 얻으면 None — 이때는 로그를 포기한다.
//
// -in: 없음
//
// -out: Option<PathBuf> = 로그 파일 경로(정할 수 없으면 None)
// -out: error = 없음
//------------------------------------------------------------------
pub fn default_path() -> Option<PathBuf> {
    // now_stamp() 은 "YYYYMMDDHHMMSS" — 앞 8자리가 날짜다(errlog 와 같은 방식).
    let stamp = crate::axes::now_stamp();
    let day = stamp.get(..8).unwrap_or("00000000");
    let name = format!("class_{}.log", day);

    if let Ok(d) = std::env::var("CSOCLASSIFY_LOGDIR") {
        if !d.is_empty() {
            return Some(PathBuf::from(d).join(name));
        }
    }
    let exe = std::env::current_exe().ok()?;
    let dir = exe.parent()?;
    Some(dir.join("log").join(name))
}

//------------------------------------------------------------------
// 로깅 시작 — 프로세스마다 딱 한 번
//=> 이번 실행이 어느 파일에 남길지 정하고, 구분선을 먼저 한 줄 찍는다.
//    1) --log 를 줬으면 그 경로, 아니면 default_path()
//    2) 상위 폴더가 없으면 만든다(log/ 가 아직 없는 새 배포본이 대부분이다)
//    3) 새 파일이면 UTF-8 BOM 을 얹는다 — 메모장·엑셀이 cp949 로 읽어
//       한글이 깨지는 것을 막는다(파이썬 판 _log_encoding 과 같은 이유)
//    4) 구분선 "-------------" 를 남긴다. 타임스탬프를 붙이지 않는 것도
//       파이썬 판과 같다 — 눈으로 실행 경계만 찾으려고 두는 줄이다
//   두 번째 호출부터는 아무 일도 하지 않는다(OnceLock).
//
// -in: cli_log = --log 로 받은 경로(None 이면 기본 경로)
// -in: verbose = True 면 파일과 함께 화면(stderr)에도 같은 줄을 낸다
//
// -out: 없음
// -out: error = 없음(파일을 못 열면 조용히 '로그 없음' 으로 진행)
//------------------------------------------------------------------
pub fn init(cli_log: Option<&str>, verbose: bool) {
    VERBOSE.store(verbose, Ordering::Relaxed);

    let resolved = match cli_log {
        Some(p) if !p.is_empty() => Some(PathBuf::from(p)),
        _ => default_path(),
    };
    // 경로를 정하지 못하면 여기서 '로그 없음'으로 못 박는다. 뒤의 line() 들은
    // 매번 다시 시도하지 않고 곧장 돌아간다(대량 처리에서 이 차이가 크다).
    let path = match resolved {
        Some(p) => p,
        None => {
            let _ = LOG_PATH.set(None);
            return;
        }
    };
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let is_new = std::fs::metadata(&path).map(|m| m.len() == 0).unwrap_or(true);
    match std::fs::OpenOptions::new().create(true).append(true).open(&path) {
        Ok(mut f) => {
            if is_new {
                let _ = f.write_all("\u{feff}".as_bytes());
            }
            let _ = writeln!(f, "-------------");
            let _ = LOG_PATH.set(Some(path));
        }
        // 못 열면 로그를 포기한다 — 분류는 그대로 계속되어야 한다.
        Err(_) => {
            let _ = LOG_PATH.set(None);
        }
    }
}

//------------------------------------------------------------------
// 로그가 켜져 있는지
//=> 줄을 만들기 전에 물어보는 용도다. 레코드 JSON 처럼 **만드는 것 자체가 비싼**
//   메시지는, 어차피 버려질 거라면 만들지도 말아야 한다(수천 건이면 차이가 난다).
//
// -in: 없음
//
// -out: bool = true 면 파일이나 화면 중 한 곳에는 남는다
// -out: error = 없음
//------------------------------------------------------------------
pub fn is_on() -> bool {
    VERBOSE.load(Ordering::Relaxed)
        || matches!(LOG_PATH.get(), Some(Some(_)))
}

//------------------------------------------------------------------
// 로그 한 줄 쓰기
//=> 파이썬 판과 같은 모양으로 한 줄 남긴다:
//   `2026-09-11 09:20:33 [PID 1234] INFO main.run:120: 내용`
//   PID 를 넣는 이유는 파이썬 판과 같다 — 한 파일에 여러 프로세스가 섞여 써도
//   누가 쓴 줄인지 갈라 볼 수 있어야 한다.
//   init() 을 아직 안 불렀거나 로그를 못 여는 상황이면 조용히 아무 일도 안 한다.
//
// -in: level = "INFO" | "DEBUG" | "ERROR" 같은 등급 이름
// -in: site  = 어디서 남겼는지("main.run:120" 처럼 모듈.함수:줄)
// -in: msg   = 남길 내용
//
// -out: 없음
// -out: error = 없음(모든 쓰기 실패를 무시)
//------------------------------------------------------------------
pub fn line(level: &str, site: &str, msg: &str) {
    if !is_on() {
        return;
    }
    let mut text = format!("{} [PID {}] {} {}: {}",
                           now_readable(), std::process::id(), level, site, msg);
    // 긴 로그는 끝에 줄바꿈을 하나 더 붙여, writeln 의 줄바꿈과 합쳐 빈 줄로 구분한다.
    spaced(&mut text);
    if VERBOSE.load(Ordering::Relaxed) {
        eprintln!("{}", text);
    }
    let p = match LOG_PATH.get() {
        Some(Some(p)) => p,
        _ => return,
    };
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(p) {
        let _ = writeln!(f, "{}", text);
    }
}

/// 이 글자 수 이상인 로그 한 건 뒤에는 빈 줄을 하나 둔다(파이썬 판 logsetup.LONG_LINE_CHARS 와 같은 값).
pub const LONG_LINE_CHARS: usize = 250;

//------------------------------------------------------------------
// 긴 로그 뒤 빈 줄 붙이기
//=> 경로·명령줄·오류 사유가 든 로그는 한 건이 수백 자라 편집기에서 여러 줄로 접혀
//   보인다. 그러면 어디서 끝나고 다음 로그가 어디서 시작하는지 알기 어렵다.
//   다 만든 로그 한 건(시각·PID 머리말 포함)이 LONG_LINE_CHARS 글자 이상이면 끝에
//   '\n' 을 하나 붙인다 — 쓰는 쪽의 writeln 줄바꿈과 합쳐 빈 줄 한 줄이 된다.
//
// -in: text = 다 만든 로그 한 건(고쳐 쓴다)
//
// -out: 없음
// -out: error = 없음
//------------------------------------------------------------------
pub fn spaced(text: &mut String) {
    // 바이트가 아니라 글자 수로 센다 — 한글은 3바이트라 바이트로 세면 일찍 걸린다.
    // 앞의 LONG_LINE_CHARS 글자만 세어 보면 되므로 긴 로그도 끝까지 훑지 않는다.
    if text.chars().take(LONG_LINE_CHARS).count() >= LONG_LINE_CHARS {
        text.push('\n');
    }
}

//------------------------------------------------------------------
// 지금 실행 중인 함수 이름
//=> 러스트에는 C 의 __func__ 같은 것이 없다. 그래서 '이 자리에 빈 함수를 하나
//   만들어 그 타입 이름을 물어보는' 상투 수법을 쓴다. 타입 이름은
//   "MpowerClassify_rs::main::f" 처럼 나오므로 끝의 "::f" 세 글자를 떼면
//   그 자리를 감싼 함수의 경로가 남는다.
//   왜 굳이 이렇게까지 하나: 파이썬 판 로그가 `cli._main:3657` 처럼 함수까지
//   적어 준다. 줄 번호만 있으면 코드가 조금만 움직여도 어디였는지 못 찾는다.
//------------------------------------------------------------------
#[macro_export]
macro_rules! func_name {
    () => {{
        fn f() {}
        fn type_name_of<T>(_: T) -> &'static str { std::any::type_name::<T>() }
        let name = type_name_of(f);
        // 끝의 "::f" 를 뗀다. 형식이 어긋나면 원본을 그대로 쓴다(로그 때문에
        // 패닉이 나면 안 된다 — 이 매크로는 오류 처리 경로에서도 불린다).
        name.strip_suffix("::f").unwrap_or(name)
    }};
}

//------------------------------------------------------------------
// 로그 매크로 — 남긴 자리를 자동으로 붙인다
//=> `linfo!("대상 {}건", n)` 처럼 쓰면 함수 경로와 줄 번호가 알아서 붙어
//   `MpowerClassify_rs::main:120` 이 된다. 호출부에서 자리를 손으로 적으면
//   코드가 움직일 때마다 틀려지므로 매크로가 대신 채운다.
//   `is_on()` 을 먼저 보는 것이 중요하다 — 로그가 꺼져 있으면 format! 자체를
//   건너뛰어, 대량 처리에서 쓸데없는 문자열 조립 비용을 물지 않는다.
//------------------------------------------------------------------
#[macro_export]
macro_rules! linfo {
    ($($arg:tt)*) => {
        if $crate::log::is_on() {
            $crate::log::line("INFO",
                &format!("{}:{}", $crate::func_name!(), line!()),
                &format!($($arg)*));
        }
    };
}

#[macro_export]
macro_rules! ldebug {
    ($($arg:tt)*) => {
        if $crate::log::is_on() {
            $crate::log::line("DEBUG",
                &format!("{}:{}", $crate::func_name!(), line!()),
                &format!($($arg)*));
        }
    };
}

#[cfg(test)]
mod tests {
    use super::*;

    // 이 모듈의 테스트는 **프로세스 전역 상태**(OnceLock LOG_PATH, 환경변수
    // CSOCLASSIFY_LOGDIR)를 건드린다. cargo test 는 테스트를 병렬 스레드로
    // 돌리므로 따로 두면 서로의 환경변수를 덮어써 엉뚱한 파일에 쓴다
    // (errlog 테스트가 실제로 그렇게 깨졌다). 한 테스트 안에서 순서대로 본다.
    // 경계값 — 글자 수로 세고, 249자는 그대로 · 250자부터 빈 줄
    #[test]
    fn spaced_counts_chars() {
        let mut a = "가".repeat(249);
        spaced(&mut a);
        assert!(!a.ends_with('\n'), "249자는 그대로여야 한다");
        let mut b = "가".repeat(250);
        spaced(&mut b);
        assert!(b.ends_with('\n') && !b.ends_with("\n\n"), "250자는 줄바꿈 하나가 붙어야 한다");
    }

    #[test]
    fn log_path_and_write() {
        // ---- (1) CSOCLASSIFY_LOGDIR 을 주면 그 폴더의 class_YYYYMMDD.log ----
        let tmp = std::env::temp_dir().join(format!("mpc_log_test_{}", std::process::id()));
        let _ = std::fs::create_dir_all(&tmp);
        std::env::set_var("CSOCLASSIFY_LOGDIR", &tmp);

        let p = default_path().expect("경로를 정할 수 있어야 한다");
        assert_eq!(p.parent().unwrap(), tmp.as_path());
        let name = p.file_name().unwrap().to_string_lossy().into_owned();
        // class_ + YYYYMMDD + .log — 파이썬 판과 이름이 같아야 한 폴더에서 짝이 맞는다.
        assert!(name.starts_with("class_"), "{}", name);
        assert!(!name.starts_with("class_err_"), "오류 로그와 이름이 겹치면 안 된다: {}", name);
        assert_eq!(name.len(), 6 + 8 + 4, "{}", name);

        // ---- (2) init() 이 구분선을 남기고, line() 이 이어 쓴다 ----
        init(None, false);
        assert!(is_on(), "경로를 구했으면 로그가 켜져야 한다");
        line("INFO", "test.case:1", "첫 줄");

        let body = std::fs::read_to_string(&p).unwrap_or_default();
        assert!(body.contains("-------------"), "구분선이 있어야 한다: {}", body);
        assert!(body.contains("첫 줄"), "쓴 내용이 있어야 한다: {}", body);
        assert!(body.contains("[PID "), "PID 가 붙어야 한다: {}", body);
        // 새 파일이면 BOM 이 맨 앞에 온다 — 메모장이 한글을 깨뜨리지 않게.
        let raw = std::fs::read(&p).unwrap_or_default();
        assert_eq!(&raw[..3], b"\xef\xbb\xbf", "새 파일은 UTF-8 BOM 으로 시작해야 한다");

        // ---- (3) 250자 이상인 로그 뒤에는 빈 줄, 짧은 로그는 그대로 ----
        line("INFO", "test.case:2", &"가".repeat(300));
        line("INFO", "test.case:3", "짧은 줄");
        let body = std::fs::read_to_string(&p).unwrap_or_default().replace("\r\n", "\n");
        assert!(body.contains(&format!("{}\n\n", "가".repeat(300))), "긴 로그 뒤에 빈 줄이 있어야 한다");
        assert!(body.contains("짧은 줄\n") && !body.contains("짧은 줄\n\n"), "짧은 로그 뒤에는 빈 줄이 없어야 한다");

        // ---- 뒷정리 ----
        std::env::remove_var("CSOCLASSIFY_LOGDIR");
        let _ = std::fs::remove_dir_all(&tmp);
    }
}
