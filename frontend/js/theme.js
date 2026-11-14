/* =========================================================================
   mcfr · 主题控制
   -------------------------------------------------------------------------
   四态：跟随系统 / 浅色 / 深色 / 跟随时间。

   为什么是「四态」，而不是「只跟系统」或「一个开关」：
     · 只跟系统 —— 系统设成深色的人不一定想在这个站点上看深色，
       尤其是这个站点有大量自定义配色的方块元素；
     · 只给两态开关 —— 那就永远回不到「跟随系统」了。
       用户在系统里改了设置，站点却还停在他很久以前手动选的那一档。
     · 「跟随时间」（v0.35）是第三种「由外面决定」的口径：白天浅色、
       夜里深色，而且夜里的深浅还会跟着钟表继续走。

   实现上的三个要点：

   1. **属性放在 <html> 上（data-theme），而且只存解析后的结果**
      （light / dark）。「跟随系统 / 跟随时间」这两个**偏好**存在
      localStorage 里，不参与 CSS。于是 CSS 那边只需要一套
      html[data-theme="dark"] 覆盖块，不必再写一份
      @media (prefers-color-scheme: dark) 的副本 ——
      两份真值放在两个地方，迟早会不同步，而且是静默的那种。

   2. **首屏不能闪。** <head> 里的 theme-boot.js 在任何 CSS 生效之前就把
      data-theme 写好了。这个文件是普通的同步脚本，跑到时首屏已经画完了，
      只靠它的话，深色系统的用户会先被闪一下白 —— 那一下非常刺眼。

   3. **系统设置变了要跟上，但只在自己处于「跟随系统」时。**
      用户手动选了浅色，系统后来切到深色，站点不该跟着变。
      这就是下面 onSystemChange 里那个 if 存在的全部理由。

   页面侧只需要两件事：
     · <head> 里放 theme-boot.js（防闪烁）；
     · 在任意位置放 <button data-theme-toggle></button>，内容由这里填。
   ========================================================================= */

