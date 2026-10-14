"""FastAPI 应用入口：装配路由、静态前端与统一错误处理。"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, admins, upgrade
from . import posts as post_logic
from .achievements import catalog
from .database import DB_PATH, connect, get_db, now_ts
from .mirror import auto_settings, auto_sync_loop
from .routers import (
    admin, auth, captcha, contacts, discover, feedback, media, mirror, posts, users,
)
from . import age
from .schemas import EDITIONS, LOOKING_FOR, ONLINE_TIMES, PLAY_STYLES

BRAND = "mcfr"

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

EDITION_LABELS = {"java": "Java 版", "bedrock": "基岩版", "both": "双版本通吃"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 升级数据库：先备份、再迁移、再自检，任何一步出错都会自动回滚到升级前。
    # 具体实现在 backend/upgrade.py，那里有为什么这么做的完整说明。
    try:
        report = upgrade.run_upgrade(trigger="startup")
    except Exception as exc:
        # 升级失败时不要直接把服务弄不起来：数据库已经被回滚成升级前的样子了，
        # 用旧版本继续跑，用户至少能正常使用；同时把问题写清楚。
        # 这里特意捕获 Exception 而不是只捕获 UpgradeError ——
        # 磁盘满、文件被占用之类的意外也不该让整个站点打不开。
        print(f"[{BRAND}] !! {exc}")
        print(f"[{BRAND}] !! 服务将以升级前的数据库继续启动，请尽快处理上面这条错误")
    else:
        if report["status"] == "up-to-date":
            print(f"[{BRAND}] 数据库已是最新结构（v{report['to_version']}，{report['users']} 个用户）")

    print(f"[{BRAND}] 数据库位置：{DB_PATH}")

    # 管理员账号体系：保证超级管理员存在，并兼容旧库里那些「全局密码」时代留下的会话
    conn = connect()
    try:
        admins.ensure_super_admin(conn)
        admins.seed_from_env_password(conn)
        conn.commit()
        total = int(conn.execute("SELECT COUNT(*) FROM admins").fetchone()[0])
    finally:
        conn.close()
    print(f"[{BRAND}] 管理员账号：{total} 个（超级管理员 {admins.SUPER_USERNAME}）")

    # ！v0.21 起这里不再有邮件那一行。
    #   原来它会打印 SMTP 主机 / 端口 / 加密方式 / 账号，用来一眼核对配置 ——
    #   现在整条邮件链路都删了，一个 SMTP 参数都不再读，
    #   再把它们打出来只会让人以为还有东西在跑。
    #   注册和登录的成本就是一张验证码，不联网、不花钱、不会被对方拒。
    print(f"[{BRAND}] 注册 / 登录：图片验证码（本站生成，不依赖任何外部服务）")

    # 模组镜像的「每日自动同步」：一个会睡觉的后台线程，实现见 mirror.auto_sync_loop。
    # ！不过它自己有没有开：开关在数据库里，管理员随时可以改。
    #   这里只负责把线程拉起来，并把当前状态打出来 ——「一直没同步」
    #   和「开关其实关着」是两件很容易混淆的事，开机的这一行能直接分清。
    stop_auto = threading.Event()
    auto_thread = threading.Thread(
        target=auto_sync_loop, args=(stop_auto,),
        name="mirror-auto-sync", daemon=True,
    )
    auto_thread.start()
    try:
        _conn = connect()
        try:
            _auto = auto_settings(_conn)
        finally:
            _conn.close()
        if _auto["enabled"]:
            print(f"[{BRAND}] 模组镜像：每天 {_auto['hour']:02d}:00 自动同步下载量前 {_auto['limit']} 名")
        else:
            print(f"[{BRAND}] 模组镜像：未开启每日自动同步（可在后台「模组镜像」里打开）")
    except Exception as exc:  # noqa: BLE001
        print(f"[{BRAND}] 模组镜像：读取自动同步设置失败（{type(exc).__name__}: {exc}）")

    # ！这里不写端口。用 --port 换过端口时，写死的那行就是错的 ——
    #   而 run.py 已经打印过真实地址了，留一个错的反而让人以为没生效。
    print(f"[{BRAND}] 就绪，地址见上面那行「服务启动中」")
    yield
    # 关服时把调度线程叫醒收工。daemon 线程本来就活不过主进程，
    # 但显式停一下，测试和 --reload 场景不会留下一堆睡着的线程。
    stop_auto.set()


# /api/docs 会把整套接口结构摆给任何人看 —— 相当于一份现成的接口清单。
# 自己开发调试时很有用，所以留个开关，但默认关掉。
DOCS_ENABLED = (os.environ.get("MCFR_ENABLE_DOCS") or "").strip().lower() in (
    "1", "true", "yes", "on",
)

app = FastAPI(
    title="mcfr · Minecraft 同好匹配",
    description="面向中小学生《我的世界》玩家的安全交友系统",
    version=__version__,
    docs_url="/api/docs" if DOCS_ENABLED else None,
    openapi_url="/api/openapi.json" if DOCS_ENABLED else None,
    redoc_url=None,
    lifespan=lifespan,
)

# 内容安全策略。
#
# 本站不引任何外部资源（无 CDN、无外部字体），所以可以写得很紧：
#   script-src 'self'           —— 脚本只能来自本站；内联脚本一概不允许。
#                                  这就是主题防闪烁那段代码被抽成
#                                  js/theme-boot.js 外部文件的原因。
#   style-src 'unsafe-inline'   —— 样式属性（style="..."）用得很多，这一项
#                                  只能放开；CSS 注入的危害比脚本小得多。
#   img-src data: blob:         —— favicon 是内联 SVG（data:），头像预览走 blob:
#   frame-ancestors 'none'      —— 不允许被任何页面套进 iframe
CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

# 这些路径【允许抓取，但不许收录】。
#
#   · 六个 SPA 路由 —— 服务端返回的都是同一份 index.html，未登录的访客
#     看到的全是登录页。收进去只会变成一堆互相打架的重复内容。
#   · /admin —— 后台入口，没有任何理由出现在搜索结果里。
#
# ！为什么用响应头而不是 robots.txt 的 Disallow：
#   两者解决的是不同的问题 —— Disallow 是「别来抓」，noindex 是「别收录」。
#   而它们会互相打架：被 Disallow 的路径爬虫根本抓不到，也就永远看不到
#   这个 noindex，于是当别的网站链过来时，它仍然可能出现在结果页里
#   （只是没有摘要文字）—— 而你还以为已经挡住了。
#   首页不在这个名单里，它是唯一应该被收录的页面。
NOINDEX_PATHS = frozenset({
    "/admin",
    "/discover", "/partners", "/achievements", "/mirror", "/me", "/register",
    "/posts", "/messages",
})

# ！带参数的路由没法写进上面那个集合（/posts/123 的 123 是变的），
#   所以另外留一份前缀名单。漏掉它的后果：每一篇帖子都会变成一个
#   可收录的独立页面 —— 而爬虫看到的其实全是同一份登录页。
NOINDEX_PREFIXES = ("/posts/",)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """给每个响应补上通用安全头。

    这些头不解决某个具体 bug，它们是「万一别处真的漏了」时的第二道墙：
    比如哪天前端渲染用户内容时忘了转义，CSP 里的 script-src 'self'
    会让注入进来的 <script> 跑不起来、onerror= 也不会执行。
    """
    response = await call_next(request)
    headers = response.headers
    # 用 setdefault：将来某个路由自己设了更细的值时，不要把它覆盖掉
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "same-origin")
    headers.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
    headers.setdefault("Content-Security-Policy", CSP)

    # HSTS：告诉浏览器「这个域名以后只准走 https」。
    #
    # ！只在这次请求确实是 https 的时候发。在 http 上也发的话，
    #   用户拿 127.0.0.1 访问一次、或者证书还没配好时打开一次页面，
    #   浏览器就会把「只准 https」记死在这个域名上，然后自己把自己关在外面 ——
    #   要手工清浏览器设置才能走出来，而用户根本不知道发生了什么。
    #
    # ！写在这里而不是 nginx：certbot 每次都会重写那份 nginx 配置，
    #   server 级的 add_header 很容易被它顺手覆盖掉，而应用层的头不会。
    #   注意 nginx 的 add_header 会覆盖上游 —— 所以 nginx 那边【不要】再加，
    #   只保留它的反代职责。
    scheme = os.environ.get("MCFR_FORCE_SCHEME", "").strip() or \
        request.headers.get("x-forwarded-proto", "").split(",")[0].strip() or \
        request.url.scheme
    if scheme == "https":
        headers.setdefault("Strict-Transport-Security", "max-age=31536000")

    # ！前端资源必须「每次用之前先回源问一下」。
    #
    #   Starlette 的 StaticFiles 只发 ETag / Last-Modified，不发 Cache-Control。
    #   而「有 Last-Modified、没有 Cache-Control」的响应会被浏览器**启发式缓存**，
    #   新鲜期大致是「距上次修改时长的 10%」——文件越老缓存越久。
    #   后果是刚改完的 js/css 在浏览器里还是旧的，刷新也没用，
    #   调试时极易误判成「代码写错了」。这个项目没有构建步骤，
    #   文件名里也没有哈希，靠缓存头是唯一的办法。
    #
    #   no-cache 不是「不缓存」，而是「缓存，但每次使用前先校验」：
    #   带着 ETag 去问，没变就是 304，几乎不花时间，但绝不会拿到旧代码。
    #   带哈希的媒体文件和不可变的镜像文件除外 —— 别覆盖它们自己的设置。
    path = request.url.path
    if not (path.startswith("/api/media/")
            or path.startswith("/api/mirror/files/")
            or path.startswith("/api/mirror/icons/")):
        headers.setdefault("Cache-Control", "no-cache")

    # 机器人指令，见上面 NOINDEX_PATHS 的说明。
    if path in NOINDEX_PATHS or path.startswith(NOINDEX_PREFIXES):
        headers.setdefault("X-Robots-Tag", "noindex")

    return response


# 跨域。
#
# 前后端由同一个进程提供（FastAPI 既发 API 又发前端静态文件），
# 正常部署根本用不到跨域。这里原来是 allow_origins=["*"]，
# 等于告诉浏览器「任何网站都可以用 JS 调这套 API」。
#
# 因为本站用的是 Bearer 令牌而不是 Cookie，别的站点拿不到你的令牌，
# 所以它**不是一个可直接利用的漏洞**；但它也没有任何好处，
# 而且哪天有人把认证改成 Cookie，它会立刻变成一个真实的 CSRF 缺口。
# 所以默认不开；确实需要时在 .env 里显式列出：
#     MCFR_ALLOWED_ORIGINS=http://127.0.0.1:5173
ALLOWED_ORIGINS = [
    item.strip()
    for item in (os.environ.get("MCFR_ALLOWED_ORIGINS") or "").split(",")
    if item.strip()
]
if ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Filename"],
    )


@app.exception_handler(RequestValidationError)
async def on_validation_error(request, exc: RequestValidationError):
    """把 Pydantic 的英文报错压成一句中文提示，前端直接弹给用户看。"""
    message = "提交的内容不符合要求，请检查后重试"
    errors = exc.errors()
    if errors:
        raw = str(errors[0].get("msg") or "").replace("Value error, ", "").strip()
        if raw:
            message = raw
    return JSONResponse(status_code=422, content={"detail": message})


@app.exception_handler(post_logic.PostError)
async def on_post_error(request: Request, exc: post_logic.PostError):
    """帖子相关的业务错误 → 一条能直接给用户看的 HTTP 错误。

    ！统一在这里转，而不是在每个路由里写 try/except：
      十几个路由都包一层的话，任何一处漏写都会变成一个 500，
      而 500 在前端只会显示成「操作失败，请稍后重试」——
      用户完全不知道是自己哪里填错了。
    """
    return JSONResponse(status_code=exc.status, content={"detail": str(exc)})


app.include_router(auth.router)
app.include_router(captcha.router)
app.include_router(users.router)
app.include_router(discover.router)
app.include_router(contacts.router)
app.include_router(feedback.router)
app.include_router(admin.router)
app.include_router(media.router)
app.include_router(mirror.router)
app.include_router(mirror.admin_router)
app.include_router(posts.router)


@app.get("/admin", include_in_schema=False)
def admin_page():
    """管理后台入口页面（需要管理员密码才能看到数据）。"""
    from fastapi.responses import FileResponse

    return FileResponse(FRONTEND_DIR / "admin.html")


# 前端路由：每个功能页都有自己的 URL，
# 直接访问、刷新、收藏、分享链接都能正常打开
@app.get("/discover", include_in_schema=False)
@app.get("/partners", include_in_schema=False)
@app.get("/achievements", include_in_schema=False)
@app.get("/me", include_in_schema=False)
@app.get("/register", include_in_schema=False)
@app.get("/mirror", include_in_schema=False)
@app.get("/posts", include_in_schema=False)
@app.get("/posts/new", include_in_schema=False)
@app.get("/messages", include_in_schema=False)
def spa_route():
    from fastapi.responses import FileResponse

    return FileResponse(FRONTEND_DIR / "index.html")


# 帖子详情：/posts/<id>。
# ！必须在这个函数【之后】声明 —— /posts/new 也是个路径，
#   把它写在下面会被 {post_id} 抢走，然后「new」解析失败直接 422。
@app.get("/posts/{post_id}", include_in_schema=False)
def post_detail_page(post_id: str):
    from fastapi.responses import FileResponse

    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/posts/{post_id}/edit", include_in_schema=False)
def post_edit_page(post_id: str):
    from fastapi.responses import FileResponse

    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/health", tags=["系统"], summary="健康检查")
def health():
    return {"ok": True, "version": __version__}


@app.get("/api/version", tags=["系统"], summary="前后端版本号（用于判断页面是不是缓存的旧版）")
def version():
    return {
        "app_version": __version__,
        "schema_version": upgrade.system_status()["schema_version"],
        "target_schema_version": upgrade.SCHEMA_VERSION,
    }


def _public_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """首页展示用的公开统计，不含任何个人信息。"""
    stamp = now_ts()
    return {
        "players": int(conn.execute("SELECT COUNT(*) FROM users WHERE status = 'active'").fetchone()[0]),
        "online": int(
            conn.execute(
                "SELECT COUNT(*) FROM users WHERE status = 'active' AND show_online = 1 AND last_seen > ?",
                (stamp - 300,),
            ).fetchone()[0]
        ),
        "connections": int(conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0]),
        "exchanged": int(
            conn.execute(
                "SELECT COUNT(*) FROM contact_requests WHERE status = 'accepted'"
            ).fetchone()[0]
        ),
    }


@app.get("/api/meta", tags=["系统"], summary="前端可用的选项字典与公开统计")
def meta(conn: sqlite3.Connection = Depends(get_db)):
    return {
        "editions": [{"value": item, "label": EDITION_LABELS[item]} for item in EDITIONS],
        # 注册/补填年龄时要用的边界与成年线（v0.29）。
        # ！上面前端不能写死这几个数字 —— 改成年线时只改 backend/age.py 一处。
        "age": {"min": age.MIN_AGE, "max": age.MAX_AGE, "adult": age.ADULT_AGE},
        "play_styles": list(PLAY_STYLES),
        "online_times": list(ONLINE_TIMES),
        "looking_for": list(LOOKING_FOR),
        "report_reasons": list(contacts.REPORT_REASONS),
        "achievements": catalog(),
        "brand": BRAND,
        "version": __version__,
        "privacy_note": (
            "注册不收集真实姓名、学校、住址与手机号；邮箱是选填的，可不填。"
            "联系方式默认隐藏，只有你同意后才会发送给对方。"
        ),
        "stats": _public_stats(conn),
    }


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
