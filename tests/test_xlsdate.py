#------------------------------------------------------------------
# 엑셀 날짜 서식 해석 테스트 — Rust 판(Rust/crates/docparse/src/xlsdate.rs)과 같은 규칙인가
#=> 엑셀은 날짜를 숫자로 저장한다(2012-12-31 → 41274). 서식을 읽어 사람이
#   보는 글자로 바꾸되, **서식이 날짜라고 말할 때만** 바꿔야 한다.
#
#   여기서 지키는 것은 전부 '조용히 틀리는' 종류다 — 결과가 그럴듯해서
#   눈으로는 안 보이고, 나중에 등급이 갈릴 때에야 드러난다.
#   Rust 판에 같은 이름의 시험이 있고, 두 판의 기대값이 같아야 한다.
#------------------------------------------------------------------

import io
import zipfile

from csoclassify.extract import xlsdate
from csoclassify.extract.office_ooxml import XlsxExtractor


#------------------------------------------------------------------
# 실측 대조 — 사이냅이 2012-12-31 로 내놓은 칸이 파일 안에서는 41274 였다
#
# -in: 없음
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_실측_일련번호를_사이냅과_같은_날짜로():
    assert xlsdate.serial_to_string(41274, xlsdate.KIND_DATE) == "2012-12-31"
    assert xlsdate.serial_to_string(41275, xlsdate.KIND_DATE) == "2013-01-01"


#------------------------------------------------------------------
# 1900 윤년 착각 구간 — 60 앞뒤로 기준일이 하루 다르다
#=> 엑셀은 있지도 않은 1900-02-29 를 60번으로 세어 둔다. 여기를 틀리면
#   옛 문서의 날짜가 전부 하루씩 어긋난다.
#
# -in: 없음
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_천구백년_윤년_착각_구간을_맞춘다():
    assert xlsdate.serial_to_string(1, xlsdate.KIND_DATE) == "1900-01-01"
    assert xlsdate.serial_to_string(59, xlsdate.KIND_DATE) == "1900-02-28"
    assert xlsdate.serial_to_string(60, xlsdate.KIND_DATE) == "1900-02-29"
    assert xlsdate.serial_to_string(61, xlsdate.KIND_DATE) == "1900-03-01"


#------------------------------------------------------------------
# 시각·날짜시각 모양
#
# -in: 없음
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_시각과_날짜시각을_함께_낸다():
    assert xlsdate.serial_to_string(0.5, xlsdate.KIND_TIME) == "12:00:00"
    assert (xlsdate.serial_to_string(41274.5, xlsdate.KIND_DATETIME)
            == "2012-12-31 12:00:00")


#------------------------------------------------------------------
# 날짜 서식이 아니면 숫자를 건드리지 않는다
#=> 서식을 안 보고 바꾸면 멀쩡한 수량·금액이 날짜로 둔갑한다.
#
# -in: 없음
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_날짜서식이_아니면_바꾸지_않는다():
    assert xlsdate.serial_to_string(41274, xlsdate.KIND_NONE) is None
    assert xlsdate.serial_to_string(-1, xlsdate.KIND_DATE) is None
    assert xlsdate.serial_to_string(3_000_000, xlsdate.KIND_DATE) is None


#------------------------------------------------------------------
# 내장 서식 번호 판별
#=> 번호만으로 뜻이 정해진 서식들. 숫자 서식을 날짜로 보면 안 된다.
#
# -in: 없음
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_내장_서식_번호를_가른다():
    assert xlsdate.builtin_kind(14) == xlsdate.KIND_DATE
    assert xlsdate.builtin_kind(22) == xlsdate.KIND_DATETIME
    assert xlsdate.builtin_kind(20) == xlsdate.KIND_TIME
    assert xlsdate.builtin_kind(31) == xlsdate.KIND_DATE
    for fid in (0, 1, 2, 9, 10, 37, 40, 44, 49):
        assert xlsdate.builtin_kind(fid) == xlsdate.KIND_NONE, f"{fid} 를 날짜로 봤다"


