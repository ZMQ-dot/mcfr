"""管理后台的密钥对登录：挑战的发放与核对（v0.32）。

把「一次性挑战」和「验签」这两件事单独放在这里，和密码登录并列：

    routers/admin.py   发挑战、收签名，管接口和 HTTP 状态码
    keylogin.py        挑战的存取与校验（本文件）
    adminkeys.py       纯数学：曲线运算、PKCS#8 导出、验签
    admins.py          密钥的存取（admin_keys 表）

## 为什么必须有「挑战」，而且必须是服务器给的

签名如果签的是**固定内容**（比如账号名），那签名本身就是个可以抄来抄去的
口令 —— 攻击者抓一次包就能永远重放。

所以流程是：

    ① 客户端 POST /api/admin/keys/challenge，拿一个服务器生成的随机串
    ② 客户端用它签一次名（签的是 "mcfr-admin-login:" + 挑战）
    ③ 交回来验证；**这个挑战当场作废**，再来一次就用不了了

挑战 2 分钟过期。够点完文件选择框、也够输一次系统 PIN，
但不够拿去离线分析。
"""

from __future__ import annotations

import secrets
import sqlite3

from . import adminkeys, admins
from .database import now_ts

# 挑战有效期。给 2 分钟而不是 5 分钟：它跟验证码不一样，
# 这里没有「看错重填」的问题，点完文件就签，正常只要几秒。
CHALLENGE_TTL_SECONDS = 120

# 同一个 IP 每小时最多领多少张挑战。
# ！这个接口是**公开**的（拿挑战是登录的第一步，不能要求先登录），
#   没有限制的话它就是一台免费的随机数生成器，而且会往表里灌垃圾。
CHALLENGE_PER_IP_HOUR = 60

KEEP_CHALLENGE_SECONDS = 3600      # 过期的挑战留一小时再清，方便排查


class KeyLoginError(RuntimeError):
    """密钥登录失败。message 可以直接给用户看。"""


def issue_challenge(conn: sqlite3.Connection, ip: str) -> dict:
    """发一张一次性挑战。"""
    stamp = now_ts()
    if ip:
        recent = int(conn.execute(
            "SELECT COUNT(*) FROM admin_challenges WHERE created_ip = ? AND created_at > ?",
            (ip, stamp - 3600),
        ).fetchone()[0])
        if recent >= CHALLENGE_PER_IP_HOUR:
            raise KeyLoginError("请求太频繁了，请稍后再试")

    challenge_id = secrets.token_urlsafe(24)
    conn.execute(
        "INSERT INTO admin_challenges(id, created_ip, expires_at, created_at) "
        "VALUES (?, ?, ?, ?)",
        (challenge_id, ip or "", stamp + CHALLENGE_TTL_SECONDS, stamp),
    )
    # 清理。这张表和 captchas 一样是「只增不减」的那类，
    # 不主动删就会一路长下去。
    conn.execute("DELETE FROM admin_challenges WHERE created_at < ?",
                 (stamp - KEEP_CHALLENGE_SECONDS,))
    conn.commit()
    return {"challenge": challenge_id, "expires_in": CHALLENGE_TTL_SECONDS}


def consume_challenge(conn: sqlite3.Connection, challenge_id: str) -> None:
    """校验并**作废**一张挑战。任何不通过都抛 KeyLoginError。

    ！「作废」和「校验」必须在同一个函数里做完（下面一条 UPDATE 就是作废）。
      分成两步的话，迟早会有人只调校验忘了作废 —— 那就是永久可重放的签名。
    """
    value = (challenge_id or "").strip()
    if not value:
        raise KeyLoginError("登录请求不完整，请重新试一次")

    row = conn.execute(
        "SELECT * FROM admin_challenges WHERE id = ?", (value,)
    ).fetchone()
    if row is None:
        raise KeyLoginError("这次登录已经失效了，请重新点一次登录")
    if row["consumed_at"] is not None:
        raise KeyLoginError("这次登录已经用过了，请重新点一次登录")
    if int(row["expires_at"]) < now_ts():
        raise KeyLoginError("登录超时了（超过 2 分钟），请重新点一次登录")

    # 先作废再往下走：后面无论成功还是失败，这张挑战都不能再用第二次
    conn.execute("UPDATE admin_challenges SET consumed_at = ? WHERE id = ?",
                 (now_ts(), value))
    conn.commit()


def check_signature(conn: sqlite3.Connection, challenge_id: str,
                    key_id: str, signature_hex: str) -> dict:
    """核对一个签名。通过就返回那把密钥的记录，否则抛 KeyLoginError。

    ! 所有失败路径给用户的提示**完全一样**。
      区分「没有这把钥匙」和「签名不对」等于告诉对方哪一步猜对了 ——
      而这里本来就什么都不该告诉他。
    """
    generic = "密钥验证失败。请确认选的是正确的那把密钥文件。"

    key = admins.key_find(conn, (key_id or "").strip())
    if key is None:
        raise KeyLoginError(generic)

    try:
        signature = bytes.fromhex((signature_hex or "").strip())
    except ValueError as exc:
        raise KeyLoginError(generic) from exc

    if not adminkeys.verify(
        str(key["public_key"]),
        adminkeys.login_message(challenge_id),
        signature,
    ):
        raise KeyLoginError(generic)

    admin = admins.find_admin_by_id(conn, int(key["admin_id"]))
    if admin is None:
        raise KeyLoginError(generic)
    if (admin.get("status") or "active") != "active":
        # 这条可以说实话：钥匙是对的，只是账号被停用了。
        # 对方已经证明了自己持有私钥，再瞒着没有意义。
        raise KeyLoginError("这个管理员账号已被停用")

    return {"key": key, "admin": admin}


def stray_challenges(conn: sqlite3.Connection) -> int:
    """还没被用掉、但已经过期的挑战数量。后台自检用。"""
    return int(conn.execute(
        "SELECT COUNT(*) FROM admin_challenges WHERE consumed_at IS NULL AND expires_at < ?",
        (now_ts(),),
    ).fetchone()[0])
