"""업무분류 2차 BM25 실험 — 설계서 plan/업무분류-2차-BM25-설계-20261022.html §10.

엔진 코드를 고치기 전에, 지금 엔진의 1차 결과(+벡터 · 추출 본문)와 기준 문서로
2차만 파이썬에서 다시 계산해 V0(벡터만) · V1(+BM25 글자 2-gram) · V2(+BM25 Kiwi)를
자기 제외(leave-one-out)로 채점한다.
"""
import argparse
import collections
import json
import math
import os
import random
import re
import sys
import time
import unicodedata

import openpyxl
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import score_evalsets as S  # noqa: E402

TITLE_UNSAFE_EXTS = {".ppt", ".xls", ".xlsx"}
KIWI_TAGS = {"NNG", "NNP", "SL", "SH", "XR"}
STOPWORDS = {"작성자", "작성일", "페이지", "문서번호", "목차", "page", "of"}
HANGUL = re.compile(r"[가-힣]")


#------------------------------------------------------------------
# jsonl 결과 읽기(경로 → 레코드)
#=> 엔진이 낸 jsonl 에서 문서 줄만 골라, 채점기와 같은 방식으로 정규화한 경로를 열쇠로 둔다.
#
# -in: path = 엔진 결과 jsonl
#
# -out: dict = {정규화 경로: 레코드}
# -out: error = 파일이 없으면 예외 전파
#------------------------------------------------------------------
def load_jsonl(path):
    out = {}
    with open(path, encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if "file" in rec:
                out[S.norm_path(rec["file"])] = rec
    return out


#------------------------------------------------------------------
# 분류 체계 읽기(조상 경로)
#=> 조상 흡수(absorb_ancestors)에 쓸 dc_id → 경로 id 목록을 체계 JSON 에서 만든다.
#
# -in: export_path = doc_classification_export.json
#
# -out: dict = {dc_id: [루트 … 자기]}
# -out: error = 파일이 없거나 깨지면 예외 전파
#------------------------------------------------------------------
def load_taxonomy(export_path):
    nodes = json.load(open(export_path, encoding="utf-8"))["nodes"]
    return {n["dc_id"]: list(n.get("path_ids") or [n["dc_id"]]) for n in nodes}


#------------------------------------------------------------------
# 기준 문서 읽기
#=> class_seed.jsonl 에서 업무분류 라벨과 벡터가 있는 줄만 모은다(엔진 DoctypeSeedIndex 와 같은 조건).
#    1) 벡터는 L2 정규화해 둔다(코사인 계산을 내적으로)
#    2) BM25 재료가 될 본문은 text_dir/<hash>.txt 에서 읽는다(없으면 빈 문자열)
#
# -in: seed_path = class_seed.jsonl
# -in: text_dir  = 기준 문서 추출 본문 폴더
#
# -out: list[dict] = {file, hash, labels, vec, text}
# -out: error = 파일이 없으면 예외 전파
#------------------------------------------------------------------
def load_seeds(seed_path, text_dir):
    seeds = []
    for line in open(seed_path, encoding="utf-8"):
        if not line.strip():
            continue
        e = json.loads(line)
        labels = e.get("doctype") or (e.get("labels") or {}).get("doctype") or []
        vec = e.get("vector") or []
        if not labels or not vec:
            continue
        tp = os.path.join(text_dir, f"{e.get('hash')}.txt")
        text = open(tp, encoding="utf-8", errors="ignore").read() if os.path.isfile(tp) else ""
        seeds.append({"file": e.get("file", ""), "hash": e.get("hash"), "labels": list(labels),
                      "vec": l2(vec), "text": text})
    return seeds


#------------------------------------------------------------------
# L2 정규화
#
# -in: v = 숫자 목록
#
# -out: list[float] = 길이 1로 맞춘 벡터(0 벡터면 그대로)
# -out: error = 없음
#------------------------------------------------------------------
def l2(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


#------------------------------------------------------------------
# 코사인(둘 다 정규화된 벡터의 내적)
#
# -in: a, b = 정규화된 벡터
#
# -out: float = 코사인 유사도
# -out: error = 없음
#------------------------------------------------------------------
def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


#------------------------------------------------------------------
# 벡터 2차(엔진 propagate_doctype 그대로)
#=> 기준 문서 k 개 이웃으로 업무분류 후보를 낸다.
#    1) 1등이 dup_threshold 이상이면 그 기준 문서 라벨 전체를 상속(dup_inherit)
#    2) 1등이 min_sim 미만이면 후보 없음(too_far)
#    3) 아니면 라벨별 유사도 합 / 전체 합 이 min_share 이상인 라벨을 모두 채택(knn_vote)
#
# -in: vec   = 문서 벡터(정규화)
# -in: seeds = 쓸 기준 문서 목록(자기 제외 적용 후)
# -in: p     = doc_rule.yaml 의 embed 블록 dict
#
# -out: list[(dc_id, share)] = 벡터 후보
# -out: error = 없음
#------------------------------------------------------------------
def propagate_doctype(vec, seeds, p):
    if not seeds or not vec:
        return []
    sims = sorted(((dot(vec, s["vec"]), i) for i, s in enumerate(seeds)), key=lambda t: -t[0])[:p["k"]]
    top1 = sims[0][0]
    if top1 >= p["dup_threshold"]:
        return [(d, top1) for d in seeds[sims[0][1]]["labels"]]
    if top1 < p["min_sim"]:
        return []
    total = max(sum(max(s, 0.0) for s, _ in sims), 1e-9)
    score = collections.defaultdict(float)
    for s, i in sims:
        for d in seeds[i]["labels"]:
            score[d] += max(s, 0.0)
    return [(d, v / total) for d, v in score.items() if v / total >= p["min_share"]]


#------------------------------------------------------------------
# 후보 합치기 + 마무리(엔진 merge_embed_candidates · finalize 그대로, BM25 칸 추가)
#=> 1차 후보에 벡터 · BM25 후보를 더하고, 조상을 흡수한 뒤 top_n 으로 자른다.
#    1) 벡터: 이미 있으면 신뢰도를 더 큰 쪽으로, 없으면 embed_cap 으로 묶어 추가
#    2) BM25 1위: 벡터 후보와 같은 분류면 cap_agree 까지 올림, 아니면 cap_alone 으로 추가
#    3) 조상 흡수 → 신뢰도 내림차순 · dc_id 오름차순 → 상위 n
#
# -in: existing = 1차 후보 [{dc_id, confidence}]
# -in: embed    = 벡터 후보 [(dc_id, share)]
# -in: bm25_top = BM25 1위 dc_id(없으면 None)
# -in: tax      = {dc_id: 경로 id 목록}
# -in: cfg      = 상한 · top_n 설정 dict
# -in: alone_cap = BM25 단독 후보에 cap_alone 대신 쓸 상한(None 이면 cap_alone)
#
# -out: list[str] = 최종 dc_id 목록(엔진 레코드의 doctype 칸과 같은 뜻)
# -out: error = 없음
#------------------------------------------------------------------
def merge_finalize(existing, embed, bm25_top, tax, cfg, alone_cap=None):
    m = {v["dc_id"]: v["confidence"] for v in existing}
    rule_ids = set(m)
    for d, c in embed:
        if d not in tax:
            continue
        m[d] = max(m[d], c) if d in m else min(c, cfg["embed_cap"])
    if bm25_top and bm25_top in tax:
        if bm25_top in rule_ids:
            pass  # 1차가 이미 붙였다 — 근거만 늘 뿐 신뢰도는 그대로
        elif any(d == bm25_top for d, _ in embed):
            # 벡터와 합의 — 벡터 단독 상한보다 높게, cap_agree 까지
            m[bm25_top] = max(m[bm25_top], cfg["cap_agree"])
        else:
            cap = cfg["cap_alone"] if alone_cap is None else alone_cap
            m[bm25_top] = max(m.get(bm25_top, 0.0), cap)
    ids = set(m)
    survivors = set(ids)
    for d in ids:
        for anc in tax.get(d, [d])[:-1]:
            survivors.discard(anc)
    kept = sorted(survivors, key=lambda d: (-m[d], d))
    return kept[:cfg["top_n"]]


#------------------------------------------------------------------
# 필드 뽑기(1차 규칙과 같은 정의 + 파일명 정리 강화판)
#=> 제목 = 첫 비어 있지 않은 줄(ppt · xls · xlsx 는 비움), 앞부분 = 앞 head_chars 자,
#   파일명 = 확장자 뺀 이름에서 noise_tails 를 떼어 낸 것.
#   name2 = 파일명에서 잡음을 더 세게 뺀 것(clean_name).
#
# -in: text       = 추출 본문
# -in: file       = 파일 경로
# -in: head_chars = 앞부분 글자 수(기본 400)
# -in: noise      = noise_tails 목록
#
# -out: dict = {title, head, name, name2}
# -out: error = 없음
#------------------------------------------------------------------
def fields_of(text, file, head_chars, noise):
    base, ext = os.path.splitext(os.path.basename(file))
    title = ""
    if ext.lower() not in TITLE_UNSAFE_EXTS:
        title = next((ln for ln in text.splitlines() if ln.strip()), "")[:200]
    name = base
    for t in noise:
        # 파일명 끝 잡음 말(최종본 · draft …)은 문서 성격과 무관해 뗀다
        name = re.sub(re.escape(t), " ", name, flags=re.I)
    return {"title": title, "head": text[:head_chars], "name": name, "name2": clean_name(name)}


#------------------------------------------------------------------
# 파일명 정리 강화
#=> 파일명에 흔한, 문서 종류와 상관없는 잡음을 뺀다.
#    1) 괄호 안 내용 — (실력산업) · (1) · [최종] 처럼 고객사 이름 · 판 번호가 많다
#    2) 숫자가 섞인 말 — 05. · v10 · 2023q1 · 20071010 같은 번호 · 버전 · 날짜
#
# -in: name = noise_tails 를 뗀 파일명
#
# -out: str = 정리된 파일명
# -out: error = 없음
#------------------------------------------------------------------
def clean_name(name):
    s = re.sub(r"[\(\[\{（【][^\)\]\}）】]*[\)\]\}）】]", " ", name)
    # 숫자가 한 글자라도 섞인 말은 통째로 뺀다(번호 · 버전 · 날짜)
    s = re.sub(r"[0-9A-Za-z가-힣]*\d[0-9A-Za-z가-힣]*", " ", s)
    return s


#------------------------------------------------------------------
# 공통 전처리
#=> NFC 정규화 + 영문 소문자. 두 엔진이 같은 글자열을 보게 하는 출발점.
#
# -in: s = 문자열
#
# -out: str = 정규화된 문자열
# -out: error = 없음
#------------------------------------------------------------------
def norm(s):
    return unicodedata.normalize("NFC", s or "").lower()


#------------------------------------------------------------------
# 자간 벌린 제목 붙이기
#=> "출 장 여 비 규 정"처럼 한 글자씩 띄운 구간만 붙인다(1차 despace 와 같은 취지 —
#   짧은 구간에만 쓴다).
#
# -in: s = 제목 · 파일명
#
# -out: str = 한 글자 띄어쓰기를 붙인 문자열
# -out: error = 없음
#------------------------------------------------------------------
def despace_spaced(s):
    return re.sub(r"(?:(?<=^)|(?<=\s))((?:[가-힣]\s){2,}[가-힣])(?=\s|$)",
                  lambda m: m.group(1).replace(" ", ""), s)


#------------------------------------------------------------------
# 글자 2-gram 토큰(+ 단어)
#=> 단어(한글 · 영숫자 덩어리) 안에서만 글자 두 개씩 겹쳐 자르고 단어 통째도 넣는다.
#    1) 한글이 든 단어: 2-gram 전부 + 단어 자체
#    2) 영문 · 숫자 단어: 단어 자체만(영문을 글자로 자르면 잡음이 크다)
#    3) 숫자만 · 1글자 · 불용어는 버린다
#
# -in: s = 필드 문자열
#
# -out: list[str] = 토큰
# -out: error = 없음
#------------------------------------------------------------------
def tok_bigram(s):
    out = []
    for w in re.findall(r"[0-9a-z가-힣]+", despace_spaced(norm(s))):
        if w.isdigit() or len(w) < 2 or w in STOPWORDS:
            continue
        out.append("w:" + w)
        if HANGUL.search(w):
            out.extend("g:" + w[i:i + 2] for i in range(len(w) - 1))
    return out


_KIWI = None


#------------------------------------------------------------------
# Kiwi 형태소 토큰
#=> 명사 · 고유명사 · 외국어 · 한자 · 어근만 남긴다. Kiwi 는 한 번만 띄운다.
#
# -in: s = 필드 문자열
#
# -out: list[str] = 토큰
# -out: error = kiwipiepy 가 없으면 ImportError 전파
#------------------------------------------------------------------
def tok_kiwi(s):
    global _KIWI
    if _KIWI is None:
        from kiwipiepy import Kiwi
        _KIWI = Kiwi()
    s = despace_spaced(norm(s))
    if not s.strip():
        return []
    out = []
    for t in _KIWI.tokenize(s):
        f = t.form.lower()
        if t.tag in KIWI_TAGS and len(f) >= 2 and not f.isdigit() and f not in STOPWORDS:
            out.append(f)
    return out


#------------------------------------------------------------------
# 분류 단위 BM25 채점
#=> 기준 문서를 분류별로 묶은 '분류 문서'에 질의(현재 문서 세 필드)를 견준다.
#    1) 자기 제외 후 기준 문서가 min_seeds 이상인 분류만 참여
#    2) IDF = ln(1 + (N − n + 0.5)/(n + 0.5)) 를 [idf_min, idf_max] 로 자름(n = 말이 나온 분류 수)
#    3) 필드별 BM25 를 가중 합
#
# -in: q     = {필드: 토큰 목록}(현재 문서)
# -in: stoks = 기준 문서별 {필드: Counter}(seeds 와 같은 순서)
# -in: seeds = 기준 문서 목록
# -in: skip  = 자기 제외로 뺄 기준 문서 번호 집합
# -in: cfg   = BM25 설정 dict
#
# -out: list[(dc_id, score)] = 점수 내림차순(참여 분류가 모자라면 빈 목록)
# -out: error = 없음
#------------------------------------------------------------------
def bm25_category(q, stoks, seeds, skip, cfg):
    cat = collections.defaultdict(lambda: {f: collections.Counter() for f in cfg["fields"]})
    nseed = collections.Counter()
    for i, s in enumerate(seeds):
        if i in skip:
            continue
        for d in s["labels"]:
            nseed[d] += 1
            for f in cfg["fields"]:
                cat[d][f].update(stoks[i][f])
    cats = [d for d in cat if nseed[d] >= cfg["min_seeds"]]
    if len(cats) < cfg["min_categories"]:
        return Ranked()
    return score_docs(q, {d: cat[d] for d in cats}, cfg)


#------------------------------------------------------------------
# 문서 단위 BM25 채점(참고 D1)
#=> 기준 문서 하나하나를 문서로 보고, 분류마다 가장 높은 점수를 그 분류 점수로 쓴다.
#
# -in: q / stoks / seeds / skip / cfg = bm25_category 와 같다
#
# -out: list[(dc_id, score)] = 점수 내림차순
# -out: error = 없음
#------------------------------------------------------------------
def bm25_document(q, stoks, seeds, skip, cfg):
    docs = {i: stoks[i] for i in range(len(seeds)) if i not in skip}
    if len(docs) < cfg["min_categories"]:
        return Ranked()
    scored = score_docs(q, docs, cfg)
    best = {}
    for i, sc in scored:
        for d in seeds[i]["labels"]:
            best[d] = max(best.get(d, 0.0), sc)
    out = Ranked(sorted(best.items(), key=lambda t: -t[1]))
    out.ub, out.self = scored.ub, scored.self
    return out


#------------------------------------------------------------------
# BM25 본체(필드 가중 합)
#
# -in: q    = {필드: 토큰 목록}
# -in: docs = {문서 열쇠: {필드: Counter}}
# -in: cfg  = k1 · b · idf_min · idf_max · fields(가중)
#
# -out: list[(열쇠, score)] = 점수 내림차순
# -out: error = 없음
#------------------------------------------------------------------
def score_docs(q, docs, cfg):
    N = len(docs)
    df = collections.Counter()
    for d in docs.values():
        seen = set()
        for f in cfg["fields"]:
            seen.update(d[f])
        df.update(seen)
    idf = lambda t: min(max(math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5)),
                            cfg["idf_min"]), cfg["idf_max"])
    avg = {f: (sum(sum(d[f].values()) for d in docs.values()) / N) or 1.0 for f in cfg["fields"]}
    k1, b = cfg["k1"], cfg["b"]
    # 한 말의 BM25 기여(tf · 문서 길이 dl · 필드 평균 길이 a)
    part = lambda t, tf, dl, a: idf(t) * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / a))
    out = Ranked()
    for key, d in docs.items():
        total = 0.0
        by_f = {}
        for f, w in cfg["fields"].items():
            dl = sum(d[f].values())
            sf = 0.0
            for t in set(q.get(f) or ()):
                tf = d[f].get(t, 0)
                if tf:
                    # 같은 말 반복은 k1 로 포화시키고, 긴 분류 문서는 b 로 깎는다
                    sf += w * part(t, tf, dl, avg[f])
            by_f[f] = sf
            total += sf
        out.append((key, total))
        # 필드별 점수 — '파일명에서만 맞았나'를 가리는 데 쓴다
        out.parts[key] = by_f
    out.sort(key=lambda t: (-t[1], str(t[0])))
    # ── 상대 기준용 분모 두 가지(같은 IDF · 평균 길이로) ──
    #   ub   : 질의의 말이 모두 '아주 많이' 나왔을 때의 상한 — Σ w · idf · (k1+1)
    #   self : 질의를 그 자체로 한 문서로 보고 채점한 점수(자기 점수)
    for f, w in cfg["fields"].items():
        qc = collections.Counter(q.get(f) or ())
        dl = sum(qc.values())
        for t, tf in qc.items():
            out.ub += w * idf(t) * (k1 + 1)
            out.self += w * part(t, tf, dl, avg[f])
    return out


