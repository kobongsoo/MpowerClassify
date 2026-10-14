# -*- coding: utf-8 -*-
"""엠파워 v11 암호화 문서 복호화 연동 — 잠긴 파일을 풀어서 분류하고, 푼 파일은 바로 지운다.

설계: plan/엠파워v11-암호화문서-복호화연동-설계-20261012.html

엠파워 v11 저장소의 문서는 암호화(+압축)된 채 디스크에 놓여 있다. 그대로 읽으면
분류기는 깨진 글자만 본다. 이 모듈은 분류 직전에 엠파워의 removeenc.sh 를 불러
한 건을 임시 폴더에 풀고, 풀린 결과가 진짜 문서인지 확인한 뒤 넘겨준다.

지키는 원칙 세 가지:
  1. 한 번에 한 건만 풀려 있다 — 풀기·분류·지우기를 문서 단위로 묶는다(cli.py 의 루프).
  2. "성공"을 믿지 않는다 — 도구는 틀린 열쇠에도 0 을 돌려주면서 원본과 같은 크기의
     무작위 바이트를 낼 수 있다(엠파워 removeenc.php 주석, 2026-09-29 실측).
     그것을 분류하면 기밀 문서가 낮은 등급으로 기록된다. 앞머리(시그니처)를 본다.
  3. 열쇠와 임시 경로는 흔적을 남기지 않는다 — 로그·결과·오류 어디에도 쓰지 않는다.

[Rust 판과의 관계] Rust/src/decrypt.rs 와 같은 계약이다(같은 옵션 · 같은 사유 이름 ·
같은 오류 번호). 한쪽만 고치면 같은 목록이 두 판에서 다르게 처리된다.
"""

import logging
import os
import shutil
import subprocess
import tempfile

# 복호화 도구의 기본 위치(엠파워 v11 서버). --decrypt-cmd · 환경변수로 바꾼다.
DEFAULT_CMD = "/MOCOMSYS/MpowerScript/removeenc/removeenc.sh"
# 복호화 한 건의 기본 시간 제한(초). 파서 시간 상한(--parser-timeout)과 같은 값이다.
DEFAULT_TIMEOUT_SECS = 60
# removeenc.php 의 ERR_LOAD_EXTENSION — PHP 확장(Mpower 64File)을 못 읽었다.
# 이 서버에서는 어떤 파일도 풀 수 없다는 뜻이라 '환경 문제'로 본다.
RC_LOAD_EXTENSION = 56

# 확장자별 '정상 문서라면 이렇게 시작한다' — 틀린 열쇠로 풀린 무작위 바이트를 가려내는 자.
# 시그니처가 없는 형식(txt·csv 등)은 여기 없다 — 이 검사로는 가릴 수 없다.
_PK = (b"PK", "zip 계열(PK)")
_OLE = (b"\xd0\xcf\x11\xe0", "OLE")
SIGNATURES = {
    "docx": _PK, "xlsx": _PK, "pptx": _PK, "hwpx": _PK, "zip": _PK,
    "docm": _PK, "xlsm": _PK, "pptm": _PK,
    "pdf": (b"%PDF", "PDF"),
    "hwp": _OLE, "doc": _OLE, "xls": _OLE, "ppt": _OLE,
    "7z": (b"\x37\x7a\xbc\xaf", "7z"),
    "rar": (b"Rar!", "rar"),
    "gz": (b"\x1f\x8b", "gzip"), "tgz": (b"\x1f\x8b", "gzip"),
}

# removeenc.php 의 종료코드가 뜻하는 것(자주 보는 것만). 번호는 결과에 그대로 싣고,
# 이 말은 사람이 바로 알아보게 덧붙이는 것이다.
_RC_HINT = {
    4: "원본 파일을 열지 못함", 5: "대상 파일을 열지 못함", 9: "복호화 실패",
    10: "압축 해제 실패", 14: "MRK 문제(KEK 로 감싼 값일 수 있음)",
    56: "PHP 확장 Mpower 64File 을 읽지 못함", 254: "인자 오류",
}


