//! 엠파워 v11 암호화 문서 복호화 연동 — 잠긴 파일을 풀어서 분류하고, 푼 파일은 바로 지운다.
//!
//! 설계: plan/엠파워v11-암호화문서-복호화연동-설계-20261012.html
//!
//! 엠파워 v11 저장소의 문서는 암호화(+압축)된 채 디스크에 놓여 있다. 그대로 읽으면
//! 분류기는 깨진 글자만 본다. 이 모듈은 분류 직전에 엠파워의 `removeenc.sh` 를 불러
//! 한 건을 임시 폴더에 풀고, 풀린 결과가 진짜 문서인지 확인한 뒤 넘겨준다.
//!
//! 지키는 원칙 세 가지:
//!   1. **한 번에 한 건만 풀려 있다.** 풀기–분류–지우기를 문서 단위로 묶는다(main.rs 의 루프).
//!   2. **"성공"을 믿지 않는다.** 도구는 틀린 열쇠에도 0 을 돌려주면서 원본과 같은 크기의
//!      무작위 바이트를 낼 수 있다(엠파워 removeenc.php 주석, 2026-09-29 실측).
//!      그것을 분류하면 기밀 문서가 낮은 등급으로 기록된다 — 앞머리(시그니처)를 본다.
//!   3. **열쇠와 임시 경로는 흔적을 남기지 않는다.** 로그·결과·오류 어디에도 쓰지 않는다.

use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

pub use crate::filelist::DecInfo;

/// 복호화 도구의 기본 위치(엠파워 v11 서버). `--decrypt-cmd` · 환경변수로 바꾼다.
pub const DEFAULT_CMD: &str = "/MOCOMSYS/MpowerScript/removeenc/removeenc.sh";
/// 복호화 한 건의 기본 시간 제한(초). 파서 시간 상한(--parser-timeout)과 같은 값이다.
pub const DEFAULT_TIMEOUT_SECS: u64 = 60;
/// removeenc.php 의 ERR_LOAD_EXTENSION — PHP 확장(Mpower 64File)을 못 읽었다.
/// 이 서버에서는 어떤 파일도 풀 수 없다는 뜻이라 '환경 문제'로 본다.
const RC_LOAD_EXTENSION: i32 = 56;

/// 지금 쓰고 있는 임시 폴더. Ctrl+C·패닉 때 치우려고 프로세스 전역에 적어 둔다.
static ACTIVE_ROOT: Mutex<Option<PathBuf>> = Mutex::new(None);

//------------------------------------------------------------------
// 복호화 실패 한 건
//=> 왜 못 풀었는지를 레코드의 error 칸에 그대로 옮길 수 있게 담는다.
//
// -필드: reason  = 사유 이름(removeenc_exit · timeout · empty_output · size_mismatch ·
//                  signature_mismatch · key_wrapped · info_incomplete · copy_failed · spawn_failed)
// -필드: rc      = removeenc 종료코드(부르지 않았으면 None)
// -필드: message = 사람이 읽는 문장(열쇠·임시 경로는 들어가지 않는다)
// -필드: fatal   = true 면 '환경 문제' — 다음 문서도 똑같이 실패하므로 실행을 멈춘다
// -필드: dec_size = 실패한 복호화 결과의 크기(바이트). 결과 파일이 있었을 때만 값이 있다 —
//                   실패 결과물은 곧바로 지우므로, 지우기 전에 재서 실패 로그로 넘긴다
//------------------------------------------------------------------
#[derive(Debug, Clone)]
pub struct DecError {
    pub reason: &'static str,
    pub rc: Option<i32>,
    pub message: String,
    pub fatal: bool,
    pub dec_size: Option<u64>,
}

impl DecError {
    fn file(reason: &'static str, rc: Option<i32>, message: String) -> Self {
        DecError { reason, rc, message, fatal: false, dec_size: None }
    }
}

//------------------------------------------------------------------
// 풀린 문서 한 건 — 임시 폴더와 그 안의 평문 파일
//=> 문서마다 폴더를 하나씩 따로 쓴다. 압축파일이면 그 폴더 안에 내부 문서가
//   펼쳐지므로, 폴더째 지우면 그 문서에서 나온 평문이 한 번에 사라진다.
//
// -필드: dir   = 이 문서 전용 임시 폴더(끝나면 통째로 지운다)
// -필드: file  = 풀린 평문 파일(dir 안)
// -필드: tool  = 복호화 도구를 실제로 불렀는가(둘 다 0 이라 복사만 했으면 false)
//------------------------------------------------------------------
#[derive(Debug)]
pub struct Decrypted {
    pub dir: PathBuf,
    pub file: PathBuf,
    pub tool: bool,
}

//------------------------------------------------------------------
// 복호화 작업장 — 도구 위치·임시 폴더·시간 제한을 쥐고 문서를 하나씩 푼다
//
// -필드: root    = 임시 폴더(<기준>/cso_dec_<pid>). 실행이 끝나면 통째로 지운다
// -필드: cmd     = 복호화 도구 경로
// -필드: timeout = 한 건의 시간 제한
// -필드: seq     = 문서 일련번호(임시 폴더·파일 이름에 쓴다)
//------------------------------------------------------------------
pub struct Session {
    root: PathBuf,
    cmd: PathBuf,
    timeout: Duration,
    seq: u64,
}

