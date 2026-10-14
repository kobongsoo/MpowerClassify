# csoclassify-rs — CSOClassify 코어의 Rust 포팅

CSOClassify(Python)의 **추출 + 규칙 분류 + 임베딩 전파(Signal B)** 코어를 Rust로 옮긴 것.
목표는 **정적 단일 바이너리·이식성**(CentOS7/glibc 무관). 무거운 자산(ONNX 모델·onnxruntime·
pdfium)은 바이너리에 넣지 않고 **exe 옆 외부 파일로 런타임 로딩**(pdfium 방식)해 바이너리는 작게 유지.
기존 Python 소스는 건드리지 않고 이 폴더에만 포팅했다.

## 분류 모드 (원본과 동일)

| 모드 | 동작 | parity(실측) |
|---|---|---|
| **기본** | 규칙 분류 + 보류문서 자동 전파(seed 있으면 on) | **100%** (교육 34/34) |
| `--rule-only` | 규칙만(임베딩·전파 없음, 가장 빠름) | 99.5% (211중 210) |
| `--vector-only` | 규칙 없이 벡터-seed 비교만(--seeds 필수) | **100%** (AI 10/10) |

> 기본 모드는 **exe 옆 `class_seed.jsonl`(또는 `--seeds`)이 있으면 자동으로 전파를 켠다** — 원본과 동일.
> 임베딩은 e5-small-ko ONNX 를 onnxruntime 으로 돌리며, Python(pypdfium2 아님, onnxruntime)과
> **임베딩 벡터가 완전 일치**(코사인=1.000000, probe 3종 검증). 전파 알고리즘(near-dup 상속·k-최근접
> 유사도 가중 다수결)도 propagate.py 그대로.
> `--rule-only` 의 유일한 1건 불일치는 **ko-pii(PII) 문제가 아니라 html 추출 한계**(Marp 슬라이드의
> 누출 JS "mou"에 L2 키워드 'MOU' 대소문자무시 오매칭; Python 은 DOM 파서로 JS 제거).

## 배포 구성 (exe 옆 외부 자산 — pdfium 방식)

| 자산 | 크기 | 용도 |
|---|---|---|
| `csoclassify-rs.exe` | **5.3MB** | 바이너리(규칙·추출·전파 로직 + 임베딩 사전) |
| `onnxruntime.dll` | 16MB | 임베딩 추론 런타임(전파/--vector-only 시) |
| `models/e5-small-ko/` | 129MB | ONNX 모델 + tokenizer(전파/--vector-only 시) |
| `pdfium.dll` | 7MB | PDF 추출 시 |

> 모델·런타임 경로 탐색: `CSO_MODEL`/`ORT_DYLIB_PATH` 환경변수 → **exe 옆** `models/e5-small-ko/`·
> `onnxruntime.dll`. 없으면 전파를 조용히 생략(규칙 결과만). `--rule-only` 는 이들 없이도 동작.

## 무엇을 포팅했나

**규칙 엔진(전부 포팅)** — `cso_rule.yaml`을 그대로 읽어(데이터는 포팅 대상 아님):
- L1 PII + 결합식별성(combo), L2 키워드(제외어·bulk 상향), 민감정보군(Signal F),
  스탬프(머리/반복 게이트, Signal E), 경로(Signal C), 파일명(Signal D)
- 보수적 max + fail-safe 융합(`fuse`), 신뢰도표, 등급서열(C>S>O)
- 레코드/신호 JSON 이 Python 과 동일 스키마

**추출(PoC 지원)** — text · html(태그제거) · docx · xlsx · pptx · hwpx (zip+XML) · **pdf(pdfium-render)**
**압축 확장** — zip · tar(+gz/bz2/xz) · gz · bz2 · xz · 7z · rar (`src/archive.rs`)
- docx/pptx 는 **문단 인식**(런 이어붙임)으로 Python 과 동일 — 런 경계로 값이 갈라지는 오탐 방지.
- pptx 는 슬라이드 본문에 더해 **발표자 노트**(`ppt/notesSlides/`)도 읽음 — 노트에 단가·설계
  상세가 적히는 일이 잦아, 본문만 읽으면 그 문서의 등급이 내려앉는다.
- xlsx 는 공유문자열표 + **워크시트 인라인 문자열(inlineStr)** 까지 읽음(sharedStrings 없는 파일 대응).
  여기에 **셀 메모(주석)** (`xl/comments*.xml` · 스레드 댓글)도 본문 뒤에 붙임 — 신청서·양식류는
  '어떻게 적어라'가 전부 메모에만 있는 일이 흔하다.
- 구형 xls(BIFF8)는 문자열·숫자 셀에 더해 **수식 셀의 계산된 결과값**(`FORMULA` 레코드)도 읽음 —
  합계·환산처럼 사람이 표에서 읽는 값 상당수가 수식이고, 하이픈 없는 전화·계좌번호가
  수식 결과로 들어 있으면 검출기 눈에 안 보인다.
