# 更新日志

> `CN.x` 为本分支自研版本号（倒序在上）；CN.3 起应用内版本号带 `CN.x` 后缀。CN.x 之前为上游（feldorn/Stash-Jellyfin-Proxy）的发布记录。

### v7.3.10-CN.4 —— Yamby 工作室导航修复（自研）

两处针对「Yamby 点工作室跳到集合 / 不显示场景」的修复：

1. **工作室条目类型**（758f3ef）：`Type=BoxSet` 的工作室会被 SenPlayer / Yamby
   路由进客户端的「集合」视图。新增 `studio_type` 每客户端配置
   （`players/profiles.py` + `mapping/image_policy.py::studio_item_type`），
   SenPlayer / Yamby 默认发 `Type=Studio`（语义正确），Infuse 等仍为
   `BoxSet + CollectionType=movies`。覆盖 `/Items/{id}`、root-studios、
   saved-filter 工作室行与视图内工作室栏。
2. **`GenreIds=studio-N` 导航**（本版）：开启 DEBUG 请求日志实测发现，Yamby
   点工作室时并不请求 `/Users/{u}/Items/studio-N`，而是发
   `GET /Users/{u}/Items?IncludeItemTypes=Movie,Series&GenreIds=studio-N&Recursive=true`
   ——把工作室 id 塞进 GenreIds 里。此前该形状被静默丢弃，工作室页拿不到
   该工作室的场景。现在 `_parse_filter_params` 识别 `studio-N` 形状并翻译成
   Stash 的 `studios INCLUDES` 过滤（`endpoints/items.py`），流派
   `genre-N` 形状不受影响。

新增单测 `tests/unit/test_genreids_studio_filter.py`（6 个）+ 
`test_studio_type_profiles.py`（5 个），全量 284 通过。

### v7.3.10-CN.3 —— 多文件版本名带上真实文件名（自研）

修复多文件（合并）场景在客户端版本选择器里的命名：此前版本名只有「分辨率 + 编码」
（如两行一模一样的 `1080p H264`），文件名完全丢失——合并场景的多个文件只要分辨率和
编码相同，用户就无法分辨哪行播的是哪个文件。

**修复后的命名规则**（`mapping/scene.py::multi_file_source_title`，
`format_jellyfin_item` 与 `endpoints/playback.py` 共用）：

| 文件情况 | 版本名 |
|---|---|
| 有文件名 + 有探测数据 | `second.mp4 (1080p H264)` |
| 有文件名、无探测数据 | `second.mp4` |
| 两者都缺 | `File <fileId>` |
| 同名冲突（不同目录同名文件） | 追加 `(#<fileId>)` 去重 |

要点：主文件的 MediaSource 名也换成文件名（仅 `MULTI_FILE_SCENES` 开启且场景含多个
文件时；单文件场景零变化）；客户端按 `Name` 区分版本行，跨源去重是硬要求。

**测试**：`tests/unit/test_scene_mapping.py` 新增 5 个测试，共 273 个单元测试通过。

详细说明见 [MULTIFILE-AND-I18N.md](MULTIFILE-AND-I18N.md) §2.8。

### v7.3.10-CN.2 —— 元数据刮削管线（自研）

为代理补上 Stash 的刮削能力桥接：Jellyfin 客户端（手机 App 的 Identify / 刷新元数据）→ 代理 → Stash 已装社区刮削器 + 已配置的 Stash Box → 结果写回 Stash。

**新增能力。**

- **三类入口**：按名搜索（`provider=all` 全扇出 / 指定社区刮削器 / 指定 Stash Box）、纯数字编号、URL 直刮。
- **数字编号多形态路由**：`SCRAPE_NUMERIC_URL_TEMPLATES` 支持一个刮削器多个 URL 形态（`|` 分隔）。默认 `fantiajp=…/posts/{id}|…/products/{id}`——Fantia 的帖子与商品是共用编号的不同对象，两边都返回 200，以前只合成 posts 形态导致数字查询刮出完全无关的结果。
- **相关性排序**：并发扇出从「先到者胜」改为「等待全部定向命中 → 按相关性排序 → 返回多结果（上限 3 组）」。相关性 = 候选 payload 与**条目自身文件名 / 标题**的 token 重叠数（camelCase 拆词，`LyaCutie` ↔ `Lya Cutie`），匹配形态置顶、其余保留但降级；同源 URL 去重（条目自身 URL 与同形态合成 URL 只显示一次）。
- **搜索词与排序词分离**：搜索用短净文本，排序用「标题 + 文件名」全文——`buena-320s.mp4` 的文件名才是区分 Fantia 同号双对象的唯一信号。
- **Stash Box 接入**：`source {stash_box_index}`；box 清单从 `configuration.general.stashBoxes` 发现并缓存；文件名自动归一化为演员查询；`SCRAPE_STASHBOX_ENABLED` 开关（UI 系统面板实时可改）。

**实测证据**（飞牛 NAS 真机）：Fantia `1006291` → 商品 `buena-320` 置顶（无关的帖子「青橙」降级第二，落库终态 `code=buena-320`）；`0541-LyaCutie-2160p` 识别 **0 → 10 条**结果（ThePornDB，performer 精确匹配）。267 项单元测试通过。