#------------------------------------------------------------------
# BM25 순위 + 상대 기준 분모
#=> 점수 순위(list)에 그 질의의 상한(ub) · 자기 점수(self)를 함께 실어 나른다.
#   순위를 다시 계산하지 않고 문턱 방식만 바꿔 볼 수 있게 하려는 것이다.
#
# -필드: ub    = 질의 말이 모두 포화될 때의 점수 상한
# -필드: self  = 질의를 그 자체로 채점한 점수
# -필드: parts = {열쇠: {필드: 점수}} 필드별 점수
#------------------------------------------------------------------
class Ranked(list):
    #------------------------------------------------------------------
    # 빈 순위 만들기
    #
    # -in: items = 초기 (열쇠, 점수) 목록(기본 없음)
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def __init__(self, items=()):
        super().__init__(items)
        self.ub = 0.0
        self.self = 0.0
        self.parts = {}


#------------------------------------------------------------------
# 문턱과 비교할 1위 점수
#=> min_score 를 무엇과 견줄지 정한다.
#    · abs  : 1위 점수 그대로(기준 문서 수 · 말 수에 따라 크기가 변한다)
#    · ub   : 1위 점수 / 질의 상한 — "질의 말 근거 중 얼마나 맞았나"(0~1)
#    · self : 1위 점수 / 자기 점수 — "자기와 똑같은 문서 대비 몇 %인가"
#
# -in: ranked = Ranked 순위
# -in: mode   = "abs" | "ub" | "self"
#
# -out: float = 견줄 값(분모가 0이면 0)
# -out: error = 없음
#------------------------------------------------------------------
def top_value(ranked, mode):
    s1 = ranked[0][1]
    if mode == "abs":
        return s1
    den = getattr(ranked, mode, 0.0)
    return s1 / den if den > 0 else 0.0


