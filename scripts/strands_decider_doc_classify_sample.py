# -*- coding: utf-8 -*-
"""Strands Decider 2B(오픈소스 판정 모델)로 문서를 분류해 보는 테스트 샘플.

이 모델은 글을 생성하지 않고, 우리가 준 '보기' 중 하나를 고르거나(choice),
순서 있는 등급을 매기거나(score), 예/아니오 확률(noul)을 돌려준다.
문서 1건(state)에 질문 3개를 한 번에 던져 문서유형·보안등급·개인정보 여부를 받는다.

주의: strands-decider 는 transformers>=5.15 를 요구해 이 프로젝트의 개발 환경
(transformers 4.x)과 충돌한다. 반드시 별도 가상환경에서 실행한다.

    python -m venv .venv-strands
    .venv-strands\\Scripts\\pip install strands-decider
    .venv-strands\\Scripts\\python scripts\\strands_decider_doc_classify_sample.py

    # 내 문서(.txt/.md) 폴더로 돌리기
    .venv-strands\\Scripts\\python scripts\\strands_decider_doc_classify_sample.py --dir D:\\some\\texts
"""
import argparse
import json
import sys
import time
from pathlib import Path

# 사용자가 지정한 기준 모델(Hugging Face 저장소 id). 첫 실행 때 가중치를 내려받는다.
DEFAULT_MODEL = "StrandsAgents/strands-decider-2B-hobson-v19"

# 문서유형 보기: {보기 이름: 설명}. 설명이 구체적일수록 모델이 경계를 잘 가른다.
DOC_TYPES = {
    "인사": "채용, 급여, 인사평가, 근태, 재직·경력 증명 등 직원에 관한 문서",
    "재무": "예산, 결산, 세금계산서, 견적, 지출결의, 회계 장부 등 돈에 관한 문서",
    "계약": "계약서, 약정서, 합의서, 비밀유지서약(NDA) 등 당사자 간 권리·의무를 정한 문서",
    "기술": "설계서, 소스코드 설명, 시험성적서, 매뉴얼 등 제품·시스템의 기술 문서",
    "영업": "제안서, 고객 목록, 영업 실적, 마케팅 계획 등 판매·고객에 관한 문서",
    "일반": "공지, 안내문, 보도자료, 회의 일정 등 위 어디에도 속하지 않는 일반 문서",
}

# 보안등급 보기: 반드시 '낮은 등급 → 높은 등급' 순서(score 는 오름차순 척도를 받는다).
CSO_LEVELS = [
    "O(공개): 외부에 공개돼도 문제없는 내용",
    "S(민감): 내부 직원만 봐야 하는 내용, 일반 개인정보나 내부 업무 자료",
    "C(기밀): 유출 시 큰 피해가 나는 내용, 주민등록번호·건강정보·영업비밀·대외비",
]
CSO_CODES = ["O", "S", "C"]

# --dir 을 주지 않았을 때 쓰는 내장 예시 문서(전부 지어낸 내용, 실제 개인정보 아님).
SAMPLE_DOCS = [
    ("급여명세서.txt",
     "2026년 9월 급여명세서\n성명: 홍길동  사번: A-00000\n주민등록번호: 000000-0000000\n"
     "기본급 3,200,000원  식대 200,000원  소득세 84,000원  실지급액 2,950,000원"),
    ("NDA.txt",
     "비밀유지계약서 (대외비)\n갑 주식회사 가나다와 을 주식회사 라마바는 신제품 공동개발과 관련하여 "
     "상호 제공하는 기술자료 및 영업비밀을 제3자에게 누설하지 아니한다. 계약기간은 체결일로부터 3년으로 한다."),
    ("휴무안내.txt",
     "[공지] 추석 연휴 고객센터 휴무 안내\n10월 3일부터 10월 5일까지 고객센터 전화 상담을 쉽니다. "
     "홈페이지 1:1 문의는 정상 접수되며 연휴 이후 순서대로 답변드립니다."),
    ("API설계서.txt",
     "문서 분류 서버 API 설계서 v1.2 (내부용)\nPOST /classify 요청 본문에 파일 경로 목록을 담아 보내면 "
     "각 파일의 등급을 JSON 배열로 응답한다. 타임아웃은 30초이며 실패 시 오류코드 E1001 을 돌려준다."),
    ("3분기_견적.txt",
     "견적서\n수신: 주식회사 라마바 구매팀\n품목: 문서보안 솔루션 라이선스 100 User\n"
     "단가 150,000원  공급가액 15,000,000원  부가세 1,500,000원  합계 16,500,000원\n유효기간: 견적일로부터 30일"),
]


