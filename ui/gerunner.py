#------------------------------------------------------------------
# csoclassify 실행 러너 — 업로드 파일 1개 → 벡터 포함 분류 레코드
#=> seed 직접 추가(업로드) 시, 관리자가 올린 파일을 csoclassify 로 돌려(추출→임베딩)
#   벡터가 든 분류 레코드를 얻는다. subprocess 로 호출해 UI 프로세스와 격리한다.
#------------------------------------------------------------------

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from types import SimpleNamespace


#------------------------------------------------------------------
# 실행 명령 문자열 → 인자 리스트
#=> "MpowerClassify.exe" 또는 "python -m csoclassify" 같은 명령을 인자 리스트로 나눈다.
#   윈도 경로를 위해 백슬래시는 이스케이프로 보지 않고, 큰따옴표로 묶인 토큰(공백 포함
#   경로)은 통째로 한 인자로 처리한다. 예)
#     '"D:\\Program Files\\a b\\MpowerClassify.exe"' → ['D:\\Program Files\\a b\\MpowerClassify.exe']
#     'python -m csoclassify'                     → ['python','-m','csoclassify']
#   (사용자가 실수로 경로를 따옴표로 감싸도, 공백이 있어도 안전하게 동작)
#
# -in: cmd_str = 명령 문자열
# -out: list = 인자 리스트(빈 문자열이면 빈 리스트)
# -out: error = 없음
#------------------------------------------------------------------
def parse_base_cmd(cmd_str):
    if not cmd_str:
        return []
    # 따옴표로 묶인 덩어리는 통째로, 나머지는 공백으로 분리. 감싼 따옴표는 벗겨낸다.
    tokens = re.findall(r'"[^"]*"|\S+', cmd_str)
    return [t[1:-1] if len(t) >= 2 and t.startswith('"') and t.endswith('"') else t
            for t in tokens]


#------------------------------------------------------------------
# glob 패턴 중괄호 확장 (UI·CLI 공용 규칙)
#=> 파이썬 표준 glob 은 셸과 달리 중괄호({a,b,c}) 확장을 지원하지 않는다. 확장자를
#   여러 개 넣고 싶을 때 쓰도록 "*.{hwp,docx,pdf}" 를 ["*.hwp","*.docx","*.pdf"] 로
#   직접 펼친다. 중괄호가 없으면 그대로 1개, 여러 그룹/중첩도 재귀로 모두 펼친다.
#   csoclassify CLI 의 collect_files 와 같은 규칙을 UI 가 공유하도록 여기에 둔다
#   (패키지는 UI 를 역참조할 수 없어 CLI 쪽은 자체 사본을 유지). 예)
#     "*.{hwp,docx}"        → ["*.hwp","*.docx"]
#     "report_*.{doc,docx}" → ["report_*.doc","report_*.docx"]
#
# -in: pattern = 원본 glob 패턴(중괄호 그룹 포함 가능)
#
# -out: list = 펼쳐진 패턴 리스트(중복 제거, 최소 1개)
# -out: error = 없음
#------------------------------------------------------------------
def expand_glob_braces(pattern):
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    pre, post = pattern[:m.start()], pattern[m.end():]
    result = []
    for opt in m.group(1).split(","):
        # 남은 뒷부분에 또 다른 그룹이 있을 수 있어 재귀로 마저 펼친다.
        for expanded in expand_glob_braces(pre + opt.strip() + post):
            if expanded not in result:
                result.append(expanded)
    return result


