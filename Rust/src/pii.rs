//! PII 검출기 (ko-pii 전면 포팅 — 체크섬·문맥앵커·행정구역 사전·겹침해소 포함).
//! 구현(20라벨): RRN·FRN·CARD·BUSINESS_REG·CORP_REG·PHONE·EMAIL·ACCOUNT·PASSPORT·IP·URL·
//!   VEHICLE·NATIONALITY·ADDRESS·DRIVER_LICENSE·MEDICAL_INSURANCE·PRESCRIPTION_ID·EDI_DRUG·
//!   COURT_CASE·PNU. (MpowerClassify 가 cso_rule.yaml 에서 쓰는 라벨 전량.)
//!
//! 동작 순서(ko-pii detect_all 동일):
//!   1) 요청 라벨의 검출기만 실행 → 각자 (span, risk, confidence) 방출
//!   2) core.overlap.resolve_overlaps 로 라벨 간 겹침 해소(위험도→확신도→길이→시작 우선)
//!   3) 살아남은 span 을 라벨별로 집계
//! ※ Rust regex 는 look-around 미지원 → 앞뒤 경계·문맥은 코드로 검사한다.

use std::collections::{HashMap, HashSet};
use std::time::{SystemTime, UNIX_EPOCH};

use once_cell::sync::Lazy;
use regex::Regex;

use crate::districts;
use crate::pii_fold;

/// 이 포팅이 '어느 ko-pii 를 보고 옮겨 적었는가'.
//=> Python 판은 ko-pii 를 exe 안에 넣어 다니지만 이 판은 알고리즘을 베껴 적은
//   사본이라, 번들 버전 같은 게 없다. 그래서 두 판을 나란히 놓고 "같은 기준이냐"를
//   따지려면 이 값이 필요하다(--version 이 이 값을 찍는다).
//   손으로 적은 값이 낡지 않도록, golden 테스트가 정답표의 kopii_version 과
//   같은지 매번 확인한다 — 정답표를 새 ko-pii 로 다시 뽑으면 여기가 먼저 깨진다.
pub const KOPII_PORTED_FROM: &str = "1.15.2";

/// 검출 1건 — 라벨 간 겹침 해소에 필요한 최소 정보(ko-pii DetectionResult 축약).
#[derive(Clone)]
struct Det {
    start: usize,
    end: usize,
    risk: i32,   // RiskLevel: INFO=1 LOW=2 MEDIUM=3 HIGH=4 CRITICAL=5
    conf: f64,
    label: &'static str,
}

// RiskLevel 상수(ko-pii core/types.RiskLevel).
const R_INFO: i32 = 1;
const R_LOW: i32 = 2;
const R_MED: i32 = 3;
const R_HIGH: i32 = 4;
const R_CRIT: i32 = 5;

//------------------------------------------------------------------
// 숫자 흉내 글자 → 숫자 (ko-pii _DIGIT_HOMOGLYPH 와 1:1)
//=> 주민번호의 0 을 알파벳 O 로, 1 을 소문자 l 로 바꿔 적으면 정규식이 못 잡는다.
//   그 회피를 막으려고 ko-pii 는 이 글자들을 숫자로 되돌린다.
//
// -in: ch = 검사할 글자
//
// -out: Some(숫자글자) = 숫자를 흉내 낸 글자였다 / None = 아니다
// -out: error = 예외 없음
//------------------------------------------------------------------
fn digit_homoglyph(ch: char) -> Option<char> {
    match ch {
        'O' | 'o' | 'Q' => Some('0'),
        'l' | 'I' | '|' => Some('1'),
        'S' => Some('5'),
        'B' => Some('8'),
        'Z' => Some('2'),
        'G' => Some('6'),
        _ => None,
    }
}

//------------------------------------------------------------------
// 숫자열 토큰 안의 흉내 글자만 골라 숫자로 되돌린다
//=> 아무 데서나 바꾸면 "Seoul" 이 "5eou1" 이 되어 멀쩡한 낱말을 망친다. 그래서
//   ko-pii 는 '숫자열처럼 생긴 토막' 안에서만 바꾸고, 그 토막에 진짜 숫자가
//   2개 이상 + 흉내 글자가 1개 이상일 때만 손댄다(_fold_digit_homoglyphs).
//    1) 숫자나 흉내 글자로 시작해 숫자·흉내글자·하이픈이 이어지는 토막을 찾는다
//    2) 그 토막의 진짜 숫자 수와 흉내 글자 수를 센다
//    3) 조건(숫자>=2, 흉내>=1)을 넘길 때만 바꾼다 — 글자 수는 그대로라 위치가 안 밀린다
//
// -in: s = 이미 전각·제로폭까지 정리된 문자열
//
// -out: out = 흉내 글자가 숫자로 바뀐 문자열(길이 동일)
// -out: error = 예외 없음
//------------------------------------------------------------------
fn fold_digit_homoglyphs(s: &str) -> String {
    // 토막에 이어붙을 수 있는 글자인가(숫자·흉내글자·각종 하이픈).
    let cont = |c: char| {
        c.is_ascii_digit() || digit_homoglyph(c).is_some()
            || c == '-' || ('\u{2010}'..='\u{2015}').contains(&c)
    };
    let chars: Vec<char> = s.chars().collect();
    let mut out = String::with_capacity(s.len());
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        // 토막은 숫자 또는 흉내 글자로만 시작한다(하이픈으로는 시작 못 한다).
        if !(c.is_ascii_digit() || digit_homoglyph(c).is_some()) {
            out.push(c);
            i += 1;
            continue;
        }
        // 이어지는 데까지 최대한 길게 잡는다(정규식의 탐욕적 매칭과 같게).
        let start = i;
        let mut end = i + 1;
        while end < chars.len() && cont(chars[end]) {
            end += 1;
        }
        let tok = &chars[start..end];
        let digits = tok.iter().filter(|c| c.is_ascii_digit()).count();
        let hg = tok.iter().filter(|c| digit_homoglyph(**c).is_some()).count();
        // 진짜 숫자가 2개 이상이고 흉내 글자가 있을 때만 — 우연한 낱말을 안 건드린다.
        if digits >= 2 && hg >= 1 {
            for &c in tok {
                out.push(digit_homoglyph(c).unwrap_or(c));
            }
        } else {
            out.extend(tok.iter());
        }
        i = end;
    }
    out
}

//------------------------------------------------------------------
// 이 글자가 '덧붙는 표시'인가 (결합표시·조합용 한글 자모)
//=> 표시는 앞 글자에 얹혀 쓰는 기호다. 숫자 사이에 끼워 넣으면 정규식이
//   숫자열을 못 알아보므로 회피 수단이 된다.
//
// -in: ch = 검사할 글자
//
// -out: true = 표시 글자다 / false = 아니다
// -out: error = 예외 없음
//------------------------------------------------------------------
#[inline]
fn is_mark(ch: char) -> bool {
    let cp = ch as u32;
    // 표시는 U+0300 위쪽에만 있다 — ASCII 는 즉시 뺀다.
    if cp < 0x300 { return false; }
    pii_fold::MARK_RANGES
        .binary_search_by(|&(a, b)| if cp < a { std::cmp::Ordering::Greater }
                                    else if cp > b { std::cmp::Ordering::Less }
                                    else { std::cmp::Ordering::Equal })
        .is_ok()
}

//------------------------------------------------------------------
// 자동 생성표에서 이 글자가 어떻게 바뀌는지 찾는다
//=> 표는 U+0000~U+2FFFF 에 드문드문 흩어져 있다. 글자마다 이진탐색을 돌리면
//   한글 본문에서 헛품이 크므로, 상위 8비트(페이지)로 먼저 O(1) 로 거른다.
//   한글 음절 페이지에는 항목이 없어 한국어 본문은 탐색을 아예 안 탄다.
//
// -in: ch = 검사할 글자
//
// -out: Fold::Keep = 그대로 / Drop = 지움 / One(c) = 한 글자로 / Many(s) = 여러 글자로
// -out: error = 예외 없음
//------------------------------------------------------------------
//------------------------------------------------------------------
// 기준 글자 + 덧붙는 표시를 한 글자로 합친다 (유니코드 정준 합성)
//=> "c" 에 "◌́" 를 얹으면 "ć" 한 글자가 된다. 표는 gen_pii_fold.py 가
//   유니코드 데이터에서 뽑았다(합성 제외 규칙까지 반영).
//
// -in: base = 앞 글자
// -in: mark = 얹을 표시 글자
//
// -out: Some(합쳐진 글자) = 합칠 수 있다 / None = 못 합친다(그대로 둔다)
// -out: error = 예외 없음
//------------------------------------------------------------------
#[inline]
fn compose(base: char, mark: char) -> Option<char> {
    let key = (base as u32, mark as u32);
    match pii_fold::COMPOSE.binary_search_by_key(&key, |&(k, _)| k) {
        Ok(i) => char::from_u32(pii_fold::COMPOSE[i].1),
        Err(_) => None,
    }
}

