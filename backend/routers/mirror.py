"""模组镜像的 HTTP 出入口。

用户侧（登录即可）：浏览模组索引、看某个模组支持哪些版本、下载**已经镜像到本站**的文件。
管理侧（受 mirror_sync / mirror_download 分别控制）：触发同步、决定镜像哪些、触发下载、看进度。

！用户侧只暴露已经下到本地的文件，其余版本给一个 Modrinth 外链。
  直接把 download_url 吐给前端等于让本站变成一个「帮你绕过网络限制」的跳板，
  而且那个链接指向 Modrinth 的 CDN，随时可能变。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import admins, mirror, modrinth
from ..database import get_db
from ..deps import get_current_user
from .admin import get_admin, require_area

router = APIRouter(prefix="/api/mirror", tags=["模组镜像"])
admin_router = APIRouter(prefix="/api/admin/mirror", tags=["管理后台 · 模组镜像"])

MODRINTH_PROJECT_URL = "https://modrinth.com/mod/"


def require_any_mirror(need: str = "view"):
    """镜像页签的入口。

    ！这里刻意用「同步或下载任意一个有权限就放行」：两个权限是分开授予的，
      只给下载权的人也应该能打开这个页签。具体每个动作再分别卡
      require_area("mirror_sync"/"mirror_download")。
    """

    def dependency(current: dict = Depends(get_admin)) -> dict:
        for area in ("mirror_sync", "mirror_download"):
            if admins.can(current["admin"], current["perms"], area, need):
                return current
        raise HTTPException(
            status_code=403,
            detail="你没有查看「模组镜像」的权限（需要同步或下载其中至少一项）",
        )

    return dependency


def _capabilities(current: dict) -> dict[str, bool]:
    """这个管理员具体能做什么，前端据此决定按钮画不画。

    ！前端藏按钮只是少让人踩一次 403，真正拦人的是每个接口上的 require_area。
    """
    perms = current["perms"]
    admin = current["admin"]
    return {
        "sync": admins.can(admin, perms, "mirror_sync", "manage"),
        "download": admins.can(admin, perms, "mirror_download", "manage"),
    }


def _brief(item: dict) -> dict:
    """列表用的精简字段。版本清单只给详情接口，不然列表会大得没必要。"""
    return {
        "id": item["id"],
        "slug": item["slug"],
        "title": item["title"],
        "description": item["description"],
        # ！给的是本站的图标地址，不是 Modrinth 的 CDN 链接：
        #   CSP 是 img-src 'self'，外链图会被浏览器直接拦掉。
        "icon": f"/api/mirror/icons/{item['id']}" if item.get("local_icon") else "",
        "author": item["author"],
        "downloads": item["downloads"],
        "follows": item["follows"],
        "categories": item["categories"],
        "loaders": item["loaders"],
        "game_versions": item["game_versions"],
        "mirror_url": f"{MODRINTH_PROJECT_URL}{item['slug'] or item['id']}",
        "synced_at": item["synced_at"],
        "selected": item["selected"],
        "local": item["local"],
        "bytes": item["bytes"],
        "bytes_text": mirror.human_size(item["bytes"]),
    }


# ---------------------------------------------------------------- 用户侧

@router.get("/icons/{mod_id}", summary="模组图标")
def mod_icon(
    mod_id: str,
    conn: sqlite3.Connection = Depends(get_db),
):
    """图标缓存。

    ！刻意不要求登录：<img> 标签发不出 Authorization 头，
      要鉴权的话前端就得用 fetch + blob 去取图，图省事反而变麻烦。
      模组图标也不是敏感信息，和 /api/media/ 一个性质。
    ！也刻意不做「本地没有就去远程拉」：那样这个接口会变成一个
      可以被匿名触发的对外请求入口。图标只在同步时抓，见 mirror._cache_icon。
    """
    row = conn.execute(
        "SELECT local_icon FROM mirror_mods WHERE id = ?", (mod_id,)
    ).fetchone()
    name = row["local_icon"] if row is not None else ""
    if not name:
        raise HTTPException(status_code=404, detail="这个模组没有缓存图标")

    path = mirror.icon_path(name)
    if not path.exists():
        raise HTTPException(status_code=410, detail="图标缓存已丢失")

    return FileResponse(
        path,
        media_type=mirror.icon_mime(name),
        headers={"Cache-Control": "public, max-age=2592000, immutable"},
    )


@router.get("/overview", summary="镜像总览")
def overview(
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    data = mirror.overview(conn)
    # 用户不需要知道「谁在什么时候触发了同步」，那部分留给后台
    data.pop("latest_job", None)
    data["source"] = "Modrinth"
    return data


@router.get("/mods", summary="模组索引列表")
def list_mods(
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    items = mirror.list_mods(conn)
    return {
        "items": [_brief(item) for item in items],
        "synced": bool(items),
    }


@router.get("/mods/{mod_id}", summary="模组详情与版本清单")
def mod_detail(
    mod_id: str,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    detail = mirror.public_mod(conn, mod_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="没有这个模组")
    detail["mirror_url"] = f"{MODRINTH_PROJECT_URL}{detail['slug'] or detail['id']}"
    detail["icon"] = f"/api/mirror/icons/{detail['id']}" if detail.get("local_icon") else ""
    return detail


@router.get("/files/{file_id}/download", summary="下载已镜像的文件")
def download_file(
    file_id: str,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    row = conn.execute("SELECT * FROM mirror_files WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="没有这个文件")
    item = dict(row)

    if not item["local_name"]:
        raise HTTPException(
            status_code=409,
            detail="这个版本本站还没有镜像，可以点「去 Modrinth 下载」自己取",
        )

    # ！用写入时的同一个函数重新算路径，而不是拼 local_name ——
    #   两边用同一套规则才能保证「写进去的」和「读出来的」是同一个文件。
    path = mirror.file_path(item["mod_id"], item["file_name"])
    if not path.exists():
        raise HTTPException(
            status_code=410, detail="记录还在，但磁盘上的文件已经不在了（可能被手动清理过）"
        )

    return FileResponse(
        path,
        filename=item["file_name"] or path.name,
        media_type="application/java-archive",
    )


# ---------------------------------------------------------------- 管理侧

class SyncIn(BaseModel):
    # 留空 = 用后台设置里的「同步前几名」。
    # ！让它是可选的，是为了让「同步索引」按钮和「每日自动同步」
    #   默认干的是同一件事 —— 否则管理员设了「前 5 名」，
    #   手点一下却拉回 20 个，看起来就像设置没生效。
    limit: int | None = Field(default=None, ge=1, le=modrinth.MAX_SYNC_MODS)


class AutoSyncIn(BaseModel):
    """每日自动同步的设置。只传想改的字段，其余保持不动。"""

    enabled: bool | None = None
    hour: int | None = Field(default=None, ge=0, le=23)
    limit: int | None = Field(default=None, ge=mirror.SYNC_LIMIT_MIN,
                              le=mirror.SYNC_LIMIT_MAX)


class ModOptionsIn(BaseModel):
    mirrored: bool | None = None
    loaders: list[str] | None = Field(default=None, max_length=16)
    versions: list[str] | None = Field(default=None, max_length=64)


class DownloadIn(BaseModel):
    # 留空 = 下载所有待下载的；填了 = 只下载这一个模组的
    mod_id: str = Field(default="", max_length=64)


@admin_router.get("/state", summary="镜像后台状态")
def admin_state(
    current: dict = Depends(require_any_mirror("view")),
    conn: sqlite3.Connection = Depends(get_db),
):
    return {
        "overview": mirror.overview(conn),
        "mods": mirror.list_mods(conn),
        "jobs": mirror.jobs(conn),
        "can": _capabilities(current),
        "auto": mirror.auto_settings(conn),
        "limits": {
            "max_selected_per_mod": mirror.MAX_SELECTED_PER_MOD,
            "max_sync_mods": modrinth.MAX_SYNC_MODS,
            "max_file_bytes": modrinth.MAX_FILE_BYTES,
            "sync_limit_min": mirror.SYNC_LIMIT_MIN,
            "sync_limit_max": mirror.SYNC_LIMIT_MAX,
        },
        "dir": str(mirror.MIRROR_DIR),
    }


def _auto_summary(settings: dict) -> str:
    """把设置讲成一句人话。自动开关这种「开了到底什么时候跑」的东西，
    光看一个勾选框很难确认自己设对了没有。"""
    if not settings["enabled"]:
        return "已关闭每日自动同步"
    return (f"已开启：每天 {settings['hour']:02d}:00 自动同步下载量前 "
            f"{settings['limit']} 名的模组")


@admin_router.patch("/settings", summary="设置每日自动同步")
def admin_save_settings(
    payload: AutoSyncIn,
    current: dict = Depends(require_area("mirror_sync", "manage")),
    conn: sqlite3.Connection = Depends(get_db),
):
    """设置「要不要每天自动同步」以及「同步下载量前几名」。

    ！只要求 mirror_sync（同步索引）权限，不要求 mirror_download：
      这两项设置管的就是「拉一份索引」这件事本身，而那个权限分得很清楚 ——
      往里下文件才是 mirror_download 要拦的。

    ！同步时间用「本地钟点的 0-23」而不是 cron 表达式：
      管理员是学生和老师，不是运维。而且这个值只对比小时，
      不需要分钟级精度（每分钟醒一次的线程本来也做得到）。
    """
    if payload.enabled is None and payload.hour is None and payload.limit is None:
        raise HTTPException(status_code=422, detail="没有要修改的内容")

    settings = mirror.save_auto_settings(
        conn, enabled=payload.enabled, hour=payload.hour, limit=payload.limit
    )
    return {"ok": True, "auto": settings, "message": _auto_summary(settings)}


@admin_router.post("/sync", summary="从 Modrinth 同步索引")
def admin_sync(
    payload: SyncIn,
    current: dict = Depends(require_area("mirror_sync", "manage")),
    conn: sqlite3.Connection = Depends(get_db),
):
    limit = payload.limit or mirror.auto_settings(conn)["limit"]
    try:
        job_id = mirror.start_job(conn, "sync", operator=current["username"], limit=limit)
    except mirror.MirrorError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "ok": True,
        "job_id": job_id,
        "message": f"同步已开始（取下载量前 {limit} 名），稍等片刻看进度",
    }


@admin_router.patch("/mods/{mod_id}", summary="设置某个模组要不要镜像")
def admin_mod_options(
    mod_id: str,
    payload: ModOptionsIn,
    current: dict = Depends(require_area("mirror_download", "manage")),
    conn: sqlite3.Connection = Depends(get_db),
):
    try:
        detail = mirror.set_mod_options(
            conn, mod_id,
            mirrored=payload.mirrored,
            loaders=payload.loaders,
            versions=payload.versions,
        )
    except mirror.MirrorError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    selected = sum(1 for v in detail.get("versions", []) if v["selected"])
    return {"ok": True, "mod": detail, "selected": selected,
            "message": f"已更新，当前选中 {selected} 个文件待镜像"}


@admin_router.post("/download", summary="开始下载选中的文件")
def admin_download(
    payload: DownloadIn,
    current: dict = Depends(require_area("mirror_download", "manage")),
    conn: sqlite3.Connection = Depends(get_db),
):
    scope = payload.mod_id.strip()
    if scope and conn.execute(
        "SELECT 1 FROM mirror_mods WHERE id = ?", (scope,)
    ).fetchone() is None:
        raise HTTPException(status_code=404, detail="没有这个模组")

    try:
        job_id = mirror.start_job(conn, "download", scope=scope,
                                  operator=current["username"])
    except mirror.MirrorError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "job_id": job_id, "message": "下载已开始，可以去做别的事"}


@admin_router.get("/jobs/{job_id}", summary="查询任务进度")
def admin_job(
    job_id: int,
    current: dict = Depends(require_any_mirror("view")),
    conn: sqlite3.Connection = Depends(get_db),
):
    data = mirror.job(conn, job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="没有这个任务")
    return data


@admin_router.get("/jobs", summary="最近的任务")
def admin_jobs(
    limit: int = Query(default=12, ge=1, le=50),
    current: dict = Depends(require_any_mirror("view")),
    conn: sqlite3.Connection = Depends(get_db),
):
    return {"items": mirror.jobs(conn, limit)}