- `--text-only` 로 **분류 없이 본문만** 뽑을 수 있음(규칙셋·PII 검사 안 함 → 파서 성능 측정용).
- hwpx 는 **문단(`hp:p`) 단위**로 이어 붙임 — 태그(`hp:t`) 단위로 끊던 예전 판은 서식 경계에서
  낱말을 쪼갰다("가치를" → "가치"/"를"). 표는 문단이 중첩되므로(`hp:tbl`>`hp:tc`>`hp:subList`>`hp:p`)
  짝 맞추기 대신 한 번 훑으며 문단 경계에서 줄을 끊는다. ※ 이 부분은 파이썬 판과 본문이 다르다.
- PDF 는 pdfium 동적 라이브러리 필요(아래). pypdfium2 의 `pdfium.dll` 을 그대로 재사용 가능.

**PII 검출(ko-pii 20라벨 전면 포팅 — 체크섬·문맥앵커·행정구역 사전·겹침해소)**:

| 라벨 | 방식 |
|---|---|
| **RRN·FRN** | 형식+세기(성별자리)+실날짜+**미래일자 배제(GS1 880 바코드 차단)**+체크섬→확신도, RRN prefix패턴/법인번호 예외 |
| **CARD** | 구분자/무구분 + **BIN 첫자리 화이트리스트{2,3,4,5,6,9}** + 길이-브랜드 일관성 + Luhn |
| **BUSINESS_REG·CORP_REG** | 국세청/법인 가중합 체크섬 |
| **PASSPORT** | 대문자 prefix 화이트리스트(PP/PM/…/M/S/G…) + 8자리(all-zero 제외) |
| **VEHICLE** | 용도한글 화이트리스트(~50자) + 4자리(0000 제외) + **뒤 단위어(원·억·명…) 거부** |
| **PHONE** | 모바일/유선 구분(위험도차) | 
| EMAIL·URL | 패턴 |
| **IP** | 옥텟+예약대역+버전/섹션 문맥게이팅 |
| **ACCOUNT** | 은행명(~60)/‘계좌’ 앵커 + 10~16자리 |
| **NATIONALITY** | 국가사전(82)+조사제거+‘인’ 접미 |
| **ADDRESS** | 4브랜치(도로명·지번·대화체·단독행정구역) + **행정구역 사전**(광역17·기초206·법정동 1만개)로 (광역+기초) 조합검증·앵커 |
| **DRIVER_LICENSE** | 하이픈형/무하이픈(키워드앵커) + 지역코드11~28 |
| **MEDICAL_INSURANCE** | 11자리 + 건강/의료보험·보험증 키워드(25자) |
| **PRESCRIPTION_ID** | 발행번호(날짜검증)+영문접두ID+의료기관기호, 각 키워드앵커 |
| **EDI_DRUG** | 9/13자리 + 키워드앵커 + 국가코드880/881/888 |
| **COURT_CASE** | 연도+부호문자(대법원 예규 화이트리스트)+일련번호 |
| **PNU** | 19자리 + 시도코드 + 필지구분·본번 검증 |

**겹침 해소(ko-pii core.overlap)** — 전 라벨 span 을 위험도→확신도→길이→시작 우선으로 채택,
겹치면 드롭. (예: GS1 바코드가 RRN·EDI 둘 다 걸릴 때 원본과 동일하게 정리.)

## 빌드 / 사용

> **처음 빌드한다면 → [빌드 가이드(Rust)](../doc/csoclassify-빌드가이드-rust.html)** — Windows·Linux 전 과정을
> 단계별로(툴체인 설치·외장 자산 조달·배포본 조립·zip/tgz 패키징·트러블슈팅·복붙용 명령) 정리한 문서다.
> 원본 파이썬판은 [빌드 가이드(Python)](../doc/csoclassify-빌드가이드-python.html).

```bat
cargo build --release           REM → target\release\csoclassify-rs.exe (2.3MB)
csoclassify-rs --dir "D:\분류함" --cso-rules cso_rule.yaml --simple --nosummary --format jsonl
csoclassify-rs --file a.docx    --cso-rules cso_rule.yaml
```
옵션: `--file/--dir · --files-from · --filelist · --cso-rules · --format json|jsonl · --out · --simple · --hash · --with-pii · --rule-only · --vector-only · --with-vector · --embed-needed · --auto-propagate · --seeds · --summary · --nosummary · --failsafe · --no-doc-id · --report-missing-id`
- `--format json|jsonl` : **안 주면 `--out` 확장자를 따른다**(`.jsonl`/`.ndjson`→jsonl, `.json`→json,
  모르는 확장자·`--out` 없음→json). 추론되면 stderr 에 한 줄 알린다. `--format` 을 직접 주면 확장자와 달라도 그 값이 이긴다.
  파이썬 판 `resolve_format()` 과 같은 표를 쓴다.
  <br>※ 전에는 `--out result.jsonl` 로 저장해도 JSON 배열이 들어가 뷰어가 열지 못했다.