#------------------------------------------------------------------
# BM25 결과에서 후보 하나 고르기
#=> 1위 값(top_value)이 min_score 이상이고 2위와의 상대 차가 min_margin 이상일 때만
#   1위를 낸다(애매하면 침묵).
#
# -in: ranked = Ranked 순위
# -in: cfg    = min_score · min_margin · norm("abs" 기본)
#
# -out: str | None = 고른 dc_id
# -out: error = 없음
#------------------------------------------------------------------
def pick(ranked, cfg):
    if not ranked or ranked[0][1] <= 0:
        return None
    s1 = ranked[0][1]
    s2 = ranked[1][1] if len(ranked) > 1 else 0.0
    if top_value(ranked, cfg.get("norm", "abs")) < cfg["min_score"] or (s1 - s2) / s1 < cfg["min_margin"]:
        return None
    return ranked[0][0]


#------------------------------------------------------------------
# BM25 1위 자체의 정확도
#=> 합치기 전에, BM25 가 고른 분류가 정답과 맞는지만 따로 센다(정답 있는 문서 · 없는 문서 따로).
#
# -in: rows  = 평가셋 행
# -in: docs  = 평가 문서 묶음
# -in: ranks = {no: 순위}
# -in: bm    = 문턱 설정
#
# -out: dict = {picked, right, wrong, on_no_label}
# -out: error = 없음
#------------------------------------------------------------------
def pick_quality(rows, docs, ranks, bm):
    by_no = {x["no"]: x for x in rows}
    c = collections.Counter()
    for d in docs:
        if not d["need"]:
            continue
        top, _ = choose(d, ranks, bm)
        if not top:
            continue
        x = by_no[d["no"]]
        c["picked"] += 1
        if x["verdict"] in S.NO_LABEL_VERDICT:
            c["on_no_label"] += 1
        elif top in (x["ans1"], x["ans2"]):
            c["right"] += 1
        else:
            c["wrong"] += 1
    return dict(c)


