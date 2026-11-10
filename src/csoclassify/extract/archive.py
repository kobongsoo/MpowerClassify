#------------------------------------------------------------------
# 압축파일 확장기 — 아카이브 1개를 내부 파일 여러 개로 펼치기
#=> 사용자가 압축파일 1개를 검사하면, 예전엔 압축 전체 텍스트가 '합본 1건'으로 분류돼
#   어느 내부 파일이 민감한지 알 수 없었다. 여기서는 압축을 임시폴더에 풀어 내부
#   '파일 하나하나'를 분류 대상으로 만들어, 파일별로 C/S/O 를 낼 수 있게 한다.
#   또 각 내부 파일은 어떤 최상위 압축(origin)에서 나왔는지 함께 돌려줘, 상위(run_classify)
#   가 '압축파일 자체'의 집계 등급(내부 최고 위험)을 매길 수 있게 한다.
#
#   지원 포맷(내용 기반 감지 — 확장자 위장도 잡음):
#     · zip                              → 표준 zipfile (docx/xlsx/pptx/hwpx 는 '문서 1개'라 제외)
#     · tar / tar.gz / tar.bz2 / tar.xz  → 표준 tarfile (무의존)
#     · gz / bz2 / xz (단일 파일 압축)   → 표준 gzip/bz2/lzma (무의존)
#     · 7z                               → py7zr (순수 파이썬 계열, CentOS7 wheel 확인됨)
#     · rar                              → rarfile + 번들 unrar 바이너리(독점 포맷)
#   중첩 압축(zip 안 7z 등)도 max_depth 까지 재귀로 펼친다.
#   각 내부 파일은 (읽기용 임시경로 src, 표시·신호용 가상경로 label, 최상위압축 origin)
#   3-튜플로 돌려준다. label 은 "<압축경로>/<내부경로>" 라 파일명/경로 규칙 신호가 그대로 먹힌다.
#------------------------------------------------------------------

import bz2
import gzip
import lzma
import os
import re
import sys
import tarfile
import zipfile

from .. import config
from ..logsetup import get_logger
from .base import human_bytes
from .detect import detect_format

log = get_logger(__name__)

# 잡음 항목(분류 대상 아님): macOS 메타.
_NOISE_PREFIX = "__MACOSX/"
_NOISE_BASENAMES = {".DS_Store"}


#------------------------------------------------------------------
# 압축 해제 예산 초과 예외 (G4)
#=> "이 압축은 풀다 보니 너무 커졌다"를 알리는 내부 신호다. expand_paths 의
#   except 가 이걸 받아 그 압축을 '펼치지 않은 원본 1건'으로 되돌린다
#   (라이브러리 없음·손상 압축과 같은 폴백 경로를 그대로 탄다).
#
# -필드: (메시지 문자열만 사용)
#------------------------------------------------------------------
class ArchiveLimitError(Exception):
    pass