- `--with-pii` : [프라이버시 예외] 검출된 **원문 PII 값**을 `pii:[{label,value,start,end}]` 로 함께 저장(기본 off, 건수만).

**오류 로그** — 오류가 나면 실행 파일 옆 `csoclassify_err_YYYYMMDD.log` 에 남긴다(`src/errlog.rs`).
실행 명령줄·오류 메시지·종료코드가 들어가고, 패닉(예상 못 한 내부 오류)은 파일:줄 위치까지 남는다.
**오류가 없으면 파일도 안 생긴다** — 파일이 보인다는 것 자체가 신호가 되어야 하기 때문이다.
위치는 환경변수 `CSOCLASSIFY_ERRLOG=<파일경로>` 로 바꾼다(파이썬 판과 같은 이름·같은 규칙).
로그를 못 써도(권한 없음 등) 분류는 그대로 진행한다.

- `--rule-only` : 규칙만(임베딩·전파 없음). `--vector-only` 와 배타.
- `--vector-only` : 규칙 없이 벡터-seed 비교만(`--seeds` 또는 exe 옆 class_seed.jsonl 필수). `--rule-only` 와 배타.
- `--with-vector` : **모든 문서**를 임베딩해 결과 레코드에 `vector`(384차원) 필드로 포함(RAG 색인 등).
  파이썬 판의 같은 이름 인자와 **같은 벡터**를 낸다 — `d:\sample` 18건 실측 코사인 1.000000000
  (최대 절대오차 1.5e-08 = float32 반올림 수준). 모델 로드 실패·본문 없음이면 필드를 아예 넣지 않는다.
  <br>※ 2026-08-26 이전 판은 추출 원문을 그대로 임베딩해 파이썬과 코사인 0.94~0.98 로 어긋났다
  (파이썬은 `clean_text()` 로 정제한 텍스트를 넣는다). 지금은 `embed::clean_for_embed()` 로 같은 모양을 만든 뒤 넣는다.
- `--embed-needed` : **아직 못 정한 문서만** 임베딩한다(규칙으로 등급이 확정된 문서는 건너뛴다).
  그 문서들의 `vector`(384차원)도 결과 레코드에 함께 실린다 — 파이썬 판과 같은 뜻이다.
  <br>※ 2026-09-02 이전 판은 이 인자를 **받아만 두고 읽지 않았다**. seed 파일이 없으면 임베딩이
  통째로 생략돼, 부르는 쪽은 임베딩을 시켰다고 믿는데 아무 일도 일어나지 않았다.
- `--propagate <결과파일>` : 1차 결과(jsonl/json 배열/객체 1개 모두 가능)를 읽어 **전파만** 다시 도는 2차 패스.
  `--file/--dir` 대신 쓰며 **문서·모델·규칙셋이 전부 불필요**하다(저장된 신호를 되살려 재융합).
  파이썬 `--propagate` 와 인자·출력·통계 줄이 같다 — 같은 1차 파일로 실측 시 통계
  (`seeds=71 already_graded=10 embed_decided=4 still_unclassified=4`)와 18개 레코드의
  `grade`/`confidence`/`method`/`decided_by`/`labels`/`signals` 가 전부 일치.
  <br>※ 1차 파일 끝의 `{"summary":…}` 줄은 문서가 아니므로 버린다(파이썬은 레코드로 세어 `no_vector` 를 1 더 잡고 낡은 요약을 다시 쓴다).
  <br>※ 유사도가 동점인 이웃의 나열 순서는 두 판이 다를 수 있다(파이썬 `np.argsort` 가 불안정 정렬). 판정에는 영향 없음.
- `--auto-propagate` : 보류 문서 전파(seed 있으면 기본 on이라 명시 불필요).
- `--seeds <class_seed.jsonl>` : 전파 기준 seed(미지정 시 exe 옆 class_seed.jsonl).
- `--build-doc-rule` : 체계 JSON(`--export-input`)·유의어 사전·본보기·`doc_rule.local.yaml` 로 `--doc-rules` 파일을 통째로 새로 만든다.
  화면 [규칙 다시 만들기]가 부르는 명령이며, 같은 입력이면 Python 판과 결과 파일이 글자까지 같다(`src/docbuild.rs`).
  어휘 생성은 Python 판과 골든 테스트(`tests/docvocab_golden.json`)로 묶여 있다.
- `--doctype-vector-only` : 업무분류(doctype) 1차 규칙 스캔을 건너뛰고 기준 문서 비교로만 분류. 규칙 파일의 `embed`·`conflict`·`defaults` 는 그대로 쓴다. security 축은 영향 없음.
- `--check-rules` : 규칙셋의 등급 값만 검사하고 종료(문서를 읽지 않음). 정상 0, 검증 실패 4.

### 입력 목록과 문서 식별자(doc_id)

