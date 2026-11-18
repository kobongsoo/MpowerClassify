/*
 * moaipii.h — 한국어 개인정보(PII) 검출 라이브러리의 C 진입점
 *   Windows: moaipii.dll   (링크형으로 쓰려면 moaipii.dll.lib)
 *   Linux  : libmoaipii.so (외부 .so 의존 없음)
 *
 * 규칙
 *   - 본문은 (const uint8_t* text, size_t len) 으로 넘긴다. 널 종료 문자열이 아니다
 *     (본문 속 \0 에서 잘리지 않게). len 이 0 이면 text 는 NULL 이어도 된다.
 *   - 옵션은 널 종료 UTF-8 JSON 이다.
 *   - char** out 으로 받은 문자열은 성공이든 실패든 반드시 moaipii_free 로 돌려준다.
 *     C 의 free() 로 지우면 안 된다(할당기가 다르다). moaipii_free 는 0 으로 덮고 지운다.
 *   - const char* 로 받는 것(버전)은 고정 문자열이라 해제하지 않는다.
 *   - 성공·실패는 반환 코드로 가른다. 자세한 사유는 결과 JSON 의 "error" 에 있다.
 *   - 전역 상태가 없어 여러 스레드에서 동시에 불러도 된다. 라이브러리는 로그를 남기지 않는다.
 *   - 함수 모양이 바뀌면 MOAIPII_ABI_VERSION 이 오른다. 불러온 직후
 *     moaipii_abi_version() == MOAIPII_ABI_VERSION 인지 확인한다.
 *
 * 검사 옵션 JSON (moaipii_scan) — 빠진 키는 기본값, 모르는 키는 무시
 *   { "labels": ["RRN","PHONE", ...],     // 필수. 켤 라벨(별칭 PRESCRIPTION 허용)
 *     "items": true,                      // false = 건수만(위치 목록 생략)
 *     "values": false,                    // true = 위치마다 원문 값도 싣는다
 *     "offsets": "char",                  // "char"(코드포인트) | "utf16" | "byte"(넘긴 바이트)
 *     "invalid_utf8": "error",            // "error" | "replace"(깨진 바이트 묶음을 U+FFFD 한 글자로)
 *     "unknown_labels": "error",          // "error" | "ignore"(결과 "unknown" 에 알려 줌)
 *     "today": null }                     // "YYYY-MM-DD" — 미래 생년월일 판단 기준일(null = 시스템 날짜)
 *
 * 검사 결과 JSON
 *   성공: { "chars": 48213,                                   // 본문 코드포인트 수
 *           "counts": { "PHONE": 7, "BUSINESS_REG": 1 },      // 0 건 라벨은 없다
 *           "items":  [ { "label": "BUSINESS_REG", "start": 13, "end": 25 }, ... ],
 *           "unknown": ["FAX"] }                              // unknown_labels:"ignore" 이고 있을 때만
 *         items 는 라벨 간 겹침 해소가 끝난 목록이고 문서 순서다. 좌표는 [start, end).
 *         invalid_utf8:"replace" + offsets:"byte" 이면 좌표는 고치기 전 '넘긴 바이트'를 가리킨다.
 *   실패: { "error": { "kind": "arg"|"utf8"|"panic", "message": "..." } }
 */
#ifndef MOAIPII_H
#define MOAIPII_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define MOAIPII_ABI_VERSION 1

typedef enum {
    MP_OK        = 0,   /* 성공 */
    MP_ERR_ARG   = 1,   /* 인자·옵션이 틀림(NULL, JSON 모양, 모르는 라벨) */
    MP_ERR_UTF8  = 2,   /* 본문이 UTF-8 이 아님(invalid_utf8:"error") */
    MP_ERR_PANIC = 99   /* 라이브러리 내부 오류(경계에서 잡음) */
} mp_status;

uint32_t    moaipii_abi_version(void);
const char* moaipii_version(void);       /* "0.1.0+abc1234" */
const char* moaipii_kopii_basis(void);   /* "1.15.2" — 검출 기준이 된 ko-pii 판 */
int         moaipii_labels(char** out_json);
int         moaipii_scan(const uint8_t* text, size_t len, const char* opts_json, char** out_json);
void        moaipii_free(char* p);

#ifdef __cplusplus
}
#endif

#endif /* MOAIPII_H */
