"""管理后台：管理员认证与后台数据接口。

入口是站点根路径下的 `/admin` 页面（前端 admin.html）。

设计要点：
- 管理员令牌存放在独立的 admin_sessions 表，与普通用户的 sessions 完全隔离，
  即使某个用户的登录令牌泄露，也无法借此拿到后台权限；
- 登录失败次数受限，防止有人暴力破解；
- **账号分两级**（v0.13 起，详见 backend/admins.py）：
  超级管理员拥有一切，并能添加 / 停用 / 删除普通管理员、逐项授权；
  普通管理员固定能用「数据概览 / 举报处理 / 用户管理 / 邮件记录」四个页签，
  之外的模块（意见反馈、系统升级、管理员管理）要由超管逐项开权限；
- 后台只展示管理所必需的信息（不返回用户的联系方式等隐私字段）。
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import admins, adminkeys, age, captcha, keylogin, posts, upgrade
from ..client_ip import client_ip as _client_ip
from ..config import update_env
from ..database import get_db, now_ts
from ..matching import to_public
from ..schemas import BirthIn, CaptchaIn, PostStatusIn
from ..security import hash_password, verify_password
from .auth import guard_captcha

router = APIRouter(prefix="/api/admin", tags=["管理后台"])

# 备份 / 还原 / 导入这类操作会在几秒内反复读写整个数据库文件，
# 两个人同时点会互相踩（尤其是「还原」—— 一个在写一个在读，结果无法预测）。
# 用一把锁把这类操作串起来，正在忙的时候直接告诉后来的人「稍等」。
# ！必须用 RLock：下面几个接口内部会互相调用（导入会走还原、还原会走升级），
#   普通 Lock 在同一个线程里二次加锁会自锁死。
_maintenance_lock = threading.RLock()

ADMIN_TTL_SECONDS = 60 * 60 * 8  # 管理员登录有效期 8 小时
MAX_FAILURES = 5                 # 同一 IP 在窗口内最多失败次数
FAILURE_WINDOW = 60 * 15         # 窗口长度 15 分钟

# 一个管理员最多同时注册几把密钥（v0.32）。
# 给 8 把：电脑一把、备用机一把、U 盘一把，再留几把周转，足够了。
# 它的作用不是防攻击，是防「列一个长到没人看得完的清单」。
MAX_KEYS_PER_ADMIN = 8

# 取来源 IP 一律走 backend/client_ip.py：那里默认**不信任** X-Forwarded-For。
# ！这一条对后台尤其重要 —— 这里的失败次数限制是口令的唯一防线，
#   如果来源 IP 可以随便伪造，这个限制就等于没有。


class AdminLoginIn(CaptchaIn):
    # username 允许为空：只填密码时默认按超级管理员处理，
    # 这样从旧版本（只有一个全局密码）过来的人不会因为不填用户名而进不去。
    username: str = ""
    password: str


class AdminPasswordIn(BaseModel):
    old_password: str
    new_password: str


class UserStatusIn(BaseModel):
    status: str = Field(pattern="^(active|suspended)$")


class AdminCreateIn(BaseModel):
    username: str
    password: str
    display_name: str = Field(default="", max_length=24)
    note: str = Field(default="", max_length=120)
    permissions: dict[str, str] = Field(default_factory=dict)


class AdminUpdateIn(BaseModel):
    display_name: str | None = Field(default=None, max_length=24)
    note: str | None = Field(default=None, max_length=120)
    password: str | None = None
    status: str | None = Field(default=None, pattern="^(active|disabled)$")
    permissions: dict[str, str] | None = None


class FeedbackUpdateIn(BaseModel):
    status: str | None = Field(default=None, pattern="^(open|read|closed)$")
    reply: str | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------- 认证

def get_admin(
    authorization: str | None = Header(default=None),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """校验管理员令牌，并把这个管理员的账号与权限一起挂上来。

    后续所有后台接口都依赖它；需要更细权限的接口再套一层 require_area / require_super。
    """
    token = ""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            token = value.strip()

    if not token:
        raise HTTPException(status_code=401, detail="请先登录后台")

    row = conn.execute(
        "SELECT * FROM admin_sessions WHERE token = ? AND expires_at > ?",
        (token, now_ts()),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")

    session = dict(row)
    admin = admins.find_admin_by_id(conn, int(session.get("admin_id") or 0))
    if admin is None:
        # 账号被删掉了 —— 顺手把这个孤儿令牌清掉，免得它一直躺在表里
        conn.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))
        conn.commit()
        raise HTTPException(status_code=401, detail="该管理员账号已被移除，请重新登录")

    if (admin.get("status") or "active") != "active":
        raise HTTPException(status_code=403, detail="该管理员账号已被停用")

    session["admin"] = admin
    session["perms"] = admins.permissions_of(conn, admin)
    session["username"] = admin["username"]
    session["is_super"] = bool(admin["is_super"])
    return session


def require_super(current: dict = Depends(get_admin)) -> dict:
    """只有超级管理员能过的关卡。"""
    if not current["is_super"]:
        raise HTTPException(status_code=403, detail="只有超级管理员能做这件事")
    return current


def require_area(area: str, need: str = "view"):
    """生成一个「需要某个受控模块权限」的依赖。

    need='view' 时 view/manage 都放行；need='manage' 时只有 manage 放行。
    """

    def dependency(current: dict = Depends(get_admin)) -> dict:
        if not admins.can(current["admin"], current["perms"], area, need):
            label = dict((key, name) for key, name, _ in admins.AREAS).get(area, area)
            if need == "manage":
                raise HTTPException(status_code=403, detail=f"你没有修改「{label}」的权限")
            raise HTTPException(status_code=403, detail=f"你没有查看「{label}」的权限")
        return current

    return dependency


def _session_payload(conn: sqlite3.Connection, admin: dict, token: str) -> dict[str, Any]:
    perms = admins.permissions_of(conn, admin)
    return {
        "ok": True,
        "token": token,
        "expires_in": ADMIN_TTL_SECONDS,
        "admin": admins.public_admin(admin, perms),
        "tabs": admins.visible_tabs(admin, perms),
        "areas": admins.area_catalog(),
        "base_tabs": list(admins.BASE_TABS),
    }


@router.post("/login", summary="管理员登录（需过人机验证）")
def admin_login(
    payload: AdminLoginIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
):
    # ！人机验证要排在计失败次数**之前**，否则这个接口就变成了一个
    #   「有没有超次数」的探针：不带验证码空刷几次就能把提示词从
    #   「密码错误」试成「密码错误次数过多」—— 虽然拿不到密码，
    #   但白白浪费了防线。先卡验证码，刷不动。
    guard_captcha(conn, payload, "admin")

    ip = _client_ip(request)
    stamp = now_ts()

    # ！密码入口可以被超管关掉（v0.32）。
    #   判断放在人机验证**之后**：否则这个接口就变成了「密码入口开没开」的探针，
    #   而且顺手把「能不能暴力试」和「入口开没开」两件事混在了一起。
    #   ！这里不要再调一次 guard_captcha —— 验证码是一次性的，上面那次已经把它
    #     消费掉了，再调必然失败，表现就是「关掉密码入口之后提示的却是验证码错误」。
    if not admins.password_login_enabled(conn):
        raise HTTPException(
            status_code=403,
            detail="密码登录已在后台关闭，请用密钥文件登录",
        )

    failures = int(
        conn.execute(
            "SELECT COUNT(*) FROM admin_logins WHERE ip = ? AND success = 0 AND created_at > ?",
            (ip, stamp - FAILURE_WINDOW),
        ).fetchone()[0]
    )
    if failures >= MAX_FAILURES:
        raise HTTPException(
            status_code=429,
            detail=f"密码错误次数过多，请 {FAILURE_WINDOW // 60} 分钟后再试",
        )

    username = (payload.username or "").strip().lower() or admins.SUPER_USERNAME
    admin = admins.verify_login(conn, username, payload.password or "")

    conn.execute(
        "INSERT INTO admin_logins(ip, success, created_at) VALUES (?, ?, ?)",
        (ip, 1 if admin else 0, stamp),
    )

    if admin is None:
        conn.commit()
        remaining = max(MAX_FAILURES - failures - 1, 0)
        # ！不要区分「账号不存在」和「口令错误」，那等于把管理员账号名列表送出去
        detail = "用户名或密码不正确"
        if remaining:
            detail += f"（还可以尝试 {remaining} 次）"
        raise HTTPException(status_code=401, detail=detail)

    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO admin_sessions(token, created_at, expires_at, ip, admin_id, admin_name, is_super) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (token, stamp, stamp + ADMIN_TTL_SECONDS, ip, admin["id"], admin["username"],
         1 if admin["is_super"] else 0),
    )
    conn.execute("UPDATE admins SET last_login = ? WHERE id = ?", (stamp, admin["id"]))
    # 登录成功后清掉这个 IP 的失败记录
    conn.execute("DELETE FROM admin_logins WHERE ip = ? AND success = 0", (ip,))
    # 顺手清理陈年数据。这两张表以前只增不删：失败记录只在登录成功时按 IP 清，
    # 成功记录从不清理；过期令牌也只是靠查询时的 expires_at 过滤，行一直留着。
    # 跑久了它们会一直长，而且后台里的「登录尝试」会越看越多。
    conn.execute("DELETE FROM admin_logins WHERE created_at < ?", (stamp - 86400,))
    conn.execute("DELETE FROM admin_sessions WHERE expires_at <= ?", (stamp,))
    conn.commit()

    return _session_payload(conn, admin, token)


# ============================================================================
# 密钥对登录（v0.32）
#
# 模型和 SSH 私钥一样：私钥是一个文件、在用户手上，服务器只存公钥。
# 曲线运算与验签在 backend/adminkeys.py，挑战的存取在 backend/keylogin.py。
#
# ！这一版**只给超级管理员**用（见下面 require_super）。普通管理员保持密码登录：
#   把「新加一个管理员」变成「还要引导对方完成一次密钥注册」，
#   收益不大而流程明显变长。以后需要再放开，只需去掉那几个 require_super。
# ============================================================================

class KeyLoginIn(BaseModel):
    challenge: str
    key_id: str
    # 十六进制字符串。WebCrypto 的 ECDSA 签名是裸的 r||s 各 32 字节
    signature: str


class AdminKeyCreateIn(BaseModel):
    """生成一把新密钥。

    ！必须带一份「身份证明」，两种之一：
      · 已经注册过密钥 → 用其中一把签一次挑战（challenge + key_id + signature）
      · 一把都还没有（首次启用）→ 输当前密码
    为什么非要这一步：拿到别人会话令牌的人如果能随手加一把自己的密钥，
    就获得了一个**改密码也踢不掉**的永久后门。
    """

    label: str = Field(default="", max_length=40)
    challenge: str = ""
    key_id: str = ""
    signature: str = ""
    password: str = ""


class AdminKeyDeleteIn(BaseModel):
    challenge: str = ""
    key_id: str = ""
    signature: str = ""
    password: str = ""


class PasswordLoginIn(BaseModel):
    enabled: bool
    # ！关掉密码入口是不可逆的自我设限（关了就得靠密钥才能再打开），
    #   所以要求超管当场再输一次密码确认。
    password: str = ""


def _prove_identity(conn: sqlite3.Connection, admin: dict, payload: Any) -> None:
    """校验「你确实是这个管理员」——给密钥管理这类高危动作用。

    ！传进来的是**管理员**，不是会话。get_admin() 返回的是 admin_sessions
      那一行，管理员本人在 current["admin"] 里 —— 拿错就会出现
      current["id"] 直接 KeyError 变成 500。

    ！两种证明方式的分岔点就是「此人名下有没有密钥」：
      有 → 必须用密钥签一次（不再接受密码，否则等于留了一条弱路径）
      没有 → 只能用密码（否则第一次根本无从开始）
    """
    if admins.key_count(conn, int(admin["id"])) > 0:
        challenge = getattr(payload, "challenge", "") or ""
        claimed = (getattr(payload, "key_id", "") or "").strip()
        # ！先确认这把密钥属于当前账号，再验签。
        #   反过来做的话，别人拿**他自己**的密钥签名也能过验签 ——
        #   因为他确实持有某把合法密钥，只不过不是这个账号的。
        key = admins.key_find(conn, claimed)
        if key is None or int(key["admin_id"]) != int(admin["id"]):
            raise HTTPException(status_code=403, detail="这把密钥不属于当前账号")
        try:
            keylogin.consume_challenge(conn, challenge)
            keylogin.check_signature(conn, challenge, claimed,
                                     getattr(payload, "signature", ""))
        except keylogin.KeyLoginError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return

    if not verify_password(getattr(payload, "password", "") or "",
                           admin.get("password_hash") or ""):
        raise HTTPException(status_code=403, detail="请输入当前密码确认身份")


@router.post("/keys/challenge", summary="领一张登录挑战（公开）")
def key_challenge(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
):
    """登录的第一步。公开接口 —— 拿挑战是登录的一部分，不可能要求先登录。

    ！返回的挑战是**一次性**的，而且只有 2 分钟有效期。
    """
    try:
        return keylogin.issue_challenge(conn, _client_ip(request))
    except keylogin.KeyLoginError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


@router.post("/keys/login", summary="用密钥文件登录（公开）")
def key_login(
    payload: KeyLoginIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
):
    """验签通过就发管理员会话。不需要密码，也不需要人机验证。

    ！不需要人机验证，是因为这一步本来就刷不动：
      每次尝试都要先领一张一次性挑战，而挑战按 IP 限了 60 张/小时；
      再加上下面的失败次数限制，能试的次数少得多。
      密码登录要人机验证，是因为密码空间小、而且会被撞库复用。
    """
    ip = _client_ip(request)
    stamp = now_ts()

    failures = int(
        conn.execute(
            "SELECT COUNT(*) FROM admin_logins WHERE ip = ? AND success = 0 AND created_at > ?",
            (ip, stamp - FAILURE_WINDOW),
        ).fetchone()[0]
    )
    if failures >= MAX_FAILURES:
        raise HTTPException(
            status_code=429,
            detail=f"验证失败次数过多，请 {FAILURE_WINDOW // 60} 分钟后再试",
        )

    try:
        # ！顺序要紧：先作废挑战，再验签。反过来的话，验签失败时挑战还活着 ——
        #   同一张挑战就能被反复用来试，一次性就名存实亡了。
        keylogin.consume_challenge(conn, payload.challenge)
        result = keylogin.check_signature(
            conn, payload.challenge, payload.key_id, payload.signature
        )
    except keylogin.KeyLoginError as exc:
        conn.execute(
            "INSERT INTO admin_logins(ip, success, created_at) VALUES (?, 0, ?)",
            (ip, stamp),
        )
        conn.commit()
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    admin = result["admin"]
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO admin_sessions(token, created_at, expires_at, ip, admin_id, admin_name, is_super) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (token, stamp, stamp + ADMIN_TTL_SECONDS, ip, admin["id"], admin["username"],
         1 if admin["is_super"] else 0),
    )
    conn.execute("UPDATE admins SET last_login = ? WHERE id = ?", (stamp, admin["id"]))
    admins.key_touch(conn, str(result["key"]["key_id"]), ip)
    conn.execute("DELETE FROM admin_logins WHERE ip = ? AND success = 0", (ip,))
    conn.execute("DELETE FROM admin_logins WHERE created_at < ?", (stamp - 86400,))
    conn.execute("DELETE FROM admin_sessions WHERE expires_at <= ?", (stamp,))
    conn.commit()

    return _session_payload(conn, admin, token)


@router.get("/keys", summary="我注册了哪些密钥")
def list_admin_keys(
    current: dict = Depends(get_admin),
    conn: sqlite3.Connection = Depends(get_db),
):
    keys = admins.key_list(conn, int(current["admin"]["id"]))
    return {
        "keys": [
            {
                "key_id": item["key_id"],
                "label": item["label"],
                "created_at": int(item["created_at"]),
                "last_used_at": int(item["last_used_at"] or 0),
                "last_used_ip": item["last_used_ip"] or "",
            }
            for item in keys
        ],
        "password_login": admins.password_login_enabled(conn),
        "can_manage": bool(current["is_super"]),
    }


@router.post("/keys", summary="生成一把新密钥（仅超级管理员）")
def create_admin_key(
    payload: AdminKeyCreateIn,
    current: dict = Depends(require_super),
    conn: sqlite3.Connection = Depends(get_db),
):
    """生成密钥对，把**私钥**交给你下载，服务器只留公钥。

    ！private_pem 只在这一次响应里出现，不落库、不记日志。
      响应结束后服务器上就再也没有它了 —— 哪怕是数据库管理员也拿不回来。
      所以前端必须立刻把它存成文件，并且提示用户别关掉页面。
    """
    if admins.key_count(conn, int(current["admin"]["id"])) >= MAX_KEYS_PER_ADMIN:
        raise HTTPException(
            status_code=409,
            detail=f"最多只能注册 {MAX_KEYS_PER_ADMIN} 把密钥，先删掉一把不用的",
        )

    _prove_identity(conn, current["admin"], payload)

    pair = adminkeys.generate()
    admins.key_add(
        conn, int(current["admin"]["id"]), str(pair["key_id"]),
        str(pair["public_key"]), payload.label,
    )
    conn.commit()
    return {
        "key_id": pair["key_id"],
        "label": (payload.label or "")[:40],
        "file_name": f"mcfr-admin-{pair['key_id']}{adminkeys.KEY_FILE_SUFFIX}",
        "private_pem": pair["private_pem"],
        "message": "密钥文件已生成。请立刻保存 —— 关掉这个窗口就再也拿不到了。",
    }


@router.delete("/keys/{key_id}", summary="删掉一把密钥（仅超级管理员）")
def delete_admin_key(
    key_id: str,
    payload: AdminKeyDeleteIn,
    current: dict = Depends(require_super),
    conn: sqlite3.Connection = Depends(get_db),
):
    """删掉一把密钥。

    ！v0.32 改过一次：以前这里禁止「用要删的那把密钥来证明身份」，理由是
      「别用 A 删掉 A」。结果是——**只有一把密钥的人永远删不掉它**：
      删密钥要求签名（有密钥就不再接受密码），而手里唯一能拿出来的密钥
      恰好就是要删的那把，怎么点都是 400。想换设备就只能去动数据库。
      而且这条规则本来也没挡住什么：能签出这个名的人，本来就已经持有
      这把密钥了，删掉它并不比删掉别的密钥更危险；真正会造成「进不去」
      的情况（删完一把不剩）由下面那条单独挡着。
    """
    _prove_identity(conn, current["admin"], payload)

    # 删掉最后一把之前先提醒：那等于把密钥这条登录路彻底断掉
    remaining = admins.key_count(conn, int(current["admin"]["id"]))
    if remaining <= 1 and not admins.password_login_enabled(conn):
        raise HTTPException(
            status_code=409,
            detail="这是最后一把密钥，而密码登录已关闭 —— 删掉就再也进不来了。"
                   "请先打开密码登录，或者再注册一把。",
        )

    if not admins.key_remove(conn, int(current["admin"]["id"]), key_id):
        raise HTTPException(status_code=404, detail="没有找到这把密钥")
    conn.commit()
    return {"ok": True, "message": "密钥已删除，用它的设备下次需要重新注册"}


@router.patch("/password-login", summary="开关密码登录入口（仅超级管理员）")
def set_password_login(
    payload: PasswordLoginIn,
    current: dict = Depends(require_super),
    conn: sqlite3.Connection = Depends(get_db),
):
    """关掉之后，后台只能靠密钥文件进。

    ！关掉之前先确认密钥真的能用 —— 所以这里要求再输一次密码。
      这不是走形式：关掉之后，密码就成了最后一道能从外面打开的门，
      而合上它的那一刻必须是你本人清醒的决定。
    """
    if not verify_password(payload.password or "",
                           current["admin"].get("password_hash") or ""):
        raise HTTPException(status_code=403, detail="请输入当前密码确认身份")

    if not payload.enabled and admins.key_count(conn, int(current["admin"]["id"])) == 0:
        raise HTTPException(
            status_code=409,
            detail="你还没有注册任何密钥。先注册一把并确认能用，再关密码登录。",
        )

    admins.set_password_login(conn, payload.enabled)
    conn.commit()
    return {
        "ok": True,
        "password_login": payload.enabled,
        "message": "密码登录已开启" if payload.enabled else "密码登录已关闭，以后只能用密钥文件进后台",
    }


@router.get("/me", summary="当前管理员是谁、能看哪些页签")
def whoami(
    current: dict = Depends(get_admin),
    conn: sqlite3.Connection = Depends(get_db),
):
    return _session_payload(conn, current["admin"], current["token"])


@router.post("/logout", summary="管理员退出")
def admin_logout(
    current: dict = Depends(get_admin),
    conn: sqlite3.Connection = Depends(get_db),
):
    conn.execute("DELETE FROM admin_sessions WHERE token = ?", (current["token"],))
    conn.commit()
    return {"ok": True}


@router.post("/password", summary="修改自己的密码")
def change_password(
    payload: AdminPasswordIn,
    current: dict = Depends(get_admin),
    conn: sqlite3.Connection = Depends(get_db),
):
    me = current["admin"]
    if not verify_password(payload.old_password or "", me["password_hash"]):
        raise HTTPException(status_code=401, detail="当前密码不正确")

    try:
        new_password = admins.validate_password(payload.new_password)
    except admins.AdminInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # 超级管理员的口令源头在 .env：改了要顺手写回去，
    # 否则下次重启 ensure_super_admin() 又会把库里的口令覆盖成旧的那串。
    if me["is_super"]:
        os.environ["MCFR_ADMIN_PASSWORD"] = new_password
        update_env({"MCFR_ADMIN_PASSWORD": new_password})

    conn.execute(
        "UPDATE admins SET password_hash = ? WHERE id = ?",
        (hash_password(new_password), me["id"]),
    )
    # 改密之后踢掉其它设备，只留当前这一个，避免旧密码被人继续用着
    conn.execute(
        "DELETE FROM admin_sessions WHERE admin_id = ? AND token != ?",
        (me["id"], current["token"]),
    )
    conn.commit()

    message = "密码已更新"
    message += "，并已写入 .env（重启后依然有效）" if me["is_super"] else "，其它设备已退出登录"
    return {"ok": True, "message": message}



# ---------------------------------------------------------------- 数据概览

@router.get("/overview", summary="后台数据概览")
def overview(
    current: dict = Depends(get_admin),
    conn: sqlite3.Connection = Depends(get_db),
):
    stamp = now_ts()

    def scalar(sql: str, args: tuple = ()) -> int:
        return int(conn.execute(sql, args).fetchone()[0])

    return {
        "users": {
            "total": scalar("SELECT COUNT(*) FROM users"),
            "active": scalar("SELECT COUNT(*) FROM users WHERE status = 'active'"),
            "suspended": scalar("SELECT COUNT(*) FROM users WHERE status != 'active'"),
            "online": scalar(
                "SELECT COUNT(*) FROM users WHERE status = 'active' "
                "AND show_online = 1 AND last_seen > ?",
                (stamp - 300,),
            ),
            "new_7d": scalar("SELECT COUNT(*) FROM users WHERE created_at > ?", (stamp - 86400 * 7,)),
        },
        "social": {
            "connections": scalar("SELECT COUNT(*) FROM connections"),
            "requests_pending": scalar(
                "SELECT COUNT(*) FROM contact_requests WHERE status = 'pending'"
            ),
            "requests_accepted": scalar(
                "SELECT COUNT(*) FROM contact_requests WHERE status = 'accepted'"
            ),
            "with_contact": scalar(
                "SELECT COUNT(*) FROM users WHERE contact_qq != '' "
                "OR contact_wechat != '' OR contact_other != ''"
            ),
        },
        "safety": {
            "reports_open": scalar("SELECT COUNT(*) FROM reports WHERE status = 'open'"),
            "reports_total": scalar("SELECT COUNT(*) FROM reports"),
            "blocks": scalar("SELECT COUNT(*) FROM blocks"),
        },
        # v0.21 起这里从「邮件验证码」换成了「人机验证」（见图表页签的安全记录）
        "captcha": captcha.stats(conn),
    }


# ---------------------------------------------------------------- 举报处理

@router.get("/reports", summary="举报列表")
def list_reports(
    status: str = Query(default="open", pattern="^(open|resolved|all)$"),
    current: dict = Depends(get_admin),
    conn: sqlite3.Connection = Depends(get_db),
):
    where = "" if status == "all" else "WHERE r.status = ?"
    args: tuple = () if status == "all" else (status,)

    rows = conn.execute(
        f"""
        SELECT r.id, r.reason, r.detail, r.status, r.created_at,
               rp.id AS reporter_id, rp.nickname AS reporter_nickname,
               rp.username AS reporter_username, rp.avatar_hue AS reporter_hue,
               tp.id AS target_id, tp.nickname AS target_nickname,
               tp.username AS target_username, tp.avatar_hue AS target_hue,
               tp.status AS target_status
        FROM reports r
        JOIN users rp ON rp.id = r.reporter_id
        JOIN users tp ON tp.id = r.target_user
        {where}
        ORDER BY r.created_at DESC
        LIMIT 100
        """,
        args,
    ).fetchall()

    return {"items": [dict(row) for row in rows]}


@router.post("/reports/{report_id}/resolve", summary="标记举报已处理")
def resolve_report(
    report_id: int,
    current: dict = Depends(get_admin),
    conn: sqlite3.Connection = Depends(get_db),
):
    if conn.execute("SELECT 1 FROM reports WHERE id = ?", (report_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="举报记录不存在")

    conn.execute("UPDATE reports SET status = 'resolved' WHERE id = ?", (report_id,))
    conn.commit()
    return {"ok": True, "message": "已标记为处理完成"}


@router.post("/reports/{report_id}/reopen", summary="重新打开举报")
def reopen_report(
    report_id: int,
    current: dict = Depends(get_admin),
    conn: sqlite3.Connection = Depends(get_db),
):
    conn.execute("UPDATE reports SET status = 'open' WHERE id = ?", (report_id,))
    conn.commit()
    return {"ok": True}


# ---------------------------------------------------------------- 安全记录

@router.get("/security", summary="人机验证记录（用于发现可疑流量）")
def security_logs(
    limit: int = Query(default=60, ge=1, le=300),
    current: dict = Depends(get_admin),
    conn: sqlite3.Connection = Depends(get_db),
):
    """原来这里是「邮件记录」，展示验证码发给了哪些邮箱。

    v0.21 把邮件整个取消之后，同样要看的东西变成了「谁在领验证码、谁一直在错」——
    对手已经从「轰炸别人的邮箱」变成了「拿脚本硬试密码」。

    ！这里只返回 created_ip（网络地址），不发任何用户身份信息：
      验证码是**登录前**发的，那一刻本来就不知道对方是谁。
    """
    return {
        "stats": captcha.stats(conn),
        "items": captcha.recent(conn, limit),
        "suspicious": captcha.suspicious_ips(conn),
    }


# ---------------------------------------------------------------- 用户管理

@router.get("/users", summary="用户列表")
def list_users(
    q: str = Query(default="", max_length=40),
    status: str = Query(default="all", pattern="^(all|active|suspended)$"),
    limit: int = Query(default=50, ge=1, le=200),
    current: dict = Depends(get_admin),
    conn: sqlite3.Connection = Depends(get_db),
):
    clauses: list[str] = []
    args: list[Any] = []

    if q.strip():
        like = f"%{q.strip()}%"
        clauses.append("(username LIKE ? OR nickname LIKE ? OR email LIKE ?)")
        args.extend([like, like, like])

    if status == "active":
        clauses.append("status = 'active'")
    elif status == "suspended":
        clauses.append("status != 'active'")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    rows = conn.execute(
        f"SELECT * FROM users {where} ORDER BY created_at DESC LIMIT ?",
        (*args, limit),
    ).fetchall()

    items = []
    for row in rows:
        user = dict(row)
        item = to_public(user)
        item["email"] = user.get("email") or ""
        item["status"] = user.get("status")
        # ！具体的月和日**只有后台拿得到** —— to_public() 对前台一律只给「几岁」。
        #   这里给出来是为了把「修正年龄」弹窗的生日预填好，
        #   否则管理员每改一个人就得从头重填一遍月和日。
        item["birth_month"] = int(user.get("birth_month") or 0)
        item["birth_day"] = int(user.get("birth_day") or 0)
        # 只告诉后台「是否预置了联系方式」，不返回具体内容
        item["contact_prepared"] = bool(
            (user.get("contact_qq") or "").strip()
            or (user.get("contact_wechat") or "").strip()
            or (user.get("contact_other") or "").strip()
        )
        items.append(item)

    return {"items": items}


@router.post("/users/{user_id}/status", summary="暂停或恢复用户")
def set_user_status(
    user_id: int,
    payload: UserStatusIn,
    current: dict = Depends(get_admin),
    conn: sqlite3.Connection = Depends(get_db),
):
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if row["is_admin"]:
        raise HTTPException(status_code=403, detail="不能修改管理员账号的状态")

    conn.execute("UPDATE users SET status = ? WHERE id = ?", (payload.status, user_id))
    if payload.status != "active":
        # 暂停的同时让该用户所有登录立即失效
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.commit()

    action = "已恢复" if payload.status == "active" else "已暂停"
    return {"ok": True, "message": f"{row['nickname']} {action}"}


@router.post("/users/{user_id}/birth", summary="修正用户年龄与生日（仅超级管理员）")
def set_user_birth(
    user_id: int,
    payload: BirthIn,
    current: dict = Depends(require_super),
    conn: sqlite3.Connection = Depends(get_db),
):
    """改正用户填错的年龄 / 生日。

    ！这个接口存在的唯一理由，就是「用户自己改不了」——
      注册和补填都是一次性的（见 routers/users.py 的 set_birth），
      填错了没有任何自助途径。没有这一步，注册页上那句
      「填错只能找管理员」就是一句空话。

    ！必须走 require_super：改年龄等于改这个人在站里属于哪一侧人群，
      这个权限不该散给普通管理员。
    """
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    year = age.birth_year_from(payload.age, payload.birth_month, payload.birth_day)
    before = age.age_on(row["birth_year"], row["birth_month"], row["birth_day"])
    conn.execute(
        "UPDATE users SET birth_year = ?, birth_month = ?, birth_day = ? WHERE id = ?",
        (year, payload.birth_month, payload.birth_day, user_id),
    )
    conn.commit()

    after = age.age_on(year, payload.birth_month, payload.birth_day)
    return {
        "ok": True,
        "before": before,
        "after": after,
        "message": f"{row['nickname']} 的年龄已从 {before} 岁改为 {after} 岁"
        if before is not None
        else f"{row['nickname']} 的年龄已设为 {after} 岁",
    }


# 临时密码用的字符表。
# ！同样去掉了 O 0 I l 1 —— 这个密码是要管理员**口头念给用户**或者写在纸上的，
#   一个分不清的字符就得从头再来一遍。
TEMP_PASSWORD_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
TEMP_PASSWORD_LENGTH = 10


@router.post("/users/{user_id}/reset-password", summary="把用户密码重置成临时密码")
def reset_user_password(
    user_id: int,
    current: dict = Depends(get_admin),
    conn: sqlite3.Connection = Depends(get_db),
):
    """取消邮件后，这是「忘记密码」唯一的出口。

    生成一个随机临时密码并**直接返回给管理员**，由管理员当面/私下告诉本人。
    刻意不做成「发到用户邮箱」—— 那正是刚刚被删掉的那条路。

    ！返回的明文只会出现在这一次响应里，库里存的仍然只有哈希，
      而且所有旧会话会立刻失效（否则密码泄露的人能继续用着）。
    """
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if row["is_admin"]:
        raise HTTPException(status_code=403, detail="不能从用户列表重置管理员密码")

    temp_password = "".join(
        secrets.choice(TEMP_PASSWORD_ALPHABET) for _ in range(TEMP_PASSWORD_LENGTH)
    )
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (hash_password(temp_password), user_id),
    )
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.commit()

    return {
        "ok": True,
        "password": temp_password,
        "nickname": row["nickname"],
        "message": f"已重置 {row['nickname']} 的密码，请当面告诉他，并提醒他登录后自己改一个",
    }


# ---------------------------------------------------------------- 升级与数据保全
#
# 这一组接口干的是「让用户数据跨版本活下来」这件事，具体实现全在 backend/upgrade.py：
#   · 备份：把当前数据库整份快照到 data/backups/
#   · 导出：和备份是同一件事，只是给管理员拿去下载 / 换机器用
#   · 导入 / 还原：用一个 .db 文件整体覆盖当前数据库，覆盖前先自动存一份
#   · 升级：把数据库结构与数据追平到当前程序版本
#
# 所有写操作都串在一把锁上（_maintenance_lock），避免两个人同时点。

def _status_payload(message: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": True, "system": upgrade.system_status()}
    if message:
        payload["message"] = message
    return payload


@router.get("/system", summary="系统状态：版本、数据库信息、备份列表、升级历史")
def system_status(current: dict = Depends(require_area("system", "view"))):
    return upgrade.system_status()


@router.post("/system/backup", summary="立即备份数据库")
def create_backup(
    label: str = Query(default="manual", max_length=32),
    current: dict = Depends(require_area("system", "manage")),
):
    with _maintenance_lock:
        try:
            item = upgrade.create_backup(label)
        except upgrade.UpgradeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        upgrade.prune_backups(upgrade.KEEP_BACKUPS, protect=item.name)

    print(f"[mcfr] 管理员手动备份完成：{item.name}")
    return _status_payload(f"已备份：{item.name}（{upgrade.human_size(item.size)}）")


@router.post("/system/upgrade", summary="立即执行待升级项")
def run_upgrade_now(current: dict = Depends(require_area("system", "manage"))):
    """正常情况下启动时就会自动升级；这个按钮用于导入旧库之后手动补一次。"""
    with _maintenance_lock:
        try:
            report = upgrade.run_upgrade(trigger="admin", force=True)
        except upgrade.UpgradeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    if report["status"] == "up-to-date":
        message = f"数据库已是最新（v{report['to_version']}），没有需要执行的升级项"
    else:
        message = (
            f"升级完成：v{report.get('from_version')} → v{report.get('to_version')}，"
            f"用户 {report.get('users')} 个，数据完整"
        )
        if report.get("backup"):
            message += f"；升级前的快照已存为 {report['backup']}"
    return _status_payload(message)


@router.get("/system/export", summary="下载当前数据库（含最新写入）")
def export_database(current: dict = Depends(require_area("system", "manage"))):
    with _maintenance_lock:
        try:
            item = upgrade.export_snapshot("export")
        except upgrade.UpgradeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return FileResponse(
        item.path,
        media_type="application/octet-stream",
        filename=item.name,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/system/backup/{name}", summary="下载某一份备份")
def download_backup(name: str, current: dict = Depends(require_area("system", "manage"))):
    item = upgrade.get_backup(name)
    if item is None:
        raise HTTPException(status_code=404, detail="找不到这份备份")
    return FileResponse(
        item.path,
        media_type="application/octet-stream",
        filename=item.name,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/system/backup/{name}/restore", summary="用某一份备份还原数据库")
def restore_backup(name: str, current: dict = Depends(require_area("system", "manage"))):
    item = upgrade.get_backup(name)
    if item is None:
        raise HTTPException(status_code=404, detail="找不到这份备份")

    with _maintenance_lock:
        try:
            report = upgrade.restore_from_file(item.path, keep_admin_token=current["token"])
        except upgrade.UpgradeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    print(f"[mcfr] 管理员用 {item.name} 还原了数据库")
    return {
        **_status_payload(
            f"已还原：该备份里有 {report['imported_users']} 个用户"
            f"（库版本 v{report['imported_version']}，已自动升到最新）"
            f"；还原前的数据存为 {report['safety_backup']}"
        ),
        "report": report,
    }


@router.delete("/system/backup/{name}", summary="删除某一份备份")
def remove_backup(name: str, current: dict = Depends(require_area("system", "manage"))):
    if not upgrade.delete_backup(name):
        raise HTTPException(status_code=404, detail="找不到这份备份")
    return _status_payload("已删除该备份")


@router.post("/system/import", summary="导入一个数据库文件（整体替换当前数据）")
async def import_database(request: Request, current: dict = Depends(require_area("system", "manage"))):
    """请求体就是数据库文件本身（application/octet-stream）。

    和图片上传一样走「原始请求体」而不是 multipart —— 这样不需要
    python-multipart，前端直接 fetch(..., { body: file }) 就行。
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > upgrade.MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="文件太大了，请确认你选的是数据库文件")

    data = await request.body()
    if len(data) > upgrade.MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="文件太大了，请确认你选的是数据库文件")

    with _maintenance_lock:
        try:
            report = upgrade.import_bytes(
                data,
                filename=request.headers.get("x-filename", "upload.db"),
                keep_admin_token=current["token"],
            )
        except upgrade.UpgradeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    print(f"[mcfr] 管理员导入了数据库（{report['imported_users']} 个用户）")
    return {
        **_status_payload(
            f"导入成功：{report['users']} 个用户已就位（来源库版本 v{report['imported_version']}，"
            f"已自动升到最新）；导入前的数据存为 {report['safety_backup']}。"
            "用户原来的登录状态不会保留，需要重新登录一次，账号与资料都在。"
        ),
        "report": report,
    }