//------------------------------------------------------------------
// 복호화 도구 위치 정하기
//=> 우선순위: --decrypt-cmd > 환경변수 CSOCLASSIFY_DECRYPT_CMD > 기본값.
//
// -in: opt = --decrypt-cmd 로 받은 값(없으면 None)
//
// -out: PathBuf = 쓸 도구 경로(있는지는 여기서 보지 않는다 — preflight 가 본다)
// -out: error = 예외 없음
//------------------------------------------------------------------
pub fn resolve_cmd(opt: Option<&str>) -> PathBuf {
    if let Some(v) = opt.filter(|v| !v.trim().is_empty()) {
        return PathBuf::from(v);
    }
    match std::env::var("CSOCLASSIFY_DECRYPT_CMD") {
        Ok(v) if !v.trim().is_empty() => PathBuf::from(v),
        _ => PathBuf::from(DEFAULT_CMD),
    }
}

//------------------------------------------------------------------
// 임시 폴더의 기준 위치 정하기
//=> 우선순위: --decrypt-tmp > 환경변수 CSOCLASSIFY_DECRYPT_TMP > 시스템 임시 폴더.
//   메모리 디스크(/dev/shm)를 주면 풀린 원문이 물리 디스크에 닿지 않는다.
//
// -in: opt = --decrypt-tmp 로 받은 값(없으면 None)
//
// -out: PathBuf = 기준 폴더(이 아래에 cso_dec_<pid> 를 만든다)
// -out: error = 예외 없음
//------------------------------------------------------------------
pub fn resolve_tmp_base(opt: Option<&str>) -> PathBuf {
    if let Some(v) = opt.filter(|v| !v.trim().is_empty()) {
        return PathBuf::from(v);
    }
    match std::env::var("CSOCLASSIFY_DECRYPT_TMP") {
        Ok(v) if !v.trim().is_empty() => PathBuf::from(v),
        _ => std::env::temp_dir(),
    }
}

//------------------------------------------------------------------
// 도구가 있고 실행할 수 있는가 — 문서를 읽기 전에 한 번 본다
//=> 없으면 1만 건이 전부 같은 이유로 실패한 결과 파일이 만들어진다. 그보다는
//   한 건도 읽지 않고 끝내는 편이 낫다(설계 §5).
//
// -in: cmd = 복호화 도구 경로
//
// -out: Ok(()) = 쓸 수 있다
// -out: error = Err(사유 문장) — 파일이 없거나 실행 권한이 없다
//------------------------------------------------------------------
pub fn preflight(cmd: &Path) -> Result<(), String> {
    if !cmd.is_file() {
        return Err(format!(
            "복호화 도구를 찾을 수 없습니다: {}\n  \
             --decrypt-cmd 또는 환경변수 CSOCLASSIFY_DECRYPT_CMD 로 위치를 알려 주세요 \
             (엠파워 v11 서버의 기본 위치는 {}).", cmd.display(), DEFAULT_CMD));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = std::fs::metadata(cmd).map(|m| m.permissions().mode()).unwrap_or(0);
        if mode & 0o111 == 0 {
            return Err(format!("복호화 도구에 실행 권한이 없습니다: {}", cmd.display()));
        }
    }
    Ok(())
}

//------------------------------------------------------------------
// 로그에 남길 명령줄에서 열쇠 가리기
//=> 일반 로그는 실행 명령을 통째로 적는다. --mrk 뒤의 값은 **** 로 바꿔 적는다.
//   `--mrk=값` 모양도 함께 가린다.
//
// -in: args = 명령줄 인자들
//
// -out: Vec<String> = 열쇠 자리가 가려진 사본
// -out: error = 예외 없음
//------------------------------------------------------------------
pub fn mask_args(args: &[String]) -> Vec<String> {
    let mut out = Vec::with_capacity(args.len());
    let mut hide_next = false;
    for a in args {
        if hide_next {
            out.push("****".to_string());
            hide_next = false;
        } else if a == "--mrk" {
            out.push(a.clone());
            hide_next = true;
        } else if a.starts_with("--mrk=") {
            out.push("--mrk=****".to_string());
        } else {
            out.push(a.clone());
        }
    }
    out
}

//------------------------------------------------------------------
// 중단·패닉 때 임시 폴더 치우기
//=> Ctrl+C 처리기와 패닉 훅이 부른다. 풀린 원문이 디스크에 남지 않게 한다.
//
// -in: 없음
//
// -out: 없음
// -out: error = 예외 없음(못 지워도 조용히 넘어간다 — 이미 끝나는 길이다)
//------------------------------------------------------------------
pub fn cleanup_on_exit() {
    // 패닉 중에 잠금이 오염됐어도 값은 꺼낸다 — 지우는 일이 더 중요하다.
    let root = match ACTIVE_ROOT.lock() {
        Ok(mut g) => g.take(),
        Err(p) => p.into_inner().take(),
    };
    if let Some(r) = root {
        let _ = std::fs::remove_dir_all(r);
    }
}

