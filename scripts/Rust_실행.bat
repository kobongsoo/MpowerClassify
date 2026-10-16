@echo off
chcp 65001 >nul
setlocal
title MpowerClassify - Rust

rem ============================================================
rem  Rust 판 MpowerClassify-rs.exe 로 분류를 실행한다.
rem
rem  사용법 1) 문서/폴더를 이 .bat 위로 끌어다 놓기
rem            → 파일이면 --file, 폴더면 --dir 로 분류한다
rem  사용법 2) 그냥 더블클릭 → 대상 경로를 물어본다
rem  사용법 3) 터미널에서 옵션을 붙여 실행 → 옵션을 그대로 넘긴다
rem            예) Rust_실행.bat --file "D:\문서\보고서.hwp" --simple
rem            예) Rust_실행.bat --filelist list.jsonl --out 결과.json
rem
rem  소스를 고쳤다면 먼저 다시 빌드해야 반영된다:
rem    cd Rust ^&^& cargo build --release
rem
rem  바꿔 쓰고 싶으면 아래 환경변수를 미리 set 해 두면 된다.
rem    CSO_RS_EXE             = 쓸 Rust 실행파일 경로
rem    CSOCLASSIFY_POLICY_DIR = 규칙셋 폴더(기본 <저장소>\resources\policy)
rem    CSOCLASSIFY_MODELS_DIR = 모델 폴더(기본 <저장소>\resources\models)
rem    CSOCLASSIFY_LOGDIR     = 로그 폴더(기본 <저장소>\log)
rem ============================================================

rem 이 .bat 는 <저장소>\scripts\ 에 있으므로 한 칸 위가 저장소 뿌리다.
set "ROOT=%~dp0.."
pushd "%ROOT%"

rem Rust 판은 규칙셋·모델을 'exe 옆'에서 찾는데, target\release 옆에는 없다.
rem 저장소의 resources 를 보게 환경변수로 알려 준다(미리 set 해 두었으면 그 값을 쓴다).
rem 로그도 파이썬 판과 같은 <저장소>\log 에 쌓이게 맞춘다.
if not defined CSOCLASSIFY_POLICY_DIR set "CSOCLASSIFY_POLICY_DIR=%CD%\resources\policy"
if not defined CSOCLASSIFY_MODELS_DIR set "CSOCLASSIFY_MODELS_DIR=%CD%\resources\models"
if not defined CSOCLASSIFY_LOGDIR set "CSOCLASSIFY_LOGDIR=%CD%\log"

if not defined CSO_RS_EXE set "CSO_RS_EXE=%CD%\Rust\target\release\MpowerClassify-rs.exe"
if not exist "%CSO_RS_EXE%" (
  echo [오류] Rust 실행파일이 없습니다: %CSO_RS_EXE%
  echo        먼저 빌드하세요:  cd Rust ^&^& cargo build --release
  echo        또는 CSO_RS_EXE 환경변수로 경로를 지정하세요.
  set "RC=9009"
  goto :END
)

rem ── 사용법 3: 첫 인자가 '-' 로 시작하면 옵션을 그대로 넘긴다 ──
set "ARG1=%~1"
if defined ARG1 if "%ARG1:~0,1%"=="-" goto :PASSTHRU

rem ── 사용법 1·2: 대상 하나를 받아 분류한다 ──
set "TARGET=%~1"
if not defined TARGET (
  echo  분류할 문서 또는 폴더 경로를 입력하고 Enter 를 누르세요.
  echo  ^(이 .bat 위로 끌어다 놓아도 됩니다^)
  set /p "TARGET=대상 경로: "
)
rem 탐색기에서 '경로 복사'하면 따옴표가 붙어 온다 — 떼어 낸다.
if defined TARGET set "TARGET=%TARGET:"=%"
if not defined TARGET (
  echo [오류] 대상 경로를 입력하지 않았습니다.
  set "RC=3"
  goto :END
)
if not exist "%TARGET%" (
  echo [오류] 대상이 없습니다: %TARGET%
  set "RC=3"
  goto :END
)

rem 폴더면 --dir, 파일이면 --file. 폴더 결과는 길어서 파일로 남긴다.
set "MODE=--file"
set "OUTOPT="
set "OUTFILE="
if exist "%TARGET%\" (
  set "MODE=--dir"
  set "OUTFILE=%TEMP%\cso_Rust_결과.json"
)
if defined OUTFILE set "OUTOPT=--out "%OUTFILE%""

echo ============================================================
echo   MpowerClassify 실행 (Rust)
echo ============================================================
echo   실행파일 : %CSO_RS_EXE%
echo   대상     : %TARGET%  (%MODE%)
if defined OUTFILE echo   결과저장 : %OUTFILE%
echo ------------------------------------------------------------
"%CSO_RS_EXE%" %MODE% "%TARGET%" %OUTOPT%
set "RC=%ERRORLEVEL%"
echo ------------------------------------------------------------
if defined OUTFILE if exist "%OUTFILE%" echo [결과] %OUTFILE%

:END
if "%RC%"=="0" (
  echo [완료] 종료코드=0
) else (
  echo [실패] 종료코드=%RC%
)
popd
echo.
pause
exit /b %RC%

rem 옵션을 넘겨 실행한 경우 — 터미널에서 부른 것이라 pause 없이 끝낸다.
:PASSTHRU
"%CSO_RS_EXE%" %*
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
