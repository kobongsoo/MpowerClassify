#------------------------------------------------------------------
# Rust 문서파서 엔진 — docparse 모듈을 TextExtractor 로 감싼다
#=> 설계: plan/Python-문서파서-Rust연동-설계-20261110.html §4-2
#   --extractor rust 일 때 HybridExtractor 의 '형식별 엔진 표' 자리에 이 엔진을 꽂는다.
#   그러면 파이썬 판과 Rust 판이 같은 파서 하나(docparse.dll / libdocparse.so)로 본문을 뽑는다.
#   사이냅 폴백 사슬 · 원시 텍스트 정제(clean.clean_text) · 오류 종류 가르기는 지금처럼
#   파이썬 쪽(hybrid.py · pipeline.py)이 맡는다. 그래서 여기서는 정제 전 원시 텍스트를 받는다.
#
#   문서파서는 별도 프로젝트(D:\Project\DocParse-rust)가 만든 공유 라이브러리이고,
#   _docparse.py(ctypes 감싸개, 정본은 DocParse-rust/bindings/python)로 불러 쓴다.
#   설계: plan/Rust-문서파서-라이브러리분리-설계-20261110.html (개정 2) §6-2
#   못 불러오면 조용히 파이썬 파서로 넘어가지 않는다 — cli 가 시작할 때 1017 로 멈춘다.
#------------------------------------------------------------------

import importlib.util
import os
import sys

from .. import config
from ..logsetup import get_logger
from . import notes
from .base import TextExtractor, ExtractError, SizeLimitError, save_extracted_text

from . import _docparse as _dp

# 라이브러리를 불러오다 난 오류(없으면 None). 처음 쓸 때 한 번 시도하고 결과를 기억한다.
_LOAD_ERROR = []

log = get_logger(__name__)

# 부분추출 경고에 쓸 형식별 이름 — 파이썬 파서들이 남기던 문장과 같은 머리말을 쓴다.
_PARTIAL_LABEL = {"pdf": "PDF", "xlsx": "xlsx", "pptx": "pptx", "hwpx": "hwpx", "docx": "docx"}

# Rust 엔진이 맡는 형식 — 파이썬 하이브리드 엔진 표와 같은 11가지다.
FORMATS = ("pdf", "hwp", "hwpx", "doc", "xls", "ppt", "docx", "xlsx", "pptx", "html", "text")


#------------------------------------------------------------------
# 문서파서 라이브러리 위치 힌트
#=> _docparse 의 기본 찾기(DOCPARSE_LIB → 감싸개 폴더 → _MEIPASS → 실행파일 폴더)
#   앞에 이 프로젝트의 자리를 더한다.
#    1) 환경변수 CSO_DOCPARSE(파일·폴더) — Rust 판과 같은 이름
#    2) 개발 PC: 저장소의 third_party/docparse/<os>-x64/ (버전을 고정해 둔 정본)
#    3) 못 찾으면 None — 감싸개 기본 찾기에 맡긴다(배포본은 실행파일 옆에 둔다)
#
# -in: 없음
#
# -out: str | None = 라이브러리 파일·폴더 경로
# -out: error = 없음
#------------------------------------------------------------------
def _lib_hint():
    env = os.environ.get("CSO_DOCPARSE")
    if env:
        return env
    sub = "windows-x64" if sys.platform.startswith("win") else "linux-x64"
    # src/csoclassify/extract/ → 저장소 최상위
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    d = os.path.join(root, "third_party", "docparse", sub)
    return d if os.path.isdir(d) and not getattr(sys, "frozen", False) else None


#------------------------------------------------------------------
# 라이브러리 불러오기(처음 한 번)
#=> 실패도 기억해 두어 문서마다 다시 찾지 않는다.
#
# -in: 없음
#
# -out: None 또는 예외 객체(실패 까닭)
# -out: error = 없음(예외를 돌려준다)
#------------------------------------------------------------------
def _try_load():
    if not _LOAD_ERROR:
        try:
            _dp.load(_lib_hint())
            _LOAD_ERROR.append(None)
        except Exception as e:  # 없음·ABI 다름·열기 실패 모두 같은 '못 씀'이다
            _LOAD_ERROR.append(e)
    return _LOAD_ERROR[0]


