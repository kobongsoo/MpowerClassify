#------------------------------------------------------------------
# 리소스 경로 해석 (개발 실행 vs PyInstaller exe 통합)
#=> snf_exe.exe, 모델 파일처럼 "코드 밖 데이터 파일"의 실제 위치를 찾아 준다.
#   PyInstaller 로 묶으면 파일들이 임시폴더(sys._MEIPASS)에 풀리므로, 개발 때
#   경로와 exe 때 경로가 달라진다. 이 차이를 이 모듈 한 곳에서 흡수한다.
#------------------------------------------------------------------

import os
import sys


#------------------------------------------------------------------
# 리소스 기준 폴더 계산
#=> resources/ 폴더가 실제로 어디 있는지 반환한다.
#    1) PyInstaller exe 로 실행 중이면 sys._MEIPASS(번들 해제 폴더)
#    2) 아니면 소스 트리의 프로젝트 루트(= 이 파일 기준 두 단계 위)
#
# -in: 없음
#
# -out: base = resources 상위 기준 폴더 절대경로
# -out: error = 없음 (항상 경로 문자열 반환)
#------------------------------------------------------------------
def _base_dir():
    # PyInstaller 는 번들을 임시폴더에 풀고 그 경로를 sys._MEIPASS 에 넣어 둔다.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return meipass
    # 소스 실행: 이 파일은 <root>/src/csoclassify/resources.py → 루트는 3단계 위.
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", ".."))


#------------------------------------------------------------------
# resources 하위 임의 경로 조합
#=> 기준 폴더 + "resources" + 넘긴 조각들을 이어 절대경로를 만든다.
#
# -in: parts = 경로 조각들 (예: "models", "e5-small-ko")
#
# -out: path = 조합된 절대경로 (존재 여부는 확인하지 않음)
# -out: error = 없음
#------------------------------------------------------------------
def resource_path(*parts):
    return os.path.join(_base_dir(), "resources", *parts)


#------------------------------------------------------------------
# 사이냅 폴더 후보 목록(우선순위)
#=> snf_exe.exe/snf_win.dll 이 있을 만한 위치를 우선순위대로 만든다. onefile 에서
#   번들 바이너리는 임시(_MEI)폴더에 있어 상주 데몬에서 사라질 수 있으므로, exe 옆
#   같은 "안정 경로" 를 먼저 본다. onedir/소스는 마지막 후보(_MEIPASS/소스)로 커버.
#    1) 환경변수 CSOCLASSIFY_SYNAP_DIR
#    2) exe 옆 synap/ , exe 옆 resources/synap/
#    3) 번들/소스 기준 resources/synap
#
# -in: 없음
#
# -out: list = 후보 폴더 경로들(존재 여부는 확인 안 함)
# -out: error = 없음
#------------------------------------------------------------------
def _synap_dir_candidates():
    cands = []
    env = os.environ.get("CSOCLASSIFY_SYNAP_DIR")
    if env:
        cands.append(env)
    ed = exe_dir()
    cands.append(os.path.join(ed, "synap"))
    cands.append(os.path.join(ed, "resources", "synap"))
    cands.append(resource_path("synap"))
    return cands


#------------------------------------------------------------------
# 사이냅 실행파일 이름 (플랫폼별)
#=> 사이냅 필터 실행파일은 OS 마다 이름이 다르다. Windows 는 'snf_exe.exe',
#   Linux 등 그 외는 확장자 없는 'snf_exe' 다. 같은 제품(v4.29)의 CLI(-U8 등)는
#   동일하므로 이름만 갈라 주면 subprocess 호출 코드는 그대로 공유된다.
#
# -in: 없음
#
# -out: name = 현재 OS 에 맞는 사이냅 실행파일 이름
# -out: error = 없음
#------------------------------------------------------------------
def synap_exe_name():
    return "snf_exe.exe" if os.name == "nt" else "snf_exe"


#------------------------------------------------------------------
# 사이냅 OS별 하위폴더 이름
#=> 사이냅 바이너리를 OS 별 하위폴더로 나눠 둔다: Windows=synap/windows/,
#   Linux=synap/linux/. 한 배포 트리에 양쪽 바이너리를 함께 담아도 서로 안 섞인다.
#
# -in: 없음
#
# -out: name = 현재 OS 의 하위폴더 이름("windows" 또는 "linux")
# -out: error = 없음
#------------------------------------------------------------------
def synap_os_subdir():
    return "windows" if os.name == "nt" else "linux"


# --synap-path 로 준 사이냅 실행파일(있으면 찾기 순서를 건너뛴다). set_synap_override 가 채운다.
_SYNAP_OVERRIDE = None


#------------------------------------------------------------------
# 사이냅 실행파일을 직접 정하기(--synap-path)
#=> 명령줄로 준 경로를 이번 실행 동안 쓴다. 이 값이 있으면 synap_exe_path 가 찾기를
#   건너뛰고 그대로 돌려준다(Rust 판 synap::find_snf 의 explicit 과 같은 뜻).
#
# -in: path = snf_exe 파일 경로(None 이면 지운다)
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def set_synap_override(path):
    global _SYNAP_OVERRIDE
    _SYNAP_OVERRIDE = path or None


