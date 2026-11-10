# MpowerV10 개인정보검출 정리 (MpowerClassify 비교용)

작성: 2026-11-05 · 근거 소스: `C:\svn\Mpower_v10\PHP\Mpower_10.0` (SVN 작업본)

---

## 1. 한눈에 보기

| 항목 | MpowerV10 | MpowerClassify |
|---|---|---|
| 검출 엔진 | PHP 확장 `mpiisl_*` (네이티브 엔진 "mpiis", 소스는 이 저장소에 없음) | ko-pii 1.15.2 (MIT, 순수 파이썬) + 자체 가드 |
| 문서 파싱 | 엔진 내부 (사이냅 필터 — 릴리스노트에 "synap filter 파싱 중 PHP crash" 기록) | `extract/` (사이냅 exe / 하이브리드 파서) |
| 검출 방법 | 정규식 + 검증 모듈(VERIFIER) + 예외값 목록 | 정규식 + 체크섬/화이트리스트/키워드 앵커 (ko-pii) |
| 정책 위치 | DB (`PI_POLICY_INFO` / `PI_POLICY_CONFIG` / `PI_PATTERN_INFO`) | `resources/policy/cso_rule.yaml` (`regex_pii`, `pii_combos`) |
| 정규식 공개 | 기본 패턴(B형)은 **암호화+BASE64 로 저장 → 관리자도 못 봄** | ko-pii 소스 그대로 공개 |
| 결과 형태 | 파일 상태코드 (`NO`/`PL`/`PI`/…) + 패턴별 건수 | 보안등급 (C/S/O) 근거 히트 (라벨 + 건수) |
| 결과의 쓰임 | 열기·다운로드·직접편집·M드라이브 **차단**, 통계, 만료/삭제 관리 | 보안등급 산정에 합산 (상향 전용) |
| 기본 판정선 | 패턴별 **20건 이상**이어야 `PI` (IP 는 25) | 1건부터 base_grade, **5건 이상** bulk_grade |

---

## 2. MpowerV10 검출 유형 (기본 정책 `PI_DEFAULT`, 12종)

출처: `www/classes/class.MShaPI.php` `insertDefaultPolicyForOracle()`,
`www/admin/init_set_data/db_factory_data_check.php:241-264`

| 순서 | PPTI_ID | 이름 | 검증 모듈(VERIFIER_NAME) | MIN_LIMIT | 기본 사용 | 설명(PPTI_DESC 요약) |
|---|---|---|---|---|---|---|
| 1 | `ip` | IP 주소 | — | 25 | **끔(0)** | 정확히 IP 주소인 경우 |
| 2 | `jumin` | 주민번호 | `CPIIVerifierJuminHard` | 20 | 켬 | 앞뒤에 숫자·대시(또는 숫자+공백, 대시+공백)가 붙으면 검출 안 함 |
| 3 | `FOREIGN` | 외국인번호 | `CPIIVerifierForeignHard` | 20 | 켬 | 주민번호와 같은 경계 규칙 |
| 4 | `bsnno` | 사업자등록번호 | `CPIIVerifierBSNNumber` | 20 | 켬 | 주민번호와 같은 경계 규칙 |
| 5 | `incno` | 법인등록번호 | `CPIIVerifierINCNumber` | 20 | 켬 | 주민번호와 같은 경계 규칙 |
| 6 | `email` | 전자우편주소 | — | 20 | 켬 | 앞뒤 숫자·`@` 붙으면 제외, 중간 도메인 2~3자만 |
| 7 | `passport` | 여권번호 | — | 20 | 켬 | 숫자·엔터 외 모든 문자를 구분자로 처리 |
| 8 | `tel` | 전화번호(유선) | — | 20 | 켬 | 같음. **예외값**: `02-2141-4105, 02-2141-4161, 02-2141-4150` |
| 9 | `cell` | 핸드폰번호 | — | 20 | 켬 | 숫자·엔터 외 모든 문자를 구분자로 처리 |
| 10 | `creditcard` | 카드번호 | `CPIIVerifierCreditCardNumber` | 20 | 켬 | 정확히 카드번호인 경우 |
| 11 | `driving` | 운전면허 | — | 20 | 켬 | 정확히 운전면허인 경우 |
| 12 | `account` | 계좌번호 | — | 20 | 켬 | 번호 사이 공백·`[]`·`()`·`{}`·콤마가 있어도 검출 |

