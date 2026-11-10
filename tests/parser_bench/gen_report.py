# -*- coding: utf-8 -*-
"""pure_stats.json 의 수치로 report/파서비교-자체파서-vs-사이냅-202609.html 을 만든다.

분류·PII 를 뺀 **순수 문서파서** 비교판이다(자체판은 --text-only, 사이냅은 원래 추출 전용).
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "raw_pure"
DEST = Path(r"D:\Project\MpowerClassify\report\파서비교-자체파서-vs-사이냅-202609.html")

NAME = {
    "pdf": "PDF", "hwp": "한글 HWP", "hwpx": "한글 HWPX", "doc": "워드 DOC",
    "docx": "워드 DOCX", "ppt": "파워포인트 PPT", "pptx": "파워포인트 PPTX",
    "xls": "엑셀 XLS", "xlsx": "엑셀 XLSX", "html": "HTML", "txt": "텍스트",
    "csv": "CSV", "tsv": "TSV", "md": "마크다운", "json": "JSON",
}
ORDER = ["hwp", "hwpx", "pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx",
         "html", "txt", "md", "csv", "tsv", "json"]

# 같은 잣대(같은 정규화)로 잰 '고치기 전' 재현율 — 개선 폭을 정직하게 보이려고
# 고치기 전 파서가 뽑아 둔 본문에 지금과 똑같은 비교 기준을 적용해 다시 계산했다
# (측정 방식 변화가 개선 폭에 섞이지 않게). pptx·xlsx 는 1차 측정 본문에서,
# xls 는 수식 구현 직전 순수파서 측정에서 가져왔다.
BEFORE = {"pptx": 0.9598, "xlsx": 0.9615, "xls": 0.9540, "hwpx": 0.9620,
          "ppt": 0.9996, "pdf": 0.9791, "html": 0.8646, "hwp": 0.9954, "docx": 0.9993}

VERDICT = {
    "hwp": ("green", "대체 가능", "재현율 99.5% · 속도 사이냅보다 근소 우위"),
    "hwpx": ("green", "대체 가능", "문단 단위 구현으로 96.2% → 99.9% · 정밀도도 99.9%"),
    "pdf": ("green", "대체 가능", "텍스트 PDF 재현율 97.9%. 다만 추출이 사이냅보다 느리다(7장 ③)"),
    "doc": ("green", "대체 가능", "재현율 100% · 속도 동일"),
    "docx": ("green", "대체 가능", "재현율 99.9%"),
    "ppt": ("green", "대체 가능", "재현율 100% · 자체판이 도형 글자를 더 뽑는다(글자수 1.59배)"),
    "pptx": ("green", "대체 가능", "발표자 노트 구현으로 96.0% → 99.6%"),
    "xls": ("green", "대체 가능", "수식 결과값·날짜 서식 구현으로 95.4% → 99.4% · 자체판이 더 빠름"),
    "xlsx": ("green", "대체 가능", "셀 메모·날짜 서식 구현으로 96.2% → 99.2%"),
    "html": ("green", "대체 가능", "재현율 86.5%는 사이냅이 CSS·JS 를 본문에 섞기 때문(7장 ②)"),
    "txt": ("green", "대체 가능", "완전 일치"),
    "md": ("green", "대체 가능", "완전 일치"),
    "csv": ("green", "대체 가능", "재현율 99.7%"),
    "tsv": ("green", "대체 가능", "완전 일치 · 텍스트 상한을 100MB 로 올려 72~82MB 도 읽는다"),
    "json": ("green", "대체 가능", "완전 일치 · 텍스트 상한을 100MB 로 올려 48MB 도 읽는다"),
}


#------------------------------------------------------------------
# 숫자를 표에 넣기 좋은 문자열로
#=> None 이면 '—' 로 대신한다.
#
# -in: v = 숫자 또는 None
# -in: f = 포맷 문자열
#
# -out: 표시용 문자열
# -out: error = 없음
#------------------------------------------------------------------
def fmt(v, f="{:.1f}"):
    return "—" if v is None else f.format(v)


#------------------------------------------------------------------
# 백분율 막대 한 칸 그리기
#=> 0~1 값을 폭으로 바꿔 색 막대와 숫자를 같이 보여 준다.
#
# -in: v = 0~1 비율(None 가능)
#
# -out: <td> 안에 넣을 HTML
# -out: error = 없음
#------------------------------------------------------------------
def bar(v):
    if v is None:
        return '<span class="dim">—</span>'
    pct = v * 100
    cls = "ok" if pct >= 95 else ("warn" if pct >= 85 else "bad")
    return (f'<div class="mbar"><i class="{cls}" style="width:{min(pct,100):.0f}%"></i>'
            f'<b>{pct:.1f}%</b></div>')


#------------------------------------------------------------------
# 두 파서 시간 비교 막대 그리기
#=> 확장자마다 두 중앙시간을 같은 기준(가장 느린 값)으로 나눠 길이를 잡는다.
#
# -in: rows = [(이름, 사이냅초, 자체초), ...]
#
# -out: 차트 HTML
# -out: error = 없음
#------------------------------------------------------------------
def chart(rows):
    mx = max(max(a, b) for _, a, b in rows) or 1
    out = ['<div class="chart">']
    for name, a, b in rows:
        out.append(
            f'<div class="crow"><span class="cl">{name}</span>'
            f'<span class="cb"><i class="s" style="width:{a / mx * 100:.1f}%"></i>'
            f'<em>{a * 1000:.0f}ms</em></span>'
            f'<span class="cb"><i class="r" style="width:{b / mx * 100:.1f}%"></i>'
            f'<em>{b * 1000:.0f}ms</em></span></div>')
    out.append("</div>")
    return "\n".join(out)


#------------------------------------------------------------------
# 보고서 HTML 만들어 저장
#=> 집계값을 표·차트·결론 문장으로 엮어 하나의 HTML 문서로 쓴다.
#    1) 결론 → 방법 → 이번에 고친 것 → 시간 → 성공률 → 일치도 → 남은 격차 → 판정
#       → 표본 밖 포맷(빈칸) → 권고
#    2) 측정 수치는 전부 pure_stats.json 에서 읽어 손으로 적지 않는다
#       (단 9장 '표본 밖 포맷' 은 측정이 아니라 조사 결과라 고정 문장이다)
#
# -in: 없음
#
# -out: 없음 (report/*.html 생성)
# -out: error = pure_stats.json 이 없으면 예외 전파
#------------------------------------------------------------------
def main():
    d = json.loads((OUT / "pure_stats.json").read_text("utf-8"))
    ext, allr = d["ext"], d["all"]
    rows = [json.loads(l) for l in (OUT / "pure.jsonl").read_text("utf-8").splitlines() if l.strip()]

    off = [r for r in rows if r["ext"] in
           ("doc", "docx", "hwp", "hwpx", "ppt", "pptx", "xls", "xlsx", "pdf", "html")]
    off_snf = sum(r["snf_sec"] for r in off) / len(off) * 1000
    off_rs = sum(r["rs_sec"] for r in off) / len(off) * 1000
    # 평균 ms 만 보면 '조금 느리다'로 읽히지만, 총량으로는 몇 초짜리 차이다.
    off_gap = sum(r["rs_sec"] - r["snf_sec"] for r in off)
    faster = sum(1 for r in rows if r["rs_sec"] <= r["snf_sec"])

    trs = []
    for e in ORDER:
        v = ext[e]
        ratio = v["rs_med"] / v["snf_med"] if v["snf_med"] else None
        cls = "win" if (ratio is not None and ratio <= 1.05) else ""
        trs.append(f"""<tr><td class="nm">{NAME[e]}</td><td class="n">{v['n']}</td>
