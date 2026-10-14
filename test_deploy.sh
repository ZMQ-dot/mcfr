#!/usr/bin/env bash
# =============================================================================
#  deploy.sh 的回归测试
# =============================================================================
#
#  用法：  bash test_deploy.sh
#
#  它做三件事：
#    A. 把 deploy.sh「主流程之前」的函数定义抽出来 source 进来，直接单测。
#       测的是真代码，不是副本。
#    B. 造一个假的发布包（故意多套一层目录、混入不该带的文件），
#       跑一遍完整流程（dry-run），验证自动识别和校验逻辑。
#    C. 拿损坏的 / 不完整的包去撞，验证它们被明确拦住，
#       而不是稀里糊涂地把站点删了。
#
#  ！改了 deploy.sh 之后请跑一遍这个。
#    尤其是改「步骤顺序」「发布包来源」「配置读取」这三块的时候。
# =============================================================================

set -uo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
WORK="/tmp/mcfr-deploy-test"
PASS=0
FAIL=0
SKIP=0

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; PASS=$((PASS + 1)); }
bad()  { printf '  \033[31m✗\033[0m %s\n      期望 %s，实际 %s\n' "$1" "$2" "$3"; FAIL=$((FAIL + 1)); }
skip() { printf '  \033[33m–\033[0m %s（环境不支持，跳过）\n' "$1"; SKIP=$((SKIP + 1)); }

check() {  # check <描述> <期望> <实际>
    if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1" "$2" "$3"; fi
}

rm -rf "$WORK"
mkdir -p "$WORK"

PY="$(command -v python || command -v python3 || true)"

# 打 zip 用 Python —— 这个环境里没有 zip 命令
make_zip() {  # make_zip <目录> <输出.zip>
    "$PY" - "$1" "$2" <<'PYEOF'
import os, sys, zipfile
src, out = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for root, _dirs, files in os.walk(src):
        for name in files:
            full = os.path.join(root, name)
            z.write(full, os.path.relpath(full, src))
PYEOF
}

if [[ -z "$PY" ]]; then
    echo "找不到 python，无法运行本测试" >&2
    exit 1
fi

# =============================================================================
echo
echo "=== A. 单测函数（sourced 自真实脚本） ==="
# =============================================================================

sed -n '1,/^require_root$/p' "$REPO/deploy.sh" | head -n -1 > "$WORK/funcs.sh"
# shellcheck disable=SC1090
source "$WORK/funcs.sh"

# ！funcs.sh 里带着 deploy.sh 开头的 `set -euo pipefail`，source 之后
#   测试也会被 set -e 接管 —— 后面那些「期望返回非 0」的用例一执行
#   就会把测试本身掀翻。这里把 -e 关掉，-u 和 pipefail 保留。
set +e

# ！funcs.sh 里带着 deploy.sh 自己的 ok()/warn()/die()...，source 之后会把
#   本测试的 ok()/bad()/skip() 覆盖掉 —— 症状：✓ 照样打出来（用的是 deploy.sh
#   的 ok），但 PASS 永远不加，末尾统计永远显示「通过 0 项」。
#   所以这里把测试自己的那套重新声明一遍，并清零计数。
PASS=0; FAIL=0; SKIP=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; PASS=$((PASS + 1)); }
bad()  { printf '  \033[31m✗\033[0m %s\n      期望 %s，实际 %s\n' "$1" "$2" "$3"; FAIL=$((FAIL + 1)); }
skip() { printf '  \033[33m–\033[0m %s（环境不支持，跳过）\n' "$1"; SKIP=$((SKIP + 1)); }
check() {
    if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1" "$2" "$3"; fi
}

SHARED_DIR="$WORK/shared-a"; mkdir -p "$SHARED_DIR"
ensure_env "K1" "v1" >/dev/null 2>&1
check "ensure_env 缺失时追加" "K1=v1" "$(grep '^K1=' "$SHARED_DIR/.env")"
ensure_env "K1" "v2" >/dev/null 2>&1
check "ensure_env 已有值时不动" "K1=v1" "$(grep '^K1=' "$SHARED_DIR/.env")"
printf 'K2=\n' >> "$SHARED_DIR/.env"
ensure_env "K2" "filled" >/dev/null 2>&1
check "ensure_env 空值时补上" "K2=filled" "$(grep '^K2=' "$SHARED_DIR/.env")"

