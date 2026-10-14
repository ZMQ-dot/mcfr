/* =========================================================================
   mcfr · 视觉特效
   -------------------------------------------------------------------------
   1. 点击涟漪 —— 每次点击都会在落点扩散一圈虹彩波纹
   2. 方块碎屑 —— 与涟漪同心溅出的几粒小方块，对应 MC 挖方块时的碎屑
   3. 离子流动 —— 背景画布上漂浮并互相连线的粒子，
      呼应 Aether 的「光被分解成信号」概念
   4. 标签页切换 —— 选中指示器滑动 + 内容按方向滑入（见文件下半部分）
   5. 草方块像素纹理 —— 程序生成的 16×16 噪声，给「进入 mcfr」按钮用
      （见 minecraft.css 第 6 节；纹理是自己算出来的，不是游戏贴图）

   这些特效都刻意做得很轻：粒子数按屏幕面积自适应，页面切到后台时自动暂停，
   并遵守系统的「减少动态效果」设置。
   ========================================================================= */

/* 挂到 window 上：顶层 const 不会成为全局属性，而 app.js / admin.js 需要跨脚本调用 */
window.McfrEffects = (() => {
  "use strict";

  // 主题色：草方块上的那三级绿（与 aether.css 里的 --grass-* 保持一致）。
  // ！这三处是 JS 里唯一直接写颜色值的地方，改主题时要跟着改。
  const GRASS = ["#86c168", "#6ba84f", "#4d7f38"];
  const LINK_DISTANCE_SQ = 19000; // 连线距离阈值（平方，省一次开方）

  /* ---------------------------------------------------- 草方块像素纹理 */

  const BLOCK_SIZE = 16;

  // 草方块的三档绿：叶面受光 / 叶面主体 / 贴着泥土的背光。
  // ！这几个色要**贴着主题色**，不能各自为政，否则贴图和按钮底色接不上。
  const GRASS_SHADES = ["#86c168", "#7cb95e", "#73b055", "#6ba84f", "#63a049", "#5b9844"];
  // 泥土的六档棕 + 偶尔掺进去的石子。
  // ！这里刻意把明暗差压得很窄（对比第一版 #8a6a44 → #432d1a 那种大跳）。
  //   差值一大，缩放后的像素点在按钮上就是一片噪点、很硬、很“脏”；
  //   压窄之后依然是土，但看起来柔和得多，也不会抢按钮文字的对比度。
  const DIRT_SHADES = ["#8f7657", "#866e51", "#7d664b", "#746045", "#6b583f", "#634f39"];
  const DIRT_SPECK = "#59462f";
  // 草皮下沿可能落在第几行（0 是最上面），一列一个值 —— 参差的那道边就是草方块的招牌
  const GRASS_EDGE_ROWS = [4, 5, 6];

  /**
   * 生成一张 16×16 的**草方块侧面**纹理，返回可以直接塞进 CSS 的 url(...)。
   *
   * ！这里**没有**、也不应该去取游戏里的草方块贴图 —— 那是 Mojang 的美术资源。
   *   草方块的辨识度其实只来自两个特征，自己算一遍就够了：
   *     1. 上缘一道**参差不齐**的草绿（每列的深度都不一样，不是一条直边）；
   *     2. 下面全部是随机深浅的泥土棕，偶尔掺几粒更深的石子。
   *   自己生成的噪声在观感上和草方块一致，又完全没有版权问题。
   *
   * 用固定的种子而不是 Math.random()：每次刷新拿到的纹理都一模一样，
   * 不会因为纹理变了而让人误以为页面闪了一下。
   */
  function buildGrassBlockTexture() {
    const canvas = document.createElement("canvas");
    canvas.width = BLOCK_SIZE;
    canvas.height = BLOCK_SIZE;

    const ctx = canvas.getContext("2d");
    if (!ctx) return "";   // 拿不到 2D 上下文时退回纯草绿（CSS 里 var() 的第二个参数）

    let seed = 20260918;
    const rand = () => {
      // 线性同余发生器：够随机，也够稳定（同一个种子必然得到同一张纹理）
      seed = (seed * 1103515245 + 12345) & 0x7fffffff;
      return seed / 0x7fffffff;
    };

    // 先逐列摇出草皮的下沿。
    // ！这一步必须**按列**算，不能每格都重新摇：那样草和土会变成上下随机交错，
    //   看起来是一团花的，完全不像草方块。草方块的草皮是一条毛边，不是噪点。
    const edgeRows = [];
    for (let x = 0; x < BLOCK_SIZE; x += 1) {
      let rows = GRASS_EDGE_ROWS[Math.floor(rand() * GRASS_EDGE_ROWS.length)];
      // 偶尔让这一列的草皮再多垂一格，毛边才不会像锯齿一样规整
      if (rand() > 0.8) rows += 1;
      edgeRows.push(rows);
    }

    for (let x = 0; x < BLOCK_SIZE; x += 1) {
      for (let y = 0; y < BLOCK_SIZE; y += 1) {
        let shade;
        if (y < edgeRows[x]) {
          shade = GRASS_SHADES[Math.floor(rand() * GRASS_SHADES.length)];
        } else if (rand() > 0.9) {
          shade = DIRT_SPECK;
        } else {
          shade = DIRT_SHADES[Math.floor(rand() * DIRT_SHADES.length)];
        }
        ctx.fillStyle = shade;
        ctx.fillRect(x, y, 1, 1);
      }
    }

    return `url("${canvas.toDataURL("image/png")}")`;
  }

  /**
   * 把纹理挂到 :root 的 --grass-block-tex 上，CSS 那边直接 var() 取用。
   * 失败（极端老浏览器）就什么都不做，按钮会退回纯草绿，不影响可用性。
   */
  function applyGrassBlockTexture() {
    try {
      const texture = buildGrassBlockTexture();
      if (!texture) return;
      document.documentElement.style.setProperty("--grass-block-tex", texture);
    } catch (error) {
      /* canvas 被禁用之类的情况：忽略，纯色照样能用 */
    }
  }

  applyGrassBlockTexture();


  /* ---------------------------------------------------------------- 涟漪 */

  function spawnRipple(x, y) {
    const node = document.createElement("span");
    node.className = "click-ripple";
    node.style.left = `${x}px`;
    node.style.top = `${y}px`;
    document.body.appendChild(node);
    window.setTimeout(() => node.remove(), 620);
  }

  /* MC 挖开方块时会溅出方形的碎屑。这里给每次点击配几粒：
     形状与运动交给 CSS（.block-bit），JS 只负责随机方向和颜色。
     颜色只用调色板里的三色虹彩，绝不引入 MC 原生的草绿 / 泥土棕。 */
  function spawnBlockBits(x, y) {
    const count = 6;
    for (let i = 0; i < count; i += 1) {
      const angle = (Math.PI * 2 * i) / count + Math.random() * 0.45;
      const reach = 15 + Math.random() * 18;
      const side = 2 + Math.round(Math.random() * 2);

      const node = document.createElement("span");
      node.className = "block-bit";
      node.style.left = `${x}px`;
      node.style.top = `${y}px`;
      node.style.width = `${side}px`;
      node.style.height = `${side}px`;
      node.style.background = GRASS[i % GRASS.length];
      node.style.setProperty("--dx", `${(Math.cos(angle) * reach).toFixed(1)}px`);
      // 稍微偏上，然后在关键帧末段被「重力」拉下去
      node.style.setProperty("--dy", `${(Math.sin(angle) * reach * 0.55 - 5).toFixed(1)}px`);

      document.body.appendChild(node);
      window.setTimeout(() => node.remove(), 620);
    }
  }

  function bindRipple(reduceMotion) {
    document.addEventListener(
      "pointerdown",
      (event) => {
        if (event.button !== 0) return;
        spawnRipple(event.clientX, event.clientY);
        if (!reduceMotion) spawnBlockBits(event.clientX, event.clientY);
      },
      { passive: true }
    );
  }

  /* ------------------------------------------------------------ 离子流动 */

  function startIonFlow(canvas) {
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let width = 0;
    let height = 0;
    let particles = [];
    let frame = null;
    let running = true;

    function seed() {
      const area = width * height;
      const count = Math.round(Math.min(Math.max(area / 26000, 26), 76));
      particles = Array.from({ length: count }, () => ({
        x: Math.random() * width,
        y: Math.random() * height,
        vx: (Math.random() - 0.5) * 0.34,
        vy: (Math.random() - 0.5) * 0.34,
        r: 0.7 + Math.random() * 1.5,
        color: GRASS[Math.floor(Math.random() * GRASS.length)],
        phase: Math.random() * Math.PI * 2,
      }));
    }

    function resize() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      width = canvas.clientWidth;
      height = canvas.clientHeight;
      canvas.width = Math.max(1, Math.floor(width * dpr));
      canvas.height = Math.max(1, Math.floor(height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      seed();
    }

    /* 背景粒子每秒才漂 ~20px，30fps 和 60fps 肉眼分不出来，
       但整块全屏画布每帧都要重新上传一次纹理（1440×950×4 字节 ≈ 5.5MB），
       砍一半帧率就省下一半带宽 —— 弱机子上这是滚动的关键成本。 */
    const PAINT_INTERVAL = 1000 / 30;
    let lastTime = 0;
    let sincePaint = 0;

    function draw(time) {
      // 位移改成按真实时间推进，不再「每帧固定一步」——
      // 顺带修掉了「高刷屏上粒子飘得更快」的问题。
      const delta = lastTime ? Math.min(time - lastTime, 50) : 16.7;
      lastTime = time;
      sincePaint += delta;

      if (sincePaint < PAINT_INTERVAL) {
        if (running) frame = window.requestAnimationFrame(draw);
        return;
      }

      const k = sincePaint / 16.7; // 相对「一帧 60fps」的倍率，保证速度不变
      sincePaint = 0;

      ctx.clearRect(0, 0, width, height);

      // 位移：匀速 + 一点正弦漂移，看起来像「流动」而不是平移
      for (const p of particles) {
        p.x += (p.vx + Math.sin(time / 2400 + p.phase) * 0.11) * k;
        p.y += (p.vy + Math.cos(time / 2800 + p.phase) * 0.11) * k;
        if (p.x < -24) p.x = width + 24;
        if (p.x > width + 24) p.x = -24;
        if (p.y < -24) p.y = height + 24;
        if (p.y > height + 24) p.y = -24;
      }

      // 近邻连线
      ctx.lineWidth = 0.6;
      for (let i = 0; i < particles.length; i += 1) {
        const a = particles[i];
        for (let j = i + 1; j < particles.length; j += 1) {
          const b = particles[j];
          const dx = a.x - b.x;
          const dy = a.y - b.y;
          const distSq = dx * dx + dy * dy;
          if (distSq > LINK_DISTANCE_SQ) continue;
          const alpha = (1 - distSq / LINK_DISTANCE_SQ) * 0.22;
          // 连线的颜色也要跟着主题走，否则换主题后背景里会剩下一片旧色
          ctx.strokeStyle = `rgba(107, 168, 79, ${alpha.toFixed(3)})`;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }

      // 粒子本体（呼吸式明暗）
      for (const p of particles) {
        const breath = 0.5 + 0.5 * Math.sin(time / 950 + p.phase);
        ctx.globalAlpha = 0.22 + breath * 0.42;
        ctx.fillStyle = p.color;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.globalAlpha = 1;

      if (running) frame = window.requestAnimationFrame(draw);
    }

    function play() {
      if (frame !== null) return;
      running = true;
      lastTime = 0;      // 重新开始计时，避免停久了第一帧猛跳一下
      sincePaint = 0;
      frame = window.requestAnimationFrame(draw);
    }

    function pause() {
      running = false;
      if (frame !== null) {
        window.cancelAnimationFrame(frame);
        frame = null;
      }
    }

    resize();
    play();

    let resizeTimer = null;
    window.addEventListener("resize", () => {
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(resize, 200);
    });

    // 切到其它标签页时停下来，不浪费电
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) pause();
      else play();
    });
  }

  /* ------------------------------------------------------------ 标签页切换 */

  const TAB_LAST = new WeakMap(); // 容器 → 上一次选中的序号

  function prefersReducedMotion() {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  /* 容器里当前选中的那个按钮。
     标签页用 aria-selected，侧栏导航用 aria-current，两种都得认。 */
  function activeTab(container) {
    const buttons = Array.from(container.querySelectorAll("button"));
    return buttons.find((button) =>
      button.getAttribute("aria-selected") === "true" ||
      button.getAttribute("aria-current") === "true"
    ) || null;
  }

  /* 这次是往右切（1）还是往左切（-1）。
     必须在改完 aria-selected / aria-current **之后**调用，
     它顺便把本次序号记下来，供下次比较。 */
  function tabDir(container) {
    if (!container) return 0;
    const buttons = Array.from(container.querySelectorAll("button"));
    const now = buttons.indexOf(activeTab(container));
    const before = TAB_LAST.has(container) ? TAB_LAST.get(container) : now;
    TAB_LAST.set(container, now);
    if (now < 0 || now === before) return 0;
    return now > before ? 1 : -1;
  }

  /* 把选中指示器挪到当前按钮上。
     指示器是容器**自己的 ::before**，不是插进去的子元素 ——
     侧栏 #sidenav 每次切页都用 innerHTML 重建全部子节点，插进去的会被一并抹掉，
     伪元素则一直活着，所以能平滑地滑过去。位置通过 --tab-x/y/w/h 交给 CSS。 */
  function tabBar(container) {
    if (!container) return;
    container.classList.add("is-tabbar");

    const active = activeTab(container);

    // 第一次挂载时先把当前选中项记下来。
    // 不记的话，用户点的第一下就是 tabDir() 的首秀 —— 没有参照物、方向算成 0，
    // 那一次切换会安静地不带动画（这个坑踩过一次）。
    if (!TAB_LAST.has(container)) {
      const buttons = Array.from(container.querySelectorAll("button"));
      TAB_LAST.set(container, buttons.indexOf(active));
    }

    if (!active) return;

    const item = active.getBoundingClientRect();
    if (!item.width || !item.height) return; // 还藏着，量不到，等下次

    const box = container.getBoundingClientRect();
    const style = getComputedStyle(container);
    // 绝对定位的参照点是容器的内边距盒，所以要把边框宽度扣掉
    const dx = item.left - box.left - (parseFloat(style.borderLeftWidth) || 0);
    const dy = item.top - box.top - (parseFloat(style.borderTopWidth) || 0);

    container.style.setProperty("--tab-x", `${dx.toFixed(1)}px`);
    container.style.setProperty("--tab-y", `${dy.toFixed(1)}px`);
    container.style.setProperty("--tab-w", `${item.width.toFixed(1)}px`);
    container.style.setProperty("--tab-h", `${item.height.toFixed(1)}px`);

    // 第一次得等两帧再打开过渡，否则指示器会从 (0,0) 一路滑到目标位置
    if (!container.classList.contains("is-ready")) {
      window.requestAnimationFrame(() =>
        window.requestAnimationFrame(() => container.classList.add("is-ready"))
      );
    }
  }

  /* 内容按方向滑入：dir > 0 从右边进来，dir < 0 从左边进来 */
  function tabIn(element, dir) {
    if (!element || !dir || prefersReducedMotion()) return;
    element.classList.remove("tab-in", "tab-in--back");
    void element.offsetWidth; // 强制重排，否则同一个类名不会重播动画
    element.classList.add(dir > 0 ? "tab-in" : "tab-in--back");
  }

  /* 一次完整的「切换」：指示器滑过去 + 内容滑进来 */
  function tabSwap(container, panel) {
    const dir = tabDir(container);
    tabBar(container);
    tabIn(panel, dir);
    return dir;
  }

  /* 窗口尺寸变了按钮宽度也变了，所有已挂载的指示器都要重新量 */
  function refreshTabBars() {
    document.querySelectorAll(".is-tabbar").forEach(tabBar);
  }

  /* ---------------------------------------------------------------- 启动 */

  function start() {
    // 尊重系统的「减少动态效果」设置
    const reduceMotion = prefersReducedMotion();
    bindRipple(reduceMotion);

    let resizeTimer = null;
    window.addEventListener("resize", () => {
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(refreshTabBars, 180);
    });

    const canvas = document.getElementById("ion-canvas");
    if (!canvas) return;
    if (reduceMotion) return;

    startIonFlow(canvas);
  }

  return { start, tabBar, tabIn, tabDir, tabSwap, grassBlockTexture: buildGrassBlockTexture };
})();
