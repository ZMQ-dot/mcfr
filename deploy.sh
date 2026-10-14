#!/usr/bin/env bash
# =============================================================================
#  mcfr 一键升级 / 部署脚本
# =============================================================================
#
#  用法：
#      sudo bash deploy.sh
#
#          就够了。一台空白服务器上也行：它会自己装依赖、自己打包、
#          配好 systemd 和 nginx、开防火墙、申请 HTTPS 证书。
#
#          发布包从哪来，按顺序判断：
#            1. --zip 指定了        → 用它
#            2. 脚本所在目录是项目   → 现打一个（把整个项目传上来直接跑）
#            3. 都不满足            → 用 $BASE_DIR/mc.zip（你手工上传的那个）
#
#  常用开关：
#      --dry-run                     只打印要做什么，不改任何东西（先跑这个）
#      --domain pccode.cn            指定域名（默认自动沿用已配置的）
#      --port 8010                   换端口（默认自动沿用已配置的）
#      --email you@example.com       申请证书用的邮箱
#      --zip /tmp/new.zip            指定发布包
#      --from-dir /path/to/project   从指定目录打包
#      --no-https / --no-firewall / --no-nginx / --no-install
#                                    跳过对应环节
#      --skip-deps                   跳过 pip install
#      --import-from /srv/mcfr       从旧安装目录搬 data 和 .env 过来（换目录结构时用一次）
#      --rollback 20260924-143000    回滚到某次升级之前
#      --help
#
# -----------------------------------------------------------------------------
#  目录结构（这个布局是刻意的，别改）
# -----------------------------------------------------------------------------
#
#      /root/mcfr/
#      ├── mc.zip                 上传的发布包
#      ├── mc/                    程序目录，每次升级整个重建
#      │   ├── run.py  backend/  frontend/  requirements.txt
#      │   ├── .env   ──软链接──► ../shared/.env
#      │   └── data   ──软链接──► ../shared/data
#      ├── shared/                **状态**。升级时绝不动它
#      │   ├── .env
#      │   └── data/
#      │       ├── mcfr.db           账号、资料、关系
#      │       ├── code_secret.key   验证码签名密钥
#      │       ├── uploads/          头像与皮肤
#      │       ├── mirror/           **模组镜像文件，几个 GB**
#      │       └── backups/          后台「系统升级」存的数据库快照
#      ├── venv/                  虚拟环境，也在外面
#      └── backups/<时间戳>/      每次升级前的快照
#
#  ！升级包和升级备份都【不含】data/mirror ——
#    镜像文件动辄几个 GB，而且随时能从 Modrinth 重新拉回来，
#    让它跟着代码走只会让每次升级都慢得离谱，还白占一份磁盘。
#    data/backups 也一并跳过：里面装的就是备份，再备一遍没意义。
#    真正丢了找不回来的只有数据库、密钥和上传的图片 —— 那些照常备份。
#    （回滚时 mirror/ 原地保留，见 do_rollback 里的说明。）
#
#  ！为什么要用软链接把 data 和 .env 接到外面：
#    这样「清空 mc/ 再解压」永远是安全的，不用维护一份
#    「哪些目录不能删」的名单 —— 那种名单迟早会漏一个，
#    而漏掉的代价是用户数据。状态和代码必须物理分开。
#
# -----------------------------------------------------------------------------
#  以后写程序要遵守的约定（不遵守这个脚本就会出错）
# -----------------------------------------------------------------------------
#
#   1. 入口固定是项目根目录下的 run.py，用 `python run.py` 启动。
#   2. **运行期数据一律放进项目根的 data/ 目录**（数据库、上传文件、密钥）。
#      不要写到 /var、/tmp、用户主目录或任何绝对路径 —— 那些不会被备份，
#      升级时也会丢。
#   3. **配置只从项目根的 .env 读**（用 backend/config.py 的 load_env()）。
#      不要自己去找别的配置文件，也不要把配置写死在代码里。
#   4. 路径一律用 `Path(__file__).resolve().parent` 相对推导，不写绝对路径。
#   5. 依赖只写在 requirements.txt 里。服务器上没有 npm、没有构建步骤。
#   6. 前端资源改动后要同步 bump index.html / admin.html 里的 ?v=，
#      以及 backend/__init__.py 的 __version__。
#
#  只要遵守这几条，升级脚本永远不需要为新功能做改动。
# =============================================================================

set -euo pipefail

# -----------------------------------------------------------------------------
# 可调参数
# -----------------------------------------------------------------------------

BASE_DIR="${MCFR_BASE:-/root/mcfr}"
APP_DIR="$BASE_DIR/mc"
SHARED_DIR="$BASE_DIR/shared"
VENV_DIR="$BASE_DIR/venv"
BACKUP_DIR="$BASE_DIR/backups"

SERVICE_NAME="mcfr"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
NGINX_NAME="mcfr"
NGINX_AVAILABLE="/etc/nginx/sites-available/${NGINX_NAME}"
NGINX_ENABLED="/etc/nginx/sites-enabled/${NGINX_NAME}"
NGINX_DEFAULT="/etc/nginx/sites-enabled/default"

RUN_USER="mcfr"

DEFAULT_PORT=8000
DEFAULT_DOMAIN="pccode.cn"

# 保留多少份历史备份。
# ！这个数字敢开到 20，前提是**备份里不含模组镜像**（见「备份配置与数据」一步）。
#   含镜像时一份就是几个 GB，5 份就能把磁盘吃满；
#   排除之后一份只有几十 MB（数据库 + 头像 + 皮肤），20 份也就一两 GB，
#   换来一整年的回滚窗口，划算。
#   想改又不想动代码：MCFR_KEEP_BACKUPS=8 sudo bash deploy.sh
KEEP_BACKUPS="${MCFR_KEEP_BACKUPS:-20}"

# pip 源。留空则用默认（PyPI）。
# 服务器在国内的话建议用清华源 —— 见 README。
PIP_INDEX="${MCFR_PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"

# 脚本自己所在的目录。如果它正好就是项目根（把整个项目传上来的情况），
# 「准备发布包」一步会直接拿它现打一个 zip，你就不用先手工压缩再上传了。
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ！脚本可能因为「自保」被复制到 $BASE_DIR 再重新执行（见后面「开始」一节）。
#   重新执行之后 $SELF_DIR 就变成 $BASE_DIR 了，而那里通常没有 run.py，
#   于是「现打发布包」这一条会静默失效、退回去用旧的 mc.zip ——
#   用户看到的就是「代码明明改了，部署上去还是旧版」。
#   所以把最初的位置记下来，跨过那次 exec 传下去。
ORIG_SELF_DIR="${MCFR_ORIG_SELF:-$SELF_DIR}"

# -----------------------------------------------------------------------------
# 参数
# -----------------------------------------------------------------------------

ZIP_FILE=""                 # 留空 = 自动决定，见「准备发布包」一步
APP_PORT=""
DOMAIN=""
DO_NGINX=1
DO_DEPS=1
DO_INSTALL=1                # 缺的系统包自动 apt 装
DO_FIREWALL=1               # 自动配 ufw
DO_HTTPS=1                  # 自动申请证书
CERT_EMAIL=""
FROM_DIR=""                 # 自打包的源目录；留空就用脚本所在目录
SRC_DIR=""                  # 实际使用的源目录，运行时确定
IMPORT_FROM=""
ROLLBACK_TS=""
DRY_RUN=0

# 看门狗：装一个 systemd timer，每 2 分钟检查一次服务，不健康就自动拉起。
# ！用环境变量而不是命令行开关，是为了不动上面那段 --help 里的开关列表
#   （那段是 usage() 按行号 sed 出来的，多一行就会错位）。
#   不想要：MCFR_NO_WATCHDOG=1 sudo bash deploy.sh
DO_WATCHDOG=1
if [[ "${MCFR_NO_WATCHDOG:-}" == "1" ]]; then DO_WATCHDOG=0; fi

