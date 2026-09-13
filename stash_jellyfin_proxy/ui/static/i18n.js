"use strict";

/**
 * Configuration-UI internationalisation (zh-CN / en).
 *
 * DESIGN — source-string keys, not synthetic ids
 * ----------------------------------------------
 * The catalog is keyed by the *English source string* itself, the way
 * gettext works. Two consequences drove this choice:
 *
 *   1. The 989-line template needs no per-node annotation. Retrofitting
 *      `data-i18n="..."` onto ~300 elements means ~300 hand edits to a
 *      file with no test coverage — a large, silent-regression surface.
 *      Here the template is untouched; the engine walks it at runtime.
 *   2. Adding new UI text never breaks existing entries. A string that
 *      has no catalog entry simply renders unchanged, and
 *      `dev-tools/i18n_audit.py` reports it as untranslated.
 *
 * The cost is homonyms: one English string can only map to one
 * translation. Where that matters the markup opts out with
 * `data-i18n-skip-attrs` (a Stash tag *name* in a placeholder must not
 * be translated even when the same word is a card title).
 *
 * WHAT GETS REWRITTEN
 *   * text nodes            — matched after whitespace normalisation,
 *                             so a phrase wrapped across source lines
 *                             still matches; surrounding whitespace is
 *                             preserved on write-back.
 *   * `title`, `placeholder`, `aria-label` attribute values.
 *
 * WHAT DOES NOT
 *   * `<script>`, `<style>`, `<code>`, `<pre>`, `<textarea>` subtrees.
 *   * Anything under `[data-i18n-skip]`.
 *   * Any node/attribute whose string is absent from the catalog.
 *   * `innerHTML` rendered by app.js — those call `t()` explicitly.
 *
 * PRECEDENCE (highest first) — see resolveLanguage() below
 *   1. this browser's stored choice (the sidebar switcher)
 *   2. UI_LANGUAGE from the server (env var > config file)
 *   3. navigator.language
 *   4. "en"
 */