(() => {
  "use strict";

  const KEY = "mcfr.theme";
  /* ！顺序就是按钮的循环顺序。新档位一律往后加，
     不然所有人习惯了的「点一下变浅色、再点变深色」会被打乱。 */
  const PREFERRED = ["system", "light", "dark", "time"];

  /* 按钮上的图标。用几何符号而不是 emoji：emoji 在 Windows / Android / iOS
     上是三套完全不同的画风，而且自带颜色，放进这个「只有绿 + 中性色」的
     界面里会非常跳。
     time 用 ◔（圆里一个扇形）—— 一个钟面，而且和 ◐（跟随系统）不像到会认错。 */
  const GLYPH = { system: "◐", light: "☀", dark: "☾", time: "◔" };
  const LABEL = { system: "跟随系统", light: "浅色", dark: "深色", time: "跟随时间" };

  /* 移动端浏览器地址栏 / 状态栏的配色，要和 --void 保持一致，
     否则深色主题下页面上方会横着一条亮色的系统栏。 */
  const CHROME = { light: "#eef1ee", dark: "#14181c" };

  /* 老浏览器兜底：没有 matchMedia 就退化成「永远是浅色」，手动切换照常可用。
     宁可少一个「跟随系统」，也不要让整个脚本在顶层就抛异常 ——
     那样连切换按钮都是空的，用户连手动选的退路都没有。 */
  const media = typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-color-scheme: dark)")
    : { matches: false, addEventListener: null, addListener: null };

  const calm = typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-reduced-motion: reduce)")
    : null;

  function reduced() {
    return Boolean(calm && calm.matches);
  }

  function readPref() {
    try {
      const raw = window.localStorage.getItem(KEY);
      return PREFERRED.indexOf(raw) >= 0 ? raw : "system";
    } catch (err) {
      /* 隐私模式、或者把站点数据禁掉之后，localStorage 会直接抛异常。
         这里必须兜住 —— 否则一个「读不到主题偏好」会把整页脚本带崩，
         而主题本身只是件锦上添花的事，不该有这个权限。 */
      return "system";
    }
  }

  function writePref(next) {
    try { window.localStorage.setItem(KEY, next); } catch (err) { /* 存不下就算了 */ }
  }

  /* 现在是夜里吗。「跟随时间」这一档的白天/黑夜由 daylight.js 说了算，
     它同时也是那层天光的主人 —— 两处用同一个判断，才不会出现
     「主题已经黑了，天空还亮着」这种半截状态。 */
  function isNight() {
    const api = window.McfrDaylight;
    if (api && typeof api.isNight === "function") return api.isNight();
    /* 兜底：daylight.js 没被加载（比如管理后台只引了 theme.js）。
       ！这里的 6 / 18 必须和 daylight.js 的 SUNRISE / SUNSET 一致。 */
    const now = new Date();
    const hour = now.getHours() + now.getMinutes() / 60;
    return hour < 6 || hour >= 18;
  }

  function resolve(value) {
    if (value === "system") return media.matches ? "dark" : "light";
    if (value === "time") return isNight() ? "dark" : "light";
    return value;
  }

  let pref = readPref();

  /* 把所有 data-theme-toggle 按钮画成当前状态 */
  function renderButtons() {
    const index = PREFERRED.indexOf(pref);
    const next = PREFERRED[(index + 1) % PREFERRED.length];

    for (const btn of document.querySelectorAll("[data-theme-toggle]")) {
      btn.innerHTML =
        '<span class="theme-toggle__icon" aria-hidden="true">' + GLYPH[pref] + "</span>" +
        '<span class="theme-toggle__text">' + LABEL[pref] + "</span>";
      btn.setAttribute("aria-label", "主题：" + LABEL[pref] + "，点击切换到" + LABEL[next]);
      btn.setAttribute("title", "主题：" + LABEL[pref] + " · 点击切换到" + LABEL[next]);
      // 这两句是拼出来的，字典里按整句的模式条目翻（"主题：{}，点击切换到{}"）。
      // ！每次重画都要重新过一遍：不跑的话，切完主题按钮的提示就变回中文。
      if (window.McfrI18n) window.McfrI18n.applyDynamic(btn);
    }
  }

  /* ---- 换色时的那一下淡变 ----
     整套令牌是瞬间替换的，白底变深板岩灰会「啪」一下。真赶上黄昏那个点
     从浅色翻到深色，这一下特别扎眼。

     ！为什么不把 transition 直接写进样式表：那会让页面上每一次 hover、
       每一次换底色都变慢 1.2 秒。所以只在**整页配色要换掉**的那一刻
       给 <html> 挂一个类，一秒后摘掉（样式见 aether.css 的 .is-shading）。 */
  let shadeTimer = 0;
  function shade() {
    if (reduced()) return;          // 「减少动态效果」的人不该被拉丝
    const node = document.documentElement;
    node.classList.add("is-shading");
    window.clearTimeout(shadeTimer);
    shadeTimer = window.setTimeout(() => node.classList.remove("is-shading"), 1200);
  }

  function paint() {
    const mode = resolve(pref);
    const node = document.documentElement;
    if (node.dataset.theme !== mode) {
      shade();
      node.dataset.theme = mode;
    }

    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", CHROME[mode]);

    /* 只有「跟随时间」这一档才点亮天光。手动选了浅色/深色的人，
       不该看着页面自己慢慢变色 —— 那是他们明确选过的。 */
    if (window.McfrDaylight) window.McfrDaylight.setActive(pref === "time");

    renderButtons();
  }

  /* 事件委托：按钮可能在任意时刻被换掉（比如后台登录后才显示顶栏），
     绑在 document 上就不必关心它什么时候出现。 */
  document.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-theme-toggle]");
    if (!btn) return;
    pref = PREFERRED[(PREFERRED.indexOf(pref) + 1) % PREFERRED.length];
    writePref(pref);
    paint();
  });

  function onSystemChange() {
    /* 只有「跟随系统」这一档才跟着变。手动选过浅色/深色的人，
       是明确表达过偏好的，不该被系统设置覆盖掉。 */
    if (pref === "system") paint();
  }

  if (media.addEventListener) media.addEventListener("change", onSystemChange);
  else if (media.addListener) media.addListener(onSystemChange);   // 老 Safari

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", paint);
  } else {
    paint();
  }

  /* 暴露出来主要是为了排查问题：在控制台里 McfrTheme.set("dark") 就能验色；
     McfrDaylight.preview(19.2) 能把时间钉在黄昏那一刻看天光。 */
  window.McfrTheme = {
    get pref() { return pref; },
    get mode() { return resolve(pref); },
    modes: PREFERRED.slice(),
    /** 不改偏好、只重算一遍。daylight.js 在白天/黑夜翻面时会调它。 */
    refresh: paint,
    set(next) {
      if (PREFERRED.indexOf(next) < 0) return;
      pref = next;
      writePref(pref);
      paint();
    },
  };
})();
