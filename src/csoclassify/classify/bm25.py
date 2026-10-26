"""업무분류 2차 BM25 — 제목 · 파일명 말 비교(설계서 plan/업무분류-2차-BM25-설계-20261022.html §16).

벡터 2차와 나란히, 지금 문서의 제목 · 파일명을 '분류별로 묶은 기준 문서'와 BM25 로
견준다. 최소 점수는 기준 문서끼리 자기 제외 모의시험으로 실행마다 자동 계산한다.
Rust 판 Rust/src/bm25.rs 와 토큰 · 점수 · 문턱이 같아야 한다(두 판 일치).
"""
import math
import re
import unicodedata
from dataclasses import dataclass, field

from .doctype import _first_line, _title_allowed

# 단어(한글 음절 · 영문 소문자 · 숫자 덩어리). 정규식 앞뒤 보기는 쓰지 않는다 — Rust regex 와 같게
_WORD = re.compile(r"[0-9a-z가-힣]+")
# 파일명의 괄호 안 내용(고객사 이름 · 판 번호가 많다)
_PAREN = re.compile(r"[\(\[\{（【][^\)\]\}）】]*[\)\]\}）】]")
# 숫자가 한 글자라도 섞인 말(번호 · 버전 · 날짜)
_DIGIT_WORD = re.compile(r"[0-9A-Za-z가-힣]*\d[0-9A-Za-z가-힣]*")
TITLE_MAX = 200


#------------------------------------------------------------------
# BM25 설정(doc_rule.yaml 의 bm25: 블록)
#=> 블록이 없으면 enabled=False 라 예전과 똑같이 돈다(옛 규칙 파일 호환).
#
# -필드: enabled          = BM25 2차를 쓸지
# -필드: tokenizer        = 토큰 방식(지금은 "bigram" 만)
# -필드: fields           = 필드 무게 {"title": 1.0, "name": 1.0}
# -필드: mode             = 문턱 방식 "auto"(기준 문서 모의시험) | "fixed"(min_score)
# -필드: target_precision = auto 의 목표 정밀도(0.95)
# -필드: min_pass         = auto 가 문턱을 정하려면 통과해야 하는 최소 건수(10)
# -필드: min_margin       = 1 · 2위 점수 차 하한(1위 대비 비율, 0 = 따지지 않음)
# -필드: min_score        = fixed 일 때 문턱
# -필드: name_only_allow  = 파일명에서만 맞은 후보를 낼지
# -필드: name_only_cap    = 그 후보의 확신 상한(0.55)
# -필드: k1 · b           = BM25 표준값
# -필드: idf_min · idf_max = 말 희소성 점수의 아래 · 위 한계
# -필드: min_seeds        = 분류가 BM25 에 참여하려면 필요한 기준 문서 수
# -필드: min_categories   = 참여 분류가 이보다 적으면 BM25 를 끈다
# -필드: cap_alone        = BM25 만 낸 후보의 확신 상한
# -필드: cap_agree        = 벡터와 같은 분류를 낸 후보의 확신
# -필드: stopwords        = 비교에서 뺄 양식 말
#------------------------------------------------------------------
@dataclass(frozen=True)
class Bm25Spec:
    enabled: bool = False
    tokenizer: str = "bigram"
    fields: tuple = (("title", 1.0), ("name", 1.0))
    mode: str = "auto"
    target_precision: float = 0.95
    min_pass: int = 10
    min_margin: float = 0.0
    min_score: float = 15.0
    name_only_allow: bool = True
    name_only_cap: float = 0.55
    k1: float = 1.2
    b: float = 0.75
    idf_min: float = 0.1
    idf_max: float = 3.0
    min_seeds: int = 3
    min_categories: int = 4
    cap_alone: float = 0.60
    cap_agree: float = 0.75
    stopwords: frozenset = field(default_factory=lambda: frozenset(
        ("작성자", "작성일", "페이지", "문서번호", "목차", "page", "of")))

    #------------------------------------------------------------------
    # 필드 무게 dict
    #
    # -in: 없음
    #
    # -out: dict = {필드: 무게}(규칙 파일에 적은 순서)
    # -out: error = 없음
    #------------------------------------------------------------------
    def weights(self):
        return dict(self.fields)