usage() {
    sed -n '3,30p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

#  ！必须在解析参数【之前】留一份副本。
#    下面那个 while 循环会把 $# 一路 shift 到 0，而脚本后面的「自保复制」
#    还要用 exec 重新执行自己 —— 那时 "$@" 已经是空的了。
#    后果：--base / --zip / --from-dir / --import-from / --dry-run 全丢，
#    脚本会朝着默认的 /root/mcfr 干下去（包括把 --dry-run 也丢掉，
#    于是「照着预览跑」变成了真部署）。踩过。
ORIG_ARGS=("$@")

while [[ $# -gt 0 ]]; do
    case "$1" in
        --zip)          ZIP_FILE="$2"; shift 2 ;;
        --port)         APP_PORT="$2"; shift 2 ;;
        --domain)       DOMAIN="$2"; shift 2 ;;
        --base)         BASE_DIR="$2"; APP_DIR="$BASE_DIR/mc"
                        SHARED_DIR="$BASE_DIR/shared"; VENV_DIR="$BASE_DIR/venv"
                        BACKUP_DIR="$BASE_DIR/backups"; shift 2 ;;
        --import-from)  IMPORT_FROM="$2"; shift 2 ;;
        --rollback)     ROLLBACK_TS="$2"; shift 2 ;;
        --from-dir)     FROM_DIR="$2"; shift 2 ;;
        --email)        CERT_EMAIL="$2"; shift 2 ;;
        --no-nginx)     DO_NGINX=0; shift ;;
        --no-install)   DO_INSTALL=0; shift ;;
        --no-firewall)  DO_FIREWALL=0; shift ;;
        --no-https)     DO_HTTPS=0; shift ;;
        --skip-deps)    DO_DEPS=0; shift ;;
        --dry-run)      DRY_RUN=1; shift ;;
        -h|--help)      usage ;;
        *)              echo "未知参数：$1（用 --help 看用法）" >&2; exit 2 ;;
    esac
done

# -----------------------------------------------------------------------------
# 输出helpers
# -----------------------------------------------------------------------------

if [[ -t 1 ]]; then
    C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'
    C_BLU=$'\033[36m'; C_DIM=$'\033[2m';  C_OFF=$'\033[0m'
else
    C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_DIM=""; C_OFF=""
fi

STEP_NO=0
TOTAL_STEPS=15
step() { STEP_NO=$((STEP_NO + 1)); printf '\n%s[%d/%d] %s%s\n' "$C_BLU" "$STEP_NO" "$TOTAL_STEPS" "$*" "$C_OFF"; }
info() { printf '      %s\n' "$*"; }
dim()  { printf '      %s%s%s\n' "$C_DIM" "$*" "$C_OFF"; }
ok()   { printf '      %s✓%s %s\n' "$C_GRN" "$C_OFF" "$*"; }
warn() { printf '      %s!%s %s\n' "$C_YEL" "$C_OFF" "$*" >&2; }
die()  { printf '\n%s✗ %s%s\n' "$C_RED" "$*" "$C_OFF" >&2; exit 1; }

run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '      %s[dry-run] %s%s\n' "$C_DIM" "$*" "$C_OFF"
        return 0
    fi
    "$@"
}

# -----------------------------------------------------------------------------
# 前置检查
# -----------------------------------------------------------------------------

require_root() {
    [[ ${EUID:-$(id -u)} -eq 0 ]] || die "需要管理员权限，请用：sudo bash $0"
}

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "缺少命令 $1，请先安装（apt install -y $2）"
}

# -----------------------------------------------------------------------------
# 读现有配置 —— 升级时不能把端口和域名悄悄改掉
# -----------------------------------------------------------------------------

detect_port() {
    [[ -n "$APP_PORT" ]] && { echo "$APP_PORT"; return; }
    if [[ -f "$SERVICE_FILE" ]]; then
        local p
        p=$(grep -oP 'run\.py.*--port[= ]\K[0-9]+' "$SERVICE_FILE" 2>/dev/null | head -1 || true)
        [[ -n "$p" ]] && { echo "$p"; return; }
    fi
    echo "$DEFAULT_PORT"
}

detect_domain() {
    [[ -n "$DOMAIN" ]] && { echo "$DOMAIN"; return; }
    if [[ -f "$NGINX_AVAILABLE" ]]; then
        local d
        d=$(grep -oP '^\s*server_name\s+\K[^;]+' "$NGINX_AVAILABLE" 2>/dev/null | head -1 || true)
        [[ -n "$d" ]] && { echo "$d"; return; }
    fi
    echo "$DEFAULT_DOMAIN"
}

# -----------------------------------------------------------------------------
# .env 读写
# -----------------------------------------------------------------------------

# 只在「缺失」或「存在但为空」时写入，已有值一律不覆盖。
# ！自动改别人的配置是危险动作，所以只做「补全」，不做「修改」。
ensure_env() {
    local key="$1" value="$2" file="$SHARED_DIR/.env"

    if [[ ! -f "$file" ]]; then
        printf '%s=%s\n' "$key" "$value" >>"$file"
        ok "新增 $key=$value"
        return
    fi

    if grep -q "^${key}=" "$file"; then
        local cur
        cur=$(grep -m1 "^${key}=" "$file" | cut -d= -f2-)
        if [[ -z "$cur" ]]; then
            run sed -i "s|^${key}=.*|${key}=${value}|" "$file"
            ok "$key 原本是空的，已填为 $value"
        else
            info "$key 已有值（$cur），不动"
        fi
    else
        printf '\n%s=%s\n' "$key" "$value" >>"$file"
        ok "追加 $key=$value"
    fi
}

# -----------------------------------------------------------------------------
# 目录穿越权限
# -----------------------------------------------------------------------------

# systemd 要**先用服务用户的身份** cd 进 WorkingDirectory，再启动程序。
# 所以 $RUN_USER 必须能「穿过」从 / 到工作目录的每一层。
#
# ！踩过的坑：/root 默认权限是 700，别的用户连穿过都不允许，于是服务
#   反复重启，日志只有一行：
#     Changing to the requested working directory failed: Permission denied
#     status=200/CHDIR
#   而 /root 正是本项目默认的部署位置。
#
# 只加 o+x（穿过），不加 o+r（列目录）：能进 /root/mcfr/mc 就够了，
# 没必要让人看得见 /root 下面还有什么别的东西。
TRAVERSAL_FIXED=()

