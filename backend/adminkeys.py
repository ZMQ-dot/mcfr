"""管理后台的密钥对登录：ECDSA P-256（v0.32）。

## 模型

和 SSH 私钥完全一样：

    · 服务器生成一对密钥 → 私钥打包成一个文件给你下载 → 服务器只留**公钥**
    · 登录时选那个文件 → 浏览器用它签一段服务器给的随机数 → 服务器验签
    · 私钥**永远不出浏览器**。发给服务器的只有签名。

! 这一点是它比密码强的地方：服务器上根本没有能冒充你的东西。
  数据库被人拖走也进不了后台 —— 公钥不能反过来推出签名。

## 为什么是「写一套密码学」而不是装个库

这个项目有硬约束：requirements.txt 永远只有 fastapi + uvicorn 两行
（README 里写明了理由：中小学环境装不了带原生扩展的包）。
而 Python 标准库**没有任何签名验签能力**，所以只能自己实现。

! 只实现**验签**，不实现签名 —— 签名在浏览器里由 WebCrypto 完成。
  验签需要的数学比签名少得多，也更容易写对。

## 用了哪些东西

    · 椭圆曲线上的点加 / 倍点 / 标量乘（仿射坐标，最简单的一种）
    · 模逆用 pow(x, -1, p)（Python 3.8+ 自带）
    · 随机数用 secrets，不用 random

! 性能完全不担心：一次验签约 5～15ms，而登录一天也就发生几次。
  所以这里刻意选最好读的写法（仿射 + 一次一点一逆元），
  而不是雅可比坐标那套快但难核对的东西。

## 私钥文件的格式

PKCS#8（DER）再套一层 PEM，也就是 `-----BEGIN PRIVATE KEY-----`。
选它是因为浏览器可以直接吃：

    crypto.subtle.importKey("pkcs8", der, {name: "ECDSA", namedCurve: "P-256"}, ...)

不需要在前端再解析一遍 ASN.1。

## 签名格式

! WebCrypto 的 ECDSA 签名是**裸的 r||s 各 32 字节**（共 64 字节），
  不是常见的 ASN.1 DER 形式。这一点省掉了一大块 DER 解析代码，
  也是这里能这么短的原因之一。两边都是我们自己写的，保持这个约定即可。
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import textwrap

# ---------------------------------------------------------------- 曲线参数
#
# NIST P-256（也叫 secp256r1 / prime256v1）。选它是因为
# WebCrypto 到处都支持，而且签名只有 64 字节。

P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
A = P - 3
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551

G = (GX, GY)

# 未压缩点：0x04 || X(32) || Y(32)，共 65 字节
POINT_BYTES = 65
SIGNATURE_BYTES = 64      # r(32) || s(32)
SCALAR_BYTES = 32

KEY_FILE_SUFFIX = ".mcfrkey"


class KeyError_(RuntimeError):
    """密钥相关的错误。message 可以直接给用户看。"""


# ---------------------------------------------------------------- 曲线运算

def _inverse(value: int, modulus: int) -> int:
    return pow(value % modulus, -1, modulus)


def _point_add(left: tuple[int, int] | None, right: tuple[int, int] | None):
    """椭圆曲线上的加法。None 表示无穷远点（群里的零元）。"""
    if left is None:
        return right
    if right is None:
        return left

    x1, y1 = left
    x2, y2 = right

    if x1 == x2:
        # 同一个 x 只有两种可能：同一个点（倍点），或者互为逆元（结果是无究远点）
        if (y1 + y2) % P == 0:
            return None
        slope = (3 * x1 * x1 + A) * _inverse(2 * y1, P) % P
    else:
        slope = (y2 - y1) * _inverse(x2 - x1, P) % P

    x3 = (slope * slope - x1 - x2) % P
    y3 = (slope * (x1 - x3) - y1) % P
    return (x3, y3)


def _point_mul(scalar: int, point: tuple[int, int]):
    """标量乘。最朴素的双倍-相加，不做任何优化 —— 够快，而且好核对。"""
    result = None
    addend = point
    while scalar:
        if scalar & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        scalar >>= 1
    return result


def _on_curve(x: int, y: int) -> bool:
    return (y * y - (x * x * x + A * x + B)) % P == 0


# ---------------------------------------------------------------- DER / PEM
#
# 只写编码，不写解码 —— 私钥是**我们生成、我们导出**的，
# 不需要读别人给的文件。

OID_EC_PUBLIC_KEY = bytes.fromhex("06072A8648CE3D0201")     # 1.2.840.10045.2.1
OID_PRIME256V1 = bytes.fromhex("06082A8648CE3D030107")      # 1.2.840.10045.3.1.7


def _der_length(size: int) -> bytes:
    if size < 0x80:
        return bytes([size])
    raw = b""
    while size:
        raw = bytes([size & 0xFF]) + raw
        size >>= 8
    return bytes([0x80 | len(raw)]) + raw


def _der(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _der_length(len(body)) + body


def _der_integer(value: int) -> bytes:
    raw = value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")
    # ! 最高位是 1 时要补一个 0x00，否则会被当成负数
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return _der(0x02, raw)


def _der_octet_string(body: bytes) -> bytes:
    return _der(0x04, body)


def _der_bit_string(body: bytes) -> bytes:
    # 前面那个 0x00 是「补了几位」，非字节对齐的场合才用得上，这里是 0
    return _der(0x03, b"\x00" + body)


# ---------------------------------------------------------------- 密钥生成

def _random_scalar() -> int:
    """1 <= d < N。用 secrets，绝不用 random。"""
    return secrets.randbelow(N - 1) + 1


def public_point(private_scalar: int) -> tuple[int, int]:
    point = _point_mul(private_scalar, G)
    if point is None:                                   # pragma: no cover
        raise KeyError_("密钥生成失败，请重试")
    return point


def encode_public(point: tuple[int, int]) -> str:
    """公钥存成 130 个十六进制字符（0x04 || X || Y）。"""
    x, y = point
    return "04" + x.to_bytes(32, "big").hex() + y.to_bytes(32, "big").hex()


def decode_public(text: str) -> tuple[int, int]:
    """把存起来的公钥解析成点，并校验它确实在曲线上。

    ! 这个校验不能省。拿到一个不在曲线上的点去做标量乘，
      在有些实现里会把私钥的某些比特泄露出去（无效曲线攻击）。
      这里虽然用的是固定曲线，但「存进数据库的东西被改过」也是可能的，
      所以照旧验一遍 —— 一次模乘的代价。
    """
    try:
        raw = bytes.fromhex((text or "").strip())
    except ValueError as exc:
        raise KeyError_("公钥格式不对") from exc
    if len(raw) != POINT_BYTES or raw[0] != 0x04:
        raise KeyError_("公钥格式不对")

    x = int.from_bytes(raw[1:33], "big")
    y = int.from_bytes(raw[33:], "big")
    if not (0 < x < P and 0 < y < P) or not _on_curve(x, y):
        raise KeyError_("公钥不在曲线 P-256 上")
    return (x, y)


def key_id(public_hex: str) -> str:
    """密钥的短标识（取公钥哈希前 16 个十六进制字符）。

    只是给人看的标签和查询用的键，不参与任何安全判断 ——
    验签永远拿完整的公钥。
    """
    return hashlib.sha256(public_hex.encode("ascii")).hexdigest()[:16]


def export_pkcs8(private_scalar: int, point: tuple[int, int]) -> str:
    """导出 PKCS#8 PEM 文本，浏览器可以直接 importKey("pkcs8", ...)。"""
    x, y = point
    private_bytes = private_scalar.to_bytes(SCALAR_BYTES, "big")
    public_bytes = b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")

    inner = _der(0x30, (
        _der_integer(1)
        + _der_octet_string(private_bytes)
        # [1] publicKey —— 标准里是可选的，但带上它能让更多解析器满意
        + _der(0xA1, _der_bit_string(public_bytes))
    ))
    algorithm = _der(0x30, OID_EC_PUBLIC_KEY + OID_PRIME256V1)
    pkcs8 = _der(0x30, _der_integer(0) + algorithm + _der_octet_string(inner))

    body = base64.b64encode(pkcs8).decode("ascii")
    return ("-----BEGIN PRIVATE KEY-----\n"
            + "\n".join(textwrap.wrap(body, 64))
            + "\n-----END PRIVATE KEY-----\n")


