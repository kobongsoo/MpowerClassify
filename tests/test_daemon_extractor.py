#------------------------------------------------------------------
# 데몬 추출기 선택 시험
#=> --embed --daemon 경로에서 데몬이 클라이언트의 --hybridparse · --extractor 를
#   따르는지 본다. 예전에는 데몬이 사이냅 단독으로 고정돼 두 옵션이 무시됐다.
#------------------------------------------------------------------

import argparse

import pytest

from csoclassify import cli, config
from csoclassify.daemon import server as srv
from csoclassify.extract.base import ExtractError


#------------------------------------------------------------------
# build_extractor 대역 설치
#=> 실제 사이냅·docparse 없이 "어떤 설정으로 몇 번 만들었나"만 기록한다.
#
# -in: monkeypatch = pytest 픽스처
#
# -out: calls = (hybrid, extractor) 호출 기록 리스트
# -out: error = 없음
#------------------------------------------------------------------
def _fake_build(monkeypatch):
    calls = []

    # 호출 인자를 기록하고 그 조합을 나타내는 표식 객체를 돌려준다
    def fake(*, hybrid=False, extractor=None):
        calls.append((hybrid, extractor))
        return ("EX", hybrid, extractor)

    monkeypatch.setattr(srv, "build_extractor", fake)
    return calls


#------------------------------------------------------------------
# 요청 설정대로 추출기를 고르는지
#=> hybrid+rust · hybrid+py · 사이냅 단독이 각각 다른 추출기가 되어야 한다.
#
# -in: monkeypatch = pytest 픽스처
#
# -out: 없음
# -out: error = 단언 실패 시 AssertionError
#------------------------------------------------------------------
def test_extractor_follows_request(monkeypatch):
    calls = _fake_build(monkeypatch)
    s = srv.DaemonServer(spec=None)
    assert s._extractor_for({"hybridparse": True, "extractor": "rust"}) == ("EX", True, "rust")
    assert s._extractor_for({"hybridparse": True, "extractor": "py"}) == ("EX", True, "py")
    assert s._extractor_for({"hybridparse": False, "extractor": "rust"}) == ("EX", False, "rust")
    assert len(calls) == 3


#------------------------------------------------------------------
# 같은 설정은 한 번만 만드는지 · 사이냅 단독은 py/rust 를 가르지 않는지
#
# -in: monkeypatch = pytest 픽스처
#
# -out: 없음
# -out: error = 단언 실패 시 AssertionError
#------------------------------------------------------------------
def test_extractor_cached(monkeypatch):
    calls = _fake_build(monkeypatch)
    s = srv.DaemonServer(spec=None)
    a = s._extractor_for({"hybridparse": True, "extractor": "rust"})
    b = s._extractor_for({"hybridparse": True, "extractor": "RUST "})
    assert a is b
    # 사이냅 단독이면 자체 파서를 안 쓰므로 extractor 값이 달라도 같은 추출기다
    s._extractor_for({"hybridparse": False, "extractor": "py"})
    s._extractor_for({"hybridparse": False, "extractor": "rust"})
    assert len(calls) == 2


#------------------------------------------------------------------
# 옛 클라이언트(설정 없는 요청)는 예전처럼 사이냅 단독
#
# -in: monkeypatch = pytest 픽스처
#
# -out: 없음
# -out: error = 단언 실패 시 AssertionError
#------------------------------------------------------------------
def test_old_client_defaults_to_synap(monkeypatch):
    calls = _fake_build(monkeypatch)
    srv.DaemonServer(spec=None)._extractor_for({})
    assert calls == [(False, "py")]


#------------------------------------------------------------------
# 만들기 실패는 기억하지 않는다 — 고친 뒤 다음 요청에서 다시 시도
#
# -in: monkeypatch = pytest 픽스처
#
# -out: 없음
# -out: error = 단언 실패 시 AssertionError
#------------------------------------------------------------------
def test_failure_not_cached(monkeypatch):
    n = []

    # 첫 번째는 실패, 두 번째부터 성공하는 대역
    def flaky(*, hybrid=False, extractor=None):
        n.append(1)
        if len(n) == 1:
            raise ExtractError("docparse 없음")
        return "EX"

    monkeypatch.setattr(srv, "build_extractor", flaky)
    s = srv.DaemonServer(spec=None)
    with pytest.raises(ExtractError):
        s._extractor_for({"hybridparse": True, "extractor": "rust"})
    assert s._extractor_for({"hybridparse": True, "extractor": "rust"}) == "EX"


#------------------------------------------------------------------
# 클라이언트가 고른 자체 파서를 요청 옵션에 싣는지
#=> --extractor 가 있으면 그 값, 없으면 config.EXTRACTOR.
#
# -in: monkeypatch = pytest 픽스처
#
# -out: 없음
# -out: error = 단언 실패 시 AssertionError
#------------------------------------------------------------------
def test_make_opts_carries_extractor(monkeypatch):
    base = dict(max_tokens=512, overlap=64, normalize=True, per_chunk=False,
                precision="fp32", textsave=None, hybridparse=True, max_chars=None,
                no_size_limit=False)
    monkeypatch.setattr(config, "EXTRACTOR", "py")
    assert cli.make_opts(argparse.Namespace(extractor="rust", **base))["extractor"] == "rust"
    assert cli.make_opts(argparse.Namespace(extractor=None, **base))["extractor"] == "py"
    monkeypatch.setattr(config, "EXTRACTOR", "rust")
    assert cli.make_opts(argparse.Namespace(extractor=None, **base))["extractor"] == "rust"
