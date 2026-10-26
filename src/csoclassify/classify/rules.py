#------------------------------------------------------------------
# 규칙 스캔(Signal A) — 추출 텍스트 → C/S/O 등급 신호
#=> L1(PII)은 ko-pii 엔진에, L2(기밀사전)는 자체 키워드 매칭에 맡겨, 문서의 내용
#   위험도 등급(C/S/O)과 근거(어떤 유형이 몇 건)를 만든다.
#   [A안, 2026-08] L1 검출을 자체 정규식에서 **ko-pii(Marker-Inc-Korea,
#   MIT, 순수 파이썬)**로 전면 교체. ko-pii 가 체크섬(주민 mod11·카드 Luhn·사업자
#   국세청·법인)·문맥·회피(전각/homoglyph) 방어까지 처리하므로 정확도가 오른다.
#   [프라이버시 불변식] 매칭된 "원문 값"은 절대 저장하지 않는다 — 유형 id 와
#   건수만 남긴다. ko-pii 가 돌려준 값(span)도 즉시 버리고 건수만 센다.
#------------------------------------------------------------------

import os
import sys
from dataclasses import dataclass, field, replace as _replace

import yaml

from ..resources import resource_path, exe_dir

# ko-pii(L1 PII 엔진)는 런타임 필수 의존성이다(A안: 폴백 없음). import 실패는 스캔
# 시점에 설치 안내와 함께 분명한 오류로 알린다(모듈 로드 자체는 막지 않는다).
#
# [성능 최적화, 2026-08] ko_pii.detect_all() 은 28개 검출기를 '전부' 돌린 뒤 include
# 로 결과만 필터링한다 → 우리가 쓰지 않는 검출기(person·personal_attr 등, 밀집 입력
# 에서 O(n²))까지 매번 실행해 낭비다. 그래서 detect_all 대신, 설정 라벨에 해당하는
# 검출기만 직접 도는 _detect_subset() 를 쓴다. 정규화(전각/제로폭/homoglyph 방어)·
# 원본 더블패스·겹침 해소는 ko-pii 내부 함수를 '그대로' 재사용하므로 검출 결과·정확도
# 는 detect_all 과 동일하고 실행시간만 줄어든다(실측 약 -40%/건, 대형 문서에선 더 큼).
# 전체 엔진(_kopii_detect_all_full)은 우리가 매핑 못 한 라벨이 오면 쓰는 안전 폴백용.
#
# [2026-09-03] 위 "결과 동일"에는 예외가 두 개 생겼다. ADDRESS 의 가짜 시군구(차이 A)
# 와 IP 의 판번호 문맥(차이 B)은 ko-pii 가 틀리는 자리라 일부러 다르게 판정한다.
# Rust 판(Rust/src/pii.rs)에 먼저 들어간 것을 이식한 것이라, 두 구현이 같은 문서에
# 같은 등급을 낸다. 근거는 _jibun_admin_ok / _guarded_ip_detect 헤더에 적었고
# 문서는 doc/ko-pii 차이점.html 이다. 새 차이를 만들 때의 절차도 그쪽에 있다.
# 주의: 위 안전 폴백(_kopii_detect_all_full)으로 빠지는 경로에는 이 가드가 걸리지
# 않는다. 현재 설정 라벨은 전부 매핑돼 있어 그 경로는 타지 않는다.
try:
    import re as _re
    from ko_pii import detect_all as _kopii_detect_all_full
    from ko_pii.core.unicode_norm import (
        needs_normalization as _kopii_needs_norm,
        normalize_unicode as _kopii_normalize,
        remap_to_source as _kopii_remap,
    )
    from ko_pii.core.overlap import resolve_overlaps as _kopii_resolve_overlaps
    from ko_pii.patterns import (
        rrn as _p_rrn, frn as _p_frn, business_reg as _p_breg,
        corp_reg as _p_creg, driver_license as _p_dl, passport as _p_pass,
        card as _p_card, medical_insurance as _p_mi, prescription as _p_presc,
        pnu as _p_pnu, phone as _p_phone, email as _p_email, vehicle as _p_veh,
        address as _p_addr, nationality as _p_nat, account as _p_acct,
        edi_drug as _p_edi, court_case as _p_court,
        url as _p_url, ip as _p_ip,
    )
    # 원본 재검사(더블패스) 필요 판정용 — detect_all 의 _PII_RUN 과 동일 정의.
    _PII_RUN = _re.compile(r"[0-9A-Za-z０-９Ａ-Ｚａ-ｚ]+")
    # 주소 프리필터용 신호 — address.detect 의 4개 패턴이 '반드시' 포함해야 하는 것.
    #  · 도로명/지번(패턴1·2): (대로|로|길|동|읍|면|리) 바로 뒤 숫자(전각숫자까지 허용).
    #  · 대화체/단독(패턴3·4): _LOOSE_ANCHORS(주소·자택·살던·이사·명함 등) 키워드.
    # loose anchor 목록은 ko-pii 원본에서 그대로 import 해 버전 간 동기화 어긋남을 막는다
    # (import 실패 시 아래 except 로 떨어져 프리필터가 '항상 실행'으로 안전 폴백).
    from ko_pii.patterns.address import _LOOSE_ANCHORS as _ADDR_LOOSE_ANCHORS
    _ADDR_STRUCT_RE = _re.compile(r"(?:대로|로|길|동|읍|면|리)\s*[0-9０-９]")
    _ADDR_LOOSE_RE = _re.compile("|".join(_re.escape(a) for a in _ADDR_LOOSE_ANCHORS))
    # [의도적 차이 A·B] 아래 사전·정규식은 ko-pii 와 '다르게' 판정하기 위한 것이다.
    # 근거와 실측은 _jibun_admin_ok / _guarded_ip_detect 헤더, 문서는
    # doc/ko-pii 차이점.html. 사전은 ko-pii 원본을 그대로 빌려 써서 두 판(Python·
    # Rust)이 같은 명단을 보게 한다(Rust 는 districts.rs 의 ALL_DISTRICTS·LEGAL_DONGS).
    from ko_pii.dictionaries.districts import is_district as _kopii_is_district
    from ko_pii.dictionaries.legal_dongs import is_legal_dong as _kopii_is_legal_dong
    # Rust/src/pii.rs 의 RE_IP_CTX_LEFT / RE_IP_CTX_RIGHT 와 글자까지 같은 패턴.
    # 파이썬의 '$' 는 끝의 개행 앞에서도 맞으므로, Rust 와 어긋나지 않게 '\Z' 를 쓴다.
    _IP_CTX_LEFT_RE = _re.compile(
        r"(?:버전|버젼|펌웨어|릴리[스즈]|빌드|패치|표|그림|별표|도표|붙임|항목|조항|조|항"
        r"|단계|절|장|챕터|version|firmware|release|build|patch|section|chapter"
        r"|figure|table|appendix|clause)\s*[.·:]?\s*\Z", _re.IGNORECASE)
    _IP_CTX_RIGHT_RE = _re.compile(
        r"\s*(?:버전|버젼|version|빌드|build|릴리[스즈])", _re.IGNORECASE)
    # [보충 사전] ko-pii 의 ALL_DISTRICTS(206개)에는 광역시의 '자치구'만 있고,
    # 광역시가 아닌 시(市) 아래의 '일반구(행정구)'가 빠져 있다. 그래서 부천시
    # 원미구처럼 실존하는 주소가 시군구 검증을 통과하지 못한다.
    #   [실측] 전수 스윕 1,780건에서 '원미구 중동 1153' 2건이 이렇게 걸러졌다
    #   (2026-09-03). 등급에는 영향이 없었지만 미탐은 미탐이다.
    # 법정동 가제티어가 그 구멍을 메워 줄 것으로 봤는데 '중동'이 그 사전에도 없어
    # 두 조건이 함께 실패했다. 목록이 33개로 작고 잘 바뀌지 않아 여기 적어 둔다.
    # 전부 고유한 지명이라 '연구' 같은 평범한 낱말을 되살리지 않는다.
    _EXTRA_DISTRICTS = frozenset({
        "장안구", "권선구", "팔달구", "영통구",                    # 수원시
        "수정구", "중원구", "분당구",                              # 성남시
        "만안구", "동안구",                                        # 안양시
        "원미구", "소사구", "오정구",                              # 부천시
        "상록구", "단원구",                                        # 안산시
        "덕양구", "일산동구", "일산서구",                          # 고양시
        "처인구", "기흥구", "수지구",                              # 용인시
        "상당구", "서원구", "흥덕구", "청원구",                    # 청주시
        "동남구", "서북구",                                        # 천안시
        "완산구", "덕진구",                                        # 전주시
        "의창구", "성산구", "마산합포구", "마산회원구", "진해구",  # 창원시
    })
    _HAVE_KOPII = True
    _KOPII_IMPORT_ERR = None
except Exception as _e:   # pragma: no cover - ko-pii 미설치 환경에서만
    _HAVE_KOPII = False
    _KOPII_IMPORT_ERR = _e
    _kopii_detect_all_full = None
    _PII_RUN = None
    _ADDR_STRUCT_RE = None
    _ADDR_LOOSE_RE = None
    _p_addr = None
    # 가드용 자원이 없으면 가드는 '아무것도 거르지 않음'으로 안전하게 꺼진다.
    _kopii_is_district = None
    _kopii_is_legal_dong = None
    _IP_CTX_LEFT_RE = None
    _IP_CTX_RIGHT_RE = None
    _EXTRA_DISTRICTS = frozenset()

# 성능/지연 가드: subset(필요한 검출기만) 으로 바꾼 뒤에도 phone(O(n²))·address·
# account 때문에 검출 시간이 입력 길이에 '초선형'으로 는다(실측: 10K≈0.08s,
# 20K≈0.2s, 50K≈0.9s, 300K≈20s). 예전엔 앞 10K 만 남기고 '잘라' 지연을 눌렀지만,
# 그러면 10K 뒤쪽 PII 를 전부 놓쳤다. 지금은 두 단계로 '커버리지 회복 + 지연 선형화'
# 를 함께 얻는다(설계: A안 + 초선형 청킹).
#   (1) 선형 검출기(초선형 셋 밖): 전체 텍스트를 한 번에 훑는다. 정규식이 선형이라
#       길이에 비례해 싸고 '전량' 커버가 된다(상한 없음).
#   (2) 초선형 검출기(_KOPII_SUPERLINEAR): _KOPII_MAX_CHARS(=청크 크기, 10K) 창으로
#       잘라 돌린다. 통짜 1회의 초선형 비용(50K=0.9s) 대신 창마다 0.08s 고정이라
#       총비용이 선형이 된다(50K=5×0.08≈0.4s). 인접 창은 _KOPII_OVERLAP 만큼 겹쳐
#       경계에 걸친 PII 미탐을 막고, 절대 오프셋 구간 병합으로 경계 중복은 없앤다.
#       최악 지연을 묶으려 초선형 스캔 범위는 앞 _KOPII_MAX_TOTAL 자로 상한을 둔다
#       (기본 100K ≈ ~1s). 선형 검출기는 이 상한과 무관하게 전량 커버.
# 세 값 모두 환경변수로 조절 가능(배치=커버리지 우선이면 상향, 실시간=지연 우선이면 하향).
def _env_pos_int(name, default):
    # 환경변수를 양의 정수로 읽되, 없거나 잘못됐거나 0 이하면 안전 기본값으로 되돌린다
    # (0/음수면 검출이 통째로 꺼지거나 무한루프가 날 수 있어 방어).
    try:
        v = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


_KOPII_MAX_CHARS = _env_pos_int("CSOCLASSIFY_KOPII_MAX_CHARS", 10_000)    # 청크 크기(=소형 문서 단일패스 기준)
# 초선형 검출기 총 스캔 상한.
#=> 2026-09-02 에 100K → 1M 으로 올렸다. 이유: 20초 걸리던 문제를 실제로 푼 것은
#   '청킹'이지 이 상한이 아니었다(실측 최악 밀도 300K: 통짜 17.8초 → 청킹 0.33초).
#   청킹이 비용을 선형으로 만들어 놓아서, 상한은 100K 당 약 0.18초(최악 밀도 기준)
#   로 예측 가능하게만 늘어난다. 그런데 100K 상한의 대가는 컸다 — 그 뒤에 있는
#   전화·주소·계좌를 '통째로' 못 잡아, 120K 문서 실측에서 기밀 문서가 O(공개)로
#   나갔다. 아끼는 시간(실제 주소록 300K 기준 0.1초 남짓)보다 잃는 것이 크다.
#   상한을 아주 없애지는 않는다 — 10MB 같은 터무니없는 입력에 대한 최후 방어는 남긴다.
_KOPII_MAX_TOTAL = _env_pos_int("CSOCLASSIFY_KOPII_MAX_TOTAL", 1_000_000)
_KOPII_OVERLAP = _env_pos_int("CSOCLASSIFY_KOPII_OVERLAP", 128)           # 인접 청크 오버랩(최장 PII span 보다 크게)
# 총상한이 청크보다 작으면 최소 한 청크는 돌도록, 오버랩이 청크 이상이면 창이 전진
# 못 하므로(무한루프) 각각 안전하게 보정한다.
if _KOPII_MAX_TOTAL < _KOPII_MAX_CHARS:
    _KOPII_MAX_TOTAL = _KOPII_MAX_CHARS
if _KOPII_OVERLAP >= _KOPII_MAX_CHARS:
    _KOPII_OVERLAP = _KOPII_MAX_CHARS // 4

# 입력 길이에 '초선형'으로 비싸지는 검출기 라벨(청킹 대상). 나머지는 선형이라 전량 스캔한다.
# (근거: 위 실측 초선형 비용의 원인은 phone(O(n²))·address·account 세 검출기다.)
_KOPII_SUPERLINEAR = frozenset({"PHONE", "ADDRESS", "ACCOUNT"})

# 스탬프(Signal E) 게이트 파라미터.
#=> 키워드(L2)와의 차이: 본문 중 '단발 언급'("대외비가 아님" 같은)의 오탐을 막으려고,
#   문서 '머리(앞 _STAMP_HEAD_CHARS 자)' 안에 있거나 '_STAMP_REPEAT_MIN 회 이상 반복'
#   (페이지마다 찍히는 워터마크 정황)일 때만 진짜 스탬프로 인정한다. 두 값 모두 환경변수로
#   조정 가능(배치=회수 우선이면 상향, 실시간=민감 우선이면 하향).
_STAMP_HEAD_CHARS = _env_pos_int("CSOCLASSIFY_STAMP_HEAD_CHARS", 400)   # '머리' 판정 범위(문자수)
_STAMP_REPEAT_MIN = _env_pos_int("CSOCLASSIFY_STAMP_REPEAT_MIN", 2)     # 반복 인정 최소 횟수


#------------------------------------------------------------------
# 영숫자 런 모양 계산 (더블패스 필요 판정)
#=> 문자열에서 '영숫자 덩어리'들의 길이 나열을 만든다. 정규화(제로폭 제거 등)가
#   이 모양을 바꿨다면 인접 PII 두 개가 경계에서 융합/분리된 것이므로 원본도 다시
#   검사해야 한다. detect_all 내부의 동명 헬퍼와 동일한 판정을 재현한다.
#
# -in: s = 검사할 문자열(정규화본 또는 원본)
#
# -out: list[int] = 각 영숫자 런의 길이 목록
# -out: error = 없음 (_PII_RUN 미초기화 시엔 호출되지 않음 — _HAVE_KOPII 가드 하에서만 사용)
#------------------------------------------------------------------
def _run_shape(s):
    return [m.end() - m.start() for m in _PII_RUN.finditer(s)]