/// 이름에서 확장자(소문자)를 뗀다. 영문·숫자 1~8자일 때만 확장자로 본다 —
/// 임시 파일 이름에 그대로 붙이므로 특수문자가 섞이면 안 된다.
fn safe_ext(name: &str) -> Option<String> {
    let base = name.rsplit(|c| c == '/' || c == '\\').next().unwrap_or(name);
    let (stem, ext) = base.rsplit_once('.')?;
    if stem.is_empty() || ext.is_empty() || ext.len() > 8
        || !ext.chars().all(|c| c.is_ascii_alphanumeric()) {
        return None;
    }
    Some(ext.to_ascii_lowercase())
}

/// 복호화 전 크기 검사에서 '압축'으로 볼 확장자 — 풀린 뒤 펼치기(MAX_ARCHIVE_BYTES)를 받는 종류.
/// docx·xlsx·pptx·hwpx 는 속이 zip 이어도 문서 1개라 넣지 않는다. egg·alz 는 펼치기를 지원하지 않아 뺀다.
/// [파이썬 판과의 관계] decrypt.py 의 PRE_ARCHIVE_EXTS 와 같아야 한다.
pub const PRE_ARCHIVE_EXTS: &[&str] = &["zip", "7z", "rar", "tar", "tgz", "gz", "bz2", "tbz2", "xz", "txz"];
/// '텍스트 계열'로 볼 확장자 — 풀린 뒤 추출기의 텍스트 상한(MAX_FILE_BYTES_TEXT)을 받는 종류.
/// [파이썬 판과의 관계] decrypt.py 의 PRE_TEXT_EXTS 와 같아야 한다.
pub const PRE_TEXT_EXTS: &[&str] = &["txt", "csv", "tsv", "json", "jsonl", "log", "md", "xml", "html", "htm"];

//------------------------------------------------------------------
// 복호화 전 크기 검사 — 상한을 인자로 받는 속함수(시험용으로 따로 둔다)
//=> 풀기 전에 '풀린 뒤 실제로 적용될 상한'을 미리 골라, 넘으면 풀지 않는다.
//   설계: plan/엠파워v11-복호화-크기상한-정합-설계-20261016.html §4. 파이썬 판 pre_size_limit_with 와 같다.
//    1) 판단 크기: 원래 크기(org_size)가 있으면 그것, 없으면 저장 파일 크기(stored_size)
//       — 원래 크기는 verify 가 풀린 결과와 같은지 보는 값이라 곧 '풀린 뒤 크기'다
//    2) 원래 이름의 확장자로 상한을 고른다: 압축 → 압축 상한, 텍스트 → 텍스트 상한,
//       그 밖·이름 없음 → 일반 상한(지금까지와 같음)
//    3) 고른 상한이 0 이면 검사하지 않는다(--no-size-limit 이 세 상한을 모두 0 으로 만든다)
//
// -in: name        = 원래 파일 이름(없으면 None)
// -in: org_size    = 엠파워 DB 의 원래 크기(바이트, 없으면 None)
// -in: stored_size = 저장 파일(.SMF) 크기(바이트)
// -in: lim_archive · lim_text · lim_file = 압축 · 텍스트 · 일반 상한(바이트, 0 이면 상한 없음)
//
// -out: None = 통과(풀어도 된다) / Some(DecError "size_limit") = 풀지 않는다
// -out: error = 없음
//------------------------------------------------------------------
pub fn pre_size_limit_with(name: Option<&str>, org_size: Option<u64>, stored_size: u64,
                           lim_archive: u64, lim_text: u64, lim_file: u64) -> Option<DecError> {
    // 원래 크기가 있으면 그것이 풀린 뒤 크기다 — zip_mode 압축·enc_mode 26 머리에 흔들리지 않는다.
    let (basis, size) = match org_size {
        Some(n) => ("org_size", n),
        None => ("stored_size", stored_size),
    };
    // 풀기 전에는 내용이 암호문이라 내용 판별을 못 한다 — 원래 이름의 확장자로 종류를 고른다.
    let ext = name.and_then(safe_ext);
    let is = |list: &[&str]| ext.as_deref().map_or(false, |e| list.contains(&e));
    let (limit, which, hint) = if is(PRE_ARCHIVE_EXTS) {
        (lim_archive, "MAX_ARCHIVE_BYTES", "--max-archive-mb")
    } else if is(PRE_TEXT_EXTS) {
        (lim_text, "MAX_FILE_BYTES_TEXT", "--max-file-mb-text")
    } else {
        (lim_file, "MAX_FILE_BYTES", "--max-file-mb")
    };
    // 0 은 '상한 없음' — --no-size-limit 이면 세 상한이 모두 0 이라 여기서 통과한다.
    if limit == 0 || size <= limit {
        return None;
    }
    Some(DecError::file("size_limit", None, format!(
        "복호화 전 크기가 상한을 넘어 풀지 않았습니다({} {}바이트 > {}바이트, {}) — {} 로 조정할 수 있습니다",
        basis, size, limit, which, hint)))
}

//------------------------------------------------------------------
// 복호화 전 크기 검사 — 지금 실행의 상한(limits)으로 판정하는 겉함수
//=> 분류 루프·--text-only 가 부르는 쪽이다. 상한은 --max-file-mb · --max-file-mb-text ·
//   --max-archive-mb · --no-size-limit 이 limits 에 새겨 둔 값을 그대로 읽는다.
//
// -in: name        = 원래 파일 이름(없으면 None)
// -in: org_size    = 엠파워 DB 의 원래 크기(없으면 None)
// -in: stored_size = 저장 파일(.SMF) 크기
//
// -out: None = 통과 / Some(DecError "size_limit") = 풀지 않는다
// -out: error = 없음
//------------------------------------------------------------------
pub fn pre_size_limit(name: Option<&str>, org_size: Option<u64>, stored_size: u64) -> Option<DecError> {
    pre_size_limit_with(name, org_size, stored_size, crate::limits::max_archive_bytes(),
                        crate::limits::max_file_bytes_text(), crate::limits::max_file_bytes())
}

