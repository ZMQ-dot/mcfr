/* mcfr · 前端多语言机制
   ============================================================================
   零依赖、零构建 —— 这个项目不引 i18n 库，也不做打包，所以引擎得自己写。
   总共就这几件事：探测语言、查表、把静态页面上的文字换掉、记住选择。

   ## 为什么用「中文原文」当 key（而不是 welcome_back 这类短名）

   这是 gettext 用了三十年的办法：**源语言字符串本身就是 key**。

       t("欢迎回来")            →  "Welcome back" / "おかえりなさい"
       t("欢迎回来，{name}", {name: "小明"})

   理由有三条，都跟这个项目的实际情况有关：

     ① **简体中文是原文，字典是空的。** 用短名的话，得先为 600 多条文案
        各自起一个名字，再写一份中文对照 —— 纯属凭空多出来一道工序，
        而且以后改一个字就要同时改 key、中文、三种译文，四处。

     ② **漏翻不会露馅。** 查不到就当没翻，原样显示中文。
        短名方案一旦漏了一条，用户看到的是 `badge.unknown` 这种东西。

     ③ **改文案时能一眼看出影响面。** grep 中文原文就能找到所有引用点。

   代价也要说清楚：
     · 改了中文原文 = 那种语言的译文立刻失效（会退回中文，不会崩）。
       所以有 check_i18n.py 专门盯着这件事。
     · key 比较长，代码里看着啰嗦一点。

   ## 怎么用

   静态 HTML（属性名后面的值就是 key，也就是简体原文）：

       <button data-i18n="登录">登录</button>
       <input data-i18n-placeholder="用户名或自己填过的邮箱">
       <button data-i18n-title="切换主题" data-i18n-aria-label="切换主题">

   JS：

       t("确定要暂停「{name}」吗？", { name })

   带 HTML 的串（要保留 <b> 之类）用 tHTML，注意译文里的标签由译者保证一致。

   ## 语言代码

       zh       简体中文（原文，默认）
       zh-Hant  繁體中文
       en       English
       ja       日本語
   ========================================================================= */

