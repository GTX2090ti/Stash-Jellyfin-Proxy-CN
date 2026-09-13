# 多文件（合并）场景修复 + 配置界面中英双语

> **项目**：Stash-Jellyfin-Proxy 自研分支（让 Infuse / SenPlayer / Swiftfin 等 Jellyfin 客户端直连 Stash）
> **上游基线**：`ef3d017`（feldorn/Stash-Jellyfin-Proxy，v7.3.10）
> **自研分支**：`local/self-maintained`
> **部署环境**：飞牛 NAS `192.168.2.210`，2026-09-13 真机验证通过
> **本文覆盖**：多文件场景修复、配置界面汉化、部署形态、踩坑记录、变更清单

---

## 0. 速览

这一轮解决了两件事，性质完全不同 —— 一件是**改行为**，一件是**加一层**。

| | 多文件（合并）场景 | 配置界面中英双语 |
|---|---|---|
| 性质 | 替换 upstream 原有行为 | 纯增量，不改协议逻辑 |
| 默认状态 | **关闭**（需显式开启） | **开启**（`auto`，中文浏览器自动中文） |
| 相关配置 | `MULTI_FILE_SCENES`、`LIBRARY_PATH_MAP` | `UI_LANGUAGE` |
| 影响范围 | Jellyfin 客户端播放行为 | 仅配置网页（`8097`） |
| 客户端的观感 | 一个场景出现**多个版本**可选 | **无变化**（协议响应不含中文） |
| 验证 | NAS 真机 21/21 断言 | 静态门禁 + 无头引擎 + 真实 HTTP 共 4 组 |
| 未验证 | 真实播放器里的版本选择器 UI | 真实浏览器里的最终视觉 |

一句话：**汉化开箱即用；多文件场景代码已就绪并已在 NAS 上打开，但它是「默认关闭」的开关式特性。**

---

## 1. 问题与结论

### 1.1 问题一：合并场景只播出来一个文件

在 Stash 里把一个场景（scene）关联多个文件（社区叫 Merge / 合并场景）之后，用 Infuse 等客户端播放时**只有主文件能看，其余文件完全不出现**，客户端里连切换入口都没有。

### 1.2 问题二：配置界面只有英文

代理自带一个配置网页（`http://<NAS>:8097`），全部文案硬编码英文，且是**服务端静态模板**（只替换 `{{SERVER_NAME}}` / `{{ASSET_V}}` 两个占位符），没有任何前端文案层。

### 1.3 结论

| 问题 | 根因 | 处置 |
|---|---|---|
| 只能播主文件 | **Stash 不可能给出非主文件**（详见 §2.2），不是代理的 bug | 绕过 Stash 流接口，非主文件由代理**直读磁盘** |
| 界面全英文 | 模板无文案层 | 新增运行时 i18n 引擎（详见 §3.3） |

> 「用我自己的实现替换原有逻辑」这句话落在两个不同层级：**多文件场景是真的替换了 upstream 的协议层行为**（原行为是静默丢弃非主文件）；**汉化则没有替换任何逻辑**，只是一层挂在界面末端的翻译引擎。

---

## 2. 修复一：多文件（合并）场景

### 2.1 现象

| 观察点 | 修复前 | 修复后 |
|---|---|---|
| `/Items/<scene>/PlaybackInfo` → `MediaSources` | 1 个（仅主文件） | 每个文件 1 个 |
| 客户端版本选择器 | 不出现 | 出现（文件名/分辨率作为版本名） |
| 非主文件可否播放 | 否 | 可（代理直读磁盘，支持拖进度条） |
| 单文件场景 | 1 个源 | 1 个源（无回归） |

### 2.2 根因

**这是 Stash 侧的硬限制，不是代理配置问题。** 在 Stash 源码 `internal/api/routes_scene.go` 里：

- 所有流式路由**硬编码**取 `scene.Files.Primary()`；
- 路由里**没有任何**按 file id 取流的路径段或查询参数（只有 `start` / `resolution` / `segment`）；
- GraphQL 的 `sceneStreams` 字段只枚举**主文件**的容器/分辨率变体，它**不是文件选择器**；
- `VideoFile` 类型**没有** `primary` 标记字段，客户端甚至无法从 API 判断哪个文件是主文件。

