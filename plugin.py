"""MoFox NAI 图片创作、历史归档与检索插件入口。"""

# Copyright (C) 2026 Lycoris radiata
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BasePlugin, register_plugin

from .actions import GenerateNaiImageAction
from .archive import initialize_archive
from .commands import NaiCommand
from .config import MofoxNaiConfig
from .handlers import ArchiveChatImagesHandler
from .tools import SearchDanbooruTool, SearchImageDatabaseTool, SearchImageHistoryTool

logger = get_logger("mofox_nai")


@register_plugin
class MofoxNaiPlugin(BasePlugin):
    """注册图片命令、归档事件、生成 Action 和查询工具。"""

    plugin_name = "mofox_nai"
    configs = [MofoxNaiConfig]
    dependent_components: list[str] = []

    def get_components(self) -> list[type]:
        """按配置返回当前启用的插件组件。"""

        config = self.config if isinstance(self.config, MofoxNaiConfig) else MofoxNaiConfig()
        if not config.plugin.enabled:
            return []

        components: list[type] = [NaiCommand]
        if config.archive.enabled:
            components.append(ArchiveChatImagesHandler)
        if config.plugin.action_enabled:
            components.append(GenerateNaiImageAction)
        if config.plugin.tools_enabled:
            components.extend(
                [SearchImageHistoryTool, SearchImageDatabaseTool, SearchDanbooruTool]
            )
        return components

    async def on_plugin_loaded(self) -> None:
        """创建图片库目录和表结构。"""

        config = self.config if isinstance(self.config, MofoxNaiConfig) else MofoxNaiConfig()
        if not config.plugin.enabled:
            return
        try:
            await initialize_archive(config)
        except Exception:
            logger.exception("mofox_nai 图片归档数据库初始化失败")

