#------------------------------------------------------------------
# 기준 문서 저장소 관리 (class_seed.jsonl) — 순수 로직 · 스키마 v3
#=> 분류 검토 UI 가 쓰는 "큐레이션된 기준 문서 저장소"의 입출력·편집·감사를 담당한다.
#   UI(Streamlit) 위젯과 분리해, 이 모듈만 단위테스트할 수 있게 한다.
#    - class_seed.jsonl      : 현재 기준 문서 상태(추가/수정/삭제로 갱신, 통째 저장)
#    - class_seed_audit.jsonl: 변경 이력(append-only, 누가·언제·어느 축을·무엇에서 무엇으로)
#
#   [스키마 v3 — 평평한 한 줄] 설계서 '기준문서-seed저장소-v3평탄화-설계-20260908'
#   한 문서 = 한 줄 = 벡터 하나다. 그 한 줄이 두 축을 나눠 쓴다.
#     {v, file, doc_id, hash, grade, doctype[], source, approved_by, note,
#      ts, dim, engine?, hold?, vector[]}
#
#   [v2 에서 무엇이 바뀌었나] v2 는 진실을 axes 안에 3겹으로 넣고 grade·labels 를
#   파생값으로 만들어 냈다. 같은 등급이 세 곳에 적혀, 저장 직전에 반드시
#   project() 를 통과해야만 앞뒤가 맞는 형식이었다. CLI·웹 같은 다른 프로세스가
#   이 파일을 쓰기 시작하면 그 규율을 강제할 방법이 없다. 그래서 폈다.
#     axes.security.value  → grade
#     axes.doctype.value   → doctype        (labels.doctype 이 아니다)
#     axes.*.state=suspect → hold[칸]       (값 보존, 엔진에는 안 보인다)
#     axes.*.state=retired → 지운다          (이전 값은 감사 로그에 남는다)
#     doc.size / doc.mtime → 뺐다            (읽는 코드가 없었다)
#
#   엔진(Python·Rust 의 SeedIndex/DoctypeSeedIndex)은 grade 와 doctype 만 읽는다.
#   그래서 이 두 칸의 '모양'은 절대 바꾸지 않는다 — doctype 을 배열이 아닌 것으로
#   바꾸면 Rust 가 그 줄을 에러 없이 통째로 건너뛴다(기준 문서가 조용히 사라진다).
#------------------------------------------------------------------

import json
import os
import time
import threading
import datetime

import numpy as np


# 저장 파일의 스키마 판. 행마다 v 로 적어 둔다(없으면 옛 행으로 보고 자동 승격).
SCHEMA_VERSION = 3

# load_seeds 가 건너뛴 줄(모르는 미래 판·깨진 줄). 화면이 이것을 읽어 사람에게
# 알린다 — 조용히 사라지면 "기준 문서가 왜 줄었지"를 아무도 알 수 없다.
SKIPPED = []

# 이 저장소가 다루는 두 축. 순서는 화면 표기 순서이기도 하다.
AXES = ("security", "doctype")
AXIS_LABEL = {"security": "보안등급", "doctype": "업무분류"}

# 축의 상태값 3가지.
#   active  = 관리자가 확정한 기준 → 전파에 쓰인다
#   retired = 관리자가 이 축을 해제 → 안 쓰인다(이력만 남는다)
#   suspect = 시스템이 의심 표시(원본 변경·원본 없음·유령 분류코드) → 안 쓰인다
STATE_ACTIVE = "active"
STATE_RETIRED = "retired"
STATE_SUSPECT = "suspect"

# 기준 문서를 '등록'할 때 사본이라고 경고하는 문턱값.
# 엔진 전파의 사본 상속 문턱(0.950)과는 다른 판단이라 값이 달라도 틀린 게 아니다.
# 이름을 갈라 둔 이유가 그것이다(propagate 쪽은 PROPAGATE_DUP_THRESHOLD).
SEED_DUP_THRESHOLD = 0.985

# 두 문서가 사실상 같은데 등급이 서로 다른지 볼 때 쓰는 문턱값(점검 화면).
CONFLICT_SIM = 0.97

# 이 도구가 파일에 적는 모든 시각의 표기. 2026-09-10 에 ISO-8601(예:
# "2026-09-08T17:10:56+09:00")에서 사람이 읽는 모양으로 바꿨다.
#   · 'T' 와 시간대 오프셋(+09:00)은 기계가 읽을 때만 쓸모가 있는데, 이 값을
#     파싱하는 코드가 두 판 어디에도 없었다(전부 '보여 주기'와 '문자열 정렬'용).
#   · 문자열로 정렬해도 시간 순서가 유지되는 모양이라 max()/sort 가 그대로 돈다.
#   · 시간대를 잃지만 이 도구는 한 사무실 안에서 도는 내부 도구이고, 로그 파일이
#     이미 같은 모양(2026-09-10 09:07:32)을 쓰고 있어 오히려 표기가 통일된다.
# Rust 판(seedstore.rs::now_iso)도 같은 모양을 쓴다 — 한쪽만 바꾸면 같은 파일에
# 두 가지 표기가 섞여 사람이 정렬해 볼 수 없다.
TS_FMT = "%Y-%m-%d %H:%M:%S"

# 이 벡터를 어느 판이 만들었나(engine 칸). 두 판은 추출기가 달라 같은 문서라도
# 벡터가 완전히 같지 않다 — 실측(문서 18건)에서 17건은 소수점 8자리 수준 차이였고
# 1건이 코사인 0.980 이었다. 전파 문턱(0.950)에는 안 걸리지만, 한 파일에 두 판이
# 쓰기 시작한 뒤에는 어느 판 것인지 되짚을 방법이 없다 — 그래서 쓸 때 남긴다.
# 차원이 같아 dim 점검에도 안 걸리므로, 이 칸이 유일한 단서다.
ENGINE = "python"

# 저장소 파일을 쓸 때 다른 프로세스와 겹치지 않게 기다리는 상한(초).
# 웹에서 부르면 두 요청이 겹치는 것이 정상이라, 무한정 기다려 배치를 세우지
# 않도록 상한을 둔다(설계서 10장 — 옵션으로 열지 않는다).
LOCK_WAIT_SEC = 10.0


#------------------------------------------------------------------
# 현재 시각 ISO 문자열
#=> 기준 문서 항목·감사 로그에 찍을 지역시간 ISO8601(초) 문자열을 만든다.
#
# -in: 없음
#
# -out: str = 예 "2026-08-28 10:00:00"
# -out: error = 없음
#------------------------------------------------------------------
def now_iso():
    return datetime.datetime.now().strftime(TS_FMT)


#------------------------------------------------------------------
# 문서 경로를 비교용 키로 정규화
#=> 같은 문서가 "d:/sample/a.doc" 와 "D:\sample\A.doc" 처럼 다르게 적히면
#   기준 문서 저장소에 같은 문서가 두 줄로 앉는다(전파 때 한 문서가 두 번 투표한다).
#   비교할 때만 표기를 통일한다 — 저장되는 값은 원래 표기 그대로 둔다
#   (원본 표기가 곧 감사 기록이라서).
#
# -in: path = 문서 경로
#
# -out: str = 소문자 + 슬래시로 통일한 비교용 키
# -out: error = 없음
#------------------------------------------------------------------
def norm_file(path):
    return str(path or "").replace("\\", "/").rstrip("/").lower()


#------------------------------------------------------------------
# 보류된 축 한 칸 만들기 (v3)
#=> 시스템이 "이 축은 잠시 쓰지 말자"고 판단했을 때, 값을 지우지 않고 hold
#   칸으로 옮겨 담는다. hold 에 들어간 값은 grade/doctype 칸에서 사라지므로
#   엔진이 자동으로 그 축을 기준에서 뺀다 — 엔진 코드를 고칠 필요가 없다.
#
# -in: value  = 보류할 값(등급 문자열 또는 dc_id 리스트)
# -in: reason = 사유 코드("stale"/"missing"/"orphan"/"dim")
#
# -out: dict = {"value","reason","ts"}
# -out: error = 없음
#------------------------------------------------------------------
def _hold_block(value, reason):
    return {"value": value, "reason": reason, "ts": now_iso()}


