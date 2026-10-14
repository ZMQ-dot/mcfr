"""自研图片验证码：纯标准库画一张 PNG，不依赖任何图像库，也不依赖任何外部服务。

# 为什么不直接用 reCAPTCHA / hCaptcha

项目的两条硬约束把外链验证码方案直接排除掉了：

  ① **零第三方依赖**：requirements.txt 只有 fastapi 和 uvicorn 两行；
  ② **不用外部 CDN**：CSP 是 `script-src 'self'`，外来脚本会被浏览器拦死。

除了约束，还有一个更要紧的理由：本站用户以未成年人为主。
挂一个第三方验证码，等于**每登录一次就把用户的 IP 交给那家公司**。
自己画则完全在本站闭环 —— 图片是本地算出来的，一个字节都不出服务器。

# 图片是怎么画出来的

没有 Pillow，所以 PNG 也得自己编码（见 `_png()`，PNG 是 zlib 加几个定长块，
标准库的 `zlib` + `struct` 就够）。字符则用一张内置的 5×7 点阵字库
（见 `_FONT`），逐像素放大贴到画布上。

# 字符表为什么少了 0 O 1 I L

这四个（加 Z 里的细节）在小字号点阵里几乎无法区分。
把易混字符从字符表里剔掉，换来的辨识度远比多几个字符的熵值划算 ——
对一个小学生来说，「你这个 0 到底是零还是字母 O」是纯粹的劝退。
剩下 31 个字符、4 位，是 92 万种组合，配上次数的硬限制完全够用。
"""

from __future__ import annotations

import math
import secrets
import sqlite3
import struct
import zlib

from .database import now_ts
from .security import server_secret, sign_captcha_answer

# ---------------------------------------------------------------- 策略参数

CODE_LENGTH = 4                 # 位数
CODE_TTL_SECONDS = 300          # 5 分钟有效（够填完，不够被慢慢暴力试）
CODE_MAX_ATTEMPTS = 5           # 一张图最多试 5 次，超了作废换新的

# 同一 IP 每小时最多领多少张。
# ！这个数字同时是「登录尝试速率的上限」—— 每登一次都要领一张图，
#   所以它直接卡住了撞库的速度。但也不能压得太低：
#   学校机房、宿舍往往共用同一个出口 IP，一节课 30 个学生同时登录
#   就会消耗 30+ 张。40 会误伤，120 既亮出了路，又让脚本每小时
#   只能试 120 次密码。
ISSUE_PER_IP_HOUR = 120
KEEP_SECONDS = 86400            # 过期的记录留一天，够后台「安全记录」看趋势

# 字符表：去掉了 0 O 1 I L 这几个在小字号点阵下分不清的字符
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"

# ---------------------------------------------------------------- 尺寸与配色

_SCALE = 3                      # 每个字号像素放大成 3×3 个像素
_GLYPH_W, _GLYPH_H = 5, 7       # 字库里的字形尺寸
_PX_W, _PX_H = _GLYPH_W * _SCALE, _GLYPH_H * _SCALE
_GAP = 7                        # 字符间距
_MARGIN_X, _MARGIN_Y = 10, 12   # 四周留白（同时也是字符上下抖动的余量）

_BG = (245, 247, 243)           # 云白，跟站点的浅色底一致
_INK = (32, 46, 38)             # 深墨绿，正文色
_NOISE_DOTS = (206, 214, 206)   # 噪点：浅灰绿
_NOISE_INK = (150, 168, 152)    # 压在字上面的深噪点
_CURVES = ((96, 178, 122), (232, 152, 74), (110, 158, 214), (196, 122, 178))


class CaptchaError(RuntimeError):
    """验证码相关失败，携带一句直接给用户看的中文。"""


# ---------------------------------------------------------------- 点阵字库