#------------------------------------------------------------------
# 2차 대상 판정(엔진 need_vec 의 --embed-needed 경우)
#
# -in: rec = 1차(--rule-only) 레코드
#
# -out: bool = 보안 미정 · seed 후보 · 업무분류 없음 중 하나면 True
# -out: error = 없음
#------------------------------------------------------------------
def need_2nd(rec):
    sec = ((rec.get("why") or {}).get("security") or {})
    dt = ((rec.get("why") or {}).get("doctype") or {}).get("values")
    return rec.get("grade") is None or bool(sec.get("seed_eligible")) or (dt is not None and not dt)


#------------------------------------------------------------------
# 채점(사람 · 전체)
#=> 평가셋 행에 이 변형의 라벨을 붙여 채점기 함수로 정밀도 · 재현율 · F1 을 낸다.
#
# -in: rows   = 평가셋 행(read_doctype_rows 결과)
# -in: labels = {no: [dc_id]}
# -in: tag    = None 이면 전체, "사람" 이면 사람 판정 문서만
#
# -out: dict = 건수 · precision · recall · f1
# -out: error = 없음
#------------------------------------------------------------------
def metrics(rows, labels, tag=None):
    sub = []
    for x in rows:
        if tag and x["tag"] != tag:
            continue
        y = dict(x)
        y["eng"] = labels.get(x["no"], [])
        sub.append(y)
    m = S.doc_metrics(sub, "eng", False, {}, {})
    p, r = m["precision"] or 0, m["recall"] or 0
    m["f1"] = round(2 * p * r / (p + r), 4) if p + r else 0.0
    return m


#------------------------------------------------------------------
# 한 변형 돌리기
#=> 평가 문서마다 자기 제외 → 벡터 2차 → (있으면) BM25 1위 → 합치기를 해서 라벨을 낸다.
#   BM25 순위는 미리 계산해 받는다(문턱만 바꿔 여러 번 돌리므로).
#
# -in: docs  = 평가 문서 묶음(no, existing, need, vec, skip)
# -in: seeds = 기준 문서
# -in: ctx   = tax · embed 설정 · 합치기 설정
# -in: ranks = {no: [(dc_id, score)]} BM25 순위(None 이면 BM25 안 씀)
# -in: bm    = 문턱 설정(min_score · min_margin)
# -in: mode  = "vec+bm25" | "bm25" | "vec"
#
# -out: dict = {no: [dc_id]}
# -out: error = 없음
#------------------------------------------------------------------
def run_variant(docs, seeds, ctx, ranks=None, bm=None, mode="vec"):
    out = {}
    for d in docs:
        existing = d["existing"]
        if not d["need"]:
            out[d["no"]] = [v["dc_id"] for v in existing]
            continue
        # 벡터 2차는 자기 제외가 같으면 결과도 같다 — set_skip 이 미리 계산해 둔 것을 쓴다
        embed = d["embed"] if mode != "bm25" else []
        top, cap = choose(d, ranks, bm, embed) if ranks is not None and mode != "vec" else (None, None)
        out[d["no"]] = merge_finalize(existing, embed, top, ctx["tax"], ctx["merge"], cap)
    return out


#------------------------------------------------------------------
# 자기 제외 정하기 + 벡터 2차 미리 계산
#=> 문서마다 뺄 기준 문서(skip)를 정하고, 그 조건의 벡터 2차 결과를 d["embed"] 에 둔다.
#   문턱만 바꿔 수백 번 돌릴 때 벡터 계산을 되풀이하지 않으려는 것이다.
#
# -in: docs  = 평가 문서 묶음
# -in: seeds = 기준 문서
# -in: ctx   = embed 설정
# -in: mode  = "none"(빼지 않음 — 재현 검증용) | "hash"(같은 지문만) | "strict"(+ 거의 같은 기준 문서)
# -in: drop  = 추가로 뺄 기준 문서 번호(강건성 시험용)
#
# -out: 없음(docs 를 고침)
# -out: error = 없음
#------------------------------------------------------------------
def set_skip(docs, seeds, ctx, mode="strict", drop=frozenset()):
    for d in docs:
        base = set() if mode == "none" else d["hard"] | (d["near"] if mode == "strict" else set())
        d["skip"] = base | set(drop)
        use = [s for i, s in enumerate(seeds) if i not in d["skip"]]
        d["embed"] = propagate_doctype(d["vec"], use, ctx["embed"]) if d["need"] else []


#------------------------------------------------------------------
# BM25 후보 고르기(+ 파일명 단독 규칙)
#=> pick 으로 1위를 고른 뒤, 그 점수가 파일명에서만 나왔으면 규칙에 따라 거른다.
#    · name_rule 없음          : 그대로
#    · "block"                 : 파일명만으로 맞은 1위는 벡터가 같은 분류를 낼 때만 인정
#    · "block_unsafe_ok"       : 위와 같되, 제목을 쓰지 않는 형식(ppt · xls · xlsx)은
#                                파일명만으로도 인정하고 확신을 낮춘다(cap_unsafe)
#    · "mark"                  : 막지 않고 확신만 cap_unsafe(0.55)로 낮춘다(§14-4 나안 · 채택)
#
# -in: d     = 평가 문서
# -in: ranks = {no: Ranked}
# -in: bm    = 문턱 · 규칙 설정
# -in: embed = 이 문서의 벡터 후보(없으면 빈 목록)
#
# -out: (dc_id | None, 확신 상한 | None) = 고른 분류와, 기본 상한 대신 쓸 값
# -out: error = 없음
#------------------------------------------------------------------
def choose(d, ranks, bm, embed=()):
    ranked = ranks.get(d["no"])
    top = pick(ranked, bm)
    rule = bm.get("name_rule")
    if not top or not rule:
        return top, None
    parts = ranked.parts.get(top, {})
    # 제목 · 앞부분에서 얻은 점수가 하나도 없으면 '파일명 단독'
    if sum(v for f, v in parts.items() if not f.startswith("name")) > 0:
        return top, None
    if any(dc == top for dc, _ in embed):
        return top, None
    if rule == "mark" or (rule == "block_unsafe_ok" and d["unsafe"]):
        return top, bm.get("cap_unsafe", 0.55)
    return None, None


#------------------------------------------------------------------
# BM25 순위 미리 계산
#=> 2차 대상 문서마다 분류(또는 기준 문서) 순위를 한 번만 계산해 둔다.
#
# -in: docs  = 평가 문서 묶음(q · skip 포함)
# -in: stoks = 기준 문서 토큰(이 토크나이저)
# -in: seeds = 기준 문서
# -in: bm    = BM25 설정(fields 가중 · tok 이름 포함)
# -in: level = "category" | "document"
#
# -out: dict = {no: [(dc_id, score)]}
# -out: error = 없음
#------------------------------------------------------------------
def rank_all(docs, stoks, seeds, bm, level="category"):
    fn = bm25_category if level == "category" else bm25_document
    return {d["no"]: fn(d["q"][bm["tok"]], stoks, seeds, d["skip"], bm) for d in docs if d["need"]}