#------------------------------------------------------------------
# 압축 해제 예산 (G4) — 설계: plan/문서크기-상한-설계-20260904.html
#=> 압축 하나를 푸는 동안 "누적 몇 바이트, 몇 개까지"를 세면서 상한을 넘으면
#   즉시 멈추게 한다.
#
#   [왜 필요한가] expand_paths 의 max_depth 는 '깊이'만 막는다. 그래서 깊이가 1인
#   평범한 zip 하나로도 디스크를 가득 채울 수 있었다(폭 방향 압축폭탄). 깊이와
#   폭은 서로 다른 축이라 한쪽 방어로 다른 쪽을 막을 수 없다.
#   [왜 읽기 전에 세나] zip·tar·rar 는 헤더에 '풀었을 때 크기'가 적혀 있다. 그
#   값을 먼저 더해 보면 실제로 풀지 않고도 폭탄을 알아챌 수 있다 — 다 푼 뒤에
#   재는 것은 이미 늦다.
#
# -필드: max_bytes   = 누적 해제 바이트 상한(0 이하면 무제한)
# -필드: max_members = 내부 파일 개수 상한(0 이하면 무제한)
# -필드: used_bytes  = 지금까지 더한 바이트
# -필드: used_members= 지금까지 센 파일 수
#------------------------------------------------------------------
class _Budget:
    #------------------------------------------------------------------
    # 생성자 — 이번 압축에 쓸 예산 잡기
    #=> 값을 안 주면 config 기본값을 쓴다(환경변수가 이미 반영된 값).
    #
    # -in: max_bytes   = 누적 바이트 상한(None 이면 config.MAX_ARCHIVE_BYTES)
    # -in: max_members = 개수 상한(None 이면 config.MAX_ARCHIVE_MEMBERS)
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def __init__(self, max_bytes=None, max_members=None):
        self.max_bytes = config.MAX_ARCHIVE_BYTES if max_bytes is None else max_bytes
        self.max_members = config.MAX_ARCHIVE_MEMBERS if max_members is None else max_members
        self.used_bytes = 0
        self.used_members = 0

    #------------------------------------------------------------------
    # 항목 하나를 예산에 더한다
    #=> 파일 하나를 풀기 직전에 부른다. 더한 결과가 상한을 넘으면 예외를 던져
    #   해제를 그 자리에서 멈춘다.
    #
    # -in: nbytes = 이 항목의 (풀었을 때) 크기. 모르면 0
    #
    # -out: 없음
    # -out: error = 상한 초과 시 ArchiveLimitError
    #------------------------------------------------------------------
    def take(self, nbytes):
        self.used_members += 1
        # 개수 먼저 — 0바이트 파일 수백만 개짜리 폭탄은 바이트로는 안 걸린다.
        if self.max_members and 0 < self.max_members < self.used_members:
            raise ArchiveLimitError(
                f"압축 내부 파일 개수 상한 초과({self.max_members}개, MAX_ARCHIVE_MEMBERS)")
        self.used_bytes += max(0, int(nbytes or 0))
        if self.max_bytes and 0 < self.max_bytes < self.used_bytes:
            raise ArchiveLimitError(
                f"압축 해제 누적 상한 초과({human_bytes(self.used_bytes)} > "
                f"{human_bytes(self.max_bytes)}, MAX_ARCHIVE_BYTES)")


#------------------------------------------------------------------
# zip 내부 항목 이름 복원(한글 깨짐 방지)
#=> 옛 zip 은 파일명을 cp437 로 저장하는데, 파이썬 zipfile 은 UTF-8 플래그가 없으면
#   cp437 로 디코드해 한글이 깨진다. 원래 한글(cp949/euc-kr)로 되돌린다.
#
# -in: info = zipfile.ZipInfo (내부 항목 메타)
#
# -out: str = 사람이 읽는 내부 경로(복원 실패 시 원본 그대로)
# -out: error = 없음(항상 문자열 반환)
#------------------------------------------------------------------
def _zip_entry_name(info):
    # UTF-8 플래그(0x800)가 있으면 이미 올바른 유니코드다.
    if info.flag_bits & 0x800:
        return info.filename
    try:
        # zipfile 이 cp437 로 잘못 디코드한 것을 바이트로 되돌린 뒤 cp949 로 재해석.
        return info.filename.encode("cp437").decode("cp949")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