#------------------------------------------------------------------
# 복호화에 필요한 세 값 — 목록 한 줄 또는 명령줄에서 온다
#=> removeenc.sh 의 -k · -e · -z 로 그대로 넘어간다. enc_mode·zip_mode 는 값을
#   해석하지 않고 글자 그대로 들고 다닌다(알고리즘 번호의 뜻은 엠파워가 안다).
#   다만 "둘 다 0"만은 본다 — 엠파워도 그때는 복호화하지 않고 복사만 한다.
#
#   [왜 __repr__ 을 직접 쓰나] 기본 표현을 쓰면 디버그 출력·예외 문구에 열쇠가 찍힌다.
#   열쇠는 로그에도 결과에도 남기지 않는다(설계 §8).
#
# -필드: mrk      = 평문 열쇠(둘 다 0 인 파일은 빈 문자열일 수 있다)
# -필드: enc_mode = ENC_MODE(글자)
# -필드: zip_mode = ZIP_MODE(글자)
#------------------------------------------------------------------
class DecInfo:
    __slots__ = ("mrk", "enc_mode", "zip_mode")

    def __init__(self, mrk, enc_mode, zip_mode):
        self.mrk = mrk
        self.enc_mode = enc_mode
        self.zip_mode = zip_mode

    #--------------------------------------------------------------
    # 암호화도 저장 시 압축도 안 된 파일인가
    #=> ENC_MODE=0 · ZIP_MODE=0. 엠파워 MCrypt::mpowerDecrypt 와 같은 판정이다 —
    #   이때는 도구를 부르지 않는다.
    #
    # -in: 없음
    #
    # -out: bool = 둘 다 0 이면 True
    # -out: error = 예외 없음
    #--------------------------------------------------------------
    def is_plain(self):
        return _is_zero(self.enc_mode) and _is_zero(self.zip_mode)

    def __repr__(self):
        return f"DecInfo(mrk=****, enc_mode={self.enc_mode}, zip_mode={self.zip_mode})"


#------------------------------------------------------------------
# 복호화 실패 한 건
#=> 왜 못 풀었는지를 레코드의 error 칸에 그대로 옮길 수 있게 담는다.
#
# -필드: reason  = 사유 이름(removeenc_exit · timeout · empty_output · size_mismatch ·
#                  signature_mismatch · key_wrapped · info_incomplete · copy_failed ·
#                  spawn_failed · size_limit)
# -필드: rc      = removeenc 종료코드(부르지 않았으면 None)
# -필드: message = 사람이 읽는 문장(열쇠·임시 경로는 들어가지 않는다)
# -필드: fatal   = True 면 '환경 문제' — 다음 문서도 똑같이 실패하므로 실행을 멈춘다
#------------------------------------------------------------------
class DecError(Exception):
    def __init__(self, reason, message, rc=None, fatal=False):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.rc = rc
        self.fatal = fatal


#------------------------------------------------------------------
# 풀린 문서 한 건 — 임시 폴더와 그 안의 평문 파일
#=> 문서마다 폴더를 하나씩 따로 쓴다. 압축파일이면 그 폴더 안에 내부 문서가
#   펼쳐지므로, 폴더째 지우면 그 문서에서 나온 평문이 한 번에 사라진다.
#
# -필드: dir  = 이 문서 전용 임시 폴더(끝나면 통째로 지운다)
# -필드: file = 풀린 평문 파일(dir 안)
# -필드: tool = 복호화 도구를 실제로 불렀는가(둘 다 0 이라 복사만 했으면 False)
#------------------------------------------------------------------
class Decrypted:
    __slots__ = ("dir", "file", "tool")

    def __init__(self, dir_, file_, tool):
        self.dir = dir_
        self.file = file_
        self.tool = tool


#------------------------------------------------------------------
# 숫자 0 을 뜻하는 글자인가
#=> "0" · "00" · " 0 " 은 0 이다. 빈 값은 0 이 아니다(칸이 없는 것과 값이 0 인 것은 다르다).
#
# -in: v = 모드 값(글자)
#
# -out: bool
# -out: error = 예외 없음
#------------------------------------------------------------------
def _is_zero(v):
    t = str(v).strip()
    return bool(t) and all(c == "0" for c in t)