#------------------------------------------------------------------
# 사용자 지정 서식 문자열 판별
#=> m 은 '월'도 '분'도 된다(mm:ss). 혼자 있으면 날짜라 단정하지 않는다.
#   따옴표 안 글자와 [색] 구역의 글자에 속으면 안 된다.
#
# -in: 없음
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_사용자서식_문자열을_가른다():
    assert xlsdate.code_kind("yyyy-mm-dd") == xlsdate.KIND_DATE
    assert xlsdate.code_kind('yyyy"년" m"월" d"일"') == xlsdate.KIND_DATE
    assert xlsdate.code_kind("yyyy-mm-dd hh:mm:ss") == xlsdate.KIND_DATETIME
    assert xlsdate.code_kind("[h]:mm:ss") == xlsdate.KIND_TIME
    # mm:ss 는 '분:초'라 시각이다(내장 45 번과 같은 뜻).
    assert xlsdate.code_kind("mm:ss") == xlsdate.KIND_TIME
    assert xlsdate.code_kind("#,##0") == xlsdate.KIND_NONE
    # 월만 있는 서식은 날짜로 단정하지 않는다(분과 가릴 수 없다).
    assert xlsdate.code_kind("mm") == xlsdate.KIND_NONE
    # 따옴표 안의 d, [Red] 의 d 에 속으면 안 된다.
    assert xlsdate.code_kind('#,##0"day"') == xlsdate.KIND_NONE
    assert xlsdate.code_kind("[Red]#,##0") == xlsdate.KIND_NONE
    # 음수 구역은 보지 않는다(양수 표시가 기준이다).
    assert xlsdate.code_kind("#,##0;[Red]\\-yyyy") == xlsdate.KIND_NONE


#------------------------------------------------------------------
# 최소 xlsx 한 개를 메모리에 만든다 (테스트 도우미)
#=> 실제 파일 없이 서식표·시트 조합을 마음대로 시험하려고 zip 을 직접 만든다.
#
# -in: styles_xml = xl/styles.xml 내용(None 이면 서식표 없는 통합문서)
# -in: sheet_xml  = xl/worksheets/sheet1.xml 내용
#
# -out: bytes = xlsx 파일 바이트
# -out: error = 없음
#------------------------------------------------------------------
def _make_xlsx(styles_xml, sheet_xml):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if styles_xml is not None:
            z.writestr("xl/styles.xml", styles_xml)
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buf.getvalue()


#------------------------------------------------------------------
# 날짜 서식 칸만 날짜로 바뀐다 (xlsx 통합)
#=> 같은 값 41274 라도 서식이 날짜인 칸만 바뀌어야 한다.
#
# -in: tmp_path = pytest 임시폴더 픽스처
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_xlsx_날짜서식_칸만_바뀐다(tmp_path):
    styles = ('<styleSheet><cellXfs count="2">'
              '<xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>')
    sheet = ('<worksheet><sheetData><row>'
             '<c s="1"><v>41274</v></c><c s="0"><v>41274</v></c>'
             '</row></sheetData></worksheet>')
    p = tmp_path / "a.xlsx"
    p.write_bytes(_make_xlsx(styles, sheet))
    got = XlsxExtractor().extract(str(p))
    assert got == "2012-12-31\t41274"


#------------------------------------------------------------------
# cellStyleXfs 를 세면 번호가 밀린다 (xlsx 통합)
#=> <xf> 는 <cellStyleXfs>(이름 있는 스타일의 원본)에도 같은 이름으로 있다.
#   함께 세면 엉뚱한 칸이 날짜가 된다 — 멀쩡한 숫자가 날짜로 둔갑한다.
#
# -in: tmp_path = pytest 임시폴더 픽스처
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_xlsx_cellStyleXfs_는_세지_않는다(tmp_path):
    styles = ('<styleSheet>'
              '<cellStyleXfs count="2"><xf numFmtId="14"/><xf numFmtId="14"/></cellStyleXfs>'
              '<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs>'
              '</styleSheet>')
    sheet = ('<worksheet><sheetData><row>'
             '<c s="0"><v>41274</v></c><c s="1"><v>41274</v></c>'
             '</row></sheetData></worksheet>')
    p = tmp_path / "b.xlsx"
    p.write_bytes(_make_xlsx(styles, sheet))
    # 0번은 일반, 1번만 날짜. cellStyleXfs 를 셌다면 둘 다 날짜가 된다.
    assert XlsxExtractor().extract(str(p)) == "41274\t2012-12-31"


