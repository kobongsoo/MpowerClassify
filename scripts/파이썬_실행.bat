@echo off
chcp 65001 >nul
setlocal
title MpowerClassify - Python source

rem ============================================================
rem  파이썬 소스(src\csoclassify)로 MpowerClassify 를 실행한다.
rem  exe 를 빌드하지 않고 지금 고친 코드를 바로 돌려 볼 때 쓴다.
rem
rem  사용법 1) 문서/폴더를 이 .bat 위로 끌어다 놓기
rem            → 파일이면 --file, 폴더면 --dir 로 분류한다
rem  사용법 2) 그냥 더블클릭 → 대상 경로를 물어본다
rem  사용법 3) 터미널에서 옵션을 붙여 실행 → 옵션을 그대로 넘긴다
rem            예) 파이썬_실행.bat --file "D:\문서\보고서.hwp" --simple
rem            예) 파이썬_실행.bat --filelist list.jsonl --out 결과.json
rem
rem  바꿔 쓰고 싶으면 아래 환경변수를 미리 set 해 두면 된다.
rem    CSO_PY = 쓸 파이썬 실행파일(기본 python)
rem ============================================================

rem 이 .bat 는 <저장소>\scripts\ 에 있으므로 한 칸 위가 저장소 뿌리다.
rem 뿌리에서 돌려야 기본 규칙셋(resources\policy\*.yaml)을 찾는다.
set "ROOT=%~dp0.."
pushd "%ROOT%"
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"

if not defined CSO_PY set "CSO_PY=python"
"%CSO_PY%" --version >nul 2>&1
if errorlevel 1 (
  echo [오류] 파이썬을 실행하지 못했습니다: %CSO_PY%
  echo        PATH 에 python 이 있는지 보거나, CSO_PY 환경변수로 경로를 지정하세요.
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
  set "OUTFILE=%TEMP%\cso_파이썬_결과.json"
)
if defined OUTFILE set "OUTOPT=--out "%OUTFILE%""

echo ============================================================
echo   MpowerClassify 실행 (파이썬 소스)
echo ============================================================
echo   파이썬   : %CSO_PY%
echo   대상     : %TARGET%  (%MODE%)
if defined OUTFILE echo   결과저장 : %OUTFILE%
echo ------------------------------------------------------------
"%CSO_PY%" -m csoclassify %MODE% "%TARGET%" %OUTOPT%
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
"%CSO_PY%" -m csoclassify %*
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
