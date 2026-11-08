/* =========================================================================
   mcfr · 管理后台前端逻辑
   -------------------------------------------------------------------------
   访客打开 /admin 只能看到密码输入框；密码正确后才渲染任何数据。
   管理员令牌存在 sessionStorage，关闭标签页即失效。
   ========================================================================= */

(() => {
  "use strict";

  const TOKEN_KEY = "mcfr.admin.token";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  let token = sessionStorage.getItem(TOKEN_KEY) || "";

  const State = {
    view: "overview",
    overview: null,
    system: null,
    reportFilter: "open",
    userQuery: "",
    userStatus: "all",
    feedbackFilter: "all",
    // 帖子与评论：筛选条件（状态、搜索词）
    contentStatus: "",
    contentQuery: "",
    // 当前登录的管理员、他能看的页签、以及受控模块的说明
    me: null,
    tabs: [],
    areas: [],
    // v0.32：本场会话登录用的那把私钥。只活在内存里 —— 刷新页面就没了，
    // 别写进任何 storage。有了它，进后台之后要「证明身份」就不用重新选文件。
    signKey: null,
    // 登录页当前选中的方式（key / password）
    loginMode: "",
    // 模组镜像：后台状态与轮询句柄
    mirror: null,
    mirrorPoll: null,
    // v0.33：启动器页签的数据快照
    launcher: null,
  };

  const TAB_LABELS = {
    overview: "数据概览",
    reports: "举报处理",
    users: "用户管理",
    security: "安全记录",
    feedback: "意见反馈",
    content: "帖子与评论",
    mirror: "模组镜像",
    system: "系统升级",
    launcher: "启动器",
    adminkeys: "登录密钥",
    admins: "管理员",
  };

  /* ============================================================
     工具
     ============================================================ */

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[ch]);
  }

  const FACE = [
    [4, 4, 4, 4, 4],
    [1, 1, 1, 1, 1],
    [1, 2, 1, 2, 1],
    [1, 1, 1, 1, 1],
    [1, 1, 3, 1, 1],
  ];

  function avatar(hue, size) {
    const palette = {
      1: `hsl(${hue} 60% 62%)`,
      2: "hsl(240 26% 9%)",
      3: `hsl(${hue} 44% 30%)`,
      4: `hsl(${hue} 52% 44%)`,
    };
    const cells = FACE.flat().map((v) => `<i style="--c:${palette[v]}"></i>`).join("");
    const cls = size ? ` mcface--${size}` : "";
    return `<div class="mcface${cls}" style="--hue:${hue}" aria-hidden="true"><div class="mcface-grid">${cells}</div></div>`;
  }

  function toast(message, kind = "ok") {
    const host = $("#toast-host");
    const node = document.createElement("div");
    node.className = `toast toast--${kind}`;
    node.textContent = message;
    host.appendChild(node);
    window.setTimeout(() => {
      node.style.transition = "opacity .3s, transform .3s";
      node.style.opacity = "0";
      node.style.transform = "translateY(10px)";
      window.setTimeout(() => node.remove(), 340);
    }, 3200);
  }

  function fmtRelative(ts) {
    if (!ts) return "—";
    const diff = Math.floor(Date.now() / 1000) - Number(ts);
    if (diff < 60) return "刚刚";
    if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
    if (diff < 604800) return `${Math.floor(diff / 86400)} 天前`;
    const date = new Date(ts * 1000);
    return `${date.getMonth() + 1}月${date.getDate()}日`;
  }

  function skeleton(count, height) {
    return Array.from({ length: count }, () => `<div class="skeleton" style="height:${height}px"></div>`).join("");
  }

  /**
   * 当前账号有没有某个受控模块的权限。
   *
   * ！这只用来决定「按钮画不画」。真正拦人的是后端每个接口上的 require_area，
   *   前端藏起来纯粋是为了少让人踩一次 403。
   */
  function can(area, need = "view") {
    const me = State.me;
    if (!me) return false;
    if (me.is_super) return true;
    const level = (me.permissions || {})[area] || "none";
    return need === "manage" ? level === "manage" : level !== "none";
  }

  /* ============================================================
     接口
     ============================================================ */

  async function api(path, options = {}) {
    const { method = "GET", body } = options;
    const headers = {};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (token) headers["Authorization"] = `Bearer ${token}`;

    const response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });

    let data = null;
    try {
      data = await response.json();
    } catch (error) {
      data = null;
    }

    if (response.status === 401) {
      token = "";
      sessionStorage.removeItem(TOKEN_KEY);
      showGate();
      throw new Error((data && data.detail) || "管理员登录已过期");
    }
    if (!response.ok) {
      throw new Error((data && data.detail) || "操作失败，请稍后重试");
    }
    return data;
  }

  /* ============================================================
     模态框
     ------------------------------------------------------------
     后台这里只有几个很轻的对话框（改密、写回复、编辑管理员），
     所以不去引 app.js 那一套，自己写一份最小的。
     ============================================================ */

  function openModal(html) {
    $("#overlay-root").innerHTML = `
      <div class="modal-backdrop" data-admin-action="close-modal">
        <div class="modal" role="dialog" aria-modal="true">${html}</div>
      </div>`;
  }

  function closeModal() {
    $("#overlay-root").innerHTML = "";
  }

  /* ============================================================
     登录闸门
     ============================================================ */

  /* ---- 人机验证（后台登录那一关） ----
     ------------------------------------------------------------
     和前台（app.js）用的是同一个后端接口，只是用途标成 admin。
     ！用途不能串用：服务端会比对凭据上的 purpose，
       拿前台领的图来登后台会被直接挡下来。 */
  let captcha = null;

  async function loadCaptcha() {
    const image = $("#captcha-admin");
    if (!image) return;
    image.classList.add("is-loading");
    try {
      captcha = await api("/api/captcha?purpose=admin");
      image.src = captcha.image;
    } catch (error) {
      captcha = null;
      image.removeAttribute("src");
      toast(error.message, "bad");
    } finally {
      image.classList.remove("is-loading");
    }
  }

  /* 登录失败之后必须换一张：服务端每错一次就给那张图记一笔，
     错满 5 次直接作废 —— 不换的话会卡在同一张已经死掉的图上。 */
  async function resetCaptcha() {
    const input = $("#admin-login-form input[name='captcha_answer']");
    if (input) input.value = "";
    await loadCaptcha();
  }

  function showGate() {
    State.me = null;
    State.tabs = [];
    // 登录用的私钥只在内存里活着，退回闸门就该忘掉它
    State.signKey = null;
    $("#admin-app").classList.add("hidden");
    $("#admin-gate").classList.remove("hidden");
    // ！已经有选中的方式就别重置 —— 密钥登录失败时 api() 会调回这里，
    //   要是每次都退回默认值，用户会看到自己刚选的「用密钥文件」被换走。
    setLoginMode(State.loginMode || defaultLoginMode(), false);
    // 每次回到登录闸门都先领一张图，别让用户对着空白框开始输入
    loadCaptcha();
    const input = $("#admin-login-form input[name='username']");
    if (input) input.focus();
  }

  function showApp() {
    $("#admin-gate").classList.add("hidden");
    $("#admin-app").classList.remove("hidden");
  }

  /** 把 /api/admin/login 或 /api/admin/me 的返回装进 State，并刷新顶栏与页签。 */
  function applySession(payload) {
    State.me = payload.admin;
    State.tabs = payload.tabs || [];
    State.areas = payload.areas || [];
    renderWho();
    renderTabs();
  }

  function renderWho() {
    const me = State.me;
    const host = $("#admin-who");
    if (!host || !me) return;

    const role = me.is_super ? "超级管理员" : "普通管理员";
    const name = (me.display_name || "").trim();
    // ！超级管理员的默认显示名就叫「超级管理员」，直接画出来会变成
    //   「超级管理员 超级管理员」这种很傻的重复。撞名就只留角色那一块。
    const showName = name && name !== role && name !== me.username;

    host.innerHTML = `
      ${showName ? `<span class="admin-who__name">${esc(name)}</span>` : ""}
      <span class="admin-who__role${me.is_super ? " admin-who__role--super" : ""}">${role}</span>
      <span class="admin-who__id">@${esc(me.username)}</span>`;
  }

  /**
   * 按权限重建页签。
   * ！页签列表完全来自服务端（State.tabs），前端不自己判断权限 ——
   *   前端藏起来的按钮挡不住人，真正管用的是后端每个接口上的 require_area。
   */
  function renderTabs() {
    const host = $("#admin-tabs");
    if (!host) return;
    host.innerHTML = State.tabs.map((key) => `
      <button data-admin-tab="${esc(key)}" aria-selected="${key === State.view}">
        ${esc(TAB_LABELS[key] || key)}
      </button>`).join("");
    tabBar(host);
  }

  /* ============================================================
     v0.32 · 密钥对登录
     ------------------------------------------------------------
     私钥永远不离开用户自己的电脑：挑战由服务端发，签名用 WebCrypto 在本地算，
     发给服务器的只有 challenge + key_id + 签名。
     服务器只存公钥，所以就算数据库被整个拖走，也没人能拿它进后台。
     ============================================================ */

  // 跟后端 adminkeys.login_message() 必须一模一样。前缀不能省：
  // 少了域名隔离，同一把密钥在别处签的东西就有机会被拿到这里重放。
  const ADMIN_SIGN_PREFIX = "mcfr-admin-login:";
  const LOGIN_MODE_KEY = "mcfr.admin.loginmode";

  function bytesToHex(buffer) {
    return Array.from(new Uint8Array(buffer))
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");
  }

  /** base64url（JWK 用的编码）-> 十六进制。 */
  function base64UrlToHex(value) {
    const text = String(value).replace(/-/g, "+").replace(/_/g, "/");
    const padded = text + "=".repeat((4 - (text.length % 4)) % 4);
    let raw;
    try {
      raw = atob(padded);
    } catch (error) {
      throw new Error("密钥文件里的公钥读不出来");
    }
    let hex = "";
    for (let index = 0; index < raw.length; index += 1) {
      hex += raw.charCodeAt(index).toString(16).padStart(2, "0");
    }
    return hex;
  }

  /** PEM 文本 -> DER 字节。包头、包尾、换行都得去掉 —— atob 只认纯 base64。 */
  function pemToDer(text) {
    const body = String(text)
      .replace(/-----BEGIN [^-]+-----/g, "")
      .replace(/-----END [^-]+-----/g, "")
      .replace(/\s+/g, "");
    if (!body) throw new Error("这个文件是空的");
    let raw;
    try {
      raw = atob(body);
    } catch (error) {
      throw new Error("这不是密钥文件（里面不是普通的 PEM 文本）");
    }
    const bytes = new Uint8Array(raw.length);
    for (let index = 0; index < raw.length; index += 1) {
      bytes[index] = raw.charCodeAt(index);
    }
    return bytes;
  }

  /**
   * 把密钥文件读成 WebCrypto 私钥对象。
   *
   * ！extractable 传 true：这份私钥的字节本来就是用户刚刚亲手交给页面的，
   *   放开导出并不会多泄露任何东西；而导出 JWK 是拿到公钥坐标（算 key_id）
   *   最稳的办法 —— 比去解析文件名靠谱得多，用户随手改个名也不会废掉。
   */
  async function importKeyFile(file) {
    if (!window.crypto || !window.crypto.subtle) {
      throw new Error(
        "这个浏览器不支持本地签名，请换 Chrome / Edge / Firefox。"
        + "如果你是通过 http:// 打开的后台，请改用 https:// 访问。"
      );
    }
    const der = pemToDer(await file.text());
    try {
      return await crypto.subtle.importKey(
        "pkcs8", der, { name: "ECDSA", namedCurve: "P-256" }, true, ["sign"]
      );
    } catch (error) {
      throw new Error("这个文件不是本站生成的密钥（读不出 P-256 私钥）");
    }
  }

  /**
   * 算 key_id，必须跟后端 adminkeys.key_id() 一模一样：
   * SHA-256(130 个十六进制字符的公钥 0x04||X||Y) 取前 16 位。
   */
  async function keyIdOf(key) {
    const jwk = await crypto.subtle.exportKey("jwk", key);
    const point = "04" + base64UrlToHex(jwk.x) + base64UrlToHex(jwk.y);
    const digest = await crypto.subtle.digest(
      "SHA-256", new TextEncoder().encode(point)
    );
    return bytesToHex(digest).slice(0, 16);
  }

  /** 用一把已导入的私钥对挑战签名。登录和「证明身份」共用这一条。 */
  async function signWithKey(key, challenge) {
    const message = new TextEncoder().encode(ADMIN_SIGN_PREFIX + challenge);
    // WebCrypto 的 ECDSA 签名正好是裸的 r||s 共 64 字节，跟后端验签用的格式一致
    const signature = await crypto.subtle.sign(
      { name: "ECDSA", hash: "SHA-256" }, key, message
    );
    return bytesToHex(signature);
  }

  /* ---- 登录闸门上的两种方式 ---- */

  function defaultLoginMode() {
    try {
      return localStorage.getItem(LOGIN_MODE_KEY) === "key" ? "key" : "password";
    } catch (error) {
      return "password";
    }
  }

  /**
   * 切换登录方式。
   * ！remember 只在用户自己点切换时才传 true。首次进页面用的是「默认值」，
   *   那一下不能落盘 —— 否则一个还没做过密钥的人一进来，偏好就被写成密码了。
   */
  function setLoginMode(mode, remember = true) {
    const useKey = mode === "key";
    const keyForm = $("#admin-key-form");
    const passwordForm = $("#admin-login-form");
    if (!keyForm || !passwordForm) return;

    State.loginMode = useKey ? "key" : "password";
    keyForm.classList.toggle("hidden", !useKey);
    passwordForm.classList.toggle("hidden", useKey);
    $$("#admin-login-modes button").forEach((button) => {
      const active = button.dataset.loginMode === State.loginMode;
      button.setAttribute("aria-selected", String(active));
      button.classList.toggle("btn-primary", active);
      button.classList.toggle("btn-ghost", !active);
    });
    if (remember) {
      try {
        localStorage.setItem(LOGIN_MODE_KEY, State.loginMode);
      } catch (error) { /* 无痕模式写不进去，不影响用 */ }
    }
    if (!useKey) loadCaptcha();
  }

  /** 用密钥文件走完整登录：领挑战 -> 本地签名 -> 换成会话令牌。 */
  async function signInWithKeyFile(file) {
    const key = await importKeyFile(file);
    const keyId = await keyIdOf(key);

    const start = await api("/api/admin/keys/challenge", { method: "POST" });
    const signature = await signWithKey(key, start.challenge);
    const result = await api("/api/admin/keys/login", {
      method: "POST",
      body: { challenge: start.challenge, key_id: keyId, signature },
    });

    // 记下来，进后台之后再要「证明身份」（生成 / 删除密钥）就不用重新选文件了
    State.signKey = { id: keyId, key };
    return result;
  }

  /** 登录成功后的共同收尾 —— 密码和密钥两条路都走这里。 */
  async function enterAdmin(result) {
    token = result.token;
    sessionStorage.setItem(TOKEN_KEY, token);
    applySession(result);
    showApp();
    await setView(State.tabs[0] || "overview");
  }

  function bindLoginModes() {
    const host = $("#admin-login-modes");
    if (!host) return;
    host.addEventListener("click", (event) => {
      const button = event.target.closest("[data-login-mode]");
      if (button) setLoginMode(button.dataset.loginMode);
    });
  }

  function bindKeyLogin() {
    const form = $("#admin-key-form");
    if (!form) return;

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const errorBox = $("#admin-key-error");
      const button = $("#admin-key-submit");
      errorBox.classList.add("hidden");

      const file = (form.key_file.files || [])[0];
      if (!file) {
        errorBox.textContent = "请先选中密钥文件";
        errorBox.classList.remove("hidden");
        return;
      }

      button.disabled = true;
      button.textContent = "正在验证…";
      try {
        await enterAdmin(await signInWithKeyFile(file));
        toast("已用密钥进入后台", "ok");
      } catch (error) {
        errorBox.textContent = error.message;
        errorBox.classList.remove("hidden");
      } finally {
        button.disabled = false;
        button.textContent = "用密钥进入后台";
      }
    });
  }

  function bindLogin() {
    // 点图片就换一张（键盘也要能用，role/tabindex 写在 HTML 上）
    const image = $("#captcha-admin");
    if (image) {
      image.addEventListener("click", loadCaptcha);
      image.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          loadCaptcha();
        }
      });
    }

    $("#admin-login-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.target;
      const errorBox = $("#admin-login-error");
      errorBox.classList.add("hidden");

      try {
        const result = await api("/api/admin/login", {
          method: "POST",
          body: {
            username: form.username.value.trim(),
            password: form.password.value,
            captcha_id: (captcha || {}).id || "",
            captcha_answer: form.captcha_answer.value.trim(),
          },
        });
        token = result.token;
        sessionStorage.setItem(TOKEN_KEY, token);
        form.password.value = "";
        applySession(result);
        showApp();
        await setView(State.tabs[0] || "overview");
        toast(`已进入后台（${result.admin.is_super ? "超级管理员" : "普通管理员"}）`, "ok");
      } catch (error) {
        errorBox.textContent = error.message;
        errorBox.classList.remove("hidden");
        await resetCaptcha();
      }
    });
  }

  /* ============================================================
     模组镜像
     ------------------------------------------------------------
     索引来自 Modrinth 官方 API。这里能做两件事：
       · 同步索引 —— 需要「模组：同步索引」权限
       · 决定镜像哪些 / 触发下载 —— 需要「模组：镜像文件」权限

     ！两个权限分开是有意的：同步只是拉一份索引（轻、可反复跑），
       下载会把文件写进服务器磁盘（重、占了就回不去）。
       按钮画不画只看 State.mirror.can；真正拦人的是后端每个接口上的
       require_area —— 前端藏按钮只是少让人踩一次 403。
     ============================================================ */

  function fmtSize(bytes) {
    let value = Number(bytes) || 0;
    const units = ["B", "KB", "MB", "GB"];
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return `${index === 0 ? Math.round(value) : value.toFixed(1)} ${units[index]}`;
  }

  function jobProgress(job) {
    if (!job.total) {
      return `<div class="job-bar"><div class="job-bar-head"><b>任务已开始</b>
        <span class="hint">${esc(job.detail || "正在准备…")}</span></div></div>`;
    }
    const percent = Math.min(100, Math.round((job.done / job.total) * 100));
    return `
      <div class="job-bar" role="status">
        <div class="job-bar-head">
          <b>${job.kind === "sync" ? "正在同步索引" : "正在下载文件"}</b>
          <span class="hint">${job.done} / ${job.total}${job.failed ? `，失败 ${job.failed}` : ""}</span>
        </div>
        <div class="job-bar-track"><span style="width:${percent}%"></span></div>
        ${job.detail ? `<p class="hint">${esc(job.detail)}</p>` : ""}
      </div>`;
  }

  function jobRow(job) {
    const label = job.kind === "sync" ? "同步索引" : "下载文件";
    const status = { running: "进行中", ok: "完成", failed: "失败" }[job.status] || job.status;
    const when = new Date((job.started_at || 0) * 1000).toLocaleString("zh-CN");
    // ！自动任务一定要有标记：晚上机器自己跑了几条之后，
    //   列表里凭空多出一堆「操作人：每日自动」看着很像被人登了号。
    const autoTag = job.auto ? `<span class="tag tag--iris">自动</span>` : "";
    return `
      <div class="mirror-row">
        <div class="mirror-row-main">
          <b>${label} · ${status} ${autoTag}</b>
          <span class="hint">${when}${job.operator ? ` · ${esc(job.operator)}` : ""}</span>
          ${job.detail ? `<span class="hint">${esc(job.detail)}</span>` : ""}
        </div>
        <div class="mirror-row-side">
          <span class="hint">${job.done}/${job.total}${job.failed ? `（失败 ${job.failed}）` : ""}</span>
        </div>
      </div>`;
  }

  /* 每日自动同步的设置面板。
     ！时间只到「小时」：管理员是学生和老师，不是运维，
       没必要逼他们理解 cron 表达式。 */
  function autoSyncPanel(auto, can, limits) {
    const locked = can.sync ? "" : 'disabled title="需要「模组：同步索引」权限"';
    const hours = Array.from({ length: 24 }, (_, hour) =>
      `<option value="${hour}" ${hour === auto.hour ? "selected" : ""}>`
      + `${String(hour).padStart(2, "0")}:00</option>`).join("");

    const last = auto.last_day
      ? `上次自动同步：${esc(auto.last_day)}`
      : "还没有自动同步过";

    return `
      <div class="mirror-auto">
        <div class="mirror-auto-head">
          <label class="check-line">
            <input type="checkbox" id="mirror-auto-enabled"
                   ${auto.enabled ? "checked" : ""} ${locked}>
            <b>每天自动同步一次</b>
          </label>
          <span class="hint">${last}</span>
        </div>
        <div class="mirror-auto-fields">
          <label class="field">
            <span>几点执行</span>
            <select class="select" id="mirror-auto-hour" ${locked}>${hours}</select>
          </label>
          <label class="field">
            <span>同步下载量前几名</span>
            <input class="input" id="mirror-auto-limit" type="number"
                   min="${limits.sync_limit_min}" max="${limits.sync_limit_max}"
                   value="${auto.limit}" ${locked}>
          </label>
          <button class="btn btn-primary" data-admin-action="mirror-save-auto" ${locked}>保存设置</button>
        </div>
        <p class="hint">
          只拉索引，不会自动下载文件 —— 下载要占磁盘，那个必须由人决定。
          服务器在设定时间点没开着也没关系，当天开机后会自动补上；
          但一天只自动跑一次，这次失败了不会自己重试，可以在上面的任务列表里看到原因。
        </p>
      </div>`;
  }

  function modRow(mod, can) {
    const locked = can.download ? "" : 'disabled title="需要「模组：镜像文件」权限"';
    return `
      <div class="mirror-row">
        <div class="mirror-row-main">
          <b>${esc(mod.title)}</b>
          <span class="hint">${esc(mod.author || "未知作者")} · 下载 ${Number(mod.downloads || 0).toLocaleString("zh-CN")}</span>
          <span class="hint">${(mod.loaders || []).map(esc).join(" / ") || "—"} · ${(mod.game_versions || []).length} 个游戏版本</span>
        </div>
        <div class="mirror-row-side">
          ${mod.mirrored
            ? `<span class="tag tag--iris">已选 ${mod.selected}</span>`
            : `<span class="tag">未镜像</span>`}
          ${mod.local ? `<span class="hint">已下 ${mod.local} 个（${fmtSize(mod.bytes)}）</span>` : ""}
          <button class="btn btn-ghost btn-sm" data-admin-action="mirror-toggle"
                  data-mid="${esc(mod.id)}" data-on="${mod.mirrored ? "1" : "0"}" ${locked}>
            ${mod.mirrored ? "停止镜像" : "开始镜像"}
          </button>
          <button class="btn btn-ghost btn-sm" data-admin-action="mirror-config"
                  data-mid="${esc(mod.id)}" ${locked}>范围</button>
        </div>
      </div>`;
  }

  function drawMirror() {
    const view = $("#admin-view");
    const state = State.mirror;
    if (!view || !state) return;

    const { overview, mods, jobs, can, auto, limits, dir } = state;
    const running = (jobs || []).find((job) => job.status === "running");
    const noSync = can.sync ? "" : 'disabled title="需要「模组：同步索引」权限"';
    const noDownload = can.download ? "" : 'disabled title="需要「模组：镜像文件」权限"';

    view.innerHTML = `
      <div class="stack">
        <div class="row" style="gap:.6rem;flex-wrap:wrap;align-items:center">
          <button class="btn btn-primary" data-admin-action="mirror-sync" ${noSync}>同步索引</button>
          <button class="btn" data-admin-action="mirror-download" ${noDownload}>
            下载待镜像文件${overview.pending ? `（${overview.pending}）` : ""}
          </button>
          <span class="spacer"></span>
          <span class="hint">数据来源 Modrinth · 单个模组最多选 ${limits.max_selected_per_mod} 个文件</span>
        </div>

        ${running ? jobProgress(running) : ""}

        ${autoSyncPanel(auto, can, limits)}

        <div class="mirror-stats">
          <div class="mirror-stat"><span class="num">${overview.mod_count}</span><span class="cap">个模组</span></div>
          <div class="mirror-stat"><span class="num">${overview.local_files}</span><span class="cap">已镜像文件</span></div>
          <div class="mirror-stat"><span class="num">${fmtSize(overview.local_bytes)}</span><span class="cap">占用空间</span></div>
          <div class="mirror-stat"><span class="num">${overview.pending}</span><span class="cap">待下载</span></div>
        </div>

        <h2 class="section-title"><span>模组（${mods.length}）</span><span class="rule"></span></h2>
        ${mods.length
          ? `<div class="mirror-list">${mods.map((mod) => modRow(mod, can)).join("")}</div>`
          : `<div class="empty"><span class="glyph">⬡</span><p>还没有索引数据</p>
               <p class="hint">点上面的「同步索引」从 Modrinth 拉一次。</p></div>`}

        <h2 class="section-title"><span>最近任务</span><span class="rule"></span></h2>
        ${jobs.length
          ? `<div class="mirror-list">${jobs.map(jobRow).join("")}</div>`
          : `<p class="hint">还没有跑过任务。</p>`}

        <p class="hint">文件目录：${esc(dir)}</p>
      </div>`;
  }

  async function refreshMirror() {
    State.mirror = await api("/api/admin/mirror/state");
    drawMirror();
  }

  async function renderMirrorAdmin() {
    await refreshMirror();
    startMirrorPoll();
  }

  function stopMirrorPoll() {
    if (State.mirrorPoll) {
      window.clearInterval(State.mirrorPoll);
      State.mirrorPoll = null;
    }
  }

  /* 任务在后台线程里跑，前端只能轮询。
     ！只在「确实有任务在跑」时开轮询，跑完就停 —— 否则会一直每 2 秒
       打一次后端，而这个页签可能被开着挂一整天。 */
  function startMirrorPoll() {
    stopMirrorPoll();
    State.mirrorPoll = window.setInterval(async () => {
      if (State.view !== "mirror") {
        stopMirrorPoll();
        return;
      }
      const running = (State.mirror?.jobs || []).find((job) => job.status === "running");
      if (!running) {
        stopMirrorPoll();
        return;
      }
      try {
        await refreshMirror();
      } catch (error) {
        stopMirrorPoll();
        toast(error.message, "bad");
      }
    }, 2000);
  }

  function openMirrorConfig(modId) {
    const mod = (State.mirror?.mods || []).find((item) => item.id === modId);
    if (!mod) return;

    // 加载器默认全选（对应「每个加载器各抓一个最新 release」）；
    // 游戏版本默认全不选（对应「不额外指定版本」）。
    const active = mod.mirror_loaders.length ? mod.mirror_loaders : mod.loaders;
    const loaderChips = (mod.loaders || []).map((name) => `
      <button type="button" class="chip" data-admin-action="mirror-pick"
              data-value="${esc(name)}" aria-pressed="${active.includes(name)}">${esc(name)}</button>`).join("");
    const versionChips = (mod.game_versions || []).map((gv) => `
      <button type="button" class="chip" data-admin-action="mirror-pick"
              data-value="${esc(gv)}" aria-pressed="${mod.mirror_versions.includes(gv)}">${esc(gv)}</button>`).join("");

    openModal(`
      <span class="eyebrow eyebrow--iris">镜像范围</span>
      <h2 class="display display-md">${esc(mod.title)}</h2>
      <p class="hint">游戏版本留空时，每个加载器只抓最新的一个 release；勾了版本，就按勾中的版本各抓一个。</p>

      <h3 style="margin-top:1rem">加载器</h3>
      <div class="chip-row" id="mirror-loaders">${loaderChips}</div>

      <h3 style="margin-top:1rem">游戏版本（可留空）</h3>
      <div class="chip-row chip-row--scroll" id="mirror-versions">${versionChips}</div>

      <div class="row" style="gap:.6rem;margin-top:1.2rem">
        <button class="btn btn-ghost" data-admin-action="close-modal">取消</button>
        <button class="btn btn-primary" data-admin-action="mirror-save" data-mid="${esc(mod.id)}">
          保存并重算
        </button>
      </div>`);
  }

  /* ============================================================
     帖子与评论
     ------------------------------------------------------------
     ！下架是【软删】，不是 DELETE。一篇帖子可能已经被别人点赞、
       评论、转发过，物理删掉会让那些人的通知点进去变成一片空白，
       而他们什么都没做错。下架之后对所有人表现为「这篇帖子不存在」，
       数据还在，误判了也能一键恢复。
     ============================================================ */

  const CONTENT_STATUS_LABELS = {
    published: "已发布", hidden: "已下架", deleted: "已删除",
  };

  /* 全文弹窗。
     ！管理员要判断「该不该下架」，就必须看得到原样的内容 ——
       列表里只有一段摘要，而恰恰是摘要看不到的那部分最可能出问题。
       图片和附件照样渲染出来：不然一张违规的图永远判不了。
     ！渲染交给 richtext.js，不要在这里另拼一份 —— 那边做了全量转义，
       自己再拼一遍就等于多了一处可能漏掉转义的地方。 */
  function openContentModal(post) {
    const rich = window.McfrRich;
    const body = rich
      ? rich.render(post.blocks)
      : `<p class="hint">（正文渲染器没加载出来，刷新一下页面）</p>`;

    openModal(`
      <span class="eyebrow eyebrow--iris">帖子全文</span>
      <h2 class="display display-md" style="margin-top:.4rem">${esc(post.title)}</h2>
      <p class="hint" style="margin-top:.4rem">
        ${esc(post.author.nickname)} · ${fmtRelative(post.created_at)}
        ${post.status !== "published" ? ` · 当前状态：${CONTENT_STATUS_LABELS[post.status] || post.status}` : ""}
      </p>
      <div class="iris-rule iris-rule--fade" style="margin:1rem 0"></div>
      <div class="rt-body" style="max-height:52vh;overflow-y:auto;padding-right:.3rem">${body}</div>
      <div class="row" style="gap:.6rem;margin-top:1.2rem;justify-content:flex-end">
        <button class="btn btn-ghost" data-admin-action="close-modal">关闭</button>
      </div>`);
  }

  function contentPostRow(item, canManage) {
    const locked = canManage ? "" : 'disabled title="需要「帖子与评论」的能改权限"';
    const hidden = item.status !== "published";
    return `
      <div class="admin-row">
        <div class="admin-row-main">
          <div class="title">
            <b>${esc(item.title)}</b>
            ${hidden ? `<span class="tag tag--warn">${CONTENT_STATUS_LABELS[item.status] || item.status}</span>` : ""}
          </div>
          <span class="meta">${esc(item.author.nickname)} · ${fmtRelative(item.created_at)}</span>
          <span class="meta">赞 ${item.like_count} · 评论 ${item.comment_count} · 转发 ${item.share_count} · 浏览 ${item.view_count}</span>
          ${item.excerpt ? `<div class="detail">${esc(item.excerpt)}</div>` : ""}
        </div>
        <div class="admin-row-actions">
          <button class="btn btn-ghost btn-sm"
                  data-admin-action="content-view" data-pid="${item.id}">看全文</button>
          ${hidden
            ? `<button class="btn btn-ghost btn-sm" data-admin-action="content-restore"
                       data-pid="${item.id}" ${locked}>恢复</button>`
            : `<button class="btn btn-danger btn-sm" data-admin-action="content-hide"
                       data-pid="${item.id}" ${locked}>下架</button>`}
        </div>
      </div>`;
  }

  function contentCommentRow(item, canManage) {
    const locked = canManage ? "" : 'disabled title="需要「帖子与评论」的能改权限"';
    const hidden = item.status !== "published";
    return `
      <div class="admin-row">
        <div class="admin-row-main">
          <div class="title">
            <b>${esc(item.user.nickname)}</b>
            <span class="meta">在《${esc(item.post_title)}》下</span>
            ${hidden ? `<span class="tag tag--warn">${CONTENT_STATUS_LABELS[item.status] || item.status}</span>` : ""}
          </div>
          <div class="detail">${esc(item.masked)}</div>
          <span class="meta">${fmtRelative(item.created_at)}</span>
        </div>
        <div class="admin-row-actions">
          <button class="btn btn-ghost btn-sm"
                  data-admin-action="content-view" data-pid="${item.post_id}">看全文</button>
          ${hidden
            ? `<button class="btn btn-ghost btn-sm" data-admin-action="comment-restore"
                       data-cid="${item.id}" ${locked}>恢复</button>`
            : `<button class="btn btn-danger btn-sm" data-admin-action="comment-hide"
                       data-cid="${item.id}" ${locked}>下架</button>`}
        </div>
      </div>`;
  }

  async function renderContent() {
    const params = new URLSearchParams({
      status: State.contentStatus || "",
      q: State.contentQuery || "",
    });
    const [data, comments] = await Promise.all([
      api(`/api/admin/content?${params}`),
      api(`/api/admin/content/comments?${params}`),
    ]);

    const canManage = data.can_manage;
    const counts = data.counts || {};
    const filters = [
      ["", "全部"],
      ["published", "已发布"],
      ["hidden", "已下架"],
      ["deleted", "已删除"],
    ];

    const posts = data.items.length
      ? data.items.map((item) => contentPostRow(item, canManage)).join("")
      : `<div class="empty"><span class="glyph">◈</span><p>这个条件下没有帖子</p></div>`;
    const commentRows = comments.items.length
      ? comments.items.map((item) => contentCommentRow(item, canManage)).join("")
      : `<div class="empty"><span class="glyph">◈</span><p>没有搜到评论</p></div>`;

    $("#admin-view").innerHTML = `
      <div class="admin-tabs" style="margin-bottom:1.1rem">
        ${filters.map(([key, label]) => `
          <button data-content-filter="${key}" aria-selected="${State.contentStatus === key}">
            ${label}${counts[key] ? ` · ${counts[key]}` : ""}
          </button>`).join("")}
      </div>

      ${canManage ? "" : `<div class="notice" style="margin-bottom:1.1rem">
         你自己只能<b>查看</b>。要能下架的话，请让超级管理员在「管理员」页里把
         你的「帖子与评论」权限调成「能改」。
      </div>`}

      <div class="row" style="gap:.6rem;margin-bottom:1.1rem;flex-wrap:wrap">
        <input class="input" id="content-search" style="flex:1 1 16rem"
               value="${esc(State.contentQuery)}"
               placeholder="搜帖子标题、正文、作者或评论内容">
        <button class="btn" data-admin-action="content-search-go">搜索</button>
        <button class="btn btn-ghost" data-admin-action="content-recount"
                ${canManage ? "" : 'disabled title="需要能改权限"'}>重算计数</button>
      </div>

      <div class="section-title">
        <h2>帖子</h2><span class="rule"></span>
        <span class="eyebrow">共 ${Number(data.total)} 篇</span>
      </div>
      <div class="panel panel--flush">${posts}</div>

      <div class="section-title" style="margin-top:1.6rem">
        <h2>最新评论</h2><span class="rule"></span>
        <span class="eyebrow">共 ${Number(comments.total)} 条</span>
      </div>
      <div class="panel panel--flush">${commentRows}</div>

      <p class="hint" style="margin-top:1.2rem">
        下架是<b>软删除</b>：帖子对所有人表现为「不存在」，但数据还在，误判了可以恢复。
        评论内容里的手机号已经遮掉了 —— 判断该不该下架不需要知道具体号码。
        被下架的帖子会给作者发一条站内通知。
      </p>`;

    tabBar($("#admin-view .admin-tabs"));
  }

  /* ============================================================
     视图切换
     ============================================================ */

  const RENDERERS = {
    overview: renderOverview,
    reports: renderReports,
    users: renderUsers,
    security: renderSecurityLogs,
    feedback: renderFeedback,
    content: renderContent,
    mirror: renderMirrorAdmin,
    launcher: renderLauncher,
    adminkeys: renderAdminkeys,
    admins: renderAdmins,
    system: renderSystem,
  };

  /* ---- 标签切换动画（实现都在 effects.js 里）---- */
  const FX = window.McfrEffects || {};
  const tabBar = (el) => FX.tabBar && FX.tabBar(el);
  const tabIn = (el, dir) => FX.tabIn && FX.tabIn(el, dir);
  const tabSwap = (bar, panel) => (FX.tabSwap ? FX.tabSwap(bar, panel) : 0);

  async function setView(name) {
    // ！必须卡一道：如果调用方传了一个当前账号无权访问的页签
    //   （历史遗留的 State.view、书签、或者手改的 URL），
    //   就退回到第一个可用页签，而不是打过去拿一个 403 白屏。
    if (!State.tabs.includes(name)) name = State.tabs[0] || "overview";

    // 离开镜像页就不用轮询了，否则会一直在后台打接口
    if (name !== "mirror") stopMirrorPoll();

    State.view = name;
    $$("#admin-tabs button").forEach((button) => {
      button.setAttribute("aria-selected", String(button.dataset.adminTab === name));
    });

    // 指示器先滑过去，内容等渲染完再滑进来
    const dir = tabSwap($("#admin-tabs"), null);

    const view = $("#admin-view");
    view.innerHTML = `<div class="stack">${skeleton(2, 110)}</div>`;

    try {
      await RENDERERS[name]();
      $("#admin-updated").textContent = `更新于 ${new Date().toLocaleTimeString("zh-CN")}`;
    } catch (error) {
      view.innerHTML = `<div class="empty"><span class="glyph">⚠</span><p>${esc(error.message)}</p></div>`;
    }
    tabIn(view, dir);
  }

  /* ============================================================
     一、数据概览
     ============================================================ */

  async function renderOverview() {
    const data = await api("/api/admin/overview");
    State.overview = data;

    const primary = [
      ["注册用户", data.users.total, ""],
      ["当前在线", data.users.online, ""],
      ["7 日新增", data.users.new_7d, ""],
      ["已暂停账号", data.users.suspended, data.users.suspended > 0 ? "stat-card--alert" : ""],
      ["已交换联系方式", data.social.connections, ""],
      ["待回应的申请", data.social.requests_pending, ""],
      ["未处理举报", data.safety.reports_open, data.safety.reports_open > 0 ? "stat-card--alert" : ""],
      ["24 小时人机验证", data.captcha.issued_24h, ""],
    ];

    const secondary = [
      ["活跃账号", data.users.active],
      ["预置了联系方式", data.social.with_contact],
      ["累计通过申请", data.social.requests_accepted],
      ["累计举报", data.safety.reports_total],
      ["屏蔽关系", data.safety.blocks],
    ];

    $("#admin-view").innerHTML = `
      <div class="stat-grid">
        ${primary.map(([label, value, cls]) => `
          <div class="stat-card ${cls}">
            <b>${Number(value)}</b>
            <span>${esc(label)}</span>
          </div>`).join("")}
      </div>

      <div class="section-title"><h2>其它指标</h2><span class="rule"></span></div>

      <div class="stat-grid">
        ${secondary.map(([label, value]) => `
          <div class="stat-card">
            <b>${Number(value)}</b>
            <span>${esc(label)}</span>
          </div>`).join("")}
      </div>

      <p class="hint" style="margin-top:1.4rem">
        提示：后台只展示运营所需的汇总数据与账号状态，不会显示用户的联系方式内容。
      </p>`;
  }

  /* ============================================================
     二、举报处理
     ============================================================ */

  function reportRow(item) {
    const open = item.status === "open";
    return `
      <div class="admin-row">
        ${avatar(item.reporter_hue, "sm")}
        <div class="body">
          <span class="title">
            <span>${esc(item.reporter_nickname)}</span>
            <span class="meta">举报</span>
            <span>${esc(item.target_nickname)}</span>
            <span class="reason-tag">${esc(item.reason)}</span>
          </span>
          <span class="meta">
            @${esc(item.reporter_username)} → @${esc(item.target_username)} · ${fmtRelative(item.created_at)}
            ${item.target_status !== "active" ? " · 该账号已被暂停" : ""}
          </span>
          ${item.detail ? `<div class="detail">${esc(item.detail)}</div>` : ""}
        </div>
        <div class="actions">
          ${open
            ? `<button class="btn btn-primary btn-sm" data-admin-action="resolve-report" data-rid="${item.id}">标记已处理</button>`
            : `<button class="btn btn-ghost btn-sm" data-admin-action="reopen-report" data-rid="${item.id}">重新打开</button>`}
          ${item.target_status === "active"
            ? `<button class="btn btn-danger btn-sm" data-admin-action="suspend-user"
                       data-uid="${item.target_id}" data-name="${esc(item.target_nickname)}">暂停 TA</button>`
            : ""}
        </div>
      </div>`;
  }

  async function renderReports() {
    const view = $("#admin-view");
    const filters = [["open", "待处理"], ["resolved", "已处理"], ["all", "全部"]];

    view.innerHTML = `
      <div class="admin-tabs" style="margin-bottom:1.1rem">
        ${filters.map(([key, label]) => `
          <button data-report-filter="${key}" aria-selected="${State.reportFilter === key}">${label}</button>`).join("")}
      </div>
      <div id="report-body" class="stack">${skeleton(3, 110)}</div>`;

    // 这组标签随页面重建，先把指示器装上（首次不滑，直接落在选中项上）
    tabBar($("#admin-view .admin-tabs"));

    await refreshReportBody();
  }

  /* 只重画列表，不重建标签栏 —— 这样切筛选时指示器能滑过去而不是跳过去 */
  async function refreshReportBody() {
    const data = await api(`/api/admin/reports?status=${State.reportFilter}`);
    const body = $("#report-body");
    if (!body) return;

    body.innerHTML = data.items.length
      ? `<div class="panel panel--flush">${data.items.map(reportRow).join("")}</div>`
      : `<div class="empty"><span class="glyph">✔</span><p>没有${State.reportFilter === "open" ? "待处理的" : ""}举报记录</p></div>`;
  }

  /* ============================================================
     三、用户管理
     ============================================================ */

  function userRow(user) {
    const active = user.status === "active";
    return `
      <div class="admin-row">
        ${avatar(user.avatar_hue, "sm")}
        <div class="body">
          <span class="title">
            <span>${esc(user.nickname)}</span>
            <span class="status-dot ${active ? "" : "status-dot--off"}">${active ? "正常" : "已暂停"}</span>
          </span>
          <span class="meta">@${esc(user.username)} · ${esc(user.email || "未绑定邮箱")} · Lv.${user.level}</span>
          <span class="meta">
            <!-- v0.29：年龄填过就不能改，用户填错了只能来找管理员。
                 ！所以要在这里把真实年龄和「有没有填过」都摆出来 ——
                   否则那句「填错只能找管理员」就成了一句空话：
                   管理员点进来根本看不到他填的是什么。 -->
            ${user.birth_declared
              ? `${esc(user.age_label)}（${user.adult ? "成年" : "未成年"}）`
              : `<span class="status-dot status-dot--off">未填写年龄</span>`} ·
            注册于 ${fmtRelative(user.created_at)} · 最后活跃 ${fmtRelative(user.last_seen)} ·
            ${user.contact_prepared ? "已预置联系方式" : "未预置联系方式"}
          </span>
        </div>
        <div class="actions">
          <!-- ！用户自己改不了年龄（注册和补填都是一次性的），填错只能由
               管理员改。所以这个按钮是注册页那句「填错只能找管理员」的兑现。
               只给超级管理员：改年龄等于改这个人在站里属于哪侧人群。 -->
          ${State.me && State.me.is_super ? `
            <button class="btn btn-ghost btn-sm" data-admin-action="fix-user-birth"
                    data-uid="${user.id}" data-name="${esc(user.nickname)}"
                    data-age="${user.age ?? ""}"
                    data-month="${user.birth_month ?? ""}"
                    data-day="${user.birth_day ?? ""}">修正年龄</button>` : ""}
          <button class="btn btn-ghost btn-sm" data-admin-action="reset-user-password"
                  data-uid="${user.id}" data-name="${esc(user.nickname)}">重置密码</button>
          ${active
            ? `<button class="btn btn-danger btn-sm" data-admin-action="suspend-user"
                       data-uid="${user.id}" data-name="${esc(user.nickname)}">暂停</button>`
            : `<button class="btn btn-primary btn-sm" data-admin-action="activate-user" data-uid="${user.id}">恢复</button>`}
        </div>
      </div>`;
  }

  async function renderUsers() {
    const view = $("#admin-view");
    view.innerHTML = `
      <div class="admin-search">
        <input class="input" id="user-query" placeholder="搜索用户名 / 昵称 / 邮箱"
               value="${esc(State.userQuery)}">
        <select class="select" id="user-status" style="width:auto">
          <option value="all" ${State.userStatus === "all" ? "selected" : ""}>全部状态</option>
          <option value="active" ${State.userStatus === "active" ? "selected" : ""}>正常</option>
          <option value="suspended" ${State.userStatus === "suspended" ? "selected" : ""}>已暂停</option>
        </select>
        <button class="btn btn-primary" data-admin-action="search-users">搜索</button>
      </div>
      <div id="user-body" class="stack">${skeleton(4, 92)}</div>`;

    const params = new URLSearchParams({
      q: State.userQuery,
      status: State.userStatus,
      limit: "60",
    });
    const data = await api(`/api/admin/users?${params.toString()}`);

    const body = $("#user-body");
    body.innerHTML = data.items.length
      ? `<div class="panel panel--flush">${data.items.map(userRow).join("")}</div>`
      : `<div class="empty"><span class="glyph">◉</span><p>没有符合条件的用户</p></div>`;
  }

  /* ---- 修正年龄（仅超管）------------------------------------------
     用户自己改不了 —— 注册和补填都是一次性的写操作（见 backend/age.py）。
     这个弹窗就是「填错只能找管理员」那句话的兑现。
     ！年龄只在后端折算成出生年，所以这里让他重新填一次「几岁 + 生日」，
       和注册时的口径完全一致，不另外搞一套。 */
  const FIX_MONTH_DAYS = [0, 31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

  function openBirthModal(node) {
    const name = node.dataset.name || "该用户";
    const age = node.dataset.age || "";
    const month = Number(node.dataset.month) || 1;
    const day = Number(node.dataset.day) || 1;
    const hasBirth = Boolean(node.dataset.month && node.dataset.day);

    openModal(`
      <span class="eyebrow eyebrow--iris">修正年龄</span>
      <h2 class="display display-md" style="margin:.35rem 0 0">${esc(name)}</h2>
      <p class="hint" style="margin-top:.5rem">
        ${hasBirth
          ? "这个人已经填过生日，这里是改正。用户自己改不了。"
          : "这个人还没填过生日。填完之后他依然改不了。"}
      </p>
      <div class="form-grid" style="margin-top:1rem">
        <label class="field">
          <span>年龄</span>
          <input class="input" type="number" id="fix-age" min="6" max="99" step="1"
                 value="${esc(String(age))}" placeholder="例如 13">
        </label>
        <label class="field">
          <span>生日（月 / 日）</span>
          <div class="birth-row">
            <select class="select" id="fix-month">
              ${Array.from({ length: 12 }, (_, i) => i + 1)
                .map((m) => `<option value="${m}" ${m === month ? "selected" : ""}>${m}</option>`).join("")}
            </select>
            <select class="select" id="fix-day"></select>
          </div>
        </label>
      </div>
      <p class="hint hint--bad hidden" id="fix-birth-error"></p>
      <div class="row" style="gap:.6rem;margin-top:1.2rem;justify-content:flex-end">
        <button class="btn btn-ghost" data-admin-action="close-modal">取消</button>
        <button class="btn btn-primary" data-admin-action="fix-user-birth-save"
                data-uid="${node.dataset.uid}" data-name="${esc(name)}">保存</button>
      </div>`);

    const monthEl = $("#fix-month");
    const dayEl = $("#fix-day");
    const rebuildDays = () => {
      const max = FIX_MONTH_DAYS[Number(monthEl.value) || 1];
      const keep = Math.min(Number(dayEl.value) || day, max);
      dayEl.innerHTML = Array.from({ length: max }, (_, i) => i + 1)
        .map((d) => `<option value="${d}" ${d === keep ? "selected" : ""}>${d}</option>`).join("");
    };
    monthEl.addEventListener("change", rebuildDays);
    rebuildDays();
  }

  /* ！临时密码只在这一个响应里出现过一次 ——
     库里存的是哈希，关掉弹窗就再也拿不回来了。
     所以这里用「只读输入框 + 复制按钮」而不是一段纯文本：
     纯文本在某些浏览器里选中不干净，而管理员是要把它念给另一个人听的。 */
  function showTempPassword(result) {
    openModal(`
      <span class="eyebrow eyebrow--iris">Temporary password</span>
      <h2 class="display display-md" style="margin:.35rem 0 0">临时密码</h2>
      <p class="hint" style="margin-top:.5rem">
        把这串告诉 <b>${esc(result.nickname)}</b> 本人。
        <b>关掉这个窗口就再也看不到了</b>（服务器只存哈希，找不回来）。
      </p>

      <div class="iris-rule iris-rule--fade" style="margin:1.1rem 0"></div>

      <input class="input temp-password" id="temp-password" readonly
             value="${esc(result.password)}" spellcheck="false">

      <p class="hint" style="margin-top:.9rem">
        他登录后可以自己去「我的 → 账号 → 修改密码」改成自己的。
        所有旧登录已经立刻失效了。
      </p>

      <div class="row" style="justify-content:flex-end;gap:.5rem;margin-top:1.2rem">
        <button class="btn btn-ghost" data-admin-action="close-modal">关闭</button>
        <button class="btn btn-primary" data-admin-action="copy-temp-password">复制</button>
      </div>`);
  }

  /* ============================================================
     四、安全记录（人机验证）
     ------------------------------------------------------------
     这里原来是「邮件记录」，展示验证码被发到哪些邮箱去了。
     v0.21 把邮件整个取消之后，同样要看的东就换了对象 ——
     对手从「拿别人的邮箱刷验证码」变成了「拿脚本硬试密码」，
     所以这里看的是：谁在领图、谁一直在错。

     ！这里显示的只能是网络地址，不会是账号：
       验证码是**登录前**发的，那一刻服务端本来就不知道对方是谁。
       想知道「是谁在试」得去看举报页和用户列表。
     ============================================================ */

  const PURPOSE_LABEL = {
    login: "登录",
    register: "注册",
    admin: "后台",
  };

  async function renderSecurityLogs() {
    const data = await api("/api/admin/security?limit=80");
    const stats = data.stats;

    const warning = data.suspicious.length
      ? `<div class="notice notice--warn" style="margin-bottom:1.1rem">
           <b>注意：</b>最近 1 小时有 ${data.suspicious.length} 个网络地址在反复领验证码
           或者一直在输错，可能是有人在拿脚本试密码。
         </div>
         <div class="panel panel--flush" style="margin-bottom:1.3rem">
           ${data.suspicious.map((row) => `
             <div class="admin-row">
               <div class="body">
                 <span class="title">
                   <span>${esc(row.created_ip || "未知地址")}</span>
                   ${row.wrong > 0 ? `<span class="reason-tag">错 ${row.wrong} 次</span>` : ""}
                   <span class="tag">共领 ${row.issued} 张</span>
                 </span>
                 <span class="meta">单张最多错过 ${row.worst} 次</span>
               </div>
             </div>`).join("")}
         </div>`
      : `<p class="hint" style="margin-bottom:1.2rem">最近 1 小时没有异常的人机验证记录。</p>`;

    const rows = data.items.length
      ? data.items.map((row) => `
          <div class="admin-row">
            <div class="body">
              <span class="title">
                <span>${esc(PURPOSE_LABEL[row.purpose] || row.purpose)}</span>
                ${row.passed
                  ? `<span class="status-dot">已通过</span>`
                  : (row.expired
                      ? `<span class="status-dot status-dot--off">已过期</span>`
                      : `<span class="status-dot status-dot--off">待填写</span>`)}
              </span>
              <span class="meta">
                ${fmtRelative(row.created_at)} · 来源 ${esc(row.created_ip || "未知")} ·
                输错 ${row.attempts} 次
              </span>
            </div>
          </div>`).join("")
      : `<div class="empty"><span class="glyph">◈</span><p>还没有人机验证记录</p></div>`;

    $("#admin-view").innerHTML = `
      ${warning}

      <div class="stat-grid" style="margin-bottom:1.4rem">
        ${[
          ["24 小时发出", stats.issued_24h, ""],
          ["24 小时通过", stats.passed_24h, ""],
          ["通过率", `${stats.pass_rate}%`, ""],
          ["1 小时发出", stats.issued_hour, ""],
        ].map(([label, value, cls]) => `
          <div class="stat-card ${cls}">
            <span class="stat-card__value">${value}</span>
            <span class="stat-card__label">${label}</span>
          </div>`).join("")}
      </div>

      <div class="section-title">
        <h2>最近的人机验证</h2>
        <span class="rule"></span>
        <span class="eyebrow">${data.items.length} 条</span>
      </div>

      <div class="panel panel--flush">${rows}</div>

      <p class="hint" style="margin-top:1.2rem">
        系统已限制：同一张图最多试 5 次就作废，5 分钟过期，
        同一网络地址每小时最多领 120 张。
        所以「领得多」或「错得多」同时出现时，基本可以确定是脚本。
      </p>`;
  }

  /* ============================================================
     五、系统升级（备份 / 导出 / 导入 / 还原 / 版本）
     ------------------------------------------------------------
     这一页回答的是一个很实际的问题：
     「我把代码换成新版本之后，原来的用户数据还在不在？」
     所以它把三件事摊开给管理员看：
       1. 程序和数据库各是什么版本，差在哪里；
       2. 手上现在有哪些备份（可以下载、可以回滚）；
       3. 以前每次升级的成败记录。
     ============================================================ */

  const KIND_LABEL = {
    install: "首次建库",
    upgrade: "结构升级",
    restore: "还原备份",
    import: "导入数据库",
  };

  function stampText(ts) {
    if (!ts) return "—";
    return new Date(Number(ts) * 1000).toLocaleString("zh-CN", { hour12: false });
  }

  async function downloadFile(url) {
    /* 不能直接用 <a href> 下载：那样不会带 Authorization 头，后台会回 401。
       所以先用带令牌的 fetch 把文件拿成 Blob，再自己造一个链接触发下载。 */
    const response = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
    if (response.status === 401) {
      token = "";
      sessionStorage.removeItem(TOKEN_KEY);
      showGate();
      throw new Error("管理员登录已过期");
    }
    if (!response.ok) {
      let detail = "下载失败";
      try { detail = (await response.json()).detail || detail; } catch (error) { /* 出错时不是 JSON */ }
      throw new Error(detail);
    }

    const disposition = response.headers.get("content-disposition") || "";
    const matched = /filename="?([^";]+)"?/.exec(disposition);
    const name = matched ? matched[1] : "mcfr.db";

    const blob = await response.blob();
    const href = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = href;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    // 立刻 revoke 会让部分浏览器来不及取数据，延后一点
    window.setTimeout(() => URL.revokeObjectURL(href), 8000);
    return name;
  }

  async function importDatabaseFile(file) {
    const confirmed = window.confirm(
      `确定要用「${file.name}」替换当前的全部数据吗？\n\n` +
      "· 当前数据会先自动备份一份，随时可以还回来（在下方备份列表里还原）\n" +
      "· 文件里的用户、资料、关系都会保留，并自动升级到当前版本\n" +
      "· 所有人的登录状态会失效，需要重新登录一次，账号和资料都在"
    );
    if (!confirmed) return;

    const response = await fetch("/api/admin/system/import", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/octet-stream",
        // 文件名可能带中文，HTTP 头只能放 ASCII，所以先编码一下
        "X-Filename": encodeURIComponent(file.name),
      },
      body: file,
    });

    let data = null;
    try { data = await response.json(); } catch (error) { data = null; }

    if (response.status === 401) {
      token = "";
      sessionStorage.removeItem(TOKEN_KEY);
      showGate();
      throw new Error("管理员登录已过期");
    }
    if (!response.ok) {
      throw new Error((data && data.detail) || "导入失败");
    }

    State.system = data.system;
    return data;
  }

  async function renderSystem() {
    const data = await api("/api/admin/system");
    State.system = data;

    const work = data.work || {};
    const health = data.health || {};
    const problems = [];

    if (work.missing_tables && work.missing_tables.length) {
      problems.push(`缺少数据表：${work.missing_tables.join("、")}`);
    }
    // ！索引缺失也要报出来（v0.31）。不报的话，往 SCHEMA 里新加的索引
    //   在已有部署上是静默失效的 —— 站点照跑、只是慢，没人会发现。
    if (work.missing_indexes && work.missing_indexes.length) {
      problems.push(`缺少索引 ${work.missing_indexes.length} 条：${work.missing_indexes.join("、")}`);
    }
    if (work.missing_columns && work.missing_columns.length) {
      problems.push(`users 表缺 ${work.missing_columns.length} 个字段：${work.missing_columns.join("、")}`);
    }
    if (work.legacy_tables && work.legacy_tables.length) {
      problems.push(`还留着早就废弃的表：${work.legacy_tables.join("、")}`);
    }
    if (work.migrations && work.migrations.length) {
      problems.push(`有 ${work.migrations.length} 条数据订正还没执行`);
    }
    (health.problems || []).forEach((item) => problems.push(item));

    const upToDate = !data.need_upgrade && health.ok !== false;

    const banner = problems.length
      ? `<div class="notice notice--warn" style="margin-bottom:1.3rem">
           <b>需要处理：</b>
           <ul style="margin:.4rem 0 0 1.1rem">${problems.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>
           <p style="margin:.6rem 0 0">点下面的「立即升级」就会自动处理：
             升级前先备份，出问题会自动回滚到升级前的样子，用户数据不会丢。</p>
         </div>`
      : `<div class="notice" style="margin-bottom:1.3rem">
           <b>一切正常。</b>数据库结构与当前程序版本一致，用户数据完整。
         </div>`;

    const backups = data.backups || [];
    const canManage = can("system", "manage");
    const backupRows = backups.length
      ? backups.map((item, index) => `
          <div class="admin-row">
            <div class="body">
              <span class="title">
                <span>${esc(item.label)}</span>
                <span class="tag">库版本 v${Number(item.version)}</span>
                ${index === 0 ? `<span class="status-dot">最新</span>` : ""}
              </span>
              <span class="meta">${esc(item.name)}</span>
              <span class="meta">${stampText(item.created_at)} · ${esc(item.size_text)}</span>
            </div>
            <div class="actions">
              ${canManage ? `
                <button class="btn btn-ghost btn-sm" data-admin-action="download-backup"
                        data-name="${esc(item.name)}">下载</button>
                <button class="btn btn-ghost btn-sm" data-admin-action="restore-backup"
                        data-name="${esc(item.name)}" data-label="${esc(item.label)}"
                        data-time="${stampText(item.created_at)}">还原</button>
                <button class="btn btn-danger btn-sm" data-admin-action="delete-backup"
                        data-name="${esc(item.name)}" data-label="${esc(item.label)}">删除</button>`
              : `<span class="meta">只能查看</span>`}
            </div>
          </div>`).join("")
      : `<div class="empty"><span class="glyph">▤</span><p>还没有备份</p></div>`;

    const records = data.history || [];
    const recordRows = records.length
      ? records.map((row) => `
          <div class="admin-row">
            <div class="body">
              <span class="title">
                <span>${esc(KIND_LABEL[row.kind] || row.kind)}</span>
                <span class="tag">v${Number(row.from_version)} → v${Number(row.to_version)}</span>
                ${row.status === "ok"
                  ? `<span class="status-dot">成功</span>`
                  : `<span class="status-dot status-dot--off">已回滚</span>`}
              </span>
              <span class="meta">${stampText(row.created_at)}
                ${row.backup_name ? ` · 升级前快照：${esc(row.backup_name)}` : ""}</span>
              ${row.detail ? `<span class="meta">${esc(row.detail)}</span>` : ""}
            </div>
          </div>`).join("")
      : `<div class="empty"><span class="glyph">≡</span><p>还没有升级记录</p></div>`;

    $("#admin-view").innerHTML = `
      ${banner}

      <div class="stat-grid">
        <div class="stat-card">
          <b>v${esc(data.app_version)}</b>
          <span>程序版本</span>
        </div>
        <div class="stat-card">
          <b>v${Number(data.schema_version)}</b>
          <span>数据库版本（目标 v${Number(data.target_schema_version)}）</span>
        </div>
        <div class="stat-card">
          <b>${Number(data.db.users)}</b>
          <span>用户账号</span>
        </div>
        <div class="stat-card ${upToDate ? "" : "stat-card--alert"}">
          <b>${upToDate ? "正常" : "待升级"}</b>
          <span>自检结果</span>
        </div>
      </div>

      <div class="section-title"><h2>数据库</h2><span class="rule"></span></div>

      <div class="panel panel--flush" style="margin-bottom:1.3rem">
        <div class="admin-row">
          <div class="body">
            <span class="title"><span>文件位置</span></span>
            <span class="meta" style="word-break:break-all">${esc(data.db.path)}</span>
          </div>
        </div>
        <div class="admin-row">
          <div class="body">
            <span class="title"><span>当前大小</span></span>
            <span class="meta">${esc(data.db.size_text)} · 备份目录 ${esc(data.data_dir)}\\backups</span>
          </div>
        </div>
        <div class="admin-row">
          <div class="body">
            <span class="title"><span>完整性自检</span></span>
            <span class="meta">${esc(health.integrity || "未检查")}
              ${Number(health.foreign_key_issues) > 0
                ? ` · 外键异常 ${Number(health.foreign_key_issues)} 条`
                : " · 外键检查通过"}</span>
          </div>
        </div>
      </div>

      <div class="stack" style="gap:.6rem;margin-bottom:1.6rem">
        <div class="actions" style="display:flex;gap:.5rem;flex-wrap:wrap">
          ${canManage ? `
            <button class="btn btn-primary" data-admin-action="backup-now">立即备份</button>
            <button class="btn" data-admin-action="download-db">下载当前数据库</button>
            <button class="btn" data-admin-action="import-db-open">导入数据库文件</button>
            ${data.need_upgrade
              ? `<button class="btn btn-primary" data-admin-action="run-upgrade">立即升级</button>`
              : `<button class="btn btn-ghost" data-admin-action="run-upgrade">重新检查并升级</button>`}
            <input type="file" id="db-file" accept=".db,application/octet-stream" class="hidden">`
          : `<span class="meta">当前账号只有查看权限，不能备份 / 导入 / 升级。</span>`}
        </div>
        <p class="hint">
          换机器、重装系统之前，先点「下载当前数据库」拿走一份 .db 文件；
          新环境启动后用「导入数据库文件」把它放回来，用户账号、资料、关系都会原样保留。
          导入后程序会自动把旧库升级到当前版本。
        </p>
      </div>

      <div class="section-title">
        <h2>备份</h2><span class="rule"></span>
        <span class="eyebrow">${backups.length} 份 · 最多保留 ${Number(data.keep_backups)} 份</span>
      </div>
      <div class="panel panel--flush" style="margin-bottom:1.3rem">${backupRows}</div>

      <div class="section-title"><h2>升级记录</h2><span class="rule"></span></div>
      <div class="panel panel--flush">${recordRows}</div>

      <p class="hint" style="margin-top:1.2rem">
        每次启动时，服务会先检查数据库与程序是否一致：
        有差异就自动备份 → 升级 → 自检，任何一步出错都会用备份把数据盖回去，
        宁可停在旧版本，也不会留下一个半新半旧的数据库。
      </p>`;

    // 备份 / 还原 / 删除这些操作是直接调 renderSystem() 重绘的，没走 setView()，
    // 所以这里自己把顶栏那个「更新于 …」补上，否则它会一直停在进页面时的。
    const stamp = $("#admin-updated");
    if (stamp) stamp.textContent = `更新于 ${new Date().toLocaleTimeString("zh-CN")}`;
  }

  /* ============================================================
     六、意见反馈
     ------------------------------------------------------------
     属于受控模块：
       view   → 只能看
       manage → 还能改状态、写回复、删除
     按钮要不要画由服务端返回的 can_manage 决定；就算有人手动改 DOM，
     后端那几个接口也各自挡着（require_area("feedback","manage")）。
     ============================================================ */

  const FEEDBACK_STATUS = {
    open: ["待处理", "status-dot"],
    read: ["已查看", "status-dot"],
    closed: ["已处理", "status-dot status-dot--off"],
  };

  async function renderFeedback() {
    const data = await api(`/api/admin/feedback?status=${State.feedbackFilter}`);
    const canManage = Boolean(data.can_manage);

    const filters = [
      ["all", "全部"],
      ["open", "待处理"],
      ["read", "已查看"],
      ["closed", "已处理"],
    ];

    const rows = data.items.length
      ? data.items.map((item) => {
          const [statusText, statusClass] = FEEDBACK_STATUS[item.status] || [item.status, "status-dot"];
          const who = item.guest
            ? `<span class="tag">访客</span>`
            : `<span class="tag tag--iris">@${esc(item.user_username || "")}</span>`;
          const actions = canManage
            ? `<div class="actions">
                 <button class="btn btn-ghost btn-sm" data-admin-action="feedback-reply"
                         data-fid="${item.id}">${item.reply ? "改回复" : "回复"}</button>
                 ${item.status === "closed"
                   ? `<button class="btn btn-ghost btn-sm" data-admin-action="feedback-reopen"
                              data-fid="${item.id}">重新打开</button>`
                   : `<button class="btn btn-primary btn-sm" data-admin-action="feedback-close"
                              data-fid="${item.id}">标记已处理</button>`}
                 <button class="btn btn-danger btn-sm" data-admin-action="feedback-delete"
                         data-fid="${item.id}">删除</button>
               </div>`
            : "";

          return `
            <div class="admin-row">
              <div class="body">
                <span class="title">
                  <span class="reason-tag">${esc(item.kind_label)}</span>
                  ${who}
                  <span class="${statusClass}">${esc(statusText)}</span>
                </span>
                <span class="meta">
                  ${fmtRelative(item.created_at)}
                  ${item.page ? ` · 来自 ${esc(item.page)}` : ""}
                  ${item.handled_by ? ` · 由 ${esc(item.handled_by)} 处理` : ""}
                </span>
                <div class="detail">${esc(item.content)}</div>
                ${item.contact ? `<span class="meta">联系方式：${esc(item.contact)}</span>` : ""}
                ${item.reply ? `<div class="detail"><b>回复：</b>${esc(item.reply)}</div>` : ""}
              </div>
              ${actions}
            </div>`;
        }).join("")
      : `<div class="empty"><span class="glyph">✉</span><p>这个分类下还没有反馈</p></div>`;

    $("#admin-view").innerHTML = `
      <div class="admin-tabs" style="margin-bottom:1.1rem">
        ${filters.map(([key, label]) => `
          <button data-feedback-filter="${key}" aria-selected="${State.feedbackFilter === key}">
            ${label}${key === "open" && data.counts.open ? ` · ${data.counts.open}` : ""}
          </button>`).join("")}
      </div>

      ${canManage ? "" : `<div class="notice" style="margin-bottom:1.1rem">
         你自己只能<b>查看</b>反馈。需要处理它们的话，请让超级管理员在「管理员」页里把你的
         「意见反馈」权限调成「能改」。
      </div>`}

      <div class="section-title">
        <h2>用户反馈</h2><span class="rule"></span>
        <span class="eyebrow">共 ${Number(data.counts.total)} 条 · 待处理 ${Number(data.counts.open)}</span>
      </div>

      <div class="panel panel--flush">${rows}</div>

      <p class="hint" style="margin-top:1.2rem">
        反馈允许匿名提交 —— 登录不上来的人恰好是最需要反馈的，
        所以「访客」那一类也要认真看。同一个人一小时最多 5 条、一天 20 条。
      </p>`;

    tabBar($("#admin-view .admin-tabs"));
  }

  function feedbackReplyModal(item) {
    openModal(`
      <span class="eyebrow eyebrow--iris">Feedback</span>
      <h2 class="display display-md" style="margin-top:.45rem">写一条回复</h2>
      <p class="hint" style="margin-top:.5rem">
        这段回复会保存在这条反馈下面，供其它管理员查看。
        对方如果要收到回复，得靠反馈里留下的联系方式。
      </p>
      <div class="iris-rule iris-rule--fade" style="margin:1rem 0"></div>
      <div class="stack">
        <div class="detail" style="padding:.6rem .75rem;border-left:2px solid var(--line-strong);
                    background:var(--cloud);border-radius:var(--r-sm);font-size:.813rem">
          ${esc(item.content)}
        </div>
        <label class="field">
          <span>回复内容</span>
          <textarea class="textarea" id="fb-reply" maxlength="500"
                    placeholder="例如：已收到，正在排查 QQ 邮箱的投递问题">${esc(item.reply || "")}</textarea>
        </label>
        <div class="row-between wrap">
          <button class="btn btn-ghost" data-admin-action="close-modal">取消</button>
          <button class="btn btn-primary" data-admin-action="feedback-save-reply"
                  data-fid="${item.id}">保存回复</button>
        </div>
      </div>`);
  }

  /* ============================================================
     七、管理员管理（超级管理员专属）
     ------------------------------------------------------------
     这里能做的事：添加普通管理员、改显示名 / 备注、重置密码、
     停用 / 恢复、删除，以及逐项设置「某个模块能不能看 / 能不能改」。
     ============================================================ */

  const LEVEL_LABELS = { none: "看不到", view: "只能看", manage: "能改" };

  function permissionPicker(perms) {
    return State.areas.map((area) => `
      <div class="perm-row">
        <div class="perm-info">
          <b>${esc(area.name)}</b>
          <span>${esc(area.desc)}</span>
        </div>
        <div class="perm-levels" data-perm-area="${esc(area.key)}">
          ${["none", "view", "manage"].map((level) => `
            <button type="button" class="perm-btn"
                    data-admin-action="perm-set"
                    data-area="${esc(area.key)}" data-level="${level}"
                    aria-pressed="${(perms[area.key] || "none") === level}">
              ${LEVEL_LABELS[level]}
            </button>`).join("")}
          </div>
      </div>`).join("");
  }

  function adminForm(admin) {
    const editing = Boolean(admin);
    const isSuper = Boolean(admin && admin.is_super);
    const perms = (admin && admin.permissions) || {};

    return `
      <span class="eyebrow eyebrow--iris">Admins</span>
      <h2 class="display display-md" style="margin-top:.45rem">
        ${editing ? `编辑「${esc(admin.display_name || admin.username)}」` : "添加普通管理员"}
      </h2>
      <p class="hint" style="margin-top:.5rem">
        ${isSuper
          ? "这是超级管理员账号，它天生拥有一切权限，不需要也不能逐项设置。"
          : "普通管理员固定能用「数据概览 / 举报处理 / 用户管理 / 邮件记录」，其余模块按下面的设置来。"}
      </p>

      <div class="iris-rule iris-rule--fade" style="margin:1rem 0"></div>

      <div class="stack">
        <div class="form-grid">
          <label class="field">
            <span>用户名（登录用）</span>
            <input class="input" id="adm-username" maxlength="24" spellcheck="false"
                   value="${editing ? esc(admin.username) : ""}"
                   ${editing ? "disabled" : ""} placeholder="3-24 位字母数字下划线">
          </label>
          <label class="field">
            <span>显示名</span>
            <input class="input" id="adm-display" maxlength="24"
                   value="${editing ? esc(admin.display_name || "") : ""}" placeholder="别人怎么称呼 TA">
          </label>
        </div>

        <label class="field">
          <span>${editing ? "重置密码（留空就不改）" : "初始密码"}</span>
          <input class="input" id="adm-password" type="password" autocomplete="new-password"
                 placeholder="至少 8 位，不要只用纯数字或纯字母">
        </label>

        <label class="field">
          <span>备注（只有超管能看见）</span>
          <input class="input" id="adm-note" maxlength="120"
                 value="${editing ? esc(admin.note || "") : ""}" placeholder="例如：负责处理举报的同学">
        </label>

        ${isSuper ? "" : `
          <div class="field">
            <span>额外权限</span>
            <div class="perm-list" id="adm-perms">${permissionPicker(perms)}</div>
          </div>`}

        <p class="error-text hidden" id="adm-error"></p>

        <div class="row-between wrap">
          <button class="btn btn-ghost" data-admin-action="close-modal">取消</button>
          <button class="btn btn-primary" data-admin-action="admin-save"
                  data-aid="${editing ? admin.id : 0}">
            ${editing ? "保存修改" : "添加"}
          </button>
        </div>
      </div>`;
  }

  /* ============================================================
     十、登录密钥（v0.32，只给超管）
     ============================================================ */

  const stamp = (ts) => (Number(ts)
    ? new Date(Number(ts) * 1000).toLocaleString("zh-CN", { hour12: false })
    : "—");

  async function renderAdminkeys() {
    const data = await api("/api/admin/keys");
    const keys = data.keys || [];
    const canManage = !!data.can_manage;
    const keysExist = keys.length > 0;

    const rows = keys.map((key) => {
      const used = key.last_used_at
        ? `上次使用 ${fmtRelative(key.last_used_at)}${key.last_used_ip ? `（${esc(key.last_used_ip)}）` : ""}`
        : "还没用它登录过";
      return `
        <div class="admin-row">
          <div class="body">
            <span class="title">
              <span>${esc(key.label || "（没写标签）")}</span>
              <span class="tag tag--iris"><code>${esc(key.key_id)}</code></span>
              ${State.signKey && State.signKey.id === key.key_id
                ? `<span class="reason-tag">这次就是用它进来的</span>` : ""}
            </span>
            <span class="meta">创建于 ${stamp(key.created_at)} · ${used}</span>
          </div>
          <div class="actions">
            ${canManage ? `<button class="btn btn-danger btn-sm"
                     data-admin-action="key-delete" data-key="${esc(key.key_id)}"
                     data-label="${esc(key.label || key.key_id)}">删除</button>` : ""}
          </div>
        </div>`;
    }).join("");

    $("#admin-view").innerHTML = `
      <div class="notice" style="margin-bottom:1.2rem">
        <b>用密钥代替密码。</b>
        私钥存在你自己的电脑上（就是那个 <code>.mcfrkey</code> 文件），
        服务器只保存公钥 —— 就算数据库被人整个拖走，也<b>没有</b>任何人能拿它进后台。
        建议再复制一份放进加密 U 盘，电脑上的丢了还有得救。
      </div>

      <div class="section-title">
        <h2>登录密钥</h2><span class="rule"></span>
        <span class="eyebrow">共 ${keys.length} 把</span>
        ${canManage ? `<button class="btn btn-primary btn-sm"
               data-admin-action="key-new" data-haskeys="${keysExist ? 1 : 0}">生成新密钥</button>` : ""}
      </div>

      <div class="panel panel--flush">${rows || `<div class="empty">
        <span class="glyph">🔑</span>
        <p>还没有密钥。点右上角「生成新密钥」，下载那个 <code>.mcfrkey</code> 文件收好。</p>
      </div>`}</div>

      <div class="section-title" style="margin-top:2rem">
        <h2>密码登录入口</h2><span class="rule"></span>
        <span class="eyebrow">${data.password_login ? "现在开着" : "已经关掉"}</span>
      </div>

      <div class="panel" style="padding:1.1rem 1.2rem">
        <p class="hint" style="margin-bottom:1rem">
          ${data.password_login
            ? "现在还能用密码进后台。等密钥都用顺了就可以关掉它 —— 关掉之后只有密钥文件能进门，密码就再也刷不进来了。"
            : "密码入口已经关闭，只能用密钥文件登录。想改回来需要至少留着一把密钥。"}
        </p>
        ${canManage ? `<button class="btn ${data.password_login ? "btn-ghost" : "btn-primary"} btn-sm"
               data-admin-action="key-password" data-next="${data.password_login ? 0 : 1}">
               ${data.password_login ? "关闭密码登录" : "重新开启密码登录"}</button>` : ""}
      </div>

      <p class="hint" style="margin-top:1.2rem">
        <b>怎么进后台：</b>打开后台首页 → 选「用密钥文件」→ 选中 <code>.mcfrkey</code> → 点登录。
        签名在你自己的电脑上完成，私钥永远不会上传。
        <br><b>密钥丢了怎么办：</b>手里还有别的密钥，就用它进来把这把删掉；
        如果只剩最后一把也丢了，请按 README 里的应急步骤在服务器上重置。
      </p>`;
  }

  /* ---- 生成 / 删除密钥，开关密码入口 ---- */

  function downloadText(text, filename) {
    const blob = new Blob([text], { type: "application/x-pem-file" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    // ！必须回收：blob URL 会把整份内容一直钉在内存里
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  /** 后端的「证明身份」规则：有密钥就要求签名，一把都没有才回退到密码。 */
  function keyProofFields(keysExist) {
    if (State.signKey) {
      return `<p class="hint">会用你这次登录用的那把密钥
        （<code>${esc(State.signKey.id)}</code>）签名确认，不用再操作什么。</p>`;
    }
    if (keysExist) {
      return `
        <label class="field">
          <span>用哪把密钥确认</span>
          <input class="input" type="file" name="key_file" required
                 accept=".mcfrkey,.key,.pem,application/x-pem-file">
        </label>
        <p class="hint">先选中任意一把属于你的密钥文件 —— 用它证明这次的改动是你本人发起的。</p>`;
    }
    return `
      <label class="field">
        <span>管理员密码</span>
        <input class="input" name="password" type="password" required
               autocomplete="current-password" placeholder="还没有密钥，先用密码确认">
      </label>`;
  }

  /** 把「证明身份」所需的字段凑成一份请求体。 */
  async function keyProofValues(form) {
    const body = {};
    let key = null;
    let keyId = "";

    if (State.signKey) {
      key = State.signKey.key;
      keyId = State.signKey.id;
    } else {
      // ！用 querySelector 而不是 form.key_file：三个弹框里只有一个带文件框，
      //   直接点名字取到的是 undefined，会在 .files 上炸掉。
      const input = form.querySelector('input[name="key_file"]');
      const file = input && input.files ? input.files[0] : null;
      if (file) {
        key = await importKeyFile(file);
        keyId = await keyIdOf(key);
      }
    }

    if (!key) {
      const input = form.querySelector('input[name="password"]');
      body.password = input ? input.value : "";
      return body;
    }

    const start = await api("/api/admin/keys/challenge", { method: "POST" });
    body.challenge = start.challenge;
    body.key_id = keyId;
    body.signature = await signWithKey(key, start.challenge);
    return body;
  }

  const MODAL_ACTIONS = `<div style="display:flex;gap:.6rem;justify-content:flex-end">
    <button class="btn btn-ghost" type="button" data-admin-action="close-modal">取消</button>`;

  /** 把弹框里的提交按钮接起来：出错就地显示，不关弹框。 */
  function wireModalForm(selector, submit) {
    const form = $(selector);
    if (!form) return;
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const errorBox = $("#admin-key-modal-error");
      const button = form.querySelector("button[type=submit]");
      const label = button ? button.textContent.trim() : "";
      errorBox.classList.add("hidden");
      button.disabled = true;
      button.textContent = "处理中…";
      try {
        await submit(form);
      } catch (error) {
        errorBox.textContent = error.message;
        errorBox.classList.remove("hidden");
      } finally {
        button.disabled = false;
        button.textContent = label;
      }
    });
  }

  function openKeyNewModal(keysExist) {
    openModal(`
      <h2 class="display display-sm">生成新密钥</h2>
      <p class="hint">生成之后私钥<b>只会显示这一次</b>，请当场下载保存。
        服务器不会再给你第二遍 —— 它只存得下公钥。</p>
      <form id="admin-key-new-form" class="stack">
        <label class="field">
          <span>标签</span>
          <input class="input" name="label" maxlength="20" required
                 value="${keysExist ? "" : "第一把密钥"}"
                 placeholder="比如：家里的电脑 / 加密 U 盘">
        </label>
        ${keyProofFields(keysExist)}
        <p class="error-text hidden" id="admin-key-modal-error"></p>
        ${MODAL_ACTIONS}
          <button class="btn btn-primary" type="submit">生成并下载</button>
        </div>
      </form>`);

    wireModalForm("#admin-key-new-form", async (form) => {
      const body = { label: form.label.value.trim() || "未命名" };
      Object.assign(body, await keyProofValues(form));
      const result = await api("/api/admin/keys", { method: "POST", body });
      downloadText(result.private_pem, result.file_name);
      // ！用户已经有密钥了，下次默认就走密钥登录那条路
      try {
        localStorage.setItem(LOGIN_MODE_KEY, "key");
      } catch (error) { /* 忽略 */ }
      closeModal();
      toast("密钥已生成，请确认文件已经存好了", "ok");
      await setView("adminkeys");
    });
  }

  function openKeyDeleteModal(keyId, label) {
    openModal(`
      <h2 class="display display-sm">删除密钥</h2>
      <p class="hint">要删掉的是「<b>${esc(label)}</b>」（<code>${esc(keyId)}</code>）。
        删掉之后，那个文件就再也进不了后台了。</p>
      <form id="admin-key-del-form" class="stack">
        ${keyProofFields(true)}
        <p class="error-text hidden" id="admin-key-modal-error"></p>
        ${MODAL_ACTIONS}
          <button class="btn btn-danger" type="submit">确认删除</button>
        </div>
      </form>`);

    wireModalForm("#admin-key-del-form", async (form) => {
      const body = await keyProofValues(form);
      await api(`/api/admin/keys/${encodeURIComponent(keyId)}`, {
        method: "DELETE", body,
      });
      closeModal();
      toast("已经删掉这把密钥", "ok");
      await setView("adminkeys");
    });
  }

  function openKeyPasswordModal(next) {
    openModal(`
      <h2 class="display display-sm">${next ? "重新开启密码登录" : "关闭密码登录"}</h2>
      <p class="hint">${next
        ? "开回去之后，密码这条路又能进门了。"
        : "关掉之后<b>只有密钥文件</b>能进后台。请先确认手上的密钥文件真的能用、而且有备份。"}</p>
      <form id="admin-key-pwd-form" class="stack">
        <label class="field">
          <span>管理员密码</span>
          <input class="input" name="password" type="password" required
                 autocomplete="current-password" placeholder="确认是你本人操作">
        </label>
        <p class="error-text hidden" id="admin-key-modal-error"></p>
        ${MODAL_ACTIONS}
          <button class="btn ${next ? "btn-primary" : "btn-danger"}" type="submit">
            ${next ? "开启" : "关闭"}
          </button>
        </div>
      </form>`);

    wireModalForm("#admin-key-pwd-form", async (form) => {
      await api("/api/admin/password-login", {
        method: "PATCH",
        body: { enabled: !!next, password: form.password.value },
      });
      closeModal();
      toast(next ? "密码登录已重新开启" : "密码登录已关闭，以后只能用密钥文件进", "ok");
      await setView("adminkeys");
    });
  }

  async function renderAdmins() {
    const data = await api("/api/admin/admins");
    const meId = State.me ? State.me.id : 0;

    const rows = data.items.map((admin) => {
      const role = admin.is_super
        ? `<span class="reason-tag">超级管理员</span>`
        : `<span class="tag tag--iris">普通管理员</span>`;
      const disabled = admin.status !== "active";
      const stateDot = disabled
        ? `<span class="status-dot status-dot--off">已停用</span>`
        : `<span class="status-dot">启用中</span>`;

      const permLine = admin.is_super
        ? `<span class="meta">全部权限</span>`
        : State.areas.map((area) => {
            const level = admin.permissions[area.key] || "none";
            const cls = level === "none" ? "perm-chip perm-chip--off" : "perm-chip";
            return `<span class="${cls}">${esc(area.name)}：${LEVEL_LABELS[level]}</span>`;
          }).join("");

      const actions = [];
      actions.push(`<button class="btn btn-ghost btn-sm" data-admin-action="admin-edit"
                            data-aid="${admin.id}">编辑</button>`);
      if (!admin.is_super && admin.id !== meId) {
        actions.push(disabled
          ? `<button class="btn btn-ghost btn-sm" data-admin-action="admin-enable"
                     data-aid="${admin.id}">恢复</button>`
          : `<button class="btn btn-ghost btn-sm" data-admin-action="admin-disable"
                     data-aid="${admin.id}" data-name="${esc(admin.display_name || admin.username)}">停用</button>`);
        actions.push(`<button class="btn btn-danger btn-sm" data-admin-action="admin-delete"
                              data-aid="${admin.id}" data-name="${esc(admin.display_name || admin.username)}">删除</button>`);
      }

      return `
        <div class="admin-row">
          <div class="body">
            <span class="title">
              <span>${esc(admin.display_name || admin.username)}</span>
              ${role}${stateDot}
              ${admin.id === meId ? `<span class="tag">这是你</span>` : ""}
            </span>
            <span class="meta">
              @${esc(admin.username)}
              ${admin.note ? ` · ${esc(admin.note)}` : ""}
            </span>
            <span class="meta">
              ${admin.last_login ? `上次登录 ${fmtRelative(admin.last_login)}` : "还没登录过"}
              · 当前 ${Number(admin.active_sessions)} 个登录中
            </span>
            <div class="perm-chips">${permLine}</div>
          </div>
          <div class="actions">${actions.join("")}</div>
        </div>`;
    }).join("");

    $("#admin-view").innerHTML = `
      <div class="notice" style="margin-bottom:1.2rem">
        <b>两级管理员。</b>
        超级管理员（<code>${esc(data.super_username)}</code>）拥有一切权限，并且能添加 / 停用 / 删除普通管理员；
        普通管理员固定能用「数据概览 / 举报处理 / 用户管理 / 邮件记录」，
        其余模块要在这里逐项授权。授权分三档：<b>看不到 / 只能看 / 能改</b>。
      </div>

      <div class="section-title">
        <h2>管理员账号</h2><span class="rule"></span>
        <span class="eyebrow">共 ${data.items.length} 个</span>
        <button class="btn btn-primary btn-sm" data-admin-action="admin-new">添加管理员</button>
      </div>

      <div class="panel panel--flush">${rows}</div>

      <p class="hint" style="margin-top:1.2rem">
        密码一律加盐哈希后入库，谁也看不到原文。
        停用或重置密码会让该账号<b>立刻</b>在所有设备上退出登录。
      </p>`;

    // 给「保存 / 停用 / 删除」按钮补上显示名，方便确认对话框写清楚是谁
    State.adminItems = data.items;
  }

  /* ============================================================
     启动器（v0.33）
     ------------------------------------------------------------
     桌面上那个程序不是网页，但「它听不听站长的话」全靠这一页：

       · 版本发布 —— 最新版本号、下载地址、是否强制更新
       · 功能开关 —— 关掉一项，客户端上那个入口立刻变灰
       · 公告     —— 打开启动器就能看到的一句话
       · 使用统计 —— 装了多少台、每天启动几次、都用的哪个版本

     ！所有写操作都要求「启动器管理」的 manage 权限；按钮画不画只是
       少让人踩一次 403，真正拦人的是后端那条 require_area。
     ============================================================ */

  const LAUNCHER_LEVELS = { info: "普通", warn: "提醒", critical: "重要" };

  function fmtDuration(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    const hours = Math.floor(total / 3600);
    if (hours >= 1000) return `${(hours / 1000).toFixed(1)}k 小时`;
    if (hours >= 1) return `${hours} 小时`;
    return `${Math.round(total / 60)} 分钟`;
  }

  /* 按天柱状图。
     ！用纯 div 画，不引图表库 —— 这个项目零依赖，为一个 14 根柱子
       的图去引一份 100KB 的图表库不值得，而且 CSP 也不允许外链脚本。 */
  function launcherDailyChart(daily) {
    if (!daily || !daily.length) {
      return `<p class="hint">还没有数据。启动器联网跑一次之后，这里会出现按天的柱状图。</p>`;
    }
    const max = Math.max(1, ...daily.map((row) => Math.max(row.launches, row.starts)));
    return `
      <div class="row" style="align-items:flex-end;gap:.35rem;height:120px;overflow-x:auto;padding-top:.4rem">
        ${daily.map((row) => {
          const value = Math.max(row.launches, row.starts);
          const height = Math.round((value / max) * 100);
          return `
            <div style="flex:1 0 1.6rem;display:flex;flex-direction:column;
                        align-items:center;justify-content:flex-end;height:100%"
                 title="${esc(row.day)} · 启动 ${Number(row.launches)} 次 · ${Number(row.devices)} 台设备">
              <span class="hint" style="font-size:.6rem">${value || ""}</span>
              <div style="width:100%;height:${Math.max(height, 2)}%;background:var(--iris);
                          border-radius:2px 2px 0 0"></div>
              <span class="hint" style="font-size:.58rem">${esc(String(row.day).slice(5))}</span>
            </div>`;
        }).join("")}
      </div>`;
  }

  function launcherDist(title, items, unit) {
    if (!items || !items.length) return "";
    const total = items.reduce((sum, item) => sum + Number(item.count || 0), 0) || 1;
    return `
      <div>
        <h3 style="margin:.4rem 0 .5rem;font-size:.95rem">${esc(title)}</h3>
        ${items.map((item) => {
          const percent = Math.round((Number(item.count || 0) / total) * 100);
          return `
            <div style="margin-bottom:.45rem">
              <div class="row" style="justify-content:space-between;gap:.5rem">
                <span class="hint">${esc(item.name)}</span>
                <span class="hint">${Number(item.count)} ${esc(unit)} · ${percent}%</span>
              </div>
              <div style="height:6px;border-radius:3px;background:var(--line);overflow:hidden">
                <span style="display:block;height:100%;width:${percent}%;background:var(--iris)"></span>
              </div>
            </div>`;
        }).join("")}
      </div>`;
  }

  function drawLauncher() {
    const data = State.launcher;
    const view = $("#admin-view");
    if (!data || !view) return;

    const manage = can("launcher", "manage");
    const locked = manage ? "" : 'disabled title="需要「启动器管理」的能改权限"';
    const settings = data.settings || {};
    const stats = data.stats || {};

    const cards = [
      ["安装设备", Number(stats.devices || 0)],
      ["7 日活跃", Number(stats.active_7d || 0)],
      ["累计启动", Number(stats.launches || 0)],
      ["累计时长", fmtDuration(stats.play_seconds)],
    ];

    const featureRows = (data.features || []).map((item) => `
      <label class="check-line" style="display:flex;gap:.6rem;align-items:flex-start;padding:.45rem 0">
        <input type="checkbox" data-launcher-feature="${esc(item.key)}"
               ${item.enabled ? "checked" : ""} ${locked}>
        <span>
          <b>${esc(item.name)}</b>
          <span class="hint" style="display:block">${esc(item.desc)}</span>
        </span>
      </label>`).join("");

    const releaseRows = (data.releases || []).length
      ? data.releases.map((row) => `
        <div class="admin-row">
          <div class="body">
            <span class="title">
              <b>v${esc(row.version)}</b>
              <span class="tag">${esc(row.channel)}</span>
              ${Number(row.forced) ? `<span class="tag tag--warn">强制更新</span>` : ""}
              ${Number(row.active) ? "" : `<span class="tag">已下线</span>`}
            </span>
            <span class="meta">${fmtRelative(row.created_at)}${row.url
              ? ` · ${esc(row.url)}` : " · <b>没填下载地址</b>"}</span>
            ${row.notes ? `<div class="detail">${esc(row.notes)}</div>` : ""}
          </div>
          <div class="actions">
            <button class="btn btn-danger btn-sm" data-admin-action="launcher-release-delete"
                    data-lid="${row.id}" data-version="${esc(row.version)}" ${locked}>删除</button>
          </div>
        </div>`).join("")
      : `<div class="empty"><span class="glyph">⬡</span><p>还没有发布过版本</p>
           <p class="hint">填好下面的表单，点「发布」。</p></div>`;

    const noticeRows = (data.notices || []).length
      ? data.notices.map((row) => `
        <div class="admin-row">
          <div class="body">
            <span class="title">
              <b>${esc(row.title)}</b>
              <span class="tag ${row.level === "critical" ? "tag--warn" : ""}">
                ${esc(LAUNCHER_LEVELS[row.level] || row.level)}</span>
              ${Number(row.active) ? `<span class="status-dot">上线中</span>`
                                   : `<span class="status-dot status-dot--off">已下线</span>`}
            </span>
            ${row.body ? `<div class="detail">${esc(row.body)}</div>` : ""}
            <span class="meta">${fmtRelative(row.created_at)}${row.created_by
              ? ` · ${esc(row.created_by)}` : ""}</span>
          </div>
          <div class="actions">
            <button class="btn btn-ghost btn-sm" data-admin-action="launcher-notice-toggle"
                    data-nid="${row.id}" data-on="${Number(row.active) ? "1" : "0"}" ${locked}>
              ${Number(row.active) ? "下线" : "上线"}</button>
            <button class="btn btn-danger btn-sm" data-admin-action="launcher-notice-delete"
                    data-nid="${row.id}" ${locked}>删除</button>
          </div>
        </div>`).join("")
      : `<p class="hint">没有公告。启动器上会显示「暂无公告」。</p>`;

    const clients = (data.clients && data.clients.items) || [];
    const clientRows = clients.length
      ? clients.map((row) => `
        <div class="admin-row">
          <div class="body">
            <span class="title">
              <b>${esc(String(row.device_id).slice(0, 10))}…</b>
              <span class="tag">${esc(row.os || "未知")}${row.arch ? ` / ${esc(row.arch)}` : ""}</span>
              ${row.user_name ? `<span class="tag tag--iris">@${esc(row.user_name)}</span>` : ""}
            </span>
            <span class="meta">v${esc(row.app_version || "?")}
              · 启动 ${Number(row.launch_count)} 次
              · 玩了 ${fmtDuration(row.play_seconds)}
              · 最后出现 ${fmtRelative(row.last_seen)}</span>
          </div>
        </div>`).join("")
      : `<p class="hint">还没有设备联网过。</p>`;

    view.innerHTML = `
      <div class="stack">
        <div class="stat-grid">
          ${cards.map(([label, value]) => `
            <div class="stat-card"><b>${value}</b><span>${esc(label)}</span></div>`).join("")}
        </div>

        ${settings.maintenance ? `<div class="notice notice--warn">
            <b>维护模式已开启。</b>启动器会显示维护提示，但不会阻止用户进游戏。
          </div>` : ""}
        ${data.ms_login_available ? "" : `<div class="notice notice--warn">
            <b>服务器还没有配置微软应用凭据。</b>启动器上的「微软正版登录」会显示为不可用，
            离线账号不受影响。在服务器的 <code>.env</code> 里填 <code>MCFR_MS_CLIENT_ID</code> 后重启即可。
          </div>`}

        <h2 class="section-title"><span>更新与版本</span><span class="rule"></span></h2>
        <div class="panel">
          <p class="hint">
            客户端每次启动都会问一次「最新版本是多少」。低于<b>最低可用版本</b>，
            或者当前最新版本被标成<b>强制更新</b>时，启动器会拦住用户，让他先更新。
          </p>
          <div class="row" style="gap:.6rem;flex-wrap:wrap;align-items:flex-end">
            <label class="field" style="flex:1 1 8rem"><span>版本号</span>
              <input class="input" id="launcher-release-version" placeholder="0.2.0" ${locked}></label>
            <label class="field" style="flex:0 0 7rem"><span>渠道</span>
              <select class="select" id="launcher-release-channel" ${locked}>
                <option value="stable">stable</option>
                <option value="beta">beta</option>
              </select></label>
            <label class="field" style="flex:2 1 14rem"><span>下载地址</span>
              <input class="input" id="launcher-release-url" placeholder="https://…/mcfr-launcher-0.2.0.exe" ${locked}></label>
          </div>
          <label class="field"><span>更新说明</span>
            <textarea class="input" id="launcher-release-notes" rows="3"
                      placeholder="这一版改了什么" ${locked}></textarea></label>
          <label class="check-line" style="margin:.4rem 0">
            <input type="checkbox" id="launcher-release-forced" ${locked}>
            <span>强制更新（低于这个版本的客户端会被拦住）</span>
          </label>
          <button class="btn btn-primary" data-admin-action="launcher-release-add" ${locked}>发布</button>
        </div>
        <div class="panel panel--flush">${releaseRows}</div>

        <h2 class="section-title"><span>功能模块</span><span class="rule"></span></h2>
        <div class="panel">
          <p class="hint">关掉某一项之后，启动器上对应的入口会变灰并写明「已由管理员停用」。改完记得保存。</p>
          ${featureRows || `<p class="hint">没有可开关的模块。</p>`}
          <button class="btn btn-primary" data-admin-action="launcher-save-features" ${locked}
                  style="margin-top:.6rem">保存功能开关</button>
        </div>

        <h2 class="section-title"><span>运行策略</span><span class="rule"></span></h2>
        <div class="panel">
          <label class="check-line" style="margin-bottom:.6rem">
            <input type="checkbox" id="launcher-maint" ${settings.maintenance ? "checked" : ""} ${locked}>
            <span>维护模式（启动器顶部显示提示）</span>
          </label>
          <label class="field"><span>维护 / 提示语</span>
            <textarea class="input" id="launcher-message" rows="2" ${locked}
              >${esc(settings.message || "")}</textarea></label>
          <div class="row" style="gap:.6rem;flex-wrap:wrap">
            <label class="field" style="flex:1 1 9rem"><span>最低可用版本</span>
              <input class="input" id="launcher-min-version" value="${esc(settings.min_version || "")}"
                     placeholder="留空 = 不限制" ${locked}></label>
            <label class="field" style="flex:0 0 9rem"><span>配置刷新间隔（秒）</span>
              <input class="input" id="launcher-heartbeat" type="number" min="60" max="3600"
                     value="${Number(settings.heartbeat_seconds || 600)}" ${locked}></label>
            <label class="field" style="flex:0 0 8rem"><span>默认渠道</span>
              <select class="select" id="launcher-channel" ${locked}>
                <option value="stable" ${settings.channel === "stable" ? "selected" : ""}>stable</option>
                <option value="beta" ${settings.channel === "beta" ? "selected" : ""}>beta</option>
              </select></label>
          </div>
          <button class="btn btn-primary" data-admin-action="launcher-save-settings" ${locked}>保存</button>
        </div>

        <h2 class="section-title"><span>公告</span><span class="rule"></span>
          <span class="eyebrow">最多同时显示 10 条</span></h2>
        <div class="panel">
          <div class="row" style="gap:.6rem;flex-wrap:wrap;align-items:flex-end">
            <label class="field" style="flex:2 1 14rem"><span>标题</span>
              <input class="input" id="launcher-notice-title" maxlength="80" ${locked}></label>
            <label class="field" style="flex:0 0 8rem"><span>级别</span>
              <select class="select" id="launcher-notice-level" ${locked}>
                <option value="info">普通</option>
                <option value="warn">提醒</option>
                <option value="critical">重要</option>
              </select></label>
          </div>
          <label class="field"><span>正文</span>
            <textarea class="input" id="launcher-notice-body" rows="3"
                      placeholder="纯文本，支持换行；不支持 HTML" ${locked}></textarea></label>
          <button class="btn btn-primary" data-admin-action="launcher-notice-add" ${locked}>发布公告</button>
        </div>
        <div class="panel panel--flush">${noticeRows}</div>

        <h2 class="section-title"><span>使用情况（最近 14 天）</span><span class="rule"></span></h2>
        <div class="panel">
          ${launcherDailyChart(stats.daily)}
          <div style="display:grid;gap:1rem;grid-template-columns:repeat(auto-fit,minmax(14rem,1fr));margin-top:1rem">
            ${launcherDist("客户端版本", stats.by_version, "台")}
            ${launcherDist("操作系统", stats.by_os, "台")}
          </div>
          <p class="hint" style="margin-top:.8rem">
            只统计「启动器自己」的使用情况：设备数、启动次数、游戏时长、版本分布。
            <b>不记录</b>玩家进了哪个服务器、在服务器里做了什么 ——
            那是游戏行为，不属于启动器统计的范围。
          </p>
        </div>

        <h2 class="section-title"><span>最近联网的设备</span><span class="rule"></span>
          <span class="eyebrow">共 ${Number((data.clients || {}).total || 0)} 台</span></h2>
        <div class="panel panel--flush">${clientRows}</div>

        <p class="hint">
          设备 ID 是启动器首次运行时<b>本地随机生成</b>的，不是硬件指纹。
          用户删掉启动器的配置目录就会换一个新身份，我们也没法把某个设备钉死在某个真人身上。
        </p>
      </div>`;
  }

  async function renderLauncher() {
    State.launcher = await api("/api/admin/launcher");
    drawLauncher();
  }

  /* ============================================================
     事件
     ============================================================ */

  document.addEventListener("click", async (event) => {
    const tab = event.target.closest("[data-admin-tab]");
    if (tab) {
      await setView(tab.dataset.adminTab);
      return;
    }

    const filter = event.target.closest("[data-report-filter]");
    if (filter) {
      State.reportFilter = filter.dataset.reportFilter;
      $$("[data-report-filter]").forEach((button) => {
        button.setAttribute("aria-selected", String(button.dataset.reportFilter === State.reportFilter));
      });
      const bar = filter.closest(".admin-tabs");
      const dir = tabSwap(bar, null);
      await refreshReportBody();
      tabIn($("#report-body"), dir);
      return;
    }

    const fbFilter = event.target.closest("[data-feedback-filter]");
    if (fbFilter) {
      State.feedbackFilter = fbFilter.dataset.feedbackFilter;
      const bar = fbFilter.closest(".admin-tabs");
      const dir = tabSwap(bar, null);
      await renderFeedback();
      tabIn($("#admin-view"), dir);
      return;
    }

    const contentFilter = event.target.closest("[data-content-filter]");
    if (contentFilter) {
      State.contentStatus = contentFilter.dataset.contentFilter;
      const bar = contentFilter.closest(".admin-tabs");
      const dir = tabSwap(bar, null);
      await renderContent();
      tabIn($("#admin-view"), dir);
      return;
    }

    const node = event.target.closest("[data-admin-action]");
    if (!node) return;

    const action = node.dataset.adminAction;
    const uid = node.dataset.uid;
    const rid = node.dataset.rid;
    const pid = Number(node.dataset.pid || 0);
    const fid = node.dataset.fid;
    const aid = Number(node.dataset.aid || 0);

    try {
      switch (action) {
        case "refresh":
          await setView(State.view);
          toast("已刷新", "ok");
          return;

        case "close-modal":
          // ！和主站同一个坑：.modal 是 .modal-backdrop 的子元素，
          //   点在对话框内部任何没有 data-admin-action 的地方也会一路冒到遮罩上，
          //   结果「点输入框就把对话框关了」。所以这里必须确认落点在面板之外。
          if (node.classList.contains("modal-backdrop") && event.target.closest(".modal")) {
            return;
          }
          closeModal();
          return;

        /* ---- 启动器 ---- */
        case "launcher-save-settings": {
          node.disabled = true;
          try {
            const result = await api("/api/admin/launcher/settings", {
              method: "PUT",
              body: {
                maintenance: $("#launcher-maint").checked,
                message: $("#launcher-message").value,
                min_version: $("#launcher-min-version").value.trim(),
                channel: $("#launcher-channel").value,
                heartbeat_seconds: Number($("#launcher-heartbeat").value),
              },
            });
            toast(result.message, "ok");
            await renderLauncher();
          } finally {
            node.disabled = false;
          }
          return;
        }

        case "launcher-save-features": {
          // ！逐个读复选框，而不是读某个「表单」。它们分散在同一个面板里，
          //   用 data-launcher-feature 当唯一来源，加一个模块不用回来改这里。
          const features = {};
          $$("[data-launcher-feature]").forEach((box) => {
            features[box.dataset.launcherFeature] = box.checked;
          });
          node.disabled = true;
          try {
            const result = await api("/api/admin/launcher/settings", {
              method: "PUT",
              body: { features },
            });
            toast(result.message, "ok");
            await renderLauncher();
          } finally {
            node.disabled = false;
          }
          return;
        }

        case "launcher-release-add": {
          const version = $("#launcher-release-version").value.trim();
          if (!version) {
            toast("先填版本号，例如 0.2.0", "bad");
            return;
          }
          node.disabled = true;
          try {
            const result = await api("/api/admin/launcher/releases", {
              method: "POST",
              body: {
                version,
                channel: $("#launcher-release-channel").value,
                url: $("#launcher-release-url").value.trim(),
                notes: $("#launcher-release-notes").value,
                forced: $("#launcher-release-forced").checked,
              },
            });
            toast(result.message, "ok");
            await renderLauncher();
          } finally {
            node.disabled = false;
          }
          return;
        }

        case "launcher-release-delete": {
          const lid = Number(node.dataset.lid || 0);
          if (!window.confirm(
            `确定删除版本 v${node.dataset.version || ""} 吗？\n\n` +
            "已经装了这个版本的用户不受影响，只是它不再作为「最新版本」出现。"
          )) return;
          await api(`/api/admin/launcher/releases/${lid}`, { method: "DELETE" });
          toast("已删除", "ok");
          await renderLauncher();
          return;
        }

        case "launcher-notice-add": {
          const title = $("#launcher-notice-title").value.trim();
          if (!title) {
            toast("公告得有个标题", "bad");
            return;
          }
          node.disabled = true;
          try {
            const result = await api("/api/admin/launcher/notices", {
              method: "POST",
              body: {
                title,
                body: $("#launcher-notice-body").value,
                level: $("#launcher-notice-level").value,
              },
            });
            toast(result.message, "ok");
            await renderLauncher();
          } finally {
            node.disabled = false;
          }
          return;
        }

        case "launcher-notice-toggle": {
          const nid = Number(node.dataset.nid || 0);
          await api(`/api/admin/launcher/notices/${nid}`, {
            method: "PATCH",
            body: { active: node.dataset.on !== "1" },
          });
          toast(node.dataset.on === "1" ? "已下线" : "已重新上线", "ok");
          await renderLauncher();
          return;
        }

        case "launcher-notice-delete": {
          if (!window.confirm("确定删除这条公告吗？删掉之后启动器上就看不到了。")) return;
          await api(`/api/admin/launcher/notices/${Number(node.dataset.nid || 0)}`, {
            method: "DELETE",
          });
          toast("已删除", "ok");
          await renderLauncher();
          return;
        }

        /* ---- 模组镜像 ---- */
        case "mirror-sync": {
          node.disabled = true;
          try {
            // ！不传 limit：让服务端用「自动同步」里配的那个数量。
            //   否则设了「前 5 名」却手动拉回 20 个，看起来就像设置没生效。
            const result = await api("/api/admin/mirror/sync", {
              method: "POST", body: {},
            });
            toast(result.message, "ok");
            await refreshMirror();
            startMirrorPoll();
          } finally {
            node.disabled = false;
          }
          return;
        }

        case "mirror-save-auto": {
          const enabled = $("#mirror-auto-enabled").checked;
          const hour = Number($("#mirror-auto-hour").value);
          const limit = Number($("#mirror-auto-limit").value);
          if (!Number.isFinite(limit) || limit < 1) {
            toast("「前几名」要填一个大于 0 的整数", "bad");
            return;
          }
          node.disabled = true;
          try {
            const result = await api("/api/admin/mirror/settings", {
              method: "PATCH", body: { enabled, hour, limit },
            });
            State.mirror.auto = result.auto;
            toast(result.message, "ok");
            drawMirror();
          } finally {
            node.disabled = false;
          }
          return;
        }

        case "mirror-download": {
          node.disabled = true;
          try {
            const result = await api("/api/admin/mirror/download", {
              method: "POST", body: { mod_id: "" },
            });
            toast(result.message, "ok");
            await refreshMirror();
            startMirrorPoll();
          } finally {
            node.disabled = false;
          }
          return;
        }

        case "mirror-toggle": {
          const modId = node.dataset.mid;
          const turnOn = node.dataset.on !== "1";
          node.disabled = true;
          try {
            const result = await api(`/api/admin/mirror/mods/${encodeURIComponent(modId)}`, {
              method: "PATCH",
              // 开启时把范围交给后端按默认规则重算；关闭时只改标志位
              body: turnOn ? { mirrored: true } : { mirrored: false },
            });
            toast(result.message, "ok");
            await refreshMirror();
          } finally {
            node.disabled = false;
          }
          return;
        }

        case "mirror-config":
          openMirrorConfig(node.dataset.mid);
          return;

        case "mirror-pick":
          node.setAttribute(
            "aria-pressed",
            node.getAttribute("aria-pressed") === "true" ? "false" : "true",
          );
          return;

        case "mirror-save": {
          const modId = node.dataset.mid;
          const loaders = $$("#mirror-loaders .chip[aria-pressed='true']")
            .map((chip) => chip.dataset.value);
          const versions = $$("#mirror-versions .chip[aria-pressed='true']")
            .map((chip) => chip.dataset.value);
          if (!loaders.length) {
            toast("至少要选一个加载器", "bad");
            return;
          }
          node.disabled = true;
          try {
            const result = await api(`/api/admin/mirror/mods/${encodeURIComponent(modId)}`, {
              method: "PATCH",
              body: { mirrored: true, loaders, versions },
            });
            closeModal();
            toast(result.message, "ok");
            await refreshMirror();
          } finally {
            node.disabled = false;
          }
          return;
        }

        case "change-password":
          openModal(`
            <span class="eyebrow eyebrow--iris">Password</span>
            <h2 class="display display-md" style="margin-top:.45rem">修改密码</h2>
            <p class="hint" style="margin-top:.5rem">
              ${State.me && State.me.is_super
                ? "超级管理员的新密码会写回 .env，重启之后依然有效。"
                : "改完之后，你的其它设备会立刻退出登录。"}
            </p>
            <div class="iris-rule iris-rule--fade" style="margin:1rem 0"></div>
            <div class="stack">
              <label class="field">
                <span>当前密码</span>
                <input class="input" id="pw-old" type="password" autocomplete="current-password">
              </label>
              <label class="field">
                <span>新密码</span>
                <input class="input" id="pw-new" type="password" autocomplete="new-password"
                       placeholder="至少 8 位，不要只用纯数字或纯字母">
              </label>
              <p class="error-text hidden" id="pw-error"></p>
              <div class="row-between wrap">
                <button class="btn btn-ghost" data-admin-action="close-modal">取消</button>
                <button class="btn btn-primary" data-admin-action="save-password">保存</button>
              </div>
            </div>`);
          return;

        case "save-password": {
          const errorBox = $("#pw-error");
          errorBox.classList.add("hidden");
          try {
            const result = await api("/api/admin/password", {
              method: "POST",
              body: { old_password: $("#pw-old").value, new_password: $("#pw-new").value },
            });
            closeModal();
            toast(result.message || "密码已更新", "ok");
          } catch (error) {
            errorBox.textContent = error.message;
            errorBox.classList.remove("hidden");
          }
          return;
        }

        case "logout":
          try { await api("/api/admin/logout", { method: "POST" }); } catch (error) { /* 令牌可能已失效 */ }
          token = "";
          sessionStorage.removeItem(TOKEN_KEY);
          showGate();
          toast("已退出后台", "ok");
          return;

        case "search-users":
          State.userQuery = $("#user-query").value.trim();
          State.userStatus = $("#user-status").value;
          await renderUsers();
          return;

        case "resolve-report":
          toast((await api(`/api/admin/reports/${rid}/resolve`, { method: "POST" })).message, "ok");
          await renderReports();
          return;

        case "reopen-report":
          await api(`/api/admin/reports/${rid}/reopen`, { method: "POST" });
          toast("已重新打开", "ok");
          await renderReports();
          return;

        case "suspend-user": {
          const name = node.dataset.name || "该用户";
          if (!window.confirm(`确定要暂停「${name}」吗？\n\n该用户会立即退出登录，无法再登录或发起交换申请。`)) return;
          const result = await api(`/api/admin/users/${uid}/status`, {
            method: "POST",
            body: { status: "suspended" },
          });
          toast(result.message, "ok");
          await setView(State.view);
          return;
        }

        case "activate-user": {
          const result = await api(`/api/admin/users/${uid}/status`, {
            method: "POST",
            body: { status: "active" },
          });
          toast(result.message, "ok");
          await setView(State.view);
          return;
        }

        case "fix-user-birth": {
          openBirthModal(node);
          return;
        }

        case "fix-user-birth-save": {
          const ageValue = Number($("#fix-age").value);
          const monthValue = Number($("#fix-month").value);
          const dayValue = Number($("#fix-day").value);
          const errorBox = $("#fix-birth-error");
          errorBox.classList.add("hidden");
          if (!ageValue || ageValue < 6 || ageValue > 99) {
            errorBox.textContent = "年龄请填 6-99 之间的数字";
            errorBox.classList.remove("hidden");
            return;
          }
          const label = node.dataset.name || "该用户";
          if (!window.confirm(
            `确定把「${label}」的年龄和生日改成 ${ageValue} 岁 / ${monthValue} 月 ${dayValue} 日 吗？\n\n`
            + "改完他依然不能自己修改。"
          )) return;
          try {
            const result = await api(`/api/admin/users/${uid}/birth`, {
              method: "POST",
              body: { age: ageValue, birth_month: monthValue, birth_day: dayValue },
            });
            closeModal();
            toast(result.message, "ok");
            await setView(State.view);
          } catch (error) {
            errorBox.textContent = error.message;
            errorBox.classList.remove("hidden");
          }
          return;
        }

        case "reset-user-password": {
          const name = node.dataset.name || "该用户";
          if (!window.confirm(
            `确定要给「${name}」重置密码吗？\n\n`
            + "会生成一个临时密码，你要当面或私下告诉他。\n"
            + "他所有已登录的设备会立刻退出登录。"
          )) return;
          const result = await api(`/api/admin/users/${uid}/reset-password`, { method: "POST" });
          showTempPassword(result);
          return;
        }

        case "copy-temp-password": {
          const field = $("#temp-password");
          if (!field) return;
          // select() 先选中：即使浏览器不让自动写剪贴板，
          // 管理员也能直接 Ctrl+C 把选中的内容拿走，不至于卡死在这一步。
          field.select();
          try {
            await navigator.clipboard.writeText(field.value);
            toast("已复制到剪贴板", "ok");
          } catch (error) {
            toast("浏览器不让自动复制，请按 Ctrl+C 复制选中的内容", "bad");
          }
          return;
        }

        /* ---- 系统升级页 ---- */

        case "backup-now": {
          const result = await api("/api/admin/system/backup?label=manual", { method: "POST" });
          State.system = result.system;
          toast(result.message || "已备份", "ok");
          await renderSystem();
          return;
        }

        case "download-db": {
          const name = await downloadFile("/api/admin/system/export");
          toast(`已开始下载 ${name}`, "ok");
          return;
        }

        case "download-backup": {
          const name = await downloadFile(
            `/api/admin/system/backup/${encodeURIComponent(node.dataset.name)}`
          );
          toast(`已开始下载 ${name}`, "ok");
          return;
        }

        case "import-db-open": {
          const picker = $("#db-file");
          if (picker) picker.click();
          return;
        }

        case "run-upgrade": {
          const result = await api("/api/admin/system/upgrade", { method: "POST" });
          State.system = result.system;
          toast(result.message || "已处理", "ok");
          await renderSystem();
          return;
        }

        case "restore-backup": {
          const name = node.dataset.name;
          const when = node.dataset.time || "";
          if (!window.confirm(
            `确定要还原到「${node.dataset.label || name}」这份备份吗？\n\n` +
            `备份时间：${when}\n\n` +
            "· 当前数据会先自动备份一份，随时可以再还回来\n" +
            "· 这份备份之后的注册和资料会消失\n" +
            "· 如果备份是旧版本的，程序会自动升级到当前版本"
          )) return;

          const result = await api(
            `/api/admin/system/backup/${encodeURIComponent(name)}/restore`,
            { method: "POST" }
          );
          State.system = result.system;
          toast(result.message || "已还原", "ok");
          await renderSystem();
          return;
        }

        case "delete-backup": {
          const name = node.dataset.name;
          if (!window.confirm(`确定要删除备份「${node.dataset.label || name}」吗？\n\n删掉之后就无法再用它还原了。`)) return;
          const result = await api(`/api/admin/system/backup/${encodeURIComponent(name)}`, {
            method: "DELETE",
          });
          State.system = result.system;
          toast(result.message || "已删除", "ok");
          await renderSystem();
          return;
        }

        /* ---- 帖子与评论 ---- */

        case "content-view": {
          const post = await api(`/api/admin/content/${pid}`);
          openContentModal(post);
          return;
        }

        case "content-hide":
        case "content-restore": {
          const hidden = action === "content-hide";
          if (hidden && !window.confirm("下架这篇帖子？作者会收到一条站内通知，内容对所有人隐藏，但可以再恢复。")) {
            return;
          }
          const result = await api(`/api/admin/content/${pid}/status`, {
            method: "POST",
            body: { status: hidden ? "hidden" : "published" },
          });
          toast(result.message || "已更新", "ok");
          await renderContent();
          return;
        }

        case "comment-hide":
        case "comment-restore": {
          const hidden = action === "comment-hide";
          const result = await api(`/api/admin/content/comments/${node.dataset.cid}/status`, {
            method: "POST",
            body: { status: hidden ? "hidden" : "published" },
          });
          toast(result.message || "已更新", "ok");
          await renderContent();
          return;
        }

        case "content-recount": {
          const result = await api("/api/admin/content/recount", { method: "POST" });
          toast(result.message || "已重算", "ok");
          await renderContent();
          return;
        }

        case "content-search-go": {
          // ！搜索词存在 State 里而不是只读输入框：切页签回来、或者
          //   下架一篇之后重渲染，这个条件都要还在。
          State.contentQuery = ($("#content-search") || {}).value?.trim() || "";
          await renderContent();
          return;
        }

        /* ---- 意见反馈 ---- */

        case "feedback-reply": {
          const listed = await api(`/api/admin/feedback?status=all&limit=300`);
          const item = (listed.items || []).find((row) => String(row.id) === String(fid));
          if (!item) {
            toast("这条反馈已经不在列表里了，刷新一下", "bad");
            return;
          }
          feedbackReplyModal(item);
          return;
        }

        case "feedback-save-reply": {
          const reply = $("#fb-reply").value.trim();
          await api(`/api/admin/feedback/${fid}`, {
            method: "PATCH",
            body: { reply, status: "closed" },
          });
          closeModal();
          toast("回复已保存，这条反馈也标记成已处理了", "ok");
          await renderFeedback();
          return;
        }

        case "feedback-close":
          await api(`/api/admin/feedback/${fid}`, { method: "PATCH", body: { status: "closed" } });
          toast("已标记为处理完成", "ok");
          await renderFeedback();
          return;

        case "feedback-reopen":
          await api(`/api/admin/feedback/${fid}`, { method: "PATCH", body: { status: "open" } });
          toast("已重新打开", "ok");
          await renderFeedback();
          return;

        case "feedback-delete": {
          if (!window.confirm("确定要删除这条反馈吗？删掉之后无法恢复。")) return;
          await api(`/api/admin/feedback/${fid}`, { method: "DELETE" });
          toast("已删除这条反馈", "ok");
          await renderFeedback();
          return;
        }

        /* ---- 管理员管理（超级管理员）---- */

        case "admin-new":
          State.draftPerms = {};
          openModal(adminForm(null));
          return;

        case "admin-edit": {
          const target = (State.adminItems || []).find((row) => Number(row.id) === aid);
          if (!target) {
            toast("这个账号已经不在列表里了，刷新一下", "bad");
            return;
          }
          // 把当前权限拷一份到草稿里，picker 后面就改这一份
          State.draftPerms = Object.assign({}, target.permissions || {});
          openModal(adminForm(target));
          return;
        }

        case "perm-set": {
          // 三档选择器：一组里只能选一个，所以先把同组全部按掉再点亮自己
          const area = node.dataset.area;
          const level = node.dataset.level;
          State.draftPerms = State.draftPerms || {};
          State.draftPerms[area] = level;
          $$(`.perm-levels[data-perm-area="${area}"] .perm-btn`).forEach((button) => {
            button.setAttribute("aria-pressed", String(button.dataset.level === level));
          });
          return;
        }

        case "admin-save": {
          const errorBox = $("#adm-error");
          errorBox.classList.add("hidden");

          const body = {
            display_name: $("#adm-display").value.trim(),
            note: $("#adm-note").value.trim(),
          };
          const password = $("#adm-password").value;
          if (password) body.password = password;
          if (!aid) {
            body.username = $("#adm-username").value.trim();
            body.permissions = State.draftPerms || {};
          } else if ($("#adm-perms")) {
            body.permissions = State.draftPerms || {};
          }

          try {
            const result = aid
              ? await api(`/api/admin/admins/${aid}`, { method: "PATCH", body })
              : await api("/api/admin/admins", { method: "POST", body });
            closeModal();
            toast(result.message || "已保存", "ok");
            await renderAdmins();
          } catch (error) {
            errorBox.textContent = error.message;
            errorBox.classList.remove("hidden");
          }
          return;
        }

        case "admin-disable": {
          const who = node.dataset.name || "该账号";
          if (!window.confirm(`确定要停用「${who}」吗？\n\n该账号会立刻在所有设备上退出登录，无法再进后台。`)) return;
          const result = await api(`/api/admin/admins/${aid}`, {
            method: "PATCH",
            body: { status: "disabled" },
          });
          toast(result.message || "已停用", "ok");
          await renderAdmins();
          return;
        }

        case "admin-enable": {
          const result = await api(`/api/admin/admins/${aid}`, {
            method: "PATCH",
            body: { status: "active" },
          });
          toast(result.message || "已恢复", "ok");
          await renderAdmins();
          return;
        }

        case "admin-delete": {
          const who = node.dataset.name || "该账号";
          if (!window.confirm(
            `确定要删除管理员「${who}」吗？\n\n账号会被彻底删除，他手上的登录立即失效。\n（他的操作记录会留在反馈 / 举报的处理人字段里）`
          )) return;
          const result = await api(`/api/admin/admins/${aid}`, { method: "DELETE" });
          toast(result.message || "已删除", "ok");
          await renderAdmins();
          return;
        }

        /* ---- v0.32 · 登录密钥 ---- */

        case "key-new":
          openKeyNewModal(node.dataset.haskeys === "1");
          return;

        case "key-delete":
          openKeyDeleteModal(node.dataset.key, node.dataset.label);
          return;

        case "key-password":
          openKeyPasswordModal(node.dataset.next === "1");
          return;

        default:
          // 排到这里说明按钮上的 data-admin-action 与 case 名对不上。
          // 症状是「点了完全没反应」但控制台干干净净 —— 极难排查，所以主动喊一声。
          console.warn("[mcfr] 后台没有对应的处理分支：", action, node);
          return;
      }
    } catch (error) {
      toast(error.message, "bad");
    }
  });

  // 数据库文件选择框（在「系统升级」页里动态生成，所以用事件委派）
  document.addEventListener("change", async (event) => {
    const input = event.target;
    if (!input || input.id !== "db-file") return;

    const file = input.files && input.files[0];
    input.value = "";   // 清空，方便连续选同一个文件
    if (!file) return;

    try {
      const result = await importDatabaseFile(file);
      toast(result.message || "导入成功", "ok");
      await renderSystem();
    } catch (error) {
      toast(error.message, "bad");
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && $("#overlay-root").innerHTML) {
      closeModal();
      return;
    }
    if (event.key !== "Enter") return;
    if (event.target.id !== "user-query") return;
    $("#admin-view") && document.querySelector("[data-admin-action='search-users']")?.click();
  });

  /* ============================================================
     启动
     ============================================================ */

  async function boot() {
    if (window.McfrEffects) window.McfrEffects.start();
    bindLogin();
    bindLoginModes();
    bindKeyLogin();

    if (token) {
      try {
        // 用 /me 而不是 /overview：进后台第一件事是搞清楚「我是谁、能看什么」，
        // 页签要先按权限画出来，再去渲染第一个页签的内容。
        const session = await api("/api/admin/me");
        applySession(session);
        showApp();
        await setView(State.tabs[0] || "overview");
        return;
      } catch (error) {
        token = "";
        sessionStorage.removeItem(TOKEN_KEY);
      }
    }

    showGate();
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
