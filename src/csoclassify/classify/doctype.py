#------------------------------------------------------------------
# 업무 분류(doctype) 축 — 실제 매칭 엔진
#=> doc_rule.yaml 의 규칙을 문서 하나에 걸어 "이 문서가 어떤 분류체계 노드에
#   해당하는가" 후보를 만들고(설계서 5장), 조상 흡수(5-2)와 축 전략(6장,
#   conflict.py)을 적용해 최종 labels.doctype 을 만든다.
#   security 축의 스캔 엔진(키워드 매칭·제외어·경로 정규화)을 그대로 재사용
#   하고(설계서 5-1 note), "여러 신호 → 등급 하나"였던 security 와 달리
#   "여러 신호 → 노드 하나"를 노드마다 반복한다는 점만 다르다.
#
#   [재설계 1단계 반영] 내용 신호를 위치별로 쪼개고 점수를 누적한다.
#     · title  : 첫 비어있지 않은 줄       — 가장 강한 증거
#     · head   : 앞 head_chars 자(표제부)  — 강한 증거
#     · body   : 문서 전체 terms           — 약한 증거(단독 채택 불가)
#     · name   : 파일명                    — 중간 증거
#   결합은 noisy-OR(1 - Π(1-c))라 근거가 쌓일수록 점수가 오른다.
#
#   [2026-09-07 제거] 재설계 7장에는 form(서식 필드어)·structure(구조 정규식)·
#   path(폴더 경로) 세 신호도 있었지만, 규칙(doctype_rules)이 그 필드를 한 번도
#   채우지 않아 실제로는 한 번도 돌지 않았다. 도는 코드와 안 도는 코드가 섞여
#   있으면 읽는 사람이 매번 "이건 도는 건가"를 되짚어야 해서 걷어냈다.
#   다시 필요해지면 _backup/before-remove-form-path-structure-20260907.tgz.
#
#   [2026-09-22 제거] 예전 채점(scoring: legacy — 신호 중 최댓값, 본문 1건이면 히트)은
#   재설계 비교 측정용으로 남겨 두었으나, 운영 규칙이 모두 자동 생성(staged)이라
#   걷어냈다. 채점은 위 방식 하나뿐이다.
#   (설계: 문서분류체계 연동 설계서 §5·6, 업무분류-내용기반-2단계-재설계 §6~8)
#------------------------------------------------------------------

import os
import re
from dataclasses import dataclass

from . import rules as R
from . import taxhead
from .conflict import resolve_doctype

# ── 신호별 신뢰도 (재설계 8-2, 실측 전 잠정치) ─────────────────────
# 상대 순서(title ≳ head > name > body)가 핵심이며,
# 절대값은 16장 검증셋 측정 후 재보정 대상이다.
#
# => 계산법 : 예) title(0.65) + name(0.30) → 1 - 0.35×0.70 = 0.755
# 성질: 항상 [0,1) 안에 머물고, 근거가 늘수록 오르되 포화합니다. 
# 약한 증거를 아무리 모아도 강한 증거 하나를 못 넘습니다. 
# 종전 max() 의 "근거가 쌓여도 점수 제자리" 문제를 푸는 게 목적입니다.
#
# [표 읽는 법]
#   세로줄(title/head/…) = "문서의 어디를 보고 맞혔나" — 신호원 4가지.
#   가로줄(high/medium/low) = 그 규칙에 적힌 weight. 규칙을 만든 사람이
#     "이 규칙은 얼마나 믿을 만한가"를 3단계로 고른 값이다(기본 medium).
#   칸의 숫자 = 그 신호 하나가 갖는 확신. 이 값들을 위 noisy-OR 로 합친다.
#
# [4가지 신호원 — 무엇을 어디서 보는가]
#   아래 설명의 예시로 쓸 규칙 하나(weight: medium 이라 가운데 칸이 쓰인다):
#     node: DC_계약서
#     title_terms: [계약서]        head_terms: [계약기간]   terms: [갑, 을]
#     filename: [계약]
#
#   title (0.65) — 문서의 '첫 비어있지 않은 줄'에 title_terms 가 있나.
#       예) 첫 줄이 "용역 계약서" → 걸림. 사람이 문서 맨 위에 일부러 적어 둔
#           이름이라 판별력이 가장 세다. "출 장 여 비 규 정" 처럼 자간을 벌려
#           놓아도 잡는다(_despace).
#       ※ .ppt/.xls/.xlsx 는 첫 줄이 제목이 아니어서 이 신호를 건너뛴다.
#
#   head (0.55) — 앞 head_chars(기본 400)자 안에 head_terms 가 있나.
#       예) 제목 줄에는 없어도 "… 계약기간: 2026-01-01 …" 이 문서 앞머리에
#           나오면 걸림. 제목 다음으로 문서 성격이 잘 드러나는 구간이다.
#
#   name (0.30) — 폴더를 뺀 '파일 이름'에 filename 조각이 있나.
#       예) "2026_계약_최종.hwp" → 걸림. 짧고 일부러 붙인 이름이라 우연 일치는
#           드물다. 다만 이 신호 하나만으로는 t_low(0.35)를 못 넘어 후보가 되지
#           못한다 — 파일 이름만 보고 붙인 라벨은 오탐이 잦기 때문이다.
#
#   body (0.15) — 문서 '전체'에서 terms 를 센다(min_distinct·min_count 를 넘겼을 때만).
#       예) 본문 어딘가에 '갑'·'을'이 나옴. 어느 문서에나 나올 수 있는 말이라
#           가장 약하고, 단독으로는 후보를 만들지 못한다(_WEAK_ONLY).
#
# [name 의 세 칸이 같은 값인 이유]
#   표가 우연히 같은 게 아니라, 코드가 name 만 weight 를 보지 않고 "medium"
#   칸을 직접 집어 쓴다(_scan_rule 의 name 분기). 파일명은 '이름에 그 말이
#   있다'는 사실 하나뿐이라 규칙의 자신감으로 등급을 나눌 근거가 없다.
#   세 칸을 같은 값으로 채워 둔 것은, 나중에 등급을 주기로 하면 코드 한 줄만
#   고치면 되도록 자리를 비워 둔 것이다.
#
# [이 네 개가 전부다] 값은 doc_rule.yaml 의 signals: 로 바꿀 수 있다
#   (본보기와 설명은 doc_rule_template.yaml).
_SIG_CONF = {
    "title":     {"high": 0.80, "medium": 0.65, "low": 0.50},
    "head":      {"high": 0.70, "medium": 0.55, "low": 0.40},
    "name":      {"high": 0.30, "medium": 0.30, "low": 0.30},
    "body":      {"high": 0.25, "medium": 0.15, "low": 0.10},
}

