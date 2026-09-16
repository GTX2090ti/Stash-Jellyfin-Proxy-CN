# Stash-Jellyfin Proxy（CN 分支）

**当前版本 v7.3.10-CN.5** · 基于上游 [feldorn/Stash-Jellyfin-Proxy](https://github.com/feldorn/Stash-Jellyfin-Proxy) v7.3.10 · MIT

一个 Python 代理服务器，通过模拟 Jellyfin 的 HTTP API，让兼容 Jellyfin 的媒体播放器（Infuse / Swiftfin / SenPlayer 等）直接浏览并播放 [Stash](https://stashapp.cc/) 媒体库。

> 英文说明见 [README.en.md](README.en.md)。完整更新日志见 [CHANGELOG.md](CHANGELOG.md)。

## CN 分支新增（相对上游 v7.3.10）

| 功能 | 说明 | 详细文档 |
|---|---|---|
| **多文件（合并）场景播放** | 合并场景的每个文件作为独立版本下发，客户端出现版本选择器；非主文件由代理直读磁盘（拖进度条正常），主文件仍走 Stash 原生流。默认关闭，需 `MULTI_FILE_SCENES=true` + `LIBRARY_PATH_MAP` | [MULTIFILE-AND-I18N.md](MULTIFILE-AND-I18N.md) |
| **配置界面中英双语** | Web 界面 8 个标签页全部支持中英切换，侧边栏一键切换；协议层零改动 | [MULTIFILE-AND-I18N.md](MULTIFILE-AND-I18N.md) |
| **元数据刮削管线** | 打通手机 App 的 Identify / 刷新元数据：按名搜索、纯数字编号、URL 直刮三类入口，支持社区刮削器 + StashDB / ThePornDB | [SCRAPING-NOTES.md](SCRAPING-NOTES.md) |
| **媒体库名汉化** | 客户端里的库分类名（场景 / 厂商 / 演员 / 分组 / 播放列表）已汉化 | — |
| **客户端导航兼容** | 按客户端 UA 下发合适的条目类型（SenPlayer / Yamby → `Studio`，Infuse → `BoxSet`）；`GenreIds=studio-N` / `StudioIds` / `ParentId=studio-N` 等各客户端写法统一解析为容器定位；容器型 `/Similar` 返回该容器的作品；空工作室（0 场景）不下发 | [CHANGELOG.md](CHANGELOG.md) |

## 支持的客户端

| 客户端 | 平台 | 状态 |
| --- | --- | --- |
| Infuse | iOS / tvOS / macOS | 完全支持 |
| Swiftfin | iOS / tvOS | 完全支持（含原生剧集导航） |
| SenPlayer | iOS | 完全支持 |
| Yamby 等 SDK 客户端 | Android / iOS | 可用，按 UA 自动适配 |

官方 Jellyfin 应用不支持（其依赖 WebView 加载 `jellyfin-web`，代理不附带）。

## 快速开始

**Docker（推荐）：**

```bash
docker run -d \
  --name stash-jellyfin-proxy \
  -p 8096:8096 -p 8097:8097 \
  -v /path/to/config:/config \
  -e PUID=1000 -e PGID=1000 -e TZ=Asia/Shanghai \
  ghcr.io/gtx2090ti/stash-jellyfin-proxy-cn:7.3.10
```

**独立运行**（Python 3.10+）：

```bash
pip install -e .
python -m stash_jellyfin_proxy
```

启动后：

1. 打开 Web 界面 `http://<host>:8097`，在「连接」标签页填 Stash URL / API Key / 登录凭据
2. 客户端中添加 Jellyfin 服务器 `http://<host>:8096`，用同一组凭据登录

## 常用配置

配置文件 `stash_jellyfin_proxy.conf`（Web 界面保存时自动重写，也可用同名环境变量）。完整键位列表以 Web 界面和配置文件内注释为准。

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `STASH_URL` | `http://localhost:9999` | Stash 服务器地址 |
| `STASH_API_KEY` | *(必填)* | Stash → 设置 → 安全 |
| `SJS_USER` / `SJS_PASSWORD` | *(必填)* | 客户端登录凭据 |
| `PROXY_PORT` / `UI_PORT` | `8096` / `8097` | API / Web 界面端口 |
| `MULTI_FILE_SCENES` | `false` | 多文件场景版本选择器 |
| `LIBRARY_PATH_MAP` | 空 | `Stash路径:容器路径` 对，供非主文件直读 |
| `SERIES_TAG` | `Series` | 带此标签的制片商成为剧集库 |
| `FAVORITE_TAG` | 空 | 场景/合集收藏标签 |
| `TAG_GROUPS` | 空 | 逗号分隔的标签，成为顶层文件夹 |
| `PLAYLIST_PARENT_TAG` | `Playlists` | 播放列表父标签 |
| `GENRE_MODE` | `parent_tag` | 流派来源：`all_tags` / `parent_tag` / `top_n` |
| `ENABLE_SCRAPING` | `true` | 刮削总开关 |
| `UI_LANGUAGE` | `auto` | Web 界面默认语言 `auto` / `en` / `zh` |

按客户端行为的差异化（海报格式、条目类型等）在 `[player.<name>]` 区段或 Web 界面「播放器」标签页配置，按 User-Agent 自动匹配。

## 架构

```
Jellyfin 客户端（Infuse / Swiftfin / SenPlayer / Yamby）
        │
        ▼
   stash-jellyfin-proxy（Starlette + Hypercorn，端口 8096）
   ─ 异步 httpx → Stash GraphQL，字节区间透传
   ─ 按客户端的播放器配置档
        │
        ▼
   Stash（GraphQL API，端口 9999）
```

## 已知限制

- **单用户认证**：所有客户端共享同一组凭据
- **官方 Jellyfin 应用不支持**：需要代理不附带的 `jellyfin-web` WebView 包
- **剧集原生导航仅 Swiftfin**：其他客户端回退到扁平 BoxSet 视图
- **图片缓存**：客户端忽略 HTTP 缓存头，代理靠重启轮换 `ImageTag` 刷新图片；个别图片卡住时清客户端缓存最可靠

## 更多文档

- [CHANGELOG.md](CHANGELOG.md) —— 完整更新日志（CN.x + 上游历史）
- [MULTIFILE-AND-I18N.md](MULTIFILE-AND-I18N.md) —— 多文件方案与 i18n 实现细节、踩坑记录
- [SCRAPING-NOTES.md](SCRAPING-NOTES.md) —— 刮削管线排障记录

## 许可证

MIT —— 见 `LICENSE`。
