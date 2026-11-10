"""업무분류 — BM25 vs 로지스틱 회귀(같은 제목 · 파일명 2글자 조각) 나란히 채점.

사람이 판정한 평가 문서(사람재검-01 의 '사람' 행)를 하나씩 가리고(자기 제외), 나머지로
만든 분류기로 가린 문서의 분류를 맞히게 한다. 입력 조각은 엔진 BM25 와 똑같다
(src/csoclassify/classify/bm25.py 의 title_of · field_tokens 를 그대로 부른다).

  BM25 (평가 문서)    : 나머지 평가 문서 중 정답 있는 것으로 분류 묶음 색인 — 엔진 rank() 그대로
  BM25 (기준 문서)    : 엔진이 실제로 쓰는 class_seed.jsonl 색인(참고용, 기준 문서가 평가 문서 안에 있다)
  LR (양성만)         : 나머지 평가 문서 중 정답 있는 것만으로 학습
  LR (+분류대상아님)  : 위에 '분류대상아님 · 체계에없음' 문서를 NONE 분류로 더해 학습

규칙 · 벡터와 합치지 않은 '분류기 단독' 성적이다. 엔진 전체 성적과 직접 견주지 않는다.
"""
import argparse
import collections
import hashlib
import json
import os
import random
import sys
import time
import warnings

import openpyxl
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import score_evalsets as S  # noqa: E402

sys.path.insert(0, os.path.join(S.ROOT, "src"))
from csoclassify.classify import bm25 as EB  # noqa: E402

NONE = "__NONE__"


#------------------------------------------------------------------
# 사람 판정 평가 문서 읽기
#=> 정답지에서 '사람' 행만 골라, 원본 파일의 지문(sha256)으로 추출 본문을 찾고
#   엔진과 똑같은 방법으로 제목 · 파일명 조각을 만든다.
#    1) 판정이 비었거나 '판단불가'인 행은 채점할 수 없어 뺀다
#    2) 제목 = 본문 첫 줄(ppt · xls · xlsx 는 빈 제목) — 엔진 title_of
#    3) 조각 = 엔진 field_tokens(제목 · 정리한 파일명)
#
# -in: xlsx     = 업무분류 정답지 경로
# -in: text_dir = 추출 본문 폴더(<hash>.txt)
# -in: spec     = 엔진 Bm25Spec(불용어 · 필드)
# -in: noise    = doc_rule.yaml 의 noise_tails
#
# -out: list[dict] = {no, verdict, ans1, ans2, tag, hash, file, folder, title, toks, labels, neg}
# -out: error = 원본 파일이 없으면 예외 전파 · 본문이 없으면 빈 제목으로 둔다
#------------------------------------------------------------------
def load_docs(xlsx, text_dir, spec, noise):
    with warnings.catch_warnings():
        # 정답지의 데이터 유효성 확장은 openpyxl 이 못 읽는다는 경고 — 채점과 무관
        warnings.simplefilter("ignore")
        wb = openpyxl.load_workbook(xlsx)
    rows, _, _ = S.read_doctype_rows(wb, {}, None, S.SRC_DIR)
    docs = []
    for x in rows:
        if x["tag"] != "사람" or not x["verdict"] or x["verdict"] == "판단불가":
            continue
        file = os.path.join(S.SRC_DIR, x["relpath"])
        h = hashlib.sha256(open(file, "rb").read()).hexdigest()
        tp = os.path.join(text_dir, f"{h}.txt")
        text = open(tp, encoding="utf-8", errors="ignore").read() if os.path.isfile(tp) else ""
        title = EB.title_of(text, file)
        labels = [d for d in (x["ans1"], x["ans2"]) if d] if x["verdict"] == "정답있음" else []
        docs.append(dict(x, hash=h, file=file, folder=os.path.dirname(x["relpath"]), title=title,
                         toks=EB.field_tokens(title, file, spec, noise), labels=labels,
                         neg=x["verdict"] in S.NO_LABEL_VERDICT))
    return docs


