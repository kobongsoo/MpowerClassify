#------------------------------------------------------------------
# 개인정보 검출 엔진 고르기(--pii-engine kopii | moaipii) — 시험
#=> 설계: D:\Project\MoAI-pii\plan\MoAI-pii-개인정보검출-라이브러리분리-설계-20261117.html §7
#   moaipii 는 Rust 판이 쓰는 것과 같은 라이브러리(moaipii.dll / libmoaipii.so)다.
#   전환 기간에는 두 엔진이 함께 있으므로 다음을 지킨다.
#    1) 흔한 문장에서 두 엔진의 건수가 같다(ko-pii 와 Rust 이식본이 같은 답을 내는 범위)
#    2) moaipii 갈래에서도 건수와 원문 값 목록이 같은 스캔에서 나온다(자기 일관성)
#    3) 좌표는 str 인덱스라 text[start:end] 가 값과 같다(짝 없는 서로게이트가 있어도)
#    4) moaipii 는 문서 전체를 보므로 100만 자 상한 표식(pii_scan_capped)이 서지 않는다
#    5) 시작 점검: 틀린 값은 1003, 라이브러리가 없으면 1018 pii_engine_missing
#------------------------------------------------------------------
import pytest

from csoclassify import config
from csoclassify.classify import rules as R

VALID_RRN = "900101-1234568"      # 체크섬 통과 주민번호


#------------------------------------------------------------------
# 이 PC 에 moaipii 라이브러리가 있는가 — 없으면 이 파일의 대부분을 건너뛴다
#=> 개발 PC 는 third_party/moaipii 에서, 배포 시험은 실행파일 옆에서 찾는다.
#
# -in: 없음
#
# -out: bool = 불러올 수 있으면 True
# -out: error = 없음
#------------------------------------------------------------------
def _have_moaipii():
    try:
        R.moaipii_load()
        return True
    except Exception:
        return False


needs_lib = pytest.mark.skipif(not _have_moaipii(),
                               reason="moaipii 라이브러리 없음(scripts/build/update-moaipii.sh 로 받으세요)")

TEXTS = [
    "담당자 홍길동 010-1234-5678, 메일 hong@example.co.kr",
    "주민등록번호 " + VALID_RRN + " / 사업자 022-00-76422 연락 010-1234-5678",
    "서버 192.168.10.20 접속, 버전 1.2.3.4 빌드",
    "",
]
LABELS = {"RRN", "PHONE", "EMAIL", "BUSINESS_REG", "IP"}


#------------------------------------------------------------------
# 시험 도우미: 엔진을 바꿔 건수를 센다
#=> config.PII_ENGINE 만 바꿔 같은 함수(_kopii_counts)를 부른다.
#
# -in: mp     = monkeypatch
# -in: engine = "kopii" | "moaipii"
# -in: text   = 본문
#
# -out: dict = {라벨: 건수}
# -out: error = 없음
#------------------------------------------------------------------
def _counts(mp, engine, text):
    mp.setattr(config, "PII_ENGINE", engine)
    return R._kopii_counts(text, LABELS)


#------------------------------------------------------------------
# 시험: 흔한 문장에서 두 엔진의 건수가 같다
#=> ko-pii 와 Rust 이식본(moaipii)이 같은 답을 내는 범위의 문장들이다. 다르면 전환 때 판정이 바뀐다.
#
# -in: monkeypatch = pytest 도구 / text = 본문(TEXTS 중 하나)
#
# -out: 없음
# -out: error = 기대와 다르면 assert 실패
#------------------------------------------------------------------
@needs_lib
@pytest.mark.parametrize("text", TEXTS)
def test_두_엔진의_건수가_같다(monkeypatch, text):
    assert _counts(monkeypatch, "moaipii", text) == _counts(monkeypatch, "kopii", text)


#------------------------------------------------------------------
# 규칙 흉내 — collect_pii 는 regex_rules 의 label 만 본다
# -필드: label = PII 라벨
#------------------------------------------------------------------
class _Rule:
    #------------------------------------------------------------------
    # 라벨 하나짜리 규칙 만들기
    #
    # -in: label = PII 라벨
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def __init__(self, label):
        self.label = label


#------------------------------------------------------------------
# 규칙셋 흉내 — LABELS 의 라벨을 모두 켠 규칙셋
# -필드: regex_rules = _Rule 목록
#------------------------------------------------------------------
class _RuleSet:
    regex_rules = [_Rule(l) for l in sorted(LABELS)]


