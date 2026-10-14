# -*- coding: utf-8 -*-
"""CLI 오류 계약 — 표 자체와 두 엔진의 일치를 지킨다.

설계: plan/CLI-오류출력-설계.html
"""
import json
import os
import re
import subprocess
import sys

import pytest

from csoclassify import cli
from csoclassify import errcodes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RS_EXE = os.path.join(ROOT, "Rust", "target", "release", "MpowerClassify-rs.exe")
RS_SRC = os.path.join(ROOT, "Rust", "src", "errcodes.rs")


#------------------------------------------------------------------
# 표 자체가 성립하는가 — 번호도 이름도 겹치면 안 된다
#=> 번호가 겹치면 받는 쪽의 분기가 두 뜻을 가지게 되고, 이름이 겹치면 표를
#   찾을 때 먼저 만난 쪽이 조용히 이긴다. 둘 다 눈으로는 안 보이는 사고다.
#
# -in: 없음
# -out: 없음(단언)
# -out: error = 없음
#------------------------------------------------------------------
def test_코드는_저마다_유일하다():
    codes = [c for c, _ in errcodes.ERRORS.values()]
    assert len(set(codes)) == len(codes), "중복된 code 가 있다"


#------------------------------------------------------------------
# 종료코드는 지금까지의 약속(0~4) 안에 있어야 한다
#=> 이 계약은 code 를 늘리는 것이지 종료코드를 늘리는 것이 아니다. 셸 스크립트가
#   종료코드만 보고도 지금까지처럼 굴러가야 한다.
#
# -in: 없음
# -out: 없음(단언)
# -out: error = 없음
#------------------------------------------------------------------
def test_종료코드는_0에서_4_사이다():
    for kind, (_, ex) in errcodes.ERRORS.items():
        assert 0 <= ex <= 4, f"{kind} 의 종료코드가 범위 밖: {ex}"


#------------------------------------------------------------------
# 갈래(백의 자리)와 종료코드가 어긋나지 않는가
#=> 받는 쪽에 "모르는 code 는 백의 자리로 뭉뚱그려 처리하라"고 안내했으므로,
#   한 갈래 안에서 뜻이 흔들리면 그 안내가 거짓말이 된다.
#
# -in: 없음
# -out: 없음(단언)
# -out: error = 없음
#------------------------------------------------------------------
def test_갈래별_종료코드가_일관된다():
    for kind, (code, ex) in errcodes.ERRORS.items():
        if code // 1000 == 1:
            # 1000 인자·입력은 부른 쪽 잘못이라 3. 1009 만 정책 위반(T11)이라 4.
            assert ex == 3 or code == 1009, f"{kind} 는 3 이어야 한다"
        elif code // 1000 == 2:
            # 2000 정책 파일은 '없으면 3 · 내용이 틀리면 4' 두 가지뿐이다.
            assert ex in (3, 4), f"{kind} 가 이상하다"


#------------------------------------------------------------------
# path 는 있을 때만 넣는다
#=> "경로가 비었다"와 "경로와 무관하다"는 다른 뜻이다. 빈 문자열을 넣으면
#   받는 쪽이 그 둘을 구분할 수 없다.
#
# -in: 없음
# -out: 없음(단언)
# -out: error = 없음
#------------------------------------------------------------------
def test_path_는_있을_때만_들어간다():
    assert errcodes.error_object("no_input", "대상 없음", "D:/x")["error"]["path"] == "D:/x"
    assert "path" not in errcodes.error_object("bad_args", "인자 오류")["error"]
    assert "path" not in errcodes.error_object("bad_args", "인자 오류", "")["error"]


#------------------------------------------------------------------
# 여러 줄 안내도 JSON 은 한 줄이어야 한다
#=> 부르는 쪽이 stdout 을 줄 단위로 읽는다. 안내가 줄바꿈을 품고 나가면
#   한 오류가 여러 줄로 쪼개져 파싱이 깨진다.
#
# -in: 없음
# -out: 없음(단언)
# -out: error = 없음
#------------------------------------------------------------------
def test_여러줄_안내도_한_줄로_담는다():
    obj = errcodes.error_object("no_input", "첫 줄\n  둘째 줄\n\n셋째 줄")
    assert obj["error"]["message"] == "첫 줄 / 둘째 줄 /  / 셋째 줄" or \
           "\n" not in obj["error"]["message"]
    assert "\n" not in json.dumps(obj, ensure_ascii=False)


#------------------------------------------------------------------
# 옵션을 안 주면 stdout 은 예전 그대로다
#=> 지금 이 CLI 를 쓰고 있는 쪽은 stdout 을 결과로만 읽는다. 옵션 없이도 JSON 이
#   섞여 나가면 그 프로그램이 그날로 깨진다 — 그래서 '선택' 옵션으로 두었다.
#
# -in: 없음
# -out: 없음(단언)
# -out: error = 없음
#------------------------------------------------------------------
def test_옵션이_없으면_JSON을_내지_않는다(capsys):
    errcodes.enable_json(False)
    try:
        errcodes.emit("no_input", "대상 없음", "D:/x")
        assert capsys.readouterr().out == ""
    finally:
        errcodes.enable_json(False)


#------------------------------------------------------------------
# 옵션을 주면 stdout 에 한 줄이 나간다
#=> 실패하면 결과가 없으므로 결과와 부딪히지 않고, 부르는 쪽은 stdout 만
#   파싱하면 된다.
#
# -in: 없음
# -out: 없음(단언)
# -out: error = 없음
#------------------------------------------------------------------
def test_옵션을_주면_stdout에_한_줄(capsys):
    errcodes.enable_json(True)
    try:
        errcodes.emit("no_input", "대상 없음", "D:/x")
        out = capsys.readouterr().out.strip()
    finally:
        errcodes.enable_json(False)
    assert json.loads(out) == {"error": {
        "code": 1001, "kind": "no_input", "message": "대상 없음", "path": "D:/x"}}


#------------------------------------------------------------------
# fail_err 는 표가 정한 종료코드를 돌려준다
#=> 실패 자리에서 종료코드를 손으로 고르지 않게 하는 것이 이 계약의 핵심이다.
#   이름만 고르면 번호와 종료코드는 표가 정한다.
#
# -in: 없음
# -out: 없음(단언)
# -out: error = 없음
#------------------------------------------------------------------
def test_fail_err_는_표가_정한_코드로_끝낸다(capsys):
    assert cli.fail_err("taxonomy_missing", "[MpowerClassify] 없다", "D:/t.yaml") == 3
    assert cli.fail_err("taxonomy_invalid", "[MpowerClassify] 깨졌다", "D:/t.yaml") == 4
    # 사람이 읽는 안내는 예전처럼 stderr 에 그대로 남는다.
    assert "없다" in capsys.readouterr().err


#------------------------------------------------------------------
# 인자 해석 실패도 계약에 태운다 — argparse 의 종료코드 2 를 쓰지 않는다
#=> argparse 기본값 2 는 우리 표의 2(임베딩 실패)와 뜻이 겹친다. 받는 쪽이 그 2 를
#   보고 "모델이 없구나" 하고 재설치를 안내하면 실제 원인(오타)과 다른 대응을 한다.
#
# -in: 없음
# -out: 없음(단언)
# -out: error = 없음
#------------------------------------------------------------------
@pytest.mark.parametrize("argv, kind", [
    (["--dir", ".", "--axis", "secuirty"], "bad_axis"),
    (["--dir", ".", "--rule-only", "--vector-only"], "mode_conflict"),
])
def test_argparse_오류도_인자오류3_이다(argv, kind, capsys):
    errcodes.enable_json(True)
    try:
        with pytest.raises(SystemExit) as ex:
            cli.build_parser().parse_args(argv)
        out = capsys.readouterr().out.strip()
    finally:
        errcodes.enable_json(False)
    assert ex.value.code == 3
    assert json.loads(out)["error"]["kind"] == kind


#------------------------------------------------------------------
# --failsafe 로 준 등급이 두 엔진에서 똑같이 적용된다
#=> Rust 판은 값을 읽지 않고 늘 "S" 를 박았다(2026-09-01 수정). 규칙으로 못 정한
#   문서에 다른 등급이 조용히 들어가던 것이라, 종료코드가 아니라 '결과'가 틀렸다.
#   번호만 맞추는 테스트로는 못 잡으므로 실제 등급을 맞춰 본다.
#
#   [왜 합성 문서를 쓰나] 예전에는 저장소의 doc/*.html 을 훑었다. 그런데 그 폴더가
#   PII 예시로 가득한 문서들로 채워지면서 12건 전부 규칙에 걸리게 됐고, failsafe 가
#   아예 발동하지 않았다. 더 나쁜 것은 [C]·[S] 가 그때도 통과했다는 점이다 —
#   규칙이 매긴 등급에 마침 C 와 S 가 있어서였지 failsafe 때문이 아니었다.
#   즉 이 시험은 한동안 아무것도 검증하지 못하면서 초록불이었다(2026-09-08 발견).
#   그래서 신호가 하나도 없는 문서를 직접 만들어 쓴다 — 저장소 문서 내용이 바뀌어도
#   흔들리지 않는다.
#
#   [method 까지 보는 이유] 등급만 보면 규칙이 우연히 같은 등급을 냈을 때 또 통과한다.
#   failsafe 로 정해졌을 때만 나오는 method("failsafe_default")를 함께 단언해,
#   '규칙이 정한 등급'으로는 절대 통과할 수 없게 만든다.
#
# -in: grade    = 시험할 failsafe 등급(C·O·S)
# -in: tmp_path = pytest 임시 폴더
#
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 skip
#------------------------------------------------------------------
@pytest.mark.parametrize("grade", ["C", "O", "S"])
def test_두_엔진이_같은_failsafe_등급을_준다(grade, tmp_path):
    if not os.path.isfile(RS_EXE):
        pytest.skip("Rust exe 없음(cargo build --release 먼저)")
    # 규칙에 걸릴 만한 것이 하나도 없는 본문. PII·기밀어·스탬프 어느 것도 없고,
    # 파일명도 기밀사전에 안 걸리는 이름으로 둔다(파일명은 Signal D 가 본다).
    doc = tmp_path / "plain.txt"
    doc.write_text(
        "봄이 오면 마당에 심은 나무에 새 잎이 돋는다." + chr(10) +
        "아침마다 물을 주며 자라는 모습을 지켜보는 일이 즐겁다." + chr(10) +
        "여름에는 그늘이 넓어져 앉아 쉬기에 좋다." + chr(10),
        encoding="utf-8")

    rules = os.path.join(ROOT, "resources", "policy", "cso_rule.yaml")
    got = {}
    for name, cmd in (("python", [sys.executable, "-m", "csoclassify"]),
                      ("rust", [RS_EXE])):
        env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"),
                   PYTHONIOENCODING="utf-8")
        r = subprocess.run(cmd + ["--file", str(doc),
                                  "--format", "jsonl", "--rule-only",
                                  "--cso-rules", rules, "--failsafe", grade],
                           cwd=ROOT, env=env, capture_output=True, text=True,
                           encoding="utf-8")
        recs = [json.loads(l) for l in r.stdout.splitlines()
                if l.strip().startswith("{") and '"file"' in l]
        assert recs, f"{name}: 레코드가 없다 :: {r.stderr[-400:]}"
        got[name] = recs[0]

    # 두 판이 같은 답을 내는가 — 이 시험이 처음 잡으려던 '늘 S' 버그가 여기 걸린다.
    assert got["python"]["grade"] == got["rust"]["grade"]
    for name, rec in got.items():
        sec = rec["why"]["security"]
        assert rec["grade"] == grade, f"{name}: {sec['grade']} != {grade}"
        # 규칙이 아니라 failsafe 가 정했는가. 이 줄이 없으면 저장소 내용이 바뀌었을 때
        # 규칙이 매긴 등급으로 조용히 통과한다(예전에 실제로 그랬다).
        assert sec.get("method") == "failsafe_default",             f"{name}: failsafe 가 아니라 {sec.get('method')} 로 정해졌다"