//------------------------------------------------------------------
// 확장자별 '정상 문서라면 이렇게 시작한다'
//=> 틀린 열쇠로 풀린 무작위 바이트를 가려내는 자다(설계 §6). 시그니처가 없는
//   형식(txt·csv 등)은 None — 이 검사로는 가릴 수 없다.
//
// -in: ext = 소문자 확장자
//
// -out: Some((시그니처 바이트, 형식 이름)) / None
// -out: error = 예외 없음
//------------------------------------------------------------------
fn signature_for(ext: &str) -> Option<(&'static [u8], &'static str)> {
    match ext {
        "docx" | "xlsx" | "pptx" | "hwpx" | "zip" | "docm" | "xlsm" | "pptm" => Some((b"PK", "zip 계열(PK)")),
        "pdf" => Some((b"%PDF", "PDF")),
        "hwp" | "doc" | "xls" | "ppt" => Some((&[0xD0, 0xCF, 0x11, 0xE0], "OLE")),
        "7z" => Some((&[0x37, 0x7A, 0xBC, 0xAF], "7z")),
        "rar" => Some((b"Rar!", "rar")),
        "gz" | "tgz" => Some((&[0x1F, 0x8B], "gzip")),
        _ => None,
    }
}

//------------------------------------------------------------------
// 풀린 결과가 진짜 문서인가 — "성공"을 한 번 더 확인한다
//=> 도구가 0 을 돌려줘도 아래 셋을 본다(설계 §6).
//    1) 파일이 있고 0바이트가 아니다
//    2) 원래 크기(size)를 받았으면 크기가 같다
//    3) 앞머리(시그니처)가 확장자와 맞는다 — 틀린 열쇠를 잡는 것은 이 검사다
//       (무작위 바이트도 원본과 크기는 같아서 2)로는 못 잡는다)
//
// -in: file     = 풀린 파일
// -in: name     = 원래 파일 이름(확장자를 본다. 없으면 3)을 건너뛴다)
// -in: size     = 원래 크기(없으면 2)를 건너뛴다)
// -in: rc       = 도구 종료코드(오류 레코드에 실을 값)
// -in: allow_empty = 원본이 0바이트라 결과도 0바이트가 정상인 경우 true
//
// -out: Ok(()) = 믿을 수 있다
// -out: error = Err(DecError) — empty_output · size_mismatch · signature_mismatch
//------------------------------------------------------------------
pub fn verify(file: &Path, name: Option<&str>, size: Option<u64>, rc: Option<i32>,
              allow_empty: bool) -> Result<(), DecError> {
    let len = match std::fs::metadata(file) {
        Ok(m) if m.is_file() => m.len(),
        _ => return Err(DecError::file("empty_output", rc,
            "복호화 도구가 결과 파일을 만들지 않았습니다".into())),
    };
    if len == 0 && !allow_empty {
        return Err(DecError::file("empty_output", rc,
            "복호화 결과가 0바이트입니다".into()));
    }
    if let Some(want) = size {
        if want != len {
            return Err(DecError::file("size_mismatch", rc, format!(
                "복호화 결과의 크기가 원래 크기와 다릅니다(원래 {}바이트 · 결과 {}바이트)", want, len)));
        }
    }
    if len == 0 {
        return Ok(());
    }
    if let Some((sig, kind)) = name.and_then(safe_ext).as_deref().and_then(signature_for) {
        let mut head = vec![0u8; sig.len()];
        let ok = std::fs::File::open(file)
            .and_then(|mut f| f.read_exact(&mut head))
            .is_ok() && head == sig;
        if !ok {
            return Err(DecError::file("signature_mismatch", rc, format!(
                "{} 파일인데 앞머리가 {} 형식이 아닙니다 — 열쇠(mrk)와 모드가 맞는지 확인하세요",
                name.and_then(safe_ext).unwrap_or_default(), kind)));
        }
    }
    Ok(())
}

/// 주인 프로세스가 아직 살아 있는가. 리눅스에서만 안다(/proc). 다른 OS 에서는
/// 모른다고 답해(=살아 있다) 남의 폴더를 지우지 않는다.
fn pid_alive(pid: u32) -> bool {
    if cfg!(target_os = "linux") {
        Path::new(&format!("/proc/{}", pid)).exists()
    } else {
        true
    }
}

