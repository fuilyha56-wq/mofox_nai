# 配置参考

配置文件由 MoFox 自动生成到 `config/plugins/mofox_nai/config.toml`。API Key 留空时不发送认证头。

| 配置节 | 字段 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `plugin` | `enabled` | `true` | 插件总开关 |
| `plugin` | `action_enabled` | `true` | 是否注册 LLM 生图 Action |
| `plugin` | `tools_enabled` | `true` | 是否注册图库、数据库和 Danbooru 查询工具 |
| `generation` | `base_url` | `http://127.0.0.1:31555/v1` | 生图 API 根地址；按协议拼接端点，也可直接填写完整端点 |
| `generation` | `api_format` | `auto` | `auto` 按模型名和 URL 路径判断；也可指定 `openai` 或 NovelAI 原生格式 `novelai` |
| `generation` | `api_key` | 空 | 生图 API Token |
| `generation` | `model` | `nai-diffusion-4-5-full` | 生图模型名 |
| `generation` | `prompt_model` | 空 | 自然语言提示词翻译模型；留空时复用 `models.vision_model` |
| `generation` | `prompt_format` | `auto` | `auto` 对 NAI V5 由翻译模型选择英文自然语言或 Tag，其他模型输出 Tag；NAI 4.5 始终输出 Tag |
| `generation` | `width` / `height` | `832` / `1216` | 生图尺寸 |
| `generation` | `steps` / `scale` | `28` / `5.0` | 采样参数 |
| `generation` | `negative_prompt` | 常用质量排除词 | 默认负面提示词 |
| `models` | `base_url` / `api_key` | 本地 `/v1` / 空 | Chat Completions 与视觉模型 API |
| `models` | `vision_model` | 空 | 必填于图片 Tag 反推和视觉描述 |
| `models` | `embedding_base_url` | 空 | 留空则用 `models.base_url` |
| `models` | `embedding_model` | 空 | Qdrant 语义检索的 Embeddings 模型 |
| `archive` | `enabled` | `true` | 图片消息归档开关 |
| `archive` | `mode` | `persistent` | `persistent` 将图片另存到插件目录和 SQLite；`off` 不做插件级持久化，命令临时从 MoFox 核心历史读取；设 `enabled=false` 也会关闭 |
| `archive` | `database_path` | `data/mofox_nai/images.sqlite3` | 插件 SQLite 元数据文件 |
| `archive` | `image_dir` | `data/mofox_nai/images` | 图片文件目录 |
| `archive` | `max_image_bytes` | 20 MiB | 单图最大文件大小 |
| `archive` | `max_records` | `5000` | 归档记录上限 |
| `archive` | `history_scan_limit` | `200` | `/nai 历史`、查看、搜索与反推最多读取的核心历史消息数 |
| `archive` | `auto_caption` | `false` | 后台调用视觉模型描述新图 |
| `archive` | `auto_index` | `false` | 将有文本的图片记录嵌入并写入 Qdrant |
| `database` | `qdrant_url` | 空 | Qdrant 地址；配置后 `search_image_database` 优先进行向量查询 |
| `database` | `sqlite_path` | 空 | 只读外部 NAI Tag SQLite 文件；未配置 Qdrant 时使用其中的 FTS5 Tag 索引；再无此项时才查询插件图片库/核心历史 |
| `database` | `collection` | `mofox_images` | 预先创建的 Qdrant Collection |
| `database` | `scope_to_stream` | `true` | Qdrant 按当前 stream ID 隔离 |
| `danbooru` | `enabled` | `true` | Danbooru 查询开关 |
| `danbooru` | `base_url` | `https://danbooru.donmai.us` | Danbooru API 根地址 |
| `danbooru` | `username` / `api_key` | 空 / 空 | 可选的 API 认证 |
| `danbooru` | `safe_only` | `true` | 强制查询 `rating:s` |
| `watermark` | `enabled` | `false` | 是否应用文字水印 |
| `watermark` | `text` | 空 | 默认水印内容 |
| `watermark` | `position` | `bottom_right` | `bottom_left`、`bottom_right`、`top_left`、`top_right` |
| `watermark` | `font_family` | `Noto Sans SC, sans-serif` | 字体优先级名称 |
| `watermark` | `font_path` | 空 | Noto Sans SC 字体文件路径 |
| `watermark` | `font_size` | `28` | 字号 |
| `watermark` | `color` / `opacity` | `#FFFFFF` / `190` | 颜色与 0–255 不透明度 |
| `watermark` | `margin` / `shadow` | `24` / `true` | 边距与描边开关 |

Qdrant 查询 API 使用 `POST /collections/{collection}/points/query`，索引使用 `PUT /collections/{collection}/points`。插件不会自动创建 Collection；其向量维度必须与配置的嵌入模型输出一致。NAI Tag SQLite 数据库以只读方式打开，要求包含 `catalog_tags` 和 FTS5 虚表 `catalog_search`；该查询使用数据库自带的全文索引，不需要 Embeddings API。

`api_format = "openai"` 使用 `POST /images/generations`，接受 OpenAI 风格的 `data[0].b64_json` 或 `data[0].url`。`api_format = "novelai"` 使用 `POST /ai/generate-image` 与 NovelAI 原生请求格式，通过 `Accept: application/json` 优先接收 `images[].image` Base64，也能直接读取原生 ZIP 图片响应。`auto` 对 `nai-diffusion-*` 模型且 URL 中不含 `/v1` 的配置选择原生格式；包含 `/v1` 则选择 OpenAI 兼容格式。若代理使用自定义模型名或路由，显式指定 `api_format` 可覆盖自动判断。

### 免保存模式

将 `[archive].mode` 设为 `"off"` 后，插件不会创建本地图像目录或 SQLite 图片库，也不会把聊天图片复制到插件存储。历史、查看、反推和历史搜索会在请求期间从 MoFox 核心聊天历史读取图片，图片字节只在内存中处理。MoFox 核心仍可能按自身设置保存聊天记录和图片；此选项只关闭本插件额外归档。旧配置的 `enabled = false` 具有相同效果。

## 许可证声明

版权所有 (C) 2026 Lycoris radiata。程序按 GNU Affero General Public License v3.0-only 授权，完整条款见插件根目录 `LICENSE`。

