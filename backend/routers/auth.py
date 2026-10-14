"""认证：注册、登录、登出、注销。

# v0.21 起：邮件整体退场

原来这里有三个接口是靠邮件跑的 —— 注册发验证码、邮箱验证码免密登录、找回密码。
现在全没了，邮箱降级成**普通资料字段**（可填可不填，不验证、不发信）。

换上来的是图片验证码（见 backend/captcha.py）：
  · 注册要过一道，防的是批量注册小号；
  · 登录要过一道，防的是撞库。

# 关于「忘记密码」

没有邮件就没有自助找回渠道了，这是取消邮件必须接受的代价。编一个「安全问题」
出来只会更糟：本站用户以未成年人为主，让他们填「我妈妈的名字」既不安全也不合适。

所以界面上保留一个「忘记密码？」按钮，点了告诉用户去找管理员，
再由管理员在后台点一下重置（见 routers/admin.py 的 reset_user_password）。
那条路径是**当面确认身份**，比任何自助流程都可靠。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi import Header

from .. import age
from .. import captcha
from ..achievements import sync_achievements
from ..database import get_db, now_ts
from ..deps import get_current_user, resolve_token
from ..matching import to_public
from ..schemas import LoginIn, PasswordChangeIn, RegisterIn, SessionKeepIn
from ..security import (
    DUMMY_HASH,
    SESSION_TTL_SECONDS,
    SHORT_SESSION_TTL_SECONDS,
    hash_password,
    new_session_token,
    pick_avatar_hue,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["认证"])


def _login_signals(now: datetime) -> dict[str, int]:
    """登录那一刻才知道的几个信号，通过 extra 传给成就判定。

    ！这些值不落库，所以只能在登录这个时机判一次。
      代价是：如果以后给某条这类成就改成可重复刷的，就必须先把它落库 ——
      光靠 extra 是数不出累计次数的。
    """
    hour = now.hour
    return {
        "night_login": 1 if (hour >= 22 or hour < 5) else 0,
        "early_login": 1 if 5 <= hour < 8 else 0,
        "weekend_login": 1 if now.weekday() >= 5 else 0,
    }


def _touch_login_day(conn: sqlite3.Connection, user_id: int, today: str) -> None:
    """维护「连续登录天数」与「累计登录天数」。

    ！不能拿 sessions 表去数：登出、过期清理、封禁都会删它，
      那张表记录的是「现在活着的令牌」，不是「这个人来过哪些天」。
      所以在 users 上老老实实存三个字段（见 database.py 的建表注释）。

    ！日期用**本地时区**的日期，不用 UTC：
      学生的「今天」是他所在时区的今天。用 UTC 的话，晚上八点以后
      登录就已经算第二天了，连续记录会在莫名其妙的时刻断开。
    """
    row = conn.execute(
        "SELECT last_login_day, login_streak, login_days FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        return

    last = str(row["last_login_day"] or "")
    streak = int(row["login_streak"] or 0)
    days = int(row["login_days"] or 0)

    # 同一天登录多次只算一次，否则刷新几次页面就能刷出「连续 7 天」
    if last == today:
        return

    yesterday = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    # 断档就从 1 重新数 —— 这里故意不设任何「补签」机制，
    # 连续记录断掉本来就是它的意义所在。
    streak = streak + 1 if last == yesterday else 1
    conn.execute(
        "UPDATE users SET last_login_day = ?, login_streak = ?, login_days = ? WHERE id = ?",
        (today, streak, days + 1, user_id),
    )
    conn.commit()


def _start_session(conn: sqlite3.Connection, user_id: int, keep: bool = False) -> dict:
    """发令牌 + 记录活跃时间 + 结算成就，登录类接口共用。

    keep 是前端在登录表单里问出来的（v0.30）——「下次还要不要重新输密码」。
    它只影响令牌活多久，不改变别的任何逻辑。
    """
    now = datetime.now()
    conn.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now_ts(), user_id))
    conn.commit()
    _touch_login_day(conn, user_id, now.strftime("%Y-%m-%d"))
    token = issue_token(conn, user_id, keep)
    unlocked = sync_achievements(conn, user_id, extra=_login_signals(now))
    return {
        "token": token,
        "user": to_public(fetch_user(conn, user_id)),
        "unlocked": unlocked,
    }


def guard_captcha(conn: sqlite3.Connection, payload, purpose: str) -> None:
    """人机验证不通过就抛 4xx。返回 None 代表这张图已经消费掉了。

    ！这一步必须排在**查数据库之前**，而且注册 / 登录 / 管理员登录三处都要用。
      两个理由：
      ① 顺序：如果先查账号再验验证码，「账号存不存在」就会从响应时间上泄出来 ——
         账号存在时要多跑一次 PBKDF2（几十毫秒），不存在时几乎立刻返回。
         先卡验证码，两种情况都在同一步被挡下，这个时间差就没了。
      ② 复用：写成一份而不是抄三份，否则迟早有一处忘了卡。
    """
    try:
        captcha.consume(conn, payload.captcha_id, payload.captcha_answer, purpose)
    except captcha.CaptchaError as exc:
        # 用 400 而不是 401：这是「表单没填对」，前端会去刷新那张图让用户重填，
        # 不该被当成「登录失败」把已经输好的内容清掉。
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def issue_token(conn: sqlite3.Connection, user_id: int, keep: bool = False) -> str:
    """发一枚令牌。keep 决定它活多久，两个 TTL 的差别见 backend/security.py。"""
    token = new_session_token()
    stamp = now_ts()
    ttl = SESSION_TTL_SECONDS if keep else SHORT_SESSION_TTL_SECONDS
    # 顺手清掉已经过期的令牌。
    # 之前只靠查询时的 expires_at > ? 过滤，过期的行却一直留在表里，
    # 每登录一次就多一行，跑久了这张表只增不减。
    conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (stamp,))
    conn.execute(
        "INSERT INTO sessions(token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, user_id, stamp, stamp + ttl),
    )
    conn.commit()
    return token


def fetch_user(conn: sqlite3.Connection, user_id: int) -> dict:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return dict(row)


@router.post("/register", summary="注册新账号（需过人机验证）")
def register(
    payload: RegisterIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
):
    guard_captcha(conn, payload, "register")

    if conn.execute(
        "SELECT 1 FROM users WHERE username = ?", (payload.username,)
    ).fetchone() is not None:
        raise HTTPException(status_code=409, detail="这个用户名已经有人用啦，换一个吧")

    # 邮箱现在是选填的资料字段，但仍然保持唯一：
    # 它是「别人怎么找到你」的入口，两个人共用一个地址会让后面所有事都说不清。
    if payload.email and conn.execute(
        "SELECT 1 FROM users WHERE email = ?", (payload.email,)
    ).fetchone() is not None:
        raise HTTPException(status_code=409, detail="这个邮箱已经被别的账号填过了")

    stamp = now_ts()
    # 用户填的是「几岁 + 几月几号」，出生年份由这里推出来（见 backend/age.py）。
    # ！只存年份、不存用户填的那个数字：年龄每年会变，存下来就得有人去改。
    birth_year = age.birth_year_from(payload.age, payload.birth_month, payload.birth_day)
    cursor = conn.execute(
        """
        INSERT INTO users(
            username, nickname, password_hash, email,
            avatar_hue, bio, edition,
            birth_year, birth_month, birth_day,
            region, play_styles, online_time, looking_for,
            contact_qq, contact_wechat, contact_other, contact_note,
            created_at, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.username,
            payload.nickname,
            hash_password(payload.password),
            payload.email,
            pick_avatar_hue(payload.username),
            payload.bio,
            payload.edition,
            birth_year,
            payload.birth_month,
            payload.birth_day,
            payload.region,
            json.dumps(payload.play_styles, ensure_ascii=False),
            json.dumps(payload.online_time, ensure_ascii=False),
            json.dumps(payload.looking_for, ensure_ascii=False),
            # ！这四个字段 v0.22 才补进来。RegisterIn 从一开头就接收并校验它们，
            #   而这条 INSERT 一直没写 —— 传进来的联系方式被静默丢掉。
            #   当时看不出来是因为注册表单没做这几个输入框，
            #   但只要有人照着接口文档调一次，数据就没了。
            #   「模型收下、SQL 忘存」这种不一致最难发现：两边都不会报错。
            payload.contact_qq,
            payload.contact_wechat,
            payload.contact_other,
            payload.contact_note,
            stamp,
            stamp,
        ),
    )
    user_id = int(cursor.lastrowid or 0)
    conn.commit()

    return _start_session(conn, user_id, payload.keep)


