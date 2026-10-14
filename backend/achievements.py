"""成就系统：定义、解锁判定与等级换算。

成就的作用是「让分享和交流有正反馈」（开题报告中的激励闭环）。

# v0.22：把「定义 / 判定 / 进度条」三份并列的真相合成一份

改之前，加一条成就得动三个地方：

    ACHIEVEMENTS          名字、描述、分值
    _conditions()         达没达成的判定
    full_catalog() 里两个手写字典 progress_map / targets    进度条

漏掉任何一处，结果分别是：成就页直接 KeyError 打不开、
或者进度条永远停在 0/5。而判定和进度条写的**其实是同一个条件**，
却要人肉保证它们一致。

现在每条成就自带 `target`（满值）和 `progress(stats)`（当前值），
达成条件就是 `progress >= target` ——
两者不可能对不上，因为它们本来就是同一个数。加一条成就只改一处。

# v0.22：顺手修掉了 explorer 用错字段的问题

「探索者（浏览 20 位同好）」原来和「人气之星（被 10 位查看）」
读的是**同一个** `profile_seen_count`。而那一列的含义是
「我的主页被多少人看过」（见 routers/users.py 的写入点），
所以「探索者」实际上被「人气之星」完全盖住，永远不可能单独拿到。

现在新增 `profile_view_count`（我主动看过多少位），两者才真正分开。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

from .database import json_list, now_ts

# ---------------------------------------------------------------- 等级

# ！阈值是照着「全部成就一共多少分」调的，改成就分值时记得回头看一眼这个数组。
#   当前满配 760 分，所以 10 级定在 720 —— 差 40 分也能满级，
#   不必把最后一条犄角旮旯的成就也啃下来才算通关。
#   另外：新阈值只能比旧的**低或相等**，否则已经攒了分的老用户会凭空掉级。
LEVEL_THRESHOLDS: tuple[int, ...] = (0, 30, 80, 150, 240, 340, 440, 540, 630, 720)

LEVEL_TITLES: tuple[str, ...] = (
    "新来的方块", "挖矿萌新", "石镐玩家", "铁装学徒", "钻石旅人",
    "下界行者", "末地远航", "信标建造者", "区块之主", "传奇同好",
)


def level_of(points: int) -> int:
    level = 1
    for index, threshold in enumerate(LEVEL_THRESHOLDS, start=1):
        if points >= threshold:
            level = index
    return level


def level_title(level: int) -> str:
    return LEVEL_TITLES[min(max(level, 1), len(LEVEL_TITLES)) - 1]


def level_progress(points: int) -> dict[str, Any]:
    """返回当前等级、下一等级所需积分与进度百分比。"""
    level = level_of(points)
    current_floor = LEVEL_THRESHOLDS[level - 1]
    if level >= len(LEVEL_THRESHOLDS):
        return {
            "level": level,
            "title": level_title(level),
            "points": points,
            "next_points": None,
            "progress": 1.0,
        }
    next_floor = LEVEL_THRESHOLDS[level]
    span = max(next_floor - current_floor, 1)
    return {
        "level": level,
        "title": level_title(level),
        "points": points,
        "next_points": next_floor,
        "progress": round(min(max((points - current_floor) / span, 0.0), 1.0), 4),
    }


# ---------------------------------------------------------------- 小工具

def _flag(condition: Any) -> int:
    """把「有没有」压成 0/1，用于满值是 1 的那类成就。"""
    return 1 if condition else 0


def _capped(value: Any, cap: int) -> int:
    """把计数裁到 [0, cap]：进度条不该因为用户超额完成而超出满格。"""
    try:
        return min(max(int(value or 0), 0), cap)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- 定义
#
# 每条成就的字段：
#   code     唯一标识，解锁记录存在 user_achievements.code 里，**改了等于清零**
#   name     成就名（显示在徽章和弹窗上）
#   desc     一句话说明「怎么拿到」，要能让小学生看懂
#   glyph    徽章上的符号（mcicons.js 会按 code 画等距图标，这里是兜底）
#   points   解锁时加的积分
#   target   进度条满值
#   progress 从 stats 取当前值，与 target 比较决定解锁 —— 见文件头

ACHIEVEMENTS: tuple[dict[str, Any], ...] = (
    # ---------------- 起步：把资料填起来 ----------------
    {
        "code": "first_step",
        "name": "初次启程",
        "desc": "写下个人简介，并选择至少一种喜欢的玩法",
        "glyph": "◆",
        "points": 10,
        "target": 2,
        "progress": lambda s: _flag(s["bio_len"] > 0) + _flag(s["style_count"] >= 1),
    },
    {
        "code": "self_intro",
        "name": "自我介绍",
        "desc": "把个人简介写到 100 字，让同好一眼看懂你在玩什么",
        "glyph": "❡",
        "points": 20,
        "target": 100,
        "progress": lambda s: _capped(s["bio_len"], 100),
    },
    {
        "code": "all_rounder",
        "name": "全能玩家",
        "desc": "填写 5 种以上喜欢的玩法",
        "glyph": "✧",
        "points": 25,
        "target": 5,
        "progress": lambda s: _capped(s["style_count"], 5),
    },
    {
        "code": "full_profile",
        "name": "有备而来",
        "desc": "简介、地区、年龄、玩法、在线时段、想找的伙伴都填上",
        "glyph": "▤",
        "points": 20,
        "target": 6,
        "progress": lambda s: (
            _flag(s["bio_len"] > 0)
            + _flag(s["region_len"] > 0)
            + _flag(s["age_known"])
            + _flag(s["style_count"] >= 1)
            + _flag(s["time_count"] >= 1)
            + _flag(s["want_count"] >= 1)
        ),
    },
    {
        "code": "open_book",
        "name": "开诚布公",
        "desc": "在资料里预置了至少一项联系方式",
        "glyph": "◈",
        "points": 15,
        "target": 1,
        "progress": lambda s: _flag(s["has_contact"]),
    },
    {
        "code": "thoughtful",
        "name": "体贴的人",
        "desc": "写下「给对方的备注」，让人知道怎么跟你打招呼",
        "glyph": "✎",
        "points": 15,
        "target": 1,
        "progress": lambda s: _flag(s["has_note"]),
    },

    # ---------------- 形象 ----------------
    {
        "code": "dressed_up",
        "name": "独一无二",
        "desc": "上传一张自定义头像，换掉默认的像素脸",
        "glyph": "☺",
        "points": 15,
        "target": 1,
        "progress": lambda s: _flag(s["has_avatar"]),
    },
    {
        "code": "skin_maker",
        "name": "皮肤设计师",
        "desc": "上传一张自己的 Minecraft 皮肤",
        "glyph": "⧉",
        "points": 25,
        "target": 1,
        "progress": lambda s: _flag(s["has_skin"]),
    },

    # ---------------- 社交 ----------------
    {
        "code": "hello_world",
        "name": "破冰问候",
        "desc": "向第一位同好发出好友申请",
        "glyph": "✦",
        "points": 10,
        "target": 1,
        "progress": lambda s: _capped(s["request_sent"], 1),
    },
    {
        "code": "first_contact",
        "name": "初次联络",
        "desc": "第一次和同好交换联系方式",
        "glyph": "⬢",
        "points": 20,
        "target": 1,
        "progress": lambda s: _capped(s["connection_count"], 1),
    },
    {
        "code": "squad_five",
        "name": "五人小队",
        "desc": "与 5 位同好交换了联系方式，可以开一局了",
        "glyph": "⬡",
        "points": 40,
        "target": 5,
        "progress": lambda s: _capped(s["connection_count"], 5),
    },
    {
        "code": "old_friends",
        "name": "联络大师",
        "desc": "与 10 位同好交换了联系方式",
        "glyph": "❖",
        "points": 60,
        "target": 10,
        "progress": lambda s: _capped(s["connection_count"], 10),
    },
    {
        "code": "friends_plus",
        "name": "伙伴遍天下",
        "desc": "与 25 位同好交换了联系方式",
        "glyph": "⁂",
        "points": 100,
        "target": 25,
        "progress": lambda s: _capped(s["connection_count"], 25),
    },
    {
        "code": "good_neighbor",
        "name": "热心邻居",
        "desc": "同意了 5 位同好的联系方式交换申请",
        "glyph": "⟡",
        "points": 30,
        "target": 5,
        "progress": lambda s: _capped(s["request_accepted"], 5),
    },
    {
        "code": "picker",
        "name": "宁缺毋滥",
        "desc": "认真跳过 10 个觉得合不来的推荐 —— 不将就也是一种本事",
        "glyph": "⌦",
        "points": 15,
        "target": 10,
        "progress": lambda s: _capped(s["skip_count"], 10),
    },

    # ---------------- 逛一逛 ----------------
    {
        "code": "explorer",
        "name": "探索者",
        "desc": "浏览 20 位同好的个人资料",
        "glyph": "◉",
        "points": 15,
        "target": 20,
        "progress": lambda s: _capped(s["profile_view_count"], 20),
    },
    {
        "code": "explorer_plus",
        "name": "见多识广",
        "desc": "浏览 50 位同好的个人资料",
        "glyph": "◎",
        "points": 30,
        "target": 50,
        "progress": lambda s: _capped(s["profile_view_count"], 50),
    },
    {
        "code": "popular",
        "name": "人气之星",
        "desc": "被 10 位同好查看过个人主页",
        "glyph": "★",
        "points": 25,
        "target": 10,
        "progress": lambda s: _capped(s["profile_seen_count"], 10),
    },
    {
        "code": "popular_plus",
        "name": "社区明星",
        "desc": "被 50 位同好查看过个人主页",
        "glyph": "✪",
        "points": 50,
        "target": 50,
        "progress": lambda s: _capped(s["profile_seen_count"], 50),
    },

    # ---------------- 登录节律 ----------------
    #
    # ！这里刻意**没有**「夜猫子（深夜登录 N 次）」这类可以重复刷的成就。
    #   本站面向中小学生，一个鼓励「今晚再熬一次」的徽章是反向激励 ——
    #   深夜登录只保留一条一次性的「深夜建筑师」，撞见就撞见了，不值得追。
    #   力气放在「连续来」上，那才是真正想让人养成的习惯。
    {
        "code": "night_architect",
        "name": "深夜建筑师",
        "desc": "在 22:00 至次日 05:00 之间登录过",
        "glyph": "☾",
        "points": 15,
        "target": 1,
        "progress": lambda s: _flag(s.get("night_login")),
    },
    {
        "code": "early_bird",
        "name": "早起的鸟",
        "desc": "在早上 5 点到 8 点之间登录过",
        "glyph": "☀",
        "points": 15,
        "target": 1,
        "progress": lambda s: _flag(s.get("early_login")),
    },
    {
        "code": "weekend_player",
        "name": "周末玩家",
        "desc": "在周末登录过 —— 平时上学，周末才是主战场",
        "glyph": "◇",
        "points": 10,
        "target": 1,
        "progress": lambda s: _flag(s.get("weekend_login")),
    },
    {
        "code": "three_days",
        "name": "三日之约",
        "desc": "连续 3 天回来看看",
        "glyph": "⋮",
        "points": 25,
        "target": 3,
        "progress": lambda s: _capped(s["login_streak"], 3),
    },
    {
        "code": "week_streak",
        "name": "一周不缺席",
        "desc": "连续 7 天回来看看",
        "glyph": "⌛",
        "points": 50,
        "target": 7,
        "progress": lambda s: _capped(s["login_streak"], 7),
    },
    {
        "code": "monthly",
        "name": "月度常客",
        "desc": "累计在 30 个不同的日子登录过",
        "glyph": "▦",
        "points": 60,
        "target": 30,
        "progress": lambda s: _capped(s["login_days"], 30),
    },

    # ---------------- 社区 ----------------
    {
        "code": "safe_guard",
        "name": "安全卫士",
        "desc": "举报过一次不良内容，共同守护社区",
        "glyph": "⛨",
        "points": 20,
        "target": 1,
        "progress": lambda s: _capped(s["report_count"], 1),
    },
    {
        "code": "feedback_giver",
        "name": "有话直说",
        "desc": "提交过一次意见反馈 —— 这个站点就是这样一点点变好的",
        "glyph": "✉",
        "points": 15,
        "target": 1,
        "progress": lambda s: _capped(s["feedback_count"], 1),
    },
    {
        "code": "both_editions",
        "name": "双版本通吃",
        "desc": "在资料里选上「双版本通吃」，Java 和基岩版的朋友都能找到你",
        "glyph": "⇄",
        "points": 10,
        "target": 1,
        "progress": lambda s: _flag(s["edition_both"]),
    },
)

_BY_CODE: dict[str, dict[str, Any]] = {item["code"]: item for item in ACHIEVEMENTS}

# 给前端的字段。`progress` 是个函数，不能直接 json 出去 ——
# ！所有对外输出都必须走 _public()，别把 ACHIEVEMENTS 原样丢给 FastAPI。
_PUBLIC_FIELDS = ("code", "name", "desc", "glyph", "points", "target")


def _public(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item[key] for key in _PUBLIC_FIELDS}


def catalog() -> list[dict[str, Any]]:
    """返回可序列化的成就定义（不含 progress 函数）。"""
    return [_public(item) for item in ACHIEVEMENTS]


# ---------------------------------------------------------------- 判定

def collect_stats(conn: sqlite3.Connection, user_id: int) -> dict[str, int]:
    """一次性收集判定所需的所有计数，避免逐条成就查库。"""
    row = conn.execute(
        """
        SELECT
            LENGTH(TRIM(bio))                        AS bio_len,
            LENGTH(TRIM(region))                     AS region_len,
            play_styles, online_time, looking_for,
            contact_qq, contact_wechat, contact_other, contact_note,
            avatar_file, skin_file, edition,
            birth_year, birth_month, birth_day,
            profile_seen_count, profile_view_count,
            login_streak, login_days
        FROM users WHERE id = ?
        """,
        (user_id,),
    ).fetchone()
    if row is None:
        return {}

    stats: dict[str, int] = {
        "bio_len": int(row["bio_len"] or 0),
        "region_len": int(row["region_len"] or 0),
        "style_count": len(json_list(row["play_styles"])),
        "time_count": len(json_list(row["online_time"])),
        "want_count": len(json_list(row["looking_for"])),
        # ！v0.29 起看的是出生日期，不再是旧的「年龄区间」下拉。
        #   老账号补填之前这条成就是没解锁的 —— 这正好，
        #   补填完 sync_achievements 会顺手把它点亮。
        "age_known": _flag(bool(row["birth_year"] and row["birth_month"] and row["birth_day"])),
        "has_contact": _flag(any(
            (row[key] or "").strip()
            for key in ("contact_qq", "contact_wechat", "contact_other")
        )),
        "has_note": _flag((row["contact_note"] or "").strip()),
        "has_avatar": _flag(row["avatar_file"]),
        "has_skin": _flag(row["skin_file"]),
        "edition_both": _flag(row["edition"] == "both"),
        "profile_seen_count": int(row["profile_seen_count"] or 0),
        "profile_view_count": int(row["profile_view_count"] or 0),
        "login_streak": int(row["login_streak"] or 0),
        "login_days": int(row["login_days"] or 0),
    }

    stats["connection_count"] = int(
        conn.execute(
            "SELECT COUNT(*) FROM connections WHERE user_low = ? OR user_high = ?",
            (user_id, user_id),
        ).fetchone()[0]
    )
    stats["request_sent"] = int(
        conn.execute(
            "SELECT COUNT(*) FROM contact_requests WHERE from_user = ?",
            (user_id,),
        ).fetchone()[0]
    )
    stats["request_accepted"] = int(
        conn.execute(
            "SELECT COUNT(*) FROM contact_requests WHERE to_user = ? AND status = 'accepted'",
            (user_id,),
        ).fetchone()[0]
    )
    stats["report_count"] = int(
        conn.execute(
            "SELECT COUNT(*) FROM reports WHERE reporter_id = ?",
            (user_id,),
        ).fetchone()[0]
    )
    stats["skip_count"] = int(
        conn.execute(
            "SELECT COUNT(*) FROM interests WHERE user_id = ? AND kind = 'skip'",
            (user_id,),
        ).fetchone()[0]
    )
    stats["feedback_count"] = int(
        conn.execute(
            "SELECT COUNT(*) FROM feedback WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]
    )
    return stats


def progress_of(item: dict[str, Any], stats: dict[str, int]) -> int:
    """算某条成就当前的进度值。

    ！缺字段一律当 0，不抛异常：
      「深夜 / 早上 / 周末登录」这几个信号只在**登录那一刻**由调用方通过
      extra 传进来（见 routers/auth.py），成就页、资料页这些地方是没有的。
      在那几处显示 0 完全正常 —— 已经解锁的会直接显示满值，不会像倒退。
    """
    fn: Callable[[dict[str, int]], int] | None = item.get("progress")
    if fn is None:
        return 0
    try:
        return max(int(fn(stats)), 0)
    except (KeyError, TypeError, ValueError):
        return 0


def sync_achievements(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    extra: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """检查并解锁新成就，返回本次新解锁的成就列表。

    `extra` 用于传入不入库的瞬时条件（例如「深夜登录」）。
    """
    stats = collect_stats(conn, user_id)
    if not stats:
        return []
    if extra:
        stats.update(extra)

    unlocked_rows = conn.execute(
        "SELECT code FROM user_achievements WHERE user_id = ?", (user_id,)
    ).fetchall()
    unlocked = {row["code"] for row in unlocked_rows}

    fresh: list[dict[str, Any]] = []
    gained = 0
    stamp = now_ts()

    for item in ACHIEVEMENTS:
        code = item["code"]
        # ！先看有没有解锁过，再算进度：算进度要查一堆 stats，
        #   而绝大多数成就早就解锁了，提前 continue 能省掉大部分无谓比较。
        if code in unlocked:
            continue
        if progress_of(item, stats) < int(item["target"]):
            continue

        conn.execute(
            "INSERT OR IGNORE INTO user_achievements(user_id, code, unlocked_at) VALUES (?, ?, ?)",
            (user_id, code, stamp),
        )
        gained += int(item["points"])
        fresh.append({**_public(item), "unlocked_at": stamp})

    if gained:
        conn.execute(
            "UPDATE users SET points = points + ? WHERE id = ?", (gained, user_id)
        )
    conn.commit()
    return fresh


def full_catalog(
    conn: sqlite3.Connection,
    user_id: int,
    stats: dict[str, int] | None = None,
) -> dict[str, Any]:
    """返回「全部成就 + 我的解锁状态」，供成就页展示。

    ！stats 允许外部传进来，这是 v0.31 补的一个真实浪费：
      collect_stats 一口气要跑 7 条查询，而「个人中心汇总」那个接口
      每次开页面都要调它，调完紧接着又调本函数 —— 不传的话，
      同样那 7 条查询白白再跑一遍（实测占了该接口一半的查询数）。
      调用方手里已经有 stats 时务必传进来，见 routers/users.py 的 my_overview。
    """
    rows = conn.execute(
        "SELECT code, unlocked_at FROM user_achievements WHERE user_id = ?", (user_id,)
    ).fetchall()
    unlocked = {row["code"]: row["unlocked_at"] for row in rows}
    if stats is None:
        stats = collect_stats(conn, user_id)

    items = []
    for item in ACHIEVEMENTS:
        code = item["code"]
        done = code in unlocked
        target = int(item["target"])
        items.append(
            {
                **_public(item),
                "unlocked": done,
                "unlocked_at": unlocked.get(code),
                # 已解锁的直接顶到满格，免得出现「已拿到但进度显示 3/5」
                "current": target if done else min(progress_of(item, stats), target),
            }
        )

    row = conn.execute("SELECT points FROM users WHERE id = ?", (user_id,)).fetchone()
    points = int(row["points"]) if row is not None else 0
    return {
        "items": items,
        "unlocked_count": len(unlocked),
        "total_count": len(ACHIEVEMENTS),
        "level": level_progress(points),
    }
