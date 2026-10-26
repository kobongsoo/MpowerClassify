//! 시험용 가짜 사이냅 문서필터(snf_exe) — 진짜와 같은 인자를 받는다.
//!
//! 개발 시험에서 진짜 사이냅 없이 '사이냅 모드'의 흐름(찾기 · 호출 · 성공 판별 ·
//! 폴백 · 실패 사유 · 제한 시간 · 출력파일 정리)을 본다(tests/cli_synap.rs).
//! 설계: plan/Rust-사이냅추출기-옵션-설계-20261019.html §9.
//!
//! 진짜 snf_exe 의 성질을 흉내 낸다:
//!   · 인자: `-U8 [-NO_WITHPAGE] <입력> <출력>`
//!   · **성공이든 실패든 종료코드는 0** — 부르는 쪽은 출력파일로 성공을 봐야 한다
//!
//! 환경변수 FAKE_SNF_MODE 로 상황을 고른다(부르는 프로그램의 환경을 물려받는다):
//!   ok(기본) → 출력파일에 "사이냅이 읽은 본문 … <입력 파일 이름>" 을 쓴다
//!   short    → 3글자만 쓴다(짧은 결과)
//!   empty    → 0바이트 파일만 만든다
//!   none     → 출력파일을 안 만들고 stdout 에 오류 문장을 찍는다
//!   sleep    → 30초 잔다(제한 시간 시험)
//! 환경변수 FAKE_SNF_LOG 가 있으면 부를 때마다 그 파일에 "입력|출력" 한 줄을 덧붙인다
//! (부르지 않았어야 하는 경우를 시험이 확인한다).

use std::io::Write;

//------------------------------------------------------------------
// 진입점 — 인자를 읽고 FAKE_SNF_MODE 대로 출력파일을 만든다
//
// -in: 없음(명령줄 · 환경변수)
//
// -out: 없음(종료코드는 늘 0)
// -out: error = 없음
//------------------------------------------------------------------
fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    // 옵션(-U8 · -NO_WITHPAGE)을 뺀 마지막 둘이 입력 · 출력이다.
    let paths: Vec<&String> = args.iter().filter(|a| !a.starts_with('-')).collect();
    if paths.len() < 2 {
        println!("usage: snf_exe -U8 [-NO_WITHPAGE] <in> <out>");
        std::process::exit(0);
    }
    let (input, output) = (paths[paths.len() - 2], paths[paths.len() - 1]);
    if let Ok(log) = std::env::var("FAKE_SNF_LOG") {
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(log) {
            let _ = writeln!(f, "{}|{}", input, output);
        }
    }
    let name = std::path::Path::new(input).file_name()
        .map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    match std::env::var("FAKE_SNF_MODE").unwrap_or_default().as_str() {
        "short" => { let _ = std::fs::write(output, "짧음"); }
        "empty" => { let _ = std::fs::write(output, b""); }
        "none" => println!("fake snf: 이 문서를 열 수 없습니다"),
        "sleep" => std::thread::sleep(std::time::Duration::from_secs(30)),
        _ => {
            let _ = std::fs::write(output, format!(
                "사이냅이 읽은 본문입니다. 계약서 · 인사평가 · 개인정보가 들어 있는 시험 문서 {}\n", name));
        }
    }
    // 진짜처럼 늘 0 을 돌려준다.
    std::process::exit(0);
}
