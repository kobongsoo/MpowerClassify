# -*- coding: utf-8 -*-
#------------------------------------------------------------------
# 업무분류 규칙의 '단어 제안' — 순수 로직
#=> 사람이 확정한 문서에서 "이 분류에만 유독 자주 나오는 말"을 뽑아 관리자에게
#   후보로 내민다. 규칙 파일은 이 모듈이 고치지 않는다 — 고르는 것도, 넣는 것도
#   사람의 일이다(설계서 plan/업무분류-규칙단어-제안기능-설계-20260916.html).
#
#   [왜 필요한가] 규칙 단어는 지금 분류 체계 이름과 공용 유의어 사전에서만 온다.
#   둘 다 그 회사의 문서를 한 장도 안 본 말이라, '품목보고서'·'입문교육'·'도움말'
#   같은 회사 고유 표기는 원리적으로 못 만든다. 사람이 놓친 문서를 열어 보고
#   손으로 적어 넣던 일(2026-09-14 V2b)이 바로 그것이고, 이 모듈은 그 반복을 줄인다.
#
#   [빈도가 아니라 대조] "많이 나오는가"가 아니라 "여기에만 나오는가"를 묻는다.
#   빈도순으로 뽑으면 인증서류에서 '인증'이 1등으로 나오는데, 그 말은 2026-09-14 에
#   오탐 때문에 규칙과 core 사전에서 일부러 뺀 말이다. 같은 실수를 되풀이하지
#   않으려고 다른 분류에서의 출현율(df_neg)을 분모 쪽에 둔다.
#
#   [바깥 의존] 표준 라이브러리 · yaml · 같은 패키지의 docvocab(끝말 목록) 뿐이다.
#   화면(streamlit)도 엔진(분류기)도 모른다 — 화면·CLI 가 똑같이 부를 수 있어야 한다.
#------------------------------------------------------------------

import json
import math
import os
import re
import shutil

import yaml

from . import docvocab


# ── 재료 요건 ────────────────────────────────────────────────────────
# 이 수보다 확정 문서가 적으면 제안하지 않는다. 한 문서에서 뽑은 말은 규칙이
# 아니라 그 문서의 지문이다.
MIN_DOCS = 3
# 이 수를 넘어야 '근거가 단단하다'로 보고 기본 체크를 켠다.
SOLID_DOCS = 5

# ── 채택 문턱 ────────────────────────────────────────────────────────
# [2026-09-16 설계 수정] 설계서 6-2 는 "df_pos ≥ 0.60"(이 분류 문서 열 중 여섯에
# 나올 것)을 문턱으로 잡았는데, 구현해 보니 이 기능의 대표 사례가 그 문턱에
# 걸려 떨어졌다. '품목보고서'는 보고서 확정문서의 37.5% 에만 나온다 —
# 한 분류(보고서) 안에 품목보고서·실태조사·이슈페이퍼처럼 여러 갈래가 있어
# 어떤 말도 그 분류를 널리 덮지 못하기 때문이다. 사람이 V2b 로 넣은 말이
# 전부 그런 '갈래 말'이었다.
#
# 그래서 묻는 것을 바꿨다 — "이 분류를 얼마나 덮는가"(df_pos)가 아니라
# "다른 분류에 안 나오는가"(df_neg) + "근거가 몇 건인가"(문서·폴더 수)다.
# 20건 중 20건이 이 분류에만 나오는 말은, 그 분류의 5% 만 덮더라도 규칙으로 쓸 값이 있다.
# df_pos 는 문턱에서 빼고 화면에 보여 주는 값으로만 남긴다.

# 근거 하한 — 그 말이 나온 문서 수와 폴더 수. 폴더 둘을 요구하는 것이
# 한 폴더 어휘가 규칙이 되는 것을 막는 유일한 장치다(한 폴더짜리는 아래 CLUSTER_ONLY 로 간다).
MIN_TERM_DOCS = 3
MIN_TERM_CLUSTERS = 2

# df_neg: 다른 분류 문서에 얼마나 나오는가. '인증'·'계약' 같은 말이 여기서 걸린다.
# 이 값이 이제 유일한 '배타성' 문턱이다.
DF_NEG_MAX = 0.05

# ── 끝말 밖 묶음 ─────────────────────────────────────────────────────
# 본문에서 끝말 목록에 안 걸린 말은 '문서 종류를 가리키는 말'이라는 근거가 약하다.
# 버리지는 않되(‘도움말’·‘동향’처럼 실제로 필요했던 말이 여기 있다) 더 죈다.
# 자름의 근거가 없는 만큼 '그 분류를 절반은 덮을 것'을 요구한다.
LOOSE_DF_NEG_MAX = 0.02
LOOSE_DF_POS_MIN = 0.50

# ── 한 폴더 전용 묶음 ────────────────────────────────────────────────
# [왜 따로 두나] 폴더 보정(같은 폴더 문서는 합쳐 1건)을 걸면 한 폴더에 몰린
# 문서 무리의 어휘는 어떤 문턱도 못 넘는다. 실제로 매뉴얼 놓침 118건이 전부
# 한 폴더(decm 관리자 도움말 html)였고, 사람은 거기서 '도움말'을 찾아 규칙에 넣었다.
# 보정을 풀면 그 폴더 어휘가 상위권을 독차지하므로, 보정은 그대로 두고
# '한 폴더 전용'이라는 딱지를 붙여 따로 모은다. 기본 체크는 켜지 않는다.
CLUSTER_ONLY_MIN_DOCS = 5      # 그 폴더 안 문서가 이만큼은 돼야 한다
CLUSTER_ONLY_DF_POS = 0.90     # 그 폴더 안에서는 거의 모든 문서에 나와야 한다
CLUSTER_ONLY_DF_NEG = 0.02     # 다른 분류에는 사실상 없어야 한다
# 두 글자 말은 넣지 않는다. 폴더가 하나뿐이라 '다른 분류에 안 나온다'는 근거가
# 그 폴더의 우연일 수 있는데, '상황'·'업무'·'진행' 같은 두 글자 말은 실제 코퍼스가
# 커지면 어디에나 나온다. 근거가 약한 묶음인 만큼 말이라도 길 것을 요구한다.
CLUSTER_ONLY_MIN_LEN = 3

# 문서 한 건을 놓고 보는 화면(suggest_for_doc)에서 한 번에 보여 줄 후보 수.
# [왜 필요한가] 문턱을 못 넘은 말도 까닭을 달아 올리는 자리라, 상한이 없으면
# 문서 하나의 낱말이 통째로 올라온다 — 실측에서 사내규정 문서 한 건이 후보
# 126개를 냈다('NUMPAGES'·'때까지'·'지장을주어서'까지). 그 목록은 아무도 못 읽는다.
NEAR_MISS_MAX = 15
# 그리고 그 문서에만 있는 말은 올리지 않는다 — 한 문서의 낱말은 규칙이 아니라
# 그 문서의 지문이다(설계 4-1 과 같은 이유, 눈앞의 문서라 하한만 낮춘 것이다).
NEAR_MISS_MIN_DOCS = 2

# 문턱을 못 넘은 말을 '참고'로 보여 줄 최대 개수. 후보도 한 폴더 전용도 하나도
# 없을 때만 내보낸다 — 화면이 조용해야 할 자리에 긴 목록을 쏟지 않기 위해서다.
#
# [왜 보여 주나] 확정 문서가 있는데도 "제안할 말이 없습니다" 한 줄만 나오면,
# 관리자는 '뽑을 말이 없는 것'인지 '문턱에 걸린 것'인지 구분할 수 없다. 기계가
# 자를 수 없는 판단(그 말이 진짜 문서 종류를 가리키는가)은 사람이 해야 하므로,
# 무엇이 왜 떨어졌는지는 보여 준다. 기본 체크는 켜지 않는다.
BELOW_MAX = 15

# 사람이 "이 분류 아님"이라고 거절한 문서는 대조군에서 이만큼 무겁게 센다.
# 그냥 다른 분류인 문서보다, 사람이 콕 집어 아니라고 한 문서가 더 강한 반례다.
REJECT_WEIGHT = 2.0

# 본문에만 나오는 말을 규칙에 넣을 때 함께 달아 줄 값. 본문은 넓어서 인용·설명
# 속 한 번 언급으로 걸린다 — 보안등급 쪽에서 같은 문제를 min_count 로 막은 전례가 있다.
BODY_MIN_COUNT = 2

# 앞부분(head)으로 볼 글자 수. doc_rule.yaml 의 defaults.head_chars 와 같은 값이다.
HEAD_CHARS = 400

# 조사·어미. 어절 끝에서 떼어 낸다. 긴 것부터 시도해야 '으로'가 '로'보다 먼저 걸린다.
PARTICLES = tuple(sorted((
    "으로서", "으로써", "이라고", "에서는", "에게서", "으로", "이라", "라고", "에서",
    "에게", "한테", "부터", "까지", "처럼", "보다", "마다", "조차", "라는", "이나",
    "께서", "들의", "들을", "들이", "들은",
    "은", "는", "이", "가", "을", "를", "의", "에", "와", "과", "도", "만", "로", "랑",
    # 인용 조사 — '“동호회”라 한다' 같은 규정 문투에서 나온다. 뗀 뒤 두 글자가
    # 안 남으면 떼지 않으므로 '나라'·'우라' 같은 명사는 그대로 살아남는다.
    "라",
), key=lambda w: -len(w)))

