#------------------------------------------------------------------
# 문서자동분류 (Streamlit) — 보안등급 · 업무분류 2축 화면
#=> csoclassify 가 만든 분류 결과(cso_result.jsonl)를 담당자가 검토·확정하는 화면.
#   한 문서가 동시에 갖는 두 가지를 '한 화면에서' 다룬다.
#     · 보안등급(security) — 얼마나 조심할 문서인가. C 기밀/S 민감/O 공개, 문서당 1개
#     · 업무분류(doctype)  — 무엇에 관한 문서인가. 회사 분류 체계 트리, 문서당 여러 개
#
#   [화면 — plan/보안등급-업무분류-UIUX-설계.html 5장]
#     상단 고정: 관점 스위치(보안등급 / 업무분류 / 전체) + 검토자 이름
#     ① 현황   — 두 축 요약 카드
#     ② 문서함 — 한 행에 두 축을 같이 둔 표 + 선택 시 상세(좌 보안 / 우 업무분류)
#     ③ 검토함 — 관점에 따라 대상과 조작이 바뀌는 '오늘 할 일'
#     ④ 설정   — 폴더·회사 분류 체계·판단 기준·분류 실행(+고급: 경로/기준 문서/리포트)
#
#   [원칙]
#     · 관점 스위치는 '보기 설정'일 뿐이다 — 데이터·판정·저장 위치를 바꾸지 않는다.
#     · 보안등급은 하나만 고르므로 라디오, 업무분류는 여럿이 맞으므로 체크박스로 받는다.
#     · 원본 자동분류(cso_result.jsonl)는 절대 수정하지 않고, 사람의 결정은
#       cso_override.jsonl 에 append-only 로 쌓아 감사 추적을 남긴다.
#     · 화면에 보이는 모든 말은 uiwords.py 가 정한다(개발 용어를 그대로 쓰지 않는다).
#   실행:  streamlit run ui/app.py   (또는 ui/실행.bat)
#------------------------------------------------------------------

import json
import re
import os
import shutil
import sys
import tempfile
import glob as globmod
import time
import datetime
from collections import Counter, defaultdict

import pandas as pd
import streamlit as st

# 같은 폴더의 화면 모듈(gerunner·rulesedit 등)을 확실히 import 하도록 경로 추가.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 기준 문서 저장소는 2026-09-08 부터 라이브러리(src/csoclassify/seedstore.py)에 있다.
# 화면·CLI·--make-doctype-seeds 가 같은 writer 를 쓰게 하려는 것이라, 화면 쪽에도
# 소스 폴더를 경로에 넣어 준다(exe 로 돌 때는 패키지 안에 들어 있어 필요 없다).
sys.path.insert(0, os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "src")))
from csoclassify import seedstore
from csoclassify import record as csorecord
from csoclassify.classify import termsuggest
from csoclassify.classify import docvocab
import gerunner
import rulesedit
import doctype_review
import docruleedit
import docrulegen
import taxonomy as taxlib
import uisettings
import uierrlog
import docidkey
import uireset
import uiwords as W

# 메뉴 4개(설계서 5장). 문자열 자체가 화면에 그대로 나가고 session_state 의 값도 된다.
MENU_HOME = "현황"
MENU_BOX = "문서함"
MENU_INBOX = "검토함"
# 설정 화면에는 '④ 분류 실행'도 함께 있다. 이름이 '설정'뿐이면 분류를 어디서
# 시작하는지 못 찾으므로, 두 가지 일을 다 한다는 것을 이름으로 알린다.
MENU_SET = "설정/분류"
# 기준 문서는 '설정 안의 한 칸'이 아니라 계속 손대는 자산이라 메뉴로 따로 뺐다.
MENU_SEED = "기준문서"

# 화면 이동 예약을 담아 두는 세션 키. 위젯 key("menu")와 '반드시 달라야' 한다 —
# 위젯 key 에 직접 값을 넣는 것이 바로 아래에서 설명하는 금지 동작이다.
_NAV_MENU = "_pending_menu"


#------------------------------------------------------------------
# 다른 화면으로 이동 예약
#=> "이 버튼을 누르면 검토함으로 가라" 같은 화면 이동을 처리한다.
#   [왜 st.session_state["menu"] 에 바로 못 넣나]
#   메뉴는 위젯(segmented_control)이 key="menu" 로 값을 들고 있다.
#   streamlit 은 **위젯이 만들어진 뒤에** 그 key 에 값을 넣는 것을 금지한다
#   (StreamlitAPIException: cannot be modified after the widget ... is instantiated).
#   그런데 화면 이동 버튼은 전부 위젯이 만들어진 다음에 그려지므로, 거기서
#   바로 넣으면 반드시 이 예외가 난다.
#   그래서 여기서는 '예약'만 별도 키에 적어 두고 rerun 하고, 다음 실행의 맨 앞
#   (위젯을 만들기 전)에서 apply_pending_nav() 가 실제 값으로 옮긴다.
#
# -in: menu = 옮겨 갈 메뉴(None 이면 메뉴는 그대로)
#
# -out: 없음(예약만 하고 st.rerun() 으로 화면을 다시 그린다 — 이 함수 뒤 코드는
#        실행되지 않는다)
# -out: error = 없음
#------------------------------------------------------------------
def goto(menu=None):
    if menu is not None:
        st.session_state[_NAV_MENU] = menu
    st.rerun()


#------------------------------------------------------------------
# 예약된 화면 이동을 실제 값으로 옮기기
#=> main() 이 위젯을 만들기 '직전'에 딱 한 번 부른다. 이 시점에는 아직 위젯이
#   없으므로 위젯 key 에 값을 넣어도 막히지 않는다.
#   예약은 한 번 쓰고 지운다(pop) — 남겨 두면 사용자가 손으로 메뉴를 바꿔도
#   매번 예약된 화면으로 되돌아가 버린다.
#
# -in: 없음
#
# -out: 없음(session_state 갱신)
# -out: error = 없음
#------------------------------------------------------------------
def apply_pending_nav():
    m = st.session_state.pop(_NAV_MENU, None)
    if m is not None:
        st.session_state["menu"] = m

# 등급 표기 규약은 uiwords.py 한 곳에서 정한다(설계서 4장). 아래는 옛 이름을
# 그대로 쓰는 코드가 많아 남겨 둔 별칭이다 — 값의 출처는 uiwords 하나뿐이다.
GRADE_LABEL = W.GRADE_LABEL
GRADE_ORDER = W.GRADE_ORDER
GRADE_CHOICES = W.GRADE_CHOICES

# 저확신 기준(검토함 편입선) — 이 값보다 확신이 낮은 판정은 사람이 한 번 본다.
# [2026-09-21] 코드 상수에서 정책 파일로 옮겼다(cso_rule.yaml 의 defaults.review_threshold).
#   목록·문턱을 코드에 두면 고치는 데 배포가 필요하고, 고객마다 검토량이 다르다.
#   아래 값은 그 칸이 없는 옛 규칙셋에서 쓰는 기본값이다 — 종전과 같은 0.6 이라
#   업그레이드만으로 검토량이 달라지지 않는다.
LOW_CONF = 0.6


#------------------------------------------------------------------
# 검토함 편입선 읽기
#=> cso_rule.yaml 의 defaults.review_threshold 를 읽는다. 파일이 없거나 값이
#   이상하면 기본값(LOW_CONF)으로 돌아간다 — 현황 화면이 규칙셋 문제로 통째로
#   멈추면 안 된다(그 문제는 규칙 편집 화면이 따로 알려 준다).
#
# -in: path = cso_rule.yaml 경로
#
# -out: float = 0 초과 1 이하의 문턱값
# -out: error = 없음(읽기·파싱 실패는 기본값으로 삼킨다)
#------------------------------------------------------------------
def load_review_threshold(path):
    try:
        v = float((rulesedit.load_doc(path).get("defaults") or {}).get("review_threshold"))
    except Exception:
        return LOW_CONF
    return v if 0.0 < v <= 1.0 else LOW_CONF

# '비슷한 문서 참고'가 등급을 옮길지 정하는 문턱값. 엔진의 판정 기준을 화면이
# 그대로 되뇌는 값이라, 엔진이 바뀌면 여기도 같이 고쳐야 한다
# (원본: classify/propagate.py 의 MIN_SIM · DUP_THRESHOLD).
EMBED_MIN_SIM = 0.55       # 가장 비슷한 문서가 이보다 멀면 등급을 옮기지 않는다
EMBED_DUP_SIM = 0.95       # 이보다 가까우면 사본으로 보고 그 등급을 그대로 상속

# 위와 헷갈리기 쉬운 '다른' 문턱값. 이건 등급을 옮기는 기준이 아니라, 기준 문서를
# 등록할 때 "이미 있는 것과 사실상 같은 문서인가"를 경고하는 기준이다. 값이 더
# 엄격한 이유 — 잘못 옮긴 등급은 사람이 화면에서 바로잡을 수 있지만, 사본이
# 기준 문서로 쌓이면 그 문서가 표를 두 번 던져 조용히 판정을 기울인다.
# (seedstore.bulk_add_confident 의 dup_thresh 기본값과 같은 값을 쓴다.)
SEED_DUP_SIM = 0.985

# 일괄 등록에서 "올릴 것이 없어 뺀다"를 나타내는 표기. 화면 표와 실제 등록이 같은
# 값을 보고 판단해야 해서 상수로 둔다(문구를 고치면 두 곳이 함께 바뀐다).
BULK_SKIP = "빠짐 — 정한 것이 없음"

# 왜 옮겼는지/안 옮겼는지 한 줄 설명(엔진이 남긴 method 값 그대로 대응).
EMBED_WHY = {
    "dup_inherit": "거의 같은 문서가 있어 그 등급을 그대로 가져왔습니다",
    "knn_vote": "비슷한 문서들의 등급이 한쪽으로 모여 그 등급을 옮겼습니다",
    "too_far": f"가장 비슷한 문서가 {int(EMBED_MIN_SIM * 100)}% 에 못 미쳐 "
               "등급을 옮기지 않았습니다",
    "mixed": "비슷한 문서들의 등급이 갈려 등급을 옮기지 않았습니다",
    "no_seeds": "비교할 기준 문서가 없어 등급을 옮기지 않았습니다",
}


#------------------------------------------------------------------
# 등급 값 정규화(None → "보류")
#=> 레코드의 grade 는 None 일 수 있는데, 표/집계에서 다루기 쉽게 문자열 "보류"로 바꾼다.
#   화면에는 "판단 못 함"으로 나간다(uiwords.GRADE_LABEL).
#
# -in: g = 등급 값("C"/"S"/"O"/None)
#
# -out: str = "C"/"S"/"O"/"보류"
# -out: error = 없음
#------------------------------------------------------------------
def norm_grade(g):
    return W.norm_grade(g)


#------------------------------------------------------------------
# 분류 결과(grades.jsonl) 로드
#=> jsonl 을 읽어 '문서 레코드'만 리스트로 돌려준다. 파일 수정시각(mtime)을
#   캐시 키에 넣어, 파일이 바뀌면 자동으로 다시 읽는다.
#   [문서가 아닌 줄을 걸러내는 이유] csoclassify 는 결과 파일 맨 끝에 실행 요약
#   ({"summary": {...}})을 한 줄 덧붙인다. 이 줄에는 file 필드가 없어서, 그대로
#   문서 목록에 넣으면 문서함에 **이름도 폴더도 빈 유령 행**이 하나 생기고
#   전체 건수도 1건 부풀려진다(등급이 없으니 '보류·등급 확인'으로도 잡힌다).
#   파일 없이 만들어진 줄은 문서가 아니므로 여기서 한 번에 걸러 낸다.
#
# -in: path  = grades.jsonl 경로
# -in: mtime = 파일 수정시각(캐시 무효화용, 값 자체는 안 씀)
#
# -out: list = 문서 레코드 dict 리스트(요약 줄 등 문서가 아닌 줄은 제외)
# -out: error = 파일 없음/파싱 오류 시 예외 전파(호출측에서 처리)
#------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def load_records(path, mtime):
    recs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            # 문서 레코드의 조건: dict 이고 file 이 채워져 있을 것.
            # (요약 줄은 file 이 아예 없다.)
            if isinstance(rec, dict) and rec.get("file"):
                recs.append(rec)
    return recs


#------------------------------------------------------------------
# 보안등급 수정 기록 로드
#=> cso_override.jsonl 을 읽어, 파일별 "가장 최근 수정"과 전체 이력을 만든다.
#   같은 문서를 여러 번 고쳤으면 마지막 것이 유효하다(append-only, 나중 것 우선).
#
#   [중요] 같은 파일에 업무분류 결정(axis:"doctype")도 함께 쌓인다. 그 줄을 여기서
#   걸러내지 않으면 "업무분류를 확정했을 뿐인데 보안등급이 사람 손을 탄 것으로
#   보이고, new_grade 가 없어 등급이 판단 못 함으로 바뀌는" 사고가 난다.
#   그래서 security 줄(axis 가 "security" 이거나, axis 자체가 없는 옛 기록)만 읽는다.
#
# -in: path = cso_override.jsonl 경로(없으면 빈 결과)
#
# -out: (latest, history) = {file: 최신 entry}, {file: [entry,...]}
# -out: error = 없음(파일 없거나 깨진 줄은 건너뜀)
#------------------------------------------------------------------
def load_overrides(path):
    latest, history = {}, defaultdict(list)
    if not path or not os.path.isfile(path):
        return latest, history
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            # axis 가 없는 줄은 두 축이 나뉘기 전에 쌓인 옛 기록 = security 다.
            if e.get("axis", "security") != "security":
                continue
            file = e.get("file")
            if not file:
                continue
            latest[file] = e                 # 나중 줄이 앞 줄을 덮음 → 최신 유효
            history[file].append(e)
    # 경로 정확 일치만으로는 사람이 고친 기록이 조용히 사라진다(실측 4건).
    # doc_id → 정규화 경로 → 대소문자 순으로 잇는 색인으로 감싼다(설계 §7-5-3).
    return docidkey.OverrideIndex(latest), history


#------------------------------------------------------------------
# 초 → "m분 ss초"/"s초" 표기
#=> 실행시간·경과시간을 사람이 읽기 좋은 한글 표기로 바꾼다.
#
# -in: sec = 초(실수/정수)
#
# -out: str = "3분 05초" 또는 "42초"
# -out: error = 없음
#------------------------------------------------------------------
def fmt_duration(sec):
    sec = int(sec)
    return f"{sec // 60}분 {sec % 60:02d}초" if sec >= 60 else f"{sec}초"


#------------------------------------------------------------------
# 실행 메타(총 실행시간) 사이드카 경로
#=> 분류 결과(grades_path) 옆에 실행 요약을 담는 작은 json 경로를 만든다.
#   결과 본문(jsonl)은 손대지 않고, 실행시간·완료시각만 따로 보관한다.
#
# -in: grades_path = 분류 결과 jsonl 경로
#
# -out: str = "<grades_path>.meta.json"
# -out: error = 없음
#------------------------------------------------------------------
def run_meta_path(grades_path):
    return grades_path + ".meta.json"


#------------------------------------------------------------------
# --simple 형식 파일 경로
#=> 분류 결과 옆에 나란히 둔다(cso_result.jsonl → cso_result.simple.jsonl).
#
# -in: grades_path = 분류 결과 jsonl 경로
#
# -out: str = 옆에 둘 --simple 파일 경로
# -out: error = 없음
#------------------------------------------------------------------
def simple_path(grades_path):
    base, ext = os.path.splitext(grades_path or "cso_result.jsonl")
    return f"{base}.simple{ext or '.jsonl'}"


#------------------------------------------------------------------
# 레코드 하나 → --simple 한 줄
#=> 엔진의 --simple 이 내는 것과 '같은 내용'을 전체 결과에서 그대로 뽑는다.
#   엔진 쪽 구현(cli.py _simple_record · main.rs)과 고르는 칸이 같아야 한다:
#     file · hash · grade (+ 업무분류 축이 돌았으면 doctype = dc_id 배열,
#                          + 문서 ID 축이 돌았으면 doc_id)
#   업무분류 키는 축이 돌았을 때만 붙인다 — 빈 배열로 내보내면 "분류를 못 했다"와
#   "축을 안 썼다"가 구분되지 않는다. doc_id 도 같은 규약이다.
#
# -in: rec = 분류 레코드(전체)
#
# -out: dict = --simple 한 줄
# -out: error = 없음
#------------------------------------------------------------------
def simple_record(rec):
    # 칸 차례도 엔진의 --simple 과 맞춘다(file·hash → grade·doctype → why).
    out = {"file": rec.get("file"), "hash": rec.get("hash"),
           "grade": csorecord.grade_of(rec)}
    if csorecord.doctype_of(rec) is not None:
        out["doctype"] = csorecord.doctype_ids(rec)
    # 문서 ID 축이 돌았을 때만 doc_id 를 붙인다. 폴백(content·path)은 우리가 만들어 낸
    # 값이라 적재하면 안 되므로 None 으로 내보낸다 — 받는 쪽은 hash 로 문서를 가린다.
    if "doc_id_source" in rec:
        out["doc_id"] = rec.get("doc_id") if rec.get("doc_id_source") == "sfile_id" else None
    # 못 읽은 문서에는 그 사실을 함께 싣는다 — 엔진의 --simple 과 같은 모양이다.
    err = rec.get("error")
    if isinstance(err, dict):
        out["error"] = err.get("reason") or "extract_failed"
    # 처리제외(스캔 PDF) 표식도 엔진의 --simple 과 같은 모양으로 싣는다.
    ex = rec.get("excluded")
    if isinstance(ex, dict):
        out["excluded"] = ex.get("kind") or "excluded"
    return out


#------------------------------------------------------------------
# --simple 형식 파일 쓰기
#=> 분류를 다시 돌리지 않는다. 방금 만든 전체 결과에서 필요한 칸만 뽑아 쓴다 —
#   --simple 은 애초에 전체 레코드의 '골라 담기'라서 결과가 같고, 다시 돌리면
#   문서를 통째로 한 번 더 읽어 시간이 두 배가 된다.
#
# -in: grades_path = 분류 결과 jsonl 경로
# -in: records     = 그 결과에서 읽은 레코드 리스트
#
# -out: (path, n) = 쓴 파일 경로와 줄 수
# -out: error = 쓰기 실패 시 예외 전파(호출부가 화면에 표시)
#------------------------------------------------------------------
def write_simple_file(grades_path, records):
    path = simple_path(grades_path)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(simple_record(rec), ensure_ascii=False) + "\n")
    return path, len(records)