#------------------------------------------------------------------
# 주소 검출 프리필터 (address.detect 실행 여부 판정)
#=> address.detect 는 4개 정규식을 전체 텍스트에 훑어 주소 없는 문서에서도 비용이
#   든다(실측 10K≈24ms). 이 함수는 '주소가 있을 가능성' 신호가 하나도 없으면
#   False 를 돌려 그 비싼 검출을 통째로 건너뛰게 한다.
#    - 신호1: (대로|로|길|동|읍|면|리) 바로 뒤 숫자 → 도로명/지번 주소 후보
#    - 신호2: _LOOSE_ANCHORS(주소·자택·살던·이사·명함 등) → 대화체/단독 주소 후보
#   [안전 불변식] address.detect 의 4개 패턴은 모두 위 두 신호 중 하나를 '반드시'
#   포함하므로, 신호가 없으면 매칭도 없다 → False 를 줘도 미탐이 생기지 않는다.
#   (실측: 실제 주소 표본 전건이 프리필터를 통과, 미탐 0)
#
# -in: text = 검사할 텍스트(정규화본 또는 원본)
#
# -out: bool = 주소 검출을 돌려야 하면 True, 확실히 없으면 False
# -out: error = 없음 (ko-pii import 실패로 정규식 미초기화면 항상 True → 안전)
#------------------------------------------------------------------
def _address_prefilter(text):
    # 정규식 미초기화(ko-pii 미설치 등)면 판정 불가 → 항상 실행해 안전하게 처리.
    if _ADDR_STRUCT_RE is None:
        return True
    return bool(_ADDR_STRUCT_RE.search(text) or _ADDR_LOOSE_RE.search(text))


#------------------------------------------------------------------
# 지번 주소 — 가짜 시군구 거르기 [ko-pii 와 의도적으로 다름]
#=> ko-pii 는 지번 주소를 "시군구로 끝나는 낱말 + 동/읍/면/리로 끝나는 낱말 + 숫자"
#   로 찾은 뒤, 광역(시·도)이 있을 때만 진짜 행정구역인지 확인한다. 광역이 없으면
#   '시군구 자리가 비어 있지 않다'는 이유만으로 통과시킨다(_has_anchor 의 "prefix").
#   그래서 주소가 아닌 말이 주소가 된다 — ko-pii 가 스스로 내놓는 출력이다.
#       detect_all("연구 정보관리 1", include=["ADDRESS"])
#         → ADDRESS conf=0.75 extra={'city': None, 'districts': '연구',
#                                    'dong': '정보관리', 'lot_number': '1'}
#   '연구'를 區, '정보관리'를 里 로 읽은 것이다. 2026-09-02 D:\분류함 실측에서
#   인사평가 엑셀 5개에 이 모양이 10건 잡혀, 주소가 하나도 없는 문서의 등급이
#   S 로 올라갔다.
#
#   [왜 우리가 고치나] 1.15.2 가 PyPI 최신이고, 업스트림 main 의 address.py 도
#   설치본과 한 줄도 다르지 않다(347줄 동일, 2026-09-02 확인). 버전을 올려
#   해결될 문제가 아니라서 우리 쪽에서 막는다.
#
#   [무엇을 다르게 하나] 광역이 없을 때, 시군구 토큰과 동/읍/면/리 토큰 중
#   최소 하나는 진짜 행정구역 이름이어야 한다고 요구한다. 둘 다 사전에 없으면
#   주소로 보지 않는다.
#
#   [왜 '둘 다'가 아니라 '둘 중 하나'인가] "시군구가 사전에 있어야 한다"로 못박으면
#   사전에 빠진 기초자치단체에서 진짜 주소를 놓친다(ALL_DISTRICTS 는 206개로
#   실제 226개 전수가 아니다). 법정동 가제티어는 10,368개로 훨씬 촘촘하니, 둘 중
#   하나만 맞아도 통과시키면 오탐을 걷어내면서 미탐을 최소화할 수 있다.
#   [측정] 그래도 0 은 아니다 — 전수 스윕(1,780건, 2026-09-03)에서 두 조건이 함께
#   실패한 실주소가 나왔다('원미구 중동 1153'). 일반구가 ko-pii 사전에 없고
#   '중동'이 법정동 가제티어에도 없어서다. 그래서 _EXTRA_DISTRICTS 를 덧댔다.
#     · "연구 정보관리 1"    → 연구✗ + 정보관리✗ → 거부 (오탐 제거)
#     · "성남시 정자동 100"  → 성남시✓           → 통과
#     · "없는시 매곡리 100"  → 없는시✗ + 매곡리✓ → 통과 (사전에 없는 시라도 살아남음)
#     · "원미구 중동 1153"  → 원미구✓(보충 사전) → 통과 (실측 미탐을 막은 자리)
#
#   [적용 범위] 지번(format="jibun") 에만 건다. 도로명은 '로/길 + 번호'라는 신호가
#   훨씬 강해 이 판정을 그대로 걸면 미탐 위험만 커진다. 도로명에서 관측된 오탐
#   (낱말이 쪼개진 "실시간으로 3")은 더 좁은 _road_admin_ok 가 따로 막는다.
#
#   [Rust 와의 관계] Rust/src/pii.rs 의 jibun_admin_ok() 과 같은 판정이다. 그쪽이
#   먼저 들어갔고 이건 그 이식본이라, 두 판이 같은 문서에 같은 등급을 낸다.
#
# -in: m = ko-pii DetectionResult (extra 에 format·city·districts·dong 이 들어 있다)
#
# -out: bool = 주소로 인정하면 True, 가짜 행정구역이라 버리면 False
# -out: error = 없음 (사전 import 실패 시엔 항상 True → 종전 동작)
#------------------------------------------------------------------
def _jibun_admin_ok(m):
    # 사전을 못 불러왔으면 판정 불가 → 거르지 않는다(ko-pii 원래 동작 유지).
    if _kopii_is_district is None:
        return True
    extra = m.extra or {}
    # 지번 브랜치가 아니면(도로명·대화체·단독) 손대지 않는다.
    if extra.get("format") != "jibun":
        return True
    # 광역이 있으면 ko-pii 가 이미 (광역)·(광역+기초) 조합을 검증했다.
    if extra.get("city"):
        return True
    parts = (extra.get("districts") or "").split()
    first_district = parts[0] if parts else ""
    # 시군구 토큰 자체가 없으면 ko-pii 도 좌측 20자에 '주소' 낱말을 요구한다
    # (_has_anchor 의 keyword 경로) — 근거가 이미 있으므로 여기선 통과시킨다.
    if not first_district:
        return True
    # 둘 중 하나라도 진짜 행정구역 이름이면 주소로 본다.
    # 시군구는 ko-pii 사전 + 보충 사전(일반구)을 함께 본다.
    return bool(_kopii_is_district(first_district)
                or first_district in _EXTRA_DISTRICTS
                or _kopii_is_legal_dong(extra.get("dong") or ""))


#------------------------------------------------------------------
# 도로명 주소 — 낱말 속 가짜 시군구 거르기 [ko-pii 와 의도적으로 다름]
#=> ko-pii 도로명 갈래는 "시군구로 끝나는 글자 + 로/길로 끝나는 글자 + 번호" 를 찾는데,
#   둘 사이에 띄어쓰기를 요구하지 않는다. 그래서 낱말 하나가 쪼개져 주소가 된다.
#       detect_all("실시간으로 3D 인물", include=["ADDRESS"])
#         → ADDRESS extra={'city': None, 'districts': '실시', 'road': '간으로', 'building_number': '3'}
#   '실시간으로'를 '실시(市)' + '간으로(路)' 로 읽은 것이다(2026-10-26 배포 교차 검증,
#   D:\sample\4_디지털콘텐츠_통합보고서_업로드용.pdf).
#
#   [무엇을 다르게 하나] 아래 셋이 모두 맞을 때만 버린다 — 진짜 주소를 놓치지 않게 좁게 막는다.
#     1) 광역(시·도)이 없다 — 있으면 ko-pii 가 이미 조합을 검증했다
#     2) 마지막 시군구 토큰과 도로명이 띄어쓰기 없이 붙어 있다 — 진짜 주소는 대개
#        "강남구 테헤란로" 처럼 띄어 쓴다
#     3) 그 시군구 토큰이 진짜 행정구역 이름이 아니다(ko-pii 사전 + 보충 사전)
#     · "실시간으로 3"        → 실시✗ · 붙음  → 거부 (오탐 제거)
#     · "강남구 테헤란로 152" → 띄어 씀       → 통과
#     · "강남구테헤란로 152"  → 붙었지만 강남구✓ → 통과
#
#   [Rust 와의 관계] Rust/src/pii.rs 의 road_admin_ok() 와 같은 판정이다. 버린 자리는
#   ko-pii 안에서 이미 '본 자리'로 남아 지번 갈래가 다시 잡지 않는다 — Rust 도 같게 한다.
#
# -in: m    = ko-pii DetectionResult (extra 에 format·city·districts·road 가 들어 있다)
#
# -out: bool = 주소로 인정하면 True, 낱말이 쪼개진 가짜 주소라 버리면 False
# -out: error = 없음 (사전 import 실패 시엔 항상 True → 종전 동작)
#------------------------------------------------------------------
def _road_admin_ok(m):
    if _kopii_is_district is None:
        return True
    extra = m.extra or {}
    if extra.get("format") != "road_name" or extra.get("city"):
        return True
    parts = (extra.get("districts") or "").split()
    road = extra.get("road") or ""
    if not parts or not road:
        return True
    last = parts[-1]
    # 띄어 쓴 주소는 건드리지 않는다 — 붙어 있을 때만 의심한다
    if (last + road) not in (m.text or ""):
        return True
    return bool(_kopii_is_district(last) or last in _EXTRA_DISTRICTS)


#------------------------------------------------------------------
# 주소 검출기 (프리필터 + 가짜 시군구 가드)
#=> _LABEL_TO_DETECTOR 에서 ADDRESS 자리에 원본 address.detect 대신 이걸 넣는다.
#    1) 프리필터가 '주소 없음'이라 판정하면 비싼 검출을 건너뛴다(비용 절감, 결과 동일)
#    2) 검출된 지번 주소 중 가짜 행정구역을 _jibun_admin_ok 로 걸러낸다(오탐 제거)
#    3) 도로명 중 낱말이 쪼개진 가짜 주소를 _road_admin_ok 로 걸러낸다(오탐 제거)
#
# -in: text = 스캔 대상 텍스트
#
# -out: iterable[DetectionResult] = 주소 검출 결과(없거나 프리필터 skip 이면 빈 튜플)
# -out: error = 없음
#------------------------------------------------------------------
def _gated_address_detect(text):
    # 주소 신호가 전혀 없으면 address.detect 자체를 호출하지 않는다.
    if not _address_prefilter(text):
        return ()
    return [m for m in _p_addr.detect(text) if _jibun_admin_ok(m) and _road_admin_ok(m)]


#------------------------------------------------------------------
# IP 검출기 — 판번호 문맥 배제 [ko-pii 와 의도적으로 다름]
#=> ko-pii 는 한글 문맥("버전 1.2.3.4")만 걸러내고, 영문 문서에서는 판번호를 IP 로
#   잡는다("Release 1.2 / 1.2.3.4 Build 7" → IP 검출). 986개 문서 실측에서 이
#   차이가 19건이었고 전부 판번호였다 — 맞추면 오히려 나빠지므로 우리 쪽이 더
#   엄격하게 간다.
#    1) 매치 왼쪽 12자에 판·절·표 따위의 낱말이 붙어 있으면 버린다
#    2) 매치 오른쪽 8자가 "버전/build/릴리스" 로 시작하면 버린다
#
#   [적용 범위] IPv4 에만 건다(Rust 도 det_ipv4 에서만 검사한다). IPv6 는 이런
#   모양의 오탐이 성립하지 않는다.
#
#   [Rust 와의 관계] Rust/src/pii.rs 의 RE_IP_CTX_LEFT / RE_IP_CTX_RIGHT 와 같은
#   패턴·같은 창 크기(왼쪽 12자·오른쪽 8자)다.
#
# -in: text = 스캔 대상 텍스트
#
# -out: list[DetectionResult] = 판번호 문맥을 걷어낸 IP 검출 결과
# -out: error = 없음 (정규식 미초기화면 원본 결과를 그대로 돌려준다)
#------------------------------------------------------------------
def _guarded_ip_detect(text):
    if _IP_CTX_LEFT_RE is None:
        return _p_ip.detect(text)
    out = []
    for m in _p_ip.detect(text):
        # IPv6 는 대상이 아니다 — 라벨은 같아도 version 으로 갈린다.
        if (m.extra or {}).get("version") == 4:
            if _IP_CTX_LEFT_RE.search(text[max(0, m.start - 12):m.start]):
                continue
            if _IP_CTX_RIGHT_RE.match(text[m.end:m.end + 8]):
                continue
        out.append(m)
    return out


# 설정 라벨 → 검출기. 튜플 순서는 ko_pii.detect.DETECTORS 실행 순서를 보존해,
# 겹침 해소의 '동점 시 먼저 시작한 span' 규칙까지 detect_all 과 동일하게 만든다.
# 두 자리만 원본 대신 우리 래퍼를 쓴다.
#   · ADDRESS → _gated_address_detect : 프리필터(비용 절감) + 가짜 시군구 거부(차이 A)
#   · IP      → _guarded_ip_detect    : 판번호 문맥 배제(차이 B)
# 이 둘은 ko-pii 와 '일부러' 결과가 다르다 — 근거는 각 함수 헤더와
# doc/ko-pii 차이점.html. import 실패(_HAVE_KOPII=False)면 빈 맵으로 둔다.
if _HAVE_KOPII:
    _LABEL_TO_DETECTOR = (
        ("RRN", _p_rrn.detect), ("FRN", _p_frn.detect),
        ("BUSINESS_REG", _p_breg.detect), ("CORP_REG", _p_creg.detect),
        ("DRIVER_LICENSE", _p_dl.detect), ("PASSPORT", _p_pass.detect),
        ("CARD", _p_card.detect), ("MEDICAL_INSURANCE", _p_mi.detect),
        # 처방전 주의: 검출기는 'PRESCRIPTION_ID' 라벨을 내보내지만 cso_rule.yaml 은
        # 'PRESCRIPTION' 을 쓴다. 두 별칭 모두 이 검출기를 고르게 하되, 실제 결과는
        # 최종 include 필터에서 라벨 불일치로 걸러진다 → 현재 detect_all 동작(설정상
        # 처방전 0건)과 정확히 일치. (설정 라벨 오타로 보이나, 본 패치는 동작 보존이
        # 목적이라 고치지 않는다. 처방전을 실제로 잡으려면 yaml label 을 고쳐야 함.)
        ("PRESCRIPTION", _p_presc.detect), ("PRESCRIPTION_ID", _p_presc.detect),
        ("PNU", _p_pnu.detect), ("PHONE", _p_phone.detect),
        ("EMAIL", _p_email.detect), ("VEHICLE", _p_veh.detect),
        ("ADDRESS", _gated_address_detect), ("NATIONALITY", _p_nat.detect),
        ("ACCOUNT", _p_acct.detect), ("EDI_DRUG", _p_edi.detect),
        ("COURT_CASE", _p_court.detect),
        ("URL", _p_url.detect), ("IP", _guarded_ip_detect),
    )
    _MAPPED_LABELS = frozenset(lbl for lbl, _ in _LABEL_TO_DETECTOR)
else:
    _LABEL_TO_DETECTOR = ()
    _MAPPED_LABELS = frozenset()


