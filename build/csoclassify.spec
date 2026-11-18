# -*- mode: python ; coding: utf-8 -*-
#------------------------------------------------------------------
# PyInstaller 스펙 (onedir, 외장 리소스) — 설계서 §10
#=> MpowerClassify.exe(+_internal/)를 만든다. 코드와 ko-pii 사전만 번들하고,
#   사이냅·임베딩 모델·규칙셋(cso_rule.yaml)은 exe 옆 '외장'으로 둔다(dist-pkg 스타일).
#   런타임에 resources.py 가 exe 옆(synap/<os>/, models/<name>/, cso_rule.yaml)에서 찾는다.
#   빌드:  pyinstaller build/csoclassify.spec  → dist/csoclassify/ (onedir, 빠른 시작)
#   ※ 리눅스 실행파일은 리눅스에서 빌드해야 한다(PyInstaller 크로스컴파일 불가).
#------------------------------------------------------------------

import os
import sys

# ko-pii(L1 PII 엔진) 사전(.txt.gz)만 훅으로 수집. collect_submodules(ko_pii) 는
# 무거운 비-검출 모듈까지 끌어와 배제하고, 정적 import 추적에 맡긴다.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(os.getcwd()))
entry = os.path.join(ROOT, "src", "csoclassify_launcher.py")

# [외장화] 사이냅·모델·규칙셋은 exe 에 번들하지 않는다(exe 옆 외장 배치). ko-pii 사전만 포함.
# 런타임 resources.py 가 exe 옆 synap/<os>/·models/<name>/·cso_rule.yaml 을 찾는다.
datas = list(collect_data_files("ko_pii"))

hiddenimports = [
    "onnxruntime", "tokenizers", "numpy", "yaml",
    "csoclassify.classify",
    "csoclassify.classify.rules",
    "csoclassify.classify.engine",
    "csoclassify.classify.context",
    "csoclassify.classify.fuse",
    # 업무분류 seed 부트스트랩(--make-doctype-seeds). cli.py 가 함수 안에서
    # 늦게 import 하므로 정적 추적에 안 걸린다 — 빠지면 그 옵션만 조용히
    # ModuleNotFoundError 로 죽는다(재설계 10장).
    "csoclassify.classify.seedgen",
    # 하이브리드 추출기(--hybridparse) — 감지+포맷별 파서(정적 import 로 자동 포함되나 명시).
    "csoclassify.extract.hybrid",
    "csoclassify.extract.detect",
    "csoclassify.extract.archive",
    "csoclassify.extract.hwpx_zip",
    "csoclassify.extract.hwp5",
    "csoclassify.extract.pdf_pdfium",
    "csoclassify.extract.plaintext",
    "csoclassify.extract.html_text",
    "csoclassify.extract.office_ooxml",
    "csoclassify.extract.office_legacy",
    # Rust 문서파서(--extractor rust) — 설계: plan/Rust-문서파서-라이브러리분리-설계-20261110.html §6-2.
    # docparse 는 별도 프로젝트(DocParse-rust)가 만든 공유 라이브러리를 ctypes(_docparse.py)로 부른다.
    # 라이브러리 파일은 아래 binaries 에 넣는다. 파이썬 판 기본 파서라(2026-11-18~) 빠지면 그 배포본은
    # 기본 설정에서 1017 로 멈춘다(--extractor py 로만 돌릴 수 있다).
    "csoclassify.extract.rust_engine",
    "csoclassify.extract._docparse",
    # 개인정보 검출 라이브러리(--pii-engine moaipii) — 별도 프로젝트 MoAI-pii 가 만든 moaipii.dll/.so 를
    # ctypes(_moaipii.py)로 부른다. 라이브러리 파일은 아래 binaries 에 넣는다. 빠지면 그 배포본은
    # --pii-engine moaipii 에서 1018 로 멈춘다.
    "csoclassify.classify._moaipii",
    # 포맷별 파서 라이브러리(전부 CentOS7/glibc2.17 호환 wheel).
    # docx/xlsx/pptx 는 stdlib(zipfile+ElementTree)로 직접 파싱하므로 라이브러리 불필요.
    "olefile", "pypdfium2", "xlrd",
    # 압축 확장(archive.py): 7z=py7zr(+C확장 의존), rar=rarfile(+번들 unrar).
    # tar/gz/bz2/xz 는 stdlib 라 추가 의존 없음.
    "py7zr", "rarfile",
    "pyppmd", "pybcj", "brotli", "inflate64", "multivolumefile", "texttable",
    "pyzstd", "backports.zstd", "Cryptodome",
    # ko-pii 는 정적 import 추적으로 자동 포함(collect_submodules 불필요·유해).
]
# numpy 서브모듈 전수 수집(리눅스 한정): numpy._core._exceptions 등 지연 import 누락 방지.
# Windows 는 기본 훅으로 충분하고, 전수 수집은 용량만 키운다.
if os.name != "nt":
    hiddenimports += collect_submodules("numpy")

