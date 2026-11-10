#!/usr/bin/env bash
#------------------------------------------------------------------
# 리눅스 배포본 만들기 — 빌드서버에서 도는 쪽
#=> 이 파일은 **빌드서버(CentOS 7.9)** 에서 실행된다. 윈도우 쪽은
#   linux-build.sh 가 소스를 올리고 이 스크립트를 호출한다.
#   실패하면 그 자리에서 멈춘다(_lib.sh 참고).
#
#   [왜 서버에서 묶나] _internal/ 안의 .so 들이 심볼릭 링크로 얽혀 있다.
#   Windows 파일시스템은 그 링크를 보존하지 못해, 폴더째 옮기면 끊긴다.
#   그래서 리눅스에서 tar 로 묶은 채 옮긴다.
#
#   [모델을 왜 대피시키나] 임베딩 모델(129MB)은 spec 이 안 넣어 주는 외장
#   자산이라 dist 폴더에 손으로 놓여 있다. PyInstaller 의 -y 가 그 폴더를
#   통째로 지운다. 예전에 이걸 잃고 리눅스 배포본이 '벡터없음'으로 나간 적이
#   있어(2026-09-08), 빌드 전후로 옮겼다 되돌린다.
#
# 쓰는 법 (빌드서버에서):
#   bash linux-remote.sh              # 두 판 다
#   bash linux-remote.sh python|rust  # 한 판만
#------------------------------------------------------------------
. "$(dirname "$0")/_lib.sh"

WHICH="${1:-both}"

# 서버 안쪽 경로는 설정에서 읽는다(코드에 박지 않는다 - 저장소가 공개다).
# 이 파일은 서버 /tmp 에서 도므로 설정도 거기로 함께 올라온다.
load_server_env "$(dirname "$0")/server.env"
PY_BUILD="$BUILD_PY_DIR"
RS_BUILD="$BUILD_RS_DIR"
MODEL_SRC="$MODEL_SRC_DIR"
# site-packages 는 파이썬 판올림마다 폴더 이름이 바뀐다(python3.13 → 3.14 …).
# 이름을 박아 두면 조용히 못 찾으므로, 있는 것을 찾아서 쓴다.
CONDA_LIB="$CONDA_ENV_DIR/lib"
CONDA_SP="$(ls -d "$CONDA_LIB"/python*/site-packages 2>/dev/null | head -1)"
[ -n "$CONDA_SP" ] || die "conda 환경에서 site-packages 를 찾지 못했습니다: $CONDA_LIB"


