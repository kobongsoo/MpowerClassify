#------------------------------------------------------------------
# HWPX(zip+OWPML) 텍스트 추출기 (설계서 CSO_HybridParse §8)
#=> HWPX 는 ZIP 컨테이너 안에 OWPML(XML)로 본문을 담는 한글 신형 포맷이다.
#   사이냅(snf_exe) 없이도 파이썬 표준 zipfile 만으로 Contents/section*.xml 에서
#   본문을 만든다. 외부 라이브러리 의존이 전혀 없어 어떤 배포 환경에서도 바로 동작한다.
#
#   [2026-09 문단 단위로 바꿈] 예전에는 <hp:t> **태그마다 한 줄**로 끊었다. 그런데
#   HWPX 는 글자 서식이 바뀌는 자리에서 <hp:t> 를 갈라 놓기 때문에, 한 낱말이 두
#   태그에 걸치면 그 자리에서 줄이 끊겼다 — 실측에서 사이냅이 "가치를·개발에·
#   보장한다"로 뽑은 것을 이 판은 "가치 / 를 / 개발 / 에" 로 내놨다.
#   낱말이 쪼개지면 **붙어 있어야 성립하는 검출**(전화·계좌번호, 앵커+값)이 통째로
#   어긋난다. docx·pptx 는 이미 문단 단위로 잇고 있었고 HWPX 만 그 보호를 못 받았다.
#   Rust 판(Rust/crates/docparse/src/extract.rs collect_hwpx_paragraphs)과 같은 규칙으로 맞춘다 —
#   두 판의 본문이 갈리면 같은 문서가 판마다 다른 등급을 받는다.
#------------------------------------------------------------------

import re
import zipfile

from .base import TextExtractor, ExtractError

# 관심 있는 자리만 한 번에 훑는 토큰 정규식(Rust 판 TOK 과 같은 순서·같은 갈래).
#=> 자기닫힘 <hp:p/> 갈래를 **맨 앞**에 둔다 — 뒤에 두면 여는 태그 갈래가 끝의
#   '/' 를 속성으로 삼켜 문단 경계를 놓친다.
# 그룹1(t)이 잡히면 글자, 그 밖은 문단 경계·줄바꿈·탭이다.
_TOK = re.compile(
    r"<hp:p(?:\s[^>]*?)?/>|<hp:p(?:\s[^>]*)?>|</hp:p>"
    r"|<hp:t(?:\s[^>]*)?>(.*?)</hp:t>"
    r"|<hp:lineBreak\b[^>]*>|<hp:tab\b[^>]*>", re.S)
# 런 내부에 남을 수 있는 인라인 태그(<hp:...> 등) 제거용.
_INNER_TAG = re.compile(r"<[^>]+>")
# 본문이 들어있는 파일만 대상: Contents/section0.xml, section1.xml, ...
_SECTION = re.compile(r"Contents/section\d+\.xml$", re.I)
# XML 기본 엔티티 복원표(&amp; 는 다른 엔티티 복원 후 마지막에 처리).
_ENTITIES = [("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&amp;", "&")]


#------------------------------------------------------------------
# section 번호로 정렬하기 위한 키
#=> "Contents/section10.xml" 이 "section2.xml" 보다 뒤에 오도록, 파일명 속
#   숫자를 뽑아 정수로 비교한다(문자열 정렬이면 10 이 2 앞에 오는 오류 방지).
#
# -in: name = zip 내부 항목 이름(예: "Contents/section3.xml")
#
# -out: int = section 번호(숫자 못 찾으면 0)
# -out: error = 없음
#------------------------------------------------------------------
def _section_no(name):
    m = re.search(r"section(\d+)\.xml$", name, re.I)
    # 숫자를 못 찾는 예외적 이름은 0 으로 두어 맨 앞에 오게 한다.
    return int(m.group(1)) if m else 0


