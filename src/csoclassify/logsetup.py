#------------------------------------------------------------------
# 로깅 설정 (--log / --verbose)
#=> 프로그램 전체가 공유하는 'csoclassify' 로거를 한 곳에서 구성한다. 파일 로그는
#   --log 경로가 있을 때, 화면(stderr) 로그는 --verbose 일 때만 켠다. 데몬도 같은
#   함수를 써서 자신이 받은 --log 경로에 처리 로그를 남긴다(데몬은 콘솔이 없으므로).
#   각 모듈은 get_logger(__name__) 으로 자식 로거를 얻어 쓰면 이름/PID 가 함께 찍힌다.
#------------------------------------------------------------------

import logging
import os
import sys
import time

from . import resources

# 모든 모듈 로거의 공통 부모 이름. 이 로거에만 핸들러를 단다.
_ROOT_NAME = "csoclassify"

# 이 글자 수 이상인 로그 한 건 뒤에는 빈 줄을 하나 둔다(Rust 판 log.rs 와 같은 값).
LONG_LINE_CHARS = 250


#------------------------------------------------------------------
# 긴 로그 뒤에 빈 줄을 두는 포매터
#=> 경로·명령줄·오류 사유가 든 로그는 한 건이 수백 자라 편집기에서 여러 줄로 접혀
#   보인다. 그러면 어디서 끝나고 다음 로그가 어디서 시작하는지 알기 어렵다.
#   그래서 다 만든 로그 한 건(시각·PID 머리말 포함)이 LONG_LINE_CHARS 글자 이상이면
#   끝에 줄바꿈을 하나 더 붙여 빈 줄로 구분한다. 짧은 로그는 그대로다.
#
# -필드: (logging.Formatter 와 같음)
#------------------------------------------------------------------
class _SpacedFormatter(logging.Formatter):
    #------------------------------------------------------------------
    # 로그 한 건 문자열 만들기
    #
    # -in: record = logging.LogRecord
    #
    # -out: str = 만든 로그(길면 끝에 "\n" 하나 더 — 핸들러가 붙이는 줄바꿈과 합쳐 빈 줄)
    # -out: error = 없음(부모 format 의 동작 그대로)
    #------------------------------------------------------------------
    def format(self, record):
        s = super().format(record)
        # 바이트가 아니라 글자 수로 센다 — 한글 로그가 일찍 걸리지 않게.
        return s + "\n" if len(s) >= LONG_LINE_CHARS else s


#------------------------------------------------------------------
# 기본 로그 파일 경로
#=> --log 를 안 줬을 때 쓰는 경로. "실행된 MpowerClassify.exe 가 있는 폴더" 아래 log/
#   폴더에 날짜별 파일로 남긴다(<exe폴더>/log/class_YYYYMMDD.log). 소스 실행 때는
#   프로젝트 루트 아래 log/ 가 된다. 날짜별로 나눠 파일이 무한정 커지지 않게 한다.
#
# -in: 없음
#
# -out: path = 기본 로그 파일 절대경로(폴더는 setup_logging 이 만든다)
# -out: error = 없음
#------------------------------------------------------------------
def default_log_path():
    # 로컬시각 기준 날짜 태그(클라이언트/데몬이 같은 날이면 같은 파일을 공유).
    day = time.strftime("%Y%m%d")
    return os.path.join(log_dir(), f"class_{day}.log")


#------------------------------------------------------------------
# 로그를 모아 둘 폴더
#=> 일반 로그와 오류 로그를 '같은 곳'에 둔다. 예전에는 오류 로그만 실행 파일
#   바로 옆에 흩어져 있어서, 여러 폴더에서 exe 를 돌리면 로그가 폴더마다
#   따로 생기고 어디를 봐야 하는지 알 수 없었다.
#    1) 환경변수 CSOCLASSIFY_LOGDIR 이 있으면 그 폴더(여러 대를 한곳에 모을 때)
#    2) 없으면 <실행 파일이 있는 폴더>/log
#   소스 실행이면 exe_dir() 이 프로젝트 루트라 <루트>/log 가 된다.
#
# -in: 없음
#
# -out: dir = 로그 폴더 절대경로(만들지는 않는다 — 쓰는 쪽이 만든다)
# -out: error = 없음
#------------------------------------------------------------------
def log_dir():
    env = os.environ.get("CSOCLASSIFY_LOGDIR")
    if env:
        return os.path.abspath(env)
    return os.path.join(resources.exe_dir(), "log")


