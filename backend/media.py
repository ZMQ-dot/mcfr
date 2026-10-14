"""用户上传的皮肤与头像：校验、落盘、取用。

设计上刻意不用 Pillow 等图像库，也不需要 python-multipart：
    · 头像由【浏览器端 canvas】统一缩放成 256×256 的 PNG 再上传，
      所以服务端只会收到规整的小图，不需要解码图片；
    · 上传走原始请求体（Content-Type: image/png），不搞 multipart 表单。
这样 requirements.txt 里始终只有 fastapi 和 uvicorn 两个依赖。

服务端仍然要把关，因为请求是客户端发的、不能信：
    · 只接受真正的 PNG（校验文件头 + IHDR 块）；
    · 头像最长边不超过 512，皮肤必须是 64×64 或 64×32；
    · 限制字节数，避免有人塞一个 100MB 的文件过来。
"""

from __future__ import annotations

import re
import secrets
import struct
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "data" / "uploads"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

AVATAR_MAX_BYTES = 1024 * 1024   # 1MB
AVATAR_MAX_SIDE = 512
SKIN_MAX_BYTES = 512 * 1024      # 512KB
SKIN_SIZES = {(64, 64), (64, 32)}

# 帖子配图（v0.27 起）。
#
# ！上限比头像宽得多，因为用途完全不同：头像是头像框里的一小块，
#   帖子配图是给人看红石线路、建筑细节、合成图的，缩狠了就看不清。
# ！但仍然坚持 PNG-only + 原始请求体（不引入 Pillow / python-multipart）：
#   游戏截图本身就是 PNG，canvas 重绘也天然产出 PNG，
#   而照片类的大图由前端逐步缩到下面这个字节上限以内（见 compressImage）。
POST_IMAGE_MAX_BYTES = 4 * 1024 * 1024   # 4MB
POST_IMAGE_MAX_SIDE = 1600
# 一篇帖子最多几张图。！不是技术限制，是内容质量限制：
# 面向中小学生的图文帖，超过一屏的图很少有人看完，而每张图都要占磁盘。
POST_IMAGE_MAX_COUNT = 12

# ---------------------------------------------------------------- 附件（v0.28）
#
# 帖子正文里可以挂「图片之外的文件」：存档、整合包、数据包、说明文档。
#
# ！这是一份【白名单】，不是黑名单。两个理由：
#   ① 服务端一律带 Content-Disposition: attachment 发送，所以 .html / .svg
#      这类能在浏览器里跑起来的格式本该无害 —— 但还是不收。
#      全站的安全性不该系在「某一个响应头哪天别被改掉」上。
#   ② 面向中小学生的站，附件就这几种，多收无益。
ATTACHMENT_EXTS: tuple[str, ...] = (
    "zip", "txt", "pdf", "md", "json",
    "mcaddon", "mcpack", "mcworld", "mctemplate",
    "jar", "litemod",
    "png", "jpg", "jpeg", "gif",
)
# ！比图片宽松得多：一张 Minecraft 地图存档动辄十几 MB，
#   而正文配图是给人「看」的、附件是给人「下」的，两者不该用同一个尺子。
ATTACHMENT_MAX_BYTES = 32 * 1024 * 1024
# 展示用的原始文件名上限。中文名按字符算，不按字节。
ATTACHMENT_NAME_MAX = 60
# 落盘用的扩展名得能出现在文件名里，所以要窄
ATTACHMENT_STORE_RE = re.compile(r"^[0-9a-f]{32}\.[a-z0-9]{1,12}$")

# 落盘的文件名一律是自己生成的 32 位十六进制 + .png，
# 这样即使有人拿文件名来做路径穿越也无从下手（另有 _safe_name 再兜一层）。
FILENAME_RE = re.compile(r"^[0-9a-f]{32}\.png$")


class MediaError(RuntimeError):
    """上传校验失败。message 可以直接展示给用户。"""


