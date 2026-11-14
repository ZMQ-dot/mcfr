"""圈子相关的 HTTP 接口。

分工与 routers/posts.py 完全一致：这个文件只管「收参数、调 backend/circles.py、
把结果原样返回」，所有规则（谁算成员、退出时圈子怎么办、名字撞车）都在
backend/circles.py 里。**不要在这里写业务判断** —— 后台批处理和脚本不经过 HTTP，
一旦规则写在这里，它们就绕过了校验。

错误处理：circles.CircleError 由 main.py 注册的全局异常处理器统一转成
HTTP 响应，状态码跟着 exc.status 走。所以这里看不到 try/except。

路由声明顺序：`/circles` 和 `/circles/{circle_id}` 的路径段数不同，不会互撞；
但 `/circles/{circle_id}/posts` 这类更长的路径必须写在 `/circles/{circle_id}`
之后也没关系 —— FastAPI 先按路径结构匹配，`/circles/3/posts` 不会落到
`/circles/{circle_id}` 上（段数就不一样）。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query

from .. import circles
from ..database import get_db
from ..deps import get_current_user
from ..schemas import CircleIn

router = APIRouter(prefix="/api", tags=["圈子"])


# ================================================================ 列表与创建

@router.get("/circles", summary="圈子列表（scope=mine 我加入的 / all 全部）")
def list_circles(
    scope: str = Query(default="all", pattern="^(mine|all)$"),
    q: str = Query(default="", max_length=40),
    page: int = Query(default=1, ge=1),
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    return circles.list_circles(
        conn, int(current["id"]), scope=scope, keyword=q, page=page
    )


@router.post("/circles", summary="创建一个圈子（创建者自动成为圈主并加入）")
def create_circle(
    payload: CircleIn,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    return circles.create_circle(
        conn, current,
        name=payload.name,
        intro=payload.intro,
        about=payload.about,
        topic=payload.topic,
    )


# ================================================================ 单个圈子

@router.get("/circles/{circle_id}", summary="圈子详情")
def read_circle(
    circle_id: int,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    return circles.get_circle(conn, int(current["id"]), circle_id)


@router.get("/circles/{circle_id}/members", summary="圈子成员")
def list_members(
    circle_id: int,
    limit: int = Query(default=circles.MEMBER_PAGE, ge=1, le=circles.MEMBER_PAGE),
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    # 先确认圈子还在，免得拿它当探针去猜「哪些 id 是活的圈子」
    circles.get_circle(conn, int(current["id"]), circle_id)
    return {"items": circles.members_of(conn, circle_id, limit=limit)}


@router.get("/circles/{circle_id}/posts", summary="圈子里的帖子（仅成员可见）")
def list_circle_posts(
    circle_id: int,
    q: str = Query(default="", max_length=40),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=50),
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    return circles.circle_posts(
        conn, int(current["id"]), circle_id,
        keyword=q, page=page, limit=limit,
    )


# ================================================================ 加入与退出

@router.post("/circles/{circle_id}/join", summary="加入圈子")
def join_circle(
    circle_id: int,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    return circles.join_circle(conn, current, circle_id)


@router.post("/circles/{circle_id}/leave", summary="退出圈子（界面上那句「删除」）")
def leave_circle(
    circle_id: int,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    """退出返回 {dissolved, successor, post_count}。

    ！return 里带上「圈子解散了没有」是刻意的：圈主是最后一个成员时
      退圈等于把圈子解散掉，前端必须能把这件事说清楚 ——
      不然用户会以为自己的帖子还在圈子里，其实已经一起删了。
    """
    return circles.leave_circle(conn, current, circle_id)
