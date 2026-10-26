#------------------------------------------------------------------
# CSO 보안등급 분류 레코드 조립 (Signal A → 1차 분류 결과)
#=> CSO 보안등급(C/S/O)의 다섯 신호를 스캔·융합·조립하여 1차 분류 레코드를 완성한다.
#    1) 다섯 스캔 실행: 규칙(내용) + 민감정보 + 스탬프 + 경로 + 파일명
#    2) 스캔 결과를 fuse_signals 로 융합 → 보수적 최댓값 + fail-safe 적용
#    3) 감사·인덱싱에 쓰기 좋은 분류 레코드 dict 로 조립
#   [경계] 경로/ACL(Signal C)·임베딩 전파(Signal B)는 아직 없다 → 이 레코드는
#   그 신호들이 붙으면 fuse 단계에서 상향될 수 있는 "1차 등급"이다.
#
#   [업무분류 동시 계산] doc_rules/taxonomy 를 함께 주면 doctype 축도 더한다
#   (둘 다 없으면 doctype 키 자체를 생략 — 설계서 4-6: "빈 목록이 아니라
#   키 자체가 없다").
#
#   [2026-09-10 레코드 평탄화] labels 껍데기를 없애고 security·doctype 을 최상위
#   나란한 두 칸으로 올렸다. 예전에는 같은 값이 최상위와 labels.security 양쪽에
#   두 벌 있었고, 전파가 한쪽만 갱신하면 어긋날 수 있었다. 버전·시각은 meta 로
#   모았다 — '판정'과 '장부'를 눈으로 가르기 위해서다.
#------------------------------------------------------------------

import datetime
from types import SimpleNamespace

from .. import record
from .rules import scan_text, scan_stamp, scan_sensitive, collect_pii
from .context import scan_filename
from .fuse import fuse_signals
from .propagate import SeedIndex, propagate, propagate_doctype
from .doctype import scan_doctype, merge_embed_candidates, merge_second_stage


#------------------------------------------------------------------
# 현재 시각 ISO 문자열
#=> 레코드에 찍을 생성 시각을 지역시간 오프셋 포함 ISO8601(초 단위)로 만든다.
#   테스트는 시각을 직접 주입하므로 이 함수는 CLI 등 실사용에서만 쓴다.
#
# -in: 없음
#
# -out: str = 예 "2026-08-12T10:30:00+09:00"
# -out: error = 없음
#------------------------------------------------------------------
def now_iso():
    # astimezone() 로 로컬 타임존 오프셋을 붙여, 나중에 시점 비교가 명확하게 한다.
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


#------------------------------------------------------------------
# security(등급) 축 조립
#=> fuse_signals 결과(FusionResult)로부터 레코드의 security 칸을 만든다.
#
#   [2026-09-10 ①] candidates 를 뺐다 — signals 에서 "등급이 있는 것만" 추린
#   값이라 같은 내용을 두 번 적는 것이었다. 화면의 '다른 의견' 표시는
#   signals 를 직접 읽어 만든다(ui/app.py::render_other_opinions).
#
#   [2026-09-10 ②] labels 껍데기를 없애고 이 칸을 최상위로 올렸다. 예전에는
#   똑같은 값이 최상위(grade/confidence/method/decided_by)와 labels.security
#   양쪽에 두 벌 있었고, 전파가 한쪽만 갱신하면 두 값이 어긋날 수 있었다.
#   한 벌만 두면 어긋날 자리가 없다.
#   value → grade 로 이름을 바꿨다(최상위에서 쓰던 이름을 그대로 쓴다).
#   strategy 는 뺐다 — 등급 축은 늘 max(가장 민감한 등급 채택) 고정이라
#   문서마다 "max" 라고 적어도 새로 알려 주는 것이 없다.
#
#   [2026-09-10 ③] 등급값(grade)을 이 칸에서 뺐다. 판정은 레코드 맨 앞에
#   한 벌만 두고(rec["grade"]), 여기에는 그 판정을 뒷받침하는 것만 남긴다.
#   같은 값이 두 군데 있으면 한쪽만 갱신되는 사고가 가능해진다 — 3단계에서
#   없앤 구조가 바로 그것이었다.
#
# -in: fused   = fuse.FusionResult(fuse_signals 반환값)
# -in: signals = 이 문서의 신호별 근거 dict(그대로 이 칸 안에 넣는다)
#
# -out: dict = {confidence, method, decided_by, seed_eligible, signals}
# -out: error = 없음
#------------------------------------------------------------------
def _security_axis(fused, signals):
    return {
        "confidence": round(fused.confidence, 3),
        "method": fused.method,
        "decided_by": list(fused.decided_by),
        # 전파(Signal B)의 seed 수집 필터가 바로 읽는다.
        "seed_eligible": fused.seed_eligible,
        "signals": signals,
    }