#------------------------------------------------------------------
# --json-errors 를 주면 오류 안내를 stderr 로 되풀이하지 않는다
#=> 부르는 쪽이 두 갈래를 합쳐 받으면(2>&1 · stderr=STDOUT) JSON 뒤에 사람용
#   문장이 따라붙어 파싱이 깨진다. 옵션을 준 호출은 "기계가 읽겠다"는 뜻이므로
#   사람용 줄을 뺀다. 옵션이 없으면 예전 그대로 stderr 로 나가야 한다.
#
# -in: 없음
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 rust 쪽은 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_json_errors면_stderr에_오류를_되풀이하지_않는다(engine):
    if engine == "rust" and not os.path.isfile(RS_EXE):
        pytest.skip("Rust exe 없음(cargo build --release 먼저)")
    cmd = [RS_EXE] if engine == "rust" else [sys.executable, "-m", "csoclassify"]
    # 정책 파일 위치를 못 찾으면 "축을 건너뜁니다" 경고가 stderr 로 섞여, 이
    # 테스트가 무엇을 보는지 흐려진다(경고는 오류가 아니라 옵션과 무관하게 나간다).
    # 두 엔진 모두 이 환경변수로 정책 폴더를 잡는다.
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"),
               PYTHONIOENCODING="utf-8",
               CSOCLASSIFY_POLICY_DIR=os.path.join(ROOT, "resources", "policy"))
    cwd = ROOT
    argv = ["--dir", "D:/없는폴더_zzz"]

    on = subprocess.run(cmd + argv + ["--json-errors"], cwd=cwd, env=env,
                        capture_output=True, text=True, encoding="utf-8")
    assert on.stderr.strip() == "", f"stderr 에 무언가 남았다: {on.stderr!r}"
    assert json.loads(on.stdout.strip())["error"]["code"] == 1001

    off = subprocess.run(cmd + argv, cwd=cwd, env=env,
                         capture_output=True, text=True, encoding="utf-8")
    assert "처리할 파일이 없습니다" in off.stderr, "옵션이 없으면 예전처럼 나가야 한다"
    assert off.stdout.strip() == "", "옵션이 없으면 stdout 에 JSON 을 내지 않는다"


#------------------------------------------------------------------
# Rust 표를 읽어 파이썬 표와 한 줄씩 맞춘다
#=> 두 엔진이 같은 상황에서 다른 번호를 내면 계약이 무의미해진다. 표를 한쪽만
#   고치는 실수를 여기서 잡는다(빌드 없이 소스만 읽으므로 항상 돌 수 있다).
#
# -in: 없음
# -out: 없음(단언)
# -out: error = Rust 소스가 없으면 skip
#------------------------------------------------------------------
def test_두_엔진의_표가_같다():
    if not os.path.isfile(RS_SRC):
        pytest.skip("Rust 소스 없음")
    src = open(RS_SRC, encoding="utf-8").read()
    body = src.split("pub const ERRORS", 1)[1].split("];", 1)[0]
    rust = {m.group(1): (int(m.group(2)), int(m.group(3)))
            for m in re.finditer(r'\("([a-z_]+)",\s*(\d+),\s*(\d+)\)', body)}
    assert rust == errcodes.ERRORS


#------------------------------------------------------------------
# 같은 상황에서 두 엔진이 같은 code 를 낸다 (실제 실행)
#=> 표가 같아도 '어느 자리에서 어떤 이름을 고르는가'가 어긋나면 소용이 없다.
#   그래서 표 대조와 별개로 실제로 두 프로세스를 돌려 맞춰 본다.
#
# -in: 없음
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 skip
#------------------------------------------------------------------
@pytest.mark.parametrize("argv, code", [
    ([], 1002),                                             # 대상을 아예 안 줌
    (["--dir", "D:/없는폴더_zzz"], 1001),                   # 줬는데 0건
    (["--propagate", "D:/없는입력_zzz.jsonl"], 1008),       # 전파 입력 없음
    (["--build-doc-rule", "--export-input", "D:/없는원본_zzz.json"], 2007),
    # 2026-09-22 에 없앤 옵션 — 조용히 무시하지 않고 두 엔진이 같은 번호로 막는다.
    (["--export-taxonomy"], 1012),
    (["--taxonomy", "x.yaml", "--check-rules"], 1012),
    # Rust 판은 --failsafe 값을 통째로 무시해(늘 S) 이 오류가 아예 안 났다.
    # 2026-09-01 에 고쳤고, 이 줄이 두 엔진이 같이 막는지를 지킨다.
    (["--failsafe", "X"], 1005),
    # --file 로 없는 경로를 주면 '추출 실패' 레코드를 만들어 결과처럼 내보내고
    # 있었다(2026-09-01 수정). 대상이 없는 것은 결과가 아니라 부른 쪽의 문제다.
    (["--file", "D:/없는파일_zzz.hwp"], 1001),
])
def test_두_엔진이_같은_code를_낸다(argv, code, tmp_path):
    if not os.path.isfile(RS_EXE):
        pytest.skip("Rust exe 없음(cargo build --release 먼저)")
    got = {}
    for name, cmd in (("python", [sys.executable, "-m", "csoclassify"]),
                      ("rust", [RS_EXE])):
        # 정책 폴더를 짚어 준다 — 안 그러면 Rust 판은 규칙셋을 못 찾아(exe 옆에
        # 없다) 이 시험이 보려는 것과 다른 오류가 난다.
        env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"),
                   PYTHONIOENCODING="utf-8",
                   CSOCLASSIFY_POLICY_DIR=os.path.join(ROOT, "resources", "policy"))
        r = subprocess.run(cmd + argv + ["--json-errors"], cwd=ROOT, env=env,
                           capture_output=True, text=True, encoding="utf-8")
        # stdout 의 마지막 줄이 오류 JSON 이다(앞에 다른 출력이 있을 수 있다).
        line = [ln for ln in r.stdout.splitlines() if ln.startswith('{"error"')]
        assert line, f"{name}: 오류 JSON 이 안 나왔다\n{r.stdout}\n{r.stderr}"
        err = json.loads(line[-1])["error"]
        got[name] = (err["code"], r.returncode)
    assert got["python"] == got["rust"] == (code, errcodes.exit_of(
        next(k for k, (c, _) in errcodes.ERRORS.items() if c == code)))


#------------------------------------------------------------------
# 실행해서 상태 줄만 뽑아 오는 도우미
#=> 아래 시험들이 같은 방식으로 stdout 의 마지막 상태 줄을 읽게 한다.
#
# -in: engine = "python" | "rust"
# -in: argv   = 인자 목록(--json-errors 는 여기서 붙인다)
#
# -out: dict = {"code":…, "kind":…, …}
# -out: error = 상태 줄이 없으면 AssertionError
#------------------------------------------------------------------
def _run_status(engine, argv):
    cmd = [RS_EXE] if engine == "rust" else [sys.executable, "-m", "csoclassify"]
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"),
               PYTHONIOENCODING="utf-8",
               CSOCLASSIFY_POLICY_DIR=os.path.join(ROOT, "resources", "policy"))
    r = subprocess.run(cmd + argv + ["--json-errors"], cwd=ROOT, env=env,
                       capture_output=True, text=True, encoding="utf-8")
    lines = [l for l in r.stdout.splitlines() if l.startswith('{"error"')]
    assert lines, f"상태 줄이 없다 :: {r.stdout[:200]} / {r.stderr[:300]}"
    st = json.loads(lines[-1])["error"]
    st["_exit"] = r.returncode
    return st


#------------------------------------------------------------------
# 성공이어도 마지막 한 줄이 나간다
#=> 실패했을 때만 줄이 나가면 부르는 쪽은 '줄이 없음'을 성공으로 읽어야 한다.
#   그건 프로세스가 조용히 죽은 경우와 구분되지 않는다. 그래서 언제나 낸다.
#
# -in: 없음
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 rust 쪽은 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_성공해도_상태줄이_나온다(engine, tmp_path):
    if engine == "rust" and not os.path.isfile(RS_EXE):
        pytest.skip("Rust exe 없음(cargo build --release 먼저)")
    doc = tmp_path / "안내문.txt"
    doc.write_text("이 문서는 사내 규정에 따른 일반 안내문입니다. " * 5, encoding="utf-8")
    st = _run_status(engine, ["--dir", str(tmp_path), "--rule-only",
                              "--format", "jsonl", "--out", str(tmp_path / "o.jsonl")])
    assert st["code"] == 0 and st["kind"] == "success"
    assert (st["total"], st["failed"]) == (1, 0)


#------------------------------------------------------------------
# 부분 실패는 결과를 살리고 숫자로 알린다
#=> 16건 중 1건을 못 읽었다고 15건을 버릴 수는 없다. 결과는 만들고(종료 1)
#   몇 건인지 숫자로 준다 — 그 숫자로 무엇을 할지는 부르는 쪽이 정한다.
#
# -in: 없음
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 rust 쪽은 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_부분실패는_건수로_알린다(engine, tmp_path):
    if engine == "rust" and not os.path.isfile(RS_EXE):
        pytest.skip("Rust exe 없음(cargo build --release 먼저)")
    (tmp_path / "정상.txt").write_text("사내 규정에 따른 일반 안내문입니다. " * 5,
                                      encoding="utf-8")
    (tmp_path / "스캔본.txt").write_text("〮 ∽ …", encoding="utf-8")
    out = tmp_path / "o.jsonl"
    st = _run_status(engine, ["--dir", str(tmp_path), "--rule-only",
                              "--format", "jsonl", "--out", str(out)])
    assert st["code"] == 3001 and st["kind"] == "extract_failed"
    assert (st["total"], st["failed"]) == (2, 1)
    # 결과 파일은 버리지 않는다 — 읽은 문서는 그대로 살아 있어야 한다.
    lines = [l for l in out.read_text(encoding="utf-8").splitlines()
             if '"file"' in l]
    assert len(lines) == 2, lines


