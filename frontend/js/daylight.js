/* =========================================================================
   mcfr · 天光：跟着时间走的光照
   -------------------------------------------------------------------------
   这一层只负责「现在几点 → 光长什么样」，主题本身（data-theme）还是
   theme.js 说了算 —— 它只是多了一档「跟随时间」，那一档在判断现在是白天
   还是黑夜时来问这个模块。

   ## 一天是怎么过的

   固定锚点：6:00 日出、18:00 日落。

   ！为什么不用真实的日出日落时刻：那需要经纬度，而本站**不收集位置**
      （面向未成年人，能少一项是一项）。固定锚点最多在冬天偏早、夏天偏晚
      半小时左右，而这件事要的是「白天亮、傍晚暖、夜里暗」的观感，
      不是天文预报。真按经纬度算，代价是每个人都要交出一次定位。

   时间轴上一共 14 个关键帧（KEYS），每一分钟都落在相邻两帧之间做线性插值，
   所以颜色、雾、暗度都是连续变化的 —— 没有「某一分钟突然变了」的台阶。

   ## 三个分量

     top / bottom   天幕的上下两端颜色（线性渐变）
     glow           太阳（或月亮）那团光斑的颜色，位置由 sunAt() 现算
     fog            雾的浓度，早上最重 —— 第 5 站那层白雾
     depth          夜里有多黑：0 = 白天，1 = 凌晨最深的那一段

   ## 为什么用「两层交叉淡入」而不是直接改渐变

   CSS 的渐变**不可插值**：background-image 从 A 换成 B 只会硬切一帧。
   要让光像太阳移动那样平滑，只能准备两块同样大小的图层，把新的那一块
   画在后面、把旧的淡出，用 opacity 过渡去糊住中间。见 HTML 里的
   #sky > .sky__layer × 2。

   ## 谁在用它

     · theme.js  —— 解析「跟随时间」这一档时问 isNight()；
                    白天黑夜切换的那一刻会反手调 McfrTheme.refresh()
     · 只有「跟随时间」这一档才点亮本模块（setActive）。
       手动选了浅色/深色的人，不该看见页面自己在变色 —— 那是他们明确选过的。
   ========================================================================= */