#------------------------------------------------------------------
# 옛 모양의 칸 걷어내기
#=> 전파는 예전에 만든 결과 파일을 다시 읽어 돌릴 수 있다. 그때 새 칸(security·
#   doctype)만 채우고 옛 칸을 남겨 두면 한 레코드 안에 두 판의 답이 공존한다 —
#   읽는 쪽이 어느 쪽을 믿어야 할지 알 수 없고, 그 어긋남은 조용히 생긴다.
#   그래서 갱신을 끝낸 레코드에서는 옛 자리를 지운다.
#
# -in: rec = 갱신을 마친 레코드(제자리에서 고친다)
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def _drop_legacy(rec):
    # grade 는 3세대에서 다시 최상위 칸이 됐으므로 지우지 않는다.
    for k in ("confidence", "method", "decided_by", "seed_eligible", "signals",
              "security"):
        rec.pop(k, None)
    labels = rec.get("labels")
    if isinstance(labels, dict):
        labels.pop("security", None)
        labels.pop("doctype", None)
        if not labels:
            rec.pop("labels", None)


#------------------------------------------------------------------
# 아무것도 못 찾은 신호 걷어내기
#=> 등급도 못 정하고 걸린 규칙도 없는 신호는 "없음"이라는 말만 차지한다.
#   키가 없는 것이 곧 "그 신호는 아무것도 못 찾았다" 는 뜻이다.
#
#   [왜 안전한가] 이 값을 읽는 곳은 전부 .get()/키 존재 확인을 거친다 —
#   재융합(propagate)은 `if name in sig` 로 거르고, --simple-why 는
#   `if not val.get("grade"): continue` 로 건너뛴다. 빈 신호는 융합에
#   기여하는 바가 없어, 빠져도 판정이 달라지지 않는다.
#
# -in: sigs = {신호이름: as_dict() 결과}
#
# -out: dict = 등급이 있거나 걸린 규칙이 있는 신호만 남긴 새 dict
# -out: error = 없음
#------------------------------------------------------------------
def _nonempty_signals(sigs):
    return {n: s for n, s in sigs.items()
            if s.get("grade") is not None or s.get("hits")}


#------------------------------------------------------------------
# doctype 축 부착(있을 때만)
#=> doc_rules 와 taxonomy 가 '둘 다' 있어야 doctype 축을 계산한다(설계서
#   4-6·3-3 — 규칙은 있는데 어휘가 없거나 그 반대면 뜻이 완성되지 않는다).
#   하나라도 없으면 아무것도 하지 않는다 — 레코드에 "doctype" 키 자체가
#   생기지 않아야 "축을 안 씀"과 "분류 못 함"이 구분된다(4-6).
#
# -in: rec       = 지금까지 조립한 레코드 dict(security 축까지 채워진 상태)
# -in: text      = 정제된 문서 텍스트
# -in: file      = 파일 경로
# -in: doc_rules = doc_rules.DocRuleSet | None
# -in: taxonomy  = axes.Taxonomy | None
#
# -out: 없음(rec 를 제자리에서 갱신)
# -out: error = 없음
#------------------------------------------------------------------
def _attach_doctype(rec, text, file, doc_rules, taxonomy):
    if doc_rules is None or taxonomy is None:
        return

    # doc_rule.yaml 파일에 설정값을 읽어와서 스캔함.
    sig = scan_doctype(text, file, doc_rules, taxonomy)
    # 상세는 why 안에. 맨 앞의 dc_id 목록은 출력 직전에 여기서 뽑아 만든다
    # (전파가 후보를 더할 수 있어, 지금 만들어 두면 어긋난다).
    rec.setdefault("why", {})["doctype"] = sig.as_dict()


