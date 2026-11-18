# -*- coding: utf-8 -*-
"""moaipii — moaipii.dll / libmoaipii.so 를 ctypes 로 불러 쓰는 Python 감싸개

정본: MoAI-pii/bindings/python/moaipii.py — 쓰는 프로젝트는 이 파일을 복사해 둔다(직접 고치지 않는다).
표준 라이브러리만 쓴다(ctypes·json). Python 3.8 이상.

    import moaipii
    r = moaipii.scan("연락처 010-1234-5678", ["PHONE"])
    r["counts"]                       # {"PHONE": 1}
    for it in r["items"]:             # 좌표는 str 인덱스(코드포인트) — text[start:end] 로 자른다
        print(it["label"], it["start"], it["end"])

라이브러리를 찾는 순서: load(path) 로 준 경로 → 환경변수 MOAIPII_LIB(파일·폴더)
→ 이 .py 파일 폴더 → 그 아래 lib/ → PyInstaller 묶음 폴더(sys._MEIPASS) → 실행파일 폴더.
ctypes.CDLL 은 호출하는 동안 GIL 을 놓으므로 여러 스레드에서 동시에 불러도 병렬로 돈다.
"""
import ctypes
import json
import os
import sys
import threading

ABI_VERSION = 1

__all__ = ["MoaiPiiError", "load", "loaded", "lib_path", "version", "kopii_basis",
           "labels", "scan", "scan_json"]

_MP_OK = 0


#------------------------------------------------------------------
# 검사 실패 예외
#=> 라이브러리가 실패 코드를 돌려줬다. kind 에 라이브러리가 준 종류가 있다.
# -필드: kind = "arg"(인자·옵션 오류) | "utf8"(본문이 UTF-8 아님) | "panic"(내부 오류)
#             | "load"(라이브러리를 못 찾음·못 엶) | "abi"(함수 모양 판이 다름)
# -필드: code = 라이브러리 반환 코드(불러오기 실패면 None)
#------------------------------------------------------------------
class MoaiPiiError(Exception):
    #------------------------------------------------------------------
    # 예외 만들기
    #
    # -in: kind    = 실패 종류
    # -in: message = 사람이 읽는 사유
    # -in: code    = 반환 코드(없으면 None)
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def __init__(self, kind, message, code=None):
        super(MoaiPiiError, self).__init__(message)
        self.kind = kind
        self.code = code


_lock = threading.Lock()
_lib = None
_lib_path = None
_version = None
_basis = None


#------------------------------------------------------------------
# 이 OS 의 라이브러리 파일 이름
#
# -in: 없음
#
# -out: str = "moaipii.dll" | "libmoaipii.so" | "libmoaipii.dylib"
# -out: error = 없음
#------------------------------------------------------------------
def _lib_name():
    if sys.platform.startswith("win"):
        return "moaipii.dll"
    if sys.platform == "darwin":
        return "libmoaipii.dylib"
    return "libmoaipii.so"


#------------------------------------------------------------------
# 라이브러리 후보 경로들
#=> 모듈 설명의 찾는 순서대로 후보를 만든다. 폴더가 오면 그 안의 라이브러리 이름을 붙인다.
#
# -in: path = 직접 지정한 파일·폴더(없으면 None)
#
# -out: list[str] = 후보 경로들(앞에서부터 본다)
# -out: error = 없음
#------------------------------------------------------------------
def _candidates(path=None):
    name = _lib_name()
    out = []

    # 파일이면 그대로, 폴더면 라이브러리 이름을 붙여 후보에 넣는다
    def add(p):
        if p:
            out.append(os.path.join(p, name) if os.path.isdir(p) else p)

    add(path)
    add(os.environ.get("MOAIPII_LIB"))
    here = os.path.dirname(os.path.abspath(__file__))
    add(here)
    add(os.path.join(here, "lib"))
    add(getattr(sys, "_MEIPASS", None))
    add(os.path.dirname(os.path.abspath(sys.executable)))
    return out