def _png_size(data: bytes) -> tuple[int, int] | None:
    """从 PNG 文件头读出宽高；不是合法 PNG 就返回 None。"""
    if len(data) < 24 or data[:8] != PNG_MAGIC:
        return None
    if data[12:16] != b"IHDR":
        return None
    try:
        width, height = struct.unpack(">II", data[16:24])
    except struct.error:
        return None
    if width == 0 or height == 0:
        return None
    return width, height


def _store(data: bytes) -> str:
    """写盘并返回文件名。"""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_hex(16)}.png"
    (UPLOAD_DIR / name).write_bytes(data)
    return name


def save_avatar(data: bytes) -> str:
    if not data:
        raise MediaError("没有收到图片内容，请重新选择一张图片。")
    if len(data) > AVATAR_MAX_BYTES:
        raise MediaError(f"头像文件太大了（上限 {AVATAR_MAX_BYTES // 1024}KB），换一张小一点的吧。")

    size = _png_size(data)
    if size is None:
        raise MediaError("头像必须是 PNG 图片。换一张图片，或用截图工具另存为 PNG 再试。")
    width, height = size
    if width > AVATAR_MAX_SIDE or height > AVATAR_MAX_SIDE:
        raise MediaError(f"头像尺寸不能超过 {AVATAR_MAX_SIDE}×{AVATAR_MAX_SIDE} 像素。")

    return _store(data)


def save_skin(data: bytes) -> str:
    if not data:
        raise MediaError("没有收到图片内容，请重新选择皮肤文件。")
    if len(data) > SKIN_MAX_BYTES:
        raise MediaError(f"皮肤文件太大了（上限 {SKIN_MAX_BYTES // 1024}KB）。")

    size = _png_size(data)
    if size is None:
        raise MediaError("皮肤必须是 PNG 图片。Minecraft 的皮肤文件本身就是 PNG 格式。")
    if size not in SKIN_SIZES:
        raise MediaError(
            f"皮肤的尺寸必须是 64×64（1.8 及以后）或 64×32（旧版），"
            f"你上传的是 {size[0]}×{size[1]}。"
        )

    return _store(data)


# 帖子配图（v0.27 起）。校验规则和头像同一套，只是上限放宽。
def save_post_image(data: bytes) -> tuple[str, int, int]:
    """保存一张帖子配图，返回 (文件名, 宽, 高)。

    ！上限比头像宽得多，因为用途完全不同：头像是头像框里的一小块，
      帖子配图是给人看红石线路、建筑细节、合成图的，缩狠了就看不清。
    ！但仍然坚持 PNG-only + 原始请求体（不引入 Pillow / python-multipart）：
      游戏截图本身就是 PNG，canvas 重绘也天然产出 PNG，
      而照片类的大图由前端逐步缩到 POST_IMAGE_MAX_BYTES 以内。
    """
    if not data:
        raise MediaError("没有收到图片内容，请重新选择一张图片。")
    if len(data) > POST_IMAGE_MAX_BYTES:
        raise MediaError(
            f"这张图太大了（上限 {POST_IMAGE_MAX_BYTES // 1024 // 1024}MB），"
            "换一张小一点的，或者先用截图工具裁掉多余的部分。"
        )

    size = _png_size(data)
    if size is None:
        raise MediaError("帖子里的图片必须是 PNG。换一张图片，或另存为 PNG 再试。")
    width, height = size
    if width > POST_IMAGE_MAX_SIDE or height > POST_IMAGE_MAX_SIDE:
        raise MediaError(
            f"图片尺寸不能超过 {POST_IMAGE_MAX_SIDE}×{POST_IMAGE_MAX_SIDE} 像素，"
            f"你上传的是 {width}×{height}。"
        )

    return _store(data), width, height


# 披风贴图是 64×32 —— 和旧版皮肤的尺寸一样，但用途不同，单独校验一次。
# 上限给得比皮肤小：披风只有 10×16 个像素是实际内容，64×32 的 PNG 撑死几十 KB。
CAPE_SIZE = (64, 32)
CAPE_MAX_BYTES = 256 * 1024