#------------------------------------------------------------------
# 필요한 검출기만 실행 (detect_all 축소판)
#=> ko_pii.detect_all 은 28개 검출기를 전부 돌린 뒤 include 로 결과만 걸러 낭비가
#   크다. 이 함수는 요청 라벨(include)에 해당하는 검출기만 골라 돌려 같은 결과를
#   더 빠르게 낸다. 정규화·역매핑·겹침해소는 ko-pii 내부 함수를 그대로 써서
#   detect_all 과 결과가 동일하도록 맞춘다.
#    1) 매핑 못 한 라벨이 섞였으면 정확도 보장 위해 전체 엔진으로 폴백
#    2) 요청 라벨에 해당하는 검출기만 (중복 없이, 원래 순서로) 선택
#    3) normalize=True 면 전각/제로폭/homoglyph 우회를 detect_all 과 똑같이 방어
#       (정규화본 검사 → 원본 역매핑 → 런 모양이 바뀌었으면 원본 더블패스)
#    4) include 로 최종 필터 후 겹침 해소해 반환
#
# -in: text      = 스캔 대상 텍스트
# -in: include   = 셀 ko-pii 라벨들(반복가능). 이 라벨의 검출기만 실행한다.
# -in: normalize = True(기본)면 우회 방어 정규화 수행. detect_all 과 동일 의미.
#
# -out: list[DetectionResult] = include 라벨에 해당하는 검출 결과(겹침 해소됨)
# -out: error = 없음 (ko-pii 미설치 시엔 _kopii_counts 가 먼저 RuntimeError 를 낸다)
#------------------------------------------------------------------
def _detect_subset(text, include, *, normalize=True):
    inc = set(include)
    # (1) 우리가 매핑 못 한 라벨이 요청에 있으면(예: 향후 yaml 에 PERSON 추가) 놓치지
    #     않도록 전체 엔진으로 안전 폴백한다. 현재 설정 라벨은 모두 매핑돼 있어 미발생.
    if inc - _MAPPED_LABELS:
        return _kopii_detect_all_full(text, include=list(inc), normalize=normalize)
    # (2) 요청 라벨에 걸리는 검출기만, detect_all 순서를 보존해 중복 없이 고른다.
    seen = set()
    detectors = []
    for label, fn in _LABEL_TO_DETECTOR:
        if label in inc and fn not in seen:
            seen.add(fn)
            detectors.append(fn)
    if not detectors:
        return []

    source = text
    offset_map = None
    # (3) detect_all 과 동일한 정규화 경로: 전각/호환문자 폴딩 + 제로폭 제거로 우회 차단.
    if normalize and _kopii_needs_norm(text):
        norm, omap = _kopii_normalize(text)
        if norm != text:
            text, offset_map = norm, omap

    raw = []
    for fn in detectors:
        raw.extend(fn(text))

    if offset_map is not None:
        # 정규화본 검출 offset 을 원본 기준으로 역매핑.
        raw = _kopii_remap(raw, offset_map, source)
        # 보이지 않는 문자 제거가 '영숫자 런 모양'을 바꾼 경우(=PII 경계 융합/분리)만
        # 원본을 한 번 더 검사해 합집합한다. detect_all 의 더블패스 조건과 동일.
        if _run_shape(source) != _run_shape(text):
            for fn in detectors:
                raw.extend(fn(source))

    # (4) detect_all 과 동일하게 include 로 최종 필터(검출기가 include 밖 라벨을 낼
    #     경우 방어; 예: prescription 이 PRESCRIPTION_ID 를 내면 걸러짐) 후 겹침 해소.
    raw = [d for d in raw if d.label in inc]
    return _kopii_resolve_overlaps(raw)


# 등급 서열(높을수록 민감). None 은 "해당 없음".
_GRADE_RANK = {"O": 0, "S": 1, "C": 2}

# 정의된 등급을 '서열 낮은 → 높은' 순으로 나열한 튜플. 오류 메시지에 "O < S < C" 처럼
# 보여 주거나, 유효성 검사에서 "이 값이 등급 맞나"를 볼 때 쓴다.
GRADES = tuple(sorted(_GRADE_RANK, key=_GRADE_RANK.get))


#------------------------------------------------------------------
# 알 수 없는 등급 문자열 예외
#=> 등급 서열에 없는 값(예: 소문자 "c", 오타 "SS")이 등급 계산에 들어왔을 때 던진다.
#   예전에는 이런 값을 조용히 '무시'해서 그 규칙이 판정에서 통째로 빠졌고, 결과적으로
#   문서 등급이 실제보다 낮게 나오는 사고로 이어졌다(fail-open). 이제는 소리 내어 실패한다.
#
# -필드: grade = 문제가 된 원본 값(그대로 보존해 사용자가 오타를 눈으로 확인)
#------------------------------------------------------------------
class UnknownGradeError(ValueError):

    #------------------------------------------------------------------
    # 예외 생성
    #=> 문제 값과 "정의된 등급은 무엇인지"를 한 문장으로 만들어 담는다.
    #
    # -in: grade = 서열에 없는 등급 값(문자열이 아닐 수도 있어 repr 로 보여 준다)
    # -in: where = 어디서 나왔는지 설명(예: "regex_pii[rrn].base_grade"). 없으면 생략
    #
    # -out: 없음(생성자)
    # -out: error = 없음
    #------------------------------------------------------------------
    def __init__(self, grade, where=None):
        self.grade = grade
        loc = f" ({where})" if where else ""
        super().__init__(
            f"정의되지 않은 등급 값{loc}: {grade!r} — 정의된 등급: {' < '.join(GRADES)}"
        )


#------------------------------------------------------------------
# 규칙셋 검증 실패 예외
#=> cso_rule.yaml 을 읽는 데는 성공했지만 내용이 규칙에 맞지 않을 때 던진다.
#   위반을 '처음 하나에서 멈추지 않고 전부 모아' 담는 게 핵심 — 관리자가 한 번에
#   고칠 수 있어야 하기 때문이다.
#
# -필드: path       = 문제의 규칙셋 파일 경로
# -필드: violations = 위반 목록. 각 항목은 dict
#                     {code, section, rule_id, field, value, detail, hint}
#------------------------------------------------------------------
class RuleSetValidationError(Exception):

    #------------------------------------------------------------------
    # 예외 생성
    #=> 파일 경로와 위반 목록을 담고, 사람이 읽을 요약문을 메시지로 만든다.
    #
    # -in: path       = 규칙셋 파일 경로
    # -in: violations = 위반 dict 리스트(빈 리스트로는 만들지 않는다)
    #
    # -out: 없음(생성자)
    # -out: error = 없음
    #------------------------------------------------------------------
    def __init__(self, path, violations):
        self.path = path
        self.violations = list(violations)
        super().__init__(format_violations(path, self.violations))

# weight → 신뢰도 기본값(cso_rule.yaml 의 confidence 블록으로 덮어쓸 수 있음).
# 신뢰도는 '검토 큐' 편입/정렬에만 쓰이고 C/S/O 등급은 바꾸지 않는다.
_REGEX_CONF = {"high": 0.90, "medium": 0.70, "low": 0.50}
_KEYWORD_CONF = {"high": 0.85, "medium": 0.70, "low": 0.50}
# 스탬프는 '작성자의 명시적 분류 의도'라 오탐이 매우 낮다 → 키워드보다 높게 잡는다.
_STAMP_CONF = {"high": 0.95, "medium": 0.80, "low": 0.65}
# 민감정보군(법상 민감정보) 키워드 — 키워드 기반이라 신뢰도는 기밀사전과 비슷하게 둔다.
_SENSITIVE_CONF = {"high": 0.85, "medium": 0.70, "low": 0.55}


#------------------------------------------------------------------
# 기본 신뢰도 표(정규식/키워드/경로/파일명)
#=> cso_rule.yaml 에 confidence 블록이 없을 때 쓰는 폴백. yaml 값이 있으면
#   load_rules 가 이 위에 덮어써 RuleSet.confidence 로 담는다.
#
# -in: 없음
# -out: dict = {"regex":{high/med/low}, "keyword":{...}, "sensitive":{...}, "stamp":{...}, "path":{...}, "name":float}
# -out: error = 없음
#------------------------------------------------------------------
def _default_confidence():
    return {
        "regex": dict(_REGEX_CONF),
        "keyword": dict(_KEYWORD_CONF),
        "sensitive": dict(_SENSITIVE_CONF),
        "stamp": dict(_STAMP_CONF),
        "path": {"high": 0.90, "medium": 0.70, "low": 0.50},
        "name": 0.60,
    }


#------------------------------------------------------------------
# 등급 최댓값 계산 (보수적 융합의 핵심)
#=> 여러 등급 후보 중 "가장 높은(민감한)" 등급을 고른다. C > S > O 서열을 쓰며,
#   None(해당 없음)은 무시한다. 후보가 전부 None 이면 None 을 반환한다.
#    1) None 은 "판단 근거 없음"이라 건너뛴다(정상)
#    2) 서열에 없는 값이 오면 조용히 넘기지 않고 UnknownGradeError 로 실패시킨다
#    3) _GRADE_RANK 로 순위를 매겨 최댓값 선택
#
#   [왜 예외인가] 예전에는 모르는 값을 rank -1 로 취급해 그냥 건너뛰었다. 그러면
#   cso_rule.yaml 에 'base_grade: c' 같은 오타가 있어도 아무 경고 없이 그 규칙만
#   판정에서 빠져 문서 등급이 실제보다 낮게 나온다. 등급이 낮게 나오는 실패는
#   보안 사고라, 조용히 넘기는 대신 시끄럽게 멈추는 쪽이 안전하다.
#   규칙셋은 load_rules 에서 미리 검증하므로, 정상 경로에서는 이 예외가 나지 않는다.
#
# -in: grades = 등급 문자열/None 들의 반복가능 객체 (예: ["O","C",None])
# -in: where  = 오류 메시지에 넣을 위치 설명(선택). 예: "signals.rule.grade"
#
# -out: grade = 가장 높은 등급 문자열, 전부 None 이면 None
# -out: error = 서열에 없는 값이 있으면 UnknownGradeError
#------------------------------------------------------------------
def max_grade(grades, where=None):
    best = None
    best_rank = -1
    for g in grades:
        # None 은 "이 신호는 등급을 내지 않았다"는 정상 상태 → 후보에서만 제외.
        if g is None:
            continue
        r = _GRADE_RANK.get(g)
        # 서열에 없는 값 = 오타이거나 다른 등급체계의 값 → 즉시 실패(fail-closed).
        if r is None:
            raise UnknownGradeError(g, where)
        if r > best_rank:
            best_rank, best = r, g
    return best


#------------------------------------------------------------------
# 스캔 기본값 묶음
#=> cso_rule.yaml 의 defaults 블록을 담는다. 개별 규칙이 값을 지정하지 않았을 때
#   여기 값을 대신 쓴다.
#
# -필드: bulk_threshold  = 이 건수 이상이면 bulk_grade 로 상향(명단/대장 정황)
# -필드: case_insensitive = True 면 영문 키워드 대소문자 무시
# -필드: review_threshold = 검토함 편입선(이보다 확신이 낮으면 사람이 본다, 기본 0.6)
# -필드: pii_grade_cap    = PII(L1·COMBO) 근거만으로 올릴 수 있는 최고 등급(None=제한 없음)
#------------------------------------------------------------------
@dataclass(frozen=True)
class Defaults:
    bulk_threshold: int = 5
    case_insensitive: bool = True
    # 검토함 편입선(2026-09-21). 이 값보다 확신이 낮은 판정은 사람이 한 번 본다.
    # 등급(C/S/O)은 이 값과 무관하다 — '사람이 볼까 말까'만 가른다.
    # 예전에는 화면 코드에 박혀 있어(ui/app.py 의 LOW_CONF) 고치려면 배포가
    # 필요했다. 고객마다 검토량이 다를 수 있어 정책 파일로 옮겼다.
    review_threshold: float = 0.6
    # 개인정보(PII)만으로 올릴 수 있는 최고 등급(2026-09-21). None 이면 제한 없음(종전).
    # 예: "S" 면 주민번호 대량·신원+금융 결합 같은 PII 근거는 S 까지만 올린다.
    # 키워드·민감정보·스탬프·파일명 근거가 C 를 내면 그 C 는 그대로다 —
    # 막는 것은 'PII 하나로만' C 가 되는 경우다. 고객마다 기준이 달라 정책 칸으로 둔다.
    pii_grade_cap: str = None


#------------------------------------------------------------------
# PII 유형 규칙 1개(L1) — ko-pii 라벨 → 등급 정책
#=> 검출은 ko-pii 가 하고(패턴·체크섬·문맥 내장), 이 규칙은 "그 유형이 몇 건이면
#   몇 등급인가"라는 정책만 담는다. (A안 이전엔 정규식 pattern 을 직접 담았다)
#
# -필드: id            = 유형 식별자 (예: "rrn") — 결과 근거의 rule_id
# -필드: name          = 사람이 읽는 이름 (예: "주민등록번호")
# -필드: label         = 대응하는 ko-pii 라벨 (예: "RRN","CARD","BUSINESS_REG")
# -필드: base_grade    = 1건이라도 있으면 부여할 등급
# -필드: bulk_grade    = bulk_threshold 초과 시 상향 등급(없으면 상향 안 함)
# -필드: bulk_threshold = 이 유형 전용 임계값(없으면 Defaults 값 사용)
# -필드: weight        = 신뢰도 가중("high"|"medium"|"low")
# -필드: seed_eligible = True 면 이 히트로 확정된 문서를 전파 seed 로 승격 가능
#------------------------------------------------------------------
@dataclass(frozen=True)
class RegexRule:
    id: str
    name: str
    label: str
    base_grade: str = "S"
    bulk_grade: str = None
    bulk_threshold: int = None
    weight: str = "medium"
    seed_eligible: bool = False


#------------------------------------------------------------------
# 키워드 규칙 1개(L2)
#=> 기밀사전. 지정한 단어가 텍스트에 있으면 히트로 본다.
#
# -필드: id            = 규칙 식별자 (예: "mark_confidential")
# -필드: name          = 사람이 읽는 이름 (예: "기밀 표식")
# -필드: terms         = 탐지 단어 리스트
# -필드: exclude       = 제외 문구 리스트. 단어가 이 문구 안에 든 매치는 오탐으로 보고
#                        세지 않는다(예: terms=[전과], exclude=[산전과·충전과]).
# -필드: term_min_count = 단어별 최소 등장 횟수 ((단어, 횟수) 짝의 튜플). 본문에서 그 횟수 미만이면
#                        그 단어는 세지 않는다. 적지 않은 단어는 1(종전 동작).
#                        예: (("징계", 3),) — 규정 말미 "위반 시 징계" 한 줄로 C 가 되지 않게.
#                        파일명 신호(filename)에는 적용하지 않는다.
# -필드: base_grade    = 1건이라도 있으면 부여할 등급
# -필드: bulk_grade    = bulk_threshold 초과 시 상향 등급(없으면 상향 안 함)
# -필드: bulk_threshold = 이 규칙 전용 임계값(없으면 Defaults 값 사용)
# -필드: weight        = 신뢰도 가중
# -필드: seed_eligible = 전파 seed 승격 가능 여부
#------------------------------------------------------------------
@dataclass(frozen=True)
class KeywordRule:
    id: str
    name: str
    terms: tuple
    # 파일명에서 찾을 말. 본문(terms)과 **따로** 적는다 — 안 적으면 이 규칙은
    # 파일명 신호를 만들지 않는다(2026-09-08, 설계 4장 P2). terms 로 되돌아가는
    # 폴백을 두지 않는 이유: 폴백은 '본문 단어가 파일명에 새는' 지금의 오탐을
    # 기본값으로 굳힌다(실측: 파일명 신호 99건 중 79건이 제품명 규칙 하나였다).
    filename: tuple = ()
    exclude: tuple = ()
    term_min_count: tuple = ()
    base_grade: str = "S"
    bulk_grade: str = None
    bulk_threshold: int = None
    weight: str = "medium"
    seed_eligible: bool = False


