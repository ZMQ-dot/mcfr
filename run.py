"""一键启动脚本。

用法：
    python run.py                         # 启动服务（启动时自动升级数据库）
    python run.py --port 9000             # 换端口

数据与升级相关（换机器、重装、出问题时用）：
    python run.py --check                 # 看当前数据库版本 / 用户数 / 备份列表
    python run.py --upgrade               # 只跑一次升级（会自动先备份）
    python run.py --backup                # 现在手工备份一份
    python run.py --list-backups          # 列出所有备份
    python run.py --import-db 路径.db     # 把一个数据库文件导入进来（会先备份当前数据）

想彻底重来（清空所有账号）时，停掉服务、删掉 data/mcfr.db 再启动即可。
备份文件都在 data/backups/ 下，直接复制走就能换机器。

！v0.21 起不再有邮件配置，也不需要任何邮箱、授权码或第三方服务。
  注册与登录卡的是本站自己画的图片验证码（backend/captcha.py），
  所以「收不到验证码」这类问题从此不存在了。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Windows 控制台默认不是 UTF-8，这里统一输出编码，避免中文日志报错
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # 老版本或重定向场景下忽略
    pass


def run_backup() -> int:
    """手工备份一份。"""
    from backend import upgrade

    try:
        item = upgrade.create_backup("cli")
    except upgrade.UpgradeError as exc:
        print(f"[mcfr] 备份失败：{exc}")
        return 1

    print(f"[mcfr] 已备份 -> {item.path}")
    print(f"[mcfr] 大小 {upgrade.human_size(item.size)}，数据库版本 v{item.version}")
    removed = upgrade.prune_backups(upgrade.KEEP_BACKUPS, protect=item.name)
    if removed:
        print(f"[mcfr] 已清理 {len(removed)} 份最旧的备份")
    return 0


def run_list_backups() -> int:
    """列出所有备份。"""
    from backend import upgrade

    items = upgrade.list_backups()
    if not items:
        print("[mcfr] 还没有任何备份。跑一次服务，或者用 --backup 建一份。")
        return 0

    print(f"[mcfr] 共 {len(items)} 份备份（目录：{upgrade.BACKUP_DIR}）")
    for item in items:
        print(f"  {item.name}   {upgrade.human_size(item.size):>9}   数据库 v{item.version}")
    print("[mcfr] 直接把这些 .db 文件复制走就能换机器；用 --import-db 放回来。")
    return 0


def run_upgrade_only() -> int:
    """只执行升级，不启动服务。"""
    from backend import upgrade

    try:
        report = upgrade.run_upgrade(trigger="cli", force=True)
    except upgrade.UpgradeError as exc:
        print(f"[mcfr] {exc}")
        return 1

    if report["status"] == "installed":
        print(f"[mcfr] 全新数据库已建好（v{report['to_version']}）")
    elif report["status"] == "up-to-date":
        print(f"[mcfr] 数据库已是最新（v{report['to_version']}，{report['users']} 个用户），不用升级")
    else:
        print(
            f"[mcfr] 升级完成：v{report['from_version']} -> v{report['to_version']}，"
            f"用户 {report['users']} 个，数据未丢失"
        )
        if report.get("backup"):
            print(f"[mcfr] 升级前的快照：{report['backup']}")
    return 0


def run_import(path: str) -> int:
    """把一个数据库文件导入进来（整体替换当前数据）。"""
    from backend import upgrade

    source = Path(path).expanduser()
    if not source.is_file():
        print(f"[mcfr] 找不到文件：{source}")
        return 1

    try:
        report = upgrade.restore_from_file(source, label="cli-import")
    except upgrade.UpgradeError as exc:
        print(f"[mcfr] 导入失败：{exc}")
        return 1

    print(f"[mcfr] 导入成功：{report['users']} 个用户已就位")
    print(f"[mcfr] 来源数据库版本 v{report['imported_version']}，已自动升到 v{upgrade.SCHEMA_VERSION}")
    print(f"[mcfr] 导入前的数据存为：{report['safety_backup']}")
    return 0


def run_check() -> int:
    """打印当前数据库状况，不改任何东西。"""
    from backend import upgrade

    status = upgrade.system_status()
    health = status["health"]
    work = status["work"]

    print(f"[mcfr] 程序版本：v{status['app_version']}")
    print(f"[mcfr] 数据库版本：v{status['schema_version']}（目标 v{status['target_schema_version']}）")
    print(f"[mcfr] 数据库位置：{status['db']['path']}")
    print(f"[mcfr] 数据库大小：{status['db']['size_text']}")
    print(f"[mcfr] 用户账号数：{status['db']['users']}")
    print(f"[mcfr] 完整性自检：{health.get('integrity') or '未检查'}")

    problems = list(health.get("problems") or [])
    if work["missing_tables"]:
        problems.append("缺少数据表：" + "、".join(work["missing_tables"]))
    if work["missing_columns"]:
        problems.append("users 表缺字段：" + "、".join(work["missing_columns"]))
    if work["legacy_tables"]:
        problems.append("还留着废弃的表：" + "、".join(work["legacy_tables"]))
    if work["migrations"]:
        problems.append(f"{len(work['migrations'])} 条数据订正未执行")

    if problems:
        print("[mcfr] 需要处理：")
        for item in problems:
            print(f"         · {item}")
        print("[mcfr] 跑一次 python run.py --upgrade 即可自动处理（会先备份）")
    else:
        print("[mcfr] 状态正常，不需要升级")

    backups = status["backups"]
    print(f"[mcfr] 现有备份：{len(backups)} 份")
    for item in backups[:5]:
        print(f"         {item['name']}（{item['size_text']}）")
    return 0


def run_reset_admin_key() -> int:
    """应急：清掉所有后台登录密钥，并把密码登录重新打开。

    用在「密钥文件和 U 盘备份一起丢了」的时候。这是唯一一个不需要先进后台
    就能执行的恢复手段，代价是必须能登上服务器。

    ！密码登录必须一起打开。只删密钥不管它的话，万一密码入口本来就是关着的，
      删完就真的谁也别想进去了 —— 那就不是恢复，是把锁死得更结实。
    """
    import secrets

    from backend import admins, database

    conn = database.connect()
    try:
        row = conn.execute(
            "SELECT id, username FROM admins WHERE is_super = 1"
        ).fetchone()
        if row is None:
            print("[mcfr] 库里没有超级管理员账号，先跑一次 python run.py --upgrade")
            return 1

        removed = conn.execute(
            "DELETE FROM admin_keys WHERE admin_id = ?", (row["id"],)
        ).rowcount
        # 顺手把没用的挑战也清了，免得留下几张还能被签名的门票
        conn.execute("DELETE FROM admin_challenges")

        # 口令优先沿用 .env 里配的那个；没配就新生成一个并写回去，
        # 否则用户手里既没有密钥也没有口令，还是进不去。
        password = admins.super_password()
        generated = not password
        if generated:
            password = secrets.token_urlsafe(12)
        conn.execute(
            "UPDATE admins SET password_hash = ?, status = 'active' WHERE id = ?",
            (admins.hash_password(password), row["id"]),
        )
        admins.set_password_login(conn, True)
        conn.commit()
        username = row["username"]
    finally:
        conn.close()

    if generated:
        # 复用后台那套打印 + 写 .env，免得用户在滚动的日志里错过口令
        admins._show_generated_password(password)

    bar = "=" * 64
    print(f"\n[ mcfr ] {bar}")
    print("[ mcfr ] 已重置后台登录方式：")
    print(f"[ mcfr ]     删掉的密钥：{removed} 把")
    print("[ mcfr ]     密码登录：已重新打开")
    print("[ mcfr ]")
    if generated:
        print("[ mcfr ] 新口令就是上面那串。")
    print(f"[ mcfr ] 用 .env 里的口令登录（用户名：{username}），")
    print("[ mcfr ] 进去后到「登录密钥」再生成一把，然后重新关掉密码登录。")
    print(f"[ mcfr ] {bar}\n")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="mcfr · Minecraft 同好匹配")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（对外服务用 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口")
    parser.add_argument("--reload", action="store_true", help="开发模式：代码改动自动重启")
    parser.add_argument("--check", action="store_true", help="检查数据库版本与健康状况后退出")
    parser.add_argument("--upgrade", action="store_true", help="只执行数据库升级后退出（会自动先备份）")
    parser.add_argument("--backup", action="store_true", help="手工备份一份数据库后退出")
    parser.add_argument("--list-backups", action="store_true", help="列出所有备份后退出")
    parser.add_argument("--import-db", metavar="文件", help="导入一个数据库文件后退出（会先备份当前数据）")
    parser.add_argument("--reset-admin-key", action="store_true",
                        help="应急：清掉后台登录密钥并重新打开密码登录后退出")
    args = parser.parse_args()

    # 这几个开关都是「做完就退出」，与服务启动互斥
    if args.check:
        raise SystemExit(run_check())
    if args.upgrade:
        raise SystemExit(run_upgrade_only())
    if args.backup:
        raise SystemExit(run_backup())
    if args.list_backups:
        raise SystemExit(run_list_backups())
    if args.import_db:
        raise SystemExit(run_import(args.import_db))
    if args.reset_admin_key:
        raise SystemExit(run_reset_admin_key())

    import uvicorn

    print(f"[mcfr] 服务启动中 -> http://{args.host}:{args.port}")
    uvicorn.run(
        "backend.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        # ！这一行必须留着，别删。
        #
        #   uvicorn 默认开着 proxy_headers，只要连接来源在它自己的
        #   --forwarded-allow-ips 里（默认值就是 127.0.0.1），它就会拿
        #   X-Forwarded-For 的值去**替换** request.client.host。
        #
        #   而那个头是客户端可以随便伪造的。被它改写之后，应用层再怎么写
        #   校验都没用 —— 所有按 IP 做的限流会同时失效：
        #     管理员登录失败限制（→ 可以无限暴破后台口令）
        #     验证码领取限制（→ 可以无限取图去离线破解）
        #     意见反馈频率限制
        #   而且失败记录里存下来的 IP 全是假的，出事后查不到人。
        #
        #   来源 IP 统一由 backend/client_ip.py 判断：那里默认谁都不信，
        #   要信任反向代理必须显式配 MCFR_TRUSTED_PROXIES。
        #   判断只留一处，才不会有「两层互相打架」这种事。
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
