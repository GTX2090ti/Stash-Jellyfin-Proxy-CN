# Stash-Jellyfin-Proxy 重构方案

> 版本基线：`ef3d017`（upstream `feldorn/Stash-Jellyfin-Proxy`，v7.3.10 merge 之后）
> 本地分支：`main`（**工作区改动尚未提交**，见 §6 风险）
> 关键词：自研实现替换、多文件场景（Merge）支持、配置界面中英双语

---

## 0. 结论摘要

三条结论，按重要性排序：

1. **「用自己的实现替换原有逻辑」已经落地在两处，性质不同**：
   - **协议层（真正替换了 upstream 行为）**：多文件场景支持。upstream 的 Stash 流式接口只能吐出场景的**主文件**，我们新增了「一个场景 → 多个 MediaSource + 按需直读磁盘」的自研链路，替换了「静默只播一个文件」的原有行为。
   - **界面层（纯增量，未替换）**：中英双语。i18n 是一层运行时翻译引擎，**不改动任何代理协议逻辑**，也不改动既有界面结构。
2. **双语切换采用「英文源串作键 + 运行时 DOM 走查」**，而不是给 300 多个节点手工加 `data-i18n` 标注。理由见 §3。语言优先级：**本机浏览器选择 > 服务端 `UI_LANGUAGE` > `navigator.language` > `en`**。
3. **配置项共 3 个新增**：`UI_LANGUAGE`（界面语言）、`MULTI_FILE_SCENES`、`LIBRARY_PATH_MAP`（后两者属于多文件场景）。全部支持「配置文件 + 环境变量」双通道，环境变量优先。

验证状态：`i18n_audit.py` 5 项检查全过（381 条目 / 0 死键 / 0 接线问题）；`i18n_smoke.js` 42 项断言全过；`i18n_server_test.py` 用 Starlette TestClient 打**真实 HTTP** 41 项全过；全包 `compileall` 与 `node --check` 通过。

---

## 1. 重构范围与涉及模块

### 1.1 变更面总览

`git diff --stat`：**16 个文件改动，+698 / −113 行**；另有 **4 个新增文件**未跟踪。

| 分组 | 文件 | 增删 | 作用 |
|---|---|---|---|
| **A. 协议层自研**<br>（替换 upstream 行为） | `util/local_media.py` | **新增 98** | `LIBRARY_PATH_MAP` 路径映射 + `FileResponse` 直读磁盘（Range 由 Starlette 处理） |
| | `util/ids.py` | +21/−4 | 新增 `get_file_id()`；`get_numeric_id()` 剥离 `-f<fileId>` 后缀 |
| | `mapping/scene.py` | +167 | 新增 `_res_label()` / `version_display_name()` / `build_media_source()` |
| | `endpoints/playback.py` | +27/−2 | `/PlaybackInfo` 为每个文件下发独立 `MediaSource` |
| | `endpoints/stream.py` | +62/−1 | 非主文件改走磁盘直读，取不到则回退 Stash 流 |
| | `endpoints/items.py` | 4 | GraphQL 字段加 `files { id ... }` |
| | `endpoints/views.py` | 4 | 同上（含 episode 字段集） |
| | `endpoints/playlists.py` | 2 | 同上 |
| | `endpoints/user_actions.py` | 2 | 同上 |
| **B. i18n 层**<br>（增量） | `ui/static/i18n.js` | **新增 778** | 翻译引擎 + zh-CN 目录（381 条） |
| | `ui/static/app.js` | +238/−113 | ~70 处调用点接 `t()`；语言变更后重渲染 |
| | `ui/templates/index.html` | +41/−2 | `<html lang>`、引导脚本、侧边栏切换器、Interface 配置卡 |
| | `ui/static/app.css` | +38 | 分段切换器样式 |
| | `ui/api.py` | +22 | `{{UI_LANG}}`/`{{HTML_LANG}}` 注入 + `_P5B_KEYS` + status 字段 |
| | `runtime.py` | +21 | `MULTI_FILE_SCENES` / `LIBRARY_PATH_MAP` / `UI_LANGUAGE` 三个运行时开关 |
| | `config/bootstrap.py` | +23 | 默认值 / 配置文件 / 环境变量 / 启动日志 / publish 五处接线 |
| | `stash_jellyfin_proxy.conf` | +41 | 两段带注释的配置说明 |
| **C. 审计工具**<br>（新增） | `dev-tools/i18n_extract.py` | 新增 94 | HTMLParser 抽取界面串（不碰 JS，理由见 §3.4） |
| | `dev-tools/i18n_audit.py` | 新增 288 | A/B/C/D/E 五项静态 CI 门禁 |
| | `dev-tools/i18n_smoke.js` | 新增 415 | Node + DOM 桩无头驱动 i18n.js，42 项断言 |
| | `dev-tools/i18n_server_test.py` | 新增 142 | Starlette TestClient 打真实 HTTP，41 项断言 |

