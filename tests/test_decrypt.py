#------------------------------------------------------------------
# 엠파워 v11 암호화 문서 복호화 연동 (2026-10-12)
#=> 설계: plan/엠파워v11-암호화문서-복호화연동-설계-20261012.html §11
#   엠파워 v11 저장소의 문서는 암호화된 채 놓여 있다. 분류 직전에 엠파워의
#   removeenc.sh 로 한 건씩 풀고, 분류가 끝나면 풀린 파일을 바로 지운다.
#
#   개발 PC 에는 진짜 removeenc.sh 가 없으므로 --decrypt-cmd 에 가짜 도구
#   (tests/fake_removeenc.py)를 끼운다. 가짜 도구는 진짜의 성질 — 틀린 열쇠에도 0 을
#   돌려주며 같은 크기의 쓰레기를 낸다 · 종료코드를 stdout 에도 찍는다 — 을 흉내 낸다.
#
#   [무엇을 지키나] Rust 판 tests/cli_decrypt.rs 와 같은 계약이다.
#     ① 풀어서 분류한 결과의 file 은 목록의 path 그대로, hash 는 '풀린 내용'의 지문
#     ② 풀지 못했거나 풀린 결과가 수상하면 분류하지 않는다(grade=null · error.kind=decrypt_failed)
#     ③ 파일 문제는 그 문서만 실패, 환경 문제는 전체 중단
#     ④ 끝나면 임시 폴더가 비어 있다 · 열쇠와 임시 경로는 결과·로그 어디에도 없다
#------------------------------------------------------------------

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import time
import zipfile

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(_HERE, ".."))
RULES = os.path.join(ROOT, "resources", "policy", "cso_rule.yaml")
FAKE_PY = os.path.join(_HERE, "fake_removeenc.py")

# 가짜 도구가 아는 '맞는 열쇠'.
GOOD = "GOODKEY"

BODY_A = "분류 시험용 문서입니다. 이 문서는 엠파워 v11 복호화 연동을 시험하려고 만든 더미 본문입니다.\n".encode("utf-8")
BODY_B = "두 번째 시험 문서입니다. 내용은 아무 뜻이 없고 복호화 뒤의 지문을 견주는 데만 씁니다.\n".encode("utf-8")


#------------------------------------------------------------------
# 지문(SHA-256)
#
# -in: data = 바이트
#
# -out: str = 16진 지문
# -out: error = 없음
#------------------------------------------------------------------
def sha256(data):
    return hashlib.sha256(data).hexdigest()


#------------------------------------------------------------------
# 가짜 도구의 "암호화" — 바이트마다 0x5A 를 XOR 한다
#
# -in: plain = 평문 바이트
#
# -out: bytes = "암호문"
# -out: error = 없음
#------------------------------------------------------------------
def encrypt(plain):
    return bytes(b ^ 0x5A for b in plain)