#------------------------------------------------------------------
# 가릴 문서 번호 만들기(자기 제외)
#=> 문서 i 를 맞힐 때 학습에서 뺄 문서들. 자기 자신과 같은 지문은 늘 빼고,
#   엄격 모드면 같은 폴더 문서도 뺀다(같은 묶음의 형제 문서가 답을 알려 주는 효과를 없앤다).
#
# -in: docs = load_docs 결과
# -in: i    = 맞힐 문서 번호
# -in: mode = "hash"(같은 지문) | "folder"(같은 지문 + 같은 폴더)
#
# -out: set[int] = 뺄 문서 번호
# -out: error = 없음
#------------------------------------------------------------------
def skip_of(docs, i, mode):
    d = docs[i]
    return {j for j, e in enumerate(docs)
            if j == i or e["hash"] == d["hash"] or (mode == "folder" and e["folder"] == d["folder"])}


#------------------------------------------------------------------
# 학습에 쓸 분류 목록
#=> BM25 와 같은 조건 — 남은 학습 문서에서 min_seeds 건 이상인 분류만 참여한다.
#   두 방법이 같은 분류 집합에서 겨루게 하려는 것.
#
# -in: docs = 평가 문서
# -in: skip = 뺄 문서 번호
# -in: min_seeds = 분류 참여 최소 건수
#
# -out: set[str] = 참여 분류
# -out: error = 없음
#------------------------------------------------------------------
def classes_of(docs, skip, min_seeds):
    n = collections.Counter(d for j, e in enumerate(docs) if j not in skip for d in e["labels"])
    return {d for d, c in n.items() if c >= min_seeds}


#------------------------------------------------------------------
# 조각 → 특징 dict
#=> 필드 이름을 앞에 붙여 제목의 '보고'와 파일명의 '보고'를 다른 특징으로 둔다
#   (BM25 도 필드별로 따로 센다). 값은 나온 횟수.
#
# -in: toks = {"title": [...], "name": [...]}
#
# -out: dict = {"title|g:보고": 2, ...}
# -out: error = 없음
#------------------------------------------------------------------
def feats(toks):
    c = collections.Counter()
    for f, ts in toks.items():
        c.update(f"{f}|{t}" for t in ts)
    return dict(c)


#------------------------------------------------------------------
# 로지스틱 회귀 학습 · 예측(문서 하나)
#=> 가린 문서를 뺀 나머지로 학습해 그 문서의 1위 분류와 확률을 낸다.
#    1) 정답 있는 문서: 정답1 · 정답2 를 각각 한 줄로(두 분류 모두 맞는 답이라)
#    2) with_none 이면 '정답 없음' 문서를 NONE 분류 한 줄로 더한다
#    3) 특징 = 조각 횟수 → TF-IDF(로그 tf · 학습 문서로 idf) → 길이 1로 정규화
#    4) 점수 = NONE 을 뺀 분류 중 가장 높은 확률(NONE 이 높으면 이 값이 낮아진다)
#
# -in: docs      = 평가 문서
# -in: i         = 맞힐 문서 번호
# -in: skip      = 뺄 문서 번호
# -in: classes   = 참여 분류
# -in: with_none = True 면 NONE 분류 포함
# -in: C         = 규제 세기의 역수(클수록 학습 문서에 더 맞춘다)
#
# -out: (pred, score, p_none) = 1위 분류(없으면 None) · 그 확률 · NONE 확률(없으면 0)
# -out: error = 없음(참여 분류가 2개 미만이면 (None, 0, 0))
#------------------------------------------------------------------
def lr_predict(docs, i, skip, classes, with_none, C):
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.feature_extraction.text import TfidfTransformer
    from sklearn.linear_model import LogisticRegression

    X, y = [], []
    for j, e in enumerate(docs):
        if j in skip:
            continue
        labs = [d for d in e["labels"] if d in classes]
        if labs:
            for d in labs:
                X.append(e["feat"])
                y.append(d)
        elif with_none and e["neg"]:
            X.append(e["feat"])
            y.append(NONE)
    if len(set(y) - {NONE}) < 2:
        return None, 0.0, 0.0
    dv = DictVectorizer()
    tf = TfidfTransformer(sublinear_tf=True)
    Xt = tf.fit_transform(dv.fit_transform(X))
    m = LogisticRegression(C=C, max_iter=2000)
    m.fit(Xt, y)
    p = m.predict_proba(tf.transform(dv.transform([docs[i]["feat"]])))[0]
    prob = dict(zip(m.classes_, p))
    p_none = prob.pop(NONE, 0.0)
    # 동점이면 dc_id 오름차순 — 실행마다 같은 답이 나오게
    pred = min(prob, key=lambda d: (-prob[d], d))
    return pred, float(prob[pred]), float(p_none)