# ---------------------------------------------------------------- 管理员管理
#
# 只有超级管理员能进来。这里能做的事：添加普通管理员、改资料 / 改密 / 停用、
# 删除，以及逐项设置「某个普通管理员能不能看 / 能不能改」某个受控模块。

def _admins_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM admins ORDER BY is_super DESC, created_at ASC"
    ).fetchall()
    items = []
    for row in rows:
        admin = dict(row)
        perms = admins.permissions_of(conn, admin)
        item = admins.public_admin(admin, perms)
        # 顺带统计每人有多少个还活着的登录，方便超管判断「这个人是不是还在用」
        item["active_sessions"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM admin_sessions WHERE admin_id = ? AND expires_at > ?",
                (admin["id"], now_ts()),
            ).fetchone()[0]
        )
        items.append(item)

    return {
        "items": items,
        "areas": admins.area_catalog(),
        "base_tabs": list(admins.BASE_TABS),
        "levels": list(admins.LEVELS),
        "super_username": admins.SUPER_USERNAME,
    }


def _reload_admin(conn: sqlite3.Connection, admin_id: int) -> dict[str, Any]:
    admin = admins.find_admin_by_id(conn, admin_id)
    if admin is None:
        raise HTTPException(status_code=404, detail="这个管理员账号不存在了")
    return admins.public_admin(admin, admins.permissions_of(conn, admin))


