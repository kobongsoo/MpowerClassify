#------------------------------------------------------------------
# 테스트 공통 설정
#=> 테스트가 설치 없이도 src/ 의 csoclassify 패키지를 import 할 수 있게 경로를 추가한다.
#   그리고 시험이 띄운 상주 데몬을 시험 전용 자리에 가두고, 끝나면 내린다(아래 _test_daemon_state).
#
# -in: 없음
#
# -out: 없음(sys.path 부작용)
# -out: error = 없음
#------------------------------------------------------------------

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))


#------------------------------------------------------------------
# 기록 파일에 적힌 데몬 프로세스 끝내기
#=> --stop 으로 내려가지 않은 데몬(다른 모델 · Rust 판 · 응답 없음)을 pid 로 직접 끝낸다.
#   기록 파일은 시험 전용 폴더에 있으므로, 여기 적힌 pid 는 시험이 띄운 데몬뿐이다.
#
# -in: path = daemon-*.json 기록 파일 경로
#
# -out: 없음
# -out: error = 없음(파일이 깨졌거나 프로세스가 이미 없으면 조용히 넘어간다)
#------------------------------------------------------------------
def _kill_recorded(path):
    try:
        with open(path, encoding="utf-8") as f:
            pid = int(json.load(f).get("pid") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return
    if pid <= 0 or pid == os.getpid():
        return
    if os.name == "nt":
        # 윈도우는 os.kill 이 TerminateProcess 라 자식까지는 못 끝낸다 — taskkill /T 로 함께 끝낸다
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=30)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


#------------------------------------------------------------------
# 시험이 띄운 상주 데몬을 시험 전용 자리에 두고, 끝나면 내린다 (세션 전체에 한 번)
#=> 분류·임베딩 시험은 --no-daemon 을 주지 않으면 데몬을 자동으로 띄운다. 그 데몬은
#   --idle-timeout 0(무한)이라 시험이 끝나도 남아, docparse.dll 같은 라이브러리를 잡고 있어
#   scripts/build/update-docparse.sh 가 '다른 프로세스가 사용 중'으로 실패했다(2026-11-18 실측 두 번).
#   또 사용자가 띄워 둔 진짜 데몬과 같은 기록 자리를 써서, 시험이 그 데몬을 빌려 쓰거나 내릴 수 있었다.
#    1) CSOCLASSIFY_STATE_DIR 을 시험 전용 임시 폴더로 — 이 프로세스와 시험이 띄우는 하위 프로세스
#       (파이썬 판 · Rust 판 모두 이 이름을 본다)가 그 폴더에 데몬 기록을 남긴다
#    2) 세션이 끝나면 그 폴더의 모델마다 --stop 을 보내 점잖게 내리고
#    3) 그래도 남은 기록의 pid 는 직접 끝낸다
#   이미 CSOCLASSIFY_STATE_DIR 을 정해 두고 돌리면(사람이 일부러) 그 값을 존중하고 정리도 하지 않는다.
#
# -in: 없음
#
# -out: 없음(yield 픽스처)
# -out: error = 없음(정리 실패는 시험 결과에 영향 주지 않음)
#------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _test_daemon_state():
    if os.environ.get("CSOCLASSIFY_STATE_DIR"):
        yield
        return
    state = tempfile.mkdtemp(prefix="cso-test-state-")
    os.environ["CSOCLASSIFY_STATE_DIR"] = state
    try:
        yield
    finally:
        env = dict(os.environ, PYTHONPATH=os.path.join(_ROOT, "src"), PYTHONIOENCODING="utf-8")
        # 파이썬 판 기록(daemon-<모델>.json)마다 그 모델의 데몬에 정지 요청을 보낸다
        for p in glob.glob(os.path.join(state, "daemon-*.json")):
            name = os.path.basename(p)[len("daemon-"):-len(".json")]
            if name.startswith("rs-"):
                continue   # Rust 판 기록은 아래에서 pid 로 정리한다
            try:
                subprocess.run([sys.executable, "-m", "csoclassify", "--stop", "--model", name],
                               cwd=_ROOT, env=env, capture_output=True, timeout=60)
            except (OSError, subprocess.SubprocessError):
                pass
        # 정지 요청을 못 받은 데몬(응답 없음 · Rust 판)은 pid 로 끝낸다
        for p in glob.glob(os.path.join(state, "daemon-*.json")):
            _kill_recorded(p)
        os.environ.pop("CSOCLASSIFY_STATE_DIR", None)
        # 시험 전용 폴더는 남길 것이 없다 — 지우지 못하는 파일(방금 끝난 프로세스가 잡던 것)은 두고 넘어간다
        shutil.rmtree(state, ignore_errors=True)
