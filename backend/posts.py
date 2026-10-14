"""帖子：发布、浏览、点赞、评论、转发与通知。

为什么单独一个模块（而不是把逻辑塞进 routers/posts.py）
------------------------------------------------------
和 mirror.py / matching.py 一样：路由层只管「收参数、抛 HTTP 错误」，
这里管「什么算合法、什么算重复、谁该收到通知」。这样测试和后台批处理
可以绕过 HTTP 直接调用，不用起一个服务器。

一、正文的数据形状
------------------
正文是**分块数组**，不是一整段 HTML，也不是 Markdown：

    [{"type": "text",  "text": "这段话"},
     {"type": "image", "file": "a1b2….png", "caption": "图下面的注释"}]

！为什么不让用户直接写 HTML / 富文本：那等于把「怎么渲染」交给了用户，
  而本站的 CSP 虽然能挡住脚本，内联样式和怪异的标签结构照样能把页面搞乱，
  而且没有任何办法给出稳定的打印 / 分享格式。分块数组的每一项
  都由前端用固定模板渲染，用户能控制的只有文字和图片本身。

二、为什么所有校验都在服务端再做一遍
------------------------------------
前端当然也会校验（提交前就报错，体验更好），但那只是体验。
请求是客户端发的，长度、类型、图片归属全都可以伪造 —— 所以
`validate_blocks` 会重新检查每一块的类型、长度，以及**每一张图是不是
这个人自己在这篇帖子里上传的**。少了最后这一条，任何人都能把别人的
图片文件名填进自己的帖子，等于白嫖别人的磁盘和图床。
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Iterable

from . import media, moderation
from .database import now_ts

# ---------------------------------------------------------------- 各项上限

TITLE_MIN = 2
TITLE_MAX = 80
BODY_MAX_BLOCKS = 400
BLOCK_TEXT_MAX = 5000
BLOCK_CAPTION_MAX = 200
BODY_TEXT_TOTAL_MAX = 20000
COMMENT_MIN = 1
COMMENT_MAX = 500
SHARE_NOTE_MAX = 120
SHARE_MAX_RECIPIENTS = 20
CREDIT_MAX = 10
ROLE_MAX = 12

# ---------------------------------------------------------------- 行内格式
#
# 正文是「块 → 行内段」两层。一个块是一段 / 一个标题 / 一组列表项；
# 块里面的每一小段可以单独加粗、变色、改字号。
#
# ！所有格式取值都是【白名单里的名字】，不是任意 CSS。
#   两个理由：
#   ① 存死十六进制颜色的话，浅色主题里配好的深红字，换到深色主题
#      就糊在暗背景上了。存名字、渲染时映射到主题变量，两个主题都对。
#   ② 白名单让服务端校验变成一句 `in`，永远不可能被塞进任意样式。
#      这比「收 HTML 再拿清洗器洗一遍」可靠得多 —— 而且本项目不许加依赖，
#      手写 HTML 清洗器正是最容易漏掉边界情况的那类代码。
RUN_COLORS = ("red", "gold", "grass", "cyan", "nether", "gray")
RUN_HIGHLIGHTS = ("yellow", "green", "cyan", "pink", "gray")
RUN_SIZES = ("small", "large", "huge")
RUN_FLAGS = ("bold", "italic", "underline", "strike")
BLOCK_ALIGNS = ("left", "center", "right")
# 单块里的行内段上限。！正常写作一个自然段也就几段格式，
# 给到 300 是因为「从 Word 里粘一大段带格式的文字」会产生很多碎段。
RUN_MAX = 300
LIST_MAX_ITEMS = 60

FEED_PAGE = 20
COMMENT_PAGE = 30
INBOX_PAGE = 30

# 上传了却一直没发出去的图，多久之后可以删。给足重新编辑的时间。
MEDIA_ORPHAN_SECONDS = 24 * 3600

# ---------------------------------------------------------------- 发布频率（v0.31）
#
# ！加这个是因为一次体检发现：反馈、验证码、登录都有频率限制，
#   而**发帖和评论一条都没有** —— 一个登录用户可以在几秒内把首页刷满。
#   封号和下架都是事后手段，而刷屏是即时伤害，受众还是其他未成年人。
#
# ！数值刻意给得很宽。本站的正常用户是中小学生：
#   憋一篇长文再发、或者一口气补几条评论，都是合理行为。
#   这套限制针对的是脚本，不是人。
POST_PER_HOUR = 10
POST_PER_DAY = 30
COMMENT_PER_HOUR = 30
COMMENT_PER_DAY = 120


def _post_flood(conn: sqlite3.Connection, user_id: int) -> str | None:
    """发帖太快时返回一句能直接给用户看的提示，否则 None。

    ！统计**不筛 status**：连发连删同样是刷屏，
      而「删了就不计数」一秒就能把限制绕过去。
    """
    stamp = now_ts()
    recent = int(conn.execute(
        "SELECT COUNT(*) FROM posts WHERE author_id = ? AND created_at > ?",
        (user_id, stamp - 3600),
    ).fetchone()[0])
    if recent >= POST_PER_HOUR:
        return f"一小时内最多发 {POST_PER_HOUR} 篇，先歇一会儿吧"
    today = int(conn.execute(
        "SELECT COUNT(*) FROM posts WHERE author_id = ? AND created_at > ?",
        (user_id, stamp - 86400),
    ).fetchone()[0])
    if today >= POST_PER_DAY:
        return f"今天已经发了 {POST_PER_DAY} 篇，明天再来吧"
    return None


def _comment_flood(conn: sqlite3.Connection, user_id: int) -> str | None:
    """评论太快时返回一句提示，否则 None。理由同 _post_flood。"""
    stamp = now_ts()
    recent = int(conn.execute(
        "SELECT COUNT(*) FROM post_comments WHERE user_id = ? AND created_at > ?",
        (user_id, stamp - 3600),
    ).fetchone()[0])
    if recent >= COMMENT_PER_HOUR:
        return "一小时内评论得太多了，歇一会儿再聊吧"
    today = int(conn.execute(
        "SELECT COUNT(*) FROM post_comments WHERE user_id = ? AND created_at > ?",
        (user_id, stamp - 86400),
    ).fetchone()[0])
    if today >= COMMENT_PER_DAY:
        return "今天评论得够多了，明天再来吧"
    return None

STATUS_LABEL = {"published": "已发布", "hidden": "已被下架", "deleted": "已删除"}


class PostError(RuntimeError):
    """业务校验失败。message 可以直接展示给用户。

    status 决定 HTTP 状态码，由 main.py 的异常处理器统一转换：
      404 —— 「这东西不存在」且【不应该告诉对方它是被隐藏还是真没了】
      403 —— 「你不能做这件事」
      422 —— 校验不通过（默认）
    ！为什么不存在也要用 404 而不是 403：被管理员下架的帖子对外人
      就是不存在。区分开来等于把「这里原本有东西，只是被下架了」告诉了所有人。
    """

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- 小工具

def _text(value: Any, *, limit: int, field: str, allow_empty: bool = True) -> str:
    """裁剪并校验一段纯文本。"""
    raw = "" if value is None else str(value)
    # 去掉除了 \n \t 之外的控制字符：它们既看不见，又能在界面上
    # 造成诡异的换行，还容易被用来在词库里「隔开」敏感词绕过检测。
    cleaned = "".join(
        ch for ch in raw if ch in "\n\t" or (ord(ch) >= 32 and ord(ch) != 127)
    ).strip()
    # 连续空行压成一个：用户在编辑器里敲的那点空白不该被原样带到页面上
    cleaned = "\n".join(line.rstrip() for line in cleaned.split("\n"))
    while "\n\n\n" in cleaned:
        cleaned = cleaned.replace("\n\n\n", "\n\n")

    if not cleaned and not allow_empty:
        raise PostError(f"{field}不能为空")
    if len(cleaned) > limit:
        raise PostError(f"{field}太长了（上限 {limit} 字，当前 {len(cleaned)} 字）")
    return cleaned


def _guard(text: str, *, field: str) -> None:
    """过一遍内容安全词库。命中就把提示原样抛出去。"""
    problems = moderation.scan_text(text, field=field)
    if problems:
        raise PostError(problems[0])


def validate_title(value: Any) -> str:
    title = _text(value, limit=TITLE_MAX, field="标题", allow_empty=False)
    if len(title) < TITLE_MIN:
        raise PostError(f"标题至少 {TITLE_MIN} 个字")
    _guard(title, field="标题")
    return title


def validate_comment(value: Any) -> str:
    content = _text(value, limit=COMMENT_MAX, field="评论", allow_empty=False)
    if len(content) < COMMENT_MIN:
        raise PostError("评论不能为空")
    _guard(content, field="评论")
    return content


def validate_note(value: Any) -> str:
    note = _text(value, limit=SHARE_NOTE_MAX, field="转发留言")
    if note:
        _guard(note, field="转发留言")
    return note


def validate_credits(conn: sqlite3.Connection, owner_id: int, raw: Any) -> list[tuple[int, str]]:
    """校验制作人员名单，返回 [(user_id, role), …]。

    ！必须是站内已注册且状态正常的用户。需求里明确要求「添加已经在本站
      注册过的用户」—— 所以这里查库确认，而不是信前端传回来的昵称。
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PostError("制作人员名单格式不对")
    if len(raw) > CREDIT_MAX:
        raise PostError(f"制作人员最多 {CREDIT_MAX} 位")

    result: list[tuple[int, str]] = []
    seen: set[int] = set()
    for item in raw:
        if isinstance(item, dict):
            uid_raw, role_raw = item.get("user_id"), item.get("role")
        else:
            uid_raw, role_raw = item, ""
        try:
            uid = int(uid_raw)
        except (TypeError, ValueError):
            raise PostError("制作人员里有无法识别的用户") from None
        if uid == owner_id:
            # 作者本人默认就在列表最前面，不需要再手动加一遍
            continue
        if uid in seen:
            continue
        seen.add(uid)

        row = conn.execute(
            "SELECT id, status FROM users WHERE id = ?", (uid,)
        ).fetchone()
        if row is None:
            raise PostError("制作人员里有账号已经不存在了，请重新选一次")
        if row["status"] != "active":
            raise PostError("制作人员里有账号已被暂停使用，请换一位")

        role = _text(role_raw, limit=ROLE_MAX, field="分工")
        if role:
            _guard(role, field="分工")
        result.append((uid, role))
    return result