#------------------------------------------------------------------
# 상태 줄은 언제나 '마지막 한 줄'이다
#=> 두 줄이 나가면 마지막 줄만 읽는 쪽이 엉뚱한 것을 본다. 실패했을 때 오류 줄을
#   냈으면 끝에서 또 내지 않아야 한다.
#
# -in: 없음
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 rust 쪽은 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_상태줄은_한_줄뿐이다(engine, tmp_path):
    if engine == "rust" and not os.path.isfile(RS_EXE):
        pytest.skip("Rust exe 없음(cargo build --release 먼저)")
    cmd = [RS_EXE] if engine == "rust" else [sys.executable, "-m", "csoclassify"]
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"),
               PYTHONIOENCODING="utf-8",
               CSOCLASSIFY_POLICY_DIR=os.path.join(ROOT, "resources", "policy"))
    r = subprocess.run(cmd + ["--file", str(tmp_path / "없다.hwp"), "--json-errors"],
                       cwd=ROOT, env=env, capture_output=True, text=True,
                       encoding="utf-8")
    lines = [l for l in r.stdout.splitlines() if l.startswith('{"error"')]
    assert len(lines) == 1, lines
    assert json.loads(lines[0])["error"]["code"] == 1001


#------------------------------------------------------------------
# --json-errors 면 stderr 에 기계용 줄만 남는다
#=> [본문없음]·[전파] 같은 사람용 알림이 섞이면 로그가 시끄럽고, 두 갈래를 합쳐
#   받는 쪽은 파싱이 흔들린다. 진행바가 읽는 [progress] 와 완료 메시지가 쓰는
#   [summary] 만 남긴다.
#
# -in: 없음
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 rust 쪽은 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_json이면_stderr에_기계용_줄만(engine, tmp_path):
    if engine == "rust" and not os.path.isfile(RS_EXE):
        pytest.skip("Rust exe 없음(cargo build --release 먼저)")
    (tmp_path / "스캔본.txt").write_text("〮 ∽ …", encoding="utf-8")
    cmd = [RS_EXE] if engine == "rust" else [sys.executable, "-m", "csoclassify"]
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"),
               PYTHONIOENCODING="utf-8",
               CSOCLASSIFY_POLICY_DIR=os.path.join(ROOT, "resources", "policy"))
    argv = ["--dir", str(tmp_path), "--rule-only", "--progress",
            "--format", "jsonl", "--out", str(tmp_path / "o.jsonl")]

    on = subprocess.run(cmd + argv + ["--json-errors"], cwd=ROOT, env=env,
                        capture_output=True, text=True, encoding="utf-8")
    bad = [l for l in on.stderr.splitlines()
           if l.strip() and not l.startswith(("[progress]", "[summary]"))]
    assert not bad, bad
    # 진행바가 읽는 줄은 살아 있어야 한다 — 여기까지 지우면 화면이 멈춘다.
    assert any(l.startswith("[progress]") for l in on.stderr.splitlines())

    off = subprocess.run(cmd + argv, cwd=ROOT, env=env,
                         capture_output=True, text=True, encoding="utf-8")
    assert "본문" in off.stderr, "옵션이 없으면 사람용 알림이 그대로 나가야 한다"


#------------------------------------------------------------------
# --out 으로 저장하면 결과 파일에도 상태가 남는다
#=> 파일만 받아 나중에 읽는 쪽은 stdout 을 이미 흘려보낸 뒤다. 파일 자체가
#   "이 결과가 온전한가"를 말해 줘야 한다. json 은 배열의 마지막 원소로,
#   jsonl 은 마지막 줄로 넣는다 — summary 와 같은 자리라 새 규약이 아니다.
#
# -in: 없음
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 rust 쪽은 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["python", "rust"])
@pytest.mark.parametrize("fmt", ["jsonl", "json"])
def test_결과파일에도_상태가_남는다(engine, fmt, tmp_path):
    if engine == "rust" and not os.path.isfile(RS_EXE):
        pytest.skip("Rust exe 없음(cargo build --release 먼저)")
    (tmp_path / "정상.txt").write_text("사내 규정에 따른 일반 안내문입니다. " * 5,
                                      encoding="utf-8")
    (tmp_path / "스캔본.txt").write_text("〮 ∽ …", encoding="utf-8")
    out = tmp_path / f"r.{fmt}"
    st = _run_status(engine, ["--dir", str(tmp_path), "--rule-only",
                              "--format", fmt, "--out", str(out)])
    assert st["code"] == 3001

    text = out.read_text(encoding="utf-8")
    if fmt == "json":
        # 배열 '밖'에 객체를 붙이면 파일이 통째로 깨진다 — 여기서 그것을 막는다.
        arr = json.loads(text)
        last = arr[-1]
    else:
        last = json.loads(text.strip().splitlines()[-1])
    assert "error" in last, last
    assert last["error"]["code"] == 3001
    assert (last["error"]["total"], last["error"]["failed"]) == (2, 1)


#------------------------------------------------------------------
# --out 이 없으면 같은 줄이 두 번 나가지 않는다
#=> 결과가 stdout 으로 나가는데 거기에도 상태를 넣으면, 마지막 줄만 읽는 쪽은
#   문제없지만 줄 수를 세는 쪽이 어긋난다. 상태 줄은 언제나 하나여야 한다.
#
# -in: 없음
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 rust 쪽은 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_out없이_stdout이면_상태줄은_하나(engine, tmp_path):
    if engine == "rust" and not os.path.isfile(RS_EXE):
        pytest.skip("Rust exe 없음(cargo build --release 먼저)")
    (tmp_path / "스캔본.txt").write_text("〮 ∽ …", encoding="utf-8")
    cmd = [RS_EXE] if engine == "rust" else [sys.executable, "-m", "csoclassify"]
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"),
               PYTHONIOENCODING="utf-8",
               CSOCLASSIFY_POLICY_DIR=os.path.join(ROOT, "resources", "policy"))
    r = subprocess.run(cmd + ["--dir", str(tmp_path), "--rule-only", "--simple",
                              "--format", "jsonl", "--json-errors"],
                       cwd=ROOT, env=env, capture_output=True, text=True,
                       encoding="utf-8")
    lines = [l for l in r.stdout.splitlines() if l.startswith('{"error"')]
    assert len(lines) == 1, lines


#------------------------------------------------------------------
# 상태 줄에 정책 버전이 실린다 (④)
#=> 결과만 있고 '어떤 규칙으로 판정했는지'가 없으면 반년 뒤에 재현할 수 없다.
#   --simple --nosummary 를 함께 쓰면 버전이 지금까지 어디에도 안 남았다 —
#   모르는 것보다 '그새 규칙이 바뀐 줄 모르고 지금 규칙으로 재현해 보고 맞다고
#   하는 것'이 더 위험하다.
#
# -in: 없음
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 rust 쪽은 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_상태줄에_정책_버전이_실린다(engine, tmp_path):
    if engine == "rust" and not os.path.isfile(RS_EXE):
        pytest.skip("Rust exe 없음(cargo build --release 먼저)")
    (tmp_path / "안내문.txt").write_text("사내 규정에 따른 일반 안내문입니다. " * 5,
                                        encoding="utf-8")
    st = _run_status(engine, ["--dir", str(tmp_path), "--rule-only", "--simple",
                              "--nosummary", "--format", "jsonl",
                              "--out", str(tmp_path / "o.jsonl")])
    assert st["rule_version"], st
    assert st["taxonomy_version"], st
    assert st["doctype_rule_version"], st


#------------------------------------------------------------------
# --simple 이면 감사용 전체 파일이 옆에 함께 생긴다 (①)
#=> 축약본 4칸만으로는 "왜 이 등급인가"를 나중에 댈 수 없다. 분류를 다시 하는
#   것이 아니라 손에 든 레코드를 한 번 더 적는 것이라 비용이 사실상 없다
#   (실측 — 분류 2,320ms 대 0.27ms).
#   --out 이 가리키는 파일은 지금까지처럼 축약본이어야 한다(계약 유지).
#
# -in: 없음
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 rust 쪽은 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_simple이면_감사용_전체파일이_함께_생긴다(engine, tmp_path):
    if engine == "rust" and not os.path.isfile(RS_EXE):
        pytest.skip("Rust exe 없음(cargo build --release 먼저)")
    (tmp_path / "안내문.txt").write_text("사내 규정에 따른 일반 안내문입니다. " * 5,
                                        encoding="utf-8")
    out = tmp_path / "r.jsonl"
    _run_status(engine, ["--dir", str(tmp_path), "--rule-only", "--simple",
                         "--nosummary", "--format", "jsonl", "--out", str(out)])
    full = tmp_path / "r.full.jsonl"
    assert full.is_file(), "감사용 전체 파일이 없다"

    def recs(p):
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
                if l.strip().startswith("{") and '"file"' in l]
    simple, whole = recs(out), recs(full)
    # --out 은 지금까지처럼 축약본이어야 한다 — 읽고 있는 쪽의 계약을 바꾸지 않는다.
    assert set(simple[0]) <= {"file", "grade", "hash", "doctype", "doc_id",
                              "error", "why"}
    # 전체 파일에는 근거가 통째로 들어 있어야 한다.
    # [2026-09-10] 근거는 그 축 안(security.signals)에, 버전은 meta 에 있다.
    assert "signals" in whole[0]["why"]["security"]
    # [2026-09-10 오후] 버전은 레코드가 아니라 맨 앞 실행 헤더 한 줄에 있다.
    head = [json.loads(l) for l in full.read_text(encoding="utf-8").splitlines()
            if l.strip().startswith("{") and '"run"' in l]
    assert head and "rule_version" in head[0]["run"], head
    assert len(simple) == len(whole)
    # 연동용에 --nosummary 를 줘도 감사용에는 요약이 남는다(정책 버전이 필요하다).
    assert '"summary"' in full.read_text(encoding="utf-8")
    assert '"summary"' not in out.read_text(encoding="utf-8")


#------------------------------------------------------------------
# --simple-why 는 근거 요약만 얹는다 (③)
#=> 전체를 주면 26배로 커진다. 판정에 실제로 쓰인 것만 추린다.
#   매칭된 원문 값은 넣지 않는다 — 이 도구의 불변식이다.
#
# -in: 없음
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 rust 쪽은 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_simple_why는_근거_요약을_얹는다(engine, tmp_path):
    if engine == "rust" and not os.path.isfile(RS_EXE):
        pytest.skip("Rust exe 없음(cargo build --release 먼저)")
    # 규칙에 확실히 걸리는 문서 — 근거가 비면 이 시험이 아무것도 못 본다.
    (tmp_path / "대외비문서.txt").write_text(
        "대외비\n이 문서는 영업비밀과 소스코드를 담고 있습니다.\n" * 5, encoding="utf-8")
    out = tmp_path / "r.jsonl"
    _run_status(engine, ["--dir", str(tmp_path), "--rule-only", "--simple-why",
                         "--nosummary", "--format", "jsonl", "--out", str(out)])
    rec = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()
           if '"file"' in l][0]
    why = rec["why"]
    # 축별로 묶여 있어야 한다 — 평평하면 보안등급의 conf 를 업무분류 값으로 오해한다.
    sec = why["security"]
    assert sec["by"], why           # 어느 신호가 정했는가
    assert sec["conf"] is not None  # 그 판정의 확신(= labels.security.confidence)
    assert sec["hits"], why         # 어느 규칙이 몇 건
    for h in sec["hits"]:
        assert set(h) <= {"sig", "id", "n", "src"}, f"근거에 군더더기가 있다: {h}"
    # 원문 값이 새면 안 된다 — 규칙 id 와 건수만 남긴다.
    blob = json.dumps(why, ensure_ascii=False)
    assert "영업비밀" not in blob and "소스코드" not in blob, blob


