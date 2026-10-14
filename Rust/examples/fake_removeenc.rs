//! 시험용 가짜 복호화 도구 — 엠파워 v11 의 removeenc.sh 와 같은 인자를 받는다.
//!
//! 개발 PC 에는 removeenc.sh 도 엠파워 PHP 확장(Mpower 64File)도 없다. 복호화 연동의
//! '흐름'(목록 읽기 · 임시 파일 · 결과 확인 · 오류 처리)을 시험하려고 `--decrypt-cmd`
//! 에 이 프로그램을 끼운다(tests/cli_decrypt.rs). 설계: plan/엠파워v11-암호화문서-
//! 복호화연동-설계-20261012.html §11.
//!
//! 진짜 도구의 성질을 흉내 낸다:
//!   · "암호화"는 바이트마다 0x5A 를 XOR 한 것이다. 열쇠가 `GOODKEY` 면 제대로 푼다.
//!   · **틀린 열쇠에도 0(성공)을 돌려주면서 원본과 같은 크기의 쓰레기를 낸다** —
//!     진짜 mpower_decrypt 가 그렇다(removeenc.php 주석, 2026-09-29 실측).
//!   · 끝날 때 종료코드를 stdout 에도 한 줄 찍는다(removeenc.sh 의 `echo $OUT`).
//!   · `v2:` 로 시작하는 열쇠는 14, 인자가 빠지면 254.
//!
//! 열쇠 값으로 그 밖의 상황을 고른다:
//!   RC9 → 9 로 실패 · RC56 → 56(확장 없음) · EMPTY → 아무것도 안 쓰고 0 ·
//!   SLEEP → 30초 잠 · NOISE → stdout 에 잡다한 글자를 잔뜩 찍고 제대로 푼다 ·
//!   SLEEPCHILD → 자식 프로세스를 띄워 그쪽이 30초 잔다(removeenc.sh 가 php 를 띄우는
//!   구조를 흉내 낸다 — 부모만 죽이면 자식이 출력 파이프를 붙잡고 남는다)

use std::collections::HashMap;

fn finish(code: i32) -> ! {
    // removeenc.sh 는 종료코드를 화면에도 찍는다 — 부르는 쪽이 이것을 가로채는지 본다.
    println!("{}", code);
    std::process::exit(code)
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut opt: HashMap<String, String> = HashMap::new();
    let mut i = 0;
    while i + 1 < args.len() {
        opt.insert(args[i].clone(), args[i + 1].clone());
        i += 2;
    }
    let (src, tgt, key) = match (opt.get("-s"), opt.get("-t"), opt.get("-k")) {
        (Some(s), Some(t), Some(k)) if !k.is_empty() => (s.clone(), t.clone(), k.clone()),
        _ => {
            println!("failed to parse argument.");
            println!("[Usage] removeenc.sh -s {{source path}} -t {{target path}} -k {{mrk}} -e {{enc mode}} -z {{zip mode}}");
            finish(254)
        }
    };
    if !opt.contains_key("-e") || !opt.contains_key("-z") {
        println!("failed to parse enc mode.");
        finish(254);
    }
    if key.starts_with("v2:") {
        println!("mrk is KEK-wrapped (v2:). unwrap it with MCrypt::decryptMRKWithKEK before calling.");
        finish(14);
    }
    match key.as_str() {
        "RC9" => finish(9),
        "RC56" => finish(56),
        "EMPTY" => finish(0),
        "SLEEPCHILD" => {
            // 같은 인자에서 열쇠만 SLEEP 으로 바꿔 자신을 다시 띄운다. 출력은 물려준다.
            let me = std::env::current_exe().unwrap();
            let child_args: Vec<String> = args.iter()
                .map(|a| if a == "SLEEPCHILD" { "SLEEP".to_string() } else { a.clone() })
                .collect();
            let st = std::process::Command::new(me).args(&child_args).status();
            finish(st.ok().and_then(|s| s.code()).unwrap_or(1))
        }
        "SLEEP" => {
            std::thread::sleep(std::time::Duration::from_secs(30));
            finish(0)
        }
        _ => {}
    }
    if key == "NOISE" {
        // 파이프 버퍼(보통 64KB)를 넘겨 찍는다 — 부르는 쪽이 출력을 안 읽으면 여기서 멈춘다.
        for n in 0..4000 {
            println!("{{\"noise\": {}, \"this is not our json\": true}}", n);
        }
    }
    let data = match std::fs::read(&src) {
        Ok(d) => d,
        Err(_) => finish(4),
    };
    // 맞는 열쇠면 0x5A 로 되돌리고, 틀린 열쇠면 다른 값으로 XOR 해 쓰레기를 만든다(그래도 0).
    let mask: u8 = if key == "GOODKEY" || key == "NOISE" { 0x5A } else { 0xC3 };
    let out: Vec<u8> = data.iter().map(|b| b ^ mask).collect();
    if std::fs::write(&tgt, out).is_err() {
        finish(5);
    }
    finish(0)
}