#------------------------------------------------------------------
# 시험 하나의 작업 환경
#=> 저장소(store) · 임시(tmp) · 로그(log) · 가짜 도구 실행 파일을 한 폴더에 만든다.
#   가짜 도구는 파이썬 스크립트라 그대로는 실행 파일이 아니다 — OS 에 맞는 얇은
#   껍데기(.cmd / .sh)를 만들어 --decrypt-cmd 로 준다.
#
# -필드: dir  = 작업 폴더
# -필드: tool = 가짜 도구 껍데기 경로
#------------------------------------------------------------------
class Env:
    def __init__(self, tmp_path):
        self.dir = str(tmp_path)
        # 저장소 폴더 이름에 공백을 넣어 둔다 — 인자가 글자 그대로 넘어가는지 함께 본다.
        os.makedirs(os.path.join(self.dir, "저장 소"))
        os.makedirs(os.path.join(self.dir, "tmp"))
        if os.name == "nt":
            self.tool = os.path.join(self.dir, "fake_removeenc.cmd")
            with io.open(self.tool, "w", encoding="ascii") as f:
                f.write('@"%s" "%s" %%*\r\n' % (sys.executable, FAKE_PY))
        else:
            self.tool = os.path.join(self.dir, "fake_removeenc.sh")
            with io.open(self.tool, "w", encoding="utf-8") as f:
                f.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, FAKE_PY))
            os.chmod(self.tool, os.stat(self.tool).st_mode | stat.S_IXUSR)

    @property
    def tmp(self):
        return os.path.join(self.dir, "tmp")

    #--------------------------------------------------------------
    # 저장소에 파일 하나 쓰기(저장 이름은 엠파워처럼 뜻 없는 이름)
    #
    # -in: rname = 저장 이름 / data = 내용(바이트)
    #
    # -out: str = 만든 파일 경로
    # -out: error = 없음
    #--------------------------------------------------------------
    def store(self, rname, data):
        p = os.path.join(self.dir, "저장 소", rname)
        with open(p, "wb") as f:
            f.write(data)
        return p

    #--------------------------------------------------------------
    # 목록 파일 쓰기(한 줄에 JSON 하나)
    #
    # -in: rows = dict 목록
    #
    # -out: str = 목록 파일 경로
    # -out: error = 없음
    #--------------------------------------------------------------
    def filelist(self, rows):
        p = os.path.join(self.dir, "filelist.jsonl")
        with io.open(p, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return p

    #--------------------------------------------------------------
    # 분류를 한 번 돌린다(소스 모듈로 — 빌드 없이도 계약을 지킬 수 있게)
    #=> 규칙만 쓰고(--rule-only) 결과는 jsonl 파일로 받는다.
    #
    # -in: args     = 추가 인자
    # -in: with_tool = False 면 --decrypt-cmd 를 붙이지 않는다(도구 없음 시험용)
    #
    # -out: (records, summary, 완료된 프로세스)
    # -out: error = 없음
    #--------------------------------------------------------------
    def run(self, args, with_tool=True):
        out = os.path.join(self.dir, "result.jsonl")
        if os.path.exists(out):
            os.remove(out)
        env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"),
                   PYTHONIOENCODING="utf-8",
                   CSOCLASSIFY_LOGDIR=os.path.join(self.dir, "log"))
        for k in ("CSOCLASSIFY_MRK", "CSOCLASSIFY_DECRYPT_CMD", "CSOCLASSIFY_DECRYPT_TMP"):
            env.pop(k, None)
        cmd = [sys.executable, "-m", "csoclassify"] + list(args) + [
            "--cso-rules", RULES, "--rule-only", "--json-errors",
            "--format", "jsonl", "--out", out, "--decrypt-tmp", self.tmp]
        if with_tool:
            cmd += ["--decrypt-cmd", self.tool]
        r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True,
                           encoding="utf-8")
        rows = []
        if os.path.exists(out):
            rows = [json.loads(l) for l in io.open(out, encoding="utf-8") if l.strip()]
        recs = [x for x in rows if "file" in x]
        summ = next((x["summary"] for x in rows if "summary" in x), {})
        return recs, summ, r

    #--------------------------------------------------------------
    # 임시 폴더에 풀린 파일이 남아 있지 않은가
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 남은 것이 있으면 AssertionError
    #--------------------------------------------------------------
    def assert_tmp_clean(self):
        left = os.listdir(self.tmp)
        assert left == [], f"임시 폴더에 남은 것이 있다: {left}"

    #--------------------------------------------------------------
    # 결과 파일과 로그의 모든 글자(열쇠·임시 경로가 새지 않았는지 볼 때 쓴다)
    #
    # -in: 없음
    #
    # -out: {"결과": str, "로그": str}
    # -out: error = 없음
    #--------------------------------------------------------------
    def texts(self):
        out = {"결과": "", "로그": ""}
        res = os.path.join(self.dir, "result.jsonl")
        if os.path.exists(res):
            out["결과"] = io.open(res, encoding="utf-8").read()
        logd = os.path.join(self.dir, "log")
        if os.path.isdir(logd):
            for n in os.listdir(logd):
                with open(os.path.join(logd, n), "rb") as f:
                    out["로그"] += f.read().decode("utf-8", "replace")
        return out


