"""SQLite 数据访问层：连接管理、建表语句与通用小工具。

第一版刻意选用 SQLite 单文件数据库：零配置、免安装服务，
方便在教室电脑 / 演示笔记本上直接跑起来（开题报告技术选型中的「可替换」项，
后续可平滑迁移到 MySQL / PostgreSQL）。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "mcfr.db"

# v0.3 起站点更名为 mcfr，数据库文件名同步调整；
# 启动时会自动把旧的 blocklink.db 接管过来，用户数据不会丢。
LEGACY_DB_PATH = DATA_DIR / "blocklink.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    username             TEXT    NOT NULL UNIQUE,
    nickname             TEXT    NOT NULL,
    password_hash        TEXT    NOT NULL,
    -- 邮箱从 v0.21 起降级成「普通资料字段」：不再验证、不再发任何信。
    -- ！原来的 email_verified_at 列已经由数据订正 v3 删掉，
    --   别再把它加回下面那张补列名单里 —— 加了的话它每次启动都会被重建。
    email                TEXT    NOT NULL DEFAULT '',
    avatar_hue           INTEGER NOT NULL DEFAULT 190,
    -- 用户自己上传的形象（v0.9 起）：只存文件名，实际文件在 data/uploads/ 下
    avatar_file          TEXT    NOT NULL DEFAULT '',
    skin_file            TEXT    NOT NULL DEFAULT '',
    -- Minecraft 游戏身份的存储位。
    -- ！列名用 mc_ 而不是 ms_：早期版本用过 ms_uuid / skin_url 那一组，
    --   后来整个功能被取消，并有一步数据订正专门把它们删掉（见 LEGACY_USER_COLUMNS）。
    --   两边名字不重叠，可以避免「新装一份站点时那一步把刚加的列又删掉」
    --   这种顺序上的坑 —— 那个 bug 只会在全新环境里出现，极难排查。
    --   写入方（微软登录）已在 v0.18 整体移除；这几列保留着，供以后接入
    --   公开的档案查询（填游戏名 → 取皮肤 / 披风）时直接复用。
    mc_uuid              TEXT    NOT NULL DEFAULT '',
    mc_name              TEXT    NOT NULL DEFAULT '',
    mc_skin_file         TEXT    NOT NULL DEFAULT '',
    mc_cape_file         TEXT    NOT NULL DEFAULT '',
    -- CLASSIC 或 SLIM。决定手臂是 4px 还是 3px，skinview.js 要用
    mc_skin_variant      TEXT    NOT NULL DEFAULT 'CLASSIC',
    mc_linked_at         INTEGER NOT NULL DEFAULT 0,
    bio                  TEXT    NOT NULL DEFAULT '',
    edition              TEXT    NOT NULL DEFAULT 'java',
    -- ！v0.29 起 age_group 停用，换成下面三列出生日期。
    --   存出生日期而不是存「几岁」：年龄每年会自己变，存成数字就得有人去改，
    --   而「没人会去改」正是这类字段烂掉的标准方式。推导逻辑见 backend/age.py。
    --   0 表示未填（老账号登录后会被要求补填）。
    birth_year           INTEGER NOT NULL DEFAULT 0,
    birth_month          INTEGER NOT NULL DEFAULT 0,
    birth_day            INTEGER NOT NULL DEFAULT 0,
    -- 　遗留列：只用来给「还从没登录过、没补填生日」的老账号估个年龄。
    --   新注册的账号一律写 'unsaid'。
    age_group            TEXT    NOT NULL DEFAULT 'unsaid',
    region               TEXT    NOT NULL DEFAULT '',
    play_styles          TEXT    NOT NULL DEFAULT '[]',
    online_time          TEXT    NOT NULL DEFAULT '[]',
    looking_for          TEXT    NOT NULL DEFAULT '[]',
    contact_qq           TEXT    NOT NULL DEFAULT '',
    contact_wechat       TEXT    NOT NULL DEFAULT '',
    contact_other        TEXT    NOT NULL DEFAULT '',
    contact_note         TEXT    NOT NULL DEFAULT '',
    points               INTEGER NOT NULL DEFAULT 0,
    -- 登录节律（v0.22 起）。三个列合起来只回答两个问题：
    --   「今天是第几天连续来？」（login_streak / last_login_day）
    --   「一共来过多少天？」（login_days）
    -- ！不能用 sessions 表数：登出、过期清理、封禁都会删它，
    --   那是「当前活着的令牌」，不是「这个人来过哪些天」。
    last_login_day       TEXT    NOT NULL DEFAULT '',
    login_streak         INTEGER NOT NULL DEFAULT 0,
    login_days           INTEGER NOT NULL DEFAULT 0,
    profile_seen_count   INTEGER NOT NULL DEFAULT 0,
    -- ！这一列和上面那列是一对反方向：
    --   profile_seen_count = 「我的主页被多少人看过」
    --   profile_view_count = 「我主动看过多少位同好」
    --   v0.22 之前只有前者，而「探索者（浏览 20 位同好）」也读的是它，
    --   于是那条成就被「人气之星」完全盖住，永远不可能单独拿到。
    profile_view_count   INTEGER NOT NULL DEFAULT 0,
    allow_contact_request INTEGER NOT NULL DEFAULT 1,
    show_online          INTEGER NOT NULL DEFAULT 1,
    is_admin             INTEGER NOT NULL DEFAULT 0,
    status               TEXT    NOT NULL DEFAULT 'active',
    created_at           INTEGER NOT NULL,
    last_seen            INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

-- 管理员会话与普通用户完全分开，即使普通令牌泄露也拿不到后台权限
CREATE TABLE IF NOT EXISTS admin_sessions (
    token      TEXT    PRIMARY KEY,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    ip         TEXT    NOT NULL DEFAULT ''
);

-- 管理员登录尝试记录，用于防暴力破解
CREATE TABLE IF NOT EXISTS admin_logins (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ip         TEXT    NOT NULL DEFAULT '',
    success    INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_admin_logins ON admin_logins(ip, created_at);

-- ！注意：依赖 reports 表的索引不能写在这里 ——
-- reports 表在本脚本更下面才创建，全新数据库会直接报 "no such table: main.reports"。
-- 凡是「索引指向的表在其他位置创建」的语句，一律放到 POST_MIGRATION_SQL 里。

-- 图片验证码凭据。
-- v0.21 起用它取代了原来的 email_verifications 表：
--   表还是「一张只增不减的短期凭据表」，只是发凭据的渠道从邮件换成了本站自己画的图。
-- ！id 用随机串而不是自增整数：自增的话凭据可以被枚举，
--   而枚举意味着可以批量取图去做离线破解。
CREATE TABLE IF NOT EXISTS captchas (
    id          TEXT PRIMARY KEY,
    answer_hash TEXT    NOT NULL,
    purpose     TEXT    NOT NULL DEFAULT 'login',
    created_ip  TEXT    NOT NULL DEFAULT '',
    attempts    INTEGER NOT NULL DEFAULT 0,
    expires_at  INTEGER NOT NULL,
    consumed_at INTEGER,
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_captchas_created ON captchas(created_at);
CREATE INDEX IF NOT EXISTS idx_captchas_ip      ON captchas(created_ip, created_at);

CREATE TABLE IF NOT EXISTS contact_requests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    to_user    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    greeting   TEXT    NOT NULL DEFAULT '',
    status     TEXT    NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL,
    handled_at INTEGER
);

CREATE TABLE IF NOT EXISTS connections (
    user_low   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user_high  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (user_low, user_high)
);

CREATE TABLE IF NOT EXISTS blocks (
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    blocked_user   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at     INTEGER NOT NULL,
    PRIMARY KEY (user_id, blocked_user)
);

CREATE TABLE IF NOT EXISTS reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    reporter_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_user INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reason      TEXT    NOT NULL,
    detail      TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'open',
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS user_achievements (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code        TEXT    NOT NULL,
    unlocked_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, code)
);

CREATE TABLE IF NOT EXISTS interests (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_user INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind        TEXT    NOT NULL,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (user_id, target_user)
);

-- 升级 / 备份记录（v0.10 起）。
-- 刻意存在数据库里而不是单独的日志文件：这样它跟着数据库一起被备份、
-- 一起被带走，换一台机器还原之后仍然知道「这个库升到过哪一版」。
CREATE TABLE IF NOT EXISTS upgrade_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    from_version INTEGER NOT NULL DEFAULT 0,
    to_version   INTEGER NOT NULL DEFAULT 0,
    kind         TEXT    NOT NULL DEFAULT 'upgrade',
    backup_name  TEXT    NOT NULL DEFAULT '',
    status       TEXT    NOT NULL DEFAULT 'ok',
    detail       TEXT    NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_upgrade_history ON upgrade_history(created_at);

-- 意见反馈（v0.13 起）。
-- user_id 允许为空：未登录的访客也能提，只是查不到是谁提的。
CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    kind       TEXT    NOT NULL DEFAULT 'suggestion',
    content    TEXT    NOT NULL,
    contact    TEXT    NOT NULL DEFAULT '',
    page       TEXT    NOT NULL DEFAULT '',
    sent_ip    TEXT    NOT NULL DEFAULT '',
    status     TEXT    NOT NULL DEFAULT 'open',
    reply      TEXT    NOT NULL DEFAULT '',
    handled_by TEXT    NOT NULL DEFAULT '',
    handled_at INTEGER,
    created_at INTEGER NOT NULL
);

-- 管理员账号（v0.13 起）。
-- 之前只有一个「全局管理员密码」，放在 .env 里；
-- 现在改成正经的账号体系：一个超级管理员 + 若干普通管理员。
CREATE TABLE IF NOT EXISTS admins (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    display_name  TEXT    NOT NULL DEFAULT '',
    password_hash TEXT    NOT NULL,
    is_super      INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'active',
    note          TEXT    NOT NULL DEFAULT '',
    created_by    TEXT    NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL,
    last_login    INTEGER NOT NULL DEFAULT 0
);

-- 普通管理员在「基础四个页签」之外的额外权限。
-- level 三档：none 完全看不到 / view 只能看 / manage 能改。
-- ！超级管理员不走这张表，它天生拥有一切；这里只存普通管理员的授权，
--   所以新增一个受控模块时，已有管理员的默认值就是「看不到」（表里没行即 none）。
CREATE TABLE IF NOT EXISTS admin_permissions (
    admin_id   INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
    area       TEXT    NOT NULL,
    level      TEXT    NOT NULL DEFAULT 'none',
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (admin_id, area)
);

-- ===========================================================================
-- 管理后台的密钥对登录（v0.32）
--
-- 模型和 SSH 私钥一样：服务器只存**公钥**，私钥是一个文件，在用户手上。
-- 登录时浏览器用私钥签一段随机挑战，服务器验签。
--
-- ! 数据库被人拖走也进不了后台 —— 这张表里没有任何能反过来冒充用户的东西。
--   这正是它比密码强的地方：密码哈希理论上还能被离线爆破，
--   而公钥「推不出」私钥，这是数学性质，不是难度问题。
--
-- ！一个管理员可以有多把密钥（电脑上一份、U 盘上一份、备用一份），
--   所以是独立一张表而不是 admins 上的一个列。
-- ===========================================================================
CREATE TABLE IF NOT EXISTS admin_keys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id     INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
    -- 公钥哈希的前 16 个十六进制字符。只是标签和查询键，不参与安全判断
    key_id       TEXT    NOT NULL UNIQUE,
    -- 未压缩点：0x04 || X(32) || Y(32)，共 130 个 hex 字符
    public_key   TEXT    NOT NULL,
    -- 给人看的备注，例如「U 盘」「家里的电脑」
    label        TEXT    NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL,
    last_used_at INTEGER NOT NULL DEFAULT 0,
    last_used_ip TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_admin_keys_admin ON admin_keys(admin_id);

-- 登录挑战。和 captchas 是同一类东西：一次性、短命、只增不减。
--
-- ！挑战必须是**服务器生成、一次性、带有效期**的。
--   让客户端自己随便报一个数字来签，签名就成了固定值 ——
--   攻击者抓一次包就能永远重放。
-- ===========================================================================
CREATE TABLE IF NOT EXISTS admin_challenges (
    id          TEXT PRIMARY KEY,          -- 随机串，不是自增（自增可枚举）
    created_ip  TEXT    NOT NULL DEFAULT '',
    expires_at  INTEGER NOT NULL,
    consumed_at INTEGER,
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_admin_challenges_created ON admin_challenges(created_at);

-- ===========================================================================
-- 模组镜像（v0.20 起）
--
-- 索引数据来自 Modrinth 官方 API（https://api.modrinth.com/v2）。
-- 分三张表，因为三件事的生命周期完全不同：
--   mirror_mods   模组索引，随每次同步刷新
--   mirror_files  版本清单，跟着索引一起刷新；管理员选中的会被下到本地
--   mirror_jobs   操作记录，只追加，用来给后台显示进度
-- ===========================================================================

CREATE TABLE IF NOT EXISTS mirror_mods (
    id            TEXT PRIMARY KEY,              -- Modrinth 的 project id
    slug          TEXT    NOT NULL DEFAULT '',
    title         TEXT    NOT NULL,
    description   TEXT    NOT NULL DEFAULT '',
    icon_url      TEXT    NOT NULL DEFAULT '',
    -- 图标在本地缓存的文件名。
    -- ！不让浏览器直接去 Modrinth 的 CDN 取：CSP 是 img-src 'self'，
    --   外链图会被拦掉；就算放开，也等于每次开页面都把用户 IP 交出去。
    local_icon    TEXT    NOT NULL DEFAULT '',
    author        TEXT    NOT NULL DEFAULT '',
    downloads     INTEGER NOT NULL DEFAULT 0,
    follows       INTEGER NOT NULL DEFAULT 0,
    categories    TEXT    NOT NULL DEFAULT '[]',
    loaders       TEXT    NOT NULL DEFAULT '[]',
    game_versions TEXT    NOT NULL DEFAULT '[]',
    -- 管理员对单个模组决定要不要镜像（默认都不要，避免同步完就撑爆磁盘）
    mirrored        INTEGER NOT NULL DEFAULT 0,
    -- 镜像范围。都留空 = 每个加载器只取最新 release，见 mirror.recompute_selection
    mirror_loaders  TEXT    NOT NULL DEFAULT '[]',
    mirror_versions TEXT    NOT NULL DEFAULT '[]',
    published_at  INTEGER NOT NULL DEFAULT 0,
    updated_at    INTEGER NOT NULL DEFAULT 0,
    synced_at     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS mirror_files (
    id             TEXT PRIMARY KEY,             -- Modrinth 的 version id
    mod_id         TEXT    NOT NULL REFERENCES mirror_mods(id) ON DELETE CASCADE,
    name           TEXT    NOT NULL DEFAULT '',
    version_number TEXT    NOT NULL DEFAULT '',
    version_type   TEXT    NOT NULL DEFAULT 'release',
    game_versions  TEXT    NOT NULL DEFAULT '[]',
    loaders        TEXT    NOT NULL DEFAULT '[]',
    file_name      TEXT    NOT NULL DEFAULT '',
    file_size      INTEGER NOT NULL DEFAULT 0,
    download_url   TEXT    NOT NULL DEFAULT '',
    sha1           TEXT    NOT NULL DEFAULT '',
    -- 被选择规则挑中 / 已经下到本地
    selected       INTEGER NOT NULL DEFAULT 0,
    local_name     TEXT    NOT NULL DEFAULT '',
    local_size     INTEGER NOT NULL DEFAULT 0,
    local_at       INTEGER NOT NULL DEFAULT 0,
    published_at   INTEGER NOT NULL DEFAULT 0
);

-- 「这个模组下选了哪些文件」和「还有哪些没下」是后台最常跑的两个查询
CREATE INDEX IF NOT EXISTS idx_mirror_files_mod ON mirror_files(mod_id, published_at);
CREATE INDEX IF NOT EXISTS idx_mirror_files_todo ON mirror_files(selected, local_name);

CREATE TABLE IF NOT EXISTS mirror_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,                -- sync | download
    -- 1 = 由每日自动同步起的，0 = 管理员手点的。
    -- ！要把这两者分开：后台列表里得能一眼看出「这一条是机器自己跑的」，
    --   否则到了晚上会莫名其妙多出一堆「操作人：自动」的记录，看着像被人登了号。
    auto        INTEGER NOT NULL DEFAULT 0,
    status      TEXT    NOT NULL DEFAULT 'running',  -- running | ok | failed
    scope       TEXT    NOT NULL DEFAULT '',     -- 限定单个模组时存 mod_id
    total       INTEGER NOT NULL DEFAULT 0,
    done        INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    detail      TEXT    NOT NULL DEFAULT '',
    operator    TEXT    NOT NULL DEFAULT '',
    started_at  INTEGER NOT NULL,
    -- 最后一次「有进展」的时间（v0.22 起）。
    -- ！卡死判定看的是这个，不是 started_at。
    --   看开始时间的后果：一个正常跑着的长任务（比如要下 24 个文件的下载任务）
    --   一旦超过阈值就会被当成卡死放行，于是两条任务同时跑，互相踩。
    --   而如果为了不被误判而把阈值调得很长，真的崩掉的任务又会堵住镜像功能很久。
    --   心跳把这两个问题一起解了：没进展才算卡死。
    touched_at  INTEGER NOT NULL DEFAULT 0,
    finished_at INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_mirror_jobs_started ON mirror_jobs(started_at);

-- 运行期设置：由管理员在界面上改、需要跨重启保留，但不属于「部署配置」的东西。
--
-- ！什么该放这里、什么不该：
--     该放  —— 能随时改、改了立刻生效、丢了也不影响站点能跑（例：每日自动同步的时间）
--     不该放 —— 数据库路径、密钥、口令源头这类启动前就要定的东西，
--              那些在 .env 里（见 backend/config.py），否则会出现
--              「配置一半在库里一半在文件里」这种谁都说不清的状态。
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL DEFAULT 0
);

-- ===========================================================================
-- 圈子（v0.34 起）
--
-- 「圈子」= 一群自己凑到一起的人 + 只有这群人看得见的帖子。
-- 它和「发现同好」是两条完全不同的路：
--   发现同好是**算法**把两个陌生人推到一起（1 对 1，靠匹配分）；
--   圈子是**人**自己挑一个主题进去（多对多，靠共同兴趣）。
--
-- ！为什么圈子帖复用 posts 表，而不是新开一张 circle_posts：
--   发帖那一套（富文本正文、配图、附件、制作人员、点赞、评论、转发、
--   频率限制、内容安全词库、后台下架、软删除）全都已经写好了，
--   而且是这个项目里最重的一块。新开一张表等于把这些全部重写一遍，
--   连带后台审核、通知、计数重算都要各写两份 —— 两份一定会跑偏。
--   所以圈子帖就是「posts 里 circle_id 不为 0 的那些行」，
--   唯一的区别是可见范围：它对**圈子成员**可见，对别人连存在都看不到。
--
-- ！circle_id 用 0 表示「不在任何圈子里」，而不是 NULL：
--   SQLite 里 NULL 参与比较永远为假，`circle_id != ?` 会把 NULL 行整片丢掉，
--   `circle_id IN (...)` 同理。0 让「公开时间线」这条过滤条件能写成
--   `p.circle_id = 0`，不用到处写 IS NULL / IS NOT NULL。
--   代价是它没法做外键（0 不是任何 circle 的 id），所以「圈子被解散之后
--   那些帖子怎么办」必须由代码显式处理 —— 见 circles.leave_circle。
CREATE TABLE IF NOT EXISTS circles (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    -- 一句话介绍，列表卡片上显示的那一行
    intro      TEXT    NOT NULL DEFAULT '',
    -- 详细说明，圈子主页显示。纯文本（换行会被渲染成分段）
    about      TEXT    NOT NULL DEFAULT '',
    -- 主题标签，取值来自 backend/circles.py 的 CIRCLE_TOPICS
    topic      TEXT    NOT NULL DEFAULT '',
    owner_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- open | deleted（解散 = 软删，和帖子一样：里面的帖子还要留着记录）
    status     TEXT    NOT NULL DEFAULT 'open',
    created_at INTEGER NOT NULL,
    -- 0 = 从没改过
    updated_at INTEGER NOT NULL DEFAULT 0
);

-- ！名字唯一，而且不区分大小写：站里同时存在「Redstone」和「redstone」
--   两个圈子，对用户来说就是同一个圈子被劈成了两半。
--   COLLATE NOCASE 只影响 ASCII 大小写，中文名本来就没有大小写之分。
--   代码里仍然会先查一次同名（给一句人话提示），这条索引是硬保证。
CREATE UNIQUE INDEX IF NOT EXISTS idx_circles_name
    ON circles(name COLLATE NOCASE);

-- 列表页固定是「按创建时间倒序取还在的」，这就是它的覆盖索引
CREATE INDEX IF NOT EXISTS idx_circles_list ON circles(status, created_at DESC);

-- 加入记录。主键就是 (圈子, 人) —— 天然保证一人一圈只加一次，
-- 不需要先查后插那种有缝隙的写法。
CREATE TABLE IF NOT EXISTS circle_members (
    circle_id INTEGER NOT NULL REFERENCES circles(id) ON DELETE CASCADE,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- owner | member。圈主也占一行，不然「圈子有几个人」要分两处数。
    role      TEXT    NOT NULL DEFAULT 'member',
    joined_at INTEGER NOT NULL,
    PRIMARY KEY (circle_id, user_id)
);

-- 「我加入了哪些圈子」是每次开圈子页都要跑的查询，这就是它的覆盖索引
CREATE INDEX IF NOT EXISTS idx_circle_members_user
    ON circle_members(user_id, joined_at DESC);

-- ===========================================================================
-- 帖子（v0.27 起）
--
-- 站内第一次出现「用户写的长内容」。之前所有用户产出的文字都是短字段
-- （简介、留言、反馈），没有一条能带图片、能被别人点赞评论。
--
-- 正文为什么存 JSON 而不是单开一张 post_blocks 表：
--   正文是【一个整体】—— 从来不单独查询、单独修改、单独排序某一块，
--   读写永远整篇进出。拆成表只会让「读一篇文章」从 1 次查询变成
--   N+1 次，而换不来任何查询能力。这和 users.play_styles 那几列是同一个判断。
--   代价：数据库里搜不到正文内部的结构 —— 但搜索本来就靠下面的 plain 列。
--
-- 计数为什么冗余在上面：
--   列表页每条帖子都要显示点赞/评论数，实时 COUNT() 就是 N+1 次查询。
--   代价是「计数可能与明细不一致」，所以每次变更都在【同一个事务】里同步，
--   并且后台提供一次重算（见 backend/posts.py 的 recount）。
CREATE TABLE IF NOT EXISTS posts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    author_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title         TEXT    NOT NULL,
    -- 分块正文：[{"type":"text","text":"..."},
    --           {"type":"image","file":"<32位hex>.png","caption":"..."}]
    body          TEXT    NOT NULL DEFAULT '[]',
    -- 正文抽出来的纯文本。列表摘要、搜索、敏感词复查都读它。
    -- ！不读 body：那是 JSON，直接 LIKE 会把 file 名和 caption 一起搜出来。
    plain         TEXT    NOT NULL DEFAULT '',
    -- published | hidden（管理员下架）| deleted（作者删除）
    -- ！删除用软删：已经有别人评论、点赞过的帖子直接物理删掉，
    --   会让那些人的通知点进去变成 404，而他们什么错都没有。
    status        TEXT    NOT NULL DEFAULT 'published',
    -- 这篇帖子发在哪个圈子里（v0.34 起）。0 = 公开发在「帖子」里。
    -- ！可见范围就靠这一列分流：
    --   circle_id = 0      → 全站可见（原来的帖子模块）
    --   circle_id = 圈子 id → 只有这个圈子的成员可见
    --   所以**任何**列出公开帖子的查询都必须显式带上 `circle_id = 0`，
    --   漏一处就等于把圈子里的私密内容捅到全站时间线上。
    --   为旧库补这一列见 TABLE_COLUMN_MIGRATIONS。
    circle_id     INTEGER NOT NULL DEFAULT 0,
    like_count    INTEGER NOT NULL DEFAULT 0,
    comment_count INTEGER NOT NULL DEFAULT 0,
    share_count   INTEGER NOT NULL DEFAULT 0,
    view_count    INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    -- 0 = 从没改过。非 0 时详情页标注「已修改」，和知乎一样。
    updated_at    INTEGER NOT NULL DEFAULT 0
);

-- 列表页固定是「按时间倒序取已发布的」，这就是它的覆盖索引
CREATE INDEX IF NOT EXISTS idx_posts_feed   ON posts(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_author ON posts(author_id, created_at DESC);

-- 制作人员。！必须是站内已注册用户（需求里明确要求）——
-- 用 user_id 外键而不是存一个名字字符串：存名字的话，
-- 对方改了昵称这里就永远对不上了，而且没法跳转到他的主页。
CREATE TABLE IF NOT EXISTS post_credits (
    post_id  INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- 这个人在这次创作里做了什么（建筑 / 红石 / 皮肤 / 测试……）。可留空。
    role     TEXT    NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (post_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_post_credits_user ON post_credits(user_id);

-- 上传过的图片登记表。
--
-- ！它存在的唯一理由是【清理】。正文 JSON 里当然能找到文件名，但那只覆盖
--   「已经发布出去」的图；用户传到一半关掉编辑器的那些，正文里根本不存在，
--   没有这张表就只能靠扫目录猜，而扫目录分不清孤儿图和正在写的草稿。
--   post_id 为空 = 还没被任何帖子引用，超过 24 小时就由清理任务删掉。
CREATE TABLE IF NOT EXISTS post_media (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    post_id    INTEGER REFERENCES posts(id) ON DELETE CASCADE,
    file_name  TEXT    NOT NULL UNIQUE,
    byte_size  INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_post_media_orphan ON post_media(post_id, created_at);

-- 点赞。主键就是 (帖子, 人) —— 天然保证一人一赞，不需要额外的去重逻辑。
CREATE TABLE IF NOT EXISTS post_likes (
    post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (post_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_post_likes_user ON post_likes(user_id, created_at DESC);

-- 评论。不做楼中楼：面向中小学生的场景里，多层嵌套既难读也难管，
-- 而且每一层都要单独处理举报与遮挡。平铺一层，按时间正序。
CREATE TABLE IF NOT EXISTS post_comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content    TEXT    NOT NULL,
    -- published | hidden（管理员下架）| deleted（作者或楼主删除）
    status     TEXT    NOT NULL DEFAULT 'published',
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_post_comments_feed ON post_comments(post_id, status, created_at);

-- 转发：把一篇帖子推给站内某个具体的人。
--
-- ！UNIQUE 三列而不是只 UNIQUE(post_id, to_user)：同一个人可以被
--   不同的人分别转同一篇帖子，那是正常的；但同一个人反复把同一篇
--   推给同一个收件人就是骚扰，这个唯一键正好把它挡在数据库层。
CREATE TABLE IF NOT EXISTS post_shares (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    from_user  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    to_user    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    note       TEXT    NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    UNIQUE (post_id, from_user, to_user)
);

CREATE INDEX IF NOT EXISTS idx_post_shares_to ON post_shares(to_user, created_at DESC);

-- ===========================================================================
-- 信息中心（v0.27 起）
--
-- 一条通知 = 「谁、在什么时候、对你做了一件什么事」。
--
-- ！payload 存的是事发当时的【快照】，不是外键。
--   外键指向的那条帖子可能被删、用户可能改昵称，而这些事后变化
--   不该让一条三天前的通知变得看不懂或者干脆无法渲染。
--   渲染时优先用快照，帖子还在则补上跳转链接。
--
-- ！actor_id 用 ON DELETE SET NULL 而不是 CASCADE：注销一个用户
--   不该顺手删掉别人收到的通知 —— 那等于「注销 = 抹掉自己骚扰过的痕迹」。
--   actor 没了就退化成一条匿名的系统消息。
CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- share（有人转发给你）| like | comment | credit（有人把你写进制作名单）| system
    kind       TEXT    NOT NULL,
    actor_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    -- 帖子没了就置空，通知本身留着
    post_id    INTEGER REFERENCES posts(id) ON DELETE SET NULL,
    payload    TEXT    NOT NULL DEFAULT '{}',
    -- 未读 = NULL。用时间戳而不是 0/1，顺带记下「什么时候读的」。
    read_at    INTEGER,
    created_at INTEGER NOT NULL
);

-- 未读角标和数据列表用的是同一个索引：先按人筛，再按读没读、按时间
CREATE INDEX IF NOT EXISTS idx_notifications_inbox ON notifications(user_id, read_at, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_requests_to     ON contact_requests(to_user, status);
CREATE INDEX IF NOT EXISTS idx_requests_from   ON contact_requests(from_user, status);
CREATE INDEX IF NOT EXISTS idx_connections     ON connections(user_low, user_high);
CREATE INDEX IF NOT EXISTS idx_sessions_token  ON sessions(token, expires_at);

-- ===========================================================================
-- v0.31 补的索引。
--
-- 先前的体检（EXPLAIN QUERY PLAN）实测出这几条都是 SCAN：
-- 数据量小的时候看不出来，但它们全部落在**每次开页面都会跑**的路径上，
-- 随着用户数增长会线性变慢。
-- ===========================================================================

-- ！登出 / 改密码 / 封号都要 DELETE FROM sessions WHERE user_id = ?，
--   而 sessions 只有 token 上的主键索引 —— 每次登出扫全表。
--   v0.30 加了「保持登录」之后令牌活 14 天，这张表只会越来越长。
CREATE INDEX IF NOT EXISTS idx_sessions_user   ON sessions(user_id);

-- ！这两条被 achievements.collect_stats 读，而那个函数每次开页面都要跑。
CREATE INDEX IF NOT EXISTS idx_reports_reporter ON reports(reporter_id);
CREATE INDEX IF NOT EXISTS idx_feedback_user    ON feedback(user_id);
-- 后台的反馈列表是「按状态筛 + 按时间倒序」，这就是它的覆盖索引
CREATE INDEX IF NOT EXISTS idx_feedback_status  ON feedback(status, created_at DESC);

-- ！collect_stats 用 WHERE user_low = ? OR user_high = ? 数伙伴数。
--   单个索引盖不住 OR 的两边，只能两边各给一个，让 SQLite 走 OR 优化。
CREATE INDEX IF NOT EXISTS idx_connections_high ON connections(user_high);

-- ！注销账号时要删干净（ON DELETE CASCADE / SET NULL）。
--   外键动作需要反查引用行，没有索引就只能逐表扫。
CREATE INDEX IF NOT EXISTS idx_notifications_actor ON notifications(actor_id);
CREATE INDEX IF NOT EXISTS idx_post_shares_from    ON post_shares(from_user);

-- ！v0.31 新增：发帖 / 评论的频率限制要「数这个人最近发了几条」。
--   已有的 idx_posts_author(author_id, created_at) 能直接用；
--   评论那边只有 (post_id, status, created_at)，开头是 post_id，用不上。
CREATE INDEX IF NOT EXISTS idx_post_comments_user ON post_comments(user_id, created_at DESC);

-- ===========================================================================
-- 启动器（v0.33 起）
--
-- 桌面启动器是站外程序，但它的「发布版本 / 功能开关 / 公告 / 使用统计」
-- 全部落在站点这一侧，由后台管理。启动器本身不带任何凭据。
--
-- ！这张表只存「一个版本发布了」这个事实，文件本身请放到对象存储 / 静态下载，
--   url 指向它即可。把几十 MB 的安装包塞进 SQLite 会让每次备份都变慢十倍。
-- ===========================================================================
CREATE TABLE IF NOT EXISTS launcher_releases (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    version        TEXT    NOT NULL,
    -- stable | beta。客户端默认只看 stable
    channel        TEXT    NOT NULL DEFAULT 'stable',
    notes          TEXT    NOT NULL DEFAULT '',
    url            TEXT    NOT NULL DEFAULT '',
    sha256         TEXT    NOT NULL DEFAULT '',
    byte_size      INTEGER NOT NULL DEFAULT 0,
    -- 1 = 这个版本必须更新（低于它就拦住，不让进主界面）
    forced         INTEGER NOT NULL DEFAULT 0,
    active         INTEGER NOT NULL DEFAULT 1,
    created_at     INTEGER NOT NULL,
    UNIQUE (channel, version)
);

CREATE INDEX IF NOT EXISTS idx_launcher_releases_live
    ON launcher_releases(active, channel, created_at DESC);

-- 公告：标题 + 正文，带生效时间窗。
-- ！正文是纯文本，客户端按行渲染，不支持 HTML —— 启动器不是浏览器，
--   允许 HTML 就等于给「谁能发公告」这个权限加了一条 XSS 的口子。
CREATE TABLE IF NOT EXISTS launcher_notices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT    NOT NULL,
    body       TEXT    NOT NULL DEFAULT '',
    -- info | warn | critical
    level      TEXT    NOT NULL DEFAULT 'info',
    active     INTEGER NOT NULL DEFAULT 1,
    starts_at  INTEGER NOT NULL DEFAULT 0,
    ends_at    INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    created_by TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_launcher_notices_live
    ON launcher_notices(active, created_at DESC);

-- 安装过启动器的设备。
-- ！device_id 是客户端首次运行时**本地随机生成**的 UUID，不是硬件指纹：
--   我们不收集 MAC / 硬盘序列号 / 主机名，删掉 userData 就换一个新身份。
--   这样「使用统计」能看趋势，但没法把某个设备钉死在某个真人身上 ——
--   面向未成年人做统计，这条线必须画清楚。
CREATE TABLE IF NOT EXISTS launcher_clients (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id    TEXT    NOT NULL UNIQUE,
    os           TEXT    NOT NULL DEFAULT '',
    arch         TEXT    NOT NULL DEFAULT '',
    app_version  TEXT    NOT NULL DEFAULT '',
    mc_version   TEXT    NOT NULL DEFAULT '',
    loader       TEXT    NOT NULL DEFAULT '',
    -- 启动器里登录了 mcfr 账号就顺手关联上；没登录就是 NULL。
    -- ！注销账号不删设备行（统计要连续），只把关联断掉。
    user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    first_seen   INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL,
    launch_count INTEGER NOT NULL DEFAULT 0,
    play_seconds INTEGER NOT NULL DEFAULT 0,
    last_ip      TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_launcher_clients_seen ON launcher_clients(last_seen DESC);

-- 事件流水：用来画「最近 14 天每天有多少人启动、启动了多少次」。
-- ！只保留粗粒度事件（start / launch / exit），不记录玩了哪个服务器、
--   玩了多久的明细 —— 那是玩家的游戏行为，不属于「启动器使用统计」。
CREATE TABLE IF NOT EXISTS launcher_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id  TEXT    NOT NULL,
    kind       TEXT    NOT NULL,
    detail     TEXT    NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_launcher_events_time ON launcher_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_launcher_events_kind ON launcher_events(kind, created_at DESC);

-- 微软设备码登录的会话（v0.33）。
--
-- ！微软的 client_id / 租户 ID 只存在于**服务器**，启动器一个都不知道。
--   客户端只拿到 flow_id（32 字节随机十六进制），拿它来轮询结果；
--   真正的 device_code / access_token 从不离开这台服务器。
--   flow_id 本身就是凭证，所以它必须足够长、且一次登录就作废。
CREATE TABLE IF NOT EXISTS launcher_ms_flows (
    id          TEXT PRIMARY KEY,
    device_id   TEXT    NOT NULL DEFAULT '',
    authority   TEXT    NOT NULL DEFAULT '',
    user_code   TEXT    NOT NULL DEFAULT '',
    device_code TEXT    NOT NULL DEFAULT '',
    -- pending | done | error
    status      TEXT    NOT NULL DEFAULT 'pending',
    interval_s  INTEGER NOT NULL DEFAULT 5,
    expires_at  INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    last_poll   INTEGER NOT NULL DEFAULT 0,
    result      TEXT    NOT NULL DEFAULT '',
    error       TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_launcher_flows_expire ON launcher_ms_flows(expires_at);
"""

