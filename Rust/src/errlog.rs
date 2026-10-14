//! 오류 로그 — `<실행 파일 폴더>/log/class_err_YYYYMMDD.log` 에 남긴다.
//!
//! 왜 필요한가: 이 프로그램은 오류를 화면(stderr)으로만 알렸다. 사람이 콘솔에서
//! 직접 돌릴 때는 보이지만, UI·배치·스케줄러가 실행하면 그 화면이 없어서
//! **무엇 때문에 실패했는지 흔적이 아무것도 남지 않는다**. 나중에 "왜 안 됐나"를
//! 물어도 답할 근거가 없다. 그래서 오류만 따로 파일로 모은다.
//!
//! 규칙:
//!  · **오류가 실제로 났을 때만** 파일을 만든다 — 정상 실행 때마다 빈 파일이
//!    쌓이면 오히려 "오류가 있었나?" 하고 헷갈린다.
//!  · 파일이 무한정 커지지 않도록 **날짜별**로 나눈다(파이썬 판 로그와 같은 방식).
//!  · 첫 오류 줄 앞에 **그때 실행한 명령줄**을 한 번 남긴다 — 로그 파일만 받아도
//!    무슨 인자로 돌렸는지 알 수 있어야 재현이 된다.
//!  · 로그를 못 써도(권한 없음·읽기전용 폴더) **프로그램은 그대로 진행**한다.
//!    로그 때문에 분류가 멈추는 것이 더 나쁘다.

use std::io::Write;
use std::path::PathBuf;
use std::sync::Once;

use crate::axes::now_stamp;

// 이번 실행에서 헤더(명령줄)를 이미 남겼는지 — 첫 오류 때 한 번만 쓴다.
static HEADER: Once = Once::new();

//------------------------------------------------------------------
// 오류 로그 파일 경로
//=> 어디에 남길지 정한다.
//    1) 환경변수 CSOCLASSIFY_ERRLOG 가 있으면 그 '파일 경로'를 그대로 쓴다
//       (읽기전용 폴더에 설치했거나, 여러 대의 로그를 한 파일로 모을 때)
//    2) 환경변수 CSOCLASSIFY_LOGDIR 이 있으면 그 폴더의 class_err_YYYYMMDD.log
//    3) 없으면 <실행 파일이 있는 폴더>/log/class_err_YYYYMMDD.log
//   exe 경로를 못 얻으면 None — 이때는 로그를 포기하고 화면에만 낸다.
//
//   [왜 exe 옆이 아니라 log/ 인가] 예전에는 실행 파일 바로 옆에 남겼는데,
//   여러 폴더에서 exe 를 돌리면 로그가 폴더마다 흩어져 어디를 봐야 하는지
//   알 수 없었다. 일반 로그(파이썬 판 log/class_*.log)와 같은 자리에 모은다.
//
// -in: 없음
//
// -out: Option<PathBuf> = 로그 파일 경로(정할 수 없으면 None)
// -out: error = 없음
//------------------------------------------------------------------
pub fn path() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("CSOCLASSIFY_ERRLOG") {
        if !p.is_empty() {
            return Some(PathBuf::from(p));
        }
    }
    // now_stamp() 은 "YYYYMMDDHHMMSS" — 앞 8자리가 날짜다.
    let stamp = now_stamp();
    let day = stamp.get(..8).unwrap_or("00000000");
    let name = format!("class_err_{}.log", day);

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
// 사람이 읽을 시각 문자열
//=> now_stamp() 의 "YYYYMMDDHHMMSS" 를 "YYYY-MM-DD HH:MM:SS" 로 벌린다.
//   로그를 눈으로 훑을 때 구분자가 있어야 시각이 바로 읽힌다.
//
// -in: 없음
//
// -out: String = "2026-08-26 11:20:33" (형식이 어긋나면 원본 그대로)
// -out: error = 없음
//------------------------------------------------------------------
pub(crate) fn now_readable() -> String {
    let s = now_stamp();
    if s.len() < 14 {
        return s;
    }
    format!("{}-{}-{} {}:{}:{}", &s[0..4], &s[4..6], &s[6..8], &s[8..10], &s[10..12], &s[12..14])
}