# 파일 이름·제목을 자를 구분자.
# [한글 따옴표·괄호도 넣는다] 실데이터에서 '“동호회”라'·'“지원금”'·'「회원」' 이
# 한 낱말로 올라왔다(2026-09-16). 영문 따옴표만 담고 있던 탓이다.
SPLIT_RE = re.compile(
    r"[\s_\-–—.,/\\()\[\]{}<>~!@#$%^&*+=:;'\"?｜|·・"
    r"“”‘’「」『』〈〉《》【】（），：；？！]+")

# 버릴 토큰 — 숫자·날짜·판번호처럼 문서 종류와 무관한 것.
JUNK_RE = re.compile(r"^(v?\d+([.\-]\d+)*[가-힣a-z]*|\d{4}년?|\d+분기|\d+차|\d+회|\d+판)$",
                     re.IGNORECASE)

# 한글이 한 글자라도 들어 있는가(길이 기준을 한글/영문에 다르게 주려고 본다).
HANGUL_RE = re.compile(r"[가-힣]")

# 숫자가 섞인 말은 버린다 — 차수·연도·조항('제1조'·'2026년'·'3차')이다.
# [근거] 현행 doc_rule.yaml 의 규칙 단어 325개 중 숫자가 든 것은 0개다.
DIGIT_RE = re.compile(r"\d")

# '~다'로 끝나는 말도 버린다 — '정한다'·'포함한다' 같은 서술어다.
# [근거] 같은 325개 중 '다'로 끝나는 것도 0개다.

# 용언 활용형의 꼬리. 사전형('~다')만 막았더니 실코퍼스에서 '클릭하면'·'선택하고'·
# '가능하며'·'통하여'·'포함되어야' 가 후보 상위를 차지했다(2026-09-16 첫 실측).
# 이건 빈도 문제가 아니라 문법 문제다 — 어느 회사 문서에서든 문서 종류를 가리키는
# 말이 아니므로, 코퍼스를 보지 않고 모양만으로 버린다.
#
# [짧은 꼬리는 일부러 뺐다] '한'·'된' 하나만 보면 기한·제한·권한·시한 같은 멀쩡한
# 명사가 함께 죽는다. 두 글자 이상이라 용언에서만 나오는 꼬리만 담았다.
#
# [검산] 현행 규칙 단어·유의어 사전·분류 이름의 낱말에 대 보니 '된'·'는' 은 0개,
# '인' 은 8개(승인·확인·가이드라인…), '한' 은 2개(경고서한…)가 걸린다.
# 그래서 '된'·'는' 은 담고 '인'·'한' 은 뺐다 — '주관부서인' 하나를 잡으려고
# '승인'·'확인'을 죽일 수는 없다. 그런 말은 금지 목록(11장)이 맡는다.
#
# [이 목록은 사전으로 옮겼다] core 사전의 verb_endings: 칸이 기준이고, 업종·local 은
# 더하기만 한다(끝말 목록 suffixes 와 같은 규약, 2026-09-15 전례).
# 여기 값은 그 칸이 없는 옛 사전이 깔렸을 때 쓰는 기본값이다 — 필터가 조용히
# 꺼지는 것을 막는다.
VERB_ENDINGS = (
    "하면", "하고", "하며", "하여", "하는", "하지", "하기", "하도록", "하였", "해야", "해서",
    "되면", "되고", "되며", "되어", "되는", "되지", "되도록", "되어야", "돼야",
    "이며", "이고", "이는", "이면", "으면", "으며",
    "어야", "아야", "여야", "니다", "습니다", "드립니다",
    "있는", "없는", "같은", "대한", "관한", "위한", "통한", "통해", "따른", "따라",
    "면서", "지만",
    # 아래 둘은 위 검산에서 0건이라 안전하다. '신고된'·'정하는' 류가 여기서 걸린다.
    "된", "는",
)

# 앞부분·본문에서 뽑은 말의 최소 길이. 제목·파일 이름에는 걸지 않는다.
# [왜 자리마다 다른가] 현행 규칙에는 두 글자 말이 29개 있다(규정·지침·교육·동향…).
# 버리면 안 되는 말이다. 그런데 그 말들은 전부 '제목에 적히는 문서 종류'다 —
# 본문에 흩어진 '목적'·'절차'·'방법'과는 다르다. 실제로 V2b 때 '동향'을 앞부분
# 칸에도 넣었다가 오탐이 나서 제목 칸만 남긴 일이 있다. 그 판단을 규칙으로 굳힌다.
TEXT_MIN_LEN = 3

# 파일 확장자로 흔한 것 — 파일 이름을 자르고 남는 찌꺼기를 막는다.
EXT_WORDS = {"doc", "docx", "hwp", "hwpx", "ppt", "pptx", "xls", "xlsx", "pdf",
             "txt", "md", "html", "htm", "jpg", "png", "zip"}


#------------------------------------------------------------------
# 경로 정규화 (비교용)
#=> 같은 문서를 가리키는 경로가 구분자(\ vs /)·대소문자만 달라도 한 문서로 잇는다.
#   seedstore·평가셋 채점기와 같은 규칙이다.
#
# -in: path = 파일 경로
#
# -out: str = 비교용으로 정규화한 경로(빈 값이면 빈 문자열)
# -out: error = 없음
#------------------------------------------------------------------
def norm_key(path):
    if not path:
        return ""
    return os.path.normcase(os.path.normpath(str(path)))


#------------------------------------------------------------------
# 문서가 속한 '묶음'(폴더) 구하기
#=> 같은 폴더에 있는 문서는 대개 같은 데서 한꺼번에 들어온 것이라, 어휘가 통째로
#   같다. 그것을 여러 건으로 세면 그 폴더의 말버릇이 규칙이 된다.
#   (매뉴얼 놓침 118건이 전부 한 폴더였던 실제 사례가 이 보정의 근거다.)
#
# -in: path = 파일 경로
#
# -out: str = 부모 폴더 경로(정규화). 경로가 없으면 문서 자체를 한 묶음으로 본다
# -out: error = 없음
#------------------------------------------------------------------
def cluster_of(path):
    key = norm_key(path)
    parent = os.path.dirname(key)
    return parent or key


#------------------------------------------------------------------
# 기준 문서 저장소에서 확정 라벨 읽기 (class_seed.jsonl)
#=> 관리자가 "이 문서가 이 분류의 본보기"라고 확정한 것만 가져온다.
#
#   [사람이 승인한 것만 쓴다] seed 는 엔진 판정에서 자동으로 뽑히기도 한다
#   (t_seed 0.85). 그렇게 들어온 줄에서 말을 뽑아 규칙을 만들면, 규칙이 만든
#   라벨이 다시 규칙을 만드는 고리가 생겨 오라벨이 스스로 번식한다.
#   그래서 approved_by 가 채워진 줄만 재료로 삼는다.
#
# -in: path            = class_seed.jsonl 경로
# -in: approved_only   = True 면 approved_by 가 있는 줄만(기본 True)
#
# -out: dict = {정규화경로: {"labels": set(dc_id), "hash": str, "file": str}}
# -out: error = 파일이 없으면 빈 dict(오류가 아니다 — seed 가 없는 배포가 있다)
#------------------------------------------------------------------
def load_seed_labels(path, approved_only=True):
    out = {}
    if not path or not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                # 깨진 줄 하나 때문에 제안 전체가 멈추면 안 된다.
                continue
            labels = [d for d in (row.get("doctype") or []) if d]
            if not labels:
                continue
            if approved_only and not row.get("approved_by"):
                continue
            key = norm_key(row.get("file"))
            if not key:
                continue
            cur = out.setdefault(key, {"labels": set(), "hash": row.get("hash"),
                                       "file": row.get("file")})
            cur["labels"].update(labels)
    return out


#------------------------------------------------------------------
# 검토 화면의 확정·거절 이력 읽기 (cso_override.jsonl 의 doctype 축)
#=> 검토를 할수록 쌓이는 파일이라, 실제 재료는 대부분 여기서 나온다.
#    1) axis 가 "doctype" 인 줄만 본다(보안등급 결정은 건드리지 않는다)
#    2) 같은 문서에 여러 번 결정했으면 마지막 것이 진실이다(append-only 파일)
#    3) rejected("이 분류 아님")도 버리지 않고 가져온다 — 대조군을 강하게 만든다
#
# -in: path = cso_override.jsonl 경로
#
# -out: dict = {정규화경로: {"confirmed": set, "rejected": set, "file": str}}
# -out: error = 파일이 없으면 빈 dict
#------------------------------------------------------------------
def load_override_labels(path):
    latest = {}
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("axis") != "doctype":
                continue
            key = norm_key(row.get("file"))
            if not key:
                continue
            # 나중 줄이 앞 줄을 덮는다 — 마지막 결정만 남긴다.
            latest[key] = {"confirmed": set(d for d in (row.get("confirmed") or []) if d),
                           "rejected": set(d for d in (row.get("rejected") or []) if d),
                           # doc_id 는 본문 색인으로 hash 를 되찾는 두 번째 열쇠다
                           # (경로가 바뀐 문서는 경로로 못 찾는다).
                           "doc_id": row.get("doc_id"),
                           "file": row.get("file")}
    return latest


