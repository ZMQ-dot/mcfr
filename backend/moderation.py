"""面向未成年人社区的第一道内容安全防线。

设计依据开题报告「内容安全风险」与「隐私保护」两节：

1. 导流 / 诈骗关键词拦截（免费送皮肤、代充、刷单等常见的未成年人受骗话术）；
2. 手机号防护 —— 这是唯一会阻断提交的个人信息类型；
3. 统一文本规范化，便于后续接入 AI 辅助审核（此处已预留接口）。

关于 QQ / 微信等联系方式：
本站 v0.2 起改为「联系方式受控交换」模式 —— 用户可以把自己愿意公开的
QQ、微信等填写到资料的联系方式栏（`contact_*` 字段），但这些信息
**只有在你逐条同意对方的交换申请之后，才会发送给那个人**。
因此自由文本里的 QQ / 微信不再拦截，仅由界面引导用户填写到专用字段。
手机号风险更高（涉及实名与线下联系），仍然阻断。
"""

from __future__ import annotations

import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
KEYWORD_FILE = BASE_DIR / "data" / "blocked_keywords.txt"

DEFAULT_KEYWORDS: tuple[str, ...] = (
    # 导流 / 诈骗类话术
    "免费送皮肤", "领皮肤", "代充", "充值返利", "低价售卖", "私聊转账",
    "扫码领取", "兼职刷单", "刷单", "贷款", "返利", "中奖了", "点击领取",
    # 外挂 / 破解类
    "外挂", "破解版下载", "防封",
)

PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
QQ_RE = re.compile(r"(?:qq|扣扣|企鹅|🐧)\s*[:：]?\s*[1-9]\d{4,11}", re.IGNORECASE)
WECHAT_RE = re.compile(
    r"(?:微信|vx|wx|v信|weixin)\s*(?:号)?\s*[:：]?\s*[A-Za-z][A-Za-z0-9_-]{4,19}",
    re.IGNORECASE,
)

MENTION_LIMIT = 10


def _load_keywords() -> tuple[str, ...]:
    """优先读取外部词库文件，便于运营期不改代码即可维护。"""
    if not KEYWORD_FILE.exists():
        return DEFAULT_KEYWORDS
    words: list[str] = []
    try:
        for line in KEYWORD_FILE.read_text(encoding="utf-8").splitlines():
            word = line.strip()
            if word and not word.startswith("#"):
                words.append(word)
    except OSError:
        return DEFAULT_KEYWORDS
    return tuple(words) or DEFAULT_KEYWORDS


def normalize(text: str) -> str:
    """统一空白与全角空格，避免用「加 我 微 信」绕过检测。"""
    return re.sub(r"[\s\u3000]+", "", text or "")


def scan_text(text: str, *, field: str = "内容") -> list[str]:
    """扫描文本，返回风险提示列表；空列表代表通过。

    注意：QQ / 微信不在拦截范围内，只有手机号与诈骗话术会被拦下。
    """
    warnings: list[str] = []
    if not text:
        return warnings

    squeezed = normalize(text)
    lowered = squeezed.lower()

    for word in _load_keywords():
        if normalize(word).lower() in lowered:
            warnings.append(f"{field}包含不适宜的词组「{word}」，请修改后再提交")
            break

    if PHONE_RE.search(squeezed):
        warnings.append(f"{field}疑似包含手机号，为保护隐私请不要公开手机号")

    return warnings


def is_clean(text: str, *, field: str = "内容") -> bool:
    return not scan_text(text, field=field)


def has_contact(text: str) -> bool:
    """文本里是否出现联系方式（用于界面提示，不做拦截）。"""
    if not text:
        return False
    return bool(PHONE_RE.search(text) or QQ_RE.search(text) or WECHAT_RE.search(text))


def mask_contacts(text: str) -> str:
    """把联系方式替换为占位符（用于管理后台展示举报内容）。"""
    if not text:
        return text
    masked = PHONE_RE.sub("[已隐藏手机号]", text)
    masked = QQ_RE.sub("[社交账号]", masked)
    masked = WECHAT_RE.sub("[社交账号]", masked)
    return masked
