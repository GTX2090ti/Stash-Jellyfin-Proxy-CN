# Stash-Jellyfin Proxy 刮削能力更新说明

**主题：Jellyfin「识别 / 刮削」桥接到 Stash 刮削引擎**
**周期：2026-09-13 ~ 2026-09-14**
**状态：已上线（NAS 192.168.2.210），全链路实测通过**
**版本基线：v7.3.10（本地分支 `local/self-maintained`）**

---

## 一、概述

本次更新为代理新增一项**能力**并完成**四轮缺陷修复**，目标是打通手机端 App 的一条完整链路：

> 手机 App 点「识别 / 刮削」 → 代理（模拟 Jellyfin API） → **调用 Stash 已安装的刮削器** → 把结果写回 Stash 媒体库

需要特别澄清一个曾导致方向偏差的点：**这是调用 Stash 的刮削引擎（`scrapeSingleScene` / `scrapeURL`），不是拿代理去检索 Stash 库里的现有元数据。** 数据流向是「第三方站点 → Stash」，不是「Stash → 客户端」。

| 阶段 | 内容 | 结果 |
|---|---|---|
| 新增能力 | Jellyfin Identify / Refresh metadata → Stash 刮削器桥接 | 7 条协议路由落地，205 项单测通过 |
| 修复一 | App 报 `Method Not Allowed`（405） | 路由补齐，214 项单测通过 |
| 修复二 | 「削刮不正常」——身份刮削能力静默失效 | 命中 0 → 1 条，耗时 25.6s → 2.0s |
| 修复三 | `fantiajp 削刮失败，4064942` | 假成功被拦截，3 类入口全部命中 |
| 修复四 | `1006291` 刮出「青橙」而非商品 `buena-320`（Fantia 双命名空间） | 新增 `FantiaProducts` 刮削器 + 多形态路由 + 相关性排序；正确对象升首位、错误对象降级保留 |
| 收口 | 全量单测 + 部署验证 + 现场只读复检 + 落库终态核对 | **261 项单测通过 / 27 项部署断言全过 / scene-118 写入 `code=buena-320`** |

---

## 二、新增能力：Identify / 刮削桥接

### 2.1 交付物

| 文件 | 变更性质 | 说明 |
|---|---|---|
| `stash_jellyfin_proxy/endpoints/metadata.py` | 新增（约 1010 行） | 桥接核心：协议映射、扇出调度、结果停放、写回 Stash |
| `stash_jellyfin_proxy/app.py` | 修改（+30 行） | 注册 10 条路由（分两批加入） |
| `stash_jellyfin_proxy/runtime.py` | 修改（+34 行） | 新增 8 个配置开关 |
| `stash_jellyfin_proxy/ui/api.py` | 修改（+15 行） | 8 个新键注册进 `_P5B_KEYS`，Web UI 可读写 |
| `stash_jellyfin_proxy/stash/client.py` | 修改（+11 行） | 4xx 响应体写入日志（原先静默） |
| `stash_jellyfin_proxy.conf` | 修改（+58 行） | 新增「Metadata scraping / Identify」配置段 |
| `tests/unit/test_metadata_scrape.py` | 新增 | 刮削逻辑单测 |
| `tests/unit/test_metadata_endpoints.py` | 新增（18 项） | 端到端 handler 测试（FakeStash 驱动） |
| `dev-tools/build_deploy_bundle.py` | 新增 | 构建部署包（含归档前缀修正） |
| `dev-tools/deploy_scraping.py` | 新增 | 部署驱动：upload → install → verify |
| `dev-tools/nas-scripts/40-install-scraping.sh` | 新增 | NAS 侧增量安装 |
| `dev-tools/nas-scripts/41-verify-scraping.sh` | 新增 | NAS 侧验证（27 项断言） |

### 2.2 协议映射

