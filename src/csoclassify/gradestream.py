# -*- coding: utf-8 -*-
#------------------------------------------------------------------
# 등급 스트림(:mgrade) 읽기 · 쓰기
#=> 분류가 끝난 문서의 NTFS 대체 데이터 스트림 ":mgrade" 에 C/S/O 등급을 쓴다.
#   PC 보안 에이전트(NEOPCHook)가 문서를 열 때 이 스트림을 읽어 허용·차단을 정한다.
#   형식은 NEOGradeToolApp -s 와 바이트까지 같다(12바이트: "MGRD" · 버전 1 · 등급).
#   설계: plan/등급스트림-기록-옵션-설계-20261019.html
#   Rust 판 gradestream.rs 와 같은 계약이다 — 한쪽을 고치면 다른 쪽도 고친다.
#------------------------------------------------------------------
import logging
import os
import stat
import struct

log = logging.getLogger("csoclassify")

# 스트림 이름 · 크기 · 매직 · 버전 — Include/NEOPCCom.h 의 FILE_STREAM_GRADE_* 와 같다.
STREAM_NAME = "mgrade"
INFO_SIZE = 12
MAGIC = 0x4452474D          # 'DRGM' → 디스크에는 4D 47 52 44("MGRD")
VERSION_CURRENT = 1

# 등급 ↔ 숫자. NONE(0)은 '스트림은 있으나 미분류'라는 뜻이다(스트림 없음과 다르다).
GRADE_CODE = {"NONE": 0, "C": 1, "S": 2, "O": 3}
CODE_GRADE = {v: k for k, v in GRADE_CODE.items()}
GRADE_MAX = 4

# 엔진(CommonFileGrade.cpp)의 판별 상태 이름과 같다.
ST_MARKED = "MARKED"
ST_UNCLASSIFIED = "UNCLASSIFIED"
ST_NO_STREAM = "NO_STREAM"
ST_UNSUPPORTED_VOLUME = "UNSUPPORTED_VOLUME"
ST_READ_ERROR = "READ_ERROR"
ST_VERSION_UNSUPPORTED = "VERSION_UNSUPPORTED"
ST_FORMAT_ERROR = "FORMAT_ERROR"

# Windows 오류 번호(툴이 따로 안내하는 셋 + 파일 없음)
_ERR_FILE_NOT_FOUND = 2
_ERR_PATH_NOT_FOUND = 3
_ERR_ACCESS_DENIED = 5
_ERR_SHARING_VIOLATION = 32
_ERR_INVALID_NAME = 123


#------------------------------------------------------------------
# 이 플랫폼에서 쓸 수 있나
#=> 대체 데이터 스트림은 Windows(NTFS)에만 있다. 리눅스에서는 옵션을 거절한다.
#
# -in: 없음
#
# -out: bool = Windows 면 True
# -out: error = 예외 없음
#------------------------------------------------------------------
def supported():
    return os.name == "nt"


#------------------------------------------------------------------
# 긴 경로 접두어(\\?\) 붙이기
#=> 툴의 BuildGradeStreamPath 와 같은 규칙이다. 260자 제한을 피하고 엔진과 같은 경로를 쓴다.
#    1) 상대경로는 절대경로로 바꾼다
#    2) 이미 \\?\ 로 시작하면 그대로
#    3) UNC(\\server\share) 는 \\?\UNC\server\share
#    4) 드라이브 경로는 \\?\C:\...
#
# -in: path = 문서 경로
#
# -out: str = \\?\ 가 붙은 경로
# -out: error = 예외 없음
#------------------------------------------------------------------
def long_path(path):
    if path.startswith("\\\\?\\"):
        return path
    p = os.path.abspath(path)
    # UNC 는 앞의 \\ 를 떼고 \\?\UNC\ 를 붙인다
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


#------------------------------------------------------------------
# 등급 스트림 경로 만들기
#=> "<긴 경로>:mgrade" — 이 경로로 열면 본문이 아니라 대체 스트림이 열린다.
#
# -in: path = 문서 경로
#
# -out: str = 스트림 경로
# -out: error = 예외 없음
#------------------------------------------------------------------
def stream_path(path):
    return long_path(path) + ":" + STREAM_NAME


#------------------------------------------------------------------
# 12바이트 내용 만들기
#=> 리틀엔디언 ULONG 3개(Magic · Version · Grade). C 구조체(#pragma pack(1))와 같은 배치다.
#
# -in: code = 등급 숫자(0..3)
#
# -out: bytes = 12바이트
# -out: error = 예외 없음
#------------------------------------------------------------------
def payload(code):
    return struct.pack("<III", MAGIC, VERSION_CURRENT, code)


