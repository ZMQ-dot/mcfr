/* =========================================================================
   mcfr · MC 抽象图标
   -------------------------------------------------------------------------
   把《我的世界》里的物件抽象成图标。三条自律：

   1. **只借形，不借色。**
      所有图标一律用 `currentColor` + 不同透明度来表达明暗面。
      于是同一个图标在「未解锁」时是一块灰、在「已解锁」时是绿的 / 金的 /
      钻石青的 —— 不用为每种状态各画一套图。颜色语义仍然由 CSS 令牌决定。

   2. **两套造型语言，各自统一。**
      · 方块类（草方块、金锭、绿宝石、堆叠的方块）用**等距投影**，
        三个面按上 1.0 / 右 0.70 / 左 0.48 的明度分层 ——
        这正是草方块「顶面受光、侧面背光」的读法。
      · 符号类（心、盾、王冠、书、月亮）用**平面形 + 挤压的侧影**
        （一个偏移 1.5px 的低透明度副本垫在后面），既有厚度又不至于在
        28px 的尺寸下糊成一团。
      两套都用平滑边缘，不做像素锯齿 —— 像素感由布局和字体承担，
      图标再像素化就只剩噪点了。

   3. **几何全部算出来，不手写坐标。**
      iso() 负责等距立方体，rhombus() 负责菱形面。改尺寸只改一个数字，
      十二个图标不会各歪各的。

   这些图标是**装饰**：全部带 aria-hidden，语义由旁边的文字承担。
   ========================================================================= */