SERVICE_FILE="$WORK/mcfr.service"
NGINX_AVAILABLE="$WORK/nginx-site.conf"
APP_PORT=""; DOMAIN=""
check "detect_port 无配置时用默认" "8000" "$(detect_port)"
check "detect_domain 无配置时用默认" "pccode.cn" "$(detect_domain)"

printf 'ExecStart=/x/venv/bin/python run.py --host 127.0.0.1 --port 9123\n' > "$SERVICE_FILE"
printf 'server {\n    server_name example.org;\n}\n' > "$NGINX_AVAILABLE"
check "detect_port 能从现有 unit 读出" "9123" "$(detect_port)"
check "detect_domain 能从现有 nginx 读出" "example.org" "$(detect_domain)"

APP_PORT="7777"; DOMAIN="override.cn"
check "--port 优先于自动检测" "7777" "$(detect_port)"
check "--domain 优先于自动检测" "override.cn" "$(detect_domain)"

# =============================================================================
echo
echo "=== B. 替身命令 ==="
# =============================================================================
# deploy.sh 会检查 python3 / unzip / nginx / certbot / ufw / apt-get。
# Windows 的 Git Bash 里前两个可能没有，后几个基本都没有。
# 这里给它们塞上替身 —— 要验的是流程逻辑，不是这些外部程序本身。

mkdir -p "$WORK/bin"

# unzip 必须真能解压：staging 那一步没包在 run() 里，dry-run 也会执行
cat > "$WORK/bin/unzip" <<'SHIM'
#!/bin/sh
z=""; d="."
while [ $# -gt 0 ]; do
  case "$1" in
    -d) d="$2"; shift 2 ;;
    -q|-o) shift ;;
    *) z="$1"; shift ;;
  esac
done
exec python -c 'import sys,zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' "$z" "$d"
SHIM

printf '#!/bin/sh\nexit 0\n'                    > "$WORK/bin/nginx"
printf '#!/bin/sh\nexit 0\n'                    > "$WORK/bin/certbot"
printf '#!/bin/sh\nexit 0\n'                    > "$WORK/bin/ufw"

# ！Git Bash 里那个 python3 多半是 Windows 应用商店的占位程序：
#   `command -v` 找得到，但什么都不做 —— `python3 -c "import venv"` 会失败，
#   于是脚本（正确地）判定缺 python3-venv，然后因为没有 apt 而退出。
#   更坑的是打包那一步也会被它静默吞掉。
#   所以这里做个转发替身：只有 -c 探测直接回答 0，其余原样交给真 python。
REAL_PY="$PY"
cat > "$WORK/bin/python3" <<SHIM
#!/bin/sh
case "\$1" in
  -c) exit 0 ;;
  *)  exec "$REAL_PY" "\$@" ;;
esac
SHIM

chmod +x "$WORK/bin"/*

# =============================================================================
echo
echo "=== C. 完整 dry-run（含自动识别） ==="
# =============================================================================

PKG="$WORK/pack/mcfr-release"
mkdir -p "$PKG/backend" "$PKG/frontend/js" "$PKG/data" "$PKG/__pycache__"
echo 'print("hi")'                 > "$PKG/run.py"
printf 'fastapi>=0.115\n'          > "$PKG/requirements.txt"
printf '__version__ = "9.9.9"\n'   > "$PKG/backend/__init__.py"
printf 'console.log(1)\n'          > "$PKG/frontend/js/api.js"
printf 'secret-from-dev-machine\n' > "$PKG/.env"
touch "$PKG/data/mcfr.db"          # 不该被带进去
touch "$PKG/__pycache__/x.pyc"     # 不该被带进去

# 把外层 pack/ 整个打包，这样 zip 里是 mcfr-release/run.py 而不是 run.py，
# 真正验证「自动去掉外层目录」那段逻辑
make_zip "$WORK/pack" "$WORK/mc.zip"

# require_root 换成 true，好在非 root 下测；其余一行未改
sed 's/^require_root$/true/' "$REPO/deploy.sh" > "$WORK/deploy_test.sh"

# </dev/null 很关键：脚本会在交互终端里问邮箱，这里必须让它认为没有终端，
# 否则测试会挂在 read 上。
PATH="$WORK/bin:$PATH" bash "$WORK/deploy_test.sh" \
    --base "$WORK/app" --zip "$WORK/mc.zip" --dry-run > "$WORK/c.log" 2>&1 </dev/null
RC=$?

echo "    [退出码 $RC]"
grep -E '识别到项目根|入口：|依赖表|后端目录|前端目录|结构校验|使用指定的发布包|监听|域名' \
    "$WORK/c.log" | sed 's/^ */      /'