#------------------------------------------------------------------
# 문서파서 라이브러리를 쓸 수 있나
#=> --extractor rust 를 고른 실행이 시작할 때 묻는다.
#
# -in: 없음
#
# -out: (ok, why) = 쓸 수 있으면 (True, ""), 아니면 (False, 사람이 읽을 까닭)
# -out: error = 없음
#------------------------------------------------------------------
def available():
    err = _try_load()
    if err is None:
        return True, ""
    return False, f"docparse 라이브러리를 불러오지 못했습니다: {err}"


#------------------------------------------------------------------
# docparse 모듈 버전
#=> "크레이트 버전+git 해시". 로그에 남겨 "어느 파서로 뽑았나"를 나중에 가린다.
#
# -in: 없음
#
# -out: str = 버전(모듈이 없으면 "")
# -out: error = 없음
#------------------------------------------------------------------
def version():
    return _dp.version() if _try_load() is None else ""


#------------------------------------------------------------------
# 불러온 문서파서 라이브러리 파일 경로
#=> 로그에 남겨 "어느 파일을 불러 썼나"를 가린다.
#
# -in: 없음
#
# -out: str = 경로(못 불러왔으면 "")
# -out: error = 없음
#------------------------------------------------------------------
def lib_path():
    return _dp.lib_path() if _try_load() is None else ""


#------------------------------------------------------------------
# docparse 모듈 꺼내기(내부 헬퍼)
#
# -in: 없음
#
# -out: module = _docparse (라이브러리를 불러온 상태)
# -out: error = 라이브러리가 없으면 ExtractError(그 파일만 실패 — 보통은 시작할 때 이미 멈춘다)
#------------------------------------------------------------------
def _mod():
    err = _try_load()
    if err is not None:
        raise ExtractError(f"docparse 라이브러리 없음: {err}")
    return _dp


#------------------------------------------------------------------
# 내용으로 포맷 감지 (Rust)
#=> 감지가 다르면 같은 파서를 써도 다른 엔진으로 가므로, Rust 엔진을 쓸 때는 감지도
#   Rust 로 한다. 이름은 파이썬 detect.py 와 같다("pdf" · "docx" · … · "unknown").
#
# -in: path = 파일 경로
#
# -out: str = 포맷 이름
# -out: error = 모듈이 없으면 ExtractError
#------------------------------------------------------------------
def detect_format(path):
    return _mod().detect_format(os.fspath(path))


#------------------------------------------------------------------
# pdfium 라이브러리 위치 찾기
#=> Rust 판과 같은 차례로 찾고, 개발 PC 를 위해 마지막에 pypdfium2 휠 안의 것을 본다.
#    1) 환경변수 CSO_PDFIUM 이 가리키는 파일(Rust 판과 같은 이름)
#    2) 배포본(PyInstaller): 실행파일 옆 · _internal(_MEIPASS) 의 pdfium.dll/libpdfium.so
#    3) pypdfium2_raw 패키지 폴더의 라이브러리(지금 파이썬 배포본·개발 PC)
#    4) 못 찾으면 None — docparse 가 자기(DLL) 폴더 → 실행파일 옆 → 시스템 순으로 찾는다
#
# -in: 없음
#
# -out: str | None = 라이브러리 경로
# -out: error = 없음
#------------------------------------------------------------------
def pdfium_lib_path():
    names = ("pdfium.dll", "libpdfium.so", "libpdfium.dylib")
    env = os.environ.get("CSO_PDFIUM")
    if env and os.path.isfile(env):
        return env
    dirs = []
    if getattr(sys, "frozen", False):
        dirs.append(os.path.dirname(sys.executable))
        if getattr(sys, "_MEIPASS", None):
            dirs.append(sys._MEIPASS)
    # pypdfium2 를 import 하지 않고 위치만 본다 — 3단계에서 의존을 지워도 이 줄은 안전하다.
    try:
        spec = importlib.util.find_spec("pypdfium2_raw")
    except (ImportError, ValueError):
        spec = None
    if spec is not None and spec.submodule_search_locations:
        dirs.extend(spec.submodule_search_locations)
    for d in dirs:
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    return None