#------------------------------------------------------------------
# 분할 zip 의 '마지막 조각'인지 — 파일 속을 들여다본다
#=> 분할 zip 은 앞 조각이 .z01·.z02 … 이고 마지막 조각만 평범한 .zip 이다.
#   그래서 이름 규칙만으로는 마지막 조각을 정상 zip 과 구분할 수 없다.
#   zip 은 파일 끝에 EOCD(End Of Central Directory) 라는 꼬리표를 두는데,
#   여기 '이 디스크 번호'가 0 이 아니면 여러 장으로 나뉜 zip 이라는 뜻이다.
#   실측: 4조각짜리 01_제품매뉴얼.zip 의 EOCD 가 disk=3 이었다.
#
#   [왜 이름이 아니라 내용인가] 마지막 조각은 이름이 그냥 .zip 이고 머리도 PK 매직이
#   아니어서(중간 데이터로 시작) 압축 감지에도 안 잡힌다. 그대로 두면 "하이브리드
#   전엔진 실패" 라는 엉뚱한 사유가 붙어, 사람은 파일이 깨진 줄 알고 원본을 뒤진다.
#
# -in: path = 실제로 읽을 수 있는 파일 경로
#
# -out: bool = 분할 zip 의 조각이면 True
# -out: error = 없음(읽기 실패·형식 불일치는 모두 False)
#------------------------------------------------------------------
def _zip_is_split_volume(path):
    import struct
    try:
        size = os.path.getsize(path)
        # EOCD 는 파일 끝에 있다. 주석이 최대 64KB 라 그만큼만 뒤에서 읽으면 충분하다.
        want = min(size, 65557 + 22)
        with open(path, "rb") as f:
            f.seek(size - want)
            tail = f.read(want)
    except Exception:   # noqa: BLE001  읽기 실패는 '모르겠다'로 두고 넘어간다
        return False
    i = tail.rfind(b"PK\x05\x06")
    if i < 0 or i + 12 > len(tail):
        return False
    this_disk, cd_disk = struct.unpack("<HH", tail[i + 4:i + 8])
    # 0xFFFF 는 "zip64 를 보라"는 표식이다 — 그것도 여러 장이라는 뜻이므로 같이 친다.
    return this_disk != 0 or cd_disk != 0


#------------------------------------------------------------------
# 분할 압축(멀티볼륨)의 조각인지 — 파일 이름으로 본다
#=> 분할 압축은 조각 하나만으로는 아무것도 꺼낼 수 없다. 이 도구는 조각을 모아
#   잇지 않으므로(zip·tar·7z 모두 라이브러리 차원에서 미지원), 그 사실을 결과에
#   분명히 남기려고 이름 규칙으로 알아본다.
#
#   [왜 이름으로 보나] 내용으로는 구분이 안 된다. 첫 조각은 정상 압축과 똑같은
#   매직으로 시작하고, 두 번째 이후 조각은 매직이 없어 '그냥 이진 파일'로 보인다.
#   실측에서 5조각짜리 7z 의 세 번째 조각이 'text' 로 감지돼, 뜻 없는 이진 쓰레기가
#   본문으로 분류될 뻔했다. 이름이 유일한 신호다 — 딱 하나, 분할 zip 의 마지막
#   조각만은 이름이 평범한 .zip 이라 내용(EOCD 디스크 번호)을 봐야 한다
#   (위 _zip_is_split_volume).
#
#   [오탐을 줄이려고] 숫자 확장자는 앞에 압축 확장자(7z·zip·tar·rar·tgz)가 붙은
#   경우만 인정한다. 'backup.001' 같은 일반 파일을 조각으로 오해하지 않으려는 것이다.
#
# -in: name = 파일 경로 또는 이름
# -in: path = 실제로 읽을 수 있는 경로(주면 zip 마지막 조각까지 내용으로 가려낸다).
#             압축 내부 항목처럼 실체가 없는 대상이면 None 을 준다
#
# -out: str | None = 조각으로 보이면 사람이 읽을 설명, 아니면 None
# -out: error = 없음
#------------------------------------------------------------------
def split_volume_hint(name, path=None):
    # --extractor rust 면 압축 판단도 Rust(docparse)가 한다 — 본문 파서와 한 스위치다.
    if config.EXTRACTOR == "rust":
        from . import rust_engine
        return rust_engine.split_volume_hint(name, path)
    base = os.path.basename(str(name or "")).lower()
    # 7z·zip·tar 분할: 이름.7z.001 · 이름.zip.002 · 이름.tar.gz.0001 …
    if re.search(r"\.(7z|zip|tar|tgz|tar\.gz|tar\.bz2|tar\.xz|rar)\.\d{2,}$", base):
        return "7z·zip·tar 분할 조각(.001 …)"
    # zip 분할의 앞 조각들: 이름.z01 · z02 … (마지막 조각만 .zip 이다)
    if re.search(r"\.z\d{2}$", base):
        return "zip 분할 조각(.z01 …)"
    # rar 신형 분할: 이름.part1.rar · part01.rar …
    if re.search(r"\.part\d+\.rar$", base):
        return "rar 분할 조각(.partN.rar)"
    # rar 구형 분할: 이름.r00 · r01 … (첫 조각만 .rar 이다)
    if re.search(r"\.r\d{2}$", base):
        return "rar 분할 조각(.r00 …)"
    # 여기까지 이름으로 못 걸렀는데 .zip 이면 마지막 조각일 수 있다 —
    # 이것만은 내용(EOCD 디스크 번호)을 봐야 정상 zip 과 구분된다.
    if path and base.endswith(".zip") and _zip_is_split_volume(path):
        return "zip 분할 마지막 조각(.zip)"
    return None