enum Fold { Keep, Drop, One(char), Many(&'static str) }

#[inline]
fn lookup_fold(ch: char) -> Fold {
    let cp = ch as u32;
    let page = (cp >> 8) as usize;
    // 사전거름 — 표가 손댈 글자가 없는 페이지면 여기서 끝난다.
    if page >= pii_fold::PAGE_HAS.len() || !pii_fold::PAGE_HAS[page] {
        return Fold::Keep;
    }
    if pii_fold::REMOVE.binary_search(&cp).is_ok() {
        return Fold::Drop;
    }
    if let Ok(i) = pii_fold::FOLD_ONE.binary_search_by_key(&cp, |&(k, _)| k) {
        return Fold::One(pii_fold::FOLD_ONE[i].1);
    }
    if let Ok(i) = pii_fold::FOLD_MANY.binary_search_by_key(&cp, |&(k, _)| k) {
        return Fold::Many(pii_fold::FOLD_MANY[i].1);
    }
    Fold::Keep
}

/// 유니코드 정규화 — ko-pii normalize_unicode 를 같은 차례로 옮긴 것.
//=> 검출 전에 글자를 '펴서' 회피를 막는다. 세 갈래가 순서대로 돈다.
//    1) 숫자 흉내 글자(O→0, l→1) — 반드시 맨 앞. ko-pii 도 NFKC '앞'에서 편다.
//       뒤로 미루면 전각 Ｏ 가 O 를 거쳐 0 이 되어 우리만 잡는 오탐이 난다.
//    2) 표시 글자(결합표시·자모) — 앞선 글자가 낱말이 아니면 지운다.
//       숫자 사이에 끼워 PII 를 쪼개는 회피를 막되, é 같은 정상 결합은 살린다.
//    3) 나머지는 자동 생성표대로(안 보이는 문자 지움 / 한 글자 / 여러 글자).
//   표는 scripts/gen_pii_fold.py 가 ko-pii 에 물어 만든다(src/pii_fold.rs).
//------------------------------------------------------------------
// 정규화 + 원본 좌표 역매핑표
//=> 정규화는 글자를 지우거나(안 보이는 문자) 늘리므로(㈜→(주)) 위치가 밀린다.
//   ko-pii 는 검출 결과를 '원본 좌표'로 되돌려 주므로, 우리도 같은 표가 있어야
//   원본 기준으로 맞출 수 있다. map[i] = 정규화 i번째 바이트가 온 원본 바이트 위치.
//
// -in: text = 원본 문자열
//
// -out: (norm, map) = (정규화 문자열, 바이트별 원본 위치표). map 길이는 norm 길이와 같다
// -out: error = 예외 없음
//------------------------------------------------------------------
pub fn normalize_with_map(text: &str) -> (String, Vec<usize>) {
    // 1) 흉내 글자 폴딩은 길이가 그대로라 원본 위치가 안 밀린다(1:1).
    let folded = fold_digit_homoglyphs(text);
    let mut out = String::with_capacity(folded.len());
    let mut map: Vec<usize> = Vec::with_capacity(folded.len());
    let mut base_is_word = false;
    // 지금 '한 덩어리(클러스터)' 안에 있는가.
    //=> ko-pii 는 기준 글자 하나에 뒤따르는 표시들을 묶어 한 덩어리로 본다.
    //   안 보이는 문자는 건너뛰면서 덩어리를 끊는다. 덩어리가 끊긴 뒤 나오는
    //   표시는 '얹힌 것'이 아니라 스스로 기준 글자가 되어 살아남는다.
    //   (예: "RX-<소프트하이픈>ᄀ124567" 에서 ᄀ 는 지워지지 않는다.)
    let mut in_cluster = false;
    for (src_i, ch) in folded.char_indices() {
        if is_mark(ch) {
            if in_cluster {
                // 앞 기준 글자에 얹힌 표시 — 기준이 낱말이 아니면(숫자·기호) 지운다.
                if !base_is_word { continue; }
                // 낱말 위의 표시는 살리되, 유니코드가 정한 대로 '한 글자로 합친다'.
                //=> ko-pii 는 덩어리를 통째로 NFKC 해서 "c"+"◌́" 가 "ć" 가 된다.
                //   합치지 않고 두 글자로 두면, 이메일 도메인 같은 데서 우리만
                //   "…mail.c" 까지 잡는 오탐이 난다(실측된 갈라짐).
                if let Some(prev) = out.chars().next_back() {
                    if let Some(c) = compose(prev, ch) {
                        let cut = out.len() - prev.len_utf8();
                        let src_of_prev = map[cut];
                        out.truncate(cut);
                        map.truncate(cut);
                        out.push(c);
                        map.resize(out.len(), src_of_prev);
                        continue;
                    }
                }
            } else {
                // 덩어리 밖에서 만난 표시는 스스로 기준 글자가 된다.
                base_is_word = ch.is_alphabetic();
                in_cluster = true;
            }
            let before = out.len();
            out.push(ch);
            map.resize(before + ch.len_utf8(), src_i);
            continue;
        }
        base_is_word = ch.is_alphabetic();
        let before = out.len();
        match lookup_fold(ch) {
            Fold::Keep => { out.push(ch); in_cluster = true; }
            // 안 보이는 문자는 지워지면서 덩어리를 끊는다(위 in_cluster 설명 참고).
            Fold::Drop => { in_cluster = false; }
            Fold::One(c) => { out.push(c); in_cluster = true; }
            Fold::Many(t) => { out.push_str(t); in_cluster = true; }
        }
        // 이 글자가 만든 바이트 전부를 같은 원본 위치로 가리킨다.
        map.resize(out.len().max(before), src_i);
    }
    (out, map)
}

// (normalize 제거) — 역매핑표가 필요해지면서 normalize_with_map 하나로 합쳤다.
// 표만 버리고 문자열만 쓰고 싶으면 normalize_with_map(t).0 을 쓰면 된다.

/// 원하는 라벨들에 대해 텍스트에서 건수를 센다(겹침해소 후 집계).
pub fn pii_counts(text: &str, wanted: &HashSet<String>) -> HashMap<String, u32> {
    let mut counts: HashMap<String, u32> = HashMap::new();
    if text.is_empty() || wanted.is_empty() {
        return counts;
    }
    let (kept, _base) = detect_all_passes(text, wanted);
    // 라벨별 집계. 처방전은 검출라벨이 PRESCRIPTION_ID 이나 yaml 별칭도 함께 채운다.
    for d in kept {
        if wanted.contains(d.label) {
            *counts.entry(d.label.to_string()).or_insert(0) += 1;
        }
        if d.label == "PRESCRIPTION_ID" && wanted.contains("PRESCRIPTION") {
            *counts.entry("PRESCRIPTION".to_string()).or_insert(0) += 1;
        }
    }
    counts
}

/// [프라이버시 예외] 검출된 PII '원문 값'을 반환(--with-pii 전용). ko-pii collect_pii 대응.
///
/// 반환: (label, value, start, end).
/// **좌표는 추출·정제가 끝난 본문의 '문자' 수**다(바이트가 아니다). 파이썬 판이
/// 그렇게 세고, 받는 쪽이 이 좌표로 마스킹하기 때문이다.
///
/// [왜 바꾸나] Rust 문자열 인덱스는 바이트 단위라 예전에는 바이트 좌표가 그대로
/// 나갔다. 한글은 3바이트여서 앞에 한글이 많을수록 벌어진다 — 실측에서 같은 값이
/// 파이썬 13, Rust 31 이었다. 오류도 안 나고 **엉뚱한 자리를 지우게** 되는 종류라,
/// 내보내기 직전에 여기서 맞춘다(2026-09-11).
pub fn pii_records(text: &str, wanted: &HashSet<String>) -> Vec<(&'static str, String, usize, usize)> {
    if text.is_empty() || wanted.is_empty() { return Vec::new(); }
    let (kept, base) = detect_all_passes(text, wanted);
    let out: Vec<(&'static str, String, usize, usize)> = kept.into_iter()
        .filter(|d| wanted.contains(d.label))
        // 값·위치는 원본 좌표 기준이다(ko-pii remap_to_source 와 같은 불변식).
        .map(|d| (d.label, base[d.start..d.end].to_string(), d.start, d.end))
        .collect();
    to_char_offsets(&base, out)
}

//------------------------------------------------------------------
// 바이트 좌표 → 문자 좌표
//=> 필요한 경계만 모아 본문을 **한 번만** 훑는다. 검출 1건마다 앞에서부터
//   글자를 다시 세면 문서가 길수록 제곱으로 느려진다.
//   (resolve_overlaps 가 이미 문서 순서로 정렬해 두므로 경계도 오름차순이다.)
//
// -in: base = 좌표의 기준이 된 본문
// -in: recs = (label, value, 시작바이트, 끝바이트) 목록
//
// -out: 같은 목록, 좌표만 문자 단위로 바뀐 것
// -out: error = 예외 없음
//------------------------------------------------------------------
fn to_char_offsets(base: &str, recs: Vec<(&'static str, String, usize, usize)>)
    -> Vec<(&'static str, String, usize, usize)> {
    if recs.is_empty() { return recs; }
    // 본문이 전부 ASCII 면 바이트 = 문자라 훑을 것도 없다(흔한 경우를 공짜로).
    if base.is_ascii() { return recs; }

    let mut marks: Vec<usize> = Vec::with_capacity(recs.len() * 2);
    for r in &recs { marks.push(r.2); marks.push(r.3); }
    marks.sort_unstable();
    marks.dedup();

    let mut char_of: HashMap<usize, usize> = HashMap::with_capacity(marks.len());
    let mut mi = 0usize;
    let mut last_ci = 0usize;
    for (ci, (bi, _c)) in base.char_indices().enumerate() {
        // '<=' 로 본다 — 혹시 글자 중간을 가리키는 경계가 와도 그 글자 자리로 접는다.
        while mi < marks.len() && marks[mi] <= bi {
            char_of.insert(marks[mi], ci);
            mi += 1;
        }
        last_ci = ci + 1;
        if mi >= marks.len() { break; }
    }
    // 본문 끝을 가리키는 경계(마지막 검출의 end)는 위 루프가 못 만난다.
    while mi < marks.len() {
        char_of.insert(marks[mi], last_ci);
        mi += 1;
    }

    recs.into_iter()
        .map(|(l, v, s, e)| {
            let cs = *char_of.get(&s).unwrap_or(&s);
            let ce = *char_of.get(&e).unwrap_or(&e);
            (l, v, cs, ce)
        })
        .collect()
}

/// 요청 라벨의 검출기만 실행 → 겹침해소된 Det 목록(집계·원문수집 공통).
//------------------------------------------------------------------
// 영숫자 덩어리 길이 나열 (ko-pii _run_shape)
//=> 정규화가 '안 보이는 문자'를 지우면 떨어져 있던 숫자열이 하나로 붙는다.
//   그러면 두 PII 가 한 덩어리로 융합돼 양쪽 검출기가 다 거부하는 누출이 생긴다.
//   그런 일이 있었는지를 '덩어리 길이 나열'이 바뀌었는가로 판정한다.
//
// -in: s = 검사할 문자열
//
// -out: shape = 영숫자 덩어리들의 길이 목록(차례대로)
// -out: error = 예외 없음
//------------------------------------------------------------------
fn run_shape(s: &str) -> Vec<usize> {
    // ko-pii _PII_RUN 과 같은 글자 범위(ASCII 영숫자 + 전각 영숫자).
    let is_run = |c: char| c.is_ascii_alphanumeric()
        || ('\u{FF10}'..='\u{FF19}').contains(&c)
        || ('\u{FF21}'..='\u{FF3A}').contains(&c)
        || ('\u{FF41}'..='\u{FF5A}').contains(&c);
    let mut out = Vec::new();
    let mut cur = 0usize;
    for c in s.chars() {
        if is_run(c) { cur += 1; }
        else if cur > 0 { out.push(cur); cur = 0; }
    }
    if cur > 0 { out.push(cur); }
    out
}

//------------------------------------------------------------------
// 검출 전체 흐름 — 정규화본 검출 + (필요하면) 원본 재검사 합집합
//=> ko-pii detect_all 과 같은 차례다.
//    1) 정규화본에서 검출한다(회피를 편 상태라 더 잘 잡힌다)
//    2) 정규화가 글자를 바꿨으면 결과 위치를 '원본 좌표'로 되돌린다
//    3) 덩어리 모양까지 바뀌었으면 '원본에서도' 한 번 더 검출해 합친다
//       — 안 보이는 문자를 지우다 두 PII 가 붙어 버려 둘 다 놓치는 일을 막는다
//       (원본에서는 그 문자가 경계 노릇을 해 둘 다 잡힌다)
//    4) 합친 결과를 겹침 해소로 정리한다(중복은 여기서 걷힌다)
//
// -in: text   = 원본 본문
// -in: wanted = 검출할 라벨 집합
//
// -out: (dets, base) = (원본 좌표 기준 검출 목록, 값을 떠낼 기준 문자열)
// -out: error = 예외 없음
//------------------------------------------------------------------
fn detect_all_passes(text: &str, wanted: &HashSet<String>) -> (Vec<Det>, String) {
    let (norm, map) = normalize_with_map(text);
    let mut dets = kept_dets_raw(&norm, wanted);

    if norm == text {
        // 바뀐 게 없으면 좌표도 그대로다.
        return (resolve_overlaps(dets), norm);
    }

    // 2) 정규화 좌표 → 원본 좌표.
    let n = map.len();
    for d in dets.iter_mut() {
        d.start = if d.start < n { map[d.start] } else { d.start };
        d.end = if d.end < n { map[d.end] } else if d.end == n { text.len() } else { d.end };
    }
    // 3) 덩어리 모양이 바뀌었을 때만 원본 재검사(제로폭 한 글자로 비용을 두 배 만드는 걸 막는다).
    if run_shape(text) != run_shape(&norm) {
        dets.extend(kept_dets_raw(text, wanted));
    }
    (resolve_overlaps(dets), text.to_string())
}

fn kept_dets_raw(norm: &str, wanted: &HashSet<String>) -> Vec<Det> {
    let has = |l: &str| wanted.contains(l);

    // 1) 요청 라벨의 검출기만 실행 → span 수집.
    //=> 차례가 ko-pii detect.DETECTORS 와 같아야 한다. 겹침 해소는 위험도·확신도·
    //   길이가 모두 같으면 '목록에서 먼저 나온 것'을 남기는데, 차례가 다르면 그
    //   동점에서 승자가 뒤바뀐다. 실제로 건강보험 번호(HIGH 0.9)와 계좌(HIGH 0.9)가
    //   겹칠 때, 계좌를 먼저 돌리는 바람에 건강보험을 통째로 잃고 있었다.
    //   (ko-pii 목록에서 우리가 안 쓰는 검출기 — fax·postal_code·person 등 — 은 뺐다.)
    let mut dets: Vec<Det> = Vec::new();
    if has("RRN") { dets.extend(det_rrn(&norm)); }
    if has("FRN") { dets.extend(det_frn(&norm)); }
    if has("BUSINESS_REG") { dets.extend(det_brn(&norm)); }
    if has("CORP_REG") { dets.extend(det_corp(&norm)); }
    if has("DRIVER_LICENSE") { dets.extend(det_driver_license(&norm)); }
    if has("PASSPORT") { dets.extend(det_passport(&norm)); }
    if has("CARD") { dets.extend(det_card(&norm)); }
    if has("MEDICAL_INSURANCE") { dets.extend(det_medical_insurance(&norm)); }
    if has("PRESCRIPTION_ID") || has("PRESCRIPTION") { dets.extend(det_prescription(&norm)); }
    if has("PNU") { dets.extend(det_pnu(&norm)); }
    if has("PHONE") { dets.extend(det_phone(&norm)); }
    if has("EMAIL") { dets.extend(det_email(&norm)); }
    // 원본 ip.py 는 한 검출기 안에서 IPv4 를 먼저, 그다음 IPv6 를 낸다.
    if has("IP") { dets.extend(det_ipv4(&norm)); dets.extend(det_ipv6(&norm)); }
    if has("VEHICLE") { dets.extend(det_vehicle(&norm)); }
    if has("URL") { dets.extend(det_url(&norm)); }
    if has("ADDRESS") { dets.extend(det_address(&norm)); }
    if has("NATIONALITY") { dets.extend(det_nationality(&norm)); }
    if has("ACCOUNT") { dets.extend(det_account(&norm)); }
    if has("EDI_DRUG") { dets.extend(det_edi_drug(&norm)); }
    if has("COURT_CASE") { dets.extend(det_court_case(&norm)); }

    // 겹침 해소는 원본 재검사까지 합친 뒤 한 번만 한다(detect_all_passes).
    dets
}

/// 겹침 해소: 위험도↑ → 확신도↑ → 길이↑ → 시작↑ 순으로 채택, 채택된 span 과 겹치면 드롭.
/// (ko-pii core/overlap.py resolve_overlaps 동일 정책.)
fn resolve_overlaps(mut items: Vec<Det>) -> Vec<Det> {
    items.sort_by(|a, b| {
        b.risk.cmp(&a.risk)
            .then(b.conf.partial_cmp(&a.conf).unwrap_or(std::cmp::Ordering::Equal))
            .then((b.end - b.start).cmp(&(a.end - a.start)))
            .then(a.start.cmp(&b.start))
    });
    let mut accepted: Vec<Det> = Vec::new();
    for d in items {
        if accepted.iter().any(|a| d.start < a.end && a.start < d.end) {
            continue;
        }
        accepted.push(d);
    }
    // 원본은 채택이 끝나면 문서 순서로 되돌려 준다("반환은 문서 순서(start, end)").
    // 우리는 그 한 줄이 빠져 검출 순서 그대로 나갔고, 두 판의 pii 배열 차례가
    // 달랐다(2026-09-11). 정렬은 '누구를 채택할지'가 아니라 '어떤 차례로 돌려줄지'라
    // 판정에는 영향이 없다 — 채택은 위쪽 우선순위 정렬이 이미 끝냈다.
    accepted.sort_by(|a, b| a.start.cmp(&b.start).then(a.end.cmp(&b.end)));
    accepted
}

// ---- 정규식 (look-around 없음) ----
// RRN/FRN 구분자: 하이픈/점/공백(래핑) — ko-pii 와 동일 규칙.
static RE_RRN: Lazy<Regex> = Lazy::new(|| Regex::new(r"([0-9]{6})(?:\s?[-./]\s?|[-./\s]{0,2})([0-9]{7})").unwrap());
static RE_RRN_PREFIXED: Lazy<Regex> = Lazy::new(|| Regex::new(r"[0-9]([0-9]{6})(?:\s?[-./]\s?|[-./\s]{0,2})([0-9]{7})").unwrap());
// 카드(ko-pii card.py): 구분자 그룹형 또는 무구분 13~19자리.
static RE_CARD: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?:[0-9]{4}[-. /]\s?[0-9]{4}[-. /]\s?[0-9]{4}[-. /]\s?[0-9]{1,7}|[0-9]{13,19})").unwrap());
// 사업자등록번호(ko-pii business_reg.py 와 1:1): 구분자는 '하이픈만' 선택적이다.
// 예전에는 `[-\s]?` 로 공백까지 받아 "436 38 20718" 같은 표를 잡았는데, ko-pii 는
// 안 잡는다 → 우리만 잡으면 멀쩡한 문서가 기밀로 올라가는 오탐이 된다.
// ※ 숫자 표기 주의: ko-pii 는 패턴마다 [0-9](ASCII 만)와 \d(유니코드 숫자, 아랍숫자 ٠ 포함)를
//   가려 쓴다. Rust 의 \d 도 유니코드라 아무 쪽이나 쓰면 결과가 갈린다 — 원본과 같은 쪽을 쓴다.
static RE_BRN: Lazy<Regex> = Lazy::new(|| Regex::new(r"[0-9]{3}-?[0-9]{2}-?[0-9]{5}").unwrap());
// 법인등록번호: 구분자는 RRN 과 같은 규약(하이픈·점·슬래시·공백, 줄바꿈 래핑 포함).
// 예전에는 `[-\s]?` 뿐이라 "110111.0000002" 처럼 점으로 쓴 표기를 놓쳤다 —
// 못 잡으면 문서가 실제보다 낮은 등급을 받으므로 파이썬과 같은 폭으로 맞춘다.
static RE_CORP: Lazy<Regex> = Lazy::new(||
    Regex::new(r"([0-9]{6})(?:\s?[-./]\s?|[-./\s]{0,2})([0-9]{7})").unwrap());
// 전화번호(ko-pii phone.py 와 1:1). 국제 접두는 +82 / 0082 / 82, 그 뒤에 (0) 이
// 끼는 표기까지 받는다. 국제표기에서는 지역번호·통신사번호의 맨 앞 0 이 빠진다(E.123).
const PH_INTL: &str = r"(?:\+82|0082|82)[-.\s]?(?:\(0\)[-.\s]?)?";
static RE_PH_MOBILE: Lazy<Regex> = Lazy::new(||
    Regex::new(r"(01[01679])[-.\s]{0,3}(\d{3,4})[-.\s]{0,3}(\d{4})").unwrap());
static RE_PH_MOBILE_INTL: Lazy<Regex> = Lazy::new(||
    Regex::new(&format!(r"{}(1[01679])[-.\s]{{0,3}}(\d{{3,4}})[-.\s]{{0,3}}(\d{{4}})", PH_INTL)).unwrap());
static RE_PH_SEOUL: Lazy<Regex> = Lazy::new(||
    Regex::new(r"(02)[-.\s)\]]{0,3}(\d{3,4})[-.\s]{0,3}(\d{4})").unwrap());
static RE_PH_SEOUL_INTL: Lazy<Regex> = Lazy::new(||
    Regex::new(&format!(r"{}(2)[-.\s]{{0,3}}(\d{{3,4}})[-.\s]{{0,3}}(\d{{4}})", PH_INTL)).unwrap());
static RE_PH_REGIONAL: Lazy<Regex> = Lazy::new(||
    Regex::new(r"(03[1-3]|04[1-4]|05[1-5]|06[1-4]|070)[-.\s)\]]{0,3}(\d{3,4})[-.\s]{0,3}(\d{4})").unwrap());
static RE_PH_REGION_INTL: Lazy<Regex> = Lazy::new(||
    Regex::new(&format!(r"{}(3[1-3]|4[1-4]|5[1-5]|6[1-4]|70)[-.\s]{{0,3}}(\d{{3,4}})[-.\s]{{0,3}}(\d{{4}})", PH_INTL)).unwrap());
// 대표번호(15xx~18xx, KISA 번호자원 가이드). 4-4 라 제품번호와 모양이 같아 오탐이
// 남지만, 파이썬 판이 recall 을 택한 것과 같은 판단을 그대로 따른다.
static RE_PH_REPRESENT: Lazy<Regex> = Lazy::new(||
    Regex::new(r"(1[5-8]\d{2})[-.\s]{0,3}(\d{4})").unwrap());
// 이메일(ko-pii email.py 와 1:1). 도메인은 '라벨을 점으로 이은 것'이고, 라벨은
// 반드시 영숫자로 시작해서 영숫자로 끝난다(가운데만 하이픈 허용).
// 예전에는 `[A-Za-z0-9.\-]+\.[A-Za-z]{2,}` 한 덩어리였는데 두 방향으로 어긋났다.
//   · 미탐: TLD 를 글자만 받아 example.co1 / example.a 를 못 잡거나 값이 잘렸다.
//   · 오탐: 라벨 구조를 안 봐서 example..com · example-.com · -example.com 을 잡았다.
static RE_EMAIL: Lazy<Regex> = Lazy::new(|| Regex::new(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)+"
).unwrap());
// 여권(ko-pii passport.py): 대문자 prefix 화이트리스트(2자 우선) + 8자리.
static RE_PASSPORT: Lazy<Regex> = Lazy::new(|| Regex::new(r"(PP|PM|PS|PO|PD|PR|PT|M|S|G|O|D|R|T)([0-9]{8})").unwrap());
// URL(ko-pii url.py 와 1:1). (?i) 는 원본의 re.IGNORECASE — HTTPS:// 도 잡는다.
// 제외 문자에 백틱·닫는 괄호/대괄호/중괄호가 들어가는 것이 중요하다. 빠뜨리면
// "자료(https://…)를" 에서 ")를" 까지 URL 로 먹어 값이 오염된다.
// \b 는 원본의 낱말 경계다. 이게 없으면 "자료는5https://…" 처럼 앞 글자에 붙어
// 있는 것까지 URL 로 잡는다(원본은 안 잡는다). 한글도 낱말 글자라 "서https://…"
// 도 같이 걸러진다 — Rust 의 \b 도 파이썬처럼 유니코드 기준이라 동작이 같다.
static RE_URL: Lazy<Regex> = Lazy::new(|| Regex::new(r#"(?i)\bhttps?://[^\s<>"'`)\]}]+"#).unwrap());
// 차량(ko-pii vehicle.py): 2~3자리 + 용도한글 + 4자리.
static RE_VEHICLE: Lazy<Regex> = Lazy::new(|| Regex::new(r"([0-9]{2,3})\s?([가-힣])\s?([0-9]{4})").unwrap());
static RE_IPV4: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}").unwrap());
// 계좌(ko-pii account.py parity): 은행명/‘계좌’ 앵커가 있어야 인정.
const BANK_NAMES: &[&str] = &[
    "국민은행","신한은행","우리은행","하나은행","기업은행","IBK","KEB하나은행","KEB","KB국민은행","KB",
    "농협은행","농협","NH농협","NH","수협은행","수협","Sh수협","SC제일은행","제일은행","씨티은행","한국씨티",
    "외환은행","스탠다드차타드","카카오뱅크","카뱅","카카오페이","토스뱅크","토스뱅킹","토스페이","케이뱅크",
    "K뱅크","네이버페이","부산은행","BNK부산","대구은행","DGB대구","경남은행","BNK경남","광주은행","전북은행",
    "제주은행","JB제주","산업은행","KDB산업","수출입은행","EXIM","한국은행","BOK","우체국","우정사업본부","우정청",
    "새마을금고","MG새마을","MG","신협","신용협동조합","수산업협동조합","농업협동조합","국민","신한","우리","하나",
];
fn bank_alt() -> String { BANK_NAMES.iter().map(|b| regex::escape(b)).collect::<Vec<_>>().join("|") }
static RE_ACCT_KW: Lazy<Regex> = Lazy::new(|| Regex::new(r"계좌\s*(?:번호|번)?\s*:?\s*([0-9][\s\-]*(?:[0-9][\s\-]*){9,19})").unwrap());
static RE_ACCT_BANK_BEFORE: Lazy<Regex> = Lazy::new(|| Regex::new(&format!(r"(?:{})\s*:?\s*([0-9]+(?:[\s\-][0-9]+){{1,4}}|[0-9]{{10,16}})", bank_alt())).unwrap());
static RE_ACCT_BANK_AFTER: Lazy<Regex> = Lazy::new(|| Regex::new(&format!(r"([0-9]+(?:[\s\-][0-9]+){{1,4}}|[0-9]{{10,16}})\s*(?:{})", bank_alt())).unwrap());

fn is_digit(b: u8) -> bool { b.is_ascii_digit() }
#[allow(dead_code)]
fn is_alnum(b: u8) -> bool { b.is_ascii_alphanumeric() }
fn is_digit_or_dot(b: u8) -> bool { b.is_ascii_digit() || b == b'.' }

/// 매치가 앞/뒤 '금지 이웃'에 붙어있지 않은지(=고립된 토큰인지) 검사.
// (isolated 제거) — Match 를 받던 판. 경계 검사는 find_bounded 에 넘겨야 해서
// 위치를 받는 isolated_at 하나로 합쳤다.

//------------------------------------------------------------------
// 앞뒤 경계 판정 — 위치(바이트)로 직접 받는 판
//=> find_bounded 에 넘겨 쓰려면 Match 가 아니라 (시작, 끝) 을 받아야 한다.
//   내용은 isolated 와 같다.
//
// -in: text = 본문
// -in: s, e = 검사할 구간(바이트)
// -in: before_bad = 앞 글자가 이거면 안 된다는 판정 함수
// -in: after_bad  = 뒤 글자가 이거면 안 된다는 판정 함수
//
// -out: true = 경계가 깨끗하다 / false = 토막 중간이다
// -out: error = 예외 없음
//------------------------------------------------------------------
fn isolated_at(text: &str, s: usize, e: usize,
               before_bad: fn(u8) -> bool, after_bad: fn(u8) -> bool) -> bool {
    let b = text.as_bytes();
    let ok_before = s == 0 || !before_bad(b[s - 1]);
    let ok_after = e >= b.len() || !after_bad(b[e]);
    ok_before && ok_after
}

fn digits(s: &str) -> Vec<u8> {
    s.bytes().filter(|b| b.is_ascii_digit()).map(|b| b - b'0').collect()
}

// ---- 오늘 날짜(미래 생년월일 배제용). SystemTime → (년,월,일). ----
static TODAY: Lazy<(i64, u32, u32)> = Lazy::new(|| {
    let secs = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
    civil_from_days(secs / 86400)
});
/// UNIX epoch 기준 일수 → (년,월,일). Howard Hinnant civil_from_days.
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = (z - era * 146097) as i64;              // [0,146096]
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365; // [0,399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0,365]
    let mp = (5 * doy + 2) / 153;                       // [0,11]
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;      // [1,31]
    let m = (if mp < 10 { mp + 3 } else { mp - 9 }) as u32;
    (y + if m <= 2 { 1 } else { 0 }, m, d)
}
/// 그레고리력 유효 날짜인지(윤년 포함).
fn valid_ymd(y: i64, m: u32, d: u32) -> bool {
    if m < 1 || m > 12 || d < 1 { return false; }
    let leap = (y % 4 == 0 && y % 100 != 0) || y % 400 == 0;
    let dim = [31, if leap {29} else {28}, 31,30,31,30,31,31,30,31,30,31][(m-1) as usize];
    d <= dim
}
/// (y,m,d) 가 오늘 이후(미래)인지.
fn is_future(y: i64, m: u32, d: u32) -> bool { (y, m, d) > *TODAY }

// ---- 체크섬(ko-pii checksum/*) ----
/// RRN mod-11 체크섬. 13자리.
fn rrn_checksum_ok(d: &[u8]) -> bool {
    if d.len() != 13 { return false; }
    const W: [u32; 12] = [2,3,4,5,6,7,8,9,2,3,4,5];
    let sum: u32 = (0..12).map(|i| d[i] as u32 * W[i]).sum();
    ((11 - sum % 11) % 10) == d[12] as u32
}
/// 법인등록번호 체크섬(가중치 1,2 교대 straight-sum). 13자리.
fn corp_checksum_ok(d: &[u8]) -> bool {
    if d.len() != 13 { return false; }
    let mut sum = 0u32;
    for i in 0..12 { sum += d[i] as u32 * if i % 2 == 0 { 1 } else { 2 }; }
    ((10 - sum % 10) % 10) == d[12] as u32
}
/// 사업자등록번호 체크섬(국세청). 10자리.
fn brn_checksum_ok(d: &[u8]) -> bool {
    if d.len() != 10 { return false; }
    const W: [u32; 9] = [1,3,7,1,3,7,1,3,5];
    let mut sum: u32 = (0..9).map(|i| d[i] as u32 * W[i]).sum();
    sum += (d[8] as u32 * 5) / 10;
    ((10 - sum % 10) % 10) == d[9] as u32
}
fn luhn_ok(d: &[u8]) -> bool {
    let mut sum = 0u32;
    let mut alt = false;
    for &x in d.iter().rev() {
        let mut v = x as u32;
        if alt { v *= 2; if v > 9 { v -= 9; } }
        sum += v;
        alt = !alt;
    }
    sum % 10 == 0
}

// ================= 주민등록번호 RRN (ko-pii rrn.py) =================
// 성별자리(7번째) → 세기. 내국인 {1,2,3,4,9,0}.
fn rrn_century(g: u8) -> Option<i64> {
    match g { 1|2 => Some(1900), 3|4 => Some(2000), 9|0 => Some(1800), _ => None }
}
fn frn_century(g: u8) -> Option<i64> {
    match g { 5|6 => Some(1900), 7|8 => Some(2000), _ => None }
}
/// RRN 한 후보 판정 → Det(또는 None). span=[start,end)(prefix 제외분은 호출측에서 offset 반영).
fn rrn_emit(front: &[u8], back: &[u8], start: usize, end: usize, real_no_sep: bool) -> Option<Det> {
    let g = back[0];
    let century = rrn_century(g)?;
    let y = century + front[0] as i64 * 10 + front[1] as i64;
    let m = front[2] as u32 * 10 + front[3] as u32;
    let d = front[4] as u32 * 10 + front[5] as u32;
    if !valid_ymd(y, m, d) { return None; }
    if is_future(y, m, d) { return None; }   // 미래 생년 → GS1 880 바코드 등 FP 차단
    let all: Vec<u8> = front.iter().chain(back.iter()).copied().collect();
    let checksum_ok = rrn_checksum_ok(&all);
    // 체크섬 실패 + 성별0 + 법인체크섬 통과 → 법인등록번호로 보고 RRN 미방출.
    if !checksum_ok && g == 0 && corp_checksum_ok(&all) { return None; }
    // 무구분자 880~ GS1/EAN-13 바코드 → RRN 아님.
    if !checksum_ok && real_no_sep && front[0]==8 && front[1]==8 && front[2]==0 { return None; }
    let conf = if checksum_ok { 1.0 } else { 0.7 };
    Some(Det { start, end, risk: R_CRIT, conf, label: "RRN" })
}
fn det_rrn(text: &str) -> Vec<Det> {
    let mut out: Vec<Det> = Vec::new();
    let mut seen: Vec<(usize, usize)> = Vec::new();
    // 기본 패턴. (?<![0-9]) / (?![0-9]) 는 captures_bounded 가 본다 —
    // 경계로 버린 자리에서 '한 글자 뒤부터 다시' 찾아야 이어붙은 주민번호를 놓치지 않는다.
    for m in captures_bounded(&RE_RRN, text, |s, e| isolated_at(text, s, e, is_digit, is_digit)) {
        let whole = m.get(0).unwrap();
        let front = digits(&m[1]); let back = digits(&m[2]);
        let real_no_sep = m[0].chars().filter(|c| !c.is_ascii_digit()).count() == 0;
        if let Some(det) = rrn_emit(&front, &back, whole.start(), whole.end(), real_no_sep) {
            seen.push((det.start, det.end));
            out.push(det);
        }
    }
    // PDF prefix 패턴(관계코드 1자리 뒤 RRN). span 은 offset 1.
    for m in captures_bounded(&RE_RRN_PREFIXED, text,
                              |s, e| isolated_at(text, s, e, is_digit, is_digit)) {
        let whole = m.get(0).unwrap();
        let start = whole.start() + 1;
        let front = digits(&m[1]); let back = digits(&m[2]);
        let real = &text[start..whole.end()];
        let real_no_sep = real.chars().filter(|c| !c.is_ascii_digit()).count() == 0;
        if seen.iter().any(|&(s,e)| s==start && e==whole.end()) { continue; }
        if let Some(det) = rrn_emit(&front, &back, start, whole.end(), real_no_sep) {
            seen.push((det.start, det.end));
            out.push(det);
        }
    }
    out
}
fn det_frn(text: &str) -> Vec<Det> {
    let mut out: Vec<Det> = Vec::new();
    // 경계 검사는 captures_bounded 에 맡긴다(det_rrn 과 같은 이유).
    for m in captures_bounded(&RE_RRN, text, |s, e| isolated_at(text, s, e, is_digit, is_digit)) {
        let whole = m.get(0).unwrap();
        let front = digits(&m[1]); let back = digits(&m[2]);
        let century = match frn_century(back[0]) { Some(c) => c, None => continue };
        let y = century + front[0] as i64 * 10 + front[1] as i64;
        let mm = front[2] as u32 * 10 + front[3] as u32;
        let dd = front[4] as u32 * 10 + front[5] as u32;
        if !valid_ymd(y, mm, dd) { continue; }
        let all: Vec<u8> = front.iter().chain(back.iter()).copied().collect();
        let conf = if rrn_checksum_ok(&all) { 1.0 } else { 0.7 };
        out.push(Det { start: whole.start(), end: whole.end(), risk: R_CRIT, conf, label: "FRN" });
    }
    out
}

// ================= 카드/사업자/법인 =================
/// 카드(ko-pii card.py): BIN 첫자리 화이트리스트 + 길이-브랜드 일관성 + Luhn.
fn det_card(text: &str) -> Vec<Det> {
    let mut out = Vec::new();
    for (st, en) in find_bounded(&RE_CARD, text, |st, en| isolated_at(text, st, en, is_digit, is_digit)) {
        let d = digits(&text[st..en]);
        if !(13..=19).contains(&d.len()) { continue; }
        // BIN 첫자리 화이트리스트({2,3,4,5,6,9}) — 0/1/7/8 은 미할당.
        if !matches!(d[0], 2|3|4|5|6|9) { continue; }
        // 길이-브랜드 일관성: 13자리=Visa(4)만, 15자리=Amex(34/37)만.
        if d.len() == 13 && d[0] != 4 { continue; }
        if d.len() == 15 && !(d[0]==3 && (d[1]==4 || d[1]==7)) { continue; }
        if !luhn_ok(&d) { continue; }
        out.push(Det { start: st, end: en, risk: R_HIGH, conf: 1.0, label: "CARD" });
    }
    out
}
fn det_brn(text: &str) -> Vec<Det> {
    let mut out = Vec::new();
    for (st, en) in find_bounded(&RE_BRN, text, |st, en| isolated_at(text, st, en, is_digit, is_digit)) {
        let d = digits(&text[st..en]);
        // 모두 0 인 placeholder 는 체크섬을 통과하지만(합=0 → 검증숫자 0) 실제
        // 사업자가 아니다. 표의 빈칸을 0 으로 채운 문서에서 무더기로 잡히고,
        // 겹침 해소에서 전화번호 자리를 빼앗아 두 라벨의 집계까지 어긋난다
        // (실측: 주소록 한 건에서 사업자등록번호 1→13, 전화 647→636).
        if d.iter().all(|&x| x == 0) { continue; }
        if brn_checksum_ok(&d) {
            out.push(Det { start: st, end: en, risk: R_HIGH, conf: 1.0, label: "BUSINESS_REG" });
        }
    }
    out
}
fn det_corp(text: &str) -> Vec<Det> {
    let mut out = Vec::new();
    for (st, en) in find_bounded(&RE_CORP, text, |st, en| isolated_at(text, st, en, is_digit, is_digit)) {
        let raw = &text[st..en];
        let d = digits(raw);
        // ① 도서 바코드 제외 — 978/979 로 시작하는 '구분자 없는 13자리' 는
        //    GS1 Bookland(ISBN/ISSN) 바코드지 법인등록번호가 아니다. 법인번호는
        //    978/979 로 시작하지 않아 이걸 빼도 놓치는 게 없다.
        //    구분자가 있으면(978958-7081780) 바코드가 아니므로 그대로 본다.
        let no_sep = raw.len() == d.len();          // 하이픈·점·공백이 하나도 없다
        if no_sep && d.len() >= 3 && d[0] == 9 && d[1] == 7 && (d[2] == 8 || d[2] == 9) {
            continue;
        }
        if !corp_checksum_ok(&d) { continue; }
        // ② 주민·외국인등록번호가 우연히 법인 체크섬까지 통과하는 경우가 있다.
        //    그때는 RRN/FRN 검출기에 양보한다(원본과 같은 판단).
        if rrn_checksum_ok(&d) && corp_date_prefix_ok(&d) { continue; }
        out.push(Det { start: st, end: en, risk: R_MED, conf: 1.0, label: "CORP_REG" });
    }
    out
}

//------------------------------------------------------------------
// 앞 6자리가 '실제 있을 수 있는 생년월일'인가 (ko-pii _is_valid_date_prefix)
//=> 주민번호와 법인번호는 모양이 같아서, 체크섬만으로는 갈리지 않을 때가 있다.
//   앞 6자리가 날짜로 말이 되면 주민번호 쪽으로 보고 법인번호에서 뺀다.
//   2000년 기준으로 따지는 것은 윤년(2/29)을 살리기 위해서다(원본과 같다).
//
// -in: d = 숫자만 뽑은 13자리
//
// -out: true = 날짜로 말이 된다 / false = 아니다
// -out: error = 예외 없음
//------------------------------------------------------------------
fn corp_date_prefix_ok(d: &[u8]) -> bool {
    if d.len() < 6 { return false; }
    let mm = d[2] as u32 * 10 + d[3] as u32;
    let dd = d[4] as u32 * 10 + d[5] as u32;
    if !(1..=12).contains(&mm) || !(1..=31).contains(&dd) { return false; }
    valid_ymd(2000, mm, dd)
}

// ================= 전화 / 단순 정규식 / 여권 =================
/// 전화번호 검출(ko-pii phone.py 전면 대응).
///
/// 예전에는 정규식 하나로 뭉뚱그렸는데, 그러면 **국제표기(+82/0082)·070·대표번호
/// (15xx~18xx)·구분자가 여러 칸인 표기**를 통째로 놓쳤다(실측: 파이썬 5건 중 1건만
/// 검출). 전화번호를 못 잡으면 문서가 실제보다 낮은 등급을 받으므로, 덜 잡는 쪽이
/// 더 위험하다. 그래서 파이썬과 같은 패턴 7개를 같은 순서로 적용한다.
///
/// [순서가 중요한 이유] 국제표기가 먼저다. "+82-10-1234-5678" 은 안쪽에 국내표기
/// 모양("10-1234-5678")을 품고 있어서, 국내 패턴이 먼저 잡으면 `+82` 가 잘려 나간다.
/// 파이썬과 똑같이 이미 잡은 구간과 겹치면 건너뛴다.
fn det_phone(text: &str) -> Vec<Det> {
    let mut out: Vec<Det> = Vec::new();
    let mut seen: Vec<(usize, usize)> = Vec::new();

    // (정규식, 모바일인가, 앞에 '+' 도 금지인가)
    //   국제표기는 앞에 '+' 가 오는 게 정상이라 '+' 를 금지하지 않는다.
    //   국내표기는 '+' 가 앞에 오면 국제표기의 일부이므로 금지한다(= 국제 패턴에 양보).
    let steps: [(&Lazy<Regex>, bool, bool); 7] = [
        (&RE_PH_MOBILE_INTL, true,  false),
        (&RE_PH_SEOUL_INTL,  false, false),
        (&RE_PH_REGION_INTL, false, false),
        (&RE_PH_MOBILE,      true,  true),
        (&RE_PH_REGIONAL,    false, true),
        (&RE_PH_SEOUL,       false, true),
        (&RE_PH_REPRESENT,   false, true),
    ];

    for (re, mobile, no_plus) in steps {
        // 경계가 안 맞으면 그 자리만 버리고 다음 글자부터 다시 찾아야 파이썬과 같다
        // (find_bounded 헤더 참고 — 예전에는 통째로 건너뛰어 "2+82-10-…" 을 놓쳤다).
        let b = text.as_bytes();
        let spans = find_bounded(re, text, |st, en| {
            let before_ok = st == 0 || {
                let p = b[st - 1];
                !p.is_ascii_digit() && !(no_plus && p == b'+')
            };
            let after_ok = en >= b.len() || !b[en].is_ascii_digit();
            before_ok && after_ok
        });
        for (st, en) in spans {
            // 앞선 패턴이 이미 가져간 구간이면 양보한다(파이썬 _overlaps 와 동일).
            if seen.iter().any(|&(s, e)| st < e && s < en) { continue; }
            seen.push((st, en));
            // 휴대전화는 개인 직통(HIGH), 유선·VoIP·대표번호는 사업장 다수(MEDIUM).
            let risk = if mobile { R_HIGH } else { R_MED };
            out.push(Det { start: st, end: en, risk, conf: 1.0, label: "PHONE" });
        }
    }
    out
}
// (det_simple 제거) — '정규식으로 찾아 그대로 담기'만 하던 도우미였다. EMAIL·URL
// 둘뿐이던 사용처가 각자 경계 검사·꼬리 제거를 갖게 되면서 쓸 데가 없어졌다.
// 원본 ko-pii 에도 '그냥 정규식만' 인 검출기는 없다 — 전부 뒤처리가 붙는다.

//------------------------------------------------------------------
// 앞뒤 경계를 보면서 정규식 매치를 모은다 (look-around 흉내)
//=> Rust 정규식은 look-behind 가 없어 '앞 글자가 숫자면 매치 금지' 같은 조건을
//   코드로 봐야 한다. 그런데 매치를 만든 뒤 버리면 파이썬과 결과가 달라진다 —
//   파이썬은 그 자리에서 '매치를 아예 안 만들고' 다음 글자부터 다시 찾기 때문에,
//   같은 덩어리 안쪽에서 시작하는 더 짧은 매치를 찾아낸다.
//   실제로 "2+82-10-123-5678" 에서 파이썬은 +를 건너뛰고 "82-10-…" 을 잡는데,
//   버리고 끝내면 아무것도 못 잡아 전화번호를 통째로 놓쳤다.
//    1) pos 부터 매치를 찾는다
//    2) 경계가 맞으면 담고 매치 끝으로 건너뛴다
//    3) 안 맞으면 '매치 시작 다음 글자'부터 다시 찾는다(파이썬과 같은 재탐색)
//
//   [숫자덩어리 건너뛰기] 3) 을 한 글자씩만 하면 긴 숫자열에서 재탐색이 자릿수만큼
//   되풀이된다(실측: 숫자 4만개 줄에서 경계검사 없는 판보다 40배 느림 — 이 자리는
//   파이썬 look-behind 보다도 느려졌다). 그런데 '앞 글자가 숫자라서' 거부된 자리는,
//   그 숫자 덩어리 안쪽 어디서 시작해도 앞 글자가 여전히 숫자라 전부 거부된다.
//   그래서 한 글자씩 갈 것 없이 덩어리 끝으로 한 번에 건너뛴다 — 결과는 그대로고
//   재탐색 횟수만 준다.
//
//   ※ 이 지름길이 성립하려면 호출부의 ok 가 '앞 글자가 숫자면 반드시 거부' 여야
//     한다. 현재 호출부 12곳은 모두 그렇다(is_digit / is_digit_or_dot /
//     is_ascii_alphanumeric / is_local_ch / 16진수 edge / num_isolated — 전부
//     숫자를 금지 글자에 포함한다). 이 전제는 tests::앞글자가_숫자면_모든_호출부가_거부한다
//     가 지키고 있으니, 숫자를 허용하는 ok 를 새로 넣으려면 그 테스트부터 볼 것.
//
// -in: re   = 쓸 정규식
// -in: text = 훑을 본문
// -in: ok   = (시작, 끝) 을 받아 경계가 맞는지 답하는 판정 함수
//             (앞 글자가 숫자면 반드시 false 를 돌려줘야 한다 — 위 ※ 참고)
//
// -out: spans = 살아남은 (시작, 끝) 목록. 겹치지 않고 앞에서부터 차례로 담긴다
// -out: error = 예외 없음
//------------------------------------------------------------------
fn find_bounded(re: &Regex, text: &str, ok: impl Fn(usize, usize) -> bool) -> Vec<(usize, usize)> {
    // 다음 글자 경계 — 한글처럼 여러 바이트인 글자를 반으로 자르지 않게 한다.
    fn next_char(text: &str, i: usize) -> usize {
        let mut j = i + 1;
        while j < text.len() && !text.is_char_boundary(j) { j += 1; }
        j
    }
    let b = text.as_bytes();
    let mut out = Vec::new();
    let mut pos = 0usize;
    while pos <= text.len() {
        let m = match re.find_at(text, pos) { Some(m) => m, None => break };
        if ok(m.start(), m.end()) {
            out.push((m.start(), m.end()));
            // 빈 매치로 제자리걸음 하는 일이 없게 최소 한 글자는 전진한다.
            pos = if m.end() > m.start() { m.end() } else { next_char(text, m.start()) };
        } else {
            let one = next_char(text, m.start());
            // 앞 글자가 숫자라 거부된 자리면, 이어지는 숫자 덩어리는 통째로 헛걸음이다.
            pos = if m.start() > 0 && b[m.start() - 1].is_ascii_digit() {
                let mut j = m.start();
                while j < b.len() && b[j].is_ascii_digit() { j += 1; }
                // 매치가 숫자 아닌 글자에서 시작했다면 j 가 제자리라 전진이 없다.
                // 그때는 한 글자 전진으로 되돌려 무한루프를 막는다.
                one.max(j)
            } else {
                one
            };
        }
    }
    out
}

//------------------------------------------------------------------
// find_bounded 의 캡처 판 — 그룹이 필요한 검출기용
//=> RRN·FRN 은 앞 6자리와 뒤 7자리를 따로 봐야 해서 span 만으로는 모자라고
//   캡처가 필요하다. 그런데 캡처를 쓰려고 captures_iter 를 쓰면, 경계 검사로
//   버린 매치의 '끝'까지 반복자가 지나가 버려 그 안쪽에서 시작하는 매치를 잃는다.
//   실제로 주민번호 두 개가 구분자 없이 붙은 표에서 하나도 못 잡았다
//   (부록 E-① 과 같은 갈래 — find_bounded 로 고쳤던 그 문제가 여기만 남아 있었다).
//   그래서 captures_at 으로 '버리면 한 글자 뒤부터 다시' 를 캡처에도 똑같이 준다.
//    1) pos 부터 캡처를 찾는다
//    2) 경계가 맞으면 담고 매치 끝으로 건너뛴다
//    3) 안 맞으면 매치 시작 다음 글자부터 다시 찾는다
//       (앞 글자가 숫자라 버린 자리면 숫자 덩어리 끝까지 건너뛴다 — find_bounded 와 같은 지름길)
//
// -in: re   = 쓸 정규식
// -in: text = 훑을 본문
// -in: ok   = (시작, 끝) 을 받아 경계가 맞는지 답하는 판정 함수
//             (find_bounded 와 같은 전제 — 앞 글자가 숫자면 반드시 false)
//
// -out: caps = 살아남은 캡처 목록(앞에서부터 차례로, 겹치지 않게)
// -out: error = 예외 없음
//------------------------------------------------------------------
fn captures_bounded<'t>(re: &Regex, text: &'t str,
                        ok: impl Fn(usize, usize) -> bool) -> Vec<regex::Captures<'t>> {
    fn next_char(text: &str, i: usize) -> usize {
        let mut j = i + 1;
        while j < text.len() && !text.is_char_boundary(j) { j += 1; }
        j
    }
    let b = text.as_bytes();
    let mut out = Vec::new();
    let mut pos = 0usize;
    while pos <= text.len() {
        let c = match re.captures_at(text, pos) { Some(c) => c, None => break };
        let whole = c.get(0).unwrap();
        let (s, e) = (whole.start(), whole.end());
        if ok(s, e) {
            out.push(c);
            pos = if e > s { e } else { next_char(text, s) };
        } else {
            let one = next_char(text, s);
            pos = if s > 0 && b[s - 1].is_ascii_digit() {
                let mut j = s;
                while j < b.len() && b[j].is_ascii_digit() { j += 1; }
                one.max(j)
            } else {
                one
            };
        }
    }
    out
}

//------------------------------------------------------------------
// URL 검출 (ko-pii url.py 와 1:1)
//=> 정규식으로 잡은 뒤 '문장부호 꼬리'를 떼어낸다. 문서에서 URL 은 대개 문장
//   한가운데 들어가므로, 끝의 마침표·쉼표를 안 떼면 값이 오염되고 span 도 길어진다.
//   원본 detect() 의 `url.rstrip(".,;:!?")` 와 같은 일이다.
//
// -in: text = 정규화된 본문
//
// -out: dets = URL 검출 목록(위험도 INFO, 확신도 0.9). 꼬리를 뗀 만큼 end 가 줄어든다
// -out: error = 예외 없음(꼬리만 남는 경우는 정규식상 생기지 않는다)
//------------------------------------------------------------------
fn det_url(text: &str) -> Vec<Det> {
    let mut out = Vec::new();
    for m in RE_URL.find_iter(text) {
        // 끝에 붙은 문장부호를 뗀다 — 전부 ASCII 라 바이트 단위로 잘라도 안전하다.
        let trimmed = m.as_str().trim_end_matches(|c| matches!(c, '.' | ',' | ';' | ':' | '!' | '?'));
        // 스킴만 남을 만큼 깎이는 일은 없지만, 빈 값이 되면 검출로 치지 않는다.
        if trimmed.is_empty() { continue; }
        out.push(Det {
            start: m.start(),
            end: m.start() + trimmed.len(),
            risk: R_INFO,
            conf: 0.9,
            label: "URL",
        });
    }
    out
}

//------------------------------------------------------------------
// 이메일 검출 (ko-pii email.py 와 1:1)
//=> 정규식만으로는 원본과 어긋난다. 원본은 정규식 앞뒤에 look-around 를 걸고,
//   찾은 뒤에도 '모양은 맞지만 이메일일 리 없는 것'을 한 번 더 버린다.
//   Rust 정규식은 look-around 를 못 하므로 그 두 가지를 코드로 옮겼다.
//    1) 앞경계: 바로 앞 글자가 로컬부에 쓰이는 글자면 건너뛴다(토막 중간에서 시작 금지)
//    2) 뒤경계: 바로 뒤 글자가 영숫자면 건너뛴다
//    3) 사후 거르기: 로컬부가 점으로 시작·끝나거나, 로컬·도메인에 점이 연달으면 버린다
//
// -in: text = 정규화된 본문
//
// -out: dets = 살아남은 EMAIL 검출 목록(위험도 MEDIUM, 확신도 1.0)
// -out: error = 예외 없음
//------------------------------------------------------------------
fn det_email(text: &str) -> Vec<Det> {
    // 로컬부에 쓰이는 글자인가 — 앞경계 판정용((?<![A-Za-z0-9._%+\-]) 대응).
    let is_local_ch = |c: u8| c.is_ascii_alphanumeric()
        || c == b'.' || c == b'_' || c == b'%' || c == b'+' || c == b'-';

    let b = text.as_bytes();
    let mut out = Vec::new();
    // 경계가 안 맞으면 그 자리만 버리고 다음 글자부터 다시 찾는다(find_bounded 헤더 참고).
    let spans = find_bounded(&RE_EMAIL, text, |st, en| {
        // 1) 앞경계 — 이미 이메일 글자가 이어져 있으면 토막 중간이라 시작점이 아니다.
        let before_ok = st == 0 || !is_local_ch(b[st - 1]);
        // 2) 뒤경계 — 뒤에 영숫자가 더 있으면 값이 잘린 것이다.
        let after_ok = en >= b.len() || !b[en].is_ascii_alphanumeric();
        before_ok && after_ok
    });
    for (st, en) in spans {
        let value = &text[st..en];
        // 3) 사후 거르기 — 원본 detect() 의 두 검사와 같다.
        //    '@' 는 마지막 것을 기준으로 나눈다(로컬부에 @ 는 못 오지만 원본과 맞춘다).
        let (local, domain) = match value.rfind('@') {
            Some(i) => (&value[..i], &value[i + 1..]),
            None => continue,
        };
        if local.starts_with('.') || local.ends_with('.') { continue; }
        if local.contains("..") || domain.contains("..") { continue; }

        out.push(Det { start: st, end: en, risk: R_MED, conf: 1.0, label: "EMAIL" });
    }
    out
}

/// 여권(ko-pii passport.py): 앞뒤 영숫자 경계 + 8자리 all-zero 거부.
fn det_passport(text: &str) -> Vec<Det> {
    let b = text.as_bytes();
    let mut out = Vec::new();
    // (?<![A-Za-z0-9]) / (?![A-Za-z0-9]) — 경계가 안 맞으면 다음 글자부터 재탐색.
    for (st, en) in find_bounded(&RE_PASSPORT, text, |st, en| {
        let before_ok = st == 0 || !b[st - 1].is_ascii_alphanumeric();
        let after_ok = en >= b.len() || !b[en].is_ascii_alphanumeric();
        before_ok && after_ok
    }) {
        // 일련번호가 전부 0 인 자리표시는 실제 여권이 아니다(뒤 8자리).
        if text[st..en].ends_with("00000000") { continue; }
        out.push(Det { start: st, end: en, risk: R_CRIT, conf: 0.9, label: "PASSPORT" });
    }
    out
}

// 차량 용도 한글 화이트리스트(자동차관리법 시행규칙 별표7) — 약 50자.
const VEHICLE_HANGUL: &[&str] = &[
    "가","나","다","라","마","거","너","더","러","머","버","서","어","저",
    "고","노","도","로","모","보","소","오","조","구","누","두","루","무","부","수","우","주",
    "바","사","아","자","배","하","허","호","외","영","준","협","대","국","합","육","해","공",
];
// 차량번호 뒤 한국어 수량/통화 단위 → 차량 아님(ko-pii _FOLLOWING_UNIT_REJECT).
const VEHICLE_UNIT_REJECT: &[&str] = &[
    "원","달러","엔","위안","유로","파운드","프랑","억","만","천","백","조",
    "%","퍼센트","퍼센트포인트","포인트","포",
    "건","건수","명","년","월","일","시간","분","초","톤","kg","g","m","km","mm",
];
/// 차량 등록번호(ko-pii vehicle.py): 용도한글 화이트리스트 + 4자리(0000 제외) + 단위어 거부.
fn det_vehicle(text: &str) -> Vec<Det> {
    let mut out = Vec::new();
    for m in RE_VEHICLE.captures_iter(text) {
        let whole = m.get(0).unwrap();
        // (?<![0-9가-힣]) : 앞 글자가 숫자·한글이면 거부.
        if whole.start() > 0 {
            let c = text[..whole.start()].chars().next_back().unwrap();
            if c.is_ascii_digit() || is_hangul(c) { continue; }
        }
        // (?![0-9]) : 뒤 글자가 숫자면 거부(한글 조사는 허용).
        if digit_after(text, whole.end()) { continue; }
        let purpose = &m[2];
        if !VEHICLE_HANGUL.contains(&purpose) { continue; }
        if &m[3] == "0000" { continue; }
        // 뒤에 수량/통화 단위가 붙으면 차량 아님.
        let tail = right_window(text, whole.end(), 8);
        let tail = tail.trim_start();
        if VEHICLE_UNIT_REJECT.iter().any(|u| tail.starts_with(u)) { continue; }
        out.push(Det { start: whole.start(), end: whole.end(), risk: R_LOW, conf: 0.85, label: "VEHICLE" });
    }
    out
}

/// IPv4: 옥텟검증 + 예약대역 제외 + 버전/섹션 문맥 제외(ko-pii ip.py parity).
// IPv6(ko-pii ip.py _IPV6). look-around 은 코드로 본다(앞뒤가 16진수·콜론·점이면 토막 중간).
// 뒤에 붙는 %eth0 같은 zone id 까지 매치에 포함한다.
//
// [IPv4 포함형을 '필수' 대안으로 먼저 두는 이유]  2026-09-11
// 원본은 `::ffff:10.1.100.25` 를 통째로 잡는데 우리는 `10.1.100.25` 만 잡고 있었다.
// 원본 패턴의 IPv4 꼬리는 선택(`(?:...)?`)이지만, 끝에 붙은 `(?![0-9A-Fa-f:.])` 가
// **역추적을 강제**한다 — `::ffff:10` 까지만 물면 다음 글자가 `.` 이라 그 look-ahead 가
// 실패하고, 엔진이 되돌아가 `::ffff:10.1.100.25` 전체를 문다.
// Rust 의 regex 는 look-around 가 없어 그 경계 검사를 코드로 옮겼는데(find_bounded),
// 코드 검사는 **이미 정해진 매치를 버릴 뿐** 정규식을 되돌리지 못한다. 그래서
// `::ffff:10` 이 경계 검사에 걸려 통째로 버려지고, 뒤의 IPv4 만 남았다.
// 꼬리가 '필수'인 대안을 앞에 두면 정규식이 스스로 역추적해 같은 결과가 된다.
static RE_IPV6: Lazy<Regex> = Lazy::new(|| Regex::new(
    r"(?:[0-9A-Fa-f:]*::[0-9A-Fa-f:]*\d{1,3}(?:\.\d{1,3}){3}|[0-9A-Fa-f:]*::[0-9A-Fa-f:]*|(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4})(?:%[A-Za-z0-9]+)?"
).unwrap());

//------------------------------------------------------------------
// IPv6 주소 검출 (ko-pii ip.py 의 IPv6 분기)
//=> 예전에는 이 분기가 통째로 없어서 IPv6 를 하나도 못 잡았다(미탐).
//   정규식이 느슨해서 "주 내용 ::" 같은 한국어 강조 표기도 걸리므로,
//   원본처럼 세 가지를 걸러낸 뒤 실제 주소로 파싱되는 것만 남긴다.
//    1) 콜론이 없으면 버린다
//    2) "::" 하나만 남는 것은 버린다(문서의 강조 표기)
//    3) zone id(%eth0)를 떼고 진짜 IPv6 로 해석되는지 확인한다
//   ※ IPv4 와 달리 예약대역·문맥 게이팅은 원본에도 없다 — 그대로 맞춘다.
//
// -in: text = 검사할 본문
//
// -out: dets = IP 라벨 검출 목록(위험도 MEDIUM, 확신도 1.0)
// -out: error = 예외 없음
//------------------------------------------------------------------
fn det_ipv6(text: &str) -> Vec<Det> {
    // (?<![0-9A-Fa-f:.]) / (?![0-9A-Fa-f:.]) 대응.
    let edge = |c: u8| c.is_ascii_hexdigit() || c == b':' || c == b'.';
    let b = text.as_bytes();
    let mut out = Vec::new();
    for (st, en) in find_bounded(&RE_IPV6, text, |st, en| {
        let before_ok = st == 0 || !edge(b[st - 1]);
        let after_ok = en >= b.len() || !edge(b[en]);
        before_ok && after_ok
    }) {
        let addr = &text[st..en];
        if !addr.contains(':') { continue; }
        if addr.trim() == "::" || addr.trim().is_empty() { continue; }
        // zone id 는 파서가 못 받으므로 떼고 검사한다(원본과 같다).
        let raw = addr.split('%').next().unwrap_or(addr);
        if raw.parse::<std::net::Ipv6Addr>().is_err() { continue; }
        out.push(Det { start: st, end: en, risk: R_MED, conf: 1.0, label: "IP" });
    }
    out
}

fn det_ipv4(text: &str) -> Vec<Det> {
    let mut out = Vec::new();
    for (st, en) in find_bounded(&RE_IPV4, text, |st, en| isolated_at(text, st, en, is_digit_or_dot, is_digit_or_dot)) {
        let oct: Vec<u32> = text[st..en].split('.').filter_map(|o| o.parse().ok()).collect();
        if oct.len() != 4 || oct.iter().any(|&v| v > 255) { continue; }
        if is_reserved_ipv4(&oct) { continue; }
        let left = left_window(text, st, 12);
        if RE_IP_CTX_LEFT.is_match(&left) { continue; }
        let right = right_window(text, en, 8);
        if RE_IP_CTX_RIGHT.is_match(&right) { continue; }
        out.push(Det { start: st, end: en, risk: R_MED, conf: 1.0, label: "IP" });
    }
    out
}
fn is_reserved_ipv4(o: &[u32]) -> bool {
    let (a, b, c) = (o[0], o[1], o[2]);
    a == 127 || a == 0
        || (a == 169 && b == 254)
        || (a == 192 && b == 0 && c == 2)
        || (a == 198 && b == 51 && c == 100)
        || (a == 203 && b == 0 && c == 113)
        || a >= 224
}
static RE_IP_CTX_LEFT: Lazy<Regex> = Lazy::new(|| Regex::new(
    r"(?i)(?:버전|버젼|펌웨어|릴리[스즈]|빌드|패치|표|그림|별표|도표|붙임|항목|조항|조|항|단계|절|장|챕터|version|firmware|release|build|patch|section|chapter|figure|table|appendix|clause)\s*[.·:]?\s*$"
).unwrap());
static RE_IP_CTX_RIGHT: Lazy<Regex> = Lazy::new(|| Regex::new(
    r"(?i)^\s*(?:버전|버젼|version|빌드|build|릴리[스즈])"
).unwrap());

// ---- 문자 유틸 ----
fn is_hangul(c: char) -> bool { ('\u{AC00}'..='\u{D7A3}').contains(&c) }

/// start(byte) 앞 nchars 글자(순서 유지).
fn left_window(text: &str, start: usize, nchars: usize) -> String {
    let pre = &text[..start];
    let mut chars: Vec<char> = pre.chars().rev().take(nchars).collect();
    chars.reverse();
    chars.into_iter().collect()
}
/// end(byte) 뒤 nchars 글자.
fn right_window(text: &str, end: usize, nchars: usize) -> String {
    text[end..].chars().take(nchars).collect()
}

// ---- 국적/조사 사전 ----
// ko-pii context/particles.py 원본 순서 그대로(긴 조사 우선).
const PARTICLES: &[&str] = &[
    "에게서","한테서","께서",
    "에게","한테","에서","으로","보다",
    "이가","이는","이도","이를",
    "은","는","이","가","을","를","와","과","의",
    "에","도","만","야","라","여",
];
fn is_country(tok: &str) -> bool { districts::is_country(tok) }
/// 조사 최장일치 제거 → (남는 토큰, 제거된 조사).
fn strip_particle(tok: &str) -> (&str, Option<&'static str>) {
    for p in PARTICLES {
        if tok.len() > p.len() && tok.ends_with(p) {
            return (&tok[..tok.len() - p.len()], Some(p));
        }
    }
    (tok, None)
}

/// 국적/국가명(ko-pii nationality.py): 고립 한글토큰(2-8) → 조사제거 → '인'제거 → 국가사전.
fn det_nationality(text: &str) -> Vec<Det> {
    let mut out = Vec::new();
    let chars: Vec<(usize, char)> = text.char_indices().collect();
    let mut i = 0;
    while i < chars.len() {
        if !is_hangul(chars[i].1) { i += 1; continue; }
        let start = i;
        while i < chars.len() && is_hangul(chars[i].1) { i += 1; }
        let run_len = i - start;
        let before_ok = start == 0 || !chars[start - 1].1.is_ascii_alphanumeric();
        let after_ok = i >= chars.len() || !chars[i].1.is_ascii_alphanumeric();
        if !(2..=8).contains(&run_len) || !before_ok || !after_ok { continue; }
        let start_b = chars[start].0;
        let tok: String = chars[start..i].iter().map(|(_, c)| *c).collect();
        let (stem, particle) = strip_particle(&tok);
        let mut t = stem.to_string();
        if t.chars().count() < 2 { continue; }
        let mut people = false;
        if t.chars().count() >= 3 && t.ends_with('인') {
            let s = &t[..t.len() - '인'.len_utf8()];
            if is_country(s) { t = s.to_string(); people = true; }
        }
        if !is_country(&t) { continue; }
        // 실제 end: 원 토큰 끝에서 조사/‘인’ 제외.
        let raw_end = start_b + tok.len();
        let end_b = raw_end - particle.map(|p| p.len()).unwrap_or(0) - if people { '인'.len_utf8() } else { 0 };
        out.push(Det { start: start_b, end: end_b, risk: R_LOW, conf: 0.7, label: "NATIONALITY" });
    }
    out
}

// ---- 주소(ko-pii address.py 전면 포팅: 도로명·지번·대화체·단독행정구역 4브랜치) ----
static RE_ROAD: Lazy<Regex> = Lazy::new(|| Regex::new(
    r"(?:([가-힣]{1,8}(?:특별시|광역시|특별자치도|특별자치시|도))\s*)?((?:[가-힣]{1,8}(?:시|군|구)\s*){0,2})([가-힣A-Za-z0-9]{1,16}(?:대로|로|길))\s*([0-9]+(?:-[0-9]+)?)"
).unwrap());
static RE_JIBUN: Lazy<Regex> = Lazy::new(|| Regex::new(
    r"(?:([가-힣]{1,8}(?:특별시|광역시|특별자치도|특별자치시|도))\s*)?((?:[가-힣]{1,8}(?:시|군|구)\s*){0,2})([가-힣]{1,8}(?:동|읍|면|리))\s*([0-9]+(?:-[0-9]+)?)"
).unwrap());
static RE_LOOSE: Lazy<Regex> = Lazy::new(|| Regex::new(
    r"([가-힣]{2,}(?:시|군|구|동|읍|면|리))(?:\s+([가-힣]+(?:동|읍|면|리)))?(?:\s+([0-9]+(?:-[0-9]+)?))?"
).unwrap());
// 위 패턴을 '통째로' 맞춰 보는 판(끝을 줄여 가며 다시 유효한지 확인할 때 쓴다).
//=> 원본은 정규식 안에 (?![가-힣]) 가 있어, 뒤가 한글이면 엔진이 되돌아가 더 짧은
//   매치를 만든다("강남구 7삼동" → "강남구 7" 실패 → "강남구"). Rust 는 look-ahead
//   가 없어 밖에서 검사하는데, 그러면 통째로 버려 원본과 달라진다. 그래서 끝을
//   한 글자씩 줄이며 '이것도 온전한 매치인가'를 이 정규식으로 물어 되돌아가기를 흉내낸다.
static RE_LOOSE_FULL: Lazy<Regex> = Lazy::new(|| Regex::new(
    r"^([가-힣]{2,}(?:시|군|구|동|읍|면|리))(?:\s+([가-힣]+(?:동|읍|면|리)))?(?:\s+([0-9]+(?:-[0-9]+)?))?$"
).unwrap());
const LOOSE_ANCHORS: &[&str] = &[
    "주소","자택","거주","본적",
    "사세요","사신다","사셨","사신","사세",
    "사는","사니까","살던","살아","살고","산다","산대",
    "이사","이사하","이사했",
    "명함",
];
const LOOSE_BIG_CITIES: &[&str] = &["서울시","부산시","대구시","광주시","대전시","울산시","인천시","수원시","고양시","용인시"];
const ADMIN_PROVINCE_ALIASES: &[&str] = &["강원도","충청도","전라도","경상도","제주도","서울시","부산시","대구시","인천시","광주시","대전시","울산시"];

//------------------------------------------------------------------
// 글자 경계로 한 칸 이동 (여러 바이트 글자를 반으로 자르지 않게)
//=> 한글은 3바이트라 바이트 인덱스를 그냥 ±1 하면 문자열이 깨진다.
//
// -in: text = 대상 문자열
// -in: i    = 현재 바이트 위치
//
// -out: 다음(또는 이전) 글자 경계 바이트 위치
// -out: error = 예외 없음(범위를 벗어나지 않게 잘라 준다)
//------------------------------------------------------------------
fn next_char_at(text: &str, i: usize) -> usize {
    let mut j = i + 1;
    while j < text.len() && !text.is_char_boundary(j) { j += 1; }
    j.min(text.len() + 1)
}

fn prev_char_at(text: &str, i: usize) -> usize {
    if i == 0 { return 0; }
    let mut j = i - 1;
    while j > 0 && !text.is_char_boundary(j) { j -= 1; }
    j
}

/// 주소 검출: ko-pii address.py 4브랜치. 각 브랜치는 자체 seen(±slack)으로 내부 겹침 관리.
static RE_ADDR_DETAIL: Lazy<Regex> = Lazy::new(|| Regex::new(
    r"^\s*(?:\d+동\s*\d+호|\d+동|\d+호|\d+층)").unwrap());
static RE_ADDR_BLDG_BRIDGE: Lazy<Regex> = Lazy::new(|| Regex::new(
    r"^\s+[가-힣A-Za-z0-9]{2,}").unwrap());
static RE_ADDR_BRIDGE_NEXT: Lazy<Regex> = Lazy::new(|| Regex::new(
    r"^\s+\d+(?:동|호|층)").unwrap());
static RE_ADDR_BLDG_TAIL: Lazy<Regex> = Lazy::new(|| Regex::new(
    r"^\s+[가-힣A-Za-z0-9]*(?:빌딩|타워|센터|스퀘어|플라자|프라자|오피스텔|아파트|맨션|하이츠|캐슬|팰리스|레지던스|펜트하우스|자이|래미안|푸르지오|더샵|아이파크|힐스테이트|디에이치|e편한세상|위브|센트레빌|롯데캐슬|데시앙|스위첸|꿈에그린|베르디움|리슈빌|코아루|우미린|한라비발디|효성해링턴|어울림|하늘채|호반써밋|아크로|써밋|주공)").unwrap());
static RE_ADDR_PAREN: Lazy<Regex> = Lazy::new(|| Regex::new(
    r"^\s*\([가-힣A-Za-z0-9,·\s]+\)").unwrap());

//------------------------------------------------------------------
// 주소 뒤 상세(동·호·층·건물명·괄호) — ko-pii _extend_with_detail
//=> "테헤란로 152" 뒤에 "103동 1202호" 가 붙으면 그것까지가 한 주소다.
//   원본은 이 상세를 span 에 포함시킨다. 이게 빠지면 --with-pii 로 뽑은 값이
//   실제보다 짧게 잘리고, 겹침 해소에 넘기는 구간도 달라진다.
//    1) 동호수·층을 붙일 수 있으면 붙인다(반복 — "401호 12층" 처럼 이어질 수 있다)
//    2) 못 붙이면 건물명을 시도한다(뒤에 동/호/층이 오는 '다리' 형태 또는 접미사로 끝나는 형태)
//    3) 마지막으로 괄호 상세 "(신정동,롯데캐슬)" 한 번
//   ※ 원본의 (?=\s+\d+(?:동|호|층)) 는 look-ahead 라 Rust 정규식으로 못 쓴다.
//     그래서 '건물명 후보'를 먼저 재고, 그 뒤가 동/호/층인지 따로 확인한다.
//
// -in: text = 본문
// -in: end  = 주소 매치가 끝난 바이트 위치
//
// -out: 확장된 끝 위치(붙일 게 없으면 end 그대로)
// -out: error = 예외 없음
//------------------------------------------------------------------
fn extend_with_detail(text: &str, end: usize) -> usize {
    let mut pos = end;
    loop {
        // 1) 동호수·층
        if let Some(m) = RE_ADDR_DETAIL.find(&text[pos..]) {
            if m.start() == 0 { pos += m.end(); continue; }
        }
        // 2) 건물명 — 다리형(뒤에 동/호/층이 이어짐)
        let mut advanced = false;
        if let Some(m) = RE_ADDR_BLDG_BRIDGE.find(&text[pos..]) {
            if m.start() == 0 {
                let cand = pos + m.end();
                if RE_ADDR_BRIDGE_NEXT.is_match(&text[cand..]) { pos = cand; advanced = true; }
            }
        }
        // 2') 건물명 — 접미사로 끝나는 형태
        if !advanced {
            if let Some(m) = RE_ADDR_BLDG_TAIL.find(&text[pos..]) {
                if m.start() == 0 { pos += m.end(); advanced = true; }
            }
        }
        if !advanced { break; }
    }
    // 3) 괄호 상세 한 번
    if let Some(m) = RE_ADDR_PAREN.find(&text[pos..]) {
        if m.start() == 0 { pos += m.end(); }
    }
    pos
}

fn det_address(text: &str) -> Vec<Det> {
    let mut seen: Vec<(usize, usize)> = vec![];
    let mut out: Vec<Det> = vec![];
    // 1) 도로명 — MEDIUM 0.8
    for m in RE_ROAD.captures_iter(text) {
        let mm = m.get(0).unwrap();
        if !addr_struct_ok(&m) { continue; }
        if !addr_tail_ok(text, mm.end()) { continue; }
        if addr_anchor(text, &m, mm.start()).is_none() { continue; }
        // 동·호·층·건물명·괄호 상세까지가 한 주소다(원본 _extend_with_detail).
        let end = extend_with_detail(text, mm.end());
        seen.push((mm.start(), end));
        // [중요] ko-pii 에 없는 추가 검증 — 함수 헤더 참고. 버려도 seen 에는 남긴다:
        // 파이썬 판은 ko-pii 가 낸 결과를 뒤에서 거르므로, 그 자리를 지번 갈래가 다시 잡지 않는다.
        if !road_admin_ok(&m) { continue; }
        out.push(Det { start: mm.start(), end, risk: R_MED, conf: 0.8, label: "ADDRESS" });
    }
    // 2) 지번 — MEDIUM 0.75
    for m in RE_JIBUN.captures_iter(text) {
        let mm = m.get(0).unwrap();
        if overlaps(&seen, mm.start(), mm.end()) { continue; }
        if !addr_struct_ok(&m) { continue; }
        if !jibun_admin_ok(&m) { continue; }   // [중요] ko-pii 에 없는 추가 검증 — 함수 헤더 참고
        if !addr_tail_ok(text, mm.end()) { continue; }
        if addr_anchor(text, &m, mm.start()).is_none() { continue; }
        let end = extend_with_detail(text, mm.end());
        seen.push((mm.start(), end));
        out.push(Det { start: mm.start(), end, risk: R_MED, conf: 0.75, label: "ADDRESS" });
    }
    // 3) 대화체 단독 — MEDIUM 0.6
    //    뒤가 한글이면 끝을 줄여 가며 다시 맞춰 본다(RE_LOOSE_FULL 헤더 참고).
    let mut pos = 0usize;
    while pos <= text.len() {
        let m = match RE_LOOSE.captures_at(text, pos) { Some(m) => m, None => break };
        let mm = m.get(0).unwrap();
        let (st, full_end) = (mm.start(), mm.end());
        // 앞경계((?<![가-힣]))가 안 맞으면 이 자리만 버리고 다음 글자부터 다시 찾는다.
        let before_ok = st == 0 || !is_hangul(text[..st].chars().next_back().unwrap());
        if !before_ok { pos = next_char_at(text, st); continue; }

        // 뒤가 한글이 아닐 때까지 끝을 줄이되, 줄인 것도 온전한 매치여야 한다.
        let mut en = full_end;
        let chosen = loop {
            let after_ok = en >= text.len() || !is_hangul(text[en..].chars().next().unwrap());
            if after_ok && RE_LOOSE_FULL.is_match(&text[st..en]) { break Some(en); }
            if en <= st { break None; }
            en = prev_char_at(text, en);
        };
        let en = match chosen { Some(e) => e, None => { pos = next_char_at(text, st); continue; } };

        // 여기서부터는 원본 순서 그대로.
        if overlaps_chars(text, &seen, st, en, 30) { pos = en.max(next_char_at(text, st)); continue; }
        let first = m.get(1).unwrap().as_str();
        let ok = LOOSE_BIG_CITIES.contains(&first) || districts::is_district(first);
        if !ok { pos = next_char_at(text, st); continue; }
        if loose_anchor(text, st, en).is_none() { pos = next_char_at(text, st); continue; }
        seen.push((st, en));
        out.push(Det { start: st, end: en, risk: R_MED, conf: 0.6, label: "ADDRESS" });
        pos = en.max(next_char_at(text, st));
    }
    // 4) 단독 행정구역 토큰 — LOW 0.7 (국가명 제외)
    for (s, e, tok) in admin_tokens(text) {
        if overlaps_chars(text, &seen, s, e, 50) { continue; }
        if is_country(&tok) { continue; }
        if tok.chars().count() >= 3 && tok.ends_with('인') {
            let stem = &tok[..tok.len() - '인'.len_utf8()];
            if is_country(stem) { continue; }
        }
        let is_admin = districts::is_province(&tok)
            || ADMIN_PROVINCE_ALIASES.contains(&tok.as_str())
            || districts::is_district(&tok)
            || districts::is_extra_city(&tok)
            || districts::is_common_dong(&tok)
            || districts::is_legal_dong(&tok);
        if !is_admin { continue; }
        if loose_anchor(text, s, e).is_none() { continue; }
        seen.push((s, e));
        out.push(Det { start: s, end: e, risk: R_LOW, conf: 0.7, label: "ADDRESS" });
    }
    out
}

/// 도로명/지번 구조 검증: 광역 prefix 실존 + (광역+기초) 조합 유효.
fn addr_struct_ok(cap: &regex::Captures) -> bool {
    let city = cap.get(1).map(|m| m.as_str()).unwrap_or("");
    if !city.is_empty() && !districts::is_province(city) { return false; }
    let districts_str = cap.get(2).map(|m| m.as_str().trim()).unwrap_or("");
    let first_district = districts_str.split_whitespace().next().unwrap_or("");
    if !city.is_empty() && !first_district.is_empty()
        && !districts::is_valid_province_district(city, first_district) {
        return false;
    }
    true
}
//------------------------------------------------------------------
// [중요] ko-pii 와 의도적으로 다르게 만든 곳 — 지번 주소의 '가짜 시군구' 거부
//=> ko-pii 는 지번 주소에서 **시·도(광역)가 있을 때만** 행정구역을 검증한다.
//   광역이 없으면 `_has_anchor` 가 "district 가 비어 있지 않다"는 이유만으로
//   무조건 통과시킨다(ko_pii/patterns/address.py):
//
//       def _has_anchor(text, start, city, district):
//           if city or district:
//               return "prefix"        # ← 여기서 검증 없이 통과
//
//   그래서 '구'로 끝나는 아무 낱말 + '동/읍/면/리'로 끝나는 아무 낱말 + 숫자면
//   주소가 된다. 실측에서 이렇게 잡혔다(ko-pii 1.15.2 실제 출력):
//
//       "연구 정보관리 1"
//         → ADDRESS, conf 0.75, evidence=['pattern:address_jibun','anchor:prefix']
//           extra={'city': None, 'districts': '연구', 'dong': '정보관리', 'lot_number': '1'}
//
//   '연구'가 區, '정보관리'가 里 로 읽힌 것이다. 인사평가 엑셀 5개에서 10건이
//   이 모양으로 잡혀 문서 등급이 S 로 올라갔다(2026-09-02 D:\분류함 실측).
//
//   [왜 우리가 고치나] 1.15.2 가 PyPI 최신이고, 업스트림 main 브랜치의
//   address.py 도 설치본과 **한 줄도 다르지 않다**(347줄 동일, 2026-09-02 확인).
//   즉 버전을 올려 해결될 문제가 아니다.
//
//   [무엇을 다르게 하나] 광역이 없을 때, 시군구 토큰과 동/읍/면/리 토큰 중
//   **최소 하나는 진짜 행정구역 이름**이어야 한다고 요구한다.
//   둘 다 사전에 없으면 주소로 보지 않는다.
//
//   [왜 '둘 중 하나'인가] '시군구가 사전에 있어야 한다'로 하면, 사전에 빠진
//   기초자치단체가 있을 때 진짜 주소를 놓친다(ALL_DISTRICTS 는 약 200개라
//   전수가 아니다). 법정동 가제티어는 10,368개로 훨씬 촘촘하므로, 둘 중 하나만
//   맞아도 통과시키면 오탐을 걷어내면서 미탐을 최소화할 수 있다.
//   [측정] 그래도 0 은 아니다 — 전수 스윕(1,780건, 2026-09-03)에서 두 조건이 함께
//   실패한 실주소가 나왔다('원미구 중동 1153'). 일반구가 ko-pii 사전에 없고
//   '중동'이 법정동 가제티어에도 없어서다. 그래서 EXTRA_DISTRICTS 를 덧댔다.
//     · "연구 정보관리 1"      → 연구✗ + 정보관리✗ → 거부 (오탐 제거)
//     · "성남시 정자동 100"     → 성남시✓            → 통과
//     · "○○시 매곡리 100"      → ○○시✗ + 매곡리✓   → 통과 (사전에 없는 시라도 살아남음)
//     · "원미구 중동 1153"    → 원미구✓(보충 사전) → 통과 (실측 미탐을 막은 자리)
//
//   [적용 범위] 지번 브랜치에만 건다. 도로명 브랜치는 '로/길 + 번호'라는 신호가
//   훨씬 강해 같은 오탐이 관측되지 않았고, 괜히 건드리면 미탐 위험만 커진다.
//
//   [시험] 이 차이는 pii_corpus.yaml(= ko-pii 가 정답인 대조표)에 넣으면 안 된다.
//   넣으면 골든 테스트가 영원히 빨간불이 된다. 대신 손으로 쓴 단위테스트
//   tests::주소_가짜시군구는_거른다 가 이 동작을 지킨다.
//   문서: doc/ko-pii 차이점.html
//
// -in: cap = RE_JIBUN 캡처 (1=시도, 2=시군구, 3=동읍면리, 4=번지)
//
// -out: true = 주소로 인정 / false = 가짜 행정구역이라 거부
// -out: error = 예외 없음
//------------------------------------------------------------------
/// ko-pii 의 ALL_DISTRICTS(206개)에는 광역시의 '자치구'만 있고, 광역시가 아닌
/// 시(市) 아래의 '일반구(행정구)'가 빠져 있다. 그래서 부천시 원미구처럼 실존하는
/// 주소가 시군구 검증을 통과하지 못한다.
///   [실측] 전수 스윕 1,780건에서 '원미구 중동 1153' 2건이 이렇게 걸러졌다
///   (2026-09-03). 등급에는 영향이 없었지만 미탐은 미탐이다.
/// 법정동 가제티어가 그 구멍을 메워 줄 것으로 봤는데 '중동'이 그 사전에도 없어 두
/// 조건이 함께 실패했다. 목록이 33개로 작고 잘 바뀌지 않아 여기 적어 둔다. 전부
/// 고유한 지명이라 '연구' 같은 평범한 낱말을 되살리지 않는다.
/// Python 판 rules.py 의 _EXTRA_DISTRICTS 와 **같은 목록이어야 한다**.
const EXTRA_DISTRICTS: &[&str] = &[
    "장안구", "권선구", "팔달구", "영통구",                    // 수원시
    "수정구", "중원구", "분당구",                              // 성남시
    "만안구", "동안구",                                        // 안양시
    "원미구", "소사구", "오정구",                              // 부천시
    "상록구", "단원구",                                        // 안산시
    "덕양구", "일산동구", "일산서구",                          // 고양시
    "처인구", "기흥구", "수지구",                              // 용인시
    "상당구", "서원구", "흥덕구", "청원구",                    // 청주시
    "동남구", "서북구",                                        // 천안시
    "완산구", "덕진구",                                        // 전주시
    "의창구", "성산구", "마산합포구", "마산회원구", "진해구",  // 창원시
];

fn jibun_admin_ok(cap: &regex::Captures) -> bool {
    let city = cap.get(1).map(|m| m.as_str()).unwrap_or("");
    // 광역이 있으면 addr_struct_ok 가 이미 (광역)·(광역+기초) 조합을 검증했다.
    if !city.is_empty() { return true; }

    let districts_str = cap.get(2).map(|m| m.as_str().trim()).unwrap_or("");
    let first_district = districts_str.split_whitespace().next().unwrap_or("");
    // 시군구 토큰 자체가 없으면 ko-pii 도 '주소' 낱말을 요구한다(addr_anchor) — 여기선 통과.
    if first_district.is_empty() { return true; }

    let dong = cap.get(3).map(|m| m.as_str()).unwrap_or("");
    // 둘 중 하나라도 진짜 행정구역 이름이면 주소로 본다.
    // 시군구는 ko-pii 사전 + 보충 사전(일반구)을 함께 본다.
    districts::is_district(first_district)
        || EXTRA_DISTRICTS.contains(&first_district)
        || districts::is_legal_dong(dong)
}

//------------------------------------------------------------------
// 도로명 주소 — 낱말 속 가짜 시군구 거르기 [ko-pii 와 의도적으로 다름]
//=> ko-pii 도로명 갈래는 시군구와 도로명 사이에 띄어쓰기를 요구하지 않아, 낱말 하나가
//   쪼개져 주소가 된다("실시간으로 3D" → 실시(市) + 간으로(路) + 3). 2026-10-26 배포
//   교차 검증에서 관측. 파이썬 rules._road_admin_ok 와 같은 판정이다.
//    1) 광역(시·도)이 있으면 통과 — addr_struct_ok 가 이미 조합을 검증했다
//    2) 마지막 시군구 토큰과 도로명이 띄어 쓰여 있으면 통과
//    3) 붙어 있으면, 그 토큰이 진짜 행정구역 이름일 때만 통과(ko-pii 사전 + 보충 사전)
//
// -in: cap = RE_ROAD 매치(1 광역 · 2 시군구 · 3 도로명)
//
// -out: bool = 주소로 인정하면 true
// -out: error = 없음
//------------------------------------------------------------------
fn road_admin_ok(cap: &regex::Captures) -> bool {
    if cap.get(1).map_or(false, |m| !m.as_str().is_empty()) { return true; }
    let raw = cap.get(2).map(|m| m.as_str()).unwrap_or("");
    let last = match raw.split_whitespace().last() { Some(t) => t, None => return true };
    // 시군구 칸 끝에 공백이 있으면 도로명과 띄어 쓴 것이다 — 건드리지 않는다
    if raw.ends_with(char::is_whitespace) { return true; }
    districts::is_district(last) || EXTRA_DISTRICTS.contains(&last)
}

/// 매치 끝 뒤에 숫자/하이픈이 이어지면 거부(ko-pii `(?![0-9-])`).
fn addr_tail_ok(text: &str, end: usize) -> bool {
    match text.as_bytes().get(end) {
        Some(&b) => !(b.is_ascii_digit() || b == b'-'),
        None => true,
    }
}
/// 앵커(ko-pii _has_anchor): 광역/시군구 prefix 또는 좌측20자 '주소'.
fn addr_anchor(text: &str, cap: &regex::Captures, start: usize) -> Option<&'static str> {
    let has_city = cap.get(1).map(|m| !m.as_str().is_empty()).unwrap_or(false);
    let has_dist = cap.get(2).map(|m| !m.as_str().trim().is_empty()).unwrap_or(false);
    if has_city || has_dist { return Some("prefix"); }
    if left_window(text, start, 20).contains("주소") { return Some("keyword"); }
    None
}
/// 대화체 anchor(ko-pii _has_loose_anchor): 앞25/뒤12자에 강한 keyword.
fn loose_anchor(text: &str, start: usize, end: usize) -> Option<&'static str> {
    let head = left_window(text, start, 25);
    let tail = right_window(text, end, 12);
    for kw in LOOSE_ANCHORS {
        if head.contains(kw) || tail.contains(kw) { return Some(kw); }
    }
    None
}
/// 단독 행정구역 토큰: 최대 한글런(2~6, 영숫자 인접 배제) → 조사 제거한 stem.
fn admin_tokens(text: &str) -> Vec<(usize, usize, String)> {
    let mut out = vec![];
    let chars: Vec<(usize, char)> = text.char_indices().collect();
    let mut i = 0;
    while i < chars.len() {
        if !is_hangul(chars[i].1) { i += 1; continue; }
        let start = i;
        while i < chars.len() && is_hangul(chars[i].1) { i += 1; }
        let run_len = i - start;
        let before_ok = start == 0 || !chars[start - 1].1.is_ascii_alphanumeric();
        let after_ok = i >= chars.len() || !chars[i].1.is_ascii_alphanumeric();
        if !(2..=6).contains(&run_len) || !before_ok || !after_ok { continue; }
        let raw: String = chars[start..i].iter().map(|(_, c)| *c).collect();
        let (stem, particle) = strip_particle(&raw);
        if stem.chars().count() < 2 { continue; }
        let start_b = chars[start].0;
        let end_b = start_b + raw.len() - particle.map(|p| p.len()).unwrap_or(0);
        out.push((start_b, end_b, stem.to_string()));
    }
    out
}
// (hangul_boundary 제거) — 매치를 만든 '뒤에' 앞뒤 한글을 검사하던 도우미다.
// 그렇게 하면 원본과 달라진다: 원본의 (?![가-힣]) 는 정규식 안에 있어, 실패하면
// 엔진이 되돌아가 더 짧은 매치를 만든다("강남구 7삼동" → "강남구"). 밖에서 검사해
// 통째로 버리면 그 짧은 매치를 잃는다. 지금은 loose 분기가 끝을 줄여 가며 다시
// 맞춰 보는 방식(RE_LOOSE_FULL)으로 되돌아가기를 흉내 낸다.

