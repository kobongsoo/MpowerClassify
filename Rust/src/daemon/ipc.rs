//! 상주 서비스 통신 — 127.0.0.1 TCP 위에 "4바이트 길이(big-endian) + UTF-8 JSON".
//! 파이썬 판 `daemon/ipc.py` 와 같은 규약이다. 연결 하나에 요청 하나, 응답 하나.

use std::io::{self, Read, Write};
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4, TcpListener, TcpStream};
use std::time::Duration;

use serde_json::Value;

use super::MAX_MSG;

//------------------------------------------------------------------
// 메시지 보내기
//=> JSON 을 [길이 4바이트][본문] 으로 보낸다. 길이를 먼저 보내야 받는 쪽이
//   몇 바이트를 읽을지 안다(TCP 에는 메시지 경계가 없다).
//
// -in: stream = 연결된 소켓
// -in: msg    = 보낼 JSON 값
//
// -out: () = 다 보냄
// -out: error = 본문이 상한(16MB)을 넘으면 InvalidInput, 전송 실패는 io::Error 그대로
//------------------------------------------------------------------
pub fn send_msg(stream: &mut TcpStream, msg: &Value) -> io::Result<()> {
    let body = serde_json::to_vec(msg)
        .map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, e))?;
    if body.len() > MAX_MSG {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "메시지가 너무 큼"));
    }
    // 길이와 본문을 한 번에 쓴다 — 둘로 나눠 쓰면 작은 패킷이 따로 나가 왕복이 늦어진다.
    let mut buf = Vec::with_capacity(4 + body.len());
    buf.extend_from_slice(&(body.len() as u32).to_be_bytes());
    buf.extend_from_slice(&body);
    stream.write_all(&buf)?;
    stream.flush()
}

//------------------------------------------------------------------
// 메시지 받기
//=> [길이][본문] 을 읽어 JSON 으로 되돌린다. 길이가 상한을 넘으면 읽지 않고 거부한다
//   (엉뚱한 프로그램이 보낸 쓰레기 4바이트로 수 GB 를 잡는 일을 막는다).
//
// -in: stream = 연결된 소켓(읽기 한도는 부르는 쪽이 미리 건다)
//
// -out: Value = 받은 JSON 값
// -out: error = 상한 초과·JSON 아님은 InvalidData, 끊김·한도 초과는 io::Error 그대로
//------------------------------------------------------------------
pub fn recv_msg(stream: &mut TcpStream) -> io::Result<Value> {
    let mut head = [0u8; 4];
    stream.read_exact(&mut head)?;
    let len = u32::from_be_bytes(head) as usize;
    if len > MAX_MSG {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "수신 메시지 길이가 상한 초과"));
    }
    let mut body = vec![0u8; len];
    stream.read_exact(&mut body)?;
    serde_json::from_slice(&body).map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))
}

//------------------------------------------------------------------
// 서버 소켓 만들기
//=> 127.0.0.1 의 빈 포트에 묶는다(포트 0 = OS 가 골라 준다). 실제 포트를 함께 돌려준다.
//
// -in: 없음
//
// -out: (TcpListener, u16) = 듣는 소켓과 실제 포트
// -out: error = 묶기 실패 시 io::Error
//------------------------------------------------------------------
pub fn make_server() -> io::Result<(TcpListener, u16)> {
    let srv = TcpListener::bind(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0))?;
    let port = srv.local_addr()?.port();
    Ok((srv, port))
}

//------------------------------------------------------------------
// 서버에 접속
//=> 한도 안에 127.0.0.1:port 로 붙는다. 붙은 뒤의 읽기·쓰기 한도도 함께 건다.
//
// -in: port    = 서버 포트(기록 파일에서 읽은 값)
// -in: connect = 접속 한도
// -in: io_wait = 붙은 뒤 읽기·쓰기 한도(응답을 기다리는 시간)
//
// -out: TcpStream = 연결된 소켓
// -out: error = 접속 실패·한도 초과 시 io::Error
//------------------------------------------------------------------
pub fn connect(port: u16, connect: Duration, io_wait: Duration) -> io::Result<TcpStream> {
    let addr = SocketAddr::V4(SocketAddrV4::new(Ipv4Addr::LOCALHOST, port));
    let s = TcpStream::connect_timeout(&addr, connect)?;
    s.set_read_timeout(Some(io_wait))?;
    s.set_write_timeout(Some(io_wait))?;
    // 요청·응답이 한 덩어리씩이라 모아 보낼 이유가 없다 — 바로 내보낸다.
    let _ = s.set_nodelay(true);
    Ok(s)
}

//------------------------------------------------------------------
// 요청 하나 보내고 응답 하나 받기
//=> 접속 → 보내기 → 받기를 한 번에 한다. 연결은 함수가 끝나면 닫힌다.
//
// -in: port    = 서버 포트
// -in: msg     = 보낼 요청
// -in: io_wait = 응답을 기다릴 한도
//
// -out: Value = 서버 응답
// -out: error = 접속·통신 실패 시 io::Error
//------------------------------------------------------------------
pub fn rpc(port: u16, msg: &Value, io_wait: Duration) -> io::Result<Value> {
    let mut s = connect(port, super::CONNECT_TIMEOUT, io_wait)?;
    send_msg(&mut s, msg)?;
    recv_msg(&mut s)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    // 보낸 그대로 돌아오는가 — 한글과 f32 벡터가 섞인 메시지로 본다.
    #[test]
    fn 메시지가_그대로_왕복한다() {
        let (srv, port) = make_server().unwrap();
        let t = std::thread::spawn(move || {
            let (mut c, _) = srv.accept().unwrap();
            let m = recv_msg(&mut c).unwrap();
            send_msg(&mut c, &m).unwrap();
        });
        let msg = json!({"op": "embed_text", "text": "가나다\n라마", "v": [0.1f32, -2.5f32]});
        let back = rpc(port, &msg, Duration::from_secs(5)).unwrap();
        assert_eq!(back, msg);
        t.join().unwrap();
    }

    // 길이 칸이 상한을 넘으면 본문을 읽지 않고 거부한다.
    #[test]
    fn 과대_길이는_거부한다() {
        let (srv, port) = make_server().unwrap();
        let t = std::thread::spawn(move || {
            let (mut c, _) = srv.accept().unwrap();
            c.write_all(&u32::MAX.to_be_bytes()).unwrap();
        });
        let mut s = connect(port, Duration::from_secs(1), Duration::from_secs(5)).unwrap();
        let e = recv_msg(&mut s).unwrap_err();
        assert_eq!(e.kind(), io::ErrorKind::InvalidData);
        t.join().unwrap();
    }

    // 아무도 듣지 않는 포트는 한도 안에 실패로 돌아온다(멈춰 있지 않는다).
    #[test]
    fn 없는_서버는_곧바로_실패한다() {
        let port = { let (srv, p) = make_server().unwrap(); drop(srv); p };
        assert!(connect(port, Duration::from_millis(300), Duration::from_secs(1)).is_err());
    }
}