- **정규식 자체는 알 수 없음.** `PI_REGEXP` 값이 `1,101,128,14,<IV>,<암호문>:<md5>` 형태로 암호화돼 있고,
  복호화는 엔진(`mpiisl_load_policy_from_memory`) 안에서만 한다. 위 "설명" 칸이 공개된 유일한 동작 설명이다.
- 검증 모듈이 붙은 것은 5종(주민·외국인·사업자·법인·카드)뿐. 나머지 7종은 정규식만으로 센다.
- 2016년 판 SQL(`backup/db/개인정보검출_20160927(MYSQL).sql`)은 주민번호 등의 설명이 지금 계좌번호 설명과 같았음
  ("번호 사이 공백 [],(),{},콤마 허용") → 이후 경계 규칙을 엄격히 바꾼 흔적.

### 패턴 유형 (PPTI_TYPE)
| 값 | 뜻 |
|---|---|
| `B` | 기본 패턴. 정규식 암호화 저장, 관리자 화면에서 내용 조회 불가 (사용 여부·순서·MIN_LIMIT·예외값은 수정 가능) |
| `C` | 사이트 사용자 정의 패턴. 정규식 평문 저장, 언제든 수정 |

### 패턴별 설정 칸 (`PI_PATTERN_INFO`)
`PPTI_SEQ`(검출 순서) · `MIN_LIMIT`(판정 최소 건수) · `PI_EXCLUDE`(콤마로 나열한 예외값 — 이 값은 세지 않음) ·
`VERIFIER_NAME`(정규식 뒤 추가 검증, 체크섬 등) · `STATUS`(0 끔 / 1 켬)

---

## 3. MpowerV10 검출 방법·흐름

### 3-1. 언제 검사하나
| 경로 | 소스 | 설명 |
|---|---|---|
| 업로드 후 배치 | `MpowerScript/classes/class.ShaFileBase.php` `scanPersonalInfoBase()` | 파일 등록 배치가 복호화된 파일(`LAST_DEC_PATH`)로 검사 |
| 정책 변경 후 재검출 | `MpowerScript/pi_batch/pi.rescan.php` (`pi.rescan.sh`) | `SHARE_FILE_INFO.PPCI_UUID ≠ 현재 정책 UUID` 인 파일만 다시 검사. 이력은 `PI_RESCAN_LOG` |
| 관리자 상세 조회 | `MShaPI::getPIDetailResult()` | 저장된 파일을 그 자리에서 다시 복호화·검사해 상세(값)/마스킹 결과를 보여 줌. **상세 값은 DB 에 저장하지 않음** |
| 요약 통계 배치 | `MpowerScript/pi_batch/pi.summary.php` → `class.ShaPISummary.php` | 조직·패턴·문서유형·프로젝트별 요약 (`PI_SUMMARY_*`) |

### 3-2. 한 파일 검사 순서 (`MShaPI::initialize()` → `scanFile()`)
1. DB 에서 현재 유효한 정책 3종을 읽어 INI 형식 문자열로 만들고 엔진에 올림 (`mpiisl_load_policy_from_memory`, `mpiisl_set_config`).
2. 크기 0 파일 → 바로 `NO`.
3. 같은 저장 파일(`RFILE_NAME`)이 같은 정책 UUID 로 이미 검사됐으면(`PI_RESULT`) 결과 재사용.
4. DRM 처리
   - 소프트캠프: `mpiisl_scan_file_drm` — 압축 안의 DRM 파일을 뽑아 따로 복호화한 뒤 재귀 검사, 건수 합산.
   - 파수: 먼저 `decryptDRM` 으로 풀고 검사.
5. 엔진 호출 `mpiisl_scan_file(파일, detail, 0, "UTF-8", …)` → `"jumin=3;cell=25;..."` 형태 패턴별 건수 반환.
6. 선택: `__SFILTER_OPT use_fork_for_scan=1` 이면 fork 한 자식 프로세스에서 검사 (엔진 SIGSEGV·시간초과 격리).

