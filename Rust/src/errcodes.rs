//! CLI 오류 계약 — 기계가 읽는 오류 코드 표.
//!
//! 설계: `plan/CLI-오류출력-설계.html`
//! 파이썬 판 `src/csoclassify/errcodes.py` 와 **같은 표**를 쓴다. 한쪽만 고치면
//! 두 엔진이 같은 상황에서 다른 번호를 내게 되므로, 표를 고칠 때는 반드시 양쪽을
//! 함께 고치고 `tests/test_errcodes.py` 의 대조 테스트로 확인한다
//! (그 테스트가 이 파일의 표를 읽어 파이썬 표와 한 줄씩 맞춰 본다).

use serde_json::Value;
use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use std::sync::Mutex;

use crate::errlog;

/// 오류 표 — (kind, code, exit).
///
/// * `code` 는 오류마다 유일하다. 한번 부여한 번호는 뜻을 바꾸지 않으며, 어떤
///   오류가 없어져도 그 번호는 **다시 쓰지 않는다** — 재사용하면 옛 버전을
///   상대하던 프로그램이 조용히 다른 뜻으로 분기한다.
/// * `exit` 는 지금까지의 약속(0~4)을 그대로 지킨다. 거친 종료코드로는 원인을
///   구분할 수 없어 `code` 를 따로 두는 것이지, 종료코드를 늘리는 것이 아니다.
/// * 백의 자리로 갈래를 묶는다 — 1000 인자·입력 / 2000 정책 파일 / 3000 처리 /
///   4000 출력 / 9000 그 밖. 받는 쪽이 모르는 번호를 만나도 갈래로는 대응할 수 있다.
pub const ERRORS: &[(&str, i64, i32)] = &[
    // ── 1000 인자 · 입력 ─────────────────────────────────────────────
    ("no_input", 1001, 3),
    ("no_target_arg", 1002, 3),
    ("bad_args", 1003, 3),
    ("bad_axis", 1004, 3),
    ("bad_failsafe", 1005, 3),
    ("mode_conflict", 1006, 3),
    ("seeds_missing", 1007, 3),
    ("propagate_input_missing", 1008, 3),
    ("bad_conflict_axis", 1009, 4),
    // 목록(--filelist)은 정책 파일이 아니라 부른 쪽이 준 입력이라, 내용이 틀려도 3 이다.
    ("filelist_missing", 1010, 3),
    ("filelist_invalid", 1011, 3),
    // 이 판에 없는 기능(데몬)을 요구받았을 때. 조용히 무시하면 부르는 쪽은
    // 요청이 받아들여진 줄 안다 — 그건 결과를 못 읽는 상태로 이어진다.
    ("unsupported_option", 1012, 3),
    // seed 벡터가 이번 모델과 차원이 다름 — 다른 모델로 만든 seed 와는 비교가 성립하지
    // 않는다. 부른 쪽이 --model 이나 seed 를 맞추면 고칠 수 있어 3 이다(2026-09-29).
    ("seeds_model_mismatch", 1013, 3),
    // 엠파워 v11 암호화 문서 복호화(설계: plan/엠파워v11-암호화문서-복호화연동-설계-20261012.html §7).
    // --decrypt 조합이 틀렸거나 도구가 없는 것은 부른 쪽이 고칠 수 있어 3 이다.
    ("decrypt_args_invalid", 1014, 3),
    ("decrypt_cmd_missing", 1015, 3),
    // 사이냅(snf_exe) 모드를 골랐는데 실행파일이 없거나 실행할 수 없다
    // (설계: plan/Rust-사이냅추출기-옵션-설계-20261019.html §6). 놓아 주면 되므로 3 이다.
    ("synap_missing", 1016, 3),
    // ── 2000 정책 파일(규칙셋 · 분류체계) ────────────────────────────
    ("rules_missing", 2001, 3),
    ("rules_invalid", 2002, 4),
    ("taxonomy_missing", 2003, 3),
    ("taxonomy_invalid", 2004, 4),
    ("doc_rules_invalid", 2005, 4),
    ("doc_rules_write_failed", 2006, 4),
    ("export_input_missing", 2007, 3),
    ("export_input_invalid", 2008, 4),
    // 사람 결정 목록(설계: 사람등급지정-수동오버라이드-설계 §08)
    ("grade_overrides_missing", 2009, 3),
    ("grade_overrides_write_failed", 2010, 4),
    // ── 3000 처리(추출 · 임베딩) ─────────────────────────────────────
    ("extract_failed", 3001, 1),
    // 목록(--filelist)에 있는데 디스크에 없는 문서. 요청받고도 처리하지 못한
    // 것이라 추출 실패와 같은 급으로 본다(파이썬 판과 같은 번호·같은 종료코드).
    ("file_missing", 3004, 1),
    // 이 문서를 풀지 못했다(파일 문제) — 그 문서만 오류 레코드로 남기고 계속한다.
    ("decrypt_failed", 3005, 1),
    // 복호화 환경이 고장이다(PHP 확장 없음 등) — 다음 문서도 똑같이 실패하므로 멈춘다.
    // 설치를 점검해야 한다는 점에서 모델 로드 실패와 같은 급(2)이다.
    ("decrypt_unavailable", 3006, 2),
    ("model_load_failed", 3002, 2),
    ("embed_failed", 3003, 2),
    // ── 4000 출력 ────────────────────────────────────────────────────
    ("output_write_failed", 4001, 1),
    // 결과는 냈는데 등급 스트림(:mgrade)을 못 쓴 문서가 있다(설계: 등급스트림-기록-옵션-설계 §08).
    ("grade_stream_write_failed", 4002, 1),
    // ── 9000 그 밖 ───────────────────────────────────────────────────
    ("internal_error", 9001, 1),
];

