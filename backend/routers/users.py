"""用户资料、隐私设置与个人中心。"""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from .. import age, posts
from ..achievements import collect_stats, full_catalog, level_progress, sync_achievements
from ..database import get_db
from ..deps import get_current_user
from ..matching import connection_ids, match_score, own_contact, relation_of, to_public
from ..schemas import BirthIn, PrivacyIn, ProfileUpdateIn

router = APIRouter(prefix="/api/users", tags=["用户"])

# ！白名单里**没有** birth_year / birth_month / birth_day，
#   这是故意的：年龄一旦能改，匹配就失去意义 ——
#   谁都会把自己改成想匹配的那个岁数。补填走 POST /me/birth，只能写一次。
#   age_group 在 v0.29 已停用，也从这里拿掉了。
EDITABLE_FIELDS = (
    "nickname", "bio", "edition", "region",
    "play_styles", "online_time", "looking_for",
    "contact_qq", "contact_wechat", "contact_other", "contact_note",
)
JSON_FIELDS = {"play_styles", "online_time", "looking_for"}


@router.get("/me/overview", summary="个人中心汇总数据")
def my_overview(
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    me_id = current["id"]
    payload = to_public(current)
    payload["is_self"] = True
    # 邮箱只回给本人，to_public() 对其他人一律不返回
    payload["email"] = current.get("email") or ""

    # ！统计只算一次，然后同时给下面两个地方用（v0.31）。
    #   collect_stats 要跑 7 条查询，而 full_catalog 内部默认也会再调它一次 ——
    #   不传进去的话，这个「每次开页面都要跑」的接口就白做了一倍的活。
    stats = collect_stats(conn, me_id)
    stats["pending_requests"] = int(
        conn.execute(
            "SELECT COUNT(*) FROM contact_requests WHERE to_user = ? AND status = 'pending'",
            (me_id,),
        ).fetchone()[0]
    )
    # 信息中心的未读角标。侧栏「消息」那一格读的就是它 ——
    # ！侧栏角标机制（NAV 里的 badge 字段）只认 stats 下的键，
    #   所以必须放在这里，而不是另开一个接口让前端去拼。
    stats["unread_notifications"] = posts.unread_count(conn, int(me_id))

    return {
        "user": payload,
        "contact": own_contact(current),
        "level": level_progress(int(current.get("points") or 0)),
        "stats": stats,
        "achievements": full_catalog(conn, me_id, stats),
    }


@router.put("/me", summary="更新个人资料")
def update_me(
    payload: ProfileUpdateIn,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    data = payload.model_dump(exclude_unset=True)
    updates = {key: value for key, value in data.items() if key in EDITABLE_FIELDS and value is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="没有需要更新的内容")

    assignments: list[str] = []
    values: list[object] = []
    for key, value in updates.items():
        assignments.append(f"{key} = ?")
        values.append(json.dumps(value, ensure_ascii=False) if key in JSON_FIELDS else value)

    values.append(current["id"])
    conn.execute(f"UPDATE users SET {', '.join(assignments)} WHERE id = ?", tuple(values))
    conn.commit()

    unlocked = sync_achievements(conn, current["id"])
    row = conn.execute("SELECT * FROM users WHERE id = ?", (current["id"],)).fetchone()
    fresh = dict(row)
    return {
        "user": to_public(fresh),
        "contact": own_contact(fresh),
        "level": level_progress(int(fresh.get("points") or 0)),
        "unlocked": unlocked,
    }


@router.patch("/me/privacy", summary="更新隐私与安全设置")
def update_privacy(
    payload: PrivacyIn,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail="没有需要更新的设置")

    assignments: list[str] = []
    values: list[object] = []
    for key, value in data.items():
        assignments.append(f"{key} = ?")
        values.append(1 if value else 0)
    values.append(current["id"])

    conn.execute(f"UPDATE users SET {', '.join(assignments)} WHERE id = ?", tuple(values))
    conn.commit()

    row = conn.execute("SELECT * FROM users WHERE id = ?", (current["id"],)).fetchone()
    return {"user": to_public(dict(row))}


@router.post("/me/birth", summary="补填年龄与生日（只写一次，之后不可改）")
def set_birth(
    payload: BirthIn,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    """给 v0.29 以前注册的老账号补填出生日期。

    ！只写一次。已填过的账号调这个接口会拿到 409 ——
      否则它就变成了「一个可以随便改年龄的后门」，
      而整个功能的价值就建立在「改不了」上面。
      真填错了只能找管理员在数据库层面处理，这是刻意的摩擦。
    """
    if age.declared(current):
        raise HTTPException(status_code=409, detail="年龄和生日已经填过了，不能再修改。")

    year = age.birth_year_from(payload.age, payload.birth_month, payload.birth_day)
    conn.execute(
        "UPDATE users SET birth_year = ?, birth_month = ?, birth_day = ? WHERE id = ?",
        (year, payload.birth_month, payload.birth_day, current["id"]),
    )
    conn.commit()

    unlocked = sync_achievements(conn, current["id"])
    row = conn.execute("SELECT * FROM users WHERE id = ?", (current["id"],)).fetchone()
    return {"user": to_public(dict(row)), "unlocked": unlocked}


@router.get("/me/achievements", summary="我的成就墙")
def my_achievements(
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    return full_catalog(conn, current["id"])


@router.get("/me/contacts", summary="已交换联系方式的同好（含对方联系方式）")
def my_contacts(
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    ids = connection_ids(conn, current["id"])
    if not ids:
        return {"items": []}

    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM users WHERE id IN ({placeholders}) ORDER BY last_seen DESC",
        tuple(ids),
    ).fetchall()
    return {"items": [to_public(dict(row), include_contact=True) for row in rows]}


@router.get("/{user_id}", summary="查看某位同好的公开资料")
def view_profile(
    user_id: int,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or row["status"] != "active":
        raise HTTPException(status_code=404, detail="这位玩家不存在或已离开")

    other = dict(row)
    relation = relation_of(conn, current["id"], user_id)

    if relation["blocked_by_me"]:
        raise HTTPException(status_code=403, detail="你已屏蔽这位玩家，可在设置中解除")
    # ！对方屏蔽了我，我也不能再看他的资料。
    #   以前只拦了 blocked_by_me，于是推荐列表和搜索都把他藏起来了，
    #   但直接拼一个 /api/users/<id> 依然打得开 —— user_id 是连续整数，
    #   遍历一遍就绕过了。界面上的原话是「屏蔽后你们将不再互相看到」，
    #   那就得真的互相看不到。
    # ！文案跟上面保持一致，不要写成「对方屏蔽了你」——
    #   那等于告诉发起方“你被谁屏蔽了”，反而是一种信息泄露。
    if relation["blocked_me"]:
        raise HTTPException(status_code=403, detail="你已屏蔽这位玩家，可在设置中解除")

    is_self = current["id"] == user_id
    if not is_self:
        conn.execute(
            "UPDATE users SET profile_seen_count = profile_seen_count + 1 WHERE id = ?",
            (user_id,),
        )
        # ！同时给自己记一笔「我看过别人」：两个方向都要有记录，
        #   否则「探索者（浏览 20 位同好）」只能去读人家的被看数，
        #   被「人气之星」完全盖住（v0.22 修的就是这个）。
        conn.execute(
            "UPDATE users SET profile_view_count = profile_view_count + 1 WHERE id = ?",
            (current["id"],),
        )
        conn.commit()
        other["profile_seen_count"] = int(other["profile_seen_count"] or 0) + 1
        sync_achievements(conn, user_id)
        # 浏览者自己这边也可能因此解锁「探索者」，得同步一次
        sync_achievements(conn, current["id"])

    profile = to_public(other, include_contact=is_self or relation["is_connected"])
    profile["is_self"] = is_self
    profile["relation"] = relation
    profile["score_info"] = {"score": 0, "reasons": []} if is_self else match_score(current, other)

    badges = conn.execute(
        """
        SELECT ua.code, ua.unlocked_at FROM user_achievements ua
        WHERE ua.user_id = ? ORDER BY ua.unlocked_at ASC
        """,
        (user_id,),
    ).fetchall()

    from ..achievements import ACHIEVEMENTS

    by_code = {item["code"]: item for item in ACHIEVEMENTS}
    unlocked = [
        {**by_code[row["code"]], "unlocked_at": row["unlocked_at"]}
        for row in badges
        if row["code"] in by_code
    ]

    return {"user": profile, "badges": unlocked}