def _run_text(raw: Any) -> str:
    """行内段的文字。

    ！和 _text() 不同：这里【不 strip】。
      行内段之间是靠空格连起来的 —— 把「你好」和「世界」之间的空格
      掐掉，拼出来就成了「你好世界」。首尾空白交给渲染层处理，
      HTML 本来就会把块首尾的空格吃掉。
    """
    text = "" if raw is None else str(raw)
    return "".join(
        ch for ch in text if ch == "\n" or (ord(ch) >= 32 and ord(ch) != 127)
    )


def _clean_runs(raw: Any, *, field: str, collected: list[str]) -> list[dict]:
    """校验一串行内段，返回规范形态。同时把纯文本收进 collected。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PostError(f"{field}格式不对")
    if len(raw) > RUN_MAX:
        raise PostError(f"{field}的格式太碎了，建议先粘到记事本里再复制回来")

    runs: list[dict] = []
    for item in raw:
        # 容忍直接给字符串：旧数据 / 手滑都会走到这里
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue

        text = _run_text(item.get("text"))
        if not text:
            continue
        if len(text) > BLOCK_TEXT_MAX:
            raise PostError(f"{field}里有一段太长了（上限 {BLOCK_TEXT_MAX} 字）")

        run: dict[str, Any] = {"text": text}
        # ！不认识的格式一律【丢掉】而不是报错：客户端多传一个字段就
        #   整个帖子发不出去太苛刻了，而丢掉之后用户看到的就是「没加粗」，
        #   自己会重试一次。
        for flag in RUN_FLAGS:
            if item.get(flag):
                run[flag] = True
        for key, allowed in (("color", RUN_COLORS), ("highlight", RUN_HIGHLIGHTS),
                             ("size", RUN_SIZES)):
            value = str(item.get(key) or "").strip()
            if value in allowed:
                run[key] = value

        runs.append(run)
        collected.append(text)
    return runs


def validate_blocks(
    conn: sqlite3.Connection,
    owner_id: int,
    raw: Any,
    *,
    post_id: int | None = None,
) -> tuple[list[dict], str]:
    """校验正文，返回 (规整后的块数组, 纯文本)。

    图片和附件都必须是「这个人自己上传的」，而且没有被别的帖子占用。
    post_id 不为空时（编辑已有帖子），该帖子已经引用的文件仍然放行。
    """
    if isinstance(raw, str):
        # 容忍前端直接把 JSON 字符串丢过来
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raise PostError("正文格式不对") from None
    if not isinstance(raw, list):
        raise PostError("正文格式不对")
    if len(raw) > BODY_MAX_BLOCKS:
        raise PostError(f"正文段落太多了（上限 {BODY_MAX_BLOCKS} 段）")

    blocks: list[dict] = []
    claimed: list[str] = []
    texts: list[str] = []

    def claim(name: str, label: str) -> dict[str, Any] | None:
        """确认这个文件是本人上传、且没被别的帖子占用；返回它在库里的那行。"""
        if not name:
            return None
        if name in claimed:
            raise PostError(f"同一个{label}不能在正文里放两次")
        row = conn.execute(
            "SELECT file_name, owner_id, post_id, byte_size, display_name "
            "FROM post_media WHERE file_name = ?",
            (name,),
        ).fetchone()
        # ！这一条最要紧：没有它，把别人的文件名填进来就能白嫖别人的图床
        if row is None:
            raise PostError(f"正文里有一个{label}不是本次上传的，请重新插入")
        if int(row["owner_id"]) != owner_id:
            raise PostError(f"正文里有一个{label}不属于你，不能使用")
        occupied = row["post_id"]
        if occupied is not None and (post_id is None or int(occupied) != post_id):
            raise PostError(f"正文里有一个{label}已经被别的帖子用掉了")
        claimed.append(name)
        return dict(row)

    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").strip()

        # 「text」是 v0.27 的旧写法，读进来时统一当成段落 ——
        # 老帖子和老客户端都还能用
        if kind in ("paragraph", "heading", "quote", "text"):
            source = [{"text": item.get("text")}] if kind == "text" else item.get("runs")
            runs = _clean_runs(source, field="正文", collected=texts)
            if not runs:
                continue
            joined = "".join(run["text"] for run in runs)
            if joined.strip():
                _guard(joined.strip(), field="正文")

            if kind == "heading":
                try:
                    level = int(item.get("level") or 2)
                except (TypeError, ValueError):
                    level = 2
                block: dict[str, Any] = {
                    "type": "heading",
                    "level": 3 if level == 3 else 2,
                    "runs": runs,
                }
            elif kind == "quote":
                block = {"type": "quote", "runs": runs}
            else:
                block = {"type": "paragraph", "runs": runs}
                align = str(item.get("align") or "").strip()
                # left 是默认值，不写进 JSON：少一个字段就少一份存储
                if align in BLOCK_ALIGNS and align != "left":
                    block["align"] = align
            blocks.append(block)

        elif kind == "list":
            entries = item.get("items")
            if not isinstance(entries, list):
                continue
            clean_items: list[list[dict]] = []
            for entry in entries[:LIST_MAX_ITEMS]:
                runs = _clean_runs(entry, field="列表项", collected=texts)
                if runs:
                    joined = "".join(run["text"] for run in runs)
                    if joined.strip():
                        _guard(joined.strip(), field="列表项")
                    clean_items.append(runs)
            if clean_items:
                blocks.append({
                    "type": "list",
                    "ordered": bool(item.get("ordered")),
                    "items": clean_items,
                })

        elif kind == "image":
            info = claim(str(item.get("file") or "").strip(), "图片")
            if not info:
                continue
            caption = _text(item.get("caption"), limit=BLOCK_CAPTION_MAX, field="图片注释")
            if caption:
                _guard(caption, field="图片注释")
            blocks.append({"type": "image", "file": info["file_name"], "caption": caption})

        elif kind == "file":
            info = claim(str(item.get("file") or "").strip(), "附件")
            if not info:
                continue
            # ！展示名和体积都【以库里的为准】，不信前端传回来的：
            #   名字会被写进 Content-Disposition 响应头，体积是给人看的
            #   承诺 —— 两者都不该由客户端说了算。
            #   前端传来的值只当上传那一刻的兜底（老数据可能没有 display_name）。
            blocks.append({
                "type": "file",
                "file": info["file_name"],
                "name": (info["display_name"]
                         or media.safe_display_name(item.get("name"))),
                "size": int(info["byte_size"] or 0),
            })

    if not blocks:
        raise PostError("正文不能为空，至少写一段文字或插一张图")

    plain = "\n".join(texts).strip()
    if not plain and not any(b["type"] in ("image", "file") for b in blocks):
        raise PostError("正文不能为空，至少写一段文字")
    if len(plain) > BODY_TEXT_TOTAL_MAX:
        raise PostError(f"正文总字数超出上限（{BODY_TEXT_TOTAL_MAX} 字）")

    return blocks, plain


# 旧版（v0.27）的正文是 {"type":"text","text":"…"}。
# 读出来时顺手升成新形状，这样渲染层只需要认识一套格式。
def _upgrade_legacy_block(block: dict) -> dict:
    if not isinstance(block, dict):
        return {}
    if block.get("type") == "text":
        return {"type": "paragraph", "runs": [{"text": block.get("text") or ""}]}
    return block


def blocks_from_row(row: sqlite3.Row | dict) -> list[dict]:
    """把数据库里的 body 列读回成块数组。坏数据返回空数组而不是抛异常。"""
    raw = row["body"] if isinstance(row, sqlite3.Row) else row.get("body")
    try:
        data = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [_upgrade_legacy_block(item) for item in data]


# ---------------------------------------------------------------- 黑名单

def hidden_user_ids(conn: sqlite3.Connection, viewer_id: int | None) -> list[int]:
    """当前用户不该看到其内容的那些人。

    ！双向都算：我拉黑了对方，不想再看到他；对方拉黑了我，我也应该
      从他的世界里消失（否则「屏蔽」只是单向静音，起不到保护作用）。
      这和 users.view_profile 里的判断是同一条规则。
    """
    if not viewer_id:
        return []
    rows = conn.execute(
        "SELECT blocked_user AS other FROM blocks WHERE user_id = ? "
        "UNION "
        "SELECT user_id AS other FROM blocks WHERE blocked_user = ?",
        (viewer_id, viewer_id),
    ).fetchall()
    return [int(r["other"]) for r in rows]


def _not_in(column: str, ids: Iterable[int]) -> tuple[str, list[int]]:
    items = list(ids)
    if not items:
        return "", []
    marks = ", ".join("?" for _ in items)
    return f" AND {column} NOT IN ({marks})", items


# ---------------------------------------------------------------- 序列化

def author_card(row: sqlite3.Row | dict) -> dict:
    """帖子里出现的「人」。刻意比 to_public 小得多 —— 一屏可能有几十条评论，
    每条都带一份完整资料会让响应体白白大出好几倍。"""
    # ！几处 SQL 是「SELECT p.*, <作者列>」的写法，而 posts 表自己也有 id 列。
    #   sqlite3.Row 按名字取同名列时只认**第一个**，所以 u.id 会被 p.id 顶掉，
    #   结果「作者」的 id 变成了帖子 id —— 头像、主页链接全指向不存在的人。
    #   作者列因此统一改名为 author_uid；只查 users 表的调用方没有这个别名，
    #   退回读 id。
    keys = row.keys()
    uid = row["author_uid"] if "author_uid" in keys else row["id"]
    return {
        "id": int(uid),
        "nickname": row["nickname"],
        "username": row["username"],
        "avatar_hue": int(row["avatar_hue"] or 190),
        "avatar_file": row["avatar_file"] or "",
        "skin_file": row["skin_file"] or "",
        "mc_skin_file": row["mc_skin_file"] or "",
        "mc_skin_variant": row["mc_skin_variant"] or "CLASSIC",
    }


_AUTHOR_COLS = (
    "u.id AS author_uid, u.nickname, u.username, u.avatar_hue, "
    "u.avatar_file, u.skin_file, u.mc_skin_file, u.mc_skin_variant"
)


def _credits_map(
    conn: sqlite3.Connection, post_ids: list[int]
) -> dict[int, list[dict]]:
    """一次把多篇帖子的「额外制作人员」全取回来，按帖子 id 分组。

    ！这个函数的存在就是为了消 N+1（v0.31）。
      列表页先前是「每篇帖子单独查一次制作人员」—— 一页 10 篇、
      就是 10 条额外查询；帖子列表和「我发的」都跑同一个循环。
      现在是一条 IN 查询 + Python 分组，与页大小无关。
    """
    if not post_ids:
        return {}
    marks = ",".join("?" * len(post_ids))
    rows = conn.execute(
        f"""
        SELECT c.post_id AS credit_post, {_AUTHOR_COLS},
               c.role AS credit_role, c.position AS credit_pos
        FROM post_credits c
        JOIN users u ON u.id = c.user_id
        WHERE c.post_id IN ({marks})
        ORDER BY c.position, u.id
        """,
        post_ids,
    ).fetchall()

    grouped: dict[int, list[dict]] = {}
    for row in rows:
        grouped.setdefault(int(row["credit_post"]), []).append({
            "user": author_card(row),
            "role": (row["credit_role"] or "").strip(),
        })
    return grouped


def _credits_from(extras: list[dict], author: dict) -> list[dict]:
    """作者本人永远排在第一位 —— 他默认就是创作者之一，
    不放进 post_credits 表只是因为那是「额外添加的人」的意思。"""
    return [{"user": author, "role": "作者"}, *extras]


def _credits_of(conn: sqlite3.Connection, post_id: int, author: dict) -> list[dict]:
    """单篇帖子的制作人员（详情页用）。批量场景走 _credits_map。"""
    extras = _credits_map(conn, [int(post_id)]).get(int(post_id), [])
    return _credits_from(extras, author)


def post_card(conn: sqlite3.Connection, row: sqlite3.Row, author: dict,
              extra_credits: list[dict] | None = None) -> dict:
    """列表页用的精简卡片：按需求只给「制作人员 + 标题」。

    extra_credits 由调用方批量预取（见 feed），不传就自己查一次。
    """
    credits = (
        _credits_from(extra_credits, author) if extra_credits is not None
        else _credits_of(conn, int(row["id"]), author)
    )
    return {
        "id": int(row["id"]),
        "title": row["title"],
        "excerpt": (row["plain"] or "")[:120],
        "author": author,
        "credits": credits,
        "like_count": int(row["like_count"]),
        "comment_count": int(row["comment_count"]),
        "share_count": int(row["share_count"]),
        "view_count": int(row["view_count"]),
        "created_at": int(row["created_at"]),
        "updated_at": int(row["updated_at"]),
        "status": row["status"],
        "image_count": sum(
            1 for block in blocks_from_row(row) if block.get("type") == "image"
        ),
    }


def post_detail(conn: sqlite3.Connection, row: sqlite3.Row, author: dict,
                viewer_id: int | None) -> dict:
    blocks = blocks_from_row(row)
    liked = False
    if viewer_id:
        liked = conn.execute(
            "SELECT 1 FROM post_likes WHERE post_id = ? AND user_id = ?",
            (row["id"], viewer_id),
        ).fetchone() is not None

    return {
        "id": int(row["id"]),
        "title": row["title"],
        "blocks": blocks,
        "author": author,
        "credits": _credits_of(conn, int(row["id"]), author),
        "like_count": int(row["like_count"]),
        "comment_count": int(row["comment_count"]),
        "share_count": int(row["share_count"]),
        "view_count": int(row["view_count"]),
        "created_at": int(row["created_at"]),
        "updated_at": int(row["updated_at"]),
        "status": row["status"],
        "status_label": STATUS_LABEL.get(row["status"], row["status"]),
        "liked": liked,
        "is_author": bool(viewer_id and int(row["author_id"]) == int(viewer_id)),
    }


# ---------------------------------------------------------------- 读取

def feed(
    conn: sqlite3.Connection,
    viewer_id: int | None,
    *,
    keyword: str = "",
    author_id: int | None = None,
    page: int = 1,
    limit: int = FEED_PAGE,
) -> dict:
    """帖子列表。按时间倒序 —— 需求要的是「最近发布的实时帖子」。"""
    page = max(1, int(page or 1))
    limit = max(1, min(int(limit or FEED_PAGE), 50))
    offset = (page - 1) * limit

    where = ["p.status = 'published'"]
    params: list[Any] = []

    if author_id:
        # 看自己的帖子时连隐藏/删除的也一并给 —— 否则「我删掉的那篇还能不能找回来」
        # 在界面上完全无从判断。看别人的仍然只给已发布的。
        if viewer_id and int(author_id) == int(viewer_id):
            where = ["p.author_id = ?"]
        else:
            where.append("p.author_id = ?")
        params.append(int(author_id))

    clause, extra = _not_in("p.author_id", hidden_user_ids(conn, viewer_id))
    if clause:
        where.append(clause.lstrip(" AND "))
        params.extend(extra)

    keyword = (keyword or "").strip()
    if keyword:
        if len(keyword) > 40:
            raise PostError("搜索词太长了")
        like = f"%{keyword}%"
        where.append("(p.title LIKE ? OR p.plain LIKE ?)")
        params.extend([like, like])

    sql_where = " AND ".join(where)
    total = int(conn.execute(
        f"SELECT COUNT(*) FROM posts p WHERE {sql_where}", params
    ).fetchone()[0])

    rows = conn.execute(
        f"""
        SELECT p.*, {_AUTHOR_COLS}
        FROM posts p JOIN users u ON u.id = p.author_id
        WHERE {sql_where}
        ORDER BY p.created_at DESC, p.id DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()

    # ！一条 IN 查询把所有帖子的制作人员全拿回来，不再一篇一条（消 N+1）
    credits_map = _credits_map(conn, [int(row["id"]) for row in rows])
    items = [
        post_card(conn, row, author_card(row), credits_map.get(int(row["id"]), []))
        for row in rows
    ]
    return {
        "items": items,
        "total": total,
        "page": page,
        "has_more": offset + len(items) < total,
    }


