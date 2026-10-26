#------------------------------------------------------------------
# 업무분류 규칙 파일 자동 생성 (설계: plan/업무분류-규칙파일-자동생성-설계-20260922.html)
#=> doc_rule.yaml 을 '사람이 고치는 파일'에서 '만들어지는 결과물'로 바꾼다.
#   입력(체계 JSON · 유의어 사전 · 본보기 · local 조정)이 같으면 결과가 글자까지 같다.
#   사람의 결정은 doc_rule.local.yaml 에만 적고, 규칙은 매번 새로 만든다 — 그래야
#   '빠진 유의어 더하기' 같은 자동 도구가 사람이 일부러 비운 칸을 되살리는 일
#   (2026-09-22 P2 되돌아감)이 구조적으로 생기지 않는다.
#
#   이 모듈이 하는 일
#    · load_rule_local()   — doc_rule.local.yaml 읽기·검증
#    · build_doc_rule()    — 입력들로 규칙 dict 만들기(설계서 8장 순서)
#    · dump_doc_rule()     — 결정적 YAML 텍스트로 쓰기(Rust 판과 글자까지 같게)
#    · check_generated()   — 실행 때 '입력이 바뀌었는데 다시 안 만들었나'·
#                            '손으로 고친 흔적이 있나' 경고
#------------------------------------------------------------------

import hashlib
import json
import os

import yaml

from . import axes as AX
from . import docvocab as DV
from . import taxhead

# 사람이 고치는 조정 파일 이름(정책 폴더, doc_rule.yaml 옆).
RULE_LOCAL = "doc_rule.local.yaml"
# 규칙의 말 칸. clear/add/remove 가 가리킬 수 있는 칸이다.
CELLS = ("title_terms", "head_terms", "terms", "filename", "exclude")
# 규칙마다 덮을 수 있는 숫자 칸.
NUM_FIELDS = ("head_chars", "min_count", "min_distinct")
# 본보기에서 가져와 local 의 settings 로 덮는 전체 설정 칸.
SETTING_SECTIONS = ("conflict", "defaults", "signals", "embed", "bm25")
WEIGHTS = ("high", "medium", "low")
# 생성 표시 — 이 칸이 있으면 엔진은 '자동 생성 규칙 파일'로 읽는다.
GENERATED_KEY = "generated"
GENERATED_BY = "MpowerClassify --build-doc-rule"

# 조정 파일(doc_rule.local.yaml) 맨 위 안내. 화면이 저장할 때마다 통째로 다시 쓰므로
# 주석은 남지 않는다 — 조정마다 이유는 why 칸에 적는다. 이관 도구 초안도 이 글을 쓴다.
LOCAL_HEADER = (
    "# 회사별 업무분류 조정 — 사람이 고치는 유일한 업무분류 파일.\n"
    "# doc_rule.yaml 은 이 파일·체계 JSON·유의어 사전으로 자동으로 만들어진다(--build-doc-rule).\n"
    "# 화면(설정 ② 판단 기준 › 업무분류)이 저장할 때마다 다시 쓰므로 주석은 남지 않는다.\n"
    "# 조정마다 이유는 why 칸에 적는다.\n"
    "#\n"
    "# [무엇을 적나] 말 칸(title_terms·head_terms·terms·filename·exclude)을 통째로 적지 않는다.\n"
    "#   자동으로 채워진 말을 '어떻게 바꿀지'만 적는다.\n"
    "#     add    = 자동으로 채워진 말에 더하기\n"
    "#     remove = 자동 말에서 빼기\n"
    "#     clear  = 그 칸을 통째로 비우기(제품 분류에 쓰던 처방 — 본문에 제품 이름이\n"
    "#              스쳐도 그 분류가 붙는 것을 막는다)\n"
    "#   말 목록은 doc_rule.yaml 과 같은 한 줄 형식([\"가\", \"나\"])으로 쓴다.\n"
    "#\n"
    "# [본보기]\n"
    "# rules:\n"
    "#   DC_002_006:                          # 프로그래밍 강좌\n"
    "#     title_at_decision: 프로그래밍 강좌   # 적어 두면 분류 이름이 바뀔 때 알려 준다\n"
    "#     add: {title_terms: [\"포인터\", \"배열\", \"함수\"], head_terms: [\"강좌\", \"프로그램\"],\n"
    "#           terms: [\"program\", \"프로그래밍\"], filename: [\"장. \"]}\n"
    "#     remove: {terms: [\"강좌\"]}\n"
    "#     why: 강의 교재 파일 이름에 분류 이름이 안 들어간다\n"
    "#   DC_002_003:                          # 엠파워 보안파일서버\n"
    "#     clear: [head_terms, terms]         # 제목·파일명으로만 판단한다\n"
    "#     why: 견적서 본문에 제품 이름이 나와 제품 분류로 잘못 붙었다\n"
    "#\n"
    "#   ※ 같은 칸에 add 와 clear 를 함께 쓰면 비우기가 먼저다 — 비운 뒤 더한 말만 남는다.\n"
)