#------------------------------------------------------------------
# 분류 레코드 조립(핵심)
#=> 다섯 신호(규칙·민감정보·스탬프·경로·파일명)를 모아 융합하고, 설계서의 분류 레코드
#   스키마에 맞춰 dict 를 만든다.
#    1) 규칙 스캔(Signal A, 텍스트) + 민감정보군(Signal F, 법상 민감정보)
#       + 스탬프(Signal E, 보안분류 표식) + 경로(Signal C) + 파일명(Signal D)
#    2) fuse_signals 로 보수적 최댓값 + fail-safe 융합 → 최종 등급/근거
#    3) signals 에 다섯 신호의 근거를 모두 담아 감사 가능하게 한다
#   [프라이버시] 모든 신호 dict 는 원문 값이 없다(건수·규칙 id·경로 규칙명만).
#   [경계] 임베딩 전파(Signal B)는 아직 없다 → 붙으면 fuse 후보로 추가만 하면 된다.
#
# -in: file     = 문서 경로(식별 + 경로/파일명 신호의 입력)
# -in: text     = MpowerClassify 가 추출·정제한 텍스트(규칙 스캔 입력)
# -in: ruleset  = load_rules() 로 만든 RuleSet
# -in: ts       = 레코드 시각 문자열(없으면 None 으로 둠 — 테스트 결정성 위해 주입식)
# -in: failsafe = 어떤 신호도 없을 때 부여할 기본등급("S"/"C" 등, 없으면 None)
# -in: vector   = 이 문서의 임베딩 벡터(있으면 레코드에 실어 전파·인덱싱에 재사용)
# -in: embed_sig = 임베딩 전파 신호(EmbedSignal). 있으면 융합 후보로 추가(상향전용)
# -in: rules_enabled = True(기본)면 규칙 신호를 모두 돌린다. False 면(=벡터-only 분류)
#                      규칙 신호를 아예 돌리지 않고 '보류(grade=None)' 레코드만 만든다
#                      → 등급은 이후 임베딩 전파(propagate_records)가 seed 비교로 정한다.
# -in: doc_rules = doc_rules.DocRuleSet | None. taxonomy 와 '둘 다' 주면 doctype 축을
#                  함께 계산한다(하나라도 없으면 doctype 키 자체를 생략, 설계서 4-6)
# -in: taxonomy  = axes.Taxonomy | None
#
# -out: dict = {file,
#               security:{grade, confidence, method, decided_by, seed_eligible,
#                         signals:{rule,sensitive,stamp,name[,embed]}},
#               [doctype:{status, strategy, values, …},]
#               [vector,] meta:{rule_version[, doctype_rule_version,
#                               taxonomy_version], ts}}
# -out: error = 없음 (스캔·융합은 예외를 내지 않음)
#------------------------------------------------------------------
def build_record(file, text, ruleset, ts=None, failsafe=None, vector=None, embed_sig=None,
                 with_pii=False, rules_enabled=True, doc_rules=None, taxonomy=None):
    # [벡터-only 모드] 규칙 신호를 하나도 돌리지 않고 '보류' 레코드만 만든다. failsafe 도
    # 여기선 적용하지 않는다(전파가 못 정했을 때 propagate_records 재융합에서 적용됨).
    # doctype 축은 security 의 이 모드와 무관하게 독립적으로 켜진다(6-4 — 두 축은
    # 서로 다른 계산량·목적을 가지므로 한쪽을 껐다고 다른 쪽도 꺼질 이유가 없다).
    if not rules_enabled:
        rec = {
            "file": file,
            "grade": None,
            "why": {"security": {
                "confidence": 0.0, "method": "unclassified",
                "decided_by": [], "seed_eligible": False, "signals": {},
            }},
            "meta": {"ts": ts},
        }
        if vector is not None:
            rec["vector"] = list(vector)
        _attach_doctype(rec, text, file, doc_rules, taxonomy)
        return rec

    # cso_rule.yaml 기반으로 문서text를 스캔한다.
    rule_sig = scan_text(text, ruleset)       # Signal A (내용)
    sens_sig = scan_sensitive(text, ruleset)  # Signal F (법상 민감정보군)
    stamp_sig = scan_stamp(text, ruleset)     # Signal E (보안분류 스탬프)
    name_sig = scan_filename(file, ruleset)   # Signal D (파일명)

    # 이름표를 붙여 융합에 넘긴다(어떤 신호가 결정했는지 추적하기 위해).
    signals = [("rule", rule_sig), ("sensitive", sens_sig), ("stamp", stamp_sig),
               ("name", name_sig)]
    if embed_sig is not None:
        # embed 는 그냥 후보로 추가만 하면, 융합이 max 라 자동으로 "상향 전용"이 된다.
        signals.append(("embed", embed_sig))
    fused = fuse_signals(signals, failsafe=failsafe)

    # 아무것도 못 찾은 신호는 적지 않는다(2026-09-10). 예전에는 네 신호를
    # 늘 적어, 셋이 {grade:null, hits:[]} 로 "없음"만 말하는 일이 흔했다.
    # 없는 키 = 그 신호가 아무것도 못 찾음. 읽는 쪽은 전부 .get() 이라 안전하다.
    sigs = _nonempty_signals({
        "rule": rule_sig.as_dict(),
        "sensitive": sens_sig.as_dict(),
        "stamp": stamp_sig.as_dict(),
        "name": name_sig.as_dict(),
    })
    if embed_sig is not None:
        sigs["embed"] = embed_sig.as_dict()

    # 축(security/doctype)을 나란한 두 칸으로 두고, 판정에 안 쓰이는 기록용
    # 값(버전·시각)은 meta 로 모은다 — 읽는 사람이 "판정"과 "장부"를 헷갈리지
    # 않게 하려는 것이다(2026-09-10).
    rec = {
        "file": file,
        # 판정을 맨 앞에 — 사람이 파일을 열면 가장 먼저 보고 싶은 값이다.
        "grade": fused.grade,
        "why": {"security": _security_axis(fused, sigs)},
        # 규칙셋 버전 세 칸은 레코드마다 적지 않는다(2026-09-10). 한 번 실행하면
        # 모든 줄이 같은 값이라, 결과 파일 맨 앞의 실행 헤더가 한 번만 적는다.
        # ts 는 줄마다 다를 수 있어(문서 하나를 처리한 시각) 여기 남는다.
        "meta": {"ts": ts},
    }

    # 업무분류 룰 분류 수행
    #=>doc_rules.yaml 이용해 어무 1차분류 수행
    _attach_doctype(rec, text, file, doc_rules, taxonomy)


    # 벡터는 전파(2차)와 RAG 인덱싱에 재사용하도록 레코드에 그대로 싣는다.
    if vector is not None:
        rec["vector"] = list(vector)
    # [프라이버시 예외] --with-pii 로 명시 요청 시에만 검출된 원문 PII 값을 싣는다.
    # 기본은 건수만(원문 미저장). 로그엔 안 남기고(_loggable_record 가 가림) --out 파일에만.
    if with_pii:
        rec["pii"] = collect_pii(text, ruleset)
    return rec


