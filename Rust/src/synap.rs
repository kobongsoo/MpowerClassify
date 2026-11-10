//! 사이냅 snf_exe 찾기 — 배포 폴더·환경변수 규칙(이 프로그램 전용)
//!
//! 사이냅 실행(snf_extract)은 docparse 라이브러리로 옮겼다. '어디서 찾을지'는
//! 이 프로그램의 배포 규칙(--synap-path · CSOCLASSIFY_SYNAP_DIR · exe 옆 synap/)이라
//! 여기 남긴다. 파이썬 판 `resources.synap_exe_path` 와 같은 차례다.

use std::path::{Path, PathBuf};

pub use crate::docparse::synap::SNF_TIMEOUT_SECS;

//------------------------------------------------------------------
// snf_exe 한 건의 제한 시간(초)
//=> 환경변수 CSOCLASSIFY_SNF_TIMEOUT 이 있으면 그 값, 없으면 60. 파이썬 판과 같은 이름.
//   0 이면 제한 없음.
//
// -in: 없음
//
// -out: u64 = 초
// -out: error = 없음(숫자가 아니면 기본값)
//------------------------------------------------------------------
pub fn snf_timeout_secs() -> u64 {
    std::env::var("CSOCLASSIFY_SNF_TIMEOUT").ok()
        .and_then(|v| v.trim().parse().ok()).unwrap_or(SNF_TIMEOUT_SECS)
}

//------------------------------------------------------------------
// 사이냅 실행파일 이름 (OS 별)
//=> Windows 는 snf_exe.exe, 그 밖은 확장자 없는 snf_exe 다. 같은 제품이라 인자는 같다.
//
// -in: 없음
//
// -out: &str = 지금 OS 의 실행파일 이름
// -out: error = 없음
//------------------------------------------------------------------
pub fn exe_name() -> &'static str {
    if cfg!(windows) { "snf_exe.exe" } else { "snf_exe" }
}

//------------------------------------------------------------------
// 사이냅 OS 별 하위폴더 이름
//=> 한 배포 트리에 두 OS 바이너리를 함께 둘 수 있게 synap/windows · synap/linux 로 나눈다.
//
// -in: 없음
//
// -out: &str = "windows" 또는 "linux"
// -out: error = 없음
//------------------------------------------------------------------
fn os_subdir() -> &'static str {
    if cfg!(windows) { "windows" } else { "linux" }
}

//------------------------------------------------------------------
// 실행할 수 있는 파일인가
//=> 파일이 있고, 유닉스면 실행 비트가 하나라도 서 있어야 한다. 윈도우에는 실행 비트가 없다.
//
// -in: p = 살펴볼 경로
//
// -out: bool
// -out: error = 없음
//------------------------------------------------------------------
fn is_runnable(p: &Path) -> bool {
    if !p.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        return std::fs::metadata(p).map(|m| m.permissions().mode() & 0o111 != 0).unwrap_or(false);
    }
    #[cfg(not(unix))]
    {
        true
    }
}

//------------------------------------------------------------------
// snf_exe 찾기 — 파이썬 resources.synap_exe_path 와 같은 차례
//=> 사이냅 모드로 도는 실행이 시작할 때 한 번 부른다. 못 찾으면 문서를 한 건도 읽지
//   않고 멈추도록(1016 synap_missing) 찾아본 경로를 모두 돌려준다.
//    1) --synap-path 를 줬으면 그 파일만 본다(찾기를 건너뛴다)
//    2) CSOCLASSIFY_SYNAP_DIR → exe 옆 synap/ → exe 옆 resources/synap/
//    3) 각 폴더에서 OS 하위폴더(windows|linux)를 먼저, 없으면 그 폴더 바로 아래
//
// -in: explicit = --synap-path 값(없으면 None)
//
// -out: Ok(PathBuf) = 쓸 snf_exe 경로
// -out: error = Err(사람이 읽는 문장 — 찾아본 경로 · 실행 권한 문제를 담는다)
//------------------------------------------------------------------
pub fn find_snf(explicit: Option<&str>) -> Result<PathBuf, String> {
    if let Some(p) = explicit {
        let pb = PathBuf::from(p);
        if !pb.is_file() {
            return Err(format!("--synap-path 가 가리키는 사이냅 실행파일이 없습니다: {}", p));
        }
        if !is_runnable(&pb) {
            return Err(format!("사이냅 실행파일에 실행 권한이 없습니다: {}", p));
        }
        return Ok(pb);
    }
    let mut dirs: Vec<PathBuf> = vec![];
    if let Ok(d) = std::env::var("CSOCLASSIFY_SYNAP_DIR") {
        if !d.trim().is_empty() {
            dirs.push(PathBuf::from(d));
        }
    }
    if let Some(ed) = std::env::current_exe().ok().and_then(|e| e.parent().map(|p| p.to_path_buf())) {
        dirs.push(ed.join("synap"));
        dirs.push(ed.join("resources").join("synap"));
    }
    let mut tried = vec![];
    for d in &dirs {
        for p in [d.join(os_subdir()).join(exe_name()), d.join(exe_name())] {
            if p.is_file() {
                // 있는데 실행이 안 되면 다음 후보로 넘어가지 않고 바로 알린다 —
                // 넘어가면 '없다'는 엉뚱한 사유로 멈춘다.
                if !is_runnable(&p) {
                    return Err(format!("사이냅 실행파일에 실행 권한이 없습니다: {} (chmod +x)", p.display()));
                }
                return Ok(p);
            }
            tried.push(p.display().to_string());
        }
    }
    Err(format!("사이냅 실행파일({})을 찾을 수 없습니다 — 찾아본 곳: {} \
                 (--synap-path 또는 환경변수 CSOCLASSIFY_SYNAP_DIR 로 알려 주세요)",
                exe_name(), tried.join(" , ")))
}

#[cfg(test)]
mod tests {
    use super::*;

    // --synap-path 가 없는 파일을 가리키면 바로 알린다.
    #[test]
    fn 없는_경로는_알린다() {
        let e = find_snf(Some("/없는/곳/snf_exe")).unwrap_err();
        assert!(e.contains("--synap-path"), "{}", e);
    }
}
