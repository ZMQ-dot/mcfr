/* =========================================================================
   mcfr · 富文本正文
   -------------------------------------------------------------------------
   把「文档 JSON」和 HTML 互相转换。编辑器和详情页共用这一份。

   一、为什么存 JSON 而不是 HTML
   ---------------------------
   编辑用的是浏览器的 contenteditable（Word 那种手感），但**存进数据库的
   不是 HTML，是一棵被校验过的结构化文档树**。

   对比一下两条路：
     · 存 HTML —— 服务端必须有一个 HTML 清洗器。它得认得所有危险的标签、
       属性、CSS 表达式、以及「看起来无害但浏览器会当脚本跑」的那些角落。
       写对很难，写漏一次就是存储型 XSS；而且本项目不许加依赖，
       手写清洗器正是最容易漏边界情况的那类代码。
     · 存 JSON —— 服务端只做 `in` 判断（颜色名在不在白名单、字号在不在
       白名单），渲染端是这里的固定模板 + 全量转义。**XSS 面为零**。

   所以这里的 `serialize()` 是整个方案的关键：它把 contenteditable
   吐出来的那棵乱树，收敛成后端认识的那几种块。

   二、文档模型
   -----------
     [ {type:"paragraph", align?:"center", runs:[run…]},
       {type:"heading",   level:2|3,     runs:[run…]},
       {type:"quote",     runs:[run…]},
       {type:"list",      ordered:bool,  items:[[run…]…]},
       {type:"image",     file, caption},
       {type:"file",      file, name, size} ]

     run = {text, bold?, italic?, underline?, strike?, color?, highlight?, size?}

   ！颜色和字号存的是【白名单里的名字】，不是数值。
     存死十六进制的话，浅色主题里配好的深红字换到深色主题就糊在暗背景上了。
     名字在渲染时映射到主题变量（见 app.css 的 .rt-* 规则），两个主题都对。
   ========================================================================= */

