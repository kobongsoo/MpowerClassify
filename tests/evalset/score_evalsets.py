# -*- coding: utf-8 -*-
#------------------------------------------------------------------
# 평가셋 채점기 — 업무분류 정밀도·재현율 / 보안등급 C 재현율·정밀도
#=> build_evalsets.py 가 만들고 사람이 정답을 채워 넣은 평가셋(xlsx)을 채점한다.
#   규칙을 고친 뒤 "좋아졌나 나빠졌나"를 숫자로 답하는 것이 이 도구의 유일한 목적이다.
#
#   [엔진 판정을 어디서 읽나 — 두 가지 방법]
#    ① --run 없이 : xlsx 의 ref_ 열(평가셋을 만들 때 찍어 둔 판정)로 채점한다.
#       평가셋을 만든 그 시점의 규칙 성적이다. 빠르지만 규칙을 바꿔도 값이 안 변한다.
#    ② --run 주면 : 그 실행 결과(jsonl)로 채점한다. 규칙을 고친 뒤 엔진을 다시 돌려
#       이 옵션으로 채점하는 것이 정상 흐름이다. --prev 를 함께 주면 두 판을 비교해
#       "어느 문서의 판정이 어떻게 바뀌었는지"까지 보여 준다.
#
#   [표본과 모집단]
#   평가셋은 층화 표집이라 표본 비율이 곧 전체 비율이 아니다. 층마다
#   (모집단 수 / 표본 수) 로 가중해 모집단 추정치를 함께 낸다. 보고할 때는
#   둘 다 말한다 — 표본만 말하면 과장되고, 모집단만 말하면 표본이 몇 건이었는지 숨는다.
#
#   사용 예:
#     python tests/evalset/score_evalsets.py                       # xlsx 의 ref_ 열로 채점
#     python tests/evalset/score_evalsets.py --axis doctype --run work/run.jsonl
#     python tests/evalset/score_evalsets.py --axis doctype --run new.jsonl --prev old.jsonl
#     python tests/evalset/score_evalsets.py --json 결과.json --details
#
#   엔진 결과(jsonl)를 만드는 법 — build_evalsets.py 와 같은 명령이다:
#     MpowerClassify-rs.exe --filelist 목록.jsonl --rule-only --format jsonl --out run.jsonl
#     (환경변수 CSOCLASSIFY_POLICY_DIR 로 채점할 규칙 폴더를 가리킨다)
#------------------------------------------------------------------

import argparse
import collections
import json
import os
import sys

import openpyxl

# 윈도우 콘솔(cp949)에서 못 찍는 글자 때문에 채점이 멈추지 않게 한다.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 평가셋 원본이 가리키는 문서 뿌리. ref_상대경로 는 이 폴더 아래의 상대 경로다.
SRC_DIR = r"D:\분류함"

# 평가셋은 resources/golden_dataset/ 에 모아 둔다(2026-10-22 report/ 에서 옮김 · gitignore)
DOCTYPE_XLSX = os.path.join(ROOT, "resources", "golden_dataset", "업무분류-평가셋-500-20260914.xlsx")
SECURITY_XLSX = os.path.join(ROOT, "resources", "golden_dataset", "보안등급-C재현율-평가셋-500-20260914.xlsx")

# 업무분류 시트의 열 번호(1부터). build_evalsets.py 가 쓰는 배치와 같아야 한다.
D_COL = {"no": 1, "folder": 2, "name": 3, "ext": 4, "ans1": 8, "ans2": 9,
         "verdict": 10, "note": 11, "relpath": 12, "eng": 13, "snap": 14,
         "signal": 15, "stratum": 16, "round": 17}

# 보안등급 시트의 열 번호(1부터).
S_COL = {"no": 1, "folder": 2, "name": 3, "ext": 4, "answer": 8, "basis": 9,
         "note": 10, "relpath": 11, "eng": 12, "method": 13, "signal": 14,
         "stratum": 15}

# 보안등급의 서열. 과대/과소 등급을 세는 데 쓴다(O < S < C).
GRADE_RANK = {"O": 0, "S": 1, "C": 2}

# 정답 칸에 들어올 수 있는 '정답 없음' 판정. 이 판정이면 정답1/정답2 는 비어 있어야 한다.
NO_LABEL_VERDICT = ("분류대상아님", "체계에없음")


#------------------------------------------------------------------
# 경로 정규화 (비교용)
#=> 엔진 결과의 file 과 평가셋의 상대경로가 구분자(\ vs /)·대소문자만 달라도
#   같은 문서로 이어지게 한다. build_evalsets.py 의 norm_path 와 같은 규칙이다.
#
# -in: p = 파일 경로
#
# -out: str = 비교용으로 정규화한 경로
# -out: error = 없음
#------------------------------------------------------------------
def norm_path(p):
    return os.path.normcase(os.path.normpath(p))


