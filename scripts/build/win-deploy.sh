#!/usr/bin/env bash
#------------------------------------------------------------------
# 윈도우 배포본 만들기 — 두 판(Python·Rust)
#=> 빌드 → 배포 폴더 갈아 끼우기 → 두 판 교차 검증까지 한 번에 한다.
#   실패하면 그 자리에서 멈춘다(_lib.sh 참고).
#
#   [왜 스크립트로 두나] 이 절차를 즉석 셸로 치다가, 실패한 명령 다음 줄이
#   그대로 돌아 배포 폴더의 런타임이 사라진 일이 있다(2026-09-11).
#   절차를 파일로 남겨야 그 사고가 되풀이되지 않는다.
#
#   [모델은 건드리지 않는다] 임베딩 모델(models/)은 spec 이 안 넣어 주는
#   외장 자산이라 배포 폴더에 손으로 놓여 있다. 이 스크립트는 실행본체와
#   _internal 만 갈아 끼우므로 모델은 제자리에 남는다.
#
# 쓰는 법 (Git Bash, 프로젝트 최상위에서):
#   bash scripts/build/win-deploy.sh            # 두 판 다
#   bash scripts/build/win-deploy.sh python     # 파이썬만
#   bash scripts/build/win-deploy.sh rust       # Rust 만
#------------------------------------------------------------------
. "$(dirname "$0")/_lib.sh"

cd "$(dirname "$0")/../.." || die "프로젝트 최상위로 이동하지 못했습니다"
ROOT="$PWD"
WHICH="${1:-both}"
SAMPLE="${CSO_SAMPLE_DIR:-/d/sample}"

PY_DIST="dist-onedir/windows"
RS_DIST="Rust/dist-onedir/windows"

[ -f build/csoclassify.spec ] || die "build/csoclassify.spec 이 없습니다 (최상위에서 실행하세요)"


