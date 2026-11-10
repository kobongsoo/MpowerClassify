# -*- coding: utf-8 -*-
#------------------------------------------------------------------
# HTML 이름 엔티티 표 생성기 (Rust 판용)
#=> 파이썬 판은 HTMLParser(convert_charrefs=True) 를 쓰므로 HTML5 이름 엔티티를
#   **전부** 푼다. Rust 판은 손으로 고른 여섯 개(&lt; &gt; &quot; &apos; &nbsp;
#   &amp;)와 숫자 엔티티만 풀고 있어, `&mdash;`·`&middot;` 같은 것이 본문에
#   글자 그대로 남았다(2026-09-11 실측: doc/*.html 에 11곳).
#
#   [왜 전부 옮기나] "자주 쓰는 것만" 고르면 목록에 없는 엔티티가 나올 때마다
#   같은 사고가 되풀이된다. 어느 것을 넣을지 판단하는 일 자체를 없애는 편이
#   낫다. 표는 기계로 만들고, 사람은 손대지 않는다.
#
#   [왜 세미콜론 표기만] html.entities.html5 에는 `&amp` 처럼 세미콜론 없는
#   레거시 표기도 들어 있다. 그건 "뒤에 무엇이 오느냐"에 따라 해석이 달라져
#   단순 치환으로 옮길 수 없고, 실제 문서에도 사실상 안 나온다.
#
# 쓰는 법:
#   python scripts/gen_html_entities.py      # Rust/crates/docparse/src/html_entities.rs 를 다시 만든다
#------------------------------------------------------------------

import io
import os
import sys
from html.entities import html5

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "..", "Rust", "crates", "docparse", "src", "html_entities.rs")

HEAD = '''//! HTML 이름 엔티티 표 — **기계로 만든 파일이다. 손으로 고치지 말 것.**
//!
//! 만드는 법: `python scripts/gen_html_entities.py`
//! 원본: 파이썬 표준 라이브러리 `html.entities.html5` (세미콜론 표기 %d개)
//!
//! 파이썬 판이 HTMLParser 로 이 목록을 전부 풀기 때문에, 두 판의 추출 본문을
//! 같게 하려면 여기도 같은 목록이어야 한다. "자주 쓰는 것만" 고르면 목록에
//! 없는 엔티티가 나올 때마다 같은 어긋남이 되풀이된다.

/// (이름, 대응 문자열) — 이름은 `&` 와 `;` 를 뺀 알맹이다.
/// 정렬돼 있어 이진 탐색으로 찾는다.
pub static NAMED: &[(&str, &str)] = &[
'''

TAIL = '''];

//------------------------------------------------------------------
// 이름으로 대응 문자열 찾기
//=> 표가 이름순으로 정렬돼 있어 이진 탐색이 된다(2천 개를 훑지 않는다).
//
// -in: name = `&` 와 `;` 를 뺀 엔티티 이름(예: "mdash")
//
// -out: Some(대응 문자열) 또는 모르는 이름이면 None
//------------------------------------------------------------------
pub fn lookup(name: &str) -> Option<&'static str> {
    NAMED.binary_search_by(|(k, _)| (*k).cmp(name)).ok().map(|i| NAMED[i].1)
}
'''


#------------------------------------------------------------------
# 러스트 문자열 리터럴로 escape
#=> 따옴표·역슬래시는 물론, 눈에 안 보이는 글자(U+00A0 등)도 코드로 적어
#   파일을 열었을 때 무엇인지 보이게 한다.
#
# -in: s = 원본 문자열
#
# -out: 큰따옴표 안에 넣을 수 있는 문자열(따옴표는 포함하지 않는다)
# -out: error = 없음
#------------------------------------------------------------------
def rust_lit(s):
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F or ord(ch) in (0xA0, 0xAD) \
                or 0x2000 <= ord(ch) <= 0x200F or 0xFEFF == ord(ch):
            # 안 보이는 글자는 코드로 — 파일만 봐서는 공백과 구분이 안 된다.
            out.append("\\u{%X}" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


def main():
    items = sorted((k[:-1], v) for k, v in html5.items() if k.endswith(";"))
    body = "".join('    ("%s", "%s"),\n' % (k, rust_lit(v)) for k, v in items)
    io.open(OUT, "w", encoding="utf-8", newline="\n").write(
        (HEAD % len(items)) + body + TAIL)
    sys.stdout.reconfigure(encoding="utf-8")
    print("생성: %s (%d개)" % (os.path.normpath(OUT), len(items)))


if __name__ == "__main__":
    main()