/// span 목록 중 [s,e) 와 겹치는 게 있는지(여유 없음 — 바이트 비교로 충분).
fn overlaps(seen: &[(usize, usize)], s: usize, e: usize) -> bool {
    seen.iter().any(|&(a, b)| s < b && a < e)
}

//------------------------------------------------------------------
// 앞뒤로 '글자 수'만큼 넓혀 겹치는지 본다
//=> ko-pii 의 인접 판정(ADJACENCY 30 · ADMIN_ALONE_ADJACENCY 50)은 파이썬
//   문자열이라 단위가 '글자'다. Rust 는 바이트 위치를 쓰므로 그 숫자를 그대로
//   더하면 한글에서 창이 1/3 로 좁아진다(한글 한 글자 = 3바이트).
//   그러면 같은 주소의 다른 조각을 '멀다'고 보고 또 잡아 과검출이 난다.
//   실제로 3만 건 퍼징에서 ADDRESS 과검출 100건이 전부 이 단위 착오였다.
//
// -in: text  = 기준 문자열(글자 경계를 세는 데 쓴다)
// -in: seen  = 이미 잡은 구간들(바이트)
// -in: s, e  = 검사할 구간(바이트)
// -in: slack = 앞뒤로 넓힐 '글자' 수
//
// -out: true = 넓힌 구간과 겹친다(=같은 주소의 일부로 본다) / false = 멀다
// -out: error = 예외 없음
//------------------------------------------------------------------
fn overlaps_chars(text: &str, seen: &[(usize, usize)], s: usize, e: usize, slack: usize) -> bool {
    seen.iter().any(|&(a, b)| {
        // 이미 잡은 구간을 앞뒤로 slack '글자' 만큼 넓힌다.
        let mut lo = a;
        for _ in 0..slack { if lo == 0 { break; } lo = prev_char_at(text, lo); }
        let mut hi = b;
        for _ in 0..slack { if hi >= text.len() { break; } hi = next_char_at(text, hi); }
        s < hi && lo < e
    })
}