| Jellyfin 端点 | 作用 | 映射到 Stash 侧 |
|---|---|---|
| `GET /Items/{id}/MetadataEditor` | Identify 对话框的 provider 列表 | `listScrapers(types: [SCENE, PERFORMER])` |
| `POST /Items/RemoteSearch/{itemType}` | jellyfin-web 搜索（RemoteSearchQuery） | `scrapeSingleScene` / `scrapeSinglePerformer` |
| `GET|POST /Items/{id}/RemoteSearch/{provider}` | 按 provider 定向搜索 | 同上 |
| `POST /Items/RemoteSearch/Apply/{id}` | 应用选中项 | `sceneUpdate` / `performerUpdate` |
| `POST|GET /Items/{id}/Refresh`、`/RefreshMetadata` | 自动匹配并应用 | 指纹 pass → 文本 pass → update |
| `POST|GET /Library/Refresh` | 触发库刷新 | `metadataScan` |
| `POST /Items/{id}` | 客户端回写元数据 | 映射到 Stash 自有字段 |
| `POST|DELETE /Items/{id}/Images/{imageType}` | 上传 / 删除封面 | 图片写回 |

### 2.3 关键设计决策

1. **provider 列表按「支持文本搜索」过滤**。只有支持 NAME 搜索的刮削器才会出现在 Identify 搜索框里——FRAGMENT/URL-only 刮削器列出来也无法查询。启动日志会打印实际可用清单：
   `Stash scrapers available: StashDB[scene/performer], ...`；若为 `none`，说明 Stash 尚未安装可用刮削器。

2. **搜索结果用 token 停放**（`ProviderIds["Stash"]`，TTL 默认 1800s）。客户端流程是「先取列表、再回传选中项」，停放完整 payload 才能让 Apply 精确命中，而不是重新猜一次。token 过期则降级为「按客户端回显的 provider + name 重刮」，再降级到 `_auto_match`。

3. **自动匹配两趟**：① 指纹/记录匹配（交由 Stash 自行选择）② 按标题文本搜索，第一个有结果的 provider 胜出。

4. **关系与图片可分别关闭**（`SCRAPE_APPLY_RELATIONSHIPS` / `SCRAPE_APPLY_IMAGES`）。原因：这两项会**替换**已有数据（studio / performers / tags、封面图），find-or-create 会按名字新建行，故设上限 `_MAX_RELATIONSHIPS = 30`，防止单次点击打出上百次 Stash 往返。

5. **studio / group / tag 故意返回空列表**。Stash 的 `ScrapeContentType` 枚举中没有 STUDIO / GROUP / TAG 成员，列出匹配项也永远取不到内容。返回空列表而不是一个「撒谎的列表」，并已写成断言钉死（studio → `ExternalIdUrls` 计数 == 0）。

6. **配置键必须同时注册进 `_P5B_KEYS`**。该表同时驱动 `GET /api/config` 的返回值与配置写入的作用域逻辑。功能即使按默认值生效，未注册也会导致：线上 `/api/config` 返回 `None`、Web UI 无法修改并持久化——**只有拿 `/api/config` 核对才会发现**。本次已补注册并加单测钉死（断键在注册表内、断 `_p5b_get_value()` 非 None、断 `live` 标记为 True）。

---

## 三、修复迭代

### 修复一：App 报 `Method Not Allowed`（405）

**根因**：`/Items/{item_id}/RefreshMetadata` 与 `/Items/{item_id}/Refresh` 未注册。请求未被接收后，落入下游 **GET-only 的 catch-all** `/Items/{item_id}`，因方法不匹配返回 405。

**修复**：注册全部 metadata 路由，且刷新类端点同时接受 POST 与 GET。源码中的注释直接记录了这一约束：

```python
# The classic refresh shape most clients still POST — must exist or the
# request falls through to the GET-only catch-all and 405s.
Route("/Items/{item_id}/RefreshMetadata", endpoint_refresh_metadata, methods=["POST", "GET"]),
```

同时把「`RemoteSearch` / `MetadataEditor` 这类字面量段」的路由注册**前置于** catch-all，避免被当作 `item_id` 吞掉。

**验证**：单测 214 passed；部署日志 `logs/deploy-405-run.log` → `INSTALL_OK` / `PASS=27 FAIL=0` / `VERIFY_OK` / `DONE — exit 0`。

---

### 修复二：「削刮不正常」