# 这些索引依赖 migrate() 补上的新列，所以必须在补列之后再执行
POST_MIGRATION_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email) WHERE email != '';

-- 必须等 SCHEMA 里所有表都建好之后再建，否则新库会初始化失败
CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status, created_at);

-- ！v0.32 从 SCHEMA 搬过来的。SCHEMA 里的 admin_sessions 只建了四个基础列，
--   admin_id / admin_name / is_super 是 TABLE_COLUMN_MIGRATIONS 后面才补的；
--   而这个索引要的就是 admin_id，所以它必须等补列之后才能建。
--   放在 SCHEMA 里的后果：**全新安装直接建库失败**（no such column: admin_id），
--   而已经把列补过的老库却一切正常 —— 于是本地拿新库测就炸，线上毫发无伤，
--   反过来也完全说得通。上面 idx_reports_status 旁边那句注释说的就是同一件事，
--   v0.31 加索引时没把这条教训用上，这次补上。
CREATE INDEX IF NOT EXISTS idx_admin_sessions_admin ON admin_sessions(admin_id);

-- ！v0.34：posts.circle_id 是**迁移补的列**（见 TABLE_COLUMN_MIGRATIONS），
--   所以「圈子里的帖子，按时间倒序」这条索引必须写在这里、不能写进 SCHEMA。
--   写进 SCHEMA 的后果就是上面那段说过的第三种：新库没事（建表时列就在），
--   老库启动时 "no such column: circle_id" 直接崩。
--   判据只有一条：**索引的列如果是迁移补的，索引就必须在 POST_MIGRATION_SQL 里。**
CREATE INDEX IF NOT EXISTS idx_posts_circle ON posts(circle_id, created_at DESC);
"""

# 旧版本遗留、不再使用的表，每次启动顺手清掉（DROP IF EXISTS 是幂等的）
#   messages / friend_requests / friendships → v0.2 起改用「联系方式交换」
#   ms_binds                                → 微软账号绑定已整体取消（v0.9）
#   ms_device_flows                         → 微软登录已整体取消（v0.18）
#   email_verifications                     → 邮箱验证码已整体取消（v0.21）
#
# ！email_verifications 必须写在这里、而不是靠数据订正删：它的两个索引也在 SCHEMA 里，
#   而 SCHEMA 是每次启动整段执行的 —— 只要索引语句还在、表却没了，
#   全新安装会直接报 "no such table: main.email_verifications"。两处都得删干净。
LEGACY_TABLES = (
    "messages", "friend_requests", "friendships", "ms_binds", "ms_device_flows",
    "email_verifications",
)

# 「这个库应该有哪些表」从 SCHEMA 里直接正则扫出来，而不是另外维护一份名单。
#
# ！这里踩过一次坑：早先 upgrade.pending_work() 把待办的表写成硬编码的三个名字，
#   结果 v0.13 新增 admins / feedback / admin_permissions 三张表之后，
#   老库的 pending_work() 完全看不出差别 → 不走升级路径 → apply_schema() 不被调用
#   → 这三张表永远不会被建出来，启动时直接 "no such table: admins" 崩掉。
#   从 SCHEMA 推导就不会再有这个问题：加表只需改 SCHEMA 一处。
_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE
)
EXPECTED_TABLES: tuple[str, ...] = tuple(_TABLE_RE.findall(SCHEMA))

# ！索引也得从 SCHEMA 推导，理由和上面那张表一模一样（v0.31 补）：
#   run_upgrade() 在「没有待办」时会**直接返回**，根本不调 apply_schema()。
#   于是往 SCHEMA 里新加一条 CREATE INDEX，在**已经存在的部署上永远建不出来** ——
#   只有全新安装才会有。而本地测试用的恰恰是全新库，所以这个坑极难发现：
#   你会看到「本地明明快了，线上一点没变」。
#   同一个坑 v0.13 踩过一次（表那一半），索引这一半这次才补上。
_INDEX_RE = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
EXPECTED_INDEXES: tuple[str, ...] = tuple(
    dict.fromkeys(_INDEX_RE.findall(SCHEMA) + _INDEX_RE.findall(POST_MIGRATION_SQL))
)

# 增量迁移：列名 -> ADD COLUMN 语句
USER_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("contact_qq", "contact_qq TEXT NOT NULL DEFAULT ''"),
    ("contact_wechat", "contact_wechat TEXT NOT NULL DEFAULT ''"),
    ("contact_other", "contact_other TEXT NOT NULL DEFAULT ''"),
    ("contact_note", "contact_note TEXT NOT NULL DEFAULT ''"),
    ("allow_contact_request", "allow_contact_request INTEGER NOT NULL DEFAULT 1"),
    # ！email_verified_at 不在这里，而且不能加回来 ——
    #   它已被数据订正 v3 删除，加回来会让它在每次启动时重建。
    ("email", "email TEXT NOT NULL DEFAULT ''"),
    ("avatar_file", "avatar_file TEXT NOT NULL DEFAULT ''"),
    ("skin_file", "skin_file TEXT NOT NULL DEFAULT ''"),
    ("mc_uuid", "mc_uuid TEXT NOT NULL DEFAULT ''"),
    ("mc_name", "mc_name TEXT NOT NULL DEFAULT ''"),
    ("mc_skin_file", "mc_skin_file TEXT NOT NULL DEFAULT ''"),
    ("mc_cape_file", "mc_cape_file TEXT NOT NULL DEFAULT ''"),
    ("mc_skin_variant", "mc_skin_variant TEXT NOT NULL DEFAULT 'CLASSIC'"),
    ("mc_linked_at", "mc_linked_at INTEGER NOT NULL DEFAULT 0"),
    # 登录节律（v0.22 起）
    ("last_login_day", "last_login_day TEXT NOT NULL DEFAULT ''"),
    ("login_streak", "login_streak INTEGER NOT NULL DEFAULT 0"),
    ("login_days", "login_days INTEGER NOT NULL DEFAULT 0"),
    # 「我主动看过多少位同好」，与 profile_seen_count 方向相反
    ("profile_view_count", "profile_view_count INTEGER NOT NULL DEFAULT 0"),
    # 出生日期（v0.29 起）。年龄由它现算，见 backend/age.py。
    # ！改名或删掉这三列之前先想清楚：用户填过之后**不允许再改**，
    #   重建列会让所有人被迫重填一次。
    ("birth_year", "birth_year INTEGER NOT NULL DEFAULT 0"),
    ("birth_month", "birth_month INTEGER NOT NULL DEFAULT 0"),
    ("birth_day", "birth_day INTEGER NOT NULL DEFAULT 0"),
)

# 其它表的补列（表名, 列名, ADD COLUMN 语句）。
# ！admin_sessions 的三个新列默认值都是在向旧数据兼容：
#   旧版本只有一个「全局管理员密码」，那时登录进来的人就是超级管理员，
#   所以 is_super 默认给 1，让升级那一刻还活着的旧令牌不要凭空掉权限。
TABLE_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("admin_sessions", "admin_id", "admin_id INTEGER NOT NULL DEFAULT 0"),
    ("admin_sessions", "admin_name", "admin_name TEXT NOT NULL DEFAULT ''"),
    ("admin_sessions", "is_super", "is_super INTEGER NOT NULL DEFAULT 1"),
    ("feedback", "sent_ip", "sent_ip TEXT NOT NULL DEFAULT ''"),
    # v0.22：区分「自动同步」和「管理员手点」的任务
    ("mirror_jobs", "auto", "auto INTEGER NOT NULL DEFAULT 0"),
    ("mirror_jobs", "touched_at", "touched_at INTEGER NOT NULL DEFAULT 0"),
    # v0.28：附件要一个「展示用的原始文件名」。
    # ！不能只存在帖子正文里 —— 下载时要用它设置 Content-Disposition 的
    #   filename，而那条路由不读正文（它只认文件名），只能从库里取。
    ("post_media", "display_name", "display_name TEXT NOT NULL DEFAULT ''"),
    # v0.34：帖子所在圈子（0 = 公开发布在「帖子」模块里）。
    # ！它在 SCHEMA 的 CREATE TABLE posts 里也有 —— 那不是重复，是这套迁移的用法：
    #   新库靠 SCHEMA 一次建好，老库靠这一行补上，migrate() 会先问 PRAGMA 再决定。
    ("posts", "circle_id", "circle_id INTEGER NOT NULL DEFAULT 0"),
)

# ---------------------------------------------------------------------------
# 一次性数据订正
#
# 1. 删掉早期版本内置的 12 个演示账号（统一密码 mc123456）。
#    真实站点里这些假人只会干扰，而且会让人分不清哪些是真实用户。
# 2. 把已经废弃的微软绑定列清掉。
#
# 为什么用 SQLite 自带的 user_version 做标记、而不是每次启动都执行：
# 演示账号被清掉之后，真实用户完全可能注册同名账号（例如 steve_zhang），
# 按名字删会让他的账号在下次启动时凭空消失。
#
# ！新增订正步骤的规矩：
#   把「处理函数」写在下面，再到 DATA_MIGRATIONS 里登记一个从未使用过的
#   版本号；SCHEMA_VERSION 会自动跟着最后一个版本号走，不需要手工同步。
#   数据库停在任意历史版本上都能一路追平（逐版本判断，不是一个总开关）。
# ---------------------------------------------------------------------------

LEGACY_DEMO_USERNAMES: tuple[str, ...] = (
    "steve_zhang", "creeper_kai", "redstone_lin", "mia_builder",
    "survivor_wang", "pixel_mo", "map_zhou", "mod_chen",
    "parkour_yu", "builder_sun", "seed_luo", "nether_gu",
)

# 微软账号绑定取消后遗留的列，已无人使用
LEGACY_USER_COLUMNS: tuple[str, ...] = (
    "ms_uuid", "ms_name", "skin_url", "skin_variant", "cape_url", "ms_bound_at",
)


def now_ts() -> int:
    """当前 Unix 时间戳（秒）。"""
    return int(time.time())


def connect() -> sqlite3.Connection:
    """新建一个 SQLite 连接（每个请求独立使用）。

    注意 check_same_thread=False 是必需的：
    FastAPI 的生成器依赖（建连接 / 关连接）与同步路由函数分别在
    不同的线程池线程中执行，而 SQLite 默认禁止跨线程复用连接对象。
    这里每个请求独享一个连接、请求内部串行使用，因此关闭该检查是安全的。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 8000")

    # ---- 下面三条是 v0.31 加的，都是纯收益、不改任何语义 ----

    # 页缓存：默认只有 2MB（-2000 是 KiB，负号表示「按 KiB 算」）。
    # 整个数据库也就几百 KB～几 MB，给 24MB 基本等于把它整个装进内存。
    conn.execute("PRAGMA cache_size = -24000")

    # 排序 / 临时 B 树默认落到磁盘文件（temp_store = 0）。
    # 体检时「未读通知列表」那条就带着 USE TEMP B-TREE FOR ORDER BY。
    # 改成内存后这类排序不再产生磁盘 IO。
    conn.execute("PRAGMA temp_store = MEMORY")

    # 内存映射读。这是只读加速，不会改变写行为，出问题最多退回普通 pread。
    # ！给 256MB 是个上限值，不是预分配 —— 文件多大就映射多大。
    conn.execute("PRAGMA mmap_size = 268435456")

    return conn


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：按请求提供数据库连接。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def adopt_legacy_database() -> None:
    """把旧版本（blocklink.db）接管为 mcfr.db，避免用户数据看起来「消失」。"""
    if DB_PATH.exists() or not LEGACY_DB_PATH.exists():
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LEGACY_DB_PATH.rename(DB_PATH)
    for suffix in ("-wal", "-shm"):
        side = LEGACY_DB_PATH.with_name(LEGACY_DB_PATH.name + suffix)
        if side.exists():
            side.rename(DB_PATH.with_name(DB_PATH.name + suffix))
    print(f"[mcfr] 已接管旧数据库：{LEGACY_DB_PATH.name} -> {DB_PATH.name}")