#------------------------------------------------------------------
# stdout 의 마지막 JSON 줄(상태 줄)에서 (kind, code) 꺼내기
#
# -in: proc = 완료된 프로세스
#
# -out: (kind, code)
# -out: error = 상태 줄이 없으면 AssertionError
#------------------------------------------------------------------
def status(proc):
    lines = [l for l in (proc.stdout or "").splitlines() if l.lstrip().startswith("{")]
    assert lines, f"상태 줄이 없다. stdout={proc.stdout!r} stderr={proc.stderr}"
    err = json.loads(lines[-1])["error"]
    return err["kind"], err["code"]


#------------------------------------------------------------------
# 결과에서 file 이 같은 레코드 찾기(경로 구분자는 가리지 않는다)
#
# -in: recs = 레코드 목록 / file = 찾을 경로
#
# -out: dict = 그 레코드
# -out: error = 없으면 AssertionError
#------------------------------------------------------------------
def rec_of(recs, file):
    want = file.replace("\\", "/")
    for r in recs:
        if (r.get("file") or "").replace("\\", "/") == want:
            return r
    raise AssertionError(f"결과에 {file} 가 없다: {[r.get('file') for r in recs]}")


def row(path, sfid, **kw):
    d = {"path": path, "sfile_id": sfid}
    d.update(kw)
    return d


#------------------------------------------------------------------
# ①④ 목록의 열쇠로 풀어서 분류하고 임시 파일을 지운다
#------------------------------------------------------------------
def test_목록의_열쇠로_풀어서_분류하고_임시파일을_지운다(tmp_path):
    env = Env(tmp_path)
    a = env.store("F_aaaa.SMF", encrypt(BODY_A))
    # 가짜 도구가 stdout 에 잡다한 글자를 잔뜩 찍는 건 — 결과가 깨지지 않아야 한다.
    b = env.store("F_bbbb.SMF", encrypt(BODY_B))
    # 둘 다 0 — 암호화 안 된 채 저장된 파일. 도구를 부르지 않는다(열쇠도 없다).
    c = env.store("F_cccc.SMF", BODY_A)
    # 열쇠 칸이 아예 없는 줄 — 지금까지처럼 그대로 읽는다.
    d = env.store("plain.txt", BODY_B)
    lst = env.filelist([
        row(a, "SF_A", mrk=GOOD, enc_mode=3, zip_mode=0, name="시험 문서 (가).txt", size=len(BODY_A)),
        row(b, "SF_B", mrk="NOISE", enc_mode="3", zip_mode="0", name="시험문서_나.txt", size=len(BODY_B)),
        row(c, "SF_C", enc_mode=0, zip_mode=0, name="평문저장.txt"),
        row(d, "SF_D"),
    ])
    recs, summ, r = env.run(["--filelist", lst])
    assert r.returncode == 0, r.stderr
    assert len(recs) == 4, recs

    ra = rec_of(recs, a)
    assert "error" not in ra, ra
    # 지문은 암호문이 아니라 '풀린 내용'의 것이어야 한다.
    assert ra["hash"] == sha256(BODY_A)
    assert ra["doc_id"] == "SF_A"
    assert ra["name"] == "시험 문서 (가).txt"
    assert ra["meta"]["decrypted"] is True
    assert rec_of(recs, b)["hash"] == sha256(BODY_B)

    # 둘 다 0 인 줄은 도구를 부르지 않았다 — '풀었다'는 표식이 없다.
    rc = rec_of(recs, c)
    assert "error" not in rc, rc
    assert rc["hash"] == sha256(BODY_A)
    assert "decrypted" not in rc.get("meta", {})
    assert rc["name"] == "평문저장.txt"

    rd = rec_of(recs, d)
    assert "name" not in rd
    assert rd["doc_id"] == "SF_D"

    assert summ.get("decrypted") == 2 and summ.get("decrypt_failed") == 0
    env.assert_tmp_clean()
    # stdout 에는 우리 상태 줄만 있어야 한다 — 가짜 도구의 출력이 섞이면 안 된다.
    assert "noise" not in r.stdout
    assert status(r)[0] == "success"
    for what, text in env.texts().items():
        assert GOOD not in text, f"{what}에 열쇠가 남았다"
        assert "cso_dec_" not in text, f"{what}에 임시 경로가 남았다"