/// `--json-errors` 를 받았는가. 프로세스 전체에 하나뿐인 상태다.
static JSON: AtomicBool = AtomicBool::new(false);
/// 이미 상태 줄을 냈는가. 실패해서 한 줄 냈는데 끝에서 또 내면 두 줄이 되어,
/// 마지막 줄만 읽는 쪽이 엉뚱한 것을 본다.
static DONE: AtomicBool = AtomicBool::new(false);
/// 이번 실행에서 다룬 문서 수 / 그중 못 읽은 수. `--dir` 의 '부분 실패'를 숫자로
/// 알려 주려고 담아 둔다. -1 이면 '셀 것이 없는 실행'(예: --check-rules).
static TOTAL: AtomicI64 = AtomicI64::new(-1);
static FAILED: AtomicI64 = AtomicI64::new(0);
/// 이번 실행에 쓴 정책 버전. 결과만 있고 '어떤 규칙으로 판정했는지'가 없으면
/// 반년 뒤에 재현할 수 없다 — 더 나쁜 것은 그새 규칙이 바뀐 줄 모르고 지금
/// 규칙으로 재현해 보고 "맞네" 하는 것이다. 실행 단위 정보라 상태 줄에 싣는다.
static VERSIONS: Mutex<Vec<(String, String)>> = Mutex::new(Vec::new());

/// 이번 실행이 '왜' 0 이 아닌 종료코드로 끝나는지 — 실행이 직접 적어 둔 이름·문장.
/// 종료코드만 보고 이름을 거꾸로 찾으면 exit 2 는 언제나 model_load_failed 가 되는
/// 식으로 엉뚱한 이름이 붙는다(한 종료코드에 이름이 여럿이다). 그래서 까닭을 아는
/// 자리가 적어 두고, 상태 줄은 그것을 먼저 쓴다.
static STATUS: Mutex<Option<(String, String)>> = Mutex::new(None);