# ---------------------------------------------------------------- 附件工具

def safe_display_name(raw: str) -> str:
    """把用户给的文件名收拾成可以安全展示的样子。

    ！只用于展示和 Content-Disposition，不参与落盘路径 ——
      落盘名永远是自已生成的随机串，所以这里不用担心路径穿越。
      但要挡掉控制字符和引号：它们能把 Content-Disposition 那个响应头搞坏
      （头部里插一个换行就能塞进第二个头）。
    """
    name = str(raw or "").strip().replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ord(ch) >= 32 and ord(ch) != 127)
    name = name.replace('"', "").replace("'", "").strip()
    return (name or "附件")[:ATTACHMENT_NAME_MAX]


def extension_of(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def save_attachment(data: bytes, raw_name: str) -> tuple[str, str, int]:
    """保存一个附件，返回 (落盘文件名, 展示用文件名, 字节数)。"""
    display = safe_display_name(raw_name)
    ext = extension_of(display)
    if ext not in ATTACHMENT_EXTS:
        raise MediaError(
            f"不支持这种文件（.{ext or '无扩展名'}）。"
            f"可以上传：{'、'.join('.' + item for item in ATTACHMENT_EXTS)}"
        )
    if not data:
        raise MediaError("没有收到文件内容，请重新选择。")
    if len(data) > ATTACHMENT_MAX_BYTES:
        raise MediaError(
            f"文件太大了（上限 {ATTACHMENT_MAX_BYTES // 1024 // 1024}MB）。"
        )
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stored = f"{secrets.token_hex(16)}.{ext}"
    (UPLOAD_DIR / stored).write_bytes(data)
    return stored, display, len(data)


def attachment_path(filename: str) -> Path | None:
    safe = (filename or "").strip()
    if not ATTACHMENT_STORE_RE.match(safe):
        return None
    path = UPLOAD_DIR / safe
    return path if path.is_file() else None


def remove_any(filename: str) -> None:
    """删掉一个上传文件，图片和附件都认。

    ！清理路径只能用它，不能用 remove()：那个只认 .png，
      拿附件名去调它会静默什么都不做，于是磁盘上攒下一堆删不掉的孤儿。
    """
    path = path_of(filename) or attachment_path(filename)
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass
CAPE_SIZE = (64, 32)
CAPE_MAX_BYTES = 256 * 1024


def save_cape(data: bytes) -> str:
    """保存从微软账号取回的披风贴图。

    ！和 save_skin 分开、而不是复用：两者尺寸要求不同，
      而且这里的输入来源是外部接口而不是用户上传，
      报错文案要能区分「你的文件不对」和「微软给的数据不对」。
    """
    if not data:
        return ""
    if len(data) > CAPE_MAX_BYTES:
        raise MediaError("披风图片过大，已放弃。")

    size = _png_size(data)
    if size is None:
        raise MediaError("披风贴图不是有效的 PNG。")
    if size != CAPE_SIZE:
        raise MediaError(f"披风贴图应为 64×32，实际是 {size[0]}×{size[1]}。")

    return _store(data)


def _safe_name(filename: str) -> str:
    """只放行我们自己生成的文件名，挡掉 ../ 之类的路径穿越。"""
    name = (filename or "").strip()
    return name if FILENAME_RE.match(name) else ""


def path_of(filename: str) -> Path | None:
    safe = _safe_name(filename)
    if not safe:
        return None
    path = UPLOAD_DIR / safe
    return path if path.is_file() else None


def remove(filename: str) -> None:
    """删掉一个已存在的上传文件（文件不在也无所谓）。"""
    path = path_of(filename)
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass  # 删不掉不影响主流程，最多留下一个孤儿文件


def url_of(filename: str) -> str:
    """对外可访问的地址；没有上传过就返回空串。"""
    return f"/api/media/{filename}" if _safe_name(filename) else ""