#------------------------------------------------------------------
# 업무분류 신뢰도에는 '어디서 나왔는가'가 붙는다
#=> 같은 0.91 이라도 뜻이 다르다 — rule 은 "정한 낱말이 제목·머리·파일명 여러
#   군데서 나왔다", embed 는 "이미 분류해 둔 기준 문서와 닮았다". 검토자가 다르게
#   봐야 하는 값인데, 처음에는 dc 와 c 만 넣어 구분할 수 없었다.
#
# -in: 없음
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 rust 쪽은 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_업무분류_신뢰도에_판정경로가_붙는다(engine, tmp_path):
    if engine == "rust" and not os.path.isfile(RS_EXE):
        pytest.skip("Rust exe 없음(cargo build --release 먼저)")
    # 규칙으로 잡히는 이름 — doc_rule.yaml 의 '사내규정' 어휘에 걸린다.
    (tmp_path / "연구소안전관리규정.txt").write_text(
        "연구소안전관리규정\n제1조 이 규정은 안전관리에 관한 사항을 정한다.\n" * 5,
        encoding="utf-8")
    out = tmp_path / "r.jsonl"
    _run_status(engine, ["--dir", str(tmp_path), "--rule-only", "--simple-why",
                         "--nosummary", "--format", "jsonl", "--out", str(out)])
    rec = json.loads([l for l in out.read_text(encoding="utf-8").splitlines()
                      if '"file"' in l][0])
    # 칸 차례도 못박는다 — 무엇을(file·hash·doc_id) → 어떻게 됐나(grade·doctype) →
    # 왜(why). doc_id 는 '이 문서가 무엇인가'라서 판정값 앞이다(2026-09-07 옮김).
    # 두 판이 같은 차례로 내야 결과 파일을 그대로 견줄
    # 수 있다(Rust 는 기본값이 사전순이라 Cargo.toml 에 preserve_order 를 켜 두었다).
    # 여기서는 --filelist 를 안 줬으므로 doc_id 는 폴백이라 값이 None 이다.
    assert list(rec) == ["file", "hash", "doc_id", "grade", "doctype", "why"], list(rec)
    assert rec["doc_id"] is None, rec["doc_id"]
    assert list(rec["why"]) == ["security", "doctype"], list(rec["why"])
    assert list(rec["why"]["security"]) == ["by", "conf", "hits"], rec["why"]["security"]
    dt = (rec.get("why") or {}).get("doctype")
    assert dt, rec
    for v in dt:
        assert set(v) == {"dc", "c", "by"}, v
        assert v["by"] in ("rule", "embed"), v
    # --rule-only 로 돌렸으니 임베딩이 관여할 수 없다.
    assert all(v["by"] == "rule" for v in dt), dt


