# -*- coding: utf-8 -*-
"""Rust 판의 상주 서버 인자와, 지원 못 하는 인자를 '조용히 무시'하지 않는가.

Rust 판에도 임베딩 모델을 물고 있는 상주 서버가 생겼다
(설계: plan/Rust-임베딩모델-상주서비스-설계.html). 옵션 이름과 출력 문구는 파이썬 판과
같아야 두 판을 같은 명령으로 부를 수 있다 — 이 시험이 그 겉모양을 지킨다.
  · --status : 서버가 없으면 "실행 중인 데몬 없음" 을 알리고 정상 종료
  · --stop   : 서버가 없으면 "정지할 데몬 없음" 을 알리고 정상 종료
    (둘 다 출력은 JSON 한 줄이다 — 문구는 error.message 에, 값은 daemon 에 들어 있다)
  · --daemon : 임베딩이 필요 없는 실행에서는 서버를 띄우지 않고 조용히 분류한다
  · --no-daemon : 기본값과 같다 → 아무 말도 하지 않는다
서버를 실제로 띄우는 동작(--serve, 자동 기동, 벡터 동일성)은 모델 파일이 있어야 해서
Rust 쪽 통합 시험(Rust/tests/cli_daemon.rs)이 맡는다. 여기서 --serve 를 부르면 안 된다 —
서버는 끝나지 않는 프로세스라 시험이 멈춘다.

--synap-only 는 사이냅을 못 찾으면 문서를 읽기 전에 멈춰야 한다(1016) — 아래 뒤쪽 시험들.
"""
import json
import os
import subprocess

import pytest

from csoclassify import errcodes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 개발 중에는 debug, 배포 검증에는 release 를 쓴다. 있는 쪽을 집는다.
_CANDIDATES = [
    os.path.join(ROOT, "Rust", "target", "release", "MpowerClassify-rs.exe"),
    os.path.join(ROOT, "Rust", "target", "debug", "MpowerClassify-rs.exe"),
]
RS_EXE = next((p for p in _CANDIDATES if os.path.isfile(p)), None)

pytestmark = pytest.mark.skipif(RS_EXE is None, reason="Rust 실행파일 없음")