# 리눅스(CentOS 7 등 구형 배포판) 전용: 시스템 libstdc++ 가 onnxruntime/numpy 확장이
# 요구하는 CXXABI_1.3.9+ 를 제공 못 해 ImportError 가 난다. 최신 libstdc++/libgcc 를
# 명시 번들해 시스템 구형 라이브러리 대신 쓰게 한다.(Windows 는 건너뜀)
#   찾는 순서: ① 환경변수 CSO_EXTRA_LIBDIR(슬림 빌드 시 conda 라이브러리 경로 지정)
#             ② sys.prefix/lib (conda 파이썬으로 직접 빌드하던 기존 방식)
# venv(--system-site-packages)로 빌드하면 sys.prefix 에는 없으므로 ①이 필요하다.
binaries = []
# 문서파서 라이브러리(docparse.dll / libdocparse.so) — 저장소 third_party/docparse/<os>-x64 의 정본을
# 묶음 최상위(_internal = sys._MEIPASS)에 둔다. _docparse.py 는 _MEIPASS 를 찾아본다.
_dp_sub = "windows-x64" if os.name == "nt" else "linux-x64"
_dp_name = "docparse.dll" if os.name == "nt" else "libdocparse.so"
_dp_path = os.path.join(ROOT, "third_party", "docparse", _dp_sub, _dp_name)
if os.path.isfile(_dp_path):
    binaries.append((_dp_path, "."))
else:
    print("[spec] 경고: 문서파서 라이브러리가 없습니다 — %s (기본 파서라 배포본이 1017 로 멈춘다)" % _dp_path)
# 개인정보 검출 라이브러리(moaipii.dll / libmoaipii.so) — 저장소 third_party/moaipii/<os>-x64 의 정본을
# 같은 자리(묶음 최상위)에 둔다. _moaipii.py 는 _MEIPASS 를 찾아본다.
_mp_name = "moaipii.dll" if os.name == "nt" else "libmoaipii.so"
_mp_path = os.path.join(ROOT, "third_party", "moaipii", _dp_sub, _mp_name)
if os.path.isfile(_mp_path):
    binaries.append((_mp_path, "."))
else:
    print("[spec] 경고: 개인정보 검출 라이브러리가 없습니다 — %s (--pii-engine moaipii 가 1018 로 멈춘다)" % _mp_path)
if os.name != "nt":
    _libdirs = [d for d in (os.environ.get("CSO_EXTRA_LIBDIR"),
                            os.path.join(sys.prefix, "lib")) if d]
    for _lib in ("libstdc++.so.6", "libgcc_s.so.1"):
        for _d in _libdirs:
            _p = os.path.join(_d, _lib)
            if os.path.exists(_p):
                binaries.append((_p, "."))
                break   # 한 곳에서 찾으면 충분

# rar 해제용 unrar 바이너리 번들(있으면). RAR 은 독점 포맷이라 순수 파이썬 해제기가 없어,
# 공식 unrar(리눅스)/unrar.exe(윈도우)를 build/vendor/ 에 두면 _internal 루트에 함께 넣는다.
# 런타임 archive._configure_unrar() 가 _MEIPASS 에서 이 파일을 찾아 rarfile 에 지정한다.
# (파일이 없으면 rar 는 시스템 unrar/bsdtar 자동탐색에 맡기고, 그것도 없으면 rar 만 미분류.)
_unrar_name = "unrar.exe" if os.name == "nt" else "unrar"
_unrar_path = os.path.join(ROOT, "build", "vendor", _unrar_name)
if os.path.exists(_unrar_path):
    binaries.append((_unrar_path, "."))

block_cipher = None