# pdfium 위치는 실행 중에 바뀌지 않으므로 한 번만 찾는다.
_PDFIUM = []


#------------------------------------------------------------------
# pdfium 위치(한 번만 찾고 기억)
#
# -in: 없음
#
# -out: str | None
# -out: error = 없음
#------------------------------------------------------------------
def _pdfium():
    if not _PDFIUM:
        _PDFIUM.append(pdfium_lib_path())
    return _PDFIUM[0]


class RustEngine(TextExtractor):
    #------------------------------------------------------------------
    # Rust 형식별 엔진 — 한 형식을 맡는다
    #=> HybridExtractor 의 엔진 표에 형식마다 하나씩 꽂힌다. 형식은 hybrid 가 감지해
    #   골라 주므로, 여기서는 그 형식으로 docparse 를 부른다.
    #
    # -필드: fmt = 맡은 형식 이름("pdf" · "docx" · …)
    #------------------------------------------------------------------
    def __init__(self, fmt):
        self.fmt = fmt

    #------------------------------------------------------------------
    # 문서 → 원시 텍스트
    #=> 지금 파이썬 파서들이 하던 일을 그대로 한다.
    #    1) 상한은 부를 때마다 config 에서 읽는다(--max-file-mb 등이 실행 중 덮어쓴다)
    #    2) docparse.extract(clean=False) — 정제는 pipeline 이 자기 clean_text 로 한다
    #    3) 크기 상한 → SizeLimitError, 그 밖의 실패 → ExtractError 로 바꿔 던진다
    #       (레코드의 error_kind 는 예외 종류로 가르므로 지금과 같다)
    #    4) 부분추출·PDF 페이지 구성 표식을 notes 에 옮겨 적는다(부분추출이면 경고 로그도)
    #    5) save_dir 가 있으면 지금과 같은 규칙으로 본문을 남긴다
    #
    # -in: input_path = 추출할 문서 경로
    # -in: save_dir   = 추출 텍스트 보존 폴더(None 이면 저장 안 함)
    #
    # -out: text = 원시 텍스트
    # -out: error = SizeLimitError(크기 상한) · ExtractError(깨짐·미지원·파서 실패·패닉)
    #------------------------------------------------------------------
    def extract(self, input_path, save_dir=None):
        dp = _mod()
        try:
            r = dp.extract(
                os.fspath(input_path), fmt=self.fmt, clean=False,
                # 0 은 '상한 없음' — docparse 도 같은 규칙(0 이하 = 없음)으로 받는다.
                parser_timeout=float(config.PARSER_TIMEOUT or 0),
                max_pdf_pages=int(config.MAX_PDF_PAGES or 0),
                max_file_bytes=int(config.MAX_FILE_BYTES or 0),
                max_file_bytes_text=int(config.MAX_FILE_BYTES_TEXT or 0),
                pdfium_lib=_pdfium())
        except dp.SizeLimitError as e:
            raise SizeLimitError(str(e)) from e
        except dp.ExtractError as e:
            raise ExtractError(str(e)) from e
        # 지금 파서들이 남기던 표식을 같은 자리에 남긴다(G5 관측 · 스캔 PDF 판정 재료).
        if r.partial:
            notes.set_partial(*r.partial)
            # Rust 쪽 경고는 파이썬 로그에 닿지 않는다 — 파이썬 파서들이 남기던 모양으로 대신 남긴다.
            reason, unit, read, total = r.partial
            log.warning("%s 부분 추출 %s 에서 중단 file=%s %s=%d/%d",
                        _PARTIAL_LABEL.get(self.fmt, self.fmt), reason, input_path, unit, read, total)
        if r.layout:
            notes.set_layout(*r.layout)
        if save_dir:
            save_extracted_text(save_dir, input_path, r.text)
        return r.text


