/* =========================================================================
   mcfr · 主题防闪烁
   -------------------------------------------------------------------------
   这段代码在 <head> 里**同步**执行：任何样式生效之前就把 <html> 的
   data-theme 定下来。属性一变，aether.css 里 html[data-theme="dark"]
   那套令牌立刻生效，所以首屏画出来就已经是对的颜色。

   为什么不能放进 js/theme.js：
     theme.js 是位于 body 末尾的普通脚本，跑到时首屏已经用浅色画完了 ——
     深色系统的用户会先看到一下白闪，再整页刷地变黑。那一下非常刺眼。

   为什么不做成 <head> 里的内联脚本（更省一次请求）：
     内容安全策略里 script-src 只放行 'self'，内联脚本会被直接拦掉；
     要放行就必须开 'unsafe-inline'，那等于把 CSP 对 XSS 的防护整个丢掉。
     多一次本地小文件的请求（几百字节），比放宽全站的脚本策略便宜得多。

   ！这段的判断逻辑必须和 theme.js 里的 resolve() 保持一致，
     两处不一致就是「首屏用 A 色画、下一帧改成 B 色」。
   ========================================================================= */

(function () {
  "use strict";

  try {
    var pref = window.localStorage.getItem("mcfr.theme");
    var dark = pref === "dark" ||
      (pref !== "light" && window.matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  } catch (err) {
    /* 隐私模式下读 localStorage 会抛异常。这里绝不能让它冒出去：
       后面的 <head> 还有样式表和 body 要解析，不能被这一行带停。
       保持默认的浅色即可，theme.js 起来之后会兜底。 */
  }
})();
