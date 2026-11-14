#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给站点灌一批「看起来像真的」的演示玩家。

    python seed_demo.py                 # 只打印计划，不写库（默认）
    python seed_demo.py --yes           # 真的写进去
    python seed_demo.py --purge --yes   # 把演示账号删干净
    python seed_demo.py --touch --yes   # 让它们「重新上线」（见下）

## 为什么要这个脚本

刚装好的站点，「发现同好」是一张空网格 —— 而它恰恰是访客登录后第一眼看到
的页面。新手对着 0 张卡片，没法判断这个站到底有没有人用。

## ⚠️ 这些是演示账号，不是真人

它们的存在只为一件事：让页面在演示、录屏、答辩时不像刚装好的空壳。
**真的上线之前请务必 `--purge`** —— 一个真的在运营的站不该有 12 个假玩家，
而且它们占着「推荐」的名额，会把真人挤下去。

三条设计上的讲究：

1. **口令是随机的、不打印。** 即使用户名泄露，也没有人能登录成这些账号。
   （演示数据只负责「看起来有人」，不负责「能进去」——能进去才是风险。）
2. **`last_seen` 是错开的。** 只有几位是「刚刚在线」，其余是几小时到几天前。
   全都在线反而不像真的：一个真站的在线人数从来不是 100%。
3. **`created_at` 是分散的。** 卡片上不显示它，但成就和等级要看得到差异 ——
   全是 Lv.1 会像注册机批量生成的。
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import date

from backend import database, security

# ---------------------------------------------------------------- 演示玩家
#
# 名字、地区、玩法、作息都按真实玩家的说法写。
# ！简介一律用「第一人称的日常」，不要写成广告（「本服招收…」「欢迎加入…」）——
#   那种句子一眼是运营写的，反而假。真正像人的句子都带一点自嘲或者具体的小事。
#
# 这些画像和 pptwork/ 里做截图用的是同一批，所以演示时的截图和站点内容对得上。

USERS: list[dict] = [
    dict(username="block_xia", nickname="方块小夏", age=13, month=4, day=18,
         edition="both", region="江苏·南京",
         styles=["生存", "创造建筑", "红石"], times=["周末白天", "每天上线"],
         wants=["联机伙伴", "建筑搭子"],
         bio="喜欢在平原上盖小镇，最近在学红石自动农场。",
         qq="1350012345", note="晚上八点后都在"),
    dict(username="kuli_pa", nickname="苦力怕不苦", age=12, month=9, day=2,
         edition="java", region="浙江·杭州",
         styles=["生存", "小游戏", "跑酷"], times=["工作日课后", "周末晚上"],
         wants=["联机伙伴", "聊天交朋友"],
         bio="开服六年老萌新，最怕的怪是苦力怕。",
         qq="1270088990", note=""),
    dict(username="redstone_j", nickname="红石小匠", age=14, month=1, day=9,
         edition="java", region="广东·深圳",
         styles=["红石", "命令方块", "地图制作"], times=["周末晚上", "假期集中玩"],
         wants=["红石大佬", "一起做地图"],
         bio="只玩红石，能做出会走路的盔甲架。",
         qq="1892233445", note=""),
    dict(username="mod_hunter", nickname="模组猎人", age=15, month=7, day=21,
         edition="java", region="四川·成都",
         styles=["模组", "材质光影"], times=["每天都上线"],
         wants=["联机伙伴", "请教问题"],
         bio="三百个模组整合包受害者，光影党。",
         qq="", note=""),
    dict(username="villager_w", nickname="村民老王", age=11, month=3, day=15,
         edition="bedrock", region="山东·青岛",
         styles=["生存", "小游戏"], times=["周末白天"],
         wants=["生存队友", "聊天交朋友"],
         bio="周末开荒，主打一个陪伴，能语音最好。",
         qq="1551122334", note=""),
    dict(username="tnt_veteran", nickname="TNT老兵", age=16, month=11, day=30,
         edition="both", region="湖南·长沙",
         styles=["PVP", "小游戏", "速通"], times=["周末晚上"],
         wants=["联机伙伴"],
         bio="PVP 十年，打不过就跑。",
         qq="", note=""),
    dict(username="moon_light", nickname="月光先森", age=12, month=6, day=8,
         edition="java", region="北京",
         styles=["跑酷", "创造建筑"], times=["工作日课后"],
         wants=["建筑搭子", "聊天交朋友"],
         bio="喜欢在夜里盖玻璃温室。",
         qq="", note=""),
    dict(username="lightning_g", nickname="闪电咕咕", age=10, month=12, day=4,
         edition="bedrock", region="福建·厦门",
         styles=["小游戏", "生存"], times=["周末白天", "假期集中玩"],
         wants=["联机伙伴", "生存队友"],
         bio="刚学会打末影龙，找个大佬带带。",
         qq="", note=""),
    dict(username="build_cat", nickname="建筑喵", age=13, month=5, day=27,
         edition="java", region="陕西·西安",
         styles=["创造建筑", "材质光影"], times=["周末白天", "周末晚上"],
         wants=["建筑搭子"],
         bio="只盖中世纪城堡，最近在研究圆墙怎么不塌。",
         qq="1763322110", note=""),
    dict(username="map_maker", nickname="地图工坊", age=14, month=8, day=13,
         edition="both", region="江苏·无锡",
         styles=["地图制作", "命令方块", "小游戏"], times=["工作日课后", "周末晚上"],
         wants=["一起做地图"],
         bio="在做一张跑酷地图，第 7 关了，缺人测。",
         qq="1559988776", note=""),
    dict(username="star_deer", nickname="星野小鹿", age=11, month=2, day=19,
         edition="bedrock", region="云南·昆明",
         styles=["生存", "跑酷"], times=["周末白天"],
         wants=["聊天交朋友", "联机伙伴"],
         bio="只玩生存，喜欢在樱花林里迷路。",
         qq="", note=""),
    dict(username="snow_hunter", nickname="雪原猎人", age=15, month=10, day=6,
         edition="java", region="黑龙江·哈尔滨",
         styles=["模组", "生存", "PVP"], times=["每天都上线"],
         wants=["生存队友", "联机伙伴"],
         bio="冰原开局，靠钓鱼活下来的那种人。",
         qq="1934488002", note=""),
]

