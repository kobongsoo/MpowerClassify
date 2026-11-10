#------------------------------------------------------------------
# Rust 문서파서 엔진(--extractor rust) 단위 시험
#=> 설계: plan/Python-문서파서-Rust연동-설계-20261110.html §4·§7
#   docparse 라이브러리(docparse.dll/.so, ctypes 감싸개 _docparse.py)를 파이썬 판 추출 흐름에
#   끼운 자리를 지킨다.
#    · 오류 종류 — 크기 상한은 SizeLimitError, 깨진 파일은 ExtractError
#    · 표식 — Rust 결과의 partial·layout 이 notes 에 옮겨 적힌다
#    · 고르기 — build_extractor(extractor="rust") 는 감지·엔진을 모두 Rust 로
#    · 압축 — 펼친 목록·label·origin 이 파이썬 해제기와 같은 모양
#    · 병렬 — 스레드 4개 동시 추출이 단일 스레드와 같다(전역 상태 없음)
#    · 시작 점검 — 라이브러리가 없으면 1017 extractor_missing 으로 멈춘다
#   라이브러리를 찾지 못하면(third_party/docparse 가 없는 작업 트리) 이 파일 전체를 건너뛴다.
#------------------------------------------------------------------

import gzip
import io
import os
import tarfile
import threading
import zipfile

import pytest

from csoclassify import config
from csoclassify.extract import build_extractor, notes, rust_engine
from csoclassify.extract import _docparse as docparse

if not rust_engine.available()[0]:
    pytest.skip("docparse 라이브러리 없음: " + rust_engine.available()[1], allow_module_level=True)
from csoclassify.extract import archive
from csoclassify.extract.base import ExtractError, SizeLimitError


