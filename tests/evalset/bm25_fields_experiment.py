"""업무분류 2차 BM25 — 비교 필드를 늘리면 나아지나(앞 1~2문장 · 키워드) 실험.

bm25_experiment.py 의 준비 · 채점 · 자기 제외 · 문턱 고르기를 그대로 쓰고, 기준 문서와
평가 문서에 필드를 더해 견준다. 지금 설정(제목 + 정리 파일명)이 기준선이다.
"""
import argparse
import collections
import json
import math
import os
import random
import re
import sys

import openpyxl
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import score_evalsets as S  # noqa: E402
import bm25_experiment as X  # noqa: E402

# 문장 끝(마침표 · 물음표 · 느낌표 · '다.') 또는 줄바꿈으로 나눈다
_SENT = re.compile(r"(?<=[.!?])\s+|\n+")


#------------------------------------------------------------------
# 제목 다음 앞부분 뽑기
#=> 첫 비어 있지 않은 줄(제목)을 뺀 나머지에서
#    · chars 모드: 앞 n 글자
#    · sents 모드: 비어 있지 않은 문장(또는 줄) n 개, 최대 200자
#
# -in: text = 추출 본문
# -in: mode = "chars" | "sents"
# -in: n    = 글자 수 또는 문장 수
#
# -out: str = 뽑은 글(없으면 빈 문자열)
# -out: error = 없음
#------------------------------------------------------------------
def lead_of(text, mode, n):
    lines = (text or "").split("\n")
    i = next((k for k, l in enumerate(lines) if l.strip()), len(lines))
    rest = "\n".join(lines[i + 1:]).strip()
    if mode == "chars":
        return rest[:n]
    sents = [s.strip() for s in _SENT.split(rest) if s and s.strip()]
    return " ".join(sents[:n])[:200]


#------------------------------------------------------------------
# 키워드 뽑기(tf-idf 상위)
#=> 본문 전체 토큰 가운데 이 문서에 자주 나오고 기준 문서 전체에는 드문 것을 고른다.
#
# -in: text = 추출 본문
# -in: kind = "w"(단어) | "g"(글자 2-gram)
# -in: k    = 고를 개수
# -in: df   = {토큰: 그 토큰이 나온 기준 문서 수}
# -in: N    = 기준 문서 수
#
# -out: list[str] = 키워드 토큰(점수 높은 순)
# -out: error = 없음
#------------------------------------------------------------------
def keywords_of(text, kind, k, df, N):
    tf = collections.Counter(t for t in X.tok_bigram(text or "") if t.startswith(kind + ":"))
    score = {t: c * math.log(1 + N / (1 + df.get(t, 0))) for t, c in tf.items()}
    return [t for t, _ in sorted(score.items(), key=lambda x: (-x[1], x[0]))[:k]]


# 파워포인트 서식 틀(마스터) 기본 문구 — 문서마다 똑같이 나와 제목이 아니다
_PLACEHOLDER = ("마스터 제목 스타일 편집", "마스터 텍스트 스타일을 편집합니다", "마스터 부제목 스타일 편집",
                "click to edit master title style", "click to edit master text styles")


#------------------------------------------------------------------
# ppt · xls · xlsx 제목 고르기(제안 규칙)
#=> 지금은 이 형식들의 제목을 비운다. 제안: 첫 줄이 5자 이하면 다음 줄로 넘어가 첫 '5자 넘는 줄'을 쓴다.
#   skip_master=True 면 파워포인트 서식 틀 기본 문구도 건너뛴다.
#
# -in: text        = 추출 본문
# -in: file        = 파일 경로
# -in: skip_master = 서식 틀 문구를 건너뛸지
#
# -out: str = 제목(형식이 ppt · xls · xlsx 가 아니면 지금 규칙의 제목)
# -out: error = 없음
#------------------------------------------------------------------
def title_x(text, file, skip_master=False):
    if os.path.splitext(file)[1].lower() not in X.TITLE_UNSAFE_EXTS:
        return X.fields_of(text, file, 400, ())["title"]
    for line in (text or "").split("\n"):
        t = line.strip()
        if len(t) <= 5:
            continue
        if skip_master and t.lower() in _PLACEHOLDER:
            continue
        return t[:200]
    return ""