#------------------------------------------------------------------
# bm25: 블록 검사 · 파싱 (T23)
#=> 규칙 파일 값이 쓸 수 있는지 보고 Bm25Spec 을 만든다. 틀린 칸은 기본값을 두고 문제로 알린다.
#   블록이 없으면 enabled=False(예전 동작).
#
# -in: raw = data.get("bm25") 원본(없으면 None)
#
# -out: (Bm25Spec, problems) = problems 는 [(칸 이름, 값, 설명)]
# -out: error = 없음
#------------------------------------------------------------------
def parse_spec(raw):
    base = Bm25Spec()
    if raw is None:
        return base, []
    if not isinstance(raw, dict):
        return base, [("bm25", raw, "bm25 는 매핑(mapping)이어야 합니다")]
    probs, vals = [], {}
    num = lambda v: not isinstance(v, bool) and isinstance(v, (int, float))
    integer = lambda v: not isinstance(v, bool) and isinstance(v, int)

    if "enabled" in raw:
        if isinstance(raw["enabled"], bool):
            vals["enabled"] = raw["enabled"]
        else:
            probs.append(("bm25.enabled", raw["enabled"], "true 또는 false 여야 합니다"))
    if "tokenizer" in raw:
        if raw["tokenizer"] == "bigram":
            vals["tokenizer"] = "bigram"
        else:
            probs.append(("bm25.tokenizer", raw["tokenizer"], "지금은 bigram 만 쓸 수 있습니다"))
    if "fields" in raw:
        f = raw["fields"]
        ok = isinstance(f, dict) and f and all(k in ("title", "name") and num(v) and v >= 0
                                               for k, v in f.items()) and any(v > 0 for v in f.values())
        if ok:
            vals["fields"] = tuple((k, float(v)) for k, v in f.items())
        else:
            probs.append(("bm25.fields", f, "{title: 숫자, name: 숫자} 매핑이어야 하고 하나는 0보다 커야 합니다"))
    th = raw.get("threshold")
    if th is not None:
        if not isinstance(th, dict):
            probs.append(("bm25.threshold", th, "매핑이어야 합니다"))
            th = {}
        if "mode" in th:
            if th["mode"] in ("auto", "fixed"):
                vals["mode"] = th["mode"]
            else:
                probs.append(("bm25.threshold.mode", th["mode"], "auto | fixed 중 하나여야 합니다"))
        for k, lo, hi, lo_open in (("target_precision", 0.0, 1.0, True), ("min_margin", 0.0, 1.0, False)):
            if k in th:
                v = th[k]
                if num(v) and (lo < v if lo_open else lo <= v) and v <= hi:
                    vals[k] = float(v)
                else:
                    probs.append((f"bm25.threshold.{k}", v, "0~1 사이 숫자여야 합니다"))
        if "min_pass" in th:
            if integer(th["min_pass"]) and th["min_pass"] >= 1:
                vals["min_pass"] = th["min_pass"]
            else:
                probs.append(("bm25.threshold.min_pass", th["min_pass"], "1 이상의 정수여야 합니다"))
        if "min_score" in th:
            if num(th["min_score"]) and th["min_score"] >= 0:
                vals["min_score"] = float(th["min_score"])
            else:
                probs.append(("bm25.threshold.min_score", th["min_score"], "0 이상 숫자여야 합니다"))
    no = raw.get("name_only")
    if no is not None:
        if not isinstance(no, dict):
            probs.append(("bm25.name_only", no, "매핑이어야 합니다"))
            no = {}
        if "allow" in no:
            if isinstance(no["allow"], bool):
                vals["name_only_allow"] = no["allow"]
            else:
                probs.append(("bm25.name_only.allow", no["allow"], "true 또는 false 여야 합니다"))
        if "cap" in no:
            if num(no["cap"]) and 0 <= no["cap"] <= 1:
                vals["name_only_cap"] = float(no["cap"])
            else:
                probs.append(("bm25.name_only.cap", no["cap"], "0~1 사이 숫자여야 합니다"))
    for k, lo, hi in (("k1", 0.0, None), ("b", 0.0, 1.0), ("idf_min", 0.0, None), ("idf_max", 0.0, None),
                      ("cap_alone", 0.0, 1.0), ("cap_agree", 0.0, 1.0)):
        if k in raw:
            v = raw[k]
            if num(v) and v >= lo and (hi is None or v <= hi):
                vals[k] = float(v)
            else:
                probs.append((f"bm25.{k}", v, "범위를 벗어난 숫자입니다"))
    for k, lo in (("min_seeds", 1), ("min_categories", 2)):
        if k in raw:
            v = raw[k]
            if integer(v) and v >= lo:
                vals[k] = v
            else:
                probs.append((f"bm25.{k}", v, f"{lo} 이상의 정수여야 합니다"))
    if "stopwords" in raw:
        sw = raw["stopwords"]
        if isinstance(sw, list) and all(isinstance(x, str) for x in sw):
            vals["stopwords"] = frozenset(norm_text(x) for x in sw)
        else:
            probs.append(("bm25.stopwords", sw, "문자열 목록이어야 합니다"))
    spec = Bm25Spec(**{**base.__dict__, **vals})
    if spec.idf_min > spec.idf_max:
        probs.append(("bm25.idf_min", spec.idf_min, "idf_min 이 idf_max 보다 큽니다"))
    return spec, probs