#------------------------------------------------------------------
# T7·T8 — --textsave 를 두 판이 같은 규칙으로 처리한다
#=> Rust 판은 예전에 --save-text 를 삼키고 아무 일도 안 했다. 오류도 경고도 없이
#   폴더가 비어 있어서, 사용자는 "저장했는데 왜 없지"를 혼자 헤맸다. 그 조용한
#   실패가 되살아나지 않게 못박는다.
#
#   [본문이 두 판에서 같지 않은 것은 정상이다] 저장하는 것은 원본이 아니라
#   *추출한 본문*이고 두 판은 파서가 다르다(실측: 같은 pptx 가 9,312 / 9,330바이트).
#   그래서 파일 '내용'이 아니라 규칙 — 이름·색인 칸·표식 — 이 같은지를 본다.
#
# -in: engine   = "python" | "rust"
# -in: tmp_path = pytest 임시 폴더
#
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_textsave가_본문을_해시이름으로_남긴다(engine, tmp_path):
    if engine == "rust" and not os.path.isfile(RS_EXE):
        pytest.skip("Rust exe 없음(cargo build --release 먼저)")
    (tmp_path / "규정.txt").write_text(
        "연구소안전관리규정\n제1조 이 규정은 안전관리에 관한 사항을 정한다.\n" * 5,
        encoding="utf-8")
    save_dir = tmp_path / "본문"
    out = tmp_path / "r.jsonl"
    _run_status(engine, ["--dir", str(tmp_path), "--rule-only", "--nosummary",
                         "--textsave", str(save_dir),
                         "--format", "jsonl", "--out", str(out)])

    rec = json.loads([l for l in out.read_text(encoding="utf-8").splitlines()
                      if '"file"' in l][0])
    # 이름이 레코드의 hash 와 이어져야 "결과 한 줄에서 본문으로" 갈 수 있다.
    assert rec["text_saved"] == rec["hash"] + ".txt", rec
    saved = save_dir / rec["text_saved"]
    assert saved.is_file(), sorted(p.name for p in save_dir.iterdir())
    assert "연구소안전관리규정" in saved.read_text(encoding="utf-8")

    # 색인 — 칸 이름·차례가 두 판에서 같아야 한 파일로 섞어 읽을 수 있다.
    rows = [json.loads(l) for l in
            (save_dir / "_index.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1, rows
    assert list(rows[0]) == ["hash", "txt", "file", "doc_id", "chars", "truncated", "ts"], rows[0]
    assert rows[0]["hash"] == rec["hash"]
    assert rows[0]["txt"] == rec["text_saved"]
    assert rows[0]["truncated"] is False
    # --filelist 를 안 줬으니 폴백이다 — 폴백 doc_id 는 싣지 않는다.
    assert rows[0]["doc_id"] is None, rows[0]

    # 폴더만 발견한 사람에게 경고가 닿아야 한다.
    assert (save_dir / "_README.md").is_file()


#------------------------------------------------------------------
# 옵션을 안 주면 두 판 모두 아무것도 남기지 않는다
#=> 기본 꺼짐은 프라이버시 장치다(설계 P4). 한쪽만 켜져 있으면 엔진을 바꾼 것만으로
#   개인정보가 디스크에 남는다.
#
# -in: engine   = "python" | "rust"
# -in: tmp_path = pytest 임시 폴더
#
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_textsave_없으면_아무것도_안_남긴다(engine, tmp_path):
    if engine == "rust" and not os.path.isfile(RS_EXE):
        pytest.skip("Rust exe 없음(cargo build --release 먼저)")
    (tmp_path / "규정.txt").write_text("연구소안전관리규정\n" * 5, encoding="utf-8")
    out = tmp_path / "r.jsonl"
    _run_status(engine, ["--dir", str(tmp_path), "--rule-only", "--nosummary",
                         "--format", "jsonl", "--out", str(out)])
    rec = json.loads([l for l in out.read_text(encoding="utf-8").splitlines()
                      if '"file"' in l][0])
    assert "text_saved" not in rec, rec
    assert not list(tmp_path.glob("**/_index.jsonl"))



# ════════════════════════════════════════════════════════════════════
# 2026-09-29 오류 계약 검토에서 나온 구멍들 — 두 엔진이 같은 번호를 내는지 지킨다
# ════════════════════════════════════════════════════════════════════

#------------------------------------------------------------------
# 한 엔진을 돌려 (종료코드, stdout 의 오류/상태 줄 전부) 를 받는 도우미
#=> _run_status 는 '줄이 있다'를 전제로 한다. 여기서는 줄이 몇 개 나왔는지까지
#   봐야 하는 시험이 있어(상태 줄이 빠지던 구멍) 줄 목록을 그대로 돌려준다.
#
# -in: engine    = "python" | "rust"
# -in: argv      = 인자 목록(--json-errors 는 여기서 붙인다)
# -in: extra_env = 덧붙일 환경변수(기본 None)
#
# -out: (returncode, [dict, ...]) = 종료코드와 {"error":…} 줄들의 error 객체
# -out: error = 없음
#------------------------------------------------------------------
def _run_lines(engine, argv, extra_env=None):
    cmd = [RS_EXE] if engine == "rust" else [sys.executable, "-m", "csoclassify"]
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"),
               PYTHONIOENCODING="utf-8",
               CSOCLASSIFY_POLICY_DIR=os.path.join(ROOT, "resources", "policy"))
    env.update(extra_env or {})
    r = subprocess.run(cmd + argv + ["--json-errors"], cwd=ROOT, env=env,
                       capture_output=True, text=True, encoding="utf-8")
    lines = [json.loads(l)["error"] for l in r.stdout.splitlines()
             if l.startswith('{"error"')]
    return r.returncode, lines


#------------------------------------------------------------------
# Rust exe 가 없으면 rust 쪽 시험을 건너뛴다
#=> 매 시험 첫 줄에 같은 두 줄을 되풀이하지 않으려는 작은 도우미다.
#
# -in: engine = "python" | "rust"
#
# -out: 없음
# -out: error = rust 인데 exe 가 없으면 pytest.skip
#------------------------------------------------------------------
def _need(engine):
    if engine == "rust" and not os.path.isfile(RS_EXE):
        pytest.skip("Rust exe 없음(cargo build --release 먼저)")


#------------------------------------------------------------------
# 시험용 평범한 문서 하나 만들기
#=> 규칙에 걸리지 않고 본문 길이도 충분한 문서 — 여러 시험이 같은 것을 쓴다.
#
# -in: folder = 문서를 둘 폴더(pathlib.Path)
#
# -out: pathlib.Path = 만든 문서 경로
# -out: error = 없음
#------------------------------------------------------------------
def _plain_doc(folder):
    folder.mkdir(parents=True, exist_ok=True)
    doc = folder / "a.txt"
    doc.write_text("사내 규정에 따른 일반 안내문입니다. " * 5, encoding="utf-8")
    return doc


#------------------------------------------------------------------
# 적어 둔 까닭(set_status)이 종료코드 역추적보다 먼저다
#=> exit 2 에는 model_load_failed·embed_failed 두 이름이 있다. 역추적만 하면
#   언제나 표 앞쪽(3002)이 붙어, "기준 문서 0건 등록"이 "모델을 못 올렸다"로
#   보고됐다. 종료코드와 짝이 안 맞는 까닭은 쓰지 않는다(code 와 exit 가 어긋나면 안 된다).
#
# -in: monkeypatch = 모듈 상태를 시험 뒤에 되돌린다
#
# -out: 없음(단언)
# -out: error = 없음
#------------------------------------------------------------------
def test_적어둔_까닭이_역추적보다_먼저다(monkeypatch):
    monkeypatch.setattr(errcodes, "_STATUS", None)
    monkeypatch.setattr(errcodes, "_TOTAL", None)
    monkeypatch.setattr(errcodes, "_FAILED", None)
    errcodes.set_status("embed_failed", "3건 중 한 건도 등록하지 못했습니다")
    st = errcodes.status_object(2)["error"]
    assert (st["code"], st["kind"]) == (3003, "embed_failed")
    assert st["message"] == "3건 중 한 건도 등록하지 못했습니다"
    # 종료코드가 다르면(1) 적어 둔 까닭을 쓰지 않는다.
    assert errcodes.status_object(1)["error"]["kind"] != "embed_failed"
    assert errcodes.status_object(0)["error"]["kind"] == "success"
    # 표에 없는 이름은 개발 중 오타다 — 조용히 넘기지 않는다.
    with pytest.raises(KeyError):
        errcodes.set_status("no_such_kind")


#------------------------------------------------------------------
# 규칙셋 YAML 문법이 깨지면 두 엔진 모두 2002(종료 4)
#=> 파이썬 판은 YAMLError 가 그대로 올라가 internal_error(9001, 종료 1)로 나갔다.
#   종료 1 은 '결과는 있는데 일부 문서를 못 읽음'이라 배치가 결과가 있는 줄 알았다.
#   --check-rules 도 같은 로더를 쓰므로 함께 본다.
#
# -in: engine   = "python" | "rust"
# -in: mode     = 추가 인자(분류 / --check-rules)
# -in: tmp_path = pytest 임시 폴더
#
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("mode", [["--rule-only"], ["--check-rules"]])
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_깨진_규칙셋은_2002(engine, mode, tmp_path):
    _need(engine)
    bad = tmp_path / "cso_rule.yaml"
    bad.write_text("regex_pii: [\n", encoding="utf-8")
    _plain_doc(tmp_path / "docs")
    rc, lines = _run_lines(engine, ["--dir", str(tmp_path / "docs"),
                                    "--cso-rules", str(bad)] + mode)
    assert (rc, lines[-1]["code"], lines[-1]["kind"]) == (4, 2002, "rules_invalid"), lines
    assert lines[-1]["path"].replace("\\", "/").endswith("cso_rule.yaml")


#------------------------------------------------------------------
# 규칙셋 최상위가 매핑이 아니어도 V0 (파이썬 로더)
#=> YAML 로는 멀쩡한데 목록 하나뿐인 파일은 로더 안의 data.get 에서 AttributeError
#   로 죽어 9001 이 됐을 자리다. 내용 오류이므로 V0 로 알린다.
#
# -in: tmp_path = pytest 임시 폴더
#
# -out: 없음(단언)
# -out: error = 없음
#------------------------------------------------------------------
def test_규칙셋_최상위가_목록이면_V0(tmp_path):
    from csoclassify.classify.rules import load_rules, RuleSetValidationError
    p = tmp_path / "cso_rule.yaml"
    p.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(RuleSetValidationError) as ei:
        load_rules(str(p))
    assert ei.value.violations[0]["code"] == "V0"


#------------------------------------------------------------------
# --files-from 목록 파일이 없으면 두 엔진 모두 1001 + path
#=> 파이썬 판은 FileNotFoundError 가 그대로 올라가 9001 로 나갔고, Rust 판은 빈
#   목록으로 삼켜 "목록이 비었거나…" 라는 엉뚱한 안내를 냈다.
#
# -in: engine   = "python" | "rust"
# -in: tmp_path = pytest 임시 폴더
#
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_files_from_목록이_없으면_1001(engine, tmp_path):
    _need(engine)
    lst = str(tmp_path / "없는목록.txt")
    rc, lines = _run_lines(engine, ["--files-from", lst, "--rule-only"])
    assert (rc, [l["code"] for l in lines]) == (3, [1001]), lines
    assert lines[0]["path"] == lst


#------------------------------------------------------------------
# --text-only 도 같은 계약을 따른다 (0건 · 출력 실패 · 성공)
#=> Rust 판은 이 모드만 errcodes 를 거치지 않아, --json-errors 를 줘도 JSON 이
#   없었고 출력 파일 실패를 인자 오류(3)로 냈다. 성공해도 상태 줄이 없었다.
#
# -in: engine   = "python" | "rust"
# -in: tmp_path = pytest 임시 폴더
#
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_text_only도_계약을_따른다(engine, tmp_path):
    _need(engine)
    docs = tmp_path / "docs"
    _plain_doc(docs)

    # (1) 대상 0건
    rc, lines = _run_lines(engine, ["--text-only", "--dir", str(tmp_path / "없다")])
    assert (rc, [l["code"] for l in lines]) == (3, [1001]), lines

    # (2) 출력 파일을 못 씀 — 인자 오류가 아니라 4001(종료 1)
    #     상위가 '폴더가 아니라 파일'인 경로를 준다. 없는 폴더는 이제 만들어 주므로
    #     (아래 test_out의_상위_폴더가_없으면_만든다) 실패하는 경우가 못 된다.
    blocker = tmp_path / "막는파일.txt"
    blocker.write_text("x", encoding="utf-8")
    bad_out = str(blocker / "o.txt")
    rc, lines = _run_lines(engine, ["--text-only", "--dir", str(docs), "--out", bad_out])
    assert (rc, [l["code"] for l in lines]) == (1, [4001]), lines
    assert lines[0]["path"] == bad_out

    # (3) 성공 — 상태 줄 한 줄, 건수 포함
    rc, lines = _run_lines(engine, ["--text-only", "--dir", str(docs),
                                    "--out", str(tmp_path / "o.txt")])
    assert rc == 0 and len(lines) == 1, lines
    assert (lines[0]["kind"], lines[0]["total"], lines[0]["failed"]) == ("success", 1, 0)


#------------------------------------------------------------------
# --check-rules 가 성공해도 상태 줄이 나온다
#=> Rust 판은 검사 결과만 찍고 그냥 return 해 상태 줄이 없었다. 부르는 쪽은
#   '줄이 없음'을 성공으로 읽어야 했다 — 프로세스가 조용히 죽은 것과 구분이 안 된다.
#
# -in: engine = "python" | "rust"
#
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_check_rules_성공도_상태줄(engine):
    _need(engine)
    rc, lines = _run_lines(engine, ["--check-rules"])
    assert rc == 0 and len(lines) == 1, lines
    assert (lines[0]["code"], lines[0]["kind"]) == (0, "success")


#------------------------------------------------------------------
# Rust --status/--stop 도 상태 줄을 낸다
#=> 파이썬 판은 데몬 질의도 _main 을 거쳐 언제나 한 줄을 낸다. Rust 판만 exit(0)
#   으로 바로 끝나 줄이 없었다. (파이썬 --status 는 데몬 접속을 시도해 느려서 뺀다.)
#
# -in: flag = "--status" | "--stop"
#
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("flag", ["--status", "--stop"])
def test_rust_데몬질의도_상태줄(flag):
    _need("rust")
    rc, lines = _run_lines("rust", [flag])
    assert rc == 0 and [l["kind"] for l in lines] == ["success"], lines


#------------------------------------------------------------------
# --propagate 입력이 전부 깨졌으면 1008
#=> 두 엔진 모두 깨진 줄을 건너뛰고 빈 배열과 success 로 끝났다. 부르는 쪽은
#   "전파할 문서가 없었다"로 읽는다 — 실제로는 입력을 하나도 못 읽었는데.
#   멀쩡한 줄이 섞여 있으면 그 줄로 계속한다(일부 손상은 경고로 알린다).
#
# -in: engine   = "python" | "rust"
# -in: tmp_path = pytest 임시 폴더
#
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_전파입력이_전부_깨지면_1008(engine, tmp_path):
    _need(engine)
    bad = tmp_path / "r.jsonl"
    bad.write_text("not json\n{broken\n", encoding="utf-8")
    rc, lines = _run_lines(engine, ["--propagate", str(bad)])
    assert (rc, [l["code"] for l in lines]) == (3, [1008]), lines
    assert lines[0]["path"] == str(bad)

    # 멀쩡한 문서 줄이 하나라도 있으면 멈추지 않는다.
    bad.write_text('not json\n{"file":"a.txt","grade":"C"}\n', encoding="utf-8")
    rc, lines = _run_lines(engine, ["--propagate", str(bad), "--axis", "security"])
    assert rc == 0 and lines[-1]["kind"] == "success", lines


#------------------------------------------------------------------
# 모델을 못 올리면 — Rust (vector-only 는 멈춤 · 기본 모드는 결과 + 3002)
#=> 예전에는 두 경우 모두 exit 0·success 였다. 설계서 5장의 "3002 면 설치 안내"
#   분기가 한 번도 타지 않았다.
#    · --vector-only: 임베딩 비교만으로 등급을 정하므로 모델이 없으면 전 문서가
#      미분류다. 그런 결과는 쓸모가 없고 정상 결과처럼 적재될 위험만 있어, 문서를
#      읽기 전에 오류만 내고 멈춘다(결과 파일을 만들지 않는다).
#    · 기본 모드: 규칙 등급은 유효하므로 결과를 내고 종료코드 2 로 알린다.
#   CSO_MODEL 을 '빈 폴더'로 준다 — 없는 경로를 주면 exe 옆 모델로 되돌아간다.
#
# -in: tmp_path = pytest 임시 폴더
#
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 건너뛴다
#------------------------------------------------------------------
def test_rust_모델을_못_올리면_3002(tmp_path):
    _need("rust")
    docs = tmp_path / "docs"
    doc = _plain_doc(docs)
    seeds = tmp_path / "seed.jsonl"
    _seed_file(seeds)   # e5 계열(384차원) seed — 차원 검사(1013)에 걸리지 않게
    (tmp_path / "빈모델").mkdir()
    env = {"CSO_MODEL": str(tmp_path / "빈모델")}

    # (1) --vector-only — 오류 한 줄만, 결과 파일은 만들지 않는다.
    out = tmp_path / "vo.jsonl"
    rc, lines = _run_lines("rust", ["--dir", str(docs), "--vector-only", "--seeds", str(seeds),
                                    "--format", "jsonl", "--out", str(out)], env)
    assert (rc, [l["code"] for l in lines]) == (2, [3002]), lines
    assert "--vector-only" in lines[0]["message"]
    assert not out.exists(), "vector-only 는 모델이 없으면 결과를 내지 않아야 한다"

    # (2) 기본 모드 — 결과는 내되 종료코드 2 · 3002.
    out = tmp_path / "def.jsonl"
    rc, lines = _run_lines("rust", ["--dir", str(docs), "--seeds", str(seeds),
                                    "--format", "jsonl", "--out", str(out)], env)
    assert (rc, lines[-1]["code"], lines[-1]["kind"]) == (2, 3002, "model_load_failed"), lines
    assert any('"file"' in l for l in out.read_text(encoding="utf-8").splitlines())

    # (3) 기준 문서 등록도 모델이 없으면 0건 — 까닭은 3002 이지 3003 이 아니다.
    rc, lines = _run_lines("rust", ["--seed-add", str(doc), "--seed-grade", "C",
                                    "--seed-reviewer", "시험", "--seeds",
                                    str(tmp_path / "new_seed.jsonl"), "--axis", "security"], env)
    assert (rc, lines[-1]["code"]) == (2, 3002), lines
    assert (lines[-1]["total"], lines[-1]["failed"]) == (1, 1)


#------------------------------------------------------------------
# 파이썬 main 을 프로세스 안에서 돌리는 준비
#=> 파이썬 판은 모델 경로를 끌 환경변수가 없어(캐시·exe 옆 등 폴백이 여럿) 모듈을
#   바꿔 끼워 '모델 없음'을 만든다. main 이 바꾸는 전역(stderr·errcodes 상태)은
#   시험 뒤에 되돌린다.
#
# -in: monkeypatch = 전역을 시험 뒤에 되돌린다
# -in: tmp_path    = pytest 임시 폴더(오류 로그 자리)
#
# -out: (docs, seeds) = 평범한 문서 폴더, seed 파일 경로
# -out: error = 없음
#------------------------------------------------------------------
def _py_inprocess_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    for name, val in (("_JSON", False), ("_DONE", False), ("_TOTAL", None),
                      ("_FAILED", None), ("_STATUS", None), ("_VERSIONS", {})):
        monkeypatch.setattr(errcodes, name, val)
    monkeypatch.setenv("CSOCLASSIFY_POLICY_DIR", os.path.join(ROOT, "resources", "policy"))
    monkeypatch.setenv("CSOCLASSIFY_ERRLOG", str(tmp_path / "err.log"))
    docs = tmp_path / "docs"
    _plain_doc(docs)
    seeds = tmp_path / "seed.jsonl"
    _seed_file(seeds)   # e5 계열(384차원) seed — 차원 검사(1013)에 걸리지 않게
    return docs, seeds


#------------------------------------------------------------------
# 파이썬 main 을 돌려 (종료코드, 오류/상태 줄들) 받기
#=> 위 준비를 마친 뒤 부른다. --json-errors 는 여기서 붙인다.
#
# -in: argv   = 인자 목록
# -in: capsys = stdout 을 받는다
#
# -out: (code, [dict, ...]) = 종료코드와 {"error":…} 줄들의 error 객체
# -out: error = 없음
#------------------------------------------------------------------
def _py_inprocess_run(argv, capsys):
    code = cli.main(argv + ["--no-daemon", "--json-errors"])
    lines = [json.loads(l)["error"] for l in capsys.readouterr().out.splitlines()
             if l.startswith('{"error"')]
    return code, lines


#------------------------------------------------------------------
# 모델 파일이 없으면 --vector-only 는 문서를 읽기 전에 멈춘다 — Python
#=> 값싼 확인(_model_unavailable)이 모델 폴더에서 model.onnx 를 못 찾으면 3002 한 줄만
#   내고 끝난다. 임베더를 만들려는 시도조차 없어야 한다(가짜 임베더가 불리면 실패).
#
# -in: monkeypatch = 모델 폴더·임베더를 바꿔 끼운다
# -in: capsys      = stdout 을 받는다
# -in: tmp_path    = pytest 임시 폴더
#
# -out: 없음(단언)
# -out: error = 없음
#------------------------------------------------------------------
def test_python_vector_only_모델없으면_멈춘다(monkeypatch, capsys, tmp_path):
    from csoclassify import resources
    from csoclassify.embed import onnx_embedder
    docs, seeds = _py_inprocess_setup(monkeypatch, tmp_path)
    empty = tmp_path / "빈모델"
    empty.mkdir()
    monkeypatch.setattr(resources, "resolve_model_dir", lambda local_dir: str(empty))

    # 임베더가 불리면 '읽기 전에 멈춘다'는 약속이 깨진 것이다.
    class _MustNotLoad:
        def __init__(self, *a, **k):
            raise AssertionError("vector-only 사전 확인이 임베더보다 먼저여야 한다")

    monkeypatch.setattr(onnx_embedder, "OnnxEmbedder", _MustNotLoad)
    out = tmp_path / "o.jsonl"
    code, lines = _py_inprocess_run(["--dir", str(docs), "--vector-only", "--seeds", str(seeds),
                                     "--axis", "security", "--format", "jsonl",
                                     "--out", str(out)], capsys)
    assert (code, [l["code"] for l in lines]) == (2, [3002]), lines
    assert "model.onnx" in lines[0]["message"]
    assert not any('"file"' in l for l in out.read_text(encoding="utf-8").splitlines())


#------------------------------------------------------------------
# 모델 파일은 있는데 로드가 실패해도 --vector-only 는 결과를 내지 않는다 — Python
#=> 깨진 모델·런타임 불일치처럼 값싼 확인을 통과한 드문 경우다. 임베더를 '늘 실패하는
#   가짜'로 바꿔 끼워 만든다. 결과가 전부 미분류라 내보내지 않고 3002 로 멈춘다.
#
# -in: monkeypatch = 임베더를 바꿔 끼운다
# -in: capsys      = stdout 을 받는다
# -in: tmp_path    = pytest 임시 폴더
#
# -out: 없음(단언)
# -out: error = 없음
#------------------------------------------------------------------
def test_python_vector_only_로드실패도_멈춘다(monkeypatch, capsys, tmp_path):
    from csoclassify.embed import onnx_embedder
    docs, seeds = _py_inprocess_setup(monkeypatch, tmp_path)
    # 사전 확인은 통과시킨다 — 파일은 있는 상황이다.
    monkeypatch.setattr(cli, "_model_unavailable", lambda model: None)

    class _Broken:
        def __init__(self, *a, **k):
            raise RuntimeError("model.onnx 깨짐(시험용)")

    monkeypatch.setattr(onnx_embedder, "OnnxEmbedder", _Broken)
    out = tmp_path / "o.jsonl"
    code, lines = _py_inprocess_run(["--dir", str(docs), "--vector-only", "--seeds", str(seeds),
                                     "--axis", "security", "--format", "jsonl",
                                     "--out", str(out)], capsys)
    assert (code, [l["code"] for l in lines]) == (2, [3002]), lines
    assert "model.onnx 깨짐" in lines[0]["message"]
    assert not any('"file"' in l for l in out.read_text(encoding="utf-8").splitlines())


#------------------------------------------------------------------
# 기본 모드는 모델을 못 올려도 결과를 내고 3002(종료 2) — Python
#=> 규칙으로 정해진 등급은 유효하므로 결과를 버리지 않는다. 예전에는 일반 로그
#   warning 한 줄뿐이라 화면에도 오류 로그에도 흔적이 없었고 exit 0 이었다.
#
# -in: monkeypatch = 임베더를 바꿔 끼운다
# -in: capsys      = stdout 을 받는다
# -in: tmp_path    = pytest 임시 폴더
#
# -out: 없음(단언)
# -out: error = 없음
#------------------------------------------------------------------
def test_python_기본모드는_결과내고_3002(monkeypatch, capsys, tmp_path):
    from csoclassify.embed import onnx_embedder
    docs, seeds = _py_inprocess_setup(monkeypatch, tmp_path)

    class _NoModel:
        def __init__(self, *a, **k):
            raise RuntimeError("model.onnx 없음(시험용)")

    monkeypatch.setattr(onnx_embedder, "OnnxEmbedder", _NoModel)
    out = tmp_path / "o.jsonl"
    code, lines = _py_inprocess_run(["--dir", str(docs), "--seeds", str(seeds),
                                     "--axis", "security", "--format", "jsonl",
                                     "--out", str(out)], capsys)
    assert (code, lines[-1]["code"], lines[-1]["kind"]) == (2, 3002, "model_load_failed"), lines
    assert "model.onnx 없음" in lines[-1]["message"]
    assert any('"file"' in l for l in out.read_text(encoding="utf-8").splitlines())


#------------------------------------------------------------------
# 기준 문서 등록이 끝나면 상태 줄 한 줄 — 건수는 '등록' 기준
#=> Rust 판은 등록 뒤 바로 exit 해 상태 줄이 없었고, 파이썬 판은 분류 단계의
#   건수를 그대로 실어 "success 인데 failed=1" 같은 어긋난 줄을 낼 수 있었다.
#
# -in: engine   = "python" | "rust"
# -in: tmp_path = pytest 임시 폴더
#
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_seed_add_성공도_상태줄(engine, tmp_path):
    _need(engine)
    doc = _plain_doc(tmp_path / "docs")
    rc, lines = _run_lines(engine, ["--seed-add", str(doc), "--seed-grade", "C",
                                    "--seed-reviewer", "시험", "--seeds",
                                    str(tmp_path / "seed.jsonl"), "--axis", "security"])
    assert rc == 0 and len(lines) == 1, lines
    assert (lines[0]["kind"], lines[0]["total"], lines[0]["failed"]) == ("success", 1, 0)



# ════════════════════════════════════════════════════════════════════
# 2026-09-29 재검토 8~11 — 중단 · stderr · 출력 경로 · 파이썬 전용 옵션
# ════════════════════════════════════════════════════════════════════

#------------------------------------------------------------------
# 두 판의 모델 표가 칸 하나까지 같은가
#=> 2026-09-29 부터 Rust 판도 세 모델(e5-small-ko · e5-small · ko-sroberta)을 --model 로
#   고른다. 같은 --model 인데 폴더·차원·프리픽스·토큰 상한이 하나라도 다르면 두 판이
#   다른 벡터를 만들고, 그 벡터로 만든 seed 가 서로 섞인다. 그래서 표 전체를 대조한다.
#
# -in: 없음
#
# -out: 없음(단언)
# -out: error = Rust 소스가 없으면 건너뛴다
#------------------------------------------------------------------
def test_두_판의_모델표가_같다():
    src_path = os.path.join(ROOT, "Rust", "src", "embed.rs")
    if not os.path.isfile(src_path):
        pytest.skip("Rust 소스 없음")
    from csoclassify import config
    src = open(src_path, encoding="utf-8").read()
    body = src.split("pub const MODELS", 1)[1].split("];", 1)[0]
    rust = [(m.group(1), m.group(2), int(m.group(3)), m.group(4), int(m.group(5)))
            for m in re.finditer(r'key:\s*"([^"]*)",\s*local_dir:\s*"([^"]*)",\s*dim:\s*(\d+),\s*'
                                 r'passage_prefix:\s*"([^"]*)",\s*max_tokens_model:\s*(\d+)', body)]
    py = [(s.key, s.local_dir, s.dim, s.passage_prefix, s.max_tokens_model)
          for s in config.MODELS.values()]
    assert rust == py
    # 기본 모델도 같아야 한다 — --model 을 안 준 호출이 두 판에서 같은 벡터를 내려면.
    assert re.search(r'DEFAULT_MODEL:\s*&str\s*=\s*"([^"]+)"', src).group(1) == config.DEFAULT_MODEL


#------------------------------------------------------------------
# 파이썬 전용 옵션 — 두 엔진이 같은 번호로 막는다(값 형식 오류 · 모르는 모델)
#=> 예전 Rust 판은 --model·--max-tokens·--precision 등의 값을 통째로 삼켜 늘 success 였다.
#   값 자체가 틀린 것(오타)은 두 판 모두 1003 이어야 한다.
#
# -in: engine   = "python" | "rust"
# -in: argv     = 덧붙일 인자
# -in: tmp_path = pytest 임시 폴더
#
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("argv", [
    ["--model", "없는모델"],
    ["--max-tokens", "abc"],
    ["--overlap", "x"],
    ["--precision", "f99"],
    ["--num-threads", "abc"],
])
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_파이썬전용_옵션_값오류는_1003(engine, argv, tmp_path):
    _need(engine)
    docs = tmp_path / "docs"
    _plain_doc(docs)
    rc, lines = _run_lines(engine, ["--dir", str(docs), "--rule-only"] + argv)
    assert (rc, [l["code"] for l in lines]) == (3, [1003]), lines


#------------------------------------------------------------------
# 결과(벡터)를 바꾸는 값을 이 판이 못 따르면 1012 — Rust
#=> --max-tokens·--overlap·--precision f16 은 파이썬 판에서 벡터를 바꾸는 값인데 이 판은
#   고정값만 쓴다 — 다른 값은 1012 로 멈춘다. 이 판이 쓰는 값(기본값)은 그대로 받는다.
#   (--model 은 2026-09-29 부터 세 모델 모두 실제로 고른다.)
#
# -in: argv     = 덧붙일 인자
# -in: code     = 기대하는 code(0 이면 성공)
# -in: tmp_path = pytest 임시 폴더
#
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("argv, code", [
    # 모델 고르기는 이제 받는다(2026-09-29 — 세 모델 모두). --rule-only 라 모델을 올리지는 않는다.
    (["--model", "ko-sroberta"], 0),
    (["--model", "e5-small"], 0),
    (["--max-tokens", "256"], 1012),
    (["--overlap", "64"], 1012),
    (["--precision", "f16"], 1012),
    (["--make-doctype-seeds", "out.jsonl"], 1012),
    # 이 판이 쓰는 값이면 받는다 — 파이썬 판과 같은 명령줄을 그대로 넘길 수 있어야 한다.
    (["--model", "e5-small-ko"], 0),
    (["--max-tokens", "512", "--overlap", "32", "--precision", "f32"], 0),
    (["--num-threads", "4", "--idle-timeout", "0"], 0),
])
def test_rust_벡터를_바꾸는_값은_1012(argv, code, tmp_path):
    _need("rust")
    docs = tmp_path / "docs"
    _plain_doc(docs)
    rc, lines = _run_lines("rust", ["--dir", str(docs), "--rule-only",
                                    "--out", str(tmp_path / "o.jsonl")] + argv)
    assert lines[-1]["code"] == code, lines
    assert rc == (0 if code == 0 else 3)


#------------------------------------------------------------------
# --json-errors 면 사람용 알림이 stderr 에 새지 않는다 (기본 모드 · --textsave)
#=> Rust 판은 note! 를 거치지 않는 eprintln! 15곳이 남아, "전파용 seed 파일이 없어…"·
#   "--textsave: 추출 본문(개인정보…)" 같은 줄이 --json-errors 에서도 나갔다.
#   실행가이드 6장의 약속은 "[progress]·[summary] 만 나간다" 이다.
#
# -in: engine   = "python" | "rust"
# -in: tmp_path = pytest 임시 폴더
#
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_json이면_경고도_stderr에_안_나간다(engine, tmp_path):
    _need(engine)
    docs = tmp_path / "docs"
    _plain_doc(docs)
    cmd = [RS_EXE] if engine == "rust" else [sys.executable, "-m", "csoclassify"]
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"), PYTHONIOENCODING="utf-8",
               CSOCLASSIFY_POLICY_DIR=os.path.join(ROOT, "resources", "policy"))
    # 기본 모드 + 없는 seed(전파 건너뜀 안내) + --textsave(개인정보 경고) — 알림이 나올 자리들.
    r = subprocess.run(cmd + ["--dir", str(docs), "--seeds", str(tmp_path / "없는seed.jsonl"),
                              "--textsave", str(tmp_path / "ts"), "--no-daemon",
                              "--format", "jsonl", "--out", str(tmp_path / "o.jsonl"),
                              "--json-errors"],
                       cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
    leaked = [l for l in r.stderr.splitlines()
              if l.strip() and not l.startswith(("[progress]", "[summary]"))]
    assert not leaked, leaked


#------------------------------------------------------------------
# --out 을 못 쓰면 문서를 읽기 전에 멈춘다 — Rust
#=> Rust 판은 결과를 끝에서 한 번에 써서, 경로가 틀려도 전체 스캔을 다 돌린 뒤에야 4001 로
#   실패했다. 스캔이 끝나야 나오는 [summary] 줄이 없어야 '읽기 전에 멈췄다'는 뜻이다.
#   (파이썬 판은 원래 시작할 때 파일을 열어 멈춘다 — 같은 모양인지 함께 본다.)
#
# -in: engine   = "python" | "rust"
# -in: tmp_path = pytest 임시 폴더
#
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_out을_못쓰면_읽기전에_멈춘다(engine, tmp_path):
    _need(engine)
    docs = tmp_path / "docs"
    _plain_doc(docs)
    cmd = [RS_EXE] if engine == "rust" else [sys.executable, "-m", "csoclassify"]
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"), PYTHONIOENCODING="utf-8",
               CSOCLASSIFY_POLICY_DIR=os.path.join(ROOT, "resources", "policy"))
    # 상위가 '폴더가 아니라 파일'이라 폴더를 만들 수도, 그 안에 쓸 수도 없는 경로.
    # (없는 폴더는 이제 만들어 주므로 '못 쓰는 경로'가 아니다.)
    blocker = tmp_path / "막는파일.txt"
    blocker.write_text("x", encoding="utf-8")
    bad = str(blocker / "o.jsonl")
    r = subprocess.run(cmd + ["--dir", str(docs), "--rule-only", "--out", bad, "--json-errors"],
                       cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
    lines = [json.loads(l)["error"] for l in r.stdout.splitlines() if l.startswith('{"error"')]
    assert (r.returncode, [l["code"] for l in lines]) == (1, [4001]), lines
    assert "[summary]" not in r.stderr, "스캔을 다 돈 뒤에 멈췄다"


#------------------------------------------------------------------
# --out 의 상위 폴더가 없으면 만들어서 쓴다 — 두 판이 같다
#=> 예: --out D:\\temp\\o.jsonl 인데 D:\\temp 가 아직 없는 경우. 예전에는 4001 로 멈췄다.
#   폴더가 없다는 이유만으로 분류를 못 하게 하지 않는다 — 중간 폴더까지 만들고 결과를 쓴다.
#   분류와 --text-only 두 모드를 함께 본다(출력 파일을 여는 자리가 서로 다르다).
#    1) 두 단계 아래의 없는 폴더를 --out 으로 준다
#    2) 실패 없이 끝나고(오류 코드 4001 이 없다), 결과 파일이 생겨 있고 비어 있지 않다
#
# -in: engine   = "python" | "rust"
# -in: mode     = 붙일 모드 인자("--rule-only" 는 분류, "--text-only" 는 본문만)
# -in: tmp_path = pytest 임시 폴더
#
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["python", "rust"])
@pytest.mark.parametrize("mode", ["--rule-only", "--text-only"])
def test_out의_상위_폴더가_없으면_만든다(engine, mode, tmp_path):
    _need(engine)
    docs = tmp_path / "docs"
    _plain_doc(docs)
    # 중간 폴더까지 없는 경로 — 한 단계만 만들면 안 되는지도 함께 본다.
    out = tmp_path / "없는폴더" / "하위" / "o.jsonl"
    rc, lines = _run_lines(engine, [mode, "--dir", str(docs), "--out", str(out),
                                    "--format", "jsonl"])
    assert 4001 not in [l["code"] for l in lines], lines
    assert out.is_file(), "결과 파일이 만들어지지 않았다 (rc=%s) %s" % (rc, lines)
    assert out.stat().st_size > 0, "결과 파일이 비어 있다"