#------------------------------------------------------------------
# 결합식별성 규칙 1개(L1-combo) — PII '조합' 가중
#=> 개별 PII 는 등급이 낮아도(예: 전화·이메일 O), 여러 유형이 '한 문서에' 모이면
#   특정 개인을 재식별하기 쉬워진다(GDPR/개인정보보호법의 '결합용이성'). 이 규칙은
#   그런 조합을 만나면 상향 등급을 준다. 판정은 '문서 단위 공존'만 본다(근접성 아님).
#    - all_of  : 이 라벨들이 '모두' 있으면 성립(예: RRN+ACCOUNT → 신원+금융)
#    - of+min_types : of 그룹에서 '서로 다른' 라벨이 min_types 종 이상이면 성립
#                     (예: 연락처 [PHONE,EMAIL,ADDRESS] 중 2종 이상 → 개인 특정)
#   두 조건을 다 적으면 둘 다 만족해야 한다(AND). 하나도 안 적으면 그 규칙은 무시.
#
# -필드: id            = 규칙 식별자 (예: "identity_financial")
# -필드: name          = 사람이 읽는 이름 (예: "신원+금융 결합")
# -필드: all_of        = 모두 존재해야 하는 ko-pii 라벨들(튜플, 없으면 빈 튜플)
# -필드: of            = min_types 판정 대상 라벨 그룹(튜플)
# -필드: min_types     = of 그룹에서 필요한 '서로 다른 라벨' 최소 종수(0이면 미사용)
# -필드: grade         = 조합 성립 시 부여할 상향 등급(기본 C)
# -필드: weight        = 신뢰도 가중("high"|"medium"|"low")
# -필드: seed_eligible = True 면 이 조합으로 확정된 문서를 전파 seed 로 승격 가능
#------------------------------------------------------------------
@dataclass(frozen=True)
class ComboRule:
    id: str
    name: str
    all_of: tuple = ()
    of: tuple = ()
    min_types: int = 0
    grade: str = "C"
    weight: str = "high"
    seed_eligible: bool = False


#------------------------------------------------------------------
# 민감정보군 규칙 1개(Signal F) — 법상 '민감정보'
#=> 개인정보보호법 제23조의 민감정보(건강·유전·성생활·사상/신념·정치·노조·범죄경력·
#   인종 등)는 별도 취급 대상이다. 일반 기밀사전(L2)과 섞지 않고 독립 신호로 두어
#   "왜 민감으로 봤는가(어느 범주)"를 감사에서 명확히 드러낸다.
#   [주의] 키워드 기반이라 오탐 가능(예: 일반 기사 속 '정치'). 보수적 max 융합·검토큐가
#   과분류를 사람 검토로 흡수하므로, 안전측(상향)으로 둔다.
#
# -필드: id            = 규칙 식별자 (예: "health")
# -필드: name          = 사람이 읽는 이름 (예: "건강정보")
# -필드: category      = 민감정보 범주(건강/유전/성생활/사상·정치/노조/범죄경력/인종)
# -필드: terms         = 탐지 단어 리스트
# -필드: exclude       = 제외 문구 리스트(오탐 방지). 예: terms=[전과], exclude=[산전과]
# -필드: min_count     = 이 규칙 단어들의 합계가 이 값 미만이면 히트로 치지 않는다(기본 1 = 종전 동작).
#                        규정·법령 설명의 단발 언급("진단서 제출", "성생활 등 민감정보")을 걸러낸다.
# -필드: grade         = 히트 시 부여 등급(법상 민감 → 기본 C)
# -필드: weight        = 신뢰도 가중("high"|"medium"|"low")
# -필드: seed_eligible = True 면 이 히트로 확정된 문서를 전파 seed 로 승격 가능
#------------------------------------------------------------------
@dataclass(frozen=True)
class SensitiveRule:
    id: str
    name: str
    category: str
    terms: tuple
    exclude: tuple = ()
    min_count: int = 1
    grade: str = "C"
    weight: str = "high"
    seed_eligible: bool = False


#------------------------------------------------------------------
# 스탬프 규칙 1개(Signal E) — 보안분류 표식
#=> "문서에 찍힌 작성자의 분류 의도"를 잡는 규칙. 키워드(L2)와 달리, 본문 단발
#   언급의 오탐을 막으려 '머리 위치'나 '반복'일 때만 인정한다(always 규칙은 예외).
#
# -필드: id            = 규칙 식별자 (예: "stamp_confidential")
# -필드: name          = 사람이 읽는 이름 (예: "대외비/기밀 스탬프")
# -필드: terms         = 표식 문구 리스트(예: "대외비","CONFIDENTIAL")
# -필드: exclude       = 제외 문구 리스트. 이 문구 안에 든 매치는 세지 않는다
#                        (예: terms=[기밀], exclude=[기밀성] — 정보보호 용어의 반복을 스탬프로 오인 방지)
# -필드: grade         = 스탬프로 인정될 때 부여할 등급(기본 C)
# -필드: weight        = 신뢰도 가중("high"|"medium"|"low")
# -필드: seed_eligible = True 면 이 스탬프로 확정된 문서를 전파 seed 로 승격 가능
# -필드: always        = True 면 위치/반복 게이트 없이 1건이라도 스탬프로 인정
#                        (거의 스탬프 전용 문구용. 예: "관계자 외 열람금지")
#------------------------------------------------------------------
@dataclass(frozen=True)
class StampRule:
    id: str
    name: str
    terms: tuple
    exclude: tuple = ()
    grade: str = "C"
    weight: str = "high"
    seed_eligible: bool = False
    always: bool = False


#------------------------------------------------------------------
# 규칙셋 전체
#=> 로드된 cso_rule.yaml 을 통째로 담는 컨테이너. 스캔 함수에 넘겨 쓴다.
#
# -필드: version       = 규칙셋 버전 문자열(감사용, 결과에 기록)
# -필드: defaults      = Defaults(기본 임계값 등)
# -필드: regex_rules    = RegexRule 리스트(L1)
# -필드: pii_combos     = ComboRule 리스트(L1-combo, PII 결합식별성)
# -필드: keyword_rules  = KeywordRule 리스트(L2)
# -필드: sensitive_rules = SensitiveRule 리스트(Signal F, 법상 민감정보군)
# -필드: stamp_rules    = StampRule 리스트(Signal E, 보안분류 스탬프)
# -필드: confidence     = weight→신뢰도 표({regex/keyword/sensitive/stamp/path:{high/med/low}, name:float})
#------------------------------------------------------------------
@dataclass(frozen=True)
class RuleSet:
    version: str
    defaults: Defaults
    regex_rules: list = field(default_factory=list)
    pii_combos: list = field(default_factory=list)
    keyword_rules: list = field(default_factory=list)
    sensitive_rules: list = field(default_factory=list)
    stamp_rules: list = field(default_factory=list)
    confidence: dict = field(default_factory=_default_confidence)


#------------------------------------------------------------------
# 규칙 히트 1건(근거)
#=> "어떤 규칙이 몇 건 걸렸고 그래서 어떤 등급인가"를 담는다. 원문 값은 없다.
#
# -필드: rule_id         = 걸린 규칙 id
# -필드: name            = 규칙 이름
# -필드: layer           = "L1"(정규식) | "L2"(키워드)
# -필드: count           = 인정된 매칭 건수(문맥·검증 통과분)
#
#   [2026-09-10 validated_count 제거] "체크섬까지 통과한 건수"를 따로 실었지만,
#   값이 언제나 L1=count · L2=0 둘 중 하나였다 — layer 만 보면 아는 값이다.
#   ko-pii 가 체크섬 탈락분을 빼고 건수를 주므로 '일부만 통과'가 나올 경로가
#   없었는데, 받는 쪽이 "validated < count 면 의심스러운 검출"로 읽으면 영영
#   오지 않을 조건을 기다리게 된다. 읽는 코드도 없어 칸째 뺐다.
# -필드: grade           = 이 규칙이 기여한 등급(상향 반영)
# -필드: seed_eligible   = 이 히트가 전파 seed 로 쓸 만큼 신뢰되는가
# -필드: confidence      = 이 히트의 신뢰도(0~1)
# -필드: terms           = 매칭된 (단어, 건수) 목록 — L2 키워드만 채운다.
#                          L1 정규식은 매칭값이 곧 원문(PII)이라 항상 비워 둔다.
# -필드: capped_from     = PII 상한(defaults.pii_grade_cap)으로 등급이 깎였을 때 원래 등급.
#                          깎이지 않았으면 None(결과에 칸 자체가 없다).
#------------------------------------------------------------------
@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    name: str
    layer: str
    count: int
    grade: str
    seed_eligible: bool
    confidence: float
    terms: tuple = ()
    capped_from: str = None


#------------------------------------------------------------------
# 규칙 스캔 결과 신호(Signal A)
#=> 문서 하나에 대한 규칙 스캔의 최종 산출. 등급 결정과 감사에 이 값을 쓴다.
#
# -필드: grade         = 규칙 기반 최종 등급(히트 없으면 None)
# -필드: confidence    = 최종 등급을 만든 히트들의 최고 신뢰도(없으면 0.0)
# -필드: seed_eligible = 이 문서를 전파 seed 로 승격해도 되는가
# -필드: hits          = RuleHit 리스트(근거 전체, 원문 없음)
#------------------------------------------------------------------
@dataclass(frozen=True)
class GradeSignal:
    grade: str
    confidence: float
    seed_eligible: bool
    hits: list = field(default_factory=list)

    #------------------------------------------------------------------
    # 결과를 직렬화용 dict 로
    #=> 분류 레코드(jsonl)의 "rule" 신호 칸에 넣기 좋은 순수 dict 로 바꾼다.
    #   원문 값이 없으므로 그대로 로그에 남겨도 안전하다.
    #
    # -in: 없음
    #
    # -out: dict = {grade, confidence, seed_eligible, hits:[{id,name,layer,count,...}]}
    # -out: error = 없음
    #------------------------------------------------------------------
    def as_dict(self):
        return {
            "grade": self.grade,
            "confidence": round(self.confidence, 3),
            "seed_eligible": self.seed_eligible,
            "hits": [_hit_dict(h) for h in self.hits],
        }


#------------------------------------------------------------------
# 규칙 히트 1건 → 결과 dict
#=> GradeSignal.as_dict 가 히트마다 부른다. PII 상한으로 등급이 깎인 히트에만
#   capped_from 칸을 더한다 — 상한을 안 쓰는 배포의 결과 파일은 예전과 글자까지 같다.
#
# -in: h = RuleHit
#
# -out: dict = {id, name, layer, count, grade, terms[, capped_from]}
# -out: error = 없음
#------------------------------------------------------------------
def _hit_dict(h):
    d = {
        "id": h.rule_id,
        "name": h.name,
        "layer": h.layer,
        "count": h.count,
        "grade": h.grade,
        # 어떤 단어가 걸렸는지(키워드만). PII 정규식은 항상 빈 목록.
        "terms": [{"term": t, "count": c} for t, c in h.terms],
    }
    # 깎인 경우에만 원래 등급을 남긴다 — "왜 C 가 아니라 S 인가"를 감사에서 보이게.
    if h.capped_from:
        d["capped_from"] = h.capped_from
    return d


#------------------------------------------------------------------
# PII 근거 등급에 상한 씌우기
#=> defaults.pii_grade_cap 이 있으면, PII 히트(L1)와 PII 결합 히트(COMBO)의 등급을
#   그 상한 아래로 낮춘다. 키워드(L2) 히트는 건드리지 않는다.
#   예: 상한 S 일 때 주민번호 12건(bulk C) → S, capped_from="C" 로 기록.
#    1) 상한이 없으면 받은 목록을 그대로 돌려준다(종전 동작)
#    2) 히트 등급이 상한보다 높으면 등급만 바꾼 새 RuleHit 를 만든다(원래 등급 기록)
#
# -in: hits = RuleHit 목록(L1·COMBO)
# -in: cap  = 상한 등급("C"/"S"/"O") 또는 None
#
# -out: list[RuleHit] = 상한을 적용한 히트 목록(순서 그대로)
# -out: error = 상한이 C/S/O 가 아니면 UnknownGradeError (보통은 로드 때 V14 로 먼저 막힌다)
#------------------------------------------------------------------
def _cap_pii_hits(hits, cap):
    if not cap:
        return list(hits)
    # 검증을 끄고 읽은 규칙셋(validate=False)에서도 오타 상한을 조용히 넘기지 않는다.
    if cap not in _GRADE_RANK:
        raise UnknownGradeError(cap, "defaults.pii_grade_cap")
    cap_rank = _GRADE_RANK[cap]
    out = []
    for h in hits:
        # 상한보다 낮은 등급(예: 상한 S 에 O 히트)은 그대로 둔다 — 상한까지 끌어올리지 않는다.
        if _GRADE_RANK.get(h.grade, -1) > cap_rank:
            h = _replace(h, grade=cap, capped_from=h.grade)
        out.append(h)
    return out


#------------------------------------------------------------------
# 건수 → 상향 등급 반영
#=> 히트 건수가 bulk_threshold 이상이면 bulk_grade(상향)를, 아니면 base_grade 를 준다.
#   bulk_grade 가 없는 규칙(대부분 키워드)은 항상 base_grade.
#
# -in: rule     = RegexRule 또는 KeywordRule
# -in: count    = 인정된 히트 건수
# -in: defaults = Defaults(규칙에 임계값이 없을 때 대체값)
#
# -out: grade = 최종 반영 등급 문자열
# -out: error = 없음
#------------------------------------------------------------------
def _escalate_grade(rule, count, defaults):
    if not rule.bulk_grade:
        return rule.base_grade
    # 규칙 전용 임계값이 없으면 전역 기본값을 쓴다.
    thr = rule.bulk_threshold if rule.bulk_threshold else defaults.bulk_threshold
    return rule.bulk_grade if count >= thr else rule.base_grade


#------------------------------------------------------------------
# 청크 창 경계 생성기
#=> [0, covered) 구간을 size 크기 창으로 (size-overlap) 만큼 전진하며 덮는다.
#   인접 창이 overlap 만큼 겹쳐, 경계에 걸친 PII 가 어느 한 창에는 '온전히' 들어온다.
#
# -in: covered = 덮을 총 길이(문자수)
# -in: size    = 창 크기(_KOPII_MAX_CHARS)
# -in: overlap = 인접 창 겹침(_KOPII_OVERLAP, size 미만 보장됨)
#
# -out: (start, end) 쌍을 순차 yield (end 는 covered 로 클리핑)
# -out: error = 없음
#------------------------------------------------------------------
def _chunk_windows(covered, size, overlap):
    step = max(1, size - overlap)
    start = 0
    while start < covered:
        end = min(start + size, covered)
        yield start, end
        if end >= covered:      # 마지막 창이 covered 에 닿으면 종료(불필요한 꼬리 창 방지)
            break
        start += step


#------------------------------------------------------------------
# 절대 오프셋 구간 병합 건수
#=> 겹치는 창들에서 같은 실제 PII 가 두 번 잡힐 수 있다. 절대 오프셋 (start,end)
#   구간을 정렬해 '겹치면 같은 검출'로 병합하고, 병합된 구간의 개수를 센다.
#   (같은 라벨끼리만 넘겨야 한다. 서로 다른 실제 PII 는 span 이 안 겹쳐 따로 세어진다.)
#   경계에서 문맥 차로 span 이 조금 달라져도(정확히 같지 않아도) '겹치면 1건'이라
#   이중계수가 안 생긴다.
#
# -in: spans = [(start,end), ...]  동일 라벨의 절대 오프셋 구간들
#
# -out: int = 병합 후 구간(=실제 PII) 개수
# -out: error = 없음
#------------------------------------------------------------------
def _count_merged_spans(spans):
    if not spans:
        return 0
    spans = sorted(spans)
    count = 1
    cur_end = spans[0][1]
    for start, end in spans[1:]:
        if start < cur_end:            # 앞 구간과 겹침 = 같은 실제 PII(경계 중복) → 병합
            cur_end = max(cur_end, end)
        else:
            count += 1
            cur_end = end
    return count


