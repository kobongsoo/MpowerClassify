# -*- coding: utf-8 -*-
"""등급 스트림(:mgrade) 기록 — NEOGradeToolApp 과 같은 12바이트, Rust 판과 같은 계약.

설계: plan/등급스트림-기록-옵션-설계-20261019.html
  · 12바이트 = "MGRD" · 버전 1 · 등급(NONE 0 · C 1 · S 2 · O 3), 리틀엔디언
  · 같은 값이면 쓰지 않는다(unchanged) · 새 버전 스트림은 덮어쓰지 않는다(newer_version)
  · 읽기 전용은 잠시 풀고 되돌린다 · 시각은 기본으로 되돌리고, --grade-stream-update-time 이면 바뀌게 둔다
  · 압축 안 문서는 건너뛰고 압축 자체에 집계 등급 · 못 읽은 문서는 건너뜀
  · 인자 조합 오류는 문서를 읽기 전에 1003 · 1006 · 1012
"""
import ctypes
import json
import os
import stat
import subprocess
import sys
import zipfile

import pytest

from csoclassify import errcodes, gradestream as gs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIN = os.name == "nt"
win_only = pytest.mark.skipif(not WIN, reason="대체 데이터 스트림은 Windows(NTFS)에만 있다")

# 규칙만으로 S 가 나오는 본문(주민번호 · 연락처)과 신호가 없는 본문
BODY_S = ("고객 명단\n홍길동 주민등록번호 900101-1234567 연락처 010-1234-5678\n"
          "김철수 주민등록번호 850505-2345678 연락처 010-9876-5432\n")
BODY_NONE = "사내 동호회 운영 안내입니다. 동호회 활동비는 분기마다 지급합니다.\n"


#------------------------------------------------------------------
# 파이썬 판을 한 번 돌린다 — 규칙만, 결과는 jsonl 파일로
#
# -in: tmp_path = 시험 폴더 · args = 명령줄 인자
#
# -out: (CompletedProcess, 레코드 목록, 요약 dict)
# -out: error = 없음
#------------------------------------------------------------------
def _run(tmp_path, *args, out=True):
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"), PYTHONIOENCODING="utf-8",
               CSOCLASSIFY_LOGDIR=str(tmp_path / "log"))
    o = tmp_path / "result.jsonl"
    extra = ["--rule-only", "--no-timing", "--json-errors", "--out", str(o)] if out else ["--json-errors"]
    r = subprocess.run([sys.executable, "-m", "csoclassify", *args, *extra], cwd=ROOT, env=env,
                       capture_output=True, text=True, encoding="utf-8")
    recs, summ = [], {}
    if out and o.exists():
        for line in o.read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            if "summary" in d:
                summ = d["summary"]
            elif "file" in d:
                recs.append(d)
    return r, recs, summ


#------------------------------------------------------------------
# 스트림 내용 바로 읽기(시험이 판정기를 거치지 않고 바이트를 본다)
#
# -in: p = 문서 경로
#
# -out: bytes | None = 스트림 내용(없으면 None)
# -out: error = 없음
#------------------------------------------------------------------
def _raw(p):
    try:
        with open(str(p) + ":" + gs.STREAM_NAME, "rb") as f:
            return f.read()
    except OSError:
        return None


def test_12바이트_배치는_툴과_같다():
    assert gs.payload(1) == bytes.fromhex("4D475244" "01000000" "01000000")
    assert gs.payload(0)[8:] == b"\0\0\0\0"
    assert gs.payload(3)[:4] == b"MGRD"


@win_only
def test_경로_규칙은_툴의_BuildGradeStreamPath_와_같다():
    assert gs.stream_path(r"C:\a\b.docx") == r"\\?\C:\a\b.docx:mgrade"
    assert gs.stream_path(r"\\srv\sh\b.docx") == r"\\?\UNC\srv\sh\b.docx:mgrade"
    assert gs.stream_path(r"\\?\C:\a\b.docx") == r"\\?\C:\a\b.docx:mgrade"
    assert gs.stream_path("C:/a/b.docx") == r"\\?\C:\a\b.docx:mgrade"