def get_row(conn: sqlite3.Connection, post_id: int) -> sqlite3.Row:
    row = conn.execute(
        f"SELECT p.*, {_AUTHOR_COLS} FROM posts p JOIN users u ON u.id = p.author_id "
        "WHERE p.id = ?",
        (post_id,),
    ).fetchone()
    if row is None:
        raise PostError("这篇帖子不存在，可能已经被删除了", 404)
    return row


def read_post(conn: sqlite3.Connection, viewer_id: int | None, post_id: int,
              *, count_view: bool = True) -> dict:
    row = get_row(conn, post_id)
    if row["status"] != "published":
        allowed = viewer_id and (
            int(row["author_id"]) == int(viewer_id) or _is_admin(conn, viewer_id)
        )
        if not allowed:
            raise PostError("这篇帖子不存在，可能已经被删除了", 404)
    if viewer_id and int(row["author_id"]) in set(hidden_user_ids(conn, viewer_id)):
        raise PostError("这篇帖子不存在，可能已经被删除了", 404)

    # 浏览量只记「别人看的」，作者自己反复刷新不该把数字刷上去
    if count_view and viewer_id and int(row["author_id"]) != int(viewer_id):
        conn.execute("UPDATE posts SET view_count = view_count + 1 WHERE id = ?", (post_id,))
        conn.commit()
        row = get_row(conn, post_id)

    return post_detail(conn, row, author_card(row), viewer_id)