#------------------------------------------------------------------
# ko-pii 라벨별 검출 건수 집계
#=> 활성 라벨을 검출해 라벨별 건수를 센다. 값(원문)은 세는 즉시 버려 프라이버시
#   불변식을 지킨다. 큰 문서에서도 커버리지를 잃지 않도록 검출기를 선형/초선형으로
#   나눠 처리한다(설계: A안 + 초선형 청킹, 상세는 _KOPII_MAX_CHARS 주석 참조).
#    1) ko-pii 미설치면 설치 안내와 함께 오류(A안: 폴백 없음)
#    2) 소형 문서(≤ 청크 크기)는 기존과 동일한 단일 패스(교차라벨 겹침해소 보존)
#    3) 큰 문서: 선형 검출기는 전량 1회 스캔, 초선형 검출기는 앞 _KOPII_MAX_TOTAL 까지
#       겹치는 청크로 나눠 스캔 후 절대 오프셋 병합으로 경계 중복 제거
#    4) normalize=True 로 전각/homoglyph 우회까지 방어(창마다 개별 정규화)
#   [참고] 선형/초선형을 각각 별도 스캔하므로, 초선형↔선형 라벨이 '겹치는' 드문
#   경우엔 detect_all 의 교차라벨 겹침해소가 적용되지 않아 양쪽이 각각 세어질 수 있다
#   (건수 기반 등급엔 영향 미미). 소형 문서(대다수)는 단일 패스라 완전히 동일하다.
#
#   [2026-09-11 하나로 합침] 예전에는 건수(_kopii_counts)와 원문 값(collect_pii)이
#   **같은 스캔을 따로 구현**하고 있었다. 겹침 해소가 건수 쪽에만 들어가 있어,
#   1만 자가 넘는 문서에서 레코드가 "전화 7건"이라고 적어 놓고 전화번호 값을
#   9개 싣는 일이 벌어졌다(같은 자리를 사업자등록번호와 전화번호가 함께 물었다).
#   이제 둘 다 이 함수가 낸 **한 목록**에서 나온다 — 세는 것과 싣는 것이
#   어긋날 자리 자체가 없다.
#
# -in: text   = 스캔 대상 텍스트
# -in: labels = 볼 ko-pii 라벨 목록(활성 유형들)
#
# -out: list = 겹침까지 해소된 ko-pii Match 목록(오프셋은 원문 text 기준 절대값)
# -out: error = ko-pii 미설치 시 RuntimeError(설치 안내)
#------------------------------------------------------------------
#------------------------------------------------------------------
# 초선형 검출기가 이 문서를 얼마나 봤나 (스캔 상한 계측)
#=> 전화·주소·계좌(_KOPII_SUPERLINEAR)는 비용이 초선형이라 앞
#   _KOPII_MAX_TOTAL 자까지만 훑는다. 나머지 검출기는 전량을 본다.
#   문제는 이 절단이 **아무 표식도 남기지 않는다**는 것이었다 — 뒤쪽에 있는
#   주소록·연락처가 통째로 안 잡혀도 결과만 보면 "없더라"와 구분되지 않는다.
#   [실측] D:\분류함 비교에서 1MB 초과 문서 36건이 이 상한에 걸려, 상한이 없는
#   Rust 판보다 전화·주소·계좌를 477건 적게 잡았다(2026-09-04).
#   본문 절단(G3, text_truncated)과는 다른 층이다 — G3 는 본문 자체를 자르고
#   이건 자르지 않은 본문의 뒷부분을 '이 세 검출기만' 못 보는 것이라, G3 에 안
#   걸린 문서도 여기 걸릴 수 있다(현재 기본값 G3 200만 자 > 이 상한 100만 자).
#
# -in: text   = 스캔 대상 텍스트
# -in: labels = 이번 실행에서 켠 ko-pii 라벨들
#
# -out: (capped, covered, total) =
#         capped  = 초선형 검출기가 뒷부분을 못 봤으면 True
#         covered = 그 검출기들이 실제로 훑은 글자 수
#         total   = 전체 글자 수
# -out: error = 없음 (초선형 라벨을 안 켰으면 언제나 (False, total, total))
#------------------------------------------------------------------
def superlinear_coverage(text, labels):
    total = len(text or "")
    # 초선형 라벨을 하나도 안 켰으면 이 상한은 이번 실행과 무관하다.
    if not (set(labels or ()) & _KOPII_SUPERLINEAR):
        return False, total, total
    covered = min(total, _KOPII_MAX_TOTAL)
    return covered < total, covered, total


def _kopii_scan(text, labels):
    if not _HAVE_KOPII:
        raise RuntimeError(
            "PII 검출 엔진 ko-pii 가 설치되어 있지 않습니다. "
            "`pip install ko-pii==1.15.2` 후 다시 실행하세요. "
            f"(원인: {_KOPII_IMPORT_ERR})")
    if not text or not labels:
        return []
    labels = set(labels)

    # (소형) 청크 크기 이하 문서는 예전과 100% 동일한 단일 패스로 처리한다.
    # 모든 라벨을 한 스캔에서 검출해 교차라벨 겹침해소(detect_all 동작)까지 그대로 보존.
    if len(text) <= _KOPII_MAX_CHARS:
        return list(_detect_subset(text, labels, normalize=True))

    # 두 갈래(선형·초선형)의 검출을 '한 자루'에 모은 뒤 마지막에 한 번만 겹침을
    # 해소한다. 예전에는 갈래마다 따로 세어, 갈래가 다른 두 라벨이 같은 자리를
    # 물면 양쪽이 다 세어졌다 — 실측(45K 주소록)에서 "0220076422" 한 자리를
    # 사업자등록번호와 전화번호로 '둘 다' 세어 전화가 1건 많았다. ko-pii
    # detect_all 은 그 자리를 사업자등록번호 하나로만 센다.
    raw = []

    # (1) 선형 검출기: 전체 텍스트를 한 번에 스캔(전량 커버, 저비용, 상한 없음).
    linear = labels - _KOPII_SUPERLINEAR
    if linear:
        raw.extend(_detect_subset(text, linear, normalize=True))

    # (2) 초선형 검출기: 앞 _KOPII_MAX_TOTAL 까지 겹치는 청크로 나눠 스캔한다.
    #     (_detect_subset 은 창 문자열 기준 오프셋을 돌려주므로 base 를 더해 절대화.)
    #     창 경계에 걸친 중복은 아래 겹침 해소가 함께 걷어낸다 — 같은 자리를 두
    #     창이 잡으면 서로 겹치므로 하나만 살아남고, 경계에 걸려 한쪽이 짧게
    #     잘린 경우엔 '더 긴 쪽'이 이긴다(resolve_overlaps 의 길이 우선 규칙).
    superlinear = labels & _KOPII_SUPERLINEAR
    if superlinear:
        covered = min(len(text), _KOPII_MAX_TOTAL)
        for base, end in _chunk_windows(covered, _KOPII_MAX_CHARS, _KOPII_OVERLAP):
            for m in _detect_subset(text[base:end], superlinear, normalize=True):
                raw.append(_replace(m, start=base + m.start, end=base + m.end))

    # (3) 교차라벨 겹침 해소 — detect_all 과 같은 우선순위(위험도→확신도→길이→시작).
    return list(_kopii_resolve_overlaps(raw))


#------------------------------------------------------------------
# 라벨별 건수
#=> _kopii_scan 이 낸 목록을 세기만 한다. 세는 규칙이 따로 있지 않다.
#
# -in: text   = 스캔 대상 텍스트
# -in: labels = 셀 ko-pii 라벨 목록(활성 유형들)
#
# -out: dict = {라벨: 건수}  (검출 없으면 빈 dict)
# -out: error = ko-pii 미설치 시 RuntimeError(_kopii_scan 이 낸다)
#------------------------------------------------------------------
def _kopii_counts(text, labels):
    counts = {}
    for m in _kopii_scan(text, labels):
        counts[m.label] = counts.get(m.label, 0) + 1
    return counts


#------------------------------------------------------------------
# PII(L1) 스캔 — ko-pii 건수 → RuleHit 목록
#=> 활성 PII 유형들을 ko-pii 로 한 번에 검출하고, 유형별로 등급 정책을 적용해
#   RuleHit 를 만든다. ko-pii 가 이미 체크섬·문맥으로 검증한 검출이므로 별도
#   검증 단계는 없다 — 여기 오는 건수는 전부 검증을 통과한 것이다.
#
# -in: text        = 스캔 대상 텍스트
# -in: regex_rules = PII 유형 규칙(RegexRule) 목록
# -in: defaults    = Defaults(bulk 임계 등)
# -in: conf_map    = weight→신뢰도 표(없으면 기본)
#
# -out: (list[RuleHit], dict) = (검출된 유형별 히트, {라벨:건수} 맵)
#       건수 맵은 결합식별성(_combo_hits) 판정에 재사용한다(재스캔 방지).
# -out: error = ko-pii 미설치 시 RuntimeError 전파
#------------------------------------------------------------------
def _scan_pii(text, regex_rules, defaults, conf_map=None):
    conf_map = conf_map if conf_map is not None else _REGEX_CONF
    if not regex_rules:
        return [], {}
    # 라벨→규칙 매핑(같은 라벨을 두 규칙이 쓰면 첫 규칙만; 설정 실수 방어).
    by_label = {}
    for rule in regex_rules:
        by_label.setdefault(rule.label, rule)

    counts = _kopii_counts(text, by_label.keys())
    hits = []
    # [히트 차례] 규칙셋(cso_rule.yaml)에 적힌 차례를 따른다.
    #   예전에는 counts 를 그대로 돌아 ko-pii 가 '검출한 차례'(대략 문서에 나온
    #   차례)로 담겼다. 그건 우리가 정한 규칙이 아니라 라이브러리 내부 사정이라,
    #   ko-pii 를 판올림하면 판정이 그대로여도 모든 레코드의 히트 차례가 조용히
    #   바뀐다. 또 Rust 판은 규칙 차례로 담고 있어 두 판의 결과 파일을 그대로
    #   견줄 수 없었다(2026-09-10 실측: 11개 문서 중 6개에서 차례가 어긋났다).
    #   규칙셋 차례는 정책을 쓴 사람이 정한 것이고 두 판이 같은 파일을 읽으므로,
    #   같은 차례가 '우연'이 아니라 '구조'로 보장된다.
    for rule in regex_rules:
        count = counts.get(rule.label, 0)
        if count <= 0 or by_label.get(rule.label) is not rule:
            continue
        grade = _escalate_grade(rule, count, defaults)
        hits.append(RuleHit(
            rule_id=rule.id, name=rule.name, layer="L1",
            count=count,
            grade=grade, seed_eligible=rule.seed_eligible,
            confidence=conf_map.get(rule.weight, 0.6),
        ))
    return hits, counts


#------------------------------------------------------------------
# 결합식별성 평가(L1-combo) — PII 조합 → 상향 히트
#=> _scan_pii 가 만든 {라벨:건수} 맵을 보고, pii_combos 규칙의 조합 조건이 성립하면
#   상향 등급 RuleHit 를 만든다. 개별 PII 히트와 '함께' rule 신호로 들어가 max 융합되어
#   '상향 전용'으로 기여한다(개별 등급을 내리지 않는다).
#    1) 건수>0 인 라벨 집합(present)을 만든다
#    2) 규칙별로 all_of(모두 존재)·of/min_types(그룹에서 N종 이상)를 판정(둘 다면 AND)
#    3) 성립하면 어떤 유형이 결합됐는지(라벨+건수)를 terms 로 남긴 COMBO 히트 생성
#   [프라이버시] terms 에 담는 건 ko-pii '라벨'과 '건수'뿐 — 원문 PII 값이 아니라 안전.
#
# -in: counts      = {ko-pii 라벨: 건수} (_scan_pii 반환값)
# -in: combo_rules = ComboRule 목록(ruleset.pii_combos)
# -in: conf_map    = weight→신뢰도 표(없으면 정규식 기본)
#
# -out: list[RuleHit] = 성립한 조합별 상향 히트(layer="COMBO", 없으면 빈 리스트)
# -out: error = 없음
#------------------------------------------------------------------
def _combo_hits(counts, combo_rules, conf_map=None):
    conf_map = conf_map if conf_map is not None else _REGEX_CONF
    if not counts or not combo_rules:
        return []
    # 한 번이라도 검출된(건수>0) 라벨만 '문서에 존재'로 본다.
    present = {lbl for lbl, c in (counts or {}).items() if c > 0}
    hits = []
    for rule in combo_rules:
        has_cond = False      # 이 규칙이 조건을 하나라도 정의했는가(무조건 규칙 오발동 방지)
        conds_met = True      # 정의된 조건을 모두 만족하는가(AND)
        matched = {}          # 성립에 기여한 (라벨→건수)

        if rule.all_of:
            has_cond = True
            if all(lbl in present for lbl in rule.all_of):
                for lbl in rule.all_of:
                    matched[lbl] = counts[lbl]
            else:
                conds_met = False

        if rule.of and rule.min_types:
            has_cond = True
            got = [lbl for lbl in rule.of if lbl in present]
            if len(got) >= rule.min_types:
                for lbl in got:
                    matched[lbl] = counts[lbl]
            else:
                conds_met = False

        # 조건 미정의(오설정)거나 하나라도 불만족이면 이 조합은 성립 안 함.
        if not has_cond or not conds_met:
            continue

        # 어떤 PII 유형이 결합됐는지 라벨 오름차순으로 근거를 남긴다(라벨+건수, 원문 아님).
        comps = tuple(sorted(matched.items()))
        hits.append(RuleHit(
            rule_id=rule.id, name=rule.name, layer="COMBO",
            count=len(comps),
            grade=rule.grade, seed_eligible=rule.seed_eligible,
            confidence=conf_map.get(rule.weight, 0.8),
            terms=comps,
        ))
    return hits


#------------------------------------------------------------------
# 제외어(exclude) 구간 찾기
#=> 규칙의 제외 문구들이 텍스트 어디에 있는지 (start,end) 구간 목록으로 모은다.
#   한국어는 단어 경계(띄어쓰기)가 없어 부분문자열 오탐이 잦다 — 예: '전과'가
#   '산전과 산후'(산전+과)에 걸린다. 그런 오탐을 규칙별 제외 문구로 걸러내기 위한 준비.
#
# -in: hay     = 검사 대상(대소문자 정책 적용 후) 문자열
# -in: excludes = 제외 문구 목록(rule.exclude)
# -in: ci      = case_insensitive 여부(제외 문구도 같은 정책으로 소문자화)
#
# -out: list[(start,end)] = 제외 문구들이 걸린 구간(정렬 안 함, 빈 리스트 가능)
# -out: error = 없음
#------------------------------------------------------------------
def _exclude_spans(hay, excludes, ci):
    spans = []
    for p in (excludes or ()):
        if not p:
            continue
        needle = p.lower() if ci else p
        i = hay.find(needle)
        n = len(needle)
        while i >= 0:
            spans.append((i, i + n))
            i = hay.find(needle, i + n)   # 비중첩
    return spans


#------------------------------------------------------------------
# 제외 구간 밖 등장 횟수
#=> needle 의 비중첩 등장 횟수를 세되, 제외 구간(ex_spans)에 '완전히 포함'되는
#   매치는 뺀다. 제외 구간이 없으면 str.count 와 동일하다(하위호환).
#
# -in: hay      = 검사 대상 문자열
# -in: needle   = 찾을 단어(정책 적용 후)
# -in: ex_spans = _exclude_spans() 결과
#
# -out: int = 제외 후 인정 건수
# -out: error = 없음
#------------------------------------------------------------------
def _count_outside(hay, needle, ex_spans):
    if not needle:
        return 0
    if not ex_spans:
        return hay.count(needle)
    total = 0
    n = len(needle)
    i = hay.find(needle)
    while i >= 0:
        j = i + n
        # 이 매치가 어떤 제외 구간 안에 통째로 들어가면(예: '전과' ⊂ '산전과') 세지 않는다.
        if not any(s <= i and j <= e for s, e in ex_spans):
            total += 1
        i = hay.find(needle, j)   # 비중첩(str.count 과 동일 진행)
    return total