#------------------------------------------------------------------
# 함수 인자·반환 모양 등록
#=> ctypes 는 기본으로 int 를 가정하므로 포인터를 다루는 함수는 모양을 꼭 적어야 한다
#   (64비트에서 포인터가 잘린다).
#
# -in: d = ctypes.CDLL
#
# -out: 없음
# -out: error = 함수가 없으면 AttributeError
#------------------------------------------------------------------
def _declare(d):
    P = ctypes.POINTER
    c, cp, vp = ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p
    d.moaipii_abi_version.restype = ctypes.c_uint32
    d.moaipii_abi_version.argtypes = []
    d.moaipii_version.restype = cp
    d.moaipii_version.argtypes = []
    d.moaipii_kopii_basis.restype = cp
    d.moaipii_kopii_basis.argtypes = []
    # out 문자열은 c_void_p 로 받아야 ctypes 가 bytes 로 바꿔 버리지 않고 해제할 포인터가 남는다.
    d.moaipii_labels.restype = c
    d.moaipii_labels.argtypes = [P(vp)]
    # 본문은 (포인터, 길이) — c_char_p 에 bytes 를 주면 복사 없이 그 버퍼 주소가 넘어간다.
    d.moaipii_scan.restype = c
    d.moaipii_scan.argtypes = [cp, ctypes.c_size_t, cp, P(vp)]
    d.moaipii_free.restype = None
    d.moaipii_free.argtypes = [vp]


#------------------------------------------------------------------
# 라이브러리 불러오기
#=> 한 번 불러오면 프로세스가 끝날 때까지 쓴다(다시 부르면 그대로 돌려준다).
#    1) 후보를 차례로 보며 있는 파일을 연다(path 를 줬으면 그것만 본다)
#    2) ABI 버전 확인 — 다르면 MoaiPiiError("abi")
#    3) 함수 모양 등록, 버전 읽기
#
# -in: path = 라이브러리 파일·폴더(None 이면 찾는 순서대로)
#
# -out: ctypes.CDLL = 불러온 라이브러리
# -out: error = 못 찾거나 못 열면 MoaiPiiError("load"), ABI 가 다르면 MoaiPiiError("abi")
#------------------------------------------------------------------
def load(path=None):
    global _lib, _lib_path, _version, _basis
    with _lock:
        if _lib is not None:
            return _lib
        cands = _candidates(path)
        if path:
            cands = cands[:1]
        found = next((p for p in cands if os.path.isfile(p)), None)
        if not found:
            raise MoaiPiiError("load", "개인정보 검출 라이브러리(%s)를 찾을 수 없습니다 — 찾아본 곳: %s "
                                       "(환경변수 MOAIPII_LIB 로 알려 주세요)" % (_lib_name(), " , ".join(cands)))
        try:
            d = ctypes.CDLL(found)
        except OSError as e:
            raise MoaiPiiError("load", "개인정보 검출 라이브러리를 열지 못했습니다: %s (%s)" % (found, e))
        d.moaipii_abi_version.restype = ctypes.c_uint32
        got = d.moaipii_abi_version()
        if got != ABI_VERSION:
            raise MoaiPiiError("abi", "개인정보 검출 라이브러리의 ABI 버전이 다릅니다: 라이브러리 %d / 감싸개 %d (%s)"
                               % (got, ABI_VERSION, found))
        _declare(d)
        _version = d.moaipii_version().decode("utf-8", "replace")
        _basis = d.moaipii_kopii_basis().decode("utf-8", "replace")
        _lib, _lib_path = d, found
        return d


#------------------------------------------------------------------
# 불러왔는지
#
# -in: 없음
#
# -out: bool = 이미 불러왔으면 True
# -out: error = 없음
#------------------------------------------------------------------
def loaded():
    return _lib is not None


#------------------------------------------------------------------
# 불러온 라이브러리 파일 경로
#
# -in: 없음
#
# -out: str = 경로(필요하면 이때 불러온다)
# -out: error = load 와 같음
#------------------------------------------------------------------
def lib_path():
    load()
    return _lib_path


#------------------------------------------------------------------
# 라이브러리 버전 ("0.1.0+abc1234")
#
# -in: 없음
#
# -out: str = 버전
# -out: error = load 와 같음
#------------------------------------------------------------------
def version():
    load()
    return _version


#------------------------------------------------------------------
# 검출 기준이 된 ko-pii 판 ("1.15.2")
#
# -in: 없음
#
# -out: str = ko-pii 판
# -out: error = load 와 같음
#------------------------------------------------------------------
def kopii_basis():
    load()
    return _basis


#------------------------------------------------------------------
# JSON 결과 함수를 불러 (코드, 문자열)로 받고 곧바로 해제
#=> 결과에 개인정보 값이 있을 수 있어 라이브러리가 0 으로 덮고 지운다(moaipii_free).
#
# -in: fn = out 자리를 받아 코드를 돌려주는 호출
#
# -out: (int, str) = 반환 코드와 결과 JSON 문자열
# -out: error = 없음
#------------------------------------------------------------------
def _call(fn):
    out = ctypes.c_void_p()
    code = fn(ctypes.byref(out))
    try:
        s = ctypes.string_at(out.value).decode("utf-8", "replace") if out.value else ""
    finally:
        # 읽다가 실패해도 라이브러리 메모리는 돌려준다
        if out.value:
            _lib.moaipii_free(out)
    return code, s


