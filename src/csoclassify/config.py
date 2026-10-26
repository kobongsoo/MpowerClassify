#------------------------------------------------------------------
# 전역 설정/기본값 모음
#=> 모델 목록, 청킹 기본값, 데몬 기본값 등 "숫자·문자열 상수"를 한 곳에 모아
#   코드 곳곳에 흩어지지 않게 한다. 여기 값만 바꾸면 전체 동작 기본값이 바뀐다.
#------------------------------------------------------------------

import os
from dataclasses import dataclass


#------------------------------------------------------------------
# 환경변수 → 불리언 해석
#=> "1/true/yes/on"(대소문자 무관)이면 True, 그 외/미설정이면 default 로 본다.
#   CLI 플래그가 없을 때의 기본 동작을 운영자가 환경변수로 일괄 조정하게 해 준다.
#
# -in: name    = 읽을 환경변수 이름
# -in: default = 미설정/해석불가일 때 반환할 기본값(기본 False)
#
# -out: bool = 해석 결과
# -out: error = 없음(항상 bool 반환)
#------------------------------------------------------------------
def _env_bool(name, default=False):
    # 미설정이면 곧장 기본값(환경변수 없음 = 기본 동작 유지).
    raw = os.environ.get(name)
    if raw is None:
        return default
    # 참으로 인정하는 값만 True, 나머지는 False 로 단순·명확하게 처리.
    return raw.strip().lower() in ("1", "true", "yes", "on")


#------------------------------------------------------------------
# 임베딩 모델 1개의 메타정보
#=> "이름(별칭) → 실제 폴더/HF 저장소/차원/프리픽스 규약"을 묶어 둔 표의 한 행.
#   e5 계열은 입력 앞에 'passage: ' / 'query: ' 를 붙여야 성능이 나오므로 그 규약도 담는다.
#
# -필드: key        = CLI --model 에서 쓰는 짧은 별칭 (예: "e5-small-ko")
# -필드: hf_id      = 원본 HuggingFace 저장소 id (ONNX 변환 시 사용)
# -필드: local_dir  = resources/models 아래 폴더명 (model.onnx, tokenizer.json 위치)
# -필드: dim        = 출력 벡터 차원
# -필드: passage_prefix = 문서(임베딩 대상)에 붙일 접두어 (없으면 빈 문자열)
# -필드: query_prefix   = 질의에 붙일 접두어 (본 도구는 문서용이라 참고용)
# -필드: max_tokens_model = 모델이 받을 수 있는 최대 토큰 길이(안전 상한)
#------------------------------------------------------------------
@dataclass(frozen=True)
class ModelSpec:
    key: str
    hf_id: str
    local_dir: str
    dim: int
    passage_prefix: str
    query_prefix: str
    max_tokens_model: int


# 지원 모델 레지스트리(별칭 → 스펙). 기본은 소형+한국어인 e5-small-ko.
MODELS = {
    "e5-small-ko": ModelSpec(
        key="e5-small-ko",
        hf_id="dragonkue/multilingual-e5-small-ko",
        local_dir="e5-small-ko",
        dim=384,
        passage_prefix="passage: ",
        query_prefix="query: ",
        max_tokens_model=512,
    ),
    "e5-small": ModelSpec(
        key="e5-small",
        hf_id="intfloat/multilingual-e5-small",
        local_dir="e5-small",
        dim=384,
        passage_prefix="passage: ",
        query_prefix="query: ",
        max_tokens_model=512,
    ),
    "ko-sroberta": ModelSpec(
        key="ko-sroberta",
        hf_id="jhgan/ko-sroberta-multitask",
        local_dir="ko-sroberta",
        dim=768,
        passage_prefix="",   # sroberta 계열은 프리픽스 규약 없음
        query_prefix="",
        max_tokens_model=512,
    ),
}