结论：**任何「播放非主文件」的方案都必须绕过 Stash 的流接口。** 可选的只有两条路 —— 改 `primary_file_id`（改数据，破坏性）或用 ffmpeg 把文件拼成一条（耗时且不可逆）。本实现选了第三条：**代理自己读磁盘**。

### 2.3 方案

三处配合：

1. **每个文件一个 `MediaSource`** —— `mapping/scene.py::build_media_source()` 为场景的每个文件生成独立媒体源，`endpoints/playback.py` 全部下发。客户端因此天然出现「版本选择器」，不用任何自定义 UI。
2. **复合 ID** —— 媒体源 ID 形如 `scene-13-f751`（场景 ID + `-f` + Stash 文件 ID）。客户端会把 `MediaSourceId` 原样回传，代理据此知道该播哪个文件；`util/ids.py::get_numeric_id()` 负责把后缀剥掉还原成纯场景 ID。
3. **非主文件直读磁盘** —— `endpoints/stream.py` 判断请求的是非主文件时，走 `util/local_media.py`：按 `LIBRARY_PATH_MAP` 把 Stash 上报的路径翻译成容器内路径，用 Starlette 的 `FileResponse` 返回。`FileResponse` 自带 `206` / `Content-Range` / `Accept-Ranges`，所以**拖进度条、跳转都正常**。

### 2.4 生效条件（三步，缺一即静默降级）

```yaml
volumes:
  - /vol1/1000/HS1:/library:ro                                    # ① 媒体库挂进容器（只读）
environment:
  - MULTI_FILE_SCENES=true                                        # ② 打开开关
  - LIBRARY_PATH_MAP=/data:/library                               # ③ 路径映射
```

**降级设计**：开关关闭、映射为空、文件读不到 —— 三者任一成立，都**自动回退到 Stash 原生流**（即只播主文件），**不报错**。也就是说这个特性「最差等于没开」，不会因为配错而导致播放失败。

代价是**配错时没有任何报错**。所以启动日志是唯一的判据：

```text
Multi-file scenes: enabled (library path map: /data:/library)     ← 生效
Multi-file scenes: UNSET — non-primary files will fall back to the Stash stream   ← 未生效
```

### 2.5 `LIBRARY_PATH_MAP` 怎么填（最容易错的一步）

格式是 `Stash上报的路径前缀:容器内路径前缀`，逗号分隔可写多组。

**左边必须写「Stash 自己上报的路径」，不是你挂载时的宿主路径。** 左不匹配 → 静默回退，且**日志无任何报错**。

本环境的正确推导过程：

| 环节 | 事实 |
|---|---|
| Stash 容器怎么挂 | `/vol1/1000/HS1 → /data`（只读） |
| 所以 Stash 上报的路径是 | `/data/PT/xxx/yyy.mp4` |
| SJP 容器怎么挂 | `/vol1/1000/HS1 → /library`（只读） |
| 所以映射应为 | `/data:/library` |

> ⚠️ 如果按宿主路径想当然写成 `/vol1/1000/HS1/PT:/library`，**永远不会命中** —— 因为 Stash 上报的是 `/data/PT/...`。这是本次部署中真实踩到的坑（见 §6.1 的相关说明）。

**怎么查真实前缀**（不用猜）：

```bash
docker exec stash-jellyfin-proxy sh -c '
  K=$(sed -n "s/^STASH_API_KEY *= *//p" /config/stash_jellyfin_proxy.conf | tr -d "\"")
  curl -s -H "ApiKey: $K" -H "Content-Type: application/json" \
    -X POST http://127.0.0.1:9999/graphql \
    -d "{\"query\":\"{ findScenes(filter:{per_page:1}){ scenes{ files{ path } } } }\"}"
'
```

返回的 `path` 就是左边该写的值。

### 2.6 实测证据（NAS 真机，2026-09-13）

```text
启动日志      Multi-file scenes: enabled (library path map: /data:/library)

scene-13（含 2 个文件）
  SRC  scene-13        ❤ スターレ〇ル …（主文件，走 Stash 原生流）
  SRC  scene-13-f751   4K HEVC（非主文件，代理直读磁盘）
  → MediaSource count = 2      （修复前为 1）

scene-14（单文件）
  → MediaSource count = 1      （无回归）

GET /Videos/scene-13-f751/stream   Range: bytes=0-1023
  → 206 Partial Content
  → content-range: bytes 0-1023/3035622018
  → accept-ranges: bytes      content-type: video/mp4
```