a = Analysis(
    [entry],
    pathex=[os.path.join(ROOT, "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # 런타임에 불필요한 대형 패키지는 제외해 용량을 줄인다.
    excludes=["torch", "transformers", "optimum", "sentence_transformers", "scipy", "matplotlib",
              "pandas", "pyarrow", "sklearn", "scikit-learn", "datasets",
              "langchain", "langchain_text_splitters", "llama_index", "presidio_analyzer",
              "spacy", "openai", "hf_xet", "huggingface_hub",
              "xberg",   # 조립형 스택으로 대체 — 번들 금지(대형·CentOS7 불가)
              # docx/xlsx/pptx 를 stdlib 로 직접 파싱 → 아래 대형 의존을 통째로 배제.
              #  · python-docx/python-pptx 가 끌던 PIL(13MB)·lxml(7MB) 제거가 핵심 감량.
              "docx", "openpyxl", "pptx", "PIL", "Pillow", "lxml",
              # [리눅스 빌드 환경 오염 차단] conda `bong` 은 데이터사이언스용이라 numba/
              # llvmlite(153MB)·jedi·IPython·jupyter 등이 깔려 있고, numpy.testing/
              # setuptools 체인을 타고 통째로 번들될 수 있다. 런타임에 전혀 안 쓰므로 배제.
              "IPython", "numba", "llvmlite", "jedi", "parso", "jupyter_client",
              "ipykernel", "jupyter_core", "nbformat", "jsonschema", "notebook",
              "numpy.typing.mypy_plugin", "numpy.matrixlib.tests", "numpy.testing",
              # 슬림화: 런타임에 안 쓰는 numpy 테스트/f2py, GUI(tkinter) 제외.
              "numpy.tests", "numpy._core.tests", "numpy.f2py", "numpy.distutils", "tkinter"],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# 슬림화: 리눅스는 번들 .so 심볼을 strip 해 용량을 줄인다. Windows 는 strip 도구가
# 없을 수 있어 끈다(os.name=='nt').
_strip = os.name != "nt"

#------------------------------------------------------------------
# 실행파일 '속성 → 자세히' 정보(버전 리소스) 만들기 — Windows 만
#=> 탐색기 속성에 파일 버전 · 제품 이름 · 제품 버전 · 저작권 · 언어가 비어 있던 것을 채운다.
#    1) 버전은 src/csoclassify/__init__.py 의 __version__(코드 버전)을 글자 그대로 읽는다
#       (패키지를 import 하면 무거운 모듈까지 끌려오므로 정규식으로만 꺼낸다)
#    2) PyInstaller 의 VSVersionInfo 로 만든다 — 언어 한국어(0x0412) · 유니코드(1200)
#   Rust 판 Rust/build.rs embed_version_info 와 같은 값을 쓴다. 리눅스(ELF)에는 이런 칸이 없어 None.
#
# -in: 없음
#
# -out: VSVersionInfo | None = EXE(version=...) 에 넘길 값
# -out: error = __init__.py 에서 버전을 못 찾으면 0.0.0 으로 쓴다(빌드는 계속)
#------------------------------------------------------------------
def _version_info():
    if os.name != "nt":
        return None
    import re
    from PyInstaller.utils.win32.versioninfo import (
        VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable, StringStruct,
        VarFileInfo, VarStruct)
    src = open(os.path.join(ROOT, "src", "csoclassify", "__init__.py"), encoding="utf-8").read()
    m = re.search(r'__version__\s*=\s*"([^"]+)"', src)
    ver = m.group(1) if m else "0.0.0"
    # 1.2.3 → (1, 2, 3, 0) — 숫자 버전은 네 자리
    nums = tuple((list(int(x) for x in re.findall(r"\d+", ver)) + [0, 0, 0, 0])[:4])
    dotted = ".".join(str(n) for n in nums)
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=nums, prodvers=nums, mask=0x3F, flags=0x0,
                          OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
        kids=[
            StringFileInfo([StringTable("041204B0", [
                StringStruct("CompanyName", "kobongsoo"),
                StringStruct("FileDescription", "MpowerClassify - 문서 보안등급 · 업무분류 (Python 판)"),
                StringStruct("FileVersion", dotted),
                StringStruct("InternalName", "MpowerClassify"),
                StringStruct("LegalCopyright", "Copyright (C) 2026 kobongsoo. All rights reserved."),
                StringStruct("OriginalFilename", "MpowerClassify.exe"),
                StringStruct("ProductName", "MpowerClassify"),
                StringStruct("ProductVersion", ver),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0412, 1200])]),
        ])


exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,   # onedir: 바이너리는 아래 COLLECT 로 모음
    name="MpowerClassify",
    version=_version_info(),  # exe 속성의 파일 버전 · 제품 이름 · 저작권 · 언어(Windows 만)
    console=True,            # CLI 도구이므로 콘솔 유지
    disable_windowed_traceback=False,
    strip=_strip,
    upx=False,               # UPX 는 DLL 호환성 확인 후 선택
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=_strip,
    upx=False,
    name="MpowerClassify",
)