#------------------------------------------------------------------
# 폴더 안 파일 개수 세기 (화면 안내용)
#=> "이 폴더에 몇 개가 있나"를 화면에 보여 주려고 센다. 분류 대상 수를 미리
#   알려 주는 용도라, 정확한 목록이 필요한 실행 경로와 달리 '빠르게·대충'이
#   목적이다.
#    1) os.scandir 로 훑는다(glob 보다 빠르고 메모리를 안 쌓는다)
#    2) cap 을 넘어서면 세기를 멈추고 capped=True 로 알린다 — 파일이 수십만 개인
#       폴더를 가리켰을 때 화면이 몇 초씩 멈추는 것을 막는다
#    3) 권한 없는 하위 폴더는 조용히 건너뛴다(안내용 숫자 때문에 화면이
#       죽으면 안 된다)
#
# -in: folder    = 셀 폴더 경로
# -in: recursive = 하위 폴더까지 셀지(기본 True)
# -in: cap       = 여기까지만 세고 멈춤(기본 20000)
#
# -out: (count, capped) = 파일 수, cap 에 걸려 멈췄으면 True
# -out: error = 폴더가 없거나 못 읽으면 (0, False)
#------------------------------------------------------------------
def count_files(folder, recursive=True, cap=20000):
    if not folder or not os.path.isdir(folder):
        return 0, False
    count = 0
    stack = [folder]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_file():
                            count += 1
                            # cap 에 닿으면 더 세지 않고 즉시 빠져나온다.
                            if count >= cap:
                                return count, True
                        elif recursive and e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                    except OSError:
                        continue
        except OSError:
            continue
    return count, False


#------------------------------------------------------------------
# glob 패턴 목록 펼치기 (UI·CLI 공용 진입점)
#=> --glob 한 값에 패턴을 여러 개 넣는 두 표기를 모두 받는다.
#     1) 콤마로 나열   : "*.hwp,*.pdf"        (기본·권장 — 접두가 달라도 됨)
#     2) 중괄호로 나열 : "*.{hwp,pdf}"         (셸 관습, 공통 접두/접미 간결)
#   먼저 "중괄호 밖" 최상위 콤마로 패턴을 나눈 뒤(중괄호 안 콤마는 그룹 구분자라
#   건드리지 않음), 각 토큰의 중괄호를 펼쳐 합친다. 두 표기를 섞어도 된다. 예)
#     "*.hwp,*.pdf"          → ["*.hwp","*.pdf"]
#     "*.{hwp,pdf},draft_*"  → ["*.hwp","*.pdf","draft_*"]
#   주의: 파일명/패턴에 리터럴 콤마가 있으면 구분자로 오인될 수 있으나, 확장자
#   필터 용도에선 사실상 발생하지 않는다.
#
# -in: spec = --glob 원본 값(콤마·중괄호로 여러 패턴 가능)
#
# -out: list = 펼쳐진 개별 glob 패턴 리스트(중복 제거, 최소 1개)
# -out: error = 없음
#------------------------------------------------------------------
def expand_glob_patterns(spec):
    # 중괄호 깊이를 세며 최상위(깊이 0) 콤마에서만 자른다.
    tokens, buf, depth = [], [], 0
    for ch in spec:
        if ch == "{":
            depth += 1
            buf.append(ch)
        elif ch == "}":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "," and depth == 0:
            tokens.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    tokens.append("".join(buf))

    result = []
    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        for pat in expand_glob_braces(tok):
            if pat not in result:
                result.append(pat)
    return result