**用户澄清（纠正了此前的错误框定）**：
> 「是手机 app 削刮调用 stash 的削削功能，并不是搜索 stash 上的数据」

**根因（四条叠加，均有原始日志）**：

| # | 根因 | 现象 |
|---|---|---|
| 1 | 串行 fan-out + 单点慢 | 12 个 NAME 刮削器中**只有 AdultTime 卡**（每次吃满 20s），其余 0.8–1.7s（JavDB 仅 7ms）。串行下 25s 预算在到达 JavBus / JavDB **之前就耗尽** → `remote search scene-13 ... -> 0 result(s)`，`Slow request (25623ms)` |
| 2 | `source: {}` 是非法调用 | Stash 报 `input error: scraper_id or stash_box_index must be set`——所谓「指纹匹配 pass」**从未生效**，一直是瞬间失败的死代码 |
| 3 | URL-only 刮削器入参认知错误 | 一度以为正确入参是 `scene_id`，实际当时结论不完整（见修复三） |
| 4 | 刮削器 id **区分大小写** | 必须 `FantiaJp`；小写 `fantiajp` → `scraper not found` |

**★ 最隐蔽的坑：常量少一个字段 → 能力静默失效**

`_load_providers` 已在解析 `spec["urls"]`，但拼接查询的常量 `_LIST_SCRAPERS` 里**没有 `urls`**。失效链路：

```
常量缺 urls 字段 → patterns 全空 → URL 前缀匹配恒假
  → 身份刮削一条候选都不产生 → Identify 静默退化成名称搜索
```

**无异常、无告警、日志干净**，仅表现为「结果变少」。定位手法：把疑似常量原样粘进独立查询发给 Stash（返回正常）→ 证明错在本方解析；再用 `docker cp` 把探针脚本拷进容器、手工喂 `runtime.STASH_URL / STASH_API_KEY / STASH_VERIFY_TLS`（注意是**模块属性**，不是 `runtime.config`）直接调内部函数打印中间值。已加断言测试 `test_list_scrapers_query_fetches_url_patterns` 锁死。

**修复要点**：

- 新增 `_normalize_url` / `_patterns_match`（归一化：去 scheme、去 `www.`、去尾斜杠后做前缀匹配）
- 新增 `_fetch_stored_urls` / `_identity_attempts`：读条目已存 URL，**只挑声明覆盖该 URL 前缀的刮削器**，用 `{scene_id}` 直连；`_search`（Identify 路径）与 `_auto_match`（Refresh 路径）都接上
- 新增 `_merge_attempts`（按 `(scraper_id, input)` 去重）
- 新增 `_fan_out_within`：`asyncio.Semaphore(6)` 并发扇出 + `asyncio.wait(FIRST_COMPLETED)`，**有命中即取消其余**
- `_scrape`：`scraper_id` 为空时**直接跳过并告警**，不再发出非法 `source:{}`

**验证**：单测 233 passed；端到端 `logs/probe-identify-live2.log`：

| 用例 | 修复前 | 修复后 |
|---|---|---|
| scene-13 日文标题 Identify | 0 条 | **1 条**（日志：`stored URL 'https://fantia.jp/posts/3072923' matches 1 scraper(s) (FantiaJp)` → `FantiaJp answered first`） |
| scene-13 纯数字 | 1 条 / 25.6s | 1 条 / **2.0s** |
| scene-62 文件名标题 | 0 条 | 0 条（全网确无匹配，非缺陷） |

---

### 修复三：`fantiajp 削刮失败，4064942`

**用户操作记录**：对 Fantia 作品 `4064942` 曾两次输入纯数字（11:57:18 / 11:59:41），并两次粘贴完整 URL（`https://fantia.jp/posts/4064942`），后者扇出 24s、0 结果。

**根因（三条叠加，全部实测确认）**：

**根因 1：假成功「fragment 回声」**

`{scene_id: "807"}` 而该条目**没有已存 URL** 时，Stash 走 fragment 刮削，URL-only 刮削器把文件名原样回吐：

```
n=1, title='5ff33487_Buncos_kfk.mp4', urls=[], 846ms
```