def _is_admin(conn: sqlite3.Connection, user_id: int) -> bool:
    row = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row and row["is_admin"])


# ---------------------------------------------------------------- 写入

def _sync_media(conn: sqlite3.Connection, post_id: int, blocks: list[dict]) -> None:
    """把正文里引用到的图片挂到这篇帖子上，并把不再引用的解绑。

    解绑（而不是直接删文件）是故意的：用户编辑时删掉一张图、
    保存前又后悔，直接删文件就找不回来了。解绑之后它变回「孤儿」，
    由 sweep_orphan_media 过了保留期再删。
    """
    # ！先全部解绑，再把这次用到的挂回去。
    #   反过来的话，「这次编辑中删掉的那个文件」会一直挂在帖子上，
    #   永远不在孤儿名单里，也就永远删不掉。
    conn.execute("UPDATE post_media SET post_id = NULL WHERE post_id = ?", (post_id,))
    # 图片和附件都算 —— 少算一种的后果是它的文件永远清不掉
    used = {
        str(b.get("file")) for b in blocks
        if b.get("type") in ("image", "file") and b.get("file")
    }
    if not used:
        return
    marks = ", ".join("?" for _ in used)
    conn.execute(
        f"UPDATE post_media SET post_id = ? WHERE file_name IN ({marks})",
        [post_id, *used],
    )