#------------------------------------------------------------------
# 사이냅을 찾아본 곳 목록
#=> 못 찾았을 때 오류 문장(1016 synap_missing)에 그대로 싣는다 — 어디에 두면 되는지 바로 안다.
#
# -in: 없음
#
# -out: list[str] = 찾아본 실행파일 경로들(찾는 차례대로)
# -out: error = 없음
#------------------------------------------------------------------
def synap_search_paths():
    if _SYNAP_OVERRIDE:
        return [_SYNAP_OVERRIDE]
    name, sub = synap_exe_name(), synap_os_subdir()
    out = []
    for d in _synap_dir_candidates():
        out += [os.path.join(d, sub, name), os.path.join(d, name)]
    return out


#------------------------------------------------------------------
# 사이냅 실행파일 경로
#=> 텍스트 추출에 쓸 사이냅 실행파일(Windows=snf_exe.exe / Linux=snf_exe)을 후보
#   폴더에서 찾는다. onefile 상주 데몬에서도 사라지지 않도록 exe 옆(안정 경로)을 우선.
#   --synap-path 로 직접 줬으면(set_synap_override) 그 경로를 그대로 쓴다.
#
# -in: 없음
#
# -out: path = 발견한 실행파일 절대경로(없으면 기본 경로 — 호출부에서 존재 확인)
# -out: error = 없음
#------------------------------------------------------------------
def synap_exe_path():
    # 직접 준 경로가 이긴다 — 있든 없든 그것을 돌려주고, 없으면 부른 쪽이 1016 으로 알린다.
    if _SYNAP_OVERRIDE:
        return _SYNAP_OVERRIDE
    name = synap_exe_name()
    sub = synap_os_subdir()
    for d in _synap_dir_candidates():
        # OS 하위폴더(synap/windows|linux/) 우선, 없으면 평면(synap/) 배치도 허용(하위호환).
        for p in (os.path.join(d, sub, name), os.path.join(d, name)):
            if os.path.isfile(p):
                return p
    # 못 찾으면 기본(번들/소스) 경로 반환 → SynapExeExtractor 가 명확한 에러를 낸다.
    return resource_path("synap", sub, name)


#------------------------------------------------------------------
# 모델 폴더 경로(번들/소스 기준)
#=> _MEIPASS(번들) 또는 소스 트리 기준의 resources/models/<local_dir> 경로.
#   onedir 빌드나 소스 실행에서 모델을 이 위치에서 찾는다.
#
# -in: local_dir = 모델 폴더명 (config.ModelSpec.local_dir)
#
# -out: path = 모델 폴더 절대경로
# -out: error = 없음
#------------------------------------------------------------------
def model_dir(local_dir):
    return resource_path("models", local_dir)


#------------------------------------------------------------------
# 실행파일이 놓인 폴더
#=> A안(외부 모델)에서 "exe 옆"을 찾기 위한 기준 폴더. 얼려진(exe) 상태면 실제
#   exe 위치(임시폴더가 아니라 사용자가 둔 곳), 소스 실행이면 프로젝트 루트.
#
# -in: 없음
#
# -out: dir = exe(또는 소스 루트)가 있는 폴더 절대경로
# -out: error = 없음
#------------------------------------------------------------------
def exe_dir():
    # onefile 도 sys.executable 은 "사용자가 둔 진짜 exe" 를 가리킨다(_MEIPASS 아님).
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return _base_dir()


#------------------------------------------------------------------
# 모델 캐시 폴더
#=> 압축 배포된 모델(.tar.xz)을 최초 1회 풀어 두는 사용자별 폴더. 다음 실행부터는
#   여기서 바로 로드해 재해제를 피한다.
#
# -in: local_dir = 모델 폴더명
#
# -out: dir = %LOCALAPPDATA%/MpowerClassify/models/<local_dir> (없으면 홈 아래 대체)
# -out: error = 없음
#------------------------------------------------------------------
def model_cache_dir(local_dir):
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".csoclassify")
    return os.path.join(base, "MpowerClassify", "models", local_dir)


#------------------------------------------------------------------
# 모델 폴더 후보 목록(우선순위)
#=> model.onnx 가 들어 있을 만한 위치를 우선순위대로 만든다.
#    1) 환경변수 CSOCLASSIFY_MODELS_DIR/<local_dir> (명시 지정)
#    2) 캐시 폴더(이전에 .tar.xz 를 푼 곳)
#    3) exe 옆 models/<local_dir>, exe 옆 resources/models/<local_dir>
#    4) 번들/소스 기준 model_dir
#
# -in: local_dir = 모델 폴더명
#
# -out: list = 후보 폴더 경로들(존재 여부는 확인 안 함)
# -out: error = 없음
#------------------------------------------------------------------
def _model_dir_candidates(local_dir):
    cands = []
    env = os.environ.get("CSOCLASSIFY_MODELS_DIR")
    if env:
        cands.append(os.path.join(env, local_dir))
    cands.append(model_cache_dir(local_dir))
    ed = exe_dir()
    cands.append(os.path.join(ed, "models", local_dir))
    cands.append(os.path.join(ed, "resources", "models", local_dir))
    cands.append(model_dir(local_dir))
    return cands