#------------------------------------------------------------------
# 축 이름 → v3 의 값 칸 이름
#=> 화면 코드는 계속 "security"/"doctype" 이라는 축 이름으로 말한다. 파일에서는
#   그 값이 grade/doctype 칸에 평평하게 들어 있으므로 여기서 한 번 번역한다.
#
# -in: axis = "security" | "doctype"
#
# -out: str = "grade" | "doctype"
# -out: error = 없음
#------------------------------------------------------------------
def _key(axis):
    return "grade" if axis == "security" else "doctype"


#------------------------------------------------------------------
# 값이 그 축의 기준으로 쓸 만한가
#=> 보안축은 C/S/O 셋 중 하나여야 하고, 업무분류축은 비어 있지 않은 목록이어야
#   한다. 빈 배열은 "값이 없다"와 같다 — 기준으로 쓸 것이 없기 때문이다.
#
# -in: axis  = 축 이름
# -in: value = 검사할 값
#
# -out: bool = 쓸 만하면 True
# -out: error = 없음
#------------------------------------------------------------------
def _usable(axis, value):
    if axis == "security":
        return value in ("C", "S", "O")
    return bool([x for x in (value or []) if x])


#------------------------------------------------------------------
# 옛 행(v1/v2)을 v3 평평한 모양으로 끌어올리기
#=> 저장소에 어떤 판이 섞여 있어도 화면·엔진이 한 가지 모양만 다루게 한다.
#   v2 는 진실이 axes 안에 3겹으로 들어 있었다 — 그것을 평평한 칸으로 편다.
#    1) v3 면 그대로 둔다
#    2) v2 면 axes 를 풀어 grade/doctype 칸으로 올린다
#       · active  → 그 값을 평평한 칸에
#       · suspect → hold 칸으로 옮긴다(값 보존, 엔진에는 안 보임)
#       · retired → 버린다. 이전 값은 class_seed_audit.jsonl 이 갖고 있다
#    3) axes 가 없는 v1 옛 행이면 grade/labels 가 곧 진실이다
#    4) 승인자·출처·메모·시각은 줄 단위 한 칸으로 합친다(설계서 7장)
#
# -in: entry = 저장소에서 읽은 한 줄(dict)
#
# -out: dict = v3 모양의 새 dict(원본은 건드리지 않는다)
# -out: error = 없음
#------------------------------------------------------------------
def ensure_v3(entry):
    e = dict(entry or {})
    if e.get("v") == SCHEMA_VERSION:
        return _clean(e)

    axes = e.get("axes") or {}
    out = {"v": SCHEMA_VERSION, "file": e.get("file")}
    if e.get("doc_id"):
        out["doc_id"] = e["doc_id"]

    # 원본 지문은 해시만 쓴다 — size/mtime 은 판정에 쓰이지 않아 v3 에서 뺐다.
    h = (e.get("doc") or {}).get("hash") or e.get("hash")
    if h:
        out["hash"] = h

    approvers_, sources, notes, times, hold = [], [], [], [], {}
    for axis in AXES:
        key = _key(axis)
        ax = axes.get(axis)
        if ax is None:
            # v1 옛 행: axes 가 없으니 파생값이 곧 진실이었다.
            val = e.get("grade") if axis == "security" \
                else (e.get("labels") or {}).get("doctype")
            if _usable(axis, val):
                out[key] = list(val) if axis == "doctype" else val
            continue
        st, val = ax.get("state"), ax.get("value")
        if axis == "doctype":
            val = [str(x) for x in (val or []) if x]
        if st == STATE_ACTIVE and _usable(axis, val):
            out[key] = val
        elif st == STATE_SUSPECT and _usable(axis, val):
            hold[key] = _hold_block(val, ax.get("reason") or "")
        # retired 는 버린다(이력은 감사 로그에 있다)
        for box, k in ((approvers_, "approved_by"), (sources, "source"),
                       (notes, "note"), (times, "ts")):
            v = (ax.get(k) or "").strip() if isinstance(ax.get(k), str) else ax.get(k)
            if v:
                box.append(v)

    if hold:
        out["hold"] = hold

    # 줄 단위 값도 후보에 넣는다(옛 행은 여기에만 있다).
    for box, k in ((approvers_, "approved_by"), (sources, "source"),
                   (notes, "note"), (times, "ts")):
        v = e.get(k)
        if isinstance(v, str) and v.strip():
            box.append(v.strip())

    if approvers_:
        out["approved_by"] = approvers_[0]
    if sources:
        # 축마다 출처가 다르면 뜻을 잃지 않게 이어 붙인다(예: "phase4+phase6").
        seen = [x for i, x in enumerate(sources) if x not in sources[:i]]
        out["source"] = "+".join(seen)
    if notes:
        out["note"] = max(notes, key=len)      # 비어 있지 않은 것 중 가장 긴 것
    out["ts"] = max(times) if times else now_iso()

    dim = (e.get("embed") or {}).get("dim")
    if not dim and e.get("vector"):
        dim = len(e["vector"])
    if dim:
        out["dim"] = dim
    # engine 은 옛 판에 없던 칸이다. 모르는 것을 "python" 으로 채우면 거짓이 된다 —
    # v2 시절에는 파이썬만 썼지만 그것은 이 코드가 아는 사실이 아니다. 비워 둔다.
    if e.get("engine"):
        out["engine"] = e["engine"]
    if e.get("vector"):
        out["vector"] = e["vector"]
    return out


#------------------------------------------------------------------
# 옛 이름(하위호환)
#=> 예전 코드가 ensure_v2 로 부르던 자리를 그대로 살려 둔다.
#
# -in: entry = 한 줄
# -out: dict = v3 모양
# -out: error = 없음
#------------------------------------------------------------------
def ensure_v2(entry):
    return ensure_v3(entry)


#------------------------------------------------------------------
# v3 줄에서 빈 칸·죽은 칸 걷어내기
#=> "값이 없다"와 "빈 문자열이 적혀 있다"를 파일에서 구분할 필요가 없다.
#   빈 것은 아예 적지 않는다 — 파일을 열었을 때 있는 것만 보이게 한다.
#
# -in: e = v3 줄
#
# -out: dict = 정리된 새 dict
# -out: error = 없음
#------------------------------------------------------------------
def _clean(e):
    out = {}
    for k, v in e.items():
        if k in ("axes", "labels", "doc", "embed"):
            continue          # v2 잔재는 버린다
        if v in (None, "", [], {}):
            continue
        out[k] = v
    out["v"] = SCHEMA_VERSION
    if not _usable("doctype", out.get("doctype")):
        out.pop("doctype", None)
    if not _usable("security", out.get("grade")):
        out.pop("grade", None)
    return out


#------------------------------------------------------------------
# (v3 에서 없어진 함수 자리) 파생값 만들기 — 이제 할 일이 없다
#=> v2 는 진실(axes)에서 파생값(grade·labels)을 만들어야 했다. v3 는 평평한
#   칸이 곧 진실이라 만들 것이 없다. 옛 호출부가 남아 있어도 깨지지 않게
#   같은 이름을 남기되, 하는 일은 정리뿐이다.
#
# -in: entry = 한 줄
#
# -out: dict = 정리된 줄
# -out: error = 없음
#------------------------------------------------------------------
def project(entry):
    return _clean(dict(entry or {}))