#------------------------------------------------------------------
# 문턱 고르기(앞 절반)
#=> 가중별 순위 묶음과 문턱 후보를 모두 돌려, V0 보다 정밀도가 1%p 넘게 떨어지지 않는 것 중
#   F1 이 가장 높은 설정을 고른다(조건을 만족하는 것이 없으면 F1 최대).
#    · abs : 문턱 후보 = 앞 절반 1위 점수의 0 · 25 · 50 · 75 · 90% 분위수
#    · ub · self : 문턱 후보 = 주어진 비율 목록
#
# -in: rows_a     = 앞 절반 평가셋 행
# -in: docs       = 평가 문서 묶음
# -in: seeds      = 기준 문서
# -in: ctx        = 합치기 설정
# -in: ranks_by_w = [(가중, {no: Ranked})]
# -in: base_bm    = BM25 기본 설정
# -in: tok        = 토크나이저 이름
# -in: norm       = "abs" | "ub" | "self"
# -in: grid       = 비율 문턱 후보(abs 면 None)
# -in: half_a     = 앞 절반 문서 번호 집합
# -in: v0         = 같은 자기 제외의 V0 라벨
#
# -out: ((ok, f1), bm, ranks) = 고른 설정과 그 순위
# -out: error = 없음
#------------------------------------------------------------------
def tune(rows_a, docs, seeds, ctx, ranks_by_w, base_bm, tok, norm, grid, half_a, v0):
    v0a = metrics(rows_a, v0)
    best = None
    for wt, ranks in ranks_by_w:
        if grid is None:
            tops = sorted(top_value(r, "abs") for n, r in ranks.items() if r and n in half_a)
            qs = [0.0] + [tops[int(len(tops) * f)] for f in (0.25, 0.5, 0.75, 0.9)] if tops else [0.0]
        else:
            qs = grid
        for ms in qs:
            for mm in (0.0, 0.1, 0.2, 0.3, 0.5):
                bm = dict(base_bm, fields=wt, min_score=round(ms, 3), min_margin=mm, tok=tok, norm=norm)
                m = metrics(rows_a, run_variant(docs, seeds, ctx, ranks, bm, "vec+bm25"))
                ok = (m["precision"] or 0) >= (v0a["precision"] or 0) - 0.01
                # 정밀도 조건을 만족하는 설정이 우선, 그다음 F1
                if best is None or (ok, m["f1"]) > best[0]:
                    best = ((ok, m["f1"]), bm, ranks)
    return best


#------------------------------------------------------------------
# 한 변형을 앞 절반에서 고르고 전체로 돌리기
#=> 가중 후보 · 문턱 방식 후보를 모두 tune 에 넣어 가장 나은 설정을 고른 뒤,
#   그 설정으로 500건 라벨을 만들어 E["res"] · E["tuned"] 에 남긴다.
#
# -in: E     = 실험 묶음(docs · seeds · ctx · stoks · rows · rows_a · half_a · base_bm · v0 · grids · res · tuned)
# -in: key   = 결과 표에 쓸 이름
# -in: tok   = 토크나이저 이름
# -in: wsets = 필드 가중 후보 목록
# -in: norms = 문턱 방식 후보("abs" · "ub" · "self")
# -in: extra = BM25 설정에 덧붙일 값(name_rule · cap_unsafe 등)
#
# -out: (bm, ranks) = 고른 설정과 그 순위
# -out: error = 없음
#------------------------------------------------------------------
def run_tuned(E, key, tok, wsets, norms, extra=None):
    base = dict(E["base_bm"], **(extra or {}))
    ranks_by_w = [(wt, rank_all(E["docs"], E["stoks"][tok], E["seeds"], dict(base, fields=wt, tok=tok)))
                  for wt in wsets]
    best = None
    for norm in norms:
        r = tune(E["rows_a"], E["docs"], E["seeds"], E["ctx"], ranks_by_w, base, tok, norm,
                 E["grids"][norm], E["half_a"], E["v0"])
        if best is None or r[0] > best[0]:
            best = r
    (ok, _), bm, ranks = best
    E["tuned"][key] = {"bm": bm, "precision_ok_on_half_a": ok,
                       "pick": pick_quality(E["rows"], E["docs"], ranks, bm)}
    E["res"][key] = run_variant(E["docs"], E["seeds"], E["ctx"], ranks, bm, "vec+bm25")
    return bm, ranks


#------------------------------------------------------------------
# 결과 표 찍기
#=> 변형마다 전체 · 사람 · 뒤 절반 사람의 정밀도 · 재현율 · F1 을 한 줄로 찍는다.
#
# -in: title = 표 제목
# -in: keys  = 찍을 변형 이름들
# -in: E     = 실험 묶음
#
# -out: dict = {변형: {all, 사람, B_사람}} 지표
# -out: error = 없음
#------------------------------------------------------------------
def print_table(title, keys, E):
    fmt = lambda m: f"{(m['precision'] or 0) * 100:5.1f} {(m['recall'] or 0) * 100:5.1f} {m['f1'] * 100:5.1f}"
    print(f"\n── {title} " + "─" * 40)
    print(f"   {'변형':<22} {'전체 P · R · F1':>17} | {'사람 P · R · F1':>17} | {'뒤절반 사람':>17}")
    out = {}
    for k in keys:
        lab = E["res"][k]
        out[k] = {"all": metrics(E["rows"], lab), "사람": metrics(E["rows"], lab, "사람"),
                  "B_사람": metrics(E["rows_b"], lab, "사람")}
        t = out[k]
        print(f"   {k:<22} {fmt(t['all'])} | {fmt(t['사람'])} | {fmt(t['B_사람'])}")
    for k in keys:
        v = E["tuned"].get(k)
        if v:
            bm = v["bm"]
            print(f"     · {k}: 문턱 {bm.get('norm')} {bm['min_score']} · 차 {bm['min_margin']} · 가중 {bm['fields']}"
                  f"{' · ' + bm['name_rule'] if bm.get('name_rule') else ''} · 정밀도조건 "
                  f"{'충족' if v['precision_ok_on_half_a'] else '미충족'} · 1위 고름 {v['pick']}")
    return out


#------------------------------------------------------------------
# 기준 문서끼리 자기 제외 채점(자동 문턱의 재료)
#=> 기준 문서를 하나씩 빼고 나머지로 분류 문서를 만든 뒤, 뺀 문서의 제목 · 파일명으로
#   BM25 1위를 구해 (1위 점수, 맞았나)를 모은다. 정답지 없이 기준 문서만으로 문턱을
#   정하려는 것이다.
#    1) 뺄 것: 자기 자신 + 같은 지문 + 거의 같은 기준 문서(벡터 ≥ dup) + drop
#    2) 자기 분류가 빠지고 나면 min_seeds 를 못 채우는 기준 문서는 채점에서 뺀다
#       (그 분류는 어차피 BM25 에 참여하지 못한다)
#
# -in: seeds = 기준 문서
# -in: stoks = 기준 문서 토큰(이 토크나이저)
# -in: bm    = BM25 설정(fields · min_seeds 등)
# -in: dup   = 거의 같은 기준 문서로 볼 벡터 유사도(기본 0.95)
# -in: drop  = 아예 없는 것으로 칠 기준 문서 번호(강건성 시험용)
#
# -out: list[(score, correct)] = 기준 문서별 1위 점수와 정답 여부
# -out: error = 없음
#------------------------------------------------------------------
def seed_loo_stats(seeds, stoks, bm, dup=0.95, drop=frozenset()):
    out = []
    for i, s in enumerate(seeds):
        if i in drop:
            continue
        skip = {j for j, t in enumerate(seeds)
                if j == i or (s["hash"] and t["hash"] == s["hash"]) or dot(s["vec"], t["vec"]) >= dup} | set(drop)
        left = collections.Counter(d for j, t in enumerate(seeds) if j not in skip for d in t["labels"])
        if not any(left[d] >= bm["min_seeds"] for d in s["labels"]):
            continue
        # 기준 문서 자신의 필드를 질의로 — Counter 를 토큰 목록으로 되돌린다
        q = {f: list(stoks[i][f].elements()) for f in bm["fields"]}
        r = bm25_category(q, stoks, seeds, skip, bm)
        if r:
            out.append((r[0][1], r[0][0] in s["labels"]))
    return out