# 이 신호들은 단독으로 후보를 만들지 못한다(재설계 8-4). 반드시 다른 신호와
# 결합해야 한다 — 이 규칙이 없으면 신뢰도를 아무리 낮춰도 conflict: all 에서
# 약한 후보가 그대로 남아 P1(본문 단어 1건 = 확정)이 재발한다.
_WEAK_ONLY = frozenset({"body"})

# 첫 줄이 제목이 아닌 포맷 — title 신호를 건너뛴다(재설계 6-3, 실측 근거).
#   .ppt   : 첫 줄이 제목인 비율 16.7%(도형 순서가 시각 순서와 다름)
#   .xls/.xlsx : 50%(첫 행이 데이터 행이라 제목 개념이 없음)
_TITLE_UNSAFE_EXTS = frozenset({".ppt", ".xls", ".xlsx"})


#------------------------------------------------------------------
# 표제부(head) 범위 뽑기 — 공백 묶음을 한 칸으로 본 앞 n 자
#=> 설계: plan/업무분류-표제부-공백정합-설계-20261019.html
#   예전에는 원문 앞 n 자를 잘랐다. 그러면 빈 줄 · 줄 앞 공백 · 탭을 다르게 남기는
#   파서끼리(자체 파서 ↔ 사이냅) 같은 문서에서 낱말이 경계 안팎으로 밀려 업무분류가
#   갈렸다(실측: '취업규칙'이 405번째 ↔ 390번째).
#    1) 공백 문자의 연속(공백 · 탭 · 줄바꿈 · 빈 줄)을 공백 한 칸으로 바꾼다
#    2) 맨 앞 공백은 버린다
#    3) 앞 n 자를 자른다(평가셋 500건에서 판정 변화 0건 — 창 크기 보정 불필요)
#   [Rust 판과의 관계] Rust/src/doctype.rs 의 head_scope 와 같은 결과여야 한다.
#   제목 · 본문 · 파일명 신호와 보안등급 · PII 는 이 함수를 쓰지 않는다(원문 그대로).
#
# -in: text = 정제된 문서 텍스트(None 이어도 된다)
# -in: n    = 표제부 글자 수(head_chars)
#
# -out: str = 표제부로 볼 글
# -out: error = 없음
#------------------------------------------------------------------
def head_scope(text, n):
    return re.sub(r"\s+", " ", text or "").lstrip()[:n]


#------------------------------------------------------------------
# 첫 비어있지 않은 줄 뽑기
#=> 재설계 6-3 의 "첫 줄 = 제목" 가설에 쓰는 값. 실측(354건)에서 개행이 없는
#   문서는 0건이었고 첫 줄 길이 중앙값은 13자, 60자 이하가 91% 였다.
#   공백만 있는 줄은 건너뛴다 — 추출기가 머리말 자리에 빈 줄을 남기는 일이 잦다.
#
# -in: text = 정제된 문서 텍스트
#
# -out: str = 첫 비어있지 않은 줄(없으면 빈 문자열)
# -out: error = 없음
#------------------------------------------------------------------
def _first_line(text):
    for line in (text or "").split("\n"):
        if line.strip():
            return line
    return ""


#------------------------------------------------------------------
# 공백 제거 사본 만들기 (제목의 '자간 벌리기' 대응)
#=> 한국어 공문·규정은 제목을 "출 장 여 비 규 정"처럼 한 글자씩 띄어 쓰는
#   관행이 있다. 이대로면 '규정' 이라는 단어가 절대 걸리지 않는다.
#   실측(354건)에서 첫 줄에 이 관행을 쓴 문서가 10건이었고, 전부 회수 대상인
#   규정·제안요청서 문서였다.
#   [적용 범위] 제목·표제부처럼 '짧은 구간'에만 쓴다. 문서 전체에 적용하면
#   줄바꿈까지 붙어 단어 경계를 넘는 오탐이 생긴다.
#
# -in: s = 원본 문자열
#
# -out: str = 모든 공백류를 제거한 문자열
# -out: error = 없음
#------------------------------------------------------------------
def _despace(s):
    return re.sub(r"\s+", "", s or "")


# 순수 ASCII 영숫자(+공백·하이픈·점)로만 이루어진 단어인지. 한글이 섞이면 False.
_ASCII_TERM = re.compile(r"^[0-9A-Za-z][0-9A-Za-z .\-_]*$")


#------------------------------------------------------------------
# 영문 약어를 단어 경계로 세기
#=> 한국어는 띄어쓰기가 없어 부분문자열 매칭이 맞지만, 영문은 정반대다.
#   'erd'(ERD, 개체관계도)를 부분문자열로 찾으면 "GERD"·"ordered"·"powered"
#   안에서 걸린다. 실측에서 실제로 의학 논문이 'DB설계서'로 분류됐다.
#   그래서 순수 ASCII 단어만 \b 경계를 붙여 센다 — 한글 단어는 종전 그대로다.
#
# -in: hay      = 검색 대상(소문자)
# -in: needle   = 찾을 ASCII 단어(소문자)
# -in: ex_spans = 제외 구간 [(start, end), ...]
#
# -out: int = 제외 구간 밖에서 단어 경계로 맞은 횟수
# -out: error = 없음 (정규식 컴파일 실패는 일어나지 않는다 — escape 하므로)
#------------------------------------------------------------------
def _count_word(hay, needle, ex_spans):
    total = 0
    for m in re.finditer(r"\b" + re.escape(needle) + r"\b", hay):
        i, j = m.span()
        if not any(s <= i and j <= e for s, e in ex_spans):
            total += 1
    return total