#------------------------------------------------------------------
# 공통 전처리 — NFC 정규화 + 소문자
#=> 같은 글자가 다른 코드로 들어와도(맥에서 만든 파일의 풀어쓴 한글 등) 같은 조각이 되게 한다.
#
# -in: s = 문자열
#
# -out: str = 정규화된 문자열
# -out: error = 없음
#------------------------------------------------------------------
def norm_text(s):
    return unicodedata.normalize("NFC", s or "").lower()


#------------------------------------------------------------------
# 자간 벌린 말 붙이기("출 장 여 비 규 정" → "출장여비규정")
#=> 공백으로 나눈 말 가운데 '한글 한 글자'가 3개 이상 이어지면 붙인다. 정규식 앞뒤 보기
#   없이 말 단위로 처리해 Rust 와 같은 결과를 낸다.
#
# -in: s = 제목 · 파일명
#
# -out: str = 붙인 결과(말 사이는 공백 하나)
# -out: error = 없음
#------------------------------------------------------------------
def despace_spaced(s):
    words = (s or "").split()
    out, run = [], []
    for w in words + [None]:
        if w is not None and len(w) == 1 and "가" <= w <= "힣":
            run.append(w)
            continue
        # 한 글자 말이 끊겼으면 모아 둔 것을 내보낸다 — 3개 이상일 때만 붙인다
        if len(run) >= 3:
            out.append("".join(run))
        else:
            out.extend(run)
        run = []
        if w is not None:
            out.append(w)
    return " ".join(out)


#------------------------------------------------------------------
# 글자 2-gram 토큰(+ 단어)
#=> 단어 안에서만 글자 두 개씩 겹쳐 자르고 단어 통째도 넣는다.
#    1) 한글이 든 단어: "w:단어" + "g:두글자" 전부
#    2) 영문 · 숫자 단어: "w:단어" 만(영문을 글자로 자르면 잡음이 크다)
#    3) 숫자만 · 1글자 · 불용어는 버린다
#
# -in: s         = 필드 문자열
# -in: stopwords = 버릴 말 집합
#
# -out: list[str] = 토큰(나온 순서)
# -out: error = 없음
#------------------------------------------------------------------
def tokenize(s, stopwords=frozenset()):
    out = []
    for w in _WORD.findall(despace_spaced(norm_text(s))):
        if w.isdigit() or len(w) < 2 or w in stopwords:
            continue
        out.append("w:" + w)
        if any("가" <= c <= "힣" for c in w):
            out.extend("g:" + w[i:i + 2] for i in range(len(w) - 1))
    return out