#------------------------------------------------------------------
# 파이썬 판 빌드 + 배포 폴더 갈아 끼우기
#=> PyInstaller 는 dist/MpowerClassify/ 에 실행본체와 _internal 을 만든다.
#   배포 폴더에는 그 둘만 갈아 끼운다(규칙셋·모델·사이냅은 그대로 둔다).
#
# -in: 없음(바깥 ROOT·PY_DIST 를 쓴다)
#
# -out: 없음
# -out: error = 어느 단계든 실패하면 종료
#------------------------------------------------------------------
build_python() {
    step "윈도우 · 파이썬 빌드"
    command -v python >/dev/null || die "python 을 찾을 수 없습니다"
    python -c "import PyInstaller" 2>/dev/null || die "PyInstaller 가 없습니다 (pip install pyinstaller)"

    # 빌드 산출물 폴더에 손으로 놓아 둔 것이 있으면 -y 가 지운다. 먼저 확인한다.
    if [ -d dist/MpowerClassify ]; then
        local stray
        stray="$(find dist/MpowerClassify -mindepth 1 -maxdepth 1 \
                 ! -name MpowerClassify.exe ! -name _internal | head -5)"
        [ -z "$stray" ] || die "dist/MpowerClassify 안에 빌드 산출물이 아닌 것이 있습니다:
$stray
       (모델을 여기 두면 -y 가 지웁니다. 옮긴 뒤 다시 실행하세요)"
    fi

    # 문서파서 라이브러리(Rust 문서파서, --extractor rust) — 설계: plan/Rust-문서파서-라이브러리분리-설계-20261110.html §6-2
    # 별도 프로젝트(DocParse-rust)가 만든 docparse.dll 을 third_party/docparse 에 받아 두고, spec 이 묶는다.
    local with_docparse=""
    if [ -f third_party/docparse/windows-x64/docparse.dll ]; then
        with_docparse=1
        echo "  docparse $(cat third_party/docparse/VERSION 2>/dev/null || echo '?')"
    else
        echo "  [경고] third_party/docparse/windows-x64/docparse.dll 이 없어 문서파서 없이 만듭니다 — 배포본에서 --extractor rust 를 쓸 수 없습니다"
    fi

    python -m PyInstaller -y build/csoclassify.spec >/dev/null 2>&1 \
        || die "PyInstaller 빌드 실패 — 'python -m PyInstaller build/csoclassify.spec' 로 로그를 보세요"
    [ -f dist/MpowerClassify/MpowerClassify.exe ] || die "빌드 결과에 실행본체가 없습니다"
    # 라이브러리를 넣었는데 배포본에 빠졌으면 멈춘다 — 빠진 채 나가면 --extractor rust 가 1017 로 멈춘다.
    if [ -n "$with_docparse" ]; then
        [ -f dist/MpowerClassify/_internal/docparse.dll ] \
            || die "배포본에 docparse.dll 이 없습니다 — build/csoclassify.spec 의 binaries 를 보세요"
    fi
    [ -d dist/MpowerClassify/_internal ] || die "빌드 결과에 _internal 이 없습니다"

    step "윈도우 · 파이썬 배포 폴더 갈아 끼우기"
    local ts; ts="$(date +%Y%m%d-%H%M)"
    if [ -f "$PY_DIST/MpowerClassify.exe" ]; then
        cp "$PY_DIST/MpowerClassify.exe" "$PY_DIST/MpowerClassify.exe.bak-$ts" \
            || die "실행본체 백업 실패"
    fi
    cp dist/MpowerClassify/MpowerClassify.exe "$PY_DIST/" || die "실행본체 복사 실패"

    rm -rf "$PY_DIST/_internal.new"
    cp -r dist/MpowerClassify/_internal "$PY_DIST/_internal.new" || die "_internal 복사 실패"
    local bak
    bak="$(swap_dir "$PY_DIST/_internal.new" "$PY_DIST/_internal")"

    # 갈아 끼운 것이 실제로 도는지 본 뒤에 옛 런타임을 지운다.
    ( cd "$PY_DIST" && ./MpowerClassify.exe --version >/dev/null 2>&1 ) \
        || die "갈아 끼운 배포본이 실행되지 않습니다 (옛 런타임은 $bak 에 있습니다)"
    [ -n "$bak" ] && rm -rf "$bak"
    rm -rf dist
    echo "  실행본체 $(ls -la "$PY_DIST/MpowerClassify.exe" | awk '{print $5}') B · _internal $(du -sh "$PY_DIST/_internal" | cut -f1)"
}


#------------------------------------------------------------------
# Rust 판 빌드 + 배포 폴더 갈아 끼우기
#=> 실행파일 하나뿐이라 폴더 갈아 끼우기가 없다. 대신 시험을 먼저 돌린다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 어느 단계든 실패하면 종료
#------------------------------------------------------------------
build_rust() {
    step "윈도우 · Rust 빌드"
    command -v cargo >/dev/null || die "cargo 를 찾을 수 없습니다"
    # 문서파서(docparse.dll)는 별도 프로젝트 산출물이다 — 받아 둔 것이 없으면 시험·실행 모두 1017 로 멈춘다.
    [ -f third_party/docparse/windows-x64/docparse.dll ] \
        || die "third_party/docparse/windows-x64/docparse.dll 이 없습니다 (scripts/build/update-docparse.sh 로 받으세요)"
    ( cd Rust && cargo test >/dev/null 2>&1 ) || die "cargo test 실패 — 'cd Rust && cargo test' 로 보세요"
    ( cd Rust && cargo build --release >/dev/null 2>&1 ) || die "cargo build --release 실패"
    [ -f Rust/target/release/MpowerClassify-rs.exe ] || die "빌드 결과에 실행파일이 없습니다"

    step "윈도우 · Rust 배포 폴더 갈아 끼우기"
    local ts; ts="$(date +%Y%m%d-%H%M)"
    if [ -f "$RS_DIST/MpowerClassify-rs.exe" ]; then
        cp "$RS_DIST/MpowerClassify-rs.exe" "$RS_DIST/MpowerClassify-rs.exe.bak-$ts" \
            || die "실행파일 백업 실패"
    fi
    cp Rust/target/release/MpowerClassify-rs.exe "$RS_DIST/" || die "실행파일 복사 실패"
    # 문서파서 라이브러리는 실행파일 옆에 둔다(없으면 문서를 읽기 전에 1017 로 멈춘다).
    cp third_party/docparse/windows-x64/docparse.dll "$RS_DIST/" || die "docparse.dll 복사 실패"
    ( cd "$RS_DIST" && ./MpowerClassify-rs.exe --version >/dev/null 2>&1 ) \
        || die "갈아 끼운 배포본이 실행되지 않습니다"
    echo "  실행파일 $(ls -la "$RS_DIST/MpowerClassify-rs.exe" | awk '{print $5}') B"
}