#------------------------------------------------------------------
# 열쇠 세 칸 → 복호화 정보 (목록 한 줄 · 명령줄 공용)
#=> 세 칸의 있고 없음으로 그 문서를 어떻게 다룰지 정한다.
#    1) 셋 다 없다            → None      : 그대로 읽는 문서(지금까지와 같다)
#    2) 모드 둘이 모두 0      → DecInfo   : 암호화 안 된 파일. mrk 는 없어도 된다
#    3) 셋 다 있다            → DecInfo   : 복호화 대상
#    4) 일부만 있다           → DecError(info_incomplete)
#    5) mrk 가 'v2:' 로 시작  → DecError(key_wrapped) — KEK 로 감싼 값이라 엠파워가 벗겨야 한다
#
# -in: mrk · enc_mode · zip_mode = 각 칸의 값(없으면 None. 숫자여도 된다)
#
# -out: DecInfo 또는 None = 위 1)~3)
# -out: error = 위 4)·5) 는 DecError 를 던진다
#------------------------------------------------------------------
def parse_dec_info(mrk, enc_mode, zip_mode):
    # 숫자(jsonl 의 3)도 글자("3")도 받는다. None·빈 문자열은 '없는 칸'이다.
    def clean(v):
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    m, e, z = clean(mrk), clean(enc_mode), clean(zip_mode)
    if m is None and e is None and z is None:
        return None
    if e is None or z is None:
        lack = [n for n, v in (("enc_mode", e), ("zip_mode", z)) if v is None]
        raise DecError("info_incomplete",
                       f"복호화 정보가 일부만 있습니다(없는 칸: {', '.join(lack)}) — "
                       f"mrk · enc_mode · zip_mode 는 함께 줘야 합니다")
    info = DecInfo(m or "", e, z)
    if info.is_plain():
        return info
    if not info.mrk:
        raise DecError("info_incomplete",
                       "복호화 정보가 일부만 있습니다(없는 칸: mrk) — "
                       "암호화·압축된 파일은 열쇠가 있어야 풉니다")
    if info.mrk.startswith("v2:"):
        raise DecError("key_wrapped",
                       "mrk 가 KEK 로 감싼 값(v2:)입니다 — 엠파워 쪽에서 "
                       "MCrypt::decryptMRKWithKEK 로 벗긴 평문을 줘야 합니다")
    return info


#------------------------------------------------------------------
# 복호화 도구 위치 정하기
#=> 우선순위: --decrypt-cmd > 환경변수 CSOCLASSIFY_DECRYPT_CMD > 기본값.
#
# -in: opt = --decrypt-cmd 로 받은 값(없으면 None)
#
# -out: str = 쓸 도구 경로(있는지는 여기서 보지 않는다 — preflight 가 본다)
# -out: error = 예외 없음
#------------------------------------------------------------------
def resolve_cmd(opt):
    if opt and str(opt).strip():
        return str(opt)
    env = os.environ.get("CSOCLASSIFY_DECRYPT_CMD", "").strip()
    return env or DEFAULT_CMD


#------------------------------------------------------------------
# 임시 폴더의 기준 위치 정하기
#=> 우선순위: --decrypt-tmp > 환경변수 CSOCLASSIFY_DECRYPT_TMP > 시스템 임시 폴더.
#   메모리 디스크(/dev/shm)를 주면 풀린 원문이 물리 디스크에 닿지 않는다.
#
# -in: opt = --decrypt-tmp 로 받은 값(없으면 None)
#
# -out: str = 기준 폴더(이 아래에 cso_dec_<pid> 를 만든다)
# -out: error = 예외 없음
#------------------------------------------------------------------
def resolve_tmp_base(opt):
    if opt and str(opt).strip():
        return str(opt)
    env = os.environ.get("CSOCLASSIFY_DECRYPT_TMP", "").strip()
    return env or tempfile.gettempdir()