> `/UserViews` 同时返回 `["场景","厂商","演员","分组","播放列表"]` —— 说明先前通过单文件挂载做的手工汉化没有在本次整体覆盖中丢失（见 §6.3）。

### 2.7 客户端表现

- **Infuse / Swiftfin / SenPlayer**：条目上会出现版本/媒体源选择，选中后正常播放并支持跳转。
- 由于主文件仍走 Stash 原生流，**主文件的播放路径与修复前完全一致**，不存在回归风险。

---

## 3. 修复二：配置界面中英双语

### 3.1 怎么用

| 入口 | 位置 | 生效范围 | 是否持久 |
|---|---|---|---|
| 侧边栏切换器 | 左下角 `AUTO / 中文 / EN` | **当前浏览器** | 存 `localStorage`，刷新保持 |
| 系统 → Interface Language | 系统标签页的下拉框 | **整个实例**（服务端默认值） | 写入配置文件 |
| 配置文件 / 环境变量 | `UI_LANGUAGE` | 整个实例（默认值） | 重启后仍生效 |

### 3.2 语言优先级

```text
本机切换器（localStorage）  >  服务端 UI_LANGUAGE  >  navigator.language  >  en
```

`UI_LANGUAGE` 取值：

| 值 | 含义 |
|---|---|
| `auto`（默认） | 跟随浏览器 `navigator.languages`，命不中就 `en` |
| `en` | 固定英文 |
| `zh` | 固定简体中文（`<html lang="zh-CN">`） |

**为什么设计成两层**：部署默认值是给整个实例定的，但同一个实例可能有多个运维者、母语不同。侧边栏选择只写本机 `localStorage`，所以 A 用中文、B 用英文、服务端保持 `auto`，互不干扰。反过来，打开页面**不会**写 `localStorage` —— 否则「auto」会被第一次访问一次性抹掉。

### 3.3 实现方式：英文源串作键 + 运行时 DOM 走查

**不用 `data-i18n` 标注。** 配置模板有 1051 行、327 个待译字符串；传统做法要给约 300 个节点逐个手写 `data-i18n="key"` 并维护一套与文案无关的合成 ID —— 约 300 处手工改动落在一个零测试覆盖的模板上，是典型的静默回归面。

本方案是 gettext 风格：**英文原文本身就是键**。

| 项目 | 处理方式 |
|---|---|
| 目录 | `i18n.js` 内的 `const ZH = {...}`，共 **387** 条 |
| 文本节点 | 先做空白归一化再查表（因此源码里跨行折行的句子也能命中），写回时保留首尾空白 |
| 属性 | `title` / `placeholder` / `aria-label`，每个元素独立记录，同名属性不冲突 |
| 跳过 | `<script> <style> <code> <pre> <textarea>` 子树、`[data-i18n-skip]` 子树、目录里查不到的串 |
| 动态区域 | app.js 用 `innerHTML` 生成的区域**由 app.js 自己调 `t()`**，不靠走查 |
| 语言变更 | `setLang()` 派发 `sjp:langchange`，app.js 监听后重跑当前页渲染函数 |

配套 API：`window.t()` 与 `window.SJP_I18N`（`t / apply / setLang / syncSwitcher / getLang / getPreference / supported / catalog / boot`）。

模板因此**零标注改动**，只加了 `<html lang>`、引导脚本、侧边栏切换器和一个 Interface 配置卡。

**代价（需要知道的边界）**：同一英文串全局只有一种译法（同形异义无法区分）。遇到必须区分的场景，用 `data-i18n-skip-attrs`（属性级）或 `data-i18n-skip`（子树级）显式退出翻译 —— 例如某输入框的 placeholder 是「Stash 标签名示例」，即使词面与某卡片标题相同也不能译。

### 3.4 两个关键正确性设计

这两点都由测试钉死，是引擎能安全重入的前提：

1. **幂等**：`apply()` 在每次语言切换、每次 app.js 重渲染后都会跑。若把译文再当原文查一次，「代理」会被污染成「代理运行中」之类。解法是 `plan()` 同时记录 `src`（读到的英文）与 `out`（写下去的译文），**只有当节点仍持有我方上次写入的值时才改写它**。
2. **不与 app.js 抢节点**：侧边栏标签启动时被译成「代理」，随后 `pollStatus()` 会用「代理运行中」覆盖它。走查遇到这种「既不是原文、也不是我方旧值」的节点**原样放过**，并顺手把新出现的英文串译为当前语言 —— 与 app.js 协作而非互相踩。

