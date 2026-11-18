#------------------------------------------------------------------
# 데몬 레지스트리 & 싱글턴 잠금 (설계서 §5-3)
#=> "지금 이 모델의 데몬이 어디(host:port)에 떠 있는가"를 파일로 기록/조회하고,
#   같은 모델 데몬이 두 개 뜨는 경쟁 조건을 원자적 잠금파일로 막는다.
#   모델별로 파일을 분리(daemon-<model>.json)해 모델마다 별도 데몬을 둔다.
#------------------------------------------------------------------

import json
import os


#------------------------------------------------------------------
# 상태 저장 폴더 경로
#=> 레지스트리/잠금 파일을 둘 사용자별 폴더를 정해 만든다(Rust 판 registry::state_dir 와 같은 순서).
#    1) 환경변수 CSOCLASSIFY_STATE_DIR — 그 폴더를 그대로 쓴다
#       (서버를 띄운 계정과 부르는 계정이 다를 때 한곳으로 모으거나, 시험이 자기 데몬을 따로 둘 때)
#    2) 윈도우면 %LOCALAPPDATA%\MpowerClassify
#    3) 그 외/미설정이면 홈 아래 .csoclassify
#
# -in: 없음
#
# -out: dir = 상태 폴더 절대경로(없으면 생성)
# -out: error = 없음(생성 실패는 상위에서 드러남)
#------------------------------------------------------------------
def state_dir():
    # 직접 정한 폴더가 있으면 그것을 쓴다 — Rust 판과 같은 이름이라 두 판이 같은 데몬 기록을 본다
    forced = os.environ.get("CSOCLASSIFY_STATE_DIR")
    if forced:
        os.makedirs(forced, exist_ok=True)
        return forced
    base = os.environ.get("LOCALAPPDATA")
    if base:
        d = os.path.join(base, "MpowerClassify")
    else:
        d = os.path.join(os.path.expanduser("~"), ".csoclassify")
    os.makedirs(d, exist_ok=True)
    return d


#------------------------------------------------------------------
# 모델별 레지스트리 파일 경로
#=> daemon-<model>.json 경로를 만든다. 모델 별칭을 파일명에 그대로 쓰되
#   안전하지 않은 문자는 '_' 로 바꾼다.
#
# -in: model = 모델 별칭(예: "e5-small-ko")
#
# -out: path = 레지스트리 json 절대경로
# -out: error = 없음
#------------------------------------------------------------------
def record_path(model):
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in model)
    return os.path.join(state_dir(), f"daemon-{safe}.json")


#------------------------------------------------------------------
# 모델별 잠금 파일 경로
#=> record_path 와 짝이 되는 .lock 경로. 이 파일의 원자적 생성으로 싱글턴을 만든다.
#
# -in: model = 모델 별칭
#
# -out: path = 잠금파일 절대경로
# -out: error = 없음
#------------------------------------------------------------------
def lock_path(model):
    return record_path(model) + ".lock"


#------------------------------------------------------------------
# 프로세스 생존 확인
#=> 주어진 pid 프로세스가 아직 살아 있는지 OS 별 방법으로 확인한다(stale 판별용).
#    1) 윈도우면 OpenProcess 로 핸들 열림 여부 확인
#    2) 그 외면 os.kill(pid, 0) 의 예외로 판별
#
# -in: pid = 확인할 프로세스 id(정수)
#
# -out: bool = 살아 있으면 True
# -out: error = 없음(모든 예외를 False/True 로 흡수)
#------------------------------------------------------------------
def is_pid_alive(pid):
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        # SYNCHRONIZE(0x00100000) 권한으로 프로세스 핸들을 열어 본다.
        PROCESS_QUERY = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY, False, int(pid))
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    # POSIX: 신호 0 은 존재/권한만 확인. ESRCH 면 없음.
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


#------------------------------------------------------------------
# 레지스트리 기록 읽기
#=> 모델 데몬 접속정보(dict)를 파일에서 읽어 온다. 파일이 없거나 깨졌으면 None.
#
# -in: model = 모델 별칭
#
# -out: dict = {pid, host, port, token, version, model, ...} 또는 None
# -out: error = 없음(읽기 실패는 None 반환)
#------------------------------------------------------------------
def read_record(model):
    p = record_path(model)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


#------------------------------------------------------------------
# 레지스트리 기록 쓰기(원자적 교체)
#=> 임시파일에 쓰고 os.replace 로 바꿔, 읽는 쪽이 반쪽짜리 파일을 보지 않게 한다.
#
# -in: model  = 모델 별칭
# -in: record = 저장할 dict
#
# -out: 없음
# -out: error = 쓰기 실패 시 OSError 전파
#------------------------------------------------------------------
def write_record(model, record):
    p = record_path(model)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False)
    # 교체는 원자적이라 동시 읽기와 충돌하지 않는다.
    os.replace(tmp, p)


#------------------------------------------------------------------
# 레지스트리/잠금 정리
#=> 데몬 종료나 stale 감지 시 기록/잠금 파일을 지운다(없어도 무시).
#
# -in: model = 모델 별칭
#
# -out: 없음
# -out: error = 없음(삭제 실패 무시)
#------------------------------------------------------------------
def clear(model):
    for p in (record_path(model), lock_path(model)):
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass


#------------------------------------------------------------------
# 싱글턴 잠금 획득(원자적) — 핵심
#=> 잠금파일을 O_CREAT|O_EXCL 로 만든다. 성공하면 "내가 유일한 데몬". 이미 있으면
#   기록된 pid 가 살아있는지 보고, 죽었으면(stale) 정리 후 한 번 더 시도한다.
#    1) O_EXCL 생성 성공 → 내 pid 기록 후 True
#    2) 이미 존재 + pid 살아있음 → 다른 데몬이 주인 → False
#    3) 이미 존재 + pid 죽음(stale) → 정리 후 재시도
#
# -in: model = 모델 별칭
#
# -out: bool = 잠금을 얻어 데몬이 되어도 되면 True, 아니면 False
# -out: error = 없음(경쟁은 False 로 표현)
#------------------------------------------------------------------
def acquire_singleton(model):
    lp = lock_path(model)
    for _ in range(2):  # stale 정리 후 최대 1회 재시도
        try:
            # O_EXCL: 파일이 이미 있으면 실패 → 원자적 "먼저 만든 자 승리".
            fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            return True
        except FileExistsError:
            # 잠금은 있는데, 실제 데몬이 살아있는지 기록으로 확인한다.
            rec = read_record(model)
            if rec and is_pid_alive(rec.get("pid")):
                return False  # 진짜로 다른 데몬이 돌고 있음
            # 기록이 없거나 죽은 pid → stale 로 보고 정리 후 재시도.
            clear(model)
            continue
    return False