check "dry-run 退出码为 0" "0" "$RC"
check "自动识别到项目根（剥掉外层目录）" "1" "$(grep -c '识别到项目根：mcfr-release' "$WORK/c.log")"
check "结构校验通过" "1" "$(grep -c '发布包结构校验通过' "$WORK/c.log")"
check "包内版本已读出来（排查「代码没换上」的依据）" "1" "$(grep -c '包内版本：9.9.9' "$WORK/c.log")"
check "包内文件数已读出来" "1" "$(grep -c '包内文件：' "$WORK/c.log")"

# =============================================================================
echo
echo "=== D. 发布包自打包（--from-dir） ==="
# =============================================================================

rm -f "$WORK/mc.zip"
PATH="$WORK/bin:$PATH" bash "$WORK/deploy_test.sh" \
    --base "$WORK/app-d" --from-dir "$PKG" --dry-run > "$WORK/d.log" 2>&1 </dev/null
RC_D=$?
# 注意：脚本把包写到 $BASE_DIR/mc.zip，也就是 app-d/ 下面，不是 $WORK/
DZIP="$WORK/app-d/mc.zip"
check "--from-dir 模式退出码为 0" "0" "$RC_D"
check "确实打包了" "1" "$(grep -c '现打发布包' "$WORK/d.log")"
if [[ -f "$DZIP" ]]; then
    ok "zip 生成在 \$BASE_DIR/mc.zip"
    check "zip 里没有 data/（不该带）" "0" \
        "$("$PY" -c "import zipfile,sys; print(sum(1 for n in zipfile.ZipFile(sys.argv[1]).namelist() if n.startswith('data/')))" "$DZIP")"
    check "zip 里没有 .env（不该带）" "0" \
        "$("$PY" -c "import zipfile,sys; print(sum(1 for n in zipfile.ZipFile(sys.argv[1]).namelist() if n == '.env'))" "$DZIP")"
    check "zip 里有 run.py" "1" \
        "$("$PY" -c "import zipfile,sys; print(sum(1 for n in zipfile.ZipFile(sys.argv[1]).namelist() if n == 'run.py'))" "$DZIP")"
    check "zip 里有 backend/" "1" \
        "$("$PY" -c "import zipfile,sys; print(1 if 'backend/__init__.py' in zipfile.ZipFile(sys.argv[1]).namelist() else 0)" "$DZIP")"
    check "dry-run 下跳过文件数校验" "1" "$(grep -c '跳过文件数校验' "$WORK/d.log")"
else
    bad "zip 生成在 \$BASE_DIR/mc.zip" "$DZIP 存在" "不存在"
fi

# =============================================================================
echo
echo "=== E. 错误输入应当被拦住 ==="
# =============================================================================

echo "garbage" > "$WORK/bad.zip"
PATH="$WORK/bin:$PATH" bash "$WORK/deploy_test.sh" \
    --base "$WORK/app-e1" --zip "$WORK/bad.zip" --dry-run > "$WORK/e1.log" 2>&1 </dev/null
E1=$?
echo "    [损坏包] 退出码=$E1  $(grep -o '解压失败.*' "$WORK/e1.log" | head -1)"
check "损坏的压缩包返回非 0" "1" "$E1"
check "提示是「解压失败」" "1" "$(grep -c '解压失败' "$WORK/e1.log")"

mkdir -p "$WORK/noproj"; echo "hi" > "$WORK/noproj/readme.txt"
make_zip "$WORK/noproj" "$WORK/noproj.zip"
PATH="$WORK/bin:$PATH" bash "$WORK/deploy_test.sh" \
    --base "$WORK/app-e2" --zip "$WORK/noproj.zip" --dry-run > "$WORK/e2.log" 2>&1 </dev/null
E2=$?
check "没有 run.py 的包返回非 0" "1" "$E2"
check "提示是「找不到 run.py」" "1" "$(grep -c '找不到 run.py' "$WORK/e2.log")"

mkdir -p "$WORK/noback"
echo 'print(1)' > "$WORK/noback/run.py"
printf 'fastapi\n' > "$WORK/noback/requirements.txt"
make_zip "$WORK/noback" "$WORK/noback.zip"
PATH="$WORK/bin:$PATH" bash "$WORK/deploy_test.sh" \
    --base "$WORK/app-e3" --zip "$WORK/noback.zip" --dry-run > "$WORK/e3.log" 2>&1 </dev/null