**无 error、被计为 1 条命中**，且 846ms 就在并发扇出中抢到首位，把正确的那次（2.8s）取消掉了。这正是用户看到的「失败」——不是没结果，是**返回了垃圾且当成成功**。

**根因 2：合成 URL 的形状选错**

`scene_input: {urls: [...]}` 对 FantiaJp / GetchuDL **恒失败**（`error while fragment scraping ... operation not supported`）。试过 `url`（单数）、补 `title` / `code` 均无效。**可用入口是 `scrapeURL(url, ty: SCENE)`**（2.8–3.4s 返回完整元数据）。

**根因 3：粘贴 URL 走了名称搜索**

URL 被当关键词发给 12 个刮削器，产生 `javdb.com/search?q=https%3A%2F%2Ffantia.jp%2F...` 这类查询，全 404 → 24s / 0 结果。

**附带坑：`scrapeURL` 返回联合类型**

字段必须放在 `... on ScrapedScene {}` 内。挂错位置时两种表现：

| 路径 | 表现 |
|---|---|
| 宿主直连 Stash | `422`，**带清晰报错** |
| 容器内走代理链路 | `400`，**无 body** |

后者极易误判成网络故障（本次为此耗时约半小时）。已加断言测试锁死。

**修复要点**：

- 新增 `_URL_SCRAPE_QUERY`（`scrapeURL`，字段置于 `... on ScrapedScene {}` 内）+ `_scrape_by_url()`；`_scrape()` 识别 `_URL_MARKER = "__url__"` 走 URL 入口。**字段列表与 `_SCRAPE_QUERY` 共享常量**（`_SCENE_FIELDS` / `_PERFORMER_FIELDS`），避免两处漂移
- `_scrape_attempts()` 改三分支：**URL term → 数字 term（模板合成 URL）→ 名称搜索**；**删除对 URL-only 刮削器的盲发 `{scene_id}`**（假成功来源）
- `_fan_out_within()` 胜负判定从「谁先到谁赢」改为**「只在还有更高优先级的『定向』尝试在飞时才继续等」**（`_is_targeted` = 输入不含 `query`）；新增 `_is_fragment_echo()` / `_is_empty_payload()` / `_is_unusable()` 兜底过滤
- `_resolve_provider()` 改为在 `_providers_for` + `_providers_any` 两个池中查找——**修掉了修复二中登记的「遗留」**：URL-only 刮削器默认不在 NAME 池，客户端回传 `SearchProviderName=FantiaJp` 时曾答 `unknown provider` 并返回空
- 新增 `_looks_like_url()`（`movie.mp4` / `ABP-123` / `5ff33487_x.mp4` 均不得误判为链接）
- `stash/client.py`：4xx 时把 `response.text` 打进日志（本次 400 无 body，直接导致诊断延迟）

**验证**：单测 255 passed；端到端 `logs/probe-identify-4064942-final2.log`：

| 用例 | 结果 |
|---|---|
| scene-807 + `4064942` | **1 条**，标题为正确的 Fantia 元数据，3489ms |
| scene-807 + 粘贴 URL | **1 条**，同上，3240ms |
| scene-807 + provider=FantiaJp | **1 条**，2989ms（修复前 0 条 / 202ms） |
| scene-807 + `99999999` | 10 条 DMM（名称搜索回落，不再是空标题） |
| scene-807 + 空词 | 0 条（不再有文件名垃圾） |
| scene-13 数字 / URL 回归 | 各 1 条 |

决策日志佐证优先级规则生效：`holding GetchuDL while a higher-priority attempt is still running` → `FantiaJp answered first`。**GetchuDL 确实先返回了错数据，被规则挡下。**

---

### 修复四：`1006291` 刮出「青橙」而非 `buena-320`（Fantia 双命名空间）

**用户报告**：数字 `1006291` 报错；随后澄清「使用的是 products」，并给出 `https://fantia.jp/products/1006291`。

**根因：`/posts/<id>` 与 `/products/<id>` 是两个互不相关的对象，却共用同一个数字**