#------------------------------------------------------------------
# 잡음 내부 항목인지
#=> macOS 메타(__MACOSX/…, .DS_Store)는 분류 대상이 아니므로 거른다.
#
# -in: name = 내부 경로 문자열
# -out: bool = 잡음이면 True
# -out: error = 없음
#------------------------------------------------------------------
def _is_noise(name):
    return name.startswith(_NOISE_PREFIX) or os.path.basename(name) in _NOISE_BASENAMES


#------------------------------------------------------------------
# 압축 종류 감지(내용 기반)
#=> 확장자가 아니라 파일 앞부분의 매직 바이트/구조로 압축 종류를 판별한다.
#    · 7z/rar 는 고유 매직, zip 은 PK 매직(단 docx 등 '문서 zip'은 detect 로 제외)
#    · tar 계열(tar/tar.gz/tar.bz2/tar.xz)은 tarfile.is_tarfile 로 판별
#    · 그 외 단일 압축(gz/bz2/xz)은 각 매직으로 판별
#
# -in: path = 검사할 파일 경로
#
# -out: str|None = 'zip'|'tar'|'7z'|'rar'|'gz'|'bz2'|'xz' 또는 None(압축 아님)
# -out: error = 없음(읽기 실패 시 None)
#------------------------------------------------------------------
def _archive_type(path):
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError:
        return None
    # 7z: '7z BC AF 27 1C'
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    # rar: 'Rar!\x1a\x07\x00'(v4) / 'Rar!\x1a\x07\x01\x00'(v5)
    if head[:7] == b"Rar!\x1a\x07\x00" or head[:8] == b"Rar!\x1a\x07\x01\x00":
        return "rar"
    # zip 계열(PK): docx/xlsx/pptx/hwpx 는 '문서 1개'라 펼치지 않는다(detect 로 구분).
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip" if detect_format(path) == "zip" else None
    # tar 계열(압축 tar 포함): 이름과 무관하게 실제 구조로 판별.
    try:
        if tarfile.is_tarfile(path):
            return "tar"
    except OSError:
        pass
    # 단일 파일 압축(tar 가 아닐 때만 여기 도달).
    if head[:2] == b"\x1f\x8b":
        return "gz"
    if head[:3] == b"BZh":
        return "bz2"
    if head[:6] == b"\xfd7zXZ\x00":
        return "xz"
    return None


#------------------------------------------------------------------
# 일반 압축 여부(내용 기반)
#=> 위 감지가 압축 종류를 돌려주면 아카이브다. (문서 zip 은 False)
#
# -in: path = 검사할 파일 경로
# -out: bool = 확장 대상 압축이면 True
# -out: error = 없음
#------------------------------------------------------------------
def is_archive(path):
    return _archive_type(path) is not None


