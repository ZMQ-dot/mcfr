"""圈子：一群人，和只有这群人看得见的帖子。

为什么单独一个模块（而不是把逻辑塞进 routers/circles.py）
------------------------------------------------------
和 posts.py / mirror.py / matching.py 一样：路由层只管「收参数、抛 HTTP 错误」，
这里管「什么算合法、谁能加入、退出时圈子怎么办」。
测试和后台脚本因此可以绕过 HTTP 直接调用，不用起一个服务器。

一、圈子和「发现同好」是两条不同的路
------------------------------------
    发现同好：算法把人推到一起 —— 1 对 1，靠匹配分，陌生人
    圈子    ：人自己挑一个主题进去 —— 多对多，靠共同兴趣

所以圈子必须能**自己找上门**（发现圈子的列表、按名字搜），
而不是像同好卡片那样等算法发牌。

二、圈子帖复用 posts 表
-----------------------
发帖那一套（富文本正文、配图、附件、制作人员、点赞、评论、转发、
频率限制、内容安全词库、后台下架、软删除）已经全部写好了，
而且是这个项目里最重的一块。

所以圈子帖就是 `posts` 里 `circle_id != 0` 的那些行，唯一的区别是**可见范围**：
它对圈子成员可见，对别人连存在都看不到（见 posts.read_post / posts.feed）。
这里不做任何「另开一张表」的事 —— 那样等于把上面每一项都再写一遍。

三、退出圈子时那个圈子怎么办
----------------------------
这是本模块唯一真正需要动脑子的一条规则：

  · 普通成员退出 —— 删掉自己那一行，什么都不会发生。
  · 圈主退出、圈子里还有别人 —— 圈主**移交给最早加入的那一位**。
    不移交的话圈子会变成「没有人是圈主」的孤儿，以后谁都没法改它。
  · 圈主退出、圈子里只剩他自己 —— 圈子**解散**（status='deleted'），
    并且把这个圈子里的帖子一起软删。
    ！为什么必须一起软删：帖子留着一个指向已解散圈子的 circle_id，
      就等于「谁都看不见，作者也不知道去哪儿了」。软删之后作者至少能在
      「我发的帖子」里看到它，并且标着「已删除」—— 和作者自己删帖的处理一致。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import moderation, posts
from .database import now_ts

# ---------------------------------------------------------------- 各项上限

NAME_MIN = 2
NAME_MAX = 20
INTRO_MAX = 60
ABOUT_MAX = 500
TOPIC_MAX = 12

# 一个人最多同时加入多少个圈子。
# ！不是防刷，是防「脚本把全站圈子都点一遍」—— 那会让每个圈子的成员数失去意义。
#   30 对真人来说绰绰有余（真正会常逛的圈子顶多三五个）。
MAX_JOINED = 30

# 一天最多创建几个圈子。理由同上，另加一条：创建会占一个唯一的名字，
# 刷屏式的创建会把好名字全部占掉。
CREATE_PER_DAY = 3

LIST_PAGE = 24
# 圈子主页上列几个成员的头像。再多的走「成员」接口。
MEMBER_PREVIEW = 8
MEMBER_PAGE = 30

# 圈子主题。！必须和 frontend 里那一组保持一致 ——
#   前端不再自己写一份，而是从 /api/meta 的 circle_topics 读（见 main.meta）。
#   这里加一档只需改这一处，前端下拉框跟着就有了。
CIRCLE_TOPICS: tuple[str, ...] = (
    "生存", "建筑", "红石", "模组", "小游戏", "联机", "聊天", "综合",
)

# 列表一次最多取多少（与 posts.FEED_PAGE 的 50 上限对齐）
MAX_PAGE = 60


class CircleError(RuntimeError):
    """圈子相关的业务错误。message 可以直接展示给用户。

    status 决定 HTTP 状态码，由 main.py 的异常处理器统一转换：
      404 —— 圈子不存在 / 已经解散 / 你不在里面（三者在对外表现上必须一样）
      403 —— 你在这个圈子里没有这个权限（比如没加入就想看帖子）
      409 —— 状态冲突（改名撞车之类）
      429 —— 频率 / 数量限制
      422 —— 校验不通过（默认）
    """

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- 小工具

def _text(value: Any, *, limit: int, field: str, allow_empty: bool = True) -> str:
    """裁剪并校验一段纯文本。规则与 posts._text 完全一致。

    ！没有直接复用它，是因为那边抛的是 PostError —— 两个模块的错误类型
      必须分开，否则「发帖失败」和「建圈子失败」在日志里分不出来。
      两边都是二十行，重复得起；而且这段逻辑一旦改动就要两处一起想。
    """
    raw = "" if value is None else str(value)
    # 去掉除 \n \t 之外的控制字符：看不见，却能在界面上造成诡异换行，
    # 也容易被用来在敏感词中间「隔开」一个字绕过检测。
    cleaned = "".join(
        ch for ch in raw if ch in "\n\t" or (ord(ch) >= 32 and ord(ch) != 127)
    ).strip()
    cleaned = "\n".join(line.rstrip() for line in cleaned.split("\n"))
    while "\n\n\n" in cleaned:
        cleaned = cleaned.replace("\n\n\n", "\n\n")

    if not cleaned and not allow_empty:
        raise CircleError(f"{field}不能为空")
    if len(cleaned) > limit:
        raise CircleError(f"{field}太长了（上限 {limit} 字，当前 {len(cleaned)} 字）")
    return cleaned


def _line(value: Any, *, limit: int, field: str, allow_empty: bool = True) -> str:
    """单行字段（名字 / 一句话介绍 / 主题）：把所有空白压成一个空格。

    ！名字里带换行会把列表卡片的排版压歪，而用户往往是无意中粘进来的。
      直接压平比报错友好 —— 报错会让人以为是自己填错了。
    """
    text = " ".join(_text(value, limit=limit * 4, field=field,
                          allow_empty=allow_empty).split())
    if len(text) > limit:
        raise CircleError(f"{field}太长了（上限 {limit} 字，当前 {len(text)} 字）")
    return text


def _guard(text: str, *, field: str) -> None:
    """过一遍内容安全词库（和帖子、昵称用的是同一份）。"""
    problems = moderation.scan_text(text, field=field)
    if problems:
        raise CircleError(problems[0])


def validate_name(value: Any) -> str:
    name = _line(value, limit=NAME_MAX, field="圈子名", allow_empty=False)
    if len(name) < NAME_MIN:
        raise CircleError(f"圈子名至少 {NAME_MIN} 个字")
    _guard(name, field="圈子名")
    return name


def validate_intro(value: Any) -> str:
    intro = _line(value, limit=INTRO_MAX, field="一句话介绍")
    _guard(intro, field="一句话介绍")
    return intro


def validate_about(value: Any) -> str:
    about = _text(value, limit=ABOUT_MAX, field="圈子说明")
    _guard(about, field="圈子说明")
    return about


def validate_topic(value: Any) -> str:
    """主题只能是 CIRCLE_TOPICS 里的一个；留空表示「综合」。"""
    topic = _line(value, limit=TOPIC_MAX, field="主题")
    if not topic:
        return ""
    if topic not in CIRCLE_TOPICS:
        raise CircleError("主题只能从列表里选一个")
    return topic


# ---------------------------------------------------------------- 序列化
#
# ！这里直接复用 posts 里那套「人」的序列化（author_card / _AUTHOR_COLS）。
#   圈子卡片上的圈主、成员列表里的人，和帖子里的作者是完全一样的形状，
#   前端用同一个头像函数就能画出来。自己再写一份的话，两处字段迟早会不一样，
#   而症状是「某个人的头像在帖子页正常，在圈子页变成默认方块脸」——
#   很难想到是序列化不一致。

_OWNER_COLS = posts._AUTHOR_COLS


def _circle_row(conn: sqlite3.Connection, circle_id: int) -> sqlite3.Row:
    """取一个还活着的圈子，连带圈主那一行。不存在 / 已解散一律 404。

    ！已解散的圈子和不存在的圈子对外表现必须完全一样：
      区分开来等于告诉别人「这里以前有个圈子，只是散了」。
    """
    row = conn.execute(
        f"SELECT c.*, {_OWNER_COLS} FROM circles c JOIN users u ON u.id = c.owner_id "
        "WHERE c.id = ? AND c.status = 'open'",
        (int(circle_id),),
    ).fetchone()
    if row is None:
        raise CircleError("这个圈子不存在，可能已经解散了", 404)
    return row


def _role(conn: sqlite3.Connection, user_id: int | None, circle_id: int) -> str:
    """我在这个圈子里的身份：'owner' / 'member' / ''（没加入）。"""
    if not user_id:
        return ""
    row = conn.execute(
        "SELECT role FROM circle_members WHERE circle_id = ? AND user_id = ?",
        (int(circle_id), int(user_id)),
    ).fetchone()
    return str(row["role"]) if row is not None else ""


def is_member(conn: sqlite3.Connection, user_id: int | None, circle_id: int) -> bool:
    """这个人现在是不是这个圈子的成员（圈主也算）。

    ！posts.py 判断「圈子帖能不能看」用的就是它 —— 那是唯一一处定义，
      不要在别处再写一遍 SELECT 1 FROM circle_members。
    """
    return bool(_role(conn, user_id, circle_id))


def _counts(conn: sqlite3.Connection, ids: list[int]) -> tuple[dict[int, int], dict[int, int]]:
    """一次把这一页所有圈子的成员数、帖子数全取回来。

    ！这两个数都是**现算的**，不落成 circles 表上的冗余列。
      冗余计数的代价是「每次加入 / 退出 / 发帖 / 删帖 / 后台下架」都要同步，
      漏一处就永久对不上（帖子那边是靠 recount 一键重算兜的）。
      而这里一次 IN 查询就把整页数完了，与页大小无关 ——
      和 v0.31 消掉帖子列表 N+1 的 _credits_map 是同一个思路。

      帖子数只数 published：作者自己删掉的、被后台下架的都不该算进
      「这个圈子有多少帖子」，否则数字会越用越大、永远不回落。
    """
    if not ids:
        return {}, {}
    marks = ", ".join("?" for _ in ids)
    members = {
        int(row[0]): int(row[1])
        for row in conn.execute(
            f"SELECT circle_id, COUNT(*) FROM circle_members "
            f"WHERE circle_id IN ({marks}) GROUP BY circle_id",
            ids,
        )
    }
    published = {
        int(row[0]): int(row[1])
        for row in conn.execute(
            f"SELECT circle_id, COUNT(*) FROM posts "
            f"WHERE circle_id IN ({marks}) AND status = 'published' GROUP BY circle_id",
            ids,
        )
    }
    return members, published


def _joined_map(conn: sqlite3.Connection, viewer_id: int | None,
                ids: list[int]) -> dict[int, str]:
    """我在这一页里分别是什么身份。没加入的圈子不会出现在返回值里。"""
    if not viewer_id or not ids:
        return {}
    marks = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT circle_id, role FROM circle_members "
        f"WHERE user_id = ? AND circle_id IN ({marks})",
        [int(viewer_id), *ids],
    )
    return {int(row["circle_id"]): str(row["role"]) for row in rows}


def _card(row: sqlite3.Row, *, members: int, published: int,
          my_role: str, with_about: bool = False) -> dict:
    card = {
        "id": int(row["id"]),
        "name": row["name"],
        "intro": row["intro"],
        "topic": row["topic"] or "综合",
        "owner": posts.author_card(row),
        "member_count": members,
        "post_count": published,
        "joined": bool(my_role),
        "is_owner": my_role == "owner",
        "created_at": int(row["created_at"]),
    }
    if with_about:
        card["about"] = row["about"]
        card["updated_at"] = int(row["updated_at"] or 0)
    return card


def _serialize(conn: sqlite3.Connection, viewer_id: int | None,
               rows: list[sqlite3.Row], *, with_about: bool = False) -> list[dict]:
    ids = [int(row["id"]) for row in rows]
    members, published = _counts(conn, ids)
    mine = _joined_map(conn, viewer_id, ids)
    return [
        _card(
            row,
            members=members.get(int(row["id"]), 0),
            published=published.get(int(row["id"]), 0),
            my_role=mine.get(int(row["id"]), ""),
            with_about=with_about,
        )
        for row in rows
    ]


# ---------------------------------------------------------------- 读取

def list_circles(
    conn: sqlite3.Connection,
    viewer_id: int | None,
    *,
    scope: str = "all",
    keyword: str = "",
    page: int = 1,
    limit: int = LIST_PAGE,
) -> dict:
    """圈子列表。

    scope = "mine" → 我加入的（按加入时间倒序，最近加的在最上面）
    scope = "all"  → 还在的圈子全部（按**人数**从多到少，让人一眼看到哪些热闹）

    ！「我的圈子」不按创建时间排，而按**加入时间**排：用户找的是「我昨天
      刚加的那个圈子」，不是「哪个圈子建得晚」。
    """
    page = max(1, int(page or 1))
    limit = max(1, min(int(limit or LIST_PAGE), MAX_PAGE))
    offset = (page - 1) * limit

    keyword = (keyword or "").strip()
    if len(keyword) > 40:
        raise CircleError("搜索词太长了")

    if scope == "mine":
        if not viewer_id:
            raise CircleError("请先登录", 401)
        base = ("FROM circles c JOIN users u ON u.id = c.owner_id "
                "JOIN circle_members m ON m.circle_id = c.id")
        where = ["m.user_id = ?", "c.status = 'open'"]
        params: list[Any] = [int(viewer_id)]
        order = "m.joined_at DESC, c.id DESC"
    else:
        base = "FROM circles c JOIN users u ON u.id = c.owner_id"
        where = ["c.status = 'open'"]
        params = []
        # ！排序用一个标量子查询数人数，而不是 JOIN + GROUP BY：
        #   JOIN 会和下面的关键词过滤、分页互相干扰，还得给每个字段套
        #   COUNT(DISTINCT ...)，写起来又长又容易错。这里每行只多一次
        #   索引查找（circle_members 的主键正好是 (circle_id, user_id)）。
        order = ("(SELECT COUNT(*) FROM circle_members x WHERE x.circle_id = c.id) DESC, "
                 "c.created_at DESC, c.id DESC")

    if keyword:
        where.append("(c.name LIKE ? OR c.intro LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])

    sql_where = " AND ".join(where)
    total = int(conn.execute(
        f"SELECT COUNT(*) {base} WHERE {sql_where}", params
    ).fetchone()[0])

    rows = conn.execute(
        f"SELECT c.*, {_OWNER_COLS} {base} WHERE {sql_where} "
        f"ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()

    items = _serialize(conn, viewer_id, rows)
    return {
        "items": items,
        "total": total,
        "page": page,
        "has_more": offset + len(items) < total,
    }


def members_of(
    conn: sqlite3.Connection,
    circle_id: int,
    *,
    limit: int = MEMBER_PREVIEW,
) -> list[dict]:
    """成员列表。圈主排最前面，其余按加入时间从早到晚。

    ！圈主排最前不是「特权」，是因为他在这个圈子里的角色和其他人确实不同 ——
      名片上要标出来，退圈时也要知道该找谁。
    """
    limit = max(1, min(int(limit or MEMBER_PREVIEW), MEMBER_PAGE))
    rows = conn.execute(
        "SELECT u.*, m.role AS member_role, m.joined_at AS member_joined "
        "FROM circle_members m JOIN users u ON u.id = m.user_id "
        "WHERE m.circle_id = ? "
        "ORDER BY (m.role = 'owner') DESC, m.joined_at, u.id "
        "LIMIT ?",
        (int(circle_id), limit),
    ).fetchall()
    return [
        {
            "user": posts.author_card(row),
            "role": str(row["member_role"]),
            "joined_at": int(row["member_joined"]),
        }
        for row in rows
    ]


def get_circle(conn: sqlite3.Connection, viewer_id: int | None, circle_id: int) -> dict:
    """圈子详情。

    ！没加入的人也能看到这个页面（不然没法「加入」），但看到的只有
      名字、介绍、人数和成员头像 —— 帖子列表在 circle_posts 里单独把关。
    """
    row = _circle_row(conn, circle_id)
    cards = _serialize(conn, viewer_id, [row], with_about=True)
    card = cards[0]
    card["members"] = members_of(conn, circle_id)
    card["is_admin"] = bool(viewer_id and posts._is_admin(conn, int(viewer_id)))
    return card


def circle_posts(
    conn: sqlite3.Connection,
    viewer_id: int | None,
    circle_id: int,
    *,
    keyword: str = "",
    page: int = 1,
    limit: int = posts.FEED_PAGE,
) -> dict:
    """圈子里的帖子。**只有成员能看**。

    ！这是圈子唯一的硬边界：圈子存在的意义就是「这些话只有圈子里的人看得到」。
      所以这里先查身份再查帖子，而不是「查出来再过滤」——
      后者任何一处漏写就是一次内容泄露。
    """
    _circle_row(conn, circle_id)
    if not is_member(conn, viewer_id, circle_id):
        raise CircleError("加入这个圈子之后，才能看到里面发了什么", 403)
    return posts.feed(
        conn, viewer_id, circle_id=int(circle_id),
        keyword=keyword, page=page, limit=limit,
    )


# ---------------------------------------------------------------- 写入

def create_circle(
    conn: sqlite3.Connection,
    user: dict,
    *,
    name: Any,
    intro: Any = "",
    about: Any = "",
    topic: Any = "",
) -> dict:
    """建一个圈子，创建者自动成为圈主和第一个成员。"""
    uid = int(user["id"])
    clean_name = validate_name(name)
    clean_intro = validate_intro(intro)
    clean_about = validate_about(about)
    clean_topic = validate_topic(topic)

    # ！先查重、再判频率。反过来的话，一个已经建了 3 个圈子的人
    #   会被拦在「今天建得太多了」上，而他真正的问题是名字撞了 ——
    #   改完名字再提交，才发现还是不行，白跑一趟。
    taken = conn.execute(
        "SELECT 1 FROM circles WHERE name = ? COLLATE NOCASE", (clean_name,)
    ).fetchone()
    if taken is not None:
        raise CircleError("已经有同名的圈子了，换一个名字吧", 409)

    stamp = now_ts()
    made = int(conn.execute(
        "SELECT COUNT(*) FROM circles WHERE owner_id = ? AND created_at > ?",
        (uid, stamp - 86400),
    ).fetchone()[0])
    if made >= CREATE_PER_DAY:
        raise CircleError(f"一天最多创建 {CREATE_PER_DAY} 个圈子，明天再来吧", 429)

    try:
        cursor = conn.execute(
            "INSERT INTO circles "
            "(name, intro, about, topic, owner_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'open', ?, 0)",
            (clean_name, clean_intro, clean_about, clean_topic, uid, stamp),
        )
    except sqlite3.IntegrityError:
        # 上面的查重和这里的插入之间有一瞬间的缝隙（两个人同时提交同名）。
        # 数据库上的唯一索引是硬保证，这里只是把它翻译成一句人话，
        # 而不是让用户撞上一个 500「操作失败」。
        raise CircleError("已经有同名的圈子了，换一个名字吧", 409) from None
    circle_id = int(cursor.lastrowid)
    conn.execute(
        "INSERT INTO circle_members (circle_id, user_id, role, joined_at) VALUES (?, ?, 'owner', ?)",
        (circle_id, uid, stamp),
    )
    conn.commit()
    return get_circle(conn, uid, circle_id)


def join_circle(conn: sqlite3.Connection, user: dict, circle_id: int) -> dict:
    """加入圈子。已经加入了就原样返回（幂等）—— 重复点一下不该报错。"""
    uid = int(user["id"])
    _circle_row(conn, circle_id)

    if is_member(conn, uid, circle_id):
        return get_circle(conn, uid, circle_id)

    joined = int(conn.execute(
        "SELECT COUNT(*) FROM circle_members WHERE user_id = ?", (uid,)
    ).fetchone()[0])
    if joined >= MAX_JOINED:
        raise CircleError(
            f"最多同时加入 {MAX_JOINED} 个圈子，先从「我的圈子」里退出几个吧", 429
        )

    conn.execute(
        "INSERT INTO circle_members (circle_id, user_id, role, joined_at) "
        "VALUES (?, ?, 'member', ?)",
        (int(circle_id), uid, now_ts()),
    )
    conn.commit()
    return get_circle(conn, uid, circle_id)


def leave_circle(conn: sqlite3.Connection, user: dict, circle_id: int) -> dict:
    """退出圈子（界面上那句「删除」就是这个）。

    返回 {dissolved, successor, post_count}：
      dissolved —— 圈子是否随之解散（只剩圈主一个人时会发生）
      successor —— 移交给了谁（只有圈主退出、且还有人时才非空）
      post_count —— 解散时一起软删了多少篇帖子

    规则写在本文件开头的第三节，改动之前先读一遍。
    """
    uid = int(user["id"])
    _circle_row(conn, circle_id)

    role = _role(conn, uid, circle_id)
    if not role:
        raise CircleError("你还没加入这个圈子", 404)

    dissolved = False
    successor: dict | None = None
    removed_posts = 0

    if role == "owner":
        nxt = conn.execute(
            "SELECT user_id FROM circle_members "
            "WHERE circle_id = ? AND user_id != ? "
            "ORDER BY joined_at, user_id LIMIT 1",
            (int(circle_id), uid),
        ).fetchone()
        if nxt is not None:
            heir = int(nxt["user_id"])
            conn.execute(
                "UPDATE circles SET owner_id = ?, updated_at = ? WHERE id = ?",
                (heir, now_ts(), int(circle_id)),
            )
            conn.execute(
                "UPDATE circle_members SET role = 'owner' WHERE circle_id = ? AND user_id = ?",
                (int(circle_id), heir),
            )
            heir_row = conn.execute(
                "SELECT nickname FROM users WHERE id = ?", (heir,)
            ).fetchone()
            successor = {"id": heir, "nickname": heir_row["nickname"] if heir_row else ""}
        else:
            dissolved = True
            conn.execute(
                "UPDATE circles SET status = 'deleted', updated_at = ? WHERE id = ?",
                (now_ts(), int(circle_id)),
            )
            # ！只把 status 改掉，不碰 updated_at —— 那个字段在详情页
            #   表示「作者改过这篇帖子」，和「被删掉」是两件事。
            cursor = conn.execute(
                "UPDATE posts SET status = 'deleted' "
                "WHERE circle_id = ? AND status != 'deleted'",
                (int(circle_id),),
            )
            removed_posts = int(cursor.rowcount or 0)

    conn.execute(
        "DELETE FROM circle_members WHERE circle_id = ? AND user_id = ?",
        (int(circle_id), uid),
    )
    conn.commit()
    return {
        "ok": True,
        "circle_id": int(circle_id),
        "dissolved": dissolved,
        "successor": successor,
        "post_count": removed_posts,
    }


# ---------------------------------------------------------------- 给 posts.py 用

def postable_circle(conn: sqlite3.Connection, user_id: int, circle_id: int) -> sqlite3.Row:
    """发帖前的把关：圈子还在、而且这个人是成员。

    ！「是成员才能发」这条不能只靠前端藏按钮 —— 请求是客户端发的，
      任何登录用户都能把 circle_id 填成别人圈子的 id。
    """
    row = _circle_row(conn, circle_id)
    if not is_member(conn, user_id, circle_id):
        raise CircleError("加入这个圈子之后，才能在里面发帖", 403)
    return row


def topics() -> list[str]:
    return list(CIRCLE_TOPICS)