| URL | 该数字对应的对象 | 状态码 |
|---|---|---|
| `fantia.jp/posts/1006291` | 同人投稿「青橙」（2021-11-28） | 200 |
| `fantia.jp/products/1006291` | 成人商品，code `buena-320` | 200 |

官方 `FantiaJp` 只声明 `fantia.jp/posts/`，代理的数字模板也只合成 `/posts/{id}` —— 于是 `1006291` 稳定返回**语法正确、对象错误**的「青橙」，而待刮条目（scene-118）的文件是 `buena-320s.mp4`。**这不是错误，是错配**：没有任何异常可捕获，唯一可用的信号是「答案里有没有提到这个条目自己提到过的词」。

**修复一：新增纯 XPath 刮削器 `FantiaProducts`**（无脚本依赖，Stash 0.31.1 实测可用）

```yaml
name: FantiaProducts
sceneByURL:
  - action: scrapeXPath
    url:
      - fantia.jp/products/
    scraper: sceneScraper
xPathScrapers:
  sceneScraper:
    common:
      $ld: //script[contains(text(),'"@type":"Product"')]/text()
      $og: //meta[@property="og:title"]/@content
    scene:
      Title:
        selector: $ld
        postProcess:
          - replace:
              - regex: '^.*?"name":"(.*?)","description".*$'
                with: $1
      Code:
        selector: $og
        postProcess:
          - replace: [{regex: '^.*【([^】]+)】.*$', with: $1}]
          - replace: [{regex: '^([A-Za-z]+)([0-9]+)$', with: $1-$2}]
      URL: //meta[@property="og:url"]/@content
      Image:
        selector: //meta[@property="og:image"]/@content
        postProcess:
          - replace: [{regex: blurred_ogp_, with: ""}]
      Studio:
        Name:
          selector: //meta[@property="og:site_name"]/@content
          postProcess:
            - replace: [{regex: ".*", with: "Fantia.jp"}]
      Details:
        selector: $ld
        postProcess:
          - replace: [{regex: '^.*?"description":"(.*?)","image".*$', with: $1}]
          - replace: [{regex: '\\n', with: "\n"}]
      Performers:
        Name:
          selector: $og
          postProcess:
            - replace: [{regex: '^.*\(([^)]+)\)の商品.*$', with: $1}]
      Tags:
        Name:
          selector: //script[contains(text(),'"content_viewed"')]/text()
          postProcess:
            - replace: [{regex: '^.*?"tag":\[(.*?)\].*$', with: $1}]
            - replace: [{regex: '"', with: ""}]
        split: ","
```

**写这类 XPath 刮削器的两个坑（都实测踩过）**：

| 坑 | 报错 / 表现 | 正确写法 |
|---|---|---|
| `xPathScrapers.<x>.scene.Performers:` 写成 YAML **列表**（`- Name: ...`） | `yaml: unmarshal errors: line 4: cannot unmarshal !!seq into map[string]interface {}`，**整份 yml 不加载** | 直接写成映射：`Performers:` → `Name:` → `selector:` |
| 商品图被站点打码 | 刮到的封面是模糊版 | `og:image` 的 URL 带 `blurred_ogp_` 前缀，`postProcess` 里 strip 掉即得原图 |

**数据来源说明**：`/products/` 页面**无需登录**，含 JSON-LD `Product` 块（标题/描述全在其中）与 `content_viewed` 里的 `tag[]`，因此纯 XPath 足够，不必写脚本。

**修复二：数字模板支持「一个数字多形态」（`|` 分隔）**

```
SCRAPE_NUMERIC_URL_TEMPLATES = fantiajp=https://fantia.jp/posts/{id}|https://fantia.jp/products/{id},getchudl=https://dl.getchu.com/i/item{id}
```

`_template_urls_for()` 返回 `List[str]`（原为单个 `str`），`_numeric_candidates` 的 `url` 字段改名 `urls`，`_scrape_attempts` 对每个形态各生成一次尝试。新增刮削器后无需改代码——`FantiaProducts` 由 `scrapeURL` 按 URL 前缀路由。

**修复三：同 id 多命中的相关性排序 + 同源去重**

