/* =========================================================================
   mcfr · 前端主逻辑
   无构建依赖的原生 JS 单页应用：状态 → 渲染 → 事件委托。

   v0.2 模型：没有站内私信。玩家在资料里预置联系方式，
   别人发起「交换申请」，本人同意后才把联系方式发送出去。
   ========================================================================= */

(() => {
  "use strict";

  /* ============================================================
     一、基础工具
     ============================================================ */

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[ch]);
  }

  /* ---- 标签切换动画（实现都在 effects.js 里）----
     effects.js 排在 app.js 之前加载，所以这里直接拿得到。
     每个入口都判一下空：动画拿不到也不该拖垮主流程。 */
  const FX = window.McfrEffects || {};
  const tabBar = (el) => FX.tabBar && FX.tabBar(el);
  const tabIn = (el, dir) => FX.tabIn && FX.tabIn(el, dir);
  const tabDir = (el) => (FX.tabDir ? FX.tabDir(el) : 0);
  const tabSwap = (bar, panel) => (FX.tabSwap ? FX.tabSwap(bar, panel) : 0);

  /* 多语言。i18n.js 排在 app.js 之前加载，所以这里直接拿得到。
     主站文案不逐句接键，渲染完之后统一扫 DOM —— 理由写在 i18n.js 里。 */
  const I18n = window.McfrI18n || { t: (text) => text, applyDynamic() {} };

  /* 像素方块头像：Minecraft 头部正面的 5×5 简化图案。
     1 = 皮肤主色  2 = 眼睛  3 = 嘴  4 = 头顶发色 */
  const FACE = [
    [4, 4, 4, 4, 4],
    [1, 1, 1, 1, 1],
    [1, 2, 1, 2, 1],
    [1, 1, 1, 1, 1],
    [1, 1, 3, 1, 1],
  ];

  /* 头像，三档优先级：
       ① 用户自己上传的头像图片       user.avatar_url
       ② 用户上传的 Minecraft 皮肤     user.skin_url（取皮肤里的头）
       ③ 根据用户名生成的像素脸（默认）
     第一个参数既可以是完整的用户对象，也可以只是一个 hue 数字。 */
  function faceGrid(hue) {
    const palette = {
      1: `hsl(${hue} 60% 62%)`,
      2: "hsl(240 26% 9%)",
      3: `hsl(${hue} 44% 30%)`,
      4: `hsl(${hue} 52% 44%)`,
    };
    return `<div class="mcface-grid">` +
      FACE.flat().map((value) => `<i style="--c:${palette[value]}"></i>`).join("") +
      `</div>`;
  }

  function avatar(userOrHue, size) {
    const user = userOrHue && typeof userOrHue === "object" ? userOrHue : { avatar_hue: userOrHue };
    const hue = user.avatar_hue ?? 190;
    const sizeClass = size ? ` mcface--${size}` : "";

    if (user.avatar_url) {
      return `<div class="mcface${sizeClass}" style="--hue:${hue}" aria-hidden="true">
        <img class="mcface-img" src="${esc(user.avatar_url)}" alt="">
      </div>`;
    }

    if (user.skin_url) {
      return `<div class="mcface mcface--skin${sizeClass}" style="--hue:${hue}" aria-hidden="true">
        <canvas class="mcface-canvas" data-skin-head="${esc(user.skin_url)}"></canvas>
      </div>`;
    }

    return `<div class="mcface${sizeClass}" style="--hue:${hue}" aria-hidden="true">${faceGrid(hue)}</div>`;
  }

  /* 图片文件没了的时候（换服务器时漏拷、用户删了文件、从旧机器迁移……），
     <img> 会显示成一个破图标 —— 比没有头像还难看。
     ！只能在**捕获阶段**听 error 事件：项目 CSP 是 script-src 'self'，
       行内 onerror 会被直接拦掉，这是唯一可行的做法。 */
  document.addEventListener("error", (event) => {
    const img = event.target;
    if (!img || img.tagName !== "IMG" || !img.classList.contains("mcface-img")) return;
    const host = img.parentElement;
    if (!host || host.dataset.faceFallback) return;   // 防自我循环
    host.dataset.faceFallback = "1";
    const hue = host.style.getPropertyValue("--hue") || 190;
    host.classList.remove("mcface--skin");
    host.innerHTML = faceGrid(hue);
  }, true);

  /* 皮肤文件没了 → skinview.js 那边只会把 canvas 清空（一块白方块）。
     它发 mcfr:skin-missing 报信，这里换成生成的像素脸 ——
     和上面那条 img 失败的处理是同一个目的。 */
  document.addEventListener("mcfr:skin-missing", (event) => {
    const canvas = event.target;
    const host = canvas && canvas.parentElement;
    if (!host || host.dataset.faceFallback) return;
    host.dataset.faceFallback = "1";
    const hue = host.style.getPropertyValue("--hue") || 190;
    host.classList.remove("mcface--skin");
    host.innerHTML = faceGrid(hue);
  });

  function skeleton(height) {
    return `<div class="skeleton" style="height:${height}px"></div>`;
  }

  /* 加载指示器：一个真的在转的方块（六个面用 CSS 3D 摆出来）。
     骨架条留着 —— 方块说「正在拉数据」，骨架条说「数据大概长这个形状」，
     两件事不重复。减少动态时方块停下来，意思由骨架条撑（见 minecraft.css）。 */
  function cubeLoader() {
    return `<div class="cube-loader" role="status" aria-label="加载中"><i>${'<b></b>'.repeat(6)}</i></div>`;
  }

  /* html 不为空时改用 innerHTML——留给成就弹窗那种带结构的提示 */
  function toast(message, kind = "ok", html = null) {
    const host = $("#toast-host");
    const node = document.createElement("div");
    node.className = `toast toast--${kind}`;
    if (html) node.innerHTML = html;
    else node.textContent = message;
    host.appendChild(node);
    I18n.applyDynamic(node);
    window.setTimeout(() => {
      node.style.transition = "opacity .3s, transform .3s";
      node.style.opacity = "0";
      node.style.transform = "translateY(10px)";
      window.setTimeout(() => node.remove(), 340);
    }, html ? 4200 : 3400);
  }

  /* 成就解锁用 MC「成就达成」那种弹窗：左边格子里装徽记，右边三行小字 */
  function toastUnlocked(list) {
    (list || []).forEach((item, index) => {
      window.setTimeout(() => {
        toast(`成就解锁 · ${item.name}  +${item.points} 积分`, "advancement", `
          <span class="slot slot--sm"><span class="glyph" style="${McfrIcons.toneStyle(item.code)}">${McfrIcons.badge(item.code, item.glyph || "◆")}</span></span>
          <span class="adv-body">
            <span class="adv-cap">成就达成</span>
            <span class="adv-name">${esc(item.name)}</span>
            <span class="adv-pts">+${Number(item.points) || 0} 积分</span>
          </span>`);
      }, index * 320);
    });
  }

  function fmtTime(ts) {
    if (!ts) return "";
    const date = new Date(ts * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    return `${date.getMonth() + 1}月${date.getDate()}日 ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function fmtRelative(ts) {
    if (!ts) return "";
    const diff = Math.floor(Date.now() / 1000) - Number(ts);
    if (diff < 60) return "刚刚";
    if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
    if (diff < 604800) return `${Math.floor(diff / 86400)} 天前`;
    const date = new Date(ts * 1000);
    return `${date.getMonth() + 1}月${date.getDate()}日`;
  }

  function chipRow(options, selected = []) {
    return options
      .map((option) => {
        const on = selected.includes(option);
        return `<button type="button" class="chip" data-chip aria-pressed="${on}">${esc(option)}</button>`;
      })
      .join("");
  }

  function presenceDot(online) {
    return `<span class="presence${online ? " presence--on" : ""}" title="${online ? "在线" : "离线"}"></span>`;
  }

  /* 联系方式卡片：只有已交换的同好才会看到这段内容 */
  function contactRows(contact, nickname) {
    const rows = [];
    if (contact && contact.qq) rows.push(["QQ", contact.qq]);
    if (contact && contact.wechat) rows.push(["微信", contact.wechat]);
    if (contact && contact.other) rows.push(["其他", contact.other]);

    if (!rows.length) {
      return `<p class="hint">${esc(nickname)} 还没有预置联系方式，可以提醒 TA 去填写。</p>`;
    }

    return `
      <div class="contact-list">
        ${rows.map(([label, value]) => `
          <div class="contact-row">
            <span class="contact-label">${label}</span>
            <span class="contact-value">${esc(value)}</span>
            <button class="btn btn-ghost btn-sm" data-act="copy" data-copy="${esc(value)}">复制</button>
          </div>`).join("")}
        ${contact.note ? `<p class="contact-note">TA 的备注：${esc(contact.note)}</p>` : ""}
      </div>`;
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast(`已复制：${text}`, "ok");
    } catch (error) {
      toast("复制失败，请手动选中文本复制", "bad");
    }
  }

  /* ============================================================
     二、全局状态
     ============================================================ */

  const State = {
    view: "discover",
    overview: null,
    discover: [],
    // 搜索结果。和 discover 分开存：卡片上的按钮只带 id，
    // 要反查完整对象时两个列表都得找，见 findUser()。
    search: { query: "", items: [] },
    // 模组镜像：列表、当前打开的模组、详情页上的筛选条件
    mirror: [],
    mirrorCurrent: null,
    mirrorFilter: { loader: "", version: "" },
    connections: [],
    requestsIn: [],
    requestsOut: [],
    partnerTab: "connections",
    registerStep: 1,
    requestQueue: null,
    requestIndex: 0,
    // 这一次进页面要不要无视缓存重新拉数据。见 setView 里的说明。
    forceFresh: false,
    // 当前打开的帖子 id（0 = 没在某个帖子上）。路由解析出来后落在这里，
    // renderPost / renderEditor 再去读。
    routePostId: 0,
    // 当前打开的圈子 id（0 = 不在圈子里）。和 routePostId 是同一个机制：
    // /circles/7 和 /circles/7/new 这两种地址都靠它。
    routeCircleId: 0,
    posts: { items: [], total: 0, page: 1, hasMore: false, query: "", mineOnly: false },
    post: null,
    postComments: { items: [], total: 0, page: 1, hasMore: false },
    // 圈子（v0.34）：列表 + 当前打开的圈子 + 这个圈子里的帖子。
    // ！tab 只有 "mine" / "all" 两档，切档时 items 必须清空 ——
    //   不清的话会先看到上一档的卡片，再被新数据换掉，闪一下。
    circles: { tab: "mine", query: "", items: [], total: 0, page: 1, hasMore: false },
    circle: null,
    circlePosts: { items: [], total: 0, page: 1, hasMore: false },
    inbox: { items: [], total: 0, page: 1, hasMore: false, box: "all", unread: 0 },
    // 编辑器工作台。切走再回来时它还在 —— 这就是「草稿」的全部实现。
    editor: null,
    shareTargets: [],
    mediaCount: 0,
  };

  /* 哪些列表已经真正拉过一次了。
     ！不能用「数组是不是空的」来判断 —— 空数组既可能是「还没拉」，
       也可能是「拉过但确实一条都没有」。混着用就会对着空数据闪占位条。 */
  const Loaded = {
    discover: false,
    partners: false,
  };

  /* 侧栏图标是线条 SVG（指南针 / 交剑 / 奖杯 / 玩家头颅），由 js/mcicons.js 提供。
     这里只留 key —— 图标不再是“一个字符”，所以不能写在数据里。 */
  const NAV = [
    { key: "discover", label: "发现同好", badge: null },
    { key: "circles", label: "圈子", badge: null },
    { key: "posts", label: "帖子", badge: null },
    { key: "mirror", label: "模组镜像", badge: null },
    { key: "partners", label: "我的伙伴", badge: "pending_requests" },
    { key: "messages", label: "消息", badge: "unread_notifications" },
    { key: "achievements", label: "成就", badge: null },
    { key: "me", label: "我的", badge: null },
  ];

  // 每个功能页对应一个真实 URL，方便收藏、分享和用浏览器标签页分别打开
  const ROUTES = {
    "/discover": "discover",
    "/circles": "circles",
    "/posts": "posts",
    "/messages": "messages",
    "/mirror": "mirror",
    "/partners": "partners",
    "/achievements": "achievements",
    "/me": "me",
  };

  /* ！帖子详情、帖子编辑、圈子主页这三个页面【不能】靠 ROUTES 这张表：
     它们的路径里有一个变化的数字（/posts/123、/circles/7），
     而表里的键是固定字符串。所以单独用正则解析，并且
     返回的是一个带参数的对象 —— 只返回页面名的话，
     「第 1 个圈子」和「第 2 个圈子」在 setView 看来是一模一样的。 */
  function routeFromPath(path) {
    const clean = (path || "/").replace(/\/+$/, "") || "/";
    if (clean === "/posts/new") return { view: "editor", postId: 0, circleId: 0 };
    const editing = /^\/posts\/(\d+)\/edit$/.exec(clean);
    if (editing) return { view: "editor", postId: Number(editing[1]), circleId: 0 };
    const detail = /^\/posts\/(\d+)$/.exec(clean);
    if (detail) return { view: "post", postId: Number(detail[1]), circleId: 0 };
    // 在圈子里写帖子：/circles/7/new
    const circleNew = /^\/circles\/(\d+)\/new$/.exec(clean);
    if (circleNew) return { view: "editor", postId: 0, circleId: Number(circleNew[1]) };
    const circleDetail = /^\/circles\/(\d+)$/.exec(clean);
    if (circleDetail) return { view: "circle", postId: 0, circleId: Number(circleDetail[1]) };
    return { view: ROUTES[clean] || "discover", postId: 0, circleId: 0 };
  }

  function pathForView(view, postId, circleId) {
    if (view === "post" && postId) return `/posts/${postId}`;
    if (view === "circle" && circleId) return `/circles/${circleId}`;
    if (view === "editor") {
      // ！顺序要紧：编辑已有帖子永远走 /posts/<id>/edit。
      //   反过来的话，从一个圈子主页点完「编辑」再回编辑器，
      //   地址栏会变成 /circles/<圈子 id>/new —— 看起来像「在这个圈子里发新帖」，
      //   实际却是在改一篇老帖子，刷新一下还会变成一篇新的空草稿。
      if (postId) return `/posts/${postId}/edit`;
      // 圈子那条把 id 放进了路径（/circles/7/new），刷新回来还在原地。
      if (circleId) return `/circles/${circleId}/new`;
      return "/posts/new";
    }
    return Object.keys(ROUTES).find((path) => ROUTES[path] === view) || "/discover";
  }

  /* 按当前地址栏渲染。三个调用点（首屏、登录后、前进后退）都走它，
     免得每处都自己拼一遍 routeFromPath + setView。 */
  async function setViewFromLocation(options = {}) {
    const route = routeFromPath(window.location.pathname);
    return setView(route.view, {
      ...options, postId: route.postId, circleId: route.circleId,
    });
  }

  /* ============================================================
     三、模态框
     ============================================================ */

  function openModal(html, options = {}) {
    const wide = options.wide ? " modal--wide" : "";
    // ！sticky：点背景不关。补填年龄这种「不填就别用」的弹窗必须用它，
    //   否则用户随手点一下空白就绕过去了，整个强制补填等于没做。
    const dismiss = options.sticky ? "" : ' data-action="close-modal"';
    $("#overlay-root").innerHTML = `
      <div class="modal-backdrop"${dismiss}>
        <div class="modal${wide}" role="dialog" aria-modal="true">${html}</div>
      </div>`;
    mountSkins($("#overlay-root"));
    // ！弹窗里的话也要过一遍字典。
    //   切语言时会整页扫一遍，但**切完之后**才打开的弹窗扫不到 ——
    //   在英文界面下点开补填框，看到的就是一屏中文。
    I18n.applyDynamic($("#overlay-root"));
  }

  function closeModal() {
    $("#overlay-root").innerHTML = "";
  }

  /* ---- 「下次还要不要重新输密码」（v0.30）-----------------------------

     问一次，答案记在这台设备上。

     为什么非要有这一问：本站的主要使用场景是**学校机房和家里的共用电脑**。
     一律「记住」是不负责任的（下一个人打开就是上一个人的账号），
     一律「不记」又会让自己的电脑上每次都要输一遍 —— 而这个区别只有用户知道。

     ！问的时机必须在**发登录请求之前**：服务端要按这个答案决定令牌活
       14 天还是 12 小时（见 backend/security.py 的两个 TTL）。
       等登完再问就晚了 —— 令牌已经发出去了，改不了。

     ！登录失败时用户其实已经回答过了。pendingKeep 就是为这个留的，
       让「输错密码 → 改一下重试」不会把同一个问题问第二遍。
       但它**不写进设备** —— 只有登录成功了才写（见 completeSignIn）。*/
  let pendingKeep = null;
  let keepModalOpen = false;

  /** 返回 Promise<true|false|null>，null 表示用户放弃这次登录。 */
  function resolveKeepChoice() {
    if (Store.keep === "1") return Promise.resolve(true);
    if (Store.keep === "0") return Promise.resolve(false);
    if (pendingKeep !== null) return Promise.resolve(pendingKeep);

    return new Promise((resolve) => {
      keepModalOpen = true;
      openModal(`
        <h2 class="display display-sm">下次还要输密码吗？</h2>
        <p class="hint">对这台电脑回答一次，以后就按这个来。</p>
        <div class="iris-rule iris-rule--fade" style="margin:1rem 0"></div>
        <div class="stack">
          <button class="btn btn-primary btn-block" type="button" data-keep="1">
            保持登录 —— 这台电脑是我自己的
          </button>
          <button class="btn btn-block" type="button" data-keep="0">
            每次都要重新登录 —— 这是公用电脑
          </button>
        </div>
        <p class="hint" style="margin-top:1rem">
          学校机房、网吧、家里的共用电脑请选下面那个 —— 不然下一个人打开就是你的账号。
        </p>
        <p class="hint">之后可以在「我的 → 账号」里改回来。</p>`, { sticky: true });

      /* ！监听挂在 .modal / .modal-backdrop 上，不是 #overlay-root 上。
         overlay-root 是常驻节点，挂上去的监听会活到刷新为止 ——
         每弹一次加一个，点一次按钮触发一堆回调。这两个节点每次都会重建。 */
      const root = $("#overlay-root");
      const panel = root.querySelector(".modal");
      const backdrop = root.querySelector(".modal-backdrop");
      if (!panel || !backdrop) {
        keepModalOpen = false;
        resolve(null);
        return;
      }

      const settle = (value) => {
        keepModalOpen = false;
        closeModal();
        resolve(value);
      };
      panel.addEventListener("click", (event) => {
        const hit = event.target.closest("[data-keep]");
        if (!hit) return;
        pendingKeep = hit.dataset.keep === "1";
        settle(pendingKeep);
      });
      /* 点背景 = 这次先不登录。
         ！留这个口子是故意的：一个怎么都关不掉的弹窗很招人烦，
           而「我不想回答，先不登了」是一个合理的选择 —— 表单里的内容还在，
           想登的时候再点一下就行。 */
      backdrop.addEventListener("click", (event) => {
        if (event.target.closest(".modal")) return;
        settle(null);
      });
    });
  }

  /* ---- 补填年龄与生日（v0.29）--------------------------------------
     v0.28 及以前注册的账号没有生日，后端在 get_current_user 里把
     除「看自己的汇总 / 补填本身 / 登出注销」之外的接口全挡了（428）。
     所以这个框是**关不掉**的：点背景无效、没有 ✕。
     ！别为了「体验好一点」给它加个关闭按钮 —— 关得掉就等于没做，
       用户会在没有任何年龄数据的情况下继续用站，匹配也就跟着失准。 */
  let birthModalOpen = false;

  function showBirthModal() {
    // 并发请求会同时撞进来，只弹一次
    if (birthModalOpen || !Store.token || !$("#app-screen")) return;
    if ($("#app-screen").classList.contains("hidden")) return;   // 还在登录页
    birthModalOpen = true;

    const ageMeta = (Store.meta || {}).age || {};
    openModal(`
      <h2 class="display display-sm">还差一步</h2>
      <p class="hint">补上年龄和生日就能继续使用。填过之后不能修改，它会决定你在站里属于未成年还是成年，也会用来匹配同好。</p>
      <form id="form-birth" class="form-stack">
        <label class="field">
          <span>年龄</span>
          <input class="input" type="number" name="age" id="backfill-age"
                 min="${ageMeta.min ?? 6}" max="${ageMeta.max ?? 99}" step="1"
                 inputmode="numeric" required
                 placeholder="填数字，例如 13">
        </label>
        <label class="field">
          <span>生日（月 / 日）</span>
          <div class="birth-row">
            <select class="select" name="birth_month" id="backfill-birth-month"
                    aria-label="出生月份"></select>
            <select class="select" name="birth_day" id="backfill-birth-day"
                    aria-label="出生日期"></select>
          </div>
        </label>
        <div class="lock-note">
          <span class="lock-note__icon" aria-hidden="true">🔒</span>
          <span>填过就不能再改了。填错只能找管理员。</span>
        </div>
        <p class="hint hint--bad hidden" id="birth-error"></p>
        <button class="btn btn-primary btn-block" type="submit">保存并继续</button>
      </form>`, { sticky: true });

    fillBirthSelects($("#backfill-birth-month"), $("#backfill-birth-day"));
    const input = $("#backfill-age");
    if (input) input.focus();

    $("#form-birth").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.target;
      const errorBox = $("#birth-error");
      errorBox.classList.add("hidden");
      const button = form.querySelector("button[type='submit']");
      button.disabled = true;
      try {
        const result = await Api.setBirth({
          age: Number(form.age.value),
          ...birthOf(form),
        });
        Store.user = result.user;
        birthModalOpen = false;
        closeModal();
        toast("已保存。年龄和生日以后不能修改。", "ok");
        toastUnlocked(result.unlocked);
        await refreshOverview();
        await setView(State.view || "discover", { fresh: true });
      } catch (error) {
        errorBox.textContent = error.message;
        errorBox.classList.remove("hidden");
        button.disabled = false;
      }
    });
  }

  /* ============================================================
     四、启动与登录
     ============================================================ */

  /* 选项按钮（chip）只能有一个切换入口：app.js 底部的全局事件委托。
     千万不要再给容器单独绑 click —— 两处都切换等于切了两次，
     表现就是「点了没反应、怎么都选不上」。 */

  function toggleChip(chip) {
    chip.setAttribute(
      "aria-pressed",
      chip.getAttribute("aria-pressed") === "true" ? "false" : "true",
    );
  }

  function selectedChips(selector) {
    return $$(`${selector} .chip[aria-pressed="true"]`).map((chip) => chip.textContent.trim());
  }

  /* 生日的「月」和「日」两个下拉。
     ！日期选项里**不带「月」「日」这两个字** —— 带了就得为 43 条选项
       各翻一遍四种语言，而数字本身就是通用的。单位写在字段标签上。
     ！2 月给 29 天：闰年出生的人填 2/29 是合法的，不该被前端拦住。 */
  const BIRTH_DAYS = [0, 31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

  function fillBirthSelects(monthEl, dayEl, month = 1, day = 1) {
    if (!monthEl || !dayEl) return;
    monthEl.innerHTML = Array.from({ length: 12 }, (_, i) => i + 1)
      .map((m) => `<option value="${m}" ${m === month ? "selected" : ""}>${m}</option>`)
      .join("");
    const rebuild = () => {
      const max = BIRTH_DAYS[Number(monthEl.value) || 1];
      const keep = Math.min(Number(dayEl.value) || day, max);
      dayEl.innerHTML = Array.from({ length: max }, (_, i) => i + 1)
        .map((d) => `<option value="${d}" ${d === keep ? "selected" : ""}>${d}</option>`)
        .join("");
    };
    monthEl.onchange = rebuild;
    rebuild();
  }

  /* 从一对生日下拉里取值。 */
  function birthOf(form) {
    return {
      birth_month: Number(form.birth_month.value),
      birth_day: Number(form.birth_day.value),
    };
  }

  function fillMetaOptions() {
    const meta = Store.meta || {};

    $("#reg-edition").innerHTML = (meta.editions || [])
      .map((item) => `<option value="${esc(item.value)}">${esc(item.label)}</option>`)
      .join("");

    // 年龄的上下限与成年线都从后端来，前端不写死这几个数字 ——
    // 站方哪天把成年线从 16 改到 18，只改 backend/age.py 一处。
    const ageMeta = meta.age || {};
    const ageInput = $("#reg-age");
    if (ageInput) {
      ageInput.min = ageMeta.min ?? 6;
      ageInput.max = ageMeta.max ?? 99;
    }
    fillBirthSelects($("#reg-birth-month"), $("#reg-birth-day"));

    $("#reg-styles").innerHTML = chipRow(meta.play_styles || []);
    $("#reg-times").innerHTML = chipRow(meta.online_times || []);
    $("#reg-wants").innerHTML = chipRow(meta.looking_for || []);
    $("#reg-note").textContent = meta.privacy_note || "";

    const stats = meta.stats || {};
    const fact = $("#fact-friends");
    if (fact) fact.textContent = stats.online ?? stats.players ?? 0;
    // ！成就数量必须从 meta 取，不能写在 HTML 里。
    //   之前这里写死的 12 在成就扩到 28 条之后就成了假数据 ——
    //   而这种「看着很具体所以没人怀疑」的数字最容易一直错下去。
    const achievementFact = $("#fact-achievements");
    if (achievementFact) achievementFact.textContent = (meta.achievements || []).length;
  }

  function showAuth() {
    $("#app-screen").classList.add("hidden");
    $("#auth-screen").classList.remove("hidden");

    // ！切回登录页 = 这台机器要换人用了。视图缓存里装的都是上一个账号的
    //   昵称、伙伴、资料，不清掉的话下一个人登录后会先闪一眼别人的数据。
    //   这种事一旦发生就是隐私事故，不能指望「应该不会吧」。
    ViewCache.clear();
    Loaded.discover = false;
    Loaded.partners = false;

    // 输入框同理，而且更直接。浏览器会把用户打过的字一直留着，而本站的
    // 主要使用场景就是学校机房和家里的共用电脑 —— 上一个人填的账号密码
    // 就那么摆在页面上。这个比缓存泄漏更显眼，也更该修。
    const loginForm = $("#form-login");
    const registerForm = $("#form-register");
    if (loginForm) loginForm.reset();
    if (registerForm) {
      registerForm.reset();
      // chip 是 <button>，不是表单控件，reset() 管不到
      $$("#form-register .chip").forEach((chip) => chip.setAttribute("aria-pressed", "false"));
      setRegisterStep(1, false);
    }

    // 验证码也换一张：上一张是给上一个人发的，留着只会让人困惑。
    captchas.login = null;
    captchas.register = null;

    // 每次回到登录页都先把图领好：不预加载的话，第一屏会先看到一块空白，
    // 而用户往往正是在那一刻开始输入的。已经领过就不重复领。
    if (!(captchas.login || {}).id) loadCaptcha("login");
  }

  function showApp() {
    $("#auth-screen").classList.add("hidden");
    $("#app-screen").classList.remove("hidden");
    renderMeChip();
    renderNav();
    // ！放在这里而不是 boot/completeSignIn 里各写一遍：
    //   「app 界面刚显示出来」是这两条路径唯一的共同点。
    //   而且必须在这之后 —— showBirthModal 要求 app-screen 不再是 hidden。
    if (Store.user && Store.user.birth_declared === false) showBirthModal();
  }

  function renderMeChip() {
    const user = Store.user;
    const level = State.overview?.level;
    if (!user) return;
    $("#me-chip").innerHTML = `
      ${avatar(user, "sm")}
      <div class="who">
        <b>${esc(user.nickname)}</b>
        <span>${level ? `Lv.${level.level} · ${esc(level.title)}` : "方块同好"}</span>
      </div>`;
    // 顶栏在 #view 之外，setView() 里的 mountSkins(view) 扫不到它。
    // 用户传过皮肤时这里会生成一个 <canvas data-skin-head>，
    // 不主动 hydrate 就永远是一块没画过的空白画布 —— 表现为「右上角一个方框」。
    mountSkins($("#me-chip"));
  }

  /* 侧栏底部那盏「服务器状态」的灯。
     ！它显示的是**真的**东西：一次 /api/health 往返的毫秒数、库里真人的在线数。
       写死一个「在线」的假灯最省事，但后端真挂了它照样亮着 —— 那种装饰一旦
       被用户发现，整页的其他「活数据」也会跟着不可信。
       data-state 只有三种：wait（还没测到）/ on / off。 */
  const NetStatus = { ok: null, ms: 0, players: 0, online: 0 };

  function paintServerStatus() {
    const box = $("#server-status");
    if (!box) return;
    const on = NetStatus.ok === true;
    const off = NetStatus.ok === false;
    box.dataset.state = on ? "on" : (off ? "off" : "wait");
    box.innerHTML = `
      <span class="dot" aria-hidden="true"></span>
      <span class="txt">${on ? "服务器在线" : off ? "连不上服务器" : "正在连接…"}</span>
      ${on && NetStatus.ms ? `<span class="ms">${NetStatus.ms} ms</span>` : ""}`;

    const sub = $("#server-sub");
    if (!sub) return;
    /* 「N 人在线」用的是 300 秒窗口内的真人（见 backend/main.py 的 _public_stats），
       不是活跃会话数 —— 后者会把「挂着页面去吃饭的人」也算成在线。 */
    sub.textContent = on && NetStatus.players
      ? `${NetStatus.online} 人在线 · 共 ${NetStatus.players} 位同好`
      : "";
  }

  async function pingServer() {
    const started = performance.now();
    try {
      const data = await Api.health();
      NetStatus.ok = true;
      NetStatus.ms = Math.round(performance.now() - started);
      const stats = data && data.stats;
      if (stats) {
        NetStatus.players = Number(stats.players) || 0;
        NetStatus.online = Number(stats.online) || 0;
      }
    } catch (error) {
      NetStatus.ok = false;
    }
    paintServerStatus();
  }

  function renderNav() {
    const stats = State.overview?.stats || {};
    const items = NAV.map((item) => {
      const badgeCount = item.badge ? Number(stats[item.badge] || 0) : 0;
      const badge =
        badgeCount > 0
          ? `<span class="count">${badgeCount > 99 ? "99+" : badgeCount}</span>`
          : "";
      return `
        <button class="nav-item" data-nav="${item.key}" aria-current="${State.view === item.key}">
          <span class="nav-icon">${McfrIcons.nav(item.key)}</span>
          <span>${item.label}</span>
          ${badge}
        </button>`;
    }).join("");

    $("#sidenav").innerHTML = `
      <span class="eyebrow" style="padding:0 .8rem .5rem">导航</span>
      ${items}
      <div class="nav-foot">
        <div class="server-status" id="server-status" data-state="wait" role="status" aria-live="polite"></div>
        <div class="server-sub" id="server-sub"></div>
        <button class="btn btn-ghost btn-sm btn-block" data-action="logout">退出登录</button>
      </div>`;

    // 侧栏每换一次页都会重画一遍，这盏灯得跟着补上当前状态
    paintServerStatus();
  }

  async function applyOverview(data) {
    State.overview = data;
    Store.user = data.user;
    renderMeChip();
    renderNav();
  }

  async function refreshOverview() {
    try {
      const data = await Api.overview();
      await applyOverview(data);
      return data;
    } catch (error) {
      return null;
    }
  }

  /* 每个页面**渲染完的成品**存一份。
     切回看过的页面时先把上次的 DOM 原样贴回去 —— 立刻可见、
     不用对着占位条等接口；随后渲染函数会用新数据把它换掉。

     ！为什么缓存 DOM 而不是缓存数据：成就、我的、模组镜像这几个页面
       是先 await 接口、拿到数据才写 innerHTML 的，中间那段时间页面上
       什么都没有。只缓存数据填不上这段空白，缓存 DOM 才能。

     ！登出时必须清掉。这里装的是上一个账号的昵称、伙伴、资料，
       不清的话下一个人登录后会先闪一眼别人的数据。见 showAuth()。 */
  const ViewCache = new Map();

  async function setView(name, options = {}) {
    State.view = name;
    // 这一次要不要无视缓存重新拉数据。只有「换一批」这类明确要求刷新的操作会传。
    State.forceFresh = options.fresh === true;

    // ！帖子详情 / 编辑页 / 圈子主页「是哪一篇 / 哪个圈子」全靠这两个。
    //   换了一篇就要把上一份缓存丢掉，否则会先闪一眼上一篇的内容。
    if (options.postId !== undefined) {
      const next = Number(options.postId) || 0;
      if (next !== State.routePostId) ViewCache.delete(name);
      State.routePostId = next;
    }
    if (options.circleId !== undefined) {
      const next = Number(options.circleId) || 0;
      if (next !== State.routeCircleId) ViewCache.delete(name);
      State.routeCircleId = next;
    }

    // 同步地址栏（从浏览器前进/后退回来时不再重复压栈）
    if (!options.fromHistory) {
      const path = pathForView(name, State.routePostId, State.routeCircleId);
      if (window.location.pathname !== path) {
        window.history.pushState({ view: name }, "", path);
      }
    }

    renderNav();
    // 侧栏的选中指示器先滑过去，内容等渲染完再滑进来。
    const dir = tabSwap($("#sidenav"), null);

    const view = $("#view");
    const cached = ViewCache.get(name);

    if (cached) {
      // 先贴上次的成品。这一步不联网，所以是「秒开」。
      view.innerHTML = cached;
      mountSkins(view);
      I18n.applyDynamic(view);
    } else {
      // 只有真的没东西可显示时才占位，而且只放一个转动的方块。
      // 以前这里是两条灰条纹，而它是整个页面最扎眼的东西 ——
      // 每切一次页就闪一下，很难看。
      view.innerHTML = `<div class="load-hint">${cubeLoader()}</div>`;
    }

    const renderer = RENDERERS[name] || RENDERERS.discover;
    try {
      await renderer();
    } catch (error) {
      ViewCache.delete(name);   // 渲染失败了，别留着那份快照误导人
      view.innerHTML = `<div class="empty"><span class="glyph">⚠</span><p>${esc(error.message)}</p></div>`;
    }

    // ！带参数的页面（帖子详情 / 帖子编辑 / 圈子主页）不进缓存：同一个页面名
    //   对应的可能是完全不同的内容，缓存下来会在切帖子时先闪一眼旧的。
    //   它们每次都是真去拉数据，本来也没有「秒开」可言。
    if (name !== "post" && name !== "editor" && name !== "circle") {
      ViewCache.set(name, view.innerHTML);
    }
    // 页面刚渲染完，把里面的皮肤 / 头像 canvas 画上
    mountSkins(view);
    tabIn(view, dir);
    // 渲染出来的文案统一过一遍字典（见 i18n.js 的 applyDynamic）。
    // 放在最后：要在所有内容都进了 DOM 之后才扫得到。
    I18n.applyDynamic(view);
    // 侧栏和顶栏不在 #view 里，单独扫
    I18n.applyDynamic($("#sidenav"));
    I18n.applyDynamic($("#me-chip"));
  }

  /* ============================================================
     五、发现同好
     ============================================================ */

  /* 卡片上的按钮只带一个 id，要拿回完整对象得跨两个列表找：
     推荐列表和搜索结果。漏一个的症状是弹窗里显示「这位玩家」，
     用户会以为自己点错了人。 */
  function findUser(id) {
    return State.discover.find((user) => user.id === id)
        || State.search.items.find((user) => user.id === id)
        || null;
  }

  /* 年龄标签（v0.29）。
     ！只显示岁数，不显示月和日 —— 生日是本人的隐私，别人只需要知道
       「大概多大」，够用来判断合不合得来。
     成年用主题色标一下：本站用户以中小学生为主，「对面是个成年人」
     是他们在决定要不要搭话时应该一眼看到的信息。 */
  function ageTag(user) {
    if (!user || !user.age) return "";   // 老账号还没补填时就不显示
    return `<span class="tag${user.adult ? " tag--iris" : ""}">${esc(user.age_label)}</span>`;
  }

  /* 卡片上那段小字。
     ！没写简介的人**不编**第一人称的话（「找个红石大佬带我」那种）——
       那是把话塞进别人嘴里，何况这站上大多是未成年人。
       改为用 TA 自己填过的资料拼一句：常玩的玩法 + 作息。
       好处是每张卡都不一样、而且每句都真 —— 之前那版所有空简介的卡片
       都在复读同一句话，一眼就看得出是空壳。
     ！两句分开成两个 <span>：i18n 是按文本节点整串查字典的，
       把「常玩 X。周末白天在线。」写成一段的话，两句都翻不了；
       拆开后各自能命中「常玩 {}。」和「{} 在线。」。 */
  const EMPTY_BIO_LINES = [
    "刚来不久，资料还在填。",
    "还没写简介，先打个招呼吧。",
    "资料还空着，人是已经来了。",
  ];

  function bioOf(user) {
    const bio = (user.bio || "").trim();
    if (bio) return esc(bio);

    const styles = (user.play_styles || []).slice(0, 2);
    const when = (user.online_time || [])[0] || "";
    if (!styles.length && !when) {
      /* 连玩法都没填的人：三句轮着来，按 id 取，**固定**不会每次重画都变。
         ！为什么不是随机：卡片的重新渲染很频繁（切页面、搜索），
           随机会让同一张卡的句子在眼前跳来跳去，比复读还怪。 */
      return EMPTY_BIO_LINES[Math.abs(Number(user.id) || 0) % EMPTY_BIO_LINES.length];
    }

    const parts = [];
    if (styles.length) parts.push(`<span>常玩${esc(styles.join("、"))}。</span>`);
    if (when) parts.push(`<span>${esc(when)}在线。</span>`);
    return parts.join("");
  }

  function peerCard(user) {
    const score = user.score_info || { score: 0, reasons: [] };
    const reasons = score.reasons || [];
    const styles = (user.play_styles || []).slice(0, 3);

    return `
      <article class="peer-card edge" data-uid="${user.id}">
        <div class="peer-top">
          ${avatar(user, null)}
          <div class="peer-id">
            <b>${esc(user.nickname)}</b>
            <span class="handle">@${esc(user.username)} · ${esc(user.edition_label)}</span>
          </div>
          <div class="score-box">
            <span class="num iris-text">${score.score}</span>
            <span class="cap">match</span>
          </div>
        </div>

        <div class="chip-row">
          ${user.online ? `<span class="tag tag--iris">在线</span>` : ""}
          ${ageTag(user)}
          ${user.region ? `<span class="tag">${esc(user.region)}</span>` : ""}
          ${styles.map((style) => `<span class="tag">${esc(style)}</span>`).join("")}
          ${user.has_contact ? `<span class="tag">已备联系方式</span>` : ""}
        </div>

        ${reasons.length ? `<ul class="reason-list" style="list-style:none;margin:0;padding:0">
          ${reasons.map((reason) => `<li><span class="spark">✦</span><span>${esc(reason)}</span></li>`).join("")}
        </ul>` : ""}

        <p class="peer-bio">${bioOf(user)}</p>

        <div class="peer-actions">
          <button class="btn btn-primary" data-act="greet" data-uid="${user.id}">申请交换联系方式</button>
          <button class="btn btn-ghost" data-act="profile" data-uid="${user.id}">资料</button>
          <button class="btn btn-ghost" data-act="skip" data-uid="${user.id}" title="不再推荐">✕</button>
        </div>
      </article>`;
  }

  /* 搜索栏：发现页顶部那一条。
     放在推荐上面而不是侧栏里 —— 「我知道要找谁」和「帮我推荐不认识的人」
     是两条相反的路径，混在一起两边都不好用。 */
  function searchBar() {
    const mac = /Mac|iPhone|iPad/.test(navigator.platform || "");
    return `
      <div class="discover-search">
        <div class="search-field">
          <span class="search-icon" aria-hidden="true">${McfrIcons.line("search")}</span>
          <input id="peer-search" type="search" autocomplete="off" spellcheck="false"
                 placeholder="搜索同好 —— 输入昵称或用户名"
                 aria-label="搜索同好" aria-describedby="search-note"/>
          <kbd class="search-kbd">${mac ? "⌘" : "Ctrl"} + /</kbd>
          <button type="button" class="search-clear hidden" data-action="clear-search"
                  aria-label="清除搜索">✕</button>
        </div>
        <p class="hint" id="search-note" role="status"></p>
      </div>`;
  }

  async function renderDiscover() {
    const view = $("#view");
    view.innerHTML = `
      <div class="view-head">
        <div class="titles">
          <span class="eyebrow eyebrow--iris">Discover</span>
          <h1 class="display display-lg">今晚想和<span class="iris-text">谁</span>一起玩</h1>
          <p class="lede">根据你的游戏版本、常玩的玩法与作息，替你排一份今晚能一起开局的名单。看中谁就发出交换申请 —— 对方同意了，你们才会互相看到联系方式。</p>
        </div>
        <button class="btn btn-ghost" data-action="reload-discover">换一批</button>
      </div>

      ${searchBar()}

      <div id="discover-body"></div>`;

    State.search = { query: "", items: [] };

    // 看过的推荐直接画出来。切回来不该再闪一次占位条，
    // 也不该让名单自己换一批 —— 那会让人以为自己记错了。
    // 想看新的有右上角「换一批」。
    if (Loaded.discover && !State.forceFresh) {
      paintRecommendations();
      return;
    }
    await loadRecommendations();
  }

  /* 把 State.discover 画进网格。
     ！和 loadRecommendations 分开：切回页面、拉完新数据两条路都要用它。 */
  function paintRecommendations() {
    const body = $("#discover-body");
    if (!body) return;
    body.innerHTML = State.discover.length
      ? `<div class="peer-grid">${State.discover.map(peerCard).join("")}</div>`
      : `<div class="empty">
           <span class="glyph">⬡</span>
           <p>暂时没有新的推荐了</p>
           <p class="hint">用上面的搜索框直接找人，或者稍后再来逛逛。</p>
         </div>`;
  }

  /* 推荐列表。搜索框清空后就回到这里。
     ！统一往 #discover-body 里写，不要用 outerHTML 把网格本身替掉 ——
       那样容器没了，再想换回推荐就得重新渲染整个视图。 */
  async function loadRecommendations() {
    const body = $("#discover-body");
    if (!body) return;
    body.innerHTML = `<div class="load-hint">${cubeLoader()}</div>`;

    const data = await Api.discover(12);
    State.discover = data.items;
    Loaded.discover = true;

    if (!$("#discover-body")) return;          // 等响应期间被切走了
    paintRecommendations();
  }

  function setSearchNote(text) {
    const note = $("#search-note");
    if (note) note.textContent = text || "";
  }

  /* 搜索。结果和推荐共用同一张卡片，所以「申请交换 / 资料 / 跳过」
     三个按钮都不用重写 —— 这也是后端返回同样字段形状的原因。 */
  async function runSearch(query) {
    const body = $("#discover-body");
    if (!body) return;

    const keyword = (query || "").trim();
    State.search.query = keyword;

    const clearButton = $("[data-action='clear-search']");
    if (clearButton) clearButton.classList.toggle("hidden", !keyword);

    if (!keyword) {
      setSearchNote("");
      await loadRecommendations();
      return;
    }

    setSearchNote("搜索中…");
    body.innerHTML = `<div class="peer-grid">${skeleton(190).repeat(3)}</div>`;

    let data;
    try {
      data = await Api.searchUsers(keyword);
    } catch (error) {
      if (!$("#discover-body")) return;
      setSearchNote("");
      body.innerHTML = `<div class="empty"><span class="glyph">⚠</span><p>${esc(error.message)}</p></div>`;
      return;
    }

    // ！慢响应回来时用户可能已经改了关键词、或者切到别的页了。
    //   不比对的话，晚到的旧结果会盖掉新结果 —— 表现为「搜完显示的不是我搜的」。
    if (!$("#discover-body") || State.search.query !== keyword) return;

    State.search.items = data.items;
    const total = data.total || 0;
    setSearchNote(total ? `找到 ${total} 位昵称或用户名含「${keyword}」的同好` : "");

    body.innerHTML = total
      ? `<div class="peer-grid">${data.items.map(peerCard).join("")}</div>`
      : `<div class="empty">
           <span class="glyph">⬡</span>
           <p>没找到「${esc(keyword)}」</p>
           <p class="hint">只搜昵称和用户名。试试只输前两个字，或者清空后看看推荐。</p>
         </div>`;
  }

  function focusSearch() {
    const input = $("#peer-search");
    if (!input) return false;
    input.focus();
    input.select();
    return true;
  }

  function clearSearch() {
    const input = $("#peer-search");
    if (input) input.value = "";
    runSearch("");
  }

  function openGreetModal(user) {
    openModal(`
      <div class="row" style="gap:.9rem">
        ${avatar(user, "lg")}
        <div class="stack-sm">
          <span class="eyebrow eyebrow--iris">联系方式交换申请</span>
          <h2 class="display display-md">${esc(user.nickname)}</h2>
          <span class="hint">${esc(user.edition_label)}${user.region ? " · " + esc(user.region) : ""}</span>
        </div>
      </div>

      <div class="iris-rule iris-rule--fade" style="margin:1.2rem 0"></div>

      <p class="lede" style="margin-bottom:1rem">
        写一句话告诉 TA 你为什么想认识 TA。对方同意后，你们会<b>互相</b>看到对方预置的联系方式。
      </p>

      <label class="field">
        <span>想对 TA 说的话</span>
        <textarea class="textarea" id="greet-text" rows="3" maxlength="100"
          placeholder="例如：我也在造中世纪城堡，要不要一起？"></textarea>
      </label>
      <p class="hint" style="margin-top:.5rem">
        建议不要在这句话里直接写手机号；QQ 与微信可以填到「我的 → 联系方式」里，由对方同意后再发送。
      </p>
      <p class="error-text hidden" id="greet-error"></p>

      <div class="row" style="justify-content:flex-end;margin-top:1.2rem">
        <button class="btn btn-ghost" data-action="close-modal">取消</button>
        <button class="btn btn-primary" data-action="send-greet" data-uid="${user.id}">发送申请</button>
      </div>`);
  }

  async function sendGreet(targetId, button) {
    const textarea = $("#greet-text");
    const errorBox = $("#greet-error");
    const greeting = textarea ? textarea.value.trim() : "";

    if (errorBox) errorBox.classList.add("hidden");
    button.disabled = true;

    try {
      const result = await Api.sendRequest(Number(targetId), greeting);
      closeModal();
      toast(result.message, "ok");
      toastUnlocked(result.unlocked);

      const card = $(`.peer-card[data-uid="${targetId}"]`);
      if (card) card.remove();
      const grid = $("#peer-grid");
      if (grid && !grid.children.length) {
        grid.outerHTML = `<div class="empty"><span class="glyph">✦</span><p>今天的推荐看完了</p>
          <p class="hint">发出的申请可以在「我的伙伴」里查看状态。</p></div>`;
      }
      await refreshOverview();
    } catch (error) {
      if (errorBox) {
        errorBox.textContent = error.message;
        errorBox.classList.remove("hidden");
      }
    } finally {
      button.disabled = false;
    }
  }

  /* ============================================================
     六、我的伙伴（联系方式交换）
     ============================================================ */

  function partnerRow(user) {
    const tags = (user.play_styles || []).slice(0, 2)
      .map((style) => `<span class="tag">${esc(style)}</span>`).join("");

    return `
      <div class="person-row person-row--stack">
        <div class="person-head">
          ${avatar(user, null)}
          <div class="person-main">
            <span class="name">${esc(user.nickname)} ${presenceDot(user.online)}</span>
            <span class="sub">@${esc(user.username)} · ${esc(user.edition_label)}${user.region ? " · " + esc(user.region) : ""}</span>
            ${tags ? `<div class="chip-row" style="margin-top:.3rem">${tags}</div>` : ""}
          </div>
          <div class="person-actions">
            <button class="btn btn-ghost btn-sm" data-act="profile" data-uid="${user.id}">资料</button>
            <button class="btn btn-ghost btn-sm" data-act="unlink" data-uid="${user.id}">解除</button>
          </div>
        </div>
        ${contactRows(user.contact, user.nickname)}
      </div>`;
  }

  async function renderPartners() {
    const view = $("#view");
    view.innerHTML = `
      <div class="view-head">
        <div class="titles">
          <span class="eyebrow eyebrow--iris">Partners</span>
          <h1 class="display display-lg">我的<span class="iris-text">伙伴</span></h1>
          <p class="lede">这里是与你有过交换联系的玩家。对方的联系方式只有在你同意之后才会显示。</p>
        </div>
      </div>
      <div class="tab-switch" style="max-width:460px;grid-template-columns:repeat(3,1fr)">
        <button data-partner-tab="connections" aria-selected="${State.partnerTab === "connections"}">已交换</button>
        <button data-partner-tab="incoming" aria-selected="${State.partnerTab === "incoming"}">待我处理</button>
        <button data-partner-tab="outgoing" aria-selected="${State.partnerTab === "outgoing"}">我发出的</button>
      </div>
      <div id="partner-body" class="stack" style="margin-top:1.2rem"></div>`;

    // 这组标签是随页面重建的，先把指示器装上（首次不滑，直接落在选中项上）
    tabBar($("#view .tab-switch"));

    // 上次看过的列表已经在 State 里了，先画出来 —— 切回来是「秒开」，
    // 不用对着占位条等三个接口。刷新在后台默默进行，内容一样就完全看不出来。
    if (Loaded.partners) paintPartnerBody();

    await refreshPartnerBody();
  }

  /* 把 State 里的伙伴数据画进 #partner-body。
     ！单独拆出来是为了让「切标签」不用联网。
       以前点一下标签就要重新请求三个接口，就为了看一份已经在本地的东西 ——
       于是每点一下都得等一轮网络，还要闪一次占位条。 */
  function paintPartnerBody() {
    const body = $("#partner-body");
    if (!body) return;

    $$("[data-partner-tab]").forEach((button) => {
      button.setAttribute("aria-selected", String(button.dataset.partnerTab === State.partnerTab));
    });

    if (State.partnerTab === "connections") {
      body.innerHTML = State.connections.length
        ? `<div class="list-panel">${State.connections.map(partnerRow).join("")}</div>`
        : `<div class="empty"><span class="glyph">⬢</span><p>还没有交换过联系方式的伙伴</p>
             <p class="hint">去「发现同好」向聊得来的人发出交换申请吧。</p></div>`;
      return;
    }

    if (State.partnerTab === "incoming") {
      body.innerHTML = State.requestsIn.length
        ? `<div class="list-panel">${State.requestsIn.map((item) => `
            <div class="person-row" style="align-items:flex-start">
              ${avatar(item.peer, null)}
              <div class="person-main">
                <span class="name">${esc(item.peer.nickname)}</span>
                <span class="sub">@${esc(item.peer.username)} · ${fmtRelative(item.created_at)}</span>
                ${item.greeting ? `<p class="greeting">${esc(item.greeting)}</p>` : ""}
              </div>
              <div class="person-actions">
                <button class="btn btn-primary btn-sm" data-act="accept" data-rid="${item.id}">同意交换</button>
                <button class="btn btn-ghost btn-sm" data-act="reject" data-rid="${item.id}">婉拒</button>
              </div>
            </div>`).join("")}</div>`
        : `<div class="empty"><span class="glyph">✦</span><p>没有待处理的申请</p></div>`;
      return;
    }

    body.innerHTML = State.requestsOut.length
      ? `<div class="list-panel">${State.requestsOut.map((item) => `
          <div class="person-row">
            ${avatar(item.peer, null)}
            <div class="person-main">
              <span class="name">${esc(item.peer.nickname)}</span>
              <span class="sub">等待回应 · ${fmtRelative(item.created_at)}</span>
            </div>
            <div class="person-actions"><span class="tag">已发送</span></div>
          </div>`).join("")}</div>`
      : `<div class="empty"><span class="glyph">◈</span><p>还没有发出过申请</p></div>`;
  }

  async function refreshPartnerBody() {
    const body = $("#partner-body");
    if (!body) return;

    // ！只有第一次进来才真的没东西可显示。之后 renderPartners 已经用
    //   State 里的旧数据画好了，这里再铺一次占位条只会把刚画好的内容盖掉。
    if (!Loaded.partners) body.innerHTML = `<div class="load-hint">${cubeLoader()}</div>`;

    const [connections, incoming, outgoing] = await Promise.all([
      Api.connections(),
      Api.requests("incoming"),
      Api.requests("outgoing"),
    ]);
    State.connections = connections.items;
    State.requestsIn = incoming.items;
    State.requestsOut = outgoing.items;
    Loaded.partners = true;

    if (!$("#partner-body")) return;   // 等响应期间被切走了
    paintPartnerBody();
  }

  /* ============================================================
     七、登录后的交换申请确认弹窗
     ============================================================ */

  async function checkPendingRequests() {
    let data = null;
    try {
      data = await Api.pendingRequests();
    } catch (error) {
      return;
    }
    if (!data || !data.items.length) return;

    State.requestQueue = data.items;
    State.requestIndex = 0;
    renderRequestDialog();
  }

  function renderRequestDialog() {
    const queue = State.requestQueue || [];
    const item = queue[State.requestIndex];
    if (!item) {
      State.requestQueue = null;
      State.requestIndex = 0;
      closeModal();
      return;
    }

    const sender = item.sender || {};
    const score = sender.score_info || { score: 0, reasons: [] };
    const remaining = queue.length - State.requestIndex;

    openModal(`
      <div class="row" style="gap:1rem;align-items:flex-start">
        ${avatar(sender, "lg")}
        <div class="stack-sm" style="flex:1">
          <span class="eyebrow eyebrow--iris">联系方式交换申请${remaining > 1 ? ` · 还有 ${remaining} 条` : ""}</span>
          <h2 class="display display-md">${esc(sender.nickname)} 想认识你</h2>
          <div class="row wrap" style="gap:.5rem">
            ${presenceDot(sender.online)}
            <span class="hint">${sender.online ? "在线" : `最后活跃 ${fmtRelative(sender.last_seen)}`}</span>
            ${sender.region ? `<span class="tag">${esc(sender.region)}</span>` : ""}
            ${sender.has_contact ? `<span class="tag">TA 已备好联系方式</span>` : `<span class="tag">TA 还没填联系方式</span>`}
          </div>
        </div>
      </div>

      <div class="iris-rule iris-rule--fade" style="margin:1.1rem 0"></div>

      ${item.greeting ? `<p class="greeting" style="margin:0 0 .9rem">${esc(item.greeting)}</p>` : ""}

      ${score.reasons && score.reasons.length ? `
        <ul class="reason-list" style="list-style:none;margin:0 0 1rem;padding:0">
          ${score.reasons.map((reason) => `<li><span class="spark">✦</span><span>${esc(reason)}</span></li>`).join("")}
        </ul>` : ""}

      <div class="notice ${item.my_contact_ready ? "notice--ok" : "notice--warn"}">
        ${item.my_contact_ready
          ? "同意后，你预置的联系方式会发送给 TA，同时你也会拿到 TA 的联系方式。"
          : "你还没有预置联系方式。同意前，建议先去「我的 → 联系方式」填写，否则对方只会收到一句空资料。"}
      </div>

      <div class="row" style="justify-content:flex-end;gap:.5rem;margin-top:1.2rem">
        <button class="btn btn-ghost" data-action="later-request">稍后再处理</button>
        <button class="btn btn-danger" data-action="reject-current" data-rid="${item.id}">婉拒</button>
        <button class="btn btn-primary" data-action="accept-current" data-rid="${item.id}">同意并交换</button>
      </div>`, { wide: true });
  }

  function renderExchangeResult(peer, myContactReady) {
    const contact = (peer && peer.contact) || null;
    openModal(`
      <span class="eyebrow eyebrow--iris">交换成功</span>
      <h2 class="display display-md" style="margin:.35rem 0 0">你和 ${esc(peer.nickname)} 建立了联系</h2>

      <div class="iris-rule iris-rule--fade" style="margin:1.1rem 0"></div>

      <div class="row" style="gap:1rem;align-items:center;margin-bottom:1rem">
        ${avatar(peer, "lg")}
        <div class="stack-sm">
          <b>${esc(peer.nickname)}</b>
          <span class="hint">@${esc(peer.username)} · ${esc(peer.edition_label)}</span>
        </div>
      </div>

      ${contactRows(contact, peer.nickname)}

      ${myContactReady
        ? `<p class="hint" style="margin-top:1rem">你预置的联系方式也已发送给 TA，接下来在你们常用的软件里聊吧。</p>`
        : `<div class="notice notice--warn" style="margin-top:1rem">
             你还没有预置联系方式，TA 只收到了你的资料。建议现在去「我的 → 联系方式」补上。
           </div>`}

      <div class="row" style="justify-content:flex-end;gap:.5rem;margin-top:1.2rem">
        <button class="btn btn-ghost" data-action="close-modal">关闭</button>
        <button class="btn btn-primary" data-action="next-request">继续</button>
      </div>`, { wide: true });
  }

  /* ============================================================
     八、成就
     ============================================================ */

  async function renderAchievements() {
    const data = await Api.overview();
    State.overview = data;
    Store.user = data.user;
    renderMeChip();
    renderNav();

    const board = data.achievements;
    const level = data.level;

    $("#view").innerHTML = `
      <div class="view-head">
        <div class="titles">
          <span class="eyebrow eyebrow--iris">Achievements</span>
          <h1 class="display display-lg">成长<span class="iris-text">记录</span></h1>
          <p class="lede">分享与被回应都算数。解锁成就获得积分，等级会显示在你的资料卡上。</p>
        </div>
      </div>

      <div class="panel level-card">
        <span class="orb"></span>
        <div class="level-num">
          <span class="big">${Number(level.level) || 1}</span>
          <span class="cap">level</span>
        </div>
        <div class="level-info">
          <div class="row-between wrap">
            <h2 class="display display-md">${esc(level.title)}</h2>
            <span class="mono muted">${level.points} 积分${level.next_points ? ` / 下一等级 ${level.next_points}` : " · 已满级"}</span>
          </div>
          <div class="meter meter--tall meter--xp"><i style="width:${Math.round((level.progress || 0) * 100)}%"></i></div>
          <div class="level-stats">
            <div class="fact"><b>${board.unlocked_count}/${board.total_count}</b><span>已解锁成就</span></div>
            <div class="fact"><b>${data.stats.connection_count || 0}</b><span>位伙伴</span></div>
            <div class="fact"><b>${data.stats.request_sent || 0}</b><span>次主动申请</span></div>
          </div>
        </div>
      </div>

      <div class="section-title">
        <h2>成就墙</h2>
        <span class="rule"></span>
        <span class="eyebrow">${board.unlocked_count} / ${board.total_count}</span>
      </div>

      <div class="badge-grid">
        ${board.items.map((item) => `
          <div class="badge ${item.unlocked ? "badge--on" : ""}">
            <span class="glyph" style="${item.unlocked ? McfrIcons.toneStyle(item.code) : ""}">${McfrIcons.badge(item.code, item.glyph)}</span>
            <span class="name">${esc(item.name)}</span>
            <span class="desc">${esc(item.desc)}</span>
            <div class="meter"><i style="width:${Math.round((item.current / item.target) * 100)}%"></i></div>
            <span class="pts">${item.unlocked ? "已解锁" : `<b>${Number(item.current) || 0}</b> / <b>${Number(item.target) || 0}</b>`} · <b>${Number(item.points) || 0}</b> 分</span>
          </div>`).join("")}
      </div>`;
  }

  /* ============================================================
     九、我的
     ============================================================ */

  async function renderMe() {
    const [data, blocks] = await Promise.all([Api.overview(), Api.blocks()]);
    State.overview = data;
    Store.user = data.user;
    renderMeChip();
    renderNav();

    const me = data.user;
    const contact = data.contact || {};
    const meta = Store.meta || {};

    $("#view").innerHTML = `
      <div class="view-head">
        <div class="titles">
          <span class="eyebrow eyebrow--iris">Profile</span>
          <h1 class="display display-lg">我的<span class="iris-text">资料</span></h1>
          <p class="lede">资料填得越具体，越容易被合得来的人找到。</p>
        </div>
      </div>

      <div class="profile-grid">
        <form class="panel form-panel edge" id="profile-form">
          <div class="row" style="gap:1rem">
            ${avatar(me, "lg")}
            <div class="stack-sm">
              <b style="font-size:1.1rem">${esc(me.nickname)}</b>
              <span class="hint">@${esc(me.username)} · ${esc(me.edition_label)}</span>              <span class="hint">${me.email ? esc(me.email) : "未绑定邮箱"}</span>
              <span class="tag tag--iris" style="align-self:flex-start">Lv.${data.level.level} ${esc(data.level.title)}</span>
            </div>
          </div>

          <div class="iris-rule iris-rule--fade"></div>

          <div class="form-grid">
            <label class="field">
              <span>游戏昵称</span>
              <input class="input" name="nickname" value="${esc(me.nickname)}" maxlength="16" required>
            </label>
            <label class="field">
              <span>所在地区</span>
              <input class="input" name="region" value="${esc(me.region)}" maxlength="20" placeholder="可留空">
            </label>
            <label class="field">
              <span>游戏版本</span>
              <select class="select" name="edition">
                ${(meta.editions || []).map((item) => `
                  <option value="${esc(item.value)}" ${item.value === me.edition ? "selected" : ""}>${esc(item.label)}</option>`).join("")}
              </select>
            </label>
            <div class="field">
              <span>年龄</span>
              <!-- ！这里是只读的，不是 disabled 的输入框 ——
                   年龄填过就不能改（改了匹配就失去意义），
                   做成不可用控件会让人以为「哪里能解锁」。 -->
              <div class="locked-value">
                <b>${esc(me.age_label || "")}</b>
                <span class="locked-value__why">填过就不能改了</span>
              </div>
            </div>
          </div>

          <div class="field">
            <span>喜欢的玩法</span>
            <div class="chip-row" id="edit-styles">${chipRow(meta.play_styles || [], me.play_styles)}</div>
          </div>
          <div class="field">
            <span>常在线时段</span>
            <div class="chip-row" id="edit-times">${chipRow(meta.online_times || [], me.online_time)}</div>
          </div>
          <div class="field">
            <span>想找什么样的伙伴</span>
            <div class="chip-row" id="edit-wants">${chipRow(meta.looking_for || [], me.looking_for)}</div>
          </div>

          <label class="field">
            <span>个人简介</span>
            <textarea class="textarea" name="bio" rows="4" maxlength="200"
              placeholder="介绍一下你在玩什么、想找什么样的伙伴">${esc(me.bio)}</textarea>
          </label>

          <div class="iris-rule iris-rule--fade"></div>

          <div class="row-between wrap">
            <span class="eyebrow">联系方式</span>
            <span class="tag">${me.has_contact ? "已预置" : "尚未填写"}</span>
          </div>
          <p class="hint">
            填在这里的 QQ / 微信 / 其他账号<b>默认对所有人不可见</b>，接口也不会返回给他人。
            只有当你在弹窗中点了「同意并交换」，这些信息才会发送给那一位申请人。
          </p>

          <div class="form-grid">
            <label class="field">
              <span>QQ</span>
              <input class="input" name="contact_qq" value="${esc(contact.qq)}" maxlength="60" placeholder="例如 1928374655">
            </label>
            <label class="field">
              <span>微信号</span>
              <input class="input" name="contact_wechat" value="${esc(contact.wechat)}" maxlength="60" placeholder="例如 mia_build_mc">
            </label>
            <label class="field span-2">
              <span>其他联系方式（可选）</span>
              <input class="input" name="contact_other" value="${esc(contact.other)}" maxlength="60"
                placeholder="例如 B 站昵称 / 基岩版 ID / 常玩的服务器">
            </label>
            <label class="field span-2">
              <span>给对方的备注</span>
              <input class="input" name="contact_note" value="${esc(contact.note)}" maxlength="60"
                placeholder="例如：加我时说一声是在 mcfr 看到的">
            </label>
          </div>

          <p class="error-text hidden" id="profile-error"></p>
          <div class="row" style="justify-content:flex-end">
            <button class="btn btn-primary" type="submit">保存资料</button>
          </div>
        </form>

        <div class="stack">
          <div class="panel side-panel">
            <span class="eyebrow">我的形象</span>
            <div class="iris-rule iris-rule--fade"></div>

            <div class="skin-stage">
              ${me.skin_url
                ? `<canvas class="skin-canvas" width="176" height="224"
                           style="width:176px;height:224px"
                           data-skin-model="${esc(me.skin_url)}" data-cape="${esc(me.cape_url)}" data-variant="${esc(
                             me.mc_variant || "CLASSIC"
                           )}" data-drag data-spin></canvas>`
                : `<span class="skin-empty">
                     <span class="slot"><span class="mcface mcface--sm" aria-hidden="true"></span></span>
                     还没有上传皮肤
                   </span>`}
            </div>
            ${me.skin_url ? `<p class="hint hint--center">按住左右拖动可以旋转，双击回正</p>` : ""}

            <div class="row" style="gap:.5rem;flex-wrap:wrap">
              <button class="btn btn-primary btn-sm" data-action="upload-skin">
                ${me.skin_url ? "换一张皮肤" : "上传皮肤"}
              </button>
              ${me.skin_url ? `<button class="btn btn-ghost btn-sm" data-action="remove-skin">移除皮肤</button>` : ""}
            </div>

            <div class="iris-rule iris-rule--fade"></div>

            <div class="face-row">
              <span class="slot slot--xl">${avatar(me, "xl")}</span>
              <div class="stack-sm">
                <b>头像</b>
                <span class="hint">${
                  me.avatar_url
                    ? "正在使用你上传的图片"
                    : (me.skin_url ? "自动取皮肤里的头部" : "根据用户名生成的像素脸")
                }</span>
              </div>
            </div>

            <div class="row" style="gap:.5rem;flex-wrap:wrap">
              <button class="btn btn-primary btn-sm" data-action="upload-avatar">上传头像</button>
              ${me.avatar_url ? `<button class="btn btn-ghost btn-sm" data-action="remove-avatar">恢复默认</button>` : ""}
            </div>

            <p class="hint hidden" id="media-status"></p>
            <p class="hint">皮肤需要是 PNG，尺寸 64×64（1.8 以后）或 64×32（旧版）。</p>
          </div>

          <div class="panel side-panel">
            <span class="eyebrow">安全设置</span>
            <div class="iris-rule iris-rule--fade"></div>

            <div class="toggle-row">
              <div class="text">
                <b>接收联系方式交换申请</b>
                <span>关闭后别人无法向你发出申请</span>
              </div>
              <button class="switch" data-privacy="allow_contact_request"
                aria-checked="${me.allow_contact_request}" aria-label="接收联系方式交换申请"></button>
            </div>

            <div class="toggle-row">
              <div class="text">
                <b>对外显示在线状态</b>
                <span>关闭后别人看不到你是否在线</span>
              </div>
              <button class="switch" data-privacy="show_online"
                aria-checked="${me.show_online}" aria-label="对外显示在线状态"></button>
            </div>
          </div>

          <div class="panel side-panel">
            <div class="row-between">
              <span class="eyebrow">屏蔽名单</span>
              <span class="count count--soft">${blocks.items.length}</span>
            </div>
            <div class="iris-rule iris-rule--fade"></div>
            ${blocks.items.length
              ? blocks.items.map((user) => `
                  <div class="row-between">
                    <span class="row" style="gap:.6rem">${avatar(user, "sm")}
                      <span style="font-size:.875rem">${esc(user.nickname)}</span></span>
                    <button class="btn btn-ghost btn-sm" data-act="unblock" data-uid="${user.id}">解除</button>
                  </div>`).join("")
              : `<p class="hint">还没有屏蔽任何人。遇到不舒服的内容可以随时屏蔽或举报。</p>`}
          </div>

          <div class="panel side-panel">
            <span class="eyebrow">账号</span>
            <div class="iris-rule iris-rule--fade"></div>

            <div class="toggle-row">
              <div class="text">
                <b>在这台设备上保持登录</b>
                <span>关闭后，关掉浏览器就要重新输密码</span>
              </div>
              <button class="switch" data-keep-login
                aria-checked="${Store.keep === "1"}" aria-label="在这台设备上保持登录"></button>
            </div>

            <div class="iris-rule iris-rule--fade"></div>

            <div class="toggle-row">
              <div class="text">
                <b>修改密码</b>
                <span>改完所有设备都要重新登录</span>
              </div>
              <button class="btn btn-ghost btn-sm" data-action="open-password-modal">去修改</button>
            </div>

            <div class="iris-rule iris-rule--fade"></div>
            <p class="hint">注销会彻底删除你的资料、伙伴关系、联系方式与成就，无法恢复。</p>
            <button class="btn btn-danger btn-block" data-action="delete-account">注销并删除账号</button>
          </div>
        </div>
      </div>`;
  }

  async function saveProfile(form) {
    const errorBox = $("#profile-error");
    errorBox.classList.add("hidden");

    const payload = {
      nickname: form.nickname.value.trim(),
      region: form.region.value.trim(),
      edition: form.edition.value,
      bio: form.bio.value.trim(),
      play_styles: selectedChips("#edit-styles"),
      online_time: selectedChips("#edit-times"),
      looking_for: selectedChips("#edit-wants"),
      contact_qq: form.contact_qq.value.trim(),
      contact_wechat: form.contact_wechat.value.trim(),
      contact_other: form.contact_other.value.trim(),
      contact_note: form.contact_note.value.trim(),
    };

    try {
      const result = await Api.updateProfile(payload);
      Store.user = result.user;
      await refreshOverview();
      toast("资料已保存", "ok");
      toastUnlocked(result.unlocked);
      await setView("me");
    } catch (error) {
      errorBox.textContent = error.message;
      errorBox.classList.remove("hidden");
    }
  }

  async function togglePrivacy(key, element) {
    const next = element.getAttribute("aria-checked") !== "true";
    element.setAttribute("aria-checked", String(next));
    try {
      const result = await Api.updatePrivacy({ [key]: next });
      Store.user = result.user;
      await refreshOverview();
      toast(next ? "已开启" : "已关闭", "ok");
    } catch (error) {
      element.setAttribute("aria-checked", String(!next));
      toast(error.message, "bad");
    }
  }

  /* ============================================================
     十、资料卡与举报弹窗
     ============================================================ */

  async function openProfileModal(userId) {
    const data = await Api.viewUser(userId);
    const user = data.user;
    const relation = user.relation || {};
    const score = user.score_info || { score: 0, reasons: [] };

    const tagList = ["play_styles", "online_time", "looking_for"]
      .map((key) => (user[key] || []).map((tag) => `<span class="tag">${esc(tag)}</span>`).join(""))
      .join("");

    let actionButtons = "";
    if (!user.is_self) {
      if (relation.is_connected) {
        actionButtons += `<button class="btn btn-primary" data-action="close-modal">已交换联系方式</button>`;
      } else if (relation.request_state === "outgoing") {
        actionButtons += `<button class="btn" disabled>申请已发送</button>`;
      } else if (relation.request_state === "incoming") {
        actionButtons += `<button class="btn btn-primary" data-action="accept-from" data-rid="${relation.request_id}">同意 TA 的申请</button>`;
      } else if (user.allow_contact_request) {
        // ！坑：这里必须用 data-act。分发改写时会写成 `action || "@" + act`，
        //   也就是说 data-action="x" 去匹配 case "x"，data-act="x" 才匹配 case "@x"。
        //   写错成 data-action="greet" 会去找不存在的 case "greet" ——
        //   按钮静默失效：点了没反应，控制台也不报错。
        actionButtons += `<button class="btn btn-primary" data-act="greet" data-uid="${user.id}">申请交换联系方式</button>`;
      } else {
        actionButtons += `<button class="btn" disabled>暂不接收交换申请</button>`;
      }
      actionButtons += `<button class="btn btn-ghost" data-act="report" data-uid="${user.id}">举报</button>`;
      actionButtons += relation.blocked_by_me
        ? `<button class="btn btn-ghost" data-act="unblock" data-uid="${user.id}">解除屏蔽</button>`
        : `<button class="btn btn-danger" data-act="block" data-uid="${user.id}">屏蔽</button>`;
    }

    openModal(`
      <div class="row" style="gap:1.1rem;align-items:flex-start">
        ${avatar(user, "xl")}
        <div class="stack-sm" style="flex:1">
          <span class="eyebrow eyebrow--iris">${esc(user.edition_label)}</span>
          <h2 class="display display-md">${esc(user.nickname)}</h2>
          <div class="row wrap" style="gap:.5rem">
            ${presenceDot(user.online)}
            <span class="hint">${user.online ? "在线" : `最后活跃 ${fmtRelative(user.last_seen)}`}</span>
            ${ageTag(user)}
            ${user.region ? `<span class="tag">${esc(user.region)}</span>` : ""}
            <span class="tag tag--iris">Lv.${user.level} · ${esc(user.level_title)}</span>
          </div>
        </div>
        ${!user.is_self ? `
          <div class="score-box">
            <span class="num iris-text">${score.score}</span>
            <span class="cap">match</span>
          </div>` : ""}
      </div>

      <div class="iris-rule iris-rule--fade" style="margin:1.2rem 0"></div>

      <p class="lede" style="margin-bottom:1rem">${bioOf(user)}</p>

      ${score.reasons && score.reasons.length ? `
        <ul class="reason-list" style="list-style:none;margin:0 0 1rem;padding:0">
          ${score.reasons.map((reason) => `<li><span class="spark">✦</span><span>${esc(reason)}</span></li>`).join("")}
        </ul>` : ""}

      <div class="chip-row">${tagList}</div>

      ${user.contact ? `
        <div class="section-title"><h2>联系方式</h2><span class="rule"></span></div>
        ${contactRows(user.contact, user.nickname)}` : ""}

      ${data.badges.length ? `
        <div class="section-title"><h2>已解锁成就</h2><span class="rule"></span></div>
        <div class="chip-row">
          ${data.badges.map((badge) => `<span class="tag tag--iris"><span class="tag-ico" style="${McfrIcons.toneStyle(badge.code)}">${McfrIcons.badge(badge.code, badge.glyph)}</span>${esc(badge.name)}</span>`).join("")}
        </div>` : ""}

      <div class="row" style="justify-content:flex-end;gap:.5rem;margin-top:1.4rem">
        <button class="btn btn-ghost" data-action="close-modal">关闭</button>
        ${actionButtons}
      </div>`, { wide: true });
  }

  function openReportModal(user) {
    const reasons = (Store.meta && Store.meta.report_reasons) || [];
    openModal(`
      <span class="eyebrow eyebrow--iris">Report</span>
      <h2 class="display display-md" style="margin:.35rem 0 0">举报 ${esc(user.nickname)}</h2>
      <p class="hint" style="margin-top:.5rem">管理员会人工复核。恶意举报会影响你的账号信用。</p>

      <div class="iris-rule iris-rule--fade" style="margin:1.1rem 0"></div>

      <div class="reason-picker" id="reason-picker">
        ${reasons.map((reason) => `
          <button type="button" class="reason-option" data-reason="${esc(reason)}" aria-pressed="false">${esc(reason)}</button>`).join("")}
      </div>

      <label class="field" style="margin-top:.9rem">
        <span>补充说明（可留空）</span>
        <textarea class="textarea" id="report-detail" rows="3" maxlength="300"></textarea>
      </label>

      <p class="error-text hidden" id="report-error"></p>

      <div class="row" style="justify-content:flex-end;gap:.5rem;margin-top:1.2rem">
        <button class="btn btn-ghost" data-action="close-modal">取消</button>
        <button class="btn btn-primary" data-action="submit-report" data-uid="${user.id}">提交举报</button>
      </div>`);
  }

  /* ============================================================
     形象：皮肤与头像的上传
     ============================================================ */

  /* 页面渲染完后，把带 data-skin-model / data-skin-head 的 canvas 画出来 */
  function mountSkins(root) {
    if (window.McfrSkin) McfrSkin.hydrate(root || document);
  }
  function setMediaStatus(text, kind) {
    const node = $("#media-status");
    if (!node) return;
    node.textContent = text || "";
    node.classList.toggle("hidden", !text);
    node.classList.toggle("hint--bad", kind === "bad");
  }

  function pickFile(accept) {
    return new Promise((resolve) => {
      const input = document.createElement("input");
      input.type = "file";
      input.accept = accept;
      input.addEventListener("change", () => resolve((input.files && input.files[0]) || null));
      input.click();
    });
  }

  function loadImageFile(file) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const image = new Image();
      image.onload = () => {
        URL.revokeObjectURL(url);
        resolve(image);
      };
      image.onerror = () => {
        URL.revokeObjectURL(url);
        reject(new Error("这个文件读不出来，换一张图试试。"));
      };
      image.src = url;
    });
  }

  /* 把用户选的图缩成正方形 PNG（居中裁切，像 CSS 的 object-fit: cover）。
     在浏览器里先把尺寸处理完，服务端就不需要任何图像库了。 */
  async function toSquarePng(file, side = 256) {
    const image = await loadImageFile(file);
    const canvas = document.createElement("canvas");
    canvas.width = side;
    canvas.height = side;
    const ctx = canvas.getContext("2d");
    const scale = Math.max(side / image.width, side / image.height);
    const width = image.width * scale;
    const height = image.height * scale;
    ctx.drawImage(image, (side - width) / 2, (side - height) / 2, width, height);
    return new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
  }

  /* 上传成功后重新拉一次个人中心，头像与预览会跟着刷新 */
  async function refreshAfterMediaChange(message) {
    await renderMe();
    mountSkins($("#view"));
    toast(message, "ok");
  }

  async function uploadSkin() {
    const file = await pickFile("image/png,image/*");
    if (!file) return;
    setMediaStatus("正在上传皮肤…");
    try {
      await Api.uploadSkin(file);
      await refreshAfterMediaChange("皮肤已更新");
    } catch (error) {
      setMediaStatus("");
      toast(error.message, "bad");
    }
  }

  async function uploadAvatar() {
    const file = await pickFile("image/*");
    if (!file) return;
    setMediaStatus("正在处理头像…");
    try {
      const blob = await toSquarePng(file, 256);
      if (!blob) throw new Error("图片处理失败，换一张试试。");
      await Api.uploadAvatar(blob);
      await refreshAfterMediaChange("头像已更新");
    } catch (error) {
      setMediaStatus("");
      toast(error.message, "bad");
    }
  }

  async function removeMedia(kind) {
    try {
      if (kind === "skin") await Api.removeSkin();
      else await Api.removeAvatar();
      await refreshAfterMediaChange(kind === "skin" ? "已移除皮肤" : "已恢复默认头像");
    } catch (error) {
      toast(error.message, "bad");
    }
  }

  /* ============================================================
     十一、帖子
     ============================================================ */

  const POST_TITLE_MAX = 80;
  const POST_IMAGE_MAX = 12;
  // 前端自己压到这个体积以内再上传。后端上限是 4MB，留一点余量：
  // 网络传输和 JSON 包装都会让实际请求体比 blob 大一点点。
  const POST_IMAGE_TARGET_BYTES = 3 * 1024 * 1024;

  // 输入防抖。每敲一个字就打一次接口既浪费，也会让请求乱序返回 ——
  // 先发的后到，列表就停在了半路那个关键词上。
  let postSearchTimer = 0;
  let circleSearchTimer = 0;
  let peopleTimer = 0;

  /* 等比缩到 maxSide 以内，转成 PNG。
     ！和 toSquarePng 分开、而不是复用它：那个是「居中裁成正方形」
       （头像要的就是一张方脸），这个是「整张装得下」——
       帖子配图裁掉两边，往往正好把红石线路的两头裁没了。 */
  async function toScaledPng(file, maxSide) {
    const image = await loadImageFile(file);
    const scale = Math.min(1, maxSide / Math.max(image.width, image.height));
    const width = Math.max(1, Math.round(image.width * scale));
    const height = Math.max(1, Math.round(image.height * scale));
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(image, 0, 0, width, height);
    return new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
  }

  /* 逐步降尺寸，直到体积达标。
     ！为什么要循环：游戏截图大片纯色、PNG 压得很小，而手机拍的照片
       同样边长下能大好几倍 —— 一张 1600px 的照片按 PNG 存有七八 MB，
       直接顶到服务端上限。与其让用户看到「太大了请换一张」然后
       不知道该怎么办，不如这里自己往下试几档。 */
  async function compressForPost(file) {
    let blob = null;
    for (const side of [1600, 1200, 900, 640]) {
      blob = await toScaledPng(file, side);
      if (!blob) break;
      if (blob.size <= POST_IMAGE_TARGET_BYTES) return blob;
    }
    if (!blob) throw new Error("图片处理失败，换一张试试。");
    return blob;
  }

  /* 制作人员。作者排在最前面（后端固定给的），extra 是额外添加的人。 */
  function makerChips(credits) {
    return (credits || []).map((item) => `
      <span class="maker">
        ${avatar(item.user, "sm")}
        <span class="maker__name">${esc(item.user.nickname)}</span>
        ${item.role ? `<span class="maker__role">${esc(item.role)}</span>` : ""}
      </span>`).join("");
  }

  /* 时间线卡片。
     ！需求写得很明确：列表**只显示制作人员和标题**，一个字的正文都不放。
       摘要看着无害，但它会让「点进去看全文」失去理由，
       也最容易把一篇本来完整的话断章取义成另一个意思。 */
  function postCard(item, options = {}) {
    const stats = [
      `赞 ${item.like_count}`,
      `评论 ${item.comment_count}`,
      item.image_count ? `图 ${item.image_count}` : "",
    ].filter(Boolean).map((text) => `<span class="post-stat">${text}</span>`).join("");

    /* options.hideCircle：在圈子主页里不要每张卡片都标一遍圈子名 ——
       那一整页都属于同一个圈子，标了纯属噪音。而在「帖子 → 我发的」里
       这个标签是有用的（否则看不出哪篇发在圈子里、哪篇发在广场上）。 */
    const circleTag = item.circle && !options.hideCircle
      ? `<span class="tag tag--iris">${esc(item.circle.name || "圈子")}</span>`
      : "";
    const statusTag = item.status && item.status !== "published"
      ? `<span class="tag tag--warn">${item.status === "hidden" ? "已被下架" : "已删除"}</span>`
      : "";
    const tags = circleTag || statusTag
      ? `<div class="chip-row">${circleTag}${statusTag}</div>`
      : "";

    return `
      <article class="post-card edge" data-post="${item.id}">
        <!-- 整卡可点，但用一层透明按钮而不是把整张卡做成 button：
             button 的内容模型只允许「短语内容」，而下面有 div（头像）。
             写进去浏览器能跑，HTML 却是错的，读屏也会读得乱七八糟。 -->
        <button type="button" class="post-card__open" data-act="post-open"
                data-id="${item.id}" aria-label="打开帖子：${esc(item.title)}"></button>
        <div class="post-card__head">
          <h3 class="post-card__title">${esc(item.title)}</h3>
          <time class="post-card__time">${fmtRelative(item.created_at)}</time>
        </div>
        ${tags}
        <div class="post-card__makers">
          <span class="eyebrow">制作</span>
          ${makerChips(item.credits)}
        </div>
        <div class="post-card__foot">${stats}</div>
      </article>`;
  }

  /* 正文渲染。实现搬到了 richtext.js —— 编辑器和详情页要共用同一套
     「文档 JSON → HTML」逻辑，放在这里的话编辑器就用不上了。
     ！安全性由那边保证：它把每个文字段都转义过再拼。 */
  function renderBlocks(blocks) {
    return RICH.render(blocks);
  }

  /* ---------------- 列表 ---------------- */

  async function renderPosts() {
    const view = $("#view");
    const keyword = State.posts.query || "";

    view.innerHTML = `
      <div class="view-head">
        <div class="titles">
          <span class="eyebrow eyebrow--iris">Posts</span>
          <h1 class="display display-lg">大家<span class="iris-text">写</span>的帖子</h1>
          <p class="lede">同好们分享的建筑、红石、地图和心得。点进去可以点赞、评论，也可以转发给站内的朋友。</p>
        </div>
      </div>

      <!-- 需求：最上方是搜索栏，搜索栏右侧是发布按钮 -->
      <div class="posts-bar">
        <div class="search-field">
          <span class="search-icon" aria-hidden="true">${McfrIcons.line("search")}</span>
          <input id="post-search" type="search" autocomplete="off" spellcheck="false"
                 value="${esc(keyword)}"
                 placeholder="搜索帖子 —— 标题或正文里的词"
                 aria-label="搜索帖子"/>
          <button type="button" class="search-clear${keyword ? "" : " hidden"}"
                  data-act="post-search-clear" aria-label="清除搜索">✕</button>
        </div>
        <button class="btn btn-primary" data-act="post-new">写帖子</button>
      </div>

      <div class="tab-switch posts-tabs" role="tablist">
        <button role="tab" data-posts-scope="all"
                aria-selected="${!State.posts.mineOnly}">全部</button>
        <button role="tab" data-posts-scope="mine"
                aria-selected="${State.posts.mineOnly}">我发的</button>
      </div>

      <div id="posts-list"></div>`;

    // ！「我发的」这一档不是可有可无的：被管理员下架的帖子在「全部」里
    //   是看不到的，而作者必须能找到它 —— 否则他会发现自己发过的东西
    //   凭空消失，而且哪里都找不着，也不知道为什么。
    tabBar($(".posts-tabs"));
    await loadPosts();
  }

  function paintPosts() {
    const list = $("#posts-list");
    if (!list) return;
    const { items, query, hasMore } = State.posts;

    if (!items.length) {
      list.innerHTML = `
        <div class="empty">
          <span class="glyph">◈</span>
          <p>${query ? "没有搜到相关的帖子" : "还没有人发过帖子"}</p>
          <p class="hint">${query
            ? "换个词试试，或者清空搜索看看全部。"
            : "第一篇就交给你了 —— 点右上角那颗「写帖子」。"}</p>
        </div>`;
      return;
    }

    list.innerHTML = `<div class="post-grid">${items.map((item) => postCard(item)).join("")}</div>`
      + (hasMore
        ? `<div class="more-row"><button class="btn btn-ghost" data-act="posts-more">加载更多</button></div>`
        : "");
  }

  async function loadPosts(append = false) {
    const list = $("#posts-list");
    if (!append && list) list.innerHTML = `<div class="load-hint">${cubeLoader()}</div>`;
    const page = append ? State.posts.page + 1 : 1;
    try {
      const data = State.posts.mineOnly
        ? await Api.myPosts(page, State.posts.query || "")
        : await Api.posts({ q: State.posts.query || "", page });
      State.posts = {
        ...State.posts,
        items: append ? [...State.posts.items, ...data.items] : data.items,
        total: data.total,
        page: data.page,
        hasMore: data.has_more,
      };
      paintPosts();
    } catch (error) {
      if (list) {
        list.innerHTML = `<div class="empty"><span class="glyph">⚠</span><p>${esc(error.message)}</p></div>`;
      }
    }
  }

  /* ---------------- 详情 ---------------- */

  async function renderPost() {
    if (!State.routePostId) {
      await setView("posts");
      return;
    }
    const view = $("#view");
    view.innerHTML = `<div class="load-hint">${cubeLoader()}</div>`;

    const post = await Api.post(State.routePostId);
    State.post = post;
    State.postComments = { items: [], total: 0, page: 1, hasMore: false };

    view.innerHTML = `
      <div class="post-view">
        <!-- 返回去处要跟着帖子走：圈子里的帖子回圈子，广场上的帖子回帖子列表。
             两者都回「帖子列表」的话，从圈子里点进一篇再返回，就被扔到广场上了。 -->
        <button class="btn btn-ghost btn-sm post-back" data-act="post-back">
          ${post.circle ? "← 返回圈子" : "← 返回帖子列表"}
        </button>

        <article class="post-article panel edge">
          <header class="post-head">
            <h1 class="display display-md">${esc(post.title)}</h1>
            <div class="post-byline">
              ${avatar(post.author, "sm")}
              <span class="post-byline__name">${esc(post.author.nickname)}</span>
              <time>${fmtTime(post.created_at)}</time>
              <span class="post-byline__dot">·</span>
              <span>${post.view_count} 浏览</span>
              ${post.circle
                ? `<span class="tag tag--iris">${esc(post.circle.name || "圈子")}</span>`
                : ""}
              <!-- 「已修改」和知乎一样只标一下，不列修改历史 -->
              ${post.updated_at ? `<span class="tag">已修改</span>` : ""}
              ${post.status !== "published"
                ? `<span class="tag tag--warn">${esc(post.status_label)}</span>` : ""}
            </div>
            <div class="post-credits">
              <span class="eyebrow">制作人员</span>
              ${makerChips(post.credits)}
            </div>
          </header>

          <div class="post-body rt-body">${renderBlocks(post.blocks)}</div>

          <footer class="post-actions">
            <button class="btn ${post.liked ? "btn-primary" : "btn-ghost"}"
                    data-act="post-like" data-id="${post.id}"
                    aria-pressed="${post.liked}">
              赞 <span class="post-like-count">${post.like_count}</span>
            </button>
            <!-- 圈子帖没有「转发」：圈子里的内容只该留在圈子里。
                 后端也会拒（posts.share），这里不画按钮只是别让人白点一次。 -->
            ${post.circle ? "" : `<button class="btn btn-ghost" data-act="post-share" data-id="${post.id}">转发给朋友</button>`}
            ${post.is_author ? `
              <button class="btn btn-ghost" data-act="post-edit" data-id="${post.id}">编辑</button>
              <button class="btn btn-danger" data-act="post-delete" data-id="${post.id}">删除</button>` : ""}
          </footer>
        </article>

        <section class="panel panel--pad post-comments">
          <h2 class="post-comments__title">评论<span class="count count--soft">${post.comment_count}</span></h2>
          <div class="comment-form">
            <textarea class="textarea" id="comment-input" rows="2" maxlength="500"
                      placeholder="说点什么…… 友善一点。"></textarea>
            <button class="btn btn-primary" data-act="comment-send" data-id="${post.id}">发表评论</button>
          </div>
          <div id="comment-list"></div>
        </section>
      </div>`;

    mountSkins(view);
    await loadComments(true);
  }

  function paintComments() {
    const host = $("#comment-list");
    if (!host) return;
    const { items, hasMore } = State.postComments;

    if (!items.length) {
      host.innerHTML = `<p class="hint hint--center">还没有人评论，来说第一句吧。</p>`;
      return;
    }

    host.innerHTML = `
      <ul class="comment-list">
        ${items.map((item) => `
          <li class="comment" data-cid="${item.id}">
            ${avatar(item.user, "sm")}
            <div class="comment__body">
              <div class="comment__head">
                <b>${esc(item.user.nickname)}</b>
                <time>${fmtRelative(item.created_at)}</time>
                ${item.mine || (State.post && State.post.is_author)
                  ? `<button class="comment__del" data-act="comment-del"
                             data-cid="${item.id}" data-id="${State.post.id}"
                             aria-label="删除这条评论">删除</button>` : ""}
              </div>
              <p class="comment__text">${esc(item.content).replace(/\n/g, "<br>")}</p>
            </div>
          </li>`).join("")}
      </ul>
      ${hasMore ? `<div class="more-row"><button class="btn btn-ghost btn-sm"
                     data-act="comments-more">加载更多评论</button></div>` : ""}`;
  }

  async function loadComments(reset = false) {
    const host = $("#comment-list");
    if (!host || !State.post) return;
    if (reset) host.innerHTML = `<div class="load-hint">${cubeLoader()}</div>`;
    const page = reset ? 1 : State.postComments.page + 1;
    const data = await Api.comments(State.post.id, page);
    State.postComments = {
      items: reset ? data.items : [...State.postComments.items, ...data.items],
      total: data.total,
      page: data.page,
      hasMore: data.has_more,
    };
    paintComments();
  }

  /* ---------------- 圈子（v0.34） ----------------

     为什么圈子值得单独一块界面，而不是并进「帖子」加个筛选：
     「帖子」是一个**广场** —— 谁都能看见、谁都能写，看的是全站的公共内容；
     「圈子」是一间**房间** —— 只有推门进来的人看得见里面的话。
     这两件事的默认行为正好相反（广场默认公开、房间默认私密），
     合在一个页面里迟早会有人发错地方。

     ！圈子里的帖子就是**普通的帖子**：同一个 postCard、同一个详情页、
       同一个富文本编辑器。唯一的区别是发帖时多带一个 circle_id，
       而且只有成员能看到。别为圈子另写一套「发帖 / 点赞 / 评论」——
       那会变成两份会逐渐跑偏的实现，而且后台审核要审两遍。 */

  /* meta 还没回来时用的兜底。真正的清单来自 /api/meta 的 circle_topics，
     和 backend/circles.py 的 CIRCLE_TOPICS 是同一份（那边是唯一的一处定义）。 */
  const CIRCLE_TOPICS_FALLBACK = ["生存", "建筑", "红石", "模组", "小游戏", "联机", "聊天", "综合"];

  /* 圈子徽记的色相：由名字派生，同一个圈子每次进来都是同一种颜色。
     和用户头像的 avatar_hue 是同一个思路（后端 security.pick_avatar_hue），
     只是圈子不需要跨设备一致，所以就在前端算。 */
  function hueOf(text) {
    let hue = 0;
    for (const ch of String(text || "")) {
      hue = (hue * 131 + (ch.codePointAt(0) || 0)) % 360;
    }
    return hue;
  }

  function circleBadge(name, big = false) {
    const text = String(name || "圈");
    // ！Array.from 而不是 text[0]：名字以 emoji 开头时，[0] 只取到半个代理对，
    //   会渲染成一个方块问号。
    const first = Array.from(text)[0] || "圈";
    return `<span class="circle-badge${big ? " circle-badge--lg" : ""}"
                  style="--hue:${hueOf(text)}" aria-hidden="true">${esc(first)}</span>`;
  }

  function circleTopics() {
    const list = (Store.meta && Store.meta.circle_topics) || [];
    return list.length ? list : CIRCLE_TOPICS_FALLBACK;
  }

  function circleCard(item) {
    const acts = item.joined
      ? `<button class="btn btn-ghost btn-sm" data-act="circle-open" data-id="${item.id}">进入</button>
         <button class="btn btn-danger btn-sm" data-act="circle-leave"
                 data-id="${item.id}" data-name="${esc(item.name)}">删除</button>`
      : `<button class="btn btn-primary btn-sm" data-act="circle-join"
                 data-id="${item.id}" data-name="${esc(item.name)}">加入</button>`;

    return `
      <article class="circle-card edge" data-cid="${item.id}">
        <!-- 整卡可点，和帖子卡片同一个做法：一层透明按钮盖在最上面，
             而不是把整张卡做成 button（那里面还有 div，HTML 不合法）。 -->
        <button type="button" class="circle-card__open" data-act="circle-open"
                data-id="${item.id}" aria-label="进入圈子：${esc(item.name)}"></button>
        <div class="circle-card__top">
          ${circleBadge(item.name)}
          <div class="circle-card__id">
            <b>${esc(item.name)}</b>
            <span class="handle">${esc(item.topic || "综合")}</span>
          </div>
        </div>
        <p class="circle-card__intro">${esc(item.intro || "这个圈子还没写介绍。")}</p>
        <div class="circle-card__foot">
          <span>${item.member_count} 位成员</span>
          <span>${item.post_count} 篇帖子</span>
          ${item.is_owner ? `<span>我建的</span>` : ""}
        </div>
        <!-- ！这一层要盖在透明按钮之上（z-index），否则点「删除」会被
             整卡可点的那层吃掉，变成「进入圈子」。 -->
        <div class="circle-card__acts">${acts}</div>
      </article>`;
  }

  async function renderCircles() {
    const view = $("#view");
    const mine = State.circles.tab === "mine";
    const keyword = State.circles.query || "";

    view.innerHTML = `
      <div class="view-head">
        <div class="titles">
          <span class="eyebrow eyebrow--iris">Circles</span>
          <h1 class="display display-lg">一起玩的<span class="iris-text">圈子</span></h1>
          <p class="lede">不用等算法发牌 —— 挑一个主题进去。圈子里发的帖子只有成员看得见，所以这里说话可以随便一点。</p>
        </div>
      </div>

      <div class="posts-bar">
        <div class="search-field">
          <span class="search-icon" aria-hidden="true">${McfrIcons.line("search")}</span>
          <input id="circle-search" type="search" autocomplete="off" spellcheck="false"
                 value="${esc(keyword)}"
                 placeholder="搜圈子 —— 名字或介绍里的词"
                 aria-label="搜索圈子"/>
          <button type="button" class="search-clear${keyword ? "" : " hidden"}"
                  data-act="circle-search-clear" aria-label="清除搜索">✕</button>
        </div>
        <button class="btn btn-primary" data-act="circle-new">建一个圈子</button>
      </div>

      <div class="tab-switch circles-tabs" role="tablist">
        <button role="tab" data-circle-scope="mine"
                aria-selected="${mine}">我的圈子</button>
        <button role="tab" data-circle-scope="all"
                aria-selected="${!mine}">发现圈子</button>
      </div>

      <div id="circle-list"></div>`;

    tabBar($(".circles-tabs"));
    await loadCircles();
  }

  function paintCircles() {
    const host = $("#circle-list");
    if (!host) return;
    const { items, hasMore, tab, query } = State.circles;

    if (!items.length) {
      const mineEmpty = tab === "mine" && !query;
      host.innerHTML = mineEmpty
        ? `<div class="empty">
             <span class="glyph">◯</span>
             <p>你还没加入任何圈子</p>
             <p class="hint">去「发现圈子」看看，或者自己建一个 —— 圈子里发的东西只有成员看得见。</p>
             <div class="empty-acts">
               <button class="btn btn-ghost" data-circle-scope="all">去发现圈子</button>
               <button class="btn btn-primary" data-act="circle-new">建一个圈子</button>
             </div>
           </div>`
        : `<div class="empty">
             <span class="glyph">◯</span>
             <p>${query ? "没有搜到相关的圈子" : (tab === "mine" ? "你还没加入任何圈子" : "还没有人建过圈子")}</p>
             <p class="hint">${query
               ? "换个词试试，或者清空搜索看看全部。"
               : "第一个圈子就交给你了 —— 点右上角那颗「建一个圈子」。"}</p>
           </div>`;
      return;
    }

    host.innerHTML = `<div class="circle-grid">${items.map(circleCard).join("")}</div>`
      + (hasMore
        ? `<div class="more-row"><button class="btn btn-ghost" data-act="circles-more">加载更多</button></div>`
        : "");
  }

  async function loadCircles(append = false) {
    const host = $("#circle-list");
    if (!append && host) host.innerHTML = `<div class="load-hint">${cubeLoader()}</div>`;
    const page = append ? State.circles.page + 1 : 1;
    try {
      const data = await Api.circles({
        scope: State.circles.tab,
        q: State.circles.query || "",
        page,
      });
      State.circles = {
        ...State.circles,
        items: append ? [...State.circles.items, ...data.items] : data.items,
        total: data.total,
        page: data.page,
        hasMore: data.has_more,
      };
      paintCircles();
    } catch (error) {
      if (host) {
        host.innerHTML = `<div class="empty"><span class="glyph">⚠</span><p>${esc(error.message)}</p></div>`;
      }
    }
  }

  /* 建圈弹窗。刻意做成弹窗而不是一个整页：只有四个字段，
     整页反而要处理「填了一半切走」「返回时草稿还在不在」这些事。 */
  function openCircleModal() {
    const topics = circleTopics();
    openModal(`
      <h2 class="display display-sm">建一个圈子</h2>
      <p class="hint">圈子 = 一群人 + 只有这群人看得见的帖子。起个别人一眼看懂的名字，再写一句介绍。</p>
      <div class="iris-rule iris-rule--fade" style="margin:1rem 0"></div>
      <div class="stack">
        <label class="field">
          <span>圈子名</span>
          <input class="input" id="circle-name" maxlength="20" autocomplete="off"
                 placeholder="例如 红石研究所 / 末地跑酷队">
        </label>
        <label class="field">
          <span>一句话介绍</span>
          <input class="input" id="circle-intro" maxlength="60" autocomplete="off"
                 placeholder="例如 每周分享一个红石作品，互相点评">
        </label>
        <div class="field">
          <span>主题</span>
          <div class="chip-row" id="circle-topic-row">
            ${topics.map((topic, index) => `
              <button type="button" class="chip" data-circle-topic="${esc(topic)}"
                      aria-pressed="${index === 0}">${esc(topic)}</button>`).join("")}
          </div>
        </div>
        <label class="field">
          <span>详细说明（选填）</span>
          <textarea class="textarea" id="circle-about" rows="4" maxlength="500"
            placeholder="这个圈子打算一起做什么？比如：每周交一个作品；新手提问也可以。"></textarea>
        </label>
        <p class="hint">建好之后你就是圈主。你退出这个圈子时，它会自动交给最早加入的那一位；如果只剩你一个人，圈子会一起解散。</p>
        <div class="row-between wrap" style="margin-top:.5rem">
          <button class="btn btn-ghost" type="button" data-action="close-modal">取消</button>
          <button class="btn btn-primary" type="button" data-act="circle-submit">创建圈子</button>
        </div>
      </div>`);
    const nameInput = $("#circle-name");
    if (nameInput) nameInput.focus();
  }

  async function submitCircleForm() {
    const button = $("[data-act='circle-submit']");
    const name = ($("#circle-name") || {}).value || "";
    const intro = ($("#circle-intro") || {}).value || "";
    const about = ($("#circle-about") || {}).value || "";
    const topic = ($("#circle-topic-row .chip[aria-pressed='true']") || {}).dataset?.circleTopic || "";

    if (!name.trim()) {
      toast("先给圈子起个名字", "bad");
      return;
    }

    const original = button ? button.textContent : "";
    if (button) {
      button.disabled = true;
      button.textContent = "创建中…";
    }
    try {
      const circle = await Api.createCircle({ name, intro, about, topic });
      closeModal();
      // 列表缓存必须丢掉：新建的圈子要出现在「我的圈子」里
      ViewCache.delete("circles");
      State.circles.tab = "mine";
      State.circles.items = [];
      toast("圈子建好了，进去看看吧", "ok");
      await setView("circle", { circleId: circle.id });
    } catch (error) {
      toast(error.message, "bad");
      if (button) {
        button.disabled = false;
        button.textContent = original;
      }
    }
  }

  async function joinCircle(button) {
    const id = Number(button.dataset.id);
    if (!id) return;
    button.disabled = true;
    try {
      const circle = await Api.joinCircle(id);
      toast(`已加入「${circle.name}」`, "ok");
      // 两个列表都脏了：加入会影响「我的圈子」，也会让「发现圈子」里那一张的按钮变样
      ViewCache.delete("circles");
      State.circles.items = [];
      if (State.circle && State.circle.id === id) {
        await renderCircle();          // 就在这个圈子的主页上：重画成「已加入」
      } else {
        await setView("circle", { circleId: id });
      }
    } catch (error) {
      toast(error.message, "bad");
      button.disabled = false;
    }
  }

  async function leaveCircle(button) {
    const id = Number(button.dataset.id);
    if (!id) return;
    const name = button.dataset.name || "这个圈子";
    const isOwner = Boolean(State.circle && State.circle.id === id && State.circle.is_owner);
    /* ！window.confirm 的正文由浏览器自己画，走不到 applyDynamic 那套 DOM 翻译，
       所以这里显式过一次字典（t 支持 {name} 这种占位符）。
       两句话的后果差得很远，圈主那句必须把「会解散」说清楚。 */
    const message = I18n.t(
      isOwner
        ? "你是「{name}」的圈主。退出后它会交给最早加入的那位成员；\n如果圈子里只剩你一个人，圈子会直接解散，里面的帖子也会一起删除。\n\n确定退出吗？"
        : "退出「{name}」？\n退出后就看不到这个圈子里的帖子了，你写过的帖子还留在里面。",
      { name },
    );
    if (!window.confirm(message)) return;

    button.disabled = true;
    try {
      const result = await Api.leaveCircle(id);
      ViewCache.delete("circles");
      State.circles.items = [];
      if (result.dissolved) {
        toast(`「${name}」解散了，里面的 ${result.post_count} 篇帖子一起删除了`, "ok");
      } else if (result.successor) {
        toast(`已退出，圈主交给了 ${result.successor.nickname}`, "ok");
      } else {
        toast("已退出圈子", "ok");
      }
      await setView("circles", { fresh: true });
    } catch (error) {
      toast(error.message, "bad");
      button.disabled = false;
    }
  }

  /* ---------------- 圈子主页 ---------------- */

  async function renderCircle() {
    if (!State.routeCircleId) {
      await setView("circles");
      return;
    }
    const view = $("#view");
    view.innerHTML = `<div class="load-hint">${cubeLoader()}</div>`;

    const circle = await Api.circle(State.routeCircleId);
    State.circle = circle;
    State.circlePosts = { items: [], total: 0, page: 1, hasMore: false };

    const members = (circle.members || []).map((item) => `
      <span class="maker">
        ${avatar(item.user, "sm")}
        <span class="maker__name">${esc(item.user.nickname)}</span>
        ${item.role === "owner" ? `<span class="maker__role">圈主</span>` : ""}
      </span>`).join("");

    view.innerHTML = `
      <div class="circle-view">
        <button class="btn btn-ghost btn-sm post-back" data-act="circle-back">← 返回圈子列表</button>

        <section class="panel panel--pad circle-hero">
          <div class="circle-hero__top">
            ${circleBadge(circle.name, true)}
            <div class="circle-hero__id">
              <h1 class="display display-md">${esc(circle.name)}</h1>
              <p class="meta-line">${esc(circle.intro || "这个圈子还没写介绍。")}</p>
              <div class="chip-row">
                <span class="tag tag--iris">${esc(circle.topic || "综合")}</span>
                <span class="tag">${circle.member_count} 位成员</span>
                <span class="tag">${circle.post_count} 篇帖子</span>
                ${circle.is_owner ? `<span class="tag">你是圈主</span>` : ""}
              </div>
            </div>
          </div>

          ${circle.about
            ? `<div class="circle-about">${esc(circle.about).replace(/\n/g, "<br>")}</div>`
            : ""}

          <div class="circle-members">
            <span class="eyebrow">成员</span>
            ${members || `<span class="hint">还没有别人</span>`}
          </div>

          <div class="circle-actions">
            ${circle.joined
              ? `<button class="btn btn-primary" data-act="circle-write" data-id="${circle.id}">
                   在圈子里发帖
                 </button>
                 <button class="btn btn-danger" data-act="circle-leave"
                         data-id="${circle.id}" data-name="${esc(circle.name)}">退出圈子</button>`
              : `<button class="btn btn-primary" data-act="circle-join"
                         data-id="${circle.id}" data-name="${esc(circle.name)}">加入圈子</button>
                 <span class="hint">加入之后才能看到圈子里发了什么</span>`}
          </div>
        </section>

        <div id="circle-posts"></div>
      </div>`;

    mountSkins(view);
    if (circle.joined) {
      await loadCirclePosts(true);
    } else {
      paintCirclePosts();
    }
  }

  function paintCirclePosts() {
    const host = $("#circle-posts");
    const circle = State.circle;
    if (!host || !circle) return;

    if (!circle.joined) {
      host.innerHTML = `
        <div class="circle-gate panel panel--pad">
          <span class="glyph" aria-hidden="true">🔒</span>
          <p>加入之后才能看到圈子里的帖子</p>
          <p class="hint">圈子里的内容只对成员可见 —— 这也是很多人愿意在这里说话的原因。</p>
          <button class="btn btn-primary" data-act="circle-join"
                  data-id="${circle.id}" data-name="${esc(circle.name)}">加入圈子</button>
        </div>`;
      return;
    }

    const { items, hasMore } = State.circlePosts;
    if (!items.length) {
      host.innerHTML = `
        <div class="empty">
          <span class="glyph">◈</span>
          <p>这个圈子里还没有帖子</p>
          <p class="hint">第一篇就交给你了 —— 上面那颗「在圈子里发帖」。</p>
        </div>`;
      return;
    }

    host.innerHTML = `<div class="post-grid">${items.map((item) => postCard(item, { hideCircle: true })).join("")}</div>`
      + (hasMore
        ? `<div class="more-row"><button class="btn btn-ghost" data-act="circle-posts-more">加载更多</button></div>`
        : "");
  }

  async function loadCirclePosts(reset = false) {
    const host = $("#circle-posts");
    if (!State.circle) return;
    if (reset && host) host.innerHTML = `<div class="load-hint">${cubeLoader()}</div>`;
    const page = reset ? 1 : State.circlePosts.page + 1;
    try {
      const data = await Api.circlePosts(State.circle.id, { page });
      State.circlePosts = {
        items: reset ? data.items : [...State.circlePosts.items, ...data.items],
        total: data.total,
        page: data.page,
        hasMore: data.has_more,
      };
      paintCirclePosts();
    } catch (error) {
      if (host) {
        host.innerHTML = `<div class="empty"><span class="glyph">⚠</span><p>${esc(error.message)}</p></div>`;
      }
    }
  }

  /* ---------------- 编辑器（富文本） ----------------
     Word 那种手感：选中一段可以加粗 / 变色 / 改字号，图片和附件能插在
     正文的任何位置。

     ！为什么用 contenteditable 而不是一堆小输入框：
       行内格式（半句话加粗）只有它能自然表达，而且浏览器的选区、
       撤销、粘贴都是现成的。自己造一套要写的量级完全不同。

     ！但存进数据库的是**结构化文档 JSON，不是 HTML** ——
       理由写在 richtext.js 开头那一大段里。

     职责划分（很重要）：
       · 打字期间 **DOM 是唯一真相**，input 事件只做一个防抖的「快照」
       · 点发布 / 离开编辑器时才正式 serialize
     这样就不会出现「每敲一个字重画一遍」那种丢焦点的经典 bug。 */

  const RICH = window.McfrRich;
  const CREDIT_ROLES = [
    "策划", "编辑", "文案", "校对", "美术", "建筑",
    "红石", "编程", "测试", "素材", "配音",
  ];
  let editorSnapshotTimer = 0;
  /* 正文里最后一次有效的光标位置。
     插图 / 附件要先把文件传上去，回来已经是几百毫秒之后 ——
     那时候"当前选区"早就不作数了，只能拿这份快照当落点。 */
  let editorAnchor = null;

  function newEditorState(post) {
    const meId = (Store.user || {}).id;
    if (!post) {
      // circle：这篇帖子要发到哪个圈子里（null = 公开发在「帖子」里）。
      // ！草稿状态里也带着它 —— 用户写到一半切走、回来接着写，
      //   发出去的地方不能变。
      return { id: 0, title: "", credits: [], blocks: [], circle: null };
    }
    /* 作者本人由后端固定排在名单最前面，编辑区上方已经给他单独一个「作者」
       标签，这里只保留「额外添加的人」。
       ！同时也按帖子作者过滤一遍：只要 Store.user 还没加载完，
         meId 就是 undefined，只靠它过滤会让作者在名单里出现两次。 */
    const authorId = (post.author || {}).id;
    return {
      id: post.id,
      title: post.title || "",
      // ！编辑已有帖子时**不能改它发在哪儿**：圈子帖改到广场上，等于把
      //   圈子里说过的话搬到全站；广场帖改到圈子里，等于把公开内容藏起来。
      //   所以这里只读回来显示，不接受切换（后端 update_post 也不动 circle_id）。
      circle: post.circle ? { id: post.circle.id, name: post.circle.name || "" } : null,
      credits: (post.credits || [])
        .filter((item) => item.user.id !== meId && item.user.id !== authorId)
        .map((item) => ({
          user_id: item.user.id,
          nickname: item.user.nickname,
          role: item.role || "",
        })),
      blocks: (post.blocks || []).map((block) => ({ ...block })),
    };
  }

  const editorEl = () => $("#rt-editor");

  function toolbarHtml() {
    const swatches = (group, items) => items.map(([value, label]) => `
      <button type="button" class="rt-swatch-btn" data-rt-run="${group}"
              data-rt-value="${value}" title="${label}">
        <span class="rt-swatch-card rt-${group === "color" ? "c" : group === "highlight" ? "h" : "z"}-${value}">${label}</span>
      </button>`).join("");

    return `
      <div class="rt-toolbar" id="rt-toolbar" role="toolbar" aria-label="正文格式">
        <button type="button" class="rt-btn" data-rt-cmd="undo" title="撤销">↶</button>
        <button type="button" class="rt-btn" data-rt-cmd="redo" title="重做">↷</button>

        <span class="rt-sep"></span>
        <select class="rt-select" id="rt-block-style" aria-label="段落样式">
          <option value="p">正文</option>
          <option value="h2">大标题</option>
          <option value="h3">小标题</option>
          <option value="blockquote">引用</option>
        </select>

        <span class="rt-sep"></span>
        <button type="button" class="rt-btn rt-btn--b" data-rt-cmd="bold" title="加粗">B</button>
        <button type="button" class="rt-btn rt-btn--i" data-rt-cmd="italic" title="斜体">I</button>
        <button type="button" class="rt-btn rt-btn--u" data-rt-cmd="underline" title="下划线">U</button>
        <button type="button" class="rt-btn rt-btn--s" data-rt-cmd="strikeThrough" title="删除线">S</button>

        <span class="rt-sep"></span>
        <div class="rt-menu">
          <button type="button" class="rt-btn rt-btn--wide" data-rt-menu="color"
                  title="文字颜色">A<span class="rt-caret">▾</span></button>
          <div class="rt-menu__panel">
            ${swatches("color", RICH.COLORS)}
            <button type="button" class="rt-reset" data-rt-run="color" data-rt-value="">默认颜色</button>
          </div>
        </div>
        <div class="rt-menu">
          <button type="button" class="rt-btn rt-btn--wide" data-rt-menu="highlight"
                  title="背景高亮">▨<span class="rt-caret">▾</span></button>
          <div class="rt-menu__panel">
            ${swatches("highlight", RICH.HIGHLIGHTS)}
            <button type="button" class="rt-reset" data-rt-run="highlight" data-rt-value="">去掉高亮</button>
          </div>
        </div>
        <div class="rt-menu">
          <button type="button" class="rt-btn rt-btn--wide" data-rt-menu="size"
                  title="字号">A<span class="rt-caret">▾</span></button>
          <div class="rt-menu__panel">
            ${swatches("size", RICH.SIZES)}
            <button type="button" class="rt-reset" data-rt-run="size" data-rt-value="">默认字号</button>
          </div>
        </div>

        <span class="rt-sep"></span>
        <button type="button" class="rt-btn" data-rt-cmd="justifyLeft" title="左对齐">⇤</button>
        <button type="button" class="rt-btn" data-rt-cmd="justifyCenter" title="居中">↔</button>
        <button type="button" class="rt-btn" data-rt-cmd="justifyRight" title="右对齐">⇥</button>

        <span class="rt-sep"></span>
        <button type="button" class="rt-btn" data-rt-cmd="insertUnorderedList" title="无序列表">•—</button>
        <button type="button" class="rt-btn" data-rt-cmd="insertOrderedList" title="有序列表">1.</button>

        <span class="rt-sep"></span>
        <button type="button" class="rt-btn rt-btn--wide" data-rt-insert="image"
                title="插入图片"><span aria-hidden="true">🖼</span> 图片</button>
        <button type="button" class="rt-btn rt-btn--wide" data-rt-insert="file"
                title="插入附件"><span aria-hidden="true">📦</span> 附件</button>
        <button type="button" class="rt-btn rt-btn--wide" data-rt-cmd="removeFormat"
                title="清除格式">清除格式</button>
      </div>`;
  }

  async function renderEditor() {
    const view = $("#view");
    view.innerHTML = `<div class="load-hint">${cubeLoader()}</div>`;

    let post = null;
    if (State.routePostId) {
      post = await Api.post(State.routePostId);
      if (!post.is_author) {
        view.innerHTML = `<div class="empty">
          <span class="glyph">⚠</span><p>只能编辑自己发布的帖子</p></div>`;
        return;
      }
    }

    /* 这次要发到哪个圈子（v0.34）：
       · 编辑已有帖子 → 跟着帖子走（post.circle），不能在编辑器里改
       · 在圈子里新写 → 地址里带着 /circles/7/new，进来先问一次接口
         ！为什么要问接口而不是从上一页传过来：刷新、直接粘链接、前进后退
           都会绕开「上一页」，那时名字和权限就都没了。而且这次调用同时
           也是权限检查 —— 不是成员会拿到 403，正好在这里把话说清楚。
    */
    let circle = post ? post.circle : null;
    if (!post && State.routeCircleId) {
      try {
        circle = await Api.circle(State.routeCircleId);
      } catch (error) {
        view.innerHTML = `<div class="empty">
          <span class="glyph">⚠</span><p>${esc(error.message)}</p></div>`;
        return;
      }
      if (!circle.joined) {
        view.innerHTML = `<div class="empty">
          <span class="glyph">⚠</span><p>加入这个圈子之后，才能在里面发帖</p></div>`;
        return;
      }
    }

    const target = post ? post.id : 0;
    const targetCircle = circle ? circle.id : 0;
    const draftedCircle = State.editor && State.editor.circle ? State.editor.circle.id : 0;
    // ！除了帖子 id，发帖的去处也算「换了一篇」：在 A 圈写了一半切到 B 圈，
    //   草稿必须重来，不然会把 A 圈的草稿发到 B 圈去。
    if (!State.editor || State.editor.id !== target || draftedCircle !== targetCircle) {
      State.editor = newEditorState(post);
      State.editor.circle = circle ? { id: circle.id, name: circle.name || "" } : null;
    }

    view.innerHTML = `
      <div class="editor">
        <div class="editor__head">
          <div class="titles">
            <span class="eyebrow eyebrow--iris">${post ? "Edit" : (circle ? "New in circle" : "New post")}</span>
            <h1 class="display display-md">${post
              ? "编辑帖子"
              : (circle ? `在「${esc(circle.name)}」里写一篇` : "写一篇帖子")}</h1>
          </div>
          <div class="editor__head-actions">
            <button class="btn btn-ghost" data-act="post-cancel">取消</button>
            <button class="btn btn-primary" data-act="post-save">
              ${post ? "保存修改" : "发布"}
            </button>
          </div>
        </div>

        <!-- 发到哪儿必须写在最显眼的位置：同一条内容发在广场上和发在圈子里，
             看到的人可能是「全站所有人」和「这个圈子的几个人」，差得很远。 -->
        <div class="editor-where${circle ? " editor-where--circle" : ""}">
          ${circle
            ? `<span class="eyebrow">发到圈子</span>
               <b>${esc(circle.name)}</b>
               <span class="hint">只有这个圈子的成员看得到</span>`
            : `<span class="eyebrow">发到</span>
               <b>帖子广场</b>
               <span class="hint">全站所有登录的人都能看到</span>`}
        </div>

        <input class="input editor__title" id="editor-title"
               maxlength="${POST_TITLE_MAX}"
               placeholder="标题 —— 一句话说清这篇写的是什么"
               value="${esc(State.editor.title)}">

        ${toolbarHtml()}

        <div class="rt-editor" id="rt-editor" contenteditable="true"
             spellcheck="false" role="textbox" aria-multiline="true"
             aria-label="正文" data-placeholder="在这里写正文…… 选中文字可以加粗、变色、改字号；图片和附件可以插在任何位置"></div>

        <div class="editor__foot">
          <span class="hint" id="editor-hint"></span>
          <span class="hint" id="editor-count"></span>
        </div>

        <section class="editor__credits panel panel--pad">
          <span class="eyebrow">制作人员</span>
          <p class="hint">一起做这篇帖子的朋友。只能添加已经在本站注册过的用户，所以做之前先让 TA 注册一下。右边可以写 TA 负责什么（策划、编辑……）。</p>
          <div id="editor-credits"></div>
          <div class="credit-add">
            <input class="input" id="credit-search" autocomplete="off"
                   placeholder="输入昵称或用户名搜人" aria-label="搜索要添加的用户">
            <div id="credit-results" class="credit-results hidden"></div>
          </div>
        </section>
      </div>`;

    const editor = editorEl();
    // 光标行为：回车产生 <p>（而不是 <div>），这样序列化时块边界很清楚
    try {
      document.execCommand("defaultParagraphSeparator", false, "p");
      document.execCommand("styleWithCSS", false, true);
    } catch (error) { /* 老浏览器不支持也无所谓，后面会兜住 */ }
    RICH.loadInto(editor, State.editor.blocks);
    editorAnchor = null;   // 换了文档，旧的光标快照指向的节点已经不在了

    paintCredits();
    setEditorHint("");
    updateEditorCount();
  }

  function setEditorHint(text, kind = "hint") {
    const node = $("#editor-hint");
    if (!node) return;
    node.textContent = text || "";
    node.classList.toggle("hint--bad", kind === "bad");
  }

  function updateEditorCount() {
    const node = $("#editor-count");
    const editor = editorEl();
    if (!node || !editor) return;
    const blocks = RICH.serialize(editor);
    node.textContent = `约 ${RICH.plainOf(blocks).replace(/\s/g, "").length} 字`;
  }

  /* 打字时的防抖快照：把 DOM 里现在的内容存回 State.editor.blocks。
     ！这样切走再回来（比如去看一眼别人的帖子找灵感）内容还在。
       不做的话，contenteditable 里的东西一离开就没了。 */
  function snapshotEditorLater() {
    window.clearTimeout(editorSnapshotTimer);
    editorSnapshotTimer = window.setTimeout(() => {
      const editor = editorEl();
      if (!editor || !State.editor) return;
      const title = $("#editor-title");
      if (title) State.editor.title = title.value;
      State.editor.blocks = RICH.serialize(editor);
      updateEditorCount();
    }, 400);
  }

  /* ---------------- 富文本命令 ---------------- */

  function focusEditor() {
    const editor = editorEl();
    if (!editor || document.activeElement === editor) return;
    /* ！只有在编辑器确实失去焦点时才补一次 focus()。
       若选区其实在编辑器里、只是 activeElement 跑到别处，
       直接 focus() 会把光标丢回开头 —— 选区就没了，
       表现是「第一次点加粗毫无反应，第二次才灵」。 */
    const selection = window.getSelection();
    const saved = selection && selection.rangeCount ? selection.getRangeAt(0).cloneRange() : null;
    editor.focus();
    if (saved && editor.contains(saved.commonAncestorContainer)) {
      selection.removeAllRanges();
      selection.addRange(saved);
    }
  }

  function execFormat(command, value = null) {
    focusEditor();
    try {
      document.execCommand(command, false, value);
    } catch (error) { /* 命令不支持就算了，界面不炸 */ }
    // 列表 / 引用这几个命令可能产出 <p><ul> 这种非法嵌套，就地扶正
    RICH.normalize(editorEl());
    snapshotEditorLater();
  }

  /* 工具条上的分组名（color / highlight / size）和真正落到 DOM 上的
     类名前缀（rt-c- / rt-h- / rt-z-）不是一回事。
     ！这张表必须和 richtext.js 的 formatOf()、以及 app.css 里的
       .rt-c-* / .rt-h-* / .rt-z-* 三组规则对齐：
       名字对不上，颜色在 DOM 里看着是对的，
       一保存就被 formatOf 当陌生类丢掉 —— 界面正常、数据没了。 */
  const RUN_GROUP_CLASS = { color: "rt-c", highlight: "rt-h", size: "rt-z" };

  /* 给选区套一层带格式类的 <span>。
     ！颜色 / 高亮 / 字号没用 execCommand：带色和字号它要么只给
       1-7 档（产出已经废弃的 <font size>），要么靠内联 style ——
       而内联 style 正是我们不想存进数据库的东西。
       自己包一层反而简单：选区内只需要处理「套上」和「摘掉」两种情况。
     group 是 "color" / "highlight" / "size"，value 为空表示恢复默认。 */
  function applyRunClass(group, value) {
    const editor = editorEl();
    const selection = window.getSelection();
    if (!editor || !selection || !selection.rangeCount) return;

    const range = selection.getRangeAt(0);
    if (range.collapsed || !editor.contains(range.commonAncestorContainer)) {
      setEditorHint("先选中一段文字，再挑颜色或字号", "bad");
      return;
    }
    setEditorHint("");

    // 先把选区里同类的旧格式摘掉，免得套娃（红→蓝会变成红的外面包蓝的）
    stripRunClass(range, group, editor);

    if (value) {
      const prefix = RUN_GROUP_CLASS[group];
      if (!prefix) return;
      const span = document.createElement("span");
      span.className = `${prefix}-${value}`;
      try {
        span.appendChild(range.extractContents());
        range.insertNode(span);
      } catch (error) {
        return;   // 跨块选区在浏览器之间差异很大，失败就当没点
      }
      // 选回去，方便连着改第二项
      selection.removeAllRanges();
      const next = document.createRange();
      next.selectNodeContents(span);
      selection.addRange(next);
    }
    snapshotEditorLater();
  }

  function stripRunClass(range, group, editor) {
    const prefix = `${RUN_GROUP_CLASS[group] || group}-`;
    for (const el of editor.querySelectorAll("span")) {
      const has = Array.from(el.classList).some((cls) => cls.startsWith(prefix));
      if (!has) continue;
      try {
        if (!range.intersectsNode(el)) continue;
      } catch (error) {
        continue;
      }
      el.className = Array.from(el.classList)
        .filter((cls) => !cls.startsWith(prefix)).join(" ");
      // 类摘光了就把这层拆掉，别留一堆没用的空 span
      if (!el.className) el.replaceWith(...Array.from(el.childNodes));
    }
  }

  /* 「清格式」按的是 execCommand('removeFormat')，它只认自己管的那几种
     内联样式，对我们自己套上去的 rt-c-* / rt-h-* / rt-z-* 视而不见。
     ！不补这一刀，用户点完「清格式」会发现颜色还在，以为按钮坏了。 */
  function clearRunClasses() {
    const editor = editorEl();
    const selection = window.getSelection();
    if (!editor || !selection || !selection.rangeCount) return;
    const range = selection.getRangeAt(0);
    if (!editor.contains(range.commonAncestorContainer)) return;
    for (const group of Object.keys(RUN_GROUP_CLASS)) {
      stripRunClass(range, group, editor);
    }
  }

  /* ---------------- 插图 / 附件 ---------------- */

  function ensureTrailingParagraph(node) {
    const next = node.nextElementSibling;
    if (!next || next.dataset.rtBlock || next.tagName === "FIGURE") {
      const p = document.createElement("p");
      p.appendChild(document.createElement("br"));
      node.after(p);
      return p;
    }
    return next;
  }

  /* 光标所在的那个顶层块（<p> / <h2> / <blockquote> / 我们自己插的图……）。 */
  function topLevelBlock(start, editor) {
    let el = start.nodeType === 3 ? start.parentNode : start;
    if (!el || el === editor) return null;
    while (el.parentNode && el.parentNode !== editor) el = el.parentNode;
    return el.parentNode === editor ? el : null;
  }

  /* 把块从光标处劈成两半，返回装着后半截的新块。 */
  function splitBlockAt(block, range) {
    const tail = block.cloneNode(false);
    const cut = document.createRange();
    cut.setStart(range.endContainer, range.endOffset);
    cut.setEndAfter(block);
    tail.appendChild(cut.extractContents());
    if (!tail.textContent && !tail.children.length) {
      tail.appendChild(document.createElement("br"));
    }
    return tail;
  }

  function insertAtCursor(node) {
    const editor = editorEl();
    if (!editor) return null;

    const selection = window.getSelection();
    /* ！这里是"异步插入"：插图 / 附件都要先上传，回来时已经过了几百毫秒，
       期间浏览器的选区完全可能被挪到别处（实测会塌到段首）。
       所以优先用选区变化时记下的锚点，只有锚点也用不上才退回当前选区。 */
    const live = selection && selection.rangeCount &&
      editor.contains(selection.getRangeAt(0).commonAncestorContainer)
      ? selection.getRangeAt(0) : null;
    const anchor = editorAnchor && editor.isConnected &&
      editor.contains(editorAnchor.commonAncestorContainer) ? editorAnchor : null;
    const range = live || anchor;

    /* ！图片 / 附件是块级元素，只能待在块与块之间。
       千万别直接 range.insertNode()：光标在 <p> 里时，那会把整个块
       塞进段落内部 —— 而 <p> 只装得下行内内容，这是非法结构。
       浏览器不会报错，但序列化时它会被剥离出去，表现是
       「图片根本没插在光标处，而是跑到文章末尾了」，极难排查。
       正确做法是把段落从光标处劈开：前半截留在原处，后半截搬进新段落，
       图片插在两者中间 —— 这也是 Word / B 站编辑器的行为。 */
    let caret = null;
    if (!range) {
      editor.appendChild(node);
      caret = ensureTrailingParagraph(node);
    } else {
      const block = topLevelBlock(range.startContainer, editor);
      const atomic = block && (block.tagName === "UL" || block.tagName === "OL" ||
        (block.dataset && block.dataset.rtBlock));
      if (!block || atomic) {
        // 列表整块不劈；落在图片 / 附件上时也是整个放到它后面
        const tail = document.createElement("p");
        tail.appendChild(document.createElement("br"));
        if (block) block.after(node);
        else range.insertNode(node);
        node.after(tail);
        caret = tail;
      } else {
        const tail = splitBlockAt(block, range);
        block.after(node);
        node.after(tail);
        caret = tail;
      }
    }

    // 光标落到后面那段里，接着就能打字
    const next = document.createRange();
    next.setStart(caret, 0);
    next.collapse(true);
    if (selection) {
      selection.removeAllRanges();
      selection.addRange(next);
    }
    editorAnchor = next.cloneRange();
    snapshotEditorLater();
    return caret;
  }

  function buildImageNode(file) {
    const node = document.createElement("div");
    node.className = "rt-figure";
    node.dataset.rtBlock = "image";
    node.dataset.file = file;
    node.innerHTML = `
      <div class="rt-figure__img" contenteditable="false">
        <img src="${RICH.esc(RICH.IMAGE_URL(file))}" alt="">
        <button type="button" class="rt-del" data-rt-del aria-label="删除这张图片">✕</button>
      </div>
      <div class="rt-cap" data-rt-cap data-placeholder="写图片注释（选填）"></div>`;
    return node;
  }

  function buildFileNode(file, name, size) {
    const node = document.createElement("div");
    node.className = "rt-file";
    node.dataset.rtBlock = "file";
    node.dataset.file = file;
    node.dataset.name = name;
    node.dataset.size = String(size || 0);
    node.contentEditable = "false";
    node.innerHTML = `
      <span class="rt-file__icon" aria-hidden="true">📦</span>
      <span class="rt-file__body">
        <b class="rt-file__name">${RICH.esc(name)}</b>
        ${size ? `<span class="rt-file__size">${RICH.esc(RICH.humanSize(size))}</span>` : ""}
      </span>
      <button type="button" class="rt-del" data-rt-del aria-label="删除这个附件">✕</button>`;
    return node;
  }

  async function insertImage() {
    const editor = editorEl();
    if (!editor) return;
    const current = RICH.serialize(editor);
    if (RICH.countImages(current) >= POST_IMAGE_MAX) {
      toast(`一篇帖子最多放 ${POST_IMAGE_MAX} 张图`, "bad");
      return;
    }
    const file = await pickFile("image/*");
    if (!file) return;

    setEditorHint("正在处理并上传图片…");
    try {
      const blob = await compressForPost(file);
      const result = await Api.uploadPostImage(blob);
      insertAtCursor(buildImageNode(result.file));
      setEditorHint("");
      toast("图片已插入", "ok");
    } catch (error) {
      setEditorHint(error.message, "bad");
    }
  }

  async function insertAttachment() {
    if (!editorEl()) return;
    // ！accept 只写扩展名，不写 MIME：不同系统给 .mcaddon 报的 MIME 五花八门，
    //   写 MIME 反而会把文件挡在选择器外面。真正的把关在服务端。
    const file = await pickFile("");
    if (!file) return;

    setEditorHint(`正在上传 ${file.name}…`);
    try {
      const result = await Api.uploadAttachment(file);
      insertAtCursor(buildFileNode(result.file, result.name, result.size));
      setEditorHint("");
      toast(`附件已插入（${RICH.humanSize(result.size)}）`, "ok");
    } catch (error) {
      setEditorHint(error.message, "bad");
    }
  }

  /* ---------------- 制作人员 ---------------- */

  function roleOptionsHtml(current) {
    const known = CREDIT_ROLES.includes(current);
    const options = CREDIT_ROLES.map((role) =>
      `<option value="${role}" ${role === current ? "selected" : ""}>${role}</option>`).join("");
    const extra = current && !known
      ? `<option value="${esc(current)}" selected>${esc(current)}</option>` : "";
    return `<option value="" ${current ? "" : "selected"}>关系…</option>${extra}${options}`;
  }

  function paintCredits() {
    const host = $("#editor-credits");
    if (!host || !State.editor) return;
    const me = Store.user || {};
    host.innerHTML = `
      <div class="credit-list">
        <span class="credit-chip credit-chip--me">
          ${avatar(me, "sm")}
          <b>${esc(me.nickname || "")}</b>
          <i>作者</i>
        </span>
        ${State.editor.credits.map((item, index) => `
          <span class="credit-chip credit-chip--edit">
            <b>${esc(item.nickname)}</b>
            <select class="credit-role" data-role-index="${index}"
                    aria-label="${esc(item.nickname)} 负责什么">
              ${roleOptionsHtml(item.role || "")}
            </select>
            <button type="button" class="credit-chip__x" data-act="credit-del"
                    data-i="${index}" aria-label="把 ${esc(item.nickname)} 移出名单">✕</button>
          </span>`).join("")}
      </div>`;
    mountSkins(host);
  }

  /* ---------------- 转发 ---------------- */

  async function openShareDialog(postId) {
    State.sharePicked = [];
    openModal(`
      <h2 class="display display-md">转发给朋友</h2>
      <p class="hint">对方下次登录会立刻看到提示；没来得及看的会留在「消息」里，不会丢。</p>
      <div class="credit-add credit-add--inline">
        <input class="input" id="share-search" autocomplete="off"
               placeholder="输入昵称或用户名搜人" aria-label="搜索要转发给谁">
        <div id="share-results" class="credit-results"></div>
      </div>
      <div id="share-picked" class="credit-list"></div>
      <label class="field"><span>附一句话（选填）</span>
        <input class="input" id="share-note" maxlength="120"
               placeholder="推荐你看看这个！"></label>
      <div class="step-actions">
        <button class="btn btn-ghost" data-action="close-modal">取消</button>
        <button class="btn btn-primary" data-act="share-send" data-id="${postId}">转发</button>
      </div>`);
    paintSharePicked();
    await searchPeople("share-results", "", "share");
  }

  function paintSharePicked() {
    const host = $("#share-picked");
    if (!host) return;
    if (!State.sharePicked.length) {
      host.innerHTML = `<p class="hint">还没有选人。</p>`;
      return;
    }
    host.innerHTML = `
      <div class="credit-list">
        ${State.sharePicked.map((item, index) => `
          <span class="credit-chip">
            <b>${esc(item.nickname)}</b>
            <button type="button" class="credit-chip__x" data-act="share-drop"
                    data-i="${index}" aria-label="移除">✕</button>
          </span>`).join("")}
      </div>`;
  }

  /* 搜人。制作人员和转发对象共用这一份 —— 两边要找的都是「站内的人」，
     分开写迟早会有一边忘了同步。mode 只决定点一下是「添加」还是「选中」。 */
  function clearPeopleResults(mode) {
    const input = $(mode === "share" ? "#share-search" : "#credit-search");
    if (input) input.value = "";
    const box = $(mode === "share" ? "#share-results" : "#credit-results");
    if (box) {
      box.innerHTML = "";
      box.classList.add("hidden");
    }
  }

  async function searchPeople(hostId, keyword, mode) {
    const host = $("#" + hostId);
    if (!host) return;
    try {
      const data = await Api.postUsers(keyword);
      const items = data.items || [];
      host.classList.remove("hidden");
      host.innerHTML = items.length
        ? items.map((user) => `
            <button type="button" class="people-row" data-act="people-pick"
                    data-mode="${mode}" data-id="${user.id}"
                    data-nickname="${esc(user.nickname)}">
              ${avatar(user, "sm")}
              <span class="people-row__name">${esc(user.nickname)}</span>
              <span class="people-row__handle">@${esc(user.username)}</span>
            </button>`).join("")
        : `<p class="hint" style="padding:.6rem .8rem">没有找到人。名字要完全对上一部分才行。</p>`;
    } catch (error) {
      host.classList.remove("hidden");
      host.innerHTML = `<p class="hint" style="padding:.6rem .8rem">${esc(error.message)}</p>`;
    }
  }

  /* ============================================================
     十二、信息中心
     ============================================================ */

  const NOTIFY_TEXT = {
    share: "转发了帖子给你",
    like: "点了赞",
    comment: "评论了",
    credit: "把你写进了制作人员",
    system: "发来一条通知",
  };

  async function renderMessages() {
    const view = $("#view");
    view.innerHTML = `
      <div class="view-head">
        <div class="titles">
          <span class="eyebrow eyebrow--iris">Messages</span>
          <h1 class="display display-lg">消息<span class="iris-text">中心</span></h1>
          <p class="lede">别人转发给你的帖子、给你的点赞和评论都在这里。读过之后角标就会消失。</p>
        </div>
        <button class="btn btn-ghost" data-act="inbox-read-all">全部标为已读</button>
      </div>
      <div class="tab-switch inbox-tabs" role="tablist">
        <button role="tab" data-inbox-box="all"
                aria-selected="${State.inbox.box === "all"}">全部</button>
        <button role="tab" data-inbox-box="unread"
                aria-selected="${State.inbox.box === "unread"}">未读</button>
      </div>
      <div id="inbox-list"></div>`;

    await loadInbox(true);
  }

  function paintInbox() {
    const host = $("#inbox-list");
    if (!host) return;
    const { items } = State.inbox;

    if (!items.length) {
      host.innerHTML = `
        <div class="empty">
          <span class="glyph">✉</span>
          <p>${State.inbox.box === "unread" ? "没有未读消息" : "还没有收到过消息"}</p>
          <p class="hint">别人转发帖子给你、或者给你的帖子点赞评论时，这里就会有东西。</p>
        </div>`;
      return;
    }

    host.innerHTML = `<ul class="inbox-list">${items.map((item) => {
      const who = item.actor ? esc(item.actor.nickname) : "系统";
      const action = NOTIFY_TEXT[item.kind] || "发来一条通知";
      const extra = item.note ? `<p class="inbox__note">「${esc(item.note)}」</p>`
        : item.excerpt ? `<p class="inbox__note">${esc(item.excerpt)}</p>` : "";
      return `
        <li class="inbox-item${item.read ? "" : " inbox-item--unread"}"
            ${item.post_id && !item.post_gone
              ? `data-act="inbox-open" data-id="${item.post_id}" data-notify="${item.id}" role="button" tabindex="0"`
              : ""}>
          ${item.actor ? avatar(item.actor, "sm") : `<span class="inbox__sys">✉</span>`}
          <div class="inbox__body">
            <p class="inbox__line">
              <b>${who}</b> ${action}
              ${item.post_title ? `<span class="inbox__title">《${esc(item.post_title)}》</span>` : ""}
            </p>
            ${extra}
            <time class="inbox__time">${fmtRelative(item.created_at)}</time>
            ${item.post_gone ? `<span class="hint">（这篇帖子已经不在了）</span>` : ""}
          </div>
          ${item.read ? "" : `<span class="inbox__dot" aria-label="未读"></span>`}
        </li>`;
    }).join("")}</ul>`
      + (State.inbox.hasMore
        ? `<div class="more-row"><button class="btn btn-ghost" data-act="inbox-more">加载更多</button></div>`
        : "");
  }

  async function loadInbox(reset = false) {
    const host = $("#inbox-list");
    if (reset && host) host.innerHTML = `<div class="load-hint">${cubeLoader()}</div>`;
    const page = reset ? 1 : State.inbox.page + 1;
    const data = await Api.notifications(State.inbox.box, page);
    State.inbox = {
      items: reset ? data.items : [...State.inbox.items, ...data.items],
      total: data.total,
      page: data.page,
      hasMore: data.has_more,
      box: State.inbox.box,
      unread: data.unread,
    };
    paintInbox();
    // 侧栏角标读的是 overview.stats，这里把它同步一下，免得要等下一次刷新
    if (State.overview && State.overview.stats) {
      State.overview.stats.unread_notifications = data.unread;
      renderNav();
    }
  }

  /* ---------------- 交互 ----------------
     帖子 / 消息相关的点击全部收在这里。
     ！为什么单独一个函数，而不是接着往下面那条长 if 链里塞：
       那条链已经有三十多个分支了，再加十几条没人看得过来。
       返回 true = 「这个点击我处理了」，主链直接 return 收工。 */

  async function saveEditor(button) {
    const editorData = State.editor;
    const editor = editorEl();
    if (!editorData || !editor) return;

    // 正式序列化：从 DOM 里读出现在的内容，而不是用那份防抖快照 ——
    // 用户可能刚敲完最后一个字就点了发布，快照还没轮到
    const blocks = RICH.serialize(editor);
    const titleNode = $("#editor-title");
    const title = (titleNode ? titleNode.value : editorData.title || "").trim();

    if (!title) {
      toast("先给帖子起个标题", "bad");
      return;
    }
    if (!blocks.length) {
      toast("正文还空着，写点什么再发布", "bad");
      return;
    }

    const editing = Boolean(editorData.id);
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "提交中…";
    try {
      const payload = {
        title,
        blocks,
        credits: editorData.credits.map((item) => ({
          user_id: item.user_id,
          role: item.role,
        })),
        // 发到哪个圈子（v0.34）。0 = 公开发在「帖子」广场上。
        // ！编辑已有帖子时后端不认这个字段（update_post 不碰 circle_id），
        //   这里照传只是为了让同一个请求体两边都能用。
        circle_id: editorData.circle ? editorData.circle.id : 0,
      };
      const saved = editing
        ? await Api.updatePost(editorData.id, payload)
        : await Api.createPost(payload);
      State.editor = null;
      // 列表的缓存必须丢掉：刚发的帖子得出现在里面
      ViewCache.delete("posts");
      // 圈子页的帖子列表和「几篇帖子」那个数字也变了
      ViewCache.delete("circles");
      if (saved.circle) {
        // 圈子帖：回到圈子看它更顺 —— 那里才是它该出现的地方。
        // 走帖子详情也行，但中间会先看到「返回帖子列表」那种错位感。
        State.circlePosts = { items: [], total: 0, page: 1, hasMore: false };
        await setView("circle", { circleId: saved.circle.id });
        toast(editing ? "改动已保存" : "发出去了，圈子里的成员都能看到了", "ok");
      } else {
        await setView("post", { postId: saved.id });
        toast(editing ? "改动已保存" : "发布成功", "ok");
      }
    } catch (error) {
      toast(error.message, "bad");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  async function likePost(button) {
    button.disabled = true;
    try {
      const result = await Api.likePost(button.dataset.id);
      if (State.post) {
        State.post.liked = result.liked;
        State.post.like_count = result.like_count;
      }
      button.classList.toggle("btn-primary", result.liked);
      button.classList.toggle("btn-ghost", !result.liked);
      button.setAttribute("aria-pressed", String(result.liked));
      const count = $(".post-like-count", button);
      if (count) count.textContent = result.like_count;
    } catch (error) {
      toast(error.message, "bad");
    } finally {
      button.disabled = false;
    }
  }

  async function removePost(postId) {
    if (!window.confirm("删除这篇帖子？下面的评论和点赞也会一起消失，而且不能恢复。")) return;
    try {
      await Api.deletePost(postId);
      ViewCache.delete("posts");
      toast("帖子已删除", "ok");
      await setView("posts", { fresh: true });
    } catch (error) {
      toast(error.message, "bad");
    }
  }

  async function sendComment() {
    const input = $("#comment-input");
    if (!input || !State.post) return;
    const content = input.value.trim();
    if (!content) {
      toast("先写点什么再发", "bad");
      return;
    }
    try {
      await Api.addComment(State.post.id, content);
      input.value = "";
      State.post.comment_count += 1;
      const counter = $(".post-comments__title .count");
      if (counter) counter.textContent = State.post.comment_count;
      await loadComments(true);
    } catch (error) {
      toast(error.message, "bad");
    }
  }

  async function handleSocialClick(target) {
    /* ---- 帖子列表 ---- */
    if (target.closest("[data-act='post-open']")) {
      await setView("post", { postId: target.closest("[data-act='post-open']").dataset.id });
      return true;
    }
    if (target.closest("[data-act='post-new']")) {
      State.editor = null;   // 新帖：把上一次没发出去的草稿丢掉
      await setView("editor", { postId: 0, circleId: 0 });
      return true;
    }
    if (target.closest("[data-act='post-back']")) {
      // 圈子里的帖子回圈子，广场上的回列表（按钮上的字也是这么写的）
      if (State.post && State.post.circle) {
        await setView("circle", { circleId: State.post.circle.id });
      } else {
        await setView("posts");
      }
      return true;
    }
    if (target.closest("[data-act='posts-more']")) {
      await loadPosts(true);
      return true;
    }
    const searchClear = target.closest("[data-act='post-search-clear']");
    if (searchClear) {
      State.posts.query = "";
      const input = $("#post-search");
      if (input) input.value = "";
      searchClear.classList.add("hidden");
      await loadPosts();
      return true;
    }
    const scope = target.closest("[data-posts-scope]");
    if (scope) {
      // 换一档就等于换了一批数据，从第一页重新拉
      State.posts.mineOnly = scope.dataset.postsScope === "mine";
      State.posts.items = [];
      await renderPosts();
      return true;
    }

    /* ---- 圈子（v0.34）----
       切档、进入、加入、退出、建圈、以及在圈子里写帖子。 */
    const circleScope = target.closest("[data-circle-scope]");
    if (circleScope) {
      State.circles.tab = circleScope.dataset.circleScope === "all" ? "all" : "mine";
      // 换一档等于换一批数据，从第一页重新拉（理由同帖子列表的 scope）
      State.circles.items = [];
      await renderCircles();
      return true;
    }
    const circleOpen = target.closest("[data-act='circle-open']");
    if (circleOpen) {
      await setView("circle", { circleId: circleOpen.dataset.id });
      return true;
    }
    if (target.closest("[data-act='circle-back']")) {
      await setView("circles");
      return true;
    }
    const circleJoin = target.closest("[data-act='circle-join']");
    if (circleJoin) {
      await joinCircle(circleJoin);
      return true;
    }
    const circleLeave = target.closest("[data-act='circle-leave']");
    if (circleLeave) {
      await leaveCircle(circleLeave);
      return true;
    }
    const circleWrite = target.closest("[data-act='circle-write']");
    if (circleWrite) {
      State.editor = null;   // 新帖：把上一次没发出去的草稿丢掉
      await setView("editor", { postId: 0, circleId: circleWrite.dataset.id });
      return true;
    }
    if (target.closest("[data-act='circle-new']")) {
      openCircleModal();
      return true;
    }
    if (target.closest("[data-act='circle-submit']")) {
      await submitCircleForm();
      return true;
    }
    if (target.closest("[data-act='circles-more']")) {
      await loadCircles(true);
      return true;
    }
    if (target.closest("[data-act='circle-posts-more']")) {
      await loadCirclePosts();
      return true;
    }
    const circleSearchClear = target.closest("[data-act='circle-search-clear']");
    if (circleSearchClear) {
      State.circles.query = "";
      const input = $("#circle-search");
      if (input) input.value = "";
      circleSearchClear.classList.add("hidden");
      await loadCircles();
      return true;
    }
    // 建圈弹窗里的主题：单选。和资料页那排 chip 不同 —— 那里可以多选，
    // 这里选两个主题没有任何意义，所以只让一个亮着。
    const topicChip = target.closest("[data-circle-topic]");
    if (topicChip) {
      $$("#circle-topic-row .chip").forEach((chip) => {
        chip.setAttribute("aria-pressed", String(chip === topicChip));
      });
      return true;
    }

    /* ---- 帖子详情 ---- */
    const editButton = target.closest("[data-act='post-edit']");
    if (editButton) {
      State.editor = null;   // 重新编辑：从服务端拉最新的一份，不要用旧草稿
      // ！circleId 必须显式传 0：不传的话会沿用上一次打开过的圈子 id，
      //   地址栏和「发到哪儿」那一行就会指向一个根本不相干的圈子。
      await setView("editor", { postId: editButton.dataset.id, circleId: 0 });
      return true;
    }
    const likeButton = target.closest("[data-act='post-like']");
    if (likeButton) {
      await likePost(likeButton);
      return true;
    }
    const deleteButton = target.closest("[data-act='post-delete']");
    if (deleteButton) {
      await removePost(deleteButton.dataset.id);
      return true;
    }
    const shareButton = target.closest("[data-act='post-share']");
    if (shareButton) {
      await openShareDialog(shareButton.dataset.id);
      return true;
    }
    if (target.closest("[data-act='comment-send']")) {
      await sendComment();
      return true;
    }
    const commentDelete = target.closest("[data-act='comment-del']");
    if (commentDelete) {
      try {
        await Api.deleteComment(commentDelete.dataset.id, commentDelete.dataset.cid);
        State.post.comment_count = Math.max(0, State.post.comment_count - 1);
        await loadComments(true);
      } catch (error) {
        toast(error.message, "bad");
      }
      return true;
    }
    if (target.closest("[data-act='comments-more']")) {
      await loadComments();
      return true;
    }

    /* ---- 编辑器 ---- */
    if (target.closest("[data-act='post-cancel']")) {
      if (!window.confirm("离开编辑器？还没发布的内容会丢掉。")) return true;
      // 取消之后回哪儿：编辑已有帖子 → 那篇帖子；在圈子里新写 → 回到圈子；
      // 在广场上新写 → 帖子列表。按原路返回，用户不会「咦我怎么跑到别处了」。
      const backTo = State.editor && State.editor.id
        ? "post"
        : (State.editor && State.editor.circle ? "circle" : "posts");
      const backId = State.editor ? State.editor.id : 0;
      const backCircle = State.editor && State.editor.circle ? State.editor.circle.id : 0;
      State.editor = null;
      await setView(backTo, { postId: backId, circleId: backCircle });
      return true;
    }
    const saveButton = target.closest("[data-act='post-save']");
    if (saveButton) {
      await saveEditor(saveButton);
      return true;
    }
    /* ---- 富文本工具条 ----
       ！点按钮会先让正文失焦，而失焦 = 选区没了。所以格式命令全在
         mousedown 阶段处理（见下面那条监听）—— 那里 preventDefault
         拦住了默认的「移动焦点」，选区还在。
         这里只兜底处理两件异步的事：插入图片、插入附件（它们要开
         文件选择框，不可能在 mousedown 里做完）。 */
    const insertButton = target.closest("[data-rt-insert]");
    if (insertButton) {
      if (insertButton.dataset.rtInsert === "image") await insertImage();
      else await insertAttachment();
      return true;
    }

    // 图片 / 附件右上角那个叉。它们被包在 contenteditable=false 里，
    // 所以按钮的点击不会被正文吃掉。
    const removeBlock = target.closest("[data-rt-del]");
    if (removeBlock) {
      const host = removeBlock.closest("[data-rt-block]");
      if (host) {
        host.remove();
        snapshotEditorLater();
      }
      return true;
    }

    const creditDelete = target.closest("[data-act='credit-del']");
    if (creditDelete) {
      State.editor.credits.splice(Number(creditDelete.dataset.i), 1);
      paintCredits();
      return true;
    }

    /* ---- 搜人（制作人员 / 转发对象共用）---- */
    const picked = target.closest("[data-act='people-pick']");
    if (picked) {
      const userId = Number(picked.dataset.id);
      const nickname = picked.dataset.nickname || "";
      if (picked.dataset.mode === "share") {
        if (!State.sharePicked.some((item) => item.user_id === userId)) {
          State.sharePicked.push({ user_id: userId, nickname });
        }
        paintSharePicked();
      } else {
        if (State.editor.credits.some((item) => item.user_id === userId)) {
          toast("TA 已经在名单里了", "bad");
        } else if (State.editor.credits.length >= 10) {
          toast("制作人员最多 10 位", "bad");
        } else {
          State.editor.credits.push({ user_id: userId, nickname, role: "" });
        }
        const search = $("#credit-search");
        if (search) search.value = "";
        const results = $("#credit-results");
        if (results) {
          results.innerHTML = "";
          results.classList.add("hidden");
        }
        paintCredits();
      }
      return true;
    }
    const shareDrop = target.closest("[data-act='share-drop']");
    if (shareDrop) {
      State.sharePicked.splice(Number(shareDrop.dataset.i), 1);
      paintSharePicked();
      return true;
    }
    const shareSend = target.closest("[data-act='share-send']");
    if (shareSend) {
      if (!State.sharePicked.length) {
        toast("先选一个人", "bad");
        return true;
      }
      const noteNode = $("#share-note");
      try {
        const result = await Api.sharePost(
          shareSend.dataset.id,
          State.sharePicked.map((item) => item.user_id),
          (noteNode && noteNode.value.trim()) || "",
        );
        closeModal();
        toast(`已转发给 ${result.sent.length} 位朋友`, "ok");
      } catch (error) {
        toast(error.message, "bad");
      }
      return true;
    }

    /* ---- 信息中心 ---- */
    if (target.closest("[data-act='inbox-read-all']")) {
      try {
        await Api.readNotifications({ all: true });
        await loadInbox(true);
        toast("已全部标为已读", "ok");
      } catch (error) {
        toast(error.message, "bad");
      }
      return true;
    }
    const inboxBox = target.closest("[data-inbox-box]");
    if (inboxBox) {
      State.inbox.box = inboxBox.dataset.inboxBox;
      State.inbox.items = [];
      await renderMessages();
      return true;
    }
    const inboxOpen = target.closest("[data-act='inbox-open']");
    if (inboxOpen) {
      // 点开就算读过了。失败也无所谓 —— 标已读只是体验，不该挡住跳转。
      Api.readNotifications({ ids: [Number(inboxOpen.dataset.notify)] }).catch(() => {});
      await setView("post", { postId: inboxOpen.dataset.id });
      return true;
    }
    if (target.closest("[data-act='inbox-more']")) {
      await loadInbox();
      return true;
    }

    return false;
  }

  /* ============================================================
     十三、全局事件委托
     ============================================================ */

  /* ---------------- 富文本工具条 ----------------
     ！这一条必须挂在 **mousedown** 阶段，不能并进下面那条 click 链。

     原因：点一个按钮会让正文失去焦点，而**选区是跟着焦点走的** ——
     等 click 触发时 selection 已经空了，命令作用不到任何东西上，
     表现就是「点了加粗没反应」。在 mousedown 里 preventDefault()
     拦住默认的焦点转移，选区原封不动，命令才落得下去。

     插入图片 / 附件不在这里做：它们要开系统文件选择框，
     没法在 mousedown 里同步完成，交给 click 阶段。 */
  document.addEventListener("mousedown", (event) => {
    const target = event.target;
    if (!target.closest) return;

    /* 「插入图片 / 插入附件」是在 click 阶段处理的（要开文件选择框，
       不可能在 mousedown 里做完）。但默认行为必须在 mousedown 这里挡掉：
       按下去的一瞬间按钮会抢走焦点，正文的选区随之作废，
       结果图片总是被丢到文章末尾，而不是光标停的地方。
       preventDefault 只拦「移动焦点」，click 事件照常发出。 */
    if (target.closest("[data-rt-insert]") && $("#rt-editor")) {
      event.preventDefault();
      return;
    }

    const tool = target.closest("[data-rt-cmd], [data-rt-run], [data-rt-menu]");
    if (!tool || $("#rt-toolbar") === null) return;

    event.preventDefault();   // ← 保住选区，本段的关键就这一行

    const menuName = tool.dataset.rtMenu;
    if (menuName) {
      const menu = tool.closest(".rt-menu");
      const wasOpen = menu.classList.contains("is-open");
      $$(".rt-menu.is-open").forEach((item) => item.classList.remove("is-open"));
      if (!wasOpen) menu.classList.add("is-open");
      return;
    }
    // 挑了具体格式就把菜单收起来
    $$(".rt-menu.is-open").forEach((item) => item.classList.remove("is-open"));

    const command = tool.dataset.rtCmd;
    if (command) {
      if (command === "removeFormat") clearRunClasses();
      execFormat(command);
      syncToolbar();
      return;
    }
    if (tool.dataset.rtRun) {
      applyRunClass(tool.dataset.rtRun, tool.dataset.rtValue || "");
    }
  });

  /* 点别处把工具条菜单收起来（不收的话它会一直浮在正文上面） */
  document.addEventListener("click", (event) => {
    if (event.target.closest && event.target.closest(".rt-menu")) return;
    $$(".rt-menu.is-open").forEach((menu) => menu.classList.remove("is-open"));
  });

  /* 选区一变就把工具条的按下状态回填，让用户知道「现在这段是粗体的」。
     顺便把「正文里的光标位置」记一份 —— 插图 / 附件是异步的，
     等上传回来时当前选区可能早就没了，那时候只能靠这份快照。 */
  document.addEventListener("selectionchange", () => {
    if (!$("#rt-editor")) return;
    syncToolbar();
    const editor = editorEl();
    const selection = window.getSelection();
    if (!editor || !selection || !selection.rangeCount) return;
    const range = selection.getRangeAt(0);
    if (editor.contains(range.commonAncestorContainer)) {
      editorAnchor = range.cloneRange();
    }
  });

  /* select 不触发 click，单独听 change */
  document.addEventListener("change", (event) => {
    const target = event.target;

    if (target.id === "rt-block-style") {
      focusEditor();
      try {
        document.execCommand("formatBlock", false, target.value);
      } catch (error) { /* 不支持就算了 */ }
      snapshotEditorLater();
      return;
    }
    if (target.classList && target.classList.contains("credit-role")) {
      const index = Number(target.dataset.roleIndex);
      if (State.editor && State.editor.credits[index]) {
        // 和后端 ROLE_MAX 对齐，超了会被截断而不是报错
        State.editor.credits[index].role = target.value.slice(0, 12);
      }
    }
  });

  function syncToolbar() {
    const bar = $("#rt-toolbar");
    const editor = editorEl();
    if (!bar || !editor) return;

    const selection = window.getSelection();
    if (!selection || !selection.rangeCount) return;
    const range = selection.getRangeAt(0);
    if (!editor.contains(range.commonAncestorContainer)) return;

    for (const button of $$("[data-rt-cmd]", bar)) {
      const command = button.dataset.rtCmd;
      let on = false;
      // 撤销 / 重做 / 清除格式没有「当前状态」，queryCommandState 会报 false
      try { on = document.queryCommandState(command); } catch (error) { on = false; }
      button.classList.toggle("is-on", Boolean(on));
    }

    const select = $("#rt-block-style");
    if (select) {
      let tag = "p";
      try {
        const value = String(document.queryCommandValue("formatBlock") || "").toLowerCase();
        if (value.includes("h2")) tag = "h2";
        else if (value.includes("h3")) tag = "h3";
        else if (value.includes("blockquote")) tag = "blockquote";
      } catch (error) { /* 拿不到就保持「正文」 */ }
      select.value = tag;
    }
  }

  /* 输入处理单独挂一条监听：上面那条点击链是异步的大链子，
     而打字是每秒好几次的事件，不该排在那后面等。

     ！这里【只写状态，不重画 DOM】。每敲一个字就重建一遍的话，
       正在打字的地方会立刻失去焦点 —— 表现就是
       「打一个字，光标跳走」，是最招人烦的一类 bug。
       富文本正文更是如此：重画一次连光标位置都找不回来了。 */
  document.addEventListener("input", (event) => {
    const target = event.target;

    if (target.id === "editor-title" && State.editor) {
      State.editor.title = target.value;
      return;
    }
    if (target.id === "rt-editor") {
      // 只做防抖快照，绝不重画
      snapshotEditorLater();
      return;
    }
    if (target.id === "post-search") {
      const value = target.value;
      window.clearTimeout(postSearchTimer);
      postSearchTimer = window.setTimeout(async () => {
        State.posts.query = value.trim();
        await loadPosts();
      }, 300);
      const clear = $("[data-act='post-search-clear']");
      if (clear) clear.classList.toggle("hidden", !value.trim());
      return;
    }
    if (target.id === "circle-search") {
      const value = target.value;
      window.clearTimeout(circleSearchTimer);
      circleSearchTimer = window.setTimeout(async () => {
        State.circles.query = value.trim();
        await loadCircles();
      }, 300);
      const clear = $("[data-act='circle-search-clear']");
      if (clear) clear.classList.toggle("hidden", !value.trim());
      return;
    }
    if (target.id === "credit-search") {
      window.clearTimeout(peopleTimer);
      peopleTimer = window.setTimeout(
        () => searchPeople("credit-results", target.value.trim(), "credit"), 250);
      return;
    }
    if (target.id === "share-search") {
      window.clearTimeout(peopleTimer);
      peopleTimer = window.setTimeout(
        () => searchPeople("share-results", target.value.trim(), "share"), 250);
    }
  });

  /* 点别处就把选人下拉收起来。
     ！必须做这件事：那个列表是浮层，不收的话它会一直悬在下面的内容上。
       实测踩过一次：转发弹窗里选完人，列表正好压在「转发」按钮上面，
       点下去变成了又选中一个人，而且从界面上完全看不出来。
       （弹窗里另外还改成了正常流，见 app.css 的 .credit-add--inline ——
        两道一起上，因为「盖住按钮」和「一直不收」是两个毛病。）

     ！这里要判 contains：点击落在搜索框那一整块里面时不能收，
       否则会出现「刚要选那个人，列表先没了」。 */
  document.addEventListener("click", (event) => {
    for (const box of $$(".credit-results")) {
      if (box.classList.contains("hidden")) continue;
      const wrap = box.closest(".credit-add");
      if (!wrap || wrap.contains(event.target)) continue;
      clearPeopleResults(box.id === "share-results" ? "share" : "credit");
    }
  });

  document.addEventListener("click", async (event) => {
    const target = event.target;

    // 帖子 / 消息那一大摊子先过一遍，见 handleSocialClick
    if (await handleSocialClick(target)) return;

    const uploadSkinButton = target.closest("[data-action='upload-skin']");
    if (uploadSkinButton) {
      await uploadSkin();
      return;
    }

    const uploadAvatarButton = target.closest("[data-action='upload-avatar']");
    if (uploadAvatarButton) {
      await uploadAvatar();
      return;
    }

    const removeSkinButton = target.closest("[data-action='remove-skin']");
    if (removeSkinButton) {
      await removeMedia("skin");
      return;
    }

    const removeAvatarButton = target.closest("[data-action='remove-avatar']");
    if (removeAvatarButton) {
      await removeMedia("avatar");
      return;
    }

    const chip = target.closest("[data-chip]");
    if (chip) {
      toggleChip(chip);
      return;
    }

    const navButton = target.closest("[data-nav]");
    if (navButton) {
      await setView(navButton.dataset.nav);
      return;
    }

    const partnerTab = target.closest("[data-partner-tab]");
    if (partnerTab) {
      State.partnerTab = partnerTab.dataset.partnerTab;
      // 数据都在 State 里，直接重画 —— 不联网，所以是瞬时的。
      // ！paintPartnerBody 会顺手把 aria-selected 改完，
      //   一定要先调它，tabDir() 才能算出正确的方向。
      paintPartnerBody();
      const bar = partnerTab.closest(".tab-switch");
      const dir = tabDir(bar);
      tabBar(bar);
      tabIn($("#partner-body"), dir);
      return;
    }

    const loginMode = target.closest("[data-login-mode]");
    if (loginMode) {
      switchLoginMode(loginMode.dataset.loginMode);
      return;
    }

    const reasonOption = target.closest(".reason-option");
    if (reasonOption) {
      $$("#reason-picker .reason-option").forEach((button) => {
        button.setAttribute("aria-pressed", String(button === reasonOption));
      });
      return;
    }

    /* 「在这台设备上保持登录」（v0.30）。和上面两个不一样，这个要调接口：
       令牌存哪儿（localStorage / sessionStorage）是本地的事，
       但「它能活多久」在服务端 —— 只改本地的话，开关看起来开了，
       12 小时后照样被打回登录页。 */
    const keepSwitch = target.closest("[data-keep-login]");
    if (keepSwitch) {
      const next = keepSwitch.getAttribute("aria-checked") !== "true";
      keepSwitch.setAttribute("aria-checked", String(next));
      Store.setToken(Store.token, next);   // 本地那份也跟着搬家
      try {
        await Api.setSessionKeep(next);
        toast(next ? "已开启：下次打开直接进来" : "已关闭：下次要重新输密码", "ok");
      } catch (error) {
        // 服务端没改成，本地也就别改了 —— 两边必须一致，不然开关在骗人
        keepSwitch.setAttribute("aria-checked", String(!next));
        Store.setToken(Store.token, !next);
        toast(error.message, "bad");
      }
      return;
    }

    const privacySwitch = target.closest("[data-privacy]");
    if (privacySwitch) {
      await togglePrivacy(privacySwitch.dataset.privacy, privacySwitch);
      return;
    }

    const actionNode = target.closest("[data-action], [data-act]");
    if (!actionNode) return;

    const action = actionNode.dataset.action;
    const act = actionNode.dataset.act;
    const rid = actionNode.dataset.rid;
    // 按钮本身可能不带 data-uid，此时向上找最近的容器（如 .peer-card / .person-row）
    const ownerNode = actionNode.closest("[data-uid]");
    const uid = actionNode.dataset.uid || (ownerNode ? ownerNode.dataset.uid : "");

    // ！坑：「点遮罩空白处关闭」只能由遮罩本身、且落点在面板之外时触发。
    //   因为 .modal 是 .modal-backdrop 的子元素，而上面用的是 closest()，
    //   点在对话框内部任何没有 data-action 的地方（输入框、说明文字、标签、
    //   分隔线…）都会一路爬到遮罩上，被误判成「点了空白处」而把对话框关掉。
    //   「点留言输入框，申请窗口就消失」就是这个原因。
    //   教训：容器上挂 action 属性 + 用 closest() 解析，就会吞掉子元素上的点击。
    if (action === "close-modal" && actionNode.classList.contains("modal-backdrop") && target.closest(".modal")) {
      return;
    }

    try {
      switch (action || `@${act}`) {
        case "close-modal":
          closeModal();
          return;

        case "logout":
          try { await Api.logout(); } catch (error) { /* 令牌可能已失效 */ }
          // forgetDevice 而不是只清令牌：登出之后这台设备相当于「没登录过」，
          // 下次登录应该重新问一遍「要不要保持」。共用电脑上尤其重要 ——
          // 上一个人选的不等于下一个人选的。
          Store.forgetDevice();
          Store.user = null;
          State.overview = null;
          State.requestQueue = null;
          showAuth();
          return;

        case "reload-discover":
          // fresh：明确要求换一批，不要拿缓存里的推荐应付
          await setView("discover", { fresh: true });
          return;

        case "clear-search":
          clearSearch();
          focusSearch();
          return;

        case "captcha-refresh":
          await loadCaptcha(actionNode.dataset.purpose || "login");
          return;

        case "reg-next":
          goToNextStep();
          return;

        case "reg-back":
          setRegisterStep(State.registerStep - 1);
          return;

        case "forgot-password":
          openForgotPasswordModal();
          return;

        case "open-password-modal":
          openPasswordModal();
          return;

        case "submit-password":
          await submitPasswordChange();
          return;

        case "send-greet":
          await sendGreet(uid, actionNode);
          return;

        /* ---- 登录后的交换申请弹窗 ---- */
        case "later-request":
          State.requestQueue = null;
          State.requestIndex = 0;
          closeModal();
          await refreshOverview();
          return;

        case "accept-current": {
          const result = await Api.acceptRequest(Number(rid));
          toast(result.message, "ok");
          toastUnlocked(result.unlocked);
          await refreshOverview();
          State.requestIndex += 1;
          renderExchangeResult(result.peer, result.my_contact_ready);
          return;
        }

        case "reject-current":
          await Api.rejectRequest(Number(rid));
          toast("已婉拒，对方不会看到你的联系方式", "ok");
          State.requestIndex += 1;
          await refreshOverview();
          renderRequestDialog();
          return;

        case "next-request":
          closeModal();
          renderRequestDialog();
          if (!State.requestQueue) await setView(State.view);
          return;

        /* ---- 其他 ---- */
        case "open-feedback":
          openFeedbackModal();
          return;

        case "send-feedback":
          await sendFeedback();
          return;

        case "accept-from": {
          const result = await Api.acceptRequest(Number(rid));
          closeModal();
          toast(result.message, "ok");
          toastUnlocked(result.unlocked);
          await refreshOverview();
          renderExchangeResult(result.peer, result.my_contact_ready);
          return;
        }

        case "delete-account":
          if (!window.confirm("确定要注销并删除账号吗？所有数据都会被清除且无法恢复。")) return;
          await Api.deleteAccount();
          Store.forgetDevice();
          Store.user = null;
          State.overview = null;
          showAuth();
          toast("账号已删除，感谢曾经的陪伴", "ok");
          return;

        case "submit-report": {
          const picked = $("#reason-picker .reason-option[aria-pressed='true']");
          const errorBox = $("#report-error");
          if (!picked) {
            errorBox.textContent = "请先选择一个举报原因";
            errorBox.classList.remove("hidden");
            return;
          }
          const detail = $("#report-detail") ? $("#report-detail").value.trim() : "";
          const result = await Api.report({
            target_user: Number(uid),
            reason: picked.dataset.reason,
            detail,
          });
          closeModal();
          toast(result.message, "ok");
          toastUnlocked(result.unlocked);
          return;
        }

        case "@greet": {
          const targetId = Number(uid);
          openGreetModal(findUser(targetId) || {
            id: targetId, nickname: "这位玩家", avatar_hue: 190, edition_label: "", region: "",
          });
          return;
        }

        /* ---- 模组镜像 ---- */
        case "@mirror-open":
          await renderMirrorDetail(actionNode.dataset.uid);
          return;

        case "@mirror-back":
          await renderMirror();
          return;

        case "@mirror-download":
          await downloadMirrorFile(actionNode.dataset.uid, actionNode.dataset.mfName, actionNode);
          return;

        case "@mf-filter": {
          const kind = actionNode.dataset.mfKind;
          if (kind !== "loader" && kind !== "version") return;
          // 再点一次同一个就取消筛选，否则「全部」和「已选中」会分不清
          State.mirrorFilter[kind] = State.mirrorFilter[kind] === actionNode.dataset.mfValue
            ? ""
            : actionNode.dataset.mfValue;
          renderMirrorBody();
          return;
        }

        case "@profile":
          await openProfileModal(Number(uid));
          return;

        case "@report": {
          const targetId = Number(uid);
          const known = State.connections.find((item) => item.id === targetId);
          await openReportModal({ id: targetId, nickname: (known && known.nickname) || "这位玩家" });
          return;
        }

        case "@skip": {
          const targetId = Number(uid);
          await Api.skipUser(targetId);
          // 两个列表都要清，否则「换一批 / 重搜」后同一个人又冒出来
          State.discover = State.discover.filter((user) => user.id !== targetId);
          State.search.items = State.search.items.filter((user) => user.id !== targetId);
          const card = actionNode.closest(".peer-card");
          if (card) card.remove();
          return;
        }

        case "@copy":
          await copyText(actionNode.dataset.copy || "");
          return;

        case "@accept": {
          const result = await Api.acceptRequest(Number(rid));
          toast(result.message, "ok");
          toastUnlocked(result.unlocked);
          await refreshPartnerBody();
          await refreshOverview();
          renderExchangeResult(result.peer, result.my_contact_ready);
          return;
        }

        case "@reject":
          await Api.rejectRequest(Number(rid));
          toast("已婉拒，对方不会看到你的联系方式", "ok");
          await refreshPartnerBody();
          return;

        case "@unlink":
          if (!window.confirm("解除后对方的联系方式将不再可见，确定吗？")) return;
          await Api.removeConnection(Number(uid));
          toast("已解除交换", "ok");
          await refreshPartnerBody();
          await refreshOverview();
          return;

        case "@unblock":
          await Api.unblock(Number(uid));
          toast("已解除屏蔽", "ok");
          await setView("me");
          return;

        case "@block":
          if (!window.confirm("屏蔽后你们将不再互相看到，交换关系也会解除。确定吗？")) return;
          await Api.block(Number(uid));
          closeModal();
          toast("已屏蔽", "ok");
          await setView(State.view);
          return;

        default:
          // 排到这里说明按钮上的 data-action / data-act 与 case 名对不上。
          // 症状是「点了完全没反应」但控制台干干净净 —— 极难排查，所以主动喊一声。
          // 对照关系：data-action="x" → case "x"；data-act="x" → case "@x"。
          console.warn("[mcfr] 没有对应的处理分支：", action || `@${act}`, actionNode);
          return;
      }
    } catch (error) {
      toast(error.message, "bad");
    }
  });

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (form.id === "profile-form") {
      event.preventDefault();
      await saveProfile(form);
    }
  });

  /* 搜索框是切页时动态渲染出来的，所以走全局事件委托，
     不要给 input 单独绑 —— 每次重建都要重绑，迟早漏一个。
     250ms 防抖：中文输入法一个字一个字地敲，不防抖就是一个字一个请求。 */
  let searchTimer = null;

  document.addEventListener("input", (event) => {
    if (event.target.id !== "peer-search") return;
    window.clearTimeout(searchTimer);
    const value = event.target.value;
    searchTimer = window.setTimeout(() => runSearch(value), 250);
  });

  document.addEventListener("keydown", (event) => {
    // 搜索框里回车：跳过防抖，立刻搜
    if (event.key === "Enter" && event.target && event.target.id === "peer-search") {
      event.preventDefault();
      window.clearTimeout(searchTimer);
      runSearch(event.target.value);
      return;
    }

    // 卡片是 <article role="button">，得自己把键盘事件接上 ——
    // 只给 div 绑 click 的话，键盘用户永远打不开它。
    if ((event.key === "Enter" || event.key === " ")
        && event.target && event.target.matches("[data-act][role='button']")) {
      event.preventDefault();
      event.target.click();
      return;
    }

    // Ctrl+/ （Mac 上是 ⌘+/）：直接跳到搜索框。
    // ！要 preventDefault：某些浏览器会把它当成自己的快捷键。
    if ((event.ctrlKey || event.metaKey) && event.key === "/") {
      if (focusSearch()) {
        event.preventDefault();
        return;
      }
    }

    // 搜索框里按 Esc：清空搜索，而不是去关弹窗
    if (event.key === "Escape" && event.target && event.target.id === "peer-search") {
      clearSearch();
      event.target.blur();
      return;
    }

    if (event.key === "Escape" && $("#overlay-root").innerHTML) {
      // 「保持登录」正在问：Escape 不能关。
      // 关了的话那个 Promise 永远不会 resolve，登录请求会一直在那儿等着，
      // 表现就是「点了登录，什么都没发生」。
      if (keepModalOpen) return;
      closeModal();
    }
  });

  /* ============================================================
     十一·补 · 意见反馈
     ------------------------------------------------------------
     左下角那枚浮动按钮点开的表单。

     几个细节：
     · 未登录也能提交。登录不上来的人、还没注册的人，恰好是最需要反馈的；
       登录了就带上令牌，接口会把 user_id 一起记下来。
     · 分类用 chip，走的是全局那一套事件委托（见上面的 .chip 处理），
       所以这里**不要**再给它单独绑 click —— 两处都切换等于切了两次。
     · 提交按钮要自己禁掉并改字，否则网速慢的时候用户会连点，
       一下提交好几条（后端虽然有限流，但体验上不该让人有机会踩到）。
     ============================================================ */

  const FEEDBACK_KINDS = [
    ["suggestion", "功能建议"],
    ["bug", "问题反馈"],
    ["content", "内容举报"],
    ["safety", "安全 / 隐私"],
    ["other", "其它"],
  ];

  function openFeedbackModal() {
    const chips = FEEDBACK_KINDS.map(([value, label], index) => `
      <button type="button" class="chip" data-fb-kind="${value}"
              aria-pressed="${index === 0 ? "true" : "false"}">${esc(label)}</button>`).join("");

    openModal(`
      <span class="eyebrow eyebrow--iris">Feedback</span>
      <h2 class="display display-md" style="margin-top:.45rem">说点什么</h2>
      <p class="hint" style="margin-top:.5rem">
        遇到问题、想要新功能、或者觉得哪里不太对劲，都可以写在这里。
        ${Store.user ? "我们知道是你提的，处理完可以回访你。" : "你还没登录，我们会收到内容，但联系不上你 —— 想收到回复的话留个联系方式吧。"}
      </p>

      <div class="iris-rule iris-rule--fade" style="margin:1.1rem 0"></div>

      <div class="stack">
        <div class="field">
          <span>这是哪一类</span>
          <div class="chip-row" id="fb-kinds">${chips}</div>
        </div>

        <label class="field">
          <span>具体内容</span>
          <textarea class="textarea" id="fb-content" maxlength="1000"
                    placeholder="例如：注册时验证码一直收不到，我用的是 QQ 邮箱"></textarea>
        </label>

        <label class="field">
          <span>联系方式（可以不填）</span>
          <input class="input" id="fb-contact" maxlength="80" autocomplete="off"
                 placeholder="想让我们回复的话，留个邮箱或 QQ">
        </label>

        <p class="error-text hidden" id="fb-error"></p>

        <div class="row-between wrap">
          <p class="hint">请不要在反馈里写密码。</p>
          <div class="row">
            <button class="btn btn-ghost" type="button" data-action="close-modal">取消</button>
            <button class="btn btn-primary" type="button" data-action="send-feedback">提交反馈</button>
          </div>
        </div>
      </div>`);

    const box = $("#fb-content");
    if (box) box.focus();
  }

  async function sendFeedback() {
    const errorBox = $("#fb-error");
    const contentBox = $("#fb-content");
    const contactBox = $("#fb-contact");
    const picked = $("#fb-kinds .chip[aria-pressed='true']");
    const button = $("[data-action='send-feedback']");

    errorBox.classList.add("hidden");

    const content = (contentBox.value || "").trim();
    if (content.length < 5) {
      errorBox.textContent = "至少写 5 个字吧，说清楚一点我们才好处理。";
      errorBox.classList.remove("hidden");
      contentBox.focus();
      return;
    }

    const label = button.textContent;
    button.disabled = true;
    button.textContent = "发送中…";

    try {
      const result = await Api.sendFeedback({
        kind: picked ? picked.dataset.fbKind : "suggestion",
        content,
        contact: (contactBox.value || "").trim(),
        page: location.pathname + location.search,
      });
      closeModal();
      toast(result.message || "已经收到啦，谢谢！", "ok");
    } catch (error) {
      errorBox.textContent = error.message;
      errorBox.classList.remove("hidden");
    } finally {
      // 弹窗可能已经被关掉了，取不到按钮就跳过
      const still = $("[data-action='send-feedback']");
      if (still) {
        still.disabled = false;
        still.textContent = label;
      }
    }
  }

  /* ============================================================
     十四、登录 / 注册表单
     ============================================================ */

  function switchAuthTab(mode) {
    const isLogin = mode === "login";
    $("#tab-login").setAttribute("aria-selected", String(isLogin));
    $("#tab-register").setAttribute("aria-selected", String(!isLogin));
    $("#form-login").classList.toggle("hidden", !isLogin);
    $("#form-register").classList.toggle("hidden", isLogin);
    // 这里不单独放分步向导的动画：整个表单已经在滑入了，再滑一次会重
    if (!isLogin) setRegisterStep(1, false);
    tabSwap($("#auth-tabs"), isLogin ? $("#form-login") : $("#form-register"));
    // 切到哪一边就保证那一边有图。已经领过就不重复领 ——
    // 每切一次都重领的话，用户填到一半切回去看看，图就白换了。
    if (!(captchas[mode] || {}).id) loadCaptcha(mode);
  }

  /* ---- 注册向导：第 1 步账号信息 → 第 2 步个人资料 ---- */

  function setRegisterStep(step, animate = true) {
    const previous = State.registerStep;
    State.registerStep = step;

    $$("#form-register .step-pane").forEach((pane) => {
      pane.classList.toggle("hidden", Number(pane.dataset.step) !== step);
    });

    $$("#form-register [data-step-dot]").forEach((dot) => {
      const index = Number(dot.dataset.stepDot);
      dot.setAttribute("aria-current", String(index === step));
      dot.classList.toggle("step--done", index < step);
    });

    const last = 2;
    $("#form-register [data-action='reg-back']").classList.toggle("hidden", step === 1);
    $("#form-register [data-action='reg-next']").classList.toggle("hidden", step === last);
    $("#reg-submit").classList.toggle("hidden", step !== last);
    $("#register-error").classList.add("hidden");

    // 往前走就从右边进、往回退就从左边进
    if (animate && previous !== step) {
      tabIn($(`#form-register .step-pane[data-step='${step}']`), step > previous ? 1 : -1);
    }
  }

  function goToNextStep() {
    // 当前这一步的必填项不通过就不放行，避免用户到后面才发现要回头改。
    // ！只校验【当前这一页】：隐藏的那几个 pane 里也有 required 的输入框，
    //   一起校验的话用户会被一个看不见的字段拦住。
    const pane = $(`#form-register .step-pane[data-step='${State.registerStep}']`);
    if (pane) {
      for (const field of $$("input, select, textarea", pane)) {
        if (!field.checkValidity()) {
          field.reportValidity();
          return;
        }
      }
    }
    setRegisterStep(State.registerStep + 1);
  }

  /* ---- 人机验证（图片验证码） ----
     ------------------------------------------------------------
     id 和答案必须成对提交：id 指向服务端那条凭据，答案是在那条凭据上校验的。

     ！两个坑写在这里，改的时候别踩：
       ① id 存在这个模块变量里，**不放进 DOM**。塞进 data 属性的话，
          拿浏览器开发者工具改一下就能用 A 图的 id 去配 B 图的答案。
       ② 提交失败后必须换一张。服务端每错一次就给那张图记一笔，
          错满 5 次直接作废 —— 不换的话用户会卡在同一张已经死掉的图上，
          怎么输都错，而提示只会说「字符不正确」。 */
  const captchas = { login: null, register: null };

  function captchaImage(purpose) {
    return $(`#captcha-${purpose}`);
  }

  function captchaAnswerInput(purpose) {
    const image = captchaImage(purpose);
    const form = image ? image.closest("form") : null;
    return form ? $("input[name='captcha_answer']", form) : null;
  }

  /* 拼出登录 / 注册要带的那两个字段 */
  function captchaFields(purpose) {
    const input = captchaAnswerInput(purpose);
    return {
      captcha_id: (captchas[purpose] || {}).id || "",
      captcha_answer: input ? input.value.trim() : "",
    };
  }

  async function loadCaptcha(purpose) {
    const image = captchaImage(purpose);
    if (!image) return;
    image.classList.add("is-loading");
    try {
      const result = await Api.captcha(purpose);
      captchas[purpose] = result;
      image.src = result.image;
    } catch (error) {
      captchas[purpose] = null;
      image.removeAttribute("src");
      toast(error.message, "bad");
    } finally {
      image.classList.remove("is-loading");
    }
  }

  async function resetCaptcha(purpose) {
    const input = captchaAnswerInput(purpose);
    if (input) input.value = "";
    await loadCaptcha(purpose);
  }

  /* ---- 忘记密码 ----
     ------------------------------------------------------------
     v0.21 取消了邮件，自助找回这条路也一并没了。

     ！不要为了「看起来完整」而编一个假的表单出来 ——
       一个填完什么都不会发生的按钮，比没有按钮更让人恼火。
       这里只把「该找谁、怎么找」说清楚，剩下的走真实的反馈渠道。 */
  function openForgotPasswordModal() {
    openModal(`
      <span class="eyebrow eyebrow--iris">Password help</span>
      <h2 class="display display-md" style="margin:.35rem 0 0">忘记密码了？</h2>
      <p class="hint" style="margin-top:.5rem">
        本站没有邮箱找回流程。这是刻意的：少一条能自动联系到你的渠道，
        就少一份被陌生人盯上的可能。
      </p>

      <div class="iris-rule iris-rule--fade" style="margin:1.1rem 0"></div>

      <p style="margin:0 0 .5rem"><b>找管理员重置就好：</b></p>
      <ol class="hint" style="margin:0;padding-left:1.2rem;line-height:2">
        <li>去「意见反馈」留言，写清你的<b>用户名</b>（不要写密码）</li>
        <li>管理员会生成一个临时密码告诉你</li>
        <li>用临时密码登录后，去「我的」页面把它改成自己的</li>
      </ol>

      <p class="hint" style="margin-top:.9rem">
        为了确认是你本人，管理员可能会问你几个只有你知道的问题，
        比如最近一起联机过的朋友叫什么。
      </p>

      <div class="row" style="justify-content:flex-end;gap:.5rem;margin-top:1.2rem">
        <button class="btn btn-ghost" data-action="close-modal">知道了</button>
        <button class="btn btn-primary" data-action="open-feedback">去意见反馈留言</button>
      </div>`);
  }

  /* ---- 修改密码 ----
     ------------------------------------------------------------
     v0.21 才补上的：原来改密码只能走「邮箱验证码重置」。邮件一取消，
     用户就彻底没有改密码的途径了，而管理员重置出来的临时密码如果不改，
     就等于永久密码。 */
  function openPasswordModal() {
    openModal(`
      <span class="eyebrow eyebrow--iris">Change password</span>
      <h2 class="display display-md" style="margin:.35rem 0 0">修改密码</h2>
      <p class="hint" style="margin-top:.5rem">
        改完之后所有设备（包括现在这台）都会退出登录，需要用新密码重新进一次。
      </p>

      <div class="iris-rule iris-rule--fade" style="margin:1.1rem 0"></div>

      <label class="field">
        <span>当前密码</span>
        <input class="input" id="pw-old" type="password" autocomplete="current-password"
               placeholder="现在正在用的密码">
      </label>

      <label class="field" style="margin-top:.9rem">
        <span>新密码</span>
        <input class="input" id="pw-new" type="password" autocomplete="new-password"
               placeholder="6 位以上，不要使用真实生日">
      </label>

      <label class="field" style="margin-top:.9rem">
        <span>再输一遍新密码</span>
        <input class="input" id="pw-repeat" type="password" autocomplete="new-password"
               placeholder="两遍要一样">
      </label>

      <p class="error-text hidden" id="pw-error" style="margin-top:.7rem"></p>

      <div class="row" style="justify-content:flex-end;gap:.5rem;margin-top:1.2rem">
        <button class="btn btn-ghost" data-action="close-modal">取消</button>
        <button class="btn btn-primary" data-action="submit-password">确认修改</button>
      </div>`);
  }

  async function submitPasswordChange() {
    const errorBox = $("#pw-error");
    const oldPassword = $("#pw-old").value;
    const newPassword = $("#pw-new").value;
    const repeat = $("#pw-repeat").value;

    // 两遍不一致就在本地拦下来，不用白跑一趟服务端（也省得把旧密码白交上去）
    const complain = (message) => {
      errorBox.textContent = message;
      errorBox.classList.remove("hidden");
    };
    errorBox.classList.add("hidden");
    if (!oldPassword) return complain("请先填写当前密码");
    if (newPassword.length < 6) return complain("新密码至少要 6 位");
    if (newPassword !== repeat) return complain("两遍新密码不一致");
    if (newPassword === oldPassword) return complain("新密码不能和当前密码一样");

    try {
      const result = await Api.changePassword(oldPassword, newPassword);
      closeModal();
      toast(result.message, "ok");
      // 服务端把会话全清了，本地也要跟着回到登录页，否则界面看起来还登着，
      // 实际每个请求都是 401。
      Store.setToken("");
      Store.user = null;
      State.overview = null;
      showAuth();
    } catch (error) {
      complain(error.message);
    }
  }

  /* ---- 登录成功后的公共收尾 ---- */

  async function completeSignIn(result, message, keep = false) {
    /* 令牌存哪儿、服务端让它活多久，用的是**同一个答案**：
         keep=true  → localStorage（关掉浏览器还在） + 14 天
         keep=false → sessionStorage（关掉就没了）     + 12 小时
       ！两边都要真的生效：只改前面那半，令牌在服务端还是活的，
         在共用电脑上等于没选。 */
    Store.setToken(result.token, keep);
    pendingKeep = null;
    Store.user = result.user;
    await refreshOverview();
    showApp();
    // 直接打开某个功能页时（比如收藏了 /partners），登录后停在那一页
    await setViewFromLocation();
    toast(message, "ok");
    toastUnlocked(result.unlocked);
    // 登录后检查是否有待处理的联系方式交换申请
    checkPendingRequests();
  }

  function bindAuthForms() {
    $("#tab-login").addEventListener("click", () => switchAuthTab("login"));
    $("#tab-register").addEventListener("click", () => switchAuthTab("register"));

    // 点图片就换一张。键盘也要能用（role/tabindex 是写在 HTML 上的）
    $$(".captcha-img").forEach((image) => {
      const refresh = () => loadCaptcha(image.dataset.purpose);
      image.addEventListener("click", refresh);
      image.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          refresh();
        }
      });
    });

    $("#form-login").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.target;
      const errorBox = $("#login-error");
      errorBox.classList.add("hidden");

      const captcha = captchaFields("login");
      if (!captcha.captcha_id) {
        errorBox.textContent = "人机验证图还没加载出来，稍等一下或点一下图片";
        errorBox.classList.remove("hidden");
        return;
      }

      try {
        // 先问「下次还要不要输密码」，再发登录请求 ——
        // 服务端要拿这个答案决定令牌活 14 天还是 12 小时，问晚了没用。
        const keep = await resolveKeepChoice();
        if (keep === null) return;   // 用户在弹窗上点了背景，放弃这次登录
        const result = await Api.login(
          form.account.value.trim(),
          form.password.value,
          captcha,
          keep,
        );
        await completeSignIn(result, `欢迎回来，${result.user.nickname}`, keep);
      } catch (error) {
        errorBox.textContent = error.message;
        errorBox.classList.remove("hidden");
        await resetCaptcha("login");
      }
    });

    $("#form-register").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.target;
      const errorBox = $("#register-error");
      errorBox.classList.add("hidden");

      // 还没到最后一步就直接回车时，先把用户推到下一步，而不是报一堆校验错误
      if ($("#reg-submit").classList.contains("hidden")) {
        goToNextStep();
        return;
      }

      const payload = {
        username: form.username.value.trim(),
        nickname: form.nickname.value.trim(),
        password: form.password.value,
        email: form.email.value.trim(),
        edition: form.edition.value,
        age: Number(form.age.value),
        ...birthOf(form),
        region: form.region.value.trim(),
        bio: form.bio.value.trim(),
        play_styles: selectedChips("#reg-styles"),
        online_time: selectedChips("#reg-times"),
        looking_for: selectedChips("#reg-wants"),
        ...captchaFields("register"),
      };

      try {
        const keep = await resolveKeepChoice();
        if (keep === null) return;   // 用户在弹窗上点了背景，放弃这次注册
        const result = await Api.register({ ...payload, keep });
        await completeSignIn(result, "注册成功，欢迎加入 mcfr！", keep);
        await setView("me");
        toast("先去「联系方式」预置一下，别人申请时就能直接交换", "iris");
      } catch (error) {
        errorBox.textContent = error.message;
        errorBox.classList.remove("hidden");
        await resetCaptcha("register");
      }
    });
  }

  /* ============================================================
     十二·补 · 模组镜像
     ------------------------------------------------------------
     索引来自 Modrinth 官方 API，由服务端同步（见 backend/mirror.py）。
     用户这边只做两件事：看有哪些模组、下载**已经镜像到本站**的文件。

     ！为什么不把 Modrinth 的下载链接直接给用户：
       那等于让本站变成一个「帮你绕过网络限制」的跳板，而且那些链接指向
       对方的 CDN、随时会变。没镜像的版本就老实说没有，另给一个去
       Modrinth 的入口。这也正是这个页签存在的理由。
     ============================================================ */

  const MIRROR_PREVIEW = 60;           // 版本列表一次最多渲染多少条
  const MIRROR_FILTER_VERSIONS = 24;   // 游戏版本筛选器最多列几个

  function humanSize(bytes) {
    let value = Number(bytes) || 0;
    const units = ["B", "KB", "MB", "GB"];
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return `${index === 0 ? Math.round(value) : value.toFixed(1)} ${units[index]}`;
  }

  function compactNumber(value) {
    const number = Number(value) || 0;
    if (number >= 100000000) return `${(number / 100000000).toFixed(1)} 亿`;
    if (number >= 10000) return `${(number / 10000).toFixed(1)} 万`;
    return String(number);
  }

  /* 图标全部走本站缓存（后端同步时抓的），确实没有时用首字色块占位。
     不用外链图：CSP 是 img-src 'self'，外链会被拦；而且每次开页面
     都把用户 IP 交给 Modrinth 的 CDN，恰好违背「镜像」的意义。 */
  function mirrorIcon(mod, extra = "") {
    if (mod.icon) {
      return `<img class="mod-icon ${extra}" src="${esc(mod.icon)}" alt="" loading="lazy"/>`;
    }
    const letter = (mod.title || "?").trim().charAt(0) || "?";
    return `<span class="mod-icon mod-icon--blank ${extra}" aria-hidden="true">${esc(letter)}</span>`;
  }

  function mirrorCard(mod) {
    const loaders = (mod.loaders || [])
      .map((name) => `<span class="tag">${esc(name)}</span>`).join("");
    return `
      <article class="mod-card edge" data-act="mirror-open" data-uid="${esc(mod.id)}"
               tabindex="0" role="button" aria-label="查看 ${esc(mod.title)} 的版本">
        <div class="mod-top">
          ${mirrorIcon(mod)}
          <div class="mod-id">
            <b>${esc(mod.title)}</b>
            <span class="handle">${esc(mod.author || "未知作者")} · 下载 ${compactNumber(mod.downloads)}</span>
          </div>
        </div>
        <p class="mod-desc">${esc(mod.description || "这个模组没有写简介。")}</p>
        <div class="chip-row">
          ${loaders}
          <span class="tag">${(mod.game_versions || []).length} 个版本</span>
        </div>
        <div class="mod-foot">
          ${mod.local > 0
            ? `<span class="tag tag--iris">已镜像 ${mod.local} 个文件</span>`
            : `<span class="tag">本站未镜像</span>`}
          <span class="spacer"></span>
          <span class="hint">${mod.local > 0 ? humanSize(mod.bytes) : ""}</span>
        </div>
      </article>`;
  }

  async function renderMirror() {
    const view = $("#view");
    view.innerHTML = `<div class="load-hint">${cubeLoader()}</div>`;

    const [overview, list] = await Promise.all([Api.mirrorOverview(), Api.mirrorMods()]);
    State.mirror = list.items;

    const head = `
      <div class="view-head">
        <div class="titles">
          <span class="eyebrow eyebrow--iris">Mirror</span>
          <h1 class="display display-lg">模组<span class="iris-text">镜像</span></h1>
          <p class="lede">取自 Modrinth 下载量最高的常用模组。已经镜像到本站的版本可以直接下载；没镜像的可以去 Modrinth 自取。</p>
        </div>
      </div>`;

    if (!list.synced) {
      view.innerHTML = head + `
        <div class="empty">
          <span class="glyph">⬡</span>
          <p>还没有同步过模组索引</p>
          <p class="hint">管理员在后台「模组镜像」页签点一次「同步索引」就会出现。</p>
        </div>`;
      return;
    }

    view.innerHTML = head + `
      <div class="mirror-stats">
        <div class="mirror-stat"><span class="num">${overview.mod_count}</span><span class="cap">个模组</span></div>
        <div class="mirror-stat"><span class="num">${overview.local_files}</span><span class="cap">个已镜像文件</span></div>
        <div class="mirror-stat"><span class="num">${humanSize(overview.local_bytes)}</span><span class="cap">占用空间</span></div>
      </div>

      <div class="mod-grid">${list.items.map(mirrorCard).join("")}</div>

      <p class="hint" style="margin-top:1.2rem">
        索引同步于 ${fmtRelative(overview.synced_at)} · 数据来源
        <a href="https://modrinth.com" target="_blank" rel="noopener noreferrer">Modrinth</a>
      </p>`;
  }

  function mirrorVersionRow(version) {
    const typeTag = version.version_type === "release"
      ? `<span class="tag tag--iris">release</span>`
      : `<span class="tag">${esc(version.version_type)}</span>`;
    const shown = version.game_versions.slice(0, 8);
    const more = version.game_versions.length - shown.length;

    return `
      <div class="mod-version">
        <div class="mod-version-main">
          <b>${esc(version.version_number)}</b>
          ${typeTag}
          ${version.loaders.map((name) => `<span class="tag">${esc(name)}</span>`).join("")}
        </div>
        <div class="mod-version-gv">
          ${shown.map((gv) => `<span class="tag tag--ghost">${esc(gv)}</span>`).join("")}
          ${more > 0 ? `<span class="hint">…还有 ${more} 个</span>` : ""}
        </div>
        <div class="mod-version-side">
          <span class="hint">${esc(version.file_size_text || "")}</span>
          ${version.available
            ? `<button class="btn btn-primary btn-sm" data-act="mirror-download"
                       data-uid="${esc(version.id)}" data-mf-name="${esc(version.file_name || "mod.jar")}">下载</button>`
            : `<span class="hint">未镜像</span>`}
        </div>
      </div>`;
  }

  /* 筛选后的列表。只重画这一块，不重新拉接口 —— 切个加载器等一整页加载太慢了。 */
  function renderMirrorBody() {
    const body = $("#mirror-body");
    const mod = State.mirrorCurrent;
    if (!body || !mod) return;

    const filter = State.mirrorFilter;
    const match = (version) => (
      (!filter.loader || version.loaders.includes(filter.loader))
      && (!filter.version || version.game_versions.includes(filter.version))
    );

    const all = (mod.versions || []).filter(match);
    const ready = all.filter((v) => v.available);
    const rest = all.filter((v) => !v.available);

    const chip = (kind, value, label) => `
      <button type="button" class="filter-chip" data-act="mf-filter"
              data-mf-kind="${kind}" data-mf-value="${esc(value)}"
              aria-pressed="${filter[kind] === value}">${esc(label)}</button>`;

    body.innerHTML = `
      <div class="mirror-filters">
        <div class="filter-row">
          <span class="filter-label">加载器</span>
          ${chip("loader", "", "全部")}
          ${(mod.loaders || []).map((name) => chip("loader", name, name)).join("")}
        </div>
        <div class="filter-row">
          <span class="filter-label">游戏版本</span>
          ${chip("version", "", "全部")}
          ${(mod.game_versions || []).slice(0, MIRROR_FILTER_VERSIONS)
            .map((gv) => chip("version", gv, gv)).join("")}
        </div>
      </div>

      <h2 class="section-title"><span>本站已镜像</span><span class="rule"></span>
        <span class="hint">${ready.length} 个可直接下载</span></h2>
      ${ready.length
        ? `<div class="mod-versions">${ready.slice(0, MIRROR_PREVIEW).map(mirrorVersionRow).join("")}</div>`
        : `<div class="empty"><span class="glyph">⬡</span>
             <p>当前筛选下没有已镜像的版本</p>
             <p class="hint">换个加载器或游戏版本试试，或者去 Modrinth 自己下载。</p>
           </div>`}

      <h2 class="section-title"><span>全部版本</span><span class="rule"></span>
        <span class="hint">按发布时间倒序，共 ${rest.length} 个</span></h2>
      ${rest.length
        ? `<div class="mod-versions">${rest.slice(0, MIRROR_PREVIEW).map(mirrorVersionRow).join("")}</div>`
        : ""}
      ${rest.length > MIRROR_PREVIEW
        ? `<p class="hint">还有 ${rest.length - MIRROR_PREVIEW} 个版本没有列出，用上面的筛选缩小范围。</p>`
        : ""}`;
  }

  async function renderMirrorDetail(modId) {
    const view = $("#view");
    view.innerHTML = `<div class="load-hint">${cubeLoader()}</div>`;

    const mod = await Api.mirrorMod(modId);
    State.mirrorCurrent = mod;
    State.mirrorFilter = { loader: "", version: "" };

    view.innerHTML = `
      <div class="view-head">
        <div class="titles">
          <button class="btn btn-ghost btn-sm" data-act="mirror-back">← 返回列表</button>
          <div class="mod-detail-head">
            ${mirrorIcon(mod, "mod-icon--lg")}
            <div>
              <h1 class="display display-md">${esc(mod.title)}</h1>
              <p class="hint">${esc(mod.author || "未知作者")} · 下载 ${compactNumber(mod.downloads)}
                 · 共 ${(mod.game_versions || []).length} 个游戏版本</p>
            </div>
          </div>
          <p class="lede">${esc(mod.description || "")}</p>
        </div>
        <a class="btn btn-ghost" href="${esc(mod.mirror_url)}" target="_blank"
           rel="noopener noreferrer">在 Modrinth 上查看</a>
      </div>

      <div id="mirror-body"></div>`;

    renderMirrorBody();
    tabIn(view, 1);
  }

  /* 下载。</br>
     ！不能用 <a href>：那个不会带 Authorization 头，后端会回 401。
       后台的数据库导出用的是同一套办法（fetch + Blob + 临时 <a download>）。 */
  function dispositionName(header) {
    const utf8 = /filename\*=UTF-8''([^;]+)/i.exec(header || "");
    if (utf8) {
      try { return decodeURIComponent(utf8[1]); } catch (error) { /* 原样用下面的 */ }
    }
    const plain = /filename="?([^";]+)"?/i.exec(header || "");
    return plain ? plain[1] : "";
  }

  async function downloadMirrorFile(fileId, fallbackName, button) {
    const original = button ? button.textContent : "";
    if (button) {
      button.disabled = true;
      button.textContent = "下载中…";
    }
    try {
      const response = await fetch(
        `/api/mirror/files/${encodeURIComponent(fileId)}/download`,
        { headers: Store.token ? { Authorization: `Bearer ${Store.token}` } : {} },
      );

      if (!response.ok) {
        let detail = "下载失败，请稍后再试";
        if ((response.headers.get("content-type") || "").includes("application/json")) {
          const data = await response.json().catch(() => null);
          if (data && typeof data.detail === "string") detail = data.detail;
        }
        throw new Error(detail);
      }

      const name = dispositionName(response.headers.get("content-disposition"))
        || fallbackName || "mod.jar";
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = name;
      document.body.appendChild(link);
      link.click();
      link.remove();
      // ！不能立刻 revoke：部分浏览器还没开始读这个 blob 就把它撤销了，
      //   表现是「点了下载但什么也没发生」。
      window.setTimeout(() => URL.revokeObjectURL(url), 8000);
      toast(`已开始下载 ${name}`, "ok");
    } catch (error) {
      toast(error.message, "bad");
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = original;
      }
    }
  }

  /* ============================================================
     十五、启动
     ============================================================ */

  const RENDERERS = {
    discover: renderDiscover,
    circles: renderCircles,
    circle: renderCircle,
    posts: renderPosts,
    post: renderPost,
    editor: renderEditor,
    messages: renderMessages,
    mirror: renderMirror,
    partners: renderPartners,
    achievements: renderAchievements,
    me: renderMe,
  };

  async function boot() {
    onUnauthorized = () => showAuth();
    // 后端在 get_current_user 里统一拦「还没填年龄」的账号（428）。
    // 挂在 api 层，这样任何一个请求撞上它都会弹出补填框，
    // 不用在每个调用点各写一遍判断。
    onNeedsBirth = () => showBirthModal();
    bindAuthForms();

    // 视觉特效：点击涟漪 / 方块碎屑 + 背景离子流动 + 标签切换动画
    if (window.McfrEffects) window.McfrEffects.start();

    // 登录页两组标签栏先装上指示器；侧栏的要等 renderNav 之后才能量到尺寸
    tabBar($("#auth-tabs"));
    tabBar($("#login-mode"));

    try {
      Store.meta = await Api.meta();
      fillMetaOptions();
    } catch (error) {
      toast("后端服务未启动，请先运行 python run.py", "bad");
    }

    /* 侧栏那盏灯的第一次测量。
       ！90 秒一次就够：它要的是「有没有人在这儿」，不是监控面板。
         切到后台就停、切回来补一枪 —— 藏在后台的标签页不该一直在打接口。 */
    pingServer();
    let netTimer = window.setInterval(() => {
      if (!document.hidden) pingServer();
    }, 90 * 1000);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) pingServer();
    });
    window.addEventListener("beforeunload", () => window.clearInterval(netTimer));

    $("#btn-refresh").addEventListener("click", async () => {
      await refreshOverview();
      // fresh：用户主动点刷新，就该真的去拿新数据，不能只把缓存贴回来
      await setView(State.view, { fresh: true });
      toast("已刷新", "ok");
    });

    // 浏览器前进 / 后退：让地址栏和页面保持同步
    window.addEventListener("popstate", () => {
      if ($("#app-screen").classList.contains("hidden")) return;
      setViewFromLocation({ fromHistory: true });
    });

    if (Store.token) {
      const data = await refreshOverview();
      if (data) {
        showApp();
        await setViewFromLocation({ fromHistory: true });
        checkPendingRequests();
        return;
      }
      Store.setToken("");
    }

    showAuth();
  }

  /* 切语言时被 i18n.js 叫回来。
     ！必须重跑一遍渲染：DOM 通道只能翻已经写进页面的文字，
       而筛选项、下拉框、时间格式这些要重新算才算得对。
     重渲染是异步的，那也没关系 —— i18n.js 随后还会再扫一遍 DOM，
     两次之间最多闪一下，不会停在中英混杂。 */
  window.McfrRerender = async function () {
    fillMetaOptions();
    if (!Store.user) return;            // 还在登录页，没什么可重建的
    renderMeChip();
    renderNav();
    await setView(State.view, { fromHistory: true });
  };

  document.addEventListener("DOMContentLoaded", boot);
})();