# ── 기본값들 ─────────────────────────────────────────────
DEFAULT_MODEL = "e5-small-ko"    # 기본 임베딩 모델 별칭
DEFAULT_MAX_TOKENS = 512         # 청크 1개의 최대 토큰 수(e5 계열 모델 최대 시퀀스 길이)
DEFAULT_OVERLAP = 32             # 인접 청크가 겹치는 토큰 수
DEFAULT_NORMALIZE = True         # 출력 벡터 L2 정규화 여부
DEFAULT_FORMAT = "json"          # 출력 형식(text/json/jsonl) — 기본 JSON(사람이 보기 좋은 들여쓰기)
DEFAULT_PRECISION = "f32"        # 출력 벡터 정밀도(f32/f16)

# 데몬 관련 기본값
DEFAULT_DAEMON = True            # 상주 데몬 모드 기본 on (설계서 결정5)
DEFAULT_IDLE_TIMEOUT = 0         # 유휴 자동 종료(초). 0 = 무한(자동종료 없음). --idle-timeout N 으로 유한 지정
DAEMON_HOST = "127.0.0.1"        # 데몬은 반드시 loopback 에만 바인딩(외부 노출 금지)
CLIENT_CONNECT_TIMEOUT = 0.3     # 데몬 접속/PING 타임아웃(초)
DAEMON_START_WAIT = 20.0         # 데몬 자동기동 후 준비 대기 최대(초)

# 사이냅 추출 관련
# snf_exe 1파일 처리 타임아웃(초). Rust 판 synap::snf_timeout_secs 와 같은 값 · 같은 환경변수.
SNF_TIMEOUT = int(os.environ.get("CSOCLASSIFY_SNF_TIMEOUT", 60))
# 추출 본문이 이보다 짧으면(공백 제외 글자 수) '읽을 글자가 없었다'로 본다.
# 스캔본(이미지) PDF · 빈 문서 · 깨진 파일이 여기 걸린다. 추출기는 이런 파일에
# 예외를 던지지 않고 장식기호 몇 개를 돌려주므로, 길이로 가리지 않으면
# '미분류'(=읽었는데 신호 없음)와 구분되지 않는다.
#   [값의 근거] d:\sample1 15건 실측 — 스캔본 3자, 가장 짧은 정상 문서 311자.
#   그 사이 어디든 되지만 정상 문서를 잘못 걸지 않도록 아래쪽(20)에 둔다.
#   환경변수 CSOCLASSIFY_MIN_TEXT_LEN 으로 현장에서 바꿀 수 있다.
MIN_TEXT_LEN = int(os.environ.get("CSOCLASSIFY_MIN_TEXT_LEN", "20"))
MIN_TEXT_LEN_WARN = MIN_TEXT_LEN     # 옛 이름(하위호환)

# ── 스캔 PDF 처리제외(EXCLUDED) 판정 — 1차 개발 범위: OCR 미적용 ──
# 텍스트 레이어 없이 페이지 그림만 든 PDF 는 '못 읽은 문서(실패)'가 아니라 '범위 밖이라
# 처리하지 않는 문서'다. 실패로 세면 스캔본이 섞인 배치마다 종료코드 1 이 나서, 진짜
# 깨진 문서가 스캔본 틈에 묻힌다. 그래서 따로 가려 '처리제외'로 표기한다.
#   [판정] ① 글자가 거의 없고(페이지 글자 < SCAN_PDF_PAGE_MAX_CHARS)
#          ② 그림이 페이지 넓이의 절반 이상을 덮는 페이지를 '스캔 페이지'로 보고,
#          ③ 스캔 페이지가 읽은 페이지의 절반 이상이면서
#          ④ 문서 전체도 페이지당 글자 < SCAN_PDF_PAGE_MAX_CHARS 이면 스캔 PDF 다.
#   [④가 왜 필요한가] 그림 페이지가 대부분이어도 몇 페이지에 제대로 된 본문이 있으면
#   읽을 것이 있는 문서다(실측 '지능형지식관리플랫폼' — 12쪽 중 11쪽이 그림, 1쪽 565자).
#   [값의 근거] D:\분류함 PDF 382건 실측 — 스캔본은 페이지당 0~0.1자·그림 덮음 100%,
#   그림이 많은 정상 문서(매뉴얼·제품소개서)는 페이지당 50자 이상이었다.
#   Rust 판(Rust/src/extract.rs 의 SCAN_PDF_*)과 같은 값이어야 한다 — 시험이 지킨다.
SCAN_PDF_PAGE_MAX_CHARS = 10     # 이 글자 수(공백 제외) 미만인 페이지만 스캔 후보
SCAN_PDF_IMAGE_COVER = 0.5       # 그림이 페이지 넓이의 이 비율 이상을 덮어야 스캔 페이지
SCAN_PDF_PAGE_RATIO = 0.5        # 스캔 페이지가 읽은 페이지의 이 비율 이상이어야 스캔 PDF