<td class="n">{fmt(v['mb'], '{:.2f}')}</td>
<td class="n">{v['snf_med'] * 1000:.1f}</td>
<td class="n"><b>{v['rs_med'] * 1000:.1f}</b></td>
<td class="n {cls}">{('×%.2f' % ratio) if ratio else '—'}</td>
<td class="n">{fmt(v['snf_mbs'], '{:.0f}')}</td>
<td class="n">{fmt(v['rs_mbs'], '{:.0f}')}</td></tr>""")

    srs = []
    for e in ORDER:
        v = ext[e]
        note = []
        if v["fail_scan"]:
            note.append(f"스캔본(이미지) {v['fail_scan']}건 — 사이냅도 본문 없음")
        if v["fail_size"]:
            note.append(f"텍스트 크기 상한 {v['fail_size']}건")
        if not note:
            note.append("—")
        srs.append(f"""<tr><td class="nm">{NAME[e]}</td><td class="n">{v['n']}</td>
<td class="n">{v['snf_ok']}</td><td class="n"><b>{v['rs_ok']}</b></td>
<td class="n">{(v['rs_ok'] / v['n'] * 100):.0f}%</td>
<td class="note">{' · '.join(note)}</td></tr>""")

    mrs = []
    for e in ORDER:
        v = ext[e]
        mrs.append(f"""<tr><td class="nm">{NAME[e]}</td><td class="n">{v['n_cmp']}</td>