//------------------------------------------------------------------
// 로그 파일에 한 줄 덧붙이기(화면에는 안 냄)
//=> 파일을 append 로 열어 한 줄 쓴다. 첫 호출이면 실행 명령줄 헤더를 먼저 남긴다.
//   쓰기에 실패해도 조용히 넘어간다 — 로그가 안 써진다고 분류를 멈출 이유가 없고,
//   여기서 또 에러를 내면 오류 처리 중에 오류가 나는 꼴이 된다.
//
// -in: msg = 남길 내용(여러 줄이어도 됨)
//
// -out: 없음
// -out: error = 없음(모든 실패를 무시)
//------------------------------------------------------------------
pub fn note(msg: &str) {
    let p = match path() {
        Some(p) => p,
        None => return,
    };
    // 이제 로그가 log/ 하위로 들어가므로 폴더가 없을 수 있다. 만들지 않으면
    // open 이 그냥 실패해 로그가 조용히 사라진다 — 오류를 남기려던 코드가
    // 오류 때문에 아무것도 못 남기는 셈이 된다.
    if let Some(dir) = p.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    // 새 파일이면 UTF-8 BOM 을 먼저 얹는다. Windows 의 메모장·엑셀·PowerShell 은
    // BOM 이 없는 UTF-8 을 ANSI(cp949)로 읽어 한글이 통째로 깨진다.
    // 이어쓰는 파일에는 붙이지 않는다 — 파일 중간에 BOM 이 박히면 더 지저분하다.
    let is_new = std::fs::metadata(&p).map(|m| m.len() == 0).unwrap_or(true);
    let mut f = match std::fs::OpenOptions::new().create(true).append(true).open(&p) {
        Ok(f) => f,
        Err(_) => return,
    };
    if is_new {
        let _ = f.write_all("\u{feff}".as_bytes());
    }
    // 헤더: 이번 실행이 무슨 명령이었는지. 로그 파일만 봐도 재현할 수 있어야 한다.
    //
    // ★ args_os() 를 쓰는 것이 중요하다. std::env::args() 는 인자에 UTF-8 이 아닌
    //   바이트가 섞이면 **패닉한다**. 이 함수는 패닉 훅에서도 불리므로, 여기서 또
    //   패닉하면 '패닉 처리 중 패닉'이 되어 프로세스가 abort 되고 로그가 0바이트로
    //   남는다(실제로 그렇게 깨졌다). args_os()+lossy 는 절대 패닉하지 않는다.
    HEADER.call_once(|| {
        let args: Vec<String> = std::env::args_os()
            .map(|a| a.to_string_lossy().into_owned())
            .collect();
        let _ = writeln!(f, "\n===== {} pid={} =====", now_readable(), std::process::id());
        // 열쇠(--mrk 뒤의 값)는 오류 로그에도 남기지 않는다.
        let _ = writeln!(f, "cmd: {}", crate::decrypt::mask_args(&args).join(" "));
    });
    let _ = writeln!(f, "{} {}", now_readable(), msg);
}

//------------------------------------------------------------------
// 오류 한 줄 — 화면과 로그 양쪽에 남긴다
//=> 지금까지 eprintln! 로만 내던 오류 메시지를 이 함수로 바꾸면, 화면 동작은
//   그대로이면서 파일에도 흔적이 남는다(호출부 수정이 한 줄로 끝난다).
//
// -in: msg = 오류 메시지
//
// -out: 없음
// -out: error = 없음
//------------------------------------------------------------------
pub fn err(msg: &str) {
    // 일반 로그에도 같은 줄을 남긴다. 파이썬 판은 로거 하나에 핸들러 둘을 달아
    // 오류가 두 파일에 모두 들어간다 — 이 판도 같아야, 일반 로그만 보고
    // "그때 아무 문제 없었네" 하고 잘못 읽는 일이 없다.
    crate::log::line("ERROR", "errlog.err", msg);
    // --json-errors 를 준 호출에서는 화면으로 내지 않는다 — 기계가 읽는 줄만
    // stderr 에 남기기로 했다(진행률·요약). 로그 파일에는 언제나 남긴다.
    if !crate::errcodes::quiet() {
        eprintln!("{}", msg);
    }
    note(msg);
}

//------------------------------------------------------------------
// 치명적 오류 — 남기고 즉시 종료
//=> "메시지 내고 exit" 를 한 번에 한다. 반환하지 않는다(`!`).
//
// -in: msg  = 오류 메시지
// -in: code = 종료 코드(2=인자/파일 문제, 3=인자 오류, 4=규칙셋 내용 오류)
//
// -out: 반환하지 않음(프로세스 종료)
// -out: error = 없음
//------------------------------------------------------------------
pub fn fail(msg: &str, code: i32) -> ! {
    err(msg);
    note(&format!("종료 코드 {} 로 중단합니다.", code));
    std::process::exit(code)
}

//------------------------------------------------------------------
// 패닉(예상 못 한 내부 오류)도 로그에 남기기
//=> Rust 는 패닉이 나면 기본적으로 화면에만 메시지를 내고 죽는다. UI·배치에서
//   돌리면 그 메시지가 사라져 원인을 영영 알 수 없다. 기본 동작(화면 출력)은
//   그대로 두고, 파일에도 같은 내용을 남기도록 훅을 하나 얹는다.
//   프로그램 시작 직후 한 번 부른다.
//
// -in: 없음
//
// -out: 없음
// -out: error = 없음
//------------------------------------------------------------------
pub fn install_panic_hook() {
    let prev = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        // 패닉 메시지는 &str 또는 String 으로 들어온다 — 둘 다 받아낸다.
        let payload = info.payload();
        let msg = payload.downcast_ref::<&str>().map(|s| s.to_string())
            .or_else(|| payload.downcast_ref::<String>().cloned())
            .unwrap_or_else(|| "(내용 없음)".to_string());
        let at = match info.location() {
            Some(l) => format!("{}:{}:{}", l.file(), l.line(), l.column()),
            None => "(위치 불명)".to_string(),
        };
        note(&format!("[패닉] {} @ {}", msg, at));
        note("  ↑ 예상하지 못한 내부 오류입니다. 이 로그와 실행 명령을 함께 알려 주세요.");
        prev(info);   // 기존 동작(화면 출력)도 그대로 유지
    }));
}