#------------------------------------------------------------------
# 한 문서의 실험 필드 묶음
#
# -in: text  = 추출 본문
# -in: file  = 파일 경로
# -in: noise = noise_tails
# -in: df    = 키워드용 기준 문서 df(w · g 둘 다 든 dict)
# -in: N     = 기준 문서 수
#
# -out: dict = {필드: 토큰 목록}
# -out: error = 없음
#------------------------------------------------------------------
def fields(text, file, noise, df, N):
    f = X.fields_of(text, file, 400, noise)
    lead100, lead2 = lead_of(text, "chars", 100), lead_of(text, "sents", 2)
    out = {"title": X.tok_bigram(f["title"]), "name2": X.tok_bigram(f["name2"]),
           "lead100": X.tok_bigram(lead100), "lead2": X.tok_bigram(lead2),
           "title_lead2": X.tok_bigram((f["title"] + " " + lead2).strip()),
           "kww10": keywords_of(text, "w", 10, df, N), "kwg20": keywords_of(text, "g", 20, df, N),
           "title_x": X.tok_bigram(title_x(text, file)), "title_xm": X.tok_bigram(title_x(text, file, True))}
    return out


#------------------------------------------------------------------
# 실험 전체
#=> 1) bm25_experiment 와 같은 재료 · 같은 자기 제외(엄격) · 같은 반 나누기
#   2) 변형마다 필드 무게 후보와 문턱(절댓값 분위수)을 앞 절반에서 고르고 표로 낸다
#   3) 지금 채택한 설정(파일명 단독은 확신 0.55 표시)을 그대로 쓴다
#
# -in: argv = 명령줄 인자(None 이면 sys.argv)
#
# -out: 없음(표준출력 + --json)
# -out: error = 입력 파일이 없으면 예외 전파
#------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", default=os.path.join(S.ROOT, "resources", "golden_dataset", "업무분류-평가셋-500-20260918-사람재검-01.xlsx"))
    ap.add_argument("--run-a", required=True)
    ap.add_argument("--run-b", required=True)
    ap.add_argument("--text", required=True)
    ap.add_argument("--policy", default=os.path.join(S.ROOT, "ui", "policy"))
    ap.add_argument("--seed-text", default=os.path.join(S.ROOT, "ui", "_extracted"))
    ap.add_argument("--json")
    ap.add_argument("--seed", type=int, default=20261022)
    a = ap.parse_args(argv)

    rule = yaml.safe_load(open(os.path.join(a.policy, "doc_rule.yaml"), encoding="utf-8"))
    emb = rule.get("embed") or {}
    ctx = {"tax": X.load_taxonomy(os.path.join(a.policy, "doc_classification_export.json")),
           "embed": {"k": emb.get("k", 5), "dup_threshold": emb.get("dup_threshold", 0.95),
                     "min_sim": emb.get("min_sim", 0.70), "min_share": emb.get("min_share", 0.40)},
           "merge": {"embed_cap": (rule.get("defaults") or {}).get("embed_cap", 0.65),
                     "cap_alone": 0.60, "cap_agree": 0.75, "top_n": (rule.get("conflict") or {}).get("n", 2)}}
    noise = rule.get("noise_tails") or []
    wb = openpyxl.load_workbook(a.xlsx)
    rows, _, _ = S.read_doctype_rows(wb, {}, None, S.SRC_DIR)
    run_a, run_b = X.load_jsonl(a.run_a), X.load_jsonl(a.run_b)
    seeds = X.load_seeds(os.path.join(a.policy, "class_seed.jsonl"), a.seed_text)

    # 키워드 df — 기준 문서 본문 기준(고객사에도 있는 재료만 쓴다)
    df = collections.Counter()
    for s in seeds:
        df.update(set(X.tok_bigram(s["text"])))
    N = len(seeds)
    sfld = [fields(s["text"], s["file"], noise, df, N) for s in seeds]
    stoks = {"bigram": [{f: collections.Counter(v) for f, v in sf.items()} for sf in sfld]}

    docs = []
    for x in rows:
        rec = run_b.get(S.norm_path(os.path.join(S.SRC_DIR, x["relpath"])))
        tp = os.path.join(a.text, f"{rec.get('hash')}.txt")
        text = open(tp, encoding="utf-8", errors="ignore").read() if os.path.isfile(tp) else ""
        ra = run_a.get(S.norm_path(rec["file"])) or {}
        vec = X.l2(ra["vector"]) if ra.get("vector") else []
        hard = {i for i, s in enumerate(seeds) if s["hash"] and s["hash"] == rec.get("hash")}
        near = {i for i, s in enumerate(seeds) if vec and X.dot(vec, s["vec"]) >= ctx["embed"]["dup_threshold"]}
        docs.append({"no": x["no"], "existing": ((rec.get("why") or {}).get("doctype") or {}).get("values") or [],
                     "need": X.need_2nd(rec), "vec": vec, "hard": hard, "near": near,
                     "unsafe": os.path.splitext(rec["file"])[1].lower() in X.TITLE_UNSAFE_EXTS,
                     "q": {"bigram": fields(text, rec["file"], noise, df, N)}})

    rng = random.Random(a.seed)
    nos = sorted(d["no"] for d in docs)
    rng.shuffle(nos)
    half_a = set(nos[: len(nos) // 2])
    base_bm = {"k1": 1.2, "b": 0.75, "idf_min": 0.1, "idf_max": 3.0, "min_seeds": 3, "min_categories": 4,
               "min_score": 0.0, "min_margin": 0.0, "name_rule": "mark", "cap_unsafe": 0.55}
    X.set_skip(docs, seeds, ctx, "strict")
    E = {"docs": docs, "seeds": seeds, "ctx": ctx, "stoks": stoks, "rows": rows,
         "rows_a": [x for x in rows if x["no"] in half_a], "rows_b": [x for x in rows if x["no"] not in half_a],
         "half_a": half_a, "base_bm": base_bm, "grids": {"abs": None}, "res": {}, "tuned": {}}
    E["v0"] = E["res"]["V0"] = X.run_variant(docs, seeds, ctx, mode="vec")

    base = {"title": 2.0, "name2": 2.0}
    variants = [("지금(제목+파일명)", [base]),
                ("ppt·xls 제목(5자 규칙)", [{"title_x": 2.0, "name2": 2.0}]),
                ("ppt·xls 제목(+서식틀 거름)", [{"title_xm": 2.0, "name2": 2.0}])]
    if os.environ.get("ONLY_TITLE_X"):
        pass
    else:
        for extra in ("lead100", "lead2", "kww10", "kwg20"):
            variants.append((f"+{extra}", [dict(base, **{extra: w}) for w in (0.5, 1.0, 2.0)]))
        variants.append(("title에 제목+1~2문장", [{"title_lead2": 2.0, "name2": 2.0}]))
        variants.append(("+lead2+kww10", [dict(base, lead2=w1, kww10=w2) for w1 in (0.5, 1.0) for w2 in (0.5, 1.0)]))
    keys = ["V0"]
    for k, wsets in variants:
        X.run_tuned(E, k, "bigram", wsets, ("abs",))
        keys.append(k)
    out = {"table": X.print_table("필드를 늘리면? (2-gram · 자기 제외 엄격 · 앞 절반에서 무게 · 문턱 고름)", keys, E),
           "tuned": E["tuned"]}
    if a.json:
        json.dump(out, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    main()