#------------------------------------------------------------------
# ② 틀린 열쇠로 풀린 결과는 분류하지 않는다
#------------------------------------------------------------------
def test_틀린_열쇠로_풀린_결과는_분류하지_않는다(tmp_path):
    env = Env(tmp_path)
    pdf = b"%PDF-1.4 dummy body for signature check, not a real pdf\n"
    # 도구는 틀린 열쇠에도 0 을 돌려주고 같은 크기의 쓰레기를 낸다.
    a = env.store("F_pdf.SMF", encrypt(pdf))
    b = env.store("F_txt.SMF", encrypt(BODY_A))
    lst = env.filelist([
        # 이름에 등급 신호가 될 만한 말을 넣는다 — 그래도 등급을 매기면 안 된다.
        row(a, "SF_PDF", mrk="WRONGKEY", enc_mode=3, zip_mode=0, name="대외비 보고서.pdf", size=len(pdf)),
        row(b, "SF_TXT", mrk=GOOD, enc_mode=3, zip_mode=0, name="메모.txt"),
    ])
    recs, summ, r = env.run(["--filelist", lst])
    # 일부 실패 — 종료코드 1, 까닭은 decrypt_failed(3005).
    assert r.returncode == 1
    assert status(r) == ("decrypt_failed", 3005)

    ra = rec_of(recs, a)
    assert ra["grade"] is None, ra
    assert ra["error"]["kind"] == "decrypt_failed"
    assert ra["error"]["detail"] == "signature_mismatch"
    assert ra["error"]["rc"] == 0
    assert ra["doc_id"] == "SF_PDF"
    # 암호문의 지문은 싣지 않는다.
    assert not ra.get("hash")

    # 다음 문서는 그대로 처리된다.
    rb = rec_of(recs, b)
    assert "error" not in rb, rb
    assert rb["hash"] == sha256(BODY_A)
    assert summ.get("decrypt_failed") == 1
    env.assert_tmp_clean()


#------------------------------------------------------------------
# ② 열쇠가 일부만 있거나 감싼 값이면 도구를 부르지 않고 오류로 남긴다
#------------------------------------------------------------------
def test_열쇠가_일부만_있거나_감싼_값이면_오류로_남긴다(tmp_path):
    env = Env(tmp_path)
    a = env.store("F_1.SMF", encrypt(BODY_A))
    b = env.store("F_2.SMF", encrypt(BODY_A))
    c = env.store("F_3.SMF", encrypt(BODY_A))
    lst = env.filelist([
        # zip_mode 가 빠졌다 — '그대로 읽기'로 넘어가면 암호문을 분류한다.
        row(a, "S1", mrk=GOOD, enc_mode=3, name="a.txt"),
        # KEK 로 감싼 열쇠.
        row(b, "S2", mrk="v2:AAAABBBB", enc_mode=3, zip_mode=0),
        # 암호화된 파일인데 열쇠가 없다.
        row(c, "S3", enc_mode=3, zip_mode=0),
    ])
    recs, _, r = env.run(["--filelist", lst])
    assert r.returncode == 1
    for file, detail in ((a, "info_incomplete"), (b, "key_wrapped"), (c, "info_incomplete")):
        x = rec_of(recs, file)
        assert x["error"]["kind"] == "decrypt_failed", x
        assert x["error"]["detail"] == detail, x
        assert x["error"]["rc"] is None, "도구를 부르지 않았어야 한다"
        assert x["grade"] is None
    env.assert_tmp_clean()


