"""配置文件加载。

优先从项目根目录的 `.env` 读取配置，没有 `.env` 时继续使用系统环境变量。
对使用者来说，写一个 `.env` 文件比配置系统环境变量友好得多，
而且便于把配置和代码分开管理。

`.env` 里没写的项会保持系统环境变量的值；已存在的系统环境变量不会被 `.env` 覆盖
（与 python-dotenv 的默认行为一致，方便部署时用环境变量临时覆盖）。

依赖说明：`python-dotenv` 由 `uvicorn[standard]` 附带；
即使它没装，下面也内置了一个极简解析器，功能不受影响。
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"

# 与本项目相关的配置项前缀
PREFIX = "MCFR_"


def _fallback_parse(text: str) -> dict[str, str]:
    """内置的极简 .env 解析：KEY=VALUE，支持 # 注释、引号与行内注释。"""
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_env(override: bool = False) -> list[str]:
    """加载 `.env`，返回实际生效的配置项名列表（便于启动时打印自检信息）。

    override=False 表示已存在的系统环境变量优先，不会被 .env 覆盖。
    """
    if not ENV_FILE.exists():
        return []

    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_FILE, override=override, encoding="utf-8")
    except ImportError:
        for key, value in _fallback_parse(ENV_FILE.read_text(encoding="utf-8")).items():
            if override or key not in os.environ:
                os.environ[key] = value

    return sorted(key for key in os.environ if key.startswith(PREFIX))


def env_file_hint() -> str:
    """给用户看的提示语。"""
    if ENV_FILE.exists():
        return f"配置文件：{ENV_FILE.name}"
    return "未找到 .env，当前使用系统环境变量（可复制 .env.example 为 .env 后修改）"


def update_env(values: dict[str, str]) -> None:
    """把键值写回 .env：已存在的行原地替换，缺失的键追加到末尾。

    保留原有注释与顺序，避免把用户手写的配置文件弄乱。
    """
    lines: list[str] = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()

    remaining = dict(values)
    output: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                output.append(f"{key}={remaining.pop(key)}")
                continue
        output.append(line)

    if remaining:
        if output and output[-1].strip():
            output.append("")
        output.append("# 以下配置由管理后台写入")
        for key, value in remaining.items():
            output.append(f"{key}={value}")

    ENV_FILE.write_text("\n".join(output) + "\n", encoding="utf-8")
