"""模组镜像：同步索引、挑选要镜像的文件、把它们下到本站。

三层职责刻意分开：
    modrinth.py   只管「怎么跟 Modrinth 说话」
    mirror.py     管「同步什么、选哪些、下到哪、任务怎么记」
    routers/      只管 HTTP 出入口与权限

！所有耗时操作都跑在后台线程里，HTTP 请求立刻返回 job_id。
  同步要发 21 个请求、下载可能要几分钟，让请求挂着等一定会超时。

！任务状态写在数据库里而不是内存里：进程重启后进度不会凭空消失，
  后台也能看到「上次同步是什么时候、成没成」。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

from . import database, modrinth
from .database import now_ts

BRAND = "mcfr"

BASE_DIR = Path(__file__).resolve().parent.parent
MIRROR_DIR = BASE_DIR / "data" / "mirror"
ICON_DIR = MIRROR_DIR / "icons"

# 图标允许的后缀与对应的 MIME。
# ！必须白名单：文件名是从远程 URL 推出来的，而它会被直接当 Content-Type 发出去，
#   配合 X-Content-Type-Options: nosniff，类型写错图就不显示。
ICON_SUFFIXES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
MAX_ICON_BYTES = 1024 * 1024

# 一个模组最多选多少个文件。
# ！必须有个上限：热门模组的版本列表动辄几百条，「把游戏版本全勾上」
#   能一次拉几百个文件下来。超了只保留最新的这些，并在界面上说明。
MAX_SELECTED_PER_MOD = 24

# 默认同步多少个模组
DEFAULT_SYNC_LIMIT = 20

# 多久没有「进展」就认定任务被中断了（进程重启、卡死等）。
# ！这是在**没有进展**的情况下算的，不是从任务开始算 ——
#   看的是 mirror_jobs.touched_at，每次 _update_job 都会刷新它。
#   早期版本看的是 started_at，于是「下 24 个文件」这种合法长任务
#   跑够 30 分钟就会被当成卡死放行，让两条任务同时跑。
STALE_JOB_SECONDS = 30 * 60

_job_lock = threading.Lock()


class MirrorError(RuntimeError):
    """镜像相关操作失败，携带给用户看的中文提示。"""


# ---------------------------------------------------------------- 小工具

_UNSAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")


def safe_segment(text: Any, fallback: str = "file") -> str:
    """把一段文字变成可以安全当文件名用的字符串。

    路径分隔符、..、空格、中文全部替换掉 —— 文件名是给操作系统看的，
    用户看到的原始名字存在数据库里，下载时通过 Content-Disposition 给出。
    """
    cleaned = _UNSAFE_SEGMENT.sub("_", str(text or "")).strip("._")
    return (cleaned or fallback)[:96]


def file_path(mod_id: str, file_name: str) -> Path:
    """某个文件在本地应该落在哪。

    ！两道闸防路径穿越：safe_segment 已经把 / 和 .. 换掉了，
      这里再确认一次落点确实在镜像目录里 —— 路径穿越一旦发生就是任意文件写入。
    """
    root = MIRROR_DIR.resolve()
    target = (root / safe_segment(mod_id) / safe_segment(file_name)).resolve()
    if not target.is_relative_to(root):
        raise MirrorError("文件路径不合法")
    return target


def icon_path(name: str) -> Path:
    """图标缓存文件的绝对路径。name 是我们自己生成的，仍然再夹一道。"""
    root = ICON_DIR.resolve()
    target = (root / safe_segment(name, "icon.png")).resolve()
    if not target.is_relative_to(root):
        raise MirrorError("图标路径不合法")
    return target


def icon_mime(name: str) -> str:
    return ICON_SUFFIXES.get(Path(name).suffix.lower(), "image/png")


def as_list(value: Any) -> list[str]:
    """同时兼容「Python 列表」和「数据库里的 JSON 字符串」。"""
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    return []


def _dump(items: Any) -> str:
    return json.dumps(sorted(set(as_list(items))), ensure_ascii=False)


def version_sort_key(text: str) -> tuple:
    """把 "1.10" 排在 "1.9" 后面（纯字符串比较会把它们弄反）。"""
    parts = re.split(r"[.\-_+]", str(text))
    key: list[tuple[int, int, str]] = []
    for part in parts:
        if part.isdigit():
            key.append((0, int(part), ""))
        else:
            key.append((1, 0, part))
    return tuple(key)


def sort_game_versions(items: Any) -> list[str]:
    """倒序排列游戏版本：正式版在前（1.21 → 1.20），快照和预发布排在后面。"""
    values = sorted(set(as_list(items)))
    release = [v for v in values if re.fullmatch(r"\d+(\.\d+)*", v)]
    others = [v for v in values if v not in release]
    release.sort(key=version_sort_key, reverse=True)
    others.sort(reverse=True)
    return release + others


# ---------------------------------------------------------------- 挑选规则

def _releases(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row.get("version_type") == "release"]


def pick_versions(rows: list[dict], loaders: list[str],
                  picked_versions: list[str]) -> set[str]:
    """决定一个模组要镜像哪些文件。

    规则（和界面上写的必须完全一致，不然用户会觉得「我勾了却没下」）：

      对每个加载器：
        · 勾了游戏版本 → 对每个勾中的版本，取覆盖它的最新 release
        · 没勾        → 取该加载器下最新 release 所覆盖的那些游戏版本
                        （即「默认每个加载器只抓一个最新 release」）

      同一个版本只会被选中一次；超过 MAX_SELECTED_PER_MOD 时只留最新的。
    """
    chosen: dict[str, int] = {}

    for loader in loaders:
        pool = [row for row in rows if loader in row["loaders"] and row["download_url"]]
        if not pool:
            continue

        if picked_versions:
            targets = list(picked_versions)
        else:
            newest = max(_releases(pool) or pool, key=lambda r: r["published_at"])
            targets = list(newest["game_versions"])

        for game_version in targets:
            matches = [r for r in pool if game_version in r["game_versions"]]
            if not matches:
                continue
            best = max(_releases(matches) or matches, key=lambda r: r["published_at"])
            chosen[best["id"]] = best["published_at"]

    if len(chosen) > MAX_SELECTED_PER_MOD:
        newest_first = sorted(chosen.items(), key=lambda item: item[1], reverse=True)
        chosen = dict(newest_first[:MAX_SELECTED_PER_MOD])

    return set(chosen)


def recompute_selection(conn: sqlite3.Connection, mod_id: str) -> int:
    """按当前设置重算这个模组「哪些文件该镜像」，返回选中数量。"""
    mod = conn.execute("SELECT * FROM mirror_mods WHERE id = ?", (mod_id,)).fetchone()
    if mod is None:
        return 0

    rows = [dict(row) for row in conn.execute(
        "SELECT * FROM mirror_files WHERE mod_id = ?", (mod_id,)
    ).fetchall()]
    for row in rows:
        row["loaders"] = as_list(row["loaders"])
        row["game_versions"] = as_list(row["game_versions"])

    wanted: set[str] = set()
    if mod["mirrored"]:
        loaders = as_list(mod["mirror_loaders"]) or as_list(mod["loaders"])
        wanted = pick_versions(rows, loaders, as_list(mod["mirror_versions"]))

    for row in rows:
        mark = 1 if row["id"] in wanted else 0
        if mark != row["selected"]:
            conn.execute("UPDATE mirror_files SET selected = ? WHERE id = ?", (mark, row["id"]))
    conn.commit()
    return len(wanted)


def set_mod_options(conn: sqlite3.Connection, mod_id: str, *,
                    mirrored: bool | None = None,
                    loaders: Any = None, versions: Any = None) -> dict:
    """管理员对单个模组设置「要不要镜像、镜像哪些」。"""
    mod = conn.execute("SELECT * FROM mirror_mods WHERE id = ?", (mod_id,)).fetchone()
    if mod is None:
        raise MirrorError("没有这个模组，先同步一次索引")

    updates: dict[str, Any] = {}
    if mirrored is not None:
        updates["mirrored"] = 1 if mirrored else 0
    if loaders is not None:
        updates["mirror_loaders"] = _dump(loaders)
    if versions is not None:
        updates["mirror_versions"] = _dump(versions)

    if updates:
        columns = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(f"UPDATE mirror_mods SET {columns} WHERE id = ?",
                     (*updates.values(), mod_id))
        conn.commit()

    recompute_selection(conn, mod_id)
    return mod_detail(conn, mod_id) or {}


# ---------------------------------------------------------------- 任务

def _update_job(conn: sqlite3.Connection, job_id: int, **fields: Any) -> None:
    if not fields:
        return
    # ！每次更新都顺手刷新心跳（touched_at）：
    #   「这个任务还活着」这件事就是靠它证明的，见 _sweep_stale_jobs。
    fields["touched_at"] = now_ts()
    # 列名全部来自本文件的字面量，不含用户输入
    columns = ", ".join(f"{key} = ?" for key in fields)
    conn.execute(f"UPDATE mirror_jobs SET {columns} WHERE id = ?",
                 (*fields.values(), job_id))
    conn.commit()


def _finish_job(conn: sqlite3.Connection, job_id: int, status: str, detail: str) -> None:
    _update_job(conn, job_id, status=status, detail=detail, finished_at=now_ts())


def _sweep_stale_jobs(conn: sqlite3.Connection) -> None:
    """把被中断的任务从 running 里放出来。

    ！不做这一步的话，进程重启后那条 running 会永远卡在那里，
      之后所有镜像操作都会被告知「已经有任务在跑」，而且没有任何办法解开。

    ！判据是「多久没有进展」而不是「跑了多久」：
      看开始时间的话，一个正在慢慢下 24 个文件的合法任务会被误判成卡死，
      然后被放行去和另一条任务同时跑。心跳把误判和真卡死分开了。
    """
    stamp = now_ts()
    conn.execute(
        "UPDATE mirror_jobs SET status = 'failed', finished_at = ?,"
        " detail = '任务被中断（进程重启或长时间无进展）'"
        " WHERE status = 'running' AND touched_at < ?",
        (stamp, stamp - STALE_JOB_SECONDS),
    )
    conn.commit()


def start_job(conn: sqlite3.Connection, kind: str, *, scope: str = "",
              operator: str = "", limit: int = DEFAULT_SYNC_LIMIT,
              auto: bool = False) -> int:
    """建一条任务记录并丢到后台线程执行，立刻返回 job_id。

    auto=True 表示这是每日自动同步起的（而不是管理员手点的），
    会记进 mirror_jobs.auto，后台列表里能一眼看出来。
    """
    if kind not in ("sync", "download"):
        raise MirrorError(f"未知的任务类型：{kind}")

    with _job_lock:
        _sweep_stale_jobs(conn)
        running = conn.execute(
            "SELECT id, kind FROM mirror_jobs WHERE status = 'running' LIMIT 1"
        ).fetchone()
        if running is not None:
            raise MirrorError(
                f"已经有一个「{'同步' if running['kind'] == 'sync' else '下载'}」任务在跑了，"
                "等它结束再试"
            )

        cursor = conn.execute(
            "INSERT INTO mirror_jobs(kind, auto, status, scope, detail, operator, started_at, touched_at)"
            " VALUES (?, ?, 'running', ?, '', ?, ?, ?)",
            (kind, 1 if auto else 0, scope, operator, now_ts(), now_ts()),
        )
        job_id = int(cursor.lastrowid)
        conn.commit()

    if kind == "sync":
        worker = threading.Thread(
            target=_do_sync, args=(job_id, limit), name=f"mirror-sync-{job_id}", daemon=True
        )
    else:
        worker = threading.Thread(
            target=_do_download, args=(job_id, scope or None),
            name=f"mirror-download-{job_id}", daemon=True
        )
    worker.start()
    return job_id


def jobs(conn: sqlite3.Connection, limit: int = 12) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM mirror_jobs ORDER BY id DESC LIMIT ?", (max(1, min(limit, 50)),)
    ).fetchall()
    return [dict(row) for row in rows]


def job(conn: sqlite3.Connection, job_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM mirror_jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------- 每日自动同步
#
# 三项设置存在 settings 表里（见 database.py 的建表注释）。
#
# ！key 统一带 `mirror.` 前缀：settings 是一张各模块共用的表，
#   前缀是唯一的隔断方式 —— 不带前缀的话，以后别的模块也想要一个
#   叫 `enabled` 的开关，两边就会静默地互相覆盖。
KEY_AUTO_ENABLED = "mirror.auto_sync"
KEY_AUTO_HOUR = "mirror.auto_hour"
KEY_SYNC_LIMIT = "mirror.sync_limit"
KEY_AUTO_LAST_DAY = "mirror.auto_last_day"

DEFAULT_AUTO_HOUR = 4
SYNC_LIMIT_MIN = 1
SYNC_LIMIT_MAX = modrinth.MAX_SYNC_MODS

# 调度线程多久醒一次。60 秒足够 —— 这是个「每天一次」的任务，
# 醒来发现还没到点就接着睡，开销可以忽略。
SCHEDULER_TICK_SECONDS = 60


def _as_int(value: Any, default: int, low: int, high: int) -> int:
    """把设置里存着的字符串转成有范围的整数。转不动就用默认值。"""
    try:
        return min(max(int(str(value).strip()), low), high)
    except (TypeError, ValueError):
        return default


def auto_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    """读「每日自动同步」的设置。

    ！类型转换只在这一个地方做。settings 表里存的**全是字符串**，
      散落在各处 int() / bool() 的话，迟早有一处忘了处理空串而抛异常 ——
      而这里抛异常等于整个镜像页打不开。
    """
    raw = database.settings_map(conn, "mirror.")
    return {
        "enabled": str(raw.get(KEY_AUTO_ENABLED, "0")).strip() == "1",
        "hour": _as_int(raw.get(KEY_AUTO_HOUR), DEFAULT_AUTO_HOUR, 0, 23),
        "limit": _as_int(raw.get(KEY_SYNC_LIMIT), DEFAULT_SYNC_LIMIT,
                         SYNC_LIMIT_MIN, SYNC_LIMIT_MAX),
        "last_day": str(raw.get(KEY_AUTO_LAST_DAY, "")).strip(),
    }


def save_auto_settings(conn: sqlite3.Connection, *, enabled: bool | None = None,
                       hour: int | None = None, limit: int | None = None) -> dict[str, Any]:
    """改设置。只传想改的那几项，其余保持不动。"""
    values: dict[str, str] = {}
    if enabled is not None:
        values[KEY_AUTO_ENABLED] = "1" if enabled else "0"
    if hour is not None:
        values[KEY_AUTO_HOUR] = str(_as_int(hour, DEFAULT_AUTO_HOUR, 0, 23))
    if limit is not None:
        values[KEY_SYNC_LIMIT] = str(
            _as_int(limit, DEFAULT_SYNC_LIMIT, SYNC_LIMIT_MIN, SYNC_LIMIT_MAX)
        )
    database.set_settings(conn, values)
    return auto_settings(conn)


def auto_sync_due(conn: sqlite3.Connection, now: datetime | None = None) -> bool:
    """今天到点了吗、而且今天还没跑过？"""
    settings = auto_settings(conn)
    if not settings["enabled"]:
        return False

    now = now or datetime.now()
    if settings["last_day"] == now.strftime("%Y-%m-%d"):
        return False                     # 今天已经跑过

    # ！写成「过了设定的钟点」而不是「正好等于那个钟点」：
    #   这样服务器在设定时间点前后重启、或者夜里关机白天才开，
    #   只要当天还没跑过，起来就会补上，不会白白跳过一整天。
    return now.hour >= settings["hour"]


def maybe_auto_sync(conn: sqlite3.Connection) -> str:
    """到点就起一个同步任务。返回一句给日志用的人话（不需要跑时返回空串）。"""
    if not auto_sync_due(conn):
        return ""

    settings = auto_settings(conn)
    try:
        job_id = start_job(conn, "sync", operator="每日自动", limit=settings["limit"],
                           auto=True)
    except MirrorError as exc:
        # 有别的任务在跑。！**不**记 last_day，下一分钟自然还会再试一次 ——
        #   否则一次撞车就能让当天的自动同步直接泡汤。
        return f"自动同步推迟：{exc}"

    database.set_settings(
        conn, {KEY_AUTO_LAST_DAY: datetime.now().strftime("%Y-%m-%d")}
    )
    return f"自动同步已开始（任务 #{job_id}，取下载量前 {settings['limit']} 名）"


def auto_sync_loop(stop: threading.Event) -> None:
    """【后台线程】每天到点跑一次同步，直到 stop 被 set。

    ！为什么不用 crontab：这个项目要的是「一条命令就能跑起来」。
      多一个 crontab 就多一处「换机器时忘了配」的地方，而且站在
      systemd 外面，日志也分成两半。一个会睡觉的线程没这些麻烦。

    ！为什么不用 APScheduler 之类：零第三方依赖是硬约束。

    ！为什么只管「起任务」而不管「怎么跑」：真正的同步还是走 start_job
      那条路，所以后台的进度条、任务历史、失败语义全都是一致的 ——
      自动和手动跑出来的东西长得一模一样。
    """
    while True:
        try:
            conn = database.connect()
            try:
                note = maybe_auto_sync(conn)
            finally:
                conn.close()
            if note:
                print(f"[{BRAND}] 模组镜像：{note}")
        except Exception as exc:  # noqa: BLE001
            # ！绝不能让异常漏出去：这个线程一死，自动同步就永远静默失效了，
            #   而界面上那个开关还显示着「已开启」，谁都发现不了。
            print(f"[{BRAND}] 模组镜像：自动同步检查出错"
                  f"（{type(exc).__name__}: {exc}）")

        if stop.wait(SCHEDULER_TICK_SECONDS):
            return


# ---------------------------------------------------------------- 同步

def _cache_icon(conn: sqlite3.Connection, mod_id: str, url: str) -> str:
    """把模组图标抓一份到本地缓存。成功返回空串，否则返回一句人能读的原因。

    ！为什么不直接让浏览器去 Modrinth 的 CDN 取：
      ① CSP 是 img-src 'self'，外链图会被浏览器直接拦掉；
      ② 就算把 CSP 放开，也等于每次开页面都把用户 IP 交给对方 CDN ——
         而这个站点存在的意义恰好是「Modrinth 从这里访问不畅」。
      图标只有几 KB，同步时顺手抓一次能用很久。

    ！不抛异常（图标拿不到不该让整个同步失败），但也**不能静默吞掉**——
      「少了一个图标、没人知道为什么」是最难查的那类问题。
      所以把原因交回给调用方，由它写进任务详情里。
    """
    if not url:
        return "没有图标地址"

    row = conn.execute(
        "SELECT local_icon FROM mirror_mods WHERE id = ?", (mod_id,)
    ).fetchone()
    if row is not None and row["local_icon"] and icon_path(row["local_icon"]).exists():
        return ""

    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if suffix not in ICON_SUFFIXES:
        suffix = ".png"
    # 带上 url 的短哈希：图标换了之后文件名跟着变，浏览器不会拿到旧图
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    name = f"{safe_segment(mod_id)}-{digest}{suffix}"

    try:
        modrinth.download_file(url, icon_path(name), limit=MAX_ICON_BYTES)
    except Exception as exc:  # noqa: BLE001 - 任何原因都只记下来，不影响同步
        return f"图标没抓到（{type(exc).__name__}: {exc}）"

    conn.execute("UPDATE mirror_mods SET local_icon = ? WHERE id = ?", (name, mod_id))
    return ""


def _store_mod(conn: sqlite3.Connection, hit: dict, versions: list[dict]) -> None:
    project_id = str(hit.get("project_id") or "").strip()
    stamp = now_ts()

    loaders = sorted({
        str(loader)
        for version in versions
        for loader in as_list(version.get("loaders"))
        if str(loader) in modrinth.LOADERS
    })
    game_versions = sort_game_versions([
        str(gv)
        for version in versions
        for gv in as_list(version.get("game_versions"))
    ])

    # ！UPSERT 的 SET 列表是手写的、故意不含 mirrored / mirror_loaders /
    #   mirror_versions —— 那三列是管理员的决定，每次同步都必须原样保留。
    #   写成 DO UPDATE SET 省略列名（或整行替换）的话，同步一次就把
    #   「哪些模组要镜像」全清空了。
    conn.execute(
        """
        INSERT INTO mirror_mods
            (id, slug, title, description, icon_url, author, downloads, follows,
             categories, loaders, game_versions, published_at, updated_at, synced_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            slug = excluded.slug,
            title = excluded.title,
            description = excluded.description,
            icon_url = excluded.icon_url,
            author = excluded.author,
            downloads = excluded.downloads,
            follows = excluded.follows,
            categories = excluded.categories,
            loaders = excluded.loaders,
            game_versions = excluded.game_versions,
            published_at = excluded.published_at,
            updated_at = excluded.updated_at,
            synced_at = excluded.synced_at
        """,
        (
            project_id,
            str(hit.get("slug") or ""),
            str(hit.get("title") or project_id),
            str(hit.get("description") or ""),
            str(hit.get("icon_url") or ""),
            str(hit.get("author") or ""),
            int(hit.get("downloads") or 0),
            int(hit.get("follows") or 0),
            _dump(hit.get("display_categories") or hit.get("categories")),
            json.dumps(loaders, ensure_ascii=False),
            json.dumps(game_versions, ensure_ascii=False),
            modrinth.parse_timestamp(hit.get("date_created")),
            modrinth.parse_timestamp(hit.get("date_modified")),
            stamp,
        ),
    )

    # 只清掉「还没下到本地」的版本行。已经下过的必须留着，
    # 删了就等于把本地文件弄丢了（磁盘上有文件，但没人知道它对应哪个版本）。
    conn.execute(
        "DELETE FROM mirror_files WHERE mod_id = ? AND local_name = ''", (project_id,)
    )

    for version in versions:
        file_info = modrinth.primary_file(version)
        if not file_info:
            continue
        version_id = str(version.get("id") or "").strip()
        if not version_id:
            continue
        conn.execute(
            """
            INSERT INTO mirror_files
                (id, mod_id, name, version_number, version_type, game_versions, loaders,
                 file_name, file_size, download_url, sha1, published_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                version_number = excluded.version_number,
                version_type = excluded.version_type,
                game_versions = excluded.game_versions,
                loaders = excluded.loaders,
                file_name = excluded.file_name,
                file_size = excluded.file_size,
                download_url = excluded.download_url,
                sha1 = excluded.sha1,
                published_at = excluded.published_at
            """,
            (
                version_id,
                project_id,
                str(version.get("name") or ""),
                str(version.get("version_number") or ""),
                str(version.get("version_type") or "release"),
                json.dumps(sort_game_versions(version.get("game_versions")), ensure_ascii=False),
                json.dumps(as_list(version.get("loaders")), ensure_ascii=False),
                str(file_info.get("filename") or ""),
                int(file_info.get("size") or 0),
                str(file_info.get("url") or ""),
                modrinth.file_sha1(file_info),
                modrinth.parse_timestamp(version.get("date_published")),
            ),
        )

    conn.commit()
    note = _cache_icon(conn, project_id, str(hit.get("icon_url") or ""))
    conn.commit()
    recompute_selection(conn, project_id)
    return note


def _prune(conn: sqlite3.Connection, keep: list[str]) -> int:
    """删掉跌出榜单、且没有被镜像的模组。"""
    if not keep:
        return 0
    placeholders = ",".join("?" for _ in keep)
    rows = conn.execute(
        f"SELECT id FROM mirror_mods WHERE id NOT IN ({placeholders})", keep
    ).fetchall()

    removed = 0
    for row in rows:
        mod_id = row["id"]
        local = conn.execute(
            "SELECT 1 FROM mirror_files WHERE mod_id = ? AND local_name != '' LIMIT 1",
            (mod_id,),
        ).fetchone()
        if local is not None:
            continue
        conn.execute("DELETE FROM mirror_mods WHERE id = ?", (mod_id,))
        removed += 1
    conn.commit()
    return removed


def _do_sync(job_id: int, limit: int) -> None:
    """【后台线程】从 Modrinth 拉榜单 + 每个模组的版本清单。

    ！自己开一个数据库连接：不能借用请求那个连接，
      请求早就返回了，连接也随之关闭；而且 SQLite 连接不能跨线程复用。
    """
    conn = database.connect()
    try:
        hits = modrinth.search_top_mods(limit)
        _update_job(conn, job_id, total=len(hits),
                    detail=f"取到 {len(hits)} 个热门模组，正在读取版本清单…")

        seen: list[str] = []
        failed = 0
        icon_notes: list[str] = []
        for index, hit in enumerate(hits, start=1):
            project_id = str(hit.get("project_id") or "").strip()
            if not project_id:
                continue
            seen.append(project_id)
            try:
                versions = modrinth.project_versions(project_id)
                note = _store_mod(conn, hit, versions)
                if note:
                    icon_notes.append(f"{hit.get('title') or project_id}：{note}")
            except modrinth.ModrinthError as exc:
                failed += 1
                _update_job(conn, job_id, failed=failed,
                            detail=f"{hit.get('title') or project_id}：{exc}")
            _update_job(conn, job_id, done=index)

        removed = _prune(conn, seen)
        summary = (f"同步完成：{len(seen) - failed} 个成功、{failed} 个失败"
                   + (f"，清理了 {removed} 个跌出榜单的模组" if removed else ""))
        if icon_notes:
            # 图标失败不影响使用（前端会退回占位方块），但必须说出来，
            # 否则「为什么这一项没有图标」会变成一个只能靠猜的问题。
            summary += f"；{len(icon_notes)} 个图标没抓到：" + "，".join(icon_notes[:3])
        _finish_job(conn, job_id, "ok" if failed == 0 else "failed", summary)
    except modrinth.ModrinthError as exc:
        _finish_job(conn, job_id, "failed", str(exc))
    except Exception as exc:  # noqa: BLE001 - 后台线程里漏出去就没人知道任务为什么卡住
        _finish_job(conn, job_id, "failed", f"意外错误：{type(exc).__name__}: {exc}")
    finally:
        conn.close()


# ---------------------------------------------------------------- 下载

def _do_download(job_id: int, mod_id: str | None) -> None:
    """【后台线程】把选中的文件下到本地。"""
    conn = database.connect()
    try:
        sql = "SELECT * FROM mirror_files WHERE selected = 1 AND local_name = ''"
        params: tuple = ()
        if mod_id:
            sql += " AND mod_id = ?"
            params = (mod_id,)
        todo = conn.execute(sql, params).fetchall()

        _update_job(conn, job_id, total=len(todo))
        if not todo:
            _finish_job(conn, job_id, "ok", "没有待下载的文件（都已经下过了）")
            return

        done = failed = 0
        for row in todo:
            item = dict(row)
            try:
                target = file_path(item["mod_id"], item["file_name"])
                size = modrinth.download_file(
                    item["download_url"], target, expected_sha1=item["sha1"]
                )
                conn.execute(
                    "UPDATE mirror_files SET local_name = ?, local_size = ?, local_at = ?"
                    " WHERE id = ?",
                    (target.name, size, now_ts(), item["id"]),
                )
                conn.commit()
                done += 1
            except (modrinth.ModrinthError, MirrorError) as exc:
                failed += 1
                _update_job(conn, job_id, failed=failed,
                            detail=f"{item['file_name'] or item['id']}：{exc}")
            _update_job(conn, job_id, done=done)

        _finish_job(
            conn, job_id, "ok" if failed == 0 else "failed",
            f"下载完成：成功 {done} 个、失败 {failed} 个",
        )
    except Exception as exc:  # noqa: BLE001
        _finish_job(conn, job_id, "failed", f"意外错误：{type(exc).__name__}: {exc}")
    finally:
        conn.close()


# ---------------------------------------------------------------- 查询

def _counts(conn: sqlite3.Connection) -> dict[str, Any]:
    selected = int(conn.execute(
        "SELECT COUNT(*) FROM mirror_files WHERE selected = 1"
    ).fetchone()[0])
    local_row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(local_size), 0) FROM mirror_files WHERE local_name != ''"
    ).fetchone()
    return {
        "selected": selected,
        "local_files": int(local_row[0]),
        "local_bytes": int(local_row[1]),
        "pending": int(conn.execute(
            "SELECT COUNT(*) FROM mirror_files WHERE selected = 1 AND local_name = ''"
        ).fetchone()[0]),
    }


def overview(conn: sqlite3.Connection) -> dict[str, Any]:
    """镜像总览，用户页和后台都用它。"""
    mod_row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(downloads), 0), MAX(synced_at) FROM mirror_mods"
    ).fetchone()
    latest = conn.execute(
        "SELECT * FROM mirror_jobs ORDER BY id DESC LIMIT 1"
    ).fetchone()

    result: dict[str, Any] = {
        "mod_count": int(mod_row[0]),
        "total_downloads": int(mod_row[1]),
        "synced_at": int(mod_row[2] or 0),
        "mirrored_mods": int(conn.execute(
            "SELECT COUNT(*) FROM mirror_mods WHERE mirrored = 1"
        ).fetchone()[0]),
        "size_limit_per_mod": MAX_SELECTED_PER_MOD,
        "latest_job": dict(latest) if latest is not None else None,
    }
    result.update(_counts(conn))
    return result


def list_mods(conn: sqlite3.Connection, *, only_mirrored: bool = False) -> list[dict]:
    """模组列表。附上每个模组「选了几个、下了几个」。"""
    sql = "SELECT * FROM mirror_mods"
    if only_mirrored:
        sql += " WHERE mirrored = 1"
    sql += " ORDER BY downloads DESC"

    counts: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT mod_id,"
        " SUM(CASE WHEN selected = 1 THEN 1 ELSE 0 END) AS selected,"
        " SUM(CASE WHEN local_name != '' THEN 1 ELSE 0 END) AS local,"
        " COALESCE(SUM(CASE WHEN local_name != '' THEN local_size ELSE 0 END), 0) AS bytes"
        " FROM mirror_files GROUP BY mod_id"
    ).fetchall():
        counts[row["mod_id"]] = {
            "selected": int(row["selected"] or 0),
            "local": int(row["local"] or 0),
            "bytes": int(row["bytes"] or 0),
        }

    items = []
    for row in conn.execute(sql).fetchall():
        item = dict(row)
        item["loaders"] = as_list(item["loaders"])
        item["game_versions"] = as_list(item["game_versions"])
        item["categories"] = as_list(item["categories"])
        item["mirror_loaders"] = as_list(item["mirror_loaders"])
        item["mirror_versions"] = as_list(item["mirror_versions"])
        item["mirrored"] = bool(item["mirrored"])
        item.update(counts.get(item["id"], {"selected": 0, "local": 0, "bytes": 0}))
        items.append(item)
    return items


def mod_detail(conn: sqlite3.Connection, mod_id: str) -> dict | None:
    """模组详情 + 按「加载器 × 游戏版本」整理过的版本清单。"""
    row = conn.execute("SELECT * FROM mirror_mods WHERE id = ?", (mod_id,)).fetchone()
    if row is None:
        return None

    mod = dict(row)
    mod["loaders"] = as_list(mod["loaders"])
    mod["game_versions"] = as_list(mod["game_versions"])
    mod["categories"] = as_list(mod["categories"])
    mod["mirror_loaders"] = as_list(mod["mirror_loaders"])
    mod["mirror_versions"] = as_list(mod["mirror_versions"])
    mod["mirrored"] = bool(mod["mirrored"])

    versions = []
    for item in conn.execute(
        "SELECT * FROM mirror_files WHERE mod_id = ? ORDER BY published_at DESC", (mod_id,)
    ).fetchall():
        entry = dict(item)
        entry["game_versions"] = as_list(entry["game_versions"])
        entry["loaders"] = as_list(entry["loaders"])
        entry["selected"] = bool(entry["selected"])
        entry["mirrored"] = bool(entry["local_name"])
        entry["file_size_text"] = human_size(entry["file_size"])
        versions.append(entry)

    mod["versions"] = versions
    return mod


def public_mod(conn: sqlite3.Connection, mod_id: str) -> dict | None:
    """给普通用户看的详情。

    ！只暴露已经下到本地的文件。没镜像的文件给一个 Modrinth 外链即可 ——
      把 download_url 直接吐给前端等于让本站变成一个「帮你绕过网络限制」的跳板，
      而且那个链接随时会 404。
    """
    detail = mod_detail(conn, mod_id)
    if detail is None:
        return None

    limits = []
    for version in detail["versions"]:
        limits.append({
            "id": version["id"],
            "version_number": version["version_number"],
            "version_type": version["version_type"],
            "game_versions": version["game_versions"],
            "loaders": version["loaders"],
            "file_name": version["file_name"],
            "file_size": version["file_size"],
            "file_size_text": version["file_size_text"],
            "published_at": version["published_at"],
            "available": version["mirrored"],
        })

    detail["versions"] = limits
    detail["local_count"] = sum(1 for v in limits if v["available"])
    return detail


def human_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
