"""帖子相关的 HTTP 接口。

分工：这个文件只管「收参数、调 backend/posts.py、把结果原样返回」，
所有规则（什么算合法、谁该收到通知、图片能不能用）都在 backend/posts.py 里。
**不要在这里写业务判断** —— 后台批处理和脚本不经过 HTTP，
一旦规则写在这个文件里，它们就绕过了校验。

错误处理：posts.PostError 由 main.py 注册的全局异常处理器统一转成
HTTP 响应，状态码跟着 exc.status 走。所以这里**看不到 try/except** ——
那是刻意的，一个路由里混几层错误转换比不写还难读。

路由声明顺序：静态路径（/media、/users、/mine）必须排在
`/{post_id}` 前面。FastAPI 是按声明顺序匹配的，倒过来的话
`/api/posts/mine` 会先撞上 `/{post_id}` 然后因为不是整数而报 422。
"""

from __future__ import annotations

import sqlite3
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from .. import media, posts
from ..database import get_db, now_ts
from ..deps import get_current_user
from ..schemas import NotificationReadIn, PostCommentIn, PostIn, PostShareIn

router = APIRouter(prefix="/api", tags=["帖子"])

# 一个用户最多能攒多少张「传了但还没发出去」的图。
# ！这不是防刷，是防「打开编辑器随手传一堆，然后关掉页面不管」——
#   那些图 24 小时后会被清理，但在这 24 小时里它们一直占着磁盘。
#   40 张足够正常人反复修改一篇图文帖了。
MAX_PENDING_MEDIA = 40


async def _read_body(request: Request, limit: int) -> bytes:
    """读原始请求体。先看 Content-Length 提前拒绝，避免白读一个大文件。"""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="这张图太大了，请换一张小一点的。")
    data = await request.body()
    if len(data) > limit:
        raise HTTPException(status_code=413, detail="这张图太大了，请换一张小一点的。")
    return data


# ================================================================ 配图上传
#
# ！和头像 / 皮肤走同一套管线：原始 PNG 请求体，不搞 multipart 表单。
#   这样 requirements.txt 里仍然只有 fastapi / uvicorn 两个依赖
#   （收 multipart 需要 python-multipart，那是一个新依赖）。

