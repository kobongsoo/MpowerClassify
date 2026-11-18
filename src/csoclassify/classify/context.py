#------------------------------------------------------------------
# 파일명 신호 (Signal D) — 파일을 "열지 않고" 판단하는 값싼 신호
#=> 문서를 열지도(텍스트 추출도) 않고, 파일 "이름"만 보고 등급을 매긴다.
#   추출·임베딩이 필요 없어 초고속이라, 대량 코퍼스의 1차 선별에 쓴다.
#
#   [이렇게 검출]
#     · 폴더를 뺀 파일명에, 규칙의 filename 목록을 적용한다.
#       (예: 파일명에 "대외비" → C, "계약서" → S)
#     · filename 을 안 적은 규칙은 파일명 신호를 만들지 않는다. 본문용 terms 를
#       빌려 쓰지 않는다 — 그러면 "본문에 나오면 기밀"인 말이 이름에 있다는
#       이유만으로 같은 등급이 되기 때문이다(2026-09-08, 설계 4장).
#
#   ※ 파일을 열지 않으므로 "이름"만으로 판단한다 → 참고 신호. 최종 등급은
#     rule/sensitive/stamp/name/embed 를 융합(max)해 정한다.
#
#   [2026-09-08 제거] 경로 신호(Signal C)는 걷어냈다. 문서가 어디 놓여 있는지는
#   담당자가 옮기면 바뀌고, 실측에서 경로가 단독으로 정한 등급 8건이 전부
#   오탐이었다. 설계: plan/보안등급-신호정리-paths제거-20260908.html
#------------------------------------------------------------------

import os
from dataclasses import dataclass, field

from . import rules as R
from .rules import max_grade

# 파일명은 오해 소지가 있어(이름만으로 단정 못 함) 신뢰도를 낮게 둔다.
_NAME_CONF = 0.60


#------------------------------------------------------------------
# 파일명 신호(Signal D) 결과
#=> 파일명(basename)에서 걸린 키워드와 그 최종 등급을 담는다.
#
# -필드: grade         = 파일명 기반 등급(없으면 None)
# -필드: confidence    = 신뢰도(0~1)
# -필드: seed_eligible = 전파 seed 승격 가능 여부(파일명은 힌트라 항상 False)
# -필드: hits          = [{id, name, grade}] 근거
#------------------------------------------------------------------
@dataclass(frozen=True)
class NameSignal:
    grade: str
    confidence: float
    seed_eligible: bool
    hits: list = field(default_factory=list)

    #------------------------------------------------------------------
    # 직렬화용 dict
    #=> 분류 레코드의 signals.name 칸에 넣을 순수 dict.
    #
    # -in: 없음
    # -out: dict = {grade, confidence, seed_eligible, hits:[...]}
    # -out: error = 없음
    #------------------------------------------------------------------
    def as_dict(self):
        return {
            "grade": self.grade,
            "confidence": round(self.confidence, 3),
            "seed_eligible": self.seed_eligible,
            "hits": list(self.hits),
        }


#------------------------------------------------------------------
# 파일명 스캔(Signal D) — "파일 이름에 든 단어"로 등급
#=> 폴더는 빼고 파일 "이름"만 보고 등급을 매긴다(위치·내용 아님, 파일 안 엶).
#    1) 폴더를 뺀 순수 파일명만 소문자로 취한다.
#    2) 내용검사에 쓰던 그 기밀사전(keyword_rules)을 파일명에 그대로 적용 →
#       단어가 이름에 있으면 그 규칙의 등급을 하나 기록(파일명은 짧아 건수는 무의미).
#    3) 걸린 규칙들의 등급 중 가장 높은 것을 최종 등급으로.
#
#   예) 대외비_보고서.hwp   → "대외비"(C)        → C
#       계약서_한빛테크.docx → "계약서"(S)        → S
#       엠파워_설치안내.pptx → "엠파워"(C)        → C
#       회의록_초안.hwp      → 걸리는 단어 없음   → None
#   ※ 파일명은 실제 내용을 보증하지 못해(누가 이름만 그럴싸하게 지을 수 있음) 참고용
#     이며, 전파 seed 로는 절대 쓰지 않는다(seed_eligible 항상 False).
#
# -in: file    = 파일 경로
# -in: ruleset = RuleSet(keyword_rules 재사용)
#
# -out: NameSignal = 파일명 기반 등급 신호(없으면 grade=None)
# -out: error = 없음
#------------------------------------------------------------------
def scan_filename(file, ruleset):
    # 폴더를 뺀 순수 파일명만 대상으로 한다.
    base = os.path.basename(file or "").lower()
    hits = []
    for rule in ruleset.keyword_rules:
        # [2026-09-08] 파일명에서 찾을 말은 rule.filename 이다. 예전에는 rule.terms
        # 를 그대로 빌려 썼는데, 그 탓에 '본문에 나오면 기밀'인 단어가 파일 이름에
        # 있다는 이유만으로 같은 등급을 만들었다(실측: 파일명 신호 99건 중 79건이
        # 제품명 규칙 하나 — 제품 매뉴얼이 이름 때문에 C 가 됐다).
        # 안 적었으면 이 규칙은 파일명 신호를 만들지 않는다 — terms 로 되돌아가지
        # 않는다. 폴백을 두면 그 오탐이 기본값으로 굳는다(설계 4장 P2).
        names = getattr(rule, "filename", ()) or ()
        if not names:
            continue
        # 제외어(오탐 방지) 구간 — 내용 스캔과 동일 규칙을 파일명에도 적용(예: '전과' vs '산전과').
        ex_spans = R._exclude_spans(base, getattr(rule, "exclude", ()), True)
        for term in names:
            # 파일 이름에서도 영문 약어는 단어로만 센다('mou' ⊂ 'mouse.pdf' 는 아니다)
            if term and R._count_outside(base, term.lower(),
                                         R._word_ex(ex_spans, base, term.lower())) > 0:
                # 어떤 단어가 파일명에 있었는지 term 으로 남긴다(키워드라 노출 안전).
                hits.append({"id": rule.id, "name": rule.name,
                             "grade": rule.base_grade, "term": term})
                break   # 같은 규칙은 한 번만 기록(파일명은 건수 무의미)

    if not hits:
        return NameSignal(grade=None, confidence=0.0, seed_eligible=False, hits=[])

    name_conf = (getattr(ruleset, "confidence", None) or {}).get("name", _NAME_CONF)
    final = max_grade(h["grade"] for h in hits)
    return NameSignal(grade=final, confidence=name_conf, seed_eligible=False, hits=hits)