#------------------------------------------------------------------
# 오류 전용 로그 파일 경로
#=> 위의 일반 로그(log/class_YYYYMMDD.log)에는 정상 처리 기록까지 전부 들어가
#   수천 줄이 된다. 문제가 생겼을 때 "무엇이 잘못됐나"만 빨리 보려면 오류만
#   따로 모은 파일이 필요하다. 그래서 같은 log/ 폴더에
#   class_err_YYYYMMDD.log 로 오류(ERROR 이상)만 따로 남긴다.
#    1) 환경변수 CSOCLASSIFY_ERRLOG 가 있으면 그 '파일 경로'를 그대로 쓴다
#       (읽기전용 폴더에 설치했거나 여러 대의 로그를 한 파일로 모을 때)
#    2) 없으면 log_dir()/class_err_YYYYMMDD.log — 일반 로그와 같은 폴더
#   Rust 판(MpowerClassify-rs)도 같은 이름·같은 환경변수를 쓴다.
#
# -in: 없음
#
# -out: path = 오류 로그 파일 절대경로
# -out: error = 없음
#------------------------------------------------------------------
def default_err_log_path():
    env = os.environ.get("CSOCLASSIFY_ERRLOG")
    if env:
        return os.path.abspath(env)
    day = time.strftime("%Y%m%d")
    return os.path.join(log_dir(), f"class_err_{day}.log")


#------------------------------------------------------------------
# 모듈용 로거 얻기
#=> get_logger("csoclassify.pipeline") 처럼 자식 로거를 돌려준다. 자식은 부모
#   ('csoclassify')의 핸들러/레벨을 물려받아, 설정을 한 곳에서만 관리하면 된다.
#
# -in: name = 보통 모듈의 __name__ (예: "csoclassify.embed.onnx_embedder")
#
# -out: logger = logging.Logger (부모 'csoclassify' 의 자식)
# -out: error = 없음
#------------------------------------------------------------------
# 로그 파일 인코딩 고르기 — 새 파일이면 UTF-8 BOM
#=> 파일은 항상 UTF-8 로 쓴다. 그런데 Windows 의 메모장·엑셀·PowerShell 은 BOM 이
#   없는 UTF-8 을 ANSI(cp949)로 읽어 한글이 통째로 깨진다("화면" → "?붾㈃").
#   처음 만드는 파일에만 BOM 을 붙여 그 오해를 막는다 — 이어쓰는 파일에 또 붙이면
#   파일 중간에 BOM 이 박힌다.
#
# -in: path = 로그 파일 경로
#
# -out: str = "utf-8-sig"(새 파일) 또는 "utf-8"(이어쓰기)
# -out: error = 없음(경로를 못 읽으면 안전하게 "utf-8")
#------------------------------------------------------------------
def _log_encoding(path):
    try:
        return "utf-8" if os.path.isfile(path) and os.path.getsize(path) > 0 else "utf-8-sig"
    except OSError:
        return "utf-8"


#------------------------------------------------------------------
def get_logger(name):
    return logging.getLogger(name)


