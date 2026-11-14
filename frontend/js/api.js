/* =========================================================================
   与后端 API 通信的薄封装：统一注入登录令牌、统一抛出可读的中文错误。

   v0.2 社交模型：不做站内私信，改为「联系方式受控交换」。
   ========================================================================= */

const TOKEN_STORAGE_KEY = "mcfr.token";

/* 「下次还要不要重新输密码」的答案，记在这台设备上（v0.30）。
   "1" = 保持登录，"0" = 不保持，null = 这台设备还没问过。 */
const KEEP_STORAGE_KEY = "mcfr.keep";

/* 令牌可能躺在两个地方，取的时候两边都要看：
     localStorage   —— 选了「保持登录」，关掉浏览器也还在；
     sessionStorage —— 选了「不保持」，关掉标签页就没了。
   只读 localStorage 会让选「不保持」的人一刷新就被踢回登录页。 */
function readStoredToken() {
  return localStorage.getItem(TOKEN_STORAGE_KEY)
    || sessionStorage.getItem(TOKEN_STORAGE_KEY)
    || "";
}

/* 这台设备之前回答过「保持登录」没有。
   ！老账号（v0.29 及以前）的令牌本来就在 localStorage 里，那时还没有这一问。
     把这种情况当成「保持登录」—— 否则升级一次，所有还登录着的人全被踢下线。 */
function readStoredKeep() {
  const saved = localStorage.getItem(KEEP_STORAGE_KEY);
  if (saved === "1" || saved === "0") return saved;
  return localStorage.getItem(TOKEN_STORAGE_KEY) ? "1" : null;
}

const Store = {
  TOKEN_KEY: TOKEN_STORAGE_KEY,
  KEEP_KEY: KEEP_STORAGE_KEY,

  token: readStoredToken(),
  /* "1" / "0" / null —— null 表示这台设备还没问过 */
  keep: readStoredKeep(),
  user: null,
  meta: null,

  /**
   * 存令牌。keep 省略时沿用这台设备上次的选择。
   *
   * ！两个位置必须都先清干净再写一个。不然会出现「localStorage 里留着上一次的、
   *   sessionStorage 里是这一次的」两份令牌，而登出 / 401 只会各清一次 ——
   *   漏掉的那份还在，表现就是「明明登出了，刷新一下又进去了」。
   */
  setToken(value, keep) {
    this.token = value || "";
    if (keep !== undefined) this.keep = keep ? "1" : "0";

    localStorage.removeItem(TOKEN_STORAGE_KEY);
    sessionStorage.removeItem(TOKEN_STORAGE_KEY);
    if (!this.token) return;

    (this.keep === "1" ? localStorage : sessionStorage).setItem(TOKEN_STORAGE_KEY, this.token);
    if (this.keep !== null) localStorage.setItem(KEEP_STORAGE_KEY, this.keep);
  },

  /**
   * 忘掉这台设备的选择。登出 / 注销时调 —— 那之后相当于「没在这台设备上登录过」，
   * 下次登录该重新问一遂。
   *
   * ！共用电脑上这一条尤其重要：上一个人选了「保持登录」，不等于下一个人也选。
   *   不重置的话，下一个人登录时会静默沿用上一个人的答案。
   */
  forgetDevice() {
    this.setToken("");
    this.keep = null;
    localStorage.removeItem(KEEP_STORAGE_KEY);
  },
};

/** 未登录回调，由 app.js 挂载，避免这里直接依赖界面。 */
let onUnauthorized = null;

/** 需要先填年龄和生日时的回调，同样由 app.js 挂载。 */
let onNeedsBirth = null;

async function api(path, options = {}) {
  const { method = "GET", body, skipAuth = false } = options;
  const headers = {};

  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  if (!skipAuth && Store.token) {
    headers["Authorization"] = `Bearer ${Store.token}`;
  }

  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (networkError) {
    throw new Error("网络连接失败，请确认后端服务已启动");
  }

  let data = null;
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    try {
      data = await response.json();
    } catch (parseError) {
      data = null;
    }
  }

  if (response.status === 401 && !skipAuth) {
    Store.setToken("");
    Store.user = null;
    if (typeof onUnauthorized === "function") onUnauthorized();
    throw new Error((data && data.detail) || "登录状态已过期，请重新登录");
  }

  // 428 =「先把年龄和生日填了」。后端在 get_current_user 里统一拦，
  // 前端只需要在任何一个请求撞上它时把补填弹窗弹出来 ——
  // 不用在每个调用点各写一遍判断（写了迟早会漏一个）。
  if (response.status === 428 && typeof onNeedsBirth === "function") {
    onNeedsBirth();
  }

  if (!response.ok) {
    const detail = data && data.detail;
    const message = typeof detail === "string" ? detail : "操作失败，请稍后重试";
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }

  return data;
}