#------------------------------------------------------------------
# OSError → Windows 오류 번호
#=> 파이썬은 Windows 에서 winerror 를 싣는다. 없으면 errno 로 대신한다.
#
# -in: e = OSError
#
# -out: int = 오류 번호
# -out: error = 예외 없음
#------------------------------------------------------------------
def _winerr(e):
    return getattr(e, "winerror", None) or e.errno or 0


# kernel32 를 한 번만 준비해 둔다(_k32 가 채운다)
_K32 = None


#------------------------------------------------------------------
# kernel32 함수 준비(한 번만)
#=> 파이썬 open() 은 CRT 를 거쳐 공유 위반(32)·접근 거부(5)를 둘 다 errno 13 으로 뭉갠다.
#   사람이 할 일이 달라 두 경우를 갈라야 하므로 툴처럼 CreateFileW 로 직접 연다.
#
# -in: 없음
#
# -out: kernel32 = 인자형을 맞춘 WinDLL
# -out: error = Windows 가 아니면 OSError
#------------------------------------------------------------------
def _k32():
    global _K32
    if _K32 is None:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.restype = wintypes.HANDLE
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        k.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                                ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        _K32 = k
    return _K32


#------------------------------------------------------------------
# 스트림 열기(CreateFileW)
#=> 공유는 R/W/D 모두 허용(툴과 같다). 실패하면 GetLastError 를 실은 OSError.
#
# -in: sp          = 스트림 경로
# -in: access      = GENERIC_READ(0x80000000) | GENERIC_WRITE(0x40000000)
# -in: disposition = OPEN_EXISTING(3) | CREATE_ALWAYS(2)
#
# -out: handle
# -out: error = 실패 시 OSError(winerror 에 원본 번호)
#------------------------------------------------------------------
def _open_stream(sp, access, disposition):
    import ctypes
    k = _k32()
    h = k.CreateFileW(sp, access, 0x7, None, disposition, 0x80, None)
    if h is None or h == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return h