#------------------------------------------------------------------
# BM25(평가 문서 색인) 예측 — 엔진 Bm25Index.rank 그대로
#=> 정답 있는 평가 문서로 분류 묶음 색인을 한 번 만들고, 문서마다 가릴 것만 skip 으로 뺀다.
#
# -in: index  = 평가 문서로 만든 엔진 Bm25Index
# -in: rowmap = {평가 문서 번호: 색인 줄 번호}
# -in: docs   = 평가 문서
# -in: i      = 맞힐 문서 번호
# -in: skip   = 뺄 평가 문서 번호
#
# -out: (pred, score) = 1위 분류 · BM25 점수(참여 분류가 모자라면 (None, 0))
# -out: error = 없음
#------------------------------------------------------------------
def bm25_predict(index, rowmap, docs, i, skip):
    rk = index.rank(docs[i]["toks"], {rowmap[j] for j in skip if j in rowmap})
    if not rk.ranked or rk.ranked[0][1] <= 0:
        return None, 0.0
    return rk.ranked[0][0], float(rk.ranked[0][1])


#------------------------------------------------------------------
# 문턱에서의 성적
#=> 점수가 문턱 이상인 문서에만 1위 분류를 붙였다고 보고 채점기(doc_metrics)로 잰다.
#
# -in: docs  = 평가 문서
# -in: preds = {문서 번호: (pred, score)}
# -in: t     = 문턱
# -in: idx   = 채점할 문서 번호 집합(None 이면 전부)
#
# -out: dict = 채점기 결과 + f1
# -out: error = 없음
#------------------------------------------------------------------
def metrics_at(docs, preds, t, idx=None):
    sub = []
    for i, d in enumerate(docs):
        if idx is not None and i not in idx:
            continue
        pred, sc = preds[i]
        sub.append(dict(d, eng=[pred] if pred and sc >= t else []))
    m = S.doc_metrics(sub, "eng", False, {}, {})
    p, r = m["precision"] or 0, m["recall"] or 0
    m["f1"] = round(2 * p * r / (p + r), 4) if p + r else 0.0
    return m


#------------------------------------------------------------------
# 문턱 후보(점수 값들)
#
# -in: preds = {문서 번호: (pred, score)}
# -in: idx   = 볼 문서 번호(None 이면 전부)
#
# -out: list[float] = 0 과 서로 다른 점수들(오름차순)
# -out: error = 없음
#------------------------------------------------------------------
def cuts(preds, idx=None):
    return sorted({0.0} | {sc for i, (p, sc) in preds.items() if p and (idx is None or i in idx)})


#------------------------------------------------------------------
# 한 방법의 성적 요약
#=> 같은 예측을 여러 눈으로 본다.
#    1) 문턱 없음: 1위를 전부 붙였을 때
#    2) 반 나누기(정직한 값): 반 A 에서 F1 이 가장 좋은 문턱을 골라 반 B 에 쓰고, 반대로도 한 뒤
#       두 반의 건수를 합쳐 채점 — 문턱을 고른 문서로 채점하지 않는다
#    3) 최고 F1(낙관): 전체에서 고른 문턱 — 곡선의 꼭대기, 비교용
#    4) 정밀도 80 · 90% 이상에서 낼 수 있는 최대 재현율(낙관)
#
# -in: docs  = 평가 문서
# -in: preds = {문서 번호: (pred, score)}
# -in: halves = (반 A 번호 집합, 반 B 번호 집합)
#
# -out: dict = {none, honest, best, r_at_p80, r_at_p90}
# -out: error = 없음
#------------------------------------------------------------------
def summarize(docs, preds, halves):
    out = {"none": metrics_at(docs, preds, 0.0)}
    picked, honest = [], {}
    for tr, te in (halves, halves[::-1]):
        t = max(cuts(preds, tr), key=lambda c: (metrics_at(docs, preds, c, tr)["f1"], -c))
        picked.append(t)
        # 고른 문턱을 다른 반에만 쓴다 — 두 반 결과를 합쳐 하나로 채점
        for i in te:
            pred, sc = preds[i]
            honest[i] = [pred] if pred and sc >= t else []
    m = S.doc_metrics([dict(d, eng=honest[i]) for i, d in enumerate(docs)], "eng", False, {}, {})
    p, r = m["precision"] or 0, m["recall"] or 0
    m["f1"] = round(2 * p * r / (p + r), 4) if p + r else 0.0
    m["thresholds"] = [round(t, 4) for t in picked]
    out["honest"] = m
    allm = [(c, metrics_at(docs, preds, c)) for c in cuts(preds)]
    out["best"] = max((m for _, m in allm), key=lambda m: m["f1"])
    for lim in (0.8, 0.9):
        ok = [m["recall"] or 0 for _, m in allm if (m["precision"] or 0) >= lim]
        out[f"r_at_p{int(lim * 100)}"] = max(ok) if ok else 0.0
    return out