### 3.5 作用范围：只汉化配置界面

**代理协议层一行没动。** `endpoints/*`、`mapping/*`、`stash/client.py` **不引用 i18n 的任何符号**，因此 Jellyfin 协议响应体里不会混入中文，切换界面语言**不影响客户端播放、不影响元数据**。

需要区分两个「汉化」：

| | 作用对象 | 做法 | 结果 |
|---|---|---|---|
| **本文的汉化** | SJP 的**配置网页** | 运行时 i18n 引擎 | 界面可中英切换 |
| 客户端侧媒体库名汉化 | **Jellyfin 客户端里看到的分类名** | 改 `endpoints/views.py` 的库名常量 | 客户端显示「场景/厂商/演员/分组…」 |

后者不是本文新增的，是先前已存在的改动（原先以单独挂载一个手改 `views.py` 的形式存在），本次已**并入自研代码树**，不再单独挂载。

### 3.6 覆盖度与门禁

`dev-tools/i18n_audit.py` 五项检查，可作 CI 门禁（退出码 0/1）：

| 检查 | 内容 |
|---|---|
| A. COVERAGE | 模板里每个待译串都有目录条目 |
| B. CALL SITES | `t()` 里的字面键都存在于目录 |
| C. DEAD KEYS | 目录里没有不可达条目（防目录腐化） |
| D. PLACEHOLDERS | 键与值的 `{var}` 集合一致 |
| E. WIRING | 模板 ↔ 服务端契约：占位符双向对齐、`SJP_DEFAULT_LANG → i18n.js → app.js` 加载顺序、切换器按钮与引擎 `PRECISIONS` 一致 |

> 检查 E 盯的是 A–D 都看不见的两类故障：**服务端漏替换的 `{{UI_LANG}}` 会把花括号直接吐给浏览器**；**脚本顺序被打乱会静默退回英文**。两者都只让页面「看起来有点不对」，最难排查。

---

## 4. 配置项

### 4.1 三个新增键

| 键 | 默认 | 说明 | 界面可改 |
|---|---|---|---|
| `UI_LANGUAGE` | `auto` | `auto` / `en` / `zh`，配置界面默认语言 | ✅ 系统 → Interface Language |
| `MULTI_FILE_SCENES` | `false` | 开启后合并场景的每个文件作为独立版本下发 | ✅ 媒体库 → Multi-File Scenes |
| `LIBRARY_PATH_MAP` | `""` | 逗号分隔的 `Stash路径:容器路径` 对 | ✅ 同上卡片 |

三者**都支持配置文件 + 环境变量双通道，环境变量优先**；且都在界面里可改、**Live 生效**（按请求读取，不需要重启）。

### 4.2 配置文件写法

写在 `stash_jellyfin_proxy.conf` 的 **global 作用域**：

```ini
UI_LANGUAGE       = auto
MULTI_FILE_SCENES = true
LIBRARY_PATH_MAP  = /data:/library
```

> ⚠️ **必须插在第一个 `[section]` 之前。** 该文件是 INI 风格且带 `[player.default]` 等节；追加到文件末尾的键会落进 `[player.*]` 节作用域，**应用读不到、静默用默认值**。详见 §6.2。

### 4.3 compose 片段（当前 NAS 实际生效）

```yaml
services:
  stash-jellyfin-proxy:
    image: bxo2rw2icxm7rl40ud-ghcr.xuanyuan.run/feldorn/stash-jellyfin-proxy:latest
    container_name: stash-jellyfin-proxy
    restart: unless-stopped
    ports:
      - "8096:8096"    # Jellyfin API（播放器连这里）
      - "8097:8097"    # 配置界面
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Asia/Shanghai
    volumes:
      - /vol2/1000/HSX/docker/stash-jellyfin-proxy:/config
      # 自研代码覆盖镜像内置包。必须 rw，见 §5.2
      - /vol2/1000/HSX/docker/stash-jellyfin-proxy/app:/app/stash_jellyfin_proxy
      # 媒体库（只读），挂载点与 Stash 自身的挂载一致
      - /vol1/1000/HS1:/library:ro
```

---

## 5. 部署记录与回滚

### 5.1 部署形态：目录挂载覆盖镜像内置包

**不去改镜像、不重新构建**，而是把自研的包目录 bind mount 到 `/app/stash_jellyfin_proxy`，覆盖镜像内同名目录。