#------------------------------------------------------------------
# csoclassify 로 파일 1개 처리(핵심)
#=> --with-vector 로 파일을 분류(기본)해 stdout 의 jsonl 첫 레코드를 파싱한다.
#   벡터·자동등급·근거가 든 dict 를 돌려준다.
#    1) 인자 조립 후 subprocess 실행(UTF-8 강제 캡처)
#    2) stdout 에서 '{' 로 시작하는 첫 줄을 JSON 으로 파싱
#
# -in: base_cmd   = 실행 인자 리스트(parse_base_cmd 결과)
# -in: file_path  = 처리할 파일 경로
# -in: pythonpath = 'python -m csoclassify' 형태일 때 넣을 PYTHONPATH(예: src)
# -in: timeout    = 최대 대기(초)
#
# -out: dict = 분류 레코드({file, grade, vector, signals, ...})
# -out: error = 출력 없거나 실패 시 RuntimeError
#------------------------------------------------------------------
def run_csoclassify_file(base_cmd, file_path, pythonpath=None, timeout=180):
    # 분류가 기본 동작이므로 --classify 는 붙이지 않는다(--with-vector 로 벡터도 산출).
    args = list(base_cmd) + ["--with-vector", "--file", file_path,
                             "--format", "jsonl", "--no-timing"]
    env = dict(os.environ)
    # 캡처 시 한글이 깨지지 않게 csoclassify 가 UTF-8 로 출력하도록.
    env["PYTHONUTF8"] = "1"
    if pythonpath:
        env["PYTHONPATH"] = pythonpath

    p = subprocess.run(args, capture_output=True, timeout=timeout, env=env)
    out = p.stdout.decode("utf-8", errors="replace")
    err = p.stderr.decode("utf-8", errors="replace")
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            rec = json.loads(line)
            # stderr 을 함께 실어 보낸다. 실행은 됐는데 vector 가 없을 때 "왜 없는지"는
            # 여기(예: "[warn] 알 수 없는 인자: --with-vector")에만 적혀 있다.
            rec["_stderr"] = err[-600:]
            return rec

    raise RuntimeError(f"csoclassify 결과를 얻지 못했습니다 (code={p.returncode}). {err[-400:]}")


#------------------------------------------------------------------
# 문서 하나에 사람 등급을 바로 쓰기(--set-grade)
#=> 검토함에서 등급을 바꾸면 그 파일의 등급 스트림(:mgrade)도 곧바로 맞춘다.
#   설계: plan/사람등급지정-수동오버라이드-설계-20261020.html §07.
#   결정 기록은 화면이 이미 cso_override.jsonl 에 남겼으므로 --grade-overrides 는 주지 않는다.
#    1) 엔진을 --set-grade <등급> --file <문서> --write-grade-stream 으로 부른다
#    2) 결과 레코드의 grade_stream 상태(written/unchanged/failed)와 실패 까닭을 돌려준다
#
# -in: base_cmd    = 실행 인자 리스트(parse_base_cmd 결과)
# -in: file_path   = 대상 문서
# -in: grade       = "C" | "S" | "O"
# -in: update_time = True 면 수정 시각이 바뀌게 둔다(--grade-stream-update-time)
# -in: pythonpath  = 'python -m csoclassify' 일 때 PYTHONPATH
# -in: timeout     = 최대 대기(초)
#
# -out: (state, reason) = 예) ("written", None), ("failed", "access_denied"), ("error", 메시지)
# -out: error = 예외 없음(실패는 ("error", 까닭)으로 돌려준다)
#------------------------------------------------------------------
def run_set_grade(base_cmd, file_path, grade, update_time=False, pythonpath=None, timeout=60):
    args = list(base_cmd) + ["--set-grade", grade, "--file", file_path,
                             "--write-grade-stream", "--format", "jsonl", "--json-errors"]
    if update_time:
        args.append("--grade-stream-update-time")
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return "error", str(e)
    out = p.stdout.decode("utf-8", errors="replace")
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        gs = rec.get("grade_stream")
        if isinstance(gs, dict):
            return gs.get("state"), gs.get("reason")
        # 레코드 대신 오류 JSON 이 왔으면 그 까닭을 그대로 보여 준다
        err = rec.get("error")
        if isinstance(err, dict) and err.get("kind") not in (None, "success"):
            return "error", err.get("message") or err.get("kind")
    return "error", p.stderr.decode("utf-8", errors="replace")[-300:] or f"code={p.returncode}"