# 旧名字，保留一个别名以防外部脚本还在引用
_adopt_legacy_database = adopt_legacy_database


def apply_schema(conn: sqlite3.Connection) -> None:
    """建表 + 补列 + 建索引，全部幂等。不动任何一行业务数据。

    刻意与「数据订正」分开：这一步可以在任何时刻反复执行，
    而数据订正只在版本号推进时才做一次。
    """
    conn.executescript(SCHEMA)
    migrate(conn)
    # 依赖新列的索引要等补列完成后再建
    conn.executescript(POST_MIGRATION_SQL)
    refresh_query_planner(conn)


def refresh_query_planner(conn: sqlite3.Connection) -> None:
    """刷新查询计划器的统计信息（v0.31）。

    ！不跑 ANALYZE 的话，SQLite 对每张表有多少行、值分布如何**一无所知**，
      只能按内置的默认假设挑索引 —— 表小的时候看不出差别，
      表一大就會选错（典型表现：明明有索引却去扫全表）。

    `PRAGMA optimize` 而不是裸的 `ANALYZE`：它会参考 sqlite_stat1 判断
    哪些表**真的需要**重新统计，没事的时候几乎不花时间。
    `analysis_limit` 则限制单表最多扫多少行，避免在几十万行的表上卡住启动。

    放在 apply_schema 末尾：那时索引已经建好，统计才有意义。
    """
    try:
        conn.execute("PRAGMA analysis_limit = 400")
        conn.execute("PRAGMA optimize")
    except sqlite3.Error:
        pass   # 统计信息只是性能提示，失败不该拖垮启动