好处：镜像保持上游原样，日后上游更新只需改 tag；回滚成本极低（见 §5.4）。

### 5.2 三条硬约束

| 约束 | 原因 |
|---|---|
| 代码挂载点必须 **rw**，不能 `:ro` | 镜像的 `docker-entrypoint.sh` 每次启动执行 `chown -R ${PUID}:${PGID} /app`，脚本带 `set -e` —— 挂成只读会让 chown 失败并**直接中断启动** |
| 媒体库挂载点要与 **Stash 的挂载同构** | Stash 把 `/vol1/1000/HS1` 挂成 `/data` 并据此上报路径，本容器挂成 `/library`，映射 `/data:/library` 才是同构 |
| 部署前必须核对**容器内是否已有手改** | 原部署单独挂了一个手改的 `views.py`；整体覆盖目录会静默回退它 |

### 5.3 部署流程（可重复执行）

部署工具在工作区级 `dev-tools/`（不在仓库内）：

```text
dev-tools/
  nas_exec.py                上传本地脚本到 NAS /tmp 并执行，输出落盘到本地 logs/
  nas_get.py                 从 NAS 取文件（用于比对容器内的手改）
  build_deploy_bundle.py     打包 stash_jellyfin_proxy/ -> deploy/app.tar.gz + sha256 清单
  nas-scripts/
    01-recon-stash.sh          查 Stash 上报的路径前缀 / 找出真实的多文件场景
    02-recon-container.sh      容器内是否另有手改（docker diff + CJK 全文扫描）
    03-recon-content-diff.sh   与镜像逐文件比 md5，排除「只是 mtime 变了」的假阳性
    10-install-code.sh         备份 + 解包 + sha256 逐文件校验 + 同文件系统 rename 换树
    11-apply-config.sh         写 compose（带 docker compose config -q 校验）
    13-fix-conf-scope.sh       修正配置键作用域，并用应用自身 loader 自证
    12-restart-verify.sh       重建容器 + 21 项端到端断言
    16-final-state.sh          汇总部署后状态
    17-probe-ui.sh             核对服务端实际吐出的界面内容
```

**校验强度**：本地打包算 sha256 清单 → NAS 上 `sha256sum -c` 逐文件比对，**58/58 通过**才换树。换树前还先与镜像内文件集合比对过（镜像 53 个 `.py` 一个不缺，仅多出新增的 `util/local_media.py`），避免整体覆盖时丢掉上游模块。

### 5.4 回滚

```bash
# 备份位置（每次部署自动创建）
/vol2/1000/HSX/docker/stash-jellyfin-proxy/_backup/<时间戳>/

# 回滚 = 换回旧代码树 + 重建
mv .../_backup/<ts>/app.prev .../app
cd /vol2/1000/HSX/docker/stash-jellyfin-proxy && docker compose up -d
```

秒级完成，**不需要重建镜像**。本次部署共留下 7 个时间点备份与 1 个 `docker-compose.yml.bak-*`。

### 5.5 运维事实

- **重启（含重建）约需 95–100 秒**才对外服务：启动阶段要先对 2300+ 场景向 Stash 解析媒体库计数，之后才 listen。期间插件客户端会连不上，属正常现象，**不是启动失败**。
- 宿主 `8096` 与 Jellyfin 默认端口冲突，两者不能同时监听。
- 打包环境是 Python 3.13、镜像是 3.11 —— 已用 `ast.parse(..., feature_version=(3,11))` 全包扫过语法兼容性。
- 根目录的 `views.py` / `views.py.bak` 已**不再被 compose 引用**（内容已并入 `app/endpoints/views.py`），留作历史备份。

---

## 6. 踩坑记录

这四个坑都只在真机暴露，且**前两个的症状都具有欺骗性**。

### 6.1 `docker diff` 会把整个目录标成 changed（侦察方法论）

`docker diff` 显示约 40 个文件被改动，包括 `stash/client.py`，一度以为改动面很大。改用**与镜像逐文件比 md5** 后确认：真正的**内容差异只有 1 个文件**（`views.py`），`client.py` 里的中文只是上游自带的注释，哈希与镜像一致。

**教训**：判断「容器内有哪些手改」不要信 `docker diff`（它会把运行时写入的日志、`__pycache__`、chown 后的 mtime 全算进去），要比对**内容哈希**。