/// 계좌: 세 앵커 중 하나 + 10~16자리. HIGH 0.9. 동일 숫자 span 중복 제거.
fn det_account(text: &str) -> Vec<Det> {
    // 원본 _normalize_and_check: 공백·하이픈만 걷어내고 '나머지가 전부 숫자' 여야 한다.
    //=> 예전에는 숫자만 골라 세서(비숫자를 그냥 버려서) "3-3허01-123c56X7" 같은
    //   글자 섞인 토막도 계좌로 잡았다. 계좌는 span 이 길어서, 잘못 잡히면 겹침
    //   해소에서 사업자번호·건강보험 같은 진짜 PII 를 밀어내 오히려 못 잡게 만든다.
    let clean_digits = |raw: &str| -> Option<usize> {
        let mut n = 0usize;
        for c in raw.chars() {
            if c.is_whitespace() || c == '-' { continue; }
            if !c.is_ascii_digit() { return None; }   // 숫자가 아닌 게 섞이면 계좌가 아니다
            n += 1;
        }
        Some(n)
    };

    let mut spans: Vec<(usize, usize)> = vec![];
    let collect = |re: &Regex, spans: &mut Vec<(usize, usize)>, check_before: bool| {
        for cap in re.captures_iter(text) {
            let g = cap.get(1).unwrap();
            if check_before && g.start() > 0 && text.as_bytes()[g.start() - 1].is_ascii_digit() {
                continue;
            }
            // 정규식 꼬리의 `[\s\-]*` 가 뒤따르는 공백·줄바꿈까지 삼킨다. 그대로 두면
            // 검출값에 "\r\n" 이 붙어 나오고(--with-pii), span 이 실제보다 길어져
            // 겹침 해소에서 다른 라벨을 부당하게 밀어낸다. 파이썬 판이 group(1) 을
            // rstrip 해 span 을 줄이는 것과 똑같이 맞춘다.
            let raw = g.as_str().trim_end();
            let end = g.start() + raw.len();
            let n = match clean_digits(raw) { Some(n) => n, None => continue };
            if !(10..=16).contains(&n) { continue; }
            // 앞 분기가 이미 가져간 자리와 겹치면 건너뛴다(원본 2·3 분기의 seen 검사).
            if spans.iter().any(|&(a, b)| a < end && g.start() < b) { continue; }
            spans.push((g.start(), end));
        }
    };
    collect(&RE_ACCT_KW, &mut spans, false);
    collect(&RE_ACCT_BANK_BEFORE, &mut spans, false);
    collect(&RE_ACCT_BANK_AFTER, &mut spans, true);
    spans.sort();
    spans.dedup();
    spans.into_iter().map(|(s, e)| Det { start: s, end: e, risk: R_HIGH, conf: 0.9, label: "ACCOUNT" }).collect()
}