# 头像色相：取一组分得开的，免得 12 张脸全是一个颜色
HUES = (28, 96, 148, 188, 212, 248, 282, 318, 12, 64, 168, 336)

# 积分 → 等级（backend/achievements.py 的 LEVEL_THRESHOLDS）。
# 让大部分人停在 Lv.2–4，只有一两个到 Lv.5 —— 一个刚起步的站不该满地是老人。
POINTS = (18, 46, 96, 132, 74, 210, 58, 24, 88, 156, 36, 118)


def _birth(age: int, month: int, day: int, today: date) -> tuple[int, int, int]:
    """按「今天这个人正好是 age 岁」倒推出生年。

    ！不能直接 year - age：生日还没到的话，实际算出来会小一岁，
      卡片上的年龄就和自己写的那个人设对不上了。
    """
    year = today.year - age - (1 if (month, day) > (today.month, today.day) else 0)
    return year, month, day


def plan(conn) -> list[dict]:
    """算出每个人要写进去的值；已经在库里的跳过。"""
    today = date.today()
    stamp = database.now_ts()
    rows = conn.execute("SELECT username FROM users").fetchall()
    taken = {row["username"] for row in rows}

    out: list[dict] = []
    for index, spec in enumerate(USERS):
        if spec["username"] in taken:
            continue
        year, month, day = _birth(spec["age"], spec["month"], spec["day"], today)

        # 在线的错开：前 4 位「几分钟内」在线，后面的从几小时到几天前。
        # ！必须是**真**时间戳：matching 用 300 秒窗口判断在线，
        #   随意写个未来时间会让所有人都挂着「在线」，比空着更假。
        if index < 4:
            last_seen = stamp - random.randint(20, 280)
        else:
            last_seen = stamp - random.randint(3600, 86400 * 4)

        out.append(dict(
            spec=spec,
            index=index,
            birth=(year, month, day),
            created_at=stamp - random.randint(86400 * 3, 86400 * 130),
            last_seen=last_seen,
            hue=HUES[index % len(HUES)],
            points=POINTS[index % len(POINTS)],
            streak=random.randint(0, 5),
            login_days=random.randint(2, 38),
            seen=random.randint(0, 14),
            viewed=random.randint(1, 26),
            # 口令随手生成、不打印：演示账号不该是「能登进去的账号」
            password_hash=security.hash_password(security_token()),
        ))
    return out


def security_token() -> str:
    import secrets
    return secrets.token_urlsafe(24)