#------------------------------------------------------------------
# 결과 표 출력
#
# -in: title = 표 제목
# -in: res   = {방법 이름: summarize 결과}
#
# -out: 없음(화면 출력)
# -out: error = 없음
#------------------------------------------------------------------
def print_table(title, res):
    pr = lambda m: f"{(m['precision'] or 0) * 100:5.1f} {(m['recall'] or 0) * 100:5.1f} {m['f1'] * 100:5.1f}"
    print(f"\n── {title} " + "─" * 30)
    print(f"   {'방법':<24} {'반나누기 P·R·F1':>17} 헛라벨 오라벨 | {'문턱없음 P·R·F1':>17} 헛라벨 | "
          f"{'최고F1(낙관)':>17} | R@P80 R@P90")
    for k, v in res.items():
        h, n, b = v["honest"], v["none"], v["best"]
        print(f"   {k:<24} {pr(h)} {h['헛라벨']:6.0f} {h['오라벨']:6.0f} | {pr(n)} {n['헛라벨']:6.0f} | "
              f"{pr(b)} | {v['r_at_p80'] * 100:5.1f} {v['r_at_p90'] * 100:5.1f}")


#------------------------------------------------------------------
# 실험 실행
#=> 1) 정답지 · 규칙 파일 · 기준 문서를 읽는다
#   2) 자기 제외 두 가지(같은 지문 / 같은 폴더까지)마다 네 방법을 문서별로 예측한다
#   3) 표를 찍고, --json 이 있으면 문서별 예측까지 저장한다
#
# -in: argv = 명령줄 인자(None 이면 sys.argv)
#
# -out: 없음(화면 출력 · json 파일)
# -out: error = 파일이 없으면 예외 전파
#------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", default=os.path.join(S.ROOT, "resources", "golden_dataset",
                                                   "업무분류-평가셋-500-20260918-사람재검-01.xlsx"))
    ap.add_argument("--policy", default=os.path.join(S.ROOT, "ui", "policy"))
    ap.add_argument("--text", default=os.path.join(S.ROOT, "ui", "_extracted"), help="추출 본문 폴더(<hash>.txt)")
    ap.add_argument("--C", default="1,10,100", help="로지스틱 회귀 C 값들(쉼표)")
    ap.add_argument("--seed", type=int, default=20261027, help="반 나누기 시드")
    ap.add_argument("--json", help="결과 json 경로")
    a = ap.parse_args(argv)

    rule = yaml.safe_load(open(os.path.join(a.policy, "doc_rule.yaml"), encoding="utf-8"))
    spec, probs = EB.parse_spec(rule.get("bm25"))
    noise = rule.get("noise_tails") or []
    docs = load_docs(a.xlsx, a.text, spec, noise)
    for d in docs:
        d["feat"] = feats(d["toks"])
    seeds = [json.loads(ln) for ln in open(os.path.join(a.policy, "class_seed.jsonl"), encoding="utf-8")
             if ln.strip()]
    # 색인에 들어가는 줄(업무분류 라벨 있음)만 남겨 색인 줄 번호와 seeds 번호를 맞춘다
    seeds = [e for e in seeds
             if isinstance(e.get("doctype") or (e.get("labels") or {}).get("doctype"), list)
             and (e.get("doctype") or (e.get("labels") or {}).get("doctype"))]
    cnt = collections.Counter("정답있음" if d["labels"] else d["verdict"] for d in docs)
    print(f"사람 판정 문서 {len(docs)}건 {dict(cnt)} · 제목 있음 {sum(1 for d in docs if d['title'])} · "
          f"기준 문서 {len(seeds)} · BM25 필드 {spec.weights()}")
    if probs:
        print("   [규칙 파일 bm25 블록 문제]", probs)

    # 평가 문서 BM25 색인 — 정답 있는 문서만 들어간다(엔진 색인 조건과 같다)
    pos = [i for i, d in enumerate(docs) if d["labels"]]
    ev_index = EB.Bm25Index([{"doctype": docs[i]["labels"], "title": docs[i]["title"], "file": docs[i]["file"],
                              "hash": docs[i]["hash"]} for i in pos], spec, noise)
    rowmap = {i: k for k, i in enumerate(pos)}
    # 엔진 실제 색인(기준 문서) + 엔진이 실행마다 계산하는 자동 문턱
    seed_index = EB.Bm25Index(seeds, spec, noise)
    auto = EB.auto_threshold(seed_index, (rule.get("embed") or {}).get("dup_threshold", 0.95))
    print(f"   엔진 기준 문서 색인 {seed_index.size()}건 · 자동 문턱 {auto}")

    rng = random.Random(a.seed)
    order = list(range(len(docs)))
    rng.shuffle(order)
    halves = (set(order[: len(order) // 2]), set(order[len(order) // 2:]))
    Cs = [float(c) for c in a.C.split(",")]
    results = {"n": len(docs), "auto_threshold": auto, "modes": {}}

    for mode, label in (("hash", "자기 제외 = 같은 지문"), ("folder", "자기 제외 = 같은 지문 + 같은 폴더(엄격)")):
        preds = collections.defaultdict(dict)
        t0 = time.perf_counter()
        for i, d in enumerate(docs):
            skip = skip_of(docs, i, mode)
            cls = classes_of(docs, skip, spec.min_seeds)
            preds["BM25 (평가 문서 색인)"][i] = bm25_predict(ev_index, rowmap, docs, i, skip)
            # 엔진 색인은 이 문서와 같은 지문의 기준 문서만 가린다(엔진 자동 문턱과 같은 조건)
            sk = {k for k, h in enumerate(seed_index.hashes) if h == d["hash"]}
            if mode == "folder":
                here = S.norm_path(os.path.dirname(d["file"]))
                sk |= {k for k, e in enumerate(seeds) if S.norm_path(os.path.dirname(e.get("file") or "")) == here}
            rk = seed_index.rank(d["toks"], sk)
            preds["BM25 (기준 문서 색인)"][i] = (rk.ranked[0][0], float(rk.ranked[0][1])) \
                if rk.ranked and rk.ranked[0][1] > 0 else (None, 0.0)
            for C in Cs:
                p, sc, _ = lr_predict(docs, i, skip, cls, False, C)
                preds[f"LR 양성만 C={C:g}"][i] = (p, sc)
                p, sc, _ = lr_predict(docs, i, skip, cls, True, C)
                preds[f"LR +분류대상아님 C={C:g}"][i] = (p, sc)
        sec = time.perf_counter() - t0
        res = {k: summarize(docs, v, halves) for k, v in preds.items()}
        # 엔진 색인은 엔진이 실제로 쓰는 자동 문턱에서도 잰다
        if auto.get("threshold") is not None:
            res["BM25 (기준 문서 색인)"]["engine_auto"] = metrics_at(docs, preds["BM25 (기준 문서 색인)"],
                                                                 auto["threshold"])
        print_table(f"{label} · {len(docs)}건 · {sec:.0f}초", res)
        ea = res["BM25 (기준 문서 색인)"].get("engine_auto")
        if ea:
            print(f"     · BM25(기준 문서) 엔진 자동 문턱 {auto['threshold']:.2f}: P {(ea['precision'] or 0) * 100:.1f} · "
                  f"R {(ea['recall'] or 0) * 100:.1f} · 붙임 {ea['적중'] + ea['오라벨'] + ea['헛라벨']:.0f}"
                  f"(헛라벨 {ea['헛라벨']:.0f})")
        for k, v in res.items():
            print(f"     · {k}: 반나누기 문턱 {v['honest']['thresholds']}")
        results["modes"][mode] = {"summary": res,
                                  "preds": {k: {docs[i]["no"]: v[i] for i in v} for k, v in preds.items()}}

    if a.json:
        with open(a.json, "w", encoding="utf-8") as fp:
            json.dump(results, fp, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