@router.post("/login", summary="密码登录（用户名或邮箱，需过人机验证）")
def login(
    payload: LoginIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
):
    guard_captcha(conn, payload, "login")

    account = payload.account.strip()
    row = conn.execute(
        "SELECT * FROM users WHERE username = ? OR (email != '' AND email = ?)",
        (account, account.lower()),
    ).fetchone()

    # ！账号不存在时也要跑一次同样开销的口令校验，不做短路。
    #   否则「账号不存在」会在 1 毫秒内返回，而「口令错误」要等 PBKDF2 跑完
    #   20 万次迭代（几十毫秒）—— 这个时间差足够把站里的用户名一个个试出来。
    #   管理员的登录接口（backend/routers/admin.py）也是同样的处理。
    stored = row["password_hash"] if row is not None else DUMMY_HASH
    matched = verify_password(payload.password or "", stored)

    if row is None or not matched:
        raise HTTPException(status_code=401, detail="用户名 / 邮箱或密码不正确")
    if row["status"] != "active":
        raise HTTPException(status_code=403, detail="该账号已被暂停使用，请联系管理员")
    return _start_session(conn, int(row["id"]), payload.keep)


@router.post("/password", summary="修改自己的密码")
def change_password(
    payload: PasswordChangeIn,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    """登录状态下改密码：验旧密码 → 写新哈希 → 踢掉所有旧会话。

    ！改完必须清 sessions，而且**包括当前这一条**。
      只踢别人、给自己留一条，是很多地方会写错的一步：
      密码之所以要改，通常正是因为怀疑泄露了，那么「还留着一个用旧密码换来的令牌」
      就完全没有意义了。代价是改完要重新登录一次，这个代价很值。
    """
    if not verify_password(payload.old_password or "", current["password_hash"]):
        raise HTTPException(status_code=400, detail="当前密码不正确")
    if payload.new_password == payload.old_password:
        raise HTTPException(status_code=400, detail="新密码不能和当前密码一样")

    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (hash_password(payload.new_password), current["id"]),
    )
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (current["id"],))
    conn.commit()
    return {"ok": True, "message": "密码已修改，请用新密码重新登录"}


