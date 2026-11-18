//! 빌드 스크립트 — pdfium · docparse(문서파서) · moaipii(개인정보 검출) 동적 라이브러리를 실행파일 옆으로 자동 복사한다.
//!
//! [왜 필요한가] `extract.rs::pdfium_lib_path()` 의 탐색 순서는
//! `CSO_PDFIUM` 환경변수 → **exe 옆** → 시스템이다. 개발 빌드(`target/debug`)에는
//! 이 라이브러리를 놓아 주는 사람이 없어서, 아무 설정 없이 실행하면 PDF 가 전부
//! 추출 실패로 떨어진다. 그런데 그 실패는 예외가 아니라 `extract_text` 가 `None` 을
//! 돌려주는 조용한 경로라, 결과만 보면 "이 PDF 들은 원래 안 읽히는 문서"처럼 보인다
//! — 원인이 드러나지 않는 종류의 고장이라 사람이 오래 헤맨다.
//!
//! 그래서 빌드할 때마다 배포본에 들어 있는 정본을 exe 옆으로 옮겨 둔다.
//! `cargo clean` 으로 target/ 이 통째로 지워져도 다음 빌드에서 저절로 복구된다.
//!
//! [실패해도 빌드를 깨지 않는다] 원본이 없는 환경(배포본을 안 받은 새 clone, CI)
//! 에서도 빌드 자체는 되어야 한다. 못 찾거나 못 복사하면 `cargo:warning` 만 남기고
//! 넘어간다 — 그때는 `CSO_PDFIUM` 으로 직접 지정하면 된다.

use std::path::{Path, PathBuf};

//------------------------------------------------------------------
// OUT_DIR → 실행파일이 놓이는 폴더
//=> cargo 는 빌드 산출 폴더를 build.rs 에 직접 알려 주지 않는다. 대신
//   OUT_DIR 이 `target/<프로파일>/build/<패키지>-<해시>/out` 형태라, 세 단계
//   올라가면 exe 가 놓이는 `target/<프로파일>` 이 된다.
//   (`--target <triple>` 을 준 경우에도 `target/<triple>/<프로파일>` 이 되어
//   같은 셈이 맞는다.)
//
// -in: 없음(환경변수 OUT_DIR 을 읽는다)
//
// -out: Option<PathBuf> = 실행파일 폴더. 모양이 예상과 다르면 None
// -out: error = 없음(판단 실패는 None)
//------------------------------------------------------------------
fn exe_dir_from_out_dir() -> Option<PathBuf> {
    let out_dir = std::env::var("OUT_DIR").ok()?;
    Path::new(&out_dir).ancestors().nth(3).map(|p| p.to_path_buf())
}

//------------------------------------------------------------------
// 이 대상 OS 에서 쓸 pdfium 파일 이름과 원본 후보들
//=> 배포본(dist-onedir) 안에 OS별로 정본이 들어 있다. 그것을 그대로 쓴다 —
//   빌드마다 다른 파일을 집어오면 배포본과 개발 빌드의 동작이 갈린다.
//
// -in: 없음(cargo 가 주는 CARGO_CFG_TARGET_OS 를 읽는다)
//
// -out: Option<(&'static str, Vec<PathBuf>)> = (파일명, 원본 후보 경로들)
//                                              지원하지 않는 OS 면 None
// -out: error = 없음
//------------------------------------------------------------------
fn lib_name_and_sources() -> Option<(&'static str, Vec<PathBuf>)> {
    // 크레이트 루트(= Rust/) 기준으로 찾는다. 빌드를 어느 폴더에서 걸든 같아진다.
    let root = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").ok()?);
    let os = std::env::var("CARGO_CFG_TARGET_OS").ok()?;

    let (name, rel): (&str, Vec<&str>) = match os.as_str() {
        "windows" => ("pdfium.dll", vec![
            "dist-onedir/windows/pdfium.dll",
            // 파이썬 배포본에도 같은 파일이 있다(sha256 동일) — Rust 배포본을
            // 아직 안 만든 작업 트리를 위한 2차 후보.
            "../dist/csoclassify/_internal/pypdfium2_raw/pdfium.dll",
        ]),
        "linux" => ("libpdfium.so", vec![
            "dist-onedir/linux/libpdfium.so",
            "../dist/csoclassify/_internal/pypdfium2_raw/libpdfium.so",
        ]),
        // macOS 용 정본은 배포본에 없다 — 있으면 그때 후보를 더한다.
        _ => return None,
    };
    Some((name, rel.into_iter().map(|r| root.join(r)).collect()))
}

