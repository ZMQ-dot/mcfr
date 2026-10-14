"""人机验证接口：领一张图片验证码。

刻意做成**独立于 /api/auth** 的路由：管理员的登录接口（/api/admin/login）
也要用它，而那条路径根本不属于 /api/auth 的语义范围。

# 为什么返回值里直接带图片的 base64，而不是给一个图片 URL

两者都行（CSP 里 `img-src` 放开了 `data:`），但合成一个请求有两个好处：

  · 少一次往返 —— 领验证码正好在登录最紧张的那一步，能少一次就少一次；
  · 从拿到 id 到拿到图之间**不可能出现不一致**（分两个请求就可能拿到 A 的
    id 配 B 的图，然后用户怎么输都错，报错还看不出原因）。

代价是响应体大几 KB。一张 100×45 的点阵图压完 1.1~1.3 KB，base64 之后约 1.7 KB，
对领验证码这种低频操作完全可以接受。
"""

from __future__ import annotations

import base64
import sqlite3

from fastapi import APIRouter, Depends, Query, Request

from ..captcha import CODE_TTL_SECONDS, CaptchaError, issue
from ..client_ip import client_ip as _client_ip
from ..database import get_db

router = APIRouter(prefix="/api/captcha", tags=["人机验证"])

# 用途只影响记录里怎么标，防止跨场景复用凭据（注册那关的一张图不能拿去登录）
PURPOSES = ("login", "register", "admin")


@router.get("", summary="领一张图片验证码")
def new_captcha(
    request: Request,
    purpose: str = Query(default="login", pattern="^(login|register|admin)$"),
    conn: sqlite3.Connection = Depends(get_db),
):
    try:
        challenge_id, png = issue(conn, purpose, _client_ip(request))
    except CaptchaError as exc:
        # 同一个接口既发图又限流，所以这里必须让错误以 429 出去 ——
        # 前端靠状态码决定「是弹提示还是把图刷新掉」。
        from fastapi import HTTPException

        raise HTTPException(status_code=429, detail=str(exc)) from exc

    payload = base64.b64encode(png).decode("ascii")
    return {
        "id": challenge_id,
        "image": f"data:image/png;base64,{payload}",
        "expires_in": CODE_TTL_SECONDS,
    }
