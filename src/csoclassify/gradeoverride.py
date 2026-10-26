# -*- coding: utf-8 -*-
#------------------------------------------------------------------
# 사람이 정한 보안등급(결정 목록) 읽기 · 적용 · 덧붙이기
#=> UI 검토함이 쌓는 cso_override.jsonl 을 엔진도 그대로 읽어, 판정이 끝난 뒤
#   사람 결정이 판정을 이기게 한다(--grade-overrides). --set-grade 는 파일 하나에
#   바로 등급을 정하고, 원하면 같은 목록에 한 줄을 덧붙인다.
#   설계: plan/사람등급지정-수동오버라이드-설계-20261020.html
#   찾는 순서 · 정규화는 ui/docidkey.py 와 같다(UI 화면의 최종 등급과 엔진 결과가 같아야 한다).
#   Rust 판 gradeoverride.rs 와 같은 계약이다 — 한쪽을 고치면 다른 쪽도 고친다.
#------------------------------------------------------------------
import datetime
import json
import os

# 등급 엄격도 — 내용이 바뀐 문서에 사람 결정을 따를지 정할 때 쓴다(§05).
_RANK = {"C": 3, "S": 2, "O": 1, None: 0}


#------------------------------------------------------------------
# 경로 정규화 — ui/docidkey.normalize_key 와 같은 규칙
#=> 구분자(\ → /) · '..' · 드라이브 문자 대소문자 차이를 없앤다. UNC 앞 // 는 지킨다.
#
# -in: path = 파일 경로
#
# -out: str = 정규화한 키(빈 경로면 "")
# -out: error = 없음
#------------------------------------------------------------------
def normalize_key(path):
    if not path:
        return ""
    s = str(path).strip().replace("\\", "/")
    # UNC(//server/share)의 앞 슬래시 두 개는 뜻이 있어 따로 떼어 둔다
    unc = s.startswith("//")
    body = s[2:] if unc else s
    body = os.path.normpath(body).replace("\\", "/")
    if unc:
        body = "//" + body.lstrip("/")
    if len(body) >= 2 and body[1] == ":" and body[0].isalpha():
        body = body[0].upper() + body[1:]
    return body


#------------------------------------------------------------------
# 결정 값 → 등급
#=> "C"/"S"/"O" 는 그대로, "보류" · "NONE" · 빈 값 · null 은 등급 없음(None).
#
# -in: v = 결정 기록의 new_grade 값
#
# -out: "C" | "S" | "O" | None
# -out: error = 없음
#------------------------------------------------------------------
def norm_grade(v):
    s = (str(v).strip().upper() if v is not None else "")
    return s if s in ("C", "S", "O") else None


