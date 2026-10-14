"""版本升级与数据保全：备份、迁移、导出、导入（还原）。

为什么单独一个模块
------------------
`database.py` 关心的是「表结构长什么样」，这个模块关心的是
「换一个版本之后，用户原来那些数据还在不在」。

三条硬性承诺
------------
1. **动结构之前一定先备份**。只要检测到有待执行的迁移（补列 / 建表 / 数据订正），
   就先把当前数据库整份快照到 `data/backups/`，再开始改。
2. **出任何错都自动回滚**。升级过程抛异常、或者升级后自检不过，
   就用刚才那份快照把数据库整体盖回去，宁可停在旧版本，也不能留下半升级状态。
3. **绝不删用户数据**。迁移只做「加表 / 加列 / 建索引 / 清理早已废弃的空列」，
   自检会把升级前后的用户数对一遍，变少了就当成失败处理。

备份为什么不用 `shutil.copy`
---------------------------
1. 数据库开着 WAL，最新的写入还在 `-wal` 文件里，
   直接拷 `.db` 拿到的是**缺了最近若干次写入**的旧快照；
2. Windows 上正在被连接打开的文件不允许改名 / 删除。
本模块统一走 SQLite 自带的在线备份 API（`Connection.backup`）——
它走正常的读写通道，Wal 里的内容会一并并进去，得到的是一份自洽的快照。
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .database import (
    DATA_DIR,
    DB_PATH,
    LEGACY_DEMO_USERNAMES,
    LEGACY_TABLES,
    SCHEMA_VERSION,
    USER_COLUMN_MIGRATIONS,
    adopt_legacy_database,
    apply_schema,
    connect,
    init_db,
    missing_indexes,
    missing_tables,
    now_ts,
    pending_migrations,
    purge_legacy_data,
    schema_version,
)

BACKUP_DIR = DATA_DIR / "backups"

# 最多保留多少份备份（不区分是自动还是手动），超出就删最旧的
KEEP_BACKUPS = 12

# 导入的数据库文件上限。本站的数据量很小，64MB 已经非常宽松
MAX_IMPORT_BYTES = 64 * 1024 * 1024

SQLITE_MAGIC = b"SQLite format 3\x00"

# 备份文件名由我们自己生成，导入 / 下载 / 删除时都用这个正则卡一遍，
# 挡掉 `..` 之类的路径穿越（和 media.py 里保护上传文件的做法一致）。
#
# ！坑：解析时不能用 name.rsplit("-v", 1) 去切 ——
#   标签本身就可能带 "-v"（例如 pre-upgrade-v0），切出来会变成 "0.db" 而报错。
#   统一用下面这个带捕获组的正则，版本号与标签一次拿全。
#   group(1) 日期 / group(2) 时间 / group(3) 版本 / group(4) 标签
BACKUP_NAME_RE = re.compile(
    r"^mcfr-(\d{8})-(\d{6})-v(\d+)-([a-z0-9-]{1,32})\.db$"
)

# 导入 mcfr 数据库时，这个库里必须有的表
REQUIRED_TABLES = frozenset({"users"})


class UpgradeError(RuntimeError):
    """升级 / 备份 / 导入失败。message 可以直接展示给用户。"""


# ---------------------------------------------------------------- 基础工具

def _open_raw(path: Path) -> sqlite3.Connection:
    """开一个不受 FastAPI 依赖管理的裸连接。"""
    conn = sqlite3.connect(str(path), timeout=20, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 8000")
    return conn


def copy_database(src_path: Path, dst_path: Path) -> None:
    """把 src_path 的内容整体复制进 dst_path（用 SQLite 在线备份 API）。

    dst 不需要预先存在；已经存在时它的内容会被完整替换。
    """
    src = _open_raw(src_path)
    dst = _open_raw(dst_path)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()


def count_users(conn: sqlite3.Connection) -> int:
    """用户数。表还不存在时返回 0，不要在这里抛异常。"""
    try:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
    except sqlite3.Error:
        return 0


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {str(row[0]) for row in rows}


def _user_columns(conn: sqlite3.Connection) -> set[str]:
    try:
        return {str(row["name"]) for row in conn.execute("PRAGMA table_info(users)")}
    except sqlite3.Error:
        return set()


def missing_user_columns(conn: sqlite3.Connection) -> list[str]:
    """老库缺哪些新列（补列前 self 检查用）。"""
    existing = _user_columns(conn)
    return [column for column, _ in USER_COLUMN_MIGRATIONS if column not in existing]


def pending_work(conn: sqlite3.Connection) -> dict[str, Any]:
    """这个库离当前版本还差些什么。全部为空就说明不用升级。"""
    tables = _table_names(conn)
    return {
        # ！必须从 SCHEMA 推导（database.missing_tables），不能写死几个表名。
        #   写死过：v0.13 新增三张表后老库检测不到差异，
        #   apply_schema() 不被调用，表就永远建不出来。
        "missing_tables": missing_tables(conn),
        # ！同上一模一样的道理，只是对象换成了索引（v0.31 补）：
        #   往 SCHEMA 里加一条 CREATE INDEX，没有这一项就不会被感知到，
        #   于是 apply_schema() 不跑 → 线上永远没建这条索引。
        "missing_indexes": missing_indexes(conn),
        "missing_columns": missing_user_columns(conn),
        "legacy_tables": sorted(table for table in LEGACY_TABLES if table in tables),
        "migrations": [step.version for step in pending_migrations(conn)],
    }


def needs_upgrade(conn: sqlite3.Connection) -> bool:
    return any(pending_work(conn).values())


# ---------------------------------------------------------------- 自检

def health_check(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """数据库健康自检：完整性 + 外键 + 关键表是否读得出来。"""
    own = conn is None
    conn = conn or connect()
    try:
        result: dict[str, Any] = {"ok": True, "problems": []}

        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
            verdicts = [str(row[0]) for row in rows]
            result["integrity"] = "ok" if verdicts == ["ok"] else "; ".join(verdicts[:3])
        except sqlite3.Error as exc:
            result["integrity"] = f"无法检查（{exc}）"
            verdicts = [str(exc)]

        if verdicts != ["ok"]:
            result["ok"] = False
            result["problems"].append("数据库文件完整性校验未通过")

        try:
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
            result["foreign_key_issues"] = len(broken)
            if broken:
                result["ok"] = False
                result["problems"].append(f"有 {len(broken)} 条记录的外键指向了不存在的行")
        except sqlite3.Error:
            result["foreign_key_issues"] = -1

        try:
            result["users"] = count_users(conn)
        except sqlite3.Error as exc:
            result["ok"] = False
            result["problems"].append(f"读取 users 表失败：{exc}")

        result["schema_version"] = schema_version(conn)
        return result
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------- 备份

@dataclass(frozen=True)
class Backup:
    name: str
    label: str
    version: int
    size: int
    created_at: int
    path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "version": self.version,
            "size": self.size,
            "created_at": self.created_at,
            "size_text": human_size(self.size),
        }


def human_size(size: int) -> str:
    value = float(max(size, 0))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _safe_label(label: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", (label or "").strip().lower()).strip("-")
    return (cleaned or "manual")[:32]


def _parse_backup(path: Path) -> Backup | None:
    """把磁盘上的文件名还原成结构化信息。名字不合规就当作不是备份文件。"""
    matched = BACKUP_NAME_RE.match(path.name)
    if matched is None:
        return None
    stat = path.stat()
    return Backup(
        name=path.name,
        label=matched.group(4),
        version=int(matched.group(3)),
        size=stat.st_size,
        created_at=int(stat.st_mtime),
        path=path,
    )


def list_backups() -> list[Backup]:
    """所有备份，最新的在前。"""
    if not BACKUP_DIR.is_dir():
        return []
    items = [parsed for parsed in (_parse_backup(p) for p in BACKUP_DIR.glob("*.db")) if parsed]
    return sorted(items, key=lambda item: item.created_at, reverse=True)


def get_backup(name: str) -> Backup | None:
    """按文件名取一份备份；名字不合规或文件不在都返回 None。"""
    safe = (name or "").strip()
    if not BACKUP_NAME_RE.match(safe):
        return None
    path = BACKUP_DIR / safe
    return _parse_backup(path) if path.is_file() else None


def create_backup(label: str = "manual", *, source: Path | None = None) -> Backup:
    """给数据库拍一份快照。

    source 默认为线上数据库；异步写盘（先写 .part 再改名），
    这样即使中途断电，也不会在备份目录里留下一个半截文件冒充完整备份。
    """
    source = source or DB_PATH
    if not source.is_file():
        raise UpgradeError("数据库文件还不存在，没有东西可以备份")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = f"mcfr-{stamp}-v{SCHEMA_VERSION}-{_safe_label(label)}"

    target = BACKUP_DIR / f"{base}.db"
    for index in range(2, 100):
        if not target.exists():
            break
        target = BACKUP_DIR / f"{base}-{index}.db"
    else:  # pragma: no cover - 一分钟内建 99 份备份基本不可能
        raise UpgradeError("备份文件过多，请先清理 data/backups 目录")

    temp = target.with_name(target.name + ".part")
    try:
        copy_database(source, temp)
        temp.replace(target)
    except Exception:
        temp.unlink(missing_ok=True)
        raise

    parsed = _parse_backup(target)
    if parsed is None:  # pragma: no cover - 文件名是自己拼的
        raise UpgradeError("备份文件名异常")
    return parsed


def prune_backups(keep: int = KEEP_BACKUPS, protect: str = "") -> list[str]:
    """只保留最近 keep 份备份，返回被删掉的文件名。

    protect 指定的那一份永远不删 —— 升级前的快照是最重要的那一份，
    不能因为它是「第 13 份」就被清理掉。
    """
    removed: list[str] = []
    for item in list_backups()[keep:]:
        if item.name == protect:
            continue
        try:
            item.path.unlink()
            removed.append(item.name)
        except OSError:
            pass
    return removed


def delete_backup(name: str) -> bool:
    item = get_backup(name)
    if item is None:
        return False
    try:
        item.path.unlink()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- 导入校验

def validate_database_file(path: Path) -> dict[str, Any]:
    """确认这个文件确实是一个能用的 mcfr 数据库。"""
    if not path.is_file():
        raise UpgradeError("文件不存在")
    if path.stat().st_size < 512:
        raise UpgradeError("文件太小了，不像是一个完整的数据库")

    with path.open("rb") as handle:
        head = handle.read(16)
    if head != SQLITE_MAGIC:
        raise UpgradeError(
            "这不是 SQLite 数据库文件。请选择从本后台「下载当前数据库」得到的那种 .db 文件。"
        )

    conn = _open_raw(path)
    try:
        verdicts = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
        if verdicts != ["ok"]:
            raise UpgradeError("这个数据库文件已损坏，无法导入：" + "; ".join(verdicts[:2]))

        tables = _table_names(conn)
        missing = REQUIRED_TABLES - tables
        if missing:
            raise UpgradeError(
                "这个文件里没有 users 表，看起来不是 mcfr 的数据库。"
                "（如果你导入的是别的程序的库，请先自行导出成 mcfr 支持的格式）"
            )

        return {
            "users": count_users(conn),
            "schema_version": schema_version(conn),
            "tables": len(tables),
            "size": path.stat().st_size,
        }
    except sqlite3.DatabaseError as exc:
        raise UpgradeError(f"读取数据库失败：{exc}") from exc
    finally:
        conn.close()


# ---------------------------------------------------------------- 升级历史

def record_history(
    conn: sqlite3.Connection,
    *,
    from_version: int,
    to_version: int,
    kind: str,
    status: str,
    detail: str = "",
    backup_name: str = "",
) -> None:
    """往 upgrade_history 写一条。表不存在（极早期的库）时静默跳过。"""
    try:
        conn.execute(
            """
            INSERT INTO upgrade_history
                (from_version, to_version, kind, backup_name, status, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (from_version, to_version, kind, backup_name, status, detail, now_ts()),
        )
    except sqlite3.Error:
        pass