#------------------------------------------------------------------
# 금지 목록 읽기 (doc_rule_stopwords.yaml)
#=> "이 말은 오탐이라 뺐다"는 지식이 커밋 메시지와 사람 머릿속에만 있으면 같은 말이
#   계속 되살아난다('인증'·'고객'·'계약'·'휴가'를 규칙과 사전에서 두 번 빼야 했다).
#
# -in: path = doc_rule_stopwords.yaml 경로(없어도 된다)
#
# -out: dict = {"global": set, "by_node": {dc_id: set}}
# -out: error = 파일 없음·파싱 실패·yaml 미설치면 빈 목록(예외를 올리지 않는다)
#------------------------------------------------------------------
def load_stopwords(path):
    empty = {"global": set(), "by_node": {}}
    if not path or not os.path.isfile(path):
        return empty
    try:
        with open(path, encoding="utf-8") as fp:
            data = yaml.safe_load(fp) or {}
    except (OSError, yaml.YAMLError):
        # 금지 목록을 못 읽었다고 제안을 멈출 이유는 없다. 다만 걸러지지 않을 뿐이다.
        return empty
    if not isinstance(data, dict):
        return empty
    glob = {str(w).strip() for w in (data.get("global") or []) if str(w).strip()}
    by_node = {}
    for node, words in (data.get("by_node") or {}).items():
        if isinstance(words, list):
            got = {str(w).strip() for w in words if str(w).strip()}
            if got:
                by_node[str(node)] = got
    return {"global": glob, "by_node": by_node}


#------------------------------------------------------------------
# 저장해 둔 추출 본문 읽기 (--textsave 폴더)
#=> seed 행에는 벡터만 있고 본문이 없다. 그러나 --textsave 가 남긴 파일은 이름이
#   원본의 SHA-256 이라, 결과 레코드의 hash 로 바로 찾아진다.
#
#   [원본을 다시 열지 않는다] 원본은 지워지거나 바뀔 수 있어(seed 가 suspect 로
#   표시하는 그 상황), 재추출 설계면 "어제는 되던 제안이 오늘은 안 나온다"가
#   조용히 벌어진다. 저장된 본문이 없으면 없는 대로 알린다.
#
# -in: text_dir = 추출 본문 폴더. None 이면 아무것도 안 읽는다
# -in: sha      = 문서의 SHA-256(결과 레코드·seed 행의 hash 칸)
#
# -out: str = 본문. 폴더나 파일이 없으면 None
# -out: error = 읽기 실패 시 None(예외를 올리지 않는다)
#------------------------------------------------------------------
def read_saved_text(text_dir, sha):
    if not text_dir or not sha:
        return None
    path = os.path.join(text_dir, f"{sha}.txt")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as fp:
            return fp.read()
    except OSError:
        return None