@router.post("/logout", summary="退出登录")
def logout(
    response: Response,
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (current["id"],))
    conn.commit()
    response.status_code = 200
    return {"ok": True}


@router.post("/session/keep", summary="改当前会话的有效期（「保持登录」开关）")
def set_session_keep(
    payload: SessionKeepIn,
    authorization: str | None = Header(default=None),
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    """「我的 → 账号 → 在这台设备上保持登录」那个开关调它。

    ！为什么非要单独一个接口：令牌能活多久是在**发令牌那一刻**定死的。
      开关如果只改前端把它存在哪（localStorage 还是 sessionStorage），
      服务端那份 12 小时的限制还在 —— 用户开了「保持登录」，
      12 小时后又被打回登录页，看起来就是开关坏了。

    ！只动当前这一条 session，不动这个人的其它设备：
      「这台设备保持登录」是设备级的意愿，没道理顺手把别的设备也延长。
    """
    token = resolve_token(authorization)
    ttl = SESSION_TTL_SECONDS if payload.keep else SHORT_SESSION_TTL_SECONDS
    conn.execute(
        "UPDATE sessions SET expires_at = ? WHERE token = ?",
        (now_ts() + ttl, token or ""),
    )
    conn.commit()
    return {"ok": True, "keep": payload.keep}


@router.delete("/account", summary="注销并删除账号（含全部关联数据）")
def delete_account(
    current: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    conn.execute("DELETE FROM users WHERE id = ?", (current["id"],))
    conn.commit()
    return {"ok": True, "message": "账号与相关数据已彻底删除"}