#------------------------------------------------------------------
# 저장할 때 키 순서 정리
#=> 벡터(384개 숫자)가 앞에 오면 사람이 파일을 열었을 때 아무것도 못 읽는다.
#   읽을 값들을 앞에 두고 벡터를 맨 뒤로 보낸다(동작에는 영향이 없다).
#   순서는 '무엇인가(file·doc_id·hash) → 무엇으로 정했나(grade·doctype) →
#   누가 언제(approved_by·ts)' 로, 사람이 훑는 순서를 따른다.
#
# -in: entry = 저장할 한 줄
#
# -out: dict = 키 순서를 정리한 새 dict
# -out: error = 없음
#------------------------------------------------------------------
def _ordered(entry):
    head = ("v", "file", "doc_id", "hash", "grade", "doctype", "title", "hold",
            "source", "approved_by", "note", "ts", "dim", "engine")
    out = {}
    for k in head:
        if k in entry:
            out[k] = entry[k]
    for k, v in entry.items():
        if k not in out and k != "vector":
            out[k] = v
    if "vector" in entry:
        out["vector"] = entry["vector"]
    return out


#------------------------------------------------------------------
# 기준 문서 저장소 로드
#=> class_seed.jsonl 을 읽어 항목 리스트로 만든다. 없으면 빈 리스트.
#   읽으면서 옛 행(v1/v2)은 v3 로 끌어올린다 — 화면 코드가 여러 모양을
#   신경 쓰지 않아도 되게 하려는 것이다(파일은 저장할 때 비로소 v3 가 된다).
#
#   [모르는 미래 판은 건너뛴다] v 가 3보다 크면 이 코드가 모르는 형식이다.
#   추측해서 읽으면 기준 문서를 잘못 해석하게 되므로 그 줄은 건너뛰되,
#   몇 줄을 건너뛰었는지 SKIPPED 에 남긴다 — 조용히 사라지면 안 된다.
#
# -in: path = class_seed.jsonl 경로
#
# -out: list = 기준 문서 항목 dict 리스트
# -out: error = 깨진 줄은 건너뜀(예외 없음)
#------------------------------------------------------------------
def load_seeds(path):
    seeds = []
    SKIPPED.clear()
    if not path or not os.path.isfile(path):
        return seeds
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                SKIPPED.append({"line": n, "why": "broken"})
                continue
            v = row.get("v")
            if isinstance(v, int) and v > SCHEMA_VERSION:
                SKIPPED.append({"line": n, "why": "v%d" % v,
                                "file": row.get("file")})
                continue
            seeds.append(ensure_v3(row))
    return seeds


#------------------------------------------------------------------
# 기준 문서로 등록된 파일 목록만 빠르게 읽기
#=> 문서함 표에 "이 문서가 기준 문서인가"를 표시하려고 매번 부르는 함수다.
#   load_seeds 는 문서마다 384차원 벡터까지 통째로 읽어 무겁다 — 여기서는
#   file 필드 하나만 있으면 되므로 벡터는 버리고 경로만 모은다.
#
# -in: path = class_seed.jsonl 경로
#
# -out: set = 정규화된 경로 집합(파일이 없으면 빈 집합)
# -out: error = 없음(깨진 줄은 건너뜀)
#------------------------------------------------------------------
def load_seed_files(path):
    files = set()
    if not path or not os.path.isfile(path):
        return files
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                f_ = json.loads(line).get("file")
            except json.JSONDecodeError:
                continue
            if f_:
                files.add(norm_file(f_))
    return files


#------------------------------------------------------------------
# 기준 문서 저장소 저장(통째 덮어쓰기)
#=> 편집된 리스트 전체를 class_seed.jsonl 에 다시 쓴다(현재 상태 = 이 파일).
#    1) 저장 직전에 v3 모양으로 맞추고 빈 칸을 걷어낸다
#    2) 기준으로 쓸 것이 하나도 없는 줄(grade·doctype·hold 전부 없음)은 뺀다.
#       hold(시스템이 잠시 빼 둔 축)는 반드시 남긴다 — 사람이 확인해 되살릴
#       대상인데 파일에서 사라지면 되살릴 방법도, 무엇이 있었는지 알 방법도 없다
#   변경 감사는 append_seed_audit 로 별도로 남긴다.
#
# -in: path  = class_seed.jsonl 경로
# -in: seeds = 기준 문서 항목 리스트
#
# -out: int = 실제로 쓴 줄 수
# -out: error = 쓰기 실패 시 예외 전파
#------------------------------------------------------------------
def save_seeds(path, seeds):
    rows = []
    for s in seeds:
        e = ensure_v3(s)
        if not keepable_axes(e):
            continue
        rows.append(_ordered(e))
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    # 잠금은 '읽기~쓰기' 전체가 아니라 쓰기 구간만 감싼다. 읽기까지 묶으려면
    # 호출부가 with seed_lock(path): 로 감싸야 한다(cli 의 --seed-add 가 그렇게 한다).
    with seed_lock(path):
        _write_atomic(path, body)
    return len(rows)


#------------------------------------------------------------------
# 파일을 통째로 바꿔치기(원자적 교체)
#=> 같은 폴더의 임시파일에 다 쓴 뒤 os.replace 로 이름을 바꾼다. 도중에
#   프로세스가 죽어도 원본은 옛 내용 그대로 남는다 — 반쪽 파일이 생기지 않는다.
#   기준 문서 파일이 반쪽이 되면 그 다음 분류가 통째로 잘못된 잣대로 돌아간다.
#    1) 같은 폴더에 임시파일을 만든다(다른 드라이브면 os.replace 가 원자적이지 않다)
#    2) 다 쓰고 flush + fsync 로 디스크에 내려보낸다
#    3) os.replace 로 갈아 끼운다 — 윈도우에서도 원자적이다
#
# -in: path = 최종 파일 경로
# -in: body = 파일에 쓸 전체 내용(문자열)
#
# -out: 없음
# -out: error = 쓰기 실패 시 예외 전파(임시파일은 지운다)
#------------------------------------------------------------------
def _write_atomic(path, body):
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".%s.tmp%d" % (os.path.basename(path), os.getpid()))
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


