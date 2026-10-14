"""发现同好：推荐列表与「跳过」。

推荐策略（第一版）：
1. 先用一条 SQL 排除掉已经处理过的人（已好友、已有待处理申请、已屏蔽、已跳过）；
2. 再在内存里按可解释的匹配度排序，取前 N 个。

这样既避免了复杂推荐算法，又保证「为什么推荐 TA」是说得清楚的。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

from ..database import get_db, now_ts
from ..deps import get_current_user
from ..matching import match_score, relation_of, to_public

router = APIRouter(prefix="/api/discover", tags=["发现同好"])

CANDIDATE_SQL = """
SELECT * FROM users
WHERE id != :me
  AND status = 'active'
  AND id NOT IN (SELECT target_user FROM interests WHERE user_id = :me)
  AND id NOT IN (SELECT blocked_user FROM blocks WHERE user_id = :me)
  AND id NOT IN (SELECT user_id FROM blocks WHERE blocked_user = :me)
  AND id NOT IN (
        SELECT CASE WHEN user_low = :me THEN user_high ELSE user_low END
        FROM connections WHERE user_low = :me OR user_high = :me
  )
  AND id NOT IN (
        SELECT to_user FROM contact_requests
        WHERE from_user = :me AND status IN ('pending', 'accepted')
  )
  AND id NOT IN (
        SELECT from_user FROM contact_requests
        WHERE to_user = :me AND status = 'pending'
  )
ORDER BY last_seen DESC
LIMIT 120
"""


@router.get("", summary="获取推荐同好列表")
def feed(
    limit: int = Query(default=12, ge=1, le=36),
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    rows = conn.execute(CANDIDATE_SQL, {"me": current["id"]}).fetchall()

    scored = []
    for row in rows:
        other = dict(row)
        info = match_score(current, other)
        scored.append((info["score"], to_public(other), info["reasons"]))

    scored.sort(key=lambda item: (item[0], item[1]["last_seen"]), reverse=True)
    top = scored[:limit]

    items = []
    for score, profile, reasons in top:
        profile["score_info"] = {"score": score, "reasons": reasons}
        items.append(profile)

    total_candidates = int(
        conn.execute("SELECT COUNT(*) FROM users WHERE status = 'active' AND id != ?", (current["id"],)).fetchone()[0]
    )

    return {
        "items": items,
        "candidate_count": len(rows),
        "community_size": total_candidates,
    }


# -----------------------------------------------------------------------------
# 按名字找人
# -----------------------------------------------------------------------------

# ！LIKE 里的 % 和 _ 是通配符，用户输入必须转义 ——
#   否则输一个 % 就等于「匹配所有人」，搜索框变成一个全站用户列表入口。
#   反斜线本身也要先转义，不然输入 \% 会绕过转义。
def _like(text: str, *, prefix: bool = False) -> str:
    escaped = (text.replace("\\", "\\\\")
                   .replace("%", "\\%")
                   .replace("_", "\\_"))
    return f"{escaped}%" if prefix else f"%{escaped}%"


SEARCH_SQL = r"""
SELECT * FROM users
WHERE id != :me
  AND status = 'active'
  AND (nickname LIKE :like ESCAPE '\' OR username LIKE :like ESCAPE '\')
  AND id NOT IN (SELECT blocked_user FROM blocks WHERE user_id = :me)
  AND id NOT IN (SELECT user_id FROM blocks WHERE blocked_user = :me)
ORDER BY
  CASE
    WHEN LOWER(nickname) = LOWER(:q) OR LOWER(username) = LOWER(:q) THEN 0
    WHEN nickname LIKE :prefix ESCAPE '\' OR username LIKE :prefix ESCAPE '\' THEN 1
    ELSE 2
  END,
  last_seen DESC
LIMIT :limit
"""
# ！这里【不】排除已连接 / 已有待处理申请的人，跟推荐列表是反的：
#   推荐是「帮我找不认识的人」，搜索是「我知道我要找谁」。
#   已经交换过联系方式的人应该能搜得到。
#   屏蔽照旧双向排除 —— 那是安全边界，不是偏好。


@router.get("/search", summary="按昵称或用户名搜索同好")
def search_users(
    q: str = Query(default="", max_length=32, description="昵称或用户名的一部分"),
    limit: int = Query(default=18, ge=1, le=48),
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    """完全匹配排最前，其次是「开头对得上」，最后才是「中间含」。

    ！关键词为空时返回空列表，而不是把所有人都倒出来 —— 否则搜索框
      就变成了一个「浏览全站用户」的入口，而产品上刻意没有这个东西。
    """
    keyword = " ".join((q or "").split())
    if not keyword:
        return {"items": [], "query": "", "total": 0}

    rows = conn.execute(
        SEARCH_SQL,
        {
            "me": current["id"],
            "q": keyword,
            "like": _like(keyword),
            "prefix": _like(keyword, prefix=True),
            "limit": limit,
        },
    ).fetchall()

    items = []
    for row in rows:
        other = dict(row)
        info = match_score(current, other)
        profile = to_public(other)
        # 和推荐列表保持同一形状，前端才能直接复用同一张卡片
        profile["score_info"] = {"score": info["score"], "reasons": info["reasons"]}
        items.append(profile)

    return {"items": items, "query": keyword, "total": len(items)}


@router.post("/{target_id}/skip", summary="跳过这位同好，不再推荐")
def skip(
    target_id: int,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    if target_id == current["id"]:
        raise HTTPException(status_code=400, detail="不能跳过自己")

    relation = relation_of(conn, current["id"], target_id)
    if relation["is_connected"]:
        raise HTTPException(status_code=409, detail="你们已经交换过联系方式啦")

    conn.execute(
        """
        INSERT INTO interests(user_id, target_user, kind, created_at)
        VALUES (?, ?, 'skip', ?)
        ON CONFLICT(user_id, target_user) DO UPDATE SET kind = 'skip', created_at = excluded.created_at
        """,
        (current["id"], target_id, now_ts()),
    )
    conn.commit()
    return {"ok": True}