(() => {
  "use strict";

  /* ---------------------------------------------------------------- 几何 */

  const fx = (n) => Math.round(n * 100) / 100;

  /** 等距菱形（俯视面）：中心 (cx, cy)，半宽 w，半高 h */
  function rhombus(cx, cy, w, h) {
    return `M${fx(cx)} ${fx(cy - h)}L${fx(cx + w)} ${fx(cy)}L${fx(cx)} ${fx(cy + h)}L${fx(cx - w)} ${fx(cy)}Z`;
  }

  /**
   * 等距立方体。返回三个面的路径。
   *   w  顶面半宽      h  顶面半高（等距下约等于 w 的 0.56 倍）
   *   v  侧面高度      cx/cy  顶面中心
   */
  function iso(cx, cy, w, h, v) {
    return {
      top: rhombus(cx, cy, w, h),
      left: `M${fx(cx - w)} ${fx(cy)}L${fx(cx)} ${fx(cy + h)}L${fx(cx)} ${fx(cy + h + v)}L${fx(cx - w)} ${fx(cy + v)}Z`,
      right: `M${fx(cx + w)} ${fx(cy)}L${fx(cx)} ${fx(cy + h)}L${fx(cx)} ${fx(cy + h + v)}L${fx(cx + w)} ${fx(cy + v)}Z`,
    };
  }

  /** 把立方体三个面铺成 SVG：先侧面后顶面，压掉接缝的毛边 */
  function solid(box, op = {}) {
    const { top = 1, right = 0.7, left = 0.48 } = op;
    return (
      `<path d="${box.left}" fill="currentColor" opacity="${left}"/>` +
      `<path d="${box.right}" fill="currentColor" opacity="${right}"/>` +
      `<path d="${box.top}" fill="currentColor" opacity="${top}"/>`
    );
  }

  /**
   * 平面形 + 挤压侧影。偏移副本垫在底下当作「厚度」。
   * 符号类图标统一走这个，保证它们看起来是一套的。
   */
  function extrude(inner, dx = 1.5, dy = 1.5, op = 0.38) {
    return `<g opacity="${op}" transform="translate(${dx} ${dy})">${inner}</g>${inner}`;
  }

  const svg = (body, viewBox = "0 0 24 24") =>
    `<svg class="mcico" viewBox="${viewBox}" width="100%" height="100%" aria-hidden="true" focusable="false">${body}</svg>`;

  /* ---------------------------------------------------------------- 方块类 */

  /** 草方块：一个素立方体。「第一块你放下的方块」 */
  const BLOCK = solid(iso(12, 7.2, 8.2, 4.6, 7.2));

  /** 三块小方块摆成一个三角：小队。
      ！不能沿用「两块并排」那个造型 —— 「五人小队」「双版本通吃」
        共用一个形状时，只能靠颜色区分，扫一眼根本分不出来。 */
  const SQUAD =
    solid(iso(12, 6.0, 4.2, 2.4, 4.4)) +
    solid(iso(7.4, 14.6, 4.2, 2.4, 4.4)) +
    solid(iso(16.6, 14.6, 4.2, 2.4, 4.4));

  /** 两个交叠的方块：两个版本、两拨人。
      刻意**不走等距**，和 SQUAD 拉开距离 —— 同样是「两个东西」，
      一个是立方体、一个是平面块，尺寸小的时侯才不会认错。 */
  const DUO = extrude(
    `<rect x="3.4" y="7.4" width="10.2" height="10.2" rx="2.4" fill="currentColor" opacity=".55"/>` +
      `<rect x="10.4" y="7.4" width="10.2" height="10.2" rx="2.4" fill="currentColor"/>`,
    1.2,
    1.2,
    0.3
  );

  /** T 恤：上传自己的皮肤。比「一张脸」更能说清「换了一身衣服」。 */
  const VEST = extrude(
    `<path fill="currentColor" d="M9 3.4L3.6 6.2l1.9 3.6 2.3-1.2v11.4h8.4V8.6l2.3 1.2 1.9-3.6L15 3.4l-1.4 1.6h-3.2z"/>`,
    1.1,
    1.1,
    0.3
  );

  /** 绿宝石：桌面上一切交易与交换的象征 */
  const GEM =
    `<path d="M5.4 8.4L12 12.2L12 19.4Z" fill="currentColor" opacity=".48"/>` +
    `<path d="M18.6 8.4L12 12.2L12 19.4Z" fill="currentColor" opacity=".7"/>` +
    `<path d="${rhombus(12, 8.4, 6.6, 3.8)}" fill="currentColor"/>` +
    `<path d="${rhombus(10.4, 7.7, 2.1, 1.2)}" fill="currentColor" opacity=".55"/>`;

  /** 物品栏式的四宫格：多样、收集。
      这一个刻意不走等距 —— 四个小格子摊开看，四个方块挤在一起就分不清了。
      造型语言仍然是「方块 + 明暗分层」，不会突然変成另一个体系。 */
  const GRID = extrude(
    `<rect x="4" y="4" width="7.4" height="7.4" rx="1.7" fill="currentColor"/>` +
      `<rect x="12.6" y="4" width="7.4" height="7.4" rx="1.7" fill="currentColor" opacity=".62"/>` +
      `<rect x="4" y="12.6" width="7.4" height="7.4" rx="1.7" fill="currentColor" opacity=".62"/>` +
      `<rect x="12.6" y="12.6" width="7.4" height="7.4" rx="1.7" fill="currentColor"/>`,
    1.3,
    1.3,
    0.3
  );

  /* ---------------------------------------------------------------- 符号类 */

  /** 立牌：把信息挂出来（预置联系方式） */
  const SIGN =
    solid(iso(12, 12.0, 1.5, 0.85, 6.0)) +
    solid(iso(12, 8.2, 5.6, 3.15, 2.0)) +
    `<path d="${rhombus(12, 8.2, 3.4, 1.9)}" fill="none" stroke="currentColor" stroke-opacity=".5" stroke-width="1"/>`;

  const HEART = extrude(
    `<path fill="currentColor" d="M12 21S3.5 15.4 3.5 9.6C3.5 6.5 5.9 4.4 8.6 4.4c1.6 0 2.8.8 3.4 1.7.6-.9 1.8-1.7 3.4-1.7 2.7 0 5.1 2.1 5.1 5.2C20.5 15.4 12 21 12 21Z"/>`
  );

  const SHIELD = extrude(
    `<path fill="currentColor" d="M12 3.2l7.4 3v6c0 4.2-3.2 7.4-7.4 8.6-4.2-1.2-7.4-4.4-7.4-8.6v-6z"/>`
  );

  /** 名片：自我介绍。左边一个小头像位，右边两行字。 */
  const IDCARD = extrude(
    `<rect x="3.4" y="6.4" width="17.2" height="11.2" rx="1.8" fill="none" stroke="currentColor" stroke-width="1.8"/>` +
      `<rect x="6.2" y="9.4" width="4.4" height="5.2" rx="1" fill="currentColor"/>` +
      `<g stroke="currentColor" stroke-width="1.5" stroke-linecap="round">` +
      `<path d="M13 10.4h5"/><path d="M13 13.6h3.4"/>` +
      `</g>`,
    1.1,
    1.1,
    0.3
  );

  /** 写字板 + 两个勾：一项一项都填上了。 */
  const CHECKLIST = extrude(
    `<rect x="5" y="4.6" width="14" height="15.8" rx="2" fill="none" stroke="currentColor" stroke-width="1.8"/>` +
      `<rect x="9" y="3" width="6" height="3.2" rx="1.2" fill="currentColor"/>` +
      `<g stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" fill="none">` +
      `<path d="M8.2 10.6l1.4 1.4 2.4-2.6"/>` +
      `<path d="M8.2 16l1.4 1.4 2.4-2.6"/>` +
      `<path d="M13.8 11.2h3"/><path d="M13.8 16.6h3"/>` +
      `</g>`,
    1.1,
    1.1,
    0.3
  );

  /** 铅笔：写下给对方的备注。 */
  const PEN = extrude(
    `<path fill="currentColor" d="M17.2 3.6l3.2 3.2-2 2-3.2-3.2z"/>` +
      `<path fill="currentColor" d="M14.4 6.4l3.2 3.2-8.2 8.2-4.2 1 1-4.2z"/>`,
    1.1,
    1.1,
    0.3
  );

  /** 纸飞机：把第一句话发出去。 */
  const PLANE = extrude(
    `<path fill="currentColor" d="M3.2 11.2L20.8 4l-6.6 16.6-2.6-6.2z"/>` +
      `<path fill="currentColor" opacity=".45" d="M11.6 14.4l9.2-10.4-6.6 16.6z"/>`,
    1.2,
    1.2,
    0.3
  );

  /** 对话气泡（里面一个感叹号）：有话直说。
      和纸飞机都是「说话」，但一个是在发出去、一个是在说出来，
      形状差得足够远 —— 同属社交类的那几个成就一眼能分开。 */
  const BUBBLE = extrude(
    `<path d="M5.6 4.8h12.8a2 2 0 012 2v7.4a2 2 0 01-2 2h-5.6l-4.6 3.4v-3.4H5.6a2 2 0 01-2-2V6.8a2 2 0 012-2z" ` +
      `fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>` +
      `<path fill="currentColor" d="M11.1 7.6h1.8v4.6h-1.8z"/>` +
      `<circle cx="12" cy="14.2" r="1.1" fill="currentColor"/>`,
    1.1,
    1.1,
    0.28
  );

  /** 奖章（绶带 + 星芯）：联络大师。比王冠低一档，比小队高一档。 */
  const MEDAL = extrude(
    `<path fill="currentColor" opacity=".55" d="M8.6 2.8l3.4 6.4-3 1.6-3.4-6.4z"/>` +
      `<path fill="currentColor" opacity=".55" d="M15.4 2.8L12 9.2l3 1.6 3.4-6.4z"/>` +
      `<circle cx="12" cy="15.6" r="5.6" fill="none" stroke="currentColor" stroke-width="1.9"/>` +
      `<path fill="currentColor" d="M12 12.4l1.1 2.3 2.5.3-1.9 1.7.5 2.5-2.2-1.2-2.2 1.2.5-2.5-1.9-1.7 2.5-.3z"/>`,
    1.1,
    1.1,
    0.28
  );

  /** 漏斗：筛掉不合的推荐。 */
  const FUNNEL = extrude(
    `<path fill="currentColor" d="M3.6 4.2h16.8l-6.2 7.4v6.6l-4.4 2.2v-8.8z"/>`,
    1.2,
    1.2,
    0.3
  );

  /** 折页地图：看得多、走得远。与指南针成对但不相同。 */
  const MAP = extrude(
    `<path fill="currentColor" opacity=".5" d="M3.4 6.2l5.2-2.4v14l-5.2 2.4z"/>` +
      `<path fill="currentColor" d="M8.6 3.8l6.8 2.4v14l-6.8-2.4z"/>` +
      `<path fill="currentColor" opacity=".5" d="M15.4 6.2l5.2-2.4v14l-5.2 2.4z"/>`,
    1.1,
    1.1,
    0.3
  );

  /* 星形。几何算出来，免得手写十个点写歪。 */
  function starPath(cx, cy, outer, inner) {
    const points = [];
    for (let i = 0; i < 10; i += 1) {
      const r = i % 2 === 0 ? outer : inner;
      const angle = -Math.PI / 2 + (i * Math.PI) / 5;
      points.push(`${fx(cx + r * Math.cos(angle))} ${fx(cy + r * Math.sin(angle))}`);
    }
    return `M${points.join("L")}Z`;
  }

  /** 一颗星：人气之星 */
  const STAR = extrude(
    `<path fill="currentColor" d="${starPath(12, 12.4, 8.4, 3.6)}"/>`,
    1.1,
    1.1,
    0.3
  );

  /** 三颗星：社区明星。与「一颗星」是同一条升级线，凭**数量**区分，\n      不再靠颜色 —— 颜色是一样的色，数量是看得见的。 */
  const STARS3 = extrude(
    `<path fill="currentColor" d="${starPath(12, 8.8, 6.4, 2.8)}"/>` +
      `<path fill="currentColor" opacity=".6" d="${starPath(6.2, 17.6, 4.2, 1.8)}"/>` +
      `<path fill="currentColor" opacity=".6" d="${starPath(17.8, 17.6, 4.2, 1.8)}"/>`,
    1.1,
    1.1,
    0.3
  );

  /** 三级上升的柱：连着来了三天。 */
  const STEPS = extrude(
    `<g fill="currentColor">` +
      `<rect x="3.8" y="13.4" width="4.4" height="7" rx="1.1" opacity=".5"/>` +
      `<rect x="9.8" y="9.4" width="4.4" height="11" rx="1.1" opacity=".75"/>` +
      `<rect x="15.8" y="5.4" width="4.4" height="15" rx="1.1"/>` +
      `</g>`,
    1.1,
    1.1,
    0.3
  );

  /** 火苗：连着来了一周 —— 「连续」的通用符号。 */
  const FLAME = extrude(
    `<path fill="currentColor" d="M12 2.6c3.6 4 6.4 6.6 6.4 10.4a6.4 6.4 0 01-12.8 0c0-2.2 1-4 2.4-6 .4 1.2 1.2 2 2.2 2.4-.4-2.8 0-5 1.8-6.8z"/>`,
    1.1,
    1.1,
    0.3
  );

  /** 十二个小格子：来了整整好些天。\n      ！不能用日历框：日历已经给了「周末玩家」，\n        两个都是日历框时就只能看颜色了。这里改用格子密度说话。 */
  const MONTHGRID = extrude(
    `<g fill="currentColor">` +
      Array.from({ length: 12 }, (_, i) => {
        const col = i % 4;
        const row = Math.floor(i / 4);
        const op = i % 3 === 2 ? ".5" : "1";
        return `<rect x="${fx(3.8 + col * 4.6)}" y="${fx(6.6 + row * 4.6)}" ` +
          `width="3.4" height="3.4" rx=".9" opacity="${op}"/>`;
      }).join("") +
      `</g>`,
    1.0,
    1.0,
    0.28
  );

  /** 王冠：最高一档的社交成就（伙伴遍天下）。 */
  const CROWN = extrude(
    `<path fill="currentColor" d="M4.2 17.8L3 7.0l5.2 3.4L12 4.8l3.8 5.6L21 7.0l-1.2 10.8z"/>`,
    1.1,
    1.1,
    0.3
  );

  /** 指南针：外圈描边 + 双色指针。探索者的初级形态。 */
  const COMPASS = extrude(
    `<circle cx="12" cy="12" r="8.4" fill="none" stroke="currentColor" stroke-width="2"/>` +
      `<path fill="currentColor" d="M12 5.6l2.6 6.4L12 18.4z"/>` +
      `<path fill="currentColor" opacity=".45" d="M12 5.6L9.4 12 12 18.4z"/>`,
    1.2,
    1.2,
    0.3
  );

  /** 月亮：深夜建筑师 */
  const MOON = extrude(
    `<path fill="currentColor" d="M15.4 4.2a8.6 8.6 0 100 15.6 7 7 0 010-15.6z"/>` +
      `<circle cx="19.2" cy="7.2" r="1.1" fill="currentColor" opacity=".7"/>` +
      `<circle cx="20.4" cy="12.4" r=".8" fill="currentColor" opacity=".5"/>`,
    1.2,
    1.2,
    0.3
  );

  /** 太阳：早起的鸟。与月亮成对，所以描边粗细、挤压量都对齐。 */
  const SUN = extrude(
    `<circle cx="12" cy="12" r="4.4" fill="currentColor"/>` +
      `<g stroke="currentColor" stroke-width="1.9" stroke-linecap="round">` +
      `<path d="M12 2.8v2.4"/><path d="M12 18.8v2.4"/>` +
      `<path d="M2.8 12h2.4"/><path d="M18.8 12h2.4"/>` +
      `<path d="M5.6 5.6l1.7 1.7"/><path d="M16.7 16.7l1.7 1.7"/>` +
      `<path d="M18.4 5.6l-1.7 1.7"/><path d="M7.3 16.7l-1.7 1.7"/>` +
      `</g>`,
    1.2,
    1.2,
    0.3
  );

  /** 日历：周末玩家专用。
      ！日历用**描边**而不是实心块：实心的话格子只能靠降低不透明度区分，
        而 30px 下那点差别完全看不出来，整张图会变成一块糊掉的方形。
      ！v0.29.2 之前这一个形状同时兼着「三日之约 / 一周不缺席 / 月度常客」，
        四个成就长一模一样，只能靠颜色区分 —— 现在只留这一处。 */
  const CALENDAR = extrude(
    `<rect x="3.6" y="5.4" width="16.8" height="15" rx="2.6" fill="none" stroke="currentColor" stroke-width="1.9"/>` +
      `<path d="M3.6 10h16.8" stroke="currentColor" stroke-width="1.9"/>` +
      `<path d="M8.2 3.4v3.4" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>` +
      `<path d="M15.8 3.4v3.4" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>` +
      `<g fill="currentColor">` +
      `<rect x="6.6" y="12.4" width="3" height="3" rx=".7"/>` +
      `<rect x="10.5" y="12.4" width="3" height="3" rx=".7"/>` +
      `<rect x="14.4" y="12.4" width="3" height="3" rx=".7"/>` +
      `</g>`,
    1.2,
    1.2,
    0.3
  );

  /** 像素脸：独一无二（上传自定义头像）。
      ！「皮肤设计师」不再用它 —— 两个成就都用一张脸的时候，
        只能靠颜色区分。皮肤那个改用 VEST（一件衣服）。 */
  const FACE = extrude(
    `<rect x="4.4" y="4.4" width="15.2" height="15.2" rx="3.4" fill="none" stroke="currentColor" stroke-width="1.9"/>` +
      `<g fill="currentColor">` +
      `<rect x="8.4" y="9.2" width="2.4" height="2.8" rx=".6"/>` +
      `<rect x="13.2" y="9.2" width="2.4" height="2.8" rx=".6"/>` +
      `</g>` +
      `<path d="M9 15.2c1.8 1.6 4.2 1.6 6 0" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>`,
    1.2,
    1.2,
    0.3
  );

  /* ---------------------------------------------------------------- 成就映射 */

  // shape: 造型  tone: 解锁后的颜色语义（对应设计文档的语义色）
  //   grass   品牌绿 —— 成长、社区、积极
  //   diamond 钻石青 —— 探索、交互
  //   gold    金锭黄 —— 最高成就、稀有奖励
  //   nether  下界紫 —— 特殊、少见
  //
  // ！v0.29.2 起：**28 个成就，28 个造型，一个都不重复。**
  //   在这之前只有 15 个造型，日历一个形状兼了 4 个成就、
  //   书本兼了 3 个 —— 墙上一排看过去全是一样的图，只能靠颜色分辨，
  //   等于没画。新增成就时请照这条规矩办：**先给它一个自己的形状**，
  //   实在想不出，改造型也不要和别人的撞。
  const BADGES = {
    // ---- 起步：把资料填起来 ----
    first_step:      { shape: BLOCK,     tone: "grass" },   // 第一块你放下的方块
    self_intro:      { shape: IDCARD,    tone: "diamond" }, // 一张写着自己的名片
    all_rounder:     { shape: GRID,      tone: "grass" },   // 每个格子都沾一点
    full_profile:    { shape: CHECKLIST, tone: "grass" },   // 一项一项都打上了勾
    open_book:       { shape: SIGN,      tone: "diamond" }, // 把信息立在门口
    thoughtful:      { shape: PEN,       tone: "grass" },   // 提笔写一句备注

    // ---- 形象 ----
    dressed_up:      { shape: FACE,      tone: "grass" },   // 换掉默认的那张脸
    skin_maker:      { shape: VEST,      tone: "diamond" }, // 给自己做一身衣服

    // ---- 社交 ----
    hello_world:     { shape: PLANE,     tone: "diamond" }, // 第一句话飞出去
    first_contact:   { shape: GEM,       tone: "diamond" }, // 第一次交换
    squad_five:      { shape: SQUAD,     tone: "grass" },   // 凑够一小队
    old_friends:     { shape: MEDAL,     tone: "gold" },    // 十个人，够挂一枚奖章
    friends_plus:    { shape: CROWN,     tone: "nether" },  // 二十五个，加冕
    good_neighbor:   { shape: HEART,     tone: "grass" },   // 热心邻居
    picker:          { shape: FUNNEL,    tone: "nether" },  // 不合适的筛掉

    // ---- 逛一逛 ----
    explorer:        { shape: COMPASS,   tone: "diamond" }, // 出门带指南针
    explorer_plus:   { shape: MAP,       tone: "gold" },    // 走得多了，地图摊开
    popular:         { shape: STAR,      tone: "gold" },    // 一颗星
    popular_plus:    { shape: STARS3,    tone: "nether" },  // 三颗星

    // ---- 登录节律 ----
    night_architect: { shape: MOON,      tone: "nether" },  // 深夜建筑师
    early_bird:      { shape: SUN,       tone: "gold" },    // 早起的鸟
    weekend_player:  { shape: CALENDAR,  tone: "grass" },   // 周末才有空
    three_days:      { shape: STEPS,     tone: "grass" },   // 连着来了三天
    week_streak:     { shape: FLAME,     tone: "diamond" }, // 一周不断，火苗
    monthly:         { shape: MONTHGRID, tone: "gold" },    // 三十个日子

    // ---- 社区 ----
    safe_guard:      { shape: SHIELD,    tone: "grass" },   // 安全卫士
    feedback_giver:  { shape: BUBBLE,    tone: "grass" },   // 有话直说
    both_editions:   { shape: DUO,       tone: "diamond" }, // 两个版本都端着
  };

  // tone 名字 → 设计令牌名。集中在这里，CSS 里就不用再写四组选择器了：
  // 图标、弹窗、标签三处共用同一个行内变量 --tone。
  const TONE_VARS = {
    grass: "grass",
    diamond: "diamond",
    gold: "gold-glyph",     // 金色文字专用那一档，比 --gold 深，浅底上读得清
    nether: "nether",
  };

  /* ---------------------------------------------------------------- 侧栏图标 */

  // 线条图标：24 格，1.7px 描边，圆角接头。尺寸小，细节一律砍掉。
  const LINE = {
    discover:
      `<circle cx="12" cy="12" r="8.6" fill="none" stroke="currentColor" stroke-width="1.7"/>` +
      `<path d="M15.6 8.4l-2 5.2-5.2 2 2-5.2z" fill="currentColor"/>`,
    partners:
      `<g stroke="currentColor" stroke-width="1.7" stroke-linecap="round" fill="none">` +
      `<path d="M5.6 5.6l9.8 9.8"/><path d="M18.4 5.6l-9.8 9.8"/>` +
      `<path d="M4.2 7l2.8-2.8"/>` +
      `<path d="M19.8 7l-2.8-2.8"/>` +
      `<path d="M13.2 16.4l4.4 4.4"/><path d="M10.8 16.4l-4.4 4.4"/>` +
      `</g>`,
    achievements:
      `<g stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" fill="none">` +
      `<path d="M7.4 4.2h9.2v4.6a4.6 4.6 0 01-9.2 0z"/>` +
      `<path d="M7.4 5.6H4.4v1.6a3 3 0 003 3"/>` +
      `<path d="M16.6 5.6h3v1.6a3 3 0 01-3 3"/>` +
      `<path d="M12 13.4v3.2"/><path d="M8.4 19.8h7.2"/>` +
      `</g>`,
    me:
      `<g stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" fill="none">` +
      `<path d="M4.6 6.2a1.8 1.8 0 011.8-1.8h11.2a1.8 1.8 0 011.8 1.8v11.6a1.8 1.8 0 01-1.8 1.8H6.4a1.8 1.8 0 01-1.8-1.8z"/>` +
      `<path d="M9 10.2v2.4"/><path d="M15 10.2v2.4"/>` +
      `<path d="M9.6 16.2h4.8"/>` +
      `</g>`,
    search:
      `<g stroke="currentColor" stroke-width="1.7" stroke-linecap="round" fill="none">` +
      `<circle cx="10.6" cy="10.6" r="5.9"/>` +
      `<path d="M15.05 15.05l4.35 4.35"/>` +
      `</g>`,
    mirror:
      `<g stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" fill="none">` +
      `<path d="M12 3.8v7.6"/>` +
      `<path d="M8.9 8.5L12 11.6l3.1-3.1"/>` +
      `<path d="M4.6 13.6v4.2a2 2 0 002 2h10.8a2 2 0 002-2v-4.2"/>` +
      `</g>`,
    // 一页纸（右上角折起来）+ 两道文字线。比「相框」更像「一篇写好的东西」。
    posts:
      `<g stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" fill="none">` +
      `<path d="M6.2 3.8h7.6l4 4v12.4H6.2z"/>` +
      `<path d="M13.8 3.8v4h4"/>` +
      `<path d="M9 12.4h6"/><path d="M9 15.8h6"/>` +
      `</g>`,
    // 信封。和「我的伙伴」的交剑区分得很开 —— 两个都是社交类，
    // 用相近的形状（比如两个人）会让侧栏一眼分不出来。
    messages:
      `<g stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" fill="none">` +
      `<rect x="3.8" y="5.6" width="16.4" height="12.8" rx="1.8"/>` +
      `<path d="M4.6 7.4l7.4 5.2 7.4-5.2"/>` +
      `</g>`,
  };

  /* ---------------------------------------------------------------- 对外接口 */

  window.McfrIcons = {
    /** 侧栏图标；认不出的 key 回退成一个方块，不会画成空白 */
    nav(key) {
      return this.line(key);
    },

    /** 通用线条图标。侧栏之外的 UI 小件（目前只有搜索框的放大镜）也走这里，
        免得 SVG 散落在 app.js 里 —— 那样改描边粗细得满仓库找。 */
    line(key) {
      const body = LINE[key] || `<rect x="5" y="5" width="14" height="14" rx="3" fill="currentColor"/>`;
      return svg(body);
    },

    /** 成就图标。fallback 是后端给的字符，图标缺失时退回去，不至于空一块 */
    badge(code, fallback = "◆") {
      const entry = BADGES[code];
      if (!entry) return fallback;
      return svg(entry.shape);
    },

    /** 解锁后应该用哪种语义色 */
    tone(code) {
      return (BADGES[code] && BADGES[code].tone) || "grass";
    },

    /** 直接写进行内 style 的颜色变量，例如 `--tone:var(--gold-glyph)`。
        用变量而不是给四个状态各写一个类：同一个图标还会出现在解锁弹窗
        和个人页的标签里，那两处都没有 .badge 这个祖先。 */
    toneStyle(code) {
      return `--tone:var(--${TONE_VARS[this.tone(code)]})`;
    },
  };
})();