#------------------------------------------------------------------
# 저장소 파일 잠금 (with 문으로 쓴다)
#=> 화면에서만 쓸 때는 사람이 한 번에 하나씩 눌러 문제가 없었다. 그런데 웹이
#   CLI 를 부르기 시작하면 두 요청이 겹치는 것이 정상이고, 그대로 두면 한쪽
#   등록이 아무 말 없이 사라진다(둘 다 옛 내용을 읽어 각자 전체를 다시 쓰므로).
#    1) <파일>.lock 을 만들어 그 파일을 잠근다 — 대상 파일 자체를 잠그면
#       os.replace 로 갈아 끼우는 것이 막힌다
#    2) 윈도우는 msvcrt.locking, 리눅스는 fcntl.flock 을 쓴다
#    3) LOCK_WAIT_SEC 를 넘겨도 못 잡으면 예외를 낸다 — 무한정 기다려
#       배치를 세우지 않는다
#
#   [같은 스레드가 겹쳐 잡아도 된다] merge_into 는 '읽기~쓰기' 전체를 잠근 뒤
#   그 안에서 save_seeds 를 부르는데, save_seeds 도 같은 파일을 잠근다.
#   겹침을 허용하지 않으면 자기가 자기를 기다리다 10초 뒤 실패한다 — 실제로
#   그렇게 됐다. 그래서 스레드마다 깊이를 세어, 가장 바깥 것만 실제로 잠근다.
#   깊이를 스레드별로 세는 이유는 화면(Streamlit)이 여러 스레드로 돌기 때문이다.
#
# -in: path = 기준 문서 파일 경로(.lock 을 붙여 쓴다)
# -in: wait = 대기 상한(초). 기본 LOCK_WAIT_SEC
#
# -out: 컨텍스트 매니저
# -out: error = 대기 상한을 넘기면 TimeoutError. 잠금 기능 자체를 못 쓰는
#               환경이면 잠그지 않고 그대로 진행한다(막지는 않는다)
#------------------------------------------------------------------
class seed_lock(object):

    # 스레드마다 '지금 잡고 있는 잠금 파일 → 겹친 깊이'. 겹쳐 들어오면 실제
    # 파일 잠금은 건너뛰고 깊이만 센다.
    _held = threading.local()

    def __init__(self, path, wait=LOCK_WAIT_SEC):
        self.path = str(path or "") + ".lock"
        self.wait = wait
        self.fh = None
        self.nested = False

    #--------------------------------------------------------------
    # 이 스레드가 지금 잡고 있는 잠금들
    #=> threading.local 은 스레드마다 처음 쓸 때 비어 있으므로 여기서 만든다.
    #
    # -in: 없음
    # -out: dict = {잠금파일 절대경로: 겹친 깊이}
    # -out: error = 없음
    #--------------------------------------------------------------
    def _depths(self):
        d = getattr(seed_lock._held, "depths", None)
        if d is None:
            d = {}
            seed_lock._held.depths = d
        return d

    def __enter__(self):
        key = os.path.abspath(self.path)
        depths = self._depths()
        # 이미 이 스레드가 잡고 있으면 깊이만 올린다(자기가 자기를 기다리지 않게).
        if depths.get(key):
            depths[key] += 1
            self.nested = True
            return self
        d = os.path.dirname(os.path.abspath(self.path))
        if d:
            os.makedirs(d, exist_ok=True)
        deadline = time.time() + self.wait
        # 잠금을 아예 못 쓰는 환경(특수 파일시스템 등)에서는 막지 않고 지나간다.
        try:
            self.fh = open(self.path, "a+")
        except OSError:
            self.fh = None
            return self
        while True:
            try:
                _lock_file(self.fh)
                depths[key] = 1
                return self
            except OSError:
                if time.time() >= deadline:
                    self.fh.close()
                    self.fh = None
                    raise TimeoutError(
                        "기준 문서 파일이 %.0f초 동안 다른 작업에 잡혀 있습니다: %s"
                        % (self.wait, self.path))
                time.sleep(0.1)

    def __exit__(self, *exc):
        key = os.path.abspath(self.path)
        depths = self._depths()
        if self.nested:
            depths[key] = max(0, depths.get(key, 1) - 1)
            return False
        depths.pop(key, None)
        if self.fh is not None:
            try:
                _unlock_file(self.fh)
            except OSError:
                pass
            self.fh.close()
            self.fh = None
        return False


#------------------------------------------------------------------
# 열린 파일 하나 잠그기(운영체제별)
#=> 윈도우와 리눅스의 잠금 호출이 다르다. 두 판이 같은 뜻으로 동작하도록
#   여기 한 곳에서만 갈라 둔다.
#
# -in: fh = 열린 파일 객체
#
# -out: 없음
# -out: error = 이미 잡혀 있으면 OSError(호출부가 기다린다)
#------------------------------------------------------------------
def _lock_file(fh):
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


#------------------------------------------------------------------
# 잠금 풀기(운영체제별)
#
# -in: fh = 잠가 둔 파일 객체
#
# -out: 없음
# -out: error = OSError 전파(호출부가 삼킨다)
#------------------------------------------------------------------
def _unlock_file(fh):
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


#------------------------------------------------------------------
# 경로로 항목 찾기(정규화 비교)
#=> 대소문자·슬래시만 다른 같은 문서를 놓치지 않는다.
#
# -in: seeds = 기준 문서 리스트
# -in: file  = 찾을 문서 경로
#
# -out: dict|None = 찾은 항목(없으면 None)
# -out: error = 없음
#------------------------------------------------------------------
def find_seed(seeds, file):
    key = norm_file(file)
    return next((s for s in seeds if norm_file(s.get("file")) == key), None)


#------------------------------------------------------------------
# 한 축의 상태 읽기
#=> v3 는 상태를 따로 적지 않는다. 값이 있으면 쓰는 중(active), hold 에 들어
#   있으면 확인 필요(suspect), 둘 다 없으면 축이 없는 것(빈 문자열)이다.
#   상태 칸을 없앤 대신 이 함수가 모양에서 상태를 읽어 낸다.
#
# -in: entry = 기준 문서 한 줄
# -in: axis  = "security" | "doctype"
#
# -out: str = "active"/"suspect" 또는 ""(축 없음)
# -out: error = 없음
#------------------------------------------------------------------
def axis_state(entry, axis):
    e = entry or {}
    key = _key(axis)
    if _usable(axis, e.get(key)):
        return STATE_ACTIVE
    if key in (e.get("hold") or {}):
        return STATE_SUSPECT
    return ""


#------------------------------------------------------------------
# 한 축의 값 읽기
#=> security 는 "C"/"S"/"O", doctype 은 dc_id 리스트를 돌려준다.
#   보류(hold) 중인 축도 값을 돌려준다 — 화면이 "무엇이 보류됐는지" 를 보여
#   줘야 사람이 되살릴지 지울지 판단할 수 있기 때문이다.
#
# -in: entry = 기준 문서 한 줄
# -in: axis  = "security" | "doctype"
#
# -out: str|list|None = 그 축의 값(축이 없으면 None)
# -out: error = 없음
#------------------------------------------------------------------
def axis_value(entry, axis):
    e = entry or {}
    key = _key(axis)
    if key in e:
        return e[key]
    held = (e.get("hold") or {}).get(key)
    return held.get("value") if held else None


#------------------------------------------------------------------
# 보류된 축의 사유 읽기
#=> "왜 이 기준 문서가 지금 안 쓰이나"를 화면에 그대로 보여 주려고 쓴다.
#   사유를 안 보여 주면 사람은 되살릴지 지울지 판단할 근거가 없다 —
#   거버넌스 도구에서 '이유 없이 빠진 것'이 가장 나쁜 상태다.
#
# -in: entry = 기준 문서 한 줄
# -in: axis  = "security" | "doctype"
#
# -out: str = "stale"/"missing"/"orphan"/"dim" 또는 ""(보류 아님)
# -out: error = 없음
#------------------------------------------------------------------
def hold_reason(entry, axis):
    held = ((entry or {}).get("hold") or {}).get(_key(axis))
    return (held or {}).get("reason") or ""


#------------------------------------------------------------------
# 이 기준 문서를 확정한 사람
#=> 표에 "누가 이 기준을 세웠나"를 보여 주려고 쓴다. 기준 문서는 사람이
#   책임지고 고른 본보기라, 나중에 결과가 이상할 때 물어볼 사람이 누구인지가
#   메모만큼이나 중요한 정보다.
#
#   [v3 에서 한 명이 된 이유] v2 는 축마다 승인자를 따로 적었다. 그런데 실제
#   운영 파일 19행 중 두 축의 승인자가 다른 줄은 0건이었고, 화면에도 축별로
#   다른 사람을 적을 수단이 없었다 — 쓰이지 않는 유연성이었다. 그래서 줄 단위
#   한 칸으로 합쳤다. "누가 어느 축을 확정했나" 는 class_seed_audit.jsonl 이
#   axis 칸과 함께 갖고 있으므로 추적은 그대로 된다.
#
# -in: entry = 기준 문서 한 줄
#
# -out: tuple = 확정자 이름(없으면 빈 튜플). 모양을 바꾸지 않으려고 튜플이다
# -out: error = 없음
#------------------------------------------------------------------
def approvers(entry):
    who = str((entry or {}).get("approved_by") or "").strip()
    return (who,) if who else ()


#------------------------------------------------------------------
# 이 줄에서 실제로 쓰이는(active) 축 목록
#=> 전파에 쓰이는 축이 하나라도 있는지 판단하는 기준이다.
#   v3 에서는 "값 칸이 있고 쓸 만한 값이면 쓰는 중" 이다.
#
# -in: entry = 기준 문서 한 줄
#
# -out: tuple = ("security",) / ("doctype",) / 둘 다 / 빈 튜플
# -out: error = 없음
#------------------------------------------------------------------
def active_axes(entry):
    e = entry or {}
    return tuple(a for a in AXES if _usable(a, e.get(_key(a))))


