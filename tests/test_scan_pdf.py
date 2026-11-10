#------------------------------------------------------------------
# 스캔 PDF 처리제외(EXCLUDED) 테스트 — M3 기준(1차 범위: OCR 미적용)
#=> 텍스트 레이어 없이 그림만 든 PDF 는 '못 읽은 문서(실패)'가 아니라 '범위 밖이라
#   처리하지 않는 문서'다. 실패(error·종료코드 1)로 세면 스캔본이 섞인 배치마다
#   종료코드가 올라가, 진짜 깨진 문서가 스캔본 틈에 묻힌다.
#
#   판정: 스캔 페이지(글자 < 10 · 그림이 절반 이상 덮음)가 절반 이상이면서 문서 전체도
#   페이지당 10자 미만. Rust 문서파서(DocParse-rust crates/docparse/src/extract.rs scan_pdf_verdict)와 같은 식·같은 값.
#------------------------------------------------------------------

import ctypes
import json
import os
import re
import subprocess
import sys

import pytest

from csoclassify import cli
from csoclassify import config
from csoclassify.extract import notes

pdfium = pytest.importorskip("pypdfium2")
import pypdfium2.raw as pdfium_raw  # noqa: E402  (importorskip 뒤에 가져온다)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RS_EXE = os.path.join(ROOT, "Rust", "target", "release", "MpowerClassify-rs.exe")


#------------------------------------------------------------------
# 한 쪽짜리 PDF 만들기 (테스트 도우미)
#=> 실제 스캔본을 저장소에 넣을 수 없어 pdfium 으로 같은 모양을 만든다.
#    · image=True  — 페이지 전체를 덮는 그림 한 장(스캔본의 모양)
#    · text=문자열 — 텍스트 레이어에 글자를 넣는다(정상 PDF 의 모양)
#
# -in: path  = 저장할 경로
# -in: image = 페이지를 덮는 그림을 넣을지(기본 True)
# -in: text  = 텍스트 레이어에 넣을 글자(기본 None = 없음)
#
# -out: 없음(파일을 만든다)
# -out: error = pdfium 오류는 그대로 전파
#------------------------------------------------------------------
def _make_pdf(path, image=True, text=None):
    pdf = pdfium.PdfDocument.new()
    page = pdf.new_page(595, 842)
    if image:
        img = pdfium.PdfImage.new(pdf)
        bm = pdfium.PdfBitmap.new_native(40, 40, pdfium_raw.FPDFBitmap_BGR)
        bm.fill_rect((200, 200, 200, 255), 0, 0, 40, 40)
        img.set_bitmap(bm)
        # 1x1 그림을 페이지 크기로 늘린다 — 스캔본은 그림 한 장이 페이지를 통째로 덮는다.
        img.set_matrix(pdfium.PdfMatrix().scale(595, 842))
        page.insert_obj(img)
    if text:
        obj = pdfium_raw.FPDFPageObj_NewTextObj(pdf.raw, b"Helvetica", 12.0)
        buf = ctypes.create_string_buffer((text + "\0").encode("utf-16-le"))
        pdfium_raw.FPDFText_SetText(obj, ctypes.cast(buf, ctypes.POINTER(pdfium_raw.FPDF_WCHAR)))
        pdfium_raw.FPDFPageObj_Transform(obj, 1, 0, 0, 1, 50, 700)
        pdfium_raw.FPDFPage_InsertObject(page.raw, obj)
    page.gen_content()
    pdf.save(str(path))
    pdf.close()


#------------------------------------------------------------------
# PDF 파서가 스캔 페이지 수를 남긴다
#=> 그림만 든 쪽은 스캔 페이지, 글자가 든 쪽은 그림이 덮여 있어도 스캔 페이지가 아니다
#   (그림 위에 글자층을 얹은 '검색 가능 PDF'는 읽을 수 있는 문서다).
#
# -in: tmp_path = pytest 임시폴더 픽스처
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_파서가_스캔_페이지를_센다(tmp_path):
    from csoclassify.extract.pdf_pdfium import PdfiumExtractor

    cases = {"scan.pdf": (True, None, 1),
             "text.pdf": (False, "Hello text layer page one two three", 0),
             "both.pdf": (True, "Hello text layer page one two three", 0)}
    for name, (image, text, want) in cases.items():
        p = tmp_path / name
        _make_pdf(p, image=image, text=text)
        notes.reset_layout()
        PdfiumExtractor().extract(str(p))
        assert notes.take_layout() == {"pages": 1, "scan_pages": want}, name


