"""화면이 기준 문서 제목(첫 줄)을 채우는 길 — gerunner.fill_seed_titles · titles_for_files.

실제 엔진(Rust 릴리스 빌드)을 불러 본다. 엔진이 없으면 건너뛴다.
"""
import json
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if os.path.join(ROOT, "ui") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "ui"))

import gerunner  # noqa: E402

RS_EXE = os.path.join(ROOT, "Rust", "target", "release", "MpowerClassify-rs.exe")
need_exe = pytest.mark.skipif(not os.path.isfile(RS_EXE), reason="Rust 릴리스 빌드가 없습니다")


#------------------------------------------------------------------
# 시험용 문서 만들기(첫 줄이 제목)
#
# -in: d     = 폴더
# -in: name  = 파일 이름
# -in: first = 첫 줄
#
# -out: str = 만든 파일 경로
# -out: error = 없음
#------------------------------------------------------------------
def _doc(d, name, first):
    p = os.path.join(str(d), name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(f"{first}\n둘째 줄 본문입니다.\n")
    return p


@need_exe
def test_저장된_기준_문서의_빈_제목을_채운다(tmp_path):
    a = _doc(tmp_path, "관리자매뉴얼.txt", "시스템 관리자 매뉴얼")
    b = _doc(tmp_path, "보고서.txt", "2026 실적 보고서")
    sp = tmp_path / "class_seed.jsonl"
    rows = [{"v": 3, "file": a, "doctype": ["DC_002_002"]},
            {"v": 3, "file": b, "doctype": ["DC_001_003"], "title": "이미 있는 제목"}]
    sp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    ok, msg = gerunner.fill_seed_titles([RS_EXE], str(sp))
    assert ok and "채움 1" in msg and "이미 있음 1" in msg
    got = {r["file"]: r.get("title") for r in map(json.loads, sp.read_text(encoding="utf-8").splitlines())}
    assert got[a] == "시스템 관리자 매뉴얼"
    assert got[b] == "이미 있는 제목"          # 있던 제목은 건드리지 않는다


@need_exe
def test_임시_파일의_제목만_뽑는다(tmp_path):
    a = _doc(tmp_path, "제안서.txt", "차세대 시스템 구축 제안서")
    ppt_like = _doc(tmp_path, "발표.xlsx", "첫 줄")      # 제목을 쓰지 않는 형식
    got = gerunner.titles_for_files([RS_EXE], [a, ppt_like, os.path.join(str(tmp_path), "없음.txt")])
    assert got == {a: "차세대 시스템 구축 제안서"}


def test_엔진_실행이_안_되면_실패를_돌려준다(tmp_path):
    sp = tmp_path / "class_seed.jsonl"
    sp.write_text("", encoding="utf-8")
    ok, msg = gerunner.fill_seed_titles([os.path.join(str(tmp_path), "없는엔진.exe")], str(sp))
    assert not ok and msg
    assert gerunner.titles_for_files([os.path.join(str(tmp_path), "없는엔진.exe")],
                                     [_doc(tmp_path, "a.txt", "제목")]) == {}