#------------------------------------------------------------------
# 실행 메타 저장(총 실행시간 등)
#=> 분류 실행이 끝나면 총 소요시간·대상 파일수·완료시각을 사이드카 json 에 쓴다.
#   대시보드가 이 값을 읽어 "총 실행시간"을 표시한다.
#
# -in: grades_path = 분류 결과 jsonl 경로
# -in: seconds     = 총 실행시간(초, wall-clock)
# -in: files       = 대상 파일 수
# -in: propagated  = 임베딩 전파까지 포함했는지 여부
#
# -out: 없음(사이드카 파일 기록)
# -out: error = 쓰기 실패는 호출부에서 무시(부가정보라 실행을 막지 않음)
#------------------------------------------------------------------
def save_run_meta(grades_path, seconds, files, propagated):
    meta = {
        "seconds": round(float(seconds), 1),
        "files": int(files),
        "propagated": bool(propagated),
        "finished_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(run_meta_path(grades_path), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)


#------------------------------------------------------------------
# 실행 메타 로드
#=> 사이드카 json 에서 총 실행시간 등을 읽는다. 없거나 깨졌으면 None.
#
# -in: grades_path = 분류 결과 jsonl 경로
#
# -out: dict|None = {seconds, files, propagated, finished_at} 또는 None
# -out: error = 없음(문제 있으면 조용히 None)
#------------------------------------------------------------------
def load_run_meta(grades_path):
    p = run_meta_path(grades_path)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


#------------------------------------------------------------------
# 오버라이드 1건 기록(append-only)
#=> 담당자가 등급을 바꿀 때, 누가·언제·왜·이전→새 등급을 overrides.jsonl 에 한 줄
#   추가한다. 원본 grades.jsonl 은 건드리지 않는다(감사 추적 보존).
#   axis:"security" 를 함께 찍어 doctype_review.append_doctype_override() 가
#   같은 파일에 쌓는 axis:"doctype" 줄과 섞이지 않게 한다(설계서 7-4-1 —
#   "축을 명시해 두 축의 수정이 섞이지 않게 한다"). load_overrides() 는 이
#   필드를 보지 않고 그대로 읽으므로 옛 파일(axis 없음)도 계속 잘 읽힌다.
#
# -in: path      = overrides.jsonl 경로
# -in: file      = 대상 문서 경로
# -in: old_grade = 바꾸기 전 등급(자동 또는 이전 오버라이드)
# -in: new_grade = 새 등급("C"/"S"/"O"/"보류")
# -in: reason    = 수정 사유(감사용)
# -in: reviewer  = 검토자 이름
#
# -out: 없음(파일에 append)
# -out: error = 쓰기 실패 시 예외 전파
#------------------------------------------------------------------
def append_override(path, file, old_grade, new_grade, reason, reviewer,
                    kind="change", doc_id=None, doc_hash=None):
    entry = {
        "file": file,
        # 다음 실행에서 이 기록을 되찾는 키. 경로만 남기면 폴더를 옮기거나
        # 표기가 달라졌을 때 조용히 끊긴다(설계 §7-5-3 ①·②).
        #   doc_id = MpowerV11 문서 번호(있을 때만) · hash = 내용 지문
        # 2026-09-10 부터 지문이 주된 결합 키다 — 번호는 목록을 준 실행에만 있고,
        # 경로는 폴더를 옮기면 끊기지만 지문은 둘 다 견딘다.
        "doc_id": doc_id,
        "hash": doc_hash,
        "axis": "security",
        # 값을 바꾼 것("change")과 자동 판정이 맞다고 확인만 한 것("confirm")을
        # 구분해 둔다. 둘 다 "사람이 판단을 끝냈다"는 뜻이라 검토함에서는 똑같이
        # 내려가지만, 나중에 규칙 정확도를 잴 때는 정반대의 뜻이라 섞이면 안 된다.
        "kind": kind,
        "old_grade": old_grade,
        "new_grade": new_grade,
        "reason": reason,
        "reviewer": reviewer,
        "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


#------------------------------------------------------------------
# 최종 등급 계산(오버라이드 우선)
#=> 자동 등급 위에 사람 수정이 있으면 그걸 최종으로 삼는다.
#
# -in: rec    = 분류 레코드
# -in: latest = 파일별 최신 오버라이드 맵
#
# -out: (final_grade, is_overridden) = 최종 등급("C"/"S"/"O"/"보류"), 수정 여부
# -out: error = 없음
#------------------------------------------------------------------
def effective_grade(rec, latest):
    auto = norm_grade(csorecord.grade_of(rec))
    # 엔진이 '결정 뒤 내용이 바뀌어 따르지 않음'(stale)으로 판단했으면 엔진 판정을 쓰고
    # 사람이 다시 보게 한다(설계: 사람등급지정 §05 · §07)
    _eov = ((rec.get("why") or {}).get("security") or {}).get("override") or {}
    if _eov.get("stale") and _eov.get("applied") is False:
        return auto, False
    ov, _how = docidkey.lookup(latest, rec)
    if ov:
        return norm_grade(ov.get("new_grade")), True
    return auto, False


#------------------------------------------------------------------
# 사람이 '값을 바꾼' 문서인가
#=> effective_grade 의 두 번째 값은 "사람이 판단을 끝냈는가"(확인만 한 것도 포함)라
#   화면에 "관리자가 고쳤습니다"라고 쓰기에는 너무 넓다. 여기서는 자동 판정과
#   실제로 값이 달라진 문서만 골라낸다 — 표·집계·내보내기의 '고침' 열은 이 값이다.
#
# -in: rec    = 분류 레코드
# -in: latest = 파일별 최신 보안등급 결정 맵
#
# -out: bool = 자동 판정과 다른 등급으로 바뀌었으면 True(확인만 했으면 False)
# -out: error = 없음
#------------------------------------------------------------------
def grade_changed(rec, latest):
    ov, _how = docidkey.lookup(latest, rec)
    if not ov:
        return False
    # 엔진이 사람 결정을 이미 적용한 결과면 grade 가 사람 등급이다 — 원래 판정(computed)과 비교한다
    sec = (rec.get("why") or {}).get("security") or {}
    auto = (sec.get("computed") or {}).get("grade") if sec.get("method") == "manual_override" \
        else csorecord.grade_of(rec)
    return norm_grade(ov.get("new_grade")) != norm_grade(auto)


#------------------------------------------------------------------
# 레코드 → 표 DataFrame (두 축을 한 줄에)
#=> 목록·집계에 쓰기 좋게 레코드들을 한 줄씩 평평한 표로 만든다. 설계서 7장의
#   핵심 요구("한 행에 보안등급과 업무분류가 동시에 보인다")를 데이터 층에서
#   만족시키는 자리다 — 화면이 두 표를 합치는 게 아니라, 표가 처음부터 하나다.
#    1) 보안등급: 자동 판정 위에 사람의 수정을 얹어 최종 등급을 낸다
#    2) 업무분류: 후보 목록에 확정/거절 결정을 얹어 표에 쓸 요약 문자열을 만든다
#    3) 두 축 각각 "사람이 더 볼 것이 남았는지"(need_sec/need_doc)를 계산한다
#
# -in: records   = 레코드 리스트
# -in: latest    = 파일별 최신 보안등급 수정 맵(load_overrides 의 latest)
# -in: latest_dt = 파일별 최신 업무분류 결정 맵(없으면 업무분류 열이 빈 값)
# -in: tax       = 회사 분류 체계('직접 추가'로 확정된 분류의 이름을 찾는 데 쓴다)
# -in: seed_files = 기준 문서로 등록된 경로 집합(seedstore.load_seed_files 결과).
#                   기준 문서는 관리자가 "이 판정을 잣대로 삼겠다"고 정한 문서라
#                   더 볼 것이 없다 — 검토 대상에서 뺀다
#
# -out: DataFrame = file/folder/name/auto/final/overridden/confidence/…
#                   + doctype(표시 문자열)/dt_n/dt_reviewed/need_sec/need_doc/todo
# -out: error = 없음
#------------------------------------------------------------------
def records_to_df(records, latest, latest_dt=None, tax=None, seed_files=None,
                  low_conf=LOW_CONF):
    latest_dt = latest_dt or {}
    seed_files = seed_files or set()
    rows = []
    for r in records:
        # decided = "사람이 판단을 끝냈다"(고침 + 확인). 검토함에서 내리는 기준.
        final, decided = effective_grade(r, latest)
        conf = round(float(csorecord.security_of(r).get("confidence") or 0.0), 3)
        cands, reviewed = doctype_review.effective_doctype(r, latest_dt, tax)
        # 업무분류에서 사람이 손대야 하는 것은 "아직 분류가 하나도 안 붙은 문서"다.
        # 기계가 후보를 낸 문서는 이미 분류된 것으로 본다.
        #   [축이 꺼진 배포와 구분] doctype 칸이 아예 없으면 이번 배포는
        #   그 축을 안 쓴 것이라, 셀 것도 검토할 것도 없다. 키는 있는데 values 가
        #   비어 있을 때만 '미분류'다 — 이 구분이 없으면 보안등급만 쓰는 배포에서
        #   모든 문서가 "업무분류 확인 필요"로 잡힌다.
        dt_axis_on = csorecord.doctype_of(r) is not None
        # [약한 라벨] 파일 이름 끝자리 핵어로만 붙은 라벨(설계서 13장 D1)뿐인 문서.
        # [2026-09-22 변경] 예전에는 이런 문서도 검토함에 올렸다. 평가셋에서 핵어
        # 라벨이 7건 중 7건 맞았고, 1차 규칙이 아무 후보도 못 낸 문서에만 붙으므로
        # '분류된 문서'로 보고 검토함에는 올리지 않는다. 결과 파일의 review 표시는
        # 그대로라 문서 상세에는 "확인 필요 — 파일 이름만 보고 제안"으로 보이고,
        # 기준 문서(seed)로도 여전히 쓰지 않는다(seedgen 이 review 표시를 본다).
        alive = [c for c in cands if c.get("status") != "rejected"]
        only_weak = bool(alive) and all(c.get("status") == "proposed" for c in alive)
        # 사람이 한 번 보고 "해당 없음"으로 정리한 문서는 다시 부르지 않는다.
        need_doc = dt_axis_on and (not cands) and (not reviewed)
        # 보안등급은 판단 못 했거나 확신이 낮으면 사람이 본다. 이미 사람이 고친
        # 문서는 다시 부르지 않는다 — 검토를 끝낸 문서가 큐에 계속 남으면 안 된다.
        need_sec = (not decided) and (final == "보류" or conf < low_conf)
        # 기준 문서로 올린 문서는 검토 대상에서 뺀다 — 관리자가 "이 문서를 잣대로
        # 삼겠다"고 직접 정한 문서를 다시 "확인이 필요합니다"라고 불러 세우면
        # 큐가 영영 줄지 않는다. (등급이 아직 '보류'인 채 업무분류만 등록한 문서도
        # 마찬가지로 뺀다. 그 사실은 문서함의 상태 열에 그대로 남는다.)
        is_seed = seedstore.norm_file(r.get("file", "")) in seed_files
        # 빼기 '전'의 값을 따로 남긴다. 현황 화면이 "차트에는 10건인데 확인 필요는
        # 11건"처럼 어긋나 보일 때, 그 차이가 어디서 왔는지(기준 문서 제외·약한
        # 라벨) 말해 주려면 두 값이 다 있어야 한다.
        raw_need_sec, raw_need_doc = need_sec, need_doc
        if is_seed:
            need_sec = need_doc = False
        rows.append({
            "is_seed": is_seed,
            "raw_need_sec": raw_need_sec,
            "raw_need_doc": raw_need_doc,
            # 파일 이름 끝자리 핵어로만 붙은 약한 라벨뿐인 문서(13장 D1).
            "dt_weak_only": only_weak,
            "file": r.get("file", ""),
            "folder": os.path.dirname(r.get("file", "")),
            "name": os.path.basename(r.get("file", "")),
            "auto": norm_grade(csorecord.grade_of(r)),
            "final": final,
            # 표·내보내기의 '고침'은 값이 실제로 달라진 문서만이다(확인만 한 것 제외).
            "overridden": grade_changed(r, latest),
            "confidence": conf,
            "method": r.get("method", ""),
            "decided_by": ", ".join(r.get("decided_by", []) or []),
            "rule_version": r.get("rule_version", ""),
            "doctype": W.doctype_cell(cands),
            "dt_n": len(cands),
            "dt_reviewed": reviewed,
            "dt_roots": " ".join(sorted({(c.get("path") or "").split(" > ")[0]
                                         for c in cands
                                         if c.get("status") != "rejected"})),
            "need_sec": need_sec,
            "need_doc": need_doc,
            "todo": W.todo_label(need_sec, need_doc),
        })
    return pd.DataFrame(rows)


#------------------------------------------------------------------
# 근거의 '무엇이 몇 번' 한 칸을 사람 글자로
#=> 엔진이 남기는 terms 한 칸의 모양이 자리마다 다르다. 화면이 한 가지만 알고
#   있으면 다른 모양을 만나는 순간 화면 전체가 죽는다 — 실제로 Rust 엔진의
#   '복합 개인정보(COMBO)' 규칙이 ["ADDRESS", 1] 짝 배열을 써서 화면이 멈췄다.
#   엔진은 dict 로 통일해 두었지만, 이미 저장된 결과 파일에는 옛 모양이 남아
#   있으므로 화면은 세 가지를 모두 읽는다.
#    1) {"term": "경조", "count": 20}  → "경조 20회"   (표준)
#    2) ["ADDRESS", 1]                 → "ADDRESS 1회" (옛 Rust COMBO)
#    3) "계약서"                        → "계약서"      (건수 없는 목록)
#
# -in: t = terms 목록의 한 칸
#
# -out: str = 화면에 그대로 쓸 한 조각
# -out: error = 없음(모르는 모양이면 그대로 글자로 만든다)
#------------------------------------------------------------------
def term_count_text(t):
    if isinstance(t, dict):
        term, cnt = t.get("term", ""), t.get("count")
    elif isinstance(t, (list, tuple)):
        term = t[0] if len(t) > 0 else ""
        cnt = t[1] if len(t) > 1 else None
    else:
        term, cnt = t, None
    return f"{term} {cnt}회" if cnt is not None else f"{term}"


#------------------------------------------------------------------
# 보안등급 근거 렌더 ("왜 이렇게 판단했나")
#=> 선택한 문서가 왜 이 등급인지를 신호별로 한 줄씩 풀어 보여준다.
#   [설계서 원칙 4] 근거는 접지 않는다. 예전에는 신호마다 expander 로 접어 뒀는데,
#   접혀 있으면 아무도 열지 않아 "결과만 보고 판단"하게 된다. 사람은 근거를 보고
#   판단해야 하므로 처음부터 펼쳐 놓는다.
#    1) 최종/자동 등급과 확신도를 위에 요약
#    2) 내용 검사·폴더 규칙·파일 이름·비슷한 문서 참고 순으로 근거를 나열
#    3) 채택되지 않은 후보(candidates)를 "다른 의견"으로 항상 보여준다 —
#       남기기만 하고 안 보여주면 남긴 이유가 없다(상위 설계 7-1)
#
# -in: rec    = 분류 레코드
# -in: latest = 보안등급 수정 맵(최종 등급 표시용)
#
# -out: 없음(Streamlit 위젯 출력)
# -out: error = 없음
#------------------------------------------------------------------
def render_evidence(rec, latest):
    final, decided = effective_grade(rec, latest)
    sec = csorecord.security_of(rec)
    sig = sec.get("signals") or {}

    # 두 축은 화면에서 대등해야 한다 — 오른쪽 업무분류 칸과 똑같은 제목 크기·모양을
    # 쓴다. 예전에는 이쪽만 metric 의 작은 회색 라벨이라 한 축이 곁다리로 보였다.
    st.markdown(f"##### {W.AXIS_SEC} <span style='font-weight:400;color:gray'>— "
                f"{W.AXIS_SEC_SUB}</span>", unsafe_allow_html=True)
    st.caption("문서당 **하나만** · 자동 판정이 맞으면 아래에서 [이대로 확정] 하세요")

    c1, c2 = st.columns(2)
    c1.metric("지금 등급", GRADE_LABEL[final], help="관리자가 고쳤으면 그 값이 최종입니다")
    c2.metric("확신", W.pct(sec.get("confidence") or 0) or "-")
    if decided and grade_changed(rec, latest):
        st.caption(f"관리자가 고친 등급입니다 · 자동 판정은 "
                   f"{GRADE_LABEL[norm_grade(csorecord.grade_of(rec))]} 이었습니다")
    elif decided:
        # 확인만 한 것도 사람이 남긴 판단이다. 화면에 안 쓰면 "왜 검토함에서
        # 사라졌지?" 를 아무도 답할 수 없다.
        st.caption("관리자가 **이대로 맞다고 확정**한 등급입니다 · 자동 판정 그대로")

    st.markdown("**왜 이렇게 판단했나**")
    lines = []

    # ── 내용 검사(개인정보·키워드) ──
    rule = sig.get("rule", {}) or {}
    for h in rule.get("hits", []) or []:
        terms = h.get("terms", [])
        # 키워드는 어떤 단어가 몇 번인지, 개인정보는 건수만(원문은 화면에 내지 않는다).
        if terms:
            detail = ", ".join(term_count_text(t) for t in terms)
        else:
            detail = f"{h.get('count', 0)}건 발견"
        lines.append(f"{W.GRADE_EMOJI.get(norm_grade(h.get('grade')), '·')} "
                     f"**{h.get('name', '')}** {detail} · _{W.SIGNAL_LABEL['rule']}_")
    if not (rule.get("hits") or []):
        lines.append(f"· 개인정보·키워드가 발견되지 않았습니다 · _{W.SIGNAL_LABEL['rule']}_")

    # ── 법상 민감정보군 ──
    # rule 과 히트 모양이 같다(id·name·grade·count·terms). 다만 '없을 때'는 줄을
    # 내지 않는다 — 엔진은 걸린 신호만 싣기 때문에 '없음'과 '안 봤음'을 구분할 수
    # 없고, 모든 카드에 빈 줄이 붙으면 정작 걸린 근거가 묻힌다.
    sens = sig.get("sensitive", {}) or {}
    for h in sens.get("hits", []) or []:
        terms = h.get("terms", [])
        detail = (", ".join(term_count_text(t) for t in terms) if terms
                  else f"{h.get('count', 0)}건 발견")
        lines.append(f"{W.GRADE_EMOJI.get(norm_grade(h.get('grade')), '·')} "
                     f"**{h.get('name', '')}** {detail} · "
                     f"_{W.SIGNAL_LABEL['sensitive']}_")

    # ── 보안분류 스탬프 ──
    # 문서에 찍힌 '대외비'·'CONFIDENTIAL' 같은 표식. 오탐이 적어 확신값이 가장
    # 높으므로(0.95), 이것이 등급을 정했을 때 화면이 그 사실을 못 적으면
    # "왜 C 인지" 를 아무도 답할 수 없다.
    stamp = sig.get("stamp", {}) or {}
    for h in stamp.get("hits", []) or []:
        where = {"head": "문서 앞부분에 ", "body": "본문에 "}.get(h.get("mode"), "")
        lines.append(f"{W.GRADE_EMOJI.get(norm_grade(h.get('grade')), '·')} "
                     f"{where}**{h.get('term', '')}** 표식이 있습니다"
                     f"({h.get('name', '')}) · _{W.SIGNAL_LABEL['stamp']}_")

    # ── 파일 이름 ──
    name = sig.get("name", {}) or {}
    for h in name.get("hits", []) or []:
        lines.append(f"{W.GRADE_EMOJI.get(norm_grade(h.get('grade')), '·')} "
                     f"파일 이름에 **{h.get('term', '')}** 이(가) 있습니다 · "
                     f"_{W.SIGNAL_LABEL['name']}_")

    # ── 비슷한 문서 참고(임베딩 전파) ──
    embed = sig.get("embed") or {}
    neigh = list((embed.get("neighbors") or [])) if embed else []
    if embed:
        top = embed.get("top_sim")
        if neigh:
            same = sum(1 for n in neigh if norm_grade(n.get("grade")) == final)
            lines.append(f"🧭 비슷한 문서 {len(neigh)}건 중 {same}건이 같은 등급입니다"
                         f"(가장 비슷한 문서 {W.pct(top)}) · _{W.SIGNAL_LABEL['embed']}_")
        else:
            lines.append(f"🧭 비슷한 문서를 찾지 못했습니다 · _{W.SIGNAL_LABEL['embed']}_")

    for ln in lines:
        st.markdown(f"- {ln}")

    # 비슷하다고 판단한 문서가 '무엇인지'를 이름까지 펼친다. 건수만 보여 주면
    # "둘 다 C 인데 왜 판단 못 함이지?" 를 풀 방법이 없다 — 사람이 근거를 보고
    # 판단해야 하므로 이름·닮은 정도·그 문서의 등급을 그대로 내놓는다.
    if neigh:
        for n in neigh[:5]:
            g = norm_grade(n.get("grade"))
            st.caption(f"　· {os.path.basename(n.get('file') or '')} · "
                       f"닮은 정도 {W.pct(n.get('sim'))} · "
                       f"{W.GRADE_EMOJI.get(g, '·')} {W.GRADE_PLAIN.get(g, g)}")
        if len(neigh) > 5:
            st.caption(f"　… 외 {len(neigh) - 5}건")
    # 왜 그 등급을 옮겼는지(또는 왜 안 옮겼는지)까지 적는다.
    why = EMBED_WHY.get(embed.get("method") or "")
    if why:
        st.caption(f"　→ {why}")

    # ── 다른 의견(채택되지 않은 후보) ──
    render_other_opinions(rec, final)


#------------------------------------------------------------------
# "다른 의견" 렌더 (채택되지 않은 보안등급 후보)
#=> 보안등급은 신호가 엇갈리면 더 엄격한 쪽 하나로 정해진다. 그 순간 나머지 근거가
#   화면에서 사라지면 검토자가 "왜 이 등급인가"를 되짚을 수 없다. 그래서 각 신호가
#   주장한 등급(security_candidates)을 사람 말로 풀어 항상 보여준다.
#   더 엄격한 후보가 있는데 채택되지 않았다면(=사람이 이미 등급을 낮춘 경우 등)
#   그 사실을 특히 눈에 띄게 알린다 — 덜 보호하는 실수가 더 비싸기 때문이다.
#
# -in: rec   = 분류 레코드(signals 로 후보를 만든다 · 옛 결과는 candidates 칸)
# -in: final = 지금 적용 중인 최종 등급("C"/"S"/"O"/"보류")
#
# -out: 없음(후보가 없거나 1개뿐이면 아무것도 그리지 않는다)
# -out: error = 없음
#------------------------------------------------------------------
def security_candidates(rec):
    """이 문서에 대해 각 신호가 주장한 등급 목록.

    2026-09-10 부터 레코드에 candidates 칸이 없다 — signals 에서 등급이 있는
    것만 추리면 같은 값이 나오므로 두 번 적지 않기로 했다. 옛 결과 파일에는
    그 칸이 남아 있어, 없을 때만 그쪽을 본다(옛 파일도 그대로 보여야 한다).
    """
    old = ((rec.get("labels") or {}).get("security") or {})
    sigs = csorecord.security_of(rec).get("signals") or {}
    if sigs:
        return [{"value": v.get("grade"),
                 "confidence": v.get("confidence", 0.0),
                 "from": name}
                for name, v in sigs.items()
                if isinstance(v, dict) and v.get("grade") is not None]
    return old.get("candidates") or []


#------------------------------------------------------------------
# '다른 의견' 표시 — 채택되지 않은 후보를 사람 말로 풀어 준다
#=> 아래 함수의 원래 헤더는 이 블록 위에 그대로 있다(옮기지 않았다).
#------------------------------------------------------------------
def render_other_opinions(rec, final):
    cands = security_candidates(rec)
    # 지금 등급과 다른 값을 주장한 후보만 "다른 의견"이다.
    others = [c for c in cands if norm_grade(c.get("value")) != final]
    if not others:
        return

    # 서열이 위인 등급(더 엄격한 쪽)을 주장한 후보가 있으면 먼저 경고한다.
    rank = {"O": 0, "S": 1, "C": 2, "보류": -1}
    stricter = [c for c in others if rank.get(norm_grade(c.get("value")), -1) > rank.get(final, -1)]
    msg = " · ".join(
        f"{GRADE_LABEL[norm_grade(c.get('value'))]}({W.pct(c.get('confidence'))}, "
        f"{W.SIGNAL_LABEL.get(c.get('from'), c.get('from') or '규칙')})"
        for c in others)
    if stricter:
        st.warning(f"💬 **다른 의견도 있었습니다** — {msg}\n\n"
                   f"더 엄격한 등급을 주장한 근거가 있습니다. 등급을 올려야 하는지 확인해 주세요.")
    else:
        st.info(f"💬 **다른 의견도 있었습니다** — {msg}")


#------------------------------------------------------------------
# 오버라이드 패널 렌더
#=> 선택 문서의 보안등급을 담당자가 '확정'하거나 '수정'하고 저장한다. 이력도 보여준다.
#   버튼은 하나지만 하는 일은 둘이다 — 고른 등급이 지금 등급과 같으면 "자동 판정이
#   맞다"는 확인(kind="confirm", 이유 선택), 다르면 정정(kind="change", 이유 필수)이다.
#   둘 중 무엇이든 한 줄이 남아야 그 문서가 검토함에서 내려간다.
#
# -in: rec       = 분류 레코드
# -in: latest    = 최신 오버라이드 맵
# -in: history   = 파일별 오버라이드 이력
# -in: ov_path   = overrides.jsonl 경로
# -in: reviewer  = 검토자 이름
# -in: scope     = 위젯 key 접두(호출 위치 구분용). st.tabs 는 모든 탭을 한 번에
#                  실행하므로, 같은 문서가 '문서목록'과 '검토큐'에 동시에 그려지면
#                  key 가 충돌한다(StreamlitDuplicateElementKey). 호출부마다 다른
#                  scope("list"/"queue")를 줘서 key 를 유일하게 만든다.
#
# -out: 없음(저장 시 파일 append + rerun)
# -out: error = 없음
#------------------------------------------------------------------
def render_override_panel(rec, latest, history, ov_path, reviewer, scope="list"):
    file = rec.get("file", "")
    cur, _ = effective_grade(rec, latest)

    st.markdown("**등급 정하기**")
    # 보안등급은 문서당 하나뿐이므로 '라디오'로 받는다 — 위젯 모양만으로 "하나만
    # 고르는 축"이라는 것이 전달된다(설계서 원칙 2).
    opts = GRADE_CHOICES + ["보류"]
    new_grade = st.radio("새 등급", opts,
                         index=opts.index(cur) if cur in opts else 0,
                         horizontal=True, key=f"ov_grade_{scope}_{file}",
                         format_func=lambda g: GRADE_LABEL[g], label_visibility="collapsed")

    # 검토함에 오는 문서 대부분은 "판정은 맞는데 확신만 낮은" 문서다. 예전에는
    # 고른 등급이 지금 등급과 같으면 저장을 거절해서("지금 등급과 같습니다"),
    # 판정이 맞다고 본 담당자는 그 문서를 끝낼 방법이 아예 없었다 — 아무리 눌러도
    # 검토함에서 내려가지 않았다. 그래서 버튼 하나가 둘을 다 받는다.
    #   · 값을 바꿨으면  → '고침'(change) · 기계를 뒤집는 것이라 이유 필수
    #   · 그대로면       → '확인'(confirm) · 원래 사람이 하기로 된 절차라 이유 선택
    # 업무분류 쪽 [이대로 확정]과 같은 뜻, 같은 자리다.
    changed = (new_grade != cur)

    reason = st.text_input(
        "바꾸는 이유를 적어 주세요 (기록에 남습니다)" if changed
        else "확정 이유 (선택)",
        key=f"ov_reason_{scope}_{file}",
        placeholder="예: 계약서지만 대외 공개용이라 O 공개로 낮춤" if changed
        else "예: 내용 확인함 — 사번·연락처가 실제로 들어 있음")

    btn = (f"{GRADE_LABEL[cur]} → {GRADE_LABEL[new_grade]} 로 바꾸기" if changed
           else f"{GRADE_LABEL[cur]} 이대로 확정")
    if st.button(btn, key=f"ov_save_{scope}_{file}", type="primary", width="stretch"):
        if not reviewer.strip():
            st.error("화면 오른쪽 위에 '검토자 이름'을 먼저 입력하세요(기록에 남깁니다).")
        elif changed and not reason.strip():
            st.error("바꾸는 이유를 적어 주세요.")
        elif new_grade == "보류":
            # '판단 못 함'을 확정으로 받아 주면 등급 없는 문서가 검토함에서
            # 사라진다 — 그 문서야말로 사람이 봐야 하는 문서다.
            st.error("‘판단 못 함’으로는 확정할 수 없습니다. C·S·O 중에서 골라 주세요.")
        else:
            append_override(ov_path, file, cur, new_grade, reason.strip(),
                            reviewer.strip(), kind="change" if changed else "confirm",
                            doc_id=rec.get("doc_id"), doc_hash=rec.get("hash"))
            st.success(f"바뀌었습니다: {GRADE_LABEL[cur]} → {GRADE_LABEL[new_grade]}"
                       if changed else f"확정됨: {GRADE_LABEL[new_grade]}")
            # 등급을 바꿨고 '문서 파일에 등급 쓰기'가 켜져 있으면 그 파일의 표식도 바로 맞춘다
            # (설계: 사람등급지정 §07). 확인만 한 것은 파일 등급이 이미 같으므로 부르지 않는다.
            if changed and grade_stream_args(st.session_state.get("run_grade_stream", True)):
                _cfg = st.session_state.get("seedcfg", {})
                _state, _why = gerunner.run_set_grade(
                    gerunner.parse_base_cmd(_cfg.get("csoclassify_cmd")) or ["MpowerClassify"],
                    file, new_grade, update_time=bool(st.session_state.get("run_gs_time")),
                    pythonpath=_cfg.get("pythonpath"))
                st.session_state[f"ov_stream_{file}"] = (_state, _why)
            st.rerun()
    # 방금 저장한 결정의 파일 표식 결과(다시 그린 뒤에도 보이게 세션에 둔다)
    _gs = st.session_state.get(f"ov_stream_{file}")
    if _gs:
        if _gs[0] in ("written", "unchanged"):
            st.caption(f"↳ 파일 표식(등급 스트림): **{_gs[0]}** — 탐색기에서 F5 를 누르면 보입니다")
        else:
            st.warning(f"파일 표식을 쓰지 못했습니다({_gs[0]}: {_gs[1]}) — 결정은 기록됐고, "
                       "다음 분류 때 '사람이 정한 등급 따르기'로 다시 씁니다.")
    st.caption("✔ 자동 판정이 맞으면 등급을 그대로 두고 눌러 확정하세요 — "
               "확정해야 검토함에서 내려갑니다. 원래 값은 지워지지 않고 남습니다.")

    hist = docidkey.history_for(history, latest, rec)
    if hist:
        with st.expander(f"결정 기록 {len(hist)}건"):
            for e in reversed(hist):
                og = GRADE_LABEL[norm_grade(e.get("old_grade"))]
                ng = GRADE_LABEL[norm_grade(e.get("new_grade"))]
                # 옛 기록에는 kind 가 없다. 그때는 값이 바뀔 때만 저장됐으므로
                # 값으로 되짚어 판단한다.
                kind = e.get("kind") or ("confirm" if og == ng else "change")
                what = f"{ng} 이대로 확정" if kind == "confirm" else f"{og} → {ng}"
                st.caption(f"- {e.get('ts','')} · {e.get('reviewer','')} · "
                           f"{what} · {e.get('reason','')}")


#------------------------------------------------------------------
# ① 현황 화면 렌더 (설계서 6장)
#=> "지금 무엇을 해야 하는지"를 정하는 화면. 읽는 화면이 아니라 다음 행동을 고르는
#   화면이므로 스크롤 없이 끝나야 한다.
#    1) 숫자 5개(전체 · 둘다 확인 필요 · 보안등급만 · 업무분류만 · 마지막 실행)
#       세 '확인 필요' 칸은 서로 겹치지 않는다 — 더하면 '할 일 있는 문서 수'가 된다
#    2) 두 축 카드를 좌우로 나란히(보안등급 왼쪽 고정)
#
# -in: df         = 문서 표(records_to_df 결과)
# -in: doctype_on = 업무분류 축을 쓰는 배포인가(회사 분류 체계가 연결됐는가)
# -in: run_meta   = 마지막 분류 실행 메타(없으면 None)
#
# -out: 없음(Streamlit 출력)
# -out: error = 없음
#------------------------------------------------------------------
def render_home(df, doctype_on, run_meta=None, low_conf=LOW_CONF):
    total = len(df)
    counts = Counter(df["final"])
    # 세 칸이 서로 겹치지 않게 나눈다 — 같은 문서가 두 칸에 동시에 잡히면 합계가
    # 전체보다 커져 "몇 건을 처리하면 끝나는가"를 셀 수 없다.
    both_n = int((df["need_sec"] & df["need_doc"]).sum())
    sec_only = int((df["need_sec"] & ~df["need_doc"]).sum())
    doc_only = int((~df["need_sec"] & df["need_doc"]).sum())

    m = st.columns(5)
    m[0].metric("전체 문서", f"{total}건")
    m[1].metric("둘 다 확인 필요", f"{both_n}건",
                help="보안등급과 업무분류 양쪽 모두 관리자가 봐야 하는 문서입니다. "
                     "아래 두 칸에는 중복해서 세지 않습니다.")
    m[2].metric("보안등급만 확인 필요", f"{sec_only}건",
                help="판단하지 못했거나 확신이 낮은 문서입니다(업무분류는 문제 없음).")
    if doctype_on:
        m[3].metric("업무분류만 확인 필요", f"{doc_only}건",
                    help="업무분류가 아직 하나도 붙지 않아 관리자가 직접 정해야 하는 "
                         "문서입니다(보안등급은 문제 없음).")
    else:
        m[3].metric("업무분류", "사용 안 함")
    # 마지막 실행은 '언제'가 곧 신뢰도라서 날짜에 시:분까지 붙여 보여준다.
    # 초와 소요시간까지는 필요할 때만 보면 되므로 도움말에 둔다.
    last = (run_meta or {}).get("finished_at")
    m[4].metric("마지막 분류 실행", last[:16] if last else "아직 없음",
                help=(f"{last} · {fmt_duration(run_meta['seconds'])} 소요"
                      if last and run_meta.get("seconds") is not None else "기록이 없습니다"))
    # 위 세 칸을 더한 값이 곧 '검토함에서 처리할 문서 수'라는 것을 한 줄로 알려 준다.
    todo_all = both_n + sec_only + doc_only
    if todo_all:
        seed_drop = int((df["is_seed"] & (df["raw_need_sec"] | df["raw_need_doc"])).sum())
        st.caption(f"→ 관리자 확인이 필요한 문서는 모두 **{todo_all}건**입니다 "
                   f"({both_n} + {sec_only} + {doc_only}). **검토함**에서 처리하세요."
                   + (f" 기준 문서로 등록한 {seed_drop}건은 세지 않았습니다 — "
                      "관리자가 이미 잣대로 정한 문서입니다." if seed_drop else ""))
    else:
        st.caption("→ 관리자 확인이 필요한 문서가 없습니다 👍")

    #--------------------------------------------------------------
    # 보안등급 카드 그리기(좌/우 어느 칸에 놓든 내용은 같아 함수로 뺐다).
    #--------------------------------------------------------------
    def _card_security():
        st.markdown(f"##### {W.AXIS_SEC} "
                    f"<span style='font-weight:400;color:gray'>— {W.AXIS_SEC_SUB}</span>",
                    unsafe_allow_html=True)
        st.caption("문서 1건에 등급 1개. 신호가 엇갈리면 **더 엄격한 쪽**으로 정합니다.")
        dist = pd.DataFrame({"등급": [GRADE_LABEL[g] for g in GRADE_ORDER],
                             "건수": [counts.get(g, 0) for g in GRADE_ORDER]}).set_index("등급")
        # 카드 가장자리에 막대가 닿으면 답답해 보인다. 양옆에 빈 칸을 둬서
        # 그래프를 가운데로 좁힌다(그래프 자체는 화면 폭을 따라 늘어난다).
        _pad_l, _mid, _pad_r = st.columns([1, 12, 1])
        with _mid:
            st.bar_chart(dist, horizontal=True, height=190)
        hold_n = int((df["raw_need_sec"] & (df["final"] == "보류")).sum())
        low_n = int((df["raw_need_sec"] & (df["final"] != "보류")).sum())
        # 기준 문서로 올린 문서는 큐에서 뺀다. 빼고 나면 '판단 못 함 + 확신 낮음'과
        # 숫자가 안 맞는데, 그 이유를 안 적으면 화면이 틀린 것처럼 보인다.
        drop_n = int((df["is_seed"] & df["raw_need_sec"]).sum())
        st.info(f"확인이 필요한 문서 **{both_n + sec_only}건** — 판단 못 함 "
                f"{hold_n}건 + 확신 {int(low_conf * 100)}% 미만 {low_n}건"
                + (f" · 기준 문서 {drop_n}건은 뺐습니다" if drop_n else ""))

    #--------------------------------------------------------------
    # 업무분류 카드 그리기. 분류마다 색을 나누지 않고 전부 같은 막대로 둔다 —
    # 색을 나누면 없는 서열이 생긴다(설계서 원칙 3).
    #--------------------------------------------------------------
    def _card_doctype():
        st.markdown(f"##### {W.AXIS_DOC} "
                    f"<span style='font-weight:400;color:gray'>— {W.AXIS_DOC_SUB}</span>",
                    unsafe_allow_html=True)
        st.caption("문서 1건에 분류 **여러 개** 가능. 문서자동분류도우미가 제안하고 "
                   "**관리자가 최종 확정**합니다.")
        # 대분류(뿌리)로 롤업해 5~6줄로 보여준다. 잎 22줄을 그대로 내면 안 읽힌다.
        roll = Counter()
        for roots in df["dt_roots"]:
            for r in (roots or "").split():
                if r:
                    roll[r] += 1
        none_n = int((df["dt_n"] == 0).sum())
        if roll:
            rdf = pd.DataFrame({"분류": list(roll.keys()),
                                "건수": list(roll.values())}).set_index("분류")
            _pad_l, _mid, _pad_r = st.columns([1, 12, 1])
            with _mid:
                st.bar_chart(rdf.sort_values("건수", ascending=False),
                             horizontal=True, height=190)
            # 위 지표(업무분류 확인 필요)와 이 차트의 '분류 없음'은 세는 기준이
            # 다르다. 두 가지가 갈린다 — ① 사람이 '해당 없음'으로 정리한 문서는
            # 지표에서 빠지고 ② 기준 문서도 빠진다. 그래서 두 숫자가 다를 수 있고,
            # 다른 이유를 여기서 밝힌다. 파일 이름만 보고 붙은 약한 라벨은 분류된
            # 문서로 세되(검토함에 안 올림), 몇 건인지는 따로 알린다.
            done_n = int((df["dt_n"] == 0).sum() - (df["raw_need_doc"] & (df["dt_n"] == 0)).sum())
            weak_n = int(df["dt_weak_only"].sum())
            drop_n = int((df["is_seed"] & df["raw_need_doc"]).sum())
            bits = []
            if done_n:
                bits.append(f"그중 {done_n}건은 ‘해당 없음’으로 정리됨")
            if weak_n:
                bits.append(f"파일 이름만 보고 붙인 분류 {weak_n}건은 분류된 것으로 셈")
            if drop_n:
                bits.append(f"기준 문서 {drop_n}건은 확인 대상에서 뺌")
            st.caption(f"아직 분류 없음 {none_n}건"
                       + (" (" + " · ".join(bits) + ")" if bits else "")
                       + " · 합계가 전체 문서 수보다 큰 것은 "
                       "**정상**입니다 — 한 문서가 여러 분류에 해당할 수 있습니다.")
        else:
            st.caption("아직 업무분류가 붙은 문서가 없습니다.")

    c1, c2 = st.columns(2)
    # 보안등급을 항상 왼쪽에 둔다 — 덜 보호하는 실수가 더 비싸므로 화면에서도
    # 보안이 앞선다(상위 설계 2-1). 두 카드는 늘 함께 보인다.
    left, right = (_card_security, _card_doctype)
    with c1:
        with st.container(border=True):
            left()
    with c2:
        with st.container(border=True):
            right()

    ov_n = int(df["overridden"].sum())
    if ov_n:
        st.caption(f"관리자가 등급을 고친 문서: {ov_n}건")


#------------------------------------------------------------------
# ② 문서함 화면 렌더 (설계서 7장)
#=> 모든 문서를 한 표에서 찾는 화면. 이 표의 존재 이유는 "한 행에 두 축이 같이
#   있다"는 것이다 — 예전처럼 보안등급 목록과 업무분류 목록이 다른 탭에 있으면
#   "계약서인데 O 공개로 잡힌 문서" 같은 교차 질문에 답할 수 없다.
#    1) 필터 4개(검색 · 보안등급 · 업무분류 대분류 · 할 일 상태)를 항상 같이 노출
#    2) 관점에 따라 기본 정렬만 바꾼다(보이는 열은 그대로 — 설계서 5-1)
#    3) 행을 고르면 바로 아래에 상세를 펼친다(새 창으로 보내지 않는다)
#
# -in: df        = 문서 표
# -in: records   = 원본 레코드(선택 문서의 상세용)
# -in: latest    = 보안등급 수정 맵
# -in: history   = 보안등급 수정 이력
# -in: latest_dt = 업무분류 결정 맵
# -in: history_dt= 업무분류 결정 이력
# -in: ov_path   = cso_override.jsonl 경로
# -in: reviewer  = 검토자 이름
# -in: tax       = 회사 분류 체계(없으면 None)
#
# -out: 없음(Streamlit 출력)
# -out: error = 없음
#------------------------------------------------------------------
def render_docbox(df, records, latest, history, latest_dt, history_dt,
                  ov_path, reviewer, tax):
    rec_by_file = {r.get("file"): r for r in records}
    doctype_on = tax is not None

    # 보안등급 칸은 네 등급이 한 줄에 들어갈 만큼 넓게 잡는다 — 좁으면 칩이
    # 두 줄로 접혀 그 아래 표까지 밀린다.
    f1, f2, f3, f4 = st.columns([3, 4, 3, 2])
    # 라벨을 감추면 이 칸만 다른 필터보다 한 줄 위로 올라가 줄이 안 맞는다.
    # 다른 칸과 같은 자리에 라벨을 세워 네 칸의 위아래를 맞춘다.
    kw = f1.text_input("🔎 파일명 검색", key="box_kw",
                       placeholder="예: 계약서, 인사",
                       help="파일 이름과 폴더 경로를 함께 찾습니다")
    grades = f2.multiselect(W.AXIS_SEC, GRADE_ORDER, default=GRADE_ORDER,
                            format_func=lambda g: W.GRADE_PLAIN[g], key="box_grade")
    root_opts = taxlib.roots(tax) if doctype_on else []
    picked_roots = f3.multiselect(W.AXIS_DOC, root_opts, default=[], key="box_root",
                                  disabled=not root_opts,
                                  help="대분류로 좁힙니다(비워 두면 전체)")
    todo_opts = ["전체", "확인 필요만", "확정된 것만"]
    # 관점이 정해져 있으면 그 축의 '할 일'을 기본으로 보여준다 — 스위치가 바꾸는
    # 것은 기본 필터·정렬뿐이고, 열이나 데이터는 그대로다.
    todo = f4.selectbox("상태", todo_opts, index=0, key="box_todo")

    # 등급 칩을 모두 지운 것은 "아무것도 보지 않겠다"가 아니라 "등급으로는 안
    # 거르겠다"는 뜻으로 받는다 — 업무분류 칸이 비었을 때 전체가 나오는 것과 같은
    # 규칙이다. (예전에는 여기서 0건이 되어 화면이 통째로 비었다.)
    v = df[df["final"].isin(grades or GRADE_ORDER)]
    if kw.strip():
        v = v[v["file"].str.lower().str.contains(kw.strip().lower())]
    if picked_roots:
        # astype(bool) 을 빼면 안 된다. 걸러진 결과가 0건일 때 apply 가 object 형
        # 빈 Series 를 돌려주고, 그것으로 색인하면 pandas 가 '열까지' 지운 빈 표를
        # 만든다 → 뒤따르는 정렬이 KeyError('need_sec') 로 죽는다.
        mask = v["dt_roots"].apply(
            lambda s: any(r in (s or "").split() for r in picked_roots)).astype(bool)
        v = v[mask]
    if todo == "확인 필요만":
        v = v[v["need_sec"] | v["need_doc"]]
    elif todo == "확정된 것만":
        v = v[~(v["need_sec"] | v["need_doc"])]

    # 기본 정렬 — 할 일이 남은 문서를 위로. 두 축을 함께 보므로 어느 한쪽 기준으로
    # 줄 세우지 않는다(정렬은 표 머리를 눌러 바꿀 수 있다).
    if not v.empty:
        v = v.sort_values(["need_sec", "need_doc"], ascending=[False, False])

    top1, top2 = st.columns([4, 1])
    top1.caption(f"전체 {len(df)}건 중 **{len(v)}건**")
    # 내보내기는 탭 하나를 쓸 만큼 큰 기능이 아니다 — 지금 필터 결과 그대로 낸다.
    with top2:
        render_export_button(v)

    if v.empty:
        st.info("조건에 맞는 문서가 없습니다. 위 필터를 비우면 전체가 나옵니다.")
        return

    show = v[["is_seed", "name", "final", "confidence", "doctype", "todo", "file"]].copy()
    # confidence 는 0.0~1.0 으로 저장돼 있다. 여기서 100을 곱하지 않으면 0.9 가
    # "%.0f%%" 서식을 만나 "1%" 로 찍힌다 — 확신 90% 문서가 1% 로 보이던 원인.
    show["confidence"] = show["confidence"] * 100
    # 기준 문서로 올린 문서를 목록 맨 앞에서 바로 알아보게 한다. 예전에는 문서를
    # 고르고 접힌 칸을 펼쳐야만 등록 여부를 알 수 있어, 같은 문서를 또 올리기 쉬웠다.
    show["is_seed"] = show["is_seed"].map(lambda x: "🌱" if x else "")
    show = show.rename(columns={"is_seed": "seed"})
    event = st.dataframe(
        show, hide_index=True, width="stretch", height=380,
        # 여러 줄 선택 — 한 건이면 상세를, 여러 건이면 '한꺼번에 등록'을 보여준다.
        # 기준 문서를 하나씩 등록하는 것이 번거롭다는 요청에서 나온 설계다.
        on_select="rerun", selection_mode="multi-row",
        column_config={
            "seed": st.column_config.TextColumn(
                "기준", width=60,
                help="🌱 은 '기준 문서'로 등록된 문서입니다 — 다음 분류 때 잣대가 되고, "
                     "검토함에는 올라오지 않습니다"),
            "name": st.column_config.TextColumn("문서", width="large"),
            # 값이 한두 글자뿐이라 픽셀로 좁게 못박는다 — 남는 폭은 문서·업무분류에.
            "final": st.column_config.TextColumn(W.AXIS_SEC, width=90),
            "confidence": st.column_config.NumberColumn("확신", format="%.0f%%",
                                                        width=70),
            "doctype": st.column_config.TextColumn(W.AXIS_DOC, width="medium"),
            "todo": st.column_config.TextColumn("상태", width="small"),
            "file": st.column_config.TextColumn("폴더", width="medium"),
        },
    )
    st.caption("업무분류는 자동으로 찾은 것도 **확정으로 봅니다** — 틀린 것이 있으면 "
               "그 문서를 골라 고치세요.")

    rows = event.selection.rows if event and event.selection else []
    if not rows:
        st.info("표에서 문서를 **한 건** 고르면 자세한 내용과 결정 화면이, "
                "**여러 건**을 고르면 기준 문서로 한꺼번에 등록하는 화면이 나옵니다.")
        return

    # 여러 건을 골랐으면 상세를 그릴 수 없다(다섯 건의 상세를 동시에 펼칠 수는
    # 없다). 대신 그 문서들을 한 번에 기준 문서로 올리는 화면을 보여준다.
    if len(rows) > 1:
        st.divider()
        render_bulk_seed_promote([show.iloc[i]["file"] for i in rows],
                                 rec_by_file, latest, latest_dt, tax)
        return

    sel_file = show.iloc[rows[0]]["file"]
    rec = rec_by_file.get(sel_file)
    if rec:
        st.divider()
        render_detail(rec, latest, history, latest_dt, history_dt,
                      ov_path, reviewer, tax, scope="box")


#------------------------------------------------------------------
# 무엇을 등록할지 먼저 계산한다(화면 없는 순수 계산)
#=> "고른 문서들을 기준 문서로 올리면 무슨 일이 벌어지는가"를 표로 보여 주는 것과
#   실제로 올리는 것이 같은 계산을 쓰게 하려고 함수로 떼어냈다. 화면에 보여 준
#   것과 실제 등록이 어긋나는 사고를 구조적으로 막는다.
#    1) 보안등급은 사람이 고친 값(오버라이드)을 우선한다
#    2) 업무분류는 '확정된 것'만 올린다 — 자동 제안(?)은 기준이 될 수 없다
#    3) 이번에 확정 분류가 없어도 예전에 등록해 둔 분류는 지우지 않는다
#    4) 등급도 확정 분류도 없으면 올릴 것이 없으므로 이유와 함께 뺀다
#
# -in: files       = 고른 문서 경로 목록
# -in: rec_by_file = 파일 경로 → 분류 레코드
# -in: latest      = 보안등급 수정 맵
# -in: latest_dt   = 업무분류 결정 맵
# -in: seeds       = 지금 기준 문서 목록(새로 등록인지 갱신인지 판단에 쓴다)
# -in: tax         = 회사 분류 체계(직접 추가한 분류를 알아보는 데 쓴다)
#
# -out: list = [{file, rec, grade, dc_ids, how}] — how 는 "새로 등록"/"갱신"/
#        "빠짐 — 정한 것이 없음"
# -out: error = 없음
#------------------------------------------------------------------
def plan_bulk_seed(files, rec_by_file, latest, latest_dt, seeds, tax=None):
    plan = []
    for f in files:
        rec = rec_by_file.get(f) or {}
        grade, _ = effective_grade(rec, latest)
        cands, _ = doctype_review.effective_doctype(rec, latest_dt, tax)
        dc_ids = [c["dc_id"] for c in cands if c["status"] == "confirmed"]
        prev = seedstore.find_seed(seeds, f)
        has_grade = grade in ("C", "S", "O")
        # 이번에 확정 분류가 없더라도 예전에 등록해 둔 분류는 살려 둔다.
        if not dc_ids and prev:
            dc_ids = list(seedstore.axis_value(prev, "doctype") or [])
        if not has_grade and not dc_ids:
            how = BULK_SKIP
        else:
            how = "갱신" if prev else "새로 등록"
        plan.append({"file": f, "rec": rec, "grade": grade if has_grade else None,
                     "dc_ids": dc_ids, "how": how})
    return plan


#------------------------------------------------------------------
# 계산한 대로 실제로 등록한다(화면 없는 순수 적용)
#=> plan_bulk_seed 가 정한 것을 그대로 저장소에 얹는다. 임베딩만 바깥에서
#   주입받으므로(embed 함수), 테스트에서는 가짜 벡터로 전 과정을 검증할 수 있다.
#    1) 벡터가 없으면 embed 로 그 문서만 구한다 — 실패하면 그 문서만 건너뛴다
#    2) 지금까지 쌓인 것과 사실상 같은 사본이면 건너뛴다(force 면 그래도 등록)
#    3) 축마다 따로 얹고, 축마다 따로 감사 기록을 남긴다
#
# -in: plan        = plan_bulk_seed 결과
# -in: seeds       = 기존 기준 문서 목록(변형하지 않는다)
# -in: reviewer    = 승인자 이름
# -in: audit_path  = 감사 로그 경로(None 이면 기록하지 않는다)
# -in: force       = True 면 사본이어도 등록
# -in: embed       = 벡터 없는 문서를 임베딩하는 함수 f(path) -> list|None
# -in: dup_sim     = 사본으로 볼 유사도(기본 SEED_DUP_SIM)
# -in: on_progress = 진행 알림 콜백 f(끝난수, 전체, 경로)(없어도 된다)
#
# -out: (new_seeds, stats) = 갱신된 목록, {"new","update","dup","fail"}
# -out: error = 없음(개별 실패는 stats.fail 로 센다)
#------------------------------------------------------------------
def apply_bulk_seed(plan, seeds, reviewer, audit_path=None, force=False,
                    embed=None, dup_sim=SEED_DUP_SIM, on_progress=None):
    todo = [x for x in plan if x["how"] != BULK_SKIP]
    cur = list(seeds)
    stats = {"new": 0, "update": 0, "dup": 0, "fail": 0}

    def audit(action, file, axis, before, after, reason):
        if audit_path:
            seedstore.append_seed_audit(audit_path, action, file, axis=axis,
                                        before=before, after=after,
                                        reviewer=reviewer, reason=reason)

    for i, x in enumerate(todo):
        f = x["file"]
        if on_progress:
            on_progress(i, len(todo), f)
        v = x["rec"].get("vector")
        if v is None and embed:
            v = embed(f)                  # 실패 이유는 embed 쪽에서 화면에 표시한다
        if v is None:
            stats["fail"] += 1
            continue
        # 사본 검사는 '지금까지 쌓인 것'과 한다 — 이번에 함께 고른 문서끼리 사본이면
        # 뒤엣것이 걸러진다(사본이 쌓이면 그 문서가 전파에서 표를 두 번 던진다).
        sim, _near = seedstore.nearest_seed_sim(v, cur, exclude_file=f)
        if sim >= dup_sim and not force:
            stats["dup"] += 1
            continue
        doc_meta = seed_doc_meta(x["rec"])
        prev = seedstore.find_seed(cur, f)
        if x["grade"]:
            before = seedstore.axis_value(prev, "security") if prev else None
            cur = seedstore.set_axis(cur, f, "security", x["grade"], reviewer,
                                     source="phase4", vector=v, doc=doc_meta)
            audit("update" if before else "add", f, "security", before, x["grade"],
                  f"문서함 일괄 등록 sim={sim:.3f}")
        if x["dc_ids"]:
            before = list(seedstore.axis_value(prev, "doctype") or []) if prev else None
            cur = seedstore.set_axis(cur, f, "doctype", x["dc_ids"], reviewer,
                                     source="phase6", vector=v, doc=doc_meta)
            audit("update" if before else "add", f, "doctype", before, x["dc_ids"],
                  "문서함 일괄 등록")
        stats["update" if prev else "new"] += 1
    if on_progress:
        on_progress(len(todo), len(todo), "")
    return cur, stats


#------------------------------------------------------------------
# 고른 문서들을 한꺼번에 기준 문서로 등록 (문서함 여러 줄 선택)
#=> 기준 문서를 한 건씩 올리는 것이 번거롭다는 요청에서 나온 화면이다. 문서함에서
#   여러 줄을 고르면 그 문서들의 보안등급·확정 업무분류를 그대로 한 번에 올린다.
#   판단(plan_bulk_seed)과 적용(apply_bulk_seed)은 위 두 함수가 하고, 여기서는
#   보여 주고 눌러 주는 일만 한다.
#
# -in: files       = 고른 문서 경로 목록
# -in: rec_by_file = 파일 경로 → 분류 레코드
# -in: latest      = 보안등급 수정 맵
# -in: latest_dt   = 업무분류 결정 맵
# -in: tax         = 회사 분류 체계(분류 이름 표기용)
#
# -out: 없음(등록 시 저장 + 감사 기록 + rerun)
# -out: error = 없음(실패는 화면에 이유와 함께 표시)
#------------------------------------------------------------------
def render_bulk_seed_promote(files, rec_by_file, latest, latest_dt, tax=None):
    cfg = st.session_state.get("seedcfg", {})
    seeds = seedstore.load_seeds(cfg.get("seed_path"))
    plan = plan_bulk_seed(files, rec_by_file, latest, latest_dt, seeds, tax)

    st.markdown(f"### 🌱 고른 문서 {len(files)}건을 기준 문서로 등록")
    # 등록 결과는 st.rerun() 을 지나 살아남아야 한다. 화면을 다시 그려야 표의
    # 🌱 표시가 갱신되는데, rerun 하면 그 직전에 띄운 성공 문구가 함께 지워진다
    # — 그래서 결과를 session_state 에 맡겨 두고 다시 그린 화면에서 보여준다.
    flash = st.session_state.pop("bulkseed_flash", None)
    if flash:
        st.success(flash)
    st.caption("기준 문서는 다음 분류 때 '비슷한 문서 참고'의 잣대가 됩니다 — "
               "확실한 문서만 올리세요. 등급이나 업무분류가 아직 정해지지 않은 "
               "문서는 자동으로 빠집니다.")

    st.dataframe(
        pd.DataFrame([{"문서": os.path.basename(x["file"]),
                       W.AXIS_SEC: W.GRADE_PLAIN[x["grade"]] if x["grade"] else "—",
                       W.AXIS_DOC: (" · ".join(taxlib.path_of(tax, d) for d in x["dc_ids"])
                                    if x["dc_ids"] else "—"),
                       "처리": x["how"]} for x in plan]),
        hide_index=True, width="stretch")

    todo = [x for x in plan if x["how"] != BULK_SKIP]
    skipped = len(plan) - len(todo)
    if skipped:
        st.caption(f"↳ {skipped}건은 보안등급도 확정 업무분류도 없어 등록에서 빠집니다 "
                   "— 먼저 검토함에서 정해 주세요.")
    if not todo:
        st.info("등록할 수 있는 문서가 없습니다.")
        return
    n_novec = sum(1 for x in todo if not x["rec"].get("vector"))
    if n_novec:
        st.caption(f"↳ {n_novec}건은 벡터가 없어(빠른 분류로 생략) 등록할 때 그 문서만 "
                   "임베딩합니다 — 건수만큼 시간이 걸립니다.")

    force = st.checkbox("이미 있는 기준 문서와 거의 같아도 등록",
                        key="bulkseed_force",
                        help=f"닮은 정도 {W.pct(SEED_DUP_SIM)} 이상이면 사본으로 보고 "
                             "건너뜁니다. 사본이 쌓이면 그 문서가 표를 두 번 던집니다")

    if not st.button(f"🌱 {len(todo)}건 기준 문서로 등록", type="primary",
                     key="bulkseed_go"):
        return
    if not cfg.get("reviewer", "").strip():
        st.error("화면 오른쪽 위에 '검토자 이름'을 먼저 입력하세요.")
        return

    prog = st.progress(0.0, text=f"등록 중… 0/{len(todo)}")

    # 진행바 갱신 — 임베딩이 느릴 수 있어 지금 어느 문서인지까지 보여준다.
    def _on_prog(done, total, path):
        txt = f"등록 중… {done}/{total}"
        if path:
            txt += f" · {os.path.basename(path)}"
        prog.progress(done / total if total else 1.0, text=txt)

    # 벡터가 없는 문서는 '한 번의 실행'으로 몰아서 임베딩한다. 문서마다 따로 돌리면
    # 임베딩 모델을 문서 수만큼 다시 읽는다 — 실측에서 6건에 6.5초 vs 1.1초(5.7배)였다.
    need = [x["file"] for x in todo if not x["rec"].get("vector")]
    ready = {}
    if need:
        prog.progress(0.0, text=f"벡터가 없는 {len(need)}건을 한 번에 임베딩 중…")
        try:
            ready = gerunner.embed_files(
                gerunner.parse_base_cmd(cfg.get("csoclassify_cmd")), need,
                pythonpath=cfg.get("pythonpath"),
                on_progress=lambda d, t, p: _on_prog(d, t, p))
        except Exception as e:
            uierrlog.show_error(f"임베딩 실패: {e}", exc=e, where="기준 문서 임베딩")

    cur, stats = apply_bulk_seed(
        plan, seeds, cfg["reviewer"].strip(), audit_path=cfg.get("audit_path"),
        force=force, embed=ready.get, on_progress=_on_prog)
    prog.progress(1.0, text="등록 완료")

    parts = [f"새로 등록 {stats['new']}건", f"갱신 {stats['update']}건"]
    if stats["dup"]:
        parts.append(f"사본이라 건너뜀 {stats['dup']}건")
    if stats["fail"]:
        parts.append(f"벡터 실패 {stats['fail']}건")
    if stats["new"] or stats["update"]:
        try:
            seedstore.save_seeds(cfg["seed_path"], cur)
        except OSError as e:
            uierrlog.show_error(f"기준 문서 저장 실패: {e}", exc=e, where="기준 문서 저장")
            return
        # BM25 재료인 제목(첫 줄)을 저장 직후 채운다 — 화면은 본문을 갖고 있지 않다
        with st.spinner("기준 문서 제목(첫 줄)을 채우는 중…"):
            tmsg = fill_titles_after_save(cfg)
        if tmsg:
            parts.append(tmsg)
        st.session_state["bulkseed_flash"] = (
            " · ".join(parts) + f" · 저장 위치: {cfg['seed_path']}")
        st.rerun()
    else:
        st.info(" · ".join(parts) + " — 저장할 것이 없었습니다.")


#------------------------------------------------------------------
# 문서 상세 렌더 — 두 축을 좌우로 나란히 (설계서 8장)
#=> 이 설계에서 가장 중요한 화면. 한 문서의 보안등급과 업무분류를 '동시에' 보여
#   준다. 위아래로 두면 아래 카드는 스크롤해야 보여서 결국 두 번 보는 예전 문제로
#   돌아가므로, 반드시 가로로 나란히 둔다(좁은 화면에서는 Streamlit 이 알아서 쌓는다).
#   왼쪽은 라디오(하나만), 오른쪽은 체크박스(여러 개) — 위젯 모양이 축의 성격을
#   말해 준다(설계서 원칙 2).
#
# -in: rec        = 분류 레코드
# -in: latest     = 보안등급 수정 맵
# -in: history    = 보안등급 수정 이력
# -in: latest_dt  = 업무분류 결정 맵
# -in: history_dt = 업무분류 결정 이력
# -in: ov_path    = cso_override.jsonl 경로
# -in: reviewer   = 검토자 이름
# -in: tax        = 회사 분류 체계(None 이면 업무분류 칸을 '사용 안 함'으로)
# -in: scope      = 위젯 key 접두(문서함/검토함에서 같은 문서를 그릴 때 충돌 방지)
#
# -out: 없음(Streamlit 출력)
# -out: error = 없음
#------------------------------------------------------------------
def render_detail(rec, latest, history, latest_dt, history_dt,
                  ov_path, reviewer, tax, scope="box"):
    file = rec.get("file", "")
    h1, h2 = st.columns([5, 1])
    h1.markdown(f"#### 📄 {os.path.basename(file)}")
    h1.caption(file)
    # 원본 미리보기는 만들지 않는다 — 기본 프로그램에 맡긴다(설계서 15장 비목표).
    h2.caption("원본은 탐색기에서 열어 확인하세요")

    def _panel_security():
        with st.container(border=True):
            render_evidence(rec, latest)
            st.divider()
            render_override_panel(rec, latest, history, ov_path, reviewer, scope=scope)

    def _panel_doctype():
        with st.container(border=True):
            render_doctype_panel(rec, latest_dt, history_dt, ov_path, reviewer,
                                 tax, scope=scope)

    c1, c2 = st.columns(2)
    left, right = (_panel_security, _panel_doctype)
    with c1:
        left()
    with c2:
        right()

    # 기준 문서 등록은 두 축 아래에 '하나'만 둔다. 예전에는 보안등급 칸과 업무분류
    # 칸에 따로 있었는데, 저장소(class_seed.jsonl)에는 어차피 문서 하나가 한 줄로
    # 들어간다 — 버튼이 둘이면 "둘 다 눌러야 하나"를 매번 고민하게 된다.
    cands, _ = doctype_review.effective_doctype(rec, latest_dt, tax)
    confirmed_dc_ids = [c["dc_id"] for c in cands if c["status"] == "confirmed"]
    # 접힌 칸 제목만 보고도 등록 여부를 알 수 있게 한다(펼쳐야 알면 또 누르게 된다).
    _seeded = seedstore.norm_file(file) in seedstore.load_seed_files(
        st.session_state.get("seedcfg", {}).get("seed_path"))
    with st.expander("🌱 기준 문서로 등록됨 — 내용 갱신" if _seeded
                     else "🌱 이 문서를 '기준 문서'로 등록"):
        render_seed_promote(rec, effective_grade(rec, latest)[0],
                            confirmed_dc_ids, tax, scope=scope)


#------------------------------------------------------------------
# ③ 검토함 화면 렌더 (설계서 9장)
#=> "오늘 할 일"만 한 건씩 처리하는 화면. 찾는 화면이 아니라 처리하는 화면이라
#   검색·필터를 두지 않고, 큐의 맨 앞 한 건만 크게 보여준다.
#   관점이 바꾸는 것은 '누가 큐에 담기는가'와 '무엇을 눌러 끝내는가' 둘이다.
#     · 보안등급 관점 — 판단 못 함·저확신 문서 → 등급 버튼 3개 중 하나로 끝
#     · 업무분류 관점 — 확정 안 된 제안이 남은 문서 → 체크하고 확정으로 끝
#     · 전체 관점    — 둘 중 하나라도 할 일이 남은 문서
#    1) 진행률(처리한 건수/전체)을 위에 두어 끝이 보이게 한다
#    2) 맨 앞 한 건은 근거까지 펼쳐서, 나머지는 접어서 보여준다
#    3) '나중에'는 이 문서를 이번 목록의 뒤로 미룬다(저장하지 않는다)
#
# -in: df         = 문서 표
# -in: records    = 원본 레코드
# -in: latest     = 보안등급 수정 맵
# -in: history    = 보안등급 수정 이력
# -in: latest_dt  = 업무분류 결정 맵
# -in: history_dt = 업무분류 결정 이력
# -in: ov_path    = cso_override.jsonl 경로
# -in: reviewer   = 검토자 이름
# -in: tax        = 회사 분류 체계(None 이면 업무분류 큐는 비어 있다)
#
# -out: 없음(Streamlit 출력)
# -out: error = 없음
#------------------------------------------------------------------
def render_inbox(df, records, latest, history, latest_dt, history_dt,
                 ov_path, reviewer, tax):
    rec_by_file = {r.get("file"): r for r in records}

    # 두 축을 함께 처리한다 — 한 문서를 열었을 때 등급과 분류를 한 번에 끝내는 편이
    # 같은 문서를 두 번 여는 것보다 낫다. 양쪽 다 봐야 하는 문서를 맨 앞에 둔다.
    queue = df[df["need_sec"] | df["need_doc"]].copy()
    queue["_both"] = queue["need_sec"] & queue["need_doc"]
    queue = queue.sort_values(["_both", "need_sec", "confidence"],
                              ascending=[False, False, True]).drop(columns="_both")
    n_both = int((df["need_sec"] & df["need_doc"]).sum())
    n_sec = int((df["need_sec"] & ~df["need_doc"]).sum())
    n_doc = int((~df["need_sec"] & df["need_doc"]).sum())
    head = (f"**할 일이 남은 문서 {len(queue)}건** — "
            f"둘 다 {n_both}건 · 보안등급만 {n_sec}건 · 업무분류만 {n_doc}건")

    # 이번 화면에서 '나중에'로 미룬 문서는 뒤로 보낸다(저장하지 않는 임시 순서).
    later = st.session_state.setdefault("later", [])
    if later:
        queue = pd.concat([queue[~queue["file"].isin(later)],
                           queue[queue["file"].isin(later)]])

    if queue.empty:
        st.success("오늘 검토할 문서가 없습니다 👍")
        if st.button("현황으로 돌아가기", key="inbox_home"):
            goto(menu=MENU_HOME)
        return

    total_axis = int((df["need_sec"] | df["need_doc"]).sum())
    st.markdown(head)
    st.progress(0.0 if not total_axis else max(0.0, 1 - len(queue) / max(total_axis, 1)),
                text=f"남은 {len(queue)}건")

    files = list(queue["file"])[:30]
    for i, f in enumerate(files):
        rec = rec_by_file.get(f)
        if not rec:
            continue
        row = queue[queue["file"] == f].iloc[0]
        label = (f"{i + 1} / {len(queue)} · {os.path.basename(f)} · "
                 f"{GRADE_LABEL[row['final']]} {W.pct(row['confidence'])} · {row['doctype']}")
        # 맨 앞 한 건만 펼친다 — 한 번에 한 건씩 처리하게 하려는 것이다.
        with st.expander(label, expanded=(i == 0)):
            st.caption(f)
            render_detail(rec, latest, history, latest_dt, history_dt,
                          ov_path, reviewer, tax, scope="inbox")
            if st.button("나중에 보기", key=f"later_{f}"):
                if f not in later:
                    later.append(f)
                st.rerun()

    if len(queue) > 30:
        st.caption(f"…외 {len(queue) - 30}건. 위에서부터 처리하면 계속 채워집니다.")


#------------------------------------------------------------------
# 업무분류 후보 하나의 근거 렌더
#=> "왜 이 분류가 제안됐는가"를 신호별로 한 줄씩, 기여도와 함께 보여준다.
#   [설계서 원칙 4] 접지 않는다 — 접어 두면 아무도 열지 않고, 그러면 결과만
#   보고 확정하게 된다. 근거를 보고 판단하라고 만든 화면이므로 펼쳐 둔다.
#   문장 만들기는 doctype_review.reason_block() 이 한다 — 이 함수는 그리기만
#   한다. 그래야 문장 쪽을 Streamlit 없이 테스트할 수 있다.
#
# -in: cand = effective_doctype() 이 낸 후보 dict
#
# -out: 없음(Streamlit 위젯 출력)
# -out: error = 없음
#------------------------------------------------------------------
def render_doctype_evidence(cand):
    block = doctype_review.reason_block(cand)

    for ln in block["lines"]:
        c = ln["conf"]
        tail = f"　<span style='color:gray'>기여 {W.pct(c)}</span>" if c is not None else ""
        st.markdown(f"　· {ln['text']}{tail}", unsafe_allow_html=True)

    if block["summary"]:
        st.caption(f"　{block['summary']}")
    if block["warn"]:
        st.warning(block["warn"])


#------------------------------------------------------------------
# doctype 후보 확정/거절 패널 렌더
#=> 선택 문서의 업무분류 후보를 하나씩 제안(보류)/확정/거절로 고르게 하고,
#   저장하면 문서당 한 줄(axis:"doctype")로 cso_override.jsonl 에 남긴다.
#   원본 레코드(labels.doctype)는 절대 고치지 않는다 — 결정은 오버라이드
#   파일에만 쌓인다(render_override_panel 과 같은 append-only 원칙, 설계서 7-4-1).
#
# -in: rec        = 분류 레코드
# -in: latest_dt  = doctype_review.load_doctype_overrides() 의 latest
# -in: history_dt = doctype_review.load_doctype_overrides() 의 history
# -in: ov_path    = cso_override.jsonl 경로(security 오버라이드와 같은 파일 — axis 로 구분)
# -in: reviewer   = 검토자 이름
# -in: scope      = 위젯 key 접두(호출 위치 구분용 — render_override_panel 과 같은 이유로
#                   같은 문서가 여러 곳에 동시에 그려질 때 key 충돌을 막는다)
#
# -out: 없음(저장 시 파일 append + rerun)
# -out: error = 없음
#------------------------------------------------------------------
def render_doctype_panel(rec, latest_dt, history_dt, ov_path, reviewer, tax, scope="doctype"):
    file = rec.get("file", "")
    candidates, reviewed = doctype_review.effective_doctype(rec, latest_dt, tax)

    n_conf = sum(1 for c in candidates if c["status"] == "confirmed")
    n_auto = sum(1 for c in candidates if c.get("auto"))
    # 확정도 거절도 아닌 채 사람을 기다리는 후보(파일 이름 핵어로만 붙은 약한 라벨).
    n_wait = sum(1 for c in candidates if c["status"] == "proposed")
    # 자동 확정은 '확정'이되 사람이 아직 확인하지 않은 확정이다. 그 사실을 배지에
    # 남긴다 — 나중에 "이 분류 누가 정했어?" 를 화면만 보고 답할 수 있어야 한다.
    if reviewed and n_conf:
        badge = "확정됨"
    elif reviewed:
        badge = "해당 없음으로 확정"
    elif n_wait and not n_auto:
        # 약한 라벨만 있는 문서 — 확정된 것이 하나도 없다는 뜻이다.
        badge = "확인 필요 · 자동 확정하지 않음"
    elif n_auto:
        badge = "자동 확정 · 관리자 확인 전" + (f" · 확인 필요 {n_wait}" if n_wait else "")
    else:
        badge = "확정 대기"
    st.markdown(f"##### {W.AXIS_DOC} <span style='font-weight:400;color:gray'>— "
                f"{W.AXIS_DOC_SUB}</span>", unsafe_allow_html=True)
    tail = ("" if not n_wait else
            " · **확인 필요** 로 표시된 것은 파일 이름만 보고 제안한 것이라 "
            "확정하지 않았습니다 — 내용을 보고 정해 주세요")
    st.caption(f"{badge} · 문서당 **여러 개 가능** · 자동으로 찾은 분류는 "
               "**확정된 것으로 봅니다** — 틀린 것만 체크를 풀고 [이대로 확정] 하세요"
               + tail)

    # 회사 분류 체계를 연결하지 않은 배포에서는 기능을 숨기지 않고 '꺼짐'으로 보여
    # 준다 — 숨기면 그런 기능이 있다는 것을 영영 모른다(설계서 12-1).
    if tax is None:
        st.info("업무분류는 아직 사용하지 않습니다. 설정 화면에서 **회사 분류 체계**를 "
                "연결하면 “이 문서가 무엇에 관한 것인지”도 함께 정리됩니다.")
        return

    # 후보 전체를 여기서는 모두 보여준다 — 목록의 "상위 N개" 제한은 표에만 적용한다.
    # 결정을 내리려면 검토자는 전부 봐야 한다(상위 설계 6-3).
    # 계층 정합성 경고(재설계 11-3) — 서로 다른 큰 갈래가 동시에 제안된 경우다.
    # 후보를 자동으로 버리지 않고 사람에게 올린다.
    # 엔진이 준 detail 은 개발자용 설명이라 그대로 쓰지 않는다. 대신 "어느
    # 분류끼리 부딪쳤는지"를 이름으로 보여준다 — 검토자에게 필요한 건 그것뿐이다.
    path_by_id = {c.get("dc_id"): (c.get("path") or c.get("dc_id")) for c in candidates}
    for cf in (csorecord.doctype_of(rec) or {}).get("conflicts") or []:
        names = [path_by_id.get(i, i) for i in (cf.get("dc_ids") or [])]
        if not names:
            continue
        st.warning("서로 다른 갈래의 분류가 함께 제안됐습니다 — "
                   + " · ".join(f"**{n}**" for n in names)
                   + ". 어느 쪽이 맞는지(또는 둘 다 맞는지) 확인해 주세요.")

    picked = {}
    if candidates:
        # 관리자가 직접 고른 분류도 이 목록에 함께 들어온다(근거 줄에 표시된다).
        st.markdown("**이 문서에 붙은 분류**")
        for c in candidates:
            dc_id = c["dc_id"]
            # 체크박스를 쓰는 이유 — 업무분류는 여러 개가 동시에 맞을 수 있다.
            # 위젯 모양이 곧 "여러 개 고를 수 있다"는 안내가 된다(설계서 원칙 2).
            # 기본으로 체크해 둔다 — 제안이 맞는 경우가 대부분이라, 맞을 때마다
            # 하나씩 체크하는 것보다 아닌 것만 풀고 [이대로 확정] 하는 쪽이 손이
            # 훨씬 덜 간다. 다만 예전에 관리자가 '아니라고 표시한'(rejected) 것은
            # 다시 체크해 두면 그 판단을 되돌리는 셈이라 그대로 비워 둔다.
            on = st.checkbox(f"**{c.get('path') or dc_id}**  ·  {W.pct(c.get('confidence'))}",
                             value=(c["status"] != "rejected"),
                             key=f"dt_ck_{scope}_{file}_{dc_id}")
            picked[dc_id] = on
            # 확정은 확정이되, 자동인지 사람이 정한 것인지는 눈에 보여야 한다.
            st.caption(f"　{W.doctype_by(c)}")
            render_doctype_evidence(c)
    else:
        st.caption("자동으로 제안된 분류가 없습니다. 아래에서 직접 고를 수 있습니다.")

    # 직접 추가 — 규칙이 아직 없는 분류는 사람이 직접 고른다. 반드시 목록에서만
    # 고르게 한다(직접 타이핑하면 오타가 그대로 잘못된 DC_ID 가 된다, 상위 설계 T5).
    opts = taxlib.selectable(tax)
    label_by_id = dict(opts)
    extra = st.multiselect("직접 추가 — 회사 분류 체계에서 찾기",
                           [dc for dc, _ in opts],
                           default=[c["dc_id"] for c in candidates
                                    if c["status"] == "confirmed"
                                    and c["dc_id"] not in {d for d, _ in opts}],
                           format_func=lambda dc: label_by_id.get(dc, dc),
                           key=f"dt_extra_{scope}_{file}")

    # "아직 안 본 문서"와 "봤는데 해당 분류가 없는 문서"는 완전히 다르다. 이 체크가
    # 없으면 해당 분류가 없는 문서가 영원히 대기 목록에 남는다(설계서 8-1).
    none_of = st.checkbox("해당하는 분류가 없습니다",
                          key=f"dt_none_{scope}_{file}")

    # 업무분류 확정은 '정정'이 아니라 원래 사람이 하기로 되어 있는 정상 절차라
    # 이유를 필수로 받지 않는다(설계서 9-1). 보안등급 쪽과 다른 점이다.
    reason = st.text_input("결정 이유 (선택)", key=f"dt_reason_{scope}_{file}",
                           placeholder="예: 제안 내용은 첨부일 뿐 계약서가 본체")

    chosen = [] if none_of else (
        [dc for dc, on in picked.items() if on] +
        [dc for dc in extra if not picked.get(dc)])
    btn = ("‘해당 없음’으로 확정" if none_of else f"이대로 확정 ({len(chosen)}개)")
    if st.button(btn, key=f"dt_save_{scope}_{file}", type="primary",
                 width="stretch"):
        if not reviewer.strip():
            st.error("화면 오른쪽 위에 '검토자 이름'을 먼저 입력하세요(기록에 남깁니다).")
        else:
            # 체크하지 않은 제안은 '아니라고 표시함'으로 남긴다 — "애초에 제안되지
            # 않음"과 구분해야 나중에 규칙 정밀도를 잴 수 있다(상위 설계 7-4-1).
            rejected = [c["dc_id"] for c in candidates if c["dc_id"] not in chosen]
            doctype_review.append_doctype_override(
                ov_path, file, rec.get("doc_id"), chosen, rejected,
                reason.strip(), reviewer.strip())
            st.success(f"확정됨: {len(chosen)}개" +
                       (f" · 아니라고 표시함 {len(rejected)}개" if rejected else ""))
            st.rerun()
    st.caption("✔ 체크하지 않은 제안은 “아니라고 표시함”으로 기록돼 다음 분류가 좋아집니다.")

    # 확정된 분류가 있으면, 그 문서에서 규칙에 넣을 말을 찾아 준다(설계 9-1).
    # 확정 직후가 가장 판단이 정확한 순간이라 이 자리에 둔다.
    settled = [c["dc_id"] for c in candidates if c["status"] == "confirmed"]
    if settled and paths_of_screen():
        render_term_suggest(rec, settled, paths_of_screen(), tax,
                            reviewer=reviewer, scope=scope)

    hist = docidkey.history_for(history_dt, latest_dt, rec)
    if hist:
        with st.expander(f"결정 기록 {len(hist)}건"):
            for e in reversed(hist):
                st.caption(f"- {e.get('ts','')} · {e.get('reviewer','')} · "
                           f"확정 {len(e.get('confirmed') or [])}개 · "
                           f"아니라고 표시함 {len(e.get('rejected') or [])}개 "
                           f"· {e.get('reason','')}")


#------------------------------------------------------------------
# 지금 화면이 쓰는 경로 설정
#=> 경로는 main() 이 session_state["paths"] 에 담아 둔다. 렌더 함수마다 인자로
#   길게 넘기는 대신 여기서 꺼내 쓴다 — 함수 하나 때문에 여러 렌더 함수의
#   인자 목록을 고치면 다른 화면까지 영향을 받는다.
#
# -in: 없음
#
# -out: dict = 경로 설정(아직 준비 전이면 빈 dict)
# -out: error = 없음
#------------------------------------------------------------------
def paths_of_screen():
    return st.session_state.get("paths") or {}


#------------------------------------------------------------------
# 기준 문서 저장 직후 제목 채우기
#=> 화면은 본문을 갖고 있지 않아 등록할 때 제목(첫 줄)을 넣을 수 없다. 업무분류 BM25
#   2차가 쓰는 재료이므로, 저장한 바로 뒤 엔진(--seed-fill-title)에 채우게 한다
#   (설계서 업무분류-2차-BM25 §17-4 남은 일).
#    1) 제목이 빈 줄만 채운다 — 추출 본문 폴더가 있으면 거기서, 없으면 원본을 다시 읽는다
#    2) 실패해도 등록은 이미 끝났다 — 까닭만 돌려줘 화면이 경고로 알린다
#
# -in: cfg = st.session_state["seedcfg"](seed_path · csoclassify_cmd · pythonpath)
#
# -out: str = 안내에 덧붙일 한 마디(성공: "제목 채움 N" · 실패: 경고 문장 · 할 일 없음: "")
# -out: error = 없음
#------------------------------------------------------------------
def fill_titles_after_save(cfg):
    base = gerunner.parse_base_cmd(cfg.get("csoclassify_cmd"))
    if not base or not cfg.get("seed_path"):
        return ""
    ok, msg = gerunner.fill_seed_titles(base, cfg["seed_path"],
                                        text_dir=paths_of_screen().get("text_dir") or None,
                                        pythonpath=cfg.get("pythonpath"))
    if not ok:
        return f"제목(첫 줄)을 채우지 못했습니다 — {msg}"
    # "[seed-fill-title] 채움 3 · …" 에서 채운 건수만 뽑아 짧게 알린다
    m = re.search(r"채움 (\d+)", msg or "")
    return f"제목 채움 {m.group(1)}건" if m and m.group(1) != "0" else ""


#------------------------------------------------------------------
# 단어 제안 재료 읽기 (캐시)
#=> 기준 문서·검토 이력·저장된 본문을 모아 온다. 문서를 열 때마다 다시 읽으면
#   느려서 캐시하되, 세 파일의 수정시각이 바뀌면 다시 읽는다.
#
# -in: seed_path = class_seed.jsonl 경로
# -in: ov_path   = cso_override.jsonl 경로
# -in: text_dir  = 추출 본문 폴더(빈 값이면 제목·파일 이름만 쓴다)
# -in: stamp     = 캐시 무효화용 수정시각 묶음(값 자체는 안 쓴다)
#
# -out: list = termsuggest.load_documents 결과
# -out: error = 없음(파일이 없으면 그만큼 적게 모인다)
#------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def load_suggest_docs(seed_path, ov_path, text_dir, stamp):
    return termsuggest.load_documents(seed_path or None, ov_path or None,
                                      text_dir or None)


#------------------------------------------------------------------
# 파일 수정시각 묶기 (캐시 열쇠)
#=> 캐시가 언제 낡았는지 판단할 값이다. 없는 파일은 0 으로 둔다.
#
# -in: paths = 경로 목록
#
# -out: tuple = 수정시각들
# -out: error = 없음
#------------------------------------------------------------------
def _stamp(paths):
    out = []
    for p in paths:
        try:
            out.append(os.path.getmtime(p) if p else 0)
        except OSError:
            out.append(0)
    return tuple(out)


#------------------------------------------------------------------
# 이 문서에서 단어 뽑기 (검토 화면 진입점 ①)
#=> 관리자가 '규칙이 못 잡은 문서'의 분류를 확정한 바로 그 자리에서, 그 문서에
#   있는 말 중 규칙에 넣을 만한 것을 후보로 보여 준다. 고른 것만 규칙에
#   덧붙인다 — 지우지 않고, 규칙이 없는 분류는 만들지 않는다.
#
#   [왜 이 자리인가] 관리자가 그 문서를 눈앞에 두고 있어 판단이 가장 정확한
#   순간이다. 설정 화면에서 분류 단위로 훑는 것보다 먼저 이 자리를 만든 이유는,
#   검토를 하면 그만큼 재료(확정 이력)도 함께 쌓이기 때문이다.
#
# -in: rec       = 지금 보고 있는 문서의 분류 레코드
# -in: confirmed = 이 문서에 확정된 dc_id 목록
# -in: paths     = 화면 경로 설정(seed·override·doc_rules·text_dir)
# -in: tax       = 회사 분류 체계(이름 표시·다른 분류 이름 거르기)
# -in: reviewer  = 검토자 이름(채택·기각 이력에 남긴다. 비어 있으면 누르지 못한다)
# -in: scope     = 위젯 키 구분용 접두
#
# -out: 없음(화면을 그린다)
# -out: error = 없음(재료가 모자라면 까닭을 화면에 적는다)
#------------------------------------------------------------------
def render_term_suggest(rec, confirmed, paths, tax, reviewer="", scope="doctype"):
    file = rec.get("file", "")
    if not confirmed:
        return
    doc_rules_path = paths.get("doc_rules") or ""
    if not os.path.isfile(doc_rules_path):
        return

    # [왜 버튼 뒤에 두나] 목록 화면은 문서 30여 건의 패널을 한 번에 그리고,
    # streamlit 의 expander 는 접혀 있어도 안쪽 코드를 그대로 실행한다. 여기서
    # 바로 계산하면 화면을 열 때마다 문서 수만큼 제안 계산이 돈다.
    # 사람이 누른 문서 하나만 계산한다.
    open_key = f"ts_open_{scope}_{file}"
    if not st.session_state.get(open_key):
        if st.button("💡 이 문서에서 규칙에 넣을 말 찾기",
                     key=f"ts_btn_{scope}_{file}", width="stretch"):
            st.session_state[open_key] = True
            st.rerun()
        return

    with st.container(border=True):
        st.markdown("**💡 이 문서에서 규칙에 넣을 말 찾기**")
        # [왜 들고 다니나] 넣기·기각 뒤에 st.rerun() 을 부르면 그 자리에서 띄운
        # st.success 가 곧바로 씻겨 나간다. 특히 "배포본 복사본도 맞춰 주세요"는
        # 놓치면 안 되는 안내라, 다음 그리기까지 들고 왔다가 한 번 보여 주고 지운다.
        msg_key = f"ts_msg_{scope}_{file}"
        done = st.session_state.pop(msg_key, None)
        if done:
            st.success(done[0])
            if len(done) > 1 and done[1]:
                st.caption(done[1])
        st.caption("이 문서에 있는 말 중 **이 분류에만 나오는 것**을 찾아 줍니다. "
                   "고른 말만 업무분류 기준에 덧붙습니다 — 지우지는 않습니다.")
        if not paths.get("text_dir") or not os.path.isdir(paths["text_dir"]):
            st.caption("· 추출 본문 폴더가 없어 **제목·파일 이름만** 봅니다"
                       "(설정 화면에서 지정하면 본문까지 봅니다).")
        if st.button("닫기", key=f"ts_close_{scope}_{file}"):
            st.session_state[open_key] = False
            st.rerun()

        docs = load_suggest_docs(paths.get("seed") or "", paths.get("override") or "",
                                 paths.get("text_dir") or "",
                                 _stamp([paths.get("seed"), paths.get("override"),
                                         doc_rules_path]))
        rule_doc, gen_ctx = suggest_rule_doc(doc_rules_path, paths)
        syn = docvocab.load_synonyms(doc_rules_path) or {}
        stop_path = os.path.join(os.path.dirname(os.path.abspath(doc_rules_path)),
                                 "doc_rule_stopwords.yaml")
        stopwords = termsuggest.load_stopwords(stop_path)
        audit_path = os.path.join(os.path.dirname(os.path.abspath(doc_rules_path)),
                                  "doc_rule_suggest_audit.jsonl")
        # termsuggest 는 tax["by_id"][dc_id]["title"] 만 본다 — 화면이 이미 읽어 둔
        # 분류 체계를 그대로 넘기면 된다(모양을 다시 만들지 않는다).
        by_id = (tax or {}).get("by_id") or {}

        for dc_id in confirmed:
            res = termsuggest.suggest_for_doc(dc_id, docs, file, rule_doc=rule_doc,
                                              stopwords=stopwords, tax=tax,
                                              suffixes=syn.get("suffixes"),
                                              endings=syn.get("verb_endings"))
            node = by_id.get(dc_id) or {}
            name = node.get("path") or node.get("title") or dc_id
            st.markdown(f"**{name}** — 이 분류로 확정된 문서 {res['docs']}건"
                        f"(폴더 {res['clusters']}곳)")
            if not res["focus_only"]:
                st.caption("　이 문서에서 새로 넣을 만한 말을 찾지 못했습니다"
                           + (f" — {res['reason']}" if res["reason"] else "."))
                continue

            picks = []
            for cand in res["focus_only"]:
                key = f"ts_{scope}_{file}_{dc_id}_{cand['term']}"
                label = f"**{cand['term']}**　{W.suggest_where(cand['fields'])}"
                on = st.checkbox(label, value=cand["checked"], key=key)
                if on:
                    picks.append(cand)
                st.caption("　" + W.suggest_why(cand))

            c1, c2 = st.columns(2)
            go = c1.button(f"기준에 넣기 ({len(picks)}개)",
                           key=f"ts_go_{scope}_{file}_{dc_id}",
                           disabled=not picks, type="primary", width="stretch")
            # 기각도 결정이다 — 눌러 두면 그 말이 다음 제안에서 자동으로 빠진다.
            # 쓸수록 조용해지는 화면을 만드는 것이 이 버튼의 목적이다(설계 11장).
            no = c2.button(f"이 말들 다시 보지 않기 ({len(picks)}개)",
                           key=f"ts_no_{scope}_{file}_{dc_id}",
                           disabled=not picks, width="stretch")
            st.caption("　[다시 보지 않기] 는 **제안만** 막습니다 — 이미 기준에 들어 "
                       "있는 말을 지우지는 않습니다.")

            if (go or no) and not reviewer.strip():
                st.error("화면 오른쪽 위에 '검토자 이름'을 먼저 입력하세요(기록에 남깁니다).")
            elif go:
                added, skipped, missing = termsuggest.apply_terms(rule_doc, dc_id, picks)
                if missing:
                    st.error(f"'{name}' 규칙이 아직 없습니다 — 설정 화면의 "
                             "[규칙 다시 만들기]로 규칙을 먼저 만드세요(꺼 둔 분류면 먼저 켜세요).")
                else:
                    try:
                        save_suggested(doc_rules_path, rule_doc, dc_id, gen_ctx, paths)
                        # doc_rule.yaml 에는 주석이 남지 않는다. "이 단어가 언제·어느
                        # 근거로 들어왔나"를 되짚을 기록은 이 이력뿐이다.
                        termsuggest.append_audit(audit_path, dc_id, "accept",
                                                 picks, reviewer.strip())
                    except OSError as e:
                        st.error(f"기준 파일을 쓰지 못했습니다: {e}")
                    else:
                        st.session_state[msg_key] = (
                            f"'{name}' 기준에 {len(added)}곳 넣었습니다"
                            + (f" · 이미 있어 건너뜀 {len(skipped)}개" if skipped else ""),
                            "판정에 바로 반영되려면 분류를 다시 돌려야 합니다. "
                            "배포본을 쓰고 있다면 기준 파일 복사본도 맞춰 주세요.")
                        st.cache_data.clear()
                        st.rerun()
            elif no:
                try:
                    # 그 분류에만 막는다 — '인증'이 인증서류에서는 넓어도 다른
                    # 분류에서는 쓸 만할 수 있다. 전체 금지는 설정 화면에서 손으로 한다.
                    gone = termsuggest.add_stopwords(
                        stop_path, [c["term"] for c in picks], node=dc_id)
                    termsuggest.append_audit(audit_path, dc_id, "reject",
                                             picks, reviewer.strip())
                except OSError as e:
                    st.error(f"목록을 쓰지 못했습니다: {e}")
                else:
                    st.session_state[msg_key] = (
                        f"{len(gone)}개를 다시 보지 않습니다 — "
                        f"'{name}' 에서 제안되지 않습니다.",
                        "기준 파일은 그대로입니다 — 이 목록은 '제안'만 막습니다.")
                    st.cache_data.clear()
                    st.rerun()

            past = termsuggest.load_audit(audit_path, dc_id)
            if past:
                with st.expander(f"이 분류의 단어 결정 기록 {len(past)}건"):
                    for row in reversed(past[-30:]):
                        st.caption(f"- {row.get('ts','')} · {row.get('reviewer','')} · "
                                   f"**{row.get('term','')}** "
                                   f"{W.suggest_action(row.get('action'))}"
                                   + (f" ({'·'.join(row.get('fields') or [])})"
                                      if row.get("action") == "accept" else ""))


#------------------------------------------------------------------
# 내보내기 버튼 렌더 (문서함 오른쪽 위)
#=> 확정 결과를 CSV/JSONL 로 내려받아 문서중앙화 시스템이 접근권한 적용에 쓰게 한다.
#   탭 하나를 쓸 만큼 큰 기능이 아니라서 문서함 구석 버튼으로 옮겼고, 지금 화면에
#   걸린 필터 결과를 그대로 낸다 — "보이는 것이 곧 내보내지는 것"이라야 헷갈리지 않는다.
#   두 축을 모두 담는다(보안등급 1개 + 업무분류 요약) — 한 파일로 두 축을 넘긴다.
#
# -in: df = 지금 화면에 보이는(필터 적용된) 문서 표
#
# -out: 없음(다운로드 버튼 2개)
# -out: error = 없음
#------------------------------------------------------------------
def render_export_button(df):
    cols = [c for c in ["file", "final", "auto", "overridden", "confidence",
                        "doctype", "dt_reviewed", "method", "rule_version"] if c in df.columns]
    out = df[cols].rename(columns={"final": "grade", "doctype": "업무분류",
                                   "dt_reviewed": "업무분류_확정여부"})
    with st.popover("⬇️ 내보내기", width="stretch"):
        st.caption(f"지금 보이는 **{len(out)}건**을 내보냅니다(보안등급 + 업무분류).")
        st.download_button("CSV 파일로", out.to_csv(index=False).encode("utf-8-sig"),
                           "classification_final.csv", "text/csv",
                           width="stretch")
        jsonl = "\n".join(json.dumps(r, ensure_ascii=False) for r in out.to_dict("records"))
        st.download_button("JSONL 파일로", jsonl.encode("utf-8"),
                           "classification_final.jsonl", "application/json",
                           width="stretch")


#------------------------------------------------------------------
# 기준 문서에 함께 적어 둘 '원본의 지문'
#=> 기준 문서는 원본의 사본(벡터)을 들고 있어서, 원본이 나중에 수정되면 낡은
#   벡터로 계속 전파하게 된다. 그것을 알아채려면 등록 시점의 지문을 남겨야 한다.
#    1) 내용 해시는 분류 실행이 --hash 로 이미 구해 둔 값을 그대로 쓴다(공짜다)
#
#   [v3 에서 크기·수정시각을 뺀 이유] 원본이 바뀌었는지 보는 코드는
#   seedstore.check_seeds / sync_with_records 둘뿐이고, 둘 다 해시만 비교한다.
#   size·mtime 을 읽는 코드는 한 줄도 없었다 — 눈으로 보는 보조 정보였다.
#
# -in: rec = 분류 레코드(hash 가 없을 수 있다 — 옛 결과이거나 --hash 없이 돌린 경우)
#
# -out: dict = {"hash"} (해시를 모르면 빈 dict)
# -out: error = 없음
#------------------------------------------------------------------
def seed_doc_meta(rec):
    meta = {}
    h = (rec or {}).get("hash")
    if h:
        meta["hash"] = h
    return meta


#------------------------------------------------------------------
# 파일 1개 온디맨드 임베딩
#=> 빠른 분류(선택적 임베딩)로 벡터가 생략된 문서를 seed 로 승격할 때, 그 문서
#   하나만 지금 임베딩해서 벡터를 얻는다("확정 문서는 나중 승격 시점에 임베딩" 정책).
#
# -in: cfg  = seedcfg(csoclassify_cmd/pythonpath)
# -in: file = 대상 문서 경로(원본이 그 위치에 있어야 함)
#
# -out: list|None = 384차원 벡터(실패하면 None, 화면에 오류 표시)
# -out: error = 없음(예외는 화면 표시 후 None 반환)
#------------------------------------------------------------------
def _embed_file_ondemand(cfg, file):
    base = gerunner.parse_base_cmd(cfg.get("csoclassify_cmd"))
    if not base:
        st.error("분류 실행 명령이 비어 있습니다 — 설정/분류 ▸ 고급 ▸ 실행 명령을 먼저 채워 주세요.")
        return None
    if not os.path.isfile(file):
        st.error(f"원본 파일을 찾을 수 없어 임베딩을 계산하지 못했습니다: {file}")
        return None
    try:
        with st.spinner(f"임베딩 계산 중… {os.path.basename(file)}"):
            r = gerunner.run_csoclassify_file(base, file, pythonpath=cfg.get("pythonpath"))
    except Exception as e:
        uierrlog.show_error(f"임베딩 실패: {e}", exc=e, where="임베딩")
        return None
    vec = r.get("vector")
    if vec is None:
        # 실행은 됐는데 벡터가 안 온 경우. 예전에는 여기서 조용히 None 을 돌려줘
        # "눌러도 아무 일이 없다"로 보였다 — 이유를 반드시 말한다. 실행 명령이
        # --with-vector 를 모르는 옛 빌드인지(그러면 stderr 에 경고가 찍힌다),
        # 아니면 문서에서 텍스트를 못 뽑은 것인지는 stderr 이 알려 준다.
        err = str(r.get("_stderr") or "")
        if "with-vector" in err:
            st.error("실행 명령이 `--with-vector` 를 모릅니다 — 기준 문서로 쓸 벡터를 "
                     "받을 수 없습니다. 실행 파일이 옛 빌드일 수 있으니 다시 빌드하거나, "
                     "설정/분류 ▸ 고급 ▸ 실행 명령을 벡터를 내는 버전으로 바꿔 주세요.")
        else:
            st.error("임베딩 벡터를 얻지 못했습니다 — 이 문서에서 텍스트를 뽑지 "
                     "못했을 수 있습니다(추출 실패). 문서를 열어 내용이 있는지 "
                     "확인해 주세요.")
        if err.strip():
            st.caption(f"실행 로그: {err.strip().splitlines()[-1][:200]}")
    return vec


#------------------------------------------------------------------
# 기준 문서 등록 버튼 렌더 (두 축 통합 · Phase 4 ① + 설계서 5-4)
#=> 확정된 문서를 기준 문서 저장소(class_seed.jsonl)에 한 줄로 올린다.
#   [왜 버튼이 하나인가] 저장소에는 문서 하나가 한 줄이다 — 그 한 줄이 등급
#   (grade)과 확정 분류(labels.doctype)를 같이 들고 있다. 예전처럼 보안등급 칸과
#   업무분류 칸에 버튼이 따로 있으면, 같은 한 줄을 만들자고 두 번 누르는 꼴이고
#   한쪽만 누른 반쪽 기준 문서가 생긴다.
#    1) 이번에 등록될 내용(등급 · 확정 분류)을 먼저 글로 보여준다
#    2) 벡터가 없으면 이 문서만 임베딩해 확보한다 — 벡터 없는 기준 문서는
#       비교 대상이 못 되므로 저장할 이유가 없다
#    3) 기존 기준 문서와 거의 같으면(사본) 경고하고 확인을 받는다
#
# -in: rec              = 분류 레코드(vector 는 없을 수 있음 → 등록 시 온디맨드 계산)
# -in: final_grade      = 최종 보안등급(C/S/O · "보류" 면 등급은 등록하지 않는다)
# -in: confirmed_dc_ids = 이미 저장된 확정 분류 dc_id 목록(없으면 업무분류는 건너뜀)
# -in: tax              = 회사 분류 체계(등록될 분류 이름을 보여주는 데만 쓴다)
# -in: scope            = 위젯 key 접두(같은 문서가 여러 화면에 그려질 때 충돌 방지)
#
# -out: 없음(등록 시 파일 저장 + 감사 기록 + rerun)
# -out: error = 없음(실패는 화면에 이유와 함께 표시)
#------------------------------------------------------------------
def render_seed_promote(rec, final_grade, confirmed_dc_ids, tax=None, scope="list"):
    cfg = st.session_state.get("seedcfg", {})
    file = rec.get("file", "")
    vec = rec.get("vector")
    has_grade = final_grade in ("C", "S", "O")

    st.markdown("**이 문서를 기준 문서로 등록**")
    st.caption("기준 문서는 다음 분류 때 '비슷한 문서 참고'의 잣대가 됩니다 — "
               "확실한 문서만 올리세요.")

    # 둘 다 없으면 올릴 것이 없다. 무엇을 먼저 해야 하는지까지 알려 준다.
    if not has_grade and not confirmed_dc_ids:
        st.caption("등급을 정하거나 업무분류를 확정한 뒤에 등록할 수 있습니다.")
        return

    # 이번에 저장될 내용을 그대로 보여준다 — 눌러 보고 나서야 알게 되면 안 된다.
    parts = []
    if has_grade:
        parts.append(f"보안등급 **{W.GRADE_PLAIN[final_grade]}**")
    if confirmed_dc_ids:
        names = " · ".join(taxlib.path_of(tax, dc) for dc in confirmed_dc_ids)
        parts.append(f"업무분류 **{len(confirmed_dc_ids)}건** ({names})")
    st.caption("등록할 내용 — " + " / ".join(parts))
    if not has_grade:
        st.caption("↳ 보안등급은 아직 정해지지 않아 업무분류만 등록합니다.")
    elif not confirmed_dc_ids:
        st.caption("↳ 확정된 업무분류가 없어 보안등급만 등록합니다.")

    seeds = seedstore.load_seeds(cfg.get("seed_path"))
    # 경로 표기(대소문자·슬래시)만 다른 같은 문서를 "처음 등록"으로 오해하지 않게
    # 정규화해서 찾는다. 이미 있으면 각 축이 지금 어떤 상태인지도 알려 준다.
    prev = seedstore.find_seed(seeds, file)
    if prev:
        cur = []
        for _ax in seedstore.AXES:
            _st = seedstore.axis_state(prev, _ax)
            if _st:
                cur.append(f"{seedstore.AXIS_LABEL[_ax]} {W.SEED_STATE[_st]}")
        st.caption("이미 기준 문서로 등록돼 있습니다("
                   + " · ".join(cur) + ") → 다시 누르면 최신 내용으로 갱신됩니다.")

    # 벡터가 있으면 미리 near-dup 을 경고(자기 자신 제외), 없으면 등록 시 온디맨드 임베딩.
    force = True
    sim = 0.0
    if vec is not None:
        sim, near = seedstore.nearest_seed_sim(vec, seeds, exclude_file=file)
        if sim >= SEED_DUP_SIM:
            st.warning(f"이미 등록된 기준 문서와 거의 같습니다(닮은 정도 {W.pct(sim)} · "
                       f"{os.path.basename(near or '')}) — 사본일 수 있어 등록해도 도움이 되지 않습니다.")
            force = st.checkbox("그래도 등록", key=f"seedforce_{scope}_{file}")
    else:
        st.caption("이 문서는 벡터가 없어(빠른 분류로 생략됨), 등록할 때 이 문서만 "
                   "임베딩을 계산합니다.")

    if st.button("🌱 기준 문서로 등록", key=f"promote_{scope}_{file}", type="primary"):
        if not cfg.get("reviewer", "").strip():
            st.error("화면 오른쪽 위에 '검토자 이름'을 먼저 입력하세요.")
            return
        if not force:
            st.info("중복이라 추가하지 않았습니다.")
            return
        v = vec
        if v is None:                       # 벡터 없으면 지금 이 문서만 임베딩해 확보
            v = _embed_file_ondemand(cfg, file)
            if v is None:
                return                      # 실패 이유는 _embed_file_ondemand 가 표시
        # 축을 하나씩 확정한다(set_axis 가 저장소로 들어가는 유일한 통로다).
        # 한 축만 확정해도 다른 축은 건드리지 않으므로 순서를 신경 쓸 필요가 없다.
        dc_ids = list(confirmed_dc_ids)
        if not dc_ids:
            # 이번엔 확정 분류가 없더라도, 예전에 등록해 둔 분류는 지우지 않는다.
            dc_ids = list(seedstore.axis_value(prev, "doctype") or []) if prev else []
        # 원본이 나중에 바뀐 것을 알아채려면 지금의 지문을 함께 적어 둬야 한다.
        doc_meta = seed_doc_meta(rec)
        who = cfg["reviewer"].strip()
        seeds2 = seeds
        if has_grade:
            before = seedstore.axis_value(prev, "security") if prev else None
            seeds2 = seedstore.set_axis(seeds2, file, "security", final_grade, who,
                                        source="phase4", vector=v, doc=doc_meta)
            seedstore.append_seed_audit(cfg["audit_path"],
                                        "update" if before else "add", file,
                                        axis="security", before=before, after=final_grade,
                                        reviewer=who, reason=f"문서함 등록 sim={sim:.3f}")
        if dc_ids:
            before = list(seedstore.axis_value(prev, "doctype") or []) if prev else None
            seeds2 = seedstore.set_axis(seeds2, file, "doctype", dc_ids, who,
                                        source="phase6", vector=v, doc=doc_meta)
            seedstore.append_seed_audit(cfg["audit_path"],
                                        "update" if before else "add", file,
                                        axis="doctype", before=before, after=dc_ids,
                                        reviewer=who, reason="문서함 등록")
        try:
            seedstore.save_seeds(cfg["seed_path"], seeds2)
        except OSError as e:
            uierrlog.show_error(f"기준 문서 저장 실패: {e}", exc=e, where="기준 문서 저장")
            return
        # BM25 재료인 제목(첫 줄)을 저장 직후 채운다 — 화면은 본문을 갖고 있지 않다
        with st.spinner("기준 문서 제목(첫 줄)을 채우는 중…"):
            tmsg = fill_titles_after_save(cfg)
        st.success(f"기준 문서로 등록됐습니다 — {' / '.join(parts)} · "
                   f"저장 위치: {cfg['seed_path']}" + (f" · {tmsg}" if tmsg else ""))
        st.rerun()


#------------------------------------------------------------------
# seed 관리 화면 렌더 (Phase 4)
#=> 지금 기준 문서가 몇 건이고 등급이 고르게 있는지만 보여준다(읽기 전용).
#   [왜 관리 기능이 여기 없나] 등록은 문서함에서 문서를 골라 하는 것이 자연스럽고
#   (여러 건 한꺼번에 등록도 거기 있다), 이 화면에 등록·수정·점검까지 모아 두니
#   설정 화면이 길어져 정작 '파일 위치'를 찾기 어려웠다.
#
# -in: records = (쓰지 않음 — 호출부 모양을 유지하려고 남겨 둔 인자)
# -in: latest  = (쓰지 않음)
# -in: tax     = (쓰지 않음)
#
# -out: 없음(Streamlit 출력)
# -out: error = 없음
#------------------------------------------------------------------
def render_seed_manager(records=None, latest=None, tax=None):
    cfg = st.session_state.get("seedcfg", {})
    seed_path = cfg.get("seed_path")

    seeds = seedstore.load_seeds(seed_path)
    counts = seedstore.grade_counts(seeds)

    ax = seedstore.axis_counts(seeds)
    st.caption("“이런 문서는 이 등급 · 이 분류” 본보기로 등록해 둔 문서입니다 · "
               f"총 {ax['total']}건 — {W.AXIS_SEC} {ax['security']} · "
               f"{W.AXIS_DOC} {ax['doctype']} (둘 다 {ax['both']})"
               + (f" · 확인 필요 {ax['suspect']}" if ax["suspect"] else ""))
    m = st.columns(4)
    m[0].metric("기준 문서", len(seeds))
    m[1].metric(W.GRADE_LABEL["C"], counts["C"])
    m[2].metric(W.GRADE_LABEL["S"], counts["S"])
    m[3].metric(W.GRADE_LABEL["O"], counts["O"])
    if seeds:
        lo = min(counts.values())
        if lo == 0 or lo < max(counts.values()) / 4:
            weak = "/".join(g for g, n in counts.items() if n == lo)
            st.warning(f"등급이 한쪽으로 쏠려 있습니다 — {weak} 기준 문서가 부족합니다. "
                       "비슷한 문서 비교가 그 등급을 잘 못 맞출 수 있어요(보강 권장).")

#------------------------------------------------------------------
# 업무분류가 확정된 기준 문서가 몇 건인가(가볍게 세기)
#=> '규칙 없이 기준 문서만으로' 옵션을 고를 수 있는지 판단하는 데만 쓴다.
#   load_seeds 는 문서마다 384차원 벡터까지 통째로 읽어 무겁다 — 여기서는
#   labels.doctype 이 있는 줄만 세면 되므로 벡터는 버린다.
#
# -in: path = class_seed.jsonl 경로
#
# -out: int = 업무분류 라벨을 가진 기준 문서 수(파일이 없으면 0)
# -out: error = 없음(깨진 줄은 건너뛴다)
#------------------------------------------------------------------
def count_doctype_seeds(path):
    if not path or not os.path.isfile(path):
        return 0
    n = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                dc = csorecord.doctype_ids(row) or (row.get("labels") or {}).get("doctype")
            except json.JSONDecodeError:
                continue
            if dc:
                n += 1
    return n


#------------------------------------------------------------------
# 분류 직후 기준 문서 자동 대조
#=> 방금 만든 결과에는 문서마다 내용 지문(hash)이 들어 있다. 기준 문서가 적어 둔
#   지문과 견주면 "등록할 때의 그 원본이 맞는가"를 공짜로 알 수 있다.
#    1) 지문이 다르거나(stale) 원본이 사라졌으면(missing) 두 축에 확인 필요 표시
#    2) 원본이 제자리로 돌아왔으면 표시를 푼다
#   [원칙] 절대 지우지 않는다. 기준 문서 하나가 잘못 사라지면 그와 비슷한 문서
#   수십 건의 분류가 함께 흔들린다 — 오탐으로 지우는 쪽이 훨씬 비싸다.
#
# -in: cfg         = seedcfg(저장소·감사 로그 경로)
# -in: grades_path = 방금 만들어진 분류 결과 파일
#
# -out: 없음(바뀐 것이 있으면 저장 + 화면에 한 줄 안내)
# -out: error = 없음(실패해도 분류 결과 자체는 이미 저장돼 있으므로 조용히 넘어간다)
#------------------------------------------------------------------
def _sync_seeds_after_run(cfg, grades_path):
    seed_path = cfg.get("seed_path")
    if not seed_path or not os.path.isfile(seed_path):
        return
    try:
        seeds = seedstore.load_seeds(seed_path)
        if not seeds:
            return
        recs = load_records(grades_path, os.path.getmtime(grades_path))
        new, events = seedstore.sync_with_records(seeds, recs)
        if not events:
            return
        seedstore.save_seeds(seed_path, new)
        for e in events:
            seedstore.append_seed_audit(cfg.get("audit_path"), e["action"], e["file"],
                                        axis=e["axis"], before=None, after=None,
                                        reviewer="system", reason=e["reason"])
    except (OSError, ValueError, json.JSONDecodeError):
        return      # 대조는 부가 기능이다 — 실패해도 분류 결과를 망치지 않는다

    n_sus = len({e["file"] for e in events if e["action"] == "suspect"})
    n_res = len({e["file"] for e in events if e["action"] == "restore"})
    if n_sus:
        st.warning(f"기준 문서 {n_sus}건의 원본이 바뀌었거나 사라졌습니다 — "
                   "그 문서들은 잣대로 쓰지 않습니다. 원본을 확인한 뒤 문서함에서 "
                   "다시 등록하면 됩니다.")
    if n_res:
        st.info(f"기준 문서 {n_res}건이 원본과 다시 맞아 잣대로 되살아났습니다.")


# 처음부터 채워 두는 파일 종류 — 엔진이 실제로 글을 뽑을 수 있는 것들이다.
#   txt·md → text, html → html, 나머지는 전용 파서(pdf·hwp·hwpx·doc·docx·
#   xls·xlsx·ppt·pptx). 여기에 없는 확장자를 넣어도 막지는 않지만, 글을 못 뽑아
#   '본문 없음'으로 남는다.
#
#   [압축 확장자도 넣는다, 2026-09-08] 엔진은 압축을 풀어 내부 문서를 하나씩
#   분류하지만, 그 전에 이 패턴으로 대상을 고른다 — 압축 확장자가 목록에 없으면
#   압축파일 자체가 안 걸려 **안에 든 문서가 통째로 빠진다**. 실측에서 압축만
#   있는 폴더가 0건으로 나왔다. 반대로 압축이 걸리기만 하면 내부 파일은 이
#   패턴을 거치지 않으므로(이미 '펼쳐진 대상'이다) 안쪽 문서는 그대로 다 분류된다.
#
#   [별표를 꼭 붙인다] 패턴은 파일 '이름 전체'와 맞춰 보므로 `.hwp` 라고만 적으면
#   이름이 정확히 ".hwp" 인 파일만 찾는다 — 한 건도 안 걸린다.
DEFAULT_GLOB = ("*.txt,*.html,*.md,*.doc,*.docx,*.ppt,*.pptx,"
                "*.xls,*.xlsx,*.hwp,*.hwpx,*.pdf,"
                # 압축 — tar.gz·tar.bz2·tar.xz 는 뒤 확장자(*.gz·*.bz2·*.xz)로 걸린다.
                "*.zip,*.7z,*.rar,*.tar,*.tgz,*.gz,*.bz2,*.xz")


#------------------------------------------------------------------
# 파일 종류 패턴에서 '별표 빠뜨린 조각' 찾기
#=> 패턴은 파일 '이름 전체'와 맞춰 본다. 그래서 `.hwp` 라고만 적으면 이름이
#   정확히 ".hwp" 인 파일만 찾아 **한 건도 안 걸린다**. 그런데 화면에는 아무
#   말도 안 나와, 관리자는 "왜 0건이지" 하고 폴더부터 의심하게 된다.
#   실제로 `.hwp,.doc,` 라고 적힌 화면을 봤다(18건짜리 폴더에서 0건).
#
# -in: pattern = 사용자가 적은 파일 종류 문자열(쉼표로 나열)
#
# -out: list = 별표가 빠진 조각들(예: [".hwp", ".doc"]) · 없으면 빈 목록
# -out: error = 없음(빈 문자열·None 도 빈 목록)
#------------------------------------------------------------------
def glob_missing_star(pattern):
    out = []
    for part in (pattern or "").split(","):
        part = part.strip()
        # 점으로 시작하는데 별표가 없으면 확장자만 적은 것이다.
        if part.startswith(".") and "*" not in part:
            out.append(part)
    return out


#------------------------------------------------------------------
# 탐색기 창을 띄워 폴더 고르기
#=> 경로를 손으로 치면 오타가 나고, 탐색기 주소창에서 복사하면 따옴표가 딸려
#   오기도 한다. 이 화면은 관리자 PC 에서 도니까(localhost) 그 PC 의 탐색기
#   창을 띄워 고르게 한다.
#    1) tkinter 로 빈 창을 하나 만들고 숨긴다(폴더 대화상자만 필요하다)
#    2) 항상 위(topmost)로 올린다 — 안 그러면 브라우저 뒤에 숨어서
#       "버튼을 눌렀는데 아무 일도 안 난다"처럼 보인다
#    3) 고르면 그 경로를, 취소하면 None 을 돌려준다
#
#   [화면이 다른 PC 에서 돌면] 대화상자는 '화면이 도는 PC' 에 뜬다 — 보는 사람
#   눈앞이 아니다. 그래서 창을 못 만들면(화면 없는 서버) 조용히 실패로 돌리고,
#   호출부가 "직접 입력하세요"라고 안내한다.
#
# -in: initial = 처음 열 폴더(없거나 잘못된 경로면 무시)
#
# -out: (path, err) = 고른 폴더 경로(취소·실패면 None) 와 실패 사유(없으면 None)
# -out: error = 예외를 올리지 않는다 — 모든 실패를 err 문자열로 돌려준다
#------------------------------------------------------------------
def pick_folder(initial=""):
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as e:                      # noqa: BLE001
        return None, f"이 PC 에서 폴더 창을 띄울 수 없습니다({e})"
    root = None
    try:
        root = tk.Tk()
        root.withdraw()                          # 빈 창은 보이지 않게
        # 브라우저 뒤에 숨으면 누른 줄도 모른다 — 항상 맨 앞으로.
        root.wm_attributes("-topmost", 1)
        start = initial if initial and os.path.isdir(initial) else None
        got = filedialog.askdirectory(title="분류할 폴더 고르기", initialdir=start,
                                      mustexist=True)
        return (os.path.normpath(got) if got else None), None
    except Exception as e:                       # noqa: BLE001
        return None, f"폴더 창을 열지 못했습니다({e})"
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:                    # noqa: BLE001
                pass                             # 창 정리 실패는 화면과 무관하다


#------------------------------------------------------------------
# 등급 스트림 체크 상자 → 분류 명령 인자
#=> "문서 파일에 등급 쓰기" 와 그 하위 선택 두 개를 엔진 인자로 바꾼다.
#   설계: plan/등급스트림-기록-옵션-설계-20261019.html
#    1) 쓸 수 없는 조건(보안등급 축이 꺼짐 · Windows 가 아님)이면 [] — 엔진이
#       1012 로 멈추거나 쓸 등급이 없다
#    2) 켰으면 --write-grade-stream, 하위 선택에 따라
#       --grade-stream-unclassified none · --grade-stream-update-time 을 덧붙인다
#    3) 끈 상태에서는 하위 선택이 켜져 있어도 넘기지 않는다(엔진이 1003 으로 멈춘다)
#
# -in: want       = 등급 쓰기 체크(기본 켬)
# -in: none       = 미분류(NONE) 쓰기 체크
# -in: update     = 수정한 날짜 바뀌게 두기 체크
# -in: do_sec     = 보안등급 축을 돌리는가
# -in: is_windows = Windows 인가(기본: 지금 실행 중인 OS)
#
# -out: list = 덧붙일 인자(쓸 수 없거나 껐으면 빈 리스트)
# -out: error = 없음
#------------------------------------------------------------------
def grade_stream_args(want, none=False, update=False, do_sec=True, is_windows=None):
    # ADS 는 NTFS 에만 있다 — 리눅스에서 넘기면 엔진이 문서를 읽기 전에 멈춘다
    if is_windows is None:
        is_windows = os.name == "nt"
    if not (want and do_sec and is_windows):
        return []
    args = ["--write-grade-stream"]
    if none:
        args += ["--grade-stream-unclassified", "none"]
    if update:
        args += ["--grade-stream-update-time"]
    return args


#------------------------------------------------------------------
# 사람 결정 목록 → 분류 명령 인자
#=> 검토함 결정 파일(cso_override.jsonl)을 엔진이 따르게 --grade-overrides 로 넘긴다.
#    1) 끔 · 경로 없음 · 파일이 아직 없음(결정이 한 건도 없음) → [] — 없는 파일을 주면
#       엔진이 2009 로 멈추므로 넘기지 않는다
#    2) 그 밖 → ["--grade-overrides", 경로]
#
# -in: on   = 체크 상자(보안등급 축이 꺼졌으면 False 로 넘긴다)
# -in: path = 결정 목록 경로(설정의 override)
#
# -out: list = 덧붙일 인자
# -out: error = 없음
#------------------------------------------------------------------
def grade_overrides_args(on, path):
    # 결정이 아직 없으면 따를 것도 없다 — 없는 파일을 넘기면 엔진이 멈춘다
    if not on or not path or not os.path.isfile(path):
        return []
    return ["--grade-overrides", path]


#------------------------------------------------------------------
# 보안등급 기준 파일 → 분류 명령 인자
#=> 화면이 고치는 cso_rule.yaml 을 엔진도 읽게 --cso-rules 로 넘긴다. 넘기지 않으면
#   엔진은 exe 옆 cso_rule.yaml 을 읽어, 화면에서 고친 기준이 분류에 반영되지 않는다.
#    1) 설정의 rules 경로에 파일이 있으면 ["--cso-rules", 경로]
#    2) 비었거나 파일이 없으면 [] — 없는 경로를 주면 엔진이 오류(2001)로 멈추므로,
#       그때는 지금까지처럼 exe 옆 규칙으로 돌게 둔다
#
# -in: paths = 화면 경로 묶음(dict, "rules" 키를 본다)
#
# -out: list = 덧붙일 인자(파일이 없으면 빈 리스트)
# -out: error = 없음
#------------------------------------------------------------------
def cso_rules_args(paths):
    rules_path = (paths or {}).get("rules") or ""
    # 파일이 있을 때만 넘긴다 — 폴더나 없는 경로는 엔진을 멈추게 한다.
    if os.path.isfile(rules_path):
        return ["--cso-rules", rules_path]
    return []


#------------------------------------------------------------------
# 설정 ④ 분류 실행 렌더 (설계서 10장)
#=> 고른 폴더를 실제로 분류한다. 예전에는 실행 명령·PYTHONPATH 같은 개발용 입력칸이
#   화면에 있었는데, 실무 관리자가 정할 값이 아니라서 전부 감추고 **체크박스 3개**만
#   남겼다. 체크박스가 곧 CLI 옵션(--axis / --doc-rules / 전파)이 된다.
#   보안등급 기준 파일은 체크박스와 상관없이 --cso-rules 로 넘긴다(2026-09-29) —
#   화면이 고치는 파일과 엔진이 읽는 파일이 달라지지 않게 하려는 것이다.
#    1) 무엇을 판정할지 고른다(보안등급 · 업무분류 · 비슷한 문서 참고)
#    2) csoclassify 를 한 번만 실행한다 — 엔진이 분류하면서 보류 문서 전파까지
#       그 자리에서 끝내므로, 결과 파일을 다시 읽는 2차 패스는 두지 않는다
#    3) 끝나면 결과를 곧바로 읽어 현황 화면으로 보낸다
#
# -in: grades_path = 결과를 저장(=다른 화면이 읽을) 경로
# -in: paths       = 화면 경로 묶음(폴더 값을 여기에 되돌려 준다)
# -in: tax         = 회사 분류 체계(None 이면 업무분류 체크박스를 끈 채 보여준다)
# -in: records     = 지난 분류 결과(폴더 칸 아래 '지난 결과 건수' 표시에만 쓴다)
#
# -out: 없음(실행 시 결과 파일 생성 + rerun)
# -out: error = 실패는 화면에 표시(예외를 올리지 않는다)
#------------------------------------------------------------------
def render_run_panel(grades_path, paths=None, tax=None, records=None):
    cfg = st.session_state.get("seedcfg", {})
    paths = paths if paths is not None else st.session_state.get("paths", {})

    # 분류할 폴더 — 예전에는 맨 위 ① 에 따로 있었다. 실행 직전에 "어느 폴더를"
    # 다시 확인하려면 화면을 위로 한참 올라가야 했고, 아래 명령줄의 --dir 이
    # 가리키는 폴더와 눈으로 잇기도 어려웠다. 실행 칸 안으로 들여왔다.
    # 지난 실행에서 탐색기로 고른 폴더가 있으면, 칸을 만들기 '전에' 넣어 준다.
    # 위젯이 만들어진 뒤에 그 key 를 건드리면 Streamlit 이 예외를 낸다.
    # value= 와 session_state 를 함께 주면 Streamlit 이 "둘 다 설정됐다"고 경고한다.
    # 처음 한 번만 심어 두고, 그 뒤로는 위젯이 자기 값을 들고 있게 한다.
    if "set_folder" not in st.session_state:
        st.session_state["set_folder"] = paths.get("folder", "") or ""
    _picked = st.session_state.pop("_picked_folder", None)
    if _picked:
        st.session_state["set_folder"] = _picked
    fc1, fc2 = st.columns([1, 0.2])
    folder = fc1.text_input("분류할 폴더",
                            placeholder="예: D:\\collected", key="set_folder")
    # 입력칸의 이름표 높이만큼 내려 버튼을 칸과 같은 줄에 맞춘다.
    fc2.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
    if fc2.button("📂 폴더 선택", key="set_folder_pick",
                  help="이 화면이 도는 PC 의 탐색기 창이 열립니다"):
        got, err = pick_folder(folder)
        if err:
            st.warning(f"{err} — 경로를 직접 입력해 주세요.")
        elif got:
            # 고른 값은 다음 실행에서 칸에 넣는다(위 주석의 이유).
            st.session_state["_picked_folder"] = got
            st.rerun()
    paths["folder"] = folder
    if folder and not os.path.isdir(folder):
        st.warning("그런 폴더가 없습니다. 경로를 확인해 주세요.")
    elif folder:
        # '이 폴더에 몇 개가 있나'를 직접 센다(지난 결과 건수와 헷갈리지 않게).
        n_files, capped = gerunner.count_files(folder, recursive=True)
        st.caption(f"이 폴더에 **{n_files:,}개{' 이상' if capped else ''}**의 파일이 "
                   "있습니다 · 하위 폴더까지 모두 검사합니다.")
        if records:
            st.caption(f"↳ 지난 분류 결과 파일에는 {len(records):,}개 문서가 담겨 있습니다"
                       " — 다시 분류하면 이 폴더 기준으로 새로 만들어집니다.")

    c1, c2, c3 = st.columns(3)
    do_sec = c1.checkbox(f"{W.AXIS_SEC} 판정", value=True, key="run_sec",
                         help="개인정보·키워드·폴더·파일 이름을 검사합니다")
    do_doc = c2.checkbox(f"{W.AXIS_DOC} 제안", value=tax is not None, key="run_doc",
                         disabled=tax is None,
                         help="회사 분류 체계를 연결해야 켤 수 있습니다")
    # 이 체크는 '2단계를 한 번 더 돌릴까'가 아니라 '전파를 할까 말까'다.
    # 엔진(Python·Rust 모두)은 분류하면서 그 자리에서 보류 문서를 기준 문서와
    # 비교해 구제한다 — 끄면 --rule-only 로 그 과정을 통째로 뺀다.
    do_prop = c3.checkbox("비슷한 문서 참고(기준 문서와 비교)", value=True, key="run_prop",
                          help="판단하지 못한 문서를 이미 등록된 '기준 문서'와 비교해 "
                               "구제합니다(분류하면서 함께 처리됩니다). 끄면 규칙만으로 "
                               "판정해 가장 빠릅니다")

    # 업무분류를 '규칙 없이 기준 문서만으로' 돌리는 선택. 규칙을 아직 안 만들었거나
    # 규칙이 못 잡는 문서가 많을 때, 사람이 확정해 둔 기준 문서만 잣대로 쓴다.
    # 세 조건이 모두 맞아야 뜻이 있으므로, 하나라도 어긋나면 끄고 이유를 말한다.
    n_dt_seed = count_doctype_seeds(cfg.get("seed_path"))
    dt_ready = bool(do_doc and do_prop and n_dt_seed)
    # 이 체크는 업무분류에 딸린 선택이므로 반드시 '업무분류 제안' 바로 밑(c2)에
    # 그린다. 화면 전체 폭에 그리면 왼쪽 끝에 붙어 보안등급 칸의 하위 항목처럼
    # 보인다 — 어느 축에 딸린 선택인지 화면이 거짓말을 하게 된다.
    dt_vec_only = c2.checkbox(
        "└ 규칙 없이 **기준 문서만으로** 분류",
        value=False, key="run_dtvec", disabled=not dt_ready,
        help="업무분류 1차(규칙 검사)를 건너뛰고, 관리자가 등록한 기준 문서와 "
             "비슷한 정도만으로 분류합니다. 보안등급 판정은 영향을 받지 않습니다")
    # 설명은 폭이 좁은 칸에서 여러 줄로 접히므로 아래 전체 폭에 한 줄로 둔다.
    if not dt_ready:
        why = ("업무분류를 켜야 고를 수 있습니다" if not do_doc else
               "‘비슷한 문서 참고’를 켜야 고를 수 있습니다" if not do_prop else
               "업무분류가 확정된 기준 문서가 아직 없습니다 — 문서함에서 먼저 등록하세요")
        c2.caption(f"　↳ {why}")
    elif dt_vec_only:
        st.caption(f"　↳ {W.AXIS_DOC}: 기준 문서 **{n_dt_seed}건**만 잣대로 씁니다 · "
                   "규칙에 없는 문서도 잡히지만, 기준 문서와 안 닮은 문서는 "
                   "분류되지 않습니다")

    # ── 체크박스 → CLI 인자 ────────────────────────────────────────────
    # 한쪽 축만 켜면 --axis 로 그 축만 돌린다(업무분류만 돌리면 가장 무거운
    # 개인정보 검출을 통째로 건너뛰어 크게 빨라진다, 상위 설계 6-4).
    extra = []
    # 문서 내용의 지문(SHA-256). 기준 문서가 "등록할 때의 그 원본"인지 확인하는
    # 유일한 근거다 — 어차피 전 문서를 여는 김에 함께 구하므로 값이 싸다.
    extra += ["--hash"]
    if do_doc and tax:
        extra += ["--doc-rules", paths.get("doc_rules") or ""]
    # 보안등급 규칙도 화면이 고치는 그 파일로 못박는다(없으면 exe 옆 규칙으로 돈다).
    rules_args = cso_rules_args(paths)
    extra += rules_args
    if not rules_args and do_sec:
        st.caption("　↳ 보안등급 기준 파일이 설정 위치에 없어 실행 파일 옆 `cso_rule.yaml` 을 씁니다"
                   " — 고급 ▸ 파일 위치에서 확인하세요")
    if do_sec and not do_doc:
        extra += ["--axis", "security"]
    elif do_doc and not do_sec:
        extra += ["--axis", "doctype"]

    if dt_vec_only and dt_ready:
        # 업무분류 1차(규칙)를 건너뛴다. 엔진은 규칙 목록만 비우고 embed 임계값·
        # conflict 전략은 doc_rule.yaml 에 적힌 그대로 쓴다.
        extra += ["--doctype-vector-only"]

    if not do_prop:
        # 전파를 끄는 유일한 스위치. 이걸 주면 엔진이 임베딩·전파를 아예 하지
        # 않는다(기준 문서 파일이 옆에 있어도 무시).
        extra += ["--rule-only"]

    # 기준 문서 저장소를 'UI 가 관리하는 파일 하나'로 못박는다.
    # 이 인자를 주지 않으면 엔진은 조용히 실행 파일 옆의 class_seed.jsonl 을 쓴다 —
    # UI 에서 등록한 기준 문서가 다음 분류에 전혀 반영되지 않던 원인이었다.
    sp = cfg.get("seed_path") if do_prop else None
    if sp and not os.path.isfile(sp):
        # 아직 등록한 기준 문서가 없어도 빈 파일을 만들어 둔다. 파일이 없으면
        # 엔진이 다시 실행 파일 옆 저장소로 되돌아가기 때문이다(빈 파일은
        # seed=0 으로 정상 동작한다 — 기준 문서 없이 규칙만으로 분류).
        try:
            os.makedirs(os.path.dirname(os.path.abspath(sp)), exist_ok=True)
            open(sp, "a", encoding="utf-8").close()
        except OSError as e:
            st.warning(f"기준 문서 저장소를 만들지 못했습니다({e}) — 이번 실행은 "
                       "실행 파일 옆 저장소를 쓸 수 있습니다.")
            sp = None
    if sp:
        extra += ["--seeds", sp]
        n_seed = len(seedstore.load_seeds(sp))
        st.caption(f"기준 문서 **{n_seed}건**을 잣대로 씁니다 · `{sp}`"
                   + ("" if n_seed else " — 아직 등록된 기준 문서가 없어 "
                                        "이번에는 규칙만으로 분류합니다"))

    # 연동용 축약 파일. 분류 결과 자체를 --simple 로 바꾸면 이 화면이 못 읽는다
    # (문서함·검토함은 signals·labels·confidence 를 본다). 그래서 전체 결과는 그대로
    # 두고, 옆에 축약본을 한 벌 더 만든다.
    want_simple = st.checkbox("연동용 **--simple 형식 파일**도 만들기",
                              value=False, key="run_simple",
                              help="문서명·등급·해시(+업무분류 dc_id)만 담은 파일을 "
                                   "분류 결과 옆에 나란히 만듭니다. 다른 시스템에 넘길 때 씁니다")

    # 등급 스트림 기록(설계: plan/등급스트림-기록-옵션-설계-20261019.html).
    # 분류한 문서의 NTFS 대체 스트림(:mgrade)에 최종 등급을 써 두면 PC 보안
    # 에이전트가 그 값으로 열기·첨부를 통제하고 탐색기가 표식을 단다. 기본으로 켠다.
    # 보안등급 축이 돌아야 쓸 등급이 있고, ADS 는 Windows(NTFS)에만 있다.
    gs_ok = bool(do_sec) and os.name == "nt"
    want_gs = st.checkbox(
        "문서 파일에 **등급 쓰기**(--write-grade-stream)",
        value=True, key="run_grade_stream", disabled=not gs_ok,
        help="분류한 문서마다 숨은 등급 표식(:mgrade, 12바이트)을 씁니다. 문서 내용은 "
             "바뀌지 않고, 이미 같은 등급이면 다시 쓰지 않습니다. 압축 안 문서는 건너뛰고 "
             "압축 파일 자체에 집계 등급을 씁니다")
    gs_none = gs_time = False
    if not gs_ok:
        st.caption("　↳ " + ("보안등급을 켜야 고를 수 있습니다" if os.name == "nt"
                             else "Windows(NTFS)에서만 쓸 수 있습니다"))
    elif want_gs:
        # 하위 선택 두 개는 '등급 쓰기'에 딸린 것이라 바로 밑에 들여 쓴다.
        g1, g2 = st.columns(2)
        gs_none = g1.checkbox(
            "└ 등급을 못 정한 문서에 **미분류(NONE)** 쓰기",
            value=False, key="run_gs_none",
            help="끄면(기본) 등급을 못 정한 문서는 건드리지 않습니다(에이전트에는 '마킹 없음'). "
                 "켜면 '미분류'로 써서 에이전트의 미분류 정책을 따르게 합니다")
        gs_time = g2.checkbox(
            "└ 수정한 날짜 **바뀌게** 두기",
            value=False, key="run_gs_time",
            help="끄면(기본) 등급을 써도 문서의 생성·수정 시각을 그대로 둡니다. 켜면 "
                 "수정한 날짜가 바뀌는 대신, 에이전트가 이미 판별해 둔 문서도 새 등급을 바로 봅니다")
    extra += grade_stream_args(want_gs, gs_none, gs_time, do_sec=do_sec)

    # 사람이 정한 등급(설계: plan/사람등급지정-수동오버라이드-설계-20261020.html §07).
    # 검토함 결정(cso_override.jsonl)을 엔진이 따르게 해, 다시 분류해도 사람 결정이
    # 판정에 덮어써지지 않고 파일 등급 스트림에도 사람 등급이 써진다. 기본으로 켠다.
    want_ov = st.checkbox(
        "**사람이 정한 등급** 따르기(--grade-overrides)",
        value=True, key="run_grade_overrides", disabled=not do_sec,
        help="검토함에서 담당자가 정한 보안등급이 자동 판정을 이깁니다. 결정 뒤 문서 내용이 "
             "바뀌었으면 더 엄격한 결정만 따르고, 나머지는 검토함에 다시 올립니다")
    extra += grade_overrides_args(want_ov and do_sec, paths.get("override"))

    # ── 실행할 명령 ────────────────────────────────────────────────────
    # 체크박스 바로 아래에 둔다 — 체크를 켜고 끌 때 명령이 그 자리에서 같이
    # 바뀌는 것이 보여야, 어느 체크가 어느 인자인지 눈으로 배울 수 있다.
    # 손으로 다시 적지 않고 gerunner.build_classify_args 를 부르는 것이 핵심이다:
    # 실행하는 쪽과 같은 함수라 화면과 실제가 어긋날 수 없다.
    ok_axis = bool(do_sec or do_doc)
    ok_folder = bool(folder) and os.path.isdir(folder)
    # 파일 종류 칸은 아래에 있지만 명령에는 지금 값이 들어가야 한다. 위젯이
    # 자기 값을 key 로 들고 있으므로 그것을 읽는다(처음에는 아직 없어 "*").
    _pat = (st.session_state.get("run_pattern") or DEFAULT_GLOB)
    if not ok_axis:
        st.warning("보안등급과 업무분류 중 적어도 하나는 켜야 합니다 — "
                   "둘 다 끄면 실행할 것이 없습니다.")
    elif not folder:
        st.info("위 **분류할 폴더** 에 경로를 넣으면 실행할 명령이 여기에 나옵니다.")
    elif not ok_folder:
        # 폴더가 잘못됐다는 말은 바로 위 칸에서 이미 했다 — 같은 말을 두 번 하지 않는다.
        st.caption("폴더 경로를 고치면 실행할 명령이 여기에 나옵니다.")
    else:
        _base = gerunner.parse_base_cmd(cfg.get("csoclassify_cmd"))
        st.caption("이 버튼이 실행하는 명령 — 명령창에서 그대로 쳐도 결과는 같습니다")
        st.code(" ".join(gerunner.build_classify_args(
            _base or ["MpowerClassify"], folder, grades_path, glob=_pat,
            extra_args=extra)), language="text")
        st.caption("↳ 위 체크 상자들이 그대로 인자가 됩니다 — `--axis`(한 축만 켤 때) · "
                   "`--cso-rules`(보안등급 기준) · `--doc-rules`(업무분류) · `--doctype-vector-only` · "
                   "`--rule-only`(비슷한 문서 참고 끔) · `--seeds`(기준 문서) · "
                   "`--write-grade-stream`(문서 파일에 등급 쓰기) · "
                   "`--grade-overrides`(사람이 정한 등급 따르기). "
                   "`--embed-needed` 는 판단 못 한 문서만 임베딩해 빠르게 도는 기본값입니다.")
        if want_simple:
            st.caption(f"↳ 축약본은 분류가 끝난 뒤 `{simple_path(grades_path)}` 에 "
                       "만듭니다. 방금 만든 결과에서 필요한 칸만 뽑아 쓰므로 **문서를 "
                       "다시 읽지 않습니다**(엔진의 `--simple` 과 같은 내용). "
                       "명령창에서 직접 만들려면 위 명령에 `--simple` 을 붙이고 "
                       "`--out` 을 다른 파일로 주세요 — 그 파일은 이 화면이 읽지 못합니다.")

        # 이 명령을 복사해 자동화에 쓰는 사람을 위한 경고. 이 화면은 이미 폴더를
        # 한 번에 돌리지만(--dir), 남이 옮겨 쓸 때 파일마다 부르는 실수를 자주 한다.
        # 실행할 때마다 임베딩 모델을 다시 읽으므로 문서 수만큼 그 값을 문다.
        st.warning("**여러 문서는 반드시 한 번에 묶어서 부르세요.** 위 명령처럼 "
                   "`--dir` 로 폴더를 통째로 주면 임베딩 모델을 **한 번만** 읽습니다. "
                   "파일마다 따로 부르면(`--file` 반복) 문서 수만큼 모델을 다시 읽어 "
                   "**5.7배** 느려집니다 — 실측 6건 기준 1.1초 → 6.6초, 20건이면 4초 → 22초. "
                   "이 화면은 이미 묶어서 실행합니다(자동화·연동할 때 지켜 주세요).")

    # (하위 폴더를 항상 훑는다는 사실은 폴더 칸 바로 아래에서 이미 말했다.
    #  두 엔진 모두 --dir 이면 언제나 재귀라 끌 수 있는 값이 아니다.)
    # 기본값을 '전체(*)' 가 아니라 실제로 다룰 수 있는 확장자 목록으로 채워 둔다.
    # 관리자가 직접 적다가 별표를 빼먹으면(`.hwp,.doc`) 한 건도 안 걸리는데,
    # 화면에는 아무 말도 안 나와 원인을 찾기 어렵다.
    pattern = st.text_input("파일 종류", value=DEFAULT_GLOB, key="run_pattern",
                            help="쉼표로 나열합니다. 확장자 앞에 `*` 를 꼭 붙이세요 — "
                                 "`*.hwp` 는 되지만 `.hwp` 는 한 건도 안 걸립니다. "
                                 "전체를 보려면 `*` 하나만 두세요.\n\n"
                                 "압축(zip·7z·rar·tar·gz…)을 빼면 **압축 안의 문서가 "
                                 "통째로 빠집니다** — 압축파일 자체가 대상에 안 걸리기 "
                                 "때문입니다. 걸리기만 하면 안쪽 문서는 이 패턴과 무관하게 "
                                 "모두 분류됩니다.") or "*"
    # 별표를 빠뜨린 조각이 있으면 알려 준다 — 조용히 0건이 되는 것을 막는다.
    _bad = glob_missing_star(pattern)
    if _bad:
        st.warning("확장자 앞에 `*` 가 빠졌습니다: " + ", ".join(f"`{b}`" for b in _bad) +
                   " → `" + ", ".join("*" + b for b in _bad) + "` 처럼 적어야 "
                   "파일이 걸립니다(지금 그대로면 한 건도 안 잡힙니다).")

    # 위에서 이미 이유를 말했으므로 여기서는 조용히 멈춘다(같은 경고를 두 번 내지 않는다).
    if not (ok_axis and ok_folder):
        return

    if st.button("▶ 분류 시작", type="primary", key="run_go"):
        # 진행 규모 안내용으로 대상 파일 수를 미리 센다(CLI 와 동일하게 콤마·중괄호
        # 다중 패턴을 펼쳐 각각 매칭 후 중복 제거).
        matched = set()
        for gp in gerunner.expand_glob_patterns(pattern):
            # 실행이 항상 재귀이므로 미리 세는 쪽도 재귀여야 한다 — 안 그러면
            # "18개 파일을 분류합니다" 라고 알린 뒤 100개를 분류하는 일이 생긴다.
            pat = os.path.join(folder, "**", gp)
            matched.update(f for f in globmod.glob(pat, recursive=True) if os.path.isfile(f))
        n = len(matched)
        if n == 0:
            st.warning("패턴에 맞는 파일이 없습니다.")
            return
        st.info(f"{n}개 파일을 분류합니다 — 규칙·폴더·파일 이름으로 확실히 정해진 문서는 "
                "임베딩을 건너뛰어 빠릅니다.")
        base = gerunner.parse_base_cmd(cfg.get("csoclassify_cmd"))

        # (1) 분류 — 규칙/경로/파일명 + 선택적 임베딩(보류·seed_eligible). 진행바 실시간 표시
        prog = st.progress(0.0, text=f"분류 준비 중… (0/{n})")
        start = time.perf_counter()

        # 초 → "m분 ss초"/"s초" 표기(경과·잔여 시간 표시용).
        _fmt = fmt_duration

        # csoclassify 가 파일 1개 끝낼 때마다 불려 진행바·경과·잔여시간을 갱신.
        def _on_prog(done, total, path=""):
            elapsed = time.perf_counter() - start
            # 첫 신호(0/N): 모델 로딩 구간이라 잠시 0% 에서 멈춰 보이는 게 정상.
            if done == 0:
                prog.progress(0.0, text=f"모델 로딩·첫 파일 준비 중… (0/{total}) · "
                                        f"첫 파일은 수십 초 걸릴 수 있어요")
                return
            frac = (done / total) if total else 0.0
            name = os.path.basename(path.rstrip()) if path else ""
            # 지금까지 속도로 남은 시간 추정(1건 이상 끝났을 때만).
            eta = (elapsed / done * (total - done)) if done else 0
            msg = f"분류 중… {done}/{total} ({int(frac * 100)}%) · 경과 {_fmt(elapsed)}"
            if done and done < total:
                msg += f" · 남은 시간 ≈ {_fmt(eta)}"
            if name:
                msg += f" · {name}"
            prog.progress(min(frac, 1.0), text=msg)

        try:
            r = gerunner.run_csoclassify_dir_stream(
                base, folder, grades_path, pattern,
                pythonpath=cfg.get("pythonpath"), on_progress=_on_prog,
                extra_args=extra)
        except Exception as e:
            uierrlog.show_error(f"실행 오류: {e}", exc=e, where="분류 실행")
            return
        if not os.path.isfile(grades_path):
            # 엔진이 계약대로 낸 오류가 있으면 그것이 가장 정확한 원인이다.
            # 없을 때만 예전처럼 stderr 뒤쪽을 보여 준다(진짜 원인이 앞쪽에 있는
            # 경우가 많아, 로그에는 언제나 전문을 남긴다).
            why = gerunner.error_line(getattr(r, "error", None))
            uierrlog.log_error("분류 결과 파일이 생성되지 않았습니다.\n"
                               f"오류: {why or '(계약 오류 없음)'}\n"
                               f"명령 stderr 전문:\n{r.stderr}", where="분류 실행")
            st.error("분류 결과 파일이 생성되지 않았습니다.\n"
                     + (why or r.stderr[-600:]))
            return
        total_elapsed = time.perf_counter() - start
        prog.progress(1.0, text=f"분류 완료 · {n}/{n} · 총 {_fmt(total_elapsed)}")
        st.success(f"분류 완료 · {r.summary or 'OK'} · 소요 {_fmt(total_elapsed)}")

        # 연동용 축약본(--simple 형식). 방금 만든 결과에서 뽑으므로 값이 싸다.
        if want_simple:
            try:
                _recs = load_records(grades_path, os.path.getmtime(grades_path))
                _sp, _n = write_simple_file(grades_path, _recs)
                st.success(f"연동용 축약 파일 {_n}건 · `{_sp}`")
            except (OSError, ValueError, json.JSONDecodeError) as e:
                uierrlog.show_error(f"축약 파일 저장 실패: {e}", exc=e,
                                    where="--simple 파일 저장")

        # 분류 결과와 기준 문서를 대조해, 원본이 바뀌었거나 사라진 기준 문서에
        # '확인 필요' 표시를 붙인다(지우지는 않는다 — 사람이 판단할 일이다).
        _sync_seeds_after_run(cfg, grades_path)

        # 예전에는 여기서 --propagate 로 결과 파일을 다시 읽어 한 번 더 전파했다.
        # 엔진이 분류하면서 이미 같은 일을 끝내 놓기 때문에(Python cli.py 의
        # auto_prop, Rust main.rs 의 auto_prop 둘 다) 두 번째 패스는 같은 문서를
        # 다시 읽고 다시 쓰는 중복이었다. 없앴다.

        # 총 실행시간을 사이드카에 남겨, 대시보드가 표기하게 한다.
        run_seconds = time.perf_counter() - start
        try:
            save_run_meta(grades_path, run_seconds, n, do_prop)
        except OSError:
            pass  # 부가정보라 저장 실패해도 실행은 정상 처리

        # 끝나면 곧바로 현황으로 보낸다 — "다 됐는데 이제 뭘 누르지"를 없앤다.
        goto(menu=MENU_HOME)


#------------------------------------------------------------------
# 규칙 설정 화면 렌더 (STEP 4 / Phase 0)
#=> cso_rule.yaml 을 UI에서 편집한다. 기밀사전(키워드)·경로·bulk 임계값을 고치고,
#   샘플 텍스트로 미리보기한 뒤, 버전을 자동 상향하며 저장(백업)한다. 정규식 PII 는
#   위험도가 커서 읽기 전용으로 두고, 패턴은 YAML 직접 편집하도록 안내한다.
#
# -in: 없음(설정은 st.session_state["rulescfg"])
# -out: 없음(저장 시 파일 기록 + rerun)
# -out: error = 로드/미리보기 실패는 화면에 표시
#------------------------------------------------------------------
def render_rules_editor():
    cfg = st.session_state.get("rulescfg", {})
    path = cfg.get("rules_path")
    if not path or not os.path.isfile(path):
        st.warning("아래 **고급**에서 보안등급 기준 파일의 위치를 확인해 주세요.")
        return

    try:
        doc = rulesedit.load_doc(path)
    except Exception as e:
        uierrlog.show_error(f"보안등급 기준을 읽지 못했습니다: {e}", exc=e, where="규칙셋 읽기")
        return

    st.caption(f"기준 파일 `{os.path.abspath(path)}` · 버전 `{doc.get('version')}`")
    n1, n2 = st.columns(2)
    bulk = n1.number_input("bulk 임계값 (PII 가 이 건수 이상이면 등급 상향)",
                           min_value=1, value=int(doc.get("defaults", {}).get("bulk_threshold", 5)))
    # 검토함 편입선 — 등급을 바꾸지 않고 '사람이 볼지'만 가른다. 값을 올리면
    # 검토할 문서가 늘고, 내리면 준다. 예전에는 코드에 박혀 있어 배포해야 고쳤다.
    review = n2.number_input(
        "검토함 편입선 (확신이 이 값보다 낮으면 사람이 확인)",
        min_value=0.05, max_value=1.0, step=0.05,
        value=float(doc.get("defaults", {}).get("review_threshold", LOW_CONF)),
        help="등급(C/S/O)은 이 값과 무관합니다 — '사람이 한 번 볼까'만 정합니다. "
             "올리면 검토할 문서가 늘고, 내리면 줄어듭니다.")
    # 개인정보만으로 올릴 수 있는 최고 등급 — 명단 속 주민번호만으로 C 가 되는 것을
    # 막고 싶은 고객용. 키워드·스탬프 등 다른 근거가 낸 C 는 그대로다.
    cap_opts = {"제한 없음 (C 까지)": None, "S 까지": "S", "O 까지": "O"}
    cur_cap = (doc.get("defaults") or {}).get("pii_grade_cap")
    cap_labels = list(cap_opts)
    # 파일에 C 가 적혀 있으면 '제한 없음'과 같은 뜻이라 그 칸을 고른다.
    cap_idx = next((i for i, k in enumerate(cap_labels) if cap_opts[k] == cur_cap), 0)
    pii_cap = cap_opts[st.selectbox(
        "개인정보만으로 올릴 수 있는 최고 등급", cap_labels, index=cap_idx,
        help="주민번호 대량·신원+금융 결합처럼 개인정보(PII)만 근거일 때 이 등급까지만 올립니다. "
             "기밀 표식·사내 규정 단어·스탬프 같은 다른 근거가 C 를 내면 그 C 는 그대로입니다. "
             "깎인 경우 결과 근거에 원래 등급(capped_from)이 남습니다.")]

    st.markdown("**이 말이 나오면 → 이 등급으로** (단어는 콤마로 구분)")
    st.caption("**본문 단어**는 문서 안을 보고, **파일명 단어**는 파일 이름만 봅니다. 파일명 칸을 비워 두면 그 규칙은 파일 이름을 보지 않습니다 — 본문 단어를 빌려 쓰지 않습니다. (예: 제품명은 본문에서만 보게 두면, 제품 매뉴얼이 이름 때문에 기밀이 되는 일이 없습니다.)")
    kdf = pd.DataFrame([{"id": k.get("id"), "name": k.get("name"),
                         "terms": ", ".join(k.get("terms") or []),
                         "filename": ", ".join(k.get("filename") or []),
                         "base_grade": k.get("base_grade"),
                         "seed": bool(k.get("seed_eligible"))} for k in doc.get("keywords", [])])
    kedit = st.data_editor(
        kdf, num_rows="dynamic", width="stretch", key="kw_editor",
        column_config={
            "id": st.column_config.TextColumn("id"),
            "name": st.column_config.TextColumn("이름"),
            "terms": st.column_config.TextColumn("본문 단어(콤마 구분)", width="large"),
            "filename": st.column_config.TextColumn("파일명 단어(콤마 구분)", width="medium"),
            "base_grade": st.column_config.SelectboxColumn("등급", options=["C", "S", "O"]),
            "seed": st.column_config.CheckboxColumn("기준 문서 후보"),
        })

    st.info("**폴더(경로)로 등급을 정하던 표는 없어졌습니다.** "
            "문서가 어디 놓여 있는지는 담당자가 옮기면 바뀌고, 실측에서 경로가 단독으로 "
            "정한 등급 8건이 전부 오탐이었습니다(제품명이 든 파일 이름·공개 데이터셋 폴더). "
            "보안 서버처럼 '여기 있는 것은 기본이 기밀'인 대상은 실행할 때 "
            "`--failsafe C` 로 지정하세요.", icon="ℹ️")

    st.markdown("**개인정보가 나오면 → 이 등급으로** (종류별 정책)")
    _labopts = sorted(set(rulesedit.kopii_labels()) |
                      {r.get("label") for r in doc.get("regex_pii", []) if r.get("label")})
    st.caption(f"검출은 **ko-pii** 가 담당합니다(패턴·체크섬·문맥·우회방어 내장). 여기선 "
               f"'label(ko-pii 유형) → 등급' 정책만 정합니다. 기본등급 → 상향등급 은 검출 건수가 "
               f"**bulk 임계값({int(bulk)}건)** 이상일 때입니다.")
    rdf = pd.DataFrame([{"id": r.get("id"), "name": r.get("name"),
                         "label": r.get("label"),
                         "base_grade": r.get("base_grade"), "bulk_grade": r.get("bulk_grade"),
                         "weight": r.get("weight", "medium"),
                         "seed": bool(r.get("seed_eligible"))}
                        for r in doc.get("regex_pii", [])])
    redit = st.data_editor(
        rdf, num_rows="dynamic", width="stretch", key="regex_editor",
        column_config={
            "id": st.column_config.TextColumn("id"),
            "name": st.column_config.TextColumn("이름"),
            "label": st.column_config.SelectboxColumn("ko-pii 유형", options=_labopts),
            "base_grade": st.column_config.SelectboxColumn("기본등급", options=["C", "S", "O"]),
            "bulk_grade": st.column_config.SelectboxColumn("상향등급", options=["C", "S", "O"]),
            "weight": st.column_config.SelectboxColumn("가중", options=["high", "medium", "low"]),
            "seed": st.column_config.CheckboxColumn("기준 문서 후보"),
        })

    # 편집 행(공셀 정리) 준비 — 미리보기·저장 공용.
    krows = kedit.fillna("").to_dict("records")
    rrows = redit.fillna("").to_dict("records")

    with st.expander("확신 계산값 — 읽기 전용"):
        st.caption("확신은 **검토함에 담기는 기준과 정렬**에만 쓰이고 등급 자체는 바꾸지 않습니다. "
                   "수정하려면 **cso_rule.yaml 을 직접 편집**하세요(이 표는 표기 전용).")
        conf = doc.get("confidence", {}) or {}
        # 규칙 파일의 confidence 블록에 실제로 있는 묶음만 보여 준다. 예전에는
        # ("regex", "keyword", "path") 로 박혀 있어, 없어진 path 는 빈 줄로 뜨고
        # sensitive·stamp 는 아예 안 보였다(2026-09-28 수정).
        st.dataframe(pd.DataFrame([{"신호": grp,
                                    "high": (conf.get(grp) or {}).get("high"),
                                    "medium": (conf.get(grp) or {}).get("medium"),
                                    "low": (conf.get(grp) or {}).get("low")}
                                   for grp in ("regex", "keyword", "sensitive", "stamp")]),
                     hide_index=True, width="stretch")
        st.caption(f"파일 이름 신호의 고정 확신값: {conf.get('name')}")

    st.divider()
    st.markdown("**미리보기 — 예시 글로 등급을 확인합니다(저장하지 않음)**")
    # 기본 샘플 = ko-pii 18종 PII 를 모두 검출하도록 실측으로 맞춘 예시(전부 가짜값).
    # ⚠ 라인 배치 주의: 차량번호(VEHICLE) 바로 뒤에 건강보험(1-2345678901)이 오면
    #   ko-pii 겹침해소로 차량이 사라져, 차량은 이메일·주소 사이에 떨어뜨려 둔다.
    _pii18_sample = (
        "[개인정보 예시 — 18종 PII 검출 확인용]\n"
        "성명 홍길동, 국적: 대한민국\n"
        "휴대전화 010-1234-5678\n"
        "이메일 hong@example.com\n"
        "차량번호 12가3456\n"
        "주소 서울특별시 강남구 테헤란로 152\n"
        "주민등록번호 900101-1234567\n"
        "외국인등록번호 900101-5234567\n"
        "여권번호 M12345678\n"
        "운전면허번호 11-12-345678-90\n"
        "신용카드 4111-1111-1111-1111\n"
        "계좌번호 국민은행 123456-04-123456\n"
        "사업자등록번호 124-81-00998\n"
        "법인등록번호 130111-0006246\n"
        "건강보험증번호 1-2345678901\n"
        "처방전번호 202401150001\n"
        "의약품 EDI코드 640500051\n"
        "사건번호 2023가단12345\n"
        "부동산 고유번호 1111010100100010001"
    )
    st.caption("기본 샘플은 ko-pii 18종 PII(주민·외국인·여권·면허·전화·이메일·주소·카드·계좌·"
               "사업자·법인·건강보험·처방전·의약품EDI·차량·사건번호·부동산·국적)를 모두 포함합니다.")
    sample = st.text_area("샘플 텍스트", value=_pii18_sample, height=320)
    if st.button("미리보기 실행"):
        preview_doc = rulesedit.load_doc(path)      # 원본 사본에 편집 반영(디스크 미변경)
        rulesedit.apply_all(preview_doc, bulk, krows, rrows, review, pii_cap)
        perrs, pwarn = rulesedit.validate_regex_rules(preview_doc)
        if perrs:
            st.error("PII 라벨 오류 — 고친 뒤 다시: " + "; ".join(f"{i}: {m}" for i, m in perrs))
            return
        if pwarn:
            st.warning("경고: " + "; ".join(f"{i}: {m}" for i, m in pwarn))
        try:
            sig = rulesedit.preview_scan(preview_doc, sample, cfg.get("src_path"))
            st.success(f"등급: {W.grade_badge(sig.grade)} · 확신 {W.pct(sig.confidence)}")
            for h in sig.hits:
                ts = h.terms
                detail = ", ".join(f"{t}×{c}" for t, c in ts) if ts else f"{h.count}건"
                # PII 상한으로 깎였으면 원래 등급을 함께 보여 준다 — 설정이 듣는지 바로 보이게.
                capped = f" (상한 적용 · 원래 {h.capped_from})" if getattr(h, "capped_from", None) else ""
                st.write(f"- [{h.layer}] {h.name} → {h.grade}{capped} · {detail}")
            if not sig.hits:
                st.caption("걸린 규칙 없음")
        except Exception as e:
            uierrlog.show_error(f"미리보기 실패: {e}", exc=e, where="규칙셋 미리보기")

    st.divider()
    if st.button("💾 보안등급 기준 저장", type="primary", key="rules_save"):
        rulesedit.apply_all(doc, bulk, krows, rrows, review, pii_cap)
        errs, warns = rulesedit.validate_regex_rules(doc)
        if errs:
            st.error("저장 취소 — PII 라벨 오류: " + "; ".join(f"{i}: {m}" for i, m in errs))
            return
        if warns:
            st.warning("경고(저장은 진행됨): " + "; ".join(f"{i}: {m}" for i, m in warns))
        doc["version"] = rulesedit.bump_version(doc.get("version"))
        try:
            rulesedit.save_doc(path, doc)
            st.success(f"저장됨 · 새 버전 `{doc['version']}` (백업: {os.path.basename(path)}.bak)")
            st.rerun()
        except Exception as e:
            uierrlog.show_error(f"저장 실패: {e}", exc=e, where="규칙셋 저장")


#------------------------------------------------------------------
# 기본 파일 경로 묶음 만들기
#=> 화면이 쓰는 파일 경로(결과·수정기록·기준문서·규칙·분류체계)의 기본값을 한 번에
#   계산한다. 예전에는 이 값들이 사이드바 입력칸 7개로 늘 보였는데, 실무 관리자가
#   정할 값이 아니라서 설정 화면의 '고급'으로 내렸다(설계서 원칙 5).
#   경로는 반드시 ui 폴더 기준 '절대경로'로 만든다 — 상대경로면 streamlit 을 어느
#   폴더에서 띄웠느냐에 따라 결과가 엉뚱한 곳에 쓰이고 화면이 못 찾는다.
#
#   [2026-09-28 고침] 고객 파일의 기본 자리를 ui/policy 로 바꿨다. 예전에는
#   ui 바로 아래(ui/doc_rule.yaml)를 보고 없으면 resources/policy 로 떨어졌는데,
#   실제 고객 파일은 둘 중 어디도 아닌 ui/policy 에 있다. 그래서 화면이 고친
#   규칙이 제품 폴더(resources/policy)에 쓰이고, scripts/sync_policy.py 는
#   ui/policy 를 원본으로 보므로 다음 동기화 때 **조용히 되돌아갔다**.
#   지금 쓰는 자리에서는 settings.yaml 이 올바른 경로를 덮어써서 가려져 있었다.
#
# -in: 없음
#
# -out: dict = {result, override, seed, seed_audit, rules, export_input,
#               doc_rules, text_dir, folder, cmd, pythonpath}
# -out: error = 없음(파일이 없어도 경로 문자열은 만든다)
#------------------------------------------------------------------
def default_paths():
    here = os.path.dirname(os.path.abspath(__file__))
    result = next((p for p in (os.path.join(here, "cso_result.jsonl"),
                               os.path.join(here, "sample_cso_result.jsonl"))
                   if os.path.isfile(p)), os.path.join(here, "cso_result.jsonl"))
    # 고객 파일이 사는 자리. sync_policy.py 가 '원본'으로 보는 곳과 같아야 한다.
    ui_policy = os.path.join(here, "policy")

    #--------------------------------------------------------------
    # 고객 파일의 기본 경로 — 언제나 고객 폴더
    #=> 규칙·체계·기준 문서는 이 회사의 것이고, 화면이 고쳐 쓰는 대상이다.
    #   파일이 아직 없어도 고객 폴더 경로를 준다 — 제품 폴더로 떨어뜨리면
    #   첫 저장이 제품 폴더에 쓰이고, sync_policy.py 가 다음에 되돌린다.
    #
    # -in: name = 파일 이름(예: "doc_rule.yaml")
    #
    # -out: str = 절대경로(ui/policy 아래)
    # -out: error = 없음
    #--------------------------------------------------------------
    def _mine(name):
        return os.path.join(ui_policy, name)


    # 실행 명령 기본값 = 배포 exe. 아직 없으면 소스 모듈로 폴백한다.
    # UI 배포 묶음(scripts/build/ui-package.sh)은 엔진을 ..\engine 에 두므로 그것을 먼저 찾는다 —
    # 다른 PC 에서 처음 띄웠을 때 따로 설정하지 않아도 분류가 돌게 하려는 것이다.
    exe = next((p for p in (
        os.path.abspath(os.path.join(here, "..", "engine", "MpowerClassify-rs.exe")),
        os.path.abspath(os.path.join(here, "..", "dist-pkg", "MpowerClassify.exe")))
        if os.path.isfile(p)), "")
    if exe:
        cmd = f'"{exe}"' if " " in exe else exe
        pythonpath = ""
    else:
        cmd = f"{sys.executable} -m csoclassify"
        pythonpath = os.path.abspath(os.path.join(here, "..", "src"))

    return {
        "result": result,
        # 검토 이력은 정책이 아니라 화면이 쌓는 기록이라 ui 바로 아래 둔다.
        "override": os.path.join(here, "cso_override.jsonl"),
        # 기준 문서는 고객 파일이다 — 규칙·체계와 같은 폴더에 모은다.
        "seed": _mine("class_seed.jsonl"),
        "seed_audit": _mine("class_seed_audit.jsonl"),
        # 보안등급 규칙도 고객 파일이다(2026-09-28 결정) — 화면이 고치는 대상이라
        # 원본이 저장소 쪽이면 고친 것이 다음 동기화에 되돌아간다. 아직 복사해
        # 오지 않았으면 화면이 '고급에서 위치를 확인하라'고 안내한다.
        "rules": _mine("cso_rule.yaml"),
        # 분류 체계를 '다시 가져올' 때 읽는 원본 JSON(MpowerV11 내보내기 결과).
        "export_input": _mine("doc_classification_export.json"),
        "doc_rules": _mine("doc_rule.yaml"),
        # 추출 본문 폴더 — 분류를 돌릴 때 --textsave 로 남긴 자리. 단어 제안이
        # 문서 본문을 읽는 유일한 통로다(원본 파일을 다시 열지 않는다).
        "text_dir": os.path.join(here, "_extracted"),
        "folder": "",
        "cmd": cmd,
        "pythonpath": pythonpath,
    }


#------------------------------------------------------------------
# 업무분류 판단 기준 편집 렌더 (설계서 10-1 · 규칙 자동 생성 12장)
#=> 규칙 파일은 --build-doc-rule 로 만들고, 사람의 결정은 doc_rule.local.yaml 에만 둔다.
#   2026-09-22 부터 엔진은 옛 모양 규칙(규칙 파일을 사람이 직접 고치고 분류체계를
#   doc_taxonomy.yaml 로 따로 읽던 방식)을 읽지 않는다. 그래서:
#    1) 옛 모양 규칙 파일이 남아 있으면 — 분류가 멈춘다는 것을 알리고 [새 방식으로 바꾸기]만 보여 준다
#    2) 그 밖에는(새 방식이거나 아직 규칙이 없음) 새 방식 편집 화면을 그린다
#
# -in: path = doc_rule.yaml 경로
# -in: tax  = 회사 분류 체계(체계 JSON 에서 읽은 것)
#
# -out: 없음(Streamlit 출력)
# -out: error = 없음(실패는 화면에 표시)
#------------------------------------------------------------------
def render_doc_rules_editor(path, tax):
    if docrulegen.is_legacy_rules(path):
        st.error("지금 업무분류 기준 파일은 **옛 모양**입니다. 이 판의 엔진은 옛 모양을 읽지 않아 "
                 "업무분류가 멈춥니다 — 아래에서 **새 방식으로 바꾸세요**. 지금 규칙에서 회사 "
                 "조정만 뽑아 옮기므로 그동안 고친 것은 그대로 남습니다.")
        render_migrate_box(path, expanded=True)
        return
    render_doc_rules_editor_gen(path, tax)


#------------------------------------------------------------------
# 업무분류 규칙 다시 만들기 실행(새 방식)
#=> 엔진의 --build-doc-rule 을 부른다. 화면은 규칙 파일을 직접 쓰지 않는다 —
#   화면에서 부르든 명령창에서 치든 같은 파일이 나온다.
#    1) 실행 명령·체계 JSON 이 있는지 먼저 확인한다
#    2) 실패하면 계약 오류와 실행한 명령을 그대로 보여 준다
#    3) 성공하면 엔진이 남긴 요약·경고를 다음 그리기까지 들고 간다(docrule_flash)
#
# -in: paths = 화면 경로 묶음(cmd · export_input · doc_rules · pythonpath)
#
# -out: bool = 성공하면 True
# -out: error = 없음(실패는 화면에 표시)
#------------------------------------------------------------------
def rebuild_doc_rules(paths):
    base = gerunner.parse_base_cmd(paths.get("cmd"))
    if not base:
        st.error("분류 실행 명령이 비어 있습니다 — ‘고급 · 실행 명령’ 을 먼저 채워 주세요.")
        return False
    export = paths.get("export_input") or ""
    if not os.path.isfile(export):
        st.error(f"회사에서 내보낸 분류 체계 파일을 찾을 수 없습니다: {export or '(경로 미설정)'}")
        return False
    try:
        with st.spinner("업무분류 규칙을 다시 만드는 중…"):
            r = gerunner.run_build_doc_rule(base, export, paths.get("doc_rules") or "",
                                            pythonpath=paths.get("pythonpath") or None)
    except Exception as e:
        uierrlog.show_error(f"규칙 만들기 실패: {e}", exc=e, where="업무분류 규칙 만들기")
        return False
    if r.returncode != 0:
        why = gerunner.error_line(getattr(r, "error", None))
        st.error(f"규칙을 만들지 못했습니다.{(' ' + why) if why else ''}")
        st.caption("실행한 명령 — 명령창에서 그대로 쳐 보면 자세한 원인을 볼 수 있습니다")
        st.code(" ".join(r.args), language="text")
        if r.stderr.strip():
            st.caption(f"실행 로그: {r.stderr.strip()[-600:]}")
        uierrlog.log_error("업무분류 규칙 만들기 실패\n"
                           f"오류: {why or '(계약 오류 없음)'}\n"
                           f"명령: {' '.join(r.args)}\nstderr:\n{r.stderr}",
                           where="업무분류 규칙 만들기")
        return False
    msg = (r.summary or "").split("] ", 1)[-1] if r.summary else "규칙을 새로 만들었습니다."
    st.session_state["docrule_flash"] = msg
    return True


#------------------------------------------------------------------
# 단어 제안이 고칠 '규칙 dict' 준비
#=> '지금 규칙'(조정 반영) 사본을 준다. 저장할 때 자동값과 견줘 조정으로 되돌려야
#   하므로 그 재료도 함께 준다.
#
# -in: path  = doc_rule.yaml 경로
# -in: paths = 화면 경로 묶음(export_input)
#
# -out: (rule_doc, ctx) = ctx 는 {"local","view","base"}
# -out: error = 옛 모양 규칙이면 ValueError · 조정 파일·체계 JSON 오류는 예외 전파
#               (화면이 받아 보여 준다)
#------------------------------------------------------------------
def suggest_rule_doc(path, paths):
    if docrulegen.is_legacy_rules(path):
        # 옛 모양 파일에 바로 쓰면 엔진이 읽지 않는 파일만 고치게 된다.
        raise ValueError("업무분류 기준 파일이 옛 모양입니다 — 설정 › ② 판단 기준 › 업무분류에서 "
                         "먼저 새 방식으로 바꾸세요")
    import copy
    local = docrulegen.load_local(path)
    view, base, _real, _w = docrulegen.views(path, paths.get("export_input") or "", local)
    return copy.deepcopy(view), {"local": local, "view": view, "base": base}


#------------------------------------------------------------------
# 단어 제안으로 고친 규칙 저장
#=> 고친 것을 조정 파일에 옮긴 뒤 규칙을 다시 만든다.
#
# -in: path     = doc_rule.yaml 경로
# -in: rule_doc = apply_terms 로 고친 규칙 dict
# -in: node     = 고친 분류 dc_id
# -in: ctx      = suggest_rule_doc 의 ctx
# -in: paths    = 화면 경로 묶음
#
# -out: 없음
# -out: error = 쓰기 실패 시 OSError · 규칙 만들기 실패 시 OSError(메시지 포함)
#------------------------------------------------------------------
def save_suggested(path, rule_doc, node, ctx, paths):
    docrulegen.apply_rule_edit(ctx["local"], ctx["base"], ctx["view"], rule_doc, node)
    docrulegen.save_local(path, ctx["local"])
    if not rebuild_doc_rules(paths):
        raise OSError("조정은 저장했지만 규칙을 다시 만들지 못했습니다 — 위 오류를 확인하세요")


#------------------------------------------------------------------
# 옛 방식 → 새 방식(자동 생성)으로 바꾸기 상자
#=> 옛 방식 화면 맨 위에 둔다. 지금 규칙 파일에서 '자동값과 다른 부분'만 조정 초안으로
#   뽑아 보여 주고, 확인하면 조정 파일을 쓰고 규칙을 다시 만든다(옛 규칙은 .bak 으로 남는다).
#   초안으로 옛 규칙이 재현되지 않으면 바꾸지 않는다 — 조정을 잃으면 안 된다.
#
# -in: path     = doc_rule.yaml 경로
# -in: expanded = 처음부터 펼쳐 둘지(옛 모양이라 분류가 멈춘 경우 True)
#
# -out: 없음(Streamlit 출력)
# -out: error = 없음(실패는 화면에 표시)
#------------------------------------------------------------------
def render_migrate_box(path, expanded=False):
    paths = st.session_state.get("paths", {})
    export = paths.get("export_input") or ""
    if not os.path.isfile(path):
        return
    if not os.path.isfile(export):
        # 조정을 뽑으려면 같은 체계로 '자동값'을 만들어 견줘야 한다 — 원본 JSON 이 꼭 있어야 한다.
        st.warning(f"회사에서 내보낸 분류 체계 파일이 없어 바꿀 수 없습니다: "
                   f"{export or '(경로 미설정)'} — ① 에서 연결하세요.")
        return
    with st.expander("🆕 새 방식(규칙 자동 생성)으로 바꾸기", expanded=expanded):
        st.caption("옛 방식은 이 화면이 규칙 파일(doc_rule.yaml)을 직접 고쳤습니다. 새 방식에서는 "
                   "**회사 조정만 doc_rule.local.yaml 에 적고**, 규칙은 분류 체계·유의어 사전으로 "
                   "**매번 자동으로 만듭니다** — 자동 도구가 사람이 일부러 비운 칸을 되살리는 일이 "
                   "없어집니다.")
        # 뽑은 결과는 다시 그리기를 지나 살아남아야 한다 — [바꾸기]를 누르면 화면이 다시
        # 그려지는데, 그때 [뽑아 보기]는 눌리지 않은 상태라 여기서 끝내면 안 된다.
        if st.button("지금 규칙에서 회사 조정 뽑아 보기", key="docrule_migrate_preview"):
            try:
                st.session_state["docrule_migrate"] = docrulegen.migrate(path, export)
            except Exception as e:
                uierrlog.show_error(f"조정을 뽑지 못했습니다: {e}", exc=e,
                                    where="새 방식으로 바꾸기")
                return
    got = st.session_state.get("docrule_migrate")
    if not got:
        return
    local, warns, gaps = got
    with st.container(border=True):
        n = len(local.get("rules") or {})
        st.markdown(f"**뽑은 회사 조정 {n}건**")
        for node, adj in (local.get("rules") or {}).items():
            st.caption(f"· **{adj.get('title_at_decision') or node}** — "
                       + docrulegen.adjust_text(adj))
        for w in warns:
            st.warning(w)
        if gaps:
            st.error("이 조정으로 다시 만들어도 지금 규칙과 다른 칸이 있어 바꾸지 않습니다: "
                     + ", ".join(gaps))
            return
        st.caption("이 조정으로 다시 만들면 지금 규칙과 **말 목록이 같습니다**(차례 무시).")
        if st.button("이대로 새 방식으로 바꾸기", key="docrule_migrate_go", type="primary"):
            try:
                docrulegen.save_local(path, local)
            except Exception as e:
                uierrlog.show_error(f"조정 파일을 쓰지 못했습니다: {e}", exc=e,
                                    where="새 방식으로 바꾸기")
                return
            st.session_state.pop("docrule_migrate", None)
            if rebuild_doc_rules(paths):
                st.cache_data.clear()
                st.rerun()


#------------------------------------------------------------------
# 업무분류 판단 기준 편집 — 새 방식(규칙 자동 생성) (설계서 12장)
#=> 규칙 파일은 보여 주기만 하고, 고친 것은 조정 파일(doc_rule.local.yaml)에 적은 뒤
#   규칙을 다시 만든다.
#    1) 표 — 분류마다 '지금 규칙'(자동값 + 회사 조정)을 보여 주고, 고치면 자동값과
#       견줘 조정(비움·뺌·더함)으로 저장한다. 사용 칸으로 분류를 끈다
#    2) 규칙을 만들 때 알린 것(조정이 가리키는 분류 없음·이름 바뀜 등)을 보여 준다
#    3) [규칙 다시 만들기] — 무엇이 바뀌는지 먼저 보여 준다
#    4) 핵어 표·단어 제안 — 저장은 모두 조정 파일로
#
# -in: path = doc_rule.yaml 경로
# -in: tax  = 회사 분류 체계(체계 JSON 에서 읽은 것)
#
# -out: 없음(Streamlit 출력)
# -out: error = 없음(실패는 화면에 표시)
#------------------------------------------------------------------
def render_doc_rules_editor_gen(path, tax):
    paths = st.session_state.get("paths", {})
    export = paths.get("export_input") or ""
    if not os.path.isfile(export):
        st.warning(f"회사에서 내보낸 분류 체계 파일이 없어 규칙을 만들 수 없습니다: "
                   f"{export or '(경로 미설정)'} — ① 에서 연결하세요.")
        return
    try:
        local = docrulegen.load_local(path)
        view, base, real, warns = docrulegen.views(path, export, local)
    except Exception as e:
        uierrlog.show_error(f"업무분류 조정을 읽지 못했습니다: {e}", exc=e,
                            where="업무분류 조정 읽기")
        return

    _flash = st.session_state.pop("docrule_flash", None)
    if _flash:
        st.success(_flash)
    st.caption("**새 방식** — 규칙은 회사 분류 체계와 유의어 사전으로 **자동으로 만들어지고**, "
               "이 표에서 고친 것만 **회사 조정**(doc_rule.local.yaml)으로 남습니다. "
               "분류 체계나 사전이 바뀌어 규칙을 다시 만들어도 회사 조정은 사라지지 않습니다.")

    rows = docrulegen.to_rows(view, local)
    edited = st.data_editor(
        pd.DataFrame(rows), hide_index=True, width="stretch",
        key="docrule_gen_editor", num_rows="fixed",
        column_config={
            "사용": st.column_config.CheckboxColumn(
                "사용", width="small", help="끄면 이 분류로는 규칙을 만들지 않습니다"),
            "분류": st.column_config.TextColumn("이 분류로", disabled=True, width="medium"),
            "회사 조정": st.column_config.TextColumn(
                "회사 조정", disabled=True, width="small",
                help="자동값과 다르게 한 칸 — 비움·뺌·더함"),
            "제목에": st.column_config.TextColumn("제목(첫 줄)에 이 말이 있으면", width="large"),
            "앞부분에": st.column_config.TextColumn("앞부분(400자)에 이 말이 있으면", width="large"),
            "이 말이 나오면": st.column_config.TextColumn("본문 어디든 이 말이 나오면(보조)",
                                                    width="large"),
            "파일 이름에": st.column_config.TextColumn("파일 이름에 이 말이 있으면"),
            "제외할 말": st.column_config.TextColumn("단, 이 말이 있으면 제외"),
            "이유": st.column_config.TextColumn(
                "조정 이유", help="왜 자동값과 다르게 했는지 — 나중에 사람이 되짚을 유일한 기록입니다"),
            "_node": None, "_title": None,
        })
    st.caption("칸을 **비우면** 그 칸을 보지 않고(비움), 말을 **지우면** 뺌, **더하면** 더함으로 "
               "저장됩니다. 자동값과 같아지면 조정이 사라집니다.")
    if st.button("💾 회사 조정 저장하고 규칙 다시 만들기", type="primary", key="docrule_gen_save"):
        n = docrulegen.apply_rows(local, base, edited.fillna("").to_dict("records"))
        try:
            docrulegen.save_local(path, local)
        except Exception as e:
            uierrlog.show_error(f"조정을 저장하지 못했습니다: {e}", exc=e, where="업무분류 조정 저장")
            return
        if rebuild_doc_rules(paths):
            st.session_state["docrule_flash"] = (f"조정 {n}개 분류 저장 · "
                                                 + st.session_state.get("docrule_flash", ""))
            st.cache_data.clear()
            st.rerun()

    if warns:
        with st.expander(f"규칙을 만들 때 알린 것 {len(warns)}건"):
            for w in warns:
                st.caption("· " + w)

    # ── 규칙 다시 만들기 ──
    with st.container(border=True):
        st.markdown("**규칙 다시 만들기**")
        st.caption("분류 체계·유의어 사전·회사 조정으로 규칙 파일을 통째로 새로 만듭니다. "
                   "분류 체계를 다시 가져왔거나 사전을 고쳤을 때 누르세요.")
        current = None
        if os.path.isfile(path):
            import yaml
            try:
                with open(path, encoding="utf-8") as f:
                    current = yaml.safe_load(f)
            except Exception:           # noqa: BLE001 (깨진 파일이면 전부 새 분류로 본다)
                current = None
        changes = docrulegen.preview_changes(current, real)
        if changes:
            st.caption(f"지금 규칙 파일과 달라지는 분류 **{len(changes)}개**")
            st.dataframe(pd.DataFrame(changes), hide_index=True, width="stretch")
        else:
            st.caption("지금 규칙 파일과 말 목록이 같습니다 — 다시 만들어도 판정은 그대로입니다.")
        if st.button("🔄 규칙 다시 만들기", key="docrule_gen_rebuild"):
            if rebuild_doc_rules(paths):
                st.cache_data.clear()
                st.rerun()
        _base = gerunner.parse_base_cmd(paths.get("cmd"))
        st.caption("이 버튼이 실행하는 명령 — 명령창에서 그대로 쳐도 결과는 같습니다")
        st.code(" ".join(_base or ["MpowerClassify"]) + " --build-doc-rule"
                + f" --export-input {export} --doc-rules {path}", language="text")

    # ── 핵어 표 — 끄기는 조정 파일의 head 칸으로 ──
    # 업종은 조정 파일이 정한다(없으면 본보기) — 규칙을 만들 때와 같은 사전으로 표를 계산한다.
    ind = docvocab.clean_industry(local.get("industry") if "industry" in local
                                  else docvocab.load_template(path).get("industry"))
    syn = docvocab.load_synonyms(path, industry=ind)

    def _save_head(doc):
        docrulegen.head_from_doc(local, doc)
        docrulegen.save_local(path, local)
        if not rebuild_doc_rules(paths):
            raise OSError("조정은 저장했지만 규칙을 다시 만들지 못했습니다")

    render_head_titles(path, docrulegen.head_as_doc(local), tax, syn, save=_save_head)
    render_term_suggest_bulk(path, tax)


#------------------------------------------------------------------
# 핵어로 쓰는 분류 — 보여 주고, 끄고 (설계서 7장 ⓐ·ⓑ·ⓒ)
#=> 파일 이름 끝자리가 분류 제목과 같으면, 다른 단서가 없는 문서에 그 분류를
#   '확인 필요'로 제안한다(13장 D1). 그 판정에 어떤 제목이 쓰이고 있는지는
#   지금까지 화면 어디에도 없었다 — 관리자가 분류 체계를 바꿔도 무엇이 달라졌는지
#   볼 곳이 없었다. 이 표가 그 자리다.
#    1) 분류마다 '쓰는 중 / 왜 안 쓰는지'를 판정 엔진과 같은 함수로 계산해 보여 준다
#    2) 자동 판정이 놓친 예외는 체크 한 번으로 끈다(ⓑ 말 단위 — 기본)
#    3) "이 분류에서만" 끄기(ⓒ)는 드물어서 접어 둔다
#
# -in: path = doc_rule.yaml 경로
# -in: doc  = load_doc() 결과
# -in: tax  = 회사 분류 체계
# -in: syn  = 유의어 사전
#
# -out: 없음(Streamlit 출력)
# -out: error = 저장 실패는 화면에 표시
#------------------------------------------------------------------
def render_head_titles(path, doc, tax, syn, save):
    rows, warns = docruleedit.head_rows(doc, tax, syn)
    if not rows:
        return
    n_use = sum(1 for r in rows if r["use"])
    with st.expander(f"파일 이름 끝자리로 찾기 — 쓰는 분류 {n_use}/{len(rows)}개"):
        st.caption("파일 이름의 **끝자리**가 분류 제목과 같으면, 다른 단서가 없는 문서에 "
                   "그 분류를 **‘확인 필요’로 제안**합니다(확정하지 않습니다). "
                   "제목은 판정할 때마다 회사 분류 체계에서 다시 읽으므로, "
                   "**제목이 바뀌면 저절로 따라갑니다.**")
        for w in warns:
            # ⓒ 끄기가 제목이 바뀌어 무효가 된 경우 — 조용히 넘어가면 안 된다.
            st.warning(w.replace("[taxonomy_node_off] ", "이 분류에서만 끄기 — "))

        table = pd.DataFrame([{
            "분류": r["path"],
            "제목": r["title"],
            "핵어로": ("✅ " if r["use"] else "⛔ ") + W.HEAD_REASON.get(r["reason"], r["reason"]),
            # 자동으로 걸러진 줄은 체크를 막는다 — 이미 안 쓰고 있어서 끌 것이 없다.
            "쓰지 않기": r["by"] == "word",
            "_dc": r["dc_id"], "_title": r["title"], "_lock": not (r["use"] or r["by"] == "word"),
        } for r in rows])
        edited = st.data_editor(
            table, hide_index=True, width="stretch", num_rows="fixed",
            key="headtitle_editor",
            disabled=["분류", "제목", "핵어로"],
            column_config={
                "분류": st.column_config.TextColumn("분류", width="medium"),
                "제목": st.column_config.TextColumn("제목", width="small"),
                "핵어로": st.column_config.TextColumn("핵어로 쓰나", width="medium"),
                "쓰지 않기": st.column_config.CheckboxColumn(
                    "쓰지 않기", help="체크하면 이 '말'을 핵어로 쓰지 않습니다"),
                "_dc": None, "_title": None, "_lock": None,
            })
        st.caption("⛔ 로 표시된 줄은 이미 자동으로 걸러진 것이라 체크할 것이 없습니다. "
                   "체크는 **노드가 아니라 말**에 걸립니다 — 나중에 그 분류 이름이 바뀌면 "
                   "목록에 없는 말이라 **저절로 다시 쓰입니다**.")

        if st.button("저장", key="headtitle_save", type="primary"):
            recs = edited.to_dict("records")
            # 자동으로 걸러진 줄에 실수로 체크가 들어가도 무시한다(끌 것이 없다).
            off = [r["_title"] for r in recs if r.get("쓰지 않기") and not r.get("_lock")]
            docruleedit.apply_head_off(doc, off, [r["_title"] for r in recs])
            try:
                save(doc)
                st.success(f"저장됨 · 핵어로 쓰지 않을 말 {len(off)}개")
                st.rerun()
            except Exception as e:
                uierrlog.show_error(f"저장 실패: {e}", exc=e, where="핵어 제외 저장")

        # ⓒ 노드 단위 — 설계서도 "드문 경우만"이라 접어 둔다.
        with st.expander("이 분류에서만 끄기 (드묾)"):
            st.caption("말은 괜찮은데 **이 분류에서만** 끄고 싶을 때 씁니다. "
                       "끌 당시 제목을 함께 적어 두므로, 나중에 제목이 바뀌면 "
                       "이 끄기는 **무효가 되고 재검토로 알려 드립니다**.")
            on_now = {r["dc_id"]: r for r in rows if r["use"] or r["by"] == "node"}
            cur_off = [r["dc_id"] for r in rows if r["by"] == "node"]
            picked = st.multiselect(
                "이 분류에서만 끄기", list(on_now),
                default=cur_off,
                format_func=lambda d: f"{on_now[d]['path']} ({on_now[d]['title']})",
                key="headtitle_nodeoff")
            if st.button("이 설정으로 저장", key="headtitle_nodeoff_save"):
                for dc_id, r in on_now.items():
                    docruleedit.set_node_off(doc, dc_id,
                                             r["title"] if dc_id in picked else None)
                try:
                    save(doc)
                    st.success(f"저장됨 · 이 분류에서만 끄기 {len(picked)}개")
                    st.rerun()
                except Exception as e:
                    uierrlog.show_error(f"저장 실패: {e}", exc=e, where="노드 끄기 저장")


#------------------------------------------------------------------
# 분류 하나를 통째로 훑어 단어 제안 (설정 화면 진입점 ②)
#=> 검토 화면의 진입점 ①이 '문서 한 건'을 보는 자리라면, 여기는 '분류 한 개'를
#   보는 자리다. 그 분류로 확정된 문서 전체에서 후보를 뽑아 표로 보여 준다.
#
#   [①과 무엇이 다른가] 문서 화면은 관리자가 그 문서를 눈앞에 두고 있어, 문턱을
#   못 넘은 말도 까닭을 달아 함께 올린다. 여기는 한 번에 수십 개를 훑는 자리라
#   반대로 조용해야 한다 — 문턱을 넘은 것만 올린다(termsuggest.suggest 그대로).
#
# -in: path = doc_rule.yaml 경로
# -in: tax  = 회사 분류 체계
#
# -out: 없음(화면을 그린다)
# -out: error = 없음(재료가 모자라면 까닭을 화면에 적는다)
#------------------------------------------------------------------
def render_term_suggest_bulk(path, tax):
    paths = paths_of_screen()
    by_id = (tax or {}).get("by_id") or {}
    with st.container(border=True):
        st.markdown("**💡 확정된 문서에서 단어 찾기**")
        st.caption("분류를 하나 고르면, 그 분류로 확정된 문서 전체에서 "
                   "**이 분류에만 나오는 말**을 찾아 줍니다. "
                   "고른 말만 위 표에 덧붙습니다 — 지우지는 않습니다.")

        # 재료가 있는 분류만 고르게 한다 — 없는 분류를 고르고 "왜 비었지" 하게
        # 두는 것보다, 고를 수 있는 것만 보여 주는 편이 낫다.
        docs = load_suggest_docs(paths.get("seed") or "", paths.get("override") or "",
                                 paths.get("text_dir") or "",
                                 _stamp([paths.get("seed"), paths.get("override"), path]))
        labelled = sorted({d for one in docs for d in one["labels"]})
        if not labelled:
            st.caption("확정된 문서가 아직 없습니다 — 검토함에서 분류를 확정하면 "
                       "그만큼 재료가 쌓입니다.")
            return
        if not paths.get("text_dir") or not os.path.isdir(paths["text_dir"]):
            st.caption("· 추출 본문 폴더가 없어 **제목·파일 이름만** 봅니다"
                       "(아래 고급 설정에서 지정하면 본문까지 봅니다).")

        name_of = lambda dc: ((by_id.get(dc) or {}).get("path")
                              or (by_id.get(dc) or {}).get("title") or dc)
        # 넣기·기각 뒤에 st.rerun() 을 부르므로, 안내를 다음 그리기까지 들고 온다.
        # 분류 고르는 칸보다 '위'에 둔다 — 아래에 두면 방금 고른 분류를 설명하는
        # 말처럼 읽힌다(분류를 바꾼 직후에는 실제로 다른 분류의 결과다).
        msg = st.session_state.pop("tsbulk_msg", None)
        if msg:
            st.success(msg[0])
            if len(msg) > 1 and msg[1]:
                st.caption(msg[1])
        node = st.selectbox("분류 고르기", labelled, format_func=name_of,
                            key="tsbulk_node")

        rule_doc, gen_ctx = suggest_rule_doc(path, paths)
        syn = docvocab.load_synonyms(path) or {}
        policy_dir = os.path.dirname(os.path.abspath(path))
        stop_path = os.path.join(policy_dir, "doc_rule_stopwords.yaml")
        audit_path = os.path.join(policy_dir, "doc_rule_suggest_audit.jsonl")
        res = termsuggest.suggest(node, docs, rule_doc=rule_doc,
                                  stopwords=termsuggest.load_stopwords(stop_path),
                                  tax=tax, suffixes=syn.get("suffixes"),
                                  endings=syn.get("verb_endings"))

        st.caption(f"확정 문서 {res['docs']}건 · 폴더 {res['clusters']}곳"
                   + (f" · 본문 없음 {res['no_text']}건" if res["no_text"] else "")
                   + (f" — {res['reason']}" if res["reason"] else ""))
        found = res["candidates"] + res["cluster_only"]
        if not found:
            st.caption("제안할 말이 없습니다.")
            return

        rows = [{"넣기": c["checked"], "단어": c["term"],
                 "넣을 칸": W.suggest_where(c["fields"]).lstrip("— "),
                 "근거": f"{c['docs']}건 / 폴더 {c['clusters']}곳",
                 "다른 분류에": f"{c['df_neg'] * 100:.1f}%",
                 "이 분류를 덮는 비율": f"{c['df_pos'] * 100:.1f}%",
                 "살펴볼 점": " · ".join(c["flags"])} for c in found]
        edited = st.data_editor(
            pd.DataFrame(rows), hide_index=True, width="stretch",
            key=f"tsbulk_tbl_{node}", num_rows="fixed",
            column_config={
                "넣기": st.column_config.CheckboxColumn("넣기", width="small"),
                "단어": st.column_config.TextColumn("단어", disabled=True),
                "넣을 칸": st.column_config.TextColumn("넣을 칸", disabled=True),
                "근거": st.column_config.TextColumn("근거", disabled=True),
                "다른 분류에": st.column_config.TextColumn("다른 분류에", disabled=True),
                "이 분류를 덮는 비율": st.column_config.TextColumn(
                    "이 분류를 덮는 비율", disabled=True),
                "살펴볼 점": st.column_config.TextColumn("살펴볼 점", disabled=True,
                                                    width="medium"),
            })
        picked = {r["단어"] for r in edited.fillna("").to_dict("records") if r["넣기"]}
        picks = [c for c in found if c["term"] in picked]

        reviewer = (st.session_state.get("reviewer") or "").strip()
        c1, c2 = st.columns(2)
        go = c1.button(f"고른 말 기준에 넣기 ({len(picks)}개)", key=f"tsbulk_go_{node}",
                       type="primary", disabled=not picks, width="stretch")
        no = c2.button(f"고른 말 다시 보지 않기 ({len(picks)}개)", key=f"tsbulk_no_{node}",
                       disabled=not picks, width="stretch")
        st.caption("[다시 보지 않기] 는 **제안만** 막습니다 — 이미 기준에 들어 있는 "
                   "말을 지우지는 않습니다.")

        if (go or no) and not reviewer:
            st.error("화면 오른쪽 위에 '검토자 이름'을 먼저 입력하세요(기록에 남깁니다).")
        elif go:
            added, skipped, no_rule = termsuggest.apply_terms(rule_doc, node, picks)
            if no_rule:
                st.error(f"'{name_of(node)}' 규칙이 아직 없습니다 — "
                         "위 [규칙 다시 만들기]로 규칙을 먼저 만드세요(꺼 둔 분류면 먼저 켜세요).")
            else:
                try:
                    save_suggested(path, rule_doc, node, gen_ctx, paths)
                    termsuggest.append_audit(audit_path, node, "accept", picks, reviewer)
                except OSError as e:
                    uierrlog.show_error(f"기준 파일을 쓰지 못했습니다: {e}", exc=e,
                                        where="단어 제안 저장")
                else:
                    st.session_state["tsbulk_msg"] = (
                        f"'{name_of(node)}' 기준에 {len(added)}곳 넣었습니다"
                        + (f" · 이미 있어 건너뜀 {len(skipped)}개" if skipped else ""),
                        "판정에 바로 반영되려면 분류를 다시 돌려야 합니다. "
                        "배포본을 쓰고 있다면 기준 파일 복사본도 맞춰 주세요.")
                    st.cache_data.clear()
                    st.rerun()
        elif no:
            try:
                gone = termsuggest.add_stopwords(stop_path, [c["term"] for c in picks],
                                                 node=node)
                termsuggest.append_audit(audit_path, node, "reject", picks, reviewer)
            except OSError as e:
                uierrlog.show_error(f"목록을 쓰지 못했습니다: {e}", exc=e,
                                    where="단어 제안 기각")
            else:
                st.session_state["tsbulk_msg"] = (
                    f"{len(gone)}개를 다시 보지 않습니다 — "
                    f"'{name_of(node)}' 에서 제안되지 않습니다.",
                    "기준 파일은 그대로입니다 — 이 목록은 '제안'만 막습니다.")
                st.cache_data.clear()
                st.rerun()

        past = termsuggest.load_audit(audit_path, node)
        if past:
            with st.expander(f"이 분류의 단어 결정 기록 {len(past)}건"):
                for row in reversed(past[-50:]):
                    st.caption(f"- {row.get('ts','')} · {row.get('reviewer','')} · "
                               f"**{row.get('term','')}** "
                               f"{W.suggest_action(row.get('action'))}"
                               + (f" ({'·'.join(row.get('fields') or [])})"
                                  if row.get("action") == "accept" else ""))


#------------------------------------------------------------------
# ④ 설정 화면 렌더 (설계서 10장)
#=> 처음 1회 잡고 잘 안 건드리는 것들을 한 화면에 모았다. 예전에는 사이드바 7칸 +
#   탭 4개(규칙 설정·실행·seed 관리·내보내기)에 흩어져 있던 것들이다.
#   번호를 붙인 블록 3개로, 위에서 아래로 따라가면 첫 실행이 끝나게 했다.
#    ① 회사 분류 체계  ② 판단 기준  ③ 분류 실행(분류할 폴더 포함)  (+ 고급)
#
# -in: paths   = 파일 경로 묶음(session_state["paths"])
# -in: tax     = 회사 분류 체계(None 이면 '연결 안 됨')
# -in: records = 지금 로드된 레코드(① 폴더 칸의 '지난 결과 건수' 표시에만 쓴다. 없으면 [])
#
# -out: 없음(Streamlit 출력)
# -out: error = 없음(각 블록이 자기 오류를 표시)
#------------------------------------------------------------------
def render_settings(paths, tax, records):
    # ── ① 회사 분류 체계 ──
    with st.container(border=True):
        c1, c2 = st.columns([4, 1])
        c1.markdown(f"##### 1️⃣ 회사 분류 체계 {'✅ 연결됨' if tax else '⛔ 연결 안 됨'}")
        if tax:
            c1.caption(f"**{tax['node_count']}개 분류** · " + " · ".join(taxlib.roots(tax)))
            c1.caption(f"가져온 날짜 **{taxlib.exported_at_kr(tax)}** · "
                       "회사 관리 화면에서 분류를 바꾸면 다시 가져와야 반영됩니다")
            age = taxlib.age_days(tax)
            # 묵은 스냅샷을 조용히 쓰지 않도록 90일이 지나면 알린다(상위 설계 T13).
            if age is not None and age > 90:
                st.warning(f"가져온 지 **{age}일** 지났습니다. 회사 분류 체계가 바뀌었을 수 "
                           "있으니 다시 가져오는 것을 권합니다.")
        else:
            c1.caption("연결하면 “이 문서가 무엇에 관한 것인지”도 함께 정리됩니다. "
                       "연결하지 않으면 보안등급만 사용합니다.")
        # 연결 전에는 '연결', 연결된 뒤에는 '다시 가져오기' — 하는 일은 같다.
        legacy = docrulegen.is_legacy_rules(paths.get("doc_rules") or "")
        if c2.button("🔗 연결" if not tax else "🔄 다시 가져오기",
                     type="primary" if not tax else "secondary",
                     width="stretch", key="tax_import", disabled=legacy,
                     help="회사에서 내보낸 파일을 읽어 업무분류 규칙을 통째로 다시 만듭니다"):
            # 체계 JSON 을 바로 읽어 업무분류 규칙을 통째로 다시 만든다(회사 조정은 다시 얹힌다).
            if rebuild_doc_rules(paths):
                st.cache_data.clear()
                st.rerun()
        if legacy:
            # 옛 모양 규칙을 그대로 덮으면 그동안 고친 것이 사라진다 — 먼저 옮기게 한다.
            c1.warning("업무분류 기준 파일이 옛 모양이라 다시 가져오기를 막았습니다 — "
                       "② 판단 기준 › 업무분류에서 먼저 **새 방식으로 바꾸세요**.")
        c1.caption("분류 체계의 원본은 MpowerV11 입니다 — 이 화면에서 고치면 두 곳이 "
                   "어긋나므로 여기서는 읽기만 합니다.")

        # 버튼이 무엇을 하는지 명령으로도 밝혀 둔다. 화면이 못 도는 상황(권한·경로
        # 문제)에서 관리자가 명령창에서 그대로 쳐 볼 수 있어야 하고, 무엇보다
        # "버튼이 몰래 무슨 일을 하는지 모르는" 상태를 만들지 않기 위해서다.
        # 실제로 실행되는 것과 같은 값을 보여준다 — 안내와 동작이 어긋나면
        # 그대로 따라 해도 안 맞는다.
        _base = gerunner.parse_base_cmd(paths.get("cmd"))
        st.caption("이 버튼이 실행하는 명령 — 명령창에서 직접 돌려도 결과는 같습니다")
        st.code(" ".join(_base or ["MpowerClassify"])
                + f" --build-doc-rule"
                  f" --export-input {paths.get('export_input') or 'doc_classification_export.json'}"
                  f" --doc-rules {paths.get('doc_rules') or 'doc_rule.yaml'}",
                language="text")
        st.caption("↳ 분류 체계를 따로 파일로 만들지 않고, 원본 JSON 으로 "
                   "**업무분류 규칙을 통째로 다시 만듭니다**(분류 체계가 규칙 안에 들어갑니다).")

    # ── ② 판단 기준 ──
    with st.container(border=True):
        st.markdown("##### 2️⃣ 판단 기준")
        which = st.segmented_control("어느 기준", [W.AXIS_SEC, W.AXIS_DOC],
                                     default=W.AXIS_SEC, key="set_rules_axis",
                                     label_visibility="collapsed")
        if which == W.AXIS_DOC:
            st.caption("“무엇을 계약서로 볼 것인가”를 정합니다. 신호가 여러 개면 "
                       "**해당하는 분류를 모두** 냅니다.")
            render_doc_rules_editor(paths["doc_rules"], tax)
        else:
            st.caption("“얼마나 조심할 문서인가”를 정합니다. 신호가 엇갈리면 "
                       "**더 엄격한 등급**이 적용됩니다.")
            render_rules_editor()

    # ── ③ 분류 실행 ──
    with st.container(border=True):
        st.markdown("##### 3️⃣ 분류 실행")
        render_run_panel(paths["result"], paths, tax, records)

    # ── 고급 ── (파일 경로·기준 문서·리포트 — 평소에는 열 일이 없다)
    with st.expander("고급 — 파일 위치"):
        st.markdown("**파일 위치**")
        g1, g2 = st.columns(2)
        paths["result"] = g1.text_input("분류 결과", value=paths["result"], key="p_result")
        paths["override"] = g2.text_input("내가 고친 기록", value=paths["override"], key="p_ov")
        paths["seed"] = g1.text_input("기준 문서", value=paths["seed"], key="p_seed")
        paths["seed_audit"] = g2.text_input("기준 문서 변경 기록",
                                            value=paths["seed_audit"], key="p_audit")
        paths["rules"] = g1.text_input("보안등급 기준 파일", value=paths["rules"], key="p_rules")
        paths["doc_rules"] = g1.text_input("업무분류 기준 파일",
                                           value=paths["doc_rules"], key="p_drules")
        paths["text_dir"] = g2.text_input(
            "추출 본문 폴더", value=paths.get("text_dir", ""), key="p_textdir",
            help="분류를 돌릴 때 --textsave 로 남긴 폴더입니다. "
                 "‘이 문서에서 단어 뽑기’가 본문을 여기서 읽습니다. "
                 "비워 두면 제목·파일 이름만 봅니다.")
        paths["export_input"] = g2.text_input(
            "회사 분류 체계 원본(내보낸 JSON)", value=paths.get("export_input", ""),
            key="p_expin",
            help="① 의 ‘다시 가져오기’가 업무분류 규칙을 만들 때 읽는 MpowerV11 내보내기 파일입니다.")
        paths["cmd"] = g1.text_input("분류 실행 명령", value=paths["cmd"], key="p_cmd")
        # cmd 와 짝인 값이라 함께 둔다 — 소스로 실행할 때만 필요하고, exe 로 바꾸면
        # 비워도 된다. 저장되는 값이므로 고칠 수 있어야 한다.
        paths["pythonpath"] = g2.text_input(
            "PYTHONPATH", value=paths.get("pythonpath", ""), key="p_pypath",
            help="‘분류 실행 명령’을 python -m csoclassify 로 쓸 때 필요한 src 폴더입니다. "
                 "exe 로 실행하면 비워 두세요.")

        # 여기서 고친 값은 settings.yaml 에 바로 적어 둔다 — 다시 띄워도 그대로 쓰게.
        # (streamlit 은 화면을 건드릴 때마다 스크립트를 다시 돌리므로, 직전에 저장한
        #  내용과 다를 때만 쓴다.)
        status, snap = uisettings.save_if_changed(paths, st.session_state.get("paths_saved"))
        st.session_state["paths_saved"] = snap
        cfg_file = uisettings.settings_path()
        if status == "failed":
            st.warning(f"설정을 저장하지 못했습니다(쓰기 권한을 확인하세요): {cfg_file}")
        else:
            st.caption(f"여기서 고친 값은 **{cfg_file}** 에 저장되어 다음 실행 때 그대로 불러옵니다."
                       + (" · 방금 저장했습니다." if status == "saved" else ""))

        if st.button("기본값으로 되돌리기", key="p_reset",
                     help="경로를 처음 상태로 되돌리고 settings.yaml 에도 그대로 반영합니다."):
            d = default_paths()
            # 입력칸은 key 로 자기 값을 따로 들고 있어서, paths 만 바꾸면 화면이 안 바뀐다.
            # 위젯 상태를 지워야 새 기본값이 입력칸에 다시 그려진다.
            for k in ("p_result", "p_ov", "p_seed", "p_audit", "p_rules",
                      "p_tax", "p_expin", "p_drules", "p_cmd", "p_pypath", "set_folder"):
                st.session_state.pop(k, None)
            st.session_state["paths"] = d
            uisettings.save_paths(d)
            st.session_state["paths_saved"] = {k: (d.get(k) or "") for k in uisettings.KEYS}
            st.rerun()


#------------------------------------------------------------------
# ⑤ 기준 문서 화면 — 축별 등록 · 수정 · 삭제
#=> "이런 문서는 이 등급 · 이 분류" 본보기를 사람이 직접 관리하는 화면.
#   문서함에서 하는 등록은 '이번에 분류한 문서' 중에서 고르는 것이라, 아직 분류하지
#   않은 대표 문서를 올리거나 이미 등록한 것을 고치려면 갈 곳이 없었다.
#    1) 위: 지금 구성(건수·등급 균형)을 먼저 보여준다
#    2) 가운데: 파일을 올려 새로 등록 — 두 축을 각각 정할 수 있다
#    3) 아래: 등록된 것을 골라 고치거나 지운다(축 하나만 해제도 여기서)
#
# -in: tax = 회사 분류 체계(업무분류를 고르는 목록에 쓴다. None 이면 그 칸만 잠근다)
#
# -out: 없음(Streamlit 출력)
# -out: error = 없음(개별 실패는 화면에 이유와 함께 표시)
#------------------------------------------------------------------
def render_seed_screen(tax=None):
    cfg = st.session_state.get("seedcfg", {})
    seed_path = cfg.get("seed_path")
    audit_path = cfg.get("audit_path")
    reviewer = (cfg.get("reviewer") or "").strip()

    st.markdown("### 🌱 기준 문서")
    st.caption("다음 분류 때 “비슷한 문서 참고”의 잣대가 되는 본보기 문서입니다 — "
               "확실한 문서만 두세요.")

    flash = st.session_state.pop("seedscr_flash", None)
    if flash:
        st.success(flash)

    seeds = seedstore.load_seeds(seed_path)
    # 읽다가 건너뛴 줄이 있으면 반드시 알린다. 조용히 넘어가면 기준 문서가
    # 소리 없이 줄어들고, 사람은 '왜 이 문서가 기준으로 안 먹지'를 알 길이 없다.
    # 대개는 이 화면보다 새 판으로 쓰인 파일을 옛 판 화면이 읽을 때 생긴다.
    if seedstore.SKIPPED:
        _fut = [x for x in seedstore.SKIPPED if x['why'] != 'broken']
        _brk = len(seedstore.SKIPPED) - len(_fut)
        _msg = []
        if _fut:
            _msg.append(f"이 화면이 모르는 새 형식 {len(_fut)}줄"
                        f"({', '.join(sorted({x['why'] for x in _fut}))})")
        if _brk:
            _msg.append(f"깨진 줄 {_brk}줄")
        st.warning(f"기준 문서 파일에서 {' · '.join(_msg)} 을 건너뛰었습니다 "
                   f"— 그만큼은 분류에 쓰이지 않습니다. 파일: {seed_path}")
    render_seed_manager()          # 현황 요약(건수·등급 균형)

    st.divider()
    render_seed_add(seeds, tax, cfg, seed_path, audit_path, reviewer)

    st.divider()
    render_seed_edit(seeds, tax, seed_path, audit_path, reviewer)


#------------------------------------------------------------------
# 기준 문서 새로 등록(파일 올리기)
#=> 아직 분류하지 않은 대표 문서를 관리자가 직접 본보기로 올린다.
#    1) 어떤 축을 정할지 먼저 고른다 — 보안등급만/업무분류만/둘 다 모두 유효하다
#    2) 올린 파일을 그 자리에서 분류기에 넣어 벡터를 얻는다(벡터 없는 기준 문서는
#       비교 대상이 못 되므로 저장할 이유가 없다)
#    3) 축마다 따로 얹고, 축마다 따로 감사 기록을 남긴다
#
# -in: seeds      = 지금 기준 문서 목록
# -in: tax        = 회사 분류 체계(업무분류 목록용)
# -in: cfg        = seedcfg(실행 명령 등)
# -in: seed_path  = 저장소 경로
# -in: audit_path = 감사 로그 경로
# -in: reviewer   = 검토자 이름
#
# -out: 없음(등록 시 저장 + rerun)
# -out: error = 없음(실패는 화면에 표시)
#------------------------------------------------------------------
def render_seed_add(seeds, tax, cfg, seed_path, audit_path, reviewer):
    st.markdown("**새로 등록 — 파일을 올려 본보기로 삼기**")
    st.caption("이미 분류한 문서를 등록하려면 **문서함**에서 고르는 편이 빠릅니다"
               "(여러 건 한꺼번에도 됩니다). 여기서는 아직 분류하지 않은 문서를 올립니다.")

    ver = st.session_state.get("seedadd_ver", 0)
    up = st.file_uploader("문서 올리기(여러 개 가능)", accept_multiple_files=True,
                          key=f"seedadd_file_{ver}")

    c1, c2 = st.columns(2)
    with c1:
        use_sec = st.checkbox(f"{W.AXIS_SEC} 정하기", value=True,
                              key=f"seedadd_usesec_{ver}")
        grade = st.radio("등급", W.GRADE_CHOICES, horizontal=True,
                         format_func=lambda g: W.GRADE_LABEL[g],
                         key=f"seedadd_grade_{ver}", disabled=not use_sec)
    with c2:
        opts = taxlib.selectable(tax)
        label_by_id = dict(opts)
        use_doc = st.checkbox(f"{W.AXIS_DOC} 정하기", value=False,
                              key=f"seedadd_usedoc_{ver}", disabled=not opts,
                              help=None if opts else "회사 분류 체계를 먼저 연결하세요")
        dc_ids = st.multiselect("분류(여러 개 가능)", [dc for dc, _ in opts],
                                format_func=lambda dc: label_by_id.get(dc, dc),
                                key=f"seedadd_dc_{ver}", disabled=not use_doc)
    note = st.text_input("메모(선택)", key=f"seedadd_note_{ver}")

    if st.button("🌱 기준 문서로 등록", type="primary", key="seedadd_go"):
        if not reviewer:
            st.error("화면 오른쪽 위에 '검토자 이름'을 먼저 입력하세요.")
            return
        if not up:
            st.warning("파일을 올리세요.")
            return
        if not (use_sec or (use_doc and dc_ids)):
            st.warning("보안등급이나 업무분류 중 적어도 하나는 정해야 합니다.")
            return

        base = gerunner.parse_base_cmd(cfg.get("csoclassify_cmd"))
        if not base:
            st.error("분류 실행 명령이 비어 있습니다 — 설정/분류 ▸ 고급 ▸ 실행 명령을 "
                     "먼저 채워 주세요.")
            return

        cur, added, failed = list(seeds), 0, []
        prog = st.progress(0.0, text=f"임베딩 중… 0/{len(up)}")

        # 올린 파일을 임시 폴더 '한 곳'에 모아 한 번에 임베딩한다. 파일마다 따로
        # 돌리면 임베딩 모델을 올린 수만큼 다시 읽어, 스무 건이면 20초를 그냥 버린다.
        tmpd = tempfile.mkdtemp()
        staged = []
        for uf in up:
            tp = os.path.join(tmpd, uf.name)
            with open(tp, "wb") as f:
                f.write(uf.getbuffer())
            staged.append((uf.name, tp))
        try:
            vecs = gerunner.embed_files(
                base, [tp for _, tp in staged], pythonpath=cfg.get("pythonpath"),
                on_progress=lambda d, t, p: prog.progress(
                    d / t if t else 1.0, text=f"임베딩 중… {d}/{t}"))
        except Exception as e:
            uierrlog.show_error(f"임베딩 실패: {e}", exc=e, where="기준 문서 등록")
            vecs = {}

        for i, (name, tp) in enumerate(staged):
            prog.progress(i / len(staged), text=f"등록 중… {i}/{len(staged)} · {name}")
            vec = vecs.get(tp)
            if not vec:
                failed.append(name)
                st.error(f"{name}: 벡터를 얻지 못했습니다(문서에서 글자를 못 뽑았을 수 "
                         "있습니다).")
                continue
            # 파일 이름만 남긴다 — 임시 폴더 경로를 적으면 다음 실행 때 '원본 없음'이 된다.
            if use_sec:
                cur = seedstore.set_axis(cur, name, "security", grade, reviewer,
                                         source="upload", note=note.strip(), vector=vec)
                seedstore.append_seed_audit(audit_path, "add", name, axis="security",
                                            before=None, after=grade, reviewer=reviewer,
                                            reason="기준문서 화면에서 직접 등록")
            if use_doc and dc_ids:
                cur = seedstore.set_axis(cur, name, "doctype", list(dc_ids), reviewer,
                                         source="upload", note=note.strip(), vector=vec)
                seedstore.append_seed_audit(audit_path, "add", name, axis="doctype",
                                            before=None, after=list(dc_ids),
                                            reviewer=reviewer,
                                            reason="기준문서 화면에서 직접 등록")
            added += 1
        prog.progress(1.0, text="완료")
        # 제목(첫 줄)은 임시 사본을 지우기 '전에' 떠 둔다 — 기준 문서에는 파일 이름만 남아
        # 나중에는 원본을 다시 읽을 수 없다(BM25 2차 재료, 설계서 §17-4)
        if added:
            titles = gerunner.titles_for_files(base, [tp for _, tp in staged],
                                               pythonpath=cfg.get("pythonpath"))
            for name, tp in staged:
                row = seedstore.find_seed(cur, name)
                if row is not None and titles.get(tp):
                    row["title"] = titles[tp]

        # 올린 파일 사본은 등록이 끝나면 지운다(예전에는 파일마다 임시 폴더를 만들고
        # 아무도 치우지 않아, 등록할 때마다 temp 에 사본이 쌓였다).
        shutil.rmtree(tmpd, ignore_errors=True)

        if added:
            try:
                seedstore.save_seeds(seed_path, cur)
            except OSError as e:
                uierrlog.show_error(f"기준 문서 저장 실패: {e}", exc=e, where="기준 문서 저장")
                return
            st.session_state["seedscr_flash"] = (
                f"{added}건을 기준 문서로 등록했습니다"
                + (f" · 실패 {len(failed)}건" if failed else ""))
            st.session_state["seedadd_ver"] = ver + 1   # 입력칸 비우기
            st.rerun()
        else:
            st.warning("등록된 기준 문서가 없습니다(위 오류를 확인하세요).")


#------------------------------------------------------------------
# 편집 화면에서 무엇이 바뀌는지 판단(화면 없는 순수 계산)
#=> 사람이 고른 값과 지금 저장된 값을 견주어 '할 일 목록'을 만든다. 화면은 이
#   목록을 그대로 적용하기만 하므로, 보여 준 것과 저장되는 것이 어긋날 수 없다.
#   [까다로운 지점] '해제'와 '값 변경'은 전혀 다른 일이다. 해제는 값을 지우지 않고
#   잣대에서만 빼야 한다(되살릴 수 있어야 하고, 무엇을 뺐는지도 남아야 한다).
#
# -in: entry     = 지금 저장된 기준 문서 한 줄
# -in: new_grade = 고른 보안등급("C"/"S"/"O") 또는 "해제"
# -in: new_dc    = 고른 업무분류 dc_id 목록(비어 있으면 해제)
# -in: new_note  = 고친 메모
#
# -out: list = [{"axis","action","before","after"}] · action 은
#        "set"(값 지정) · "retire"(축 해제) · "note"(메모만)
# -out: error = 없음
#------------------------------------------------------------------
def seed_edit_ops(entry, new_grade, new_dc, new_note):
    ops = []
    act = seedstore.active_axes(entry)

    was_g = seedstore.axis_value(entry, "security") if "security" in act else None
    if new_grade == "해제":
        if "security" in act:
            ops.append({"axis": "security", "action": "retire",
                        "before": was_g, "after": None})
    elif new_grade and new_grade != was_g:
        ops.append({"axis": "security", "action": "set",
                    "before": was_g, "after": new_grade})

    was_dc = list(seedstore.axis_value(entry, "doctype") or []) if "doctype" in act else []
    new_dc = list(new_dc or [])
    if new_dc != was_dc:
        if new_dc:
            ops.append({"axis": "doctype", "action": "set",
                        "before": was_dc or None, "after": new_dc})
        elif was_dc:
            ops.append({"axis": "doctype", "action": "retire",
                        "before": was_dc, "after": None})

    if (new_note or "") != (entry.get("note", "") or ""):
        ops.append({"axis": "-", "action": "note",
                    "before": entry.get("note", "") or "", "after": new_note or ""})
    return ops


#------------------------------------------------------------------
# 판단한 대로 적용한다(화면 없는 순수 적용)
#=> seed_edit_ops 가 만든 목록만 적용한다. 여기서 새로 판단하지 않는 것이 핵심이다.
#
# -in: seeds      = 기준 문서 목록(변형하지 않고 새 목록 반환)
# -in: file       = 대상 문서
# -in: ops        = seed_edit_ops 결과
# -in: reviewer   = 승인자
# -in: audit_path = 감사 로그 경로(None 이면 기록하지 않는다)
#
# -out: list = 갱신된 기준 문서 목록
# -out: error = 없음
#------------------------------------------------------------------
def apply_seed_edit(seeds, file, ops, reviewer, audit_path=None):
    cur = list(seeds)
    for op in ops:
        axis, action = op["axis"], op["action"]
        if action == "set":
            cur = seedstore.set_axis(cur, file, axis, op["after"], reviewer)
            act = "update" if op["before"] else "add"
        elif action == "retire":
            cur, _ = seedstore.retire_axis(cur, file, axis)
            act = "retire"
        else:                       # 메모는 줄 전체에 붙는 값이라 축과 무관하다
            key = seedstore.norm_file(file)
            cur = [({**x, "note": op["after"]}
                    if seedstore.norm_file(x.get("file")) == key else x) for x in cur]
            act = "update"
        if audit_path:
            seedstore.append_seed_audit(audit_path, act, file, axis=axis,
                                        before=op["before"], after=op["after"],
                                        reviewer=reviewer,
                                        reason="기준문서 화면에서 수정")
    return cur


#------------------------------------------------------------------
# 등록된 기준 문서 고치기 · 지우기
#=> 한 건을 골라 두 축을 각각 손본다. 표에서 여러 줄을 한 번에 고치는 방식은
#   업무분류가 '여러 개'라 표 칸에 담기지 않는다 — 골라서 아래에서 고치는 쪽이
#   두 축 모두를 제대로 다룰 수 있다.
#
# -in: seeds      = 지금 기준 문서 목록
# -in: tax        = 회사 분류 체계
# -in: seed_path  = 저장소 경로
# -in: audit_path = 감사 로그 경로
# -in: reviewer   = 검토자 이름
#
# -out: 없음(저장 시 rerun)
# -out: error = 없음
#------------------------------------------------------------------
def render_seed_edit(seeds, tax, seed_path, audit_path, reviewer):
    st.markdown("**고치기 · 지우기**")
    if not seeds:
        st.info("아직 등록된 기준 문서가 없습니다. 위에서 올리거나 문서함에서 등록하세요.")
        return

    rows = []
    for e in seeds:
        dc = list(seedstore.axis_value(e, "doctype") or [])
        state = []
        for a in seedstore.AXES:
            stt = seedstore.axis_state(e, a)
            if stt and stt != seedstore.STATE_ACTIVE:
                reason = seedstore.hold_reason(e, a)
                state.append(f"{seedstore.AXIS_LABEL[a]} {W.SEED_STATE.get(stt, stt)}"
                             + (f"({W.SEED_REASON.get(reason, reason)})" if reason else ""))
        rows.append({
            "문서": os.path.basename(e.get("file") or ""),
            W.AXIS_SEC: (seedstore.axis_value(e, "security") or "—")
                        if "security" in seedstore.active_axes(e) else "—",
            W.AXIS_DOC: " · ".join(taxlib.path_of(tax, d) for d in dc) if dc else "—",
            "상태": " · ".join(state) or "쓰는 중",
            "메모": e.get("note", "") or "",
            # 기준 문서는 사람이 책임지고 고른 본보기다. 결과가 이상할 때
            # 물어볼 사람이 누구인지가 메모만큼 중요하다.
            "검토자": " · ".join(seedstore.approvers(e)) or "—",
            "위치": e.get("file") or "",
        })
    ev = st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=300,
                      on_select="rerun", selection_mode="single-row",
                      column_config={"문서": st.column_config.TextColumn(width="large"),
                                     W.AXIS_SEC: st.column_config.TextColumn(width=90),
                                     "상태": st.column_config.TextColumn(width="small"),
                                     "검토자": st.column_config.TextColumn(width="small")})
    picked = ev.selection.rows if ev and ev.selection else []
    if not picked:
        st.caption("고치거나 지울 문서를 표에서 한 건 고르세요.")
        return

    e = seeds[picked[0]]
    file = e.get("file") or ""
    st.markdown(f"##### {os.path.basename(file)}")
    st.caption(f"위치 `{file}`")

    c1, c2 = st.columns(2)
    with c1:
        cur_g = seedstore.axis_value(e, "security")
        sec_on = "security" in seedstore.active_axes(e)
        choices = W.GRADE_CHOICES + ["해제"]
        idx = choices.index(cur_g) if (sec_on and cur_g in W.GRADE_CHOICES) else len(choices) - 1
        new_g = st.radio(f"{W.AXIS_SEC}", choices, index=idx, horizontal=True,
                         format_func=lambda g: W.GRADE_LABEL.get(g, "쓰지 않음"),
                         key=f"seededit_g_{file}")
    with c2:
        opts = taxlib.selectable(tax)
        label_by_id = dict(opts)
        cur_dc = [d for d in (seedstore.axis_value(e, "doctype") or [])
                  if d in label_by_id]
        new_dc = st.multiselect(f"{W.AXIS_DOC}", [dc for dc, _ in opts],
                                default=cur_dc if "doctype" in seedstore.active_axes(e) else [],
                                format_func=lambda dc: label_by_id.get(dc, dc),
                                key=f"seededit_dc_{file}", disabled=not opts,
                                help="비우면 이 문서는 업무분류 잣대에서 빠집니다")
    new_note = st.text_input("메모", value=e.get("note", "") or "", key=f"seededit_n_{file}")

    b1, b2 = st.columns([1, 1])
    if b1.button("💾 저장", type="primary", key=f"seededit_save_{file}"):
        if not reviewer:
            st.error("화면 오른쪽 위에 '검토자 이름'을 먼저 입력하세요.")
            return
        ops = seed_edit_ops(e, new_g, new_dc, new_note)
        if not ops:
            st.info("바뀐 것이 없습니다.")
            return
        cur = apply_seed_edit(seeds, file, ops, reviewer, audit_path)
        changed = len(ops)
        left = seedstore.save_seeds(seed_path, cur)
        gone = seedstore.find_seed(seedstore.load_seeds(seed_path), file) is None
        st.session_state["seedscr_flash"] = (
            f"{os.path.basename(file)} — {changed}곳을 고쳤습니다 · 남은 기준 문서 {left}건"
            + ("(두 축이 모두 해제돼 목록에서 빠졌습니다)" if gone else ""))
        st.rerun()

    with b2:
        sure = st.checkbox("정말 지웁니다", key=f"seededit_sure_{file}")
        if st.button("🗑 지우기", disabled=not sure, key=f"seededit_del_{file}"):
            if not reviewer:
                st.error("화면 오른쪽 위에 '검토자 이름'을 먼저 입력하세요.")
                return
            cur = seedstore.remove_seed(seeds, file)
            seedstore.append_seed_audit(
                audit_path, "delete", file, axis="-",
                before={a: seedstore.axis_value(e, a) for a in seedstore.AXES
                        if seedstore.axis_state(e, a)},
                after=None, reviewer=reviewer, reason="기준문서 화면에서 삭제")
            seedstore.save_seeds(seed_path, cur)
            st.session_state["seedscr_flash"] = f"{os.path.basename(file)} 을 지웠습니다."
            st.rerun()


#------------------------------------------------------------------
# 초기화 확인 창
#=> [초기화]를 누르면 열리는 대화상자. 되돌릴 수 없는 일이므로 세 가지를 지킨다.
#    1) 지울 파일을 '이름과 크기까지' 그대로 먼저 보여준다 — 무엇이 사라지는지
#       모르는 채로 누르게 하지 않는다
#    2) 남기는 것도 함께 밝힌다(소스 코드·회사 분류 체계 원본·기준 규칙)
#    3) 확인 체크를 해야만 지우는 버튼이 눌린다
#   화면이 보여 준 목록(plan_reset)과 실제로 지우는 목록이 같은 값이라, "보여 준
#   것과 다른 것이 지워지는" 사고가 구조적으로 생기지 않는다.
#
# -in: 없음(ui 폴더 위치는 이 파일 위치에서 구한다)
#
# -out: 없음(지운 뒤 캐시를 비우고 화면을 새로 그린다)
# -out: error = 없음(지우지 못한 파일은 화면에 이유와 함께 표시)
#------------------------------------------------------------------
@st.dialog("초기화 — 처음 상태로 되돌리기")
def reset_dialog():
    ui_dir = os.path.dirname(os.path.abspath(__file__))
    drop_gen = st.checkbox(
        "만들어진 업무분류 규칙(doc_rule.yaml)도 지우기",
        value=True,
        help="회사 분류 체계 원본(doc_classification_export.json)과 "
             "doc_rule_template.yaml 로 다시 만들 수 있습니다")
    # 기준 문서만 따로 뺄 수 있게 한다 — "결과는 갈아엎되 그동안 확정한 기준
    # 문서는 살려 두고 다시 분류"가 실무에서 자주 필요하다. 기본은 지움
    # ('처음 상태로 되돌리기'라는 이 기능의 뜻을 기본값으로 유지).
    drop_seed = st.checkbox(
        "기준 문서(class_seed.jsonl)도 지우기",
        value=True, key="reset_drop_seed",
        help="끄면 그동안 확정해 둔 기준 문서를 그대로 남깁니다. "
             "다시 분류할 때 이 기준으로 비슷한 문서를 계속 분류합니다. "
             "변경 이력(class_seed_audit.jsonl)은 이 선택과 무관하게 지웁니다")
    targets, kept = uireset.plan_reset(ui_dir, drop_generated=drop_gen,
                                       drop_seed=drop_seed)

    if not targets:
        st.success("지울 것이 없습니다 — 이미 처음 상태입니다.")
        return

    total = uireset.human_size(sum(t["size"] for t in targets))
    st.markdown(f"**지울 파일 {len(targets)}건 · {total}**")
    st.dataframe(
        pd.DataFrame([{"파일": os.path.relpath(t["path"], ui_dir),
                       "무엇을 담고 있나": t["why"],
                       "다시 만들려면": uireset.recover_text(t),
                       "크기": uireset.human_size(t["size"])} for t in targets]),
        hide_index=True, width="stretch")

    st.caption("남는 것 — 소스 코드 · " + " · ".join(k["name"] for k in kept)
               + " · cso_rule.yaml · doc_rule_template.yaml · synonyms 폴더 · "
                 "화면 설정(settings.yaml)")

    # 되돌릴 수 있는 것과 없는 것은 무게가 전혀 다르다. 그 둘을 뭉뚱그려
    # "되돌릴 수 없습니다" 라고만 하면, 정말 위험한 쪽이 묻힌다.
    lost = [t for t in targets if t.get("recoverable") is False]
    if lost:
        st.warning("**사람이 쌓은 판단 " + str(len(lost)) + "건이 영영 사라집니다** — "
                   + " · ".join(t["name"] for t in lost) + ". "
                   "다시 분류해도 돌아오지 않습니다(관리자가 고친 등급, 확정한 "
                   "업무분류, 등록해 둔 기준 문서와 그 이력).")
    if len(lost) < len(targets):
        st.info("나머지는 표의 ‘다시 만들려면’ 대로 하면 그대로 되돌아옵니다.")

    ok = st.checkbox("위 파일들을 지웁니다", key="reset_ok")
    if st.button("🗑 초기화", type="primary", disabled=not ok, key="reset_go"):
        deleted, failed = uireset.run_reset(targets)
        # 결과 파일을 읽어 둔 캐시가 남아 있으면, 파일이 없는데도 화면에 옛 문서가
        # 그대로 보인다 — 지운 직후에는 반드시 캐시를 비운다.
        st.cache_data.clear()
        st.session_state["reset_flash"] = (
            f"초기화 완료 — {len(deleted)}건을 지웠습니다"
            + (f" · 지우지 못함 {len(failed)}건" if failed else ""))
        if failed:
            st.session_state["reset_failed"] = failed
        st.rerun()


#------------------------------------------------------------------
# 메인 — 상단 관점 스위치 + 메뉴 4개 (설계서 5장)
#=> 화면 전체의 뼈대. 예전에는 탭 9개 + 사이드바 7칸이었는데, 매일 쓰는 화면 3개와
#   처음 1회 쓰는 설정 1개로 접었다.
#    1) 설정을 session_state 에 한 번만 만들어 화면들이 같은 값을 보게 한다
#    2) 상단에 관점 스위치(보안등급/업무분류/전체)와 검토자 이름을 고정 배치
#    3) 메뉴는 st.tabs 가 아니라 segmented_control 로 둔다 — 그래야 "검토 시작"
#       같은 버튼이 화면을 옮길 수 있고, 한 번에 한 화면만 그려 빠르다
#    4) 결과 파일이 없으면 설정만 열어 준다(처음 실행 흐름, 설계서 5-2)
#
# -in: 없음
# -out: 없음(Streamlit 앱)
# -out: error = 결과 로드 실패 시 화면에 안내하고 멈춘다
#------------------------------------------------------------------
def main():
    st.set_page_config(page_title="문서자동분류", page_icon="🗂️", layout="wide")

    # 현황 화면의 숫자 칸(st.metric) 글씨 줄이기.
    # 기본 2.25rem 은 다섯 칸을 나란히 놓으면 "2026-08-25 16:41" 같은 값이 잘려
    # "2026-08-25 16…" 로 나온다. 숫자는 조금 작아도 충분히 읽히지만, 잘린 값은
    # 아예 못 읽는다 — 그래서 폰트를 줄여 값이 온전히 보이는 쪽을 택한다.
    st.markdown("""
        <style>
        [data-testid="stMetricValue"] { font-size: 1.6rem; }
        [data-testid="stMetricLabel"] p { font-size: 0.85rem; }
        </style>
    """, unsafe_allow_html=True)

    # 설정값은 한 번만 만들고 이후에는 session_state 의 것을 계속 쓴다.
    #   기본값(default_paths) 위에 settings.yaml 에 저장해 둔 값을 덮어 시작한다 —
    #   그래야 '고급 · 파일 위치' 에서 고친 경로가 다시 띄워도 그대로 남는다.
    if "paths" not in st.session_state:
        loaded = uisettings.load_paths(default_paths())
        st.session_state["paths"] = loaded
        # 지금 값이 곧 '저장된 상태'다. 이걸 기준으로 나중에 바뀐 것만 다시 쓴다.
        # 단 파일이 아직 없으면 None 을 넣어, 설정 화면을 처음 열 때 기본값 그대로
        # 한 번 써 두게 한다 — 파일이 실제로 있어야 관리자가 열어 보고 손댈 수 있다.
        st.session_state["paths_saved"] = (
            {k: (loaded.get(k) or "") for k in uisettings.KEYS}
            if os.path.isfile(uisettings.settings_path()) else None)
    paths = st.session_state["paths"]
    st.session_state.setdefault("menu", MENU_HOME)
    # 예약된 화면 이동을 '위젯을 만들기 전에' 반영한다 — 아래 두 줄 순서가 중요하다.
    apply_pending_nav()

    # ── 상단 고정 줄 ──
    # 예전에는 여기에 '관점(보안등급/업무분류/전체)' 스위치가 있었다. 없앤 이유:
    # 두 축은 한 문서에 대해 함께 판단해야 하는 것이라, 관점을 고르게 하면 한쪽을
    # 보는 동안 다른 쪽이 보이지 않아 같은 문서를 두 번 열게 된다. 이제 모든 화면이
    # 두 축을 나란히 보여준다.
    # 메뉴를 제목과 검토자 이름 사이에 둔다 — 제목·이동·작성자가 한 줄에 모여
    # 화면 위쪽 두 줄을 한 줄로 줄인다(표가 그만큼 위로 올라온다).
    # vertical_alignment="bottom": 제목·입력칸 높이가 서로 달라도 아랫줄을 맞춘다.
    t1, t2, t3, t4 = st.columns([3, 5, 2, 1], vertical_alignment="bottom")
    t1.markdown("### 🗂️ 문서자동분류")
    # 메뉴는 '검토함 · N' 의 N 을 알아야 그릴 수 있는데 그 수는 결과 파일을 읽어야
    # 나온다. 자리만 먼저 잡아 두고 아래(결과를 읽은 뒤)에서 채운다.
    menu_slot = t2.empty()
    reviewer = t3.text_input("검토자 이름", key="reviewer",
                             placeholder="기록에 남습니다")
    # 초기화는 되돌릴 수 없다 — 누르면 곧바로 지우지 않고 확인 창을 먼저 연다.
    if t4.button("초기화", key="reset_open", width="stretch",
                 help="분류 결과·기준 문서 등 앱이 만든 데이터를 지우고 처음 상태로 되돌립니다"):
        reset_dialog()

    # 분류 체계 연결 결과도 rerun 을 지나 살아남아야 한다.
    _tax_flash = st.session_state.pop("tax_flash", None)
    if _tax_flash:
        st.success(_tax_flash)

    # 초기화 결과는 rerun 을 지나 살아남아야 한다(지운 뒤 화면을 새로 그리므로).
    _flash = st.session_state.pop("reset_flash", None)
    if _flash:
        st.success(_flash)
        for _f, _why in st.session_state.pop("reset_failed", []):
            st.error(f"지우지 못했습니다: {os.path.basename(_f)} — {_why} "
                     "(다른 프로그램이 열고 있는지 확인하세요)")

    # 하위 화면들이 참조할 설정을 세션에 보관(기준 문서 승격·분류 실행에 쓴다).
    st.session_state["seedcfg"] = {
        "seed_path": paths["seed"], "audit_path": paths["seed_audit"],
        "csoclassify_cmd": paths["cmd"], "pythonpath": paths["pythonpath"] or None,
        "reviewer": reviewer or "",
    }
    st.session_state["rulescfg"] = {
        "rules_path": paths["rules"],
        "src_path": os.path.abspath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "src")),
    }

    # 회사 분류 체계는 '있으면 켜지고 없으면 꺼지는 외장 자산'이다(상위 설계 4-6).
    # 원본 체계 JSON 을 바로 읽는다 — 따로 떨어진 스냅샷(doc_taxonomy.yaml)은 없다.
    tax = taxlib.load_from_export(paths["export_input"])
    doctype_on = tax is not None

    grades_path = paths["result"]
    have_result = bool(grades_path) and os.path.isfile(grades_path)

    # ── 결과가 아직 없으면: 설정만 ──
    if not have_result:
        st.info("아직 분류한 문서가 없습니다. 아래 **③ 분류 실행**에서 폴더를 고르고 **분류 시작**을 누르세요.")
        render_settings(paths, tax, [])
        return

    try:
        records = load_records(grades_path, os.path.getmtime(grades_path))
    except Exception as e:
        uierrlog.show_error(f"분류 결과를 읽지 못했습니다: {e}", exc=e, where="결과 파일 읽기")
        return

    latest, history = load_overrides(paths["override"])
    latest_dt, history_dt = doctype_review.load_doctype_overrides(paths["override"])
    # 기준 문서 목록은 화면마다 다시 읽지 않고 여기서 한 번만 읽어 표에 실어 둔다.
    seed_files = seedstore.load_seed_files(paths.get("seed"))
    # 검토함 편입선은 정책 파일이 정한다(cso_rule.yaml). 화면마다 다시 읽지 않고
    # 여기서 한 번 읽어 넘긴다 — 두 화면이 다른 문턱을 쓰면 숫자가 어긋난다.
    low_conf = load_review_threshold(paths.get("rules") or "")
    df = records_to_df(records, latest, latest_dt, tax, seed_files, low_conf)
    run_meta = load_run_meta(grades_path)

    # ── 메뉴 ── 검토함에는 남은 건수를 붙여, 열지 않고도 할 일이 있는지 보이게 한다.
    todo_n = int((df["need_sec"] | df["need_doc"]).sum())
    labels = {MENU_HOME: MENU_HOME, MENU_BOX: MENU_BOX,
              MENU_INBOX: f"{MENU_INBOX} · {todo_n}" if todo_n else MENU_INBOX,
              MENU_SET: MENU_SET, MENU_SEED: MENU_SEED}
    # 위에서 잡아 둔 제목 옆자리에 그린다. st.empty() 는 한 요소만 담으므로
    # container() 로 감싼다 — 나중에 안내 문구를 덧붙일 여지도 남는다.
    with menu_slot.container():
        menu = st.segmented_control(
            "메뉴", list(labels), key="menu",
            format_func=lambda m: labels[m], label_visibility="collapsed") or MENU_HOME

    # 업무분류를 안 쓰는 배포에서는 기능을 숨기지 않고 '꺼짐'을 알린다(설계서 12-1).
    if not doctype_on and menu in (MENU_HOME, MENU_BOX):
        b1, b2 = st.columns([5, 1])
        b1.warning(f"{W.AXIS_DOC}는 아직 사용하지 않습니다 — 회사 분류 체계를 연결하면 "
                   "“이 문서가 무엇에 관한 것인지”도 함께 정리됩니다.")
        if b2.button("설정/분류에서 연결 →", key="go_set"):
            goto(menu=MENU_SET)

    if menu == MENU_HOME:
        render_home(df, doctype_on, run_meta, low_conf)
    elif menu == MENU_BOX:
        render_docbox(df, records, latest, history, latest_dt, history_dt,
                      paths["override"], reviewer or "", tax)
    elif menu == MENU_INBOX:
        render_inbox(df, records, latest, history, latest_dt, history_dt,
                     paths["override"], reviewer or "", tax)
    elif menu == MENU_SEED:
        render_seed_screen(tax)
    else:
        render_settings(paths, tax, records)


# Streamlit 은 이 파일을 __main__ 으로 실행한다. 가드를 두어 테스트 시 import 로
# 함수만 가져다 쓸 수 있게 한다(그때는 main 이 안 돈다).
if __name__ == "__main__":
    # 화면 어디서 예외가 터지든 파일(log/class_err_YYYYMMDD.log)에 스택까지 남긴다.
    # Streamlit 은 예외를 자기가 잡아 브라우저에만 그리기 때문에, 감싸 두지 않으면
    # 원격에 띄운 화면에서는 무슨 일이 있었는지 볼 방법이 없다.
    uierrlog.install_hooks()
    uierrlog.run_guarded(main)