#------------------------------------------------------------------
# 이 줄을 파일에 남겨 둘 이유가 있는 축 목록
#=> 지금 쓰는 축(값이 있는 것) 뿐 아니라 보류(hold)된 축도 남긴다.
#   보류는 "사람이 확인해서 되살리거나 지울 대상" 이므로, 파일에서 사라지면
#   확인 자체가 불가능해진다. 사람이 해제한 축만 버린다(그 자리는 이미 비었다).
#
# -in: entry = 기준 문서 한 줄
#
# -out: tuple = 남겨 둘 이유가 있는 축 이름들(비면 그 줄은 저장하지 않는다)
# -out: error = 없음
#------------------------------------------------------------------
def keepable_axes(entry):
    e = entry or {}
    hold = e.get("hold") or {}
    return tuple(a for a in AXES
                 if _usable(a, e.get(_key(a))) or _key(a) in hold)


#------------------------------------------------------------------
# 축 하나를 확정(추가·갱신) — 저장소로 들어가는 유일한 통로
#=> "이 문서의 이 축을 이 값으로 확정한다"를 한 함수로 모은다. 줄이 없으면
#   만들고, 있으면 그 줄의 해당 축만 갈아 끼운다(다른 축은 손대지 않는다).
#    1) 정규화 비교로 같은 문서 줄을 찾는다(표기만 다른 중복 줄을 막는다)
#    2) 그 축 블록을 새로 쓰고 누가·언제·어디서 왔는지 남긴다
#    3) 벡터·원본지문·임베딩정보가 새로 주어졌으면 함께 갱신한다
#    4) project() 로 엔진이 읽는 파생값을 다시 만든다
#
# -in: seeds    = 기존 리스트(변형하지 않고 새 리스트 반환)
# -in: file     = 문서 경로
# -in: axis     = "security" | "doctype"
# -in: value    = security 면 "C"/"S"/"O", doctype 이면 dc_id 리스트
# -in: reviewer = 승인자 이름
# -in: source   = 출처 태그("phase4"/"phase2"/"phase6"/"upload" 등)
# -in: note     = 메모(None 이면 기존 메모 유지)
# -in: state    = 축 상태(기본 active)
# -in: vector   = 문서 벡터(None 이면 기존 것 유지)
# -in: doc      = {"hash","size","mtime"} 원본 지문(None 이면 유지)
# -in: embed    = {"model","dim","ts"} 임베딩 정보(None 이면 유지)
#
# -out: list = 갱신된 리스트
# -out: error = 없음
#------------------------------------------------------------------
def set_axis(seeds, file, axis, value, reviewer, *, source="", note=None,
             state=STATE_ACTIVE, vector=None, doc=None, embed=None):
    key = norm_file(file)
    out, found = [], False
    for s in seeds:
        if norm_file(s.get("file")) != key:
            out.append(s)
            continue
        out.append(_apply_axis(ensure_v3(s), axis, value, reviewer, source, note,
                               state, vector, doc, embed))
        found = True
    if not found:
        base = {"v": SCHEMA_VERSION, "file": file}
        out.append(_apply_axis(base, axis, value, reviewer, source, note,
                               state, vector, doc, embed))
    return out


#------------------------------------------------------------------
# 축 한 칸 실제로 갈아 끼우기(내부)
#=> set_axis 가 부르는 알맹이. v3 에서는 평평한 칸 하나를 쓰는 일이다.
#    1) doctype 은 항상 리스트로 정규화한다(문자열 하나가 와도 배열로)
#    2) 사람이 다시 확정하면 그 축의 보류(hold)는 풀린다
#    3) 승인자·출처·메모·시각은 줄 단위 칸에 적는다
#
# -in: e        = 대상 줄(v3 로 맞춰진 상태)
# -in: 나머지    = set_axis 와 동일
#
# -out: dict = 고쳐진 새 줄
# -out: error = 없음
#------------------------------------------------------------------
def _apply_axis(e, axis, value, reviewer, source, note, state, vector, doc, embed):
    e = dict(e)
    key = _key(axis)
    if axis == "doctype":
        if value is None:
            value = e.get(key) or []
        elif isinstance(value, str):
            value = [value]
        value = [str(x) for x in value if x]

    if state == STATE_ACTIVE:
        e[key] = value
        # 사람이 다시 확정했으면 시스템이 붙여 둔 보류 표시는 의미를 잃는다.
        hold = dict(e.get("hold") or {})
        hold.pop(key, None)
        if hold:
            e["hold"] = hold
        else:
            e.pop("hold", None)
    elif state == STATE_SUSPECT:
        e.pop(key, None)
        e["hold"] = {**(e.get("hold") or {}), key: _hold_block(value, "")}
    else:                                   # retired = 그 축을 버린다
        e.pop(key, None)
        hold = dict(e.get("hold") or {})
        hold.pop(key, None)
        if hold:
            e["hold"] = hold
        else:
            e.pop("hold", None)

    if source:
        # 축마다 출처가 다르면 뜻을 잃지 않게 이어 붙인다(예: "phase4+phase6").
        prev = [x for x in str(e.get("source") or "").split("+") if x]
        if source not in prev:
            prev.append(source)
        e["source"] = "+".join(prev)
    if reviewer:
        e["approved_by"] = reviewer
    if note is not None:
        e["note"] = note

    if vector is not None:
        e["vector"] = vector
        e["dim"] = len(vector)
        # 벡터를 새로 넣을 때만 판 이름을 적는다 — 값이 그대로면 만든 판도 그대로다.
        e["engine"] = ENGINE
    if doc:
        # v3 는 해시만 쓴다 — size/mtime 은 판정에 쓰이지 않아 뺐다(설계서 5장).
        h = doc.get("hash") if isinstance(doc, dict) else None
        if h:
            e["hash"] = h
    if embed and embed.get("dim"):
        e["dim"] = embed["dim"]

    e["ts"] = now_iso()
    return _clean(e)


#------------------------------------------------------------------
# 축 하나 해제(retire)
#=> "업무분류만 기준에서 빼고 보안등급은 유지" 를 가능하게 하는 함수.
#   v3 는 그 축의 칸을 지운다 — v2 는 값을 남겨 두는 척했지만, 파생값이
#   지워져 엔진이 못 보고, 두 축을 다 해제하면 줄째로 파일에서 빠졌으며,
#   재등록도 막지 못했다. 즉 이미 '지운다' 와 같았다. 지운 값은 호출부가
#   append_seed_audit(before=이전값) 로 감사 로그에 남긴다.
#
# -in: seeds = 기존 리스트
# -in: file  = 대상 문서
# -in: axis  = 해제할 축
#
# -out: (list, removed) = 갱신된 리스트, removed 는 이 줄이 통째로 빠지는지 여부
# -out: error = 없음
#------------------------------------------------------------------
def retire_axis(seeds, file, axis):
    key = norm_file(file)
    out, removed = [], False
    for s in seeds:
        if norm_file(s.get("file")) != key:
            out.append(s)
            continue
        e = _apply_axis(ensure_v3(s), axis, None, "", "", None,
                        STATE_RETIRED, None, None, None)
        removed = not keepable_axes(e)
        out.append(e)
    return out, removed