경로가 비슷한 두 옵션은 **하는 일이 다르다.** `--files-from` 은 "무엇을 처리할지"만 정하고,
`--filelist` 는 거기에 더해 "그 문서가 누구인지"(`sfile_id`)를 알려준다.

| 옵션 | 형식 | `sfile_id` | 역할 |
|---|---|---|---|
| `--files-from <목록>` | 텍스트, 한 줄에 경로 하나(`-` 면 stdin) | 없음 | 대상 지정만 |
| `--filelist <목록>` | jsonl 또는 csv (`path` + `sfile_id`) | 있음 | `doc_id` 주입 (+대상 지정) |

- `--filelist <목록>` : MpowerV11 이 뽑아 준 `{path, sfile_id}` 목록으로 결과 레코드의
  `doc_id` 를 채운다(설계 §7-5-2-1 · [src/filelist.rs](src/filelist.rs)).
  `--file/--dir` 과 **함께** 주면 대상은 그쪽이 정하고 목록은 'ID 사전' 역할만 하며,
  **단독**으로 주면 목록에 적힌 파일이 대상이 된다.
  ```jsonl
  {"path": "D:/collected/법무/용역계약서_한빛테크_2026.docx", "sfile_id": "SF20260724110743001"}
  {"path": "D:/collected/인사/임직원_명부_2026.xlsx",        "sfile_id": "SF20260724110812007"}
  ```
  csv 도 받는다 — 첫 줄이 헤더이고 `path`·`sfile_id` 열이 있으면 된다(열 순서 무관).
  `hash` 열은 선택 — 주면 실제 파일과 대조해 어긋난 건수만 센다(F6, 처리는 계속).
  상대경로는 **목록 파일이 있는 폴더** 기준으로 푼다.
  <br>매칭은 정규화 경로 키(`/` 통일 · `..`/`.` 해소 · 드라이브 문자만 대문자)로 하고,
  못 찾으면 대소문자를 접어 한 번 더 본다 — 그렇게 이어진 건은 `rematched_by:"case"` 로 남긴다.
  <br>목록이 깨져 있으면 **문서를 한 건도 읽기 전에 멈춘다** — 파일 없음 1010,
  내용 잘못됨(F1 전부 파싱 실패 · F3 한 경로에 두 `sfile_id`) 1011. 절반쯤 잘못된 ID 가
  붙은 결과가 나가는 것이 최악이기 때문이다. F2·F4·F7 은 경고만 내고 계속한다.
- `--no-doc-id` : 결과 레코드에 `doc_id`/`key` 를 넣지 않는다(보안등급만 볼 때).
  기본은 넣는다 — 계산 비용이 사실상 0 이고, 사람이 고친 등급을 결과와 잇는 데도 쓰인다.
- `--report-missing-id <경로>` : `sfile_id` 를 못 얻어 폴백으로 채운 문서 목록을 jsonl 로 쓴다.
  그 문서들은 **매핑 테이블 적재 대상이 아니다**(R14).

`doc_id` 는 ① 목록의 `sfile_id` → ② 파일 내용 SHA-256 앞 40자 → ③ 정규화 경로 SHA-256 앞 40자
순으로 정하고, 어느 것을 썼는지 `doc_id_source`(`sfile_id`|`content`|`path`)로 남긴다.
②·③ 은 MpowerV11 문서와 이어지지 않으므로 적재는 `doc_id_source == "sfile_id"` 인 행만 넣는다.
요약 끝줄에 출처별 건수가 뜬다: `[summary][문서 ID] sfile_id 121 · 폴백(content) 6 · 폴백(path) 1`.

값은 **파이썬 판 [`filelist.py`](../src/csoclassify/filelist.py) 와 반드시 같아야 한다** — 두 판이
다른 `doc_id` 를 내면 같은 문서가 매핑 테이블에 두 건으로 들어간다. `src/filelist.rs` 의 단위
테스트가 정규화·해시·F3 판정을 파이썬 판과 같은 값으로 묶어 둔다.

**규칙셋 검증(fail-closed)** — 로드 시점에 등급 값을 검사한다. 정의되지 않은 등급(`base_grade: c` 오타 등)이나
`bulk_grade` 가 `base_grade` 보다 낮은 역전이 있으면 문서를 한 건도 읽지 않고 위반을 전부 모아 보고하고
**종료코드 4** 로 끝난다. 보고문·종료코드는 Python 판과 동일하다(바이트 단위 일치 확인).
예전에는 이런 값을 조용히 무시해 그 규칙이 판정에서 빠졌고, 문서가 실제보다 낮은 등급을 받았다.
경로 규칙은 `acl_restricted: true` 면 `grade` 를 생략할 수 있고(스스로 등급을 내지 않고 무신호일 때만
fail-safe 최고등급), 둘 다 없으면 V12 위반이다. Python 판과 동작·메시지가 같다.