#------------------------------------------------------------------
# 단어 1개 세기 — 언어에 맞는 방식 고르기
#=> ASCII 단어면 경계 기반, 그 밖(한글 포함)이면 종전 부분문자열 기반.
#   security 축과 동작을 맞춰야 하는 곳은 여전히 R._count_outside 를 쓴다.
#
# -in: hay      = 검색 대상(소문자)
# -in: needle   = 찾을 단어(소문자)
# -in: ex_spans = 제외 구간
#
# -out: int = 등장 횟수
# -out: error = 없음
#------------------------------------------------------------------
def _count_term(hay, needle, ex_spans):
    if _ASCII_TERM.match(needle):
        return _count_word(hay, needle, ex_spans)
    return R._count_outside(hay, needle, ex_spans)


#------------------------------------------------------------------
# 지정 구간에서 단어 찾기 (title/head/body 공통)
#=> rules.py 의 _exclude_spans/_count_outside 를 그대로 재사용해, 제외어 구간
#   안에 든 매치는 세지 않는다(한국어는 띄어쓰기가 없어 부분문자열 오탐이 잦다).
#   "어디를 볼 것인가"만 호출자가 정하고, 세는 방식은 security 축과 완전히 같다.
#
# -in: scope   = 검색 대상 문자열(이미 잘라 낸 구간)
# -in: terms   = 찾을 단어 목록
# -in: exclude = 제외어 목록
# -in: compact = True 면 공백을 지운 사본에서도 찾아 본다(제목의 자간 벌리기 대응).
#                건수는 두 결과의 '최댓값'을 쓴다 — 합치면 같은 매치를 두 번 세게 된다
#
# -out: list[(단어, 건수)] = 걸린 단어와 건수. 없으면 빈 리스트
# -out: error = 없음
#------------------------------------------------------------------
def _find_terms(scope, terms, exclude, compact=False):
    if not terms or not scope:
        return []
    hay = scope.lower()
    ex_spans = R._exclude_spans(hay, exclude, True)

    hay_c = ex_c = None
    if compact:
        hay_c = _despace(hay)
        # 제외어도 같은 규약으로 눕혀야 "가이드라인" 같은 제외가 공백 제거
        # 사본에서 그대로 살아남는다.
        ex_c = R._exclude_spans(hay_c, [_despace(str(e)) for e in (exclude or ())], True)

    hits = []
    for term in terms:
        if not term:
            continue
        needle = str(term).lower()
        c = _count_term(hay, needle, ex_spans)
        if compact:
            # 공백 제거 사본에서는 ASCII 단어의 경계가 무너지므로(예: "user guide"
            # → "userguide") 한글 자간 벌리기 대응이 목적인 이 경로는 한글 단어에만
            # 의미가 있다. ASCII 단어는 원본 결과를 그대로 쓴다.
            if not _ASCII_TERM.match(needle):
                c = max(c, R._count_outside(hay_c, _despace(needle), ex_c))
        if c:
            hits.append((term, c))
    return hits


#------------------------------------------------------------------
# 규칙별 임계값 고르기
#=> 규칙에 값이 적혀 있으면 그것을, 없으면 전역 defaults 값을 쓴다.
#   "규칙이 전역을 이긴다"는 한 줄 규약을 한 곳에 모아 둔다.
#
# -in: rule     = DoctypeRule
# -in: defaults = doc_rules.Defaults
# -in: name     = 필드 이름("head_chars"|"min_distinct"|"min_count")
#
# -out: int = 적용할 값
# -out: error = 없음
#------------------------------------------------------------------
def _thr(rule, defaults, name):
    v = getattr(rule, name, None)
    return getattr(defaults, name) if v is None else v


#------------------------------------------------------------------
# noisy-OR 결합 (재설계 8-1)
#=> 독립 증거들을 합쳐 하나의 점수로 만든다. score = 1 - Π(1 - cᵢ).
#   단순 덧셈과 달리 항상 [0,1) 안에 머물고, 근거가 늘수록 단조 증가하되
#   포화한다 — 약한 증거를 아무리 모아도 강한 증거 하나를 넘지 못한다.
#   이것이 종전 max() 의 문제(근거가 쌓여도 점수가 그대로)를 푼다.
#
# -in: parts = [{"signal": 이름, "c": 신뢰도}, ...]
#
# -out: float = 결합 점수(parts 가 비면 0.0)
# -out: error = 없음
#
# => 계산법 : 예) title(0.65) + name(0.30) → 1 - 0.35×0.70 = 0.755
# 성질: 항상 [0,1) 안에 머물고, 근거가 늘수록 오르되 포화합니다. 
# 약한 증거를 아무리 모아도 강한 증거 하나를 못 넘습니다. 
# 종전 max() 의 "근거가 쌓여도 점수 제자리" 문제를 푸는 게 목적입니다.
#------------------------------------------------------------------
def _noisy_or(parts):
    remain = 1.0
    for p in parts:
        remain *= (1.0 - float(p["c"]))
    return 1.0 - remain


#------------------------------------------------------------------
# 파일명 매칭 — DoctypeRule.filename
#=> context.py 의 scan_filename 과 같은 방식(폴더를 뺀 파일명만, exclude 도
#   동일하게 적용). 파일명은 짧고 의도적으로 붙인 이름이라 우연 일치가 드물어,
#   재설계에서도 매칭 로직을 그대로 유지한다(비중만 재조정).
#
# -in: file = 파일 경로(폴더 포함)
# -in: rule = DoctypeRule
#
# -out: list[(단어, 건수)] = 파일명에서 걸린 단어들
# -out: error = 없음
#------------------------------------------------------------------
def _match_filename(file, rule):
    base = os.path.basename(file or "")
    if not base or not rule.filename:
        return []
    return _find_terms(base, rule.filename, rule.exclude, compact=True)


