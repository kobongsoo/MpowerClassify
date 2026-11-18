#------------------------------------------------------------------
# 상주 데몬 서버 (설계서 결정5)
#=> 모델을 한 번 올려 메모리에 상주시키고, 클라이언트의 embed 요청을 받아 벡터를
#   돌려준다. 같은 모델 데몬이 둘 뜨지 않도록 싱글턴 잠금을 먼저 잡고, 일정 시간
#   요청이 없으면(유휴) 스스로 종료해 메모리를 회수한다.
#------------------------------------------------------------------

import os
import time

from .. import __version__
from .. import config
from ..extract import build_extractor
from ..embed.onnx_embedder import OnnxEmbedder
from ..logsetup import get_logger
from ..timing import Timing
from .. import pipeline
from . import ipc
from . import registry

log = get_logger(__name__)


class DaemonServer:
    #------------------------------------------------------------------
    # 생성자 — 데몬 파라미터 보관
    #=> 어떤 모델을, 얼마의 유휴시간으로 상주시킬지 기억한다. 실제 준비는 run()에서.
    #
    # -in: spec         = config.ModelSpec (상주시킬 모델)
    # -in: idle_timeout = 유휴 자동종료(초, 기본 config.DEFAULT_IDLE_TIMEOUT)
    # -in: num_threads  = onnxruntime 스레드 수(None 기본)
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def __init__(self, spec, idle_timeout=None, num_threads=None):
        self.spec = spec
        # 0/None = 무한(유휴 자동종료 없음). `or` 를 쓰면 명시적 0 이 기본값으로 바뀌므로 is None 으로 판별.
        self.idle_timeout = config.DEFAULT_IDLE_TIMEOUT if idle_timeout is None else idle_timeout
        self.num_threads = num_threads
        self._token = None
        # 추출기는 요청이 고른 설정(하이브리드 여부 · 자체 파서 py/rust)마다 하나씩 만들어 재사용한다.
        self._extractors = {}
        self._embedder = None
        self._stop = False

    #------------------------------------------------------------------
    # 데몬 실행 (핵심)
    #=> 싱글턴 잠금 → 모델 로드 → 소켓 리슨 → 레지스트리 기록 → 요청 루프.
    #    1) acquire_singleton 실패 시 "다른 데몬이 이미 있음"으로 보고 조용히 종료
    #    2) 임베더 준비 후 모델을 실제로 로드(데몬의 존재 이유). 추출기는 요청별로 나중에
    #       만든다(_extractor_for — 클라이언트의 --hybridparse · --extractor 를 따른다)
    #    3) loopback 리슨 소켓 + 랜덤 토큰으로 레지스트리 기록
    #    4) accept 타임아웃을 유휴시간으로 두고, 그 안에 요청이 없으면 종료
    #
    # -in: 없음
    #
    # -out: int = 종료코드(0 정상, 그 외 이상). "이미 데몬 있음"도 0 으로 조용히 종료
    # -out: error = 치명적 준비 실패 시 예외를 잡아 stderr 로 남기고 비정상 코드 반환
    #------------------------------------------------------------------
    def run(self):
        model = self.spec.key
        # 유휴시간이 0/None 이면 '무한'(자동종료 없음). 로그·소켓 타임아웃 처리에 함께 쓴다.
        infinite = not self.idle_timeout or self.idle_timeout <= 0
        idle_disp = "무한(자동종료 없음)" if infinite else f"{self.idle_timeout}s"
        # (1) 나만 데몬이 되도록 원자적 잠금. 못 얻으면 다른 데몬이 주인 → 조용히 종료.
        if not registry.acquire_singleton(model):
            log.info("데몬 기동 취소: 이미 %s 데몬이 존재", model)
            return 0

        srv = None
        try:
            log.info("데몬 시작 model=%s pid=%d idle_timeout=%s", model, os.getpid(), idle_disp)
            # (2) 임베더를 준비하고 모델을 실제로 메모리에 올린다.
            #     추출기는 여기서 만들지 않는다 — 요청마다 클라이언트가 고른 설정
            #     (--hybridparse · --extractor)을 따라야 하므로 처음 쓰일 때 만든다(_extractor_for).
            #     예전에는 여기서 사이냅 단독으로 고정해, 사이냅이 없는 PC 에서는 데몬이 아예 못 떴다.
            self._embedder = OnnxEmbedder(self.spec, num_threads=self.num_threads)
            self._embedder.ensure_loaded()

            # (3) loopback 리슨 소켓 + 접속 인증용 랜덤 토큰 생성.
            srv, port = ipc.make_server(config.DAEMON_HOST)
            self._token = os.urandom(16).hex()
            registry.write_record(model, {
                "pid": os.getpid(),
                "host": config.DAEMON_HOST,
                "port": port,
                "token": self._token,
                "version": __version__,
                "model": model,
                "idle_timeout": self.idle_timeout,
                "started": time.time(),
            })

            log.info("모델 상주 준비 완료 port=%d — 요청 대기(유휴 %s)", port,
                     "무한 = 자동종료 없음" if infinite else f"{self.idle_timeout}s 후 자동 종료")

            # (4) 요청 루프: 유휴시간 안에 연결이 없으면 종료. 무한이면 settimeout(None) 으로
            #     영원히 대기(자동종료 없음). --stop 은 실제 연결을 보내 accept 를 깨우므로 무한이어도 동작.
            srv.settimeout(None if infinite else self.idle_timeout)
            while not self._stop:
                try:
                    conn, _addr = srv.accept()
                except OSError:
                    # 유한 모드의 타임아웃(유휴 초과)이면 정상 자동종료. 무한 모드의 그 외 소켓오류도 종료.
                    if not infinite:
                        log.info("유휴 %ds 초과 → 데몬 종료", self.idle_timeout)
                    break
                # 연결 하나를 처리(요청→응답). 처리 중 예외는 이 안에서 흡수.
                self._handle(conn)
            if self._stop:
                log.info("정지 요청 수신 → 데몬 종료")
            return 0
        except Exception as e:
            # 준비 단계 실패는 드러나야 하므로 stderr + 로그(스택)로 남긴다.
            import sys
            print(f"[csoclassify:daemon] 실패: {e}", file=sys.stderr)
            log.exception("데몬 준비/실행 실패: %s", e)
            return 1
        finally:
            # 소켓 닫고 레지스트리/잠금 정리(다음 기동이 stale 로 오인하지 않게).
            if srv is not None:
                try:
                    srv.close()
                except OSError:
                    pass
            registry.clear(model)

    #------------------------------------------------------------------
    # 연결 1개 처리
    #=> 요청 메시지를 받아 op 에 따라 응답한다. 통신 오류는 연결만 끊고 서버는 유지.
    #    - ping : 인증 없이 헬스/핸드셰이크 정보 반환
    #    - embed: 토큰 확인 후 파이프라인 실행, 결과/에러 반환
    #    - stop : 토큰 확인 후 종료 예약
    #
    # -in: conn = accept 로 얻은 연결 소켓
    #
    # -out: 없음(응답은 소켓으로 전송)
    # -out: error = 없음(통신/처리 예외를 내부에서 흡수)
    #------------------------------------------------------------------
    def _handle(self, conn):
        try:
            req = ipc.recv_msg(conn)
            op = req.get("op")

            if op == "ping":
                # 헬스체크는 인증 없이 응답(클라이언트가 살아있음/모델준비를 판단).
                ipc.send_msg(conn, {
                    "ok": True,
                    "model_ready": self._embedder.is_ready(),
                    "model": self.spec.key,
                    "version": __version__,
                    "pid": os.getpid(),
                })
            elif op == "embed":
                # 임베딩은 토큰 일치해야 수행(로컬 타 사용자 차단).
                if req.get("token") != self._token:
                    ipc.send_msg(conn, {"ok": False, "error": "인증 실패", "kind": "auth"})
                else:
                    self._do_embed(conn, req)
            elif op == "embed_text":
                # 분류 경로용: 이미 추출·정제된 '텍스트'를 받아 벡터만 돌려준다(재추출 없음).
                if req.get("token") != self._token:
                    ipc.send_msg(conn, {"ok": False, "error": "인증 실패", "kind": "auth"})
                else:
                    self._do_embed_text(conn, req)
            elif op == "stop":
                if req.get("token") != self._token:
                    ipc.send_msg(conn, {"ok": False, "error": "인증 실패", "kind": "auth"})
                else:
                    ipc.send_msg(conn, {"ok": True})
                    self._stop = True  # 루프 다음 회차에서 빠져나감
            else:
                ipc.send_msg(conn, {"ok": False, "error": f"알 수 없는 op: {op}", "kind": "bad_op"})
        except Exception:
            # 개별 연결 처리 실패가 데몬 전체를 죽이면 안 되므로 조용히 무시.
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    #------------------------------------------------------------------
    # embed 요청 처리
    #=> 요청의 경로/옵션으로 파이프라인을 돌려 결과를 응답한다. 모델은 이미 상주
    #   상태이므로 model_load 는 0 에 수렴한다.
    #
    # -in: conn = 연결 소켓
    # -in: req  = {op:"embed", token, path, opts} 요청 dict
    #
    # -out: 없음(결과/에러를 소켓으로 전송)
    # -out: error = 없음(추출/임베딩 예외를 잡아 kind 와 함께 응답)
    #------------------------------------------------------------------
    def _do_embed(self, conn, req):
        from ..extract.base import ExtractError
        from ..embed.base import EmbedError
        path = req.get("path")
        opts = req.get("opts", {})
        timing = Timing()
        try:
            # 클라이언트가 고른 추출 설정 그대로 — in-process 로 돌 때와 같은 파서로 읽게 한다.
            extractor = self._extractor_for(opts)
            # 데몬은 모델이 이미 로드돼 있어 병렬 예열이 불필요.
            result = pipeline.process_file(
                path, opts, extractor, self._embedder, timing, parallel_preload=False
            )
            ipc.send_msg(conn, {"ok": True, "result": result})
        except ExtractError as e:
            ipc.send_msg(conn, {"ok": False, "error": str(e), "kind": "extract"})
        except EmbedError as e:
            ipc.send_msg(conn, {"ok": False, "error": str(e), "kind": "embed"})
        except Exception as e:
            ipc.send_msg(conn, {"ok": False, "error": str(e), "kind": "other"})

    #------------------------------------------------------------------
    # 요청이 고른 추출기 꺼내기 (없으면 만들어 기억)
    #=> 데몬은 여러 클라이언트가 함께 쓰는 프로세스라, 추출 방식을 데몬 쪽에서 하나로
    #   정해 두면 클라이언트의 --hybridparse · --extractor 가 무시된다. 그래서 요청에 실린
    #   설정을 보고 그 조합의 추출기를 쓴다.
    #    1) opts["hybridparse"] — 없으면 False(옛 클라이언트는 사이냅 단독이던 예전 동작 그대로)
    #    2) opts["extractor"]   — "py" | "rust", 없으면 이 데몬의 기본(config.EXTRACTOR, 기본 rust)
    #    3) 같은 조합은 한 번 만든 것을 다시 쓴다(사이냅 탐색·라이브러리 로드를 매번 하지 않게)
    #
    # -in: opts = 요청의 처리 옵션 dict
    #
    # -out: extractor = TextExtractor 구현체
    # -out: error = 만들 수 없으면 ExtractError(사이냅 단독인데 snf 없음 · rust 인데 docparse 없음).
    #               실패는 기억하지 않는다 — 고쳐 놓은 뒤 다음 요청에서 다시 시도한다
    #------------------------------------------------------------------
    def _extractor_for(self, opts):
        hybrid = bool(opts.get("hybridparse", False))
        # 옛 클라이언트는 extractor 를 안 싣는다 — 이 데몬의 기본 파서(기본 rust)로 읽는다.
        which = str(opts.get("extractor") or config.EXTRACTOR or "rust").strip().lower()
        # 사이냅 단독이면 자체 파서를 안 쓰므로 py/rust 를 가르지 않는다(같은 추출기 하나).
        key = (hybrid, which if hybrid else "")
        ex = self._extractors.get(key)
        if ex is None:
            ex = build_extractor(hybrid=hybrid, extractor=which)
            self._extractors[key] = ex
            log.info("데몬 추출기 준비 hybrid=%s extractor=%s", hybrid, which if hybrid else "-")
        return ex

    #------------------------------------------------------------------
    # embed_text 요청 처리 (분류 경로 전용)
    #=> 이미 추출·정제된 '텍스트'를 받아 상주 모델로 임베딩해 '벡터'만 돌려준다.
    #   분류는 추출·규칙을 로컬에서 이미 수행하므로, 데몬이 재추출하지 않고 임베딩만 맡는다.
    #   모델이 상주 상태라 model_load 는 0 이다(반복 호출에도 웜 재사용).
    #
    # -in: conn = 연결 소켓
    # -in: req  = {op:"embed_text", token, text, opts} 요청 dict
    #             (opts: max_tokens/overlap/normalize — 없으면 기본값)
    #
    # -out: 없음(벡터/에러를 소켓으로 전송: {ok, vector, dim, chunks} 또는 {ok:false, ...})
    # -out: error = 없음(임베딩 예외를 잡아 kind 와 함께 응답)
    #------------------------------------------------------------------
    def _do_embed_text(self, conn, req):
        from ..embed.base import EmbedError
        text = req.get("text", "")
        opts = req.get("opts", {}) or {}
        try:
            result, n = self._embedder.embed_document(
                text,
                opts.get("max_tokens", config.DEFAULT_MAX_TOKENS),
                opts.get("overlap", config.DEFAULT_OVERLAP),
                normalize=opts.get("normalize", True),
                per_chunk=False,
            )
            ipc.send_msg(conn, {"ok": True, "vector": result, "dim": self.spec.dim, "chunks": n})
        except EmbedError as e:
            ipc.send_msg(conn, {"ok": False, "error": str(e), "kind": "embed"})
        except Exception as e:
            ipc.send_msg(conn, {"ok": False, "error": str(e), "kind": "other"})
