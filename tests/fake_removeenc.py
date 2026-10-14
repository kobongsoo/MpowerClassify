# -*- coding: utf-8 -*-
"""시험용 가짜 복호화 도구 — 엠파워 v11 의 removeenc.sh 와 같은 인자를 받는다.

개발 PC 에는 removeenc.sh 도 엠파워 PHP 확장(Mpower 64File)도 없다. 복호화 연동의
'흐름'(목록 읽기 · 임시 파일 · 결과 확인 · 오류 처리)을 시험하려고 --decrypt-cmd 에
이 스크립트를 끼운다(tests/test_decrypt.py). Rust 판의 examples/fake_removeenc.rs 와
같은 동작이다. 설계: plan/엠파워v11-암호화문서-복호화연동-설계-20261012.html §11

진짜 도구의 성질을 흉내 낸다:
  · "암호화"는 바이트마다 0x5A 를 XOR 한 것이다. 열쇠가 GOODKEY 면 제대로 푼다.
  · 틀린 열쇠에도 0(성공)을 돌려주면서 원본과 같은 크기의 쓰레기를 낸다 —
    진짜 mpower_decrypt 가 그렇다(removeenc.php 주석, 2026-09-29 실측).
  · 끝날 때 종료코드를 stdout 에도 한 줄 찍는다(removeenc.sh 의 `echo $OUT`).
  · v2: 로 시작하는 열쇠는 14, 인자가 빠지면 254.

열쇠 값으로 그 밖의 상황을 고른다:
  RC9 → 9 로 실패 · RC56 → 56(확장 없음) · EMPTY → 아무것도 안 쓰고 0 ·
  SLEEP → 30초 잠 · NOISE → stdout 에 잡다한 글자를 잔뜩 찍고 제대로 푼다
"""

import sys
import time


#------------------------------------------------------------------
# 종료코드를 화면에도 찍고 끝낸다
#=> removeenc.sh 는 종료코드를 stdout 에도 찍는다 — 부르는 쪽이 이것을 가로채는지 본다.
#
# -in: code = 종료코드
#
# -out: 없음(프로세스 종료)
# -out: error = 없음
#------------------------------------------------------------------
def finish(code):
    print(code)
    sys.exit(code)


#------------------------------------------------------------------
# 가짜 복호화 본체
#=> -s 원본을 읽어 -t 대상에 쓴다. 열쇠(-k)에 따라 성공·쓰레기·실패를 흉내 낸다.
#    1) 인자를 -s/-t/-k/-e/-z 짝으로 읽는다(빠지면 254)
#    2) 감싼 열쇠(v2:)면 14, 특수 열쇠면 그 상황을 낸다
#    3) 맞는 열쇠면 0x5A 로 되돌리고, 틀린 열쇠면 다른 값으로 XOR 해 쓰레기를 만든다(그래도 0)
#
# -in: argv = 명령줄 인자(프로그램 이름 제외)
#
# -out: 없음(finish 로 끝난다)
# -out: error = 원본을 못 읽으면 4, 대상을 못 쓰면 5 로 종료
#------------------------------------------------------------------
def main(argv):
    opt = dict(zip(argv[0::2], argv[1::2]))
    src, tgt, key = opt.get("-s"), opt.get("-t"), opt.get("-k")
    if not src or not tgt or not key or "-e" not in opt or "-z" not in opt:
        print("failed to parse argument.")
        print("[Usage] removeenc.sh -s {source path} -t {target path} -k {mrk} "
              "-e {enc mode} -z {zip mode}")
        finish(254)
    if key.startswith("v2:"):
        print("mrk is KEK-wrapped (v2:). unwrap it with MCrypt::decryptMRKWithKEK before calling.")
        finish(14)
    if key == "RC9":
        finish(9)
    if key == "RC56":
        finish(56)
    if key == "EMPTY":
        finish(0)
    if key == "SLEEP":
        time.sleep(30)
        finish(0)
    if key == "NOISE":
        # 파이프 버퍼(보통 64KB)를 넘겨 찍는다 — 부르는 쪽이 출력을 안 읽으면 여기서 멈춘다.
        for n in range(4000):
            print('{"noise": %d, "this is not our json": true}' % n)
    try:
        with open(src, "rb") as f:
            data = f.read()
    except OSError:
        finish(4)
    mask = 0x5A if key in ("GOODKEY", "NOISE") else 0xC3
    try:
        with open(tgt, "wb") as f:
            f.write(bytes(b ^ mask for b in data))
    except OSError:
        finish(5)
    finish(0)


if __name__ == "__main__":
    main(sys.argv[1:])