//------------------------------------------------------------------
// 이미 같은 파일이 놓여 있나
//=> 매 빌드마다 7MB 를 다시 복사할 이유가 없다. 크기가 같으면 같은 파일로 본다
//   (정본이 바뀌면 크기도 바뀌고, 아래 rerun-if-changed 가 다시 돌린다).
//
// -in: src = 원본 경로
// -in: dst = 목적지 경로
//
// -out: bool = true 면 복사할 필요 없음
// -out: error = 없음(잴 수 없으면 false = 복사한다)
//------------------------------------------------------------------
fn already_same(src: &Path, dst: &Path) -> bool {
    match (std::fs::metadata(src), std::fs::metadata(dst)) {
        (Ok(a), Ok(b)) => a.len() == b.len(),
        _ => false,
    }
}

//------------------------------------------------------------------
// 이 대상 OS 에서 쓸 docparse 라이브러리 파일 이름과 원본 후보들
//=> 문서파서는 별도 프로젝트(DocParse-rust)가 만든 배포 묶음을 저장소의
//   third_party/docparse/<os>-x64/ 에 풀어 둔 것이 정본이다. 빌드서버에는 Rust/ 아래에
//   third_party/ 가 같이 올라가므로 두 자리를 다 본다.
//
// -in: 없음(cargo 가 주는 CARGO_CFG_TARGET_OS 를 읽는다)
//
// -out: Option<(&'static str, Vec<PathBuf>)> = (파일명, 원본 후보 경로들)
//                                              지원하지 않는 OS 면 None
// -out: error = 없음
//------------------------------------------------------------------
fn docparse_name_and_sources() -> Option<(&'static str, Vec<PathBuf>)> {
    let root = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").ok()?);
    let os = std::env::var("CARGO_CFG_TARGET_OS").ok()?;
    let (name, sub) = match os.as_str() {
        "windows" => ("docparse.dll", "windows-x64"),
        "linux" => ("libdocparse.so", "linux-x64"),
        _ => return None,
    };
    let rel = format!("third_party/docparse/{}/{}", sub, name);
    Some((name, vec![root.join("..").join(&rel), root.join(&rel)]))
}

//------------------------------------------------------------------
// 이 대상 OS 에서 쓸 moaipii(개인정보 검출) 라이브러리 파일 이름과 원본 후보들
//=> 개인정보 검출은 별도 프로젝트(MoAI-pii)가 만든 배포 묶음을 저장소의
//   third_party/moaipii/<os>-x64/ 에 풀어 둔 것이 정본이다(docparse 와 같은 자리 규칙).
//
// -in: 없음(cargo 가 주는 CARGO_CFG_TARGET_OS 를 읽는다)
//
// -out: Option<(&'static str, Vec<PathBuf>)> = (파일명, 원본 후보 경로들)
//                                              지원하지 않는 OS 면 None
// -out: error = 없음
//------------------------------------------------------------------
fn moaipii_name_and_sources() -> Option<(&'static str, Vec<PathBuf>)> {
    let root = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").ok()?);
    let os = std::env::var("CARGO_CFG_TARGET_OS").ok()?;
    let (name, sub) = match os.as_str() {
        "windows" => ("moaipii.dll", "windows-x64"),
        "linux" => ("libmoaipii.so", "linux-x64"),
        _ => return None,
    };
    let rel = format!("third_party/moaipii/{}/{}", sub, name);
    Some((name, vec![root.join("..").join(&rel), root.join(&rel)]))
}