**限制**：StashDB / ThePornDB **不收录**小众站场景（如 Cospuri 编号场景），box 给出的是同演员其他站的候选，需人工挑选；精确刮削需自写站点刮削器且要求 NAS 有可达该站的出网路径。

完整排障记录见 [SCRAPING-NOTES.md](SCRAPING-NOTES.md)。

### v7.3.10-CN.1 —— 多文件（合并）场景 + 配置界面中英双语（自研）

**多文件（合并）场景。** 上游只下发合并场景的主文件——根因是 Stash 侧硬限制（流式路由硬编码 `scene.Files.Primary()`，无按文件取流的 API）。本分支的实现：

- `/PlaybackInfo` 为每个文件下发独立 `MediaSource`（复合 ID `scene-13-f751`），客户端天然出现版本选择器
- 非主文件由代理按 `LIBRARY_PATH_MAP` 翻译路径后**直读磁盘**（`FileResponse`，206 分片、拖进度条正常）；主文件仍走 Stash 原生流，零回归
- 默认关闭（`MULTI_FILE_SCENES` + `LIBRARY_PATH_MAP`）；开关关闭 / 映射为空 / 文件不可读均**静默回退**到原生流，不会因配错导致播放失败

**配置界面中英双语。** 8 标签页全部支持中英切换：gettext 风格「英文原串作键 + 运行时 DOM 走查」（zh-CN 目录 387 条），模板零标注；幂等重入、不与动态渲染抢节点；侧边栏切换（localStorage）与服务端 `UI_LANGUAGE` 两层优先级。**协议层零改动**，切换语言不影响客户端播放。

**一并修复**：`Content-Disposition` 携带中日文文件名时 HTTP 头（latin-1）编码崩溃成 500 空响应——按 RFC 6266 同时输出 ASCII 回退名与 `filename*=UTF-8''` 编码名。

客户端侧媒体库名汉化（场景/厂商/演员/分组/播放列表）同轮并入代码树，不再依赖单独挂载手改文件。

详细方案、部署形态与踩坑记录见 [MULTIFILE-AND-I18N.md](MULTIFILE-AND-I18N.md)。

### v7.3.10

@tanlidoushen 在 #28 上的第四次报告——我最初漏看并在未处理的情况下关闭了该 issue——Yamby 场景详情页头部中的视频流芯片（chip）渲染为空白，而音频芯片显示正常。

**根因。** `mapping/scene.format_jellyfin_item` 在为音频 `MediaStream` 构建时带了 `DisplayTitle`（例如 `"AAC - Stereo"`），但视频流完全没有 `DisplayTitle`。Jellyfin SDK 客户端从 `MediaStreams[].DisplayTitle` 渲染详情头部的流芯片，所以缺失的值会渲染为空白。而页面底部的媒体信息面板直接使用 `Width` / `Height` / `Codec`，这正是该面板不受影响的原因。

**修复。** 当已知宽高时，视频流现在按 Jellyfin 约定获得 `DisplayTitle` 与 `Title`，格式为 `{分辨率档位} {CODEC}`——`"4K H264"`、`"1080p HEVC"`、`"720p H264"`、`"SD MPEG4"`，或对不常见高度使用 `"{h}p {CODEC}"`。当尺寸未知时不编造标签（芯片保持空白，而非谎报媒体信息）。按报告者验证过的代码，按其建议应用。

**测试。** `tests/unit/test_scene_mapping.py` 中新增 5 个测试，覆盖每个分辨率档位及无尺寸的兜底情况。共 133 个测试通过。

### v7.3.9

关闭 #28——三个相关的标签/流派 bug，均由 @tanlidoushen 报告并定位根因，且本地修复已确认。

**Bug 1 —— `TAG_GROUPS` 文件夹对较短/常见的标签名（例如 `POV`）为空。** `endpoints/items.py`（两处）与 `endpoints/views.py` 中的标签查找使用了 Stash 的 `findTags(filter: {q: <name>})`，且默认 `per_page: 25`。当一个短标签名是许多更长标签的子串时（「Anal POV」、「Doggy POV」……中的 `POV`），精确匹配的标签可能被排到第一页之外。随后的 `.lower() == .lower()` 检查便失败 → 「未找到标签」→ 空文件夹。已在这三处调用点加上 `per_page: -1`，以匹配 `search.py`/`playlists.py` 中已有的模式。

**Bug 2 —— `GenreIds` 筛选被静默忽略（Yamby Android 应用）。** `endpoint_genres` 发出的流派条目带有 `Id: "genre-<stash-tag-id>"`。`endpoints/items.py` 中的 `_parse_filter_params` 读取了 `Genres` / `Tags` / `Years`（基于名称），但没有读取 `GenreIds`（基于 id），所以当 Yamby 在点击后将 id 回传时，该参数被静默丢弃，整个媒体库在未经筛选的情况下返回。现在：