#------------------------------------------------------------------
# ③ 도구가 실패하면 그 문서만 실패로 남기고 계속한다
#------------------------------------------------------------------
def test_도구가_실패하면_그_문서만_실패로_남기고_계속한다(tmp_path):
    env = Env(tmp_path)
    a = env.store("F_1.SMF", encrypt(BODY_A))
    b = env.store("F_2.SMF", encrypt(BODY_A))
    c = env.store("F_3.SMF", encrypt(BODY_B))
    lst = env.filelist([
        row(a, "S1", mrk="RC9", enc_mode=3, zip_mode=0, name="a.txt"),
        row(b, "S2", mrk="EMPTY", enc_mode=3, zip_mode=0, name="b.txt"),
        row(c, "S3", mrk=GOOD, enc_mode=3, zip_mode=0, name="c.txt"),
    ])
    recs, _, r = env.run(["--filelist", lst])
    assert r.returncode == 1
    ra = rec_of(recs, a)
    assert ra["error"]["detail"] == "removeenc_exit"
    assert ra["error"]["rc"] == 9
    assert rec_of(recs, b)["error"]["detail"] == "empty_output"
    rc = rec_of(recs, c)
    assert "error" not in rc, rc
    assert rc["hash"] == sha256(BODY_B)
    env.assert_tmp_clean()


#------------------------------------------------------------------
# ③ 환경 문제면 전체를 멈춘다
#------------------------------------------------------------------
def test_환경_문제면_전체를_멈춘다(tmp_path):
    env = Env(tmp_path)
    a = env.store("F_1.SMF", encrypt(BODY_A))
    b = env.store("F_2.SMF", encrypt(BODY_A))
    lst = env.filelist([
        # 56 = PHP 확장을 못 읽었다. 이 서버에서는 어떤 파일도 풀 수 없다.
        row(a, "S1", mrk="RC56", enc_mode=3, zip_mode=0),
        row(b, "S2", mrk="RC56", enc_mode=3, zip_mode=0),
    ])
    _, _, r = env.run(["--filelist", lst])
    assert r.returncode == 2
    assert status(r) == ("decrypt_unavailable", 3006)
    env.assert_tmp_clean()


#------------------------------------------------------------------
# 도구가 없으면 한 건도 읽지 않고 끝낸다
#------------------------------------------------------------------
def test_도구가_없으면_한_건도_읽지_않고_끝낸다(tmp_path):
    env = Env(tmp_path)
    a = env.store("F_1.SMF", encrypt(BODY_A))
    lst = env.filelist([row(a, "S1", mrk=GOOD, enc_mode=3, zip_mode=0)])
    missing = os.path.join(env.dir, "no_such_removeenc.sh")
    recs, _, r = env.run(["--filelist", lst, "--decrypt-cmd", missing], with_tool=False)
    assert r.returncode == 3
    assert status(r) == ("decrypt_cmd_missing", 1015)
    assert recs == []
    env.assert_tmp_clean()


#------------------------------------------------------------------
# 시간 제한을 넘으면 끊고 다음으로 간다
#------------------------------------------------------------------
def test_시간_제한을_넘으면_끊고_다음으로_간다(tmp_path):
    env = Env(tmp_path)
    a = env.store("F_1.SMF", encrypt(BODY_A))
    b = env.store("F_2.SMF", encrypt(BODY_B))
    lst = env.filelist([
        row(a, "S1", mrk="SLEEP", enc_mode=3, zip_mode=0, name="a.txt"),
        row(b, "S2", mrk=GOOD, enc_mode=3, zip_mode=0, name="b.txt"),
    ])
    t0 = time.time()
    recs, _, r = env.run(["--filelist", lst, "--decrypt-timeout", "2"])
    # 가짜 도구는 30초를 잔다 — 제한에서 끊겼다면 훨씬 일찍 끝난다.
    assert time.time() - t0 < 25, "시간 제한이 듣지 않았다"
    assert r.returncode == 1
    assert rec_of(recs, a)["error"]["detail"] == "timeout"
    assert "error" not in rec_of(recs, b)


