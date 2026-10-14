"""意见反馈：用户提交，管理员查看处理。

左侧那个悬浮按钮点开的表单就是这里。

几个刻意的设计：
- **允许匿名提交**（`get_optional_user`）。登录不上来的人、还没注册的人，
  恰恰是最需要反馈问题的 —— 如果要求必须先登录，最该收到的那类反馈就收不到了。
  登录了就把 user_id 一起记上，方便管理员回看上下文。
- **限流**：同一 IP 每小时 5 条、每天 20 条。没有限流的公开表单等于给别人
  一个往你库里灌垃圾的入口。登录用户额外按账号再限一次。
- **不做内容过滤**。反馈里经常需要贴出错信息、联系方式、截图里的文字，
  按聊天内容那套规则去拦会误伤。这里只做长度限制与空白清理，
  真正的内容判断交给管理员人工看（后台有「删除」）。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..client_ip import client_ip as _client_ip
from ..database import get_db, now_ts
from ..deps import get_optional_user

router = APIRouter(prefix="/api/feedback", tags=["意见反馈"])

KINDS: tuple[tuple[str, str], ...] = (
    ("suggestion", "功能建议"),
    ("bug", "问题反馈"),
    ("content", "内容举报"),
    ("safety", "安全 / 隐私"),
    ("other", "其它"),
)
KIND_VALUES = tuple(key for key, _ in KINDS)

MIN_CONTENT = 5
MAX_CONTENT = 1000
MAX_CONTACT = 80
MAX_PAGE = 120

HOURLY_PER_IP = 5
DAILY_PER_IP = 20
HOURLY_PER_USER = 5


class FeedbackIn(BaseModel):
    kind: str = "suggestion"
    content: str
    contact: str = Field(default="", max_length=MAX_CONTACT)
    page: str = Field(default="", max_length=MAX_PAGE)


def _clean(text: str, limit: int) -> str:
    """去掉控制字符、压缩多余空行，再按长度截断。

    ！必须清控制字符：反馈会被原样渲染到管理后台，
      换行符、制表符还好，`\\x00` 之类的字符进 SQLite 会直接报错。
    """
    value = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    value = "".join(ch for ch in value if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    # 连续 3 个以上换行压成 2 个，避免有人用空行把整页刷满
    while "\n\n\n" in value:
        value = value.replace("\n\n\n", "\n\n")
    return value.strip()[:limit]


@router.post("", summary="提交一条意见反馈")
def submit_feedback(
    payload: FeedbackIn,
    request: Request,
    current: dict | None = Depends(get_optional_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    content = _clean(payload.content, MAX_CONTENT)
    if len(content) < MIN_CONTENT:
        raise HTTPException(status_code=422, detail=f"请至少写 {MIN_CONTENT} 个字，说清楚一点我们才好处理")
    if len(payload.content or "") > MAX_CONTENT:
        raise HTTPException(status_code=422, detail=f"内容太长了，请压缩到 {MAX_CONTENT} 字以内")

    kind = (payload.kind or "suggestion").strip().lower()
    if kind not in KIND_VALUES:
        kind = "other"

    ip = _client_ip(request)
    stamp = now_ts()

    def count(sql: str, args: tuple) -> int:
        return int(conn.execute(sql, args).fetchone()[0])

    if count(
        "SELECT COUNT(*) FROM feedback WHERE sent_ip = ? AND created_at > ?",
        (ip, stamp - 3600),
    ) >= HOURLY_PER_IP:
        raise HTTPException(status_code=429, detail="一小时内提交得太频繁了，请稍后再来")

    if count(
        "SELECT COUNT(*) FROM feedback WHERE sent_ip = ? AND created_at > ?",
        (ip, stamp - 86400),
    ) >= DAILY_PER_IP:
        raise HTTPException(status_code=429, detail="今天提交得够多了，明天再来吧")

    if current and count(
        "SELECT COUNT(*) FROM feedback WHERE user_id = ? AND created_at > ?",
        (current["id"], stamp - 3600),
    ) >= HOURLY_PER_USER:
        raise HTTPException(status_code=429, detail="一小时内提交得太频繁了，请稍后再来")

    cursor = conn.execute(
        """
        INSERT INTO feedback
            (user_id, kind, content, contact, page, status, sent_ip, created_at)
        VALUES (?, ?, ?, ?, ?, 'open', ?, ?)
        """,
        (
            current["id"] if current else None,
            kind,
            content,
            _clean(payload.contact, MAX_CONTACT),
            _clean(payload.page, MAX_PAGE),
            ip,
            stamp,
        ),
    )
    conn.commit()

    who = current["username"] if current else "访客"
    print(f"[mcfr] 收到一条意见反馈（#{cursor.lastrowid}，来自 {who}）")

    return {
        "ok": True,
        "id": int(cursor.lastrowid),
        "message": "已经收到啦，谢谢你帮我们把这里变得更好！",
    }


@router.get("/mine", summary="我提交过的反馈")
def my_feedback(
    current: dict | None = Depends(get_optional_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    if current is None:
        return {"items": []}

    rows = conn.execute(
        """
        SELECT id, kind, content, status, reply, created_at, handled_at
        FROM feedback WHERE user_id = ? ORDER BY created_at DESC LIMIT 20
        """,
        (current["id"],),
    ).fetchall()

    labels = dict(KINDS)
    items = []
    for row in rows:
        item = dict(row)
        item["kind_label"] = labels.get(item["kind"], item["kind"])
        items.append(item)
    return {"items": items}
