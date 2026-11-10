#------------------------------------------------------------------
# PPTX 발표자 노트 · XLSX 셀 메모 테스트 — Rust 판과 같은 규칙인가
#=> 둘 다 '본문 파일에는 없는 글자'라, 읽지 않으면 통째로 사라진다.
#   그런데 사라져도 추출은 성공으로 보이기 때문에 눈에 띄지 않는다.
#    · 노트: 발표자만 보는 자리라 단가·설계 상세·내부 사정이 적히는 일이 잦다.
#      실측에서 본문 812자를 정확히 뽑고도 노트 2,000자를 놓친 문서가 있었다.
#    · 메모: 신청서·양식류는 '어떻게 적어라'가 전부 메모에 있다. 실측에서 셀 값은
#      완벽히 뽑고도 재현율 48.7% 가 된 신청서가 있었다.
#
#   Rust 판(Rust/crates/docparse/src/extract.rs)에 같은 동작이 있고 두 판의 본문이 같아야 한다.
#------------------------------------------------------------------

import io
import zipfile

from csoclassify.extract.office_ooxml import PptxExtractor, XlsxExtractor


#------------------------------------------------------------------
# 항목 몇 개짜리 zip 을 메모리에 만든다 (테스트 도우미)
#
# -in: entries = {zip 내부 이름: 내용 문자열}
#
# -out: bytes = zip 파일 바이트
# -out: error = 없음
#------------------------------------------------------------------
def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in entries.items():
            z.writestr(name, body)
    return buf.getvalue()


#------------------------------------------------------------------
# 발표자 노트를 본문 뒤에 이어 붙인다
#=> 노트를 읽지 않으면 슬라이드 본문만 남아 내부 사정이 통째로 빠진다.
#
# -in: tmp_path = pytest 임시폴더 픽스처
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_pptx_발표자_노트를_본문_뒤에_붙인다(tmp_path):
    slide = "<p:sld xmlns:p='urn:p' xmlns:a='urn:a'><a:p><a:r><a:t>슬라이드 본문</a:t></a:r></a:p></p:sld>"
    note = "<p:notes xmlns:p='urn:p' xmlns:a='urn:a'><a:p><a:r><a:t>노트에만 있는 단가</a:t></a:r></a:p></p:notes>"
    p = tmp_path / "a.pptx"
    p.write_bytes(_zip({"ppt/slides/slide1.xml": slide,
                        "ppt/notesSlides/notesSlide1.xml": note}))
    assert PptxExtractor().extract(str(p)) == "슬라이드 본문\n노트에만 있는 단가"


#------------------------------------------------------------------
# 노트가 없는 문서는 본문만 낸다
#=> 노트는 없는 문서가 더 많다. 빈 줄이 붙으면 안 된다.
#
# -in: tmp_path = pytest 임시폴더 픽스처
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_pptx_노트가_없으면_본문만(tmp_path):
    slide = "<p:sld xmlns:p='urn:p' xmlns:a='urn:a'><a:p><a:r><a:t>본문만</a:t></a:r></a:p></p:sld>"
    p = tmp_path / "b.pptx"
    p.write_bytes(_zip({"ppt/slides/slide1.xml": slide}))
    assert PptxExtractor().extract(str(p)) == "본문만"


#------------------------------------------------------------------
# 노트 파일도 번호순으로 읽는다
#=> 문자열 정렬이면 notesSlide10 이 notesSlide2 앞에 와 차례가 뒤집힌다.
#
# -in: tmp_path = pytest 임시폴더 픽스처
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_pptx_노트를_번호순으로_읽는다(tmp_path):
    def note(t):
        return f"<p:notes xmlns:p='urn:p' xmlns:a='urn:a'><a:p><a:r><a:t>{t}</a:t></a:r></a:p></p:notes>"
    p = tmp_path / "c.pptx"
    p.write_bytes(_zip({
        "ppt/slides/slide1.xml": "<p:sld xmlns:p='urn:p' xmlns:a='urn:a'><a:p><a:r><a:t>본문</a:t></a:r></a:p></p:sld>",
        "ppt/notesSlides/notesSlide10.xml": note("열번째"),
        "ppt/notesSlides/notesSlide2.xml": note("두번째"),
    }))
    assert PptxExtractor().extract(str(p)) == "본문\n두번째\n열번째"


#------------------------------------------------------------------
# 옛 방식 셀 메모를 읽는다 (서식 때문에 쪼개진 조각을 이어 붙인다)
#=> <comment> 안이 <r><t> 로 잘게 쪼개져 온다. 태그마다 줄을 끊으면 한 낱말이
#   갈라져 붙어 있어야 성립하는 검출이 어긋난다.
#
# -in: tmp_path = pytest 임시폴더 픽스처
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_xlsx_옛방식_셀메모를_이어_읽는다(tmp_path):
    sheet = ("<worksheet><sheetData><row><c t='inlineStr'><is><t>셀값</t></is></c>"
             "</row></sheetData></worksheet>")
    comments = ("<comments><commentList>"
                "<comment ref='A1'><text><r><t>셀 서식을</t></r>"
                "<r><t> 텍스트로</t></r></text></comment>"
                "</commentList></comments>")
    p = tmp_path / "d.xlsx"
    p.write_bytes(_zip({"xl/worksheets/sheet1.xml": sheet,
                        "xl/comments1.xml": comments}))
    assert XlsxExtractor().extract(str(p)) == "셀값\n셀 서식을 텍스트로"