ensure_traversal() {
    #  ！必须拆成两句。`local a="$1" b="$a"` 里的 $a 是在赋值【之前】展开的，
    #    取到的是外层的 a（不存在）→ 在 set -u 下直接 "unbound variable" 崩掉。
    local path="$1"
    local d="$path" step mode
    local -a chain=()

    while [[ "$d" != "/" && "$d" != "." && -n "$d" ]]; do
        chain+=("$d")
        d="$(dirname "$d")"
    done

    local i
    for (( i=${#chain[@]}-1; i>=0; i-- )); do
        step="${chain[$i]}"
        mode="$(stat -c '%A' "$step" 2>/dev/null || true)"
        # 目录还不存在（dry-run）或读不到属性，就跳过
        if [[ -z "$mode" ]]; then continue; fi
        # 最后一位是 other 的执行位；已经是 x 就不用动
        if [[ "${mode:9:1}" == "x" ]]; then continue; fi
        run chmod o+x "$step"
        TRAVERSAL_FIXED+=("$step")
    done
}

# -----------------------------------------------------------------------------
# 回滚
# -----------------------------------------------------------------------------

do_rollback() {
    local src="$BACKUP_DIR/$ROLLBACK_TS"
    [[ -d "$src" ]] || die "找不到备份目录：$src"

    step "从 $src 恢复"

    systemctl stop "$SERVICE_NAME" 2>/dev/null || true

    [[ -f "$src/.env" ]] && cp -a "$src/.env" "$SHARED_DIR/.env"

    if [[ -d "$src/data" ]]; then
        # ！备份里没有 data/mirror，而下面要把整个 data 删掉重建 ——
        #   不先把镜像挪出去，回滚一次就会把几个 GB 的模组删光。
        #   而且这个过程**完全静默**：站点照常打开、数据库也回来了，
        #   只有镜像列表变成一堆下不动的空壳，不特意去看根本发现不了。
        #
        #   ！必须挪到 $SHARED_DIR 而不是 $SHARED_DIR/data 里面：
        #     放 data 里面的话，下一行的 rm -rf 会连它一起带走。
        MIRROR_STASH=""
        if [[ -d "$SHARED_DIR/data/mirror" ]]; then
            MIRROR_STASH="$SHARED_DIR/.mirror-rollback"
            rm -rf "$MIRROR_STASH"
            mv "$SHARED_DIR/data/mirror" "$MIRROR_STASH"
        fi

        rm -rf "$SHARED_DIR/data"
        cp -a "$src/data" "$SHARED_DIR/data"

        if [[ -n "$MIRROR_STASH" && -d "$MIRROR_STASH" ]]; then
            rm -rf "$SHARED_DIR/data/mirror"
            mv "$MIRROR_STASH" "$SHARED_DIR/data/mirror"
            ok "模组镜像已原地保留（回滚不动它）"
        fi
    fi

    chown -R "$RUN_USER:$RUN_USER" "$SHARED_DIR" 2>/dev/null || true

    systemctl start "$SERVICE_NAME" 2>/dev/null || true

    ok "已回滚到 $ROLLBACK_TS"
    info "配置文件已恢复；程序代码需要你重新上传那一版的 mc.zip 再跑一次本脚本"
    exit 0
}

# =============================================================================
#  开始
# =============================================================================

require_root

# ！把自己挪到安全的地方再继续。
#   本脚本就住在项目根里，而项目根正是这次要被整个清空重建的目录 ——
#   继续在「随时会被删掉的文件」上跑，行为是未定义的。
#   先复制一份到 $BASE_DIR（那个目录不会被清），再从那里重新执行自己。
#   只在真的身处险境时才这么做：脚本在别处（比如已经搬过一次）就什么都不做。
case "$(readlink -f "$0" 2>/dev/null || echo "$0")" in
    "$APP_DIR"/*)
        mkdir -p "$BASE_DIR"
        cp -f "$0" "$BASE_DIR/deploy.sh"
        printf '      已将脚本复制到 %s/deploy.sh 并重新执行\n' "$BASE_DIR"
        # 把「我原来在哪」和原始参数一起带过去。
        # ！参数必须用 ORIG_ARGS：上面的解析循环已经把 "$@" shift 空了。
        exec env MCFR_ORIG_SELF="$SELF_DIR" bash "$BASE_DIR/deploy.sh" "${ORIG_ARGS[@]}"
        ;;
esac

if [[ -n "$ROLLBACK_TS" ]]; then
    do_rollback
fi

APP_PORT="$(detect_port)"
DOMAIN="$(detect_domain)"

cat <<BANNER

  mcfr 部署 / 升级脚本
  ────────────────────────────────────────────────
    程序目录 $APP_DIR
    数据目录 $SHARED_DIR
    监听     http://127.0.0.1:$APP_PORT
    域名     $DOMAIN
    备份保留 $KEEP_BACKUPS 份
    （发布包在第 2 步确定）
BANNER

[[ $DRY_RUN -eq 1 ]] && warn "dry-run 模式：只打印要做什么，不真的执行"

# 首次申请证书需要一个邮箱（Let 的 Encrypt 用来在证书快过期时提醒你）。
# 只在交互式终端里问，而且允许直接回车跳过 —— 不能因为没邮箱就卡住整个部署。
if [[ $DO_HTTPS -eq 1 && $DO_NGINX -eq 1 && -z "$CERT_EMAIL" && ! -d /etc/letsencrypt/accounts && -t 0 ]]; then
    printf '\n  申请 HTTPS 证书需要一个邮箱（用于到期提醒，直接回车可跳过）\n'
    read -rp '  邮箱: ' CERT_EMAIL || true
    [[ -n "$CERT_EMAIL" ]] || warn "没填邮箱，将跳过证书申请，网站先用 http 跑"
fi

# -----------------------------------------------------------------------------
step "安装系统依赖"
# -----------------------------------------------------------------------------

# ！这里全部用 if 而不是 `[[ … ]] && cmd` 的写法。
#   在 set -e 下，`A && B` 里如果 B 失败（且 B 是最后一个），整个列表返回非 0，
#   脚本会直接退出 —— “检查某个包在不在”这种天天失败几十次的判断，
#   一不小心就成了意外退出的触发器。
MISSING=()
add_missing() {  # add_missing <用于检测的命令> <要装的包…>
    command -v "$1" >/dev/null 2>&1 && return 0
    shift
    MISSING+=("$@")
}

add_missing python3 python3
if ! python3 -c 'import venv' >/dev/null 2>&1; then MISSING+=(python3-venv); fi
add_missing unzip   unzip
add_missing curl    curl
if [[ $DO_NGINX -eq 1 ]]; then add_missing nginx nginx; fi
if [[ $DO_HTTPS -eq 1 ]]; then add_missing certbot certbot python3-certbot-nginx; fi

if [[ ${#MISSING[@]} -eq 0 ]]; then
    dim "系统依赖齐全"
elif [[ $DO_INSTALL -eq 0 ]]; then
    warn "缺这些包，但 --no-install 指定了不装：${MISSING[*]}"
else
    info "缺这些包，准备安装：${MISSING[*]}"
    if command -v apt-get >/dev/null 2>&1; then
        run apt-get update -qq
        run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${MISSING[@]}"
        ok "系统依赖安装完成"
    else
        die "这台机器不是 apt 系（Debian / Ubuntu），请先手工装：${MISSING[*]}"
    fi
fi

# 装完再核对一遍。缺了就当场说清楚，别拖到后面才莫名其妙地崩。
need_cmd python3 "python3"
need_cmd unzip   "unzip"

if ! command -v nginx >/dev/null 2>&1; then
    DO_NGINX=0
    warn "nginx 不可用，跳过反向代理配置"
fi
if ! command -v curl >/dev/null 2>&1; then
    warn "curl 不可用，稍后跳过健康检查"
fi
if [[ $DO_HTTPS -eq 1 ]] && ! command -v certbot >/dev/null 2>&1; then
    DO_HTTPS=0
    warn "certbot 不可用，跳过 HTTPS"
fi

info "Python: $(python3 --version 2>&1)"

# 运行用户
if ! id -u "$RUN_USER" >/dev/null 2>&1; then
    info "创建系统用户 $RUN_USER"
    run useradd --system --home-dir "$BASE_DIR" --shell /usr/sbin/nologin "$RUN_USER"
else
    info "系统用户 $RUN_USER 已存在"
fi

# 目录骨架。mkdir -p 幂等且无副作用，所以不包在 run() 里 ——
# 下面那个临时解压目录就建在 $BASE_DIR 下，依赖它先存在。
mkdir -p "$BASE_DIR" "$SHARED_DIR" "$BACKUP_DIR"

# -----------------------------------------------------------------------------
step "准备发布包"
# -----------------------------------------------------------------------------

# 发布包从哪来，规则就三条，按顺序判断：
#   1. --zip 指定了     → 用它
#   2. 脚本所在目录是项目 → 现打一个（把整个项目传上来直接跑，最常用）
#   3. 上面都不满足     → 退回去找 $BASE_DIR/mc.zip
if [[ -n "$ZIP_FILE" && -f "$ZIP_FILE" ]]; then
    info "使用指定的发布包：$ZIP_FILE"
elif [[ -n "$FROM_DIR" ]]; then
    SRC_DIR="$FROM_DIR"
elif [[ -f "$ORIG_SELF_DIR/run.py" ]]; then
    SRC_DIR="$ORIG_SELF_DIR"
elif [[ -f "$SELF_DIR/run.py" ]]; then
    SRC_DIR="$SELF_DIR"
elif [[ -f "$BASE_DIR/mc.zip" ]]; then
    ZIP_FILE="$BASE_DIR/mc.zip"
    info "使用已上传的发布包：$ZIP_FILE"
    # ！这是「代码改了但没换上」的头号原因：用的是之前上传的那个 zip。
    warn "！用的是 $BASE_DIR/mc.zip，不是当前目录里的代码。"
    warn "  它上次更新于：$(stat -c '%y' "$ZIP_FILE" 2>/dev/null | cut -d. -f1 || echo '（读不到）')"
    warn "  代码改过就必须重新上传它，否则部署上去的还是旧版。"
fi

if [[ -n "$SRC_DIR" ]]; then
    [[ -f "$SRC_DIR/run.py" ]] || die "$SRC_DIR 里没有 run.py，看起来不是项目根目录"
    SRC_DIR="$(cd "$SRC_DIR" && pwd)"
    ZIP_FILE="$BASE_DIR/mc.zip"
    info "从 $SRC_DIR 现打发布包"
    # ！这一步在 dry-run 里也照做，没包在 run() 里。
    #   它只是在本地生成一个 zip，不碰系统、不改配置，没有任何副作用；
    #   而后面「解压并校验」必须真看到内容才有意义 —— 跳过它的话，
    #   dry-run 会在下一步报「找不到发布包」，白白吓人一跳。
    python3 - "$SRC_DIR" "$ZIP_FILE" <<'PACK'
import os, sys, zipfile

src, out = sys.argv[1], sys.argv[2]
# ！`data` 和 `mirror` 必须在里面：模组镜像（data/mirror）动辄几个 GB，
#   一旦打进发布包，zip 会变成几百 MB —— 每次升级都要重传一遍，
#   而它本来就是能从 Modrinth 重新下载的东西，不该跟着代码走。
#   `backups` 同理。加 `mirror` 是以防布局变动（正常它就在 data 里面，
#   已经跟着 data 一起被跳过了）。
SKIP_DIRS  = {".venv", "venv", "__pycache__", ".git", ".idea", ".vscode",
              "data", "mirror", "backups", "node_modules", ".stage"}
SKIP_FILES = {".env", "server.log"}
#  .zip 也要排除：项目目录里常常躺着一个旧的 mc.zip，
#  打进去会套娃，而且让人误以为「包里多带了新东西」。
SKIP_SUFFIX = (".pyc", ".pyo", ".log", ".bat", ".zip")

if os.path.exists(out):
    os.remove(out)

count = 0
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs
                   if d not in SKIP_DIRS and not d.startswith(".stage")]
        for name in files:
            if name in SKIP_FILES or name.endswith(SKIP_SUFFIX):
                continue
            # ！`_` 开头的是开发时的临时脚本（_mkip.py / _syntax.py 这类），
            #   平时用完就自己删掉，但打包的那一刻它可能正好还在 ——
            #   于是被顺手打进发布包、送到服务器上。这次就中过一次。
            #   只排「单个下划线」：backend/__init__.py 和
            #   backend/routers/__init__.py 是双下划线，必须照常打包。
            if name.startswith("_") and not name.startswith("__"):
                continue
            full = os.path.join(root, name)
            z.write(full, os.path.relpath(full, src))
            count += 1
print(f"      {count} 个文件已打包")
PACK
    ok "打包完成：$ZIP_FILE"
fi

if [[ -z "$ZIP_FILE" || ! -f "$ZIP_FILE" ]]; then
    die "没有可用的发布包。两种用法：
        1) 把整个项目传上来（含 deploy.sh），在本目录直接跑：sudo bash deploy.sh
        2) 只传一个 zip：scp mc.zip root@服务器IP:$BASE_DIR/  然后加 --zip 指定"
fi
[[ -s "$ZIP_FILE" ]] || die "发布包是空的：$ZIP_FILE"

# -----------------------------------------------------------------------------
step "解压发布包并校验（先校验，后动手删）"
# -----------------------------------------------------------------------------

# ！顺序很重要：先在临时目录解压并确认里面确实是个能跑的项目，
#   确认无误之后才去删现有的程序目录。反过来做的话，
#   一个损坏的 zip 会把站点删没了却补不回来。
STAGE="$(mktemp -d "$BASE_DIR/.stage.XXXXXX")"
cleanup_stage() { [[ -d "${STAGE:-}" ]] && rm -rf "$STAGE"; }
trap cleanup_stage EXIT

# ！这一步不包在 run() 里：解压到临时目录没有任何副作用，
#   而它后面的「校验」必须看到真实内容才有意义。
#   把它跳掉的话，dry-run 会一路报到「找不到 run.py」，白白吓人一跳。
unzip -q "$ZIP_FILE" -d "$STAGE" || die "解压失败，发布包可能已损坏：$ZIP_FILE"

# 自动识别项目根：zip 可能平铺，也可能多套了一层目录（比如导出的仓库名）
PROJECT_ROOT=""
if [[ -f "$STAGE/run.py" ]]; then
    PROJECT_ROOT="$STAGE"
else
    # 往下找两层，取第一个含 run.py 的目录
    while IFS= read -r candidate; do
        PROJECT_ROOT="$candidate"
        break
    done < <(find "$STAGE" -maxdepth 3 -name run.py -printf '%h\n' 2>/dev/null | sort)
fi

[[ -n "$PROJECT_ROOT" ]] || die "压缩包里找不到 run.py，这不像是一个 mcfr 发布包
      包内实际内容：
$(find "$STAGE" -maxdepth 2 -mindepth 1 | head -20 | sed 's/^/        /')"

# ！注意这里不能写成 ${PROJECT_ROOT#$STAGE/}：
#   如果 run.py 就在包根，PROJECT_ROOT 正好等于 $STAGE（结尾没有斜杠），
#   而带斜杠的模式匹配不上，会原样把内部临时路径打印出来。
REL_ROOT="${PROJECT_ROOT#"$STAGE"}"
REL_ROOT="${REL_ROOT#/}"
info "识别到项目根：${REL_ROOT:-（包根，没有外层目录）}"
info "          入口：run.py"
info "        依赖表：$([[ -f "$PROJECT_ROOT/requirements.txt" ]] && echo requirements.txt || echo '（缺失！）')"
info "        后端目录：$([[ -d "$PROJECT_ROOT/backend" ]] && echo backend/ || echo '（缺失！）')"
info "        前端目录：$([[ -d "$PROJECT_ROOT/frontend" ]] && echo frontend/ || echo '（缺失！）')"

#  ！把「包里到底是哪一版」打出来 —— 这是排查「代码没换上」最快的依据。
#   包的时间和版本一眼就能看出是不是旧的。
PKG_VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$PROJECT_ROOT/backend/__init__.py" 2>/dev/null || true)"
PKG_FILES="$(find "$PROJECT_ROOT" -type f | wc -l | tr -d ' ')"
info "        包内版本：${PKG_VERSION:-（读不到）}"
info "        包内文件：$PKG_FILES 个"
info "        包的来源：$ZIP_FILE"
info "        包的更新：$(stat -c '%y' "$ZIP_FILE" 2>/dev/null | cut -d. -f1 || echo '（读不到）')"

[[ -f "$PROJECT_ROOT/requirements.txt" ]] || die "发布包里没有 requirements.txt"
[[ -d "$PROJECT_ROOT/backend" ]] || die "发布包里没有 backend/ 目录"
[[ -d "$PROJECT_ROOT/frontend" ]] || die "发布包里没有 frontend/ 目录"

ok "发布包结构校验通过"

# -----------------------------------------------------------------------------
step "停止服务"
# -----------------------------------------------------------------------------

if systemctl list-unit-files "${SERVICE_NAME}.service" >/dev/null 2>&1; then
    run systemctl stop "$SERVICE_NAME" || true
    ok "已停止 $SERVICE_NAME"
else
    dim "服务还没装过，跳过"
fi

# 确保没有残留的手工进程占着端口
if pgrep -f "run.py.*--port" >/dev/null 2>&1; then
    warn "检测到手工启动的 run.py 进程，一并结束"
    run pkill -f "run.py.*--port" || true
    sleep 1
fi

# -----------------------------------------------------------------------------
step "备份配置与数据"
# -----------------------------------------------------------------------------

STAMP="$(date +%Y%m%d-%H%M%S)"
BK="$BACKUP_DIR/$STAMP"
run mkdir -p "$BK"

# ！服务已经停了才备份 —— SQLite 开的是 WAL，热拷贝有可能拿到撕裂的状态。

# 备份 data/，但**跳过模组镜像**。
# 这一步以前就是一句 `cp -a data`，而 data/mirror 是下载下来的模组文件，
# 几个 GB 起步；每升级一次就整个复制一份，几分钟就没了。
# 而镜像内容随时能从 Modrinth 重新拉，丢了也不心疼 ——
# 真正找不回来的只有数据库、密钥和上传的图片，那些照常进备份。
#
# 用 tar 而不是「cp -a 再删」：后者会先把几个 GB 完整复制过去，
# 虽然最后删掉了，但磁盘峰值和那段时间已经付出去了。
# tar 的 --exclude 是流式的，根本不读那些文件。
# （tar 在 Debian / Ubuntu 上必定存在，不用额外装东西。）
backup_data() {
    tar -C "$SHARED_DIR" \
        --exclude='data/mirror' \
        --exclude='data/mirror/*' \
        --exclude='data/backups' \
        --exclude='data/backups/*' \
        -cf - data | tar -C "$BK" -xf -
}

if [[ -d "$SHARED_DIR" ]]; then
    [[ -f "$SHARED_DIR/.env" ]] && run cp -a "$SHARED_DIR/.env" "$BK/.env" 2>/dev/null || true
    # 没写成 `run backup_data` 只是因为 dry-run 时想把「跳过了什么」说清楚，
    # 不是写不了 —— run 会把整个函数当一条命令执行，那样也是对的。
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '      %s[dry-run] tar 备份 data/（跳过 mirror 与 backups）%s\n' "$C_DIM" "$C_OFF"
    elif [[ -d "$SHARED_DIR/data" ]]; then
        backup_data
    fi
fi

[[ -f "$SERVICE_FILE" ]]     && run cp -a "$SERVICE_FILE"     "$BK/mcfr.service"   2>/dev/null || true
[[ -f "$NGINX_AVAILABLE" ]]  && run cp -a "$NGINX_AVAILABLE"  "$BK/nginx-site.conf" 2>/dev/null || true

if [[ -f "$BK/data/mcfr.db" ]]; then
    ok "备份完成：$BK"
    info "  数据库 $(stat -c%s "$BK/data/mcfr.db" 2>/dev/null || echo '?') 字节"
    info "  整包 $(du -sh "$BK" 2>/dev/null | cut -f1 || echo '?')（不含模组镜像）"
else
    warn "备份目录里没有数据库 —— 这可能是第一次部署"
    ok "备份目录：$BK"
fi

# 清理过老的备份：只留最新的 KEEP_BACKUPS 份。
# 目录名是 YYYYmmdd-HHMMSS，所以 sort -r 拿到的就是「新的排在前面」，
# tail -n +N 跳掉前 N-1 行，剩下的就是该删的。
if [[ -d "$BACKUP_DIR" ]]; then
    KEPT_BEFORE="$(find "$BACKUP_DIR" -maxdepth 1 -mindepth 1 -type d | wc -l | tr -d ' ')"
    mapfile -t olds < <(find "$BACKUP_DIR" -maxdepth 1 -mindepth 1 -type d -printf '%f\n' | sort -r | tail -n +$((KEEP_BACKUPS + 1)))
    if [[ ${#olds[@]} -gt 0 ]]; then
        for old in "${olds[@]}"; do
            [[ -n "$old" ]] || continue
            run rm -rf "$BACKUP_DIR/$old"
            dim "清理旧备份 $old"
        done
        info "备份保留最新 $KEEP_BACKUPS 份（原有 $KEPT_BEFORE 份，删掉 ${#olds[@]} 份）"
    else
        dim "备份共 $KEPT_BEFORE 份，未超过 $KEEP_BACKUPS 份上限"
    fi
fi

# -----------------------------------------------------------------------------
step "安装程序文件"
# -----------------------------------------------------------------------------

# ！这里直接清空 mc/。之所以是安全的，全靠 data 和 .env 都在 mc/ 外面。
#   以后加新的持久化目录，请放到 shared/ 并在这里加一条软链接，
#   不要往 mc/ 里塞需要保留的东西。
run rm -rf "$APP_DIR"
run mkdir -p "$APP_DIR"

# 让服务用户能穿到工作目录。放在启动服务之前就行，这里最自然。
ensure_traversal "$APP_DIR"
if [[ ${#TRAVERSAL_FIXED[@]} -gt 0 ]]; then
    ok "已开放目录穿越权限：${TRAVERSAL_FIXED[*]}"
    if [[ " ${TRAVERSAL_FIXED[*]} " == *" /root "* ]]; then
        warn "为把 /root 的权限改成了 701（原本是 700）"
        warn "这是让服务能跑起来的必要条件，但 /root 毕竟是 root 的家目录。"
        warn "更干净的做法是把站点放到 /srv/mcfr：sudo bash $0 --base /srv/mcfr"
    fi
fi

# 搬运（连同 dotfiles）
shopt -s dotglob nullglob
run cp -a "$PROJECT_ROOT"/* "$APP_DIR"/
shopt -u dotglob nullglob

# 发布包里不该带这些 —— 带了会让新环境跑着本地开发时的状态
for junk in data .env .venv venv __pycache__ .git .idea .vscode; do
    if [[ -e "$APP_DIR/$junk" ]]; then
        run rm -rf "$APP_DIR/$junk"
        dim "剔除发布包里的 $junk"
    fi
done

# 建软链接：状态留在外面，程序目录可以随时整个删
run mkdir -p "$SHARED_DIR/data"
run ln -sfn "$SHARED_DIR/.env"  "$APP_DIR/.env"
run ln -sfn "$SHARED_DIR/data"  "$APP_DIR/data"

# ！真的换上了吗？数一数。发布包里的文件数和装完之后的文件数应该一致；
#   对不上就说明 cp 被打断 / 有东西没搬进来 —— 而这正是「看着像成功了，
#   但跑的还是旧代码」最容易发生的地方。（.env 和 data 是软链接，不计入 -type f）
if [[ $DRY_RUN -eq 1 ]]; then
    dim "dry-run：跳过文件数校验"
else
    INS_FILES="$(find "$APP_DIR" -type f | wc -l | tr -d ' ')"
    if [[ "$INS_FILES" == "$PKG_FILES" ]]; then
        ok "文件已全部替换：$INS_FILES 个"
    else
        warn "！装进去的文件数对不上：包里 $PKG_FILES 个，实到 $INS_FILES 个"
        warn "  先重跑一次脚本；仍然对不上就把这一行发出来。"
    fi
fi

ok "程序已就位：$APP_DIR"
info "   .env -> $SHARED_DIR/.env"
info "   data -> $SHARED_DIR/data"

# -----------------------------------------------------------------------------
step "准备配置"
# -----------------------------------------------------------------------------

# 从旧安装目录搬过来（换目录结构时用一次）
if [[ -n "$IMPORT_FROM" ]]; then
    if [[ ! -f "$SHARED_DIR/.env" && -f "$IMPORT_FROM/.env" ]]; then
        run cp -a "$IMPORT_FROM/.env" "$SHARED_DIR/.env"
        ok "从 $IMPORT_FROM 导入 .env"
    fi
    if [[ ! -f "$SHARED_DIR/data/mcfr.db" && -f "$IMPORT_FROM/data/mcfr.db" ]]; then
        run cp -a "$IMPORT_FROM/data/." "$SHARED_DIR/data/"
        ok "从 $IMPORT_FROM 导入 data/"
    fi
fi

# ！踩过的坑：.env 里装的不只是密码，还有签名密钥这类「丢了只能重弄一份」
#   的东西（v0.21 起已经不再有邮箱授权码了）。而它是【静默】丢的 ——
#   脚本一路绿灯，用户直到注册时才发现不对。
#   所以这里主动去几个常见的旧安装位置找一找，找到就直接沿用配置文件。
#   只搬 .env（配置），不搬数据库 —— 数据库要不要沿用是更大的决定，
#   仍然由 --import-from 显式指定。
LEGACY_ENV=""
if [[ -z "$IMPORT_FROM" && ! -f "$SHARED_DIR/.env" ]]; then
    for cand in /srv/mcfr /opt/mcfr /var/www/mcfr; do
        [[ -f "$cand/.env" ]] || continue
        [[ "$(readlink -f "$cand" 2>/dev/null || true)" == "$BASE_DIR" ]] && continue
        LEGACY_ENV="$cand/.env"
        break
    done
fi

if [[ -n "$LEGACY_ENV" ]]; then
    run cp -a "$LEGACY_ENV" "$SHARED_DIR/.env"
    ok "检测到旧安装的配置文件，已沿用：$LEGACY_ENV"
    warn "如果不想要旧配置，删掉 $SHARED_DIR/.env 再跑一次即可"
fi

# 全新部署：从模板生成
if [[ ! -f "$SHARED_DIR/.env" ]]; then
    if [[ -f "$APP_DIR/.env.example" ]]; then
        run cp -a "$APP_DIR/.env.example" "$SHARED_DIR/.env"
        ok "已从 .env.example 生成 $SHARED_DIR/.env"
        warn "！这是一份【全新】配置，之前的所有设置都不会自动生效："
        warn "    MCFR_ADMIN_PASSWORD  后台口令 —— 留空就行，程序首次启动会自己生成一个"
        warn "                         随机口令，并在下面「后台口令」那一行展示出来"
        warn "    MCFR_CODE_SECRET     验证码签名密钥 —— 同样会自动生成"
        warn "  旧配置如果还在旧安装目录里，可以搬过来："
        warn "      sudo bash $0 --import-from /旧的/安装/目录"
    else
        run touch "$SHARED_DIR/.env"
        warn "发布包里没有 .env.example，建了个空的 $SHARED_DIR/.env"
    fi
fi

# 旧数据库没搬也要说一声 —— 「网站能打开」很容易让人误以为数据都在
if [[ -z "$IMPORT_FROM" && ! -f "$SHARED_DIR/data/mcfr.db" ]]; then
    for cand in /srv/mcfr /opt/mcfr /var/www/mcfr; do
        if [[ -f "$cand/data/mcfr.db" ]]; then
            warn "！发现旧数据库 $cand/data/mcfr.db 没有被沿用，当前站点是空库。"
            warn "  要把账号和上传文件搬过来：sudo bash $0 --import-from $cand"
            break
        fi
    done
fi

# 反向代理后面的真实 IP。这是部署时最容易漏、后果又最严重的一项：
#   不配 → 所有请求的来源 IP 都是 127.0.0.1 → 按 IP 的限流变成全站共享，
#          一个人试错 5 次管理员密码，所有人一小时内进不去后台。
#   配成 * → 谁都能伪造 X-Forwarded-For，限流等于没有。
if [[ $DO_NGINX -eq 1 ]]; then
    ensure_env "MCFR_TRUSTED_PROXIES" "127.0.0.1"
else
    dim "未启用 nginx，跳过 MCFR_TRUSTED_PROXIES"
fi

run chown -R "$RUN_USER:$RUN_USER" "$SHARED_DIR"
run chmod 600 "$SHARED_DIR/.env"

# ！把 data/ 收成 700。
#   它装着 mcfr.db（口令哈希 + 未成年人的联系方式）、server_secret.key
#   和上传的图片，按默认 umask 建出来是 644。
#   而本脚本为了能让服务跑起来，刚刚给 /root 加上了 o+x（可穿过）——
#   两条叠在一起，本机任何账号都能一路读走整个数据库。
#   700 之后只有服务用户和 root 进得去，而 nginx 不靠它工作（静态文件在 mc/frontend）。
run chmod 700 "$SHARED_DIR/data"

# -----------------------------------------------------------------------------
step "安装 Python 依赖"
# -----------------------------------------------------------------------------

if [[ $DO_DEPS -eq 0 ]]; then
    dim "--skip-deps：跳过"
else
    if [[ ! -x "$VENV_DIR/bin/python" ]]; then
        info "创建虚拟环境 $VENV_DIR"
        run python3 -m venv "$VENV_DIR"
    else
        info "复用已有的虚拟环境"
    fi

    run "$VENV_DIR/bin/python" -m pip install --upgrade pip -q

    if [[ -n "$PIP_INDEX" ]]; then
        info "pip 源：$PIP_INDEX"
        run "$VENV_DIR/bin/python" -m pip install -q -r "$APP_DIR/requirements.txt" -i "$PIP_INDEX"
    else
        run "$VENV_DIR/bin/python" -m pip install -q -r "$APP_DIR/requirements.txt"
    fi

    ok "依赖就绪"
fi

run chown -R "$RUN_USER:$RUN_USER" "$VENV_DIR" 2>/dev/null || true

# -----------------------------------------------------------------------------
step "配置系统服务"
# -----------------------------------------------------------------------------

# ！这里刻意没有开 ProtectHome。
#   systemd 的 ProtectHome=true 会把 /root 整个藏起来，
#   而这个项目按约定就装在 /root/mcfr 下，开了服务会直接起不来。
#   想用上这条加固，把 --base 换到 /srv/mcfr 之类的路径，再自行加上。
run tee "$SERVICE_FILE" >/dev/null <<UNIT
[Unit]
Description=mcfr · Minecraft 同好匹配
After=network-online.target
Wants=network-online.target

# ！关掉 systemd 的「启动频率限制」。
#
#   默认规则是：10 秒内启动失败 5 次，就认定这个单元“起不来了”，
#   从此**永久放弃重试** —— 直到有人手工 systemctl start（或 reset-failed）。
#
#   后果很隐蔽：服务在凌晨因为某个原因崩了一次、连着崩了几次，
#   之后整晚都是死的，而 Restart=always 明明写着“总是重启”。
#   用户看到的就是「昨晚好好的，早上起来打不开，重跑一遍部署脚本又好了」
#   —— 因为部署脚本里的 systemctl start 正好把这个状态清掉了。
#
#   置 0 = 不限制。代价是最坏情况下每 RestartSec 秒重试一次（很便宜），
#   换来的是「任何一次崩溃都能自己爬起来」。
StartLimitIntervalSec=0

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$VENV_DIR/bin/python run.py --host 127.0.0.1 --port $APP_PORT
Restart=always
RestartSec=3
SyslogIdentifier=$SERVICE_NAME

# 通用加固。ProtectHome 见上文说明，故意没开。
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
UNIT

run systemctl daemon-reload
run systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true

ok "已写入 $SERVICE_FILE"
# -----------------------------------------------------------------------------
step "配置反向代理"
# -----------------------------------------------------------------------------

if [[ $DO_NGINX -eq 0 ]]; then
    dim "跳过 nginx"
else
    run tee "$NGINX_AVAILABLE" >/dev/null <<NGINX
# 由 deploy.sh 自动生成，手改会被下次升级覆盖。
server {
    listen 80;
    server_name $DOMAIN;

    # 皮肤上传够用了
    client_max_body_size 2m;

    location / {
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # 「导入数据库」要传一个完整的 .db（程序里卡 64MB）。
    # ！必须单独开一个 location，不能把上面那个 2m 一起抬到 64m ——
    #   那等于给每个接口都开放 64MB 的请求体，白送一个填磁盘的入口。
    location = /api/admin/system/import {
        client_max_body_size 64m;
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # 帖子配图。程序里卡 4MB，前端会一路缩到这个数以内 ——
    # ！不能靠上面那个 2m：手机拍的照片缩完仍在 2-3MB，会被 nginx 拦在
    #   门口，而报错是 HTML 的 413 页面，前端只能显示成一句莫名其妙的
    #   「上传失败」，根本看不出是体积问题。
    location ^~ /api/posts/media {
        client_max_body_size 6m;
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # 帖子附件（存档、材质包、地图……）。程序里卡 32MB，这里给 40m 余量。
    location ^~ /api/posts/attachment {
        client_max_body_size 40m;
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
NGINX

    run ln -sfn "$NGINX_AVAILABLE" "$NGINX_ENABLED"

    # 默认站点会让「用 IP 访问」显示 nginx 欢迎页，
    # 看起来像服务没起来，徒增误会。只跑一个站的话删掉更清楚。
    if [[ -e "$NGINX_DEFAULT" ]]; then
        run rm -f "$NGINX_DEFAULT"
        dim "已移除 nginx 默认站点"
    fi

    if ! run nginx -t 2>/dev/null; then
        die "nginx 配置校验失败，看上面的报错。
      你现有的配置已经备份在 $BK/nginx-site.conf
      恢复：cp $BK/nginx-site.conf $NGINX_AVAILABLE && nginx -t && systemctl reload nginx"
    fi

    if ! run systemctl reload nginx 2>/dev/null; then
        run systemctl restart nginx
    fi
    ok "已配置 $DOMAIN -> 127.0.0.1:$APP_PORT"
fi

# -----------------------------------------------------------------------------
step "配置防火墙"
# -----------------------------------------------------------------------------

if [[ $DO_FIREWALL -eq 0 ]]; then
    dim "--no-firewall：跳过"
elif ! command -v ufw >/dev/null 2>&1; then
    dim "没装 ufw，跳过"
else
    # ！顺序不能反：先放行 SSH 再 enable，
    #   否则一条 ufw enable 就把自己关在门外了。
    if ! run ufw allow OpenSSH; then warn "OpenSSH 规则没加上，请手工确认，否则可能断连"; fi
    if ! run ufw allow 80/tcp;  then warn "80 端口规则没加上"; fi
    if ! run ufw allow 443/tcp; then warn "443 端口规则没加上"; fi
    if ! run ufw --force enable; then warn "ufw enable 失败"; fi
    ok "ufw 已放行 22 / 80 / 443"
fi

warn "云服务器（阿里云 / 腾讯云）还有一层安全组，必须去控制台也放行 80 和 443"
warn "系统防火墙开了、安全组没开的话外面照样连不上 —— 这个坑已经踩过一次"

# -----------------------------------------------------------------------------
step "启动并自检"
# -----------------------------------------------------------------------------

run systemctl reset-failed "$SERVICE_NAME" >/dev/null 2>&1 || true
run systemctl start "$SERVICE_NAME"
sleep 2

if [[ $DRY_RUN -eq 1 ]]; then
    dim "dry-run：跳过自检"
    exit 0
fi

HEALTHY=0
if command -v curl >/dev/null 2>&1; then
    for _ in $(seq 1 20); do
        if curl -fsS "http://127.0.0.1:$APP_PORT/api/meta" -o /dev/null 2>/dev/null; then
            HEALTHY=1
            break
        fi
        sleep 1
    done
else
    HEALTHY=1  # 没 curl 就不判断了，别误报
fi

if [[ $HEALTHY -eq 1 ]]; then
    ok "服务已启动，接口响应正常"
else
    printf '\n%s✗ 服务起来了但接口没响应，最近 30 行日志：%s\n' "$C_RED" "$C_OFF" >&2
    SVC_LOG="$(journalctl -u "$SERVICE_NAME" -n 30 --no-pager 2>/dev/null || true)"
    printf '%s\n' "$SVC_LOG" >&2

    # 把两种「看了日志也不知道怎么办」的情况直接翻译成命令。
    # 这两种都属于配置问题，不是程序 bug，所以值得单独说。
    if [[ "$SVC_LOG" == *"status=200/CHDIR"* ]]; then
        printf '\n%s工作目录进不去（CHDIR Permission denied）。%s\n' "$C_YEL" "$C_OFF" >&2
        printf '  服务用户 %s 穿不过 %s 上面某一层目录。多半是 /root 的权限：\n' "$RUN_USER" "$APP_DIR" >&2
        printf '      sudo chmod o+x /root && sudo systemctl restart %s\n\n' "$SERVICE_NAME" >&2
    elif [[ "$SVC_LOG" == *"status=203/EXEC"* ]]; then
        printf '\n%s找不到解释器（EXEC）。%s\n' "$C_YEL" "$C_OFF" >&2
        printf '  虚拟环境可能没建好，看看：ls -l %s/bin/python\n\n' "$VENV_DIR" >&2
    elif [[ "$SVC_LOG" == *"address already in use"* ]]; then
        printf '\n%s端口被占了。%s\n' "$C_YEL" "$C_OFF" >&2
        printf '  查是谁：ss -lptn "sport = :%s"\n\n' "$APP_PORT" >&2
    fi

    printf '  回滚：sudo bash %s --rollback %s\n' "$0" "$STAMP" >&2
    exit 1
fi

# -----------------------------------------------------------------------------
step "申请 HTTPS 证书"
# -----------------------------------------------------------------------------

if [[ $DO_HTTPS -eq 0 ]]; then
    dim "已跳过（--no-https，或 certbot 不可用）"
elif [[ $DO_NGINX -eq 0 || ! -f "$NGINX_AVAILABLE" ]]; then
    dim "没有 nginx 站点可挂，跳过"
elif [[ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]]; then
    # ！这里【不能】只跑 `certbot renew` —— 踩过的坑，后果很隐蔽。
    #   第 10 步刚把 nginx 站点配置整个重写成纯 HTTP（没有 listen 443），
    #   所以证书必须重新挂回去。而 `certbot renew` 只处理「快到期」的证书，
    #   没到期就什么都不做 → 证书文件还在，443 却没人监听。
    #   更糟的是脚本一路显示成功、结尾还提示你访问 https，实际只会超时。
    #   （不带 --force-renewal 就是「没到期则重新安装已有证书」，不会重复签发，
    #     Let’s Encrypt 的速率限制也不会被触发。）
    info "证书已存在，重新挂到 nginx 上"
    if run certbot --nginx -d "$DOMAIN" --non-interactive --redirect 2>/dev/null; then
        ok "HTTPS 已重新启用：https://$DOMAIN"
    elif run certbot install --nginx -d "$DOMAIN" --cert-name "$DOMAIN" --non-interactive 2>/dev/null; then
        ok "HTTPS 已重新启用：https://$DOMAIN"
    else
        warn "！证书挂不上去。文件在 /etc/letsencrypt/live/$DOMAIN/ 但 nginx 没在用它，"
        warn "  也就是说 https 打不开。手工修："
        warn "      sudo certbot --nginx -d $DOMAIN --non-interactive --redirect"
    fi

    # 顺带处理真的快到期的情况。失败了也不要紧 —— 上面已经把证书挂好了，
    # 而且系统里的 certbot 定时任务之后会自动重试。
    run certbot renew --quiet 2>/dev/null || true
elif [[ ! -d /etc/letsencrypt/accounts && -z "$CERT_EMAIL" ]]; then
    warn "首次申请证书需要邮箱，已跳过。补上：sudo bash $0 --email 你的邮箱"
else
    info "向 Let’s Encrypt 申请 $DOMAIN 的证书"
    CERT_OPTS=""
    if [[ ! -d /etc/letsencrypt/accounts ]]; then
        CERT_OPTS="--agree-tos --email $CERT_EMAIL"
    fi

    # ！证书申请失败不能算部署失败 —— 站点用 http 照样能跑。
    #   菜鸟最容易在这里放弃，其实只是 DNS 还没生效。
    if run certbot --nginx -d "$DOMAIN" --non-interactive --redirect $CERT_OPTS; then
        ok "HTTPS 已启用：https://$DOMAIN"
    else
        warn "证书申请失败，网站仍然可以用 http://$DOMAIN 访问"
        warn "多半是：DNS 还没解析到这台机器，或者云安全组没放行 80"
        warn "处理好之后重跑一次本脚本即可"
    fi
fi

# -----------------------------------------------------------------------------
step "端到端自检"
# -----------------------------------------------------------------------------

# ！前面 [12/15] 只证明了「本机 127.0.0.1:端口 上有个进程在答」。
#   可站点是挂在 nginx 后面的，「服务活着但外面进不来」才是常态：
#   443 没人监听、有第二个 server 块在抢同一个域名、云安全组没放行。
#   这些情况下 [12/14] 一律绿灯，用户打开网页却是超时/白屏，
#   而脚本「没报错」—— 于是根本不知道往哪查。所以这里从 nginx 那层再验一遍。
NGINX_HTTP_CODE=""
NGINX_HTTPS_OK=0
LISTEN_443=0

if [[ $DO_NGINX -eq 0 ]]; then
    dim "没走 nginx，跳过"
else
    if systemctl is-active --quiet nginx 2>/dev/null; then
        ok "nginx 运行中"
    else
        warn "！nginx 没在运行 —— 外面肯定连不上，先看：systemctl status nginx"
    fi

    # 带上 Host 头直接打本机 80 端口，绕开 DNS。能过就说明 nginx 那一层是通的。
    NGINX_HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' -m 8 \
        -H "Host: $DOMAIN" "http://127.0.0.1/api/meta" 2>/dev/null || true)"
    if [[ "$NGINX_HTTP_CODE" == "200" ]]; then
        ok "nginx 反代正常（http://$DOMAIN）"
    else
        warn "！过 nginx 打本机没拿到 200，实际是「${NGINX_HTTP_CODE:-无响应}」"
        warn "  常见原因是有两个 server 块都声明了 $DOMAIN，nginx 只用先加载的那个。"
        warn "  看看是谁在抢："
        warn "      grep -rl '$DOMAIN' /etc/nginx/sites-enabled/ /etc/nginx/conf.d/ 2>/dev/null"
    fi

    # 443 到底有没有进程在听。这是「昨天还能用 https，今天打不开」的头号原因。
    if ss -lnt 2>/dev/null | grep -qE '[:.]443[[:space:]]'; then
        LISTEN_443=1
    fi

    if [[ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]]; then
        if [[ $LISTEN_443 -eq 1 ]]; then
            HTTPS_CODE="$(curl -ks -o /dev/null -w '%{http_code}' -m 8 \
                --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/api/meta" 2>/dev/null || true)"
            if [[ "$HTTPS_CODE" == "200" ]]; then
                NGINX_HTTPS_OK=1
                ok "https 正常：https://$DOMAIN"
            else
                warn "！443 在监听，但没返回 200（实际「${HTTPS_CODE:-无响应}」）"
            fi
        else
            warn "！证书文件在，但没有任何进程监听 443 —— https 必然打不开。"
            warn "  说明证书没挂到 nginx 上。修："
            warn "      sudo certbot --nginx -d $DOMAIN --non-interactive --redirect"
        fi
    fi
fi

warn "脚本管不到最后一环：阿里云控制台的【安全组】必须放行 80 和 443。"
warn "系统防火墙开了、安全组没开，外面照样连不上。"

# -----------------------------------------------------------------------------
step "安装看门狗（服务挂了自动拉起，并留下现场）"
# -----------------------------------------------------------------------------

# ！这一步治的是「部署完好好的，第二天早上打不开，重跑一遍又好了」。
#
#   那种症状说明服务在某个时刻死了、而且自己没能起来。原因可能有好几种：
#     · systemd 因为「反复启动失败」而永久放弃了重试（上面已经关掉这个限制）
#     · 凌晨的自动安全更新重装了 nginx
#     · 证书续期把 nginx 配置改坏了
#     · 内存被打满，进程被 OOM killer 干掉
#   排查它们需要**现场** —— 而现场只在出事的那几分钟里存在。
#
#   看门狗每 2 分钟跑一次，干两件事，顺序不能反：
#     ① 先把现场记下来（单元状态、最近日志、监听端口、磁盘、内存）
#     ② 再把服务和 nginx 拉起来
#   反过来做的话，重启会把崩溃现场冲掉，下次照样查不出原因。
WATCHDOG_BIN="/usr/local/bin/${SERVICE_NAME}-watchdog"
WATCHDOG_LOG="$BASE_DIR/watchdog.log"
WATCHDOG_SERVICE="/etc/systemd/system/${SERVICE_NAME}-watchdog.service"
WATCHDOG_TIMER="/etc/systemd/system/${SERVICE_NAME}-watchdog.timer"

if [[ $DO_WATCHDOG -eq 0 ]]; then
    dim "MCFR_NO_WATCHDOG=1：跳过（顺便把以前装过的停掉）"
    run systemctl disable --now "${SERVICE_NAME}-watchdog.timer" >/dev/null 2>&1 || true
else
    run tee "$WATCHDOG_BIN" >/dev/null <<WATCHDOG
#!/bin/bash
# mcfr 看门狗 —— 由 deploy.sh 生成，systemd timer 每 2 分钟跑一次。
#
# ！健康就静默退出。这条路径每天要跑几百次，不能留下一堆噪音。
set -u

PORT="$APP_PORT"
SERVICE="$SERVICE_NAME"
LOG="$WATCHDOG_LOG"

healthy() {
    curl -fsS -m 8 "http://127.0.0.1:\${PORT}/api/meta" -o /dev/null 2>/dev/null
}

healthy && exit 0

{
    echo "================ \$(date '+%F %T') 检测到服务无响应 ================"
    echo "--- 单元状态 ---"
    systemctl status "\$SERVICE" --no-pager -l 2>&1 | head -20
    echo "--- 重启次数 / 上次结果 ---"
    systemctl show "\$SERVICE" -p NRestarts -p Result -p ExecMainStatus -p ActiveState 2>&1
    echo "--- 最近 40 行日志 ---"
    journalctl -u "\$SERVICE" -n 40 --no-pager 2>&1
    echo "--- nginx 是否在跑 ---"
    systemctl is-active nginx 2>&1
    echo "--- 监听端口 ---"
    ss -lnt 2>&1 | grep -E ':(80|443|\${PORT})\b' || echo "（80 / 443 / \${PORT} 都没有进程在听）"
    echo "--- 磁盘 ---"
    df -h / 2>&1 | tail -2
    echo "--- 内存 ---"
    free -h 2>&1 | head -2
    echo
} >> "\$LOG" 2>&1

# 日志别无限长：超过 2000 行就砍掉前面一半
if [[ \$(wc -l < "\$LOG") -gt 2000 ]]; then
    tail -n 1000 "\$LOG" > "\$LOG.tmp" && mv "\$LOG.tmp" "\$LOG"
fi

# 拉起来。
# ！reset-failed 必须先做：systemd 在「反复启动失败」之后会把单元标成
#   failed 并拒绝再启动，不清掉那个状态的话 start 也是白搭 ——#   而这恰恰就是「重跑一遍部署脚本才好」的原因所在。
systemctl reset-failed "\$SERVICE" 2>/dev/null || true
systemctl start "\$SERVICE" 2>/dev/null || true

# nginx 也一并照看：有些故障（自动升级重装了 nginx）挂的是它而不是应用。
systemctl is-active --quiet nginx 2>/dev/null || systemctl start nginx 2>/dev/null || true

sleep 6
if healthy; then
    echo "  → 已自动恢复（\$(date '+%F %T')）" >> "\$LOG"
else
    echo "  → ！自动恢复失败，需要人工介入（\$(date '+%F %T')）" >> "\$LOG"
fi
WATCHDOG

    run chmod 700 "$WATCHDOG_BIN"

    run tee "$WATCHDOG_SERVICE" >/dev/null <<WSVC
[Unit]
Description=mcfr 健康检查（不健康就把服务拉起来）

[Service]
Type=oneshot
ExecStart=$WATCHDOG_BIN
WSVC

    run tee "$WATCHDOG_TIMER" >/dev/null <<WTMR
[Unit]
Description=mcfr 健康检查，每 2 分钟一次

[Timer]
OnBootSec=3min
OnUnitActiveSec=2min
AccuracySec=15s

[Install]
WantedBy=timers.target
WTMR

    run touch "$WATCHDOG_LOG"
    run chmod 600 "$WATCHDOG_LOG"
    run systemctl daemon-reload
    run systemctl enable --now "${SERVICE_NAME}-watchdog.timer" >/dev/null 2>&1 || true

    ok "看门狗已装好：每 2 分钟检查一次，出问题先记现场再自动拉起"
    info "  现场日志：$WATCHDOG_LOG"
    dim "不想要它：MCFR_NO_WATCHDOG=1 sudo bash $0"
fi

# -----------------------------------------------------------------------------
# 汇总
# -----------------------------------------------------------------------------

VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$APP_DIR/backend/__init__.py" 2>/dev/null || true)"

# 备份到底多大。把模组镜像从备份里拿掉之后，这一个数字就能直接说明省下了多少。
BK_SIZE="$(du -sh "$BK" 2>/dev/null | cut -f1 || true)"
BK_COUNT="$(find "$BACKUP_DIR" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"

WATCHDOG_SUMMARY="每 2 分钟检查一次，不健康则记现场并自动拉起"
if [[ $DO_WATCHDOG -eq 0 ]]; then WATCHDOG_SUMMARY="未安装（MCFR_NO_WATCHDOG=1）"; fi

# ！首次部署时程序会【随机生成】后台口令，并打进启动日志。
#   在部署结尾把它捞出来展示一次 —— 不这么做的话，用户得自己去 journalctl 里翻，
#   而「后台进不去」是没法自助恢复的：密码是随机的、没人知道，也没有找回这条路。
#   --since 限在最近几分钟：只在本次启动真的生成过时才捞得到。
JOURNAL_TAIL="$(journalctl -u "$SERVICE_NAME" --since "-3min" --no-pager 2>/dev/null || true)"
ADMIN_USER="$(printf '%s\n' "$JOURNAL_TAIL" | grep -m1 '用户名：' | sed 's/.*用户名： *//' | tr -d ' \r' || true)"
ADMIN_PW="$(printf '%s\n' "$JOURNAL_TAIL" | grep -m1 '口令：' | sed 's/.*口令： *//' | tr -d ' \r' || true)"

# ！新代码到底换上没有 —— 对比「包里的版本」和「装完之后的版本」。
#   不一致就说明这次部署用的不是你以为的那份代码，
#   最常见的原因是 mc.zip 没重新上传（脚本会退回去用旧的那份）。
if [[ -n "${PKG_VERSION:-}" && "$PKG_VERSION" != "$VERSION" ]]; then
    warn "！版本对不上：发布包里是 $PKG_VERSION，装出来却是 ${VERSION:-读不到}"
    warn "  这次部署用的不是你以为的那份代码，先确认 mc.zip 是不是新的。"
fi

# ！这里以前会检查 MCFR_SMTP_* 配齐了没有，因为配错了不报错、用户只是收不到信。
#   v0.21 把邮件整个取消之后，这个坑不存在了：注册和登录卡的是本站自己画的
#   图片验证码（backend/captcha.py），不联网、不依赖任何外部服务、不需要任何配置。
#   「换了台服务器忘了配邮箱」从此不再是部署失误的一种。
cat <<SUMMARY

  ────────────────────────────────────────────────
  部署完成
  ────────────────────────────────────────────────
    版本      ${VERSION:-（读不到）}
    程序      $APP_DIR
    数据      $SHARED_DIR/data
    备份      $BK   (${BK_SIZE:-?}，不含模组镜像)
    历史备份  $BK_COUNT 份（上限 $KEEP_BACKUPS，超出自动删最旧的）
    看门狗    $WATCHDOG_SUMMARY
    验证     图片验证码（本站生成，无需配置）
    服务      systemctl status $SERVICE_NAME
    日志      journalctl -u $SERVICE_NAME -f

SUMMARY

# ！单独占一块，而不是塞进上面的表格里 —— 这一块只在真的生成了新口令时出现，
#   而它比上面的每一项都重要：错过这一屏，后台就再也进不去了。
if [[ -n "$ADMIN_PW" ]]; then
    cat <<PWSHOW
  ┌──────────────────────────────────────────────────────────┐
  │  本次部署生成了【全新的后台口令】，只显示这一次        │
  └──────────────────────────────────────────────────────────┘

      后台入口  https://$DOMAIN/admin
      用户名    ${ADMIN_USER:-(见 .env)}
      口令      $ADMIN_PW

      它已经写进 $SHARED_DIR/.env 的 MCFR_ADMIN_PASSWORD。
      想换成自己记得住的：改那一行，然后 systemctl restart $SERVICE_NAME

PWSHOW
fi

# ！这里以前只看证书文件在不在，于是会出现「脚本让你访问 https，
#   可 443 上根本没有进程在听」的误导。改成看实际验证结果。
if [[ $NGINX_HTTPS_OK -eq 1 ]]; then
    printf '    访问      https://%s\n\n' "$DOMAIN"
elif [[ $DO_NGINX -eq 1 && "$NGINX_HTTP_CODE" != "200" ]]; then
    printf '    访问      （nginx 这一层没通，看上面 [14/15] 的提示）\n\n'
else
    printf '    访问      http://%s\n\n' "$DOMAIN"
fi

if [[ $DO_NGINX -eq 1 ]]; then
    cat <<'TIP'
  上线后请务必确认这一项（限流是否正常全看它）：
      故意把管理员密码输错一次，然后执行
      sqlite3 <数据目录>/mcfr.db "SELECT ip FROM admin_logins ORDER BY id DESC LIMIT 1;"
      应该显示你自己的公网 IP。显示 127.0.0.1 就说明 MCFR_TRUSTED_PROXIES 没生效。

TIP
fi

cat <<TIP
  回滚到本次升级之前：
      sudo bash $0 --rollback $STAMP

TIP
