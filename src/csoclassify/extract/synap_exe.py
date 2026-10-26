#------------------------------------------------------------------
# 사이냅 snf_exe.exe subprocess 추출기 (설계서 결정1 / §9-1)
#=> 사이냅 문서필터의 독립 실행파일을 프로세스로 호출해 문서에서 텍스트를 뽑는다.
#   snf_exe 는 종료코드가 성공/실패 모두 0 이라 믿을 수 없으므로, "출력파일이
#   생기고 비어있지 않은가"로 성공을 판별한다(설계서 §9-1의 오류판별 규칙).
#------------------------------------------------------------------

import os
import subprocess
import uuid

from .. import config
from .. import resources
from .base import TextExtractor, ExtractError, check_size_limit


class SynapExeExtractor(TextExtractor):
    #------------------------------------------------------------------
    # 생성자 — 추출기 준비
    #=> 사용할 snf_exe 경로, 작업(임시)폴더, 타임아웃, 페이지마커 제거 여부를 잡는다.
    #    1) exe 경로 미지정 시 resources 에서 자동 탐색
    #    2) 작업폴더 미지정 시 OS 임시폴더 사용
    #
    # -in: exe_path     = snf_exe.exe 경로(None 이면 자동)
    # -in: work_dir     = 임시 출력파일을 둘 폴더(None 이면 OS temp)
    # -in: timeout      = 1파일 처리 제한시간(초, 기본 config.SNF_TIMEOUT)
    # -in: no_withpage  = True 면 '-NO_WITHPAGE' 로 페이지 마커 미삽입
    #
    # -out: 없음
    # -out: error = snf_exe 가 실제로 없으면 ExtractError
    #------------------------------------------------------------------
    def __init__(self, exe_path=None, work_dir=None, timeout=None, no_withpage=True):
        # exe 경로 확정: 명시값 우선, 없으면 번들/소스 리소스에서 찾는다.
        self.exe_path = exe_path or resources.synap_exe_path()
        # 실행 전에 존재를 확인해, 실패를 늦지 않게 앞단에서 분명히 알린다.
        if not os.path.isfile(self.exe_path):
            raise ExtractError(f"snf_exe 를 찾을 수 없음: {self.exe_path}")
        # 작업폴더: 임시 출력파일을 프로그램이 관리하는 위치에 만든다(경로 예측 가능).
        import tempfile
        self.work_dir = work_dir or tempfile.gettempdir()
        self.timeout = timeout or config.SNF_TIMEOUT
        self.no_withpage = no_withpage

    #------------------------------------------------------------------
    # 임시/보존 출력파일 경로 결정
    #=> 추출 결과 텍스트를 쓸 출력파일 경로와 "끝나고 지울지" 여부를 정한다.
    #    1) save_dir 있으면 그 폴더에 원본명 기반으로 남긴다(유지)
    #    2) 없으면 작업폴더에 유니크 이름으로 만들고 나중에 지운다
    #
    # -in: input_path = 원본 문서 경로(보존 시 파일명 근거)
    # -in: save_dir   = 보존 폴더(None 이면 임시)
    #
    # -out: (out_path, keep) = 출력파일 경로, 유지여부(True=보존)
    # -out: error = 없음
    #------------------------------------------------------------------
    def _decide_output(self, input_path, save_dir):
        if save_dir:
            # 보존: 원본 파일명에 .txt 를 붙이고 충돌 시 __N 으로 회피한다.
            os.makedirs(save_dir, exist_ok=True)
            stem = os.path.splitext(os.path.basename(input_path))[0]
            candidate = os.path.join(save_dir, stem + ".txt")
            n = 2
            while os.path.exists(candidate):
                candidate = os.path.join(save_dir, f"{stem}__{n}.txt")
                n += 1
            return candidate, True
        # 임시: 충돌 없는 uuid 이름으로 만들고 처리 후 삭제 대상으로 표시.
        # 복호화로 풀린 문서(경로 위쪽에 cso_dec_<pid> 폴더)면 그 문서 폴더 안에 둔다 — 풀린
        # 문서의 본문이 복호화 임시 폴더 밖에 놓이지 않게(Rust 판 synap::out_dir_for 와 같다).
        work = self.work_dir
        parts = os.path.normpath(os.path.dirname(os.path.abspath(input_path))).split(os.sep)
        if any(p.startswith("cso_dec_") for p in parts):
            work = os.path.dirname(os.path.abspath(input_path))
        out_path = os.path.join(work, f"csoclassify_{uuid.uuid4().hex}.txt")
        return out_path, False

    #------------------------------------------------------------------
    # 문서 → 원시 텍스트 (핵심)
    #=> snf_exe 를 '-U8 [-NO_WITHPAGE] <입력> <출력>' 로 호출해 UTF-8 텍스트를 얻는다.
    #    1) 입력 존재 확인 → 출력경로 결정
    #    2) 리스트 인자로 subprocess 호출(경로 공백/한글 안전) + 타임아웃
    #    3) 출력파일 생성·비어있지 않음으로 성공 판별(종료코드는 신뢰 못함)
    #    4) 실패 시 snf_exe 진단(종료코드 rc + 출력파일 상태 + stdout/stderr)을 붙여 ExtractError
    #
    # -in: input_path = 추출할 문서 경로
    # -in: save_dir   = 추출 텍스트 보존 폴더(None 이면 임시파일 자동삭제)
    #
    # -out: text = 추출된 원시 텍스트(UTF-8)
    # -out: error = 입력없음/타임아웃/산출물없음 등에서 ExtractError
    #------------------------------------------------------------------
    def extract(self, input_path, save_dir=None):
        # (1) 입력 파일이 없으면 굳이 프로세스를 띄우지 않고 바로 실패시킨다.
        if not os.path.isfile(input_path):
            # 경로는 호출부(cli 의 file=<경로>)가 붙이므로 여기선 사유만 담는다(중복 방지).
            raise ExtractError("입력 파일 없음")

        # 크기 상한(G1·G2). --synap-only 로 하이브리드를 끄고 도는 경로가 상한을
        # 우회하면 안 되므로 여기서도 본다. 하이브리드의 폴백으로 불릴 때는 이미
        # 통과한 파일이라 곧바로 통과한다(getsize 한 번이라 비용이 없다).
        check_size_limit(input_path)

        out_path, keep = self._decide_output(input_path, save_dir)

        # (2) 인자 구성: -U8(UTF-8), 필요시 -NO_WITHPAGE(페이지마커 제거)
        args = [self.exe_path, "-U8"]
        if self.no_withpage:
            args.append("-NO_WITHPAGE")
        args += [input_path, out_path]

        try:
            # 리스트로 넘겨 셸 파싱을 피하고, 표준출력을 캡처해 실패 진단에 쓴다.
            proc = subprocess.run(
                args,
                capture_output=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            # 타임아웃 시 남은 부분 출력물이 있으면 지워서 오탐을 막는다.
            self._safe_remove(out_path, keep)
            # 경로는 호출부가 붙인다 → 여기선 사유(타임아웃)만.
            raise ExtractError(f"snf_exe 타임아웃({self.timeout}s)")

        # (3) 성공 판별: 출력파일이 실제로 생겼고 크기가 0 이 아니어야 한다.
        if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
            # 실패 원인을 로그에서 바로 알 수 있게 snf_exe 의 '에러값'을 최대한 모은다.
            # snf_exe 는 오류를 stdout 으로 뱉을 때가 많고, 아무 말 없이(빈 출력) 실패
            # 하는 경우도 있어 — 그럴 때도 최소한 '종료코드'와 '출력파일 상태'는 남는다.
            out = self._decode(proc.stdout).strip()
            err = self._decode(proc.stderr).strip()
            # 출력파일이 아예 안 생겼는지, 생겼지만 0바이트인지 구분해 원인 좁히기에 쓴다.
            state = "출력파일 미생성" if not os.path.isfile(out_path) else "출력파일 0바이트"
            # 종료코드는 snf_exe 가 조용히 실패해도 남는 핵심 신호라 항상 포함한다.
            detail = f"rc={proc.returncode}; {state}"
            # stdout/stderr 는 있을 때만(둘 다 비어도 위 rc·state 로 진단 가능).
            if out:
                detail += f"; stdout={out[:300]}"
            if err:
                detail += f"; stderr={err[:300]}"
            # 0바이트 잔여 임시파일은 지워 디스크 누수·다음 실행 오탐을 막는다(보존 모드는 유지).
            self._safe_remove(out_path, keep)
            # 경로는 호출부(cli 의 file=<경로>)가 붙이므로 여기선 사유(rc/상태/출력)만 담는다.
            raise ExtractError(detail)

        # (4) 결과 읽기: -U8 은 BOM 없는 UTF-8 → 그대로 디코드. 깨진 바이트는 대체.
        try:
            with open(out_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        finally:
            # 보존 옵션이 아니면 임시 출력파일을 반드시 정리(디스크 누수 방지).
            self._safe_remove(out_path, keep)

        return text

    #------------------------------------------------------------------
    # 임시 출력파일 안전 삭제
    #=> 보존(keep=True)이면 남기고, 아니면 조용히 지운다. 이미 없거나 잠겨도
    #   예외로 흐름을 끊지 않는다.
    #
    # -in: path = 삭제 후보 경로
    # -in: keep = True 면 삭제하지 않음
    #
    # -out: 없음
    # -out: error = 없음 (삭제 실패는 무시)
    #------------------------------------------------------------------
    def _safe_remove(self, path, keep):
        if keep:
            return
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            # 정리 실패는 치명적이지 않으므로 무시(다음 실행/OS 가 정리).
            pass

    #------------------------------------------------------------------
    # 프로세스 출력 바이트 → 문자열
    #=> snf_exe 진단 메시지를 사람이 읽게 디코드한다. 인코딩이 확실치 않아
    #   utf-8 우선, 실패하면 cp949(윈도우 한글)로 재시도한다.
    #
    # -in: raw = subprocess 가 준 bytes(또는 None)
    #
    # -out: text = 디코드된 문자열(없으면 빈 문자열)
    # -out: error = 없음
    #------------------------------------------------------------------
    def _decode(self, raw):
        if not raw:
            return ""
        for enc in ("utf-8", "cp949"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        # 어떤 인코딩도 안 되면 손실을 감수하고 대체문자로 디코드한다.
        return raw.decode("utf-8", errors="replace")