@router.get("/admins", summary="管理员账号列表（超级管理员）")
def list_admins(
    current: dict = Depends(require_super),
    conn: sqlite3.Connection = Depends(get_db),
):
    return _admins_payload(conn)


@router.post("/admins", summary="添加普通管理员（超级管理员）")
def create_admin(
    payload: AdminCreateIn,
    current: dict = Depends(require_super),
    conn: sqlite3.Connection = Depends(get_db),
):
    try:
        username = admins.validate_username(payload.username)
        password = admins.validate_password(payload.password)
    except admins.AdminInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if admins.find_admin(conn, username) is not None:
        raise HTTPException(status_code=409, detail="这个用户名已经有人用了")

    cursor = conn.execute(
        """
        INSERT INTO admins
            (username, display_name, password_hash, is_super, status,
             note, created_by, created_at, last_login)
        VALUES (?, ?, ?, 0, 'active', ?, ?, ?, 0)
        """,
        (
            username,
            (payload.display_name or "").strip() or username,
            hash_password(password),
            (payload.note or "").strip(),
            current["username"],
            now_ts(),
        ),
    )
    admin_id = int(cursor.lastrowid)
    admins.store_permissions(
        conn, admin_id, admins.normalize_permissions(payload.permissions)
    )
    conn.commit()

    print(f"[mcfr] 超管 {current['username']} 添加了普通管理员：{username}")
    return {
        "ok": True,
        "message": f"已添加管理员「{username}」",
        "item": _reload_admin(conn, admin_id),
        "admins": _admins_payload(conn)["items"],
    }


