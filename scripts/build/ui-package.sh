#!/usr/bin/env bash
#------------------------------------------------------------------
# 분류 화면(Streamlit UI) 배포 묶음 만들기 — 다른 PC 에서 UI 만 돌릴 때
#=> ui · src · Rust 엔진 · 파이썬 패키지(wheels) · 설치/실행 배치 · 설치 가이드를
#   한 폴더에 모아 ZIP 으로 묶는다. 받은 PC 는 압축을 풀고 설치.bat → 실행.bat 만 하면 된다.
#   설치 가이드: doc/분류화면-다른PC-설치-시험가이드-20261021.html
#
#   [가져가지 않는 것] ui/settings.yaml(이 PC 경로가 박혀 있다) · 분류 결과 · 검토 결정 ·
#   추출 본문 · __pycache__ · 엔진 백업(.bak-*) · 엔진 로그 · 엔진 샘플.
#   ui/policy(회사 규칙 · 분류 체계 · 기준 문서)는 꼭 가져간다.
#
# 쓰는 법 (Git Bash, 프로젝트 최상위에서):
#   bash scripts/build/ui-package.sh              # wheels 포함(인터넷 없는 PC 용)
#   bash scripts/build/ui-package.sh --no-wheels  # wheels 없이(그 PC 가 인터넷으로 설치)
#------------------------------------------------------------------
. "$(dirname "$0")/_lib.sh"

cd "$(dirname "$0")/../.." || die "프로젝트 최상위로 이동하지 못했습니다"
ROOT="$PWD"
# 파이썬이 찍는 한글 안내가 깨지지 않게
export PYTHONIOENCODING=utf-8
WHEELS=1
[ "${1:-}" = "--no-wheels" ] && WHEELS=0

NAME="MpowerClassify-UI"
STAMP="$(date +%Y%m%d)"
OUT="dist-ui"
STAGE="$OUT/$NAME"
ZIP="$OUT/$NAME-$STAMP.zip"
ENGINE_SRC="Rust/dist-onedir/windows"
GUIDE="doc/분류화면-다른PC-설치-시험가이드-20261021.html"
# wheels 를 받아 둘 파이썬 버전 — 설치.bat 의 버전 검사(3.11~3.14)와 맞춘다
UI_PY_VERSIONS="3.11 3.12 3.13 3.14"

[ -f "$ENGINE_SRC/MpowerClassify-rs.exe" ] || die "Rust 엔진 배포본이 없습니다: $ENGINE_SRC (먼저 win-deploy.sh)"
[ -f ui/app.py ] || die "ui/app.py 가 없습니다"
[ -f "$GUIDE" ] || die "설치 가이드가 없습니다: $GUIDE"

step "묶음 폴더 비우기"
rm -rf "$STAGE" && mkdir -p "$STAGE" || die "묶음 폴더를 만들지 못했습니다: $STAGE"

#------------------------------------------------------------------
# 폴더 복사(빼는 것 지정) — robocopy 는 성공해도 1~7 을 돌려준다
#
# -in: $1 = 원본 · $2 = 대상 · 나머지 = robocopy 의 /XD · /XF 인자
#
# -out: 없음
# -out: error = robocopy 가 8 이상이면 중단
#------------------------------------------------------------------
copy_dir() {
    local src="$1" dst="$2"; shift 2
    local rc=0
    # robocopy 는 성공해도 1(복사함)을 돌려준다 — set -e 에 걸리지 않게 코드를 받아 둔다
    MSYS_NO_PATHCONV=1 robocopy "$(cygpath -w "$src")" "$(cygpath -w "$dst")" /E /NFL /NDL /NJH /NJS /NP "$@" >/dev/null || rc=$?
    [ "$rc" -lt 8 ] || die "복사 실패: $src → $dst (robocopy $rc)"
}

step "ui · src · 엔진 복사"
copy_dir ui "$STAGE/ui" /XD __pycache__ _extracted /XF settings.yaml "cso_result.jsonl*" cso_override.jsonl "*.pyc"
copy_dir src "$STAGE/src" /XD __pycache__ /XF "*.pyc"
copy_dir "$ENGINE_SRC" "$STAGE/engine" /XD log sample /XF "*.bak-*" sample.tar sample.zip
cp "$GUIDE" "$STAGE/설치가이드.html" || die "가이드 복사 실패"
echo "  ui $(du -sh "$STAGE/ui" | cut -f1) · src $(du -sh "$STAGE/src" | cut -f1) · engine $(du -sh "$STAGE/engine" | cut -f1)"

if [ "$WHEELS" = 1 ]; then
    step "파이썬 패키지 받기(wheels — 인터넷 없는 PC 용)"
    # 받는 PC 의 파이썬이 3.12 가 아닐 수 있다 — pandas · numpy 처럼 버전별 wheel 이 따로인
    # 패키지는 3.11~3.14 를 모두 받는다(64비트 Windows). 순수 파이썬 wheel 은 한 번만 받힌다
    for PYV in $UI_PY_VERSIONS; do
        python -m pip download -q -r ui/requirements.txt -d "$STAGE/wheels" \
            --only-binary=:all: --python-version "$PYV" --platform win_amd64 \
            || die "Python $PYV 용 패키지를 받지 못했습니다(인터넷 · pip 확인, 또는 --no-wheels)"
    done
    echo "  wheels $(ls "$STAGE/wheels" | wc -l)개 · $(du -sh "$STAGE/wheels" | cut -f1)"