#------------------------------------------------------------------
# 저장된 본문의 색인 읽기 (--textsave 폴더의 _index.jsonl)
#=> 본문 파일 이름은 SHA-256 인데, 검토 확정 이력(cso_override.jsonl)에는
#   hash 칸이 없다. 그 파일은 '누가 무엇을 확정했나'를 남기는 자리라 지문을 적지
#   않는다. 그래서 확정 이력만 있는 문서는 hash 를 모르고, 본문을 못 찾는다.
#
#   [색인이 그 구멍을 메운다] --textsave 는 본문을 쓸 때마다 색인 한 줄
#   {"hash","txt","file","doc_id",…} 을 함께 남긴다. 원본 경로로 hash 를 되찾을 수
#   있으므로, 확정 이력만 있는 문서도 본문을 붙일 수 있다.
#   (색인이 없으면 예전처럼 hash 를 아는 문서만 본문을 갖는다 — 오류가 아니다.)
#
# -in: text_dir = --textsave 폴더. None 이면 빈 색인
#
# -out: (by_path, by_doc_id) = {정규화경로: hash}, {doc_id: hash}
# -out: error = 파일 없음·깨진 줄은 건너뛴다(예외를 올리지 않는다)
#------------------------------------------------------------------
def load_text_index(text_dir):
    by_path, by_doc = {}, {}
    if not text_dir:
        return by_path, by_doc
    path = os.path.join(text_dir, "_index.jsonl")
    if not os.path.isfile(path):
        return by_path, by_doc
    with open(path, encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            sha = row.get("hash")
            if not sha:
                continue
            # 같은 문서를 여러 번 저장했으면 마지막 줄이 이긴다(내용이 바뀌었을 수 있다).
            key = norm_key(row.get("file"))
            if key:
                by_path[key] = sha
            if row.get("doc_id"):
                by_doc[str(row["doc_id"])] = sha
    return by_path, by_doc


#------------------------------------------------------------------
# 문서 한 건 만들기
#=> 채점·점수 계산이 보는 칸만 담은 평평한 dict 를 만든다. 파일을 읽는 일과
#   말을 세는 일을 갈라 두어야, 시험이 파일 없이 돌 수 있다.
#
# -in: file     = 원본 경로(파일 이름·폴더를 여기서 뽑는다)
# -in: labels   = 이 문서로 확정된 dc_id 집합
# -in: rejected = 이 문서에서 거절된 dc_id 집합
# -in: text     = 저장된 추출 본문(없으면 None — 제목·파일 이름만으로 참여한다)
# -in: head_chars = 앞부분으로 볼 글자 수(공백 묶음을 한 칸으로 센다, 기본 400)
#
# -out: dict = {key,file,name,folder,cluster,title,head,body,labels,rejected,has_text}
# -out: error = 없음
#------------------------------------------------------------------
def make_doc(file, labels=(), rejected=(), text=None, head_chars=HEAD_CHARS):
    name = os.path.basename(str(file or ""))
    body = text or ""
    # 제목은 본문의 첫 비어 있지 않은 줄로 본다 — 엔진의 제목 추출과 같은 규약이다.
    title = ""
    for line in body.splitlines():
        if line.strip():
            title = line.strip()
            break
    # 앞부분은 엔진의 표제부와 같은 규칙(공백 묶음을 한 칸으로 본 앞 n 자)으로 자른다 —
    # 다르면 제안한 낱말이 실제 판정의 표제부에서는 안 걸린다.
    from .doctype import head_scope
    return {"key": norm_key(file), "file": file, "name": name,
            "folder": os.path.dirname(str(file or "")),
            "cluster": cluster_of(file),
            "title": title, "head": head_scope(body, head_chars), "body": body,
            "labels": set(labels), "rejected": set(rejected),
            "has_text": bool(text)}


#------------------------------------------------------------------
# 재료 모으기 — seed 와 검토 확정을 한 자리에 합친다
#=> 두 저장소를 문서 단위로 잇고, 저장된 본문을 붙여 문서 목록을 만든다.
#    1) seed 의 확정 라벨(사람 승인분)과 검토 화면의 confirmed 를 합친다
#    2) 검토 화면의 rejected 는 따로 들고 있는다(대조군 강화용)
#    3) hash 를 아는 문서는 저장된 본문을 읽어 붙인다
#
# -in: seed_path     = class_seed.jsonl 경로
# -in: override_path = cso_override.jsonl 경로
# -in: text_dir      = --textsave 폴더(없으면 제목·파일 이름만 쓴다)
# -in: approved_only = seed 를 사람 승인분으로 제한할지(기본 True)
#
# -out: list = make_doc 결과들(라벨도 거절도 없는 문서는 넣지 않는다)
# -out: error = 없음(파일이 없으면 그만큼 적게 모인다)
#------------------------------------------------------------------
def load_documents(seed_path=None, override_path=None, text_dir=None, approved_only=True):
    seeds = load_seed_labels(seed_path, approved_only=approved_only)
    overrides = load_override_labels(override_path)

    merged = {}
    for key, row in seeds.items():
        merged[key] = {"file": row.get("file"), "hash": row.get("hash"),
                       "labels": set(row["labels"]), "rejected": set()}
    for key, row in overrides.items():
        cur = merged.setdefault(key, {"file": row.get("file"), "hash": None,
                                      "doc_id": row.get("doc_id"),
                                      "labels": set(), "rejected": set()})
        cur["labels"].update(row["confirmed"])
        cur["rejected"].update(row["rejected"])
        # 같은 문서를 확정했다가 거절한 축은 확정에서 뺀다 — 마지막 결정이 진실이다.
        cur["labels"] -= row["rejected"]

    # 검토 확정 이력에는 hash 가 없다. 저장된 본문의 색인으로 되찾는다
    # (색인이 없으면 hash 를 아는 문서만 본문을 갖는다 — 오류가 아니다).
    by_path, by_doc = load_text_index(text_dir)

    docs = []
    for key, row in merged.items():
        if not row["labels"] and not row["rejected"]:
            continue
        sha = row.get("hash") or by_path.get(key) or by_doc.get(str(row.get("doc_id")))
        text = read_saved_text(text_dir, sha)
        docs.append(make_doc(row["file"], row["labels"], row["rejected"], text))
    return docs


#------------------------------------------------------------------
# 끝말로 끝나는가 (빠른 판정)
#=> "이 말이 끝말 목록의 어느 것으로 끝나는가"는 이 모듈에서 가장 자주 묻는 것이다.
#   목록을 처음부터 훑으면 말 하나마다 43번을 본다. 끝말은 길이가 두세 가지뿐이라,
#   그 길이만큼 잘라 집합에서 찾으면 두세 번이면 끝난다.
#   (실데이터 문서는 본문이 평균 12만 자라 이 차이가 분 단위로 벌어진다.)
#
# -in: word     = 검사할 말
# -in: suffixes = 끝말 목록(없으면 docvocab 내장 목록)
#
# -out: bool = 끝말로 끝나면 True
# -out: error = 없음
#------------------------------------------------------------------
def ends_with_suffix(word, suffixes=None):
    table, lens = _suffix_index(suffixes or docvocab.DOC_SUFFIXES)
    for n in lens:
        if len(word) > n and word[-n:] in table:
            return True
        if len(word) == n and word in table:
            return True
    return False


#------------------------------------------------------------------
# 끝말 목록을 '집합 + 길이들'로 바꿔 기억해 둔다
#=> 같은 목록으로 수만 번 묻게 되므로 한 번만 만들어 두고 다시 쓴다.
#
# -in: suffixes = 끝말 목록
#
# -out: (set, list) = 끝말 집합, 나오는 길이들(긴 것부터)
# -out: error = 없음
#------------------------------------------------------------------
_SUFFIX_INDEX = {}


def _suffix_index(suffixes):
    key = tuple(suffixes)
    got = _SUFFIX_INDEX.get(key)
    if got is None:
        table = set(key)
        got = (table, sorted({len(w) for w in table}, reverse=True))
        _SUFFIX_INDEX[key] = got
    return got


#------------------------------------------------------------------
# 어절에서 조사·어미 떼기
#=> '품목보고서를' → '품목보고서'. 형태소 분석기를 넣지 않고 끝말 목록으로
#   해결하려는 설계라, 조사만 떼어 내면 대개 충분하다.
#    1) 끝말 목록에 걸리는 형태가 나오면 거기서 멈춘다(가장 믿을 만한 자름)
#    2) 아니면 가장 긴 조사 하나를 떼되, 남는 말이 두 글자는 돼야 한다
#
# -in: word     = 어절(구두점은 미리 제거된 상태)
# -in: suffixes = 끝말 목록(없으면 docvocab 내장 목록)
#
# -out: str = 조사를 뗀 말(뗄 것이 없으면 그대로)
# -out: error = 없음
#------------------------------------------------------------------
def strip_particle(word, suffixes=None):
    word = (word or "").strip()
    if not word:
        return ""
    suffixes = suffixes or docvocab.DOC_SUFFIXES
    # 이미 끝말로 끝나면 건드리지 않는다 — '계약서'의 '서'를 조사로 오해하지 않게.
    if ends_with_suffix(word, suffixes):
        return word
    for par in PARTICLES:
        if word.endswith(par) and len(word) - len(par) >= 2:
            # 뗀 뒤에 끝말이 드러나든 아니든 뗀 말을 쓴다(판정은 부르는 쪽 몫).
            return word[: -len(par)]
    return word


#------------------------------------------------------------------
# 말 한 개가 후보가 될 자격이 있는가
#=> 숫자·날짜·판번호·확장자·너무 짧은 말을 걸러낸다. 여기서 막지 않으면
#   후보 목록이 '2026'·'v1.2'·'pdf' 로 뒤덮여 아무도 안 본다.
#
# -in: term    = 후보 말
# -in: endings = 활용형 꼬리 목록(사전의 verb_endings). None 이면 내장 목록
#
# -out: bool = 쓸 만하면 True
# -out: error = 없음
#------------------------------------------------------------------
def is_usable(term, endings=None):
    term = (term or "").strip()
    if not term or JUNK_RE.match(term):
        return False
    if term.lower() in EXT_WORDS:
        return False
    if DIGIT_RE.search(term):
        # 차수·연도·조항이다. 규칙 단어에 숫자가 드는 일은 없다.
        return False
    if HANGUL_RE.search(term):
        if term.endswith("다"):
            # 서술어('정한다'·'포함한다'). 문서 종류를 가리키는 말이 아니다.
            return False
        if term.endswith(tuple(endings) if endings else VERB_ENDINGS):
            # 활용형('클릭하면'·'선택하고'·'통하여'). 위와 같은 이유다.
            return False
        # 한글은 한 글자면 뜻이 너무 넓다('서'·'안'·'표').
        return len(term) >= 2
    # 영문·숫자 섞인 말은 세 글자부터. 두 글자 약어는 오탐이 잦다.
    return len(term) >= 3 and any(ch.isalpha() for ch in term)


#------------------------------------------------------------------
# 구분자로 잘라 말 뽑기 (파일 이름·제목용)
#=> 파일 이름과 제목은 사람이 뜻을 담아 적은 자리라, 구분자로만 잘라도 쓸 만한
#   말이 나온다. 끝말 목록을 요구하지 않는 이유가 그것이다 —
#   '이슈페이퍼'·'현황분석'처럼 끝말에 없는 말이 바로 여기서 나온다.
#
# -in: text     = 파일 이름 또는 제목
# -in: suffixes = 끝말 목록(조사 떼기에 쓴다)
# -in: drop_ext = True 면 맨 끝 확장자를 먼저 떼어 낸다(파일 이름용)
#
# -out: set = 뽑힌 말들
# -out: error = 없음
#------------------------------------------------------------------
def split_words(text, suffixes=None, drop_ext=False, endings=None):
    text = (text or "").strip()
    if not text:
        return set()
    if drop_ext:
        root, ext = os.path.splitext(text)
        if ext and ext[1:].lower() in EXT_WORDS:
            text = root
    out = set()
    for tok in SPLIT_RE.split(text):
        tok = strip_particle(tok.strip(), suffixes)
        if is_usable(tok, endings):
            out.add(tok)
    return out


#------------------------------------------------------------------
# 줄글에서 말 뽑기 (앞부분·본문용)
#=> 본문은 넓어서 아무 말이나 들어온다. 그래서 두 묶음으로 갈라 돌려준다.
#    · 끝말 걸림 : 끝말 목록('보고서'·'매뉴얼'·'규정' …)으로 끝나는 말.
#                  한국어 문서 이름은 거의 전부 "무엇+문서종류" 꼴이라
#                  이 자름만으로 분류에 쓸모 있는 말이 골라진다
#    · 끝말 밖   : 나머지. 버리지는 않되 더 높은 문턱을 건다(‘도움말’·‘동향’)
#
# -in: text     = 줄글
# -in: suffixes = 끝말 목록(없으면 docvocab 내장 목록)
#
# -out: (in_suffix, out_suffix) = 두 집합
# -out: error = 없음
#------------------------------------------------------------------
def scan_words(text, suffixes=None, endings=None):
    suffixes = suffixes or docvocab.DOC_SUFFIXES
    hit, rest = set(), set()
    # [먼저 중복을 없앤다] 조사 떼기는 어절 하나마다 끝말 43개·조사 30개를 훑는
    # 무거운 일이다. 실데이터 문서는 본문이 평균 12만 자(어절 2만 개)인데 서로
    # 다른 말은 그 1/5 도 안 된다. 떼기 전에 set 으로 줄이면 그만큼 덜 훑는다.
    # 결과는 어차피 집합이라 값이 달라지지 않는다.
    for tok in {t.strip() for t in SPLIT_RE.split(text or "")}:
        tok = strip_particle(tok, suffixes)
        if not is_usable(tok, endings):
            continue
        if ends_with_suffix(tok, suffixes):
            hit.add(tok)
        else:
            rest.add(tok)
    return hit, rest


#------------------------------------------------------------------
# 문서 한 건에서 자리별로 말 뽑기
#=> 같은 말이라도 어디서 나왔느냐에 따라 규칙의 어느 칸에 넣을지가 달라진다
#   (설계 8장). 그래서 자리를 섞지 않고 따로 담는다.
#
# -in: doc      = make_doc 결과
# -in: suffixes = 끝말 목록
#
# -out: dict = {"name","title","head","body"} 자리별 말의 집합 +
#              "loose" 끝말에 안 걸린 말(자리와는 별개의 표식이다)
#
#   [자리와 끝말은 다른 이야기다] '분기실적'은 본문에서 나왔고(자리=본문) 끝말
#   목록에는 없다(loose). 둘을 한 칸에 섞으면 "본문 전용이니 min_count 를 달자"는
#   판단과 "끝말 근거가 없으니 문턱을 죄자"는 판단을 함께 할 수 없다.
#
# -out: error = 없음
#------------------------------------------------------------------
def doc_words(doc, suffixes=None, endings=None):
    suffixes = suffixes or docvocab.DOC_SUFFIXES
    name = split_words(doc.get("name"), suffixes, drop_ext=True, endings=endings)
    title = split_words(doc.get("title"), suffixes, endings=endings)
    head_hit, head_rest = scan_words(doc.get("head"), suffixes, endings=endings)
    body_hit, body_rest = scan_words(doc.get("body"), suffixes, endings=endings)
    # 제목은 본문 첫 줄이라 앞부분에도 들어 있다. 자리를 정할 때 제목이 이기도록
    # 앞부분·본문에서 제목의 말은 빼 둔다 — 그래야 '제목에서 나온 말'로 분류된다.
    head = ((head_hit | head_rest) - title) - name
    body = ((body_hit | body_rest) - title - head) - name
    # 끝말 밖 표식은 앞부분·본문에서 뽑은 말에만 단다. 제목·파일 이름은 사람이
    # 뜻을 담아 적은 자리라 구분자로 자른 것만으로 근거가 된다.
    loose = ((head_rest | body_rest) - title) - name
    return {"name": name, "title": title, "head": head, "body": body, "loose": loose}


#------------------------------------------------------------------
# 묶음(폴더) 가중치 만들기
#=> 같은 폴더 문서들이 합쳐서 1건이 되도록, 문서마다 1/(그 폴더의 문서 수) 를 준다.
#   한 폴더에 35건이 몰려 있어도 출현율 계산에서는 1건으로 세어진다.
#
# -in: docs = 문서 목록
#
# -out: (weights, clusters) = weights: {문서 key: 가중치}, clusters: 폴더 수
# -out: error = 없음
#------------------------------------------------------------------
def cluster_weights(docs):
    size = {}
    for d in docs:
        size[d["cluster"]] = size.get(d["cluster"], 0) + 1
    weights = {d["key"]: 1.0 / size[d["cluster"]] for d in docs}
    return weights, len(size)


#------------------------------------------------------------------
# 규칙 파일에서 이미 쓰고 있는 말 모으기
#=> 중복 제안을 막고, 다른 분류 규칙과 겹치는 말을 가려내기 위해 본다.
#   대소문자만 다른 말도 같은 말로 본다 — DC_007 규칙에 Mpower/mpower 가 함께
#   들어가 본문 건수가 두 배로 세어지던 실제 사고가 있었다.
#
# -in: doc = doc_rule.yaml 을 읽은 dict(없으면 빈 결과)
#
# -out: (by_node, owner) = by_node: {dc_id: {소문자 말}},
#                          owner: {소문자 말: dc_id}(먼저 나온 분류가 임자)
# -out: error = 없음
#------------------------------------------------------------------
def rule_terms(doc):
    by_node, owner = {}, {}
    for rule in ((doc or {}).get("doctype_rules") or []):
        node = rule.get("node")
        if not node:
            continue
        got = by_node.setdefault(node, set())
        for cell in ("title_terms", "head_terms", "terms", "filename"):
            for word in (rule.get(cell) or []):
                low = str(word).strip().lower()
                if low:
                    got.add(low)
                    owner.setdefault(low, node)
    return by_node, owner


#------------------------------------------------------------------
# 분류 체계에서 '다른 분류 이름' 모으기
#=> 계약서 문서에서 '회의록'이 뽑히면 두 규칙이 서로를 먹는다. 분류 이름은
#   이미 규칙 생성이 다루는 말이기도 해서, 제안 후보에서는 뺀다.
#
# -in: tax  = {"by_id": {dc_id: {"title": …}}} 모양의 분류 체계(또는 None)
# -in: node = 지금 제안하는 분류(이 분류의 이름은 빼지 않는다)
#
# -out: set = 소문자로 맞춘 다른 분류 이름들
# -out: error = 없음
#------------------------------------------------------------------
def other_titles(tax, node):
    out = set()
    for dc_id, item in ((tax or {}).get("by_id") or {}).items():
        if dc_id == node:
            continue
        title = str((item or {}).get("title") or "").strip()
        if title:
            out.add(title.lower())
    return out


#------------------------------------------------------------------
# 고유명사 의심 판정
#=> 회사명·제품명·인명은 대개 그 폴더에서만 통하는 말이라 과적합이 된다.
#   다만 DC_007(엠파워제품)처럼 제품명이 곧 정답인 분류도 있어, 기계가 정할 수
#   없다. 그래서 막지 않고 표식만 단다 — 관리자가 한 번 더 생각하게 하는 것이 목적이다.
#
# -in: term     = 후보 말
# -in: docs     = 그 분류의 확정 문서들(폴더 이름을 본다)
# -in: suffixes = 끝말 목록
#
# -out: bool = 의심되면 True
# -out: error = 없음
#------------------------------------------------------------------
def looks_proper_noun(term, docs, suffixes=None):
    suffixes = suffixes or docvocab.DOC_SUFFIXES
    # 끝말로 끝나는 말은 문서 종류를 가리키는 말이다 — 고유명사로 보지 않는다.
    if ends_with_suffix(term, suffixes):
        return False
    # 영문 대문자만으로 된 말(EZis·SCPI)은 거의 제품·규격 이름이다.
    if not HANGUL_RE.search(term) and term.isupper():
        return True
    # 폴더 이름에도 그대로 나오면 그 폴더 전용 어휘일 확률이 높다.
    low = term.lower()
    return any(low in str(d.get("folder") or "").lower() for d in docs)


#------------------------------------------------------------------
# 말 하나의 자리 → 규칙의 어느 칸에 넣을지
#=> 점수는 "이 말이 이 분류에 편중되는가"만 말해 줄 뿐, 어느 칸이 안전한지는
#   말해 주지 않는다. 그래서 '발견된 자리'로 칸을 정한다(설계 8장).
#     제목·파일 이름 → title_terms + filename   (기본 체크 켬)
#     앞부분에서만   → head_terms               (기본 체크 끔)
#     본문에서만     → terms + min_count 2      (기본 체크 끔)
#
#   [왜 이렇게 기울이나] 제목·파일 이름은 문서를 대표하는 자리라 우연히 들어오지
#   않는다. 앞부분(400자)은 표지·머리말이 섞이는 자리이고(V2 오탐이 여기서 났다),
#   본문은 인용·설명 속 한 번 언급으로 걸린다.
#
# -in: where = "name"|"title"|"head"|"body"|"loose" 중 그 말이 처음 나온 자리
#
# -out: (fields, extra, checked) = 넣을 칸 목록, 함께 달 값(min_count 등), 기본 체크 여부
# -out: error = 없음
#------------------------------------------------------------------
def placement(where):
    if where in ("title", "name"):
        return ["title_terms", "filename"], {}, True
    if where == "head":
        return ["head_terms"], {}, False
    return ["terms"], {"min_count": BODY_MIN_COUNT}, False


#------------------------------------------------------------------
# 대조 점수 (로그 오즈)
#=> "이 분류에 많이 나오는가"가 아니라 "이 분류에만 나오는가"를 숫자로 만든다.
#   분모에 다른 분류의 출현율이 들어가므로, 어느 분류에나 나오는 말('인증'·'계약')은
#   아무리 자주 나와도 점수가 오르지 않는다.
#   0으로 나누는 것을 막으려고 양쪽에 작은 값을 더한다(스무딩).
#
# -in: df_pos = 이 분류 문서에서의 출현율(0~1)
# -in: df_neg = 다른 분류 문서에서의 출현율(0~1)
# -in: n_pos  = 이 분류의 묶음 수(스무딩 크기를 정한다)
# -in: n_neg  = 대조군의 묶음 수
#
#   [문턱이 아니라 순위다] 이 값으로 자르지 않는다. 스무딩 항이 코퍼스 크기에
#   따라 달라져(작은 코퍼스에서 점수가 낮게 나온다) 문턱으로 쓰면 재료가 늘 때마다
#   기준이 흔들린다. 자르는 것은 df_neg 와 근거 건수가 하고, 이 값은 후보를
#   줄 세우는 데만 쓴다.
#
# -out: float = 로그 오즈. 클수록 이 분류 쪽으로 치우친 말
# -out: error = 없음
#------------------------------------------------------------------
def log_odds(df_pos, df_neg, n_pos, n_neg):
    a = df_pos + 0.5 / max(n_pos, 1.0)
    b = df_neg + 0.5 / max(n_neg, 1.0)
    return math.log(a / b)


#------------------------------------------------------------------
# 분류 하나에 대한 단어 제안
#=> 이 모듈의 알맹이. 확정 문서에서 말을 뽑고, 대조 점수로 거른 뒤, 넣을 칸까지
#   정해 후보 목록을 만든다. 규칙 파일은 건드리지 않는다.
#    1) 재료를 가른다 — 이 분류로 확정된 문서(D+) / 나머지와 거절분(D−)
#    2) 문서 수가 모자라면 왜 비었는지 말하고 끝낸다
#    3) 자리별로 말을 뽑고, 폴더 가중치로 출현율을 센다
#    4) 문턱을 넘은 말만 남기고, 넣으면 안 되는 말을 거른다
#    5) 한 폴더에 몰린 어휘는 '한 폴더 전용' 묶음으로 따로 담는다
#
# -in: node      = 제안할 분류의 dc_id
# -in: docs      = load_documents 결과(또는 make_doc 으로 만든 목록)
# -in: rule_doc  = doc_rule.yaml 을 읽은 dict(이미 있는 말을 빼려고 본다)
# -in: stopwords = load_stopwords 결과
# -in: tax       = 분류 체계(다른 분류 이름을 빼려고 본다)
# -in: suffixes  = 끝말 목록(없으면 docvocab 내장 목록)
# -in: min_docs  = 제안을 시작할 최소 확정 문서 수(기본 3)
#
# -out: dict = {"node","docs","clusters","no_text","reason",
#               "candidates":[…], "cluster_only":[…], "below":[…]}
#        below: 문턱을 못 넘은 말(참고용). 후보도 한 폴더 전용도 비었을 때만 채운다
#        후보 한 개: {"term","fields","extra","checked","where","group",
#                     "df_pos","df_neg","score","docs","clusters","flags"}
# -out: error = 없음(재료가 모자라면 candidates 가 비고 reason 에 까닭이 담긴다)
#------------------------------------------------------------------
def suggest(node, docs, rule_doc=None, stopwords=None, tax=None, suffixes=None,
            min_docs=MIN_DOCS, endings=None):
    suffixes = suffixes or docvocab.DOC_SUFFIXES
    stopwords = stopwords or {"global": set(), "by_node": {}}

    pos = [d for d in docs if node in d["labels"]]
    neg = [d for d in docs if node not in d["labels"]]
    # 사람이 "이 분류 아님"이라고 콕 집은 문서는 그냥 다른 분류인 문서보다 강한 반례다.
    rejected = [d for d in docs if node in d.get("rejected", set())]

    pos_w, pos_clusters = cluster_weights(pos)
    neg_w, _ = cluster_weights(neg)
    for d in rejected:
        neg_w[d["key"]] = neg_w.get(d["key"], 1.0) * REJECT_WEIGHT

    no_text = sum(1 for d in pos if not d["has_text"])
    out = {"node": node, "docs": len(pos), "clusters": pos_clusters,
           "no_text": no_text, "reason": "", "candidates": [], "cluster_only": [],
           "below": []}

    if len(pos) < min_docs:
        out["reason"] = (f"확정 문서 {len(pos)}건 — {min_docs}건부터 제안합니다")
        return out

    n_pos = sum(pos_w.values()) or 1.0
    n_neg = sum(neg_w.values()) or 1.0

    # ── 자리별로 말을 세어 모은다 ────────────────────────────────────
    # first_where: 그 말이 '가장 센 자리'에서 나왔는지 기억한다(제목 > 앞부분 > 본문).
    order = ("title", "name", "head", "body")
    pos_hits, first_where, doc_hits, cluster_hits = {}, {}, {}, {}
    # 끝말에 안 걸린 채로만 나온 말인가 — 한 문서에서라도 끝말로 걸렸으면 푼다.
    loose_only = {}
    for d in pos:
        words = doc_words(d, suffixes, endings)
        seen = set()
        for where in order:
            for term in words[where]:
                if term in seen:
                    continue
                seen.add(term)
                pos_hits[term] = pos_hits.get(term, 0.0) + pos_w[d["key"]]
                doc_hits[term] = doc_hits.get(term, 0) + 1
                cluster_hits.setdefault(term, set()).add(d["cluster"])
                # 이미 더 센 자리에서 봤으면 덮지 않는다.
                if term not in first_where:
                    first_where[term] = where
                is_loose = term in words["loose"]
                loose_only[term] = loose_only.get(term, True) and is_loose

    neg_hits = {}
    for d in neg:
        words = doc_words(d, suffixes, endings)
        for term in set().union(*words.values()) if words else set():
            if term in pos_hits:
                neg_hits[term] = neg_hits.get(term, 0.0) + neg_w[d["key"]]

    # ── 거르기 ──────────────────────────────────────────────────────
    by_node, owner = rule_terms(rule_doc)
    mine = by_node.get(node, set())
    banned = set(stopwords.get("global") or set())
    banned |= set((stopwords.get("by_node") or {}).get(node) or set())
    banned_low = {w.lower() for w in banned}
    forbidden_titles = other_titles(tax, node)

    for term, hit in pos_hits.items():
        low = term.lower()
        if low in mine:                       # ① 이 규칙에 이미 있는 말
            continue
        if low in banned_low:                 # ③ 금지 목록
            continue
        if low in forbidden_titles:           # ⑤ 다른 분류의 이름
            continue

        df_pos = hit / n_pos
        df_neg = neg_hits.get(term, 0.0) / n_neg
        score = log_odds(df_pos, df_neg, n_pos, n_neg)
        where = first_where[term]
        clusters = len(cluster_hits[term])
        # 제목·파일 이름에서 나온 말은 끝말 근거를 따지지 않는다(설계 5장 보강).
        loose = bool(loose_only.get(term)) and where in ("head", "body")

        flags = []
        conflict = owner.get(low)
        if conflict and conflict != node:     # ② 다른 분류 규칙에 있는 말
            flags.append(f"충돌: {conflict} 규칙에 있음")
        if looks_proper_noun(term, pos, suffixes):   # ⑥ 고유명사 의심(막지 않는다)
            flags.append("고유명사 의심")

        # 떨어진 말도 까닭을 달아 모아 둔다(참고 목록). 자르는 조건은 그대로다.
        def _below(why):
            out["below"].append({
                "term": term, "where": where, "why": why,
                "df_pos": round(df_pos, 4), "df_neg": round(df_neg, 4),
                "score": round(score, 3), "docs": doc_hits[term],
                "clusters": clusters, "flags": list(flags)})

        # 앞부분·본문에서 나온 두 글자 말은 넣지 않는다(위 TEXT_MIN_LEN 설명).
        if where in ("head", "body") and len(term) < TEXT_MIN_LEN:
            _below("두 글자 — 앞부분·본문에서는 뺍니다")
            continue

        # 근거(문서·폴더 수)와 배타성(df_neg)이 문턱이다. 점수는 줄 세우기에만 쓴다.
        enough = doc_hits[term] >= MIN_TERM_DOCS and clusters >= MIN_TERM_CLUSTERS
        if loose:
            passed = (enough and df_neg <= LOOSE_DF_NEG_MAX
                      and df_pos >= LOOSE_DF_POS_MIN)
        else:
            passed = enough and df_neg <= DF_NEG_MAX

        # 한 폴더에 몰린 어휘 — 보정 때문에 문턱을 못 넘지만 버리기 아까운 자리.
        cluster_only = (not passed and clusters == 1
                        and len(term) >= CLUSTER_ONLY_MIN_LEN
                        and doc_hits[term] >= CLUSTER_ONLY_MIN_DOCS
                        and df_neg <= CLUSTER_ONLY_DF_NEG
                        and _in_cluster_share(term, pos, cluster_hits[term],
                                             suffixes, endings)
                        >= CLUSTER_ONLY_DF_POS)
        if not passed and not cluster_only:
            # 왜 떨어졌는지 한 가지만 고른다 — 가장 먼저 걸린 까닭이 사람에게 쓸모 있다.
            if clusters < MIN_TERM_CLUSTERS:
                why = f"폴더 {clusters}곳 — {MIN_TERM_CLUSTERS}곳 이상이어야 합니다"
            elif doc_hits[term] < MIN_TERM_DOCS:
                why = f"근거 {doc_hits[term]}건 — {MIN_TERM_DOCS}건 이상이어야 합니다"
            elif loose and df_pos < LOOSE_DF_POS_MIN:
                why = (f"끝말 밖 — 이 분류를 {df_pos * 100:.0f}% 만 덮습니다"
                       f"({LOOSE_DF_POS_MIN * 100:.0f}% 이상 필요)")
            else:
                cap = LOOSE_DF_NEG_MAX if loose else DF_NEG_MAX
                why = (f"다른 분류에 {df_neg * 100:.1f}% 나옵니다"
                       f"({cap * 100:.0f}% 이하여야 합니다)")
            _below(why)
            continue

        # 기본 체크는 아주 보수적으로 켠다 — 제목·파일 이름에서 나왔고, 폴더가 둘 이상이고,
        # 근거가 단단하고, 끝말 밖도 아니고, 충돌·고유명사 표식도 없을 때만.
        fields, extra, checked = placement(where)
        checked = bool(checked and passed and not loose and clusters >= 2
                       and len(pos) >= SOLID_DOCS and not flags)
        if cluster_only:
            flags.append("한 폴더 전용")

        out["cluster_only" if cluster_only else "candidates"].append({
            "term": term, "fields": fields, "extra": extra, "checked": checked,
            "where": where, "group": "loose" if loose else "strong",
            "df_pos": round(df_pos, 4), "df_neg": round(df_neg, 4),
            "score": round(score, 3), "docs": doc_hits[term],
            "clusters": clusters, "flags": flags})

    # 점수가 높고 근거가 많은 말이 위로 오게 한다.
    sort_key = lambda c: (-c["score"], -c["docs"], c["term"])
    out["candidates"].sort(key=sort_key)
    out["cluster_only"].sort(key=sort_key)
    # 참고 목록은 '보여 줄 것이 하나도 없을 때'만 남긴다. 그 외에는 통째로 버린다 —
    # 후보가 있는데 그 아래 긴 목록이 따라붙으면 정작 후보가 묻힌다.
    if out["candidates"] or out["cluster_only"]:
        out["below"] = []
    else:
        out["below"].sort(key=sort_key)
        del out["below"][BELOW_MAX:]
    if len(pos) < SOLID_DOCS:
        out["reason"] = f"확정 문서 {len(pos)}건 — 근거가 얕습니다({SOLID_DOCS}건 이상 권장)"
    return out


#------------------------------------------------------------------
# 한 폴더 안에서의 출현율
#=> '한 폴더 전용' 묶음에 넣을지 판단할 때만 쓴다. 그 폴더 문서 거의 전부에
#   나와야 "그 폴더의 문서 종류를 가리키는 말"로 볼 수 있다.
#
# -in: term     = 후보 말
# -in: pos      = 이 분류의 확정 문서들
# -in: clusters = 그 말이 나온 폴더 집합(한 개일 때만 부른다)
# -in: suffixes = 끝말 목록
#
# -out: float = 그 폴더 문서 중 이 말이 나온 비율(0~1)
# -out: error = 없음
#------------------------------------------------------------------
def _in_cluster_share(term, pos, clusters, suffixes, endings=None):
    same = [d for d in pos if d["cluster"] in clusters]
    if not same:
        return 0.0
    hit = 0
    for d in same:
        words = doc_words(d, suffixes, endings)
        if any(term in words[where] for where in words):
            hit += 1
    return hit / len(same)


#------------------------------------------------------------------
# 문서 한 건을 놓고 후보 고르기 (검토 화면용)
#=> 관리자가 '규칙이 못 잡은 문서'를 열어 분류를 확정한 그 순간에 쓴다.
#   분류 전체의 후보를 다 보여 주면 지금 보고 있는 문서와 상관없는 말이 섞여
#   판단이 흐려진다. 그래서 <b>이 문서에 실제로 있는 말</b>로만 목록을 좁힌다.
#
#   [문턱을 넘지 못한 말도 보여 준다] 관리자가 그 문서를 눈앞에 두고 있어
#   판단이 가장 정확한 순간이다. 여기서는 '흔한 말'·'근거 적음' 표식을 달아
#   함께 올리고, 기본 체크만 꺼 둔다 — 버리면 사람이 볼 기회가 아예 없어진다.
#   (분류 단위로 훑는 suggest() 는 반대로 조용해야 하므로 문턱에서 자른다.)
#
# -in: node      = 확정한 분류의 dc_id
# -in: docs      = 재료 문서 목록(load_documents 결과)
# -in: focus     = 지금 보고 있는 문서의 경로(정규화 전 원본 경로도 된다)
# -in: 나머지    = suggest() 와 같다
#
# -out: dict = suggest() 결과와 같은 모양 + "focus_only" (이 문서에만 있는 말로
#              좁힌 목록. 문턱을 못 넘은 말은 flags 에 까닭이 담긴다) +
#              "near_cut" (상한 때문에 접어 둔 '까닭 단 후보' 수)
# -out: error = 없음(그 문서를 못 찾으면 focus_only 가 빈 목록)
#------------------------------------------------------------------
def suggest_for_doc(node, docs, focus, rule_doc=None, stopwords=None, tax=None,
                    suffixes=None, min_docs=MIN_DOCS, endings=None):
    suffixes = suffixes or docvocab.DOC_SUFFIXES
    res = suggest(node, docs, rule_doc=rule_doc, stopwords=stopwords, tax=tax,
                  suffixes=suffixes, min_docs=min_docs, endings=endings)

    key = norm_key(focus)
    doc = next((d for d in docs if d["key"] == key), None)
    res["focus"] = focus
    res["focus_only"] = []
    if doc is None:
        return res

    mine = set().union(*doc_words(doc, suffixes, endings).values())
    # 문턱을 넘은 후보 중 이 문서에 있는 것부터.
    seen = set()
    for cand in res["candidates"] + res["cluster_only"]:
        if cand["term"] in mine:
            res["focus_only"].append(dict(cand))
            seen.add(cand["term"])

    # 문턱을 못 넘은 말도 까닭을 달아 올린다.
    near = _near_misses(node, docs, doc, mine - seen, rule_doc, stopwords, tax,
                        suffixes, endings)
    res["focus_only"].extend(near)
    res["focus_only"].sort(key=lambda c: (-c["score"], -c["docs"], c["term"]))
    # 문턱을 넘은 후보는 몇 개든 남기고, 까닭을 단 것(near)만 상한을 건다 —
    # 제대로 걸러진 말을 상한 때문에 잃으면 안 된다.
    passed = [c for c in res["focus_only"] if c["group"] != "near"]
    misses = [c for c in res["focus_only"] if c["group"] == "near"][:NEAR_MISS_MAX]
    res["focus_only"] = passed + misses
    res["near_cut"] = max(0, len(near) - len(misses))
    return res


#------------------------------------------------------------------
# 문턱을 못 넘은 말에 까닭 달기 (suggest_for_doc 의 뒷부분)
#=> "왜 이 말은 제안 목록에 없나"를 화면에서 답할 수 있게 한다. 그냥 빼 버리면
#   관리자는 그 말이 검토조차 안 됐다고 오해한다.
#    · 흔한 말      다른 분류에도 자주 나온다(df_neg 초과) — 넣으면 오탐이 는다
#    · 근거 적음    이 분류 확정문서 두세 건에만 나온다
#    · 한 폴더      그 폴더에서만 쓰는 말일 수 있다
#   아주 지우는 말(이미 규칙에 있음·금지 목록·형태 쓰레기)은 여기에도 안 올린다.
#
# -in: node·docs·doc·terms = 대상과 아직 안 올라간 말들
# -in: 나머지 = suggest() 와 같다
#
# -out: list = 후보 dict 목록(checked 는 모두 False)
# -out: error = 없음
#------------------------------------------------------------------
def _near_misses(node, docs, doc, terms, rule_doc, stopwords, tax, suffixes,
                 endings=None):
    stopwords = stopwords or {"global": set(), "by_node": {}}
    by_node, owner = rule_terms(rule_doc)
    mine_rule = by_node.get(node, set())
    banned = {w.lower() for w in (stopwords.get("global") or set())}
    banned |= {w.lower() for w in (stopwords.get("by_node") or {}).get(node) or set()}
    titles = other_titles(tax, node)

    pos = [d for d in docs if node in d["labels"]]
    neg = [d for d in docs if node not in d["labels"]]
    pos_w, _ = cluster_weights(pos)
    neg_w, _ = cluster_weights(neg)
    n_pos = sum(pos_w.values()) or 1.0
    n_neg = sum(neg_w.values()) or 1.0

    # 이 문서의 말이 다른 문서들에서 얼마나 나오는지 센다.
    stats = {t: {"pos": 0.0, "neg": 0.0, "docs": 0, "clusters": set()} for t in terms}
    for d in pos:
        for t in set().union(*doc_words(d, suffixes, endings).values()) & terms:
            stats[t]["pos"] += pos_w[d["key"]]
            stats[t]["docs"] += 1
            stats[t]["clusters"].add(d["cluster"])
    for d in neg:
        for t in set().union(*doc_words(d, suffixes, endings).values()) & terms:
            stats[t]["neg"] += neg_w[d["key"]]

    words = doc_words(doc, suffixes, endings)
    out = []
    for term, st in stats.items():
        low = term.lower()
        # 아주 지우는 것들 — 여기서도 안 올린다.
        if low in mine_rule or low in banned or low in titles:
            continue
        where = next((w for w in ("title", "name", "head", "body") if term in words[w]), None)
        if where is None:
            continue
        if where in ("head", "body") and len(term) < TEXT_MIN_LEN:
            continue
        # 이 문서에만 있는 말은 올리지 않는다(위 NEAR_MISS_MIN_DOCS 설명).
        if st["docs"] < NEAR_MISS_MIN_DOCS:
            continue
        df_pos = st["pos"] / n_pos
        df_neg = st["neg"] / n_neg
        flags = []
        if df_neg > DF_NEG_MAX:
            flags.append("흔한 말")
        if st["docs"] < MIN_TERM_DOCS:
            flags.append("근거 적음")
        if len(st["clusters"]) < MIN_TERM_CLUSTERS:
            flags.append("한 폴더")
        conflict = owner.get(low)
        if conflict and conflict != node:
            flags.append(f"충돌: {conflict} 규칙에 있음")
        if looks_proper_noun(term, pos, suffixes):
            flags.append("고유명사 의심")
        fields, extra, _ = placement(where)
        out.append({"term": term, "fields": fields, "extra": extra,
                    "checked": False, "where": where, "group": "near",
                    "df_pos": round(df_pos, 4), "df_neg": round(df_neg, 4),
                    "score": round(log_odds(df_pos, df_neg, n_pos, n_neg), 3),
                    "docs": st["docs"], "clusters": len(st["clusters"]),
                    "flags": flags})
    return out


#------------------------------------------------------------------
# 고른 말을 규칙에 덧붙이기
#=> 관리자가 체크한 말을 doc_rule.yaml 의 dict 에 넣는다. 파일을 쓰지는 않는다 —
#   저장은 화면이 docvocab.save_doc() 한 경로로만 한다(주석 머리글·키 차례를
#   한 곳에서 정하려고 그렇게 나눠 둔다).
#
#   [덧붙이기만 한다] 있는 말은 순서까지 그대로 두고 뒤에 붙인다. 지우지 않는다.
#   규칙이 없는 분류면 만들지 않는다 — 규칙 생성은 '분류 불러오기'의 일이고,
#   같은 일을 두 곳에서 하면 결과가 갈라진다(docvocab 과 같은 원칙).
#
# -in: doc   = docvocab.load_doc() 결과(이 dict 를 제자리에서 고친다)
# -in: node  = 넣을 분류의 dc_id
# -in: picks = [{"term","fields","extra"}] 관리자가 고른 것들
#
# -out: (added, skipped, missing) = added: [(칸, 말)] 실제로 넣은 것,
#        skipped: [(칸, 말)] 이미 있어서 건너뛴 것,
#        missing: True 면 그 분류의 규칙이 없어 아무것도 못 넣었다
# -out: error = 없음
#------------------------------------------------------------------
def apply_terms(doc, node, picks):
    rules = (doc or {}).get("doctype_rules") or []
    rule = next((r for r in rules if r.get("node") == node), None)
    if rule is None:
        return [], [], True

    added, skipped = [], []
    for pick in picks:
        term = str(pick.get("term") or "").strip()
        if not term:
            continue
        for cell in pick.get("fields") or []:
            cur = rule.setdefault(cell, [])
            # 대소문자만 다른 중복도 막는다(Mpower/mpower 로 본문 건수가 두 배로
            # 세어지던 실제 사고가 있었다).
            if term.lower() in {str(w).strip().lower() for w in cur}:
                skipped.append((cell, term))
                continue
            cur.append(term)
            added.append((cell, term))
        # 본문 칸에 넣을 때 따라오는 값(min_count 등)은 규칙 줄에 함께 적는다.
        for key, val in (pick.get("extra") or {}).items():
            # 이미 더 센(작은) 값이 적혀 있으면 덮지 않는다 — 사람이 정한 값이 이긴다.
            if key not in rule:
                rule[key] = val
    return added, skipped, False


# 금지 목록 파일 맨 위에 남기는 안내. 이 파일도 저장할 때마다 통째로 다시 쓰여
# 주석이 남지 않으므로, 무엇을 적는 자리인지만 가리킨다(doc_rule.yaml 과 같은 규약).
STOPWORDS_HEADER = (
    "# 업무분류 규칙에 '제안하지 않을 말' 목록 — 화면에서 [다시 보지 않기] 를\n"
    "# 누를 때마다 다시 쓰는 파일이라 주석이 남지 않는다.\n"
    "#   global : 어느 분류에서도 제안하지 않는다\n"
    "#   by_node: 그 분류에서만 제안하지 않는다\n"
    "# 이 목록은 '제안'만 막는다 — 이미 규칙에 들어 있는 말을 지우지는 않는다.\n"
)


#------------------------------------------------------------------
# 금지 목록에 말 더하기
#=> "이 말은 오탐이라 뺐다"는 지식이 사람 머릿속에만 있으면 같은 말이 계속
#   되살아난다 — '인증'·'고객'·'계약'·'휴가'를 규칙과 사전 양쪽에서 두 번 빼야 했다.
#   관리자가 후보를 기각하면 그 판단을 파일로 굳혀, 다음 제안에서 자동으로 빠지게 한다.
#    1) 있는 파일을 읽어 합친다(지우지 않는다 — 더하기만)
#    2) node 를 주면 그 분류에만, 안 주면 모든 분류에 적용한다
#    3) 원본은 .bak 으로 남기고 통째로 다시 쓴다(docvocab.save_doc 과 같은 규약)
#
# -in: path  = doc_rule_stopwords.yaml 경로(없으면 새로 만든다)
# -in: terms = 더할 말 목록
# -in: node  = 그 분류에만 적용할 dc_id. None 이면 global
#
# -out: list = 실제로 새로 더해진 말(이미 있던 말은 빠진다)
# -out: error = 파일을 못 쓰면 OSError 를 그대로 올린다(부르는 쪽이 사람에게 알린다)
#------------------------------------------------------------------
def add_stopwords(path, terms, node=None):
    cur = load_stopwords(path)
    want = [str(t).strip() for t in (terms or []) if str(t).strip()]
    if not want:
        return []

    target = cur["by_node"].setdefault(str(node), set()) if node else cur["global"]
    added = [t for t in want if t not in target]
    target.update(added)
    if not added:
        return []

    data = {"version": _today(), "global": sorted(cur["global"])}
    by_node = {k: sorted(v) for k, v in cur["by_node"].items() if v}
    if by_node:
        data["by_node"] = by_node
    if os.path.isfile(path):
        shutil.copyfile(path, path + ".bak")
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(STOPWORDS_HEADER)
        yaml.safe_dump(data, fp, allow_unicode=True, sort_keys=False)
    return added


#------------------------------------------------------------------
# 오늘 날짜 (파일에 적는 판 표시용)
#=> 금지 목록의 version 칸에 넣는다. 언제 손댄 목록인지만 알면 되므로 날짜까지다.
#
# -in: 없음
#
# -out: str = "2026.09.16"
# -out: error = 없음
#------------------------------------------------------------------
def _today():
    import datetime
    return datetime.date.today().strftime("%Y.%m.%d")


#------------------------------------------------------------------
# 채택·기각 이력 남기기
#=> doc_rule.yaml 에는 주석이 남지 않는다. "이 단어가 언제·어느 근거로 들어왔나"를
#   되짚을 수 있는 기록은 이 파일뿐이다. seedstore 의 감사 로그와 같은 원칙으로
#   append-only 다 — 고치지 않고 덧붙이기만 한다.
#
#   [기각도 남긴다] 금지 목록은 '지금 무엇을 막고 있나'만 보여 주고, 왜 막게 됐는지는
#   말해 주지 못한다. 나중에 "이 말 왜 안 나오지"를 답하려면 이력이 있어야 한다.
#
# -in: path     = doc_rule_suggest_audit.jsonl 경로
# -in: node     = 대상 분류의 dc_id
# -in: action   = "accept" | "reject"
# -in: cands    = 후보 dict 목록(term·fields·df_pos·df_neg·docs·clusters 를 싣는다)
# -in: reviewer = 결정한 사람
# -in: reason   = 메모(선택)
#
# -out: int = 남긴 줄 수
# -out: error = 파일을 못 쓰면 OSError 를 그대로 올린다
#------------------------------------------------------------------
def append_audit(path, node, action, cands, reviewer, reason=""):
    rows = []
    for cand in cands or []:
        rows.append({"ts": _now(), "node": node, "term": cand.get("term"),
                     "action": action, "fields": list(cand.get("fields") or []),
                     "df_pos": cand.get("df_pos"), "df_neg": cand.get("df_neg"),
                     "docs": cand.get("docs"), "clusters": cand.get("clusters"),
                     "flags": list(cand.get("flags") or []),
                     "reviewer": reviewer, "reason": reason})
    if not rows:
        return 0
    with open(path, "a", encoding="utf-8") as fp:
        for row in rows:
            fp.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


#------------------------------------------------------------------
# 지금 시각 (이력에 적는 표기)
#=> seedstore·평가셋 채점기와 같은 모양으로 쓴다. 문자열로 정렬해도 시간 순서가
#   유지되는 모양이라 나중에 sort 만으로 이력을 훑을 수 있다.
#
# -in: 없음
#
# -out: str = "2026-09-16 10:12:03"
# -out: error = 없음
#------------------------------------------------------------------
def _now():
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


#------------------------------------------------------------------
# 이력 읽기 (화면에 보여 주기용)
#=> "이 분류에 무엇을 넣었고 무엇을 기각했나"를 되짚는다.
#
# -in: path = doc_rule_suggest_audit.jsonl 경로
# -in: node = 이 분류의 것만 골라낸다(None 이면 전부)
#
# -out: list = 이력 dict 목록(파일에 적힌 차례 그대로)
# -out: error = 파일 없음·깨진 줄은 건너뛴다(예외를 올리지 않는다)
#------------------------------------------------------------------
def load_audit(path, node=None):
    out = []
    if not path or not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if node is None or row.get("node") == node:
                out.append(row)
    return out