#------------------------------------------------------------------
# 도구가 있고 실행할 수 있는가 — 문서를 읽기 전에 한 번 본다
#=> 없으면 1만 건이 전부 같은 이유로 실패한 결과 파일이 만들어진다. 그보다는
#   한 건도 읽지 않고 끝내는 편이 낫다(설계 §5).
#
# -in: cmd = 복호화 도구 경로
#
# -out: None = 쓸 수 있다 / str = 못 쓰는 사유(사람이 읽는 문장)
# -out: error = 예외 없음
#------------------------------------------------------------------
def preflight(cmd):
    if not os.path.isfile(cmd):
        return (f"복호화 도구를 찾을 수 없습니다: {cmd}\n"
                f"  --decrypt-cmd 또는 환경변수 CSOCLASSIFY_DECRYPT_CMD 로 위치를 알려 주세요 "
                f"(엠파워 v11 서버의 기본 위치는 {DEFAULT_CMD}).")
    # 윈도우는 실행 비트가 없어 os.access 가 늘 참이다 — 리눅스에서만 뜻이 있다.
    if os.name != "nt" and not os.access(cmd, os.X_OK):
        return f"복호화 도구에 실행 권한이 없습니다: {cmd}"
    return None


#------------------------------------------------------------------
# 로그에 남길 명령줄에서 열쇠 가리기
#=> 일반 로그는 실행 명령을 통째로 적는다. --mrk 뒤의 값은 **** 로 바꿔 적는다.
#   `--mrk=값` 모양도 함께 가린다.
#
# -in: args = 명령줄 인자들(list[str])
#
# -out: list[str] = 열쇠 자리가 가려진 사본
# -out: error = 예외 없음
#------------------------------------------------------------------
def mask_args(args):
    out, hide_next = [], False
    for a in args:
        if hide_next:
            out.append("****")
            hide_next = False
        elif a == "--mrk":
            out.append(a)
            hide_next = True
        elif isinstance(a, str) and a.startswith("--mrk="):
            out.append("--mrk=****")
        else:
            out.append(a)
    return out


#------------------------------------------------------------------
# 이름에서 확장자(소문자) 떼기
#=> 영문·숫자 1~8자일 때만 확장자로 본다 — 임시 파일 이름에 그대로 붙이므로
#   특수문자가 섞이면 안 된다.
#
# -in: name = 파일 이름 또는 경로
#
# -out: str 또는 None = 안전한 확장자(없으면 None)
# -out: error = 예외 없음
#------------------------------------------------------------------
def safe_ext(name):
    if not name:
        return None
    base = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in base:
        return None
    stem, ext = base.rsplit(".", 1)
    if not stem or not ext or len(ext) > 8 or not (ext.isascii() and ext.isalnum()):
        return None
    return ext.lower()


#------------------------------------------------------------------
# 풀린 결과가 진짜 문서인가 — "성공"을 한 번 더 확인한다
#=> 도구가 0 을 돌려줘도 아래 셋을 본다(설계 §6).
#    1) 파일이 있고 0바이트가 아니다
#    2) 원래 크기(size)를 받았으면 크기가 같다
#    3) 앞머리(시그니처)가 확장자와 맞는다 — 틀린 열쇠를 잡는 것은 이 검사다
#       (무작위 바이트도 원본과 크기는 같아서 2)로는 못 잡는다)
#
# -in: file        = 풀린 파일
# -in: name        = 원래 파일 이름(확장자를 본다. 없으면 3)을 건너뛴다)
# -in: size        = 원래 크기(없으면 2)를 건너뛴다)
# -in: rc          = 도구 종료코드(오류 레코드에 실을 값)
# -in: allow_empty = 원본이 0바이트라 결과도 0바이트가 정상이면 True
#
# -out: 없음(믿을 수 있으면 그냥 돌아온다)
# -out: error = DecError — empty_output · size_mismatch · signature_mismatch
#------------------------------------------------------------------
def verify(file, name=None, size=None, rc=None, allow_empty=False):
    if not os.path.isfile(file):
        raise DecError("empty_output", "복호화 도구가 결과 파일을 만들지 않았습니다", rc)
    length = os.path.getsize(file)
    if length == 0 and not allow_empty:
        raise DecError("empty_output", "복호화 결과가 0바이트입니다", rc)
    if size is not None and int(size) != length:
        raise DecError("size_mismatch",
                       f"복호화 결과의 크기가 원래 크기와 다릅니다"
                       f"(원래 {size}바이트 · 결과 {length}바이트)", rc)
    if length == 0:
        return
    ext = safe_ext(name)
    sig = SIGNATURES.get(ext) if ext else None
    if sig is not None:
        magic, kind = sig
        with open(file, "rb") as f:
            head = f.read(len(magic))
        if head != magic:
            raise DecError("signature_mismatch",
                           f"{ext} 파일인데 앞머리가 {kind} 형식이 아닙니다 — "
                           f"열쇠(mrk)와 모드가 맞는지 확인하세요", rc)


