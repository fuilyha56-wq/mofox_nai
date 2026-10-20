# MoFox NAI 图片创作与图库

独立的 Neo-MoFox 插件，由 **Lycoris radiata** 编写，许可证为 **GNU AGPL v3**。插件不依赖其他 MoFox 插件，只使用 MoFox 插件基类、消息发送 API、聊天历史 API 与任务管理器。

## 能力

- `/nai <中文描述或 Tag>` 支持 OpenAI 兼容的 `/images/generations` 与 NovelAI 原生 `/ai/generate-image` 两种接口。
- 自然语言提示词通过配置的翻译模型转换后再生图；直接输入 Tag 时原样使用。NAI 4.5 固定转换为英文 Tag，NAI V5 在英文自然语言描述与 Tag 之间自动选择。
- `/nai 反推` 读取被回复的图片，或本聊天最近一张图片，调用视觉模型反推 Tag，并以引用消息发送结果。
- 图片归档可选。默认 `persistent` 会把图片复制到本地图片目录与 SQLite；`off` 只从 MoFox 核心聊天历史临时读取，不在插件目录或插件数据库保存图片。
- `/nai 查看 <记录ID或消息ID>` 重新发送图片；`/nai 搜索 <关键词>` 搜索当前聊天的描述、Tag 和原消息文本。
- 可选的四角文字水印支持左上、右上、左下、右下位置，字体优先使用 **Noto Sans SC, sans-serif**。
- LLM 可调用图片历史、Qdrant 向量库、只读 SQLite NAI Tag 目录和 Danbooru 查询工具。Qdrant 语义搜索需要配置 OpenAI 兼容 Embeddings API；Tag SQLite 使用数据库自带的 FTS5 索引。
- Danbooru 默认强制 `rating:s`，可选配置用户名和 API Key。

## 命令

| 命令 | 作用 |
| --- | --- |
| `/nai <提示词>` | 自然语言或 Tag 生图 |
| `/nai 生图 <提示词>` | 显式生图 |
| `/nai 反推` | 反推引用图片或本聊天最近图片的 Tag |
| `/nai 历史 [数量]` | 列出本聊天图片；`off` 模式只读核心历史 |
| `/nai 查看 <记录ID或消息ID>` | 重新发送本聊天中指定图片 |
| `/nai 搜索 <关键词>` | 搜索图片描述、Tag 或消息文本 |
| `/nai 帮助` | 查看命令说明 |

## 安装与配置

把 `mofox_nai` 目录放到 MoFox 的 `plugins/` 下。启用后插件管理器会生成 `config/plugins/mofox_nai/config.toml`。先配置生图接口：

```toml
[generation]
base_url = "http://127.0.0.1:31555/v1"
api_format = "auto"
api_key = ""
model = "nai-diffusion-4-5-full"
prompt_model = ""
prompt_format = "auto"
width = 832
height = 1216
steps = 28
scale = 5.0
```

`api_format = "auto"` 会在模型名为 `nai-diffusion-*` 且地址不含 `/v1` 时使用 NovelAI 原生请求格式；地址含 `/v1` 时使用 OpenAI 兼容格式。你的 NAI 原生服务请设置 `api_format = "novelai"`，避免自动判断或旧配置选错协议；也可明确设为 `openai`。例如原生接口地址可配置为 `http://127.0.0.1:41800`，插件会请求 `/ai/generate-image`；`api_key` 填写服务要求的 Bearer Token。NovelAI 原生模式发送 `action`、`input`、`model` 和 `parameters` 字段，并能读取 JSON Base64 或 ZIP 图片响应。使用代理自定义路径时，可将完整 `/ai/generate-image` 地址填入 `base_url`，或将 `api_format` 指定为 `novelai`。

自然语言生图会使用 `prompt_model`；留空时复用 `[models].vision_model`，并通过兼容 Chat Completions 的 `[models].base_url` 请求。`prompt_format = "auto"` 会对 NAI 4.5 强制输出 Tag，并允许 NAI V5 翻译模型自行选择英文自然语言或 Tag；其他模型默认输出 Tag。也可以设置 `tags` 或 `natural_language` 指定翻译格式，但 NAI 4.5 始终只输出 Tag。直接输入 Tag 不需要提示词翻译模型。