// ================= 공통 문맥/경계 헬퍼 (신규 검출기 6종 공용) =================
fn kw_before(text: &str, start: usize, window: usize, kws: &[&str]) -> bool {
    let head = left_window(text, start, window);
    kws.iter().any(|kw| head.contains(kw))
}
fn digit_before(text: &str, pos: usize) -> bool {
    pos > 0 && text.as_bytes()[pos - 1].is_ascii_digit()
}
fn digit_after(text: &str, pos: usize) -> bool {
    text.as_bytes().get(pos).map_or(false, |b| b.is_ascii_digit())
}
fn num_isolated(text: &str, s: usize, e: usize) -> bool {
    !digit_before(text, s) && !digit_after(text, e)
}
fn num2(d: &[u8], i: usize) -> u32 { d[i] as u32 * 10 + d[i+1] as u32 }

// ================= 운전면허번호 (ko-pii driver_license.py) =================
static RE_DL_HYPHEN: Lazy<Regex> = Lazy::new(|| Regex::new(r"([0-9]{2})-([0-9]{2})-([0-9]{6})-([0-9]{2})").unwrap());
static RE_DL_PLAIN: Lazy<Regex> = Lazy::new(|| Regex::new(r"[0-9]{12}").unwrap());
const DL_KEYWORDS: &[&str] = &["운전면허", "면허번호", "면허증"];
fn dl_region_ok(r: &str) -> bool { r.parse::<u32>().map_or(false, |v| (11..=28).contains(&v)) }
fn det_driver_license(text: &str) -> Vec<Det> {
    let mut out = Vec::new();
    for m in RE_DL_HYPHEN.captures_iter(text) {
        let whole = m.get(0).unwrap();
        if digit_before(text, whole.start()) || digit_after(text, whole.end()) { continue; }
        if !dl_region_ok(&m[1]) { continue; }
        out.push(Det { start: whole.start(), end: whole.end(), risk: R_CRIT, conf: 0.85, label: "DRIVER_LICENSE" });
    }
    for (st, en) in find_bounded(&RE_DL_PLAIN, text, |st, en| num_isolated(text, st, en)) {
        if !dl_region_ok(&&text[st..en][..2]) { continue; }
        if !kw_before(text, st, 15, DL_KEYWORDS) { continue; }
        out.push(Det { start: st, end: en, risk: R_CRIT, conf: 0.85, label: "DRIVER_LICENSE" });
    }
    out
}