/// 이번 실행이 끝나는 까닭을 적어 둔다(상태 줄의 kind·message 가 된다).
///
/// 표에 없는 이름이면 즉시 패닉한다(개발 중 오타) — `row()` 와 같은 원칙이다.
pub fn set_status(kind: &str, message: &str) {
    let _ = row(kind);
    if let Ok(mut st) = STATUS.lock() {
        *st = Some((kind.to_string(), message.to_string()));
    }
}

/// 이번 실행에 쓴 정책 버전을 기록한다(빈 값은 넣지 않는다).
pub fn set_versions(pairs: &[(&str, Option<String>)]) {
    if let Ok(mut v) = VERSIONS.lock() {
        for (k, val) in pairs {
            if let Some(s) = val {
                if !s.is_empty() {
                    v.push((k.to_string(), s.clone()));
                }
            }
        }
    }
}

/// 이번 실행의 문서 건수를 기록한다(분류가 끝나는 자리에서 한 번 부른다).
pub fn set_counts(total: i64, failed: i64) {
    TOTAL.store(total, Ordering::Relaxed);
    FAILED.store(failed, Ordering::Relaxed);
}

/// 표에서 한 줄 찾기. 없는 이름은 개발 중 오타이므로 즉시 패닉한다 —
/// 조용히 0 을 돌려주면 받는 쪽이 "성공"으로 읽는다.
fn row(kind: &str) -> (i64, i32) {
    ERRORS
        .iter()
        .find(|(k, _, _)| *k == kind)
        .map(|(_, c, e)| (*c, *e))
        .unwrap_or_else(|| panic!("오류 표에 없는 이름: {}", kind))
}

/// JSON 오류 출력을 켜고 끈다.
///
/// `--json-errors` 를 준 호출만 새 계약(stdout 에 JSON)을 받는다. 옵션이 없으면
/// 오늘과 똑같이 stderr 한 줄만 나간다 — 지금 이 CLI 를 쓰는 쪽의 stdout 파싱이
/// 깨지지 않도록 '선택'으로 둔다.
pub fn enable_json(on: bool) {
    JSON.store(on, Ordering::Relaxed);
}

/// 인자 목록에 `--json-errors` 가 있는가.
///
/// 인자 해석이 실패해도 JSON 으로 알려야 하므로, 파싱 전에 날것의 argv 를 훑는다.
pub fn wants_json<I: IntoIterator<Item = String>>(argv: I) -> bool {
    argv.into_iter().any(|a| a == "--json-errors")
}

/// 오류 이름 → 고유 번호.
#[allow(dead_code)]
pub fn code_of(kind: &str) -> i64 {
    row(kind).0
}

/// 오류 이름 → 프로세스 종료코드.
pub fn exit_of(kind: &str) -> i32 {
    row(kind).1
}

/// 오류 객체(JSON 문자열)를 만든다.
///
/// 출력과 분리해 두어 테스트가 "무엇을 낼 것인가"만 따로 확인할 수 있게 한다.
/// `path` 는 경로와 관련된 오류에만 넣는다 — 빈 문자열을 넣지 않는다. "경로가
/// 비었다"와 "경로와 무관하다"는 다른 뜻이기 때문이다.
pub fn error_json(kind: &str, message: &str, path: Option<&str>) -> String {
    let (code, _) = row(kind);
    // 여러 줄 안내는 한 줄로 눌러 담는다 — JSON 한 줄 계약을 지키려는 것이고,
    // 받는 쪽이 message 로 분기하지 않기로 했으므로 손실이 없다.
    let one_line = message
        .lines()
        .map(|l| l.trim())
        .filter(|l| !l.is_empty())
        .collect::<Vec<_>>()
        .join(" / ");
    let mut obj = serde_json::Map::new();
    obj.insert("code".into(), serde_json::json!(code));
    obj.insert("kind".into(), serde_json::json!(kind));
    obj.insert("message".into(), serde_json::json!(one_line));
    if let Some(p) = path {
        if !p.is_empty() {
            obj.insert("path".into(), serde_json::json!(p));
        }
    }
    serde_json::json!({ "error": serde_json::Value::Object(obj) }).to_string()
}