#------------------------------------------------------------------
# 임시파일에 바이트 저장(내부 헬퍼)
#=> 내부 파일 내용을 tmp_root 아래 '일련번호' 이름으로 평평하게 쓴다. detect 는 내용
#   기반이라 임시 파일명이 무엇이든 상관없다(경로 탈출·한글깨짐 걱정 없음).
#
# -in: tmp_root = 임시 루트폴더
# -in: seqbox   = [정수] 형태의 가변 카운터(호출 간 증가)
# -in: data     = 저장할 바이트
#
# -out: str = 만들어진 임시파일 경로
# -out: error = 파일 쓰기 실패 시 OSError 전파
#------------------------------------------------------------------
def _spill(tmp_root, seqbox, data):
    seqbox[0] += 1
    p = os.path.join(tmp_root, "a%d" % seqbox[0])
    with open(p, "wb") as f:
        f.write(data)
    return p


#------------------------------------------------------------------
# 번들 unrar 실행파일 경로 탐색 + rarfile 설정(내부 헬퍼)
#=> rar 는 독점 포맷이라 해제에 외부 unrar 바이너리가 필요하다. 배포에 함께 넣은
#   unrar(리눅스)/unrar.exe(윈도우)를 찾아 rarfile 에 지정한다. 못 찾으면 rarfile 이
#   시스템(unrar/bsdtar 등)을 자동 탐색하게 둔다.
#
# -in: 없음(실행 위치 기준으로 후보 폴더를 훑음)
# -out: 없음(rarfile.UNRAR_TOOL 설정 시도)
# -out: error = 없음(못 찾으면 기본값 유지)
#------------------------------------------------------------------
def _configure_unrar(rarfile_mod):
    exe = "unrar.exe" if os.name == "nt" else "unrar"
    # 후보: PyInstaller 번들(_MEIPASS) → 실행파일 옆 → 이 모듈 옆.
    cands = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(os.path.join(meipass, exe))
    cands.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), exe))
    cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), exe))
    for c in cands:
        if os.path.isfile(c):
            if os.name != "nt":
                # 리눅스는 실행권한이 없을 수 있으니 보정.
                try:
                    os.chmod(c, 0o755)
                except OSError:
                    pass
                # 번들 unrar 은 최신 libstdc++/libgcc(CXXABI)를 요구하는데, 구형 배포판
                # (CentOS7)의 시스템 libstdc++ 는 그걸 못 준다. 우리 배포에 이미 넣어둔
                # 최신 libstdc++.so.6(_MEIPASS/실행파일 옆)를 unrar 가 쓰도록 LD_LIBRARY_PATH
                # 에 그 폴더를 앞세운다(자식 프로세스가 os.environ 을 상속받는다).
                libdir = os.path.dirname(c)
                prev = os.environ.get("LD_LIBRARY_PATH", "")
                if libdir not in prev.split(os.pathsep):
                    os.environ["LD_LIBRARY_PATH"] = (
                        libdir + (os.pathsep + prev if prev else ""))
            rarfile_mod.UNRAR_TOOL = c
            return


#------------------------------------------------------------------
# 단일 파일 압축의 내부 이름 만들기(내부 헬퍼)
#=> gz/bz2/xz 는 파일 1개를 감싼 것이라 내부 이름이 없다. 표시용으로 압축 확장자를
#   떼어 이름을 만든다(예: "report.txt.gz" → "report.txt").
#
# -in: label = 압축의 표시 경로
# -in: atype = 'gz'|'bz2'|'xz'
# -out: str = 내부 파일 표시 이름
# -out: error = 없음
#------------------------------------------------------------------
def _single_inner_name(label, atype):
    base = os.path.basename(label)
    exts = {"gz": (".gz", ".gzip"), "bz2": (".bz2", ".bz"), "xz": (".xz", ".lzma")}
    for e in exts.get(atype, ()):
        if base.lower().endswith(e):
            return base[: -len(e)]
    # 확장자를 못 떼면 원본 이름 뒤에 표식을 붙여 원본과 구분한다.
    return base + ".out"