# 每个字符 5 列 × 7 行，'#' 是亮的像素。
# ！写成一行、用 / 分行，是为了整张表能一眼扫完 —— 加字符不用翻好几屏。
#   想改某个字形，把那一行里的 '#' 挪一挪就行，不需要重新数坐标。
_FONT_SOURCE: dict[str, str] = {
    "2": ".###./#...#/....#/..##./.#.../#..../#####",
    "3": "####./....#/....#/.###./....#/....#/####.",
    "4": "#...#/#...#/#...#/#####/....#/....#/....#",
    "5": "#####/#..../#..../####./....#/....#/####.",
    "6": ".###./#..../#..../####./#...#/#...#/.###.",
    "7": "#####/....#/...#./..#../.#.../.#.../.#...",
    "8": ".###./#...#/#...#/.###./#...#/#...#/.###.",
    "9": ".###./#...#/#...#/.####/....#/....#/.###.",
    "A": ".###./#...#/#...#/#####/#...#/#...#/#...#",
    "B": "####./#...#/#...#/####./#...#/#...#/####.",
    "C": ".###./#...#/#..../#..../#..../#...#/.###.",
    "D": "####./#...#/#...#/#...#/#...#/#...#/####.",
    "E": "#####/#..../#..../####./#..../#..../#####",
    "F": "#####/#..../#..../####./#..../#..../#....",
    "G": ".###./#...#/#..../#.###/#...#/#...#/.###.",
    "H": "#...#/#...#/#...#/#####/#...#/#...#/#...#",
    "J": "..###/...#./...#./...#./...#./#..#./.##..",
    "K": "#...#/#..#./#.#../##.../#.#../#..#./#...#",
    "M": "#...#/##.##/#.#.#/#.#.#/#...#/#...#/#...#",
    "N": "#...#/##..#/#.#.#/#..##/#...#/#...#/#...#",
    "P": "####./#...#/#...#/####./#..../#..../#....",
    "Q": ".###./#...#/#...#/#...#/#.#.#/#..#./.##.#",
    "R": "####./#...#/#...#/####./#.#../#..#./#...#",
    "S": ".####/#..../#..../.###./....#/....#/####.",
    "T": "#####/..#../..#../..#../..#../..#../..#..",
    "U": "#...#/#...#/#...#/#...#/#...#/#...#/.###.",
    "V": "#...#/#...#/#...#/#...#/#...#/.#.#./..#..",
    "W": "#...#/#...#/#...#/#.#.#/#.#.#/##.##/#...#",
    "X": "#...#/#...#/.#.#./..#../.#.#./#...#/#...#",
    "Y": "#...#/#...#/.#.#./..#../..#../..#../..#..",
    "Z": "#####/....#/...#./..#../.#.../#..../#####",
}

_FONT: dict[str, list[str]] = {
    char: rows.split("/") for char, rows in _FONT_SOURCE.items()
}


# ---------------------------------------------------------------- PNG 编码

def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def _png(width: int, height: int, pixels: bytearray) -> bytes:
    """把一段 RGB 像素编码成 PNG。

    真彩（颜色类型 2）、8 位深、逐行过滤器固定 0（None）。
    不做滤波是因为画面本来就小（约 100×44），压缩率差一点无所谓，
    而滤波器实现起来要按行做预测，代码量和出错面都翻好几倍。
    """
    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)                                   # 每行开头是过滤器类型
        raw += pixels[y * stride : (y + 1) * stride]

    return (
        b"\x89PNG\r\n\x1a\n"                            # PNG magic
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )


class _Canvas:
    """一块 RGB 画布。坐标超出范围一律安静忽略 —— 干扰线画到外面很正常。"""

    def __init__(self, width: int, height: int, background: tuple[int, int, int]):
        self.width = width
        self.height = height
        self.rows = bytearray(bytes(background) * (width * height))

    def mix(self, x: int, y: int, color: tuple[int, int, int], alpha: int) -> None:
        """按百分比把 color 混到底色上。alpha 100 = 完全覆盖。"""
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        if alpha >= 100:
            blended = color
        else:
            offset = (y * self.width + x) * 3
            pixel = self.rows
            blended = tuple(
                (pixel[offset + i] * (100 - alpha) + color[i] * alpha) // 100
                for i in range(3)
            )
        offset = (y * self.width + x) * 3
        self.rows[offset : offset + 3] = bytes(blended)

    def pixels(self) -> bytearray:
        return self.rows


# ---------------------------------------------------------------- 画图

def _draw_glyph(canvas: _Canvas, char: str, left: int, top: int,
                color: tuple[int, int, int], alpha: int) -> None:
    rows = _FONT.get(char)
    if rows is None:
        return
    for row_index, row in enumerate(rows):
        for col_index, cell in enumerate(row):
            if cell != "#":
                continue
            x0 = left + col_index * _SCALE
            y0 = top + row_index * _SCALE
            for dy in range(_SCALE):
                for dx in range(_SCALE):
                    canvas.mix(x0 + dx, y0 + dy, color, alpha)