// ================= 건강보험증 번호 (ko-pii medical_insurance.py) =================
static RE_MI_NUM: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?:[0-9]-[0-9]{10})|(?:[0-9]{11})").unwrap());
static RE_MI_KW: Lazy<Regex> = Lazy::new(|| Regex::new(r"건강\s*보험|의료\s*보험|보험증").unwrap());
fn det_medical_insurance(text: &str) -> Vec<Det> {
    let mut out = Vec::new();
    for m in RE_MI_NUM.find_iter(text) {
        let bad_before = m.start() > 0 && {
            let b = text.as_bytes()[m.start()-1]; b.is_ascii_digit() || b == b'-'
        };
        let bad_after = text.as_bytes().get(m.end()).map_or(false, |&b| b.is_ascii_digit() || b == b'-');
        if bad_before || bad_after { continue; }
        if !RE_MI_KW.is_match(&left_window(text, m.start(), 25)) { continue; }
        out.push(Det { start: m.start(), end: m.end(), risk: R_HIGH, conf: 0.9, label: "MEDICAL_INSURANCE" });
    }
    out
}

// ================= 처방전 발행번호 (ko-pii prescription.py) =================
static RE_PRESC_ISSUE: Lazy<Regex> = Lazy::new(|| Regex::new(r"\d{12}").unwrap());
static RE_PRESC_LABELED: Lazy<Regex> = Lazy::new(|| Regex::new(r"[A-Za-z]{2,6}-[0-9][0-9A-Za-z-]{3,22}").unwrap());
static RE_PRESC_INST: Lazy<Regex> = Lazy::new(|| Regex::new(r"\d{8}").unwrap());
const PRESC_KEYWORDS: &[&str] = &["처방번호","처방전번호","처방전 번호","처방전 발행번호","처방전교부번호","교부번호","Rx 번호","Rx번호","Rx"];
const PRESC_INST_KEYWORDS: &[&str] = &["의료기관기호","기관기호","요양기관기호","요양기관번호","병원코드"];
fn det_prescription(text: &str) -> Vec<Det> {
    let mut out: Vec<Det> = Vec::new();
    let mut seen: Vec<(usize, usize)> = vec![];
    // (1) 발행번호 12자리
    for (st, en) in find_bounded(&RE_PRESC_ISSUE, text, |st, en| num_isolated(text, st, en)) {
        let d = digits(&text[st..en]);
        let y = d[0] as i64*1000 + d[1] as i64*100 + d[2] as i64*10 + d[3] as i64;
        if y < 1990 || y > 2099 || !valid_ymd(y, num2(&d,4), num2(&d,6)) { continue; }
        if !kw_before(text, st, 20, PRESC_KEYWORDS) { continue; }
        seen.push((st, en));
        out.push(Det { start: st, end: en, risk: R_HIGH, conf: 0.9, label: "PRESCRIPTION_ID" });
    }
    // (2) 영문접두 ID
    for m in RE_PRESC_LABELED.find_iter(text) {
        let before_bad = m.start() > 0 && text.as_bytes()[m.start()-1].is_ascii_alphanumeric();
        let after_bad = text.as_bytes().get(m.end()).map_or(false, |b| b.is_ascii_alphanumeric());
        if before_bad || after_bad { continue; }
        if overlaps(&seen, m.start(), m.end()) { continue; }
        if !kw_before(text, m.start(), 18, PRESC_KEYWORDS) { continue; }
        seen.push((m.start(), m.end()));
        out.push(Det { start: m.start(), end: m.end(), risk: R_HIGH, conf: 0.88, label: "PRESCRIPTION_ID" });
    }
    // (3) 의료기관기호 8자리
    for (st, en) in find_bounded(&RE_PRESC_INST, text, |st, en| num_isolated(text, st, en)) {
        if overlaps(&seen, st, en) { continue; }
        if !kw_before(text, st, 15, PRESC_INST_KEYWORDS) { continue; }
        seen.push((st, en));
        out.push(Det { start: st, end: en, risk: R_MED, conf: 0.85, label: "PRESCRIPTION_ID" });
    }
    out
}

