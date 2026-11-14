"""请求 / 响应数据模型（Pydantic v2）。

同时把「面向未成年人友好」的取值集合集中在这里，
前端下拉框与后端校验共用同一份口径。
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from .age import MAX_AGE, MIN_AGE, valid_day
from .moderation import scan_text

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,16}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

# 图片验证码的答案：只留字母与数字，统一转大写。
# ！在验证码这种地方惩罚大小写没有任何意义 —— 用户把小写的 l 输成大写的 I
#   是常见笔误，而不是攻击。真正的防线是「次数」和「有效期」。
CAPTCHA_RE = re.compile(r"[^A-Za-z0-9]")
CAPTCHA_LENGTH = 4

EDITIONS: tuple[str, ...] = ("java", "bedrock", "both")
# 　遗留：v0.29 起不再让用户选年龄区间，改用真实出生日期（backend/age.py）。
#   这个常量只服务于「老账号还没补填生日」时的年龄估值。
AGE_GROUPS: tuple[str, ...] = ("6-9", "10-12", "13-15", "16-18", "unsaid")

PLAY_STYLES: tuple[str, ...] = (
    "生存", "创造建筑", "红石", "命令方块", "PVP", "服务器管理",
    "模组", "材质光影", "跑酷", "小游戏", "地图制作", "速通",
)
ONLINE_TIMES: tuple[str, ...] = (
    "工作日课后", "周末白天", "周末晚上", "每天都上线", "假期集中玩",
)
LOOKING_FOR: tuple[str, ...] = (
    "联机伙伴", "建筑搭子", "红石大佬", "生存队友",
    "一起做地图", "聊天交朋友", "请教问题",
)

MAX_TAGS = 8
MAX_CONTACT_LENGTH = 60


def _clean_contact(value: str, field_label: str) -> str:
    """联系方式只做去空白与长度限制，不拦截内容。

    v0.2 起允许玩家预置 QQ / 微信等账号，但只会在本人同意
    「联系方式交换申请」后才会发送给对方，所以这里不做关键词过滤。
    """
    return (value or "").strip()[:MAX_CONTACT_LENGTH]


def _clean_tags(values: list[str] | None, field_label: str) -> list[str]:
    result: list[str] = []
    for raw in values or []:
        tag = (raw or "").strip()
        if not tag:
            continue
        if len(tag) > 12:
            raise ValueError(f"{field_label}的单个标签不能超过 12 个字")
        if tag not in result:
            result.append(tag)
    if len(result) > MAX_TAGS:
        raise ValueError(f"{field_label}最多选择 {MAX_TAGS} 项")
    return result


def _guard(text: str, field_label: str) -> str:
    problems = scan_text(text, field=field_label)
    if problems:
        raise ValueError(problems[0])
    return text


def _clean_email(value: str) -> str:
    """邮箱统一小写、去空白，并做格式校验。

    ！从 v0.21 起邮箱降级成**普通资料字段**：不再验证、不再发任何信。
      所以它变成选填 —— 不填返回空串，填了才校格式。
    """
    value = (value or "").strip().lower()
    if not value:
        return ""
    if not EMAIL_RE.match(value) or len(value) > 120:
        raise ValueError("邮箱格式看起来不对，例如 name@example.com（不填也可以）")
    return value


def _clean_captcha(value: str) -> str:
    """人机验证的答案：只留字母数字，最多 8 位（多余的字符肯定是粘贴带进来的）。"""
    return CAPTCHA_RE.sub("", value or "")[:CAPTCHA_LENGTH]


class CaptchaIn(BaseModel):
    """需要过人机验证的接口共用这两个字段。

    拆成一个基类而不是每个模型里各写一遍：注册和登录的校验必须完全一致，
    拄成两份迟早会有一份忘了改。
    """

    captcha_id: str = ""
    captcha_answer: str = ""

    # 「在这台设备上保持登录」（v0.30）。
    #
    # ！默认必须是 False，不能是 True。这个字段决定令牌活 14 天还是 12 小时，
    #   而「记住我」这种事**只能由用户主动选择**。默认 True 等于替所有人做了
    #   选择，而且是在学校机房这种最不该记住的场合。
    #   漏传这个字段的老客户端会退成短会话，是安全的那一侧。
    keep: bool = False

    @field_validator("captcha_id")
    @classmethod
    def check_captcha_id(cls, value: str) -> str:
        return (value or "").strip()[:64]

    @field_validator("captcha_answer")
    @classmethod
    def check_captcha_answer(cls, value: str) -> str:
        return _clean_captcha(value)


class RegisterIn(CaptchaIn):
    username: str
    nickname: str
    password: str
    email: str = ""
    edition: str = "java"
    # 年龄 + 生日（v0.29 起）。用户不知道也不需要知道出生年份，
    # 服务端按 backend/age.py 的推导算出年份再存。
    # ！没有默认值：这两个字段必须真填，给默认值等于允许「没填就注册」。
    age: int
    birth_month: int
    birth_day: int
    region: str = ""
    play_styles: list[str] = Field(default_factory=list)
    online_time: list[str] = Field(default_factory=list)
    looking_for: list[str] = Field(default_factory=list)
    bio: str = ""
    contact_qq: str = ""
    contact_wechat: str = ""
    contact_other: str = ""
    contact_note: str = ""

    @field_validator("username")
    @classmethod
    def check_username(cls, value: str) -> str:
        value = (value or "").strip()
        if not USERNAME_RE.match(value):
            raise ValueError("用户名需为 4-16 位字母、数字或下划线")
        return value

    @field_validator("nickname")
    @classmethod
    def check_nickname(cls, value: str) -> str:
        value = (value or "").strip()
        if not 1 <= len(value) <= 16:
            raise ValueError("游戏昵称需为 1-16 个字符")
        return _guard(value, "昵称")

    @field_validator("password")
    @classmethod
    def check_password(cls, value: str) -> str:
        value = value or ""
        if not 6 <= len(value) <= 64:
            raise ValueError("密码长度需为 6-64 位")
        return value

    @field_validator("email")
    @classmethod
    def check_email(cls, value: str) -> str:
        return _clean_email(value)

    @field_validator("edition")
    @classmethod
    def check_edition(cls, value: str) -> str:
        return value if value in EDITIONS else "java"

    @field_validator("age")
    @classmethod
    def check_age(cls, value: int) -> int:
        if not MIN_AGE <= int(value) <= MAX_AGE:
            raise ValueError(f"年龄请填 {MIN_AGE}-{MAX_AGE} 之间的数字")
        return int(value)

    @field_validator("birth_month", "birth_day")
    @classmethod
    def check_birth_part(cls, value: int) -> int:
        # 具体合法性（比如 2 月 30 日）要两个字段一起看，放到 model_validator 里
        return int(value)

    @model_validator(mode="after")
    def check_birthday(self):
        if not valid_day(self.birth_month, self.birth_day):
            raise ValueError("生日看起来不存在，请再选一次")
        return self

    @field_validator("region")
    @classmethod
    def check_region(cls, value: str) -> str:
        return _guard((value or "").strip()[:20], "地区")

    @field_validator("bio")
    @classmethod
    def check_bio(cls, value: str) -> str:
        return _guard((value or "").strip()[:200], "个人简介")

    @field_validator("play_styles")
    @classmethod
    def check_styles(cls, value: list[str]) -> list[str]:
        return _clean_tags(value, "喜欢的玩法")

    @field_validator("online_time")
    @classmethod
    def check_online(cls, value: list[str]) -> list[str]:
        return _clean_tags(value, "常在线时段")

    @field_validator("looking_for")
    @classmethod
    def check_looking(cls, value: list[str]) -> list[str]:
        return _clean_tags(value, "想找的伙伴")

    @field_validator("contact_qq", "contact_wechat", "contact_other", "contact_note")
    @classmethod
    def check_contacts(cls, value: str) -> str:
        return _clean_contact(value, "联系方式")


class LoginIn(CaptchaIn):
    """密码登录：account 可以是用户名，也可以是自己填过的邮箱。"""

    account: str
    password: str

    @field_validator("account")
    @classmethod
    def check_account(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("请输入用户名或邮箱")
        return value[:120]


class SessionKeepIn(BaseModel):
    """「在这台设备上保持登录」的开关（v0.30）。

    只影响**当前这一个会话**的有效期，不碰这个人的其它设备。
    """

    keep: bool = False


class PasswordChangeIn(BaseModel):
    """登录后修改自己的密码。

    ！这个接口是 v0.21 才补上的：原来改密码只能走「邮箱验证码重置」，
      邮件一取消，用户就彻底没有改密码的途径了 —— 而管理员重置出来的
      临时密码如果不改，就等于永久密码。所以这条路必须自己长出来。
    """

    old_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def check_new_password(cls, value: str) -> str:
        value = value or ""
        if not 6 <= len(value) <= 64:
            raise ValueError("新密码长度需为 6-64 位")
        return value


class ProfileUpdateIn(BaseModel):
    nickname: str | None = None
    bio: str | None = None
    edition: str | None = None
    region: str | None = None
    play_styles: list[str] | None = None
    online_time: list[str] | None = None
    looking_for: list[str] | None = None
    contact_qq: str | None = None
    contact_wechat: str | None = None
    contact_other: str | None = None
    contact_note: str | None = None

    @field_validator("contact_qq", "contact_wechat", "contact_other", "contact_note")
    @classmethod
    def check_contacts(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _clean_contact(value, "联系方式")

    @field_validator("nickname")
    @classmethod
    def check_nickname(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not 1 <= len(value) <= 16:
            raise ValueError("游戏昵称需为 1-16 个字符")
        return _guard(value, "昵称")

    @field_validator("bio")
    @classmethod
    def check_bio(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _guard(value.strip()[:200], "个人简介")

    @field_validator("region")
    @classmethod
    def check_region(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _guard(value.strip()[:20], "地区")

    @field_validator("edition")
    @classmethod
    def check_edition(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value if value in EDITIONS else None

    @field_validator("play_styles")
    @classmethod
    def check_styles(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _clean_tags(value, "喜欢的玩法")

    @field_validator("online_time")
    @classmethod
    def check_online(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _clean_tags(value, "常在线时段")

    @field_validator("looking_for")
    @classmethod
    def check_looking(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _clean_tags(value, "想找的伙伴")


class PrivacyIn(BaseModel):
    allow_contact_request: bool | None = None
    show_online: bool | None = None


class BirthIn(BaseModel):
    """补填年龄 + 生日（v0.29）。

    ！这是一个**只写一次**的接口。老账号（v0.28 及以前注册的）没有生日，
      登录后被强制补填；一旦填过，再调就直接 409。
      之所以不做成「个人资料的一部分」，是因为资料是随时能改的 ——
      而年龄一旦能改，匹配就失去意义了（谁都会把自己改成想匹配的那个岁数）。
    """

    age: int
    birth_month: int
    birth_day: int

    @field_validator("age")
    @classmethod
    def check_age(cls, value: int) -> int:
        if not MIN_AGE <= int(value) <= MAX_AGE:
            raise ValueError(f"年龄请填 {MIN_AGE}-{MAX_AGE} 之间的数字")
        return int(value)

    @field_validator("birth_month", "birth_day")
    @classmethod
    def check_birth_part(cls, value: int) -> int:
        return int(value)

    @model_validator(mode="after")
    def check_birthday(self):
        if not valid_day(self.birth_month, self.birth_day):
            raise ValueError("生日看起来不存在，请再选一次")
        return self


class ContactRequestIn(BaseModel):
    greeting: str = ""

    @field_validator("greeting")
    @classmethod
    def check_greeting(cls, value: str) -> str:
        return _guard((value or "").strip()[:100], "打招呼的话")


class ReportIn(BaseModel):
    target_user: int
    reason: str
    detail: str = ""

    @field_validator("reason")
    @classmethod
    def check_reason(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("请选择举报原因")
        return value[:30]

    @field_validator("detail")
    @classmethod
    def check_detail(cls, value: str) -> str:
        return (value or "").strip()[:300]


class BlockIn(BaseModel):
    target_user: int


# ---------------------------------------------------------------- 帖子（v0.27）
#
# ！这里刻意不写长度 / 类型约束，全部用宽松类型。
#   原因：真正的校验在 backend/posts.py 里，而且它必须【单独存在】——
#   后台批量修复、脚本导入这些不走 HTTP 的路径也要用同一套规则。
#   在这里再写一遍会变成两份会跑偏的真相，而 Pydantic 的
#   英文报错也不如 posts.py 里那句中文提示好懂。
#   所以这层只负责「形状对不对」，值域一律往下扔。

class PostIn(BaseModel):
    title: Any = ""
    blocks: Any = Field(default_factory=list)
    credits: Any = Field(default_factory=list)
    # 发到哪个圈子里（v0.34）。0 / 不传 = 公开发在「帖子」模块。
    # ！同样只在这里放行任何形状，真正的校验（圈子在不在、是不是成员）
    #   在 backend/circles.py 的 postable_circle 里。
    circle_id: Any = 0


class CircleIn(BaseModel):
    """建圈子。约束同样全部在 backend/circles.py 里。"""

    name: Any = ""
    intro: Any = ""
    about: Any = ""
    topic: Any = ""


class PostCommentIn(BaseModel):
    content: Any = ""


class PostShareIn(BaseModel):
    user_ids: Any = Field(default_factory=list)
    note: Any = ""


class NotificationReadIn(BaseModel):
    """标记已读。ids 为空且 all=true 时表示「全部已读」。"""

    ids: Any = None
    all: bool = False


class PostStatusIn(BaseModel):
    """后台下架 / 恢复。status 只接受 published | hidden | deleted。"""

    status: str
    reason: str = ""