#------------------------------------------------------------------
# 로깅 구성 (핵심)
#=> 'csoclassify' 로거에 파일/화면 핸들러를 붙이고 레벨을 정한다. 프로세스마다 시작 시
#   한 번 부른다(클라이언트/데몬/in-process 공통).
#    1) 레벨: verbose 면 DEBUG, 아니면 INFO
#    2) --log 있으면 파일 핸들러(append, UTF-8) — 상위 폴더 없으면 만든다
#       2-1) 파일 핸들러를 붙인 직후, 이번 실행 시작을 알리는 구분선("---")을 파일에
#            먼저 남겨 이전 실행 로그와 눈으로 구분되게 한다
#    3) verbose 면 stderr 핸들러도 추가(화면에서 실시간 확인)
#    ※ 모든 핸들러가 _SpacedFormatter 를 함께 써서, 250자 이상인 로그 뒤에는 빈 줄이 붙는다
#    4) 핸들러가 하나도 없으면 NullHandler 로 'No handlers' 경고 방지
#
# -in: log_path     = 로그 파일 경로(None 이면 파일 로그 없음)
# -in: verbose      = True 면 DEBUG 레벨 + 화면(stderr) 출력
# -in: err_log_path = 오류(ERROR 이상) 전용 로그 경로(None 이면 안 남김).
#                     오류가 실제로 날 때만 파일이 생긴다(delay=True)
#
# -out: logger = 구성된 'csoclassify' 로거
# -out: error = 없음(파일 열기 실패는 조용히 무시하고 화면/Null 로 진행)
#------------------------------------------------------------------
def setup_logging(log_path=None, verbose=False, err_log_path=None):
    logger = logging.getLogger(_ROOT_NAME)
    # 자식→부모까지만 흐르게 하고 루트로는 전파하지 않아 중복 출력을 막는다.
    logger.propagate = False
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    # 재호출(테스트/재설정) 시 핸들러가 쌓이지 않도록 먼저 비운다.
    for h in list(logger.handlers):
        logger.removeHandler(h)

    # PID 를 넣어 클라이언트/데몬 로그를 한 파일에서도 구분할 수 있게 한다.
    # 긴 로그(LONG_LINE_CHARS 글자 이상) 뒤에는 빈 줄이 하나 붙는다 — 일반·오류·화면 로그 모두.
    fmt = _SpacedFormatter(
        "%(asctime)s [PID %(process)d] %(levelname)s "
        "%(name)s.%(funcName)s:%(lineno)d: %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    # (2) 파일 로그: --log 가 있을 때만. 데몬도 이 경로로 남긴다.
    if log_path:
        try:
            # 상대경로는 현재 작업폴더 기준. 상위 폴더가 없으면 만들어 준다.
            parent = os.path.dirname(os.path.abspath(log_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            fh = logging.FileHandler(log_path, mode="a", encoding=_log_encoding(log_path))
            fh.setFormatter(fmt)
            logger.addHandler(fh)
            # 새 실행 시작을 이전 로그와 눈으로 구분하도록 구분선을 파일에 먼저 남긴다.
            # 직접 stream 에 써서 타임스탬프/PID 접두 없이 구분선만 남긴다. 파일은 text
            # 모드라 '\n' 이 Windows 에서 '\r\n' 으로 저장돼 기존 로그 줄과 개행이 일치한다.
            try:
                fh.stream.write("-------------\n")
                fh.stream.flush()
            except Exception:
                # 구분선은 부가 기능이라 실패해도 본 로깅은 계속되어야 한다.
                pass
        except OSError:
            # 로그 파일을 못 열어도 본 기능은 계속되어야 하므로 조용히 넘어간다.
            pass

    # (2-2) 오류 전용 로그: ERROR 이상만 log/class_err_YYYYMMDD.log 로.
    #   delay=True 가 핵심이다 — 이렇게 해야 '실제로 오류가 났을 때' 비로소 파일이
    #   만들어진다. 안 그러면 정상 실행마다 빈 파일이 쌓여, 파일이 있다는 것만으로는
    #   오류가 있었는지 알 수 없게 된다(있으면 = 오류가 있었다, 가 되어야 유용하다).
    if err_log_path:
        try:
            parent = os.path.dirname(os.path.abspath(err_log_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            eh = logging.FileHandler(err_log_path, mode="a",
                                     encoding=_log_encoding(err_log_path), delay=True)
            eh.setLevel(logging.ERROR)
            eh.setFormatter(fmt)
            logger.addHandler(eh)
        except OSError:
            # 오류 로그를 못 열어도 본 기능은 계속되어야 한다.
            pass

    # (3) 화면 로그: verbose 일 때만(평소엔 stdout/stderr 를 깔끔하게 유지).
    if verbose:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    # (4) 아무 핸들러도 없으면 INFO/DEBUG 는 조용히 버려지도록 NullHandler 를 둔다.
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())

    return logger