#------------------------------------------------------------------
# 저장된 신호 dict → 융합용 객체
#=> 1차 분류 레코드의 signals[name] dict 를 fuse_signals 가 읽을 수 있는 최소
#   속성 객체로 되살린다(재융합 때 사용).
#
# -in: d = 신호 dict (grade/confidence/seed_eligible)
#
# -out: SimpleNamespace = .grade/.confidence/.seed_eligible
# -out: error = 없음
#------------------------------------------------------------------
def _ns_from_signal(d):
    return SimpleNamespace(
        grade=d.get("grade"),
        confidence=d.get("confidence", 0.0),
        seed_eligible=d.get("seed_eligible", False),
    )


#------------------------------------------------------------------
# 2차 패스 진행 알리기
#=> 전파 루프가 레코드 하나를 볼 때마다 호출자에게 알린다. 화면 진행바와
#   콘솔의 [progress] 줄이 이것으로 움직인다.
#   [왜 감싸나] 알림은 부속이지 판정이 아니다. 콜백이 터졌다고 분류가
#   멈추면 안 되므로 예외를 삼킨다 — 진행 표시를 못 하는 것과 결과를 못
#   내는 것은 무게가 전혀 다르다.
#
# -in: cb    = 호출자가 준 함수 f(본수, 전체, 파일경로). None 이면 아무 일도 안 한다
# -in: done  = 지금까지 본 레코드 수(1부터)
# -in: total = 전체 레코드 수
# -in: rec   = 지금 보고 있는 레코드(파일 경로를 꺼내는 데만 쓴다)
#
# -out: 없음
# -out: error = 없음(콜백이 던진 예외는 삼킨다)
#------------------------------------------------------------------
def _tell(cb, done, total, rec):
    if cb is None:
        return
    try:
        cb(done, total, (rec or {}).get("file") or "")
    except Exception:
        pass