### 3-3. 판정 규칙 (코드 기준)
```
패턴별 건수 >= 그 패턴의 MIN_LIMIT  →  FILTER_TOTAL 에 더함
FILTER_TOTAL > 0                     →  PI  (개인정보 포함)
전체 건수 > 0 이지만 FILTER_TOTAL = 0 →  PL  (포함, 기준 미만)
전체 건수 = 0                         →  NO
```
- DB 주석은 "이 개수 **이하**면 개인정보로 처리 안 함"이라 적혀 있지만, 실제 코드는 `건수 >= MIN_LIMIT` 이면 PI (정확히 20건이면 PI).
- 기본값 20 → **주민번호가 19건 있어도 `PL`** 이고, 기본 차단 목록(`WT,WR,ER,PI`)에 PL 이 없어서 제한되지 않는다.

### 3-4. 상태 코드 (`www/common/global_val.php:646-655`)
| 코드 | 뜻 | 누가 |
|---|---|---|
| WT / WR | 대기 / 재검색 대기 | 시스템 |
| ER / EX | 오류 / 대상 아님(모르는 형식) | 시스템 |
| NO / PL / PI | 없음 / 기준 미만 / 포함 | 시스템 |
| MS / DL / IG | 오탐 처리 / 관리자 삭제 / 포함이나 제한 안 함 | 관리자 (사유 입력) |

### 3-5. 엔진 한도 (기본값, `db_factory_data_check.php:241,246`)
| 설정 | 기본값 | 뜻 |
|---|---|---|
| MAX_FILE_SIZE | 10 GB | 이보다 큰 파일은 검사 안 함 |
| MAX_FIND_LIMIT | 1000 | 패턴별 이만큼 찾으면 그 패턴은 그만 셈 |
| MAX_COMPRESS_DEPTH | 3 | 압축 안의 압축 3단계까지 (0 이면 압축 처리 안 함) |
| MEM_FILE_SIZE | 100 MB | 이 이하 파일은 통째로 메모리, 넘으면 블록 단위 |
| MEM_FIND_LIMIT | 200 | 메모리에 담을 최대 검출 수 (넘으면 결과를 파일로) |
| MAX_PROC_TIME | 100 초 | 넘으면 중단·오류 (0 = 무제한) |
| WORK_DIR | `/MOCOMSYS/mpiis/temp` | 임시 작업 폴더 |

### 3-6. 검출 후 조치
- `__PI_DOWNLOAD_LIMIT = "WT,WR,ER,PI"` — 이 상태의 파일은 열기·다운로드·직접편집 차단, 폴더 다운로드도 실패 처리.
- `__PI_MDRIVE_LIMIT = "WR,ER,PI"` — M드라이브 접근 차단 (`__PI_EXIST_PROCESS` 로 예외 프로세스 지정).
- 관리자 화면 `www/admin/personal_information/` — 정책(`manage/policy*`), 엔진 설정(`manage/setting*`),
  담당자·실시간 현황(`stat/manager*`, `stat/realtime_manager*`), 재검출(`stat/redetect*`), 로그(`log/Privacy_list.php`).
- 사용자 접속기 API `www/cs/sha/reqShaManagePiFileCns.php` — `L` 개인정보 파일 목록, `LR` 실시간 조회, `LD` 만료 삭제 파일.
- 사이트 맞춤: `www/classes/custom/class.WOORICAP_MShaPI.php`, `class.DWJB_MShaPI.php` (조회 화면만 재정의).

### 3-7. 저장 테이블 (`DBScript/Mpower10U_MYSQL_20190523.sql:3842-4290`)
| 테이블 | 내용 |
|---|---|
| `PI_POLICY_INFO` / `PI_POLICY_CONFIG` / `PI_PATTERN_INFO` | 정책 (유효기간 이력식, 정책 바뀌면 새 `PPCI_UUID`) |
| `PI_RESULT` | 저장 파일별 전체 건수 + 관리자 변경 상태 |
| `PI_RESULT_DETAIL` | 저장 파일 × 패턴별 건수 (값은 없음) |
| `SHARE_FILE_INFO.PI_STATUS / PPCI_UUID` | 파일 목록에 붙는 상태 |
| `PI_RESCAN_LOG` | 재검출 배치 이력 |
| `PI_SUMMARY_ORG / PATTERN / DOC_TYPE / PROJECT` | 요약 통계 |
| `PI_BATCH_JOB_*`, `PI_ODS` | 배치 작업 정보 |

---

## 4. 유형 대응표 (V10 ↔ MpowerClassify)

MpowerClassify 쪽: `resources/policy/cso_rule.yaml` `regex_pii` (base / bulk 등급, bulk 기준 5건)