#------------------------------------------------------------------
# 단일 파일은 --decrypt 를 줄 때만 푼다
#------------------------------------------------------------------
def test_단일_파일은_decrypt_를_줄_때만_푼다(tmp_path):
    env = Env(tmp_path)
    enc = env.store("F_single.SMF", encrypt(BODY_A))
    name = "보고서 (최종) $(echo x) & 사본.txt"
    recs, _, r = env.run(["--file", enc, "--decrypt", "--mrk", GOOD,
                          "--enc-mode", "3", "--zip-mode", "0", "--name", name])
    assert r.returncode == 0, r.stderr
    x = rec_of(recs, enc)
    assert x["hash"] == sha256(BODY_A)
    assert x["name"] == name
    assert x["meta"]["decrypted"] is True
    env.assert_tmp_clean()
    # 명령줄로 준 열쇠가 로그에 남지 않는다(실행 명령을 통째로 적는 자리가 있다).
    logs = env.texts()["로그"]
    assert "--mrk" in logs, "실행 명령이 로그에 있어야 한다"
    assert GOOD not in logs, "로그에 열쇠가 남았다"

    # --decrypt 를 안 주면 지금까지처럼 그대로 읽는다(이미 풀어 둔 파일용).
    plain = env.store("이미 풀어 둔 문서.txt", BODY_B)
    recs, _, r = env.run(["--file", plain])
    assert r.returncode == 0
    x = rec_of(recs, plain)
    assert x["hash"] == sha256(BODY_B)
    assert "decrypted" not in x.get("meta", {})
    # 우리가 만든 파일이 아니므로 지우지 않는다.
    assert os.path.isfile(plain)


#------------------------------------------------------------------
# 인자 조합이 틀리면 시작 전에 끝낸다
#------------------------------------------------------------------
@pytest.mark.parametrize("extra", [
    # 열쇠만 주고 --decrypt 가 없다 — "풀릴 줄 알았는데 안 풀린" 상태를 만들지 않는다.
    ["--mrk", GOOD, "--enc-mode", "3", "--zip-mode", "0"],
    # --decrypt 인데 열쇠가 없다.
    ["--decrypt"],
    # 일부만 줬다.
    ["--decrypt", "--mrk", GOOD, "--enc-mode", "3"],
    # 감싼 열쇠.
    ["--decrypt", "--mrk", "v2:XXXX", "--enc-mode", "3", "--zip-mode", "0"],
    ["--decrypt", "--no-decrypt", "--mrk", GOOD, "--enc-mode", "3", "--zip-mode", "0"],
])
def test_인자_조합이_틀리면_시작_전에_끝낸다(tmp_path, extra):
    env = Env(tmp_path)
    enc = env.store("F_x.SMF", encrypt(BODY_A))
    recs, _, r = env.run(["--file", enc] + extra)
    assert r.returncode == 3, extra
    assert status(r) == ("decrypt_args_invalid", 1014), extra
    assert recs == [], "문서를 읽으면 안 된다"


def test_폴더에는_decrypt_를_쓸_수_없다(tmp_path):
    env = Env(tmp_path)
    env.store("F_x.SMF", encrypt(BODY_A))
    # 폴더에는 파일마다 다른 열쇠를 줄 방법이 없다.
    _, _, r = env.run(["--dir", os.path.join(env.dir, "저장 소"), "--decrypt",
                       "--mrk", GOOD, "--enc-mode", "3", "--zip-mode", "0"])
    assert r.returncode == 3
    assert status(r) == ("decrypt_args_invalid", 1014)


