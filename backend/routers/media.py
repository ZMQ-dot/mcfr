"""皮肤与头像：上传、移除，以及对外提供图片文件。

上传走的是「原始请求体」而不是 multipart 表单 —— 这样就不需要
python-multipart 这个额外依赖，前端直接把 canvas 生成的 Blob 当作
body 发过来即可（Content-Type: image/png）。

三个接口的分工：
    POST   /api/users/me/avatar   上传头像（浏览器已缩放成 256×256 PNG）
    POST   /api/users/me/skin     上传 Minecraft 皮肤（64×64 或 64×32）
    GET    /api/media/{filename}  读取图片（只认服务端自己生成的文件名）
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from .. import media
from ..database import get_db
from ..deps import get_current_user
from ..matching import to_public

router = APIRouter(tags=["皮肤与头像"])

# 上传的图片永久有效（文件名带随机串，内容不会变），可以放心长缓存
MEDIA_CACHE_HEADERS = {"Cache-Control": "public, max-age=2592000, immutable"}


async def _read_png(request: Request, limit: int) -> bytes:
    """读取上传内容。先看 Content-Length 提前拒绝，避免白读一个大文件。"""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="文件太大了，请换一张小一点的图片。")

    data = await request.body()
    if len(data) > limit:
        raise HTTPException(status_code=413, detail="文件太大了，请换一张小一点的图片。")
    return data


def _fresh_user(conn: sqlite3.Connection, user_id: int) -> dict:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return to_public(dict(row))


# ---------------------------------------------------------------- 头像

@router.post("/api/users/me/avatar", summary="上传自定义头像")
async def upload_avatar(
    request: Request,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    data = await _read_png(request, media.AVATAR_MAX_BYTES)
    try:
        filename = media.save_avatar(data)
    except media.MediaError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # 换新头像时把旧文件删掉，避免 data/uploads 越堆越多
    media.remove(current.get("avatar_file") or "")
    conn.execute("UPDATE users SET avatar_file = ? WHERE id = ?", (filename, current["id"]))
    conn.commit()
    return {"user": _fresh_user(conn, int(current["id"]))}


@router.delete("/api/users/me/avatar", summary="移除自定义头像")
def delete_avatar(
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    media.remove(current.get("avatar_file") or "")
    conn.execute("UPDATE users SET avatar_file = '' WHERE id = ?", (current["id"],))
    conn.commit()
    return {"user": _fresh_user(conn, int(current["id"]))}


# ---------------------------------------------------------------- 皮肤

@router.post("/api/users/me/skin", summary="上传 Minecraft 皮肤")
async def upload_skin(
    request: Request,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    data = await _read_png(request, media.SKIN_MAX_BYTES)
    try:
        filename = media.save_skin(data)
    except media.MediaError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    media.remove(current.get("skin_file") or "")
    # 手动传了皮肤 = 不想再用微软账号里那张，把导入的那份让出来
    # （只清列，不删文件 —— 用户随时可以重新走一次微软登录拿回来）
    conn.execute(
        "UPDATE users SET skin_file = ?, mc_skin_file = '' WHERE id = ?",
        (filename, current["id"]),
    )
    conn.commit()
    return {"user": _fresh_user(conn, int(current["id"]))}


@router.delete("/api/users/me/skin", summary="移除皮肤")
def delete_skin(
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    media.remove(current.get("skin_file") or "")
    conn.execute("UPDATE users SET skin_file = '' WHERE id = ?", (current["id"],))
    conn.commit()
    return {"user": _fresh_user(conn, int(current["id"]))}


# ---------------------------------------------------------------- 图片文件

@router.get("/api/media/{filename}", summary="读取上传的图片", include_in_schema=False)
def read_media(filename: str):
    path = media.path_of(filename)
    if path is None:
        raise HTTPException(status_code=404, detail="图片不存在。")
    return FileResponse(path, media_type="image/png", headers=MEDIA_CACHE_HEADERS)