fi

step "설치.bat · 실행.bat 만들기"
# cmd 는 배치 파일을 CP949 로 읽는다 — CP949 + CRLF 로 쓴다
python - "$STAGE" <<'PY' || die "배치 파일을 만들지 못했습니다"
import os, sys
stage = sys.argv[1]
install = r'''@echo off
rem ------------------------------------------------------------------
rem 분류 화면 설치 - 파이썬 패키지를 깐다(한 번만)
rem  - wheels 폴더가 있으면 인터넷 없이, 없으면 인터넷으로 받는다
rem ------------------------------------------------------------------
cd /d %~dp0
echo [1/3] 파이썬 확인
python --version
if errorlevel 1 goto :nopython
rem wheels 는 64비트 Python 3.11~3.14 용만 들어 있다 - 다르면 pip 오류 대신 여기서 알린다
python -c "import sys,struct; sys.exit(0 if struct.calcsize('P')==8 and (3,11)<=sys.version_info[:2]<=(3,14) else 1)"
if errorlevel 1 goto :badpython
echo [2/3] 패키지 설치
if exist wheels goto :offline
python -m pip install -r ui\requirements.txt
goto :checked
:offline
python -m pip install --no-index --find-links wheels -r ui\requirements.txt
:checked
if errorlevel 1 goto :failed
echo [3/3] Streamlit 첫 실행 질문(이메일) 건너뛰기 설정
if not exist "%USERPROFILE%\.streamlit" mkdir "%USERPROFILE%\.streamlit"
if not exist "%USERPROFILE%\.streamlit\credentials.toml" (
  echo [general]> "%USERPROFILE%\.streamlit\credentials.toml"
  echo email = "">> "%USERPROFILE%\.streamlit\credentials.toml"
)
echo.
echo 설치가 끝났습니다. 실행.bat 을 더블클릭하세요.
pause
exit /b 0
:nopython
echo.
echo 파이썬이 없습니다. python.org 에서 Python 3.12 64비트를 설치하세요.
echo 설치 첫 화면에서 "Add python.exe to PATH" 를 꼭 체크하세요.
pause
exit /b 1
:badpython
echo.
echo 이 파이썬은 쓸 수 없습니다. 64비트 Python 3.11 ~ 3.14 가 필요합니다.
python -c "import sys,struct; print('  지금 파이썬:', sys.version.split()[0], struct.calcsize('P')*8, '비트 -', sys.executable)"
echo python.org 에서 Python 3.12 64비트 (Windows installer 64-bit) 를 설치하세요.
echo 여러 파이썬이 깔려 있으면 PATH 에서 맞는 것이 먼저 오게 하세요.
pause
exit /b 1
:failed
echo.
echo 패키지 설치에 실패했습니다. 위 오류를 확인하세요.
pause
exit /b 1
'''
run = r'''@echo off
rem ------------------------------------------------------------------
rem 분류 화면 실행 - 브라우저에서 http://localhost:8501 이 열린다
rem  - 이 창을 닫으면 화면도 멈춘다
rem ------------------------------------------------------------------
cd /d %~dp0ui
python -m streamlit run app.py --server.port 8501 --browser.gatherUsageStats false
pause
'''
remote = r'''@echo off
rem ------------------------------------------------------------------
rem 분류 화면 실행(다른 PC 에서 접속 허용) - http://이PC주소:8501
rem  - 방화벽에서 TCP 8501 을 열어야 한다. 로그인이 없으니 사내망에서만 쓴다
rem ------------------------------------------------------------------
cd /d %~dp0ui
python -m streamlit run app.py --server.address 0.0.0.0 --server.port 8501 --browser.gatherUsageStats false
pause
'''
for name, text in (("설치.bat", install), ("실행.bat", run), ("실행-원격접속허용.bat", remote)):
    with open(os.path.join(stage, name), "wb") as f:
        f.write(text.replace("\r\n", "\n").replace("\n", "\r\n").encode("cp949"))
print("  설치.bat · 실행.bat · 실행-원격접속허용.bat")
PY

step "ZIP 만들기"
rm -f "$ZIP"
python - "$STAGE" "$ZIP" "$NAME" <<'PY' || die "ZIP 을 만들지 못했습니다"
import os, sys, zipfile
stage, zip_path, name = sys.argv[1], sys.argv[2], sys.argv[3]
n = 0
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for dirpath, _dirs, files in os.walk(stage):
        for fn in files:
            full = os.path.join(dirpath, fn)
            arc = os.path.join(name, os.path.relpath(full, stage))
            z.write(full, arc)
            n += 1
print(f"  파일 {n}개 · {os.path.getsize(zip_path) / 1024 / 1024:.0f}MB")
PY

step "완료"
echo "  $ROOT/$ZIP"
echo "  (묶음 폴더 $STAGE 는 확인용으로 남겨 둡니다)"