#------------------------------------------------------------------
# 경로에서 확장자 뺀 파일 이름
#=> '/' · '\' 어느 쪽이든 마지막 조각을 쓰고, 마지막 '.' 앞까지를 이름으로 본다(Rust 와 같은 규칙).
#
# -in: file = 파일 경로
#
# -out: str = 확장자 뺀 파일 이름
# -out: error = 없음
#------------------------------------------------------------------
def file_stem(file):
    base = re.split(r"[\\/]", file or "")[-1]
    dot = base.rfind(".")
    return base[:dot] if dot > 0 else base


#------------------------------------------------------------------
# 파일명 정리(강화판)
#=> 문서 종류와 상관없는 잡음을 뺀다.
#    1) 끝 잡음 말(noise_tails: 최종본 · 사본 · draft …) — 대소문자 무시
#    2) 괄호 안 내용 — (실력산업) · (1) · [최종]
#    3) 숫자가 섞인 말 — 05. · v2.0 · 2023Q1 · 20071010
#
# -in: name  = 확장자 뺀 파일 이름
# -in: noise = noise_tails 목록
#
# -out: str = 정리된 파일 이름
# -out: error = 없음
#------------------------------------------------------------------
def clean_name(name, noise=()):
    s = name or ""
    for t in noise:
        if t:
            s = re.sub(re.escape(t), " ", s, flags=re.IGNORECASE)
    s = _PAREN.sub(" ", s)
    return _DIGIT_WORD.sub(" ", s)


#------------------------------------------------------------------
# 문서 제목(1차 규칙과 같은 정의)
#=> 첫 비어 있지 않은 줄, 앞 200자. ppt · xls · xlsx 는 첫 줄이 제목이 아닌 경우가 많아 비운다.
#
# -in: text = 추출 본문
# -in: file = 파일 경로
#
# -out: str = 제목(없으면 빈 문자열)
# -out: error = 없음
#------------------------------------------------------------------
def title_of(text, file):
    if not _title_allowed(file):
        return ""
    return _first_line(text or "")[:TITLE_MAX]


#------------------------------------------------------------------
# 비교 필드 토큰 만들기
#
# -in: title = 제목(이미 title_of 로 뽑은 것)
# -in: file  = 파일 경로
# -in: spec  = Bm25Spec
# -in: noise = noise_tails
#
# -out: dict = {"title": [토큰], "name": [토큰]}
# -out: error = 없음
#------------------------------------------------------------------
def field_tokens(title, file, spec, noise=()):
    return {"title": tokenize(title, spec.stopwords),
            "name": tokenize(clean_name(file_stem(file), noise), spec.stopwords)}


#------------------------------------------------------------------
# 말 세기(dict) — collections.Counter 대신 순서가 정해진 dict 를 써 Rust 와 합산 순서를 맞춘다
#
# -in: toks = 토큰 목록
#
# -out: dict = {토큰: 횟수}
# -out: error = 없음
#------------------------------------------------------------------
def count(toks):
    out = {}
    for t in toks:
        out[t] = out.get(t, 0) + 1
    return out


#------------------------------------------------------------------
# BM25 순위 한 묶음
#=> 점수 내림차순 (분류, 점수) 목록과, 분류별 필드 점수를 함께 나른다.
#
# -필드: ranked = [(dc_id, score)] 점수 내림차순 · 같으면 dc_id 오름차순
# -필드: parts  = {dc_id: {필드: 점수}}
# -필드: words  = {dc_id: [파일명에서 겹친 단어]} — 근거 표시용
#------------------------------------------------------------------
@dataclass
class Ranking:
    ranked: list
    parts: dict
    words: dict