(() => {
  "use strict";

  /* 和 backend/posts.py 里的 RUN_COLORS / RUN_HIGHLIGHTS / RUN_SIZES 一一对应。
     ！改这里必须同时改那边 —— 服务端不认识的名字会被静默丢掉，
       表现就是「颜色点了没反应」，很难查。 */
  const COLORS = [
    ["red", "红"], ["gold", "金"], ["grass", "绿"],
    ["cyan", "青"], ["nether", "紫"], ["gray", "灰"],
  ];
  const HIGHLIGHTS = [
    ["yellow", "黄"], ["green", "绿"], ["cyan", "青"], ["pink", "粉"], ["gray", "灰"],
  ];
  const SIZES = [["small", "小"], ["large", "大"], ["huge", "特大"]];

  const FLAG_CLASS = {
    bold: "rt-b", italic: "rt-i", underline: "rt-u", strike: "rt-s",
  };
  const CLASS_FLAG = {
    "rt-b": "bold", "rt-i": "italic", "rt-u": "underline", "rt-s": "strike",
  };

  const ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ESC_MAP[ch]);

  const IMAGE_URL = (file) => `/api/media/${encodeURIComponent(file)}`;
  const FILE_URL = (file) => `/api/posts/attachment/${encodeURIComponent(file)}`;

  /* 人类可读的体积。附件卡片上要显示，让下载前心里有数。 */
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

  /* ================================================================
     一、渲染（文档 JSON → 安全 HTML）
     ================================================================ */

  function runHtml(run) {
    if (!run || !run.text) return "";
    // ！先转义再插 <br>，顺序绝不能反 —— 反了就是一个教科书级的 XSS。
    //   换行在 run 内部是 \n，渲染时变成 <br>。
    const inner = esc(run.text).replace(/\n/g, "<br>");

    const classes = [];
    for (const [flag, cls] of Object.entries(FLAG_CLASS)) {
      if (run[flag]) classes.push(cls);
    }
    if (run.color) classes.push(`rt-c-${run.color}`);
    if (run.highlight) classes.push(`rt-h-${run.highlight}`);
    if (run.size) classes.push(`rt-z-${run.size}`);

    return classes.length
      ? `<span class="${classes.join(" ")}">${inner}</span>`
      : inner;
  }

  const runsHtml = (runs) => (runs || []).map(runHtml).join("");

  function blockHtml(block) {
    if (!block || typeof block !== "object") return "";
    switch (block.type) {
      case "heading": {
        const level = block.level === 3 ? 3 : 2;
        return `<h${level} class="rt-h">${runsHtml(block.runs)}</h${level}>`;
      }
      case "quote":
        return `<blockquote class="rt-quote">${runsHtml(block.runs)}</blockquote>`;
      case "list": {
        const tag = block.ordered ? "ol" : "ul";
        const items = (block.items || [])
          .map((item) => `<li>${runsHtml(item)}</li>`).join("");
        return `<${tag} class="rt-list">${items}</${tag}>`;
      }
      case "image": {
        if (!block.file) return "";
        return `
          <figure class="rt-figure">
            <img src="${esc(IMAGE_URL(block.file))}" alt="${esc(block.caption || "配图")}"
                 loading="lazy" decoding="async">
            ${block.caption ? `<figcaption class="rt-cap">${esc(block.caption)}</figcaption>` : ""}
          </figure>`;
      }
      case "file": {
        if (!block.file) return "";
        const size = block.size ? `<span class="rt-file__size">${esc(humanSize(block.size))}</span>` : "";
        return `
          <a class="rt-file" href="${esc(FILE_URL(block.file))}" download>
            <span class="rt-file__icon" aria-hidden="true">📦</span>
            <span class="rt-file__body">
              <b class="rt-file__name">${esc(block.name || "附件")}</b>
              ${size}
            </span>
            <span class="rt-file__go" aria-hidden="true">下载</span>
          </a>`;
      }
      default: {
        const align = block.align && ["center", "right"].includes(block.align)
          ? ` style="text-align:${block.align}"` : "";
        return `<p class="rt-p"${align}>${runsHtml(block.runs)}</p>`;
      }
    }
  }

  /** 详情页用的只读渲染。 */
  function render(blocks) {
    return (blocks || []).map(blockHtml).join("");
  }

  /** 纯文本（列表摘要、搜索、字数统计都读它）。 */
  function plainOf(blocks) {
    return (blocks || []).map((block) => {
      if (block.type === "list") {
        return (block.items || []).map((item) => plainOf(item)).join("\n");
      }
      if (block.type === "image") return block.caption || "";
      if (block.type === "file") return block.name || "";
      return (block.runs || []).map((run) => run.text || "").join("");
    }).filter(Boolean).join("\n");
  }

  function countImages(blocks) {
    return (blocks || []).filter((block) => block.type === "image").length;
  }

  /* ================================================================
     二、编辑器（文档 JSON → contenteditable 的初始 DOM）
     ================================================================ */

  /* 编辑态和只读态的画法**故意不一样**：
       只读：<img> 直接铺在正文里，注释是 <figcaption>。
       编辑：图片包在一层 contenteditable="false" 里变成「一个整体」
             （按 Backspace 一次删掉、光标不会钻进去），
             注释单独一个可编辑的小块。 */
  function editorImageHtml(block) {
    return `
      <div class="rt-figure" data-rt-block="image" data-file="${esc(block.file)}">
        <div class="rt-figure__img" contenteditable="false">
          <img src="${esc(IMAGE_URL(block.file))}" alt="">
          <button type="button" class="rt-del" data-rt-del aria-label="删除这张图片">✕</button>
        </div>
        <div class="rt-cap" data-rt-cap data-placeholder="写图片注释（选填）">${esc(block.caption || "")}</div>
      </div>`;
  }

  function editorFileHtml(block) {
    const size = block.size ? `<span class="rt-file__size">${esc(humanSize(block.size))}</span>` : "";
    const name = block.name || "附件";
    return `
      <div class="rt-file" data-rt-block="file" data-file="${esc(block.file)}"
           data-name="${esc(name)}" data-size="${Number(block.size) || 0}" contenteditable="false">
        <span class="rt-file__icon" aria-hidden="true">📦</span>
        <span class="rt-file__body">
          <b class="rt-file__name">${esc(name)}</b>
          ${size}
        </span>
        <button type="button" class="rt-del" data-rt-del aria-label="删除这个附件">✕</button>
      </div>`;
  }

  function editorBlockHtml(block) {
    switch (block.type) {
      case "heading":
        return blockHtml(block);
      case "quote":
        return blockHtml(block);
      case "list":
        return blockHtml(block);
      case "image":
        return editorImageHtml(block);
      case "file":
        return editorFileHtml(block);
      default:
        return blockHtml(block);
    }
  }

  /** 把文档填进编辑器元素。 */
  function loadInto(root, blocks) {
    const list = (blocks || []).filter((block) => block && block.type);
    root.innerHTML = list.length
      ? list.map(editorBlockHtml).join("")
      : "<p><br></p>";
  }

  /* ================================================================
     三、序列化（contenteditable 的 DOM → 文档 JSON）
     ================================================================ */

  const BLOCK_TAGS = new Set([
    "P", "DIV", "H1", "H2", "H3", "H4", "H5", "H6",
    "UL", "OL", "LI", "BLOCKQUOTE", "FIGURE", "TABLE", "HR", "PRE",
  ]);

  /* 从元素自己身上读出「它给后代加了什么格式」。
     ！只认白名单：粘贴进来的任意 style 一律丢掉。
       这正是「不收 HTML」的意义 —— 与其去分析一段陌生 CSS 危不危险，
       不如只认识自己认得的几个类。 */
  function formatOf(el, inherited) {
    const format = { ...inherited };
    const tag = el.tagName;

    if (tag === "B" || tag === "STRONG") format.bold = true;
    if (tag === "I" || tag === "EM") format.italic = true;
    if (tag === "U") format.underline = true;
    if (tag === "S" || tag === "STRIKE" || tag === "DEL" || tag === "INS") {
      if (tag !== "INS") format.strike = true;
    }

    for (const cls of el.classList || []) {
      const flag = CLASS_FLAG[cls];
      if (flag) { format[flag] = true; continue; }
      if (cls.startsWith("rt-c-")) format.color = cls.slice(5);
      else if (cls.startsWith("rt-h-")) format.highlight = cls.slice(5);
      else if (cls.startsWith("rt-z-")) format.size = cls.slice(5);
    }

    // execCommand 打开 styleWithCSS 之后会产出内联样式，这里认回来
    const style = el.style;
    if (style) {
      const weight = style.fontWeight;
      if (weight === "bold" || weight === "bolder" || Number(weight) >= 600) format.bold = true;
      if (style.fontStyle === "italic") format.italic = true;
      const decoration = `${style.textDecoration || ""} ${style.textDecorationLine || ""}`;
      if (decoration.includes("underline")) format.underline = true;
      if (decoration.includes("line-through")) format.strike = true;
      // 字号只认我们自己产出的那几档 em 值 —— 别的一律忽略
      const size = style.fontSize || "";
      if (size.endsWith("em")) {
        const value = Number(size.slice(0, -2));
        if (Math.abs(value - 0.875) < 0.01) format.size = "small";
        else if (Math.abs(value - 1.25) < 0.01) format.size = "large";
        else if (Math.abs(value - 1.6) < 0.01) format.size = "huge";
      }
    }
    return format;
  }

  /** 递归收集一段文本里的行内段。 */
  function runsFrom(nodes, inherited, out) {
    for (const node of nodes) {
      if (node.nodeType === 3) {
        if (node.nodeValue) out.push({ ...inherited, text: node.nodeValue });
        continue;
      }
      if (node.nodeType !== 1) continue;
      const tag = node.tagName;
      if (tag === "SCRIPT" || tag === "STYLE") continue;

      if (tag === "BR") {
        // 换行接到上一段末尾；一段开头的 <br> 自己起一段
        if (out.length) out[out.length - 1].text += "\n";
        else out.push({ ...inherited, text: "\n" });
        continue;
      }
      // 块级元素出现在行内上下文里（粘贴来的），当成换行处理
      if (BLOCK_TAGS.has(tag) && !["LI"].includes(tag)) {
        out.push({ ...inherited, text: "\n" });
        runsFrom(node.childNodes, inherited, out);
        continue;
      }
      runsFrom(node.childNodes, formatOf(node, inherited), out);
    }
    return out;
  }

  /** 把相邻的同格式段合并、丢掉空段、裁掉块首尾的空白。 */
  function cleanRuns(runs) {
    const out = [];
    for (const run of runs) {
      const text = run.text;
      if (!text) continue;
      const prev = out[out.length - 1];
      const sameFormat = prev
        && Object.keys(run).every((key) => key === "text" || prev[key] === run[key])
        && Object.keys(prev).every((key) => key === "text" || run[key] === prev[key]);
      if (sameFormat) prev.text += text;
      else out.push({ ...run });
    }
    if (!out.length) return out;

    // 块首尾的空白没有意义（HTML 本来也会吃掉），但段中间的空格要留着
    out[0].text = out[0].text.replace(/^[\s\u3000]+/, "");
    out[out.length - 1].text = out[out.length - 1].text.replace(/[\s\u3000]+$/, "");
    return out.filter((run) => run.text);
  }

  function alignOf(el) {
    const value = (el.style && el.style.textAlign) || "";
    return ["center", "right"].includes(value) ? value : "";
  }

  /* <p> 里只允许行内内容。可是浏览器的 execCommand 时不时会把
     <ul> / <blockquote> / <h2> 塞进 <p> 里（DOM 层面不报错，但非法）。
     ！这一步不能省：serialize 的 P 分支会把里面的块当成行内小玩意，
       结果是「插入列表」点下去界面看着有，一保存列表整个没了。
     做法是把这些块级子元素提到 <p> 后面，保持原有先后顺序。 */
  const LIFT_TAGS = new Set([
    "P", "DIV", "UL", "OL", "BLOCKQUOTE", "H1", "H2", "H3", "H4", "H5", "H6",
    "FIGURE", "PRE", "TABLE", "HR",
  ]);

  function normalize(root) {
    if (!root || !root.querySelectorAll) return;
    for (const p of Array.from(root.querySelectorAll("p"))) {
      const bad = Array.from(p.children).filter((child) =>
        LIFT_TAGS.has(child.tagName) ||
        (child.dataset && child.dataset.rtBlock));
      if (!bad.length) continue;
      // 倒着搬：p.after() 每次都紧贴 <p>，倒序搬完正好是原来的顺序
      for (const child of bad.reverse()) p.after(child);
      // <p> 被搬空了就顺手去掉，别留一堆空行
      if (!p.textContent.trim() && !p.children.length) p.remove();
    }
  }

  /** 序列化一个编辑器元素，得到文档 JSON。 */
  function serialize(root) {
    normalize(root);
    const blocks = [];
    let pending = [];   // 顶层散落的行内节点，攒成一段

    const flush = () => {
      if (!pending.length) return;
      const runs = cleanRuns(runsFrom(pending, {}, []));
      pending = [];
      if (runs.length) blocks.push({ type: "paragraph", runs });
    };

    const visit = (parent) => {
      for (const node of Array.from(parent.childNodes)) {
        if (node.nodeType === 3) {
          if (node.nodeValue) pending.push(node);
          continue;
        }
        if (node.nodeType !== 1) continue;

        const tag = node.tagName;
        if (tag === "SCRIPT" || tag === "STYLE") continue;

        // 我们自己插进去的原子块，先认它们
        const kind = node.dataset ? node.dataset.rtBlock : "";
        if (kind === "image") {
          flush();
          const file = node.dataset.file || "";
          if (file) {
            const cap = node.querySelector("[data-rt-cap]");
            blocks.push({
              type: "image",
              file,
              caption: cap ? cap.textContent.trim().slice(0, 200) : "",
            });
          }
          continue;
        }
        if (kind === "file") {
          flush();
          const file = node.dataset.file || "";
          if (file) {
            blocks.push({
              type: "file",
              file,
              name: node.dataset.name || "附件",
              size: Number(node.dataset.size) || 0,
            });
          }
          continue;
        }

        if (tag === "UL" || tag === "OL") {
          flush();
          const items = [];
          for (const li of node.querySelectorAll(":scope > li")) {
            const runs = cleanRuns(runsFrom(li.childNodes, {}, []));
            if (runs.length) items.push(runs);
          }
          if (items.length) {
            blocks.push({ type: "list", ordered: tag === "OL", items });
          }
          continue;
        }
        if (tag === "BLOCKQUOTE") {
          flush();
          const runs = cleanRuns(runsFrom(node.childNodes, {}, []));
          if (runs.length) blocks.push({ type: "quote", runs });
          continue;
        }
        if (["H2", "H3", "H4", "H5", "H6"].includes(tag)) {
          flush();
          const runs = cleanRuns(runsFrom(node.childNodes, {}, []));
          if (runs.length) {
            blocks.push({ type: "heading", level: tag === "H2" ? 2 : 3, runs });
          }
          continue;
        }
        if (tag === "P") {
          flush();
          const runs = cleanRuns(runsFrom(node.childNodes, {}, []));
          if (runs.length) {
            const block = { type: "paragraph", runs };
            const align = alignOf(node);
            if (align) block.align = align;
            blocks.push(block);
          }
          continue;
        }
        if (tag === "DIV" || tag === "SECTION") {
          // ！浏览器的 contenteditable 到处塞 <div>。有的是真段落，
          //   有的只是「包一层」。判断办法：里面还有块级子元素就往下递归，
          //   否则它自己就是一段。
          const nested = Array.from(node.children).some((child) =>
            BLOCK_TAGS.has(child.tagName));
          if (nested) {
            flush();
            visit(node);
          } else {
            flush();
            const runs = cleanRuns(runsFrom(node.childNodes, {}, []));
            if (runs.length) {
              const block = { type: "paragraph", runs };
              const align = alignOf(node);
              if (align) block.align = align;
              blocks.push(block);
            }
          }
          continue;
        }
        // 其他行内元素：先攒着，交给 flush 归到某一段里
        pending.push(node);
      }
    };

    visit(root);
    flush();

    // 首尾的空段落没有意义，去掉（编辑器里按回车很容易留下）
    while (blocks.length && blocks[0].type === "paragraph"
           && !plainOfBlock(blocks[0]).trim()) {
      blocks.shift();
    }
    while (blocks.length && blocks[blocks.length - 1].type === "paragraph"
           && !plainOfBlock(blocks[blocks.length - 1]).trim()) {
      blocks.pop();
    }
    return blocks;
  }

  function plainOfBlock(block) {
    if (block.type === "list") {
      return (block.items || []).map((item) =>
        (item || []).map((run) => run.text || "").join("")).join("\n");
    }
    return (block.runs || []).map((run) => run.text || "").join("");
  }

  /* ================================================================
     四、对外接口
     ================================================================ */

  window.McfrRich = {
    COLORS, HIGHLIGHTS, SIZES,
    esc, humanSize, IMAGE_URL, FILE_URL,
    render, plainOf, countImages,
    loadInto, serialize, normalize,
    // 工具条要按选区状态回填，所以这几个也暴露出去
    runHtml, cleanRuns,
  };
})();