@win_only
def test_쓰고_읽고_같으면_다시_안_쓴다(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("x", encoding="utf-8")
    assert gs.read_state(str(p))[0] == gs.ST_NO_STREAM
    assert gs.mark(str(p), "C") == {"state": "written", "value": "C"}
    assert _raw(p) == gs.payload(1)
    assert gs.read_state(str(p))[:2] == (gs.ST_MARKED, 1)
    # 같은 값이면 쓰지 않는다 — 수정 시각도 그대로
    mt = os.stat(p).st_mtime_ns
    assert gs.mark(str(p), "C")["state"] == "unchanged"
    assert os.stat(p).st_mtime_ns == mt
    # 다른 값이면 덮어쓰고 이전 값을 남긴다
    assert gs.mark(str(p), "NONE") == {"state": "written", "value": "NONE", "prev": "C"}
    assert gs.read_state(str(p))[:2] == (gs.ST_UNCLASSIFIED, 0)
    # 본문은 그대로다
    assert p.read_text(encoding="utf-8") == "x"


@win_only
def test_새_버전_스트림은_덮어쓰지_않는다(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("x", encoding="utf-8")
    newer = bytes.fromhex("4D475244" "02000000" "01000000") + b"\x09\x09"
    with open(str(p) + ":mgrade", "wb") as f:
        f.write(newer)
    assert gs.read_state(str(p))[0] == gs.ST_VERSION_UNSUPPORTED
    assert gs.mark(str(p), "S") == {"state": "skipped", "value": "S", "reason": "newer_version"}
    assert _raw(p) == newer


@win_only
def test_형식이_틀린_스트림은_고쳐_쓴다(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("x", encoding="utf-8")
    with open(str(p) + ":mgrade", "wb") as f:
        f.write(b"garbage")
    assert gs.read_state(str(p))[0] == gs.ST_FORMAT_ERROR
    assert gs.mark(str(p), "O")["state"] == "written"
    assert _raw(p) == gs.payload(3)


@win_only
def test_읽기_전용은_풀었다가_되돌린다(tmp_path):
    p = tmp_path / "ro.txt"
    p.write_text("x", encoding="utf-8")
    os.chmod(p, stat.S_IREAD)
    try:
        assert gs.mark(str(p), "S")["state"] == "written"
        assert os.stat(p).st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
        assert _raw(p) == gs.payload(2)
    finally:
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD)


@win_only
def test_기본은_시각을_되돌린다(tmp_path):
    p = tmp_path / "t.txt"
    p.write_text("x", encoding="utf-8")
    old = 1_600_000_000_000_000_000
    os.utime(p, ns=(old, old))
    # 기본(keep_time=True)은 생성·접근·수정 시각을 그대로 둔다
    assert gs.mark(str(p), "C")["state"] == "written"
    assert os.stat(p).st_mtime_ns == old
    # keep_time=False(--grade-stream-update-time)면 수정 시각이 바뀐다 — 엔진 캐시가 바로 새 값을 보게
    assert gs.mark(str(p), "S", keep_time=False)["state"] == "written"
    assert os.stat(p).st_mtime_ns != old


@win_only
def test_명령줄_기본은_보존_update_time_은_변경(tmp_path):
    p = tmp_path / "s.txt"
    p.write_text(BODY_S, encoding="utf-8")
    old = 1_600_000_000_000_000_000
    os.utime(p, ns=(old, old))
    r, recs, _ = _run(tmp_path, "--file", str(p), "--write-grade-stream")
    assert recs[0]["grade_stream"]["state"] == "written", r.stderr[-400:]
    assert os.stat(p).st_mtime_ns == old
    # 같은 값이면 update-time 이어도 쓰지 않는다 — 다른 값을 미리 넣어 두고 본다
    with open(str(p) + ":mgrade", "wb") as f:
        f.write(gs.payload(3))
    os.utime(p, ns=(old, old))
    r, recs, _ = _run(tmp_path, "--file", str(p), "--write-grade-stream", "--grade-stream-update-time")
    assert recs[0]["grade_stream"] == {"state": "written", "value": "S", "prev": "O"}
    assert os.stat(p).st_mtime_ns != old


@win_only
def test_다른_프로그램이_독점하면_sharing_violation(tmp_path):
    p = tmp_path / "lock.txt"
    p.write_text("x", encoding="utf-8")
    # 잠금은 스트림마다 따로다 — 본문이 아니라 :mgrade 자체를 공유 0 으로 열어 둔다
    with open(str(p) + ":mgrade", "wb") as f:
        f.write(gs.payload(3))
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.restype = ctypes.c_void_p
    h = k.CreateFileW(str(p) + ":mgrade", 0x80000000, 0, None, 3, 0, None)
    assert h not in (None, ctypes.c_void_p(-1).value)
    try:
        r = gs.mark(str(p), "C")
        assert r["state"] == "failed" and r["reason"] == "sharing_violation" and r["winerr"] == 32, r
    finally:
        k.CloseHandle(ctypes.c_void_p(h))


def test_보조_옵션만_주면_1003(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text(BODY_NONE, encoding="utf-8")
    r, _, _ = _run(tmp_path, "--file", str(p), "--grade-stream-update-time", out=False)
    assert r.returncode == 3 and "bad_args" in r.stdout, r.stdout
    # 시각 보존이 기본이 되어 keep-time 은 없앴다 — 주면 모르는 인자
    r, _, _ = _run(tmp_path, "--file", str(p), "--write-grade-stream", "--grade-stream-keep-time", out=False)
    assert r.returncode == 3 and "bad_args" in r.stdout, r.stdout
    r, _, _ = _run(tmp_path, "--file", str(p), "--write-grade-stream",
                   "--grade-stream-unclassified", "zero", out=False)
    assert r.returncode == 3 and "bad_args" in r.stdout, r.stdout


@pytest.mark.parametrize("mode", [["--text-only"], ["--embed"]])
def test_분류_결과가_없는_모드와_함께면_1006(tmp_path, mode):
    p = tmp_path / "a.txt"
    p.write_text(BODY_NONE, encoding="utf-8")
    r, _, _ = _run(tmp_path, "--file", str(p), "--write-grade-stream", *mode, out=False)
    assert r.returncode == errcodes.exit_of("mode_conflict") == 3
    assert "mode_conflict" in r.stdout, r.stdout
    assert _raw(p) is None


@pytest.mark.skipif(WIN, reason="Windows 가 아닌 곳에서만 거절된다")
def test_윈도우가_아니면_1012(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text(BODY_NONE, encoding="utf-8")
    r, _, _ = _run(tmp_path, "--file", str(p), "--write-grade-stream", out=False)
    assert r.returncode == 3 and "unsupported_option" in r.stdout, r.stdout


@win_only
def test_옵션을_안_주면_아무것도_쓰지_않는다(tmp_path):
    p = tmp_path / "s.txt"
    p.write_text(BODY_S, encoding="utf-8")
    r, recs, summ = _run(tmp_path, "--file", str(p))
    assert r.returncode == 0, r.stderr[-400:]
    assert "grade_stream" not in recs[0] and "grade_stream" not in summ
    assert _raw(p) is None


@win_only
def test_폴더_분류_대상_규칙(tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    (d / "s.txt").write_text(BODY_S, encoding="utf-8")
    (d / "none.txt").write_text(BODY_NONE, encoding="utf-8")
    (d / "broken.hwp").write_bytes(b"xx")
    with zipfile.ZipFile(d / "z.zip", "w") as z:
        z.writestr("in.txt", BODY_S)
    r, recs, summ = _run(tmp_path, "--dir", str(d), "--write-grade-stream")
    by = {(os.path.basename(x["file"]), bool(x.get("archive"))): x for x in recs}
    assert by[("s.txt", False)]["grade_stream"] == {"state": "written", "value": "S"}
    assert by[("none.txt", False)]["grade_stream"] == {"state": "skipped", "reason": "unclassified"}
    assert by[("broken.hwp", False)]["grade_stream"] == {"state": "skipped", "reason": "not_read"}
    member = [x for x in recs if x["file"].endswith("in.txt")][0]
    assert member["grade_stream"] == {"state": "skipped", "reason": "archive_member"}
    assert by[("z.zip", True)]["grade_stream"] == {"state": "written", "value": "S"}
    assert summ["grade_stream"] == {"written": 2, "unchanged": 0, "skipped": 3, "failed": 0}
    assert _raw(d / "s.txt") == gs.payload(2) and _raw(d / "z.zip") == gs.payload(2)
    assert _raw(d / "none.txt") is None and _raw(d / "broken.hwp") is None
    # 다시 돌리면 전부 그대로 — none 옵션이면 미분류에 NONE 을 쓴다
    r, recs, summ = _run(tmp_path, "--dir", str(d), "--write-grade-stream",
                         "--grade-stream-unclassified", "none")
    assert summ["grade_stream"] == {"written": 1, "unchanged": 2, "skipped": 2, "failed": 0}
    assert _raw(d / "none.txt") == gs.payload(0)
    assert _raw(d / "broken.hwp") is None


@win_only
def test_쓰기_실패는_4002_종료코드_1(tmp_path):
    p = tmp_path / "s.txt"
    p.write_text(BODY_S, encoding="utf-8")
    # :mgrade 를 공유 0 으로 잡아 둔다 — 본문 읽기(분류)는 되고 스트림 쓰기만 공유 위반이 난다
    with open(str(p) + ":mgrade", "wb") as f:
        f.write(gs.payload(3))
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.restype = ctypes.c_void_p
    h = k.CreateFileW(str(p) + ":mgrade", 0x80000000, 0, None, 3, 0, None)
    try:
        r, recs, summ = _run(tmp_path, "--file", str(p), "--write-grade-stream")
    finally:
        k.CloseHandle(ctypes.c_void_p(h))
    assert recs[0]["grade"] == "S"
    assert recs[0]["grade_stream"]["state"] == "failed", recs[0]
    assert summ["grade_stream"]["failed"] == 1
    assert r.returncode == errcodes.exit_of("grade_stream_write_failed") == 1
    assert "4002" in r.stdout or "4002" in open(tmp_path / "result.jsonl", encoding="utf-8").read()