def init_db() -> None:
    """建库 + 迁移（不给旧库做备份的「快路径」）。

    正常启动请走 `upgrade.run_upgrade()`：它会在动结构之前先备份一份，
    出问题能自动回滚。这里保留原样是为了让脚本 / 测试能直接拿到一个可用库。
    """
    adopt_legacy_database()
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        apply_schema(conn)
        purge_legacy_data(conn)
        conn.commit()
    finally:
        conn.close()


def missing_tables(conn: sqlite3.Connection) -> list[str]:
    """SCHEMA 里声明了、但这个库里还没有的表（按声明顺序返回）。"""
    existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return [name for name in EXPECTED_TABLES if name not in existing]


def missing_indexes(conn: sqlite3.Connection) -> list[str]:
    """SCHEMA / POST_MIGRATION_SQL 里声明了、但库里还没有的索引。

    ！它的存在只有一个理由：让**已有的库**也能发现「少了索引」。
      没有它的话，往 SCHEMA 里加索引在线上是静默无效的 —— 详见
      EXPECTED_INDEXES 上面那段注释。
    """
    existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    return [name for name in EXPECTED_INDEXES if name not in existing]


def migrate(conn: sqlite3.Connection) -> None:
    """轻量增量迁移。

    1. 给旧库的 users 表补齐新增列（联系方式等），避免用户数据丢失；
    2. 给其它表（admin_sessions 等）补齐新增列；
    3. 清理 v0.2 起被「联系方式交换」取代的私信 / 好友表。
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    for column, ddl in USER_COLUMN_MIGRATIONS:
        if column not in existing:
            conn.execute(f"ALTER TABLE users ADD COLUMN {ddl}")

    # ！表可能还不存在（迁移函数在 SCHEMA 之后跑，但顺序不能假设），
    #   所以每张表先问一次 PRAGMA；表不在就整组跳过，不要盲目 ALTER。
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    columns_cache: dict[str, set[str]] = {}
    for table, column, ddl in TABLE_COLUMN_MIGRATIONS:
        if table not in tables:
            continue
        if table not in columns_cache:
            columns_cache[table] = {
                row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
            }
        if column not in columns_cache[table]:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
            columns_cache[table].add(column)

    for table in LEGACY_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")


def schema_version(conn: sqlite3.Connection) -> int:
    """数据库当前所处的数据订正版本（SQLite 自带的 user_version）。"""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def pending_migrations(conn: sqlite3.Connection) -> list[SchemaStep]:
    """还没执行过的数据订正步骤（旧库从低版本追平时会有一串）。"""
    version = schema_version(conn)
    return [step for step in DATA_MIGRATIONS if step.version > version]


def purge_legacy_data(conn: sqlite3.Connection) -> list[SchemaStep]:
    """执行所有尚未做过的数据订正，返回实际执行的步骤。

    为什么按版本号逐条判断、而不是一个总开关：数据库可能停在任何历史版本上，
    已经做过 v1 的库仍然需要做 v2。具体原因见本文件上方注释。
    """
    todo = pending_migrations(conn)
    for step in todo:
        detail = step.apply(conn)
        print(f"[mcfr] 数据订正 v{step.version}：{step.name}" + (f" —— {detail}" if detail else ""))

    if todo:
        conn.execute(f"PRAGMA user_version = {DATA_MIGRATIONS[-1].version}")
    return todo


def _remove_demo_users(conn: sqlite3.Connection) -> str:
    marks = ", ".join("?" for _ in LEGACY_DEMO_USERNAMES)
    cursor = conn.execute(
        f"DELETE FROM users WHERE username IN ({marks})", LEGACY_DEMO_USERNAMES
    )
    removed = max(cursor.rowcount, 0)
    if not removed:
        return "本库没有内置演示账号"
    print(f"[mcfr] 已清除早期版本内置的 {removed} 个演示用户（相关会话与关系一并删除）")
    print("[mcfr] 如果你原来用的就是这些演示账号，请重新注册一个")
    return f"清除 {removed} 个演示账号"


def _drop_legacy_user_columns(conn: sqlite3.Connection) -> str:
    """微软登录取消后剩下的空列。

    DROP COLUMN 需要 SQLite 3.35+；老版本或任何异常都不该拦住服务启动，
    所以逐个包一层 try。删不掉也无所谓 —— 代码已经不读这些列了。
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    dropped = [c for c in LEGACY_USER_COLUMNS if c in existing]
    for column in dropped:
        try:
            conn.execute(f"ALTER TABLE users DROP COLUMN {column}")
        except sqlite3.Error:
            pass
    if not dropped:
        return "没有遗留列"
    print(f"[mcfr] 已清理取消微软登录后遗留的 {len(dropped)} 个空列")
    return f"移除 {len(dropped)} 个空列"