### 1.2 模块依赖方向（i18n 部分）

改动是**单向、末端**的——i18n 只挂在 UI 层，代理协议链完全不受影响：

```text
runtime.UI_LANGUAGE ──┐
config/bootstrap.py   │  环境变量 > 配置文件
   .conf / env        │
                      ▼
            ui/api.py::ui_index()
      .replace({{UI_LANG}}) / ({{HTML_LANG}})
                      ▼
   index.html  <html lang> + window.SJP_DEFAULT_LANG
                      ▼
                i18n.js（引擎，先于 app.js 加载）
                      ▼
        app.js   t()   +   DOM 走查（静态文案）
```

**代理协议链（`endpoints/*` → `mapping/*` → `stash/client.py`）不引用 i18n 任何符号**，因此 Jellyfin 协议响应体里不会混入中文，客户端侧行为零变化。

---

## 2. 保留 vs 替换：核心功能矩阵

### 2.1 保留（不动）

| 功能 | 位置 | 理由 |
|---|---|---|
| Jellyfin 协议端点全集 | `endpoints/`（除上述 5 处） | 客户端兼容性靠它，动它等于重建项目 |
| Stash GraphQL 客户端 / 重试 / 超时 | `stash/client.py` | 与 upstream 行为一致 |
| 鉴权中间件、路径中间件、日志中间件 | `middleware/` | 无改动需求 |
| 播放器画像匹配（Infuse/Swiftfin/SenPlayer） | `players/` | upstream 逻辑更完善，保留 |
| 图片策略 / 类型映射 / 剧集识别 | `mapping/*`（scene.py 除外） | 仅新增函数，未改既有函数 |
| 配置迁移 | `config/migration.py` | 新增键走默认值路径，无需迁移脚本 |
| 单元测试 | `tests/unit/*`、`tests/characterization/*` | 全部保留，未改动 |

### 2.2 替换（自研实现接管）

| 原行为 | 自研实现 | 为什么必须替换 |
|---|---|---|
| 多文件场景（Stash「Merge」）只播**主文件**，其余静默丢失 | `build_media_source()` 为每个文件生成一个 `MediaSource`；客户端出现**版本选择器** | Stash 的 HTTP 层在 `routes_scene.go` 里硬编码 `scene.Files.Primary()`，**没有任何路由或参数能取到非主文件**；`sceneStreams` 也只枚举主文件的容器/分辨率变体 |
| 非主文件无法获取 | `util/local_media.py` 按 `LIBRARY_PATH_MAP` 映射 Stash 路径 → 容器路径，直接读盘 | 唯一不改动 Stash 状态的办法。Starlette `FileResponse` 自带 206 / `Content-Range` / `Accept-Ranges`，播放器拖进度条正常 |
| 场景 ID 只能是纯数字 | `scene-123-f456` 复合 ID；`get_numeric_id()` 剥离 `-f` 后缀 | 客户端会把 `MediaSourceId` 原样回传，用后缀区分是哪个文件 |
| 界面只有英文 | 运行时 i18n 引擎 + 侧边栏切换器 | 需求本身 |

