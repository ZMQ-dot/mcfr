#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""i18n 覆盖率检查 —— 谁没翻译，一眼看清。

    .\\.venv\\Scripts\\python.exe check_i18n.py

## 为什么需要它

本项目用 gettext 式做法：**简体原文就是 key**。好处是漏译只会退回中文，
永远不会露出 `auth.login.title` 这种鬼东西；坏处是**漏译不会报错**。
一个页面切到日文只翻了一半，看上去"也没崩"，没人会发现。

所以这个脚本干三件事：

1. 从 HTML 的 `data-i18n*` 属性和 JS 的 `t("...")` 调用里，收集**所有已经接键的原文**；
2. 打开三份字典，看每条原文有没有对应译文；
3. 顺便查 `data-i18n-html` 的**标签是否对得上** —— 这是唯一会真正"坏掉"的情况：
   译文里少了 `<span class="iris-text">`，那台词的彩色高亮就没了。

## 退出码

0 = 全绿；1 = 有漏译或缺标签。可以直接挂到 CI 上。

## 不做的事

不检查"翻译得好不好"，也不扫描没接键的中文（那种字符串**本来就该是中文**，
扫出来几万条全是噪音）。想知道还有多少没接键，看 `--todo`。
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"

LANGS = ("zh-Hant", "en", "ja")

# 这些中文串确实在源码里，但**不该**进字典。每一条都有理由，
# 写清楚是为了下次有人看到时知道能不能删。
IGNORE = {
    # 首页大标题拆成三段的残留。真正接键的是 data-i18n-html 那一整条，
    # 这几段只是 <h1> 里给无 JS 情况看的兜底正文，单独翻没有意义。
    "一起", "造世界", "的人", "找到能",
    # console 里给开发者看的，不面向用户。
    "[mcfr] 没有对应的处理分支：",
    # 主题按钮的 title 是拼出来的（“主题：” + 状态 + “，点击切换到” + 下一个）。
    # 按整句翻，见字典里的 “主题：{}，点击切换到{}” 那条模式。
    "主题：", "，点击切换到", "· 点击切换到",
    # 万 / 亿：这不是文案问题，是数字换算问题。
    # 英文要用 B / M，光翻后缀只会得到 “3.2 0K” 这种错数字，必须改代码。
    "{} 万", "{} 亿",
    # 天光里的时段名（js/daylight.js 的 label()）。它们**不进界面**，
    # 只在控制台里给人看：McfrDaylight.label() / preview() 调试用。
    # 真要显示到页面上，先把这九条从名单里删掉，检查器立刻会要求补齐译文。
    "深夜", "黎明", "早雾", "上午", "中午", "下午", "黄昏", "蓝调", "夜晚",
}

# 字典文件。zh 是空的 —— 原文即译文，不需要字典。
LOCALE_FILES = {
    "zh-Hant": FRONTEND / "js" / "locale.zh-Hant.js",
    "en": FRONTEND / "js" / "locale.en.js",
    "ja": FRONTEND / "js" / "locale.ja.js",
}

# 要扫的"接键点"
HTML_FILES = [FRONTEND / "index.html", FRONTEND / "admin.html"]
JS_FILES: list[Path] = []

# data-i18n 一族。顺序无所谓，用 finditer 一次扫完。
ATTR_RE = re.compile(
    r"data-i18n(?:-html|-placeholder|-title|-aria-label|-value|-alt)?"
    r"\s*=\s*\"([^\"]*)\""
)

# JS 里的 t("...") / t('...')。故意只认**纯字面量**：
# 拼出来的 key（t("共 " + n + " 条")）检查器看不见，那种一律写成带占位符的整句
# —— 顺带也是翻译质量的要求，碎片拼接的句子在日语里语序会散架。
T_CALL_RE = re.compile(r"(?<![\w.])t\(\s*([\"'])(.*?)(?<!\\)\1", re.DOTALL)

# 标签比对用。只关心开闭标签名，属性值（class 之类）也一起比，
# 因为 iris-text 这个 class 本身就是语义。
TAG_RE = re.compile(r"</?\s*([a-zA-Z][\w-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)/?>")


# --------------------------------------------------------------------------
# JS 字面量扫描
# --------------------------------------------------------------------------