#------------------------------------------------------------------
# 주인 프로세스가 아직 살아 있는가
#=> 리눅스에서만 안다(/proc). 다른 OS 에서는 모른다고 답해(=살아 있다) 남의 폴더를
#   지우지 않는다.
#
# -in: pid = 프로세스 번호
#
# -out: bool
# -out: error = 예외 없음
#------------------------------------------------------------------
def _pid_alive(pid):
    if os.path.isdir("/proc"):
        return os.path.exists(f"/proc/{pid}")
    return True


#------------------------------------------------------------------
# 자식 프로세스를 손자까지 한꺼번에 끝내기
#=> removeenc.sh 는 bash 가 php 를 띄운다. bash 만 죽이면 php 가 남아 계속 돌고,
#   출력 파이프를 붙잡고 있어 부른 쪽이 php 가 끝날 때까지 기다리게 된다.
#    1) 리눅스: 자식을 새 세션으로 띄웠으므로 그 무리(pgid)에 SIGKILL 을 보낸다
#    2) 윈도우: taskkill /T 로 자식과 그 아래를 모두 끝낸다
#    3) 어느 쪽이든 마지막에 자식 자체도 한 번 더 죽인다(위가 실패했을 때의 안전망)
#
# -in: proc = subprocess.Popen(새 무리로 띄운 것)
#
# -out: 없음
# -out: error = 예외 없음(이미 끝난 프로세스여도 조용히 넘어간다)
#------------------------------------------------------------------
def _kill_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


#------------------------------------------------------------------
# 도구 출력에서 사람이 읽을 첫 줄 고르기
#=> removeenc.sh 가 끝에 찍는 '종료코드 숫자만 있는 줄'과 사용법 줄은 건너뛴다.
#
# -in: stdout · stderr = 도구가 남긴 출력(bytes)
#
# -out: str = 첫 줄(없으면 빈 문자열, 200자까지)
# -out: error = 예외 없음
#------------------------------------------------------------------
def _first_message_line(stdout, stderr):
    for buf in (stderr, stdout):
        for line in (buf or b"").decode("utf-8", "replace").splitlines():
            t = line.strip()
            if not t or t.isdigit() or t.startswith("[Usage]"):
                continue
            return t[:200]
    return ""


#------------------------------------------------------------------
# 로그에서 임시 경로 가리기 (logging 필터)
#=> 추출기들은 "어느 파일을 읽었나"를 로그에 적는데, 풀린 문서는 그 경로가 임시 경로다.
#   임시 경로는 곧 지워지는 경로이고 임시 폴더 구조가 새어 나가므로, 로그에 적기 전에
#   원래 경로로 바꿔 적는다(설계 §8 "임시 경로는 결과·로그 어디에도 쓰지 않는다").
#    1) 풀린 파일 경로 → 원래 경로
#    2) 그 문서 전용 폴더(압축에서 펼친 내부 문서) → 원래 경로
#    3) 그래도 남은 임시 뿌리 → "<복호화 임시>"
#
# -필드: sess = 임시 경로와 원래 경로의 짝을 쥔 Session
#------------------------------------------------------------------
class _TmpPathFilter(logging.Filter):
    def __init__(self, sess):
        super().__init__()
        self.sess = sess

    #--------------------------------------------------------------
    # 로그 한 줄 손보기
    #
    # -in: record = logging.LogRecord(제자리에서 고친다)
    #
    # -out: True(언제나 통과 — 지우지 않고 바꿔 적기만 한다)
    # -out: error = 예외 없음(실패하면 원래 줄 그대로 둔다)
    #--------------------------------------------------------------
    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        root = self.sess.root
        if root not in msg and root.replace("\\", "/") not in msg:
            return True
        for tmp, label in list(self.sess.labels.items()):
            for t in (tmp, tmp.replace("\\", "/")):
                msg = msg.replace(t, label)
        for t in (root, root.replace("\\", "/")):
            msg = msg.replace(t, "<복호화 임시>")
        record.msg = msg
        record.args = None
        return True


