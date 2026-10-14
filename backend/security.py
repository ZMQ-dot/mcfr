"""口令哈希、登录令牌与服务端签名。

口令用标准库的 PBKDF2-HMAC-SHA256，不引入额外原生依赖，
避免在校园电脑上安装 bcrypt / argon2 时出现编译问题。

图片验证码的答案则用带服务端密钥的 HMAC-SHA256（见 sign_captcha_answer）：
答案只有 4 位、且 5 分钟就过期，用 PBKDF2 跑 20 万次迭代纯属浪费 ——
并且答案本身就不该落库成明文，HMAC 同时解决这两件事。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 200_000
SALT_BYTES = 16
TOKEN_BYTES = 32
SESSION_TTL_SECONDS = 60 * 60 * 24 * 14  # 14 天：用户选了「保持登录」

# 用户**没**选「保持登录」时的有效期（v0.30）。
#
# ！这条不能省。前端的 sessionStorage 只挡得住「关掉浏览器」这一种情况：
#   标签页开着过夜、笔记本合盖再打开、学校机房下班不关机 —— 令牌照样还在。
#   真正让「不保持」生效的是这里：12 小时之后服务端就不认它了。
#
# 12 小时是这么定的：一场游戏、一个下午、一次作业的时间都够用，
# 但「今天放学登录、明天早上再来」会正好过期。
SHORT_SESSION_TTL_SECONDS = 60 * 60 * 12



def hash_password(password: str) -> str:
    """生成 `pbkdf2_sha256$迭代次数$盐$摘要` 格式的口令串。"""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"{ALGORITHM}${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """常数时间比对口令，任何异常都视为校验失败。"""
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$", 3)
        if algorithm != ALGORITHM:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
    except (ValueError, AttributeError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def new_session_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


# 假哈希：格式和 real hash 一模一样，但永远不可能匹配上任何口令。
#
# ！它的唯一用途是在「账号不存在」时拿去跑一次同样开销的校验。
#   否则账号不存在会在 1 毫秒内返回，而口令错误要等 PBKDF2 跑完 20 万次迭代
#   （几十毫秒）—— 这个时间差足够把站里有哪些账号一个个试出来。
#   普通用户登录和管理员登录都要用，所以放在这里共享一份。
DUMMY_HASH = (
    f"{ALGORITHM}${ITERATIONS}$"
    "00000000000000000000000000000000$"
    "0000000000000000000000000000000000000000000000000000000000000000"
)


def pick_avatar_hue(seed: str) -> int:
    """根据用户名稳定地派生一个头像色相，让每位玩家有独一无二的方块头像。"""
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return int(digest[:4], 16) % 360


# ---------------------------------------------------------------- 服务端签名

# 服务端密钥：用于给图片验证码的答案做 HMAC。
# 优先读环境变量 MCFR_CODE_SECRET；没配则持久化到 data/server_secret.key，
# 首次运行自动生成。这样服务重启后，用户手里那张图仍然有效
# （若用进程内随机密钥，重启会让所有人正在填的验证码全部作废）。
def _load_server_secret() -> bytes:
    from_env = os.environ.get("MCFR_CODE_SECRET", "").strip()
    if from_env:
        return from_env.encode("utf-8")

    data_dir = Path(__file__).resolve().parent.parent / "data"
    key_file = data_dir / "server_secret.key"
    # 旧版本这文件叫 code_secret.key，看到就接管过来 ——
    # 不接管也不会出错（验证码都是即用即弃的），只是会白白多留一个文件。
    legacy_file = data_dir / "code_secret.key"
    try:
        if not key_file.exists() and legacy_file.exists():
            legacy_file.rename(key_file)
        if key_file.exists():
            saved = key_file.read_bytes().strip()
            if saved:
                return saved
        secret = secrets.token_bytes(32)
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_bytes(secret)
        return secret
    except OSError:
        # 磁盘不可写时兜底（容器只读挂载等场景）
        return secrets.token_bytes(32)


_SERVER_SECRET = _load_server_secret()


def server_secret() -> bytes:
    return _SERVER_SECRET


def sign_captcha_answer(challenge_id: str, answer: str) -> str:
    """把验证码答案跟凭据 id 绑起来做 HMAC。

    绑 id 是为了防止「同一张图换个 id 接着用」：
    答案本身只有 4 位、才 92 万种，只要拿到过一次哈希就能离线建表比对；
    带上 id 之后，每个 id 的哈希表都得重算一遍，离线撞的成本直接乘上 id 的数量。

    答案统一转大写再去空白 —— 用户把 L 写成小写 l 很正常，
    在人机验证上惩罚这个没有任何意义。
    """
    message = f"captcha:{challenge_id}:{answer.strip().upper()}".encode("utf-8")
    return hmac.new(_SERVER_SECRET, message, hashlib.sha256).hexdigest()
