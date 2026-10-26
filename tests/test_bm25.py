"""업무분류 2차 BM25 핵심 함수 시험 — Rust/src/bm25.rs 의 시험과 같은 입력 · 같은 기대값."""
from csoclassify.classify import bm25 as B


#------------------------------------------------------------------
# 시험용 기준 문서 줄 만들기
#
# -in: file   = 파일 경로
# -in: dc     = 분류 id
# -in: title  = 제목
# -in: h      = 지문
#
# -out: dict = class_seed.jsonl 한 줄 모양
# -out: error = 없음
#------------------------------------------------------------------
def _row(file, dc, title="", h=""):
    return {"file": file, "doctype": [dc], "title": title, "hash": h}


#------------------------------------------------------------------
# 분류 4개 × 3건 기준 문서 묶음(최소 참여 조건을 채움)
#
# -in: 없음
#
# -out: list[dict] = 기준 문서 줄 12개
# -out: error = 없음
#------------------------------------------------------------------
def _rows():
    out = []
    for dc, word in (("M", "매뉴얼"), ("R", "보고서"), ("C", "계약서"), ("P", "제안서")):
        for i in range(3):
            out.append(_row(f"D:/x/{word}_{i}.docx", dc, f"시스템 {word} {i}", f"{dc}{i}"))
    return out


def test_토큰_2gram과_단어():
    assert B.tokenize("사용자매뉴얼") == ["w:사용자매뉴얼", "g:사용", "g:용자", "g:자매", "g:매뉴", "g:뉴얼"]
    # 영문은 단어만, 숫자만 · 1글자 · 불용어는 버린다
    assert B.tokenize("Mpower 2023 a 목차 EZisC", B.Bm25Spec().stopwords) == ["w:mpower", "w:ezisc"]


def test_자간_벌린_말_붙이기():
    assert B.despace_spaced("출 장 여 비 규 정") == "출장여비규정"
    # 두 글자만 띄운 것은 그대로
    assert B.despace_spaced("가 나 보고서") == "가 나 보고서"
    assert B.tokenize("출 장 여 비 규 정")[0] == "w:출장여비규정"


def test_파일명_정리():
    name = B.file_stem(r"D:\a\05.파일공유시스템_사용자매뉴얼(접속기)_V2.0_최종본.doc")
    assert name == "05.파일공유시스템_사용자매뉴얼(접속기)_V2.0_최종본"
    cleaned = B.clean_name(name, ["최종본"])
    assert B.tokenize(cleaned)[:1] == ["w:파일공유시스템"]
    assert "w:사용자매뉴얼" in B.tokenize(cleaned)
    assert not any("접속기" in t or "v2" in t for t in B.tokenize(cleaned))


def test_제목은_ppt_xls_에서_비운다():
    assert B.title_of("\n\n 첫 줄 제목\n둘째", "a.docx") == " 첫 줄 제목"
    assert B.title_of("첫 줄", "a.pptx") == "첫 줄"
    assert B.title_of("첫 줄", "a.ppt") == ""
    assert B.title_of("첫 줄", "a.xlsx") == ""


def test_분류_단위_채점과_고르기():
    spec = B.Bm25Spec(enabled=True)
    idx = B.Bm25Index(_rows(), spec)
    assert idx.size() == 12
    q = B.field_tokens("시스템 매뉴얼 설치", "D:/y/관리자매뉴얼.docx", spec)
    rk = idx.rank(q)
    assert rk.ranked[0][0] == "M"
    assert rk.ranked[0][1] > rk.ranked[1][1]
    assert rk.parts["M"]["title"] > 0 and rk.parts["M"]["name"] > 0
    assert B.pick(rk, 0.0) == "M"
    assert B.pick(rk, rk.ranked[0][1] + 1) is None


def test_참여_분류가_모자라면_빈_결과():
    spec = B.Bm25Spec(enabled=True)
    rows = [r for r in _rows() if r["doctype"][0] != "P"]
    rk = B.Bm25Index(rows, spec).rank(B.field_tokens("매뉴얼", "a.docx", spec))
    assert rk.ranked == []


#------------------------------------------------------------------
# 합치기 시험용 분류 체계(루트 아래 잎 셋)
#
# -in: 없음
#
# -out: axes.Taxonomy
# -out: error = 없음
#------------------------------------------------------------------
def _tax():
    from csoclassify.classify import axes as A
    return A.Taxonomy("t", "20260824000000", 4, [
        A.TaxonomyNode(dc_id="ROOT", parent=None, order=1, title="루트", status=1),
        A.TaxonomyNode(dc_id="L1", parent="ROOT", order=1, title="잎1", status=1),
        A.TaxonomyNode(dc_id="L2", parent="ROOT", order=2, title="잎2", status=1),
        A.TaxonomyNode(dc_id="L3", parent="ROOT", order=3, title="잎3", status=1),
    ])


#------------------------------------------------------------------
# BM25 1위 dict 만들기
#
# -in: dc    = 분류 id
# -in: title = 제목 필드 점수
#
# -out: dict = merge_second_stage 의 bm25_pick
# -out: error = 없음
#------------------------------------------------------------------
def _pick(dc, title=3.0):
    return {"dc_id": dc, "score": 20.0, "threshold": 15.0, "title": title, "name": 2.0, "words": ["매뉴얼"]}