#------------------------------------------------------------------
# --out 확인이 기존 결과 파일을 자르지 않는다 — Rust
#=> 읽기 전 확인은 append 로 열기만 한다. 뒤에서 다른 이유로 멈춰도(여기서는 --vector-only
#   인데 seed 가 없음) 예전 결과 파일은 그대로 남아야 한다.
#
# -in: tmp_path = pytest 임시 폴더
#
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 건너뛴다
#------------------------------------------------------------------
def test_rust_out_확인이_기존파일을_자르지_않는다(tmp_path):
    _need("rust")
    docs = tmp_path / "docs"
    _plain_doc(docs)
    out = tmp_path / "old.jsonl"
    out.write_text('{"file":"예전결과"}\n', encoding="utf-8")
    rc, lines = _run_lines("rust", ["--dir", str(docs), "--vector-only",
                                    "--seeds", str(tmp_path / "없는seed.jsonl"), "--out", str(out)])
    assert lines[-1]["code"] == 1007, lines
    assert out.read_text(encoding="utf-8") == '{"file":"예전결과"}\n'


#------------------------------------------------------------------
# Ctrl+C 로 멈추면 130 으로 끝나고 임시폴더를 치운다 — Rust (윈도우)
#=> 예전에는 처리기가 없어 OS 가 프로세스를 끊었다 — 종료코드가 0xC000013A 로 파이썬 판(130)과
#   달랐고, 압축을 풀어 둔 임시폴더가 내부 원문째로 남았다.
#   `--files-from -` 로 띄우면 표준입력을 기다리며 멈춰 있으므로, 그 사이에 이 프로세스 몫의
#   임시폴더를 하나 만들어 두고 중단 신호(CTRL_BREAK)를 보낸다.
#
# -in: tmp_path = pytest 임시 폴더
#
# -out: 없음(단언)
# -out: error = 윈도우가 아니거나 Rust exe 가 없으면 건너뛴다
#------------------------------------------------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="CTRL_BREAK 신호는 윈도우 시험")
def test_rust_중단하면_130_이고_임시폴더를_치운다(tmp_path):
    import signal
    import tempfile
    import time
    _need("rust")
    env = dict(os.environ, CSOCLASSIFY_POLICY_DIR=os.path.join(ROOT, "resources", "policy"),
               CSOCLASSIFY_ERRLOG=str(tmp_path / "err.log"))
    p = subprocess.Popen([RS_EXE, "--files-from", "-", "--rule-only", "--json-errors"],
                         cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        # 이 프로세스 몫의 압축 임시폴더(안에 원문이 있다고 친다).
        leftover = os.path.join(tempfile.gettempdir(), f"cso_zip_{p.pid}")
        os.makedirs(leftover, exist_ok=True)
        open(os.path.join(leftover, "원문.txt"), "w", encoding="utf-8").write("민감한 본문")
        time.sleep(1.0)     # 처리기를 달 시간
        p.send_signal(signal.CTRL_BREAK_EVENT)
        # communicate() 는 표준입력을 닫는다 — 그러면 목록 읽기가 끝나 '대상 0건(3)'과
        # 경주하게 되므로, 입력은 열어 둔 채 끝나기만 기다린다.
        p.wait(timeout=20)
        out = p.stdout.read()
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == 130
    assert not os.path.exists(leftover), "임시폴더(원문)가 남았다"
    # 중단은 결과가 아니다 — 상태 줄을 내지 않는다(파이썬 판과 같다).
    assert b'{"error"' not in out


#------------------------------------------------------------------
# --files-from - (표준입력 목록) 이 두 엔진에서 동작한다
#=> Rust 판의 인자 해석기가 '-' 로 시작하는 값을 모두 다음 옵션으로 보아, 문서에 적힌
#   `--files-from -` 가 Rust 판에서는 처음부터 "값이 없습니다"(1003)로 죽었다(2026-09-29 발견).
#
# -in: engine   = "python" | "rust"
# -in: tmp_path = pytest 임시 폴더
#
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_files_from_표준입력이_된다(engine, tmp_path):
    _need(engine)
    doc = _plain_doc(tmp_path / "docs")
    cmd = [RS_EXE] if engine == "rust" else [sys.executable, "-m", "csoclassify"]
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"), PYTHONIOENCODING="utf-8",
               CSOCLASSIFY_POLICY_DIR=os.path.join(ROOT, "resources", "policy"))
    r = subprocess.run(cmd + ["--files-from", "-", "--rule-only", "--out", str(tmp_path / "o.jsonl"),
                              "--json-errors"],
                       input=str(doc) + "\n", cwd=ROOT, env=env, capture_output=True,
                       text=True, encoding="utf-8")
    st = [json.loads(l)["error"] for l in r.stdout.splitlines() if l.startswith('{"error"')][-1]
    assert (r.returncode, st["kind"], st["total"]) == (0, "success", 1), st