/**
 * 上传一个二进制 Blob（头像是 canvas 生成的 PNG，皮肤是用户选的文件）。
 *
 * 为什么不用 FormData：后端就等着收一个原生 PNG 请求体，
 * 这样服务端不需要 python-multipart，少一个依赖。
 */
async function apiUpload(path, blob, extraHeaders = null) {
  let response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: {
        "Content-Type": "image/png",
        ...(Store.token ? { Authorization: `Bearer ${Store.token}` } : {}),
        ...(extraHeaders || {}),
      },
      body: blob,
    });
  } catch (networkError) {
    throw new Error("网络连接失败，请确认后端服务已启动");
  }

  let data = null;
  if ((response.headers.get("content-type") || "").includes("application/json")) {
    try {
      data = await response.json();
    } catch (parseError) {
      data = null;
    }
  }

  if (response.status === 401) {
    Store.setToken("");
    Store.user = null;
    if (typeof onUnauthorized === "function") onUnauthorized();
    throw new Error((data && data.detail) || "登录状态已过期，请重新登录");
  }

  if (!response.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : "上传失败，请稍后重试");
  }

  return data;
}

const Api = {
  meta: () => api("/api/meta", { skipAuth: true }),

  /* ---- 认证 ---- */
  register: (payload) => api("/api/auth/register", { method: "POST", body: payload, skipAuth: true }),
  /* 人机验证的答案随登录 / 注册一起提交，所以这两个函数多收一个 captcha 参数；
     keep 是「在这台设备上保持登录」，服务端靠它决定令牌活 14 天还是 12 小时。 */
  login: (account, password, captcha, keep) =>
    api("/api/auth/login", {
      method: "POST",
      body: { account, password, keep: Boolean(keep), ...captcha },
      skipAuth: true,
    }),
  changePassword: (oldPassword, newPassword) =>
    api("/api/auth/password", {
      method: "POST",
      body: { old_password: oldPassword, new_password: newPassword },
    }),
  /* 领一张图片验证码。返回 { id, image, expires_in } —— image 是一段 data: URI */
  captcha: (purpose) =>
    api(`/api/captcha?purpose=${encodeURIComponent(purpose)}`, { skipAuth: true }),
  logout: () => api("/api/auth/logout", { method: "POST" }),
  /* 「在这台设备上保持登录」的开关。改的是当前这一个会话的有效期。 */
  setSessionKeep: (keep) => api("/api/auth/session/keep", { method: "POST", body: { keep: Boolean(keep) } }),
  deleteAccount: () => api("/api/auth/account", { method: "DELETE" }),

  overview: () => api("/api/users/me/overview"),
  updateProfile: (payload) => api("/api/users/me", { method: "PUT", body: payload }),
  // 年龄和生日：只写一次。已经填过的账号调它会被后端拒掉（409）。
  setBirth: (payload) => api("/api/users/me/birth", { method: "POST", body: payload }),
  updatePrivacy: (payload) => api("/api/users/me/privacy", { method: "PATCH", body: payload }),
  viewUser: (id) => api(`/api/users/${id}`),

  discover: (limit = 12) => api(`/api/discover?limit=${limit}`),
  searchUsers: (query) => api(`/api/discover/search?q=${encodeURIComponent(query)}`),
  skipUser: (id) => api(`/api/discover/${id}/skip`, { method: "POST" }),

  /* 模组镜像（索引来自 Modrinth，由服务端同步）*/
  mirrorOverview: () => api("/api/mirror/overview"),
  mirrorMods: () => api("/api/mirror/mods"),
  mirrorMod: (id) => api(`/api/mirror/mods/${encodeURIComponent(id)}`),

  /* 已交换联系方式的同好 */
  connections: () => api("/api/connections"),
  removeConnection: (id) => api(`/api/connections/${id}`, { method: "DELETE" }),

  /* 联系方式交换申请 */
  requests: (box = "incoming") => api(`/api/contact-requests?box=${box}`),
  pendingRequests: () => api("/api/contact-requests/pending"),
  sendRequest: (targetId, greeting) =>
    api(`/api/contact-requests/${targetId}`, { method: "POST", body: { greeting } }),
  acceptRequest: (id) => api(`/api/contact-requests/${id}/accept`, { method: "POST" }),
  rejectRequest: (id) => api(`/api/contact-requests/${id}/reject`, { method: "POST" }),

  blocks: () => api("/api/blocks"),
  block: (targetId) => api("/api/blocks", { method: "POST", body: { target_user: targetId } }),
  unblock: (targetId) => api(`/api/blocks/${targetId}`, { method: "DELETE" }),

  reportReasons: () => api("/api/reports/reasons"),
  report: (payload) => api("/api/reports", { method: "POST", body: payload }),

  /* 形象：上传 / 移除头像与皮肤（都是原生 PNG 请求体）*/
  uploadAvatar: (blob) => apiUpload("/api/users/me/avatar", blob),
  removeAvatar: () => api("/api/users/me/avatar", { method: "DELETE" }),
  uploadSkin: (blob) => apiUpload("/api/users/me/skin", blob),
  removeSkin: () => api("/api/users/me/skin", { method: "DELETE" }),

  /* ---- 意见反馈（v0.13）---- */
  sendFeedback: (payload) => api("/api/feedback", { method: "POST", body: payload }),
  myFeedback: () => api("/api/feedback/mine"),

  /* ---- 帖子（v0.27）----
     列表接口用 URLSearchParams 拼查询串，而不是手写 `?q=${q}` ——
     搜索词是用户输入，直接拼进去的话一个 `&` 或 `#` 就能把后面的参数吃掉。 */
  posts: (params = {}) => api(`/api/posts?${new URLSearchParams(params)}`),
  myPosts: (page = 1, q = "") =>
    api(`/api/posts/mine?${new URLSearchParams({ page, q })}`),
  post: (id) => api(`/api/posts/${id}`),
  createPost: (payload) => api("/api/posts", { method: "POST", body: payload }),
  updatePost: (id, payload) => api(`/api/posts/${id}`, { method: "PATCH", body: payload }),
  deletePost: (id) => api(`/api/posts/${id}`, { method: "DELETE" }),
  likePost: (id) => api(`/api/posts/${id}/like`, { method: "POST" }),

  comments: (id, page = 1) => api(`/api/posts/${id}/comments?page=${page}`),
  addComment: (id, content) =>
    api(`/api/posts/${id}/comments`, { method: "POST", body: { content } }),
  deleteComment: (id, commentId) =>
    api(`/api/posts/${id}/comments/${commentId}`, { method: "DELETE" }),

  /* 制作人员与转发对象共用同一个搜索接口：两边要找的都是「站内的人」 */
  postUsers: (q = "") => api(`/api/posts/users?q=${encodeURIComponent(q)}`),
  shareTargets: (id, q = "") =>
    api(`/api/posts/${id}/share-targets?q=${encodeURIComponent(q)}`),
  sharePost: (id, userIds, note) =>
    api(`/api/posts/${id}/share`, {
      method: "POST",
      body: { user_ids: userIds, note },
    }),

  /* 配图和头像走同一条管线：原始 PNG 请求体，不经 multipart 表单 */
  uploadPostImage: (blob) => apiUpload("/api/posts/media", blob),

  /* 附件（v0.28）。文件名走 X-Filename 自定义头，而且要先做
     encodeURIComponent —— HTTP 头只能放 ASCII，中文文件名直接放进去
     会被浏览器拒掉。服务端那边会 unquote 回来。
     这和后台导入数据库文件用的是同一套写法。 */
  uploadAttachment: (file) =>
    apiUpload("/api/posts/attachment", file, {
      "Content-Type": "application/octet-stream",
      "X-Filename": encodeURIComponent(file.name || "attachment"),
    }),

  /* ---- 圈子（v0.34）----
     圈子帖就是普通的帖子，只是多带一个 circle_id 并且只有成员可见；
     所以「圈子里的帖子」走的是 /api/circles/{id}/posts，不是 /api/posts。 */
  circles: (params = {}) => api(`/api/circles?${new URLSearchParams(params)}`),
  circle: (id) => api(`/api/circles/${id}`),
  createCircle: (payload) => api("/api/circles", { method: "POST", body: payload }),
  joinCircle: (id) => api(`/api/circles/${id}/join`, { method: "POST" }),
  /* 退出 = 界面上那句「删除」。返回 {dissolved, successor, post_count} ——
     前端要能说清「圈子解散了没有」，所以不能用「POST 完就当成功了」的处理。 */
  leaveCircle: (id) => api(`/api/circles/${id}/leave`, { method: "POST" }),
  circlePosts: (id, params = {}) =>
    api(`/api/circles/${id}/posts?${new URLSearchParams(params)}`),
  circleMembers: (id, limit = 30) => api(`/api/circles/${id}/members?limit=${limit}`),

  /* ---- 系统 ---- */
  /* 健康检查。侧栏那盏「服务器在线」的灯用的就是它：
     既要知道能不能连上，也要拿一次往返时间 —— 所以不缓存、每次都真打。 */
  health: () => api("/api/health", { skipAuth: true }),

  /* ---- 信息中心（v0.27）---- */
  notifications: (box = "all", page = 1) =>
    api(`/api/notifications?box=${encodeURIComponent(box)}&page=${page}`),
  readNotifications: (payload) =>
    api("/api/notifications/read", { method: "POST", body: payload }),
};
