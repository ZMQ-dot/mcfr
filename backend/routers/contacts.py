"""联系方式交换：申请、同意、拒绝，以及屏蔽 / 举报等安全功能。

本站 v0.2 的社交模型（与第一版的「私信 + 好友」不同）：

1. 站内**不做即时聊天**，交流发生在玩家自己熟悉的社交软件里；
2. 每位玩家可以在「我的」页面预置愿意公开的 QQ / 微信 / 其他平台账号，
   这些信息**默认对任何人不可见**，接口层面也不会返回；
3. 想认识你的人发出「联系方式交换申请」并附一句话；
4. 你在登录后会看到确认弹窗，**只有点「同意」，你预置的信息才会发送给对方**，
   同时你也会拿到对方的联系方式 —— 双向交换，一次授权。

同意之前，谁都不知道对方填了什么，这是本模块最关键的安全边界。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

from ..achievements import sync_achievements
from ..database import get_db, now_ts
from ..deps import get_current_user
from ..matching import connection_ids, has_contact, match_score, relation_of, to_public
from ..schemas import BlockIn, ContactRequestIn, ReportIn

router = APIRouter(prefix="/api", tags=["联系方式交换"])

REPORT_REASONS = (
    "广告或诈骗信息", "索要真实联系方式", "辱骂或人身攻击",
    "不适合未成年人的内容", "冒充他人", "其他",
)


def _make_connection(conn: sqlite3.Connection, left: int, right: int) -> None:
    low, high = sorted((left, right))
    conn.execute(
        "INSERT OR IGNORE INTO connections(user_low, user_high, created_at) VALUES (?, ?, ?)",
        (low, high, now_ts()),
    )


def _load_active_user(conn: sqlite3.Connection, user_id: int) -> dict:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or row["status"] != "active":
        raise HTTPException(status_code=404, detail="这位玩家不存在或已离开")
    return dict(row)


def _relationship(conn: sqlite3.Connection, me: dict, other: dict) -> dict:
    """把原始行包装成前端需要的「对方资料 + 关系状态」。"""
    relation = relation_of(conn, me["id"], other["id"])
    profile = to_public(other, include_contact=relation["is_connected"])
    profile["relation"] = relation
    profile["score_info"] = match_score(me, other)
    return profile


# ================================================================ 我的伙伴

@router.get("/connections", summary="已交换联系方式的同好（含对方联系方式）")
def list_connections(
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

    items = []
    for row in rows:
        item = to_public(dict(row), include_contact=True)
        item["relation"] = {"is_connected": True}
        items.append(item)
    return {"items": items}


@router.delete("/connections/{peer_id}", summary="解除交换关系（对方联系方式将不再可见）")
def remove_connection(
    peer_id: int,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    low, high = sorted((current["id"], peer_id))
    conn.execute("DELETE FROM connections WHERE user_low = ? AND user_high = ?", (low, high))
    conn.commit()
    return {"ok": True, "message": "已解除交换，对方的联系方式不再对你可见"}


# ================================================================ 交换申请

@router.get("/contact-requests", summary="交换申请列表")
def list_requests(
    box: str = Query(default="incoming", pattern="^(incoming|outgoing)$"),
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    if box == "incoming":
        rows = conn.execute(
            """
            SELECT r.*, u.nickname, u.username, u.avatar_hue, u.edition, u.region,
                   u.play_styles, u.bio, u.last_seen, u.points
            FROM contact_requests r JOIN users u ON u.id = r.from_user
            WHERE r.to_user = ? AND r.status = 'pending'
            ORDER BY r.created_at DESC
            """,
            (current["id"],),
        ).fetchall()
        peer_key = "from_user"
    else:
        rows = conn.execute(
            """
            SELECT r.*, u.nickname, u.username, u.avatar_hue, u.edition, u.region,
                   u.play_styles, u.bio, u.last_seen, u.points
            FROM contact_requests r JOIN users u ON u.id = r.to_user
            WHERE r.from_user = ? AND r.status = 'pending'
            ORDER BY r.created_at DESC
            """,
            (current["id"],),
        ).fetchall()
        peer_key = "to_user"

    items = []
    for row in rows:
        record = dict(row)
        peer_row = {
            "id": record[peer_key],
            "username": record["username"],
            "nickname": record["nickname"],
            "avatar_hue": record["avatar_hue"],
            "edition": record["edition"],
            "region": record["region"],
            "bio": record["bio"],
            "play_styles": record["play_styles"],
            "last_seen": record["last_seen"],
            "points": record["points"],
        }
        items.append(
            {
                "id": record["id"],
                "greeting": record["greeting"],
                "created_at": record["created_at"],
                "peer": to_public(peer_row),
                "my_contact_ready": has_contact(current),
            }
        )
    return {"items": items}


@router.get("/contact-requests/pending", summary="待我处理的申请（登录后弹窗用）")
def pending_requests(
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    rows = conn.execute(
        """
        SELECT r.id, r.greeting, r.created_at, r.from_user, u.*
        FROM contact_requests r JOIN users u ON u.id = r.from_user
        WHERE r.to_user = ? AND r.status = 'pending'
        ORDER BY r.created_at ASC
        LIMIT 20
        """,
        (current["id"],),
    ).fetchall()

    items = []
    for row in rows:
        sender = dict(row)
        sender["id"] = row["from_user"]
        items.append(
            {
                "id": row["id"],
                "greeting": row["greeting"],
                "created_at": row["created_at"],
                "sender": _relationship(conn, current, sender),
                "my_contact_ready": has_contact(current),
            }
        )
    return {"items": items, "total": len(items)}


@router.post("/contact-requests/{target_id}", summary="向 TA 申请交换联系方式")
def send_request(
    target_id: int,
    payload: ContactRequestIn,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    me_id = current["id"]
    if target_id == me_id:
        raise HTTPException(status_code=400, detail="不能向自己申请")

    target = _load_active_user(conn, target_id)
    relation = relation_of(conn, me_id, target_id)

    if relation["blocked_by_me"]:
        raise HTTPException(status_code=403, detail="你已屏蔽这位玩家，请先解除屏蔽")
    if relation["blocked_me"]:
        raise HTTPException(status_code=403, detail="暂时无法向这位玩家发送申请")
    if relation["is_connected"]:
        raise HTTPException(status_code=409, detail="你们已经交换过联系方式了")

    stamp = now_ts()

    # 对方已经先申请过我 —— 直接建立交换关系，省掉一次点击
    if relation["request_state"] == "incoming":
        conn.execute(
            "UPDATE contact_requests SET status = 'accepted', handled_at = ? WHERE id = ?",
            (stamp, relation["request_id"]),
        )
        _make_connection(conn, me_id, target_id)
        conn.commit()
        unlocked = sync_achievements(conn, me_id)
        fresh = _load_active_user(conn, target_id)
        return {
            "ok": True,
            "status": "connected",
            "message": f"你们和 {target['nickname']} 互相都想认识对方，联系方式已互换",
            "peer": _relationship(conn, current, fresh),
            "unlocked": unlocked,
        }

    if relation["request_state"] == "outgoing":
        raise HTTPException(status_code=409, detail="申请已经发出，等对方回应吧")

    if not target.get("allow_contact_request", 1):
        raise HTTPException(status_code=403, detail=f"{target['nickname']} 暂时不接收交换申请")

    conn.execute(
        """
        INSERT INTO contact_requests(from_user, to_user, greeting, status, created_at)
        VALUES (?, ?, ?, 'pending', ?)
        """,
        (me_id, target_id, payload.greeting, stamp),
    )
    conn.commit()
    unlocked = sync_achievements(conn, me_id)

    hint = (
        "对方同意后，你们就能互相看到联系方式"
        if has_contact(target)
        else "对方还没有预置联系方式，可以先打个招呼"
    )
    return {
        "ok": True,
        "status": "pending",
        "message": f"申请已送达 {target['nickname']}，{hint}",
        "unlocked": unlocked,
    }


@router.post("/contact-requests/{request_id}/accept", summary="同意交换联系方式")
def accept_request(
    request_id: int,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    row = conn.execute(
        "SELECT * FROM contact_requests WHERE id = ? AND to_user = ? AND status = 'pending'",
        (request_id, current["id"]),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="申请不存在或已处理")

    stamp = now_ts()
    sender_id = int(row["from_user"])
    _make_connection(conn, current["id"], sender_id)
    conn.execute(
        "UPDATE contact_requests SET status = 'accepted', handled_at = ? WHERE id = ?",
        (stamp, request_id),
    )
    conn.commit()

    unlocked = sync_achievements(conn, current["id"])
    sender = _load_active_user(conn, sender_id)
    peer = _relationship(conn, current, sender)

    if has_contact(sender):
        message = f"已同意，你和 {sender['nickname']} 互相交换了联系方式"
    else:
        message = f"已同意 {sender['nickname']} 的申请，对方还没有填写联系方式"

    return {
        "ok": True,
        "message": message,
        "peer": peer,
        "my_contact_ready": has_contact(current),
        "unlocked": unlocked,
    }


@router.post("/contact-requests/{request_id}/reject", summary="婉拒交换申请")
def reject_request(
    request_id: int,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    row = conn.execute(
        "SELECT 1 FROM contact_requests WHERE id = ? AND to_user = ? AND status = 'pending'",
        (request_id, current["id"]),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="申请不存在或已处理")

    conn.execute(
        "UPDATE contact_requests SET status = 'rejected', handled_at = ? WHERE id = ?",
        (now_ts(), request_id),
    )
    conn.commit()
    return {"ok": True, "message": "已婉拒，对方不会看到你的联系方式"}


# ================================================================ 屏蔽

@router.get("/blocks", summary="我的屏蔽名单")
def list_blocks(
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    rows = conn.execute(
        """
        SELECT u.* FROM blocks b JOIN users u ON u.id = b.blocked_user
        WHERE b.user_id = ? ORDER BY b.created_at DESC
        """,
        (current["id"],),
    ).fetchall()
    return {"items": [to_public(dict(row)) for row in rows]}


@router.post("/blocks", summary="屏蔽某位玩家")
def add_block(
    payload: BlockIn,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    target_id = payload.target_user
    if target_id == current["id"]:
        raise HTTPException(status_code=400, detail="不能屏蔽自己")

    _load_active_user(conn, target_id)
    conn.execute(
        "INSERT OR REPLACE INTO blocks(user_id, blocked_user, created_at) VALUES (?, ?, ?)",
        (current["id"], target_id, now_ts()),
    )
    # 屏蔽后自动解除交换关系，并清掉双方未处理的申请
    low, high = sorted((current["id"], target_id))
    conn.execute("DELETE FROM connections WHERE user_low = ? AND user_high = ?", (low, high))
    conn.execute(
        """
        DELETE FROM contact_requests
        WHERE status = 'pending'
          AND ((from_user = ? AND to_user = ?) OR (from_user = ? AND to_user = ?))
        """,
        (current["id"], target_id, target_id, current["id"]),
    )
    conn.commit()
    return {"ok": True, "message": "已屏蔽，你和 TA 不会再互相看到"}


@router.delete("/blocks/{target_id}", summary="解除屏蔽")
def remove_block(
    target_id: int,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    conn.execute(
        "DELETE FROM blocks WHERE user_id = ? AND blocked_user = ?",
        (current["id"], target_id),
    )
    conn.commit()
    return {"ok": True}


# ================================================================ 举报

@router.get("/reports/reasons", summary="可选的举报原因")
def report_reasons(current: dict = Depends(get_current_user)):
    return {"items": list(REPORT_REASONS)}


@router.post("/reports", summary="举报不良内容")
def submit_report(
    payload: ReportIn,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    if payload.target_user == current["id"]:
        raise HTTPException(status_code=400, detail="不能举报自己")

    _load_active_user(conn, payload.target_user)
    conn.execute(
        """
        INSERT INTO reports(reporter_id, target_user, reason, detail, status, created_at)
        VALUES (?, ?, ?, ?, 'open', ?)
        """,
        (current["id"], payload.target_user, payload.reason, payload.detail, now_ts()),
    )
    conn.commit()
    unlocked = sync_achievements(conn, current["id"])
    return {
        "ok": True,
        "message": "举报已提交，管理员会尽快复核，感谢你守护社区",
        "unlocked": unlocked,
    }