//------------------------------------------------------------------
// 내용까지 같은 파일인가
//=> docparse 라이브러리는 고친 판이 같은 크기일 수 있어 크기만으로는 못 가른다.
//   몇 MB 라 통째로 비교해도 빌드 시간에 영향이 없다.
//
// -in: src = 원본 경로
// -in: dst = 목적지 경로
//
// -out: bool = true 면 복사할 필요 없음
// -out: error = 없음(못 읽으면 false = 복사한다)
//------------------------------------------------------------------
fn same_bytes(src: &Path, dst: &Path) -> bool {
    match (std::fs::read(src), std::fs::read(dst)) {
        (Ok(a), Ok(b)) => a == b,
        _ => false,
    }
}

//------------------------------------------------------------------
// 원본 하나를 exe 옆으로 복사
//=> 후보 중 처음 있는 파일을 골라, 이미 같은 파일이 있으면 건너뛴다.
//   복사가 막혀도(실행 중인 프로그램이 물고 있음) 빌드는 깨지 않는다.
//
// -in: name     = 목적지 파일명
// -in: sources  = 원본 후보들
// -in: exe_dir  = 실행파일 폴더
// -in: same     = 같은 파일인지 보는 함수
// -in: missing  = 원본이 없을 때 남길 경고 문장
//
// -out: 없음
// -out: error = 없음(실패는 cargo:warning)
//------------------------------------------------------------------
fn copy_beside(name: &str, sources: &[PathBuf], exe_dir: &Path, same: fn(&Path, &Path) -> bool, missing: &str) {
    // 정본이 바뀌면 이 스크립트를 다시 돌려 복사본을 갱신한다.
    for src in sources {
        println!("cargo:rerun-if-changed={}", src.display());
    }
    let Some(src) = sources.iter().find(|p| p.is_file()) else {
        println!("cargo:warning={}", missing);
        return;
    };
    let dst = exe_dir.join(name);
    if same(src, &dst) {
        return;
    }
    if let Err(e) = std::fs::copy(src, &dst) {
        // 실행 중인 바이너리가 DLL 을 물고 있으면 윈도우에서 복사가 막힌다.
        println!("cargo:warning={name} 복사 실패({e}) — 이미 있는 파일을 그대로 씁니다");
    }
}

//------------------------------------------------------------------
// 빌드 진입점
//=> pdfium · docparse · moaipii 라이브러리를 exe 옆(target/<프로파일>)으로 복사한다.
//
// -in: 없음
//
// -out: 없음
// -out: error = 없음(원본이 없거나 복사가 막히면 경고만)
//------------------------------------------------------------------
//------------------------------------------------------------------
// Windows SDK 의 리소스 컴파일러(rc.exe) 찾기
//=> 새 크레이트(embed-resource 등)를 들이지 않으려고 SDK 의 rc.exe 를 직접 부른다 — 리눅스
//   빌드서버가 새 의존성을 받지 못해도 빌드가 깨지지 않게 하려는 것이다.
//    1) 환경변수 RC 가 있으면 그 파일
//    2) Windows Kits\10\bin\<버전>\x64\rc.exe 중 가장 새 버전(버전 폴더 이름순 마지막)
//
// -in: 없음
//
// -out: Option<PathBuf> = rc.exe 경로(못 찾으면 None)
// -out: error = 없음
//------------------------------------------------------------------
fn find_rc() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("RC") {
        let p = PathBuf::from(p);
        if p.is_file() { return Some(p); }
    }
    let base = PathBuf::from(r"C:\Program Files (x86)\Windows Kits\10\bin");
    let mut vers: Vec<PathBuf> = std::fs::read_dir(&base).ok()?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.join("x64").join("rc.exe").is_file())
        .collect();
    // '10.0.26100.0' 같은 이름 — 자리마다 숫자로 견줘 가장 새 것을 고른다
    vers.sort_by_key(|p| p.file_name().and_then(|n| n.to_str()).unwrap_or("")
        .split('.').map(|x| x.parse::<u32>().unwrap_or(0)).collect::<Vec<_>>());
    vers.last().map(|p| p.join("x64").join("rc.exe"))
}

