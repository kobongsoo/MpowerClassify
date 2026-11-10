#!/usr/bin/env bash
#------------------------------------------------------------------
# 리눅스 배포본 만들기 — 윈도우에서 도는 쪽(지휘)
#=> 소스를 빌드서버로 올리고, 거기서 linux-remote.sh 를 돌린 뒤, 결과 tgz 를
#   내려받는다. 실패하면 그 자리에서 멈춘다(_lib.sh 참고).
#
#   [접속 정보] 프로젝트 최상위의 .env_vllm 에서 읽는다(cp949). 비밀번호는
#   화면에도 로그에도 찍지 않는다 — 임시 파일에만 잠깐 두고 끝나면 지운다.
#
#   [왜 옛 배포본을 바로 안 지우나] 내려받기가 실패하면 손에 아무것도 안 남는다.
#   먼저 .bak-<시각> 으로 옮겨 두고, 새것을 다 받은 뒤에 지울지 정한다.
#
# 쓰는 법 (Git Bash, 프로젝트 최상위에서):
#   bash scripts/build/linux-build.sh            # 두 판 다
#   bash scripts/build/linux-build.sh python     # 한 판만
#------------------------------------------------------------------
. "$(dirname "$0")/_lib.sh"

cd "$(dirname "$0")/../.." || die "프로젝트 최상위로 이동하지 못했습니다"
WHICH="${1:-both}"

# 서버 안쪽 경로는 설정에서 읽는다(코드에 박지 않는다 - 저장소가 공개다).
load_server_env "$(dirname "$0")/server.env"
PY_BUILD="$BUILD_PY_DIR"
RS_BUILD="$BUILD_RS_DIR"

command -v plink >/dev/null || die "plink 를 찾을 수 없습니다 (PuTTY 설치·PATH 확인)"
command -v pscp  >/dev/null || die "pscp 를 찾을 수 없습니다"
[ -f .env_vllm ] || die ".env_vllm 이 없습니다 (접속 정보 파일)"

TMP="$(mktemp -d)"
# 끝나면 비밀번호 임시파일까지 반드시 지운다(정상 종료든 실패든).
trap 'rm -rf "$TMP"' EXIT

step "접속 정보 읽기"
python - "$TMP" <<'PY' || die ".env_vllm 에서 접속 정보를 읽지 못했습니다"
import io, sys
d = {}
for line in io.open(".env_vllm", encoding="cp949"):
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        d[k.strip()] = v.strip().strip('"').strip("'")
for k in ("SSH_SERVER", "SSH_USER_ID", "SSH_USER_PWD"):
    if not d.get(k):
        raise SystemExit("%s 가 없습니다" % k)
# 비밀번호는 파일로만 넘긴다 — 화면·명령행 인자에 남기지 않는다.
io.open(sys.argv[1] + "/.pw", "w", encoding="utf-8", newline="").write(d["SSH_USER_PWD"])
# [newline="" 가 핵심] 윈도우 파이썬은 기본으로 "\n" 을 "\r\n" 으로 바꿔 쓴다.
# 그러면 read 가 읽은 사용자명 끝에 "\r" 이 붙고, plink/pscp 가 그 이름으로
# 붙어 **인증만 조용히 거부**된다("Access denied"). 비밀번호는 멀쩡한데 비밀번호를
# 의심하게 되는, 찾기 고약한 실패였다(2026-09-11 실측).
# 끝에 개행은 넣는다 — read 가 개행 없이 EOF 를 만나면 값을 제대로 채우고도
# non-zero 를 돌려주고, set -e 가 그걸 실패로 보고 죽인다.
io.open(sys.argv[1] + "/.host", "w", encoding="utf-8", newline="").write(
    "%s %s\n" % (d["SSH_SERVER"].split(":")[0], d["SSH_USER_ID"]))
PY
# 위에서 개행을 넣었지만, 혹시 없더라도 값은 채워지므로 read 의 반환값으로
# 죽지 않게 한다(비어 있으면 바로 아래에서 걸러진다).
read -r HOST USER_ID < "$TMP/.host" || true
# 캐리지리턴이 섞여 들어와도 여기서 떨군다 — 위에서 막았지만, 이 값이 한 글자라도
# 더러우면 증상이 "비밀번호가 틀렸다"로 나타나 엉뚱한 곳을 뒤지게 된다.
HOST="${HOST%$'\r'}"; USER_ID="${USER_ID%$'\r'}"
[ -n "$HOST" ] && [ -n "$USER_ID" ] || die "접속 정보를 읽지 못했습니다(.env_vllm 확인)"
PW="$(cat "$TMP/.pw")"; PW="${PW%$'\r'}"
echo "  $USER_ID@$HOST (비밀번호는 출력하지 않습니다)"

step "소스 올리기"
tar -czf "$TMP/cso_src.tgz" \
    --exclude='build/_work' --exclude='build/_work_onefile' \
    --exclude='build/pyi-onefile' --exclude='build/csoclassify' \
    src build requirements.txt third_party/docparse/linux-x64 third_party/docparse/VERSION \
    || die "파이썬 소스를 묶지 못했습니다"
# third_party/docparse/linux-x64 = 별도 프로젝트(DocParse-rust)가 만든 libdocparse.so(문서파서).
# Rust 판은 build.rs 가 exe 옆으로 복사하고, 파이썬 판은 spec 이 묶는다. 빠지면 두 판 모두 1017 로 멈춘다.
tar -czf "$TMP/rustsrc.tgz" -C Rust \
    Cargo.toml Cargo.lock build.rs src resources tests README.md \
    -C .. third_party/docparse/linux-x64 third_party/docparse/VERSION \
    || die "Rust 소스를 묶지 못했습니다"
