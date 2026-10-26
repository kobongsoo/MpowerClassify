#------------------------------------------------------------------
# PDF 추출기 — pypdfium2 (설계서 CSO_HybridParse)
#=> PDF 텍스트층을 pdfium(구글 PDF 엔진) 바인딩으로 뽑는다. 순수 네이티브라
#   빠르고(실측 snf 2배·xberg 40배), 한글 PDF 읽기순서도 snf 와 사실상 동일
#   (실측 sim 0.96~1.00). manylinux2014(glibc 2.17) wheel 이 있어 CentOS 7 에서도
#   동작한다(xberg 가 못 하던 것). 스캔(이미지) PDF 는 텍스트층이 없어 빈 결과 →
#   상위가 snf 로 폴백한다(OCR 은 별도).
#------------------------------------------------------------------

import re

from .. import config
from ..logsetup import get_logger
from . import notes
from .base import TextExtractor, ExtractError, ParserDeadline

log = get_logger(__name__)

# pypdfium2 가 없으면 이 경로만 비활성(→ snf 폴백)되게 지연 취급.
try:
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_raw
except ImportError:
    pdfium = None
    pdfium_raw = None


#------------------------------------------------------------------
# 이 페이지가 스캔 페이지인가 (처리제외 판정 재료)
#=> 글자가 거의 없고, 그림이 페이지 넓이의 절반 이상을 덮으면 스캔 페이지로 본다.
#    1) 페이지 글자(공백 제외)가 config.SCAN_PDF_PAGE_MAX_CHARS 이상이면 바로 아니다
#       — 글자 많은 페이지(대부분의 정상 페이지)는 그림을 훑지 않아 느려지지 않는다
#    2) 페이지 바로 아래 그림 개체들의 영역을 페이지 안으로 잘라 넓이를 더한다
#    3) 덮은 비율이 config.SCAN_PDF_IMAGE_COVER 이상이면 스캔 페이지
#
#   [왜 페이지 바로 아래만 보나] Rust 판(pdfium-render)이 같은 범위를 본다. 실측
#   382건에서 폼 개체 안까지(깊이 15) 봐도 처리제외 판정은 69건으로 똑같았다.
#
# -in: page = pypdfium2 PdfPage
# -in: text = 그 페이지에서 뽑은 원문 텍스트
#
# -out: bool = 스캔 페이지면 True
# -out: error = 없음(크기가 0 인 이상한 페이지는 False)
#------------------------------------------------------------------
def _is_scan_page(page, text):
    # 글자가 있는 페이지는 스캔본이 아니다 — 그림을 볼 필요도 없다.
    if len(re.sub(r"\s", "", text or "")) >= config.SCAN_PDF_PAGE_MAX_CHARS:
        return False
    w, h = page.get_width(), page.get_height()
    if w <= 0 or h <= 0:
        return False
    covered = 0.0
    for obj in page.get_objects(filter=[pdfium_raw.FPDF_PAGEOBJ_IMAGE], max_depth=0):
        left, bottom, right, top = obj.get_bounds()
        # 페이지 밖으로 삐져나간 부분은 세지 않는다 — 넓이 비율이 1 을 넘지 않게.
        covered += (max(0.0, min(right, w) - max(left, 0.0))
                    * max(0.0, min(top, h) - max(bottom, 0.0)))
    return covered / (w * h) >= config.SCAN_PDF_IMAGE_COVER


#------------------------------------------------------------------
# pdfium 이 끼워 넣는 표식 글자 지우기
#=> pypdfium2 의 get_text_range 는 낱말 사이 · 줄 끝에 U+FFFE(유니코드 비문자)를 끼워
#   돌려준다(예: "Technology￾ Roadmap"). 본문 글자가 아니라 pdfium 내부 표식이다.
#   Rust 판(pdfium-render)은 이 글자를 돌려주지 않으므로, 남겨 두면 두 판의 본문이
#   달라지고 PII 패턴이 한쪽에서만 끊겨 판정 근거가 갈린다(2026-10-26 교차 검증).
#    1) U+FFFE 만 지운다 — 공백 · 줄바꿈 등 다른 글자는 그대로 둔다
#
# -in: text = get_text_range() 가 돌려준 페이지 본문(None 이면 빈 문자열로 본다)
#
# -out: str = 표식을 뺀 본문
# -out: error = 없음
#------------------------------------------------------------------
def _strip_pdfium_marks(text):
    return (text or "").replace("￾", "")


