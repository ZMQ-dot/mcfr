"""公共依赖：登录态解析与当前用户。

鉴权采用简单的 Bearer Token + sessions 表，
比 JWT 更适合这一版：可即时吊销（登出、封禁、改密码）。
"""

from __future__ import annotations

import sqlite3

from fastapi import Depends, Header, HTTPException, Request

from . import age
from .database import get_db, now_ts

# 补填年龄之前仍然放行的接口（见 get_current_user 里那段说明）。
_BIRTH_EXEMPT = frozenset({
    "/api/users/me/overview",   # 前端要靠它才知道该弹补填框
    "/api/users/me/birth",      # 补填本身
    "/api/auth/logout",         # 想走就走
    "/api/auth/account",        # 注销账号，不想填的人总得有路离开
})


def resolve_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def load_user_by_token(conn: sqlite3.Connection, token: str) -> dict | None:
    row = conn.execute(
        """
        SELECT u.* FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.token = ? AND s.expires_at > ?
        """,
        (token, now_ts()),
    ).fetchone()
    return dict(row) if row is not None else None


def get_optional_user(
    authorization: str | None = Header(default=None),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict | None:
    """允许匿名访问的接口使用；未登录返回 None。"""
    token = resolve_token(authorization)
    if not token:
        return None
    user = load_user_by_token(conn, token)
    if user is None or user["status"] != "active":
        return None
    return user


def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """必须登录的接口使用。"""
    token = resolve_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="请先登录后再操作")

    user = load_user_by_token(conn, token)
    if user is None:
        raise HTTPException(status_code=401, detail="登录状态已过期，请重新登录")
    if user["status"] != "active":
        raise HTTPException(status_code=403, detail="该账号已被暂停使用")

    # 没填过年龄和生日的老账号（v0.28 及以前注册的）先补填，再谈别的。
    # ！这道闸放在这里，而不是每个业务接口里各写一遍 ——
    #   「哪个接口忘了加」是没法靠读代码保证的，而漏一个就等于开了个后门。
    #   例外只有三条，都是「不补填也必须能用」的：看自己的汇总（前端要靠它
    #   才知道该弹补填框）、补填本身、以及登出和注销账号（不想填的人总得
    #   有办法离开）。
    if not age.declared(user) and request.url.path not in _BIRTH_EXEMPT:
        raise HTTPException(
            status_code=428,
            detail="请先填写年龄和生日。填过之后不能再改，它会用于同好匹配。",
        )

    stamp = now_ts()
    conn.execute("UPDATE users SET last_seen = ? WHERE id = ?", (stamp, user["id"]))
    conn.commit()
    user["last_seen"] = stamp
    return user