def history(limit: int = 20) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM upgrade_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def last_upgrade() -> dict[str, Any] | None:
    """最近一次成功升级的记录。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM upgrade_history WHERE status = 'ok' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row is not None else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


# ---------------------------------------------------------------- 主流程

def apply_pending(conn: sqlite3.Connection) -> dict[str, Any]:
    """在给定连接上把结构与数据都追平（不做备份、不管回滚）。"""
    before = schema_version(conn)
    tables_before = _table_names(conn)
    users_before = count_users(conn)

    apply_schema(conn)
    applied = purge_legacy_data(conn)
    conn.commit()

    return {
        "from_version": before,
        "to_version": schema_version(conn),
        "users_before": users_before,
        "users_after": count_users(conn),
        "migrations": [step.name for step in applied],
        "new_tables": sorted(_table_names(conn) - tables_before),
    }


def run_upgrade(*, trigger: str = "startup", force: bool = False) -> dict[str, Any]:
    """把线上数据库升到当前版本。返回一份可以直接打印 / 返回给前端的报告。

    这是启动流程唯一应该调用的入口：
        先看有没有活要干 → 备份 → 迁移 → 自检 → 记录；
        任何一步失败都用备份把数据库整体盖回去，然后抛出异常。
    """
    adopt_legacy_database()

    if not DB_PATH.is_file():
        # 全新安装：没有任何用户数据要保护，直接建库
        init_db()
        conn = connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            record_history(
                conn, from_version=0, to_version=SCHEMA_VERSION,
                kind="install", status="ok", detail="全新安装，已建库",
            )
            users = count_users(conn)
            conn.commit()
        finally:
            conn.close()
        print(f"[mcfr] 全新数据库已建好：{DB_PATH}")
        return {
            "status": "installed",
            "fresh": True,
            "from_version": 0,
            "to_version": SCHEMA_VERSION,
            "users": users,
            "backup": None,
            "trigger": trigger,
        }

    conn = connect()
    try:
        work = pending_work(conn)
        before_version = schema_version(conn)
        before_users = count_users(conn)
        before_health = health_check(conn)
    finally:
        conn.close()

    todo = any(work.values())

    if not todo and not force:
        return {
            "status": "up-to-date",
            "fresh": False,
            "from_version": before_version,
            "to_version": before_version,
            "users": before_users,
            "backup": None,
            "work": work,
            "health": before_health,
            "trigger": trigger,
        }

    backup = create_backup(f"pre-upgrade-v{before_version}")
    print(f"[mcfr] 升级前已备份：{backup.name}（{human_size(backup.size)}）")

    try:
        conn = connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            report = apply_pending(conn)
        finally:
            conn.close()

        after_health = health_check()

        if not after_health["ok"]:
            raise UpgradeError("升级后自检未通过：" + "；".join(after_health["problems"]))

        # 用户数只会因为「清理内置演示账号」这一步而减少，
        # 所以这里把预期减少量算出来对比，而不是简单地要求「不能变少」。
        expected_loss = 0
        if 1 in work["migrations"]:
            conn = connect()
            try:
                marks = ", ".join("?" for _ in LEGACY_DEMO_USERNAMES)
                expected_loss = int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM users WHERE username IN ({marks})",
                        LEGACY_DEMO_USERNAMES,
                    ).fetchone()[0]
                )
            finally:
                conn.close()

        if report["users_after"] < report["users_before"] - expected_loss:
            raise UpgradeError(
                f"用户数异常减少（{report['users_before']} -> {report['users_after']}），已回滚"
            )

        removed = prune_backups(KEEP_BACKUPS, protect=backup.name)
        if removed:
            print(f"[mcfr] 已清理 {len(removed)} 份过旧的备份")

        conn = connect()
        try:
            detail = "、".join(report["migrations"]) or "无数据订正，仅同步表结构"
            record_history(
                conn,
                from_version=report["from_version"],
                to_version=report["to_version"],
                kind="upgrade",
                status="ok",
                detail=(
                    f"{detail}；用户数 {report['users_before']} -> {report['users_after']}"
                ),
                backup_name=backup.name,
            )
            conn.commit()
        finally:
            conn.close()

        print(
            f"[mcfr] 升级完成：数据库 v{report['from_version']} -> v{report['to_version']}"
            f"，用户 {report['users_after']} 个，数据未丢失"
        )

        return {
            "status": "upgraded",
            "fresh": False,
            "from_version": report["from_version"],
            "to_version": report["to_version"],
            "users": report["users_after"],
            "users_before": report["users_before"],
            "applied": report["migrations"],
            "backup": backup.name,
            "health": after_health,
            "trigger": trigger,
        }

    except Exception as exc:
        # 不管是「事务里出错」还是「提交后自检不过」，统一用备份整体盖回去。
        # 宁可停在旧版本继续跑，也不能留下一个半新半旧的库。
        restore_report: dict[str, Any] = {"restored": False, "detail": str(exc)}
        try:
            copy_database(backup.path, DB_PATH)
            conn = connect()
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                restore_report["restored"] = True
                restore_report["users"] = count_users(conn)
                record_history(
                    conn, from_version=SCHEMA_VERSION, to_version=before_version,
                    kind="upgrade", status="failed",
                    detail=str(exc)[:300], backup_name=backup.name,
                )
                conn.commit()
            finally:
                conn.close()
            print(f"[mcfr] 升级失败，已从备份 {backup.name} 回滚：{exc}")
        except Exception as rollback_exc:  # pragma: no cover - 备份文件都读不了才会到这里
            restore_report["detail"] = f"{exc}（回滚也失败了：{rollback_exc}）"
            print(f"[mcfr] !! 升级失败且回滚失败：{rollback_exc}")
            print(f"[mcfr] !! 请手工把 {backup.path} 复制成 {DB_PATH}")

        if isinstance(exc, UpgradeError):
            error = UpgradeError(f"升级失败：{exc}（数据已回滚到升级前）")
        else:
            error = UpgradeError(f"升级失败：{exc}（数据已回滚到升级前）")
        error.backup = backup.name  # type: ignore[attr-defined]
        error.restore = restore_report
        raise error from exc


def _demo_usernames() -> tuple[str, ...]:
    """保留给旧调用方的兼容函数。"""
    return LEGACY_DEMO_USERNAMES


# ---------------------------------------------------------------- 导出 / 还原 / 导入

def export_snapshot(label: str = "export") -> Backup:
    """把当前线上数据库导出成一份可下载的备份。"""
    return create_backup(label)


def restore_from_file(
    source: Path, *, keep_admin_token: str = "", label: str = "restore"
) -> dict[str, Any]:
    """用一个数据库文件整体覆盖线上数据库。

    覆盖前会先把当前的库存成 `pre-restore` 备份；覆盖之后立刻跑一次升级，
    所以哪怕导入的是一年前的旧库（缺列、少表），它也会被自动追平到当前版本。
    """
    info = validate_database_file(source)

    safety = create_backup(f"pre-{label}")
    print(f"[mcfr] 覆盖前已备份当前数据库：{safety.name}")

    copy_database(source, DB_PATH)

    conn = connect()
    try:
        # 备份里的 journal_mode 会跟着一起被复制过来，这里补一次
        conn.execute("PRAGMA journal_mode = WAL")
        conn.commit()
    finally:
        conn.close()

    # 覆盖过来的库是它自己那份 admin_sessions，里面没有当前管理员令牌，
    # 不补回去的话，管理员做完这次还原就会立刻被自己的后台踢出去。
    if keep_admin_token:
        conn = connect()
        try:
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO admin_sessions(token, created_at, expires_at, ip) "
                    "VALUES (?, ?, ?, '')",
                    (keep_admin_token, now_ts(), now_ts() + 8 * 3600),
                )
                conn.commit()
            except sqlite3.Error:
                pass  # 极早期的库没有这张表，交给下面的升级去补
        finally:
            conn.close()

    report = run_upgrade(trigger=label)

    return {
        "imported_users": info["users"],
        "imported_version": info["schema_version"],
        "safety_backup": safety.name,
        "upgrade": report,
        "users": report.get("users", info["users"]),
    }


def import_bytes(
    data: bytes, *, filename: str = "upload.db", keep_admin_token: str = ""
) -> dict[str, Any]:
    """从上传的字节流导入数据库。"""
    if not data:
        raise UpgradeError("没有收到文件内容，请重新选择要导入的数据库文件")
    if len(data) > MAX_IMPORT_BYTES:
        raise UpgradeError(f"文件太大了（上限 {MAX_IMPORT_BYTES // 1024 // 1024}MB）")
    if not data.startswith(SQLITE_MAGIC):
        raise UpgradeError(
            "这不是 SQLite 数据库文件。请选择从本后台「下载当前数据库」得到的 .db 文件。"
        )

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    temp = BACKUP_DIR / f"import-{int(time.time())}-{_safe_label(filename)[:16]}.db"
    temp.write_bytes(data)
    try:
        return restore_from_file(temp, keep_admin_token=keep_admin_token, label="import")
    finally:
        temp.unlink(missing_ok=True)


# ---------------------------------------------------------------- 状态总览

def system_status() -> dict[str, Any]:
    """管理后台「系统升级」页要的全部信息。"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    conn = connect()
    try:
        work = pending_work(conn)
        users = count_users(conn)
        version = schema_version(conn)
        health = health_check(conn)
    finally:
        conn.close()

    size = DB_PATH.stat().st_size if DB_PATH.is_file() else 0
    return {
        "app_version": __version__,
        "schema_version": version,
        "target_schema_version": SCHEMA_VERSION,
        "need_upgrade": any(work.values()),
        "work": work,
        "db": {
            "path": str(DB_PATH),
            "size": size,
            "size_text": human_size(size),
            "users": users,
        },
        "health": health,
        "backups": [item.to_dict() for item in list_backups()],
        "keep_backups": KEEP_BACKUPS,
        "history": history(10),
        "last_upgrade": last_upgrade(),
        "data_dir": str(DATA_DIR),
    }


def free_space_hint() -> str:
    """磁盘剩余空间，出错就返回空串（纯提示用，不该影响功能）。"""
    try:
        usage = shutil.disk_usage(DATA_DIR if DATA_DIR.exists() else Path.cwd())
        return human_size(usage.free)
    except OSError:
        return ""