#------------------------------------------------------------------
# --no-decrypt 면 목록에 열쇠가 있어도 풀지 않는다
#------------------------------------------------------------------
def test_no_decrypt_면_목록에_열쇠가_있어도_풀지_않는다(tmp_path):
    env = Env(tmp_path)
    # 같은 목록으로 '이미 풀어 둔 사본'을 가리키는 경우 — 파일은 평문이다.
    a = env.store("copy.txt", BODY_A)
    lst = env.filelist([row(a, "S1", mrk=GOOD, enc_mode=3, zip_mode=0, name="a.txt")])
    recs, _, r = env.run(["--filelist", lst, "--no-decrypt"])
    assert r.returncode == 0
    x = rec_of(recs, a)
    assert x["hash"] == sha256(BODY_A)
    assert "decrypted" not in x.get("meta", {})
    env.assert_tmp_clean()


#------------------------------------------------------------------
# 풀린 압축파일은 내부 문서로 펼친다
#------------------------------------------------------------------
def test_풀린_압축파일은_내부_문서로_펼친다(tmp_path):
    env = Env(tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("inner_a.txt", BODY_A)
        z.writestr("sub/inner_b.txt", BODY_B)
    zdata = buf.getvalue()
    # ① 암호화된 zip ② 암호화 없이(0,0) 저장된 zip — 저장 이름에는 확장자 단서가 없다.
    a = env.store("F_zip1.SMF", encrypt(zdata))
    b = env.store("F_zip2.SMF", zdata)
    lst = env.filelist([
        row(a, "SZ1", mrk=GOOD, enc_mode=3, zip_mode=0, name="묶음1.zip", size=len(zdata)),
        row(b, "SZ2", enc_mode=0, zip_mode=0, name="묶음2.zip", size=len(zdata)),
    ])
    recs, _, r = env.run(["--filelist", lst])
    assert r.returncode == 0, (r.stderr, recs)
    for top in (a, b):
        # 내부 문서의 file 은 '목록 경로/내부 경로' — 임시 경로가 아니다.
        assert rec_of(recs, top + "/inner_a.txt")["hash"] == sha256(BODY_A)
        assert rec_of(recs, top + "/sub/inner_b.txt")["hash"] == sha256(BODY_B)
        # 압축 자체의 집계 레코드도 목록 경로로 나온다.
        assert rec_of(recs, top).get("archive") is True
    assert "cso_dec_" not in env.texts()["결과"], "결과에 임시 경로가 남았다"
    env.assert_tmp_clean()


#------------------------------------------------------------------
# 목록 해석 — 열쇠 칸의 있고 없음(F8)
#=> 프로세스를 띄우지 않고 모듈로 본다. Rust 판 filelist::parse_dec_info 와 같은 판정.
#------------------------------------------------------------------
def test_열쇠_세_칸_해석():
    from csoclassify import decrypt
    assert decrypt.parse_dec_info(None, None, None) is None
    assert decrypt.parse_dec_info("", "", " ") is None
    # 둘 다 0 이면 열쇠가 없어도 된다(암호화 안 된 파일).
    assert decrypt.parse_dec_info(None, 0, "0").is_plain()
    info = decrypt.parse_dec_info("K", 3, 0)
    assert (info.mrk, info.enc_mode, info.zip_mode) == ("K", "3", "0")
    assert not info.is_plain()
    # 열쇠는 표현에 찍히지 않는다.
    assert "SECRETKEY" not in repr(decrypt.parse_dec_info("SECRETKEY", 3, 0))
    for args, reason in ((("K", 3, None), "info_incomplete"),
                         ((None, 3, 0), "info_incomplete"),
                         (("v2:xx", 3, 0), "key_wrapped")):
        with pytest.raises(decrypt.DecError) as ei:
            decrypt.parse_dec_info(*args)
        assert ei.value.reason == reason


def test_로그용_명령줄에서_열쇠를_가린다():
    from csoclassify import decrypt
    m = decrypt.mask_args(["--file", "x", "--mrk", "SECRET", "--enc-mode", "3", "--mrk=SECRET2"])
    assert m == ["--file", "x", "--mrk", "****", "--enc-mode", "3", "--mrk=****"]
