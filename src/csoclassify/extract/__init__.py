#------------------------------------------------------------------
# 추출 서브패키지 초기화
#=> 문서 → 텍스트 추출기들을 모은 패키지. 기본(SynapExeExtractor)과 포맷별
#   조립형 추출기들, 이를 골라 주는 팩토리(build_extractor)를 최상위로 재노출한다.
#
# -필드: 없음(재노출 + 팩토리)
#------------------------------------------------------------------

from .base import TextExtractor, ExtractError, save_extracted_text
from .synap_exe import SynapExeExtractor
from .detect import detect_format
from .hwpx_zip import HwpxZipExtractor
from .hwp5 import Hwp5Extractor
from .pdf_pdfium import PdfiumExtractor
from .plaintext import PlainTextExtractor
from .html_text import HtmlTextExtractor
from .office_ooxml import DocxExtractor, XlsxExtractor, PptxExtractor
from .office_legacy import DocExtractor, XlsExtractor, PptExtractor
from .hybrid import HybridExtractor

__all__ = [
    "TextExtractor", "ExtractError", "save_extracted_text", "detect_format",
    "SynapExeExtractor", "HwpxZipExtractor", "Hwp5Extractor", "PdfiumExtractor",
    "PlainTextExtractor", "HtmlTextExtractor", "DocxExtractor", "XlsxExtractor",
    "PptxExtractor", "DocExtractor", "XlsExtractor", "PptExtractor",
    "HybridExtractor", "build_extractor",
]


#------------------------------------------------------------------
# 추출기 팩토리 (설계서 CSO_HybridParse §6.2)
#=> "이 실행에서 쓸 추출기 하나"를 만들어 준다. --hybridparse 여부만 받아서
#   기본은 사이냅 단독(현행), 하이브리드면 내용감지→포맷별 조립형 추출기를 반환한다.
#    1) hybrid=False → SynapExeExtractor()  (기존과 100% 동일)
#    2) hybrid=True  → HybridExtractor(감지타입→포맷별 파서 표 + snf 폴백)
#   snf 는 폴백 전용이라 snf_exe 가 없으면 snf=None 으로 두고, 전용 파서가 실패한
#   파일만 미분류로 처리한다(크래시하지 않음).
#
#    3) 형식별 파서는 extractor(없으면 config.EXTRACTOR)로 고른다
#       "py"   = 지금 파이썬 파서들 / "rust" = docparse(Rust 판과 같은 파서) — 감지도 Rust
#       설계: plan/Python-문서파서-Rust연동-설계-20261110.html §4-2·§4-3
#
# -in: hybrid    = True 면 하이브리드 라우팅 사용(기본 False=현행 사이냅 단독)
# -in: extractor = "py" | "rust" (None 이면 config.EXTRACTOR)
#
# -out: TextExtractor = 사용할 추출기 인스턴스
# -out: error = 기본 모드에서 snf_exe 를 못 찾으면 ExtractError(하이브리드는 snf 없이도 동작).
#               rust 를 골랐는데 docparse 모듈이 없으면 ExtractError(보통은 cli 가 시작할 때 1017 로 먼저 멈춘다)
#------------------------------------------------------------------
def build_extractor(*, hybrid=False, extractor=None):
    # 기본(플래그 없음): 지금까지와 완전히 동일하게 사이냅 단독(snf 없으면 여기서 에러).
    if not hybrid:
        return SynapExeExtractor()
    from .. import config
    which = (extractor or config.EXTRACTOR or "py").lower()
    # snf 는 폴백 전용 — 없으면(미설치) None 으로 두고 전용 파서 실패분만 미분류.
    try:
        snf = SynapExeExtractor()
    except ExtractError:
        snf = None
    if which == "rust":
        # Rust 문서파서: 형식별 엔진 표 전부를 docparse 로, 감지도 docparse 로 한다 —
        # 감지가 다르면 같은 파서를 써도 다른 엔진으로 가기 때문이다.
        from . import rust_engine
        ok, why = rust_engine.available()
        if not ok:
            raise ExtractError(why)
        return HybridExtractor(engines=rust_engine.engines(), snf=snf,
                               detector=rust_engine.detect_format)
    # 하이브리드: 감지타입 → 포맷별 전용 파서 표.
    engines = {
        "pdf":  PdfiumExtractor(),
        "hwp":  Hwp5Extractor(),
        "hwpx": HwpxZipExtractor(),
        "doc":  DocExtractor(),
        "xls":  XlsExtractor(),
        "ppt":  PptExtractor(),
        "docx": DocxExtractor(),
        "xlsx": XlsxExtractor(),
        "pptx": PptxExtractor(),
        "html": HtmlTextExtractor(),
        "text": PlainTextExtractor(),
    }
    return HybridExtractor(engines=engines, snf=snf)
