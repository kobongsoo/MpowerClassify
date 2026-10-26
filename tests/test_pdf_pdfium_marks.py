#------------------------------------------------------------------
# PDF 추출 — pdfium 표식 U+FFFE 지우기 테스트
#=> pypdfium2 의 get_text_range 는 낱말 사이 · 줄 끝에 U+FFFE 를 끼워 돌려줄 때가 있다
#   (실측: D:\sample\4_디지털콘텐츠_통합보고서_업로드용.pdf 에서 50,908자). Rust 판
#   (pdfium-render)은 이 글자를 돌려주지 않아, 남겨 두면 두 판의 본문이 달라지고 PII 패턴이
#   한쪽에서만 끊긴다(2026-10-26 배포 교차 검증에서 '주소' 근거가 갈림).
#   이 표식은 시험용 PDF 를 만들어서는 생기지 않아, get_text_range 를 바꿔 끼워 확인한다.
#------------------------------------------------------------------

import ctypes

import pytest

pdfium = pytest.importorskip("pypdfium2")
import pypdfium2.raw as pdfium_raw  # noqa: E402  (importorskip 뒤에 가져온다)

from csoclassify.extract import pdf_pdfium  # noqa: E402
from csoclassify.extract.pdf_pdfium import PdfiumExtractor, _strip_pdfium_marks  # noqa: E402


#------------------------------------------------------------------
# 글자 한 줄짜리 PDF 만들기 (테스트 도우미)
#
# -in: path = 저장할 경로
# -in: text = 텍스트 레이어에 넣을 글자
#
# -out: 없음(파일을 만든다)
# -out: error = pdfium 오류는 그대로 전파
#------------------------------------------------------------------
def _make_text_pdf(path, text):
    pdf = pdfium.PdfDocument.new()
    page = pdf.new_page(595, 842)
    obj = pdfium_raw.FPDFPageObj_NewTextObj(pdf.raw, b"Helvetica", 12.0)
    buf = ctypes.create_string_buffer((text + "\0").encode("utf-16-le"))
    pdfium_raw.FPDFText_SetText(obj, ctypes.cast(buf, ctypes.POINTER(pdfium_raw.FPDF_WCHAR)))
    pdfium_raw.FPDFPageObj_Transform(obj, 1, 0, 0, 1, 50, 700)
    pdfium_raw.FPDFPage_InsertObject(page.raw, obj)
    page.gen_content()
    pdf.save(str(path))
    pdf.close()


#------------------------------------------------------------------
# 표식만 지우고 다른 글자는 그대로 둔다
#
# -in: 없음
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_표식만_지운다():
    assert _strip_pdfium_marks("Technology\ufffe Roadmap\ufffe\r\nfor SME") == "Technology Roadmap\r\nfor SME"
    # 공백 · 줄바꿈 · 다른 특수문자(U+FEFF, U+FFFD)는 건드리지 않는다
    assert _strip_pdfium_marks("가 나\n다\ufeff\ufffd") == "가 나\n다\ufeff\ufffd"
    assert _strip_pdfium_marks("") == ""
    assert _strip_pdfium_marks(None) == ""


#------------------------------------------------------------------
# 추출기가 페이지 본문에서 표식을 지운다
#=> get_text_range 가 표식을 끼워 돌려주게 바꿔 끼우고, 추출 결과에 U+FFFE 가 없는지 본다.
#
# -in: tmp_path    = pytest 임시폴더 픽스처
# -in: monkeypatch = pytest 바꿔 끼우기 픽스처
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_추출기가_표식을_지운다(tmp_path, monkeypatch):
    p = tmp_path / "marks.pdf"
    _make_text_pdf(p, "Technology Roadmap for SME")
    real = pdf_pdfium.pdfium.PdfTextPage.get_text_range

    # 실제 본문의 공백 앞마다 표식을 끼운다 — 실문서에서 본 모양("Technology\ufffe Roadmap")
    def with_marks(self, *a, **kw):
        return real(self, *a, **kw).replace(" ", "\ufffe ")

    monkeypatch.setattr(pdf_pdfium.pdfium.PdfTextPage, "get_text_range", with_marks)
    text = PdfiumExtractor().extract(str(p))
    assert "\ufffe" not in text
    assert "Technology Roadmap for SME" in text