E3=$?
check "缺少 backend/ 返回非 0" "1" "$E3"
check "提示指出缺 backend/" "1" "$(grep -c '没有 backend/' "$WORK/e3.log")"

mkdir -p "$WORK/nofront"
echo 'print(1)' > "$WORK/nofront/run.py"
printf 'fastapi\n' > "$WORK/nofront/requirements.txt"
#  ！backend/ 里必须放个真文件：zip 不保存空目录，
#    只 mkdir 的话打出来的包里根本没有 backend/，
#    于是先撞上「没有 backend/」那条校验，测不到 frontend 这一条。
mkdir -p "$WORK/nofront/backend"
echo 'x' > "$WORK/nofront/backend/__init__.py"
make_zip "$WORK/nofront" "$WORK/nofront.zip"
PATH="$WORK/bin:$PATH" bash "$WORK/deploy_test.sh" \
    --base "$WORK/app-e4" --zip "$WORK/nofront.zip" --dry-run > "$WORK/e4.log" 2>&1 </dev/null
E4=$?
check "缺少 frontend/ 返回非 0" "1" "$E4"
check "提示指出缺 frontend/" "1" "$(grep -c '没有 frontend/' "$WORK/e4.log")"

# =============================================================================
echo
echo "=== F. 自保复制后，发布包来源不能退化 ==="
# =============================================================================
# ！这一节是照着一次真实故障写的（用户报「代码改了，部署上去还是旧版」）：
#   项目如果直接传进程序目录（$BASE_DIR/mc/）再运行 deploy.sh，脚本会
#   「自保」把自己复制到 $BASE_DIR 再重新执行。重新执行后它原来所在的
#   目录就找不到了，而 $BASE_DIR 下没有 run.py，于是「现打发布包」静默失效、
#   退回去用旧的 mc.zip —— 而且 rm -rf $APP_DIR 还会把刚传上去的源码删掉。
#   修复办法：把原目录通过 MCFR_ORIG_SELF 带过那次 exec。
#
#   这里就复现那个场景。

SELF_BASE="$WORK/self/app"
SELF_MC="$SELF_BASE/mc"
mkdir -p "$SELF_MC/backend" "$SELF_MC/frontend"
echo 'print(1)'                   > "$SELF_MC/run.py"
printf 'fastapi\n'                > "$SELF_MC/requirements.txt"
printf '__version__ = "7.7.7"\n' > "$SELF_MC/backend/__init__.py"
echo 'x'                          > "$SELF_MC/frontend/index.html"
sed 's/^require_root$/true/' "$REPO/deploy.sh" > "$SELF_MC/deploy.sh"

# 故意在 $BASE_DIR 放一个没用的旧 mc.zip：
# 如果脚本退化去用它，就会在「找不到 run.py」那一步直接失败。
make_zip "$WORK/noproj" "$SELF_BASE/mc.zip"

PATH="$WORK/bin:$PATH" bash "$SELF_MC/deploy.sh" \
    --base "$SELF_BASE" --dry-run > "$WORK/f.log" 2>&1 </dev/null
RC_F=$?
echo "    [退出码 $RC_F]"
grep -E '已将脚本复制|现打发布包|使用已上传的发布包|包内版本' "$WORK/f.log" | sed 's/^ */      /'

check "自保复制后仍然退出 0" "0" "$RC_F"
check "确实发生了自保复制" "1" "$(grep -c '已将脚本复制到' "$WORK/f.log")"
check "仍然从原目录现打发布包（没退化）" "1" "$(grep -c '现打发布包' "$WORK/f.log")"
check "没有去用 \$BASE_DIR 里那个旧 mc.zip" "0" "$(grep -c '使用已上传的发布包' "$WORK/f.log")"
check "包内容确实来自原目录（版本 7.7.7）" "1" "$(grep -c '包内版本：7.7.7' "$WORK/f.log")"

# =============================================================================
echo
# 把关键日志复制到仓库里 —— 终端回显时好时坏，用文件读才靠得住。
for f in c d e1 e2 e3 e4 f; do
    [[ -f "$WORK/$f.log" ]] && cp -f "$WORK/$f.log" "deploy-test-$f.log"
done

printf '通过 %d 项，失败 %d 项，跳过 %d 项\n' "$PASS" "$FAIL" "$SKIP"
[[ $FAIL -eq 0 ]] || exit 1