def test_합치기_네_가지_경우():
    from csoclassify.classify import doctype as DT
    from csoclassify.classify import doc_rules as D
    from csoclassify.classify.propagate import DoctypeEmbedSignal
    tax, spec, cf = _tax(), B.Bm25Spec(enabled=True), D.ConflictSpec(strategy="all")
    rule = ({"dc_id": "L1", "path": tax.path("L1"), "path_ids": tax.path_ids("L1"),
             "confidence": 0.8, "from": ("title",)},)
    esig = DoctypeEmbedSignal(values=({"dc_id": "L2", "confidence": 0.5},), method="knn_vote", top_sim=0.8)
    # 1) 규칙이 이미 낸 분류 → 근거만, 확신 그대로
    v = {x["dc_id"]: x for x in DT.merge_second_stage(rule, None, tax, cf, bm25_pick=_pick("L1"),
                                                       bm25_spec=spec).as_dict()["values"]}
    assert v["L1"]["confidence"] == 0.8 and "bm25" in v["L1"]["from"] and v["L1"]["stage"] == "both"
    # 2) 벡터만 낸 분류 → 합의, cap_agree
    v = {x["dc_id"]: x for x in DT.merge_second_stage(rule, esig, tax, cf, embed_cap=0.65,
                                                       bm25_pick=_pick("L2"), bm25_spec=spec).as_dict()["values"]}
    assert v["L2"]["confidence"] == 0.75 and v["L2"]["stage"] == "embed+bm25" and "review" not in v["L2"]
    # 3) 새 분류(제목에서도 맞음) → cap_alone · review · basis bm25
    v = {x["dc_id"]: x for x in DT.merge_second_stage(rule, None, tax, cf, bm25_pick=_pick("L3"),
                                                       bm25_spec=spec).as_dict()["values"]}
    assert v["L3"]["confidence"] == 0.6 and v["L3"]["review"] and v["L3"]["basis"] == "bm25"
    assert v["L3"]["signals"]["bm25"]["field"] == "title+name"
    # 4) 파일명에서만 맞음 → name_only_cap · basis bm25_name_only
    v = {x["dc_id"]: x for x in DT.merge_second_stage(rule, None, tax, cf, bm25_pick=_pick("L3", 0.0),
                                                       bm25_spec=spec).as_dict()["values"]}
    assert v["L3"]["confidence"] == 0.55 and v["L3"]["basis"] == "bm25_name_only"
    assert v["L3"]["signals"]["bm25"]["field"] == "name" and v["L3"]["signals"]["bm25"]["words"] == ["매뉴얼"]


def test_합치기는_1차_review_표시를_지킨다():
    from csoclassify.classify import doctype as DT
    from csoclassify.classify import doc_rules as D
    from csoclassify.classify.propagate import DoctypeEmbedSignal
    tax = _tax()
    weak = ({"dc_id": "L1", "path": tax.path("L1"), "path_ids": tax.path_ids("L1"), "confidence": 0.35,
             "from": ("name_head",), "review": True, "basis": "name_head"},)
    esig = DoctypeEmbedSignal(values=({"dc_id": "L2", "confidence": 0.5},), method="knn_vote", top_sim=0.8)
    v = {x["dc_id"]: x for x in DT.merge_embed_candidates(weak, esig, tax, D.ConflictSpec(strategy="all"))
         .as_dict()["values"]}
    assert v["L1"]["review"] and v["L1"]["basis"] == "name_head"


def test_bm25_블록_검사():
    spec, probs = B.parse_spec({"enabled": True, "fields": {"title": 1.0, "name": 1.0},
                                "threshold": {"mode": "auto", "target_precision": 0.95, "min_pass": 10,
                                              "min_margin": 0.0},
                                "name_only": {"allow": True, "cap": 0.55}})
    assert probs == [] and spec.enabled and spec.weights() == {"title": 1.0, "name": 1.0}
    _, probs = B.parse_spec({"tokenizer": "kiwi", "fields": {"head": 1}, "threshold": {"mode": "x"},
                             "name_only": {"cap": 2}})
    assert [p[0] for p in probs] == ["bm25.tokenizer", "bm25.fields", "bm25.threshold.mode",
                                     "bm25.name_only.cap"]
    assert B.parse_spec(None)[0].enabled is False


def test_기준_문서_title_칸은_저장해도_남는다(tmp_path):
    from csoclassify import seedstore
    p = str(tmp_path / "s.jsonl")
    seedstore.save_seeds(p, [{"v": 3, "file": "a.docx", "doctype": ["L1"], "title": "시스템 매뉴얼",
                              "vector": [0.1, 0.2]}])
    row = seedstore.load_seeds(p)[0]
    assert row["title"] == "시스템 매뉴얼"
    assert list(row)[:4] == ["v", "file", "doctype", "title"]


def test_자동_문턱():
    spec = B.Bm25Spec(enabled=True, min_pass=4)
    res = B.auto_threshold(B.Bm25Index(_rows(), spec))
    # 12건 모두 자기 분류가 남은 2건 → min_seeds 3 을 못 채워 채점 대상이 없다
    assert res["scored"] == 0 and res["threshold"] is None
    rows = _rows() + [_row(f"D:/x/매뉴얼_{i}b.docx", "M", f"시스템 매뉴얼 {i}b", f"Mb{i}") for i in range(3)]
    for dc, word in (("R", "보고서"), ("C", "계약서"), ("P", "제안서")):
        rows.append(_row(f"D:/x/{word}_9.docx", dc, f"시스템 {word} 9", f"{dc}9"))
    res = B.auto_threshold(B.Bm25Index(rows, spec))
    assert res["scored"] == len(rows)
    assert res["threshold"] is not None and res["correct"] / res["passed"] >= 0.95