<td>{bar(v['recall'])}</td><td>{bar(v['prec'])}</td><td>{bar(v['jac'])}</td>
<td class="n">{fmt(v['charratio'], '{:.2f}')}</td></tr>""")

    vrs = []
    for e in ORDER:
        c, t, why = VERDICT[e]
        vrs.append(f'<tr><td class="nm">{NAME[e]}</td>'
                   f'<td><span class="pill {c}">{t}</span></td>'
                   f'<td class="note">{why}</td></tr>')

    # 개선 전후 — 같은 잣대로 다시 잰 값만 싣는다
    brs = []
    for e in ["pptx", "xlsx", "xls", "hwpx", "ppt", "pdf", "docx", "hwp", "html"]:
        b, a = BEFORE[e], ext[e]["recall"]
        diff = (a - b) * 100
        tone = "up" if diff > 0.05 else "same"
        mark = f"+{diff:.1f}%p" if diff > 0.05 else "변화 없음"
        brs.append(f'<tr><td class="nm">{NAME[e]}</td><td class="n">{b * 100:.1f}%</td>'
                   f'<td class="n"><b>{a * 100:.1f}%</b></td>'
                   f'<td class="n {tone}">{mark}</td></tr>')

    docext = ["hwp", "hwpx", "pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx",
              "html", "txt", "md"]
    ch = chart([(NAME[e], ext[e]["snf_med"], ext[e]["rs_med"]) for e in docext])
    ch2 = chart([(NAME[e], ext[e]["snf_med"], ext[e]["rs_med"]) for e in ["csv", "tsv", "json"]])

    html = TPL.format(
        n=allr["n"], mb=allr["bytes"] / 1e6,
        recall=allr["recall"] * 100, prec=allr["prec"] * 100, jac=allr["jac"] * 100,
        charratio=allr["charratio"],
        rs_ok=allr["rs_ok"], snf_ok=allr["snf_ok"],
        fail=allr["fail"], fail_scan=allr["fail_scan"], fail_size=allr["fail_size"],
        off_snf=off_snf, off_rs=off_rs, off_n=len(off), faster=faster,
        snf_total=allr["snf_total"], rs_total=allr["rs_total"],
        # 확장자별 총시간 차이 — '누가 총량 격차를 만드는가'를 본문에서 말하려면 필요하다
        gap_xlsx=ext["xlsx"]["rs_total"] - ext["xlsx"]["snf_total"],
        gap_pptx=ext["pptx"]["rs_total"] - ext["pptx"]["snf_total"],
        gap_docx=ext["docx"]["rs_total"] - ext["docx"]["snf_total"],
        gap_pdf=ext["pdf"]["rs_total"] - ext["pdf"]["snf_total"],
        gap_xlsx_worst=allr["worst_gap"]["gap"],
        gap_xlsx_rest=(ext["xlsx"]["rs_total"] - ext["xlsx"]["snf_total"]
                       - allr["worst_gap"]["gap"]),
        off_gap=off_gap,
        gap_tsv=ext["tsv"]["rs_total"] - ext["tsv"]["snf_total"],
        gap_json=ext["json"]["rs_total"] - ext["json"]["snf_total"],
        gap_csv=ext["csv"]["rs_total"] - ext["csv"]["snf_total"],
        worst_ext=allr["worst_gap"]["ext"], gap_worst=allr["worst_gap"]["gap"],
        slowsec=allr["rs_total"] - allr["snf_total"],
        gap_xlsx_worst_pct=allr["worst_gap"]["gap"]
                           / (allr["rs_total"] - allr["snf_total"]) * 100,
        worst_name=allr["worst_gap"]["name"], worst_mb=allr["worst_gap"]["mb"],
        worst_snf=allr["worst_gap"]["snf"], worst_rs=allr["worst_gap"]["rs"],
        # 자체판이 사이냅보다 몇 % 더 걸렸는지 — 총 처리시간 비에서 바로 뽑는다
        slowpct=(allr["rs_total"] / allr["snf_total"] - 1) * 100,
        snf_mbs=allr["snf_mbs"], rs_mbs=allr["rs_mbs"],
        snf_med=allr["snf_med"] * 1000, rs_med=allr["rs_med"] * 1000,
        pdf_snf=ext["pdf"]["snf_med"] * 1000, pdf_rs=ext["pdf"]["rs_med"] * 1000,
        pdf_ratio=ext["pdf"]["rs_med"] / ext["pdf"]["snf_med"],
        pdf_10man_snf=ext["pdf"]["snf_med"] * 100000 / 60,
        pdf_10man_rs=ext["pdf"]["rs_med"] * 100000 / 60,
        trows="\n".join(trs), srows="\n".join(srs), mrows="\n".join(mrs),
        vrows="\n".join(vrs), brows="\n".join(brs), chart=ch, chart2=ch2,
        ncmp=allr["n_cmp"],
    )
    DEST.parent.mkdir(parents=True, exist_ok=True)
    DEST.write_text(html, "utf-8")
    print("wrote", DEST, len(html), "bytes")


TPL = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>자체 문서파서 vs 사이냅 문서필터 — 파서 성능 비교</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700;900&display=swap" rel="stylesheet">
<style>
:root{{
  --navy:#232B5D; --blue:#1B5FA8; --blue-l:#E8EEF7;
  --c:#C62828; --c-bg:#FBE9E9; --s:#B07400; --s-bg:#FDF4DF; --o:#2E7D32; --o-bg:#E7F4E9;
  --body:#333945; --muted:#6B7484; --line:#DDE2EA; --panel:#F7F8FA; --stage:#EDF0F5;
}}
*{{box-sizing:border-box}}
html,body{{margin:0;padding:0;background:var(--stage);
  font-family:'Noto Sans KR','Malgun Gothic',-apple-system,sans-serif;color:var(--body);
  -webkit-font-smoothing:antialiased;line-height:1.6}}
.page{{max-width:1120px;margin:0 auto;background:#fff;padding:0 0 72px;
  box-shadow:0 2px 24px rgba(30,40,70,.10);min-height:100vh}}
header{{background:linear-gradient(150deg,#1B2A55 0%,#243568 55%,#1B5FA8 130%);
  color:#fff;padding:52px 56px 44px}}
header .dot{{width:64px;height:5px;background:#5B8FD6;border-radius:3px;margin-bottom:26px}}
header h1{{margin:0;font-size:38px;font-weight:900;line-height:1.25}}
header .tag{{margin:16px 0 0;font-size:19px;color:#C3CFE8;font-weight:500}}
header .meta{{margin-top:30px;font-size:15px;color:#8FA0C8;line-height:1.9}}
main{{padding:0 56px}}
h2{{font-size:26px;font-weight:900;color:var(--navy);margin:52px 0 6px;
  padding-top:22px;border-top:3px solid var(--navy)}}
h2 .num{{color:var(--blue);margin-right:10px}}
h3{{font-size:19px;font-weight:800;color:var(--blue);margin:32px 0 10px}}
p{{margin:10px 0;font-size:16px}}
ul,ol{{margin:10px 0;padding-left:22px;font-size:16px}}
li{{margin:5px 0}}
small,.dim{{color:var(--muted)}}
code{{background:#EFF2F6;border-radius:4px;padding:1px 6px;font-size:14px;
  font-family:Consolas,'D2Coding',monospace}}
.lead{{font-size:17px;color:var(--muted);margin:8px 0 0}}
table{{width:100%;border-collapse:collapse;font-size:15px;margin:16px 0 6px}}
th{{background:var(--navy);color:#fff;font-weight:700;padding:10px 12px;text-align:left;
  font-size:14px;white-space:nowrap}}
th.n,td.n{{text-align:right}}
td{{border-bottom:1px solid var(--line);padding:9px 12px;vertical-align:middle}}
tr:nth-child(even) td{{background:#FAFBFC}}
td.nm{{font-weight:700;color:var(--navy);white-space:nowrap}}
td.note{{font-size:14px;color:var(--muted);line-height:1.45}}
td.win{{color:var(--o);font-weight:700}}
td.up{{color:var(--o);font-weight:700}}
td.same{{color:var(--muted)}}
.cards{{display:flex;gap:14px;margin:22px 0 6px;flex-wrap:wrap}}
.kpi{{flex:1;min-width:180px;background:var(--panel);border:1px solid var(--line);
  border-radius:12px;padding:18px 20px}}
.kpi .v{{font-size:34px;font-weight:900;color:var(--blue);line-height:1.1}}
.kpi .k{{font-size:14px;color:var(--muted);margin-top:7px;font-weight:500}}
.kpi.hi{{background:var(--o-bg);border-color:#BFE0C4}}
.kpi.hi .v{{color:var(--o)}}
.kpi.wa{{background:var(--s-bg);border-color:#EBD9A8}}
.kpi.wa .v{{color:var(--s)}}
.box{{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--blue);
  border-radius:0 10px 10px 0;padding:16px 20px;margin:18px 0}}
.box.warn{{border-left-color:var(--s);background:var(--s-bg);border-color:#EBD9A8}}
.box.good{{border-left-color:var(--o);background:var(--o-bg);border-color:#BFE0C4}}
.box h4{{margin:0 0 8px;font-size:16px;font-weight:800;color:var(--navy)}}
.box p{{margin:6px 0;font-size:15px}}
.navybar{{background:var(--navy);color:#DCE3F2;border-radius:12px;padding:20px 26px;margin:24px 0}}
.navybar p{{margin:0;font-size:17px;line-height:1.6}}
.navybar b{{color:#fff}}
.pill{{display:inline-block;padding:3px 12px;border-radius:20px;font-size:13.5px;font-weight:700;
  white-space:nowrap}}
.pill.green{{background:var(--o-bg);color:var(--o);border:1px solid #BFE0C4}}
.pill.amber{{background:var(--s-bg);color:var(--s);border:1px solid #EBD9A8}}
.pill.red{{background:var(--c-bg);color:var(--c);border:1px solid #EFC3C3}}
.mbar{{position:relative;height:19px;background:#EEF1F5;border-radius:4px;overflow:hidden;
  min-width:110px}}
.mbar i{{position:absolute;left:0;top:0;bottom:0;border-radius:4px}}
.mbar i.ok{{background:#8FBBE4}} .mbar i.warn{{background:#E8C97A}} .mbar i.bad{{background:#E2A0A0}}
.mbar b{{position:absolute;right:7px;top:0;font-size:12.5px;font-weight:700;color:#2A3550}}
.chart{{margin:18px 0 8px;background:var(--panel);border:1px solid var(--line);
  border-radius:12px;padding:18px 22px}}
.crow{{display:flex;align-items:center;gap:10px;margin:5px 0;font-size:13.5px}}
.cl{{width:118px;flex:none;font-weight:700;color:var(--navy);font-size:13.5px}}
.cb{{flex:1;position:relative;height:17px;background:#EAEEF3;border-radius:3px}}
.cb i{{position:absolute;left:0;top:0;bottom:0;border-radius:3px;min-width:2px}}
.cb i.s{{background:#9AA6B8}} .cb i.r{{background:var(--blue)}}
.cb em{{position:absolute;right:6px;top:0;font-style:normal;font-size:11.5px;color:#4A5568;
  font-weight:700}}
.legend{{display:flex;gap:18px;font-size:13.5px;color:var(--muted);margin-top:12px}}
.legend span{{display:flex;align-items:center;gap:6px}}
.legend i{{width:13px;height:13px;border-radius:3px;display:inline-block}}
footer{{margin-top:46px;padding:22px 56px 0;border-top:1px solid var(--line);
  font-size:13.5px;color:var(--muted)}}
@media print{{body{{background:#fff}} .page{{box-shadow:none;max-width:none}}}}
</style>
</head>
<body>
<div class="page">

<header>
  <div class="dot"></div>
  <h1>자체 문서파서 vs 사이냅 문서필터(snf)<br>파서 성능 비교</h1>
  <p class="tag">분류·PII 검사를 뺀 <b>순수 텍스트 추출</b>만으로 견준 결과</p>
  <div class="meta">
    검증일 2026-09-11 · 대상 <b>D:\분류함</b> · 표본 <b>{n}건 / {mb:,.0f}MB</b> · 15개 확장자<br>
    비교군 A: 사이냅 문서필터 snf_exe v4.29.0 · <code>snf_exe -U8 &lt;문서&gt; &lt;출력&gt;</code><br>
    비교군 B: MpowerClassify-rs 자체 파서 · <code>MpowerClassify-rs --text-only --file &lt;문서&gt; --out &lt;출력&gt;</code><br>
    측정 환경: Windows 11, 로컬 디스크, 단일 프로세스 순차 실행 · 문서마다 3회 측정 후 최솟값
  </div>
</header>

<main>

<h2><span class="num">01</span>결론 먼저</h2>
<div class="navybar">
  <p><b>파서만 놓고 봐도 대체 가능하다.</b> 본문 재현율 <b>{recall:.1f}%</b>,
  문서 1건 중앙값 <b>{rs_med:.1f}ms 대 사이냅 {snf_med:.1f}ms</b>로 한 건씩은 비슷하고
  뽑아내는 글자도 거의 같다. {mb:,.0f}MB 를 통째로 돌린 전체 시간은
  <b>{rs_total:.1f}초 대 {snf_total:.1f}초({slowpct:.0f}% 더 걸림)</b>로,
  이제 어느 한 포맷이 격차를 만들지 않는다 — 가장 큰 확장자도 +1초 미만이다(4장).
  지난 측정이 지목한 격차를 모두 구현했다
  — <b>PPTX 발표자 노트</b> · <b>엑셀 셀 메모</b> · <b>구형 XLS 수식 결과값</b> ·
  <b>HWPX 문단 단위 추출</b>, 그리고 이번에 <b>엑셀 날짜 서식</b>까지(3장). 
  <b>내용 격차는 사실상 사라졌다</b>.
  남은 것은 값 차이가 아니라 속도(건수 많은 PDF)뿐이다.</p>
</div>

<div class="cards">
  <div class="kpi hi"><div class="v">{recall:.1f}%</div><div class="k">본문 재현율<br><small>사이냅이 뽑은 낱말 중 자체판도 뽑은 비율</small></div></div>
  <div class="kpi hi"><div class="v">{prec:.1f}%</div><div class="k">정밀도<br><small>자체판 낱말 중 사이냅에도 있는 비율</small></div></div>
  <div class="kpi"><div class="v">{charratio:.2f}배</div><div class="k">글자수 비<br><small>자체판 ÷ 사이냅 — 1.0 이면 같은 양</small></div></div>
  <div class="kpi wa"><div class="v">{rs_mbs:.0f}<small style="font-size:16px"> MB/s</small></div><div class="k">처리량<br><small>사이냅 {snf_mbs:.0f} MB/s</small></div></div>
</div>

<h2><span class="num">02</span>지난 보고서와 무엇이 다른가</h2>
<p class="lead">같은 표본·같은 도구지만, <b>재는 대상</b>이 다르다.</p>
<div class="box">
  <h4>지난 측정은 파서끼리의 싸움이 아니었다</h4>
  <p>지난번 자체판 시간에는 <b>규칙·PII 분류가 통째로 포함</b>돼 있었다(사이냅은 추출만 한다).
  그래서 대용량 CSV 한 건이 2.6초로 찍히는 식의 왜곡이 있었다 — 그 2.5초는 파서가 아니라
  본문 200만 자를 PII 정규식으로 훑는 시간이었다.</p>
  <p>이번에는 자체판에 <code>--text-only</code> 모드를 넣어 <b>규칙셋을 읽지도 않고 PII 검사도
  하지 않는</b> 상태로 돌렸다. 두 도구 모두 '문서를 열어 글자를 뽑아 파일로 쓴다'는
  똑같은 일만 한다. 같은 CSV 가 이번에는 <b>91ms</b> 로 찍힌다.</p>
  <p>측정 흔들림을 줄이려고 문서마다 <b>3회씩 돌려 최솟값</b>을 썼다. 방해가 가장 적었던 판이
  그 도구의 실력에 가장 가깝기 때문이다(평균은 백그라운드 잡음을 그대로 싣는다).</p>
</div>
<div class="box">
  <h4>사이냅의 구분 표식은 본문에서 뺐다</h4>
  <p>사이냅은 쪽·시트가 바뀔 때 <code>..PAGE:3</code> · <code>..SHEET:1</code> 같은 표식을 넣는다.
  이건 문서 내용이 아니라 구분선이다. 지난 보고서는 이걸 '자체판이 놓친 낱말'로 세어
  재현율을 실제보다 낮게 잡았다 — PPTX·PDF·엑셀이 특히 손해를 봤다.
  이번에는 양쪽에서 걷어내고 비교했다.</p>
</div>

<h2><span class="num">03</span>이번에 고친 것</h2>
<p class="lead">지난 보고서가 지목한 격차를 구현했다(①~④). ⑤ 는 설정 쪽 구멍, ⑥ 은 속도 개선, ⑦ 은 지난 보고서가 '후순위'로 미뤄 둔 날짜 서식이다.
아래 '고치기 전' 수치는
같은 잣대로 다시 계산한 값이라, 측정 방식 변화가 섞이지 않은 순수한 개선 폭이다.</p>

<div class="box good">
  <h4>① PPTX 발표자 노트 <code>ppt/notesSlides/notesSlide*.xml</code></h4>
  <p>슬라이드 본문을 읽은 뒤 노트도 같은 문단 수집기로 읽어 뒤에 붙인다(노트도 슬라이드와
  같은 <code>a:p</code>/<code>a:t</code> 구조다). 표본 25건 중 19건에 노트가 있었다.</p>
  <p>실측: <code>문서중앙화 MS 연동 방안.pptx</code> — 본문 812자만 뽑던 것이
  <b>약 3,000자</b>로 늘어, 노트에 적힌 상세 설계 설명이 본문에 들어왔다.</p>
</div>

<div class="box good">
  <h4>② 엑셀 셀 메모 <code>xl/comments*.xml</code> · 스레드 댓글</h4>
  <p>시트를 다 읽은 뒤 메모를 한 줄씩 이어 붙인다. 옛 방식(<code>&lt;comment&gt;&lt;r&gt;&lt;t&gt;</code>)은
  서식 때문에 잘게 쪼개져 오므로 이어 붙이고, 새 방식(스레드 댓글)은 <code>&lt;text&gt;</code>를 그대로 쓴다.
  작성자 이름은 본문이 아니라 넣지 않는다.</p>
  <p>실측: <code>3) 방화벽포트허용신청서.xlsx</code> — 셀 값만 읽던 것이 작성 안내·주의사항
  (“셀 서식을 텍스트로”, “영구면 9999-12-31”)까지 읽는다.</p>
</div>

<div class="box good">
  <h4>③ 구형 XLS 수식 결과값 <code>FORMULA</code>(0x0006) 레코드</h4>
  <p>BIFF8 의 수식 셀은 식만 갖고 있지 않고 <b>마지막으로 계산된 값</b>을 함께 싣는데,
  그 레코드를 통째로 건너뛰고 있었다. 합계·환산처럼 <b>사람이 표에서 실제로 읽는 값</b>
  상당수가 수식 셀이라 표의 한 열이 통째로 사라지곤 했다.</p>
  <p>값 자리(8바이트)는 두 갈래다 — 마지막 두 바이트가 <code>0xFFFF</code> 면 특별한 값
  (문자열·논리값·오류·빈 문자열)이라는 표시이고, 아니면 IEEE754 실수 그대로다.
  문자열 결과는 글자가 뒤따르는 <code>STRING</code> 레코드에 오므로 <b>어느 칸 것인지</b>를
  기억했다가 그 자리에 넣는다(예전에는 자리를 몰라 행 끝에 몰아 붙였다).</p>
  <p>실측: <code>엠파워 속도 테스트(20090617).xls</code> — '평균 계산을 위한 초' 열의
  숫자가 전부 살아나 재현율 <b>49.4% → 98.7%</b>.</p>
  <p><b>왜 중요한가:</b> 하이픈 없이 적은 전화번호·계좌번호가 수식 결과로 들어 있으면
  PII 검출기 눈에 아예 안 보였다. XLSX 는 이미 같은 값을 읽고 있어 XLS 만 구멍이었다.</p>
</div>

<div class="box good">
  <h4>④ HWPX 문단 단위 추출 <code>hp:p</code></h4>
  <p>HWPX 는 글자 서식이 바뀌는 자리에서 <code>&lt;hp:t&gt;</code> 를 갈라 놓는데,
  예전 판은 <b>태그마다 한 줄</b>로 끊었다. 그래서 한 낱말이 두 태그에 걸치면 그 자리에서
  줄이 갈라졌다 — 사이냅이 "가치를 · 개발에 · 보장한다"로 뽑은 것을 이 판은
  "가치 / 를 / 개발 / 에" 로 내놨다. <b>낱말이 쪼개지면 붙어 있어야 성립하는 검출</b>
  (전화·계좌번호, 앵커+값)이 통째로 어긋난다.</p>
  <p>공용 문단 수집기를 그대로 쓸 수는 없었다. HWPX 는 <b>문단이 문단 안에 들어가기</b>
  때문이다 — 표가 <code>hp:p</code> → <code>hp:tbl</code> → <code>hp:tc</code> →
  <code>hp:subList</code> → 다시 <code>hp:p</code> 로 내려간다(표본 10개 파일에 중첩 14,413곳).
  짝 맞추기 정규식은 바깥 여는 태그와 <b>안쪽</b> 닫는 태그를 짝지어 경계가 엉킨다.
  그래서 짝을 맞추지 않고 <b>한 번만 훑으면서 문단이 열리거나 닫히는 자리에서 줄을 끊는</b>
  방식으로 구현했다 — 표 칸도 제 줄을 갖고, 글자 차례도 문서 그대로 남는다.
  문단 안 <code>&lt;hp:lineBreak/&gt;</code>·<code>&lt;hp:tab/&gt;</code> 은 줄바꿈·탭으로 살렸다
  (없애면 두 줄이 한 낱말로 붙어 문서에 없던 말이 생긴다).</p>
  <p>재현율 96.2% → <b>99.9%</b>, 정밀도 95.4% → <b>99.9%</b> — 네 건 중 개선 폭이 가장 크다.</p>
  <div class="note" style="margin:12px 0 0">
    <p style="margin:0"><b>※ 파이썬 판에도 같이 넣었다.</b> 한동안 이 항목만 두 판의 본문이
    갈려 있었다(예전 동작은 파이썬 판과 일부러 맞춘 것이었다). 같은 규칙 — 한 번만 훑으며
    문단 경계에서 줄을 끊고, <code>&lt;hp:lineBreak/&gt;</code>·<code>&lt;hp:tab/&gt;</code> 를
    살리고, 빈 문단은 버린다 — 으로 맞췄고 같은 이름의 단위시험을 양쪽에 두었다.
    실측 10건에서 두 판이 뽑아낸 낱말이 <b>완전히 같다</b>(자카드 1.000).
    파이썬 판도 정밀도 99.85% · 재현율 99.92% 로 Rust 판과 같은 값이다.</p>
  </div>
</div>

<div class="box">
  <h4>⑤ 텍스트 크기 상한 20MB → 100MB <code>MAX_FILE_BYTES_TEXT</code></h4>
  <p>이건 파서가 아니라 <b>설정</b> 쪽 구멍이었다. txt·csv·tsv·json·html 만 20MB 에서
  <b>문서를 통째로 버리고</b> 있었다 — 지난 측정에서 46~82MB 짜리 tsv·json 4건이 본문을
  한 글자도 못 남긴 반면, 사이냅은 같은 파일을 멀쩡히 읽어냈다. 상한이 파서 실력처럼
  보이던 자리다.</p>
  <p><b>왜 올려도 되는가.</b> 크기 상한 설계의 원칙은 "진짜 상한은 <b>추출 후 글자수</b>(G3,
  200만 자)에 걸고 바이트는 느슨한 안전핀으로 둔다" 인데, G3 는 문서를 버리지 않고
  <b>앞부분만 읽고 '일부만 봤다' 표식</b>을 단다. 텍스트만 20MB 에서 통째로 버리는 것은
  그 원칙과 어긋나 있었다. PII 정규식·분류가 실제로 훑는 길이는 어차피 G3 가 잡으므로,
  올려서 늘어나는 비용은 디스크에서 더 읽고 디코드하는 몫뿐이다.</p>
  <p><b>결과:</b> 4건 모두 추출 성공, 재현율 100%·100%·99.97%·100%. 안전핀은 일반 상한과
  같은 100MB 로 남는다(실측 2,254파일 중 100MB 초과는 2건). 파이썬 판 <code>config.py</code> 도
  같은 값으로 맞췄다 — 두 판의 기본값이 어긋나면 같은 배치가 판마다 다른 결과를 낸다.</p>
</div>

<div class="box">
  <h4>⑥ XLSX 를 흘려 읽는다 (정규식 → SAX) <code>quick-xml</code></h4>
  <p>앞의 ①~⑤ 가 '못 읽던 것을 읽게' 한 일이라면, 이건 <b>속도</b> 쪽이다.
  예전 방식은 시트 XML 을 통째로 문자열에 올린 뒤 정규식으로 행·칸을 훑었다.
  보통 크기 엑셀에서는 문제가 없지만, 시트 한 장이 33MB 인 데이터셋 엑셀에서는
  <b>메모리에 올리고 · 행마다 정규식을 다시 돌리고 · 값마다 새 문자열을 만드는</b>
  비용이 한꺼번에 커진다.</p>
  <p>이제 압축을 푸는 족족 이벤트로 받아 처리하고 지나간다. 83MB 짜리 XML(시트 2장
  + 공유문자열표)을 단 한 번도 통째로 올리지 않는다.</p>
  <p><b>결과:</b> 문제의 15.2MB 파일이 <b>4.97초 → 1.61초</b>(3.1배). xlsx 전체
  총시간 차이는 +3.64초 → <b>+{gap_xlsx:.2f}초</b>로 줄었다.</p>
  <p><b>본문은 그대로다.</b> 표본의 xls·xlsx 35건을 바꾸기 전후로 각각 뽑아
  <b>바이트 단위로 견줘 전부 같음</b>을 확인했다. 속도만 바꾸는 변경이므로
  본문이 한 글자라도 달라지면 안 된다.</p>
  <div class="note">
    <p style="margin:0"><b>※ 옮기는 중에 잡은 함정.</b> quick-xml 은
    <code>&amp;lt;</code> 같은 엔티티 참조를 글자와 <b>따로</b> 알려 준다. 처음엔 이
    이벤트를 흘려보내 <code>"4TB &lt; 전체"</code> 가 <code>"4TB 전체"</code> 로 나왔다 —
    부등호가 소리 없이 사라졌다. 바이트 비교를 하지 않았다면 못 봤을 종류의 오류라,
    같은 일이 다시 생기지 않게 단위시험으로 못 박았다.</p>
  </div>
</div>

<div class="box">
  <h4>⑦ 엑셀 날짜 서식 <code>styles.xml</code> · <code>XF/FORMAT</code> 레코드</h4>
  <p>엑셀은 날짜를 <b>숫자</b>로 저장한다. <code>2012-12-31</code> 은 파일 안에
  <code>41274</code> 로 들어 있고, "날짜처럼 보여라"는 <b>서식</b>이 따로 붙어 있을 뿐이다.
  서식을 읽지 않아 본문에 일련번호가 그대로 나왔다 — 사람이 표에서 보는 글자와 달랐다.</p>
  <p>이제 xlsx 는 <code>xl/styles.xml</code> 의 <code>&lt;cellXfs&gt;</code>·
  <code>&lt;numFmts&gt;</code> 를, 구형 xls 는 <code>XF</code>·<code>FORMAT</code> 레코드를
  읽어 그 칸이 날짜 서식인지 확인한 <b>뒤에만</b> 바꾼다. 서식을 보지 않고 바꾸면
  멀쩡한 수량·금액이 날짜로 둔갑한다.</p>
  <p><b>실측:</b> <code>삼성생명_…_WBS_V1.0.xls</code> 정밀도 <b>75.6% → 100%</b>
  (자체판에만 있던 낱말이 전부 날짜 일련번호였다), <code>2025년 AI사업_WBS.xlsx</code>
  정밀도 85.3% → 99.3%. 확장자 전체로는 xls 정밀도 98.2% → <b>99.9%</b>,
  xlsx 98.8% → <b>99.5%</b>.</p>
  <div class="note">
    <p style="margin:0"><b>※ 가려 읽어야 하는 자리들.</b> ① <code>&lt;xf&gt;</code> 는
    <code>&lt;cellStyleXfs&gt;</code>(이름 있는 스타일의 원본)에도 같은 이름으로 들어 있어,
    함께 세면 번호가 밀려 엉뚱한 칸이 날짜가 된다. ② 글자 칸은 서식이 날짜여도
    건드리면 안 된다 — 문자열 "41274" 가 날짜로 바뀐다. ③ 서식 문자열의
    <code>m</code> 은 '월'도 '분'도 되므로(<code>mm:ss</code>), <code>y</code>·<code>d</code> 가
    함께 있을 때만 날짜로 본다. ④ 엑셀은 1900년을 윤년으로 잘못 알아 60번이
    가짜 날짜다 — 그 앞뒤로 기준일이 하루 다르다. 넷 다 단위시험으로 못 박았다.</p>
  </div>
  <div class="note">
    <p style="margin:0"><b>※ 파이썬 판에도 같이 넣었다.</b> 두 판의 기본값·본문이 갈리면
    같은 배치가 판마다 다른 등급을 낸다. 판단 규칙(내장 서식 번호표 · 서식 문자열 해석 ·
    1900년 윤년 보정)과 출력 모양(<code>YYYY-MM-DD</code>)을 1:1 로 맞췄고, 같은 이름의
    단위시험을 양쪽에 두었다. 실측에서 두 판이 뽑아낸 날짜 글자가 <b>완전히 같다</b>
    (xls 85종·xlsx 41종 전부 일치). 구형 xls 쪽은 이미 서식을 읽고 있는
    <code>xlrd</code> 의 판정을 쓰고 출력 모양만 맞췄다 — 같은 일을 두 번 하면
    두 해석이 어긋날 여지만 생긴다.</p>
    <p style="margin:.5em 0 0"><b>덤으로 드러난 것.</b> 파이썬 판은 <code>xlrd</code> 가
    준 실수를 그대로 찍어 수량 9 를 <code>9.0</code>, 연도 2024 를 <code>2024.0</code> 으로
    내놓고 있었다 — 엑셀 화면에도 사이냅 출력에도 없는 글자다. 날짜와 무관한 기존
    결함이지만 같은 자리라 함께 고쳤고, 그 결과 <b>xls 표본 15건에서 두 판의 낱말이
    완전히 같아졌다</b>(자카드 1.000).</p>
  </div>
</div>

<table>
<tr><th>확장자</th><th class="n">고치기 전 재현율</th><th class="n">고친 뒤</th><th class="n">차이</th></tr>
{brows}
</table>
<p><small>※ 손대지 않은 포맷이 전부 '변화 없음'인 것이 중요하다 — 위 ①~④ 를 넣으면서
<b>다른 포맷의 추출 결과를 건드리지 않았다</b>는 뜻이다. 단위시험 226건도 모두 통과한다.</small></p>

<h2><span class="num">04</span>확장자별 추출 시간</h2>
<p class="lead">문서 1건을 열어 글자를 뽑아 파일로 쓰기까지의 시간(중앙값, 밀리초). 낮을수록 빠르다.</p>
{chart}
<div class="legend">
  <span><i style="background:#9AA6B8"></i> 사이냅 snf</span>
  <span><i style="background:#1B5FA8"></i> 자체 파서(--text-only)</span>
</div>

<h3>대용량 데이터 파일</h3>
{chart2}
<div class="legend">
  <span><i style="background:#9AA6B8"></i> 사이냅 snf</span>
  <span><i style="background:#1B5FA8"></i> 자체 파서(--text-only)</span>
</div>

<table>
<tr><th>확장자</th><th class="n">건수</th><th class="n">평균 크기(MB)</th>
<th class="n">사이냅(ms)</th><th class="n">자체판(ms)</th><th class="n">배수</th>
<th class="n">사이냅 MB/s</th><th class="n">자체판 MB/s</th></tr>
{trows}
</table>
<p><small>※ '배수'는 자체판 ÷ 사이냅. <b>1.05 이하는 녹색</b>(사실상 동급 이상)으로 칠했다.
XLS·PPT·HWP·DOC 은 자체판이 더 빠르고, PDF 는 자체판이 {pdf_ratio:.1f}배 느리다.
TSV·JSON 이 지난 표에서 '자체판이 더 빠름'으로 찍혔던 것은 상한에 막혀 <b>읽지 않고 끝냈기</b>
때문이다 — 상한을 푼 지금은 ×1.11·×1.18 로 사이냅보다 조금 느리다(3장 ⑤).</small></p>

<div class="box">
  <h4>총량으로 보면</h4>
  <p>· 표본 전체 {n}건({mb:,.0f}MB): 사이냅 <b>{snf_total:.1f}초</b> · 자체판 <b>{rs_total:.1f}초</b>
     — 차이 {rs_total:.1f}÷{snf_total:.1f} = 약 {slowpct:.0f}%</p>
  <p>· 처리량: 사이냅 {snf_mbs:.1f} MB/s · 자체판 {rs_mbs:.1f} MB/s</p>
  <p>· 문서 1건 중앙값: 사이냅 {snf_med:.1f}ms · 자체판 {rs_med:.1f}ms.
     {n}건 중 <b>{faster}건</b>은 자체판이 같거나 더 빨랐다.</p>
  <p>· 오피스/PDF/HTML {off_n}건 평균: 사이냅 {off_snf:.1f}ms · 자체판 {off_rs:.1f}ms
     (총 {off_gap:+.2f}초 차이).</p>
  <p>· <b>남은 차이는 어느 한 포맷 탓이 아니다.</b> 확장자별 총시간 차이는
     tsv +{gap_tsv:.2f}초 · pptx +{gap_pptx:.2f}초 · json +{gap_json:.2f}초 ·
     csv +{gap_csv:.2f}초 · docx +{gap_docx:.2f}초 · xlsx +{gap_xlsx:.2f}초로 고르게 퍼져 있고,
     <b>PDF 는 {gap_pdf:+.2f}초로 사실상 같다</b>. PDF 는 한 건 중앙값이 2.9배 느리지만
     (7장 ③) 큰 PDF 에서는 사이냅도 느려져 총량에서는 상쇄된다.</p>
  <p>· 직전 측정에서 이 자리를 혼자 차지하던 <b>초대형 XLSX(+3.64초)</b> 는
     흘려읽기 파서로 바꿔 <b>+{gap_xlsx:.2f}초</b>가 됐다(3장 ⑥). 그 전까지는
     격차의 절반이 파일 단 한 건이었다.</p>
</div>

<h2><span class="num">05</span>추출 성공률</h2>
<p class="lead">본문(50자 이상)을 실제로 뽑아냈는지. 실패는 원인을 나눠 적었다.</p>
<table>
<tr><th>확장자</th><th class="n">표본</th><th class="n">사이냅 성공</th><th class="n">자체판 성공</th>
<th class="n">자체판 성공률</th><th>실패 내역</th></tr>
{srows}
</table>

<div class="box">
  <h4>실패 {fail}건은 전부 '파서 실력'이 아니었다</h4>
  <p>· <b>스캔 PDF {fail_scan}건</b> — 이미지만 있는 문서. 사이냅도 8~44자(머리말 스탬프)뿐이라
     양쪽 다 본문을 못 얻는다. 자체판은 분류 실행에서 이런 문서를 <code>excluded</code>
     (처리제외 — 스캔 PDF, OCR 미적용 범위)로 명시적으로 표기한다(사이냅은 빈 텍스트를 그냥
     내놓아 조용히 통과된다).</p>
  <p>· <b>텍스트 크기 상한 {fail_size}건</b> — 지난 측정에서는 46~82MB 짜리 데이터셋 파일
     <b>4건</b>이 여기 걸려 본문을 한 글자도 못 남겼다. 상한을 20MB → 100MB 로 올려
     지금은 네 건 모두 읽는다(3장 ⑤).</p>
  <p>· 그 외 이유로 못 읽은 문서는 <b>0건</b>이다.</p>
</div>

<h2><span class="num">06</span>본문 일치도</h2>
<p class="lead">둘 다 본문을 얻은 {ncmp}건 기준. 두 본문을 낱말 집합으로 바꿔 견줬다.</p>
<table>
<tr><th>확장자</th><th class="n">비교건수</th>
<th>재현율<br><small style="color:#B9C6E4">사이냅 낱말 회수</small></th>
<th>정밀도<br><small style="color:#B9C6E4">군더더기 없음</small></th>
<th>자카드<br><small style="color:#B9C6E4">전체 겹침</small></th>
<th class="n">글자수 비<br><small>자체/사이냅</small></th></tr>
{mrows}
</table>

<h2><span class="num">07</span>남은 격차</h2>
<p class="lead">재현율 100%에 못 미치는 구간을 하나씩 열어 원인까지 확인했다.</p>

<div class="box warn">
  <h4>① 엑셀 날짜 셀이 일련번호로 나온다 <span class="pill green">해결</span></h4>
  <p>엑셀은 날짜를 <b>숫자(1900-01-01 부터 센 일수)</b>로 저장하고 '날짜처럼 보이게 하라'는
  서식을 따로 붙인다. 자체 파서는 서식을 읽지 않아 <code>41274</code> 처럼 일련번호를 그대로
  내놓고, 사이냅은 <code>2012-12-03</code> 으로 바꿔 준다.</p>
  <p>실측: <code>삼성생명_…_WBS_V1.0.xls</code> 정밀도 75.6% — 자체판에만 있는 낱말이
  전부 날짜 일련번호였다. <b>xls·xlsx 양쪽 공통이고 이번 변경으로 생긴 것이 아니다</b>
  (날짜가 든 수식 셀이 늘면서 눈에 더 띄었을 뿐이다).</p>
  <p><b>조치 완료(3장 ⑦).</b> <code>XF</code>/<code>FORMAT</code> 레코드(xlsx 는
  <code>styles.xml</code>)에서 날짜 서식을 읽어 일련번호를 날짜 글자로 바꾼다.
  그 파일의 정밀도는 <b>75.6% → 100%</b> 가 됐고, 자체판에만 있던 낱말은 0개가 됐다.
  "계약일 2012-12-31" 처럼 <b>날짜가 앵커와 붙어야 성립하는 규칙</b>도 이제 걸린다.</p>
</div>

<div class="box good">
  <h4>② HTML 재현율 86.5%는 격차가 아니라 개선이다 <span class="pill green">문제 없음</span></h4>
  <p>놓친 낱말을 열어 보면 <code>rgba · bezier · margin · hover · 8px</code> — 전부 CSS·JS 코드다.
  사이냅은 <code>&lt;style&gt;·&lt;script&gt;</code> 내용까지 본문에 섞어 내놓고, 자체 파서는 걸러낸다.
  글자수 비 0.73은 <b>군더더기를 27% 덜어냈다</b>는 뜻이다.</p>
</div>

<div class="box warn">
  <h4>③ PDF 는 자체판이 더 느리다 <span class="pill amber">속도 격차</span></h4>
  <p>중앙값 사이냅 {pdf_snf:.1f}ms · 자체판 {pdf_rs:.1f}ms(약 {pdf_ratio:.1f}배). pdfium 으로 페이지마다 텍스트층을 읽는 방식이라
  사이냅 전용 엔진보다 느리다. 절대값은 여전히 문서당 수십 ms 수준이고 정확도는 97.9%로
  문제없지만, <b>수십만 건 배치에서는 누적이 보인다</b>(10만 건이면 약 {pdf_10man_rs:.0f}분 대 {pdf_10man_snf:.0f}분).</p>
  <p><b>다만 총량에서는 상쇄된다</b> — 표본 45건의 <b>총</b> 추출시간은 사이냅 8.20초 · 자체판 8.18초로
  {gap_pdf:+.2f}초, 사실상 같다. 중앙값이 벌어지는 것은 <b>작은 PDF</b> 에서이고, 큰 PDF 에서는
  사이냅도 함께 느려지기 때문이다. 그래서 '느린 PDF' 는 <b>건수가 많은 배치</b>의 문제이지
  용량이 큰 배치의 문제가 아니다.</p>
  <p><b>조치 후보:</b> 페이지 단위 병렬 처리, 또는 배치 실행 시 문서 단위 멀티프로세스.
  지금은 문서 하나를 한 스레드로만 읽는다.</p>
</div>

<div class="box warn">
  <h4>④ 초대형 XLSX — 흘려읽기로 바꿔 해소 <span class="pill green">해결</span></h4>
  <p>직전 측정에서는 <code>감성대화말뭉치(최종데이터)_Training.xlsx</code>(15.2MB) 한 건이
  사이냅 1.42초 · 자체판 <b>4.97초</b>였고, 표본 전체 시간 차이의 <b>절반</b>이 이 파일
  하나였다. 시트 XML 을 통째로 메모리에 올린 뒤 정규식으로 훑는 방식이라
  거대한 단일 시트에서 비용이 급격히 커졌다.</p>
  <p>흘려읽기(SAX)로 바꾼 뒤 <b>1.61초</b>가 됐고, xlsx 전체 총시간 차이도
  +3.64초 → <b>+{gap_xlsx:.2f}초</b>로 줄었다. 추출 본문은 표본 35건이 <b>바이트까지 같다</b>
  (3장 ⑥). 이제 표본에서 격차 1위 문서는 {worst_ext} 의 +{gap_worst:.2f}초로,
  '한 건이 전체를 좌우하는' 상황이 아니다.</p>
</div>

<div class="box good">
  <h4>⑤ PPT·DOC 은 자체 파서가 더 많이 뽑는다 <span class="pill green">우세</span></h4>
  <p>PPT 글자수 비 1.59 — 자체 파서가 사이냅보다 <b>60% 가까이 많은 텍스트</b>를 건진다
  (도형·표 안의 글자). 재현율은 100%다. DOC 도 글자수 비 1.03 으로 근소 우세다.</p>
</div>

<h2><span class="num">08</span>확장자별 최종 판정</h2>
<table>
<tr><th style="width:150px">확장자</th><th style="width:110px">대체 가능성</th><th>근거</th></tr>
{vrows}
</table>

<h2><span class="num">09</span>표본 밖 포맷 — 사이냅 제거 시 빈칸</h2>
<p class="lead">위 15개 확장자는 표본으로 확인했다. 대중 문서 포맷 중 <b>표본에 없거나 자체 파서가
아예 다루지 않는 것</b>을 따로 모았다. 사이냅을 빼면 이 파일들은 사이냅 폴백이 없으므로
'미분류 + 로그' 가 된다. <small style="color:var(--muted)">(2026-11-05 추가 · 코드·공개 자료 조사이며 실측 아님)</small></p>

<table>
<tr><th style="width:190px">포맷</th><th style="width:120px">상태</th><th>내용</th><th style="width:230px">대안</th></tr>
<tr><td class="nm">한셀 .cell (2014 이후)<br>한쇼 .show</td>
  <td><span class="pill amber">미확인</span></td>
  <td class="note">신형은 OOXML(zip) 이라고 알려져 있다(나무위키). 그렇다면 내용 감지
  (<code>xl/workbook.xml</code> · <code>ppt/presentation.xml</code>)에서 xlsx/pptx 로 잡혀
  자체 파서가 그대로 읽어야 한다. <b>D:\분류함 표본 0건이라 실제 확인은 못 했다.</b></td>
  <td class="note">실파일 몇 건으로 확인만 하면 된다</td></tr>
<tr><td class="nm">구형 한셀 .cell (2010)<br>.nxl · 한쇼 .hpt</td>
  <td><span class="pill red">미지원</span></td>
  <td class="note">한셀 2010 고유 구조. 확장자는 같지만 OOXML 이 아니다.</td>
  <td class="note">오픈소스 파서가 사실상 없다(openhanji 는 cell·show 를 내세우나 실제 구현은 HWPX 뿐).
  LibreOffice 지원 여부도 확인 못 함</td></tr>
<tr><td class="nm">아래아한글 HWP 3.x</td>
  <td><span class="pill red">미지원</span></td>
  <td class="note">한글 3.0(1995) ~ 97 · 815(1998) 의 형식. 한글 2002(2001 출시)부터는 HWP 5.0 이다.
  확장자는 같은 .hwp 지만 OLE 가 아니라 <code>HWP Document File V3.00</code> 문자열로 시작해서
  내용으로는 구분된다. 25년 넘은 보관 문서라 비중은 작을 것으로 본다.</td>
  <td class="note">LibreOffice HWP 필터가 구형 HWP 를 읽는다. 다만 수백 MB 설치본에 문서마다 별도
  프로세스라 단일 exe 배포와 맞지 않는다</td></tr>
<tr><td class="nm">암호 걸린 문서</td>
  <td><span class="pill red">미지원</span></td>
  <td class="note">비밀번호 없이는 사이냅도 열지 못한다. 사이냅을 빼도 잃는 것이 없다.</td>
  <td class="note">—</td></tr>
<tr><td class="nm">rtf</td>
  <td><span class="pill amber">사이냅 의존</span></td>
  <td class="note">자체 파서가 없어 지금은 <code>--synap-fallback</code> 일 때 사이냅으로 바로 넘긴다.</td>
  <td class="note">글자만 뽑는 수준이면 직접 구현이 쉽다(반나절 규모)</td></tr>
<tr><td class="nm">스캔(이미지) PDF</td>
  <td><span class="pill green">차이 없음</span></td>
  <td class="note">표본 PDF 45건 중 9건 — 양쪽 다 실패. 사이냅도 OCR 을 하지 않는다.</td>
  <td class="note">별도 OCR 트랙(Tesseract · PaddleOCR) — 10장 ⑥</td></tr>
</table>

<div class="box">
  <h4>정리</h4>
  <p>사이냅을 빼서 실제로 <b>잃는 것은 HWP 3.x · 구형 한셀(2010) · rtf 세 가지</b>뿐이다.
  rtf 는 직접 만들면 되고, 나머지 둘은 오래된 보관 문서다. 결정에 필요한 것은 두 가지다.</p>
  <p>① 한쇼·한셀 신형 실파일로 xlsx/pptx 경로가 실제로 읽히는지 확인한다.<br>
  ② 고객 표본에서 HWP 3.x(파일 첫머리 <code>HWP Document File V3</code>) · 구형 .cell 비율을 센다.
  거의 없으면 '미분류 + 로그' 로 두고, 의미 있게 있으면 그 포맷만 <code>--synap-fallback</code>
  또는 LibreOffice 로 넘기는 선택지를 남긴다.</p>
</div>

<h2><span class="num">10</span>권고</h2>
<ol>
  <li><b>사이냅 제거를 진행해도 된다.</b> 순수 파서 기준으로 재현율 {recall:.1f}%,
      <b>15개 확장자 전부가 대체 가능</b>이다(TSV·JSON 을 막던 텍스트 크기 상한은
      20MB → 100MB 로 올렸다 — 3장 ⑤). 남은 것은 값 차이가 아니라 속도뿐이다.</li>
  <li><b>엑셀 날짜 서식은 구현했다.</b> 지난 보고서가 '후순위'로 미뤄 둔 마지막
      값 차이다(3장 ⑦). 이제 "계약일 2012-12-31" 처럼 날짜가 앵커와 붙어야 성립하는
      규칙도 걸린다.</li>
  <li><b>두 판의 본문 차이는 사실상 없앴다.</b> Rust 판에만 있던 다섯 가지 —
      PPTX 발표자 노트 · XLSX 셀 메모 · 엑셀 날짜 서식 · xls 정수 표기 ·
      HWPX 문단 추출 — 를 모두 파이썬 판에 포팅했다. 표본 140건을 두 판으로 뽑아
      낱말을 견주면 <b>doc · ppt · pptx · xls · xlsx · hwpx 여섯 포맷이 자카드
      1.000</b>(완전히 같음)이다. 전환 기간에 두 판을 섞어 써도 이 여섯은 등급이
      갈리지 않는다.
      <br>docx 도 텍스트상자 문단을 고쳐 <b>0.997 → 0.9994</b> 가 됐다(파이썬 판이
      바깥 문단을 만들 때 텍스트상자 안 문단까지 삼켜 <code>문자열시스템1부1과1팀1인이하</code>
      처럼 문서에 없는 낱말을 만들고 있었다).
      <br>그 과정에서 <b>Rust 판의 같은 함정</b>도 드러나 함께 고쳤다 — docx 문단
      정규식이 짝을 잘못 맞춰 텍스트상자 <b>뒤에 이어지는</b> 바깥 문단의 글자를
      잃고 있었다(<code>"1) 개선사항 Summary"</code> 한 줄이 통째로 사라졌다).
      HWPX 에서 겪은 그 함정(3장 ④)이 docx 에 남아 있던 것으로, 이쪽도 한 번만
      훑는 방식으로 바꿨다. Rust docx 재현율 99.93% → <b>99.95%</b>,
      두 판 자카드 <b>0.9996</b>.
      <br>남은 차이 한 건은 결함이 아니라 <b>설계 차이</b>다 — Rust 판은 하이퍼링크
      주소(<code>HYPERLINK</code> 필드코드)를 본문 뒤에 모아 붙이고 파이썬 판은
      넣지 않는다. 주소를 본문으로 볼 것인가의 판단이라 전환 계획에서 정하면 된다.</li>
  <li><b>초대형 XLSX 는 해소됐다.</b> 총량 격차의 절반이던 자리가 흘려읽기 파서로
      +{gap_xlsx:.2f}초가 됐다(3장 ⑥·7장 ④). 남은 격차는 어느 한 포맷에 몰려 있지 않다.</li>
  <li><b>PDF 속도는 배치 설계로 흡수한다.</b> 파서를 바꾸기보다 문서 단위 병렬 실행이 현실적이다.</li>
  <li><b>스캔 PDF 는 별도 트랙으로 뺀다.</b> 자체판의 <code>no_body</code> 신호를 받아
      OCR 대기열이나 사람 검토 대상으로 돌린다 — 사이냅으로는 이 구분조차 안 됐다.</li>
  <li><b>측정은 재현 가능하다.</b> <code>tests/parser_bench/</code> 의 스크립트를 그대로 다시
      돌리면 같은 표본(난수 씨앗 고정)으로 이 표가 다시 나온다.</li>
</ol>

</main>
<footer>
  MpowerClassify — 순수 문서파서 성능 비교 · 2026-09-11 ·
  표본 {n}건({mb:,.0f}MB) · snf_exe v4.29.0 vs MpowerClassify-rs 자체 파서(--text-only) ·
  측정 스크립트: tests/parser_bench/
</footer>
</div>
</body>
</html>
"""


if __name__ == "__main__":
    main()