- `endpoints/search.py` 维护一个模块级的 `_GENRE_ID_NAMES` 字典（`"genre-<id>" → 名称`），由 `endpoint_genres` 在每次 `/Genres` 响应时填充（包括父筛选和库级两个分支）。
- `_parse_filter_params` 读取 `GenreIds`，在字典中查找每个 id，并将解析出的名称追加到 `genres`——于是现有的标签名筛选路径接管。未解析的 id 静默丢弃（客户端预期在点击流派前调用 `/Genres`，每个 SDK 客户端都会这样做）。

**Bug 3 —— 场景详情页没有流派区块（Yamby 及其他 Jellyfin SDK 客户端）。** `mapping/scene.format_jellyfin_item` 中有两处缺口：

- `_SCENE_FIELDS` GraphQL 片段（在 `items.py` 中两处相同代码）获取了 `tags { name }` 但没有 `id`——即使映射代码想要，也没有可用于附加的 id。现在获取 `tags { name id }`——为对称也扩展到 `studio.tags` 与 `parent_studio.tags`（负载小幅增加，无害）。
- 条目字典发出了 `Genres: string[]`（遗留，Infuse/Swiftfin 使用）但从未发出 `GenreItems: NameGuidPair[]`（当前 Jellyfin SDK 用来渲染详情页流派行）。现在发出 `GenreItems`，其 `Id: "genre-<tag-id>"` 匹配 `endpoint_genres` 生成的形状，以便点击流派时能通过 Bug 2 的 `GenreIds` 解析器往返。

**中间件规范映射扩展。** 在 #27 的映射基础上新增了 `genreids`、`genres`、`tags`、`years`，使全小写的客户端（Roku 风格）命中相同的 `.get("GenreIds") / .get("Genres")` 读取。

**测试。** 新增 6 个：4 个在 `tests/unit/test_scene_mapping.py` 中用于 `GenreItems` 发出（形状、缺失 id 处理、空标签情况、顺序保持），2 个在 `tests/unit/test_middleware_paths.py` 中用于新的规范映射条目。总计 128 个测试通过。

### v7.3.8

关闭 #27——Roku 的制片商 / 演员磁贴为空，因为 Roku Jellyfin 频道发送**全小写的查询参数名**（`parentid=`、`startindex=`、`personids=`），而 `endpoints/items.py` 中的处理器只读取了混合大小写和 camelCase 拼写。由 @madlens95 报告并定位根因——包括确认的本地修复，以及建议在中间件中与已有的路径规范化一并规范化查询字符串的方案。

**比 v7.3.0 的路径修复低一层。** `CaseInsensitivePathMiddleware`（arsfeld 在 v7.3.0 为小写路径引入）只重写了 `scope["path"]`。`scope["query_string"]` 原样保留，所以 `/items/` → `/Items` 规范化正常，但查询中的 `parentid=studio-5` 仍保留其小写拼写，`.get("ParentId") or .get("parentId")` 落到 None。

**修复——在同一中间件中规范化查询字符串。** 新的 `_normalize_query_string` 辅助函数解析查询字符串，将每个参数名的小写形式与规范拼写映射（`{"parentid": "ParentId", "startindex": "StartIndex", …}`）匹配，命中时重写。未知参数原样通过；当查询已是规范形式时，恒等检查快速路径跳过解析+编码。代码库中每个现有的 `.get("ParentId") or .get("parentId")` 链现在对**所有**客户端大小写都生效——Infuse camelCase、Swiftfin PascalCase、Roku 小写——无需改动处理器。新的端点只需读取规范拼写，即可免费获得所有客户端支持。

受影响的参数（现在全部规范化）：`ParentId`、`StartIndex`、`Limit`、`Ids`、`PersonIds`、`SearchTerm`、`SortBy`、`SortOrder`、`SeasonId`、`EntryIds`、`Filters`、`Name`、`IncludeItemTypes`，以及（新增）`StudioIds`。

**另外（按报告者的附带观察）：** `endpoints/items.py` 现在处理裸 `StudioIds` 查询参数，将其映射到 `ParentId=studio-N`，使仅通过 `StudioIds` 筛选制片商的客户端（而非将制片商嵌进 `ParentId`）到达相同代码路径。逗号分隔值取第一个条目，以匹配现有 `studio-` 分支的单制片商语义。

**测试。** `tests/unit/test_middleware_paths.py` 中新增 14 个测试，覆盖既有的路径规范化（回归）与新的查询规范化：小写 → 规范、混合大小写 → 规范、规范为无操作、未知参数通过、空 QS 为无操作、值不被触碰、以及同请求中的端到端路径 + QS。

### v7.3.7

由一位在 iPad Brave 上测试 v7.3.6 的用户报告：新的复制按钮没有任何作用。在排查了许多误导性线索后，真正的 bug 其实是**静态资源缓存**——浏览器执行的是缓存中的 v7.3.6 之前版本的 `app.js`，根本没看到新的 `.pw-copy` 处理器。v7.3.6 的代码本身没问题。

**在每个版本发布时以及每次重启时都让静态资源缓存失效。**

- `index.html` 现在引用 `/static/app.js?v={{ASSET_V}}` 与 `/static/app.css?v={{ASSET_V}}`。模板替换注入 `<__version__>-<PROXY_START_TIME>`，所以任何版本变更或重启都会强制浏览器重新获取。iOS Safari（以及 iPad 上包装 WebKit 的 Chromium 浏览器——包括 Brave）对静态资源缓存非常激进，以至于硬刷新都不可靠地逐出；查询字符串变更才是可靠的修复。