#------------------------------------------------------------------
# 규칙 1개 스캔 — 신호원 수집 + 점수화 (핵심)
#=> 한 규칙(=노드)에 대해 모든 신호원을 확인하고, 점수·근거·구성요소를 만든다.
#    1) 신호원별 매칭 — title/head/body/name
#    2) noisy-OR 결합 + 단독 채택 금지(8-4) + t_low 미만 탈락(8-3)
#    3) 근거(evidence)를 남긴다 — P6(근거 미보존) 해소는 계측의 전제조건이다
#
# -in: text      = 정제된 문서 텍스트
# -in: file      = 파일 경로
# -in: rule      = DoctypeRule(active=True 인 것만 호출자가 넘겨야 함)
# -in: defaults  = doc_rules.Defaults
# -in: allow_title = False 면 title 신호를 건너뛴다(ppt/xls/xlsx, 6-3)
# -in: sig        = 신호별 신뢰도 표(doc_rule.yaml 의 signals:). None 이면 _SIG_CONF
#
# -out: dict | None = {score, sources, evidence, parts} — 후보가 아니면 None
# -out: error = 없음
#------------------------------------------------------------------
def _scan_rule(text, file, rule, defaults, allow_title=True, sig=None):
    # 정책 파일의 signals: 블록이 있으면 그 표를, 없으면 코드 기본값을 쓴다.
    sig = sig or _SIG_CONF
    evidence = {}
    parts = []

    def add(signal, conf, ev):
        evidence[signal] = ev
        parts.append({"signal": signal, "c": conf})

    # ── title: 첫 비어있지 않은 줄 ──────────────────────────────
    if allow_title and rule.title_terms:
        hits = _find_terms(_first_line(text), rule.title_terms, rule.exclude, compact=True)
        if hits:
            add("title", sig["title"].get(rule.weight, 0.65),
                {"terms": [{"term": t, "count": c} for t, c in hits]})

    # ── head: 앞 head_chars 자(공백 묶음은 한 칸으로 센다) ───────────
    if rule.head_terms:
        n = _thr(rule, defaults, "head_chars")
        # 파서마다 다른 빈 줄·줄 앞 공백이 경계를 밀지 않게 head_scope 로 범위를 정한다.
        hits = _find_terms(head_scope(text, n), rule.head_terms, rule.exclude, compact=True)
        if hits:
            add("head", sig["head"].get(rule.weight, 0.55),
                {"terms": [{"term": t, "count": c} for t, c in hits], "window": n})

    # ── body: 문서 전체 terms ──────────────────────────────────
    if rule.terms:
        hits = _find_terms(text, rule.terms, rule.exclude)
        total = sum(c for _t, c in hits)
        distinct = len(hits)
        need_d = _thr(rule, defaults, "min_distinct")
        need_c = _thr(rule, defaults, "min_count")
        if hits and distinct >= need_d and total >= need_c:
            add("body", sig["body"].get(rule.weight, 0.15),
                {"terms": [{"term": t, "count": c} for t, c in hits],
                 "distinct": distinct, "total": total,
                 "min_distinct": need_d, "min_count": need_c})

    # ── name: 파일명 ───────────────────────────────────────────
    nm = _match_filename(file, rule)
    if nm:
        # 파일 이름 신호는 규칙의 weight 를 보지 않고 medium 칸만 쓴다.
        add("name", sig["name"]["medium"], {"terms": [t for t, _c in nm]})

    if not parts:
        return None

    sources = tuple(p["signal"] for p in parts)

    # 보조 신호만으로는 후보를 만들지 않는다(8-4).
    if set(sources) <= _WEAK_ONLY:
        return None

    score = _noisy_or(parts)
    # 약한 후보 임계 미만은 후보에서 뺀다(8-3). 이 컷이 없으면 conflict: all
    # 에서 약한 증거가 그대로 라벨로 남아 다중 라벨이 다시 늘어난다.
    if score < defaults.t_low:
        return None

    return {"score": score, "sources": sources, "evidence": evidence, "parts": parts}


#------------------------------------------------------------------
# 조상 흡수(설계서 5-2)
#=> 같은 가지의 조상-자손이 함께 걸리면 조상은 버리고 자손만 남긴다 — 자손의
#   path_ids 에 조상이 이미 들어 있으므로 별도 라벨로 중복 낼 이유가 없다.
#   서로 다른 가지끼리 걸린 경우(계약서+제안서)는 조상 관계가 아니므로 그대로
#   둘 다 남는다(이게 진짜 충돌이고, conflict 전략이 다루는 대상이다).
#
# -in: dc_ids    = 매칭된 dc_id 반복가능 객체
# -in: taxonomy  = axes.Taxonomy
#
# -out: set[str] = 조상이 제거된 dc_id 집합
# -out: error = taxonomy 에 없는 dc_id 가 섞이면 KeyError(path_ids 가 던짐 —
#                호출자가 taxonomy.get() 으로 이미 걸러진 dc_id만 넘겨야 함)
#------------------------------------------------------------------
def _absorb_ancestors(dc_ids, taxonomy):
    matched = set(dc_ids)
    survivors = set(matched)
    for dc_id in matched:
        ancestors = set(taxonomy.path_ids(dc_id)[:-1])   # 자기 자신 제외
        survivors -= (ancestors & matched)
    return survivors


#------------------------------------------------------------------
# 근거 두 칸(evidence·score_parts)을 신호별 한 덩어리로 합친다
#=> 예전에는 후보 하나에 근거가 두 군데로 흩어져 있었다.
#     evidence    = {"head": {무슨 말이 몇 번}, "body": {...}}
#     score_parts = [{"signal":"head","c":0.55}, {"signal":"body","c":0.15}]
#   둘 다 열쇠가 '신호 이름'으로 같아서, 읽는 쪽은 매번 {이름:점수} 표를 만들어
#   이름으로 맞춰 붙여야 했다(화면이 실제로 그렇게 했다). 합쳐서 내보내면
#   "head 신호는 0.55점이고 이런 말이 걸렸다"가 한 덩어리로 읽힌다.
#
#   [c 가 없을 수 있다] 전파(embed)가 이미 있는 후보에 근거만 더하는 경로가
#   있다 — 그때는 점수 몫이 따로 없다. 그런 신호는 "c" 키를 만들지 않는다
#   (0.0 으로 채우면 "0점 기여"라는 없는 사실을 말하게 된다).
#
# -in: evidence = {신호이름: 근거dict} 또는 None
# -in: parts    = [{"signal":이름,"c":점수}] 또는 None
#
# -out: dict = {신호이름: {"c":점수(있을 때만), **근거필드}} — 순서는 evidence
#               가 만들어진 순서(title→head→body→name→embed)를 그대로 따른다
# -out: error = 없음
#------------------------------------------------------------------
def _merge_signals(evidence, parts):
    ev = dict(evidence or {})
    conf_by = {p["signal"]: round(float(p["c"]), 3)
               for p in (parts or []) if isinstance(p, dict)}
    out = {}
    for name, block in ev.items():
        one = {}
        # 점수를 앞에 둔다 — "얼마나 셌나"를 먼저 읽고 "왜"를 뒤에 읽는다.
        if name in conf_by:
            one["c"] = conf_by[name]
        one.update(block or {})
        out[name] = one
    # 근거 없이 점수만 있는 신호는 없어야 하지만, 있으면 잃지 않고 담는다.
    for name, c in conf_by.items():
        if name not in out:
            out[name] = {"c": c}
    return out