@router.patch("/admins/{admin_id}", summary="修改管理员资料 / 密码 / 状态 / 权限（超级管理员）")
def update_admin(
    admin_id: int,
    payload: AdminUpdateIn,
    current: dict = Depends(require_super),
    conn: sqlite3.Connection = Depends(get_db),
):
    target = admins.find_admin_by_id(conn, admin_id)
    if target is None:
        raise HTTPException(status_code=404, detail="这个管理员账号不存在了")

    is_self = int(target["id"]) == int(current["admin"]["id"])
    notes: list[str] = []

    if payload.status is not None and not target["is_super"]:
        if payload.status == "disabled":
            # 停用自己会让手上这个令牌立刻失效，体验很怪 —— 直接挡掉
            if is_self:
                raise HTTPException(status_code=422, detail="不能停用自己")
            conn.execute("UPDATE admins SET status = 'disabled' WHERE id = ?", (admin_id,))
            admins.revoke_sessions(conn, admin_id)
            notes.append("已停用，该账号的登录立即失效")
        else:
            conn.execute("UPDATE admins SET status = 'active' WHERE id = ?", (admin_id,))
            notes.append("已恢复")

    # ！超级管理员账号不能被降级、停用或删掉 —— 否则一旦把自己锁死，
    #   就只能去改数据库文件才能救回来了。这里只允许改它的显示名。
    if payload.display_name is not None:
        name = (payload.display_name or "").strip()
        if not name:
            name = admins.SUPER_USERNAME if target["is_super"] else target["username"]
        conn.execute("UPDATE admins SET display_name = ? WHERE id = ?", (name, admin_id))
        notes.append("显示名已更新")

    if payload.note is not None and not target["is_super"]:
        conn.execute(
            "UPDATE admins SET note = ? WHERE id = ?", ((payload.note or "").strip(), admin_id)
        )
        notes.append("备注已更新")

    if payload.password:
        try:
            new_password = admins.validate_password(payload.password)
        except admins.AdminInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        conn.execute(
            "UPDATE admins SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), admin_id),
        )
        # 改密之后把该账号的登录全部踢掉（包括他在别处开着的那几个）
        admins.revoke_sessions(conn, admin_id)
        notes.append("密码已重置，该账号需要重新登录")

    if payload.permissions is not None:
        if target["is_super"]:
            raise HTTPException(status_code=422, detail="超级管理员不需要也不接受逐项授权")
        admins.store_permissions(conn, admin_id, admins.normalize_permissions(payload.permissions))
        notes.append("权限已更新")

    conn.commit()

    return {
        "ok": True,
        "message": "；".join(notes) or "没有任何改动",
        "item": _reload_admin(conn, admin_id),
        "admins": _admins_payload(conn)["items"],
    }


