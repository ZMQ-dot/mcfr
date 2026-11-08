"""启动器接口：客户端侧（公开）与管理侧（后台）。

分成两个 router 而不是一个：
  · `/api/launcher/*` 是**给桌面上那个程序**用的，它没有管理员身份，
    只能拿到自己的配置、公告和登录结果；
  · `/api/admin/launcher/*` 是给后台页签用的，走管理员令牌 + require_area。

！两条路由的鉴权模型完全不同，混在一个 router 里迟早会有人把
  「谁都能调的接口」写成「要管理员令牌」，或者反过来按错依赖。
"""

from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .. import launcher
from ..client_ip import client_ip as _client_ip
from ..database import get_db, now_ts
from .admin import require_area

# ===========================================================================
# 公开接口
# ===========================================================================

router = APIRouter(prefix="/api/launcher", tags=["启动器"])

# 极简的按 IP 限流：这张表只活在内存里，进程重启就清空。
#
# ！为什么不落库：启动器的请求全是匿名的，落库意味着任何人只要发几个请求
#   就能让一张表无限增长 —— 那正好是拒绝服务想要的效果。
#   内存计数在「重启」时会清零，但重启本身就是管理员主动做的事。
_BUCKET: dict[str, list[float]] = {}
RATE_WINDOW = 60.0
RATE_MAX = 240          # 每分钟每 IP 最多 240 次（心跳 + 事件 + 轮询，够用）


def _throttle(request: Request) -> None:
    ip = _client_ip(request) or "-"
    now = time.time()
    hits = _BUCKET.setdefault(ip, [])
    while hits and now - hits[0] > RATE_WINDOW:
        hits.pop(0)
    if len(hits) >= RATE_MAX:
        raise HTTPException(status_code=429, detail="请求太频繁，请稍后再试")
    hits.append(now)
    if len(_BUCKET) > 4000:  # 极端情况下别让这张表无限长
        _BUCKET.clear()


def _user_from_token(conn: sqlite3.Connection, token: str) -> int | None:
    value = (token or "").strip()
    if not value:
        return None
    row = conn.execute(
        "SELECT user_id FROM sessions WHERE token = ? AND expires_at > ?",
        (value, now_ts()),
    ).fetchone()
    return int(row["user_id"]) if row is not None else None


class HeartbeatIn(BaseModel):
    device_id: str
    app_version: str = Field(default="", max_length=24)
    os: str = Field(default="", max_length=24)
    arch: str = Field(default="", max_length=16)
    mc_version: str = Field(default="", max_length=32)
    loader: str = Field(default="", max_length=24)
    # 启动器里登录了 mcfr 账号才有；没登录就留空
    token: str = Field(default="", max_length=128)


class EventIn(BaseModel):
    device_id: str
    kind: str = Field(pattern="^(start|launch|exit|update)$")
    detail: str = Field(default="", max_length=200)
    seconds: int = 0
    mc_version: str = Field(default="", max_length=32)
    loader: str = Field(default="", max_length=24)


class DeviceIn(BaseModel):
    device_id: str = ""


class PollIn(BaseModel):
    flow_id: str
    device_id: str = ""


@router.get("/config", summary="启动器配置：版本策略、功能开关、公告")
def get_config(
    request: Request,
    device_id: str = Query(default="", max_length=64),
    app_version: str = Query(default="", max_length=24),
    channel: str = Query(default="", max_length=16),
    conn: sqlite3.Connection = Depends(get_db),
):
    """一次性拿到启动器需要知道的一切。

    ！这个接口允许匿名访问，且**不写库** —— 它在启动器每次开机时被调用一次，
      写库的话光靠重开程序就能把 launcher_clients 刷成热点页。
      要留存记录请用心跳接口。
    """
    _throttle(request)
    _ = device_id
    return launcher.config_payload(conn, app_version=app_version, channel=channel)