리눅스 바이너리 — **CentOS7(glibc 2.17)에서 gnu 타깃으로 빌드**한다. 실행파일에 `RPATH=$ORIGIN` 을 박아
`libonnxruntime.so` 가 요구하는 신형 `libstdc++.so.6` 를 exe 옆에서 찾게 하는 것이 핵심이다
(빠뜨리면 실행 시 `CXXABI_1.3.8 not found`).
```bash
export RUSTFLAGS='-C link-arg=-Wl,-rpath,$ORIGIN -C link-arg=-Wl,--disable-new-dtags'
cargo build --release        # → target/release/csoclassify-rs (5.4MB, glibc 2.17+ 어디서나 동작)
```
> musl 정적 빌드(`x86_64-unknown-linux-musl`)는 현 빌드서버에 `musl-gcc` 가 없어 `tokenizers` 의
> C 코드(oniguruma)를 컴파일할 수 없다. 또 `libonnxruntime.so`·`libpdfium.so` 가 외부 glibc 라이브러리라
> musl 로도 완전 정적이 되지 않는다. 실용적으로는 CentOS7 gnu 빌드가 호환 범위가 더 넓다.

## PDF 실행 요건 (pdfium)

PDF 추출은 pdfium 동적 라이브러리가 필요하다(pdfium-render). 다음 순서로 찾는다:
`CSO_PDFIUM` 환경변수 → **exe 옆 `pdfium.dll`**(리눅스 `libpdfium.so`) → 시스템 라이브러리.
**개발 빌드는 손댈 필요가 없다** — `build.rs` 가 빌드할 때마다 정본을 exe 옆
(`target/debug`·`target/release`)으로 자동 복사한다. `cargo clean` 으로 지워져도 다음
빌드에서 저절로 복구된다. 원본 후보는 `dist-onedir/<os>/` → (없으면) 파이썬 배포본
`../dist/csoclassify/_internal/pypdfium2_raw/` 순이며, 둘 다 없으면 빌드는 그대로 되고
`cargo:warning` 으로 "PDF 추출이 비활성됩니다" 만 알린다.

> **주의** — DLL 만 지우고 `cargo build` 를 하면 복구되지 않을 수 있다. cargo 가
> 빌드 스크립트를 다시 돌릴 이유(입력 변화)를 못 찾아 캐시된 결과를 쓰기 때문이다.
> `cargo clean` 이나 `touch build.rs` 로 다시 돌리면 된다.

배포본을 직접 꾸릴 때는 수동 복사(≈7MB):
```
copy dist-onedir\windows\pdfium.dll  target\release\
```
※ 이 pdfium.dll 은 pypdfium2 와 동일 엔진이라 Python(pypdfium2)과 추출 결과가 일치.

## 아직 안 된 것 (별도 과제 / 문서화된 갭)

- ~~구형 바이너리 포맷 추출: doc·xls·ppt(OLE)·hwp5 미지원~~ → **해소됨.** 자체 OLE 파서
  (`src/ole.rs`)와 포맷별 파서(`office_legacy.rs` doc·ppt / `xls.rs` / `hwp5.rs`)로 처리한다.
  실측(2026-09-02, `--with-pii` 로 라벨별 건수 대조):
  `.doc`·`.hwp` 는 Python 과 **완전 일치**, `.xls`(주소록 1,434건 검출)는 **PHONE 1건 차이**
  (646 vs 647) — 추출 단계의 미세한 차이로 보이며 원인 미규명.
- **이미지 추출 미지원 — 단, 이는 parity 갭이 아니다**: jpg·png 등은 `Fmt::Image` 로
  인식만 하고 본문을 못 뽑는다. **Python 판도 마찬가지다** — 사이냅 문서필터는 OCR 엔진이
  아니라 문서 텍스트 추출기라, 이미지를 넣으면 0바이트를 내고 실패한다
  (실측: `snf:rc=0; 출력파일 0바이트; stdout=[ERROR : 40101]`). OCR 은 별도 제품이며
  이 프로젝트에 붙어 있지 않다(`extract/detect.py` 주석 "OCR 별도").
  <br>두 판 모두 `grade=null` + `stage:extract` 오류로 **같게 처리**한다.
  ※ 단 이 파일들도 **경로·파일명 신호는 확장자 무관하게 동작**한다.
- **html 추출(정규식 기반)의 한계**: `<script>` 내부에 마크업 문자열이 박힌 Marp/JS-heavy
  export 는 정규식으로 완전 분리 불가 → 누출된 JS 조각이 L2 키워드에 오매칭될 수 있음
  (실측 유일 1건 diff). 완전 해소는 실제 DOM 파서 필요(단일 바이너리 목표와 상충 → 보류).
- **ko-pii 미포팅 라벨(0 반환)**: NAME/PERSON·BIRTH·HEALTH 등 — CSOClassify 의 cso_rule.yaml
  이 등급판정에 쓰지 않는 라벨이라 포팅 대상 아님(현 20라벨로 등급 parity 충족).