(() => {
  "use strict";

  const root = document.documentElement;

  /* 日出日落。！theme-boot.js 里有一段同样的判断（首屏防闪烁，必须同步执行），
     改这两个数的时候两处都要改。 */
  const SUNRISE = 6;
  const SUNSET = 18;

  /* 每 20 秒重算一次。太阳每分钟只挪 0.14% 的宽度，再密也看不出差别；
     但必须比「人离开一会儿再回来」短得多，否则回来时看到的是旧光。 */
  const TICK_MS = 20000;

  /* 雾的颜色（早上那层白）。刻意比纯白灰一点、蓝一点 —— 纯白会把整页
     洗成一张纸，而真实的晨雾是带一点点青的。 */
  const FOG = [232, 236, 236];

  /* ---- 一天的 14 个关键帧 ----
     h 是小时（0–24）。0 和 24 两帧必须给出**完全相同**的值，
     否则午夜那一秒会看到一次跳变。

     每帧两组数：

       天色  top / bottom / glow / alpha / fog / depth  —— 天上是什么颜色
       光影  ray / dapple / shade / moon               —— 光**怎么落下来**

     ！第二批才是这个功能的主角。「天色」只是底色，真正让人觉得「有太阳」
       的是光线（ray）、光斑（dapple）、投在地上的影子（shade）和月光（moon）。
       这四样都是 0–1 的强度，最终由 CSS 里的四个常量换算成不透明度。 */
  const KEYS = [
    // 凌晨：最深的夜
    { h: 0.0,  top: [10, 15, 22],    bottom: [18, 25, 36],    glow: [120, 150, 200], alpha: 0.30, fog: 0.00, depth: 1.00,
      ray: 0.00, dapple: 0.00, shade: 0.42, moon: 0.55 },
    { h: 4.2,  top: [14, 20, 32],    bottom: [26, 34, 50],    glow: [124, 152, 202], alpha: 0.27, fog: 0.05, depth: 0.86,
      ray: 0.00, dapple: 0.00, shade: 0.40, moon: 0.38 },
    // 蓝调（晨）：天还没亮，东边先蓝起来 —— 一天里最好看的那十几分钟
    { h: 5.2,  top: [30, 44, 72],    bottom: [66, 84, 116],   glow: [150, 170, 210], alpha: 0.30, fog: 0.12, depth: 0.60,
      ray: 0.04, dapple: 0.02, shade: 0.30, moon: 0.16 },
    // 日出：地平线一抹橘，雾开始往上冒。光柱从这一刻起才真的看得见
    { h: 6.3,  top: [122, 138, 160], bottom: [214, 158, 122], glow: [255, 198, 150], alpha: 0.34, fog: 0.34, depth: 0.30,
      ray: 0.55, dapple: 0.16, shade: 0.55, moon: 0.00 },
    // 早雾：丁达尔效应最强的一段 —— 雾够浓，光柱才有东西可以「照出来」
    { h: 7.6,  top: [206, 216, 224], bottom: [232, 228, 214], glow: [255, 244, 220], alpha: 0.20, fog: 0.46, depth: 0.10,
      ray: 0.62, dapple: 0.28, shade: 0.45, moon: 0.00 },
    // 上午：雾散了，光斑开始清晰，影子还偏长
    { h: 9.6,  top: [216, 235, 246], bottom: [246, 242, 226], glow: [255, 250, 232], alpha: 0.16, fog: 0.10, depth: 0.02,
      ray: 0.26, dapple: 0.55, shade: 0.58, moon: 0.00 },
    // 中午：光从头顶来 —— 影子最短、光斑最实、光柱几乎没有
    { h: 12.3, top: [228, 244, 250], bottom: [252, 250, 242], glow: [255, 255, 246], alpha: 0.12, fog: 0.02, depth: 0.00,
      ray: 0.08, dapple: 0.70, shade: 0.28, moon: 0.00 },
    // 下午：光线往暖里走，影子开始变长
    { h: 15.2,  top: [244, 232, 206], bottom: [252, 240, 214], glow: [255, 226, 168], alpha: 0.18, fog: 0.04, depth: 0.02,
      ray: 0.14, dapple: 0.60, shade: 0.55, moon: 0.00 },
    // 黄昏：整片泛橙，光斑又大又低，影子拉得最长
    { h: 17.05, top: [250, 202, 152], bottom: [255, 170, 112], glow: [255, 176, 108], alpha: 0.34, fog: 0.08, depth: 0.08,
      ray: 0.34, dapple: 0.38, shade: 0.72, moon: 0.00 },
    // 日落：橘红最浓的那几分钟。
    // ！刻意压在 18:00 **之前**：18:00 一过主题就翻深色，而最浓的那口橘红
    //   要是落在深底上，会变成一块发闷的褐色 —— 那不是日落，那是脏。
    { h: 17.75, top: [206, 116, 96],  bottom: [246, 132, 84],  glow: [255, 130, 74],  alpha: 0.46, fog: 0.05, depth: 0.16,
      ray: 0.46, dapple: 0.20, shade: 0.62, moon: 0.00 },
    // 蓝调（晚）：暖色退干净，天从紫灰一路沉到冷蓝，月亮开始接光
    { h: 18.55, top: [70, 84, 116],   bottom: [120, 96, 102],  glow: [190, 140, 130], alpha: 0.28, fog: 0.03, depth: 0.42,
      ray: 0.20, dapple: 0.05, shade: 0.45, moon: 0.10 },
    { h: 19.60, top: [40, 56, 88],    bottom: [58, 66, 96],    glow: [140, 160, 210], alpha: 0.30, fog: 0.02, depth: 0.62,
      ray: 0.10, dapple: 0.00, shade: 0.38, moon: 0.32 },
    { h: 21.0,  top: [16, 22, 34],    bottom: [26, 33, 46],    glow: [120, 150, 200], alpha: 0.26, fog: 0.00, depth: 0.90,
      ray: 0.04, dapple: 0.00, shade: 0.42, moon: 0.52 },
    { h: 24.0,  top: [10, 15, 22],    bottom: [18, 25, 36],    glow: [120, 150, 200], alpha: 0.30, fog: 0.00, depth: 1.00,
      ray: 0.00, dapple: 0.00, shade: 0.42, moon: 0.55 },
  ];

  /* 四样光最终换算成 CSS 不透明度的上限。都是「强度 1 时有多亮」。
     ！全部压得很低：这一层要像真的光，不能像贴了一张发光的底图。
     这几个数是配合 aether.css 里的普通叠加调出来的 —— 用 screen 时它们
     得更大，但白天根本没有效果（白 + 光 = 白），所以整组改成了普通叠加。 */
  const LIGHT_MAX = {
    ray: 0.60,      // 光柱：早上/黄昏最明显
    dapple: 0.22,   // 光斑：中午最实
    shade: 0.34,    // 影子：黄昏最长、最重
    moon: 0.62,     // 月色：淡淡的就好
  };

  /* ---------------------------------------------------------------- 小工具 */

  function clamp(value, lo, hi) {
    return value < lo ? lo : (value > hi ? hi : value);
  }

  function lerp(a, b, t) {
    return a + (b - a) * t;
  }

  function mix(a, b, t) {
    return [
      Math.round(lerp(a[0], b[0], t)),
      Math.round(lerp(a[1], b[1], t)),
      Math.round(lerp(a[2], b[2], t)),
    ];
  }

  function rgba(color, alpha) {
    return "rgba(" + color[0] + ", " + color[1] + ", " + color[2] + ", " +
      clamp(alpha, 0, 1).toFixed(3) + ")";
  }

  function hourOf(date) {
    const d = date || new Date();
    return d.getHours() + d.getMinutes() / 60 + d.getSeconds() / 3600;
  }

  /* ---------------------------------------------------------------- 纯计算 */

  /** 现在是不是夜里。theme.js 和 theme-boot.js 都问这一句。 */
  function isNight(date) {
    const h = hourOf(date);
    return h < SUNRISE || h >= SUNSET;
  }

  /** 在 14 个关键帧之间线性插值。0 ≤ hour < 24。 */
  function sample(hour) {
    const h = ((hour % 24) + 24) % 24;
    let before = KEYS[0];
    let after = KEYS[KEYS.length - 1];
    for (let i = 0; i < KEYS.length - 1; i += 1) {
      if (h >= KEYS[i].h && h <= KEYS[i + 1].h) {
        before = KEYS[i];
        after = KEYS[i + 1];
        break;
      }
    }
    const span = after.h - before.h || 1;
    const t = clamp((h - before.h) / span, 0, 1);

    /* ！这里刻意用**线性**插值，不套 smoothstep。
       套了的话每一帧的导数在关键帧处归零，光色会一段一段地「停顿」，
       2 小时一停，反而看得出来是人手调的。线性只在关键帧处有个不可见的
       折角 —— 颜色的一阶导数没人看得出来，位置的一阶导数才要命，
       而位置（太阳走的那条弧）是下面 sunAt() 单独用 sin 算的。 */
    return {
      hour: h,
      top: mix(before.top, after.top, t),
      bottom: mix(before.bottom, after.bottom, t),
      glow: mix(before.glow, after.glow, t),
      alpha: lerp(before.alpha, after.alpha, t),
      fog: lerp(before.fog, after.fog, t),
      depth: lerp(before.depth, after.depth, t),
      // 光影四件套
      ray: lerp(before.ray, after.ray, t),
      dapple: lerp(before.dapple, after.dapple, t),
      shade: lerp(before.shade, after.shade, t),
      moon: lerp(before.moon, after.moon, t),
    };
  }

  /** 太阳（夜里是月亮）在天幕上的位置，百分比。
      白天从左往右走一条正弦弧，夜里用月亮接着走同一段弧 ——
      这样光斑在晚上也不会僵在一个点上。

      ！夜里是**往回走**的：18:00 光从最右边（刚落下去的太阳）交给月亮，
        月亮一路往左挪，到 6:00 正好和升起的太阳停在同一个位置。
        不这么做的话，6:00 和 18:00 各会有一次**瞬移** —— 光源从画面一头
        跳到另一头，那正是「不无缝」最刺眼的样子（实测过：一秒钟里
        sunX 从 93.9 跳到 6.0）。真实世界里太阳在西边落、月亮在东边升，
        但屏幕上只有一个光斑，连续性比天文正确重要。

      y 只在 46%（贴地平线）和 8%（头顶）之间走：光源永远在画面**上半部分**，
      影子才总是朝下落 —— 这是人看一个「有光的天花板」时唯一说得通的朝向。 */
  function sunAt(hour) {
    const day = hour >= SUNRISE && hour < SUNSET;
    const span = day ? (SUNSET - SUNRISE) : (24 - (SUNSET - SUNRISE));
    const local = day
      ? hour - SUNRISE
      : (hour >= SUNSET ? hour - SUNSET : hour + 24 - SUNSET);
    const p = clamp(local / span, 0, 1);
    const lift = Math.sin(Math.PI * p);      // 0 = 刚出地平线，1 = 最高
    const travel = day ? p : (1 - p);
    return {
      day,
      x: 6 + travel * 88,                    // 6% → 94%
      y: 46 - lift * 38,                     // 46%（贴着地平线）→ 8%（头顶）
      lift,
    };
  }

  /* ---- 光影：光源在哪、影子朝哪、每样光多强 ----

     ！这才是「时间带来的光」，上面那些颜色只是它的后果。
       四个强度直接喂给 CSS 里的四层：
         ray    光柱（丁达尔）—— 跟着光源转，雾越浓越明显
         dapple 光斑           —— 光落在表面上的一团团亮
         shade  影子           —— 覆盖整页的方向性压暗，朝背光一侧
         moon   月色           —— 夜里那层冷光

     这里只算数据，一行 DOM 都不碰：好测、也好调。 */
  function lightsOf(s, hour) {
    const sun = sunAt(hour);
    /* 影子朝哪：从光源指向画面中心，再继续往那一侧压暗。
       ！这里要的是**旋转角**，不是 CSS 渐变的 θ。底下那条渐变写的是
         `90deg`（暗的一头朝右），所以「把这块方块转 φ」就等于让暗的一头
         指向 (cos φ, sin φ)。屏幕坐标 y 向下，于是 φ = atan2(dy, dx)。
         常见错法是直接套渐变角 atan2(dx, -dy)：转出来正好差 90°，
         症状是「太阳在左上，影子却往左下倒」。 */
    const dx = 50 - sun.x;
    const dy = 50 - sun.y;
    const phi = Math.atan2(dy, dx) * 180 / Math.PI;

    return {
      sunX: sun.x,
      sunY: sun.y,
      // 太阳（月亮）在地平线附近时影子最长：用 lift 当「低角度」的度量
      lowAngle: 1 - sun.lift,
      angle: (phi + 360) % 360,
      ray: s.ray * LIGHT_MAX.ray,
      dapple: s.dapple * LIGHT_MAX.dapple,
      // 影子在黄昏最重；低角度本身也会让它更斜更明显
      shade: s.shade * LIGHT_MAX.shade,
      dappleStretch: 1 + s.shade * 0.75,
      moon: s.moon * LIGHT_MAX.moon,
    };
  }

  /** 一天里这段时间叫什么。给调试和以后可能的界面提示用。
      ！边界要和 KEYS 对得上：18.3 之前还是黄昏（日落高峰在 17.75），
        之后才轮到蓝调。对不上就会出现「标签写着蓝调、画面还是橘红」。 */
  function label(hour) {
    const h = hour === undefined ? hourOf() : hour;
    if (h < 4.2) return "深夜";
    if (h < SUNRISE) return "黎明";
    if (h < 8.4) return "早雾";
    if (h < 11) return "上午";
    if (h < 14) return "中午";
    if (h < 16.6) return "下午";
    if (h < 18.3) return "黄昏";
    if (h < 19.9) return "蓝调";
    if (h < 22.5) return "夜晚";
    return "深夜";
  }

  /** 把一次采样变成 CSS 的 background。
      三个渐变叠在一起：光斑 + 天幕 + 地平线附近那层雾。 */
  function backgroundOf(s) {
    const sun = sunAt(s.hour);
    const glow = rgba(s.glow, s.alpha * 0.58);
    const glowEdge = rgba(s.glow, 0);
    const top = rgba(s.top, s.alpha * 0.92);
    const bottom = rgba(s.bottom, s.alpha * 0.70);
    const fog = rgba(FOG, s.fog * 0.55);
    const fogEdge = rgba(FOG, 0);
    return [
      // 光斑：位置跟着太阳走，靠近地平线时它会更大更散（circle 默认吃满远角）
      "radial-gradient(circle at " + sun.x.toFixed(2) + "% " + sun.y.toFixed(2) + "%, " +
        glow + " 0%, " + glowEdge + " 58%)",
      // 天幕：上深下浅
      "linear-gradient(180deg, " + top + " 0%, " + bottom + " 100%)",
      // 雾：只压在下半部分，越靠地面越浓
      "linear-gradient(180deg, " + fogEdge + " 38%, " + fog + " 100%)",
    ].join(", ");
  }

  /* ---------------------------------------------------------------- 画到页面上 */

  let active = false;
  let timer = 0;
  let layers = [];
  let lights = [];
  let front = -1;
  let painted = false;
  let lastNight = null;
  let manual = null;      // 调试：把时间钉死在某个小时

  function currentHour() {
    return manual === null ? hourOf() : manual;
  }

  function apply() {
    if (!active) return;
    const hour = currentHour();
    const s = sample(hour);
    const bg = backgroundOf(s);

    if (layers.length === 2) {
      const target = front === -1 ? 0 : 1 - front;
      const element = layers[target];
      if (element.dataset.bg !== bg) {
        element.dataset.bg = bg;
        element.style.background = bg;
        /* 第一次上色不要做慢淡入 —— 页面一打开就该是「现在这个点」的样子，
           而不是从一片空白里慢慢亮起来。之后的每一次都交给 CSS 过渡。 */
        element.style.transitionDuration = painted ? "" : "0s";
        element.classList.add("is-visible");
        if (front !== -1) layers[front].classList.remove("is-visible");
        front = target;
        painted = true;
      }
    }

    // 夜里有多黑。CSS 拿它当乘数（见 aether.css 的 #night-veil）。
    root.style.setProperty("--night-depth", s.depth.toFixed(3));

    /* 光影：只改这几个 CSS 变量，**不重建任何渐变**。
       ！这一点决定了它能不能「无缝」：渐变一旦重建就是一帧硬切，
         而 transform / opacity 的变化浏览器会自己补间（见 CSS 里的过渡）。
         光源位置每 20 秒才挪那么一点点，26 秒的过渡把它们连成一条连续的轨迹，
         看起来就是太阳在走，而不是每 20 秒跳一下。 */
    const light = lightsOf(s, hour);
    root.style.setProperty("--sun-x", light.sunX.toFixed(2));
    root.style.setProperty("--sun-y", light.sunY.toFixed(2));
    root.style.setProperty("--ray-alpha", light.ray.toFixed(4));
    root.style.setProperty("--dapple-alpha", light.dapple.toFixed(4));
    root.style.setProperty("--dapple-stretch", light.dappleStretch.toFixed(3));
    root.style.setProperty("--shade-angle", light.angle.toFixed(2) + "deg");
    root.style.setProperty("--shade-alpha", light.shade.toFixed(4));
    root.style.setProperty("--moon-alpha", light.moon.toFixed(4));

    /* 白天/黑夜翻面的那一刻，通知主题层重算一遍 —— 但只在「跟随时间」这一档
       才会真的改变什么（别的档 resolve() 根本不看时间）。 */
    const night = hour < SUNRISE || hour >= SUNSET;
    if (lastNight !== null && night !== lastNight && window.McfrTheme) {
      window.McfrTheme.refresh();
    }
    lastNight = night;
  }

  function tick() { apply(); }

  function startTimer() {
    if (timer) return;
    timer = window.setInterval(tick, TICK_MS);
  }

  function stopTimer() {
    if (!timer) return;
    window.clearInterval(timer);
    timer = 0;
  }

  /* 后台标签页里的定时器会被浏览器限流（几分钟才跑一次），而且**没人看**。
     所以切走就把定时器停掉，切回来再补一枪 —— 一来省电，二来免得回来时
     看到的是几小时前的光。 */
  document.addEventListener("visibilitychange", () => {
    if (!active) return;
    if (document.visibilityState === "visible") {
      apply();
      startTimer();
    } else {
      stopTimer();
    }
  });

  function setActive(on) {
    const next = Boolean(on);
    if (next === active) return;
    active = next;

    if (active) {
      if (!layers.length) {
        layers = Array.from(document.querySelectorAll("#sky .sky__layer"));
        lights = Array.from(document.querySelectorAll(".light"));
      }
      root.setAttribute("data-sky", "time");
      front = -1;
      painted = false;
      lastNight = null;
      /* ！第一次点亮要**瞬间**到位，不要淡。
         四层光都写着 26 秒的 opacity 过渡 —— 那是给「每 20 秒挪一点点」用的；
         第一次打开页面时如果也从 0 开始淡，用户会看着一道光慢慢浮上来，
         像加载动画一样。所以先摘掉过渡，画完再放回去。 */
      for (const element of lights) element.style.transitionDuration = "0s";
      apply();
      window.requestAnimationFrame(() => {
        for (const element of lights) element.style.transitionDuration = "";
      });
      startTimer();
    } else {
      root.removeAttribute("data-sky");
      root.style.removeProperty("--night-depth");
      /* ！光影那几个变量也要一并清掉，而且和 --night-depth 一起清。
         漏掉的后果是：手动切到「浅色」之后，那道方向性的影子还留在页面上 ——
         而且它不再更新，就一直斜在那里。 */
      for (const name of ["--sun-x", "--sun-y", "--ray-alpha", "--dapple-alpha",
                          "--dapple-stretch", "--shade-angle", "--shade-alpha",
                          "--moon-alpha"]) {
        root.style.removeProperty(name);
      }
      stopTimer();
      /* ！两块图层要一起清空，包括 dataset.bg 里记着的上一份样式。
         只去掉 .is-visible 的话，切回来时如果刚好还落在同一分钟的同一份
         样式上，下面那句 `dataset.bg !== bg` 会判成「没变化」而**什么都不画**，
         天光就再也不出现了 —— 一个只在「切走又切回来」时才复现的 bug。 */
      for (const element of layers) {
        element.classList.remove("is-visible");
        element.removeAttribute("data-bg");
        element.style.background = "";
      }
      front = -1;
      painted = false;
      lastNight = null;
    }
  }

  window.McfrDaylight = {
    SUNRISE,
    SUNSET,
    isNight,
    hourOf,
    sample,
    sunAt,
    lightsOf,
    /** 当前这一刻的四层光参数（控制台里调试用） */
    lights(hour) {
      const h = hour === undefined ? currentHour() : Number(hour);
      return lightsOf(sample(h), h);
    },
    label,
    setActive,
    apply,
    get active() { return active; },
    /** 调试：把时间钉在某个小时看效果（控制台里 McfrDaylight.preview(19.2)） */
    preview(hour) {
      manual = hour === null || hour === undefined ? null : Number(hour);
      apply();
      if (window.McfrTheme) window.McfrTheme.refresh();
    },
    resume() {
      manual = null;
      apply();
      if (window.McfrTheme) window.McfrTheme.refresh();
    },
  };
})();