def scan_js_object_keys(text: str) -> list[str]:
    """从 `McfrI18n.add("xx", { ... })` 里取出**顶层**的 key。

    为什么不用正则：key 里有 `\"` 转义，也有跨行的长句子，
    正则要么漏要么错。这里就老老实实按字符串扫一遍 —— 六十行，够用一辈子。

    刻意不管嵌套对象（字典是平铺的），遇到 `{` 里的 `{` 就跳过整段。
    """
    start = text.find("add(")
    if start < 0:
        return []
    brace = text.find("{", start)
    if brace < 0:
        return []

    keys: list[str] = []
    i = brace + 1
    depth = 0
    n = len(text)
    pending: str | None = None   # 刚读完的字符串，等着看后面是不是冒号

    while i < n:
        ch = text[i]

        if ch == "{" and depth == 0 and pending is None:
            depth += 1
            i += 1
            continue

        if depth == 0 and ch in "\"'":
            # 读一个字符串字面量
            quote = ch
            i += 1
            buf: list[str] = []
            while i < n and text[i] != quote:
                if text[i] == "\\":
                    buf.append(text[i:i + 2])
                    i += 2
                    continue
                buf.append(text[i])
                i += 1
            i += 1  # 收尾引号
            raw = "".join(buf)
            pending = unescape_js(raw)
            continue

        if depth == 0 and ch == ":" and pending is not None:
            keys.append(pending)
            pending = None
            i += 1
            continue

        if depth == 0 and ch in ",":
            pending = None
            i += 1
            continue

        if depth > 0:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
            continue

        if ch == "}" and depth == 0:
            break

        i += 1

    return keys


def unescape_js(raw: str) -> str:
    """把 JS 字符串里的转义还原成真实字符，好和 HTML/源码里的原文对齐。"""
    out: list[str] = []
    i = 0
    while i < len(raw):
        if raw[i] == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            out.append({
                "n": "\n", "t": "\t", "r": "\r", "\\": "\\",
                "\"": "\"", "'": "'", "`": "`", "/": "/",
            }.get(nxt, nxt))
            i += 2
            continue
        out.append(raw[i])
        i += 1
    return "".join(out)