#------------------------------------------------------------------
# 판정식 — 그림만 든 문서는 처리제외, 본문이 있으면 아니다
#=> Rust 판 scan_pdf_verdict 시험과 같은 경우·같은 기대값이다. 한쪽만 고치면 두 판의
#   판정이 조용히 갈린다.
#
# -in: 없음
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_판정식():
    def ex(pages, scan, chars):
        return cli._scan_pdf_excluded({"pages": pages, "scan_pages": scan}, chars) is not None

    assert ex(5, 5, 0)                  # 모든 쪽이 그림, 글자 0자 — 실측 스캔본의 모양
    assert ex(4, 4, 30)                 # 머리말 스탬프 몇 글자(페이지당 10자 미만)
    assert not ex(12, 11, 565)          # 12쪽 중 11쪽 그림이어도 1쪽 565자면 읽을 것이 있다
    assert not ex(10, 4, 0)             # 그림 페이지가 절반 미만 — 스캔본이 아니라 '못 읽음'
    assert not ex(2, 0, 0)
    # 경계 — 절반 '이상', 페이지당 10자 '미만'(Rust 와 비교 방향이 같아야 한다)
    assert ex(2, 1, 0)
    assert ex(2, 2, 19)
    assert not ex(2, 2, 20)
    assert not ex(0, 0, 0)              # 한 쪽도 못 읽었으면 판정하지 않는다
    # PDF 가 아니면(페이지 구성 없음) 처리제외가 아니다
    assert cli._scan_pdf_excluded(None, 0) is None


#------------------------------------------------------------------
# 표식은 달되 등급은 그대로, error 는 달지 않는다
#=> 파일명·경로 신호로 정해진 등급을 잃으면 안 된다. 처리제외는 실패가 아니라서
#   error 칸이 붙으면 받는 쪽이 '깨진 문서'로 오해한다.
#
# -in: 없음
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_표식은_달되_등급은_그대로():
    info = {"pages": 3, "scan_pages": 3, "text_len": 0}
    rec = {"grade": "C", "why": {"security": {"method": "fusion"}}}
    cli._mark_excluded(rec, info)
    assert rec["grade"] == "C"
    assert rec["why"]["security"]["method"] == "fusion"
    assert rec["excluded"]["kind"] == "scan_pdf"
    assert "EXCLUDED" in rec["excluded"]["reason"]
    assert "error" not in rec

    # 아무것도 못 정했으면 'unclassified'(봤는데 없더라)가 아니라 'excluded' 로 적는다.
    rec2 = {"grade": None, "why": {"security": {"method": "unclassified"}}}
    cli._mark_excluded(rec2, info)
    assert rec2["why"]["security"]["method"] == "excluded"


#------------------------------------------------------------------
# --simple 에도 처리제외가 남는다 (화면이 만드는 축약본도 같다)
#=> --simple 만 받는 쪽(문서중앙화)이 OCR 대상 문서를 가를 수 있어야 한다.
#
# -in: 없음
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_simple에_처리제외가_남는다():
    sys.path.insert(0, os.path.join(ROOT, "ui"))
    import app  # noqa: E402  (화면 모듈 — 엔진과 같은 칸을 고르는지 본다)

    rec = {"file": "a.pdf", "hash": "h", "grade": None,
           "excluded": {"kind": "scan_pdf", "reason": "처리제외(EXCLUDED)"}}
    out = cli._simple_record(rec)
    assert out["excluded"] == "scan_pdf"
    assert "error" not in out
    assert app.simple_record(rec) == out


#------------------------------------------------------------------
# 판정값이 Rust 판과 같다
#=> 값이 하나라도 다르면 같은 PDF 가 한 판에서는 처리제외, 다른 판에서는 정상으로
#   나간다. 두 판이 같은 문서에 같은 결과를 내야 한다(설계 원칙 4).
#
# -in: 없음
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_판정값이_Rust판과_같다():
    # 문서파서 소스는 별도 프로젝트(DocParse-rust)에 있다 — 옆 폴더(또는 DOCPARSE_SRC)가 없으면 건너뛴다.
    # 그림 덮개 비율(IMAGE_COVER)은 라이브러리 안에서만 쓰여 소스로만 확인할 수 있다.
    base = os.environ.get("DOCPARSE_SRC") or os.path.join(os.path.dirname(ROOT), "DocParse-rust")
    path = os.path.join(base, "crates", "docparse", "src", "extract.rs")
    if not os.path.isfile(path):
        pytest.skip("DocParse-rust 소스 없음: " + path)
    src = open(path, encoding="utf-8").read()

    def rust(name):
        m = re.search(rf"pub const {name}: \w+ = ([0-9.]+);", src)
        assert m, f"Rust 판에 {name} 이 없다"
        return float(m.group(1))

    assert rust("SCAN_PDF_PAGE_MAX_CHARS") == config.SCAN_PDF_PAGE_MAX_CHARS
    assert rust("SCAN_PDF_IMAGE_COVER") == config.SCAN_PDF_IMAGE_COVER
    assert rust("SCAN_PDF_PAGE_RATIO") == config.SCAN_PDF_PAGE_RATIO