> **降级设计**：`MULTI_FILE_SCENES` 关闭、或 `LIBRARY_PATH_MAP` 为空、或文件读不到 —— 三者任一成立，都**自动回退到原有 Stash 流**（只播主文件），不会报错。也就是说这个特性「最差等于没开」。

### 2.3 新增（纯加法）

- i18n 引擎与 zh-CN 目录（`ui/static/i18n.js`）
- 侧边栏语言切换器 +「系统 → Interface Language」下拉
- `UI_LANGUAGE` 配置项全链路
- 三个 dev-tools 校验脚本

---

## 3. 界面语言切换：实现方式

### 3.1 为什么用「源串作键」而不是 `data-i18n` 标注

`index.html` 有 1026 行、320 个待译字符串。若走传统方案，要给约 300 个节点逐个手写 `data-i18n="key"`，且目录要维护一套与文案无关联的合成 ID。代价是**约 300 处手工改动落在一个零测试覆盖的模板上**——这是典型的静默回归面。

本方案改为 gettext 风格：**英文原文即键**。

- 模板**零改动**（除了语言切换器与 `<html lang>`），引擎运行时走查 DOM。
- 新增文案忘记翻译时，**不报错、不空白**——原样显示，并被 `i18n_audit.py` 报为未覆盖。
- 代价是同形异义：同一英文串只能有一种译法。需要用「同一个词、不同语境」的地方，用 `data-i18n-skip-attrs` 显式退出翻译——例如 `PLAYLIST_PARENT_TAG` 输入框的 placeholder 是 Stash 标签名示例，即使同一个词也是某张卡片标题，也不能译。

### 3.2 引擎行为

`i18n.js` 暴露 `window.t()` 与 `window.SJP_I18N`（`t / apply / setLang / syncSwitcher / getLang / getPreference / supported / catalog / boot`）。

| 项目 | 处理方式 |
|---|---|
| 文本节点 | 先做空白归一化再查表，因此源码里跨行折行的句子也能命中；写回时保留首尾空白 |
| 属性 | `title` / `placeholder` / `aria-label` 三个（每个元素独立记录，同名属性不冲突） |
| 跳过 | `<script> <style> <code> <pre> <textarea>` 子树、`[data-i18n-skip]` 子树、以及目录里查不到的串 |
| 动态区域 | app.js 用 `innerHTML` 生成的区域由 app.js 自己调 `t()`，**不靠走查** |
| 语言变更广播 | `setLang()` 派发 `sjp:langchange`，app.js 监听后重跑当前页渲染函数（`show_<page>()`）与 `populateSortDefaults()` |

### 3.3 两个关键正确性设计

1. **幂等**。`apply()` 在每次语言切换、每次 app.js 重渲染后都会跑。若「把译文再当原文查一次」，`代理 → 代理运行中` 这类会被二次污染。解法是 `plan()` 同时记录 `src`（读到的英文）与 `out`（写下去的译文），**只有当节点仍持有我方上次写入的值时才改写它**。
2. **不覆盖并发更新**。侧边栏标签在启动时被译成「代理」，随后 `pollStatus()` 会用新文案覆盖它。走查遇到这种「既不是原文、也不是我方旧值」的节点**原样放过**，并把新出现的英文串**顺势译为当前语言**——即与 app.js 协作而非互相踩。

### 3.4 为什么审计脚本不解析 app.js 的字符串

最初想用正则从 app.js 里抠出所有中文字面量，结果因**跨函数引号配对**产出大量垃圾（会出现 `|`、`)` 之类的碎片）。

改为更可靠的两条路：
- **模板侧**：`i18n_extract.py` 用 `html.parser.HTMLParser` 精确抽取文本节点与三个属性（不碰 JS）。
- **代码侧**：`i18n_audit.py` 反查 **真实 `t()` 调用点**，而不是猜字符串——这是对着实际用法做的精确校验。