**防御性的剪贴板改进**——在缓存角度清晰之前，基于报告 bug 的迭代而来。

- `copyText()` 现在检测 iOS/iPad（`navigator.platform` 上的 `/iP(hone|ad|od)/`，或以触摸能力「Mac」出现的设备——iPad 在桌面站点模式下的自我报告方式）并直接走 iOS 友好路径：`<textarea readonly>` + `focus` + `select` + `setSelectionRange(0, len)` + `document.execCommand("copy")`。`.select()` 之后的 `setSelectionRange` 是 iOS Safari 需要的特定步骤。
- 新的 `copyLazy(getText, msg)` 辅助函数用于异步获取值的情况（连接标签页上的掩码字段揭示）。在非 iOS Chromium 上使用 `ClipboardItem({"text/plain": Promise<Blob>})` + `navigator.clipboard.write`——跨 `await` 保留瞬时用户激活的正确模式。在 `writeText` 之前 await 会在 Chrome/Brave/Edge 中丢弃用户手势，并以 `NotAllowedError` 被拒绝。
- 复制按钮在点击时闪烁 `✓` 700 毫秒，所以即使剪贴板操作本身失败，移动用户也能立即获得点击已被接收的视觉反馈。
- `copyText` 的 `execCommand` 兜底现在正确地将 `false` 返回视为失败（之前在复制实际未发生时静默提示「成功」）。

### v7.3.6

连接（Connection）标签页上的小型体验增强：在你会在设置期间输入到兼容 Jellyfin 的播放器的四个字段旁边加了复制按钮（⧉）——**API 密钥、公共 URL、用户名、密码**。与仪表盘上的「连接播放器」模态框（它过去是、现在仍是一次抓取全部三者的最快方式）思路与辅助函数相同，但适用于那些先落在连接标签页、又不想在触摸设备上从 `<input>` 中选择并复制的人。

掩码字段（密码、API 密钥）在复制前通过现有的 `/api/config/reveal` 端点获取真实值，所以剪贴板携带的是真实密钥，而非星号或空白。复用了 v7.2.0 引入的 `.pw-wrap` / `.pw-reveal` 样式与 `copyText()` 剪贴板辅助函数。

### v7.3.5

关闭 #26（@stashcollection14）——Stash 每场景的「总播放时长」列一直为零，因为代理从未在 `sceneSaveActivity` 上发送 `playDuration` 参数。播放次数和进度更新正确（在 v7.3.4 修复）；时长没有。

**根因。** Stash 的 `sceneSaveActivity(id, resume_time, playDuration)` 变更将 `playDuration` 参数*累加*进场景的总时长中。我们在 `endpoints/views.py` 中的三处调用点（进度、停止 >90%、停止 ≤90%）只传了 `resume_time`，所以 play_duration 从未被触碰。

**修复。** 现在三处调用点都发送 `playDuration = 自该串流上一次事件以来的挂钟秒数`。该增量来自一个小辅助函数（`_consume_watched_delta`），它从被追踪的 `_active_streams` 记录读取 `last_progress_time`，更新它，并返回上限为每次事件 60 秒的增量。使用挂钟时间（而非位置增量）意味着该计数不受拖动影响，并在行为良好的客户端上自然正确地处理暂停（它们在暂停时停止触发 Progress 事件，所以不计时间）。每次事件 60 秒的上限防御了客户端长时间停滞或延迟的 Progress 事件，否则会夸大计数。

每个事件的日志行现在包含所加的增量，便于在日志中抽查：

```
⏸ Saved resume + recorded play: scene-3814 at 1570s (21%, +14s duration)
▶ Auto-marked played: scene-3814 (100% watched, +8s duration)
```

### v7.3.4

关闭 #25 第二部分——部分播放的会话未被记录到 Stash 的 `play_history` 中。报告者 @tanlidoushen 观看了 Hills Lite 的一个场景到 21%，退出，并期望该场景出现在 `?sortby=last_played_at&sortdir=desc` 的顶部。续播位置被正确地保存了，但 `play_count` 从未递增，也没有写入 play_history 条目——所以 Stash 不知道发生了一次播放。

**根因。** `endpoints/views.py` 在 `Sessions/Playing/Stopped` 上仅当用户观看 >90% 时才调用 `sceneAddPlay`。低于此值的都被当作纯粹的「进行中」——保存续播位置，不记录播放。在此策略下，观看半个场景后退出的用户在 Stash 历史中没有任何该会话的证据。

**修复。** 任何停止位置超过 30 秒阈值的会话现在都通过 `sceneAddPlay` 记录一次播放（它递增 `play_count`、添加 `play_history` 条目并推进 `last_played_at`）。低于 30 秒的会话仍只保存续播位置——30 秒是「短暂点按」阈值，低于它我们假设是意外点击或漂移的拖动，而非真实观看。现有的 >90% 自动标记行为不变（仍记录播放并清除续播位置）。

