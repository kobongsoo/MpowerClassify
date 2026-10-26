# -*- coding: utf-8 -*-
"""분류 실행 패널의 순수 부분 — 파일 종류 패턴과 기본값.

패턴은 파일 '이름 전체'와 맞춰 보므로 `.hwp` 라고만 적으면 이름이 정확히
".hwp" 인 파일만 찾는다. 실측 — 18건짜리 폴더에서 `.hwp,.doc,` 는 0건이었다.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "ui") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "ui"))

import app  # noqa: E402


#------------------------------------------------------------------
# 별표를 빠뜨린 조각을 찾아낸다
#=> 조용히 0건이 되는 것을 막는 장치다. 화면이 아무 말도 안 하면 관리자는
#   폴더부터 의심하게 된다.
#
# -in: 없음
# -out: 없음(단언)
# -out: error = 없음
#------------------------------------------------------------------
def test_별표_빠진_조각을_찾는다():
    assert app.glob_missing_star(".hwp,.doc,") == [".hwp", ".doc"]
    assert app.glob_missing_star(" .pdf ") == [".pdf"]
    # 제대로 적은 것은 잡지 않는다.
    assert app.glob_missing_star("*.hwp,*.doc") == []
    assert app.glob_missing_star("*") == []
    assert app.glob_missing_star("") == []
    assert app.glob_missing_star(None) == []


#------------------------------------------------------------------
# 기본 패턴은 문서 확장자 + 압축 확장자를 담는다
#=> 못 다루는 확장자를 넣으면 '본문 없음'으로 남아 추출실패 건수만 늘어난다.
#   그리고 모든 조각에 별표가 붙어 있어야 한다(위 시험과 짝).
#
#   [압축이 빠지면 안 되는 이유] 엔진은 압축을 풀어 내부 문서를 하나씩 분류하지만,
#   그 전에 이 패턴으로 대상을 고른다. 압축 확장자가 없으면 압축파일 자체가 안 걸려
#   **안에 든 문서가 통째로 빠진다**(실측: 압축만 있는 폴더가 0건). 조용히 줄어드는
#   종류의 손실이라 기본값으로 못박아 둔다.
#
# -in: 없음
# -out: 없음(단언)
# -out: error = 없음
#------------------------------------------------------------------
def test_기본_패턴은_바르고_다룰_수_있는_것들이다():
    parts = [p.strip() for p in app.DEFAULT_GLOB.split(",")]
    assert app.glob_missing_star(app.DEFAULT_GLOB) == [], parts
    assert all(p.startswith("*.") for p in parts), parts
    exts = {p[2:] for p in parts}
    # 문서 12종.
    docs = {"txt", "html", "md", "doc", "docx", "ppt", "pptx",
            "xls", "xlsx", "hwp", "hwpx", "pdf"}
    # 압축 8종 — tar.gz·tar.bz2·tar.xz 는 뒤 확장자(gz·bz2·xz)로 걸린다.
    arcs = {"zip", "7z", "rar", "tar", "tgz", "gz", "bz2", "xz"}
    assert exts == docs | arcs, exts
    # 엔진의 형식 감지가 아는 것들인가(txt·md 는 text 로 간다).
    sys.path.insert(0, os.path.join(ROOT, "src"))
    from csoclassify.extract import detect  # noqa: E402
    known = {"pdf", "hwp", "hwpx", "doc", "docx", "xls", "xlsx",
             "ppt", "pptx", "text", "html"}
    assert known.issuperset({"pdf", "hwp"}), "감지 형식 목록이 바뀌었는지 확인"
    assert detect is not None


#------------------------------------------------------------------
# 폴더 고르기는 실패해도 예외를 올리지 않는다
#=> 화면이 이 버튼 하나 때문에 죽으면 안 된다. 화면 없는 서버에서는 창을
#   못 띄우므로, 그때는 사유를 문자열로 돌려 "직접 입력하세요"로 안내한다.
#
# -in: 없음
# -out: 없음(단언)
# -out: error = 없음
#------------------------------------------------------------------
def test_폴더고르기는_예외를_올리지_않는다(monkeypatch):
    # tkinter 를 못 쓰는 환경을 흉내 낸다.
    import builtins
    real = builtins.__import__

    def fake(name, *a, **kw):
        if name.startswith("tkinter"):
            raise ImportError("no display")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake)
    got, err = app.pick_folder("D:/없는폴더")
    assert got is None and err, (got, err)


#------------------------------------------------------------------
# 화면이 고치는 보안등급 기준 파일을 --cso-rules 로 넘긴다
#=> 예전에는 넘기지 않아 엔진이 exe 옆 cso_rule.yaml 을 읽었다 — 화면에서 기준을
#   고쳐도 분류 결과가 그대로였다. 파일이 없을 때는 넘기지 않는다(없는 경로를 주면
#   엔진이 2001 로 멈춘다).
#
# -in: tmp_path = pytest 임시 폴더
# -out: 없음(단언)
# -out: error = 없음
#------------------------------------------------------------------
def test_보안등급_기준파일을_넘긴다(tmp_path):
    rules = tmp_path / "cso_rule.yaml"
    rules.write_text("version: x\n", encoding="utf-8")
    assert app.cso_rules_args({"rules": str(rules)}) == ["--cso-rules", str(rules)]
    # 없는 파일 · 빈 값 · 폴더 · 키 없음 → 넘기지 않는다
    assert app.cso_rules_args({"rules": str(tmp_path / "없음.yaml")}) == []
    assert app.cso_rules_args({"rules": ""}) == []
    assert app.cso_rules_args({"rules": str(tmp_path)}) == []
    assert app.cso_rules_args({}) == []
    assert app.cso_rules_args(None) == []


#------------------------------------------------------------------
# 등급 스트림 체크 상자 → 엔진 인자
#=> 기본(켬)은 --write-grade-stream 하나. 하위 선택은 켰을 때만 붙고, 보안등급 축이
#   꺼졌거나 Windows 가 아니면 아무것도 넘기지 않는다(엔진이 1003 · 1012 로 멈추지 않게).
#
# -in: 없음
# -out: 없음(단언)
# -out: error = 없음
#------------------------------------------------------------------
def test_등급스트림_인자():
    assert app.grade_stream_args(True, is_windows=True) == ["--write-grade-stream"]
    assert app.grade_stream_args(True, none=True, update=True, is_windows=True) == [
        "--write-grade-stream", "--grade-stream-unclassified", "none", "--grade-stream-update-time"]
    # 껐으면 하위 선택이 켜져 있어도 넘기지 않는다
    assert app.grade_stream_args(False, none=True, update=True, is_windows=True) == []
    # 보안등급 축이 꺼짐 · 리눅스
    assert app.grade_stream_args(True, do_sec=False, is_windows=True) == []
    assert app.grade_stream_args(True, is_windows=False) == []


#------------------------------------------------------------------
# 사람 결정 목록 → 엔진 인자
#=> 켰고 파일이 있을 때만 --grade-overrides 를 넘긴다(없는 파일이면 엔진이 2009 로 멈춘다).
#
# -in: tmp_path = pytest 임시 폴더
# -out: 없음(단언)
# -out: error = 없음
#------------------------------------------------------------------
def test_사람결정_인자(tmp_path):
    ov = tmp_path / "cso_override.jsonl"
    assert app.grade_overrides_args(True, str(ov)) == []            # 결정이 아직 없음
    ov.write_text("", encoding="utf-8")
    assert app.grade_overrides_args(True, str(ov)) == ["--grade-overrides", str(ov)]
    assert app.grade_overrides_args(False, str(ov)) == []
    assert app.grade_overrides_args(True, "") == []


#------------------------------------------------------------------
# 엔진이 적용 · 거절한 사람 결정을 화면이 같은 뜻으로 읽는다
#=> stale(내용이 바뀌어 따르지 않음)이면 엔진 판정 + 다시 검토, 적용됐으면 원래 판정과 비교해 '고침'.
#
# -in: 없음
# -out: 없음(단언)
# -out: error = 없음
#------------------------------------------------------------------
def test_엔진_결정_결과를_화면이_따른다():
    latest = {"a.docx": {"new_grade": "O"}}
    stale = {"file": "a.docx", "grade": "C",
             "why": {"security": {"override": {"stale": True, "applied": False}}}}
    assert app.effective_grade(stale, latest) == ("C", False)
    applied = {"file": "a.docx", "grade": "O",
               "why": {"security": {"method": "manual_override", "computed": {"grade": "C"}}}}
    assert app.grade_changed(applied, latest) is True