#------------------------------------------------------------------
# 기준 문서에 제목 채우기(--seed-fill-title)
#=> 업무분류 BM25 2차는 기준 문서의 제목(첫 줄) · 파일명을 비교한다. 화면은 본문을
#   갖고 있지 않아 등록할 때 제목을 넣을 수 없으므로, 저장한 직후 엔진에 채우게 한다.
#    1) 제목이 빈 줄만 채운다(이미 있는 줄은 건드리지 않는다)
#    2) 본문은 추출 본문 폴더(text_dir)에서 먼저 찾고, 없으면 원본을 다시 읽는다
#    3) 엔진이 stderr 에 남기는 "[seed-fill-title] 채움 …" 한 줄을 그대로 돌려준다
#
# -in: base_cmd   = 실행 인자 리스트(parse_base_cmd 결과)
# -in: seed_path  = class_seed.jsonl 경로
# -in: text_dir   = --textsave 로 저장해 둔 추출 본문 폴더(없으면 None)
# -in: pythonpath = 모듈 실행 시 PYTHONPATH
# -in: timeout    = 최대 대기(초)
#
# -out: (ok, msg) = 성공 여부, 엔진의 요약 한 줄 또는 실패 까닭
# -out: error = 없음(실행 실패도 (False, 까닭) 으로 돌려준다 — 등록 자체는 이미 끝났다)
#------------------------------------------------------------------
def fill_seed_titles(base_cmd, seed_path, text_dir=None, pythonpath=None, timeout=300):
    args = list(base_cmd) + ["--seed-fill-title", "--seeds", seed_path, "--json-errors"]
    if text_dir and os.path.isdir(text_dir):
        args += ["--text-dir", text_dir]
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    err = p.stderr.decode("utf-8", errors="replace")
    line = next((l.strip() for l in err.splitlines() if l.strip().startswith("[seed-fill-title]")), "")
    if p.returncode == 0:
        return True, line
    why = error_line(parse_error_json(p.stdout.decode("utf-8", errors="replace")))
    return False, why or err[-300:] or f"code={p.returncode}"


#------------------------------------------------------------------
# 파일들의 제목(첫 줄) 뽑기 — 곧 지울 임시 파일용
#=> 기준문서 화면에서 올린 파일은 임시 폴더에 잠깐 있다가 지워지고, 기준 문서에는
#   파일 이름만 남는다. 지운 뒤에는 --seed-fill-title 이 원본을 찾을 수 없으므로
#   지우기 전에 제목을 떠 둔다. 제목 규칙을 화면이 따로 두지 않도록 엔진의
#   --seed-fill-title 을 임시 기준 문서 파일에 돌려 그대로 쓴다.
#    1) 임시 폴더에 {file: 파일경로, grade: "O"} 줄들로 된 기준 문서 파일을 만든다
#       (등급은 저장이 줄을 버리지 않게 하는 자리표시일 뿐이다)
#    2) 엔진이 채운 title 을 읽어 {파일경로: 제목} 으로 돌려준다
#
# -in: base_cmd   = 실행 인자 리스트
# -in: files      = 제목을 뽑을 파일 경로 리스트
# -in: pythonpath = 모듈 실행 시 PYTHONPATH
# -in: timeout    = 최대 대기(초)
#
# -out: dict = {파일경로: 제목}(제목이 없거나 못 읽은 파일은 키가 없다)
# -out: error = 없음(실패하면 빈 dict — 제목 없이도 파일명으로는 비교된다)
#------------------------------------------------------------------
def titles_for_files(base_cmd, files, pythonpath=None, timeout=300):
    files = [f for f in files if f and os.path.isfile(f)]
    if not files:
        return {}
    tmpd = tempfile.mkdtemp(prefix="csotitle_")
    try:
        sp = os.path.join(tmpd, "titles_seed.jsonl")
        with open(sp, "w", encoding="utf-8") as fp:
            for f in files:
                fp.write(json.dumps({"v": 3, "file": f, "grade": "O"}, ensure_ascii=False) + "\n")
        ok, _ = fill_seed_titles(base_cmd, sp, pythonpath=pythonpath, timeout=timeout)
        if not ok:
            return {}
        out = {}
        with open(sp, encoding="utf-8") as fp:
            for line in fp:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("title"):
                    out[row.get("file")] = row["title"]
        return out
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