#------------------------------------------------------------------
# 합쳐진 signals 를 다시 evidence·score_parts 로 되돌린다
#=> 엔진 속 계산(_finalize·전파 병합)은 여전히 두 칸으로 다룬다 — 레코드에
#   적는 모양만 바꿨기 때문이다. 레코드를 다시 읽어 전파를 돌릴 때 이 함수로
#   원래 모양을 되찾는다.
#   [옛 레코드] signals 가 없으면 예전 두 칸을 그대로 읽는다 — 안 그러면
#   전파를 한 번 거친 옛 문서가 "왜 이 라벨인지"를 통째로 잃는다.
#
# -in: v = 레코드에 적힌 후보 dict
#
# -out: (evidence, parts) = 엔진 내부가 쓰는 두 칸
# -out: error = 없음
#------------------------------------------------------------------
def _split_signals(v):
    sigs = v.get("signals")
    if not isinstance(sigs, dict):
        return dict(v.get("evidence") or {}), list(v.get("score_parts") or [])
    evidence, parts = {}, []
    for name, one in sigs.items():
        one = dict(one or {})
        c = one.pop("c", None)
        evidence[name] = one
        if c is not None:
            parts.append({"signal": name, "c": c})
    return evidence, parts


#------------------------------------------------------------------
# doctype 축 결과(labels.doctype)
#=> 설계서 7-1 의 labels.doctype 레코드를 그대로 담는다.
#
# -필드: values    = 채택된 후보 dict 튜플. 각 항목
#                     {dc_id, path, path_ids, confidence, from, stage, signals}
# -필드: strategy  = 실제로 적용된 전략("all"|"top_n"). 레코드에는 --conflict 로
#                     덮어썼을 때만 실린다(그때만 규칙 파일에서 되짚을 수 없다)
# -필드: status    = 항상 "proposed"(자동 판정은 확정하지 않는다, 설계서 7-4-1).
#                     값이 하나뿐이라 2026-09-10 부터 레코드에는 싣지 않는다.
#                     사람이 확정한 결과는 cso_override.jsonl 에 따로 쌓인다
# -필드: truncated = top_n 으로 잘려나간 후보 수(all 이면 항상 0).
#                     레코드에는 0보다 클 때만 실린다
# -필드: conflicts = 계층 정합성 경고 목록(재설계 11-3). 자동으로 후보를 버리지
#                     않고 검토 큐 우선순위 신호로만 쓴다
#------------------------------------------------------------------
@dataclass(frozen=True)
class DoctypeSignal:
    values: tuple = ()
    strategy: str = "all"
    status: str = "proposed"
    truncated: int = 0
    conflicts: tuple = ()

    #------------------------------------------------------------------
    # 직렬화용 dict
    #=> 분류 레코드의 labels.doctype 칸에 넣을 순수 dict.
    #   [프라이버시] evidence 에는 규칙에 정의된 단어와 그 건수만 담는다 —
    #   문서 원문 조각(스니펫)은 절대 넣지 않는다.
    #
    # -in: 없음
    #
    # -out: dict = {values:[{dc_id,path,path_ids,confidence,from,stage,signals}]
    #               [, truncated][, conflicts]}
    #               — truncated/conflicts 는 값이 있을 때만. strategy 는
    #                 --conflict 로 덮어썼을 때 cli 가 따로 각인한다.
    # -out: error = 없음
    #------------------------------------------------------------------
    def as_dict(self):
        out = []
        for v in self.values:
            item = {
                "dc_id": v["dc_id"],
                "path": v["path"],
                "path_ids": list(v["path_ids"]),
                "confidence": round(v["confidence"], 3),
                "from": list(v["from"]),
            }
            # 아래 두 칸은 재설계 13-2 의 근거 블록. 없으면(구버전 경로로 만든
            # 후보) 키 자체를 만들지 않아, 소비자가 "안 씀"과 "비었음"을 구분할 수 있다.
            if v.get("stage"):
                item["stage"] = v["stage"]
            # 파일명 핵어로만 붙은 약한 라벨은 '검토 대상'으로 표시한다(13장 D1 ②).
            # 값이 있을 때만 적는다 — 없으면 "그냥 보통 라벨"이라는 뜻이다.
            if v.get("review"):
                item["review"] = True
                item["basis"] = v.get("basis") or "name_head"
            sigs = _merge_signals(v.get("evidence"), v.get("score_parts"))
            if sigs:
                item["signals"] = sigs
            out.append(item)
        res = {"values": out}
        # [2026-09-10] 값이 하나뿐이던 칸들을 걷어냈다.
        #  · status  : 엔진은 "proposed" 말고 다른 값을 낼 수 없다. 확정은 검토
        #    화면의 몫이고 그 결정은 cso_override.jsonl 에 쌓인다(원본 레코드는
        #    고치지 않는다). 문서마다 같은 글자를 적어도 새로 알려 주는 게 없다.
        #  · truncated: 안 잘렸으면 0이다. 없으면 '아무것도 안 잘림' 이라는 뜻이고,
        #    **잘렸을 때는 반드시 보인다** — 무엇이 왜 빠졌는지는 숨기면 안 된다.
        #  · conflicts: 비었으면 적지 않는다(같은 이유).
        #  · strategy : 규칙 파일에 적힌 값을 그대로 옮기던 칸이다. 새 정보는
        #    '실행 시 --conflict 로 덮어썼다'는 사실뿐이라, 그때만 cli 가 각인한다
        #    (6-5 재현성). 없으면 "규칙 파일이 정한 대로" —
        #    어느 규칙 파일인지는 meta.doctype_rule_version 이 가리킨다.
        if self.truncated:
            res["truncated"] = self.truncated
        if self.conflicts:
            res["conflicts"] = list(self.conflicts)
        return res