# ════════════════════════════════════════════════════════════════════
# 2026-09-29 세 모델 지원 — 다른 모델로 만든 seed 는 섞지 않는다(1013)
# ════════════════════════════════════════════════════════════════════

#------------------------------------------------------------------
# 384차원 seed 파일 만들기(e5 계열이 만든 seed 흉내)
#=> 값은 의미가 없고 '차원'만 본다.
#
# -in: path = 만들 파일 경로(pathlib.Path)
# -in: dim  = 벡터 차원(기본 384)
#
# -out: pathlib.Path = 만든 파일
# -out: error = 없음
#------------------------------------------------------------------
def _seed_file(path, dim=384):
    path.write_text(json.dumps({"file": "x.txt", "grade": "C", "vector": [0.01] * dim}) + "\n",
                    encoding="utf-8")
    return path


#------------------------------------------------------------------
# 다른 모델의 seed 로 전파하려 하면 1013 — 분류 · 기준 문서 등록
#=> --model ko-sroberta(768차원)로 돌리는데 seed 는 e5(384차원)로 만든 것이면 비교가
#   성립하지 않는다. 예전에는 파이썬 판이 행렬 모양 오류로 죽고(9001), Rust 판은 짧은 쪽에
#   맞춰 잘라 엉뚱한 유사도를 냈다. 문서를 읽기 전에(모델을 올리기 전에) 멈춘다 —
#   이 PC 에 ko-sroberta 모델 파일이 없어도 1013 이 먼저 나와야 한다.
#
# -in: engine   = "python" | "rust"
# -in: mode     = 추가 인자(분류 기본 모드 / 기준 문서 등록)
# -in: tmp_path = pytest 임시 폴더
#
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["classify", "seed_add"])
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_다른모델_seed는_1013(engine, mode, tmp_path):
    _need(engine)
    doc = _plain_doc(tmp_path / "docs")
    seeds = _seed_file(tmp_path / "seed.jsonl")
    if mode == "classify":
        argv = ["--dir", str(tmp_path / "docs"), "--seeds", str(seeds)]
    else:
        argv = ["--seed-add", str(doc), "--seed-grade", "C", "--seed-reviewer", "시험",
                "--seeds", str(seeds), "--axis", "security"]
    rc, lines = _run_lines(engine, argv + ["--model", "ko-sroberta", "--no-daemon"])
    assert (rc, [l["code"] for l in lines]) == (3, [1013]), lines
    assert lines[0]["path"] == str(seeds)
    assert "768" in lines[0]["message"] and "384" in lines[0]["message"]
    # 같은 모델이면 이 검사에 걸리지 않는다(규칙만 쓰는 실행으로 확인 — 모델 파일 불필요).
    rc, lines = _run_lines(engine, ["--dir", str(tmp_path / "docs"), "--seeds", str(seeds),
                                    "--model", "ko-sroberta", "--rule-only",
                                    "--out", str(tmp_path / "o.jsonl")])
    assert lines[-1]["code"] == 0, lines