#------------------------------------------------------------------
# 형식별 엔진 표 만들기
#=> build_extractor 가 부른다. 11개 형식에 같은 RustEngine 을 형식만 바꿔 꽂는다.
#
# -in: 없음
#
# -out: dict = {형식 이름: RustEngine}
# -out: error = 없음
#------------------------------------------------------------------
def engines():
    return {f: RustEngine(f) for f in FORMATS}


#------------------------------------------------------------------
# 압축 펼치기 (Rust) — archive.expand_paths 의 rust 갈래
#=> 파이썬 archive.expand_paths 와 같은 반환 모양(3-튜플 목록 · stats["unexpanded"])으로
#   돌려준다. 그래서 cli.py 는 고칠 것이 없다.
#    1) docparse.expand_paths 를 한 번 부른다(임시파일 일련번호가 겹치지 않게 한 번에)
#    2) 표시 경로(label·origin·stats path)의 맨 앞을 입력 경로 표기 그대로 되돌린다 —
#       Rust 는 '\' 를 '/' 로 바꿔 적지만 파이썬 판은 받은 경로를 그대로 적어 왔다
#    3) 못 펼친 압축은 파이썬 판과 같은 경고 로그를 남긴다
#
# -in: paths     = 입력 파일 경로 리스트
# -in: tmp_root  = 내부 파일을 풀 임시폴더(호출부가 만들고 끝나면 지운다)
# -in: max_depth = 중첩 압축을 펼칠 최대 깊이
# -in: stats     = 결과 보고용 dict(선택)
# -in: log       = 경고를 남길 로거
#
# -out: list[tuple(str,str,str|None)] = (읽기용 실제경로, 표시·신호용 경로, 최상위압축경로 또는 None)
# -out: error = 모듈이 없으면 ExtractError(개별 압축 실패는 stats 에 담고 원본 1건으로 둔다)
#------------------------------------------------------------------
def expand_paths(paths, tmp_root, *, max_depth=3, stats=None, log=None):
    dp = _mod()
    paths = [os.fspath(p) for p in paths]
    jobs, st = dp.expand_paths(paths, os.fspath(tmp_root), max_depth=max_depth,
                               max_bytes=int(config.MAX_ARCHIVE_BYTES or 0),
                               max_members=int(config.MAX_ARCHIVE_MEMBERS or 0))
    # Rust 표기('/' 로 바꾼 경로) → 입력 표기. 같은 경로가 두 번 와도 표기는 같다.
    back = {p.replace("\\", "/"): p for p in paths}

    def fix(label):
        # 맨 앞의 '입력 경로' 부분만 되돌린다 — 압축 내부 이름은 '/' 그대로 둔다.
        if label is None:
            return None
        if label in back:
            return back[label]
        i = label.rfind("/")
        while i > 0:
            head = label[:i]
            if head in back:
                return back[head] + label[i:]
            i = label.rfind("/", 0, i)
        return label

    out = [(src, fix(label), fix(origin)) for src, label, origin in jobs]
    if stats is not None:
        stats.setdefault("unexpanded", [])
    for u in st.get("unexpanded", []):
        u["path"] = fix(u["path"])
        if log is not None:
            log.warning("압축 확장 실패 → 펼치지 않음 file=%s :: %s", u["path"], u["reason"])
        if stats is not None:
            stats["unexpanded"].append(u)
    # 파이썬 판에 없던 두 갈래(미지원 압축 · 일부만 꺼냄)는 로그와 stats 의 별도 칸으로 남긴다.
    for key in ("unsupported", "partial"):
        for u in st.get(key, []):
            u["path"] = fix(u["path"])
            if log is not None:
                log.warning("압축 확장 %s file=%s :: %s", key, u["path"], u["reason"])
            if stats is not None:
                stats.setdefault(key, []).append(u)
    return out


#------------------------------------------------------------------
# 분할 압축 조각인지 (Rust)
#
# -in: name = 표시 이름(경로)
# -in: path = 실제 파일 경로(선택)
#
# -out: str | None = 조각 종류 설명 또는 None
# -out: error = 모듈이 없으면 ExtractError
#------------------------------------------------------------------
def split_volume_hint(name, path=None):
    return _mod().split_volume_hint(os.fspath(name), None if path is None else os.fspath(path))