@router.delete("/admins/{admin_id}", summary="删除普通管理员（超级管理员）")
def delete_admin(
    admin_id: int,
    current: dict = Depends(require_super),
    conn: sqlite3.Connection = Depends(get_db),
):
    target = admins.find_admin_by_id(conn, admin_id)
    if target is None:
        raise HTTPException(status_code=404, detail="这个管理员账号不存在了")
    if target["is_super"]:
        raise HTTPException(status_code=422, detail="超级管理员账号不能删除")
    if int(target["id"]) == int(current["admin"]["id"]):
        raise HTTPException(status_code=422, detail="不能删除自己")

    admins.revoke_sessions(conn, admin_id)
    conn.execute("DELETE FROM admin_permissions WHERE admin_id = ?", (admin_id,))
    conn.execute("DELETE FROM admins WHERE id = ?", (admin_id,))
    conn.commit()

    print(f"[mcfr] 超管 {current['username']} 删除了管理员：{target['username']}")
    return {
        "ok": True,
        "message": f"已删除管理员「{target['username']}」",
        "admins": _admins_payload(conn)["items"],
    }


# ---------------------------------------------------------------- 意见反馈
#
# 归属「feedback」这个受控模块：
#   view   → 能看列表和内容
#   manage → 还能改状态、写回复、删除