#------------------------------------------------------------------
# 배치 임베딩 전파(Signal B, 2차 패스) — 핵심
#=> 벡터가 실린 1차 레코드들을 받아, 고신뢰 등급을 seed 로 삼아 "보류(grade=None)"
#   문서에만 라벨을 전파한다. 이미 등급이 확정된 문서는 손대지 않는다.
#    1) seed 인덱스 구성(seed_eligible + 등급 + 벡터) — 모든 레코드에서 씨앗 수집
#    2) grade=None(보류) 레코드만 propagate → embed 신호
#    3) rule/path/name(+embed) 재융합 → 등급/근거 갱신, labels.security 도 함께 갱신
#   [doctype 은 손대지 않는다] 이 함수는 security 축 전용이다. doctype 축 전파는
#   라벨별 독립 점수가 필요해 알고리즘 자체가 다르다(설계서 5-4, 로드맵 D7) — 아직
#   없으므로 레코드에 이미 있던 labels.doctype 은 그대로 통과한다.
#   [왜 보류만?] 임베딩은 주제는 잡아도 민감도 자체는 보증 못 한다(공개문서가
#   기밀문서와 주제상 이웃일 수 있음). 그래서 규칙/경로로 이미 확정된 등급을
#   임베딩으로 흔들지 않고(공개문서 과분류 방지), 근거가 없어 보류된 문서를
#   구제하는 데만 쓴다. seed 가 자기 자신을 이웃으로 잡는 자기매칭도 사라진다.
#
# -in: records    = 1차 분류 레코드 리스트(각 rec 에 'grade','vector')
# -in: seed_index = 외부 큐레이션 seed(class_seed.jsonl 로 만든 것). 주면 코퍼스 내부
#                   seed(seed_eligible 문서)와 "합쳐서" 비교 기준으로 쓰고, None 이면
#                   내부 seed 만 쓴다.
# -in: failsafe   = 재융합 시에도 무신호면 부여할 기본등급(없으면 None)
# -in: on_progress = 레코드 하나를 볼 때마다 부를 함수 f(본수, 전체, 파일경로).
#                    None 이면 알리지 않는다. 건너뛴 레코드에서도 부른다 —
#                    세는 수가 끊기면 진행바가 중간에서 멈춘 것처럼 보인다
# -in: kw         = propagate() 임계값 override(k/dup_threshold/min_sim/min_share)
#
# -out: (records, stats) = 갱신된 레코드들, 통계 dict
#       stats = {seeds, already_graded, embed_decided, still_unclassified, no_vector}
# -out: error = 없음
#------------------------------------------------------------------
def propagate_records(records, seed_index=None, failsafe=None,
                      on_progress=None, **kw):
    # 비교 기준 seed = 코퍼스 내부 seed(seed_eligible 문서) + (있으면) 외부 큐레이션 seed.
    # 외부 seed 를 주면 '외부 + 내부' 양쪽과 비교하고(사용자 정책), 없으면 내부만 쓴다.
    # => cso_rule.yaml에서 고신뢰(seed_eligible) 설정된 경우에 대해 class_seed.jsonl 만듬 
    internal = SeedIndex.from_records(records)
    if seed_index is not None and seed_index.size:
        seeds = SeedIndex.merge(seed_index, internal)
    else:
        seeds = internal
    stats = {"seeds": seeds.size, "already_graded": 0,
             "embed_decided": 0, "still_unclassified": 0, "no_vector": 0}

    total = len(records)
    for done, rec in enumerate(records, 1):
        # 이 레코드를 건너뛰든 아니든 진행은 알린다(아래 continue 들 때문에 try 로 감싼다).
        _tell(on_progress, done, total, rec)
        # 새 모양은 why.security, 옛 결과 파일은 최상위/labels — 둘 다 읽는다.
        if "security" not in rec.get("why", {}):
            old = record.security_of(rec)
            rec["grade"] = old.pop("grade", None)
            rec.setdefault("why", {})["security"] = old
        sec = rec["why"]["security"]
        # 이미 등급이 확정된 문서는 전파 대상이 아니다 → 그대로 둔다(과분류 방지).
        if record.grade_of(rec) is not None:
            stats["already_graded"] += 1
            continue

        vec = rec.get("vector")
        if vec is None:
            # 보류인데 벡터도 없으면 전파 불가 → 여전히 미분류.
            stats["no_vector"] += 1
            continue

        esig = propagate(vec, seeds, **kw)
        sig = sec.setdefault("signals", {})
        sig["embed"] = esig.as_dict()

        # 저장된 rule/sensitive/stamp/path/name 신호를 되살려 embed 와 함께 재융합.
        parts = [(name, _ns_from_signal(sig[name]))
                 for name in ("rule", "sensitive", "stamp", "name") if name in sig]
        parts.append(("embed", esig))
        fused = fuse_signals(parts, failsafe=failsafe)

        # 등급 축을 통째로 다시 만든다 — 예전에는 최상위와 labels.security
        # 양쪽에 같은 값을 두 벌 적어야 했고, 한쪽만 갱신하면 어긋났다.
        # 이제 한 벌뿐이라 어긋날 자리가 없다(2026-09-10).
        rec["grade"] = fused.grade
        rec["why"]["security"] = _security_axis(fused, sig)
        # 옛 레코드를 그대로 받았을 수 있다 — 두 모양이 한 파일에 섞이면 읽는
        # 쪽이 어느 쪽을 믿어야 할지 알 수 없으므로, 갱신한 김에 옛 칸을 걷어낸다.
        _drop_legacy(rec)

        if "embed" in fused.decided_by:
            stats["embed_decided"] += 1
        if fused.grade is None:
            stats["still_unclassified"] += 1

    return records, stats