#------------------------------------------------------------------
# title 신호를 쓸 수 있는 포맷인지
#=> 실측(재설계 3-B-3)에서 .ppt 는 첫 줄이 제목인 비율이 16.7%,
#   .xls/.xlsx 는 50% 였다. 이 포맷들은 첫 줄 신호를 끄고 표제부(head)로
#   폴백해야 오탐을 만들지 않는다.
#
# -in: file = 파일 경로
#
# -out: bool = title 신호를 써도 되면 True
# -out: error = 없음
#------------------------------------------------------------------
def _title_allowed(file):
    return os.path.splitext(file or "")[1].lower() not in _TITLE_UNSAFE_EXTS


#------------------------------------------------------------------
# doctype 축 스캔(핵심 진입점)
#=> 문서 하나(텍스트+경로)를 doc_rule.yaml 규칙 전체에 걸어 최종 DoctypeSignal
#   을 만든다.
#    1) 활성 규칙마다 신호원을 합쳐 노드별 (점수, from, 근거) 후보를 모은다
#    2) 같은 노드를 두 규칙이 참조하면 점수가 더 높은 쪽을 남기고 from 은 합집합
#    3) 조상 흡수(5-2) — 같은 가지의 상위 노드는 자손이 있으면 버린다
#    4) conflict.resolve_doctype 으로 축 전략(all/top_n) 적용
#    5) DoctypeSignal 로 포장(status 는 항상 "proposed")
#
# -in: text          = 정제된 문서 텍스트(없으면 파일명·경로 신호만으로 판단)
# -in: file          = 파일 경로(파일명·경로 신호에 씀)
# -in: doc_rule_set  = doc_rules.load_doc_rules() 결과(DocRuleSet)
# -in: taxonomy      = 자동 생성 규칙에서 지은 axes.Taxonomy — 경로·조상 계산에 필수
#
# -out: DoctypeSignal = 매칭 결과(후보가 없으면 values=())
# -out: error = 없음 (문서에 아무 신호도 없으면 그냥 빈 결과)
#------------------------------------------------------------------
def scan_doctype(text, file, doc_rule_set, taxonomy):
    defaults = getattr(doc_rule_set, "defaults", None)
    if defaults is None:
        from .doc_rules import Defaults
        defaults = Defaults()
    allow_title = _title_allowed(file)
    # 신호별 신뢰도는 정책 파일에서 덮어쓸 수 있다(doc_rule.yaml 의 signals:).
    # 없으면 None 이라 _scan_rule 이 코드 기본값으로 돈다.
    sig = getattr(doc_rule_set, "signals", None)

    merged = {}   # dc_id -> {"confidence","from","evidence","parts"}

    for rule in doc_rule_set.active_rules:
        hit = _scan_rule(text, file, rule, defaults, allow_title, sig)
        if hit is None:
            continue
        node = taxonomy.get(rule.node)
        if node is None:
            # T5 가 로드 시점에 이미 이런 규칙을 막지만, taxonomy 없이 로드된
            # doc_rule_set(교차검증 생략)이 넘어올 수도 있으니 여기서도 방어한다.
            continue

        entry = merged.get(rule.node)
        if entry is None:
            merged[rule.node] = {
                "confidence": hit["score"], "from": set(hit["sources"]),
                "evidence": hit["evidence"], "parts": hit["parts"],
            }
        else:
            entry["from"].update(hit["sources"])
            # 같은 노드를 두 규칙이 맞혔다면 더 강하게 맞힌 쪽의 근거를 남긴다 —
            # 근거와 점수가 서로 다른 규칙에서 나오면 감사 때 설명이 안 된다.
            if hit["score"] > entry["confidence"]:
                entry["confidence"] = hit["score"]
                entry["evidence"] = hit["evidence"]
                entry["parts"] = hit["parts"]

    # ── 1층 핵어(13장 D1) — 다른 후보가 하나도 없을 때만 ───────────
    # 파일명은 바뀔 수 있는 문자열이라 이것만으로 확정하지 않는다. 그래서
    # ① 규칙이 아무 후보도 못 만든 문서에만 작동하고 ② 검토 대상으로 표시하며
    # ③ 임베딩 씨앗으로는 쓰지 않는다(seedgen.candidate_ok).
    if not merged:
        _add_name_head(merged, file, doc_rule_set, taxonomy, defaults)

    return _finalize(merged, taxonomy, doc_rule_set.conflict)


#------------------------------------------------------------------
# 파일명 끝자리 핵어로 약한 후보 하나 만들기 (13장 D1)
#=> 분류체계 제목에서 유도한 핵어 사전(doc_rule_set.head_lexicon)을 파일 이름의
#   끝자리에 걸어 본다. 걸리면 문턱값(t_low)짜리 후보 하나를 만든다 —
#   "파일 이름 어딘가에 그 말이 있다"(name 0.30, 문턱 미달)와 달리 '핵어 자리'에
#   왔으므로 문턱은 넘기되, 확정이 아니라 검토 대상으로 둔다.
#   분류체계 파일이 없거나 쓸 만한 제목이 없으면 사전이 비어 아무 일도 안 한다.
#
# -in: merged       = 노드별 후보 dict(제자리에서 고친다 — 비어 있을 때만 불린다)
# -in: file         = 파일 경로
# -in: doc_rule_set = DocRuleSet(head_lexicon·head_noise 를 읽는다)
# -in: taxonomy     = axes.Taxonomy(사전이 낡아 없는 노드를 가리키면 버린다)
# -in: defaults     = doc_rules.Defaults(t_low 를 점수로 쓴다)
#
# -out: 없음(merged 를 고친다)
# -out: error = 없음
#------------------------------------------------------------------
def _add_name_head(merged, file, doc_rule_set, taxonomy, defaults):
    lexicon = getattr(doc_rule_set, "head_lexicon", None) or {}
    hit = taxhead.match_filename_head(file, lexicon,
                                      getattr(doc_rule_set, "head_noise", None))
    if not hit:
        return
    dc_id, word, title = hit
    # 스냅샷이 갱신돼 사전이 낡았을 수 있다 — 없는 노드면 조용히 버린다.
    if taxonomy.get(dc_id) is None:
        return
    conf = float(defaults.t_low)
    merged[dc_id] = {
        "confidence": conf,
        "from": {"name_head"},
        # 유도된 말은 파일 어디에도 안 적혀 있으므로 출처를 남긴다(7장 ②).
        "evidence": {"name_head": {"terms": [word], "taxonomy_title": title,
                                   "source": "doc_taxonomy"}},
        "parts": [{"signal": "name_head", "c": conf}],
        "review": True,
        "basis": "name_head",
    }