#------------------------------------------------------------------
# 질문 3개 만들기 (문서유형·보안등급·개인정보 여부)
#=> 모델에게 던질 질문 묶음을 만든다. 문서 1건에 질문을 여러 개 묶어 보내면
#   문서를 한 번만 읽고 답하므로, 질문마다 따로 부르는 것보다 훨씬 빠르다.
#    1) doc_type  = choice(보기 중 하나 고르기)
#    2) cso_grade = score(낮음→높음 순서 척도에서 등급 매기기)
#    3) has_pii   = noul(예/아니오, '예'일 확률)
#
# -in: 없음
#
# -out: questions = {질문이름: 질문객체} 딕셔너리 (engine.ask 에 그대로 넘긴다)
# -out: error = 보기 개수가 규격 밖이면 pydantic ValidationError (choice 2~255개, score 2~10단계)
#------------------------------------------------------------------
def build_questions():
    # 무거운 패키지라 --help 만 볼 때는 불러오지 않도록 함수 안에서 import
    from strands_decider.schema import ChoiceQuestion, NoulQuestion, ScoreQuestion

    return {
        "doc_type": ChoiceQuestion(
            instructions="이 문서는 어떤 업무 유형의 문서인가?",
            criteria=DOC_TYPES,
        ),
        "cso_grade": ScoreQuestion(
            instructions="이 문서의 보안등급은 어느 수준인가?",
            criteria=CSO_LEVELS,
        ),
        "has_pii": NoulQuestion(
            instructions="이 문서에 특정 개인을 알아볼 수 있는 개인정보가 들어 있는가?",
            criteria={
                "true": "이름과 함께 주민등록번호, 전화번호, 주소, 계좌번호, 급여 등이 적혀 있다",
                "false": "회사명·부서명·제품명만 있고 개인을 특정할 정보가 없다",
            },
        ),
    }


#------------------------------------------------------------------
# 분류할 문서 목록 읽기
#=> 폴더를 주면 그 안의 .txt/.md 를 읽고, 안 주면 내장 예시 문서를 쓴다.
#   (hwp·pdf 같은 원본은 이 샘플 범위 밖 — 먼저 텍스트로 뽑아서 넣는다.)
#    1) 폴더가 없으면 내장 예시 반환
#    2) 있으면 하위 폴더까지 .txt/.md 를 이름순으로 읽기
#
# -in: doc_dir = 텍스트 파일 폴더 경로 (None 이면 내장 예시 사용)
#
# -out: docs = [(파일이름, 본문)] 리스트
# -out: error = 폴더가 없으면 FileNotFoundError. 깨진 글자는 무시하고 읽는다(예외 없음)
#------------------------------------------------------------------
def load_docs(doc_dir):
    if not doc_dir:
        return SAMPLE_DOCS
    root = Path(doc_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"폴더가 없습니다: {root}")
    docs = []
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() in (".txt", ".md"):
            # 인코딩이 섞여 있어도 멈추지 않도록 깨진 바이트는 버린다
            docs.append((str(path.relative_to(root)), path.read_text(encoding="utf-8", errors="ignore")))
    return docs


#------------------------------------------------------------------
# 실행 장치 자동 선택
#=> GPU 가 있으면 GPU, 없으면 CPU 를 고른다.
#   CPU 는 문서 1건에 수 초가 걸리고 메모리를 7GB 가량 쓴다(모델을 fp32 로 올리기 때문).
#
# -in: 없음
#
# -out: device = "cuda" | "mps" | "cpu"
# -out: error = 없음 (항상 문자열 반환)
#------------------------------------------------------------------
def pick_device():
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