#------------------------------------------------------------------
# 엔진이 낸 오류 JSON 한 줄 읽기
#=> --json-errors 를 주면 엔진이 실패할 때 stdout 에
#   {"error":{"code":…,"kind":…,"message":…,"path":…}} 를 한 줄 낸다.
#   화면은 이 값으로 "왜 실패했는지"를 정확히 말할 수 있다(예전에는 stderr
#   뒤 400자만 보여 줘, 진짜 원인이 앞쪽에 있으면 알 수 없었다).
#    1) stdout 을 줄 단위로 훑어 '{"error"' 로 시작하는 마지막 줄을 찾는다
#    2) 그 줄만 JSON 으로 읽는다(결과 본문과 섞여도 안전하다)
#
# -in: out = 자식 프로세스의 stdout 전체(문자열)
#
# -out: dict = {"code":…, "kind":…, "message":…, ["path":…]} · 없으면 None
# -out: error = 없음(JSON 이 깨져 있어도 None 을 돌려준다 — 화면이 멈추면 안 된다)
#------------------------------------------------------------------
def parse_error_json(out):
    if not out:
        return None
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith('{"error"'):
            try:
                err = json.loads(line).get("error")
            except (ValueError, AttributeError):
                return None
            # 마지막 줄은 성공일 때도 나온다(code 0). 화면에서 error 는 '오류'만
            # 뜻해야 하므로, 성공 줄은 없는 것으로 돌려준다.
            return None if not err or err.get("code") == 0 else err
    return None


#------------------------------------------------------------------
# 오류를 사람이 읽는 한 줄로
#=> 화면과 로그가 같은 문장을 쓰게 한다. kind·code 를 함께 남기는 이유는
#   나중에 "그때 무슨 오류였나"를 문구가 아니라 번호로 확인할 수 있어서다.
#
# -in: err = parse_error_json 결과(없으면 None)
#
# -out: str = "메시지 [kind(code)]" · err 가 없으면 빈 문자열
# -out: error = 없음
#------------------------------------------------------------------
def error_line(err):
    if not err:
        return ""
    tag = f"{err.get('kind')}({err.get('code')})"
    return f"{err.get('message') or '알 수 없는 오류'} [{tag}]"


#------------------------------------------------------------------
# 실행 인자에 --json-errors 를 한 번만 더한다
#=> 화면이 부르는 모든 길에서 오류를 같은 방식으로 받으려는 것이다.
#   이미 들어 있으면 그대로 둔다(중복 인자는 엔진이 경고를 찍는다).
#
# -in: args = 실행 인자 리스트
#
# -out: list = --json-errors 가 포함된 새 리스트
# -out: error = 없음
#------------------------------------------------------------------
def with_json_errors(args):
    args = list(args)
    if "--json-errors" not in args:
        args.append("--json-errors")
    return args


#------------------------------------------------------------------
# 공통 subprocess 실행
#=> 인자 리스트로 csoclassify 를 돌리고 결과(코드/stderr/요약줄)를 담아 돌려준다.
#   결과 본문은 --out 파일로 나가므로 여기선 상태·요약만 본다.
#
# -in: args       = 전체 실행 인자 리스트
# -in: pythonpath = 모듈 실행 시 PYTHONPATH
# -in: timeout    = 최대 대기(초)
# -in: marker     = stderr 에서 뽑을 요약줄 표식(예: "[classify]")
#
# -out: SimpleNamespace(returncode, stderr, summary)
# -out: error = timeout 시 예외 전파
#------------------------------------------------------------------
def _run(args, pythonpath, timeout, marker):
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"          # 한글 출력 UTF-8 고정
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    # 오류를 기계가 읽을 수 있게 받는다 — 화면이 원인을 정확히 말할 수 있다.
    args = with_json_errors(args)
    p = subprocess.run(args, capture_output=True, timeout=timeout, env=env)
    err = p.stderr.decode("utf-8", errors="replace")
    out = p.stdout.decode("utf-8", errors="replace")
    # 요약 한 줄(예: "[classify] files=.. C=.. ..")만 뽑아 표시용으로.
    summary = ""
    for line in err.splitlines():
        if marker in line:
            summary = line.strip()
    return SimpleNamespace(returncode=p.returncode, stderr=err, summary=summary,
                           error=parse_error_json(out))