#------------------------------------------------------------------
# 파이썬 판 — 소스 풀기 → 모델 대피 → 빌드 → 모델 복귀 → tgz
#
# -in: 없음(바깥 PY_BUILD·MODEL_SRC 를 쓴다)
#
# -out: /tmp/MpowerClassify-onedir-linux.tgz
# -out: error = 어느 단계든 실패하면 종료
#------------------------------------------------------------------
build_python() {
    step "리눅스 · 파이썬 소스 풀기"
    cd "$PY_BUILD" || die "빌드 폴더가 없습니다: $PY_BUILD"
    [ -f cso_src.tgz ] || die "cso_src.tgz 가 없습니다 (윈도우 쪽에서 먼저 올리세요)"
    rm -rf src build
    tar xzf cso_src.tgz || die "소스 압축을 풀지 못했습니다"
    [ -f build/csoclassify.spec ] || die "푼 소스에 build/csoclassify.spec 이 없습니다"

    # 모델 대피 — 아래 -y 가 dist 폴더를 통째로 지운다.
    local kept=""
    if [ -d dist/MpowerClassify/models ]; then
        kept=/tmp/keep_models.$$
        rm -rf "$kept"
        mv dist/MpowerClassify/models "$kept" || die "모델을 대피시키지 못했습니다"
        echo "  모델 대피: $(du -sh "$kept" | cut -f1)"
    fi

    step "리눅스 · 파이썬 빌드"
    # shellcheck disable=SC1091
    . "$CONDA_PROFILE" || die "conda 초기화 스크립트를 찾지 못했습니다: $CONDA_PROFILE"
    conda activate "$CONDA_ENV_NAME" || die "conda 환경을 켜지 못했습니다: $CONDA_ENV_NAME"
    # 문서파서 라이브러리(Rust 문서파서, --extractor rust) — 설계: plan/Rust-문서파서-라이브러리분리-설계-20261110.html §6-2
    # 별도 프로젝트(DocParse-rust)가 빌드서버에서 만든 libdocparse.so 를 소스 묶음에 실어 왔다. spec 이 묶는다.
    local with_docparse=""
    if [ -f third_party/docparse/linux-x64/libdocparse.so ]; then
        with_docparse=1
        echo "  docparse $(cat third_party/docparse/VERSION 2>/dev/null || echo '?')"
    else
        echo "  [경고] third_party/docparse/linux-x64/libdocparse.so 가 없어 문서파서 없이 만듭니다 — 배포본에서 --extractor rust 를 쓸 수 없습니다"
    fi
    pyinstaller -y build/csoclassify.spec > build.log 2>&1 \
        || die "PyInstaller 빌드 실패 — $PY_BUILD/build.log 를 보세요"
    [ -f dist/MpowerClassify/MpowerClassify ] || die "빌드 결과에 실행본체가 없습니다"
    if [ -n "$with_docparse" ]; then
        [ -f dist/MpowerClassify/_internal/libdocparse.so ] \
            || die "배포본에 libdocparse.so 가 없습니다 — build/csoclassify.spec 의 binaries 를 보세요"
    fi

    # 모델 복귀 — 대피시킨 것이 있으면 그것을, 없으면 원본에서 새로 복사한다.
    if [ -n "$kept" ]; then
        mv "$kept" dist/MpowerClassify/models || die "모델을 되돌리지 못했습니다 (대피본: $kept)"
    else
        [ -d "$MODEL_SRC" ] || die "모델 원본이 없습니다: $MODEL_SRC"
        mkdir -p dist/MpowerClassify/models
        cp -r "$MODEL_SRC" dist/MpowerClassify/models/ || die "모델 복사 실패"
    fi
    # 모델이 실제로 들어갔는지 본다 — 없으면 전파가 조용히 꺼진 배포본이 된다.
    [ -f dist/MpowerClassify/models/e5-small-ko/model.onnx ] \
        || die "모델이 배포 폴더에 없습니다 (이대로 나가면 임베딩 전파가 통째로 꺼집니다)"
    echo "  모델 확인: $(du -sh dist/MpowerClassify/models | cut -f1)"

    step "리눅스 · 파이썬 묶기"
    ( cd dist && tar czf /tmp/MpowerClassify-onedir-linux.tgz MpowerClassify ) \
        || die "tgz 로 묶지 못했습니다"
    [ "$(tar tzf /tmp/MpowerClassify-onedir-linux.tgz | grep -c 'models/e5-small-ko/model.onnx')" = "1" ] \
        || die "묶인 tgz 에 모델이 없습니다"
    echo "  $(ls -lh /tmp/MpowerClassify-onedir-linux.tgz | awk '{print $5}')"
}