#------------------------------------------------------------------
# 축에 의심 표시 붙이기(suspect) — 시스템이 붙인다
#=> 원본이 바뀌었거나(stale) 원본을 못 찾거나(missing) 분류코드가 사라졌을 때
#   (orphan) 그 축을 전파에서 빼되 값은 그대로 둔다. 사람이 확인해 되살리거나
#   지운다 — 시스템은 절대 스스로 지우지 않는다.
#
#   [v3 에서 어떻게 빼나] 값을 hold 칸으로 옮긴다. grade/doctype 칸이 비면
#   엔진이 그 축을 자동으로 안 쓰게 되므로, 엔진 코드를 고칠 필요가 없다.
#   대신 반드시 세어야 한다 — check_seeds 가 hold 를 suspect 로 센다.
#
# -in: seeds  = 기존 리스트
# -in: file   = 대상 문서
# -in: axis   = 대상 축
# -in: reason = 사유 코드("stale"/"missing"/"orphan"/"dim")
#
# -out: list = 갱신된 리스트
# -out: error = 없음
#------------------------------------------------------------------
def suspect_axis(seeds, file, axis, reason):
    key = norm_file(file)
    out = []
    for s in seeds:
        if norm_file(s.get("file")) != key:
            out.append(s)
            continue
        e = ensure_v3(s)
        k = _key(axis)
        val = e.get(k)
        if not _usable(axis, val):
            # 이미 보류 중이면 사유만 갱신한다(없는 축은 건드리지 않는다).
            held = (e.get("hold") or {}).get(k)
            if held:
                e = dict(e)
                e["hold"] = {**e["hold"], k: _hold_block(held.get("value"), reason)}
            out.append(e)
            continue
        e = dict(e)
        e.pop(k, None)
        e["hold"] = {**(e.get("hold") or {}), k: _hold_block(val, reason)}
        out.append(e)
    return out


#------------------------------------------------------------------
# 의심 표시 해제(다시 기준으로 씀)
#=> 사람이 확인했거나 원본을 다시 읽어 갱신했을 때, hold 에 넣어 둔 값을
#   제자리로 되돌린다. 되돌릴 값이 없으면 아무 일도 하지 않는다.
#
# -in: seeds = 기존 리스트
# -in: file  = 대상 문서
# -in: axis  = 대상 축
#
# -out: list = 갱신된 리스트
# -out: error = 없음
#------------------------------------------------------------------
def restore_axis(seeds, file, axis):
    key = norm_file(file)
    out = []
    for s in seeds:
        if norm_file(s.get("file")) != key:
            out.append(s)
            continue
        e = dict(ensure_v3(s))
        k = _key(axis)
        held = (e.get("hold") or {}).get(k)
        if held and _usable(axis, held.get("value")):
            e[k] = held["value"]
            hold = dict(e["hold"])
            hold.pop(k, None)
            if hold:
                e["hold"] = hold
            else:
                e.pop("hold", None)
            e["ts"] = now_iso()
        out.append(_clean(e))
    return out


#------------------------------------------------------------------
# 값은 그대로 두고 부속 정보만 갱신
#=> 등급·분류코드는 그대로인데 벡터를 다시 뽑았거나 원본 지문을 새로 얻었을
#   때 쓴다. v3 는 해시와 차원만 갖는다(size·mtime 은 판정에 안 쓰여 뺐다).
#
# -in: seeds  = 기존 리스트
# -in: file   = 대상 문서
# -in: vector = 새 벡터(None 이면 유지)
# -in: doc    = {"hash":...} 원본 지문(None 이면 유지)
# -in: embed  = {"dim":...} 임베딩 정보(None 이면 벡터 길이로 채운다)
#
# -out: list = 갱신된 리스트
# -out: error = 없음
#------------------------------------------------------------------
def set_meta(seeds, file, *, vector=None, doc=None, embed=None):
    key = norm_file(file)
    out = []
    for s in seeds:
        if norm_file(s.get("file")) != key:
            out.append(s)
            continue
        e = dict(ensure_v3(s))
        if vector is not None:
            e["vector"] = vector
            e["dim"] = len(vector)
            e["engine"] = ENGINE
        if doc and doc.get("hash"):
            e["hash"] = doc["hash"]
        if embed and embed.get("dim"):
            e["dim"] = embed["dim"]
        e["ts"] = now_iso()
        out.append(_clean(e))
    return out


#------------------------------------------------------------------
# 기준 문서 줄 통째 삭제
#=> 두 축을 함께 지운다. 경로는 정규화해서 비교한다 — 예전에는 원문 그대로
#   비교해서 "D:\a.doc 로 등록된 것을 d:/a.doc 로 지우면 아무 일도 안 일어나는데
#   성공한 것처럼 보이는" 버그가 있었다.
#
# -in: seeds = 기존 리스트
# -in: file  = 삭제할 문서 경로
#
# -out: list = 제거된 새 리스트
# -out: error = 없음
#------------------------------------------------------------------
def remove_seed(seeds, file):
    key = norm_file(file)
    return [s for s in seeds if norm_file(s.get("file")) != key]


#------------------------------------------------------------------
# 보안등급 기준 문서 추가/갱신 (set_axis 의 얇은 껍데기)
#=> 옛 이름을 그대로 쓰는 호출부가 많아 남겨 둔 편의 함수다.
#
# -in: seeds    = 기존 리스트
# -in: file     = 문서 경로
# -in: grade    = 등급("C"/"S"/"O")
# -in: vector   = 문서 벡터
# -in: reviewer = 승인자
# -in: source   = 출처("phase2"/"phase4"/"upload")
# -in: note     = 메모
# -in: doc      = 원본 지문(선택)
#
# -out: list = 갱신된 리스트
# -out: error = 없음
#------------------------------------------------------------------
def add_seed(seeds, file, grade, vector, reviewer, source="phase4", note="", doc=None):
    return set_axis(seeds, file, "security", grade, reviewer,
                    source=source, note=note, vector=vector, doc=doc)


#------------------------------------------------------------------
# 업무분류 기준 문서 추가/갱신 (set_axis 의 얇은 껍데기)
#=> 보안등급으로 먼저 등록된 줄이 있으면 그 줄에 업무분류축만 얹는다.
#   없으면 업무분류축만 있는 줄을 새로 만든다 — "보안등급은 안 정했지만
#   업무분류만 확정했다" 도 유효한 기준 문서다.
#
# -in: seeds    = 기존 리스트
# -in: file     = 문서 경로
# -in: dc_ids   = 확정된 dc_id 리스트
# -in: vector   = 문서 벡터
# -in: reviewer = 승인자
# -in: source   = 출처(기본 "phase6")
# -in: note     = 메모
# -in: doc      = 원본 지문(선택)
#
# -out: list = 갱신된 리스트
# -out: error = 없음
#------------------------------------------------------------------
def add_doctype_seed(seeds, file, dc_ids, vector, reviewer, source="phase6",
                     note="", doc=None):
    return set_axis(seeds, file, "doctype", list(dc_ids), reviewer,
                    source=source, note=note, vector=vector, doc=doc)


#------------------------------------------------------------------
# 보안등급만 바꾸기(표 편집용)
#=> 기준 문서 표에서 등급 칸만 고쳤을 때 쓴다. 업무분류축은 손대지 않는다.
#
# -in: seeds    = 기존 리스트
# -in: file     = 대상 문서
# -in: grade    = 새 등급
# -in: reviewer = 승인자(빈 문자열이면 기존 승인자 유지)
#
# -out: list = 갱신된 리스트
# -out: error = 없음
#------------------------------------------------------------------
def set_grade(seeds, file, grade, reviewer=""):
    return set_axis(seeds, file, "security", grade, reviewer)