#------------------------------------------------------------------
# 제외 구간 밖 첫 등장 위치
#=> needle 이 제외 구간(ex_spans)에 통째로 들지 않고 처음 나오는 위치를 찾는다.
#   스탬프 '머리' 판정용 — 예: 머리에 '기밀성'만 있고 진짜 '기밀' 표식이 없으면 머리로 치지 않는다.
#    1) 제외 구간이 없으면 str.find 와 같다(하위호환)
#    2) 있으면 비중첩으로 앞에서부터 훑어 제외 구간 밖의 첫 매치를 돌려준다
#
# -in: hay      = 검사 대상 문자열
# -in: needle   = 찾을 단어(정책 적용 후)
# -in: ex_spans = _exclude_spans() 결과
#
# -out: int = 첫 인정 위치(없으면 -1)
# -out: error = 없음
#------------------------------------------------------------------
def _find_outside(hay, needle, ex_spans):
    if not needle:
        return -1
    if not ex_spans:
        return hay.find(needle)
    n = len(needle)
    i = hay.find(needle)
    while i >= 0:
        # 제외 구간 안에 통째로 든 매치(예: '기밀' ⊂ '기밀성')는 건너뛴다.
        if not any(s <= i and i + n <= e for s, e in ex_spans):
            return i
        i = hay.find(needle, i + n)
    return -1


#------------------------------------------------------------------
# 키워드 규칙 1개 스캔
#=> 규칙의 단어들이 텍스트에 몇 번 나오는지 세어 RuleHit 를 만든다. 하나도 없으면 None.
#   대소문자 무시(defaults.case_insensitive)면 양쪽을 소문자로 맞춰 센다.
#   제외어(rule.exclude)가 있으면 그 문구 안에 든 매치는 오탐으로 보고 세지 않는다.
#
# -in: text     = 스캔 대상 텍스트
# -in: rule     = KeywordRule
# -in: defaults = Defaults
#
# -out: RuleHit | None
# -out: error = 없음
#------------------------------------------------------------------
def _scan_keyword(text, rule, defaults, conf_map=None):
    conf_map = conf_map if conf_map is not None else _KEYWORD_CONF
    haystack = text.lower() if defaults.case_insensitive else text
    # 제외어(exclude) 구간을 미리 찾아 둔다 — 한국어는 띄어쓰기가 없어 부분문자열 오탐이
    # 잦다(예: '전과'가 '산전과 산후'에 걸림). 제외 문구 안에 든 매치는 세지 않는다.
    ex_spans = _exclude_spans(haystack, getattr(rule, "exclude", ()), defaults.case_insensitive)
    # 단어별 최소 횟수표(없으면 전부 1)
    min_of = dict(getattr(rule, "term_min_count", ()) or ())
    total = 0
    # 단어별 건수를 따로 모아, 나중에 "어떤 단어가 몇 번 걸렸는지" 근거로 남긴다.
    per_term = []
    for term in rule.terms:
        if not term:
            continue
        needle = term.lower() if defaults.case_insensitive else term
        # 비중첩 등장 횟수(str.count 동등) 중, 제외 구간에 든 매치는 뺀다.
        c = _count_outside(haystack, needle, ex_spans)
        # 그 단어의 최소 횟수 미달(규정 말미 경고 같은 단발 언급)은 센 것으로 치지 않는다.
        if c and c >= min_of.get(term, 1):
            per_term.append((term, c))
            total += c

    if total == 0:
        return None

    grade = _escalate_grade(rule, total, defaults)
    conf = conf_map.get(rule.weight, 0.6)
    return RuleHit(
        rule_id=rule.id, name=rule.name, layer="L2",
        count=total,
        grade=grade, seed_eligible=rule.seed_eligible, confidence=conf,
        terms=tuple(per_term),
    )


#------------------------------------------------------------------
# 텍스트 규칙 스캔(핵심 진입점)
#=> 정제 텍스트에 L1(정규식)·L2(키워드) 규칙을 모두 걸어 GradeSignal 을 만든다.
#    1) 모든 규칙을 돌려 RuleHit 를 모은다
#    2) 히트가 없으면 grade=None 신호 반환(=규칙상 특이사항 없음)
#    3) 히트가 있으면 max_grade 로 최종 등급, 그 등급을 만든 히트들의
#       최고 신뢰도·seed 여부를 뽑아 담는다
#
# -in: text    = MpowerClassify 가 추출·정제한 문서 텍스트
# -in: ruleset = load_rules() 로 만든 RuleSet
#
# -out: GradeSignal = 규칙 기반 등급 신호(근거 hits 포함, 원문 없음)
# -out: error = 없음 (빈 텍스트/무매칭은 grade=None 으로 정상 반환)
#------------------------------------------------------------------
def scan_text(text, ruleset):
    text = text or ""
    conf = getattr(ruleset, "confidence", None) or _default_confidence()
    hits = []

    # L1: ko-pii 로 PII 유형을 한 번에 검출(유형별 RuleHit) + 건수 맵 회수.
    pii_hits, pii_counts = _scan_pii(text, ruleset.regex_rules, ruleset.defaults, conf.get("regex"))

    # L1-combo: 개별 PII 는 낮아도 여러 유형이 한 문서에 모이면(결합용이성) 재식별↑ → 상향.
    pii_hits += _combo_hits(pii_counts, getattr(ruleset, "pii_combos", ()), conf.get("regex"))
    # PII 근거는 정책 상한(pii_grade_cap)까지만 올린다 — 없으면 종전 그대로.
    hits.extend(_cap_pii_hits(pii_hits, getattr(ruleset.defaults, "pii_grade_cap", None)))
    for rule in ruleset.keyword_rules:
        h = _scan_keyword(text, rule, ruleset.defaults, conf.get("keyword"))
        if h:
            hits.append(h)

    if not hits:
        # 규칙상 아무것도 안 걸림 → 등급 미정(상위 융합에서 경로/전파가 결정).
        return GradeSignal(grade=None, confidence=0.0, seed_eligible=False, hits=[])

    final = max_grade(h.grade for h in hits)

    # 최종 등급을 실제로 만든(=같은 등급인) 히트만 신뢰도·seed 판단에 쓴다.
    deciding = [h for h in hits if h.grade == final]
    confidence = max(h.confidence for h in deciding)
    seed_eligible = any(h.seed_eligible for h in deciding)
    return GradeSignal(
        grade=final, confidence=confidence,
        seed_eligible=seed_eligible, hits=hits,
    )


#------------------------------------------------------------------
# 스탬프 스캔 결과 신호(Signal E)
#=> 문서에 찍힌 보안분류 표식의 최종 산출. 원문 PII 가 아니라 '정책 표식 문구'라
#   근거(term)를 그대로 남겨도 안전하다.
#
# -필드: grade         = 스탬프 기반 등급(표식 없으면 None)
# -필드: confidence    = 최종 등급을 만든 스탬프들의 최고 신뢰도(없으면 0.0)
# -필드: seed_eligible = 이 문서를 전파 seed 로 승격해도 되는가
# -필드: hits          = [{id,name,grade,mode,count,term,confidence,seed_eligible}] 근거
#                        (mode: always|head|repeat|head+repeat — 왜 스탬프로 인정됐는지)
#------------------------------------------------------------------
@dataclass(frozen=True)
class StampSignal:
    grade: str
    confidence: float
    seed_eligible: bool
    hits: list = field(default_factory=list)

    #------------------------------------------------------------------
    # 직렬화용 dict
    #=> 분류 레코드의 signals.stamp 칸에 넣을 순수 dict.
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
            "hits": [dict(h) for h in self.hits],
        }


#------------------------------------------------------------------
# 스탬프 스캔(Signal E) — "문서에 찍힌 분류 표식"으로 등급
# => 문서 본문에서 보안분류 스탬프(대외비/기밀/내부용 등)를 찾는다. 키워드(L2)와의
#   결정적 차이는 '스탬프답게 찍혔는가'를 따진다는 점이다 → 본문 한복판의 단발 언급
#   ("이 문서는 대외비가 아님")은 스탬프로 치지 않아 오탐이 낮다.
#    1) 각 규칙의 표식 문구를 (대소문자 정책대로) 텍스트에서 센다
#    2) 인정 조건 판정:
#         · always 규칙  → 1건이라도 인정(거의 스탬프 전용 문구)
#         · head        → 첫 등장이 문서 머리(앞 _STAMP_HEAD_CHARS 자) 안
#         · repeat      → _STAMP_REPEAT_MIN 회 이상 반복(페이지 워터마크 정황)
#       셋 중 아무 것도 아니면(=본문 단발 언급) 스탬프로 인정하지 않는다.
#    3) 인정된 스탬프들의 등급을 max_grade 로 합쳐 최종 등급 산정
#   [프라이버시] 표식 문구는 정책 키워드라 원문 PII 가 아니다 → term 을 근거로 남겨도 안전.
#   [융합] 결과는 build_record 에서 다른 신호와 함께 max 융합되어 '상향 전용'으로 기여한다.
#
# -in: text    = MpowerClassify 가 추출·정제한 문서 텍스트
# -in: ruleset = load_rules() 로 만든 RuleSet(stamp_rules 사용)
#
# -out: StampSignal = 스탬프 기반 등급 신호(근거 hits 포함)
# -out: error = 없음 (빈 텍스트/규칙없음/무매칭은 grade=None 으로 정상 반환)
#------------------------------------------------------------------
def scan_stamp(text, ruleset):
    rules = getattr(ruleset, "stamp_rules", None)
    if not text or not rules:
        return StampSignal(grade=None, confidence=0.0, seed_eligible=False, hits=[])

    ci = ruleset.defaults.case_insensitive
    # 대소문자 무시 정책이면 양쪽을 소문자로 맞춰(count/find) 찾는다.
    hay = text.lower() if ci else text
    conf_map = (getattr(ruleset, "confidence", None) or {}).get("stamp") or _STAMP_CONF

    hits = []
    for rule in rules:
        # 제외어 구간 — 예: '기밀성'(정보보호 용어)이 반복돼 '기밀' 스탬프로 오인되던 것을 막는다.
        ex_spans = _exclude_spans(hay, getattr(rule, "exclude", ()), ci)
        total = 0
        head = False
        head_term = None      # 머리에서 걸린 문구(근거 표기 우선)
        first_term = None     # 아무거나 처음 걸린 문구(머리 없을 때 근거용)
        for term in rule.terms:
            if not term:
                continue
            needle = term.lower() if ci else term
            # 제외어 안의 매치는 뺀다(제외어가 없으면 종전 str.count 와 같다).
            c = _count_outside(hay, needle, ex_spans)
            if not c:
                continue
            total += c
            if first_term is None:
                first_term = term
            # 첫 등장이 문서 '머리' 범위 안이면 head 로 인정(제목/표지 스탬프 정황).
            #   위치도 제외어 밖에서 처음 나온 곳으로 본다.
            if not head and 0 <= _find_outside(hay, needle, ex_spans) < _STAMP_HEAD_CHARS:
                head, head_term = True, term

        if total == 0:
            continue

        repeat = total >= _STAMP_REPEAT_MIN
        # 인정 사유(mode) 결정 — 하나도 해당 없으면 본문 단발 언급이라 스탬프 아님.
        if rule.always:
            mode = "always"
        elif head and repeat:
            mode = "head+repeat"
        elif head:
            mode = "head"
        elif repeat:
            mode = "repeat"
        else:
            continue

        hits.append({
            "id": rule.id, "name": rule.name, "grade": rule.grade,
            "mode": mode, "count": total, "term": head_term or first_term,
            "confidence": conf_map.get(rule.weight, 0.8),
            "seed_eligible": bool(rule.seed_eligible),
        })

    if not hits:
        return StampSignal(grade=None, confidence=0.0, seed_eligible=False, hits=[])

    final = max_grade(h["grade"] for h in hits)
    # 최종 등급을 실제로 만든(=같은 등급) 스탬프만 신뢰도·seed 판단에 쓴다.
    deciding = [h for h in hits if h["grade"] == final]
    confidence = max(h["confidence"] for h in deciding)
    seed_eligible = any(h["seed_eligible"] for h in deciding)
    return StampSignal(grade=final, confidence=confidence,
                       seed_eligible=seed_eligible, hits=hits)


#------------------------------------------------------------------
# 민감정보군 스캔 결과 신호(Signal F)
#=> 법상 민감정보 키워드 매칭의 최종 산출. 근거(범주·단어)는 정책 키워드라
#   원문 PII 가 아니므로 로그/결과에 남겨도 안전하다.
#
# -필드: grade         = 민감정보 기반 등급(매칭 없으면 None)
# -필드: confidence    = 최종 등급을 만든 히트들의 최고 신뢰도(없으면 0.0)
# -필드: seed_eligible = 이 문서를 전파 seed 로 승격해도 되는가
# -필드: hits          = [{id,name,category,grade,count,terms:[(단어,건수)],confidence,seed_eligible}]
#------------------------------------------------------------------
@dataclass(frozen=True)
class SensitiveSignal:
    grade: str
    confidence: float
    seed_eligible: bool
    hits: list = field(default_factory=list)

    #------------------------------------------------------------------
    # 직렬화용 dict
    #=> 분류 레코드의 signals.sensitive 칸에 넣을 순수 dict.
    #
    # -in: 없음
    # -out: dict = {grade, confidence, seed_eligible, hits:[{...,terms:[{term,count}]}]}
    # -out: error = 없음
    #------------------------------------------------------------------
    def as_dict(self):
        out = []
        for h in self.hits:
            d = dict(h)
            # terms 는 (단어,건수) 튜플 목록 → 직렬화 친화적 dict 목록으로.
            d["terms"] = [{"term": t, "count": c} for t, c in h.get("terms", ())]
            out.append(d)
        return {
            "grade": self.grade,
            "confidence": round(self.confidence, 3),
            "seed_eligible": self.seed_eligible,
            "hits": out,
        }


#------------------------------------------------------------------
# 민감정보군 스캔(Signal F) — 법상 '민감정보' 키워드
#=> 개인정보보호법상 민감정보(건강·유전·성생활·사상/신념·정치·노조·범죄경력·인종)를
#   범주별 키워드로 찾는다. 일반 기밀사전(L2, scan_text 내부)과 '분리된' 독립 신호라,
#   결과에서 "어느 민감범주 때문에 등급이 올랐는지"가 명확히 드러난다.
#    1) 각 규칙의 단어를 (대소문자 정책대로) 텍스트에서 센다
#    2) 하나라도 걸리면 그 규칙의 등급(기본 C)을 기여 → max_grade 로 최종 등급
#   [프라이버시] 근거로 남기는 건 정책 키워드(단어)와 범주·건수뿐 — 원문 PII 아님.
#   [융합] build_record 에서 다른 신호와 max 융합되어 '상향 전용'으로 기여한다.
#
# -in: text    = MpowerClassify 가 추출·정제한 문서 텍스트
# -in: ruleset = load_rules() 로 만든 RuleSet(sensitive_rules 사용)
#
# -out: SensitiveSignal = 민감정보 기반 등급 신호(근거 hits 포함)
# -out: error = 없음 (빈 텍스트/규칙없음/무매칭은 grade=None 으로 정상 반환)
#------------------------------------------------------------------
def scan_sensitive(text, ruleset):
    rules = getattr(ruleset, "sensitive_rules", None)
    if not text or not rules:
        return SensitiveSignal(grade=None, confidence=0.0, seed_eligible=False, hits=[])

    ci = ruleset.defaults.case_insensitive
    # 대소문자 무시 정책이면 양쪽을 소문자로 맞춰 센다.
    hay = text.lower() if ci else text
    conf_map = (getattr(ruleset, "confidence", None) or {}).get("sensitive") or _SENSITIVE_CONF

    hits = []
    for rule in rules:
        # 제외어 구간(오탐 방지) — 예: '전과'가 '산전과 산후'에 부분일치하는 것을 뺀다.
        ex_spans = _exclude_spans(hay, getattr(rule, "exclude", ()), ci)
        per_term = []
        total = 0
        for term in rule.terms:
            if not term:
                continue
            needle = term.lower() if ci else term
            c = _count_outside(hay, needle, ex_spans)
            if c:
                per_term.append((term, c))
                total += c
        # 최소 건수 미달(단발 언급)은 민감정보 '보유'로 보지 않는다.
        if total == 0 or total < getattr(rule, "min_count", 1):
            continue
        hits.append({
            "id": rule.id, "name": rule.name, "category": rule.category,
            "grade": rule.grade, "count": total, "terms": tuple(per_term),
            "confidence": conf_map.get(rule.weight, 0.8),
            "seed_eligible": bool(rule.seed_eligible),
        })

    if not hits:
        return SensitiveSignal(grade=None, confidence=0.0, seed_eligible=False, hits=[])

    final = max_grade(h["grade"] for h in hits)
    # 최종 등급을 실제로 만든(=같은 등급) 히트만 신뢰도·seed 판단에 쓴다.
    deciding = [h for h in hits if h["grade"] == final]
    confidence = max(h["confidence"] for h in deciding)
    seed_eligible = any(h["seed_eligible"] for h in deciding)
    return SensitiveSignal(grade=final, confidence=confidence,
                           seed_eligible=seed_eligible, hits=hits)