// ================= EDI 약품코드 (ko-pii edi_drug.py) =================
static RE_EDI_13: Lazy<Regex> = Lazy::new(|| Regex::new(r"\d{13}").unwrap());
static RE_EDI_9: Lazy<Regex> = Lazy::new(|| Regex::new(r"\d{9}").unwrap());
const EDI_KEYWORDS: &[&str] = &["EDI","edi","약품코드","의약품코드","주성분코드","KD코드","KD 코드","약가코드"];
fn det_edi_drug(text: &str) -> Vec<Det> {
    let mut out: Vec<Det> = Vec::new();
    let mut seen: Vec<(usize, usize)> = vec![];
    for (st, en) in find_bounded(&RE_EDI_13, text, |st, en| num_isolated(text, st, en)) {
        if !kw_before(text, st, 18, EDI_KEYWORDS) { continue; }
        let s = &text[st..en];
        if !(s.starts_with("880") || s.starts_with("881") || s.starts_with("888")) { continue; }
        seen.push((st, en));
        out.push(Det { start: st, end: en, risk: R_LOW, conf: 0.9, label: "EDI_DRUG" });
    }
    for (st, en) in find_bounded(&RE_EDI_9, text, |st, en| num_isolated(text, st, en)) {
        if overlaps(&seen, st, en) { continue; }
        if !kw_before(text, st, 18, EDI_KEYWORDS) { continue; }
        seen.push((st, en));
        out.push(Det { start: st, end: en, risk: R_LOW, conf: 0.85, label: "EDI_DRUG" });
    }
    out
}

// ================= 법원 사건번호 (ko-pii court_case.py) =================
const COURT_CODES: &[&str] = &[
    "가합","가단","가소","카합","카단","카기","고합","고단","고정","형보","고약","고전",
    "구합","구단","구약","드합","드단","수단","수합","후단","후합",
    "헌가","헌나","헌다","헌라","헌마","헌바",
    "나","다","차","머","노","도","초","누","두","르","므","호","자","보","사","허","라","마","바",
];
static RE_COURT: Lazy<Regex> = Lazy::new(|| {
    let mut codes: Vec<&str> = COURT_CODES.to_vec();
    codes.sort_by(|a, b| b.chars().count().cmp(&a.chars().count()));
    let alts = codes.iter().map(|c| regex::escape(c)).collect::<Vec<_>>().join("|");
    Regex::new(&format!(r"((?:19|20)[0-9]{{2}})({})([0-9]{{1,6}})", alts)).unwrap()
});
fn det_court_case(text: &str) -> Vec<Det> {
    let mut out = Vec::new();
    for m in RE_COURT.captures_iter(text) {
        let whole = m.get(0).unwrap();
        if !digit_hangul_boundary(text, whole.start(), whole.end()) { continue; }
        let serial: u64 = m[3].parse().unwrap_or(0);
        if serial == 0 { continue; }
        out.push(Det { start: whole.start(), end: whole.end(), risk: R_MED, conf: 0.9, label: "COURT_CASE" });
    }
    out
}
/// [s,e) 앞뒤 글자가 숫자도 한글도 아님. ko-pii (?<![0-9가-힣])/(?![0-9가-힣]).
fn digit_hangul_boundary(text: &str, s: usize, e: usize) -> bool {
    let before_ok = s == 0 || {
        let c = text[..s].chars().next_back().unwrap();
        !c.is_ascii_digit() && !is_hangul(c)
    };
    let after_ok = e >= text.len() || {
        let c = text[e..].chars().next().unwrap();
        !c.is_ascii_digit() && !is_hangul(c)
    };
    before_ok && after_ok
}