# 만들어진 파일 맨 위 안내. Rust 판(docbuild.rs)과 글자까지 같아야 한다.
GEN_HEADER = (
    "# ────────────────────────────────────────────────────────────────\n"
    "# 자동 생성 파일 — 고치지 마세요. 다시 만들면 고친 것이 사라집니다.\n"
    "# 회사별 조정은 doc_rule.local.yaml(분류별 조정·설정)과\n"
    "# synonyms/doc_synonyms.local.yaml(회사 말)에 적고 다시 만드세요.\n"
    "# 다시 만들기: MpowerClassify --build-doc-rule\n"
    "# ────────────────────────────────────────────────────────────────\n"
)


#------------------------------------------------------------------
# local 조정 파일 검증 실패
#=> doc_rule.local.yaml 에 틀린 칸이 있으면 규칙을 만들지 않는다. 틀린 조정을
#   조용히 버리면 관리자는 적용됐다고 믿는데 규칙은 기본값으로 나간다.
#
# -필드: path       = 파일 경로
# -필드: violations = [{code, where, value, detail}] 목록
#------------------------------------------------------------------
class RuleLocalValidationError(Exception):

    #------------------------------------------------------------------
    # 생성자
    #=> 위반 목록을 사람이 읽는 보고문으로 바꿔 예외 메시지로 쓴다.
    #
    # -in: path       = 파일 경로
    # -in: violations = 위반 dict 목록
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def __init__(self, path, violations):
        self.path = path
        self.violations = list(violations)
        lines = [f"업무분류 조정 파일 검증 실패: {path} ({len(self.violations)}건)"]
        for v in self.violations:
            lines.append(f"  [{v['code']}] {v['where']} = {v['value']!r} — {v['detail']}")
        super().__init__("\n".join(lines))


#------------------------------------------------------------------
# 위반 한 건 만들기
#
# -in: code   = 위반 코드("L0"~"L6")
# -in: where  = 어느 칸인지(예: "rules.DC_007.clear")
# -in: value  = 문제의 값
# -in: detail = 사람이 읽을 설명
#
# -out: dict = 위반 한 건
# -out: error = 없음
#------------------------------------------------------------------
def _lv(code, where, value, detail):
    return {"code": code, "where": where, "value": value, "detail": detail}


#------------------------------------------------------------------
# 문자열 목록인가
#
# -in: v = 임의 값
#
# -out: bool = 문자열만 든 list 면 True
# -out: error = 없음
#------------------------------------------------------------------
def _is_str_list(v):
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


