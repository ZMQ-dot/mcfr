"""同好匹配度算法与用户资料序列化。

匹配度不追求"玄学推荐"，而是把开题报告里提到的交友线索
（共同玩法、共同在线时段、版本、年龄段、地区）做成可解释的分数：
用户能直接看到「为什么推荐 TA」，这比黑盒推荐更适合中小学社区。
"""

from __future__ import annotations

import sqlite3

from . import age as agelib
from .achievements import level_of, level_title
from .database import json_list, now_ts
from .media import url_of as media_url

ONLINE_WINDOW_SECONDS = 300  # 5 分钟内活跃即视为在线

EDITION_LABEL = {"java": "Java 版", "bedrock": "基岩版", "both": "双版本通吃"}


CONTACT_KEYS = ("contact_qq", "contact_wechat", "contact_other")


def has_contact(user: dict) -> bool:
    return any((user.get(key) or "").strip() for key in CONTACT_KEYS)


def to_public(user: dict, *, include_contact: bool = False) -> dict:
    """把数据库行转换成本站对外公开的资料结构（绝不包含口令哈希）。

    联系方式默认**不返回**，只有双方建立起「交换关系」后，
    调用方才会显式传入 include_contact=True。
    """
    stamp = now_ts()
    last_seen = int(user.get("last_seen") or 0)
    show_online = bool(user.get("show_online", 1))
    # 估算值（老账号用旧年龄区间算出来的）只服务于匹配打分。
    # ！对外展示的年龄必须是**用户真的填过**的：把估出来的数显示在卡片上，
    #   等于替别人编了一个岁数 —— 「这人 8 岁」和「我们不知道他几岁」
    #   是完全不同的两件事，界面不能把后者说成前者。
    declared = agelib.declared(user)
    resolved_age = agelib.resolve_age(user)
    shown_age = resolved_age if declared else None

    payload = {
        "id": user["id"],
        "username": user["username"],
        "nickname": user["nickname"],
        "avatar_hue": int(user.get("avatar_hue") or 190),
        # 用户自己上传的形象。头像优先用自定义上传的那张；
        # skin_url 是拿来渲染 3D 预览和头部的皮肤。
        # ！从微软账号导入的皮肤优先于手动上传的 —— 用户既然走了一次
        #   微软登录，显然就是想用自己正版账号上那张。手动上传时
        #   会把 mc_skin_file 清掉（见 routers/media.py），表示「我不用那个」。
        #   这样 skin_url 这个旧字段的含义不变，前端渲染一行都不用改。
        "avatar_url": media_url(user.get("avatar_file") or ""),
        "skin_url": media_url(user.get("mc_skin_file") or user.get("skin_file") or ""),
        # 披风是新增的，没有旧字段可以复用
        "cape_url": media_url(user.get("mc_cape_file") or ""),
        "mc_name": user.get("mc_name") or "",
        "mc_variant": (user.get("mc_skin_variant") or "CLASSIC").upper(),
        "bio": user.get("bio") or "",
        "edition": user.get("edition") or "java",
        "edition_label": EDITION_LABEL.get(user.get("edition") or "java", "Java 版"),
        # v0.29：对外只给「几岁」和「成年与否」，不给月和日 ——
        # 生日是本人的隐私，而匹配只需要知道「大概多大」。
        # ！没填过生日的账号这里会是 None，前端要能处理。
        "age": shown_age,
        "age_label": agelib.label(shown_age),
        "adult": agelib.is_adult(shown_age),
        # 前端靠它决定要不要弹「补填年龄」的框。
        # ！不能用 age 是不是 None 来判断：老账号会用旧年龄区间估出一个值，
        #   age 有数但生日其实没填 —— 那样就永远弹不出补填框了。
        "birth_declared": declared,
        "region": user.get("region") or "",
        "play_styles": json_list(user.get("play_styles")),
        "online_time": json_list(user.get("online_time")),
        "looking_for": json_list(user.get("looking_for")),
        "points": int(user.get("points") or 0),
        "level": level_of(int(user.get("points") or 0)),
        "level_title": level_title(level_of(int(user.get("points") or 0))),
        "profile_seen_count": int(user.get("profile_seen_count") or 0),
        "created_at": int(user.get("created_at") or 0),
        "last_seen": last_seen,
        "online": show_online and (stamp - last_seen) <= ONLINE_WINDOW_SECONDS,
        "show_online": show_online,
        "allow_contact_request": bool(user.get("allow_contact_request", 1)),
        "has_contact": has_contact(user),
    }

    if include_contact:
        payload["contact"] = {
            "qq": user.get("contact_qq") or "",
            "wechat": user.get("contact_wechat") or "",
            "other": user.get("contact_other") or "",
            "note": user.get("contact_note") or "",
        }

    return payload


def own_contact(user: dict) -> dict:
    """仅返回给本人的联系方式（用于「我的」页面编辑）。"""
    return {
        "qq": user.get("contact_qq") or "",
        "wechat": user.get("contact_wechat") or "",
        "other": user.get("contact_other") or "",
        "note": user.get("contact_note") or "",
    }