impl Session {
    //--------------------------------------------------------------
    // 작업장 열기
    //=> 임시 폴더를 만들고, 강제 종료(kill -9)로 남은 옛 폴더를 치운다.
    //    1) 기준 폴더 아래에 cso_dec_<pid> 를 만든다(유닉스는 0700)
    //    2) 주인 프로세스가 죽은 cso_dec_* 폴더를 찾아 지운다(리눅스)
    //    3) 중단·패닉 때 치울 수 있게 전역에 적어 둔다
    //
    // -in: cmd          = 복호화 도구 경로
    // -in: tmp_base     = 임시 폴더의 기준 위치
    // -in: timeout_secs = 한 건의 시간 제한(초). 0 이면 제한 없음
    //
    // -out: Ok(Session)
    // -out: error = Err(사유 문장) — 임시 폴더를 만들지 못했다(환경 문제)
    //--------------------------------------------------------------
    pub fn open(cmd: PathBuf, tmp_base: &Path, timeout_secs: u64) -> Result<Session, String> {
        let me = std::process::id();
        // 강제 종료 뒤에는 지울 기회가 없었다 — 다음 실행이 대신 치운다(설계 §8).
        if let Ok(rd) = std::fs::read_dir(tmp_base) {
            for e in rd.flatten() {
                let n = e.file_name().to_string_lossy().into_owned();
                if let Some(pid) = n.strip_prefix("cso_dec_").and_then(|p| p.parse::<u32>().ok()) {
                    if pid != me && !pid_alive(pid) {
                        let _ = std::fs::remove_dir_all(e.path());
                    }
                }
            }
        }
        let root = tmp_base.join(format!("cso_dec_{}", me));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).map_err(|e| format!(
            "복호화 임시 폴더를 만들 수 없습니다: {} ({})", root.display(), e))?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            // 같은 서버의 다른 계정이 풀린 원문을 읽지 못하게 한다.
            let _ = std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o700));
        }
        if let Ok(mut g) = ACTIVE_ROOT.lock() {
            *g = Some(root.clone());
        }
        Ok(Session { root, cmd, timeout: Duration::from_secs(timeout_secs), seq: 0 })
    }

    /// 임시 폴더 뿌리 — 어떤 경로가 '풀린 파일'인지 가릴 때 쓴다(로그에 임시 경로를 안 쓰려고).
    pub fn root(&self) -> &Path {
        &self.root
    }

    //--------------------------------------------------------------
    // 문서 한 건 풀기
    //=> 저장 파일을 이 문서 전용 임시 폴더에 평문으로 만든다.
    //    1) 폴더 <root>/<일련번호>/ 를 만들고, 파일 이름은 <일련번호>.<확장자> 로 한다
    //       (원래 이름을 쓰지 않는다 — 이름 자체가 민감할 수 있고 특수문자로 경로가 깨진다)
    //    2) 둘 다 0 이거나 원본이 0바이트면 복사만 한다(엠파워 mpowerDecrypt 와 같다)
    //    3) 아니면 도구를 부르고, 종료코드와 결과를 확인한다
    //    4) 실패하면 만든 폴더를 지우고 사유를 돌려준다
    //
    // -in: src  = 저장된(암호화된) 파일
    // -in: info = 열쇠와 모드
    // -in: name = 원래 파일 이름(확장자 판별·시그니처 검사용. 없어도 된다)
    // -in: size = 원래 크기(없어도 된다)
    //
    // -out: Ok(Decrypted) = 풀린 파일. 다 쓰면 release() 로 지운다
    // -out: error = Err(DecError) — fatal=true 면 환경 문제라 실행을 멈춰야 한다
    //--------------------------------------------------------------
    pub fn decrypt(&mut self, src: &Path, info: &DecInfo, name: Option<&str>, size: Option<u64>)
        -> Result<Decrypted, DecError> {
        self.seq += 1;
        let dir = self.root.join(format!("{:06}", self.seq));
        if let Err(e) = std::fs::create_dir_all(&dir) {
            return Err(DecError { reason: "tmp_failed", rc: None, fatal: true, dec_size: None,
                message: format!("복호화 임시 폴더를 만들 수 없습니다({})", e) });
        }
        // 확장자는 원래 이름에서, 없으면 저장 경로에서 가져온다.
        let ext = name.and_then(safe_ext)
            .or_else(|| safe_ext(&src.to_string_lossy()));
        let file = match &ext {
            Some(x) => dir.join(format!("{:06}.{}", self.seq, x)),
            None => dir.join(format!("{:06}", self.seq)),
        };
        let src_len = std::fs::metadata(src).map(|m| m.len()).unwrap_or(0);
        let result = if info.is_plain() || src_len == 0 {
            // 암호화도 압축도 안 된 파일(또는 빈 파일) — 도구를 부르지 않는다.
            // removeenc.php 는 -k 가 비면 인자 오류(254)로 끝나므로, 부르면
            // 멀쩡한 파일이 실패로 기록된다(설계 §12 확인 결과 ①).
            match std::fs::copy(src, &file) {
                Ok(_) => verify(&file, name, size, None, src_len == 0).map(|_| false),
                Err(e) => Err(DecError::file("copy_failed", None,
                    format!("저장 파일을 읽지 못했습니다({})", e))),
            }
        } else {
            self.run_tool(src, &file, info)
                .and_then(|rc| verify(&file, name, size, Some(rc), false).map(|_| true))
        };
        match result {
            Ok(tool) => {
                #[cfg(unix)]
                {
                    use std::os::unix::fs::PermissionsExt;
                    let _ = std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o600));
                }
                Ok(Decrypted { dir, file, tool })
            }
            Err(mut e) => {
                // 지우기 전에 결과 크기를 재 둔다 — 실패 로그에서 원래 크기와 견줘 볼 수 있게.
                e.dec_size = std::fs::metadata(&file).ok().map(|m| m.len());
                // 실패한 결과물(무작위 바이트일 수 있다)을 남기지 않는다.
                let _ = std::fs::remove_dir_all(&dir);
                Err(e)
            }
        }
    }

    //--------------------------------------------------------------
    // 복호화 도구 실행
    //=> removeenc.sh -s <원본> -t <대상> -k <열쇠> -e <ENC_MODE> -z <ZIP_MODE>
    //    1) 셸을 거치지 않고 인자를 낱개로 넘긴다 — 파일 이름의 공백·따옴표·$( ) 가
    //       명령으로 해석될 길이 없다
    //    2) 자식의 stdout·stderr 를 가로챈다 — removeenc.sh 가 종료코드 숫자를 화면에도
    //       찍는데, 그대로 흘리면 우리 결과 JSON 에 섞인다
    //    3) 시간 제한을 넘으면 자식을 '무리째' 죽인다 — removeenc.sh 는 bash 가 php 를
    //       띄우는 구조라, bash 만 죽이면 php 가 살아남아 출력 파이프를 붙잡고 있다
    //    4) 성공 여부는 프로세스 종료코드로만 판단한다
    //
    // -in: src  = 저장 파일
    // -in: tgt  = 평문을 쓸 임시 파일
    // -in: info = 열쇠와 모드
    //
    // -out: Ok(0) = 도구가 성공이라고 답했다(결과 확인은 verify 가 한다)
    // -out: error = Err(DecError) — spawn_failed(환경) · timeout · removeenc_exit
    //--------------------------------------------------------------
    fn run_tool(&self, src: &Path, tgt: &Path, info: &DecInfo) -> Result<i32, DecError> {
        let mut cmd = Command::new(&self.cmd);
        cmd.arg("-s").arg(src)
            .arg("-t").arg(tgt)
            .arg("-k").arg(&info.mrk)
            .arg("-e").arg(&info.enc_mode)
            .arg("-z").arg(&info.zip_mode)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(unix)]
        {
            // 자식을 새 프로세스 무리로 띄운다 — 시간 초과 때 손자(php)까지 한 번에 끝내려는 것이다.
            use std::os::unix::process::CommandExt;
            cmd.process_group(0);
        }
        let mut child = cmd
            .spawn()
            .map_err(|e| DecError {
                // 도구 자체를 못 띄웠다 — 다음 문서도 똑같이 실패한다.
                reason: "spawn_failed", rc: None, fatal: true, dec_size: None,
                message: format!("복호화 도구를 실행하지 못했습니다: {} ({})", self.cmd.display(), e),
            })?;
        // 출력은 따로 읽는다 — 안 읽으면 자식이 파이프가 차서 멈출 수 있다.
        let out_h = child.stdout.take().map(|mut s| std::thread::spawn(move || {
            let mut b = Vec::new();
            let _ = s.read_to_end(&mut b);
            b
        }));
        let err_h = child.stderr.take().map(|mut s| std::thread::spawn(move || {
            let mut b = Vec::new();
            let _ = s.read_to_end(&mut b);
            b
        }));
        let t0 = Instant::now();
        let status = loop {
            match child.try_wait() {
                Ok(Some(st)) => break Some(st),
                Ok(None) => {
                    if !self.timeout.is_zero() && t0.elapsed() >= self.timeout {
                        kill_tree(child.id());
                        let _ = child.kill();
                        let _ = child.wait();
                        break None;
                    }
                    std::thread::sleep(Duration::from_millis(10));
                }
                Err(_) => break None,
            }
        };
        let status = match status {
            Some(s) => s,
            // 시간 초과면 출력을 기다리지 않는다 — 혹시 살아남은 손자가 파이프를 붙잡고
            // 있으면 읽기 스레드가 끝나지 않는다. 스레드는 파이프가 닫힐 때 스스로 끝난다.
            None => return Err(DecError::file("timeout", None, format!(
                "복호화가 시간 제한({}초)을 넘어 중단했습니다", self.timeout.as_secs()))),
        };
        let stdout = out_h.and_then(|h| h.join().ok()).unwrap_or_default();
        let stderr = err_h.and_then(|h| h.join().ok()).unwrap_or_default();
        let rc = status.code().unwrap_or(-1);
        if rc == 0 {
            return Ok(0);
        }
        // 도구가 남긴 말의 첫 줄을 사유에 붙인다. removeenc 는 열쇠를 출력하지 않지만,
        // 혹시 모를 경우를 위해 열쇠 글자가 들어 있으면 가린다.
        let said = first_message_line(&stdout, &stderr).replace(&info.mrk, "****");
        let hint = rc_hint(rc);
        let mut msg = format!("복호화 도구가 실패했습니다(종료코드 {}{})", rc,
                              if hint.is_empty() { String::new() } else { format!(" · {}", hint) });
        if !said.is_empty() {
            msg.push_str(&format!(" — {}", said));
        }
        Err(DecError { reason: "removeenc_exit", rc: Some(rc), message: msg, dec_size: None,
                       fatal: rc == RC_LOAD_EXTENSION })
    }

    //--------------------------------------------------------------
    // 다 쓴 문서 치우기
    //=> 그 문서 전용 폴더를 통째로 지운다(압축에서 펼친 내부 문서 포함).
    //
    // -in: dir = Decrypted.dir
    //
    // -out: 없음
    // -out: error = 예외 없음(못 지우면 close 가 폴더째 한 번 더 지운다)
    //--------------------------------------------------------------
    pub fn release(&self, dir: &Path) {
        // 뿌리 밖의 경로는 건드리지 않는다 — 실수로 남의 폴더를 지우지 않게.
        if dir.starts_with(&self.root) && dir != self.root {
            let _ = std::fs::remove_dir_all(dir);
        }
    }

    //--------------------------------------------------------------
    // 작업장 닫기
    //=> 임시 폴더를 통째로 지우고 전역 표식을 비운다.
    //
    // -in: 없음
    //
    // -out: 없음
    // -out: error = 예외 없음
    //--------------------------------------------------------------
    pub fn close(&self) {
        let _ = std::fs::remove_dir_all(&self.root);
        if let Ok(mut g) = ACTIVE_ROOT.lock() {
            *g = None;
        }
    }
}

