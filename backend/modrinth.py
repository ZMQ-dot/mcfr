"""Modrinth 官方 API 的最小客户端。

！只用标准库（urllib）—— 本项目有「零新增依赖」的硬约束，
  requirements.txt 里只有 fastapi 和 uvicorn。为了一个功能把 requests
  或 httpx 引进来不划算，而且 urllib 干这件事完全够用。

！Modrinth 明确要求请求带上能识别出调用方的 User-Agent，匿名 UA 会被限流
  甚至直接拒绝。所以默认带一个能说明来意的，也可以用 MCFR_MODRINTH_UA
  覆盖成带联系方式的（万一流量异常，他们能联系到你，而不是直接封 IP）。

API 文档：https://docs.modrinth.com/api/
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

API_BASE = "https://api.modrinth.com/v2"

# Modrinth 支持的模组加载器。
# ！搜索接口把「加载器」和「分类」混在同一个 categories 数组里返回
#   （"fabric" 和 "adventure" 并列），只能靠这张表区分 ——
#   把分类当成加载器去筛选，筛出来会是空。
LOADERS: tuple[str, ...] = (
    "fabric", "forge", "neoforge", "quilt", "liteloader", "modloader", "rift",
)

# 单个文件的下载上限。热门模组一般几百 KB 到十几 MB，给 64MB 留足余量，
# 同时挡住「万一 URL 被换成别的东西」的情况。
MAX_FILE_BYTES = 64 * 1024 * 1024

# 一次同步最多取多少个模组。需求是前 20，留个上界防止被参数刷爆。
MAX_SYNC_MODS = 50

# ---------------------------------------------------------------- 超时策略
#
# ！这里分三层，别当成一件事：
#     SOCKET_TIMEOUT    每一次 socket 操作（一次 recv）最多等多久
#     REQUEST_BUDGET    一整次请求（含读完整个响应体）最多花多久
#     RESPONSE_LIMIT    响应体最大允许多少字节
#
# ！为什么单靠 urlopen(timeout=…) 不够：
#   那个值卡的是**每一次 socket 操作**。只要对方每隔几秒吐一小块，
#   读就能一直拖下去 —— 实测遇过一次，后果是那条同步任务在后台卡了
#   十几分钟，而 start_job 看到有 running 任务就拒绝新的镜像操作，
#   等于整个镜像功能被一条僵尸任务锁住。
SOCKET_TIMEOUT = 10
REQUEST_BUDGET = 45
RESPONSE_LIMIT = 16 * 1024 * 1024

# 网络抖一下很常见：实测同一个请求会偶发被对方直接重置
# （ConnectionResetError / WinError 10054），隔一秒再试就正常。
# 尤其是「每日自动同步」这种没人看着的任务，一次抖动就把当天的同步废掉太可惜。
RETRY_TIMES = 2
RETRY_BACKOFF = 1.5

# 单个文件的下载总时长上限。64MB 上限的文件在慢网络上要几分钟，
# 给 20 分钟已经很宽松 —— 超过这个时间基本可以断定是卡住了，不是慢。
DOWNLOAD_BUDGET = 20 * 60


class ModrinthError(RuntimeError):
    """调用 Modrinth 失败，携带给用户看的中文提示。"""


class TransientError(ModrinthError):
    """临时的网络抖动，值得重试一次。

    ！和普通 ModrinthError 分开是为了**不要重试确定性的失败**：
      404、401 重试一百次也是同一个结果，白等三秒还占着任务槽位。
      真正值得重试的是连不上、被重置、读超时这一类。
    """


def user_agent() -> str:
    custom = (os.environ.get("MCFR_MODRINTH_UA") or "").strip()
    if custom:
        return custom
    return "mcfr-mod-mirror/1.0 (self-hosted Minecraft community site)"


def parse_timestamp(value: Any) -> int:
    """Modrinth 的时间是 ISO8601，形如 2024-05-01T12:34:56.789Z。

    fromisoformat 在旧版本 Python 上不认结尾的 Z，所以先换掉。
    """
    if not isinstance(value, str) or not value:
        return 0
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp())


# ---------------------------------------------------------------- HTTP

def _url(path: str, params: dict[str, Any] | None = None) -> str:
    full = f"{API_BASE}{path}"
    if params:
        full = f"{full}?{urllib.parse.urlencode(params)}"
    return full


def _read_all(response, budget: float) -> bytes:
    """带**总时长**上限地把响应体读完。

    ！为什么不直接 response.read()：见文件上方 SOCKET_TIMEOUT 那一段的说明。
      一句话：单次操作超时 ≠ 整次请求超时。

    ！顺手把响应体也卡住：把一个几 GB 的响应读进内存，和崩掉没什么区别。
    """
    deadline = time.monotonic() + budget
    chunks: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() > deadline:
            raise TransientError(f"读取 Modrinth 的响应超过 {budget:.0f} 秒，已放弃")
        chunk = response.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > RESPONSE_LIMIT:
            raise ModrinthError(
                f"Modrinth 返回的内容超过 {RESPONSE_LIMIT // 1048576}MB，已放弃"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _get_json_once(path: str, params: dict[str, Any] | None,
                   budget: float) -> Any:
    request = urllib.request.Request(
        _url(path, params),
        headers={"User-Agent": user_agent(), "Accept": "application/json"},
    )

    # ！捕获顺序有讲究：HTTPError 是 URLError 的子类，
    #   URLError 又是 OSError 的子类，具体的一定要排在前面。
    try:
        with urllib.request.urlopen(request, timeout=SOCKET_TIMEOUT) as response:
            raw = _read_all(response, budget)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            # ！429 不重试：它就是在说「你太快了，退下去」。立刻重试只会更糟。
            raise ModrinthError(
                "Modrinth 说请求太频繁了（HTTP 429），过几分钟再试"
            ) from exc
        raise ModrinthError(f"Modrinth 返回 HTTP {exc.code}（{path}）") from exc
    except urllib.error.URLError as exc:
        raise TransientError(f"连不上 Modrinth：{getattr(exc, 'reason', exc)}") from exc
    except (socket.timeout, TimeoutError) as exc:
        raise TransientError(f"访问 Modrinth 超时（{path}）") from exc
    except OSError as exc:
        # ConnectionResetError 就是走这一条 —— 实测最常见的一种抖动
        raise TransientError(f"网络错误：{exc}") from exc

    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModrinthError(f"Modrinth 返回的内容不是合法 JSON（{path}）") from exc


def _get_json(path: str, params: dict[str, Any] | None = None, *,
              timeout: float = REQUEST_BUDGET) -> Any:
    """取一份 JSON。遇到网络抖动会自己重试一次。"""
    last: ModrinthError | None = None
    for attempt in range(RETRY_TIMES):
        try:
            return _get_json_once(path, params, timeout)
        except TransientError as exc:
            last = exc
            if attempt + 1 >= RETRY_TIMES:
                break
            print(f"[mcfr] Modrinth 请求抖动，{RETRY_BACKOFF:.1f} 秒后重试"
                  f"（{path}）：{exc}")
            time.sleep(RETRY_BACKOFF)
    raise last or ModrinthError("请求 Modrinth 失败")


# ---------------------------------------------------------------- 接口

def search_top_mods(limit: int = 20) -> list[dict]:
    """按总下载量倒序取模组。

    只筛 project_type:mod —— 不加这个的话资源包、光影、整合包都会混进来。
    """
    limit = max(1, min(int(limit), MAX_SYNC_MODS))
    data = _get_json(
        "/search",
        {
            "facets": json.dumps([["project_type:mod"]]),
            "index": "downloads",
            "limit": limit,
        },
    )
    hits = data.get("hits") if isinstance(data, dict) else None
    if not isinstance(hits, list):
        raise ModrinthError("搜索接口返回的结构不符合预期")
    return [hit for hit in hits if isinstance(hit, dict)]


def project_versions(project_id: str) -> list[dict]:
    """某个模组的全部版本（含每个版本的主文件信息）。"""
    data = _get_json(f"/project/{urllib.parse.quote(str(project_id))}/version")
    if not isinstance(data, list):
        raise ModrinthError(f"版本接口返回的结构不符合预期（{project_id}）")
    return [item for item in data if isinstance(item, dict)]


def split_loaders(categories: Any) -> list[str]:
    """把搜索结果里混杂的 categories 拆出加载器部分。"""
    if not isinstance(categories, list):
        return []
    return [str(item) for item in categories if str(item) in LOADERS]


def primary_file(version: dict) -> dict | None:
    """取一个版本的主文件。

    一个版本可以挂多个文件（比如 sources jar）；用户要的是标了 primary 的那个，
    没有 primary 时退回第一个。
    """
    files = version.get("files")
    if not isinstance(files, list) or not files:
        return None
    for item in files:
        if isinstance(item, dict) and item.get("primary"):
            return item
    first = files[0]
    return first if isinstance(first, dict) else None


def file_sha1(file_info: dict) -> str:
    hashes = file_info.get("hashes")
    if isinstance(hashes, dict):
        return str(hashes.get("sha1") or "")
    return ""


# ---------------------------------------------------------------- 下载

def download_file(url: str, dest: Path, *, expected_sha1: str = "",
                  limit: int = MAX_FILE_BYTES) -> int:
    """流式下载到 dest，返回字节数。

    ！必须流式。一次性 read() 进内存再判断大小的话，一个被换掉的 URL
      就能直接把进程撑爆 —— 这个服务跑在只有 1-2G 内存的小机器上是常态。

    ！校验 sha1 再改名。Modrinth 每个文件都给了哈希，白给的完整性校验，
      不做的话「下到一半断网」留下半个 jar 会一直骗人。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")

    request = urllib.request.Request(url, headers={"User-Agent": user_agent()})
    digest = hashlib.sha1()
    written = 0
    # ！同样卡总时长，理由和 _read_all 一样：
    #   单次 read 有 60 秒超时，但只要对方慢慢滴，下载就能拖到天荒地老 ——
    #   而一条卡住的下载任务会占着任务槽位，让整个镜像功能做不了别的。
    deadline = time.monotonic() + DOWNLOAD_BUDGET
    try:
        with urllib.request.urlopen(request, timeout=SOCKET_TIMEOUT) as response:
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > limit:
                raise ModrinthError(
                    f"文件 {int(declared) // 1048576}MB 超过上限 {limit // 1048576}MB，已放弃"
                )
            with open(part, "wb") as handle:
                while True:
                    if time.monotonic() > deadline:
                        raise ModrinthError(
                            f"下载超过 {DOWNLOAD_BUDGET // 60} 分钟，已放弃"
                        )
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > limit:
                        raise ModrinthError(
                            f"文件超过上限 {limit // 1048576}MB，已放弃"
                        )
                    digest.update(chunk)
                    handle.write(chunk)
    except urllib.error.HTTPError as exc:
        part.unlink(missing_ok=True)
        raise ModrinthError(f"下载文件失败：HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        part.unlink(missing_ok=True)
        raise ModrinthError(f"下载文件失败：{getattr(exc, 'reason', exc)}") from exc
    except ModrinthError:
        part.unlink(missing_ok=True)
        raise
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise ModrinthError(f"写文件失败：{exc}") from exc

    if expected_sha1 and digest.hexdigest().lower() != expected_sha1.lower():
        part.unlink(missing_ok=True)
        raise ModrinthError("下载回来的文件和 Modrinth 给的校验值对不上，已丢弃")

    part.replace(dest)
    return written