def _replace_credits(conn: sqlite3.Connection, post_id: int,
                     credits: list[tuple[int, str]]) -> None:
    conn.execute("DELETE FROM post_credits WHERE post_id = ?", (post_id,))
    for position, (uid, role) in enumerate(credits):
        conn.execute(
            "INSERT INTO post_credits (post_id, user_id, role, position) VALUES (?, ?, ?, ?)",
            (post_id, uid, role, position),
        )


def create_post(
    conn: sqlite3.Connection,
    user: dict,
    *,
    title: Any,
    blocks: Any,
    credits: Any = None,
) -> dict:
    clean_title = validate_title(title)
    clean_blocks, plain = validate_blocks(conn, int(user["id"]), blocks)
    clean_credits = validate_credits(conn, int(user["id"]), credits)

    # ！频率限制放在所有校验之后：先把内容问题告诉用户（改一下就能重发），
    #   而不是先报「发得太多了」、改完再报「标题太长」—— 那样要来回好几次。
    flood = _post_flood(conn, int(user["id"]))
    if flood:
        raise PostError(flood, status=429)

    stamp = now_ts()
    cursor = conn.execute(
        "INSERT INTO posts (author_id, title, body, plain, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'published', ?, 0)",
        (
            int(user["id"]),
            clean_title,
            json.dumps(clean_blocks, ensure_ascii=False),
            plain,
            stamp,
        ),
    )
    post_id = int(cursor.lastrowid)

    _replace_credits(conn, post_id, clean_credits)
    _sync_media(conn, post_id, clean_blocks)

    # 被写进制作名单的人，应该知道自己的名字被挂上去了
    for uid, _role in clean_credits:
        notify(
            conn, uid, "credit", actor_id=int(user["id"]), post_id=post_id,
            payload={"title": clean_title},
        )
    conn.commit()
    return read_post(conn, int(user["id"]), post_id, count_view=False)


def update_post(
    conn: sqlite3.Connection,
    user: dict,
    post_id: int,
    *,
    title: Any = None,
    blocks: Any = None,
    credits: Any = None,
) -> dict:
    row = get_row(conn, post_id)
    if int(row["author_id"]) != int(user["id"]):
        raise PostError("只能修改自己发布的帖子", 403)
    if row["status"] == "deleted":
        raise PostError("这篇帖子已经删除了，改不了")

    clean_title = validate_title(title) if title is not None else row["title"]
    if blocks is None:
        clean_blocks = blocks_from_row(row)
        plain = row["plain"]
    else:
        clean_blocks, plain = validate_blocks(conn, int(user["id"]), blocks, post_id=post_id)
    clean_credits = (
        validate_credits(conn, int(user["id"]), credits)
        if credits is not None
        else None
    )

    conn.execute(
        "UPDATE posts SET title = ?, body = ?, plain = ?, updated_at = ? WHERE id = ?",
        (
            clean_title,
            json.dumps(clean_blocks, ensure_ascii=False),
            plain,
            now_ts(),
            post_id,
        ),
    )
    if clean_credits is not None:
        before = {int(r["user_id"]) for r in conn.execute(
            "SELECT user_id FROM post_credits WHERE post_id = ?", (post_id,)
        ).fetchall()}
        _replace_credits(conn, post_id, clean_credits)
        for uid, _role in clean_credits:
            if uid not in before:
                notify(
                    conn, uid, "credit", actor_id=int(user["id"]), post_id=post_id,
                    payload={"title": clean_title},
                )
    _sync_media(conn, post_id, clean_blocks)
    conn.commit()
    return read_post(conn, int(user["id"]), post_id, count_view=False)


def delete_post(conn: sqlite3.Connection, user: dict, post_id: int) -> None:
    row = get_row(conn, post_id)
    if int(row["author_id"]) != int(user["id"]) and not _is_admin(conn, int(user["id"])):
        raise PostError("只能删除自己发布的帖子", 403)
    # ！软删。这篇帖子可能已经被别人点赞、评论、转发过，
    #   物理删掉会让那些人的通知和链接点进去变成一片空白，
    #   而他们什么都没做错。软删之后对他们表现为「已被删除」。
    conn.execute("UPDATE posts SET status = 'deleted' WHERE id = ?", (post_id,))
    conn.commit()


# ---------------------------------------------------------------- 点赞

def toggle_like(conn: sqlite3.Connection, user: dict, post_id: int) -> dict:
    row = get_row(conn, post_id)
    if row["status"] != "published":
        raise PostError("这篇帖子当前不能被点赞")
    uid = int(user["id"])

    exists = conn.execute(
        "SELECT 1 FROM post_likes WHERE post_id = ? AND user_id = ?", (post_id, uid)
    ).fetchone()
    if exists:
        conn.execute(
            "DELETE FROM post_likes WHERE post_id = ? AND user_id = ?", (post_id, uid)
        )
        conn.execute(
            "UPDATE posts SET like_count = MAX(0, like_count - 1) WHERE id = ?", (post_id,)
        )
        conn.execute(
            "DELETE FROM notifications WHERE kind = 'like' AND post_id = ? AND actor_id = ?",
            (post_id, uid),
        )
        liked = False
    else:
        conn.execute(
            "INSERT INTO post_likes (post_id, user_id, created_at) VALUES (?, ?, ?)",
            (post_id, uid, now_ts()),
        )
        conn.execute("UPDATE posts SET like_count = like_count + 1 WHERE id = ?", (post_id,))
        notify(
            conn, int(row["author_id"]), "like", actor_id=uid, post_id=post_id,
            payload={"title": row["title"]},
        )
        liked = True

    conn.commit()
    count = int(conn.execute(
        "SELECT like_count FROM posts WHERE id = ?", (post_id,)
    ).fetchone()[0])
    return {"liked": liked, "like_count": count}


