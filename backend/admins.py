"""管理员账号体系：超级管理员、普通管理员与附加权限。

v0.13 之前，后台只有一个「全局管理员密码」放在 `.env` 里，
谁能拿到那串密码谁就是管理员，没有账号、没有分级、也没法追责。
现在改成正经的账号体系：

    超级管理员（固定一个，用户名 adminzmq）
      ├─ 能进全部页签
      ├─ 能添加 / 停用 / 删除普通管理员
      └─ 能逐项决定每个普通管理员「看不到 / 只能看 / 能改」哪些受控模块

    普通管理员（由超管创建）
      ├─ 基础四页签**始终可用**：数据概览、举报处理、用户管理、邮件记录
      └─ 受控模块必须由超管逐项开权限，默认一律看不到

受控模块登记在 `AREAS` 里；基础页签不在其中，因为它们是普通管理员的默认工作区。

！口令一律用 `security.hash_password` 存哈希，绝不落明文。
  唯一的例外是超级管理员的初始口令 —— 它来自 `.env`（或内置默认值），
  启动时由 `ensure_super_admin()` 同步进库，这样「改 .env 重启」仍然能改密码。
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
from typing import Any

from .config import update_env
from .database import now_ts
from .security import DUMMY_HASH, hash_password, verify_password

# 超级管理员账号名固定。改这个常量会让已有的超管账号变成孤立数据，
# 所以不要改；要换名字请用后台的「重命名」功能。
SUPER_USERNAME = "adminzmq"

# ！这里【故意】没有默认口令。
#
#   以前这里写着一串硬编码常量，`super_password()` 在 .env 没配时就会用它。
#   后果是：谁拿到这份源码，谁就拿到一个可能有效的后台口令 ——
#   而源码是要交作业、传仓库、打进发布包（deploy.sh 的 zip 里确实带 backend/）的。
#   一串“看起来很像真的”的默认口令，比 “admin123” 危险得多，
#   因为它不会引起任何人的警觉。
#
#   现在的规则：
#     · .env 里配了 MCFR_ADMIN_PASSWORD → 用它（保持“改 .env 重启即改密”的老习惯）
#     · 首次启动、库里还没有超管     → 现场生成一个随机口令，写回 .env，并打印出来
#     · 已存在但 .env 没配            → **什么都不做**，保持库里现有口令
ENV_ADMIN_PASSWORD = "MCFR_ADMIN_PASSWORD"

# 认为是“没改过”的占位值，见到就重新生成。
# ！change-me 是 .env.example 里的写法 —— 全新部署会把它拷成 .env，
#   不拦的话一个默认口令就这么上生产了。
PLACEHOLDER_PASSWORDS = {"", "change-me", "changeme", "password"}

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{3,24}$")
MIN_PASSWORD_LEN = 8

# 普通管理员在基础四页签之外能碰的模块。
# level 三档：none 看不到 / view 只能看 / manage 能改。
AREAS: tuple[tuple[str, str, str], ...] = (
    ("feedback", "意见反馈", "查看、处理用户提交的反馈"),
    # ！需要单独一档而不是并进 feedback：两者的处置动作不一样。
    #   反馈是用户主动告诉你的，读一读就行了；帖子是在公开页面上
    #   所有人都看得见的内容，出事时要能马上摘下去。
    ("content", "帖子与评论", "查看并下架不合规的帖子和评论"),
    ("system", "系统升级", "备份、导出、导入、还原数据库与版本升级"),
    # ！镜像故意拆成两个权限：同步只是拉一份索引（轻、可反复跑），
    #   下载会把几十上百 MB 的文件写进服务器磁盘（重、占了就回不去）。
    #   很多时候你想让某个管理员能刷新索引，但不想让他把磁盘塞满。
    ("mirror_sync", "模组：同步索引", "从 Modrinth 拉取热门模组与版本清单"),
    ("mirror_download", "模组：镜像文件", "决定镜像哪些模组，并把文件下载到本站"),
    # ！v0.33 起的桌面启动器。它不在浏览器里跑，但「谁能改功能开关」
    #   和站内其它受控模块是同一件事，所以沿用同一套 view / manage 分级。
    ("launcher", "启动器管理", "发布安装包、开关功能模块、发布公告、查看使用统计"),
)
AREA_KEYS: tuple[str, ...] = tuple(key for key, _, _ in AREAS)
LEVELS: tuple[str, ...] = ("none", "view", "manage")
LEVEL_LABELS = {"none": "看不到", "view": "只能看", "manage": "能改"}

# 受控模块 -> 它出现在哪个后台页签上。
# 目前只有镜像会多个模块共用一个页签（同步 + 下载合成「模组镜像」），
# 其余都是一对一。
AREA_TABS: dict[str, str] = {
    "feedback": "feedback",
    "system": "system",
    "mirror_sync": "mirror",
    "mirror_download": "mirror",
    "launcher": "launcher",
}

# 普通管理员永远可用的页签（不需要授权）
# ！「mail」在 v0.21 改叫「security」：邮件已经整个取消，
#   那个页签现在展示的是人机验证记录，再去叫邮件记录就说不通了。
BASE_TABS: tuple[str, ...] = ("overview", "reports", "users", "security")


def area_catalog() -> list[dict[str, Any]]:
    """给前端渲染「权限设置」界面用的模块说明。"""
    return [
        {"key": key, "name": name, "desc": desc, "levels": list(LEVELS), "labels": LEVEL_LABELS}
        for key, name, desc in AREAS
    ]


def super_password() -> str:
    """`.env` 里配的超级管理员口令；没配就返回空串（**不再回退到固定值**）。"""
    value = os.environ.get(ENV_ADMIN_PASSWORD, "").strip()
    return "" if value.lower() in PLACEHOLDER_PASSWORDS else value


def _show_generated_password(password: str) -> None:
    """把新生成的口令写回 .env，并在控制台上显眼地打出来。

    ！写回 .env 而不是只打印：systemd 的日志会轮转，用户一旦错过那一屏，
      就再也拿不到这个口令了，而超管进不去后台是没法自助恢复的。
    """
    try:
        update_env({ENV_ADMIN_PASSWORD: password})
        where = "已写入 .env"
    except OSError as exc:
        where = f"写 .env 失败（{exc}），请手工记下来"

    bar = "=" * 64
    print(f"\n[ mcfr ] {bar}")
    print(f"[ mcfr ] 已生成后台超级管理员口令（{where}）：")
    print(f"[ mcfr ]")
    print(f"[ mcfr ]     用户名：{SUPER_USERNAME}")
    print(f"[ mcfr ]     口令：  {password}")
    print(f"[ mcfr ]")
    print(f"[ mcfr ] 请现在记下来。想自己指定就改 .env 里的 {ENV_ADMIN_PASSWORD} 后重启。")
    print(f"[ mcfr ] {bar}\n")


# ---------------------------------------------------------------- 初始化

def ensure_super_admin(conn: sqlite3.Connection) -> None:
    """保证超级管理员账号存在，并让它跟 .env 里的口令保持一致。

    ！为什么不无条件重算哈希：
      以前这里每次都拿 .env（或硬编码默认值）重算一遍。好处是“改 .env 重启即改密”，
      代价是 —— .env 一旦丢失或被覆盖（换服务器、重装、发布包解压），
      口令就会**静默**退回那个默认值。现在只在这两种情况下重算：
        ① 账号还不存在（老库升级上来）
        ② .env 里确实配了非占位值的口令
    """
    stamp = now_ts()
    row = conn.execute(
        "SELECT * FROM admins WHERE username = ?", (SUPER_USERNAME,)
    ).fetchone()
    configured = super_password()

    if row is None:
        password = configured
        generated = not password
        if generated:
            password = secrets.token_urlsafe(12)
        conn.execute(
            """
            INSERT INTO admins
                (username, display_name, password_hash, is_super, status,
                 note, created_by, created_at, last_login)
            VALUES (?, ?, ?, 1, 'active', ?, ?, ?, 0)
            """,
            (
                SUPER_USERNAME,
                "超级管理员",
                hash_password(password),
                "系统内置，不可删除",
                "system",
                stamp,
            ),
        )
        if generated:
            _show_generated_password(password)
        else:
            print(f"[mcfr] 已创建超级管理员账号（{SUPER_USERNAME}，口令来自 .env）")
        return

    if configured:
        # 用户按 README 改 .env 里的 MCFR_ADMIN_PASSWORD 再重启，
        # 就是他心里「改管理员密码」的操作，必须真的生效。
        conn.execute(
            "UPDATE admins SET password_hash = ?, is_super = 1, status = 'active' WHERE id = ?",
            (hash_password(configured), row["id"]),
        )
        return

    # ！.env 里没配口令。保持库里现有的哈希不动，只顺手救回被误停用的超管。
    #   这一步什么都不打印 —— 如果每次都提醒“你没配 .env”，
    #   会冼漠掉真正重要的那条（首次生成口令）。
    conn.execute(
        "UPDATE admins SET is_super = 1, status = 'active' WHERE id = ?", (row["id"],)
    )


def seed_from_env_password(conn: sqlite3.Connection) -> None:
    """兼容旧库：把所有还挂着旧「全局密码」的会话认成超级管理员。

    旧版本的 admin_sessions 里没有 admin_id / is_super，补列时 is_super 默认给了 1。
    这里只是把 admin_id / admin_name 也补成超管，免得界面上显示成空白账号。
    """
    row = conn.execute("SELECT id FROM admins WHERE is_super = 1 LIMIT 1").fetchone()
    if row is None:
        return
    conn.execute(
        "UPDATE admin_sessions SET admin_id = ?, admin_name = ? "
        "WHERE is_super = 1 AND (admin_id = 0 OR admin_name = '')",
        (row["id"], SUPER_USERNAME),
    )


# ---------------------------------------------------------------- 查询

def find_admin(conn: sqlite3.Connection, username: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM admins WHERE username = ?", ((username or "").strip().lower(),)
    ).fetchone()
    return dict(row) if row is not None else None


def find_admin_by_id(conn: sqlite3.Connection, admin_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM admins WHERE id = ?", (admin_id,)).fetchone()
    return dict(row) if row is not None else None


def permissions_of(conn: sqlite3.Connection, admin: dict | None) -> dict[str, str]:
    """某个管理员在受控模块上的权限。超管一律 manage，普通管理员查表。"""
    result = {key: "none" for key in AREA_KEYS}
    if not admin:
        return result
    if admin.get("is_super"):
        return {key: "manage" for key in AREA_KEYS}

    rows = conn.execute(
        "SELECT area, level FROM admin_permissions WHERE admin_id = ?", (admin["id"],)
    ).fetchall()
    for row in rows:
        area = str(row["area"])
        level = str(row["level"])
        if area in result and level in LEVELS:
            result[area] = level
    return result


def visible_tabs(admin: dict, perms: dict[str, str]) -> list[str]:
    """这个管理员能看到哪些页签（顺序就是后台标签栏的顺序）。"""
    tabs = list(BASE_TABS)

    def add(area: str) -> None:
        tab = AREA_TABS.get(area, area)
        if tab not in tabs:
            tabs.append(tab)

    if admin.get("is_super"):
        # 超管额外多两个页签
        for key in AREA_KEYS:
            add(key)
        # ！「登录密钥」刻意**不**写进 AREAS：写进去它就会出现在
        #   「管理员」页那张可授权表里，而普通管理员即使被授权也用不了
        #   （那几个接口都是 require_super）。给一个点了就 403 的开关，
        #   比没有这个开关更让人困惑。
        tabs.append("adminkeys")
        tabs.append("admins")
        return tabs

    for key in AREA_KEYS:
        if perms.get(key, "none") != "none":
            add(key)
    return tabs


def can(admin: dict | None, perms: dict[str, str], area: str, need: str = "view") -> bool:
    """权限判定。need 是 'view' 或 'manage'；manage 蕴含 view。"""
    if not admin:
        return False
    if admin.get("is_super"):
        return True
    level = perms.get(area, "none")
    if need == "manage":
        return level == "manage"
    return level in ("view", "manage")


def public_admin(admin: dict, perms: dict[str, str]) -> dict[str, Any]:
    """给前端的账号信息（不含任何口令相关字段）。"""
    return {
        "id": admin["id"],
        "username": admin["username"],
        "display_name": admin.get("display_name") or admin["username"],
        "is_super": bool(admin.get("is_super")),
        "status": admin.get("status") or "active",
        "note": admin.get("note") or "",
        "created_at": admin.get("created_at") or 0,
        "created_by": admin.get("created_by") or "",
        "last_login": admin.get("last_login") or 0,
        "permissions": perms,
    }


# ---------------------------------------------------------------- 登录校验

def verify_login(conn: sqlite3.Connection, username: str, password: str) -> dict | None:
    """校验账号口令。成功返回账号字典，失败返回 None。

    ！账号不存在时也照常跑一次哈希比对，让「账号不存在」和「口令错误」
      花掉的时间差不多 —— 否则可以靠响应时间枚举出有哪些管理员账号。
      这里直接拿一个固定的假哈希来比，不做短路。
    """
    admin = find_admin(conn, username)
    if admin is None:
        verify_password(password or "", DUMMY_HASH)
        return None
    if not verify_password(password or "", admin.get("password_hash") or ""):
        return None
    if (admin.get("status") or "active") != "active":
        return None
    return admin


# ---------------------------------------------------------------- 输入校验

class AdminInputError(ValueError):
    """账号参数不合法。message 可以直接展示给管理员看。"""


def validate_username(username: str) -> str:
    value = (username or "").strip().lower()
    if not USERNAME_RE.match(value):
        raise AdminInputError("用户名只能是 3-24 位的字母、数字、下划线或连字符")
    if value == SUPER_USERNAME:
        raise AdminInputError("这个用户名是超级管理员专用的，换一个")
    return value


def validate_password(password: str) -> str:
    value = (password or "").strip()
    if len(value) < MIN_PASSWORD_LEN:
        raise AdminInputError(f"密码至少 {MIN_PASSWORD_LEN} 位")
    if value.isdigit() or value.isalpha():
        raise AdminInputError("密码不要只用纯数字或纯字母")
    return value


def normalize_permissions(payload: dict[str, Any] | None) -> dict[str, str]:
    """把前端传来的权限表收拾干净：只认已知模块、未知档位一律当 none。"""
    result: dict[str, str] = {}
    for key, value in (payload or {}).items():
        if key not in AREA_KEYS:
            continue
        level = str(value or "none").strip().lower()
        result[key] = level if level in LEVELS else "none"
    return result


def store_permissions(conn: sqlite3.Connection, admin_id: int, perms: dict[str, str]) -> None:
    stamp = now_ts()
    for area in AREA_KEYS:
        level = perms.get(area, "none")
        if level == "none":
            conn.execute(
                "DELETE FROM admin_permissions WHERE admin_id = ? AND area = ?",
                (admin_id, area),
            )
        else:
            conn.execute(
                """
                INSERT INTO admin_permissions(admin_id, area, level, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(admin_id, area) DO UPDATE SET level = excluded.level,
                                                          updated_at = excluded.updated_at
                """,
                (admin_id, area, level, stamp),
            )


def revoke_sessions(conn: sqlite3.Connection, admin_id: int) -> None:
    """让某个管理员的全部登录立即失效（停用、改密、删除时调用）。"""
    conn.execute("DELETE FROM admin_sessions WHERE admin_id = ?", (admin_id,))


# ---------------------------------------------------------------- 密钥对登录（v0.32）
#
# 曲线运算和验签在 backend/adminkeys.py；这里只管存取。
# 分开的理由：那个模块是纯数学，不碰数据库，所以可以单独拿去对着
# 浏览器 WebCrypto 做交叉验证（已经这么做过了）。

# 是否还允许用密码登录后台。
# ！它是**全局**的，不是每个管理员一份 —— 密码入口是「后门」，
#   要么开着要么关掉；一个账号一套只会让人搞不清现在到底是什么状态。
PASSWORD_LOGIN_KEY = "admin_password_login"

# 密码登录默认**开着**。理由很实际：密钥是新增的一条路，
# 用户可能还没生成，或者生成完还没真试过。默认就把密码关掉的话，
# 一旦密钥文件出问题（没带、U 盘读不出来、复制坏了），人就真的进不去了。
# 等确认密钥能用之后再关 —— 后台「安全」页有那个开关。
PASSWORD_LOGIN_DEFAULT = True


def password_login_enabled(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (PASSWORD_LOGIN_KEY,)
    ).fetchone()
    if row is None:
        return PASSWORD_LOGIN_DEFAULT
    return str(row["value"]).strip() != "0"


def set_password_login(conn: sqlite3.Connection, enabled: bool) -> None:
    conn.execute(
        """
        INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                       updated_at = excluded.updated_at
        """,
        (PASSWORD_LOGIN_KEY, "1" if enabled else "0", now_ts()),
    )


def key_list(conn: sqlite3.Connection, admin_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM admin_keys WHERE admin_id = ? ORDER BY created_at ASC",
        (admin_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def key_count(conn: sqlite3.Connection, admin_id: int) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM admin_keys WHERE admin_id = ?", (admin_id,)
    ).fetchone()[0])


def key_add(conn: sqlite3.Connection, admin_id: int, key_id: str,
            public_key: str, label: str) -> None:
    conn.execute(
        "INSERT INTO admin_keys(admin_id, key_id, public_key, label, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (admin_id, key_id, public_key, (label or "")[:40], now_ts()),
    )


def key_remove(conn: sqlite3.Connection, admin_id: int, key_id: str) -> bool:
    """删掉一把密钥。

    ！条件里必须同时带 admin_id，而且那个值只能来自会话 ——
      只按 key_id 删的话，知道别人 key_id 的人就能删掉别人的钥匙。
    """
    cursor = conn.execute(
        "DELETE FROM admin_keys WHERE admin_id = ? AND key_id = ?",
        (admin_id, key_id),
    )
    return cursor.rowcount > 0


def key_find(conn: sqlite3.Connection, key_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM admin_keys WHERE key_id = ?", (key_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def key_touch(conn: sqlite3.Connection, key_id: str, ip: str) -> None:
    """记一笔「这把钥匙什么时候、从哪里被用过」，方便在后台看出异常。"""
    conn.execute(
        "UPDATE admin_keys SET last_used_at = ?, last_used_ip = ? WHERE key_id = ?",
        (now_ts(), ip or "", key_id),
    )