**需注意的行为变更。** 每个现有的 Infuse / Swiftfin / SenPlayer / Roku 用户都会开始在 Stash 播放历史中看到部分观看会话，并反映在 `last_played_at` 中。这与 Stash 自带的 Web UI、Plex、Trakt 及大多数媒体系统追踪「你看过这个」的方式一致——但如果你之前依赖「只有完整观看才算数」，这个版本会改变它。

### v7.3.3

#25 中三个问题里的两个（由 @tanlidoushen 报告）。第三个——Hills Lite 的「继续观看」——仍在等待报告者提供日志片段调查中。

**GraphQL 别名提示不再作为警告记录**（#25 问题 1a）

- Stash GraphQL 响应中的 `errors` 数组混合了真实错误与信息性提示——例如当配置名通过 Stash 别名而非主名解析时（`"name 'SERIES' is used as alias for '系列'"`）。代理曾将整个数组以 `WARNING` 记录，所以使用非英文主标签名的用户每次查找都会看到虚假的噪音。匹配 `is used as alias for` 的提示现在以 `DEBUG` 记录；真实错误仍以 `WARNING` 记录。

**生成媒体库封面上 CJK 字形**（#25 问题 1b）

- 标签组虚拟媒体库封面生成器（`util/images.py`）使用带 DejaVu Sans Bold 的 PIL，其缺少 CJK 字形——中文/日文/韩文标签名在封面上渲染为豆腐块（`[ ] [ ]`）。在标签包含 CJK / 半角全角范围内任何字符（码点 ≥ 0x2E80）时，新增了优先使用的 CJK 可用字体路径列表。纯拉丁标签继续使用 DejaVu，所以现有封面在视觉上不变。
- **Dockerfile：** 新增 `fonts-noto-cjk`，使 Noto Sans CJK Bold TTC 在容器内可用。原生（非 Docker）安装需要自行安装 CJK 字体；选择器会在标准系统路径自动找到 Noto CJK、PingFang 或 Hiragino Sans GB。

### v7.3.2

修复问题 #24——Windows 原生运行时的 `UnicodeDecodeError`（Docker 用户从未受影响）。由 @stashcollection14 报告，并给出确切的单行修复。

**根因**

- Python 的 `Path.read_text()` 与 `open(path, 'r')` 在未提供编码时使用平台的首选编码。在 Linux/macOS 上是 UTF-8；在 Windows 上是 `cp1252`，其无法解码 v7.2.0 仪表盘模板为「连接播放器」密码揭示引入的眼球 emoji（👁 / 🙈）。代理在 Windows 模块导入时崩溃，报错 `'charmap' codec can't decode byte 0x81`。

**修复**

- 在生成代码中每次文本模式文件打开/读取/写入都加上 `encoding='utf-8'`：仪表盘模板（被报告的代码点）、配置文件读写（加载器、写入器、辅助函数、迁移、v7.3.1 的 heal-append、仪表盘配置保存器、封禁 IP 写入器）、日志尾读取器、统计 JSON、认证调试转储。同样的潜在 bug 存在于 16 处；报告者恰好命中了导入时崩溃的那一处。任何读取或写入用户内容文本的地方现在都显式按 UTF-8 解码/编码，与平台无关。
- 回归测试：`tests/unit/test_encoding.py` 锁住 `index.html` 包含 `cp1252` 无法解码的字节，所以从 `ui/api.py` 移除显式 `encoding='utf-8'` 会在 Windows 上重新引入崩溃并使 CI 失败。

### v7.3.1

关闭 v7.3.0 的一个缺口：现有的 v2 安装升级到 v7.3.0 时不会在其配置或播放器标签页中看到新的 `[player.roku]` 配置档，因为 `V2_DEFAULT_PLAYERS` 仅在那些安装已经运行过的一次性 v1→v2 迁移期间被查询。任何后续添加默认配置档的发布对它们都是不可见的。

- **缺失默认播放器配置档的启动修复。** 在 schema 迁移短路（配置已为 `CONFIG_VERSION = 2`）之后，检查 `V2_DEFAULT_PLAYERS` 中用户配置缺少的任何区段并追加它们。幂等、仅追加——绝不修改或移除现有区段，所以手动自定义的 `[player.roku]` 原样保留。当配置只读时为无操作（记录问题并继续）。将 `V2_DEFAULT_PLAYERS` 视为一个鲜活的「每个安装都应拥有」列表，而非冻结的 v1→v2 快照。

### v7.3.0

Roku Jellyfin 应用支持，加上一个影响任何发送全小写 URL 的客户端的路径匹配 bug。全部四个提交由 @arsfeld 创作，从其 fork 拉入。

**Roku 支持**

- 新的 `[player.roku]` 配置档（横版海报、BoxSet 演员类型）——仅在全新安装 / v1→v2 迁移时添加；现有的 v2 安装需要通过播放器标签页添加它。
- Roku 应用探测的四个新端点桩，仅 iOS 客户端不探测：`/System/Configuration/Encoding`（声明仅直接播放、无转码）、`/Items/{id}/Images/Logo[/{index}]`（静默 404——Stash 没有 logo 概念）、`/Items/{id}/Images`（声明 Primary + Backdrop；空列表会在详情屏使 Roku 应用崩溃）、以及显式的 `/Items/Suggestions` 路由（之前匹配 `/Items/{item_id}`，`item_id="Suggestions"` 并作为数字 id 被发往 Stash GraphQL）。