#------------------------------------------------------------------
# 판정 식이 문서파서 라이브러리와 같다 (실제 DLL 로)
#=> 소스가 없어도 배포된 라이브러리의 판정을 경계값으로 확인한다.
#    · 쪽당 글자 수가 상한 바로 아래/위 · 스캔 쪽 비율이 기준 바로 위/아래
#
# -in: 없음
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError, 라이브러리가 없으면 건너뜀
#------------------------------------------------------------------
def test_판정식이_라이브러리와_같다():
    from csoclassify.extract import rust_engine
    ok, why = rust_engine.available()
    if not ok:
        pytest.skip(why)
    from csoclassify.extract import _docparse as dp
    pages = 100
    cases = [
        (pages, pages, 0),                                                   # 전부 스캔·글자 없음
        (pages, pages, int(config.SCAN_PDF_PAGE_MAX_CHARS * pages) - 1),     # 글자 상한 바로 아래
        (pages, pages, int(config.SCAN_PDF_PAGE_MAX_CHARS * pages)),         # 글자 상한
        (pages, int(config.SCAN_PDF_PAGE_RATIO * pages), 0),                 # 비율 경계
        (pages, int(config.SCAN_PDF_PAGE_RATIO * pages) - 1, 0),             # 비율 바로 아래
        (0, 0, 0),
    ]
    for p, sp, ch in cases:
        want = p > 0 and sp / p >= config.SCAN_PDF_PAGE_RATIO and ch / p < config.SCAN_PDF_PAGE_MAX_CHARS
        assert dp.scan_pdf_verdict(p, sp, ch) == want, (p, sp, ch)


#------------------------------------------------------------------
# 두 판이 스캔 PDF 를 처리제외로 같게 내보낸다 (실제 실행)
#=> 스캔 PDF 와 정상 문서를 함께 돌린다.
#    · 스캔 PDF 레코드에 excluded(kind=scan_pdf)가 붙고 error 는 없다
#    · 요약에 excluded=1, extract_failed=0
#    · 종료코드는 0 — 처리제외는 실패가 아니다
#    · --simple 에도 excluded 가 남는다
#
# -in: engine   = "python" | "rust"
# -in: tmp_path = pytest 임시폴더 픽스처
# -out: 없음(assert)
# -out: error = Rust exe 가 없으면 rust 쪽은 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_두_판이_스캔PDF를_처리제외로_낸다(engine, tmp_path):
    if engine == "rust" and not os.path.isfile(RS_EXE):
        pytest.skip("Rust exe 없음(cargo build --release 먼저)")
    docs = tmp_path / "docs"
    docs.mkdir()
    _make_pdf(docs / "스캔본.pdf")
    (docs / "정상.txt").write_text("이 문서는 사내 규정에 따른 일반 안내문입니다. " * 5,
                                  encoding="utf-8")
    cmd = [RS_EXE] if engine == "rust" else [sys.executable, "-m", "csoclassify"]
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"),
               PYTHONIOENCODING="utf-8",
               CSOCLASSIFY_POLICY_DIR=os.path.join(ROOT, "resources", "policy"))

    r = subprocess.run(cmd + ["--dir", str(docs), "--rule-only", "--format", "jsonl"],
                       cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
    rows = [json.loads(l) for l in r.stdout.splitlines() if l.strip().startswith("{")]
    recs = {os.path.basename(x["file"]): x for x in rows if "file" in x}
    summary = next(x["summary"] for x in rows if "summary" in x)
    scan = recs["스캔본.pdf"]
    assert scan.get("excluded", {}).get("kind") == "scan_pdf", (scan, r.stderr[-400:])
    assert "error" not in scan, scan
    assert "excluded" not in recs["정상.txt"], "정상 문서의 모양이 바뀌면 안 된다"
    assert summary.get("excluded") == 1 and summary["extract_failed"] == 0, summary
    assert r.returncode == 0, r.stderr[-400:]

    s = subprocess.run(cmd + ["--dir", str(docs), "--rule-only", "--simple", "--format", "jsonl"],
                       cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
    simple = {os.path.basename(json.loads(l)["file"]): json.loads(l)
              for l in s.stdout.splitlines() if l.strip().startswith("{") and '"file"' in l}
    assert simple["스캔본.pdf"].get("excluded") == "scan_pdf", simple
    assert "error" not in simple["스캔본.pdf"], simple
