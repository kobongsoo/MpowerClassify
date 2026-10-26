//! MpowerClassify-rs — MpowerClassify rule-only 분류 코어의 Rust 포팅 (PoC).
//! 추출(text/html/ooxml) + 규칙 분류(PII·키워드·민감·스탬프·경로·파일명·융합) + CLI.

mod archive;
mod textsave;
mod axes;
mod bm25;
mod conflict;
mod daemon;
mod decrypt;
mod detect;
mod districts;
mod doc_rules;
mod doctype;
mod docbuild;
mod docvocab;
mod embed;
mod errcodes;
mod errlog;
mod log;
mod extract;
mod synap;
mod gradestream;
mod gradeoverride;
mod html_entities;
mod filelist;
mod hwp5;
mod limits;
mod office_legacy;
mod ole;
mod pii;
mod pii_fold;
mod propagate;
mod record;
mod rules;
mod seedcli;
mod taxhead;
mod termsuggest;
mod seedstore;
mod xls;
mod xlsdate;

use std::path::{Path, PathBuf};
use std::time::Instant;

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use axes::Taxonomy;
use doc_rules::{ConflictSpec, DocRuleSet};
use rules::{build_record, load_rules, Grade, RuleSet};

#[derive(Clone)]
struct Opts {
    file: Option<String>,
    dir: Option<String>,
    files_from: Option<String>, // 경로 목록 파일("-" 이면 stdin). 흩어진 파일을 한 프로세스로
    filelist: Option<String>,   // {path, sfile_id} 목록. 결과의 doc_id 를 채운다(설계 §7-5-2-1)
    // 일반 로그(log/class_YYYYMMDD.log). 예전에는 --log 를 받고도 아무 데도 쓰지
    // 않아, 로그가 남은 줄 알고 나중에 찾으면 파일이 없었다(조용한 실패).
    log: Option<String>,
    verbose: bool,
    // 임베딩 상주 서버(daemon 모듈) — 설계: plan/Rust-임베딩모델-상주서비스-설계.html
    //  · serve/status/stop : 서버 기동·조회·정지(handle_daemon_args 가 처리하고 끝낸다)
    //  · daemon            : 분류할 때 임베딩을 상주 서버에 맡긴다(기본은 직접 로드)
    //  · idle_timeout      : 서버 유휴 자동 종료(초). 0 = 무한
    serve: bool,
    status: bool,
    stop: bool,
    daemon: bool,
    idle_timeout: u64,
    // 추출기 선택(설계: plan/Rust-사이냅추출기-옵션-설계-20261019.html).
    //  · synap_only     : 모든 문서를 사이냅(snf_exe)으로
    //  · synap_fallback : 자체 파서가 못 읽은 문서만 사이냅으로
    //  · synap_path     : snf_exe 파일을 직접 가리킨다(없으면 찾기 순서대로)
    synap_only: bool,
    synap_fallback: bool,
    synap_path: Option<String>,
    // 등급 스트림 기록(설계: plan/등급스트림-기록-옵션-설계-20261019.html).
    //  · write_grade_stream        : 분류한 문서의 :mgrade 에 최종 등급을 쓴다
    //  · grade_stream_unclassified : 등급 없는 문서 — "skip"(기본) | "none"(NONE 기록)
    //  · grade_stream_update_time  : 쓸 때 수정 시각이 바뀌게 둔다(툴 기본). 기본은 시각 보존(설계 §14)
    //  · embed_flag                : --embed 를 받았나(이 판은 무시하지만 등급 스트림과는 충돌로 본다)
    write_grade_stream: bool,
    grade_stream_unclassified: Option<String>,
    grade_stream_update_time: bool,
    embed_flag: bool,
    // 사람이 정한 등급(설계: plan/사람등급지정-수동오버라이드-설계-20261020.html).
    //  · grade_overrides : 사람 결정 목록(UI 의 cso_override.jsonl)
    //  · set_grade       : --file 문서에 판정 없이 바로 정할 등급(C/S/O/NONE)
    //  · reviewer/reason : --set-grade 결정을 목록에 덧붙일 때 검토자 · 사유
    grade_overrides: Option<String>,
    set_grade: Option<String>,
    reviewer: Option<String>,
    reason: Option<String>,
    no_doc_id: bool,            // 결과에 doc_id/key 를 넣지 않는다(기본은 넣는다)
    report_missing_id: Option<String>, // sfile_id 를 못 얻은 문서 목록을 쓸 경로
    rules_path: Option<String>,
    // [설계 7단계] --taxonomy(doc_taxonomy.yaml 을 따로 읽던 길)는 없앴다 — 분류체계는
    // --build-doc-rule 로 만든 doc_rule.yaml 안에 들어 있다.
    doc_rules: Option<String>,  // doc_rule.yaml(업무분류 규칙 + 분류체계)
    axis: Option<String>,       // "security" | "doctype" — 이번 실행에서 쓸 축을 하나로 좁힘
    conflict: Option<String>,   // 실행 시 축 전략 덮어쓰기(예: doctype=top_n:3)
    glob: Option<String>,       // --dir 에서 고를 파일 패턴(콤마·중괄호로 여러 개)
    export_input: Option<String>,   // --build-doc-rule 의 원본 체계 JSON(MpowerV11 내보내기)
    out: Option<String>,
    fmt: String,        // "json" | "jsonl"
    fmt_explicit: bool, // --format 을 직접 줬는가(안 줬으면 --out 확장자로 판단)
    simple: bool,
    summary_only: bool,
    no_summary: bool,
    hash: bool,
    with_pii: bool,
    with_text: bool,       // 추출(정제) 텍스트를 결과 레코드의 text 칸에 함께 싣는다

    rule_only: bool,
    vector_only: bool,
    doctype_vector_only: bool,   // 업무분류만 규칙 없이 seed 비교로
    progress: bool,              // 파일마다 '[progress] 처리수/총수 경로' 를 stderr 로(화면 진행바용)
    embed_needed: bool,          // 임베딩을 '아직 못 정한 문서'에만(Python --embed-needed 와 같은 뜻)
    no_timing: bool,             // 처리 시간 출력 끄기(요약줄의 총시간)
    build_doc_rule: bool,        // 체계 JSON·사전·local 조정으로 doc_rule.yaml 을 통째로 만든다
    seed_fill_title: bool,       // 기준 문서에 제목(첫 줄)을 채운다 — 업무분류 BM25 2차 재료
    // 규칙 단어 제안(--suggest-terms) — 문서도 모델도 읽지 않는 단독 모드.
    suggest_terms: Option<String>,   // 분류 하나의 dc_id 또는 "all"
    overrides: Option<String>,       // 검토 화면 확정·거절 이력(cso_override.jsonl)
    text_dir: Option<String>,        // --textsave 로 저장해 둔 추출 본문 폴더
    stopwords: Option<String>,       // 제안하지 않을 말 목록(doc_rule_stopwords.yaml)
    suggest_json: Option<String>,    // 결과를 json 으로도 남길 경로
    suggest_min_docs: Option<usize>, // 제안을 시작할 최소 확정 문서 수
    include_auto_seeds: bool,        // 사람이 승인하지 않은 seed 도 재료에 넣는다
    with_vector: bool,      // 모든 문서를 임베딩해 결과 레코드에 vector 필드로 실어 보냄
    propagate: Option<String>,  // 1차 결과(jsonl/json)를 읽어 전파만 다시 도는 2차 패스
    auto_propagate: bool,
    seeds: Option<String>,
    // 기준 문서 등록(--seed-add …) — 설계서 plan/기준문서-CLI등록-설계-20260908.html
    seed: seedcli::SeedArgs,
    failsafe: Option<String>,
    check_rules: bool,      // 규칙셋만 검사하고 종료(문서는 읽지 않음)
    json_errors: bool,      // 실패할 때 stdout 에 오류 JSON 한 줄(계약: plan/CLI-오류출력-설계.html)
    simple_why: bool,       // 축약본에 판정 근거 요약(why)을 더한다(--simple 포함)
    // 문서 크기 상한(Size Gate) CLI 덮어쓰기 — 설계: plan/문서크기-상한-설계-20260904.html
    // 우선순위는 파이썬 판과 같게 'CLI > 환경변수 > 기본값'. 지정 안 한 항목은 None.
    size_limits: limits::Overrides,
    no_size_limit: bool,    // 모든 상한 해제(조사·디버깅 전용)
    // 추출 본문 보존 — 설계: plan/추출텍스트-저장-설계-20260907.html
    // 폴더를 필수로 받는다(개인정보가 '어디에 쌓이는지 모르는 채로' 생기면 안 된다).
    textsave: Option<String>,
    // 분류 없이 '추출한 본문만' 내보내는 모드(--text-only). 파이썬 판과 같다.
    // 규칙셋도 읽지 않으므로 순수 파서 성능을 재는 데 쓸 수 있다.
    text_only: bool,
    // 엠파워 v11 암호화 문서 복호화 — 설계: plan/엠파워v11-암호화문서-복호화연동-설계-20261012.html
    //  · decrypt           : --file 한 건을 풀어서 분류한다(기본은 그대로 읽기)
    //  · mrk/enc_mode/zip_mode : removeenc.sh 의 -k · -e · -z 로 넘어갈 값
    //  · name              : 원래 파일 이름(확장자 판별 · 파일명 신호)
    //  · dec_size          : 원래 파일 크기(--size, 글자 그대로. 모양은 check_decrypt_args 가 본다)
    //  · no_decrypt        : 목록에 열쇠가 있어도 풀지 않는다
    //  · decrypt_cmd/tmp/timeout : 도구 위치 · 임시 폴더 · 한 건의 시간 제한(초)
    decrypt: bool,
    mrk: Option<String>,
    enc_mode: Option<String>,
    zip_mode: Option<String>,
    name: Option<String>,
    dec_size: Option<String>,
    no_decrypt: bool,
    decrypt_cmd: Option<String>,
    decrypt_tmp: Option<String>,
    decrypt_timeout: Option<u64>,
}

//------------------------------------------------------------------
// 버전 문자열 만들기 (앱 버전 + PII 포팅 기준 ko-pii 버전)
//=> Python 판(MpowerClassify.exe)은 ko-pii 를 exe 안에 넣어 다니므로 '번들된 버전'을
//   찍는다. 이 판은 ko-pii 를 쓰지 않고 알고리즘을 옮겨 적었으므로 찍을 번들이
//   없다 — 대신 '어느 ko-pii 를 보고 옮겼는지'를 찍는다. 두 판의 이 값이 같아야
//   같은 기준으로 도는 것이고, 다르면 검출 결과가 갈릴 수 있다는 신호다.
//
// -in: 없음
//
// -out: text = "MpowerClassify-rs <앱버전> (ko-pii 포팅 기준 <버전>)" 한 줄
// -out: error = 예외 없음(전부 컴파일 시점에 정해진 상수)
//------------------------------------------------------------------
fn version_text() -> String {
    format!("MpowerClassify-rs {} (ko-pii 포팅 기준 {})",
            env!("CARGO_PKG_VERSION"), pii::KOPII_PORTED_FROM)
}

//------------------------------------------------------------------
// 도움말 출력 (--help / -h)
//=> 옵션이 40개가 넘어 한 덩어리로 쏟아내면 무엇부터 봐야 할지 알 수 없다.
//   "무엇을 읽나 → 무엇으로 판단하나 → 무엇을 내보내나" 순서로 묶어 찍는다.
//
//   [note! 가 아니라 eprintln! 인 이유] note! 는 --json-errors 일 때 조용해진다
//   (기계가 읽는 호출에 사람용 문장을 겹쳐 내지 않으려는 장치다). 그런데 도움말은
//   오류가 아니라 사용자가 대놓고 요청한 출력이라, 조용해지면 --help 가 거의
//   아무것도 안 찍는 이상한 상태가 된다. 그래서 언제나 찍는다.
//
// -in: 없음
//
// -out: 없음(stderr 로 찍는다 — 결과 stdout 과 섞이지 않게)
// -out: error = 없음
//------------------------------------------------------------------
fn usage() {
    // 중괄호는 포맷 자리표시자로 읽히므로 {{ }} 로 escape 한다.
    eprintln!("MpowerClassify-rs {} — 한국어 문서 → C/S/O 보안등급 · 업무분류 자동분류",
              env!("CARGO_PKG_VERSION"));
    eprintln!("");
    eprintln!("사용법:");
    eprintln!("  MpowerClassify-rs --file <파일> [옵션]");
    eprintln!("  MpowerClassify-rs --dir  <폴더> [옵션]        (하위 폴더까지 재귀)");
    eprintln!("");
    eprintln!("─── ① 무엇을 읽나 (대상) ───────────────────────────────────────");
    eprintln!("  --file <파일>             문서 1개");
    eprintln!("  --dir <폴더>              폴더 전체(재귀). 압축파일은 내부 문서로 펼쳐 각각 분류");
    eprintln!("  --files-from <목록>       처리할 경로를 한 줄에 하나씩 적은 파일(\"-\" 이면 표준입력).");
    eprintln!("                            여러 폴더에 흩어진 파일을 한 번에 — --file/--dir 과 배타");
    eprintln!("  --filelist <목록>         MpowerV11 이 뽑아 준 {{path, sfile_id}} 목록(jsonl 또는 csv).");
    eprintln!("                            결과의 doc_id 를 이 목록으로 채운다. --file/--dir 이 있으면");
    eprintln!("                            그쪽이 대상을 정하고 목록은 'ID 사전' 역할만 한다");
    eprintln!("  --glob <패턴>             --dir 에서 고를 파일 패턴(예: \"*.hwp,*.pdf\" · \"*.{{hwp,pdf}}\")");
    eprintln!("");
    eprintln!("─── ② 무엇으로 판단하나 (정책 파일 · 축) ───────────────────────");
    eprintln!("  --cso-rules <cso_rule.yaml>     보안등급 규칙셋(미지정 시 exe 옆/CSOCLASSIFY_POLICY_DIR)");
    eprintln!("  --doc-rules <doc_rule.yaml>     업무분류 규칙셋 — 분류체계도 이 안에 있다(--build-doc-rule 로 만든 것).");
    eprintln!("                                  없으면 업무분류 축을 끔");
    eprintln!("  --seeds <class_seed.jsonl>      전파 비교 기준 seed(미지정 시 exe 옆 class_seed.jsonl)");
    eprintln!("  --axis security|doctype   이번 실행에 쓸 축만 지정(doctype 이면 보안등급 계산 생략)");
    eprintln!("  --conflict <축>=<전략>    업무분류 축 전략 덮어쓰기(예: doctype=top_n:3 · doctype=all)");
    eprintln!("  --check-rules             규칙셋 값만 검사하고 종료(정상 0, 검증 실패 4)");
    eprintln!("");
    eprintln!("─── ③ 어떻게 판단하나 (분류 방식) ─────────────────────────────");
    eprintln!("  (기본)                    규칙 + 임베딩 전파. 규칙이 못 정한 문서를 seed 와 비교해 구제");
    eprintln!("  --rule-only               규칙만 — 임베딩·전파 없음(가장 빠름). --vector-only 와 배타");
    eprintln!("  --vector-only             규칙 없이 임베딩 비교로만(--seeds 필수). --rule-only 와 배타");
    eprintln!("  --doctype-vector-only     업무분류만 기준 문서 비교로(보안등급 축은 그대로)");
    eprintln!("  --auto-propagate          보류 문서를 seed 로 전파(seed 가 있으면 기본 on)");
    eprintln!("  --embed-needed            임베딩을 '아직 못 정한 문서'에만(기본 동작과 같음)");
    eprintln!("  --propagate <결과파일>    1차 결과(jsonl/json)를 읽어 전파만 다시 수행");
    eprintln!("                            (문서·모델 불필요). --file/--dir 대신 쓴다");
    eprintln!("  --failsafe [등급]         아무 신호도 없을 때 줄 기본등급. O/S/C 만(값 생략 시 S)");
    eprintln!("  --model <이름>            임베딩 모델: e5-small-ko(기본) · e5-small · ko-sroberta.");
    eprintln!("                            seed 도 같은 모델로 만든 것이어야 합니다(차원이 다르면 코드 1013)");
    eprintln!("");
    eprintln!("─── ④ 무엇을 내보내나 (출력) ──────────────────────────────────");
    eprintln!("  --out <파일>              결과 저장(미지정 시 stdout)");
    eprintln!("  --format json|jsonl       출력 형식(미지정 시 --out 확장자로 판단, 없으면 json 배열)");
    eprintln!("  --simple                  파일별 결과를 file·hash·doc_id·grade·doctype 으로 줄여서");
    eprintln!("                            doc_id 는 목록의 sfile_id 로 채운 것만 싣는다(폴백은 null — hash 로 가림)");
    eprintln!("  --simple-why              --simple 에 판정 근거 요약(why)을 더한다");
    eprintln!("  --hash                    각 문서 SHA-256 포함(--simple 이면 자동)");
    eprintln!("  --with-vector             모든 문서를 임베딩해 vector 필드(384차원)로 포함(RAG 등)");
    eprintln!("  --with-pii                [프라이버시 예외] 검출된 원문 PII 값 포함(pii 필드)");
    eprintln!("  --with-text               [프라이버시 주의] 추출(정제) 본문을 결과의 text 필드에 포함");
    eprintln!("  --summary / --nosummary   요약만 출력 / 요약 제거(파일별만)");
    eprintln!("  --no-doc-id               결과에 doc_id·key 를 넣지 않는다(보안등급만 볼 때)");
    eprintln!("  --report-missing-id <경로>  sfile_id 를 못 얻어 폴백으로 채운 문서 목록(jsonl).");
    eprintln!("                            그 문서들은 매핑 테이블 적재 대상이 아니다");
    eprintln!("  --progress                파일마다 진행 상황을 stderr 로('[progress] 처리수/총수 경로').");
    eprintln!("                            2차 패스(전파)도 같은 모양으로 단계 이름을 달고 나간다");
    eprintln!("                            ('[progress][전파·보안등급]' · '[progress][전파·업무분류]')");
    eprintln!("  --no-timing               요약줄에서 총시간 표기를 뺀다");
    eprintln!("  --json-errors             실패할 때 stdout 에 오류 JSON 한 줄");
    eprintln!("                            ({{\"error\":{{code,kind,message,path}}}})");
    eprintln!("  -V, --version             버전 출력(앱 버전 + PII 포팅 기준 ko-pii 버전)");
    eprintln!("  -h, --help                이 도움말");
    eprintln!("");
    eprintln!("─── ⑤ 추출 본문을 파일로 남기기 ───────────────────────────────");
    eprintln!("  --textsave <폴더>         분류하면서 뽑아낸 본문을 <sha256>.txt 로 남긴다.");
    eprintln!("                            파일명이 결과의 hash 칸과 같아 결과 한 줄에서 본문으로 바로 간다.");
    eprintln!("                            같이 생기는 _index.jsonl 이 hash 와 원본 경로를 잇는다.");
    eprintln!("                            ※ 개인정보가 평문으로 남습니다 — C(기밀) 문서 본문도 그대로.");
    eprintln!("                              기본은 꺼짐이고 폴더를 반드시 지정해야 합니다.");
    eprintln!("                            ※ 파이썬 판과 파서가 달라 본문이 다릅니다 — 폴더를 나누세요");
    eprintln!("  --text-only               분류하지 않고 뽑아낸 본문만 내보낸다(파이썬 판과 같다).");
    eprintln!("                            규칙셋·PII 검사를 아예 하지 않아 파서 성능을 잴 때 쓴다.");
    eprintln!("                            문서마다 '===== 경로 =====' 줄 다음에 본문이 온다.");
    eprintln!("                            --out 을 주면 파일로, 없으면 화면으로 나간다.");
    eprintln!("                            ※ 이 모드는 본문이 곧 출력이라 --textsave 를 보지 않는다.");
    eprintln!("");
    eprintln!("─── ⑥ 문서 크기 상한 ──────────────────────────────────────────");
    eprintln!("  초대형 문서가 시간·메모리를 무제한으로 먹는 것을 막습니다.");
    eprintln!("  우선순위는 CLI > 환경변수 > 기본값. 0 이하를 주면 해제됩니다.");
    eprintln!("  --max-file-mb N           원본 파일 크기 상한(MB, 기본 100). 넘으면 읽지 않고");
    eprintln!("                            보류 레코드로 내보냅니다(error.kind=size_limit)");
    eprintln!("  --max-file-mb-text N      텍스트 계열(txt/csv/tsv/json/html) 상한(MB, 기본 100).");
    eprintln!("                            이 포맷군만 바이트=글자수라 따로 둡니다");
    eprintln!("  --max-text-chars N        정제 본문 글자수 상한(기본 2,000,000). 넘으면 앞부분만");
    eprintln!("                            보고 분류하고 text_truncated 표식을 답니다");
    eprintln!("  --max-pdf-pages N         PDF 페이지 상한(기본 3000). 넘으면 앞 N 쪽만 읽고");
    eprintln!("                            partial_extract 표식을 답니다");
    eprintln!("  --parser-timeout N        파서 1파일 시간 상한(초, 기본 60). 위와 같은 표식");
    eprintln!("  --no-size-limit           모든 상한 해제 — 조사·디버깅 전용");
    eprintln!("");
    eprintln!("─── ⑦ 압축 확장 ───────────────────────────────────────────────");
    eprintln!("  zip · tar(+gz/bz2/xz) · gz · bz2 · xz · 7z · rar 을 내부 문서로 펼쳐 각각 분류하고,");
    eprintln!("  압축 자체에는 내부 최고 위험을 매긴 집계 레코드(archive:true)를 덧붙입니다.");
    eprintln!("  --max-archive-mb N        압축 1건당 해제 누적 상한(MB, 기본 500). 압축파일 크기가");
    eprintln!("                            아니라 '풀었을 때 합계'다. 넘으면 펼치지 않는다");
    eprintln!("  --max-archive-members N   압축 1건당 내부 파일 개수 상한(기본 5000)");
    eprintln!("                            ※ 분할 볼륨은 어느 포맷도 잇지 않는다(사유를 결과에 남긴다)");
    eprintln!("");
    eprintln!("─── ⑦-2 엠파워 v11 암호화 문서 ───────────────────────────────");
    eprintln!("  엠파워 v11 저장소의 문서는 암호화된 채 놓여 있습니다. 분류 직전에 엠파워의");
    eprintln!("  removeenc.sh 로 한 건씩 풀고, 분류가 끝나면 풀린 파일을 바로 지웁니다.");
    eprintln!("  [다량] --filelist 의 각 줄에 mrk · enc_mode · zip_mode 를 적으면 그 줄만 풉니다.");
    eprintln!("         name(원래 파일 이름) · size(원래 크기)도 함께 주기를 권합니다.");
    eprintln!("         enc_mode 와 zip_mode 가 둘 다 0 인 줄은 도구를 부르지 않고 그대로 읽습니다");
    eprintln!("  [단일] --file 에 아래를 붙일 때만 풉니다(안 붙이면 지금처럼 그대로 읽습니다)");
    eprintln!("  --decrypt                 이 파일을 풀어서 분류");
    eprintln!("  --mrk <값>                평문 MRK(환경변수 CSOCLASSIFY_MRK 로도 줄 수 있음).");
    eprintln!("                            'v2:' 로 시작하는 감싼 값은 받지 않습니다");
    eprintln!("  --enc-mode <값>  --zip-mode <값>   ENC_MODE · ZIP_MODE(0 도 올바른 값)");
    eprintln!("  --name <이름>             원래 파일 이름(확장자 판별 · 파일명 신호)");
    eprintln!("  --size <바이트>           원래 파일 크기. 주면 풀린 결과의 크기와 견줍니다(안 주면 건너뜀)");
    eprintln!("  --no-decrypt              목록에 열쇠가 있어도 풀지 않습니다");
    eprintln!("  --decrypt-cmd <경로>      복호화 도구(환경변수 CSOCLASSIFY_DECRYPT_CMD,");
    eprintln!("                            기본 {})", decrypt::DEFAULT_CMD);
    eprintln!("  --decrypt-tmp <폴더>      풀린 파일을 잠깐 둘 곳(환경변수 CSOCLASSIFY_DECRYPT_TMP,");
    eprintln!("                            기본 시스템 임시 폴더). /dev/shm 을 주면 디스크에 닿지 않습니다");
    eprintln!("  --decrypt-timeout <초>    복호화 한 건의 시간 제한(기본 {})", decrypt::DEFAULT_TIMEOUT_SECS);
    eprintln!("  ※ 풀지 못했거나 풀린 결과가 수상하면 분류하지 않고 error.kind=decrypt_failed 로 남깁니다.");
    eprintln!("     열쇠와 임시 경로는 로그·결과에 남기지 않습니다");
    eprintln!("");
    eprintln!("─── ⑧ 문서분류체계 rule 생성 (문서를 읽지 않는 모드) ─────────────────────");
    eprintln!("  --build-doc-rule          체계 JSON(--export-input)·사전·doc_rule.local.yaml 로 --doc-rules 파일을 통째로 새로 만든다");
    eprintln!("  --export-input <파일>     그 원본 체계 JSON(미지정 시 규칙 파일 옆 doc_classification_export.json)");
    eprintln!("  --seed-fill-title         기준 문서(--seeds)에 제목(첫 줄)을 채운다 — 업무분류 BM25 2차 재료");
    eprintln!("                            본문은 --text-dir/<지문>.txt 에서 먼저 찾고, 없으면 원본을 다시 읽는다");
    eprintln!("");
    eprintln!("─── ⑧-2 규칙 단어 제안 (--suggest-terms · 문서를 읽지 않는 모드) ───────");
    eprintln!("  이미 확정된 라벨과 저장해 둔 본문만 보고, 규칙에 넣을 만한 말의 후보를 보여 줍니다.");
    eprintln!("  규칙 파일은 고치지 않습니다(보여 주기만). 파이썬 판과 같은 후보·차례입니다.");
    eprintln!("  --suggest-terms <dc_id|all>  분류 하나, 또는 all 이면 확정 문서가 있는 분류 모두");
    eprintln!("  --seeds <파일>            재료 ① 기준 문서(미지정 시 exe 옆 class_seed.jsonl)");
    eprintln!("  --overrides <파일>        재료 ② 검토 확정·거절 이력(기본은 정책 폴더 옆 cso_override.jsonl)");
    eprintln!("  --text-dir <폴더>         재료 ③ --textsave 로 저장해 둔 추출 본문.");
    eprintln!("                            없으면 제목·파일 이름만으로 후보를 만든다");
    eprintln!("  --stopwords <파일>        제안하지 않을 말 목록(기본은 정책 폴더 옆 doc_rule_stopwords.yaml)");
    eprintln!("  --suggest-json <경로>     결과를 json 으로도 남긴다");
    eprintln!("  --suggest-min-docs <수>   제안을 시작할 최소 확정 문서 수(기본 3)");
    eprintln!("  --include-auto-seeds      사람이 승인하지 않은 seed 도 재료에 넣는다(권하지 않음)");
    eprintln!("  ※ 재료는 기준 문서·확정 이력 둘 중 하나는 있어야 합니다(없으면 종료코드 3).");
    eprintln!("");
    eprintln!("─── ⑨ 기준 문서 등록 (--seed-add) ─────────────────────────────");
    eprintln!("  화면 없이 class_seed.jsonl 에 기준 문서를 등록합니다. 파이썬 판과 같습니다.");
    eprintln!("  --seed-add <문서…>        등록할 문서(여러 개). 아래 축 값이 모든 문서에 적용");
    eprintln!("  --seed-add-from <목록|->  문서마다 다른 값을 줄 때(jsonl 한 줄에 문서 하나).");
    eprintln!("                            '-' 면 표준입력. --seed-add 와 배타");
    eprintln!("                            칸: file(필수) doc_id grade doctype note reviewer");
    eprintln!("  --seed-grade <C|S|O>      보안등급");
    eprintln!("  --seed-doctype <dc_id,…>  업무분류(콤마로 여러 개)");
    eprintln!("  --seed-reviewer <이름>    ※ 필수 — 없으면 오류(코드 3).");
    eprintln!("                            누가 확정했는지 모르는 기준 문서는 만들지 않습니다");
    eprintln!("  --seed-note <메모>        선택");
    eprintln!("  --seed-audit <경로>       변경 기록 파일(안 주면 --seeds 옆 class_seed_audit.jsonl)");
    eprintln!("  --seeds <파일>            주면 그 파일에 병합(잠금+원자적 교체),");
    eprintln!("                            안 주면 등록될 줄만 stdout 으로(파일 안 건드림)");
    eprintln!("  종료코드: 0 전부 등록 · 1 일부 실패 · 2 한 건도 못 함 · 3 인자 잘못");
    eprintln!("  ※ 지문(hash)과 벡터는 이 모드에서 항상 만듭니다(--hash 를 안 줘도).");
    eprintln!("     지문이 없으면 원본이 바뀌어도 점검이 조용히 넘어갑니다.");
    eprintln!("");
    eprintln!("─── ⑩ 임베딩 상주 서버 ────────────────────────────────────");
    eprintln!("  파일마다 이 프로그램을 따로 부르면 호출마다 임베딩 모델을 새로 읽습니다.");
    eprintln!("  모델을 물고 있는 서버를 띄워 두면 그 시간이 없어집니다(분류 결과는 같습니다).");
    eprintln!("  --serve                   이 프로세스가 상주 서버가 됩니다(정지·유휴 종료까지 실행).");
    eprintln!("                            같은 모델 서버가 이미 있으면 알리고 끝냅니다(코드 0)");
    eprintln!("  --daemon                  임베딩을 상주 서버에 맡깁니다. 서버가 없으면 자동으로 띄우고,");
    eprintln!("                            못 쓰면 모델을 직접 읽습니다. 기본은 꺼짐(직접 읽기)");
    eprintln!("  --no-daemon               상주 서버를 쓰지 않습니다(기본)");
    eprintln!("  --status / --stop         서버 상태 한 줄 / 서버 정지 요청(둘 다 코드 0)");
    eprintln!("  ※ --serve·--status·--stop 의 출력(stdout)은 언제나 JSON 한 줄입니다:");
    eprintln!("     {{\"error\":{{\"code\":0,\"kind\":\"success\",\"message\":\"…\"}},\"daemon\":{{\"running\":true,\"pid\":…,\"port\":…,\"ready\":true,…}}}}");
    eprintln!("     code 0 = 정상(서버가 없다는 답도 정상 — daemon.running 으로 구분). 실패는 code≠0");
    eprintln!("  --idle-timeout <초>       서버가 이만큼 요청이 없으면 스스로 끝납니다. 0=무한(기본)");
    eprintln!("  --model <이름>            서버는 모델마다 하나입니다 — 서버와 호출이 같은 모델이어야 만납니다");
    eprintln!("  [환경변수] CSOCLASSIFY_STATE_DIR  서버 접속 기록을 둘 폴더. 서버를 띄운 계정과");
    eprintln!("             부르는 계정이 다르면 양쪽에 같은 폴더를 줘야 서로 찾습니다");
    eprintln!("");
    eprintln!("─── ⑩-0 추출기 — 기본은 자체 파서, 사이냅(snf_exe)은 고를 때만 ──────");
    eprintln!("  (없음) · --hybridparse   자체 파서만 씁니다(기본). 사이냅이 옆에 있어도 쓰지 않습니다");
    eprintln!("  --synap-fallback          자체 파서가 못 읽은 문서(실패 · 빈 결과 · 20자 미만)만 사이냅으로");
    eprintln!("  --synap-only              모든 문서를 사이냅으로(파이썬 판 --synap-only 와 같음)");
    eprintln!("  --synap-path <경로>       snf_exe 파일을 직접 가리킵니다");
    eprintln!("  [찾기] --synap-path → CSOCLASSIFY_SYNAP_DIR → exe 옆 synap/ → exe 옆 resources/synap/");
    eprintln!("         (각 폴더에서 {}/ 하위폴더 먼저). 못 찾으면 문서를 읽기 전에 1016 synap_missing",
              if cfg!(windows) { "windows" } else { "linux" });
    eprintln!("  [환경변수] CSOCLASSIFY_SYNAP_MODE=off|fallback|only (인자가 이깁니다)");
    eprintln!("");
    eprintln!("─── ⑩-0b 등급 스트림 기록 — 문서 파일의 :mgrade 에 등급을 쓴다(Windows NTFS) ──");
    eprintln!("  --write-grade-stream      최종 등급(C/S/O)을 NEOGradeToolApp -s 와 같은 12바이트로 씁니다");
    eprintln!("                            같은 값이면 쓰지 않습니다. 압축 안 문서는 건너뛰고 압축 파일 자체에 집계 등급");
    eprintln!("  --grade-stream-unclassified skip|none  등급 없는 문서: skip=건드리지 않음(기본), none=NONE(0)");
    eprintln!("  --grade-stream-update-time 쓸 때 수정 시각이 바뀌게 둡니다(툴 기본, -k 없음)");
    eprintln!("                            기본은 생성·접근·수정 시각을 그대로 둡니다");
    eprintln!("  결과 레코드 grade_stream · 요약 grade_stream. 못 쓴 문서가 있으면 4002(종료코드 1)");
    eprintln!("  리눅스에서는 1012 로 거절합니다(ADS 가 없다)");
    eprintln!("");
    eprintln!("─── ⑩-0c 사람이 정한 등급 ─────────────────────────────────────");
    eprintln!("  --grade-overrides <파일>  사람 결정 목록(UI 의 cso_override.jsonl). 판정 뒤 사람 등급이 이깁니다");
    eprintln!("                            결정 뒤 내용이 바뀐 문서는 더 엄격한 결정만 따릅니다(stale)");
    eprintln!("  --set-grade C|S|O|NONE    --file 문서에 판정 없이 바로 등급을 정합니다(--dir 불가)");
    eprintln!("                            --write-grade-stream 이면 파일에도 씁니다");
    eprintln!("  --reviewer · --reason     --set-grade 결정을 --grade-overrides 에 덧붙일 때 필수");
    eprintln!("");
    eprintln!("─── ⑩-1 파이썬 판에는 있고 이 판에는 없는 것 ──────────────────");
    eprintln!("  [씨앗 자동 생성] 규칙 고신뢰 문서를 골라 씨앗을 만드는 기능은 이 판에 없습니다.");
    eprintln!("  --make-doctype-seeds      지원하지 않음 — 오류로 종료(코드 3).");
    eprintln!("                            파이썬 판으로 만드세요(만든 파일은 이 판도 그대로 읽습니다).");
    eprintln!("                            문서 하나씩 등록하려면 --seed-add 를 쓰세요(⑨)");
    eprintln!("");
    eprintln!("─── ⑩-2 실행 기록(로그) ───────────────────────────────────");
    eprintln!("  무엇을 어떻게 분류했는지 파일로 남깁니다. 화면이 없는 환경(UI·배치·");
    eprintln!("  스케줄러)에서 돌리면 이 파일이 유일한 근거가 됩니다.");
    eprintln!("  --log <경로>              로그 파일 경로.");
    eprintln!("                            안 주면 <exe폴더>/log/class_날짜.log");
    eprintln!("  -v, --verbose             같은 내용을 화면(stderr)에도 냅니다.");
    eprintln!("  [환경변수] CSOCLASSIFY_LOGDIR  로그를 모아 둘 폴더(여러 대를 한곳에).");
    eprintln!("             CSOCLASSIFY_ERRLOG  오류 로그 파일 경로.");
    eprintln!("  남는 파일은 두 가지입니다 — 파이썬 판과 이름·자리가 같습니다:");
    eprintln!("    class_날짜.log      실행 명령·파일별 결과·요약까지 전부");
    eprintln!("    class_err_날짜.log  오류만 따로(오류가 났을 때만 생깁니다)");
    eprintln!("");
    eprintln!("─── ⑪ 파이썬 판 인자 중 이 판에서 효과가 없거나 값이 정해진 것 ──────");
    eprintln!("  파이썬 판과 같은 명령줄을 그대로 넣어도 멈추지 않도록 받아 줍니다.");
    eprintln!("  다만 아래처럼 효과가 다르므로 숨기지 않고 적어 둡니다 —");
    eprintln!("  '줬는데 왜 안 되지'를 혼자 헤매는 것이 가장 나쁜 상태이기 때문입니다.");
    eprintln!("  [효과 없음]     --embed  --per-chunk  --normalize  --no-normalize");
    eprintln!("                  --timing  -r, --recursive  --classify");
    eprintln!("                  ※ 벡터가 필요하면 --with-vector, 본문은 --with-text·--textsave·--text-only");
    eprintln!("                  ※ --dir 은 원래 늘 재귀라 -r 은 있으나 마나입니다");
    eprintln!("  [값은 안 씀]    --num-threads  --seed-per-dir  --seed-per-node");
    eprintln!("                  값이 정수가 아니면 멈춥니다(코드 1003) — 파이썬 판으로 옮길 때의 오타를 미리 잡으려고");
    eprintln!("  [기본값만 받음] --max-tokens 512  --overlap 32  --precision f32");
    eprintln!("                  다른 값은 벡터가 파이썬 판과 달라지므로 멈춥니다(코드 1012)");
}

//------------------------------------------------------------------
// 상한 인자 → 정수
//=> "--max-pdf-pages 3000" 같은 값을 읽는다. 0 이하는 '해제'라 0 으로 접는다.
//   숫자가 아니면 조용히 기본값으로 흘리지 않고 그 자리에서 끝낸다 — 오타 하나가
//   상한을 통째로 무효로 만들면 왜 안 걸렸는지 아무도 모른다.
//
// -in: v    = 사용자가 준 값
// -in: flag = 오류 문구에 실을 플래그 이름
//
// -out: u64 = 해석된 값(0 이하는 0 = 해제)
// -out: error = 숫자가 아니면 errcodes::fail 로 종료(반환하지 않음)
//------------------------------------------------------------------
fn parse_u64(v: &str, flag: &str) -> u64 {
    match v.trim().parse::<f64>() {
        Ok(n) if n > 0.0 => n as u64,
        // 0 이하는 '상한 없음'. 음수도 같은 뜻으로 받아 준다.
        Ok(_) => 0,
        Err(_) => errcodes::fail("bad_args",
            &format!("[MpowerClassify-rs] {} 값이 숫자가 아닙니다: {}", flag, v), None),
    }
}

//------------------------------------------------------------------
// MB 단위 상한 인자 → 바이트
//=> --max-file-mb 처럼 사람이 MB 로 주는 값을 바이트로 바꾼다. 소수점을 받는
//   이유는 1MB 미만을 시험할 때 필요해서다(0.01 등).
//
// -in: v    = 사용자가 준 값(MB)
// -in: flag = 오류 문구에 실을 플래그 이름
//
// -out: u64 = 바이트 수(0 이하는 0 = 해제)
// -out: error = 숫자가 아니면 errcodes::fail 로 종료(반환하지 않음)
//------------------------------------------------------------------
fn parse_mb(v: &str, flag: &str) -> u64 {
    match v.trim().parse::<f64>() {
        Ok(n) if n > 0.0 => (n * 1_048_576.0) as u64,
        Ok(_) => 0,
        Err(_) => errcodes::fail("bad_args",
            &format!("[MpowerClassify-rs] {} 값이 숫자가 아닙니다: {}", flag, v), None),
    }
}

//------------------------------------------------------------------
// 크기 상한 CLI 덮어쓰기를 확정 (파이썬 _apply_size_limit_overrides 와 같은 역할)
//=> parse_args 결과를 limits 모듈에 등록한다. 상한을 실제로 보는 곳은 CLI 인자를
//   볼 수 없는 깊은 자리(추출기·PDF 파서)라, 여기서 한 번 새겨 둔다.
//    1) --no-size-limit 이면 모든 상한을 0(=무제한)으로 만든다
//    2) --max-file-mb 로 일반 상한을 낮췄는데 텍스트 상한이 그보다 크면 함께 내린다
//       (단 --max-file-mb-text 를 명시했으면 그 값이 이긴다 — 명시값을 자동 보정이
//        덮으면 플래그를 준 의미가 없다)
//
// -in: o = 파싱된 Opts(size_limits / no_size_limit 를 본다)
//
// -out: 없음(limits::set_overrides 로 등록)
// -out: error = 없음
//------------------------------------------------------------------
fn apply_size_limits(o: &Opts) {
    if o.no_size_limit {
        limits::set_overrides(limits::Overrides {
            max_file_bytes: Some(0), max_file_bytes_text: Some(0),
            max_text_chars: Some(0), parser_timeout: Some(0), max_pdf_pages: Some(0),
            max_archive_bytes: Some(0), max_archive_members: Some(0),
        });
        // 조용히 상한을 끄면 나중에 "그때 왜 안 걸렸지"를 설명할 수 없다.
        errlog::note("[MpowerClassify-rs] --no-size-limit: 문서 크기 상한을 모두 해제했습니다\
 (초대형 문서에서 시간·메모리가 무제한으로 늘 수 있습니다)");
        return;
    }
    let mut ov = o.size_limits;
    // 일반 상한을 낮췄으면 텍스트 상한도 그 아래로 따라 내린다 — '일반 5MB,
    // 텍스트 100MB' 같은 앞뒤 안 맞는 설정이 되지 않게.
    if let Some(n) = ov.max_file_bytes {
        if ov.max_file_bytes_text.is_none() && (n == 0 || limits::max_file_bytes_text() > n) {
            ov.max_file_bytes_text = Some(n);
        }
    }
    limits::set_overrides(ov);
}

//------------------------------------------------------------------
// 없어진 옵션 → 안내 문구
//=> 설계 7단계에서 옛 방식(분류체계 doc_taxonomy.yaml 을 따로 만들고 읽던 길)의 옵션을
//   없앴다. 그 옵션을 주면 무엇으로 바꿔야 하는지 한 줄로 알려 준다. 파이썬 판과
//   같은 문구·같은 코드(unsupported_option, 종료 3)로 나가야 두 판이 같아진다.
//    1) --taxonomy 는 '분류체계가 doc_rule.yaml 안에 있다'고 알린다
//    2) 나머지(규칙·체계를 만들던 옵션)는 '--build-doc-rule 로 만든다'고 알린다
//
// -in: flag = 사용자가 준 인자 그대로(예: "--sync-doc-rule")
//
// -out: Some(문구) = 없어진 옵션이다 / None = 없어진 옵션이 아니다
// -out: error = 예외 없음
//------------------------------------------------------------------
fn removed_option_message(flag: &str) -> Option<String> {
    match flag {
        "--taxonomy" => Some("[MpowerClassify-rs] --taxonomy 는 없어졌습니다 — \
분류체계는 doc_rule.yaml(--build-doc-rule 로 만든 것) 안에 있습니다.".to_string()),
        // 이름만 바뀐 둘. 옛 이름을 조용히 받아 주면 배치가 언제까지나 옛 이름에
        // 머무르고, 두 이름이 같은 자리를 가리키는 상태가 굳는다.
        "--rules" => Some("[MpowerClassify-rs] --rules 는 없어졌습니다 — \
보안등급 규칙셋은 --cso-rules 로 지정합니다(업무분류는 --doc-rules).".to_string()),
        "--save-text" => Some("[MpowerClassify-rs] --save-text 는 없어졌습니다 — \
추출 본문을 남길 폴더는 --textsave 로 지정합니다.".to_string()),
        "--export-taxonomy" | "--scaffold-doc-rule" | "--sync-doc-rule"
        | "--no-fill-blank" | "--sync-enrich" => Some(format!(
            "[MpowerClassify-rs] {} 는 없어졌습니다 — 업무분류 규칙은 --build-doc-rule 로 \
만듭니다(분류체계가 규칙 안에 들어갑니다).", flag)),
        _ => None,
    }
}

fn parse_args() -> Result<Opts, String> {
    let mut o = Opts {
        log: None, verbose: false,
        file: None, dir: None, files_from: None, rules_path: None,
        doc_rules: None, axis: None, conflict: None,
        export_input: None,
        suggest_terms: None, overrides: None, text_dir: None,
        stopwords: None, suggest_json: None, suggest_min_docs: None,
        include_auto_seeds: false,
        glob: None, out: None, json_errors: false, simple_why: false,
        fmt: "json".into(), fmt_explicit: false, simple: false, summary_only: false,
        no_summary: false, hash: false, with_pii: false, with_text: false,
        rule_only: false, vector_only: false, doctype_vector_only: false,
        progress: false, embed_needed: false, no_timing: false,
        build_doc_rule: false, seed_fill_title: false,
        with_vector: false, propagate: None,
        auto_propagate: false, seeds: None,
        seed: seedcli::SeedArgs::default(),
        failsafe: None, check_rules: false,
        filelist: None, no_doc_id: false, report_missing_id: None,
        serve: false, status: false, stop: false, daemon: false, idle_timeout: 0,
        synap_only: false,
        synap_fallback: false,
        synap_path: None,
        write_grade_stream: false, grade_stream_unclassified: None, grade_stream_update_time: false,
        embed_flag: false,
        grade_overrides: None, set_grade: None, reviewer: None, reason: None,
        size_limits: limits::Overrides::default(), no_size_limit: false,
        textsave: None,
        text_only: false,
        decrypt: false, mrk: None, enc_mode: None, zip_mode: None, name: None, dec_size: None,
        no_decrypt: false, decrypt_cmd: None, decrypt_tmp: None, decrypt_timeout: None,
    };
    // args_os() 를 쓴다. std::env::args() 는 인자에 UTF-8 이 아닌 바이트가 섞이면
    // **패닉한다**(리눅스에서 CP949 로 깨진 경로를 받으면 실제로 그렇게 죽었다).
    // 인자로 실제 쓰는 값은 ASCII 플래그와 경로뿐이므로, 깨진 바이트는 U+FFFD 로
    // 바꿔 받고 "그런 경로 없음"이라는 평범한 오류로 흘려보낸다 — 죽는 것보다 낫다.
    let args: Vec<String> = std::env::args_os()
        .skip(1)
        .map(|a| a.to_string_lossy().into_owned())
        .collect();
    let mut i = 0;
    while i < args.len() {
        let a = &args[i];
        // 옵션의 값을 집어 온다. optional=true 면 '값이 없어도 되는' 옵션이다
        // (--failsafe 처럼 단독으로도 쓰는 것). 다음 토큰이 '-' 로 시작하면 그건
        // 값이 아니라 다음 옵션이므로 집어 오지 않는다 — 그렇게 하지 않으면
        // `--failsafe --json-errors` 가 등급 "--json-errors" 로 읽힌다.
        let mut take = |optional: bool| -> Option<String> {
            match args.get(i + 1) {
                // 단 '-' 하나는 옵션이 아니라 '표준입력'이라는 값이다(`--files-from -`).
                // 예전에는 이것까지 옵션으로 보아 `--files-from -` 가 늘 "값이 없습니다"로 죽었다.
                Some(v) if v == "-" || !v.starts_with('-') => {
                    i += 1;
                    Some(v.clone())
                }
                // 값이 빠진 옵션을 빈 문자열로 흘려보내면 "그런 경로 없음"처럼
                // 엉뚱한 오류로 나타나 원인을 못 찾는다. 여기서 끝낸다.
                _ if !optional => errcodes::fail("bad_args",
                    &format!("[MpowerClassify-rs] {} 뒤에 값이 없습니다.", args[i]), None),
                _ => None,
            }
        };
        match a.as_str() {
            "--file" | "-file" => o.file = Some(take(false).unwrap()),
            "--dir" | "-dir" => o.dir = Some(take(false).unwrap()),
            "--files-from" => o.files_from = Some(take(false).unwrap()),
            "--cso-rules" => o.rules_path = Some(take(false).unwrap()),
            // [설계 7단계] 옛 방식(doc_taxonomy.yaml 을 따로 두던 길)의 옵션들은 없앴다.
            // '모르는 인자'(bad_args)로 흘리지 않고 '없어진 옵션'이라고 사실대로 답한다 —
            // 옛 명령줄을 쓰던 사람이 무엇으로 바꿔야 하는지 그 자리에서 알게 하려는 것이다.
            // --taxonomy 는 값을 받던 옵션이지만 값을 집지 않고 바로 끝낸다(값이 빠져도 죽지 않게).
            "--taxonomy" | "--export-taxonomy" | "--scaffold-doc-rule" | "--sync-doc-rule"
            | "--no-fill-blank" | "--sync-enrich" | "--rules" | "--save-text" => {
                let msg = removed_option_message(a).unwrap_or_default();
                errcodes::fail("unsupported_option", &msg, None);
            }
            "--doc-rules" => o.doc_rules = Some(take(false).unwrap()),
            "--axis" => o.axis = Some(take(false).unwrap()),
            "--conflict" => o.conflict = Some(take(false).unwrap()),
            "--glob" => o.glob = Some(take(false).unwrap()),
            "--export-input" => o.export_input = Some(take(false).unwrap()),
            "--out" => o.out = Some(take(false).unwrap()),
            "--format" => { o.fmt = take(false).unwrap(); o.fmt_explicit = true; }
            "--simple" => o.simple = true,
            "--summary" => o.summary_only = true,
            "--nosummary" => o.no_summary = true,
            "--hash" => o.hash = true,
            "--with-pii" => o.with_pii = true,
            // [2026-09-10] 이 인자는 '조용히 무시'하는 목록에 들어 있었다. 파이썬은
            // text 칸을 실어 보내는데 Rust 는 오류도 경고도 없이 안 실었다 — 두 판을
            // 견주려고 이 인자를 준 사람에게는 "Rust 는 본문이 비어 있다"로 보였다.
            // 실제로 이 자리 때문에 추출 격차를 진단할 수단이 하나 없었다.
            "--with-text" => o.with_text = true,
            "--rule-only" => o.rule_only = true,
            "--vector-only" => o.vector_only = true,
            "--doctype-vector-only" => o.doctype_vector_only = true,
            "--progress" => o.progress = true,
            "--embed-needed" => o.embed_needed = true,
            "--no-timing" => o.no_timing = true,
            "--build-doc-rule" => o.build_doc_rule = true,
            "--seed-fill-title" => o.seed_fill_title = true,
            "--suggest-terms" => o.suggest_terms = Some(take(false).unwrap()),
            "--overrides" => o.overrides = Some(take(false).unwrap()),
            "--text-dir" => o.text_dir = Some(take(false).unwrap()),
            "--stopwords" => o.stopwords = Some(take(false).unwrap()),
            "--suggest-json" => o.suggest_json = Some(take(false).unwrap()),
            "--suggest-min-docs" => o.suggest_min_docs = take(false).unwrap().parse().ok(),
            "--include-auto-seeds" => o.include_auto_seeds = true,
            "--with-vector" => o.with_vector = true,
            "--propagate" => o.propagate = Some(take(false).unwrap()),
            "--auto-propagate" => o.auto_propagate = true,
            "--seeds" => o.seeds = Some(take(false).unwrap()),
            // 기준 문서 등록 — --seed-add 만 값을 여러 개 받는다(다음 옵션 전까지).
            // take 는 '-' 로 시작하지 않는 다음 토큰만 집어 오므로, 반복해서
            // 부르면 파일 목록이 자연스럽게 끊긴다.
            "--seed-add" => {
                while let Some(v) = take(true) { o.seed.add.push(v); }
            }
            "--seed-add-from" => o.seed.add_from = Some(take(false).unwrap()),
            "--seed-grade" => o.seed.grade = Some(take(false).unwrap()),
            "--seed-doctype" => o.seed.doctype = Some(take(false).unwrap()),
            "--seed-reviewer" => o.seed.reviewer = Some(take(false).unwrap()),
            "--seed-note" => o.seed.note = take(false).unwrap(),
            "--seed-audit" => o.seed.audit = Some(take(false).unwrap()),
            "--filelist" => o.filelist = Some(take(false).unwrap()),
            "--no-doc-id" => o.no_doc_id = true,
            "--report-missing-id" => o.report_missing_id = Some(take(false).unwrap()),
            // 값이 있으면 그 값, 없으면 S(파이썬 판 nargs="?" const="S" 와 같다).
            // 검증은 인자를 다 읽은 뒤에 한다 — 여기서 하면 --failsafe 가
            // 여러 번 나올 때 마지막 값만 검사하는 것이 어색해진다.
            "--failsafe" => o.failsafe = Some(take(true).unwrap_or_else(|| "S".into())),
            "--check-rules" => o.check_rules = true,
            "--json-errors" => o.json_errors = true,
            // 축약본만 받으면 "이 문서가 왜 C 인가"에 답할 수 없다.
            "--simple-why" => { o.simple_why = true; o.simple = true; }
            // 추출기 선택(handle_extractor_args 가 모드를 정한다).
            //  · --hybridparse    : 기본(자체 파서)과 같다 — 받아들이고 아무 일도 안 한다
            //  · --synap-only     : 모든 문서를 사이냅으로
            //  · --synap-fallback : 자체 파서가 못 읽은 문서만 사이냅으로
            //  · --synap-path     : snf_exe 파일 경로
            "--hybridparse" => {}
            "--synap-only" => o.synap_only = true,
            "--synap-fallback" => o.synap_fallback = true,
            "--synap-path" => o.synap_path = take(false),
            // 등급 스트림 기록(check_grade_stream_args 가 조합을 본다)
            "--write-grade-stream" => o.write_grade_stream = true,
            "--grade-stream-unclassified" => o.grade_stream_unclassified = Some(take(false).unwrap_or_default()),
            "--grade-stream-update-time" => o.grade_stream_update_time = true,
            // 사람 등급 지정(check_override_args 가 조합을 본다)
            "--grade-overrides" => o.grade_overrides = Some(take(false).unwrap_or_default()),
            "--set-grade" => o.set_grade = Some(take(false).unwrap_or_default()),
            "--reviewer" => o.reviewer = Some(take(false).unwrap_or_default()),
            "--reason" => o.reason = Some(take(false).unwrap_or_default()),

            // ── 문서 크기 상한(Size Gate) — 파이썬 판과 같은 이름·같은 뜻 ──
            // 어느 플래그든 0 이하는 '해제'로 읽는다(파이썬 판과 동일 규칙).
            "--max-file-mb" =>
                o.size_limits.max_file_bytes = Some(parse_mb(&take(false).unwrap(), a)),
            "--max-file-mb-text" =>
                o.size_limits.max_file_bytes_text = Some(parse_mb(&take(false).unwrap(), a)),
            "--max-text-chars" =>
                o.size_limits.max_text_chars = Some(parse_u64(&take(false).unwrap(), a)),
            "--parser-timeout" =>
                o.size_limits.parser_timeout = Some(parse_u64(&take(false).unwrap(), a)),
            "--max-pdf-pages" =>
                o.size_limits.max_pdf_pages = Some(parse_u64(&take(false).unwrap(), a)),
            "--no-size-limit" => o.no_size_limit = true,
            // 압축 확장을 이 판에도 포팅해(archive.rs) 두 상한이 실제로 동작한다.
            "--max-archive-mb" =>
                o.size_limits.max_archive_bytes = Some(parse_mb(&take(false).unwrap(), a)),
            "--max-archive-members" =>
                o.size_limits.max_archive_members = Some(parse_u64(&take(false).unwrap(), a)),
            "-h" | "--help" => { usage(); std::process::exit(0); }
            // 이 판은 ko-pii 를 번들하지 않고 '옮겨 적은' 사본이라, 번들 버전 대신
            // '어느 ko-pii 를 보고 옮겼는지'를 찍는다. Python 판의 (ko-pii x.y.z)
            // 와 이 값이 같아야 두 판이 같은 기준으로 도는 것이다.
            "-V" | "--version" => { println!("{}", version_text()); std::process::exit(0); }

            // 임베딩 상주 서버 — 파이썬 판과 같은 이름이다(뜻도 같다).
            "--serve" => o.serve = true,
            "--status" => o.status = true,
            "--stop" => o.stop = true,
            // 이 판은 기본이 '직접 로드'다(파이썬 판은 기본이 데몬). 둘을 같이 주면 나중 것이 이긴다.
            "--daemon" => o.daemon = true,
            "--no-daemon" => o.daemon = false,
            // 서버 유휴 자동 종료(초). 0 이하는 무한 — 파이썬 판과 같다.
            "--idle-timeout" => {
                let v = take(false).unwrap();
                match v.trim().parse::<i64>() {
                    Ok(n) => o.idle_timeout = n.max(0) as u64,
                    Err(_) => errcodes::fail("bad_args",
                        &format!("[MpowerClassify-rs] {} 값이 정수가 아닙니다: {}", a, v), None),
                }
            }
            // 파이썬 판에만 있는 옵션들 — 이 판에서는 할 일이 없다. 그래도 '모르는
            // 인자'로 막으면, 두 판을 같은 명령으로 부르던 호출부가 깨진다.
            // 값이 없는 것들:
            // --embed 는 파이썬 판에서 '벡터만' 모드라, 등급 스트림과 함께면 막으려고 기억해 둔다.
            "--embed" => o.embed_flag = true,
            "--classify"
            | "--per-chunk" | "--normalize" | "--no-normalize"
            | "--timing" | "--recursive" | "-r" => {}
            // 분류 없이 본문만 — 파이썬 판과 같은 이름·같은 출력 모양이다.
            "--text-only" => o.text_only = true,
            // 화면에도 로그를 내라는 뜻. 예전에는 그냥 삼켰다.
            "--verbose" | "-v" => o.verbose = true,
            // 로그 파일 경로. 미지정이면 <exe폴더>/log/class_YYYYMMDD.log.
            "--log" => o.log = take(false),
            // ※ "--nosummary"·"--with-text" 는 실제로 처리하므로 여기 두면 안 된다
            //   (도달할 수 없는 갈래가 되어 unreachable_patterns 경고가 난다).
            // 임베딩 모델 고르기 — 파이썬 판과 같은 세 가지(e5-small-ko · e5-small · ko-sroberta).
            // [2026-09-29] 예전에는 값을 삼키고 늘 e5-small-ko 를 썼다 — `--model ko-sroberta` 를
            // 줘도 조용히 다른 모델로 벡터를 만들었다. 이제 고른 모델을 실제로 쓰고,
            // 모르는 이름은 파이썬 판과 같은 문구·같은 번호(1003)로 멈춘다.
            "--model" => {
                let v = take(false).unwrap();
                if !embed::select(&v) {
                    errcodes::fail("bad_args",
                        &format!("[MpowerClassify-rs] 알 수 없는 모델 '{}'. 사용 가능: {}",
                                 v, embed::model_keys()), None);
                }
            }
            "--max-tokens" | "--overlap" => {
                let v = take(false).unwrap();
                let fixed = if a.as_str() == "--max-tokens" { embed::CHUNK_MAX_TOKENS } else { embed::CHUNK_OVERLAP };
                match v.trim().parse::<i64>() {
                    Err(_) => errcodes::fail("bad_args",
                        &format!("[MpowerClassify-rs] {} 값이 정수가 아닙니다: {}", a, v), None),
                    // 청크를 바꾸면 벡터가 바뀐다 — 이 판은 고정값이라 다른 값은 받을 수 없다.
                    Ok(n) if n != fixed as i64 => errcodes::fail("unsupported_option",
                        &format!("[MpowerClassify-rs] {} {} 은 이 판에 없습니다 — 이 판은 {} 로 고정입니다(값이 다르면 벡터가 파이썬 판과 달라집니다).",
                                 a, v, fixed), None),
                    Ok(_) => {}
                }
            }
            "--precision" => {
                let v = take(false).unwrap();
                match v.as_str() {
                    "f32" => {}
                    // f16 은 벡터를 반올림해 내보내는 파이썬 판 기능이다 — 이 판은 늘 f32 다.
                    "f16" => errcodes::fail("unsupported_option",
                        "[MpowerClassify-rs] --precision f16 은 이 판에 없습니다 — 이 판은 벡터를 늘 f32 로 냅니다.", None),
                    _ => errcodes::fail("bad_args",
                        &format!("[MpowerClassify-rs] --precision 은 f32 또는 f16 입니다: {}", v), None),
                }
            }
            // 결과와 무관한 옵션(스레드 수·파이썬 판 씨앗 설정) — 값은 쓰지 않지만
            // 형식은 본다. 오타가 조용히 통과하면 파이썬 판으로 옮겼을 때 처음 터진다.
            "--num-threads" | "--seed-per-dir" | "--seed-per-node" => {
                let v = take(false).unwrap();
                if v.trim().parse::<i64>().is_err() {
                    errcodes::fail("bad_args",
                        &format!("[MpowerClassify-rs] {} 값이 정수가 아닙니다: {}", a, v), None);
                }
            }
            // 이 판에는 업무분류 씨앗 자동 생성이 없다. 예전에는 값까지 삼키고
            // 아무 일도 안 했다 — 오류도 경고도 없이 class_seed.jsonl 이 그대로라,
            // "왜 씨앗이 안 생기지"를 아무도 알 수 없었다. 조용한 실패를 없앤다.
            "--make-doctype-seeds" => {
                take(false);
                // 인자가 틀린 것이 아니라 '이 판에 없는 기능'이다 — 1003 이 아니라 1012.
                errcodes::fail("unsupported_option",
                    &["[MpowerClassify-rs] --make-doctype-seeds 는 이 판(Rust)에 없습니다.",
                      "                 파이썬 판(MpowerClassify)으로 만드세요 — 만든 파일은 이 판도 그대로 읽습니다.",
                      "                 문서 하나씩 등록하려면 --seed-add 를 쓰세요(두 판 모두 있습니다)."]
                        .join("
"),
                    None);
            }
            // 추출 본문 보존(설계 1장). 옛 이름 --save-text 는 없앴다 —
            // 위 '없어진 옵션' 자리에서 새 이름을 알려 준다.
            "--textsave" => { o.textsave = take(false); }

            // 엠파워 v11 암호화 문서 복호화(설계 §4·§5).
            "--decrypt" => o.decrypt = true,
            "--no-decrypt" => o.no_decrypt = true,
            "--mrk" => o.mrk = take(false),
            "--enc-mode" => o.enc_mode = take(false),
            "--zip-mode" => o.zip_mode = take(false),
            "--name" => o.name = take(false),
            "--size" => o.dec_size = take(false),
            "--decrypt-cmd" => o.decrypt_cmd = take(false),
            "--decrypt-tmp" => o.decrypt_tmp = take(false),
            "--decrypt-timeout" =>
                o.decrypt_timeout = Some(parse_u64(&take(false).unwrap(), "--decrypt-timeout")),

            // 여기까지 안 걸렸으면 정말 모르는 인자다. 조용히 넘어가면 오타 하나가
            // 옵션을 통째로 무효로 만든다(예: --json-erros). 파이썬 판도 여기서
            // 막으므로, 그 자리에서 끝내는 것이 두 판이 같아지는 길이다.
            _ => errcodes::fail("bad_args",
                &format!("[MpowerClassify-rs] 알 수 없는 인자: {}\n\
                          철자를 확인하세요. 쓸 수 있는 인자는 --help 로 볼 수 있습니다.", a),
                None),
        }
        i += 1;
    }
    // --axis 는 오타가 조용히 "축 전체 무시"로 이어지면 안 되므로 여기서 막는다.
    if let Some(a) = &o.axis {
        if a != "security" && a != "doctype" {
            // 축 오타는 조용히 '축 전체 무시'로 이어지면 안 된다. 계약상 1004.
            errcodes::fail("bad_axis",
                &format!("[MpowerClassify-rs] --axis 는 security 또는 doctype 이어야 합니다: {:?}", a),
                None);
        }
    }
    // --failsafe 는 규칙셋을 거치지 않고 곧바로 최종 등급이 된다. 오타가 있으면
    // 정의되지 않은 등급이 그대로 결과에 박히므로(규칙셋 오타와 같은 종류의
    // fail-open) 스캔 시작 전에 막는다. 파이썬 판과 같은 자리·같은 코드(1005).
    if let Some(g) = &o.failsafe {
        if !rules::GRADES.contains(&g.as_str()) {
            errcodes::fail("bad_failsafe",
                &format!("[MpowerClassify-rs] --failsafe 값이 올바르지 않습니다: {:?}\n\
                          정의된 등급: {}", g, rules::GRADES.join(" < ")),
                None);
        }
    }
    // --format 을 안 줬으면 --out 확장자로 형식을 정한다.
    if !o.fmt_explicit {
        if let Some(g) = guess_format(&o.out) {
            if g != o.fmt {
                note!("[MpowerClassify-rs] --out 확장자에 맞춰 --format {} 로 저장합니다. \
                           (다르게 하려면 --format 을 직접 지정하세요)", g);
            }
            o.fmt = g;
        }
    }
    Ok(o)
}

/// 전체 레코드를 '읽는 차례'로 다시 담는다(파이썬 판 `_order_record()` 와 같은 표).
///
/// 만들어진 순서 그대로 내보내면 눈에 안 들어온다 — 가장 궁금한 업무분류가
/// `labels` 안에 묻혀 열 번째에 있고, 판정 근거(`signals`)가 결과보다 먼저 나온다.
///   1. 무엇을      `file` · `hash`
///   2. 어떻게 됐나  `grade` · `doctype` · `confidence` · `method` · `decided_by` · `error`
///   3. 왜          `labels`(축별 상세) · `signals`(근거)
///   4. 부속        `seed_eligible` · 버전 3개 · `ts` · `elapsed_ms`
///
/// 앞 다섯 칸이 `--simple` 과 같아, 축약본이 전체의 '앞부분만 떼어낸 것'이 된다.
/// `doctype` 은 `labels.doctype.values` 에서 뽑은 같은 값이라 새 정보가 아니고,
/// 축이 돌았을 때만 넣는다(빈 배열이면 "분류 못 함"과 "축 안 씀"이 안 갈린다).
/// `doc_id` 도 축이 돌았을 때만 넣는다(`--no-doc-id` 면 키가 없다). 다만 축약본은
/// 폴백 값을 `null` 로 바꿔 싣는다 — 차례는 같고 값만 좁힌다(위 `--simple` 참고).
/// 표에 없는 칸은 **뒤에 그대로 붙인다** — 새 칸이 생겼을 때 이 함수가 조용히
/// 지워 버리면 가장 찾기 어려운 사고가 된다.
// [2026-09-10] labels 껍데기를 없애고 축을 최상위로 올렸다. 예전에는 같은 값이
// 최상위(grade·confidence·method·decided_by)와 labels.security 양쪽에 두 벌
// 있었고, 최상위 doctype(dc_id 목록)은 labels.doctype.values 에서 뽑은 세 번째
// 사본이었다. 이제 축마다 한 벌씩만 있다(파이썬 판 _REC_ORDER 와 같은 표).
// [2026-09-10 오후] 판정을 맨 앞으로 올렸다. 파일을 열면 가장 먼저 보고 싶은
// 것은 "이 문서가 무엇이고(①) 어떻게 판정됐나(②)" 이지 그 판정의 근거가 아니다.
//   ① 무엇을      file · hash · doc_id · doc_id_source
//   ② 어떻게 됐나  grade · doctype(dc_id 목록) · error · excluded(처리제외)
//   ③ 왜          why(축별 상세) · pii · vector
//   ④ 장부        meta · elapsed_ms
const REC_ORDER: &[&str] = &[
    // name 은 엠파워 v11 의 원래 파일 이름 — 목록·명령줄이 줬을 때만 있다.
    // 신원 세 칸(file · hash · doc_id)이 맨 앞이라는 약속은 그대로 두고 그 뒤에 넣는다.
    "file", "hash", "doc_id", "doc_id_source", "rematched_by", "name",
    "grade", "doctype", "error", "excluded", "why", "pii", "vector",
    // --textsave 표식은 판정값이 아니라 부속이다(파이썬 판과 같은 자리).
    "text_saved", "text_save_error",
    // --write-grade-stream 결과(켠 실행에서만 있다) — 파이썬 판과 같은 자리.
    "grade_stream",
    "meta", "elapsed_ms",
];

//------------------------------------------------------------------
// 레코드 생성 시각 — 파이썬 판과 같은 모양의 ISO-8601 문자열
//=> 파이썬 판은 datetime.now().astimezone().isoformat(timespec="seconds") 로
//   "2026-09-07T13:46:14+09:00" 처럼 로컬 타임존 오프셋이 붙은 초 단위 값을 낸다.
//   두 판의 레코드를 그대로 대조할 수 있어야 하므로 같은 모양으로 맞춘다.
//
//   [왜 UTC 가 아니라 로컬인가] 이 값은 사람이 "언제 분류했나"를 보는 용도이고,
//   오프셋(+09:00)이 붙어 있어 시점 비교에도 애매함이 없다. 파이썬 판이 이미
//   그렇게 내보내고 있어서, 여기서 UTC 로 바꾸면 두 판이 갈린다.
//
// -in: 없음
//
// -out: String = 예) "2026-09-07 13:46:14"
// -out: error = 없음
//------------------------------------------------------------------
fn now_iso() -> String {
    // 2026-09-10: ISO-8601(…T13:46:14+09:00)에서 사람이 읽는 모양으로 바꿨다.
    // 이 값을 파싱하는 코드가 두 판 어디에도 없고(보여 주기·문자열 정렬 전용),
    // 문자열로 정렬해도 시간 순서가 그대로 유지된다.
    // 파이썬 판 seedstore.TS_FMT 와 반드시 같은 모양이어야 한다 — 한쪽만 바꾸면
    // 같은 파일에 두 표기가 섞여 사람이 정렬해 볼 수 없다.
    chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string()
}

//------------------------------------------------------------------
// 압축파일 '자체'의 집계 등급 레코드 (파이썬 _archive_record 포팅)
//=> 내부 파일들의 등급을 모아 압축파일 자체에 '최고 위험 등급'을 매긴다.
//   압축만 봐도 위험도를 알 수 있어야, 내부 100건을 일일이 안 봐도 판단이 선다.
//   contains 에 등급 분포를 담아 어느 위험이 몇 건인지 보이게 한다.
//
//   [최고 위험을 쓰는 이유] 평균이나 다수결을 쓰면 C 문서 1건이 O 문서 99건에
//   묻힌다. 거버넌스에서 그 1건이 정확히 문제의 그 1건이다.
//
// -in: origin   = 최상위 압축파일 경로
// -in: grades   = 그 압축에 속한 내부 파일들의 최종 등급 목록
// -in: doctypes = 그 압축의 업무분류 분포 {dc_id: 건수}(축을 안 썼으면 None)
//
// -out: Value = 집계 레코드(archive=true, method="archive_rollup")
// -out: error = 없음
//------------------------------------------------------------------
//------------------------------------------------------------------
// 목록에 있으나 디스크에 없는 문서의 레코드
//=> --filelist 가 대상을 정한 실행에서, 목록에 적힌 문서가 실제로 없을 때 만든다.
//   예전에는 이런 문서가 결과에서 통째로 사라졌다(stderr 의 F4 경고가 전부였다).
//   파이썬 판 _missing_file_record 와 같은 칸·같은 값을 낸다.
//
//   [왜 규칙을 안 돌리나] 파일이 없으니 본문도 파일명 신호도 '확인된 것'이 아니다.
//   없는 문서에 등급을 매기면 사람이 확인할 기회를 잃는다.
//
// -in: path    = 목록에 적힌 문서 경로(원본 표기 그대로)
// -in: sfid    = 목록이 준 sfile_id
// -in: rule_ver = 규칙셋 판(rule_version 칸에 적는다)
//
// -out: Value = 결과 레코드(error.kind="file_missing")
//------------------------------------------------------------------
fn missing_file_record(path: &str, sfid: &str, rule_ver: &str) -> Value {
    json!({
        "file": path,
        "doc_id": sfid,
        "doc_id_source": "sfile_id",
        "grade": Value::Null,
        "why": {"security": {
            "confidence": 0.0, "method": "file_missing",
            "decided_by": [], "seed_eligible": false, "signals": {}
        }},
        "error": {
            "stage": "input",
            "kind": "file_missing",
            "reason": "목록(--filelist)에 있으나 파일이 없습니다"
        },
        "meta": {"ts": now_iso()}
    })
}

fn archive_record(
    origin: &str,
    grades: &[Option<Grade>],
    doctypes: Option<&std::collections::BTreeMap<String, u32>>,
) -> Value {
    let (mut nc, mut ns, mut no, mut nn) = (0u32, 0u32, 0u32, 0u32);
    // 심각도 순위로 최고 위험을 고른다(C > S > O > 미분류).
    let mut worst: Option<Grade> = None;
    for g in grades {
        match g {
            Some(Grade::C) => nc += 1,
            Some(Grade::S) => ns += 1,
            Some(Grade::O) => no += 1,
            None => nn += 1,
        }
        let sev = |x: &Option<Grade>| match x {
            Some(Grade::C) => 3,
            Some(Grade::S) => 2,
            Some(Grade::O) => 1,
            None => 0,
        };
        if sev(g) > sev(&worst) {
            worst = *g;
        }
    }
    let mut rec = json!({
        "file": origin,
        "archive": true,                 // 이 레코드는 '압축파일 자체'의 집계임을 표시
        "grade": worst.map(|g| g.as_str()),
        "why": {"security": {"method": "archive_rollup", "decided_by": ["archive"]}},
        "contains": {"total": grades.len(), "C": nc, "S": ns, "O": no, "unclassified": nn},
        // 파이썬 판과 같은 사람이 읽는 초 단위 시각.
        "meta": {"ts": now_iso()},
    });
    // 업무분류는 서열이 없어 대표 하나를 못 고른다 — 분포를 그대로 싣는다.
    // 축이 안 돌았으면 키 자체를 넣지 않는다('축 미사용'과 '분류 못 함'의 구분).
    if let Some(d) = doctypes {
        if !d.is_empty() {
            let m: serde_json::Map<String, Value> =
                d.iter().map(|(k, v)| (k.clone(), json!(v))).collect();
            rec["contains_doctype"] = Value::Object(m);
        }
    }
    rec
}

/// doctype 축 안쪽 칸 차례. 2026-09-10 부터 truncated·conflicts 는 값이 있을 때만,
/// strategy 는 --conflict 로 덮어썼을 때만 실린다 — 즉 '언제 붙느냐'가 경로마다
/// 달라졌다. 붙는 자리를 표로 고정하지 않으면 같은 문서인데도 두 판(또는 1패스와
/// 2패스 전파)이 칸 차례가 다른 파일을 내고, 결과를 나란히 견줄 수 없게 된다.
/// 파이썬 판 `_DOCTYPE_ORDER` 와 같은 표다.
const DOCTYPE_ORDER: &[&str] = &["values", "strategy", "truncated", "conflicts", "embed"];

/// doctype 축 안쪽 칸을 표 차례로 다시 담는다. 표에 없는 칸은 잃지 않고 뒤에 붙인다.
fn order_doctype(dt: &Value) -> Value {
    let src = match dt.as_object() {
        Some(o) => o,
        None => return dt.clone(),
    };
    let mut out = serde_json::Map::new();
    for key in DOCTYPE_ORDER {
        if let Some(v) = src.get(*key) {
            out.insert((*key).to_string(), v.clone());
        }
    }
    for (k, v) in src.iter() {
        if !out.contains_key(k) {
            out.insert(k.clone(), v.clone());
        }
    }
    Value::Object(out)
}

fn order_record(rec: &Value) -> Value {
    let src = match rec.as_object() {
        Some(o) => o,
        None => return rec.clone(),
    };
    let mut out = serde_json::Map::new();
    for key in REC_ORDER {
        if *key == "doctype" {
            // 맨 앞의 doctype 은 dc_id 목록이다 — why.doctype.values 에서 지금
            // 뽑는다. 전파가 후보를 더할 수 있어 미리 적어 두면 어긋난다.
            // 축이 안 돌았으면 키 자체를 만들지 않는다(--simple 과 같은 규약).
            if record::doctype(rec).is_some() {
                out.insert("doctype".into(), Value::Array(record::doctype_ids(rec)));
            }
            continue;
        }
        if *key == "why" {
            if let Some(w) = src.get("why").and_then(|w| w.as_object()) {
                let mut nw = w.clone();
                if let Some(d) = w.get("doctype") {
                    nw.insert("doctype".into(), order_doctype(d));
                }
                out.insert("why".into(), Value::Object(nw));
            }
            continue;
        }
        if let Some(v) = src.get(*key) {
            out.insert((*key).to_string(), v.clone());
        }
    }
    // 표에 없는 칸은 잃지 않고 뒤에 붙인다.
    for (k, v) in src {
        if !out.contains_key(k) {
            out.insert(k.clone(), v.clone());
        }
    }
    Value::Object(out)
}

/// 감사용 전체 결과 파일 경로 — `result.jsonl` → `result.full.jsonl`.
///
/// 화면(UI)이 축약본을 `<이름>.simple.<확장자>` 로 부르는 것과 같은 결의 이름이다.
fn full_out_path(out: &str) -> String {
    let p = Path::new(out);
    let ext = p.extension().map(|e| e.to_string_lossy().into_owned())
        .unwrap_or_else(|| "jsonl".into());
    let stem = p.with_extension("");
    format!("{}.full.{}", stem.display(), ext)
}

/// 판정 근거 요약을 만든다(`--simple-why`).
///
/// 축약본 4칸으로는 "왜 이 등급인가"를 댈 수 없다. 그렇다고 전체 레코드를 주면
/// 26배로 커진다. 판정에 실제로 쓰인 것만 추린다 — 전체의 15% 쯤이다.
/// 레코드 자신의 `labels.security` / `labels.doctype` 갈래를 그대로 따라 **축별로
/// 묶는다**. 평평하게 늘어놓으면 보안등급의 `conf` 를 업무분류의 값으로 오해한다
/// (게다가 이 판은 키를 사전순으로 내보내 `dt` 가 `conf` 와 `hits` 사이에 끼었다).
///   * `security.by`   어느 신호가 등급을 정했는가
///   * `security.conf` 그 판정의 확신     * `security.hits` 걸린 규칙 id 와 건수
///   * `doctype`       업무분류 후보의 dc_id·확신과 **판정 경로(`by`)**
///
/// `doctype.by` 는 같은 0.91 이라도 뜻이 다르기 때문에 넣는다 —
/// `rule` 은 "정한 낱말이 제목·머리·파일명 여러 군데서 나왔다", `embed` 는
/// "이미 분류해 둔 기준 문서와 닮았다". 검토자가 다르게 봐야 하는 값이다.
///
/// 매칭된 **원문 값은 넣지 않는다** — 이 도구의 불변식이다(주민번호 12건 검출,
/// 이지 그 번호 자체는 남기지 않는다). 파이썬 판 `_why_record()` 와 같은 칸이다.
fn why_record(rec: &Value) -> Value {
    let mut hits: Vec<Value> = vec![];
    if let Some(sigs) = record::signals(rec).and_then(|v| v.as_object()) {
        for (sig, val) in sigs {
            if val.get("grade").map_or(true, |g| g.is_null()) {
                continue;
            }
            match val.get("hits").and_then(|h| h.as_array()) {
                Some(list) if !list.is_empty() => {
                    for h in list {
                        hits.push(json!({"sig": sig, "id": h.get("id"), "n": h.get("count")}));
                    }
                }
                // path 처럼 '걸린 규칙 목록'이 없는 신호는 출처만 남긴다.
                _ => hits.push(json!({"sig": sig, "src": val.get("source")})),
            }
        }
    }
    let mut sec = serde_json::Map::new();
    sec.insert("by".into(), record::decided_by(rec).cloned().unwrap_or(json!([])));
    sec.insert("conf".into(), record::security(rec).and_then(|s| s.get("confidence"))
        .or_else(|| rec.get("confidence")).cloned().unwrap_or(json!(0.0)));
    sec.insert("hits".into(), Value::Array(hits));
    let mut why = serde_json::Map::new();
    why.insert("security".into(), Value::Object(sec));
    if let Some(vals) = record::doctype(rec)
        .and_then(|d| d.get("values")).and_then(|v| v.as_array()) {
        let dt: Vec<Value> = vals.iter().filter_map(|v| v.get("dc_id").map(|id| json!({
            "dc": id,
            "c": (v.get("confidence").and_then(|c| c.as_f64()).unwrap_or(0.0) * 100.0).round() / 100.0,
            // stage 는 이 후보가 규칙 스캔에서 나왔는지 기준 문서 비교에서
            // 나왔는지 말해 준다 — 같은 숫자라도 뜻이 달라 함께 낸다.
            "by": v.get("stage").and_then(|s| s.as_str()).unwrap_or_else(|| {
                let embed = v.get("from").and_then(|f| f.as_array())
                    .map_or(false, |a| a.iter().any(|x| x.as_str() == Some("embed")));
                if embed { "embed" } else { "rule" }
            }),
        }))).collect();
        if !dt.is_empty() {
            why.insert("doctype".into(), Value::Array(dt));
        }
    }
    Value::Object(why)
}

/// `--out` 파일 이름의 확장자로 출력 형식을 추측한다.
///
/// "`--out result.jsonl` 로 저장했는데 안에는 JSON 배열이라 뷰어가 못 연다"는 함정을
/// 없애려는 것이다 — 파일 이름이 사실상 형식을 말하고 있으므로 그대로 따른다.
/// 모르는 확장자면 None 을 돌려 기본값(json)을 그대로 쓰게 둔다.
/// 파이썬 판 `resolve_format()` 과 같은 표를 쓴다.
fn guess_format(out: &Option<String>) -> Option<String> {
    let p = out.as_ref()?;
    let ext = Path::new(p).extension()?.to_string_lossy().to_lowercase();
    match ext.as_str() {
        "jsonl" | "ndjson" => Some("jsonl".to_string()),
        "json" => Some("json".to_string()),
        _ => None,   // txt 등은 Rust 판에 text 출력이 없으므로 건드리지 않는다
    }
}

/// 정책 파일 경로 결정: 명시 인자 → CSOCLASSIFY_POLICY_DIR → exe 옆.
/// 명시 인자는 파일이 없어도 그대로 돌려준다 — "지정했는데 없다"는 조용히
/// 다른 파일로 대체되면 안 되고, 로더가 그 경로로 실패해 알려 줘야 하기 때문이다.
fn resolve_policy_file(explicit: &Option<String>, filename: &str) -> Option<PathBuf> {
    if let Some(p) = explicit {
        return Some(PathBuf::from(p));
    }
    // 환경변수가 exe 옆보다 먼저다. 환경변수는 관리자가 "이 정책을 써라"고 **직접
    // 지시한 것**이고, exe 옆 파일은 그냥 거기 있을 뿐이다. 지시가 우선해야 한다.
    // [2026-09-01 순서 교정] 예전에는 exe 옆이 먼저였다. 파이썬 판은 환경변수가
    // 먼저였으므로, 두 조건이 함께 성립하면 **두 판이 서로 다른 규칙셋을 읽었다**.
    // 실측에서 15건 중 11건의 등급이 갈렸다 — 그것도 조용히. 정책이 바뀐 줄
    // 모르는 채로 등급이 달라지는 것은 거버넌스 도구에서 가장 나쁜 실패다.
    if let Ok(d) = std::env::var("CSOCLASSIFY_POLICY_DIR") {
        let p = Path::new(&d).join(filename);
        if p.is_file() { return Some(p); }
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let p = dir.join(filename);
            if p.is_file() { return Some(p); }
        }
    }
    None
}

/// 보안등급 규칙셋 파일 이름. 업무분류(doc_rule.yaml)와 짝을 맞춰 단수형으로
/// 바꿨다(2026-09-08).
const RULES_NAME: &str = "cso_rule.yaml";
/// 옛 이름. 이미 배포된 폴더에는 이 이름 파일이 그대로 있고, 그것 때문에
/// "규칙셋을 찾을 수 없습니다"로 죽으면 안 되므로 당분간 함께 받는다.
const RULES_NAME_OLD: &str = "cso_rules.yaml";

//------------------------------------------------------------------
// 규칙셋 경로 결정 — 새 이름 우선, 없으면 옛 이름
//=> --cso-rules → CSOCLASSIFY_POLICY_DIR → exe 옆 차례는 그대로다(파이썬 판과 같다).
//   각 폴더 안에서 새 이름을 먼저 보고, 없을 때만 옛 이름을 쓴다. 옛 이름을
//   썼으면 한 줄 알린다 — 조용히 쓰면 "언제까지 이대로 두어도 되나"를 아무도
//   모르고, 어느 날 지원이 끊길 때 갑자기 멈춘다.
//
// -in: opts = 실행 옵션(--cso-rules 를 줬으면 그 경로가 그대로 이긴다)
//
// -out: Option<PathBuf> = 찾은 규칙셋 경로(둘 다 없으면 None)
// -out: error = 없음
//------------------------------------------------------------------
fn resolve_rules(opts: &Opts) -> Option<PathBuf> {
    if let Some(p) = resolve_policy_file(&opts.rules_path, RULES_NAME) {
        return Some(p);
    }
    // --cso-rules 를 준 경우에는 그 경로가 이미 위에서 반환됐다. 여기 온다면
    // 자동 탐색이었으므로, 같은 차례로 옛 이름을 한 번 더 찾는다.
    if opts.rules_path.is_none() {
        if let Some(p) = resolve_policy_file(&None, RULES_NAME_OLD) {
            note!("[MpowerClassify-rs] {} 은 옛 이름입니다 — {} 로 바꿔 두세요(지금은 그대로 씁니다).", RULES_NAME_OLD, RULES_NAME);
            return Some(p);
        }
    }
    None
}

/// 규칙 파일이 어떤 모양인가 — 업무분류 축을 켤지·막을지를 가르는 기준.
#[derive(Debug, PartialEq)]
enum RuleFileKind {
    /// --build-doc-rule 로 만든 파일(generated 칸이 있음). 분류체계가 규칙 줄 안에 있다.
    Generated,
    /// 옛 모양(분류체계 doc_taxonomy.yaml 을 따로 읽던 방식). 이 판부터는 읽지 않는다.
    Legacy,
    /// 못 읽거나 YAML 이 깨졌다 — 모양을 판단할 수 없으니 로더가 그 까닭을 알린다.
    Unreadable,
}

//------------------------------------------------------------------
// 규칙 파일 모양 가리기 (자동 생성본 / 옛 모양 / 못 읽음)
//=> 설계 7단계부터는 --build-doc-rule 로 만든 규칙만 읽는다. 옛 모양 파일은 분류체계를
//   따로(doc_taxonomy.yaml) 읽어야 하는데 그 길을 없앴으므로, 읽기 전에 모양부터 본다.
//    1) 파일을 못 읽거나 YAML 이 깨졌으면 Unreadable — '옛 모양'이라고 잘못 알리지 않게
//    2) generated 칸이 있으면 Generated, 없으면 Legacy
//
// -in: path = doc_rule.yaml 경로
//
// -out: RuleFileKind = 모양
// -out: error = 예외 없음(읽기 실패도 Unreadable 로 돌려준다)
//------------------------------------------------------------------
fn rule_file_kind(path: &Path) -> RuleFileKind {
    let text = match std::fs::read_to_string(path) { Ok(t) => t, Err(_) => return RuleFileKind::Unreadable };
    let yv = match serde_yaml::from_str::<serde_yaml::Value>(&text) {
        Ok(v) => v,
        Err(_) => return RuleFileKind::Unreadable,
    };
    match serde_json::to_value(&yv) {
        Ok(j) if docbuild::is_generated(&j) => RuleFileKind::Generated,
        Ok(_) => RuleFileKind::Legacy,
        Err(_) => RuleFileKind::Unreadable,
    }
}

//------------------------------------------------------------------
// 업무분류(doctype) 축 로드 — 있으면 켜고 없으면 끄는 외장 자산
//=> 분류체계는 --build-doc-rule 로 만든 doc_rule.yaml 안에 들어 있다(설계 7단계 —
//   doc_taxonomy.yaml 을 따로 읽던 옛 길과 'seed 전파 전용' 모드는 없앴다).
//    1) --axis security 면 시도조차 하지 않는다(이번 실행이 안 쓰는 축이라 검증할 이유가 없다)
//    2) 규칙 파일이 없으면: --axis doctype 명시 시 종료(taxonomy_missing, 3),
//       아니면 알리고 축을 끈다(보안등급만)
//    3) 옛 모양 규칙 파일이면 항상 종료(doc_rules_invalid, 4) — 조용히 축을 끄면
//       "업무분류가 왜 비었지"를 아무도 모른다. 새 방식으로 다시 만들라고 알린다
//    4) 자동 생성본이면 읽는다. 내용이 깨졌으면 종료(4), 경고(입력이 바뀜 등)는 보여 준다
//
// -in: opts = 실행 옵션(axis · doc_rules 를 본다)
//
// -out: Ok(None) = 축을 끄고 security 만 / Ok(Some(..)) = 축 켜짐
// -out: error = 규칙 파일 없음(--axis doctype)·옛 모양·내용 오류면 errcodes::fail 로 종료
//               (Err(code) 는 지금은 쓰지 않지만 부르는 쪽 계약으로 남겨 둔다)
//------------------------------------------------------------------
fn load_doctype_axis(opts: &Opts) -> Result<Option<(Taxonomy, DocRuleSet)>, i32> {
    if opts.axis.as_deref() == Some("security") {
        return Ok(None);
    }
    let explicit = opts.axis.as_deref() == Some("doctype");

    let rpath = match resolve_policy_file(&opts.doc_rules, doc_rules::default_doc_rule_filename()) {
        Some(p) if p.is_file() => p,
        other => {
            // 경로가 잡혔으면(=명시했는데 파일이 없음) 그 경로를, 아무 데도 없으면 파일
            // 이름을 보여 준다 — 없는 경로를 지어내 보여주지 않는다.
            let shown = other.map(|p| p.display().to_string())
                .unwrap_or_else(|| doc_rules::default_doc_rule_filename().into());
            if explicit {
                // '파일 없음'은 내용 오류(4)가 아니라 부른 쪽이 고칠 문제(3)다.
                errcodes::fail("taxonomy_missing",
                    &format!("[MpowerClassify-rs] 업무분류 규칙 파일(doc_rule.yaml)이 없습니다 — \
분류체계가 그 안에 있습니다. --build-doc-rule 로 만드세요. 찾은 경로: {}", shown),
                    Some(&shown));
            }
            note!("[MpowerClassify-rs] doc_rule.yaml 이 없어 업무분류(doctype) 축을 건너뜁니다. \
규칙은 --build-doc-rule 로 만드세요.");
            return Ok(None);
        }
    };

    // 옛 모양 파일은 읽기 전에 막는다. 깨진 파일(Unreadable)은 아래 로더가 까닭을 알린다.
    if rule_file_kind(&rpath) == RuleFileKind::Legacy {
        errcodes::fail("doc_rules_invalid",
            &format!("[MpowerClassify-rs] [G0] 옛 모양 규칙 파일입니다(분류체계 doc_taxonomy.yaml 을 \
따로 읽던 방식): {}\n  이 판부터는 --build-doc-rule 로 만든 규칙만 읽습니다. \
화면의 [새 방식으로 바꾸기] 또는 --build-doc-rule 로 다시 만드세요.", rpath.display()),
            rpath.to_str());
    }

    let mut drs = match doc_rules::load_doc_rules(&rpath, None) {
        Ok(d) => d,
        Err(e) => errcodes::fail("doc_rules_invalid",
            &format!("[MpowerClassify-rs] {}", e), rpath.to_str()),
    };
    for w in &drs.warnings {
        note!("[MpowerClassify-rs] {}", w);
    }
    // 자동 생성본은 로더가 규칙 줄에서 분류체계를 지어 싣는다. 없으면 모양이 어긋난 것이다.
    let tax = match drs.taxonomy.take() {
        Some(t) => t,
        None => errcodes::fail("doc_rules_invalid",
            &format!("[MpowerClassify-rs] 규칙 파일에서 분류체계를 짓지 못했습니다: {}", rpath.display()),
            rpath.to_str()),
    };
    Ok(Some((tax, drs)))
}

/// 후보 한 줄을 사람이 읽는 글로 — 파이썬 cli.py::_suggest_line 과 글자까지 같아야 한다.
fn suggest_line(c: &termsuggest::Cand) -> String {
    let mark = if c.checked { "[v]" } else { "[ ]" };
    // 칸 이름은 화면 말로 바꾼다(개발 용어를 안 보여 준다).
    fn name(f: &str) -> &str {
        match f {
            "title_terms" => "제목", "head_terms" => "앞부분",
            "terms" => "본문", "filename" => "파일이름", other => other,
        }
    }
    let mut wheres: String = c.fields.iter().map(|f| name(f))
        .collect::<Vec<&str>>().join("·");
    if let Some(n) = c.min_count { wheres.push_str(&format!("(같은 말 {}회 이상)", n)); }
    let flags: String = c.flags.iter().map(|f| format!("<{}>", f))
        .collect::<Vec<String>>().join(" ");
    format!("  {} {:<14} {:<22} 근거 {:>3}건/폴더{:<2} 다른분류 {:>5.1}% 덮는율 {:>5.1}%  {}",
            mark, c.term, wheres, c.docs, c.clusters,
            c.df_neg * 100.0, c.df_pos * 100.0, flags)
}

/// 문턱을 못 넘은 말 한 줄 — 후보가 하나도 없을 때만 나온다.
/// 파이썬 cli.py::_below_line 과 글자까지 같아야 한다.
fn below_line(c: &termsuggest::Below) -> String {
    let where_ = match c.where_ {
        "name" => "파일이름", "title" => "제목", "head" => "앞부분",
        "body" => "본문", other => other,
    };
    let flags: String = c.flags.iter().map(|f| format!("<{}>", f))
        .collect::<Vec<String>>().join(" ");
    format!("  [-] {:<14} {:<8} 근거 {:>3}건/폴더{:<2} 다른분류 {:>5.1}% 덮는율 {:>5.1}%  {} {}",
            c.term, where_, c.docs, c.clusters, c.df_neg * 100.0, c.df_pos * 100.0,
            c.why, flags)
}

/// 문턱을 못 넘은 말 한 개를 json 으로.
fn below_json(c: &termsuggest::Below) -> Value {
    json!({
        "term": c.term, "where": c.where_, "why": c.why,
        "df_pos": c.df_pos, "df_neg": c.df_neg, "score": c.score,
        "docs": c.docs, "clusters": c.clusters, "flags": c.flags,
    })
}

/// 후보 한 개를 json 으로 — 파이썬 판이 --suggest-json 으로 남기는 모양과 같다.
fn suggest_cand_json(c: &termsuggest::Cand) -> Value {
    let mut extra = serde_json::Map::new();
    if let Some(n) = c.min_count { extra.insert("min_count".into(), json!(n)); }
    json!({
        "term": c.term, "fields": c.fields, "extra": Value::Object(extra),
        "checked": c.checked, "where": c.where_, "group": c.group,
        "df_pos": c.df_pos, "df_neg": c.df_neg, "score": c.score,
        "docs": c.docs, "clusters": c.clusters, "flags": c.flags,
    })
}

/// 규칙 단어 제안 전용 모드(--suggest-terms) — Python run_suggest_terms 와 같은 일.
/// 문서도 모델도 읽지 않는다. 이미 확정된 라벨과 저장해 둔 본문만 보고, 규칙에 넣을
/// 만한 말의 후보를 보여 준다. 규칙 파일은 고치지 않는다.
/// -out: 종료코드(0 정상 / 3 재료 없음 / 4 결과 쓰기 실패)
fn run_suggest_terms(opts: &Opts) -> i32 {
    // 재료 경로. 검토 이력·금지 목록은 정책 파일 옆에 두는 것을 규약으로 삼는다.
    let seeds_path = opts.seeds.clone().or_else(default_seed_path);
    let policy_dir = seeds_path.as_ref()
        .and_then(|p| Path::new(p).parent().map(|d| d.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."));
    let ov_path = opts.overrides.clone()
        .map(PathBuf::from)
        .unwrap_or_else(|| policy_dir.join("cso_override.jsonl"));
    let stop_path = opts.stopwords.clone()
        .map(PathBuf::from)
        .unwrap_or_else(|| policy_dir.join("doc_rule_stopwords.yaml"));

    let seed_file = seeds_path.as_ref().map(PathBuf::from);
    let have_seed = seed_file.as_ref().map(|p| p.is_file()).unwrap_or(false);
    let have_ov = ov_path.is_file();
    if !have_seed && !have_ov {
        let shown = seed_file.as_ref().map(|p| p.display().to_string())
            .unwrap_or_else(|| "class_seed.jsonl".into());
        errcodes::fail("seeds_missing",
            &format!("[MpowerClassify-rs] 제안할 재료가 없습니다.\n  · 기준 문서: {}\n  \
· 검토 확정 이력: {}\n  둘 중 하나는 있어야 합니다(--seeds · --overrides 로 지정).",
                     shown, ov_path.display()),
            Some(&shown));
    }

    let text_dir = opts.text_dir.as_ref().map(PathBuf::from);
    let docs = termsuggest::load_documents(
        if have_seed { seed_file.as_deref() } else { None },
        if have_ov { Some(ov_path.as_path()) } else { None },
        text_dir.as_deref(), !opts.include_auto_seeds);
    if opts.include_auto_seeds {
        note!("[MpowerClassify-rs] 사람이 승인하지 않은 seed 도 재료에 넣었습니다 — \
규칙이 만든 라벨이 다시 규칙을 만드는 고리가 생길 수 있습니다.");
    }

    // 규칙·분류 체계는 '거르기'에만 쓴다. 없어도 제안은 돈다.
    let rules_path = resolve_policy_file(&opts.doc_rules, doc_rules::default_doc_rule_filename());
    let mut rule_doc: Option<Value> = None;
    let mut suffixes: Vec<String> = vec![];
    let mut endings: Vec<String> = vec![];
    let mut titles: std::collections::HashMap<String, String> = Default::default();
    match rules_path.as_ref().filter(|p| p.is_file()) {
        Some(p) => {
            match std::fs::read_to_string(p).ok()
                .and_then(|t| serde_yaml::from_str::<Value>(&t).ok()) {
                Some(doc) => {
                    let rules: Vec<Value> = doc.get("doctype_rules").and_then(|v| v.as_array())
                        .map(|a| a.to_vec()).unwrap_or_default();
                    match axes::taxonomy_from_rules(&rules, "") {
                        Ok(tax) => {
                            for n in &tax.nodes { titles.insert(n.dc_id.clone(), n.title.clone()); }
                        }
                        Err(e) => note!("[MpowerClassify-rs] 규칙에서 분류 체계를 짓지 \
못해 '다른 분류 이름'을 거르지 못합니다: {}", e),
                    }
                    rule_doc = Some(doc);
                }
                None => note!("[MpowerClassify-rs] 규칙 파일을 읽지 못해 '이미 있는 말'을 \
거르지 못합니다: {}", p.display()),
            }
            // 끝말·활용형 꼬리는 규칙 파일 옆 유의어 사전에서 온다(core 가 기준).
            let syn = docvocab::load_synonyms(p);
            suffixes = syn.suffixes.clone();
            endings = termsuggest::load_endings(&syn.layers);
        }
        None => note!("[MpowerClassify-rs] 규칙 파일이 없어 '이미 있는 말'을 거르지 \
못합니다: {}", doc_rules::default_doc_rule_filename()),
    }
    let sidx = termsuggest::suffix_index(&suffixes);
    let stopwords = termsuggest::load_stopwords(Some(stop_path.as_path()));

    // 훑을 분류를 정한다. all 이면 확정 문서가 한 건이라도 있는 분류 전부.
    let want = opts.suggest_terms.clone().unwrap_or_default().trim().to_string();
    let mut labelled: Vec<String> = docs.iter().flat_map(|d| d.labels.iter().cloned())
        .collect::<std::collections::BTreeSet<String>>().into_iter().collect();
    labelled.sort();
    let nodes: Vec<String> = if want.to_lowercase() == "all" {
        if labelled.is_empty() {
            println!("[MpowerClassify-rs] 확정된 문서가 한 건도 없습니다 — \
검토 화면에서 분류를 확정하면 재료가 쌓입니다.");
            return 0;
        }
        labelled.clone()
    } else {
        if !labelled.contains(&want) {
            note!("[MpowerClassify-rs] '{}' 로 확정된 문서가 없습니다. 확정 문서가 있는 분류: {}",
                      want, if labelled.is_empty() { "(없음)".to_string() } else { labelled.join(", ") });
        }
        vec![want]
    };

    let min_docs = opts.suggest_min_docs.unwrap_or(termsuggest::MIN_DOCS);
    println!("[MpowerClassify-rs] 재료 {}건(기준문서 {} · 검토이력 {} · 본문 {})",
             docs.len(),
             if have_seed { "있음" } else { "없음" },
             if have_ov { "있음" } else { "없음" },
             if opts.text_dir.is_some() { "있음" } else { "없음" });
    // 숫자 두 개의 뜻을 여기서 한 번만 밝힌다 — 줄마다 붙이면 목록이 못 읽게 길어진다.
    println!("  덮는율 = 이 분류 문서 중 그 말이 나온 비율(참고) · 다른분류 = 다른 분류 문서 중 나온 비율(낮을수록 좋음 — 이 값으로 거릅니다)");
    println!("  같은 폴더 문서는 합쳐서 1건으로 세어 비율을 냅니다. [v] 근거가 단단한 후보 · [ ] 후보 · [-] 문턱 미달(참고)");

    let mut out: Vec<Value> = vec![];
    for node in &nodes {
        let res = termsuggest::suggest(node, &docs, rule_doc.as_ref(), &stopwords,
                                       &titles, &sidx, &endings, min_docs);
        let name = titles.get(node).cloned().unwrap_or_default();
        let head = if name.is_empty() { format!("\n■ {}", node) }
                   else { format!("\n■ {}({})", name, node) };
        let tail = if res.no_text > 0 { format!(" · 본문 없음 {}건", res.no_text) }
                   else { String::new() };
        println!("{} — 확정 {}건 / 폴더 {}곳{}", head, res.docs, res.clusters, tail);
        if !res.reason.is_empty() { println!("  {}", res.reason); }
        if res.candidates.is_empty() && res.cluster_only.is_empty() {
            if res.reason.is_empty() { println!("  제안할 말이 없습니다."); }
            if !res.below.is_empty() {
                // 문턱은 기계가 긋지만, 그 말이 쓸모 있는지는 사람이 안다.
                println!("  [문턱 미달 — 참고] 검출은 됐지만 기준에 못 미친 말입니다. \
쓸 만한지는 관리자가 판단하세요:");
                for c in &res.below { println!("{}", below_line(c)); }
            }
            out.push(suggestion_json(&res));
            continue;
        }
        for c in &res.candidates { println!("{}", suggest_line(c)); }
        if !res.cluster_only.is_empty() {
            // 한 폴더에 몰린 어휘는 근거가 약해 자동 채택하지 않는다.
            println!("  [한 폴더 전용] 그 폴더에서만 쓰는 말일 수 있습니다:");
            for c in &res.cluster_only { println!("{}", suggest_line(c)); }
        }
        out.push(suggestion_json(&res));
    }

    if let Some(path) = &opts.suggest_json {
        let text = serde_json::to_string_pretty(&Value::Array(out)).unwrap_or_default();
        if let Err(e) = std::fs::write(path, text) {
            errcodes::fail("output_write_failed",
                &format!("[MpowerClassify-rs] 결과를 쓰지 못했습니다: {}", e), Some(path));
        }
        println!("\n[MpowerClassify-rs] 결과를 남겼습니다: {}", path);
    }

    println!("\n[MpowerClassify-rs] 규칙 파일은 고치지 않았습니다 — \
넣을 말은 화면(설정 ② 판단 기준)에서 고르세요.");
    0
}

/// 제안 결과 하나를 json 으로(파이썬 판 --suggest-json 과 같은 칸).
fn suggestion_json(res: &termsuggest::Suggestion) -> Value {
    json!({
        "node": res.node, "docs": res.docs, "clusters": res.clusters,
        "no_text": res.no_text, "reason": res.reason,
        "candidates": res.candidates.iter().map(suggest_cand_json).collect::<Vec<Value>>(),
        "cluster_only": res.cluster_only.iter().map(suggest_cand_json).collect::<Vec<Value>>(),
        "below": res.below.iter().map(below_json).collect::<Vec<Value>>(),
    })
}

/// 업무분류 규칙 자동 생성(--build-doc-rule) — Python run_build_doc_rule 과 같은 일.
/// 체계 JSON · 사전 · 본보기 · doc_rule.local.yaml 로 doc_rule.yaml 을 통째로 만들고,
/// 방금 쓴 파일을 로더로 다시 읽어 검증한다.
/// -out: 종료코드(0 정상 / 3 체계 JSON 없음 / 4 입력 오류·쓰기 실패)
/// --seed-fill-title — 기준 문서에 제목(첫 줄)을 채운다(파이썬 run_seed_fill_title 과 같은 규칙 · 같은 안내).
/// BM25 2차는 기준 문서의 제목 · 파일명을 비교한다. 예전에 등록한 줄에는 제목 칸이 없어 한 번 채운다.
///  1) 제목이 이미 있는 줄은 건너뛴다
///  2) 본문은 --text-dir/<지문>.txt 에서 먼저 찾고, 없으면 원본 파일을 다시 추출한다
///  3) 하나라도 채웠으면 잠근 채로 다시 쓴다
fn run_seed_fill_title(opts: &Opts) -> i32 {
    let path = match opts.seeds.as_deref() {
        Some(p) if std::path::Path::new(p).is_file() => p.to_string(),
        other => errcodes::fail("bad_args", &format!(
            "[MpowerClassify-rs] --seed-fill-title 은 기준 문서 파일이 필요합니다 (--seeds <class_seed.jsonl>): {}",
            other.unwrap_or("(없음)")), None),
    };
    let _lock = match seedstore::SeedLock::acquire(&path) {
        Ok(l) => l,
        Err(e) => errcodes::fail("output_write_failed", &format!("[MpowerClassify-rs] {}", e), Some(path.as_str())),
    };
    let (mut rows, _skipped) = seedstore::load(&path);
    let (mut filled, mut empty, mut nosrc, mut had) = (0u32, 0u32, 0u32, 0u32);
    for r in rows.iter_mut() {
        if r.get("title").and_then(|t| t.as_str()).map_or(false, |t| !t.is_empty()) {
            had += 1;
            continue;
        }
        let file = r.get("file").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let hash = r.get("hash").and_then(|v| v.as_str()).unwrap_or("").to_string();
        if !doctype::title_allowed(&file) {
            // ppt · xls · xlsx 는 제목을 쓰지 않는다 — 원본을 다시 읽어 봐야 빈 제목이다
            empty += 1;
            continue;
        }
        let mut text: Option<String> = None;
        if let (Some(dir), false) = (opts.text_dir.as_deref(), hash.is_empty()) {
            let tp = std::path::Path::new(dir).join(format!("{}.txt", hash));
            if let Ok(b) = std::fs::read(&tp) {
                text = Some(String::from_utf8_lossy(&b).into_owned());
            }
        }
        if text.is_none() && !file.is_empty() && std::path::Path::new(&file).is_file() {
            // 저장해 둔 본문이 없으면 원본을 다시 읽는다
            let p = std::path::Path::new(&file);
            text = extract::extract_text_with(p, detect::detect_format(p), extract::mode(), &file).ok();
        }
        let Some(text) = text else { nosrc += 1; continue };
        let t = bm25::title_of(&text, &file);
        if t.is_empty() {
            empty += 1;
        } else if let Some(m) = r.as_object_mut() {
            m.insert("title".into(), json!(t));
            filled += 1;
        }
    }
    if filled > 0 {
        if let Err(e) = seedstore::save(&path, &rows) {
            errcodes::fail("output_write_failed",
                &format!("[MpowerClassify-rs] 기준 문서를 쓰지 못했습니다: {} :: {}", path, e), Some(path.as_str()));
        }
    }
    // 결과 요약이라 --json-errors 여도 낸다(note! 는 그때 꺼진다 — 파이썬 판도 늘 낸다)
    eprintln!("[seed-fill-title] 채움 {} · 제목 없음 {}(ppt·xls·빈 첫 줄) · 원문 못 찾음 {} · 이미 있음 {}",
              filled, empty, nosrc, had);
    0
}

fn run_build_doc_rule(opts: &Opts) -> i32 {
    // 출력은 '아직 없을 수 있는 파일'이라 is_file 로 거르지 않는다. 없으면 정책 폴더/exe 옆.
    let out_path = match &opts.doc_rules {
        Some(p) => PathBuf::from(p),
        None => match std::env::var("CSOCLASSIFY_POLICY_DIR") {
            Ok(d) => Path::new(&d).join(doc_rules::default_doc_rule_filename()),
            Err(_) => std::env::current_exe().ok().and_then(|e| e.parent().map(|p| p.to_path_buf()))
                .unwrap_or_else(|| PathBuf::from(".")).join(doc_rules::default_doc_rule_filename()),
        },
    };
    let policy_dir = out_path.parent().filter(|p| !p.as_os_str().is_empty())
        .map(|p| p.to_path_buf()).unwrap_or_else(|| PathBuf::from("."));
    // 체계 JSON 기본 자리는 규칙 파일 옆 — 연동 때 엠파워가 그 자리에 둔다.
    let export = opts.export_input.as_ref().map(PathBuf::from)
        .unwrap_or_else(|| policy_dir.join(axes::default_export_input_filename()));
    if !export.is_file() {
        errcodes::fail("export_input_missing",
            &format!("[MpowerClassify-rs] 체계 JSON 을 찾을 수 없습니다: {}\n  · --export-input <파일경로> 로 지정하거나,\n  · 규칙 파일 옆에 doc_classification_export.json 을 두세요.",
                     export.display()), export.to_str());
    }
    let (doc, warns) = match docbuild::build_doc_rule(&export, &policy_dir) {
        Ok(x) => x,
        Err(docbuild::BuildError::Local(e)) => errcodes::fail("doc_rules_invalid",
            &format!("[MpowerClassify-rs] {}", e), None),
        Err(docbuild::BuildError::Export(e)) => errcodes::fail("export_input_invalid",
            &format!("[MpowerClassify-rs] {}", e), export.to_str()),
    };
    if let Err(e) = docbuild::write_doc_rule(&out_path, &doc) {
        errcodes::fail("doc_rules_write_failed", &format!("[MpowerClassify-rs] 규칙 파일을 쓰지 못했습니다: {}", e),
                       out_path.to_str());
    }
    // 방금 쓴 파일을 엔진이 그대로 읽는지 확인한다 — 다음 분류 때 알게 되면 늦다.
    if let Err(e) = doc_rules::load_doc_rules(&out_path, None) {
        errcodes::fail("doc_rules_invalid", &format!("[MpowerClassify-rs] {}", e), out_path.to_str());
    }
    // 요약·경고는 오류가 아니다 — --json-errors(화면 호출)여도 stderr 로 낸다(화면이 요약 줄을 읽는다).
    for w in &warns { note!("[MpowerClassify-rs] {}", w); }
    let rules = doc["doctype_rules"].as_array().map(|a| a.len()).unwrap_or(0);
    let local = doc["doctype_rules"].as_array().map(|a| a.iter().filter(|r| r.get("local").is_some()).count()).unwrap_or(0);
    let inputs = doc["generated"]["inputs"].as_array().map(|a| a.len()).unwrap_or(0);
    note!("[MpowerClassify-rs] {} 생성 — 규칙 {}개(회사 조정 {}개) · 판 {} · 입력 {}개",
          out_path.display(), rules, local, doc["version"].as_str().unwrap_or(""), inputs);
    0
}

/// --conflict "doctype=top_n:3" → (축 id, ConflictSpec). 값 검증은 doc_rule.yaml
/// 로더의 검증기를 그대로 재사용해, 파일에 적을 때와 실행 시 덮어쓸 때가 항상
/// 같은 기준으로 걸러지게 한다(설계서 6-5).
fn parse_conflict_override(spec: &str) -> Result<(String, ConflictSpec), String> {
    let (axis, value) = match spec.split_once('=') {
        Some((a, v)) => (a.trim().to_string(), v.trim().to_string()),
        None => return Err(format!("--conflict 형식이 올바르지 않습니다: {:?} (예: doctype=top_n:3)", spec)),
    };
    let raw: Value = match value.split_once(':') {
        Some((strategy, n_str)) => {
            let n: i64 = n_str.parse()
                .map_err(|_| format!("--conflict 의 n 값이 정수가 아닙니다: {:?}", n_str))?;
            json!({"strategy": strategy, "n": n})
        }
        None => json!(value),
    };
    if let Some(v) = doc_rules::check_conflict(&raw) {
        return Err(format!("--conflict {:?} — {}", spec, v.detail));
    }
    Ok((axis, doc_rules::parse_conflict(&raw)))
}

/// labels.doctype 의 후보들에서 (뿌리 카테고리, 실제 걸린 노드) 이름 쌍을 뽑는다.
/// 이미 레코드에 박힌 path 문자열("A > B > C")만 쓰므로 taxonomy 없이도 집계된다.
fn doctype_breakdown(sig: &doctype::DoctypeSignal) -> Vec<(String, String)> {
    sig.values.iter().map(|v| {
        let segments: Vec<&str> = v.path.split(" > ").filter(|s| !s.is_empty()).collect();
        match segments.as_slice() {
            [] => (v.dc_id.clone(), v.dc_id.clone()),
            segs => (segs[0].to_string(), segs[segs.len() - 1].to_string()),
        }
    }).collect()
}

//------------------------------------------------------------------
// 업무분류 신호 → dc_id 목록
//=> 압축별 분포를 셀 때 쓴다. doctype_breakdown 은 화면용 '뿌리/말단 제목'을
//   주는데, 집계에는 제목이 아니라 **식별자**가 필요하다 — 제목은 분류체계를
//   다시 내보내면 바뀔 수 있지만 dc_id 는 그 문서의 이름표라 안 바뀐다.
//
// -in: sig = 업무분류 신호
//
// -out: Vec<String> = dc_id 목록(라벨이 없으면 빈 목록)
// -out: error = 없음
//------------------------------------------------------------------
fn doctype_ids(sig: &doctype::DoctypeSignal) -> Vec<String> {
    sig.values.iter().map(|v| v.dc_id.clone()).collect()
}

/// `--files-from` 목록을 읽어 대상 경로로 바꾼다("-" 이면 표준입력).
///
/// 여러 폴더에 흩어진 문서를 **한 프로세스**로 처리하려고 만든 입력 방식이다.
/// `--dir` 은 폴더 하나 아래만 훑을 수 있어서, 경로가 흩어져 있으면 파일마다
/// 프로세스를 새로 띄우게 되고 그때마다 ONNX 모델을 다시 읽어 느려진다.
/// 목록을 통째로 받으면 모델을 한 번만 읽고 전부 처리한다.
/// 규칙은 Python 판 `read_files_from()` 과 같다 — 빈 줄·`#` 주석 무시, 감싼
/// 따옴표 제거, 실제 파일만, 중복은 첫 것만(입력 순서 보존).
fn read_files_from(src: &str) -> Vec<PathBuf> {
    let raw = if src == "-" {
        let mut buf = String::new();
        use std::io::Read;
        // 표준입력을 못 읽으면 대상 0건이 된다 — 아래 '처리할 파일이 없습니다'가 받는다.
        let _ = std::io::stdin().read_to_string(&mut buf);
        buf
    } else {
        // 목록 파일 자체를 못 열면 그 자리에서 멈춘다. 예전에는 빈 목록으로 삼켜
        // "목록이 비었거나…" 라는 엉뚱한 안내가 나갔다 — 원인(파일 없음·권한)이 다르다.
        match std::fs::read_to_string(src) {
            Ok(s) => s,
            Err(e) => errcodes::fail("no_input",
                &format!("[MpowerClassify-rs] --files-from 목록 파일을 읽을 수 없습니다: {}\n  {}", src, e),
                Some(src)),
        }
    };
    // 메모장이 붙이는 BOM 을 떼어 낸다(안 떼면 첫 경로가 통째로 어긋난다).
    let raw = raw.trim_start_matches('\u{feff}');

    let mut files: Vec<PathBuf> = Vec::new();
    let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
    let mut skipped = 0usize;
    for line in raw.lines() {
        // 공백이 든 경로를 따옴표로 감싼 목록(dir /b 결과 등)도 받아 준다.
        let path = line.trim().trim_matches('"');
        if path.is_empty() || path.starts_with('#') { continue; }
        let p = PathBuf::from(path);
        // 같은 문서를 두 번 임베딩하지 않도록 여기서 미리 중복을 접는다.
        // 존재하는 경로는 canonicalize 로, 없는 경로는 문자열로 비교한다.
        let key = std::fs::canonicalize(&p)
            .map(|q| q.to_string_lossy().to_lowercase())
            .unwrap_or_else(|_| path.to_lowercase());
        if !seen.insert(key) { continue; }
        if p.is_file() { files.push(p); } else { skipped += 1; }
    }
    // 조용히 버리면 "왜 결과 건수가 모자라지?" 로 이어진다 — 건수만이라도 남긴다.
    if skipped > 0 {
        note!("[MpowerClassify-rs] --files-from: 파일이 아니어서 건너뜀 {}건", skipped);
    }
    files
}

/// 대상 파일 수집: --files-from 목록, --file 하나, --dir 재귀.
/// 이번 실행에서 목록(--filelist)이 '대상을 정했나'.
/// --dir 과 함께 준 경우 목록은 ID 사전일 뿐이라, 그 바깥 항목을 '빠졌다'고 하면
/// 엉뚱한 경고가 된다. 그래서 없는 파일을 결과에 남길지 여기서 가린다.
fn filelist_is_target(opts: &Opts) -> bool {
    opts.file.is_none() && opts.dir.is_none() && opts.files_from.is_none()
}

//------------------------------------------------------------------
// 처리할 문서가 0건일 때 — 계약대로 알리고 종료
//=> 분류와 --text-only 가 같은 말·같은 코드로 멈추게 한 곳에 모은다.
//   예전에는 --text-only 만 stderr 한 줄과 종료코드 3 으로 끝나, --json-errors 를
//   줘도 JSON 이 나가지 않았다.
//    1) 대상을 아예 안 줌 → 1002 no_target_arg (명령 자체를 고쳐야 한다)
//    2) 줬는데 0건      → 1001 no_input (사용자에게 대상을 다시 물으면 된다)
//
// -in: opts = 파싱된 실행 옵션(file/dir/files_from/filelist/glob 을 본다)
//
// -out: 반환하지 않음(프로세스 종료)
// -out: error = 언제나 errcodes::fail 로 종료(종료코드 3)
//------------------------------------------------------------------
fn fail_no_input(opts: &Opts) -> ! {
    // 부르는 쪽의 대응이 다르므로 두 상황을 갈라 준다.
    match opts.file.as_deref().or(opts.dir.as_deref()).or(opts.files_from.as_deref())
              .or(opts.filelist.as_deref()) {
        None => errcodes::fail("no_target_arg",
            "[MpowerClassify-rs] 처리할 파일이 없습니다. --file · --dir · --files-from · --filelist 중 하나 지정.", None),
        Some(t) => {
            // 왜 0건인지를 상황에 맞게 말해 준다 — "없다"만으로는 무엇을
            // 고칠지 모른다.
            let why = if opts.file.is_some() {
                if Path::new(t).is_dir() {
                    "폴더입니다 — 폴더는 --dir 로 지정하세요.".to_string()
                } else {
                    "그런 파일이 없습니다.".to_string()
                }
            } else if opts.files_from.is_some() {
                // 파이썬 판과 같은 안내 — 목록은 '폴더·패턴'과 고칠 곳이 다르다.
                "목록이 비었거나, 목록의 경로가 모두 실제 파일이 아닙니다.".to_string()
            } else {
                format!("폴더가 없거나, --glob 패턴({})에 맞는 파일이 없습니다.",
                        opts.glob.as_deref().unwrap_or("*"))
            };
            errcodes::fail("no_input",
                &format!("[MpowerClassify-rs] 처리할 파일이 없습니다: {}\n{}", t, why),
                Some(t))
        }
    }
}

//------------------------------------------------------------------
// 복호화 성공 한 줄 남기기
//=> 어느 문서를 어떤 모드로 어디에 풀었는지 나중에 일반 로그(class_YYYYMMDD.log)로
//   추적할 수 있게 한다. 파이썬 판 _iter_dec_jobs 의 '복호화 성공' 줄과 모양이 같다.
//    1) 원래 경로·원래 이름·도구를 불렀는지(평문이면 복사만)를 적는다
//    2) enc_mode·zip_mode 와 임시 파일 경로, 걸린 시간을 적는다
//    3) 저장 파일 크기(stored_size — 엠파워 저장소의 .SMF, 암호화 여부와 상관없이)와
//       풀린 문서 크기(dec_size)를 적는다 — 둘을 견주면 압축(zip_mode)으로 얼마나
//       줄었는지, 풀린 결과가 터무니없지 않은지 로그만 보고 안다.
//       (목록·--size 의 size 는 DB 원본 크기라 뜻이 달라 이름을 겹치지 않게 했다)
//    4) 열쇠(mrk)는 적지 않는다
//
// -in: display = 원래 경로(결과의 file 칸과 같은 값)
// -in: name    = 원래 파일 이름(없으면 "-")
// -in: org_size = 엠파워 DB 원본 크기(--size 또는 목록의 size 칸. 없으면 None → 칸을 안 싣는다)
// -in: src     = 저장된(암호) 파일 경로(크기를 잰다)
// -in: d       = 풀린 결과(임시 파일 경로 · 도구 사용 여부)
// -in: info    = 복호화 정보(enc_mode · zip_mode 를 본다)
// -in: t0      = 풀기 시작 시각
//
// -out: 없음
// -out: error = 없음(로그를 못 써도 조용히 넘어간다. 크기를 못 재면 -1 로 적는다)
//------------------------------------------------------------------
fn log_dec_ok(display: &str, name: Option<&str>, org_size: Option<u64>, src: &Path,
              d: &decrypt::Decrypted, info: &filelist::DecInfo, t0: Instant) {
    // 크기를 재기 전에 시간을 먼저 끊는다 — 크기 재기는 풀기에 든 시간이 아니다.
    let elapsed = t0.elapsed().as_millis();
    let size_of = |p: &Path| std::fs::metadata(p).map(|m| m.len() as i64).unwrap_or(-1);
    // DB 원본 크기는 받은 값이 있을 때만 싣는다(파이썬 판과 같은 모양).
    let org_note = org_size.map(|n| format!("org_size={} ", n)).unwrap_or_default();
    linfo!("복호화 성공 file={} name={} tool={} enc_mode={} zip_mode={} {}stored_size={} dec_size={} tmp={} elapsed={}ms",
           display, name.unwrap_or("-"), if d.tool { "yes" } else { "no(copy)" },
           info.enc_mode, info.zip_mode, org_note, size_of(src), size_of(&d.file),
           d.file.to_string_lossy().replace('\\', "/"), elapsed);
}

//------------------------------------------------------------------
// 복호화 실패 로그 한 줄 만들기
//=> 성공 줄(log_dec_ok)과 같은 칸을 같은 차례로 싣는다 — 두 줄을 나란히 놓고 견줄 수 있게.
//   파이썬 판 _dec_fail_line 과 모양이 같다.
//    1) file · name · reason(사유 이름) · rc(도구 종료코드, 부르지 않았으면 "-")
//    2) enc_mode · zip_mode — 열쇠 칸이 잘못돼 해석하지 못했으면 "-"
//    3) org_size(DB 원본 크기, 받았을 때만) · stored_size · dec_size(결과 파일이 있었을 때만)
//    4) elapsed, 그리고 ' :: ' 뒤에 사람이 읽는 문장
//   임시 경로(tmp)는 싣지 않는다 — 실패한 결과물은 이미 지워졌다. 열쇠도 싣지 않는다.
//
// -in: display     = 원래 경로(결과의 file 칸과 같은 값)
// -in: name        = 원래 파일 이름(없으면 "-")
// -in: org_size    = 엠파워 DB 원본 크기(--size 또는 목록의 size 칸. 없으면 None)
// -in: info        = 복호화 정보(열쇠 칸을 해석하지 못했으면 None)
// -in: stored_size = 저장 파일(.SMF) 크기
// -in: e           = 실패 사유
// -in: t0          = 이 문서를 다루기 시작한 시각
//
// -out: String = 로그에 남길 한 줄(앞의 시각·PID 는 로거가 붙인다)
// -out: error = 없음
//------------------------------------------------------------------
fn dec_fail_line(display: &str, name: Option<&str>, org_size: Option<u64>,
                 info: Option<&filelist::DecInfo>, stored_size: u64,
                 e: &decrypt::DecError, t0: Instant) -> String {
    let org = org_size.map(|n| format!("org_size={} ", n)).unwrap_or_default();
    let dec = e.dec_size.map(|n| format!("dec_size={} ", n)).unwrap_or_default();
    format!("복호화 실패 file={} name={} reason={} rc={} enc_mode={} zip_mode={} {}stored_size={} {}elapsed={}ms :: {}",
            display, name.unwrap_or("-"), e.reason,
            e.rc.map(|r| r.to_string()).unwrap_or_else(|| "-".into()),
            info.map(|i| i.enc_mode.as_str()).unwrap_or("-"),
            info.map(|i| i.zip_mode.as_str()).unwrap_or("-"),
            org, stored_size, dec, t0.elapsed().as_millis(), e.message)
}

//------------------------------------------------------------------
// 본문만 뽑아 내보내기 (--text-only)
//=> 등급도 PII 도 보지 않고, 문서에서 글자만 뽑아 정제해 그대로 내놓는다.
//   파이썬 판 `run_text_only` 와 같은 모양이다 — 문서마다 '===== 경로 =====' 한 줄,
//   그 다음 본문. 규칙셋을 읽지 않으므로 **파서 자체의 속도·결과**를 잴 수 있다.
//    1) 대상 파일을 모으고 압축은 내부 문서로 펼친다(분류 때와 같은 대상 규칙)
//    2) 크기 상한은 그대로 지킨다 — 상한을 끄고 싶으면 --no-size-limit 을 준다
//    3) 본문 저장이 필요하면 --textsave 대신 --out 을 쓴다 — 이 모드는 본문이
//       곧 출력이라 따로 또 남길 이유가 없다
//
// -in: opts = 명령줄 옵션 묶음(--file/--dir/--out/--textsave 등을 본다)
//
// -out: 종료코드 — 전부 성공 0, 한 건이라도 추출 실패면 1
// -out: error = 개별 실패는 stderr 로 알리고 나머지는 계속 처리한다
//------------------------------------------------------------------
fn run_text_only(opts: &Opts) -> i32 {
    let files = collect_files(opts, None);
    // 엠파워 v11 암호화 문서(--file … --decrypt) — 먼저 풀고, 풀린 파일에서 본문을 뽑는다.
    // 이 모드는 목록을 읽지 않으므로 열쇠는 명령줄에서만 온다(check_decrypt_args 가 이미 봤다).
    let mut dec_sess: Option<decrypt::Session> = None;
    // (임시 라벨, 원래 라벨) — 출력의 '===== 경로 =====' 에 임시 경로가 나가지 않게 되돌린다.
    let mut dec_relabel: Option<(String, String)> = None;
    let files = match (opts.decrypt && !opts.no_decrypt, files.first().cloned()) {
        (true, Some(src)) => {
            let display = src.to_string_lossy().replace('\\', "/");
            let plan = dec_plan_for(opts, None, &display);
            let (info, name, size) = match plan {
                Some(DecPlan { info: Ok(i), name, size }) => (i, name, size),
                Some(DecPlan { info: Err((_, why)), .. }) => errcodes::fail(
                    "decrypt_args_invalid", &format!("[MpowerClassify-rs] {}", why), Some(&display)),
                None => errcodes::fail("decrypt_args_invalid",
                    "[MpowerClassify-rs] --decrypt 에 필요한 열쇠 정보가 없습니다.", Some(&display)),
            };
            let cmd = decrypt::resolve_cmd(opts.decrypt_cmd.as_deref());
            if !info.is_plain() {
                if let Err(why) = decrypt::preflight(&cmd) {
                    errcodes::fail("decrypt_cmd_missing", &format!("[MpowerClassify-rs] {}", why),
                                   cmd.to_str());
                }
            }
            let base = decrypt::resolve_tmp_base(opts.decrypt_tmp.as_deref());
            let mut sess = match decrypt::Session::open(
                cmd, &base, opts.decrypt_timeout.unwrap_or(decrypt::DEFAULT_TIMEOUT_SECS)) {
                Ok(s) => s,
                Err(why) => errcodes::fail("decrypt_unavailable",
                    &format!("[MpowerClassify-rs] {}", why), base.to_str()),
            };
            // 풀기에 걸린 시간을 성공 로그에 남기려고 잰다.
            let t_dec = Instant::now();
            // 분류 모드와 같은 복호화 전 크기 검사 — 예전에는 이 자리에 없어 큰 문서를 다 푼 뒤
            // 추출 상한에서 버렸다(파이썬 판은 같은 검사를 거친다). 넘으면 도구를 부르지 않는다.
            let stored_len = std::fs::metadata(&src).map(|m| m.len()).unwrap_or(0);
            let outcome = match decrypt::pre_size_limit(name.as_deref(), size, stored_len) {
                Some(e) => Err(e),
                None => sess.decrypt(&src, &info, name.as_deref(), size),
            };
            match outcome {
                Ok(d) => {
                    // 분류 모드와 같은 '복호화 성공' 줄을 남긴다.
                    log_dec_ok(&display, name.as_deref(), size, &src, &d, &info, t_dec);
                    dec_relabel = Some((d.file.to_string_lossy().replace('\\', "/"), display));
                    let v = vec![d.file.clone()];
                    dec_sess = Some(sess);
                    v
                }
                Err(e) => {
                    sess.close();
                    // 분류 모드와 같은 '복호화 실패' 줄을 먼저 남긴다(아래 fail 은 상태 문장만 남긴다).
                    let stored = std::fs::metadata(&src).map(|m| m.len()).unwrap_or(0);
                    errlog::err(&dec_fail_line(&display, name.as_deref(), size, Some(&info),
                                               stored, &e, t_dec));
                    // 환경 문제면 2, 이 파일만의 문제면 1 — 분류 때와 같은 갈래다.
                    errcodes::fail(if e.fatal { "decrypt_unavailable" } else { "decrypt_failed" },
                        &format!("[MpowerClassify-rs] 복호화하지 못했습니다 — {}", e.message),
                        Some(&display));
                }
            }
        }
        _ => files,
    };
    // 압축은 분류 때와 똑같이 내부 문서로 펼친다 — 대상이 달라지면 비교가 안 된다.
    let arc_tmp = std::env::temp_dir().join(format!("cso_txt_{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&arc_tmp);
    let _ = std::fs::create_dir_all(&arc_tmp);
    let mut arc_stats = archive::ExpandStats::default();
    let mut jobs: Vec<archive::Job> = archive::expand_paths(&files, &arc_tmp, 3, &mut arc_stats);
    if let Some((tmp_label, display)) = &dec_relabel {
        for j in jobs.iter_mut() {
            if j.label.starts_with(tmp_label.as_str()) {
                j.label = format!("{}{}", display, &j.label[tmp_label.len()..]);
            }
        }
    }
    if jobs.is_empty() {
        let _ = std::fs::remove_dir_all(&arc_tmp);
        fail_no_input(opts);
    }
    // --out 이 있으면 파일로, 없으면 화면으로. 본문이 크므로 버퍼를 쓴다.
    // --out 의 상위 폴더가 없으면 만들어 둔다. 못 만들면 아래 생성이 실패하며 그 까닭을 알린다.
    if let Some(parent) = opts.out.as_deref().and_then(|p| Path::new(p).parent()) {
        if !parent.as_os_str().is_empty() { let _ = std::fs::create_dir_all(parent); }
    }
    let mut sink: Box<dyn std::io::Write> = match &opts.out {
        Some(p) => match std::fs::File::create(p) {
            Ok(f) => Box::new(std::io::BufWriter::new(f)),
            Err(e) => {
                // 인자 오류(3)가 아니라 출력 실패(4001, 종료코드 1)다 — 파이썬 판과 같다.
                let _ = std::fs::remove_dir_all(&arc_tmp);
                errcodes::fail("output_write_failed",
                    &format!("[MpowerClassify-rs] 출력 파일을 쓰지 못했습니다: {}\n  {}", p, e),
                    Some(p));
            }
        },
        None => Box::new(std::io::BufWriter::new(std::io::stdout())),
    };
    let mut code = 0;
    // 못 읽은 문서 수 — 상태 줄에 "N건 중 M건"을 싣는다(종료코드 1 의 까닭).
    let mut failed = 0i64;
    for job in &jobs {
        let path = &job.src;
        let fmt = detect::detect_format(path);
        // 크기 상한은 분류 때와 같은 자를 쓴다 — 여기만 무제한이면 '이 판이 읽을 수
        // 있는 문서'의 범위가 모드마다 달라져 비교가 어긋난다.
        let limits_hit = limits::check_size_limit(path, fmt);
        // 사이냅 모드에서 못 읽은 까닭(엔진별). 자체 파서 모드는 빈 문자열이라 예전 문장 그대로다.
        let mut ext_why = String::new();
        let text = if let Some(why) = limits_hit.as_ref() {
            note!("[MpowerClassify-rs] 크기 초과: {} :: {}", job.label, why);
            code = 1;
            failed += 1;
            None
        } else {
            // 이번 실행의 추출 모드(자체 파서 · 사이냅 폴백 · 사이냅만)로 뽑는다.
            match extract::extract_text_with(path, fmt, extract::mode(), &job.label) {
                Ok(t) => Some(t),
                Err(why) => { ext_why = why; None }
            }
        };
        // 파서가 상한에서 멈췄다는 표식은 가져가서 비워 둔다(다음 문서에 안 묻게).
        let _ = limits::notes_take();
        match text {
            Some(t) => {
                let _ = writeln!(sink, "===== {} =====", job.label);
                let _ = writeln!(sink, "{}", t);
            }
            None => {
                // 크기 초과로 이미 센 문서는 또 세지 않는다(text 가 None 인 까닭이 같다).
                if limits_hit.is_none() { failed += 1; }
                if ext_why.is_empty() {
                    note!("[MpowerClassify-rs] 추출 실패: {} (감지={})",
                              job.label, fmt.as_str());
                } else {
                    note!("[MpowerClassify-rs] 추출 실패: {} (감지={}) :: {}",
                              job.label, fmt.as_str(), ext_why);
                }
                code = 1;
            }
        }
    }
    let _ = sink.flush();
    let _ = std::fs::remove_dir_all(&arc_tmp);
    // 풀어 둔 평문을 지운다(본문은 이미 다 뽑았다).
    if let Some(sess) = dec_sess.as_ref() { sess.close(); }
    errcodes::set_counts(jobs.len() as i64, failed);
    code
}

//------------------------------------------------------------------
// 문서 하나를 어떻게 꺼낼지 — 복호화 계획
//
// -필드: info = 열쇠와 모드. Err 이면 그 문서는 풀지도 읽지도 않고 오류 레코드로 남긴다
//               (열쇠 칸이 일부만 있거나 감싼 열쇠가 온 경우)
// -필드: name = 원래 파일 이름(없을 수 있다)
// -필드: size = 원래 크기(없을 수 있다)
//------------------------------------------------------------------
struct DecPlan {
    info: Result<decrypt::DecInfo, (&'static str, String)>,
    name: Option<String>,
    size: Option<u64>,
}

//------------------------------------------------------------------
// 명령줄로 받은 열쇠 세 칸 → 복호화 정보
//=> --mrk 가 없으면 환경변수 CSOCLASSIFY_MRK 를 본다(명령줄에 열쇠를 남기지 않는 길).
//
// -in: opts = 파싱된 실행 옵션
//
// -out: Ok(None) = 명령줄에 열쇠 정보가 없다 / Ok(Some) = 쓸 수 있다
// -out: error = Err((사유, 문장)) — 일부만 줬거나 감싼 열쇠
//------------------------------------------------------------------
fn cli_dec_info(opts: &Opts) -> Result<Option<decrypt::DecInfo>, (&'static str, String)> {
    let env_mrk = std::env::var("CSOCLASSIFY_MRK").ok();
    // 환경변수는 모드(--enc-mode·--zip-mode)를 명령줄로 줬을 때만 본다 — 안 그러면
    // 남아 있던 환경변수 하나가 '목록에서 열쇠를 찾는 실행'을 '일부만 줌' 오류로 만든다.
    let modes_given = opts.enc_mode.is_some() || opts.zip_mode.is_some();
    let mrk = opts.mrk.as_deref()
        .or(if opts.decrypt && modes_given { env_mrk.as_deref() } else { None });
    filelist::parse_dec_info(mrk, opts.enc_mode.as_deref(), opts.zip_mode.as_deref())
}

//------------------------------------------------------------------
// 명령줄로 받은 원래 크기(--size) → 정수
//=> 목록의 size 칸과 같은 뜻이다. 0 이상의 정수만 받는다(앞뒤 공백은 무시).
//   파이썬 판 _cli_dec_size 와 같은 판정이다.
//
// -in: opts = 파싱된 실행 옵션
//
// -out: Ok(Some(n)) = 원래 크기(바이트) / Ok(None) = --size 를 안 줌(크기 검사 건너뜀)
// -out: error = 숫자가 아니거나 음수면 Err(사람이 읽는 문장)
//------------------------------------------------------------------
fn cli_dec_size(opts: &Opts) -> Result<Option<u64>, String> {
    let raw = match &opts.dec_size {
        Some(v) => v,
        None => return Ok(None),
    };
    let s = raw.trim();
    // "1,024" · "-1" · "1.5" 같은 값은 받지 않는다 — 크기를 잘못 주면 멀쩡한 문서가 실패로 남는다.
    if s.is_empty() || !s.bytes().all(|b| b.is_ascii_digit()) {
        return Err(format!("--size 는 0 이상의 정수(바이트)여야 합니다: {}", raw));
    }
    s.parse::<u64>().map(Some)
        .map_err(|_| format!("--size 는 0 이상의 정수(바이트)여야 합니다: {}", raw))
}

//------------------------------------------------------------------
// --decrypt 관련 인자 조합 검사 — 문서를 읽기 전에 끝낸다
//=> "풀릴 줄 알았는데 안 풀린" 상태를 만들지 않는다(설계 §4 표).
//    1) --decrypt 는 --file 한 건에만 쓴다(폴더에는 파일마다 다른 열쇠를 줄 방법이 없다)
//    2) --decrypt 와 --no-decrypt 는 함께 못 쓴다
//    3) --decrypt 면 열쇠 셋이 있어야 한다(없으면 --filelist 에서 찾는다)
//    4) 열쇠·이름·크기만 주고 --decrypt 를 안 주면 오류
//    5) --size 는 0 이상의 정수여야 한다
//
// -in: opts = 파싱된 실행 옵션
//
// -out: 없음(문제가 없으면 그냥 돌아온다)
// -out: error = 조합이 틀리면 errcodes::fail("decrypt_args_invalid") 로 종료(코드 3)
//------------------------------------------------------------------
fn check_decrypt_args(opts: &Opts) {
    let bad = |msg: &str| -> ! {
        errcodes::fail("decrypt_args_invalid", &format!("[MpowerClassify-rs] {}", msg), None)
    };
    let has_keys = opts.mrk.is_some() || opts.enc_mode.is_some() || opts.zip_mode.is_some();
    if !opts.decrypt {
        if has_keys || opts.name.is_some() || opts.dec_size.is_some() {
            bad("--mrk · --enc-mode · --zip-mode · --name · --size 는 --decrypt 와 함께 써야 합니다.\n  \
                 풀어서 분류하려면 --decrypt 를 붙이고, 이미 풀어 둔 파일이면 이 인자들을 빼세요.");
        }
        return;
    }
    if opts.no_decrypt {
        bad("--decrypt 와 --no-decrypt 는 함께 쓸 수 없습니다.");
    }
    if opts.file.is_none() {
        bad("--decrypt 는 --file 한 건에만 씁니다. 여러 파일은 --filelist 의 각 줄에 \
             mrk · enc_mode · zip_mode 를 적어 주세요(파일마다 열쇠가 다릅니다).");
    }
    match cli_dec_info(opts) {
        Ok(Some(_)) => {}
        // 명령줄에 열쇠가 없으면 목록에서 찾는다 — 목록도 없으면 풀 방법이 없다.
        // (--text-only 는 목록을 읽지 않으므로 명령줄에 있어야 한다.)
        Ok(None) if opts.filelist.is_some() && !opts.text_only => {}
        Ok(None) => bad("--decrypt 에는 --mrk · --enc-mode · --zip-mode 가 필요합니다\
                         (--mrk 는 환경변수 CSOCLASSIFY_MRK 로도 줄 수 있습니다)."),
        Err((_, why)) => bad(&why),
    }
    // --size 는 문서를 읽기 전에 모양을 본다 — 잘못된 값으로 돌면 그 문서가 size_mismatch 로 남는다.
    if let Err(why) = cli_dec_size(opts) {
        bad(&why);
    }
}

//------------------------------------------------------------------
// 이 문서의 복호화 계획 찾기
//=> 명령줄(--decrypt) → 목록(--filelist)의 그 줄 순으로 본다.
//    1) --no-decrypt 면 언제나 None(그대로 읽는다)
//    2) --decrypt 면 명령줄 값이 우선, 없으면 목록의 그 줄
//    3) 그 밖에는 목록의 그 줄에 열쇠 칸이 있을 때만
//
// -in: opts    = 파싱된 실행 옵션
// -in: flist   = 읽어 둔 목록(없으면 None)
// -in: display = 문서 경로(목록의 path 와 같은 표기)
//
// -out: Some(DecPlan) = 풀어야(또는 오류로 남겨야) 하는 문서 / None = 그대로 읽는 문서
// -out: error = 예외 없음
//------------------------------------------------------------------
fn dec_plan_for(opts: &Opts, flist: Option<&filelist::FileList>, display: &str) -> Option<DecPlan> {
    if opts.no_decrypt {
        return None;
    }
    let entry = flist.and_then(|fl| fl.lookup(display)).map(|(e, _)| e);
    if opts.decrypt {
        let name = opts.name.clone().or_else(|| entry.and_then(|e| e.name.clone()));
        // 크기도 이름처럼 명령줄(--size)이 우선, 없으면 목록의 size 칸. 둘 다 없으면 None → 크기 검사 건너뜀.
        // 값 모양은 check_decrypt_args 가 이미 봤다.
        let size = cli_dec_size(opts).ok().flatten().or_else(|| entry.and_then(|e| e.size));
        return match cli_dec_info(opts) {
            Ok(Some(info)) => Some(DecPlan { info: Ok(info), name, size }),
            Err(why) => Some(DecPlan { info: Err(why), name, size }),
            Ok(None) => match entry {
                Some(e) if e.dec.is_some() => Some(DecPlan { info: Ok(e.dec.clone().unwrap()), name, size }),
                Some(e) if e.dec_problem.is_some() =>
                    Some(DecPlan { info: Err(e.dec_problem.clone().unwrap()), name, size }),
                _ => Some(DecPlan { info: Err(("info_incomplete",
                    "--decrypt 를 줬지만 명령줄에도 목록에도 이 파일의 열쇠가 없습니다".into())), name, size }),
            },
        };
    }
    let e = entry?;
    if let Some(why) = &e.dec_problem {
        return Some(DecPlan { info: Err(why.clone()), name: e.name.clone(), size: e.size });
    }
    e.dec.as_ref().map(|d| DecPlan { info: Ok(d.clone()), name: e.name.clone(), size: e.size })
}

fn collect_files(opts: &Opts, flist: Option<&filelist::FileList>) -> Vec<PathBuf> {
    // 목록 입력이 있으면 그것이 대상이다(--file/--dir 과의 동시 사용은 호출부에서 막는다).
    if let Some(lst) = &opts.files_from {
        return read_files_from(lst);
    }
    if let Some(f) = &opts.file {
        // 실제 파일일 때만 대상으로 삼는다(파이썬 판과 같은 규칙). 없는 경로·폴더를
        // 그대로 넘기면 '추출 실패' 레코드가 만들어져 결과처럼 나간다.
        let p = PathBuf::from(f);
        return if p.is_file() { vec![p] } else { vec![] };
    }
    if let Some(d) = &opts.dir {
        // --glob 패턴(콤마·중괄호로 여러 개)을 펼쳐 파일 '이름'에 맞춰 거른다.
        // 패턴이 없거나 "*" 면 전부 통과시킨다(예전과 같은 동작).
        let pats = expand_glob_patterns(opts.glob.as_deref().unwrap_or("*"));
        let pass_all = pats.is_empty() || pats.iter().any(|p| p == "*");
        return walkdir::WalkDir::new(d).into_iter()
            .filter_map(|e| e.ok())
            .filter(|e| e.file_type().is_file())
            .map(|e| e.into_path())
            .filter(|p| {
                if pass_all { return true; }
                let name = p.file_name().map(|n| n.to_string_lossy().into_owned())
                    .unwrap_or_default();
                pats.iter().any(|pat| glob_match(pat, &name))
            })
            .collect();
    }
    // --file/--dir/--files-from 이 하나도 없고 목록만 준 경우 — 목록이 대상을 정한다
    // (설계 §7-5-2-1 "정식 운영"). --dir 과 함께 주면 위에서 이미 돌아갔고, 그때
    // 목록은 'ID 사전' 역할만 한다.
    if let Some(fl) = flist {
        return fl.target_paths().into_iter().map(PathBuf::from).collect();
    }
    vec![]
}

/// --glob 값 하나를 개별 패턴 목록으로 펼친다(Python cli.expand_glob_patterns 포팅).
///
/// 두 표기를 모두 받는다 — 콤마 나열("*.hwp,*.pdf")과 중괄호("*.{hwp,pdf}").
/// 중괄호 안의 콤마는 그룹 구분자이므로, 먼저 '중괄호 밖' 최상위 콤마로만 자른 뒤
/// 각 토큰의 중괄호를 펼친다. 두 표기를 섞어 써도 된다.
fn expand_glob_patterns(spec: &str) -> Vec<String> {
    let mut tokens: Vec<String> = vec![];
    let (mut buf, mut depth) = (String::new(), 0i32);
    for ch in spec.chars() {
        match ch {
            '{' => { depth += 1; buf.push(ch); }
            '}' => { depth = (depth - 1).max(0); buf.push(ch); }
            ',' if depth == 0 => { tokens.push(std::mem::take(&mut buf)); }
            _ => buf.push(ch),
        }
    }
    tokens.push(buf);

    let mut out: Vec<String> = vec![];
    for tok in tokens {
        let tok = tok.trim();
        if tok.is_empty() { continue; }
        for p in expand_braces(tok) {
            if !out.contains(&p) { out.push(p); }
        }
    }
    out
}

/// "a.{x,y}" → ["a.x", "a.y"]. 중괄호가 없으면 그대로 한 개.
fn expand_braces(pat: &str) -> Vec<String> {
    let open = match pat.find('{') { Some(i) => i, None => return vec![pat.to_string()] };
    // 짝이 되는 닫는 괄호를 깊이를 세며 찾는다(중첩 대비).
    let mut depth = 0i32;
    let mut close = None;
    for (i, ch) in pat.char_indices().skip(open) {
        match ch {
            '{' => depth += 1,
            '}' => { depth -= 1; if depth == 0 { close = Some(i); break; } }
            _ => {}
        }
    }
    let close = match close { Some(i) => i, None => return vec![pat.to_string()] };
    let (pre, post) = (&pat[..open], &pat[close + 1..]);
    let inner = &pat[open + 1..close];

    let mut out = vec![];
    let (mut buf, mut d) = (String::new(), 0i32);
    let mut opts_: Vec<String> = vec![];
    for ch in inner.chars() {
        match ch {
            '{' => { d += 1; buf.push(ch); }
            '}' => { d -= 1; buf.push(ch); }
            ',' if d == 0 => opts_.push(std::mem::take(&mut buf)),
            _ => buf.push(ch),
        }
    }
    opts_.push(buf);
    for o in opts_ {
        for expanded in expand_braces(&format!("{}{}{}", pre, o.trim(), post)) {
            if !out.contains(&expanded) { out.push(expanded); }
        }
    }
    out
}

/// glob 한 개를 파일 이름에 맞춰 본다(`*` 와 `?` 만 지원 — 확장자 필터가 목적).
/// 대소문자는 무시한다(윈도우 관습, "*.PDF" 로 적어도 .pdf 가 잡히게).
fn glob_match(pat: &str, name: &str) -> bool {
    let p: Vec<char> = pat.to_lowercase().chars().collect();
    let n: Vec<char> = name.to_lowercase().chars().collect();
    // 고전적인 두 포인터 방식 — '*' 를 만나면 되돌아올 지점을 기억해 둔다.
    let (mut pi, mut ni) = (0usize, 0usize);
    let (mut star, mut mark) = (None, 0usize);
    while ni < n.len() {
        if pi < p.len() && (p[pi] == '?' || p[pi] == n[ni]) {
            pi += 1; ni += 1;
        } else if pi < p.len() && p[pi] == '*' {
            star = Some(pi); mark = ni; pi += 1;
        } else if let Some(s) = star {
            // 직전 '*' 가 한 글자 더 먹도록 되돌린다.
            pi = s + 1; mark += 1; ni = mark;
        } else {
            return false;
        }
    }
    while pi < p.len() && p[pi] == '*' { pi += 1; }
    pi == p.len()
}

//------------------------------------------------------------------
// 추출 모드 정하기 — 자체 파서(기본) · 사이냅 폴백 · 사이냅만
//=> 설계: plan/Rust-사이냅추출기-옵션-설계-20261019.html §3·§6
//   기본은 지금처럼 자체 파서만 쓴다. 사이냅(snf_exe)은 명시할 때만 쓴다 — 옆에 있다고
//   알아서 쓰면 같은 실행이 서버마다 다른 추출기로 돌아 본문 · 등급이 갈린다.
//    1) 인자(--synap-fallback · --synap-only)가 있으면 그것, 없으면 환경변수
//       CSOCLASSIFY_SYNAP_MODE(off · fallback · only)
//    2) 두 인자를 함께 주면 1006 mode_conflict
//    3) 사이냅 모드면 snf_exe 를 찾는다 — 없거나 실행할 수 없으면 문서를 한 건도 읽기
//       전에 1016 synap_missing 으로 멈춘다(옵션이 무시된 채 그럴듯한 결과가 나오지 않게)
//    4) 정한 모드를 일반 로그에 한 줄 남긴다(어느 추출기로 돌았는지 나중에 안다)
//
// -in: opts = 파싱된 실행 옵션
//
// -out: 없음(extract::set_mode 로 이번 실행의 모드를 새긴다)
// -out: error = 1006 mode_conflict · 1016 synap_missing · 1003 bad_args(환경변수 값이 틀림)로 종료(코드 3)
//------------------------------------------------------------------
fn handle_extractor_args(opts: &Opts) {
    if opts.synap_fallback && opts.synap_only {
        errcodes::fail("mode_conflict",
            "[MpowerClassify-rs] --synap-fallback 과 --synap-only 는 함께 쓸 수 없습니다 — 하나만 고르세요.", None);
    }
    // 인자가 환경변수를 이긴다. 인자가 없을 때만 운영용 환경변수를 본다.
    let want = if opts.synap_only {
        "only".to_string()
    } else if opts.synap_fallback {
        "fallback".to_string()
    } else {
        std::env::var("CSOCLASSIFY_SYNAP_MODE").unwrap_or_default().trim().to_ascii_lowercase()
    };
    let mode = match want.as_str() {
        "" | "off" => {
            // --synap-path 만 주고 모드를 안 고르면 쓰이지 않는다 — 조용히 넘기지 않고 알린다.
            if opts.synap_path.is_some() {
                note!("[MpowerClassify-rs] --synap-path 는 --synap-fallback 또는 --synap-only 와 함께 쓸 때만 쓰입니다(지금은 자체 파서만 씁니다).");
            }
            extract::ExtractMode::Builtin
        }
        "fallback" | "only" => {
            let snf = match synap::find_snf(opts.synap_path.as_deref()) {
                Ok(p) => p,
                Err(why) => errcodes::fail("synap_missing",
                    &format!("[MpowerClassify-rs] {}", why), opts.synap_path.as_deref()),
            };
            if want == "only" { extract::ExtractMode::SynapOnly(snf) } else { extract::ExtractMode::SynapFallback(snf) }
        }
        other => errcodes::fail("bad_args",
            &format!("[MpowerClassify-rs] CSOCLASSIFY_SYNAP_MODE 값이 올바르지 않습니다: {} (off · fallback · only)", other), None),
    };
    match &mode {
        extract::ExtractMode::Builtin => linfo!("실행 추출기={}", mode.name()),
        extract::ExtractMode::SynapFallback(p) | extract::ExtractMode::SynapOnly(p) =>
            linfo!("실행 추출기={} snf={}", mode.name(), p.display()),
    }
    extract::set_mode(mode);
}

//------------------------------------------------------------------
// 등급 스트림 인자 검사 — 문서를 읽기 전에 끝낸다
//=> 설계: plan/등급스트림-기록-옵션-설계-20261019.html §03 · §08. 파이썬 판 check_grade_stream_args 와 같다.
//    1) 보조 옵션만 주면 1003 (켜는 옵션 없이 무시된 채 도는 일을 막는다)
//    2) --grade-stream-unclassified 값은 skip · none 만 — 그 밖이면 1003
//    3) 분류 결과를 내지 않는 모드(--text-only · --embed · --seed-add · 데몬 명령 ·
//       --build-doc-rule · --suggest-terms)와 함께면 1006
//    4) Windows 가 아니면(ADS 없음) 1012
//
// -in: opts = 해석된 인자
//
// -out: 없음(문제가 없으면 돌아온다)
// -out: error = 위 조건이면 errcodes::fail 로 종료(코드 3)
//------------------------------------------------------------------
fn check_grade_stream_args(opts: &Opts) {
    if !opts.write_grade_stream {
        if opts.grade_stream_unclassified.is_some() || opts.grade_stream_update_time {
            errcodes::fail("bad_args",
                "[MpowerClassify-rs] --grade-stream-unclassified · --grade-stream-update-time 은 --write-grade-stream 과 함께 줘야 합니다.", None);
        }
        return;
    }
    if let Some(v) = &opts.grade_stream_unclassified {
        if v != "skip" && v != "none" {
            errcodes::fail("bad_args",
                &format!("[MpowerClassify-rs] --grade-stream-unclassified 값은 skip 또는 none 입니다: {:?}", v), None);
        }
    }
    // 분류 결과가 없는 모드에서는 쓸 등급이 없다 — 조용히 무시하지 않고 멈춘다
    let modes = [
        ("--text-only", opts.text_only),
        ("--embed", opts.embed_flag),
        ("--seed-add", !opts.seed.add.is_empty() || opts.seed.add_from.is_some()),
        ("--serve", opts.serve), ("--status", opts.status), ("--stop", opts.stop),
        ("--build-doc-rule", opts.build_doc_rule),
        ("--seed-fill-title", opts.seed_fill_title),
        ("--suggest-terms", opts.suggest_terms.is_some()),
    ];
    for (name, used) in modes {
        if used {
            errcodes::fail("mode_conflict", &format!(
                "[MpowerClassify-rs] --write-grade-stream 은 {} 와 함께 쓸 수 없습니다 — 분류 결과를 내는 실행에서만 등급을 씁니다.", name), None);
        }
    }
    if !gradestream::supported() {
        errcodes::fail("unsupported_option",
            "[MpowerClassify-rs] --write-grade-stream 은 Windows(NTFS)에서만 쓸 수 있습니다 — 이 플랫폼에는 대체 데이터 스트림이 없습니다.", None);
    }
}

//------------------------------------------------------------------
// 사람 등급 지정 인자 검사 — 문서를 읽기 전에 끝낸다
//=> 설계: plan/사람등급지정-수동오버라이드-설계-20261020.html §08. 파이썬 판 check_override_args 와 같다.
//    1) --set-grade 값은 C · S · O · NONE 만 — 그 밖이면 1003
//    2) --set-grade 는 --file 하나에만. --dir 이면 1006, 대상이 없으면 1003, 분류 결과가 없는 모드와 함께면 1006
//    3) 결정을 목록에 덧붙이려면(--grade-overrides) --reviewer · --reason 필수(1003)
//    4) --reviewer · --reason 만 따로 주면 1003
//    5) 분류에 --grade-overrides 를 주면 그 파일이 있어야 한다 — 없으면 2009
//
// -in: opts = 해석된 인자
//
// -out: 없음(문제가 없으면 돌아온다)
// -out: error = 위 조건이면 errcodes::fail 로 종료
//------------------------------------------------------------------
fn check_override_args(opts: &Opts) {
    let filled = |v: &Option<String>| v.as_deref().map_or(false, |s| !s.trim().is_empty());
    if let Some(sg) = &opts.set_grade {
        if !matches!(sg.trim().to_ascii_uppercase().as_str(), "C" | "S" | "O" | "NONE") {
            errcodes::fail("bad_args", &format!("[MpowerClassify-rs] --set-grade 값은 C · S · O · NONE 입니다: {:?}", sg), None);
        }
        if opts.dir.is_some() {
            errcodes::fail("mode_conflict",
                "[MpowerClassify-rs] --set-grade 는 --dir 과 함께 쓸 수 없습니다 — 폴더 전체를 한 등급으로 정하는 실수를 막으려는 것입니다. 문서는 --file 로 하나씩 주세요.", None);
        }
        let modes = [
            ("--text-only", opts.text_only), ("--embed", opts.embed_flag),
            ("--seed-add", !opts.seed.add.is_empty() || opts.seed.add_from.is_some()),
            ("--serve", opts.serve), ("--status", opts.status), ("--stop", opts.stop),
        ];
        for (name, used) in modes {
            if used {
                errcodes::fail("mode_conflict", &format!("[MpowerClassify-rs] --set-grade 는 {} 와 함께 쓸 수 없습니다.", name), None);
            }
        }
        if opts.file.is_none() {
            errcodes::fail("bad_args", "[MpowerClassify-rs] --set-grade 는 --file 로 대상 문서를 줘야 합니다.", None);
        }
        if opts.grade_overrides.is_some() && !(filled(&opts.reviewer) && filled(&opts.reason)) {
            errcodes::fail("bad_args",
                "[MpowerClassify-rs] --set-grade 결정을 --grade-overrides 에 덧붙이려면 --reviewer 와 --reason 을 함께 줘야 합니다(감사 기록).", None);
        }
        return;
    }
    if opts.reviewer.is_some() || opts.reason.is_some() {
        errcodes::fail("bad_args",
            "[MpowerClassify-rs] --reviewer · --reason 은 --set-grade 와 --grade-overrides 를 함께 줄 때만 쓰입니다.", None);
    }
    if let Some(ov) = &opts.grade_overrides {
        if !Path::new(ov).is_file() {
            errcodes::fail("grade_overrides_missing", &format!("[MpowerClassify-rs] 사람 결정 목록 파일이 없습니다: {}", ov), Some(ov));
        }
    }
}

//------------------------------------------------------------------
// --set-grade — 문서 하나에 등급을 바로 정한다(파이썬 run_set_grade 와 같다)
//=> 문서를 읽거나 판정하지 않는다. 지문 · 결과 레코드 · (선택) 파일 스트림 · (선택) 결정 목록.
//
// -in: opts = 해석된 인자
//
// -out: 없음(끝나면 프로세스를 끝낸다)
// -out: error = 대상 없음 3004 · 스트림 실패 4002 · 목록 쓰기 실패 2010 — 종료코드로 알린다
//------------------------------------------------------------------
fn run_set_grade(opts: &Opts) -> ! {
    let path = opts.file.clone().unwrap_or_default();
    let grade = gradeoverride::norm_grade(Some(&json!(opts.set_grade.clone().unwrap_or_default())));
    let label = grade.clone().unwrap_or_else(|| "NONE".to_string());
    let mut code = 0;
    let mut rec;
    if !Path::new(&path).is_file() {
        rec = json!({"file": path, "grade": Value::Null,
                     "error": {"kind": "file_missing", "reason": "file_missing", "message": "파일이 없습니다"},
                     "meta": {"ts": now_iso()}});
        errcodes::set_status("file_missing", &format!("[MpowerClassify-rs] 파일이 없습니다: {}", path));
        errcodes::set_counts(1, 1);
        code = errcodes::exit_of("file_missing");
    } else {
        let doc_hash = file_hash(Path::new(&path));
        let info = json!({"reviewer": opts.reviewer, "reason": opts.reason, "ts": now_iso(),
                          "matched_by": "set-grade", "kind": "change", "applied": true});
        rec = json!({"file": path, "hash": doc_hash, "grade": grade,
                     "why": {"security": {"method": "manual_override", "decided_by": ["reviewer"], "override": info}},
                     "meta": {"ts": now_iso()}});
        // 결정 목록에 남길 '이전 등급' — 지금 파일에 써 있는 값(모르면 None)
        let mut old: Option<String> = None;
        if gradestream::supported() {
            let (st, cur, _) = gradestream::read_state(&path);
            if matches!(st, gradestream::State::Marked) {
                old = cur.map(|c| gradestream::code_grade(c).to_string());
            }
        }
        if opts.write_grade_stream {
            let res = gradestream::mark(&path, &label, !opts.grade_stream_update_time);
            linfo!("등급스트림 state={} file={} value={}", res["state"].as_str().unwrap_or(""), path, label);
            if res["state"] == "failed" {
                errcodes::set_status("grade_stream_write_failed", &format!(
                    "[MpowerClassify-rs] 등급 스트림을 쓰지 못했습니다: {}", res["reason"].as_str().unwrap_or("")));
                code = errcodes::exit_of("grade_stream_write_failed");
            }
            rec["grade_stream"] = res;
        }
        if let Some(ov) = &opts.grade_overrides {
            if let Err(e) = gradeoverride::append_entry(ov, &path, old.as_deref(), grade.as_deref(),
                    opts.reason.as_deref().unwrap_or(""), opts.reviewer.as_deref().unwrap_or(""), doc_hash.as_deref()) {
                errcodes::set_status("grade_overrides_write_failed",
                    &format!("[MpowerClassify-rs] 사람 결정 목록에 쓰지 못했습니다: {} ({})", ov, e));
                code = errcodes::exit_of("grade_overrides_write_failed");
            }
        }
        linfo!("사람등급 지정 file={} grade={} 목록={}", path, label, opts.grade_overrides.as_deref().unwrap_or("-"));
        errcodes::set_counts(1, 0);
    }
    // 출력 — --simple 이면 문서명 · 해시 · 등급(파이썬 _simple_record 와 같은 칸)
    let out_rec = if opts.simple {
        let mut m = serde_json::Map::new();
        m.insert("file".into(), rec["file"].clone());
        m.insert("hash".into(), rec.get("hash").cloned().unwrap_or(Value::Null));
        m.insert("grade".into(), rec["grade"].clone());
        if let Some(e) = rec.get("error") {
            m.insert("error".into(), e.get("reason").cloned().unwrap_or(Value::Null));
        }
        Value::Object(m)
    } else { order_record(&rec) };
    let mut out: Vec<Value> = vec![out_rec];
    if errcodes::quiet() && opts.out.is_some() {
        out.push(errcodes::status_object(code, Some(&path)));
    }
    // 상태 줄을 결과 파일에 함께 넣을 때만 json 이 배열이 된다(파이썬 판과 같다)
    let body = if opts.fmt == "jsonl" {
        out.iter().map(|r| serde_json::to_string(r).unwrap() + "\n").collect::<String>()
    } else if out.len() == 1 {
        format!("{}\n", serde_json::to_string_pretty(&out[0]).unwrap())
    } else {
        format!("{}\n", serde_json::to_string_pretty(&out).unwrap())
    };
    match &opts.out {
        Some(p) => {
            if let Some(parent) = Path::new(p).parent() {
                if !parent.as_os_str().is_empty() { let _ = std::fs::create_dir_all(parent); }
            }
            if let Err(e) = std::fs::write(p, &body) {
                errcodes::fail("output_write_failed", &format!("[MpowerClassify-rs] 출력 저장 실패 {}: {}", p, e), Some(p));
            }
        }
        None => print!("{}", body),
    }
    let stream_note = rec.get("grade_stream").map(|g| format!(" (파일 스트림 {})", g["state"].as_str().unwrap_or(""))).unwrap_or_default();
    let list_note = opts.grade_overrides.as_ref().map(|o| format!(" · 결정 목록 {}", o)).unwrap_or_default();
    note!("[MpowerClassify-rs] 사람 등급 지정: {} → {}{}{}", path, label, stream_note, list_note);
    std::process::exit(errcodes::finish(code, Some(&path)));
}

//------------------------------------------------------------------
// 상주 서버 관련 요청을 처리한다 (--serve / --status / --stop)
//=> 임베딩 모델을 물고 있는 상주 서버를 띄우거나, 상태를 묻거나, 내린다.
//   셋 다 문서를 분류하지 않으므로 여기서 처리하고 프로세스를 끝낸다.
//    · --serve  : 이 프로세스가 서버가 된다. 정지 요청·유휴 종료 때까지 돌아오지 않는다
//    · --status : 서버 상태를 알리고 0
//    · --stop   : 정지 요청을 보내고(없으면 없다고 알리고) 0
//   --daemon 은 여기서 다루지 않는다 — 분류 흐름의 임베딩 단계가 직접 본다.
//
//   [출력은 언제나 JSON 한 줄이다] 이 세 명령은 연동 프로그램이 불러 결과를 읽는다.
//   그래서 --json-errors 를 안 줘도 분류 실행의 상태 줄과 같은 모양으로 낸다
//   (daemon::report 참고): {"error":{"code":0,"kind":"success","message":…},"daemon":{…}}
//   실패도 같은 모양(code≠0)으로 나가도록 여기서 JSON 오류 출력을 켠다.
//   사람이 읽던 문구는 error.message 에 그대로 들어 있다.
//
// -in: opts = 파싱된 실행 옵션
//
// -out: 없음 (세 요청 중 하나면 여기서 프로세스가 끝난다)
// -out: error = --serve 기동 실패는 daemon::server::run 이 internal_error/model_load_failed 로 종료
//------------------------------------------------------------------
fn handle_daemon_args(opts: &Opts) {
    if !(opts.serve || opts.status || opts.stop) {
        return;
    }
    let spec = embed::current();
    // 실패(모델을 못 올림 등)도 stdout 에 JSON 한 줄로 나가게 한다 — 성공만 JSON 이면
    // 받는 쪽이 실패를 따로(stderr 문장으로) 읽어야 한다.
    errcodes::enable_json(true);
    if opts.serve {
        // 준비되면 run 안에서 한 줄을 낸다. 끝날 때(정지·유휴)는 더 내지 않는다 —
        // 그때는 읽을 쪽이 이미 떠났고, 종료코드 0 이 "정상으로 끝났다"를 말해 준다.
        std::process::exit(daemon::server::run(spec, opts.idle_timeout));
    }
    if opts.status {
        match daemon::client::status(spec.key) {
            Some((rec, ready)) => {
                // 사람이 읽는 줄의 유휴시간 표시: 0/없음 = 무한(자동종료 없음) — 파이썬 판과 같은 표기.
                let idle = match rec.get("idle_timeout").and_then(Value::as_u64) {
                    Some(n) if n > 0 => format!("{}s", n),
                    _ => "무한(자동종료 없음)".to_string(),
                };
                let msg = format!("model={} pid={} port={} version={} idle_timeout={} ready={}",
                                  spec.key, rec["pid"], rec["port"],
                                  rec["version"].as_str().unwrap_or(""), idle, ready);
                daemon::report(&msg, daemon::info(&rec, ready));
            }
            // 서버가 없다는 것도 '정상으로 얻은 답'이다 — code 는 0 이고 running 이 false 다.
            None => daemon::report(&format!("model={}: 실행 중인 데몬 없음", spec.key),
                                   json!({"running": false, "model": spec.key})),
        }
        std::process::exit(0);
    }
    if opts.stop {
        let stopped = daemon::client::stop(spec.key);
        // stopped=false 는 "내릴 서버가 없었다" — 원하는 끝 상태(서버 없음)는 이미 참이라 성공이다.
        // running 칸은 넣지 않는다: 정지 '요청'을 보낸 직후라 서버가 아직 내려가는 중일 수 있다.
        daemon::report(if stopped { "데몬 정지 요청 전송" } else { "정지할 데몬 없음" },
                       json!({"model": spec.key, "stopped": stopped}));
        std::process::exit(0);
    }
}

/// 기본 seed 저장소 경로: CSOCLASSIFY_POLICY_DIR/class_seed.jsonl → exe 옆 class_seed.jsonl.
fn default_seed_path() -> Option<String> {
    if let Ok(dir) = std::env::var("CSOCLASSIFY_POLICY_DIR") {
        let p = Path::new(&dir).join("class_seed.jsonl");
        return Some(p.to_string_lossy().into_owned());
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            return Some(dir.join("class_seed.jsonl").to_string_lossy().into_owned());
        }
    }
    None
}

//------------------------------------------------------------------
// 기준 문서 등록 마무리 — 레코드를 기준 문서 줄로 바꿔 쓴다
//=> 설계서 8장 ④~⑧. 분류가 이미 지문과 벡터를 만들어 뒀으므로, 여기서는
//   사람이 정한 값을 얹어 저장하거나 stdout 으로 낸다. 파이썬 판
//   cli.py::_finish_seed_add 와 같은 차례·같은 문구여야 한다 — 두 판이 같은
//   파일에 쓰므로 한쪽만 달라지면 아무도 눈치채지 못한 채 모양이 갈린다.
//    1) --seeds 를 주면 잠근 채로 읽고 병합, 안 주면 stdout(파일 안 건드림)
//    2) 축마다 감사 기록을 남긴다 — CLI 로 넣은 것만 이력이 없으면 감사 구멍이다
//    3) 부분 실패를 종료코드로 구분한다 — 받는 쪽이 줄 수만 세면 못 알아챈다
//
// -in: opts    = 실행 인자
// -in: plan    = 등록 계획
// -in: records = 분류가 만든 결과 레코드(file·hash·vector·doc_id 를 본다)
//
// -out: i32 = 0 전부 등록 · 1 일부 실패 · 2 한 건도 못 함
// -out: error = 파일을 못 쓰면 output_write_failed 로 끝낸다(exit 1)
//------------------------------------------------------------------
fn finish_seed_add(opts: &Opts, plan: &[seedcli::Plan], records: &[Value],
                   titles: &std::collections::HashMap<String, String>) -> i32 {
    // 레코드를 경로로 찾을 수 있게 정규화 키로 색인한다(표기만 다른 같은 문서 대비).
    let mut by_file: std::collections::HashMap<String, &Value> = Default::default();
    for r in records {
        if let Some(f) = r.get("file").and_then(|v| v.as_str()) {
            by_file.insert(seedstore::norm_file(f), r);
        }
    }

    // 파일 모드면 잠근 채로 읽는다 — 읽고 나서 남이 쓴 뒤 우리가 덮으면
    // 그 등록이 아무 말 없이 사라진다.
    let _lock = match &opts.seeds {
        Some(p) => match seedstore::SeedLock::acquire(p) {
            Ok(l) => Some(l),
            Err(e) => {
                errcodes::fail("output_write_failed",
                    &format!("[MpowerClassify-rs] {}", e), opts.seeds.as_deref());
                unreachable!()
            }
        },
        None => None,
    };

    let mut rows: Vec<Value> = match &opts.seeds {
        Some(p) => seedstore::load(p).0,
        None => vec![],
    };
    // 감사 기록의 before 에 넣을 '고치기 전 값'을 미리 떠 둔다.
    let mut was: std::collections::HashMap<String, (Value, Value)> = Default::default();
    for p in plan {
        let k = seedstore::norm_file(&p.file);
        let cur = rows.iter().find(|r| {
            r.get("file").and_then(|v| v.as_str()).map(|f| seedstore::norm_file(f))
                .as_deref() == Some(&k)
        });
        let (g, d) = match cur {
            Some(c) => (c.get("grade").cloned().unwrap_or(Value::Null),
                        c.get("doctype").cloned().unwrap_or(Value::Null)),
            None => (Value::Null, Value::Null),
        };
        was.insert(k, (g, d));
    }

    let mut added: Vec<&seedcli::Plan> = vec![];
    let mut failed: Vec<(String, String)> = vec![];
    for p in plan {
        let Some(rec) = by_file.get(&seedstore::norm_file(&p.file)) else {
            failed.push((p.file.clone(), "문서를 읽지 못했습니다".into()));
            continue;
        };
        // 벡터 없는 기준 문서는 비교 대상이 못 된다 — 저장할 이유가 없다(설계 Q3).
        let vec: Option<Vec<f32>> = rec.get("vector").and_then(|v| v.as_array())
            .map(|a| a.iter().filter_map(|x| x.as_f64().map(|f| f as f32)).collect());
        let Some(vec) = vec.filter(|v: &Vec<f32>| !v.is_empty()) else {
            let why = rec.get("error").and_then(|e| e.get("kind")).and_then(|v| v.as_str())
                .unwrap_or("벡터를 얻지 못했습니다").to_string();
            failed.push((p.file.clone(), why));
            continue;
        };
        let hash = rec.get("hash").and_then(|v| v.as_str());

        if let Some(g) = &p.grade {
            seedstore::set_axis(&mut rows, &p.file, "security", json!(g),
                                &p.reviewer, seedcli::SOURCE, &p.note, Some(&vec), hash);
        }
        if !p.doctype.is_empty() {
            seedstore::set_axis(&mut rows, &p.file, "doctype", json!(p.doctype),
                                &p.reviewer, seedcli::SOURCE, &p.note, Some(&vec), hash);
        }
        // BM25 2차가 비교할 제목(첫 줄)도 함께 남긴다 — 본문은 남기지 않는다(설계서 업무분류-2차-BM25 §09).
        if let Some(t) = titles.get(&seedstore::norm_file(&p.file)).filter(|t| !t.is_empty()) {
            let k = seedstore::norm_file(&p.file);
            for r in rows.iter_mut() {
                let same = r.get("file").and_then(|v| v.as_str())
                    .map(|f| seedstore::norm_file(f)).as_deref() == Some(&k);
                if same {
                    if let Some(m) = r.as_object_mut() { m.insert("title".into(), json!(t)); }
                }
            }
        }
        // 문서 ID — 목록이 준 값이 먼저다(웹이 아는 진짜 신분증).
        if let Some(id) = seedcli::pick_doc_id(p, rec) {
            let k = seedstore::norm_file(&p.file);
            for r in rows.iter_mut() {
                let same = r.get("file").and_then(|v| v.as_str())
                    .map(|f| seedstore::norm_file(f)).as_deref() == Some(&k);
                if same {
                    if let Some(m) = r.as_object_mut() { m.insert("doc_id".into(), json!(id)); }
                }
            }
        }
        added.push(p);
    }

    // stdout 모드 — 등록될 줄만 내보낸다(파일은 건드리지 않는다).
    let mut total = 0usize;
    match &opts.seeds {
        Some(p) => match seedstore::save(p, &rows) {
            Ok(n) => total = n,
            Err(e) => {
                errcodes::fail("output_write_failed",
                    &format!("[MpowerClassify-rs] 기준 문서를 쓰지 못했습니다: {} :: {}", p, e),
                    Some(p.as_str()));
                unreachable!()
            }
        },
        None => {
            let done: Vec<String> = added.iter()
                .map(|p| seedstore::norm_file(&p.file)).collect();
            for r in &rows {
                let k = r.get("file").and_then(|v| v.as_str())
                    .map(|f| seedstore::norm_file(f)).unwrap_or_default();
                if done.contains(&k) { println!("{}", seedcli::to_line(r)); }
            }
        }
    }

    // 감사 기록 — 파일 모드에서만. stdout 모드는 파일을 안 만지는 모드다.
    let audit = seedcli::audit_path(&opts.seed, opts.seeds.as_ref());
    if opts.seeds.is_some() {
        if let Some(ap) = &audit {
            for p in &added {
                let (bg, bd) = was.get(&seedstore::norm_file(&p.file))
                    .cloned().unwrap_or((Value::Null, Value::Null));
                for (axis, before) in [("security", bg), ("doctype", bd)] {
                    let after = seedcli::after_value(axis, p);
                    if after.is_null() { continue; }
                    if axis == "doctype" && p.doctype.is_empty() { continue; }
                    let action = if before.is_null() { "add" } else { "update" };
                    if let Err(e) = seedstore::append_audit(
                        ap, action, &p.file, axis, before, after, &p.reviewer,
                        "CLI --seed-add") {
                        // 씨앗은 이미 저장됐다 — 되돌리지도, 조용히 넘기지도 않는다.
                        note!("[seed-add] 변경 기록을 남기지 못했습니다: {} :: {}", ap, e);
                        break;
                    }
                }
            }
        }
    }

    note!("{}", seedcli::summary(added.len(), failed.len(), opts.seeds.as_ref(), total));
    for (f, why) in &failed {
        note!("{}", seedcli::fail_line(f, why));
    }
    if opts.seeds.is_some() {
        if let Some(ap) = &audit { note!("[seed-add] 변경 기록: {}", ap); }
    }

    // 받는 쪽이 stdout 줄 수만 세면 "3건 넣었는데 2줄"을 못 알아챈다.
    // 0 과 1 이 다르다는 것을 종료코드로 못박는다(설계 P4).
    // 상태 줄의 건수·까닭은 '등록'을 기준으로 다시 적는다. 앞선 분류 단계의 건수를
    // 그대로 두면 "success 인데 failed=1" 같은 어긋난 줄이 나간다.
    errcodes::set_counts(plan.len() as i64, failed.len() as i64);
    let first_why = failed.first().map(|(_, w)| w.as_str()).unwrap_or("");
    if added.is_empty() {
        // 실패 사유는 '문서를 못 읽음'·'벡터를 못 얻음' — 기준으로 쓸 벡터를 못 만든
        // 것이라 embed_failed(3003)다. 역추적하면 model_load_failed 로 잘못 붙는다.
        errcodes::set_status("embed_failed",
            &format!("{}건 중 한 건도 등록하지 못했습니다({})", plan.len(), first_why));
        return 2;
    }
    if failed.is_empty() { return 0; }
    errcodes::set_status("extract_failed",
        &format!("{}건 중 {}건을 등록하지 못했습니다({})", plan.len(), failed.len(), first_why));
    1
}


fn file_hash(path: &Path) -> Option<String> {
    let bytes = std::fs::read(path).ok()?;
    let mut h = Sha256::new();
    h.update(&bytes);
    Some(format!("{:x}", h.finalize()))
}

//------------------------------------------------------------------
// seed 파일이 이번 벡터와 같은 차원인가 — 파이썬 cli._seed_dim_problem 과 같은 문구
//=> 다른 모델로 만든 seed 를 쓰면 비교가 성립하지 않는다. 이 판의 cosine 은 zip 으로
//   짧은 쪽에 맞춰 잘라 곱해, 오류 없이 엉뚱한 유사도를 냈다. 쓰기 전에 차원을 본다.
//
// -in: path = seed 파일 경로
// -in: dim  = 이번 실행 벡터의 차원
// -in: what = 안내에 쓸 '이번 벡터'의 설명(예: "모델 ko-sroberta(768차원)")
//
// -out: Option<String> = 맞지 않는 까닭(맞으면 None)
// -out: error = 없음
//------------------------------------------------------------------
fn seed_dim_problem(path: &str, dim: usize, what: &str) -> Option<String> {
    let dims = propagate::seed_file_dims(path);
    if dims.keys().all(|d| *d == dim) { return None; }
    let shown = dims.iter().map(|(d, n)| format!("{}차원 {}건", d, n)).collect::<Vec<_>>().join(" · ");
    Some(format!("기준 문서(seed) 벡터가 이번 벡터와 맞지 않습니다 — {}, seed 파일은 {}: {}\n  \
seed 를 만든 모델로 --model 을 맞추거나, 이 모델로 seed 를 다시 만드세요.", what, shown, path))
}

//------------------------------------------------------------------
// Ctrl+C(중단) 처리기 달기
//=> 예전에는 처리기가 없어 OS 가 프로세스를 그냥 끊었다. 그러면
//    · 종료코드가 윈도우에서 0xC000013A 로 나가 파이썬 판(130)과 달랐고
//    · 압축을 풀어 둔 임시폴더(cso_zip_<pid>·cso_txt_<pid>)가 **내부 원문째로
//      디스크에 남았다** — 파이썬 판은 finally 로 지우는데 이 판만 흘렸다.
//   그래서 중단 신호를 받으면 임시폴더를 지우고 130 으로 끝낸다.
//    1) 이 프로세스의 임시폴더 두 개를 지운다(없으면 조용히 넘어간다)
//    2) --json-errors 가 아니면 사람용 안내 한 줄
//    3) 130 으로 끝낸다 — 상태 줄은 내지 않는다(중단은 결과가 아니다. 파이썬 판과 같다)
//   오류 로그에는 남기지 않는다 — 사용자가 멈춘 것은 오류가 아니다(파이썬 판과 같다).
//
// -in: 없음
//
// -out: 없음
// -out: error = 없음(처리기를 못 달면 예전처럼 OS 기본 동작으로 둔다)
//------------------------------------------------------------------
fn install_interrupt_handler() {
    let _ = ctrlc::set_handler(|| {
        let pid = std::process::id();
        // 압축 확장 임시폴더 — 분류(cso_zip_)와 --text-only(cso_txt_) 두 곳이 만든다.
        for prefix in ["cso_zip_", "cso_txt_"] {
            let _ = std::fs::remove_dir_all(std::env::temp_dir().join(format!("{}{}", prefix, pid)));
        }
        // 상주 서버로 떠 있었다면 접속 기록·잠금도 치운다(서버가 아니면 아무 일도 안 한다).
        daemon::server::cleanup_on_interrupt();
        // 복호화해 둔 평문도 치운다(임시 폴더 위치가 --decrypt-tmp 로 바뀔 수 있어 모듈이 기억한다).
        decrypt::cleanup_on_exit();
        if !errcodes::quiet() {
            eprintln!("\n[MpowerClassify-rs] 사용자가 중단했습니다.");
        }
        std::process::exit(130);
    });
}

// -----------------------------------------------------------------
// 메인함수
// -----------------------------------------------------------------
fn main() {
    // 예상 못 한 내부 오류(패닉)도 파일에 남긴다 — 화면이 없는 환경(UI·배치)에서
    // 죽으면 원인을 알 방법이 사라진다. 가장 먼저 건다.
    errlog::install_panic_hook();
    // 패닉으로 죽을 때도 복호화해 둔 평문을 남기지 않는다(설계 §8). 기존 훅(로그 기록)은 그대로 부른다.
    {
        let prev = std::panic::take_hook();
        std::panic::set_hook(Box::new(move |info| {
            decrypt::cleanup_on_exit();
            prev(info);
        }));
    }
    install_interrupt_handler();

    // 히든: 임베딩 검증용(--emb-test "<텍스트>") → 384벡터 JSON 출력.
    let raw: Vec<String> = std::env::args_os()
        .skip(1)
        .map(|a| a.to_string_lossy().into_owned())
        .collect();
    if raw.first().map(|s| s == "--emb-test").unwrap_or(false) {
        let text = raw.get(1).cloned().unwrap_or_default();
        match embed::Embedder::load() {
            Some(mut e) => match e.embed_document(&text) {
                Some(v) => println!("{}", serde_json::to_string(&v).unwrap()),
                None => errcodes::fail("embed_failed",
                    "[MpowerClassify-rs] embed 실패(빈 텍스트?)", None),
            },
            None => errcodes::fail("model_load_failed",
                &format!("[MpowerClassify-rs] 모델/런타임 로드 실패({}/{}, onnxruntime.dll 확인)",
                         embed::MODEL_ROOT, embed::current().local_dir),
                None),
        }
        return;
    }

    // 인자 해석이 실패해도 JSON 으로 알려야 하므로, 파싱 전에 날것의 argv 를 훑는다.
    errcodes::enable_json(errcodes::wants_json(std::env::args()));
    // 상주 서버 명령(--serve/--status/--stop)은 언제나 JSON 한 줄로 답한다(handle_daemon_args).
    // 인자 해석 단계에서 실패해도(모르는 --model 등) 같은 모양이 되도록 여기서 미리 켠다 —
    // 성공은 JSON 인데 인자 오류만 문장이면, 받는 쪽이 두 가지 읽는 법을 가져야 한다.
    if raw.iter().any(|a| matches!(a.as_str(), "--serve" | "--status" | "--stop")) {
        errcodes::enable_json(true);
    }
    
    // [2026-09-01] 예전에는 인자 오류를 2(임베딩 실패)로 냈다. 받는 쪽이 그 2 를 보고
    // "모델이 없구나" 하고 재설치를 안내하면 실제 원인(경로 오타)과 전혀 다른 대응을
    // 하게 된다. 파이썬 판·실행가이드와 같은 3 으로 맞춘다.
    let mut opts = match parse_args() {
        Ok(o) => o,
        Err(e) => errcodes::fail("bad_args", &format!("[MpowerClassify-rs] {}", e), None),
    };
    // 일반 로그를 연다. parse_args 뒤에 두는 이유는 --log 경로를 알아야 하기
    // 때문이고, 그 앞의 인자 오류는 errlog(오류 로그)가 이미 받아 준다.
    log::init(opts.log.as_deref(), opts.verbose);
    // 파이썬 판이 남기는 첫 두 줄과 같은 내용이다 — 로그 파일만 받아도 무슨
    // 명령으로 돌렸는지 알 수 있어야 재현이 된다.
    // 열쇠(--mrk 뒤의 값)는 로그에 남기지 않는다 — **** 로 가려서 적는다(설계 §8).
    linfo!("실행 cmd={}", decrypt::mask_args(&std::env::args_os()
        .map(|a| a.to_string_lossy().into_owned())
        .collect::<Vec<_>>()).join(" "));
    linfo!("실행 argv={:?}", decrypt::mask_args(&raw));
    // --decrypt 조합이 틀렸으면 아무것도 읽기 전에 끝낸다.
    check_decrypt_args(&opts);

    // 크기 상한 CLI 덮어쓰기를 여기서 한 번만 새긴다 — 아래 모든 단계가
    // limits 모듈을 통해 이 값을 읽는다.
    apply_size_limits(&opts);
    linfo!("크기 상한 MAX_FILE_BYTES={} TEXT={} ARCHIVE={} MEMBERS={} TEXT_CHARS={} PDF_PAGES={} TIMEOUT={}s",
           limits::max_file_bytes(), limits::max_file_bytes_text(),
           limits::max_archive_bytes(), limits::max_archive_members(),
           limits::max_text_chars(), limits::max_pdf_pages(),
           limits::parser_timeout_secs());
    let t0 = Instant::now();

    // 데몬 관련 요청에 답한다 — 규칙셋을 읽기 전에 해야 한다(--status 는 정책
    // 파일과 무관한 질의라, 규칙셋이 없다고 엉뚱한 곳에서 멈추면 안 된다).
    // 등급 스트림 옵션 조합이 틀렸거나 쓸 수 없는 플랫폼이면 읽기 전에 끝낸다(데몬 명령보다 먼저 —
    // --serve 와 함께 준 실수를 조용히 넘기지 않는다).
    check_grade_stream_args(&opts);
    // 사람 등급 지정 인자(--set-grade · --grade-overrides)도 읽기 전에 본다.
    check_override_args(&opts);
    // 결정 목록은 지문으로 잇고 내용 변경을 알아야 하므로 지문을 구한다.
    if opts.grade_overrides.is_some() && opts.set_grade.is_none() {
        opts.hash = true;
    }
    handle_daemon_args(&opts);
    handle_extractor_args(&opts);
    // --set-grade 는 문서를 읽지 않는 별도 모드다 — 규칙 · 모델을 올리기 전에 끝낸다.
    if opts.set_grade.is_some() {
        run_set_grade(&opts);
    }

    // 업무분류 규칙 자동 생성 모드 — 체계 JSON 과 정책 폴더만 있으면 된다. 규칙셋을
    // 먼저 읽는 아래 흐름을 타면 "cso_rule.yaml 이 없다"고 엉뚱한 곳에서 멈추고,
    // 만들려던 doc_rule.yaml 이 아직 옛 모양이라는 이유로 축 로드에서 또 걸린다.
    // 기준 문서 제목 채우기 — 기준 문서 파일과 본문(또는 원본)만 본다. 규칙셋을 읽기 전에 끝낸다.
    if opts.seed_fill_title {
        std::process::exit(errcodes::finish(run_seed_fill_title(&opts), None));
    }
    if opts.build_doc_rule {
        std::process::exit(errcodes::finish(run_build_doc_rule(&opts), None));
    }

    // 규칙 단어 제안 — 문서도 모델도 읽지 않는다. 규칙셋을 먼저 읽는 아래 흐름을
    // 타면 cso_rule.yaml 이 없다는 이유로 엉뚱하게 멈춘다(위 --build-doc-rule 과 같은 자리).
    if opts.suggest_terms.is_some() {
        std::process::exit(errcodes::finish(run_suggest_terms(&opts), None));
    }

    // 전파 전용 모드 — 입력이 1차 '레코드 파일'이라 문서도 규칙셋도 모델도 필요 없다.
    // 규칙셋을 먼저 읽는 아래 흐름을 타면 cso_rule.yaml 이 없다는 이유로 엉뚱하게
    // 멈추므로, 규칙셋 로드 전에 끝낸다(파이썬 cli.py 의 (1.5) 와 같은 자리).
    if opts.propagate.is_some() {
        std::process::exit(errcodes::finish(run_propagate(&opts),
                                            opts.propagate.as_deref()));
    }

    // 기준 문서 등록 모드 — 문서를 읽기 전에 인자를 전부 검사한다. 절반 읽고
    // 나서 인자가 틀린 것을 알면 이미 쓴 것과 안 쓴 것이 섞여 되돌리기 어렵다.
    let seed_plan: Vec<seedcli::Plan> = if opts.seed.is_on() {
        let has_class = opts.file.is_some() || opts.dir.is_some();
        match seedcli::build_plan(&opts.seed, has_class) {
            Ok(p) => p,
            Err((kind, msg)) => {
                errcodes::fail(kind, &format!("[MpowerClassify-rs] {}", msg), None);
                unreachable!()
            }
        }
    } else { vec![] };
    if !seed_plan.is_empty() {
        // 지문과 벡터는 이 모드에서 선택이 아니다 — 둘 다 없으면 기준으로 못 쓴다.
        // 지문이 없으면 원본이 바뀌어도 점검이 조용히 넘어가고(v3 는 hash 하나로만
        // 판정한다), 벡터가 없으면 비교 대상이 못 된다.
        opts.hash = true;
        opts.with_vector = true;
        // stdout 에는 등록된 기준 문서 줄만 나가야 한다(설계 P5).
        opts.summary_only = false;
        opts.no_summary = true;
    }

    // ── 본문만 뽑는 모드(--text-only) ──────────────────────────────
    // 규칙셋을 읽기 '전에' 갈라져 나간다. 분류를 안 할 거면 규칙 파일도, PII 검사도
    // 필요 없고, 없어야 이 모드가 '순수 파서'를 재는 자가 된다(있으면 규칙 읽는
    // 시간이 파서 시간으로 둔갑한다). 규칙셋이 아예 없는 자리에서도 돌아간다.
    if opts.text_only {
        // 끝을 상태 줄로 알린다 — 분류와 같은 계약(성공도 한 줄). 파이썬 판과 같다.
        let target = opts.file.clone().or_else(|| opts.dir.clone());
        std::process::exit(errcodes::finish(run_text_only(&opts), target.as_deref()));
    }

    let rules_path = match resolve_rules(&opts) {
        Some(p) => p,
        None => errcodes::fail("rules_missing",
            "[MpowerClassify-rs] 규칙셋(cso_rule.yaml)을 찾을 수 없습니다. --cso-rules 로 지정하세요.",
            opts.rules_path.as_deref()),
    };
    if !rules_path.is_file() {
        errcodes::fail("rules_missing",
            &format!("[MpowerClassify-rs] 규칙셋(cso_rule.yaml)을 찾을 수 없습니다: {}",
                     rules_path.display()),
            rules_path.to_str());
    }
    let rs: RuleSet = match load_rules(&rules_path) {
        Ok(rs) => rs,
        // 파일은 있는데 등급 값이 틀린 경우만 코드 4 로 나눠, 배치가 "파일 없음"과
        // "내용 오류"에 다르게 대응할 수 있게 한다(Python 판 EXIT_RULES_INVALID 와 동일).
        // 파일은 있는데 등급 값이 틀린 경우도, 읽다가 깨진 경우도 '내용 문제'(4)다.
        // 파일이 아예 없는 경우(3)와 갈라 두면 배치가 대응을 나눌 수 있다.
        Err(rules::RulesError::Invalid(msg)) => errcodes::fail("rules_invalid",
            &format!("[MpowerClassify-rs] {}", msg), rules_path.to_str()),
        Err(e) => errcodes::fail("rules_invalid",
            &format!("[MpowerClassify-rs] {}", e), rules_path.to_str()),
    };

    // 업무분류(doctype) 축 — 있으면 켜고 없으면 security 만(4-6). --axis doctype 이면 필수.
    let mut dt_axis = match load_doctype_axis(&opts) {
        Ok(v) => v,
        Err(code) => std::process::exit(code),
    };

    let doctype_strategy_override = apply_conflict_override(&opts, &mut dt_axis);

    // --doctype-vector-only : 업무분류 1차(규칙 스캔)를 통째로 건너뛴다. 규칙 '목록'만
    // 비우고 embed 임계값·conflict 전략·defaults 는 파일에 적힌 그대로 둔다 —
    // 빈 규칙셋(DocRuleSet::empty)으로 갈아치우면 관리자가 정해 둔 임계값까지 기본값으로
    // 되돌아가 조용히 무시된다. 1차가 빈손이 되면 그 뒤는 'doc_rule.yaml 이 없는 배포'와
    // 똑같은 길을 타므로(2차 전파가 라벨을 채운다) 새 분기를 만들 필요가 없다.
    if opts.doctype_vector_only {
        if let Some((_, drs)) = dt_axis.as_mut() {
            drs.rules.clear();
            note!("[MpowerClassify-rs] 업무분류: 규칙을 쓰지 않고 기준 문서(class_seed) 비교로만 분류합니다(--doctype-vector-only).");
        }
    }

    // 규칙셋 검사 모드: 여기까지 왔다는 것은 검증을 통과했다는 뜻 → 요약만 알리고 종료.
    // 문서·모델이 필요 없으므로 파일 수집 전에 끝낸다.
    if opts.check_rules {
        // 검사만 하는 모드도 끝을 알린다 — 부르는 쪽이 한 가지 방법으로만 읽게.
        println!("[MpowerClassify-rs] 규칙셋 정상: {}", rules_path.display());
        println!("  version={}  등급={}", rs.version, rules::GRADES.join("/"));
        println!("  regex_pii={} · pii_combos={} · keywords={} · sensitive={} · stamps={}",
                 rs.regex_rules.len(), rs.pii_combos.len(), rs.keyword_rules.len(),
                 rs.sensitive_rules.len(), rs.stamp_rules.len());
        match &dt_axis {
            Some((taxonomy, drs)) => {
                println!("[MpowerClassify-rs] 업무분류(doctype) 축 정상");
                println!("  분류체계 노드={} (사용중={}) · exported_at={}",
                         taxonomy.len(), taxonomy.active_nodes().len(), taxonomy.exported_at);
                // 'seed 전파 전용'(규칙 파일 없이 축을 켜던 모드)은 없앴다 — 규칙 파일이
                // 없으면 축이 꺼지므로 여기 오는 것은 언제나 자동 생성 규칙이다.
                println!("  doctype_rules={} (활성={}) · version={} · conflict={}",
                         drs.rules.len(), drs.active_rules().len(), drs.version, drs.conflict.strategy);
            }
            None => println!("[MpowerClassify-rs] 업무분류(doctype) 축은 이번 실행에서 꺼져 있습니다."),
        }
        // 예전에는 여기서 그냥 return 해 --json-errors 여도 상태 줄이 없었다.
        std::process::exit(errcodes::finish(0, None));
    }

    // 목록과 --file/--dir 을 같이 주면 어느 쪽이 진짜 대상인지 알 수 없다 —
    // 조용히 하나를 고르면 '왜 저 파일이 빠졌지?' 로 이어지므로 그 자리에서 막는다.
    if opts.files_from.is_some() && (opts.file.is_some() || opts.dir.is_some()) {
        errcodes::fail("bad_args",
            "[MpowerClassify-rs] --files-from 은 --file/--dir 과 함께 쓸 수 없습니다.", None);
    }

    // 입력 목록(--filelist) 로드 — 대상 수집보다 먼저 한다. 목록이 깨져 있으면
    // (F1·F3) 문서를 한 건도 읽기 전에 멈추는 것이 맞다. 절반쯤 잘못된 ID 가 붙은
    // 결과가 나가는 것이 최악이기 때문이다(규칙셋 검증과 같은 원칙).
    let flist: Option<filelist::FileList> = match opts.filelist.as_deref() {
        None => None,
        Some(lp) => match filelist::load(lp) {
            Ok(fl) => {
                // 주석이 있으면 몇 줄을 건너뛰었는지도 알려 준다 — "왜 3건만
                // 읽혔지?" 를 파일을 열어 보지 않고 알 수 있게.
                let skipped = if fl.comments > 0 {
                    format!(" · 주석 {}줄 건너뜀", fl.comments)
                } else {
                    String::new()
                };
                note!("[MpowerClassify-rs] 목록 {}: {}건 적재(데이터 {}줄{})",
                          Path::new(lp).file_name()
                              .map(|s| s.to_string_lossy().into_owned())
                              .unwrap_or_else(|| lp.to_string()),
                          fl.len(), fl.lines, skipped);
                for w in &fl.warnings {
                    note!("[MpowerClassify-rs] {}", w);
                }
                // 열쇠 칸(mrk·enc_mode·zip_mode)이 붙은 줄이 있으면 알린다 — 풀어서 읽는다는 것을
                // 모르고 돌리는 일이 없게. --no-decrypt 면 풀지 않는다는 것도 함께 말한다.
                if fl.dec_count() > 0 {
                    if opts.no_decrypt {
                        note!("[MpowerClassify-rs] 목록에 복호화 정보가 {}건 있지만 --no-decrypt 라 풀지 않고 그대로 읽습니다",
                              fl.dec_count());
                    } else {
                        note!("[MpowerClassify-rs] 복호화 정보가 있는 문서 {}건 — 한 건씩 풀어서 분류하고 바로 지웁니다",
                              fl.dec_count());
                    }
                }
                Some(fl)
            }
            // '파일이 없다'와 '내용이 잘못됐다'는 부르는 쪽의 대응이 다르므로 코드를 나눈다.
            Err(e) => {
                let kind = if Path::new(lp).is_file() { "filelist_invalid" } else { "filelist_missing" };
                errcodes::fail(kind, &format!("[MpowerClassify-rs] {}", e), None);
                unreachable!()
            }
        },
    };

    let files = if seed_plan.is_empty() {
        collect_files(&opts, flist.as_ref())
    } else {
        seed_plan.iter().map(|p| PathBuf::from(&p.file)).collect()
    };
    // 압축 확장 — 압축 1건을 내부 문서 N건으로 펼친다(archive.rs).
    // 임시폴더는 프로세스 단위로 하나 만들고, 처리가 끝나면 통째로 지운다.
    let arc_tmp = std::env::temp_dir().join(format!("cso_zip_{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&arc_tmp);
    let _ = std::fs::create_dir_all(&arc_tmp);
    let mut arc_stats = archive::ExpandStats::default();
    // 복호화할 문서를 먼저 갈라 둔다(설계 §9 '구조 변경'). 이 문서들은 여기서 펼치지 않는다 —
    // 지금 펼치려면 먼저 다 풀어야 하고, 그러면 평문 수천 건이 한꺼번에 디스크에 놓인다.
    // 아래 루프가 한 건씩 풀기 → (압축이면) 펼치기 → 분류 → 지우기로 처리한다.
    // 기준 문서 등록 모드(--seed-add)는 대상이 사람이 고른 평문이라 복호화하지 않는다.
    let mut dec_plans: std::collections::HashMap<String, DecPlan> = std::collections::HashMap::new();
    let (plain_paths, dec_paths): (Vec<PathBuf>, Vec<PathBuf>) = if seed_plan.is_empty() {
        files.into_iter().partition(|p| {
            // 라벨은 압축 확장(archive::expand_paths)이 만드는 것과 같은 표기로 맞춘다.
            let label = p.to_string_lossy().replace('\\', "/");
            match dec_plan_for(&opts, flist.as_ref(), &label) {
                Some(plan) => { dec_plans.insert(label, plan); false }
                None => true,
            }
        })
    } else {
        (files, vec![])
    };
    let mut files: Vec<archive::Job> = archive::expand_paths(&plain_paths, &arc_tmp, 3, &mut arc_stats);
    for p in &dec_paths {
        files.push(archive::Job {
            src: p.clone(),
            label: p.to_string_lossy().replace('\\', "/"),
            origin: None,
        });
    }
    if files.is_empty() {
        fail_no_input(&opts);
    }
    // 복호화 도구가 있는지 문서를 읽기 전에 본다. 없으면 대상 전부가 같은 이유로 실패한
    // 결과 파일이 만들어진다 — 그보다 한 건도 읽지 않고 끝내는 편이 낫다(설계 §5).
    // 둘 다 0 인(암호화 안 된) 문서뿐이면 도구를 부를 일이 없어 보지 않는다.
    let dec_cmd = decrypt::resolve_cmd(opts.decrypt_cmd.as_deref());
    if dec_plans.values().any(|p| matches!(&p.info, Ok(i) if !i.is_plain())) {
        if let Err(why) = decrypt::preflight(&dec_cmd) {
            let _ = std::fs::remove_dir_all(&arc_tmp);
            errcodes::fail("decrypt_cmd_missing", &format!("[MpowerClassify-rs] {}", why),
                           dec_cmd.to_str());
        }
    }
    // --out 을 쓸 수 있는지 문서를 읽기 전에 본다. 결과는 끝에서 한 번에 쓰므로, 예전에는
    // 경로가 틀려도 전체 스캔을 다 돌린 뒤에야 4001 로 실패했다(파이썬 판은 시작 전에 멈춘다).
    // append 로 열어 기존 파일을 자르지 않고, 확인하려고 새로 만든 파일이면 바로 치운다 —
    // 뒤에서 다른 이유로 멈췄을 때 빈 결과 파일이 남아 '결과가 있다'로 오인되지 않게.
    if let Some(p) = opts.out.as_deref() {
        // 상위 폴더가 없으면 만들어 둔다(예: D:\temp 가 없을 때). 못 만들면 아래 열기가
        // 실패하며 그 까닭을 알린다.
        if let Some(parent) = Path::new(p).parent() {
            if !parent.as_os_str().is_empty() { let _ = std::fs::create_dir_all(parent); }
        }
        let existed = Path::new(p).exists();
        match std::fs::OpenOptions::new().create(true).append(true).open(p) {
            Ok(_) => { if !existed { let _ = std::fs::remove_file(p); } }
            Err(e) => {
                let _ = std::fs::remove_dir_all(&arc_tmp);
                errcodes::fail("output_write_failed",
                    &format!("[MpowerClassify-rs] 출력 파일을 쓰지 못했습니다: {}\n  {}", p, e),
                    Some(p));
            }
        }
    }

    // 분류 모드 결정(ko-pii cli.py) — 배타: 벡터만 / 규칙만 / 기본(규칙+자동전파).
    let seeds_path: Option<String> = opts.seeds.clone().or_else(default_seed_path);
    let seed_exists = seeds_path.as_deref().map(|p| Path::new(p).is_file()).unwrap_or(false);
    let mut auto_prop = opts.auto_propagate;
    let (mut rules_enabled, embed_mode): (bool, &str) = if opts.vector_only {
        if !seed_exists {
            errcodes::fail("seeds_missing",
                "[MpowerClassify-rs] --vector-only 는 비교 기준 seed 파일이 필요합니다: --seeds <class_seed.jsonl>(또는 exe 옆)",
                opts.seeds.as_deref());
        }
        auto_prop = true;
        (false, "all")
    } else if opts.rule_only {
        auto_prop = false;
        (true, "none")
    } else {
        // 기본: seed 가 있으면 자동 전파 on(명시 --auto-propagate 없이도).
        if !auto_prop && seed_exists {
            auto_prop = true;
        } else if !auto_prop {
            // seed 가 없으면 전파(Signal B)가 통째로 꺼진다 — 규칙이 못 정한 문서는
            // 구제받지 못하고 등급 없이(null) 나간다. 예전에는 두 판 모두 이걸 아무
            // 말 없이 넘어갔고, 배포 폴더에 파일이 빠진 것을 아무도 몰랐다(실측:
            // Python 배포본에 class_seed.jsonl 이 없어 보류 21건이 미분류로 나갔다 —
            // 부록 C-5). 파일이 없는 것 자체는 정상 배포일 수도 있으므로(규칙만 쓰는
            // 배포) 오류로 막지는 않고, '무엇이 꺼졌는지'만 분명히 알린다.
            // 문구는 cli.py 와 같게 맞춘다 — 두 판을 같은 눈으로 보게 하려는 것이다.
            let shown = seeds_path.clone().unwrap_or_else(|| "class_seed.jsonl".into());
            note!("[MpowerClassify-rs] 전파용 seed 파일이 없어 자동 전파를 건너뜁니다: {}\n\
                       \x20               규칙이 못 정한 문서는 미분류로 남습니다. \
                      의도한 것이면 --rule-only 로 명시하세요.", shown);
        }
        // 임베딩 범위: --embed-needed(명시) > --with-vector 는 '전량'(레코드에 벡터를
        // 실어야 하므로 확정 문서도 빠뜨리면 안 된다) > 전파용 'needed'(못 정한 문서만) > 'none'.
        // 전파가 켜져 있어도 --with-vector 면 all 이 이긴다 — needed 로 내리면 등급이
        // 이미 확정된 문서에 벡터가 안 실려 "전량 벡터"라는 약속이 깨진다(cli.py 와 동일).
        //
        // [2026-09-02] --embed-needed 를 맨 앞에 둔다. 예전에는 이 인자를 Opts 에 받아만
        // 두고 아무도 읽지 않아, seed 파일이 없으면 'none' 으로 떨어져 **인자가 통째로
        // 무시**됐다(파이썬은 cli.py 의 `if args.embed_needed` 로 언제나 needed 다).
        // 부르는 쪽이 "못 정한 문서만 임베딩하라"고 명시했는데 조용히 아무것도 안 하면,
        // --serve 를 무시하던 것과 같은 종류의 함정이 된다.
        let m = if opts.embed_needed { "needed" }
                else if opts.with_vector { "all" }
                else if auto_prop { "needed" }
                else { "none" };
        (true, m)
    };

    // --axis doctype 이면 security 신호 자체를 계산하지 않는다(6-4) — PII 검출까지
    // 통째로 생략해, 가장 비싼 단계를 건너뛴 대량 업무분류 스캔이 크게 빨라진다.
    if opts.axis.as_deref() == Some("doctype") {
        rules_enabled = false;
    }

    // seed 를 실제로 쓰는 실행(전파·기준 문서 등록)이면, 문서를 읽기 전에 seed 벡터가
    // 이번 모델과 같은 차원인지 본다 — 다른 모델로 만든 seed 와는 비교가 성립하지 않는다
    // (파이썬 판 run_classify 와 같은 자리·같은 조건). 규칙만 쓰는 실행은 보지 않는다.
    if (auto_prop || !seed_plan.is_empty()) && seed_exists {
        if let Some(p) = seeds_path.as_deref() {
            let spec = embed::current();
            if let Some(why) = seed_dim_problem(p, spec.dim, &format!("모델 {}({}차원)", spec.key, spec.dim)) {
                let _ = std::fs::remove_dir_all(&arc_tmp);
                errcodes::fail("seeds_model_mismatch", &format!("[MpowerClassify-rs] {}", why), Some(p));
            }
        }
    }

    // 모델·런타임 배포 점검(값싼 확인) — 세션은 만들지 않고 파일 존재만 본다.
    //
    // [2026-09-02] 임베딩 모델을 '필요할 때만' 올리게 되면서, 규칙만으로 끝나는 배치는
    // 모델을 전혀 건드리지 않는다. 그러면 onnxruntime.dll·models/ 가 빠진 배포가
    // 조용히 성공하고, 한참 뒤 미분류가 나오는 배치를 만나서야 드러난다 —
    // "그때는 되고 지금은 안 되네" 는 거버넌스 도구에서 가장 나쁜 실패다.
    // 상주 서버를 안 쓰는 실행에서는 배포 이상을 알릴 자리가 여기밖에 없다
    // (--status 는 서버가 떠 있는지만 답하고, 이 프로세스의 모델 파일은 보지 않는다).
    //
    // 점검 결과를 '알리고 끝'이 아니라 뒤에서 실제로 쓴다. onnxruntime 을 못 찾으면
    // ort 는 PATH 에 있는 아무 onnxruntime.dll 이나 집어 들고, 버전이 다르면 Err 가
    // 아니라 **패닉**으로 죽는다(실측: "expected 1.22.x, but got 1.17.1", 종료코드 101).
    // Embedder::load() 가 None 을 준다는 전제가 거기서 깨지므로, 확인이 실패했으면
    // 아예 부르지 않는다 — 결과 파일도 못 내고 죽는 것보다 '전파만 못 한' 결과가 낫다.
    // 확인 실패 사유 — 실제로 임베딩이 필요했던 배치면 아래에서 종료코드 2(3002)로 올린다.
    let mut deploy_why: Option<String> = None;
    let deploy_ok = if embed_mode != "none" {
        match embed::check_deployment() {
            Ok(()) => true,
            // --daemon 이면 여기서 판단하지 않는다 — 이 프로세스에 모델이 없어도 살아 있는
            // 상주 서버가 벡터를 줄 수 있다. 서버까지 못 쓰는 것이 확인된 뒤(2차 임베딩
            // 단계)에 같은 기준으로 멈추거나 종료코드 2 로 알린다.
            Err(why) if opts.daemon => {
                deploy_why = Some(why);
                false
            }
            Err(why) => {
                // --vector-only 는 임베딩 비교만으로 등급을 정한다 — 모델이 없으면 전 문서가
                // 미분류가 되므로 결과를 내지 않고 여기서 멈춘다(아직 문서를 읽기 전이다).
                // 기본 모드는 규칙 등급이 유효하므로 결과를 내고 종료코드 2 로만 알린다.
                if opts.vector_only {
                    errcodes::fail("model_load_failed",
                        &format!("[MpowerClassify-rs] --vector-only 는 임베딩 모델이 있어야 합니다 — {}", why),
                        None);
                }
                errlog::err(&format!(
                    "[MpowerClassify-rs] 임베딩 모델/런타임 확인 실패 — {} \
→ 임베딩·전파를 건너뜁니다(규칙으로 정해진 등급은 그대로 나갑니다)", why));
                deploy_why = Some(why);
                false
            }
        }
    } else {
        false   // 임베딩을 안 하는 모드에서는 이 값이 쓰이지 않는다.
    };

    // [--simple 과 --with-pii 를 같이 준 경우] 축약본에는 pii 를 싣지 않는다.
    // --simple 은 연동용(문서중앙화) 형식이고, 이 도구의 불변식은 "매칭된 원문
    // PII 값은 결과에 저장하지 않는다" 이다 — 주민번호 원본이 연동 경로로
    // 흘러가지 않는 것이 옳은 기본값이다.
    //
    // 그런데 사용자는 --with-pii 를 **명시해서** 줬다. 아무 말 없이 무시하면
    // "줬으니 받았겠지" 라고 믿게 된다. 무엇이 왜 빠졌는지 말해 주지 않는 것이
    // 이 도구에서 가장 나쁜 실패다(--synap-only·데몬 인자와 같은 원칙).
    //
    // --out 이 있으면 감사용 <out>.full 에 그대로 남으므로 어디서 찾을지 알려
    // 주면 되고, --out 이 없으면 그 사이드카가 아예 안 만들어져 **PII 가 어디에도
    // 안 남는다** — 그때는 더 분명히 말한다. 파이썬 판과 같은 문구다.
    if opts.simple && opts.with_pii && !opts.summary_only {
        match &opts.out {
            Some(p) => {
                note!("[MpowerClassify-rs] --simple 축약본에는 pii 를 싣지 않습니다\
(연동 형식에 원문 PII 를 넣지 않는다는 규칙).");
                note!("                 검출된 원문 값은 감사용 전체 파일에 있습니다: {}",
                      full_out_path(p));
            }
            None => {
                note!("[MpowerClassify-rs] --with-pii 가 이번 실행에서는 아무 데도 남지 않습니다.");
                note!("                 --simple 축약본은 pii 를 싣지 않고, --out 이 없어 \
감사용 전체 파일도 만들지 않습니다.");
                note!("                 원문 값이 필요하면 --out 을 주거나(<out>.full 에 남습니다) \
--simple 을 빼세요.");
            }
        }
    }

    // --textsave 는 파일 이름을 해시로 삼으므로 해시가 반드시 있어야 한다.
    // 비용은 파일 1회 순차 읽기라 추출에 비하면 무시할 수준이다(설계 5장).
    let need_hash = opts.hash || opts.simple || opts.textsave.is_some();
    // 저장 폴더는 여기서 한 번 준비한다. 못 쓰면 문서를 한 건도 읽기 전에 끝낸다 —
    // 다 돌린 뒤 빈 폴더를 보게 하는 것이 가장 나쁘다(설계 9장).
    let text_save_dir: Option<std::path::PathBuf> = match &opts.textsave {
        None => None,
        Some(d) => match textsave::prepare(d) {
            Ok(p) => {
                // 기본 방어를 여는 문이다 — 조용히 열리면 안 된다(설계 10장 S2).
                note!("[MpowerClassify-rs] --textsave: 추출 본문(개인정보 포함 가능)을 \
                           {} 에 평문으로 남깁니다. 보안등급 C 문서도 그대로 남습니다.",
                          p.display());
                Some(p)
            }
            Err(e) => errcodes::fail(
                "bad_args",
                &format!("[MpowerClassify-rs] --textsave 폴더를 쓸 수 없습니다: {}", e),
                Some(d.as_str()),
            ),
        },
    };
    // 저장이 조용히 반쯤 실패하는 것을 막는 관측 칸(설계 8장).
    let mut n_text_saved = 0usize;
    let mut n_text_dedup = 0usize;
    let mut n_text_save_failed = 0usize;
    let mut fail = 0u32;
    // 본문에 쓸 만한 글자가 이보다 적으면 '읽을 글자가 없었다'로 본다(파이썬 판
    // config.MIN_TEXT_LEN 과 같은 값·같은 뜻). 스캔본(이미지) PDF 는 추출기가
    // 실패를 알리지 않고 장식기호 몇 개를 돌려주므로, 길이로 가리지 않으면
    // '미분류'(=읽었는데 신호 없음)와 구분되지 않는다.
    let min_text_len: usize = std::env::var("CSOCLASSIFY_MIN_TEXT_LEN")
        .ok().and_then(|v| v.parse().ok()).unwrap_or(20);
    // 글자수 상한(G3). 파일마다 다시 읽을 이유가 없어 루프 밖에서 한 번만 정한다.
    let text_limit = limits::max_text_chars();
    // 크기 상한(G1·G2)에 걸려 아예 읽지 않은 문서 수. 추출 실패와 따로 세야
    // "상한을 올리면 처리되는 건"과 "원본이 깨진 건"을 구분할 수 있다.
    let mut n_size_skipped = 0usize;
    // 글자수 상한에 걸려 '앞부분만 보고' 판정한 문서 수.
    let mut n_text_truncated = 0usize;
    // 파서가 상한에서 멈춰 끝까지 읽지 못한 문서 수(G5). 절단(G3)과 다른 사건이라
    // 따로 센다 — 재검토 시 손볼 상한값이 서로 다르다.
    let mut n_partial_extract = 0usize;
    // 스캔 PDF 라 '처리제외'로 가린 문서 수. 추출 실패와 따로 센다 — 실패가 아니라
    // OCR 미적용(1차 범위 밖)이라 종료코드에도 넣지 않는다(파이썬 판과 동일).
    let mut n_excluded = 0usize;
    // 분할 압축의 조각이라 읽지 않은 문서 수. 추출 실패와 따로 세야
    // '원본이 깨진 것'과 '조각이라 원래 못 읽는 것'을 구분할 수 있다.
    let mut n_split_volume = 0usize;
    // 해제 상한(G4)에 걸려 못 펼친 압축의 경로 → 사유. 그 압축 레코드에 표식을
    // 달아야 한다 — 요약 숫자만 있으면 '어느 압축이' 빠졌는지 결과에서 알 수 없다.
    // 경로 → (사유, 종류). 종류는 "limit"(상한 초과) 또는 "error"(손상·미지원 등).
    // 둘을 섞으면 안내가 엇나간다 — 손상 압축에 "상한을 올리세요"는 틀린 처방이다.
    let mut arch_unexpanded: std::collections::BTreeMap<String, (String, &'static str)> = arc_stats
        .unexpanded
        .iter()
        .map(|(p, r, k)| (p.clone(), (r.clone(), *k)))
        .collect();
    let mut arch_unsupported: std::collections::BTreeMap<String, String> =
        arc_stats.unsupported.iter().cloned().collect();

    // 1차: 추출 + (규칙) 분류 → 아이템 수집(전파 위해 text/seed_eligible/vector 보관).
    struct Item { rec: Value, grade: Option<Grade>, text: String, seed_eligible: bool,
                  vector: Option<Vec<f32>>, file: String, dt: Option<doctype::DoctypeSignal>,
                  // 업무분류 축의 전파 신호. 예전에는 등급 신호들과 같은 signals
                  // 상자에 doctype_embed 로 섞여 있었는데, 2026-09-10 부터 그 축
                  // 안(doctype.embed)에 넣는다 — 축이 둘이라는 사실이 레코드
                  // 모양에서 드러나야 한다. 조립은 아래에서 하므로 여기 들고 있는다.
                  dt_embed: Option<Value>,
                  // 벡터 2차가 낸 업무분류 후보(후보, 판정 방법, 1등 유사도). 바로 합치지 않고
                  // 들고 있다가 BM25 1위와 한 번에 합친다(설계서 업무분류-2차-BM25 §08).
                  dt_embed_vals: Option<(Vec<(String, f64)>, String, f64)>,
                  // 1차 업무분류가 본 파일 경로(sig_label). 정상 추출 경로에서만 Some —
                  // BM25 제목 · 파일명 재료다(추출 실패 문서는 파이썬처럼 BM25 를 보지 않는다).
                  sig: Option<String>,
                  // 이 문서가 나온 최상위 압축 경로(일반 파일이면 None).
                  // 압축 '자체'의 집계 등급(내부 최고 위험)을 매기는 데 쓴다.
                  origin: Option<String> }
    let mut items: Vec<Item> = vec![];
    // 문서 식별자 집계 — ID 를 어떤 출처로 채웠는지 센다(요약 · R14).
    let (mut n_sfile, mut n_content, mut n_pathid) = (0usize, 0usize, 0usize);
    let (mut n_case, mut n_hash_mismatch) = (0usize, 0usize);
    let mut missing_id_rows: Vec<Value> = vec![];
    let docid_on = !opts.no_doc_id;
    // 화면(UI)이 진행바를 그리려면 '몇 개 중 몇 개째'를 알아야 한다. 첫 줄은 0/N 으로
    // 내보낸다 — 규칙셋·분류체계를 읽고 대상을 훑는 준비 단계가 있어, 첫 파일이 끝나기
    // 전까지 화면이 멈춘 것처럼 보이지 않게 하려는 것이다(Python cli.py 와 같은 형식·차례).
    // ※ 임베딩 모델은 여기가 아니라 아래 2차 단계에서, 그것도 필요할 때만 올린다.
    //    예전 주석은 "모델 로딩 때문에 첫 파일이 오래 걸려"라고 적혀 있었는데, 로딩은
    //    이 루프가 '끝난 뒤'라 사실과 달랐다.
    if opts.progress {
        eprintln!("[progress] 0/{} ", files.len());
    }
    // ── 복호화 작업장(엠파워 v11 암호화 문서) ─────────────────────────
    // 임시 폴더는 문서를 읽기 직전에 만든다 — 위의 사전 점검에서 끝나면 빈 폴더도 남기지 않는다.
    let mut dec_sess: Option<decrypt::Session> = None;
    if !dec_plans.is_empty() {
        let base = decrypt::resolve_tmp_base(opts.decrypt_tmp.as_deref());
        match decrypt::Session::open(dec_cmd.clone(), &base,
                                     opts.decrypt_timeout.unwrap_or(decrypt::DEFAULT_TIMEOUT_SECS)) {
            Ok(s) => dec_sess = Some(s),
            Err(why) => {
                let _ = std::fs::remove_dir_all(&arc_tmp);
                errcodes::fail("decrypt_unavailable", &format!("[MpowerClassify-rs] {}", why),
                               base.to_str());
            }
        }
    }
    // 풀어 둔 문서의 임시 폴더 — (그 문서에서 나온 마지막 작업의 번호, 폴더).
    // 그 번호를 지나면 폴더째 지운다. 압축이면 내부 문서 N건이 끝날 때까지 들고 있어야 한다.
    let mut dec_open: Vec<(usize, PathBuf)> = vec![];
    // 풀린 문서의 부속 정보 — 표시 경로 → (원래 파일 이름, 도구를 실제로 불렀는가).
    let mut dec_meta: std::collections::HashMap<String, (Option<String>, bool)> =
        std::collections::HashMap::new();
    // 복호화 도구로 푼 문서 수 / 풀지 못한 문서 수(요약 · 종료코드).
    let (mut n_dec, mut n_dec_fail) = (0usize, 0usize);
    // 번호로 돈다 — 복호화한 압축파일은 그 자리에서 내부 문서 N건으로 바뀌므로(splice)
    // 목록 길이가 도는 중에 달라진다.
    let mut next = 0usize;
    while next < files.len() {
        let done = next;
        // 이미 끝난 문서의 평문을 지운다 — "한 번에 한 건만 풀려 있다"(설계 §3).
        if let Some(sess) = dec_sess.as_ref() {
            dec_open.retain(|(last, dir)| {
                if *last < done { sess.release(dir); false } else { true }
            });
        }
        // 이 차례가 '아직 풀지 않은 암호화 문서'면 여기서 푼다.
        if let Some(plan) = dec_plans.remove(&files[done].label) {
            let display = files[done].label.clone();
            let src = files[done].src.clone();
            let sess = dec_sess.as_mut().expect("복호화 계획이 있으면 작업장도 있다");
            // 풀기 전에 크기를 본다 — 어차피 풀린 뒤 상한에서 버려질 파일은 풀지도 않는다(설계 §9).
            // 풀린 뒤의 실제 크기는 아래 크기 상한(G1·G2)과 압축 펼치기 상한이 지금까지처럼 다시 본다.
            let src_len = std::fs::metadata(&src).map(|m| m.len()).unwrap_or(0);
            // 앞 상한은 '풀린 뒤 실제로 적용될 상한'과 같게 고른다 — 원래 이름의 확장자로
            // 압축·텍스트·일반을 가르고, 원래 크기가 있으면 그 크기로 본다
            // (plan/엠파워v11-복호화-크기상한-정합-설계-20261016.html). --no-size-limit 이면 통과.
            let too_big = decrypt::pre_size_limit(plan.name.as_deref(), plan.size, src_len);
            // 풀기에 걸린 시간을 성공 로그에 남기려고 잰다.
            let t_dec = Instant::now();
            let outcome: Result<decrypt::Decrypted, decrypt::DecError> = match &plan.info {
                // 열쇠 칸이 일부만 있거나 감싼 열쇠 — 도구를 부르지 않고 오류로 남긴다.
                Err((reason, msg)) => Err(decrypt::DecError {
                    reason: *reason, rc: None, message: msg.clone(), fatal: false, dec_size: None }),
                Ok(_) if too_big.is_some() => Err(too_big.clone().unwrap()),
                Ok(info) => sess.decrypt(&src, info, plan.name.as_deref(), plan.size),
            };
            match outcome {
                Ok(d) => {
                    // 성공도 문서마다 한 줄 남긴다(파이썬 판과 같은 모양). 열쇠(mrk)는 남기지 않는다.
                    if let Ok(info) = &plan.info {
                        log_dec_ok(&display, plan.name.as_deref(), plan.size, &src, &d, info, t_dec);
                    }
                    if d.tool { n_dec += 1; }
                    // 압축파일이면 내부 문서로 펼친다(이 문서 전용 폴더 안에). 순서는
                    // 복호화 → 펼치기 → 분류다. zip_mode(저장 시 압축)는 도구가 이미 풀었고,
                    // 여기서 펼치는 것은 사용자가 올린 zip·7z 같은 '문서로서의 압축'이다.
                    let xdir = d.dir.join("x");
                    let _ = std::fs::create_dir_all(&xdir);
                    let tmp_label = d.file.to_string_lossy().replace('\\', "/");
                    let mark = (arc_stats.unexpanded.len(), arc_stats.unsupported.len(),
                                arc_stats.partial.len());
                    let mut jobs = archive::expand_paths(&[d.file.clone()], &xdir, 3, &mut arc_stats);
                    // 라벨에서 임시 경로를 원래 경로로 되돌린다 — 임시 경로는 결과·로그 어디에도
                    // 나가면 안 된다(곧 지워지는 경로이고, 임시 폴더 구조가 새어 나간다).
                    let fix = |s: &str| -> String {
                        if s == tmp_label {
                            display.clone()
                        } else if s.starts_with(&tmp_label) && s[tmp_label.len()..].starts_with('/') {
                            format!("{}{}", display, &s[tmp_label.len()..])
                        } else {
                            s.to_string()
                        }
                    };
                    for j in jobs.iter_mut() {
                        j.label = fix(&j.label);
                        j.origin = j.origin.as_deref().map(&fix);
                    }
                    for (p, r, k) in arc_stats.unexpanded[mark.0..].iter_mut() {
                        *p = fix(p);
                        arch_unexpanded.insert(p.clone(), (r.clone(), *k));
                    }
                    for (p, r) in arc_stats.unsupported[mark.1..].iter_mut() {
                        *p = fix(p);
                        arch_unsupported.insert(p.clone(), r.clone());
                    }
                    for (p, _) in arc_stats.partial[mark.2..].iter_mut() {
                        *p = fix(p);
                    }
                    dec_meta.insert(display.clone(), (plan.name.clone(), d.tool));
                    let n = jobs.len();
                    files.splice(done..done + 1, jobs);
                    if n == 0 {
                        sess.release(&d.dir);
                    } else {
                        dec_open.push((done + n - 1, d.dir));
                    }
                    // 번호를 올리지 않고 다시 돈다 — 이 자리가 이제 '풀린 문서'다.
                    continue;
                }
                // 환경 문제(도구를 못 띄움 · PHP 확장 없음) — 다음 문서도 똑같이 실패한다.
                // 남은 문서를 전부 실패로 채운 결과를 내느니 여기서 멈춘다(설계 §7).
                Err(e) if e.fatal => {
                    sess.close();
                    let _ = std::fs::remove_dir_all(&arc_tmp);
                    errcodes::fail("decrypt_unavailable",
                        &format!("[MpowerClassify-rs] 복호화 환경 문제로 중단합니다 — {}", e.message),
                        Some(&display));
                }
                // 파일 문제 — 이 문서만 오류 레코드로 남기고 다음으로 간다.
                // 암호문을 "그래도 분류해 보자"로 넘기지 않는다. 등급이 나와 버리면 그것이
                // 정상 결과처럼 DB 에 들어간다. 실패는 언제나 grade=null + error 다.
                Err(e) => {
                    let kind = if e.reason == "size_limit" {
                        n_size_skipped += 1;
                        fail += 1;
                        "size_limit"
                    } else {
                        n_dec_fail += 1;
                        "decrypt_failed"
                    };
                    // 성공 줄과 같은 칸(모드·크기·걸린 시간)을 실어 실패 원인을 로그만 보고 가늠하게 한다.
                    errlog::err(&dec_fail_line(&display, plan.name.as_deref(), plan.size,
                                               plan.info.as_ref().ok(), src_len, &e, t_dec));
                    let sig = plan.name.clone().unwrap_or_else(|| display.clone());
                    let mut rec = json!({
                        "file": display,
                        "grade": Value::Null,
                        "why": {"security": {"confidence": 0.0,
                                     "method": "decrypt_failed", "decided_by": [],
                                     "seed_eligible": false, "signals": {}}},
                        // kind 로 가르고(기계), reason 은 사람이 읽는 문장이다 — 다른 실패 레코드와
                        // 같은 틀. detail 은 세부 사유 이름, rc 는 removeenc 종료코드(안 불렀으면 null).
                        "error": {"stage": "decrypt", "kind": kind, "detail": e.reason,
                                  "rc": e.rc, "reason": e.message},
                        "meta": {"ts": now_iso()}
                    });
                    if let Some(n) = &plan.name { rec["name"] = json!(n); }
                    if docid_on {
                        // 지문(hash)은 싣지 않는다 — 여기서 읽을 수 있는 것은 암호문뿐이고,
                        // 암호문의 지문은 '이 문서가 무엇인가'를 말해 주지 않는다.
                        match flist.as_ref().and_then(|fl| fl.lookup(&display)) {
                            Some((en, how)) => {
                                n_sfile += 1;
                                rec["doc_id"] = json!(en.sfile_id);
                                rec["doc_id_source"] = json!("sfile_id");
                                if how == "case" {
                                    n_case += 1;
                                    rec["rematched_by"] = json!("case");
                                }
                            }
                            None => {
                                n_pathid += 1;
                                rec["doc_id"] = Value::Null;
                                rec["doc_id_source"] = json!("path");
                                missing_id_rows.push(json!({
                                    "file": rec["file"].clone(),
                                    "doc_id": Value::Null,
                                    "doc_id_source": "path"}));
                            }
                        }
                    }
                    items.push(Item {
                        dt_embed: None, dt_embed_vals: None, sig: None, rec,
                        grade: None, text: String::new(), seed_eligible: false,
                        vector: None, file: display.clone(), origin: None,
                        dt: dt_axis.as_ref().map(|(taxonomy, drs)|
                            doctype::scan_doctype("", &sig, drs, taxonomy)),
                    });
                    if opts.progress {
                        eprintln!("[progress] {}/{} {}", done + 1, files.len(), display);
                    }
                    next += 1;
                    continue;
                }
            }
        }
        // 여기부터는 이 문서를 끝까지 처리한다 — 아래의 continue 가 다음 문서로 가게 번호를 먼저 올린다.
        next += 1;
        let job = files[done].clone();
        // src = 실제로 읽을 경로(압축 내부면 임시파일), label = 표시·신호용 경로.
        // 압축 내부 문서는 이 둘이 다르다 — 결과에는 '압축경로/내부경로'가 나와야
        // 파일명·경로 규칙 신호가 원래대로 먹고, 사람도 어디 있던 문서인지 안다.
        let path = &job.src;
        let display = job.label.clone();
        // 파일명 신호에 쓸 이름. 엠파워 저장 이름(F_….SMF)에는 단서가 없어서, 목록이 준
        // 원래 이름(name)이 있으면 그것을 본다. 결과의 file 칸은 언제나 display 다.
        let sig_label: String = dec_meta.get(&display).and_then(|m| m.0.clone())
            .unwrap_or_else(|| display.clone());
        // 로그·진행 표시에 쓸 경로. 풀린 문서는 임시 경로 대신 원래 경로를 적는다.
        let shown: String = match dec_sess.as_ref() {
            Some(s) if path.starts_with(s.root()) => display.clone(),
            _ => path.display().to_string(),
        };
        let fmt = detect::detect_format(path);
        // 크기 상한(G1·G2) — 설계: plan/문서크기-상한-설계-20260904.html
        // 추출기를 부르기 전에 원본 바이트를 본다. 넘으면 아예 읽지 않는다.
        // 이유(reason)를 여기서 만들어 두고, 아래 추출 실패 경로와 같은 모양의
        // 보류 레코드로 내보내되 error.kind 로 갈라 놓는다 — '깨져서 못 읽음'과
        // '너무 커서 안 읽음'은 운영 대응이 다르기 때문이다.
        let size_reason = limits::check_size_limit(path, fmt);
        // 분할 압축의 '두 번째 이후 조각'은 압축으로 감지조차 되지 않아 일반
        // 파일로 흘러든다. 그대로 두면 뜻 없는 이진 덩어리를 본문으로 읽어
        // 분류한다(실측: 5조각 7z 의 세 번째 조각이 text 로 감지됐다).
        // 첫 조각은 압축 확장 단계가 이미 사유를 달았으므로 여기서 또 세지 않는다.
        let vol_reason = if size_reason.is_none() && !arch_unexpanded.contains_key(&display) {
            // 표시용 라벨(display)로 이름을 보고, 실제 경로(path)로 내용을 본다 —
            // 분할 zip 의 마지막 조각은 이름이 평범한 .zip 이라 내용까지 봐야 안다.
            archive::split_volume_hint(&display, Some(path)).map(|h| {
                format!(
                    "분할 압축의 조각입니다({}) — 조각 하나만으로는 내용을 꺼낼 수 \
                     없어 읽지 않았습니다. 이 판은 분할 압축을 잇지 않습니다.",
                    h
                )
            })
        } else {
            None
        };
        // 사이냅 모드에서 못 읽은 까닭(엔진별). 자체 파서 모드는 빈 문자열이라 예전 문장 그대로다.
        let mut ext_why = String::new();
        let extracted = if size_reason.is_some() || vol_reason.is_some() {
            None
        } else {
            // 이번 실행의 추출 모드(자체 파서 · 사이냅 폴백 · 사이냅만)로 뽑는다.
            // 로그에는 임시 경로 대신 원래 경로(shown)를 적는다.
            match extract::extract_text_with(path, fmt, extract::mode(), &shown) {
                Ok(t) => Some(t),
                Err(why) => { ext_why = why; None }
            }
        };
        // 파서가 '상한에서 멈췄다'를 남겼으면 가져온다(G5 관측). 가져가면 비워지므로
        // 다음 문서에 지난 표식이 남지 않는다.
        let partial = limits::notes_take();
        // PDF 파서가 남긴 페이지 구성(읽은 쪽 수, 스캔 쪽 수) — 처리제외 판정 재료.
        let layout = limits::notes_take_layout();
        let text = match extracted {
            Some(t) => t,
            None => {
                // 못 읽은 문서도 '결과'다 — 레코드를 안 만들면 그 문서는 결과에서
                // 통째로 사라져, 화면에는 "18개 중 11건"처럼 조용히 줄어든 숫자만
                // 남는다. 무엇이 왜 빠졌는지 알 수 없는 것은 거버넌스 도구에서
                // 가장 나쁜 실패다. 등급 없이(=보류) 실패 사유를 실어 내보낸다.
                // 크기 초과는 '실패'가 아니라 '안 읽기로 한 것'이라 말투와 kind 를 나눈다.
                // 같은 문장으로 남기면 운영자가 파일이 깨진 줄 알고 원본을 뒤진다.
                let (kind, reason) = match (&size_reason, &vol_reason) {
                    // 분할 조각 — 깨진 파일이 아니라 '원래 단독으로 못 읽는 것'이다.
                    (None, Some(why)) => {
                        errlog::note(&format!("[분할조각] {} :: {}", shown, why));
                        n_split_volume += 1;
                        ("split_volume", why.clone())
                    }
                    (Some(why), _) => {
                        errlog::err(&format!("[크기초과] {} :: {}", shown, why));
                        n_size_skipped += 1;
                        ("size_limit", why.clone())
                    }
                    (None, None) if ext_why.is_empty() => {
                        errlog::err(&format!("[추출실패] {} (감지={})",
                                             shown, fmt.as_str()));
                        ("extract_failed", "텍스트를 추출하지 못했습니다".to_string())
                    }
                    // 사이냅 모드 — 엔진마다 왜 못 읽었는지를 사유에 붙인다(파이썬 판과 같은 모양).
                    (None, None) => {
                        errlog::err(&format!("[추출실패] {} (감지={}) :: {}",
                                             shown, fmt.as_str(), ext_why));
                        ("extract_failed", format!("텍스트를 추출하지 못했습니다 — {}", ext_why))
                    }
                };
                fail += 1;
                items.push(Item {
                    dt_embed: None, dt_embed_vals: None, sig: None,
                    rec: json!({
                        "file": display,
                        "grade": Value::Null,
                        "why": {"security": {"confidence": 0.0,
                                     "method": "extract_failed", "decided_by": [],
                                     "seed_eligible": false, "signals": {}}},
                        "error": {"stage": "extract", "detected": fmt.as_str(),
                                  "kind": kind, "reason": reason},
                        "meta": {"ts": now_iso()}
                    }),
                    grade: None, text: String::new(), seed_eligible: false,
                    vector: None, file: display.clone(),
                    // 본문이 없어도 업무분류 축이 켜져 있으면 '축은 돌았다'를 남긴다 —
                    // 그래야 화면이 "분류 안 됨(사람이 봐야 함)"으로 셀 수 있다.
                    origin: job.origin.clone(),
                    dt: dt_axis.as_ref().map(|(taxonomy, drs)|
                        doctype::scan_doctype("", &sig_label, drs, taxonomy)),
                });
                // 못 펼친 압축은 대개 '추출도 실패'해 이 갈래로 온다(이 판에는 압축
                // 본문을 읽어 줄 사이냅이 없다). 표식을 성공 경로에만 달면 정작
                // 필요한 이 자리에서 빠지므로 여기서도 단다.
                {
                    let r = &mut items.last_mut().unwrap().rec;
                    if let Some((reason, kind)) = arch_unexpanded.get(&display) {
                        r["archive_unexpanded"] = json!(true);
                        r["archive_unexpanded_reason"] = json!(reason);
                        // 상한 때문인지(limit) 그 밖의 실패인지(error) — 사람이 할 일이 다르다.
                        r["archive_unexpanded_kind"] = json!(kind);
                    }
                    if let Some(reason) = arch_unsupported.get(&display) {
                        r["archive_unsupported"] = json!(true);
                        r["archive_unsupported_reason"] = json!(reason);
                    }
                }
                // 못 읽은 문서에도 식별자는 단다 — 그 문서도 사람이 손볼 '결과'라
                // 나중에 수정을 이으려면 키가 필요하다(내용을 못 읽으면 경로 해시로 내려간다).
                if docid_on {
                    let (did, src, _key, matched, chash) =
                        filelist::resolve_doc_id(&display, path, flist.as_ref());
                    match src {
                        "sfile_id" => n_sfile += 1,
                        "content" => n_content += 1,
                        _ => n_pathid += 1,
                    }
                    if matched == Some("case") { n_case += 1; }
                    let r = &mut items.last_mut().unwrap().rec;
                    r["doc_id"] = match &did { Some(v) => json!(v), None => Value::Null };
                    r["doc_id_source"] = json!(src);
                    // 못 읽은 문서라도 지문을 얻었으면 싣는다 — 사람이 손본 기록을
                    // 나중에 다시 이으려면 이 값이 열쇠다.
                    if let Some(h) = &chash { r["hash"] = json!(h); }
                    if matched == Some("case") { r["rematched_by"] = json!("case"); }
                    if src != "sfile_id" {
                        missing_id_rows.push(json!({
                            "file": r["file"].clone(),
                            "doc_id": r["doc_id"].clone(),
                            "doc_id_source": r["doc_id_source"].clone()}));
                    }
                }
                if let Some((name, tool)) = dec_meta.get(&display) {
                    let r = &mut items.last_mut().unwrap().rec;
                    if let Some(n) = name { r["name"] = json!(n); }
                    if *tool { r["meta"]["decrypted"] = json!(true); }
                }
                continue;
            }
        };
        // 글자수 상한(G3) — 여기서 자르는 이유: 아래로 이어지는 비용(PII 정규식
        // 전문 스캔 · 임베딩 청크 수)이 전부 이 길이 하나로 결정된다. 한 곳에서
        // 유계로 만들면 함께 유계가 된다. 버리는 게 아니라 앞부분만 보는 것이며,
        // 표식은 레코드가 만들어진 뒤에 단다.
        let (text, n_text_orig, was_truncated) = limits::truncate_text(text, text_limit);
        let (mut rec, grade) = if rules_enabled {
            build_record(&sig_label, &text, &rs, opts.failsafe.as_deref())
        } else {
            // 벡터-only: 규칙 미실행 → 보류 레코드(빈 signals). failsafe 는 전파 재융합에서 적용.
            (json!({
                "file": display,
                "grade": Value::Null,
                "why": {"security": {"confidence": 0.0,
                             "method": "unclassified", "decided_by": [],
                             "seed_eligible": false, "signals": {}}},
                "meta": {"ts": now_iso()}
            }), None)
        };
        // 신호는 원래 이름으로 봤지만, 결과의 file 은 부른 쪽이 준 경로 그대로여야 한다
        // (목록과 결과를 잇는 키다).
        if sig_label != display { rec["file"] = json!(display); }
        // 본문을 사실상 못 읽었으면 표식을 단다(분류 결과 자체는 건드리지 않는다 —
        // 파일명·경로 신호는 본문과 무관하고, 몇 글자라도 규칙에 걸렸으면 그 등급이
        // 맞다). 다만 아무것도 못 정했다면 method 를 'unclassified' 로 두지 않는다.
        // 그 말은 "봤는데 없더라"라는 뜻이라 사실과 다르다.
        // 본문 일부만 보고 판정했으면 표식을 단다(분류 결과 자체는 건드리지 않는다).
        // 추출 실패와 달리 fail 은 올리지 않는다 — 등급은 정상적으로 나왔고,
        // '전문을 못 봤다'는 것은 실패가 아니라 재검토 대상이라는 뜻이다.
        if was_truncated {
            n_text_truncated += 1;
            rec["text_truncated"] = json!(true);
            rec["text_chars"] = json!(text.chars().count());
            rec["text_chars_original"] = json!(n_text_orig);
            errlog::note(&format!("[본문절단] {} {}자 → {}자(상한)",
                                  shown, n_text_orig, text_limit));
        }
        // 파서가 상한에서 멈춰 끝까지 읽지 못했으면 그 사실도 남긴다(G5).
        // 절단(G3)과 따로 세는 이유: G3 은 '다 읽고 나서 잘랐다', 이건 '애초에
        // 끝까지 못 읽었다' — 재검토 시 손볼 상한값이 서로 다르다.
        // 압축을 펼치지 못했으면 그 압축 레코드에 표식을 단다(G4/미지원).
        // 이게 없으면 내부 문서 수백 건이 사라진 것이 결과에 전혀 드러나지 않는다.
        if let Some((reason, kind)) = arch_unexpanded.get(&display) {
            rec["archive_unexpanded"] = json!(true);
            rec["archive_unexpanded_reason"] = json!(reason);
            rec["archive_unexpanded_kind"] = json!(kind);
        }
        if let Some(reason) = arch_unsupported.get(&display) {
            rec["archive_unsupported"] = json!(true);
            rec["archive_unsupported_reason"] = json!(reason);
        }
        if let Some((reason, unit, read, total)) = partial {
            n_partial_extract += 1;
            rec["partial_extract"] = json!(true);
            rec["partial_extract_info"] = json!({
                "reason": reason, "unit": unit, "read": read, "total": total
            });
        }
        let body_chars = text.chars().filter(|c| !c.is_whitespace()).count();
        // 스캔 PDF 는 먼저 가린다 — '못 읽음(실패)'이 아니라 '처리제외(범위 밖)'라서
        // error 도 달지 않고 종료코드도 올리지 않는다. 본문이 20자를 넘는 스캔본(머리말
        // 몇 줄만 글자로 든 문서)도 있어 본문없음 판정과 무관하게 먼저 본다.
        let scan = layout.filter(|&(pages, scan_pages)|
            extract::scan_pdf_verdict(pages, scan_pages, body_chars));
        if let Some((pages, scan_pages)) = scan {
            n_excluded += 1;
            errlog::note(&format!("[처리제외] {} (스캔 PDF, {}쪽 중 {}쪽이 그림)",
                                  shown, pages, scan_pages));
            rec["excluded"] = json!({
                "kind": "scan_pdf", "pages": pages, "scan_pages": scan_pages,
                "text_len": body_chars,
                "reason": "처리제외(EXCLUDED) — 스캔 PDF(텍스트 레이어 없음)입니다. OCR 은 1차 \
                           범위에서 제외되어, 본문 없이 파일명·경로 신호로만 판정했습니다."
            });
            // 아무것도 못 정했을 때만 method 를 바꾼다 — 'unclassified' 는 "봤는데 없더라"다.
            if rec["grade"].is_null()
                && rec["method"].as_str().map_or(true, |m| m == "unclassified") {
                rec["method"] = json!("excluded");
            }
        } else if body_chars < min_text_len {
            fail += 1;
            errlog::err(&format!("[본문없음] {} ({}자) — 스캔본(이미지)일 수 있습니다",
                                 shown, body_chars));
            // kind 는 여기서도 채운다 — 크기 초과(size_limit)가 이 칸을 쓰기
            // 시작했으므로, 어떤 실패는 kind 가 있고 어떤 실패는 없으면 읽는 쪽이
            // 매번 존재 확인을 해야 하는 반쪽짜리 필드가 된다(파이썬 판과 동일).
            rec["error"] = json!({
                "stage": "extract", "detected": fmt.as_str(), "text_len": body_chars,
                "kind": "no_body",
                "reason": "본문 텍스트가 거의 없습니다 — 스캔본(이미지)이거나 빈 문서일 수 \
                           있습니다. OCR 이나 사람 확인이 필요합니다."
            });
            if rec["grade"].is_null()
                && rec["method"].as_str().map_or(true, |m| m == "unclassified") {
                rec["method"] = json!("extract_failed");
            }
        }
        // 문서 식별자(doc_id·key) — 설계 §7-5. 해시 계산보다 먼저 달아 둔다.
        if docid_on {
            let (did, src, key, matched, chash) =
                filelist::resolve_doc_id(&display, path, flist.as_ref());
            match src {
                "sfile_id" => n_sfile += 1,
                "content" => n_content += 1,
                _ => n_pathid += 1,
            }
            if matched == Some("case") {
                n_case += 1;
                rec["rematched_by"] = json!("case");
            }
            // F6 — 목록이 준 해시와 실제 파일이 다르면 문서가 수정된 것이다.
            // 같은 문서이므로 처리는 그대로 계속하고 건수만 센다.
            if src == "sfile_id" {
                if let Some(fl) = flist.as_ref() {
                    if let Some((e, _)) = fl.lookup(&display) {
                        if let Some(want) = &e.hash {
                            if file_hash(path).as_deref() != Some(want.as_str()) {
                                n_hash_mismatch += 1;
                            }
                        }
                    }
                }
            }
            rec["doc_id"] = match &did { Some(v) => json!(v), None => Value::Null };
            rec["doc_id_source"] = json!(src);
            let _ = &key;   // key 칸은 2026-09-10 에 없앴다(file 에서 그대로 나온다)
            // 지문은 여기서 딱 한 번 구해 싣는다 — 아래 need_hash 가 다시 구하지 않는다.
            if let Some(h) = &chash { rec["hash"] = json!(h); }
            if src != "sfile_id" {
                missing_id_rows.push(json!({
                    "file": rec["file"].clone(),
                    "doc_id": rec["doc_id"].clone(),
                    "doc_id_source": rec["doc_id_source"].clone()}));
            }
        }
        if need_hash { rec["hash"] = json!(file_hash(path)); }

        // 추출 본문 보존(--textsave) — 설계: plan/추출텍스트-저장-설계-20260907.html
        // 해시를 단 바로 뒤에 부른다: 파일 이름이 그 해시이기 때문이다.
        // 저장하는 text 는 정제·절단까지 끝난 값 — 도구가 실제로 보고 판정한
        // 바로 그 텍스트여야 나중에 근거를 되짚을 때 어긋나지 않는다(설계 6장).
        if let Some(dir) = &text_save_dir {
            // 본문이 없으면 파일을 만들지 않는다 — 0바이트 파일을 남기면
            // "저장됐다"로 오해된다. text_saved 키가 없는 것으로 구분된다.
            let sha = rec.get("hash").and_then(|v| v.as_str()).map(|x| x.to_string());
            if let (Some(sha), false) = (sha, text.is_empty()) {
                // doc_id 는 sfile_id 로 얻은 것만 싣는다 — 폴백은 hash 앞 40자라
                // 옆 칸과 같은 값이고, 받는 쪽이 적재해 버리면 안 된다.
                let did = if rec.get("doc_id_source").and_then(|v| v.as_str()) == Some("sfile_id") {
                    rec.get("doc_id").and_then(|v| v.as_str()).map(|x| x.to_string())
                } else {
                    None
                };
                let saved = textsave::save(dir, &sha, &text).and_then(|(name, dedup)| {
                    textsave::index_append(dir, &sha, &name, &display, did.as_deref(),
                                           text.chars().count(), was_truncated, &now_iso())
                        .map(|_| (name, dedup))
                });
                match saved {
                    Ok((name, dedup)) => {
                        rec["text_saved"] = json!(name);
                        n_text_saved += 1;
                        if dedup { n_text_dedup += 1; }
                    }
                    // 저장 실패는 분류 실패가 아니다 — 종료코드를 건드리지 않고
                    // 그 문서만 건너뛴다(설계 P1·P2).
                    Err(e) => {
                        rec["text_save_error"] = json!(e);
                        n_text_save_failed += 1;
                    }
                }
            }
        }
        if opts.with_pii { rec["pii"] = json!(rules::collect_pii(&text, &rs)); }
        // 추출(정제) 텍스트를 레코드에 함께 싣는다 — 기본은 off(프라이버시).
        // 파이썬은 safe_text()로 짝 없는 대리 문자를 걸러내지만, Rust 의 String 은
        // 언제나 올바른 UTF-8 이라 그 자리가 없다 — 걸러낼 것이 애초에 없다.
        // 칸 차례는 파이썬과 같다: _REC_ORDER 에 없는 이름이라 맨 뒤에 붙는다.
        if opts.with_text { rec["text"] = json!(text); }
        // 업무분류 축은 security 모드(rule-only/vector-only)와 무관하게 독립적으로 돈다(6-4).
        let dt = dt_axis.as_ref().map(|(taxonomy, drs)| doctype::scan_doctype(&text, &sig_label, drs, taxonomy));
        // [2026-09-10] 최상위 rec["seed_eligible"] 을 읽고 있었다. 평탄화로 그 칸은
        // security 안으로 옮겨져 최상위에는 더 이상 없다 — 늘 false 가 나왔고,
        // 규칙이 고신뢰로 찍은 문서가 아래 임베딩 대상 판정에서 통째로 빠졌다.
        // 엠파워 v11 문서면 원래 이름과 '풀어서 읽었다'는 사실을 남긴다. 열쇠는 남기지 않는다.
        if let Some((name, tool)) = dec_meta.get(&display) {
            if let Some(n) = name { rec["name"] = json!(n); }
            if *tool { rec["meta"]["decrypted"] = json!(true); }
        }
        let seed_elig = record::seed_eligible_of(&rec);
        if opts.progress {
            // 형식은 Python 판과 같아야 한다 — 화면이 같은 규칙으로 읽는다.
            eprintln!("[progress] {}/{} {}", done + 1, files.len(), shown);
        }
        items.push(Item { rec, grade, text, seed_eligible: seed_elig, vector: None, file: display, dt,
                          dt_embed: None, dt_embed_vals: None, sig: Some(sig_label.clone()),
                          origin: job.origin.clone() });
    }

    // 풀어 둔 평문을 모두 지운다 — 2차(임베딩·전파)는 뽑아 둔 본문만 쓰고 파일을 다시 읽지 않는다.
    if let Some(sess) = dec_sess.as_ref() {
        for (_, dir) in dec_open.drain(..) { sess.release(&dir); }
        sess.close();
    }

    // 2차: 임베딩 → (auto_prop 이면) seed 비교 → 보류 문서 재융합.
    //   임베딩 자체는 전파와 분리해서 돌린다 — --with-vector 는 전파를 안 하더라도
    //   레코드에 벡터를 실어야 하므로, 전파 조건에 묶어 두면 벡터가 통째로 비게 된다.
    let mut prop_stats: Option<(usize, u32, u32)> = None;
    // 모델을 올릴 필요가 '한 건이라도' 있는가 — 세션을 만들기 전에 먼저 답한다.
    //
    // [2026-09-02] 예전에는 embed_mode 만 보고 무조건 Embedder::load() 했다. 전 문서가
    // 1차 규칙에서 확정된 배치에서는 model.onnx(118MB)+tokenizer.json(17MB)을 읽고
    // ORT 세션까지 구성한 뒤, 아래 루프에서 need 가 전부 false 라 **한 건도 임베딩하지
    // 않고** 끝났다. 상주 서버(--daemon)를 안 쓰면 그 비용을 프로세스마다 새로 낸다
    // (실측 건당 약 1.4초, Rust/README.md 성능표). 그래서 아래 need 와 같은 판정을
    // 배치 전체에 대해 미리 한 번 돌려, 필요 없으면 모델을 아예 읽지 않는다.
    //
    // [2026-09-10] 이 미리보기 판정과 아래 루프의 판정이 갈려 있었다. 여기서는
    // seed_eligible 을 `opts.embed_needed` 일 때만 셌고(아래 루프는 늘 센다),
    // 그 근거는 "seed_eligible 문서의 벡터는 security 전파 루프에서만 쓰이는데
    // 그 루프는 보류 문서만 돈다" 였다. 업무분류 축이 생기면서 그 전제가 깨졌다 —
    // doctype 전파 루프는 **벡터가 있는 문서 전부**를 돌기 때문에, 규칙으로 등급이
    // 확정된 seed_eligible 문서의 벡터도 실제로 쓰인다.
    //
    // 그 결과 전 문서가 규칙으로 확정된 배치에서는 모델을 아예 안 읽고 끝나,
    // 업무분류 2단계가 통째로 사라졌다(파이썬은 같은 배치에서 임베딩한다).
    // 두 판정이 갈릴 자리를 아예 없앤다 — 하나의 판정 함수를 양쪽이 같이 쓴다.
    //
    // 미리보기 자체는 그대로 필요하다: 상주 서버(--daemon)를 안 쓰는 실행은 model.onnx(118MB)
    // + tokenizer.json(17MB) 적재와 ORT 세션 구성을 프로세스마다 새로 내므로
    // (실측 건당 약 1.4초, Rust/README.md 성능표), 한 건도 임베딩하지 않을 배치에서
    // 모델을 읽는 것은 순수한 낭비다.
    //
    //   · security — 보류(grade=None) 이거나 seed 승격 후보(seed_eligible)
    //   · doctype  — 축은 켜졌는데 라벨이 하나도 안 붙음(values 가 빔)
    // doctype 조건이 꼭 필요한 이유: 규칙이 못 맞힌 문서(또는 --doctype-vector-only 로
    // 규칙을 비운 배치)는 1차 스캔이 빈손인데 security 등급은 멀쩡히 나올 수 있다. security
    // 기준만 보면 벡터를 안 만들고 → 전파도 못 하고 → 업무분류가 영영 미분류로 남는다.
    //
    // 파이썬 cli.py 의 need_vec 과 항이 하나씩 대응한다(둘이 갈리면 두 판이 갈린다).
    let need_vec = |it: &Item| {
        let dt_undecided = it.dt.as_ref().map_or(false, |s| s.values.is_empty());
        embed_mode == "all" || it.grade.is_none() || it.seed_eligible || dt_undecided
    };
    let need_any = items.iter().any(&need_vec);
    // 2차 대상 표시는 지금(전파로 등급이 바뀌기 전) 정해 둔다 — 파이썬은 파일마다 도는 자리에서
    // need_vec 을 보고 BM25 재료를 남기므로, 보안 전파 뒤에 다시 보면 대상이 달라진다.
    let need_flags: Vec<bool> = items.iter().map(&need_vec).collect();
    if embed_mode != "none" && !need_any {
        // 조용히 넘어가면 "임베딩이 안 돌았다"와 "임베딩이 필요 없었다"가 구분되지 않는다.
        // 뒤에 붙던 [전파] 요약줄도 이 경우엔 안 나가므로, 그 자리를 이 줄이 대신한다.
        note!("[MpowerClassify-rs] 임베딩 불필요(전 문서 규칙 확정) → 모델 로드 생략");
    }
    // 모델을 못 올려 임베딩이 필요한 문서를 못 다룬 경우 — 결과는 그대로 내되
    // 종료코드를 2(3002 model_load_failed)로 올린다. 예전에는 exit 0·success 로 끝나,
    // --vector-only 에서 전 문서가 미분류인데도 부르는 쪽은 성공으로 읽었다.
    let mut model_fail: Option<String> = None;
    if embed_mode != "none" && need_any && !deploy_ok && !opts.daemon {
        model_fail = Some(format!("[MpowerClassify-rs] 임베딩 모델/런타임을 올리지 못해 임베딩·전파를 건너뛰었습니다 — {}",
                                  deploy_why.clone().unwrap_or_default()));
    }
    // --daemon 이면 임베딩을 상주 서버에 맡긴다(없으면 자동으로 띄우고, 못 쓰면 직접 로드로
    // 되돌아간다). 클라이언트는 여기서 만들기만 하고 접속은 첫 문서 때 한다 — 그래서
    // 임베딩이 필요 없는 실행(need_any=false)은 서버를 건드리지도 띄우지도 않는다.
    if embed_mode != "none" && need_any && (deploy_ok || opts.daemon) {
        let client = opts.daemon.then(|| daemon::client::DaemonClient::new(
            embed::current(), opts.idle_timeout, opts.log.clone(), opts.verbose));
        match embed::EmbedBackend::open(client, deploy_ok) {
            Some(mut embedder) => {
                // 대상 판정은 위 미리보기와 **같은 함수**다 — 갈릴 자리를 두지 않는다.
                for it in items.iter_mut() {
                    if need_vec(it) { it.vector = embedder.embed_document(&it.text); }
                }
                // 상주 서버도 직접 로드도 못 쓴 경우(--daemon 에서만 여기로 온다 — 직접 로드만
                // 쓰는 실행은 open() 이 None 을 줘 아래 None 갈래로 간다). 직접 로드가 실패했을
                // 때와 같은 기준으로 다룬다: --vector-only 는 멈추고, 그 밖은 결과를 내되 2 로 알린다.
                let embed_failed = embedder.failed();
                if embed_failed {
                    let why = deploy_why.clone().unwrap_or_else(|| format!(
                        "모델/런타임 로드 실패({}/{}, onnxruntime.dll 확인)",
                        embed::MODEL_ROOT, embed::current().local_dir));
                    if opts.vector_only {
                        errcodes::fail("model_load_failed",
                            &format!("[MpowerClassify-rs] --vector-only 는 임베딩 모델이 있어야 합니다 — {}", why),
                            None);
                    }
                    let m = format!("[MpowerClassify-rs] 임베딩 모델/런타임을 올리지 못해 임베딩·전파를 건너뛰었습니다 — {}", why);
                    errlog::err(&m);
                    model_fail = Some(m);
                }
                // 여기부터는 '전파'다. --with-vector 만 준 경우(전파 off)는 위에서 벡터만
                // 만들고 끝내야 하므로 통째로 건너뛴다. 벡터를 못 만든 실행도 건너뛴다.
                if auto_prop && !embed_failed {
                // 내부 seed(코퍼스 seed_eligible) + 외부 seed(class_seed.jsonl) 병합.
                let rec_tuples: Vec<(Option<Vec<f32>>, Option<String>, bool, String)> = items.iter()
                    .map(|it| (it.vector.clone(), it.grade.map(|g| g.as_str().to_string()), it.seed_eligible, it.file.clone()))
                    .collect();
                let internal = propagate::SeedIndex::from_records(&rec_tuples);
                let external = match &seeds_path {
                    Some(p) => propagate::SeedIndex::from_seed_file(p),
                    None => propagate::SeedIndex::empty(),
                };
                let seeds = propagate::SeedIndex::merge(external, internal);
                // 보류 문서만 전파(이미 등급 확정 문서는 손대지 않음).
                let (mut decided, mut still) = (0u32, 0u32);
                // 2차 패스도 진행을 알린다. 추출·1차는 파일마다 [progress] 를 내는데
                // 여기서부터는 여태 아무 말이 없어, 문서가 수천 건이면 "멈춘 건가"를
                // 알 수 없었다. 건너뛰는 레코드에서도 세어 진행이 끊기지 않게 한다.
                let prop_total = items.len();
                for (done, it) in items.iter_mut().enumerate() {
                    if opts.progress {
                        eprintln!("[progress][전파·보안등급] {}/{} {}", done + 1, prop_total, it.file);
                    }
                    if it.grade.is_some() { continue; }
                    let vec = match &it.vector { Some(v) => v.clone(), None => continue };
                    let esig = propagate::propagate(&vec, &seeds);
                    let dict = esig.as_dict();
                    it.grade = rules::refuse_with_embed(&mut it.rec, esig.grade.as_deref(), esig.confidence as f64, dict, opts.failsafe.as_deref());
                    if it.grade.is_some() { decided += 1; } else { still += 1; }
                }
                prop_stats = Some((seeds.size(), decided, still));

                // doctype 축 전파(설계서 5-4). security 와 달리 "이미 후보가 있어도"
                // embed 가 후보를 더할 수 있다 — 다중 라벨이라 상향 전용이 아니라
                // 추가 전용이다. 그래서 "이미 분류됐으면 건너뛴다"는 조건이 없다.
                if let Some((_, drs)) = dt_axis.as_ref() {
                    let dt_seeds = match &seeds_path {
                        Some(p) => propagate::DoctypeSeedIndex::from_seed_file(p),
                        None => propagate::DoctypeSeedIndex::empty(),
                    };
                    // 정책 파일의 embed: 블록이 이 단계의 스위치이자 임계값이다.
                    // 스위치가 꺼져 있으면 조용히 넘어가지 말고 이유를 남긴다 —
                    // 그러지 않으면 "왜 벡터가 안 도는지"를 아무도 못 찾는다.
                    // (embed.enabled 가 꺼진 경우의 안내는 아래 '2차 합치기'에서 낸다 — BM25 만
                    //  켜진 실행이면 2차가 여전히 돌기 때문이다)
                    if dt_seeds.size() > 0 && drs.embed.enabled {
                        let dt_params = propagate::DoctypeParams {
                            k: drs.embed.k as usize,
                            dup_threshold: drs.embed.dup_threshold as f32,
                            min_sim: drs.embed.min_sim as f32,
                            min_share: drs.embed.min_share as f32,
                        };
                        let dt_total = items.len();
                        for (done, it) in items.iter_mut().enumerate() {
                            if opts.progress {
                                eprintln!("[progress][전파·업무분류] {}/{} {}", done + 1, dt_total, it.file);
                            }
                            if it.dt.is_none() { continue; }
                            let vec = match &it.vector { Some(v) => v.clone(), None => continue };
                            let esig = propagate::propagate_doctype(&vec, &dt_seeds, dt_params);
                            it.dt_embed = Some(esig.as_dict());
                            // 합치기는 아래 '2차 합치기'에서 BM25 1위와 함께 한 번에 한다
                            it.dt_embed_vals = Some((esig.values.iter().map(|(id, c)| (id.clone(), *c as f64)).collect(),
                                                     esig.method.clone(), esig.top_sim as f64));
                        }
                    }
                    // seed 가 없는 경우의 안내는 위(모드 결정 직후)에서 이미 냈다 — 그쪽은
                    // auto_prop 이 아예 꺼진 경우까지 잡아 주므로 여기서 또 내지 않는다.
                }
                } // if auto_prop
            }
            None => {
                // 파일은 있는데 로드가 실패한 드문 경우(깨진 모델 등). --vector-only 는
                // 결과가 전부 미분류라 내보내지 않는다 — 아직 한 건도 출력하지 않은 자리다.
                if opts.vector_only {
                    errcodes::fail("model_load_failed",
                        &format!("[MpowerClassify-rs] --vector-only 인데 임베딩 모델을 올리지 못했습니다({}/{}, onnxruntime.dll 확인).",
                                 embed::MODEL_ROOT, embed::current().local_dir),
                        None);
                }
                let m = format!(
                    "[MpowerClassify-rs] 임베딩 모델/런타임 로드 실패 → 임베딩·전파 생략({}/{}, onnxruntime.dll 확인).",
                    embed::MODEL_ROOT, embed::current().local_dir);
                errlog::err(&m);
                model_fail = Some(m);
            }
        }
    }

    // 2차 합치기 — 위에서 계산해 둔 벡터 후보와 BM25 1위를 1차 후보에 한 번에 얹는다
    // (설계서 업무분류-2차-BM25 §08 · §16). BM25 는 벡터가 없어도 돈다 — 모델이 없거나
    // 임베딩이 실패한 실행에서도 제목 · 파일명은 있기 때문이다. 그래서 임베딩 블록 밖에 둔다.
    if auto_prop && embed_mode != "none" && need_any {
        if let Some((taxonomy, drs)) = dt_axis.as_ref() {
            let dt_seed_n = match &seeds_path {
                Some(p) => propagate::DoctypeSeedIndex::from_seed_file(p).size(),
                None => 0,
            };
            let bm = bm25_context(drs, seeds_path.as_deref());
            let bm_thr = bm.as_ref().and_then(|b| b.1);
            if dt_seed_n > 0 && !drs.embed.enabled && bm_thr.is_none() {
                // 정책 파일의 embed: · bm25: 블록이 이 단계의 스위치다. 둘 다 꺼져 있으면
                // 조용히 넘어가지 말고 이유를 남긴다 — "왜 2차가 안 도는지"를 아무도 못 찾는다.
                note!("[전파][업무분류] seed {}건이 있으나 doc_rule.yaml 의 \
embed.enabled 가 false 라 2단계를 건너뜁니다", dt_seed_n);
            } else if dt_seed_n > 0 {
                let (mut contributed, mut no_vector) = (0u32, 0u32);
                let (mut picked, mut agree, mut alone, mut name_only) = (0u32, 0u32, 0u32, 0u32);
                let no_skip: std::collections::HashSet<usize> = std::collections::HashSet::new();
                for (i, it) in items.iter_mut().enumerate() {
                    let existing = match &it.dt { Some(sig) => sig.values.clone(), None => continue };
                    let emb = it.dt_embed_vals.take();
                    let mut pick: Option<doctype::Bm25Pick> = None;
                    if let (Some((idx, Some(thr))), Some(sig), true) = (bm.as_ref(), it.sig.as_ref(), need_flags[i]) {
                        let sp = &drs.bm25;
                        let title = bm25::title_of(&it.text, sig);
                        let q = bm25::field_tokens(&title, sig, sp, &drs.head_noise);
                        let rk = idx.rank(&q, &no_skip);
                        if let Some(top) = bm25::pick(&rk, *thr, sp.min_margin) {
                            let part = rk.parts.get(&top).cloned().unwrap_or_default();
                            let (pt, pn) = (*part.get("title").unwrap_or(&0.0), *part.get("name").unwrap_or(&0.0));
                            let agrees = emb.as_ref().map_or(false, |(v, _, _)| v.iter().any(|(d, _)| *d == top));
                            if pt > 0.0 || agrees || sp.name_only_allow {
                                pick = Some(doctype::Bm25Pick {
                                    score: rk.ranked[0].1, threshold: *thr, title: pt, name: pn,
                                    words: rk.words.get(&top).cloned().unwrap_or_default(), dc_id: top,
                                });
                            }
                        }
                    }
                    if emb.is_none() && pick.is_none() {
                        // 벡터도 BM25 1위도 없으면 1차 결과를 그대로 둔다(파이썬과 같다).
                        no_vector += 1;
                        continue;
                    }
                    let merged = doctype::merge_second_stage(
                        &existing,
                        emb.as_ref().map(|(v, m, t)| (v.as_slice(), (m.as_str(), *t))),
                        taxonomy, &drs.conflict, Some(drs.defaults.embed_cap),
                        pick.as_ref().map(|p| (p, &drs.bm25)));
                    if merged.values.iter().any(|v| v.from.iter().any(|f| f == "embed")) {
                        contributed += 1;
                    }
                    if let Some(p) = &pick {
                        picked += 1;
                        if let Some(got) = merged.values.iter().find(|v| v.dc_id == p.dc_id) {
                            if got.from.iter().any(|f| f == "bm25") {
                                if got.basis == "bm25_name_only" { name_only += 1; }
                                else if got.from.iter().any(|f| f == "embed") { agree += 1; }
                                else if got.basis == "bm25" { alone += 1; }
                            }
                        }
                    }
                    it.dt = Some(merged);
                }
                note!("[전파][업무분류] seed={} embed기여={} 벡터없음={}", dt_seed_n, contributed, no_vector);
                if bm_thr.is_some() {
                    note!("[전파][업무분류][bm25] 후보={} 합의={} 단독={} 파일명단독={}",
                          picked, agree, alone, name_only);
                }
            }
        }
    }

    // 기준 문서 등록이면 문서마다 제목(첫 줄)을 떠 둔다 — 아래에서 본문이 사라지기 전에.
    let seed_titles: std::collections::HashMap<String, String> = if seed_plan.is_empty() {
        Default::default()
    } else {
        items.iter().filter_map(|it| it.sig.as_ref().map(|sig|
            (seedstore::norm_file(&it.file), bm25::title_of(&it.text, sig)))).collect()
    };

    // 3차: 집계 + 출력 레코드 조립.
    let (mut c, mut s_, mut o_, mut none) = (0u32, 0u32, 0u32, 0u32);
    // 업무분류 집계(7-3 뿌리 카테고리 롤업). 이 축이 한 건도 안 돌았으면 0 으로
    // 남아 요약에서 섹션 자체가 빠진다 — "축을 안 씀"과 "미분류"의 구분(4-6).
    let mut dt_total = 0u32;
    let mut dt_unclassified = 0u32;
    let mut dt_root_totals: std::collections::BTreeMap<String, u32> = Default::default();
    let mut dt_node_totals: std::collections::BTreeMap<(String, String), u32> = Default::default();
    // 압축 '자체'의 집계 등급용: origin(최상위 압축) → 내부 파일 최종등급 목록.
    let mut arch_members: std::collections::BTreeMap<String, Vec<Option<Grade>>> = Default::default();
    // 압축별 업무분류 분포: {origin: {dc_id: 건수}}. 보안등급과 달리 서열이 없어
    // 대표 하나를 못 고르므로, 고르지 않고 분포를 그대로 모은다(파이썬 판과 같다).
    let mut arch_doctypes: std::collections::BTreeMap<String, std::collections::BTreeMap<String, u32>> =
        Default::default();
    let mut records: Vec<Value> = vec![];
    // --simple 일 때만 채운다 — 감사용 전체 결과 파일에 쓸 원본 레코드.
    let mut full_records: Vec<Value> = vec![];
    // 등급 스트림 기록기 — --write-grade-stream 일 때만 만든다(꺼진 실행은 파일을 건드리지 않는다).
    // 전파까지 끝난 이 루프가 등급이 최종인 자리라 여기서 쓴다(설계 §10).
    let mut grade_marker = if opts.write_grade_stream {
        Some(gradestream::Marker::new(
            opts.grade_stream_unclassified.as_deref() == Some("none"),
            // 기본은 시각 보존 — --grade-stream-update-time 일 때만 수정 시각이 바뀐다
            !opts.grade_stream_update_time))
    } else { None };
    // 사람 결정 목록 — 판정 · 전파가 끝난 이 루프에서 사람 등급이 이긴다(설계: 사람등급지정 §03).
    let mut ov_applier = match &opts.grade_overrides {
        Some(p) => match gradeoverride::load(p) {
            Ok(idx) => {
                note!("[MpowerClassify-rs] 사람 결정 목록: {} · 보안등급 결정 {}건{}", p, idx.latest.len(),
                      if idx.broken > 0 { format!(" · 읽지 못한 줄 {}건", idx.broken) } else { String::new() });
                Some(gradeoverride::Applier::new(idx))
            }
            Err(e) => errcodes::fail("grade_overrides_missing",
                &format!("[MpowerClassify-rs] 사람 결정 목록을 읽지 못했습니다: {} ({})", p, e), Some(p)),
        },
        None => None,
    };
    for it in items {
        let mut it = it;
        // 사람 결정을 먼저 적용해, 아래 등급별 건수 · 압축 집계가 사람 등급을 쓰게 한다.
        if let Some(ap) = ov_applier.as_mut() {
            if ap.apply(&mut it.rec) {
                it.grade = it.rec.get("grade").and_then(|v| v.as_str()).and_then(Grade::from_str_exact);
            }
        }
        match it.grade {
            Some(Grade::C) => c += 1,
            Some(Grade::S) => s_ += 1,
            Some(Grade::O) => o_ += 1,
            None => none += 1,
        }
        // 압축에서 나온 문서면 그 압축의 집계에 등급을 넣는다.
        if let Some(og) = &it.origin {
            arch_members.entry(og.clone()).or_default().push(it.grade);
        }
        let mut rec = it.rec;
        if let Some(sig) = &it.dt {
            dt_total += 1;
            let pairs = doctype_breakdown(sig);
            if pairs.is_empty() { dt_unclassified += 1; }
            // 압축에서 나온 문서면 그 압축의 업무분류 분포에 더한다.
            // 축이 안 돌았으면 이 갈래에 아예 안 들어오므로, '축 미사용'과
            // '축은 돌았는데 못 맞힘'이 결과에서 구분된다.
            if let Some(og) = &it.origin {
                let bucket = arch_doctypes.entry(og.clone()).or_default();
                let ids = doctype_ids(sig);
                if ids.is_empty() {
                    // 축은 돌았는데 라벨이 안 붙은 문서 — 그것도 사실이라 따로 센다.
                    *bucket.entry("unclassified".to_string()).or_insert(0) += 1;
                } else {
                    for dc in ids {
                        *bucket.entry(dc).or_insert(0) += 1;
                    }
                }
            }
            for (root, node) in pairs {
                *dt_root_totals.entry(root.clone()).or_insert(0) += 1;
                *dt_node_totals.entry((root, node)).or_insert(0) += 1;
            }
            let mut d = sig.as_dict();
            if let Some(ov) = &doctype_strategy_override {
                d["strategy"] = json!(ov);
            }
            // 전파 신호는 그 축 안에 둔다(파이썬 판과 같은 자리).
            if let Some(e) = it.dt_embed.clone() {
                d["embed"] = e;
            }
            if !rec.get("why").map_or(false, |w| w.is_object()) {
                rec["why"] = json!({});
            }
            rec["why"]["doctype"] = d;
        }

        // --with-vector(전량) · --embed-needed(못 정한 문서만): 문서벡터를 레코드에
        // 실어 보낸다(cli.py 의 rec["vector"] 와 같은 형식).
        //   f32 를 f64 로 넓혀 담는다 — 값 자체는 f32 그대로라 손실이 없고,
        //   파이썬 쪽 numpy float32 → float 변환과 같은 수를 낸다.
        //   모델 로드에 실패했거나 추출 텍스트가 비어 벡터가 없으면 필드를 아예 넣지 않는다
        //   ('벡터 없음'을 빈 배열로 적으면 0차원 벡터와 구분이 안 된다).
        //
        // [2026-09-02] --embed-needed 를 조건에 더했다. 이 인자는 파이썬에서 "못 정한
        // 문서의 벡터를 결과에 실어 달라"는 뜻인데(cli.py 는 벡터를 만들었으면 언제나
        // 싣는다), Rust 는 --with-vector 만 봐서 그 벡터가 나갈 구멍이 없었다. 실측에서
        // 임베딩에 1.1초를 쓰고도 결과 레코드가 바이트 단위로 똑같았다 — 순수한 낭비다.
        // 파이썬처럼 '만들었으면 언제나'로 하지 않은 이유: 그러면 아무 인자도 안 준
        // 기본 실행(UI 경로)에서도 미분류 문서마다 384개 실수가 붙어 결과 파일이 커진다.
        // 인자를 명시한 실행에서만 싣는다 — 기존 기본 출력은 그대로 둔다.
        if opts.with_vector || opts.embed_needed {
            if let Some(v) = &it.vector {
                rec["vector"] = Value::Array(v.iter().map(|x| json!(*x as f64)).collect());
            }
        }

        // 등급 스트림은 출력 모양(--summary · --simple)과 상관없이 쓴다 — 기록은 판정의 결과다.
        if let Some(gm) = grade_marker.as_mut() {
            gm.apply(&mut rec, it.origin.is_some());
        }
        if opts.summary_only { continue; }
        let out_rec = if opts.simple {
            // 읽는 차례로 넣는다 — 무엇을(file·hash·doc_id) → 어떻게 됐나(grade·doctype)
            // → 왜(why). Cargo.toml 의 serde_json preserve_order 가 이 차례를 지켜 준다
            // (기본값은 사전순이라 conf 가 업무분류 값 사이에 끼는 식으로 읽혔다).
            let mut m = serde_json::Map::new();
            m.insert("file".into(), rec["file"].clone());
            m.insert("hash".into(), rec.get("hash").cloned().unwrap_or(Value::Null));
            // 문서 ID 축이 돌았을 때만 doc_id 키를 붙인다(--no-doc-id 면 키 자체가 없다).
            // doctype 과 같은 규약 — "못 얻었다(null)"와 "축을 안 썼다(키 없음)"를 가른다.
            //
            // [왜 sfile_id 일 때만 값을 싣나] 폴백 doc_id(content)는 '파일 SHA-256 앞
            // 40자'라서 이미 hash 칸에 들어 있다(doc_id == hash[:40]). 한 칸 더 실어 봐야
            // 새 정보가 없고, 오히려 MpowerV11 문서 ID 처럼 생긴 값이 적재 쪽으로 흘러가
            // 같은 문서가 두 건으로 들어가는 사고를 부른다. 받는 쪽은 null 이면 hash 로
            // 문서를 가리면 된다.
            if let Some(src) = rec.get("doc_id_source") {
                let v = if src == "sfile_id" {
                    rec.get("doc_id").cloned().unwrap_or(Value::Null)
                } else {
                    Value::Null
                };
                m.insert("doc_id".into(), v);
            }
            // 엠파워 v11 의 원래 파일 이름 — 줬을 때만 싣는다(없던 실행의 줄 모양은 그대로다).
            // 신원 세 칸(file · hash · doc_id) 뒤에 둔다 — 그 차례는 받는 쪽과의 약속이다.
            if let Some(n) = rec.get("name") { m.insert("name".into(), n.clone()); }
            m.insert("grade".into(), match record::grade_of(&rec) {
                Some(g) => json!(g), None => Value::Null });
            // 업무분류 축이 돌았을 때만 doctype 키를 붙인다. 축을 안 쓰는 배포에서 빈
            // 배열이 나가면 "분류를 못 했다"와 "축을 안 썼다"가 구분되지 않는다.
            if record::doctype(&rec).is_some() {
                m.insert("doctype".into(), Value::Array(record::doctype_ids(&rec)));
            }
            // 못 읽은 문서에는 그 사실을 함께 싣는다. 없으면 '읽었는데 미분류'와
            // 글자 그대로 같은 모습이라, --simple 만 받는 쪽은 스캔본을 영영
            // 못 가려낸다. 성공한 문서에는 이 칸이 아예 없다(기존 모양 그대로).
            if let Some(e) = rec.get("error") {
                m.insert("error".into(), e.get("reason").cloned()
                    .unwrap_or_else(|| Value::String("extract_failed".into())));
            }
            // 처리제외(스캔 PDF) 표식도 싣는다 — 없으면 '읽었는데 미분류'와 같은 모습이라
            // 받는 쪽이 OCR 대상을 못 가린다. 기계가 가를 수 있게 종류(kind)만 싣는다.
            if let Some(ex) = rec.get("excluded") {
                m.insert("excluded".into(), ex.get("kind").cloned()
                    .unwrap_or_else(|| Value::String("excluded".into())));
            }
            if opts.simple_why {
                m.insert("why".into(), why_record(&rec));
            }
            // 축약본만으로는 "왜 이 등급인가"를 나중에 댈 수 없다 — 원본을 따로
            // 들고 있다가 감사용 파일로 함께 남긴다(분류를 다시 하지 않는다).
            full_records.push(order_record(&rec));
            Value::Object(m)
        } else { order_record(&rec) };
        records.push(out_rec);
    }
    // 압축 '자체'의 집계 레코드를 내부 문서들 뒤에 덧붙인다(파이썬 판과 같은 차례).
    // 이게 없으면 결과에 압축파일이 아예 안 보여, "이 zip 은 검사했나"에 답할 수 없다.
    let n_archives = arch_members.len();
    for (origin, grades) in &arch_members {
        let mut arec = archive_record(origin, grades, arch_doctypes.get(origin));
        // 압축 파일 자체에 사람 결정이 있으면 그것이 이긴다.
        if let Some(ap) = ov_applier.as_mut() {
            ap.apply(&mut arec);
        }
        // 압축 파일 자체에는 집계 등급을 쓴다(엔진은 zip 을 열 때 zip 의 스트림을 본다).
        if let Some(gm) = grade_marker.as_mut() {
            gm.apply(&mut arec, false);
        }
        records.push(order_record(&arec));
    }

    // 목록에 있으나 없는 파일 — 결과에도 한 줄씩 남긴다(F4 를 stderr 에만 두지 않는다).
    // 예전에는 이런 문서가 결과에서 통째로 사라져, 7건을 요청했는데 1건만 돌아와도
    // 결과 파일만 보는 쪽은 "요청한 만큼 다 됐다"고 믿었다.
    let mut n_missing_file: usize = 0;
    if filelist_is_target(&opts) {
        if let Some(fl) = flist.as_ref() {
            for (path, sfid) in fl.missing_entries() {
                let mut mrec = missing_file_record(&path, &sfid, &rs.version);
                // 없는 파일은 쓸 대상이 아니다 — 파이썬 판처럼 skipped(not_read)로 표시만 한다
                if let Some(gm) = grade_marker.as_mut() {
                    gm.apply(&mut mrec, false);
                }
                records.push(order_record(&mrec));
                n_missing_file += 1;
                none += 1;
            }
        }
        if n_missing_file > 0 {
            note!("[MpowerClassify-rs] 목록에 있으나 파일이 없어 처리하지 못함: {}건 \
— 결과에 error.kind=file_missing 으로 남겼습니다", n_missing_file);
        }
    }

    // 기준 문서 등록 모드는 여기가 본론이다 — 위의 분류는 지문과 벡터를 얻는
    // 과정이었을 뿐이고, 이제 그 결과를 기준 문서 줄로 바꿔 쓴다.
    if !seed_plan.is_empty() {
        let code = finish_seed_add(&opts, &seed_plan, &records, &seed_titles);
        // 한 건도 못 넣은 까닭이 '모델을 못 올림'이면 그것을 알린다 — 대응(설치 점검)이
        // 문서 탓(embed_failed)과 다르다. 둘 다 종료코드 2 라 번호만 달라진다.
        if code == errcodes::exit_of("model_load_failed") {
            if let Some(m) = model_fail.as_ref() {
                errcodes::set_status("model_load_failed", m);
            }
        }
        // 등록 모드도 끝을 상태 줄로 알린다(예전에는 바로 exit 해 줄이 없었다).
        std::process::exit(errcodes::finish(code, None));
    }

    if let Some((seeds_n, decided, still)) = prop_stats {
        note!("[전파] seed={} 전파결정={} 미분류잔여={}", seeds_n, decided, still);
    }

    // 없는 파일도 '다룬 문서'로 센다 — 요청받은 문서이기 때문이다. total 에서 빼면
    // 부르는 쪽이 "7건 요청했는데 total 이 1" 인 것을 눈치챌 수단이 사라진다.
    let total = files.len() + n_missing_file;
    let detected = c + s_ + o_;
    let total_ms = round1(t0.elapsed().as_secs_f64() * 1000.0);
    let mut summary = json!({
        "total": total, "detected": detected,
        "C": c, "S": s_, "O": o_, "unclassified": none,
        "extract_failed": fail,
        "elapsed_total_ms": total_ms, "rule_version": rs.version
    });
    // 크기 상한에 걸린 문서는 '있을 때만' 요약에 싣는다 — 0건일 때 칸이 늘면
    // 기존 요약을 읽던 스크립트·눈이 괜히 흔들린다(파이썬 판과 동일한 규칙).
    // 목록에 있으나 없던 파일 — 있을 때만 싣는다(0건이 정상이라 칸이 늘면 헷갈린다).
    if n_missing_file > 0 {
        summary["file_missing"] = json!(n_missing_file);
    }
    // 복호화 — 쓴 실행에서만 싣는다(엠파워 v11 암호화 문서).
    if n_dec > 0 || n_dec_fail > 0 {
        summary["decrypted"] = json!(n_dec);
        summary["decrypt_failed"] = json!(n_dec_fail);
    }
    if n_text_truncated > 0 {
        summary["text_truncated"] = json!(n_text_truncated);
    }
    if n_size_skipped > 0 {
        summary["size_skipped"] = json!(n_size_skipped);
    }
    if n_partial_extract > 0 {
        summary["partial_extract"] = json!(n_partial_extract);
    }
    // 처리제외는 extract_failed 에 들어 있지 않다(실패가 아니다) — 따로 싣는다.
    if n_excluded > 0 {
        summary["excluded"] = json!(n_excluded);
    }
    if n_split_volume > 0 {
        summary["split_volume"] = json!(n_split_volume);
    }
    if !arc_stats.partial.is_empty() {
        summary["archive_partial"] = json!(arc_stats.partial.len());
    }
    // 압축을 펼쳤으면 몇 건이었는지, 못 펼친 것이 있으면 그것도 싣는다.
    if n_archives > 0 {
        summary["archives"] = json!(n_archives);
    }
    if !arc_stats.unexpanded.is_empty() {
        summary["archive_unexpanded"] = json!(arc_stats.unexpanded.len());
    }
    if !arc_stats.unsupported.is_empty() {
        summary["archive_unsupported"] = json!(arc_stats.unsupported.len());
    }
    // 등급 스트림 — 옵션을 켠 실행에서만 싣는다(끈 실행의 요약은 글자 하나 다르지 않다).
    if let Some(gm) = grade_marker.as_ref() {
        summary["grade_stream"] = gm.summary();
    }
    // 사람 결정 — --grade-overrides 를 준 실행에서만.
    if let Some(ap) = ov_applier.as_ref() {
        summary["grade_overrides"] = ap.summary();
    }
    if dt_total > 0 {
        let by_root: serde_json::Map<String, Value> = dt_root_totals.iter()
            .map(|(k, v)| (k.clone(), json!(v))).collect();
        summary["doctype"] = json!({
            "total": dt_total,
            "unclassified": dt_unclassified,
            "by_root": by_root,
            "doctype_rule_version": dt_axis.as_ref().map(|(_, d)| d.version.clone()),
            "taxonomy_version": dt_axis.as_ref().map(|(t, _)| t.exported_at.clone()),
        });
    }

    // 상태 줄에 실을 건수를 여기서 먼저 남긴다 — 아래 status_object 가 그 값을 쓴다.
    // 상태 줄의 '실패' 건수에는 없는 파일도 넣는다 — 부르는 쪽에는 둘 다
    // "요청했는데 결과를 못 받은 문서"로 같은 뜻이다(파이썬 판과 동일).
    errcodes::set_counts(total as i64, fail as i64 + n_missing_file as i64 + n_dec_fail as i64);
    // 정책 버전도 함께 — 결과만 있고 '어떤 규칙으로 판정했는지'가 없으면 재현할 수 없다.
    errcodes::set_versions(&[
        ("rule_version", Some(rs.version.clone())),
        ("taxonomy_version", dt_axis.as_ref().map(|(t, _)| t.exported_at.clone())),
        ("doctype_rule_version", dt_axis.as_ref().map(|(_, d)| d.version.clone())),
    ]);
    let target = opts.file.clone().or_else(|| opts.dir.clone());
    // 못 읽은 문서와 아예 없던 문서는 부르는 쪽에 같은 뜻이다 —
    // "요청했는데 결과를 못 받았다". 둘 중 하나라도 있으면 종료코드를 올린다.
    let mut code = if fail > 0 || n_missing_file > 0 {
        errcodes::exit_of("extract_failed")
    } else { 0 };
    // 풀지 못한 문서가 있으면 그것을 까닭으로 알린다(종료코드는 같은 1 이고 번호만 3005 다).
    if n_dec_fail > 0 {
        errcodes::set_status("decrypt_failed", &format!(
            "[MpowerClassify-rs] 복호화하지 못한 문서 {}건 — 결과에 error.kind=decrypt_failed 로 남겼습니다",
            n_dec_fail));
        code = errcodes::exit_of("decrypt_failed");
    }
    // 모델 실패는 추출 실패보다 먼저 알린다 — 대응(설치 점검)이 배치 전체에 걸린다.
    // 못 읽은 문서 건수는 상태 줄의 total·failed 에 그대로 남는다.
    if let Some(m) = model_fail.as_ref() {
        errcodes::set_status("model_load_failed", m);
        code = errcodes::exit_of("model_load_failed");
    }
    // 등급 스트림을 못 쓴 문서가 있으면 알린다(4002, 종료코드 1). 결과 파일은 온전하다.
    // 다른 실패가 이미 있으면 그쪽 까닭을 그대로 둔다 — 건수는 요약 grade_stream 에 남는다.
    if let Some(gm) = grade_marker.as_ref() {
        if gm.failed() > 0 && code == 0 {
            errcodes::set_status("grade_stream_write_failed", &format!(
                "[MpowerClassify-rs] 등급 스트림을 쓰지 못한 문서 {}건 — 결과의 grade_stream.reason 을 보세요",
                gm.failed()));
            code = errcodes::exit_of("grade_stream_write_failed");
        }
    }

    // 출력 조립. --out 으로 저장할 때는 상태도 파일 안에 남긴다 — 파일만 받아
    // 나중에 읽는 쪽은 stdout 을 이미 흘려보낸 뒤라, 파일 자체가 "이 결과가
    // 온전한가"를 말해 줘야 한다. --out 이 없으면 결과가 stdout 으로 나가고
    // 상태 줄도 거기 붙으므로 여기서 또 넣지 않는다(같은 줄이 두 번 나간다).
    let status = if errcodes::quiet() && opts.out.is_some() {
        Some(errcodes::status_object(code, target.as_deref()))
    } else {
        None
    };
    // 실행 헤더 한 줄 — 이번 실행이 어떤 기준으로 판정했는가.
    //
    // 규칙셋 버전 세 칸은 한 번 실행하면 모든 줄이 같은 값이라, 예전에는 레코드마다
    // 같은 글자를 되풀이해 적었다(실측: 결과 파일의 7.9%가 meta 였고 그중 버전 세
    // 칸이 6.1%였다). 요약 줄에도 같은 값이 있어, 18개 문서면 같은 버전이 19번
    // 적히는 셈이었다.
    //
    // [왜 요약이 아니라 헤더인가] 요약은 맨 뒤에 나온다. jsonl 을 한 줄씩 처리하는
    // 쪽은 문서를 다 읽은 뒤에야 "어느 규칙으로 판정된 것인지"를 알게 되고,
    // --nosummary 를 주면 요약 자체가 안 나가 버전이 어디에도 안 남는다.
    // 맨 앞 한 줄이면 두 문제가 다 없어진다(파이썬 판 _run_header 와 같은 모양).
    //
    // [축약본에는 넣지 않는다] --simple 은 받는 쪽과의 계약이라 줄 모양을 바꾸지
    // 않는다. 감사용 전체 파일(.full)에는 넣는다.
    // 파일별 판정 결과를 한 줄씩 남긴다. 파이썬 판 cli._emit 과 같은 내용이라,
    // 두 판의 로그를 나란히 놓고 어디서 갈렸는지 비교할 수 있다.
    // run_header 를 끼우기 '전'에 남기는 이유: 그 머리글은 결과가 아니라 이번
    // 실행의 정책 버전이라, 파일별 결과 줄 사이에 섞이면 세기가 어긋난다.
    // is_on() 으로 먼저 거르는 까닭은, 로그가 꺼져 있으면 JSON 직렬화(건당 수 KB)
    // 자체를 하지 말아야 하기 때문이다 — 수천 건이면 그 비용이 그대로 드러난다.
    if log::is_on() {
        // --simple 일 때 records 는 축약본이다. 로그에는 근거까지 있는 원본을
        // 남겨야 나중에 되짚을 수 있으므로, 있으면 full_records 를 쓴다.
        let logged = if full_records.is_empty() { &records } else { &full_records };
        for r in logged {
            linfo!("결과 {}", serde_json::to_string(r).unwrap_or_default());
        }
    }

    let run_header = {
        let mut m = serde_json::Map::new();
        m.insert("rule_version".into(), json!(rs.version));
        if let Some((taxonomy, drs)) = dt_axis.as_ref() {
            m.insert("doctype_rule_version".into(), json!(drs.version));
            m.insert("taxonomy_version".into(), json!(taxonomy.exported_at));
        }
        m.insert("started_at".into(), json!(now_iso()));
        json!({"run": Value::Object(m)})
    };
    if !opts.summary_only {
        if !opts.simple {
            records.insert(0, run_header.clone());
        }
        if !full_records.is_empty() {
            full_records.insert(0, run_header.clone());
        }
    }

    // 이번 실행의 요약. 로그를 뒤에서부터 읽을 때 '몇 건을 어떻게 끝냈나'가
    // 바로 보여야 하므로 결과 줄 다음에 한 줄로 남긴다.
    linfo!("요약 {}", serde_json::to_string(&summary).unwrap_or_default());
    let body = render(&records, &summary, &opts, status.as_ref());
    match &opts.out {
        Some(p) => {
            if let Err(e) = std::fs::write(p, &body) {
                errcodes::fail("output_write_failed",
                    &format!("[MpowerClassify-rs] 출력 저장 실패 {}: {}", p, e), Some(p));
            }
            // --simple 이면 전체 레코드를 '<out>.full.<확장자>' 에 함께 남긴다.
            //   · --out 이 가리키는 파일은 지금까지처럼 축약본이다(계약 유지)
            //   · 이 파일에는 --nosummary 와 무관하게 요약을 남긴다 — 연동용이
            //     아니라 '나중에 되짚어 보는' 파일이라 정책 버전이 있어야 한다
            if opts.simple {
                let fp = full_out_path(p);
                let mut fopts = opts.clone();
                fopts.simple = false;
                fopts.no_summary = false;
                fopts.summary_only = false;
                let fbody = render(&full_records, &summary, &fopts, status.as_ref());
                if let Err(e) = std::fs::write(&fp, &fbody) {
                    errcodes::fail("output_write_failed",
                        &format!("[MpowerClassify-rs] 감사용 전체 결과 저장 실패 {}: {}", fp, e),
                        Some(&fp));
                }
                note!("[MpowerClassify-rs] 감사용 전체 결과: {}", fp);
            }
        }
        None => { print!("{}", body); }
    }

    // --report-missing-id — sfile_id 를 못 얻은 문서를 따로 뽑아 둔다. 이 문서들은
    // 매핑 테이블 적재 대상이 아니므로(R14), 왜 못 얻었는지 확인할 수 있게 남긴다.
    if let Some(mp) = opts.report_missing_id.as_deref() {
        if !missing_id_rows.is_empty() {
            let body: String = missing_id_rows.iter()
                .map(|r| format!("{}
", r)).collect();
            match std::fs::write(mp, body) {
                Ok(_) => note!("[MpowerClassify-rs] 문서 ID 미획득 목록: {} ({}건)",
                                   mp, missing_id_rows.len()),
                // 본 작업은 끝난 뒤라, 리포트를 못 썼다고 결과까지 버릴 이유는 없다.
                Err(e) => note!("[MpowerClassify-rs] 미획득 목록을 쓰지 못했습니다: {}", e),
            }
        }
    }

    // 화면 요약(stderr)
    if !opts.no_summary {
        // 문서 ID 획득 현황 — 설계 §7-5-2-1. 폴백 건수를 등급 줄보다 먼저 보여 준다.
        // 이 수치가 조용히 커지면 매핑에 들어가지 못하는 문서가 쌓이는데, 아무 오류도
        // 나지 않아 알아채기 어려운 종류의 실패이기 때문이다(R14).
        if docid_on && (n_sfile + n_content + n_pathid) > 0 {
            eprintln!("[summary][문서 ID] sfile_id {} · 폴백(content) {} · 폴백(path) {}",
                      n_sfile, n_content, n_pathid);
            let n_fallback = n_content + n_pathid;
            if n_fallback > 0 {
                let hint = if opts.report_missing_id.is_some() { "" }
                           else { " --report-missing-id 로 목록 확인" };
                eprintln!("[summary][문서 ID] ※ 폴백 {}건은 매핑 테이블 적재 대상이 아닙니다.{}",
                          n_fallback, hint);
            }
            if n_case > 0 {
                eprintln!("[summary][문서 ID] 대소문자만 달라 목록과 이어진 문서 {}건(rematched_by=case)",
                          n_case);
            }
            if n_hash_mismatch > 0 {
                eprintln!("[summary][문서 ID] [F6] 목록의 hash 와 실제 파일이 다른 문서 {}건 — 수정된 것으로 보이며 같은 문서로 처리했습니다", n_hash_mismatch);
            }
        }
        // --no-timing 이면 총시간을 뺀다(Python 판 --no-timing 과 같은 뜻).
        // 시간은 실행마다 달라지는 값이라, 결과를 비교·기록할 때 걸리적거린다.
        // 절단·크기초과는 0건이 정상이라, 있을 때만 칸을 늘려 눈에 띄게 한다.
        let trunc_note = if n_text_truncated > 0 {
            format!(", 본문절단={}", n_text_truncated)
        } else { String::new() };
        let size_note = if n_size_skipped > 0 {
            format!(", 크기초과={}", n_size_skipped)
        } else { String::new() };
        let part_note = if n_partial_extract > 0 {
            format!(", 부분추출={}", n_partial_extract)
        } else { String::new() };
        let excl_note = if n_excluded > 0 {
            format!(", 처리제외={}", n_excluded)
        } else { String::new() };
        let arch_note = if n_archives > 0 { format!(", 압축 {}건", n_archives) } else { String::new() };
        let vol_note = if n_split_volume > 0 { format!(", 분할조각={}", n_split_volume) } else { String::new() };
        // --textsave 를 안 쓰면 칸을 아예 안 낸다(쓰는 사람만 보게).
        let ts_note = if text_save_dir.is_some() {
            let f = if n_text_save_failed > 0 { format!(", 저장실패={}", n_text_save_failed) }
                    else { String::new() };
            format!(", 텍스트저장={}{}", n_text_saved, f)
        } else { String::new() };
        let arch_bad = {
            let mut v = String::new();
            if !arc_stats.unexpanded.is_empty() { v += &format!(", 압축미해제={}", arc_stats.unexpanded.len()); }
            if !arc_stats.unsupported.is_empty() { v += &format!(", 압축미지원={}", arc_stats.unsupported.len()); }
            if n_dec > 0 || n_dec_fail > 0 { v += &format!(", 복호화 {}건(실패 {})", n_dec, n_dec_fail); }
            v
        };
        if opts.no_timing {
            eprintln!("[summary] 총 {}개 / 검출 {}, C={} S={} O={} 미분류={} 추출실패={}{}{}{}{}{}{}{}{}",
                total, detected, c, s_, o_, none, fail, excl_note, trunc_note, size_note, part_note, arch_note, arch_bad, vol_note, ts_note);
        } else {
            eprintln!("[summary] 총 {}개 / 검출 {}, C={} S={} O={} 미분류={} 추출실패={}{}{}{}{}{}{}{}{}, 총시간={}ms",
                total, detected, c, s_, o_, none, fail, excl_note, trunc_note, size_note, part_note, arch_note, arch_bad, vol_note, ts_note, total_ms);
        }
        // 사람 결정은 --grade-overrides 를 준 실행에서만 한 줄 더 낸다(파이썬 판과 같은 문구).
        if let Some(ap) = ov_applier.as_ref() {
            eprintln!("[summary][사람결정] 적용={} 확인={} 내용바뀜={} (목록 {}건)",
                ap.applied, ap.confirmed, ap.stale, ap.loaded);
        }
        // 등급 스트림은 켠 실행에서만 한 줄 더 낸다(파이썬 판과 같은 문구).
        if let Some(gm) = grade_marker.as_ref() {
            eprintln!("[summary][등급스트림] written={} unchanged={} skipped={} failed={}",
                gm.counts["written"], gm.counts["unchanged"], gm.counts["skipped"], gm.counts["failed"]);
        }
        // 숫자만 던지면 운영자가 상한을 올려야 할지 그 문서를 따로 봐야 할지 판단할
        // 근거가 없다 — 무엇을 하면 되는지까지 알려 준다(파이썬 판과 같은 문구).
        // 저장은 곁다리라 결과를 안 바꾸지만, 몇 건이 어디에 남았는지는 말해야 한다.
        // 중복 건수를 함께 내는 이유: 폴더의 .txt 개수가 문서 수보다 적은 것을 보고
        // "빠졌나" 의심하지 않게 하려는 것이다.
        if let Some(dir) = &text_save_dir {
            let dedup = if n_text_dedup > 0 {
                format!("(내용이 같은 {}건은 한 파일로 합쳐졌습니다)", n_text_dedup)
            } else { String::new() };
            eprintln!("[summary][텍스트 저장] {}건을 {} 에 남겼습니다{}. \
원본 발췌이므로 공유·백업에 주의하세요.",
                      n_text_saved, dir.display(), dedup);
        }
        if n_text_save_failed > 0 {
            eprintln!("[summary][텍스트 저장 실패] {}건은 남기지 못했습니다 — \
분류 결과에는 영향이 없습니다. 사유는 레코드의 text_save_error 를 보세요.",
                      n_text_save_failed);
        }
        if n_text_truncated > 0 {
            eprintln!("[summary][본문 절단] {}건은 본문 앞 {}자만 보고 판정했습니다\
(text_truncated=true) — 재검토 대상입니다. 상한 조정은 --max-text-chars 로 합니다.",
                n_text_truncated, text_limit);
        }
        if n_size_skipped > 0 {
            eprintln!("[summary][크기 초과] {}건은 원본이 상한보다 커서 읽지 않았습니다\
(error.kind=size_limit) — 상한 조정은 --max-file-mb (텍스트 파일은 --max-file-mb-text) 로 합니다.",
                n_size_skipped);
        }
        // 분할 압축 조각 — 깨진 파일이 아니라 '원래 단독으로 못 읽는 것'이다.
        if n_split_volume > 0 {
            eprintln!("[summary][분할 조각] {}건은 분할 압축의 조각이라 읽지 않았습니다(error.kind=split_volume) — 파일이 깨진 것이 아닙니다. 이 판은 조각을 이어 붙이지 않으므로, 원본을 한 파일로 합친 뒤 넣어야 내부 문서가 분류됩니다.",
                n_split_volume);
        }
        // G4 — 가장 조용한 손실. 압축 안 문서 수백 건이 통째로 빠졌을 수 있다.
        if !arc_stats.unexpanded.is_empty() {
            // 상한 초과와 그 밖의 실패는 처방이 달라 문구를 나눈다.
            let n_limit = arc_stats.unexpanded.iter().filter(|(_, _, k)| *k == "limit").count();
            let n_vol = arc_stats.unexpanded.iter().filter(|(_, _, k)| *k == "split_volume").count();
            let n_error = arc_stats.unexpanded.len() - n_limit - n_vol;
            if n_limit > 0 {
                eprintln!("[summary][압축 미해제] {}건은 해제 상한을 넘어 내부 파일로 펼치지 못했습니다(archive_unexpanded=true) — 그 압축 안의 문서는 한 건도 분류되지 않았습니다. 상한 조정은 --max-archive-mb · --max-archive-members 로 합니다.",
                    n_limit);
            }
            if n_vol > 0 {
                eprintln!("[summary][분할 조각] {}건은 분할 압축의 첫 조각이라 펼치지 못했습니다(archive_unexpanded=true, kind=split_volume) — 파일이 깨진 것이 아닙니다. 원본을 한 파일로 합친 뒤 넣어야 내부 문서가 분류됩니다.",
                    n_vol);
            }
            if n_error > 0 {
                eprintln!("[summary][압축 확장 실패] {}건은 압축을 펼치지 못했습니다(archive_unexpanded=true, kind=error) — 그 압축 안의 문서는 한 건도 분류되지 않았습니다. 손상·암호·분할볼륨이거나 지원하지 않는 포맷일 수 있습니다. 사유는 각 레코드의 archive_unexpanded_reason 을 보세요.",
                    n_error);
            }
        }
        // 펼치기는 했지만 일부 항목을 못 꺼낸 압축(rar 하드링크 등).
        if !arc_stats.partial.is_empty() {
            eprintln!("[summary][압축 일부 누락] {}건은 펼쳤지만 내부 항목 일부를 꺼내지 못했습니다 — 하드링크·심볼릭링크는 이 판이 파일로 내놓지 않습니다. 원본 파일은 그대로 분류되었으니, 같은 내용의 다른 이름만 빠집니다.",
                arc_stats.partial.len());
        }
        // 지원하지 않는 포맷. 조용히 넘기면 '검사했다'는 착각을 준다.
        if !arc_stats.unsupported.is_empty() {
            eprintln!("[summary][압축 미지원] {}건은 이 판(Rust)이 펼치지 못하는 압축(rar)이라 내부 문서가 분류되지 않았습니다 — 파이썬 판으로 처리하세요.",
                arc_stats.unsupported.len());
        }
        // G5 — 끝까지 못 읽은 문서. 등급은 나왔지만 뒷부분을 안 본 판정이라
        // 재검토 대상이며, 손볼 상한값이 본문 절단(G3)과 다르다.
        if n_partial_extract > 0 {
            eprintln!("[summary][부분 추출] {}건은 파서가 상한에서 멈춰 문서 앞부분만 읽었습니다(partial_extract=true) — 재검토 대상입니다. 상한 조정은 --max-pdf-pages · --parser-timeout 으로 합니다.",
                n_partial_extract);
        }
        // 처리제외 — 실패가 아니므로 무엇을 뜻하는지(종료코드에 안 들어간다)까지 알린다.
        if n_excluded > 0 {
            eprintln!("[summary][처리제외] {}건은 스캔 PDF(텍스트 레이어 없음)라 OCR 미적용 범위로 처리에서 제외했습니다(excluded.kind=scan_pdf) — 실패가 아니므로 종료코드에 넣지 않았습니다. 등급은 파일명·경로 신호로만 판정했습니다.",
                n_excluded);
        }
        // 업무분류 롤업 — 뿌리 카테고리별 총계(괄호 안은 실제 걸린 노드별 내역) + 미분류.
        if dt_total > 0 {
            let mut roots: Vec<(&String, &u32)> = dt_root_totals.iter().collect();
            roots.sort_by(|a, b| b.1.cmp(a.1).then_with(|| a.0.cmp(b.0)));
            let parts: Vec<String> = roots.iter().map(|(root, total)| {
                let mut nodes: Vec<(&String, &u32)> = dt_node_totals.iter()
                    .filter(|((r, _), _)| &r == root)
                    .map(|((_, n), c)| (n, c)).collect();
                nodes.sort_by(|a, b| b.1.cmp(a.1).then_with(|| a.0.cmp(b.0)));
                let breakdown: Vec<String> = nodes.iter().map(|(n, c)| format!("{} {}", n, c)).collect();
                format!("{} {} ({})", root, total, breakdown.join(" · "))
            }).collect();
            // 한 건도 못 맞히면 앞부분이 빈 문자열이 되어 "[summary][업무분류]  / 미분류 3"
            // 처럼 공백이 뜬다 — 값이 없다는 걸 말로 적어 준다(파이썬 판과 같은 문구).
            let head = if parts.is_empty() { "(분류된 문서 없음)".to_string() } else { parts.join(" / ") };
            eprintln!("[summary][업무분류] {} / 미분류 {}", head, dt_unclassified);
        }
    }

    // 본문을 못 읽은 문서가 하나라도 있으면 종료코드 1 로 끝낸다(계약상 3001).
    // 결과 파일은 정상적으로 만들어진다 — 실패한 문서도 레코드로 들어 있다.
    // [2026-09-01] 여기가 없어서 Rust 판은 추출에 실패해도 늘 0 을 냈다. 파이썬
    // 판·실행가이드는 1 을 약속하고 있었으므로, 배치가 두 판에서 다르게 굴렀다.
    std::process::exit(errcodes::finish(code, target.as_deref()));
}

/// json 배열 / jsonl / summary-only 를 MpowerClassify 와 같은 형태로 렌더.
/// 전파 입력(1차 결과 파일)을 레코드 목록으로 읽는다.
///
/// 1차 분류는 형식이 세 가지로 나올 수 있다 — `--dir` 기본은 JSON 배열, `--file` 기본은
/// 객체 1개, `--format jsonl` 은 한 줄 1건. 어느 것이든 그대로 받아야 사용자가
/// "1차를 어떻게 냈는지" 신경 쓰지 않아도 된다(파이썬 run_propagate 와 같은 순서).
///   1) 파일 전체를 JSON 으로 파싱 → 배열이면 그대로, 객체면 1건
///   2) 실패하면 jsonl 로 폴백(깨진 줄은 건너뛰고 알린다)
/// 마지막 `{"summary":{...}}` 줄은 문서가 아니므로 걸러낸다 — 안 거르면 전파 통계가
/// 문서 1건을 더 센 것처럼 나온다.
fn load_propagate_input(path: &str) -> Vec<Value> {
    let raw = match std::fs::read_to_string(path) {
        Ok(s) => s,
        Err(e) => {
            errcodes::fail("propagate_input_missing",
                &format!("[MpowerClassify-rs] 전파 입력 파일을 읽을 수 없습니다 {}: {}", path, e),
                Some(path));
        }
    };
    let mut recs: Vec<Value> = vec![];
    // 깨진 줄 수 — 한 줄도 못 읽었는데 성공(빈 결과)으로 끝나지 않도록 센다.
    let mut bad_lines = 0usize;
    match serde_json::from_str::<Value>(&raw) {
        Ok(Value::Array(a)) => recs = a,
        Ok(Value::Object(o)) => recs.push(Value::Object(o)),
        Ok(_) => {}
        Err(_) => {
            // jsonl 폴백.
            for (i, line) in raw.lines().enumerate() {
                let line = line.trim();
                if line.is_empty() { continue; }
                match serde_json::from_str::<Value>(line) {
                    Ok(v) => recs.push(v),
                    Err(e) => {
                        bad_lines += 1;
                        errlog::err(&format!("[MpowerClassify-rs] 전파 입력 {}행 파싱 실패: {}", i + 1, e));
                    }
                }
            }
        }
    }
    // 요약 줄 제거 — 'file' 이 있는 것만 문서 레코드로 본다.
    recs.retain(|r| r.get("file").map_or(false, |f| f.is_string()));
    // 깨진 줄만 있고 문서가 한 건도 없으면 '못 읽음'(1008)이다. 예전에는 빈 배열과
    // success 로 끝나, 부르는 쪽이 "전파할 문서가 없었다"로 잘못 읽었다.
    if recs.is_empty() && bad_lines > 0 {
        errcodes::fail("propagate_input_missing",
            &format!("[MpowerClassify-rs] 전파 입력을 읽지 못했습니다(문서 레코드 0건, 깨진 줄 {}줄): {}",
                     bad_lines, path),
            Some(path));
    }
    recs
}

/// 임베딩 라벨 전파 2차 패스(`--propagate`) — 파이썬 `run_propagate` 대응.
///
/// `--with-vector` 로 만든 1차 레코드(벡터 포함)를 읽어, 고신뢰 등급을 seed 삼아
/// 미분류 문서에 라벨을 전파하고 등급을 다시 융합해 출력한다.
/// **임베딩 모델이 필요 없다** — 이미 뽑힌 벡터로 벡터연산만 한다. 그래서 모델을
/// 못 올리는 서버에서도 1차 결과만 옮겨 와 전파를 돌릴 수 있다.
///
/// -in: opts = 파싱된 CLI 옵션(propagate=입력파일, seeds/out/fmt/failsafe/taxonomy/doc-rules)
///
/// -out: 종료코드(정상 0, 입력·seed 파일 문제 3, 축 검증 실패 4)
fn run_propagate(opts: &Opts) -> i32 {
    let input = opts.propagate.as_deref().unwrap_or("");
    if !Path::new(input).is_file() {
        errcodes::fail("propagate_input_missing",
            &format!("[MpowerClassify-rs] 전파 입력 파일이 없습니다: {}", input), Some(input));
    }
    let mut records = load_propagate_input(input);

    // 업무분류 축은 분류 때와 같은 규칙으로 켜고 --conflict 도 똑같이 적용한다.
    let mut dt_axis = match load_doctype_axis(opts) {
        Ok(v) => v,
        Err(code) => return code,
    };
    let doctype_strategy_override = apply_conflict_override(opts, &mut dt_axis);

    // 외부 seed. --seeds 를 '명시했는데' 파일이 없으면 조용히 내부 seed 로 넘어가지
    // 않고 멈춘다 — 기준을 지정했는데 다른 기준으로 판정되면 결과를 믿을 수 없다.
    let seeds_path: Option<String> = opts.seeds.clone().or_else(default_seed_path);
    if let Some(p) = &opts.seeds {
        if !Path::new(p).is_file() {
            errcodes::fail("seeds_missing",
                &format!("[MpowerClassify-rs] seed 파일이 없습니다: {}", p), Some(p));
        }
    }
    // 1차 결과의 벡터와 seed 벡터가 같은 모델에서 나왔는지(차원) 본다. 전파 모드는 모델을
    // 올리지 않으므로 '이번 벡터'는 입력 레코드에 이미 실린 벡터다(파이썬 run_propagate 와 같다).
    let rec_dim = records.iter()
        .find_map(|r| r.get("vector").and_then(|v| v.as_array()).filter(|a| !a.is_empty()).map(|a| a.len()));
    if let (Some(d), Some(p)) = (rec_dim, seeds_path.as_deref()) {
        if Path::new(p).is_file() {
            if let Some(why) = seed_dim_problem(p, d, &format!("1차 결과 벡터({}차원)", d)) {
                errcodes::fail("seeds_model_mismatch", &format!("[MpowerClassify-rs] {}", why), Some(p));
            }
        }
    }
    let external = match &seeds_path {
        Some(p) if Path::new(p).is_file() => {
            let idx = propagate::SeedIndex::from_seed_file(p);
            note!("[MpowerClassify-rs] 외부 seed {}건 로드: {}", idx.size(), p);
            idx
        }
        _ => propagate::SeedIndex::empty(),
    };

    // 내부 seed(입력 코퍼스 안의 seed_eligible 문서) + 외부 seed 병합.
    let tuples: Vec<(Option<Vec<f32>>, Option<String>, bool, String)> = records.iter()
        .map(|r| (
            r.get("vector").and_then(vector_from_json),
            // 위와 같은 이유로 접근자를 쓴다 — 여기 입력은 1차 결과 '파일'이라
            // 옛 모양(최상위 grade/seed_eligible)도 섞여 들어올 수 있다.
            record::grade_of(r).map(|s| s.to_string()),
            record::seed_eligible_of(r),
            r.get("file").and_then(|f| f.as_str()).unwrap_or("").to_string(),
        ))
        .collect();
    let internal = propagate::SeedIndex::from_records(&tuples);
    let seeds = if external.size() > 0 {
        propagate::SeedIndex::merge(external, internal)
    } else {
        internal
    };

    // security 전파 — 이미 등급이 확정된 문서는 손대지 않는다(상향 전용, 과분류 방지).
    let (mut already, mut decided, mut still, mut novec) = (0u32, 0u32, 0u32, 0u32);
    for rec in records.iter_mut() {
        if rec.get("grade").map_or(false, |g| !g.is_null()) { already += 1; continue; }
        let vec = match rec.get("vector").and_then(vector_from_json) {
            Some(v) => v,
            None => { novec += 1; continue; }   // 보류인데 벡터도 없으면 전파 불가
        };
        let esig = propagate::propagate(&vec, &seeds);
        let dict = esig.as_dict();
        // refuse_with_embed 가 security 칸을 통째로 다시 만든다(옛 칸은 걷어낸다).
        let grade = rules::refuse_with_embed(rec, esig.grade.as_deref(), esig.confidence as f64,
                                             dict, opts.failsafe.as_deref());
        if record::decided_by(rec).and_then(|d| d.as_array())
              .map_or(false, |a| a.iter().any(|v| v.as_str() == Some("embed"))) {
            decided += 1;
        }
        if grade.is_none() { still += 1; }
    }

    // doctype 전파 — security 와 달리 '이미 후보가 있어도' embed 가 후보를 더한다
    // (다중 라벨은 상향이 아니라 추가가 원칙, 설계서 5-4).
    if let Some((taxonomy, drs)) = dt_axis.as_ref() {
        let mut dt_seeds = match &seeds_path {
            Some(p) => propagate::DoctypeSeedIndex::from_seed_file(p),
            None => propagate::DoctypeSeedIndex::empty(),
        };
        if dt_seeds.size() > 0 {
            note!("[MpowerClassify-rs] 업무분류 seed {}건 로드: {}",
                      dt_seeds.size(), seeds_path.clone().unwrap_or_default());
        }
        // 임계값·스위치는 정책 파일(embed: 블록)이 정한다. 1차 분류 경로와
        // 같은 값을 써야 "분류할 때와 다시 전파할 때 결과가 다른" 일이 없다.
        let dt_params = propagate::DoctypeParams {
            k: drs.embed.k as usize,
            dup_threshold: drs.embed.dup_threshold as f32,
            min_sim: drs.embed.min_sim as f32,
            min_share: drs.embed.min_share as f32,
        };
        if dt_seeds.size() > 0 && !drs.embed.enabled {
            note!("[propagate][업무분류] seed {}건이 있으나 doc_rule.yaml 의 \
embed.enabled 가 false 라 2단계를 건너뜁니다", dt_seeds.size());
            dt_seeds = propagate::DoctypeSeedIndex::empty();
        }
        let (mut contributed, mut dt_novec, mut axis_off) = (0u32, 0u32, 0u32);
        for rec in records.iter_mut() {
            // doctype 칸이 없으면 그 축을 안 쓴 배포의 레코드다 — 건너뛴다.
            let existing = match record::doctype(rec).and_then(|d| d.get("values")) {
                Some(v) => candidates_from_json(v),
                None => { axis_off += 1; continue; }
            };
            let vec = match rec.get("vector").and_then(vector_from_json) {
                Some(v) => v,
                None => { dt_novec += 1; continue; }
            };
            let esig = propagate::propagate_doctype(&vec, &dt_seeds, dt_params);
            let embed_dict = esig.as_dict();
            let embed_values: Vec<(String, f64)> = esig.values.iter()
                .map(|(id, c)| (id.clone(), *c as f64)).collect();
            let merged = doctype::merge_embed_candidates(&existing, &embed_values, taxonomy, &drs.conflict,
                                                       Some(drs.defaults.embed_cap),
                                                       (esig.method.as_str(), esig.top_sim as f64));
            if merged.values.iter().any(|v| v.from.iter().any(|f| f == "embed")) {
                contributed += 1;
            }
            let mut d = merged.as_dict();
            if let Some(ov) = &doctype_strategy_override {
                d["strategy"] = json!(ov);
            }
            // 전파 신호는 그 축 안에 둔다 — 예전에는 등급 신호들과 같은 signals
            // 상자에 doctype_embed 라는 이름으로 섞여 있어, 축이 둘이라는 사실이
            // 레코드 모양에서 드러나지 않았다(2026-09-10).
            d["embed"] = embed_dict;
            if !rec.get("why").map_or(false, |w| w.is_object()) {
                rec["why"] = json!({});
            }
            rec["why"]["doctype"] = d;
            if let Some(o) = rec.as_object_mut() { o.remove("doctype"); }
        }
        note!("[propagate][업무분류] seeds={} embed_contributed={} no_vector={} axis_off={}",
                  dt_seeds.size(), contributed, dt_novec, axis_off);
    }

    // 출력 — 전파 입력은 '레코드 묶음'이라 요약을 붙이지 않는다(파이썬과 동일).
    // json 은 항상 배열로 감싸 유효한 JSON 파일이 되게 한다.
    let records: Vec<Value> = records.iter().map(order_record).collect();
    let body = if opts.fmt == "jsonl" {
        let mut out = String::new();
        for r in &records {
            out.push_str(&serde_json::to_string(r).unwrap());
            out.push('\n');
        }
        out
    } else {
        format!("{}\n", serde_json::to_string_pretty(&records).unwrap())
    };
    match &opts.out {
        Some(p) => {
            // 상위 폴더가 없으면 만들어 둔다(분류·--text-only 와 같다). 못 만들면 아래 쓰기가 까닭을 알린다.
            if let Some(parent) = Path::new(p).parent() {
                if !parent.as_os_str().is_empty() { let _ = std::fs::create_dir_all(parent); }
            }
            if let Err(e) = std::fs::write(p, &body) {
                errcodes::fail("output_write_failed",
                    &format!("[MpowerClassify-rs] 출력 저장 실패 {}: {}", p, e), Some(p));
            }
        }
        None => print!("{}", body),
    }

    note!("[propagate] seeds={} already_graded={} embed_decided={} still_unclassified={} no_vector={}",
              seeds.size(), already, decided, still, novec);
    0
}

/// 레코드의 `vector` 필드(JSON 숫자 배열) → `Vec<f32>`.
/// 배열이 아니거나 비어 있으면 None — '벡터 없음'과 같이 취급해 전파에서 건너뛴다.
/// BM25 2차 재료 만들기(파이썬 cli._bm25_context 와 같은 규칙 · 같은 안내 문구).
/// bm25.enabled 일 때 기준 문서로 분류 단위 색인을 만들고 최소 점수를 정한다.
/// 반환: (색인, 문턱) — 문턱이 None 이면 그 실행은 BM25 를 건너뛴다. bm25 를 안 쓰면 None.
fn bm25_context(drs: &doc_rules::DocRuleSet, seeds_path: Option<&str>) -> Option<(bm25::Bm25Index, Option<f64>)> {
    let sp = &drs.bm25;
    let path = seeds_path?;
    if !sp.enabled || !std::path::Path::new(path).is_file() {
        return None;
    }
    let index = bm25::Bm25Index::from_seed_file(path, sp, &drs.head_noise);
    if sp.mode == "fixed" {
        note!("[전파][업무분류][bm25] 문턱 {:.3}(고정) · 기준 문서 {}건", sp.min_score, index.size());
        return Some((index, Some(sp.min_score)));
    }
    let auto = bm25::auto_threshold(&index, drs.embed.dup_threshold);
    match auto.threshold {
        None => note!("[전파][업무분류][bm25] 기준 문서가 부족해 문턱을 정하지 못해 이번 실행은 BM25 를 \
건너뜁니다(기준 문서 {}건 · 모의시험 {}건) — 기준 문서 50건 이상(분류마다 3건 이상)을 권합니다",
                      index.size(), auto.scored),
        Some(t) => note!("[전파][업무분류][bm25] 문턱 {:.3} — 기준 문서 {}건 자기 제외 모의시험(통과 {} · 정답 {} · {:.1}%)",
                         t, index.size(), auto.passed, auto.correct,
                         auto.correct as f64 / auto.passed as f64 * 100.0),
    }
    let t = auto.threshold;
    Some((index, t))
}

fn vector_from_json(v: &Value) -> Option<Vec<f32>> {
    let arr = v.as_array()?;
    if arr.is_empty() { return None; }
    let mut out = Vec::with_capacity(arr.len());
    for x in arr {
        out.push(x.as_f64()? as f32);
    }
    Some(out)
}

/// `labels.doctype.values`(JSON) → 병합에 넣을 후보 목록.
/// 형식이 어긋난 원소는 조용히 버린다 — 손으로 고친 결과 파일이 들어와도
/// 전파 자체가 죽지는 않아야 한다.
fn candidates_from_json(v: &Value) -> Vec<doctype::Candidate> {
    let arr = match v.as_array() { Some(a) => a, None => return vec![] };
    let mut out = vec![];
    for c in arr {
        let dc_id = match c.get("dc_id").and_then(|x| x.as_str()) { Some(s) => s.to_string(), None => continue };
        let (evidence, score_parts) = doctype::split_signals(c);
        out.push(doctype::Candidate {
            dc_id,
            path: c.get("path").and_then(|x| x.as_str()).unwrap_or("").to_string(),
            path_ids: c.get("path_ids").and_then(|x| x.as_array())
                .map(|a| a.iter().filter_map(|s| s.as_str().map(|s| s.to_string())).collect())
                .unwrap_or_default(),
            confidence: c.get("confidence").and_then(|x| x.as_f64()).unwrap_or(0.0),
            from: c.get("from").and_then(|x| x.as_array())
                .map(|a| a.iter().filter_map(|s| s.as_str().map(|s| s.to_string())).collect())
                .unwrap_or_default(),
            // 근거 블록은 왕복에서 살려 둔다 — 여기서 버리면 전파를 한 번
            // 거친 문서만 "왜 이 라벨인지"를 설명할 수 없게 된다.
            stage: c.get("stage").and_then(|x| x.as_str()).unwrap_or("rule").to_string(),
            // 레코드에는 근거가 signals 한 칸으로 합쳐져 있다(2026-09-10).
            // 엔진 속 계산은 두 칸으로 다루므로 여기서 되돌린다.
            evidence, score_parts,
            // 검토 대상 표시(13장 D1 ②)도 왕복에서 살린다 — 전파를 한 번 거쳤다고
            // 파일명만으로 붙은 약한 라벨이 확정 라벨로 둔갑하면 안 된다.
            review: c.get("review").and_then(|x| x.as_bool()).unwrap_or(false),
            basis: c.get("basis").and_then(|x| x.as_str()).unwrap_or("").to_string(),
        });
    }
    out
}

/// `--conflict` 로 축 전략을 실행 시 덮어쓴다(6-5). security 는 서열 축이라 금지(T11).
///
/// 분류 모드와 전파 모드가 같은 규칙으로 움직여야 하므로 함수로 뺐다 — 한쪽에만
/// 고쳐 넣으면 "같은 --conflict 를 줬는데 전파에서만 안 먹는" 상황이 생긴다.
/// 반환값(적용된 전략 문자열)은 결과 레코드에 그대로 남긴다: 같은 규칙셋인데
/// 결과가 다르면 왜 달랐는지 레코드만 보고 알 수 있어야 하기 때문이다.
fn apply_conflict_override(opts: &Opts, dt_axis: &mut Option<(Taxonomy, DocRuleSet)>) -> Option<String> {
    let spec = opts.conflict.as_ref()?;
    let (axis, parsed) = match parse_conflict_override(spec) {
        Ok(v) => v,
        Err(msg) => errcodes::fail("bad_conflict_axis",
            &format!("[MpowerClassify-rs] {}", msg), None),
    };
    if let Err(msg) = conflict::ensure_overridable_axis(&axis) {
        errcodes::fail("bad_conflict_axis", &format!("[MpowerClassify-rs] {}", msg), None);
    }
    if axis != "doctype" {
        errcodes::fail("bad_conflict_axis",
            &format!("[MpowerClassify-rs] --conflict 에 알 수 없는 축입니다: {:?}", axis), None);
    }
    if let Some((_, drs)) = dt_axis.as_mut() {
        drs.conflict = parsed;
    }
    spec.split_once('=').map(|(_, v)| v.trim().to_string())
}

fn render(records: &[Value], summary: &Value, opts: &Opts, status: Option<&Value>) -> String {
    if opts.summary_only {
        // 단일 요약 객체
        return format!("{}\n", serde_json::to_string_pretty(&json!({"summary": summary})).unwrap());
    }
    if opts.fmt == "jsonl" {
        let mut out = String::new();
        for r in records {
            out.push_str(&serde_json::to_string(r).unwrap());
            out.push('\n');
        }
        if !opts.no_summary {
            out.push_str(&serde_json::to_string(&json!({"summary": summary})).unwrap());
            out.push('\n');
        }
        if let Some(st) = status {
            out.push_str(&serde_json::to_string(st).unwrap());
            out.push('\n');
        }
        out
    } else {
        // json 배열(들여쓰기), 마지막에 summary 원소
        let mut arr: Vec<Value> = records.to_vec();
        if !opts.no_summary {
            arr.push(json!({"summary": summary}));
        }
        // 배열 '밖'에 객체를 붙이면 파일이 통째로 깨진다 — 마지막 원소로 넣는다.
        if let Some(st) = status {
            arr.push(st.clone());
        }
        format!("{}\n", serde_json::to_string_pretty(&arr).unwrap())
    }
}

fn round1(x: f64) -> f64 { (x * 10.0).round() / 10.0 }

#[cfg(test)]
mod tests {
    //------------------------------------------------------------------
    // ts 는 파이썬 판과 같은 모양이어야 한다
    //=> 두 판의 결과를 같은 표에 놓고 비교하므로, 이 필드만 모양이 다르면
    //   대조가 깨진다. 파이썬은 seedstore.TS_FMT("%Y-%m-%d %H:%M:%S")를 쓴다.
    //   밀리초가 붙거나 "T"·시간대 오프셋이 다시 들어오면 안 된다.
    //
    //   [2026-09-10 변경] 그 전에는 ISO-8601("2026-09-07T14:11:45+09:00")이었다.
    //   이 값을 파싱하는 코드가 두 판 어디에도 없어(보여 주기·문자열 정렬 전용)
    //   사람이 읽는 모양으로 바꿨다. 이 시험이 그때 옛 형식을 붙들고 있어서
    //   바꾼 사실을 바로 잡아 줬다 — 그래서 형식을 계속 여기에 못박아 둔다.
    //------------------------------------------------------------------
    #[test]
    fn now_iso_는_사람이_읽는_초단위_형식이다() {
        let t = super::now_iso();
        let re = regex::Regex::new(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$").unwrap();
        assert!(re.is_match(&t), "파이썬 판과 다른 모양이다: {}", t);
    }

    use super::*;
    use doctype::{Candidate, DoctypeSignal};

    fn sig(values: Vec<Candidate>) -> DoctypeSignal {
        DoctypeSignal { values, strategy: "all".into(), status: "proposed".into(),
                        truncated: 0, conflicts: vec![] }
    }

    fn cand(dc_id: &str, path: &str) -> Candidate {
        Candidate::bare(dc_id.into(), path.into(), vec![], 0.7, vec![])
    }

    //------------------------------------------------------------------
    // 없어진 옵션은 무엇으로 바꿔야 하는지 알린다 (설계 7단계)
    //=> 파이썬 판과 같은 문구여야 두 판을 같은 명령으로 부르는 쪽이 같은 답을 받는다.
    //   없어지지 않은 옵션(--export-input·--build-doc-rule)은 여기 걸리면 안 된다.
    //------------------------------------------------------------------
    #[test]
    fn 없어진_옵션은_바꿀_길을_알린다() {
        for f in ["--export-taxonomy", "--scaffold-doc-rule", "--sync-doc-rule",
                  "--no-fill-blank", "--sync-enrich"] {
            let m = removed_option_message(f).unwrap_or_else(|| panic!("{} 가 안 걸렸다", f));
            assert_eq!(m, format!("[MpowerClassify-rs] {} 는 없어졌습니다 — 업무분류 규칙은 \
--build-doc-rule 로 만듭니다(분류체계가 규칙 안에 들어갑니다).", f));
        }
        assert_eq!(removed_option_message("--taxonomy").unwrap(),
                   "[MpowerClassify-rs] --taxonomy 는 없어졌습니다 — 분류체계는 \
doc_rule.yaml(--build-doc-rule 로 만든 것) 안에 있습니다.");
        for keep in ["--export-input", "--build-doc-rule", "--doc-rules", "--check-rules"] {
            assert!(removed_option_message(keep).is_none(), "{} 는 살아 있는 옵션이다", keep);
        }
        // 코드 이름이 계약표에 있어야 종료 3 으로 나간다(없으면 errcodes 가 다른 값을 준다).
        assert_eq!(errcodes::code_of("unsupported_option"), 1012);
        assert_eq!(errcodes::exit_of("unsupported_option"), 3);
    }

    //------------------------------------------------------------------
    // 규칙 파일 모양 가리기 — 옛 모양은 Legacy, 자동 생성본은 Generated
    //=> 옛 모양을 조용히 읽거나 축을 끄면 업무분류가 소리 없이 비는데, 깨진 YAML 을
    //   '옛 모양'이라고 알리면 엉뚱한 처방(다시 만들기)을 준다. 셋을 갈라 둔다.
    //------------------------------------------------------------------
    #[test]
    fn 규칙_파일_모양을_가린다() {
        let dir = std::env::temp_dir().join(format!("csors_kind_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let legacy = dir.join("legacy.yaml");
        std::fs::write(&legacy, "conflict: all\ndoctype_rules:\n- id: dt_a\n  node: A\n  terms: [x]\n").unwrap();
        assert_eq!(rule_file_kind(&legacy), RuleFileKind::Legacy);
        let broken = dir.join("broken.yaml");
        std::fs::write(&broken, "doctype_rules: [\n  - 닫히지 않음").unwrap();
        assert_eq!(rule_file_kind(&broken), RuleFileKind::Unreadable);
        assert_eq!(rule_file_kind(&dir.join("없음.yaml")), RuleFileKind::Unreadable);
        // 저장소에 들어 있는 배포용 규칙은 --build-doc-rule 로 만든 것이다.
        let shipped = Path::new(env!("CARGO_MANIFEST_DIR")).join("resources/policy/doc_rule.yaml");
        assert_eq!(rule_file_kind(&shipped), RuleFileKind::Generated);
        let _ = std::fs::remove_dir_all(&dir);
    }


    
    
    #[test]
    fn doctype_breakdown은_뿌리와_말단만_뽑는다() {
        let pairs = doctype_breakdown(&sig(vec![cand("DC_1", "기술/개발 > 설계문서 > 요구사항정의서")]));
        assert_eq!(pairs, vec![("기술/개발".to_string(), "요구사항정의서".to_string())]);
    }

    #[test]
    fn doctype_breakdown은_경로가_비면_dc_id로_대체() {
        let pairs = doctype_breakdown(&sig(vec![cand("DC_9", "")]));
        assert_eq!(pairs, vec![("DC_9".to_string(), "DC_9".to_string())]);
    }

    #[test]
    fn conflict_덮어쓰기_파싱() {
        let (axis, spec) = parse_conflict_override("doctype=top_n:3").unwrap();
        assert_eq!(axis, "doctype");
        assert_eq!(spec.strategy, "top_n");
        assert_eq!(spec.n, Some(3));
        let (_, spec2) = parse_conflict_override("doctype=all").unwrap();
        assert_eq!(spec2.strategy, "all");
    }

    #[test]
    fn conflict_덮어쓰기_잘못된_값은_오류() {
        assert!(parse_conflict_override("doctype").is_err());       // '=' 없음
        assert!(parse_conflict_override("doctype=max").is_err());   // T2
        assert!(parse_conflict_override("doctype=top_n").is_err()); // T10(n 없음)
        assert!(parse_conflict_override("doctype=top_n:x").is_err());
    }

    // ---- --propagate 입력 파서 ----

    #[test]
    fn 전파입력은_요약줄을_문서로_세지_않는다() {
        // 1차 결과 jsonl 의 마지막 {"summary":{...}} 줄은 문서가 아니다. 이걸 문서로
        // 세면 전파 통계가 한 건 더 있는 것처럼 나오고, 결과 파일에도 '내용 없는
        // 문서' 한 줄이 따라다닌다(화면에서 빈 행으로 보이던 그 문제).
        let dir = std::env::temp_dir().join("csors_prop_test");
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("in.jsonl");
        std::fs::write(&p, "{\"file\":\"a.txt\",\"grade\":\"C\"}
{\"summary\":{\"total\":1}}
").unwrap();
        let recs = load_propagate_input(p.to_str().unwrap());
        assert_eq!(recs.len(), 1);
        assert_eq!(recs[0]["file"], json!("a.txt"));
    }

    #[test]
    fn 전파입력은_json배열도_객체하나도_받는다() {
        let dir = std::env::temp_dir().join("csors_prop_test");
        std::fs::create_dir_all(&dir).unwrap();
        // --dir 기본 출력(JSON 배열, 끝에 summary 원소)
        let a = dir.join("arr.json");
        std::fs::write(&a, "[{\"file\":\"a\"},{\"file\":\"b\"},{\"summary\":{}}]").unwrap();
        assert_eq!(load_propagate_input(a.to_str().unwrap()).len(), 2);
        // --file 기본 출력(객체 1개)
        let o = dir.join("one.json");
        std::fs::write(&o, "{\"file\":\"a\",\"grade\":null}").unwrap();
        assert_eq!(load_propagate_input(o.to_str().unwrap()).len(), 1);
    }

    #[test]
    fn vector_from_json은_빈배열과_비배열을_없음으로_본다() {
        assert_eq!(vector_from_json(&json!([0.5, -0.25])), Some(vec![0.5f32, -0.25f32]));
        // 빈 배열은 '0차원 벡터'가 아니라 '벡터 없음'으로 다뤄야 전파에서 건너뛴다.
        assert_eq!(vector_from_json(&json!([])), None);
        assert_eq!(vector_from_json(&json!(null)), None);
        assert_eq!(vector_from_json(&json!("0.5")), None);
        // 숫자가 아닌 원소가 섞이면 통째로 버린다(반쪽 벡터로 비교하면 안 된다).
        assert_eq!(vector_from_json(&json!([0.1, "x"])), None);
    }

    #[test]
    fn candidates_from_json은_형식이_틀린_원소를_버린다() {
        let v = json!([
            {"dc_id":"DC_1","path":"A > B","path_ids":["DC_0","DC_1"],"confidence":0.7,"from":["rule"]},
            {"path":"dc_id 없음"},
            {"dc_id":"DC_2"}
        ]);
        let out = candidates_from_json(&v);
        assert_eq!(out.len(), 2);
        assert_eq!(out[0].dc_id, "DC_1");
        assert_eq!(out[0].path_ids, vec!["DC_0", "DC_1"]);
        assert_eq!(out[0].from, vec!["rule"]);
        // 빠진 항목은 기본값으로 채운다 — 손으로 고친 결과 파일이 와도 죽지 않아야 한다.
        assert_eq!(out[1].dc_id, "DC_2");
        assert_eq!(out[1].confidence, 0.0);
        assert!(out[1].from.is_empty());
    }

    #[test]
    fn out_확장자로_형식을_추측한다() {
        // "--out result.jsonl 인데 안에는 JSON 배열"이라는 함정을 없애려는 것이다.
        assert_eq!(guess_format(&Some("result.jsonl".into())), Some("jsonl".into()));
        assert_eq!(guess_format(&Some("result.ndjson".into())), Some("jsonl".into()));
        assert_eq!(guess_format(&Some("result.json".into())), Some("json".into()));
        // 대소문자 무시 — 윈도우에서 .JSONL 로 적는 경우가 있다.
        assert_eq!(guess_format(&Some("D:/out/RESULT.JSONL".into())), Some("jsonl".into()));
        // 모르는 확장자·확장자 없음·--out 없음 → 기본값을 그대로 쓰게 None.
        assert_eq!(guess_format(&Some("result.csv".into())), None);
        assert_eq!(guess_format(&Some("result".into())), None);
        assert_eq!(guess_format(&None), None);
        // 경로에 점이 있어도 파일명 확장자만 본다.
        assert_eq!(guess_format(&Some("D:/a.b.c/result.jsonl".into())), Some("jsonl".into()));
    }
}