# 규칙셋·분류체계·seed 는 '갱신되는 쪽'에서 가져온다(빌드가이드 4장 '정책 파일 정본').
tar -czf "$TMP/assets.tgz" -C . \
    resources/policy/cso_rule.yaml ui/policy/doc_rule.yaml \
    >/dev/null 2>&1 || true

pscp -batch -q -pw "$PW" "$TMP/cso_src.tgz" "$USER_ID@$HOST:$PY_BUILD/"  || die "파이썬 소스 전송 실패"
pscp -batch -q -pw "$PW" "$TMP/rustsrc.tgz" "$USER_ID@$HOST:$RS_BUILD/"  || die "Rust 소스 전송 실패"
# 서버에서 도는 쪽도 같은 설정을 쓴다 - 함께 올린다.
pscp -batch -q -pw "$PW" scripts/build/_lib.sh scripts/build/linux-remote.sh \
     scripts/build/server.env "$USER_ID@$HOST:/tmp/" || die "빌드 스크립트 전송 실패"

# 외장 자산(규칙셋·분류체계·seed·유의어·README)도 함께 올린다 — 빠지면
# 배포본이 오류 없이 반쪽이 된다.
# 자동 생성 규칙(doc_rule.yaml)은 만들 때 쓴 입력(체계 JSON·본보기·회사 조정)이
# 곁에 없으면 돌 때마다 "입력이 없어짐" 경고를 내고, 현장에서 다시 만들 수도 없다.
pscp -batch -q -pw "$PW" \
     resources/policy/cso_rule.yaml ui/policy/doc_rule.yaml \
     ui/policy/doc_classification_export.json resources/policy/doc_rule_template.yaml \
     Rust/dist-onedir/linux/class_seed.jsonl Rust/dist-onedir/linux/README.txt \
     "$USER_ID@$HOST:$RS_BUILD/assets/" || die "외장 자산 전송 실패"
# 회사 조정 파일은 없는 회사도 있다 — 있을 때만 올리고, 없으면 서버의 옛것을 지운다.
if [ -f ui/policy/doc_rule.local.yaml ]; then
    pscp -batch -q -pw "$PW" ui/policy/doc_rule.local.yaml "$USER_ID@$HOST:$RS_BUILD/assets/" \
        || die "회사 조정 파일 전송 실패"
else
    plink -batch -ssh -l "$USER_ID" -pw "$PW" "$HOST" "rm -f $RS_BUILD/assets/doc_rule.local.yaml" \
        || die "서버의 옛 회사 조정 파일을 지우지 못했습니다"
fi
tar -czf "$TMP/synonyms.tgz" -C Rust/dist-onedir/linux synonyms 2>/dev/null \
    && pscp -batch -q -pw "$PW" "$TMP/synonyms.tgz" "$USER_ID@$HOST:/tmp/" \
    && plink -batch -ssh -l "$USER_ID" -pw "$PW" "$HOST" \
       "tar xzf /tmp/synonyms.tgz -C $RS_BUILD/assets" || die "유의어 사전 전송 실패"

step "빌드서버에서 빌드"
plink -batch -ssh -l "$USER_ID" -pw "$PW" "$HOST" "bash /tmp/linux-remote.sh $WHICH" \
    || die "빌드서버에서 실패했습니다 (위 출력 참고)"

step "내려받기"
TS="$(date +%Y%m%d-%H%M)"
fetch() {
    local remote="$1" local_dir="$2" name="$3"
    if [ -f "$local_dir/$name" ]; then
        mv "$local_dir/$name" "$local_dir/$name.bak-$TS" || die "옛 배포본을 옮기지 못했습니다"
    fi
    if ! pscp -batch -q -pw "$PW" "$USER_ID@$HOST:$remote" "$local_dir/"; then
        # 못 받았으면 옛것을 되돌린다 — 손에 아무것도 없는 상태로 두지 않는다.
        [ -f "$local_dir/$name.bak-$TS" ] && mv "$local_dir/$name.bak-$TS" "$local_dir/$name"
        die "내려받기 실패: $remote"
    fi
    # 받은 tgz 가 온전한지 목록을 읽어 본다(끊긴 파일을 배포본으로 두지 않는다).
    tar tzf "$local_dir/$name" >/dev/null 2>&1 || die "받은 파일이 온전하지 않습니다: $name"
    echo "  $name  $(ls -la "$local_dir/$name" | awk '{print $5}') B"
}

case "$WHICH" in
    python|both) fetch /tmp/MpowerClassify-onedir-linux.tgz dist-onedir/linux \
                       MpowerClassify-onedir-linux.tgz ;;
esac
case "$WHICH" in
    rust|both)
        fetch /tmp/MpowerClassify-rs-linux.tgz Rust/dist-onedir/linux \
              MpowerClassify-rs-linux.tgz
        pscp -batch -q -pw "$PW" "$USER_ID@$HOST:$RS_BUILD/target/release/MpowerClassify-rs" \
             Rust/dist-onedir/linux/ || die "Rust 실행파일 내려받기 실패" ;;
esac

step "완료"
echo "  dist-onedir/linux · Rust/dist-onedir/linux"
echo "  (옛 배포본은 .bak-$TS 로 남겨 뒀습니다 — 확인 후 지우세요)"