#------------------------------------------------------------------
# 최근접 기준 문서 유사도(사본 판정용)
#=> 주어진 벡터와 기존 기준 문서들 사이 최대 코사인 유사도와 그 문서를 찾는다.
#   중복(정보 없는) 기준 문서 추가를 막기 위한 경고 근거.
#
# -in: vector       = 후보 문서 벡터(list/ndarray)
# -in: seeds        = 기존 리스트(각 벡터 보유)
# -in: exclude_file = 비교에서 뺄 문서 경로. 후보가 이미 저장소에 있으면
#                     자기 자신과 sim=1.000 이 나와 "무조건 중복"으로 오판되므로,
#                     승격 시 후보 자신을 넣어 자기비교를 배제한다
#
# -out: (max_sim, file) = 최대 유사도, 그 문서 경로(없으면 (0.0, None))
# -out: error = 없음
#------------------------------------------------------------------
def nearest_seed_sim(vector, seeds, exclude_file=None):
    key = norm_file(exclude_file) if exclude_file else None
    # 벡터가 있고, 자기 자신(같은 문서)이 아닌 것만 비교 대상으로.
    pairs = [(s.get("vector"), s.get("file")) for s in seeds
             if s.get("vector") and (key is None or norm_file(s.get("file")) != key)]
    if vector is None or not pairs:
        return 0.0, None
    vecs = [p[0] for p in pairs]
    files = [p[1] for p in pairs]
    v = np.asarray(vector, dtype=np.float32)
    n = np.linalg.norm(v)
    v = v / (n if n else 1.0)
    arr = np.asarray(vecs, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    sims = (arr / norms) @ v
    i = int(np.argmax(sims))
    return float(sims[i]), files[i]


#------------------------------------------------------------------
# 고신뢰 후보 일괄 등록(중복 자동 제외)
#=> 이번 분류에서 뽑은 "고신뢰 C/S 후보"들을 한 번에 등록하되, 정보량이 없는
#   사본(near-dup)은 자동으로 건너뛴다.
#    1) 후보를 순회하며: 벡터 없음/이미 등록됨/기존·직전추가와 사본이면 제외
#    2) 통과분만 add_seed 로 누적(뒤 후보는 앞서 추가된 것과도 중복 비교됨)
#   중복 판정은 경로를 정규화해서 한다 — 표기만 다른 같은 문서가 두 줄로 앉으면
#   전파에서 한 문서가 두 번 투표한다.
#
# -in: seeds      = 기존 리스트(변형하지 않고 새 리스트 반환)
# -in: candidates = [{file, grade, vector, doc?}] 후보들
# -in: reviewer   = 승인자(감사 기록용)
# -in: dup_thresh = 사본 판정 코사인 임계(기본 SEED_DUP_THRESHOLD)
# -in: source     = 출처 태그(기본 "phase2" = 배치 등록)
# -in: note       = 각 항목 메모
#
# -out: (new_seeds, added, stats)
#        new_seeds = 갱신된 리스트
#        added     = 실제 추가된 [{file, grade}]
#        stats     = {"added","dup","exist","novec"} 각 사유별 건수
# -out: error = 없음
#------------------------------------------------------------------
def bulk_add_confident(seeds, candidates, reviewer, dup_thresh=SEED_DUP_THRESHOLD,
                       source="phase2", note="일괄 등록"):
    out = list(seeds)
    existing = {norm_file(s.get("file")) for s in out}   # 이미 등록된 문서(정규화 비교)
    added = []
    dup = exist = novec = 0
    for c in candidates:
        f, g, v = c.get("file"), c.get("grade"), c.get("vector")
        if not v:                                # 벡터 없으면 기준 문서로 못 씀
            novec += 1
            continue
        if norm_file(f) in existing:             # 이미 저장소에 있음 → 건너뜀
            exist += 1
            continue
        # 자기 자신 제외하고, 기존+직전 추가분과의 최대 유사도로 사본 여부 판단.
        sim, _ = nearest_seed_sim(v, out, exclude_file=f)
        if sim >= dup_thresh:
            dup += 1
            continue
        out = add_seed(out, f, g, v, reviewer, source=source, note=note,
                       doc=c.get("doc"))
        existing.add(norm_file(f))
        added.append({"file": f, "grade": g})
    stats = {"added": len(added), "dup": dup, "exist": exist, "novec": novec}
    return out, added, stats


#------------------------------------------------------------------
# 등급별 기준 문서 개수
#=> 보안축이 살아 있는(active) 줄만 센다 — 해제·의심 표시된 것은 전파에 쓰이지
#   않으므로 균형 점검에서도 빼야 한다.
#
# -in: seeds = 리스트
#
# -out: dict = {"C":n, "S":n, "O":n}
# -out: error = 없음
#------------------------------------------------------------------
def grade_counts(seeds):
    counts = {"C": 0, "S": 0, "O": 0}
    for s in seeds:
        if "security" in active_axes(s):
            g = axis_value(s, "security")
            if g in counts:
                counts[g] += 1
    return counts


#------------------------------------------------------------------
# 축별 기준 문서 개수
#=> "보안 96 · 업무분류 74 (둘 다 42)" 를 만드는 집계다.
#   retired 는 v3 에서 '지운다' 와 같아져 셀 대상이 없다 — 칸은 남겨 두되
#   항상 0 이다(화면이 이 키를 읽고 있어 없애면 깨진다).
#
# -in: seeds = 리스트
#
# -out: dict = {"total","security","doctype","both","suspect","retired"}
# -out: error = 없음
#------------------------------------------------------------------
def axis_counts(seeds):
    out = {"total": len(seeds), "security": 0, "doctype": 0, "both": 0,
           "suspect": 0, "retired": 0}
    for s in seeds:
        act = active_axes(s)
        if "security" in act:
            out["security"] += 1
        if "doctype" in act:
            out["doctype"] += 1
        if len(act) == 2:
            out["both"] += 1
        out["suspect"] += len((s or {}).get("hold") or {})
    return out


#------------------------------------------------------------------
# 기준 문서 변경 감사 기록(append-only)
#=> 누가·언제·어느 축을·무엇에서 무엇으로 바꿨는지 class_seed_audit.jsonl 에 남긴다.
#   예전에는 detail 이 자유 문자열이라 나중에 기계가 읽을 수 없었다 — 축과
#   전후값을 각각 칸으로 분리했다. 줄을 지운 뒤에는 이 파일이 유일한 이력이다.
#
# -in: path     = class_seed_audit.jsonl 경로
# -in: action   = "add"|"update"|"retire"|"restore"|"suspect"|"delete"
# -in: file     = 대상 문서 경로
# -in: axis     = "security"|"doctype"|"-"(줄 전체)
# -in: before   = 바뀌기 전 값(없으면 None)
# -in: after    = 바뀐 뒤 값(없으면 None)
# -in: reviewer = 작업자 이름(시스템이 붙인 것이면 "system")
# -in: reason   = 사유 한 줄(선택)
#
# -out: 없음(append)
# -out: error = 쓰기 실패 시 예외 전파
#------------------------------------------------------------------
def append_seed_audit(path, action, file, axis="-", before=None, after=None,
                      reviewer="", reason=""):
    entry = {"ts": now_iso(), "action": action, "axis": axis, "file": file,
             "before": before, "after": after,
             "reviewer": reviewer, "reason": reason}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


#------------------------------------------------------------------
# 기준 문서가 업로드로 등록돼 원본 경로가 없는 줄인가
#=> '파일을 직접 올려 등록' 한 기준 문서는 file 에 파일 이름만 들어 있다.
#   이런 줄까지 "원본을 못 찾음" 으로 세면 점검 화면이 경고로 뒤덮여
#   진짜 사라진 문서를 못 보게 된다.
#
# -in: entry = 기준 문서 한 줄
#
# -out: bool = True 면 원본 경로가 애초에 없는 줄
# -out: error = 없음
#------------------------------------------------------------------
def _is_uploaded(entry):
    f = str(entry.get("file") or "")
    if os.path.isabs(f):
        return False
    return "/" not in f.replace("\\", "/")


#------------------------------------------------------------------
# 기준 문서 점검 — 무엇이 썩었는지 한 번에 센다
#=> 설계서 6장 '기준 문서 점검' 화면이 쓰는 계산. 판정은 전부 여기서 하고
#   화면은 세어 보여 주기만 한다(시스템은 표시만 하고, 지우는 것은 사람이 한다).
#    1) stale   : 저장해 둔 원본 지문(doc.hash)과 이번 분류 결과의 해시가 다름
#    2) missing : 원본 파일이 그 경로에 없음(업로드 등록분은 제외)
#    3) orphan  : 분류 체계에서 사라진 dc_id 를 들고 있음
#    4) novec   : 벡터가 없어 비교에 못 쓰임
#    5) dim     : 벡터 차원이 다수와 다름(임베딩 모델이 바뀐 흔적)
#    6) conflict: 사실상 같은 문서인데 등급이 서로 다름 → 전파가 mixed 로 죽는다
#    7) engine  : 한 파일에 두 판(python·rust)이 만든 벡터가 섞여 있음
#
# -in: seeds      = 기준 문서 리스트
# -in: records    = 이번 분류 레코드 리스트(hash 비교용, 없으면 stale 판정 생략)
# -in: tax        = 회사 분류 체계(없으면 orphan 판정 생략)
# -in: conflict_sim = 등급 충돌로 볼 유사도 임계(기본 CONFLICT_SIM)
#
# -out: dict = {"counts":축별집계, "stale":[], "missing":[], "orphan":[],
#               "novec":[], "dim":[], "conflict":[], "suspect":[],
#               "engine":{판이름:건수} — 두 종류 이상일 때만 채운다}
# -out: error = 없음
#------------------------------------------------------------------
def check_seeds(seeds, records=None, tax=None, conflict_sim=CONFLICT_SIM):
    rep = {"counts": axis_counts(seeds), "stale": [], "missing": [], "orphan": [],
           "novec": [], "dim": [], "conflict": [], "suspect": []}

    # 결과 레코드를 경로로 찾아 쓸 수 있게 정리(해시 비교용).
    by_file = {}
    for r in (records or []):
        by_file[norm_file(r.get("file"))] = r

    dims = {}
    for s in seeds:
        f = s.get("file")
        v = s.get("vector")
        if not v:
            rep["novec"].append(f)
        else:
            dims[norm_file(f)] = len(v)

        # 원본 지문 대조 — 양쪽 다 있을 때만 판정한다(모르면 조용히 넘어간다).
        seed_hash = s.get("hash")
        rec = by_file.get(norm_file(f))
        now_hash = (rec or {}).get("hash")
        if seed_hash and now_hash and seed_hash != now_hash:
            rep["stale"].append({"file": f, "before": seed_hash, "after": now_hash})

        if not _is_uploaded(s) and not os.path.isfile(str(f or "")):
            rep["missing"].append(f)

        # 이미 해제·확인필요로 빠져 있는 축은 분류에 영향을 주지 않는다. 그걸 계속
        # '없는 분류코드'로 세면, 정리를 끝낸 뒤에도 경고가 영영 사라지지 않는다
        # (그 줄은 아래 suspect 목록에서 사람이 처리한다).
        if tax and axis_state(s, "doctype") == STATE_ACTIVE:
            ids = [x for x in (axis_value(s, "doctype") or []) if x]
            gone = [x for x in ids if x not in (tax.get("by_id") or {})]
            if gone:
                rep["orphan"].append({"file": f, "dc_ids": gone})

        for a in AXES:
            if axis_state(s, a) == STATE_SUSPECT:
                rep["suspect"].append({"file": f, "axis": a,
                                       "reason": hold_reason(s, a)})

    # 벡터 차원이 다수와 다른 줄 — 임베딩 모델을 갈아끼운 흔적이다.
    if dims:
        common = max(set(dims.values()), key=list(dims.values()).count)
        for s in seeds:
            d = dims.get(norm_file(s.get("file")))
            if d and d != common:
                rep["dim"].append({"file": s.get("file"), "dim": d, "common": common})

    # 벡터를 만든 판이 섞였는지 — 두 판은 추출기가 달라 같은 문서라도 벡터가
    # 완전히 같지 않다(실측 최악 코사인 0.980). 전파 문턱에는 안 걸렸지만,
    # 차원이 같아 위의 dim 점검에도 안 걸린다 — 이 칸이 유일한 단서다.
    # 한 종류뿐이면 알릴 것이 없으므로 비워 둔다(정상을 경고로 만들지 않는다).
    engines = {}
    for s in seeds:
        name = (s or {}).get("engine")
        if name:
            engines[name] = engines.get(name, 0) + 1
    rep["engine"] = engines if len(engines) > 1 else {}

    rep["conflict"] = _conflicting_pairs(seeds, conflict_sim)
    return rep


#------------------------------------------------------------------
# 사실상 같은데 등급이 다른 기준 문서 쌍 찾기
#=> 유사도 0.97 인 두 문서가 하나는 C, 하나는 O 로 등록돼 있으면 그 근처 문서는
#   전부 mixed 로 떨어져 전파가 사실상 꺼진다. 그 쌍을 찾아 준다.
#   문서 수가 많아도 감당되도록 행렬 한 번으로 계산한다.
#
# -in: seeds = 기준 문서 리스트
# -in: thr   = 같은 문서로 볼 유사도 임계
#
# -out: list = [{"a","b","sim","ga","gb"}] 유사도 내림차순(최대 50쌍)
# -out: error = 없음
#------------------------------------------------------------------
def _conflicting_pairs(seeds, thr):
    rows = [(s.get("file"), axis_value(s, "security"), s.get("vector"))
            for s in seeds
            if s.get("vector") and "security" in active_axes(s)]
    if len(rows) < 2:
        return []
    arr = np.asarray([r[2] for r in rows], dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    arr = arr / norms
    sims = arr @ arr.T
    out = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            s = float(sims[i, j])
            if s >= thr and rows[i][1] != rows[j][1]:
                out.append({"a": rows[i][0], "b": rows[j][0], "sim": s,
                            "ga": rows[i][1], "gb": rows[j][1]})
    out.sort(key=lambda d: -d["sim"])
    return out[:50]


#------------------------------------------------------------------
# 분류 결과를 보고 기준 문서에 의심 표시 붙이기(자동 동기화)
#=> 분류를 돌리면 어차피 전 문서를 훑으므로, 그 결과와 기준 문서를 대조하는 일은
#   공짜다. 원본이 바뀌었거나 사라진 기준 문서에 표시만 붙인다.
#   [중요] 절대 지우지 않는다. 기준 문서 한 건이 잘못 사라지면 그와 비슷한 문서
#   수십 건의 분류가 함께 흔들리므로, 오탐으로 지우는 쪽이 훨씬 비싸다.
#    1) 저장된 지문과 이번 해시가 다르면 두 축 모두 suspect(stale)
#    2) 원본이 없으면 suspect(missing) — 업로드 등록분은 제외
#    3) 지문이 다시 같아졌으면(사람이 되돌려 놓았으면) 표시를 푼다
#
# -in: seeds   = 기준 문서 리스트
# -in: records = 이번 분류 레코드 리스트(hash 포함되어야 판정 가능)
#
# -out: (new_seeds, events) = 갱신된 리스트, events 는
#        [{"file","axis","action","reason"}] (감사 로그로 남길 목록)
# -out: error = 없음
#------------------------------------------------------------------
def sync_with_records(seeds, records):
    by_file = {norm_file(r.get("file")): r for r in (records or [])}
    out, events = list(seeds), []
    for s in list(seeds):
        f = s.get("file")
        seed_hash = s.get("hash")
        rec = by_file.get(norm_file(f))
        now_hash = (rec or {}).get("hash")
        missing = (not _is_uploaded(s)) and (not os.path.isfile(str(f or "")))
        stale = bool(seed_hash and now_hash and seed_hash != now_hash)

        for a in AXES:
            st = axis_state(s, a)
            if st not in (STATE_ACTIVE, STATE_SUSPECT):
                continue          # 사람이 해제한 축은 건드리지 않는다
            reason = "missing" if missing else ("stale" if stale else "")
            cur_reason = hold_reason(s, a)
            if reason and (st != STATE_SUSPECT or cur_reason != reason):
                out = suspect_axis(out, f, a, reason)
                events.append({"file": f, "axis": a, "action": "suspect", "reason": reason})
            elif not reason and st == STATE_SUSPECT and cur_reason in ("stale", "missing"):
                # 원본이 제자리로 돌아왔다 → 표시를 풀어 다시 기준으로 쓴다.
                out = restore_axis(out, f, a)
                events.append({"file": f, "axis": a, "action": "restore", "reason": cur_reason})
    return out, events