#------------------------------------------------------------------
# 엔진 실행 결과(jsonl) 읽기
#=> --run/--prev 로 받은 실행 결과를 '경로 → 레코드' 표로 만든다.
#    1) 첫 줄의 {"run": ...} 머리글은 문서가 아니므로 따로 빼 둔다(규칙 판 표시에 쓴다)
#    2) 마지막의 {"summary": ...} 같은 줄도 file 칸이 없으므로 자연히 걸러진다
#
# -in: path = 결과 jsonl 경로. None 이면 아무것도 안 읽는다
#
# -out: (recs, meta) = recs: {정규화경로: 레코드}, meta: 실행 머리글 dict(없으면 {})
# -out: error = 파일이 없으면 SystemExit(사람이 읽는 문장으로 끝낸다)
#------------------------------------------------------------------
def load_run(path):
    if not path:
        return {}, {}
    if not os.path.isfile(path):
        sys.exit(f"엔진 결과 파일이 없습니다: {path}")
    recs, meta = {}, {}
    with open(path, encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                # 깨진 줄 하나 때문에 채점 전체가 멈추면 안 된다 — 건너뛰고 계속한다.
                continue
            if "run" in rec and "file" not in rec:
                meta = rec["run"]
            elif "file" in rec:
                recs[norm_path(rec["file"])] = rec
    return recs, meta


#------------------------------------------------------------------
# '집계' 시트에서 층별 모집단 수 읽기
#=> 층 이름(D1_…·G1_… )과 그 층의 전체 문서 수를 짝지어 모은다. 이 값이 있어야
#   표본 결과를 모집단 추정치로 환산할 수 있다.
#
# -in: agg    = '집계' 시트
# -in: prefix = 층 이름의 첫 글자("D" 업무분류 · "G" 보안등급)
#
# -out: dict = {층 이름: 모집단 수}
# -out: error = 없음(못 찾으면 빈 dict)
#------------------------------------------------------------------
def strata_pop(agg, prefix):
    pop = {}
    for r in range(1, agg.max_row + 1):
        a, b = agg.cell(r, 1).value, agg.cell(r, 2).value
        if isinstance(a, str) and a.startswith(prefix) and "_" in a and isinstance(b, (int, float)):
            pop[a] = b
    return pop


#------------------------------------------------------------------
# 비고 칸으로 정답의 출처 구분하기
#=> 정답을 사람이 직접 달았는지, AI 가 달아 사람이 확인만 했는지 나눈다.
#   AI 가 단 정답은 엔진과 같은 단서를 봤을 수 있어 성적이 실제보다 좋게 나온다.
#   그래서 '사람' 부분집합의 성적을 따로 보는 것이 이 함수의 목적이다.
#
#   [알아 둘 왜곡] 2026-09-14 에 DC_007(엠파워제품)을 나중에 평가셋에 넣으면서,
#   원래 사람이 정답을 달았던 행에도 "[AI라벨·DC_007]" 표식이 붙었다. 그 행들은
#   정답1 은 사람 것인데 여기서는 'AI' 로 분류된다 — 그래서 '사람' 표본이
#   132건에서 113건 남짓으로 줄어 보인다. 수치를 인용할 때 함께 말할 것.
#
# -in: note = 비고 칸 문자열(없으면 빈 문자열)
#
# -out: str = "사람" | "AI" | "검토권장"
# -out: error = 없음
#------------------------------------------------------------------
def tag_of(note):
    note = str(note or "")
    if "검토권장" in note:
        return "검토권장"
    # "AI라벨"·"AI추가C"·"AI검토" 를 한꺼번에 잡는다 — 표식 이름이 축마다 다르다.
    return "AI" if "AI" in note else "사람"


#------------------------------------------------------------------
# 업무분류 평가셋 읽기
#=> 시트 한 줄을 채점에 필요한 값만 남긴 dict 로 바꾸고, 엔진 판정을 붙인다.
#    1) 정답1/정답2/판정/층/비고 를 읽는다
#    2) 엔진 판정은 --run 이 있으면 그 결과에서, 없으면 xlsx 의 ref_현행엔진 열에서 가져온다
#    3) 정답 칸이 앞뒤가 맞는지 함께 점검한다(분류체계에 없는 dc_id, 빈 판정 등)
#
# -in: wb      = 업무분류 평가셋 워크북
# -in: run     = --run 결과 {경로: 레코드}(없으면 빈 dict)
# -in: prev    = --prev 결과 {경로: 레코드}(없으면 빈 dict)
# -in: src_dir = 상대경로를 붙일 문서 뿌리 폴더
#
# -out: (rows, bad, missing) = rows: 채점용 행 리스트,
#        bad: 정답 시트의 이상한 줄 [(no, 사유)],
#        missing: --run 결과에서 못 찾은 문서 [(no, 경로)]
# -out: error = 없음
#------------------------------------------------------------------
def read_doctype_rows(wb, run, prev, src_dir):
    ws, c = wb["평가셋"], D_COL
    valid = {r[0] for r in wb["분류체계"].iter_rows(min_row=2, values_only=True) if r[0]}
    rows, bad, missing = [], [], []
    for r in range(2, ws.max_row + 1):
        if ws.cell(r, c["no"]).value is None:
            continue
        cell = lambda k: ws.cell(r, c[k]).value
        strip = lambda v: v.strip() or None if isinstance(v, str) else v
        x = dict(no=cell("no"), folder=cell("folder"), name=cell("name"), ext=cell("ext"),
                 ans1=strip(cell("ans1")), ans2=strip(cell("ans2")),
                 verdict=strip(cell("verdict")), note=str(cell("note") or ""),
                 stratum=cell("stratum"), round=cell("round"),
                 signal=cell("signal") or "", relpath=cell("relpath") or "")
        x["tag"] = tag_of(x["note"])

        # ── 정답 시트 자체의 앞뒤 맞음 점검(채점 전에 사람에게 알린다) ──────────
        if not x["verdict"]:
            bad.append((x["no"], "판정 비어있음"))
        elif x["verdict"] == "정답있음" and x["ans1"] not in valid:
            bad.append((x["no"], f"정답1 이상: {x['ans1']}"))
        elif x["verdict"] in NO_LABEL_VERDICT and (x["ans1"] or x["ans2"]):
            bad.append((x["no"], "'정답 없음' 판정인데 정답 칸이 채워져 있음"))
        if x["ans2"] and x["ans2"] not in valid:
            bad.append((x["no"], f"정답2 이상: {x['ans2']}"))

        # ── 엔진 판정 붙이기 ────────────────────────────────────────────────
        if run:
            p = norm_path(os.path.join(src_dir, x["relpath"]))
            rec = run.get(p)
            if rec is None:
                missing.append((x["no"], x["relpath"]))
                x["eng"], x["src"] = [], {}
            else:
                x["eng"] = rec.get("doctype") or []
                vals = ((rec.get("why") or {}).get("doctype") or {}).get("values") or []
                # 어느 신호(제목·앞부분·본문·파일이름)로 붙었는지 — 오부착 원인 분석용
                x["src"] = {v.get("dc_id"): "+".join(v.get("from") or []) for v in vals}
            x["prev"] = (prev.get(p) or {}).get("doctype") or [] if prev else None
        else:
            # xlsx 의 ref_현행엔진 열은 "DC_a;DC_b" 꼴 문자열이다.
            x["eng"] = [d for d in str(cell("eng") or "").split(";") if d]
            x["src"] = {}
            x["prev"] = [d for d in str(cell("snap") or "").split(";") if d] if prev is None else None
        rows.append(x)
    return rows, bad, missing


#------------------------------------------------------------------
# 업무분류 한 건의 채점 등급
#=> 정답과 엔진 라벨을 견줘 다섯 갈래로 가른다.
#     적중     정답으로 단 분류를 엔진도 달았다
#     오라벨   엔진이 달긴 달았는데 정답이 아니다
#     놓침     정답이 있는데 엔진이 아무것도 안 달았다
#     헛라벨   정답이 '없음'(분류대상아님·체계에없음)인데 엔진이 달았다
#     정상무라벨 정답이 '없음'이고 엔진도 안 달았다
#
#   [정답2 도 정답이다] 한 문서가 두 분류에 걸칠 수 있어 정답 칸이 둘이다.
#   둘 중 하나만 맞혀도 적중으로 본다 — 사람도 둘 다 맞다고 판단한 것이라 그렇다.
#
# -in: x   = read_doctype_rows 가 만든 행
# -in: key = 견줄 엔진 판정 칸 이름("eng" 현재 · "prev" 이전)
#
# -out: str = 위 다섯 중 하나(판단불가·빈 판정이면 그 값 또는 빈 문자열)
# -out: error = 없음
#------------------------------------------------------------------
def doc_grade(x, key="eng"):
    verdict, ans1, ans2, got = x["verdict"], x["ans1"], x["ans2"], x[key] or []
    if not verdict or verdict == "판단불가":
        return verdict or ""
    if verdict in NO_LABEL_VERDICT:
        return "정상무라벨" if not got else "헛라벨"
    if not ans1:
        return ""
    if not got:
        return "놓침"
    return "적중" if ans1 in got or (ans2 and ans2 in got) else "오라벨"


#------------------------------------------------------------------
# 업무분류 지표 계산
#=> 행 묶음 하나의 적중·오라벨·헛라벨·놓침을 세고 정밀도·재현율을 낸다.
#
#   [정밀도] 엔진이 단 라벨 중 맞은 비율 = 적중 / (적중+오라벨+헛라벨)
#   [재현율] 정답이 있는 문서 중 맞힌 비율 = 적중 / (적중+오라벨+놓침)
#   오라벨은 양쪽 분모에 다 들어간다 — 틀린 라벨을 붙인 동시에 정답을 놓친 것이라 그렇다.
#
# -in: rows     = 행 리스트
# -in: key      = 견줄 엔진 판정 칸("eng"·"prev")
# -in: weighted = True 면 층별 가중치를 곱해 모집단 추정치로 만든다
# -in: pop      = {층: 모집단 수}
# -in: n_by     = {층: 표본 수}
#
# -out: dict = 건수들과 precision·recall(분모가 0이면 None)
# -out: error = 없음
#------------------------------------------------------------------
def doc_metrics(rows, key, weighted, pop, n_by):
    c = collections.Counter()
    for x in rows:
        w = pop[x["stratum"]] / n_by[x["stratum"]] if weighted else 1
        c[doc_grade(x, key)] += w
    hit, wrong, false_, miss = c["적중"], c["오라벨"], c["헛라벨"], c["놓침"]
    return {"n": len(rows), "적중": round(hit, 1), "오라벨": round(wrong, 1),
            "헛라벨": round(false_, 1), "놓침": round(miss, 1),
            "정상무라벨": round(c["정상무라벨"], 1), "판단불가": round(c["판단불가"], 1),
            "precision": round(hit / (hit + wrong + false_), 4) if hit + wrong + false_ else None,
            "recall": round(hit / (hit + wrong + miss), 4) if hit + wrong + miss else None}


#------------------------------------------------------------------
# 보안등급 평가셋 읽기
#=> 업무분류와 같은 일을 보안등급 시트에 한다. 정답이 등급 한 글자(C/S/O)라
#   구조가 더 단순하다.
#
# -in: wb / run / prev / src_dir = read_doctype_rows 와 같다
#
# -out: (rows, bad, missing) = read_doctype_rows 와 같은 모양
# -out: error = 없음
#------------------------------------------------------------------
def read_security_rows(wb, run, prev, src_dir):
    ws, c = wb["평가셋"], S_COL
    rows, bad, missing = [], [], []
    for r in range(2, ws.max_row + 1):
        if ws.cell(r, c["no"]).value is None:
            continue
        cell = lambda k: ws.cell(r, c[k]).value
        answer = cell("answer")
        x = dict(no=cell("no"), folder=cell("folder"), name=cell("name"), ext=cell("ext"),
                 answer=str(answer).strip() if answer else None, basis=cell("basis"),
                 note=str(cell("note") or ""), stratum=cell("stratum"),
                 relpath=cell("relpath") or "", method=cell("method"))
        x["tag"] = tag_of(x["note"])
        if x["answer"] not in ("C", "S", "O", "판단불가"):
            bad.append((x["no"], f"정답등급 이상: {x['answer']}"))

        if run:
            p = norm_path(os.path.join(src_dir, x["relpath"]))
            rec = run.get(p)
            if rec is None:
                missing.append((x["no"], x["relpath"]))
                x["eng"], x["signal"] = "", ""
            else:
                x["eng"] = rec.get("grade") or ""
                x["signal"] = security_hits(rec)
            x["prev"] = (prev.get(p) or {}).get("grade") or "" if prev else None
        else:
            x["eng"] = cell("eng") or ""
            x["signal"] = cell("signal") or ""
            x["prev"] = None
        rows.append(x)
    return rows, bad, missing


#------------------------------------------------------------------
# 보안등급 레코드에서 걸린 신호 이름 뽑기
#=> 오탐을 볼 때 "무엇 때문에 C 가 됐나"를 바로 읽을 수 있게, 규칙 이름과 횟수를
#   한 줄로 잇는다.
#
# -in: rec = 엔진 결과 레코드 한 건
#
# -out: str = "국적×2, 기밀표시×1" 꼴(최대 8개). 신호가 없으면 빈 문자열
# -out: error = 없음
#------------------------------------------------------------------
def security_hits(rec):
    sec = (rec.get("why") or {}).get("security") or {}
    hits = [f"{h.get('name')}×{h.get('count')}"
            for sig in (sec.get("signals") or {}).values()
            for h in (sig or {}).get("hits") or []]
    return ", ".join(hits[:8])


#------------------------------------------------------------------
# 보안등급 한 건의 채점 등급
#=> 이 평가셋의 목적이 'C 를 놓치지 않는가'라서 C 를 기준으로 네 갈래로 가른다.
#     C적중  정답 C · 엔진 C
#     C놓침  정답 C · 엔진이 C 가 아님   ← 가장 아픈 오류(기밀이 새어 나간다)
#     C오탐  정답이 C 가 아닌데 엔진이 C  ← 사람 손이 더 가게 만드는 오류
#     비C정상 정답도 엔진도 C 가 아님
#
# -in: x   = read_security_rows 가 만든 행
# -in: key = 견줄 엔진 판정 칸("eng"·"prev")
#
# -out: str = 위 넷 중 하나(판단불가·빈 정답이면 그 값 또는 빈 문자열)
# -out: error = 없음
#------------------------------------------------------------------
def sec_grade(x, key="eng"):
    if not x["answer"] or x["answer"] == "판단불가":
        return x["answer"] or ""
    if x["answer"] == "C":
        return "C적중" if x[key] == "C" else "C놓침"
    return "C오탐" if x[key] == "C" else "비C정상"


#------------------------------------------------------------------
# 보안등급 지표 계산
#=> C 재현율·정밀도와 함께 3등급 일치·과대·과소 비율을 낸다.
#   과대등급(실제보다 높게 매김)은 C 정밀도와 다른 이야기다 — O 를 S 로 올린 것은
#   C 정밀도에 안 잡히지만 사용자에게는 똑같이 불편하다. 그래서 따로 센다.
#
# -in: rows / key / weighted / pop / n_by = doc_metrics 와 같다
#
# -out: dict = 건수와 C_recall·C_precision·3등급 일치/과대/과소 비율
# -out: error = 없음
#------------------------------------------------------------------
def sec_metrics(rows, key, weighted, pop, n_by):
    c = collections.Counter()
    exact = over = under = total = 0.0
    for x in rows:
        w = pop[x["stratum"]] / n_by[x["stratum"]] if weighted else 1
        c[sec_grade(x, key)] += w
        if x["answer"] in GRADE_RANK:
            got = GRADE_RANK.get(x[key], -1)   # 미분류는 -1 - 가장 낮은 것으로 본다
            want = GRADE_RANK[x["answer"]]
            total += w
            exact += w if got == want else 0
            over += w if got > want else 0
            under += w if got < want else 0
    tp, fn, fp = c["C적중"], c["C놓침"], c["C오탐"]
    pct = lambda v: round(v / total, 4) if total else None
    return {"n": len(rows), "C적중": round(tp, 1), "C놓침": round(fn, 1), "C오탐": round(fp, 1),
            "비C정상": round(c["비C정상"], 1), "판단불가": round(c["판단불가"], 1),
            "C_recall": round(tp / (tp + fn), 4) if tp + fn else None,
            "C_precision": round(tp / (tp + fp), 4) if tp + fp else None,
            "3등급일치": pct(exact), "과대등급": pct(over), "과소등급": pct(under)}


#------------------------------------------------------------------
# 지표 dict 한 줄 표기
#=> 화면에 쓸 한 줄로 만든다. 비율은 사람이 읽는 % 로 쓴다(설계 규약 — 0.85 가 아니라 85%).
#
# -in: m    = doc_metrics/sec_metrics 결과
# -in: keys = 비율로 보여 줄 칸 이름들
#
# -out: str = 한 줄 문자열
# -out: error = 없음
#------------------------------------------------------------------
def fmt(m, keys):
    counts = " ".join(f"{k}{m[k]:g}" for k in m
                      if k not in ("n", "precision", "recall") and k not in keys
                      and isinstance(m[k], (int, float)))
    rates = " ".join(f"{k} {m[k]:.1%}" if m[k] is not None else f"{k} -" for k in keys)
    return f"[{m['n']}건] {counts} | {rates}"


#------------------------------------------------------------------
# 업무분류 채점 보고
#=> 지표를 층·출처·회차별로 갈라 찍고, 요구하면 오류 목록까지 펼친다.
#
# -in: rows / pop / n_by = 채점 재료
# -in: prev_on = 이전 판과 견주는 중인가
# -in: details = True 면 놓침·오부착 목록을 분류별로 펼친다
# -in: taxonomy = {dc_id: 전체경로} 이름표(화면 표기용)
#
# -out: dict = --json 으로 내보낼 결과
# -out: error = 없음
#------------------------------------------------------------------
def report_doctype(rows, pop, n_by, prev_on, details, taxonomy):
    keys = ("precision", "recall")
    met = lambda sub, key="eng", w=False: doc_metrics(sub, key, w, pop, n_by)
    out = {"sample": met(rows), "population": met(rows, w=True),
           "by_tag": {}, "by_stratum": {}, "by_answer": {}}

    print("\n── 업무분류 ─────────────────────────────────────────────")
    print("  표본      ", fmt(out["sample"], keys))
    print("  모집단추정", fmt(out["population"], keys))

    print("\n  [정답 출처별] - AI 가 단 정답은 엔진과 같은 단서를 봤을 수 있어 성적이 낙관적이다")
    for t in ("사람", "AI", "검토권장"):
        sub = [x for x in rows if x["tag"] == t]
        if sub:
            out["by_tag"][t] = met(sub)
            print(f"   {t:<6}", fmt(out["by_tag"][t], keys))

    print("\n  [층별]")
    for s in sorted(pop):
        sub = [x for x in rows if x["stratum"] == s]
        if sub:
            out["by_stratum"][s] = met(sub)
            print(f"   {s:<12}", fmt(out["by_stratum"][s], keys))

    # 분류별 재현/부착 — 어느 분류가 약한지 한눈에 본다.
    name = lambda d: (taxonomy.get(d, d) or d).split(" > ")[-1]
    print("\n  [분류별] 정답건수 / 그중 맞힘 · 엔진부착 / 그중 맞음")
    for a in sorted({x["ans1"] for x in rows if x["ans1"]}):
        gold = [x for x in rows if a in (x["ans1"], x["ans2"])]
        got = sum(1 for x in gold if a in x["eng"])
        pred = [x for x in rows if a in x["eng"]]
        right = sum(1 for x in pred if a in (x["ans1"], x["ans2"]))
        out["by_answer"][a] = {"정답": len(gold), "재현": got,
                               "엔진부착": len(pred), "그중맞음": right}
        flag = "  <-" if gold and got / len(gold) < 0.6 else ""
        print(f"   {name(a):<12}({a}) 정답 {len(gold):>3} / 맞힘 {got:>3}"
              f"   부착 {len(pred):>3} / 맞음 {right:>3}{flag}")

    if prev_on:
        out["prev_sample"] = met(rows, "prev")
        out["prev_population"] = met(rows, "prev", True)
        print("\n  [이전 판]")
        print("   표본      ", fmt(out["prev_sample"], keys))
        print("   모집단추정", fmt(out["prev_population"], keys))
        changed = [x for x in rows if doc_grade(x) != doc_grade(x, "prev")]
        out["changed"] = len(changed)
        counter = collections.Counter(f"{doc_grade(x, 'prev')}→{doc_grade(x)}" for x in changed)
        print(f"   판정이 바뀐 문서 {len(changed)}건 {dict(counter)}")
        for x in changed[:40]:
            print(f"     {x['no']:>3} {doc_grade(x, 'prev')}→{doc_grade(x)}"
                  f" 정답={x['ans1']} 엔진={x['eng']} (이전 {x['prev']}) {str(x['name'])[:40]}")

    if details:
        print("\n  [놓침] 정답 분류별 - 규칙에 넣을 말을 찾는 자리다")
        miss = collections.defaultdict(list)
        for x in rows:
            if doc_grade(x) == "놓침":
                miss[x["ans1"]].append(x)
        for d, xs in sorted(miss.items(), key=lambda z: -len(z[1])):
            folders = dict(collections.Counter(x["folder"] for x in xs).most_common(4))
            print(f"   {name(d)}({d}) 놓침 {len(xs)}건 · 폴더 {folders}")
            for x in xs[:8]:
                print(f"     {x['no']:>3} {x['ext']} [{str(x['folder'])[:16]}] {str(x['name'])[:50]}")

        print("\n  [오라벨·헛라벨] 엔진이 잘못 단 분류별")
        badlab = collections.defaultdict(list)
        for x in rows:
            if doc_grade(x) in ("오라벨", "헛라벨"):
                for d in x["eng"]:
                    if d not in (x["ans1"], x["ans2"]):
                        badlab[d].append(x)
        for d, xs in sorted(badlab.items(), key=lambda z: -len(z[1])):
            right = sum(1 for x in rows if d in x["eng"] and d in (x["ans1"], x["ans2"]))
            print(f"   {name(d)}({d}) 틀린부착 {len(xs)} / 맞은부착 {right}")
            for x in xs[:12]:
                ans = name(x["ans1"]) if x["ans1"] else x["verdict"]
                sig = x["src"].get(d) or x.get("signal") or "-"
                print(f"     {x['no']:>3} {doc_grade(x)} 정답={ans} 신호={sig}"
                      f" [{str(x['folder'])[:14]}] {str(x['name'])[:42]}")
    return out


#------------------------------------------------------------------
# 보안등급 채점 보고
#=> C 재현율·정밀도를 층·출처별로 찍고, 요구하면 놓친 C 와 오탐 목록을 펼친다.
#
# -in: rows / pop / n_by / prev_on / details = report_doctype 와 같다
#
# -out: dict = --json 으로 내보낼 결과
# -out: error = 없음
#------------------------------------------------------------------
def report_security(rows, pop, n_by, prev_on, details):
    keys = ("C_recall", "C_precision", "3등급일치", "과대등급", "과소등급")
    met = lambda sub, key="eng", w=False: sec_metrics(sub, key, w, pop, n_by)
    out = {"sample": met(rows), "population": met(rows, w=True),
           "by_tag": {}, "by_stratum": {}}

    print("\n── 보안등급 ─────────────────────────────────────────────")
    print("  표본      ", fmt(out["sample"], keys))
    print("  모집단추정", fmt(out["population"], keys))

    print("\n  [정답 출처별]")
    for t in ("사람", "AI", "검토권장"):
        sub = [x for x in rows if x["tag"] == t]
        if sub:
            out["by_tag"][t] = met(sub)
            print(f"   {t:<6}", fmt(out["by_tag"][t], keys))

    print("\n  [층별]")
    for s in sorted(pop):
        sub = [x for x in rows if x["stratum"] == s]
        if sub:
            out["by_stratum"][s] = met(sub)
            print(f"   {s:<12}", fmt(out["by_stratum"][s], keys))

    # 정답 등급 → 엔진 등급 표. 어느 칸이 새는지 한눈에 본다.
    conf = collections.Counter(f"{x['answer']}→{x['eng'] or '미분류'}" for x in rows)
    out["confusion"] = dict(sorted(conf.items()))
    print("\n  [정답→엔진]", out["confusion"])

    if prev_on:
        out["prev_sample"] = met(rows, "prev")
        out["prev_population"] = met(rows, "prev", True)
        print("\n  [이전 판]")
        print("   표본      ", fmt(out["prev_sample"], keys))
        print("   모집단추정", fmt(out["prev_population"], keys))
        changed = [x for x in rows if x["eng"] != x["prev"]]
        out["changed"] = len(changed)
        counter = collections.Counter(
            f"{x['prev'] or '미분류'}→{x['eng'] or '미분류'}(정답 {x['answer']})" for x in changed)
        print(f"   등급이 바뀐 문서 {len(changed)}건 {dict(counter)}")
        for x in changed[:40]:
            print(f"     {x['no']:>3} {x['prev'] or '미분류'}→{x['eng'] or '미분류'}"
                  f" 정답{x['answer']} [{str(x['folder'])[:14]}] {str(x['name'])[:42]}"
                  f" | {str(x['signal'])[:60]}")

    if details:
        print("\n  [C 놓침] - 기밀이 새는 자리다")
        for x in rows:
            if sec_grade(x) == "C놓침":
                print(f"   {x['no']:>3} {x['eng'] or '미분류'} 근거={x['basis']}"
                      f" [{str(x['folder'])[:14]}] {str(x['name'])[:50]}"
                      f" | {str(x['signal'])[:60]}")
        print("\n  [C 오탐] 걸린 신호별 - 규칙을 좁힐 자리다")
        top = collections.Counter(
            str(x["signal"] or "").split(",")[0].split("×")[0].strip()
            for x in rows if sec_grade(x) == "C오탐")
        for sig, cnt in top.most_common(12):
            print(f"   {sig or '(신호없음)'} {cnt}건")
    return out


#------------------------------------------------------------------
# 시트 점검 결과 알리기
#=> 정답 시트의 이상한 줄과, 엔진 결과에서 못 찾은 문서를 사람에게 알린다.
#   조용히 빼고 채점하면 "왜 건수가 줄었지"를 아무도 모른다.
#
# -in: label   = 축 이름(화면 표기용)
# -in: bad     = [(no, 사유)]
# -in: missing = [(no, 상대경로)]
#
# -out: 없음(화면에 찍는다)
# -out: error = 없음
#------------------------------------------------------------------
def warn(label, bad, missing):
    if bad:
        print(f"  [!] {label} 정답 시트에 이상한 줄 {len(bad)}건:")
        for no, why in bad[:10]:
            print(f"     {no}행 - {why}")
    if missing:
        print(f"  [!] {label} 엔진 결과에서 못 찾은 문서 {len(missing)}건"
              f" (경로가 바뀌었거나 실행에서 빠졌습니다):")
        for no, rel in missing[:10]:
            print(f"     {no}행 - {rel}")


#------------------------------------------------------------------
# 진입점
#=> 인자를 받아 축마다 채점하고, 요구하면 결과를 json 으로도 남긴다.
#
# -in: argv = 명령행 인자(None 이면 sys.argv)
#
# -out: int = 종료코드 0(채점 성공) / 2(정답 시트에 이상한 줄이 있음)
# -out: error = 파일이 없으면 SystemExit
#------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="평가셋 채점 - 업무분류 정밀도·재현율 / 보안등급 C 재현율")
    ap.add_argument("--axis", choices=("doctype", "security", "both"), default="both",
                    help="채점할 축(기본 both)")
    ap.add_argument("--run", metavar="JSONL",
                    help="채점할 엔진 실행 결과. 없으면 xlsx 의 ref_ 열로 채점한다")
    ap.add_argument("--prev", metavar="JSONL",
                    help="견줄 이전 실행 결과(규칙을 고치기 전 결과)")
    ap.add_argument("--doctype-xlsx", default=DOCTYPE_XLSX, help="업무분류 평가셋 경로")
    ap.add_argument("--security-xlsx", default=SECURITY_XLSX, help="보안등급 평가셋 경로")
    ap.add_argument("--src-dir", default=SRC_DIR,
                    help=f"ref_상대경로를 붙일 문서 뿌리 폴더(기본 {SRC_DIR})")
    ap.add_argument("--details", action="store_true",
                    help="놓침·오부착 목록까지 펼친다")
    ap.add_argument("--json", metavar="경로", help="결과를 json 으로도 남긴다")
    args = ap.parse_args(argv)

    run, run_meta = load_run(args.run)
    prev, _ = load_run(args.prev)
    if args.run:
        print(f"채점 대상: {args.run}  규칙 {run_meta.get('rule_version', '?')}"
              f" · 업무분류규칙 {run_meta.get('doctype_rule_version', '?')}"
              f" · 분류체계 {run_meta.get('taxonomy_version', '?')}")
    else:
        print("채점 대상: xlsx 의 ref_ 열(평가셋을 만든 시점의 판정)"
              " - 규칙을 고친 뒤라면 --run 으로 새 결과를 주세요")

    result, has_bad = {}, False

    if args.axis in ("doctype", "both"):
        if not os.path.isfile(args.doctype_xlsx):
            sys.exit(f"업무분류 평가셋이 없습니다: {args.doctype_xlsx}")
        wb = openpyxl.load_workbook(args.doctype_xlsx)
        taxonomy = {r[0]: r[1] for r in wb["분류체계"].iter_rows(min_row=2, values_only=True) if r[0]}
        rows, bad, missing = read_doctype_rows(wb, run, prev, args.src_dir)
        pop = strata_pop(wb["집계"], "D")
        n_by = collections.Counter(x["stratum"] for x in rows)
        warn("업무분류", bad, missing)
        has_bad = has_bad or bool(bad)
        result["doctype"] = report_doctype(rows, pop, n_by, bool(args.prev), args.details, taxonomy)
        result["doctype"]["bad"] = bad
        result["doctype"]["missing"] = missing
        wb.close()

    if args.axis in ("security", "both"):
        if not os.path.isfile(args.security_xlsx):
            sys.exit(f"보안등급 평가셋이 없습니다: {args.security_xlsx}")
        wb = openpyxl.load_workbook(args.security_xlsx)
        rows, bad, missing = read_security_rows(wb, run, prev, args.src_dir)
        pop = strata_pop(wb["집계"], "G")
        n_by = collections.Counter(x["stratum"] for x in rows)
        warn("보안등급", bad, missing)
        has_bad = has_bad or bool(bad)
        result["security"] = report_security(rows, pop, n_by, bool(args.prev), args.details)
        result["security"]["bad"] = bad
        result["security"]["missing"] = missing
        wb.close()

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fp:
            json.dump(result, fp, ensure_ascii=False, indent=1, default=str)
        print(f"\n결과를 남겼습니다: {args.json}")

    # 정답 시트가 깨져 있으면 자동화가 알아채야 한다 — 숫자는 이미 찍었으므로 2로 끝낸다.
    return 2 if has_bad else 0


if __name__ == "__main__":
    sys.exit(main())