#------------------------------------------------------------------
# 검출된 PII '원문 값' 수집 (옵션 --with-pii 전용)
#=> [프라이버시 예외] 기본 동작은 원문 값을 절대 저장하지 않는다(건수만). 하지만
#   사용자가 --with-pii 로 '명시 요청'하면 이 함수로 실제 검출된 PII 값을 모아
#   결과(--out)에만 싣는다(로그엔 남기지 않음 — cli._loggable_record 가 가림).
#   [건수와 같은 목록에서 나온다] 건수(_kopii_counts)와 이 값 목록은 **같은
#   _kopii_scan 결과**를 쓴다. 예전에는 같은 스캔을 따로 구현해 두 곳이 갈렸다 —
#   겹침 해소가 건수 쪽에만 있어서, 1만 자가 넘는 문서에서 "전화 7건"이라고
#   적어 놓고 전화번호 값을 9개 싣고 있었다(2026-09-11 고침). 한 목록에서
#   파생시키면 어긋날 자리가 없다.
#    1) 규칙에 등록된 PII 라벨(regex_rules)만 검출 — 설정에 없는 유형은 안 남긴다
#    2) 검출 1건마다 {label, value, start, end} (offset 은 원문 text 기준 절대값)
#
# -in: text    = 스캔 대상 텍스트
# -in: ruleset = RuleSet(활성 PII 라벨 = regex_rules)
#
# -out: list[dict] = [{"label","value","start","end"}, ...] (없으면 빈 리스트)
# -out: error = ko-pii 미설치 시엔 이미 scan_text 가 먼저 RuntimeError → 여기선 [] 로 안전 처리
#------------------------------------------------------------------
def collect_pii(text, ruleset):
    if not _HAVE_KOPII or not text or not getattr(ruleset, "regex_rules", None):
        return []
    labels = {r.label for r in ruleset.regex_rules}
    if not labels:
        return []

    # 건수를 세는 그 목록에서 그대로 값을 꺼낸다 — 같은 스캔이므로
    # hits 의 건수와 여기 실리는 값 개수가 구조적으로 같아진다.
    return [{"label": m.label, "value": m.text, "start": m.start, "end": m.end}
            for m in _kopii_scan(text, labels)]


# 보안등급 규칙셋 파일 이름. 업무분류(doc_rule.yaml)와 짝을 맞춰 단수형으로
# 바꿨다(2026-09-08). 옛 이름도 당분간 받는다 — 이미 배포된 폴더에는 옛 이름
# 파일이 그대로 있고, 그것 때문에 "규칙셋을 찾을 수 없습니다"로 죽으면 안 된다.
RULES_NAME = "cso_rule.yaml"
RULES_NAME_OLD = "cso_rules.yaml"


#------------------------------------------------------------------
# 규칙셋 파일 한 개 고르기 — 새 이름 우선, 없으면 옛 이름
#=> 한 폴더 안에서 새 이름을 먼저 보고, 없을 때만 옛 이름을 쓴다. 옛 이름을
#   썼으면 화면에 한 줄 알린다 — 조용히 쓰면 "언제까지 이대로 두어도 되나"를
#   아무도 모르고, 어느 날 지원이 끊길 때 갑자기 멈춘다.
#
# -in: folder = 찾아볼 폴더
#
# -out: path = 실제로 있는 파일 경로. 둘 다 없으면 '새 이름' 경로
#              (호출부의 오류 메시지가 새 이름을 가리키게 하려는 것)
# -out: error = 없음
#------------------------------------------------------------------
def _pick_rules_file(folder):
    new = os.path.join(folder, RULES_NAME)
    if os.path.isfile(new):
        return new
    old = os.path.join(folder, RULES_NAME_OLD)
    if os.path.isfile(old):
        print(f"[MpowerClassify] {RULES_NAME_OLD} 은 옛 이름입니다 — "
              f"{RULES_NAME} 로 바꿔 두세요(지금은 그대로 씁니다).", file=sys.stderr)
        return old
    return new


#------------------------------------------------------------------
# 기본 규칙셋 파일 경로
#=> cso_rule.yaml 의 위치를 정한다. 규칙셋은 exe 에 내장하지 않고 '외장 파일'로
#   두어(exe 옆) 재빌드 없이 규칙만 교체·배포할 수 있게 한다.
#    1) 환경변수 CSOCLASSIFY_POLICY_DIR 이 있으면 그 폴더의 cso_rule.yaml
#    2) exe 로 실행 중이면 exe 실행 경로(exe 옆)의 cso_rule.yaml  ← 기본(외장)
#    3) 소스(개발) 실행이면 트리의 resources/policy/cso_rule.yaml
#
# -in: 없음
#
# -out: path = cso_rule.yaml 절대경로(존재 여부는 확인 안 함 — 없으면 load_rules 가
#              FileNotFoundError 로 그 경로를 알려 준다)
# -out: error = 없음
#------------------------------------------------------------------
def default_rules_path():
    env = os.environ.get("CSOCLASSIFY_POLICY_DIR")
    if env:
        return _pick_rules_file(env)
    # exe(PyInstaller) 로 얼린 실행: 내장 번들 대신 'exe 옆'의 외장 규칙셋을 쓴다.
    if getattr(sys, "frozen", False):
        return _pick_rules_file(exe_dir())
    # 소스(개발) 실행: 저장소 트리의 resources/policy/cso_rule.yaml.
    return _pick_rules_file(os.path.dirname(resource_path("policy", RULES_NAME)))


#------------------------------------------------------------------
# 기본 seed 저장소 경로
#=> 전파 비교 기준 seed 파일(class_seed.jsonl)의 위치를 규칙셋과 '같은 규약'으로 정한다.
#   기본 분류(--file/--dir)에서 이 파일이 있으면 자동 전파에 쓴다(--seeds 로 덮어쓸 수 있음).
#    1) 환경변수 CSOCLASSIFY_POLICY_DIR 이 있으면 그 폴더의 class_seed.jsonl
#    2) exe 로 실행 중이면 exe 실행 경로(exe 옆)의 class_seed.jsonl  ← 배포 기본
#    3) 소스(개발) 실행이면 트리의 resources/policy/class_seed.jsonl
#
# -in: 없음
#
# -out: path = class_seed.jsonl 절대경로(존재 여부는 확인 안 함 — 호출부가 isfile 로 판단)
# -out: error = 없음
#------------------------------------------------------------------
def default_seed_path():
    env = os.environ.get("CSOCLASSIFY_POLICY_DIR")
    if env:
        return os.path.join(env, "class_seed.jsonl")
    if getattr(sys, "frozen", False):
        return os.path.join(exe_dir(), "class_seed.jsonl")
    return resource_path("policy", "class_seed.jsonl")


# ────────────────────────────────────────────────────────────────────────
# 규칙셋 검증 (Phase 0 — fail-open 제거)
#   yaml 에 적힌 등급 값이 정말 정의된 등급인지, bulk 상향이 뒤집히진 않았는지를
#   '문서를 한 건도 스캔하기 전에' 확인한다. 여기서 걸러야 잘못 분류된 결과가
#   절반쯤 만들어지는 최악을 막을 수 있다.
# ────────────────────────────────────────────────────────────────────────

# 등급 필드가 어느 섹션의 어느 키에 있는지 정의.
#   (YAML 섹션 키, 단일등급 필드들, (base 필드, bulk 필드) 또는 None)
# bulk 쌍이 있는 섹션만 V6(상향 방향) 검사를 한다.
# [2026-09-08] paths 섹션이 없어지면서, 그 하나를 위해 있던 '등급 생략 허용'
# 자리(네 번째 칸)도 함께 뺐다 — 쓰는 데가 없는 장치를 남겨 두면 다음 사람이
# '이건 언제 쓰나'를 되짚어야 한다.
_GRADE_SECTIONS = (
    ("regex_pii",  (),         ("base_grade", "bulk_grade")),
    ("pii_combos", ("grade",), None),
    ("keywords",   (),         ("base_grade", "bulk_grade")),
    ("sensitive",  ("grade",), None),
    ("stamps",     ("grade",), None),
)

# 없어진 최상위 블록 → 사람이 읽을 사유. 조용히 무시하지 않으려고 표로 둔다.
_GONE_SECTIONS = {
    "paths": "paths 는 더 이상 쓰지 않는 블록입니다(2026-09-08 제거) — 이 신호는 동작하지 않으므로 규칙셋에서 지우세요",
}

# "키가 아예 없음"과 "키는 있는데 값이 비었음(null)"을 구분하기 위한 표식.
#   키가 없으면 로더가 기본값을 넣으므로 정상, null 이면 관리자의 실수다.
_ABSENT = object()


#------------------------------------------------------------------
# 오타 후보 제안
#=> 잘못 적힌 등급 값이 흔한 오타(소문자/앞뒤 공백)인지 보고, 맞을 법한 등급을 알려준다.
#   실제로 가장 많이 나오는 실수가 'c'(소문자)와 'C '(뒤 공백)라 이 둘만 잡아도 충분하다.
#
# -in: value = 문제가 된 원본 값(문자열이 아닐 수도 있다)
#
# -out: hint = 제안할 등급 문자열, 짚이는 게 없으면 None
# -out: error = 없음
#------------------------------------------------------------------
def _grade_hint(value):
    if not isinstance(value, str):
        return None
    # 앞뒤 공백을 없애고 대문자로 맞췄을 때 정의된 등급이 되면 그걸 제안한다.
    cand = value.strip().upper()
    return cand if cand in GRADES and cand != value else None


#------------------------------------------------------------------
# 위반 값 표시 문자열
#=> 보고문에 값을 어떻게 보여 줄지 한 곳에서 정한다. Rust 판(rules.rs 의 show)과
#   글자 하나까지 같아야 두 구현의 보고문이 일치한다.
#    · 키 자체가 없음 → "(없음)"   · null → "(null)"
#    · 문자열 → 'C ' 처럼 따옴표로 감싸 앞뒤 공백이 눈에 보이게
#    · 그 외(숫자·불리언·목록) → JSON 표기 그대로
#
# -in: value = 원본 값(_ABSENT 이면 키가 없었다는 뜻)
#
# -out: text = 보고문에 넣을 문자열
# -out: error = 없음
#------------------------------------------------------------------
def _show(value):
    if value is _ABSENT:
        return "(없음)"
    if value is None:
        return "(null)"
    if isinstance(value, str):
        return f"'{value}'"
    import json as _json
    return _json.dumps(value, ensure_ascii=False)


#------------------------------------------------------------------
# 값의 자료형 이름
#=> 오류 메시지에 쓸 자료형 이름을 YAML 쪽 용어로 통일한다. 파이썬 이름(int/dict)을
#   그대로 쓰면 Rust 판(number/mapping)과 메시지가 달라져 두 구현의 보고문이 갈린다.
#   관리자에게도 "int" 보다 "number" 가 YAML 문법과 가깝다.
#
# -in: value = 원본 값
#
# -out: name = "number" | "bool" | "list" | "mapping" | 파이썬 형이름(그 외)
# -out: error = 없음
#------------------------------------------------------------------
def _type_name(value):
    # bool 은 int 의 하위형이라 반드시 먼저 판별해야 한다.
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, (list, tuple)):
        return "list"
    if isinstance(value, dict):
        return "mapping"
    return type(value).__name__


#------------------------------------------------------------------
# 위반 1건 만들기
#=> 검증 결과를 화면 출력과 테스트가 함께 쓰는 dict 한 벌로 통일한다.
#
# -in: code    = 검증 항목 코드("V5"/"V6"/"V0")
# -in: section = YAML 섹션 키(예: "regex_pii")
# -in: rule_id = 규칙 id(없으면 "#3" 처럼 순번)
# -in: field   = 문제가 된 필드명(항목 자체 문제면 빈 문자열)
# -in: value   = 문제가 된 값
# -in: detail  = 사람이 읽을 설명 한 줄
#
# -out: dict = {code, section, rule_id, field, value, detail, hint}
# -out: error = 없음
#------------------------------------------------------------------
def _violation(code, section, rule_id, field, value, detail):
    return {
        "code": code, "section": section, "rule_id": rule_id,
        # value 는 원본(테스트·프로그램용), value_text 는 보고문용 표시 문자열.
        "field": field, "value": None if value is _ABSENT else value,
        "value_text": _show(value), "detail": detail,
        "hint": _grade_hint(value),
    }


#------------------------------------------------------------------
# 등급 필드 1개 검사 (V5)
#=> 값이 정의된 등급인지 본다. 키가 아예 없으면 로더가 기본값을 넣으므로 통과시킨다.
#    1) 키 없음 → 검사 대상 아님(None 반환)
#    2) 값이 null/문자열 아님/서열에 없음 → 위반
#
# -in: item        = 규칙 항목 dict
# -in: field       = 볼 필드명
# -in: section     = 섹션 키(메시지용)
# -in: rule_id     = 규칙 id(메시지용)
#
# -out: (violation, grade) = 위반 dict 또는 None, 그리고 검증을 통과한 등급 값(아니면 None)
# -out: error = 없음
#------------------------------------------------------------------
def _check_grade_field(item, field, section, rule_id):
    value = item.get(field, _ABSENT)
    # 키 자체가 없으면 로더 기본값(항상 유효)이 쓰인다 → 검사할 것이 없다.
    if value is _ABSENT:
        return None, None
    if value is None:
        return _violation("V5", section, rule_id, field, value, "값이 비어 있습니다(null)"), None
    if not isinstance(value, str):
        return _violation("V5", section, rule_id, field, value,
                          f"등급은 문자열이어야 하는데 {_type_name(value)} 입니다"), None
    if value not in _GRADE_RANK:
        return _violation("V5", section, rule_id, field, value, "정의되지 않은 등급"), None
    return None, value