| V10 | MpowerClassify 라벨 | MC 등급 (1건~ / 5건~) | V10 검증 | MC 검증 (ko-pii) |
|---|---|---|---|---|
| jumin 주민번호 | `RRN` | S / C | 표준 체크섬, **틀리면 버림** (실측) | 7번째 자리(1·2·3·4·9·0) + 실제 날짜. 체크섬은 **틀려도 검출**(신뢰도만 0.7, MC 는 신뢰도로 거르지 않음) |
| FOREIGN 외국인번호 | `FRN` | S / C | **옛 외국인번호 검증식(표준 검증숫자 + 2)** 만 받음 (실측) | 7번째 자리(5~8) + 날짜. 체크섬은 **틀려도 검출** |
| bsnno 사업자번호 | `BUSINESS_REG` | O / S | 체크섬 검사함. 단 맞는 값 10건 중 8건은 `account`(계좌)로 분류됨, 틀린 값은 전부 `account` | 국세청 가중합 체크섬 (틀리면 버림) |
| incno 법인번호 | `CORP_REG` | O / S | 체크섬 + 실재 등기소코드만 받음 (실측) | 법인 체크섬 (틀리면 버림) |
| creditcard 카드 | `CARD` | S / C | Luhn 체크섬, 틀리면 버림 (실측) | Luhn + 카드사 IIN 화이트리스트 |
| account 계좌 | `ACCOUNT` | S / C | 정규식만 | **"계좌" 키워드 앵커 필수** |
| passport 여권 | `PASSPORT` | S / C | 정규식만 | 영문 1~2자 + 숫자 8자리 형식 |
| driving 운전면허 | `DRIVER_LICENSE` | S / C | 정규식만 | 12자리 + 지방청 코드 범위 |
| email | `EMAIL` | O / S | 정규식만 (중간 도메인 2~3자 제한) | 단순화한 RFC 5322 |
| tel 유선 + cell 휴대폰 | `PHONE` (하나로 합침) | O / S | 정규식만, tel 예외값 3개 | 휴대/일반/인터넷/국제 모두 (`extra.type` 으로 구분 가능) |
| ip (기본 끔) | `IP` | O / S | 정규식만 | ko-pii + 판번호 문맥 배제 가드 (차이 B) |
| — | `ADDRESS` | S / C | 없음 | ko-pii + 가짜 시군구 거르기 (차이 A) |
| — | `MEDICAL_INSURANCE` · `PRESCRIPTION_ID` | C / C | 없음 | ko-pii |
| — | `EDI_DRUG` · `VEHICLE` · `COURT_CASE` · `PNU` · `NATIONALITY` · `URL` | S·O / C·S | 없음 | ko-pii |
| — | 결합 규칙 `pii_combos` (주민+계좌, 주민+카드, 주민+주소, 외국인+계좌 → C, 연락처 2종 이상 → S) | — | 없음 | 문서 단위 공존 |

---

## 5. 비교할 때 주의할 차이

1. **판정선이 크게 다르다.** V10 은 패턴별 20건(기본)이 넘어야 `PI`, 그 아래는 `PL`(제한 없음).
   MpowerClassify 는 1건만 나와도 base_grade 가 붙고 5건이면 bulk_grade.
   → 같은 파일이라도 "주민번호 3건"은 V10 `PL` / MC `S` 로 갈린다. 비교는 **상태·등급이 아니라 패턴별 건수**로 해야 공정하다.
2. **V10 정규식은 볼 수 없다.** B형이 암호화돼 있어 소스 대조는 불가능.
   같은 파일 묶음을 양쪽에 돌려 **패턴별 건수를 블랙박스로 비교**하는 수밖에 없다
   (V10 은 `PI_RESULT_DETAIL`, 또는 엔진 직접 호출 `mpiisl_scan_file`).
3. **계좌번호 방식이 정반대.** V10 은 문맥 없이 숫자 모양 + 구분자 허용 → 오탐 많을 가능성,
   MC 는 "계좌" 키워드가 근처에 있어야 함 → 표에 계좌만 나열된 문서는 놓칠 가능성.
4. **전화번호:** V10 은 유선/휴대 분리 + 회사 대표번호 예외값. MC 는 `PHONE` 하나, 예외값 기능 없음
   (MC 의 `exclude` 는 키워드·민감·스탬프 규칙에만 있고 PII 규칙엔 없다).