def _overlap(left: list[str], right: list[str]) -> list[str]:
    right_set = set(right)
    return [item for item in left if item in right_set]


def match_score(me: dict, other: dict) -> dict:
    """返回 {score: 0-100, reasons: [可读理由]}。"""
    my_styles = json_list(me.get("play_styles"))
    my_times = json_list(me.get("online_time"))
    my_wants = json_list(me.get("looking_for"))

    their_styles = json_list(other.get("play_styles"))
    their_times = json_list(other.get("online_time"))
    their_wants = json_list(other.get("looking_for"))

    score = 0
    reasons: list[str] = []

    shared_styles = _overlap(my_styles, their_styles)
    if shared_styles:
        score += min(len(shared_styles) * 12, 36)
        reasons.append("都爱玩" + "、".join(shared_styles[:3]))

    shared_wants = _overlap(my_wants, their_wants)
    if shared_wants:
        score += min(len(shared_wants) * 8, 16)
        reasons.append("都在找" + "、".join(shared_wants[:2]))

    shared_times = _overlap(my_times, their_times)
    if shared_times:
        score += min(len(shared_times) * 8, 16)
        reasons.append("都是" + shared_times[0] + "在线")

    my_edition = me.get("edition") or "java"
    their_edition = other.get("edition") or "java"
    if my_edition == their_edition:
        score += 14
        reasons.append(f"同为{EDITION_LABEL.get(my_edition, 'Java 版')}")
    elif "both" in (my_edition, their_edition):
        score += 12
        reasons.append("版本可以互相兼顾")

    my_age = agelib.resolve_age(me)
    their_age = agelib.resolve_age(other)
    if my_age is not None and their_age is not None:
        gap = abs(my_age - their_age)
        # 同段优先：年龄越接近分越高，差得越远扣得越多。
        # ！成年 / 未成年**不是**硬隔离（站方选的是「仍可见、只是排后面」），
        #   但跨过 16 岁那条线时额外再减一档 —— 一边是中小学生、
        #   一边是社会中人，能聊到一块儿去的概率本来就低。
        cross_adult = agelib.is_adult(my_age) != agelib.is_adult(their_age)
        if gap == 0:
            score += 12
            reasons.append("和你同岁")
        elif gap <= 2:
            score += 10
            # ！跨成年线时不写「只差 N 岁」：那句话读起来是「你们很近」，
            #   而这条恰恰是被扣了分的情况，写出来反而误导。
            if not cross_adult:
                reasons.append(f"只差 {gap} 岁")
        elif gap <= 4:
            score += 5
        elif gap >= 8:
            score -= 10
        if cross_adult:
            score -= 8

    my_region = (me.get("region") or "").strip()
    their_region = (other.get("region") or "").strip()
    if my_region and my_region == their_region:
        score += 10
        reasons.append(f"同在{their_region}")

    if (other.get("bio") or "").strip():
        score += 4
    if now_ts() - int(other.get("last_seen") or 0) < 86400:
        score += 6

    # ！下界也要卡住。年龄项是**减法**（差得远和跨成年线都扣分），
    #   不卡 0 的话卡片上会出现「match -4」这种分数 ——
    #   以前所有项都是加分，所以只写了 min(100)，负数这个坑一直在那儿。
    return {"score": max(0, min(score, 100)), "reasons": reasons[:3]}


def relation_of(conn: sqlite3.Connection, me_id: int, other_id: int) -> dict:
    """查询我与某人之间的关系状态。"""
    low, high = sorted((me_id, other_id))
    is_connected = (
        conn.execute(
            "SELECT 1 FROM connections WHERE user_low = ? AND user_high = ?", (low, high)
        ).fetchone()
        is not None
    )

    request_state = "none"
    request_id = None
    row = conn.execute(
        """
        SELECT id, from_user, status FROM contact_requests
        WHERE status = 'pending'
          AND ((from_user = ? AND to_user = ?) OR (from_user = ? AND to_user = ?))
        ORDER BY id DESC LIMIT 1
        """,
        (me_id, other_id, other_id, me_id),
    ).fetchone()
    if row is not None:
        request_id = row["id"]
        request_state = "outgoing" if row["from_user"] == me_id else "incoming"

    return {
        "is_connected": is_connected,
        "request_state": request_state,
        "request_id": request_id,
        "blocked_by_me": conn.execute(
            "SELECT 1 FROM blocks WHERE user_id = ? AND blocked_user = ?", (me_id, other_id)
        ).fetchone()
        is not None,
        "blocked_me": conn.execute(
            "SELECT 1 FROM blocks WHERE user_id = ? AND blocked_user = ?", (other_id, me_id)
        ).fetchone()
        is not None,
    }


def connection_ids(conn: sqlite3.Connection, user_id: int) -> set[int]:
    """我已经交换过联系方式的同好 id 集合。"""
    rows = conn.execute(
        "SELECT user_low, user_high FROM connections WHERE user_low = ? OR user_high = ?",
        (user_id, user_id),
    ).fetchall()
    return {row["user_high"] if row["user_low"] == user_id else row["user_low"] for row in rows}