#------------------------------------------------------------------
# 시험용 docx 만들기
#=> 문단 두 개짜리 최소 docx 를 zip 으로 직접 쓴다(python-docx 없이).
#
# -in: path  = 만들 파일 경로
# -in: paras = 문단 글자 목록
#
# -out: str = 만든 경로
# -out: error = 없음
#------------------------------------------------------------------
def _make_docx(path, paras):
    body = "".join(f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>" for t in paras)
    doc = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f"<w:body>{body}</w:body></w:document>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("word/document.xml", doc)
    return str(path)


#------------------------------------------------------------------
# docx 본문을 Rust 엔진으로 뽑는다 — 감지도 Rust
#------------------------------------------------------------------
def test_docx_본문(tmp_path):
    p = _make_docx(tmp_path / "a.docx", ["가나다 문단", "둘째 문단"])
    assert rust_engine.detect_format(p) == "docx"
    text = rust_engine.RustEngine("docx").extract(p)
    assert "가나다 문단" in text and "둘째 문단" in text


#------------------------------------------------------------------
# 원시 텍스트를 돌려준다 — 정제는 파이썬 clean_text 가 한다
#=> 연속 공백이 그대로 남아 있어야 한다(clean=False).
#------------------------------------------------------------------
def test_원시_텍스트(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("가   나\n\n\n\n다", encoding="utf-8")
    raw = rust_engine.RustEngine("text").extract(str(p))
    assert "가   나" in raw
    from csoclassify.clean import clean_text
    # 파이썬 정제와 Rust 정제가 같은 결과를 낸다(두 판이 같은 본문을 보게).
    assert clean_text(raw) == docparse.clean_text(raw)


#------------------------------------------------------------------
# 크기 상한 → SizeLimitError (레코드 error_kind 가 지금과 같게)
#------------------------------------------------------------------
def test_크기상한은_SizeLimitError(tmp_path, monkeypatch):
    p = tmp_path / "big.txt"
    p.write_bytes(b"a" * 5000)
    monkeypatch.setattr(config, "MAX_FILE_BYTES_TEXT", 1000)
    with pytest.raises(SizeLimitError) as e:
        rust_engine.RustEngine("text").extract(str(p))
    # 파이썬 판 check_size_limit 와 같은 문장이다.
    assert "크기 상한 초과" in str(e.value) and "MAX_FILE_BYTES_TEXT" in str(e.value)
    # 0 은 '상한 없음'
    monkeypatch.setattr(config, "MAX_FILE_BYTES_TEXT", 0)
    assert rust_engine.RustEngine("text").extract(str(p)).startswith("aaa")


#------------------------------------------------------------------
# 깨진 파일 → ExtractError (SizeLimitError 가 아니다)
#------------------------------------------------------------------
def test_깨진_파일은_ExtractError(tmp_path):
    p = tmp_path / "broken.docx"
    p.write_bytes(b"PK\x03\x04 not really a zip")
    with pytest.raises(ExtractError) as e:
        rust_engine.RustEngine("docx").extract(str(p))
    assert not isinstance(e.value, SizeLimitError)


#------------------------------------------------------------------
# 표식 옮겨 적기 — docparse 결과의 partial·layout 이 notes 로 간다
#=> 실제로 상한에 걸리는 문서를 만들기 어려워 결과 객체를 흉내 낸다.
#------------------------------------------------------------------
def test_표식을_notes_에_옮긴다(tmp_path, monkeypatch):
    class Fake:
        text = "본문"
        partial = ("시간 상한", "pages", 3, 10)
        layout = (3, 1)

    class FakeMod:
        ExtractError = docparse.ExtractError
        SizeLimitError = docparse.SizeLimitError

        @staticmethod
        def extract(*a, **k):
            return Fake()

    monkeypatch.setattr(rust_engine, "_dp", FakeMod)
    notes.reset()
    notes.reset_layout()
    assert rust_engine.RustEngine("pdf").extract(str(tmp_path / "x.pdf")) == "본문"
    assert notes.take() == {"reason": "시간 상한", "unit": "pages", "read": 3, "total": 10}
    assert notes.take_layout() == {"pages": 3, "scan_pages": 1}


#------------------------------------------------------------------
# build_extractor(extractor="rust") — 엔진 표와 감지가 모두 Rust
#------------------------------------------------------------------
def test_build_extractor_rust():
    ex = build_extractor(hybrid=True, extractor="rust")
    assert ex.detect is rust_engine.detect_format
    assert set(ex.engines) == set(rust_engine.FORMATS)
    assert all(isinstance(e, rust_engine.RustEngine) for e in ex.engines.values())


#------------------------------------------------------------------
# 라이브러리가 없으면 build_extractor 가 조용히 py 로 넘어가지 않는다
#------------------------------------------------------------------
def test_모듈없으면_조용히_넘어가지_않는다(monkeypatch):
    # 라이브러리를 못 불러온 상태를 흉내 낸다(불러오기 결과 기억 자리에 오류를 넣는다).
    monkeypatch.setattr(rust_engine, "_LOAD_ERROR", [OSError("없음")])
    with pytest.raises(ExtractError):
        build_extractor(hybrid=True, extractor="rust")


#------------------------------------------------------------------
# 시작 점검 — rust 인데 라이브러리가 없으면 1017 extractor_missing(종료코드 3)
#------------------------------------------------------------------
def test_시작점검_모듈없음_1017(monkeypatch, capsys):
    from csoclassify import cli, errcodes
    # 라이브러리를 못 불러온 상태를 흉내 낸다(불러오기 결과 기억 자리에 오류를 넣는다).
    monkeypatch.setattr(rust_engine, "_LOAD_ERROR", [OSError("없음")])
    monkeypatch.setattr(config, "EXTRACTOR", "py")

    class A:
        extractor = "rust"

    code = cli.check_extractor_args(A())
    assert code == errcodes.exit_of("extractor_missing") == 3
    assert errcodes.ERRORS["extractor_missing"][0] == 1017
    # 실패했으면 고른 값을 새기지 않는다.
    assert config.EXTRACTOR == "py"


#------------------------------------------------------------------
# 시작 점검 — 고른 값을 config 에 새긴다 · 틀린 환경변수 값은 1003
#------------------------------------------------------------------
def test_시작점검_값_새기기(monkeypatch):
    from csoclassify import cli, errcodes
    monkeypatch.setattr(config, "EXTRACTOR", "py")

    class A:
        extractor = "rust"

    assert cli.check_extractor_args(A()) is None
    assert config.EXTRACTOR == "rust"

    class B:
        extractor = None

    monkeypatch.setattr(config, "EXTRACTOR", "pyy")
    assert cli.check_extractor_args(B()) == errcodes.exit_of("bad_args")


#------------------------------------------------------------------
# 병렬 — 스레드 4개가 동시에 뽑아도 단일 스레드와 같다
#------------------------------------------------------------------
def test_스레드_4개_동시_추출(tmp_path):
    files = [_make_docx(tmp_path / f"d{i}.docx", [f"문서 {i} 본문", "공통 줄"]) for i in range(8)]
    eng = rust_engine.RustEngine("docx")
    single = {f: eng.extract(f) for f in files}
    got, errs = {}, []

    def work(fs):
        try:
            for f in fs:
                got[f] = eng.extract(f)
        except Exception as e:   # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=work, args=(files[i::4],)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs
    assert got == single


#------------------------------------------------------------------
# 압축 — 파이썬 해제기와 Rust 해제기의 펼친 목록이 같다
#=> label 앞부분은 입력 경로 표기 그대로('\' 포함), 내부 경로는 '/'.
#------------------------------------------------------------------
def _expand_both(monkeypatch, paths, work_root):
    out = {}
    for which in ("py", "rust"):
        monkeypatch.setattr(config, "EXTRACTOR", which)
        w = os.path.join(work_root, which)
        os.makedirs(w, exist_ok=True)
        st = {}
        jobs = archive.expand_paths(paths, w, stats=st)
        out[which] = (sorted((label, origin) for _src, label, origin in jobs), st.get("unexpanded", []))
        # 펼친 파일은 실제로 읽을 수 있어야 한다.
        for src, _l, _o in jobs:
            assert os.path.isfile(src)
    return out


def test_압축_zip_중첩_docx(tmp_path, monkeypatch):
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("in.txt", "안쪽")
    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr("a.txt", "hello")
        z.writestr("sub/b.txt", "world")
        z.writestr("__MACOSX/._a.txt", "noise")
        z.writestr("nested.zip", inner.getvalue())
    docx = _make_docx(tmp_path / "doc.docx", ["문서"])
    plain = tmp_path / "p.txt"
    plain.write_text("평문", encoding="utf-8")
    r = _expand_both(monkeypatch, [str(outer), docx, str(plain)], str(tmp_path / "w"))
    assert r["py"] == r["rust"]
    labels = [l for l, _o in r["rust"][0]]
    # 입력 경로 표기(윈도우면 '\')를 그대로 지키고, 내부는 '/' 로 잇는다.
    assert str(outer) + "/nested.zip/in.txt" in labels
    assert docx in labels


def test_압축_targz_gz(tmp_path, monkeypatch):
    tg = tmp_path / "t.tar.gz"
    with tarfile.open(tg, "w:gz") as t:
        data = "타르 안".encode("utf-8")
        info = tarfile.TarInfo("dir/x.txt")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    g = tmp_path / "note.txt.gz"
    with gzip.open(g, "wb") as f:
        f.write("지 압축".encode("utf-8"))
    r = _expand_both(monkeypatch, [str(tg), str(g)], str(tmp_path / "w"))
    assert r["py"][0] == r["rust"][0]


#------------------------------------------------------------------
# 압축 상한 — 개수 상한을 넘으면 펼치지 않고 stats 에 kind=limit 로 남긴다
#------------------------------------------------------------------
def test_압축_개수상한(tmp_path, monkeypatch):
    z = tmp_path / "many.zip"
    with zipfile.ZipFile(z, "w") as zz:
        for i in range(5):
            zz.writestr(f"f{i}.txt", "x")
    monkeypatch.setattr(config, "MAX_ARCHIVE_MEMBERS", 2)
    r = _expand_both(monkeypatch, [str(z)], str(tmp_path / "w"))
    for which in ("py", "rust"):
        jobs, unexp = r[which]
        assert jobs == [(str(z), None)]
        assert len(unexp) == 1 and unexp[0]["kind"] == "limit" and unexp[0]["path"] == str(z)


#------------------------------------------------------------------
# 분할 조각 판정도 같은 스위치를 따른다
#------------------------------------------------------------------
def test_분할조각_판정(monkeypatch):
    monkeypatch.setattr(config, "EXTRACTOR", "rust")
    assert archive.split_volume_hint("자료.7z.002") is not None
    assert archive.split_volume_hint("보통.zip") is None