- 新增 `_payload_relevance(payload, seed_text)`：条目自有词（token 集合）与刮削结果（title / name / code / details / urls / performers / tags / studio）的**交集大小**。**只用于排序，不用于丢弃**——这正是用户选择的「保留但降级排序」。
- 新增 `_fetch_rank_text()`，**与 `_fetch_seed_text()` 分开**：后者是「搜索关键词」（必须短而干净），前者是「条目自己的全部词」（title **加** 每个文件名）。此分离是本轮的核心修正：

  > scene-118 的标题就是裸数字 `1006291`，而两个形态的 URL 里都含这个数字 → 只用 title 做种子时两者**同分**，错的「青橙」按尝试序胜出。真正能与商品 `buena-320` 对上的是文件名 `buena-320s.mp4`。

- 排序后**按结果自带 URL 去重**：「已存 URL」与「同形态合成 URL」打开的是同一页面，同一对象只列一次。

**验证**：单测 **261 passed**（新增 `TestRankText` 3 例 + 同源去重 1 例）。端到端证据：

| 验证项 | 结果 |
|---|---|
| 客户端聚合搜索 scene-118 + `1006291` | **2 条**：第 1 条 `…【buena320】…`（正确商品），第 2 条「青橙」（保留但降级） |
| 日志 | `scene: FantiaJp answered (1 result(s); 15 scraper(s) fanned out; kept 1 more targeted hit(s) ranked below)` |
| `FantiaProducts` 注册 | `listScrapers` 中 `scene=URL`，先前那条加载报错已消失 |
| **落库终态** | scene-118 → `code=buena-320`、`urls=["https://fantia.jp/products/1006291"]`、6 个 tag、performer `たかまり↑おぢさん` |

**排查过程的一个环境坑**：探针脚本用 `os.environ.get("TERM")` 读目标数字，而 Windows/Git-Bash 环境里 `TERM=dumb` —— 结果探针实际查的是字符串 `dumb`，输出「log lines mentioning dumb / (none)」。**跑这类以环境变量传参的探针，必须显式赋值**（`$env:TERM='1006291'`），不要依赖默认值。

---

## 四、可复用的工程结论

以下六条是本次排障中代价最高的经验，已分别沉淀到技能文档 `stash-custom-scraper`（§19 / §20）与 `fnos-docker-bindmount-deploy`（§4.10）：

1. **「返回 0」与「返回垃圾但成功」是两类故障**，后者更隐蔽。任何「假成功」都要有显式判别函数（`_is_fragment_echo` / `_is_empty_payload`），并在扇出胜负判定中排除。
2. **GraphQL 查询常量必须与解析代码同步演进**。少一个字段不会报错，只会让下游能力静默归零。凡「解析某字段」就必须有断言测试证明「查询里真的取了这个字段」。
3. **同一查询形状经不同网络路径返回不同状态码**（宿主 422 有报错 / 容器 400 无 body）。因此**新查询形状必须先在两条路径上分别验证，再部署**。
4. **并发扇出不能用「先到者胜」，要用「优先级 + 定向性」判定**。否则快而错的结果会稳定压过慢而对的结果。
5. **同一命名空间里的一个 id 可能对应多个互不相关的对象**（Fantia `/posts/` vs `/products/`，两者都 200）。这类「错配」没有异常可捕获，**唯一可用的判据是「答案是否提到条目自己提到过的词」**；而排序种子必须取「条目全部自有词」（title + 文件名），只取 title 会在 title 本身就是那个数字时完全失效。
6. **排序与搜索对文本的需求是相反的**：搜索关键词要短而干净，排序种子要长而全。把两者塞进同一个变量（如 `_auto_match` 早期的 `seed_text`）必然在某一侧出错——**应当拆成 `_fetch_seed_text` / `_fetch_rank_text` 两个函数**。

---

## 五、配置参考

配置文件：`/config/stash_jellyfin_proxy.conf`（NAS 上为
`/vol2/1000/HSX/docker/stash-jellyfin-proxy/stash_jellyfin_proxy.conf`）
**以下键只在全局作用域设置（第一个 `[section]` 之前），否则不生效。**

