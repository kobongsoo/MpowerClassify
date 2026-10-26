# -*- coding: utf-8 -*-
"""사람이 정한 보안등급(--grade-overrides · --set-grade) — Rust 판과 같은 계약.

설계: plan/사람등급지정-수동오버라이드-설계-20261020.html
  · UI 의 cso_override.jsonl 을 그대로 읽는다 — security 줄만, 같은 문서는 나중 줄
  · 찾는 순서: doc_id → 내용 지문 → 경로 → 정규화 경로 → 대소문자 무시
  · 결정 뒤 내용이 바뀌었으면 더 엄격한 결정만 따른다(나머지는 stale)
  · --set-grade 는 문서를 읽지 않고 등급을 정하고, 원하면 목록에 한 줄 덧붙인다
  · 인자 조합 오류는 문서를 읽기 전에 1003 · 1006 · 2009
"""
import hashlib
import json
import os
import subprocess
import sys

import pytest

from csoclassify import errcodes, gradeoverride as go

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIN = os.name == "nt"
win_only = pytest.mark.skipif(not WIN, reason="파일 스트림 확인은 Windows(NTFS)에서만")

# 규칙만으로 S 가 나오는 본문과 신호가 없는 본문(test_grade_stream.py 와 같다)
BODY_S = ("고객 명단\n홍길동 주민등록번호 900101-1234567 연락처 010-1234-5678\n"
          "김철수 주민등록번호 850505-2345678 연락처 010-9876-5432\n")
BODY_NONE = "사내 동호회 운영 안내입니다. 동호회 활동비는 분기마다 지급합니다.\n"


#------------------------------------------------------------------
# 파이썬 판 한 번 실행
#
# -in: tmp_path = 시험 폴더 · args = 명령줄 인자
#
# -out: CompletedProcess
# -out: error = 없음
#------------------------------------------------------------------
def _run(tmp_path, *args):
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"), PYTHONIOENCODING="utf-8",
               CSOCLASSIFY_LOGDIR=str(tmp_path / "log"))
    return subprocess.run([sys.executable, "-m", "csoclassify", *args, "--json-errors"], cwd=ROOT,
                          env=env, capture_output=True, text=True, encoding="utf-8")


#------------------------------------------------------------------
# jsonl 결과 읽기 — (파일 이름 → 레코드, 요약)
#
# -in: path = 결과 파일
#
# -out: (dict, dict)
# -out: error = 없음
#------------------------------------------------------------------
def _read(path):
    recs, summ = {}, {}
    for line in open(path, encoding="utf-8"):
        d = json.loads(line)
        if "summary" in d:
            summ = d["summary"]
        elif "file" in d:
            recs[os.path.basename(d["file"].replace("/", os.sep))] = d
    return recs, summ