# ── 문서 크기 상한(Size Gate) — 설계: plan/문서크기-상한-설계-20260904.html ──
# [왜 필요한가] 지금까지 이 도구에는 "문서가 너무 크다"를 판단하는 자리가 한 군데도
#   없었다. 그래서 수천 페이지 PDF 한 건이 배치 전체를 붙잡거나, 328MB 짜리 학습용
#   tsv 가 통째로 PII 정규식에 들어가는 일을 막을 방법이 없었다.
# [핵심 원칙] 원본 '바이트 크기'는 비용의 대리지표로 부정확하다. 실측에서 55.3MB
#   PDF(이미지 위주 회사소개서)의 추출 글자가 343자였다. 바이트로 자르면 정상 문서를
#   잃는다. 그래서 진짜 상한은 '추출 후 글자수'(MAX_TEXT_CHARS)에 걸고, 바이트 상한은
#   문서가 아닌 것만 걸러내는 느슨한 안전핀으로 둔다.

# [G1] 추출 '전' 원본 파일 바이트 상한(안전핀).
#   [값의 근거] D:\분류함 2,254파일 실측 — p99=25MB, 100MB 이상은 2건(둘 다 학습
#   코퍼스 tsv)뿐이라 정상 문서를 한 건도 잃지 않는다. 50MB 로 낮추면 정상 업무문서인
#   55.3MB 회사소개서가 걸리므로 낮추면 안 된다.
MAX_FILE_BYTES = int(os.environ.get("CSOCLASSIFY_MAX_FILE_BYTES", 100 * 1024 * 1024))

# [G2] 텍스트 계열(txt/csv/tsv/json/html) 전용 바이트 상한.
#   [왜 20MB 에서 100MB 로 올렸나] 이 게이트는 문서를 '버린다'. 그런데 바로 뒤의 G3 는
#   같은 상황에서 버리지 않고 앞부분만 읽고 표식을 단다(아래 [주의] 참고). 텍스트만
#   20MB 에서 통째로 버리면 위 [핵심 원칙]("진짜 상한은 G3, 바이트는 느슨한 안전핀")과
#   어긋난다. 실측(D:\분류함 305건 파서 비교)에서 46~82MB 짜리 tsv·json 4건이 본문을
#   한 글자도 못 남기고 빠졌고, 사이냅은 같은 파일을 읽어냈다.
#   [비용이 늘지 않는 이유] PII 정규식·분류가 실제로 훑는 길이는 G3(200만 자)가 잡는다.
#   올려서 늘어나는 일은 디스크에서 더 읽고 디코드하는 몫뿐이다.
#   [그래도 안전핀은 남는다] G1 과 같은 100MB — 실측 2,254파일 중 100MB 초과는 2건뿐이다.
MAX_FILE_BYTES_TEXT = int(os.environ.get("CSOCLASSIFY_MAX_FILE_BYTES_TEXT", 100 * 1024 * 1024))

# [G3] 정제 텍스트 글자수 상한 — 다섯 게이트 중 '본체'.
#   [값의 근거] ① 실측 854자/페이지 기준 약 2,340페이지로, 최악 케이스(581페이지)의
#   4배라 정상 문서를 자를 위험이 없다. ② 한글 UTF-8 3바이트로 약 6MB → 데몬 IPC
#   상한(ipc.MAX_MSG=16MB)에 구조적으로 걸리지 않는다. ③ 전체 시간의 66%를 먹던
#   PII 정규식 스캔의 대상 길이가 여기서 유계가 된다.
#   [주의] 이 상한을 넘겨도 문서를 버리지 않는다 — 앞부분만 보고 등급을 내되 결과에
#   '일부만 봤다'는 표식을 단다(cli._apply_text_limit 참고).
MAX_TEXT_CHARS = int(os.environ.get("CSOCLASSIFY_MAX_TEXT_CHARS", 2_000_000))