/// 오류 JSON 을 stdout 에 한 줄로 내보낸다(옵션이 꺼져 있으면 아무것도 안 한다).
///
/// 실패하면 결과가 없으므로 결과와 부딪히지 않고, 부르는 쪽은 stdout 만
/// 파싱하면 된다.
pub fn emit(kind: &str, message: &str, path: Option<&str>) {
    if JSON.load(Ordering::Relaxed) {
        println!("{}", error_json(kind, message, path));
        DONE.store(true, Ordering::Relaxed);
    }
}

/// 실행이 끝났음을 한 줄로 알리고 그 종료코드를 돌려준다(성공이어도 낸다).
///
/// 실패했을 때만 줄이 나가면, 부르는 쪽은 '줄이 없음'을 성공으로 읽어야 한다.
/// 그건 프로세스가 조용히 죽은 경우와 구분되지 않는다.
///
/// * 종료코드 0 → `code 0` · `kind success`
/// * 종료코드 1 → 결과는 있는데 못 읽은 문서가 있다 → `3001 extract_failed`
/// * 그 밖      → 표에서 이름을 찾아 낸다
///
/// `code` 는 종료코드와 늘 짝이 맞는다 — 둘 중 무엇으로 분기해도 결과가 같다.
/// 위치는 stdout 의 **마지막 줄**이다(실패일 때 이미 그 규약이고, `--format json`
/// 배열 앞에 객체를 끼우면 stdout 이 통째로 깨진다).
pub fn finish(code: i32, path: Option<&str>) -> i32 {
    if !JSON.load(Ordering::Relaxed) || DONE.load(Ordering::Relaxed) {
        return code;
    }
    println!("{}", status_object(code, path));
    code
}

/// 실행 결과 객체를 만든다(내보내지는 않는다).
///
/// 만들기와 내보내기를 가른 이유: 같은 객체를 결과 파일에도 넣어야 해서다.
/// 파일만 받아 나중에 읽는 쪽은 stdout 을 이미 흘려보낸 뒤라, 파일 자체가
/// "이 결과가 온전한가"를 말해 줘야 한다.
pub fn status_object(code: i32, path: Option<&str>) -> Value {
    let total = TOTAL.load(Ordering::Relaxed);
    let failed = FAILED.load(Ordering::Relaxed);
    // 실행이 까닭을 직접 적어 두었고 그 종료코드와 짝이 맞으면 그것을 쓴다.
    let explicit = STATUS.lock().ok().and_then(|st| st.clone())
        .filter(|(k, _)| code != 0 && exit_of(k) == code);
    let (kind, msg): (&str, String) = if code == 0 {
        ("success", String::new())
    } else if let Some((k, m)) = explicit.as_ref() {
        (k.as_str(), m.clone())
    } else if code == 1 && failed > 0 {
        // 결과 파일은 정상적으로 만들어졌다. 다만 사람이 볼 문서가 몇 건 있다.
        ("extract_failed", format!("{}건 중 {}건을 읽지 못했습니다", total, failed))
    } else {
        (ERRORS.iter().find(|(_, _, e)| *e == code).map(|(k, _, _)| *k)
            .unwrap_or("internal_error"), String::new())
    };
    let mut obj = serde_json::Map::new();
    obj.insert("code".into(), serde_json::json!(if kind == "success" { 0 } else { code_of(kind) }));
    obj.insert("kind".into(), serde_json::json!(kind));
    obj.insert("message".into(), serde_json::json!(msg));
    if let Some(p) = path {
        if !p.is_empty() { obj.insert("path".into(), serde_json::json!(p)); }
    }
    // 건수는 문서를 다룬 실행에만 붙는다(--check-rules 같은 모드에는 셀 것이 없다).
    if total >= 0 {
        obj.insert("total".into(), serde_json::json!(total));
        obj.insert("failed".into(), serde_json::json!(failed));
    }
    // 정책 버전 — 이 값이 있어야 나중에 같은 판정을 재현할 수 있다.
    if let Ok(v) = VERSIONS.lock() {
        for (k, val) in v.iter() {
            obj.insert(k.clone(), serde_json::json!(val));
        }
    }
    serde_json::json!({ "error": serde_json::Value::Object(obj) })
}