#------------------------------------------------------------------
# doc_rule.local.yaml 내용 검증
#=> 모르는 칸·틀린 모양을 전부 모은다(첫 오류에서 멈추지 않는다).
#    · L0 최상위가 매핑이 아니거나 모르는 칸
#    · L1 industry 모양
#    · L2 settings 모양(값의 뜻은 만든 규칙을 엔진 검증기로 다시 본다)
#    · L3 rules.<dc_id> 의 모르는 칸·틀린 모양
#    · L4 clear/add/remove 가 모르는 말 칸을 가리킴
#    · L5 weight·숫자 칸 값
#    · L6 head 칸 모양
#
# -in: data = yaml.safe_load 결과
#
# -out: list = 위반 dict 목록(없으면 빈 목록)
# -out: error = 없음
#------------------------------------------------------------------
def validate_rule_local(data):
    out = []
    if data is None:
        return out
    if not isinstance(data, dict):
        return [_lv("L0", "(최상위)", data, "매핑(mapping)이어야 합니다")]
    known = {"version", "industry", "settings", "rules", "head"}
    for k in data:
        if k not in known:
            out.append(_lv("L0", str(k), data[k],
                           f"모르는 칸입니다(쓸 수 있는 칸: {', '.join(sorted(known))})"))

    ind = data.get("industry")
    if ind is not None and not (isinstance(ind, str) or _is_str_list(ind)):
        out.append(_lv("L1", "industry", ind, "업종 이름 하나(문자열) 또는 목록이어야 합니다"))

    st = data.get("settings")
    if st is not None:
        if not isinstance(st, dict):
            out.append(_lv("L2", "settings", st, "매핑이어야 합니다"))
        else:
            for k, v in st.items():
                if k not in SETTING_SECTIONS:
                    out.append(_lv("L2", f"settings.{k}", v,
                                   f"모르는 칸입니다(쓸 수 있는 칸: {', '.join(SETTING_SECTIONS)})"))
                elif k != "conflict" and not isinstance(v, dict):
                    out.append(_lv("L2", f"settings.{k}", v, "매핑이어야 합니다"))

    rules = data.get("rules")
    if rules is not None and not isinstance(rules, dict):
        out.append(_lv("L3", "rules", rules, "dc_id 를 열쇠로 하는 매핑이어야 합니다"))
        rules = {}
    rule_keys = {"title_at_decision", "enabled", "weight", "clear", "add", "remove", "why"} \
        | set(NUM_FIELDS)
    for dc_id, r in (rules or {}).items():
        where = f"rules.{dc_id}"
        if not isinstance(r, dict):
            out.append(_lv("L3", where, r, "매핑이어야 합니다"))
            continue
        for k, v in r.items():
            if k not in rule_keys:
                out.append(_lv("L3", f"{where}.{k}", v,
                               f"모르는 칸입니다(쓸 수 있는 칸: {', '.join(sorted(rule_keys))})"))
        if "enabled" in r and not isinstance(r["enabled"], bool):
            out.append(_lv("L3", f"{where}.enabled", r["enabled"], "true 또는 false 여야 합니다"))
        for k in ("title_at_decision", "why"):
            if k in r and not isinstance(r[k], str):
                out.append(_lv("L3", f"{where}.{k}", r[k], "문자열이어야 합니다"))
        if "weight" in r and r["weight"] not in WEIGHTS:
            out.append(_lv("L5", f"{where}.weight", r["weight"], "high · medium · low 중 하나"))
        for k in NUM_FIELDS:
            if k in r:
                v = r[k]
                if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                    out.append(_lv("L5", f"{where}.{k}", v, "1 이상의 정수여야 합니다"))
        if "clear" in r:
            c = r["clear"]
            if not _is_str_list(c):
                out.append(_lv("L4", f"{where}.clear", c, "말 칸 이름의 목록이어야 합니다"))
            else:
                for cell in c:
                    if cell not in CELLS:
                        out.append(_lv("L4", f"{where}.clear", cell,
                                       f"모르는 말 칸입니다({', '.join(CELLS)})"))
        for op in ("add", "remove"):
            if op not in r:
                continue
            m = r[op]
            if not isinstance(m, dict):
                out.append(_lv("L4", f"{where}.{op}", m, "칸 이름 → 말 목록 매핑이어야 합니다"))
                continue
            for cell, words in m.items():
                if cell not in CELLS:
                    out.append(_lv("L4", f"{where}.{op}.{cell}", words,
                                   f"모르는 말 칸입니다({', '.join(CELLS)})"))
                elif not _is_str_list(words):
                    out.append(_lv("L4", f"{where}.{op}.{cell}", words, "말(문자열) 목록이어야 합니다"))

    head = data.get("head")
    if head is not None:
        if not isinstance(head, dict):
            out.append(_lv("L6", "head", head, "매핑이어야 합니다"))
        else:
            for k, v in head.items():
                if k not in ("title_exclude", "node_off"):
                    out.append(_lv("L6", f"head.{k}", v, "모르는 칸입니다(title_exclude · node_off)"))
            te = head.get("title_exclude")
            if te is not None and not _is_str_list(te):
                out.append(_lv("L6", "head.title_exclude", te, "말(문자열) 목록이어야 합니다"))
            no = head.get("node_off")
            if no is not None:
                ok = isinstance(no, list) and all(
                    isinstance(x, dict) and isinstance(x.get("node"), str) for x in no)
                if not ok:
                    out.append(_lv("L6", "head.node_off", no,
                                   "[{node: dc_id, title_at_decision: 제목}] 목록이어야 합니다"))
    return out


#------------------------------------------------------------------
# doc_rule.local.yaml 읽기
#=> 파일이 없으면 빈 조정(= 제품 기본값 그대로)이다. 있는데 틀리면 멈춘다.
#
# -in: path = 조정 파일 경로
#
# -out: dict = 조정 내용(없으면 {})
# -out: error = YAML 이 깨졌거나 검증 위반이면 RuleLocalValidationError
#------------------------------------------------------------------
def load_rule_local(path):
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise RuleLocalValidationError(path, [_lv("L0", "(파일)", "", f"YAML 로 읽히지 않습니다: {e}")])
    violations = validate_rule_local(data)
    if violations:
        raise RuleLocalValidationError(path, violations)
    return data or {}