FEEDBACK_KIND_LABELS = {
    "suggestion": "功能建议",
    "bug": "问题反馈",
    "content": "内容举报",
    "safety": "安全 / 隐私",
    "other": "其它",
}


@router.get("/feedback", summary="意见反馈列表")
def list_feedback(
    status: str = Query(default="all", pattern="^(all|open|read|closed)$"),
    kind: str = Query(default="all", max_length=20),
    limit: int = Query(default=80, ge=1, le=300),
    current: dict = Depends(require_area("feedback", "view")),
    conn: sqlite3.Connection = Depends(get_db),
):
    clauses: list[str] = []
    args: list[Any] = []
    if status != "all":
        clauses.append("f.status = ?")
        args.append(status)
    if kind != "all":
        clauses.append("f.kind = ?")
        args.append(kind)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    rows = conn.execute(
        f"""
        SELECT f.*, u.username AS user_username, u.nickname AS user_nickname,
               u.avatar_hue AS user_hue, u.status AS user_status
        FROM feedback f
        LEFT JOIN users u ON u.id = f.user_id
        {where}
        ORDER BY f.created_at DESC
        LIMIT ?
        """,
        (*args, limit),
    ).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["kind_label"] = FEEDBACK_KIND_LABELS.get(item["kind"], item["kind"])
        item["guest"] = item["user_id"] is None
        items.append(item)

    counts = {
        "open": int(conn.execute("SELECT COUNT(*) FROM feedback WHERE status = 'open'").fetchone()[0]),
        "total": int(conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]),
    }

    return {
        "items": items,
        "counts": counts,
        "kinds": [{"value": k, "label": v} for k, v in FEEDBACK_KIND_LABELS.items()],
        "can_manage": admins.can(current["admin"], current["perms"], "feedback", "manage"),
    }


