#------------------------------------------------------------------
# 긴 로그 뒤 빈 줄 시험
#=> 한 건이 250자 이상인 로그는 편집기에서 여러 줄로 접혀 시작과 끝을 알기 어렵다.
#   그래서 그런 로그 뒤에는 빈 줄이 하나 붙어야 한다(일반 로그·오류 로그 모두).
#   짧은 로그는 예전과 같이 빈 줄 없이 이어져야 한다.
#------------------------------------------------------------------

import logging

import pytest

from csoclassify import logsetup


#------------------------------------------------------------------
# 각 시험 뒤 로거 핸들러 정리
#=> 파일 핸들러가 남으면 다음 시험이 지워진 임시 파일에 쓰려다 충돌한다.
#
# -in: 없음(pytest fixture)
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clean_handlers():
    yield
    lg = logging.getLogger("csoclassify")
    for h in list(lg.handlers):
        h.close()
        lg.removeHandler(h)


#------------------------------------------------------------------
# 로그 파일을 줄 단위로 읽기(줄바꿈 표기 통일)
#
# -in: p = 파일 경로
#
# -out: str = 파일 내용('\r\n' → '\n')
# -out: error = 파일이 없으면 FileNotFoundError
#------------------------------------------------------------------
def _read(p):
    return p.read_text(encoding="utf-8-sig").replace("\r\n", "\n")


#------------------------------------------------------------------
# 일반 로그 — 긴 것 뒤에만 빈 줄
#
# -in: tmp_path = pytest 임시 폴더
#
# -out: 없음
# -out: error = 단언 실패 시 AssertionError
#------------------------------------------------------------------
def test_long_line_followed_by_blank(tmp_path):
    p = tmp_path / "run.log"
    logsetup.setup_logging(log_path=str(p))
    log = logsetup.get_logger("csoclassify.test")
    log.info("가" * 300)
    log.info("짧은 줄")
    log.info("끝")
    body = _read(p)
    assert ("가" * 300) + "\n\n" in body, "긴 로그 뒤에 빈 줄이 있어야 한다"
    assert "짧은 줄\n" in body and "짧은 줄\n\n" not in body, "짧은 로그 뒤에는 빈 줄이 없어야 한다"


#------------------------------------------------------------------
# 경계값 — 머리말 포함 글자 수로 249자는 그대로, 250자부터 빈 줄
#
# -in: 없음
#
# -out: 없음
# -out: error = 단언 실패 시 AssertionError
#------------------------------------------------------------------
def test_boundary_counts_chars():
    f = logsetup._SpacedFormatter("%(message)s")
    rec = lambda m: logging.LogRecord("csoclassify.t", logging.INFO, __file__, 1, m, None, None)
    assert not f.format(rec("가" * 249)).endswith("\n")
    assert f.format(rec("가" * 250)).endswith("\n")


#------------------------------------------------------------------
# 오류 로그도 같은 규칙
#
# -in: tmp_path = pytest 임시 폴더
#
# -out: 없음
# -out: error = 단언 실패 시 AssertionError
#------------------------------------------------------------------
def test_err_log_spaced_too(tmp_path):
    err = tmp_path / "err.log"
    logsetup.setup_logging(err_log_path=str(err))
    logsetup.get_logger("csoclassify.test").error("오류 " * 100)
    assert _read(err).endswith("\n\n")