4-1. **V10 "검증모듈" ≠ MC "체크섬".** 6절 실측 결과 — V10 은 주민·외국인·사업자·법인·카드 모두
   틀린 검증숫자를 버린다. MC(ko-pii)는 사업자·법인·카드만 버리고 **주민·외국인은 틀려도 센다**
   (2020년 이후 주민번호는 체크섬이 안 맞을 수 있어서 ko-pii 가 일부러 그렇게 함 → V10 은 이런 번호를 못 잡는다).
   외국인번호는 V10 이 **옛 규칙(검증숫자 +2)** 을 써서, 표준 검증식으로 만든 값은 0건, +2 규칙 값만 잡는다.
5. **유형 범위:** MC 가 주소·건강보험·처방전 등 8종 더 넓다. V10 에서 IP 는 기본 꺼짐.
6. **크기·범위 한도:** V10 은 10 GB 파일·압축 3단계·패턴당 1000건·100초.
   MC 는 텍스트 200만 자 상한, PHONE/ADDRESS/ACCOUNT 는 1만 자 창(128자 겹침)으로 앞 100만 자까지.
   압축 처리 깊이는 MC `extract/archive.py` 와 따로 대조 필요.
7. **값 보관:** 둘 다 DB 엔 건수만 둔다. V10 은 관리자가 상세 볼 때 다시 검사해 값/마스킹을 보여 주고,
   MC 는 `--with-pii` 를 줄 때만 원문 값을 결과에 싣는다.

---

## 6. 체크섬 실측 (2026-11-05, 이 PC 의 V10 엔진)

유형마다 몸통 10개 × 검증숫자 0~9 = 100건 (정답 10 / 오답 90). 값 1개당 파일 1개.
V10 은 `tools/v10pii/v10pii.py`(아래 7절), MC 는 실제 계수 경로 `rules._kopii_counts`.
스크립트 `tools/v10pii/checksum_probe.py`, 원자료 `tools/v10pii/checksum_probe_result.json`.

| 유형 | 정답 V10 | 정답 MC | 오답 V10 | 오답 MC | 비고 |
|---|---|---|---|---|---|
| 주민번호 | 10/10 | 10/10 | **0/90** | **90/90** | MC 는 체크섬 틀려도 셈 |
| 외국인번호 | **0/10** | 10/10 | 6/90 | 90/90 | V10 이 받은 6건은 전부 "표준 검증숫자 + 2" 값 (7번째 5·6). 7·8 은 생년이 미래(20xx)라 날짜 검사에서 빠진 것으로 보임 |
| 사업자번호 | 2/10 | 10/10 | 0/90 | 0/90 | V10: 맞는 값 8건·틀린 값 90건 전부 `account` 로 잡힘 |
| 법인번호 | 9/10 | 8/10 | 0/90 | 0/90 | 실재 등기소코드 사용. MC 는 2건을 FRN 으로 셈, V10 은 1건을 FOREIGN 으로 셈 |
| 카드번호 | 10/10 | 10/10 | 0/90 | 0/90 | 동일 |

## 7. V10 엔진을 이 PC 에서 돌리기 — `tools/v10pii/v10pii.py`

서버(19.40)·DB·PHP 없이, V10 개인정보 검출엔진 DLL 만 파이썬에서 직접 부르는 자체 시험환경이다.

### 7-1. 구성 원리
```
v10pii.py ──ctypes──▶ mpiisl.dll (V10 개인정보 엔진)
   │                     ├─ snf_win.dll      문서 → 텍스트 (사이냅 필터)
   │                     ├─ pcre2-16.dll     정규식
   │                     ├─ MocoCrypto64.dll 암호화 정규식 풀기
   │                     └─ SDMakeLib64.dll
   └─ db_factory_data_check.php 에서 기본 패턴 12종(암호화 정규식 그대로)을 읽어
      MShaPI::initialize() 와 같은 INI 문자열로 만들어 엔진에 넣는다
```
- 호출 순서(`mpiisl.h`): `mpiisl_init` → `mpiisl_create_scanner` → `mpiisl_load_policy_from_memory` →
  한도 설정(`set_max_file_size` 등, 서버 기본값과 같게) → `mpiisl_set_work_dir_w` →
  `mpiisl_scan_file_w(파일, 2, 0, "UTF-8")` → `mpiisl_get_pi_count_by_id` / `mpiisl_get_pi_detail_result_value_by_id`