#------------------------------------------------------------------
# 기준 문서 색인(분류 단위 BM25)
#=> 기준 문서의 제목 · 파일명 토큰을 분류별로 묶어 '분류 문서'를 만들고, 질의를 채점한다.
#   자기 제외(모의시험)를 위해 기준 문서 번호로 뺄 수 있다.
#
# -필드: spec   = Bm25Spec
# -필드: labels = 기준 문서별 분류 목록
# -필드: hashes = 기준 문서별 지문
# -필드: vecs   = 기준 문서별 정규화 벡터(없으면 None)
# -필드: toks   = 기준 문서별 {필드: {토큰: 횟수}}
#------------------------------------------------------------------
class Bm25Index:
    #------------------------------------------------------------------
    # 기준 문서 줄들로 색인 만들기
    #=> 업무분류 라벨이 있는 줄만 쓴다. 제목은 줄의 "title" 칸(없으면 빈 제목 — 파일명만).
    #
    # -in: rows  = class_seed.jsonl 줄(dict) 목록
    # -in: spec  = Bm25Spec
    # -in: noise = noise_tails
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def __init__(self, rows, spec, noise=()):
        self.spec = spec
        self.labels, self.hashes, self.vecs, self.toks = [], [], [], []
        for e in rows or ():
            dt = e.get("doctype") or (e.get("labels") or {}).get("doctype") or []
            if not isinstance(dt, list) or not dt:
                continue
            self.labels.append([d for d in dt if isinstance(d, str)])
            self.hashes.append(e.get("hash") or "")
            v = e.get("vector")
            if isinstance(v, list) and v:
                n = math.sqrt(sum(float(x) * float(x) for x in v)) or 1.0
                self.vecs.append([float(x) / n for x in v])
            else:
                self.vecs.append(None)
            f = field_tokens(e.get("title") or "", e.get("file") or "", spec, noise)
            self.toks.append({k: count(f[k]) for k in ("title", "name")})

    #------------------------------------------------------------------
    # 기준 문서 수
    #
    # -in: 없음
    #
    # -out: int = 색인에 든 기준 문서 수
    # -out: error = 없음
    #------------------------------------------------------------------
    def size(self):
        return len(self.labels)

    #------------------------------------------------------------------
    # 분류 단위 채점
    #=> 1) skip 을 뺀 기준 문서로 분류별 필드 말 수를 합친다
    #   2) 기준 문서 min_seeds 이상인 분류만 참여, 참여 분류가 min_categories 미만이면 빈 결과
    #   3) IDF = ln(1 + (N − n + 0.5)/(n + 0.5)) 를 [idf_min, idf_max] 로 자름
    #   4) 필드별 BM25 를 무게로 더한다(말은 정렬 순서로 더해 Rust 와 합산 순서를 맞춤)
    #
    # -in: q    = {필드: 토큰 목록}(질의)
    # -in: skip = 뺄 기준 문서 번호 집합
    #
    # -out: Ranking = 순위(참여 분류가 모자라면 ranked 가 빈 목록)
    # -out: error = 없음
    #------------------------------------------------------------------
    def rank(self, q, skip=frozenset()):
        sp = self.spec
        w = sp.weights()
        nseed, cat = {}, {}
        for i, labs in enumerate(self.labels):
            if i in skip:
                continue
            for d in labs:
                nseed[d] = nseed.get(d, 0) + 1
                c = cat.setdefault(d, {f: {} for f in w})
                for f in w:
                    for t, n in self.toks[i].get(f, {}).items():
                        c[f][t] = c[f].get(t, 0) + n
        cats = sorted(d for d in cat if nseed[d] >= sp.min_seeds)
        if len(cats) < sp.min_categories:
            return Ranking([], {}, {})
        N = len(cats)
        df = {}
        for d in cats:
            seen = set()
            for f in w:
                seen.update(cat[d][f])
            for t in seen:
                df[t] = df.get(t, 0) + 1
        idf = lambda t: min(max(math.log(1 + (N - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5)),
                                sp.idf_min), sp.idf_max)
        avg = {f: (sum(sum(cat[d][f].values()) for d in cats) / N) or 1.0 for f in w}
        qset = {f: sorted(set(q.get(f) or ())) for f in w}
        ranked, parts, words = [], {}, {}
        for d in cats:
            total, by_f, hit = 0.0, {}, []
            for f, wt in w.items():
                dl = sum(cat[d][f].values())
                sf = 0.0
                for t in qset[f]:
                    tf = cat[d][f].get(t, 0)
                    if not tf:
                        continue
                    # 같은 말 반복은 k1 로 포화시키고, 긴 분류 문서는 b 로 깎는다
                    sf += wt * idf(t) * tf * (sp.k1 + 1) / (tf + sp.k1 * (1 - sp.b + sp.b * dl / avg[f]))
                    if f == "name" and t.startswith("w:"):
                        hit.append(t[2:])
                by_f[f] = sf
                total += sf
            ranked.append((d, total))
            parts[d] = by_f
            words[d] = hit
        ranked.sort(key=lambda x: (-x[1], x[0]))
        return Ranking(ranked, parts, words)