#------------------------------------------------------------------
# 모델 압축 아카이브(.tar.xz) 탐색
#=> 외부 배포 시 함께 준 <local_dir>.tar.xz 를 여러 위치에서 찾는다.
#
# -in: local_dir = 모델 폴더명
#
# -out: path = 찾은 .tar.xz 경로, 없으면 None
# -out: error = 없음
#------------------------------------------------------------------
def _find_model_archive(local_dir):
    name = f"{local_dir}.tar.xz"
    search = []
    env = os.environ.get("CSOCLASSIFY_MODELS_DIR")
    if env:
        search.append(os.path.join(env, name))
    ed = exe_dir()
    search.append(os.path.join(ed, name))
    search.append(os.path.join(ed, "models", name))
    # 개발 편의: pack_model.py 의 기본 출력 위치
    search.append(os.path.join(_base_dir(), "dist-model", name))
    for p in search:
        if os.path.isfile(p):
            return p
    return None


#------------------------------------------------------------------
# 아카이브를 캐시에 1회 해제(원자적)
#=> .tar.xz 를 임시폴더에 풀고, 완성되면 캐시 폴더로 원자적 이동한다. 두 프로세스가
#   동시에 풀어도(레이스) 먼저 끝낸 쪽만 남고 나머지는 조용히 버려진다.
#    1) 임시폴더에 extractall(안전 필터)
#    2) model.onnx 위치를 확인(루트 또는 한 단계 하위)
#    3) 캐시가 아직 없으면 os.replace 로 이동
#
# -in: archive = .tar.xz 경로
# -in: target  = 최종 캐시 폴더 경로
#
# -out: bool = 캐시에 model.onnx 가 준비되면 True
# -out: error = 없음(해제 실패는 False 로 흡수)
#------------------------------------------------------------------
def _extract_archive_to(archive, target):
    import tarfile
    import tempfile
    import shutil

    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".gmtmp_", dir=parent)
    try:
        with tarfile.open(archive, "r:xz") as tf:
            # 경로 traversal 방지용 안전 필터(파이썬 3.12+). 구버전이면 일반 해제.
            try:
                tf.extractall(tmp, filter="data")
            except TypeError:
                tf.extractall(tmp)

        # 아카이브가 파일을 루트에 담았는지, 한 겹 폴더로 감쌌는지 판별해 실제 소스 결정.
        if os.path.isfile(os.path.join(tmp, "model.onnx")):
            src = tmp
        else:
            subs = [d for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d))]
            src = os.path.join(tmp, subs[0]) if len(subs) == 1 else tmp

        # 이미 다른 프로세스가 캐시를 만들었으면 그대로 두고 성공 처리.
        if not os.path.exists(target):
            try:
                os.replace(src, target)
            except OSError:
                # 레이스로 그 사이 생겼을 수 있으니 최종 존재만 확인.
                pass
        return os.path.isfile(os.path.join(target, "model.onnx"))
    except (OSError, tarfile.TarError):
        return False
    finally:
        # 남은 임시물 정리(이미 이동됐으면 무시).
        shutil.rmtree(tmp, ignore_errors=True)


#------------------------------------------------------------------
# 모델 폴더 최종 해석 (A안 핵심)
#=> 후보들 중 model.onnx 가 있는 첫 폴더를 쓰고, 없으면 .tar.xz 를 찾아 캐시에
#   1회 해제한 뒤 그 캐시를 쓴다. 끝까지 못 찾으면 기본 경로를 돌려줘 임베더가
#   "model.onnx 없음" 에러로 원인을 분명히 알리게 한다.
#
# -in: local_dir = 모델 폴더명
#
# -out: dir = model.onnx 를 로드할 폴더 경로
# -out: error = 없음(최종적으로 못 찾아도 경로만 반환)
#------------------------------------------------------------------
def resolve_model_dir(local_dir):
    # 1) 이미 파일이 놓인 폴더가 있으면 그대로 사용.
    for cand in _model_dir_candidates(local_dir):
        if os.path.isfile(os.path.join(cand, "model.onnx")):
            return cand

    # 2) 없으면 압축 배포본(.tar.xz)을 찾아 캐시에 1회 해제.
    cache = model_cache_dir(local_dir)
    if os.path.isfile(os.path.join(cache, "model.onnx")):
        return cache
    archive = _find_model_archive(local_dir)
    if archive and _extract_archive_to(archive, cache):
        return cache

    # 3) 어디에도 없음 → 기본 경로 반환(임베더가 명확한 에러를 낸다).
    return model_dir(local_dir)