#------------------------------------------------------------------
# HWPX 문단 수집 — 한 문단을 한 줄로
#=> 한 번만 훑으면서 <hp:t> 글자는 이어 붙이고, 문단이 열리거나 닫히는 자리에서
#   줄을 끊는다.
#    1) <hp:t> 글자는 '지금 줄'에 계속 이어 붙인다(낱말이 끊기지 않게)
#    2) 문단 경계에서 지금 줄을 끊어 낸다
#    3) <hp:lineBreak/> 는 줄바꿈, <hp:tab/> 은 탭으로 살린다
#       — 없애면 두 줄이 한 낱말로 붙어 문서에 없던 말이 생긴다
#
#   [왜 짝을 맞추지 않고 한 번만 훑나] HWPX 는 **문단이 문단 안에 들어간다**.
#   표가 <hp:p> 안의 <hp:tbl> → <hp:tc> → <hp:subList> → 또 <hp:p> 로 내려가기
#   때문이다(실측 10개 파일에 중첩 14,413곳). 짝 맞추기 정규식은 바깥 여는 태그와
#   **안쪽** 닫는 태그를 짝지어 버려 문단 경계가 엉킨다. 한 번만 훑으면 안쪽
#   문단(표 칸)도 제 줄을 갖고, 바깥 문단의 글자도 순서대로 남는다.
#
# -in: xml   = Contents/section*.xml 본문
# -in: parts = 결과 줄을 덧붙일 리스트(제자리에서 늘어난다)
#
# -out: 없음(parts 에 문단마다 한 줄씩 덧붙인다)
# -out: error = 없음
#------------------------------------------------------------------
def _collect_paragraphs(xml, parts):
    line = []

    # 지금까지 모은 줄을 끊어 낸다. 빈 줄은 버린다 — HWPX 는 빈 문단을 여백으로 쓴다.
    def flush():
        text = "".join(line)
        if text.strip():
            parts.append(text.rstrip())
        del line[:]

    for m in _TOK.finditer(xml):
        run = m.group(1)
        if run is not None:
            # <hp:t> 안의 글자 — 낱말이 끊기지 않게 '이어' 붙인다.
            # 런 안에 남은 인라인 태그는 지워 순수 텍스트만 남긴다.
            line.append(_INNER_TAG.sub("", run))
            continue
        whole = m.group(0)
        if whole.startswith("<hp:lineBreak"):
            line.append("\n")
        elif whole.startswith("<hp:tab"):
            line.append("\t")
        else:
            # 문단이 열리거나 닫히는 자리 — 여기가 줄 경계다.
            flush()
    flush()


class HwpxZipExtractor(TextExtractor):
    #------------------------------------------------------------------
    # 문서 → 원시 텍스트 (핵심)
    #=> HWPX(zip) 를 열어 section XML 들을 **문단 단위**로 이어 붙인다.
    #    1) zip 으로 열고, 이름이 Contents/section<n>.xml 인 항목만 번호순 정렬
    #    2) 각 XML 을 한 번만 훑으며 <hp:t> 글자는 '지금 줄'에 계속 이어 붙이고,
    #       문단이 열리거나 닫히는 자리에서 줄을 끊는다
    #    3) XML 엔티티(&lt; 등)를 원문자로 복원
    #    4) 문단을 개행으로 이어 반환(정제=clean_text 는 상위 파이프라인 담당)
    #   ※ save_dir 은 이 추출기가 직접 저장하지 않는다 — 하이브리드 추출기가
    #     '승자 엔진' 텍스트만 한 번 저장하도록 위임한다(중복 저장 방지, §base).
    #
    # -in: input_path = 추출할 .hwpx 경로
    # -in: save_dir   = (호환용) 여기서는 사용 안 함. 저장은 상위(HybridExtractor)가 담당
    #
    # -out: text = 추출된 본문 텍스트(런이 0개면 빈 문자열 → 상위가 폴백 판단)
    # -out: error = 파일 없음/ZIP 아님/손상 등은 ExtractError(→ HybridExtractor 가 snf 폴백)
    #------------------------------------------------------------------
    def extract(self, input_path, save_dir=None):
        parts = []
        try:
            # zip 컨테이너로 연다. HWPX 가 아니거나 손상되면 여기서 예외 → 폴백 신호.
            with zipfile.ZipFile(input_path) as z:
                # 본문 section 만, 번호 순서대로 처리해 문서 흐름을 유지한다.
                names = sorted((n for n in z.namelist() if _SECTION.search(n)),
                               key=_section_no)
                for n in names:
                    # OWPML 은 UTF-8. 깨진 바이트는 대체문자로 흡수(추출 자체는 계속).
                    data = z.read(n).decode("utf-8", "replace")
                    _collect_paragraphs(data, parts)
        except FileNotFoundError:
            # 경로는 호출부가 붙이므로 여기선 사유만(다른 추출기와 메시지 관례 통일).
            raise ExtractError("입력 파일 없음")
        except (zipfile.BadZipFile, OSError) as e:
            # ZIP 이 아니거나 읽기 실패 → 상위가 snf 로 폴백하도록 실패로 알린다.
            raise ExtractError(f"HWPX(zip) 열기 실패: {type(e).__name__}")

        text = "\n".join(parts)
        # XML 엔티티를 원문자로 되돌린다(&amp; 는 마지막이라 이중복원 안 됨).
        for a, b in _ENTITIES:
            text = text.replace(a, b)
        return text
