# Stash-Jellyfin-Proxy 重构方案

> 面向交付/使用的说明文档见 [`docs/multifile-and-i18n.md`](docs/multifile-and-i18n.md)（多文件场景修复 + 界面汉化、部署形态、踩坑记录、变更清单）。
>
> 版本基线：`ef3d017`（upstream `feldorn/Stash-Jellyfin-Proxy`，v7.3.10 merge 之后）
> 本地分支：`local/self-maintained`（自研改动已提交，`main` 保持上游干净态）
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
| `LIBRARY_PATH_MAP` | `""` | 逗号分隔的 `stash_path:container_path`，如 `/data:/library` |

> ⚠️ **左边是「Stash 上报的路径」，不是宿主路径。** Stash 自己把 `/vol1/1000/HS1` 挂成 `/data`，因此它上报 `/data/PT/...`；本容器把同一棵树挂成 `/library`，映射才是 `/data:/library`。写成宿主的 `/vol1/1000/HS1/PT:/library` 是**不匹配**的 —— 会静默回退且日志无报错。真实前缀从 Stash 的 GraphQL 读一条 `files { path }` 即可确认。

需要同时给容器挂载只读卷：

```yaml
volumes:
  - /vol1/1000/HS1:/library:ro        # 挂载点与 Stash 自身一致
environment:
  - MULTI_FILE_SCENES=true
  - LIBRARY_PATH_MAP=/data:/library   # Stash 容器内路径 : 本容器内路径

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

### 6.1 ✅ 已提交到自研分支（2026-09-13）

原本 23 个改动 + 4 个新增全部散在工作区、`HEAD` 停在 `ef3d017`，一次误操作的 `git checkout` 就能不可逆丢掉。现已落到独立分支：

```bash
git switch -c local/self-maintained
# dc0989d  feat: own multi-file scene implementation + zh-CN config UI
# 7f94f4e  feat(ui): expose the multi-file scene knobs in the config UI
# efce417  fix(multi-file): non-ASCII filenames broke the stream response
# d346fa6  test: cover local_media path mapping and Content-Disposition encoding
```

`main` 仍是 upstream 干净态（`ef3d017`），日后同步上游只需 `git fetch && git rebase origin/main`，冲突集中在自己的补丁里。提交用内联 `-c user.name=... -c user.email=...`，没有写进本地或全局 git 配置。

### 6.2 上游同步风险

已改动 `endpoints/*` 与 `mapping/scene.py`，这些是 upstream 迭代最频繁的文件（尤其 `items.py` 与 `views.py` 的 GraphQL 字段串）。每次同步上游都会在这几处产生冲突，且**字段串是单行超长字符串**，冲突很难自动合并。

建议：把「`files { id ... }`」这一改动做成小补丁并记录在案，冲突时直接重打，而不是试图手工合并整行。

### 6.3 已知边界

- **同形异义**：同一英文串全局只有一种译法。新遇到冲突时用 `data-i18n-skip-attrs`（属性）或 `data-i18n-skip`（子树）显式退出。
- **`innerHTML` 区域**：依赖 app.js 自己调 `t()`。新写的动态渲染若忘了调 `t()`，界面会混入英文，但 `i18n_audit.py` 会因「`t()` 键缺失」或「目录死键」暴露出来（取决于写法）。
- **语言支持范围**：仅 `en` / `zh`。新增语种只需在 `i18n.js` 里加一份目录 + 在 `SUPPORTED` / `PRECISIONS` / 切换器里加一项，检查 E 会强制三者对齐。
- **自动化覆盖到哪、没到哪**：服务端注入链路已用 `TestClient` 走真实 HTTP 验证；翻译引擎已用 DOM 桩验证行为。**没验证的是**：真实浏览器里的最终视觉（尤其 `innerHTML` 区域的动态文案，属 app.js 调用点覆盖问题，静态检查只能保证「调用了 `t()`」，不能保证「视觉正确」）。多文件场景已于 2026-09-13 在飞牛 NAS 真机验证通过，见 §7。

---

## 7. 部署实况（2026-09-13，飞牛 NAS）

### 7.1 部署形态：目录挂载覆盖镜像内置包

不改镜像，用 bind mount 把整个包目录覆盖掉：

```yaml
volumes:
  - /vol2/1000/HSX/docker/stash-jellyfin-proxy:/config
  - /vol2/1000/HSX/docker/stash-jellyfin-proxy/app:/app/stash_jellyfin_proxy   # 自研代码
  - /vol1/1000/HS1:/library:ro                                                # 媒体库
```

三个关键约束（都已踩过验证）：

| 约束 | 原因 |
|---|---|
| `/app/stash_jellyfin_proxy` 必须 **rw** | `docker-entrypoint.sh` 每次启动执行 `chown -R ${PUID}:${PGID} /app`，脚本带 `set -e`，只读挂载会让 chown 失败并**直接中断启动** |
| 媒体库挂载点必须与 Stash 的挂载**完全一致** | Stash 自己把 `/vol1/1000/HS1` 挂成 `/data`，所以它上报的路径是 `/data/PT/...`；本容器也挂成 `/library`，`LIBRARY_PATH_MAP=/data:/library` 才是同构映射。当初按宿主路径 `/vol1/1000/HS1/PT` 去猜是错的 |
| 部署前必须核对容器内是否已有手改 | 原部署在 compose 里单独挂了一个手改的 `views.py`（客户端侧媒体库名汉化）。整体覆盖目录会**静默回退**这项改动 —— 已合并进 `endpoints/views.py` 后统一由自研树提供 |

回滚 = 把 `_backup/<ts>/app.prev` 换回 `app/` + `docker compose up -d`，秒级完成，不用重建镜像。

### 7.2 部署流程（可重复执行）

部署工具在**工作区级** `dev-tools/`（不是仓库内的 `sjp/dev-tools/`）：

```
dev-tools/
  nas_exec.py              # 把本地脚本上传到 /tmp 再执行，输出落盘
  nas_get.py               # 从 NAS 取文件（用于 diff 容器内手改）
  build_deploy_bundle.py   # 打包 sjp/stash_jellyfin_proxy -> deploy/app.tar.gz + sha256 清单
  nas-scripts/
    01-recon-stash.sh           # Stash 上报路径前缀 / 找出真实多文件场景
    02-recon-container.sh       # 容器内是否另有手改（docker diff + CJK 扫描）
    03-recon-content-diff.sh    # 与镜像逐文件比 md5，排除「只是 mtime 变了」的假阳性
    10-install-code.sh          # 备份 + 解包 + sha256 校验 + 同文件系统 rename 换树
    11-apply-config.sh          # 写 compose（含 docker compose config -q 校验）
    13-fix-conf-scope.sh        # 修正键作用域，并用应用自身 loader 证明
    12-restart-verify.sh        # 重建容器 + 21 项端到端断言
    16-final-state.sh           # 汇总部署后状态
```

校验强度：本地打包时算 sha256 清单 → NAS 上 `sha256sum -c` 逐文件比对，**58/58 通过**才换树。文件数先与镜像内的文件集合比对过（镜像 53 个 `.py` 一个不缺，只多出新增的 `util/local_media.py`），避免整体覆盖时丢掉上游模块。

### 7.3 真机验证结果（21/21 通过）

| 断言 | 结果 |
|---|---|
| 启动日志 `Multi-file scenes: enabled (library path map: /data:/library)` | ✅ |
| `/api/config` → `MULTI_FILE_SCENES=True` / `LIBRARY_PATH_MAP=/data:/library` | ✅ |
| `/api/status` → `uiLanguage=auto` | ✅ |
| 配置界面含 `i18n.js` + 语言切换器，**无残留 `{{占位符}}`** | ✅ |
| `/UserViews` 返回 `["场景","厂商","演员","分组","播放列表"]`（手改汉化未丢） | ✅ |
| `scene-13`（2 文件）`PlaybackInfo` → **2 个 MediaSource**：`scene-13` + `scene-13-f751`(4K HEVC) | ✅ |
| `scene-14`（单文件）→ 1 个 MediaSource（无回归） | ✅ |
| **非主文件串流** `GET /Videos/scene-13-f751/stream` Range 0-1023 → **206 / 1024 字节**，带 `accept-ranges` + `content-range: bytes 0-1023/3035622018` | ✅ |
| 重启后日志无 traceback | ✅ |

### 7.4 两个只有真机会暴露的坑（已修）

**① 配置文件不是扁平的 `KEY=value`。** `.conf` 用了 INI 式 `[player.xxx]` 节作用域（见 `config/loader.py`），而 player 节在文件末尾。把新键**追加到文件末尾**会落进 `[player.default]` 节，应用读不到、静默用默认值 —— 表面 `GET /api/config` 一切正常，实际 `MULTI_FILE_SCENES` 仍是 `False`，多文件场景照旧只播一个文件，且**不报任何错**。修法是插到第一个 `[` 之前，并用应用自己的 `load_config()` 验证键落在 global 作用域、`player.*` 节零泄漏。

**② HTTP 头是 latin-1，文件名带 CJK 会炸。** 非主文件由代理直读磁盘后，`local_file_response` 把 basename 原样塞进 `Content-Disposition`；日文文件名在 ASGI 发响应阶段抛 `UnicodeEncodeError`，客户端只看到 **500 空体**，而日志里**连 traceback 都没有**（只有一行 `ERROR ... 'latin-1' codec can't encode...`），因为路径映射和读盘其实都已经成功了。改为同时输出 RFC 6266 的 ASCII 回退名 + RFC 5987 的 `filename*=UTF-8''` 百分号编码名。已加 `tests/unit/test_local_media.py`（22 项）钉死，含此前完全无覆盖的路径映射行为。

### 7.5 运维注意

- **重启（含重建）约需 95–100 秒**才对外服务：启动阶段要先对 2300+ 场景的 Stash 解析媒体库计数，之后才 listen。期间客户端会连不上，属正常。
- 打包环境是 Python 3.13、镜像是 3.11，已用 `ast.parse(..., feature_version=(3,11))` 全包扫过语法兼容性。
- 宿主 `8096` 与 Jellyfin 默认端口冲突，两者不能同时监听（原注释已保留）。
- `_backup/` 下 7 个时间点备份、`docker-compose.yml.bak-*` 均保留；根目录的 `views.py` / `views.py.bak` 已不再被 compose 引用（内容已并入 `app/endpoints/views.py`），留作历史备份。