#------------------------------------------------------------------
# 계층 정합성 검사 (재설계 11-3)
#=> 1계층(주제)과 2계층(양식)은 서로 다른 엔진이 결정하므로 모순이 생길 수 있다.
#   조상-자손 관계가 아닌 노드가 서로 다른 1계층 가지에 걸리면 모순으로 보고
#   플래그만 남긴다 — 자동으로 하나를 버리지 않는다(자동 판정은 확정하지 않는다).
#
# -in: dc_ids   = 조상 흡수 후 살아남은 dc_id 집합
# -in: taxonomy = axes.Taxonomy
#
# -out: tuple[dict] = [{"type","dc_ids","detail"}] — 모순이 없으면 빈 튜플
# -out: error = 없음
#------------------------------------------------------------------
def _check_hierarchy(dc_ids, taxonomy):
    ids = sorted(dc_ids)
    if len(ids) < 2:
        return ()
    # 각 후보의 최상위 조상(1계층)을 모은다. 두 종류 이상이면 서로 다른
    # 업무 영역이 동시에 제안된 것 — 사람이 먼저 봐야 할 문서다.
    roots = {i: taxonomy.path_ids(i)[0] for i in ids}
    if len(set(roots.values())) < 2:
        return ()
    return ({
        "type": "cross_branch",
        "dc_ids": ids,
        "detail": "서로 다른 1계층 가지의 후보가 동시에 제안됐습니다 — 검토 우선순위 상향",
    },)


#------------------------------------------------------------------
# BM25 1위를 병합 중간 상태에 얹기
#=> 설계서 §08 · §14-4.
#    1) 근거 블록: 점수 · 문턱 · 어느 필드에서 맞았나(field) · 파일명에서 겹친 말(words)
#    2) 1차 규칙 근거가 있는 노드 → 근거만 더한다
#    3) 벡터만 낸 노드 → 합의, 확신을 cap_agree 까지
#    4) 새 노드 → 파일명에서만 맞았으면 name_only_cap, 아니면 cap_alone. review 표시
#
# -in: merged = 노드별 중간 상태 dict(고쳐 쓴다)
# -in: pick   = BM25 고른 결과 dict
# -in: spec   = doc_rules.Bm25Spec
#
# -out: 없음(merged 를 고침)
# -out: error = 없음
#------------------------------------------------------------------
def _add_bm25(merged, pick, spec):
    dc_id = pick["dc_id"]
    in_title, in_name = pick.get("title", 0.0) > 0, pick.get("name", 0.0) > 0
    ev = {"score": round(float(pick["score"]), 3), "threshold": round(float(pick["threshold"]), 3),
          "field": "title+name" if in_title and in_name else ("title" if in_title else "name")}
    if pick.get("words"):
        ev["words"] = list(pick["words"])[:5]
    entry = merged.get(dc_id)
    if entry is not None and entry["from"] - {"embed", "bm25"}:
        # 1차 규칙이 이미 붙인 분류 — 근거만 늘 뿐 확신은 그대로
        entry["from"].add("bm25")
        entry["evidence"]["bm25"] = ev
        return
    if entry is not None:
        # 벡터와 같은 분류를 냈다 — 두 근거가 합의하면 벡터 단독보다 높게
        conf = max(entry["confidence"], spec.cap_agree)
        entry["confidence"] = conf
        entry["from"].add("bm25")
        entry["evidence"]["bm25"] = ev
        entry["parts"].append({"signal": "bm25", "c": conf})
        return
    name_only = not in_title
    conf = spec.name_only_cap if name_only else spec.cap_alone
    merged[dc_id] = {"confidence": conf, "from": {"bm25"}, "evidence": {"bm25": ev},
                     "parts": [{"signal": "bm25", "c": conf}], "review": True,
                     "basis": "bm25_name_only" if name_only else "bm25"}


#------------------------------------------------------------------
# 노드별 병합 상태 → 최종 DoctypeSignal (scan_doctype·merge_embed_candidates 공유)
#=> "노드별 (점수, from, 근거)" 중간 상태에서 조상 흡수(5-2)와 축 전략(6장)을
#   적용해 최종 결과를 만드는 뒷단 절반이다. 규칙 스캔이든 임베딩 전파와의
#   병합이든 같은 마무리를 거치게 해, 두 곳의 로직이 어긋날 위험을 없앤다.
#
# -in: merged   = {dc_id: {"confidence","from","evidence","parts"}}
# -in: taxonomy = axes.Taxonomy
# -in: conflict = doc_rules.ConflictSpec
#
# -out: DoctypeSignal
# -out: error = 없음
#------------------------------------------------------------------
def _finalize(merged, taxonomy, conflict):
    survivors = _absorb_ancestors(merged.keys(), taxonomy)

    candidates = []
    for dc_id in survivors:
        m = merged[dc_id]
        srcs = set(m["from"])
        # stage: 이 후보가 규칙에서 왔는지, 2차(벡터 · BM25)에서 왔는지, 둘 다인지.
        # 검토 화면이 "근거를 제시할 수 있는 후보"를 구분하는 데 쓴다.
        second = srcs & {"embed", "bm25"}
        if srcs and not (srcs - second):
            # 2차만으로 붙은 후보 — 무엇이 냈는지까지 남긴다
            stage = {frozenset({"embed"}): "embed", frozenset({"bm25"}): "bm25"}.get(
                frozenset(second), "embed+bm25")
        elif second:
            stage = "both"
        else:
            stage = "rule"
        candidates.append({
            "dc_id": dc_id,
            "path": taxonomy.path(dc_id),
            "path_ids": taxonomy.path_ids(dc_id),
            "confidence": m["confidence"],
            "from": tuple(sorted(srcs)),
            "stage": stage,
            "evidence": m.get("evidence") or {},
            "score_parts": m.get("parts") or [],
        })
        if m.get("review"):
            # 약한 라벨 표시는 노드 단위로 따라다닌다 — 이 라벨을 받는 쪽(화면·
            # 고객 시스템)이 자동 확정하지 않게 하는 것이 유일한 목적이다.
            candidates[-1]["review"] = True
            candidates[-1]["basis"] = m.get("basis") or "name_head"

    res = resolve_doctype(candidates, conflict)
    return DoctypeSignal(values=res.values, strategy=conflict.strategy,
                         status="proposed", truncated=res.truncated,
                         conflicts=_check_hierarchy(survivors, taxonomy))


