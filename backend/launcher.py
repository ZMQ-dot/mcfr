"""启动器服务端逻辑（v0.33 起）。

这个模块负责三件互不相干、但都属于「桌面启动器要不要听站长的话」的事：

1. **发布与开关**：最新版本号、强制更新线、各功能模块的开关、公告。
2. **使用统计**：装过启动器的设备、启动次数、每日曲线。
3. **微软正版登录代理**：设备码流程、Xbox Live、XSTS、Minecraft 服务
   全部在这台服务器上跑完，启动器只拿一个 flow_id 来问结果。

第 3 点是这一版最关键的变化。在此之前 client_id 是打在启动器里的密文，
现在它只存在于服务器（`.env` 的 MCFR_MS_CLIENT_ID），
启动器反编译出来也找不到任何应用凭据。

！依赖纪律：这里只能用标准库（urllib）。项目硬约束是零新增依赖，
  所以没有 requests / httpx —— 下面那些 `_post_*` 就是为此手写的。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .database import now_ts, settings_map

BRAND = "mcfr"
USER_AGENT = "mcfr-launcher-server/1.0 (+https://mcfr.cn)"

# ===========================================================================
# 功能开关
#
# 后台可以逐个关掉。关掉之后启动器上对应的入口会变灰并显示原因 ——
# 这是「出事时能立刻止血」的那只手：某个接口被打崩了、某个模块有严重 bug，
# 不用重新发版让所有用户下载，改一个开关就行。
#
# ！新增一项时，记得同时在启动器 renderer 的 FEATURE_GUARDS 里登记，
#   否则那一项在客户端不会真的被挡住（只会从配置里读到一个没人看的键）。
# ===========================================================================
FEATURES: tuple[tuple[str, str, str], ...] = (
    ("friends", "好友与消息", "好友列表、入站申请、通知中心"),
    ("discover", "发现新朋友", "按兴趣推荐与搜索用户"),
    ("versions", "下载与安装游戏", "原版与模组加载器的版本清单、下载安装"),
    ("mods", "模组管理", "查看、启用 / 禁用、删除实例里的模组"),
    ("java_download", "自动下载 Java", "从 Mojang 拉取 Java 运行时"),
    ("offline_login", "离线账号", "不联网的本地账号登录"),
    ("microsoft_login", "微软正版登录", "经 mcfr 服务器完成的微软账户登录"),
    ("logs", "日志与崩溃分析", "读取游戏日志、解析崩溃报告"),
)
FEATURE_KEYS: tuple[str, ...] = tuple(key for key, _, _ in FEATURES)

# 每个模块的默认状态。没写的默认 "1"（开）。
#
# ！microsoft_login 默认**关**：Mojang 对第三方启动器的应用接入走的是
#   人工白名单审核（aka.ms/mce-reviewappid），而且 2026-10 的申请被明确驳回，
#   官方也声明不提供咨询与排查。默认开着只会让用户点一次、失败一次。
#   哪天真的进了白名单，管理员在后台把开关打开即可，不需要改代码。
FEATURE_DEFAULTS: dict[str, str] = {
    "microsoft_login": "0",
}

# 运行期设置（存在 settings 表里，key 一律 launcher. 前缀）
KEY_MAINTENANCE = "launcher.maintenance"
KEY_MESSAGE = "launcher.message"
KEY_MIN_VERSION = "launcher.min_version"
KEY_HEARTBEAT = "launcher.heartbeat_seconds"
KEY_CHANNEL = "launcher.channel"

DEFAULTS: dict[str, str] = {
    KEY_MAINTENANCE: "0",
    KEY_MESSAGE: "",
    KEY_MIN_VERSION: "",
    KEY_HEARTBEAT: "600",
    KEY_CHANNEL: "stable",
}

HEARTBEAT_MIN_SECONDS = 60      # 心跳最快 1 分钟一次，再快就是客户端有问题
HEARTBEAT_MAX_SECONDS = 3600


def feature_key(name: str) -> str:
    return f"launcher.feature.{name}"


# ===========================================================================
# 版本号比较
# ===========================================================================

_VERSION_RE = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?")


def version_tuple(value: str) -> tuple[int, ...]:
    """把 "0.2.1" / "v1.0" / "1.2.3-beta" 统一成可比较的整数元组。

    ！只取开头那段数字，后缀（-beta / +build）直接忽略：
      预发布标签的排序规则各家都不一样，而这里只回答「谁更新」。
      真要区分渠道，用 channel 字段（stable / beta），不要塞进版本号。
    """
    match = _VERSION_RE.search((value or "").strip())
    if not match:
        return (0,)
    parts = [int(g) for g in match.groups() if g is not None]
    return tuple(parts) if parts else (0,)


def older_than(a: str, b: str) -> bool:
    """a 是否比 b 旧。"""
    if not a or not b:
        return False
    ta, tb = version_tuple(a), version_tuple(b)
    length = max(len(ta), len(tb))
    ta = ta + (0,) * (length - len(ta))
    tb = tb + (0,) * (length - len(tb))
    return ta < tb


# ===========================================================================
# 设置读写
# ===========================================================================

def read_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    raw = settings_map(conn, "launcher.")
    features = {
        key: raw.get(feature_key(key), FEATURE_DEFAULTS.get(key, "1")) != "0"
        for key in FEATURE_KEYS
    }
    heartbeat = _to_int(raw.get(KEY_HEARTBEAT), 600)
    heartbeat = max(HEARTBEAT_MIN_SECONDS, min(HEARTBEAT_MAX_SECONDS, heartbeat))
    channel = (raw.get(KEY_CHANNEL) or "stable").strip() or "stable"
    return {
        "maintenance": raw.get(KEY_MAINTENANCE, "0") == "1",
        "message": raw.get(KEY_MESSAGE, ""),
        "min_version": (raw.get(KEY_MIN_VERSION) or "").strip(),
        "heartbeat_seconds": heartbeat,
        "channel": channel,
        "features": features,
    }


def write_settings(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    stamp = now_ts()
    updates: dict[str, str] = {}
    if "maintenance" in payload and payload["maintenance"] is not None:
        updates[KEY_MAINTENANCE] = "1" if payload["maintenance"] else "0"
    if "message" in payload and payload["message"] is not None:
        updates[KEY_MESSAGE] = str(payload["message"])[:2000]
    if "min_version" in payload and payload["min_version"] is not None:
        updates[KEY_MIN_VERSION] = str(payload["min_version"]).strip()[:32]
    if "channel" in payload and payload["channel"] is not None:
        channel = str(payload["channel"]).strip().lower()
        updates[KEY_CHANNEL] = channel if channel in ("stable", "beta") else "stable"
    if "heartbeat_seconds" in payload and payload["heartbeat_seconds"] is not None:
        value = _to_int(payload["heartbeat_seconds"], 600)
        value = max(HEARTBEAT_MIN_SECONDS, min(HEARTBEAT_MAX_SECONDS, value))
        updates[KEY_HEARTBEAT] = str(value)
    features = payload.get("features") or {}
    if isinstance(features, dict):
        for key, value in features.items():
            if key not in FEATURE_KEYS:
                continue
            updates[feature_key(key)] = "1" if value else "0"

    for key, value in updates.items():
        conn.execute(
            """
            INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                           updated_at = excluded.updated_at
            """,
            (key, value, stamp),
        )
    return read_settings(conn)


def _to_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


# ===========================================================================
# 发布版本
# ===========================================================================

def release_rows(conn: sqlite3.Connection, channel: str | None = None) -> list[dict[str, Any]]:
    if channel:
        rows = conn.execute(
            "SELECT * FROM launcher_releases WHERE channel = ? ORDER BY created_at DESC",
            (channel,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM launcher_releases ORDER BY created_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def latest_release(conn: sqlite3.Connection, channel: str = "stable") -> dict[str, Any] | None:
    """该渠道下 active 的最新版本。版本号在 Python 里比，不靠 SQL 排序。"""
    rows = [
        row for row in release_rows(conn, channel) if int(row.get("active") or 0)
    ]
    if not rows:
        return None
    return max(rows, key=lambda row: version_tuple(row.get("version") or ""))


def add_release(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    version = str(payload.get("version") or "").strip()
    if not version_tuple(version):
        raise ValueError("版本号不能为空，例如 0.2.0")
    channel = str(payload.get("channel") or "stable").strip().lower()
    if channel not in ("stable", "beta"):
        raise ValueError("渠道只能是 stable 或 beta")
    cursor = conn.execute(
        """
        INSERT INTO launcher_releases
            (version, channel, notes, url, sha256, byte_size, forced, active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel, version) DO UPDATE SET
            notes = excluded.notes,
            url = excluded.url,
            sha256 = excluded.sha256,
            byte_size = excluded.byte_size,
            forced = excluded.forced,
            active = excluded.active
        """,
        (
            version[:32],
            channel,
            str(payload.get("notes") or "")[:8000],
            str(payload.get("url") or "").strip()[:500],
            str(payload.get("sha256") or "").strip().lower()[:64],
            _to_int(payload.get("byte_size"), 0),
            1 if payload.get("forced") else 0,
            1 if payload.get("active", True) else 0,
            now_ts(),
        ),
    )
    row = conn.execute(
        "SELECT * FROM launcher_releases WHERE channel = ? AND version = ?",
        (channel, version[:32]),
    ).fetchone()
    if row is None:  # pragma: no cover - ON CONFLICT 之后必然存在
        raise ValueError("写入版本失败")
    _ = cursor
    return dict(row)


def delete_release(conn: sqlite3.Connection, release_id: int) -> bool:
    cursor = conn.execute("DELETE FROM launcher_releases WHERE id = ?", (release_id,))
    return cursor.rowcount > 0


# ===========================================================================
# 公告
# ===========================================================================

def notice_rows(conn: sqlite3.Connection, *, only_live: bool = False) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM launcher_notices ORDER BY created_at DESC LIMIT 200"
    ).fetchall()
    items = [dict(row) for row in rows]
    if not only_live:
        return items
    now = now_ts()
    live: list[dict[str, Any]] = []
    for item in items:
        if not int(item.get("active") or 0):
            continue
        starts = int(item.get("starts_at") or 0)
        ends = int(item.get("ends_at") or 0)
        if starts and starts > now:
            continue
        if ends and ends < now:
            continue
        live.append(item)
    return live[:10]


def add_notice(conn: sqlite3.Connection, payload: dict[str, Any], author: str = "") -> dict[str, Any]:
    title = str(payload.get("title") or "").strip()
    if not title:
        raise ValueError("公告标题不能为空")
    level = str(payload.get("level") or "info").strip().lower()
    if level not in ("info", "warn", "critical"):
        raise ValueError("级别只能是 info / warn / critical")
    cursor = conn.execute(
        """
        INSERT INTO launcher_notices
            (title, body, level, active, starts_at, ends_at, created_at, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title[:80],
            str(payload.get("body") or "")[:4000],
            level,
            1 if payload.get("active", True) else 0,
            _to_int(payload.get("starts_at"), 0),
            _to_int(payload.get("ends_at"), 0),
            now_ts(),
            author[:24],
        ),
    )
    row = conn.execute(
        "SELECT * FROM launcher_notices WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return dict(row) if row is not None else {}


def update_notice(conn: sqlite3.Connection, notice_id: int, payload: dict[str, Any]) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM launcher_notices WHERE id = ?", (notice_id,)
    ).fetchone()
    if row is None:
        return None
    current = dict(row)
    title = str(payload.get("title", current["title"]) or "").strip()[:80] or current["title"]
    body = str(payload.get("body", current["body"]) or "")[:4000]
    level = str(payload.get("level", current["level"]) or "info").strip().lower()
    if level not in ("info", "warn", "critical"):
        level = current["level"]
    conn.execute(
        """
        UPDATE launcher_notices
           SET title = ?, body = ?, level = ?, active = ?, starts_at = ?, ends_at = ?
         WHERE id = ?
        """,
        (
            title,
            body,
            level,
            1 if payload.get("active", bool(current["active"])) else 0,
            _to_int(payload.get("starts_at", current["starts_at"]), current["starts_at"]),
            _to_int(payload.get("ends_at", current["ends_at"]), current["ends_at"]),
            notice_id,
        ),
    )
    row = conn.execute(
        "SELECT * FROM launcher_notices WHERE id = ?", (notice_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def delete_notice(conn: sqlite3.Connection, notice_id: int) -> bool:
    cursor = conn.execute("DELETE FROM launcher_notices WHERE id = ?", (notice_id,))
    return cursor.rowcount > 0


# ===========================================================================
# 设备与使用统计
# ===========================================================================

DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def clean_device_id(value: str) -> str:
    device = (value or "").strip()
    if not DEVICE_RE.match(device):
        raise ValueError("device_id 不合法")
    return device


def touch_client(
    conn: sqlite3.Connection,
    device_id: str,
    *,
    os_name: str = "",
    arch: str = "",
    app_version: str = "",
    mc_version: str = "",
    loader: str = "",
    user_id: int | None = None,
    ip: str = "",
) -> dict[str, Any]:
    """记录「这台设备还活着」，顺便更新它的画像。

    ！20 秒内的重复心跳直接返回，不写库：启动器切页面时可能连发几次，
      每次都写一遍会让 launcher_clients 变成热页。
    """
    device = clean_device_id(device_id)
    stamp = now_ts()
    row = conn.execute(
        "SELECT * FROM launcher_clients WHERE device_id = ?", (device,)
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO launcher_clients
                (device_id, os, arch, app_version, mc_version, loader, user_id,
                 first_seen, last_seen, launch_count, play_seconds, last_ip)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
            """,
            (
                device,
                os_name[:24],
                arch[:16],
                app_version[:24],
                mc_version[:32],
                loader[:24],
                user_id,
                stamp,
                stamp,
                ip[:64],
            ),
        )
        row = conn.execute(
            "SELECT * FROM launcher_clients WHERE device_id = ?", (device,)
        ).fetchone()
    else:
        last_seen = int(row["last_seen"] or 0)
        if stamp - last_seen >= 20:
            conn.execute(
                """
                UPDATE launcher_clients
                   SET os = COALESCE(NULLIF(?, ''), os),
                       arch = COALESCE(NULLIF(?, ''), arch),
                       app_version = COALESCE(NULLIF(?, ''), app_version),
                       mc_version = COALESCE(NULLIF(?, ''), mc_version),
                       loader = COALESCE(NULLIF(?, ''), loader),
                       user_id = COALESCE(?, user_id),
                       last_seen = ?,
                       last_ip = ?
                 WHERE device_id = ?
                """,
                (
                    os_name[:24],
                    arch[:16],
                    app_version[:24],
                    mc_version[:32],
                    loader[:24],
                    user_id,
                    stamp,
                    ip[:64],
                    device,
                ),
            )
    return dict(row) if row is not None else {}


EVENT_KINDS = ("start", "launch", "exit", "update")


def record_event(
    conn: sqlite3.Connection,
    device_id: str,
    kind: str,
    detail: str = "",
    *,
    seconds: int = 0,
    mc_version: str = "",
    loader: str = "",
) -> None:
    """记一条事件。

    kind 只有四种，且**不接受**客户端自定义的字符串 —— 这张表要给后台画曲线，
    什么都能写进来，第二天就会变成一张没人看得懂的杂表。
    """
    if kind not in EVENT_KINDS:
        raise ValueError("未知的事件类型")
    device = clean_device_id(device_id)
    stamp = now_ts()
    conn.execute(
        "INSERT INTO launcher_events(device_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
        (device, kind, str(detail or "")[:200], stamp),
    )
    if kind == "launch":
        conn.execute(
            """
            UPDATE launcher_clients
               SET launch_count = launch_count + 1,
                   mc_version = CASE WHEN ? != '' THEN ? ELSE mc_version END,
                   loader = CASE WHEN ? != '' THEN ? ELSE loader END
             WHERE device_id = ?
            """,
            (mc_version[:32], mc_version[:32], loader[:24], loader[:24], device),
        )
    elif kind == "exit":
        seconds = max(0, min(24 * 3600, int(seconds or 0)))
        conn.execute(
            "UPDATE launcher_clients SET play_seconds = play_seconds + ? WHERE device_id = ?",
            (seconds, device),
        )


def stats(conn: sqlite3.Connection, days: int = 14) -> dict[str, Any]:
    days = max(1, min(90, int(days or 14)))
    since = now_ts() - days * 86400
    daily_rows = conn.execute(
        """
        SELECT date(created_at, 'unixepoch', 'localtime') AS day,
               kind,
               COUNT(DISTINCT device_id) AS devices,
               COUNT(*) AS total
          FROM launcher_events
         WHERE created_at >= ?
         GROUP BY day, kind
        """,
        (since,),
    ).fetchall()
    daily: dict[str, dict[str, int]] = {}
    for row in daily_rows:
        day = str(row["day"])
        bucket = daily.setdefault(day, {"starts": 0, "launches": 0, "devices": 0})
        kind = str(row["kind"])
        if kind == "start":
            bucket["starts"] = int(row["total"])
        elif kind == "launch":
            bucket["launches"] = int(row["total"])
        bucket["devices"] = max(bucket["devices"], int(row["devices"]))

    totals = conn.execute(
        """
        SELECT COUNT(*) AS devices,
               COALESCE(SUM(launch_count), 0) AS launches,
               COALESCE(SUM(play_seconds), 0) AS play_seconds,
               COALESCE(SUM(CASE WHEN last_seen >= ? THEN 1 ELSE 0 END), 0) AS active_7d
          FROM launcher_clients
        """,
        (now_ts() - 7 * 86400,),
    ).fetchone()

    by_version = [
        {"name": str(row["app_version"] or "未知"), "count": int(row["c"])}
        for row in conn.execute(
            """
            SELECT app_version, COUNT(*) AS c FROM launcher_clients
             GROUP BY app_version ORDER BY c DESC LIMIT 10
            """
        ).fetchall()
    ]
    by_os = [
        {"name": str(row["os"] or "未知"), "count": int(row["c"])}
        for row in conn.execute(
            """
            SELECT os, COUNT(*) AS c FROM launcher_clients
             GROUP BY os ORDER BY c DESC LIMIT 10
            """
        ).fetchall()
    ]
    return {
        "days": days,
        "daily": [
            {"day": day, **value} for day, value in sorted(daily.items())
        ],
        "devices": int(totals["devices"] or 0),
        "active_7d": int(totals["active_7d"] or 0),
        "launches": int(totals["launches"] or 0),
        "play_seconds": int(totals["play_seconds"] or 0),
        "by_version": by_version,
        "by_os": by_os,
    }


def client_rows(conn: sqlite3.Connection, *, page: int = 1, size: int = 40, q: str = "") -> dict[str, Any]:
    page = max(1, int(page or 1))
    size = max(1, min(100, int(size or 40)))
    where = ""
    params: list[Any] = []
    if q:
        where = "WHERE device_id LIKE ? OR os LIKE ? OR app_version LIKE ?"
        like = f"%{q.strip()}%"
        params = [like, like, like]
    total = int(
        conn.execute(f"SELECT COUNT(*) FROM launcher_clients {where}", params).fetchone()[0]
    )
    rows = conn.execute(
        f"""
        SELECT c.*, u.username AS user_name
          FROM launcher_clients c
          LEFT JOIN users u ON u.id = c.user_id
          {where}
         ORDER BY c.last_seen DESC
         LIMIT ? OFFSET ?
        """,
        [*params, size, (page - 1) * size],
    ).fetchall()
    return {"items": [dict(row) for row in rows], "total": total, "page": page, "size": size}


# ===========================================================================
# 给客户端的配置快照
# ===========================================================================

def config_payload(
    conn: sqlite3.Connection,
    *,
    app_version: str = "",
    channel: str = "",
) -> dict[str, Any]:
    settings = read_settings(conn)
    use_channel = (channel or settings["channel"] or "stable").strip().lower()
    if use_channel not in ("stable", "beta"):
        use_channel = "stable"

    release = latest_release(conn, use_channel)
    if release is None and use_channel != "stable":
        release = latest_release(conn, "stable")

    latest = (release or {}).get("version") or ""
    required = False
    if app_version:
        if settings["min_version"] and older_than(app_version, settings["min_version"]):
            required = True
        if release and int(release.get("forced") or 0) and older_than(app_version, latest):
            required = True

    return {
        "server_time": now_ts(),
        "maintenance": settings["maintenance"],
        "message": settings["message"],
        "features": settings["features"],
        "notices": [
            {
                "id": item["id"],
                "title": item["title"],
                "body": item["body"],
                "level": item["level"],
            }
            for item in notice_rows(conn, only_live=True)
        ],
        "release": None
        if release is None
        else {
            "version": release["version"],
            "channel": release["channel"],
            "notes": release["notes"],
            "url": release["url"],
            "sha256": release["sha256"],
            "byte_size": int(release["byte_size"] or 0),
            "forced": bool(release["forced"]),
            "published_at": int(release["created_at"] or 0),
        },
        "latest_version": latest,
        "min_version": settings["min_version"],
        "update_required": required,
        "heartbeat_seconds": settings["heartbeat_seconds"],
        "ms_login_available": ms_configured(),
    }


# ===========================================================================
# 微软登录代理
#
# 流程（全部在服务器侧）：
#   devicecode → 用户去网页输 8 位码 → token → XBL → XSTS → login_with_xbox
#   → profile → 返回 {access_token, id, name, xuid} 给启动器
#
# 启动器永远拿不到 client_id，也不需要知道租户是谁。
# ===========================================================================

AAD = "https://login.microsoftonline.com"
XBL_AUTH = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_AUTH = "https://xsts.auth.xboxlive.com/xsts/authorize"
MC_AUTH = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_PROFILE = "https://api.minecraftservices.com/minecraft/profile"

SCOPE = "XboxLive.signin offline_access"

FLOW_TTL_SECONDS = 900          # 设备码本身 15 分钟过期，这里同口径
FLOW_PER_DEVICE_HOUR = 10       # 同一台设备每小时最多发起几次
FLOW_HARD_LIMIT = 4000          # 表里最多留多少条，防止被人刷爆

MS_ERRORS: dict[str, str] = {
    "AADSTS700016": "服务器上配置的 client_id 在租户里不存在，请检查 .env 的 MCFR_MS_CLIENT_ID",
    "AADSTS7000218": "服务器上那个应用没有开启「允许公共客户端流」",
    "AADSTS65001": "那个应用还没有被授予 XboxLive.signin 权限",
    "AADSTS50020": "这个微软账号不属于配置的租户",
    "AADSTS9002346": "应用注册成了「仅个人 Microsoft 帐户」，只能用 /consumers 端点（服务器会自动处理）",
    "AADSTS900561": "应用配置不完整（缺少必要的重定向 URI 或客户端类型）",
}

XERR: dict[str, str] = {
    "2148916233": "这个微软账号还没有注册 Xbox 账号，请先去 xbox.com 创建一个",
    "2148916235": "该微软账号所在地区不支持 Xbox Live",
    "2148916236": "该账号需要先在网页上完成成年人验证",
    "2148916237": "该账号需要先在网页上完成成年人验证",
    "2148916238": "这是未成年账号，需要由家长加入家庭组后才能登录",
}


def ms_client_id() -> str:
    return (os.environ.get("MCFR_MS_CLIENT_ID") or "").strip()


def ms_configured() -> bool:
    return bool(ms_client_id())


def _authorities() -> list[str]:
    """依次尝试的 authority。

    个人账户应用只能用 consumers（用租户端会报 AADSTS9002346），
    多租户应用走 common 也通，只允许本组织的应用最后才用配置的租户。
    ！顺序不能改：这是实测出来的，「先通用、后专用」。
    """
    tenant = (os.environ.get("MCFR_MS_TENANT") or "").strip()
    chain = ["consumers", "common"]
    if tenant and tenant not in chain:
        chain.append(tenant)
    return chain


def _http(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 15,
) -> tuple[int, str]:
    request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    request.add_header("User-Agent", USER_AGENT)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if exc.fp else ""
        return int(exc.code), body
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, f"网络错误：{exc}"


def _post_form(url: str, form: dict[str, str], timeout: int = 15) -> tuple[int, str]:
    body = urllib.parse.urlencode(form).encode("utf-8")
    return _http(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        timeout=timeout,
    )


def _post_json(url: str, payload: Any, bearer: str = "", timeout: int = 15) -> tuple[int, str]:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    return _http(url, data=body, headers=headers, timeout=timeout)


def _translate_aad(text: str) -> str:
    code = re.search(r"AADSTS\d+", text or "")
    if code and code.group(0) in MS_ERRORS:
        return f"{MS_ERRORS[code.group(0)]}（{code.group(0)}）"
    cleaned = re.sub(r"<[^>]+>", " ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:300] or "微软返回了未知错误"


def _translate_xerr(text: str) -> str:
    match = re.search(r'"XErr"\s*:\s*(\d+)', text or "")
    if match and match.group(1) in XERR:
        return XERR[match.group(1)]
    return ""


def cleanup_flows(conn: sqlite3.Connection) -> None:
    """删掉过期很久的登录会话。每次发起新登录时顺手跑一次，不需要额外线程。"""
    conn.execute("DELETE FROM launcher_ms_flows WHERE expires_at < ?", (now_ts() - 600,))
    total = int(conn.execute("SELECT COUNT(*) FROM launcher_ms_flows").fetchone()[0])
    if total > FLOW_HARD_LIMIT:
        conn.execute(
            """
            DELETE FROM launcher_ms_flows WHERE id IN (
                SELECT id FROM launcher_ms_flows ORDER BY created_at ASC LIMIT ?
            )
            """,
            (total - FLOW_HARD_LIMIT,),
        )


def start_device_flow(
    conn: sqlite3.Connection, device_id: str = "", ip: str = ""
) -> dict[str, Any]:
    """向微软要一个设备码，并把 device_code 留在服务器上。"""
    client_id = ms_client_id()
    if not client_id:
        raise RuntimeError("服务器还没有配置微软应用凭据（.env 的 MCFR_MS_CLIENT_ID）")

    # ！开关关了就直接拒，不要在服务端还留一条能走的暗门：
    #   客户端藏按钮只是「看不见」，真正的边界在这里。
    if not read_settings(conn)["features"].get("microsoft_login"):
        raise RuntimeError("站长已关闭微软正版登录，请使用离线账号")

    device = ""
    if device_id:
        device = clean_device_id(device_id)
        recent = int(
            conn.execute(
                "SELECT COUNT(*) FROM launcher_ms_flows WHERE device_id = ? AND created_at >= ?",
                (device, now_ts() - 3600),
            ).fetchone()[0]
        )
        if recent >= FLOW_PER_DEVICE_HOUR:
            raise RuntimeError("登录请求太频繁了，请过一会儿再试")

    cleanup_flows(conn)

    last_error = ""
    for authority in _authorities():
        status, text = _post_form(
            f"{AAD}/{authority}/oauth2/v2.0/devicecode",
            {"client_id": client_id, "scope": SCOPE},
        )
        if status == 200:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                last_error = "微软返回的内容无法解析"
                continue
            flow_id = secrets.token_hex(24)
            now = now_ts()
            conn.execute(
                """
                INSERT INTO launcher_ms_flows
                    (id, device_id, authority, user_code, device_code, status,
                     interval_s, expires_at, created_at, last_poll, result, error)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, 0, '', '')
                """,
                (
                    flow_id,
                    device,
                    authority,
                    str(payload.get("user_code") or "")[:16],
                    str(payload.get("device_code") or "")[:4096],
                    max(3, _to_int(payload.get("interval"), 5)),
                    now + min(FLOW_TTL_SECONDS, _to_int(payload.get("expires_in"), FLOW_TTL_SECONDS)),
                    now,
                ),
            )
            print(f"[{BRAND}] 启动器微软登录：已下发设备码（authority={authority}，{ip or '未知来源'}）")
            return {
                "flow_id": flow_id,
                "user_code": payload.get("user_code") or "",
                "verification_uri": payload.get("verification_uri")
                or payload.get("verification_url")
                or "https://www.microsoft.com/link",
                "expires_in": _to_int(payload.get("expires_in"), FLOW_TTL_SECONDS),
                "interval": max(3, _to_int(payload.get("interval"), 5)),
                "message": payload.get("message") or "",
            }
        last_error = _translate_aad(text)

    raise RuntimeError(f"获取设备码失败：{last_error or '请检查服务器网络'}")


def poll_device_flow(
    conn: sqlite3.Connection, flow_id: str, device_id: str = ""
) -> dict[str, Any]:
    """客户端来问「授权好了没」。

    每次都只在**服务端**推进这个状态机；客户端拿到 done 之后
    就把 flow 删掉，同一张设备码不会被使用两次。
    """
    flow = conn.execute(
        "SELECT * FROM launcher_ms_flows WHERE id = ?", ((flow_id or "").strip(),)
    ).fetchone()
    if flow is None:
        raise KeyError("登录会话不存在或已经结束，请重新发起登录")
    row = dict(flow)
    if device_id and row.get("device_id") and clean_device_id(device_id) != row["device_id"]:
        raise KeyError("登录会话不存在或已经结束，请重新发起登录")

    if row["status"] == "done":
        return {"status": "done", "minecraft": json.loads(row["result"] or "{}")}
    if row["status"] == "error":
        return {"status": "error", "message": row["error"] or "登录失败"}

    now = now_ts()
    if now >= int(row["expires_at"]):
        conn.execute(
            "UPDATE launcher_ms_flows SET status = 'error', error = ? WHERE id = ?",
            ("设备码已过期，请重新发起登录", row["id"]),
        )
        return {"status": "error", "message": "设备码已过期，请重新发起登录"}

    interval = max(3, int(row["interval_s"] or 5))
    if now - int(row["last_poll"] or 0) < interval:
        return {"status": "pending", "retry_after": interval}
    conn.execute(
        "UPDATE launcher_ms_flows SET last_poll = ? WHERE id = ?", (now, row["id"])
    )

    client_id = ms_client_id()
    if not client_id:
        return {"status": "error", "message": "服务器还没有配置微软应用凭据"}

    status, text = _post_form(
        f"{AAD}/{row['authority']}/oauth2/v2.0/token",
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": client_id,
            "device_code": row["device_code"],
        },
    )

    if status != 200:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {}
        code = str(payload.get("error") or "")
        if code == "authorization_pending":
            return {"status": "pending", "retry_after": interval}
        if code == "slow_down":
            interval += 5
            conn.execute(
                "UPDATE launcher_ms_flows SET interval_s = ? WHERE id = ?",
                (interval, row["id"]),
            )
            return {"status": "pending", "retry_after": interval}
        message = (
            "你在网页上拒绝了授权"
            if code == "authorization_declined"
            else "设备码已过期，请重新发起登录"
            if code == "expired_token"
            else _translate_aad(text) or code or "微软登录失败"
        )
        conn.execute(
            "UPDATE launcher_ms_flows SET status = 'error', error = ? WHERE id = ?",
            (message, row["id"]),
        )
        return {"status": "error", "message": message}

    try:
        ms_access = str(json.loads(text).get("access_token") or "")
    except json.JSONDecodeError:
        ms_access = ""
    if not ms_access:
        message = "微软没有返回访问令牌"
        conn.execute(
            "UPDATE launcher_ms_flows SET status = 'error', error = ? WHERE id = ?",
            (message, row["id"]),
        )
        return {"status": "error", "message": message}

    try:
        result = _complete_minecraft_login(ms_access)
    except RuntimeError as exc:
        conn.execute(
            "UPDATE launcher_ms_flows SET status = 'error', error = ? WHERE id = ?",
            (str(exc), row["id"]),
        )
        return {"status": "error", "message": str(exc)}

    # ！成功后把 expires_at 压缩到 5 分钟内：令牌只在库里待到客户端取走为止。
    #   保留一小段是为了「网络抖一下、客户端重试一次」仍然拿得到，
    #   而不是一取走就删 —— 那样一次丢包就得让用户重新走一遍网页授权。
    conn.execute(
        "UPDATE launcher_ms_flows SET status = 'done', result = ?, expires_at = ? WHERE id = ?",
        (json.dumps(result, ensure_ascii=False), now_ts() + 300, row["id"]),
    )
    print(f"[{BRAND}] 启动器微软登录成功：{result.get('name')}（设备 {row['device_id'] or '未知'}）")
    return {"status": "done", "minecraft": result}


def _complete_minecraft_login(ms_access: str) -> dict[str, Any]:
    """微软令牌 → Xbox Live → XSTS → Minecraft 令牌 → 档案。

    ！这几步必须一次跑完：中间任何一步的返回值都只在这几秒内有用，
      拆成「客户端分步调用」只会把中间令牌暴露给客户端。
    """
    status, text = _post_json(
        XBL_AUTH,
        {
            "Properties": {
                "AuthMethod": "RPS",
                "SiteName": "user.auth.xboxlive.com",
                "RpsTicket": f"d={ms_access}",
            },
            "RelyingParty": "http://auth.xboxlive.com",
            "TokenType": "JWT",
        },
    )
    if status != 200:
        raise RuntimeError(f"Xbox Live 授权失败：{_translate_aad(text) if 'AADSTS' in text else text[:160]}")
    try:
        xbl = json.loads(text)
        xbl_token = str(xbl["Token"])
    except (json.JSONDecodeError, KeyError, TypeError):
        raise RuntimeError("Xbox Live 返回的结构无法识别") from None

    status, text = _post_json(
        XSTS_AUTH,
        {
            "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl_token]},
            "RelyingParty": "rp://api.minecraftservices.com/",
            "TokenType": "JWT",
        },
    )
    if status != 200:
        message = _translate_xerr(text)
        if not message:
            raise RuntimeError(f"Xbox 授权失败：{text[:160]}")
        raise RuntimeError(message)
    try:
        xsts = json.loads(text)
        xsts_token = str(xsts["Token"])
        uhs = str(xsts["DisplayClaims"]["xui"][0]["uhs"])
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        raise RuntimeError("XSTS 返回的结构无法识别") from None

    status, text = _post_json(
        MC_AUTH, {"identityToken": f"XBL3.0 x={uhs};{xsts_token}"}
    )
    if status == 403 and "Invalid app registration" in text:
        raise RuntimeError(
            "Minecraft 拒绝了这次授权：这个应用没有被 Mojang 加入 API 白名单。"
            "该白名单是人工审核且不提供申诉渠道，第三方启动器基本拿不到。"
            "请到后台把「微软正版登录」关掉，让用户改用离线账号或官方启动器。"
        )
    if status != 200:
        raise RuntimeError(f"Minecraft 授权失败：{text[:160]}")
    try:
        mc_access = str(json.loads(text)["access_token"])
    except (json.JSONDecodeError, KeyError, TypeError):
        raise RuntimeError("Minecraft 没有返回访问令牌") from None

    status, text = _http(
        MC_PROFILE, headers={"Authorization": f"Bearer {mc_access}", "Accept": "application/json"}
    )
    if status == 404:
        raise RuntimeError("这个微软账号没有购买 Minecraft: Java 版，无法用于正版登录")
    if status != 200:
        raise RuntimeError(f"获取 Minecraft 档案失败（HTTP {status}）")
    try:
        profile = json.loads(text)
        return {
            "access_token": mc_access,
            "id": str(profile["id"]),
            "name": str(profile["name"]),
            "xuid": uhs,
        }
    except (json.JSONDecodeError, KeyError, TypeError):
        raise RuntimeError("Minecraft 档案返回的结构无法识别") from None


def drop_flow(conn: sqlite3.Connection, flow_id: str) -> bool:
    """主动丢弃一个登录会话（用户点了取消、或者拿到了错误的账号）。"""
    cursor = conn.execute(
        "DELETE FROM launcher_ms_flows WHERE id = ?", ((flow_id or "").strip(),)
    )
    return cursor.rowcount > 0