#------------------------------------------------------------------
# 아카이브 1개 → 내부 (임시경로, 내부이름) 목록 추출(내부 헬퍼)
#=> 종류별로 알맞은 라이브러리로 내부 파일들을 임시폴더에 풀고, (임시경로, 내부이름)
#   목록을 만든다. 폴더 항목·잡음은 거른다.
#
# -in: src      = 아카이브 실제 경로
# -in: atype    = _archive_type 결과
# -in: label    = 표시용 경로(단일 압축 내부이름 계산에 사용)
# -in: tmp_root = 임시 루트폴더
# -in: seqbox   = 가변 카운터
# -in: budget   = 해제 예산(_Budget). None 이면 이 압축 전용으로 새로 만든다.
#                 중첩 압축은 최상위 압축의 예산을 물려받아야 합계가 상한을 지킨다.
#
# -out: list[tuple(str,str)] = (임시경로, 내부이름) 목록
# -out: error = 라이브러리 없음/손상 등은 예외 전파(호출부가 폴백 처리).
#               해제 총량·개수 상한을 넘으면 ArchiveLimitError(G4)
#------------------------------------------------------------------
def _extract_members(src, atype, label, tmp_root, seqbox, budget=None):
    out = []
    # 예산(G4)을 안 받았으면 이 압축 하나짜리 예산을 새로 만든다. 중첩 압축은
    # 호출부가 같은 예산을 물려줘야 '전체 합계'가 상한을 지킨다.
    budget = _Budget() if budget is None else budget

    if atype == "zip":
        with zipfile.ZipFile(src) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                name = _zip_entry_name(info)
                if _is_noise(name):
                    continue
                # zip 헤더의 file_size 는 '풀었을 때 크기'다. 읽기 전에 예산에 넣어
                # 압축폭탄을 실제로 풀기 전에 잡아낸다.
                budget.take(info.file_size)
                out.append((_spill(tmp_root, seqbox, z.read(info)), name))
        return out

    if atype == "tar":
        # r:* = 압축 종류(gz/bz2/xz/무압축) 자동 판별.
        with tarfile.open(src, "r:*") as t:
            for m in t.getmembers():
                if not m.isfile():
                    continue
                if _is_noise(m.name):
                    continue
                # tar 헤더에도 원본 크기가 있어 읽기 전에 셀 수 있다.
                budget.take(m.size)
                f = t.extractfile(m)
                if f is None:
                    continue
                out.append((_spill(tmp_root, seqbox, f.read()), m.name))
        return out

    if atype in ("gz", "bz2", "xz"):
        # 단일 파일 압축: 통째로 풀어 파일 하나로.
        # 이 세 포맷만은 헤더에 원본 크기가 없어 '읽어 봐야' 안다. 그래서 상한
        # +1 바이트까지만 읽어 보고, 거기까지 찼으면 폭탄으로 판정한다(전부 읽고
        # 나서 재면 이미 메모리를 다 쓴 뒤다).
        opener = {"gz": gzip.open, "bz2": bz2.open, "xz": lzma.open}[atype]
        cap = budget.max_bytes - budget.used_bytes if budget.max_bytes else 0
        with opener(src, "rb") as f:
            data = f.read(cap + 1) if cap > 0 else f.read()
        if cap > 0 and len(data) > cap:
            raise ArchiveLimitError(
                f"단일 압축 해제 누적 상한 초과(>{human_bytes(budget.max_bytes)}, "
                "MAX_ARCHIVE_BYTES)")
        budget.take(len(data))
        out.append((_spill(tmp_root, seqbox, data), _single_inner_name(label, atype)))
        return out

    if atype == "7z":
        import py7zr   # 지연 import(미설치면 상위가 폴백)
        seqbox[0] += 1
        sub = os.path.join(tmp_root, "s7_%d" % seqbox[0])
        os.makedirs(sub, exist_ok=True)
        with py7zr.SevenZipFile(src, "r") as z:
            # 7z 는 항목별로 풀 수 없어 extractall 로 한 번에 푼다. 그래서 풀기
            # '전에' 목록의 원본 크기 합계를 먼저 확인해야 한다 — 풀고 나서 재면
            # 이미 디스크를 다 쓴 뒤다.
            try:
                for info in z.list():
                    budget.take(getattr(info, "uncompressed", 0) or 0)
            except ArchiveLimitError:
                raise
            except Exception:   # noqa: BLE001  목록 조회 실패는 해제 자체를 막지 않는다
                pass
            z.extractall(path=sub)
        # 풀린 실제 파일들을 걷는다(임시경로=실제경로, 내부이름=상대경로).
        for root, _dirs, fnames in os.walk(sub):
            for fn in fnames:
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, sub).replace(os.sep, "/")
                if _is_noise(rel):
                    continue
                out.append((full, rel))
        return out

    if atype == "rar":
        import rarfile   # 지연 import(미설치면 상위가 폴백)
        _configure_unrar(rarfile)
        with rarfile.RarFile(src) as rf:
            for info in rf.infolist():
                if info.isdir():
                    continue
                name = info.filename
                if _is_noise(name):
                    continue
                # rar 헤더에도 원본 크기가 있어 읽기 전에 셀 수 있다.
                budget.take(getattr(info, "file_size", 0) or 0)
                out.append((_spill(tmp_root, seqbox, rf.read(info)), name))
        return out

    return out