(function () {
  "use strict";

  const LANGS = ["zh", "zh-Hant", "en", "ja"];

  /* 界面上给用户看的名字。！用该语言自己的写法（endonym），
     不要写「英语」「日语」—— 一个只会日语的人看不懂「日语」这两个汉字。 */
  const LABELS = {
    zh: "简体中文",
    "zh-Hant": "繁體中文",
    en: "English",
    ja: "日本語",
  };

  /* 窄屏用的短标签。四个全称加起来 219px，在 390px 宽的手机上会把整个
     顶栏撑爆（用户胶囊被挤出屏幕外，见 mobile.css 里那段说明）。
     ！短标签只进 .short 那个 span，而且带 aria-hidden —— 读屏读到的仍是
       LABELS 里的全称，不会因为屏幕窄就只读到「中文」这种省略说法。 */
  const SHORT = {
    zh: "中文",
    "zh-Hant": "繁中",
    en: "EN",
    ja: "日本語",
  };

  const STORAGE_KEY = "mcfr.lang";

  /* 字典：{ 语言: { 中文原文: 译文 } }。zh 永远是空的 —— 它就是原文。 */
  const dicts = { zh: {} };

  /* 把浏览器给的语言标签归到我们支持的四档。
     ！这里的顺序有讲究：先判 zh-Hant 相关，再判 zh ——
       "zh-TW" 两个条件都沾边，先判宽的会把繁体错认成简体。 */
  function normalize(tag) {
    const raw = String(tag || "").trim();
    if (!raw) return "";
    const value = raw.toLowerCase().replace("_", "-");

    if (value.startsWith("ja")) return "ja";
    if (value.startsWith("zh")) {
      if (/hant|tw|hk|mo/.test(value)) return "zh-Hant";
      if (/hans|cn|sg|my/.test(value)) return "zh";
      // 只说 zh 没带地区：按大陆习惯算简体
      return "zh";
    }
    if (value.startsWith("en")) return "en";
    return "";
  }

  /* 从浏览器偏好里挑一个我们支持的。
     ！只在「本机没存过选择」时用 —— 存过就以用户的选择为准。 */
  function detect() {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && LANGS.includes(stored)) return stored;

    const list = navigator.languages && navigator.languages.length
      ? navigator.languages
      : [navigator.language];
    for (const tag of list) {
      const hit = normalize(tag);
      if (hit) return hit;
    }
    // 都不认识就退回英文：会来看这个站的日语/中文用户，
    // 浏览器标的多半就是 ja / zh，走到这一行的基本是其它语种。
    return "en";
  }

  let current = "zh";

  function lookup(text) {
    const table = dicts[current];
    if (!table) return text;
    const hit = table[text];
    return typeof hit === "string" && hit ? hit : text;
  }

  /* 把 {name} 这类占位符换掉。
     ！译文里的占位符由译者保证同名 —— 换不到就原样留着，
       方便一眼看出是哪条翻译写错了。 */
  function fill(text, params) {
    if (!params) return text;
    return text.replace(/\{(\w+)\}/g, (whole, key) =>
      Object.prototype.hasOwnProperty.call(params, key) ? String(params[key]) : whole
    );
  }

  /* 主入口。t("已复制")、t("欢迎回来，{name}", { name }) */
  function t(text, params) {
    if (text === undefined || text === null) return "";
    // ！非字符串直接放行：调用方偶尔会传数字或已经拼好的片段，
    //   在这里抛异常等于为了一个文案把整个渲染打断。
    if (typeof text !== "string") return text;
    return fill(lookup(text), params);
  }

  /* ---------------------------------------------------------------- 静态页面 */

  /* 扫描 [data-i18n] 系列属性并替换。
     ！属性名后面的**值**就是 key（也就是简体原文），例如 data-i18n="登录"。
       这样 HTML 默认显示的就是简体，JS 没跑起来也完全正常 ——
       不会出现「脚本挂了整页空白」那种情况。

     ！必须能反复调用（切语言要重跑）。所以每一项都从 data 属性重新取 key，
       而不是从上一次改完的文字里取 —— 后者换回中文的时候就找不回来了。 */
  const ATTR_MAP = {
    "data-i18n": null,                       // 替换文字内容（textContent）
    "data-i18n-placeholder": "placeholder",
    "data-i18n-title": "title",
    "data-i18n-aria-label": "aria-label",
    "data-i18n-value": "value",
    "data-i18n-alt": "alt",
  };

  /* ！为什么要单独有一个 innerHTML 版本：
     首页那个大标题把「造世界」三个字包在 <span class="iris-text"> 里做成渐变，
     直接 textContent 会把渐变一起抹掉。
     ！用它的地方**译文必须带同样的标签**（check_i18n.py 会查这个），
       否则页面会渲染出一段没有样式的裸文字。
     ！绝不把用户产生的内容放进 data-i18n-html 的 key —— 那是固定的文案才走的路。 */
  function applyHTML(root) {
    const scope = root || document;
    for (const node of scope.querySelectorAll("[data-i18n-html]")) {
      const key = node.getAttribute("data-i18n-html");
      if (key) node.innerHTML = t(key);
    }
  }

  function applyStatic(root) {
    const scope = root || document;
    for (const [attr, target] of Object.entries(ATTR_MAP)) {
      const nodes = scope.querySelectorAll("[" + attr + "]");
      for (const node of nodes) {
        const key = node.getAttribute(attr);
        if (!key) continue;
        const text = t(key);
        if (target === null) node.textContent = text;
        else node.setAttribute(target, text);
      }
    }
    applyHTML(scope);
  }

  /* ------------------------------------------------------- 动态内容（主站） */

  /* 静态页面用 data-i18n 接键；**登录之后的界面不接键**，改用这里。

     为什么两套做法：

     静态页面的文案在 HTML 里写得清清楚楚，标一遍属性最直观，也最省事。
     但 app.js 是用模板字符串拼 HTML 的 —— 一个卡片模板里嵌七八句话，
     每句都包一层 t() 之后长这样：

         `<b>${t("邀请你交换联系方式")}</b><p>${t("同意后你会收到 TA 的…")}</p>`

     渲染代码本身就没法读了，而且新加一句话很容易忘了包。所以主站这边反过来：
     **先照常渲染，再统一把 DOM 里的文字过一遍字典。**

     两套走的是同一个字典、同一套 key（还是简体原文），所以不存在
     「两处要同步维护」的问题。

     这个做法的三个前提，写清楚免得以后踩：
       · 一句话在 DOM 里必须**整段出现**，不能跨元素拼（跨元素本来也没法翻）。
       · 带变量的句子（"已复制：abc"）用带 {} 的条目前缀匹配，见 PATTERN。
       · 必须是幂等的 —— 每次渲染都是全新的 DOM，翻完不会累积。
         这一点天然成立：渲染重新生成简体，再过一遍字典即可。

     ！跳过 SCRIPT/STYLE/CODE：里面有中文注释和真实代码，
       翻掉它们会把页面搞坏（虽然一般也翻不着）。 */
  const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "CODE", "PRE"]);

  /* 会被翻译的属性。textContent 之外，用户能看到的文字基本就这几个。 */
  const DYN_ATTRS = ["placeholder", "title", "aria-label", "alt"];

  /* 带 {} 的条目按通配符匹配："已复制：{}" 能翻 "已复制：abc123"。
     ！只在精确查表落空、且文本里确实有汉字时才试，
       否则每个纯英文节点都要跑一遍，白费。

     ！模式表按语言缓存。不缓存的话，每个没命中的中文节点都要把整张字典
       遍历一遍 —— 一次渲染几百个节点，就是几万次字符串比较，
       卡顿会实实在在地出现。 */
  const patternCache = new Map();

  function compilePattern(key, value) {
    const parts = key.split("{}");
    if (parts.length < 2) return null;
    // ！非贪婪：不然 "3 分钟前" 会被 (.+) 整个吃掉
    const source = parts
      .map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
      .join("(.+?)");
    return { re: new RegExp("^" + source + "$"), value, key };
  }

  function patterns() {
    if (patternCache.has(current)) return patternCache.get(current);
    const table = dicts[current] || {};
    const list = [];
    for (const [key, value] of Object.entries(table)) {
      const rule = compilePattern(key, value);
      if (rule) list.push(rule);
    }
    // 长的排前面：多个条目都能匹配时，先命中更具体的那个，少翻半句
    list.sort((a, b) => b.key.length - a.key.length);
    patternCache.set(current, list);
    return list;
  }

  /* 一句话里可以有好几个 {}：“找到 {} 位…含「{}」的同好”。 */
  function patternFor(text) {
    for (const rule of patterns()) {
      if (rule.re.test(text)) return rule;
    }
    return null;
  }

  function translateText(raw) {
    const text = raw.trim();
    if (!text) return null;
    const direct = dicts[current] && dicts[current][text];
    if (typeof direct === "string" && direct) {
      // 保留原有的首尾空白，免得把排版挤没
      return raw.replace(text, direct);
    }
    if (!CJK.test(text)) return null;
    const hit = patternFor(text);
    if (!hit) return null;
    // 把捕获到的片段按顺序填回译文的 {} 里。
    // ！每个片段还要再查一次表。模式条目匹配上的是**整句**，
    //   但句子里的零件往往自己也是条目：主题按钮的提示是拼出来的
    //   “主题：跟随系统 · 点击切换到浅色”，光翻外壳会得到
    //   “Theme: 跟随系统 · click for 浅色” 这种半汉半英的东西。
    const parts = text.match(hit.re).slice(1).map((part) => lookup(part.trim()));
    let index = 0;
    const filled = hit.value.replace(/\{\}/g, () => (index < parts.length ? parts[index++] : ""));
    return raw.replace(text, filled);
  }

  const CJK = /[\u4e00-\u9fff]/;

  /* 把 root 底下（含自己）所有文字和属性过一遍字典。
     切语言、每次渲染之后都要调，代价是 O(节点数) 的查表，很便宜。 */
  function applyDynamic(root) {
    const scope = root || document.body;
    if (!scope) return;

    const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const tag = node.parentNode && node.parentNode.nodeName;
        if (SKIP_TAGS.has(tag)) return NodeFilter.FILTER_REJECT;
        if (node.parentNode && node.parentNode.closest("[data-i18n-skip]")) {
          return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      },
    });

    const todo = [];
    while (walker.nextNode()) todo.push(walker.currentNode);
    for (const node of todo) {
      const next = translateText(node.nodeValue);
      if (next !== null) node.nodeValue = next;
    }

    // ！要把 scope 自己也算上：调用方常常就是把待翻的元素传进来
    //   （theme.js 传的就是按钮本身），只用 querySelectorAll 会漏掉它。
    const nodes = [scope];
    if (scope.querySelectorAll) nodes.push(...scope.querySelectorAll("*"));
    for (const node of nodes) {
      if (node.closest && node.closest("[data-i18n-skip]")) continue;
      for (const attr of DYN_ATTRS) {
        if (!node.hasAttribute(attr)) continue;
        const next = translateText(node.getAttribute(attr));
        if (next !== null) node.setAttribute(attr, next);
      }
    }
  }

  /* ---------------------------------------------------------------- 切换 */

  const listeners = [];

  function apply(lang, { persist = true, rerender = true } = {}) {
    if (!LANGS.includes(lang)) lang = "zh";
    current = lang;
    if (persist) localStorage.setItem(STORAGE_KEY, lang);

    document.documentElement.setAttribute("lang", lang);
    applyStatic();
    renderSwitchers();
    for (const fn of listeners) {
      try {
        fn(lang);
      } catch (error) {
        // ！单个监听器出错不能连累别人 —— 否则一个模块的小 bug
        //   会让整站只翻译一半，而且看不出是哪一层的问题。
        console.error("[i18n] 切换回调出错：", error);
      }
    }
    // ！顺序要紧：先让 app.js 用新语言重渲染（McfrRerender），
    //   再扫一遍 DOM。反过来的话，刚翻好的文字会立刻被重渲染盖回简体。
    if (rerender && typeof window.McfrRerender === "function") {
      window.McfrRerender();
    }
    applyDynamic();
  }

  function onApply(fn) {
    listeners.push(fn);
  }

  /* 切换器：所有 [data-lang-switch] 容器里都画一排按钮。
     ！用按钮而不是 <select>：三个选项、一眼看全，而且手机上少一次点击。 */
  function renderSwitchers() {
    const nodes = document.querySelectorAll("[data-lang-switch]");
    for (const node of nodes) {
      /* ！全称和短标签两个 span 都渲染出来，显示哪一个交给 mobile.css 的
         媒体查询决定。宽窄切换时不需要重跑这段 —— 切窗口宽度不会重建按钮，
         靠 JS 切就要额外挂 resize 监听，而 CSS 天生就会自己重算。 */
      node.innerHTML = LANGS.map((code) => `
        <button type="button" data-lang-pick="${code}"
                data-i18n-skip
                aria-pressed="${code === current}"
                aria-label="${LABELS[code]}"
                title="${LABELS[code]}"><span class="full">${LABELS[code]}</span><span class="short" aria-hidden="true">${SHORT[code]}</span></button>`).join("");
    }
  }

  document.addEventListener("click", (event) => {
    const hit = event.target.closest("[data-lang-pick]");
    if (!hit) return;
    event.preventDefault();
    apply(hit.dataset.langPick);
  });

  /* ---------------------------------------------------------------- 对外 */

  window.McfrI18n = {
    LANGS,
    LABELS,
    t,
    apply,
    applyStatic,
    applyDynamic,
    onApply,
    add(lang, table) {
      if (!LANGS.includes(lang)) return;
      dicts[lang] = Object.assign(dicts[lang] || {}, table);
      patternCache.delete(lang);   // 字典变了，缓存作废
    },
    get lang() {
      return current;
    },
    /* 给后端用：让接口按同一门语言回错误提示 */
    acceptLanguage() {
      return current === "zh" ? "zh-CN" : current;
    },
    /** 覆盖率自查：某种语言还差多少条。翻漏的时候靠它兜底。 */
    stats() {
      const result = {};
      for (const code of LANGS) {
        const size = Object.keys(dicts[code] || {}).length;
        result[code] = { translated: size };
      }
      result.current = current;
      return result;
    },
  };

  /* 脚手架：给页面直接用的两个短名 */
  window.t = t;

  /* ---------------------------------------------------------------- 启动
     ！这里不能直接调 apply()：
       ① 此刻其它模块还没注册 onApply 回调，调了也白调；
       ② McfrRerender 还不存在。
      真正带重渲染的切换由用户点语言按钮时触发。
     ！脚本放在 </body> 前，DOM 已经解析完了，所以这一句用不着等 DOMContentLoaded。
       真等到那时候，首屏会先用中文画一遍再突然变语言 —— 那一下比什么都明显。 */
  function boot() {
    current = detect();
    document.documentElement.setAttribute("lang", current);
    applyStatic();
    renderSwitchers();
    // ！再补一次 DOM 通道。页面是带着 localStorage 里的语言直接打开的，
    //   光跑 applyStatic 只能翻 data-i18n 标过的那些 —— 主题按钮的提示、
    //   index.html 里没标属性的句子全是中文，用户会觉得"根本只有一半是英文"。
    //   放到 DOMContentLoaded 之后跑，此时排在后面的脚本都已经执行完了。
    window.setTimeout(applyDynamic, 0);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