class PdfiumExtractor(TextExtractor):
    #------------------------------------------------------------------
    # 문서 → 원시 텍스트 (핵심)
    #=> PDF 를 열어 페이지마다 텍스트층을 뽑아 이어 붙인다.
    #    1) pypdfium2 없으면 실패(→ snf 폴백)
    #    2) 페이지마다 get_textpage().get_text_range() 로 본문 추출
    #       (pdfium 표식 U+FFFE 는 지운다 — Rust 판과 같은 본문을 내기 위해)
    #       (G5: 페이지 수·시간 상한을 넘으면 거기서 멈추고 읽은 만큼만 쓴다)
    #    3) 페이지들을 개행으로 결합(빈 결과면 상위가 폴백 판단)
    #   ※ save_dir 저장은 상위(HybridExtractor)가 승자 텍스트만 한 번 처리한다.
    #
    # -in: input_path = 추출할 .pdf 경로
    # -in: save_dir   = (호환용) 여기서는 사용 안 함
    #
    # -out: text = 추출 본문(텍스트층 없으면 빈 문자열 → snf 폴백)
    # -out: error = pypdfium2 없음/열기 실패(손상·암호화) 시 ExtractError
    #------------------------------------------------------------------
    def extract(self, input_path, save_dir=None):
        if pdfium is None:
            raise ExtractError("pypdfium2 미설치(PDF 파서 비활성)")
        try:
            doc = pdfium.PdfDocument(input_path)
        except Exception as e:   # noqa: BLE001  (손상/암호화 PDF 등)
            raise ExtractError(f"PDF 열기 실패: {type(e).__name__}")
        try:
            parts = []
            # 페이지·시간 상한(G5) — 설계: plan/문서크기-상한-설계-20260904.html
            # 지금까지 이 루프에는 아무 제한이 없어, 수천 페이지 PDF 한 건이
            # 배치 전체를 무한정 붙잡을 수 있었다. 두 겹으로 막는다:
            #   ① 시간 예산(B안) — 실제로 오래 걸리는 것을 직접 막는다
            #   ② 페이지 수(A안) — 시간이 널널해도 터무니없는 분량은 자른다
            # 넘겨도 예외를 던지지 않고 '그때까지 읽은 부분'을 돌려준다. 부분
            # 텍스트라도 등급 판정에는 대개 충분하고, 예외로 버리면 그 문서가
            # 미분류로 쌓이기 때문이다(base.ParserDeadline 주석 참고).
            deadline = ParserDeadline()
            max_pages = config.MAX_PDF_PAGES
            stopped = None
            # 스캔 페이지(글자 거의 없음 + 그림이 절반 이상) 수 — 처리제외 판정 재료.
            scan_pages = 0
            for i, page in enumerate(doc):
                if max_pages and 0 < max_pages <= i:
                    stopped = f"페이지 상한({max_pages}p)"
                    break
                if deadline.expired():
                    stopped = f"시간 상한({deadline.seconds}s)"
                    break
                # 페이지 텍스트층에서 전체 범위 문자열을 얻는다.
                tp = page.get_textpage()
                page_text = _strip_pdfium_marks(tp.get_text_range())
                parts.append(page_text)
                if _is_scan_page(page, page_text):
                    scan_pages += 1
            # 잘렸다면 반드시 로그로 남긴다 — 남기지 않으면 '왜 이 문서만 본문이
            # 적지'를 나중에 추적할 근거가 사라진다.
            if stopped:
                log.warning("PDF 부분 추출 %s 에서 중단 file=%s pages=%d/%d",
                            stopped, input_path, len(parts), len(doc))
                # 로그만으로는 결과를 보는 사람에게 안 닿는다 — 레코드까지 가도록
                # 표식을 남긴다(notes 모듈 주석 참고).
                notes.set_partial(stopped, "pages", len(parts), len(doc))
            # 페이지 구성을 남긴다 — 분류 쪽이 최종 본문 길이와 함께 '처리제외(스캔 PDF)'를
            # 가린다. 여기서 바로 가리지 않는 이유: 사이냅 폴백이 본문을 읽어 내면 스캔본이
            # 아니기 때문이다(판정은 '최종 본문' 기준이어야 한다).
            notes.set_layout(len(parts), scan_pages)
            return "\n".join(parts)
        finally:
            doc.close()