/// 치명적 오류 — 계약대로 내보내고 즉시 종료한다(반환하지 않는다).
///
/// 실패 자리에서 종료코드를 손으로 고르지 않게 한다. 이름(kind)만 고르면 번호와
/// 종료코드는 위의 표가 정한다 — 같은 상황에서 Rust 는 2, Python 은 3 을 내던
/// 원인을 없앤다.
pub fn fail(kind: &str, message: &str, path: Option<&str>) -> ! {
    // 여기서 프로세스가 끝난다 — 복호화해 둔 평문이 있으면 먼저 치운다
    // (설계: plan/엠파워v11-암호화문서-복호화연동-설계-20261012.html §8).
    crate::decrypt::cleanup_on_exit();
    emit(kind, message, path);
    let code = exit_of(kind);
    if quiet() {
        // 같은 내용이 이미 stdout 으로 나갔다. stderr 로 한 번 더 내면 부르는 쪽이
        // 두 갈래를 합쳐 받을 때(2>&1) JSON 뒤에 사람용 문장이 따라붙는다.
        // 로그 파일에는 그대로 남긴다 — 화면이 없는 배치에서 원인을 찾을 흔적이다.
        errlog::note(message);
        errlog::note(&format!("종료 코드 {} 로 중단합니다.", code));
        std::process::exit(code)
    }
    errlog::fail(message, code)
}

/// 사람이 읽는 알림을 stderr 로 낸다 — `--json-errors` 면 아무것도 하지 않는다.
///
/// `eprintln!` 을 그대로 쓰면 기계가 읽는 호출에서도 [전파]·[본문없음] 같은 줄이
/// 섞여 나간다. 그런 자리가 60곳이 넘어 하나씩 고치면 빠뜨리기 쉬우므로, 길목을
/// 하나로 모은다. 진행률(`[progress]`)과 요약(`[summary]`)은 기계가 읽는 줄이라
/// 이 매크로를 쓰지 않고 `eprintln!` 그대로 둔다.
#[macro_export]
macro_rules! note {
    ($($arg:tt)*) => {
        if !$crate::errcodes::quiet() { eprintln!($($arg)*); }
    };
}