@router.patch("/feedback/{feedback_id}", summary="处理 / 回复一条反馈")
def update_feedback(
    feedback_id: int,
    payload: FeedbackUpdateIn,
    current: dict = Depends(require_area("feedback", "manage")),
    conn: sqlite3.Connection = Depends(get_db),
):
    row = conn.execute("SELECT * FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="这条反馈不存在了")

    sets: list[str] = []
    args: list[Any] = []
    if payload.status is not None:
        sets.append("status = ?")
        args.append(payload.status)
        if payload.status != "open":
            sets.append("handled_at = ?")
            args.append(now_ts())
            sets.append("handled_by = ?")
            args.append(current["username"])
    if payload.reply is not None:
        sets.append("reply = ?")
        args.append((payload.reply or "").strip())

    if not sets:
        raise HTTPException(status_code=422, detail="没有需要更新的内容")

    conn.execute(f"UPDATE feedback SET {', '.join(sets)} WHERE id = ?", (*args, feedback_id))
    conn.commit()
    return {"ok": True, "message": "已更新这条反馈"}


@router.delete("/feedback/{feedback_id}", summary="删除一条反馈")
def delete_feedback(
    feedback_id: int,
    current: dict = Depends(require_area("feedback", "manage")),
    conn: sqlite3.Connection = Depends(get_db),
):
    cursor = conn.execute("DELETE FROM feedback WHERE id = ?", (feedback_id,))
    conn.commit()
    if not cursor.rowcount:
        raise HTTPException(status_code=404, detail="这条反馈不存在了")
    return {"ok": True, "message": "已删除"}


# ================================================================ 帖子与评论（v0.27）
#
# 站里第一次出现「用户写的、所有人都看得见的长内容」，所以单独给一档权限。
#
# ！「下架」全部是软删（status 改成 hidden），不是 DELETE：
#   一篇帖子可能已经被别人点赞、评论、转发过，物理删掉会让那些人的
#   通知点进去变成一片空白，而他们什么错都没犯。下架之后对所有人
#   表现为「这篇帖子不存在」，数据还在，误判了也能一键恢复。

_CONTENT_STATUS = ("published", "hidden", "deleted")


@router.get("/content", summary="帖子列表（含已下架 / 已删除）")
def list_content(
    status: str = Query(default="", max_length=12),
    q: str = Query(default="", max_length=40),
    page: int = Query(default=1, ge=1),
    current: dict = Depends(require_area("content", "view")),
    conn: sqlite3.Connection = Depends(get_db),
):
    if status and status not in _CONTENT_STATUS:
        status = ""
    data = posts.admin_list(conn, status=status, keyword=q, page=page)
    data["counts"] = {
        row[0]: int(row[1])
        for row in conn.execute("SELECT status, COUNT(*) FROM posts GROUP BY status").fetchall()
    }
    data["can_manage"] = admins.can(current["admin"], current["perms"], "content", "manage")
    return data


# ！下面这条必须排在 /content/{post_id} 【前面】。
#   FastAPI 按声明顺序匹配，而 "comments" 会被 {post_id} 抓到、
#   然后因为不是整数报 422 —— 报错长得像参数写错了，其实纯粹是顺序问题。
#   实测踩过一次：内容页整个打不开，提示「Input should be a valid integer」。
@router.get("/content/comments", summary="评论列表（含已隐藏）")
def list_content_comments(
    q: str = Query(default="", max_length=40),
    page: int = Query(default=1, ge=1),
    current: dict = Depends(require_area("content", "view")),
    conn: sqlite3.Connection = Depends(get_db),
):
    data = posts.admin_comments(conn, keyword=q, page=page)
    data["can_manage"] = admins.can(current["admin"], current["perms"], "content", "manage")
    return data


@router.get("/content/{post_id}", summary="单篇帖子全文（复核用）")
def read_content(
    post_id: int,
    current: dict = Depends(require_area("content", "view")),
    conn: sqlite3.Connection = Depends(get_db),
):
    """给后台看全文。

    ！不复用前台那个 /api/posts/{id}：它要求「登录的普通用户」身份，
      而管理员在后台用的是另一套会话（admin_sessions），前台接口认不出来。
      另外前台接口对已下架的帖子一律返回 404 —— 而后台下架的帖子
      恰恰是最需要能翻出来复核的那一批。
    """
    row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="这篇帖子不存在")
    author = conn.execute(
        "SELECT id, nickname, username FROM users WHERE id = ?", (row["author_id"],)
    ).fetchone()
    return {
        "id": int(row["id"]),
        "title": row["title"],
        "blocks": posts.blocks_from_row(row),
        "status": row["status"],
        "created_at": int(row["created_at"]),
        "updated_at": int(row["updated_at"]),
        "author": {"nickname": author["nickname"] if author else "（账号已注销）"},
    }