@router.post("/heartbeat", summary="启动器心跳（顺带登记设备）")
def heartbeat(
    request: Request,
    payload: HeartbeatIn,
    conn: sqlite3.Connection = Depends(get_db),
):
    _throttle(request)
    try:
        user_id = _user_from_token(conn, payload.token)
        launcher.touch_client(
            conn,
            payload.device_id,
            os_name=payload.os,
            arch=payload.arch,
            app_version=payload.app_version,
            mc_version=payload.mc_version,
            loader=payload.loader,
            user_id=user_id,
            ip=_client_ip(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    conn.commit()
    data = launcher.config_payload(conn, app_version=payload.app_version)
    data["ok"] = True
    return data


@router.post("/events", summary="上报一次使用事件（启动 / 进游戏 / 退出）")
def post_event(
    request: Request,
    payload: EventIn,
    conn: sqlite3.Connection = Depends(get_db),
):
    _throttle(request)
    try:
        launcher.record_event(
            conn,
            payload.device_id,
            payload.kind,
            payload.detail,
            seconds=payload.seconds,
            mc_version=payload.mc_version,
            loader=payload.loader,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    conn.commit()
    return {"ok": True}


@router.get("/releases", summary="最近的发布版本（更新日志）")
def list_public_releases(
    request: Request,
    channel: str = Query(default="stable", max_length=16),
    limit: int = Query(default=10, ge=1, le=30),
    conn: sqlite3.Connection = Depends(get_db),
):
    _throttle(request)
    rows = [
        row
        for row in launcher.release_rows(conn, channel)
        if int(row.get("active") or 0)
    ]
    rows.sort(key=lambda row: launcher.version_tuple(row.get("version") or ""), reverse=True)
    return {
        "items": [
            {
                "version": row["version"],
                "notes": row["notes"],
                "url": row["url"],
                "sha256": row["sha256"],
                "forced": bool(row["forced"]),
                "published_at": int(row["created_at"] or 0),
            }
            for row in rows[:limit]
        ]
    }


@router.post("/ms/device", summary="发起微软正版登录（服务器侧设备码）")
def ms_device(
    request: Request,
    payload: DeviceIn,
    conn: sqlite3.Connection = Depends(get_db),
):
    """！client_id 与租户只存在于服务器。客户端拿到的是 flow_id + 8 位用户码。"""
    _throttle(request)
    try:
        payload = launcher.start_device_flow(conn, payload.device_id, _client_ip(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # ！必须提交：device_code 就存在这一行里，不提交的话下一次轮询
    #   会拿到「会话不存在」—— 表现得就像登录莫名其妙地失败了。
    conn.commit()
    return payload


@router.post("/ms/poll", summary="查询微软登录进度")
def ms_poll(
    request: Request,
    payload: PollIn,
    conn: sqlite3.Connection = Depends(get_db),
):
    _throttle(request)
    try:
        result = launcher.poll_device_flow(conn, payload.flow_id, payload.device_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    conn.commit()
    return result


@router.post("/ms/cancel", summary="放弃本次微软登录")
def ms_cancel(
    request: Request,
    payload: PollIn,
    conn: sqlite3.Connection = Depends(get_db),
):
    _throttle(request)
    launcher.drop_flow(conn, payload.flow_id)
    conn.commit()
    return {"ok": True}


# ===========================================================================
# 管理接口
# ===========================================================================

admin_router = APIRouter(prefix="/api/admin/launcher", tags=["后台 · 启动器"])


class LauncherSettingsIn(BaseModel):
    maintenance: bool | None = None
    message: str | None = Field(default=None, max_length=2000)
    min_version: str | None = Field(default=None, max_length=32)
    channel: str | None = Field(default=None, max_length=16)
    heartbeat_seconds: int | None = None
    features: dict[str, bool] | None = None


class ReleaseIn(BaseModel):
    version: str = Field(max_length=32)
    channel: str = Field(default="stable", max_length=16)
    notes: str = Field(default="", max_length=8000)
    url: str = Field(default="", max_length=500)
    sha256: str = Field(default="", max_length=64)
    byte_size: int = 0
    forced: bool = False
    active: bool = True


class NoticeIn(BaseModel):
    title: str = Field(max_length=80)
    body: str = Field(default="", max_length=4000)
    level: str = Field(default="info", max_length=16)
    active: bool = True
    starts_at: int = 0
    ends_at: int = 0


class NoticePatchIn(BaseModel):
    title: str | None = Field(default=None, max_length=80)
    body: str | None = Field(default=None, max_length=4000)
    level: str | None = Field(default=None, max_length=16)
    active: bool | None = None
    starts_at: int | None = None
    ends_at: int | None = None


@admin_router.get("", summary="启动器总览")
def admin_overview(
    conn: sqlite3.Connection = Depends(get_db),
    current: dict = Depends(require_area("launcher", "view")),
):
    _ = current
    settings = launcher.read_settings(conn)
    stats = launcher.stats(conn, 14)
    releases = launcher.release_rows(conn)
    notices = launcher.notice_rows(conn)
    return {
        "settings": settings,
        "features": [
            {"key": key, "name": name, "desc": desc, "enabled": settings["features"][key]}
            for key, name, desc in launcher.FEATURES
        ],
        "stats": stats,
        "releases": releases,
        "notices": notices,
        # 首页只带最近 20 台设备，翻页走 /clients
        "clients": launcher.client_rows(conn, page=1, size=20),
        "ms_login_available": launcher.ms_configured(),
        "latest": launcher.latest_release(conn, settings["channel"]),
    }


@admin_router.get("/settings", summary="读取启动器设置")
def admin_get_settings(
    conn: sqlite3.Connection = Depends(get_db),
    current: dict = Depends(require_area("launcher", "view")),
):
    _ = current
    return launcher.read_settings(conn)


@admin_router.put("/settings", summary="修改启动器设置（含功能开关）")
def admin_put_settings(
    payload: LauncherSettingsIn,
    conn: sqlite3.Connection = Depends(get_db),
    current: dict = Depends(require_area("launcher", "manage")),
):
    _ = current
    data = launcher.write_settings(conn, payload.model_dump())
    conn.commit()
    return {"ok": True, "settings": data, "message": "已保存，启动器下次心跳时生效"}


@admin_router.get("/stats", summary="使用统计（按天）")
def admin_stats(
    days: int = Query(default=14, ge=1, le=90),
    conn: sqlite3.Connection = Depends(get_db),
    current: dict = Depends(require_area("launcher", "view")),
):
    _ = current
    return launcher.stats(conn, days)


@admin_router.get("/clients", summary="设备列表")
def admin_clients(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=40, ge=1, le=100),
    q: str = Query(default="", max_length=40),
    conn: sqlite3.Connection = Depends(get_db),
    current: dict = Depends(require_area("launcher", "view")),
):
    _ = current
    return launcher.client_rows(conn, page=page, size=size, q=q)


@admin_router.post("/releases", summary="发布一个版本")
def admin_add_release(
    payload: ReleaseIn,
    conn: sqlite3.Connection = Depends(get_db),
    current: dict = Depends(require_area("launcher", "manage")),
):
    _ = current
    try:
        row = launcher.add_release(conn, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    conn.commit()
    return {"ok": True, "release": row, "message": f"已发布 v{row['version']}"}


@admin_router.delete("/releases/{release_id}", summary="删除一个版本")
def admin_delete_release(
    release_id: int,
    conn: sqlite3.Connection = Depends(get_db),
    current: dict = Depends(require_area("launcher", "manage")),
):
    _ = current
    if not launcher.delete_release(conn, release_id):
        raise HTTPException(status_code=404, detail="这个版本不存在")
    conn.commit()
    return {"ok": True, "message": "已删除"}


@admin_router.post("/notices", summary="发布一条公告")
def admin_add_notice(
    payload: NoticeIn,
    conn: sqlite3.Connection = Depends(get_db),
    current: dict = Depends(require_area("launcher", "manage")),
):
    author = str((current.get("admin") or {}).get("username") or "")
    try:
        row = launcher.add_notice(conn, payload.model_dump(), author)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    conn.commit()
    return {"ok": True, "notice": row, "message": "公告已发布"}


@admin_router.patch("/notices/{notice_id}", summary="修改或上下线一条公告")
def admin_patch_notice(
    notice_id: int,
    payload: NoticePatchIn,
    conn: sqlite3.Connection = Depends(get_db),
    current: dict = Depends(require_area("launcher", "manage")),
):
    _ = current
    row = launcher.update_notice(conn, notice_id, payload.model_dump(exclude_unset=True))
    if row is None:
        raise HTTPException(status_code=404, detail="这条公告不存在")
    conn.commit()
    return {"ok": True, "notice": row, "message": "已保存"}


@admin_router.delete("/notices/{notice_id}", summary="删除一条公告")
def admin_delete_notice(
    notice_id: int,
    conn: sqlite3.Connection = Depends(get_db),
    current: dict = Depends(require_area("launcher", "manage")),
):
    _ = current
    if not launcher.delete_notice(conn, notice_id):
        raise HTTPException(status_code=404, detail="这条公告不存在")
    conn.commit()
    return {"ok": True, "message": "已删除"}


@admin_router.get("/flows", summary="未完成的微软登录会话（排障用）")
def admin_flows(
    conn: sqlite3.Connection = Depends(get_db),
    current: dict = Depends(require_area("launcher", "view")),
):
    """只给数量与状态，**不返回** device_code / 令牌。"""
    _ = current
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS c FROM launcher_ms_flows GROUP BY status
        """
    ).fetchall()
    return {"counts": {str(row["status"]): int(row["c"]) for row in rows}}


@admin_router.post("/flows/purge", summary="清空所有登录会话")
def admin_purge_flows(
    conn: sqlite3.Connection = Depends(get_db),
    current: dict = Depends(require_area("launcher", "manage")),
):
    _ = current
    cursor = conn.execute("DELETE FROM launcher_ms_flows")
    conn.commit()
    return {"ok": True, "message": f"已清空 {cursor.rowcount} 条登录会话"}
