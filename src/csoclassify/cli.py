#------------------------------------------------------------------
# CLI 진입점 (설계서 §6)
#=> MpowerClassify.exe 의 명령행을 해석하고 모드를 분기한다.
#    - --serve/--status/--stop : 데몬 제어
#    - 일반 : 파일/폴더를 처리해 파일명+벡터(+시간)를 출력
#   기본은 데몬 on. 데몬을 못 쓰면 in-process 로 자동 폴백해 항상 동작하게 한다.
#------------------------------------------------------------------

import argparse
import dataclasses
import glob
import json
import os
import re
import sys

from . import __version__
from . import config
from . import record
from . import errcodes
from . import logsetup
from . import output
from . import seedcli
from .classify import bm25 as bm25mod
from .seedstore import norm_file as seedstore_norm_file
from .timing import Timing


#------------------------------------------------------------------
# 인자 해석 실패도 오류 계약으로 내보내는 파서
#=> argparse 는 인자가 틀리면 제 손으로 usage 를 찍고 종료코드 2 로 죽는다.
#   그 2 는 우리 종료코드 표(2=임베딩 실패)와 뜻이 겹쳐, 받는 쪽이 "모델이
#   없구나" 하고 엉뚱한 안내를 하게 된다. 그래서 error() 를 가로채
#   '인자 오류(3)'로 바꾸고, --json-errors 면 JSON 도 함께 낸다.
#   메시지를 보고 갈래를 나누는 이유: argparse 는 어떤 규칙에 걸렸는지를
#   예외 종류가 아니라 문장으로만 알려 준다.
#
# -필드: 없음(argparse.ArgumentParser 를 그대로 쓰고 error() 만 바꾼다)
#------------------------------------------------------------------
class _ContractParser(argparse.ArgumentParser):
    #--------------------------------------------------------------
    # 인자 오류 처리 — usage 는 그대로, 종료코드만 계약대로
    #=> argparse 기본 동작(usage 출력)은 사람에게 유용하므로 남기고,
    #   종료코드와 JSON 만 계약에 맞춘다.
    #
    # -in: message = argparse 가 만든 영어 오류 문장
    #
    # -out: 없음(프로세스 종료)
    # -out: error = 언제나 SystemExit(3)
    #--------------------------------------------------------------
    def error(self, message):
        # --axis 오타는 '축 전체 무시'로 조용히 이어질 수 있어 따로 구분한다.
        if "--axis" in message and "invalid choice" in message:
            kind = "bad_axis"
        # 배타 옵션(--rule-only 와 --vector-only)을 함께 준 경우.
        elif "not allowed with argument" in message:
            kind = "mode_conflict"
        else:
            kind = "bad_args"
        errcodes.emit(kind, message)
        # --json-errors 면 usage 도 내지 않는다 — 그것도 stderr 로 나가는 사람용 글이다.
        if not errcodes.is_json():
            self.print_usage(sys.stderr)
            print(f"[MpowerClassify] {message}", file=sys.stderr)
        sys.exit(errcodes.exit_of(kind))


#------------------------------------------------------------------
# 버전 문자열 만들기 (앱 버전 + 번들된 ko-pii 버전)
#=> ko-pii 는 PyInstaller 가 '빌드 시점에' exe 안으로 복사해 넣는다. 그래서
#   개발 PC 에서 pip 로 ko-pii 를 올려도 이미 만들어진 exe 는 옛 버전을 계속
#   쓴다. 그런데 그 '박혀 있는 버전'을 밖에서 알아낼 방법이 없어서, 배포본이
#   실제로 어떤 검출기를 쓰는지 확인할 수가 없었다(번들에는 dist-info 가
#   남지 않는다). 그래서 --version 이 직접 물어보고 찍어 준다.
#    1) ko_pii 를 그 자리에서 import 해 __version__ 을 읽는다
#    2) 못 읽어도 --version 은 성공해야 하므로, 사유만 괄호 안에 적는다
#
# -in: 없음
#
# -out: text = "MpowerClassify <앱버전> (ko-pii <버전>)" 형태의 한 줄
# -out: error = 예외 없음 — ko-pii 가 없거나 버전이 없으면 "없음"/"버전미상"으로 적는다
#------------------------------------------------------------------
def version_text():
    try:
        import ko_pii
        # 아주 옛 버전은 __version__ 이 없을 수 있어 기본값을 둔다.
        kopii = getattr(ko_pii, "__version__", None) or "버전미상"
    except Exception:
        # PII 검출은 ko-pii 없이는 못 하지만, 그 진단은 실제 스캔 때 하면 된다.
        # 여기서 죽으면 "왜 안 되는지" 물어보려던 사람이 답을 못 얻는다.
        kopii = "없음"
    return f"MpowerClassify {__version__} (ko-pii {kopii})"


#------------------------------------------------------------------
# --version 전용 동작 — 쓸 때만 ko-pii 를 읽는다
#=> argparse 의 기본 version 액션은 문자열을 '파서를 만드는 시점'에 받는다.
#   거기에 ko-pii 버전을 끼워 넣으면 --version 을 안 쓰는 평범한 실행에서도
#   매번 ko_pii 를 import 하게 된다(실측 약 0.28초). 실시간 UI 경로라 그
#   비용을 늘 물 수는 없어서, 실제로 --version 이 들어왔을 때만 읽도록
#   액션을 따로 만든다.
#
# -필드: 없음(argparse.Action 을 그대로 쓰고 __call__ 만 바꾼다)
#------------------------------------------------------------------
class _VersionAction(argparse.Action):
    #--------------------------------------------------------------
    # --version 이 실제로 주어졌을 때 호출된다
    #=> 버전 한 줄을 찍고 정상 종료(0)한다. argparse 의 version 액션과
    #   같은 동작이라, 쓰는 쪽에서는 달라진 게 없다.
    #
    # -in: parser = 이 액션을 가진 파서(종료 처리에 쓴다)
    # -in: namespace = 파싱 중인 결과(쓰지 않음)
    # -in: values = 이 옵션의 값(nargs=0 이라 항상 None)
    # -in: option_string = 실제로 쓰인 옵션 이름(쓰지 않음)
    #
    # -out: 없음(프로세스 종료)
    # -out: error = 언제나 SystemExit(0)
    #--------------------------------------------------------------
    def __call__(self, parser, namespace, values, option_string=None):
        parser.exit(message=version_text() + "\n")


# 없어진 옵션 → 대신 쓸 것. 2026-09-22 업무분류 규칙 자동 생성으로 옛 분류체계
# 스냅샷(doc_taxonomy.yaml)과 그것을 만들던 명령을 없앴다(설계서 업무분류-규칙파일-자동생성 7단계).
REMOVED_OPTIONS = {
    "--export-taxonomy":   "업무분류 규칙은 --build-doc-rule 로 만듭니다(분류체계가 규칙 안에 들어갑니다)",
    "--scaffold-doc-rule": "업무분류 규칙은 --build-doc-rule 로 만듭니다(분류체계가 규칙 안에 들어갑니다)",
    "--sync-doc-rule":     "업무분류 규칙은 --build-doc-rule 로 만듭니다(분류체계가 규칙 안에 들어갑니다)",
    "--no-fill-blank":     "업무분류 규칙은 --build-doc-rule 로 만듭니다(분류체계가 규칙 안에 들어갑니다)",
    "--sync-enrich":       "업무분류 규칙은 --build-doc-rule 로 만듭니다(분류체계가 규칙 안에 들어갑니다)",
    "--taxonomy":          "분류체계는 doc_rule.yaml(--build-doc-rule 로 만든 것) 안에 있습니다",
    # 이름만 바뀐 둘(2026-09-28). 옛 이름을 조용히 받아 주면 배치가
    # 언제까지나 옛 이름에 머무르고, 두 이름이 같은 자리를 가리키는 상태가 굳는다.
    "--rules":             "보안등급 규칙셋은 --cso-rules 로 지정합니다(업무분류는 --doc-rules)",
    "--save-text":         "추출 본문을 남길 폴더는 --textsave 로 지정합니다",
}

# 이 중 값을 받던 옵션들. 값까지 삼켜야 뒤 인자가 대상 파일로 잘못 읽히지 않는다.
REMOVED_WITH_VALUE = {"--taxonomy", "--rules", "--save-text"}


#------------------------------------------------------------------
# 없어진 옵션을 줬는지 본다
#=> 숨겨 둔 옛 옵션(REMOVED_OPTIONS)이 하나라도 들어왔으면 무엇을 대신 쓰는지 알리고 멈춘다.
#   조용히 무시하면 부른 쪽은 옛 명령이 먹힌 줄 안다 — 그래서 '인자 오류(3)'로 끝낸다.
#
# -in: args = 파싱된 인자
#
# -out: code = 멈춰야 하면 종료코드(3), 아니면 None
# -out: error = 없음
#------------------------------------------------------------------
def check_removed_options(args):
    for flag, hint in REMOVED_OPTIONS.items():
        dest = "removed_" + flag[2:].replace("-", "_")
        val = getattr(args, dest, None)
        # --taxonomy 는 값(문자열)을, 나머지는 True 를 담는다 — 둘 다 '줬다'는 뜻이다.
        if val is not None and val is not False:
            return fail_err("unsupported_option",
                            f"[MpowerClassify] {flag} 는 없어졌습니다 — {hint}.")
    return None


#------------------------------------------------------------------
# 인자 파서 구성
#=> 설계서 §6 의 옵션들을 argparse 로 정의한다. 긴 이름은 '--'(GNU 관례)로 통일하고
#   '-file'/'-dir' 은 하위호환 별칭으로만 유지한다.
#
# -in: 없음
#
# -out: parser = 구성된 ArgumentParser
# -out: error = 없음
#------------------------------------------------------------------
def build_parser():
    p = _ContractParser(
        prog="MpowerClassify",
        description="한국어 문서 → C/S/O 및 분류체계 자동분류(기본) · 임베딩 벡터(--embed) · 텍스트 추출(--text-only)",
    )
    # 입력 대상
    # 표준(GNU) 관례에 맞춰 긴 이름은 '--' 로 통일한다(--file/--dir). 기존 스크립트·
    # UI 가 쓰던 단일대시 '-file'/'-dir' 도 하위호환 별칭으로 계속 받는다.
    p.add_argument("--file", "-file", dest="file", help="대상 문서 1개 경로")
    p.add_argument("--dir", "-dir", dest="dir", help="대상 폴더(배치)")
    p.add_argument("--files-from", dest="files_from", default=None,
                   help="처리할 파일 경로 목록(한 줄에 하나, '-' 면 표준입력). "
                        "여러 폴더에 흩어진 파일을 한 프로세스로 처리 — --file/--dir 과 배타")
    # 문서 식별자(doc_id) — 설계 §7-5 · D0/D0b.
    p.add_argument("--filelist", dest="filelist", default=None,
                   help="MpowerV11 이 뽑아 준 {path, sfile_id} 목록(jsonl 또는 csv). "
                        "이 목록으로 결과 레코드의 doc_id 를 채운다. "
                        "--file/--dir 이 있으면 그쪽이 대상을 정하고 목록은 'ID 사전' "
                        "역할만 하며, 없으면 목록에 적힌 파일이 대상이 된다")
    p.add_argument("--no-doc-id", dest="no_doc_id", action="store_true",
                   help="결과 레코드에 doc_id/key 를 넣지 않는다(보안등급만 볼 때). "
                        "기본은 넣는다 — 계산 비용이 사실상 0 이고, 사람이 고친 등급을 "
                        "결과와 잇는 데도 쓰인다")
    p.add_argument("--report-missing-id", dest="report_missing_id", default=None,
                   metavar="경로",
                   help="sfile_id 를 못 얻어 폴백으로 채운 문서 목록을 이 파일에 쓴다"
                        "(jsonl). 그 문서들은 매핑 테이블 적재 대상이 아니다")
    # 엠파워 v11 암호화 문서 복호화 — 설계: plan/엠파워v11-암호화문서-복호화연동-설계-20261012.html
    #   [다량] --filelist 의 각 줄에 mrk·enc_mode·zip_mode 를 적으면 그 줄만 푼다(옵션 불필요).
    #   [단일] --file 에 --decrypt 를 붙일 때만 푼다(안 붙이면 지금처럼 그대로 읽는다).
    p.add_argument("--decrypt", dest="decrypt", action="store_true",
                   help="[엠파워 v11] --file 한 건을 removeenc.sh 로 풀어서 분류한다. "
                        "--mrk · --enc-mode · --zip-mode 가 함께 있어야 한다. "
                        "풀린 파일은 분류가 끝나면 바로 지운다")
    p.add_argument("--no-decrypt", dest="no_decrypt", action="store_true",
                   help="[엠파워 v11] 목록(--filelist)에 열쇠가 있어도 풀지 않고 그대로 읽는다")
    p.add_argument("--mrk", dest="mrk", default=None, metavar="값",
                   help="[엠파워 v11] 평문 MRK(환경변수 CSOCLASSIFY_MRK 로도 줄 수 있다). "
                        "'v2:' 로 시작하는 감싼 값은 받지 않는다. 로그·결과에는 남기지 않는다")
    p.add_argument("--enc-mode", dest="enc_mode", default=None, metavar="값",
                   help="[엠파워 v11] ENC_MODE(0 도 올바른 값)")
    p.add_argument("--zip-mode", dest="zip_mode", default=None, metavar="값",
                   help="[엠파워 v11] ZIP_MODE(0 도 올바른 값)")
    p.add_argument("--name", dest="dec_name", default=None, metavar="이름",
                   help="[엠파워 v11] 원래 파일 이름(확장자 판별 · 파일명 신호). "
                        "저장 이름에는 확장자도 뜻도 없어서 주기를 권한다")
    p.add_argument("--size", dest="dec_size", default=None, metavar="바이트",
                   help="[엠파워 v11] 원래 파일 크기(바이트). 주면 풀린 결과의 크기가 이 값과 "
                        "같은지 본다(잘린 결과·덜 풀린 압축을 잡는다). 안 주면 크기 검사를 건너뛴다")
    p.add_argument("--decrypt-cmd", dest="decrypt_cmd", default=None, metavar="경로",
                   help="[엠파워 v11] 복호화 도구 경로(환경변수 CSOCLASSIFY_DECRYPT_CMD, "
                        "기본 /MOCOMSYS/MpowerScript/removeenc/removeenc.sh)")
    p.add_argument("--decrypt-tmp", dest="decrypt_tmp", default=None, metavar="폴더",
                   help="[엠파워 v11] 풀린 파일을 잠깐 둘 곳(환경변수 CSOCLASSIFY_DECRYPT_TMP, "
                        "기본 시스템 임시 폴더). /dev/shm 을 주면 디스크에 닿지 않는다")
    p.add_argument("--decrypt-timeout", dest="decrypt_timeout", type=int, default=None,
                   metavar="초", help="[엠파워 v11] 복호화 한 건의 시간 제한(기본 60)")
    p.add_argument("--glob", dest="glob", default="*",
                   help="배치 필터 패턴(기본 *). 여러 개는 콤마로 나열: "
                        "예) \"*.hwp,*.docx,*.pdf\" (중괄호 \"*.{hwp,docx,pdf}\" 도 가능)")
    # --dir 은 이제 '항상 하위폴더까지 재귀'가 기본이다. -r/--recursive 는 하위호환용으로
    # 받아만 주고 무시한다(줘도 안 줘도 재귀). 기존 스크립트가 깨지지 않게 유지.
    p.add_argument("-r", "--recursive", action="store_true",
                   help="(무시됨) --dir 은 항상 하위 폴더까지 재귀 탐색합니다")

    # 모델/청킹/출력
    p.add_argument("--model", default=config.DEFAULT_MODEL, help="임베딩 모델 별칭")
    # default 를 None 으로 두는 이유: "사용자가 --format 을 줬는가"를 알아야
    # --out 확장자로 추론할지 말지를 정할 수 있다. 기본값을 여기서 박아 두면
    # 명시한 "json" 과 기본값 "json" 을 구분할 수 없다. 실제 기본값은
    # resolve_format() 이 넣는다.
    p.add_argument("--format", dest="fmt", choices=["text", "json", "jsonl"],
                   default=None,
                   help=f"출력 형식(미지정 시 --out 확장자로 판단, 그것도 없으면 {config.DEFAULT_FORMAT})")
    p.add_argument("--out", dest="out", help="결과 저장 파일(미지정 시 stdout)")
    p.add_argument("--per-chunk", action="store_true", help="청크별 벡터 출력")
    p.add_argument("--max-tokens", type=int, default=config.DEFAULT_MAX_TOKENS, help="청크 최대 토큰")
    p.add_argument("--overlap", type=int, default=config.DEFAULT_OVERLAP, help="청크 겹침 토큰")
    p.add_argument("--normalize", dest="normalize", action="store_true", default=config.DEFAULT_NORMALIZE)
    p.add_argument("--no-normalize", dest="normalize", action="store_false", help="L2 정규화 끄기")
    p.add_argument("--precision", choices=["f32", "f16"], default=config.DEFAULT_PRECISION, help="출력 벡터 정밀도")

    # 시간/텍스트 보존/디버그
    p.add_argument("--timing", dest="timing", action="store_true", default=True)
    p.add_argument("--no-timing", dest="timing", action="store_false", help="처리 시간 출력 끄기")
    # 추출 본문 보존 — 설계: plan/추출텍스트-저장-설계-20260907.html
    # 폴더를 필수로 받는다. 예전 --save-text 는 값을 생략하면 ./_extracted 로 갔는데,
    # 개인정보가 든 파일이 "어디에 쌓이는지 모르는 채로" 생기면 안 된다(설계 S1).
    p.add_argument("--textsave", dest="textsave", metavar="폴더", default=None,
                   help="추출 본문을 이 폴더에 <sha256>.txt 로 남긴다(개인정보 포함 가능)")
    # 옛 이름 — 같은 동작. 쓰면 안내 한 줄을 내고 계속한다(옛 명령줄을 깨지 않는다).
    p.add_argument("--text-only", action="store_true", help="벡터 없이 추출 텍스트만 출력")

    # 문서 크기 상한(Size Gate) — 설계: plan/문서크기-상한-설계-20260904.html
    # 기본값은 config 가 갖고, 여기서는 '이번 실행만' 덮어쓴다(기본값을 박지 않는
    # 이유: 환경변수로 현장 조정한 값을 CLI 기본값이 덮어써 버리면 안 된다).
    p.add_argument("--max-file-mb", dest="max_file_mb", type=float, default=None,
                   help=f"원본 파일 크기 상한(MB, 기본 {config.MAX_FILE_BYTES // 1048576}). "
                        f"텍스트 계열(txt/csv/tsv/json/html)은 별도로 더 낮다"
                        f"(기본 {config.MAX_FILE_BYTES_TEXT // 1048576}MB). "
                        "넘으면 읽지 않고 보류 레코드로 내보낸다.")
    p.add_argument("--max-file-mb-text", dest="max_file_mb_text", type=float, default=None,
                   help=f"텍스트 계열(txt/csv/tsv/json/html) 원본 크기 상한(MB, 기본 "
                        f"{config.MAX_FILE_BYTES_TEXT // 1048576}). 이 포맷군만 바이트=글자수라 "
                        "따로 둔다. 주면 --max-file-mb 보다 우선한다.")
    p.add_argument("--max-archive-mb", dest="max_archive_mb", type=float, default=None,
                   help=f"압축 1건당 해제 누적 상한(MB, 기본 "
                        f"{config.MAX_ARCHIVE_BYTES // 1048576}). 넘으면 그 압축은 펼치지 않고 "
                        "1건으로 둔다(내부 문서가 분류되지 않는다).")
    p.add_argument("--max-archive-members", dest="max_archive_members", type=int, default=None,
                   help=f"압축 1건당 내부 파일 개수 상한(기본 {config.MAX_ARCHIVE_MEMBERS}). "
                        "0바이트 파일 수백만 개짜리 폭탄은 용량으로는 안 걸린다.")
    p.add_argument("--parser-timeout", dest="parser_timeout", type=int, default=None,
                   help=f"파서 1파일 시간 상한(초, 기본 {config.PARSER_TIMEOUT}). 넘으면 "
                        "읽은 부분까지만 쓰고 partial_extract 표식을 단다.")
    p.add_argument("--max-pdf-pages", dest="max_pdf_pages", type=int, default=None,
                   help=f"PDF 1파일 페이지 상한(기본 {config.MAX_PDF_PAGES}). "
                        "넘으면 앞 N 페이지만 읽고 partial_extract 표식을 단다.")
    p.add_argument("--max-text-chars", dest="max_text_chars", type=int, default=None,
                   help=f"정제 본문 글자수 상한(기본 {config.MAX_TEXT_CHARS:,}). "
                        "넘으면 앞부분만 보고 분류하고 결과에 text_truncated 표식을 단다.")
    p.add_argument("--no-size-limit", dest="no_size_limit", action="store_true",
                   help="문서 크기 상한을 모두 해제한다(조사·디버깅 전용). "
                        "초대형 문서에서 시간·메모리가 무제한으로 늘 수 있다.")
    # 추출 방식 — 기본은 '자체 파서'(하이브리드). Rust 포트와 추출 텍스트를 맞추기
    # 위한 기본값이다(config.DEFAULT_HYBRID_PARSE 주석 참고).
    p.add_argument("--hybridparse", dest="hybridparse", action="store_true",
                   default=config.DEFAULT_HYBRID_PARSE,
                   help="내용 감지로 포맷별 전용 파서 추출: pdf→pypdfium2, hwp→HWP5, "
                        "hwpx→zip/OWPML, doc/ppt→자체파서, xls→xlrd, docx/xlsx/pptx→python-*, text→직접읽기. "
                        "전용 파서 실패 시 snf 폴백(snf 없으면 그 파일만 미분류). "
                        "[기본값이라 따로 줄 필요 없다 — 되돌리려면 --synap-only] "
                        "설계: plan/CSO_HybridParse.html")
    p.add_argument("--synap-only", dest="hybridparse", action="store_false",
                   help="종전 방식 — 모든 포맷을 사이냅(snf_exe) 하나로만 추출한다. "
                        "자체 파서가 특정 문서에서 이상할 때 비교용으로 쓴다.")
    # 설계: plan/Rust-사이냅추출기-옵션-설계-20261019.html — Rust 판과 같은 이름을 받는다.
    p.add_argument("--synap-fallback", dest="synap_fallback", action="store_true",
                   help="자체 파서가 못 읽은 문서만 사이냅으로(기본 동작과 같다). 명시하면 "
                        "snf_exe 가 꼭 있어야 하고, 없으면 문서를 읽기 전에 1016 synap_missing 으로 멈춘다")
    p.add_argument("--synap-path", dest="synap_path", default=None, metavar="경로",
                   help="사이냅 실행파일(snf_exe) 경로를 직접 준다(찾기 순서를 건너뛴다)")

    # 등급 스트림 기록(설계: plan/등급스트림-기록-옵션-설계-20261019.html). Rust 판과 같은 이름이다.
    p.add_argument("--write-grade-stream", dest="write_grade_stream", action="store_true",
                   help="분류한 문서의 NTFS 대체 스트림(:mgrade)에 최종 등급(C/S/O)을 쓴다 — "
                        "NEOGradeToolApp -s 와 같은 형식. Windows(NTFS)만")
    p.add_argument("--grade-stream-unclassified", dest="grade_stream_unclassified", default=None,
                   metavar="skip|none",
                   help="등급을 못 정한 문서: skip=건드리지 않음(기본), none=NONE(0) 기록")
    # 사람이 정한 등급(설계: plan/사람등급지정-수동오버라이드-설계-20261020.html). Rust 판과 같은 이름.
    p.add_argument("--grade-overrides", dest="grade_overrides", default=None, metavar="파일",
                   help="사람 결정 목록(UI 의 cso_override.jsonl). 판정이 끝난 뒤 사람 등급이 판정을 이긴다. "
                        "--set-grade 와 함께면 그 결정을 이 파일에 덧붙인다")
    p.add_argument("--set-grade", dest="set_grade", default=None, metavar="C|S|O|NONE",
                   help="문서를 읽지 않고 --file 문서에 이 등급을 바로 정한다(--write-grade-stream 이면 파일에도 쓴다)")
    p.add_argument("--reviewer", dest="reviewer", default=None, metavar="이름",
                   help="--set-grade 결정을 --grade-overrides 에 덧붙일 때 검토자(필수)")
    p.add_argument("--reason", dest="reason", default=None, metavar="사유",
                   help="--set-grade 결정을 --grade-overrides 에 덧붙일 때 사유(필수)")
    # 시각 보존이 기본이다(설계 §14) — 바꾸고 싶을 때만 이 옵션을 준다.
    p.add_argument("--grade-stream-update-time", dest="grade_stream_update_time", action="store_true",
                   help="등급을 쓸 때 문서의 수정 시각이 바뀌게 둔다(툴 기본, -k 없음). "
                        "기본은 생성·접근·수정 시각을 그대로 둔다")

    # 처리 모드 — 기본은 C/S/O 자동 분류다. 임베딩 벡터만 필요하면 --embed,
    # 추출 텍스트만 필요하면 --text-only 를 준다.
    p.add_argument("--embed", dest="embed", action="store_true",
                   help="임베딩 벡터만 출력(분류 없이). 미지정 시 기본은 C/S/O 분류")
    # (하위호환) 예전의 --classify 는 이제 '기본 동작'이라 붙일 필요가 없다. 기존
    # 실패를 기계가 읽을 수 있게 낸다(설계: plan/CLI-오류출력-설계.html).
    # 옵션을 안 주면 오늘과 똑같이 동작한다 — 지금 이 CLI 를 쓰는 쪽의 stdout 파싱이
    # 깨지지 않도록 '선택'으로 둔다.
    # 축약본만 받으면 "이 문서가 왜 C 인가"에 답할 수 없다. 판정에 쓰인 신호와
    # 규칙 id·건수만 요약해 얹는다(원문 값은 애초에 저장하지 않으므로 새는 것이 없다).
    p.add_argument("--simple-why", dest="simple_why", action="store_true",
                   help="--simple 에 판정 근거 요약(why)을 더한다"
                        "(어느 신호가 정했는지 · 어느 규칙이 몇 건). --simple 을 포함한다")
    p.add_argument("--json-errors", dest="json_errors", action="store_true",
                   help="실패할 때 stdout 에 오류 JSON 한 줄을 낸다"
                        "({\"error\":{code,kind,message,path}})")
    # 스크립트·UI 가 계속 넘겨도 오류 없이 받아들이되(무시), 도움말/문서에는 노출하지 않는다.
    p.add_argument("--classify", dest="classify", action="store_true",
                   help=argparse.SUPPRESS)
    # C/S/O 자동 분류(규칙 스캔 Signal A)
    p.add_argument("--cso-rules", dest="rules", default=None,
                   help="보안등급 규칙셋 경로(기본 resources/policy/cso_rule.yaml)")
    p.add_argument("--failsafe", dest="failsafe", nargs="?", const="S", default=None,
                   help="규칙 미검출 시 부여할 기본등급(값 생략 시 S)")
    p.add_argument("--check-rules", dest="check_rules", action="store_true",
                   help="규칙셋(cso_rule.yaml)의 등급 값만 검사하고 종료(문서는 읽지 않음). "
                        "정상 0, 검증 실패 4")
    # 업무 분류(doctype) 축 — 문서분류체계 연동(설계서 §3~6).
    p.add_argument("--doc-rules", dest="doc_rules", default=None,
                   help="업무분류 규칙셋 경로(기본 resources/policy/doc_rule.yaml). "
                        "--build-doc-rule 로 만든 파일이며 분류체계가 그 안에 있다. "
                        "없으면 doctype 축을 건너뛰고 security 만 처리(경고 후 계속)")
    p.add_argument("--axis", dest="axis", choices=["security", "doctype"], default=None,
                   help="처리할 축을 하나로 제한(미지정 시 가능한 축 전부). "
                        "doctype 은 PII 검출을 생략해 더 빠르다")
    p.add_argument("--conflict", dest="conflict", default=None,
                   help="doctype 축의 충돌 전략을 실행 시 덮어쓰기: 예) --conflict doctype=top_n:3 "
                        "(security 축은 서열이 있어 덮어쓸 수 없음 — 주면 오류)")
    # 업무분류 규칙 자동 생성(문서 처리 없음, --build-doc-rule 단독 모드).
    p.add_argument("--export-input", dest="export_input", default=None,
                   help="--build-doc-rule 의 원본 체계 JSON 경로(기본: 규칙 파일 옆 "
                        "doc_classification_export.json)")
    p.add_argument("--seed-fill-title", dest="seed_fill_title", action="store_true",
                   help="기준 문서(--seeds)에 제목(첫 줄)을 채운다 — 업무분류 BM25 2차의 재료. "
                        "본문은 --text-dir/<지문>.txt 에서 먼저 찾고, 없으면 원본 파일을 다시 읽는다")
    p.add_argument("--build-doc-rule", dest="build_doc_rule", action="store_true",
                   help="체계 JSON(--export-input) · 유의어 사전 · 본보기 · doc_rule.local.yaml 로 "
                        "--doc-rules 파일을 통째로 새로 만든다(자동 생성 — 사람이 고치지 않는 파일). "
                        "기존 파일은 .bak 으로 남긴다")
    # 없어진 옵션 — 도움말에는 숨기되 받아는 둔다. 모르는 옵션으로 argparse 가 멈추면
    # "왜 없어졌고 무엇을 쓰면 되는지"를 알려 줄 수 없다(엠파워 배치가 옛 명령을 부를 수 있다).
    for flag in REMOVED_OPTIONS:
        dest = "removed_" + flag[2:].replace("-", "_")
        if flag in REMOVED_WITH_VALUE:
            p.add_argument(flag, dest=dest, nargs="?", const="",
                           default=None, help=argparse.SUPPRESS)
        else:
            p.add_argument(flag, dest=dest, action="store_true",
                           help=argparse.SUPPRESS)
    # ── 규칙 단어 제안(--suggest-terms) — 문서도 모델도 필요 없는 단독 모드 ──
    # 사람이 확정한 문서에서 "이 분류에만 나오는 말"을 뽑아 보여 주기만 한다.
    # 규칙 파일은 건드리지 않는다 — 넣는 것은 사람이 화면에서 할 일이다.
    p.add_argument("--suggest-terms", dest="suggest_terms", metavar="DC_ID",
                   default=None,
                   help="업무분류 규칙에 넣을 만한 단어 후보를 보여 준다. 분류 하나의 "
                        "dc_id 를 주거나 all 을 주면 확정 문서가 있는 분류를 모두 훑는다. "
                        "규칙 파일은 고치지 않는다(보여 주기만)")
    p.add_argument("--overrides", dest="overrides", default=None,
                   help="--suggest-terms 재료 — 검토 화면의 확정·거절 이력"
                        "(cso_override.jsonl). 기본은 정책 폴더 옆")
    p.add_argument("--text-dir", dest="text_dir", default=None,
                   help="--suggest-terms 재료 — --textsave 로 저장해 둔 추출 본문 폴더. "
                        "없으면 제목·파일 이름만으로 후보를 만든다")
    p.add_argument("--stopwords", dest="stopwords", default=None,
                   help="--suggest-terms 에서 제안하지 않을 말 목록"
                        "(doc_rule_stopwords.yaml). 기본은 정책 폴더 옆")
    p.add_argument("--suggest-json", dest="suggest_json", metavar="경로", default=None,
                   help="--suggest-terms 결과를 json 으로도 남긴다")
    p.add_argument("--suggest-min-docs", dest="suggest_min_docs", type=int, default=None,
                   help="--suggest-terms 에서 제안을 시작할 최소 확정 문서 수(기본 3)")
    p.add_argument("--include-auto-seeds", dest="include_auto_seeds",
                   action="store_true",
                   help="--suggest-terms 에서 사람이 승인하지 않은 seed(자동 선별분)도 "
                        "재료로 쓴다. 규칙→seed→규칙 자기강화가 생길 수 있어 기본은 끔")
    p.add_argument("--with-vector", dest="with_vector", action="store_true",
                   help="분류 시 모든 문서에 임베딩 벡터 산출(구 동작, 모델 필요)")
    p.add_argument("--embed-needed", dest="embed_needed", action="store_true",
                   help="임베딩을 '보류(미확정)'와 'seed_eligible' 문서에만 수행(확정문서 임베딩 생략, 속도↑)")
    p.add_argument("--propagate", dest="propagate", default=None,
                   help="1차 레코드(jsonl)에 임베딩 라벨 전파를 적용해 재분류")
    p.add_argument("--seeds", dest="seeds", default=None,
                   help="전파 비교 기준 seed 저장소(class_seed.jsonl). 없으면 코퍼스 내부 seed 사용")
    p.add_argument("--with-text", dest="with_text", action="store_true",
                   help="분류 결과 레코드에 추출(정제) 텍스트를 함께 저장(기본 미포함=프라이버시)")
    p.add_argument("--with-pii", dest="with_pii", action="store_true",
                   help="[프라이버시 주의] 검출된 원문 PII 값(주민번호·카드번호 등 실제 값)을 "
                        "결과의 pii 필드에 함께 저장(기본 미포함). 결과 파일 취급에 주의")
    p.add_argument("--auto-propagate", dest="auto_propagate", action="store_true",
                   help="분류 직후 보류 문서를 seed(--seeds 또는 내부)로 전파까지 수행(보류 임베딩 자동 활성화)")
    # 업무분류 2단계(벡터 비교)를 켜려면 seed 가 있어야 하는데, 사람이 라벨링할
    # 수는 없다. 규칙이 아주 자신 있게 분류한 문서를 씨앗으로 재활용한다(재설계 10장).
    p.add_argument("--make-doctype-seeds", dest="make_doctype_seeds", default=None,
                   metavar="경로",
                   help="업무분류 seed 를 만들어 지정한 class_seed.jsonl 에 기록한다"
                        "(규칙 고신뢰 문서만 · 전량 임베딩 자동 활성화). "
                        "기존 보안등급 seed 는 보존하고 업무분류 seed 만 갈아 끼운다")

    # 기준 문서를 화면 없이 등록하는 옵션들(설계서 '기준문서-CLI등록'). 웹·배치가
    # exe 한 번으로 부를 수 있게 하는 것이 목적이라, 옵션 정의는 seedcli 에 모아 둔다.
    seedcli.add_args(p)
    p.add_argument("--seed-per-dir", dest="seed_per_dir", type=int, default=6,
                   help="--make-doctype-seeds: 한 폴더에서 뽑을 최대 seed 수(기본 6). "
                        "편향 완화용 — 한 폴더 문서 모양이 그 분류의 정의가 되는 것을 막는다. "
                        "실측(검증셋 150건)에서 3 은 너무 좁아 씨앗이 33건뿐이었고, "
                        "6 으로 풀면 48건이 되면서 정밀도·재현율이 둘 다 올랐다")
    p.add_argument("--seed-per-node", dest="seed_per_node", type=int, default=50,
                   help="--make-doctype-seeds: 한 분류에서 뽑을 최대 seed 수(기본 50)")
    # 분류 방식 배타 선택: 규칙만(rule-only) vs 벡터만(vector-only). 둘 다 안 주면 기존 동작.
    _clsmode = p.add_mutually_exclusive_group()
    _clsmode.add_argument("--rule-only", dest="rule_only", action="store_true",
                   help="규칙(cso_rule.yaml)만으로 분류 — 임베딩·전파를 하지 않음(가장 빠름). --vector-only 와 배타")
    _clsmode.add_argument("--vector-only", dest="vector_only", action="store_true",
                   help="규칙 검사 없이 임베딩 벡터를 seed 와 비교해서만 분류(--seeds 필수). --rule-only 와 배타")
    p.add_argument("--doctype-vector-only", dest="doctype_vector_only",
                   action="store_true",
                   help="업무분류(doctype)를 규칙 없이 기준 문서(class_seed.jsonl) 비교로만 "
                        "분류한다. 규칙 파일의 embed·conflict 설정은 그대로 쓴다. "
                        "security 축은 영향을 받지 않는다")
    p.add_argument("--progress", action="store_true",
                   help="파일별 진행 상황을 stderr 로 출력(형식: '[progress] 처리수/총수 경로'). "
                        "2차 패스(전파)도 단계 이름을 달고 같은 모양으로 나간다"
                        "('[progress][전파·보안등급]'·'[progress][전파·업무분류]'). UI 진행바용")

    # 출력 축약 옵션(C/S/O 분류 모드 전용). 모두 JSON 으로 출력한다.
    p.add_argument("--hash", dest="hash", action="store_true",
                   help="각 문서의 해시(SHA-256)를 결과 레코드에 hash 필드로 포함")
    # 요약(summary) 제어 — 배타: '요약만' vs '요약 제거'.
    _sumgrp = p.add_mutually_exclusive_group()
    _sumgrp.add_argument("--summary", dest="summary_only", action="store_true",
                   help="파일별 레코드 없이 '맨 끝 요약(summary)'만 JSON 으로 출력(--file/--dir)")
    _sumgrp.add_argument("--nosummary", dest="no_summary", action="store_true",
                   help="맨 끝 요약(summary) 레코드를 결과 출력에서 제거(파일별 레코드만)")
    p.add_argument("--simple", dest="simple", action="store_true",
                   help="파일별 결과를 문서명·해시·등급(+업무분류 dc_id·doc_id)으로 줄여서 "
                        "출력. doc_id 는 목록의 sfile_id 로 채워진 것만 싣는다"
                        "(폴백은 null — hash 로 가림)")

    # 데몬 제어
    p.add_argument("--daemon", dest="daemon", action="store_true", default=config.DEFAULT_DAEMON)
    p.add_argument("--no-daemon", dest="daemon", action="store_false", help="데몬 없이 in-process 처리")
    p.add_argument("--serve", action="store_true", help="데몬 프로세스로 기동")
    p.add_argument("--status", action="store_true", help="데몬 상태 출력")
    p.add_argument("--stop", action="store_true", help="데몬 정지")
    p.add_argument("--idle-timeout", type=int, default=config.DEFAULT_IDLE_TIMEOUT,
                   help="데몬 유휴 자동종료(초). 미지정/0 = 무한(자동종료 없음). 예: --idle-timeout 600")
    p.add_argument("--num-threads", type=int, default=None, help="onnxruntime 스레드 수")

    p.add_argument("-v", "--verbose", action="store_true", help="상세 로그(화면 출력 + DEBUG)")
    p.add_argument("--log", dest="log", default=None,
                   help="로그 파일 경로(미지정 시 <exe폴더>/log/class_날짜.log)")
    # 번들된 ko-pii 버전까지 찍는다. 문자열을 미리 만들지 않고 액션으로 미루는
    # 이유는 _VersionAction 헤더 참고(평상시 실행에 ko_pii import 비용을 안 물린다).
    p.add_argument("--version", action=_VersionAction, nargs=0,
                   help="버전 출력(앱 버전 + 번들된 ko-pii 버전)")
    return p


#------------------------------------------------------------------
# --textsave 저장 폴더를 미리 준비한다
#=> 폴더를 시작 전에 만들고 실제로 써 본다. 여기서 막지 않으면 문서 수천 건을
#   다 돌린 뒤에야 "한 건도 저장 못 했다"를 알게 된다(설계 9장).
#    1) 폴더를 만들고 실제로 써 보며 확인한다
#    2) "평문으로 남긴다"를 화면에 한 줄 알린다 — 켜 사람이 못 봤다고 할 수 없게
#
# -in: args = argparse 결과(제자리에서 args.textsave 를 절대경로로 바꾼다)
#
# -out: code = 계속해도 되면 None, 끝내야 하면 종료코드
# -out: error = 없음(실패는 fail_err 로 안내한 뒤 종료코드를 돌려준다)
#------------------------------------------------------------------
def resolve_textsave(args):
    if not getattr(args, "textsave", None):
        return None
    from . import textsave as _ts
    try:
        args.textsave = _ts.prepare(args.textsave)
    except OSError as e:
        return fail_err("bad_args",
                        f"[MpowerClassify] --textsave 폴더를 쓸 수 없습니다: {e}",
                        args.textsave)
    # 이 옵션은 "민감정보를 디스크에 남기지 않는다"는 기본 방어를 여는 문이다.
    # 조용히 열리면 안 된다(설계 10장 S2).
    print(f"[MpowerClassify] --textsave: 추출 본문(개인정보 포함 가능)을 "
          f"{args.textsave} 에 평문으로 남깁니다. 보안등급 C 문서도 그대로 남습니다.",
          file=sys.stderr)
    return None


#------------------------------------------------------------------
# 옵션 dict 구성
#=> 파싱된 args 에서 파이프라인/데몬이 쓰는 처리 옵션만 뽑아 dict 로 만든다(데몬에
#   IPC 로 넘기기 좋게 순수 값들로 구성). 저장 폴더는 resolve_textsave() 가
#   이미 절대경로로 확정해 둔 값을 그대로 싣는다.
#
# -in: args = argparse 결과 네임스페이스
#
# -out: opts = {max_tokens, overlap, normalize, per_chunk, precision, save_dir, ...}
# -out: error = 없음
#------------------------------------------------------------------
def make_opts(args):
    # 저장 폴더는 resolve_textsave() 가 이미 절대경로로 확정해 둔 값이다.
    save_dir = getattr(args, "textsave", None)

    return {
        "max_tokens": args.max_tokens,
        "overlap": args.overlap,
        "normalize": args.normalize,
        "per_chunk": args.per_chunk,
        "precision": args.precision,
        "save_dir": save_dir,
        "keep_page_markers": False,
        # 추출기 선택용(--hybridparse). run_text_only 처럼 args 를 직접 안 받는 경로가 읽는다.
        "hybridparse": getattr(args, "hybridparse", False),
        # 글자수 상한(G3). 분류 경로만이 아니라 --embed 경로(pipeline.process_file)와
        # 데몬 경로도 같은 상한을 갖게 해야, 같은 문서를 어느 경로로 넣든 결과가
        # 갈리지 않는다. None 이면 상한 없음(--no-size-limit).
        "max_text_chars": _resolve_text_limit(args),
    }


#------------------------------------------------------------------
# glob 패턴의 중괄호 확장
#=> 파이썬 표준 glob 은 셸과 달리 중괄호({a,b,c}) 확장을 지원하지 않는다. 그래서
#   확장자를 여러 개 넣고 싶을 때 쓰도록, "*.{hwp,docx,pdf}" 같은 패턴을
#   ["*.hwp","*.docx","*.pdf"] 로 직접 펼쳐 준다. 중괄호가 없으면 그대로 1개.
#   여러 그룹/중첩도 재귀로 모두 펼친다. 예)
#     "*.{hwp,docx}"        → ["*.hwp","*.docx"]
#     "report_*.{doc,docx}" → ["report_*.doc","report_*.docx"]
#
# -in: pattern = 원본 glob 패턴(중괄호 그룹 포함 가능)
#
# -out: list = 펼쳐진 패턴 리스트(중복 제거, 최소 1개)
# -out: error = 없음
#------------------------------------------------------------------
def expand_glob_braces(pattern):
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    pre, post = pattern[:m.start()], pattern[m.end():]
    result = []
    for opt in m.group(1).split(","):
        # 남은 뒷부분에 또 다른 그룹이 있을 수 있어 재귀로 마저 펼친다.
        for expanded in expand_glob_braces(pre + opt.strip() + post):
            if expanded not in result:
                result.append(expanded)
    return result


#------------------------------------------------------------------
# glob 패턴 목록 펼치기 (--glob 진입점)
#=> --glob 한 값에 패턴을 여러 개 넣는 두 표기를 모두 받는다.
#     1) 콤마로 나열   : "*.hwp,*.pdf"   (기본·권장 — 접두가 달라도 됨)
#     2) 중괄호로 나열 : "*.{hwp,pdf}"    (셸 관습, 공통 접두/접미 간결)
#   먼저 "중괄호 밖" 최상위 콤마로 패턴을 나눈 뒤(중괄호 안 콤마는 그룹 구분자라
#   건드리지 않음), 각 토큰의 중괄호를 펼쳐 합친다. 두 표기를 섞어도 된다. 예)
#     "*.hwp,*.pdf"          → ["*.hwp","*.pdf"]
#     "*.{hwp,pdf},draft_*"  → ["*.hwp","*.pdf","draft_*"]
#
# -in: spec = --glob 원본 값(콤마·중괄호로 여러 패턴 가능)
#
# -out: list = 펼쳐진 개별 glob 패턴 리스트(중복 제거, 최소 1개)
# -out: error = 없음
#------------------------------------------------------------------
def expand_glob_patterns(spec):
    # 중괄호 깊이를 세며 최상위(깊이 0) 콤마에서만 자른다.
    tokens, buf, depth = [], [], 0
    for ch in spec:
        if ch == "{":
            depth += 1
            buf.append(ch)
        elif ch == "}":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "," and depth == 0:
            tokens.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    tokens.append("".join(buf))

    result = []
    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        for pat in expand_glob_braces(tok):
            if pat not in result:
                result.append(pat)
    return result


#------------------------------------------------------------------
# 파일 경로 목록 읽기 (--files-from)
#=> 여러 폴더에 흩어진 문서를 "한 프로세스"로 처리하려고 만든 입력 방식이다.
#   --dir 은 폴더 하나 아래만 훑을 수 있어서, 경로가 흩어져 있으면 파일마다
#   프로세스를 새로 띄우게 되고 그때마다 임베딩 모델을 다시 읽어 느려진다.
#   목록을 통째로 받으면 모델을 한 번만 읽고 전부 처리한다.
#    1) "-" 이면 표준입력, 아니면 그 파일을 UTF-8(BOM 허용)로 읽는다
#    2) 줄 앞뒤 공백을 떼고, 빈 줄과 "#" 으로 시작하는 주석 줄은 버린다
#    3) 실제 "파일"인 것만 남긴다(없는 경로·폴더는 건너뛰고 건수를 알린다)
#    4) 같은 경로가 여러 번 나오면 처음 것만 남긴다(입력 순서는 그대로 보존)
#
# -in: src = 목록 파일 경로. "-" 이면 표준입력에서 읽는다
#
# -out: files = 처리할 파일 경로 리스트(입력 순서 유지, 중복 제거)
# -out: error = 목록 파일 자체를 못 열면 OSError 를 그대로 올린다(상위에서 처리).
#               목록 "안"의 잘못된 경로는 예외 없이 건너뛰고 stderr 로 건수만 알린다
#------------------------------------------------------------------
def read_files_from(src):
    if src == "-":
        raw = sys.stdin.read()
    else:
        # 윈도우 메모장으로 저장한 목록에는 BOM 이 붙는다 — utf-8-sig 로 흡수한다.
        with open(src, "r", encoding="utf-8-sig", errors="replace") as fh:
            raw = fh.read()
    files, seen, skipped = [], set(), 0
    for line in raw.splitlines():
        # 경로에 공백이 있어 따옴표로 감싼 목록(dir /b 결과 등)도 받아 준다.
        path = line.strip().strip('"')
        if not path or path.startswith("#"):
            continue
        # 같은 문서를 두 번 임베딩하지 않도록 여기서 미리 중복을 접는다.
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        if os.path.isfile(path):
            files.append(path)
        else:
            skipped += 1
    # 조용히 버리면 "왜 결과 건수가 모자라지?" 로 이어진다 — 건수만이라도 남긴다.
    if skipped:
        print(f"[MpowerClassify] --files-from: 파일이 아니어서 건너뜀 {skipped}건",
              file=sys.stderr)
    return files


#------------------------------------------------------------------
# 처리 대상 파일 목록 수집
#=> --file 이면 그 파일 하나, --dir 이면 폴더에서 glob 패턴에 맞는 "파일"들을 모은다.
#   --dir 은 '항상 하위 폴더까지 재귀'가 기본이다(-r/--recursive 는 무시됨).
#    1) --glob 의 콤마·중괄호를 여러 패턴으로 펼친다(다중 확장자 지원)
#    2) <dir>/**/<glob> 을 recursive=True 로 확장(항상 하위폴더 포함)
#
# -in: args = argparse 결과(dir/glob)
#
# -out: files = 처리할 파일 경로 리스트(정렬·중복제거, 디렉터리는 제외)
# -out: error = --file/--dir 둘 다 없거나 대상이 없으면 빈 리스트(상위에서 처리)
#------------------------------------------------------------------
#------------------------------------------------------------------
# 문서 하나를 어떻게 꺼낼지 — 복호화 계획
#=> 엠파워 v11 암호화 문서용. 설계: plan/엠파워v11-암호화문서-복호화연동-설계-20261012.html
#
# -필드: info    = 열쇠와 모드(decrypt.DecInfo). problem 이 있으면 None
# -필드: problem = 풀 수 없는 사유(decrypt.DecError). 있으면 그 문서는 풀지도 읽지도 않고
#                  오류 레코드로 남긴다(열쇠 칸이 일부만 있거나 감싼 열쇠가 온 경우)
# -필드: name    = 원래 파일 이름(없을 수 있다)
# -필드: size    = 원래 크기(없을 수 있다)
#------------------------------------------------------------------
class _DecPlan:
    __slots__ = ("info", "problem", "name", "size")

    def __init__(self, info, problem, name, size):
        self.info = info
        self.problem = problem
        self.name = name
        self.size = size


#------------------------------------------------------------------
# 풀지 못한 문서 한 건 — 분류 루프에 '실패로 남겨라'고 알리는 표식
#
# -필드: label = 문서 경로(목록의 path 와 같은 표기)
# -필드: plan  = 그 문서의 복호화 계획(_DecPlan)
# -필드: err   = 실패 사유(decrypt.DecError)
# -필드: stored_size = 저장 파일(.SMF) 크기(바이트, 못 재면 0)
# -필드: elapsed_ms  = 이 문서를 다루기 시작해 실패할 때까지 걸린 시간(ms)
#------------------------------------------------------------------
class _DecFailed:
    __slots__ = ("label", "plan", "err", "stored_size", "elapsed_ms")

    def __init__(self, label, plan, err, stored_size=0, elapsed_ms=0):
        self.label = label
        self.plan = plan
        self.err = err
        self.stored_size = stored_size
        self.elapsed_ms = elapsed_ms


#------------------------------------------------------------------
# 복호화 실패 로그 한 줄 만들기
#=> 성공 줄과 같은 칸을 같은 차례로 싣는다 — 두 줄을 나란히 놓고 견줄 수 있게.
#    1) file · name · reason(사유 이름) · rc(도구 종료코드, 부르지 않았으면 "-")
#    2) enc_mode · zip_mode — 열쇠 칸이 잘못돼 해석하지 못했으면 "-"
#    3) org_size(DB 원본 크기, 받았을 때만) · stored_size · dec_size(결과 파일이 있었을 때만)
#    4) elapsed, 그리고 ' :: ' 뒤에 사람이 읽는 문장
#   임시 경로(tmp)는 싣지 않는다 — 실패한 결과물은 이미 지워졌다. 열쇠도 싣지 않는다.
#
# -in: item = _DecFailed
#
# -out: str = 로그에 남길 한 줄(앞의 시각·PID 는 로거가 붙인다)
# -out: error = 예외 없음
#------------------------------------------------------------------
def _dec_fail_line(item):
    plan, e = item.plan, item.err
    info = plan.info
    org = f"org_size={plan.size} " if plan.size is not None else ""
    dec = f"dec_size={e.dec_size} " if e.dec_size is not None else ""
    return (f"복호화 실패 file={item.label} name={plan.name or '-'} reason={e.reason} "
            f"rc={'-' if e.rc is None else e.rc} "
            f"enc_mode={info.enc_mode if info is not None else '-'} "
            f"zip_mode={info.zip_mode if info is not None else '-'} "
            f"{org}stored_size={item.stored_size} {dec}elapsed={item.elapsed_ms}ms "
            f":: {e.message}")


#------------------------------------------------------------------
# 이번 실행의 복호화 상태 — 계획·작업장·집계를 한데 묶는다
#
# -필드: plans  = {문서 경로: _DecPlan} — 아직 풀지 않은 문서들(풀 차례에 꺼낸다)
# -필드: sess   = decrypt.Session(임시 폴더와 도구)
# -필드: meta   = {문서 경로: (원래 이름, 도구를 실제로 불렀는가)} — 풀린 문서의 부속 정보
# -필드: n_dec  = 도구로 푼 문서 수 / n_fail = 풀지 못한 문서 수(요약 · 종료코드)
# -필드: extra  = 압축을 펼쳐 늘어난 작업 수(진행 표시의 분모 보정)
# -필드: arch_unexpanded = 못 펼친 압축 표(run_classify 의 것을 함께 쓴다)
#------------------------------------------------------------------
class _DecContext:
    def __init__(self, plans, sess):
        self.plans = plans
        self.sess = sess
        self.meta = {}
        self.n_dec = 0
        self.n_fail = 0
        self.extra = 0
        self.arch_unexpanded = {}


#------------------------------------------------------------------
# 명령줄로 받은 열쇠 세 칸 → 복호화 정보
#=> --mrk 가 없으면 환경변수 CSOCLASSIFY_MRK 를 본다(명령줄에 열쇠를 남기지 않는 길).
#   환경변수는 모드(--enc-mode·--zip-mode)를 명령줄로 줬을 때만 본다 — 안 그러면
#   남아 있던 환경변수 하나가 '목록에서 열쇠를 찾는 실행'을 '일부만 줌' 오류로 만든다.
#
# -in: args = 파싱된 인자
#
# -out: DecInfo 또는 None(명령줄에 열쇠 정보가 없다)
# -out: error = 일부만 줬거나 감싼 열쇠면 decrypt.DecError
#------------------------------------------------------------------
def _cli_dec_info(args):
    from . import decrypt
    modes_given = args.enc_mode is not None or args.zip_mode is not None
    mrk = args.mrk
    if mrk is None and args.decrypt and modes_given:
        mrk = os.environ.get("CSOCLASSIFY_MRK") or None
    return decrypt.parse_dec_info(mrk, args.enc_mode, args.zip_mode)


#------------------------------------------------------------------
# 명령줄로 받은 원래 크기(--size) → 정수
#=> 목록의 size 칸과 같은 뜻이다. 0 이상의 정수만 받는다(앞뒤 공백은 무시).
#
# -in: args = 파싱된 인자
#
# -out: int 또는 None = 원래 크기(바이트). --size 를 안 줬으면 None(크기 검사 건너뜀)
# -out: error = 숫자가 아니거나 음수면 ValueError
#------------------------------------------------------------------
def _cli_dec_size(args):
    raw = getattr(args, "dec_size", None)
    if raw is None:
        return None
    s = str(raw).strip()
    # "1,024" · "-1" · "1.5" 같은 값은 받지 않는다 — 크기를 잘못 주면 멀쩡한 문서가 실패로 남는다.
    if not s.isdigit():
        raise ValueError(f"--size 는 0 이상의 정수(바이트)여야 합니다: {raw}")
    return int(s)


#------------------------------------------------------------------
# 사이냅 관련 인자 검사 — 문서를 읽기 전에 끝낸다
#=> 설계: plan/Rust-사이냅추출기-옵션-설계-20261019.html §6. Rust 판 handle_extractor_args 와 같다.
#    1) --synap-path 를 주면 이번 실행 동안 그 경로를 쓴다
#    2) --synap-fallback 과 --synap-only 를 함께 주면 1006 mode_conflict
#    3) 사이냅을 명시해 골랐으면(--synap-fallback · --synap-only · CSOCLASSIFY_HYBRID=0)
#       snf_exe 가 있어야 한다 — 없거나 실행할 수 없으면 1016 synap_missing.
#       명시하지 않은 기본 실행은 지금처럼 '있으면 폴백, 없으면 자체 파서만'이다
#
# -in: args = 파싱된 인자
#
# -out: None = 문제 없음 / int = 종료코드(이미 오류를 알렸다 — 그대로 return 한다)
# -out: error = 예외 없음
#------------------------------------------------------------------
def check_synap_args(args):
    from . import resources
    if getattr(args, "synap_path", None):
        resources.set_synap_override(args.synap_path)
    synap_only = not getattr(args, "hybridparse", True)
    fallback = getattr(args, "synap_fallback", False)
    if synap_only and fallback:
        return fail_err("mode_conflict",
                        "[MpowerClassify] --synap-fallback 과 --synap-only 는 함께 쓸 수 없습니다 — 하나만 고르세요.")
    if not (synap_only or fallback):
        return None
    exe = resources.synap_exe_path()
    if not os.path.isfile(exe):
        tried = " , ".join(resources.synap_search_paths())
        return fail_err("synap_missing",
                        f"[MpowerClassify] 사이냅 실행파일({resources.synap_exe_name()})을 찾을 수 없습니다 — "
                        f"찾아본 곳: {tried} (--synap-path 또는 환경변수 CSOCLASSIFY_SYNAP_DIR 로 알려 주세요)", exe)
    # 리눅스는 실행 비트가 있어야 한다(윈도우는 늘 참이다).
    if os.name != "nt" and not os.access(exe, os.X_OK):
        return fail_err("synap_missing",
                        f"[MpowerClassify] 사이냅 실행파일에 실행 권한이 없습니다: {exe} (chmod +x)", exe)
    return None


#------------------------------------------------------------------
# 등급 스트림 인자 검사 — 문서를 읽기 전에 끝낸다
#=> 설계: plan/등급스트림-기록-옵션-설계-20261019.html §03 · §08. Rust 판 check_grade_stream_args 와 같다.
#    1) 보조 옵션(--grade-stream-unclassified · --grade-stream-update-time)만 주면 1003
#       (켜는 옵션 없이 무시된 채 도는 일을 막는다)
#    2) --grade-stream-unclassified 값은 skip · none 만 — 그 밖이면 1003
#    3) 분류 결과를 내지 않는 모드(--text-only · --embed · --seed-add · 데몬 명령 ·
#       --build-doc-rule · --suggest-terms)와 함께면 1006
#    4) Windows 가 아니면(ADS 없음) 1012
#
# -in: args = 파싱된 인자
#
# -out: None = 문제 없음 / int = 종료코드(이미 오류를 알렸다 — 그대로 return 한다)
# -out: error = 예외 없음
#------------------------------------------------------------------
def check_grade_stream_args(args):
    from . import gradestream, seedcli
    on = getattr(args, "write_grade_stream", False)
    uncl = getattr(args, "grade_stream_unclassified", None)
    upd = getattr(args, "grade_stream_update_time", False)
    if not on:
        if uncl is not None or upd:
            return fail_err("bad_args",
                            "[MpowerClassify] --grade-stream-unclassified · --grade-stream-update-time 은 "
                            "--write-grade-stream 과 함께 줘야 합니다.")
        return None
    if uncl is not None and uncl not in ("skip", "none"):
        return fail_err("bad_args",
                        f"[MpowerClassify] --grade-stream-unclassified 값은 skip 또는 none 입니다: {uncl!r}")
    # 분류 결과가 없는 모드에서는 쓸 등급이 없다 — 조용히 무시하지 않고 멈춘다
    modes = [("--text-only", getattr(args, "text_only", False)),
             ("--embed", getattr(args, "embed", False)),
             ("--seed-add", seedcli.is_seed_add(args)),
             ("--serve", getattr(args, "serve", False)),
             ("--status", getattr(args, "status", False)),
             ("--stop", getattr(args, "stop", False)),
             ("--build-doc-rule", getattr(args, "build_doc_rule", False)),
             ("--seed-fill-title", getattr(args, "seed_fill_title", False)),
             ("--suggest-terms", bool(getattr(args, "suggest_terms", None)))]
    for name, used in modes:
        if used:
            return fail_err("mode_conflict",
                            f"[MpowerClassify] --write-grade-stream 은 {name} 와 함께 쓸 수 없습니다 — "
                            f"분류 결과를 내는 실행에서만 등급을 씁니다.")
    if not gradestream.supported():
        return fail_err("unsupported_option",
                        "[MpowerClassify] --write-grade-stream 은 Windows(NTFS)에서만 쓸 수 있습니다 — "
                        "이 플랫폼에는 대체 데이터 스트림이 없습니다.")
    return None


#------------------------------------------------------------------
# 사람 등급 지정 인자 검사 — 문서를 읽기 전에 끝낸다
#=> 설계: plan/사람등급지정-수동오버라이드-설계-20261020.html §08. Rust 판 check_override_args 와 같다.
#    1) --set-grade 값은 C · S · O · NONE 만 — 그 밖이면 1003
#    2) --set-grade 는 --file 하나에만. --dir 이면 1006(폴더 전체를 한 등급으로 찍는 실수 방지),
#       대상이 없으면 1003. 분류 결과를 내지 않는 모드와 함께면 1006
#    3) --set-grade 결정을 목록에 덧붙이려면(--grade-overrides) --reviewer · --reason 필수(1003)
#    4) --reviewer · --reason 만 따로 주면 1003(쓰이지 않은 채 도는 일 방지)
#    5) 분류에 --grade-overrides 를 주면 그 파일이 있어야 한다 — 없으면 2009
#
# -in: args = 파싱된 인자
#
# -out: None = 문제 없음 / int = 종료코드(이미 오류를 알렸다)
# -out: error = 예외 없음
#------------------------------------------------------------------
def check_override_args(args):
    from . import seedcli
    sg = getattr(args, "set_grade", None)
    ov = getattr(args, "grade_overrides", None)
    who = getattr(args, "reviewer", None)
    why = getattr(args, "reason", None)
    if sg is not None:
        if str(sg).strip().upper() not in ("C", "S", "O", "NONE"):
            return fail_err("bad_args", f"[MpowerClassify] --set-grade 값은 C · S · O · NONE 입니다: {sg!r}")
        if getattr(args, "dir", None):
            return fail_err("mode_conflict",
                            "[MpowerClassify] --set-grade 는 --dir 과 함께 쓸 수 없습니다 — 폴더 전체를 한 "
                            "등급으로 정하는 실수를 막으려는 것입니다. 문서는 --file 로 하나씩 주세요.")
        modes = [("--text-only", getattr(args, "text_only", False)),
                 ("--embed", getattr(args, "embed", False)),
                 ("--seed-add", seedcli.is_seed_add(args)),
                 ("--serve", getattr(args, "serve", False)),
                 ("--status", getattr(args, "status", False)),
                 ("--stop", getattr(args, "stop", False))]
        for name, used in modes:
            if used:
                return fail_err("mode_conflict",
                                f"[MpowerClassify] --set-grade 는 {name} 와 함께 쓸 수 없습니다.")
        if not getattr(args, "file", None):
            return fail_err("bad_args", "[MpowerClassify] --set-grade 는 --file 로 대상 문서를 줘야 합니다.")
        if ov and not ((who or "").strip() and (why or "").strip()):
            return fail_err("bad_args",
                            "[MpowerClassify] --set-grade 결정을 --grade-overrides 에 덧붙이려면 "
                            "--reviewer 와 --reason 을 함께 줘야 합니다(감사 기록).")
        return None
    if who is not None or why is not None:
        return fail_err("bad_args",
                        "[MpowerClassify] --reviewer · --reason 은 --set-grade 와 --grade-overrides 를 "
                        "함께 줄 때만 쓰입니다.")
    if ov and not os.path.isfile(ov):
        return fail_err("grade_overrides_missing",
                        f"[MpowerClassify] 사람 결정 목록 파일이 없습니다: {ov}", ov)
    return None


#------------------------------------------------------------------
# --set-grade — 문서 하나에 등급을 바로 정한다
#=> 설계 §04. 문서를 읽거나 판정하지 않는다(암호화 문서도 풀지 않는다).
#    1) 대상이 없으면 file_missing 레코드 + 3004(종료 1)
#    2) 내용 지문을 구하고, 결과 레코드(method:manual_override)를 만든다
#    3) --write-grade-stream 이면 그 등급을 파일 스트림에 쓴다(NONE 은 '미분류' 로)
#    4) --grade-overrides 면 결정을 한 줄 덧붙인다 — 못 쓰면 2010(종료 4)
#
# -in: args   = 파싱된 인자
# -in: out_fp = 결과를 쓸 파일(None 이면 표준출력)
#
# -out: int = 종료코드
# -out: error = 예외 없음(실패는 오류코드로 알린다)
#------------------------------------------------------------------
def run_set_grade(args, out_fp):
    import datetime
    from . import gradeoverride, gradestream
    from .classify import now_iso
    # 이 함수는 main 밖에서 불려 main 의 log 를 못 본다 — 같은 이름의 로거를 직접 얻는다
    log = logsetup.get_logger("csoclassify")
    path = args.file
    grade = gradeoverride.norm_grade(args.set_grade)       # NONE → None
    label = grade or "NONE"
    code = config.EXIT_OK
    # 상태 줄을 결과 파일에 함께 넣을 때만(--out + --json-errors) json 이 배열이 된다 — Rust 판과 같다
    writer = output.RecordWriter(out_fp or sys.stdout, args.fmt,
                                 multi=bool(errcodes.is_json() and args.out))
    if not os.path.isfile(path):
        rec = {"file": path, "grade": None,
               "error": {"kind": "file_missing", "reason": "file_missing",
                         "message": "파일이 없습니다"},
               "meta": {"ts": now_iso()}}
        errcodes.set_status("file_missing", f"[MpowerClassify] 파일이 없습니다: {path}")
        errcodes.set_counts(1, 1)
        writer.write_record(_simple_record(rec) if getattr(args, "simple", False) else _order_record(rec))
        if errcodes.is_json() and args.out and args.fmt in ("json", "jsonl"):
            writer.write_record(errcodes.status_object(errcodes.exit_of("file_missing"), path))
        writer.close()
        return errcodes.exit_of("file_missing")
    doc_hash = _file_hash(path)
    info = {"reviewer": getattr(args, "reviewer", None), "reason": getattr(args, "reason", None),
            "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "matched_by": "set-grade", "kind": "change", "applied": True}
    rec = {"file": path, "hash": doc_hash, "grade": grade,
           "why": {"security": {"method": "manual_override", "decided_by": ["reviewer"],
                                "override": info}},
           "meta": {"ts": now_iso()}}
    # 결정 목록에 남길 '이전 등급' — 지금 파일에 써 있는 값(모르면 None)
    old = None
    if gradestream.supported():
        st, cur, _w = gradestream.read_state(path)
        if st in (gradestream.ST_MARKED, gradestream.ST_UNCLASSIFIED):
            old = gradestream.CODE_GRADE.get(cur)
            old = None if old == "NONE" else old
    if getattr(args, "write_grade_stream", False):
        res = gradestream.mark(path, label,
                               keep_time=not getattr(args, "grade_stream_update_time", False))
        rec["grade_stream"] = res
        log.info("등급스트림 state=%s file=%s value=%s", res.get("state"), path, label)
        if res.get("state") == "failed":
            errcodes.set_status("grade_stream_write_failed",
                                f"[MpowerClassify] 등급 스트림을 쓰지 못했습니다: {res.get('reason')}")
            code = errcodes.exit_of("grade_stream_write_failed")
    ov = getattr(args, "grade_overrides", None)
    if ov:
        try:
            gradeoverride.append_entry(ov, path, old, grade, args.reason, args.reviewer, doc_hash)
        except OSError as e:
            errcodes.set_status("grade_overrides_write_failed",
                                f"[MpowerClassify] 사람 결정 목록에 쓰지 못했습니다: {ov} ({e})")
            code = errcodes.exit_of("grade_overrides_write_failed")
    log.info("사람등급 지정 file=%s grade=%s 목록=%s", path, label, ov or "-")
    errcodes.set_counts(1, 0)
    writer.write_record(_simple_record(rec) if getattr(args, "simple", False) else _order_record(rec))
    if errcodes.is_json() and args.out and args.fmt in ("json", "jsonl"):
        writer.write_record(errcodes.status_object(code, path))
    writer.close()
    print(f"[MpowerClassify] 사람 등급 지정: {path} → {label}"
          + (f" (파일 스트림 {rec['grade_stream'].get('state')})" if "grade_stream" in rec else "")
          + (f" · 결정 목록 {ov}" if ov else ""), file=sys.stderr)
    return code


#------------------------------------------------------------------
# --decrypt 관련 인자 조합 검사 — 문서를 읽기 전에 끝낸다
#=> "풀릴 줄 알았는데 안 풀린" 상태를 만들지 않는다(설계 §4 표). Rust 판 check_decrypt_args 와 같다.
#    1) 열쇠·이름만 주고 --decrypt 를 안 주면 오류
#    2) --decrypt 와 --no-decrypt 는 함께 못 쓴다
#    3) --decrypt 는 --file 한 건에만 쓴다(폴더에는 파일마다 다른 열쇠를 줄 방법이 없다)
#    4) --decrypt 면 열쇠 셋이 있어야 한다(없으면 --filelist 에서 찾는다)
#
# -in: args = 파싱된 인자
#
# -out: None = 문제 없음 / int = 종료코드(이미 오류를 알렸다 — 그대로 return 한다)
# -out: error = 예외 없음
#------------------------------------------------------------------
def check_decrypt_args(args):
    from . import decrypt

    def bad(msg):
        return fail_err("decrypt_args_invalid", f"[MpowerClassify] {msg}")

    has_keys = (args.mrk is not None or args.enc_mode is not None
                or args.zip_mode is not None)
    if not args.decrypt:
        if has_keys or args.dec_name is not None or args.dec_size is not None:
            return bad("--mrk · --enc-mode · --zip-mode · --name · --size 는 --decrypt 와 함께 써야 합니다.\n"
                       "  풀어서 분류하려면 --decrypt 를 붙이고, 이미 풀어 둔 파일이면 이 인자들을 빼세요.")
        return None
    if args.no_decrypt:
        return bad("--decrypt 와 --no-decrypt 는 함께 쓸 수 없습니다.")
    if not args.file:
        return bad("--decrypt 는 --file 한 건에만 씁니다. 여러 파일은 --filelist 의 각 줄에 "
                   "mrk · enc_mode · zip_mode 를 적어 주세요(파일마다 열쇠가 다릅니다).")
    try:
        info = _cli_dec_info(args)
    except decrypt.DecError as e:
        return bad(e.message)
    # --size 는 문서를 읽기 전에 모양을 본다 — 잘못된 값으로 돌면 그 문서가 size_mismatch 로 남는다.
    try:
        _cli_dec_size(args)
    except ValueError as e:
        return bad(str(e))
    # 명령줄에 열쇠가 없으면 목록에서 찾는다 — 목록도 없으면 풀 방법이 없다.
    # (--text-only 는 Rust 판이 목록을 읽지 않으므로, 두 판을 맞춰 명령줄에 있어야 한다.)
    if info is None and not (getattr(args, "filelist", None) and not args.text_only):
        return bad("--decrypt 에는 --mrk · --enc-mode · --zip-mode 가 필요합니다"
                   "(--mrk 는 환경변수 CSOCLASSIFY_MRK 로도 줄 수 있습니다).")
    return None


#------------------------------------------------------------------
# 이 문서의 복호화 계획 찾기
#=> 명령줄(--decrypt) → 목록(--filelist)의 그 줄 순으로 본다.
#    1) --no-decrypt 면 언제나 None(그대로 읽는다)
#    2) --decrypt 면 명령줄 값이 우선, 없으면 목록의 그 줄
#    3) 그 밖에는 목록의 그 줄에 열쇠 칸이 있을 때만
#
# -in: args = 파싱된 인자(_filelist 에 읽어 둔 목록이 실려 있다)
# -in: path = 문서 경로(목록의 path 와 같은 표기)
#
# -out: _DecPlan = 풀어야(또는 오류로 남겨야) 하는 문서 / None = 그대로 읽는 문서
# -out: error = 예외 없음
#------------------------------------------------------------------
def _dec_plan_for(args, path):
    from . import decrypt
    if getattr(args, "no_decrypt", False):
        return None
    fl = getattr(args, "_filelist", None)
    entry = fl.lookup(path)[0] if fl is not None else None
    if getattr(args, "decrypt", False):
        name = args.dec_name or (entry.name if entry is not None else None)
        # 크기도 이름처럼 명령줄(--size)이 우선, 없으면 목록의 size 칸. 둘 다 없으면 None → 크기 검사 건너뜀.
        # 값 모양은 check_decrypt_args 가 이미 봤다.
        size = _cli_dec_size(args)
        if size is None:
            size = entry.size if entry is not None else None
        try:
            info = _cli_dec_info(args)
        except decrypt.DecError as e:
            return _DecPlan(None, e, name, size)
        if info is not None:
            return _DecPlan(info, None, name, size)
        if entry is not None and entry.dec is not None:
            return _DecPlan(entry.dec, None, name, size)
        if entry is not None and entry.dec_problem is not None:
            return _DecPlan(None, entry.dec_problem, name, size)
        return _DecPlan(None, decrypt.DecError(
            "info_incomplete",
            "--decrypt 를 줬지만 명령줄에도 목록에도 이 파일의 열쇠가 없습니다"), name, size)
    if entry is None:
        return None
    if entry.dec_problem is not None:
        return _DecPlan(None, entry.dec_problem, entry.name, entry.size)
    if entry.dec is not None:
        return _DecPlan(entry.dec, None, entry.name, entry.size)
    return None


#------------------------------------------------------------------
# 작업 목록을 돌면서 암호화 문서를 '차례가 왔을 때' 푼다 (제너레이터)
#=> "한 번에 한 건만 풀려 있다"(설계 §3)를 지키는 자리다. 미리 다 풀어 두면 평문
#   수천 건이 한꺼번에 디스크에 놓이고, 중간에 죽으면 그대로 남는다.
#    1) 계획이 없는 작업은 그대로 내보낸다(지금까지와 같다)
#    2) 계획이 있으면 풀고, 압축파일이면 그 문서 전용 폴더 안에 내부 문서로 펼친다
#       (순서는 복호화 → 펼치기 → 분류. zip_mode 는 도구가 이미 풀었고, 여기서 펼치는
#        것은 사용자가 올린 zip·7z 같은 '문서로서의 압축'이다)
#    3) 라벨에서 임시 경로를 원래 경로로 되돌린다 — 임시 경로는 결과·로그에 나가면 안 된다
#    4) 그 문서에서 나온 작업을 다 내보낸 뒤(= 부른 쪽이 다음 것을 달라고 할 때) 평문을 지운다
#    5) 못 풀면 _DecFailed 를 내보낸다. 환경 문제(fatal)는 예외로 올려 실행을 멈춘다
#    6) 풀리면 '복호화 성공' 로그 한 줄(원래 경로·이름·도구 사용·enc_mode·zip_mode·
#       DB 원본 크기(org_size — --size·목록 size 가 있을 때만)·저장 파일 크기(stored_size)·
#       풀린 크기(dec_size)·임시 파일 경로·걸린 시간)을 남긴다.
#       열쇠는 남기지 않는다
#
# -in: jobs = 작업 목록(경로 문자열 또는 (src, label, origin) 튜플)
# -in: ctx  = _DecContext(없으면 jobs 를 그대로 흘린다)
#
# -out: 작업 하나씩(튜플·문자열) 또는 _DecFailed
# -out: error = 환경 문제면 decrypt.DecError(fatal=True) 전파
#------------------------------------------------------------------
def _iter_dec_jobs(jobs, ctx):
    if ctx is None:
        yield from jobs
        return
    import time
    from . import decrypt
    from .extract.archive import expand_paths
    # 문서별 복호화 성공 줄을 일반 로그(class_YYYYMMDD.log)에 남길 로거.
    log = logsetup.get_logger("csoclassify.cli")
    for item in jobs:
        src, label, origin = _as_job(item)
        plan = ctx.plans.pop(label, None) if origin is None else None
        if plan is None:
            yield item
            continue
        # 걸린 시간과 저장 파일 크기는 성공·실패 로그 모두에 싣는다 — 그래서 try 앞에서 잰다.
        t_dec = time.perf_counter()
        # 풀기 전에 크기를 본다 — 어차피 풀린 뒤 상한에서 버려질 파일은 풀지도 않는다(설계 §9).
        # 풀린 뒤의 실제 크기는 추출기·압축 펼치기의 상한이 지금까지처럼 다시 본다.
        try:
            src_len = os.path.getsize(src)
        except OSError:
            src_len = 0
        try:
            if plan.problem is not None:
                # 열쇠 칸이 일부만 있거나 감싼 열쇠 — 도구를 부르지 않고 오류로 남긴다.
                raise decrypt.DecError(plan.problem.reason, plan.problem.message)
            # 앞 상한은 '풀린 뒤 실제로 적용될 상한'과 같게 고른다 — 원래 이름의 확장자로
            # 압축·텍스트·일반을 가르고, 원래 크기가 있으면 그 크기로 본다
            # (plan/엠파워v11-복호화-크기상한-정합-설계-20261016.html). --no-size-limit 이면 통과.
            too_big = decrypt.pre_size_limit(plan.name, plan.size, src_len)
            if too_big is not None:
                raise too_big
            # decrypt.py => decrypt 함수 호출(removenc.sh 호출)
            d = ctx.sess.decrypt(src, plan.info, plan.name, plan.size)
        except decrypt.DecError as e:
            if e.fatal:
                raise
            # 실패 로그도 성공 줄과 같은 칸을 싣도록 저장 크기·걸린 시간을 함께 넘긴다.
            yield _DecFailed(label, plan, e, src_len,
                             int((time.perf_counter() - t_dec) * 1000))
            continue
        # 성공도 문서마다 한 줄 남긴다 — 어느 문서를 어떤 모드로 어디에 풀었는지 나중에 추적한다.
        # 열쇠(mrk)는 남기지 않는다. keep_tmp=True 는 임시 경로 가림 필터를 건너뛰게 하는 표식이다.
        elapsed_ms = int((time.perf_counter() - t_dec) * 1000)
        # stored_size = 엠파워 저장소의 .SMF 파일 크기(암호화 여부와 상관없이, 풀기 전에 잰 값),
        # dec_size = 복호화(또는 복사) 뒤 실제 문서 크기. 둘을 견주면 압축(zip_mode)으로 얼마나
        # 줄었는지, 풀린 결과가 터무니없지 않은지 로그만 보고 안다.
        # (목록·--size 의 size 는 DB 원본 크기라 뜻이 달라 이름을 겹치지 않게 했다)
        try:
            dec_size = os.path.getsize(d.file)
        except OSError:
            dec_size = -1
        # org_size = 엠파워 DB 의 원본 크기(--size 또는 목록의 size 칸). 받은 값이 있을 때만 싣는다 —
        # 성공 줄이면 verify 가 dec_size 와 같은지 이미 봤으므로, 세 값이 한 줄에 나란히 남는다.
        org_note = f"org_size={plan.size} " if plan.size is not None else ""
        log.info("복호화 성공 file=%s name=%s tool=%s enc_mode=%s zip_mode=%s %sstored_size=%d "
                 "dec_size=%d tmp=%s elapsed=%dms",
                 label, plan.name or "-", "yes" if d.tool else "no(copy)",
                 plan.info.enc_mode, plan.info.zip_mode, org_note, src_len, dec_size, d.file,
                 elapsed_ms, extra={"keep_tmp": True})
        if d.tool:
            ctx.n_dec += 1
        ctx.sess.remember(d, label)
        xdir = os.path.join(d.dir, "x")
        os.makedirs(xdir, exist_ok=True)
        st = {}
        inner = expand_paths([d.file], xdir, stats=st)

        # 임시 경로로 시작하는 라벨을 원래 경로로 되돌린다.
        def fix(s, _tmp=d.file, _label=label):
            if s is None:
                return None
            if s == _tmp:
                return _label
            if s.startswith(_tmp) and s[len(_tmp):len(_tmp) + 1] in ("/", "\\"):
                return _label + s[len(_tmp):]
            return s

        inner = [(j[0], fix(j[1]), fix(j[2])) for j in (_as_job(x) for x in inner)]
        for row in st.get("unexpanded", []):
            row["path"] = fix(row["path"])
            ctx.arch_unexpanded[row["path"]] = (row["reason"], row.get("kind", "limit"))
        ctx.meta[label] = (plan.name, d.tool)
        ctx.extra += len(inner) - 1
        try:
            for j in inner:
                yield j
        finally:
            # 이 문서에서 나온 작업이 모두 끝났다(또는 중단됐다) — 평문을 지운다.
            ctx.sess.release(d.dir)


#------------------------------------------------------------------
# 풀린 문서의 레코드에 부속 정보 달기
#=> 신호는 원래 이름으로 봤지만 결과의 file 은 부른 쪽이 준 경로 그대로여야 한다
#   (목록과 결과를 잇는 키다). 원래 이름과 '풀어서 읽었다'는 사실도 남긴다.
#   열쇠는 남기지 않는다.
#
# -in: rec  = 결과 레코드(제자리에서 고친다)
# -in: path = 문서 경로(결과의 file 칸에 들어갈 값)
# -in: dm   = (원래 이름, 도구를 불렀는가) 또는 None(엠파워 문서가 아님)
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def _apply_dec_meta(rec, path, dm):
    if not dm:
        return
    name, tool = dm
    rec["file"] = path
    if name:
        rec["name"] = name
    if tool:
        rec.setdefault("meta", {})["decrypted"] = True


#------------------------------------------------------------------
# 복호화 실패 레코드
#=> 풀지 못했거나 풀린 결과가 수상한 문서를 결과에 한 줄로 남긴다.
#   암호문을 "그래도 분류해 보자"로 넘기지 않는다 — 등급이 나와 버리면 그것이 정상
#   결과처럼 DB 에 들어간다. 실패는 언제나 grade=None + error 다(설계 §7).
#    1) 등급 없는 레코드를 직접 만든다(파일명 신호로도 등급을 매기지 않는다)
#    2) error 는 다른 실패 레코드와 같은 틀 — kind 로 가르고(기계), reason 은 사람이 읽는 문장.
#       detail 은 세부 사유 이름, rc 는 removeenc 종료코드(안 불렀으면 None)
#    3) 업무분류 축이 켜져 있으면 '축은 돌았다'를 남긴다(화면이 "분류 안 됨"으로 세게)
#
# -in: path     = 문서 경로
# -in: name     = 원래 파일 이름(없으면 None)
# -in: err      = decrypt.DecError
# -in: kind     = "decrypt_failed" 또는 "size_limit"
# -in: ruleset · doc_rules · taxonomy = 업무분류 축 조립용(분류 때와 같은 값)
#
# -out: dict = 결과 레코드
# -out: error = 없음
#------------------------------------------------------------------
def _decrypt_failed_record(path, name, err, kind, ruleset, doc_rules=None, taxonomy=None):
    from .classify import now_iso
    rec = {
        "file": path,
        "grade": None,
        "why": {"security": {"confidence": 0.0, "method": "decrypt_failed",
                             "decided_by": [], "seed_eligible": False, "signals": {}}},
        "error": {"stage": "decrypt", "kind": kind, "detail": err.reason,
                  "rc": err.rc, "reason": err.message},
        "meta": {"ts": now_iso()},
    }
    if name:
        rec["name"] = name
    if doc_rules is not None and taxonomy is not None:
        # 빈 본문으로 축만 돌려 그 결과를 옮겨 온다(보안등급 쪽은 버린다).
        tmp = _extract_failed_record(name or path, err.message, ruleset, doc_rules, taxonomy)
        dt = (tmp.get("why") or {}).get("doctype")
        if dt is not None:
            rec["why"]["doctype"] = dt
    return rec


def collect_files(args):
    # 목록 입력이 있으면 그것이 대상이다(--file/--dir 과의 동시 사용은 호출부에서 막는다).
    if getattr(args, "files_from", None):
        return read_files_from(args.files_from)
    if args.file:
        # 실제 파일일 때만 대상으로 삼는다. 없는 경로·폴더를 그대로 넘기면
        # '추출 실패' 레코드가 만들어져 결과처럼 나간다 — 아래 no_input 으로
        # 걸러 부른 쪽에 "대상이 없다"고 알린다.
        return [args.file] if os.path.isfile(args.file) else []
    if args.dir:
        found = []
        # 콤마/중괄호로 패턴 여러 개를 준 경우 패턴별로 각각 매칭해 합친다.
        # *.hwp,*.pdf 식으로 추가.
        for gp in expand_glob_patterns(args.glob):
            # --dir 은 항상 '**' 로 하위 폴더까지 재귀 탐색한다(-r 여부와 무관).
            found.extend(glob.glob(os.path.join(args.dir, "**", gp), recursive=True))
        # 매칭 결과 중 실제 파일만(폴더 제외) 대상으로, 중복 제거해 정렬한다
        # (여러 패턴에 동시에 걸린 파일이 있을 수 있음).
        return sorted({f for f in found if os.path.isfile(f)})
    # --file/--dir/--files-from 이 하나도 없고 목록만 준 경우 — 목록이 대상을 정한다
    # (설계 §7-5-2-1 "정식 운영": MpowerV11 이 '이번에 분류할 문서'를 고른다).
    # 반대로 --dir 과 함께 주면 위에서 이미 돌아갔다 — 그때 목록은 'ID 사전' 역할만 한다.
    flist = getattr(args, "_filelist", None)
    if flist is not None:
        # 목록이 대상을 정한 실행에서만 '없는 파일'을 결과에 남긴다. --dir 과 함께
        # 준 경우에는 목록이 ID 사전일 뿐이라, 그 바깥 항목을 빠졌다고 하면 안 된다.
        args._filelist_is_target = True
        return flist.target_paths()
    return []


#------------------------------------------------------------------
# 처리 작업 정규화 — 경로 or 튜플 → (src, label, origin)
#=> 파일 목록의 각 원소는 '읽을 경로 문자열'이거나, 압축 확장으로 생긴
#   (읽기용 실제경로 src, 표시·신호용 가상경로 label, 최상위압축 origin) 튜플이다.
#   어느 쪽이든 (src, label, origin) 로 통일해 준다.
#   (일반 파일은 src==label, origin=None 이라 예전 동작과 100% 동일하다.)
#
# -in: item = 경로 문자열 또는 (src,label) / (src,label,origin) 튜플
#
# -out: (src, label, origin) = 읽기용 경로 / 표시·신호용 경로 / 최상위 압축경로(없으면 None)
# -out: error = 없음
#------------------------------------------------------------------
def _as_job(item):
    if isinstance(item, tuple):
        if len(item) == 3:
            src, label, origin = item
            return (src, safe_text(label), safe_text(origin))
        # (src, label) 형태는 origin 없음으로 채운다.
        return (item[0], safe_text(item[1]), None)
    # 읽기용(src)은 원본 그대로 둔다 — 손대면 파일을 못 연다. 표시·기록용만 고친다.
    return (item, safe_text(item), None)


#------------------------------------------------------------------
# 결과에 실을 문자열을 '안전하게' 만들기 (파일명이 UTF-8 이 아닐 때)
#=> 리눅스에서 파일 이름은 그냥 바이트열이라 UTF-8 이 아닐 수 있다(윈도우에서
#   만든 CP949 이름을 그대로 복사한 경우 등). 파이썬은 그런 이름을 읽을 때
#   surrogateescape 로 '짝 없는 대리 문자'(\udcXX)를 끼워 넣는데, 이 문자는
#   UTF-8 로 다시 인코딩할 수 없어서 json.dumps 가 예외를 던진다.
#   그대로 두면 그런 파일 하나 때문에 배치 전체가 중단되고, 그때까지 분류한
#   결과까지 통째로 날아간다 — 한 파일의 이름 문제로 잃을 만한 것이 아니다.
#   그래서 결과·로그에 실을 때만 그 자리를 U+FFFD 로 바꾼다.
#   [주의] 파일을 여는 데 쓰는 경로에는 절대 쓰지 말 것 — 바꾼 이름으로는
#   파일을 못 연다. '읽기용 경로'와 '표시·기록용 경로'를 나눠 쓰는 이유다.
#
# -in: s = 임의 문자열(경로 등). None 이면 그대로 None 을 돌려준다
#
# -out: str|None = UTF-8 로 인코딩 가능한 문자열(문제 없으면 원본 그대로)
# -out: error = 없음
#------------------------------------------------------------------
def safe_text(s):
    # 실제 처리는 output.scrub_surrogates 한 곳에 모아 뒀다.
    #=> 예전에는 여기서 surrogateescape 만 썼는데, 그 방법은 U+DC80~U+DCFF 밖의
    #   대리 문자(깨진 문서에서 반쪽만 남은 UTF-16 대리쌍 등)를 만나면 **다시 예외를
    #   낸다.** 파일 이름뿐 아니라 본문에도 그런 글자가 들어오면서 실제로 배치가
    #   통째로 죽었다. 두 경우를 모두 막는 판이 output 쪽에 있으므로 그것을 쓴다.
    return output.scrub_surrogates(s)


# 등급 심각도(집계용): C > S > O > 미분류. 압축파일 '자체' 등급은 내부 최고 위험으로 정한다.
_GRADE_SEVERITY = {"C": 3, "S": 2, "O": 1, "none": 0, None: 0}


#------------------------------------------------------------------
# 여러 등급 중 최고 위험 등급 고르기
#=> 압축파일 안 파일들의 등급 목록에서 가장 위험한 등급(C>S>O>미분류)을 고른다.
#
# -in: grades = 등급 문자열/None 의 리스트
# -out: str|None = 최고 위험 등급(전부 미분류면 None)
# -out: error = 없음
#------------------------------------------------------------------
def _worst_grade(grades):
    best, best_sev = None, -1
    for g in grades:
        sev = _GRADE_SEVERITY.get(g, 0)
        if sev > best_sev:
            best, best_sev = g, sev
    return best


#------------------------------------------------------------------
# 압축파일 '자체'의 집계 등급 레코드 만들기
#=> 내부 파일들의 등급을 모아, 압축파일 자체에 '최고 위험 등급'을 부여한 요약 레코드를
#   만든다. contains 에 내부 등급 분포를 담아 어느 위험이 몇 건인지 보이게 한다.
#
# -in: origin = 최상위 압축파일 경로
# -in: grades = 그 압축에 속한 내부 파일들의 최종 등급 리스트
# -in: ts     = 타임스탬프 문자열
#
# -out: dict = 압축 집계 레코드(archive=True 로 표시)
# -out: error = 없음
#------------------------------------------------------------------
def _archive_record(origin, grades, ts, doctypes=None):
    from collections import Counter
    dist = Counter(g if g in ("C", "S", "O") else "none" for g in grades)
    worst = _worst_grade(grades)
    rec = {
        "file": origin,
        "archive": True,                 # 이 레코드는 '압축파일 자체'의 집계임을 표시
        "grade": worst,
        "why": {"security": {"method": "archive_rollup", "decided_by": ["archive"]}},
        # 내부 등급 분포(어느 위험이 몇 건인지) — 압축만 봐도 위험도를 파악.
        "contains": {"total": len(grades), "C": dist["C"], "S": dist["S"],
                     "O": dist["O"], "unclassified": dist["none"]},
        "meta": {"ts": ts},
    }
    # 업무분류는 서열이 없어 대표 하나를 못 고른다 — 분포를 그대로 싣는다.
    # 축이 안 돌았으면 키 자체를 넣지 않는다('축 미사용'과 '분류 못 함'의 구분).
    if doctypes:
        rec["contains_doctype"] = dict(sorted(doctypes.items()))
    return rec


#------------------------------------------------------------------
# 추출 실패 문서의 결과 레코드
#=> 텍스트를 못 뽑은 문서를 '없던 일'로 넘기지 않고, 등급 없는(=보류) 레코드로
#   남긴다. 그래야 화면·집계에서 그 문서가 보이고 사람이 처리할 수 있다.
#    1) 등급은 매기지 않는다 — 내용을 못 봤으므로 어떤 등급도 근거가 없다.
#       (failsafe 도 적용하지 않는다. '못 읽었다'와 '읽었는데 신호가 없다'는
#        다른 상태이고, 전자를 자동으로 등급 매기면 사람이 확인할 기회를 잃는다.)
#    2) 왜 실패했는지 error 필드에 남긴다 — 사람이 다음에 뭘 해야 할지 알아야 한다
#    3) 업무분류 축이 켜져 있으면 본문 없이(파일명·경로만으로) 한 번 훑는다.
#       이름만으로 잡히는 것이 있으면 건지고, 없으면 '미분류'로 남는다.
#
# -in: path       = 문서 경로(표시용)
# -in: err        = ExtractError 등 실패 사유
# -in: ruleset    = 규칙셋(rule_version 표기용)
# -in: doc_rules  = doc_rules.DocRuleSet | None
# -in: taxonomy   = axes.Taxonomy | None
# -in: excluded = 스캔 PDF 처리제외 정보({pages, scan_pages, text_len}). 주면 error 대신
#                 excluded 표식을 단다(기본 None = 일반 추출 실패)
#
# -out: dict = 결과 레코드(grade=None, method="extract_failed" 또는 처리제외면 "excluded")
# -out: error = 없음
#------------------------------------------------------------------
def _extract_failed_record(path, err, ruleset, doc_rules=None, taxonomy=None, excluded=None):
    # now_iso·build_record 는 무거운 모듈을 import 시점에 끌어오지 않으려는 이 파일의
    # 관례를 따라 그때 가져온다.
    from .classify import now_iso
    from .classify.engine import build_record
    # 크기 초과 예외인지 가리려고 가져온다(이 파일의 지역 import 관례를 따른다).
    from .extract.base import SizeLimitError

    # 빈 본문으로 정상 경로를 그대로 태운다.
    #=> 본문을 못 읽었어도 경로(Signal C)·파일명(Signal D)은 본문과 무관하게 유효하다.
    #   내용 기반 신호(rule·sensitive·stamp)는 빈 텍스트라 자연히 아무것도 못 찾는다.
    #   [왜 바꿨나] 예전에는 여기서 signals 를 통째로 비워, 보안서버 경로의 읽을 수
    #   없는 문서가 아무 등급 없이 나갔다. 그런데 같은 파일 안에서도 처리가 갈렸다 —
    #   본문이 '거의' 없으면(_mark_no_body) 경로 신호를 살리고, '완전히' 실패하면
    #   버렸다. 둘 다 "본문을 못 읽었다"인데 대응이 달랐던 것이다. 업무분류 축은
    #   이미 파일명·경로로 돌고 있었으니 보안 축만 유독 버려지던 셈이기도 하다.
    #   (실측: D:\분류함 1,492건 비교에서 보안서버 경로의 스캔 PDF 4건이 Python 만
    #    미분류로 나갔다. Rust 판은 같은 문서를 경로 신호로 C 로 잡는다.)
    #   [failsafe 는 주지 않는다] '무신호 기본등급'은 "읽었는데 신호가 없다"에 쓰는
    #   것이지 "못 읽었다"에 쓰는 것이 아니다. 못 읽은 문서를 자동으로 등급 매기면
    #   사람이 확인할 기회를 잃는다.
    rec = build_record(path, "", ruleset, ts=now_iso(), failsafe=None, vector=None,
                       rules_enabled=True, doc_rules=doc_rules, taxonomy=taxonomy)

    # kind 로 '깨져서 못 읽음'과 '너무 커서 안 읽음'을 가른다 — 운영 대응이 다르기
    # 때문이다. 크기 초과는 상한을 올리거나 비동기 큐로 넘기면 그대로 처리되는 정상
    # 문서이고, 그냥 '추출 실패'로 묻히면 그 구분이 사라진다.
    if excluded:
        # 처리제외(스캔 PDF)는 실패가 아니다 — error 대신 excluded 표식을 단다.
        # method 도 여기서 'excluded' 가 되므로 아래 extract_failed 치환에 걸리지 않는다.
        _mark_excluded(rec, excluded)
    else:
        rec["error"] = {"stage": "extract", "reason": str(err),
                        "kind": "size_limit" if isinstance(err, SizeLimitError) else "extract_failed"}

    # 못 읽은 문서는 전파 seed 가 될 수 없다 — 근거를 못 본 문서를 다른 문서의
    # 기준으로 삼으면 오분류가 스스로를 강화한다.
    sec = rec.setdefault("why", {}).setdefault("security", {})
    sec["seed_eligible"] = False

    # 아무것도 못 정했으면 '봤는데 없더라'(unclassified)가 아니라 '못 읽었다'로 적는다.
    # 조건과 표기를 _mark_no_body 와 같게 맞춰, 두 실패 경로가 같은 모양을 내게 한다.
    if not rec.get("grade") and sec.get("method") in (None, "", "unclassified"):
        sec["method"] = "extract_failed"
    return rec


#------------------------------------------------------------------
# 결과 레코드에 문서 식별자 달기 (설계 §7-5)
#=> 지금까지 결과와 사람의 수정을 잇는 유일한 키가 '경로 문자열'이었다. 경로는
#   문서의 주소이지 신분증이 아니라서, 폴더를 옮기거나 드라이브 문자 대소문자만
#   달라져도 그 문서에 쌓아 둔 판단 이력이 끊긴다(실측 4건 유실).
#   그래서 변하지 않는 이름표(doc_id)와 정규화 경로(key)를 함께 심는다.
#    1) 목록(--filelist)에 있으면 그 sfile_id 를 쓴다 — 시스템이 이미 정한 정체성
#    2) 없으면 내용 해시 → 그것도 안 되면 경로 해시로 폴백하고, 어느 것을 썼는지 남긴다
#    3) 목록의 hash 와 실제 파일이 다르면 경고만 세어 둔다(F6 — 수정됐어도 같은 문서다)
#
#   [왜 stats 를 따로 세나] 폴백으로 채워진 문서는 매핑 테이블에 넣으면 안 된다
#   (그 값은 MpowerV11 문서와 이어지지 않아 같은 문서가 두 건으로 들어간다 — R14).
#   그런데 이건 아무 오류도 내지 않는 조용한 누적이라, 건수를 요약 첫 줄에 띄운다.
#
# -in: rec   = 완성된 결과 레코드(제자리에서 고친다)
# -in: path  = 표시용 경로(rec["file"] 과 같은 값)
# -in: src   = 실제로 읽은 경로(압축 내부 파일이면 임시 실경로). None 이면 path
# -in: flist = filelist.FileList 또는 None
# -in: stats = 집계 dict(sfile_id/content/path/case/hash_mismatch 키를 늘린다)
#
# -out: 없음(rec 을 제자리에서 고친다)
# -out: error = 없음
#------------------------------------------------------------------
def _attach_doc_id(rec, path, src, flist, stats):
    from . import filelist as filelist_mod
    doc_id, source, key, matched, chash = filelist_mod.resolve_doc_id(path, src, flist)
    # doc_id 는 이제 sfile_id 뿐이다 — 못 얻으면 None 그대로 둔다(지어내지 않는다).
    rec["doc_id"] = doc_id
    rec["doc_id_source"] = source
    # 지문은 여기서 딱 한 번 구해 싣는다. 예전에는 이 함수가 40자 doc_id 를
    # 만드느라 한 번, 아래 need_hash 가 또 한 번 같은 파일을 해싱했다.
    if chash:
        rec["hash"] = chash
    stats[source] = stats.get(source, 0) + 1
    # 대소문자만 달라 구제된 경우는 조용히 넘기지 않고 그 사실을 레코드에 남긴다.
    if matched == "case":
        rec["rematched_by"] = "case"
        stats["case"] = stats.get("case", 0) + 1
    # F6 — 목록이 준 해시와 실제 파일이 다르면 문서가 수정된 것이다. 같은 문서이므로
    # 처리는 그대로 계속하고 건수만 센다.
    if flist is not None and source == "sfile_id":
        entry, _ = flist.lookup(path)
        if entry is not None and entry.hash:
            actual = _file_hash(src or path)
            if actual and actual != entry.hash:
                stats["hash_mismatch"] = stats.get("hash_mismatch", 0) + 1


#------------------------------------------------------------------
# 목록에 있으나 디스크에 없는 문서의 레코드
#=> --filelist 가 대상을 정한 실행에서, 목록에 적힌 문서가 실제로 없을 때 만든다.
#   예전에는 이런 문서가 결과에서 통째로 사라졌다(stderr 의 F4 경고가 전부였다).
#   그래서 7건을 요청했는데 1건만 돌아와도, 결과 파일만 보는 쪽은 "요청한 만큼
#   다 됐다"고 믿었다 — 조용히 빠지는 것이 이 도구에서 가장 나쁜 실패다.
#
#   [왜 규칙을 안 돌리나] 파일이 없으니 본문도 파일명 신호도 '확인된 것'이 아니다.
#   없는 문서에 등급을 매기면 사람이 확인할 기회를 잃는다(추출 실패에 failsafe 를
#   주지 않는 것과 같은 이유다). grade 는 null 로 두고 왜 없는지만 남긴다.
#
# -in: path      = 목록에 적힌 문서 경로(원본 표기 그대로)
# -in: doc_id    = 목록이 준 sfile_id
# -in: ruleset   = 규칙셋(rule_version 을 적으려고 받는다)
# -in: doc_rules = 업무분류 규칙(축이 꺼져 있으면 None)
# -in: taxonomy  = 분류체계(축이 꺼져 있으면 None)
#
# -out: dict = 결과 레코드(error.kind="file_missing")
# -out: error = 없음
#------------------------------------------------------------------
def _missing_file_record(path, doc_id, ruleset, doc_rules=None, taxonomy=None):
    from .classify import now_iso
    from .classify.engine import build_record

    # rules_enabled=False — 없는 문서에 신호를 지어내지 않는다.
    rec = build_record(path, "", ruleset, ts=now_iso(), failsafe=None, vector=None,
                       rules_enabled=False, doc_rules=None, taxonomy=None)
    rec["error"] = {
        "stage": "input",
        "kind": "file_missing",
        "reason": "목록(--filelist)에 있으나 파일이 없습니다",
    }
    rec["why"]["security"]["method"] = "file_missing"
    # 목록이 준 번호는 그대로 싣는다 — 부르는 쪽이 '어느 문서가 빠졌나'를
    # 자기 ID 로 알아야 목록을 고칠 수 있다.
    if doc_id:
        rec["doc_id"] = doc_id
        rec["doc_id_source"] = "sfile_id"
    # 없는 파일은 지문도 없다. 칸을 만들지 않는다(빈 값을 지어내지 않는다).
    rec["why"]["security"]["seed_eligible"] = False
    return rec


#------------------------------------------------------------------
# 본문에 쓸 만한 글자가 있었나
#=> 스캔본(이미지) PDF 는 추출기가 예외를 던지지 않는다. 장식기호 몇 개를
#   돌려주고 성공한 척한다. 길이로 가려내지 않으면 '미분류'와 섞여 버린다.
#    1) 공백류를 모두 지운 뒤 글자 수를 센다(줄바꿈만 잔뜩인 파일을 거르려고)
#    2) 임계값(config.MIN_TEXT_LEN)보다 적으면 '읽을 글자가 없었다'로 본다
#
# -in: text = 정제된 추출 본문
#
# -out: (부족한가, 글자수) = (True/False, 공백 제외 글자 수)
# -out: error = 없음
#------------------------------------------------------------------
def _body_too_short(text):
    n = len(re.sub(r"\s", "", text or ""))
    return n < config.MIN_TEXT_LEN, n


#------------------------------------------------------------------
# '본문을 못 읽었다' 표식 달기
#=> 분류 결과는 그대로 두고 표식만 더한다 — 파일명·경로 신호는 본문과
#   무관하고, 몇 글자라도 규칙에 걸렸으면 그 등급이 맞다. 다만 아무것도
#   못 정했다면 method 를 'unclassified' 로 두지 않는다. 그 말은 "봤는데
#   없더라"라는 뜻이라 사실과 다르다.
#
# -in: rec = 완성된 레코드(제자리에서 고친다)
# -in: n   = 실제로 얻은 글자 수(공백 제외)
#
# -out: 없음(rec 을 제자리에서 고친다)
# -out: error = 없음
#------------------------------------------------------------------
def _mark_no_body(rec, n):
    # kind 를 여기서도 채운다 — 크기 초과(size_limit)가 이 칸을 쓰기 시작했으므로,
    # 어떤 실패는 kind 가 있고 어떤 실패는 없으면 읽는 쪽이 매번 존재 확인을 해야 한다.
    rec["error"] = {"stage": "extract", "text_len": n, "kind": "no_body",
                    "reason": "본문 텍스트가 거의 없습니다 — 스캔본(이미지)이거나 "
                              "빈 문서일 수 있습니다. OCR 이나 사람 확인이 필요합니다."}
    sec = rec.setdefault("why", {}).setdefault("security", {})
    if not rec.get("grade") and sec.get("method") in (None, "", "unclassified"):
        sec["method"] = "extract_failed"


#------------------------------------------------------------------
# 스캔 PDF 인가 — 처리제외(EXCLUDED) 판정
#=> 1차 개발 범위는 OCR 을 하지 않는다. 텍스트 레이어 없이 그림만 든 PDF 는 '못 읽은
#   문서(실패)'가 아니라 '범위 밖이라 처리하지 않는 문서'로 가려야 한다. 실패로 세면
#   스캔본이 섞인 배치마다 종료코드 1 이 나서, 진짜 깨진 문서가 그 틈에 묻힌다.
#    1) PDF 파서가 남긴 페이지 구성(notes.take_layout)이 없으면 PDF 가 아니다 → 아님
#    2) 스캔 페이지가 읽은 페이지의 SCAN_PDF_PAGE_RATIO 이상이어야 한다
#    3) 최종 본문도 페이지당 SCAN_PDF_PAGE_MAX_CHARS 자 미만이어야 한다
#       (그림 페이지가 대부분이어도 몇 쪽에 본문이 있으면 읽을 것이 있는 문서다.
#        사이냅 폴백이 본문을 읽어 낸 경우도 여기서 걸러진다)
#   Rust 판 extract::scan_pdf_verdict 와 같은 비교식이다.
#
# -in: layout  = {"pages", "scan_pages"} 또는 None(PDF 가 아님)
# -in: n_chars = 최종 본문 글자 수(공백 제외)
#
# -out: dict | None = 스캔 PDF 면 {pages, scan_pages, text_len}, 아니면 None
# -out: error = 없음
#------------------------------------------------------------------
def _scan_pdf_excluded(layout, n_chars):
    if not layout:
        return None
    pages = layout.get("pages") or 0
    scan_pages = layout.get("scan_pages") or 0
    # 한 쪽도 못 읽었으면 판단할 근거가 없다 — 추출 실패 쪽에서 다룬다.
    if pages <= 0:
        return None
    if scan_pages / pages < config.SCAN_PDF_PAGE_RATIO:
        return None
    if n_chars / pages >= config.SCAN_PDF_PAGE_MAX_CHARS:
        return None
    return {"pages": pages, "scan_pages": scan_pages, "text_len": n_chars}


#------------------------------------------------------------------
# '처리제외(스캔 PDF)' 표식 달기
#=> 분류 결과는 그대로 두고 표식만 더한다(_mark_no_body 와 같은 원칙) — 파일명·경로
#   신호는 본문과 무관하게 유효하고, 몇 글자라도 규칙에 걸렸으면 그 등급이 맞다.
#   error 칸은 달지 않는다 — 실패가 아니라 범위 밖이라서다. 아무것도 못 정했을 때만
#   method 를 'excluded' 로 둔다('unclassified' 는 "봤는데 없더라"라 사실과 다르다).
#
# -in: rec  = 완성된 레코드(제자리에서 고친다)
# -in: info = _scan_pdf_excluded 가 돌려준 {pages, scan_pages, text_len}
#
# -out: 없음(rec 을 제자리에서 고친다)
# -out: error = 없음
#------------------------------------------------------------------
def _mark_excluded(rec, info):
    rec["excluded"] = {
        "kind": "scan_pdf",
        "pages": info["pages"], "scan_pages": info["scan_pages"],
        "text_len": info["text_len"],
        "reason": "처리제외(EXCLUDED) — 스캔 PDF(텍스트 레이어 없음)입니다. OCR 은 1차 "
                  "범위에서 제외되어, 본문 없이 파일명·경로 신호로만 판정했습니다.",
    }
    sec = rec.setdefault("why", {}).setdefault("security", {})
    if not rec.get("grade") and sec.get("method") in (None, "", "unclassified"):
        sec["method"] = "excluded"


#------------------------------------------------------------------
# 크기 상한 CLI 덮어쓰기를 config 에 반영 (G1·G2·G4·G5)
#=> --max-file-mb / --no-size-limit 을 이번 프로세스의 config 값에 새겨 넣는다.
#   G3(글자수)만은 값을 인자로 들고 다니지만, 나머지 게이트는 추출기·압축 해제기·
#   PDF 파서처럼 args 를 볼 수 없는 깊은 자리에서 config 를 직접 읽는다. 거기까지
#   인자를 실어 나르면 함수 시그니처 대여섯 개가 상한 하나 때문에 바뀐다.
#   config 값은 이미 환경변수로 정해지는 '이 실행의 설정'이므로, CLI 를 같은
#   자리에 한 번 얹는 것이 우선순위(CLI > 환경변수 > 기본값)와도 맞는다.
#    1) --no-size-limit 이면 모든 상한을 0(=무제한)으로 만들고 경고를 남긴다
#    2) --max-file-mb 가 있으면 MB → 바이트로 바꿔 새기고, 텍스트 상한이 그보다
#       크면 뒤집힌 설정이 되므로 같이 내린다
#
# -in: args = 파싱된 CLI 인자(max_file_mb / no_size_limit 를 본다)
# -in: log  = 로거(None 이면 기록 생략)
#
# -out: 없음(config 모듈 값을 제자리에서 고친다)
# -out: error = 없음
#------------------------------------------------------------------
def _apply_size_limit_overrides(args, log=None):
    # --no-size-limit 은 '전부 해제'다. 0 을 넣어 두면 각 게이트가 0 이하를
    # '상한 없음'으로 읽도록 이미 만들어져 있어, 분기 없이 한 번에 꺼진다.
    if getattr(args, "no_size_limit", False):
        config.MAX_FILE_BYTES = 0
        config.MAX_FILE_BYTES_TEXT = 0
        config.MAX_ARCHIVE_BYTES = 0
        config.MAX_ARCHIVE_MEMBERS = 0
        config.PARSER_TIMEOUT = 0
        config.MAX_PDF_PAGES = 0
        # 조용히 상한을 끄면 나중에 "그때 왜 안 걸렸지"를 설명할 수 없다.
        if log:
            log.warning("--no-size-limit: 문서 크기 상한을 모두 해제했습니다"
                        "(초대형 문서에서 시간·메모리가 무제한으로 늘 수 있습니다)")
        return

    # 0 이하는 어느 플래그에서든 '해제'로 받는다(--max-text-chars 0 과 같은 규칙).
    def _mb(v):
        return int(v * 1048576) if v > 0 else 0

    mb = getattr(args, "max_file_mb", None)
    if mb is not None:
        n = _mb(mb)
        config.MAX_FILE_BYTES = n
        # 텍스트 계열 상한이 일반 상한보다 크면 뒤집힌 설정이라 의미가 없다.
        # 사용자가 일반 상한을 낮췄으면 텍스트 상한도 그 아래로 따라 내린다.
        if n and config.MAX_FILE_BYTES_TEXT > n:
            config.MAX_FILE_BYTES_TEXT = n
        elif not n:
            config.MAX_FILE_BYTES_TEXT = 0

    # 텍스트 상한을 '직접' 지정했으면 위의 따라내리기보다 이쪽이 우선이다 —
    # 사용자가 명시한 값을 자동 보정이 덮으면 플래그를 준 의미가 없다.
    mbt = getattr(args, "max_file_mb_text", None)
    if mbt is not None:
        config.MAX_FILE_BYTES_TEXT = _mb(mbt)

    amb = getattr(args, "max_archive_mb", None)
    if amb is not None:
        config.MAX_ARCHIVE_BYTES = _mb(amb)

    for attr, name in (("max_archive_members", "MAX_ARCHIVE_MEMBERS"),
                       ("parser_timeout", "PARSER_TIMEOUT"),
                       ("max_pdf_pages", "MAX_PDF_PAGES")):
        v = getattr(args, attr, None)
        if v is not None:
            setattr(config, name, v if v > 0 else 0)

    if log:
        log.info("크기 상한 MAX_FILE_BYTES=%s TEXT=%s ARCHIVE=%s MEMBERS=%s "
                 "PARSER_TIMEOUT=%s PDF_PAGES=%s",
                 config.MAX_FILE_BYTES, config.MAX_FILE_BYTES_TEXT,
                 config.MAX_ARCHIVE_BYTES, config.MAX_ARCHIVE_MEMBERS,
                 config.PARSER_TIMEOUT, config.MAX_PDF_PAGES)


#------------------------------------------------------------------
# 이번 실행에 쓸 글자수 상한 결정 (G3)
#=> 우선순위는 다른 옵션들과 같게 'CLI 플래그 > 환경변수 > 코드 기본값' 이다.
#   --no-size-limit 이면 None 을 돌려 상한을 끈다(truncate_text 가 그대로 통과시킴).
#    1) --no-size-limit 이 있으면 무조건 해제
#    2) --max-text-chars 를 줬으면 그 값(0 이하는 '해제'로 읽는다)
#    3) 아니면 config.MAX_TEXT_CHARS(환경변수가 이미 반영된 값)
#
# -in: args = 파싱된 CLI 인자(max_text_chars / no_size_limit 를 본다)
#
# -out: limit = 글자수 상한 정수, 또는 None(상한 없음)
# -out: error = 없음
#------------------------------------------------------------------
def _resolve_text_limit(args):
    # 조사·디버깅용 탈출구 — 상한 때문에 결과가 달라졌는지 확인할 때 쓴다.
    if getattr(args, "no_size_limit", False):
        return None
    n = getattr(args, "max_text_chars", None)
    if n is not None:
        # 0 이하를 '해제'로 받아 준다 — --max-text-chars 0 이 직관적으로 통한다.
        return n if n > 0 else None
    return config.MAX_TEXT_CHARS


#------------------------------------------------------------------
# '파서가 끝까지 못 읽었다' 표식 달기 (G5)
#=> 페이지·시간 상한에 걸려 파서가 중간에 멈춘 문서에 단다. 절단(G3)과 다른
#   사건이다 — G3 은 '다 읽고 나서 잘랐다', 이건 '애초에 끝까지 못 읽었다'라
#   재검토할 때 손볼 상한값이 서로 다르다(MAX_TEXT_CHARS vs MAX_PDF_PAGES).
#   분류 결과 자체는 건드리지 않는다. 읽은 부분만으로도 규칙에 걸렸다면 그
#   등급은 맞다.
#
# -in: rec  = 완성된 레코드(제자리에서 고친다)
# -in: note = notes.take() 가 준 {reason, unit, read, total}
#
# -out: 없음(rec 을 제자리에서 고친다)
# -out: error = 없음
#------------------------------------------------------------------
def _mark_partial_extract(rec, note):
    # 어디까지 읽었는지(읽은 수/전체 수)를 함께 남긴다 — '일부만 읽었다'만으로는
    # 3,000쪽 중 2,999쪽을 읽은 것과 10쪽을 읽은 것이 구분되지 않아, 사람이
    # 재검토 우선순위를 정할 수 없다.
    rec["partial_extract"] = True
    rec["partial_extract_info"] = {
        "reason": note.get("reason"),
        "unit": note.get("unit"),
        "read": note.get("read"),
        "total": note.get("total"),
    }


#------------------------------------------------------------------
# 압축별 업무분류 분포 모으기
#=> 압축 안 문서 하나가 최종 확정될 때마다 그 업무분류 라벨을 압축별 통계에
#   더한다. 보안등급은 'C>S>O' 서열이 있어 최고 위험 하나로 롤업되지만,
#   업무분류는 서열이 없고 한 문서가 여러 라벨을 갖는다 — 대표 하나를 고르면
#   반드시 무언가를 버린다. 그래서 고르지 않고 분포를 그대로 모은다.
#
#   [축을 안 쓰는 배포와 구분] doctype 칸 자체가 없으면 업무분류 축을
#   아예 안 돌린 것이므로 아무것도 모으지 않는다. 그래야 결과에서 '축 미사용'과
#   '축은 돌았는데 못 맞힘'이 구분된다(기존 레코드 규칙과 같은 정신).
#
# -in: store  = {origin: {dc_id: 건수}} 누적 dict(제자리에서 고친다)
# -in: origin = 이 문서가 나온 최상위 압축 경로
# -in: rec    = 최종 확정된 문서 레코드
#
# -out: 없음(store 를 제자리에서 고친다)
# -out: error = 없음
#------------------------------------------------------------------
def _collect_archive_doctype(store, origin, rec):
    # 축이 안 돌았으면(doctype 칸 자체가 없음) 아무것도 모으지 않는다 —
    # '축을 안 씀'과 '분류를 못 함'을 결과에서 구분하려는 기존 규칙과 같다.
    dt = record.doctype_of(rec)
    if dt is None:
        return
    bucket = store.setdefault(origin, {})
    values = dt.get("values") or []
    if not values:
        # 축은 돌았는데 라벨이 안 붙은 문서 — 그것도 사실이라 따로 센다.
        bucket["unclassified"] = bucket.get("unclassified", 0) + 1
        return
    for v in values:
        dc = v.get("dc_id")
        if dc:
            bucket[dc] = bucket.get(dc, 0) + 1


#------------------------------------------------------------------
# '압축을 펼치지 못했다' 표식 달기 (G4)
#=> 해제 상한에 걸려 내부 파일로 펼치지 못한 압축에 단다.
#
# -in: rec    = 완성된 레코드(제자리에서 고친다)
# -in: reason = 왜 못 펼쳤는지
# -in: kind   = "limit"(상한) | "error"(그 밖의 실패) | "split_volume"(분할 조각)
#
# -out: 없음(rec 을 제자리에서 고친다)
# -out: error = 없음
#------------------------------------------------------------------
def _mark_archive_unexpanded(rec, reason, kind="limit"):
    rec["archive_unexpanded"] = True
    rec["archive_unexpanded_reason"] = reason
    # 상한 때문인지(limit) 그 밖의 실패인지(error)를 갈라 둔다 — 사람이 할 일이
    # 다르다. 상한이면 값을 올리면 되고, 실패면 원본이나 포맷을 봐야 한다.
    rec["archive_unexpanded_kind"] = kind


#------------------------------------------------------------------
# '본문 일부만 보고 판정했다' 표식 달기 (G3)
#=> 분류 결과 자체는 건드리지 않는다. 앞부분만으로도 규칙에 걸렸다면 그 등급은
#   맞다. 다만 뒷부분을 안 봤다는 사실을 반드시 남겨야 한다.
#
# -in: rec    = 완성된 레코드(제자리에서 고친다)
# -in: n_orig = 자르기 전 원래 글자수
# -in: n_kept = 실제로 스캔한 글자수(=상한값)
#
# -out: 없음(rec 을 제자리에서 고친다)
# -out: error = 없음
#------------------------------------------------------------------
def _mark_truncated(rec, n_orig, n_kept):
    # 세 값을 함께 남긴다 — '잘렸다'만 있으면 얼마나 잘렸는지 몰라 재검토 우선순위를
    # 못 정한다(200만 중 201만 자와 200만 중 3천만 자는 위험도가 다르다).
    rec["text_truncated"] = True
    rec["text_chars"] = n_kept
    rec["text_chars_original"] = n_orig


#------------------------------------------------------------------
# PII 스캔 상한에 걸렸다는 표식 달기
#=> 전화·주소·계좌는 비용이 초선형이라 앞부분까지만 훑는다(rules 의
#   _KOPII_MAX_TOTAL). 그 절단이 지금까지 아무 표식도 남기지 않아, 뒤쪽 주소록이
#   통째로 안 잡혀도 결과만 보면 "없더라"와 구분되지 않았다.
#   본문 절단(text_truncated)과 나란히 두되 이름을 따로 쓴다 — 자른 층이 다르고,
#   G3 에 안 걸린 문서도 여기 걸릴 수 있기 때문이다.
#   [왜 등급은 안 건드리나] 앞부분에서 잡힌 신호는 그대로 유효하다. 이건 "덜 봤다"는
#   사실을 기록하는 것이지 판정을 뒤집는 것이 아니다.
#
# -in: rec     = 결과 레코드(제자리에서 고친다)
# -in: covered = 초선형 검출기가 실제로 훑은 글자 수
# -in: total   = 전체 글자 수
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def _mark_pii_scan_capped(rec, covered, total):
    # 얼마나 못 봤는지를 함께 남긴다 — 100만 중 101만 자와 100만 중 3억 자는
    # 재검토 우선순위가 전혀 다르다(실측 최악은 커버율 0.3%였다).
    rec["pii_scan_capped"] = True
    rec["pii_scan_chars"] = covered
    rec["pii_scan_chars_total"] = total


#------------------------------------------------------------------
# 추출 본문 한 건을 파일로 남긴다 (--textsave)
#=> 파일 이름은 원본의 SHA-256 이라, 결과 레코드의 hash 칸으로 바로 찾을 수 있고
#   같은 내용의 문서는 한 파일로 합쳐진다. 색인 한 줄도 함께 남겨,
#   hash 이름만 보고는 못 알아보는 문제를 메운다(설계 5·7장).
#
#   [실패해도 분류는 계속한다] 저장은 곁다리다. 디스크가 차거나 잠겨도 등급은
#   그대로 나와야 한다(설계 P1·P2). 대신 무엇이 왜 빠졌는지는 반드시 남긴다 —
#   rec["text_save_error"] 와 요약의 '저장실패 N'.
#
# -in: save_dir  = 저장 폴더(resolve_textsave 가 확정한 절대경로). None 이면 아무것도 안 한다
# -in: rec       = 결과 레코드(제자리에서 표식을 단다)
# -in: text      = 저장할 본문(정제·절단까지 끝난 텍스트)
# -in: path      = 표시 경로(압축 내부면 '압축경로/내부경로')
# -in: truncated = G3 로 뒤가 잘렸는가
# -in: ts        = 저장 시각 문자열
#
# -out: state = "saved" | "dedup" | "skip"(본문 없음/해시 없음) | "error"
# -out: error = 없음(모든 실패는 rec 에 담고 "error" 로 알린다)
#------------------------------------------------------------------
def _save_text_file(save_dir, rec, text, path, truncated, ts):
    if not save_dir:
        return "skip"
    sha = rec.get("hash")
    # 본문이 없으면 파일을 만들지 않는다 — 0바이트 파일을 남기면 "저장됐다"로
    # 오해된다. text_saved 키가 없는 것으로 구분된다(설계 9장).
    if not sha or not text:
        return "skip"
    from . import textsave as _ts
    try:
        name, deduped = _ts.save(save_dir, sha, text)
        # doc_id 는 sfile_id 로 얻은 것만 싣는다 — 폴백(content)은 'hash 앞 40자'라
        # 바로 옆 hash 칸과 같은 값이고, MpowerV11 문서 ID 처럼 생긴 값을 남기면
        # 받는 쪽이 적재해 버린다(--simple 과 같은 규약).
        _doc_id = (rec.get("doc_id")
                   if rec.get("doc_id_source") == "sfile_id" else None)
        _ts.index_append(save_dir, sha, name, path, _doc_id,
                         len(text), truncated, ts)
    except OSError as e:
        # 문서 한 건만 건너뛴다. 종료코드는 건드리지 않는다 — 저장 실패는
        # 분류 실패가 아니고, 자동화가 이걸로 멈추면 안 된다.
        rec["text_save_error"] = f"{type(e).__name__}: {e}"
        return "error"
    rec["text_saved"] = name
    return "dedup" if deduped else "saved"


#------------------------------------------------------------------
# 전체 레코드를 '읽는 차례'로 다시 담기
#=> 만들어진 순서 그대로 내보내면 눈에 안 들어온다 — 가장 궁금한 업무분류가
#   labels 안에 묻혀 열 번째에 있고, 판정 근거(signals)가 결과보다 먼저 나오며,
#   버전 칸이 여기저기 흩어져 있었다.
#    ① 무엇을      file · hash · doc_id
#    ② 어떻게 됐나  grade · doctype · confidence · method · decided_by · error
#    ③ 왜          labels(축별 상세) · signals(근거)
#    ④ 부속        seed_eligible · 버전 3개 · ts · elapsed_ms
#   앞 다섯 칸이 --simple 과 같아, 축약본이 전체의 '앞부분만 떼어낸 것'이 된다.
#
#   [doc_id 가 grade 앞에 있는 이유] doc_id 는 '이 문서가 무엇인가'라서 file·hash
#   와 같은 ① 묶음이다. 예전에는 grade·doctype 뒤(다섯째)에 있었는데, 묶음
#   정의와 어긋나 읽는 사람이 매번 예외를 외워야 했다(2026-09-07 옮김).
#
#   [doctype 을 최상위로] labels.doctype.values 에서 뽑은 같은 값이라 새로 만드는
#   정보가 아니다. 축이 돌았을 때만 넣는다 — 빈 배열이 나가면 "분류 못 함"과
#   "축 안 씀"이 구분되지 않는다(--simple 과 같은 규약).
#
#   [모르는 칸도 잃지 않는다] 표에 없는 칸은 뒤에 그대로 붙인다. 나중에 새 칸이
#   생겼을 때 이 함수가 조용히 지워 버리면 가장 찾기 어려운 사고가 된다.
#
# -in: rec = 결과 레코드
#
# -out: dict = 같은 내용, 차례만 바뀐 새 dict
# -out: error = 없음
#------------------------------------------------------------------
# doc_id_source 는 --simple 다섯 칸 바로 뒤에 둔다 — 문서를 잇는 값의 '출처'라
# doc_id 곁에 있어야 읽히고, 앞 다섯 칸(=--simple)의 차례를 건드리면 "축약본은
# 전체의 앞부분"이라는 약속이 깨진다.
#
# [2026-09-10] key 칸을 뺐다. file 에 normalize_key() 를 걸면 그대로 나오는
# 값이라 실을 이유가 없었다 — 읽던 세 곳이 전부 '없으면 file 로 계산' 폴백을
# 갖고 있었고, override 색인은 저장된 key 를 아예 보지 않고 매번 다시 계산했다.
#
# [2026-09-10] labels 껍데기를 없애고 축을 최상위로 올렸다. 예전에는
# 같은 값이 최상위(grade·confidence·method·decided_by)와 labels.security
# 양쪽에 두 벌 있었고, 최상위 doctype(dc_id 목록)은 labels.doctype.values
# 에서 뽑은 세 번째 사본이었다. 이제 축마다 한 벌씩만 있다.
#
# [2026-09-10 오후] 판정을 맨 앞으로 올렸다. 파일을 열면 가장 먼저 보고 싶은
# 것은 "이 문서가 무엇이고(①) 어떻게 판정됐나(②)" 이지, 그 판정의 근거가
# 아니다. 근거는 why 한 덩어리로 뒤에 둔다 — --simple 이 ①②만 떼어낸
# 모양이라, 축약본과 전체가 같은 낱말·같은 차례를 쓰게 된다.
#   ① 무엇을      file · hash · doc_id · doc_id_source
#   ② 어떻게 됐나  grade · doctype(dc_id 목록) · error · excluded(처리제외)
#   ③ 왜          why(축별 상세) · pii · vector
#   ④ 장부        meta · elapsed_ms
# name 은 엠파워 v11 의 원래 파일 이름 — 목록·명령줄이 줬을 때만 있다(Rust 판 REC_ORDER 와 같다).
# 신원 세 칸(file · hash · doc_id)이 맨 앞이라는 약속은 그대로 두고 그 뒤에 넣는다.
_REC_ORDER = ("file", "hash", "doc_id", "doc_id_source", "rematched_by", "name",
              "grade", "doctype", "error", "excluded", "why", "pii", "vector",
              "text_saved", "text_save_error", "grade_stream", "meta", "elapsed_ms")


# doctype 축 안쪽 칸 차례. 2026-09-10 부터 truncated·conflicts 는 값이 있을 때만,
# strategy 는 --conflict 로 덮어썼을 때만 실린다 — 즉 '언제 붙느냐'가 경로마다
# 달라졌다. 붙는 자리를 표로 고정하지 않으면 같은 문서인데도 두 판(또는 1패스와
# 2패스 전파)이 칸 차례가 다른 파일을 내고, 결과를 나란히 견줄 수 없게 된다.
_DOCTYPE_ORDER = ("values", "strategy", "truncated", "conflicts", "embed")


#------------------------------------------------------------------
# doctype 축 안쪽 칸을 표 차례로 다시 담기
#=> 위 _DOCTYPE_ORDER 를 그대로 따르되, 표에 없는 칸은 잃지 않고 뒤에 붙인다
#   (새 칸이 생겼을 때 조용히 지워 버리면 가장 찾기 어려운 사고가 된다).
#
# -in: dt = rec["doctype"] dict
#
# -out: dict = 같은 내용, 차례만 바뀐 새 dict
# -out: error = 없음
#------------------------------------------------------------------
def _order_doctype(dt):
    if not isinstance(dt, dict):
        return dt
    out = {k: dt[k] for k in _DOCTYPE_ORDER if k in dt}
    for k, v in dt.items():
        if k not in out:
            out[k] = v
    return out


def _order_record(rec):
    if not isinstance(rec, dict):
        return rec
    out = {}
    for key in _REC_ORDER:
        if key == "doctype":
            # 맨 앞의 doctype 은 dc_id 목록이다 — why.doctype.values 에서 지금
            # 뽑는다. 전파가 후보를 더할 수 있어 레코드를 만들 때 미리 적어 두면
            # 어긋난다. 축이 안 돌았으면 키 자체를 만들지 않는다(빈 배열이 나가면
            # "분류 못 함"과 "축 안 씀"이 구분되지 않는다 — --simple 과 같은 규약).
            if record.doctype_of(rec) is not None:
                out["doctype"] = record.doctype_ids(rec)
            continue
        if key == "why" and isinstance(rec.get("why"), dict):
            w = dict(rec["why"])
            if isinstance(w.get("doctype"), dict):
                w["doctype"] = _order_doctype(w["doctype"])
            out["why"] = w
            continue
        if key in rec:
            out[key] = rec[key]
    # 표에 없는 칸은 잃지 않고 뒤에 붙인다.
    for key, val in rec.items():
        if key not in out:
            out[key] = val
    return out


#------------------------------------------------------------------
# 판정 근거 요약 만들기 (--simple-why)
#=> 축약본 4칸으로는 "왜 이 등급인가"를 댈 수 없다. 그렇다고 전체 레코드를
#   주면 26배로 커진다. 판정에 실제로 쓰인 것만 추린다 — 전체의 15% 쯤이다.
#
#   [축별로 묶는다] 레코드 자신의 labels.security / labels.doctype 갈래를 그대로
#   따른다. 평평하게 늘어놓으면 보안등급의 conf 를 업무분류의 값으로 오해한다
#   (Rust 는 키를 사전순으로 내보내 dt 가 conf 와 hits 사이에 끼기까지 했다).
#    1) security.by   : 어느 신호가 등급을 정했는가(rule·stamp·path·name·sensitive)
#    2) security.conf : 그 판정의 확신(= labels.security.confidence)
#    3) security.hits : 걸린 규칙의 id 와 건수만 (이름·용어·원문은 넣지 않는다)
#    4) doctype       : 업무분류 후보의 dc_id·확신과 **판정 경로(by)**
#
#   [doctype 의 by 가 왜 중요한가] 같은 0.91 이라도 뜻이 다르다.
#     rule  = 우리가 정한 낱말이 제목·머리·파일명 여러 군데서 나왔다
#     embed = 이미 분류해 둔 기준 문서와 닮았다
#   검토자가 다르게 봐야 하는 값이라 갈라 준다.
#
#   [원문을 넣지 않는 이유] 이 도구의 불변식이다 — 매칭된 원문 값은 결과에
#   저장하지 않는다(주민번호 12건 검출, 이 아니라 그 번호 자체는 안 남긴다).
#   근거 요약도 그 규칙을 그대로 따른다.
#
# -in: rec = 완성된 결과 레코드
#
# -out: dict = {"security": {"by","conf","hits"}, ["doctype": [{"dc","c","by"}…]]}
# -out: error = 없음(신호가 없으면 hits 가 빈 목록)
#------------------------------------------------------------------
def _why_record(rec):
    s = record.security_of(rec)
    sec = {"by": s.get("decided_by") or [], "conf": s.get("confidence")}
    hits = []
    for sig, val in (s.get("signals") or {}).items():
        if not isinstance(val, dict) or not val.get("grade"):
            continue
        got = val.get("hits") or []
        if got:
            for h in got:
                if isinstance(h, dict):
                    hits.append({"sig": sig, "id": h.get("id"), "n": h.get("count")})
        else:
            # path·embed 처럼 '걸린 규칙 목록'이 없는 신호는 출처만 남긴다.
            hits.append({"sig": sig, "src": val.get("source") or val.get("seed")})
    sec["hits"] = hits
    why = {"security": sec}
    dt = record.doctype_of(rec)
    if dt is not None:
        # stage 는 이 후보가 규칙 스캔에서 나왔는지(rule) 기준 문서 비교에서
        # 나왔는지(embed) 말해 준다 — 같은 숫자라도 뜻이 달라 반드시 함께 낸다.
        vals = [{"dc": v.get("dc_id"), "c": round(v.get("confidence") or 0, 2),
                 "by": v.get("stage") or ("embed" if "embed" in (v.get("from") or [])
                                          else "rule")}
                for v in (dt.get("values") or []) if v.get("dc_id")]
        if vals:
            why["doctype"] = vals
    return why


#------------------------------------------------------------------
# 파일 해시(SHA-256) 계산
#=> 문서 '내용'의 지문을 만든다. 같은 문서인지·변조됐는지 판별, 중복 제거 등에 쓴다.
#   큰 파일도 메모리 폭발 없이 1MB 씩 나눠 읽어 갱신한다.
#
# -in: path = 해시를 구할 파일 경로(압축 내부 파일이면 그 임시 실경로)
# -in: algo = 해시 알고리즘 이름(기본 sha256)
#
# -out: str = 소문자 16진수 해시 문자열
# -out: error = 읽기 실패(OSError) 시 None
#------------------------------------------------------------------
def _file_hash(path, algo="sha256"):
    import hashlib
    h = hashlib.new(algo)
    try:
        with open(path, "rb") as f:
            # 1MB 단위로 읽어 대용량도 안전하게 누적 해시.
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


#------------------------------------------------------------------
# --simple 용 레코드 축약(문서명·등급·해시 + 업무분류 + 문서 ID)
#=> 결과 레코드에서 연동에 필요한 칸만 남긴 새 dict 를 만든다.
#   원본 레코드는 건드리지 않는다.
#
#   [doc_id 를 왜 sfile_id 일 때만 싣나]
#   폴백 doc_id(content)는 '파일 SHA-256 앞 40자'라서 이미 hash 칸에 들어 있다
#   (doc_id == hash[:40]). 그 값을 한 칸 더 실어 봐야 새 정보가 없고, 오히려
#   MpowerV11 문서 ID 처럼 생긴 값이 적재 쪽으로 흘러가 같은 문서가 두 건으로
#   들어가는 사고를 부른다. 그래서 sfile_id 로 채워진 경우만 값을 싣고,
#   나머지는 None 으로 둔다 — 받는 쪽은 "None 이면 hash 로 문서를 가린다".
#
# -in: rec = 전체 결과 레코드(hash 필드가 이미 채워져 있어야 함)
# -in: why = True 면 판정 근거 요약(why)을 함께 담는다(--simple-why)
#
# -out: dict = {"file":..., "hash":..., "grade":..., ["doctype":...],
#               ["doc_id":...], ["error":...], ["why":...]}
# -out: error = 없음
#------------------------------------------------------------------
def _simple_record(rec, why=False):
    # 읽는 차례로 넣는다 — 무엇을(file·hash·doc_id) → 어떻게 됐나(grade·doctype) → 왜(why).
    # 셋 다 '이 문서가 무엇인가'라서 붙여 두고, 판정 결과와 섞지 않는다.
    out = {"file": rec.get("file"), "hash": rec.get("hash")}
    # 문서 ID 축이 돌았을 때만 doc_id 키를 붙인다(--no-doc-id 면 키 자체가 없다).
    # doctype 과 같은 규약이다 — "못 얻었다(None)"와 "축을 안 썼다(키 없음)"를 가른다.
    if "doc_id_source" in rec:
        # sfile_id 로 채워진 것만 MpowerV11 문서와 이어지는 값이다. 폴백(content·path)은
        # 우리가 만들어 낸 값이라 적재하면 안 되므로 None 으로 내보낸다.
        out["doc_id"] = rec.get("doc_id") if rec.get("doc_id_source") == "sfile_id" else None
    # 엠파워 v11 의 원래 파일 이름 — 줬을 때만 싣는다(없던 실행의 줄 모양은 그대로다).
    # 신원 세 칸(file · hash · doc_id) 뒤에 둔다 — 그 차례는 받는 쪽과의 약속이다.
    if rec.get("name"):
        out["name"] = rec["name"]
    out["grade"] = record.grade_of(rec)
    # 업무분류 축이 돌았을 때만 doctype 키를 붙인다. 축을 안 쓰는 배포에서 빈 배열이
    # 나가면 "분류를 못 했다"와 "축을 안 썼다"가 구분되지 않는다(설계서 4-6과 같은 규약).
    if record.doctype_of(rec) is not None:
        # 확신 내림차순은 엔진이 이미 맞춰 놓았다. 여기서는 dc_id 만 뽑아 담는다 —
        # 받는 쪽(문서중앙화)은 분류체계를 이미 갖고 있으므로 이름은 필요 없다.
        out["doctype"] = record.doctype_ids(rec)
    # 못 읽은 문서에는 그 사실을 함께 싣는다. 없으면 '읽었는데 미분류'와 글자 그대로
    # 같은 모습이라, --simple 만 받는 쪽은 스캔본을 영영 못 가려낸다.
    # 성공한 문서에는 이 칸이 아예 없다(기존 모양 그대로).
    err = rec.get("error")
    if isinstance(err, dict):
        out["error"] = err.get("reason") or "extract_failed"
    # 처리제외(스캔 PDF) 문서도 표식을 싣는다. 없으면 '읽었는데 미분류'와 같은 모습이라
    # 받는 쪽이 OCR 대상으로 돌릴 문서를 못 가린다. 기계가 가를 수 있게 종류(kind)만 싣는다.
    ex = rec.get("excluded")
    if isinstance(ex, dict):
        out["excluded"] = ex.get("kind") or "excluded"
    if why:
        out["why"] = _why_record(rec)
    return out


#------------------------------------------------------------------
# 텍스트 추출만 수행(--text-only)
#=> 임베딩 없이 추출→정제한 텍스트를 그대로 출력한다. 모델이 필요 없으므로 항상
#   in-process 로 처리한다.
#
# -in: files    = 대상 파일 리스트(경로 or (src,label))
# -in: opts     = 처리 옵션(save_dir 등)
# -in: out_fp   = 출력 파일객체(None 이면 stdout)
#
# -out: code = 종료코드(모두 성공이면 0, 하나라도 추출 실패면 EXIT_EXTRACT_FAIL)
# -out: error = 없음(개별 실패는 stderr 로 알림)
#------------------------------------------------------------------
def run_text_only(files, opts, out_fp, dec_ctx=None):
    from .extract import build_extractor
    from .extract.base import ExtractError
    from .clean import clean_text

    # --hybridparse 여부에 따라 사이냅 단독 또는 하이브리드 추출기를 만든다.
    extractor = build_extractor(hybrid=opts.get("hybridparse", False))
    code = config.EXIT_OK
    # 못 읽은 문서 수 — 상태 줄에 "N건 중 M건"을 싣는다(종료코드 1 의 까닭).
    failed = 0
    n_jobs = 0
    # 엠파워 v11 암호화 문서는 차례가 왔을 때 풀어서 읽는다(dec_ctx 가 없으면 그대로 돈다).
    for item in _iter_dec_jobs(files, dec_ctx):
        n_jobs += 1
        if isinstance(item, _DecFailed):
            print(f"[MpowerClassify] 복호화 실패: {item.label} :: {item.err.message}",
                  file=sys.stderr)
            # 분류 모드와 같이 로그 파일에도 남긴다 — 화면만 보면 실행이 끝난 뒤 흔적이 없다.
            logsetup.get_logger("csoclassify.cli").error("%s", _dec_fail_line(item))
            errcodes.set_status("decrypt_failed",
                                f"[MpowerClassify] 복호화하지 못했습니다 — {item.err.message}")
            code = errcodes.exit_of("decrypt_failed")
            failed += 1
            continue
        # src=실제 읽을 경로(압축 내부면 임시파일), label=표시용(압축경로/내부경로).
        src, label, _origin = _as_job(item)
        try:
            raw = extractor.extract(src, save_dir=opts.get("save_dir"))
            text = clean_text(raw)
            print(f"===== {label} =====", file=(out_fp or sys.stdout))
            print(text, file=(out_fp or sys.stdout))
        except ExtractError as e:
            # ExtractError 는 이제 사유만 담으므로 경로는 여기서 한 번 붙인다.
            print(f"[MpowerClassify] 추출 실패: {label} :: {e}", file=sys.stderr)
            code = config.EXIT_EXTRACT_FAIL
            failed += 1
    errcodes.set_counts(n_jobs, failed)
    return code


#------------------------------------------------------------------
# 임베딩 모델을 쓸 수 있는가 (값싼 확인 — 세션은 만들지 않는다)
#=> --vector-only 는 임베딩 비교만으로 등급을 정하므로, 모델이 없으면 결과가 전부
#   미분류가 된다. 그런 결과 파일은 쓸모가 없고 정상 결과처럼 적재될 위험만 있다.
#   그래서 문서를 한 건도 읽기 전에 확인한다(Rust 판 embed::check_deployment 와 같은 자리).
#    1) 모델 폴더를 찾는다(env·캐시·exe 옆·번들 순 — 임베더와 같은 규칙)
#    2) model.onnx · tokenizer.json 이 있는지 본다
#    3) onnxruntime · tokenizers 를 불러올 수 있는지 본다
#
# -in: model = 모델 별칭(--model 값)
#
# -out: str = 쓸 수 없는 까닭(쓸 수 있으면 None)
# -out: error = 없음(모든 실패를 까닭 문자열로 돌려준다)
#------------------------------------------------------------------
def _model_unavailable(model):
    from . import resources
    try:
        spec = config.get_model_spec(model)
        mdir = resources.resolve_model_dir(spec.local_dir)
    except Exception as e:      # noqa: BLE001 (별칭 오류·압축 해제 실패 모두 '못 씀')
        return f"모델 폴더를 정하지 못했습니다: {type(e).__name__}: {e}"
    # 임베더가 실제로 여는 두 파일 — 폴더만 있고 알맹이가 없는 배포가 있었다.
    for name in ("model.onnx", "tokenizer.json"):
        if not os.path.isfile(os.path.join(mdir, name)):
            return f"{name} 이(가) 없습니다: {os.path.join(mdir, name)}"
    try:
        import onnxruntime  # noqa: F401
        import tokenizers   # noqa: F401
    except ImportError as e:
        return f"필수 라이브러리를 불러오지 못했습니다(onnxruntime/tokenizers): {e}"
    return None


#------------------------------------------------------------------
# seed 파일이 이번 벡터와 같은 차원인가
#=> 다른 모델로 만든 seed 를 쓰면 비교가 성립하지 않는다(파이썬은 행렬 오류로 죽고,
#   Rust 판은 조용히 잘라 엉뚱한 유사도를 냈다). 쓰기 전에 차원을 맞춰 본다.
#    1) seed 파일의 {차원: 건수} 를 센다
#    2) 이번 차원과 다른 것이 하나라도 있으면(섞인 경우 포함) 안내 문장을 돌려준다
#
# -in: seeds_path = seed 파일 경로
# -in: dim        = 이번 실행 벡터의 차원
# -in: what       = 안내에 쓸 '이번 벡터'의 설명(예: "모델 ko-sroberta(768차원)")
#
# -out: str = 맞지 않는 까닭(맞으면 None)
# -out: error = 없음
#------------------------------------------------------------------
def _seed_dim_problem(seeds_path, dim, what):
    from .classify.propagate import seed_file_dims
    dims = seed_file_dims(seeds_path)
    bad = {d: n for d, n in dims.items() if d != dim}
    if not bad:
        return None
    shown = " · ".join(f"{d}차원 {n}건" for d, n in sorted(dims.items()))
    return (f"기준 문서(seed) 벡터가 이번 벡터와 맞지 않습니다 — {what}, seed 파일은 {shown}: {seeds_path}\n"
            f"  seed 를 만든 모델로 --model 을 맞추거나, 이 모델로 seed 를 다시 만드세요.")


#------------------------------------------------------------------
# 업무분류(doctype) 축 로드 — 자동 생성 규칙 파일 하나로 켠다 (설계서 4-6)
#=> --build-doc-rule 로 만든 doc_rule.yaml 을 읽는다. 분류체계(제목·경로·조상)가
#   규칙 줄 안에 있으므로 다른 파일은 읽지 않는다(2026-09-22 — doc_taxonomy.yaml 없앰).
#    1) --axis security 면 애초에 시도하지 않는다(파일이 깨져 있어도 상관없다 —
#       이번 실행이 쓰지 않을 축이므로 검증할 이유가 없다)
#    2) 규칙 파일이 없으면: --axis doctype 명시 시 종료(3), 아니면 경고 후 security 만.
#       예전의 'seed 전파 전용'(규칙 없이 축 켜기)은 없다 — 분류체계가 규칙 안에
#       있어서, 규칙 파일이 없으면 seed 의 dc_id 를 경로로 풀 방법도 없다
#    3) 파일이 있는데 옛 모양(자동 생성본이 아님)이면 항상 종료(4) — 조용히 축을 끄면
#       "왜 업무분류가 안 나오지"가 된다. 새로 만드는 법을 알려 준다(YAML 이 깨졌으면
#       같은 코드로 읽기 오류를 알린다)
#    4) 자동 생성본이면 _load_generated_axis 로 켠다
#
# -in: args = argparse 결과(doc_rules/axis/doctype_vector_only 필드 사용)
# -in: log  = 로거
#
# -out: (taxonomy, doc_rules_set, code) — code 가 None 이 아니면 호출자는 그 값을
#        즉시 반환해야 한다(치명적 오류). code 가 None 이면 taxonomy/doc_rules_set
#        은 축이 꺼졌으면 둘 다 None, 켜졌으면 둘 다 값이 있다
# -out: error = 없음(모든 실패는 code 로 환원)
#------------------------------------------------------------------
def _load_doctype_axis(args, log):
    axis = getattr(args, "axis", None)
    if axis == "security":
        return None, None, None   # 이번 실행은 doctype 을 아예 안 쓴다 — 시도조차 안 함

    from .classify import doc_rules as DR

    doc_rules_path = args.doc_rules or DR.default_doc_rules_path()
    if not os.path.isfile(doc_rules_path):
        if axis == "doctype":
            # '파일 없음'은 내용 오류(4)가 아니라 부른 쪽이 고칠 문제(3)다.
            # 코드 이름은 옛 약속(taxonomy_missing)을 그대로 쓴다 — 분류체계가 없다는 뜻은 같다.
            return None, None, fail_err(
                "taxonomy_missing",
                f"[MpowerClassify] 업무분류 규칙 파일(doc_rule.yaml)이 없습니다 — 분류체계가 "
                f"그 안에 있습니다. --build-doc-rule 로 만드세요. 찾은 경로: {doc_rules_path}",
                doc_rules_path)
        print("[MpowerClassify] doc_rule.yaml 이 없어 업무분류(doctype) 축을 건너뜁니다. "
              "규칙은 --build-doc-rule 로 만드세요.", file=sys.stderr)
        log.warning("doc_rule.yaml 없음 — 업무분류 축 끔 :: path=%s", doc_rules_path)
        return None, None, None

    if not _is_generated_rules(doc_rules_path):
        # YAML 자체가 깨졌으면 '옛 모양'이 아니라 읽기 오류를 그대로 알린다(Rust 판과 같은 갈래).
        import yaml
        try:
            with open(doc_rules_path, encoding="utf-8") as f:
                yaml.safe_load(f)
        except (OSError, yaml.YAMLError) as e:
            log.error("업무분류 규칙을 읽지 못함 :: path=%s", doc_rules_path)
            return None, None, fail_err("doc_rules_invalid",
                                        f"[MpowerClassify] 업무분류 규칙 파일을 읽지 못했습니다: "
                                        f"{doc_rules_path}\n  {e}", doc_rules_path)
        # 읽히는데 자동 생성본이 아니다 — 옛 모양(분류체계를 doc_taxonomy.yaml 로 따로 읽던 방식).
        log.error("업무분류 규칙이 자동 생성본이 아님 :: path=%s", doc_rules_path)
        return None, None, fail_err(
            "doc_rules_invalid",
            f"[MpowerClassify] [G0] 옛 모양 규칙 파일입니다(분류체계 doc_taxonomy.yaml 을 "
            f"따로 읽던 방식): {doc_rules_path}\n"
            f"  이 판부터는 --build-doc-rule 로 만든 규칙만 읽습니다. 화면의 "
            f"[새 방식으로 바꾸기] 또는 --build-doc-rule 로 다시 만드세요.",
            doc_rules_path)
    return _load_generated_axis(args, log, doc_rules_path)

#------------------------------------------------------------------
# --conflict 실행 시 덮어쓰기 파싱 (설계서 6-5·T2·T10·T11)
#=> "doctype=top_n:3" 또는 "doctype=all" 형태를 (축 id, ConflictSpec) 으로 바꾼다.
#   값 검증은 doc_rules.py 의 기존 검증기(_check_conflict)를 그대로 재사용해,
#   doc_rule.yaml 에 적을 때와 실행 시 덮어쓸 때가 항상 같은 기준으로 걸러지게 한다.
#
# -in: spec = --conflict 원본 문자열
#
# -out: (axis, ConflictSpec) = 덮어쓸 축 id 와 파싱된 전략
# -out: error = 형식·값이 틀리면 ValueError(사람이 읽는 메시지 포함)
#------------------------------------------------------------------
def _parse_conflict_override(spec):
    from .classify import doc_rules as DR

    if "=" not in spec:
        raise ValueError(f"--conflict 형식이 올바르지 않습니다: {spec!r} (예: doctype=top_n:3)")
    axis, value = (part.strip() for part in spec.split("=", 1))

    if ":" in value:
        strategy, n_str = value.split(":", 1)
        try:
            n = int(n_str)
        except ValueError:
            raise ValueError(f"--conflict 의 n 값이 정수가 아닙니다: {n_str!r}")
        raw = {"strategy": strategy, "n": n}
    else:
        raw = value

    violation = DR._check_conflict(raw)
    if violation:
        raise ValueError(f"--conflict {spec!r} — {violation['detail']}")
    return axis, DR._parse_conflict(raw)


#------------------------------------------------------------------
# labels.doctype → (뿌리, 노드) 쌍 목록 (요약 집계용)
#=> rec["labels"]["doctype"]["values"] 의 각 후보에서 path 문자열의 첫 조각(뿌리
#   카테고리)과 마지막 조각(실제 걸린 노드 이름)만 뽑는다. taxonomy 객체가 없어도
#   되도록 이미 레코드에 박힌 path 문자열만 쓴다(설계서 4-4의 "> " 구분자 그대로).
#
# -in: doctype_label = rec["labels"]["doctype"] dict
#
# -out: list[(str, str)] = (뿌리 title, 노드 title) 쌍(후보가 없으면 빈 목록)
# -out: error = 없음
#------------------------------------------------------------------
def _doctype_breakdown(doctype_label):
    pairs = []
    for v in (doctype_label.get("values") or []):
        segments = [s for s in (v.get("path") or "").split(" > ") if s]
        if not segments:
            segments = [v.get("dc_id", "?")]
        pairs.append((segments[0], segments[-1]))
    return pairs


#------------------------------------------------------------------
# --seed-fill-title — 기준 문서에 제목(첫 줄)을 채운다
#=> BM25 2차는 기준 문서의 제목 · 파일명을 비교한다(설계서 업무분류-2차-BM25 §09).
#   예전에 등록한 기준 문서에는 제목 칸이 없으므로 한 번 채워 준다.
#    1) 제목이 이미 있는 줄은 건너뛴다
#    2) 본문은 --text-dir/<지문>.txt 에서 먼저 찾고, 없으면 원본 파일을 다시 추출한다
#    3) ppt · xls · xlsx 이거나 첫 줄이 비면 제목은 비어 있다(파일명만으로 비교)
#    4) 하나라도 채웠으면 잠근 채로 다시 쓴다
#
# -in: args = 파싱된 인자(seeds · text_dir)
#
# -out: int = 종료코드(정상 0)
# -out: error = --seeds 가 없거나 파일이 없으면 bad_args(1003), 쓰기 실패면 output_write_failed
#------------------------------------------------------------------
def run_seed_fill_title(args):
    from . import seedstore
    from .extract import build_extractor
    from .extract.base import ExtractError
    from .clean import clean_text
    from .classify.doctype import _title_allowed

    path = getattr(args, "seeds", None)
    if not path or not os.path.isfile(path):
        return fail_err("bad_args", "[MpowerClassify] --seed-fill-title 은 기준 문서 파일이 필요합니다 "
                        f"(--seeds <class_seed.jsonl>): {path or '(없음)'}")
    text_dir = getattr(args, "text_dir", None)
    extractor = None
    filled = empty = nosrc = had = 0
    try:
        with seedstore.seed_lock(path):
            rows = seedstore.load_seeds(path)
            for r in rows:
                if r.get("title"):
                    had += 1
                    continue
                text, h, f = None, r.get("hash"), r.get("file") or ""
                if not _title_allowed(f):
                    # ppt · xls · xlsx 는 제목을 쓰지 않는다 — 원본을 다시 읽어 봐야 빈 제목이다
                    empty += 1
                    continue
                if text_dir and h and os.path.isfile(os.path.join(text_dir, f"{h}.txt")):
                    with open(os.path.join(text_dir, f"{h}.txt"), encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                if text is None and f and os.path.isfile(f):
                    try:
                        # 저장해 둔 본문이 없으면 원본을 다시 읽는다(추출기는 처음 필요할 때만 만든다)
                        extractor = extractor or build_extractor()
                        text = clean_text(extractor.extract(f, save_dir=None))
                    except (ExtractError, OSError):
                        text = None
                if text is None:
                    nosrc += 1
                    continue
                t = bm25mod.title_of(text, f)
                if t:
                    r["title"] = t
                    filled += 1
                else:
                    empty += 1
            if filled:
                seedstore.save_seeds(path, rows)
    except (TimeoutError, OSError) as e:
        return fail_err("output_write_failed", f"[MpowerClassify] 기준 문서를 쓰지 못했습니다: {path} :: {e}", path)
    print(f"[seed-fill-title] 채움 {filled} · 제목 없음 {empty}(ppt·xls·빈 첫 줄) · "
          f"원문 못 찾음 {nosrc} · 이미 있음 {had}", file=sys.stderr)
    return 0


#------------------------------------------------------------------
# BM25 2차 재료 만들기(색인 · 자동 문턱)
#=> doc_rule.yaml 의 bm25.enabled 일 때, 기준 문서로 분류 단위 BM25 색인을 만들고
#   최소 점수를 정한다(설계서 §15 · §16).
#    1) mode=auto  — 기준 문서끼리 자기 제외 모의시험으로 문턱을 계산한다
#    2) mode=fixed — 규칙 파일의 min_score 를 쓴다
#    3) 문턱을 못 정하면 threshold=None — 그 실행은 BM25 를 건너뛰고 이유를 알린다
#
# -in: doc_rules_set = DocRuleSet(bm25 · head_noise 를 쓴다)
# -in: seeds_path    = class_seed.jsonl 경로
# -in: dup           = 거의 같은 기준 문서로 볼 벡터 유사도(embed.dup_threshold)
# -in: fields        = {레코드 번호: (제목, 경로)} — 2차 대상 문서의 BM25 재료
#
# -out: dict | None = {index, spec, threshold, noise, fields, auto} (bm25 를 안 쓰면 None)
# -out: error = 없음(기준 문서 파일이 깨졌으면 읽을 수 있는 줄만 쓴다)
#------------------------------------------------------------------
def _bm25_context(doc_rules_set, seeds_path, dup, fields):
    sp = doc_rules_set.bm25
    if not sp.enabled or not seeds_path or not os.path.isfile(seeds_path):
        return None
    rows = []
    with open(seeds_path, encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue   # 깨진 줄 하나 때문에 2차 전체를 멈추지 않는다
    noise = tuple(doc_rules_set.head_noise or ())
    index = bm25mod.Bm25Index(rows, sp, noise)
    if sp.mode == "fixed":
        auto = {"threshold": sp.min_score, "passed": 0, "correct": 0, "scored": 0}
        print(f"[전파][업무분류][bm25] 문턱 {sp.min_score:.3f}(고정) · 기준 문서 {index.size()}건",
              file=sys.stderr)
    else:
        auto = bm25mod.auto_threshold(index, dup)
        if auto["threshold"] is None:
            print(f"[전파][업무분류][bm25] 기준 문서가 부족해 문턱을 정하지 못해 이번 실행은 BM25 를 "
                  f"건너뜁니다(기준 문서 {index.size()}건 · 모의시험 {auto['scored']}건) — "
                  f"기준 문서 50건 이상(분류마다 3건 이상)을 권합니다", file=sys.stderr)
        else:
            print(f"[전파][업무분류][bm25] 문턱 {auto['threshold']:.3f} — 기준 문서 {index.size()}건 "
                  f"자기 제외 모의시험(통과 {auto['passed']} · 정답 {auto['correct']} · "
                  f"{auto['correct'] / auto['passed'] * 100:.1f}%)", file=sys.stderr)
    return {"index": index, "spec": sp, "threshold": auto["threshold"], "noise": noise,
            "fields": fields, "auto": auto}


#------------------------------------------------------------------
# C/S/O 보안등급 & 업무분류 분류 처리 (기본 모드 · 예전 --classify)
#=> 추출→정제한 텍스트에 규칙 스캔(Signal A)을 걸어 보안등급(C/S/O)과 업무분류(doctype)
#   를 동시에 수행하여 문서마다 분류 결과 레코드를 출력한다.
#    1) 규칙셋 1회 로드: CSO 보안등급(cso_rule.yaml) + 업무분류(doc_rule.yaml, 선택)
#    2) 파일마다: 추출→정제→build_record(규칙/경로/파일명/임베딩으로 등급·분류 산출) → jsonl/json
#    3) 마지막에 CSO 등급 분포 + 업무분류 롤업 요약을 stderr 로 출력(오탐률/분류현황 실측용)
#   [프라이버시] 추출 텍스트는 기본적으로 디스크에 남기지 않는다(save_dir=None → 자동삭제).
#     단 --with-text 를 주면 결과 레코드에 정제 텍스트를 rec["text"] 로 함께 저장한다.
#   [진행표시] args.progress 면 파일마다 "[progress] 처리수/총수 경로" 를 stderr 로
#     흘려 보낸다(성공/실패 무관). UI 가 이 줄을 읽어 진행바·경과시간을 그린다.
#   [보안등급(security)] 임베딩은 등급 결정엔 안 쓰이고(1차 분류는 규칙/경로/파일명만),
#     전파(2차)의 '보류 구제 대상'과 '내부 seed 기준'에만 필요하다. 그래서:
#       · --with-vector : 모든 문서 임베딩(구 동작, RAG 등 전량 벡터가 필요할 때)
#       · --embed-needed: 보류(grade=None)이거나 seed_eligible 인 문서만 임베딩
#                         → 규칙으로 확정된 대다수 문서의 임베딩을 건너뛰어 대폭 빨라짐
#       · 둘 다 없음     : 임베딩 안 함(가장 빠른 규칙-only 분류)
#   [업무분류(doctype)] doc_rule.yaml 이 있으면 활성화. 규칙(내용 기반) + 임베딩(벡터 비교)
#     으로 업무분류 후보를 산출하고, seed 전파로 미분류를 구제한다. 없으면 미활성화(축 자체 생략).
#   [전파(자동)] 기본 분류는 보류 문서를 seed 와 임베딩 비교해 자동 전파한다:
#     seed 파일(--seeds 또는 exe 옆 class_seed.jsonl)이 '있으면' auto_prop 을 자동으로 켜고,
#     보류 문서 벡터가 필요하므로 임베딩(needed)도 자동 활성화한다. seed 파일이 없으면
#     전파 없이 규칙만으로 끝낸다(레코드 스트리밍). --auto-propagate 로 명시할 수도 있다.
#     전파는 '외부 seed(class_seed.jsonl) + 내부 seed_eligible'을 함께 기준으로 쓴다.
#   [분류방식 배타옵션]
#       · --rule-only  : 규칙(cso_rule.yaml + doc_rule.yaml)만으로 분류. seed 가 옆에 있어도
#                        임베딩·전파를 아예 안 한다(순수 규칙 분류를 보장하는 명시적 차단, 가장 빠름).
#       · --vector-only: 규칙 검사를 하지 않고(rules_enabled=False → 전부 보류 레코드),
#                        전량 임베딩 후 seed 와 벡터 비교(전파)로만 등급을 정한다.
#                        비교 기준 seed 파일(--seeds)이 반드시 있어야 한다(없으면 인자 오류).
#   [--axis 축 선택]
#       · (기본)        : CSO 보안등급만 분류(업무분류 축 제외, 가장 빠름)
#       · --axis doctype: 업무분류만 수행(CSO 보안등급 계산 생략, PII 검출 포함)
#
# -in: files  = 대상 파일 리스트
# -in: args   = argparse 결과(fmt/rules/failsafe 등)
# -in: out_fp = 출력 파일객체(None 이면 stdout)
#
# -out: code = 종료코드(모두 성공 0, 하나라도 추출 실패면 EXIT_EXTRACT_FAIL)
# -out: error = 없음(개별 실패는 stderr + 로그로 알림)
#------------------------------------------------------------------
def run_classify(files, args, out_fp):
    import json
    import time

    from . import PROCESS_START_PERF   # 프로세스(파이썬 진입) 시작 시각 — startup 측정 기준
    from .extract import build_extractor
    from .extract import notes
    # 분할 압축 조각인지 이름으로 가려내는 헬퍼(압축 확장기와 같은 규칙을 쓴다).
    from .extract.archive import split_volume_hint as archive_split_hint
    from .extract.base import ExtractError, SizeLimitError
    from .clean import clean_text, truncate_text
    from .classify import (load_rules, build_record, now_iso, default_seed_path, superlinear_coverage,
                           RuleSetValidationError)

    log = logsetup.get_logger("csoclassify.cli")

    #-----------------------------------------------------------
    # C/S/O 분류 규칙파일 로딩.
    #-----------------------------------------------------------
    # 규칙셋은 파일마다 다시 읽지 않도록 한 번만 로드해 재사용한다.
    # 규칙셋은 외장 파일(exe 옆)이라 누락이 흔하다 → 원시 스택 대신 안내 후 정상 종료.
    try:
        # => C/S/O 분류 cso_rule.yaml 파일을 불러온다.
        ruleset = load_rules(args.rules)
    except FileNotFoundError as e:
        log.error("규칙셋 로드 실패 :: %s", e)
        # 예외가 실제로 뒤진 경로를 알고 있으면 그것을 쓴다(--cso-rules 를 안 준 경우 기본 경로).
        return fail_err("rules_missing", f"[MpowerClassify] {e}",
                        getattr(e, "filename", None) or args.rules)
    except RuleSetValidationError as e:
        # 파일은 있지만 내용이 틀림 → 스캔을 시작하지 않고 위반 전체를 보여 준다.
        # "파일 없음(3)"과 구분되는 코드 4 로 나가 배치가 대응을 나눌 수 있게 한다.
        log.error("규칙셋 검증 실패 count=%d path=%s", len(e.violations), e.path)
        return fail_err("rules_invalid", f"[MpowerClassify] {e}", e.path)

    #-----------------------------------------------------------
    # 업무분류 규칙파일 로딩.
    #-----------------------------------------------------------
    # 업무분류(doctype) 축 — 있으면 켜고 없으면 security 만(4-6). --axis doctype 이면 필수.
    # => doc_taxomomy.yaml 과 doc_rules.yaml 파일을 불러온다.
    taxonomy, doc_rules_set, dt_code = _load_doctype_axis(args, log)

    # not None => 규칙파일이 잘못된 경우임. 이때는 에러띄우고 종료.
    if dt_code is not None: 
        return dt_code

    #-----------------------------------------------------------
    # --conflict 옵션 : 업무분류시 분류체계를 몇개남길꺼나(기본=all)
    #-----------------------------------------------------------
    # --conflict 로 doctype 전략을 실행 시 덮어쓰기(6-5). security 는 서열 축이라 금지(T11).
    # => --conflit 는 한문서에 업무분류체계가 여러개일때 몇개를 남길껀지 설정값(기본=all)
    doctype_strategy_override = None   # 있으면 rec["labels"]["doctype"]["strategy"] 에 각인
    # --conflict 설정되어 있으면 설정한 숫자(1,2,3,..)를 설정한다.
    if getattr(args, "conflict", None):
        from .classify import AxisNotOverridableError, ensure_overridable_axis
        try:
            override_axis, override_spec = _parse_conflict_override(args.conflict)
            ensure_overridable_axis(override_axis)
        except (ValueError, AxisNotOverridableError) as e:
            return fail_err("bad_conflict_axis", f"[MpowerClassify] {e}")
        if doc_rules_set is not None:
            import dataclasses
            doc_rules_set = dataclasses.replace(doc_rules_set, conflict=override_spec)
        # 재현성을 위해 실제 적용된 문자열을 그대로 결과에 남긴다(설계서 6-5 warn).
        doctype_strategy_override = args.conflict.split("=", 1)[1]

    #-----------------------------------------------------------
    # --axis 옵션 : 등급분류만 할꺼냐, 업무분류만 할꺼냐, 아님 둘다(기본=둘다)
    #-----------------------------------------------------------
    # => --axis security|doctype   이번 실행에 쓸 축만 지정(doctype 이면 보안등급 계산 생략) 
    axis = getattr(args, "axis", None)

    #-----------------------------------------------------------
    # -- hybridparse 옵션 : 하이브리드 추출기 만듬.
    # --hybridparse 여부에 따라 사이냅 단독 또는 하이브리드 추출기를 만든다(분류는 추출이 클라이언트측).
    #-----------------------------------------------------------
    extractor = build_extractor(hybrid=getattr(args, "hybridparse", False))

    #-----------------------------------------------------------
    # 부가적인 옵션값 설정.
    #-----------------------------------------------------------
    with_text = getattr(args, "with_text", False)         # 결과 레코드에 추출 텍스트 포함
    # 벡터를 '결과 파일에 실어 보낼지'. 만들지 말지가 아니다 — 전파는 벡터를
    # 레코드 안에서 주고받으므로 언제나 만들어야 하고, 여기서 정하는 것은 출력이다.
    #
    # [2026-09-11] 예전에는 이 구분이 없어, 임베딩한 문서면 **언제나** 384차원
    # 실수 배열이 결과 파일에 실렸다. 두 가지가 조용히 어긋났다:
    #   · Rust 판은 이 두 인자가 있을 때만 싣는다 — 같은 명령의 결과가 판마다 달랐다
    #   · 인자를 준 적 없는 사용자의 결과 파일에 본문에서 뽑은 벡터가 들어갔다.
    #     벡터는 본문의 파생물이라 공유·백업 시 텍스트와 같은 급으로 다뤄야 한다.
    # 두 인자의 도움말이 이미 "벡터 산출/포함"이라고 말하고 있었다 — 코드만 안 따랐다.
    emit_vector = (getattr(args, "with_vector", False)
                   or getattr(args, "embed_needed", False))
    auto_prop = getattr(args, "auto_propagate", False)    # 분류 직후 보류 문서를 seed 로 전파
    rule_only = getattr(args, "rule_only", False)         # 규칙만(임베딩·전파 없음)
    vector_only = getattr(args, "vector_only", False)     # 규칙 없이 벡터-seed 비교만

    #-----------------------------------------------------------
    # rules_enabled 설정.
    # --axis doctype 이면 security 신호 자체를 계산하지 않는다(6-4 — PII 검출까지 포함해
    # 통째로 생략, 가장 비싼 단계를 건너뛰어 대량 업무분류 스캔이 크게 빨라진다).
    #-----------------------------------------------------------
    rules_enabled = not vector_only and axis != "doctype"

    #-----------------------------------------------------------
    # 전파 비교 기준 seed 경로
    #  --seeds 우선, 없으면 exe 옆 class_seed.jsonl(기본 규약).
    #-----------------------------------------------------------
    seeds_path = args.seeds or default_seed_path()

    #-----------------------------------------------------------
    # 분류 방식 결정 — 배타 3분기: 벡터만 / 규칙만 / 기본(규칙+자동전파).
    #-----------------------------------------------------------
    if vector_only:
        # 규칙 미사용: 전량 임베딩 후 seed 와 비교해 등급을 정한다 → 비교 기준 seed 파일 필수.
        if not seeds_path or not os.path.isfile(seeds_path):
            return fail_err("seeds_missing",
                            "[MpowerClassify] --vector-only 는 비교 기준 seed 파일이 필요합니다: "
                            "--seeds <class_seed.jsonl>(또는 exe 옆 class_seed.jsonl)",
                            seeds_path)
        # 모델을 못 쓰면 이 모드는 전 문서가 미분류가 된다 — 결과를 내지 않고 멈춘다.
        # (기본 모드는 규칙 등급이 유효하므로 결과를 내고 종료코드 2 로만 알린다.)
        why = _model_unavailable(args.model)
        if why:
            return fail_err("model_load_failed",
                            f"[MpowerClassify] --vector-only 는 임베딩 모델이 있어야 합니다 — {why}")
        embed_mode = "all"      # 모든 문서를 임베딩해야 seed 와 비교 가능
        auto_prop = True        # 전파(=벡터 비교)로 등급을 정하므로 항상 켠다
    elif rule_only:
        # 규칙만: 임베딩·전파를 하지 않는다(가장 빠름). seed 가 옆에 있어도 무시(명시적 차단).
        embed_mode = "none"
        auto_prop = False
    else:
        # 기본: 규칙 분류 + '보류 문서 자동 전파'. seed 파일(exe 옆 class_seed.jsonl 또는 --seeds)이
        # 있으면 auto_prop 을 자동으로 켠다(명시적 --auto-propagate 없이도). 없으면 전파 없이 규칙만.
        if not auto_prop and seeds_path and os.path.isfile(seeds_path):
            auto_prop = True
        elif not auto_prop:
            # seed 가 없으면 전파(Signal B)가 통째로 꺼진다 — 규칙이 못 정한 문서는
            # 구제받지 못하고 등급 없이(null) 나간다. 예전에는 이걸 아무 말 없이
            # 넘어갔고, 배포 폴더에 파일이 빠진 것을 아무도 몰랐다(실측: 배포본에
            # class_seed.jsonl 이 없어 보류 21건이 미분류로 나갔다 — 부록 C-5).
            # 파일이 없는 것 자체는 정상 배포일 수도 있으므로(규칙만 쓰는 배포)
            # 오류로 막지는 않고, '무엇이 꺼졌는지'만 분명히 알린다.
            print(f"[MpowerClassify] 전파용 seed 파일이 없어 자동 전파를 건너뜁니다: "
                  f"{seeds_path}\n"
                  f"              규칙이 못 정한 문서는 미분류로 남습니다. "
                  f"의도한 것이면 --rule-only 로 명시하세요.", file=sys.stderr)
        # 임베딩 정책 needed(선택적) > all(전량) > none. embed_needed 가 우선.
        if getattr(args, "embed_needed", False):
            embed_mode = "needed"
        elif getattr(args, "with_vector", False):
            embed_mode = "all"
        else:
            embed_mode = "none"
        # seed 를 만들려면 '보류 문서'가 아니라 '규칙이 자신 있게 분류한 문서'의
        # 벡터가 필요하다. needed 모드는 확정 문서의 임베딩을 건너뛰므로 씨앗이
        # 하나도 안 나온다 — 전량 임베딩으로 올린다.
        if getattr(args, "make_doctype_seeds", None):
            embed_mode = "all"
        # 전파(auto_prop)는 보류 문서 벡터가 있어야 구제 가능 → 꺼져 있으면 needed 로 올린다.
        if auto_prop and embed_mode == "none":
            embed_mode = "needed"

    # seed 를 실제로 쓰는 실행(전파·기준 문서 등록)이면, 문서를 읽기 전에 seed 벡터가
    # 이번 모델과 같은 차원인지 본다 — 다른 모델로 만든 seed 와는 비교가 성립하지 않는다.
    # 규칙만 쓰는 실행은 exe 옆 seed 가 무엇이든 상관없으므로 보지 않는다.
    if (auto_prop or getattr(args, "_seed_plan", None)) and seeds_path and os.path.isfile(seeds_path):
        _spec = config.get_model_spec(args.model)
        why = _seed_dim_problem(seeds_path, _spec.dim, f"모델 {_spec.key}({_spec.dim}차원)")
        if why:
            return fail_err("seeds_model_mismatch", f"[MpowerClassify] {why}", seeds_path)

    #-----------------------------------------------------------
    # 임베딩 수단 준비(임베딩이 필요한 정책일 때만). 데몬(웜 모델 재사용) 우선, 실패 시
    # in-process 폴백. 데몬을 쓰면 '따로따로 반복 실행'해도 model_load=0 으로 빨라진다.
    #-----------------------------------------------------------
    daemon_client = None
    ip_embedder = {"v": None}    # in-process 임베더(폴백/‘--no-daemon’). 처음 필요할 때 지연 생성.
    if embed_mode != "none" and args.daemon:
        from .daemon.client import DaemonClient
        # 데몬이 같은 로그파일에 남기도록 경로/verbose 전달. 유휴시간(무한 기본)도 함께.
        daemon_client = DaemonClient(args.model, idle_timeout=args.idle_timeout,
                                     log_path=getattr(args, "_log_path", None), verbose=args.verbose)

    #--------------------------------------------------------------
    # in-process 임베더 확보(지연 생성)
    #=> 데몬 미사용/폴백 시 쓸 OnnxEmbedder 를 한 번만 만들어 재사용한다.
    # -in: 없음
    # -out: OnnxEmbedder
    # -out: error = 없음
    #--------------------------------------------------------------
    def _get_ip_embedder():
        if ip_embedder["v"] is None:
            from .embed.onnx_embedder import OnnxEmbedder
            ip_embedder["v"] = OnnxEmbedder(config.get_model_spec(args.model),
                                            num_threads=args.num_threads)
        return ip_embedder["v"]

    #--------------------------------------------------------------
    # 텍스트 → 벡터 (데몬 우선, in-process 폴백)
    #=> 이미 추출·정제된 텍스트를 임베딩한다. 데몬이 가능하면 웜 모델로(model_load=0),
    #   접속/통신 실패면 조용히 in-process 로 폴백한다. 단계 시간(model_load/embed)도 기록.
    # -in: text   = 정제 텍스트
    # -in: timing = 이 파일의 Timing(단계시간 기록용)
    # -out: list[float] = 문서 임베딩 벡터
    # -out: error = 임베딩 자체 실패는 예외 전파(호출부가 잡아 벡터 없이 진행)
    #--------------------------------------------------------------
    def _embed_text(text, timing):
        emb_opts = {"max_tokens": args.max_tokens, "overlap": args.overlap, "normalize": True}
        if daemon_client is not None:
            from .daemon.client import DaemonUnavailable
            try:
                with timing.measure("embed"):
                    vec = daemon_client.request_embed_text(text, emb_opts)
                timing.set("model_load", 0.0)      # 데몬은 이미 웜
                return list(vec)
            except DaemonUnavailable as e:
                # 데몬 불가 → in-process 폴백(원인은 로그로 남긴다).
                if args.verbose:
                    print(f"[MpowerClassify] 데몬 폴백(in-process): {e}", file=sys.stderr)
                log.warning("분류 임베딩 데몬 불가 → in-process 폴백 :: %s", e)

        #--------------------------------------------------------------
        # 임베딩모델 로딩
        #--------------------------------------------------------------
        emb = _get_ip_embedder()
        
        timing.set("model_load", emb.ensure_loaded())
        with timing.measure("embed"):
            result, _ = emb.embed_document(text, args.max_tokens, args.overlap,
                                           normalize=True, per_chunk=False)
        return list(result)

    # 등급 분포 집계(요약용). none = 규칙 미검출(+failsafe 미사용).
    counts = {"C": 0, "S": 0, "O": 0, "none": 0}
    embedded = 0            # 실제 임베딩한 문서 수(요약·검증용)
    embed_errors = []       # 임베딩 실패 사유(한 건도 성공 못 했으면 모델 실패로 본다)
    # 본문을 못 읽은 문서 수 — 추출기가 예외를 던진 것과 '글자가 사실상 없던 것'을
    # 함께 센다. 부르는 쪽이 "이번 실행에 손봐야 할 문서가 몇 건인가"를 알아야 한다.
    n_extract_failed = 0
    # 글자수 상한(G3)에 걸려 '앞부분만 보고' 판정한 문서 수. 요약에 실어, 운영자가
    # "이번 배치에서 상한에 걸린 문서가 몇 건인가"를 보고 상한값을 조정할 수 있게 한다.
    n_text_truncated = 0
    n_pii_capped = 0        # PII 스캔 상한에 걸려 뒷부분을 못 본 문서 수
    # 크기 상한(G1·G2)에 걸려 '아예 읽지 않은' 문서 수. 추출 실패와 따로 세야
    # 운영자가 "상한을 올리면 처리되는 건"과 "원본이 깨진 건"을 구분할 수 있다.
    n_size_skipped = 0
    # 분할 압축의 조각이라 읽지 않은 문서 수. 추출 실패와 따로 세야
    # "원본이 깨진 것"과 "조각이라 원래 못 읽는 것"을 구분할 수 있다.
    n_split_volume = 0
    # 파서가 상한에서 멈춰 '앞부분만' 읽은 문서 수(G5). 절단(G3)과 다른 사건이다 —
    # G3 은 다 읽고 나서 자른 것이고, 이건 애초에 끝까지 읽지 못한 것이다.
    n_partial_extract = 0
    # 스캔 PDF 라 '처리제외'로 가린 문서 수. 추출 실패와 따로 센다 — 실패가 아니라
    # OCR 미적용(1차 범위 밖)이라 종료코드에도 넣지 않는다.
    n_excluded = 0
    # --textsave 관측 — 저장이 조용히 반쯤 실패하는 것을 막는다(설계 8장).
    n_text_saved = 0
    n_text_dedup = 0
    n_text_save_failed = 0
    # 해제 상한(G4)에 걸려 내부 파일로 펼치지 못한 압축(main 이 expand_paths 에서 받아 둔다).
    # 경로 → 사유. 아래 루프에서 해당 압축 레코드에 표식을 달 때 쓴다.
    # 경로 → (사유, 종류). 종류는 "limit"(상한 초과) 또는 "error"(손상·미지원·분할볼륨 등).
    # 둘을 섞으면 안내가 엇나간다 — 손상 압축에 "상한을 올리세요"는 틀린 처방이다.
    arch_unexpanded = {row["path"]: (row["reason"], row.get("kind", "limit"))
                       for row in (getattr(args, "_archive_stats", None) or {}).get("unexpanded", [])}
    # 상한은 파일마다 다시 계산할 이유가 없으므로 루프 밖에서 한 번만 정한다.
    text_limit = _resolve_text_limit(args)
    # 문서 식별자 집계 — ID 를 어떤 출처로 채웠는지 센다(요약 첫 줄 · R14).
    docid_stats = {}
    docid_flist = getattr(args, "_filelist", None)
    docid_on = not getattr(args, "no_doc_id", False)
    missing_id_rows = []          # --report-missing-id 로 뽑을 폴백 문서들
    code = config.EXIT_OK

    # 업무분류(doctype) 축 집계(요약용, 설계서 7-3 — 뿌리 카테고리 롤업). doctype 축이
    # 이번 실행에서 한 건도 안 돌았으면 doctype_total_docs 가 0 으로 남아 요약에서
    # 이 섹션 자체를 뺀다(4-6 — "축을 안 씀"과 "미분류"를 구분).
    doctype_total_docs = 0
    doctype_unclassified = 0
    doctype_root_totals = {}     # {뿌리 title: 문서-분류 쌍 수}
    doctype_node_totals = {}     # {(뿌리 title, 노드 title): 건수}

    # 출력 축약 옵션 — 모두 C/S/O 분류 모드에서만 의미가 있다.
    summary_only = getattr(args, "summary_only", False)   # 요약만 출력(파일별 레코드 생략)
    no_summary = getattr(args, "no_summary", False)       # 맨 끝 요약 레코드를 출력에서 제거
    simple_why = getattr(args, "simple_why", False)       # 축약본에 판정 근거 요약까지
    simple = getattr(args, "simple", False) or simple_why  # 파일별 문서명·등급·해시 3필드만
    # --textsave 는 파일 이름을 해시로 삼으므로 해시가 반드시 있어야 한다.
    # 비용은 파일 1회 순차 읽기라 추출에 비하면 무시할 수준이다(설계 5장).
    text_save_dir = getattr(args, "textsave", None)
    need_hash = (getattr(args, "hash", False) or simple
                 or bool(text_save_dir))    # --hash/--simple/--textsave 면 해시 계산
    # 사람 결정 목록 — 판정이 끝난 뒤 사람 등급이 이긴다(설계: 사람등급지정 §03).
    # 지문으로 잇고 내용 변경을 알아야 하므로 지문도 구한다.
    ov_applier = None
    if getattr(args, "grade_overrides", None):
        from . import gradeoverride
        _ov_index = gradeoverride.load(args.grade_overrides)
        ov_applier = gradeoverride.Applier(_ov_index)
        need_hash = True
        print(f"[MpowerClassify] 사람 결정 목록: {args.grade_overrides} · 보안등급 결정 "
              f"{len(_ov_index.latest)}건" + (f" · 읽지 못한 줄 {_ov_index.broken}건" if _ov_index.broken else ""),
              file=sys.stderr)

    #--------------------------------------------------------------
    # 레코드 1개 출력(+로그)
    #=> 형식(json=들여쓰기 / 그외=1줄 압축)을 한 곳에서 정해 출력하고, 같은 내용을
    #   로그에도 남긴다(벡터·텍스트는 축약). 일반/전파 두 경로가 공유한다.
    #   ※ --summary 면 파일별 레코드는 내보내지 않고(요약만), --simple 이면 3필드로 줄인다.
    # -in: rec = 결과 레코드
    # -out: 없음(파일/화면 출력 + 로그)
    # -out: error = 없음
    #--------------------------------------------------------------
    # json 은 '항상' 유효한 배열([ ... ])로 낸다. 분류는 맨 끝에 summary 레코드를 반드시
    # 덧붙이므로(단건이라도 레코드+summary = 2건), 배열로 감싸지 않으면 객체가 이어붙어
    # 깨진 JSON({..}{..})이 된다. 압축 확장으로 단건이 여러 건이 되는 경우도 배열이라야 맞다.
    # 단, --summary 는 요약 객체 하나만 내므로 배열로 감싸지 않는다(단일 JSON 객체).
    writer = output.RecordWriter(out_fp or sys.stdout, args.fmt, multi=not summary_only)

    # --simple + --out 이면 전체 레코드를 '<out>.full.<확장자>' 에 함께 남긴다(감사용).
    #   · --out 이 없으면(화면 출력) 두 갈래를 한 곳에 흘릴 수 없으므로 만들지 않는다
    #   · --out 이 가리키는 파일은 지금까지처럼 축약본이다 — 이미 그 파일을 읽고
    #     있는 쪽의 계약을 바꾸지 않는다
    #   · 분류를 다시 하는 것이 아니라 손에 든 레코드를 한 번 더 적을 뿐이다
    #     (실측 — 분류 2,320ms 대 축약본 쓰기 0.27ms = 0.011%)
    full_path = full_fp = full_writer = None
    if simple and args.out and not summary_only:
        _b, _e = os.path.splitext(args.out)
        full_path = f"{_b}.full{_e or '.jsonl'}"
        try:
            full_fp = open(full_path, "w", encoding="utf-8")
        except OSError as e:
            return fail_err("output_write_failed",
                            f"[MpowerClassify] 감사용 전체 결과 파일을 쓰지 못했습니다: "
                            f"{full_path} :: {e}", full_path)
        full_writer = output.RecordWriter(full_fp, args.fmt, multi=True)

    # [--simple 과 --with-pii 를 같이 준 경우] 축약본에는 pii 를 싣지 않는다.
    # --simple 은 연동용(문서중앙화) 형식이고, 이 도구의 불변식은 "매칭된 원문
    # PII 값은 결과에 저장하지 않는다" 이다 — 주민번호 원본이 연동 경로로
    # 흘러가지 않는 것이 옳은 기본값이다.
    #
    # 그런데 사용자는 --with-pii 를 **명시해서** 줬다. 아무 말 없이 무시하면
    # "줬으니 받았겠지" 라고 믿게 된다. 무엇이 왜 빠졌는지 말해 주지 않는 것이
    # 이 도구에서 가장 나쁜 실패다(--synap-only·데몬 인자와 같은 원칙).
    #
    # --out 이 있으면 감사용 <out>.full 에 그대로 남으므로 어디서 찾을지 알려
    # 주면 되고, --out 이 없으면 그 사이드카가 아예 안 만들어져 **PII 가 어디에도
    # 안 남는다** — 그때는 더 분명히 말한다.
    if simple and getattr(args, "with_pii", False) and not summary_only:
        if full_writer is not None:
            print("[MpowerClassify] --simple 축약본에는 pii 를 싣지 않습니다"
                  "(연동 형식에 원문 PII 를 넣지 않는다는 규칙).", file=sys.stderr)
            print(f"                 검출된 원문 값은 감사용 전체 파일에 있습니다: "
                  f"{full_path}", file=sys.stderr)
        else:
            print("[MpowerClassify] --with-pii 가 이번 실행에서는 아무 데도 남지 않습니다.",
                  file=sys.stderr)
            print("                 --simple 축약본은 pii 를 싣지 않고, --out 이 없어 "
                  "감사용 전체 파일도 만들지 않습니다.", file=sys.stderr)
            print("                 원문 값이 필요하면 --out 을 주거나(<out>.full 에 남습니다) "
                  "--simple 을 빼세요.", file=sys.stderr)

    #--------------------------------------------------------------
    # 실행 헤더 한 줄 — 이번 실행이 어떤 기준으로 판정했는가
    #=> 규칙셋 버전 세 칸은 한 번 실행하면 모든 줄이 같은 값이라, 예전에는
    #   레코드마다 같은 글자를 되풀이해 적었다(실측: 결과 파일의 7.9%가 meta 였고
    #   그중 버전 세 칸이 6.1%였다). 요약 줄에도 이미 같은 값이 들어 있어,
    #   18개 문서를 분류하면 같은 버전이 19번 적히는 셈이었다.
    #
    #   [왜 요약이 아니라 헤더인가] 요약은 맨 뒤에 나온다. jsonl 을 한 줄씩
    #   처리하는 쪽은 문서를 다 읽은 뒤에야 "어느 규칙으로 판정된 것인지"를
    #   알게 된다. 게다가 --nosummary 를 주면 요약 자체가 안 나가 버전이
    #   어디에도 안 남는다. 맨 앞 한 줄이면 두 문제가 다 없어진다.
    #
    #   [축약본에는 넣지 않는다] --simple 은 받는 쪽(문서중앙화)과의 계약이라
    #   줄 모양을 바꾸지 않는다. 감사용 전체 파일(.full)에는 넣는다.
    #
    # -in: 없음(바깥 스코프의 ruleset·doc_rules_set·taxonomy 를 본다)
    #
    # -out: dict = {"run": {...}} 한 줄
    # -out: error = 없음
    #--------------------------------------------------------------
    def _run_header():
        run = {"rule_version": ruleset.version}
        # 업무분류 축이 돌았을 때만 그 두 버전을 적는다(축 미사용과 구분).
        if doc_rules_set is not None and taxonomy is not None:
            run["doctype_rule_version"] = doc_rules_set.version
            run["taxonomy_version"] = taxonomy.exported_at
        run["started_at"] = now_iso()
        return {"run": run}

    _header_done = []

    # 등급 스트림 기록기 — --write-grade-stream 일 때만 만든다(꺼진 실행은 파일을 건드리지 않는다).
    # 출력 직전(_emit)이 등급이 최종인 자리라 거기서 쓴다(설계 §10).
    grade_marker = None
    if getattr(args, "write_grade_stream", False):
        from . import gradestream
        grade_marker = gradestream.Marker(
            unclassified=getattr(args, "grade_stream_unclassified", None) or "skip",
            # 기본은 시각 보존 — --grade-stream-update-time 일 때만 수정 시각이 바뀐다
            keep_time=not getattr(args, "grade_stream_update_time", False))

    #--------------------------------------------------------------
    # 사람 결정 적용(+ 등급별 건수 보정)
    #=> 레코드가 최종이 된 자리에서 부른다. 이미 센 건수(counts)를 사람 등급으로 옮긴다.
    #   adjust=False 는 건수를 아직 세지 않은 자리(전파 뒤 다시 세는 루프 · 압축 집계)용이다.
    #
    # -in: rec    = 결과 레코드
    # -in: adjust = 등급별 건수를 고칠지
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _ov(rec, adjust=True):
        if ov_applier is None:
            return
        changed = ov_applier(rec)
        if changed and adjust and changed[0] != changed[1]:
            o, n = changed[0] or "none", changed[1] or "none"
            counts[o] = counts.get(o, 0) - 1
            counts[n] = counts.get(n, 0) + 1

    #--------------------------------------------------------------
    # 압축 집계에 넣을 등급 — 못 읽은 문서는 None 이지만 사람이 정했으면 그 등급
    #
    # -in: rec = 결과 레코드
    #
    # -out: 등급 | None
    # -out: error = 없음
    #--------------------------------------------------------------
    def _member_grade(rec):
        sec = (rec.get("why") or {}).get("security") or {}
        return rec.get("grade") if sec.get("method") == "manual_override" else None

    def _emit_header_once():
        if _header_done:
            return
        _header_done.append(True)
        if summary_only:
            return
        # 축약본에는 안 넣는다 — 전체 출력일 때만 맨 앞 줄로 나간다.
        if not simple:
            writer.write_record(_run_header())
        if full_writer is not None:
            full_writer.write_record(_run_header())

    def _emit(rec):
        # 등록 모드에서는 분류 결과가 목적이 아니다 — 레코드를 모아 두고
        # 마지막에 기준 문서 줄로 바꾼다. stdout 은 등록 결과만 나가야 한다(P5).
        if getattr(args, "_seed_plan", None) is not None:
            args._seed_recs.append(rec)
            return
        # 등급 스트림은 출력 모양(--summary · --simple)과 상관없이 쓴다 — 기록은 판정의 결과다.
        if grade_marker is not None:
            grade_marker.apply(rec)
        # --summary 면 파일별/집계 레코드는 아예 출력하지 않는다(맨 끝 요약만 낸다).
        if summary_only:
            return
        _emit_header_once()
        # --simple 이면 문서명·등급·해시 3가지만 남긴다.
        out = _simple_record(rec, why=simple_why) if simple else _order_record(rec)
        # 벡터는 인자로 요청했을 때만 내보낸다. 레코드 자체에서 지우지 않고
        # '내보낼 사본'에서만 뺀다 — 뒤이어 도는 전파·seed 수집이 rec["vector"] 를
        # 그대로 읽기 때문이다. 여기서 지우면 그쪽이 조용히 빈손이 된다.
        if not emit_vector:
            out.pop("vector", None)
        writer.write_record(out)
        # 축약본만으로는 "왜 이 등급인가"를 나중에 댈 수 없다 — 전체를 옆에 남긴다.
        # 감사용 전체본에도 같은 기준을 적용한다(Rust 판은 애초에 안 싣는다).
        if full_writer is not None:
            full = _order_record(rec)
            if not emit_vector:
                full.pop("vector", None)
            full_writer.write_record(full)
        log.info("결과 %s", output.dumps_safe(_loggable_record(rec), separators=(",", ":")))

    #--------------------------------------------------------------
    # 업무분류 라벨 마무리 + 요약 집계
    #=> 레코드가 '최종'이 된 시점(=출력 직전)에 딱 한 번 부른다. 전파가 라벨을
    #   더 붙일 수 있어서, 1차 스캔 직후에 세면 전파 전 숫자가 요약에 박힌다.
    #    1) --conflict 로 전략을 덮어썼으면 그 문자열을 레코드에 각인(6-5 재현성)
    #    2) 뿌리/말단 분류별로 문서 수를 센다(라벨이 없으면 미분류로 카운트)
    #
    # -in: rec = 결과 레코드(doctype 축이 있을 수도, 없을 수도)
    #
    # -out: 없음(rec 갱신 + 바깥 집계 변수 갱신)
    # -out: error = 없음
    #--------------------------------------------------------------
    def _finalize_doctype(rec):
        nonlocal doctype_total_docs, doctype_unclassified
        label = record.doctype_of(rec)
        # 키 자체가 없으면 이번 실행이 축을 안 쓴 것 — 집계 대상이 아니다(4-6).
        if label is None:
            return
        if doctype_strategy_override:
            # 재현 불가를 막는다(6-5 warn) — 같은 규칙셋인데 실행 시 덮어써서 결과가
            # 다르면, 그 사실이 레코드 자체에 남아야 나중에 왜 달랐는지 알 수 있다.
            label["strategy"] = doctype_strategy_override
        doctype_total_docs += 1
        pairs = _doctype_breakdown(label)
        if not pairs:
            doctype_unclassified += 1
        for root_title, node_title in pairs:
            doctype_root_totals[root_title] = doctype_root_totals.get(root_title, 0) + 1
            key = (root_title, node_title)
            doctype_node_totals[key] = doctype_node_totals.get(key, 0) + 1

    # 진행표시를 켰으면 총 개수를 먼저 알린다(UI 가 0/total 로 바를 초기화).
    total = len(files)
    show_progress = getattr(args, "progress", False)
    if show_progress:
        print(f"[progress] 0/{total} ", file=sys.stderr, flush=True)

    do_timing = getattr(args, "timing", True)   # 결과 레코드에 elapsed_ms 를 넣을지(--no-timing 이면 끔)
    totals = []       # 파일별 total ms(배치 요약용)
    records = []      # auto_prop 일 때만 모아 뒀다가 전파 후 한꺼번에 출력
    # 2차 대상 문서의 BM25 재료 {레코드 번호: (제목, 경로)} — 본문 대신 제목 한 줄만 남긴다
    bm25_fields = {}
    # 업무분류 seed 후보. 전파가 라벨을 더하기 '전' 상태로 담아 둔다 —
    # 전파로 붙은 라벨은 절대 씨앗이 되면 안 되기 때문이다(재설계 10-4 drift 방지).
    # 본문 전체가 아니라 길이만 담는다(문서 하나가 수 MB 인 경우가 있다).
    seed_pool = []

    #---------------------------------------------------------------------
    # 실제 문서 분류 시작
    # => 1.문서추출->2.1차 룰분류->3.벡터생성->4.업무분류 seed 생성->5.전파
    #---------------------------------------------------------------------
    # startup: 프로세스(파이썬 진입) 시작 → 여기(첫 추출 직전)까지의 일회성 준비 비용.
    #   무거운 import(ko-pii·numpy·onnxruntime)·규칙셋 로드·데몬 준비 등이 포함된다.
    #   (exe 부트로더의 _internal 로딩 등 파이썬 진입 이전은 측정 불가.) 로그로 남기고,
    #   맨 처음 출력되는 레코드의 elapsed_ms 앞에 'startup' 키로 실어 total 에도 더한다.
    startup_ms = (time.perf_counter() - PROCESS_START_PERF) * 1000.0
    log.info("startup %.1fms (프로세스 시작→분류 시작; import·규칙로드·데몬준비 포함)", startup_ms)
    startup_pending = do_timing   # 첫 레코드에만 startup 을 붙인다(일회성 비용)

    # 압축파일 '자체'의 집계 등급용: origin(최상위 압축) → 내부 파일 최종등급 리스트.
    arch_members = {}
    # 압축별 업무분류 분포: {origin: {dc_id: 건수}}. 보안등급과 달리 서열이 없어
    # 대표 하나를 못 고르므로, 고르지 않고 분포를 그대로 모은다.
    arch_doctypes = {}
    rec_origins = []   # auto_prop 경로에서 records 와 나란히 origin 을 보관

    # 엠파워 v11 암호화 문서 — 차례가 왔을 때 풀어서 읽는다(없으면 dec_ctx 는 None).
    dec_ctx = getattr(args, "_dec_ctx", None)
    if dec_ctx is not None:
        # 풀린 압축에서 못 펼친 것도 같은 표에 들어오게 한다.
        dec_ctx.arch_unexpanded = arch_unexpanded
    n_jobs = 0    # 실제로 돈 작업 수(복호화한 압축이 내부 문서로 늘어나면 len(files) 와 달라진다)

    # for문을 돌면서 문서처리..
    for i, item in enumerate(_iter_dec_jobs(files, dec_ctx), 1):
        n_jobs = i
        if dec_ctx is not None:
            total = len(files) + dec_ctx.extra
        # 풀지 못한 문서 — 분류하지 않고 오류 레코드로 남긴 뒤 다음으로 간다.
        if isinstance(item, _DecFailed):
            path, _e = item.label, item.err
            if _e.reason == "size_limit":
                _kind = "size_limit"
                n_size_skipped += 1
                n_extract_failed += 1
                code = config.EXIT_EXTRACT_FAIL
            else:
                _kind = "decrypt_failed"
                dec_ctx.n_fail += 1
            print(f"[MpowerClassify] 복호화 실패: {path} :: {_e.message}({_e.reason})",
                  file=sys.stderr)
            # 성공 줄과 같은 칸(모드·크기·걸린 시간)을 실어 실패 원인을 로그만 보고 가늠하게 한다.
            log.error("%s", _dec_fail_line(item))
            rec = _decrypt_failed_record(path, item.plan.name, _e, _kind,
                                         ruleset, doc_rules_set, taxonomy)
            counts["none"] = counts.get("none", 0) + 1
            if docid_on:
                # 지문(hash)은 싣지 않는다 — 여기서 읽을 수 있는 것은 암호문뿐이고,
                # 암호문의 지문은 '이 문서가 무엇인가'를 말해 주지 않는다.
                _ent, _how = (docid_flist.lookup(path) if docid_flist is not None
                              else (None, None))
                if _ent is not None:
                    rec["doc_id"] = _ent.sfile_id
                    rec["doc_id_source"] = "sfile_id"
                    docid_stats["sfile_id"] = docid_stats.get("sfile_id", 0) + 1
                    if _how == "case":
                        rec["rematched_by"] = "case"
                        docid_stats["case"] = docid_stats.get("case", 0) + 1
                else:
                    rec["doc_id"] = None
                    rec["doc_id_source"] = "path"
                    docid_stats["path"] = docid_stats.get("path", 0) + 1
                    missing_id_rows.append(rec)
            if auto_prop:
                records.append(rec)
                rec_origins.append(None)
            else:
                _ov(rec)
                _finalize_doctype(rec)
                _emit(rec)
            if show_progress:
                print(f"[progress] {i}/{total} {path}", file=sys.stderr, flush=True)
            continue
        # src=실제 읽을 경로(압축 내부면 임시파일), path(label)=표시·규칙신호용(압축경로/내부경로),
        # origin=이 파일이 나온 최상위 압축경로(일반 파일이면 None).
        src, path, origin = _as_job(item)
        # 압축 안에서 나온 문서는 등급 스트림 대상이 아니다 — 표시 경로를 기억해 둔다.
        if grade_marker is not None and origin is not None:
            grade_marker.members.add(path)
        # 엠파워 v11 문서면 (원래 이름, 도구를 불렀는가). 파일명 신호에는 원래 이름을 쓴다 —
        # 저장 이름(F_….SMF)에는 단서가 없다. 결과의 file 칸은 언제나 path 다.
        _dm = dec_ctx.meta.get(path) if dec_ctx is not None else None
        sig_path = _dm[0] if (_dm and _dm[0]) else path
        # 파일별 스톱워치 — 추출/정제/규칙/(임베딩) 단계별 소요시간을 잰다(--embed 와 동일 형식).
        timing = Timing()

        # 분할 압축의 '두 번째 이후 조각'은 압축으로 감지조차 되지 않아 일반 파일로
        # 흘러든다. 그대로 두면 뜻 없는 이진 덩어리를 본문으로 읽어 분류한다 —
        # 실측에서 5조각 7z 의 세 번째 조각이 'text' 로 감지됐다. 읽기 전에 막고
        # 무엇인지 분명히 남긴다. 첫 조각은 압축 확장 단계가 이미 사유를 달았으므로
        # 여기서 또 세지 않는다.
        # 표시용 라벨(path)로 이름을 보고, 실제 경로(src)로 내용을 본다 —
        # 분할 zip 의 마지막 조각은 이름이 평범한 .zip 이라 내용을 봐야 안다.
        _vol = archive_split_hint(path, src)
        if _vol and path not in arch_unexpanded:
            rec = _extract_failed_record(
                sig_path, f"분할 압축의 조각입니다({_vol}) — 조각 하나만으로는 내용을 "
                      f"꺼낼 수 없어 읽지 않았습니다. 이 도구는 분할 압축을 잇지 않습니다.",
                ruleset, doc_rules_set, taxonomy)
            _apply_dec_meta(rec, path, _dm)
            rec["error"]["kind"] = "split_volume"
            n_split_volume += 1
            code = config.EXIT_EXTRACT_FAIL
            counts["none"] = counts.get("none", 0) + 1
            n_extract_failed += 1
            if docid_on:
                _attach_doc_id(rec, path, src, docid_flist, docid_stats)
                if rec.get("doc_id_source") != "sfile_id":
                    missing_id_rows.append(rec)
            if auto_prop:
                records.append(rec)
                rec_origins.append(origin)
            else:
                _ov(rec)
                _finalize_doctype(rec)
                _emit(rec)
                if origin is not None:
                    arch_members.setdefault(origin, []).append(_member_grade(rec))
                    _collect_archive_doctype(arch_doctypes, origin, rec)
            if show_progress:
                print(f"[progress] {i}/{total} {path}", file=sys.stderr, flush=True)
            continue

        try:
            #----------------------------------------------------------------------------
            # **(1) 문서text 추출**
            #----------------------------------------------------------------------------
            #=> 문서포멧감지(detected_format)->포멧문서파서로 text 추출
            #=> 분류에서는 원문 텍스트를 (기본은) 디스크에 남기지 않는다(민감정보 잔존 방지).
            with timing.measure("extract"):
                # 파서가 남길 '부분 추출' 표식 자리를 비우고 시작한다(G5 관측).
                notes.reset()
                # PDF 페이지 구성 칸도 문서마다 비운다 — 지난 PDF 의 값이 이 문서에 붙으면
                # PDF 가 아닌 문서가 처리제외로 잘못 가려진다.
                notes.reset_layout()
                raw = extractor.extract(src, save_dir=None)
                partial = notes.take()
                layout = notes.take_layout()

            #----------------------------------------------------------------------------
            # 추출된 text 정제
            #----------------------------------------------------------------------------
            with timing.measure("clean"):
                text = clean_text(raw)
                #----------------------------------------------------------------------
                # 글자수 상한(G3) — 설계: plan/문서크기-상한-설계-20260904.html
                #----------------------------------------------------------------------
                # 여기서 자르는 이유: 이 아래로 이어지는 세 가지 비용 — PII 정규식
                # 전문 스캔(실측 전체 시간의 66%) · 임베딩 청크 수 · 데몬 IPC
                # 페이로드(16MB 상한) — 이 전부 이 길이 하나로 결정된다. 한 곳에서
                # 유계로 만들면 셋이 함께 유계가 된다.
                # 버리는 게 아니라 앞부분만 보는 것이며, 표식은 레코드가 만들어진
                # 뒤에 단다(_mark_truncated).
                text, n_text_orig, was_truncated = truncate_text(text, text_limit)

        except ExtractError as e:
            # 스캔 PDF 는 대개 이 갈래로 온다 — 모든 쪽이 0자면 pdfium 결과가 '빈 문자열'이라
            # 하이브리드가 짧은 결과로 붙잡지 못하고 전 엔진 실패로 올린다(실측: 사이냅도
            # 0바이트). 그래도 파서가 남긴 페이지 구성이 스캔본이면 '실패'가 아니라 '처리제외'다.
            layout = notes.take_layout()
            scan = None if isinstance(e, SizeLimitError) else _scan_pdf_excluded(layout, 0)
            if scan:
                print(f"[MpowerClassify] 처리제외(스캔 PDF, {scan['pages']}쪽 중 "
                      f"{scan['scan_pages']}쪽이 그림): {path}", file=sys.stderr)
                n_excluded += 1
            # 크기 초과(G1·G2)는 '실패'가 아니라 '안 읽기로 한 것'이라 말투를 나눈다.
            # 같은 문장으로 찍으면 운영자가 파일이 깨진 줄 알고 원본을 뒤진다.
            elif isinstance(e, SizeLimitError):
                print(f"[MpowerClassify] 크기 상한 초과로 건너뜀: {path} :: {e}", file=sys.stderr)
                log.warning("크기 상한 초과 file=%s :: %s", path, e)
                n_size_skipped += 1
            else:
                print(f"[MpowerClassify] 추출 실패: {path} :: {e}", file=sys.stderr)
                log.error("분류 추출 실패 file=%s :: %s", path, e)
            # 처리제외는 실패가 아니므로 종료코드를 올리지 않는다.
            if not scan:
                code = config.EXIT_EXTRACT_FAIL
            # 못 읽은 문서도 '결과'다 — 레코드를 안 내면 그 문서는 결과에서 통째로
            # 사라져, 화면에는 "18개 중 11건"처럼 조용히 줄어든 숫자만 남는다.
            # 무엇이 왜 빠졌는지 알 수 없는 것은 거버넌스 도구에서 가장 나쁜 실패다.
            # 등급 없이(=보류) 실패 사유를 실어 내보내 사람이 처리하게 한다.
            rec = _extract_failed_record(sig_path, e, ruleset, doc_rules_set, taxonomy,
                                         excluded=scan)
            _apply_dec_meta(rec, path, _dm)
            # 못 펼친 압축은 '본문 추출'까지 실패해 이 갈래로 오는 일이 흔하다
            # (분할 7z 처럼 사이냅도 못 읽는 경우). 표식을 성공 경로에만 달면
            # 정작 필요한 이 자리에서 빠져, 요약은 세는데 레코드는 비는 상태가 된다.
            if path in arch_unexpanded:
                _reason, _kind = arch_unexpanded[path]
                _mark_archive_unexpanded(rec, _reason, _kind)
            # 못 읽은 문서에도 식별자는 단다 — 그 문서도 사람이 손볼 '결과'라
            # 나중에 수정을 이으려면 키가 필요하다(내용을 못 읽으면 경로 해시로 내려간다).
            if docid_on:
                _attach_doc_id(rec, path, src, docid_flist, docid_stats)
                if rec.get("doc_id_source") != "sfile_id":
                    missing_id_rows.append(rec)
            if scan:
                # 처리제외는 실패가 아니다 — 파일명·경로 신호로 정해진 등급 칸에 그대로 센다.
                _g = rec.get("grade") or "none"
                counts[_g] = counts.get(_g, 0) + 1
            else:
                counts["none"] = counts.get("none", 0) + 1
                n_extract_failed += 1
            if auto_prop:
                records.append(rec)
                rec_origins.append(origin)
            else:
                _ov(rec)
                _finalize_doctype(rec)
                _emit(rec)
                if origin is not None:
                    arch_members.setdefault(origin, []).append(_member_grade(rec))
                    _collect_archive_doctype(arch_doctypes, origin, rec)
            # 실패해도 진행수는 늘려 UI 바가 멈추지 않게 한다.
            if show_progress:
                print(f"[progress] {i}/{total} {path}", file=sys.stderr, flush=True)
            continue

        #----------------------------------------------------------------------------
        # **(2) CSO 보안등급 & 업무분류 1차 분류**
        #----------------------------------------------------------------------------
        # => build_record() 함수가 다섯 신호(규칙·민감정보·스탬프·경로·파일명)를 스캔·융합하여
        #    CSO 보안등급 1차 분류를 완성한다(cso_rule.yaml 기반).
        #    동시에 doc_rules 와 taxonomy 가 '둘 다' 있으면 _attach_doctype() 함수 내에서
        #    scan_doctype() 을 통해 업무분류 1차 분류도 수행한다(doc_rule.yaml 기반).
        #
        #   [CSO 보안등급 규칙 스캔]
        #    - 규칙(내용·민감정보·스탬프·경로·파일명)으로 등급을 낸다(임베딩은 관여 안 함)
        #    - --vector-only(rules_enabled=False)면 규칙을 건너뛰고 '보류' 레코드만 만든다
        #      → 등급은 아래 전파 단계가 seed 비교로 정한다
        #    - --with-pii 면 검출된 원문 PII 값도 레코드에 싣는다(기본 off=미저장)
        #
        #   [업무분류]
        #    - doc_rule.yaml 규칙으로 스캔하여 다중 라벨 후보를 산출
        #    - doc_rules_set 또는 taxonomy 중 하나라도 없으면 labels.doctype 키 자체를 생략
        with timing.measure("rule"):
            rec = build_record(sig_path, text, ruleset, ts=now_iso(),
                               failsafe=args.failsafe, vector=None,
                               with_pii=getattr(args, "with_pii", False),
                               rules_enabled=rules_enabled,
                               doc_rules=doc_rules_set, taxonomy=taxonomy)
            _apply_dec_meta(rec, path, _dm)
            
        # 본문을 사실상 못 읽었으면 표식을 단다(분류 결과 자체는 건드리지 않는다).
        # 종료코드도 추출 실패(1)로 올린다 — 배치가 "이 실행에 손봐야 할 문서가
        # 있다"를 종료코드만으로 알 수 있어야 한다.
        # 본문 일부만 보고 판정했으면 표식을 단다(분류 결과 자체는 건드리지 않는다).
        # 추출 실패와 달리 종료코드는 올리지 않는다 — 등급은 정상적으로 나왔고,
        # '전문을 못 봤다'는 것은 실패가 아니라 재검토 대상이라는 뜻이다.
        if was_truncated:
            _mark_truncated(rec, n_text_orig, len(text))
            n_text_truncated += 1
            log.info("본문 절단 file=%s %d자 → %d자(상한)", path, n_text_orig, len(text))

        # 파서가 상한에서 멈춰 끝까지 읽지 못했으면 그 사실도 남긴다(G5).
        # 절단(G3)과 따로 세는 이유: G3 은 '다 읽고 나서 잘랐다', 이건 '애초에
        # 끝까지 못 읽었다' — 재검토 시 손볼 곳(상한값)이 서로 다르다.
        if partial:
            _mark_partial_extract(rec, partial)
            n_partial_extract += 1

        # 압축을 펼치지 못했으면 그 압축 레코드에 표식을 단다(G4).
        # 이게 없으면 내부 문서 수백 건이 사라진 것이 결과에 전혀 드러나지 않는다.
        if path in arch_unexpanded:
            _reason, _kind = arch_unexpanded[path]
            _mark_archive_unexpanded(rec, _reason, _kind)

        # PII 스캔 상한(초선형 검출기: 전화·주소·계좌)에 걸렸는지 본다. 본문 절단과
        # 다른 층이라 따로 센다 — G3 에 안 걸린 문서도 여기 걸릴 수 있다.
        _capped, _cov, _tot = superlinear_coverage(text, {r.label for r in ruleset.regex_rules})
        if _capped:
            _mark_pii_scan_capped(rec, _cov, _tot)
            n_pii_capped += 1
            log.info("PII 스캔 상한 file=%s %d자 중 %d자만 훑음", path, _tot, _cov)

        short, n_chars = _body_too_short(text)
        # 스캔 PDF 는 먼저 가린다 — '못 읽음(실패)'이 아니라 '처리제외(범위 밖)'라서
        # error 도 달지 않고 종료코드도 올리지 않는다. 본문 길이가 20자를 넘는 스캔본
        # (머리말 몇 줄만 글자로 든 문서)도 있어 short 와 무관하게 판정한다.
        scan = _scan_pdf_excluded(layout, n_chars)
        if scan:
            _mark_excluded(rec, scan)
            n_excluded += 1
            print(f"[MpowerClassify] 처리제외(스캔 PDF, {scan['pages']}쪽 중 "
                  f"{scan['scan_pages']}쪽이 그림): {path}", file=sys.stderr)
        elif short:
            _mark_no_body(rec, n_chars)
            n_extract_failed += 1
            code = config.EXIT_EXTRACT_FAIL
            print(f"[MpowerClassify] 본문 텍스트 없음({n_chars}자): {path}"
                  f" — 스캔본(이미지)일 수 있습니다", file=sys.stderr)

        # 업무분류 집계는 여기서 하지 않는다 — 전파(auto_prop)가 라벨을 더 붙일 수
        # 있어서, 지금 세면 전파 전 숫자가 요약에 박힌다. 레코드가 '최종'이 되는
        # 출력 직전에 _finalize_doctype() 으로 센다.

        #----------------------------------------------------------------------------
        # 문서해쉬값
        # =>인자가 --hash/--simple 인 경우에만 문서 '내용'의 해시를 레코드에 싣는다(읽기 실경로 src 기준).
        #----------------------------------------------------------------------------
        # 문서 식별자(doc_id·key) — 설계 §7-5. 해시 계산보다 먼저 달아 둔다.
        if docid_on:
            _attach_doc_id(rec, path, src, docid_flist, docid_stats)
            if rec.get("doc_id_source") != "sfile_id":
                missing_id_rows.append(rec)

        # 지문 — _attach_doc_id 가 이미 구해 뒀으면 그대로 쓴다(두 번 해싱 금지).
        if need_hash and not rec.get("hash"):
            rec["hash"] = _file_hash(src)

        #----------------------------------------------------------------------------
        # 추출 본문 보존(--textsave) — 설계: plan/추출텍스트-저장-설계-20260907.html
        #----------------------------------------------------------------------------
        # 해시를 단 바로 뒤에 부른다: 파일 이름이 그 해시이기 때문이다.
        # 저장하는 text 는 '정제·절단까지 끝난' 값 — 도구가 실제로 보고 판정한
        # 바로 그 텍스트여야 나중에 근거를 되짚을 때 어긋나지 않는다(설계 6장).
        if text_save_dir:
            # [2026-09-10] ts 는 meta 안으로 들어갔다. 예전 코드가 rec["ts"] 를
            # 그대로 읽어 늘 None 이 나왔고, 색인에 '레코드 시각' 대신 '저장 시각'이
            # 조용히 들어가고 있었다 — 오류가 안 나서 못 알아챘다.
            _st = _save_text_file(text_save_dir, rec, text, path, was_truncated,
                                  record.meta_of(rec).get("ts") or now_iso())
            if _st == "saved":
                n_text_saved += 1
            elif _st == "dedup":
                n_text_saved += 1
                n_text_dedup += 1
            elif _st == "error":
                n_text_save_failed += 1

        #----------------------------------------------------------------------------
        # ** (3) 벡터 생성 **        
        #----------------------------------------------------------------------------
        #  => 벡터 임베딩이 필요한지 판단 (비싼 작업이므로 필요할 때만 수행)
        #    기본 모드: --auto-propagate 켜짐 && 규칙 분류 사용 → "needed" 모드가 기본
        #
        #    · "all" 모드: 항상 임베딩 (--with-vector 또는 seed 생성 시)
        #    · "needed" 모드(**기본): 다음 중 하나 만족 시만 임베딩 (선택적 = 비용 절감)
        #      - 보안등급 미정(grade=None): 규칙이 등급을 못 정한 경우
        #      - seed 전파 후보(seed_eligible=True): cso_rule.yaml에서 marked
        #      - 문서타입 미정: doc_rule.yaml에서 라벨은 있지만 선택지가 비어있음
        #        예: "doctype": {"values": []} ← 규칙이 후보를 찾지 못함
        #
        #    · "none" 모드: 임베딩 안 함 (--rule-only 또는 규칙 분류만 사용할 때)
        #
        #  ⚠️ 중요: 벡터가 없으면 업무분류 seed도 생성 안 됨!
        #    · 업무분류 seed는 '벡터가 있는 문서'에만 들어간다
        #    · 세 조건을 모두 만족 못하면 → need_vec=False → 벡터 생성 안 함
        #    · 벡터 없음 → seed_pool에 추가 안 됨 → 업무분류 seed 최종 미포함
        #
        dt_label = record.doctype_of(rec)

        dt_undecided = dt_label is not None and not dt_label.get("values")
        rsec = rec["why"]["security"]
        need_vec = embed_mode == "all" or (
            embed_mode == "needed"
            and (rec.get("grade") is None or rsec.get("seed_eligible") or dt_undecided))

        if need_vec and embed_mode != "none":
            try:
                # 벡터 생성 (데몬 서버 우선, 실패 시 로컬 폴백)
                rec["vector"] = _embed_text(text, timing)
                embedded += 1
            except Exception as e:
                # 모델 없음 등의 오류: 벡터 없이 계속 진행(결과는 낸다).
                # 첫 실패는 화면·오류 로그에도 남긴다 — 예전에는 일반 로그 warning 뿐이라
                # 모델이 빠진 배포가 아무 흔적 없이 '미분류만 많은 결과'로 끝났다.
                if not embed_errors:
                    print(f"[MpowerClassify] 임베딩 실패 → 벡터 없이 계속합니다: {e}", file=sys.stderr)
                    log.error("분류 임베딩 실패 file=%s :: %s", path, e)
                else:
                    log.warning("분류 임베딩 실패 file=%s :: %s", path, e)
                embed_errors.append(f"{type(e).__name__}: {e}")

        # 옵션: 추출(정제) 텍스트를 결과 레코드에 함께 저장(기본 off — 프라이버시).
        # safe_text 를 거치는 이유: 깨진 문서에서 '짝 없는 대리 문자'가 본문에 섞여
        # 들어오면 결과를 파일에 쓸 때 UnicodeEncodeError 로 배치 전체가 죽는다.
        # 레코드에 넣기 전에 걸러 두면 값 자체도 깨끗해진다(출력단 방어는 그대로 둔다).
        if with_text:
            rec["text"] = safe_text(text)

        # 처리시간(단계별 ms)을 레코드에 부착 — --embed 와 동일 형식(extract/clean/rule/
        # [model_load/embed]/total). --no-timing 이면 생략.
        if do_timing:
            timing.finalize_total()
            em = timing.as_dict()
            # 첫 레코드에는 startup 을 맨 앞 키로 넣고 total 에도 더한다(프로세스 시작~완료 = 실제 체감).
            if startup_pending:
                em = {"startup": round(startup_ms, 2), **em}
                em["total"] = round(em.get("total", 0.0) + startup_ms, 2)
                startup_pending = False
            rec["elapsed_ms"] = em
            totals.append(em.get("total", 0.0))

        counts[rec.get("grade") or "none"] = counts.get(rec.get("grade") or "none", 0) + 1

        # 씨앗 후보 적재 — 지금이 '규칙만으로 판정한 상태'라 씨앗 자격을 보기에 맞다.
        if getattr(args, "make_doctype_seeds", None) and rec.get("vector"):
            seed_pool.append({"file": rec.get("file"), "doctype": rec.get("doctype"),
                              "vector": rec["vector"], "text_len": len(text or "")})

        # 기준 문서 등록이면 제목(첫 줄)을 떠 둔다 — 등록할 때 BM25 재료로 줄에 넣는다
        if getattr(args, "_seed_plan", None):
            args._seed_titles[seedstore_norm_file(rec.get("file") or path)] = \
                bm25mod.title_of(text, sig_path)

        # auto_prop 면 전파 후 출력하려고 모아 두고, 아니면 바로 출력.
        if auto_prop:
            # 2차 대상이면 BM25 에 쓸 제목 · 파일명 경로를 레코드 번호로 남긴다(본문은 남기지 않는다)
            if need_vec and dt_label is not None:
                bm25_fields[len(records)] = (bm25mod.title_of(text, sig_path), sig_path)
            records.append(rec)
            rec_origins.append(origin)   # 압축 집계용 origin 을 레코드와 나란히 보관
        else:
            # 스트리밍 경로는 전파가 없으니 지금이 최종 — 사람 결정을 여기서 적용한다.
            _ov(rec)
            # 스트리밍 경로는 전파가 없으니 지금이 최종 — 여기서 업무분류를 센다.
            _finalize_doctype(rec)
            _emit(rec)
            # 스트리밍 경로는 지금 등급이 최종이라 바로 압축별로 모은다.
            if origin is not None:
                arch_members.setdefault(origin, []).append(rec.get("grade"))
                _collect_archive_doctype(arch_doctypes, origin, rec)

        # 한 파일 처리 완료 → 진행수 갱신(UI 진행바/경과시간용).
        if show_progress:
            print(f"[progress] {i}/{total} {path}", file=sys.stderr, flush=True)

    #----------------------------------------------------------------------------
    # **(옵션) 업부분류를 seed에 추가 한다. **
    # --make_doctype_seeds 옵션된 경우에만 
    #  ⚠️ 중요: 벡터가 없으면 업무분류 seed도 생성 안 됨!
    #    · 업무분류 seed는 '벡터가 있는 문서'에만 들어간다
    #----------------------------------------------------------------------------
    # 옵션: 업무분류 seed 만들기(재설계 10장). 전파보다 '먼저' 한다 —
    # 전파가 라벨을 더하고 나면 어느 라벨이 규칙에서 온 것인지 흐려진다.
    if getattr(args, "make_doctype_seeds", None):
        from .classify import seedgen
        out_path = args.make_doctype_seeds
        if taxonomy is None or doc_rules_set is None:
            print("[MpowerClassify] 업무분류 축이 꺼져 있어 seed 를 만들 수 없습니다 "
                  "(--doc-rules 확인)", file=sys.stderr)
        else:
            # 업무분류측 class_seed.jsonl 씨드파일을 만든다.
            seeds, sstats = seedgen.select_doctype_seeds(
                seed_pool, taxonomy,
                t_seed=doc_rules_set.defaults.t_seed,
                per_dir=getattr(args, "seed_per_dir", 3),
                per_node=getattr(args, "seed_per_node", 50))

            # class_seed.jsonl 파일 생성.
            merged = seedgen.merge_into(seeds, out_path)

            print(f"[seed][업무분류] 후보 {sstats['입력']} → 채택 {sstats['채택']} "
                  f"(분류 {len(sstats['노드별'])}종) · {out_path}", file=sys.stderr)
            print(f"[seed][업무분류] 기존 보안등급 seed {merged['기존유지']}건 유지 · "
                  f"이전 업무분류 seed {merged['이전doctype제거']}건 교체", file=sys.stderr)

            # 왜 안 뽑혔는지를 남긴다 — 씨앗이 0건일 때 이 줄이 없으면 원인을 못 찾는다.
            if sstats["탈락사유"]:
                why = " ".join(f"{k}={v}" for k, v in
                               sorted(sstats["탈락사유"].items(), key=lambda x: -x[1]))
                print(f"[seed][업무분류] 탈락 사유: {why}", file=sys.stderr)

    # --vector-only 인데 모델을 못 올렸다(파일은 있는데 로드 실패 — 깨진 모델·런타임 불일치).
    # 앞의 값싼 확인을 통과한 드문 경우다. 이 모드의 결과는 전부 미분류라 내보내지 않는다.
    # (auto_prop 이 늘 켜진 모드라 아직 한 건도 출력하지 않은 상태다.)
    if vector_only and embed_errors and not embedded:
        return fail_err("model_load_failed",
                        f"[MpowerClassify] --vector-only 인데 임베딩 모델을 올리지 못했습니다 — "
                        f"{embed_errors[0]}")

    #----------------------------------------------------------------------------
    # **(4) 보류문서들 전파(seed 비교)
     #----------------------------------------------------------------------------
    # => 외부용 class_seed.json 파일과, cse_rule.yaml에 seed 문서들이 벡터값을 읽어와서 비교함.
    # 옵션: 분류 직후 보류 문서를 seed 로 전파해 구제(단일 문서 확인에도 유용).
    if auto_prop:
        from .classify.propagate import SeedIndex
        from .classify.engine import propagate_records
        seed_index = None

        # 외부 class_seed.jsonl 파일 읽어오기
        if seeds_path and os.path.isfile(seeds_path):
            seed_index = SeedIndex.from_seed_file(seeds_path)
            print(f"[MpowerClassify] 외부 seed {seed_index.size}건 로드: {seeds_path}", file=sys.stderr)
        elif args.seeds:
            # 사용자가 --seeds 로 명시했는데 파일이 없을 때만 알린다(내부 seed 로 진행).
            print(f"[MpowerClassify] seed 파일 없음(내부 seed 만 사용): {args.seeds}", file=sys.stderr)

        #----------------------------------------------------------
        # 2차 패스 진행 알림 만들기
        #=> 추출·1차 분류는 파일마다 [progress] 를 내는데, 그 뒤 전파 단계는
        #   여태 아무 말이 없었다. 문서가 수천 건이면 "멈춘 건가" 를 알 수 없다.
        #   같은 [progress] 로 내되 단계 이름을 붙여 어느 패스인지 알게 한다
        #   (진행바는 [progress] 로 시작하는 줄만 보므로 그대로 움직인다).
        #
        # -in: label = 단계 이름(예: "전파·업무분류")
        #
        # -out: fn = f(본수, 전체, 경로). --progress 가 아니면 None
        # -out: error = 없음
        #----------------------------------------------------------
        def _prop_progress(label):
            if not show_progress:
                return None

            def _tell(done, total, path):
                print(f"[progress][{label}] {done}/{total} {path}",
                      file=sys.stderr, flush=True)
            return _tell

        # 내부 seed 읽기(cso_rule.yaml)
        # => cso_rule.yaml에서 고신뢰(seed_eligible) 설정된 경우에 대해 읽어옴
        records, pstats = propagate_records(records, seed_index=seed_index, failsafe=args.failsafe,
                                            on_progress=_prop_progress("전파·보안등급"))

        #-------------------------------------------------------------
        # **업무분류 전파**
        # => doc_rule.yaml(분류체계 내장)이 있는 경우에만 실행.
        # 업무분류 축도 같은 seed 저장소로 전파한다. security 전파와 두 가지가 다르다:
        #   · 대상 — security 는 '보류 문서만' 구제하지만, doctype 은 이미 라벨이
        #     있는 문서에도 후보를 '더한다'(한 문서가 여러 분류에 동시에 맞을 수 있다).
        #-------------------------------------------------------------
        if doc_rules_set is not None and taxonomy is not None:

            from .classify.propagate import DoctypeSeedIndex
            from .classify.engine import propagate_doctype_records

            # 외부 class_seed.jsonl 파일 읽어오기
            dt_seeds = DoctypeSeedIndex.from_seed_file(seeds_path)
            emb = doc_rules_set.embed
            # BM25 2차 재료(색인 · 자동 문턱) — bm25.enabled 가 아니면 None
            bm = _bm25_context(doc_rules_set, seeds_path, emb.dup_threshold, bm25_fields)

            if dt_seeds.size and not emb.enabled and (bm is None or bm["threshold"] is None):
                # seed 는 있는데 스위치가 꺼져 있는 상태. 조용히 넘어가면
                # "왜 벡터가 안 도는지" 를 아무도 못 찾는다.
                print(f"[전파][업무분류] seed {dt_seeds.size}건이 있으나 "
                      f"doc_rule.yaml 의 embed.enabled 가 false 라 2단계를 건너뜁니다",
                      file=sys.stderr)
            elif dt_seeds.size:
                # 벡터 단독 후보 상한은 defaults.embed_cap, kNN 임계값은 embed 블록이
                # 정한다. 여태 이 값들은 파일에 적혀 있어도 쓰이지 않았다.
                records, dt_stats = propagate_doctype_records(
                    records, dt_seeds, taxonomy, doc_rules_set.conflict,
                    embed_cap=doc_rules_set.defaults.embed_cap,
                    on_progress=_prop_progress("전파·업무분류"),
                    use_embed=emb.enabled, bm25=bm, **emb.kwargs())
                print(f"[전파][업무분류] seed={dt_stats['seeds']} "
                      f"embed기여={dt_stats['embed_contributed']} "
                      f"벡터없음={dt_stats['no_vector']}", file=sys.stderr)
                if bm is not None and bm["threshold"] is not None:
                    print(f"[전파][업무분류][bm25] 후보={dt_stats['bm25_picked']} "
                          f"합의={dt_stats['bm25_agree']} 단독={dt_stats['bm25_alone']} "
                          f"파일명단독={dt_stats['bm25_name_only']}", file=sys.stderr)
            # seed 가 없는 경우의 안내는 위(모드 결정 직후)에서 이미 냈다 — 그쪽은
            # auto_prop 이 아예 꺼진 경우까지 잡아 주므로 여기서 또 내지 않는다.

        # 전파로 등급이 바뀌었을 수 있으니 분포를 다시 집계한 뒤 출력.
        counts = {"C": 0, "S": 0, "O": 0, "none": 0}
        for rec, origin in zip(records, rec_origins):
            # 전파까지 끝난 뒤 사람 결정을 적용한다(건수는 바로 아래에서 새로 센다).
            _ov(rec, adjust=False)
            g = record.grade_of(rec)
            counts[g or "none"] = counts.get(g or "none", 0) + 1
            # 전파까지 끝난 지금이 최종 — 여기서 업무분류를 센다(전파로 붙은 라벨 포함).
            _finalize_doctype(rec)
            _emit(rec)
            # 전파 후가 최종 등급이므로 여기서 압축별로 모은다.
            if origin is not None:
                arch_members.setdefault(origin, []).append(g)
                _collect_archive_doctype(arch_doctypes, origin, rec)
        print(
            f"[propagate] seeds={pstats['seeds']} already_graded={pstats['already_graded']} "
            f"embed_decided={pstats['embed_decided']} "
            f"still_unclassified={pstats['still_unclassified']} no_vector={pstats['no_vector']}",
            file=sys.stderr,
        )

    # (0) 압축파일 '자체'의 집계 등급 레코드를 내부 파일들 뒤에 덧붙인다(요청 옵션).
    #     내부 파일 중 최고 위험(C>S>O)을 압축파일 등급으로 삼아, 압축만 봐도 위험도를 안다.
    #     이 집계 레코드는 아래 등급 카운트(내부 파일 기준)에는 넣지 않는다(중복 방지).
    arch_recs = []
    if arch_members:
        ts = now_iso()
        for origin, grades in arch_members.items():
            arec = _archive_record(origin, grades, ts, arch_doctypes.get(origin))
            # --hash/--simple 이면 압축파일 자체의 해시도 함께 싣는다(origin=압축파일 실경로).
            if need_hash:
                arec["hash"] = _file_hash(origin)
            # 압축 파일 자체에 사람 결정이 있으면 그것이 이긴다.
            _ov(arec, adjust=False)
            arch_recs.append(arec)
            _emit(arec)

    # 목록에 있으나 없는 파일 — 결과에도 한 줄씩 남긴다(F4 를 stderr 에만 두지 않는다).
    # 목록이 대상을 정한 실행에서만 한다. --dir 과 함께 준 경우 목록은 ID 사전일 뿐이라
    # 그 바깥 항목을 '빠졌다'고 하면 엉뚱한 경고가 된다.
    n_missing_file = 0
    if getattr(args, "_filelist_is_target", False):
        _fl = getattr(args, "_filelist", None)
        for _e in (_fl.missing_entries() if _fl is not None else ()):
            mrec = _missing_file_record(_e.path, _e.sfile_id, ruleset,
                                        doc_rules_set, taxonomy)
            n_missing_file += 1
            counts["none"] = counts.get("none", 0) + 1
            _emit(mrec)
        if n_missing_file:
            # 추출 실패와 같은 급으로 종료코드를 올린다 — 부르는 쪽이 결과를
            # 파싱하지 않고 종료코드만 봐도 '요청한 만큼 안 됐다'를 알아야 한다.
            code = config.EXIT_EXTRACT_FAIL
            print(f"[MpowerClassify] 목록에 있으나 파일이 없어 처리하지 못함: "
                  f"{n_missing_file}건 — 결과에 error.kind=file_missing 으로 남겼습니다",
                  file=sys.stderr)

    # 최종 요약 집계 — 총수/검출수(C+S+O)/등급별/미분류, 그리고 프로세스 시작~지금까지의
    #   '총 소요시간'(startup 포함 = exe 실행부터 여기까지의 실제 체감 시간).
    c_cnt = counts.get("C", 0)
    s_cnt = counts.get("S", 0)
    o_cnt = counts.get("O", 0)
    none_cnt = counts.get("none", 0)
    # 없는 파일도 '다룬 문서'로 센다 — 요청받은 문서이기 때문이다. total 에서 빼면
    # 부르는 쪽이 "7건 요청했는데 total 이 1" 인 것을 눈치챌 수단이 사라진다.
    total_files = (n_jobs or len(files)) + n_missing_file
    # 복호화 집계(엠파워 v11) — 쓴 실행에서만 값이 있다.
    n_dec = dec_ctx.n_dec if dec_ctx is not None else 0
    n_dec_fail = dec_ctx.n_fail if dec_ctx is not None else 0
    detected = c_cnt + s_cnt + o_cnt                      # 등급이 매겨진(=검출된) 문서 수
    total_ms = round((time.perf_counter() - PROCESS_START_PERF) * 1000.0, 1)
    # 상태 줄에 실을 건수를 남겨 둔다 — '16건 중 1건 실패'를 숫자로 알려 준다.
    # 상태 줄의 '실패' 건수에는 없는 파일도 넣는다 — 부르는 쪽에는 둘 다
    # "요청했는데 결과를 못 받은 문서"로 같은 뜻이다.
    # 임베딩이 필요했는데 한 건도 못 했다 = 모델·런타임을 못 올렸다. 결과는 그대로
    # 내되 종료코드를 2(3002 model_load_failed)로 올린다 — 예전에는 exit 0·success 로
    # 끝나, --vector-only 에서 전 문서가 미분류인데도 부르는 쪽은 성공으로 읽었다.
    # 일부만 실패한 것은 문서 문제라 올리지 않는다(Rust 판과 같은 기준).
    if embed_errors and not embedded:
        errcodes.set_status("model_load_failed",
                            f"[MpowerClassify] 임베딩 모델을 올리지 못해 임베딩·전파를 "
                            f"건너뛰었습니다 — {embed_errors[0]}")
        code = errcodes.exit_of("model_load_failed")
    # 풀지 못한 문서가 있으면 그것을 까닭으로 알린다(종료코드는 같은 1 이고 번호만 3005 다).
    # 모델 실패(2)가 이미 잡혀 있으면 그쪽이 우선이다 — 대응이 배치 전체에 걸린다.
    if n_dec_fail and code != errcodes.exit_of("model_load_failed"):
        errcodes.set_status("decrypt_failed",
                            f"[MpowerClassify] 복호화하지 못한 문서 {n_dec_fail}건 — "
                            f"결과에 error.kind=decrypt_failed 로 남겼습니다")
        code = errcodes.exit_of("decrypt_failed")
    # 등급 스트림을 못 쓴 문서가 있으면 알린다(4002, 종료코드 1). 결과 파일은 온전하다.
    # 다른 실패가 이미 있으면 그쪽 까닭을 그대로 둔다 — 건수는 요약 grade_stream 에 남는다.
    if grade_marker is not None and grade_marker.counts["failed"] and code == 0:
        errcodes.set_status("grade_stream_write_failed",
                            f"[MpowerClassify] 등급 스트림을 쓰지 못한 문서 "
                            f"{grade_marker.counts['failed']}건 — 결과의 grade_stream.reason 을 보세요")
        code = errcodes.exit_of("grade_stream_write_failed")
    errcodes.set_counts(total_files, n_extract_failed + n_missing_file + n_dec_fail)
    # 정책 버전도 함께 — 결과만 있고 '어떤 규칙으로 판정했는지'가 없으면 나중에
    # 재현할 수 없다(--simple --nosummary 면 지금까지 어디에도 안 남았다).
    errcodes.set_versions(
        rule_version=ruleset.version,
        taxonomy_version=taxonomy.exported_at if taxonomy else None,
        doctype_rule_version=doc_rules_set.version if doc_rules_set else None)
    summary = {
        "total": total_files, "detected": detected,
        "C": c_cnt, "S": s_cnt, "O": o_cnt, "unclassified": none_cnt,
        # 본문을 못 읽은 문서 수. Rust 판 요약에는 있었는데 파이썬 판에는 빠져 있어,
        # 같은 실행인데 두 판의 요약 칸이 달랐다(2026-09-01 맞춤).
        "extract_failed": n_extract_failed,
        "elapsed_total_ms": total_ms, "rule_version": ruleset.version, "embedded": embedded,
    }
    # 목록에 있으나 없던 파일 — 있을 때만 싣는다(0건이 정상이라 칸이 늘면 헷갈린다).
    if n_missing_file:
        summary["file_missing"] = n_missing_file
    # 복호화 — 쓴 실행에서만 싣는다(엠파워 v11 암호화 문서).
    if n_dec or n_dec_fail:
        summary["decrypted"] = n_dec
        summary["decrypt_failed"] = n_dec_fail
    # 글자수 상한에 걸린 문서가 '있을 때만' 요약에 싣는다 — 0건일 때 칸이 늘면
    # 기존 요약을 읽던 스크립트·눈이 괜히 흔들린다(doctype 칸과 같은 방식).
    if n_text_truncated:
        summary["text_truncated"] = n_text_truncated
    # PII 스캔 상한도 있을 때만 싣는다(0건이 정상).
    if n_pii_capped:
        summary["pii_scan_capped"] = n_pii_capped
    # 크기 상한에 걸린 문서도 같은 이유로 있을 때만 싣는다. extract_failed 안에도
    # 포함돼 있지만, 그중 몇 건이 '크기 때문'인지를 따로 알아야 대응을 정할 수 있다.
    if n_size_skipped:
        summary["size_skipped"] = n_size_skipped
    if n_split_volume:
        summary["split_volume"] = n_split_volume
    if n_partial_extract:
        summary["partial_extract"] = n_partial_extract
    # 처리제외는 extract_failed 에 들어 있지 않다(실패가 아니다) — 따로 싣는다.
    if n_excluded:
        summary["excluded"] = n_excluded
    if arch_unexpanded:
        summary["archive_unexpanded"] = len(arch_unexpanded)
    # 압축파일 집계 레코드가 있으면 개수도 요약에 표기(내부 파일 total 과는 별개).
    if arch_recs:
        summary["archives"] = len(arch_recs)
    # 등급 스트림 — 옵션을 켠 실행에서만 싣는다(끈 실행의 요약은 글자 하나 다르지 않다).
    if grade_marker is not None:
        summary["grade_stream"] = grade_marker.summary()
    # 사람 결정 — --grade-overrides 를 준 실행에서만.
    if ov_applier is not None:
        summary["grade_overrides"] = ov_applier.summary()
    # doctype 축이 이번 실행에서 한 건이라도 돌았을 때만 요약에 싣는다(4-6).
    if doctype_total_docs:
        summary["doctype"] = {
            "total": doctype_total_docs,
            "unclassified": doctype_unclassified,
            "by_root": doctype_root_totals,
            "doctype_rule_version": doc_rules_set.version if doc_rules_set else None,
            "taxonomy_version": taxonomy.exported_at if taxonomy else None,
        }

    # (1) 결과 파일(json/jsonl)에도 맨 마지막에 요약을 JSON 으로 남긴다.
    #     json 배열이면 마지막 원소로, jsonl 이면 마지막 줄로 들어간다({"summary": {...}}).
    #     단 --nosummary 면 이 요약 레코드를 출력에서 뺀다(파일별 레코드만 남긴다).
    if args.fmt in ("json", "jsonl") and not no_summary:
        writer.write_record({"summary": summary})
    # 감사용 전체 파일에는 --nosummary 와 무관하게 요약을 남긴다 — 이 파일은
    # 연동용이 아니라 '나중에 되짚어 보는' 파일이라 정책 버전이 반드시 있어야 한다.
    if full_writer is not None and args.fmt in ("json", "jsonl"):
        full_writer.write_record({"summary": summary})
    # (1-2) --out 으로 저장할 때는 상태도 파일 안에 남긴다. 파일만 받아 나중에 읽는
    #     쪽은 stdout 을 이미 흘려보낸 뒤라, 파일 자체가 "이 결과가 온전한가"를
    #     말해 줘야 한다. summary 와 같은 자리(json 은 배열 마지막 원소, jsonl 은
    #     마지막 줄)라 새 규약이 아니다. --out 이 없으면 결과가 stdout 으로 나가고
    #     상태 줄도 거기 붙으므로 여기서 또 넣지 않는다(같은 줄이 두 번 나간다).
    if errcodes.is_json() and args.out and args.fmt in ("json", "jsonl"):
        writer.write_record(errcodes.status_object(code, args.file or args.dir))
    # json 배열 모드면 마지막에 ']' 로 닫아 유효한 JSON 파일을 완성한다.
    writer.close()
    if full_writer is not None:
        if errcodes.is_json():
            full_writer.write_record(errcodes.status_object(code, args.file or args.dir))
        full_writer.close()
        full_fp.close()
        print(f"[MpowerClassify] 감사용 전체 결과: {full_path}", file=sys.stderr)

    # (1-b) --report-missing-id — sfile_id 를 못 얻은 문서를 따로 뽑아 둔다.
    #       이 문서들은 매핑 테이블 적재 대상이 아니므로(R14), 왜 ID 를 못 얻었는지
    #       사람이 확인할 수 있게 목록으로 남긴다.
    if getattr(args, "report_missing_id", None) and missing_id_rows:
        try:
            with open(args.report_missing_id, "w", encoding="utf-8") as mf:
                for r in missing_id_rows:
                    mf.write(output.dumps_safe(
                        {"file": r.get("file"),
                         "doc_id": r.get("doc_id"),
                         "doc_id_source": r.get("doc_id_source")},
                        separators=(",", ":")) + "\n")
            print(f"[MpowerClassify] 문서 ID 미획득 목록: {args.report_missing_id} "
                  f"({len(missing_id_rows)}건)", file=sys.stderr)
        except OSError as e:
            # 본 작업은 끝난 뒤라, 리포트를 못 썼다고 결과까지 버릴 이유는 없다.
            print(f"[MpowerClassify] 미획득 목록을 쓰지 못했습니다: {e}", file=sys.stderr)

    # (2) 화면(stderr) 최종 요약 — 사용자가 요청한 형식(총수/검출/등급별/미분류/총시간).
    #     --nosummary 면 이 화면 요약 줄도 내지 않는다(summary 를 마지막 출력에서 완전 제거).
    if not no_summary:
        # 문서 ID 획득 현황 — 설계 §7-5-2-1. 폴백 건수를 등급 줄보다 먼저 보여 준다.
        # 이 수치가 조용히 커지면 매핑에 들어가지 못하는 문서가 쌓이는데, 아무 오류도
        # 나지 않아 알아채기 어려운 종류의 실패이기 때문이다(R14).
        if docid_on and docid_stats:
            n_sf = docid_stats.get("sfile_id", 0)
            n_content = docid_stats.get("content", 0)
            n_path = docid_stats.get("path", 0)
            print(f"[summary][문서 ID] sfile_id {n_sf} · 폴백(content) {n_content} "
                  f"· 폴백(path) {n_path}", file=sys.stderr)
            n_fallback = n_content + n_path
            if n_fallback:
                hint = ("" if getattr(args, "report_missing_id", None)
                        else " --report-missing-id 로 목록 확인")
                print(f"[summary][문서 ID] ※ 폴백 {n_fallback}건은 매핑 테이블 "
                      f"적재 대상이 아닙니다.{hint}", file=sys.stderr)
            if docid_stats.get("case"):
                print(f"[summary][문서 ID] 대소문자만 달라 목록과 이어진 문서 "
                      f"{docid_stats['case']}건(rematched_by=case)", file=sys.stderr)
            if docid_stats.get("hash_mismatch"):
                print(f"[summary][문서 ID] [F6] 목록의 hash 와 실제 파일이 다른 문서 "
                      f"{docid_stats['hash_mismatch']}건 — 수정된 것으로 보이며 "
                      f"같은 문서로 처리했습니다", file=sys.stderr)
        arch_note = f", 압축 {len(arch_recs)}건" if arch_recs else ""
        # 절단은 0건이 정상이라, 있을 때만 칸을 늘려 눈에 띄게 한다.
        trunc_note = f", 본문절단={n_text_truncated}" if n_text_truncated else ""
        size_note = f", 크기초과={n_size_skipped}" if n_size_skipped else ""
        vol_note = f", 분할조각={n_split_volume}" if n_split_volume else ""
        part_note = f", 부분추출={n_partial_extract}" if n_partial_extract else ""
        excl_note = f", 처리제외={n_excluded}" if n_excluded else ""
        arch_un_note = f", 압축미해제={len(arch_unexpanded)}" if arch_unexpanded else ""
        if n_dec or n_dec_fail:
            arch_un_note += f", 복호화 {n_dec}건(실패 {n_dec_fail})"
        # --textsave 를 안 쓰면 0/0 이라 칸을 아예 안 낸다(쓰는 사람만 보게).
        ts_note = f", 텍스트저장={n_text_saved}" if text_save_dir else ""
        ts_fail_note = f", 저장실패={n_text_save_failed}" if n_text_save_failed else ""
        print(
            f"[summary] 총 {total_files}개 / 검출 {detected}, "
            f"C={c_cnt} S={s_cnt} O={o_cnt} 미분류={none_cnt} "
            f"추출실패={n_extract_failed}{excl_note}{arch_note}{trunc_note}{size_note}"
            f"{part_note}{arch_un_note}{vol_note}{ts_note}{ts_fail_note}, "
            f"총시간={total_ms}ms",
            file=sys.stderr,
        )
        # 사람 결정은 --grade-overrides 를 준 실행에서만 한 줄 더 낸다(Rust 판과 같은 문구).
        if ov_applier is not None:
            _o = ov_applier.counts
            print(f"[summary][사람결정] 적용={_o['applied']} 확인={_o['confirmed']} "
                  f"내용바뀜={_o['stale']} (목록 {_o['loaded']}건)", file=sys.stderr)
        # 등급 스트림은 켠 실행에서만 한 줄 더 낸다(Rust 판과 같은 문구).
        if grade_marker is not None:
            _g = grade_marker.counts
            print(f"[summary][등급스트림] written={_g['written']} unchanged={_g['unchanged']} "
                  f"skipped={_g['skipped']} failed={_g['failed']}", file=sys.stderr)
        # 저장은 곁다리라 결과를 안 바꾸지만, 몇 건이 어디에 남았는지는 말해야 한다.
        # 중복이 합쳐진 건수를 함께 내는 이유: 폴더의 .txt 개수가 문서 수보다
        # 적은 것을 보고 "빠졌나" 의심하지 않게 하려는 것이다.
        if text_save_dir:
            dedup_note = (f"(내용이 같은 {n_text_dedup}건은 한 파일로 합쳐졌습니다)"
                          if n_text_dedup else "")
            print(f"[summary][텍스트 저장] {n_text_saved}건을 {text_save_dir} 에 "
                  f"남겼습니다{dedup_note}. 원본 발췌이므로 공유·백업에 주의하세요.",
                  file=sys.stderr)
        if n_text_save_failed:
            print(f"[summary][텍스트 저장 실패] {n_text_save_failed}건은 남기지 "
                  f"못했습니다 — 분류 결과에는 영향이 없습니다. 사유는 레코드의 "
                  f"text_save_error 를 보세요.", file=sys.stderr)
        # 크기 초과는 '고장'이 아니라 '설정에 걸린 것'이므로, 무엇을 하면 되는지까지 알려 준다.
        if n_size_skipped:
            print(f"[summary][크기 초과] {n_size_skipped}건은 원본이 상한보다 커서 읽지 "
                  f"않았습니다(error.kind=size_limit) — 상한 조정은 --max-file-mb "
                  f"(텍스트 파일은 --max-file-mb-text) 로 합니다.", file=sys.stderr)
        # G5 — 끝까지 못 읽은 문서. 등급은 나왔지만 뒷부분을 안 본 판정이다.
        if n_partial_extract:
            print(f"[summary][부분 추출] {n_partial_extract}건은 파서가 상한에서 멈춰 문서 "
                  f"앞부분만 읽었습니다(partial_extract=true) — 재검토 대상입니다. "
                  f"상한 조정은 --max-pdf-pages · --parser-timeout 으로 합니다.",
                  file=sys.stderr)
        # 처리제외 — 실패가 아니므로 무엇을 뜻하는지(종료코드에 안 들어간다)까지 알린다.
        if n_excluded:
            print(f"[summary][처리제외] {n_excluded}건은 스캔 PDF(텍스트 레이어 없음)라 "
                  f"OCR 미적용 범위로 처리에서 제외했습니다(excluded.kind=scan_pdf) — "
                  f"실패가 아니므로 종료코드에 넣지 않았습니다. 등급은 파일명·경로 신호로만 "
                  f"판정했습니다.", file=sys.stderr)
        # 분할 압축 조각 — 깨진 파일이 아니라 '원래 단독으로 못 읽는 것'이다.
        if n_split_volume:
            print(f"[summary][분할 조각] {n_split_volume}건은 분할 압축의 조각이라 읽지 "
                  f"않았습니다(error.kind=split_volume) — 파일이 깨진 것이 아닙니다. "
                  f"이 도구는 조각을 이어 붙이지 않으므로, 원본을 한 파일로 합친 뒤 "
                  f"넣어야 내부 문서가 분류됩니다.", file=sys.stderr)
        # G4 — 가장 조용한 손실. 내부 문서 수백 건이 통째로 빠졌을 수 있다.
        # 상한 초과와 그 밖의 실패는 사람이 할 일이 달라서 문구를 나눈다.
        if arch_unexpanded:
            n_limit = sum(1 for _r, k in arch_unexpanded.values() if k == "limit")
            n_vol = sum(1 for _r, k in arch_unexpanded.values() if k == "split_volume")
            n_error = len(arch_unexpanded) - n_limit - n_vol
            if n_limit:
                print(f"[summary][압축 미해제] {n_limit}건은 해제 상한을 넘어 "
                      f"내부 파일로 펼치지 못했습니다(archive_unexpanded=true) — 그 압축 안의 "
                      f"문서는 한 건도 분류되지 않았습니다. 상한 조정은 --max-archive-mb "
                      f"· --max-archive-members 로 합니다.", file=sys.stderr)
            if n_vol:
                print(f"[summary][분할 조각] {n_vol}건은 분할 압축의 첫 조각이라 펼치지 못했습니다"
                      f"(archive_unexpanded=true, kind=split_volume) — 파일이 깨진 것이 아닙니다. "
                      f"원본을 한 파일로 합친 뒤 넣어야 내부 문서가 분류됩니다.",
                      file=sys.stderr)
            if n_error:
                print(f"[summary][압축 확장 실패] {n_error}건은 압축을 펼치지 못했습니다"
                      f"(archive_unexpanded=true, kind=error) — 그 압축 안의 문서는 한 건도 "
                      f"분류되지 않았습니다. 손상·암호·분할볼륨이거나 해제 도구가 없을 수 "
                      f"있습니다. 사유는 각 레코드의 archive_unexpanded_reason 을 보세요.",
                      file=sys.stderr)
        # 절단이 있었으면 무엇을 해야 하는지까지 알려 준다 — 숫자만 던지면 운영자가
        # 상한을 올려야 할지 그 문서를 따로 봐야 할지 판단할 근거가 없다.
        if n_text_truncated:
            print(f"[summary][본문 절단] {n_text_truncated}건은 본문 앞 "
                  f"{text_limit:,}자만 보고 판정했습니다(text_truncated=true) — "
                  f"재검토 대상입니다. 상한 조정은 --max-text-chars 로 합니다.",
                  file=sys.stderr)
        # 본문은 안 잘렸어도 전화·주소·계좌만 뒷부분을 못 본 문서가 있다.
        if n_pii_capped:
            print(f"[summary][PII 스캔 상한] {n_pii_capped}건은 전화·주소·계좌를 "
                  f"앞부분까지만 훑었습니다 — 뒤쪽 연락처·주소록이 안 잡혔을 수 "
                  f"있습니다(레코드의 pii_scan_capped 참고)", file=sys.stderr)
        # doctype 축 롤업 — 뿌리 카테고리별 총계(괄호 안은 실제 걸린 노드별 내역) + 미분류.
        # 상세 문서별 내역이 필요하면 결과 파일을 classify.summarize_records() 로 다시 돌리면 된다.
        if doctype_total_docs:
            parts = []
            for root_title, total in sorted(doctype_root_totals.items(),
                                            key=lambda kv: kv[1], reverse=True):
                nodes = sorted(
                    ((node, cnt) for (root, node), cnt in doctype_node_totals.items()
                     if root == root_title),
                    key=lambda kv: kv[1], reverse=True,
                )
                breakdown = " · ".join(f"{n} {c}" for n, c in nodes)
                parts.append(f"{root_title} {total} ({breakdown})")
            # 한 건도 못 맞힌 경우 앞부분이 빈 문자열이 되어 "[summary][업무분류]  / 미분류 3"
            # 처럼 공백이 뜬다 — 값이 없다는 걸 말로 적어 준다.
            head = " / ".join(parts) if parts else "(분류된 문서 없음)"
            print(f"[summary][업무분류] {head} / 미분류 {doctype_unclassified}",
                  file=sys.stderr)

    # 기준 문서 등록 모드는 여기가 본론이다 — 위의 분류는 지문과 벡터를 얻는
    # 과정이었을 뿐이고, 이제 그 결과를 기준 문서 줄로 바꿔 쓴다.
    if getattr(args, "_seed_plan", None):
        seed_code = _finish_seed_add(args, taxonomy)
        # 한 건도 못 넣은 까닭이 '모델을 못 올림'이면 그것을 알린다 — 대응(설치 점검)이
        # 문서 탓(embed_failed)과 다르다. 둘 다 종료코드 2 라 번호만 달라진다.
        if seed_code == config.EXIT_EMBED_FAIL and embed_errors and not embedded:
            errcodes.set_status("model_load_failed",
                                f"[MpowerClassify] 임베딩 모델을 올리지 못해 기준 문서를 "
                                f"한 건도 등록하지 못했습니다 — {embed_errors[0]}")
        return seed_code
    return code


#------------------------------------------------------------------
# 기준 문서 등록 마무리 — 레코드를 기준 문서 줄로 바꿔 쓴다
#=> 설계서 8장 ④~⑧. 분류가 이미 지문(hash)과 벡터를 만들어 뒀으므로,
#   여기서는 사람이 정한 값을 얹어 저장하거나 stdout 으로 낸다.
#    1) 없는 dc_id 는 여기서 막는다 — 분류체계는 이 시점에 알 수 있다
#    2) --seeds 를 주면 읽기~쓰기를 잠그고 병합, 안 주면 stdout(파일 안 건드림)
#    3) 축마다 감사 기록을 남긴다 — CLI 로 넣은 것만 이력이 없으면 감사 구멍이다
#    4) 부분 실패를 종료코드로 구분한다 — 받는 쪽이 줄 수만 세면 못 알아챈다
#
# -in: args     = 파싱된 인자(_seed_plan·_seed_recs 가 실려 있다)
# -in: taxonomy = 분류체계(없으면 dc_id 검사를 건너뛴다)
#
# -out: code = 0 전부 등록 · 1 일부 실패 · 2 한 건도 못 함 · 3 인자 잘못
# -out: error = 파일을 못 쓰면 output_write_failed(exit 1)
#------------------------------------------------------------------
def _finish_seed_add(args, taxonomy):
    from . import seedstore

    plan = args._seed_plan
    # 없는 분류로 씨앗을 만들면 전파가 조용히 헛돈다. 체계를 아는 지금 막는다.
    if taxonomy is not None:
        for p in plan:
            gone = [d for d in p["doctype"] if taxonomy.get(d) is None]
            if gone:
                return fail_err("bad_args",
                                "[MpowerClassify] %s: 분류체계에 없는 dc_id 입니다: %s"
                                % (p["file"], ", ".join(gone)))

    seeds_path = getattr(args, "seeds", None)
    audit = seedcli.audit_path(args)

    #--------------------------------------------------------------
    # 등록 한 판 — 파일 모드면 잠근 채로 읽고 쓴다
    #=> 읽고 나서 남이 쓴 뒤 우리가 덮으면 그 등록이 아무 말 없이 사라진다.
    #   그래서 읽기~쓰기 전체를 한 잠금 안에서 한다.
    #
    # -in: 없음(바깥 변수를 쓴다)
    # -out: (added, failed, before) = 등록·실패 목록과 축별 이전 값
    # -out: error = 없음
    #--------------------------------------------------------------
    def _do():
        base = seedstore.load_seeds(seeds_path) if seeds_path else []
        # 감사 기록의 before 에 넣을 '고치기 전 값'을 미리 떠 둔다.
        was = {}
        for p in plan:
            cur = seedstore.find_seed(base, p["file"])
            was[seedstore.norm_file(p["file"])] = {
                a: seedstore.axis_value(cur, a) for a in seedstore.AXES} if cur else {}
        rows, added, failed = seedcli.build_rows(plan, args._seed_recs, base)
        # BM25 2차가 비교할 제목(첫 줄)도 함께 남긴다 — 본문은 남기지 않는다(설계서 §09)
        titles = getattr(args, "_seed_titles", {}) or {}
        for p in plan:
            t = titles.get(seedstore.norm_file(p["file"]))
            row = seedstore.find_seed(rows, p["file"]) if t else None
            if row is not None:
                row["title"] = t
        return rows, added, failed, was

    try:
        if seeds_path:
            with seedstore.seed_lock(seeds_path):
                rows, added, failed, was = _do()
                n = seedstore.save_seeds(seeds_path, rows)
        else:
            rows, added, failed, was = _do()
            n = 0
    except TimeoutError as e:
        return fail_err("output_write_failed", "[MpowerClassify] %s" % e, seeds_path)
    except OSError as e:
        return fail_err("output_write_failed",
                        "[MpowerClassify] 기준 문서를 쓰지 못했습니다: %s :: %s"
                        % (seeds_path, e), seeds_path)

    # stdout 모드 — 등록된 줄만 jsonl 로 내보낸다(파일은 건드리지 않는다).
    if not seeds_path:
        done = {seedstore.norm_file(a["file"]) for a in added}
        for r in rows:
            if seedstore.norm_file(r.get("file")) in done:
                print(json.dumps(seedstore._ordered(r), ensure_ascii=False))

    # 감사 기록 — 파일 모드에서만. stdout 모드는 파일을 안 만지는 모드다.
    if seeds_path and audit and added:
        for a in added:
            prev = was.get(seedstore.norm_file(a["file"]), {})
            for axis, val in (("security", a["grade"]), ("doctype", a["doctype"])):
                if not val:
                    continue
                try:
                    seedstore.append_seed_audit(
                        audit, "update" if prev.get(axis) else "add", a["file"],
                        axis=axis, before=prev.get(axis), after=val,
                        reviewer=plan[0]["reviewer"], reason="CLI --seed-add")
                except OSError as e:
                    # 씨앗은 이미 저장됐다 — 되돌리지도, 조용히 넘기지도 않는다.
                    print("[seed-add] 변경 기록을 남기지 못했습니다: %s :: %s"
                          % (audit, e), file=sys.stderr)
                    break

    where = seeds_path if seeds_path else "stdout"
    msg = "[seed-add] %d건 등록" % len(added)
    if failed:
        msg += " · %d건 실패(%s)" % (len(failed), failed[0]["why"])
    if seeds_path:
        msg += " → %s (전체 %d줄)" % (where, n)
    else:
        msg += " → stdout (파일은 건드리지 않았습니다)"
    print(msg, file=sys.stderr)
    for f in failed:
        print("[seed-add] 실패: %s — %s" % (f["file"], f["why"]), file=sys.stderr)
    if seeds_path and audit:
        print("[seed-add] 변경 기록: %s" % audit, file=sys.stderr)

    # 받는 쪽이 stdout 줄 수만 세면 "3건 넣었는데 2줄"을 못 알아챈다.
    # 0 과 1 이 다르다는 것을 종료코드로 못박는다(설계 P4).
    # 상태 줄의 건수·까닭은 '등록'을 기준으로 다시 적는다. 앞선 분류 단계의 건수를
    # 그대로 두면 "success 인데 failed=1" 같은 어긋난 줄이 나간다.
    errcodes.set_counts(len(plan), len(failed))
    first_why = failed[0]["why"] if failed else ""
    if not added:
        # 실패 사유는 '문서를 못 읽음'·'벡터를 못 얻음' — 기준으로 쓸 벡터를 못 만든
        # 것이라 embed_failed(3003)다. 역추적하면 model_load_failed 로 잘못 붙는다.
        errcodes.set_status("embed_failed",
                            f"{len(plan)}건 중 한 건도 등록하지 못했습니다({first_why})")
        return config.EXIT_EMBED_FAIL
    if not failed:
        return config.EXIT_OK
    errcodes.set_status("extract_failed",
                        f"{len(plan)}건 중 {len(failed)}건을 등록하지 못했습니다({first_why})")
    return config.EXIT_EXTRACT_FAIL


#------------------------------------------------------------------
# --out 의 상위 폴더가 없으면 만든다
#=> 결과 파일을 열기 직전에 부른다. 예를 들어 --out D:\\temp\\o.json 인데 D:\\temp 가
#   아직 없으면, 예전에는 "출력 파일을 쓰지 못했습니다"(4001)로 멈췄다. 폴더가 없다는
#   이유만으로 분류를 못 하는 것은 부르는 쪽에 일을 떠넘기는 것이라 만들어 준다.
#   Rust 판(main.rs 의 --out 확인 자리)과 같은 동작이다 — 두 판이 갈리면 안 된다.
#    1) 경로에 폴더 부분이 없으면(파일 이름만) 할 일이 없다
#    2) 있으면 중간 폴더까지 한 번에 만든다(이미 있으면 그대로 둔다)
#    3) 못 만들어도 여기서는 조용히 넘어간다 — 바로 뒤의 open() 이 실패하면서
#       그 까닭을 4001 로 알린다(실패를 알리는 자리를 한 곳으로 둔다)
#
# -in: path = --out 으로 받은 결과 파일 경로
#
# -out: 없음
# -out: error = 없음(폴더를 못 만든 경우는 뒤따르는 open() 이 OSError 로 드러낸다)
#------------------------------------------------------------------
def _ensure_out_parent(path):
    parent = os.path.dirname(path)
    if not parent:
        return
    try:
        # 상위의 상위까지 없을 수 있으므로 중간 폴더도 함께 만든다.
        os.makedirs(parent, exist_ok=True)
    except OSError:
        # 권한 없음·상위가 파일임 등 — 뒤의 open() 이 같은 까닭으로 실패해 알려 준다.
        pass


#------------------------------------------------------------------
# 임베딩 라벨 전파(--propagate, Signal B 2차 패스)
#=> --with-vector 로 만든 1차 레코드(jsonl, 벡터 포함)를 읽어, 고신뢰 등급을 seed
#   삼아 미분류·저신뢰 문서에 라벨을 전파하고 최종 등급을 재계산해 다시 출력한다.
#   임베딩 모델이 필요 없다(이미 뽑힌 벡터로 벡터연산만).
#    1) jsonl 레코드 로드
#    2) propagate_records 로 전파+재융합
#    3) 갱신 레코드 출력 + 통계 요약
#   [로그 라벨] 헷갈림 방지로 레코드마다 "무엇이 등급을 정했는지"를 태그로 구분한다:
#     · [전파결과][임베딩]          = 보류였고 임베딩 전파가 관여함(signals.embed 존재)
#     · [전파결과][통과(rule 확정)] = 1차에서 이미 확정돼 임베딩 안 거침(그대로 통과)
#     · [전파결과][보류(미확정)]    = 벡터 없음 등으로 끝내 등급 못 정함
#   [doctype 축, 로드맵 D7] security 전파 뒤에 이어서, labels.doctype 이 있는
#   레코드에 한해 별도로 doctype 축도 전파한다(같은 --seeds 파일의 labels.doctype
#   필드를 읽는다). security 와 달리 "이미 후보가 있어도" embed 후보를 추가한다
#   (다중 라벨은 상향이 아니라 추가가 원칙, 설계서 5-4).
#
# -in: args = argparse 결과(propagate=입력파일, out/fmt/failsafe, taxonomy/doc_rules/axis)
#
# -out: code = 종료코드(성공 0, 입력파일 문제면 EXIT_ARG_ERROR)
# -out: error = 없음(개별 파싱 오류는 건너뛰고 로그)
#------------------------------------------------------------------
def run_propagate(args):
    import json

    from .classify.engine import propagate_records, propagate_doctype_records
    from .classify.propagate import SeedIndex, DoctypeSeedIndex
    from .classify import UnknownGradeError

    log = logsetup.get_logger("csoclassify.cli")

    # 1차 레코드(jsonl) 로드.
    if not os.path.isfile(args.propagate):
        return fail_err("propagate_input_missing",
                        f"[MpowerClassify] 전파 입력 파일이 없습니다: {args.propagate}",
                        args.propagate)

    # 입력 레코드 로드 — classify 가 형식에 따라 'JSON 배열'(--dir 기본), '객체 1개'
    # (--file 기본), 'jsonl'(--format jsonl) 중 무엇이든 낼 수 있으므로 모두 받아들인다.
    #  1) 파일 전체를 JSON 으로 파싱 시도 → 배열이면 그대로, 객체면 1건으로.
    #  2) 실패하면 jsonl(한 줄=한 레코드)로 폴백 파싱.
    recs = []
    # 깨진 줄 수 — 한 줄도 못 읽었는데 성공(빈 결과)으로 끝나지 않도록 센다.
    bad_lines = 0
    with open(args.propagate, encoding="utf-8") as f:
        raw = f.read()
    try:
        data = json.loads(raw)
        recs = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    except json.JSONDecodeError:
        for i, line in enumerate(raw.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError as e:
                bad_lines += 1
                # 조용히 건너뛰면 결과 건수만 줄어든다 — 화면에도 알린다(Rust 판과 같다).
                print(f"[MpowerClassify] 전파 입력 {i}행 파싱 실패: {e}", file=sys.stderr)
                log.warning("전파 입력 %d 행 파싱 실패: %s", i, e)

    # 문서가 아닌 줄을 걸러낸다 — 'file' 이 있는 것만 문서 레코드로 본다.
    #
    # [2026-09-11] 1차 결과에는 문서 줄 말고도 맨 앞의 실행 헤더({"run":…})와
    # 맨 뒤의 요약({"summary":…})이 들어 있다. 여태 그 둘을 그대로 문서로 받아
    # 전파를 돌렸고, 헤더가 등급 없는 유령 레코드 한 줄로 **결과에 실려 나갔다**
    # (실측: 파이썬 19줄 vs Rust 18줄). 전파 통계도 그만큼 어긋났다
    # (axis_off=1 · no_vector=1 이 헤더 한 줄 때문에 붙었다).
    # 오류가 안 나는 종류라, 2차 패스를 돌릴 때마다 미분류가 1건씩 늘어나는데도
    # 그것이 문서인 줄 알았다. Rust 판 load_propagate_input 과 같은 기준이다.
    recs = [r for r in recs if isinstance(r, dict) and isinstance(r.get("file"), str)]
    # 깨진 줄만 있고 문서가 한 건도 없으면 '못 읽음'(1008)이다. 예전에는 빈 배열과
    # success 로 끝나, 부르는 쪽이 "전파할 문서가 없었다"로 잘못 읽었다.
    if not recs and bad_lines:
        return fail_err("propagate_input_missing",
                        f"[MpowerClassify] 전파 입력을 읽지 못했습니다"
                        f"(문서 레코드 0건, 깨진 줄 {bad_lines}줄): {args.propagate}",
                        args.propagate)

    # --seeds 가 있으면 외부 큐레이션 seed 저장소를 기준으로 비교(Phase 3 정석).
    seed_index = None
    if args.seeds:
        if not os.path.isfile(args.seeds):
            return fail_err("seeds_missing",
                            f"[MpowerClassify] seed 파일이 없습니다: {args.seeds}",
                            args.seeds)
        # 1차 결과의 벡터와 seed 벡터가 같은 모델에서 나왔는지(차원) 본다. 전파 모드는 모델을
        # 올리지 않으므로 '이번 벡터'는 입력 레코드에 이미 실린 벡터다.
        rec_dim = next((len(r["vector"]) for r in recs
                        if isinstance(r.get("vector"), list) and r["vector"]), None)
        if rec_dim:
            why = _seed_dim_problem(args.seeds, rec_dim, f"1차 결과 벡터({rec_dim}차원)")
            if why:
                return fail_err("seeds_model_mismatch", f"[MpowerClassify] {why}", args.seeds)
        seed_index = SeedIndex.from_seed_file(args.seeds)
        print(f"[MpowerClassify] 외부 seed {seed_index.size}건 로드: {args.seeds}", file=sys.stderr)

    # 전파 + 재융합(상향 전용).
    # 입력 레코드/seed 는 외부 파일이라 손으로 고쳐졌을 수 있다. 등급 값이 정의된
    # 서열에 없으면 max_grade 가 예외를 던지므로(fail-closed), 원시 스택 대신
    # "어느 파일이 문제인지" 알려 주고 규칙셋 오류와 같은 코드 4 로 나간다.
    try:
        records, stats = propagate_records(recs, seed_index=seed_index, failsafe=args.failsafe)
    except UnknownGradeError as e:
        log.error("전파 입력 등급 오류 :: %s", e)
        return fail_err("rules_invalid",
                        f"[MpowerClassify] 전파 입력의 등급 값이 올바르지 않습니다: "
                        f"{args.propagate}\n  {e}", args.propagate)

    # doctype 축 전파(D7) — security 와 별개 축이라 별도 seed 저장소(같은 class_seed.jsonl
    # 파일의 labels.doctype 필드, 설계서 5-4 "한 파일에 두 축")와 별도 통계로 처리한다.
    # taxonomy/doc_rules 가 있어야(=이 배치가 doctype 축을 쓰고 있어야) 조상 흡수·전략
    # 재적용에 쓸 Taxonomy·ConflictSpec 을 알 수 있다 — 4-6 과 같은 "있으면 켜짐" 원칙.
    taxonomy, doc_rules_set, dt_code = _load_doctype_axis(args, log)
    if dt_code is not None:
        return dt_code
    if taxonomy is not None and doc_rules_set is not None:
        dt_seeds = DoctypeSeedIndex.from_seed_file(args.seeds)
        emb = doc_rules_set.embed
        if dt_seeds.size:
            print(f"[MpowerClassify] 업무분류 seed {dt_seeds.size}건 로드: {args.seeds}", file=sys.stderr)
        if dt_seeds.size and not emb.enabled:
            print("[MpowerClassify] doc_rule.yaml 의 embed.enabled 가 false 라 "
                  "업무분류 2단계를 건너뜁니다", file=sys.stderr)
            dt_seeds = DoctypeSeedIndex.from_seed_file(None)   # 빈 인덱스로 통과
        records, dt_stats = propagate_doctype_records(
            records, dt_seeds, taxonomy, doc_rules_set.conflict,
            embed_cap=doc_rules_set.defaults.embed_cap, **emb.kwargs())
        print(
            f"[propagate][업무분류] seeds={dt_stats['seeds']} "
            f"embed_contributed={dt_stats['embed_contributed']} "
            f"no_vector={dt_stats['no_vector']} axis_off={dt_stats['axis_off']}",
            file=sys.stderr,
        )

    # 결과 출력(--out 있으면 파일). 전파 입력은 '레코드 묶음'이라 json 은 항상 배열로
    # 감싸 유효한 JSON 파일이 되게 한다(다건 concatenation 무효화 방지).
    try:
        # 상위 폴더가 없으면 만들어 둔다(Rust 판과 같다). 못 만들면 아래 open 이 까닭을 알린다.
        if args.out:
            _ensure_out_parent(args.out)
        out_fp = open(args.out, "w", encoding="utf-8") if args.out else None
    except OSError as e:
        return fail_err("output_write_failed",
                        f"[MpowerClassify] 출력 파일을 쓰지 못했습니다: {args.out}\n  {e}",
                        args.out)
    writer = output.RecordWriter(out_fp or sys.stdout, args.fmt, multi=len(records) > 1)
    try:
        for rec in records:
            writer.write_record(_order_record(rec))
            # 전파 후 최종 결과 JSON 도 로그로 남긴다(벡터 축약).
            # 이 레코드의 등급이 임베딩 전파로 정해졌는지, 1차에서 이미 확정돼 그냥
            # 통과했는지를 태그로 구분해 로그만 봐도 바로 알 수 있게 한다.
            #  · signals.embed 존재 = propagate_records 가 보류 문서에만 붙임 → 임베딩 관여
            #  · 없고 grade 있음    = 1차(rule/path/name) 확정본이 그대로 통과
            #  · 없고 grade 없음    = 끝내 미확정(보류)
            rsec = record.security_of(rec)
            if "embed" in (rsec.get("signals") or {}):
                tag = "[임베딩]"
            elif rsec.get("grade") is not None:
                # 실제로 무엇이 확정했는지(rule/path/name)를 그대로 보여 준다.
                who = "·".join(rsec.get("decided_by") or []) or "1차"
                tag = f"[통과({who} 확정)]"
            else:
                tag = "[보류(미확정)]"
            log.info("[전파결과]%s %s", tag,
                     output.dumps_safe(_loggable_record(rec), separators=(",", ":")))
        # json 배열 모드면 마지막에 ']' 로 닫는다(파일 닫기 전에).
        writer.close()
    finally:
        if out_fp is not None:
            out_fp.close()

    print(
        f"[propagate] seeds={stats['seeds']} already_graded={stats['already_graded']} "
        f"embed_decided={stats['embed_decided']} "
        f"still_unclassified={stats['still_unclassified']} no_vector={stats['no_vector']}",
        file=sys.stderr,
    )
    return config.EXIT_OK


#------------------------------------------------------------------
# 일반 처리(임베딩) — 데몬 우선, 폴백 포함
#=> 파일들을 처리해 벡터를 출력한다. 데몬 on 이면 클라이언트로 위임하고, 데몬을
#   못 쓰면 in-process 로 폴백한다. in-process 임베더는 한 번만 만들어 재사용한다.
#    1) 데몬 클라이언트 준비(옵션)
#    2) 파일마다: 데몬 요청 → 실패 시 in-process
#    3) 결과 출력 + 배치 시간요약
#
# -in: files  = 대상 파일 리스트
# -in: args   = argparse 결과(모델/포맷/timing 등)
# -in: opts   = 처리 옵션 dict
# -in: out_fp = 출력 파일객체(None 이면 stdout)
#
# -out: code = 종료코드(성공 0, 추출/임베딩 실패 코드)
# -out: error = 없음(개별 실패는 stderr 로 알림 + 코드 반영)
#------------------------------------------------------------------
def run_embed(files, args, opts, out_fp):
    from .extract import build_extractor
    from .extract.base import ExtractError
    from .embed.onnx_embedder import OnnxEmbedder
    from .embed.base import EmbedError
    from . import pipeline

    log = logsetup.get_logger("csoclassify.cli")
    spec = config.get_model_spec(args.model)

    # 데몬 클라이언트(옵션). 실제 접속/기동은 첫 요청 때 일어난다.
    client = None
    if args.daemon:
        from .daemon.client import DaemonClient
        # 데몬이 같은 로그파일에 남기도록 경로/verbose 를 함께 넘긴다.
        client = DaemonClient(args.model, idle_timeout=args.idle_timeout,
                              log_path=getattr(args, "_log_path", None), verbose=args.verbose)

    # in-process 폴백용 자원은 처음 필요할 때 한 번만 만들어 재사용한다.
    ip_extractor = {"v": None}
    ip_embedder = {"v": None}

    #--------------------------------------------------------------
    # in-process 로 파일 1개 처리(내부 헬퍼)
    #=> 폴백/‘--no-daemon’ 경로. 추출기·임베더를 지연 생성해 재사용하고, 단일
    #   파일이면 추출과 모델 로딩을 병렬 예열한다.
    #
    # -in: path = 처리할 파일 경로
    #
    # -out: result = 파이프라인 결과 dict
    # -out: error = ExtractError/EmbedError 전파
    #--------------------------------------------------------------
    def process_inprocess(path):
        if ip_extractor["v"] is None:
            # --hybridparse 여부에 따라 사이냅 단독/하이브리드. (데몬 경로는 서버측 snf — 설계 §9 선택A)
            ip_extractor["v"] = build_extractor(hybrid=getattr(args, "hybridparse", False))
            ip_embedder["v"] = OnnxEmbedder(spec, num_threads=args.num_threads)
        timing = Timing()
        # 파일이 1개뿐이면 병렬 예열 이득이 크다(배치는 이미 로드돼 있어 불필요).
        parallel = (len(files) == 1)
        return pipeline.process_file(
            path, opts, ip_extractor["v"], ip_embedder["v"], timing, parallel_preload=parallel
        )

    code = config.EXIT_OK
    totals = []
    # json 은 처리 대상이 여러 건(배치 --dir 또는 압축 확장으로 다건)일 때 배열로 감싼다.
    # 진짜 단건이면 객체 하나(기존 동작 유지).
    writer = output.RecordWriter(out_fp or sys.stdout, args.fmt,
                                 multi=(len(files) > 1))
    for item in files:
        # src=실제 읽을 경로(압축 내부면 임시파일), label=표시용(압축경로/내부경로).
        src, label, _origin = _as_job(item)
        try:
            if client is not None:
                # 데몬 경로: 사용 불가 신호(DaemonUnavailable)면 조용히 폴백.
                from .daemon.client import DaemonUnavailable
                try:
                    result = client.request_embed(src, opts)
                except DaemonUnavailable as e:
                    if args.verbose:
                        print(f"[MpowerClassify] 데몬 폴백(in-process): {e}", file=sys.stderr)
                    # 폴백은 파일 로그에 항상 남겨 원인을 나중에 확인할 수 있게 한다.
                    log.warning("데몬 사용 불가 → in-process 폴백 file=%s :: %s", label, e)
                    result = process_inprocess(src)
            else:
                result = process_inprocess(src)

            # 결과의 file 필드를 표시용 경로(zip 내부 파일명)로 교체한다(임시경로 노출 방지).
            if isinstance(result, dict):
                result["file"] = label
            # 본문은 배열 경계를 관리하는 writer 로, 시간요약은 stderr 로 분리해 출력.
            stdout_str, stderr_str = output.format_result(result, args.fmt, timing=args.timing)
            writer.write_block(stdout_str)
            if stderr_str:
                print(stderr_str, file=sys.stderr)
            # 배치 시간요약에 쓸 total 수집.
            totals.append(result.get("elapsed_ms", {}).get("total", 0.0))

        except ExtractError as e:
            print(f"[MpowerClassify] 추출 실패: {label} :: {e}", file=sys.stderr)
            log.error("추출 실패 file=%s :: %s", label, e)
            code = config.EXIT_EXTRACT_FAIL
        except EmbedError as e:
            print(f"[MpowerClassify] 임베딩 실패: {label} :: {e}", file=sys.stderr)
            log.error("임베딩 실패 file=%s :: %s", label, e)
            code = config.EXIT_EMBED_FAIL
        except RuntimeError as e:
            # 데몬이 돌려준 처리 오류(추출/임베딩)를 코드로 환원.
            msg = str(e)
            print(f"[MpowerClassify] 처리 실패: {label} :: {msg}", file=sys.stderr)
            # 스택까지 파일 로그에 남겨 원인 추적을 돕는다.
            log.exception("처리 실패 file=%s :: %s", label, msg)
            code = config.EXIT_EMBED_FAIL if "embed" in msg else config.EXIT_EXTRACT_FAIL

    # json 배열 모드면 마지막에 ']' 로 닫는다.
    writer.close()

    # 배치이고 timing 이면 마지막에 합계/평균/최댓값 요약.
    if args.timing and len(files) > 1:
        line = output.batch_timing_line(totals)
        if line:
            print(line, file=sys.stderr)
    return code


#------------------------------------------------------------------
# 데몬 제어 서브커맨드 처리
#=> --serve/--status/--stop 중 하나가 있으면 그에 맞게 동작하고 종료코드를 준다.
#
# -in: args = argparse 결과
#
# -out: code 또는 None = 처리했으면 종료코드, 해당 없으면 None(일반 처리로 진행)
# -out: error = 없음
#------------------------------------------------------------------
def handle_daemon_commands(args):
    # 데몬 본체로 기동: 이 프로세스가 서버가 되어 블로킹 실행.
    # => --serve 인자인 경우.
    if args.serve:
        from .daemon.server import DaemonServer
        spec = config.get_model_spec(args.model)
        server = DaemonServer(spec, idle_timeout=args.idle_timeout, num_threads=args.num_threads)
        return server.run()

    # 상태 조회.
    if args.status:
        from .daemon.client import DaemonClient
        rec, ping = DaemonClient(args.model).status()
        if rec:
            # 유휴시간 표시: 0/없음 = 무한(자동종료 없음).
            _it = rec.get("idle_timeout")
            idle_disp = "무한(자동종료 없음)" if not _it else f"{_it}s"
            print(f"model={rec.get('model')} pid={rec.get('pid')} "
                  f"port={rec.get('port')} version={rec.get('version')} "
                  f"idle_timeout={idle_disp} ready={ping.get('model_ready')}")
        else:
            print(f"model={args.model}: 실행 중인 데몬 없음")
        return config.EXIT_OK

    # 정지.
    if args.stop:
        from .daemon.client import DaemonClient
        stopped = DaemonClient(args.model).stop()
        print("데몬 정지 요청 전송" if stopped else "정지할 데몬 없음")
        return config.EXIT_OK

    return None


#------------------------------------------------------------------
# 실제 실행 커맨드라인 문자열
#=> 로그에 "무엇을 어떻게 실행했는지"를 재현 가능하게 남기려고, 이 프로세스의 실행
#   커맨드라인을 풀경로로 조립한다.
#    - frozen(exe): sys.argv[0] 이 exe 풀경로라 그대로 사용 (예: C:\...\MpowerClassify.exe ...)
#    - 소스/모듈 실행: 앞에 파이썬 실행기를 붙여 실제 실행형태로 복원
#
# -in: 없음
# -out: cmd = 따옴표 처리된 전체 커맨드라인 문자열
# -out: error = 없음(조립 실패 시 공백 join 으로 폴백)
#------------------------------------------------------------------
def _full_command_line():
    import subprocess
    parts = list(sys.argv) if getattr(sys, "frozen", False) else [sys.executable] + list(sys.argv)
    # 열쇠(--mrk 뒤의 값)는 로그에 남기지 않는다 — **** 로 가려서 적는다.
    from . import decrypt
    parts = decrypt.mask_args(parts)
    try:
        return subprocess.list2cmdline(parts)
    except Exception:
        return " ".join(str(p) for p in parts)


#------------------------------------------------------------------
# 로그용 레코드 축약
#=> 결과 JSON 을 로그로 남길 때 벡터(384차원)와 추출 텍스트(--with-text 시)는 너무 길어
#   로그를 가리므로 길이/차원 요약으로 바꾼다. 등급/신호 등 사람이 볼 값은 그대로 둔다.
#
# -in: rec = 분류/전파 결과 dict
#
# -out: dict = vector→"[vec dim=N]", text→"[text len=N]" 로 축약한 얕은 복사본
#              (해당 필드가 없으면 원본 그대로)
# -out: error = 없음
#------------------------------------------------------------------
def _loggable_record(rec):
    # vector/text/pii 중 하나라도 있으면 로그용으로 축약·가림(원문 유출 방지).
    if not (isinstance(rec, dict) and (rec.get("vector") is not None
            or rec.get("text") is not None or rec.get("pii") is not None)):
        return rec
    r = dict(rec)
    if r.get("vector") is not None:
        try:
            r["vector"] = f"[vec dim={len(rec['vector'])}]"
        except TypeError:
            r["vector"] = "[vec]"
    if r.get("text") is not None:
        try:
            r["text"] = f"[text len={len(rec['text'])}]"
        except TypeError:
            r["text"] = "[text]"
    # [프라이버시] 원문 PII 는 로그에 절대 남기지 않는다 — 건수만 표기(값은 --out 파일에만).
    if r.get("pii") is not None:
        try:
            r["pii"] = f"[pii {len(rec['pii'])} detected]"
        except TypeError:
            r["pii"] = "[pii]"
    return r


#------------------------------------------------------------------
# 규칙 파일이 자동 생성본인가
#=> 파일 맨 위를 읽어 generated 칸이 있는지 본다. 없거나 못 읽으면 False —
#   그때 부르는 쪽은 "옛 모양이니 --build-doc-rule 로 다시 만드세요"로 멈춘다.
#
# -in: path = doc_rule.yaml 경로
#
# -out: bool
# -out: error = 없음
#------------------------------------------------------------------
def _is_generated_rules(path):
    if not path or not os.path.isfile(path):
        return False
    import yaml
    from .classify import docbuild
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception:          # noqa: BLE001 (깨진 파일도 '자동 생성본 아님'으로 본다)
        return False
    return docbuild.is_generated(data)


#------------------------------------------------------------------
# 자동 생성 규칙 파일로 업무분류 축 켜기
#=> 분류체계를 규칙 줄에서 다시 짓는다. 경고(입력이 바뀜·손으로 고친 흔적)는
#   화면에 그대로 보여 주고 판정은 계속한다.
#   --doctype-vector-only 면 규칙 목록만 비우고 나머지(embed 임계값·conflict·defaults)는
#   파일 값을 그대로 쓴다 — 통째로 갈면 관리자가 정해 둔 임계값이 조용히 무시된다.
#
# -in: args           = argparse 결과
# -in: log            = 로거
# -in: doc_rules_path = 규칙 파일 경로
#
# -out: (taxonomy, doc_rules_set, code) — _load_doctype_axis 와 같은 약속
# -out: error = 없음(검증 실패는 code 로)
#------------------------------------------------------------------
def _load_generated_axis(args, log, doc_rules_path):
    from .classify import doc_rules as DR
    try:
        doc_rules_set = DR.load_doc_rules(doc_rules_path)
    except DR.DocRuleValidationError as e:
        log.error("업무분류 규칙셋 검증 실패 count=%d path=%s", len(e.violations), e.path)
        return None, None, fail_err("doc_rules_invalid", f"[MpowerClassify] {e}", e.path)
    for w in doc_rules_set.warnings:
        print(f"[MpowerClassify] {w}", file=sys.stderr)
        log.warning("doctype 규칙 경고 :: %s", w)
    if getattr(args, "doctype_vector_only", False):
        doc_rules_set = dataclasses.replace(doc_rules_set, rules=())
        print("[MpowerClassify] 업무분류: 규칙을 쓰지 않고 기준 문서(class_seed) 비교로만 "
              "분류합니다(--doctype-vector-only).", file=sys.stderr)
    return doc_rules_set.taxonomy, doc_rules_set, None


#------------------------------------------------------------------
# 업무분류 규칙 자동 생성(--build-doc-rule)
#=> 체계 JSON · 사전 · 본보기 · doc_rule.local.yaml 로 doc_rule.yaml 을 통째로 만든다.
#   화면의 [규칙 다시 만들기]·엠파워 연동의 '준비' 단계가 이 한 번의 호출이다.
#    1) 체계 JSON 이 없으면 종료(3)
#    2) 만들기(체계·조정 파일 검증 실패는 4)
#    3) 쓰고, 엔진 로더로 다시 읽어 검증한다(round-trip)
#    4) 경고(조정이 가리키는 분류가 없음·이름 바뀜·이미 없는 말 빼기 등)를 보여 준다
#
# -in: args = 파싱된 인자(export_input · doc_rules)
#
# -out: code = 0(정상) · 3(체계 JSON 없음) · 4(입력 오류·쓰기 실패)
# -out: error = 없음(예외를 종료코드로 환원)
#------------------------------------------------------------------
def run_build_doc_rule(args):
    from .classify import axes as AX
    from .classify import doc_rules as DR
    from .classify import docbuild

    log = logsetup.get_logger("csoclassify.cli")
    rules_path = args.doc_rules or DR.default_doc_rules_path()
    policy_dir = os.path.dirname(os.path.abspath(rules_path))
    # 체계 JSON 기본 자리는 규칙 파일 옆 — 연동 때 엠파워가 그 자리에 둔다.
    export_path = args.export_input or os.path.join(policy_dir, "doc_classification_export.json")
    if not os.path.isfile(export_path):
        return fail_err("export_input_missing",
                        f"[MpowerClassify] 체계 JSON 을 찾을 수 없습니다: {export_path}\n"
                        f"  · --export-input <파일경로> 로 지정하거나,\n"
                        f"  · 규칙 파일 옆에 doc_classification_export.json 을 두세요.",
                        export_path)
    try:
        doc, warns = docbuild.build_doc_rule(export_path, policy_dir)
    except docbuild.RuleLocalValidationError as e:
        return fail_err("doc_rules_invalid", f"[MpowerClassify] {e}", e.path)
    except AX.TaxonomyValidationError as e:
        return fail_err("export_input_invalid", f"[MpowerClassify] {e}", export_path)
    except (ValueError, OSError) as e:
        return fail_err("export_input_invalid",
                        f"[MpowerClassify] 체계 JSON 을 읽지 못했습니다: {e}", export_path)
    try:
        docbuild.write_doc_rule(rules_path, doc)
    except OSError as e:
        return fail_err("doc_rules_write_failed",
                        f"[MpowerClassify] 규칙 파일을 쓰지 못했습니다: {rules_path}\n  {e}",
                        rules_path)
    # 방금 쓴 파일을 엔진이 그대로 읽는지 확인한다 — 다음 분류 때 알게 되면 늦다.
    try:
        DR.load_doc_rules(rules_path)
    except DR.DocRuleValidationError as e:
        return fail_err("doc_rules_invalid", f"[MpowerClassify] {e}", e.path)
    for w in warns:
        print(f"[MpowerClassify] {w}", file=sys.stderr)
        log.warning("규칙 생성 경고 :: %s", w)
    n_rules = len(doc.get("doctype_rules") or [])
    n_local = sum(1 for r in doc["doctype_rules"] if r.get("local"))
    print(f"[MpowerClassify] {rules_path} 생성 — 규칙 {n_rules}개(회사 조정 {n_local}개) · "
          f"판 {doc['version']} · 입력 {len(doc['generated']['inputs'])}개",
          file=sys.stderr)
    log.info("업무분류 규칙 자동 생성 :: rules=%d local=%d version=%s path=%s",
             n_rules, n_local, doc["version"], rules_path)
    return 0


#------------------------------------------------------------------
# 규칙 단어 제안 — 후보 한 줄 찍기
#=> 사람이 표를 눈으로 훑을 수 있게 한 줄로 만든다. 개발 용어(로그 오즈·df)를
#   그대로 내보내지 않고, 판단에 필요한 것만 남긴다 —
#   "어디에 넣을지 · 근거가 몇 건인지 · 다른 분류에도 나오는지".
#
# -in: cand = termsuggest.suggest 가 만든 후보 dict
#
# -out: str = 화면에 찍을 한 줄
# -out: error = 없음
#------------------------------------------------------------------
def _suggest_line(cand):
    mark = "[v]" if cand["checked"] else "[ ]"
    # 칸 이름은 화면 말로 바꾼다(uiwords 규약과 같은 원칙 — 개발 용어를 안 보여 준다).
    names = {"title_terms": "제목", "head_terms": "앞부분",
             "terms": "본문", "filename": "파일이름"}
    where = "·".join(names.get(f, f) for f in cand["fields"])
    if cand["extra"].get("min_count"):
        where += f"(같은 말 {cand['extra']['min_count']}회 이상)"
    flags = " ".join(f"<{f}>" for f in cand["flags"])
    return (f"  {mark} {cand['term']:<14} {where:<22}"
            f" 근거 {cand['docs']:>3}건/폴더{cand['clusters']:<2}"
            f" 다른분류 {cand['df_neg'] * 100:>5.1f}%"
            f" 덮는율 {cand['df_pos'] * 100:>5.1f}%  {flags}")


#------------------------------------------------------------------
# 문턱을 못 넘은 말 한 줄 (참고 목록)
#=> 후보가 하나도 없을 때만 나온다. 기계가 자를 수 없는 판단(그 말이 진짜 문서
#   종류를 가리키는가)은 사람이 해야 하므로, 무엇이 왜 떨어졌는지를 보여 준다.
#
# -in: cand = suggest() 결과의 below 항목 하나
#
# -out: str = 화면에 찍을 한 줄
# -out: error = 없음
#------------------------------------------------------------------
def _below_line(cand):
    where = {"name": "파일이름", "title": "제목", "head": "앞부분",
             "body": "본문"}.get(cand["where"], cand["where"])
    flags = " ".join(f"<{f}>" for f in cand["flags"])
    return (f"  [-] {cand['term']:<14} {where:<8}"
            f" 근거 {cand['docs']:>3}건/폴더{cand['clusters']:<2}"
            f" 다른분류 {cand['df_neg'] * 100:>5.1f}%"
            f" 덮는율 {cand['df_pos'] * 100:>5.1f}%  {cand['why']} {flags}")


#------------------------------------------------------------------
# 규칙 단어 제안 전용 모드 (--suggest-terms)
#=> 문서도 모델도 읽지 않는다. 이미 확정된 라벨과 저장해 둔 본문만 보고,
#   규칙에 넣을 만한 말의 후보를 보여 준다. 규칙 파일은 고치지 않는다.
#    1) 재료를 모은다 — seed(사람 승인분) + 검토 확정 이력 + 저장된 본문
#    2) 분류를 정한다 — dc_id 하나거나, all 이면 확정 문서가 있는 분류 전부
#    3) 분류마다 후보를 뽑아 찍는다(요구하면 json 으로도 남긴다)
#
#   [왜 화면보다 이것을 먼저 만드나] 문턱이 실제 문서에서 어떻게 도는지는
#   눈으로 봐야 정할 수 있다. 화면을 먼저 만들면 그 확인이 화면 뒤에 숨는다.
#
# -in: args = 파싱된 인자(사용: suggest_terms·seeds·overrides·text_dir·
#             stopwords·doc_rules·suggest_json·suggest_min_docs·
#             include_auto_seeds)
#
# -out: code = 0(정상) · 3(재료 없음·분류 이름이 틀림)
# -out: error = 없음(예외를 종료코드로 환원)
#------------------------------------------------------------------
def run_suggest_terms(args):
    from .classify import termsuggest as TS
    from .classify import axes as AX
    from .classify import doc_rules as DR
    from .classify import default_seed_path
    from .classify import docvocab

    log = logsetup.get_logger("csoclassify.cli")

    # 재료 경로. 검토 이력·금지 목록은 정책 파일 옆에 두는 것을 규약으로 삼는다.
    seeds_path = args.seeds or default_seed_path()
    policy_dir = os.path.dirname(os.path.abspath(seeds_path))
    ov_path = args.overrides or os.path.join(policy_dir, "cso_override.jsonl")
    stop_path = args.stopwords or os.path.join(policy_dir, "doc_rule_stopwords.yaml")

    have_seed = os.path.isfile(seeds_path)
    have_ov = os.path.isfile(ov_path)
    if not have_seed and not have_ov:
        return fail_err("seeds_missing",
                        f"[MpowerClassify] 제안할 재료가 없습니다.\n"
                        f"  · 기준 문서: {seeds_path}\n"
                        f"  · 검토 확정 이력: {ov_path}\n"
                        f"  둘 중 하나는 있어야 합니다(--seeds · --overrides 로 지정).",
                        seeds_path)

    docs = TS.load_documents(seeds_path if have_seed else None,
                             ov_path if have_ov else None,
                             args.text_dir,
                             approved_only=not args.include_auto_seeds)
    if args.include_auto_seeds:
        print("[MpowerClassify] 사람이 승인하지 않은 seed 도 재료에 넣었습니다 — "
              "규칙이 만든 라벨이 다시 규칙을 만드는 고리가 생길 수 있습니다.",
              file=sys.stderr)

    # 규칙·분류 체계는 '거르기'에만 쓴다. 없어도 제안은 돌되, 무엇을 못 거르는지 알린다.
    rule_doc, tax_raw, suffixes, endings = None, None, None, None
    doc_rules_path = args.doc_rules or DR.default_doc_rules_path()
    if os.path.isfile(doc_rules_path):
        rule_doc = docvocab.load_doc(doc_rules_path)
        # 끝말 목록은 규칙 파일 옆 유의어 사전에서 온다(core 가 기준).
        syn = docvocab.load_synonyms(doc_rules_path) or {}
        suffixes = syn.get("suffixes")
        # 활용형 꼬리도 사전에서 온다(core 기준 + 업종·local 더하기).
        endings = syn.get("verb_endings")
    else:
        print(f"[MpowerClassify] 규칙 파일이 없어 '이미 있는 말'을 거르지 못합니다: "
              f"{doc_rules_path}", file=sys.stderr)
    # 분류 이름(대분류 포함)은 자동 생성 규칙의 path·path_ids 에서 다시 짓는다.
    if rule_doc is not None:
        try:
            tax = AX.taxonomy_from_rules(
                [r for r in rule_doc.get("doctype_rules") or [] if isinstance(r, dict)])
            tax_raw = {"by_id": {n.dc_id: {"title": n.title} for n in tax}}
        except ValueError as e:
            # 제안은 분류 체계 없이도 돈다 — 옛 모양 규칙이라 못 지었다고 멈출 자리가 아니다.
            print(f"[MpowerClassify] 규칙에서 분류 체계를 짓지 못해 '다른 분류 이름'을 "
                  f"거르지 못합니다: {e}", file=sys.stderr)

    stopwords = TS.load_stopwords(stop_path)

    # 훑을 분류를 정한다. all 이면 확정 문서가 한 건이라도 있는 분류 전부.
    want = str(args.suggest_terms).strip()
    labelled = sorted({d for doc in docs for d in doc["labels"]})
    if want.lower() == "all":
        nodes = labelled
        if not nodes:
            print("[MpowerClassify] 확정된 문서가 한 건도 없습니다 — "
                  "검토 화면에서 분류를 확정하면 재료가 쌓입니다.")
            return config.EXIT_OK
    else:
        nodes = [want]
        if want not in labelled:
            print(f"[MpowerClassify] '{want}' 로 확정된 문서가 없습니다. "
                  f"확정 문서가 있는 분류: {', '.join(labelled) or '(없음)'}",
                  file=sys.stderr)

    title_of = {}
    if tax_raw:
        title_of = {k: v["title"] for k, v in tax_raw["by_id"].items()}

    min_docs = args.suggest_min_docs or TS.MIN_DOCS
    log.info("단어 제안 시작 nodes=%d docs=%d text_dir=%s",
             len(nodes), len(docs), args.text_dir or "-")
    print(f"[MpowerClassify] 재료 {len(docs)}건"
          f"(기준문서 {'있음' if have_seed else '없음'} · "
          f"검토이력 {'있음' if have_ov else '없음'} · "
          f"본문 {'있음' if args.text_dir else '없음'})")
    # 숫자 두 개의 뜻을 여기서 한 번만 밝힌다 — 줄마다 붙이면 목록이 못 읽게 길어진다.
    print("  덮는율 = 이 분류 문서 중 그 말이 나온 비율(참고) · "
          "다른분류 = 다른 분류 문서 중 나온 비율(낮을수록 좋음 — 이 값으로 거릅니다)")
    print("  같은 폴더 문서는 합쳐서 1건으로 세어 비율을 냅니다. "
          "[v] 근거가 단단한 후보 · [ ] 후보 · [-] 문턱 미달(참고)")

    out = []
    for node in nodes:
        res = TS.suggest(node, docs, rule_doc=rule_doc, stopwords=stopwords,
                         tax=tax_raw, suffixes=suffixes, min_docs=min_docs,
                         endings=endings)
        out.append(res)
        name = title_of.get(node, "")
        head = f"\n■ {name}({node})" if name else f"\n■ {node}"
        print(f"{head} — 확정 {res['docs']}건 / 폴더 {res['clusters']}곳"
              + (f" · 본문 없음 {res['no_text']}건" if res["no_text"] else ""))
        if res["reason"]:
            print(f"  {res['reason']}")
        if not res["candidates"] and not res["cluster_only"]:
            if not res["reason"]:
                print("  제안할 말이 없습니다.")
            if res["below"]:
                # 문턱은 기계가 긋지만, 그 말이 쓸모 있는지는 사람이 안다.
                print("  [문턱 미달 — 참고] 검출은 됐지만 기준에 못 미친 말입니다. "
                      "쓸 만한지는 관리자가 판단하세요:")
                for cand in res["below"]:
                    print(_below_line(cand))
            continue
        for cand in res["candidates"]:
            print(_suggest_line(cand))
        if res["cluster_only"]:
            # 한 폴더에 몰린 어휘는 따로 보여 준다 — 근거가 약해 자동 채택하지 않는다.
            print("  [한 폴더 전용] 그 폴더에서만 쓰는 말일 수 있습니다:")
            for cand in res["cluster_only"]:
                print(_suggest_line(cand))

    if args.suggest_json:
        try:
            with open(args.suggest_json, "w", encoding="utf-8") as fp:
                json.dump(out, fp, ensure_ascii=False, indent=1)
            print(f"\n[MpowerClassify] 결과를 남겼습니다: {args.suggest_json}")
        except OSError as e:
            return fail_err("output_write_failed",
                            f"[MpowerClassify] 결과를 쓰지 못했습니다: {e}",
                            args.suggest_json)

    print("\n[MpowerClassify] 규칙 파일은 고치지 않았습니다 — "
          "넣을 말은 화면(설정 ② 판단 기준)에서 고르세요.")
    return config.EXIT_OK


#------------------------------------------------------------------
# 규칙셋 검사 전용 모드 (--check-rules)
#=> 문서는 한 건도 읽지 않고 cso_rule.yaml 의 등급 값만 확인하고 끝낸다.
#   규칙셋을 고친 뒤 '실제 스캔을 돌리기 전에' 안전한지 확인하는 용도다.
#   특히 검증을 새로 도입한 직후, 기존 규칙셋에 이미 오타가 있는지 미리 보는 데 쓴다.
#    1) 규칙셋을 검증까지 포함해 읽어 본다
#    2) 문제가 없으면 규칙 건수를 요약해 보여 준다
#    3) doc_rule.yaml 이 있으면 그것도 같은 자리에서 검사한다
#       (security 만 검사하고 doctype 은 실제 배치 때에야 오류를 만나면, "확인했는데
#       왜 또 실패하지" 하는 상황이 생긴다 — 있는 파일은 전부 미리 본다)
#       분류체계는 자동 생성 규칙 안에 있다. 옛 모양 규칙이면 실제 분류처럼 종료(4)
#
# -in: args = 파싱된 인자(사용 필드: rules·doc_rules·seeds)
#
# -out: code = 0(정상) · 3(파일 없음) · 4(검증 실패)
# -out: error = 없음(예외를 종료코드로 환원)
#------------------------------------------------------------------
def run_check_rules(args):
    from .classify import (load_rules, default_rules_path,
                           RuleSetValidationError, GRADES)

    log = logsetup.get_logger("csoclassify.cli")
    path = args.rules or default_rules_path()
    try:
        rs = load_rules(args.rules)
    except FileNotFoundError as e:
        return fail_err("rules_missing", f"[MpowerClassify] {e}", path)
    except RuleSetValidationError as e:
        log.error("규칙셋 검증 실패 count=%d path=%s", len(e.violations), e.path)
        return fail_err("rules_invalid", f"[MpowerClassify] {e}", e.path)

    # 통과했으면 "무엇을 검사했는지"를 건수로 보여 준다. 규칙이 0건이면 파일을
    # 잘못 지정했을 가능성이 크므로 눈에 띄게 알려 주는 편이 낫다.
    print(f"[MpowerClassify] 규칙셋 정상: {path}")
    print(f"  version={rs.version}  등급={'/'.join(GRADES)}")
    print(f"  regex_pii={len(rs.regex_rules)} · pii_combos={len(rs.pii_combos)} · "
          f"keywords={len(rs.keyword_rules)} · sensitive={len(rs.sensitive_rules)} · "
          f"stamps={len(rs.stamp_rules)}")

    # doctype 축은 선택 자산이다(4-6) — 파일이 아예 없으면 "안 씀"으로 보고 건너뛴다.
    # (--axis security 로 좁히지 않는다 — --check-rules 는 배치 실행이 아니라 사람이
    # "지금 상태가 괜찮은지" 미리 보는 자리라, 있는 건 다 보여주는 편이 낫다.)
    from .classify import doc_rules as DR

    # 자동 생성 규칙이면 분류체계가 규칙 줄 안에 있다 — doc_taxonomy.yaml 을 보지 않는다.
    # (실제 분류의 _load_doctype_axis 와 같은 갈래로 가야 '검사는 됐는데 실행은 다름'이 없다)
    doc_rules_path = args.doc_rules or DR.default_doc_rules_path()
    if _is_generated_rules(doc_rules_path):
        try:
            drs = DR.load_doc_rules(doc_rules_path)
        except DR.DocRuleValidationError as e:
            log.error("업무분류 규칙셋 검증 실패 count=%d path=%s", len(e.violations), e.path)
            return fail_err("doc_rules_invalid", f"[MpowerClassify] {e}", e.path)
        # 손으로 고침·입력 바뀜 같은 경고도 여기서 미리 보여 준다.
        for w in drs.warnings:
            print(f"[MpowerClassify] {w}")
        tax = drs.taxonomy
        print(f"[MpowerClassify] 업무분류 규칙셋 정상(자동 생성 — 분류체계 내장): {doc_rules_path}")
        print(f"  version={drs.version}  conflict={drs.conflict.strategy}  "
              f"규칙={len(drs.rules)}건(활성 {len(drs.active_rules)}건)  "
              f"노드={len(tax)}개  최상위={len(tax.roots)}개")
        return config.EXIT_OK

    if not os.path.isfile(doc_rules_path):
        # 규칙이 없으면 업무분류 축은 돌지 않는다(분류체계도 규칙 안에 있다).
        print(f"[MpowerClassify] 업무분류 규칙셋 없음 → 업무분류(doctype) 축 미사용: {doc_rules_path}\n"
              f"  규칙은 --build-doc-rule 로 만듭니다.")
        return config.EXIT_OK
    # 파일은 있는데 자동 생성본이 아니다 — 실제 분류(_load_doctype_axis)와 같은 판단으로 멈춘다.
    return fail_err("doc_rules_invalid",
                    f"[MpowerClassify] [G0] 옛 모양 규칙 파일입니다(분류체계 doc_taxonomy.yaml 을 "
                    f"따로 읽던 방식): {doc_rules_path}\n"
                    f"  이 판부터는 --build-doc-rule 로 만든 규칙만 읽습니다. 화면의 "
                    f"[새 방식으로 바꾸기] 또는 --build-doc-rule 로 다시 만드세요.",
                    doc_rules_path)


#------------------------------------------------------------------
# 오류 이름 하나로: JSON + 화면 + 로그 + 종료코드
#=> 실패 자리에서 종료코드를 손으로 고르지 않게 한다. 이름(kind)만 고르면
#   번호(code)와 종료코드는 errcodes 표가 정한다 — 같은 상황에서 값이
#   갈리던 원인을 없앤다.
#    1) --json-errors 면 stdout 에 오류 JSON 한 줄
#    2) 화면(stderr)과 오류 로그에는 지금까지와 똑같이
#    3) 표가 정한 종료코드를 돌려준다
#
#   [이름 주의] fail_code 와 마찬가지로 프로세스를 끝내지 않는다.
#   반드시 `return fail_err(...)` 로 써야 한다.
#
# -in: kind = 오류 이름(errcodes.ERRORS 의 키)
# -in: msg  = 사용자에게 보일 안내(여러 줄 가능)
# -in: path = 문제가 된 파일·폴더(없으면 JSON 에서 키 자체가 빠진다, 기본 None)
#
# -out: code = 표가 정한 종료코드
# -out: error = 표에 없는 이름이면 KeyError(개발 중 오타)
#------------------------------------------------------------------
def fail_err(kind, msg, path=None):
    errcodes.emit(kind, msg, path)
    if errcodes.is_json():
        # 같은 내용이 이미 stdout 으로 나갔다. stderr 로 한 번 더 내면, 부르는 쪽이
        # 두 갈래를 합쳐 받을 때(2>&1) JSON 뒤에 사람용 문장이 따라붙어 파싱이 깨진다.
        # 로그 파일에는 그대로 남긴다 — 화면이 없는 배치에서 원인을 찾을 흔적이다.
        logsetup.get_logger("csoclassify.cli").error(
            "%s", " / ".join(msg.splitlines()), stacklevel=2)
        return errcodes.exit_of(kind)
    return fail_code(msg, errcodes.exit_of(kind))


#------------------------------------------------------------------
# 오류 안내 + 오류 로그 + 종료코드 (한 번에)
#=> "화면에 안내하고 코드로 끝낸다"를 한 함수로 묶는다. 예전에는 print 만 하고
#   끝내는 자리가 많아, exe 를 UI·배치로 돌리면 그 안내가 사라져 아무 흔적도
#   남지 않았다. 이 함수를 쓰면 화면 동작은 그대로면서 오류 로그
#   (log/class_err_YYYYMMDD.log)에도 같은 내용이 남는다.
#    1) 화면(stderr)에는 받은 그대로 — 여러 줄이면 여러 줄로 보여 준다
#    2) 로그에는 한 줄로 눌러 담는다(아래 이유 참고)
#    3) 받은 종료코드를 그대로 돌려준다
#
#   [이름 주의] 이 함수는 프로세스를 끝내지 않는다. 종료코드를 '돌려주기만' 한다.
#   그래서 이름이 fail 이 아니라 fail_code 다 — 반드시 `return fail_code(...)` 처럼
#   return 과 함께 써야 한다. return 을 빠뜨리면 메시지·로그만 남고 실행이 그대로
#   이어진다. (Rust 판 errlog::fail() 은 이름이 비슷하지만 정말로 exit 하고
#   돌아오지 않는다 — 두 구현을 오가며 읽을 때 혼동하지 않도록 이름을 구분했다.)
#
# -in: msg  = 사용자에게 보일 안내(여러 줄 가능)
# -in: code = 돌려줄 종료코드(config.EXIT_* 중 하나)
#
# -out: code = 받은 종료코드 그대로 (호출부에서 return fail_code(...) 로 쓴다)
# -out: error = 없음(예외를 던지지 않는다)
#------------------------------------------------------------------
def fail_code(msg, code):
    print(msg, file=sys.stderr)
    # 로그는 한 줄로 눌러 담는다 — 여러 줄이면 로그 파일에서 한 사건이 여러 건처럼 보인다.
    # stacklevel=2 로 '이 함수'가 아니라 '실제로 오류를 낸 호출부'의 위치가 기록되게 한다
    # (포맷에 %(funcName)s/%(lineno)d 를 넣으면 6건 모두 fail_code 로 찍히는 것을 막는다).
    logsetup.get_logger("csoclassify.cli").error(
        "%s", " / ".join(msg.splitlines()), stacklevel=2)
    return code


#------------------------------------------------------------------
# 출력 형식 확정 — --format 이 없으면 --out 확장자로 판단
#=> "--out result.jsonl 로 저장했는데 안에는 JSON 배열이 들어 있어 뷰어가 못 연다"는
#   함정을 없앤다. --out 이름이 사실상 형식을 말하고 있으므로 그대로 따른다.
#    1) --format 을 명시했으면 그 값을 그대로 쓴다(사용자 의도가 최우선)
#    2) 없으면 --out 확장자로 정한다(.jsonl/.ndjson→jsonl, .json→json, .txt→text)
#    3) --out 도 없거나 모르는 확장자면 기본값(json)
#   추론이 일어나면 화면에 한 줄 알린다 — 조용히 형식이 바뀌면 그게 더 놀랍다.
#
# -in: fmt = --format 값(안 줬으면 None)
# -in: out = --out 경로(안 줬으면 None)
# -in: log = 로거(없으면 로그 생략)
#
# -out: str = "text" | "json" | "jsonl"
# -out: error = 없음(모르는 확장자는 기본값으로 환원)
#------------------------------------------------------------------
def resolve_format(fmt, out, log=None):
    # 명시했으면 그대로 — 확장자가 달라도 사용자가 정한 것이 이긴다.
    if fmt:
        return fmt
    if out:
        ext = os.path.splitext(out)[1].lower()
        guessed = {".jsonl": "jsonl", ".ndjson": "jsonl",
                   ".json": "json", ".txt": "text"}.get(ext)
        if guessed:
            if guessed != config.DEFAULT_FORMAT:
                print(f"[MpowerClassify] --out 확장자({ext})에 맞춰 --format {guessed} 로 저장합니다."
                      f" (다르게 하려면 --format 을 직접 지정하세요)", file=sys.stderr)
            if log is not None:
                log.info("출력 형식 추론: out=%s → format=%s", out, guessed)
            return guessed
    return config.DEFAULT_FORMAT


#------------------------------------------------------------------
# 기계가 읽는 줄만 통과시키는 stderr 거름망
#=> --json-errors 를 준 호출에서 사람용 알림([전파]·본문없음·경고 등)을 막는다.
#   자리를 하나씩 고치지 않는 이유: 그런 자리가 60곳이 넘어 빠뜨리기 쉽고,
#   나중에 새로 생기는 줄까지 자동으로 걸러야 하기 때문이다.
#    1) 줄 단위로 모은다(print 는 본문과 줄바꿈을 따로 쓴다)
#    2) [progress]·[summary] 로 시작하는 줄만 진짜 stderr 로 흘린다
#       — 화면 진행바와 완료 메시지가 그 두 줄을 읽는다
#
# -필드: raw  = 진짜 stderr(여기로만 통과시킨다)
# -필드: buf  = 아직 줄바꿈을 못 만난 조각
#------------------------------------------------------------------
class _QuietStderr:
    KEEP = ("[progress]", "[summary]")

    #--------------------------------------------------------------
    # 거름망 만들기
    #=> 원래 stderr 를 품고 있다가 통과시킬 줄만 넘긴다.
    #
    # -in: raw = 원래 sys.stderr
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self, raw):
        self.raw = raw
        self.buf = ""

    #--------------------------------------------------------------
    # 글자 받기 — 줄이 완성될 때마다 판단한다
    #=> print 는 "본문"과 "\n" 을 따로 쓴다. 조각만 보고 판단하면 같은 줄을
    #   반쯤 흘려보내게 되므로, 줄바꿈을 만날 때까지 모았다가 검사한다.
    #
    # -in: s = 쓰려는 글자
    #
    # -out: int = 받은 글자 수(파일 객체 흉내)
    # -out: error = 없음
    #--------------------------------------------------------------
    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.startswith(self.KEEP):
                self.raw.write(line + "\n")
        return len(s)

    #--------------------------------------------------------------
    # 남은 조각 내보내기
    #=> 줄바꿈 없이 끝난 마지막 조각도 규칙에 맞으면 흘려보낸다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def flush(self):
        if self.buf.startswith(self.KEEP):
            self.raw.write(self.buf)
            self.buf = ""
        self.raw.flush()

    #--------------------------------------------------------------
    # 그 밖의 파일 객체 흉내
    #=> isatty() 처럼 라이브러리가 물어보는 것들을 원래 stderr 에 넘긴다.
    #
    # -in: name = 찾는 속성 이름
    #
    # -out: 원래 stderr 의 그 속성
    # -out: error = 없으면 AttributeError
    #--------------------------------------------------------------
    def __getattr__(self, name):
        return getattr(self.raw, name)


#------------------------------------------------------------------
# 상태 줄에 실을 '대상' 뽑기
#=> 부르는 쪽이 여러 폴더를 돌릴 때, 어느 실행의 결과인지 알아야 한다.
#   인자를 다시 해석하지 않고 날것의 argv 에서 --file/--dir 값만 집어 온다
#   (여기는 argparse 가 실패한 뒤에도 불릴 수 있는 자리다).
#
# -in: argv = 인자 리스트(None 이면 sys.argv[1:])
#
# -out: str = --file 또는 --dir 값 · 없으면 None
# -out: error = 없음
#------------------------------------------------------------------
def _target_of(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    for i, a in enumerate(argv):
        if a in ("--file", "-file", "--dir", "-dir") and i + 1 < len(argv):
            return argv[i + 1]
    return None


#------------------------------------------------------------------
# 진입점 — 예상 못 한 오류까지 파일에 남기는 바깥 껍데기
#=> 실제 처리는 _main() 이 한다. 여기서는 그 바깥을 try 로 감싸, 어디서든 잡히지
#   않은 예외가 올라오면 오류 로그에 스택까지 남기고 종료코드로 환원한다.
#   왜 필요한가: exe 로 배포하면 UI·배치·스케줄러가 실행해 화면이 없다. 그러면
#   파이썬이 찍는 트레이스백이 그대로 사라져 "왜 죽었는지" 알 방법이 없어진다.
#
# -in: argv = 인자 리스트(None 이면 sys.argv 사용)
#
# -out: code = 종료코드(정상 0, 그 밖은 각 모드가 정한 값, 예상 못 한 예외는 1)
# -out: error = 없음(모든 예외를 코드로 환원 — 여기서 예외를 올리면 로그가 안 남는다)
#------------------------------------------------------------------
def main(argv=None):
    # 인자 해석이 실패해도 JSON 으로 알려야 하므로, 파서를 만들기 전에 날것의
    # argv 를 훑어 옵션을 먼저 켠다.
    errcodes.enable_json(errcodes.wants_json(argv))
    # 기계가 읽는 호출에서는 사람용 알림을 stderr 로 내지 않는다(로그 파일에는 남는다).
    if errcodes.is_json():
        sys.stderr = _QuietStderr(sys.stderr)
    try:
        #------------------------
        # _main 호출
        #------------------------
        code = _main(argv)

        # 성공이든 부분 실패든 마지막에 상태 한 줄을 낸다(이미 실패 줄을 냈으면
        # emit_final 이 알아서 넘어간다).
        errcodes.emit_final(code, _target_of(argv))
        return code
    except SystemExit:
        raise                       # sys.exit() 는 정상 흐름이므로 그대로 통과
    except KeyboardInterrupt:
        # 사용자가 Ctrl+C 로 멈춘 것은 '오류'가 아니다 — 로그를 더럽히지 않는다.
        if not errcodes.is_json():
            print("\n[MpowerClassify] 사용자가 중단했습니다.", file=sys.stderr)
        return 130
    except BaseException as e:      # noqa: BLE001  (여기서 놓치면 흔적이 안 남는다)
        log = logsetup.get_logger("csoclassify.cli")
        # exc_info=True 로 스택까지 남긴다 — 한 줄 메시지만으로는 원인을 못 찾는다.
        log.error("예상하지 못한 오류로 중단: %s: %s", type(e).__name__, e, exc_info=True)
        errcodes.emit("internal_error", f"{type(e).__name__}: {e}")
        if not errcodes.is_json():
            print(f"[MpowerClassify] 예상하지 못한 오류: {type(e).__name__}: {e}", file=sys.stderr)
            print(f"[MpowerClassify] 자세한 내용은 오류 로그를 보세요: "
                  f"{logsetup.default_err_log_path()}", file=sys.stderr)
        return 1


#------------------------------------------------------------------
# 메인 (핵심)
#=> 인자를 파싱하고 모드를 분기해 실행한다. 파일 대상이 없으면 사용법을 알린다.
#    1) 데몬 제어 명령이면 그쪽 처리
#    1.4) --check-rules 면 규칙셋만 검사하고 종료(문서 불필요)
#    2) 모델 별칭 검증
#    3) 대상 파일 수집 → text-only/일반 처리
#
# -in: argv = 명령행 인자 리스트(None 이면 sys.argv 사용)
#
# -out: code = 프로세스 종료코드
# -out: error = 잡지 못한 예외는 그대로 올린다(바깥 main() 이 받아 로그를 남기고 코드로 환원)
#------------------------------------------------------------------
def _main(argv=None):
    # 출력 인코딩을 UTF-8 로 고정한다. 얼려진(PyInstaller exe) 환경에서는 PYTHONUTF8 이
    # 무시되어 stdout/stderr 가 로케일(예: 한국어 Windows=cp949)로 나가는데, UI 가
    # 파이프를 UTF-8 로 읽으면 [progress] 줄의 한글 파일명이 깨진다. 코드로 강제 고정해
    # 소스/모듈/exe 어디서 실행하든 항상 UTF-8 로 내보낸다.
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass  # 스트림이 없거나(windowed) reconfigure 미지원이면 그냥 둔다

    #------------------------------------------------------------
    # 입력 인자 파싱
    # => --dir, --simple-why, --nosummary, --json-errors 등등...
    #------------------------------------------------------------
    parser = build_parser()
    args = parser.parse_args(argv)

    # (0) 로깅 먼저 구성: --log 없으면 exe 옆 log/ 폴더 기본 경로를 쓴다.
    #     여기서 확정한 절대경로를 데몬에도 그대로 넘겨(같은 파일에 기록) 한다.
    args._log_path = os.path.abspath(args.log) if args.log else logsetup.default_log_path()
    # 오류(ERROR 이상)는 log/class_err_YYYYMMDD.log 에도 따로 쌓는다.
    # 일반 로그는 정상 처리 기록까지 수천 줄이라, 문제만 빨리 보려면 별도 파일이 필요하다.
    args._err_log_path = logsetup.default_err_log_path()
    logsetup.setup_logging(args._log_path, args.verbose, err_log_path=args._err_log_path)
    log = logsetup.get_logger("csoclassify.cli")

    # 어떤 실행이었는지 남겨 재현/추적을 돕는다: (1) 실제 커맨드라인 풀경로, (2) 파싱된 argv.
    log.info("실행 cmd=%s", _full_command_line())
    from . import decrypt as decrypt_mod
    log.info("실행 argv=%s", decrypt_mod.mask_args(list(argv if argv is not None else sys.argv[1:])))
    # --decrypt 조합이 틀렸으면 아무것도 읽기 전에 끝낸다.
    _dc = check_decrypt_args(args)
    if _dc is not None:
        return _dc
    # 사이냅을 명시해 골랐는데 없으면 문서를 읽기 전에 끝낸다(1016).
    _sc = check_synap_args(args)
    if _sc is not None:
        return _sc
    # 등급 스트림 옵션 조합이 틀렸거나 쓸 수 없는 플랫폼이면 읽기 전에 끝낸다.
    _gc = check_grade_stream_args(args)
    if _gc is not None:
        return _gc
    # 사람 등급 지정 인자(--set-grade · --grade-overrides)도 읽기 전에 본다.
    _oc = check_override_args(args)
    if _oc is not None:
        return _oc

    # (0-1) 출력 형식 확정 — 아래 모든 모드가 args.fmt 를 그대로 읽으므로 여기서 한 번만 정한다.
    args.fmt = resolve_format(args.fmt, args.out, log)

    # (0-1b) --set-grade 는 문서를 읽지 않는 별도 모드다 — 규칙 · 모델을 올리기 전에 끝낸다.
    if getattr(args, "set_grade", None) is not None:
        if args.out:
            _ensure_out_parent(args.out)
        try:
            _sg_fp = open(args.out, "w", encoding="utf-8") if args.out else None
        except OSError as e:
            return fail_err("output_write_failed",
                            f"[MpowerClassify] 출력 파일을 쓰지 못했습니다: {args.out}\n  {e}", args.out)
        try:
            return run_set_grade(args, _sg_fp)
        finally:
            if _sg_fp is not None:
                _sg_fp.close()

    # (0-2) 크기 상한(Size Gate) CLI 덮어쓰기를 여기서 한 번만 반영한다.
    _apply_size_limit_overrides(args, log)

    # (1) 데몬 제어 명령 우선 처리.
    dc = handle_daemon_commands(args)
    if dc is not None:
        log.info("데몬실행(--serve/--status/--stop)")
        return dc

    # (1.3) --failsafe 값 검증. 이 값은 규칙셋을 거치지 않고 곧바로 최종 등급이 되므로,
    #   오타가 있으면 정의되지 않은 등급이 그대로 결과에 박힌다(규칙셋 오타와 같은 종류의
    #   fail-open). 스캔 시작 전에 막는다.
    #   => 어느 신호로도 못 정한 문서에 부여할 기본등급. C/S/O 만 허용(그 외는 종료코드 4)
    #   => 지정하지 않으면 기본등급: S 
    if args.failsafe is not None:
        from .classify import GRADES
        if args.failsafe not in GRADES:
            # 부른 쪽이 준 값이 틀린 것이므로 '인자 오류(3)'다. 예전에는 규칙셋
            # 내용 오류와 같은 4 로 나가 배치가 원인을 구분할 수 없었다.
            return fail_err("bad_failsafe",
                            f"[MpowerClassify] --failsafe 값이 올바르지 않습니다: {args.failsafe!r}\n"
                            f"  정의된 등급: {' < '.join(GRADES)}")

    # (1.35) 없어진 옵션(--export-taxonomy·--sync-doc-rule·--taxonomy 등)을 줬으면 안내하고 멈춘다.
    code = check_removed_options(args)
    if code is not None:
        return code

    # (1.354) 기준 문서 제목 채우기 모드: 기준 문서 파일과 본문(또는 원본)만 본다.
    if getattr(args, "seed_fill_title", False):
        log.info("기준 문서 제목 채우기(--seed-fill-title)")
        return run_seed_fill_title(args)

    # (1.355) 업무분류 규칙 자동 생성 모드: 체계 JSON 과 정책 폴더만 있으면 된다.
    # => doc_rule.yaml 을 통째로 새로 만든다(설계: 업무분류-규칙파일-자동생성-설계).
    if getattr(args, "build_doc_rule", False):
        log.info("문서분류규칙파일 doc_rule.yaml 자동 생성(--build-doc-rule)")
        return run_build_doc_rule(args)

    # (1.365) 규칙 단어 제안 모드: 확정된 라벨과 저장해 둔 본문만 본다.
    # => 문서도 모델도 읽지 않고, 규칙에 넣을 만한 말의 후보를 보여 주기만 한다.
    if getattr(args, "suggest_terms", None):
        log.info("업무분류 규칙 단어 제안(--suggest-terms %s)", args.suggest_terms)
        return run_suggest_terms(args)

    # (1.37) 기준 문서 등록 모드: 인자를 먼저 다 검사한 뒤 분류 경로에 태운다.
    # 문서를 절반 읽고 나서 인자가 틀린 것을 알면, 이미 쓴 것과 안 쓴 것이 섞여
    # 되돌리기 어렵다. 그래서 읽기 전에 전부 본다(설계서 6장).
    if seedcli.is_seed_add(args):
        log.info("기준 문서 등록 모드(--seed-add)")
        plan, err = seedcli.build_plan(args, tax=None)
        if err:
            return fail_err(err[0], "[MpowerClassify] %s" % err[1])
        # 분류 경로가 그대로 돌도록 대상 파일을 인자에 실어 준다.
        args._seed_plan = plan
        args._seed_recs = []
        args._seed_titles = {}
        args.file = None
        # 지문과 벡터는 이 모드에서 선택이 아니다 — 둘 다 없으면 기준으로 못 쓴다.
        args.hash = True
        args.with_vector = True
        # stdout 에는 등록된 기준 문서 줄만 나가야 한다(설계 P5 — stdout 은
        # 기계가 읽는 것). 분류 레코드도 요약 레코드도 여기서는 소리가 된다.
        args.no_summary = True
        args.summary_only = False
        args.fmt = "jsonl"

    # (1.4) 규칙셋 검사 모드: 문서도 모델도 필요 없다 → 파일 수집 전에 먼저 끝낸다.
    # => cso_rule.yaml, doc_rules.yaml, doc_taxonomy.yaml 파일 유효성 검사.
    if getattr(args, "check_rules", False):
        log.info("규칙파일 유효성 검사(--check_rules)")
        return run_check_rules(args)

    # (1.5) 전파 모드: 입력이 레코드 파일이라 --file/--dir·모델이 필요 없다 → 먼저 처리.
    # => cli.py --propagate / --auto-propagate 인자로 실행하면.
    if args.propagate:
        log.info("전파모드실행(--propagate, --auto-propagate)")
        return run_propagate(args)

    # (2) 모델 별칭이 유효한지 먼저 확인(빠른 실패).
    # => DEFAULT_MODEL = "e5-small-ko" 로 정의되어 있음.
    try:
        config.get_model_spec(args.model)
    except KeyError as e:
        return fail_err("bad_args", f"[MpowerClassify] {e}")

    # (3) 대상 파일 수집.
    # => --file 혹은 --dir 처리
    # => --dir 인 경우에는 --glob 로 확장자도 지정(# *.hwp,*.pdf)할수 있음.
    # 목록과 --file/--dir 을 같이 주면 어느 쪽이 진짜 대상인지 알 수 없다 —
    # 조용히 하나를 고르면 '왜 저 파일이 빠졌지?' 로 이어지므로 그 자리에서 막는다.
    if getattr(args, "files_from", None) and (args.file or args.dir):
        return fail_err("bad_args",
                        "[MpowerClassify] --files-from 은 --file/--dir 과 함께 쓸 수 없습니다.")

    # (3-a) 입력 목록(--filelist) 로드 — 대상 수집보다 먼저 한다.
    # 목록이 깨져 있으면(F1·F3) 문서를 한 건도 읽기 전에 멈추는 것이 맞다.
    # 절반쯤 잘못된 ID 가 붙은 결과가 나가는 것이 최악이기 때문이다(규칙셋 검증과 같은 원칙).
    args._filelist = None
    if getattr(args, "filelist", None):
        from . import filelist as filelist_mod
        try:
            args._filelist = filelist_mod.load(args.filelist)
        except filelist_mod.FileListError as e:
            # '파일이 없다'와 '내용이 잘못됐다'는 부르는 쪽의 대응이 다르므로 코드를 나눈다.
            kind = ("filelist_missing" if not os.path.isfile(args.filelist or "")
                    else "filelist_invalid")
            return fail_err(kind, f"[MpowerClassify] {e}")
        st = args._filelist.stats
        # 주석이 있으면 몇 줄을 건너뛰었는지도 알려 준다 — "왜 3건만 읽혔지?" 를
        # 파일을 열어 보지 않고 알 수 있게.
        _skipped = (f" · 주석 {st['comments']}줄 건너뜀"
                    if st.get("comments") else "")
        print(f"[MpowerClassify] 목록 {os.path.basename(args.filelist)}: "
              f"{st['loaded']}건 적재(데이터 {st['lines']}줄{_skipped})", file=sys.stderr)
        for w in args._filelist.warnings:
            print(f"[MpowerClassify] {w}", file=sys.stderr)
        # 열쇠 칸(mrk·enc_mode·zip_mode)이 붙은 줄이 있으면 알린다 — 풀어서 읽는다는 것을
        # 모르고 돌리는 일이 없게. --no-decrypt 면 풀지 않는다는 것도 함께 말한다.
        _n_dec = args._filelist.dec_count()
        if _n_dec:
            if args.no_decrypt:
                print(f"[MpowerClassify] 목록에 복호화 정보가 {_n_dec}건 있지만 --no-decrypt 라 "
                      f"풀지 않고 그대로 읽습니다", file=sys.stderr)
            else:
                print(f"[MpowerClassify] 복호화 정보가 있는 문서 {_n_dec}건 — 한 건씩 풀어서 "
                      f"분류하고 바로 지웁니다", file=sys.stderr)

    #-------------------------------------------
    # 문서 수집
    # => --file, --dir, --filelist 등 인자값에 따라 분류할 문서수집
    #-------------------------------------------
    try:
        files = collect_files(args)
    except OSError as e:
        # --files-from 목록 파일 자체를 못 연 경우(없음·권한). 예전에는 예외가 그대로
        # 올라가 internal_error(9001)로 나갔다 — Rust 판과 같은 no_input(1001)으로 맞춘다.
        return fail_err("no_input",
                        f"[MpowerClassify] --files-from 목록 파일을 읽을 수 없습니다: "
                        f"{args.files_from}\n  {e}", args.files_from)
    # 등록 모드에서는 계획에 적힌 문서가 곧 대상이다(--file/--dir 은 못 쓴다).
    if getattr(args, "_seed_plan", None):
        files = [p["file"] for p in args._seed_plan]
    log.info("대상수집파일목록(%d건): dir=%s, file=%s", 
             len(files), args.dir, args.file)
    
    # 수집대상목록 출력 해봄.
    #for f in files:
    #    print(f"[대상수집목록] {f}", file=sys.stderr)
    #    log.inof(f"[대상수집목록] {f}")

    if not files:
        # 두 상황을 갈라 준다 — 부르는 쪽의 대응이 다르다.
        #   · 대상을 아예 안 줌(1002)      → 명령 자체를 고쳐야 한다
        #   · 줬는데 0건(1001)             → 사용자에게 폴더를 다시 물으면 된다
        target = (args.file or args.dir or getattr(args, "files_from", None)
                  or getattr(args, "filelist", None))
        if not target:
            return fail_err("no_target_arg",
                            "[MpowerClassify] 처리할 파일이 없습니다. "
                            "--file <경로> · --dir <폴더> · --files-from <목록> · "
                            "--filelist <목록> 중 하나를 지정하세요.")
        # 왜 0건인지를 상황에 맞게 말해 준다 — "없다"만으로는 무엇을 고칠지 모른다.
        if args.file:
            why = ("폴더입니다 — 폴더는 --dir 로 지정하세요."
                   if os.path.isdir(args.file) else "그런 파일이 없습니다.")
        elif getattr(args, "files_from", None):
            why = "목록이 비었거나, 목록의 경로가 모두 실제 파일이 아닙니다."
        else:
            why = f"폴더가 없거나, --glob 패턴({args.glob})에 맞는 파일이 없습니다."
        return fail_err("no_input",
                        f"[MpowerClassify] 처리할 파일이 없습니다: {target}\n  {why}",
                        target)

    # (3-0) 추출 본문 보존(--textsave) 준비 — 설계 9장.
    #   폴더를 못 쓰면 여기서 끝낸다. 압축을 풀고 문서를 다 돌린 뒤에
    #   "한 건도 저장 못 했다"를 알게 되는 것이 가장 나쁘다.
    _ts_code = resolve_textsave(args)
    if _ts_code is not None:
        return _ts_code

    # (3-1) 압축파일(zip) 확장: zip 이 섞여 있으면 임시폴더에 풀어 내부 '파일별'로 나눈다.
    #   → 압축 1개가 여러 건으로 분류돼 어느 내부 파일이 C/S/O 인지 알 수 있다.
    #   임시폴더는 처리 후 반드시 지운다(민감정보 잔존 방지). 일반 파일만 있으면 그대로.
    #
    # => Windows 기준으로 이런 폴더를 만듬.(압축푸는건 아님. 폴더만 만듬)
    # C:\Users\bong9\AppData\Local\Temp\cso_zip_a7f3k1qz\
    #                              └────┬────┘└──┬──┘
    #                                prefix    자동 생성된 무작위 문자
    import shutil
    import tempfile
    from .extract.archive import expand_paths

    # (3-0b) 엠파워 v11 암호화 문서 — 복호화할 문서를 먼저 갈라 둔다(설계 §9 '구조 변경').
    #   이 문서들은 아래에서 미리 펼치지 않는다. 지금 펼치려면 먼저 다 풀어야 하고, 그러면
    #   평문 수천 건이 한꺼번에 디스크에 놓인다. 분류 루프가 한 건씩 풀기→펼치기→분류→지우기로
    #   처리한다(_iter_dec_jobs). 기준 문서 등록(--seed-add)·벡터 단독(--embed)은 대상이 아니다.
    dec_plans = {}
    if not getattr(args, "_seed_plan", None) and not args.embed:
        for _f in files:
            _plan = _dec_plan_for(args, _f)
            if _plan is not None:
                dec_plans[safe_text(_f)] = _plan
    # 복호화 도구가 있는지 문서를 읽기 전에 본다. 없으면 대상 전부가 같은 이유로 실패한
    # 결과 파일이 만들어진다(설계 §5). 둘 다 0 인(암호화 안 된) 문서뿐이면 보지 않는다.
    dec_cmd = decrypt_mod.resolve_cmd(args.decrypt_cmd)
    if any(p.info is not None and not p.info.is_plain() for p in dec_plans.values()):
        _why = decrypt_mod.preflight(dec_cmd)
        if _why:
            return fail_err("decrypt_cmd_missing", f"[MpowerClassify] {_why}", dec_cmd)
    args._dec_ctx = None

    arch_tmp = tempfile.mkdtemp(prefix="cso_zip_")

    # 파싱된 args 에서 파이프라인/데몬이 쓰는 처리 옵션만 뽑아 dict 로 만든다
    opts = make_opts(args)

    # --out 이 있으면 결과 본문을 파일로 쓴다(시간요약은 여전히 stderr).
    out_fp = None
    if args.out:
        # 상위 폴더가 없으면 만들어 둔다(Rust 판과 같다). 못 만들면 아래 open 이 까닭을 알린다.
        _ensure_out_parent(args.out)
        try:
            out_fp = open(args.out, "w", encoding="utf-8")
        except OSError as e:
            # 권한·디스크·다른 프로그램이 잡고 있음 — 결과를 만들어도 둘 데가 없다.
            shutil.rmtree(arch_tmp, ignore_errors=True)
            return fail_err("output_write_failed",
                            f"[MpowerClassify] 출력 파일을 쓰지 못했습니다: {args.out}\n  {e}",
                            args.out)
        
    #-----------------------------------------------------
    # 분류 시작
    #-----------------------------------------------------
    try:
        # 압축 해제 상한(G4)에 걸린 건을 분류 요약까지 실어 나르려고 stats 를 준다.
        # args 에 실어 두는 것은 _filelist·_log_path 와 같은 이 파일의 관례다.
        args._archive_stats = {}
        # 복호화할 문서는 빼고 펼친다 — 그 문서들은 차례가 왔을 때 풀어서 펼친다.
        _plain = [f for f in files if safe_text(f) not in dec_plans]
        jobs = expand_paths(_plain, arch_tmp, stats=args._archive_stats)
        jobs.extend(f for f in files if safe_text(f) in dec_plans)
        if dec_plans:
            # 임시 폴더는 문서를 읽기 직전에 만든다 — 위의 사전 점검에서 끝나면 빈 폴더도 안 남는다.
            try:
                _sess = decrypt_mod.Session(
                    dec_cmd, decrypt_mod.resolve_tmp_base(args.decrypt_tmp),
                    args.decrypt_timeout if args.decrypt_timeout is not None
                    else decrypt_mod.DEFAULT_TIMEOUT_SECS)
            except decrypt_mod.DecError as e:
                return fail_err("decrypt_unavailable", f"[MpowerClassify] {e.message}")
            # 추출기가 로그에 적는 '읽은 경로'가 임시 경로로 나가지 않게 한다.
            _sess.attach_log_mask(logsetup.get_logger("csoclassify"))
            args._dec_ctx = _DecContext(dec_plans, _sess)

        # 명시 모드가 있으면 그쪽으로, 없으면 기본 = C/S/O 분류.
        # => --text-only 일때 문서에서 text만 추출(*python 모드 exe 일때만)
        if args.text_only: 
            return run_text_only(jobs, opts, out_fp, args._dec_ctx)
        # => --embed 일때는 문서에서 벡터만 추출(*python 모드 exe 일때만)
        if args.embed:
            return run_embed(jobs, args, opts, out_fp)

        #--------------------------------------------------------------------------
        # 실제 분류 처리.
        # => --text-only, --embed 옵션 외는 분류진행.
        # => text 추출/정제 -> 1단계 rule 분류 -> 벡터생성 -> 씨드생성 -> 2차 전파(분류)
        #--------------------------------------------------------------------------
        return run_classify(jobs, args, out_fp)
    except decrypt_mod.DecError as e:
        # 환경 문제(도구를 못 띄움 · PHP 확장 없음) — 다음 문서도 똑같이 실패한다.
        # 남은 문서를 전부 실패로 채운 결과를 내느니 여기서 멈춘다(설계 §7).
        return fail_err("decrypt_unavailable",
                        f"[MpowerClassify] 복호화 환경 문제로 중단합니다 — {e.message}")
    finally:
        # 풀어 둔 평문을 통째로 지운다(정상 종료 · 예외 · Ctrl+C 모두 여기를 지난다).
        if getattr(args, "_dec_ctx", None) is not None:
            args._dec_ctx.sess.close()
        if out_fp is not None:
            out_fp.close()
        # 압축 확장에 쓴 임시폴더(내부 원문 포함)를 통째로 정리한다.
        shutil.rmtree(arch_tmp, ignore_errors=True)