- **PERSON 사전(surnames/hanja/romanization) 미포팅**: 위와 같은 이유로 스코프 밖.
- ~~문자 정규화(NFKC·대시류·안 보이는 문자) 미포팅~~ → **2026-09-02 해소.** `src/pii_fold.rs`
  (자동 생성, 항목 5,595개 + 표시범위 195)로 전면 포팅했다. 회피 시도(전각·호모글리프
  `95lOO6`·결합표시 `951́006`·소프트하이픈·아랍숫자·수학볼드)를 ko-pii 와 같이 검출한다.
  <br>※ 다만 **NFKC 를 표로 근사**한 것이라, 두 글자 이상이 합쳐지는 정규화(`e`+´→`é`)는
  하지 않는다. PII 는 숫자·ASCII 패턴이라 검출에 영향이 없다고 보고 넘긴 부분이다.
  <br>※ 비용: 1MB 문서에서 +4.7%, 실무 크기 문서에서는 차이 없음(오차 범위), exe +70KB.
- **파일을 하나씩 따로 실행하면 느리다 — `--daemon`(상주 서버)이나 `--files-from` 으로 푼다**:
  이 판은 기본으로 실행할 때마다 ONNX 모델을 새로 로드하고 프로세스가 끝나면 버린다. Python 판은
  **기본 on 인 상주 데몬**이 모델을 메모리에 물고 있어 2회차부터 모델 로딩이 0 이다.
  <br>→ **[2026-10-12] 임베딩 상주 서버 추가** — 호출 구조를 못 바꾸는 연동(엠파워 v11: 파일 1건 = 실행 1회)을
  위해 이 판에도 상주 서버를 넣었다(설계: [plan/Rust-임베딩모델-상주서비스-설계.html](../plan/Rust-임베딩모델-상주서비스-설계.html)).
  - `MpowerClassify-rs --serve [--model <이름>] [--idle-timeout <초>]` — 모델을 물고 있는 서버를 띄운다(0=무한, 기본).
  - 분류 호출에 `--daemon` — 임베딩을 서버에 맡긴다. 서버가 없으면 자동으로 띄우고, 못 쓰면 모델을 직접 읽는다
    (결과는 같다). **기본은 꺼짐**이라 `--daemon` 을 안 주면 예전과 똑같이 돈다(파이썬 판은 기본이 켜짐).
  - `--status` / `--stop` — 상태 / 정지. **`--serve`·`--status`·`--stop` 의 출력은 언제나 JSON 한 줄**이다:
    `{"error":{"code":0,"kind":"success","message":"…"},"daemon":{"running":true,"pid":…,"port":…,"ready":true,…}}`.
    `code` 0 = 정상(서버가 없다는 답도 정상 — `daemon.running` 으로 구분), 실패는 `code`≠0(예: 3002).
    파이썬 판이 내는 문구는 `message` 에 그대로 들어 있다(파이썬 판은 문장 한 줄만 낸다).
  - 서버는 **임베딩만** 한다. 추출·규칙·전파는 호출한 프로세스가 그대로 하므로 결과가 갈리지 않는다
    (`tests/cli_daemon.rs` 가 레코드와 벡터의 비트 단위 일치를 확인한다).
  - 접속 기록은 `%LOCALAPPDATA%\MpowerClassify\daemon-rs-<모델>.json`(파이썬 데몬과 파일을 나눠 서로 간섭하지 않는다).
    서버를 띄운 계정과 부르는 계정이 다르면 양쪽에 `CSOCLASSIFY_STATE_DIR` 로 같은 폴더를 준다.
  - 실측(txt 20건을 `--file` 로 하나씩, `--with-vector`, release, 동일 PC): 직접 로드 25.7s(**1.29s/건**) →
    `--daemon` 3.8s(**0.19s/건**). 서버 메모리 약 480MB(e5-small-ko). ※ 1회 측정, txt 만 — 아래 200건 표와
    같은 조건으로는 아직 재지 않았다.

  → **`--files-from <목록>`(2026-09-02 추가, 양쪽 판 모두)으로 경로 목록을 한 번에 넘기면**
  모델 로드가 1회로 끝나 이 벌점이 사실상 사라진다. 여러 폴더에 흩어진 파일도 묶을 수 있어
  `--dir`(한 폴더만) 로는 못 하던 배치가 된다.

  실측 — `D:\분류함` 에서 **양쪽이 모두 추출 지원하는 포맷 200건**(pdf 69·txt 64·pptx 28·
  docx 18·md 11·xlsx 6·hwpx 3·html 1), `--with-vector`, 동일 PC:

  | 호출 방식 | Rust | Python(데몬 on) | Python(`--no-daemon`) |
  |---|---|---|---|
  | `--files-from` 1회 (200건 한 프로세스) | **44.0s** | 84.0s | 62.8s |
  | `--file` 200회 (파일마다 프로세스) | 320.6s | **265.5s** | 471.6s |
  | 건당 고정비(개별 호출) | 1.60s | **1.33s** | 2.36s |

  읽는 법:
  1. **`--files-from` 의 효과가 가장 크다** — Rust 320.6s → 44.0s(**7.3배**),
     Python(데몬off) 471.6s → 62.8s(**7.5배**). 호출 구조를 바꾸는 것이 엔진을 바꾸는 것보다
     훨씬 크게 먹힌다.
  2. **배치에서는 Rust 가 빠르다**(44.0s vs 62.8~84.0s). 모델 로드 고정비가 200건에
     희석되면서 추출·규칙 처리 속도가 그대로 드러난다.
  3. **개별 호출에서는 Python(데몬 on)이 빠르다**(265.5s vs 320.6s) — 데몬이 모델 로드를
     0 으로 만들기 때문. 이것이 이 항목이 '갭'인 이유다.
  4. **`--files-from` 을 쓰면 데몬이 할 일이 없어진다** — 한 프로세스면 모델 로드가 어차피
     1회라, Python 도 데몬을 끈 쪽(62.8s)이 켠 쪽(84.0s)보다 느리지 않았다.
  ※ 프로세스 기동 자체는 Rust 가 더 빠르다(데몬 끈 Python 2.36s/건 > Rust 1.60s/건). 즉 이
  격차는 코드가 느려서가 아니라 **웜 모델 재사용의 유무**에서 온다.
  ※ `--rule-only` 는 모델을 아예 읽지 않으므로 이 문제가 없다 — 규칙만 필요하면 개별 호출도 빠르다.
  <br>※ **[2026-09-02] 지연 로딩** — 1차 규칙 스캔을 마친 뒤 "임베딩할 문서가 하나라도 있는지"를 먼저 보고,
  없으면 모델을 아예 올리지 않는다(`[csoclassify-rs] 임베딩 불필요(전 문서 규칙 확정) → 모델 로드 생략`).
  위 표는 **`--with-vector`(전량 임베딩)로 잰 것이라 이 효과가 안 나타난다** — `--with-vector` 는
  확정 문서까지 전부 임베딩하므로 게이트가 걸리지 않는다. 평소 호출(`--embed-needed`)에서 규칙으로
  확정된 문서 1건을 `--file` 로 부르면 **1,133ms → 18ms**(5회 중앙값, txt). 규칙 적중률이 높은
  코퍼스에서는 개별 호출 열세(320.6s vs Python 265.5s)가 뒤집힌다.
  <br>※ **[2026-09-02] `--embed-needed` 가 실제로 동작한다** — 예전에는 인자를 받아만 두고 읽지 않아,
  seed 파일이 없으면 임베딩이 통째로 생략됐다(파이썬 판은 언제나 동작). 이제 파이썬과 같은 우선순위이고,
  이 인자를 주면 **못 정한 문서의 `vector` 도 결과 레코드에 실린다**. 인자 없는 기본 실행의 출력은 예전 그대로다.
  ※ 아래 표는 상주 서버가 생기기 전(2026-09)에 잰 것이다 — Rust 열의 개별 호출 값은 `--daemon` 없이 부른 경우다.
  ※ 측정 주의: 배치 3회 반복의 산포가 컸다(Rust 32.6~57.0s, Python 데몬 66.6~143.0s — 디스크
  캐시 워밍 영향). 200회 개별 호출은 1회만 쟀다. **배수(7배)와 대소 관계는 견고하지만 초 단위
  절대값은 ±30% 로 보라.**