#------------------------------------------------------------------
# 전파 입력의 벡터와 seed 의 차원이 다르면 1013 — --propagate
#=> 전파 모드는 모델을 올리지 않는다. 비교 대상은 1차 결과 레코드에 이미 실린 벡터다.
#   seed 안에 차원이 섞여 있어도(일부만 다른 모델) 같은 오류다.
#
# -in: engine   = "python" | "rust"
# -in: tmp_path = pytest 임시 폴더
#
# -out: 없음(단언)
# -out: error = Rust exe 가 없으면 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_전파입력과_seed_차원이_다르면_1013(engine, tmp_path):
    _need(engine)
    rec = tmp_path / "r1.jsonl"
    rec.write_text(json.dumps({"file": "a.txt", "grade": None, "vector": [0.02] * 768}) + "\n",
                   encoding="utf-8")
    seeds = _seed_file(tmp_path / "seed.jsonl")
    rc, lines = _run_lines(engine, ["--propagate", str(rec), "--seeds", str(seeds),
                                    "--axis", "security"])
    assert (rc, [l["code"] for l in lines]) == (3, [1013]), lines

    # 차원이 섞인 seed(384 + 768) — 하나라도 다르면 멈춘다.
    with open(seeds, "a", encoding="utf-8") as f:
        f.write(json.dumps({"file": "y.txt", "grade": "S", "vector": [0.03] * 768}) + "\n")
    rc, lines = _run_lines(engine, ["--propagate", str(rec), "--seeds", str(seeds),
                                    "--axis", "security"])
    assert (rc, [l["code"] for l in lines]) == (3, [1013]), lines
    assert "384차원 1건" in lines[0]["message"] and "768차원 1건" in lines[0]["message"]

    # 차원이 맞으면 그대로 돈다.
    _seed_file(seeds, dim=768)
    rc, lines = _run_lines(engine, ["--propagate", str(rec), "--seeds", str(seeds),
                                    "--axis", "security"])
    assert rc == 0 and lines[-1]["kind"] == "success", lines



#------------------------------------------------------------------
# 같은 --model 이면 두 판이 같은 벡터를 낸다 — 세 모델 모두
#=> 2026-09-29 부터 Rust 판도 --model 로 세 모델을 고른다. 모델마다 프리픽스(e5 'passage: ' ·
#   sroberta 없음)·차원(384·768)·입력 이름(RoBERTa 는 token_type_ids 없음)이 달라, 한 칸만
#   어긋나도 벡터가 달라진다. 긴 문서를 하나 넣어 청크 나누기(프리픽스 길이만큼 예산이
#   달라진다)까지 견준다.
#   모델 파일은 저장소에 없다(scripts/export_onnx.py 로 만든다) — 없는 모델은 건너뛴다.
#   두 판 모두 CSOCLASSIFY_MODELS_DIR 로 같은 폴더를 가리킨다(Rust 판도 이 변수를 읽는다).
#
# -in: model    = 모델 별칭
# -in: tmp_path = pytest 임시 폴더
#
# -out: 없음(단언)
# -out: error = 모델 파일이나 Rust exe 가 없으면 건너뛴다
#------------------------------------------------------------------
@pytest.mark.parametrize("model", ["e5-small-ko", "e5-small", "ko-sroberta"])
def test_같은_모델이면_두판의_벡터가_같다(model, tmp_path):
    _need("rust")
    from csoclassify import config
    spec = config.MODELS[model]
    models_dir = os.path.join(ROOT, "resources", "models")
    if not os.path.isfile(os.path.join(models_dir, spec.local_dir, "model.onnx")):
        pytest.skip(f"{model} 모델 파일 없음(python scripts/export_onnx.py --model {model})")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "짧은.txt").write_text("사내 규정에 따른 일반 안내문입니다. 회의는 매주 화요일에 열립니다.",
                                  encoding="utf-8")
    # 512 토큰을 넘겨 청크가 여러 개가 되게 한다.
    (docs / "긴.txt").write_text(
        "연구소 안전관리 규정은 실험실의 사고를 예방하고 연구원의 건강을 지키기 위해 만든 것이다. " * 80,
        encoding="utf-8")

    vecs = {}
    for engine in ("python", "rust"):
        cmd = [RS_EXE] if engine == "rust" else [sys.executable, "-m", "csoclassify"]
        env = {k: v for k, v in os.environ.items() if k != "CSO_MODEL"}
        env.update(PYTHONPATH=os.path.join(ROOT, "src"), PYTHONIOENCODING="utf-8",
                   CSOCLASSIFY_POLICY_DIR=os.path.join(ROOT, "resources", "policy"),
                   CSOCLASSIFY_MODELS_DIR=models_dir)
        out = tmp_path / f"{engine}.jsonl"
        r = subprocess.run(cmd + ["--dir", str(docs), "--model", model, "--with-vector",
                                  "--seeds", str(tmp_path / "없는seed.jsonl"), "--axis", "security",
                                  "--no-daemon", "--format", "jsonl", "--out", str(out)],
                           cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, f"{engine}: {r.stderr[-800:]}"
        recs = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if '"file"' in l]
        vecs[engine] = {os.path.basename(x["file"]): x.get("vector") for x in recs}

    assert set(vecs["python"]) == set(vecs["rust"]) == {"짧은.txt", "긴.txt"}
    for name in vecs["python"]:
        a, b = vecs["python"][name], vecs["rust"][name]
        assert a and b, (name, "벡터가 없다")
        assert len(a) == len(b) == spec.dim, (name, len(a), len(b))
        # 두 판 모두 L2 정규화 벡터 — 내적이 곧 코사인이다.
        cos = sum(x * y for x, y in zip(a, b))
        assert cos > 0.9999, (model, name, cos)
