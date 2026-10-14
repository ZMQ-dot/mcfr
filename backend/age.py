"""年龄与「成年 / 未成年」的唯一真相源。

## 为什么存出生日期，而不是存「年龄」这个数字

年龄是个**会自己变**的数字。今天存下「14 岁」，明年它还是 14 —— 除非有人
去改它，而「没有人会去改」正是这类字段最容易烂掉的方式。

所以库里存的是**出生年月日**，年龄每次现算。这样「到了生日自动长一岁」
不需要任何定时任务、不需要任何后台批处理，也就没有「任务没跑成」这种故障模式。

## 为什么注册时只让用户填「几岁 + 几月几号」

用户真正知道的、也愿意填的就这两样。出生年份由服务端推出来：

    今天 2026-10-04，填「14 岁、生日 3 月 15 日」
      → 今年 3 月 15 日已经过了，说明 14 岁生日是今年过的
      → 出生年 = 2026 - 14 = 2012

    今天 2026-01-10，填「14 岁、生日 3 月 15 日」
      → 今年生日还没到，说明 14 岁生日是去年过的
      → 出生年 = 2026 - 14 - 1 = 2011

两种情况下算出来的「当前年龄」都是 14，且往后每年生日自动 +1，
和让用户直接填年份完全等价 —— 而注册表单因此少问一个字段。

## ！成年线是 16 岁，不是 18

这是站方定的业务分界。改它等于整个站的人群划分变了，所以
**ADULT_AGE 只在这一个地方定义**，别在别处再写一次 16。
"""

from __future__ import annotations

from datetime import date

# 满 16 岁算成年侧。见上面那段说明 —— 全站唯一的一处定义。
ADULT_AGE = 16

# 注册时的合法年龄范围。下限按小学入学年龄，上限防手滑（把 41 打成 14）。
MIN_AGE = 6
MAX_AGE = 99

# 每个月的天数。2 月按 29 天算 —— 闰年出生的人填 2/29 是合法的，
# 校验时不该被拦；真正换算年龄时下面的元组比较会自动把非闰年的
# 生日落到 3 月 1 日（这是通行做法）。
_MONTH_DAYS = (0, 31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)

# 旧的「年龄区间」下拉里各档的中值。
# ！只服务于 v0.28 及以前注册、还没补填生日的老账号 ——
#   他们登录后会被要求补填，补完这条路径就不再走了。
#   留着它是因为「有人再也没登录过」这种情况会一直存在。
LEGACY_MIDPOINT = {"6-9": 8, "10-12": 11, "13-15": 14, "16-18": 17}


def valid_day(month: int, day: int) -> bool:
    """月份和「几号」是不是一个合法的组合。"""
    if not 1 <= month <= 12:
        return False
    return 1 <= day <= _MONTH_DAYS[month]


def birth_year_from(age: int, month: int, day: int, today: date | None = None) -> int:
    """由「现在几岁 + 生日」反推出出生年份。见模块开头那段推导。"""
    today = today or date.today()
    year = today.year - age
    # 今年的生日还没到 → 这个岁数是去年生日时就到了，出生年再往前退一年
    if (today.month, today.day) < (month, day):
        year -= 1
    return year


def age_on(
    birth_year: int | None,
    month: int | None,
    day: int | None,
    today: date | None = None,
) -> int | None:
    """算出「今天几岁」。没填生日返回 None。

    ！这里刻意用**元组比较**而不是构造 date(y, m, d)：
      2 月 29 日出生的人，在非闰年会直接 ValueError 把接口打挂。
      元组比较天然把它当成「3 月 1 日才过生日」，不需要特判。
    """
    if not birth_year or not month or not day:
        return None
    today = today or date.today()
    years = today.year - int(birth_year)
    if (today.month, today.day) < (int(month), int(day)):
        years -= 1
    return years


def is_adult(age: int | None) -> bool:
    """成年与否。没填年龄的一律按**未成年**处理 —— 宁可严一点。"""
    return age is not None and age >= ADULT_AGE


def label(age: int | None) -> str:
    """对外展示用的一句话。只给岁数，不给月和日 —— 生日是本人的隐私，
    别人只需要知道「大概多大」，够用来判断合不合得来。"""
    return f"{age} 岁" if age is not None else "未填写年龄"


def declared(user) -> bool:
    """这个账号填过生日没有。"""
    return bool(user.get("birth_year") and user.get("birth_month") and user.get("birth_day"))


def resolve_age(user, today: date | None = None) -> int | None:
    """把一行用户记录换算成「现在几岁」。

    出生日期优先；老账号还没补填时退回旧的年龄区间中值 ——
    这样匹配算法只需要处理一条路径，不用到处写 if。
    """
    age = age_on(
        user.get("birth_year"), user.get("birth_month"), user.get("birth_day"), today
    )
    if age is not None:
        return age
    bucket = (user.get("age_group") or "").strip()
    return LEGACY_MIDPOINT.get(bucket)