#------------------------------------------------------------------
# 자동 문턱 고르기
#=> (점수, 맞았나) 목록을 점수 내림차순으로 줄 세워, 통과한 것들의 정밀도가 목표 이상인
#   가장 낮은 점수를 문턱으로 쓴다. 통과 건수가 min_pass 보다 적으면 정하지 않는다.
#
# -in: stats    = seed_loo_stats 결과
# -in: target   = 목표 정밀도(예: 0.90)
# -in: min_pass = 문턱을 정하려면 통과해야 하는 최소 건수
#
# -out: dict | None = {min_score, passed, correct, precision, n} (못 정하면 None)
# -out: error = 없음
#------------------------------------------------------------------
def auto_threshold(stats, target, min_pass=10):
    st = sorted(stats, key=lambda t: -t[0])
    best, ok = None, 0
    for k, (score, good) in enumerate(st, 1):
        ok += good
        # 같은 점수가 이어지면 그 묶음 끝에서만 판단한다(문턱은 '이상'이라 함께 통과한다)
        if k < len(st) and st[k][0] == score:
            continue
        if k >= min_pass and ok / k >= target:
            best = {"min_score": round(score, 3), "passed": k, "correct": ok,
                    "precision": round(ok / k, 4), "n": len(st)}
    return best


#------------------------------------------------------------------
# E. 자동 문턱 검증
#=> 기준 문서만으로 자동 계산한 문턱이, 평가셋 정답지로 맞춘 문턱만큼 통하는지 본다.
#    1) 같은 설정(2-gram · 제목 2 + 정리 파일명 2 · 파일명 단독 0.55 표시)에서
#       평가셋 앞 절반으로 맞춘 문턱 · 고정 30.0 · 자동(목표 85 · 90 · 95%)을 견준다
#    2) 기준 문서 70%(3회)에서도 자동 문턱 · 고정 30 · 다시 맞춘 문턱을 견준다
#
# -in: E    = 실험 묶음
# -in: seed = 난수 시드(강건성용 기준 문서 고르기)
#
# -out: dict = 표 · 자동 문턱 기록
# -out: error = 없음
#------------------------------------------------------------------
def auto_check(E, seed):
    docs, seeds, ctx, rows = E["docs"], E["seeds"], E["ctx"], E["rows"]
    stoks = E["stoks"]["bigram"]
    fields = {"title": 2.0, "name2": 2.0}
    base = dict(E["base_bm"], name_rule="mark", cap_unsafe=0.55)
    cfg = dict(base, fields=fields, tok="bigram", norm="abs", min_margin=0.0)
    targets = (0.85, 0.90, 0.95)
    met = lambda lab: (metrics(rows, lab, "사람"), metrics(E["rows_b"], lab, "사람"), metrics(rows, lab))
    out = {"full": {}, "robust": []}

    set_skip(docs, seeds, ctx, "strict")
    ranks = rank_all(docs, stoks, seeds, cfg)
    _, bm_eval, _ = tune(E["rows_a"], docs, seeds, ctx, [(fields, ranks)], base, "bigram", "abs", None,
                         E["half_a"], E["v0"])
    stats = seed_loo_stats(seeds, stoks, cfg, ctx["embed"]["dup_threshold"])
    rowsout = [("V0 지금", None, met(E["v0"])),
               (f"평가셋으로 맞춤({bm_eval['min_score']})", bm_eval["min_score"],
                met(run_variant(docs, seeds, ctx, ranks, bm_eval, "vec+bm25"))),
               ("고정 30.0", 30.0, met(run_variant(docs, seeds, ctx, ranks, dict(cfg, min_score=30.0), "vec+bm25")))]
    autos = {}
    for t in targets:
        a = auto_threshold(stats, t)
        autos[t] = a
        if a:
            lab = run_variant(docs, seeds, ctx, ranks, dict(cfg, min_score=a["min_score"]), "vec+bm25")
            rowsout.append((f"자동 목표 {int(t * 100)}% → {a['min_score']}", a["min_score"], met(lab)))
        else:
            rowsout.append((f"자동 목표 {int(t * 100)}% → 못 정함", None, None))
    out["full"] = {"seed_stats_n": len(stats), "seed_stats_correct": sum(g for _, g in stats),
                   "auto": {str(t): v for t, v in autos.items()},
                   "rows": [(n, s, m and {"사람": m[0], "B_사람": m[1], "all": m[2]}) for n, s, m in rowsout]}

    fmt = lambda m: f"{(m['precision'] or 0) * 100:5.1f} {(m['recall'] or 0) * 100:5.1f} {m['f1'] * 100:5.1f}"
    print("\n── E. 자동 문턱 — 기준 문서끼리 자기 제외로 정한 문턱 vs 평가셋으로 맞춘 문턱 ──")
    print(f"   기준 문서 자기 제외 채점 {len(stats)}건 · 1위 정답 {sum(g for _, g in stats)}건")
    for t, v in autos.items():
        print(f"   목표 {int(t * 100)}%: " + (f"문턱 {v['min_score']} · 통과 {v['passed']} · 정답 {v['correct']} · "
                                        f"정밀도 {v['precision'] * 100:.1f}%" if v else "정하지 못함"))
    print(f"   {'문턱':<26} {'사람 P · R · F1':>17} | {'뒤절반 사람':>17} | {'전체 P · R · F1':>17}")
    for n, _, m in rowsout:
        print(f"   {n:<26} " + (f"{fmt(m[0])} | {fmt(m[1])} | {fmt(m[2])}" if m else "—"))

    # ── 강건성: 기준 문서 70% ──
    print("\n   [강건성] 기준 문서 70%(3회) · 사람 F1 — 자동 85 / 90 / 95% · 고정 30 · 다시 맞춤(평가셋)")
    for rep in range(3):
        drop = set(random.Random(seed + 100 + rep).sample(range(len(seeds)), int(len(seeds) * 0.3)))
        set_skip(docs, seeds, ctx, "strict", drop)
        v0s = run_variant(docs, seeds, ctx, mode="vec")
        rk = rank_all(docs, stoks, seeds, cfg)
        st = seed_loo_stats(seeds, stoks, cfg, ctx["embed"]["dup_threshold"], drop)
        line, rec = [], {"V0": metrics(rows, v0s, "사람")["f1"]}
        for t in targets:
            a = auto_threshold(st, t)
            f1 = metrics(rows, run_variant(docs, seeds, ctx, rk, dict(cfg, min_score=a["min_score"]), "vec+bm25"),
                         "사람")["f1"] if a else None
            rec[f"auto{int(t * 100)}"] = (a and a["min_score"], f1)
            line.append(f"{a['min_score']:.1f}→{f1 * 100:.1f}" if a else "—")
        f30 = metrics(rows, run_variant(docs, seeds, ctx, rk, dict(cfg, min_score=30.0), "vec+bm25"), "사람")["f1"]
        _, bo, _ = tune(E["rows_a"], docs, seeds, ctx, [(fields, rk)], base, "bigram", "abs", None, E["half_a"], v0s)
        fo = metrics(rows, run_variant(docs, seeds, ctx, rk, bo, "vec+bm25"), "사람")["f1"]
        rec["fixed30"], rec["retuned"] = f30, (bo["min_score"], fo)
        out["robust"].append(rec)
        print(f"   {rep + 1}회  V0 {rec['V0'] * 100:.1f} · 자동 " + " / ".join(line)
              + f" · 고정30 {f30 * 100:.1f} · 다시맞춤 {bo['min_score']:.1f}→{fo * 100:.1f}")
    set_skip(docs, seeds, ctx, "strict")
    return out