#------------------------------------------------------------------
# 스트림 앞부분 읽기
#
# -in: sp = 스트림 경로 · n = 읽을 바이트 수
#
# -out: bytes = 읽은 내용(n 보다 짧을 수 있다)
# -out: error = 열기 · 읽기 실패 시 OSError
#------------------------------------------------------------------
def _read_stream(sp, n):
    import ctypes
    from ctypes import wintypes
    k = _k32()
    h = _open_stream(sp, 0x80000000, 3)
    try:
        buf = ctypes.create_string_buffer(n)
        got = wintypes.DWORD(0)
        if not k.ReadFile(h, buf, n, ctypes.byref(got), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return buf.raw[:got.value]
    finally:
        k.CloseHandle(h)


#------------------------------------------------------------------
# 스트림 통째로 쓰기(CREATE_ALWAYS — 툴의 CommandSet 과 같다)
#
# -in: sp = 스트림 경로 · data = 쓸 바이트
#
# -out: int = 쓴 바이트 수
# -out: error = 열기 · 쓰기 실패 시 OSError
#------------------------------------------------------------------
def _write_stream(sp, data):
    import ctypes
    from ctypes import wintypes
    k = _k32()
    h = _open_stream(sp, 0x40000000, 2)
    try:
        done = wintypes.DWORD(0)
        if not k.WriteFile(h, data, len(data), ctypes.byref(done), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return done.value
    finally:
        k.CloseHandle(h)


#------------------------------------------------------------------
# 오류 번호 → 결과에 적을 reason
#=> 사람이 할 일이 다른 셋(볼륨 · 권한 · 공유)을 갈라 준다. 나머지는 os_error.
#
# -in: code = Windows 오류 번호
#
# -out: str = reason
# -out: error = 예외 없음
#------------------------------------------------------------------
def reason_of(code):
    return {_ERR_INVALID_NAME: "unsupported_volume",
            _ERR_ACCESS_DENIED: "access_denied",
            _ERR_SHARING_VIOLATION: "sharing_violation",
            _ERR_FILE_NOT_FOUND: "not_found",
            _ERR_PATH_NOT_FOUND: "not_found"}.get(code, "os_error")


#------------------------------------------------------------------
# 지금 스트림 상태 읽기
#=> 엔진(NEOGetFileGrade)과 같은 순서로 검사한다 — 우리가 '같다'고 본 것을 엔진도 같다고 보게.
#    1) 열기 실패: 파일 없음=NO_STREAM, 이름 오류=ADS 미지원 볼륨, 그 밖=READ_ERROR
#    2) 12바이트 미만 · 매직 불일치 = FORMAT_ERROR
#    3) 버전이 1..CURRENT 밖 = VERSION_UNSUPPORTED
#    4) 등급 ≥ MAX = FORMAT_ERROR
#    5) 0 이면 UNCLASSIFIED, 아니면 MARKED
#
# -in: path = 문서 경로
#
# -out: (state, code, winerr) = 상태 이름, 등급 숫자(MARKED/UNCLASSIFIED 일 때만), 원본 오류 번호
# -out: error = 예외 없음(열기 실패도 상태로 돌려준다)
#------------------------------------------------------------------
def read_state(path):
    try:
        data = _read_stream(stream_path(path), INFO_SIZE)
    except OSError as e:
        w = _winerr(e)
        # 스트림이 없을 때 Windows 는 '파일 없음'을 돌려준다
        if w == _ERR_FILE_NOT_FOUND:
            return ST_NO_STREAM, None, w
        # FAT32 · exFAT 처럼 ADS 가 없는 볼륨은 "경로:이름" 을 이름 오류로 본다
        if w == _ERR_INVALID_NAME:
            return ST_UNSUPPORTED_VOLUME, None, w
        return ST_READ_ERROR, None, w
    if len(data) < INFO_SIZE:
        return ST_FORMAT_ERROR, None, 0
    magic, ver, code = struct.unpack("<III", data)
    if magic != MAGIC:
        return ST_FORMAT_ERROR, None, 0
    # 우리보다 새 버전은 뒤에 필드가 붙었을 수 있다 — 덮어쓰면 그 필드를 잃는다
    if ver < 1 or ver > VERSION_CURRENT:
        return ST_VERSION_UNSUPPORTED, None, 0
    if code >= GRADE_MAX:
        return ST_FORMAT_ERROR, None, 0
    return (ST_UNCLASSIFIED if code == 0 else ST_MARKED), code, 0


#------------------------------------------------------------------
# 파일 시각 저장 · 복원 (기본 — --grade-stream-update-time 이면 안 한다)
#=> 스트림에 쓰면 NTFS 가 수정 시각을 바꾼다. 툴의 -k 처럼 생성·접근·수정 시각을 그대로
#   되돌린다. os.utime 은 생성 시각을 못 다뤄 Win32 GetFileTime/SetFileTime 을 쓴다.
#------------------------------------------------------------------
class _FileTimes:
    #------------------------------------------------------------------
    # 시각 셋을 읽어 둔다
    #=> FILE_READ_ATTRIBUTES 로만 열어 다른 프로그램을 막지 않는다.
    #
    # -in: path = 문서 경로
    #
    # -out: 없음(self.times 에 FILETIME 셋)
    # -out: error = 실패 시 OSError
    #------------------------------------------------------------------
    def __init__(self, path):
        import ctypes
        from ctypes import wintypes
        self._k = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k.CreateFileW.restype = wintypes.HANDLE
        self._k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                        wintypes.HANDLE]
        self._path = long_path(path)
        self._ft = wintypes.FILETIME
        self.times = [self._ft(), self._ft(), self._ft()]
        h = self._open(0x80)                       # FILE_READ_ATTRIBUTES
        try:
            if not self._k.GetFileTime(h, *[ctypes.byref(t) for t in self.times]):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self._k.CloseHandle(h)

    #------------------------------------------------------------------
    # 시각 다루기용으로 파일 열기
    #=> 공유 R/W/D 모두 허용 — 열려 있는 문서에도 시각만 만지고 나온다.
    #
    # -in: access = 접근 권한(FILE_READ_ATTRIBUTES / FILE_WRITE_ATTRIBUTES)
    #
    # -out: handle = 열린 핸들
    # -out: error = 실패 시 OSError
    #------------------------------------------------------------------
    def _open(self, access):
        import ctypes
        h = self._k.CreateFileW(self._path, access, 0x7, None, 3, 0x02000000, None)
        # INVALID_HANDLE_VALUE(-1) 면 실패
        if h is None or h == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        return h

    #------------------------------------------------------------------
    # 읽어 둔 시각으로 되돌린다
    #=> 스트림 쓰기 핸들을 닫은 뒤에 불러야 한다(닫힐 때 시각이 다시 바뀔 수 있다).
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 실패 시 OSError
    #------------------------------------------------------------------
    def restore(self):
        import ctypes
        h = self._open(0x100)                      # FILE_WRITE_ATTRIBUTES
        try:
            if not self._k.SetFileTime(h, *[ctypes.byref(t) for t in self.times]):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self._k.CloseHandle(h)


#------------------------------------------------------------------
# 한 파일에 등급 쓰기
#=> 설계 §05 절차. 같은 값이면 안 쓰고, 새 버전 스트림은 지키고, 쓴 뒤 다시 읽어 확인한다.
#    1) 지금 상태를 읽는다 — 같은 등급이면 unchanged(수정 시각도 그대로)
#    2) 새 버전이면 skipped(newer_version), ADS 미지원 볼륨이면 바로 failed
#    3) 읽기 전용이면 잠시 풀고, keep_time 이면 시각을 저장
#    4) 12바이트를 쓴다(CREATE_ALWAYS)
#    5) 시각 · 읽기 전용을 되돌린다(실패는 경고만 — 등급은 이미 써졌다)
#    6) 다시 읽어 기대한 값인지 본다(가로채고 성공을 돌려주는 필터 대비)
#
# -in: path      = 문서 경로
# -in: grade     = "C" | "S" | "O" | "NONE"
# -in: keep_time = True 면 시각 보존(기본 True)
#
# -out: dict = {"state": written|unchanged|skipped|failed, "value": 등급, ["prev"], ["reason"], ["winerr"]}
# -out: error = 예외 없음(실패도 dict 로 돌려준다)
#------------------------------------------------------------------
def mark(path, grade, keep_time=True):
    code = GRADE_CODE[grade]
    if not os.path.isfile(path):
        return {"state": "failed", "value": grade, "reason": "not_found",
                "winerr": _ERR_FILE_NOT_FOUND}
    st, cur, w = read_state(path)
    # 이미 같은 값 — 쓰지 않아야 재실행해도 파일이 흔들리지 않는다
    if st in (ST_MARKED, ST_UNCLASSIFIED) and cur == code:
        return {"state": "unchanged", "value": grade}
    if st == ST_VERSION_UNSUPPORTED:
        return {"state": "skipped", "value": grade, "reason": "newer_version"}
    if st == ST_UNSUPPORTED_VOLUME:
        return {"state": "failed", "value": grade, "reason": "unsupported_volume", "winerr": w}
    prev = CODE_GRADE.get(cur) if st in (ST_MARKED, ST_UNCLASSIFIED) else None

    # 읽기 전용 속성을 잠시 푼다(툴과 같다). 끝나면 반드시 되돌린다.
    ro = False
    try:
        ro = bool(os.stat(path).st_file_attributes & stat.FILE_ATTRIBUTE_READONLY)
    except (OSError, AttributeError):
        ro = False
    times = None
    try:
        if keep_time:
            times = _FileTimes(path)
        if ro:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        # 핸들은 _write_stream 안에서 닫힌다 — 시각 복원(finally)보다 먼저다
        n = _write_stream(stream_path(path), payload(code))
        if n != INFO_SIZE:
            return {"state": "failed", "value": grade, "reason": "os_error", "winerr": 0}
    except OSError as e:
        w = _winerr(e)
        return {"state": "failed", "value": grade, "reason": reason_of(w), "winerr": w}
    finally:
        # 되돌리기는 쓰기 핸들이 닫힌 뒤다 — 실패해도 등급 결과는 바꾸지 않는다
        if times is not None:
            try:
                times.restore()
            except OSError as e:
                log.warning("등급스트림 시각 복원 실패 file=%s :: %s", path, e)
        if ro:
            try:
                os.chmod(path, stat.S_IREAD)
            except OSError as e:
                log.warning("등급스트림 읽기전용 복원 실패 file=%s :: %s", path, e)

    # 다시 읽어 확인 — 회사 미니필터가 쓰기를 삼키고 성공을 돌려주는 경우를 잡는다
    st2, cur2, _ = read_state(path)
    if st2 not in (ST_MARKED, ST_UNCLASSIFIED) or cur2 != code:
        return {"state": "failed", "value": grade, "reason": "verify_failed"}
    out = {"state": "written", "value": grade}
    if prev is not None:
        out["prev"] = prev
    return out


#------------------------------------------------------------------
# 사람이 정한 등급인가
#=> 본문을 못 읽은 문서(스캔 PDF 등)도 사람이 등급을 정했으면 그 등급을 파일에 쓴다.
#
# -in: rec = 결과 레코드
#
# -out: bool
# -out: error = 없음
#------------------------------------------------------------------
def _is_manual(rec):
    sec = (rec.get("why") or {}).get("security") or {}
    return sec.get("method") == "manual_override"


#------------------------------------------------------------------
# 분류 실행 하나의 등급 스트림 기록기
#=> 최종 레코드(출력 직전)를 받아 무엇에 쓸지 고르고(설계 §04), 쓰고, 결과를 레코드에 붙인다.
#
# -필드: unclassified = "skip"(기본) | "none" — 등급 없는 문서에 NONE 을 쓸지
# -필드: keep_time    = 수정 시각 보존 여부
# -필드: members      = 압축 안에서 나온 문서의 표시 경로 집합(쓰지 않는다)
# -필드: counts       = written/unchanged/skipped/failed 건수
#------------------------------------------------------------------
class Marker:
    #------------------------------------------------------------------
    # 기록기 만들기
    #=> 옵션과 '압축 내부 문서' 목록을 받아 둔다.
    #
    # -in: unclassified = "skip" | "none"
    # -in: keep_time    = 시각 보존(기본 True)
    # -in: members      = 압축 내부 문서 경로들(표시 경로)
    #
    # -out: 없음
    # -out: error = 예외 없음
    #------------------------------------------------------------------
    def __init__(self, unclassified="skip", keep_time=True, members=()):
        self.unclassified = unclassified
        self.keep_time = keep_time
        self.members = set(members)
        self.counts = {"written": 0, "unchanged": 0, "skipped": 0, "failed": 0}
        self._bad_volume_noted = set()

    #------------------------------------------------------------------
    # 레코드 하나 처리
    #=> 설계 §04 표대로 대상을 고르고 mark() 를 부른 뒤 rec["grade_stream"] 을 단다.
    #    1) 압축 집계 레코드(archive=True) → 압축 파일 자체에 집계 등급
    #    2) 압축 안 문서 → skipped(archive_member)
    #    3) 못 읽은 문서(error · excluded) → skipped(not_read)
    #    4) 압축 미해제 · 등급 없음 → unclassified 옵션대로(skip 또는 NONE)
    #    5) 그 밖 → 최종 등급
    #
    # -in: rec = 출력 직전의 최종 레코드(제자리에서 grade_stream 을 단다)
    #
    # -out: 없음
    # -out: error = 예외 없음
    #------------------------------------------------------------------
    def apply(self, rec):
        from . import record
        path = rec.get("file")
        res = None
        if not path:
            res = {"state": "skipped", "reason": "not_read"}
        elif not rec.get("archive") and path in self.members:
            # 압축 안에는 ADS 가 없다 — 꺼낸 임시본에 써도 의미가 없다
            res = {"state": "skipped", "reason": "archive_member"}
        elif not rec.get("archive") and not rec.get("archive_unexpanded") and not _is_manual(rec) and (
                rec.get("error") or rec.get("excluded")):
            # 내용을 못 본 판정이다 — NONE 옵션이어도 쓰지 않는다
            res = {"state": "skipped", "reason": "not_read"}
        else:
            # 못 펼친 압축은 안을 안 봤으니 등급이 있어 보여도 미분류로 다룬다
            g = None if rec.get("archive_unexpanded") else record.grade_of(rec)
            if g is None and self.unclassified != "none":
                res = {"state": "skipped", "reason": "unclassified"}
            else:
                res = mark(path, g or "NONE", self.keep_time)
        rec["grade_stream"] = res
        self.counts[res["state"]] = self.counts.get(res["state"], 0) + 1
        self._log(path, res)

    #------------------------------------------------------------------
    # 파일당 한 줄 로그
    #=> 실패는 WARNING. ADS 미지원 볼륨은 같은 드라이브에서 한 번만 안내를 덧붙인다.
    #
    # -in: path = 문서 경로
    # -in: res  = mark() 결과
    #
    # -out: 없음
    # -out: error = 예외 없음
    #------------------------------------------------------------------
    def _log(self, path, res):
        extra = " ".join(f"{k}={res[k]}" for k in ("value", "prev", "reason", "winerr") if k in res)
        line = f"등급스트림 state={res['state']} file={path} {extra}".rstrip()
        if res["state"] == "failed":
            log.warning(line)
            if res.get("reason") == "unsupported_volume":
                drive = os.path.splitdrive(os.path.abspath(path or ""))[0]
                if drive not in self._bad_volume_noted:
                    self._bad_volume_noted.add(drive)
                    log.warning("등급스트림: %s 볼륨은 대체 데이터 스트림(ADS)을 지원하지 않습니다"
                                "(FAT32 · exFAT 등) — 이 볼륨의 문서에는 등급을 쓸 수 없습니다", drive)
        else:
            log.info(line)

    #------------------------------------------------------------------
    # 요약에 실을 집계
    #
    # -in: 없음
    #
    # -out: dict = {"written":..,"unchanged":..,"skipped":..,"failed":..}
    # -out: error = 예외 없음
    #------------------------------------------------------------------
    def summary(self):
        return dict(self.counts)