@router.post("/posts/media", summary="上传一张帖子配图")
async def upload_post_image(
    request: Request,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    data = await _read_body(request, media.POST_IMAGE_MAX_BYTES)

    pending = int(conn.execute(
        "SELECT COUNT(*) FROM post_media WHERE owner_id = ? AND post_id IS NULL "
        "AND created_at > ?",
        (current["id"], now_ts() - posts.MEDIA_ORPHAN_SECONDS),
    ).fetchone()[0])
    if pending >= MAX_PENDING_MEDIA:
        raise HTTPException(
            status_code=429,
            detail="你有一批还没用上的图片，先把它们用到帖子里，或者等它们过期。",
        )

    try:
        filename, width, height = media.save_post_image(data)
    except media.MediaError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    conn.execute(
        "INSERT INTO post_media (owner_id, post_id, file_name, byte_size, created_at) "
        "VALUES (?, NULL, ?, ?, ?)",
        (current["id"], filename, len(data), now_ts()),
    )
    conn.commit()
    return {
        "file": filename,
        "url": media.url_of(filename),
        "width": width,
        "height": height,
        "max_count": media.POST_IMAGE_MAX_COUNT,
    }


# ================================================================ 附件
#
# 图片之外的文件：存档、整合包、数据包、说明文档。
#
# ！文件名走自定义请求头 X-Filename（值需要 encodeURIComponent），
#   不走 multipart —— 后者要装 python-multipart，而这是本项目
#   唯一不允许的东西。后台导入数据库用的也是同一套写法。

@router.post("/posts/attachment", summary="上传一个附件")
async def upload_attachment(
    request: Request,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    # HTTP 头只能放 ASCII，所以前端发的是 encodeURIComponent 之后的值
    display_in = unquote(request.headers.get("x-filename", ""))
    data = await _read_body(request, media.ATTACHMENT_MAX_BYTES)

    try:
        stored, display, size = media.save_attachment(data, display_in)
    except media.MediaError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    conn.execute(
        "INSERT INTO post_media (owner_id, post_id, file_name, byte_size, created_at, "
        "display_name) VALUES (?, NULL, ?, ?, ?, ?)",
        (current["id"], stored, size, now_ts(), display),
    )
    conn.commit()
    return {
        "file": stored,
        "name": display,
        "size": size,
        "max_bytes": media.ATTACHMENT_MAX_BYTES,
    }


@router.get("/posts/attachment/{filename}", summary="下载附件")
def download_attachment(
    filename: str,
    conn: sqlite3.Connection = Depends(get_db),
):
    """！这条【不要求登录】，是故意的。

    浏览器打开一个 <a href> 时不会带上 Authorization 头。要让它必须登录，
    就只能改成前端 fetch + blob 下载 —— 那样就没法右键另存、
    也没法在新标签页里打开、更没法分享给同学。

    代价是「知道链接就能下载」。文件名是 128 位随机串，猜不出来，
    也不可枚举 —— 和头像图片（/api/media/）是同一个安全模型。
    """
    path = media.attachment_path(filename)
    if path is None:
        raise HTTPException(status_code=404, detail="这个附件不存在或已经被删除了")

    row = conn.execute(
        "SELECT display_name FROM post_media WHERE file_name = ?", (filename,)
    ).fetchone()
    display = media.safe_display_name(row["display_name"] if row else "") 

    return FileResponse(
        path,
        # ！统一声明 application/octet-stream，绝不按扩展名去猜。
        #   猜成 text/html 或 image/svg+xml 的话，浏览器会把它当网页
        #   在当前域下渲染 —— 那就成了存储型 XSS。附件是给人「下」的，
        #   不是给人「看」的。
        media_type="application/octet-stream",
        filename=display,
        headers={
            "X-Content-Type-Options": "nosniff",
            # 附件内容不变，但文件名可能被改（改了会换一行记录），
            # 所以只给一个短缓存
            "Cache-Control": "public, max-age=3600",
        },
    )


# ================================================================ 可添加的人

@router.get("/posts/users", summary="搜索可以添加为制作人员 / 转发对象的用户")
def search_users(
    q: str = Query(default="", max_length=40),
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    return {"items": posts.search_users(conn, current, q)}


# ================================================================ 我发布的

@router.get("/posts/mine", summary="我发布的帖子（含已下架 / 已删除）")
def my_posts(
    q: str = Query(default="", max_length=40),
    page: int = Query(default=1, ge=1),
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    """！这个接口和 /posts 的差别不只是「多一个过滤条件」：
    它【不过滤状态】—— 自己发的、被管理员下架的帖子必须能看到，
    否则作者会发现自己发过的东西凭空消失了，而且哪里都找不到。
    """
    return posts.feed(
        conn, current["id"], author_id=int(current["id"]), keyword=q, page=page
    )


# ================================================================ 列表与增删改

@router.get("/posts", summary="帖子列表（按发布时间倒序）")
def list_posts(
    q: str = Query(default="", max_length=40),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=posts.FEED_PAGE, ge=1, le=50),
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    return posts.feed(conn, current["id"], keyword=q, page=page, limit=limit)


@router.post("/posts", summary="发布帖子")
def create_post(
    payload: PostIn,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    return posts.create_post(
        conn, current,
        title=payload.title,
        blocks=payload.blocks,
        credits=payload.credits,
    )


@router.get("/posts/{post_id}", summary="帖子详情")
def read_post(
    post_id: int,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    return posts.read_post(conn, current["id"], post_id)


@router.patch("/posts/{post_id}", summary="编辑帖子")
def update_post(
    post_id: int,
    payload: PostIn,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    return posts.update_post(
        conn, current, post_id,
        title=payload.title,
        blocks=payload.blocks,
        credits=payload.credits,
    )


@router.delete("/posts/{post_id}", summary="删除帖子")
def delete_post(
    post_id: int,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    posts.delete_post(conn, current, post_id)
    return {"ok": True}


# ================================================================ 点赞 / 评论 / 转发

@router.post("/posts/{post_id}/like", summary="点赞或取消点赞")
def like_post(
    post_id: int,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    return posts.toggle_like(conn, current, post_id)


@router.get("/posts/{post_id}/comments", summary="评论列表")
def list_comments(
    post_id: int,
    page: int = Query(default=1, ge=1),
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    return posts.list_comments(conn, current["id"], post_id, page=page)


@router.post("/posts/{post_id}/comments", summary="发表评论")
def add_comment(
    post_id: int,
    payload: PostCommentIn,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    return posts.add_comment(conn, current, post_id, payload.content)


@router.delete("/posts/{post_id}/comments/{comment_id}", summary="删除评论")
def delete_comment(
    post_id: int,
    comment_id: int,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    posts.delete_comment(conn, current, post_id, comment_id)
    return {"ok": True}


@router.get("/posts/{post_id}/share-targets", summary="可以转发给谁")
def share_targets(
    post_id: int,
    q: str = Query(default="", max_length=40),
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    # 先确认这篇帖子对当前用户是可见的，避免拿它当探针去猜帖子存不存在
    posts.get_row(conn, post_id)
    return {"items": posts.share_recipients(conn, current, q)}


@router.post("/posts/{post_id}/share", summary="把帖子转发给站内用户")
def share_post(
    post_id: int,
    payload: PostShareIn,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    return posts.share(conn, current, post_id, payload.user_ids, payload.note)


# ================================================================ 信息中心

@router.get("/notifications", summary="信息中心列表")
def list_notifications(
    box: str = Query(default="all", pattern="^(all|unread)$"),
    page: int = Query(default=1, ge=1),
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    return posts.inbox(conn, int(current["id"]), box=box, page=page)


@router.post("/notifications/read", summary="标记已读")
def read_notifications(
    payload: NotificationReadIn,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    changed = posts.mark_read(
        conn, int(current["id"]),
        ids=payload.ids,
        all_unread=bool(payload.all),
    )
    return {"changed": changed, "unread": posts.unread_count(conn, int(current["id"]))}