(function (global) {
  const PREF_KEY = "sjp.ui.lang";
  const SUPPORTED = ["en", "zh"];
  const PRECISIONS = ["auto", "en", "zh"];

  // Attributes rewritten in place. Order matters only for determinism.
  const ATTRS = ["title", "placeholder", "aria-label"];
  // Subtrees that never contain prose.
  const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "CODE", "PRE", "TEXTAREA"]);
  // Opt-outs honoured on elements and their descendants.
  const SKIP_TEXT_FLAG = "data-i18n-skip";
  const SKIP_ATTR_FLAG = "data-i18n-skip-attrs";

  /* ==================================================================
   * CATALOG — zh-CN
   * ================================================================== */
  const ZH = {
    /* ---- Sidebar / navigation ---- */
    "Dashboard": "仪表盘",
    "Connection": "连接",
    "Libraries": "媒体库",
    "Players": "播放器",
    "Playback": "播放",
    "Search": "搜索",
    "System": "系统",
    "Logs": "日志",
    "Proxy": "代理",
    "Uptime: —": "运行时长：—",
    "Uptime: {v}": "运行时长：{v}",

    /* ---- Language switcher ---- */
    "Language": "语言",
    "Follow browser / server default": "跟随浏览器 / 服务端默认",
    "Switch to Chinese": "切换到中文",
    "Switch to English": "切换到英文",

    /* ---- Restart banner ---- */
    "Some changes require a server restart to take effect.": "部分改动需要重启服务才能生效。",
    "Restart Now": "立即重启",

    /* ---- Dashboard ---- */
    "At-a-glance health check and activity monitor": "状态总览与活动监控",
    "Connect a Player": "连接播放器",
    "Proxy Status": "代理状态",
    "Stash Connection": "Stash 连接",
    "Active Streams": "活跃串流",
    "In the last 5 min": "最近 5 分钟内",
    "Library Size": "媒体库规模",
    "Scenes": "场景",
    "Updates every 5s": "每 5 秒刷新",
    "No active streams.": "当前没有活跃串流。",
    "Started {time}": "开始于 {time}",
    "Library Stats": "媒体库统计",
    "Refreshes every 60s": "每 60 秒刷新",
    "Performers": "演员",
    "Studios": "片商",
    "Groups": "合集",
    "Tags": "标签",
    "Proxy Usage": "代理用量",
    "Reset": "重置",
    "Streams Today": "今日串流",
    "Total Streams": "累计串流",
    "Auth Success": "认证成功",
    "Auth Failed": "认证失败",
    "Top Played": "播放排行",
    "No plays recorded yet.": "暂无播放记录。",
    "Recent Logs": "最近日志",
    "View All Logs": "查看全部日志",
    "(no log entries yet)": "（暂无日志）",

    /* ---- Dashboard: config health banners ---- */
    "⚠ Config file is not writable.": "⚠ 配置文件不可写。",
    "can't be saved to, so": "无法写入，因此",
    "and": "和",
    "will regenerate on every restart — clients (Infuse, Swiftfin, SenPlayer) will need to re-add the server each time.":
      "将在每次重启时重新生成——客户端（Infuse、Swiftfin、SenPlayer）需要每次重新添加服务器。",
    "Fix: ensure the proxy process can write to that file. If running in Docker, mount a writable volume for the config directory.":
      "修复方法：确保代理进程对该文件有写权限。若运行在 Docker 中，请为配置目录挂载可写卷。",
    "⚠ Config file is not surviving restarts.": "⚠ 配置文件未能跨重启保留。",
    "is writable, but the marker we wrote on the previous boot is gone — the file got wiped between restarts. Settings,":
      "可写，但上次启动写入的标记已丢失——文件在重启之间被清空。设置、",
    ", and": "，以及",
    "will keep resetting until this is fixed.": "在问题修复前会一直被重置。",
    "Most likely cause:": "最可能的原因：",
    "is mounted as an anonymous Docker volume, or the volume isn't actually mounted (so writes land in the container's ephemeral layer and vanish on restart). Check your":
      "被挂载为匿名 Docker 卷，或该卷实际未挂载（写入落在容器临时层，重启即丢失）。请检查你的",
    "volume mapping.": "卷映射。",
    "ℹ Config migrated from v1 to v2.": "ℹ 配置已从 v1 迁移到 v2。",
    "Your original config is backed up at": "原始配置已备份至",
    ". Review your new settings in": "。请在以下页面检查新设置：",
    "tabs.": " 标签页。",
    "Dismiss": "忽略",

    /* ---- Connection tab ---- */
    "Stash server connection and client credentials": "Stash 服务器连接与客户端凭据",
    "Stash Server": "Stash 服务器",
    "Stash URL": "Stash URL",
    "Full URL including port. Use": "完整 URL（含端口）。若 Stash 运行在 Docker 宿主机上，请使用",
    "if Stash is on the Docker host.": "。",
    "API Key": "API 密钥",
    "Show/hide API key": "显示/隐藏 API 密钥",
    "Copy API key to clipboard": "复制 API 密钥到剪贴板",
    "Copy API key": "复制 API 密钥",
    "From Stash → Settings → Security → API Key. Required for images to load. Leave blank to keep the current value.":
      "来自 Stash → 设置 → 安全 → API Key。图片加载依赖此项。留空则保持当前值。",
    "GraphQL Path": "GraphQL 路径",
    "Advanced: override for reverse-proxy setups.": "高级选项：反向代理场景下可覆盖。",
    "Request Timeout (seconds)": "请求超时（秒）",
    "Retry Attempts": "重试次数",
    "Verify TLS Certificate": "校验 TLS 证书",
    "Disable for self-signed certificates.": "使用自签名证书时请关闭。",
    "Test Connection": "测试连接",
    "Runs a live probe against the values above (no save required).": "按上面的值实时探测（无需保存）。",
    "(unchanged)": "（保持不变）",

    /* ---- Connection: public address ---- */
    "Public Address": "公开地址",
    "Public URL": "公开地址",
    "The address players use to reach the": "播放器从外网访问",
    "from outside — e.g.": "所使用的地址——例如",
    "or": "或",
    ". Include the scheme; omit the port if the reverse proxy serves it on 443/80. The proxy can't auto-detect this (its own IP is an internal Docker address), so the Dashboard's":
      "。请包含协议；若反向代理已监听 443/80 可省略端口。代理无法自动探测该地址（其自身 IP 属于 Docker 内网），因此只有在设置后，仪表盘的",
    "card shows the server address only once this is set.": "卡片才会显示服务器地址。",
    "Copy Public URL to clipboard": "复制公开地址到剪贴板",
    "Copy Public URL": "复制公开地址",
    "Not configured. Set a": "尚未配置。请设置",
    "under": "（位于",
    "Connection → Public Address": "连接 → 公开地址",
    "to show the address players should use to reach this proxy.": "）以显示播放器应使用的代理地址。",
    "Add this server in Infuse, Swiftfin, or SenPlayer, then sign in with the username and password below.":
      "在 Infuse、Swiftfin 或 SenPlayer 中添加本服务器，然后用下面的用户名与密码登录。",
    "Server address": "服务器地址",
    "Copy server address to clipboard": "复制服务器地址到剪贴板",
    "Copy": "复制",
    "Done": "完成",
    "Close": "关闭",

    /* ---- Connection: credentials ---- */
    "Client Credentials": "客户端凭据",
    "Username": "用户名",
    "The username Jellyfin clients use to connect to this proxy.": "Jellyfin 客户端连接本代理所用的用户名。",
    "Password": "密码",
    "Leave blank to keep the current password.": "留空则保持当前密码。",
    "Show/hide password": "显示/隐藏密码",
    "Copy password to clipboard": "复制密码到剪贴板",
    "Copy password": "复制密码",
    "Copy username to clipboard": "复制用户名到剪贴板",
    "Copy username": "复制用户名",

    /* ---- Save badges / buttons ---- */
    "Save": "保存",
    "Restart required": "需重启生效",
    "Live": "即时生效",
    "Mixed": "部分需重启",
    "Every field in this card requires a proxy restart before the new value takes effect. Save writes to the config file; restart applies it.":
      "本卡片中每个字段都需要重启代理后新值才生效。保存会写入配置文件，重启后应用。",
    "Username and password are only applied at startup. Save writes them to the config file; restart picks them up.":
      "用户名与密码仅在启动时应用。保存会写入配置文件，重启后生效。",
    "Applied immediately on save.": "保存后立即生效。",
    "Applies immediately on save.": "保存后立即生效。",
    "Library structure changes need a restart so all clients see a consistent folder layout.":
      "媒体库结构调整需要重启，以便所有客户端看到一致的目录布局。",
    "Genre resolution is cached at startup. Changing the mode or source tag needs a restart to rebuild the cache.":
      "类型解析结果在启动时缓存。修改模式或来源标签需要重启以重建缓存。",
    "Episode patterns apply immediately on save. The Series Tag change requires a restart (studio classification is cached at startup).":
      "剧集规则保存后立即生效。剧集标签的修改需要重启（片商分类在启动时缓存）。",
    "The crop anchor is baked into cached artwork. Restart clears the image cache so new posters use the new anchor.":
      "裁剪锚点会被写入已缓存的海报。重启会清空图片缓存，使新海报使用新锚点。",
    "Applied immediately on save. The next library browse that doesn't send an explicit sort preference will use the new default.":
      "保存后立即生效。下一次未显式指定排序的媒体库浏览将使用新默认值。",
    "Applied immediately on save. Next listing that sorts by SortName uses the new article list.":
      "保存后立即生效。接下来按 SortName 排序的列表将使用新的冠词列表。",
    "The hero image pool is built once at startup. Restart rebuilds it with the new source.":
      "主视觉图池在启动时构建一次。重启后会按新来源重建。",
    "Applied immediately on save. Next client search uses the updated scope.":
      "保存后立即生效。下一次客户端搜索将使用更新后的范围。",
    "Applied immediately on save. The filter panel cache is cleared so the next /Items/Filters response uses the new settings.":
      "保存后立即生效。筛选面板缓存会被清空，下一次 /Items/Filters 响应将使用新设置。",
    "Server Name is included in the handshake response every client caches on first connection. Restart makes all clients re-read it.":
      "服务器名称会包含在每个客户端首次连接时缓存的握手响应中。重启后所有客户端会重新读取。",
    "Page-size limits and the Stash HTTP client are built once at startup. Restart rebuilds them with the new values.":
      "分页上限与 Stash HTTP 客户端在启动时构建一次。重启后会按新值重建。",
    "Log Level applies immediately on save. Log Directory, file size, and backup count require a restart (the rotating file handler is bound once at startup).":
      "日志级别保存后立即生效。日志目录、文件大小与备份数量需要重启（轮转处理器在启动时绑定一次）。",
    "Applied immediately on save. Ban threshold, window, and the current banned-IP list are read fresh on every auth attempt.":
      "保存后立即生效。封禁阈值、时间窗与当前封禁列表在每次认证时重新读取。",
    "Server-side default. Every browser can override it with the switcher in the sidebar.":
      "服务端默认值。每个浏览器都可用侧边栏的切换器单独覆盖。",

    /* ---- Libraries tab ---- */
    "Tag groups, genre mapping, series detection": "标签分组、类型映射、剧集识别",
    "Tag Groups": "标签分组",
    "Comma-separated list of Stash tags to expose as top-level library folders. Each tag becomes a movies library.":
      "以逗号分隔的 Stash 标签，将作为顶层媒体库文件夹暴露。每个标签对应一个电影媒体库。",
    "Tit Worship, JOI, Gooning": "Tit Worship, JOI, Gooning",
    "Favorite Tag": "收藏标签",
    "FAVORITE": "FAVORITE",
    "Stash tag name used for scene favorites. Toggling favorite in a client adds/removes this tag.":
      "用于场景收藏的 Stash 标签名。在客户端切换收藏会添加/移除此标签。",
    "Show Tags Library": "显示标签媒体库",
    "Expose a Tags folder with tag-based navigation.": "暴露一个按标签导航的「标签」文件夹。",
    "Show All Tags Subfolder": "显示「全部标签」子文件夹",
    "Include an \"All Tags\" subfolder (can be large). Only meaningful if Tags Library is enabled.":
      "包含一个「全部标签」子文件夹（可能很大）。仅在启用标签媒体库时有意义。",
    "Genre Configuration": "类型配置",
    "Genre Mode": "类型模式",
    "All Tags": "全部标签",
    "Parent Tag": "父标签",
    "(default)": "（默认）",
    "Top N": "前 N 个",
    "Genre Parent Tag": "类型父标签",
    "GENRE": "GENRE",
    "Create this tag in Stash and attach your genre tags as direct children. Only one level deep — no nesting.":
      "在 Stash 中创建该标签，并把你的类型标签作为其直接子标签。只支持一层——不允许嵌套。",
    "Top N Count": "前 N 个数量",
    "Number of tags (by scene count, descending) to use as genres. No Stash-side setup required.":
      "按场景数量降序取前 N 个标签作为类型，无需在 Stash 侧做任何配置。",
    "Playlists": "播放列表",
    "Playlist Parent Tag": "播放列表父标签",
    "Stash tag whose direct children represent Jellyfin playlists. Each child tag becomes one playlist; the scenes carrying that child tag are its items. Created on first playlist creation. Leave blank to disable.":
      "该 Stash 标签的直接子标签代表 Jellyfin 播放列表。每个子标签成为一个播放列表，带有该子标签的场景即其条目。首次创建播放列表时生成。留空则关闭该功能。",
    "Series Detection": "剧集识别",
    "Series Tag": "剧集标签",
    "Series": "Series",
    "Apply this tag to a Studio in Stash to expose it as a TV Series. Scenes get Season/Episode navigation from title parsing.":
      "在 Stash 中给某个片商打上该标签，即可将其暴露为电视剧集。场景会根据标题解析获得季/集导航。",
    "Episode Pattern List": "剧集标题规则列表",
    "One regex per line. Each must capture two groups: (season, episode). First match wins. No match → Season 0, auto-numbered.":
      "每行一条正则。每条必须捕获两个分组：(季, 集)。首个匹配生效。无匹配 → 归入第 0 季并自动编号。",
    "Test a Scene Title": "测试场景标题",
    "S02:E05 — Some Episode": "S02:E05 — 某集标题",
    "Test": "测试",
    "Multi-File Scenes": "多文件场景",
    "Advertise Every File as a Version": "每个文件作为独立版本",
    "Stash streams only a scene's primary file and exposes no endpoint for the others, so a merged scene silently plays one file. When enabled, each file becomes a selectable version in the player and the non-primary files are read straight off disk.":
      "Stash 只串流场景的主文件，且未提供访问其它文件的接口，因此合并场景只会静默播放其中一个文件。开启后，每个文件都会成为播放器中可选的一个版本，非主文件由代理直接读盘提供。",
    "Library Path Map": "媒体库路径映射",
    "Comma-separated stash-path:container-path pairs. Required by the switch above — the media library must be mounted into this container and the paths Stash reports have to be translated to paths this container can read. Left empty, or when a file is unreachable, playback falls back to the Stash stream.":
      "以逗号分隔的「Stash 路径:容器路径」映射对。上方开关依赖此项 —— 媒体库需挂载进本容器，并把 Stash 上报的路径翻译成本容器可读的路径。留空或文件不可达时，播放回退到 Stash 串流。",
    "Read on every request — no restart needed.": "每次请求时读取，无需重启。",
    "Enter a title to test.": "请输入要测试的标题。",
    "✓ Matched pattern {n}: Season {s}, Episode {e}": "✓ 匹配第 {n} 条规则：第 {s} 季第 {e} 集",
    "✗ Pattern {n} invalid: {e}": "✗ 第 {n} 条规则无效：{e}",
    "✗ No match — would go to Season 0": "✗ 无匹配——将归入第 0 季",

    /* ---- Players tab ---- */
    "Per-client rendering profiles and live User-Agent feed": "各客户端的渲染配置与实时 User-Agent 流",
    "Connected Players": "已连接的播放器",
    "Last 7 days · auto-refresh 30s": "最近 7 天 · 每 30 秒自动刷新",
    "No clients have connected yet.": "尚无客户端连接。",
    "Last seen: {time}": "最近出现：{time}",
    "Profile:": "所属配置：",
    "Copy User-Agent": "复制 User-Agent",
    "Player Profiles": "播放器配置",
    "No profiles configured.": "尚未配置播放器配置。",
    "— default —": "— 默认 —",
    "match: {v}": "匹配：{v}",
    "(empty)": "（空）",
    "Edit": "编辑",
    "Delete": "删除",
    "+ Add Profile": "+ 新建配置",
    "How to use": "使用说明",
    "To add a new client: connect it to the proxy, wait for it to appear in": "新增客户端：让它先连接代理，等它出现在",
    ", copy its User-Agent string, click": "，复制它的 User-Agent 字符串，点击",
    ", paste the string into": "，再把该字符串粘贴到",
    "User-Agent Match": "User-Agent 匹配",
    ", and configure rendering options.": "，然后配置渲染选项。",
    "Edit Player Profile": "编辑播放器配置",
    "Edit Player Profile: {name}": "编辑播放器配置：{name}",
    "Add Player Profile": "新建播放器配置",
    "Profile Name": "配置名称",
    "Lowercase, no spaces. Used as the config section name (": "小写、无空格。用作配置段名（",
    "Case-insensitive substring. Regex not supported. Leave empty for the default profile only.":
      "不区分大小写的子串匹配，不支持正则。仅默认配置可留空。",
    "Performer Display Type": "演员展示类型",
    "Person": "人物",
    "BoxSet": "合集",
    "Person: native bio + filmography (Swiftfin). BoxSet: safe fallback (Infuse, SenPlayer).":
      "人物：原生简介 + 作品列表（Swiftfin）。合集：更稳妥的降级方案（Infuse、SenPlayer）。",
    "Poster Image Format": "海报图片格式",
    "Portrait (2:3 crop)": "竖版（裁剪为 2:3）",
    "Landscape (original 16:9)": "横版（原始 16:9）",
    "Original (no processing)": "原始（不做处理）",
    "Cancel": "取消",
    "Profile name must be lowercase letters/digits/underscore": "配置名只能包含小写字母、数字与下划线",
    "Profile {name} saved.": "配置 {name} 已保存。",
    "Delete profile [{name}]? Clients matching '{ua}' will fall back to [default].":
      "删除配置 [{name}]？匹配 '{ua}' 的客户端将回退到 [default]。",
    "Profile {name} deleted.": "配置 {name} 已删除。",
    "Delete failed: {e}": "删除失败：{e}",

    /* ---- Playback tab ---- */
    "Image formatting, sort defaults, Home-tab hero": "图片处理、排序默认值、首页主视觉",
    "Poster Images": "海报图片",
    "Portrait Crop Anchor": "竖版裁剪锚点",
    "Center (default)": "居中（默认）",
    "Left": "左",
    "Right": "右",
    "Where to anchor the crop when generating a 2:3 portrait from a 16:9 screenshot. Applies only when a player's poster format is":
      "从 16:9 截图生成 2:3 竖版海报时的裁剪锚点。仅当播放器的海报格式为",
    "Per-client poster format (portrait vs landscape) is configured in the Players tab.":
      "各客户端的海报格式（竖版 / 横版）在「播放器」页配置。",
    "Sort Defaults": "排序默认值",
    "Library Type": "媒体库类型",
    "Default Sort": "默认排序",
    "Saved Filters": "已保存筛选",
    "This sort is applied when the client sends no sort preference.": "当客户端未指定排序偏好时，使用此排序。",
    "Date Added": "添加时间",
    "Name": "名称",
    "Rating": "评分",
    "Scene Count": "场景数量",
    "Random": "随机",
    "Sort Name": "排序名",
    "Strip Leading Articles": "剔除开头冠词",
    "The, A, An": "The, A, An",
    "Comma-separated. Titles beginning with these words sort by the remainder. Leave empty to disable.":
      "以逗号分隔。以此类词开头的标题将按其余部分排序。留空则关闭。",
    "Home Tab Hero Image": "首页主视觉图",
    "Hero Image Source": "主视觉图片来源",
    "Recent (default)": "最近（默认）",
    "Favorites": "收藏",
    "Top Rated": "高评分",
    "Recently Watched": "最近观看",
    "Source pool for the cinematic header image on clients' Home tab.":
      "客户端首页顶部大幅头图的来源池。",
    "Minimum Rating": "最低评分",
    "Scenes with a Stash rating at or above this threshold are eligible. Only applies when \"Top Rated\" is selected.":
      "Stash 评分不低于该阈值的场景才会入选。仅在选择「高评分」时生效。",

    /* ---- Search tab ---- */
    "Search scope and filter-panel behavior": "搜索范围与筛选面板行为",
    "Search Scope": "搜索范围",
    "Include Scenes": "包含场景",
    "Scenes appear in search results.": "场景会出现在搜索结果中。",
    "Include Performers": "包含演员",
    "Performers appear in search results as People (Swiftfin) or Collections (other clients).":
      "演员会以「人物」（Swiftfin）或「合集」（其他客户端）形式出现在搜索结果中。",
    "Include Studios": "包含片商",
    "Studios appear in search results as Collections.": "片商会以「合集」形式出现在搜索结果中。",
    "Include Groups": "包含合集",
    "Groups appear in search results as Collections.": "合集会以「合集」形式出现在搜索结果中。",
    "Filter Panel": "筛选面板",
    "Maximum Tags in Filter": "筛选中标签数量上限",
    "Maximum number of genre and tag options shown in the client filter panel. Tags are ordered by scene count, most-used first. Applies to both Genres and Tags dimensions.":
      "客户端筛选面板中显示的类型与标签选项上限。标签按场景数量降序排列，最常用的在前。对「类型」与「标签」两个维度同时生效。",
    "Multi-Select Filter Logic": "多选筛选逻辑",
    "When multiple genres are selected in the filter panel:": "在筛选面板中选中多个类型时：",
    "= scene must have all selected tags;": "= 场景必须包含全部已选标签；",
    "= scene must have any. Standard Jellyfin behavior is OR — AND is a proxy-specific enhancement.":
      "= 场景包含任意一个即可。Jellyfin 的标准行为是 OR——AND 是本代理的增强功能。",
    "Walk Tag Hierarchy": "遍历标签层级",
    "When filtering by a tag, also include scenes tagged with any of its descendants in Stash.":
      "按某标签筛选时，同时包含在 Stash 中带有其后代标签的场景。",
    "Genre Hierarchy": "类型层级",
    "Genre definitions (configured in": "类型定义（配置于",
    "Libraries → Genre Configuration": "媒体库 → 类型配置",
    ") are flat — only direct children of your genre parent tag are used as genres.":
      "）是扁平的——只有类型父标签的直接子标签会被用作类型。",
    "applies at search/filter query time only: when a user selects a genre in the filter panel, scenes tagged with that genre's descendants in Stash are included in results.":
      "仅在搜索/筛选查询时生效：当用户在筛选面板选择某个类型时，结果会包含在 Stash 中带有该类型后代标签的场景。",

    /* ---- System tab ---- */
    "Server identity, performance, logging, security, server control":
      "服务器标识、性能、日志、安全与服务器控制",
    "Interface": "界面",
    "Interface Language": "界面语言",
    "Language used by this configuration UI. \"Automatic\" follows the browser and falls back to English.":
      "本配置界面使用的语言。选择「自动」时跟随浏览器，并回退到英文。",
    "Automatic (browser)": "自动（跟随浏览器）",
    "Server Identity": "服务器标识",
    "Server Name": "服务器名称",
    "Display name shown in client server lists.": "在客户端服务器列表中显示的名称。",
    "Server ID": "服务器 ID",
    "Auto-generated UUID used by clients to identify this server. Changing this invalidates all paired clients.":
      "自动生成的 UUID，客户端用它识别本服务器。修改后所有已配对客户端都将失效。",
    "Performance": "性能",
    "Default Page Size": "默认分页大小",
    "Max Page Size": "最大分页大小",
    "Stash Timeout (seconds)": "Stash 超时（秒）",
    "Stash Retries": "Stash 重试次数",
    "Logging": "日志",
    "Log Level": "日志级别",
    "Log Directory": "日志目录",
    "Max Log File Size (MB)": "日志文件大小上限（MB）",
    "Log Backup Count": "日志备份数量",
    "Security": "安全",
    "Require Auth for Config UI": "修改配置需鉴权",
    "Prompt for the client password before allowing config changes.": "修改配置前要求输入客户端密码。",
    "IP Ban Threshold": "IP 封禁阈值",
    "Failed auth attempts before an IP is banned.": "认证失败达到该次数后封禁该 IP。",
    "Ban Window (minutes)": "封禁时间窗（分钟）",
    "Banned IPs": "已封禁 IP",
    "comma-separated; leave empty to clear": "以逗号分隔；留空则清空",
    "Comma-separated list. Edit to unban an IP. Saved immediately.": "以逗号分隔的列表。编辑可解封某 IP。立即保存。",
    "Server Control": "服务器控制",
    "Restart Proxy": "重启代理",
    "Restart the proxy process. Active streams will be interrupted.": "重启代理进程。正在进行的串流会被中断。",
    "Clear Cache": "清理缓存",
    "Flush image cache, library-card artwork, genre allow-list, and in-memory TTL caches.":
      "清空图片缓存、媒体库卡片图、类型白名单与内存 TTL 缓存。",
    "Download Config": "下载配置",
    "Download the current config file as a text backup.": "将当前配置文件下载为文本备份。",
    "Download stash_jellyfin_proxy.conf": "下载 stash_jellyfin_proxy.conf",

    /* ---- Logs tab ---- */
    "Proxy log stream, filterable by level and text": "代理日志流，可按级别与文本筛选",
    "Log Viewer": "日志查看器",
    "Auto-scroll": "自动滚动",
    "Auto-refresh": "自动刷新",
    "100 lines": "100 行",
    "250 lines": "250 行",
    "500 lines": "500 行",
    "1000 lines": "1000 行",
    "Filter by text…": "按文本筛选…",
    "Lines to fetch": "获取行数",
    "Copy visible logs to clipboard": "复制当前可见日志到剪贴板",
    "Clear display": "清空显示",
    "No log lines match the current filter.": "没有日志匹配当前筛选条件。",
    "No log lines to copy (filter removed everything).": "没有可复制的日志（筛选已排除全部）。",
    "Copied {n} log lines to clipboard.": "已复制 {n} 行日志到剪贴板。",
    "{n} lines": "{n} 行",
    "Showing {n} of {t} lines": "显示 {t} 行中的 {n} 行",
    "Failed to load logs: {e}": "日志加载失败：{e}",

    /* ---- Shared / dynamic ---- */
    "Loading…": "加载中…",
    "Proxy Running": "代理运行中",
    "Proxy Down": "代理已停止",
    "Stash {v}": "Stash {v}",
    "Stash OK": "Stash 正常",
    "Stash Error": "Stash 异常",
    "Running": "运行中",
    "Stopped": "已停止",
    "Connected": "已连接",
    "Error": "异常",
    "(not set)": "（未设置）",
    "unknown": "未知",
    "unknown error": "未知错误",
    "Saved.": "已保存。",
    "Saved. Applied live: {keys}": "已保存。即时生效：{keys}",
    "Saved. Requires restart: {keys}": "已保存。需重启生效：{keys}",
    "Save failed: {e}": "保存失败：{e}",
    "Reset failed: {e}": "重置失败：{e}",
    "Restart failed: {e}": "重启失败：{e}",
    "Overridden by environment variable": "已被环境变量覆盖",
    "Restart the proxy now? Active streams will be interrupted.": "现在重启代理？正在进行的串流会被中断。",
    "Restart initiated — reconnecting…": "已发起重启——正在重新连接…",
    "Reset proxy statistics? This clears play counts and auth counters.":
      "重置代理统计？将清空播放计数与认证计数。",
    "Statistics reset.": "统计已重置。",
    "Cache cleared: {v}": "缓存已清理：{v}",
    "Clear cache failed: {e}": "清理缓存失败：{e}",
    "Downloaded {f}.": "已下载 {f}。",
    "Download failed: {e}": "下载失败：{e}",
    "Config load failed: {e}": "配置加载失败：{e}",
    "Failed to load: {e}": "加载失败：{e}",
    "Testing…": "测试中…",
    "✓ Connected — Stash {v}": "✓ 已连接——Stash {v}",
    "✗ Connection failed: {e}": "✗ 连接失败：{e}",
    "User-Agent copied to clipboard": "User-Agent 已复制到剪贴板",
    "{n}s ago": "{n} 秒前",
    "{n}m ago": "{n} 分钟前",
    "{n}h ago": "{n} 小时前",
    "{n}d ago": "{n} 天前",
    // Uptime unit suffixes. Composed rather than concatenated so the
    // Chinese form does not inherit English spacing.
    "{d}d {h}h {m}m": "{d}天 {h}小时 {m}分",
    "{h}h {m}m": "{h}小时 {m}分",
    "{m}m {s}s": "{m}分 {s}秒",

    /* ---- Clipboard helpers ---- */
    "Copied to clipboard.": "已复制到剪贴板。",
    "Nothing to copy.": "没有可复制的内容。",
    "Copy failed — please long-press the field and pick Copy.": "复制失败——请长按该字段并选择「拷贝」。",
    "Copy failed — please select the value and copy manually.": "复制失败——请手动选中该值后复制。",
    "Copy failed: {e}": "复制失败：{e}",
    "clipboard unavailable": "剪贴板不可用",
    "Server address copied to clipboard.": "服务器地址已复制到剪贴板。",
    "Username copied to clipboard.": "用户名已复制到剪贴板。",
    "Password copied to clipboard.": "密码已复制到剪贴板。",
    "No username set.": "未设置用户名。",
    "No password set.": "未设置密码。",
    "API key": "API 密钥",
    "Value": "值",
    "{label} copied to clipboard.": "{label} 已复制到剪贴板。",
    "No {label} is set.": "未设置{label}。",
    "Could not reveal value: {e}": "无法读取该值：{e}",

    /* ---- Genre mode notes (Libraries) ---- */
    "Every tag on a scene becomes a genre. Best for small, curated tag sets.":
      "场景上的每个标签都会成为一个类型。适合标签集较小且经过整理的情况。",
    "Only tags that are direct children of your GENRE parent tag become genres. Recommended for large collections.":
      "只有 GENRE 父标签的直接子标签会成为类型。推荐用于大型收藏库。",
    "The tags with the most scenes become genres automatically. No Stash-side setup required.":
      "场景数量最多的标签会自动成为类型，无需在 Stash 侧做任何配置。",
  };

  const CATALOGS = { en: null, zh: ZH };

  /* ==================================================================
   * Engine
   * ================================================================== */
  let preference = "auto";   // "auto" | "en" | "zh"
  let lang = "en";           // effective
  let dict = null;           // null => English (identity)

  // Originals, so switching back to English is lossless.
  const origText = new WeakMap();   // Text -> original nodeValue
  const origAttr = new WeakMap();   // Element -> { attrName: originalValue }

  function interpolate(s, vars) {
    if (!vars) return s;
    return s.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
  }

  /** Translate a source string. Unknown strings pass through unchanged. */
  function t(src, vars) {
    if (src == null) return src;
    const s = String(src);
    if (!dict) return interpolate(s, vars);
    const hit = dict[s];
    return interpolate(hit === undefined ? s : hit, vars);
  }

  function normaliseKey(raw) {
    return raw.trim().replace(/\s+/g, " ");
  }

  function detectBrowser() {
    const tags = (navigator.languages && navigator.languages.length)
      ? navigator.languages
      : [navigator.language || "en"];
    for (const tag of tags) {
      const base = String(tag).toLowerCase().split("-")[0];
      if (SUPPORTED.indexOf(base) !== -1) return base;
    }
    return "en";
  }

  function serverDefault() {
    const v = String(global.SJP_DEFAULT_LANG || "auto").toLowerCase().split("-")[0];
    return SUPPORTED.indexOf(v) !== -1 ? v : "auto";
  }

  function storedPreference() {
    try {
      const v = localStorage.getItem(PREF_KEY);
      return PRECISIONS.indexOf(v) !== -1 ? v : "auto";
    } catch (_) {
      return "auto";
    }
  }

  /**
   * Effective language. A browser's explicit choice wins over the server
   * default, because the switcher exists precisely so one admin can read
   * the UI in a language different from the deployment default.
   */
  function resolveLanguage(pref) {
    if (pref === "en" || pref === "zh") return pref;
    const fromServer = serverDefault();
    if (fromServer !== "auto") return fromServer;
    return detectBrowser();
  }

  function inSkipSubtree(node) {
    const el = node.nodeType === 1 ? node : node.parentNode;
    return !!(el && el.closest && el.closest("[" + SKIP_TEXT_FLAG + "]"));
  }

  /* Decide what to do with one string. Returns null when the string is not
   * in scope for rewriting, otherwise a plan carrying both halves of the
   * rewrite:
   *
   *   src   — the English string we read
   *   out   — what we are writing in its place
   *   lead / trail — the whitespace the source had around it
   *
   * Recording `out` is what makes re-running safe. apply() walks the whole
   * document, including regions app.js has since filled via textContent or
   * innerHTML. Restoring "the original" blindly would stomp that newer
   * content — the sidebar label goes "Proxy" → "代理" at boot, then
   * pollStatus() overwrites it with "代理运行中"; a naive restore would put
   * "Proxy" back. So a node is only touched while it still holds exactly
   * what this engine last wrote into it. */
  function plan(raw, rec) {
    if (raw == null || !String(raw).trim()) return null;
    const key = normaliseKey(String(raw));
    const stillOurs = !!(rec && key === rec.out);

    if (!dict) {
      if (!stillOurs) return null;                      // nothing of ours to undo
      return { action: "restore", src: rec.src, lead: rec.lead, trail: rec.trail };
    }
    // After a re-render the node may hold a fresh English string; prefer the
    // live value over a stale record in that case.
    const srcKey = stillOurs ? rec.src : key;
    const hit = dict[srcKey];
    if (hit === undefined) return null;                 // not translated on purpose
    return {
      action: "translate",
      src: srcKey,
      out: hit,
      lead: stillOurs ? rec.lead : String(raw).match(/^\s*/)[0],
      trail: stillOurs ? rec.trail : String(raw).match(/\s*$/)[0],
    };
  }

  function applyTextNode(node) {
    const el = node.parentNode;
    if (!el) return;
    if (SKIP_TAGS.has(el.nodeName)) return;
    if (inSkipSubtree(node)) return;

    const rec = origText.get(node);
    const p = plan(node.nodeValue, rec);
    if (!p) return;
    if (p.action === "restore") {
      node.nodeValue = p.lead + p.src + p.trail;
      origText.delete(node);
    } else {
      node.nodeValue = p.lead + p.out + p.trail;
      origText.set(node, p);
    }
  }

  /* Attribute records live in a per-element bucket, so title /
   * placeholder / aria-label on the same input never collide. */
  function applyAttr(el, name) {
    if (SKIP_TAGS.has(el.nodeName)) return;
    if (el.closest && el.closest("[" + SKIP_ATTR_FLAG + "]")) return;

    let store = origAttr.get(el);
    const p = plan(el.getAttribute(name), store && store[name]);
    if (!p) return;
    if (p.action === "restore") {
      el.setAttribute(name, p.lead + p.src + p.trail);
      delete store[name];
    } else {
      el.setAttribute(name, p.lead + p.out + p.trail);
      if (!store) { store = {}; origAttr.set(el, store); }
      store[name] = p;
    }
  }

  /** Walk `root` and translate every text node and attribute in scope. */
  function apply(root) {
    const scope = root || global.document.body || global.document.documentElement;
    if (!scope) return;

    const walker = global.document.createTreeWalker(
      scope, NodeFilter.SHOW_TEXT, null
    );
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const n of nodes) applyTextNode(n);

    const elements = scope.querySelectorAll ? scope.querySelectorAll("*") : [];
    for (const el of elements) {
      for (const name of ATTRS) {
        if (el.hasAttribute && el.hasAttribute(name)) applyAttr(el, name);
      }
    }
    if (scope.nodeType === 1) {
      for (const name of ATTRS) {
        if (scope.hasAttribute(name)) applyAttr(scope, name);
      }
    }
  }

  function setLang(next, opts) {
    const options = opts || {};
    const pref = PRECISIONS.indexOf(next) !== -1 ? next : "auto";
    preference = pref;
    lang = resolveLanguage(pref);
    dict = CATALOGS[lang] || null;

    if (options.persist !== false) {
      try { localStorage.setItem(PREF_KEY, pref); } catch (_) { /* private mode */ }
    }

    const doc = global.document;
    doc.documentElement.setAttribute("lang", lang === "zh" ? "zh-CN" : "en");
    apply(doc.body || doc.documentElement);

    // Let app.js re-render regions it built with innerHTML.
    doc.dispatchEvent(new CustomEvent("sjp:langchange", {
      detail: { lang: lang, preference: preference },
    }));
  }

  function syncSwitcher() {
    const box = global.document.getElementById("lang-switch");
    if (!box) return;
    for (const btn of box.querySelectorAll(".lang-opt")) {
      btn.classList.toggle("active", btn.dataset.lang === preference);
    }
  }

  function wireSwitcher() {
    const box = global.document.getElementById("lang-switch");
    if (!box) return;
    box.addEventListener("click", (e) => {
      const btn = e.target.closest(".lang-opt");
      if (!btn) return;
      setLang(btn.dataset.lang);
      syncSwitcher();
    });
    syncSwitcher();
  }

  let booted = false;
  function boot() {
    if (booted) return;
    booted = true;
    // Persist the *effective* preference so the switcher highlights the
    // right chip on first load without writing to storage.
    setLang(storedPreference(), { persist: false });
    wireSwitcher();
  }

  global.t = t;
  global.SJP_I18N = {
    t: t,
    apply: apply,
    setLang: setLang,
    syncSwitcher: syncSwitcher,
    getLang: () => lang,
    getPreference: () => preference,
    supported: SUPPORTED.slice(),
    catalog: ZH,
    boot: boot,
  };

  // This file is loaded in <head>, so the DOM is still parsing: run after
  // it is ready but before app.js's own DOMContentLoaded handler (which
  // was registered later), so dynamic renders already see the catalog.
  if (global.document.readyState === "loading") {
    global.document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})(window);