# [G4] 압축 1건당 해제 누적 상한 / 내부 파일 개수 상한.
#   archive.expand_paths 의 max_depth 는 '깊이'만 막는다. 깊이 1짜리 zip 하나로도
#   디스크를 채울 수 있어(폭 방향 압축폭탄) 총량·개수 상한이 따로 필요하다.
MAX_ARCHIVE_BYTES = int(os.environ.get("CSOCLASSIFY_MAX_ARCHIVE_BYTES", 500 * 1024 * 1024))
MAX_ARCHIVE_MEMBERS = int(os.environ.get("CSOCLASSIFY_MAX_ARCHIVE_MEMBERS", 5000))

# [G5] 전용 파서(하이브리드) 시간 상한과 PDF 페이지 상한.
#   snf 는 subprocess 라 SNF_TIMEOUT 으로 이미 보호되지만, 기본 경로인 자체 파서는
#   in-process 호출이라 아무 방어가 없다(pdf_pdfium 은 전 페이지를 무제한 순회).
PARSER_TIMEOUT = int(os.environ.get("CSOCLASSIFY_PARSER_TIMEOUT", 60))
MAX_PDF_PAGES = int(os.environ.get("CSOCLASSIFY_MAX_PDF_PAGES", 3000))

# 추출 방식 — 설계: doc/CSO_HybridParse.html
#   [2026-08-26 기본값 변경] 기본이 '자체 파서'(하이브리드)다. 내용 감지로 포맷별
#   전용 파서를 라우팅한다(pdf→pypdfium2, hwp→HWP5, hwpx→zip/OWPML, doc/ppt→자체파서,
#   xls→xlrd, docx/xlsx/pptx→python-*, text→직접읽기). 전용 파서가 실패한 파일만
#   사이냅(snf)으로 폴백한다.
#   [왜 바꿨나] Rust 포트는 처음부터 자체 파서만 쓴다. Python 이 사이냅을 기본으로
#   쓰면 같은 문서에서 두 구현의 추출 텍스트가 달라져, 분류 로직이 같아도 결과가
#   갈린다(실측 354건 중 11건). 기본을 자체 파서로 맞춰 그 원인을 없앤다.
#   CSOCLASSIFY_HYBRID=0 또는 --synap-only 로 종전(사이냅 단독)으로 되돌릴 수 있다.
DEFAULT_HYBRID_PARSE = _env_bool("CSOCLASSIFY_HYBRID", True)

# 종료 코드(설계서 §7)
EXIT_OK = 0
EXIT_EXTRACT_FAIL = 1
EXIT_EMBED_FAIL = 2
EXIT_ARG_ERROR = 3
# 규칙셋 자체는 찾았지만 내용이 틀린 경우(등급 오타·bulk 역전 등). 배치 스크립트가
# "파일 없음(3)"과 "내용 오류(4)"를 구분해 대응할 수 있도록 별도 코드를 준다.
# Rust 판(MpowerClassify-rs)도 같은 값 4 를 쓴다 — 두 구현의 동작을 일치시킨다.
EXIT_RULES_INVALID = 4


#------------------------------------------------------------------
# 모델 별칭 → 스펙 조회
#=> 사용자가 준 --model 별칭으로 실제 모델 스펙을 찾아 준다.
#   없는 별칭이면 어떤 별칭이 가능한지 알려주는 친절한 에러를 낸다.
#
# -in: name = 모델 별칭 (예: "e5-small-ko")
#
# -out: ModelSpec = 해당 모델 메타정보
# -out: error = 등록되지 않은 별칭이면 KeyError
#------------------------------------------------------------------
def get_model_spec(name):
    # 등록 표에 없으면, 쓸 수 있는 이름 목록을 붙여 알려 준다.
    if name not in MODELS:
        avail = ", ".join(MODELS.keys())
        raise KeyError(f"알 수 없는 모델 '{name}'. 사용 가능: {avail}")
    return MODELS[name]