/// 사람이 읽는 오류 안내를 stderr 로 내지 말아야 하는가.
///
/// `--json-errors` 를 준 호출은 "기계가 읽겠다"는 뜻이다. 그때는 같은 내용이
/// stdout 으로 이미 나가므로 stderr 로 되풀이하지 않는다.
/// 진행률·요약·경고는 오류가 아니므로 이 규칙과 무관하게 그대로 나간다.
pub fn quiet() -> bool {
    JSON.load(Ordering::Relaxed)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 표 자체가 성립하는가 — 번호도 이름도 겹치면 안 된다.
    ///
    /// 번호가 겹치면 받는 쪽의 분기가 두 뜻을 가지게 되고, 이름이 겹치면
    /// `row()` 가 먼저 만난 쪽을 조용히 고른다. 둘 다 눈으로는 안 보이는 사고다.
    #[test]
    fn 코드와_이름은_저마다_유일하다() {
        let mut codes: Vec<i64> = ERRORS.iter().map(|(_, c, _)| *c).collect();
        let n = codes.len();
        codes.sort_unstable();
        codes.dedup();
        assert_eq!(codes.len(), n, "중복된 code 가 있다");

        let mut kinds: Vec<&str> = ERRORS.iter().map(|(k, _, _)| *k).collect();
        kinds.sort_unstable();
        kinds.dedup();
        assert_eq!(kinds.len(), n, "중복된 kind 가 있다");
    }

    /// 종료코드는 지금까지의 약속(0~4) 안에 있어야 한다.
    ///
    /// 계약은 code 를 늘리는 것이지 종료코드를 늘리는 것이 아니다 — 셸 스크립트가
    /// 종료코드만 보고도 지금까지처럼 굴러가야 한다.
    #[test]
    fn 종료코드는_0에서_4_사이다() {
        for (kind, _, ex) in ERRORS {
            assert!((0..=4).contains(ex), "{} 의 종료코드가 범위 밖: {}", kind, ex);
        }
    }

    /// 백의 자리(갈래)와 종료코드가 서로 어긋나지 않는가.
    ///
    /// 받는 쪽에 "모르는 code 는 백의 자리로 뭉뚱그려 처리하라"고 안내했으므로,
    /// 갈래 안에서 뜻이 흔들리면 그 안내가 거짓말이 된다.
    #[test]
    fn 갈래별_종료코드가_일관된다() {
        for (kind, code, ex) in ERRORS {
            match code / 1000 {
                // 1000 인자·입력 — 부른 쪽 잘못이라 3. 단 1009 는 정책 위반(T11)이라 4.
                1 => assert!(*ex == 3 || *code == 1009, "{} 는 3 이어야 한다", kind),
                // 2000 정책 파일 — 없으면 3, 내용이 틀리면 4.
                2 => assert!(*ex == 3 || *ex == 4, "{} 가 이상하다", kind),
                _ => {}
            }
        }
    }

    /// path 는 있을 때만 넣는다 — 빈 값이면 키 자체가 없어야 한다.
    ///
    /// "경로가 비었다"와 "경로와 무관하다"는 다른 뜻이다. 빈 문자열을 넣으면
    /// 받는 쪽이 그 둘을 구분할 수 없다.
    #[test]
    fn path_는_있을_때만_들어간다() {
        let with = error_json("no_input", "대상 없음", Some("D:/x"));
        assert!(with.contains("\"path\":\"D:/x\""));
        assert!(!error_json("bad_args", "인자 오류", None).contains("path"));
        assert!(!error_json("bad_args", "인자 오류", Some("")).contains("path"));
    }

    /// 실행이 적어 둔 까닭(set_status)이 종료코드 역추적보다 먼저다.
    ///
    /// exit 2 에는 model_load_failed·embed_failed 두 이름이 있다. 역추적만 하면
    /// 언제나 앞의 것이 붙어, "0건 등록"이 "모델을 못 올렸다"로 보고됐다.
    /// 종료코드와 짝이 안 맞는 까닭은 쓰지 않는다(code 와 exit 가 어긋나면 안 된다).
    #[test]
    fn 적어둔_까닭이_역추적보다_먼저다() {
        set_status("embed_failed", "3건 중 한 건도 등록하지 못했습니다");
        let v = status_object(2, None);
        assert_eq!(v["error"]["code"], 3003);
        assert_eq!(v["error"]["kind"], "embed_failed");
        assert_eq!(v["error"]["message"], "3건 중 한 건도 등록하지 못했습니다");
        // 종료코드가 다르면(1) 적어 둔 까닭을 쓰지 않는다.
        let v1 = status_object(1, None);
        assert_ne!(v1["error"]["kind"], "embed_failed");
        // 성공은 언제나 success 다.
        assert_eq!(status_object(0, None)["error"]["kind"], "success");
        if let Ok(mut st) = STATUS.lock() { *st = None; }
    }

    /// 여러 줄 안내도 JSON 은 한 줄이어야 한다(부르는 쪽이 줄 단위로 읽는다).
    #[test]
    fn 여러줄_안내도_한_줄로_담는다() {
        let s = error_json("no_input", "첫 줄\n  둘째 줄\n\n셋째 줄", None);
        assert!(s.contains("첫 줄 / 둘째 줄 / 셋째 줄"), "{}", s);
        assert_eq!(s.lines().count(), 1);
    }
}