#------------------------------------------------------------------
# 파일 지문
#
# -in: p = 경로
#
# -out: str = sha256 hex
# -out: error = 없음
#------------------------------------------------------------------
def _h(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def test_정규화는_UI_와_같다():
    assert go.normalize_key("c:\\a\\b\\..\\c.docx") == "C:/a/c.docx"
    assert go.normalize_key("D:/x//y/./z.txt") == "D:/x/y/z.txt"
    assert go.normalize_key("\\\\srv\\share\\a.doc") == "//srv/share/a.doc"


def test_목록_읽기는_security_줄만_나중_줄_우선(tmp_path):
    p = tmp_path / "ov.jsonl"
    p.write_text("\n".join([
        json.dumps({"file": "a", "new_grade": "C"}),                         # axis 없음 = 옛 security
        json.dumps({"file": "a", "axis": "security", "new_grade": "S"}),      # 나중 줄이 이긴다
        json.dumps({"file": "b", "axis": "doctype", "new_grade": "C"}),       # 업무분류 줄은 건너뜀
        "깨진 줄",
    ]) + "\n", encoding="utf-8")
    idx = go.load(str(p))
    assert set(idx.latest) == {"a"} and idx.latest["a"]["new_grade"] == "S" and idx.broken == 1


def test_찾는_순서():
    idx = go.OverrideIndex({
        "D:/x/doc1.txt": {"new_grade": "C", "doc_id": "SF1"},
        "D:/x/doc2.txt": {"new_grade": "S", "hash": "h2"},
        "D:/x/Doc3.txt": {"new_grade": "O"},
    })
    assert idx.lookup({"file": "zzz", "doc_id": "SF1"})[1] == "doc_id"
    assert idx.lookup({"file": "옮긴곳/doc2.txt", "hash": "h2"})[1] == "hash"
    assert idx.lookup({"file": "d:\\x\\doc1.txt"})[1] == "path"
    assert idx.lookup({"file": "D:/X/DOC3.TXT"})[1] == "case"
    assert idx.lookup({"file": "D:/x/none.txt"}) == (None, None)


def test_적용_규칙():
    r = {"file": "a", "grade": "O", "hash": "h1", "why": {"security": {"method": "rule"}}}
    assert go.apply(r, {"new_grade": "C", "hash": "h1"}, "hash") == "applied"
    assert r["grade"] == "C" and r["why"]["security"]["computed"]["grade"] == "O"
    # 내용이 바뀌었고 사람 등급이 더 느슨하면 따르지 않는다
    r = {"file": "a", "grade": "C", "hash": "h2"}
    assert go.apply(r, {"new_grade": "O", "hash": "h1"}, "path") == "stale"
    assert r["grade"] == "C" and r["why"]["security"]["override"]["stale"] is True
    # 내용이 바뀌었어도 더 엄격하면 따른다
    r = {"file": "a", "grade": "O", "hash": "h2"}
    assert go.apply(r, {"new_grade": "C", "hash": "h1"}, "path") == "applied" and r["grade"] == "C"
    # 확인만 한 결정은 등급을 바꾸지 않는다 · 보류는 미분류
    r = {"file": "a", "grade": "S"}
    assert go.apply(r, {"new_grade": "S", "kind": "confirm"}, "path") == "confirmed" and r["grade"] == "S"
    r = {"file": "a", "grade": "S"}
    go.apply(r, {"new_grade": "보류"}, "path")
    assert r["grade"] is None


def test_인자_오류(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text(BODY_NONE, encoding="utf-8")
    r = _run(tmp_path, "--set-grade", "X", "--file", str(p))
    assert r.returncode == 3 and "bad_args" in r.stdout, r.stdout
    r = _run(tmp_path, "--set-grade", "C", "--dir", str(tmp_path))
    assert r.returncode == 3 and "mode_conflict" in r.stdout, r.stdout
    r = _run(tmp_path, "--set-grade", "C", "--file", str(p), "--text-only")
    assert r.returncode == 3 and "mode_conflict" in r.stdout, r.stdout
    # 목록에 덧붙이는데 검토자 · 사유가 없다
    r = _run(tmp_path, "--set-grade", "C", "--file", str(p), "--grade-overrides", str(tmp_path / "ov.jsonl"))
    assert r.returncode == 3 and "bad_args" in r.stdout, r.stdout
    # 검토자만 따로
    r = _run(tmp_path, "--file", str(p), "--reviewer", "a")
    assert r.returncode == 3 and "bad_args" in r.stdout, r.stdout
    # 분류에 없는 목록
    r = _run(tmp_path, "--file", str(p), "--grade-overrides", str(tmp_path / "없음.jsonl"))
    assert r.returncode == errcodes.exit_of("grade_overrides_missing") == 3
    assert "2009" in r.stdout, r.stdout


def test_set_grade_없는_파일은_3004(tmp_path):
    r = _run(tmp_path, "--set-grade", "C", "--file", str(tmp_path / "없음.txt"), "--format", "jsonl")
    assert r.returncode == 1, r.stderr[-300:]
    assert "file_missing" in r.stdout


@win_only
def test_set_grade_와_재분류_유지(tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    s = d / "s.txt"
    n = d / "none.txt"
    s.write_text(BODY_S, encoding="utf-8")
    n.write_text(BODY_NONE, encoding="utf-8")
    ov = tmp_path / "ov.jsonl"
    # ① 미분류 문서를 C 로 바로 정하고 목록에 남긴다
    r = _run(tmp_path, "--set-grade", "C", "--file", str(n), "--write-grade-stream",
             "--grade-overrides", str(ov), "--reviewer", "홍길동", "--reason", "시험", "--format", "jsonl")
    assert r.returncode == 0, r.stderr[-400:]
    rec = json.loads(r.stdout.splitlines()[0])
    assert rec["grade"] == "C" and rec["why"]["security"]["method"] == "manual_override"
    assert rec["grade_stream"]["state"] == "written"
    entry = json.loads(ov.read_text(encoding="utf-8").splitlines()[-1])
    assert entry["new_grade"] == "C" and entry["reviewer"] == "홍길동" and entry["hash"] == _h(n)
    # ② S 문서는 내용이 바뀐 뒤의 O 결정이라 따르지 않는다(stale)
    with open(ov, "a", encoding="utf-8") as f:
        f.write(json.dumps({"file": str(s), "hash": "0" * 64, "axis": "security", "kind": "change",
                            "new_grade": "O"}, ensure_ascii=False) + "\n")
    out = tmp_path / "r.jsonl"
    r = _run(tmp_path, "--dir", str(d), "--rule-only", "--no-timing", "--format", "jsonl",
             "--grade-overrides", str(ov), "--write-grade-stream", "--out", str(out))
    assert r.returncode == 0, r.stderr[-400:]
    recs, summ = _read(out)
    assert recs["none.txt"]["grade"] == "C"
    assert recs["none.txt"]["why"]["security"]["override"]["matched_by"] == "hash"
    assert recs["none.txt"]["grade_stream"]["state"] == "unchanged"
    assert recs["s.txt"]["grade"] == "S" and recs["s.txt"]["why"]["security"]["override"]["stale"] is True
    assert summ["grade_overrides"] == {"loaded": 2, "applied": 1, "confirmed": 0, "stale": 1}
    assert summ["C"] == 1 and summ["S"] == 1 and summ["unclassified"] == 0


def test_옵션을_안_주면_결과가_같다(tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    (d / "s.txt").write_text(BODY_S, encoding="utf-8")
    out = tmp_path / "r.jsonl"
    r = _run(tmp_path, "--dir", str(d), "--rule-only", "--no-timing", "--format", "jsonl", "--out", str(out))
    assert r.returncode == 0
    recs, summ = _read(out)
    assert "grade_overrides" not in summ
    assert "override" not in (recs["s.txt"].get("why") or {}).get("security", {})


#------------------------------------------------------------------
# --set-grade 를 목록(--filelist)의 문서 전부에
#=> 설계 §04 '여러 건'. 목록의 문서마다 같은 등급 · 결정 목록에 한 줄씩 · doc_id 는 sfile_id.
#   없는 파일도 결과에 남기고(file_missing), 종료코드는 처음 난 실패(없는 파일 → 1)를 따른다.
#   Rust 판 cli_grade_override.rs 의 set_grade_목록_여러건 과 같은 경우다.
#
# -in: tmp_path = pytest 임시폴더 픽스처
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_set_grade_목록_여러건(tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    a, b = d / "a.txt", d / "b.txt"
    a.write_text(BODY_NONE, encoding="utf-8")
    b.write_text(BODY_S, encoding="utf-8")
    gone = d / "c_없음.txt"
    fl = tmp_path / "list.jsonl"
    fl.write_text("".join(json.dumps({"path": str(p), "sfile_id": sid}, ensure_ascii=False) + "\n"
                          for p, sid in ((b, "SF-2"), (gone, "SF-3"), (a, "SF-1"))), encoding="utf-8")
    ov = tmp_path / "ov.jsonl"
    ov.write_text("", encoding="utf-8")
    r = _run(tmp_path, "--set-grade", "S", "--filelist", str(fl), "--grade-overrides", str(ov),
             "--reviewer", "홍길동", "--reason", "일괄 지정", "--format", "jsonl")
    # 없는 파일이 하나 있어 종료코드 1 — 그래도 나머지 두 건은 끝까지 처리한다
    assert r.returncode == 1, r.stderr[-400:]
    recs = [json.loads(x) for x in r.stdout.splitlines() if x.strip().startswith('{"file"')]
    assert [os.path.basename(x["file"]) for x in recs] == ["a.txt", "b.txt", "c_없음.txt"]
    assert [x.get("doc_id") for x in recs] == ["SF-1", "SF-2", "SF-3"]
    assert recs[0]["grade"] == "S" and recs[1]["grade"] == "S"
    assert recs[2]["grade"] is None and recs[2]["error"]["kind"] == "file_missing"
    lines = [json.loads(x) for x in ov.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert sorted(os.path.basename(x["file"]) for x in lines) == ["a.txt", "b.txt"]
    assert all(x["new_grade"] == "S" and x["reviewer"] == "홍길동" for x in lines)
    assert '"kind": "file_missing"' in r.stdout or '"kind":"file_missing"' in r.stdout


#------------------------------------------------------------------
# --set-grade 대상 없음 · --filelist 도 없으면 1003
#
# -in: tmp_path = pytest 임시폴더 픽스처
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_set_grade_대상이_없으면_1003(tmp_path):
    r = _run(tmp_path, "--set-grade", "C")
    assert r.returncode == 3 and "bad_args" in r.stdout, r.stdout


#------------------------------------------------------------------
# 등급을 새로 쓰면 탐색기에 알린다 · 같은 값이면 알리지 않는다
#=> SHChangeNotify 를 실제로 확인할 방법이 없어 notify_shell 을 바꿔 끼워 불린 횟수를 센다.
#
# -in: tmp_path    = pytest 임시폴더 픽스처
# -in: monkeypatch = pytest 바꿔 끼우기 픽스처
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
@win_only
def test_등급을_쓰면_탐색기에_알린다(tmp_path, monkeypatch):
    from csoclassify import gradestream
    p = tmp_path / "doc.txt"
    p.write_text(BODY_NONE, encoding="utf-8")
    called = []
    monkeypatch.setattr(gradestream, "notify_shell", lambda path: called.append(path))
    assert gradestream.mark(str(p), "C")["state"] == "written"
    assert called == [str(p)]
    # 같은 값을 다시 쓰면 unchanged — 파일도 탐색기도 건드리지 않는다
    assert gradestream.mark(str(p), "C")["state"] == "unchanged"
    assert called == [str(p)]
    # 진짜 함수도 예외 없이 끝나야 한다(탐색기가 없어도)
    monkeypatch.undo()
    gradestream.notify_shell(str(p))