**路径规范化（影响所有客户端）**

- `CaseInsensitivePathMiddleware` 之前仅在请求路径与其小写形式不同时才运行模板匹配，所以任何发送全小写路径（`/items/scene-11/images`）的客户端静默绕过了静态映射和模板匹配器，落到 `catch_all`。Roku 这样做；其他客户端也可能。现在在静态映射未命中时总是运行模板匹配。
- 尾部斜杠兜底：`/items/?…` 现在匹配注册的 `/Items` 路由。显式以尾部斜杠注册的路由（`/Playlists/`）仍通过首次查找解析，所以兜底不会遮蔽它们。

**播放诊断**

- `PlaybackInfo` 入口、`PlaybackInfo` 响应（含封装格式/编解码器/分辨率/码率/时长/字幕数）、以及串流端点入口从 DEBUG 提升到 INFO。生产 INFO 日志现在显示每次播放尝试的三行追踪，而非客户端导航与现有 `▶ Stream started` 标记之间的静默间隙。有助于诊断 Roku/Streamyfin 直接播放 vs 转码的失败。

### v7.2.0

仪表盘上新的「连接播放器」界面与可配置的公共地址，使播放器所需的凭据和服务器 URL 在一处可见，而非分散在配置中。

**连接播放器**

- 仪表盘头部按钮打开「连接播放器」模态框，显示服务器地址、用户名与密码，每个都带复制按钮。密码以眼球揭示开关掩码，并在模态框关闭时重新掩码。作为偶用弹窗而非常驻仪表盘卡片展示。

**公共 URL**

- 新的 `PUBLIC_URL` 配置键（连接 → 公共地址，实时——无需重启）用于外部可达的 Jellyfin API 地址。代理无法自动检测此地址——它自己的 IP 是内部 Docker 地址，且在 SWAG 等反向代理之后，公共主机/协议/端口存在于代理上——所以连接卡片仅在设置了 `PUBLIC_URL` 后才显示服务器地址，否则显示提示。没有误导性的自动猜测地址。

**密钥揭示**

- 连接标签页上的 API 密钥与客户端密码字段获得眼球揭示。由于表单将密钥输入留空以使「留空 = 不变」在保存时成立，揭示会惰性地从新的白名单 `GET /api/config/reveal` 端点获取真实值，隐藏时再次清除——输入新值则保留编辑。

**修复**

- 下载配置现在在反向代理后可用：将顶层 `window.location` 导航（SWAG 可拦截并丢弃同源上下文）替换为带凭据的 `fetch` → Blob 下载，从 `Content-Disposition` 解析文件名。

### v7.1.7

问题 #16（SERVER_ID 轮换）+ 问题 #17（Infuse 中的收藏）——打包在一起，因为 #17 的根因被证明与 #16 同属配置写入器的 bug 家族。

**收藏**

- `FAVORITE_TAG` 的大小写不敏感比较。配置 `FAVORITE_TAG=FAVORITE` 对应现有名为 `Favorite` 的 Stash 标签，曾静默破坏 `IsFavorite` 读取——代理正确应用了标签（Stash 的标签查找大小写不敏感）但在读取时返回 False，所以 Infuse 从未反映回收藏，也从未发送移除。
- 当 Stash 写入失败（例如标签无法创建）时，切换处理器不再声称成功；响应现在反映实际的前置状态。

**配置写入器**

- 仪表盘保存全新键现在插入到全局作用域（在第一个 `[section]` 头部之前，任何 `# ==== ... ====` 分隔线之上）。之前新键分支追加到文件末尾，加载器将那里的 `KEY = VALUE` 绑定到尾部区段的字典——所以刚设置的 `FAVORITE_TAG` 最终变成 `cfg_sections["player.default"]["FAVORITE_TAG"]`，在运行时不可见。插入逻辑通过 `find_global_insert_idx` 辅助函数在 `save_config_value`（单键写入）与仪表盘处理器（批量写入）间共享。
- 读取时修复预扫描：当仪表盘处理器读取配置时，它会剥离任何位于 `[section]` 块内、属于已知全局键的行，并记录 `Hoisting misplaced global key out of [...]: <KEY>`。下一次保存会重新插入到全局作用域，所以现有文件自愈。
- 注释去重，使 `CONFIG_LAST_BOOT_AT` 的每次启动重写不会累积同一注释行的副本。
- 写入前折叠空行漂移（每次重启多一个空行）。

**配置持久化诊断**

- `SERVER_ID` 与 `ACCESS_TOKEN` 在首次生成时持久化（在 v7.0.0 中每次启动都重新生成，破坏了客户端重连——问题 #16）。
- 跨重启持久化检测器：引导时每次启动写入 `CONFIG_LAST_BOOT_AT` 和一次性的 `CONFIG_PERSISTENCE_INTRODUCED` 标记。结合是否加载了 `SERVER_ID`，将文件分类为 `persisted` / `not_persistent` / `not_writable` / `unverified` 并在仪表盘展示。捕获匿名卷 / tmpfs / 缺失 `/config` 挂载等从用户侧看像保存 bug 的情况。之前的 `os.access + open(r+)` 可写性探测被保存并回读往返取代。
- 仪表盘新增 `not_writable`（现有）与 `not_persistent`（新）横幅，各指向可能的原因。

