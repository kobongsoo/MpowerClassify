# CHANGELOG

판마다 **결과 바뀜** 여부를 적는다. "결과 바뀜: 예" 인 판을 받으면 쓰는 쪽(MpowerClassify 등)은
평가셋을 다시 돌린다.

## 미출시

- 결과 바뀜: **아니오** (검출 라이브러리는 그대로)
- 배포 시험 도구 `moaipii-test(.exe)` 추가 — 묶음의 DLL·.so 를 실행 중에 불러 `info`·`labels`·`demo`·`selftest`·
  `scan`·`mask`·`bench`. Windows zip · Linux tgz 의 `lib/` 에 함께 들어간다.
- 묶음에 `doc/moaipii-test-guide.html`(시험 도구 사용설명서) 추가, 연동 설명서 보강(언어별 예제·검출 항목 상세)

## 0.1.0 — 2026-11-17

첫 판. MpowerClassify `Rust/src/pii.rs`(+ `pii_fold.rs`·`districts*.rs`)를 떼어 냈다.

- 결과 바뀜: **아니오** (MpowerClassify Rust 판과 같은 검출기 — 옮기기 전 단위 시험 23개·골든 시험 그대로 통과)
- 바뀐 동작 하나: "오늘 날짜"(미래 생년월일 판단)를 프로세스 시작 때 한 번이 아니라 호출마다 계산한다.
  같은 날에는 결과가 같다. 옵션 `today` 로 고정할 수 있다.
- C 진입점 6개(ABI 1): `moaipii_abi_version` · `moaipii_version` · `moaipii_kopii_basis` · `moaipii_labels` ·
  `moaipii_scan` · `moaipii_free`
- 감싸개: Python · Rust · Java(JNA) · PHP(FFI) · C++(헤더 하나)