`i18n_audit.py` 五项检查构成 CI 门禁（退出码 0/1）：

| 检查 | 内容 |
|---|---|
| A. COVERAGE | 模板里每个待译串都有目录条目（`ALLOW_UNTRANSLATED` 白名单除外） |
| B. CALL SITES | `t()` 里的字面键都存在于目录 |
| C. DEAD KEYS | 目录里没有不可达条目（防止目录腐化） |
| D. PLACEHOLDERS | 键与值的 `{var}` 集合一致 |
| E. WIRING | 模板 ↔ 服务端契约：占位符双向对齐、`SJP_DEFAULT_LANG → i18n.js → app.js` 加载顺序、切换器按钮与 `PRECISIONS` 一致 |

> 检查 E 是这次补上的，它盯的是 A–D 与冒烟测试都看不见的两类故障：**服务端没替换的 `{{UI_LANG}}` 会把花括号直接吐给浏览器**；**脚本顺序被打乱会静默退回英文**。两者都只让页面「看起来有点不对」，最难排查。

---

## 4. 配置项

### 4.1 界面语言

| 项 | 值 | 说明 |
|---|---|---|
| 配置文件 | `UI_LANGUAGE = auto` | `stash_jellyfin_proxy.conf`，见「Config UI language」段 |
| 环境变量 | `UI_LANGUAGE=auto\|en\|zh` | **优先于**配置文件 |
| 界面内 | 系统 → Interface Language 下拉 | 等价于写配置文件（Live 生效） |
| 侧边栏切换器 | `AUTO / 中文 / EN` | **逐浏览器**生效，存 `localStorage["sjp.ui.lang"]`，**优先级最高** |

取值语义：

| 值 | 含义 |
|---|---|
| `auto`（默认） | 跟随浏览器 `navigator.languages`，命中不了则 `en` |
| `en` | 固定英文 |
| `zh` | 固定简体中文（`<html lang="zh-CN">`） |

**为什么要「服务端默认 + 本机覆盖」两层**：部署默认值是给整个实例定的，但同一个实例可能有多个运维者，各自母语不同。侧边栏切换器写在本机 `localStorage`，所以 A 用中文、B 用英文、服务端保持 `auto`，互不影响。`boot()` 刻意**不写** `localStorage`——打开页面不等于改变偏好，否则「auto」会被一次性抹掉。

优先级（高 → 低）：本机切换器 → 服务端 `UI_LANGUAGE` → `navigator.language` → `en`。

### 4.2 多文件场景（配套项）

| 项 | 默认 | 说明 |
|---|---|---|
| `MULTI_FILE_SCENES` | `false` | 开启后，含多文件的场景会暴露多个版本 |
| `LIBRARY_PATH_MAP` | `""` | 逗号分隔的 `stash_path:container_path`，如 `/vol1/1000/HS1/PT:/library` |

需要同时给容器挂载只读卷：

```yaml
volumes:
  - /vol1/1000/HS1/PT:/library:ro
environment:
  - MULTI_FILE_SCENES=true
  - LIBRARY_PATH_MAP=/vol1/1000/HS1/PT:/library
```

启动日志会打印实际生效值，未配映射时会明确提示「non-primary files will fall back to the Stash stream」。

---

## 5. 验证步骤

```bash
# 1) 语法
python -m compileall stash_jellyfin_proxy          # Python 全包
node --check stash_jellyfin_proxy/ui/static/i18n.js
node --check stash_jellyfin_proxy/ui/static/app.js

# 2) 覆盖度 / 接线门禁（CI 用，退出码 0=通过）
python dev-tools/i18n_audit.py

# 3) 翻译引擎无头行为测试
node dev-tools/i18n_smoke.js

# 4) 服务端注入链路端到端（需 starlette + httpx）
python dev-tools/i18n_server_test.py
```

当前实测结果：