#------------------------------------------------------------------
# 1위 고르기(문턱 · 1 · 2위 차)
#
# -in: rk        = Ranking
# -in: threshold = 최소 점수
# -in: margin    = 1 · 2위 차 하한(1위 대비 비율)
#
# -out: str | None = 고른 dc_id
# -out: error = 없음
#------------------------------------------------------------------
def pick(rk, threshold, margin=0.0):
    if not rk.ranked or rk.ranked[0][1] <= 0:
        return None
    s1 = rk.ranked[0][1]
    s2 = rk.ranked[1][1] if len(rk.ranked) > 1 else 0.0
    if s1 < threshold or (s1 - s2) / s1 < margin:
        return None
    return rk.ranked[0][0]


#------------------------------------------------------------------
# 자동 문턱(기준 문서끼리 자기 제외 모의시험)
#=> 설계서 §15. 기준 문서를 하나씩 가리고 나머지로 그 문서의 분류를 맞혀 보게 한 뒤,
#   "문턱을 넘은 답 중 target_precision 이상 맞힌 가장 낮은 점수"를 문턱으로 쓴다.
#    1) 가릴 것: 자기 자신 + 같은 지문 + 벡터가 dup 이상 비슷한 기준 문서
#    2) 가린 뒤 자기 분류가 min_seeds 미만이 되는 기준 문서는 채점에서 뺀다
#    3) 통과 min_pass 건 미만이면 문턱을 정하지 않는다(None)
#
# -in: index = Bm25Index
# -in: dup   = 거의 같은 기준 문서로 볼 벡터 유사도(embed.dup_threshold)
#
# -out: dict = {threshold(None 이면 못 정함), passed, correct, scored}
# -out: error = 없음
#------------------------------------------------------------------
def auto_threshold(index, dup=0.95):
    sp = index.spec
    stats = []
    for i, labs in enumerate(index.labels):
        skip = {i}
        for j in range(index.size()):
            if j == i:
                continue
            same_hash = bool(index.hashes[i]) and index.hashes[j] == index.hashes[i]
            vi, vj = index.vecs[i], index.vecs[j]
            near = vi is not None and vj is not None and sum(a * b for a, b in zip(vi, vj)) >= dup
            if same_hash or near:
                skip.add(j)
        left = {}
        for j, lj in enumerate(index.labels):
            if j not in skip:
                for d in lj:
                    left[d] = left.get(d, 0) + 1
        if not any(left.get(d, 0) >= sp.min_seeds for d in labs):
            continue
        # 기준 문서 자신의 필드를 질의로 — 말 수는 채점에 쓰지 않으므로 목록으로 되돌리기만 한다
        q = {f: list(index.toks[i].get(f, {})) for f in sp.weights()}
        rk = index.rank(q, skip)
        if rk.ranked:
            stats.append((rk.ranked[0][1], rk.ranked[0][0] in labs))
    out = {"threshold": None, "passed": 0, "correct": 0, "scored": len(stats)}
    st = sorted(stats, key=lambda x: -x[0])
    ok = 0
    for k, (score, good) in enumerate(st, 1):
        ok += 1 if good else 0
        # 같은 점수가 이어지면 그 묶음 끝에서만 판단한다(문턱은 '이상'이라 함께 통과한다)
        if k < len(st) and st[k][0] == score:
            continue
        if k >= sp.min_pass and ok / k >= sp.target_precision:
            out.update(threshold=score, passed=k, correct=ok)
    return out