- 정규식은 암호화된 채로 넘기면 엔진이 안에서 푼다 → 내용은 끝까지 볼 수 없지만 **돌리는 데는 문제없다**.
- 패턴 12종은 기본으로 **모두 켠다**(설치 기본값에선 IP 가 꺼져 있지만, 서버 비교 때 정책은 IP 를 켠 상태였다).
  `V10PII(all_on=False)` 로 만들면 설치 기본값(IP 끔)을 따른다.

### 7-2. 준비물
| 항목 | 내용 | 이 PC 위치 |
|---|---|---|
| OS | Windows 64비트 | — |
| Python | **64비트** 3.x (DLL 이 x64 라 32비트 파이썬으론 못 올림). 추가 패키지 필요 없음 (`ctypes` 만 씀) | 3.12.10 64bit |
| VC++ 런타임 | **Visual C++ 2010 재배포 패키지 x64** (`msvcp100.dll`, `msvcr100.dll`) — `mpiisl.dll` 이 이것을 씀 | `C:\Windows\System32` 에 있음 |
| 엔진 DLL 5개 | `mpiisl.dll` · `snf_win.dll` · `pcre2-16.dll` · `MocoCrypto64.dll` · `SDMakeLib64.dll` | `C:\svn\SRC_V10.0B20170828\Release_VS2019_x64\` (VS2017_x64 판도 같은 구성) |
| 패턴 원본 | V10 설치 스크립트 `db_factory_data_check.php` (기본 12종 INSERT 문이 들어 있음) | `C:\svn\Mpower_v10\PHP\Mpower_10.0\www\admin\init_set_data\` |
| 도구 | `v10pii.py` (+ 비교 시험 `checksum_probe.py`) | `D:\Project\MpowerClassify\tools\v10pii\` |
| (비교 시 만) ko-pii | MC 쪽 계수용. MpowerClassify 개발환경에 이미 설치됨 | — |

> 참고: `C:\svn\SRC_V10.0B20170828\Common_VS2019\Include\mpiisl.h` 가 API 설명서다(오류코드 3000~3999 포함).

### 7-3. 이 PC 에서 바로 실행
```bash
cd D:\Project\MpowerClassify
python -X utf8 tools/v10pii/v10pii.py 검사할파일1 검사할파일2
```
출력(파일마다 한 줄):
```
t1.txt {"ret": 0, "counts": {"jumin": 1, "email": 1, ...}, "values": {"jumin": ["900101-1234568"], "email": ["hong@example.co"], ...}}
```
- `ret` 0 = 성공. 0 이 아니면 `mpiisl.h` 오류코드 (예: 3024 모르는 형식, 3061 최대 크기 초과, 3062 시간 초과). 이때 `extractor` 에 사이냅 쪽 코드가 붙는다.
- `counts` = 패턴ID 별 건수 (서버의 `PI_RESULT_DETAIL` 과 같은 값), `values` = 검출된 원문 값.
- 파이썬 코드에서 쓰기:
  ```python
  import sys; sys.path.insert(0, r"D:\Project\MpowerClassify\tools\v10pii")
  from v10pii import V10PII
  eng = V10PII()                          # DLL 올리기 + 정책 넣기 (한 번만)
  r = eng.scan(r"C:\샘플\문서.hwp")         # values=False 면 건수만 (빠름)
  ```
- 작업 폴더 `tools/v10pii/mpiis_work/` 가 자동으로 생긴다(엔진 임시파일).

### 7-4. 다른 PC 로 옮기기 (SVN 없이)
1. 새 폴더 하나에 아래처럼 복사한다.
   ```
   v10pii\
     v10pii.py
     db_factory_data_check.php
     engine\  mpiisl.dll  snf_win.dll  pcre2-16.dll  MocoCrypto64.dll  SDMakeLib64.dll
   ```
2. 대상 PC 에 64비트 Python 과 **VC++ 2010 x64 재배포 패키지**를 깐다.
3. 환경변수로 경로를 알려 주고 실행한다.
   ```bat
   set V10PII_DLL_DIR=C:\v10pii\engine
   set V10PII_PATTERN_SRC=C:\v10pii\db_factory_data_check.php
   rem 작업 폴더를 바꾸려면: set V10PII_WORK_DIR=D:\temp\mpiis
   python -X utf8 C:\v10pii\v10pii.py 샘플.docx
   ```
- 2026-11-05 에 위 구성(DLL 5개 + PHP 파일 1개 + v10pii.py)만 새 폴더에 복사해 돌려 같은 결과가 나오는 것을 확인했다.
  사이냅 라이선스 파일 등 다른 파일은 필요 없었다.
- ⚠ 엔진 DLL 과 암호화 정규식은 사내 자산이다. 외부 PC·외부 저장소로 내보내지 않는다.

### 7-5. 제대로 됐는지 확인
아래 내용으로 `t1.txt` 를 만들어 돌리면 6개 패턴이 1건씩 나와야 한다.
```
주민등록번호: 900101-1234568
연락처 010-1234-5678, 02-555-1234
메일 hong@example.co.kr
사업자 123-45-67891
법인 130111-0006246
```
기대값: `jumin·bsnno·incno·email·tel·cell` 각 1건, 이메일 값은 **`hong@example.co`** (서버 엔진의 알려진 `.kr` 잘림이 그대로 재현되면 같은 엔진이다).

### 7-6. 잘 안 될 때
| 증상 | 원인 / 조치 |
|---|---|
| `OSError: [WinError 193]` | 32비트 파이썬 → 64비트로 실행 |
| `OSError: [WinError 126] 모듈을 찾을 수 없습니다` | DLL 5개 중 빠진 것이 있거나 VC++ 2010 x64 런타임 없음. `V10PII_DLL_DIR` 경로 확인 |
| `RuntimeError: load_policy=30xx` | 패턴 원본이 깨졌거나 다른 판. `load_patterns()` 가 12개를 읽는지 확인 (3044·3048~3054 = 암호화 정규식 풀기 실패) |
| `RuntimeError: set_work_dir=3060` | 작업 폴더를 만들 수 없음 → `V10PII_WORK_DIR` 를 쓰기 가능한 곳으로 |
| `ret` 3024 / 3022 / 3025 | 모르는 형식 / 암호화 파일 / DRM 파일 — 엠파워 암호화 파일은 먼저 복호화해서 넣어야 함 |
| 파이썬이 그냥 죽음 | 문서 파싱 중 엔진 충돌(서버에서도 알려진 현상 — 서버는 fork 방식으로 격리). 문제 파일을 따로 빼고 진행 |

### 7-7. 한계
- DLL 빌드(2026-02-20, `SRC_V10.0B20170828`)와 운영 서버 `.so` 판이 완전히 같다는 보장은 없다. 이메일 잘림이 같은 것으로 보아 검출 규칙은 같은 계열.
- 패턴은 **설치 기본 12종**이다. 고객사가 바꾼 정책(MIN_LIMIT·예외값·C형 사용자 패턴)은 그 사이트 DB 의 `PI_PATTERN_INFO` 를 받아 와야 재현된다.
- 판정(`PI`/`PL`/`NO`)은 하지 않고 **건수와 값만** 낸다. 판정이 필요하면 3-3절 규칙(건수 ≥ MIN_LIMIT)을 그대로 적용하면 된다.
- DRM(소프트캠프·파수) 처리, 압축 안 DRM 재귀 검사는 넣지 않았다(`mpiisl_scan_file_w` 만 씀). 일반 압축은 엔진이 깊이 3까지 푼다.

---

## 8. 소스 위치 모음 (V10)

| 무엇 | 경로 |
|---|---|
| 검출 핵심 클래스 | `www/classes/class.MShaPI.php` (initialize 114, scanFile 387, getPIDetailResult 2112, 기본 패턴 2230) |
| 업로드 배치 검출 | `MpowerScript/classes/class.ShaFileBase.php` `scanPersonalInfoBase()` (571~) |
| 재검출 배치 | `MpowerScript/pi_batch/pi.rescan.php`, `pi.rescan.sh` |
| 요약 배치 | `MpowerScript/pi_batch/pi.summary.php`, `MpowerScript/classes/class.ShaPISummary.php` |
| 상태·제한 설정 | `www/common/global_val.php:646-716`, `www/site_props/_global_val_comm.php:556-574` |
| 관리자 화면 | `www/admin/personal_information/` |
| 접속기 API | `www/cs/sha/reqShaManagePiFileCns.php` |
| 기본 데이터 | `www/admin/init_set_data/db_factory_data_check.php:241-264` |
| 테이블 정의 | `DBScript/Mpower10U_MYSQL_20190523.sql:3842-4290` |
| 이력 | `release_note.txt` (PI 재검출 6153~, 다운로드 제한 4419~, fork 방식 711~) |