// ================= 필지고유번호 PNU (ko-pii pnu.py) =================
static RE_PNU: Lazy<Regex> = Lazy::new(|| Regex::new(r"\d{19}").unwrap());
const PNU_SIDO: &[&str] = &["11","21","22","23","24","25","26","29","31","32","33","34","35","36","37","38","39","41","42","43","44","45","46","47","48","50"];
fn det_pnu(text: &str) -> Vec<Det> {
    let mut out = Vec::new();
    for (st, en) in find_bounded(&RE_PNU, text, |st, en| num_isolated(text, st, en)) {
        let s = &text[st..en];
        if !PNU_SIDO.contains(&&s[0..2]) { continue; }
        if &s[10..11] == "0" { continue; }
        if &s[11..15] == "0000" { continue; }
        out.push(Det { start: st, end: en, risk: R_LOW, conf: 0.9, label: "PNU" });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 편의 함수 — 한 라벨만 켜고 검출 건수를 센다.
    fn count(text: &str, label: &str) -> u32 {
        let want: HashSet<String> = [label.to_string()].into_iter().collect();
        *pii_counts(text, &want).get(label).unwrap_or(&0)
    }

    //--------------------------------------------------------------
    // find_bounded 의 '숫자덩어리 건너뛰기' 전제를 지키는 테스트
    //=> 지름길은 "앞 글자가 숫자면 ok 가 반드시 거부한다"는 전제 위에 서 있다.
    //   전제가 깨지면 조용히 미탐이 나므로(테스트가 아니라 운영에서 드러난다),
    //   호출부 12곳이 쓰는 판정식을 여기 모아 두고 숫자 앞에서 전부 false 인지 본다.
    //   새 검출기를 붙일 때 여기에도 그 판정식을 넣어야 한다.
    //
    // -in: 없음
    //
    // -out: 없음
    // -out: error = 숫자 앞인데 통과시키는 판정식이 있으면 assert 실패
    //--------------------------------------------------------------
    #[test]
    fn 앞글자가_숫자면_모든_호출부가_거부한다() {
        // 호출부에서 쓰는 '앞 글자 금지' 판정식들. (이름, 판정식)
        let is_local_ch = |c: u8| c.is_ascii_alphanumeric()
            || c == b'.' || c == b'_' || c == b'%' || c == b'+' || c == b'-';
        let ipv6_edge = |c: u8| c.is_ascii_hexdigit() || c == b':' || c == b'.';
        let befores: [(&str, &dyn Fn(u8) -> bool); 5] = [
            ("is_digit (card/brn/corp/phone)", &|c: u8| is_digit(c)),
            ("is_digit_or_dot (ipv4)",         &|c: u8| is_digit_or_dot(c)),
            ("is_ascii_alphanumeric (여권)",   &|c: u8| c.is_ascii_alphanumeric()),
            ("is_local_ch (email)",            &is_local_ch),
            ("hexdigit edge (ipv6)",           &ipv6_edge),
        ];
        for (name, bad) in befores {
            for c in b'0'..=b'9' {
                assert!(bad(c), "{} 가 숫자 '{}' 를 금지 글자로 안 본다 \
                    → find_bounded 의 숫자덩어리 건너뛰기가 미탐을 만든다", name, c as char);
            }
        }
        // num_isolated(dl/처방전/EDI/PNU)는 위치로 판정하므로 직접 확인한다.
        assert!(!num_isolated("12345", 2, 4), "num_isolated 가 숫자 앞을 통과시킨다");
    }

    //--------------------------------------------------------------
    // 숫자덩어리 건너뛰기 전/후 결과가 같은가 (등가성)
    //=> 지름길이 '빠르기만 하고 결과는 그대로'인지 확인한다. 한 글자씩 전진하던
    //   예전 판을 참조 구현으로 두고, 숫자가 빽빽한 입력에서 두 판의 결과가
    //   한 칸도 다르지 않은지 실제 정규식들로 대조한다.
    //    1) 숫자·구분자·한글을 섞어 만든 입력을 여러 벌 만든다
    //    2) 실제 검출기가 쓰는 정규식과 판정식으로 두 판을 돌린다
    //    3) span 목록이 완전히 같아야 한다
    //
    // -in: 없음
    //
    // -out: 없음
    // -out: error = 한 칸이라도 다르면 assert 실패
    //--------------------------------------------------------------
    #[test]
    fn 숫자덩어리_건너뛰기가_한글자씩과_같다() {
        // 예전 판 — 거부하면 무조건 한 글자만 전진한다.
        fn find_bounded_ref(re: &Regex, text: &str,
                            ok: impl Fn(usize, usize) -> bool) -> Vec<(usize, usize)> {
            fn next_char(text: &str, i: usize) -> usize {
                let mut j = i + 1;
                while j < text.len() && !text.is_char_boundary(j) { j += 1; }
                j
            }
            let mut out = Vec::new();
            let mut pos = 0usize;
            while pos <= text.len() {
                let m = match re.find_at(text, pos) { Some(m) => m, None => break };
                if ok(m.start(), m.end()) {
                    out.push((m.start(), m.end()));
                    pos = if m.end() > m.start() { m.end() } else { next_char(text, m.start()) };
                } else {
                    pos = next_char(text, m.start());
                }
            }
            out
        }

        // 재현 가능한 난수(외부 crate 없이) — xorshift.
        let mut seed = 0x5EED_1234u64;
        let mut rnd = move || {
            seed ^= seed << 13; seed ^= seed >> 7; seed ^= seed << 17; seed
        };
        // 숫자가 이어지는 구간을 일부러 길게 만들어 지름길이 실제로 발동하게 한다.
        let alphabet = ["0","1","2","4","5","7","9","-",".", " ", "가", "@", ":", "e", "\n"];
        let mut samples: Vec<String> = Vec::new();
        for _ in 0..40 {
            let mut s = String::new();
            for _ in 0..600 {
                let r = rnd() as usize;
                // 70% 는 숫자를 뽑아 긴 숫자열이 자주 생기게 한다.
                if r % 10 < 7 { s.push_str(alphabet[r % 7]); }
                else { s.push_str(alphabet[7 + r % 8]); }
            }
            samples.push(s);
        }
        // 실제 문서에서 문제가 됐던 모양도 함께 본다.
        samples.push("7".repeat(3000));
        samples.push(format!("{}{}", "9".repeat(500), "가나다".repeat(100)));
        samples.push("카드번호 4556-3795-0918-5094 로 결제. 2+82-10-123-5678".to_string());

        for s in &samples {
            let text: &str = s;
            let b = text.as_bytes();
            let is_local_ch = |c: u8| c.is_ascii_alphanumeric()
                || c == b'.' || c == b'_' || c == b'%' || c == b'+' || c == b'-';

            // (이름, 정규식, 판정식) — 호출부가 쓰는 조합을 그대로 옮겨 온다.
            macro_rules! cmp {
                ($name:expr, $re:expr, $ok:expr) => {{
                    let fast = find_bounded($re, text, $ok);
                    let slow = find_bounded_ref($re, text, $ok);
                    assert_eq!(fast, slow, "{} 에서 지름길 결과가 다르다\n본문: {:?}", $name, text);
                }};
            }
            cmp!("CARD",     &RE_CARD, |st, en| isolated_at(text, st, en, is_digit, is_digit));
            cmp!("BRN",      &RE_BRN,  |st, en| isolated_at(text, st, en, is_digit, is_digit));
            cmp!("CORP",     &RE_CORP, |st, en| isolated_at(text, st, en, is_digit, is_digit));
            cmp!("IPV4",     &RE_IPV4, |st, en| isolated_at(text, st, en, is_digit_or_dot, is_digit_or_dot));
            cmp!("PNU",      &RE_PNU,  |st, en| num_isolated(text, st, en));
            cmp!("DL_PLAIN", &RE_DL_PLAIN, |st, en| num_isolated(text, st, en));
            cmp!("EMAIL",    &RE_EMAIL, |st: usize, en: usize| {
                (st == 0 || !is_local_ch(b[st - 1]))
                    && (en >= b.len() || !b[en].is_ascii_alphanumeric())
            });
            cmp!("PASSPORT", &RE_PASSPORT, |st: usize, en: usize| {
                (st == 0 || !b[st - 1].is_ascii_alphanumeric())
                    && (en >= b.len() || !b[en].is_ascii_alphanumeric())
            });
            cmp!("PH_MOBILE", &RE_PH_MOBILE, |st: usize, en: usize| {
                let before_ok = st == 0 || { let p = b[st - 1]; !p.is_ascii_digit() && p != b'+' };
                let after_ok = en >= b.len() || !b[en].is_ascii_digit();
                before_ok && after_ok
            });
            cmp!("PH_MOBILE_INTL", &RE_PH_MOBILE_INTL, |st: usize, en: usize| {
                let before_ok = st == 0 || !b[st - 1].is_ascii_digit();
                let after_ok = en >= b.len() || !b[en].is_ascii_digit();
                before_ok && after_ok
            });
        }
    }

    //--------------------------------------------------------------
    // [중요] ko-pii 와 의도적으로 다른 동작을 지키는 시험
    //=> ko-pii 는 광역(시·도)이 없으면 시군구를 검증하지 않아, '구'로 끝나는
    //   아무 낱말이면 주소가 된다("연구 정보관리 1" → ADDRESS). 우리는 거부한다.
    //   이 사례는 pii_corpus.yaml 에 넣으면 안 된다 — 거기는 ko-pii 가 정답인
    //   대조표라 넣는 순간 골든 테스트가 영원히 실패한다. 그래서 여기 손으로 둔다.
    //   구현 근거는 jibun_admin_ok 헤더, 문서는 doc/ko-pii 차이점.html.
    //
    // -in: 없음
    //
    // -out: 없음
    // -out: error = 동작이 바뀌면 assert 실패
    //--------------------------------------------------------------
    #[test]
    fn 주소_가짜시군구는_거른다() {
        // '연구'(구로 끝남) + '정보관리'(리로 끝남) — 둘 다 실제 행정구역이 아니다.
        assert_eq!(count("연구 정보관리 1", "ADDRESS"), 0);
        assert_eq!(count("연구 정보관리 3 항목", "ADDRESS"), 0);

        // 진짜 주소는 그대로 잡아야 한다(미탐을 만들지 않았는지 확인).
        assert_eq!(count("주소는 서울특별시 강남구 역삼동 737 입니다.", "ADDRESS"), 1);
        assert_eq!(count("주소는 충청남도 아산시 탕정면 매곡리 100 입니다.", "ADDRESS"), 1);
        // 광역이 없어도 시군구가 실존하면 통과.
        assert_eq!(count("성남시 정자동 100", "ADDRESS"), 1);
        // 시군구가 사전에 없어도 법정동이 실존하면 통과(미탐 방지 장치).
        assert_eq!(count("없는시 매곡리 100", "ADDRESS"), 1);
        // 일반구(행정구)는 ko-pii 사전에 없다 — 보충 사전이 그 구멍을 메운다.
        // 전수 스윕에서 실제로 걸러졌던 미탐이라, 여기서 고정해 둔다.
        assert_eq!(count("주소는 경기도 부천시 원미구 중동 1153 입니다.", "ADDRESS"), 1);
        assert_eq!(count("원미구 중동 1153", "ADDRESS"), 1);
        assert_eq!(count("분당구 정자동 100", "ADDRESS"), 1);
    }

    //--------------------------------------------------------------
    // [중요] ko-pii 와 의도적으로 다른 동작 — 도로명 낱말 쪼개기 거르기
    //=> ko-pii 는 "실시간으로 3D" 를 실시(市) + 간으로(路) + 3 으로 읽어 주소로 낸다.
    //   위 지번 시험과 같은 이유로 pii_corpus.yaml 이 아니라 여기 손으로 둔다.
    //   파이썬 판에도 같은 벡터가 있다(tests/test_rules.py::test_road_glued_fake_district_rejected).
    //   구현 근거는 road_admin_ok 헤더.
    //
    // -in: 없음
    //
    // -out: 없음
    // -out: error = 동작이 바뀌면 assert 실패
    //--------------------------------------------------------------
    #[test]
    fn 주소_도로명_낱말쪼개기는_거른다() {
        assert_eq!(count("표정 변화가 실시간으로 3D 인물 표정", "ADDRESS"), 0);
        // 진짜 도로명은 그대로 — 띄어 쓴 것 · 붙였어도 실제 시군구인 것 · 광역이 있는 것
        assert_eq!(count("주소: 강남구 테헤란로 152", "ADDRESS"), 1);
        assert_eq!(count("강남구테헤란로 152 에서", "ADDRESS"), 1);
        assert_eq!(count("서울특별시 강남구 테헤란로 152", "ADDRESS"), 1);
        assert_eq!(count("성남시 분당구 판교역로 235", "ADDRESS"), 1);
        assert_eq!(count("원미구 길주로 1 번지", "ADDRESS"), 1);
    }

    //--------------------------------------------------------------
    // [중요] ko-pii 와 의도적으로 다른 동작을 지키는 시험 (차이 B)
    //=> ko-pii 는 한글 문맥("버전 1.2.3.4")만 걸러내고 영문 문서에서는 판번호를
    //   IP 로 잡는다. 986개 문서 실측에서 이 차이가 19건이었고 전부 판번호였다.
    //   위 주소 시험과 같은 이유로 pii_corpus.yaml 이 아니라 여기 손으로 둔다.
    //   Python 판에도 같은 벡터가 있다
    //   (tests/test_rules.py::test_ip_version_context_rejected) — 한쪽만 바뀌면
    //   다른 쪽이 빨간불이 되어 두 판이 갈린 것을 곧바로 알 수 있다.
    //   구현 근거는 RE_IP_CTX_LEFT / RE_IP_CTX_RIGHT, 문서는 doc/ko-pii 차이점.html.
    //
    // -in: 없음
    //
    // -out: 없음
    // -out: error = 동작이 바뀌면 assert 실패
    //--------------------------------------------------------------
    #[test]
    fn ip_판번호_문맥은_거른다() {
        // 왼쪽 문맥 — 영문(ko-pii 가 놓치던 자리)과 한글(ko-pii 도 거르는 자리) 모두.
        assert_eq!(count("Release 1.2 / 1.2.3.4 Build 7", "IP"), 0);
        assert_eq!(count("Section 10.20.30.40", "IP"), 0);
        assert_eq!(count("appendix: 10.20.30.40", "IP"), 0);
        assert_eq!(count("펌웨어 10.20.30.40", "IP"), 0);
        // 오른쪽 문맥 — 매치 뒤 8자에 build/버전이 붙는 모양.
        assert_eq!(count("1.2.3.4 build 7", "IP"), 0);

        // 진짜 IP 는 그대로 잡아야 한다(미탐을 만들지 않았는지 확인).
        assert_eq!(count("서버 8.8.8.8 접속", "IP"), 1);
        assert_eq!(count("접속 IP 는 10.1.100.25 입니다", "IP"), 1);
    }


    //--------------------------------------------------------------
    // IPv4 를 품은 IPv6 표기를 통째로 잡는다
    //=> `::ffff:10.1.100.25` 를 파이썬 판(ko-pii)은 통째로 잡는데 우리는
    //   뒤쪽 `10.1.100.25` 만 잡고 있었다(2026-09-11 실측).
    //
    //   원본 패턴의 IPv4 꼬리는 선택이지만 끝의 `(?![0-9A-Fa-f:.])` 가 역추적을
    //   강제한다. 우리는 look-around 가 없어 그 경계 검사를 코드로 옮겼는데,
    //   코드 검사는 이미 정해진 매치를 버릴 뿐 정규식을 되돌리지 못한다 —
    //   `::ffff:10` 이 통째로 버려지고 뒤의 IPv4 만 남았다.
    //
    //   값이 갈리면 마스킹하는 쪽이 `::ffff:` 를 남긴 채 지우게 된다.
    //   두 판의 결과를 나란히 견줄 수도 없다.
    //
    // -out: 없음
    // -out: error = 동작이 바뀌면 assert 실패
    //--------------------------------------------------------------
    #[test]
    fn ipv6에_박힌_ipv4를_통째로_잡는다() {
        // 값까지 본다 — 건수만 보면 뒤쪽 IPv4 만 잡아도 1건이라 통과해 버린다.
        let one = |t: &str| -> String {
            let mut w = std::collections::HashSet::new();
            w.insert("IP".to_string());
            let v = super::pii_records(t, &w);
            assert_eq!(v.len(), 1, "검출이 1건이 아니다: {:?}", v);
            v[0].1.clone()
        };
        assert_eq!(one("서버 주소는 ::ffff:10.1.100.25 이고"), "::ffff:10.1.100.25");
        assert_eq!(one("게이트웨이 ::10.1.2.3 입니다"), "::10.1.2.3");
        assert_eq!(one("외부 ::ffff:1.2.3.4 입니다"), "::ffff:1.2.3.4");
        // 원래 되던 것들이 그대로인지 — 이 고침이 다른 표기를 깨지 않았는가.
        assert_eq!(one("전체 2001:0db8:85a3:0000:0000:8a2e:0370:7334 요"),
                   "2001:0db8:85a3:0000:0000:8a2e:0370:7334");
        assert_eq!(one("루프백 ::1 요"), "::1");
        assert_eq!(one("존 fe80::1%eth0 요"), "fe80::1%eth0");
        // 한국어 강조 표기 "::" 는 주소가 아니다.
        let mut w = std::collections::HashSet::new();
        w.insert("IP".to_string());
        assert_eq!(super::pii_records("주 내용 :: 참고", &w).len(), 0);
    }

    // 체크섬이 맞는 실제 번호는 잡아야 한다.
    #[test]
    fn 유효한_사업자등록번호는_검출한다() {
        // 국세청 규칙을 통과하는 값(하이픈 유무 모두).
        assert_eq!(count("사업자등록번호 220-81-62517 입니다.", "BUSINESS_REG"), 1);
        assert_eq!(count("2208162517", "BUSINESS_REG"), 1);
    }

    // 체크섬이 틀리면 걸러야 한다(3-2-5 모양만 맞는 숫자).
    #[test]
    fn 체크섬_틀리면_거른다() {
        assert_eq!(count("220-81-62518", "BUSINESS_REG"), 0);
    }

    // 회귀 핵심 — 모두 0 인 placeholder 는 체크섬을 통과하지만 실제 사업자가 아니다.
    // 표의 빈칸을 0 으로 채운 문서에서 무더기로 오검출되던 것을 막는다.
    #[test]
    fn 전부0인_placeholder는_거른다() {
        assert_eq!(count("0000000000", "BUSINESS_REG"), 0);
        assert_eq!(count("000-00-00000", "BUSINESS_REG"), 0);
        // 0 이 섞여 있어도 전부 0 이 아니면 체크섬 규칙대로 판단한다.
        assert_eq!(count("000-00-00001", "BUSINESS_REG"), 0);   // 체크섬 불일치
    }

    // ── 전화번호(ko-pii phone.py 7패턴 대응) ─────────────────────
    // 예전에는 정규식 하나뿐이라 아래 넷을 통째로 놓쳤다. 전화번호를 못 잡으면
    // 문서가 실제보다 낮은 등급을 받으므로 '덜 잡는' 쪽이 더 위험하다.
    #[test]
    fn 국제표기_전화번호를_잡는다() {
        assert_eq!(count("연락 +82-10-1234-5678", "PHONE"), 1);
        assert_eq!(count("연락 0082 10 9876 5432", "PHONE"), 1);
        assert_eq!(count("서울 +82-2-123-4567", "PHONE"), 1);
        assert_eq!(count("지역 +82-31-123-4567", "PHONE"), 1);
    }

    #[test]
    fn voip_070과_대표번호를_잡는다() {
        assert_eq!(count("070-1234-5678", "PHONE"), 1);
        assert_eq!(count("대표 1588-1234", "PHONE"), 1);
        assert_eq!(count("대표 1666-9999", "PHONE"), 1);
    }

    // 구분자가 여러 칸이어도 한 건으로 본다(파이썬 [-.\s]{0,3} 과 동일).
    #[test]
    fn 구분자_여러칸도_한건이다() {
        assert_eq!(count("010 - 1234 - 5678", "PHONE"), 1);
        assert_eq!(count("010.1234.5678", "PHONE"), 1);
    }

    // 국제표기가 안쪽 국내표기보다 먼저다 — 순서가 뒤집히면 "+82" 가 잘려 나간다.
    #[test]
    fn 국제표기가_국내표기보다_우선이다() {
        let want: HashSet<String> = ["PHONE".to_string()].into_iter().collect();
        let recs = pii_records("+82-10-1234-5678", &want);
        assert_eq!(recs.len(), 1);
        assert!(recs[0].1.starts_with("+82"), "값={:?}", recs[0].1);
    }

    // ── 법인등록번호 구분자(ko-pii corp_reg.py 와 동일 폭) ────────
    // 점·슬래시로 쓴 표기를 놓치면 그 문서가 낮은 등급을 받는다.
    #[test]
    fn 법인등록번호는_점_슬래시_구분자도_받는다() {
        assert_eq!(count("110111-0000002", "CORP_REG"), 1);
        assert_eq!(count("110111.0000002", "CORP_REG"), 1);
        assert_eq!(count("110111/0000002", "CORP_REG"), 1);
        assert_eq!(count("110111 0000002", "CORP_REG"), 1);
    }

    // ── 계좌번호 span 꼬리 ────────────────────────────────────────
    // 정규식 꼬리가 줄바꿈까지 삼키면 검출값이 오염되고, span 이 길어져
    // 겹침 해소에서 다른 라벨을 부당하게 밀어낸다.
    #[test]
    fn 계좌번호_span은_공백을_물지_않는다() {
        let want: HashSet<String> = ["ACCOUNT".to_string()].into_iter().collect();
        let recs = pii_records("계좌번호 110-234-567890

다음 줄", &want);
        assert_eq!(recs.len(), 1);
        assert_eq!(recs[0].1, "110-234-567890");
    }

    // placeholder 를 거르면 그 자리를 전화번호가 되찾는다(겹침 해소 확인).
    #[test]
    fn placeholder자리는_다른_라벨이_가져간다() {
        let want: HashSet<String> = ["BUSINESS_REG".to_string(), "PHONE".to_string()]
            .into_iter().collect();
        let got = pii_counts("연락처 010-0000-0000", &want);
        assert_eq!(*got.get("BUSINESS_REG").unwrap_or(&0), 0);
    }
}

#[cfg(test)]
mod golden {
    use super::*;

    //--------------------------------------------------------------
    // 정답표를 읽어 (라벨집합, 사례목록) 으로 푼다
    //=> tests/pii_golden.json 은 scripts/gen_pii_golden.py 가 진짜 ko-pii 로
    //   뽑아 굳혀 둔 답이다. 파일이 없거나 깨졌으면 "왜 없는지"보다 "무엇을
    //   하라"가 중요하므로, 생성 명령을 함께 알려주고 멈춘다.
    //
    // -in: 없음
    //
    // -out: (labels, cases) = (검증 대상 라벨집합, 정답표 사례 JSON 배열)
    // -out: error = 파일이 없거나 JSON 이 깨졌으면 panic(테스트 실패)
    //--------------------------------------------------------------
    fn load() -> (HashSet<String>, Vec<serde_json::Value>) {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        let path = root.join("tests").join("pii_golden.json");
        let txt = std::fs::read_to_string(&path).unwrap_or_else(|e| {
            panic!("정답표를 못 읽었다 {:?}: {}\n\
                    → python scripts/gen_pii_golden.py 로 만들어라.", path, e)
        });
        let doc: serde_json::Value = serde_json::from_str(&txt)
            .unwrap_or_else(|e| panic!("정답표 JSON 이 깨졌다 {:?}: {}", path, e));

        let labels: HashSet<String> = doc["labels"].as_array()
            .expect("정답표에 labels 가 없다")
            .iter().filter_map(|v| v.as_str()).map(|s| s.to_string()).collect();
        let cases = doc["cases"].as_array()
            .expect("정답표에 cases 가 없다").clone();
        (labels, cases)
    }

    //--------------------------------------------------------------
    // JSON 오브젝트를 {라벨: 건수} 맵으로 바꾼다
    //=> 정답표의 counts 는 0건 라벨을 아예 담지 않는다. Rust pii_counts() 도
    //   마찬가지라, 두 맵을 통째로 비교하면 '덜 잡음'과 '더 잡음'이 한꺼번에
    //   드러난다(한쪽만 보면 오탐을 놓친다).
    //
    // -in: v = 정답표 사례의 "counts" 값
    //
    // -out: map = {라벨: 건수}. 값이 없거나 오브젝트가 아니면 빈 맵
    // -out: error = 예외 없음
    //--------------------------------------------------------------
    fn counts_of(v: &serde_json::Value) -> HashMap<String, u32> {
        let mut m = HashMap::new();
        if let Some(obj) = v.as_object() {
            for (k, n) in obj {
                if let Some(n) = n.as_u64() {
                    m.insert(k.clone(), n as u32);
                }
            }
        }
        m
    }

    //--------------------------------------------------------------
    // --version 이 찍는 '포팅 기준 버전'이 정답표와 같은가
    //=> KOPII_PORTED_FROM 은 사람이 손으로 적는 값이라 낡기 쉽다. 정답표를
    //   새 ko-pii 로 다시 뽑았는데 이 상수를 안 고치면, --version 이 거짓말을
    //   하게 되고 두 exe 의 기준을 맞춰 보려던 사람이 속는다. 그래서 묶어 둔다.
    //
    // -in: 없음
    //
    // -out: 없음
    // -out: error = 상수와 정답표의 kopii_version 이 다르면 assert 실패
    //--------------------------------------------------------------
    #[test]
    fn 포팅기준_버전이_정답표와_같다() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        let path = root.join("tests").join("pii_golden.json");
        let txt = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("정답표를 못 읽었다 {:?}: {}", path, e));
        let doc: serde_json::Value = serde_json::from_str(&txt).unwrap();
        let want = doc["kopii_version"].as_str().unwrap_or("(없음)");
        assert_eq!(KOPII_PORTED_FROM, want,
                   "--version 이 찍을 포팅 기준({})과 정답표를 만든 ko-pii({})가 다르다\n\
                    → src/pii.rs 의 KOPII_PORTED_FROM 을 정답표에 맞춰 고쳐라.",
                   KOPII_PORTED_FROM, want);
    }

    //--------------------------------------------------------------
    // 라벨별 건수가 ko-pii 정답표와 같은가(핵심 골든 테스트)
    //=> Rust pii.rs 는 ko-pii 를 '쓰는' 게 아니라 '옮겨 적은' 것이라, ko-pii 를
    //   올리면 소리 없이 어긋날 수 있다. 이 테스트가 그 어긋남을 잡는 그물이다.
    //   등급 parity(두 exe 결과 비교)로는 부족하다 — 등급은 건수를 임계값으로
    //   뭉갠 결과라, 라벨 하나를 통째로 놓쳐도 등급이 같으면 통과해 버린다.
    //    1) 정답표의 20라벨만 켜고 사례마다 pii_counts 를 돌린다
    //    2) 라벨별 건수 맵을 통째로 비교한다(미탐·오탐 양쪽 다 잡힌다)
    //    3) 어긋난 사례는 앞 몇 건을 사람이 읽을 수 있게 찍는다
    //
    // -in: 없음
    //
    // -out: 없음
    // -out: error = 한 칸이라도 다르면 assert 실패
    //--------------------------------------------------------------
    #[test]
    fn 정답표와_라벨별_건수가_같다() {
        let (labels, cases) = load();
        assert!(cases.len() >= 20, "정답표가 너무 작다: {}", cases.len());

        let mut bad = 0;
        for c in &cases {
            let name = c["name"].as_str().unwrap_or("(이름없음)");
            let text = c["text"].as_str().unwrap_or("");
            let want = counts_of(&c["counts"]);
            let got = pii_counts(text, &labels);
            if got != want {
                bad += 1;
                if bad <= 1000 {
                    // 어느 라벨이 몇 건 어긋났는지 바로 보이게 합집합으로 훑는다.
                    let mut keys: Vec<&String> = want.keys().chain(got.keys()).collect();
                    keys.sort();
                    keys.dedup();
                    eprintln!("[다름] {}", name);
                    for k in keys {
                        let w = *want.get(k).unwrap_or(&0);
                        let g = *got.get(k).unwrap_or(&0);
                        if w != g {
                            eprintln!("   {:18} ko-pii={} rust={}", k, w, g);
                        }
                    }
                }
            }
        }
        assert_eq!(bad, 0, "ko-pii 정답표와 다른 사례가 {}건 있다\n\
             → ko-pii 를 올렸다면 python scripts/gen_pii_golden.py 로 정답표를 \
             다시 뽑고, 그 차이만큼 src/pii.rs 를 따라가게 고쳐라.", bad);
    }

    //--------------------------------------------------------------
    // 검출한 '값' 자체가 정답표와 같은가(span 경계 검증)
    //=> 건수만 맞고 값이 다르면 span 경계가 틀린 것이다(예: 계좌 정규식 꼬리가
    //   줄바꿈까지 삼키거나, 국제표기 "+82" 가 잘려 나가는 경우). 건수 테스트는
    //   이걸 못 잡으므로 따로 본다.
    //   단 values_comparable=false 인 사례(전각·제로폭이 있어 정규화가 글자를
    //   실제로 바꾼 문서)는 건너뛴다 — ko-pii 는 값을 '원본 좌표'로 되돌려 주고
    //   Rust pii_records 는 '정규화 좌표' 값을 주므로 서로 다른 게 정상이다.
    //
    // -in: 없음
    //
    // -out: 없음
    // -out: error = 값이 다르면 assert 실패
    //--------------------------------------------------------------
    #[test]
    fn 정답표와_검출값이_같다() {
        let (labels, cases) = load();
        let mut bad = 0;
        let mut checked = 0;
        for c in &cases {
            // 정규화가 글자를 바꾼 사례는 값 좌표계가 달라 비교 대상이 아니다.
            if !c["values_comparable"].as_bool().unwrap_or(false) {
                continue;
            }
            checked += 1;
            let name = c["name"].as_str().unwrap_or("(이름없음)");
            let text = c["text"].as_str().unwrap_or("");

            // Rust 검출값을 라벨별로 모아 정렬한다(정답표도 정렬해 두었다 —
            // 검출 순서는 구현 세부라 '무엇을 잡았나'만 비교한다).
            let mut got: HashMap<String, Vec<String>> = HashMap::new();
            for (lab, val, _s, _e) in pii_records(text, &labels) {
                got.entry(lab.to_string()).or_default().push(val);
            }
            for v in got.values_mut() {
                v.sort();
            }

            let mut want: HashMap<String, Vec<String>> = HashMap::new();
            if let Some(obj) = c["values"].as_object() {
                for (lab, arr) in obj {
                    let list: Vec<String> = arr.as_array().map(|a| a.iter()
                        .filter_map(|s| s.as_str()).map(|s| s.to_string()).collect())
                        .unwrap_or_default();
                    want.insert(lab.clone(), list);
                }
            }

            if got != want {
                bad += 1;
                if bad <= 1000 {
                    eprintln!("[다름] {}\n   ko-pii: {:?}\n   rust  : {:?}", name, want, got);
                }
            }
        }
        assert!(checked > 0, "값 비교 대상 사례가 하나도 없다 — 정답표 생성이 잘못됐다");
        assert_eq!(bad, 0, "검출값이 다른 사례가 {}건 있다(span 경계 의심)", bad);
    }

    //--------------------------------------------------------------
    // 20라벨이 정답표에서 최소 1건씩은 검출되는가(커버리지 그물)
    //=> 어떤 라벨이 정답표에서 통째로 0건이면, Rust 가 그 라벨을 아예 안 잡아도
    //   위 두 테스트가 초록불이 된다. '검증한 줄 알았는데 안 한' 상태를 막는다.
    //   코퍼스를 손대다 라벨 사례를 지우면 여기서 먼저 걸린다.
    //
    // -in: 없음
    //
    // -out: 없음
    // -out: error = 한 번도 안 잡힌 라벨이 있으면 assert 실패
    //--------------------------------------------------------------
    #[test]
    fn 정답표가_20라벨을_모두_덮는다() {
        let (labels, cases) = load();
        let mut seen: HashSet<String> = HashSet::new();
        for c in &cases {
            if let Some(obj) = c["counts"].as_object() {
                for (k, n) in obj {
                    if n.as_u64().unwrap_or(0) > 0 {
                        seen.insert(k.clone());
                    }
                }
            }
        }
        let mut missing: Vec<&String> = labels.difference(&seen).collect();
        missing.sort();
        assert!(missing.is_empty(),
                "정답표가 못 덮는 라벨이 있다(그 라벨은 0 을 반환해도 통과한다): {:?}\n\
                 → scripts/gen_pii_golden.py 의 CORPUS 에 해당 라벨 사례를 넣어라.",
                missing);
    }
}