#[cfg(test)]
mod tests {
    use super::*;

    // 이 모듈의 테스트는 전부 **프로세스 전역 상태**(환경변수 CSOCLASSIFY_ERRLOG,
    // HEADER: Once, 패닉 훅)를 건드린다. cargo test 는 테스트를 병렬 스레드로 돌리므로
    // 따로 두면 서로의 환경변수를 덮어써 엉뚱한 파일에 쓰게 된다(실제로 그렇게 깨졌다).
    // 그래서 전역 상태를 쓰는 검사는 **한 테스트 안에서 순서대로** 확인한다.
    #[test]
    fn 오류로그_경로와_기록_규칙() {
        // ---- (1) 경로: 환경변수가 있으면 그 경로를 그대로 쓴다 ----
        std::env::set_var("CSOCLASSIFY_ERRLOG", "D:/tmp/my_err.log");
        assert_eq!(path(), Some(PathBuf::from("D:/tmp/my_err.log")));

        // 빈 값은 '지정 안 함'으로 본다 — 빈 경로에 쓰려다 실패하면 더 헷갈린다.
        std::env::set_var("CSOCLASSIFY_ERRLOG", "");
        let p = path().unwrap();
        assert!(p.file_name().unwrap().to_string_lossy().starts_with("class_err_"));

        // ---- (2) 기본 경로: <exe 폴더>/log/class_err_YYYYMMDD.log ----
        std::env::remove_var("CSOCLASSIFY_ERRLOG");
        let full = path().unwrap();
        // 파이썬 판(logsetup.log_dir)과 같은 자리여야 한다 — 두 판의 로그가
        // 서로 다른 폴더로 흩어지면 한 사건을 좇는 데 두 곳을 뒤져야 한다.
        assert_eq!(full.parent().unwrap().file_name().unwrap(), "log");
        let name = full.file_name().unwrap().to_string_lossy().to_string();
        assert!(name.starts_with("class_err_"), "{}", name);
        assert!(name.ends_with(".log"), "{}", name);
        assert_eq!(name.len(), 10 + 8 + 4, "{}", name);   // class_err_ + YYYYMMDD + .log
        assert!(name[10..18].chars().all(|c| c.is_ascii_digit()), "{}", name);

        // ---- (2-1) CSOCLASSIFY_LOGDIR 로 폴더만 옮길 수 있다 ----
        std::env::set_var("CSOCLASSIFY_LOGDIR", "D:/tmp/mylogs");
        let d = path().unwrap();
        assert_eq!(d.parent().unwrap(), PathBuf::from("D:/tmp/mylogs"));
        assert!(d.file_name().unwrap().to_string_lossy().starts_with("class_err_"));
        std::env::remove_var("CSOCLASSIFY_LOGDIR");

        // ---- (3) 오류를 쓰면 그때 파일이 생기고, 명령줄 헤더가 한 번만 들어간다 ----
        let tmp = std::env::temp_dir().join("csors_errlog_test.log");
        let _ = std::fs::remove_file(&tmp);
        std::env::set_var("CSOCLASSIFY_ERRLOG", &tmp);
        note("첫 오류입니다");
        note("두 번째 오류입니다");
        let body = std::fs::read_to_string(&tmp).unwrap();
        assert!(body.contains("첫 오류입니다"), "{}", body);
        assert!(body.contains("두 번째 오류입니다"), "{}", body);
        assert!(body.contains("cmd: "), "명령줄 헤더가 없다:
{}", body);
        // 헤더는 이번 실행에 한 번만 — 오류마다 반복되면 로그가 읽기 어려워진다.
        assert_eq!(body.matches("cmd: ").count(), 1, "{}", body);

        // ---- (4) 패닉(예상 못 한 내부 오류)도 같은 파일에 남는다 ----
        // catch_unwind 로 패닉을 잡아 테스트 프로세스가 죽지 않게 한 채 훅만 확인한다.
        install_panic_hook();
        let r = std::panic::catch_unwind(|| {
            panic!("일부러 낸 내부 오류");
        });
        assert!(r.is_err());
        let body = std::fs::read_to_string(&tmp).unwrap();
        assert!(body.contains("[패닉] 일부러 낸 내부 오류"), "{}", body);
        // 어디서 터졌는지(파일:줄)가 있어야 원인을 찾을 수 있다.
        assert!(body.contains(r"src\errlog.rs:") || body.contains("src/errlog.rs:"), "{}", body);

        let _ = std::panic::take_hook();
        std::env::remove_var("CSOCLASSIFY_ERRLOG");
        let _ = std::fs::remove_file(&tmp);
    }
}