#------------------------------------------------------------------
# 파일 지문
#=> 줄끝(CRLF/LF)과 UTF-8 BOM 을 맞춘 뒤의 sha256. 같은 내용을 윈도우·리눅스에서
#   저장해도 같은 지문이 나와야 '입력이 바뀌었나'를 헛경고 없이 가릴 수 있다.
#
# -in: path = 파일 경로
#
# -out: str = 16진 지문(파일이 없으면 None)
# -out: error = 없음
#------------------------------------------------------------------
def file_digest(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


#------------------------------------------------------------------
# 규칙 본문 지문
#=> generated 칸을 뺀 나머지를 키 정렬·공백 없는 JSON 으로 바꿔 sha256 을 낸다.
#   만들 때 적어 두고, 실행 때 다시 계산해 다르면 '손으로 고친 흔적'으로 알린다.
#   Rust 판도 같은 규칙으로 계산해야 한다(키 정렬 · ensure_ascii 끔 · 구분자 , :).
#
# -in: doc = 규칙 dict
#
# -out: str = 16진 지문
# -out: error = 없음
#------------------------------------------------------------------
def body_digest(doc):
    body = {k: v for k, v in doc.items() if k != GENERATED_KEY}
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


#------------------------------------------------------------------
# 정책 폴더 안의 상대경로(없으면 파일 이름)
#=> 생성 기록에는 사람이 읽을 짧은 이름을 남긴다. 폴더 밖 파일이면 이름만 남긴다.
#
# -in: path       = 파일 경로
# -in: policy_dir = 정책 폴더
#
# -out: str = "synonyms/doc_synonyms.core.yaml" 꼴('/' 구분)
# -out: error = 없음
#------------------------------------------------------------------
def _rel(path, policy_dir):
    ap, pd = os.path.abspath(path), os.path.abspath(policy_dir)
    try:
        rel = os.path.relpath(ap, pd)
    except ValueError:            # 드라이브가 다르면 상대경로가 없다
        return os.path.basename(ap)
    if rel.startswith(".."):
        return os.path.basename(ap)
    return rel.replace(os.sep, "/")


#------------------------------------------------------------------
# YAML 파일의 version 칸
#
# -in: path = 파일 경로
#
# -out: str|None = version 값(없거나 못 읽으면 None)
# -out: error = 없음
#------------------------------------------------------------------
def _version_of(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return None
    v = data.get("version") if isinstance(data, dict) else None
    return None if v is None else str(v)


#------------------------------------------------------------------
# 한 겹씩 덮기(묶음은 한 겹 안까지)
#=> 본보기 설정 위에 local 의 settings 를 얹는다. defaults 처럼 묶음이면 적은
#   항목만 덮고, signals.title 처럼 두 겹이면 그 안까지 덮는다.
#
# -in: base = 본보기 값
# -in: over = local 값
#
# -out: 새 값(입력은 바꾸지 않는다)
# -out: error = 없음
#------------------------------------------------------------------
def _overlay(base, over):
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _overlay(base.get(k), v) if k in base else v
        return out
    return over


#------------------------------------------------------------------
# 규칙 한 줄에 local 조정 덧씌우기
#=> 자동으로 만든 말 위에 사람의 결정을 얹는다. 순서는 clear → remove → add.
#   clear 한 칸에도 add 는 들어간다(비운 뒤 사람이 고른 말만 두는 경우).
#    1) clear: 칸을 비운다(예: P2 — 자사 제품은 앞부분·본문을 보지 않는다)
#    2) remove: 그 칸에서 말을 뺀다. 없는 말이면 경고(사전이 바뀌어 이미 빠짐)
#    3) add: 없는 말만 뒤에 붙인다
#    4) weight·숫자 칸을 덮는다
#
# -in: rule  = 규칙 dict(제자리에서 고친다)
# -in: adj   = rules.<dc_id> 조정 dict
# -in: warns = 경고를 모을 목록
#
# -out: list = 이 규칙에 적용된 조정 표지(예: ["clear:terms", "add:title_terms"])
# -out: error = 없음
#------------------------------------------------------------------
def _apply_adjust(rule, adj, warns):
    marks = []
    node = rule["node"]
    for cell in adj.get("clear") or []:
        rule[cell] = []
        marks.append(f"clear:{cell}")
    for cell in CELLS:
        words = (adj.get("remove") or {}).get(cell)
        if not words:
            continue
        have = rule.get(cell) or []
        gone = [w for w in words if w not in have]
        for w in gone:
            warns.append(f"[local] {node}.remove.{cell}: '{w}' 는 이미 규칙에 없습니다 "
                         f"— 지워도 되는 조정일 수 있습니다")
        rule[cell] = [w for w in have if w not in words]
        marks.append(f"remove:{cell}")
    for cell in CELLS:
        words = (adj.get("add") or {}).get(cell)
        if not words:
            continue
        have = list(rule.get(cell) or [])
        have += [w for w in words if w not in have]
        rule[cell] = have
        marks.append(f"add:{cell}")
    if "weight" in adj:
        rule["weight"] = adj["weight"]
        marks.append("weight")
    for k in NUM_FIELDS:
        if k in adj:
            rule[k] = adj[k]
            marks.append(k)
    return marks


#------------------------------------------------------------------
# 규칙 dict 만들기 — 설계서 8장 순서
#=> 체계 JSON · 사전 · 본보기 · local 조정으로 doc_rule.yaml 의 내용을 만든다.
#   파일은 쓰지 않는다(dump_doc_rule·write_doc_rule 의 몫).
#    1) 체계 JSON → 분류체계(검증 포함). doc_taxonomy.yaml 을 거치지 않는다
#    2) 업종은 local 조정의 industry(없으면 본보기) → 사전 3겹을 얹는다
#    3) 규칙을 만들 분류(꺼 둔 분류·서랍 제외, 전체경로 순)마다 기본 말을 만든다
#    4) local 조정(clear → remove → add · weight · 숫자 칸 · enabled)을 덧씌운다
#    5) 핵어를 계산해 규칙마다 적고, 잡음 꼬리 목록도 적는다
#    6) 본보기 설정 위에 local settings 를 덮는다
#    7) 입력 지문·본문 지문·판(version)을 적는다
#
# -in: export_path = 체계 JSON(doc_classification_export.json) 경로
# -in: policy_dir  = 정책 폴더(사전·본보기·local 조정을 여기서 찾는다)
# -in: local       = 조정 dict 를 직접 줄 때(화면 미리보기용). None 이면 파일을 읽는다.
#                    직접 줘도 검증은 똑같이 한다
#
# -out: (doc, warnings) = 규칙 dict, 사람에게 보여 줄 경고 문자열 목록
# -out: error = 체계 JSON 없음 FileNotFoundError · 모양 오류 ValueError ·
#               체계 검증 실패 TaxonomyValidationError · 조정 파일 오류 RuleLocalValidationError
#------------------------------------------------------------------
def build_doc_rule(export_path, policy_dir, local=None):
    warns = []
    policy_dir = os.path.abspath(policy_dir)
    rules_path = os.path.join(policy_dir, "doc_rule.yaml")

    # 1) 체계 JSON 을 바로 분류체계로 — 스냅샷 파일을 만들지 않는다.
    with open(export_path, encoding="utf-8-sig") as f:
        raw = json.load(f)
    snapshot, conv_warns = AX.convert_mpower_json(raw)
    warns += [f"[체계] {w}" for w in conv_warns]
    taxonomy = AX.taxonomy_from_snapshot(snapshot, export_path)

    local_path = os.path.join(policy_dir, RULE_LOCAL)
    if local is None:
        local = load_rule_local(local_path)
    else:
        # 화면이 저장 전에 미리 보는 조정도 파일과 같은 기준으로 막는다.
        violations = validate_rule_local(local)
        if violations:
            raise RuleLocalValidationError("(화면 조정)", violations)
    tpl_path = os.path.join(policy_dir, "doc_rule_template.yaml")
    tpl = {}
    if os.path.isfile(tpl_path):
        with open(tpl_path, encoding="utf-8") as f:
            tpl = yaml.safe_load(f) or {}

    # 2) 업종 — 회사가 정한 것(local)이 본보기보다 앞선다.
    industry = DV.clean_industry(local.get("industry") if "industry" in local
                                 else tpl.get("industry"))
    syn = DV.load_synonyms(rules_path, industry=industry)

    new_rule = tpl.get("new_rule") if isinstance(tpl.get("new_rule"), dict) else {}
    weight = new_rule.get("weight", "medium")

    adjust = local.get("rules") or {}
    head_cfg = local.get("head") or {}
    nodes = DV.nodes_from_taxonomy(taxonomy)
    titles = {n["dc_id"]: n["title"] for n in nodes}

    # local 조정이 가리키는 분류가 지금 체계에 있는지·이름이 바뀌었는지 먼저 알린다.
    for dc_id, adj in adjust.items():
        if dc_id not in titles:
            warns.append(f"[local] rules.{dc_id}: 규칙을 만드는 분류에 없습니다(삭제·꺼 둔 분류·"
                         f"대분류·오타) — 이 조정은 쓰이지 않습니다")
            continue
        was = str(adj.get("title_at_decision") or "").strip()
        if was and was.replace(" ", "") != titles[dc_id].replace(" ", ""):
            warns.append(f"[local] rules.{dc_id}: 조정할 때 이름은 '{was}', 지금 이름은 "
                         f"'{titles[dc_id]}' 입니다 — 이 조정이 계속 맞는지 확인하세요")

    # 5) 핵어 — 판정 때 하던 계산을 만들 때 한 번 한다(결과를 규칙에 적는다).
    lexicon, head_warns = taxhead.build_head_lexicon(
        taxonomy, syn, head_cfg.get("title_exclude") or (), head_cfg.get("node_off") or ())
    warns += head_warns

    rules = []
    for n in nodes:
        dc_id, title = n["dc_id"], n["title"]
        adj = adjust.get(dc_id) or {}
        # 회사가 쓰지 않는 분류는 규칙을 만들지 않는다(체계에서도 사라진 것처럼 된다).
        if adj.get("enabled") is False:
            warns.append(f"[local] {dc_id}({title}): enabled=false — 규칙을 만들지 않습니다")
            continue
        vocab = DV.rule_vocab(title, syn=syn)
        rule = {
            "id": f"dt_{dc_id.lower()}",
            "node": dc_id,
            "title": title,
            "path": taxonomy.path(dc_id),
            "path_ids": list(taxonomy.path_ids(dc_id)),
            "weight": weight,
        }
        for cell in CELLS:
            rule[cell] = list(vocab.get(cell) or [])
        marks = _apply_adjust(rule, adj, warns)
        rule["heads"] = list((lexicon.get(dc_id) or {}).get("heads") or [])
        if marks:
            rule["local"] = marks
        rules.append(_order_rule(rule))

    # 6) 설정 — 본보기 기본값 위에 회사 설정.
    settings = local.get("settings") or {}
    doc = {"version": "", GENERATED_KEY: {}}
    for sec in SETTING_SECTIONS:
        base = tpl.get(sec)
        val = _overlay(base, settings[sec]) if sec in settings else base
        if sec == "conflict" and val is None:
            val = "all"
        if val is not None:
            doc[sec] = val
    doc["noise_tails"] = list(taxhead.noise_tails(syn))
    doc["doctype_rules"] = rules

    # 7) 입력 지문 — 실행 때 '입력이 바뀌었는데 다시 안 만들었나'를 가리는 기준.
    inputs = [{"file": _rel(export_path, policy_dir), "sha256": file_digest(export_path)}]
    for p in (syn or {}).get("layers") or []:
        inputs.append(_input_entry(p, policy_dir))
    for p in (tpl_path, local_path):
        if os.path.isfile(p):
            inputs.append(_input_entry(p, policy_dir))
    seed = json.dumps([inputs, industry], ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    doc["version"] = "doctype-gen-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    doc[GENERATED_KEY] = {"by": GENERATED_BY, "industry": industry, "inputs": inputs,
                          "body_sha256": ""}
    doc[GENERATED_KEY]["body_sha256"] = body_digest(doc)
    return doc, warns


#------------------------------------------------------------------
# 규칙 한 줄의 말 칸 차이 → 조정(clear·add·remove)
#=> 자동값(base)과 사람이 고친 값(new)을 칸마다 견준다. 화면 저장·단어 제안·옮기기
#   도구가 같은 함수를 쓴다 — 셋이 다르게 옮기면 같은 결정이 다르게 적힌다.
#    1) 같거나 차례만 다르면 조정 없음(판정에 영향 없음)
#    2) 새 칸이 비었고 자동값에 말이 있으면 clear
#    3) 그 밖에는 자동값에만 있는 말은 remove, 새 칸에만 있는 말은 add
#
# -in: base = 자동값 규칙 dict(local 조정 없이 만든 것)
# -in: new  = 사람이 고친 규칙 dict
#
# -out: dict = {"clear": [...], "remove": {칸: [...]}, "add": {칸: [...]}} 중 있는 것만
# -out: error = 없음
#------------------------------------------------------------------
def diff_cells(base, new):
    adj = {}
    for cell in CELLS:
        a = list((base or {}).get(cell) or [])
        b = list((new or {}).get(cell) or [])
        if a == b or set(a) == set(b):
            continue
        if not b and a:
            adj.setdefault("clear", []).append(cell)
            continue
        rm = [w for w in a if w not in b]
        add = [w for w in b if w not in a]
        if rm:
            adj.setdefault("remove", {})[cell] = rm
        if add:
            adj.setdefault("add", {})[cell] = add
    return adj


#------------------------------------------------------------------
# 옛 doc_rule.yaml → 조정(doc_rule.local.yaml) 초안
#=> 사람이 고쳐 온 옛 규칙 파일에서 '자동값과 다른 부분'만 조정으로 뽑는다.
#   화면의 [새 방식으로 바꾸기]와 scripts/migrate_doc_rule_local.py 가 같은 함수를 쓴다.
#    1) 옛 파일의 업종으로, 조정 없이 규칙을 한 번 만든다(업종이 다르면 헛차이가 난다)
#    2) 규칙마다 말 칸 차이(diff_cells)·weight·숫자 칸 차이를 조정으로 적는다
#    3) conflict·defaults·signals·embed 가 다르면 settings 로, 핵어 끄기는 head 로
#    4) 초안을 얹어 다시 만들어 옛 규칙과 말 목록이 같은지 확인한다
#
# -in: old_doc     = 옛 doc_rule.yaml 내용(dict)
# -in: export_path = 체계 JSON 경로
# -in: policy_dir  = 정책 폴더(사전·본보기)
# -in: why         = 조정마다 적을 이유 문구
#
# -out: (local, warns, gaps) = 조정 초안 dict, 경고 목록,
#        초안으로 다시 만들어도 옛 규칙과 다른 칸 목록("DC_x.terms" 꼴 — 비어야 정상)
# -out: error = build_doc_rule 과 같음
#------------------------------------------------------------------
def migrate_from_old(old_doc, export_path, policy_dir, why="옛 규칙에서 옮겨 옴"):
    old_doc = old_doc or {}
    warns = []
    local = {"version": "migrated"}
    if old_doc.get("industry"):
        local["industry"] = old_doc["industry"]
    new_doc, _ = build_doc_rule(export_path, policy_dir,
                                local={k: v for k, v in local.items() if k == "industry"})
    settings = {}
    for sec in SETTING_SECTIONS:
        if sec in old_doc and old_doc.get(sec) != new_doc.get(sec):
            settings[sec] = old_doc[sec]
    if settings:
        local["settings"] = settings
    new_by = {r["node"]: r for r in new_doc.get("doctype_rules") or []}
    rules = {}
    for r in old_doc.get("doctype_rules") or []:
        node = r.get("node")
        if node not in new_by:
            warns.append(f"옛 규칙 {node} 는 새 규칙에 없습니다(체계에서 빠졌거나 꺼진 분류) — 옮기지 않습니다")
            continue
        # 새 규칙이 자동값, 옛 규칙이 사람이 고친 값이다.
        adj = diff_cells(new_by[node], r)
        if r.get("weight") and r.get("weight") != new_by[node].get("weight"):
            adj["weight"] = r["weight"]
        for k in NUM_FIELDS:
            if r.get(k) is not None and r.get(k) != new_by[node].get(k):
                adj[k] = r[k]
        if adj:
            rules[node] = {"title_at_decision": new_by[node].get("title", ""), **adj, "why": why}
    if rules:
        local["rules"] = rules
    head = {}
    if old_doc.get("taxonomy_title_exclude"):
        head["title_exclude"] = list(old_doc["taxonomy_title_exclude"])
    if old_doc.get("taxonomy_node_off"):
        head["node_off"] = list(old_doc["taxonomy_node_off"])
    if head:
        local["head"] = head

    # 초안으로 다시 만들어 옛 규칙과 같은지(차례 무시) 본다.
    again, _ = build_doc_rule(export_path, policy_dir, local=local)
    by_again = {r["node"]: r for r in again["doctype_rules"]}
    gaps = []
    for r in old_doc.get("doctype_rules") or []:
        other = by_again.get(r.get("node"))
        if other is None:
            continue
        for cell in CELLS:
            if set(r.get(cell) or []) != set(other.get(cell) or []):
                gaps.append(f"{r['node']}.{cell}")
    return local, warns, gaps


#------------------------------------------------------------------
# 입력 파일 기록 한 줄
#
# -in: path       = 파일 경로
# -in: policy_dir = 정책 폴더
#
# -out: dict = {file, [version,] sha256}
# -out: error = 없음
#------------------------------------------------------------------
def _input_entry(path, policy_dir):
    e = {"file": _rel(path, policy_dir)}
    v = _version_of(path)
    if v is not None:
        e["version"] = v
    e["sha256"] = file_digest(path)
    return e


# 규칙 한 줄의 칸 차례 — 사람이 읽을 때 '어느 분류의 어떤 규칙인가'를 먼저 보게.
_RULE_ORDER = ("id", "node", "title", "path", "path_ids", "weight") + NUM_FIELDS \
    + CELLS + ("heads", "local")


#------------------------------------------------------------------
# 규칙 dict 칸 차례 맞추기
#
# -in: rule = 규칙 dict
#
# -out: dict = _RULE_ORDER 차례로 다시 담은 dict(없는 칸은 뺀다)
# -out: error = 없음
#------------------------------------------------------------------
def _order_rule(rule):
    return {k: rule[k] for k in _RULE_ORDER if k in rule}


#------------------------------------------------------------------
# 값 하나 → JSON 조각(YAML 흐름 표기로도 읽힌다)
#=> 문자열·숫자·목록을 JSON 으로 적으면 YAML 로도 그대로 읽히고, Python·Rust 가
#   같은 글자를 낸다(PyYAML·serde_yaml 의 따옴표·줄바꿈 규칙 차이를 피한다).
#
# -in: v = 문자열·숫자·불리언·None·목록·매핑
#
# -out: str = 한 줄 JSON
# -out: error = 없음
#------------------------------------------------------------------
def _j(v):
    return json.dumps(v, ensure_ascii=False, separators=(", ", ": "))


#------------------------------------------------------------------
# 매핑 → YAML 줄들(결정적)
#=> 매핑은 블록으로, 그 밖의 값은 한 줄 JSON 으로 적는다. 매핑 목록
#   (doctype_rules · inputs)은 '- ' 로 시작하는 블록 항목으로 적는다.
#
# -in: m      = dict
# -in: indent = 들여쓰기 칸 수
#
# -out: list[str] = 줄 목록(줄끝 없음)
# -out: error = 없음
#------------------------------------------------------------------
def _emit_map(m, indent):
    pad = " " * indent
    lines = []
    for k, v in m.items():
        if isinstance(v, dict) and v:
            lines.append(f"{pad}{k}:")
            lines += _emit_map(v, indent + 2)
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            lines.append(f"{pad}{k}:")
            for item in v:
                sub = _emit_map(item, indent + 2)
                # 첫 줄의 들여쓰기 자리에 '- ' 를 넣는다.
                sub[0] = f"{pad}- " + sub[0][indent + 2:]
                lines += sub
        else:
            lines.append(f"{pad}{k}: {_j(v)}")
    return lines


#------------------------------------------------------------------
# 규칙 dict → 파일 텍스트
#
# -in: doc = build_doc_rule() 결과
#
# -out: str = 머리 안내 + 본문(줄끝 LF, 끝에 개행 하나)
# -out: error = 없음
#------------------------------------------------------------------
def dump_doc_rule(doc):
    return GEN_HEADER + "\n".join(_emit_map(doc, 0)) + "\n"


#------------------------------------------------------------------
# 규칙 파일 쓰기(백업 남김)
#=> 이미 있으면 .bak 으로 한 벌 남긴다 — 옮겨 가는 첫 생성에서 손으로 고친
#   옛 규칙 파일을 잃지 않게.
#
# -in: path = 쓸 경로
# -in: doc  = 규칙 dict
#
# -out: 없음
# -out: error = 쓰기 실패 시 OSError 전파
#------------------------------------------------------------------
def write_doc_rule(path, doc):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    if os.path.isfile(path):
        with open(path, "rb") as f:
            old = f.read()
        with open(path + ".bak", "wb") as f:
            f.write(old)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(dump_doc_rule(doc))


#------------------------------------------------------------------
# 자동 생성 규칙 파일인가
#
# -in: data = yaml.safe_load 결과
#
# -out: bool = generated 칸이 매핑이면 True
# -out: error = 없음
#------------------------------------------------------------------
def is_generated(data):
    return isinstance(data, dict) and isinstance(data.get(GENERATED_KEY), dict)


#------------------------------------------------------------------
# 실행 때 생성 규칙 점검 — 경고만(판정은 계속)
#=> 두 가지를 본다.
#    1) 손으로 고친 흔적 — 본문 지문이 만들 때와 다르면 "다음 생성 때 사라집니다"
#    2) 입력이 바뀜 — 기록된 입력 파일의 지금 지문이 다르면 "다시 만드세요".
#       체계 JSON 은 연동 때 잠깐 두는 파일일 수 있어 없으면 조용히 넘긴다.
#       다른 입력이 없어졌으면 알린다
#
# -in: data       = yaml.safe_load 결과(생성 규칙)
# -in: rules_path = 규칙 파일 경로(입력 파일은 이 폴더 기준으로 찾는다)
#
# -out: list[str] = 경고 문자열 목록
# -out: error = 없음
#------------------------------------------------------------------
def check_generated(data, rules_path):
    warns = []
    gen = data.get(GENERATED_KEY) or {}
    want = gen.get("body_sha256")
    if want and body_digest(data) != want:
        warns.append("doc_rule.yaml 이 만들어진 뒤 손으로 고쳐졌습니다 — 다음에 다시 만들면 "
                     "고친 것이 사라집니다. 조정은 doc_rule.local.yaml 에 적으세요")
    base = os.path.dirname(os.path.abspath(rules_path))
    stale = []
    for i, e in enumerate(gen.get("inputs") or []):
        if not isinstance(e, dict) or not e.get("file"):
            continue
        p = os.path.join(base, *str(e["file"]).split("/"))
        now = file_digest(p)
        if now is None:
            # 첫 줄은 체계 JSON — 연동 임시 파일일 수 있다.
            if i > 0:
                stale.append(f"{e['file']}(없어짐)")
            continue
        if now != e.get("sha256"):
            stale.append(str(e["file"]))
    if stale:
        warns.append("규칙을 만든 뒤 입력이 바뀌었습니다: " + ", ".join(stale)
                     + " — --build-doc-rule 로 다시 만드세요")
    return warns