def _drop_email_verification_column(conn: sqlite3.Connection) -> str:
    """邮箱降级成普通资料字段（v0.21 起不再验证、不再发信）。

    留着 email_verified_at 会让下一个人以为「这个邮箱是验证过的」——
    它现在已经不对应任何事实，所以删掉。
    DROP COLUMN 需要 SQLite 3.35+，删不掉也无所谓（代码已经不读它），
    所以跟 v2 一样逐个包一层 try，不让它拦住服务启动。
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "email_verified_at" not in existing:
        return "这一列已经不在了"
    try:
        conn.execute("ALTER TABLE users DROP COLUMN email_verified_at")
    except sqlite3.Error as exc:
        return f"删不掉（{exc}）—— 不影响使用，代码已不再读写它"
    print("[mcfr] 邮箱已降级为普通资料字段，不再验证也不再发信")
    return "移除 email_verified_at 列"


class SchemaStep(NamedTuple):
    """一步数据订正。apply 返回一句给日志用的人话说明。"""

    version: int
    name: str
    detail: str
    apply: Callable[[sqlite3.Connection], str]


# ！顺序必须按版本号从小到大；新增步骤只能往后追加，绝不能插入或改动旧步骤 ——
#   已经升过级的库不会重跑旧步骤，改了也白改，还会让两台机器的库对不上。
DATA_MIGRATIONS: tuple[SchemaStep, ...] = (
    SchemaStep(1, "清除内置演示账号", "删掉早期版本自带的 12 个假账号", _remove_demo_users),
    SchemaStep(2, "移除微软登录遗留列", "微软登录已取消，相关空列不再需要", _drop_legacy_user_columns),
    SchemaStep(
        3,
        "邮箱降级为普通资料字段",
        "取消邮箱验证与发信，清掉验证时间戳",
        _drop_email_verification_column,
    ),
)

# 由步骤表推导，避免「加了步骤忘了改版本号」这种低级错误
SCHEMA_VERSION = DATA_MIGRATIONS[-1].version


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# ---------------------------------------------------------------- 运行期设置

def settings_map(conn: sqlite3.Connection, prefix: str = "") -> dict[str, str]:
    """读一批设置。prefix 用来只取某一组（例如 mirror.）。"""
    if prefix:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE ?", (f"{prefix}%",)
        ).fetchall()
    else:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row is not None else default


def set_settings(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    """写一批设置（UPSERT）。

    ！值统一当字符串存：这张表里混着开关、数字、日期，
      类型全靠读的一方自己转换。好处是加一项新设置不用改表结构，
      代价是「读的时候必须写死怎么解释」—— 所以调用方集中在
      mirror.auto_settings() 那一个地方转换，别到处 int()/bool()。
    """
    if not values:
        return
    stamp = now_ts()
    conn.executemany(
        "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
        " updated_at = excluded.updated_at",
        [(key, str(value), stamp) for key, value in values.items()],
    )
    conn.commit()


def json_list(raw: Any) -> list[str]:
    """把数据库里存的 JSON 字符串安全地还原成字符串列表。"""
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def parse_row_datetime(hour_value: Any) -> int:
    """保留字段迁移空间，当前返回整型。"""
    return int(hour_value or 0)