def apply(conn, items: list[dict]) -> None:
    for item in items:
        spec = item["spec"]
        year, month, day = item["birth"]
        conn.execute(
            """
            INSERT INTO users (
                username, nickname, password_hash, avatar_hue, bio, edition,
                birth_year, birth_month, birth_day, region,
                play_styles, online_time, looking_for,
                contact_qq, contact_note, points,
                login_streak, login_days, last_login_day,
                profile_seen_count, profile_view_count,
                created_at, last_seen
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                spec["username"], spec["nickname"], item["password_hash"], item["hue"],
                spec["bio"], spec["edition"], year, month, day, spec["region"],
                _json(spec["styles"]), _json(spec["times"]), _json(spec["wants"]),
                spec["qq"], spec["note"], item["points"],
                item["streak"], item["login_days"], date.today().isoformat(),
                item["seen"], item["viewed"],
                item["created_at"], item["last_seen"],
            ),
        )
    conn.commit()


def _json(values: list[str]) -> str:
    import json
    return json.dumps(values, ensure_ascii=False)


def purge(conn) -> int:
    """删掉这 12 个账号，以及它们留下的痕迹。"""
    names = [spec["username"] for spec in USERS]
    marks = ",".join("?" for _ in names)
    ids = [row["id"] for row in conn.execute(
        f"SELECT id FROM users WHERE username IN ({marks})", names
    ).fetchall()]
    if not ids:
        return 0
    id_marks = ",".join("?" for _ in ids)
    # 帖子 / 评论 / 点赞 / 会话都跟着走（外键是 ON DELETE CASCADE 的，
    # 但这里显式删一遍，免得以后有人把外键关掉）
    for table, column in (
        ("post_comments", "user_id"), ("post_likes", "user_id"),
        ("post_shares", "from_user"), ("post_shares", "to_user"),
        ("contact_requests", "from_user"), ("contact_requests", "to_user"),
        ("connections", "user_a"), ("connections", "user_b"),
        ("sessions", "user_id"), ("user_achievements", "user_id"),
        ("notifications", "user_id"),
    ):
        try:
            conn.execute(f"DELETE FROM {table} WHERE {column} IN ({id_marks})", ids)
        except Exception:  # noqa: BLE001  表名对不上就跳过，别为此中断
            pass
    conn.execute(f"DELETE FROM posts WHERE author_id IN ({id_marks})", ids)
    conn.execute(f"DELETE FROM users WHERE id IN ({id_marks})", ids)
    conn.commit()
    return len(ids)


def touch(conn) -> int:
    """让演示玩家重新「上线」一下。

    ！「在线」是算出来的，不是存出来的：matching 只看 last_seen 是不是在 300 秒
      以内。所以灌完数据过五分钟，那几位就又「离线」了 —— 这不是 bug，
      真的站也是这样。只是演示前想让人数好看，就需要重新拍一下时间戳。
    """
    stamp = database.now_ts()
    names = [spec["username"] for spec in USERS]
    marks = ",".join("?" for _ in names)
    changed = 0
    for index, name in enumerate(names):
        # 前 4 位「刚刚在线」，其余散在几小时到一天前 —— 和首次灌入时同样的错开
        seen = stamp - random.randint(20, 280) if index < 4 else stamp - random.randint(1200, 86400)
        cursor = conn.execute(
            "UPDATE users SET last_seen = ? WHERE username = ?", (seen, name)
        )
        changed += cursor.rowcount
    conn.commit()
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description="给站点灌一批演示玩家")
    parser.add_argument("--yes", action="store_true", help="真的写库（默认只是预览）")
    parser.add_argument("--purge", action="store_true", help="把这批演示账号删掉")
    parser.add_argument("--touch", action="store_true", help="只刷新在线时间（让它们重新上线）")
    args = parser.parse_args()

    conn = database.connect()
    try:
        if args.touch:
            if not args.yes:
                print("将刷新 12 位演示玩家的在线时间（前 4 位会显示在线）。")
                print("确认无误后加 --yes 再跑一次。")
                return 0
            changed = touch(conn)
            print(f"已刷新 {changed} 位演示玩家的在线时间。")
            return 0

        if args.purge:
            if not args.yes:
                print("将要删除：", "、".join(s["nickname"] for s in USERS))
                print("确认无误后加 --yes 再跑一次。")
                return 0
            removed = purge(conn)
            print(f"已删除 {removed} 个演示账号（含它们的帖子、评论、会话）。")
            return 0

        items = plan(conn)
        if not items:
            print("12 个演示账号都已经在库里了，不用再灌。")
            print("想重新来一遍：python seed_demo.py --purge --yes")
            return 0

        print(f"准备写入 {len(items)} 位演示玩家：")
        for item in items:
            spec = item["spec"]
            print(f"  · {spec['nickname']:<6} {spec['region']:<8} "
                  f"{'/'.join(spec['styles'][:2]):<10} {spec['age']} 岁")
        if not args.yes:
            print("\n这只是预览。确认后加 --yes 真的写进去。")
            return 0

        apply(conn, items)
        print(f"\n写好 {len(items)} 位。现在打开「发现同好」应该能看到他们了。")
        print("⚠️ 上线前记得：python seed_demo.py --purge --yes")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