### 6.2 配置文件有 INI 节作用域 → 第一次部署「假成功」

`.conf` 末尾是 `[player.*]` 节。把新键**追加到文件末尾**，它们就落进了 `[player.default]` 节：应用读不到、静默用默认值。

症状极具欺骗性 —— **服务正常、日志无错、`/api/config` 一切正常**，但多文件场景照旧只播一个文件。

处置：插到第一个 `[` 之前，并用**应用自身的 `load_config()`** 证明键在 global 作用域、`player.*` 零泄漏：

```text
global  UI_LANGUAGE        = 'auto'
global  MULTI_FILE_SCENES  = 'true'
global  LIBRARY_PATH_MAP   = '/data:/library'
leaked into sections: none
sections present   : ['player.default', 'player.infuse', 'player.roku', 'player.senplayer', 'player.swiftfin']
```

### 6.3 整体覆盖目录会静默吃掉已有的手工改动

原部署在 compose 里**单独挂了一个手改的 `views.py`**（就是客户端侧媒体库名汉化）。如果把整个包目录覆盖上去，这个单文件挂载仍在，但会被目录内容遮蔽 → 汉化**静默回退**，不报错。

处置：把这份手改与新改动**合并**进 `app/endpoints/views.py`，统一由自研树提供，然后从 compose 里移除那个单文件挂载。

### 6.4 HTTP 头是 latin-1，文件名含 CJK 会炸成 500 空体

非主文件改走磁盘直读后，只要文件名含中日文字符，客户端就收到 **500 空响应**，而**日志里连 traceback 都没有**（只有一行 `'latin-1' codec can't encode...`）。

原因：`Content-Disposition` 里直接写了 basename，而 HTTP 头必须是 latin-1（RFC 7230），于是 ASGI 在发响应阶段抛 `UnicodeEncodeError`。误导点在于**路径映射和读盘其实都已经成功了** —— 日志里紧邻的一行正是 `Serving file 751 from disk`。

处置：按 RFC 6266 同时输出两种形式 —— ASCII 回退名（`filename=`）+ 百分号编码的 UTF-8 名（`filename*=UTF-8''`），客户端读后者。

已加 `tests/unit/test_local_media.py`（22 项)钉死该行为，同时补上了此前**完全无覆盖**的路径映射逻辑（前缀按路径分量匹配、多组映射、Windows 风格目标路径、非 ASCII 路径）。

---

## 7. 变更清单

### 7.1 涉及文件（相对上游 `ef3d017`，24 文件 / +2888 −127）

**A. 协议层 —— 多文件场景（替换 upstream 行为）**

| 文件 | 作用 |
|---|---|
| `util/local_media.py`（新增） | 路径映射 + `FileResponse` 直读磁盘（含 RFC 6266 头部编码） |
| `util/ids.py` | 新增 `get_file_id()`；`get_numeric_id()` 剥离 `-f<fileId>` 后缀 |
| `mapping/scene.py` | 新增 `_res_label()` / `version_display_name()` / `build_media_source()` |
| `endpoints/playback.py` | `/PlaybackInfo` 为每个文件下发独立 `MediaSource` |
| `endpoints/stream.py` | 非主文件改走磁盘直读，取不到则回退 Stash 流 |
| `endpoints/{items,views,playlists,user_actions}.py` | GraphQL 字段集补 `files { id ... }`（`views.py` 同时含客户端库名汉化） |
| `runtime.py` / `config/bootstrap.py` | 两个运行时开关的默认值/配置/环境变量/日志/publish 接线 |

**B. i18n 层 —— 配置界面汉化（纯增量）**

| 文件 | 作用 |
|---|---|
| `ui/static/i18n.js`（新增，786 行） | 翻译引擎 + zh-CN 目录（387 条） |
| `ui/static/app.js` | ~70 处调用点接入 `t()`；监听 `sjp:langchange` 重渲染 |
| `ui/templates/index.html` | `<html lang>`、引导脚本、侧边栏切换器、Interface 卡、Multi-File Scenes 卡 |
| `ui/static/app.css` | 分段切换器样式 |
| `ui/api.py` | `{{UI_LANG}}`/`{{HTML_LANG}}` 注入；`_P5B_KEYS` 注册三个键；status 暴露 `uiLanguage` |
| `stash_jellyfin_proxy.conf` | 三段带注释的配置说明 |

**C. 校验工具（新增）**

