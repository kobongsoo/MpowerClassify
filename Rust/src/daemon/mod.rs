//! 임베딩 상주 서비스 — 모델을 한 번 올려 메모리에 물고 있는 프로세스와, 거기에 붙는 쪽.
//!
//! 왜 필요한가: 엠파워 v11 연동은 **파일 하나마다 exe 를 한 번씩** 부른다. 이 판은
//! 호출마다 임베딩 모델(model.onnx 118MB + tokenizer.json 17MB)을 새로 읽어 건당
//! 약 1.4초를 낸다(README 성능표). 모델을 프로세스 밖에 상주시키면 그 비용이 사라진다.
//!
//! 무엇을 맡기나: **임베딩만**(텍스트 → 벡터). 추출·규칙·전파는 지금처럼 호출한 exe 안에서
//! 돈다 — 분류 로직이 한 곳에만 있어야 결과가 갈리지 않는다.
//!
//! 구성 — 파이썬 판 `csoclassify/daemon/` 과 파일이 하나씩 대응한다:
//!   · ipc      : 127.0.0.1 TCP 위의 "4바이트 길이 + JSON" 송수신
//!   · registry : "지금 어느 포트에 떠 있나" 기록 파일 + 하나만 뜨게 하는 잠금
//!   · server   : `--serve` — 모델을 물고 요청에 답한다
//!   · client   : `--daemon` — 서버를 찾고(없으면 띄우고) 벡터를 받아 온다
//!
//! 설계: plan/Rust-임베딩모델-상주서비스-설계.html

pub mod client;
pub mod ipc;
pub mod registry;
pub mod server;

use std::time::Duration;

/// 서버는 반드시 내 PC 안에서만 받는다(외부 주소 금지).
pub const HOST: &str = "127.0.0.1";

/// 통신 규약 번호 — 메시지 모양을 바꾸면 올린다. 다르면 옛 서버를 내리고 새로 띄운다.
pub const PROTO: u64 = 1;

/// 이 빌드의 버전 — 기록·응답에 싣는다. exe 를 교체했는데 옛 서버가 남은 경우를 잡는다.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// 한 메시지 최대 크기(16MB) — 파이썬 판 ipc.MAX_MSG 와 같다.
pub const MAX_MSG: usize = 16 * 1024 * 1024;

/// 접속 한도 — 서버가 없으면 곧바로 알아채야 한다(파이썬 판 CLIENT_CONNECT_TIMEOUT).
pub const CONNECT_TIMEOUT: Duration = Duration::from_millis(300);
/// ping 응답 한도 — 서버는 ping 을 임베딩과 따로 곧바로 답하므로 짧아도 된다.
pub const PING_TIMEOUT: Duration = Duration::from_secs(1);
/// 자동 기동 뒤 준비 대기 한도(파이썬 판 DAEMON_START_WAIT).
pub const START_WAIT: Duration = Duration::from_secs(20);
/// 임베딩 응답 한도 — 큰 문서와 앞선 요청 대기를 넉넉히 본다.
pub const EMBED_TIMEOUT: Duration = Duration::from_secs(120);
/// 서버가 요청 하나를 읽는 한도 — 보내다 만 클라이언트가 연결을 붙잡지 못하게.
pub const SERVER_IO_TIMEOUT: Duration = Duration::from_secs(10);
/// 막 생긴 잠금으로 보는 시간 — 이 안에는 "기록을 아직 못 쓴 기동 중"일 수 있다.
pub const LOCK_YOUNG: Duration = Duration::from_secs(3);
