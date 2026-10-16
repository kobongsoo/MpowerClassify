# MpowerClassify

한국어(다국어) **수집 문서를 C/S/O(기밀·민감·공개)로 자동 분류**하는 로컬 CLI 도구입니다.
사이냅 문서필터(`snf_exe`)로 텍스트를 뽑고(옵션 `--hybridparse` 로 내용 감지 후 포맷별 오픈소스 파서 사용 — 사이냅 불필요),
**여러 판정 신호**(내용·민감정보·보안분류 스탬프·경로·파일명)를
보수적으로 융합해 등급을 매깁니다. 임베딩 벡터는 보류 문서 구제·seed 비교(전파)에 씁니다.
분류가 **기본 동작**이며, 벡터만 필요하면 `--embed` 를 줍니다. 서버·인터넷 없이 로컬에서 동작하고
PyInstaller `onedir` 로 패키징합니다.

**문서 저장소의 암호화 문서**도 분류합니다. 분류 직전에 한 건씩 풀고, 분류가 끝나면 풀린 사본을 바로 지웁니다
([아래 장](#암호화-문서-분류)).
같은 기능을 **Python 판**(`src/csoclassify`)과 **Rust 판**(`Rust/`) 두 가지로 제공하며, 두 판은 같은 인자·같은 결과 모양·같은 오류 코드를 씁니다.

> 실행: [실행 가이드](doc/MpowerClassify-실행가이드.html) · [실행 가이드(리눅스)](doc/MpowerClassify-실행가이드-linux.html) · [규칙셋 필드 레퍼런스](doc/CSO_Rule.html) · [업무분류 규칙](doc/DOC_Rule.html)
> 설계: [설계서](plan/설계서.md) · [하이브리드 추출](plan/CSO_HybridParse.html) · [복호화 연동](plan/엠파워v11-암호화문서-복호화연동-설계-20261012.html) · [CLI 오류 출력](plan/CLI-오류출력-설계.html) · [Rust 임베딩 상주 서버](plan/Rust-임베딩모델-상주서비스-설계.html)
> 빌드: [빌드 가이드(Python)](doc/MpowerClassify-빌드가이드-python.html) · [빌드 가이드(Rust 포팅판)](doc/MpowerClassify-빌드가이드-rust.html) · 리눅스 빌드 스크립트 `scripts/build/linux-build.sh`

## 빠른 사용

```bash
# C/S/O 자동 분류 (기본 동작 — 모드 플래그 불필요). 출력 기본값 = JSON
csoclassify.exe --file "D:\docs\보고서.hwp"

# 폴더 전체 분류 → 결과 파일 (가장 많이 쓰는 형태)
csoclassify.exe --dir "D:\collected" -r --embed-needed --format jsonl --out result.jsonl

# 임베딩 벡터만 뽑기(분류 없이)
csoclassify.exe --embed --file "D:\docs\보고서.hwp"

# 암호화 문서 한 건을 풀어서 분류 (리눅스 서버)
./csoclassify --file /data/store/0001234 --decrypt --mrk "$MRK" --enc-mode "$ENC_MODE" --zip-mode "$ZIP_MODE" --name "보고서.hwp" --size 52311

# 암호화 문서 목록 파일로 여러 건 — 줄마다 열쇠 칸이 있으면 그 줄만 푼다
./csoclassify --filelist list.jsonl --format jsonl --out result.jsonl
```

`--file` 단건은 JSON 객체 하나, `--dir` 배치는 레코드들의 **JSON 배열**을 출력합니다.
줄단위 스트리밍/후처리는 `--format jsonl`(문서당 1줄).

## 암호화 문서 분류

문서 저장소의 문서는 암호화된 채 저장되어 있어 그대로 읽으면 분류할 수 없습니다.
그래서 분류 직전에 저장소의 복호화 도구(`removeenc.sh`)로 **한 건씩** 풀고, 분류가 끝나면 풀린 사본을 **바로 지웁니다**.

| 방식 | 주는 법 |
|---|---|
| 여러 건 | `--filelist` 의 각 줄에 `mrk` · `enc_mode` · `zip_mode`(+ `name` · `size`)를 적으면 **그 줄만** 풉니다. `enc_mode`·`zip_mode` 가 둘 다 0 인 줄은 도구를 부르지 않고 그대로 읽습니다 |
| 한 건 | `--file` 에 `--decrypt --mrk --enc-mode --zip-mode [--name] [--size]`. `--size`(원래 크기, 바이트)를 주면 풀린 결과의 크기와 견주고, 안 주면 크기 검사를 건너뜁니다 |
| 풀지 않기 | `--no-decrypt` — 목록에 열쇠가 있어도 그대로 읽습니다 |

목록 한 줄(jsonl) 예:

```json
{"path": "/data/store/0001234", "sfile_id": "F0001234", "mrk": "…", "enc_mode": "…", "zip_mode": "…", "name": "보고서.hwp", "size": 52311}
```

- 저장 이름에는 확장자도 뜻도 없으므로 **`name`(원래 파일 이름)을 주기를 권합니다** — 확장자 판별과 파일명 신호에 씁니다.
- 열쇠가 틀려도 도구는 성공(0)을 돌려줍니다. 그래서 풀린 파일의 **크기·시그니처를 따로 확인**하고, 이상하면 분류하지 않고
  `grade: null` + `error.kind = "decrypt_failed"`(이유는 `detail`)로 냅니다.
- 풀기 전에 **풀린 뒤 실제로 적용될 크기 상한**을 미리 보고, 넘으면 풀지 않고 `error.kind = "size_limit"` 로 냅니다.
  상한은 원래 이름의 확장자로 고릅니다 — 압축(zip·7z 등)은 `--max-archive-mb`(500MB), 텍스트(txt·csv 등)는 `--max-file-mb-text`,
  그 밖은 `--max-file-mb`(100MB). 크기는 `size`(원래 크기)가 있으면 그것으로 봅니다. `--no-size-limit` 이면 이 검사도 꺼집니다.
- 결과 레코드에는 `name`(원래 이름)과 `meta.decrypted` 가 실립니다.
- **열쇠(MRK)와 임시 경로는 로그·결과에 남기지 않습니다.** `v2:` 로 시작하는 감싼 MRK 는 받지 않습니다(평문만).
- 시간 제한(`--decrypt-timeout`, 기본 60초)을 넘기면 하위 프로세스까지 모두 끝냅니다.

| 옵션 | 설명 | 기본 |
|---|---|---|
| `--decrypt-cmd` | 복호화 도구 경로 | 저장소의 `removeenc.sh` 기본 설치 경로(`--help` 참고) |
| `--decrypt-tmp` | 풀린 파일을 잠깐 둘 곳. `/dev/shm` 을 주면 디스크에 닿지 않습니다 | 시스템 임시 폴더 |
| `--decrypt-timeout` | 한 건의 시간 제한(초) | 60 |

> 자세히: [복호화 연동 설계](plan/엠파워v11-암호화문서-복호화연동-설계-20261012.html) · [암호화 문서 생성 및 테스트](doc/엠파워v11연계-암호화%20문서%20생성%20및%20테스트.html)

## 판정 신호 (보수적 max 융합)

문서 하나에 여러 신호가 붙고, 최종 등급은 **가장 민감한 등급**으로 융합됩니다(각 신호는 등급을
**올릴 수만** 있고 함부로 내리지 않음). 어느 신호로도 못 정하면 **보류(none)** → 검토 대상.

| 신호 | 무엇을 보나 | 비고 |
|---|---|---|
| `rule` | 본문의 **PII 유형**(ko-pii 20종) + **기밀사전 키워드** + **결합식별성(combo)** | 내용 기반 |
| `sensitive` | **법상 민감정보**(건강·유전·성생활·사상/정치·노조·범죄경력·인종) 키워드 → C | 일반 키워드와 분리된 독립 신호 |
| `stamp` | 문서에 찍힌 **보안분류 표식**("대외비/기밀"→C, "내부용"→S) | 머리·반복일 때만 인정(오탐↓) |
| `path` | 파일이 놓인 **폴더/서버**(예: `/인사/`, `//hr-server/`) | 텍스트 안 엶 |
| `name` | **파일명** 속 키워드 | 참고용 |
| `embed` | 보류 문서를 seed와 **임베딩 유사도**로 구제 | `--propagate`(2차 패스) |

- **PII 20종(ko-pii)**: RRN·FRN·CARD·ACCOUNT·PASSPORT·DRIVER_LICENSE·ADDRESS·MEDICAL_INSURANCE·
  PRESCRIPTION_ID·EDI_DRUG·VEHICLE·COURT_CASE·PNU·NATIONALITY(민감/기밀) · PHONE·EMAIL·BUSINESS_REG·
  CORP_REG·URL·IP(공개, 대량이면 상향). 검출은 ko-pii(패턴·체크섬·문맥·우회방어 내장)가 담당.
- **결합식별성(combo)**: 개별론 낮아도 여러 PII가 한 문서에 모이면(결합용이성) 상향 — 예:
  `주민번호+계좌 → C`, `전화·이메일·주소 2종+ → S`. `rule` 신호의 `layer:"COMBO"` 히트로 표시.
- 정책(유형·키워드·조합·경로·등급)은 모두 `cso_rule.yaml` 또는 UI 규칙설정에서 조정 —
  필드 의미는 [CSO_Rule.html](doc/CSO_Rule.html) 참조.

> **프라이버시 불변식**: 매칭된 **원문 PII 값은 저장하지 않습니다**(유형 id·건수만).
> `--with-pii` 를 명시할 때만 검출 원문값을 **`--out` 파일에** 싣고(로그엔 `[pii N detected]` 만), 취급에 주의.

## 분류 방식 선택 (배타 옵션)

| 옵션 | 동작 | 임베딩 | seed |
|---|---|---|---|
| (기본) | 규칙으로 등급 → **미분류(보류)면 seed와 문맥 비교해 자동 전파** | 보류·seed 후보만 | exe 옆 `class_seed.jsonl`(있으면) |
| `--rule-only` | 규칙(내용·민감정보·스탬프·경로·파일명)**만**으로 분류 (전파 차단) | 안 함(가장 빠름) | 불필요 |
| `--vector-only` | 규칙 검사 **없이** 문서 벡터를 seed와 비교(전파)해서만 분류 | 전량 임베딩 | **필수** `--seeds` |

> **기본 자동 전파**: `--file`/`--dir` 만 줘도 **exe 옆에 `class_seed.jsonl` 이 있으면** 규칙 미분류 문서를 그 seed와 임베딩 비교해 자동 분류합니다(확정 문서는 건너뜀). seed 파일이 없으면 규칙만으로 끝냅니다. 전파를 원치 않으면 `--rule-only`.

## 주요 옵션 (전체는 `--help`)

| 옵션 | 설명 | 기본 |
|---|---|---|
| `--file` / `--dir` | 단일 파일 / 폴더 배치 (구 `-file`/`-dir` 호환) | — |
| `-r`, `--recursive` | **무시됨** — `--dir` 은 항상 하위 폴더까지 재귀(하위호환용) | 항상 재귀 |
| `--files-from` | 처리할 파일 경로를 **한 줄에 하나씩** 적은 목록 파일(`-` 이면 표준입력). 여러 폴더에 흩어진 파일을 **한 프로세스**로 처리한다 — `--file`/`--dir` 과 배타 | — |
| `--glob` | 배치 필터(예: `"*.hwp,*.pdf"`) | `*` |
| (모드 없음) | **C/S/O 분류**(기본). 예전 `--classify` 는 생략 가능 | 분류 |
| `--rule-only` / `--vector-only` | 규칙만 / 벡터-seed 비교만 (배타) | — |
| `--embed` | 분류 대신 **임베딩 벡터만** 출력 | — |
| `--text-only` | 벡터 없이 **추출 텍스트만** | — |
| `--hybridparse` | **내용 감지 → 포맷별 파서**(사이냅 없이): pdf→pypdfium2, hwp→HWP5, hwpx→zip/OWPML, doc/ppt→자체 olefile 파서, xls→xlrd, docx/xlsx/pptx→zip/OOXML 직접파싱(stdlib), html→태그제거(stdlib), text→직접읽기. 실패 시 snf 폴백(snf 없으면 미분류). 전부 순수py/소형 wheel로 **Win·Linux(CentOS7) 동일 번들**. snf 대비 동급~수십배 빠름 | off(전부 snf) |
| `--filelist` | 문서 저장소가 뽑아 준 `{path, sfile_id}` 목록(jsonl·csv). 결과 레코드의 `doc_id` 를 채우고, 복호화 열쇠 칸이 있으면 그 줄을 풀어서 분류. `--file`/`--dir` 이 있으면 'ID 사전' 역할만 함 | — |
| `--decrypt` / `--no-decrypt` | 암호화 문서 풀기 / 풀지 않기 ([위 장](#암호화-문서-분류)) | 목록에 열쇠가 있을 때만 풂 |
| `--cso-rules` | 보안등급 규칙셋 경로 | exe 옆 `cso_rule.yaml` |
| `--failsafe [등급]` | 어느 신호로도 못 정한 문서의 기본등급(값 생략=S) | 미부여(보류) |
| `--embed-needed` | **보류·seed 후보**에만 임베딩(확정문서 생략 → 빠름) | — |
| `--with-vector` | **모든 문서**에 임베딩 | — |
| `--auto-propagate` | 분류 직후 보류 문서를 seed로 **전파까지** | — |
| `--propagate <jsonl>` | 1차 레코드에 임베딩 전파 적용(모델 불필요) | — |
| `--seeds <경로>` | 전파 비교 기준 seed(`class_seed.jsonl`) | 내부 seed |
| `--doctype-vector-only` | **업무분류**를 규칙 없이 기준 문서 비교로만 분류(보안등급 축은 그대로) | 규칙 + 전파 |
| `--build-doc-rule` | 체계 JSON(`--export-input`)·유의어 사전·본보기·`doc_rule.local.yaml`(회사 조정)로 `doc_rule.yaml` 을 **통째로 새로 만든다**(분류체계 내장, 두 판 글자까지 같음). 자세히는 `doc/DOC_Rule.html` | — |
| `--with-text` | 결과에 추출 텍스트(`text`) 포함 | off(프라이버시) |
| `--with-pii` | **[주의]** 검출 원문 PII 값을 `pii` 필드로 저장 | off |
| `--hash` | 각 문서의 **해시(SHA-256)** 를 `hash` 필드로 포함 | off |
| `--simple` | 파일별 결과를 **문서명·등급·해시(+업무분류 `doctype`)** 로 줄여서 출력(JSON). `doctype` 은 업무분류 축이 돌았을 때만 붙는 dc_id 배열 | off |
| `--summary` | 파일별 레코드 없이 **맨 끝 요약(summary)만** 출력(JSON) | off |
| `--nosummary` | 맨 끝 **요약(summary)을 출력에서 제거**(파일별 레코드만) · `--summary`와 배타 | off |
| `--format` | `text` / `json` / `jsonl` | **`json`** |
| `--out` | 결과 저장 파일 | stdout |
| `--model` | 임베딩 모델: `e5-small-ko` · `e5-small` · `ko-sroberta` (두 판 같음) | `e5-small-ko` |
| `--max-tokens` / `--overlap` | 청크 크기 / 겹침 | 512 / 32 |
| `--progress` | 진행상황(stderr) 표시. 2차 패스(전파)도 알림 | off |
| `--daemon` / `--no-daemon` | 상주 데몬 모드 | Python **on** · Rust off |
| `--serve` / `--status` / `--stop` | 데몬 기동 / 상태 / 정지 | — |

> **압축파일 자동 확장** — 입력이 압축파일이면 내부 파일을 임시폴더에 풀어 **파일 하나하나를 개별 분류**한다(중첩 압축도 재귀). 지원: **zip · tar · tar.gz · tar.bz2 · tar.xz · gz · bz2 · xz**(stdlib, 무의존) · **7z**(py7zr) · **rar**(번들 unrar). 결과 JSON 은 파일별 레코드의 배열이고 각 레코드 `file` 은 `"<압축경로>/<내부경로>"` 로 표기돼 **어느 내부 파일이 C/S/O 인지** 알 수 있다. 처리 후 임시폴더는 자동 삭제(민감정보 잔존 방지). ※ docx/xlsx/pptx/hwpx 는 내부가 zip 이라도 '문서 1개'로 취급해 펼치지 않는다(내용 감지로 구분).
>
> **압축파일 '자체'의 등급(집계)** — 압축은 그 안 파일 중 **가장 높은 위험등급(C>S>O)** 을 자기 등급으로 받는다. 결과에 `"archive": true, "grade": "<최고위험>", "contains": {C,S,O,미분류,total}` 형태의 요약 레코드가 파일별 레코드 뒤에 추가돼, **압축만 봐도 위험도**를 안다. (내부 파일 등급 집계와 별개 레코드라 총계에는 중복 계산되지 않음.)

## 오류 출력

실패는 결과 JSON 의 `error` 칸(`{"code", "kind", "message"}`)과 종료코드로 알립니다. 두 판은 같은 상황에 같은 코드를 냅니다.

| 코드 대역 | 뜻 | 예 |
|---|---|---|
| 1000 | 인자 · 입력 | `1010 filelist_missing` · `1014 decrypt_args_invalid` · `1015 decrypt_cmd_missing` |
| 2000 | 정책 파일(규칙셋 · 분류체계) | `2001 rules_missing` · `2002 rules_invalid` |
| 3000 | 처리(추출 · 임베딩 · 복호화) | `3001 extract_failed` · `3005 decrypt_failed`(그 문서만) · `3006 decrypt_unavailable`(즉시 멈춤) |
| 4000 | 출력 | `4001 output_write_failed` |
| 9000 | 그 밖 | `9001 internal_error` |

> 전체 목록은 `src/csoclassify/errcodes.py`(Rust: `Rust/src/errcodes.rs`), 설계는 [CLI 오류 출력](plan/CLI-오류출력-설계.html).

## 처리 흐름

```
문서 → [사이냅 snf_exe] 텍스트 → 정제 → 규칙 스캔(rule·sensitive·stamp·path·name 융합) → 등급 레코드
        └(--hybridparse: 내용감지→포맷별 파서(pdf=pypdfium2·hwp/hwpx·doc/xls/ppt·docx/xlsx/pptx·text), 실패 시 snf 폴백)
                                     └(선택) 토큰 청킹 → [ONNX 임베더] 벡터 → 전파(보류 구제)
```

## 상주 데몬 모드 (기본 on)

반복 호출 시 모델을 매번 로딩하지 않도록, 첫 호출에서 데몬을 자동 기동해 모델을 메모리에
상주시킵니다(설계서 결정5). 2회차부터 모델 로딩 시간은 0 입니다.
**`--embed` 뿐 아니라 분류(`--file`/`--dir`)도** 임베딩이 필요할 때(자동 전파·`--embed-needed`·`--with-vector`) 데몬을 경유해 웜 모델을 재사용합니다(데몬 불가 시 in-process 폴백; 규칙만으로 끝나면 데몬 미사용).

- 모델별 별도 데몬(레지스트리 키=모델 별칭), loopback + 토큰 인증.
- 동시 요청 경쟁은 **엔드포인트/잠금 원자적 생성**으로 데몬 1개만 생존.
- **유휴 자동종료는 기본 무한**(자동 종료 안 함) — `--idle-timeout N`(초)으로 유한하게 둘 수 있고, `--stop` 으로 즉시 종료.
- 기동/통신 실패 시 **in-process 자동 폴백** → 멈추지 않음.
- 단발/디버그는 `--no-daemon`(또는 `CSOCLASSIFY_DAEMON=0`).
- **데몬 프로세스**는 콘솔 창 없이 뜨며 이름은 `csoclassify.exe`(소스 실행 시 `python.exe`). 찾기: `csoclassify.exe --status`(pid 표시), 작업관리자 **세부 정보** 탭, 또는 `Get-Process csoclassify`.

**Rust 판의 임베딩 상주 서버** — Rust 판은 기본이 직접 로드(`--no-daemon`)이고, `--daemon` 을 주면 임베딩을 상주 서버에
맡깁니다(서버가 없으면 자동으로 띄움). 인자 이름과 뜻은 Python 판과 같습니다.

- `--serve` 는 이 프로세스가 서버가 됩니다(정지 요청·유휴 종료까지 실행). `--status` · `--stop` 으로 조회·정지.
- `--serve` · `--status` · `--stop` 의 출력은 언제나 **JSON 한 줄**입니다:
  `{"error":{"code":0,"kind":"success","message":"…"},"daemon":{"running":true,"pid":…,"port":…,"ready":true,…}}`
  — 서버가 없다는 답도 `code 0` 이고, `daemon.running` 으로 구분합니다.
- 설계: [Rust 임베딩 상주 서버](plan/Rust-임베딩모델-상주서비스-설계.html)

## 개발 셋업

```bash
pip install -r requirements.txt          # ko-pii, onnxruntime, tokenizers, numpy, pyyaml ...
$env:PYTHONPATH="src"; python -m csoclassify --file "D:\docs\보고서.hwp"   # 소스 실행
python -m pytest tests/ -q               # 테스트(사이냅 통합은 snf_exe 있을 때만)
```

### 임베딩 모델을 ONNX 로 준비 (최초 1회, 인터넷 필요)

런타임(exe)은 `onnxruntime`+`tokenizers` 만 쓰지만, 모델을 처음 만들 때는 `optimum`/`transformers`/`torch` 가 필요합니다.

```bash
pip install "optimum[onnxruntime]" transformers
python scripts/export_onnx.py   --model e5-small-ko   # fp32 model.onnx(470MB) 생성
python scripts/quantize_onnx.py --model e5-small-ko   # int8 경량화(→118MB, 랭킹 보존)
```
→ `resources/models/e5-small-ko/{model.onnx, tokenizer.json, ...}` 생성(저장소 미포함 — 용량/라이선스).

## 빌드 · 배포 (onedir)

> **처음 빌드한다면 → [빌드 가이드(Python)](doc/MpowerClassify-빌드가이드-python.html)** — Windows·Linux 전 과정을
> 단계별로(설치·spec 설명·트러블슈팅·복붙용 명령) 정리한 문서입니다. 아래는 요약입니다.
> Rust 포팅판(`Rust/`)은 [빌드 가이드(Rust)](doc/MpowerClassify-빌드가이드-rust.html) 를 보세요.

정식 배포는 **onedir 2종(Windows·Linux)** 입니다. 실행본체(`csoclassify`)와 파이썬 런타임(`_internal/`)이
한 폴더에 있고, **규칙셋·모델·사이냅은 그 옆에 외장 리소스**로 둡니다(재빌드 없이 규칙 교체 가능,
매 실행 압축해제 없어 빠름 ~2초).

```bash
# Windows (로컬)
pyinstaller build/csoclassify.spec        # → dist/csoclassify/{csoclassify.exe, _internal/}

# Linux (반드시 리눅스에서 — 크로스컴파일 불가). conda 예:
pip install pyinstaller onnxruntime tokenizers numpy pyyaml ko-pii==1.15.2
pyinstaller build/csoclassify.spec        # → dist/csoclassify/csoclassify (ELF)
```

빌드 후, 실행파일 옆에 외장 리소스를 배치합니다.

```
dist-onedir/windows/                    dist-onedir/linux/
├─ csoclassify.exe + _internal/         ├─ csoclassify-onedir-linux.tgz  (리눅스에서 tar 풀기: 심볼릭링크 보존)
├─ cso_rule.yaml                       ├─ cso_rule.yaml
├─ synap/windows/{snf_exe.exe,          ├─ synap/linux/snf_exe
│              snf_win.dll}             ├─ models/e5-small-ko/
└─ models/e5-small-ko/                  └─ README.txt
```

- **Windows**: `dist-onedir/windows/csoclassify.exe --file "문서"` — 바로 실행.
- **Linux**: 폴더를 리눅스로 복사 → `tar xzf csoclassify-onedir-linux.tgz`(**반드시 리눅스에서** —
  Windows에서 풀면 `_internal`의 .so 심볼릭링크가 깨짐) → `chmod +x csoclassify synap/linux/snf_exe`
  → `./csoclassify --file "문서"`.
- 규칙셋이 없으면 실행 시 어디에 둘지 안내하고 종료(코드 3).
- 규칙셋은 **로드 시점에 검증**합니다 — 정의되지 않은 등급(`base_grade: c` 같은 오타),
  `bulk_grade` 가 `base_grade` 보다 낮은 역전, 아무 일도 하지 않는 경로 규칙(`grade`·
  `acl_restricted` 둘 다 없음)이 있으면 **문서를 한 건도 읽지 않고** 위반을 전부 모아
  보여 주고 종료(코드 4). 예전에는 이런 값을 조용히 무시해 그 규칙이 판정에서
  빠졌고, 문서가 실제보다 낮은 등급을 받았습니다.
  배포 전 미리 확인: `csoclassify --check-rules --cso-rules cso_rule.yaml` (문서 불필요)
- 경로 규칙(`paths`)은 `acl_restricted: true` 인 경우 `grade` 를 **생략**할 수 있습니다. 그러면
  스스로 등급을 내지 않고, 내용·파일명에서 아무 신호도 없을 때만 fail-safe 로 최고 등급을
  줍니다 — "보안 폴더인데 내용을 못 읽는 파일"이 미분류로 새는 것을 막는 안전망입니다.
- 리눅스 `snf_exe` 는 정적 링크라 `.so`/헤더 없이 단독 실행.

> onefile(단일 exe) 방식(`build/csoclassify-onefile.spec`)은 스펙만 남겨두고 **현재 빌드 대상이 아닙니다**
> (매 실행 임시폴더 해제로 느리고 데몬이 임시 `snf_exe` 참조에 실패). onedir만 유지·배포합니다.

### 환경변수 · 로그

| 변수 | 설명 |
|---|---|
| `CSOCLASSIFY_POLICY_DIR` | 규칙셋 폴더(`--cso-rules` 로도 지정) |
| `CSOCLASSIFY_MODELS_DIR` / `CSOCLASSIFY_SYNAP_DIR` | 모델 / 사이냅 위치 재정의 |
| `CSOCLASSIFY_DAEMON=0` | 데몬 전역 off |
| `CSOCLASSIFY_HYBRID=1` | 하이브리드 추출(`--hybridparse`) 전역 기본 on |
| `CSOCLASSIFY_STAMP_HEAD_CHARS` / `_REPEAT_MIN` | 스탬프 머리범위(400) / 반복횟수(2) |
| `CSOCLASSIFY_MRK` | [복호화] 평문 MRK(`--mrk` 대신) |
| `CSOCLASSIFY_DECRYPT_CMD` / `CSOCLASSIFY_DECRYPT_TMP` | [복호화] 복호화 도구 경로 / 임시 폴더 |

- 기본 로그: `<exe폴더>/log/class_YYYYMMDD.log`(실행 커맨드·파일별 결과 JSON, 벡터는 축약).
  오류만 모은 `<exe폴더>/log/class_err_YYYYMMDD.log` 도 같은 폴더에 남는다.
  경로는 `--log`, 화면 출력·DEBUG는 `-v`. 데몬도 같은 파일에 `[PID …]` 로 구분해 기록.

## 문서자동분류 UI (Streamlit)

```bash
pip install -r ui/requirements.txt
streamlit run ui/app.py        # 또는 ui/실행.bat
```
관리자용 화면 **문서자동분류** 입니다. 현황 대시보드·문서함·검토함·seed 관리·
**판단 기준 편집**(보안등급 규칙 + 업무분류 규칙, 주석 보존)·균형/중복 리포트를 제공합니다.
수동 오버라이드는 원본 불변, `cso_override.jsonl` 에 append-only 로 기록.

## 프로젝트 구조

```
src/csoclassify/
  cli.py            CLI 진입점(모드 분기, 데몬 우선+폴백)
  filelist.py       입력 목록(--filelist) 읽기 · doc_id 사전 · 복호화 열쇠 칸
  decrypt.py        암호화 문서 복호화(removeenc.sh 호출 · 풀린 파일 검증 · 즉시 삭제)
  errcodes.py       오류 코드 표(두 판 공통 계약)
  pipeline.py       추출→정제→임베딩 오케스트레이션(+시간기록)
  clean.py chunk.py 텍스트 정제 · 토큰 청킹
  classify/         분류 정책층
    rules.py        규칙 스캔(PII·combo·기밀사전·민감정보·스탬프) + 규칙셋 로드
    context.py      경로(path)·파일명(name) 신호
    fuse.py         보수적 max 융합 + fail-safe
    engine.py       분류 레코드 조립 + 임베딩 전파(propagate_records)
    propagate.py    seed 인덱스 · 임베딩 유사도 전파
  extract/          추출기: synap_exe(사이냅) + detect(내용감지) + hybrid 라우터
                     + hwp5·hwpx_zip·office_legacy(doc/xls/ppt)·office_ooxml(docx/xlsx/pptx)·pdf_pdfium·plaintext
  embed/            ONNX 임베더(base + onnx_embedder)
  daemon/           상주 데몬(server/client/ipc/registry)
ui/policy/          규칙의 원본(고객 파일): cso_rule.yaml · doc_rule.yaml · 유의어 사전 · 기준 문서
resources/policy/   새 고객이 복사해 가는 제품 기본값(동기화 대상 아님)
ui/                 Streamlit 관리자 화면 "문서자동분류"(검토·판단 기준 편집)
Rust/               Rust 포팅판(같은 인자·결과·오류 코드, 임베딩 상주 서버 포함)
build/*.spec        PyInstaller 스펙
scripts/build/      빌드 스크립트(리눅스 빌드 서버용 linux-build.sh 등)
doc/                실행 가이드 · 규칙셋 레퍼런스 · 빌드 가이드(Python/Rust) · 작업기록 (HTML)
```

## 현재 상태

- ✅ 추출(사이냅)·정제·청킹·CLI·데몬(싱글턴/폴백)·출력·시간측정.
- ✅ 하이브리드 추출(`--hybridparse`): **내용 감지 → 포맷별 오픈소스 파서**(pdf=pypdfium2·hwp/hwpx·구형 doc/xls/ppt·현대 docx/xlsx/pptx=zip/OOXML 직접파싱(stdlib)·text). snf는 폴백 전용(없으면 미분류). 실측 snf 대비 동급~수십배, 품질 sim≈1.0. 전부 순수py/소형 wheel로 **Windows·Linux(CentOS7) 동일 동작**(xberg는 검토 후 채택 안 함 — 117MB·CentOS7 불가·PDF 느림).
- ✅ C/S/O 분류: 5신호(rule·sensitive·stamp·path·name) 융합 + 결합식별성(combo) + 임베딩 전파(embed).
- ✅ PII 20종(ko-pii) · 분류 방식 옵션(`--rule-only`/`--vector-only`) · 문서자동분류 UI(검토·판단 기준 편집).
- ✅ onedir 배포(Windows·Linux) · 규칙셋/모델/사이냅 외장화.
- ✅ 문서 식별자 `doc_id` · 입력 목록 `--filelist`(설계 D0·D0b) — 결과 레코드에
  `doc_id`(문서 저장소의 `sfile_id`, 없으면 내용/경로 해시 폴백) · `key`(정규화 경로) ·
  `doc_id_source` 를 심는다. 검토 화면은 `doc_id → 정규화 경로 → 대소문자` 순으로
  결과와 사람의 수정을 잇는다(경로 표기 차이로 수정이 유실되던 문제 해소).
  Python·Rust 동일 동작.
- ✅ 오류 출력 계약: 두 판이 같은 상황에 같은 코드(1000~9000 대역)를 냄.
- ✅ Rust 판: 임베딩 모델 세 가지(e5-small-ko · e5-small · ko-sroberta) 지원 · 임베딩 상주 서버(`--serve`/`--daemon`/`--status`/`--stop`).
- ✅ 암호화 문서 복호화 연동(Python·Rust) — 실제 서버의 암호화 문서로 풀린 내용이 원본과 같음을 확인.
- ✅ 단위/통합 테스트 통과(Rust 305건 · Python 1,052건, 2026-10-14 기준).

## 라이선스 · 주의

- 사이냅 문서필터(`snf_exe`)의 재배포는 사이냅 라이선스 조건 확인이 필요합니다(설계서 §14).
  이 저장소에는 사이냅 바이너리·임베딩 모델·자격증명을 포함하지 않습니다.
- `--with-text` / `--with-pii` 는 원문 텍스트·PII를 결과 파일에 남기므로 민감문서 처리 시 주의하세요.
