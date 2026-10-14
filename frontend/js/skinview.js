/* ============================================================
   mcfr · Minecraft 皮肤 3D 预览（纯 Canvas 2D，无第三方库）
   ------------------------------------------------------------
   本项目刻意不用 CDN（校园网下外链经常加载不出来），所以没有引 three.js，
   而是自己用 Canvas 2D 画：

     1. 把 64×64 的皮肤贴图按 Minecraft 的 UV 版式切成一个个矩形；
     2. 每个身体部位当成一个长方体，算出它 6 个面在 3D 空间里的四个角；
     3. 正交投影（先绕 Y 轴转，再绕 X 轴稍微俯一点）之后，每个面仍然是
        一个「平行四边形」，而平行四边形恰好能用 ctx.setTransform 的
        仿射矩阵精确表示 —— 于是就能把贴图矩形贴到投影后的形状上；
     4. 按面朝向来剔除背面，再按深度从远到近画一遍（画家算法）。

   交互：按住鼠标（或手指）左右拖动即可旋转，松手后带惯性，
   一直不动的话会自己慢慢转；双击回到正面。
   ============================================================ */

window.McfrSkin = (function () {
  "use strict";

  const TILT = 0.20;          // 俯视角，让头顶（头发）露出来
  const DRAG_SPEED = 0.010;   // 每像素拖动对应的弧度
  const MAX_VELOCITY = 0.16;  // 惯性最大角速度，防止一甩就飞
  const FRICTION = 0.93;      // 惯性衰减
  const IDLE_SPIN = 0.55;     // 无人操作时的自转速度（弧度/秒）

  const textureCache = new Map();
  const registry = new Set();
  let rafId = 0;

  /* ---------------------------------------------------------- 贴图加载 */

  function loadTexture(url) {
    if (!url) return Promise.resolve(null);
    if (textureCache.has(url)) return textureCache.get(url);

    const promise = new Promise((resolve) => {
      const image = new Image();
      image.decoding = "async";
      image.onload = () => resolve(image);
      image.onerror = () => resolve(null); // 拿不到就当没有皮肤，别把页面卡住
      image.src = url;
    });
    textureCache.set(url, promise);
    return promise;
  }

  /* ---------------------------------------------------------- 模型定义 */

  /* 每个部位记下贴图原点 (u, v) 与尺寸（宽, 高, 厚），中心点以「脚底 y=0」为基准，
     单位就是皮肤的像素，整个模型高 32 像素。 */
  function buildParts(variant) {
    const armWidth = variant === "SLIM" ? 3 : 4;
    const armX = 4 + armWidth / 2;

    return [
      { key: "head", uv: [0, 0], size: [8, 8, 8], center: [0, 28, 0], over: [32, 0] },
      { key: "body", uv: [16, 16], size: [8, 12, 4], center: [0, 18, 0], over: [16, 32] },
      { key: "rightArm", uv: [40, 16], size: [armWidth, 12, 4], center: [-armX, 18, 0], over: [40, 32] },
      { key: "leftArm", uv: [32, 48], size: [armWidth, 12, 4], center: [armX, 18, 0], over: [48, 48] },
      { key: "rightLeg", uv: [0, 16], size: [4, 12, 4], center: [-2, 6, 0], over: [0, 32] },
      { key: "leftLeg", uv: [16, 48], size: [4, 12, 4], center: [2, 6, 0], over: [0, 48] },
    ];
  }

  /* 长方体的六个面。
     corners 里的 TL/TR/BR/BL 指的是【贴图矩形】的四个角被摆到 3D 的哪个位置。
     从背面看时贴图本身是镜像的 —— 这是展开图（net）的编码方式，
     不能想当然地按屏幕左右去摆；校验办法是看贴图里相邻的两个矩形在 net 中
     共边，那么它们在 3D 里也必须共边。 */
  function boxFaces(uv, size) {
    const [u, v] = uv;
    const [w, h, d] = size;
    const dx = w / 2, dy = h / 2, dz = d / 2;

    return [
      { rect: [u + d, v + d, w, h], normal: [0, 0, 1],
        corners: [[-dx, dy, dz], [dx, dy, dz], [dx, -dy, dz], [-dx, -dy, dz]] },
      { rect: [u + 2 * d + w, v + d, w, h], normal: [0, 0, -1],
        corners: [[dx, dy, -dz], [-dx, dy, -dz], [-dx, -dy, -dz], [dx, -dy, -dz]] },
      { rect: [u, v + d, d, h], normal: [-1, 0, 0],
        corners: [[-dx, dy, -dz], [-dx, dy, dz], [-dx, -dy, dz], [-dx, -dy, -dz]] },
      { rect: [u + d + w, v + d, d, h], normal: [1, 0, 0],
        corners: [[dx, dy, dz], [dx, dy, -dz], [dx, -dy, -dz], [dx, -dy, dz]] },
      { rect: [u + d, v, w, d], normal: [0, 1, 0],
        corners: [[-dx, dy, -dz], [dx, dy, -dz], [dx, dy, dz], [-dx, dy, dz]] },
      { rect: [u + d + w, v, w, d], normal: [0, -1, 0],
        corners: [[dx, -dy, dz], [-dx, -dy, dz], [-dx, -dy, -dz], [dx, -dy, -dz]] },
    ];
  }

  /* ---------------------------------------------------------- 投影 */

  function makeProjector(rot, scale, cx, cy) {
    const cosY = Math.cos(rot), sinY = Math.sin(rot);
    const cosT = Math.cos(TILT), sinT = Math.sin(TILT);

    return function project(point) {
      const [x, y, z] = point;
      const rx = x * cosY + z * sinY;
      const rz = -x * sinY + z * cosY;
      const ry = y * cosT - rz * sinT;
      const depth = y * sinT + rz * cosT;
      return [cx + rx * scale, cy - ry * scale, depth];
    };
  }

  /* 相机在 +Z 方向看过来，把面法线转过来之后 z 分量为正就说明这面朝镜头 */
  function facesCamera(normal, rot) {
    const cosY = Math.cos(rot), sinY = Math.sin(rot);
    const cosT = Math.cos(TILT), sinT = Math.sin(TILT);
    const rz = -normal[0] * sinY + normal[2] * cosY;
    return normal[1] * sinT + rz * cosT > 0;
  }

  function translate(corners, center) {
    return corners.map(([x, y, z]) => [x + center[0], y + center[1], z + center[2]]);
  }

  /* ---------------------------------------------------------- 披风 */

  /* 披风就是一块 10×16 厚 1 的薄片，UV 依旧用同一套展开图规则算，
     net 原点放在 [0,0]，算出来的六个矩形正好就是披风贴图的六个区。 */
  const CAPE_TILT = 0.22; // 约 12.6°，从肩膀往后飘一点

  function capePart() {
    // 中心 y=16、高 16 ⇒ 顶端在 y=24（肩膀高度），底端垂到 y=8
    return { uv: [0, 0], size: [10, 16, 1], center: [0, 16, -2.6] };
  }

  /* ！披风的 ±Z 两个面需要对调。
     boxFaces 沿用「躯干」的约定：+Z 那面是朝前的（对躯干来说就是胸口）。
     披风挂在背后，它的 +Z 面其实是贴着身体的内侧，而设计图案画在 net 的
     第二个矩形（[12,1] 那块）上。对调之后，从背后看过去展示的才是 [1,1]
     那块真正的图案。
     如果哪天发现图案左右反了或者画到了内侧，先删掉这个函数试试。 */
  function swapZRects(faces) {
    const front = faces.find((f) => f.normal[2] > 0);
    const back = faces.find((f) => f.normal[2] < 0);
    const swap = front.rect;
    front.rect = back.rect;
    back.rect = swap;
    return faces;
  }

  /* 披风是垂下来的，不是一块贴在背上的平板：让它绕【顶端挂点】旋转。
     绕脚底转会把整片披风平移到身体外面去，所以挂点是关键。 */
  function tiltFromTop(corners, center, halfHeight, angle) {
    const cos = Math.cos(angle), sin = Math.sin(angle);
    const pivotY = center[1] + halfHeight;
    const pivotZ = center[2];

    return corners.map(([x, y, z]) => {
      const dy = y + center[1] - pivotY;
      const dz = z + center[2] - pivotZ;
      return [x + center[0], pivotY + dy * cos - dz * sin, pivotZ + dy * sin + dz * cos];
    });
  }

  /* 覆盖层（帽子 / 外套 / 袖子 / 裤腿）：同一个长方体各轴向外扩 0.5 再画一遍 */
  function inflate(corners, center, amount) {
    return corners.map(([x, y, z]) => [
      center[0] + x + (x === 0 ? 0 : Math.sign(x)) * amount,
      center[1] + y + (y === 0 ? 0 : Math.sign(y)) * amount,
      center[2] + z + (z === 0 ? 0 : Math.sign(z)) * amount,
    ]);
  }

  /* ---------------------------------------------------------- 贴在平面上 */

  function paintFace(ctx, image, rect, quad) {
    const [tl, tr, br, bl] = quad;
    const [u, v, w, h] = rect;

    // 目标是个平行四边形，用 TL、TR、BL 三点就能定出仿射矩阵
    const a = (tr[0] - tl[0]) / w;
    const b = (tr[1] - tl[1]) / w;
    const c = (bl[0] - tl[0]) / h;
    const d = (bl[1] - tl[1]) / h;

    ctx.save();
    ctx.beginPath();
    ctx.moveTo(tl[0], tl[1]);
    ctx.lineTo(tr[0], tr[1]);
    ctx.lineTo(br[0], br[1]);
    ctx.lineTo(bl[0], bl[1]);
    ctx.closePath();
    ctx.clip();
    ctx.setTransform(a, b, c, d, tl[0], tl[1]);
    // 比矩形稍大一点点，盖住相邻面之间的发丝缝
    ctx.drawImage(image, u, v, w, h, -0.35, -0.35, w + 0.7, h + 0.7);
    ctx.restore();
  }

  /* ---------------------------------------------------------- 3D 模型 */

  function renderModel(canvas, options) {
    const opts = options || {};
    const skin = opts.skin || null;
    const cape = opts.cape || null;
    const variant = (opts.variant || "CLASSIC").toUpperCase();
    const rotation = opts.rotation || 0;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    /* ！整段渲染都在【设备像素】坐标系里做：
       setTransform 会直接覆盖 ctx 上的缩放，所以不能用 ctx.scale(dpr, dpr)
       那一套，而是把 dpr 乘进 canvas 尺寸和投影缩放里。 */
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const cssWidth = canvas.clientWidth || 160;
    const cssHeight = canvas.clientHeight || 200;
    const width = Math.max(1, Math.round(cssWidth * dpr));
    const height = Math.max(1, Math.round(cssHeight * dpr));

    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.imageSmoothingEnabled = false;
    if (!skin) return;

    const legacy = skin.naturalHeight <= 32; // 2014 年之前的 64×32 老皮肤
    const parts = buildParts(variant);
    // 老皮肤没有独立的左臂 / 左腿，用右臂右腿的贴图镜像过来
    const mirrored = legacy ? { leftArm: "rightArm", leftLeg: "rightLeg" } : {};

    const scale = Math.min(width / 26, height / 36);
    const project = makeProjector(rotation, scale, width / 2, height / 2 + 15 * scale);

    const queue = [];

    parts.forEach((part) => {
      const source = parts.find((p) => p.key === (mirrored[part.key] || part.key)) || part;
      const flip = Boolean(mirrored[part.key]);

      boxFaces(source.uv, source.size).forEach((face) => {
        if (!facesCamera(face.normal, rotation)) return;
        let corners = translate(face.corners, part.center);
        if (flip) corners = [corners[1], corners[0], corners[3], corners[2]];
        const quad = corners.map(project);
        queue.push({
          image: skin,
          rect: face.rect,
          quad,
          depth: (quad[0][2] + quad[1][2] + quad[2][2] + quad[3][2]) / 4,
        });
      });

      // 覆盖层：老皮肤只有帽子那一层，没有外套 / 袖子 / 裤腿
      if (part.key !== "head" && legacy) return;

      boxFaces(source.over, source.size).forEach((face) => {
        if (!facesCamera(face.normal, rotation)) return;
        let corners = inflate(face.corners, part.center, 0.5);
        if (flip) corners = [corners[1], corners[0], corners[3], corners[2]];
        const quad = corners.map(project);
        queue.push({
          image: skin,
          rect: face.rect,
          quad,
          // 覆盖层永远压在同一个面的基础层之上
          depth: (quad[0][2] + quad[1][2] + quad[2][2] + quad[3][2]) / 4 + 0.001,
        });
      });
    });

    /* 披风：和皮肤一样排进同一个队列，靠 image 字段区分用哪张图。
       位置在身体后面，深度自然比躯干小，画家算法会先画它、再用躯干盖上。 */
    if (cape) {
      const part = capePart();
      swapZRects(boxFaces(part.uv, part.size)).forEach((face) => {
        if (!facesCamera(face.normal, rotation)) return;
        const quad = tiltFromTop(
          face.corners, part.center, part.size[1] / 2, CAPE_TILT
        ).map(project);
        queue.push({
          image: cape,
          rect: face.rect,
          quad,
          depth: (quad[0][2] + quad[1][2] + quad[2][2] + quad[3][2]) / 4,
        });
      });
    }

    // 画家算法：深度小的（远的）先画
    queue.sort((left, right) => left.depth - right.depth);
    queue.forEach((item) => paintFace(ctx, item.image, item.rect, item.quad));

    ctx.setTransform(1, 0, 0, 1, 0, 0);
  }

  /* ---------------------------------------------------------- 只画头（当头像用） */

  function renderHead(canvas, options) {
    const skin = (options || {}).skin || null;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const size = Math.max(1, Math.round((canvas.clientWidth || 40) * dpr));
    if (canvas.width !== size || canvas.height !== size) {
      canvas.width = size;
      canvas.height = size;
    }

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, size, size);
    ctx.imageSmoothingEnabled = false;
    if (!skin) return;

    // 头部正面 (8,8) + 帽子层 (40,8)，各 8×8 像素
    ctx.drawImage(skin, 8, 8, 8, 8, 0, 0, size, size);
    ctx.drawImage(skin, 40, 8, 8, 8, 0, 0, size, size);
  }

  /* ---------------------------------------------------------- 拖动交互 */

  function prefersReducedMotion() {
    return Boolean(
      window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches,
    );
  }

  function bindDrag(entry) {
    const canvas = entry.canvas;
    let pointerId = null;
    let lastX = 0;

    canvas.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 && event.pointerType === "mouse") return;
      pointerId = event.pointerId;
      lastX = event.clientX;
      entry.dragging = true;
      entry.velocity = 0;
      entry.touched = true;
      canvas.setPointerCapture(pointerId);
      canvas.classList.add("is-grabbing");
      // ！这里千万不要 preventDefault()：
      //   在 pointerdown 上阻止默认行为会连带抑制浏览器派发的兼容鼠标事件
      //   （mousedown/mouseup/click/dblclick），双击回正就永远收不到事件。
      //   拖动时的文字选中由 CSS 的 user-select: none 处理。
      ensureLoop();
    });

    canvas.addEventListener("pointermove", (event) => {
      if (!entry.dragging || event.pointerId !== pointerId) return;
      const delta = event.clientX - lastX;
      lastX = event.clientX;
      const step = delta * DRAG_SPEED;
      entry.rotation += step;
      // 用最近一帧的位移当作角速度，松手后就能顺势滑一段
      entry.velocity = Math.max(-MAX_VELOCITY, Math.min(MAX_VELOCITY, step));
      paint(entry);
    });

    const release = (event) => {
      if (!entry.dragging || (pointerId !== null && event.pointerId !== pointerId)) return;
      entry.dragging = false;
      canvas.classList.remove("is-grabbing");
      if (pointerId !== null && canvas.hasPointerCapture(pointerId)) {
        canvas.releasePointerCapture(pointerId);
      }
      pointerId = null;
      ensureLoop();
    };

    canvas.addEventListener("pointerup", release);
    canvas.addEventListener("pointercancel", release);

    // 双击回到正面，省得用户转歪了找不到北
    canvas.addEventListener("dblclick", () => {
      entry.velocity = 0;
      animateTo(entry, 0);
    });

    // 拖动时不要选中页面上的文字
    canvas.addEventListener("selectstart", (event) => event.preventDefault());
  }

  /* 平滑地转回某个角度 */
  function animateTo(entry, target) {
    const start = entry.rotation;
    const delta = target - start;
    const begin = performance.now();
    entry.tween = () => {
      const t = Math.min(1, (performance.now() - begin) / 260);
      const eased = 1 - Math.pow(1 - t, 3);
      entry.rotation = start + delta * eased;
      return t < 1;
    };
    ensureLoop();
  }

  /* ---------------------------------------------------------- 渲染循环 */

  function paint(entry) {
    if (entry.skin) {
      renderModel(entry.canvas, {
        skin: entry.skin,
        cape: entry.cape,
        variant: entry.variant,
        rotation: entry.rotation,
      });
    }
  }

  function step(entry) {
    let busy = false;

    if (entry.tween) {
      const alive = entry.tween();
      if (!alive) entry.tween = null;
      busy = true;
    }

    if (entry.dragging) return true;

    if (Math.abs(entry.velocity) > 0.0008) {
      entry.rotation += entry.velocity;
      entry.velocity *= FRICTION;
      busy = true;
    } else if (entry.velocity !== 0) {
      entry.velocity = 0;
      busy = true;
    }

    // 用户没碰过它，且没有开启「减弱动态效果」时，让它自己慢慢转
    if (!entry.touched && entry.spin && !prefersReducedMotion()) {
      entry.rotation += IDLE_SPIN / 60;
      busy = true;
    }

    if (busy) paint(entry);
    return busy;
  }

  function loop() {
    let alive = false;
    // 标签页在后台时不推进动画，让循环自然停下；
    // 页面重新可见时由 visibilitychange 再把它拉起来。
    // 注意：可见性判断放在循环内部而不是 ensureLoop()，
    //       否则后台标签页里「拖拽结束后双击回正」会因为循环取不到启动机会而完全不生效。
    if (document.visibilityState !== "hidden") {
      Array.from(registry).forEach((entry) => {
        if (!entry.canvas.isConnected) {
          registry.delete(entry);
          return;
        }
        if (entry.canvas.clientWidth === 0) return; // 还没显示出来，等下一帧
        if (!entry.skin) return;
        if (step(entry)) alive = true;
      });
    }
    rafId = alive ? requestAnimationFrame(loop) : 0;
  }

  function ensureLoop() {
    if (!rafId) rafId = requestAnimationFrame(loop);
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") {
      if (rafId) cancelAnimationFrame(rafId);
      rafId = 0;
    } else {
      ensureLoop();
    }
  });

  /* ---------------------------------------------------------- 自动挂载 */

  /**
   * 扫描 root 下带这些属性的 canvas 并画出来：
   *   <canvas data-skin-model="/api/media/xx.png" data-cape="/api/media/yy.png" data-drag>  // 3D，可拖拽
   *   <canvas data-skin-head="/api/media/xx.png">              // 平面头部当头像
   *.
   * ！披风不是依赖流程：披风图没加载到（或者压根没绑微软账号）
   *   就渲染一个没披风的玩家，不是报错。所以两条 loadTexture 各管各的。
   */
  function hydrate(root) {
    const scope = root || document;

    Array.from(scope.querySelectorAll("canvas[data-skin-model], canvas[data-skin-head]")).forEach((canvas) => {
      const headOnly = canvas.hasAttribute("data-skin-head");
      const url = canvas.getAttribute(headOnly ? "data-skin-head" : "data-skin-model") || "";
      const capeUrl = canvas.getAttribute("data-cape") || "";
      const variant = (canvas.getAttribute("data-variant") || "CLASSIC").toUpperCase();

      // 同一个 canvas 重新渲染时，先把旧的注册项丢掉
      Array.from(registry).forEach((old) => {
        if (old.canvas === canvas) registry.delete(old);
      });

      const entry = {
        canvas,
        headOnly,
        skin: null,
        cape: null,
        variant,
        rotation: parseFloat(canvas.getAttribute("data-angle") || "0") || 0,
        velocity: 0,
        dragging: false,
        touched: false,
        tween: null,
        spin: canvas.hasAttribute("data-spin"),
      };
      registry.add(entry);

      if (!headOnly) bindDrag(entry);

      // 平面头像用不到披风，没必要多下载一个文件
      if (!headOnly && capeUrl) {
        loadTexture(capeUrl).then((image) => {
          // ！先赋值再重画，但不要 ensureLoop：披风是静态的，
          //   不需要为了它多跑一轮动画。
          entry.cape = image;
          if (image && entry.skin) paint(entry);
        });
      }

      loadTexture(url).then((image) => {
        entry.skin = image;
        if (!image) {
          const ctx = canvas.getContext("2d");
          if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height);
          // ！皮肤文件丢了（换服务器时漏拷、被清理掉）就只剩一块空白方块，
          //   比没有头像还难看。这里只负责报信，怎么回退由 app.js 决定 ——
          //   这一层不认识「像素脸」那套东西，不该把它的画法抄过来。
          canvas.dispatchEvent(new CustomEvent("mcfr:skin-missing", { bubbles: true }));
          return;
        }
        if (headOnly) renderHead(canvas, { skin: image });
        else paint(entry);
        ensureLoop();
      });
    });
  }

  return { hydrate, loadTexture, renderModel, renderHead };
})();