#------------------------------------------------------------------
# 두 판 교차 검증
#=> 같은 문서를 두 배포본에 돌려 레코드가 같은지 본다. 이것이 이 프로젝트의
#   불변식이다 — 두 구현이 다른 답을 내면 어느 쪽도 믿을 수 없다.
#   ts(줄마다 찍는 시각)와 윈도우 경로 구분자만 빼고 통째로 견준다.
#
# -in: 없음(바깥 SAMPLE 을 쓴다)
#
# -out: 없음
# -out: error = 한 건이라도 다르면 종료
#------------------------------------------------------------------
cross_check() {
    [ -d "$SAMPLE" ] || { echo "  (검증 건너뜀 — 표본 폴더가 없습니다: $SAMPLE)"; return 0; }
    [ -f "$PY_DIST/MpowerClassify.exe" ] || return 0
    [ -f "$RS_DIST/MpowerClassify-rs.exe" ] || return 0

    step "두 판 교차 검증 ($SAMPLE)"
    local tmp; tmp="$(mktemp -d)"
    ( cd "$PY_DIST" && ./MpowerClassify.exe --dir "$SAMPLE" --out "$tmp/py.json" \
        --format json --rule-only --nosummary >/dev/null 2>&1 ) || die "파이썬 판 실행 실패"
    ( cd "$RS_DIST" && ./MpowerClassify-rs.exe --dir "$SAMPLE" --out "$tmp/rs.json" \
        --format json --rule-only --nosummary >/dev/null 2>&1 ) || die "Rust 판 실행 실패"

    python - "$tmp" <<'PY' || die "두 판의 결과가 다릅니다 (위 목록 참고)"
import io, json, sys
sys.stdout.reconfigure(encoding="utf-8")
tmp = sys.argv[1]
def load(p):
    d = json.load(io.open(p, encoding="utf-8"))
    return {x["file"].replace("\\", "/"): x for x in d if "file" in x}
def norm(r):
    r = json.loads(json.dumps(r))
    r.pop("elapsed_ms", None)                       # 실행할 때마다 다르다
    r["file"] = r["file"].replace("\\", "/")        # 윈도우 경로 구분자
    if isinstance(r.get("meta"), dict):
        r["meta"].pop("ts", None)                   # 줄마다 찍는 시각
    return r
a, b = load(tmp + "/py.json"), load(tmp + "/rs.json")
bad = [f for f in a if f not in b or norm(a[f]) != norm(b[f])]
for f in bad[:5]:
    print("  다름:", f)
print("  비교 %d건 · 불일치 %d건" % (len(a), len(bad)))
sys.exit(1 if bad else 0)
PY
    rm -rf "$tmp"
}


case "$WHICH" in
    python) build_python ;;
    rust)   build_rust ;;
    both)   build_python; build_rust ;;
    *)      die "쓰는 법: $0 [python|rust|both]" ;;
esac
cross_check

step "완료"
echo "  배포본: $PY_DIST · $RS_DIST"