#------------------------------------------------------------------
# 복호화 작업장 — 도구 위치·임시 폴더·시간 제한을 쥐고 문서를 하나씩 푼다
#
# -필드: root    = 임시 폴더(<기준>/cso_dec_<pid>). 실행이 끝나면 통째로 지운다
# -필드: cmd     = 복호화 도구 경로
# -필드: timeout = 한 건의 시간 제한(초). 0 이면 제한 없음
# -필드: seq     = 문서 일련번호(임시 폴더·파일 이름에 쓴다)
# -필드: labels  = {임시 경로: 원래 경로} — 로그에서 임시 경로를 가릴 때 쓴다
#------------------------------------------------------------------
class Session:
    #--------------------------------------------------------------
    # 작업장 열기
    #=> 임시 폴더를 만들고, 강제 종료(kill -9)로 남은 옛 폴더를 치운다.
    #    1) 주인 프로세스가 죽은 cso_dec_* 폴더를 찾아 지운다(리눅스)
    #    2) 기준 폴더 아래에 cso_dec_<pid> 를 만든다(유닉스는 0700)
    #
    # -in: cmd          = 복호화 도구 경로
    # -in: tmp_base     = 임시 폴더의 기준 위치
    # -in: timeout_secs = 한 건의 시간 제한(초, 기본 60). 0 이면 제한 없음
    #
    # -out: 없음
    # -out: error = 임시 폴더를 못 만들면 DecError(fatal=True)
    #--------------------------------------------------------------
    def __init__(self, cmd, tmp_base, timeout_secs=DEFAULT_TIMEOUT_SECS):
        self.cmd = cmd
        self.timeout = timeout_secs
        self.seq = 0
        self.labels = {}
        self._masked = []     # 필터를 붙여 둔 로그 핸들러들(close 때 뗀다)
        self._filter = None
        me = os.getpid()
        # 강제 종료 뒤에는 지울 기회가 없었다 — 다음 실행이 대신 치운다(설계 §8).
        try:
            for n in os.listdir(tmp_base):
                if n.startswith("cso_dec_") and n[8:].isdigit():
                    pid = int(n[8:])
                    if pid != me and not _pid_alive(pid):
                        shutil.rmtree(os.path.join(tmp_base, n), ignore_errors=True)
        except OSError:
            pass
        self.root = os.path.join(tmp_base, f"cso_dec_{me}")
        shutil.rmtree(self.root, ignore_errors=True)
        try:
            # 같은 서버의 다른 계정이 풀린 원문을 읽지 못하게 0700 으로 만든다.
            os.makedirs(self.root, mode=0o700, exist_ok=True)
        except OSError as e:
            raise DecError("tmp_failed",
                           f"복호화 임시 폴더를 만들 수 없습니다: {self.root} ({e})", fatal=True)

    #--------------------------------------------------------------
    # 문서 한 건 풀기
    #=> 저장 파일을 이 문서 전용 임시 폴더에 평문으로 만든다.
    #    1) 폴더 <root>/<일련번호>/ 를 만들고, 파일 이름은 <일련번호>.<확장자> 로 한다
    #       (원래 이름을 쓰지 않는다 — 이름 자체가 민감할 수 있고 특수문자로 경로가 깨진다)
    #    2) 둘 다 0 이거나 원본이 0바이트면 복사만 한다(엠파워 mpowerDecrypt 와 같다)
    #    3) 아니면 도구를 부르고, 종료코드와 결과를 확인한다
    #    4) 실패하면 만든 폴더를 지우고 사유를 던진다
    #
    # -in: src  = 저장된(암호화된) 파일
    # -in: info = 열쇠와 모드(DecInfo)
    # -in: name = 원래 파일 이름(확장자 판별·시그니처 검사용. 없어도 된다)
    # -in: size = 원래 크기(없어도 된다)
    #
    # -out: Decrypted = 풀린 파일. 다 쓰면 release() 로 지운다
    # -out: error = DecError — fatal=True 면 환경 문제라 실행을 멈춰야 한다
    #--------------------------------------------------------------
    def decrypt(self, src, info, name=None, size=None):
        self.seq += 1
        d = os.path.join(self.root, f"{self.seq:06d}")
        try:
            os.makedirs(d, mode=0o700, exist_ok=True)
        except OSError as e:
            raise DecError("tmp_failed", f"복호화 임시 폴더를 만들 수 없습니다({e})", fatal=True)
        # 확장자는 원래 이름에서, 없으면 저장 경로에서 가져온다.
        ext = safe_ext(name) or safe_ext(src)
        file = os.path.join(d, f"{self.seq:06d}.{ext}" if ext else f"{self.seq:06d}")
        try:
            src_len = os.path.getsize(src)
        except OSError:
            src_len = 0
        try:
            if info.is_plain() or src_len == 0:
                # 암호화도 압축도 안 된 파일(또는 빈 파일) — 도구를 부르지 않는다.
                # removeenc.php 는 -k 가 비면 인자 오류(254)로 끝나므로, 부르면
                # 멀쩡한 파일이 실패로 기록된다(설계 §12 확인 결과 ①).
                try:
                    shutil.copyfile(src, file)
                except OSError as e:
                    raise DecError("copy_failed", f"저장 파일을 읽지 못했습니다({e})")
                verify(file, name, size, None, allow_empty=(src_len == 0))
                tool = False
            else:
                rc = self._run_tool(src, file, info)
                verify(file, name, size, rc)
                tool = True
        except DecError:
            # 실패한 결과물(무작위 바이트일 수 있다)을 남기지 않는다.
            shutil.rmtree(d, ignore_errors=True)
            raise
        try:
            os.chmod(file, 0o600)
        except OSError:
            pass
        return Decrypted(d, file, tool)

    #--------------------------------------------------------------
    # 복호화 도구 실행
    #=> removeenc.sh -s <원본> -t <대상> -k <열쇠> -e <ENC_MODE> -z <ZIP_MODE>
    #    1) 셸을 거치지 않고 인자를 낱개로 넘긴다 — 파일 이름의 공백·따옴표·$( ) 가
    #       명령으로 해석될 길이 없다
    #    2) 자식의 stdout·stderr 를 가로챈다 — removeenc.sh 가 종료코드 숫자를 화면에도
    #       찍는데, 그대로 흘리면 우리 결과 JSON 에 섞인다
    #    3) 시간 제한을 넘으면 자식을 '무리째' 죽인다 — removeenc.sh 는 bash 가 php 를
    #       띄우는 구조라, bash 만 죽이면 php 가 살아남아 출력 파이프를 붙잡고 있다
    #    4) 성공 여부는 프로세스 종료코드로만 판단한다
    #
    # -in: src  = 저장 파일
    # -in: tgt  = 평문을 쓸 임시 파일
    # -in: info = 열쇠와 모드
    #
    # -out: 0 = 도구가 성공이라고 답했다(결과 확인은 verify 가 한다)
    # -out: error = DecError — spawn_failed(환경) · timeout · removeenc_exit
    #--------------------------------------------------------------
    def _run_tool(self, src, tgt, info):
        argv = [self.cmd, "-s", src, "-t", tgt, "-k", info.mrk,
                "-e", str(info.enc_mode), "-z", str(info.zip_mode)]
        # 자식을 새 프로세스 무리로 띄운다 — 시간 초과 때 손자(php)까지 한 번에 끝내려는 것이다.
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, **kw)
        except OSError as e:
            # 도구 자체를 못 띄웠다 — 다음 문서도 똑같이 실패한다.
            raise DecError("spawn_failed",
                           f"복호화 도구를 실행하지 못했습니다: {self.cmd} ({e})", fatal=True)
        try:
            out, err = proc.communicate(
                timeout=(self.timeout if self.timeout and self.timeout > 0 else None))
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                # 무리를 죽였으면 파이프가 곧 닫힌다. 그래도 안 닫히면 더 기다리지 않는다.
                proc.communicate(timeout=5)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass
            raise DecError("timeout",
                           f"복호화가 시간 제한({self.timeout}초)을 넘어 중단했습니다")
        rc = proc.returncode
        if rc == 0:
            return 0
        # 도구가 남긴 말의 첫 줄을 사유에 붙인다. removeenc 는 열쇠를 출력하지 않지만,
        # 혹시 모를 경우를 위해 열쇠 글자가 들어 있으면 가린다.
        said = _first_message_line(out, err)
        if info.mrk:
            said = said.replace(info.mrk, "****")
        hint = _RC_HINT.get(rc, "")
        msg = f"복호화 도구가 실패했습니다(종료코드 {rc}{' · ' + hint if hint else ''})"
        if said:
            msg += f" — {said}"
        raise DecError("removeenc_exit", msg, rc, fatal=(rc == RC_LOAD_EXTENSION))

    #--------------------------------------------------------------
    # 풀린 문서의 임시 경로와 원래 경로 짝 등록
    #=> 로그 필터가 임시 경로를 원래 경로로 바꿔 적을 수 있게 한다.
    #   파일을 먼저, 폴더를 나중에 넣는다(긴 경로부터 바뀌어야 한다 — dict 는 넣은 차례를 지킨다).
    #
    # -in: dec   = Decrypted
    # -in: label = 원래 경로(목록의 path)
    #
    # -out: 없음
    # -out: error = 예외 없음
    #--------------------------------------------------------------
    def remember(self, dec, label):
        self.labels[dec.file] = label
        self.labels[dec.dir] = label

    #--------------------------------------------------------------
    # 로그에 임시 경로가 나가지 않게 필터 붙이기
    #
    # -in: logger = 핸들러가 달린 로거(이 프로그램은 "csoclassify")
    #
    # -out: 없음
    # -out: error = 예외 없음
    #--------------------------------------------------------------
    def attach_log_mask(self, logger):
        self._filter = _TmpPathFilter(self)
        for h in list(logger.handlers):
            h.addFilter(self._filter)
            self._masked.append(h)

    #--------------------------------------------------------------
    # 어떤 경로가 '풀린 파일'인가
    #=> 로그·진행 표시에 임시 경로를 쓰지 않으려고 가린다.
    #
    # -in: path = 살펴볼 경로
    #
    # -out: bool = 임시 폴더 아래의 경로면 True
    # -out: error = 예외 없음
    #--------------------------------------------------------------
    def owns(self, path):
        try:
            root = os.path.normcase(os.path.abspath(self.root))
            p = os.path.normcase(os.path.abspath(path))
        except (TypeError, ValueError):
            return False
        return p == root or p.startswith(root + os.sep)

    #--------------------------------------------------------------
    # 다 쓴 문서 치우기
    #=> 그 문서 전용 폴더를 통째로 지운다(압축에서 펼친 내부 문서 포함).
    #
    # -in: d = Decrypted.dir
    #
    # -out: 없음
    # -out: error = 예외 없음(못 지우면 close 가 폴더째 한 번 더 지운다)
    #--------------------------------------------------------------
    def release(self, d):
        # 뿌리 밖의 경로는 건드리지 않는다 — 실수로 남의 폴더를 지우지 않게.
        if d and self.owns(d) and os.path.abspath(d) != os.path.abspath(self.root):
            shutil.rmtree(d, ignore_errors=True)
            # 지운 문서의 경로 짝도 잊는다(수만 건을 돌려도 표가 커지지 않게).
            for k in [k for k in self.labels if k == d or k.startswith(d + os.sep)]:
                self.labels.pop(k, None)

    #--------------------------------------------------------------
    # 작업장 닫기
    #=> 임시 폴더를 통째로 지운다. 여러 번 불러도 된다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 예외 없음
    #--------------------------------------------------------------
    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)
        for h in self._masked:
            h.removeFilter(self._filter)
        self._masked = []