#------------------------------------------------------------------
# 문서 1건 분류
#=> 문서 본문을 state 로, 질문 3개를 한 번에 던져 답을 받아 보기 쉬운 dict 로 정리한다.
#    1) engine.ask(본문, 질문들) 호출 — 본문이 길면 모델이 앞부분(약 3천 토큰)만 본다
#    2) choice → 고른 유형과 확신도
#    3) score  → 기대값(0~2)을 반올림해 O/S/C 로 변환
#    4) noul   → '예'일 확률(0~1)
#
# -in: engine    = load_engine 으로 올린 판정 엔진
# -in: questions = build_questions 가 만든 질문 딕셔너리
# -in: text      = 문서 본문
#
# -out: result = {doc_type, doc_type_conf, grade, grade_score, grade_conf, pii_prob, tokens, sec}
# -out: error = 본문이 비어 있으면 ValueError (호출 쪽에서 빈 문서는 미리 건너뛴다)
#------------------------------------------------------------------
def classify_one(engine, questions, text):
    started = time.perf_counter()
    response = engine.ask(text, questions)
    elapsed = time.perf_counter() - started

    doc_type = response.answers["doc_type"]
    grade = response.answers["cso_grade"]
    pii = response.answers["has_pii"]
    # score 는 등급 번호의 기대값(예: 1.07)이라 가장 가까운 정수 등급으로 바꾼다
    grade_idx = min(len(CSO_CODES) - 1, max(0, round(grade.score)))
    return {
        "doc_type": doc_type.choice,
        "doc_type_conf": doc_type.confidence,
        "grade": CSO_CODES[grade_idx],
        "grade_score": grade.score,
        "grade_conf": grade.confidence,
        "pii_prob": pii.noul,
        "tokens": response.usage.input_tokens,
        "sec": round(elapsed, 2),
    }


#------------------------------------------------------------------
# 샘플 실행 진입점
#=> 모델을 한 번 올리고 문서를 차례로 분류해 표(또는 JSON)로 출력한다.
#    1) 인자 해석 → 문서 목록 읽기
#    2) 모델 로드(첫 실행은 가중치 다운로드로 오래 걸림)
#    3) 문서마다 classify_one 호출
#    4) 확신도가 기준보다 낮으면 '검토' 표시(사람이 다시 볼 대상)
#
# -in: 없음 (명령줄 인자 --model / --device / --dir / --min-conf / --json 사용)
#
# -out: exit_code = 0 정상, 1 분류할 문서 없음
# -out: error = 모델 로드·다운로드 실패, 폴더 없음 등은 예외를 그대로 전파
#------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Strands Decider 2B 문서 분류 샘플")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="HF 저장소 id 또는 로컬 체크포인트 경로")
    parser.add_argument("--device", default=None, help="cuda | mps | cpu (생략 시 자동)")
    parser.add_argument("--dir", default=None, help=".txt/.md 문서 폴더 (생략 시 내장 예시 5건)")
    parser.add_argument("--min-conf", type=float, default=0.9, help="이 값 미만이면 '검토' 표시 (기본 0.9)")
    parser.add_argument("--json", action="store_true", help="표 대신 JSON 배열로 출력")
    args = parser.parse_args()

    # 빈 문서는 모델이 거부(ValueError)하므로 미리 걸러낸다
    docs = [(name, text) for name, text in load_docs(args.dir) if text.strip()]
    if not docs:
        print("분류할 문서가 없습니다.", file=sys.stderr)
        return 1

    from strands_decider.infer import load_engine

    device = args.device or pick_device()
    print(f"모델 로드 중: {args.model} (device={device})", file=sys.stderr)
    engine = load_engine(args.model, device=device)
    questions = build_questions()

    results = []
    for name, text in docs:
        row = {"file": name, **classify_one(engine, questions, text)}
        # 유형·등급 둘 중 하나라도 확신이 낮으면 사람이 다시 볼 대상으로 표시
        row["review"] = min(row["doc_type_conf"], row["grade_conf"]) < args.min_conf
        results.append(row)
        if not args.json:
            flag = "검토" if row["review"] else "    "
            print(f"{flag} {row['grade']}({row['grade_conf']:.2f}) "
                  f"{row['doc_type']}({row['doc_type_conf']:.2f}) "
                  f"PII={row['pii_prob']:.2f} {row['sec']:>6.2f}s  {name}")

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
