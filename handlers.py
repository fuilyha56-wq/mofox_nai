"""归档聊天中收到和发送的图片。"""

# Copyright (C) 2026 Lycoris radiata
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import asyncio
from typing import Any

from src.app.plugin_system.base import BaseEventHandler
from src.app.plugin_system.types import EventType
from src.kernel.concurrency import get_task_manager
from src.kernel.event import EventDecision
from src.kernel.logger import get_logger

from .archive import archive_image, message_images, update_image_description
from .config import MofoxNaiConfig
from .service import MofoxNaiService

logger = get_logger("mofox_nai.archive")


class ArchiveChatImagesHandler(BaseEventHandler):
    """在消息发送成功或收到后归档图片及其来源信息。"""

    name = "archive_chat_images"
    description = "把当前聊天中的图片复制到 MoFox NAI 本地图片库"
    init_subscribe = [EventType.ON_MESSAGE_RECEIVED, EventType.AFTER_MESSAGE_SENT]

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """快速提取图片并写入本地归档，耗时索引交给任务管理器。"""

        config = self.plugin.config
        if not isinstance(config, MofoxNaiConfig) or not config.archive.enabled:
            return EventDecision.PASS, params
        message = params.get("message")
        if message is None:
            return EventDecision.PASS, params

        max_bytes = max(1024, config.archive.max_image_bytes)
        images = await asyncio.to_thread(message_images, message, max_bytes)
        if not images:
            return EventDecision.PASS, params

        direction = "outgoing" if event_name == EventType.AFTER_MESSAGE_SENT.value else "incoming"
        for image_data, extension in images:
            try:
                record = await archive_image(config, message, image_data, extension, direction)
                if record and (config.archive.auto_caption or config.archive.auto_index):
                    get_task_manager().create_task(
                        self._enrich_record(config, record, image_data),
                        name=f"mofox_nai_index_{record.get('id', 'image')}",
                        daemon=True,
                    )
            except Exception:
                logger.exception("聊天图片归档失败")
        return EventDecision.SUCCESS, params

    @staticmethod
    async def _enrich_record(
        config: MofoxNaiConfig, record: dict[str, Any], image_data: bytes
    ) -> None:
        """在后台补充图片描述并可选写入 Qdrant。"""

        service = MofoxNaiService(config)
        current = record
        if config.archive.auto_caption and not str(record.get("caption", "")).strip():
            if config.models.vision_model.strip():
                caption = await service.describe_image(image_data)
                current = update_image_description(
                    config, int(record["id"]), caption, str(record.get("tags", ""))
                ) or record
        if config.archive.auto_index and config.database.qdrant_url.strip():
            await service.index_record(current)

