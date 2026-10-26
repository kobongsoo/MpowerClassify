# -*- coding: utf-8 -*-
"""파이썬 판 사이냅 인자 — Rust 판과 같은 이름 · 같은 오류 코드.

설계: plan/Rust-사이냅추출기-옵션-설계-20261019.html
  · --synap-fallback · --synap-only 를 명시했는데 snf_exe 가 없으면 문서를 읽기 전에 1016
  · 두 옵션을 함께 주면 1006
  · --synap-path 로 snf_exe 를 직접 가리킬 수 있다
  · 복호화로 풀린 문서면 사이냅 출력파일을 그 문서 폴더(cso_dec_*) 안에 둔다
가짜 snf 는 Rust 판 시험용 예제(Rust/target/*/examples/fake_snf)를 빌려 쓴다 — 없으면 그 시험만 건너뛴다.
"""
import os
import subprocess
import sys

import pytest

from csoclassify import errcodes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXE = "fake_snf.exe" if os.name == "nt" else "fake_snf"
FAKE_SNF = next((p for p in (os.path.join(ROOT, "Rust", "target", d, "examples", _EXE)
                             for d in ("debug", "release")) if os.path.isfile(p)), None)
BODY = "월간 운영 보고서입니다. 정기 점검을 예정대로 수행했고 특이사항은 없습니다.\n"
MARK = "사이냅이 읽은 본문입니다"


#------------------------------------------------------------------
# 파이썬 판을 한 번 돌린다 — 사이냅 찾는 곳을 빈 폴더로 막아 이 PC 의 사이냅을 피한다
#
# -in: tmp_path = 시험 폴더 · args = 명령줄 인자
#
# -out: CompletedProcess
# -out: error = 없음
#------------------------------------------------------------------
def _run(tmp_path, *args):
    empty = tmp_path / "no_synap"
    empty.mkdir(exist_ok=True)
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"), PYTHONIOENCODING="utf-8",
               CSOCLASSIFY_LOGDIR=str(tmp_path / "log"), CSOCLASSIFY_SYNAP_DIR=str(empty))
    return subprocess.run([sys.executable, "-m", "csoclassify", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, encoding="utf-8")


def _doc(tmp_path):
    p = tmp_path / "보고서.txt"
    p.write_text(BODY, encoding="utf-8")
    return str(p)


@pytest.mark.parametrize("flag", ["--synap-only", "--synap-fallback"])
def test_사이냅을_골랐는데_없으면_읽기_전에_1016(tmp_path, flag):
    # 소스 실행이면 저장소의 resources/synap 에 진짜 사이냅이 있을 수 있다 — 없는 파일을 직접 가리켜 '없음'을 만든다.
    missing = str(tmp_path / "없는" / "snf_exe.exe")
    r = _run(tmp_path, "--text-only", "--file", _doc(tmp_path), flag, "--synap-path", missing, "--json-errors")
    assert r.returncode == errcodes.exit_of("synap_missing") == 3, r.stderr[-400:]
    assert "synap_missing" in r.stdout and "1016" in r.stdout, r.stdout
    assert "월간 운영 보고서" not in r.stdout, "문서를 읽었다"


def test_두_옵션을_함께_주면_1006(tmp_path):
    r = _run(tmp_path, "--text-only", "--file", _doc(tmp_path), "--synap-only", "--synap-fallback",
             "--json-errors")
    assert r.returncode == 3
    assert "mode_conflict" in r.stdout, r.stdout


def test_기본은_사이냅이_없어도_그대로_돈다(tmp_path):
    # 명시하지 않은 기본 실행은 지금처럼 '있으면 폴백, 없으면 자체 파서만'이다.
    r = _run(tmp_path, "--text-only", "--file", _doc(tmp_path))
    assert r.returncode == 0, r.stderr[-400:]
    assert "월간 운영 보고서" in r.stdout


@pytest.mark.skipif(FAKE_SNF is None, reason="가짜 snf(Rust examples) 없음")
def test_synap_path_로_직접_가리킨다(tmp_path):
    r = _run(tmp_path, "--text-only", "--file", _doc(tmp_path), "--synap-only", "--synap-path", FAKE_SNF)
    assert r.returncode == 0, r.stderr[-400:]
    assert MARK in r.stdout, r.stdout


def test_복호화_문서면_사이냅_출력파일을_그_문서_폴더에_둔다(tmp_path):
    from csoclassify.extract.synap_exe import SynapExeExtractor
    ext = SynapExeExtractor.__new__(SynapExeExtractor)   # snf 존재 확인을 건너뛰고 경로 규칙만 본다
    ext.work_dir = str(tmp_path / "sys_tmp")
    dec = tmp_path / "cso_dec_123" / "000001" / "000001.docx"
    out, keep = ext._decide_output(str(dec), None)
    assert os.path.dirname(out) == str(dec.parent) and keep is False
    plain = tmp_path / "docs" / "a.docx"
    out, _ = ext._decide_output(str(plain), None)
    assert os.path.dirname(out) == ext.work_dir