def _draw_curves(canvas: _Canvas, count: int) -> None:
    """画几条正弦干扰线。

    ！它们必须**压在字上面**且有一定宽度 —— 细到看不见的干扰线防不住任何人，
    而真正管用的一点是：把字形切断的线会让自动识别多花很多功夫，
    却又不会难倒一个正在认真看的小学生。
    """
    for index in range(count):
        color = _CURVES[index % len(_CURVES)]
        amplitude = secrets.randbelow(6) + 3
        period = secrets.randbelow(26) + 22
        phase = secrets.randbelow(628) / 100.0
        base = secrets.randbelow(canvas.height - 10) + 5
        alpha = secrets.randbelow(26) + 34          # 34~59，能盖住字但不糊成一片
        for x in range(canvas.width):
            y = base + int(amplitude * math.sin(2 * math.pi * x / period + phase))
            # 画 2 像素宽，让线更结实
            canvas.mix(x, y, color, alpha)
            canvas.mix(x, y + 1, color, max(alpha - 22, 10))


def _draw_dots(canvas: _Canvas, count: int,
               color: tuple[int, int, int], alpha: int) -> None:
    for _ in range(count):
        x = secrets.randbelow(canvas.width)
        y = secrets.randbelow(canvas.height)
        canvas.mix(x, y, color, alpha)


def render(code: str) -> bytes:
    """把 4 个字符画成一张 PNG。"""
    width = _MARGIN_X * 2 + len(code) * _PX_W + (len(code) - 1) * _GAP
    height = _MARGIN_Y * 2 + _PX_H
    canvas = _Canvas(width, height, _BG)

    # 顺序有讲究：浅噪点 → 干扰线 → 字 → 少数深噪点压在最上面。
    # 深噪点放最后，是为了有几颗真的盖在笔画上，而不是只飘在空白处。
    _draw_dots(canvas, width * 2, _NOISE_DOTS, 60)
    _draw_curves(canvas, 3)

    for index, char in enumerate(code):
        left = _MARGIN_X + index * (_PX_W + _GAP)
        # 上下抖动 ±3 像素（留白是 12，抖不出去）
        top = _MARGIN_Y + secrets.randbelow(7) - 3
        _draw_glyph(canvas, char, left, top, _INK, 100)

    _draw_dots(canvas, 40, _NOISE_INK, 45)
    return _png(width, height, canvas.pixels())


# ---------------------------------------------------------------- 生成与校验

def new_code() -> str:
    """生成一个 4 位验证码（密码学安全随机源，不用 random 模块）。"""
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def _purge(conn: sqlite3.Connection, stamp: int) -> None:
    """清掉过期一天的记录。这张表跟原来的 email_verifications 一样只增不减。"""
    conn.execute("DELETE FROM captchas WHERE created_at < ?", (stamp - KEEP_SECONDS,))