- ~~**압축 확장**: 후속(현재 압축파일은 추출 스킵).~~ → **2026-09-04 해소.**
  `src/archive.rs` 로 포팅했다. 압축 1건을 내부 문서 N건으로 펼쳐 **파일별로 등급**을
  내고, 압축 자체에는 내부 최고 위험을 매긴 집계 레코드(`archive: true`)를 덧붙인다.
  지원: **zip · tar · tar.gz · tar.bz2 · tar.xz · gz · bz2 · xz · 7z**
  (새 의존은 전부 순수 Rust — `tar`·`bzip2-rs`·`lzma-rs`·`sevenz-rust2`. C 라이브러리를
  안 끌어와 정적 단일 바이너리 목표를 지킨다).
  <br>**rar 은 2026-09-07 지원 시작** — `rars`(MIT/Apache-2.0, 순수 Rust). 다른 길인
  `unrar` 크레이트는 **UnRAR C 소스를 빌드에 포함**해 C 컴파일이 생기고 라이선스
  조건을 따로 확인해야 해서 피했다.
  <br>넣기 전에 참조 구현(번들 `unrar.exe`)과 픽스처 177개를 바이트 단위로 대조했다 —
  단일 아카이브 131건 기준 **판정 일치 95.4%**, **참조가 푸는데 rars 가 못 푼 경우 0건**,
  손상 입력 117건에서 **패닉·멈춤 0건**. 실제 분류 대조에서는 공통 554건의 **등급이
  100% 일치**했다.
  <br>남은 한계 둘은 **표식으로 드러낸다** — 분할 볼륨(`kind=split_volume`)과
  하드링크·심볼릭링크(`stats.partial` → "압축 일부 누락"). 실측 차이 7건이 정확히
  이 둘이었다(분할 2 · 하드링크 3 · 심볼릭 2).
  <br>**7z 는 2026-09-07 지원 시작** — `sevenz-rust2`(Apache-2.0, 순수 Rust).
  파이썬 판(py7zr)과 실측 대조에서 레코드 9건(내부 8 + 집계 1)이 **등급까지 전부 일치**했고,
  해제 상한(G4)도 7z 에 똑같이 걸린다.
  <br>다만 **조용히 넘기지 않는다** — 결과 레코드에 `archive_unsupported: true` 와
  사유를 싣고 요약에 `압축미지원=N` 으로 띄운다.
  <br>해제 상한(G4 `--max-archive-mb` · `--max-archive-members`)도 함께 동작하며,
  상한에 걸려 못 펼친 압축은 `archive_unexpanded: true` 로 표시된다.
  <br>실측 대조(zip 12개 내부 문서): 파이썬 판과 **레코드 13건(내부 12 + 집계 1)
  전부 일치**(등급·contains 분포·요약줄).
  <br>※ 남은 차이: 상한에 걸려 **안 펼친** 압축을 파이썬은 사이냅으로 '합본 1건'
  텍스트를 읽어 등급을 내지만(실측 90만 자), 이 판은 사이냅이 없어 추출실패로 둔다.
  ※ **임베딩 전파(Signal B)·--vector-only 는 포팅 완료**(외부 ONNX 로딩).