#------------------------------------------------------------------
# 시험: moaipii 갈래의 건수와 원문 값 목록이 같은 스캔에서 나온다
#=> 2026-09-11 '전화 7건인데 값은 9개' 같은 어긋남이 moaipii 갈래에서도 없어야 한다. 값은 text[start:end] 와도 같아야 한다.
#
# -in: monkeypatch = pytest 도구
#
# -out: 없음
# -out: error = 기대와 다르면 assert 실패
#------------------------------------------------------------------
@needs_lib
def test_moaipii_건수와_값이_같은_스캔(monkeypatch):
    monkeypatch.setattr(config, "PII_ENGINE", "moaipii")
    text = TEXTS[1] * 3
    vals = R.collect_pii(text, _RuleSet())
    counts = R._kopii_counts(text, LABELS)
    # 값 목록을 라벨별로 세면 건수와 같아야 한다
    seen = {}
    for v in vals:
        seen[v["label"]] = seen.get(v["label"], 0) + 1
    assert seen == counts
    for v in vals:
        assert text[v["start"]:v["end"]] == v["value"]


#------------------------------------------------------------------
# 시험: 짝 없는 서로게이트·이모지가 있어도 좌표가 str 인덱스와 맞다
#=> 감싸개가 서로게이트를 '?' 한 글자로 바꿔 넘기므로 글자 수가 그대로여야 한다.
#
# -in: monkeypatch = pytest 도구
#
# -out: 없음
# -out: error = 기대와 다르면 assert 실패
#------------------------------------------------------------------
@needs_lib
def test_짝없는_서로게이트도_좌표가_맞다(monkeypatch):
    monkeypatch.setattr(config, "PII_ENGINE", "moaipii")
    text = "\ud800 전화 010-1234-5678 😀 메일 a.b@example.co.kr"
    vals = R.collect_pii(text, _RuleSet())
    assert [v["label"] for v in vals] == ["PHONE", "EMAIL"]
    assert [v["value"] for v in vals] == ["010-1234-5678", "a.b@example.co.kr"]


#------------------------------------------------------------------
# 시험: moaipii 는 100만 자 상한 표식을 세우지 않는다
#=> moaipii 는 문서 전체를 한 번에 본다. kopii 갈래는 예전처럼 상한에 걸린다.
#
# -in: monkeypatch = pytest 도구
#
# -out: 없음
# -out: error = 기대와 다르면 assert 실패
#------------------------------------------------------------------
@needs_lib
def test_moaipii_는_상한표식이_없다(monkeypatch):
    monkeypatch.setattr(config, "PII_ENGINE", "moaipii")
    big = "x" * (R._KOPII_MAX_TOTAL + 10)
    assert R.superlinear_coverage(big, {"PHONE"}) == (False, len(big), len(big))
    monkeypatch.setattr(config, "PII_ENGINE", "kopii")
    assert R.superlinear_coverage(big, {"PHONE"})[0] is True


#------------------------------------------------------------------
# 시험: 시작 점검 — 틀린 값은 1003 · 라이브러리가 없으면 1018
#=> 조용히 kopii 로 넘어가면 판정이 몰래 바뀌므로, 못 불러오면 문서를 읽기 전에 멈춰야 한다.
#
# -in: monkeypatch = pytest 도구
#
# -out: 없음
# -out: error = 기대와 다르면 assert 실패
#------------------------------------------------------------------
def test_시작점검(monkeypatch):
    from csoclassify import cli, errcodes
    monkeypatch.setattr(config, "PII_ENGINE", "kopii")

    # 엔진을 고르지 않은 인자(환경변수 값을 쓰게 된다)
    class Bad:
        pii_engine = None

    monkeypatch.setattr(config, "PII_ENGINE", "kopi")
    assert cli.check_pii_engine_args(Bad()) == errcodes.exit_of("bad_args")

    # 라이브러리를 못 찾는 상태를 흉내 낸다(이미 불러온 것을 비우고 엉뚱한 경로를 준다)
    from csoclassify.classify import _moaipii
    monkeypatch.setattr(_moaipii, "_lib", None)
    monkeypatch.setenv("CSO_MOAIPII", "없는폴더/moaipii.dll")
    monkeypatch.setattr(config, "PII_ENGINE", "kopii")

    # moaipii 를 고른 인자
    class Mp:
        pii_engine = "moaipii"

    assert cli.check_pii_engine_args(Mp()) == errcodes.exit_of("pii_engine_missing") == 3
    assert errcodes.ERRORS["pii_engine_missing"][0] == 1018
    # 실패했으면 고른 값을 새기지 않는다
    assert config.PII_ENGINE == "kopii"


#------------------------------------------------------------------
# 시험: 시작 점검 — 고른 엔진을 config 에 새긴다
#=> rules.pii_engine 이 config.PII_ENGINE 을 보므로 성공하면 값이 바뀌어 있어야 한다.
#
# -in: monkeypatch = pytest 도구
#
# -out: 없음
# -out: error = 기대와 다르면 assert 실패
#------------------------------------------------------------------
@needs_lib
def test_시작점검_값_새기기(monkeypatch):
    from csoclassify import cli
    monkeypatch.setattr(config, "PII_ENGINE", "kopii")

    # moaipii 를 고른 인자
    class Mp:
        pii_engine = "moaipii"

    assert cli.check_pii_engine_args(Mp()) is None
    assert config.PII_ENGINE == "moaipii"