//------------------------------------------------------------------
// 실행파일 '속성 → 자세히' 정보(버전 리소스) 넣기 — Windows 대상만
//=> 탐색기 속성에 파일 버전 · 제품 이름 · 제품 버전 · 저작권 · 언어가 비어 있던 것을 채운다.
//    1) Cargo.toml 의 version(0.1.0)을 파일 · 제품 버전으로 쓴다(코드 버전과 한 곳에서 맞춤)
//    2) .rc 를 OUT_DIR 에 쓰고 rc.exe 로 .res 를 만든다(UTF-16 으로 써서 한글이 깨지지 않게)
//    3) 링커에 .res 를 넘긴다 — MSVC 링커는 .res 를 바로 받는다
//   rc.exe 가 없거나 실패하면 경고만 남기고 넘어간다(속성이 빌 뿐, 실행에는 지장 없다).
//   파이썬 판 build/csoclassify.spec 의 버전 정보와 같은 값을 쓴다.
//
// -in: 없음(CARGO_CFG_TARGET_OS · CARGO_PKG_VERSION · OUT_DIR 을 읽는다)
//
// -out: 없음
// -out: error = 없음(실패는 cargo:warning)
//------------------------------------------------------------------
fn embed_version_info() {
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("windows") {
        return;   // ELF(리눅스)에는 이런 속성 칸이 없다
    }
    let ver = std::env::var("CARGO_PKG_VERSION").unwrap_or_else(|_| "0.0.0".into());
    // 1.2.3 → 1,2,3,0 (리소스의 숫자 버전은 네 자리)
    let mut nums: Vec<u16> = ver.split(|c: char| !c.is_ascii_digit())
        .filter(|s| !s.is_empty()).take(4).map(|s| s.parse().unwrap_or(0)).collect();
    nums.resize(4, 0);
    let num = nums.iter().map(|n| n.to_string()).collect::<Vec<_>>().join(",");
    let dotted = nums.iter().map(|n| n.to_string()).collect::<Vec<_>>().join(".");
    let Ok(out) = std::env::var("OUT_DIR") else { return; };
    let rc_path = PathBuf::from(&out).join("version.rc");
    let res_path = PathBuf::from(&out).join("version.res");
    // 언어 0x0412(한국어) · 코드페이지 1200(유니코드) — '041204B0' 블록과 Translation 이 짝이다
    // winver.h 를 include 하지 않는다 — Visual Studio 환경(INCLUDE)이 없어도 rc.exe 가 돌게
    // 상수를 숫자로 쓴다: 1 = VS_VERSION_INFO · 0x40004 = VOS_NT_WINDOWS32 · 0x1 = VFT_APP
    let rc = format!(r#"1 VERSIONINFO
 FILEVERSION {num}
 PRODUCTVERSION {num}
 FILEFLAGSMASK 0x3fL
 FILEFLAGS 0x0L
 FILEOS 0x40004L
 FILETYPE 0x1L
 FILESUBTYPE 0x0L
BEGIN
  BLOCK "StringFileInfo"
  BEGIN
    BLOCK "041204B0"
    BEGIN
      VALUE "CompanyName", "kobongsoo"
      VALUE "FileDescription", "MpowerClassify-rs - 문서 보안등급 · 업무분류 (Rust 판)"
      VALUE "FileVersion", "{dotted}"
      VALUE "InternalName", "MpowerClassify-rs"
      VALUE "LegalCopyright", "Copyright (C) 2026 kobongsoo. All rights reserved."
      VALUE "OriginalFilename", "MpowerClassify-rs.exe"
      VALUE "ProductName", "MpowerClassify"
      VALUE "ProductVersion", "{ver}"
    END
  END
  BLOCK "VarFileInfo"
  BEGIN
    VALUE "Translation", 0x0412, 1200
  END
END
"#);
    // rc.exe 는 BOM 붙은 UTF-16LE 를 유니코드로 읽는다 — 한글 설명이 깨지지 않게 그렇게 쓴다
    let mut bytes = vec![0xFF, 0xFE];
    for u in rc.encode_utf16() { bytes.extend_from_slice(&u.to_le_bytes()); }
    if std::fs::write(&rc_path, bytes).is_err() {
        println!("cargo:warning=버전 리소스(.rc)를 쓰지 못해 exe 속성 정보를 건너뜁니다");
        return;
    }
    let Some(rc_exe) = find_rc() else {
        println!("cargo:warning=rc.exe(Windows SDK)를 찾지 못해 exe 속성 정보를 건너뜁니다 — 환경변수 RC 로 알려 주세요");
        return;
    };
    let ok = std::process::Command::new(&rc_exe)
        .args(["/nologo", "/fo"]).arg(&res_path).arg(&rc_path)
        .status().map(|s| s.success()).unwrap_or(false);
    if !ok {
        println!("cargo:warning=rc.exe 가 버전 리소스를 만들지 못해 exe 속성 정보를 건너뜁니다");
        return;
    }
    // 실행파일에만 붙인다(시험 실행파일에는 필요 없다)
    println!("cargo:rustc-link-arg-bins={}", res_path.display());
}

fn main() {
    // exe 속성(파일 버전 · 제품 이름 · 저작권 …) — 라이브러리 복사와 무관하게 먼저 넣는다
    println!("cargo:rerun-if-env-changed=RC");
    embed_version_info();
    // 환경변수로 직접 지정해 쓰는 사람도 있어, 그 값이 바뀌면 다시 판단하게 한다.
    println!("cargo:rerun-if-env-changed=CSO_PDFIUM");
    println!("cargo:rerun-if-env-changed=CSO_DOCPARSE");
    println!("cargo:rerun-if-env-changed=CSO_MOAIPII");

    let Some(exe_dir) = exe_dir_from_out_dir() else {
        println!("cargo:warning=빌드 산출 폴더를 알 수 없어 pdfium·docparse 복사를 건너뜁니다");
        return;
    };

    if let Some((name, sources)) = lib_name_and_sources() {
        // 배포본을 안 받은 작업 트리 — 빌드는 되게 두고, 왜 PDF 가 안 읽히는지만 알린다.
        copy_beside(name, &sources, &exe_dir, already_same,
                    &format!("{name} 원본을 찾지 못했습니다 — PDF 추출이 비활성됩니다.                               CSO_PDFIUM 환경변수로 직접 지정하거나 배포본(dist-onedir)을 받으세요"));
    }
    if let Some((name, sources)) = docparse_name_and_sources() {
        // 문서파서가 없으면 실행하자마자 1017 로 멈춘다 — 빌드 때 미리 알린다.
        copy_beside(name, &sources, &exe_dir, same_bytes,
                    &format!("{name} 원본(third_party/docparse)을 찾지 못했습니다 — 실행하면                               1017 extractor_missing 으로 멈춥니다. CSO_DOCPARSE 로 직접 지정하세요"));
    }
    if let Some((name, sources)) = moaipii_name_and_sources() {
        // 개인정보 검출 라이브러리가 없으면 분류를 시작하자마자 1018 로 멈춘다 — 빌드 때 미리 알린다.
        copy_beside(name, &sources, &exe_dir, same_bytes,
                    &format!("{name} 원본(third_party/moaipii)을 찾지 못했습니다 — 분류하면 \
                              1018 pii_engine_missing 으로 멈춥니다. CSO_MOAIPII 로 직접 지정하세요"));
    }
}