#------------------------------------------------------------------
# 배치 임베딩 전파 — doctype 축(Signal B, 로드맵 D7)
#=> security 의 propagate_records() 와 같은 2차 패스 구조이지만, 대상 선정
#   기준이 다르다 — security 는 "아직 등급 없는(grade=None) 문서만" 구제하지만,
#   doctype 은 다중 라벨이라 이미 규칙으로 후보가 붙은 문서에도 embed 가 새
#   후보를 "더할" 수 있다(설계서 5-4 — 상향 전용이 아니라 추가 전용). 그래서
#   "이미 채워졌으면 건너뛴다"는 조건 자체가 없다 — labels.doctype 축이 켜진
#   문서라면(=T14/T16 을 통과해 이 축을 쓰는 배포라면) 전부 시도한다.
#    1) labels.doctype 이 없는 레코드(그 축을 안 쓴 배포)는 건너뛴다
#    2) 벡터 없으면 전파 불가 → 건너뛴다
#    3) propagate_doctype 으로 embed 후보를 찾고, merge_embed_candidates 로
#       기존(규칙) 후보와 합쳐 조상 흡수·축 전략을 다시 적용
#   [seed 승격 없음] doctype 축은 seed_eligible 개념이 없다 — 전파로 붙은 라벨은
#   그 자체로 "제안"일 뿐이고, seed 로 쓰이려면 사람이 확정(D6)한 뒤 별도로
#   seed 저장소에 승격돼야 한다(설계서 5-4 선순환 — "확정본이 seed 가 됨").
#
# -in: records   = build_record(doc_rules=…, taxonomy=…) 로 만든 1차 레코드 리스트
# -in: dt_seeds  = propagate.DoctypeSeedIndex(class_seed.jsonl 의 labels.doctype 로 구성)
# -in: taxonomy  = axes.Taxonomy(경로·조상 계산에 필요)
# -in: conflict  = doc_rules.ConflictSpec(축 전략 — merge 후 재적용할 때 필요)
# -in: embed_cap = 벡터 '단독' 후보의 신뢰도 상한(재설계 9-3a). None 이면 상한
#                  없음(종전 동작). doc_rule.yaml 의 defaults.embed_cap 을
#                  호출자(cli.py)가 그대로 넘겨 주면 된다
# -in: on_progress = 레코드 하나를 볼 때마다 부를 함수 f(본수, 전체, 파일경로).
#                    None 이면 알리지 않는다(propagate_records 와 같은 약속)
# -in: kw        = propagate_doctype() 임계값 override(k/dup_threshold/min_sim/min_share)
# -in: use_embed = False 면 벡터 비교를 하지 않는다(embed.enabled=false · BM25 만 켤 때)
# -in: bm25      = BM25 묶음 dict 또는 None —
#                  {index: bm25.Bm25Index, spec: Bm25Spec, threshold: 문턱(None 이면 안 함),
#                   noise: noise_tails, fields: {레코드 번호: (제목, 파일명에 쓸 경로)}}
#                  fields 에 든 레코드(= 2차 대상)만 BM25 를 본다
#
# -out: (records, stats) = 갱신된 레코드들, 통계 dict
#       stats = {seeds, embed_contributed, no_vector, axis_off,
#                bm25_picked, bm25_agree, bm25_alone, bm25_name_only}
# -out: error = 없음
#------------------------------------------------------------------
def propagate_doctype_records(records, dt_seeds, taxonomy, conflict,
                              embed_cap=None, on_progress=None, use_embed=True, bm25=None, **kw):
    from . import bm25 as B
    stats = {"seeds": dt_seeds.size if dt_seeds else 0,
             "embed_contributed": 0, "no_vector": 0, "axis_off": 0,
             "bm25_picked": 0, "bm25_agree": 0, "bm25_alone": 0, "bm25_name_only": 0}
    bm_on = bool(bm25) and bm25.get("threshold") is not None

    total = len(records)
    for done, rec in enumerate(records, 1):
        _tell(on_progress, done, total, rec)
        dt = record.doctype_of(rec)
        if dt is None:
            stats["axis_off"] += 1
            continue

        vec = rec.get("vector") if use_embed else None
        flds = bm25["fields"].get(done - 1) if bm_on else None
        if vec is None and flds is None:
            stats["no_vector"] += 1
            continue

        esig = propagate_doctype(vec, dt_seeds, **kw) if vec is not None else None
        pick = None
        if flds is not None:
            # 제목 · 파일명으로 BM25 1위를 고른다 — 파일명에서만 맞은 후보는 설정에 따라 버린다
            sp = bm25["spec"]
            q = B.field_tokens(flds[0], flds[1], sp, bm25["noise"])
            rk = bm25["index"].rank(q)
            top = B.pick(rk, bm25["threshold"], sp.min_margin)
            if top:
                part = rk.parts.get(top, {})
                agree = esig is not None and any(v["dc_id"] == top for v in esig.values)
                if part.get("title", 0.0) > 0 or agree or sp.name_only_allow:
                    pick = {"dc_id": top, "score": rk.ranked[0][1], "threshold": bm25["threshold"],
                            "title": part.get("title", 0.0), "name": part.get("name", 0.0),
                            "words": rk.words.get(top, [])}

        if esig is None and pick is None:
            # 벡터도 BM25 1위도 없으면 1차 결과를 그대로 둔다 — 다시 마무리하면 truncated 표시를
            # 잃는다(Rust 판도 이 경우 손대지 않는다)
            if vec is None:
                stats["no_vector"] += 1
            continue

        existing = dt.get("values") or []
        merged_signal = merge_second_stage(existing, esig, taxonomy, conflict, embed_cap=embed_cap,
                                           bm25_pick=pick, bm25_spec=bm25["spec"] if bm25 else None)
        merged = merged_signal.as_dict()
        # 전파 신호는 그 축 안에 둔다 — 예전에는 등급 신호들과 같은 signals
        # 상자에 doctype_embed 라는 이름으로 섞여 있어, 축이 둘이라는 사실이
        # 레코드 모양에서 드러나지 않았다(2026-09-10).
        if esig is not None:
            merged["embed"] = esig.as_dict()
        rec.setdefault("why", {})["doctype"] = merged
        rec.pop("doctype", None)      # 옛 세대가 여기에 두던 자리
        _drop_legacy(rec)

        if any("embed" in v["from"] for v in merged_signal.values):
            stats["embed_contributed"] += 1
        if pick:
            stats["bm25_picked"] += 1
            got = next((v for v in merged_signal.values if v["dc_id"] == pick["dc_id"]), None)
            if got is not None and "bm25" in got["from"]:
                if got.get("basis") == "bm25_name_only":
                    stats["bm25_name_only"] += 1
                elif "embed" in got["from"]:
                    stats["bm25_agree"] += 1
                elif got.get("basis") == "bm25":
                    stats["bm25_alone"] += 1

    return records, stats