#------------------------------------------------------------------
# 규칙셋 원본(YAML) 검증 — V5·V6
#=> load_rules 가 파싱한 원시 dict 를 그대로 훑어, 등급 관련 위반을 '전부' 모은다.
#   첫 오류에서 멈추지 않는 이유는 관리자가 한 번에 고칠 수 있게 하기 위해서다.
#    1) 섹션마다 항목을 돌며 등급 필드를 검사(V5)
#    2) base/bulk 쌍이 있으면 bulk 가 base 보다 낮지 않은지 검사(V6)
#    3) 이제 안 쓰는 blocks(paths)가 남아 있으면 알린다(V12)
#
#   [V12 의 뜻이 바뀌었다, 2026-09-08] 예전에는 '경로 규칙에 grade 도
#   acl_restricted 도 없다'는 검사였다. 경로 신호를 걷어내면서, 같은 번호를
#   '경로 블록이 아직 남아 있다'는 안내로 쓴다. 이 파서는 모르는 최상위 키를
#   조용히 무시하므로, 알리지 않으면 배포된 규칙셋에서 등급이 소리 없이 바뀐다.
#
#   [V6 가 필요한 이유] bulk_grade 는 "대량 검출 시 등급을 올린다"는 설계다.
#   그런데 base=S, bulk=O 처럼 거꾸로 적으면 주민번호가 많이 나올수록 등급이
#   내려간다. 문법상으론 멀쩡해 보여서 눈으로는 놓치기 쉬운 종류의 실수다.
#
#   [검증 대상 범위] 로더가 실제로 읽어들이는 항목만 본다. 예를 들어 regex_pii 에서
#   label 이 없는 옛 항목은 로더가 건너뛰므로 검증도 건너뛴다(쓰이지 않는 규칙을
#   두고 오류를 내면 이행 중 혼란만 준다).
#
# -in: data = yaml.safe_load 결과 dict
#
# -out: violations = 위반 dict 리스트(문제 없으면 빈 리스트)
# -out: error = 없음(예외를 던지지 않는다 — 판단은 호출자 몫)
#------------------------------------------------------------------
def validate_rules_data(data):
    violations = []
    data = data or {}

    # [2026-09-08] 없어진 블록이 남아 있으면 알린다. 이 파서는 모르는 최상위 키를
    # 조용히 무시하므로, 이게 없으면 '경로로 C 가 붙던 문서'가 아무 말 없이
    # 등급을 잃는다 — 배포된 규칙셋 3벌에 실제로 paths: 가 들어 있다.
    for gone, why in _GONE_SECTIONS.items():
        if data.get(gone):
            violations.append(_violation("V12", gone, "-", None, "(블록)", why))

    # 검토함 편입선 — 0 초과 1 이하의 수여야 한다. 틀린 값이 들어가면 검토 큐가
    # 통째로 비거나(0) 전부 들어가(1 초과) 화면이 못 쓰게 된다. 조용히 기본값으로
    # 되돌리지 않고 알린다 — 고쳤는데 왜 안 바뀌지가 되면 안 된다.
    rt = (data.get("defaults") or {}).get("review_threshold")
    if rt is not None:
        try:
            v = float(rt)
        except (TypeError, ValueError):
            v = None
        if v is None or not (0.0 < v <= 1.0):
            violations.append(_violation("V13", "defaults", "review_threshold", "-", rt,
                                         "0 보다 크고 1 이하인 수여야 합니다"
                                         "(검토함 편입선 — 이보다 확신이 낮으면 사람이 봅니다)"))

    # PII 등급 상한 — C/S/O 중 하나(대문자 그대로)여야 한다. 오타를 조용히 '제한 없음'으로
    # 읽으면 관리자는 막았다고 믿는데 PII 만으로 C 가 계속 나온다.
    cap = (data.get("defaults") or {}).get("pii_grade_cap")
    if cap is not None and (not isinstance(cap, str) or cap not in _GRADE_RANK):
        violations.append(_violation("V14", "defaults", "pii_grade_cap", "-", cap,
                                     "C · S · O 중 하나여야 합니다"
                                     "(개인정보만으로 올릴 수 있는 최고 등급 — 제한하지 않으려면 칸을 지우세요)"))

    for section, single_fields, bulk_pair in _GRADE_SECTIONS:
        items = data.get(section) or []
        # 섹션이 리스트가 아니면(예: 들여쓰기 실수로 dict 가 됨) 순회 자체가 무의미하다.
        if not isinstance(items, list):
            violations.append(_violation("V0", section, "-", "", items,
                                         "섹션이 목록(list)이 아닙니다"))
            continue

        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                violations.append(_violation("V0", section, f"#{idx}", "", item,
                                             "항목이 매핑(mapping)이 아닙니다"))
                continue
            rule_id = item.get("id") or f"#{idx}"
            # 로더가 label 없는 regex_pii 항목을 건너뛰므로 검증도 같은 기준을 쓴다.
            if section == "regex_pii" and not (item.get("label") or item.get("kopii_label")):
                continue

            for field in single_fields:
                v, _ = _check_grade_field(item, field, section, rule_id)
                if v:
                    violations.append(v)

            if bulk_pair:
                base_field, bulk_field = bulk_pair
                v_base, base_grade = _check_grade_field(item, base_field, section, rule_id)
                v_bulk, bulk_grade = _check_grade_field(item, bulk_field, section, rule_id)
                if v_base:
                    violations.append(v_base)
                if v_bulk:
                    violations.append(v_bulk)
                # 둘 다 유효할 때만 방향을 본다(하나가 오타면 V5 로 이미 보고됨).
                if base_grade and bulk_grade:
                    if _GRADE_RANK[bulk_grade] < _GRADE_RANK[base_grade]:
                        violations.append(_violation(
                            "V6", section, rule_id, bulk_field, bulk_grade,
                            f"bulk 는 상향이어야 하는데 낮습니다 "
                            f"(base={base_grade}({_GRADE_RANK[base_grade]}) → "
                            f"bulk={bulk_grade}({_GRADE_RANK[bulk_grade]}))"))

    return violations


#------------------------------------------------------------------
# 위반 목록 → 사람이 읽는 보고문
#=> 검증 코드(V5/V6/V0)별로 묶어 한 화면에 정리한다. 오타 후보가 있으면 함께 보여
#   관리자가 파일을 뒤지지 않고 바로 고칠 수 있게 한다.
#
# -in: path       = 규칙셋 파일 경로
# -in: violations = validate_rules_data 결과
#
# -out: text = 여러 줄 문자열(끝에 개행 없음)
# -out: error = 없음
#------------------------------------------------------------------
def format_violations(path, violations):
    titles = {
        "V0": "[V0] 규칙셋 구조 오류",
        "V5": "[V5] 정의되지 않은 등급 참조",
        "V6": "[V6] bulk 상향 규칙 위반 (bulk_grade 가 base_grade 보다 낮음)",
        "V12": "[V12] 이제 쓰지 않는 블록이 남아 있음",
    }
    lines = [
        f"[규칙셋 오류] {path} — 검증 실패 {len(violations)}건. 분류를 시작하지 않았습니다.",
        "",
        f"  정의된 등급: {' < '.join(GRADES)}",
        "",
    ]
    # 코드 순서를 고정해(V0 → V5 → V6) 실행할 때마다 보고서 모양이 흔들리지 않게 한다.
    for code in ("V0", "V5", "V6", "V12"):
        group = [v for v in violations if v["code"] == code]
        if not group:
            continue
        lines.append(f"  {titles.get(code, '[' + code + ']')}")
        for v in group:
            where = f"{v['section']}[{v['rule_id']}]"
            if v["field"]:
                where += f".{v['field']}"
            hint = f"   → 혹시 {v['hint']} ?" if v.get("hint") else ""
            lines.append(f"    {where} = {v.get('value_text', v['value'])}  — {v['detail']}{hint}")
        lines.append("")
    lines.append(f"  고치는 법: {path} 를 열어 위 항목을 고치세요.")
    lines.append(f"    · 등급 값은 {'/'.join(GRADES)} 중 하나여야 합니다.")
    # V12 는 '등급을 고치라'가 아니라 '지우라'는 안내라 따로 한 줄 붙인다.
    if any(v["code"] == "V12" for v in violations):
        lines.append("    · paths: 블록은 2026-09-08 에 없어졌습니다 — 통째로 지우세요. "
                     "경로로 등급을 정하던 자리는 --failsafe 로 대신합니다.")
    return "\n".join(lines)


#------------------------------------------------------------------
# 규칙셋 로드(YAML → RuleSet)
#=> cso_rule.yaml 을 읽어 RuleSet 을 만든다. 경로를 안 주면 default_rules_path().
#    1) YAML 파싱 → defaults 블록 해석
#    2) regex_pii 각 항목을 (ko-pii 라벨 기반) RegexRule 로
#    3) keywords 각 항목을 KeywordRule 로
#    4) 등급 값 검증(V5·V6) — 하나라도 어긋나면 RuleSet 을 만들지 않고 실패
#
#   [4단계를 왜 로드에 붙였나] 규칙셋이 잘못된 채로 스캔이 시작되면, 절반쯤
#   잘못 분류된 결과가 만들어진다. 그 결과는 겉보기에 정상이라 더 위험하다.
#   그래서 문서를 한 건도 열기 전에 여기서 막는다.
#
# -in: path     = 규칙셋 파일 경로(없으면 기본 경로)
# -in: validate = False 면 검증을 건너뛴다. 검증기 자체를 시험하거나, 잘못된
#                 규칙셋을 일부러 읽어 봐야 하는 도구용 탈출구다(기본 True)
#
# -out: RuleSet
# -out: error = 파일 없음 시 FileNotFoundError(어디에 두면 되는지 안내 메시지 포함)
# -out: error = 등급 값이 틀리면 RuleSetValidationError(위반 전체 목록 포함)
# -out: error = YAML 문법 오류·최상위가 매핑이 아니면 RuleSetValidationError(V0 1건)
#------------------------------------------------------------------
def load_rules(path=None, validate=True):
    path = path or default_rules_path()
    # 규칙셋은 exe 옆 외장 파일이라 '깜빡 누락'이 흔하다. 원시 스택 대신 어디에 무엇을
    # 둬야 하는지 알려 주는 친절한 오류로 바꿔, 사용자가 바로 조치할 수 있게 한다.
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"규칙셋(cso_rule.yaml)을 찾을 수 없습니다: {path}\n"
            f"  · exe 와 같은 폴더에 cso_rule.yaml 을 두거나,\n"
            f"  · --cso-rules <파일경로> 로 지정하거나,\n"
            f"  · 환경변수 CSOCLASSIFY_POLICY_DIR 로 폴더를 지정하세요."
        )
    # YAML 문법이 깨졌거나 최상위가 매핑이 아니면 '내용 오류(V0)'로 알린다.
    # 예전에는 YAMLError 가 그대로 올라가 internal_error(9001, 종료코드 1)로 나갔다 —
    # 종료코드 1 은 '결과는 있는데 일부 문서를 못 읽음'이라 배치가 결과가 있는 줄 알았다.
    # validate=False 여도 막는다: 매핑이 아니면 아래 data.get 에서 어차피 죽는다.
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise RuleSetValidationError(path, [_violation(
            "V0", "(파일)", "-", "", _ABSENT,
            "YAML 문법 오류 — " + " / ".join(str(e).splitlines()))])
    if not isinstance(data, dict):
        raise RuleSetValidationError(path, [_violation(
            "V0", "(파일)", "-", "", data, "최상위가 매핑(mapping)이 아닙니다")])

    # 등급 값 검증을 '파싱 직후·객체 생성 전'에 한다. 로더가 기본값을 채워 넣기 전의
    # 원본을 봐야 관리자가 실제로 적은 값을 그대로 짚어 줄 수 있다.
    if validate:
        violations = validate_rules_data(data)
        if violations:
            raise RuleSetValidationError(path, violations)

    d = data.get("defaults", {}) or {}
    defaults = Defaults(
        bulk_threshold=int(d.get("bulk_threshold", 5)),
        case_insensitive=bool(d.get("case_insensitive", True)),
        # 값이 없으면 종전과 같은 0.6 — 옛 규칙셋이 깔린 곳에서 검토량이
        # 조용히 달라지지 않게 한다.
        review_threshold=float(d.get("review_threshold", 0.6)),
        # 칸이 없으면 None = 제한 없음 — 옛 규칙셋은 판정이 한 글자도 달라지지 않는다.
        pii_grade_cap=d.get("pii_grade_cap"),
    )

    # 신뢰도 표: 기본값 위에 yaml 의 confidence 블록을 덮어쓴다(없으면 기본값 그대로).
    confidence = _default_confidence()
    yc = data.get("confidence", {}) or {}
    for grp in ("regex", "keyword", "sensitive", "stamp", "path"):
        g = yc.get(grp) or {}
        for w in ("high", "medium", "low"):
            if w in g:
                confidence[grp][w] = float(g[w])
    if "name" in yc:
        confidence["name"] = float(yc["name"])

    regex_rules = []
    for r in data.get("regex_pii", []) or []:
        # A안: pattern/validate 대신 ko-pii 라벨을 읽는다(label 필수).
        label = r.get("label") or r.get("kopii_label")
        if not label:
            # 라벨 없는 옛 항목은 조용히 건너뛴다(설정 이행 중 안전).
            continue
        regex_rules.append(RegexRule(
            id=r["id"],
            name=r.get("name", r["id"]),
            label=label,
            base_grade=r.get("base_grade", "S"),
            bulk_grade=r.get("bulk_grade"),
            bulk_threshold=r.get("bulk_threshold"),
            weight=r.get("weight", "medium"),
            seed_eligible=bool(r.get("seed_eligible", False)),
        ))

    pii_combos = []
    for r in data.get("pii_combos", []) or []:
        pii_combos.append(ComboRule(
            id=r["id"],
            name=r.get("name", r["id"]),
            all_of=tuple(r.get("all_of") or []),
            of=tuple(r.get("of") or []),
            min_types=int(r.get("min_types", 0) or 0),
            grade=r.get("grade", "C"),
            weight=r.get("weight", "high"),
            seed_eligible=bool(r.get("seed_eligible", False)),
        ))

    keyword_rules = []
    for r in data.get("keywords", []) or []:
        keyword_rules.append(KeywordRule(
            id=r["id"],
            name=r.get("name", r["id"]),
            terms=tuple(r.get("terms") or []),
            # 없으면 빈 튜플 — '파일명 신호 없음'이 된다(terms 로 폴백하지 않는다).
            filename=tuple(r.get("filename") or []),
            exclude=tuple(r.get("exclude") or []),
            # 단어별 최소 횟수 — 0·음수·빈 값은 1(종전 동작)로 본다. Rust 판 로더와 같은 규칙.
            term_min_count=tuple((str(k), max(1, int(v or 1)))
                                 for k, v in (r.get("term_min_count") or {}).items()),
            base_grade=r.get("base_grade", "S"),
            bulk_grade=r.get("bulk_grade"),
            bulk_threshold=r.get("bulk_threshold"),
            weight=r.get("weight", "medium"),
            seed_eligible=bool(r.get("seed_eligible", False)),
        ))

    sensitive_rules = []
    for r in data.get("sensitive", []) or []:
        sensitive_rules.append(SensitiveRule(
            id=r["id"],
            name=r.get("name", r["id"]),
            category=r.get("category", ""),
            terms=tuple(r.get("terms") or []),
            exclude=tuple(r.get("exclude") or []),
            # 0·음수·빈 값은 1(종전 동작)로 본다 — Rust 판 로더와 같은 규칙.
            min_count=max(1, int(r.get("min_count") or 1)),
            grade=r.get("grade", "C"),
            weight=r.get("weight", "high"),
            seed_eligible=bool(r.get("seed_eligible", False)),
        ))

    stamp_rules = []
    for r in data.get("stamps", []) or []:
        stamp_rules.append(StampRule(
            id=r["id"],
            name=r.get("name", r["id"]),
            terms=tuple(r.get("terms") or []),
            exclude=tuple(r.get("exclude") or []),
            grade=r.get("grade", "C"),
            weight=r.get("weight", "high"),
            seed_eligible=bool(r.get("seed_eligible", False)),
            always=bool(r.get("always", False)),
        ))

    return RuleSet(
        version=str(data.get("version", "unknown")),
        defaults=defaults,
        regex_rules=regex_rules,
        pii_combos=pii_combos,
        keyword_rules=keyword_rules,
        sensitive_rules=sensitive_rules,
        stamp_rules=stamp_rules,
        confidence=confidence,
    )