#------------------------------------------------------------------
# 폴더 분류 실행(스트리밍) — 진행바/경과시간용
#=> 폴더를 통째로 분류하되, subprocess.run(블로킹) 대신 Popen 으로
#   자식의 stderr 를 "한 줄씩" 읽는다. csoclassify 가 --progress 로 흘리는
#   "[progress] 처리수/총수 경로" 를 만나면 on_progress 콜백을 불러 UI 가 진행바와
#   경과시간을 실시간으로 갱신할 수 있게 한다.
#    1) --progress 를 붙여 Popen 기동(결과 본문은 --out 파일로 나가 stdout 은 버림)
#    2) stderr 를 줄단위로 읽어 [progress] 파싱→콜백, [classify] 요약줄 보관
#    3) timeout 초과 시 threading.Timer 로 프로세스 강제 종료
#
# -in: base_cmd    = 실행 인자 리스트(exe 또는 python -m csoclassify)
# -in: folder      = 대상 폴더
# -in: out_path    = 결과 jsonl 저장 경로
# -in: glob        = 파일 패턴(기본 *)
# -in: recursive   = (쓰이지 않음) 두 엔진 모두 --dir 은 항상 재귀다
# -in: embed_mode  = 임베딩 정책. "needed"=보류·seed_eligible 만(기본, 빠름),
#                    "all"=전량, "none"=임베딩 안 함
# -in: pythonpath  = 모듈 실행 시 PYTHONPATH
# -in: timeout     = 최대 대기(초)
# -in: on_progress = 콜백 fn(done:int, total:int, path:str). None 이면 진행보고 생략
# -in: extra_args  = 그대로 덧붙일 인자 리스트(예: ["--doc-rules", "…", "--axis", "security"]).
#                    업무분류 축을 켤 때 화면이 넘긴다. None 이면 아무것도 안 붙는다
#
# -out: SimpleNamespace(returncode, stderr, summary)
# -out: error = 시작 실패 등은 예외 전파(호출부에서 표시)
#------------------------------------------------------------------
# 분류 실행 명령 조립
#=> 실제로 돌릴 인자 목록을 만든다. 화면의 '이 버튼이 실행하는 명령' 표시도 이
#   함수를 부른다 — 보여 주는 것과 도는 것이 같은 값에서 나와야 어긋나지 않는다.
#   (예전에는 화면이 명령을 손으로 다시 적어, 인자를 하나 더할 때마다 두 곳을
#    고쳐야 했고 실제로 어긋났다.)
#
# -in: base_cmd   = 실행 인자 리스트(parse_base_cmd 결과)
# -in: folder     = 분류할 폴더
# -in: out_path   = 결과 jsonl 경로
# -in: glob       = 파일 패턴
# -in: embed_mode = "needed"(보류·seed 후보만) · "all"(전량) · 그 밖(임베딩 없음)
# -in: extra_args = 화면이 덧붙이는 인자(--doc-rules/--axis/--hash 등)
#
# -out: list = 실행 인자 전체
# -out: error = 없음
#------------------------------------------------------------------
def build_classify_args(base_cmd, folder, out_path, glob="*", embed_mode="needed",
                        extra_args=None):
    # 분류가 기본 동작이므로 --classify 는 생략한다(임베딩 정책만 아래에서 지정).
    args = list(base_cmd) + ["--dir", folder, "--glob", glob,
                             "--format", "jsonl", "--out", out_path, "--no-timing",
                             "--progress"]
    if embed_mode == "needed":
        args.append("--embed-needed")
    elif embed_mode == "all":
        args.append("--with-vector")
    # -r 은 넘기지 않는다. 두 엔진 모두 --dir 이면 **언제나 하위 폴더까지** 훑는다
    # (Python cli.py 의 -r 은 help 에 '(무시됨)' 이라 적혀 있고, Rust 는 walkdir 로
    # 항상 재귀한다). Rust 판은 모르는 인자라며 경고까지 찍어 로그만 지저분해졌다.
    # 업무분류(doctype) 축 관련 인자(--doc-rules/--axis)를 화면에서 그대로
    # 넘길 수 있게 열어 둔다.
    if extra_args:
        args += list(extra_args)
    return args