#------------------------------------------------------------------
# 새 방식(스레드 댓글) 메모도 읽는다
#=> 최신 엑셀은 메모를 xl/threadedComments/ 에 <text> 로 담는다.
#
# -in: tmp_path = pytest 임시폴더 픽스처
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_xlsx_스레드댓글_메모도_읽는다(tmp_path):
    sheet = "<worksheet><sheetData><row><c><v>1</v></c></row></sheetData></worksheet>"
    tc = ("<ThreadedComments>"
          "<threadedComment ref='A1'><text>영구면 9999-12-31</text></threadedComment>"
          "</ThreadedComments>")
    p = tmp_path / "e.xlsx"
    p.write_bytes(_zip({"xl/worksheets/sheet1.xml": sheet,
                        "xl/threadedComments/threadedComment1.xml": tc}))
    assert XlsxExtractor().extract(str(p)) == "1\n영구면 9999-12-31"


#------------------------------------------------------------------
# 메모가 없으면 시트 값만 낸다
#=> 메모가 없는 문서가 대부분이다. 빈 줄이 붙으면 안 된다.
#
# -in: tmp_path = pytest 임시폴더 픽스처
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_xlsx_메모가_없으면_시트만(tmp_path):
    sheet = "<worksheet><sheetData><row><c><v>7</v></c></row></sheetData></worksheet>"
    p = tmp_path / "f.xlsx"
    p.write_bytes(_zip({"xl/worksheets/sheet1.xml": sheet}))
    assert XlsxExtractor().extract(str(p)) == "7"


#------------------------------------------------------------------
# 작성자 이름은 본문이 아니다
#=> xl/comments*.xml 의 <authors> 는 사람 이름 목록이라 본문에 섞으면 안 된다.
#   섞이면 문서에 없던 인명이 본문에 생겨 PII 검출까지 흔든다.
#
# -in: tmp_path = pytest 임시폴더 픽스처
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_xlsx_작성자_이름은_넣지_않는다(tmp_path):
    sheet = "<worksheet><sheetData><row><c><v>1</v></c></row></sheetData></worksheet>"
    comments = ("<comments><authors><author>홍길동</author></authors><commentList>"
                "<comment ref='A1'><text><t>메모 본문</t></text></comment>"
                "</commentList></comments>")
    p = tmp_path / "g.xlsx"
    p.write_bytes(_zip({"xl/worksheets/sheet1.xml": sheet,
                        "xl/comments1.xml": comments}))
    got = XlsxExtractor().extract(str(p))
    assert got == "1\n메모 본문"
    assert "홍길동" not in got


#------------------------------------------------------------------
# 텍스트상자 안 문단을 바깥 문단이 삼키지 않는다 (docx)
#=> docx 는 문단이 문단 안에 들어간다 — 텍스트상자(<w:txbxContent>)가 런 안에
#   있고 그 안에 또 <w:p> 가 있다. 바깥 문단을 만들 때 안쪽 런까지 끌어오면
#   표지의 여러 칸이 한 줄로 뭉쳐 '가나다라' 처럼 문서에 없는 낱말이 생기고,
#   안쪽 문단은 제 줄로 또 나와 같은 글자가 두 번 나온다.
#
# -in: tmp_path = pytest 임시폴더 픽스처
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_docx_텍스트상자_문단을_바깥이_삼키지_않는다(tmp_path):
    from csoclassify.extract.office_ooxml import DocxExtractor

    doc = ("<w:document xmlns:w='urn:w'><w:body>"
           "<w:p><w:r><w:t>바깥</w:t></w:r>"
           "<w:r><w:txbxContent>"
           "<w:p><w:r><w:t>가나</w:t></w:r></w:p>"
           "<w:p><w:r><w:t>다라</w:t></w:r></w:p>"
           "</w:txbxContent></w:r></w:p>"
           "</w:body></w:document>")
    p = tmp_path / "h.docx"
    p.write_bytes(_zip({"word/document.xml": doc}))
    # 바깥 문단은 '바깥'만, 안쪽 두 문단은 각자 제 줄. '가나다라' 가 생기면 안 된다.
    assert DocxExtractor().extract(str(p)) == "바깥\n가나\n다라"


#------------------------------------------------------------------
# 탭 정지 위치 '정의'는 탭 문자가 아니다 (docx)
#=> <w:pPr><w:tabs> 안의 <w:tab> 은 위치 정의일 뿐이다. 세어 버리면 문단 앞에
#   문서에 없던 들여쓰기가 생긴다. 진짜 탭은 런 안의 <w:tab/> 다.
#
# -in: tmp_path = pytest 임시폴더 픽스처
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_docx_탭_정의는_탭문자가_아니다(tmp_path):
    from csoclassify.extract.office_ooxml import DocxExtractor

    doc = ("<w:document xmlns:w='urn:w'><w:body><w:p>"
           "<w:pPr><w:tabs><w:tab w:pos='1'/><w:tab w:pos='2'/></w:tabs></w:pPr>"
           "<w:r><w:t>가</w:t><w:tab/><w:t>나</w:t></w:r>"
           "</w:p></w:body></w:document>")
    p = tmp_path / "i.docx"
    p.write_bytes(_zip({"word/document.xml": doc}))
    assert DocxExtractor().extract(str(p)) == "가\t나"