反推与自动描述使用支持多模态输入的 OpenAI 兼容 Chat Completions 模型：

```toml
[models]
base_url = "http://127.0.0.1:31555/v1"
api_key = ""
vision_model = "your-vision-model"
embedding_base_url = "http://127.0.0.1:8000/v1"
embedding_api_key = ""
embedding_model = "your-embedding-model"
```

默认情况下，图片本体保存在 `data/mofox_nai/images/`，索引保存在 `data/mofox_nai/images.sqlite3`。如不希望插件另存图片，在生成的配置文件中设置：

```toml
[archive]
mode = "off"
```

`off` 模式不会创建插件图片目录或 SQLite 图片库，不会自动归档或索引图片。`/nai 历史`、`/nai 查看`、`/nai 反推` 和本地历史搜索会在请求期间从 MoFox 核心历史读取图片，图片字节只临时保存在内存中。此设置仅关闭本插件额外保存；MoFox 核心聊天历史仍按核心自身的配置和保留策略管理。旧配置可将 `enabled = false` 达到相同效果。`history_scan_limit` 控制回查数量。

要用 Qdrant 的向量语义检索，创建好维度与嵌入模型一致的 Collection 后配置：

```toml
[database]
qdrant_url = "http://127.0.0.1:6333"
qdrant_api_key = ""
collection = "mofox_images"
scope_to_stream = true

[archive]
auto_caption = true
auto_index = true
```

如要查询 NAI Tag SQLite 目录，保持 `qdrant_url` 为空并填写只读数据库路径：

```toml
[database]
qdrant_url = ""
sqlite_path = "E:/data/nai-tags.sqlite3"
```

`auto_caption` 和 `auto_index` 默认关闭，避免每张聊天图片都产生额外模型请求。打开后，视觉描述与嵌入写入由 MoFox 任务管理器在后台处理。`scope_to_stream=true` 时向量检索只返回带当前聊天流 ID 的记录；只有当你确认外部 Collection 可供所有聊天共享时，才将其关闭。

水印默认关闭。启用后设置水印内容，并确保机器安装 Noto Sans SC；若系统字体目录未被自动识别，请直接配置字体文件：

```toml
[watermark]
enabled = true
text = "Lycoris radiata"
position = "bottom_right"
font_family = "Noto Sans SC, sans-serif"
font_path = "C:/Windows/Fonts/NotoSansSC-Regular.otf"
font_size = 28
color = "#FFFFFF"
opacity = 190
margin = 24
```

`position` 接受 `bottom_left`、`bottom_right`、`top_left` 和 `top_right`。开启水印但找不到 Noto Sans SC 时，插件会返回清晰的字体路径错误，不会悄悄换字体。

Danbooru 工具默认只搜 `rating:s`。配置实例、登录信息和 API Key：

```toml
[danbooru]
enabled = true
base_url = "https://danbooru.donmai.us"
username = ""
api_key = ""
safe_only = true
```

## LLM 工具

- `generate_nai_image`：按当前对话需求生成并发送图片。
- `search_image_history`：只搜索当前流的本地图片历史。
- `search_image_database`：优先查询配置的 Qdrant；未配置 Qdrant 时，若设置 `database.sqlite_path` 则只读查询 NAI Tag SQLite 的 FTS5 目录；否则退回本地图片库或当前流核心历史文本。
- `search_danbooru`：按 Tag 查询 Danbooru。

## 隐私和保留

开启 `persistent` 时，图片数据保存在本机配置的目录和 SQLite 文件中，默认最多保存 5000 条图片消息记录，单张图片最大 20 MiB。`off` 模式不创建或写入插件本地图片归档，但 MoFox 核心历史仍由核心自身管理。历史回查遵循当前聊天流，不会跨聊天搜索；Qdrant 检索默认按 `stream_id` 限定范围。