#------------------------------------------------------------------
def run_csoclassify_dir_stream(base_cmd, folder, out_path, glob="*", recursive=True,
                               embed_mode="needed", pythonpath=None, timeout=3600,
                               on_progress=None, extra_args=None):
    args = build_classify_args(base_cmd, folder, out_path, glob=glob,
                               embed_mode=embed_mode, extra_args=extra_args)

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"              # 자식이 UTF-8 로 출력(한글 깨짐 방지)
    if pythonpath:
        env["PYTHONPATH"] = pythonpath

    # 결과 본문은 out_path 파일로 나가므로 stdout 에는 사실상 오류 JSON 한 줄만
    # 온다(예전에는 DEVNULL 로 버렸다). 파이프가 찰 만큼 나오지 않으므로 stderr 를
    # 다 읽은 뒤에 한 번에 받아도 막히지 않는다.
    args = with_json_errors(args)
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    # 자식이 멈춰 무한대기하지 않도록 타임아웃이 지나면 강제 종료.
    timer = threading.Timer(timeout, proc.kill)
    timer.start()

    err_lines = []
    summary = ""
    try:
        for line in proc.stderr:
            s = line.rstrip("\n")
            err_lines.append(s)
            if s.startswith("[progress]"):
                # "[progress] 3/30 D:\경로 파일.hwp" → done, total, path(공백 허용)
                if on_progress is not None:
                    try:
                        parts = s.split(" ", 2)          # 경로에 공백이 있어도 안전
                        done, total = parts[1].split("/")
                        path = parts[2] if len(parts) > 2 else ""
                        on_progress(int(done), int(total), path)
                    except (ValueError, IndexError):
                        pass
            elif s.startswith("[summary] "):
                # 두 엔진 모두 "[summary] 총 N개 / 검출 …" 한 줄로 끝을 알린다.
                # 예전에는 "[classify]" 를 찾고 있었는데 어느 엔진도 그런 줄을
                # 내지 않아, 분류가 끝나면 늘 "분류 완료 · OK" 만 떴다(건수·등급
                # 분포가 화면에 전혀 안 나왔다). 업무분류 롤업 줄
                # ("[summary][업무분류]…")은 대괄호가 이어 붙어 여기 안 걸린다.
                summary = s.strip()
        out = proc.stdout.read()
        proc.wait()
    finally:
        timer.cancel()

    return SimpleNamespace(returncode=proc.returncode,
                           stderr="\n".join(err_lines), summary=summary,
                           error=parse_error_json(out))




#------------------------------------------------------------------
# 업무분류 규칙 자동 생성 실행(--build-doc-rule)
#=> 새 방식에서 화면의 [규칙 다시 만들기]·조정 저장·분류 체계 다시 가져오기가
#   모두 이 한 줄을 부른다. 규칙 파일은 엔진만 쓴다 — 화면이 직접 쓰지 않는다.
#     csoclassify --build-doc-rule --export-input <체계 JSON> --doc-rules <doc_rule.yaml>
#
# -in: base_cmd     = 실행 인자 리스트(parse_base_cmd 결과)
# -in: export_input = 체계 JSON 경로
# -in: doc_rules    = 만들 doc_rule.yaml 경로(같은 폴더의 사전·조정 파일을 쓴다)
# -in: pythonpath   = 모듈 실행 시 PYTHONPATH
# -in: timeout      = 최대 대기(초)
#
# -out: SimpleNamespace(returncode, stderr, summary, error, args)
# -out: error = timeout 시 예외 전파
#------------------------------------------------------------------
def run_build_doc_rule(base_cmd, export_input, doc_rules, pythonpath=None, timeout=180):
    args = list(base_cmd) + ["--build-doc-rule",
                             "--export-input", export_input, "--doc-rules", doc_rules]
    r = _run(args, pythonpath, timeout, "생성")
    r.args = args
    return r