@router.post("/content/{post_id}/status", summary="下架 / 恢复一篇帖子")
def set_content_status(
    post_id: int,
    payload: PostStatusIn,
    current: dict = Depends(require_area("content", "manage")),
    conn: sqlite3.Connection = Depends(get_db),
):
    try:
        posts.set_status(conn, post_id, payload.status, reason=payload.reason or "")
    except posts.PostError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    label = {"published": "已恢复", "hidden": "已下架", "deleted": "已标记为删除"}
    return {"ok": True, "message": label.get(payload.status, "已更新")}


@router.post("/content/comments/{comment_id}/status", summary="隐藏 / 恢复一条评论")
def set_comment_status(
    comment_id: int,
    payload: PostStatusIn,
    current: dict = Depends(require_area("content", "manage")),
    conn: sqlite3.Connection = Depends(get_db),
):
    try:
        posts.set_comment_status(conn, comment_id, payload.status)
    except posts.PostError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return {"ok": True, "message": "已下架" if payload.status != "published" else "已恢复"}


@router.post("/content/recount", summary="按明细重算点赞 / 评论 / 转发数")
def recount_content(
    current: dict = Depends(require_area("content", "manage")),
    conn: sqlite3.Connection = Depends(get_db),
):
    """冗余计数兜底修复。

    ！什么时候会需要它：计数和明细是同事务更新的，正常情况下不会漂。
      但只要有人手工改过数据库、或者哪次迁移中断在半路，两者就会对不上。
      这种时候与其去猜哪边是对的，不如留一个「照着明细重算一遍」的按钮。
    """
    count = posts.recount(conn)
    return {"ok": True, "message": f"已重算 {count} 篇帖子的计数"}
