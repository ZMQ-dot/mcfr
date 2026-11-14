/* mcfr · English (en)
   ============================================================================
   The key is the original Simplified-Chinese string; the value is the translation.
   Missing entries fall back to Chinese rather than showing a raw key —
   the mechanism is explained at the top of js/i18n.js.

   ## Tone

   The audience is 6–18 year old Minecraft players, so: short sentences, no
   corporate wording, no exclamation marks piled up. "Sign in" not
   "Authenticate"; "Player name" not "Username" where it reads better.
   ========================================================================= */

window.McfrI18n.add("en", {
  /* ---- Landing ---- */
  "mcfr · 找到你的 MC 伙伴": "mcfr · Find your Minecraft people",
  "mcfr · Minecraft 同好匹配": "mcfr · Minecraft matchmaking",
  "找到能<br>一起<span class=\"iris-text\">造世界</span><br>的人":
    "Find<br>people to<br><span class=\"iris-text\">build with</span>",
  "面向《我的世界》玩家的安全、纯净交友站：按玩法、版本和上线时间匹配同好。注册不需要邮箱，一句话就能开始；联系方式默认对所有人隐藏，只有你逐条同意，才会发送给想认识你的那个人。":
    "A safe, simple place for Minecraft players to meet. We match you by play " +
    "style, game version and when you're online. No email needed to sign up. " +
    "Your contact details stay hidden by default — they're only sent to someone " +
    "after you approve them, one by one.",
  "位同好在线": "players online",
  "条站内私信与陌生骚扰": "DMs or random strangers here",
  "项成长成就": "achievements to unlock",

  /* ---- Sign in / register tabs ---- */
  "登录": "Sign in",
  "注册": "Sign up",

  /* ---- Sign-in form ---- */
  "用户名或邮箱": "Username or email",
  "用户名，或自己填过的邮箱": "Your username, or an email you added",
  "密码": "Password",
  "注册时设置的密码": "The password you signed up with",
  "人机验证": "Human check",
  "点一下换一张": "Click for a new one",
  "人机验证图片，点一下换一张": "Human-check image. Click to get a new one.",
  "填图中的 4 个字符": "Type the 4 characters",
  "看不清就点一下图片换一张，字母不分大小写。":
    "Can't read it? Click the image for a new one. Letters aren't case-sensitive.",
  "进入 mcfr": "Enter mcfr",
  "每次登录都要过一次，用来挡住拿脚本撞密码的":
    "Every sign-in passes this check — it stops scripts from guessing passwords",
  "忘记密码？": "Forgot your password?",

  /* ---- Register wizard ---- */
  "账号信息": "Account",
  "个人资料": "Profile",
  "用户名": "Username",
  "4-16 位字母数字下划线": "4–16 letters, numbers or underscores",
  "游戏昵称": "Player name",
  "别人怎么称呼你": "What should people call you?",
  "6 位以上，不要使用真实生日": "At least 6 characters — don't use your real birthday",
  "邮箱（选填）": "Email (optional)",
  "方便别人联系你，不填也可以": "So others can reach you. You can leave it blank.",
  "用来挡住批量注册的小号；看不清就点图片换一张。":
    "This stops bots from mass-registering. Can't read it? Click for a new image.",
  "游戏版本": "Game edition",
  "所在地区（可留空）": "Region (optional)",
  "例如 江苏·南京": "e.g. Berlin, Germany",
  "喜欢的玩法": "What you like doing",
  "常在线时段": "When you're usually online",
  "想找什么样的伙伴": "Who you're looking for",
  "一句话介绍自己": "One line about you",
  "例如：喜欢红石和自动农场，想找人一起开荒":
    "e.g. I love redstone and auto-farms, looking for someone to start a world with",
  "上一步": "Back",
  "下一步": "Next",
  "创建我的账号": "Create my account",

  /* ---- Topbar / nav ---- */
  "刷新": "Refresh",
  "反馈": "Feedback",
  "导航": "Menu",
  "退出登录": "Sign out",
  "发现同好": "Discover",
  "模组镜像": "Mods",
  "我的伙伴": "Partners",
  "成就": "Achievements",
  "我的": "Me",
  "方块同好": "Block buddy",

  /* ---- Theme toggle ---- */
  "主题：{}，点击切换到{}": "Theme: {} — click for {}",
  "主题：{} · 点击切换到{}": "Theme: {} · click for {}",
  "跟随系统": "Auto",
  "浅色": "light",
  "深色": "dark",
  "跟随时间": "By the clock",

  /* ---- Time ---- */
  "刚刚": "just now",
  "{} 分钟前": "{} min ago",
  "{} 小时前": "{} h ago",
  "{} 天前": "{} d ago",
  "{}月{}日 {}:{}": "{}/{} {}:{}",
  "{}月{}日": "{}/{}",
  "最后活跃 {}": "Last seen {}",

  /* ---- Search ---- */
  "搜索同好 —— 输入昵称或用户名": "Find players — type a name or username",
  "搜索同好": "Search players",
  "清除搜索": "Clear search",
  "搜索中…": "Searching…",
  "找到 {} 位昵称或用户名含「{}」的同好": "Found {} players matching 「{}」",

  /* ---- Cards / contact ---- */
  "微信": "WeChat",
  "其他": "Other",
  "已复制：{}": "Copied: {}",
  "复制失败，请手动选中文本复制": "Couldn't copy — select the text and copy it yourself",
  "这位玩家": "This player",
  "尚未填写": "Not filled in",
  "例如 B 站昵称 / 基岩版 ID / 常玩的服务器":
    "e.g. Discord handle / Bedrock ID / your usual server",
  "例如：加我时说一声是在 mcfr 看到的": "e.g. Mention you found me on mcfr",

  /* ---- Exchange requests ---- */
  "例如：我也在造中世纪城堡，要不要一起？":
    "e.g. I'm building a medieval castle too — want to team up?",
  "同意后，你预置的联系方式会发送给 TA，同时你也会拿到 TA 的联系方式。":
    "Once you accept, your saved contact details go to them, and you get theirs.",
  "你还没有预置联系方式。同意前，建议先去「我的 → 联系方式」填写，否则对方只会收到一句空资料。":
    "You haven't saved any contact details yet. Fill them in under " +
    "Me → Contact first, otherwise they'll just receive an empty profile.",
  "已婉拒，对方不会看到你的联系方式": "Declined — they won't see your contact details",

  /* ---- Profile / media ---- */
  "介绍一下你在玩什么、想找什么样的伙伴":
    "Say what you're playing and who you're looking for",
  "换一张皮肤": "Change skin",
  "上传皮肤": "Upload a skin",
  "正在使用你上传的图片": "Using the image you uploaded",
  "自动取皮肤里的头部": "Head is taken from your skin",
  "根据用户名生成的像素脸": "Pixel face generated from your username",
  "接收联系方式交换申请": "Allow contact exchange requests",
  "对外显示在线状态": "Show my online status",
  "资料已保存": "Profile saved",
  "已开启": "On",
  "已关闭": "Off",
  "正在上传皮肤…": "Uploading skin…",
  "皮肤已更新": "Skin updated",
  "已移除皮肤": "Skin removed",
  "正在处理头像…": "Processing avatar…",
  "头像已更新": "Avatar updated",
  "已恢复默认头像": "Back to the default avatar",
  "这个文件读不出来，换一张图试试。": "That file won't open — try another image.",
  "图片处理失败，换一张试试。": "Couldn't process that image — try another one.",

  /* ---- Danger zone ---- */
  "确定要注销并删除账号吗？所有数据都会被清除且无法恢复。":
    "Delete your account? Everything will be erased and can't be recovered.",
  "账号已删除，感谢曾经的陪伴": "Account deleted. Thanks for playing with us.",
  "请先选择一个举报原因": "Pick a reason for the report first",
  "解除后对方的联系方式将不再可见，确定吗？":
    "Their contact details will be hidden again. Sure?",
  "已解除交换": "Exchange cancelled",
  "已解除屏蔽": "Unblocked",
  "屏蔽后你们将不再互相看到，交换关系也会解除。确定吗？":
    "You'll both disappear from each other, and any exchange will be cancelled. Sure?",
  "已屏蔽": "Blocked",

  /* ---- Feedback ---- */
  "问题反馈": "Send feedback",
  "我们知道是你提的，处理完可以回访你。": "We know it's from you and can follow up.",
  "你还没登录，我们会收到内容，但联系不上你 —— 想收到回复的话留个联系方式吧。":
    "You're not signed in, so we'll get your message but can't reply. " +
    "Leave a way to reach you if you want an answer.",
  "例如：注册时验证码一直收不到，我用的是 QQ 邮箱":
    "e.g. The captcha image never loads — I'm on QQ Mail",
  "想让我们回复的话，留个邮箱或 QQ": "Leave an email or QQ if you want a reply",
  "至少写 5 个字吧，说清楚一点我们才好处理。":
    "Give us at least a few words so we can actually help.",
  "发送中…": "Sending…",
  "已经收到啦，谢谢！": "Got it — thanks!",

  /* ---- Password change ---- */
  "现在正在用的密码": "Your current password",
  "两遍要一样": "Both entries must match",
  "请先填写当前密码": "Enter your current password first",
  "新密码至少要 6 位": "The new password needs at least 6 characters",
  "两遍新密码不一致": "The two new passwords don't match",
  "新密码不能和当前密码一样": "The new password can't be the same as the old one",
  "人机验证图还没加载出来，稍等一下或点一下图片":
    "The human-check image hasn't loaded yet — wait a moment or click it",

  /* ---- Sign-in / register results ---- */
  "欢迎回来，{}": "Welcome back, {}",
  "注册成功，欢迎加入 mcfr！": "You're in — welcome to mcfr!",
  "先去「联系方式」预置一下，别人申请时就能直接交换":
    "Add your contact details first so people can exchange right away",
  "成就解锁 · {}  +{} 积分": "Achievement unlocked · {}  +{} pts",

  /* ---- Mirror downloads ---- */
  "下载中…": "Downloading…",
  "下载失败，请稍后再试": "Download failed — try again later",
  "已开始下载 {}": "Downloading {}",

  /* ---- Errors ---- */
  "网络连接失败，请确认后端服务已启动":
    "No connection — check that the backend is running",
  "登录状态已过期，请重新登录": "Your session expired — please sign in again",
  "后端服务未启动，请先运行 python run.py":
    "The backend isn't running — start it with python run.py",
  "已刷新": "Refreshed",

  /* ---- Mirror / achievements ---- */
  "全部": "All",
  "其它": "Other",
  "在线": "Online",
  "内容举报": "Content report",
  "功能建议": "Feature idea",
  "安全 / 隐私": "Safety & privacy",
  "可留空": "optional",
  "已解锁": "Unlocked",
  "已预置": "Saved",
  "未绑定邮箱": "No email linked",
  "未知作者": "Unknown author",
  "这个模组没有写简介。": "This mod has no description.",
  "这位玩家还没有写简介。": "This player hasn't written a bio yet.",
  "不再推荐": "Stop recommending",
  "上传失败，请稍后重试": "Upload failed — try again later",
  "操作失败，请稍后重试": "That didn't work — try again later",
  "· 已满级": "· Max level",
  "· 还有 {} 条": "· {} more",
  "/ 下一等级 {}": "/ next level {}",
  "查看 {} 的版本": "See {} versions",
  "加载中": "Loading",

  /* ---- 帖子（v0.27）---- */
  "TA 已经在名单里了":
    "They're already on the list",
  "一篇帖子最多放 {} 张图":
    "Up to {} images per post",
  "保存修改":
    "Save changes",
  "先写点什么再发":
    "Write something first",
  "先给帖子起个标题":
    "Give the post a title first",
  "先选一个人":
    "Pick someone first",
  "写一篇帖子":
    "Write a post",
  "删除这条评论":
    "Delete this comment",
  "删除这篇帖子？下面的评论和点赞也会一起消失，而且不能恢复。":
    "Delete this post? Its comments and likes go too, and this can't be undone.",
  "制作人员最多 10 位":
    "Up to 10 collaborators",
  "发布":
    "Publish",
  "发布成功":
    "Published",
  "发来一条通知":
    "sent you a notification",
  "图 {}":
    "{} images",
  "图片已插入":
    "Image inserted",
  "已全部标为已读":
    "All marked as read",
  "已转发给 {} 位朋友":
    "Shared with {} friends",
  "帖子":
    "Posts",
  "帖子已删除":
    "Post deleted",
  "打开帖子：{}":
    "Open post: {}",
  "把 {} 移出名单":
    "Remove {} from the list",
  "把你写进了制作人员":
    "added you to the credits",
  "换个词试试，或者清空搜索看看全部。":
    "Try another word, or clear the search to see everything.",
  "推荐你看看这个！":
    "You should check this out!",
  "提交中…":
    "Submitting…",
  "搜索帖子":
    "Search posts",
  "搜索帖子 —— 标题或正文里的词":
    "Search posts — words in the title or body",
  "搜索要添加的用户":
    "Search for a user to add",
  "搜索要转发给谁":
    "Search for someone to share with",
  "改动已保存":
    "Changes saved",
  "标题 —— 一句话说清这篇写的是什么":
    "Title — one line about what this is",
  "正在处理并上传图片…":
    "Processing and uploading…",
  "正文还空着，写点什么再发布":
    "The body is empty — write something first",
  "没有搜到相关的帖子":
    "No matching posts",
  "没有未读消息":
    "No unread messages",
  "消息":
    "Messages",
  "点了赞":
    "liked your post",
  "离开编辑器？还没发布的内容会丢掉。":
    "Leave the editor? Unpublished content will be lost.",
  "移除":
    "Remove",
  "第一篇就交给你了 —— 点右上角那颗「写帖子」。":
    "Be the first — tap “Write a post” at the top right.",
  "系统":
    "System",
  "编辑帖子":
    "Edit post",
  "评论 {}":
    "{} comments",
  "评论了":
    "commented on",
  "说点什么…… 友善一点。":
    "Say something… be kind.",
  "赞 {}":
    "{} likes",
  "转发了帖子给你":
    "shared a post with you",
  "输入昵称或用户名搜人":
    "Search by nickname or username",
  "还没有人发过帖子":
    "No posts yet",
  "还没有收到过消息":
    "No messages yet",
  "配图":
    "Post image",
  "已删除":
    "Deleted",
  "已被下架":
    "Taken down",

  /* ---- v0.28 富文本编辑器 ---- */
  "正文格式":
    "Text formatting",
  "撤销":
    "Undo",
  "重做":
    "Redo",
  "段落样式":
    "Paragraph style",
  "正文":
    "Body text",
  "加粗":
    "Bold",
  "斜体":
    "Italic",
  "下划线":
    "Underline",
  "删除线":
    "Strikethrough",
  "文字颜色":
    "Text color",
  "背景高亮":
    "Highlight",
  "字号":
    "Text size",
  "左对齐":
    "Align left",
  "居中":
    "Align center",
  "右对齐":
    "Align right",
  "无序列表":
    "Bulleted list",
  "有序列表":
    "Numbered list",
  "插入图片":
    "Insert image",
  "插入附件":
    "Insert file",
  "清除格式":
    "Clear formatting",
  "特大":
    "Extra large",
  "在这里写正文…… 选中文字可以加粗、变色、改字号；图片和附件可以插在任何位置":
    "Write your post here… select some text to make it bold, change its color or size. Images and files can go anywhere.",
  "先选中一段文字，再挑颜色或字号":
    "Select some text first, then pick a color or size",
  "写图片注释（选填）":
    "Write a caption (optional)",
  "删除这张图片":
    "Remove this image",
  "删除这个附件":
    "Remove this attachment",
  "附件":
    "Attachment",
  "正在上传 {}…":
    "Uploading {}…",
  "附件已插入（{}）":
    "Attachment added ({})",
  "约 {} 字":
    "About {} characters",
  "策划":
    "Planning",
  "编辑":
    "Editing",
  "文案":
    "Copywriting",
  "校对":
    "Proofreading",
  "美术":
    "Art",
  "建筑":
    "Building",
  "红石":
    "Redstone",
  "编程":
    "Programming",
  "测试":
    "Testing",
  "素材":
    "Assets",
  "配音":
    "Voice acting",
  "{} 负责什么":
    "What {} did",
  "<option value=\"\" {}>关系…</option>{}{}":
    "<option value=\"\" {}>Role…</option>{}{}",

  /* ---- v0.28 编辑器里的短词与属性文案 ---- */
  "大标题":
    "Large heading",
  "小标题":
    "Small heading",
  "引用":
    "Quote",
  "图片":
    "Image",
  "默认颜色":
    "Default color",
  "去掉高亮":
    "No highlight",
  "默认字号":
    "Default size",
  "红":
    "Red",
  "金":
    "Gold",
  "绿":
    "Green",
  "青":
    "Cyan",
  "紫":
    "Purple",
  "灰":
    "Gray",
  "黄":
    "Yellow",
  "粉":
    "Pink",
  "小":
    "Small",
  "大":
    "Large",
  "制作人员":
    "Credits",
  "作者":
    "Author",
  "一起做这篇帖子的朋友。只能添加已经在本站注册过的用户，所以做之前先让 TA 注册一下。右边可以写 TA 负责什么（策划、编辑……）。":
    "The people who worked on this post with you. You can only add users who have already signed up here, so get them to register first. The menu next to each name is what they did (planning, editing…).",
  "意见反馈":
    "Feedback",

  /* ---- v0.28 编辑器里的短词与属性文案 ---- */
  "取消": "Cancel",

  /* ---- v0.29 年龄与生日 ---- */
  "年龄":
    "Age",
  "填数字，例如 13":
    "A number, e.g. 13",
  "生日（月 / 日）":
    "Birthday (month / day)",
  "{} 岁":
    "{} years old",
  "未填写年龄":
    "Age not set",
  "还差一步":
    "One more step",
  "保存并继续":
    "Save and continue",
  "填过就不能改了":
    "can't be changed later",
  "补上年龄和生日就能继续使用。填过之后不能修改，它会决定你在站里属于未成年还是成年，也会用来匹配同好。":
    "Add your age and birthday to keep using the site. They can't be changed afterwards, and they decide whether you're on the adult or minor side of the site — and how you get matched.",
  "年龄和生日填过就不能再改了。它们决定你是未成年还是成年，也会用来匹配同好 —— 请认真填。":
    "Your age and birthday can't be changed once you submit them. They decide whether you're on the adult or minor side of the site, and they're used for matching — so please fill them in carefully.",
  "填过就不能再改了。填错只能找管理员。":
    "This can't be changed later. If you get it wrong, ask an admin.",
  "已保存。年龄和生日以后不能修改。":
    "Saved. Your age and birthday can no longer be changed.",

  /* ---- v0.29 年龄与生日 ---- */
  "出生月份": "Birth month",
  "出生日期": "Birth day",

  /* ---- v0.30 「在这台设备上保持登录」 ---- */
  "下次还要输密码吗？": "Type your password again next time?",
  "对这台电脑回答一次，以后就按这个来。":
    "Answer once for this computer and we'll stick to it.",
  "保持登录 —— 这台电脑是我自己的": "Stay signed in — this computer is mine",
  "每次都要重新登录 —— 这是公用电脑": "Ask me every time — this is a shared computer",
  "学校机房、网吧、家里的共用电脑请选下面那个 —— 不然下一个人打开就是你的账号。":
    "School labs, internet cafés and shared home computers: pick the second one — " +
    "otherwise the next person opens the site into your account.",
  "之后可以在「我的 → 账号」里改回来。":
    "You can change this later under Me → Account.",
  "在这台设备上保持登录": "Stay signed in on this device",
  "关闭后，关掉浏览器就要重新输密码":
    "Off means closing the browser signs you out",
  "已开启：下次打开直接进来": "On — next visit goes straight in",
  "已关闭：下次要重新输密码": "Off — you'll sign in again next time",

  /* ---- Circles (v0.34) ----
     The audience is kids, so a circle is described as "a room you walk into",
     not as a "group" or a "community" (both sound corporate). */
  "圈子": "Circles",
  "一起玩的": "Play together in",
  "不用等算法发牌 —— 挑一个主题进去。圈子里发的帖子只有成员看得见，所以这里说话可以随便一点。":
    "No waiting for the algorithm to deal you a card — just pick a topic and walk in. " +
    "What's posted inside a circle is visible only to its members, so people talk more freely here.",
  "我的圈子": "My circles",
  "发现圈子": "Find circles",
  "去发现圈子": "Find circles",
  "搜圈子 —— 名字或介绍里的词": "Search circles — by name or description",
  "搜索圈子": "Search circles",
  "建一个圈子": "Start a circle",
  "你还没加入任何圈子": "You haven't joined any circles yet",
  "去「发现圈子」看看，或者自己建一个 —— 圈子里发的东西只有成员看得见。":
    "Take a look in Find circles, or start your own — whatever you post in a circle " +
    "is visible only to its members.",
  "没有搜到相关的圈子": "No circles matched",
  "还没有人建过圈子": "Nobody has started a circle yet",
  "第一个圈子就交给你了 —— 点右上角那颗「建一个圈子」。":
    "The first one is yours — hit “Start a circle” up top.",
  "进入": "Open",
  "删除": "Remove",
  "加入": "Join",
  "我建的": "Started by me",
  "{} 位成员": "{} members",
  "{} 篇帖子": "{} posts",
  "圈子名": "Circle name",
  "例如 红石研究所 / 末地跑酷队": "e.g. Redstone Lab / End Run Club",
  "一句话介绍": "One-line intro",
  "例如 每周分享一个红石作品，互相点评": "e.g. one redstone build a week, everyone reviews",
  "主题": "Topic",
  "详细说明（选填）": "Details (optional)",
  "这个圈子打算一起做什么？比如：每周交一个作品；新手提问也可以。":
    "What will you do together? One build a week, or just somewhere beginners can ask questions.",
  "建好之后你就是圈主。你退出这个圈子时，它会自动交给最早加入的那一位；如果只剩你一个人，圈子会一起解散。":
    "You'll be the owner. If you leave, the circle goes to whoever joined first; " +
    "if you're the only one left, the circle is dissolved.",
  "创建圈子": "Create circle",
  "创建中…": "Creating…",
  "先给圈子起个名字": "Give the circle a name first",
  "圈子建好了，进去看看吧": "Circle created — have a look",
  "这个圈子还没写介绍。": "This circle has no intro yet.",
  "这个圈子": "this circle",
  "已加入「{}」": "Joined 「{}」",
  "已退出圈子": "You left the circle",
  "「{}」解散了，里面的 {} 篇帖子一起删除了":
    "「{}」 was dissolved, and its {} posts went with it",
  "已退出，圈主交给了 {}": "You left — ownership passed to {}",
  "进入圈子：{}": "Open circle: {}",
  "你是「{name}」的圈主。退出后它会交给最早加入的那位成员；\n如果圈子里只剩你一个人，圈子会直接解散，里面的帖子也会一起删除。\n\n确定退出吗？":
    "You own 「{name}」. If you leave, it goes to whoever joined first.\n" +
    "If you're the only one left, the circle is dissolved and its posts are deleted with it.\n\n" +
    "Leave the circle?",
  "退出「{name}」？\n退出后就看不到这个圈子里的帖子了，你写过的帖子还留在里面。":
    "Leave 「{name}」?\nYou won't see this circle's posts any more. Posts you wrote stay inside.",
  "成员": "Members",
  "还没有别人": "nobody else yet",
  "你是圈主": "You own this circle",
  "在圈子里发帖": "Post in this circle",
  "退出圈子": "Leave circle",
  "加入圈子": "Join circle",
  "加入之后才能看到圈子里发了什么": "Join to see what's posted inside",
  "加入之后才能看到圈子里的帖子": "Join to see the posts in this circle",
  "圈子里的内容只对成员可见 —— 这也是很多人愿意在这里说话的原因。":
    "Only members can see what's posted here — that's why people feel okay talking.",
  "这个圈子里还没有帖子": "No posts in this circle yet",
  "第一篇就交给你了 —— 上面那颗「在圈子里发帖」。":
    "The first one is yours — hit “Post in this circle” above.",
  "发到圈子": "Posting to circle",
  "只有这个圈子的成员看得到": "Only members of this circle can see it",
  "发到": "Posting to",
  "帖子广场": "the public board",
  "全站所有登录的人都能看到": "Everyone signed in can see it",
  "在「{}」里写一篇": "Write in 「{}」",
  "发出去了，圈子里的成员都能看到了": "Posted — everyone in the circle can see it now",
  "← 返回圈子": "← Back to the circle",
  "← 返回帖子列表": "← Back to posts",
  /* circle topics */
  "生存": "Survival",
  "建筑": "Building",
  "红石": "Redstone",
  "模组": "Mods",
  "小游戏": "Minigames",
  "联机": "Multiplayer",
  "聊天": "Chatting",
  "综合": "General",

  /* ---- v0.37：卡片上的替代简介与侧栏状态灯 ----
     「常玩 {}。」和「{} 在线。」是拆开的两个句子，因为前端把它们渲染成
     两个独立的文本节点（见 app.js 的 bioOf），各自查一次字典。 */
  "常玩 {}。": "Usually plays {}.",
  "{} 在线。": "Usually on {}.",
  "刚来不久，资料还在填。": "Just joined — still filling in the profile.",
  "还没写简介，先打个招呼吧。": "No bio yet — say hi first.",
  "资料还空着，人是已经来了。": "Profile's empty, but they're here.",
  "服务器在线": "Server online",
  "连不上服务器": "Can't reach the server",
  "正在连接…": "Connecting…",
  "{} 人在线 · 共 {} 位同好": "{} online · {} members",
});