## parity 검증 방법

```
Rust:   csoclassify-rs --dir <t> --cso-rules <r> --simple --nosummary --format jsonl
Python: csoclassify.exe --dir <t> --hybridparse --rule-only --simple --nosummary --format jsonl
```
두 결과를 파일명 기준으로 등급 비교. 다만 **등급 비교만으로는 PII 검증이 안 된다** — 등급은
라벨별 건수를 임계값으로 뭉갠 결과라, 라벨 하나를 통째로 놓쳐도 등급이 같으면 통과한다.
그래서 PII 는 아래 골든 픽스처로 따로 검증한다.

### PII 골든 픽스처 (라벨별 건수·검출값 대조)

`pii.rs` 는 ko-pii 를 **쓰는** 게 아니라 **옮겨 적은** 것이라, ko-pii 를 올리면 소리 없이
어긋난다. 그래서 진짜 ko-pii 가 낸 답을 `tests/pii_golden.json` 에 굳혀 두고 대조한다.

- `tests/pii_corpus.yaml` — **검사 문장 목록(사람이 고치는 파일)**. 사례를 넣고 빼는 곳.
- `tests/pii_golden.json` — 그 문장을 ko-pii 에 넣어 나온 **정답(자동 생성, 손대지 말 것)**.
- `src/pii_fold.rs` — 문자 정규화표(**자동 생성**). `scripts/gen_pii_fold.py` 가 ko-pii 에
  글자를 하나씩 넣어 '실제로 무엇이 되는지' 물어 뽑는다. ko-pii 표(`_CHAR_FOLD`)를 베끼지
  않는 이유는 그 표가 NFKC 뒤에 적용돼 실제 효과가 표와 다른 항목이 있기 때문이다.
  ko-pii 를 올리면 `python scripts/gen_pii_fold.py` 로 다시 뽑는다(`--check` 로 확인).

```
python scripts/gen_pii_golden.py            # 정답표 생성/갱신 (ko-pii 필요)
python scripts/gen_pii_golden.py --check    # 정답표가 최신인지 확인만 (다르면 exit 1)
cargo test pii                              # 정답표와 대조 (ko-pii·exe·문서추출 불필요)
```

- 정답표 기준은 `detect_all(text, include=<20라벨>)` — 운영 코드(`rules.py`)의 부분검출·
  청킹 최적화가 아니라 **원본 엔진**이 정답이다.
- 테스트 3종: 라벨별 **건수** 일치 / 검출 **값** 일치(span 경계) / 정답표가 **20라벨을 모두 덮는지**
  (마지막 것이 없으면 "0건이라 검증된 줄 알았는데 안 한" 라벨이 생긴다).
- 값 비교는 `values_comparable=false` 인 사례(전각·제로폭으로 정규화가 글자를 바꾼 문서)만
  건너뛴다 — ko-pii 는 값을 원본 좌표로 되돌려 주고 Rust `pii_records` 는 정규화 좌표를 주므로
  다른 게 정상이다.
- 실문서를 코퍼스에 넣으려면 `--corpus-dir <폴더>` (추출기 차이가 끼면 안 되므로 `.txt/.md` 만).
- **ko-pii 를 올릴 때**: 정답표를 다시 뽑고 `cargo test` 를 돌린다. 거기서 깨지는 차이가 곧
  포팅이 따라가야 할 변경분 목록이다.