| 配置键 | 默认值 | 说明 |
|---|---|---|
| `ENABLE_SCRAPING` | `true` | 总开关 |
| `SCRAPE_APPLY_RELATIONSHIPS` | `true` | 允许刮削结果写入 studio / performers / tags（不存在时按名新建行） |
| `SCRAPE_APPLY_IMAGES` | `true` | 允许刮削结果替换封面图 |
| `SCRAPE_RESULT_TTL_SECONDS` | `1800` | 已取回搜索结果对后续 Apply 调用的有效时长 |
| `SCRAPE_NUMERIC_SCRAPERS` | `fantiajp,GetchuDL` | 搜索词为**纯数字**时优先尝试的刮削器（顺序有意义） |
| `SCRAPE_NUMERIC_URL_TEMPLATES` | `fantiajp=https://fantia.jp/posts/{id}\|https://fantia.jp/products/{id},getchudl=https://dl.getchu.com/i/item{id}` | 数字如何转成 URL（上述刮削器不支持按名查询，只能按 URL / fragment）。**同一个刮削器可用 `\|` 列出多个形态**：Fantia 的 `/posts/<id>` 与 `/products/<id>` 是共用同一数字的两个不同对象，两个都要试，再由相关性排序决定谁在前 |
| `SCRAPE_ATTEMPT_TIMEOUT_SECONDS` | `20` | 单个刮削器单次往返上限 |
| `SCRAPE_SEARCH_BUDGET_SECONDS` | `25` | 单次搜索总预算；耗尽后未尝试的 provider 被跳过（部分结果优于无结果） |

> 若启动日志中 `Stash scrapers available:` 显示为 `none`，需先在 Stash → 设置 → 元数据提供器 中安装刮削器。

---

## 六、部署形态与验证

### 6.1 部署形态（增量升级，镜像不动）

| 项目 | 值 |
|---|---|
| 主机 / 路径 | `192.168.2.210:/vol2/1000/HSX/docker/stash-jellyfin-proxy` |
| 目录挂载 | `app/` → `/app/stash_jellyfin_proxy`（**rw**，entrypoint 需 chown）；自身 → `/config`；`/vol1/1000/HS1` → `/library:ro` |
| 关键环境 | `LIBRARY_PATH_MAP = /data:/library`（左侧为 **Stash 容器内**路径）；`MULTI_FILE_SCENES = True` |
| 升级方式 | 仅替换 `app/` 目录树 |
| 回滚方式 | 换回 `_backup/<ts>/app.prev` + `docker compose up -d` |
| 部署命令 | `python dev-tools/deploy_scraping.py`（`--ask` 可交互输入密码） |

**归档布局注意**：`build_deploy_bundle.py` 产出带 `app/` 前缀，故解归档必须 `--strip-components=1`，否则 `sha256sum -c` 全量报 `No such file`。旧的 `10-install-code.sh` 按「归档根即包名」编写，**已不可直接复用**，故新增 40 / 41 脚本。新脚本额外带两条守卫：

1. 线上每个文件必须在新包中存在，否则 `DROPPED-IN-SWAP` 直接 fatal（整体替换 bind-mount 目录会静默删除新包没有的文件）
2. 解归档后断言文件数 == 清单行数

### 6.2 验证证据

| 验证项 | 证据 / 结果 |
|---|---|
| 单元测试 | **261 passed**（205 → 214 → 233 → 255 → 258 → 261），零回归 |
| 部署（最终轮） | 13:50 与 13:39 两轮均为 `INSTALL_OK` / `PASS=27 FAIL=0` / `VERIFY_OK` / `DONE`；`metadata.py` 安装侧 sha256 = `2519330f…4d95c`，NAS 上实测一致 |
| 捆绑包自检 | `staged files : 59`，与 `40-install-scraping.sh` 的 `EXPECT_FILES=59` 相符；容器 Python 3.11 下 `55 .py` 全部通过 `ast.parse` |
| Fantia 双形态路由 | `SCRAPE_NUMERIC_URL_TEMPLATES` 有效值含 `…/posts/{id}\|…/products/{id}`；`listScrapers` 中 `FantiaProducts` = `scene=URL` |
| 产物抽查 | `tar -xzOf deploy/app.tar.gz ... \| grep -c 'on ScrapedScene'` = 1 |
| NAS 侧断言 | `logs/nas-41-verify-scraping.log` → `PASS=27 FAIL=0 VERIFY_OK`，容器经历 `Recreate → Recreated → Starting → Started`（证明新代码**从磁盘加载**，非旧进程） |
| 现场只读复检 | `logs/live-check-3.log` → `ALL LIVE CHECKS PASS`，0 traceback，`metadata.py` 哈希容器内 == 宿主机 |
| 服务可用性 | `/System/Info/Public` = 200；`8097/api/status` → 200 `running:true version:v7.3.10` |
| Identify 提供者数 | scene-13 → 12 个（AdultTime, DMM, DugaJP, GIGA, JapanHDV, Javbus, JAVDatabase, JavDB, JavDB_zh, JavLibrary, JavLibrary_CN, R18.dev） |

