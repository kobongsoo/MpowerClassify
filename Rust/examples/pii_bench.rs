//! 개인정보 검출 비교 시험용 진입점 — V10 vs MC(파이썬) vs MC(Rust).
//!
//! 제품 바이너리의 추출기·정리(clean)·분류를 거치지 않고, TXT 원문 문자열에
//! `pii::pii_records` / `pii::pii_counts` 만 직접 부른다(시험서: 문서 파서 없음).
//! 제품 코드와 같은 소스(src/pii.rs 등)를 #[path] 로 그대로 끌어와 쓰므로
//! 검출 로직은 제품과 한 글자도 다르지 않다.
//!
//! 사용:
//!   pii_bench values <라벨,라벨,...> <파일...>        → 파일마다 JSON 한 줄 {file, dets:[{label,value,start,end}]}
//!   pii_bench time   <라벨,...> <반복수> <파일...>     → 파일마다 JSON 한 줄 {file, found, t_values, t_counts}
//!   pii_bench init   <라벨,...>                       → 정규식 첫 컴파일(지연 초기화) 시간
//! 파일은 UTF-8 로 읽고 줄바꿈(CRLF)을 그대로 둔다. 시간은 초 단위, 워밍업 1회 뒤 반복 중앙값.

#[path = "../src/pii.rs"]
#[allow(dead_code)]
mod pii;
#[path = "../src/pii_fold.rs"]
#[allow(dead_code)]
mod pii_fold;
#[path = "../src/districts.rs"]
#[allow(dead_code)]
mod districts;

use std::collections::HashSet;
use std::time::Instant;

/// 파일 내용 읽기 — UTF-8, 줄바꿈 그대로.
fn read(p: &str) -> String {
    std::fs::read_to_string(p).unwrap_or_else(|e| panic!("읽기 실패 {p}: {e}"))
}

/// 중앙값(초).
fn median(mut xs: Vec<f64>) -> f64 {
    xs.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let n = xs.len();
    if n % 2 == 1 { xs[n / 2] } else { (xs[n / 2 - 1] + xs[n / 2]) / 2.0 }
}

/// 한 작업을 워밍업 1회 + reps 회 재서 중앙값(초)을 낸다.
fn timeit<F: FnMut()>(reps: usize, mut f: F) -> f64 {
    f();
    let mut xs = Vec::with_capacity(reps);
    for _ in 0..reps {
        let t = Instant::now();
        f();
        xs.push(t.elapsed().as_secs_f64());
    }
    median(xs)
}

/// JSON 문자열 이스케이프 (serde 없이 최소한만).
fn js(s: &str) -> String {
    let mut o = String::with_capacity(s.len() + 2);
    o.push('"');
    for ch in s.chars() {
        match ch {
            '"' => o.push_str("\\\""),
            '\\' => o.push_str("\\\\"),
            '\n' => o.push_str("\\n"),
            '\r' => o.push_str("\\r"),
            '\t' => o.push_str("\\t"),
            c if (c as u32) < 0x20 => o.push_str(&format!("\\u{:04x}", c as u32)),
            c => o.push(c),
        }
    }
    o.push('"');
    o
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 3 {
        eprintln!("사용: pii_bench values|time|init <라벨,...> [반복수] <파일...>");
        std::process::exit(2);
    }
    let mode = args[1].as_str();
    // 활성 라벨 = cso_rule.yaml regex_pii 의 라벨 목록 (부르는 쪽이 넘겨 준다)
    let wanted: HashSet<String> = args[2].split(',').filter(|s| !s.is_empty()).map(String::from).collect();
    match mode {
        "init" => {
            // Lazy 정규식들이 첫 호출 때 컴파일된다 — 그 비용을 준비 시간으로 본다
            let t = Instant::now();
            let _ = pii::pii_records("주민등록번호 900101-1234568 hong@example.com", &wanted);
            println!("{{\"init_s\":{}}}", t.elapsed().as_secs_f64());
        }
        "values" => {
            for p in &args[3..] {
                let text = read(p);
                let recs = pii::pii_records(&text, &wanted);
                let dets: Vec<String> = recs.iter().map(|(l, v, s, e)| {
                    format!("{{\"label\":{},\"value\":{},\"start\":{},\"end\":{}}}", js(l), js(v), s, e)
                }).collect();
                println!("{{\"file\":{},\"chars\":{},\"dets\":[{}]}}", js(p), text.chars().count(), dets.join(","));
            }
        }
        "time" => {
            let reps: usize = args[3].parse().expect("반복수");
            for p in &args[4..] {
                let found = pii::pii_records(&read(p), &wanted).len();
                // 파이썬 판과 같은 조건: 파일 읽기 + 검출을 한 묶음으로 잰다
                let tv = timeit(reps, || { let _ = pii::pii_records(&read(p), &wanted); });
                let tc = timeit(reps, || { let _ = pii::pii_counts(&read(p), &wanted); });
                println!("{{\"file\":{},\"found\":{},\"t_values\":{},\"t_counts\":{}}}", js(p), found, tv, tc);
            }
        }
        _ => {
            eprintln!("모드는 values | time | init");
            std::process::exit(2);
        }
    }
}