#------------------------------------------------------------------
# 글자 칸은 서식이 날짜여도 그대로 둔다 (xlsx 통합)
#=> 문자열 "41274" 가 날짜로 둔갑하면 안 된다.
#
# -in: tmp_path = pytest 임시폴더 픽스처
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_xlsx_글자칸은_날짜서식이어도_둔다(tmp_path):
    styles = '<styleSheet><cellXfs count="1"><xf numFmtId="14"/></cellXfs></styleSheet>'
    sheet = ('<worksheet><sheetData><row>'
             '<c t="inlineStr" s="0"><is><t>41274</t></is></c>'
             '<c t="str" s="0"><v>41274</v></c>'
             '</row></sheetData></worksheet>')
    p = tmp_path / "c.xlsx"
    p.write_bytes(_make_xlsx(styles, sheet))
    assert XlsxExtractor().extract(str(p)) == "41274\t41274"


#------------------------------------------------------------------
# 서식표가 없으면 날짜 해석을 하지 않는다 (xlsx 통합)
#=> styles.xml 이 없는 통합문서에서 엉뚱하게 숫자를 바꾸면 안 된다.
#
# -in: tmp_path = pytest 임시폴더 픽스처
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_xlsx_서식표가_없으면_숫자를_그대로(tmp_path):
    sheet = '<worksheet><sheetData><row><c s="1"><v>41274</v></c></row></sheetData></worksheet>'
    p = tmp_path / "d.xlsx"
    p.write_bytes(_make_xlsx(None, sheet))
    assert XlsxExtractor().extract(str(p)) == "41274"


#------------------------------------------------------------------
# 사용자 지정 서식 번호를 문자열로 판단한다 (xlsx 통합)
#
# -in: tmp_path = pytest 임시폴더 픽스처
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_xlsx_사용자지정_서식을_문자열로_판단(tmp_path):
    styles = ('<styleSheet>'
              '<numFmts count="2">'
              '<numFmt numFmtId="176" formatCode="yyyy-mm-dd"/>'
              '<numFmt numFmtId="177" formatCode="#,##0"/>'
              '</numFmts>'
              '<cellXfs count="2"><xf numFmtId="176"/><xf numFmtId="177"/></cellXfs>'
              '</styleSheet>')
    sheet = ('<worksheet><sheetData><row>'
             '<c s="0"><v>41274</v></c><c s="1"><v>41274</v></c>'
             '</row></sheetData></worksheet>')
    p = tmp_path / "e.xlsx"
    p.write_bytes(_make_xlsx(styles, sheet))
    assert XlsxExtractor().extract(str(p)) == "2012-12-31\t41274"


#------------------------------------------------------------------
# xls 숫자 표기 — Rust 판 num_str 과 같은 규칙인가
#=> xlrd 는 모든 숫자를 실수로 준다. 그대로 str() 하면 수량 9 가 "9.0" 이 된다 —
#   엑셀 화면에도, 사이냅 출력에도, Rust 판에도 없는 글자다. 본문에 없던 ".0"
#   이 붙으면 낱말이 달라져 "수량 9" 처럼 붙어 있어야 성립하는 규칙이 어긋난다.
#
# -in: 없음
# -out: 없음(assert)
# -out: error = 실패 시 AssertionError
#------------------------------------------------------------------
def test_xls_정수는_소수점_없이_쓴다():
    from csoclassify.extract.office_legacy import _num_str

    assert _num_str(9.0) == "9"
    assert _num_str(2024.0) == "2024"
    assert _num_str(0.0) == "0"
    assert _num_str(-7.0) == "-7"
    # 소수는 그대로 둔다 — 값을 바꾸면 안 된다.
    assert _num_str(1.5) == "1.5"
    assert _num_str(0.25) == "0.25"
    # 아주 큰 수는 정수로 바꾸면 정밀도를 잃는다(Rust 판과 같은 1e15 경계).
    assert "." in _num_str(1e16) or "e" in _num_str(1e16)