**版本报告**

- `stash_jellyfin_proxy/__init__.py` 中的单一 `__version__` 常量。仪表盘 `/api/status`、启动日志横幅与 HTML 品牌徽章都读取它；之前三个独立硬编码字符串在各版本间漂移（仪表盘停在 v7.0.0，启动横幅停在 v7.1.1）。

### v7.1.0

**播放列表**

- 新的 `Playlists` 库，由可配置父标签（`PLAYLIST_PARENT_TAG`，默认 `Playlists`）支撑。该标签的每个直接子标签就是一份播放列表；携带该子标签的场景即为其条目。
- 完整的 Jellyfin `PlaylistsController` 表面：创建、重命名、添加/移除条目、删除、列出用户——每个变更都有保护，使得只有配置父标签的直接子标签可被触碰。
- 按客户端渲染：Infuse 与 Jellyfin Web 客户端用原生 `Playlist` 类型（完整的创建/编辑/删除 UI）；Swiftfin 与 SenPlayer 用 `BoxSet` 形状（它们的 UI 缺少原生 Playlist 渲染器——可浏览和播放但无法管理）。配置档标志 `playlist_native` 在需要时按客户端覆盖。
- 播放列表磁贴渲染为场景截图合成，播放列表名作为标签叠加（与 TAG_GROUPS 外观相同）。
- 播放列表父标签及其子标签从通用标签列表、搜索提示与每场景的标签 / 流派中自动隐藏，使标记标签不渗入 UI 的其余部分。

### v7.0.0

该项目历史上最大的发布——多月的重构（阶段 0 → 5B）加上一波发布后的打磨。

**架构与打包**

- **现在是一个正规的 Python 包**。用 `python -m stash_jellyfin_proxy` 或 `stash-jellyfin-proxy` 控制台脚本运行。顶层的 `stash_jellyfin_proxy.py` 启动器已移除——Dockerfile、compose 与 CI 都调用该包。**对已自行固定 Docker `CMD` 的用户是破坏性变更**；已发布的镜像不受影响。
- **全程异步 httpx**——`requests` 不再是依赖。串流是真正的字节区间透传，使用 `aiter_bytes()`。
- 模块布局拆分为 `endpoints/`、`mapping/`、`middleware/`、`players/`、`stash/`、`state/`、`ui/`、`util/`、`config/`、`cache/`。唯一事实来源在 `runtime.py`。
- 纯 ASGI 请求日志中间件，使串流不被包裹。
- v1 → v2 配置迁移在启动时运行一次，并在 Web UI 中以横幅提示变更摘要。
- TTLCache 实时追踪 Stash 连接性，而非每次请求轮询。
- 全局错误契约（`StashUnavailable` / `StashError` / `BadRequest`）带一致的 JSON 形状。
- 特性描述测试工具 + 92 个单元测试。

**剧集支持（阶段 2）**

- 带有 `SERIES_TAG` 的制片商被视为电视剧。它们的场景在各处成为单集——列表、详情、图片、搜索。
- 按客户端的 `series_collection_type`：Swiftfin → `tvshows`（通过 `/Shows/{id}/Seasons` + `/Shows/{id}/Episodes` 的原生「剧集 → 季 → 单集」导航）；其他客户端 → `movies`（扁平 BoxSet）。
- 通过 `SERIES_EPISODE_PATTERNS` 的单集标题解析链，在 Web UI 中带 regex 测试器。
- 制片商/剧集详情页获得完整的 About 元数据；季磁贴渲染横版；单集海报强制横版。
- 自动创建标签大小写不敏感（配置 `Series` 匹配现有 `series`）。

**播放器配置档（阶段 2）**

- 按客户端行为由 `[player.*]` 配置区段驱动——UA 子串匹配、首胜、默认兜底。
- 配置档控制 `performer_item_type`、`scene_poster_format`、`series_collection_type`。
- 独特 UA 捕获到 `ua_log.json` 并在 Web UI 中展示，支持一键创建配置档。

**图片（阶段 3）**

- 感知宽高比的图像接口，带有真实竖版裁切和可配置锚点（`POSTER_CROP_ANCHOR`）。
- 在 SERIES 兜底链中，制片商 Logo 优先于场景截图。
- 媒体库磁贴重新设计：场景截图背景 + 50% 变暗 + 标签叠加，应用于媒体库根与 TAG_GROUPS。
- 按进程重启的 `ImageTag` 轮换破除原生客户端图片缓存。

**流派与筛选面板（阶段 3 §7.1，阶段 4 §8.5）**

- `GENRE_MODE`：`all_tags` / `parent_tag` / `top_n` 带 `GENRE_PARENT_TAG` / `GENRE_TOP_N`。
- Swiftfin 筛选抽屉：年份、流派、标签、已喜欢、已播放——在 `/Items` 路径与搜索中全程遵守。
- AND/OR 流派逻辑；感知层级的标签筛选（深度：-1）。
- 流派 + 标签在显示与每场景中按字母排序。