#------------------------------------------------------------------
# 임베딩 전파 후보를 규칙 후보와 병합 (D7, 설계서 5-4·재설계 11-1)
#=> 1차(규칙) 스캔이 이미 만든 최종 후보(values)에, doctype 축 전파가 찾아낸
#   후보를 더한 뒤 다시 조상 흡수·축 전략을 적용한다.
#    1) 기존 values 를 노드별 상태로 되돌린다(근거 블록도 함께 살린다)
#    2) embed 후보를 더한다 — 같은 노드면 점수는 더 높은 쪽, from 에 "embed" 추가
#    3) taxonomy 에 없는 dc_id(삭제됐거나 seed 가 낡음)는 조용히 무시한다
#    4) _finalize 로 조상 흡수 + 축 전략을 다시 적용
#   [원칙] embed 는 후보를 "추가"만 한다 — 이미 있던 규칙 기반 후보를 빼앗지
#   않는다(security 축이 embed 를 상향 전용으로만 쓰는 것과 같은 정신).
#   [상한] 벡터 '단독' 후보는 embed_cap 을 넘지 못한다(재설계 9-3a) — knn_vote
#   의 득표율은 "이웃 중 몇 %인가"이지 "이 문서가 그 라벨일 확률"이 아니라서,
#   근거를 제시할 수 있는 규칙 히트보다 세지면 안 된다.
#
# -in: values       = 병합 전 최종 후보 튜플(labels.doctype.values, 없으면 빈 튜플)
# -in: embed_signal = propagate.DoctypeEmbedSignal(propagate_doctype() 반환값)
# -in: taxonomy     = axes.Taxonomy
# -in: conflict     = doc_rules.ConflictSpec
# -in: embed_cap    = 벡터 단독 후보 신뢰도 상한(None 이면 상한 없음 — 종전 동작)
#
# -out: DoctypeSignal = embed 후보까지 반영된 최종 결과(둘 다 비었으면 values=())
# -out: error = 없음
#------------------------------------------------------------------
def merge_embed_candidates(values, embed_signal, taxonomy, conflict, embed_cap=None):
    return merge_second_stage(values, embed_signal, taxonomy, conflict, embed_cap=embed_cap)


#------------------------------------------------------------------
# 2차 후보(벡터 · BM25)를 1차 후보와 한 번에 병합
#=> 벡터 후보와 BM25 후보를 같은 중간 상태에 함께 얹은 뒤 한 번만 마무리(조상 흡수 ·
#   top_n)한다. 둘을 따로 마무리하면 top_n 이 먼저 잘라 BM25 가 벡터와 합의했는지
#   볼 후보가 사라질 수 있다.
#    1) 1차 후보를 노드별 상태로 되돌린다(review · basis 도 그대로 — Rust 와 같게)
#    2) 벡터 후보: 같은 노드면 점수는 큰 쪽 · from 에 "embed", 없으면 embed_cap 으로 추가
#    3) BM25 1위(bm25_pick):
#       · 1차 규칙이 이미 낸 노드 → 근거만 더한다(점수 그대로)
#       · 벡터만 낸 노드 → 합의: 확신을 cap_agree 까지 올린다
#       · 새 노드 → cap_alone(파일명에서만 맞았으면 name_only_cap)으로 추가, review 표시
#
# -in: values       = 병합 전 최종 후보(labels.doctype.values, 없으면 빈 튜플)
# -in: embed_signal = propagate.DoctypeEmbedSignal(벡터를 못 만들었으면 None)
# -in: taxonomy     = axes.Taxonomy
# -in: conflict     = doc_rules.ConflictSpec
# -in: embed_cap    = 벡터 단독 후보 신뢰도 상한(None 이면 상한 없음)
# -in: bm25_pick    = BM25 고른 결과 dict(dc_id · score · threshold · title · name · words) 또는 None
# -in: bm25_spec    = doc_rules.Bm25Spec(bm25_pick 이 있을 때 필요)
#
# -out: DoctypeSignal = 2차 후보까지 반영된 최종 결과
# -out: error = 없음
#------------------------------------------------------------------
def merge_second_stage(values, embed_signal, taxonomy, conflict, embed_cap=None,
                       bm25_pick=None, bm25_spec=None):
    merged = {}
    for v in values or ():
        # 레코드에는 근거가 signals 한 칸으로 합쳐져 있다(2026-09-10).
        # 엔진 속 계산은 두 칸으로 다루므로 여기서 되돌린다.
        evidence, parts = _split_signals(v)
        merged[v["dc_id"]] = {
            "confidence": v["confidence"],
            "from": set(v.get("from") or ()),
            "evidence": evidence,
            "parts": parts,
        }
        if v.get("review"):
            # 약한 라벨 표시는 병합을 지나도 따라가야 한다(Rust 판도 지킨다)
            merged[v["dc_id"]]["review"] = True
            merged[v["dc_id"]]["basis"] = v.get("basis") or "name_head"

    for ev in (embed_signal.values if embed_signal is not None else ()):
        dc_id = ev["dc_id"]
        if taxonomy.get(dc_id) is None:
            continue   # 삭제된 노드를 가리키는 낡은 seed — 조용히 무시
        ev_block = {
            "method": embed_signal.method,
            "top_sim": round(float(embed_signal.top_sim), 3),
            "share": round(float(ev["confidence"]), 3),
        }
        entry = merged.get(dc_id)
        if entry is None:
            # 벡터 단독 후보 — 상한을 건다.
            conf = float(ev["confidence"])
            if embed_cap is not None:
                conf = min(conf, embed_cap)
            merged[dc_id] = {"confidence": conf, "from": {"embed"},
                             "evidence": {"embed": ev_block},
                             "parts": [{"signal": "embed", "c": conf}]}
        else:
            entry["from"].add("embed")
            entry["evidence"]["embed"] = ev_block
            if ev["confidence"] > entry["confidence"]:
                entry["confidence"] = float(ev["confidence"])

    if bm25_pick and taxonomy.get(bm25_pick["dc_id"]) is not None:
        _add_bm25(merged, bm25_pick, bm25_spec)

    if not merged:
        return DoctypeSignal(values=(), strategy=conflict.strategy,
                             status="proposed", truncated=0)
    return _finalize(merged, taxonomy, conflict)