| 文件 | 作用 |
|---|---|
| `dev-tools/i18n_extract.py` | 用 `HTMLParser` 抽取界面串（不解析 JS，理由见 §3.6） |
| `dev-tools/i18n_audit.py` | A–E 五项静态 CI 门禁 |
| `dev-tools/i18n_smoke.js` | Node + DOM 桩无头驱动引擎，42 项断言 |
| `dev-tools/i18n_server_test.py` | Starlette `TestClient` 打真实 HTTP，41 项断言 |
| `tests/unit/test_local_media.py` | 路径映射 + 头部编码回归测试（22 项） |

### 7.2 提交（分支 `local/self-maintained`）

```text
d346fa6  test: cover local_media path mapping and Content-Disposition encoding
efce417  fix(multi-file): non-ASCII filenames broke the stream response
7f94f4e  feat(ui): expose the multi-file scene knobs in the config UI
dc0989d  feat: own multi-file scene implementation + zh-CN config UI
ef3d017  ← main 仍停在这里（upstream 干净态）
```

`main` 保持上游原样，日后同步上游只需 `git fetch && git rebase origin/main`，冲突集中在自己补丁里。

### 7.3 复跑验证

```bash
P=C:/Users/qaz22/.workbuddy/binaries/python/versions/3.13.12/python.exe
N=C:/Users/qaz22/.workbuddy/binaries/node/versions/22.22.2-3/node.exe
V=C:/Users/qaz22/.workbuddy/binaries/python/envs/default/Scripts/python.exe

$P -c "import compileall;compileall.compile_dir('stash_jellyfin_proxy',quiet=1,force=True)"   # 语法
$N --check stash_jellyfin_proxy/ui/static/i18n.js
$N --check stash_jellyfin_proxy/ui/static/app.js
$P dev-tools/i18n_audit.py            # 覆盖度/接线门禁，退出码 0=通过
$N dev-tools/i18n_smoke.js            # 引擎无头行为
$V dev-tools/i18n_server_test.py      # 服务端注入链路（真实 HTTP）
$V -m pytest tests/unit -q            # 单元测试
```

当前实测（2026-09-13）：

```text
Catalog entries            : 387
Strings in index.html      : 327
Literal keys in t() calls  : 91
Keys via lookup tables     : 8
Unreachable catalog entries: 0
Wiring problems            : 0
ALL CHECKS PASSED

i18n_smoke.js            42 passed, 0 failed
i18n_server_test.py      41 passed, 0 failed
tests/unit              155 passed
```

> `tests/characterization/test_replay.py` 有 11 项失败，与本次改动无关 —— 该测试硬编码指向开发机 `http://192.168.0.200:18096`，本环境不可达，属既有环境性失败。

---

## 8. 边界与未验证项

**已知边界**

- **同形异义**：同一英文串全局只有一种译法。需区分时用 `data-i18n-skip-attrs`（属性）或 `data-i18n-skip`（子树）显式退出。
- **`innerHTML` 区域**：新写的动态渲染若忘了调 `t()`，界面会混入英文。静态门禁能通过「`t()` 键缺失」或「目录死键」暴露，但取决于写法。
- **语言仅 `en` / `zh`**：新增语种需在 `i18n.js` 加一份目录，并在 `SUPPORTED` / `PRECISIONS` / 切换器里各加一项，检查 E 会强制三者对齐。
- **多文件场景默认关闭**：需按 §2.4 显式开启，否则行为与修复前一致。

**尚未验证的两件事（需在真环境确认）**

1. **真实浏览器里的界面视觉** —— 已验的是 HTML/JSON 层（含服务端实际吐出的字节：切换器、`i18n.js`、新卡片、0 个残留占位符），未验像素级效果，尤其是 `innerHTML` 动态区域的文案。
2. **真实播放器里的版本选择器** —— HTTP 层已证明 `PlaybackInfo` 下发 2 个源、非主文件可 `206` 分片读取；但 Infuse / Swiftfin 客户端 UI 里是否如期出现版本选择，需实际点一次。

**上游同步风险**

已改动 `endpoints/*` 与 `mapping/scene.py`，这些是上游迭代最频繁的文件。每次同步上游都会在这几处产生冲突，而 GraphQL 字段串是**单行超长字符串**，冲突很难自动合并。建议把「`files { id ... }`」做成独立小补丁记录在案，冲突时直接重打，而不是试图手工合并整行。
