mcfr · 内置字体说明
===================

Press Start 2P
--------------
文件：press-start-2p-latin.woff2（仅 latin 子集，约 12KB）

用途：**只用于数字、等级、短标签**（例如 Lv.1、25、1 / 12）。
      正文与标题一律使用系统无衬线字体。

版权：Copyright (c) 2012, Sorkin Type Co (www.sorkintype.com),
      保留字体名称 "Press Start 2P"。
许可：SIL Open Font License, Version 1.1
      全文见 https://scripts.sil.org/OFL
      允许自由使用、修改与再分发（含商用），要求保留本声明，
      且不得单独出售字体文件本身。

来源：Google Fonts（https://fonts.google.com/specimen/Press+Start+2P）
      已本地自托管 —— 页面不会向任何外部域名发起请求。

为什么要本地放一份而不是引 CDN
------------------------------
本项目的一条硬约束是「不引 CDN、没有构建步骤」。字体直接从 Google Fonts
拉会引入一个外部依赖，而且离线 / 校园内网环境下会直接 404。
把它放进仓库是最省事也最稳的做法：12KB，一次请求，之后长期缓存。

字符覆盖的注意点
----------------
latin 子集**不含中文**。CSS 里通过 unicode-range 精确声明了它负责的字符范围，
所以把 "Press Start 2P" 放在字体栈的第一位是安全的 ——
中文、以及该子集没有的符号，浏览器会自动落到后面的系统字体上。

这也正好符合设计要求：像素字体只承担数字与拉丁短标签，中文永远走正文字体。
全站中文使用像素字体既没有可用字形，也会严重拖慢阅读。