#------------------------------------------------------------------
# 결과 JSON → dict, 실패면 예외
#
# -in: code = 반환 코드
# -in: s    = 결과 JSON 문자열
#
# -out: dict = 성공 결과
# -out: error = 실패 코드면 MoaiPiiError(결과의 error.kind)
#------------------------------------------------------------------
def _result(code, s):
    try:
        v = json.loads(s) if s else {}
    except ValueError:
        v = {"error": {"kind": "panic", "message": "결과 JSON 오류: %r" % s[:200]}}
    if code != _MP_OK:
        e = v.get("error") or {}
        raise MoaiPiiError(e.get("kind") or "panic", e.get("message") or "알 수 없는 오류", code)
    return v


#------------------------------------------------------------------
# 지원 라벨 목록
#
# -in: 없음
#
# -out: dict = {"labels": [...], "aliases": {"PRESCRIPTION": "PRESCRIPTION_ID"}}
# -out: error = load 와 같음, 라이브러리 실패면 MoaiPiiError
#------------------------------------------------------------------
def labels():
    load()
    return _result(*_call(lambda o: _lib.moaipii_labels(o)))


#------------------------------------------------------------------
# 결과 JSON 문자열 그대로 받기
#=> 옵션을 직접 JSON 으로 만들어 넘기고 싶을 때 쓴다. 실패도 예외 없이 (코드, JSON) 으로 준다.
#
# -in: data      = 본문 bytes (UTF-8)
# -in: opts_json = 옵션 JSON 문자열(labels 필수)
#
# -out: (int, str) = 반환 코드와 결과 JSON 문자열
# -out: error = load 와 같음
#------------------------------------------------------------------
def scan_json(data, opts_json):
    load()
    o = opts_json.encode("utf-8") if isinstance(opts_json, str) else opts_json
    return _call(lambda out: _lib.moaipii_scan(data, len(data), o, out))


#------------------------------------------------------------------
# 본문 검사
#=> 본문에서 개인정보 위치·건수를 찾는다. 건수와 위치는 한 번의 스캔에서 나온다.
#    1) str 이면 UTF-8 로 바꾼다 — 짝 없는 서로게이트는 "?" 한 글자로 바꿔 글자 수를 지킨다
#       (그래야 돌려받은 char 좌표로 원래 str 을 그대로 자를 수 있다)
#    2) bytes 면 그대로 넘긴다(깨진 UTF-8 은 invalid_utf8 에 따라)
#    3) 옵션 JSON 을 만들어 moaipii_scan 을 부른다
#
# -in: text           = 본문 str 또는 bytes
# -in: labels         = 켤 라벨들(목록·집합)
# -in: items          = False 면 건수만(기본 True)
# -in: values         = True 면 위치마다 원문 값(기본 False — 값은 text[start:end] 로 자르면 된다)
# -in: offsets        = "char"(기본, str 인덱스) | "utf16" | "byte"
# -in: invalid_utf8   = "error"(기본) | "replace" — bytes 를 줄 때만 뜻이 있다
# -in: unknown_labels = "error"(기본) | "ignore"
# -in: today          = None(시스템 날짜) 또는 "YYYY-MM-DD"
#
# -out: dict = {"chars": int, "counts": {라벨: 건수}, "items": [{"label","start","end"[,"value"]}],
#               "unknown": [...]}(무시한 라벨이 있을 때만)
# -out: error = 인자·옵션 오류·내부 오류면 MoaiPiiError, 라이브러리가 없으면 load 와 같음
#------------------------------------------------------------------
def scan(text, labels, items=True, values=False, offsets="char", invalid_utf8="error",
         unknown_labels="error", today=None):
    load()
    if isinstance(text, str):
        # "replace" 는 짝 없는 서로게이트 한 칸을 "?" 한 칸으로 바꾼다 → 코드포인트 수가 그대로다.
        data = text.encode("utf-8", "replace")
    else:
        data = bytes(text)
    opts = {"labels": sorted(set(labels)), "items": bool(items), "values": bool(values),
            "offsets": offsets, "invalid_utf8": invalid_utf8, "unknown_labels": unknown_labels,
            "today": today}
    return _result(*_call(lambda out: _lib.moaipii_scan(
        data, len(data), json.dumps(opts).encode("utf-8"), out)))