---

## 七、遗留事项与待办

**非阻塞（已知且可接受）**

1. 空搜索词会扇出约 7.7s 才返回 0（无垃圾数据，仅偏慢）。
2. 全部未命中时仍会等最慢的那个（AdultTime 20s），之后自动进入 600s 降级冷却。
3. URL-only 刮削器（如 FantiaJp）不会出现在 Identify 的 provider 列表中（其不支持名称搜索），因此客户端 provider 级调用拿不到它——但**聚合搜索（`provider=all`）已能命中**。

4. **`FantiaProducts.yml` 不在任何备份链里**。它直接放在 `<stash config>/scrapers/FantiaProducts.yml`（NAS 上 `/vol2/1000/HSX/docker/stash/config/scrapers/`），既不属于 `sjp` 捆绑包、也不属于任何 Stash 包：
   - ✅ 好处：不会被 `installPackages` 的后台任务覆盖（见技能 §17.7 的「隐藏炸弹」）；
   - ⚠️ 风险：`stash-jellyfin-proxy` 的回滚（`_backup/<ts>/app.prev`）**不含它**，需单独备份。源文本已归档在本说明「修复四」一节，可直接据此恢复。

**待办**

5. **git 未提交**。`sjp/` 仓库现状：分支 `local/self-maintained`，**无 upstream**，remote 仅 `origin = https://github.com/feldorn/Stash-Jellyfin-Proxy.git`（上游作者，无 push 权限）。要推送需先 fork 并添加自己的 remote。未提交内容：
   - 已修改：`stash_jellyfin_proxy.conf`、`app.py`、`runtime.py`、`stash/client.py`、`ui/api.py`、`endpoints/metadata.py`
   - 未跟踪：`tests/unit/test_metadata_endpoints.py`、`tests/unit/test_metadata_scrape.py`、`dev-tools/build_deploy_bundle.py`
6. **（优先级更高）RAID0 备份**：`/vol1` 为零冗余 RAID0，磁盘 `sdd` 已累计约 38 万次 UDMA CRC 错误，数据存在全丢风险。此项优先级高于任何功能迭代。

---

## 附：相关技能文档同步情况

| 技能 | 变更 |
|---|---|
| `stash-custom-scraper` §19 | 更正旧结论（`scene_id` → `scrapeURL`）；新增 §19.6 两种假成功、§19.7 定向尝试优先级、§19.8 联合类型 / 400 无 body |
| `stash-custom-scraper` §20 | **新增**：Fantia 双命名空间（`/posts/` vs `/products/` 共用数字）、纯 XPath `FantiaProducts` 刮削器全文与两个 YAML 坑（`Performers:` 必须是映射、`blurred_ogp_` 去前缀）、多形态数字模板、相关性排序与「搜索 vs 排序」文本拆分、同源 URL 去重 |
| `fnos-docker-bindmount-deploy` §4.10 | 更正「先返回者胜」旧结论；新增 4.10(e) 跨网络路径状态码不同、4.10(f) 新查询形状先验证再部署 |

---

*说明：本文档补充《Stash-Jellyfin-Proxy中文文档.md》（产品说明中文版）中尚未涵盖的刮削能力与本次修复记录，两者可对照阅读。*