```text
Catalog entries            : 381
Strings in index.html      : 320
Literal keys in t() calls  : 91
Keys via lookup tables     : 8
Unreachable catalog entries: 0
Wiring problems            : 0
ALL CHECKS PASSED

i18n.js headless smoke test     : 42 passed, 0 failed
config-UI language e2e HTTP test: 41 passed, 0 failed
```

第 4 项打的是**真实 HTTP**（`TestClient(ui_app)`），已确认：`UI_LANGUAGE` 取 `auto/en/zh/非法值/空值` 五种输入时，响应里的 `<html lang>` 与 `SJP_DEFAULT_LANG` 均正确且非法值被钳制为 `auto`；响应体**无残留 `{{…}}` 占位符**；服务端返回的字节流里 `SJP_DEFAULT_LANG → i18n.js → app.js` 顺序正确；三个静态资源均 200 且 `i18n.js` 以 JS MIME 返回；`/api/status` 的 `uiLanguage` 与运行时一致。

人工验收（需起服务）：

1. 侧边栏点「中文」→ 整个界面即时切换，`<html lang>` 变 `zh-CN`，刷新后保持。
2. 点「EN」→ **逐字还原英文**（不是重新拼接）。
3. 点「AUTO」→ 按服务端默认 / 浏览器语言落位。
4. 切到「日志」页再切语言 → 日志区文案随语言重渲染（验证 `sjp:langchange` 生效）。
5. 客户端（Infuse/Swiftfin）侧抓一份 `/PlaybackInfo` 响应 → **里面不应出现任何中文**。

---

## 6. 风险与后续建议

### 6.1 ⚠️ 当前改动未提交（最高优先）

工作区有 **16 个修改 + 4 个新增**，`git log` 仍停在 `ef3d017`，**没有任何提交**。这意味着一次误操作的 `git checkout` / `git pull` / 重装都可能**不可逆地丢掉全部自研实现**。

建议立刻落到本地分支，保住工作成果：

```bash
git switch -c local/self-maintained      # 自研改动独立成支，main 保留 upstream 干净态
git add -A
git commit -m "feat: multi-file scene support (own impl) + zh-CN config UI"
```

好处：`main` 仍是 upstream 原样，日后同步上游只需 `git fetch && git rebase origin/main`，冲突集中在自己的补丁里。

### 6.2 上游同步风险

已改动 `endpoints/*` 与 `mapping/scene.py`，这些是 upstream 迭代最频繁的文件（尤其 `items.py` 与 `views.py` 的 GraphQL 字段串）。每次同步上游都会在这几处产生冲突，且**字段串是单行超长字符串**，冲突很难自动合并。

建议：把「`files { id ... }`」这一改动做成小补丁并记录在案，冲突时直接重打，而不是试图手工合并整行。

### 6.3 已知边界

- **同形异义**：同一英文串全局只有一种译法。新遇到冲突时用 `data-i18n-skip-attrs`（属性）或 `data-i18n-skip`（子树）显式退出。
- **`innerHTML` 区域**：依赖 app.js 自己调 `t()`。新写的动态渲染若忘了调 `t()`，界面会混入英文，但 `i18n_audit.py` 会因「`t()` 键缺失」或「目录死键」暴露出来（取决于写法）。
- **语言支持范围**：仅 `en` / `zh`。新增语种只需在 `i18n.js` 里加一份目录 + 在 `SUPPORTED` / `PRECISIONS` / 切换器里加一项，检查 E 会强制三者对齐。
- **自动化覆盖到哪、没到哪**：服务端注入链路已用 `TestClient` 走真实 HTTP 验证；翻译引擎已用 DOM 桩验证行为。**没验证的是两件必须在真环境做的事**：① 真实浏览器里的最终视觉（尤其 `innerHTML` 区域的动态文案，属 app.js 调用点覆盖问题，静态检查只能保证「调用了 `t()`」，不能保证「视觉正确」）；② `MULTI_FILE_SCENES` 的真实播放（需媒体目录已挂载进容器）。首次部署后请执行 §5 的人工验收。