#------------------------------------------------------------------
# 여러 문서를 '한 번의 실행'으로 임베딩
#=> 기준 문서를 여러 건 등록할 때 쓴다. 예전에는 문서 하나에 프로세스 하나를 띄웠는데,
#   그러면 임베딩 모델을 문서 수만큼 다시 읽는다. 실측(5건·Rust)에서 한 건씩은
#   5,439ms(건당 1,088ms), 한 번에 묶으면 1,110ms(건당 222ms)로 **4.9배** 차이였다.
#   20건이면 22초와 4초의 차이라, 관리자가 체감하는 자리다.
#
#   [어떻게 묶나] 대상 문서들이 서로 다른 폴더에 흩어져 있어도 되도록, 임시 폴더에
#   한데 모아 --dir 로 한 번 돌린다. 옮길 때는 하드링크를 먼저 시도한다 — 큰 문서를
#   복사하면 그 시간이 아낀 시간을 도로 까먹는다(다른 드라이브면 복사로 물러선다).
#   이름이 겹칠 수 있으므로(다른 폴더의 같은 이름) 번호를 앞에 붙이고, 결과를
#   그 번호로 원본에 되짚는다. 벡터는 '내용'에서 나오므로 이름이 바뀌어도 같다.
#
# -in: base_cmd    = 실행 인자 리스트(parse_base_cmd 결과)
# -in: files       = 임베딩할 원본 문서 경로 리스트
# -in: pythonpath  = 모듈 실행 시 PYTHONPATH
# -in: timeout     = 최대 대기(초)
# -in: on_progress = 진행 알림 f(끝난수, 전체, 경로)(없어도 된다)
#
# -out: dict = {원본경로: 벡터}. 벡터를 못 얻은 문서는 키가 없다
#              (호출부가 '실패'로 세고 이유를 사람에게 알린다)
# -out: error = 실행 자체가 실패하면 예외 전파(호출부가 화면에 표시)
#------------------------------------------------------------------
def embed_files(base_cmd, files, pythonpath=None, timeout=1800, on_progress=None):
    files = [f for f in files if f]
    if not files:
        return {}

    tmpd = tempfile.mkdtemp(prefix="csoembed_")
    try:
        stage = os.path.join(tmpd, "docs")
        os.makedirs(stage)
        by_index = {}
        for i, src in enumerate(files):
            name = f"{i:04d}_{os.path.basename(src)}"
            dst = os.path.join(stage, name)
            try:
                os.link(src, dst)          # 같은 드라이브면 복사 없이 순간
            except OSError:
                try:
                    shutil.copyfile(src, dst)
                except OSError:
                    continue               # 못 읽는 파일은 그냥 빠진다(결과에 키가 없다)
            by_index[i] = src

        if not by_index:
            return {}

        out_path = os.path.join(tmpd, "out.jsonl")
        run_csoclassify_dir_stream(base_cmd, stage, out_path, glob="*",
                                   embed_mode="all", pythonpath=pythonpath,
                                   timeout=timeout, on_progress=on_progress)

        vectors = {}
        if os.path.isfile(out_path):
            with open(out_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(rec, dict) or not rec.get("file"):
                        continue
                    vec = rec.get("vector")
                    if not vec:
                        continue
                    # "0007_계약서.docx" → 7 번째로 넣은 원본
                    head = os.path.basename(rec["file"]).split("_", 1)[0]
                    try:
                        src = by_index.get(int(head))
                    except ValueError:
                        continue
                    if src:
                        vectors[src] = vec
        return vectors
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