#------------------------------------------------------------------
# Rust 판 — 소스 풀기 → 빌드 → 배포 폴더 조립 → tgz
#=> RPATH=$ORIGIN 이 핵심이다. 이게 없으면 CentOS 7 에서
#   CXXABI_1.3.8 not found 로 못 뜬다.
#
# -in: 없음(바깥 RS_BUILD·CONDA_* 를 쓴다)
#
# -out: /tmp/MpowerClassify-rs-linux.tgz
# -out: error = 어느 단계든 실패하면 종료
#------------------------------------------------------------------
build_rust() {
    step "리눅스 · Rust 소스 풀기"
    export PATH="$CARGO_BIN_DIR:$PATH"
    cd "$RS_BUILD" || die "빌드 폴더가 없습니다: $RS_BUILD"
    [ -f rustsrc.tgz ] || die "rustsrc.tgz 가 없습니다 (윈도우 쪽에서 먼저 올리세요)"
    # 옛 crates/(분리 전 작업 공간)와 third_party/ 도 지운다 — 옮기거나 지운 파일이 남아 섞이지 않게.
    rm -rf src crates tests resources third_party
    tar xzf rustsrc.tgz || die "소스 압축을 풀지 못했습니다"

    step "리눅스 · Rust 빌드"
    # 실행파일에 "필요한 .so 는 내 옆 폴더에서 찾아라"를 새겨 넣는다.
    # --disable-new-dtags 로 RUNPATH 대신 옛 RPATH 를 쓴다 — RUNPATH 는
    # 실행파일이 '직접' 쓰는 것에만 걸려, libonnxruntime 이 다시 부르는
    # libstdc++ 까지는 안 미친다.
    export RUSTFLAGS='-C link-arg=-Wl,-rpath,$ORIGIN -C link-arg=-Wl,--disable-new-dtags'
    cargo build --release > build.log 2>&1 || die "cargo build 실패 — $RS_BUILD/build.log"
    [ -f target/release/MpowerClassify-rs ] || die "빌드 결과에 실행파일이 없습니다"
    readelf -d target/release/MpowerClassify-rs | grep -q 'RPATH.*\$ORIGIN' \
        || die "실행파일에 RPATH=\$ORIGIN 이 없습니다 (RUSTFLAGS 가 안 먹었습니다)"

    step "리눅스 · Rust 배포 폴더 조립"
    local D="$RS_BUILD/dist-onedir/linux"
    rm -rf "$D" && mkdir -p "$D/models"
    cp target/release/MpowerClassify-rs "$D/" && chmod +x "$D/MpowerClassify-rs"
    # 문서파서 라이브러리(별도 프로젝트 DocParse-rust 산출물) — 없으면 문서를 읽기 전에 1017 로 멈춘다.
    [ -f third_party/docparse/linux-x64/libdocparse.so ] || die "third_party/docparse/linux-x64/libdocparse.so 가 없습니다"
    cp third_party/docparse/linux-x64/libdocparse.so "$D/" 
    # ONNX Runtime 은 반드시 버전 숫자 없는 이름으로 둔다(로더가 그 이름을 찾는다).
    cp "$CONDA_SP/onnxruntime/capi/libonnxruntime.so.1.26.0"           "$D/libonnxruntime.so" \
        || die "libonnxruntime 복사 실패"
    cp "$CONDA_SP/onnxruntime/capi/libonnxruntime_providers_shared.so" "$D/"
    cp "$CONDA_SP/pypdfium2_raw/libpdfium.so"                          "$D/"
    # CentOS7 시스템 libstdc++(4.8.5)는 CXXABI_1.3.8+ 를 못 준다 — 반드시 동봉.
    cp "$CONDA_LIB/libstdc++.so.6.0.34" "$D/libstdc++.so.6" || die "libstdc++ 복사 실패"
    cp "$CONDA_LIB/libgcc_s.so.1"       "$D/"
    strip --strip-unneeded "$D/libstdc++.so.6"
    chmod +x "$D"/*.so
    [ -d "$MODEL_SRC" ] || die "모델 원본이 없습니다: $MODEL_SRC"
    cp -r "$MODEL_SRC" "$D/models/" || die "모델 복사 실패"
    chmod -R u+rwX,go+rX "$D/models"
    # 규칙셋·분류체계·seed·유의어·README — 빠뜨리면 배포본이 조용히 반쪽이 된다.
    # 체계 JSON·본보기는 자동 생성 규칙의 입력이다 — 곁에 없으면 돌 때마다 경고가 난다.
    for f in cso_rule.yaml doc_rule.yaml doc_classification_export.json \
             doc_rule_template.yaml class_seed.jsonl README.txt; do
        [ -f "$RS_BUILD/assets/$f" ] || die "assets/$f 가 없습니다 (윈도우 쪽에서 올리세요)"
        cp "$RS_BUILD/assets/$f" "$D/"
    done
    # 회사 조정 파일은 없는 회사도 있다(없으면 건너뛴다 — set -e 에 걸리지 않게 if 로 쓴다).
    if [ -f "$RS_BUILD/assets/doc_rule.local.yaml" ]; then
        cp "$RS_BUILD/assets/doc_rule.local.yaml" "$D/"
    fi
    [ -d "$RS_BUILD/assets/synonyms" ] && cp -r "$RS_BUILD/assets/synonyms" "$D/"

    step "리눅스 · Rust 묶기"
    cd "$RS_BUILD/dist-onedir"
    rm -rf MpowerClassify-rs-linux
    cp -r linux MpowerClassify-rs-linux
    tar czf /tmp/MpowerClassify-rs-linux.tgz MpowerClassify-rs-linux || die "tgz 로 묶지 못했습니다"
    rm -rf MpowerClassify-rs-linux
    [ "$(tar tzf /tmp/MpowerClassify-rs-linux.tgz | grep -c 'models/e5-small-ko/model.onnx')" = "1" ] \
        || die "묶인 tgz 에 모델이 없습니다"
    echo "  $(ls -lh /tmp/MpowerClassify-rs-linux.tgz | awk '{print $5}')"
}


#------------------------------------------------------------------
# 깨끗한 자리에서 두 판을 돌려 본다
#=> 빌드 폴더에 남아 있는 파일에 기대고 있으면 여기서 드러난다.
#   '전파' 줄이 뜨면 모델과 .so 가 패키지 안에서 제대로 로드된 것이다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 한 판이라도 못 돌면 종료
#------------------------------------------------------------------
clean_room() {
    [ -d /tmp/csotest/doc ] || { echo "  (검증 건너뜀 — /tmp/csotest/doc 가 없습니다)"; return 0; }
    step "리눅스 · 깨끗한 자리 검증"
    rm -rf /tmp/deploytest && mkdir -p /tmp/deploytest
    cd /tmp/deploytest
    [ -f /tmp/MpowerClassify-rs-linux.tgz ] && tar xzf /tmp/MpowerClassify-rs-linux.tgz
    [ -f /tmp/MpowerClassify-onedir-linux.tgz ] && tar xzf /tmp/MpowerClassify-onedir-linux.tgz

    if [ -d /tmp/deploytest/MpowerClassify-rs-linux ]; then
        ( cd /tmp/deploytest/MpowerClassify-rs-linux \
          && ./MpowerClassify-rs --dir /tmp/csotest/doc --out /tmp/c_rs.jsonl \
             --simple --nosummary --format jsonl >/dev/null 2>&1 ) \
            || die "Rust 배포본이 깨끗한 자리에서 돌지 않습니다"
        ldd /tmp/deploytest/MpowerClassify-rs-linux/MpowerClassify-rs | grep -q 'not found' \
            && die "Rust 배포본에 못 찾는 .so 가 있습니다"
        echo "  Rust  OK"
    fi
    if [ -d /tmp/deploytest/MpowerClassify ]; then
        ( cd /tmp/deploytest/MpowerClassify \
          && ./MpowerClassify --dir /tmp/csotest/doc --out /tmp/c_py.jsonl \
             --simple --nosummary --format jsonl --cso-rules "$RS_BUILD/assets/cso_rule.yaml" >/dev/null 2>&1 ) \
            || die "파이썬 배포본이 깨끗한 자리에서 돌지 않습니다"
        echo "  Python OK"
    fi
}


case "$WHICH" in
    python) build_python ;;
    rust)   build_rust ;;
    both)   build_python; build_rust ;;
    *)      die "쓰는 법: $0 [python|rust|both]" ;;
esac
clean_room

step "완료 — 윈도우에서 내려받으세요"
ls -lh /tmp/MpowerClassify-*.tgz 2>/dev/null | awk '{print "  " $9 "  " $5}'