#------------------------------------------------------------------
# 결정 목록 색인
#=> 파일별 최신 결정(나중 줄 우선)을 담고, doc_id · 지문 · 정규화 경로 · 대소문자 무시
#   경로로 찾을 수 있게 한다(ui/docidkey.OverrideIndex 와 같은 순서).
#
# -필드: latest  = {파일경로: 최신 결정 dict}
# -필드: broken  = 읽지 못한 줄 수
# -필드: path    = 결정 목록 파일 경로
#------------------------------------------------------------------
class OverrideIndex:
    #--------------------------------------------------------------
    # 색인 만들기
    #=> 같은 문서의 여러 결정 중 마지막 것만 남기고, 찾기용 사전 넷을 만든다.
    #
    # -in: latest = {파일경로: 결정}
    # -in: broken = 깨진 줄 수
    # -in: path   = 원본 파일 경로
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self, latest=None, broken=0, path=None):
        self.latest = dict(latest or {})
        self.broken = broken
        self.path = path
        self.by_doc_id, self.by_hash, self.by_key = {}, {}, {}
        fold = {}
        for f, e in self.latest.items():
            did = e.get("doc_id")
            if did:
                self.by_doc_id.setdefault(did, f)
            h = e.get("hash")
            if h:
                self.by_hash.setdefault(h, f)
            k = normalize_key(f)
            self.by_key.setdefault(k, f)
            fold.setdefault(k.casefold(), []).append(f)
        # 대소문자만 다른 경로가 둘 이상이면 어느 쪽인지 모르므로 쓰지 않는다
        self.by_fold = {k: v[0] for k, v in fold.items() if len(v) == 1}

    #--------------------------------------------------------------
    # 레코드 → 결정 찾기
    #=> ① doc_id ② 내용 지문(옛 기록의 '지문 앞 40자' doc_id 포함) ③ 경로 정확 일치
    #   ④ 정규화 경로 ⑤ 대소문자 무시 순서로 찾는다.
    #
    # -in: rec = 결과 레코드(file · doc_id · hash 를 본다)
    #
    # -out: (결정 dict, how) = how 는 "doc_id"|"hash"|"path"|"case". 없으면 (None, None)
    # -out: error = 없음
    #--------------------------------------------------------------
    def lookup(self, rec):
        rec = rec or {}
        f = rec.get("file")
        did = rec.get("doc_id")
        if did and did in self.by_doc_id:
            return self.latest[self.by_doc_id[did]], "doc_id"
        h = rec.get("hash")
        if h:
            if h in self.by_hash:
                return self.latest[self.by_hash[h]], "hash"
            # 2026-09-10 이전 기록은 doc_id 칸에 지문 앞 40자가 들어 있다
            if h[:40] in self.by_doc_id:
                return self.latest[self.by_doc_id[h[:40]]], "hash"
        if f in self.latest:
            return self.latest[f], "path"
        k = normalize_key(f)
        if k in self.by_key:
            return self.latest[self.by_key[k]], "path"
        f2 = self.by_fold.get(k.casefold())
        if f2 is not None:
            return self.latest[f2], "case"
        return None, None

    #--------------------------------------------------------------
    # 지문으로 찾을 일이 있는가
    #=> 결정 중 하나라도 hash 를 갖고 있으면 레코드에도 지문이 있어야 잇거나 내용 변경을 안다.
    #
    # -in: 없음
    #
    # -out: bool
    # -out: error = 없음
    #--------------------------------------------------------------
    def needs_hash(self):
        return bool(self.by_hash)


#------------------------------------------------------------------
# 결정 목록 파일 읽기
#=> axis 가 "security" 이거나 없는(옛) 줄만 읽고, 같은 문서는 나중 줄이 유효하다.
#   업무분류 결정(axis:"doctype")은 건너뛴다. 깨진 줄은 세기만 하고 넘어간다.
#
# -in: path = cso_override.jsonl 경로
#
# -out: OverrideIndex
# -out: error = 파일을 못 열면 OSError 전파(없는 파일 검사는 부르는 쪽이 먼저 한다)
#------------------------------------------------------------------
def load(path):
    latest, broken = {}, 0
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                broken += 1
                continue
            if not isinstance(e, dict):
                broken += 1
                continue
            # axis 가 없는 줄은 두 축이 나뉘기 전의 옛 기록 = 보안등급이다
            if e.get("axis", "security") != "security":
                continue
            file = e.get("file")
            if not file:
                broken += 1
                continue
            latest[file] = e
    return OverrideIndex(latest, broken, path)