#------------------------------------------------------------------
# 실험 전체
#=> 1) 평가셋 · 1차 결과 · 기준 문서를 읽는다
#   2) 자기 제외 없이 V0 를 다시 계산해 엔진 정상 실행(runA)과 같은지 확인한다(재현 검증)
#   3) 평가셋을 반으로 나눠 앞 절반에서 BM25 문턱 · 가중을 고르고 뒤 절반에서 확인한다
#   4) 자기 제외(엄격)로 A 문턱 방식 · B 필드 가중 · C 파일명 다루는 방식을 채점하고,
#      D 기준 문서를 70% 만 쓸 때도 고른 문턱이 통하는지 본다 — 표 · json 으로 남긴다
#
# -in: argv = 명령줄 인자(None 이면 sys.argv)
#
# -out: 없음(표준출력 + --json 파일)
# -out: error = 입력 파일이 없으면 예외 전파
#------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", default=os.path.join(S.ROOT, "resources", "golden_dataset", "업무분류-평가셋-500-20260918-사람재검-01.xlsx"))
    ap.add_argument("--run-a", required=True, help="엔진 정상 실행 결과(재현 검증용)")
    ap.add_argument("--run-b", required=True, help="--rule-only --with-vector --hash --textsave 결과")
    ap.add_argument("--text", required=True, help="run-b 의 --textsave 폴더")
    ap.add_argument("--policy", default=os.path.join(S.ROOT, "ui", "policy"))
    ap.add_argument("--seed-text", default=os.path.join(S.ROOT, "ui", "_extracted"))
    ap.add_argument("--json", help="결과 json 경로")
    ap.add_argument("--seed", type=int, default=20261022, help="반 나누기 시드")
    ap.add_argument("--auto-only", action="store_true",
                    help="자동 문턱 검증(E)만 — A~D 와 Kiwi 를 건너뛰어 빨리 돈다")
    a = ap.parse_args(argv)

    rule = yaml.safe_load(open(os.path.join(a.policy, "doc_rule.yaml"), encoding="utf-8"))
    emb = rule.get("embed") or {}
    ctx = {"tax": load_taxonomy(os.path.join(a.policy, "doc_classification_export.json")),
           "embed": {"k": emb.get("k", 5), "dup_threshold": emb.get("dup_threshold", 0.95),
                     "min_sim": emb.get("min_sim", 0.70), "min_share": emb.get("min_share", 0.40)},
           "merge": {"embed_cap": (rule.get("defaults") or {}).get("embed_cap", 0.65),
                     "cap_alone": 0.60, "cap_agree": 0.75,
                     "top_n": (rule.get("conflict") or {}).get("n", 2)}}
    head_chars = (rule.get("defaults") or {}).get("head_chars", 400)
    noise = rule.get("noise_tails") or []

    wb = openpyxl.load_workbook(a.xlsx)
    rows, _, _ = S.read_doctype_rows(wb, {}, None, S.SRC_DIR)
    run_a, run_b = load_jsonl(a.run_a), load_jsonl(a.run_b)
    seeds = load_seeds(os.path.join(a.policy, "class_seed.jsonl"), a.seed_text)
    print(f"평가 문서 {len(rows)} · 기준 문서 {len(seeds)}(본문 있음 {sum(1 for s in seeds if s['text'])})")

    toks = {"bigram": tok_bigram} if a.auto_only else {"bigram": tok_bigram, "kiwi": tok_kiwi}
    # 기준 문서 토큰은 토크나이저마다 한 번만 만든다
    stoks, tok_ms = {}, {}
    for name, fn in toks.items():
        t0 = time.perf_counter()
        stoks[name] = [{f: collections.Counter(fn(v)) for f, v in
                        fields_of(s["text"], s["file"], head_chars, noise).items()} for s in seeds]
        tok_ms[name] = (time.perf_counter() - t0) * 1000 / max(len(seeds), 1)

    docs, miss = [], 0
    for x in rows:
        rec = run_b.get(S.norm_path(os.path.join(S.SRC_DIR, x["relpath"])))
        if rec is None:
            miss += 1
            continue
        tp = os.path.join(a.text, f"{rec.get('hash')}.txt")
        text = open(tp, encoding="utf-8", errors="ignore").read() if os.path.isfile(tp) else ""
        # 1차 후보는 --rule-only(run_b)에서, 벡터는 정상 실행(run_a)에서 — 정상 실행은
        # 2차 대상 문서에만 벡터를 실어 내므로 엔진이 실제로 쓴 벡터와 같다
        ra = run_a.get(S.norm_path(rec["file"])) or {}
        vec = l2(ra["vector"]) if ra.get("vector") else []
        flds = fields_of(text, rec["file"], head_chars, noise)
        hard = {i for i, s in enumerate(seeds) if s["hash"] and s["hash"] == rec.get("hash")}
        near = {i for i, s in enumerate(seeds) if vec and dot(vec, s["vec"]) >= ctx["embed"]["dup_threshold"]}
        docs.append({"no": x["no"], "existing": ((rec.get("why") or {}).get("doctype") or {}).get("values") or [],
                     "need": need_2nd(rec), "vec": vec, "hard": hard, "near": near, "flds": flds,
                     "unsafe": os.path.splitext(rec["file"])[1].lower() in TITLE_UNSAFE_EXTS,
                     "q": {n: {f: fn(v) for f, v in flds.items()} for n, fn in toks.items()}})
    print(f"1차 결과에서 못 찾은 평가 문서 {miss} · 2차 대상 {sum(d['need'] for d in docs)}")
    print(f"자기 제외: 같은 지문 {sum(bool(d['hard']) for d in docs)}건 · 거의 같은 기준 문서(≥0.95) 있음 {sum(bool(d['near']) for d in docs)}건")

    # ── 재현 검증: 자기 제외 없이 V0 = 엔진 정상 실행 ─────────────────
    set_skip(docs, seeds, ctx, "none")
    v0_full = run_variant(docs, seeds, ctx, mode="vec")
    same = diff = 0
    for x in rows:
        rec = run_a.get(S.norm_path(os.path.join(S.SRC_DIR, x["relpath"])))
        if rec is None or x["no"] not in v0_full:
            continue
        if sorted(rec.get("doctype") or []) == sorted(v0_full[x["no"]]):
            same += 1
        else:
            diff += 1
    print(f"[재현 검증] 자기 제외 없는 V0 vs 엔진 정상 실행: 같음 {same} · 다름 {diff}")

    rng = random.Random(a.seed)
    nos = sorted(d["no"] for d in docs)
    rng.shuffle(nos)
    half_a = set(nos[: len(nos) // 2])

    base_bm = {"k1": 1.2, "b": 0.75, "idf_min": 0.1, "idf_max": 3.0,
               "min_seeds": 3, "min_categories": 4, "min_score": 0.0, "min_margin": 0.0}
    grids = {"abs": None, "ub": (0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6),
             "self": (0.0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0)}
    weights = {"제목2·앞1·파일1.5": {"title": 2.0, "head": 1.0, "name": 1.5},
               "제목1·앞1·파일1": {"title": 1.0, "head": 1.0, "name": 1.0},
               "제목3·앞0.5·파일2": {"title": 3.0, "head": 0.5, "name": 2.0},
               "제목2·앞0·파일2": {"title": 2.0, "head": 0.0, "name": 2.0}}
    name2_w = [{"title": 2.0, "name2": 2.0}, {"title": 2.0, "name2": 1.0}, {"title": 1.0, "name2": 1.0}]
    name_w = [{"title": 2.0, "name": 2.0}, {"title": 2.0, "name": 1.0}, {"title": 1.0, "name": 1.0}]
    all_norms = ("abs", "ub", "self")

    # 자기 제외는 엄격(같은 지문 + 거의 같은 기준 문서)으로 고정 — 판단 근거로 쓰는 기준
    set_skip(docs, seeds, ctx, "strict")
    E = {"docs": docs, "seeds": seeds, "ctx": ctx, "stoks": stoks, "rows": rows,
         "rows_a": [x for x in rows if x["no"] in half_a], "rows_b": [x for x in rows if x["no"] not in half_a],
         "half_a": half_a, "base_bm": base_bm, "grids": grids, "res": {}, "tuned": {}}
    E["v0"] = E["res"]["V0"] = run_variant(docs, seeds, ctx, mode="vec")
    results = {"repro": {"same": same, "diff": diff}, "tok_ms_per_seed": tok_ms, "tables": {}}

    if not a.auto_only:
        # ── A. 문턱 방식(abs · ub · self) × 토크나이저 ──────────────────
        keys_a = ["V0"]
        for tok in ("bigram", "kiwi"):
            for norm in all_norms:
                k = f"{'V1' if tok == 'bigram' else 'V2'}-{norm}"
                run_tuned(E, k, tok, list(weights.values()), [norm])
                keys_a.append(k)
        results["tables"]["A_문턱방식"] = print_table("A. 문턱 방식 비교(자기 제외 엄격)", keys_a, E)

        # ── B. 가중 설정별(2-gram, 문턱 방식은 셋 중 가장 나은 것) ─────────
        keys_b = ["V0"]
        for label, wt in weights.items():
            k = f"W {label}"
            run_tuned(E, k, "bigram", [wt], all_norms)
            keys_b.append(k)
        results["tables"]["B_가중별"] = print_table("B. 필드 가중별(2-gram) — 앞 400자를 넣으면?", keys_b, E)

        # ── C. 파일명 변형(2-gram) ─────────────────────────────────────
        variants = [
            ("F 제목만", [{"title": 1.0}], None),
            ("F 제목+파일명", name_w, None),
            ("F 제목+파일명 단독금지", name_w, {"name_rule": "block"}),
            ("F 제목+정리파일명", name2_w, None),
            ("F 정리파일명 단독금지", name2_w, {"name_rule": "block"}),
            ("F 정리 단독금지+ppt예외", name2_w, {"name_rule": "block_unsafe_ok", "cap_unsafe": 0.55}),
        ]
        keys_c = ["V0"]
        for k, wsets, extra in variants:
            run_tuned(E, k, "bigram", wsets, all_norms, extra)
            keys_c.append(k)
        results["tables"]["C_파일명"] = print_table("C. 파일명 다루는 방식(2-gram)", keys_c, E)

        # ── D. 강건성: 기준 문서를 70% 만 쓸 때, 전체로 고른 문턱이 그대로 통하나 ──
        rob_keys = ["V1-abs", "V1-ub", "V1-self", "F 정리파일명 단독금지", "F 정리 단독금지+ppt예외"]
        rob = collections.defaultdict(list)
        for rep in range(3):
            drop = set(random.Random(a.seed + 100 + rep).sample(range(len(seeds)), int(len(seeds) * 0.3)))
            set_skip(docs, seeds, ctx, "strict", drop)
            v0s = run_variant(docs, seeds, ctx, mode="vec")
            rob["V0"].append(metrics(rows, v0s, "사람")["f1"])
            for k in rob_keys:
                bm = E["tuned"][k]["bm"]
                ranks = rank_all(docs, stoks["bigram"], seeds, bm)
                got = metrics(rows, run_variant(docs, seeds, ctx, ranks, bm, "vec+bm25"), "사람")
                # 같은 70% 기준 문서로 문턱만 다시 골랐다면 얻었을 성적 — 옮겨 쓴 손해를 잰다
                extra = {kk: bm[kk] for kk in ("name_rule", "cap_unsafe") if kk in bm}
                _, bmo, ro = tune(E["rows_a"], docs, seeds, ctx, [(bm["fields"], ranks)],
                                  dict(base_bm, **extra), "bigram", bm["norm"], grids[bm["norm"]], half_a, v0s)
                best = metrics(rows, run_variant(docs, seeds, ctx, ro, bmo, "vec+bm25"), "사람")
                rob[k].append((got["f1"], best["f1"], got["precision"], got["recall"]))
        set_skip(docs, seeds, ctx, "strict")
        results["robust"] = dict(rob)
        print("\n── D. 강건성 — 기준 문서 70%(3회) · 사람 F1: 옮겨 쓴 문턱 / 다시 고른 문턱 (P·R) ──")
        print("   V0                      " + " · ".join(f"{x * 100:.1f}" for x in rob["V0"]))
        for k in rob_keys:
            v = rob[k]
            print(f"   {k:<24} " + " · ".join(f"{g * 100:.1f}/{o * 100:.1f} ({p * 100:.0f}·{r * 100:.0f})"
                                             for g, o, p, r in v)
                  + f"   평균 손해 {sum(o - g for g, o, _, _ in v) / len(v) * 100:.1f}%p")
        results["tuned"] = {k: v for k, v in E["tuned"].items()}

    # ── E. 자동 문턱 검증 ──────────────────────────────────────────
    results["auto"] = auto_check(E, a.seed)


    # ── 시간: 문서 하나의 토큰화 + 채점 ─────────────────────────────
    for tok in toks:
        bm = dict(base_bm, fields={"title": 2.0, "head": 1.0, "name": 1.5}, tok=tok)
        t0 = time.perf_counter()
        for d in docs[:200]:
            # 질의 토큰화 + 분류 단위 채점까지 — 문서 하나에 실제로 더해지는 시간
            q = {f: toks[tok](v) for f, v in d["flds"].items()}
            bm25_category(q, stoks[tok], seeds, set(), bm)
        results.setdefault("score_ms_per_doc", {})[tok] = (time.perf_counter() - t0) * 1000 / 200
    print("\n   시간(ms) 기준 문서 1건 토큰화:", {k: round(v, 2) for k, v in tok_ms.items()},
          "· 문서 1건 BM25 채점:", {k: round(v, 2) for k, v in results["score_ms_per_doc"].items()})

    if a.json:
        with open(a.json, "w", encoding="utf-8") as fp:
            json.dump(results, fp, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