//------------------------------------------------------------------
// 자식 프로세스를 손자까지 한꺼번에 끝내기
//=> removeenc.sh 는 bash 가 php 를 띄운다. bash 만 죽이면 php 가 남아 계속 돌고,
//   출력 파이프를 붙잡고 있어 부른 쪽이 php 가 끝날 때까지 기다리게 된다.
//    1) 유닉스: 자식을 새 무리로 띄웠으므로 그 무리(pgid = 자식 pid)에 KILL 을 보낸다
//    2) 윈도우: taskkill /T 로 자식과 그 아래를 모두 끝낸다
//   외부 명령(kill · taskkill)을 쓰는 까닭: 무리에 신호를 보내려면 libc 가 필요한데,
//   이 한 가지 때문에 의존을 늘리지 않으려는 것이다. 둘 다 OS 에 기본으로 있다.
//
// -in: pid = 자식 프로세스 번호
//
// -out: 없음
// -out: error = 예외 없음(이미 끝난 프로세스여도 조용히 넘어간다)
//------------------------------------------------------------------
fn kill_tree(pid: u32) {
    let mut c = if cfg!(windows) {
        let mut c = Command::new("taskkill");
        c.args(["/T", "/F", "/PID", &pid.to_string()]);
        c
    } else {
        let mut c = Command::new("kill");
        c.args(["-KILL", "--", &format!("-{}", pid)]);
        c
    };
    let _ = c.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).status();
}