def generate() -> dict:
    """生成一对新密钥。

    返回的 private_pem **只在这一次出现**，不落库、不记日志。
    调用方必须立刻把它交给用户下载，之后谁也拿不回来。
    """
    scalar = _random_scalar()
    point = public_point(scalar)
    public_hex = encode_public(point)
    return {
        "public_key": public_hex,
        "key_id": key_id(public_hex),
        "private_pem": export_pkcs8(scalar, point),
    }


# ---------------------------------------------------------------- 验签

def login_message(challenge: str) -> bytes:
    """被签名的内容。

    ! 带上固定前缀再签：这样即使以后加了别的签名场景，
      两个场景的签名也不能互相冒用（域分离）。
    """
    return ("mcfr-admin-login:" + (challenge or "")).encode("utf-8")


def verify(public_hex: str, message: bytes, signature: bytes) -> bool:
    """验签。任何异常都返回 False —— 绝不把内部错误抛给调用方。"""
    try:
        qx, qy = decode_public(public_hex)
    except KeyError_:
        return False

    if not signature or len(signature) != SIGNATURE_BYTES:
        return False

    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if not (1 <= r < N and 1 <= s < N):
        return False

    digest = hashlib.sha256(message).digest()
    z = int.from_bytes(digest, "big")
    # 标准要求把哈希右移到不超过 n 的位长；P-256 与 SHA-256 同为 256 位，
    # 实际不会移，但留着这行才对所有曲线都成立。
    excess = len(digest) * 8 - N.bit_length()
    if excess > 0:
        z >>= excess

    try:
        w = _inverse(s, N)
    except ValueError:                                  # pragma: no cover
        return False

    point = _point_add(_point_mul((z * w) % N, G),
                       _point_mul((r * w) % N, (qx, qy)))
    if point is None:
        return False
    return point[0] % N == r