def issue(conn: sqlite3.Connection, purpose: str, ip: str) -> tuple[str, bytes]:
    """发一张新的验证码，返回 (凭据 id, PNG 图片字节)。

    ！明文答案只在这个函数内部存在过：落库的是 HMAC，出去的是图片。
      所以调用方**拿不到**答案，也就没机会把它写进日志或响应体。
    """
    stamp = now_ts()

    # ！限流放在最前面：否则这个公开接口就是一台免费的图片生成器。
    if ip:
        recent = int(
            conn.execute(
                "SELECT COUNT(*) FROM captchas WHERE created_ip = ? AND created_at > ?",
                (ip, stamp - 3600),
            ).fetchone()[0]
        )
        if recent >= ISSUE_PER_IP_HOUR:
            raise CaptchaError("当前网络请求验证码太频繁了，请稍后再试")

    code = new_code()
    challenge_id = secrets.token_urlsafe(18)
    conn.execute("DELETE FROM captchas WHERE expires_at < ?", (stamp - KEEP_SECONDS,))
    conn.execute(
        """
        INSERT INTO captchas(id, answer_hash, purpose, created_ip, expires_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            challenge_id,
            sign_captcha_answer(challenge_id, code),
            purpose,
            ip,
            stamp + CODE_TTL_SECONDS,
            stamp,
        ),
    )
    if secrets.randbelow(20) == 0:
        _purge(conn, stamp)
    conn.commit()
    return challenge_id, render(code)


def consume(conn: sqlite3.Connection, challenge_id: str, answer: str, purpose: str) -> None:
    """校验并消费一张验证码。任何不通过都抛 CaptchaError（带一句中文）。

    ！失败时**不**让这张图继续能用：每错一次 attempts 就 +1，
      到了上限直接作废，用户必须换一张。拉长一张图的寿命等于给它更多试错次数。
    """
    answer = (answer or "").strip().upper()
    if not challenge_id:
        raise CaptchaError("请先完成人机验证")
    if not answer:
        raise CaptchaError("请填写图中的字符")

    row = conn.execute(
        "SELECT * FROM captchas WHERE id = ?", (challenge_id,)
    ).fetchone()
    if row is None:
        raise CaptchaError("人机验证已失效，请点图片换一张")
    if row["consumed_at"] is not None:
        raise CaptchaError("这张验证码已经用过了，请换一张")
    if row["purpose"] != purpose:
        raise CaptchaError("人机验证已失效，请换一张")
    if int(row["expires_at"]) < now_ts():
        raise CaptchaError("人机验证已过期，请点图片换一张")
    if int(row["attempts"]) >= CODE_MAX_ATTEMPTS:
        raise CaptchaError("这张验证码错得太多次了，请点图片换一张")

    if not secrets.compare_digest(
        sign_captcha_answer(challenge_id, answer), str(row["answer_hash"])
    ):
        conn.execute(
            "UPDATE captchas SET attempts = attempts + 1 WHERE id = ?", (challenge_id,)
        )
        conn.commit()
        raise CaptchaError("图中的字符不正确，再仔细看看")

    conn.execute(
        "UPDATE captchas SET consumed_at = ? WHERE id = ?", (now_ts(), challenge_id)
    )
    conn.commit()


# ---------------------------------------------------------------- 后台统计

def stats(conn: sqlite3.Connection) -> dict:
    """给后台「安全记录」页签用的汇总。"""
    stamp = now_ts()
    day_ago = stamp - 86400
    hour_ago = stamp - 3600

    def scalar(sql: str, args: tuple = ()) -> int:
        return int(conn.execute(sql, args).fetchone()[0])

    issued_24h = scalar("SELECT COUNT(*) FROM captchas WHERE created_at > ?", (day_ago,))
    passed_24h = scalar(
        "SELECT COUNT(*) FROM captchas WHERE consumed_at > ?", (day_ago,)
    )
    # 「被拒」= 发了但一天过去还没被用掉，且至少错过一次 —— 机器人或刷子
    rejected_24h = scalar(
        "SELECT COUNT(*) FROM captchas WHERE created_at > ? AND consumed_at IS NULL "
        "AND attempts > 0",
        (day_ago,),
    )
    return {
        "issued_24h": issued_24h,
        "passed_24h": passed_24h,
        "rejected_24h": rejected_24h,
        "pass_rate": round(passed_24h * 100 / issued_24h) if issued_24h else 0,
        "issued_hour": scalar("SELECT COUNT(*) FROM captchas WHERE created_at > ?", (hour_ago,)),
    }


def recent(conn: sqlite3.Connection, limit: int) -> list[dict]:
    """最近发出的一批验证码记录（用于后台列表）。"""
    rows = conn.execute(
        """
        SELECT id, purpose, created_ip, attempts, expires_at, consumed_at, created_at
        FROM captchas ORDER BY created_at DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    stamp = now_ts()
    items = []
    for row in rows:
        record = dict(row)
        record["passed"] = record["consumed_at"] is not None
        record["expired"] = int(record["expires_at"]) < stamp
        # 凭据本身没有展示价值，露出去只会让人以为它是可复用的
        record.pop("id", None)
        items.append(record)
    return items


def suspicious_ips(conn: sqlite3.Connection, limit: int = 12) -> list[dict]:
    """一小时内领得最多、或者错得最多的网络地址。

    两个信号含义不同，所以都列出来而不是合成一个分数：
      · 领得多 = 有人在批量刷图（或者只是宿舍共用同一个出口 IP，得自己判断）
      · 错得多 = 有人在硬试，典型的脚本行为
    """
    stamp = now_ts()
    rows = conn.execute(
        """
        SELECT created_ip, COUNT(*) AS issued,
               SUM(CASE WHEN attempts > 0 THEN 1 ELSE 0 END) AS wrong,
               MAX(attempts) AS worst
        FROM captchas
        WHERE created_at > ? AND created_ip != ''
        GROUP BY created_ip
        HAVING issued >= 8 OR wrong >= 5
        ORDER BY wrong DESC, issued DESC
        LIMIT ?
        """,
        (stamp - 3600, limit),
    ).fetchall()
    return [dict(row) for row in rows]