# ---------------------------------------------------------------- 评论

def list_comments(
    conn: sqlite3.Connection,
    viewer_id: int | None,
    post_id: int,
    *,
    page: int = 1,
    limit: int = COMMENT_PAGE,
) -> dict:
    get_row(conn, post_id)
    page = max(1, int(page or 1))
    limit = max(1, min(int(limit or COMMENT_PAGE), 100))
    offset = (page - 1) * limit

    where = ["c.post_id = ?", "c.status = 'published'"]
    params: list[Any] = [post_id]
    clause, extra = _not_in("c.user_id", hidden_user_ids(conn, viewer_id))
    if clause:
        where.append(clause.lstrip(" AND "))
        params.extend(extra)

    sql_where = " AND ".join(where)
    total = int(conn.execute(
        f"SELECT COUNT(*) FROM post_comments c WHERE {sql_where}", params
    ).fetchone()[0])
    rows = conn.execute(
        f"""
        SELECT c.id, c.content, c.created_at, c.user_id, {_AUTHOR_COLS}
        FROM post_comments c JOIN users u ON u.id = c.user_id
        WHERE {sql_where}
        ORDER BY c.created_at ASC, c.id ASC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()

    items = [
        {
            "id": int(row["id"]),
            "content": row["content"],
            "created_at": int(row["created_at"]),
            "user": author_card(row),
            "mine": bool(viewer_id and int(row["user_id"]) == int(viewer_id)),
        }
        for row in rows
    ]
    return {"items": items, "total": total, "page": page,
            "has_more": offset + len(items) < total}


def add_comment(conn: sqlite3.Connection, user: dict, post_id: int, content: Any) -> dict:
    row = get_row(conn, post_id)
    if row["status"] != "published":
        raise PostError("这篇帖子当前不能评论")
    clean = validate_comment(content)
    flood = _comment_flood(conn, int(user["id"]))
    if flood:
        raise PostError(flood, status=429)

    stamp = now_ts()
    cursor = conn.execute(
        "INSERT INTO post_comments (post_id, user_id, content, status, created_at) "
        "VALUES (?, ?, ?, 'published', ?)",
        (post_id, int(user["id"]), clean, stamp),
    )
    conn.execute(
        "UPDATE posts SET comment_count = comment_count + 1 WHERE id = ?", (post_id,)
    )
    notify(
        conn, int(row["author_id"]), "comment", actor_id=int(user["id"]), post_id=post_id,
        payload={"title": row["title"], "excerpt": clean[:60]},
    )
    conn.commit()
    return {
        "id": int(cursor.lastrowid),
        "content": clean,
        "created_at": stamp,
        "user": author_card(user),
        "mine": True,
    }


def delete_comment(conn: sqlite3.Connection, user: dict, post_id: int, comment_id: int) -> None:
    row = conn.execute(
        "SELECT c.user_id, c.post_id, p.author_id FROM post_comments c "
        "JOIN posts p ON p.id = c.post_id WHERE c.id = ?",
        (comment_id,),
    ).fetchone()
    if row is None or int(row["post_id"]) != int(post_id):
        raise PostError("这条评论不存在", 404)

    uid = int(user["id"])
    # 评论作者本人，或者帖子的作者（楼主有权清理自己帖子下面的评论），或者管理员
    if uid not in (int(row["user_id"]), int(row["author_id"])) and not _is_admin(conn, uid):
        raise PostError("只能删除自己的评论", 403)

    conn.execute("UPDATE post_comments SET status = 'deleted' WHERE id = ?", (comment_id,))
    conn.execute(
        "UPDATE posts SET comment_count = MAX(0, comment_count - 1) WHERE id = ?", (post_id,)
    )
    conn.commit()


# ---------------------------------------------------------------- 转发

def share(
    conn: sqlite3.Connection,
    user: dict,
    post_id: int,
    target_ids: Any,
    note: Any = "",
) -> dict:
    row = get_row(conn, post_id)
    if row["status"] != "published":
        raise PostError("这篇帖子当前不能被转发")

    if not isinstance(target_ids, list) or not target_ids:
        raise PostError("请选择要转发给谁")
    if len(target_ids) > SHARE_MAX_RECIPIENTS:
        raise PostError(f"一次最多转发给 {SHARE_MAX_RECIPIENTS} 个人")

    clean_note = validate_note(note)
    uid = int(user["id"])

    sent: list[int] = []
    skipped: list[int] = []
    for raw in target_ids:
        try:
            target = int(raw)
        except (TypeError, ValueError):
            continue
        if target == uid or target in sent or target in skipped:
            continue

        target_row = conn.execute(
            "SELECT id, status FROM users WHERE id = ?", (target,)
        ).fetchone()
        if target_row is None or target_row["status"] != "active":
            skipped.append(target)
            continue

        # 对方拉黑了我，就不要再推过去了 —— 推过去他也看不到，
        # 只会让「转发成功」变成一句空话。
        blocked = conn.execute(
            "SELECT 1 FROM blocks WHERE user_id = ? AND blocked_user = ?",
            (target, uid),
        ).fetchone()
        if blocked:
            skipped.append(target)
            continue

        already = conn.execute(
            "SELECT 1 FROM post_shares WHERE post_id = ? AND from_user = ? AND to_user = ?",
            (post_id, uid, target),
        ).fetchone()
        if already:
            skipped.append(target)
            continue

        conn.execute(
            "INSERT INTO post_shares (post_id, from_user, to_user, note, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (post_id, uid, target, clean_note, now_ts()),
        )
        notify(
            conn, target, "share", actor_id=uid, post_id=post_id,
            payload={"title": row["title"], "note": clean_note},
        )
        sent.append(target)

    if not sent:
        raise PostError("一个都没发出去：对方可能已经拉黑了你，或者你已经转给过他们了")

    conn.execute(
        "UPDATE posts SET share_count = share_count + ? WHERE id = ?", (len(sent), post_id)
    )
    conn.commit()
    return {"sent": sent, "skipped": skipped, "share_count": int(conn.execute(
        "SELECT share_count FROM posts WHERE id = ?", (post_id,)
    ).fetchone()[0])}


def share_recipients(conn: sqlite3.Connection, user: dict, keyword: str = "") -> list[dict]:
    """可转发的人：排除自己、已拉黑关系的、以及已经转过的（查过就算）。"""
    uid = int(user["id"])
    where = ["u.id != ?", "u.status = 'active'"]
    params: list[Any] = [uid]
    clause, extra = _not_in("u.id", hidden_user_ids(conn, uid))
    if clause:
        where.append(clause.lstrip(" AND "))
        params.extend(extra)
    if keyword:
        like = f"%{keyword.strip()[:40]}%"
        where.append("(u.nickname LIKE ? OR u.username LIKE ?)")
        params.extend([like, like])

    rows = conn.execute(
        f"SELECT {_AUTHOR_COLS} FROM users u WHERE {' AND '.join(where)} "
        "ORDER BY u.last_seen DESC LIMIT 30",
        params,
    ).fetchall()
    # ！这里刻意不排除「已经转过」的人：这个列表是用来挑人的，
    #   把一部分人藏起来会让人以为搜不到。重复转发在 share() 里
    #   会被跳过，并如实告诉用户跳过了谁。
    return [author_card(row) for row in rows]


def search_users(conn: sqlite3.Connection, user: dict, keyword: str) -> list[dict]:
    """制作人员 / 转发对象的搜索。空关键词返回最近活跃的一批，方便直接点。"""
    uid = int(user["id"])
    keyword = (keyword or "").strip()[:40]
    where = ["u.id != ?", "u.status = 'active'"]
    params: list[Any] = [uid]
    if keyword:
        like = f"%{keyword}%"
        where.append("(u.nickname LIKE ? OR u.username LIKE ?)")
        params.extend([like, like])

    rows = conn.execute(
        f"SELECT {_AUTHOR_COLS} FROM users u WHERE {' AND '.join(where)} "
        "ORDER BY u.last_seen DESC LIMIT 20",
        params,
    ).fetchall()
    return [author_card(row) for row in rows]


# ---------------------------------------------------------------- 通知

def notify(
    conn: sqlite3.Connection,
    user_id: int,
    kind: str,
    *,
    actor_id: int | None = None,
    post_id: int | None = None,
    payload: dict | None = None,
) -> None:
    """写一条通知。自己对自己的操作不产生通知。"""
    if actor_id is not None and int(actor_id) == int(user_id):
        return
    conn.execute(
        "INSERT INTO notifications (user_id, kind, actor_id, post_id, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            int(user_id),
            kind,
            int(actor_id) if actor_id is not None else None,
            int(post_id) if post_id is not None else None,
            json.dumps(payload or {}, ensure_ascii=False),
            now_ts(),
        ),
    )


def unread_count(conn: sqlite3.Connection, user_id: int) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read_at IS NULL",
        (user_id,),
    ).fetchone()[0])


def inbox(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    box: str = "all",
    page: int = 1,
    limit: int = INBOX_PAGE,
) -> dict:
    page = max(1, int(page or 1))
    limit = max(1, min(int(limit or INBOX_PAGE), 100))
    offset = (page - 1) * limit

    where = ["n.user_id = ?"]
    params: list[Any] = [int(user_id)]
    if box == "unread":
        where.append("n.read_at IS NULL")
    sql_where = " AND ".join(where)

    total = int(conn.execute(
        f"SELECT COUNT(*) FROM notifications n WHERE {sql_where}", params
    ).fetchone()[0])
    rows = conn.execute(
        f"""
        SELECT n.*, a.nickname AS actor_nickname, a.username AS actor_username,
               a.avatar_hue AS actor_hue, a.avatar_file AS actor_avatar,
               a.skin_file AS actor_skin, a.mc_skin_file AS actor_mcskin,
               a.mc_skin_variant AS actor_variant,
               p.title AS post_title, p.status AS post_status
        FROM notifications n
        LEFT JOIN users a ON a.id = n.actor_id
        LEFT JOIN posts p ON p.id = n.post_id
        WHERE {sql_where}
        ORDER BY n.created_at DESC, n.id DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()

    items = []
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        actor = None
        if row["actor_nickname"]:
            actor = {
                "id": int(row["actor_id"]),
                "nickname": row["actor_nickname"],
                "username": row["actor_username"],
                "avatar_hue": int(row["actor_hue"] or 190),
                "avatar_file": row["actor_avatar"] or "",
                "skin_file": row["actor_skin"] or "",
                "mc_skin_file": row["actor_mcskin"] or "",
                "mc_skin_variant": row["actor_variant"] or "CLASSIC",
            }
        items.append({
            "id": int(row["id"]),
            "kind": row["kind"],
            "actor": actor,
            # 帖子标题优先用快照：帖子被删之后 title 是 NULL，
            # 但那条通知总得显示点什么
            "post_title": payload.get("title") or row["post_title"] or "",
            "note": payload.get("note") or "",
            "excerpt": payload.get("excerpt") or "",
            "post_id": int(row["post_id"]) if row["post_id"] is not None else None,
            "post_gone": row["post_id"] is None or row["post_status"] in (None, "deleted", "hidden"),
            "read": row["read_at"] is not None,
            "created_at": int(row["created_at"]),
        })

    return {
        "items": items,
        "total": total,
        "page": page,
        "has_more": offset + len(items) < total,
        "unread": unread_count(conn, int(user_id)),
    }


def mark_read(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    ids: Any = None,
    all_unread: bool = False,
) -> int:
    """标记已读，返回影响条数。"""
    uid = int(user_id)
    stamp = now_ts()
    if all_unread:
        cursor = conn.execute(
            "UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL",
            (stamp, uid),
        )
    else:
        if not isinstance(ids, list) or not ids:
            return 0
        clean: list[int] = []
        for raw in ids[:200]:
            try:
                clean.append(int(raw))
            except (TypeError, ValueError):
                continue
        if not clean:
            return 0
        marks = ", ".join("?" for _ in clean)
        cursor = conn.execute(
            f"UPDATE notifications SET read_at = ? "
            f"WHERE user_id = ? AND read_at IS NULL AND id IN ({marks})",
            [stamp, uid, *clean],
        )
    conn.commit()
    return int(cursor.rowcount or 0)


# ---------------------------------------------------------------- 维护

def sweep_orphan_media(conn: sqlite3.Connection, *, now: int | None = None) -> int:
    """删掉上传了但一直没被任何帖子引用的文件，返回删掉的个数。

    ！有保留期（MEDIA_ORPHAN_SECONDS）：用户可能在编辑器里传了图、
      关掉页面、第二天打开继续写。当场删掉的话他回来会发现图没了。
    """
    from .media import remove_any

    cutoff = (now if now is not None else int(time.time())) - MEDIA_ORPHAN_SECONDS
    rows = conn.execute(
        "SELECT id, file_name FROM post_media WHERE post_id IS NULL AND created_at < ?",
        (cutoff,),
    ).fetchall()
    for row in rows:
        remove_any(row["file_name"])
        conn.execute("DELETE FROM post_media WHERE id = ?", (row["id"],))
    if rows:
        conn.commit()
    return len(rows)


def delete_post_media(conn: sqlite3.Connection, post_id: int) -> int:
    """彻底删掉一篇帖子的所有图片与附件（管理员彻底清理时用）。"""
    from .media import remove_any

    rows = conn.execute(
        "SELECT id, file_name FROM post_media WHERE post_id = ?", (post_id,)
    ).fetchall()
    for row in rows:
        remove_any(row["file_name"])
        conn.execute("DELETE FROM post_media WHERE id = ?", (row["id"],))
    conn.commit()
    return len(rows)


def recount(conn: sqlite3.Connection, post_id: int | None = None) -> int:
    """按明细重算冗余计数。后台的一键修复。"""
    if post_id is None:
        ids = [int(r["id"]) for r in conn.execute("SELECT id FROM posts").fetchall()]
    else:
        ids = [int(post_id)]
    for pid in ids:
        conn.execute(
            "UPDATE posts SET "
            "like_count = (SELECT COUNT(*) FROM post_likes WHERE post_id = ?), "
            "comment_count = (SELECT COUNT(*) FROM post_comments "
            "                 WHERE post_id = ? AND status = 'published'), "
            "share_count = (SELECT COUNT(*) FROM post_shares WHERE post_id = ?) "
            "WHERE id = ?",
            (pid, pid, pid, pid),
        )
    conn.commit()
    return len(ids)


# ---------------------------------------------------------------- 后台

def admin_list(
    conn: sqlite3.Connection,
    *,
    status: str = "",
    keyword: str = "",
    page: int = 1,
    limit: int = 30,
) -> dict:
    page = max(1, int(page or 1))
    limit = max(1, min(int(limit or 30), 100))
    offset = (page - 1) * limit

    where = ["1 = 1"]
    params: list[Any] = []
    if status in ("published", "hidden", "deleted"):
        where.append("p.status = ?")
        params.append(status)
    keyword = (keyword or "").strip()[:40]
    if keyword:
        like = f"%{keyword}%"
        where.append("(p.title LIKE ? OR p.plain LIKE ? OR u.nickname LIKE ?)")
        params.extend([like, like, like])
    sql_where = " AND ".join(where)

    total = int(conn.execute(
        f"SELECT COUNT(*) FROM posts p JOIN users u ON u.id = p.author_id WHERE {sql_where}",
        params,
    ).fetchone()[0])
    rows = conn.execute(
        f"""
        SELECT p.*, {_AUTHOR_COLS}
        FROM posts p JOIN users u ON u.id = p.author_id
        WHERE {sql_where}
        ORDER BY p.created_at DESC, p.id DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()

    items = []
    for row in rows:
        items.append({
            "id": int(row["id"]),
            "title": row["title"],
            "excerpt": (row["plain"] or "")[:160],
            "author": author_card(row),
            "status": row["status"],
            "status_label": STATUS_LABEL.get(row["status"], row["status"]),
            "like_count": int(row["like_count"]),
            "comment_count": int(row["comment_count"]),
            "share_count": int(row["share_count"]),
            "view_count": int(row["view_count"]),
            "created_at": int(row["created_at"]),
        })
    return {"items": items, "total": total, "page": page,
            "has_more": offset + len(items) < total}


def set_status(conn: sqlite3.Connection, post_id: int, status: str,
               *, reason: str = "") -> None:
    """管理员下架 / 恢复一篇帖子。"""
    if status not in ("published", "hidden", "deleted"):
        raise PostError("状态不合法")
    row = get_row(conn, post_id)
    conn.execute("UPDATE posts SET status = ? WHERE id = ?", (status, post_id))
    if status == "hidden":
        notify(
            conn, int(row["author_id"]), "system", post_id=post_id,
            payload={"title": row["title"], "reason": reason or "内容不符合社区规范"},
        )
    conn.commit()


def admin_comments(
    conn: sqlite3.Connection,
    *,
    keyword: str = "",
    page: int = 1,
    limit: int = 30,
) -> dict:
    """后台用：按时间倒序列出评论（含已隐藏的），带所属帖子标题。"""
    page = max(1, int(page or 1))
    limit = max(1, min(int(limit or 30), 100))
    offset = (page - 1) * limit

    where = ["1 = 1"]
    params: list[Any] = []
    keyword = (keyword or "").strip()[:40]
    if keyword:
        like = f"%{keyword}%"
        where.append("(c.content LIKE ? OR u.nickname LIKE ? OR p.title LIKE ?)")
        params.extend([like, like, like])
    sql_where = " AND ".join(where)

    total = int(conn.execute(
        f"""SELECT COUNT(*) FROM post_comments c
            JOIN users u ON u.id = c.user_id
            JOIN posts p ON p.id = c.post_id
            WHERE {sql_where}""",
        params,
    ).fetchone()[0])
    rows = conn.execute(
        f"""
        SELECT c.id, c.content, c.status, c.created_at, c.post_id,
               {_AUTHOR_COLS}, p.title AS post_title
        FROM post_comments c
        JOIN users u ON u.id = c.user_id
        JOIN posts p ON p.id = c.post_id
        WHERE {sql_where}
        ORDER BY c.created_at DESC, c.id DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()

    items = [
        {
            "id": int(row["id"]),
            "content": row["content"],
            "status": row["status"],
            "created_at": int(row["created_at"]),
            "post_id": int(row["post_id"]),
            "post_title": row["post_title"],
            "user": author_card(row),
            # ！给后台看的内容要遮掉手机号。管理员需要判断「该不该下架」，
            #   而不是需要知道某个学生的真实号码 —— 多一个人看到就多一份风险。
            "masked": moderation.mask_contacts(row["content"]),
        }
        for row in rows
    ]
    return {"items": items, "total": total, "page": page,
            "has_more": offset + len(items) < total}


def set_comment_status(conn: sqlite3.Connection, comment_id: int, status: str) -> None:
    """管理员隐藏 / 恢复一条评论，并把帖子的评论数同步回去。"""
    if status not in ("published", "hidden", "deleted"):
        raise PostError("状态不合法")
    row = conn.execute(
        "SELECT post_id, status FROM post_comments WHERE id = ?", (comment_id,)
    ).fetchone()
    if row is None:
        raise PostError("这条评论不存在", 404)
    was_visible = row["status"] == "published"
    will_visible = status == "published"

    conn.execute("UPDATE post_comments SET status = ? WHERE id = ?", (status, comment_id))
    # ！计数要跟着动，而且只能动一次：从「可见」变成「不可见」减一，
    #   反过来加一，其余情况不变。写成每次都增减的话，
    #   连点两次「隐藏」就会把计数减成负数。
    if was_visible and not will_visible:
        conn.execute(
            "UPDATE posts SET comment_count = MAX(0, comment_count - 1) WHERE id = ?",
            (row["post_id"],),
        )
    elif not was_visible and will_visible:
        conn.execute(
            "UPDATE posts SET comment_count = comment_count + 1 WHERE id = ?",
            (row["post_id"],),
        )
    conn.commit()
