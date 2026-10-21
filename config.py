"""MoFox NAI 图片插件的配置模型。"""

# Copyright (C) 2026 Lycoris radiata
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from typing import Literal

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


class MofoxNaiConfig(BaseConfig):
    """命令、生图模型、视觉模型、图库、向量库与水印配置。"""

    name = "config"
    description = "MoFox NAI 图片创作与图库配置"

    @config_section("plugin")
    class PluginSection(SectionBase):
        """插件与 LLM 能力开关。"""

        enabled: bool = Field(default=True, description="是否启用插件")
        action_enabled: bool = Field(default=True, description="是否向 LLM 提供生图 Action")
        tools_enabled: bool = Field(default=True, description="是否向 LLM 提供图库、向量库及 Danbooru 查询工具")
        group_whitelist: list[str] = Field(
            default_factory=list,
            description="群聊白名单；留空表示不启用白名单，填写后仅允许名单内群使用插件",
        )
        group_blacklist: list[str] = Field(
            default_factory=list,
            description="群聊黑名单；名单内群禁止使用插件，黑名单优先于白名单",
        )
        private_whitelist: list[str] = Field(
            default_factory=list,
            description="私聊白名单（QQ 用户 ID）；留空表示不启用白名单，填写后仅允许名单内用户使用插件",
        )
        private_blacklist: list[str] = Field(
            default_factory=list,
            description="私聊黑名单（QQ 用户 ID）；名单内用户禁止使用插件，黑名单优先于白名单",
        )
        settings_admin_ids: list[str] = Field(
            default_factory=list,
            description="允许通过聊天命令修改生图设置和预设的用户 ID",
        )

    @config_section("generation")
    class GenerationSection(SectionBase):
        """OpenAI 兼容或 NovelAI 原生生图接口和默认参数。"""

        base_url: str = Field(default="http://127.0.0.1:31555/v1", description="生图 API 根地址；支持 OpenAI 兼容与 NovelAI 原生接口")
        api_format: Literal["auto", "openai", "novelai"] = Field(
            default="auto",
            description="生图协议；NAI 原生端点建议明确指定 novelai；auto 按模型和 URL 判断，也可指定 openai",
        )
        api_key: str = Field(default="", description="生图 API Bearer Token")
        model: str = Field(default="nai-diffusion-4-5-full", description="生图模型名称")
        prompt_model: str = Field(default="", description="可选的专用提示词翻译模型；留空时复用 models.vision_model，直接输入 Tag 无需模型")
        prompt_format: Literal["auto", "tags", "natural_language"] = Field(
            default="auto",
            description="翻译结果格式；auto 对 NAI V5 允许模型选择英文自然语言或 Tag，其他模型输出 Tag，NAI 4.5 始终输出 Tag",
        )
        request_mode: Literal["parallel", "queue"] = Field(
            default="parallel",
            description="图片请求调度；parallel 并行发送，queue 按顺序串行生成",
        )
        active_style: str = Field(default="", description="当前启用的画风预设名称，留空表示不附加画风")
        width: int = Field(default=832, ge=64, le=2048, description="默认图片宽度，建议为 64 的倍数")
        height: int = Field(default=1216, ge=64, le=2048, description="默认图片高度，建议为 64 的倍数")
        steps: int = Field(default=28, ge=1, le=50, description="采样步数")
        scale: float = Field(default=5.0, ge=0, le=30, description="CFG 引导强度")
        seed: int = Field(default=-1, ge=-1, le=2147483647, description="固定随机种子；-1 表示每次随机")
        sampler: str = Field(default="k_euler_ancestral", description="NAI 原生采样器")
        noise_schedule: str = Field(default="karras", description="NAI 原生噪声调度")
        reference_strength: float = Field(default=0.6, ge=0, le=1, description="参考图风格强度，范围 0–1")
        reference_information: float = Field(default=0.6, ge=0, le=1, description="参考图信息提取量，范围 0–1")
        precise_reference_strength: float = Field(default=1.0, ge=0, le=1, description="NAI 4.5 Precise Reference 强度，范围 0–1")
        precise_reference_fidelity: float = Field(default=1.0, ge=0, le=1, description="NAI 4.5 Precise Reference 忠实度，范围 0–1")
        redraw_strength: float = Field(default=0.65, ge=0, le=1, description="重绘保留原图程度，范围 0–1")
        redraw_noise: float = Field(default=0.2, ge=0, le=1, description="重绘噪声比例，范围 0–1")
        negative_prompt: str = Field(default="lowres, worst quality, bad quality, jpeg artifacts, bad hands, extra digits, watermark, signature", description="默认负面提示词")
        timeout: float = Field(default=180.0, description="生图请求超时秒数")

    @config_section("models")
    class ModelsSection(SectionBase):
        """视觉理解与文本嵌入模型设置。"""

        base_url: str = Field(default="http://127.0.0.1:31555/v1", description="OpenAI 兼容聊天模型 API 根地址")
        api_key: str = Field(default="", description="聊天模型 API Bearer Token")
        vision_model: str = Field(default="", description="支持图片输入的视觉模型，用于 /nai 反推和可选图库标注")
        embedding_base_url: str = Field(default="", description="OpenAI 兼容 Embeddings API 根地址；留空时使用 models.base_url")
        embedding_api_key: str = Field(default="", description="嵌入模型 API Bearer Token；留空时使用 models.api_key")
        embedding_model: str = Field(default="", description="文本嵌入模型名称，用于 Qdrant 语义检索")
        timeout: float = Field(default=60.0, description="视觉与嵌入请求超时秒数")

    @config_section("archive")
    class ArchiveSection(SectionBase):
        """聊天图片归档设置。"""

        mode: Literal["persistent", "off"] = Field(
            default="persistent",
            description="persistent 将图片保存到插件图库；off 不复制图片到插件目录/SQLite，并从 MoFox 核心历史临时读取",
        )
        enabled: bool = Field(
            default=True,
            description="兼容旧配置的本地归档开关；设为 false 与 mode=off 相同",
        )
        database_path: str = Field(default="data/mofox_nai/images.sqlite3", description="本地 SQLite 图片元数据数据库路径")
        image_dir: str = Field(default="data/mofox_nai/images", description="聊天图片及生成图片的本地文件目录")
        max_image_bytes: int = Field(default=20971520, description="单张图片最大归档大小，默认 20 MiB")
        max_records: int = Field(default=5000, description="最多保留的图片消息记录数量")
        history_scan_limit: int = Field(default=200, description="命令读取历史图片时最多回查的已有聊天消息数")
        auto_caption: bool = Field(default=False, description="归档时调用视觉模型生成图片描述；启用后每张入站图片都会产生一次模型请求")
        auto_index: bool = Field(default=False, description="将有描述或消息文本的归档图片自动嵌入并写入 Qdrant")

    @config_section("database")
    class DatabaseSection(SectionBase):
        """外部图片向量库和可选 NAI Tag SQLite 目录。"""

        qdrant_url: str = Field(default="", description="本地或远程 Qdrant HTTP 地址；配置后优先查询 Qdrant")
        qdrant_api_key: str = Field(default="", description="Qdrant API Key（本地无认证可留空）")
        collection: str = Field(default="mofox_images", description="Qdrant Collection 名称")
        sqlite_path: str = Field(default="", description="只读外部 NAI Tag SQLite 文件路径；Qdrant 未配置时优先查询该目录")
        default_limit: int = Field(default=5, description="查询工具默认返回数量，最多 10 条")
        scope_to_stream: bool = Field(default=True, description="Qdrant 查询默认限制在当前聊天流；关闭后会搜索整个配置的 Collection")

    @config_section("danbooru")
    class DanbooruSection(SectionBase):
        """Danbooru 查询设置。"""

        enabled: bool = Field(default=True, description="是否允许 LLM 查询 Danbooru")
        base_url: str = Field(default="https://danbooru.donmai.us", description="Danbooru 站点根地址")
        username: str = Field(default="", description="可选 Danbooru 用户名，用于 API 认证")
        api_key: str = Field(default="", description="可选 Danbooru API Key")
        safe_only: bool = Field(default=True, description="默认仅搜索 rating:s 安全内容")

    @config_section("watermark")
    class WatermarkSection(SectionBase):
        """生成图片文字水印设置。"""

        enabled: bool = Field(default=False, description="是否在生成图片左下角、右下角、左上角或右上角添加水印")
        text: str = Field(default="", description="水印文字；留空时只在 Action 显式传入 watermark_text 后显示")
        position: Literal["bottom_left", "bottom_right", "top_left", "top_right"] = Field(default="bottom_right", description="水印位置")
        font_family: str = Field(default="Noto Sans SC, sans-serif", description="水印字体族优先级")
        font_path: str = Field(default="", description="Noto Sans SC 字体文件路径；留空时自动查找常见安装位置")
        font_size: int = Field(default=28, description="水印字号")
        color: str = Field(default="#FFFFFF", description="水印颜色，格式为 #RRGGBB")
        opacity: int = Field(default=190, description="水印不透明度，范围 0 到 255")
        margin: int = Field(default=24, description="水印与图片边缘的间距（像素）")
        shadow: bool = Field(default=True, description="是否绘制深色文字描边以提高可读性")

    @config_section("presets")
    class PresetsSection(SectionBase):
        """可在聊天命令中切换的画风与模型预设。"""

        style_presets: list[dict[str, str]] = Field(
            default_factory=lambda: [
                {"name": "柔光", "prompt": "soft lighting, gentle expression, pastel colors, clean anime illustration"},
                {"name": "电影感", "prompt": "cinematic lighting, atmospheric composition, dramatic depth, film still"},
                {"name": "赛璐璐", "prompt": "cel shading, crisp outlines, flat colors, anime illustration"},
            ],
            description="画风预设；每项包含 name 和 prompt，可用 /nai 画风 添加 管理",
            input_type="json",
        )
        model_presets: list[dict[str, str]] = Field(
            default_factory=lambda: [
                {"name": "V5 Full", "model": "nai-diffusion-5-full"},
                {"name": "V4.5 Full", "model": "nai-diffusion-4-5-full"},
            ],
            description="模型预设；每项包含 name 和 model，可用 /nai 模型 添加 管理",
            input_type="json",
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    generation: GenerationSection = Field(default_factory=GenerationSection)
    models: ModelsSection = Field(default_factory=ModelsSection)
    archive: ArchiveSection = Field(default_factory=ArchiveSection)
    database: DatabaseSection = Field(default_factory=DatabaseSection)
    danbooru: DanbooruSection = Field(default_factory=DanbooruSection)
    watermark: WatermarkSection = Field(default_factory=WatermarkSection)
    presets: PresetsSection = Field(default_factory=PresetsSection)


def archive_is_enabled(config: MofoxNaiConfig) -> bool:
    """判断插件本地图片持久化是否开启。"""

    return config.archive.enabled and config.archive.mode == "persistent"
