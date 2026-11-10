#!/usr/bin/env bash
#------------------------------------------------------------------
# 문서파서(docparse) 배포 묶음 받아 넣기
#=> 별도 프로젝트 D:\Project\DocParse-rust 가 낸 묶음(zip·tgz)을 이 저장소에 풀어 버전을 고정한다.
#   설계: plan/Rust-문서파서-라이브러리분리-설계-20261110.html (개정 2) §3-3
#    1) 묶음을 임시 폴더에 풀고 SHA256SUMS 로 파일이 온전한지 확인
#    2) lib/* → third_party/docparse/<windows-x64|linux-x64>/ , include/ → third_party/docparse/include/
#    3) VERSION → third_party/docparse/VERSION (두 플랫폼 버전이 다르면 경고)
#    4) 감싸개 정본을 이 프로젝트 사본으로 갈아 끼움
#         python/docparse.py   → src/csoclassify/extract/_docparse.py
#         rust/docparse_dyn.rs → Rust/src/docparse_dyn.rs
#   그 뒤에 할 일: Rust 판 cargo build(build.rs 가 exe 옆으로 복사) · 평가셋 비교(CHANGELOG 에 '결과 바뀜'이 있으면)
#
#   사용: bash scripts/build/update-docparse.sh <묶음.zip|묶음.tgz> [...]
#         (인자가 없으면 ../DocParse-rust/dist 의 가장 새 묶음을 플랫폼마다 하나씩)
#------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")/../.." || exit 1

# 실패하면 사유를 남기고 멈춘다
die() { echo "[실패] $*" >&2; exit 1; }

bundles=("$@")
if [ ${#bundles[@]} -eq 0 ]; then
    for pat in "windows-x64.zip" "linux-x64.tgz"; do
        b="$(ls -t ../DocParse-rust/dist/docparse-*-"$pat" 2>/dev/null | head -1 || true)"
        [ -n "$b" ] && bundles+=("$b")
    done
fi
[ ${#bundles[@]} -gt 0 ] || die "받을 묶음이 없습니다 (../DocParse-rust/dist 를 보거나 경로를 주세요)"

PYTHONIOENCODING=utf-8 python -I - "${bundles[@]}" <<'PY' || die "묶음을 받아 넣지 못했습니다"
import hashlib, io, os, shutil, sys, tarfile, tempfile, zipfile

T = os.path.join("third_party", "docparse")

# 묶음 하나를 풀어 확인하고 제자리에 넣는다
def take(bundle):
    plat = "windows-x64" if bundle.endswith("windows-x64.zip") else "linux-x64" if bundle.endswith("linux-x64.tgz") else None
    if plat is None:
        raise SystemExit("묶음 이름으로 플랫폼을 모릅니다: %s" % bundle)
    tmp = tempfile.mkdtemp()
    try:
        if bundle.endswith(".zip"):
            zipfile.ZipFile(bundle).extractall(tmp)
        else:
            with tarfile.open(bundle) as t:
                t.extractall(tmp)
        (top,) = os.listdir(tmp)
        root = os.path.join(tmp, top)
        # SHA256SUMS 로 파일 확인 — 받다가 깨진 묶음을 넣지 않는다
        for line in io.open(os.path.join(root, "SHA256SUMS"), encoding="utf-8"):
            h, rel = line.strip().split("  ", 1)
            got = hashlib.sha256(open(os.path.join(root, rel), "rb").read()).hexdigest()
            if got != h:
                raise SystemExit("해시가 다릅니다: %s (%s)" % (rel, bundle))
        dst = os.path.join(T, plat)
        # 덮어쓰기 — 실행 중인 프로그램(시험·파이썬)이 DLL 을 물고 있으면 윈도우가 막으므로 사유를 알린다
        try:
            shutil.copytree(os.path.join(root, "lib"), dst, dirs_exist_ok=True)
            shutil.copytree(os.path.join(root, "include"), os.path.join(T, "include"), dirs_exist_ok=True)
        except (PermissionError, shutil.Error) as e:
            raise SystemExit("덮어쓰지 못했습니다(그 파일을 쓰는 프로그램을 닫고 다시): %s" % e)
        ver = io.open(os.path.join(root, "VERSION"), encoding="utf-8").read().strip()
        io.open(os.path.join(dst, "VERSION"), "w", encoding="utf-8", newline="\n").write(ver + "\n")
        # 감싸개 사본 갈아 끼우기
        shutil.copy2(os.path.join(root, "python", "docparse.py"), os.path.join("src", "csoclassify", "extract", "_docparse.py"))
        shutil.copy2(os.path.join(root, "rust", "docparse_dyn.rs"), os.path.join("Rust", "src", "docparse_dyn.rs"))
        shutil.copy2(os.path.join(root, "CHANGELOG.md"), os.path.join(T, "CHANGELOG.md"))
        print("  %-12s %s ← %s" % (plat, ver, os.path.basename(bundle)))
        return ver
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

os.makedirs(T, exist_ok=True)
vers = {take(b) for b in sys.argv[1:]}
# 두 플랫폼을 같은 버전으로 맞춘다 — 다르면 '같은 파서' 라는 전제가 깨진다
others = []
for plat in ("windows-x64", "linux-x64"):
    p = os.path.join(T, plat, "VERSION")
    if os.path.isfile(p):
        others.append(io.open(p, encoding="utf-8").read().strip())
if len(set(others)) > 1:
    print("  [경고] 플랫폼끼리 버전이 다릅니다: %s" % ", ".join(sorted(set(others))))
io.open(os.path.join(T, "VERSION"), "w", encoding="utf-8", newline="\n").write(sorted(vers)[-1] + "\n")
PY
echo "  다음: (cd Rust && cargo build) · CHANGELOG 에 '결과 바뀜'이 있으면 평가셋 비교"