**首页 / 焦点图（阶段 4 §8.2，§8.4）**

- `HERO_SOURCE` 可在 `recent` / `random` / `favorites` / `top_rated` / `recently_watched` 间配置，带 `HERO_MIN_RATING`。
- 各媒体库默认排序（`SCENES_DEFAULT_SORT`、`STUDIOS_DEFAULT_SORT` 等）用于未指定 SortBy 的客户端。
- 阶段 4 §8 首页标签页 + 筛选面板 + 排序默认值 + 媒体库美术定稿。

**元数据（阶段 3 §7.2）**

- 排序冠词剥离（`SORT_STRIP_ARTICLES`）使「The X」排在 X 下。
- `OFFICIAL_RATING` 作为配置键暴露（默认 `NC-17`）。
- 场景元数据：完整 About 面板内容、标签行、详情页上的父制片商数据。

**Web UI（阶段 5A + 5B）**

- 8 标签页侧边栏导航取代单页 UI：仪表盘、连接、媒体库、播放器、播放、搜索、系统、日志。
- 实时测试连接探针、带实时 UA 反馈的按客户端播放器配置档编辑器、客户端侧剧集-单集 regex 测试器。
- 实时仪表盘带活动串流 + 最热播场景 + 持久化累计统计。
- HTML/CSS/JS 提取到模板 + `/static/app.css` + `/static/app.js`。
- 保存行为徽章带一致的符号与悬停提示。
- 日志标签页带筛选、下载与复制按钮。

**发布后打磨**

- 媒体库根 `ImageTag` 轮换扩展到 TAG_GROUPS 磁贴。
- Swiftfin：筛选参数在搜索 + 全局 `/Items` 路径中遵守；制片商 + 合集页上的 rail 探测泄漏修复；CollectionType + LATEST_GROUPS 启动崩溃修复；`/Shows` 端点接线；演员页不再显示 7 个空白类别 rail。
- SenPlayer：收藏切换响应现在为完整 `UserItemDataDto`；横幅通过 `BANNER_MODE` / `BANNER_POOL_SIZE` / `BANNER_TAGS` 显示随机场景。
- Findroid / iPad 客户端：缺失端点桩化；所有 BoxSet 文件夹项设置 ImageBlurHashes。
- 停止将 `/` 重定向到 `/System/Info/Public`——官方 Jellyfin 应用的启动探针依赖 `/`。
- 剧集根磁贴不再渲染空白（缺失 `MENU_ICONS` 条目）。
- `/Items//` 双斜杠警告加固。

### v6.02

- **SenPlayer 首页横幅**：SenPlayer 的轮播横幅现在由带截图的随机场景驱动——两种模式，`recent` 与 `tag`，通过 Web UI 暴露。
- **按场景唯一的 `ImageTag` 与 `Etag`**：每个场景有独特的 `ImageTags.Primary`（`p<id>`）与 `BackdropImageTags`（`b<id>`）；`Etag` 派生自播放状态，使客户端在状态变化时重新获取。
- **收藏切换修复**：`POST` / `DELETE` 在 `/Users/{userId}/FavoriteItems/{id}`（及 `UserFavoriteItems` 别名）返回完整 `UserItemDataDto`，使客户端 UI 无需导航往返即对账。
- **DateLastContentAdded 排序**：SenPlayer 对制片商 / 演员 / 合集的默认排序键现在映射到 `created_at`。

### v6.01

- **合集收藏**通过用于场景的相同 `FAVORITE_TAG` 标签切换方法（`movieUpdate` 变更 + 每个合集查询中的 `tags { name }`）。

### v6.00

- 多客户端支持（Infuse、SenPlayer 完全；Swiftfin 及其他部分——在 v7.0.0 完全收尾）。
- 完整的 Swiftfin 兼容走查：`/UserFavoriteItems/` 别名、每个 BoxSet 项上的 `ImageBlurHashes`。
- 播放 / 续播 / 已看同步——`play_count`、`resume_time`、`last_played_at` 与 Stash 往返。>90% 已看 = 自动已看 + 清除续播。
- 场景基于标签的收藏（取代损坏的 `organized` 方法）、演员原生字段、制片商 `studioUpdate`。
- `RunTimeTicks` 始终存在于 `MediaSources` 中；停止处理器在客户端发 0 时从 Stash 解析时长。
- Android 客户端支持：大小写不敏感路径中间件；`/ClientLog/Document` 桩。
- 丰富的 `MediaStreams` 元数据（编解码器、分辨率、码率、帧率、声道布局）。

### v5.04

- 跨演员 / 制片商 / 合集 / 标签 / 已保存筛选器的排序支持。
- 移除流派/标签上限。

### v5.03

- 此前加载失败的场景的部分日期 ISO-8601 修复。
- 无图片的演员将其 `PrimaryImageTag` 设为 null。

### v5.02

- 丰富的 `MediaStreams` 元数据。
- 字幕下发（SRT / VTT）。
- 已保存筛选器浏览。
- 演员 / 制片商 / 合集图片服务。
- 基于标签的媒体库文件夹。

### v5.00

- 初始发布：Jellyfin API 模拟、Stash GraphQL 集成、Web UI、Docker。