#------------------------------------------------------------------
# Rust 판을 인자만 주고 한 번 돌린다
#=> 상주 서버 관련 인자는 문서·규칙셋이 없어도 답해야 하는 것들이라(질의·모드 요청),
#   대상 파일을 주지 않고 그대로 부를 수 있다.
#    1) 서버 접속 기록을 두는 상태 폴더를 시험용 임시 폴더로 돌린다 — 개발자 PC 에
#       실제로 떠 있는 서버를 건드리거나, 그 서버 때문에 결과가 달라지지 않게
#    2) 로그도 같은 폴더로 보낸다(저장소를 어지르지 않게)
#
# -in: args      = 붙일 CLI 인자들
# -in: state_dir = 상태 폴더로 쓸 경로(시험마다 tmp_path 를 준다)
#
# -out: CompletedProcess = stdout/stderr/returncode
# -out: error = 없음
#------------------------------------------------------------------
def _run(*args, state_dir):
    env = dict(os.environ)
    env["CSOCLASSIFY_STATE_DIR"] = str(state_dir)
    env["CSOCLASSIFY_LOGDIR"] = os.path.join(str(state_dir), "log")
    return subprocess.run([RS_EXE, *args], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", env=env)


#------------------------------------------------------------------
# 상태 폴더에 서버 기록이 생겼는가
#=> 서버가 뜨면 상태 폴더에 daemon-rs-<모델>.json 과 그 .lock 이 생긴다.
#   "서버를 띄우지 않았다"를 이 파일들이 없는 것으로 확인한다.
#
# -in: state_dir = 상태 폴더 경로
#
# -out: bool = 서버 기록·잠금 파일이 하나라도 있으면 True
# -out: error = 없음(폴더가 없으면 False)
#------------------------------------------------------------------
def _has_daemon_files(state_dir):
    if not os.path.isdir(str(state_dir)):
        return False
    return any(n.startswith("daemon-rs-") for n in os.listdir(str(state_dir)))


#------------------------------------------------------------------
# --status · --stop 은 서버가 없으면 그 사실을 알리고 정상 종료한다
#=> 문구는 파이썬 판이 데몬이 없을 때 내는 것과 같다. 다만 이 판은 그 문구를
#   JSON 한 줄의 error.message 에 담아 낸다 — 연동 프로그램이 문구를 잘라 읽지 않고
#   error.code(0=정상)와 daemon.running 으로 분기하게 하려는 것이다.
#    1) 종료코드 0, stdout 은 정확히 한 줄
#    2) 그 줄이 JSON 이고 error.code=0 · kind=success
#    3) 서버가 없으므로 daemon.running 은 False
#
# -in: opt      = 시험할 인자
# -in: expect   = stdout 에 있어야 할 문구(파이썬 판과 같은 문구)
# -in: tmp_path = pytest 임시 폴더(상태 폴더로 쓴다)
# -out: 없음(단언)
# -out: error = 종료코드나 안내가 달라지면 AssertionError
#------------------------------------------------------------------
@pytest.mark.parametrize("opt,expect", [("--status", "실행 중인 데몬 없음"),
                                        ("--stop", "정지할 데몬 없음")])
def test_status_stop은_알리고_정상종료(opt, expect, tmp_path):
    r = _run(opt, state_dir=tmp_path)
    assert r.returncode == 0, f"exit={r.returncode} :: {r.stderr[-300:]}"
    assert expect in r.stdout, f"안내가 없다 :: {r.stdout!r}"
    # 받는 쪽은 stdout 한 줄을 JSON 으로 읽는다 — 줄이 늘거나 모양이 바뀌면 연동이 깨진다.
    lines = r.stdout.strip().splitlines()
    assert len(lines) == 1, f"한 줄이어야 한다 :: {r.stdout!r}"
    obj = json.loads(lines[0])
    assert obj["error"]["code"] == 0 and obj["error"]["kind"] == "success"
    assert expect in obj["error"]["message"]
    # 서버가 없다 — --status 는 running=False, --stop 은 stopped=False 로 알린다.
    assert not obj["daemon"].get("running", False)
    if opt == "--stop":
        assert obj["daemon"]["stopped"] is False
    # 묻기만 했는데 서버가 뜨면 안 된다.
    assert not _has_daemon_files(tmp_path)


#------------------------------------------------------------------
# --daemon 은 임베딩이 필요 없으면 서버를 띄우지 않고 분류만 한다
#=> --daemon 은 "임베딩을 상주 서버에 맡겨라"는 뜻이다. 규칙만 쓰는 실행(--rule-only)은
#   임베딩할 문서가 없으므로 서버에 접속하지도, 서버를 새로 띄우지도 않아야 한다.
#   여기서 서버가 뜨면 모델을 쓰지도 않는 호출이 수백 MB 짜리 프로세스를 남긴다.
#
# -in: tmp_path = pytest 임시 폴더
# -out: 없음(단언)
# -out: error = 분류가 안 돌거나, 서버 기록이 생기면 AssertionError
#------------------------------------------------------------------
def test_daemon은_임베딩이_필요없으면_서버를_띄우지_않는다(tmp_path):
    doc = tmp_path / "문서.txt"
    doc.write_text("월간 운영 보고\n\n정기 점검을 예정대로 수행했습니다.\n",
                   encoding="utf-8")
    state = tmp_path / "state"
    rules = os.path.join(ROOT, "resources", "policy", "cso_rule.yaml")
    r = _run("--daemon", "--file", str(doc), "--cso-rules", rules,
             "--rule-only", "--format", "jsonl", "--nosummary", "--no-timing",
             state_dir=state)
    assert r.returncode == 0, f"exit={r.returncode} :: {r.stderr[-300:]}"
    assert '"file"' in r.stdout, f"분류가 돌지 않았다 :: {r.stderr[-300:]}"
    # 예전의 "이 판에는 데몬이 없습니다" 경고는 이제 사실이 아니다 — 나오면 안 된다.
    assert "데몬" not in r.stderr, f"옛 경고가 남아 있다 :: {r.stderr[-300:]}"
    assert not _has_daemon_files(state), "임베딩이 필요 없는데 서버가 떴다"


#------------------------------------------------------------------
# --no-daemon 에는 아무 말도 하지 않는다 (오탐 방지)
#=> 이 판은 기본이 상주 서버 없이 도는 것이다. 기본값을 다시 말했을 뿐인데 경고를
#   내면, 매 실행마다 뜨는 소음이 되어 정작 진짜 경고를 가린다.
#
# -in: tmp_path = pytest 임시 폴더
# -out: 없음(단언)
# -out: error = 불필요한 경고가 생기면 AssertionError
#------------------------------------------------------------------
def test_no_daemon은_조용하다(tmp_path):
    doc = tmp_path / "문서.txt"
    doc.write_text("월간 운영 보고\n\n정기 점검을 예정대로 수행했습니다.\n",
                   encoding="utf-8")
    rules = os.path.join(ROOT, "resources", "policy", "cso_rule.yaml")
    r = _run("--no-daemon", "--file", str(doc), "--cso-rules", rules,
             "--rule-only", "--format", "jsonl", "--nosummary", "--no-timing",
             state_dir=tmp_path / "state")
    assert r.returncode == 0, f"exit={r.returncode} :: {r.stderr[-300:]}"
    assert '"file"' in r.stdout, f"분류가 돌지 않았다 :: {r.stderr[-300:]}"
    assert "데몬" not in r.stderr, f"쓸데없는 경고가 떴다 :: {r.stderr[-300:]}"


#------------------------------------------------------------------
# --synap-only 인데 사이냅이 없으면 문서를 읽기 전에 멈춘다 (1016 synap_missing)
#=> 2026-10-19 부터 Rust 판도 사이냅(snf_exe)을 옵션으로 쓴다
#   (설계: plan/Rust-사이냅추출기-옵션-설계-20261019.html). 예전에는 '지원하지 않음'(1012)
#   으로 멈췄다. 이제는 사이냅을 찾아 쓰되, 못 찾으면 조용히 자체 파서로 돌지 않고
#   멈춘다 — 다른 추출기로 돌면 본문이 달라져 등급까지 갈리기 때문이다(원칙은 그대로).
#   찾는 곳을 빈 폴더로 막아, 이 PC 에 사이냅이 있어도 '없음'을 재현한다.
#
# -in: tmp_path = pytest 임시 폴더(상태 폴더 · 빈 사이냅 폴더로 쓴다)
# -out: 없음(단언)
# -out: error = 조용히 넘어가면 AssertionError
#------------------------------------------------------------------
def test_synap_only는_사이냅이_없으면_멈춘다(tmp_path):
    empty = tmp_path / "no_synap"
    empty.mkdir()
    env_dir = os.environ.get("CSOCLASSIFY_SYNAP_DIR")
    os.environ["CSOCLASSIFY_SYNAP_DIR"] = str(empty)
    try:
        r = _run("--synap-only", "--synap-path", str(empty / "snf_exe.exe"), "--json-errors",
                 state_dir=tmp_path)
    finally:
        if env_dir is None:
            os.environ.pop("CSOCLASSIFY_SYNAP_DIR", None)
        else:
            os.environ["CSOCLASSIFY_SYNAP_DIR"] = env_dir
    assert r.returncode == errcodes.exit_of("synap_missing") == 3, \
        f"exit={r.returncode} :: {r.stderr[-300:]}"
    # 기계가 읽는 오류 줄에 이유 이름 · 번호가 있고, 문장에 무엇이 없는지(경로)가 있다.
    assert "synap_missing" in r.stdout and "1016" in r.stdout and "snf_exe" in r.stdout, r.stdout[-300:]


#------------------------------------------------------------------
# --hybridparse 에는 아무 말도 하지 않는다 (오탐 방지)
#=> 이 판이 늘 하는 일이라 요청이 이미 충족돼 있다. 경고를 내면 매 실행 소음이 된다.
#
# -in: tmp_path = pytest 임시 폴더
# -out: 없음(단언)
# -out: error = 불필요한 경고가 생기면 AssertionError
#------------------------------------------------------------------
def test_hybridparse는_조용하다(tmp_path):
    doc = tmp_path / "문서.txt"
    doc.write_text("월간 운영 보고" + chr(10) * 2 +
                   "정기 점검을 예정대로 수행했습니다." + chr(10), encoding="utf-8")
    rules = os.path.join(ROOT, "resources", "policy", "cso_rule.yaml")
    r = _run("--hybridparse", "--file", str(doc), "--cso-rules", rules,
             "--rule-only", "--format", "jsonl", "--nosummary", "--no-timing",
             state_dir=tmp_path / "state")
    assert r.returncode == 0, f"exit={r.returncode} :: {r.stderr[-300:]}"
    assert '"file"' in r.stdout, f"분류가 돌지 않았다 :: {r.stderr[-300:]}"
    assert "추출기" not in r.stderr and "synap" not in r.stderr