#------------------------------------------------------------------
# 결정 하나를 레코드에 적용
#=> 설계 §03 · §05 · §06.
#    1) kind:"confirm"(판정이 맞다고 확인만 함) → 등급은 그대로, 확인 표시만
#    2) 경로로 찾았는데 결정 때의 지문과 지금 지문이 다르면(내용이 바뀜) → 사람 등급이
#       판정보다 엄격할 때만 따르고, 느슨하면 따르지 않고 stale 로 표시
#    3) 그 밖 → 등급을 사람 등급으로 바꾸고, 원래 판정은 why.security.computed 에 남긴다
#
# -in: rec   = 결과 레코드(제자리에서 고친다)
# -in: entry = 결정 dict
# -in: how   = 찾은 방법
#
# -out: str = "applied" | "confirmed" | "stale"
# -out: error = 없음
#------------------------------------------------------------------
def apply(rec, entry, how):
    computed = norm_grade(rec.get("grade"))
    new = norm_grade(entry.get("new_grade"))
    kind = entry.get("kind") or "change"
    eh, rh = entry.get("hash"), rec.get("hash")
    # 지문으로 찾았으면 같은 내용이다. 다른 길로 찾았는데 지문이 다르면 내용이 바뀐 것
    stale = bool(how != "hash" and eh and rh and eh != rh)
    info = {"reviewer": entry.get("reviewer"), "reason": entry.get("reason"),
            "ts": entry.get("ts"), "matched_by": how, "kind": kind}
    why = rec.setdefault("why", {})
    sec = why.get("security") if isinstance(why.get("security"), dict) else {}
    if kind == "confirm":
        info["applied"] = False
        if stale:
            info["stale"] = True
        sec = dict(sec)
        sec["override"] = info
        why["security"] = sec
        return "confirmed"
    if stale and _RANK[new] < _RANK[computed]:
        # 바뀐 내용에 새 위험이 들어갔을 수 있다 — 사람 결정으로 등급을 낮추지 않는다
        info.update(applied=False, stale=True, grade=new)
        sec = dict(sec)
        sec["override"] = info
        why["security"] = sec
        return "stale"
    info["applied"] = True
    if stale:
        info["stale"] = True
    computed_sec = dict(sec)
    computed_sec["grade"] = computed
    rec["grade"] = new
    why["security"] = {"method": "manual_override", "decided_by": ["reviewer"],
                       "override": info, "computed": computed_sec}
    return "applied"


#------------------------------------------------------------------
# 결정 목록에 한 줄 덧붙이기(--set-grade)
#=> UI 의 append_override 와 같은 칸으로 쓴다(같은 파일을 함께 쓰므로). via 로 출처를 남긴다.
#
# -in: path      = 결정 목록 경로(없으면 만든다)
# -in: file      = 문서 경로
# -in: old_grade = 바꾸기 전 등급(지금 파일 스트림 값, 모르면 None)
# -in: new_grade = "C"|"S"|"O"|None(None 은 "보류"로 쓴다)
# -in: reason    = 사유
# -in: reviewer  = 검토자
# -in: doc_hash  = 내용 지문
#
# -out: dict = 덧붙인 결정
# -out: error = 쓰기 실패 시 OSError 전파
#------------------------------------------------------------------
def append_entry(path, file, old_grade, new_grade, reason, reviewer, doc_hash=None):
    entry = {
        "file": file,
        "doc_id": None,
        "hash": doc_hash,
        "axis": "security",
        "kind": "change",
        "old_grade": old_grade if old_grade is not None else "보류",
        "new_grade": new_grade if new_grade is not None else "보류",
        "reason": reason,
        "reviewer": reviewer,
        "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "via": "cli",
    }
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


#------------------------------------------------------------------
# 한 실행의 적용기
#=> 레코드마다 한 번만 적용하고(같은 레코드가 여러 길로 와도), 건수를 센다.
#
# -필드: index  = OverrideIndex
# -필드: counts = loaded / applied / confirmed / stale
#------------------------------------------------------------------
class Applier:
    #--------------------------------------------------------------
    # 적용기 만들기
    #
    # -in: index = OverrideIndex
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self, index):
        self.index = index
        self.counts = {"loaded": len(index.latest), "applied": 0, "confirmed": 0, "stale": 0}
        self._done = set()

    #--------------------------------------------------------------
    # 레코드 하나에 적용
    #=> 결정이 있으면 apply() 하고 (바뀌기 전 등급, 바뀐 뒤 등급)을 돌려준다 —
    #   부르는 쪽이 등급별 건수를 고칠 수 있게. 결정이 없거나 이미 처리했으면 None.
    #
    # -in: rec = 결과 레코드
    #
    # -out: (old, new) | None
    # -out: error = 없음
    #--------------------------------------------------------------
    def __call__(self, rec):
        if rec is None or id(rec) in self._done:
            return None
        self._done.add(id(rec))
        entry, how = self.index.lookup(rec)
        if entry is None:
            return None
        old = rec.get("grade")
        result = apply(rec, entry, how)
        self.counts[result] = self.counts.get(result, 0) + 1
        return old, rec.get("grade")

    #--------------------------------------------------------------
    # 요약에 실을 집계
    #
    # -in: 없음
    #
    # -out: dict
    # -out: error = 없음
    #--------------------------------------------------------------
    def summary(self):
        return dict(self.counts)