/// 도구 출력에서 사람이 읽을 첫 줄을 고른다. removeenc.sh 가 끝에 찍는 '종료코드
/// 숫자만 있는 줄'은 건너뛴다.
fn first_message_line(stdout: &[u8], stderr: &[u8]) -> String {
    for buf in [stderr, stdout] {
        let text = String::from_utf8_lossy(buf);
        for line in text.lines() {
            let t = line.trim();
            if t.is_empty() || t.chars().all(|c| c.is_ascii_digit()) || t.starts_with("[Usage]") {
                continue;
            }
            return t.chars().take(200).collect();
        }
    }
    String::new()
}

/// removeenc.php 의 종료코드가 뜻하는 것(자주 보는 것만). 번호는 결과에 그대로 싣고,
/// 이 말은 사람이 바로 알아보게 덧붙이는 것이다.
fn rc_hint(rc: i32) -> &'static str {
    match rc {
        4 => "원본 파일을 열지 못함",
        5 => "대상 파일을 열지 못함",
        9 => "복호화 실패",
        10 => "압축 해제 실패",
        14 => "MRK 문제(KEK 로 감싼 값일 수 있음)",
        56 => "PHP 확장 Mpower 64File 을 읽지 못함",
        254 => "인자 오류",
        _ => "",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // 복호화 전 크기 상한 — 확장자로 상한을 고르고, 원래 크기가 있으면 그것으로 본다.
    // 파이썬 판 test_복호화_전_크기_상한_판정 과 같은 표다(상한: 압축 500 · 텍스트 300 · 일반 100).
    #[test]
    fn 복호화_전_크기_상한_판정() {
        let cases: &[(Option<&str>, Option<u64>, u64, Option<&str>, Option<&str>)] = &[
            (Some("묶음.7z"), None, 400, None, None),
            (Some("묶음.7z"), None, 600, Some("MAX_ARCHIVE_BYTES"), Some("stored_size")),
            (Some("자료.csv"), None, 200, None, None),
            (Some("자료.csv"), None, 400, Some("MAX_FILE_BYTES_TEXT"), Some("stored_size")),
            (Some("보고서.hwp"), None, 200, Some("MAX_FILE_BYTES"), Some("stored_size")),
            (Some("보고서.docx"), None, 200, Some("MAX_FILE_BYTES"), Some("stored_size")),
            (Some("이상한.egg"), None, 200, Some("MAX_FILE_BYTES"), Some("stored_size")),
            (None, None, 200, Some("MAX_FILE_BYTES"), Some("stored_size")),
            (Some("보고서.hwp"), Some(200), 50, Some("MAX_FILE_BYTES"), Some("org_size")),
            (Some("보고서.hwp"), Some(100), 132, None, None),
        ];
        for (name, org, stored, which, basis) in cases {
            let r = pre_size_limit_with(*name, *org, *stored, 500, 300, 100);
            match which {
                None => assert!(r.is_none(), "{:?} 는 통과해야 한다", name),
                Some(w) => {
                    let e = r.unwrap_or_else(|| panic!("{:?} 는 걸려야 한다", name));
                    assert_eq!(e.reason, "size_limit");
                    assert!(e.message.contains(w) && e.message.contains(basis.unwrap()), "{}", e.message);
                }
            }
        }
        // --no-size-limit(세 상한 0)이면 앞 상한도 꺼진다.
        for name in [Some("a.7z"), Some("a.txt"), Some("a.hwp"), None] {
            assert!(pre_size_limit_with(name, Some(1 << 40), 1 << 40, 0, 0, 0).is_none());
        }
    }

    fn tmp(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("csors_dec_unit_{}_{}", tag, std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn 열쇠는_로그용_명령줄에서_가려진다() {
        let a: Vec<String> = ["--file", "x", "--mrk", "SECRET", "--enc-mode", "3", "--mrk=SECRET2"]
            .iter().map(|s| s.to_string()).collect();
        let m = mask_args(&a);
        assert_eq!(m, vec!["--file", "x", "--mrk", "****", "--enc-mode", "3", "--mrk=****"]);
        assert!(!m.join(" ").contains("SECRET"));
    }

    #[test]
    fn 열쇠는_디버그_출력에_찍히지_않는다() {
        let i = DecInfo { mrk: "SECRETKEY".into(), enc_mode: "3".into(), zip_mode: "0".into() };
        assert!(!format!("{:?}", i).contains("SECRETKEY"));
    }

    #[test]
    fn 확장자는_안전한_글자만_받는다() {
        assert_eq!(safe_ext("급여 대장 (최종).XLSX").as_deref(), Some("xlsx"));
        assert_eq!(safe_ext("/a/b/F_872c.SMF").as_deref(), Some("smf"));
        assert_eq!(safe_ext("이름없음"), None);
        assert_eq!(safe_ext("a.b$(x)"), None);
        assert_eq!(safe_ext(".bashrc"), None);
    }

    #[test]
    fn 틀린_열쇠로_풀린_무작위_바이트를_시그니처로_잡는다() {
        let d = tmp("sig");
        let f = d.join("x.docx");
        // 크기는 원본과 같지만 앞머리가 PK 가 아니다 — 크기 검사로는 못 잡는 경우.
        std::fs::write(&f, [0xDAu8, 0x6E, 0x3C, 0x8C, 1, 2, 3, 4]).unwrap();
        let e = verify(&f, Some("워드문서.docx"), Some(8), Some(0), false).unwrap_err();
        assert_eq!(e.reason, "signature_mismatch");
        assert!(!e.fatal);
        // 진짜 zip 머리면 통과.
        std::fs::write(&f, b"PK\x03\x04abcd").unwrap();
        assert!(verify(&f, Some("워드문서.docx"), Some(8), Some(0), false).is_ok());
        // 시그니처가 없는 형식(txt)은 이 검사를 건너뛴다.
        assert!(verify(&f, Some("메모.txt"), None, Some(0), false).is_ok());
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn 빈_결과와_크기_불일치를_잡는다() {
        let d = tmp("size");
        let f = d.join("x.pdf");
        assert_eq!(verify(&f, None, None, Some(0), false).unwrap_err().reason, "empty_output");
        std::fs::write(&f, b"").unwrap();
        assert_eq!(verify(&f, None, None, Some(0), false).unwrap_err().reason, "empty_output");
        assert!(verify(&f, None, Some(0), None, true).is_ok());
        std::fs::write(&f, b"%PDF-1.7").unwrap();
        assert_eq!(verify(&f, Some("a.pdf"), Some(99), Some(0), false).unwrap_err().reason, "size_mismatch");
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn 둘_다_0이면_도구_없이_복사한다() {
        let d = tmp("plain");
        let src = d.join("F_e57f.SMF");
        std::fs::write(&src, b"PK\x03\x04zipbody").unwrap();
        // 도구 경로는 없는 파일이다 — 부르면 실패해야 하는데, 부르지 않으므로 성공한다.
        let mut s = Session::open(d.join("no_such_tool"), &d, 5).unwrap();
        let info = DecInfo { mrk: String::new(), enc_mode: "0".into(), zip_mode: "0".into() };
        let got = s.decrypt(&src, &info, Some("압축묶음.zip"), Some(11)).unwrap();
        assert!(!got.tool);
        assert_eq!(got.file.extension().and_then(|e| e.to_str()), Some("zip"));
        assert_eq!(std::fs::read(&got.file).unwrap(), b"PK\x03\x04zipbody");
        s.release(&got.dir);
        assert!(!got.dir.exists());
        s.close();
        assert!(!s.root().exists());
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn 도구를_못_띄우면_환경_문제로_본다() {
        let d = tmp("spawn");
        let src = d.join("enc.bin");
        std::fs::write(&src, b"xxxx").unwrap();
        let mut s = Session::open(d.join("no_such_tool"), &d, 5).unwrap();
        let info = DecInfo { mrk: "K".into(), enc_mode: "3".into(), zip_mode: "0".into() };
        let e = s.decrypt(&src, &info, Some("a.docx"), None).unwrap_err();
        assert_eq!(e.reason, "spawn_failed");
        assert!(e.fatal);
        s.close();
        let _ = std::fs::remove_dir_all(&d);
    }
}