def load_locale(path: Path) -> list[str]:
    if not path.exists():
        return []
    return scan_js_object_keys(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 收集"已接键的原文"
# --------------------------------------------------------------------------

def collect_from_html(path: Path) -> set[str]:
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8")
    found = set()
    for m in ATTR_RE.finditer(text):
        value = html.unescape(m.group(1))
        if value:
            found.add(value)
    return found


def collect_from_js(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    found = set()
    for m in T_CALL_RE.finditer(text):
        value = unescape_js(m.group(2))
        # 只收中文开头的 —— t() 也用来查英文 key（比如接口回传的枚举）
        if value:
            found.add(value)
    return found


def js_files() -> list[Path]:
    """扫 frontend/js 下所有业务脚本，但跳过字典自己和 i18n 机制本体。"""
    skip = {"i18n.js"} | {p.name for p in LOCALE_FILES.values()}
    return sorted(
        p for p in (FRONTEND / "js").glob("*.js")
        if p.name not in skip and not p.name.endswith(".min.js")
    )


# --------------------------------------------------------------------------
# 标签一致性
# --------------------------------------------------------------------------

def normalize_placeholder(text: str) -> str:
    """`已复制：${text}` → `已复制：{}`。

    源码里写的是模板字符串，字典里写的是 {}。两边的写法必须归一，
    否则检查器会永远报告"这条没翻译"，而实际上早就翻了。
    """
    return re.sub(r"\$\{[^}]*\}", "{}", text)


# 完整标签（含属性）。
# ！不能用简单的 <[^>]*> —— data-i18n-html 的值里就带着 <br>、<span>，
#   尖括号一多就会被截断，然后把属性值当成正文报成漏译。
#   这里跟 HTML 一样，先看到引号就把整段引号吞下。
TAG_FULL_RE = re.compile(r"<[^>\"']*(?:\"[^\"]*\"[^>\"']*|'[^']*'[^>\"']*)*>")


def tag_signature(text: str) -> list[str]:
    """把一段带标记的文案压成"标签骨架"，用来比对译文有没有改坏结构。

    `<span class="iris-text">造世界</span>` → ['span.iris-text', '/span']
    文字全丢掉，只留标签。
    """
    out = []
    for m in TAG_RE.finditer(text):
        name = m.group(1).lower()
        if name in ("br", "wbr"):
            out.append(name)
            continue
        attrs = m.group(2) or ""
        classes = re.search(r"class\s*=\s*\"([^\"]*)\"", attrs)
        if classes:
            cls = ".".join(sorted(classes.group(1).split()))
            name = f"{name}.{cls}"
        out.append("/" + name if m.group(0).startswith("</") else name)
    return out


# --------------------------------------------------------------------------
# 没接键的存量（--todo）
# --------------------------------------------------------------------------

# 中文串（先跳过纯 ASCII/数字/符号）
CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# 明显不该翻译的东西
NOISE_MARKERS = (
    "console.", "//", "/*", "* ", "data:", "@media", "::", "px", "rgba(",
    "mcfr", "Minecraft", "Modrinth", "API", "Token", "token",
)


def scan_unkeyed(path: Path) -> list[tuple[int, str]]:
    """粗扫：这行里有没有中文，但整行没有任何 data-i18n / t(。"""
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not CJK_RE.search(line):
            continue
        if "data-i18n" in line or re.search(r"(?<![\w.])t\(", line):
            continue
        stripped = line.strip()
        if stripped.startswith(("//", "*", "/*", "<!--", "#")):
            continue
        if any(m in line for m in NOISE_MARKERS):
            continue
        hits.append((lineno, stripped[:110]))
    return hits


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def main() -> int:
    # Windows 控制台默认 GBK，打印 ✔ 会直接 UnicodeEncodeError 崩掉。
    # 重定向到文件时同理。这里统一按 UTF-8 输出。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="i18n 覆盖率检查")
    ap.add_argument("--todo", action="store_true",
                    help="额外列出还没接键的中文（存量清单）")
    ap.add_argument("--stale", action="store_true",
                    help="列出字典里多余、源码里找不到的条目。默认不报 —— "
                         "多余的条目不会出错，只会占几行，"
                         "天天唠叨反而让人忽略真正的漏译。")
    ap.add_argument("--lang", choices=LANGS, help="只看某一种语言")
    ap.add_argument("--out", type=Path,
                    help="把报告写成 UTF-8 文件。PowerShell 的 `>` 会写成 UTF-16，"
                         "中文在别的工具里就成乱码了 —— 用这个避开。")
    ap.add_argument("--extract", metavar="LANG", choices=LANGS,
                    help="抓出源码里所有中文字面量，生成待翻译清单（骨架）")
    args = ap.parse_args()

    if args.extract:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
        return run_extract(args.extract, args.out)

    if args.out:
        # 整体改道：后面照常 print 即可
        sys.stdout = args.out.open("w", encoding="utf-8", newline="\n")

    wanted: set[str] = set()
    for p in HTML_FILES:
        wanted |= collect_from_html(p)
    for p in js_files():
        wanted |= collect_from_js(p)

    # 主站（登录之后的界面）不接键，而是渲染后扫 DOM。那些文案要从源码里抓，
    # 否则检查器会当成"字典里多余"全报出来。
    live = [p for p in HTML_FILES + js_files() if "admin" not in p.name and p.exists()]
    for text in extract_literals(live):
        wanted.add(normalize_placeholder(text))

    langs = (args.lang,) if args.lang else LANGS
    dicts = {lg: load_locale(LOCALE_FILES[lg]) for lg in langs}
    index = {lg: {k: i for i, k in enumerate(dicts[lg])} for lg in langs}

    print("mcfr i18n 检查")
    print("=" * 68)
    print(f"已接键原文: {len(wanted)} 条")
    for lg in langs:
        path = LOCALE_FILES[lg]
        mark = "" if path.exists() else "  (文件不存在)"
        print(f"  {lg:<8} 字典 {len(dicts[lg]):>4} 条{mark}")
    print()

    total_missing = 0
    total_tag = 0

    for lg in langs:
        keys = index[lg]
        missing = sorted(k for k in wanted if k not in keys and k not in IGNORE)
        stale = sorted(k for k in keys if k not in wanted)

        # 标签一致性：只查带标记的原文
        tag_bad = []
        for k in wanted:
            if k not in keys:
                continue
            want_sig = tag_signature(k)
            if not want_sig:
                continue
            # 译文按位置取回
            got_sig = tag_signature(dicts[lg][keys[k]])
            if want_sig != got_sig:
                tag_bad.append((k, want_sig, got_sig))

        covered = len(wanted) - len(missing) - len([k for k in wanted if k in IGNORE])
        pct = (covered / len(wanted) * 100) if wanted else 100.0
        print(f"── {lg} ──  {covered}/{len(wanted)}  ({pct:.0f}%)")

        if missing:
            total_missing += len(missing)
            print(f"  漏译 {len(missing)} 条:")
            for k in missing:
                print(f"    · {short(k)}")
        else:
            print("  漏译: 无 ✔")

        if tag_bad:
            total_tag += len(tag_bad)
            print(f"  标记对不上 {len(tag_bad)} 条（译文里少了/多了标签，会影响排版）:")
            for k, w, g in tag_bad:
                print(f"    · {short(k)}")
                print(f"        原文 {w}")
                print(f"        译文 {g}")
        elif any(tag_signature(k) for k in wanted):
            print("  标记: 一致 ✔")

        if stale and args.stale:
            print(f"  多余 {len(stale)} 条（字典里有、源码里找不到，多半是改过原文）:")
            for k in stale:
                print(f"    · {short(k)}")
        print()

    if args.todo:
        print("── 还没接键的中文（存量，接一条少一条）──")
        print()
        for p in HTML_FILES + js_files():
            hits = scan_unkeyed(p)
            if not hits:
                continue
            print(f"{p.relative_to(ROOT)}  {len(hits)} 处")
            for lineno, line in hits[:40]:
                print(f"  {lineno:>5}  {line}")
            if len(hits) > 40:
                print(f"  ... 还有 {len(hits) - 40} 处")
            print()

    print("=" * 68)
    if total_missing or total_tag:
        print(f"结论: 漏译 {total_missing} 条，标记错误 {total_tag} 条")
        return 1
    print("结论: 全部通过 ✔")
    return 0


def short(text: str, limit: int = 58) -> str:
    flat = text.replace("\n", " ")
    return flat if len(flat) <= limit else flat[:limit] + "…"


# --------------------------------------------------------------------------
# --extract：把所有中文字面量抓出来，生成字典骨架
# --------------------------------------------------------------------------

# JS 里任意一种引号包围的字符串，内容含中文。
# ！分隔符本身必须排除在外，否则 `a="接收…"` 这种会从上一个引号
#   一路匹配到下面一行，把两句话粘成一句。
JS_STR_RE = re.compile(
    r"(?<![\w$])([\"'`])((?:(?!\1)[^\\\n]|\\.)*?[\u4e00-\u9fff](?:(?!\1)[^\\\n]|\\.)*?)\1"
)

# 明显是"给机器看的"字符串，不算文案
SKIP_PREFIX = (
    "http", "/api/", "data-", "#", ".", "px", "var(", "--", "rgba(", "hsl(",
    "Bearer", "application/", "text/", "image/", "sha256-", "Mozilla",
)
SKIP_CONTAINS = (
    "=>", "===", "!==", "function", "return ", "class=", "<span", "<div",
    "@media", "!important", "cubic-bezier", "translate", "url(",
)


def looks_like_copy(text: str) -> bool:
    """判断这个字符串像不像"给人看的话"。"""
    t = text.strip()
    if len(t) < 2 or len(t) > 220:
        return False
    if not CJK_RE.search(t):
        return False
    if t.startswith(SKIP_PREFIX):
        return False
    if any(mark in t for mark in SKIP_CONTAINS):
        return False
    # 中文占比：先摘掉 ${变量} 和 HTML 标签再算。
    # ！不摘的话 `已复制：${text}` 会被算成"中文只占 23%"而当成代码丢掉 ——
    #   而这恰恰是最需要翻译的一类（弹出来的提示）。
    #   data-i18n-html 的值同理：里面裹着 <span class="...">，不摘就只剩下 20%。
    plain = re.sub(r"\$\{[^}]*\}", "", t)
    plain = re.sub(r"<[^>]+>", "", plain)
    if not plain:
        return False
    cjk = len(CJK_RE.findall(plain))
    if cjk / len(plain) < 0.35:
        return False
    return True


def extract_literals(paths: list[Path]) -> dict[str, list[str]]:
    """返回 { 原文: [出处, ...] }，按首次出现顺序排列。"""
    found: dict[str, list[str]] = {}

    def add(text: str, where: str) -> None:
        found.setdefault(text, []).append(where)

    for path in paths:
        where = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")

        if path.suffix == ".html":
            attrs = set()
            for m in ATTR_RE.finditer(text):
                value = html.unescape(m.group(1)).strip()
                if looks_like_copy(value):
                    add(value, where)
                    attrs.add(re.sub(r"\s+", "", value))

            # 正文文字。先把 data-i18n 系列属性整个删掉再切标签 ——
            # 它们的值是同一句话（只是没换行），留着就会被当成两条不同的原文，
            # 白白报一个"漏译"。而且 data-i18n-html 的值里带 <br>，
            # 用正则切标签一定会被截断。
            # ！属性值里的引号都是 &quot;，所以 [^"]* 能安全地吃掉整段值。
            body = re.sub(
                r"data-i18n(?:-html|-placeholder|-title|-aria-label|-value|-alt)?"
                r"\s*=\s*\"[^\"]*\"", "", text)
            for chunk in TAG_FULL_RE.split(body):
                value = re.sub(r"\s+", " ", html.unescape(chunk)).strip()
                if not looks_like_copy(value):
                    continue
                # ！这一段和某个 data-i18n 属性说的是同一句话（浏览器里就是同一处文字），
                #   跳过。判断时两边都去掉所有空白：源码里为了断行会加缩进，
                #   而属性值写在一行 —— 不比空白就会当成两条不同的原文。
                flat = re.sub(r"\s+", "", value)
                if any(flat in attr for attr in attrs):
                    continue
                add(value, where)
            continue

        for m in JS_STR_RE.finditer(text):
            value = unescape_js(m.group(2)).strip()
            if looks_like_copy(value):
                add(value, where)

    return found


def write_skeleton(lang: str, entries: dict[str, list[str]], out: Path) -> None:
    lines = [
        f"/* mcfr · 字典骨架（{lang}）",
        "   ============================================================",
        "   把右侧的 \"\" 填成译文，整段贴回 frontend/js/locale.<lang>.js 的",
        "   McfrI18n.add(...) 里即可。左侧**一个字都不要改** —— 它是原文，",
        "   也是查找用的 key。",
        "",
        "   带 ${...} 的条目是动态拼接的句子：翻译时要保留 ${变量名} 原样。",
        "   ！以下内容由 check_i18n.py --extract 自动抓取，含少量误抓，",
        "     正式写进字典前请自己过一遍。",
        "   ============================================================ */",
        "",
    ]
    for text, wheres in entries.items():
        lines.append(f"  /* {wheres[0]} */")
        lines.append(f"  {js_quote(text)}: \"\",")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def js_quote(text: str) -> str:
    """按字典文件的风格输出 key。

    一律输出**单行**字面量，换行转义成 \\n —— 跨行拼接虽然好看，
    但很容易在复制粘贴时少一个引号，不值得。
    """
    body = (text.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\n", "\\n")
                .replace("\t", "\\t"))
    return f'"{body}"'


def run_extract(lang: str, out: Path | None) -> int:
    """抓中文字面量，生成待翻译清单。

    只抓**主站**。管理后台（admin.html / admin.js）不翻译 —— 那是给管理员看的，
    翻译它只会让后台多三份需要同步维护的文案，见 frontend/js/i18n.js 开头的说明。
    """
    targets = [p for p in HTML_FILES + js_files() if "admin" not in p.name]
    targets = [p for p in targets if p.exists()]
    entries = extract_literals(targets)

    # 已经进了字典的就不必再看
    have = set(load_locale(LOCALE_FILES[lang]))
    pending = {k: v for k, v in entries.items() if k not in have}

    print(f"抓到主站中文字面量 {len(entries)} 条；其中 {lang} 尚未翻译 {len(pending)} 条")
    if out:
        write_skeleton(lang, pending, out)
        print(f"骨架已写入 {out}")
        return 0

    for text, wheres in pending.items():
        flat = text.replace("\n", "\\n")
        print(f"  [{wheres[0]}] {flat}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