#------------------------------------------------------------------
# 경로 목록을 '분류 작업' 목록으로 확장
#=> 입력 경로들을 훑어, 압축은 임시폴더에 풀어 내부 파일들로 치환하고, 그 외 일반
#   파일은 그대로 둔다. 반환값의 각 원소는 (src, label, origin) 3-튜플이다.
#    1) 큐에 (src, label, depth, origin) 로 넣고 하나씩 처리
#    2) src 가 압축이고 depth<max_depth 면 내부 파일을 임시폴더로 풀어 재귀로 큐에 추가
#       (내부 파일의 origin 은 '최상위 압축' 경로로 물려줘, 압축 자체 등급 집계에 쓴다)
#    3) 압축이 아니면 (src, label, origin) 작업으로 확정
#   ※ 라이브러리 없음/손상/빈 압축 등은 '펼치지 않고' 원본 1건으로 둔다(상위가 폴백 처리).
#
# -in: paths    = 입력 파일 경로 리스트(collect_files 결과)
# -in: tmp_root = 내부 파일을 풀 임시폴더(호출부가 만들고 끝나면 지운다)
# -in: max_depth= 중첩 압축을 펼칠 최대 깊이(기본 3; 압축폭탄 무한재귀 방지)
# -in: stats    = 결과 보고용 dict(선택). 주면 해제 상한(G4)에 걸려 펼치지 못한
#                 압축을 stats["unexpanded"] 에 [{path, reason}] 로 담아 준다.
#
#   [--extractor rust] config.EXTRACTOR 가 "rust" 면 이 파이썬 해제 본체 대신 Rust
#   (docparse.expand_paths)로 펼친다. 반환 모양은 같으므로 부르는 쪽은 그대로다.
#   설계: plan/Python-문서파서-Rust연동-설계-20261110.html §4-4
#
# -out: list[tuple(str,str,str|None)] = (읽기용 실제경로, 표시·신호용 경로, 최상위압축경로 또는 None)
# -out: error = 없음(개별 압축 실패는 그 압축만 원본 1건으로 폴백)
#------------------------------------------------------------------
def expand_paths(paths, tmp_root, *, max_depth=3, stats=None):
    # 본문 파서와 같은 스위치로 압축 해제기도 고른다(rust = docparse, py = 아래 본체).
    if config.EXTRACTOR == "rust":
        from . import rust_engine
        return rust_engine.expand_paths(paths, tmp_root, max_depth=max_depth, stats=stats, log=log)
    jobs = []
    # 상한에 걸려 펼치지 못한 압축을 여기 모아 호출부에 알린다. 로그만 남기면
    # 결과를 보는 사람에게 안 닿는다 — 내부 문서 수백 건이 통째로 빠졌는데
    # 요약에는 "압축 1건 처리"로만 보이는 것이 지금까지의 문제였다.
    if stats is not None:
        stats.setdefault("unexpanded", [])
    # (src=읽을 실제경로, label=표시/신호용 경로, depth=중첩깊이, origin=최상위압축경로|None,
    #  budget=해제 예산|None) — 예산은 최상위 압축에서 만들어 자식에게 물려준다.
    queue = [(p, p, 0, None, None) for p in paths]
    seqbox = [0]   # 임시파일 이름 충돌 방지용 가변 카운터

    while queue:
        src, label, depth, origin, budget = queue.pop(0)

        # 너무 깊으면(중첩 폭탄 방지) 더는 안 펼치고 한 건으로 둔다.
        atype = None if depth >= max_depth else _archive_type(src)
        if atype is None:
            jobs.append((src, label, origin))
            continue

        # 예산(G4)은 '최상위 압축 1건'을 단위로 잡는다. 중첩 압축이 예산을 새로
        # 받으면 zip 안에 zip 을 겹치는 것만으로 상한을 무한히 늘릴 수 있다.
        if budget is None:
            budget = _Budget()

        try:
            members = _extract_members(src, atype, label, tmp_root, seqbox, budget)
        except ArchiveLimitError as e:
            # 상한 초과는 '손상'과 다르다 — 정상 압축인데 너무 큰 것이므로, 왜
            # 안 펼쳤는지 로그로 분명히 남긴다(조용히 1건으로 줄면 추적이 안 된다).
            log.warning("압축 해제 상한 초과 → 펼치지 않음 file=%s :: %s", label, e)
            if stats is not None:
                stats["unexpanded"].append(
                    {"path": label, "reason": str(e), "kind": "limit"})
            jobs.append((src, label, origin))
            continue
        except Exception as e:   # noqa: BLE001  라이브러리 없음/손상/분할볼륨 등 → 폴백
            # [2026-09-07] 예전에는 여기서 조용히 폴백만 했다. 그러면 압축 안의
            # 문서 수십·수백 건이 통째로 빠졌는데 결과에는 '못 읽은 파일 1건'으로만
            # 보여, 무엇이 왜 사라졌는지 알 방법이 없었다(실측: 분할 7z 를 넣으면
            # 감지는 7z 로 정확히 되는데 stats 가 비어 아무 신호도 안 남았다).
            # 상한 초과와 같은 자리에 담되 kind 로 갈라, 안내 문구가 엉뚱한 처방
            # ("상한을 올리세요")을 내놓지 않게 한다.
            log.warning("압축 확장 실패 → 펼치지 않음 file=%s :: %s: %s",
                        label, type(e).__name__, e)
            if stats is not None:
                # 이름이 분할 조각을 가리키면 그렇게 말해 준다. "Bad7zFile: invalid
                # header data" 만 남기면 사람은 파일이 깨진 줄 알고 원본을 뒤진다 —
                # 실제로는 멀쩡한 압축인데 이 도구가 조각을 못 잇는 것뿐이다.
                hint = split_volume_hint(label, src)
                if hint:
                    kind = "split_volume"
                    reason = (f"분할 압축의 조각으로 보입니다({hint}) — 이 도구는 분할 "
                              f"압축을 잇지 않아 내부 문서를 꺼내지 못했습니다. "
                              f"원인: {type(e).__name__}: {e}")
                else:
                    kind = "error"
                    reason = f"{type(e).__name__}: {e}"
                stats["unexpanded"].append(
                    {"path": label, "reason": reason, "kind": kind})
            jobs.append((src, label, origin))
            continue

        # 빈 압축(또는 전부 폴더/잡음)이면 원본 1건으로 남긴다.
        if not members:
            jobs.append((src, label, origin))
            continue

        # 내부 파일의 origin 은 '최상위 압축' 경로. (이미 있으면 물려주고, 없으면 이 압축이 최상위)
        child_origin = origin if origin is not None else label
        for tmp_path, inner_name in members:
            # 자식은 같은 예산 객체를 그대로 물려받는다(합계가 상한을 지키게).
            queue.append((tmp_path, label + "/" + inner_name, depth + 1, child_origin, budget))

    return jobs
