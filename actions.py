"""供 LLM 生成图片并发送到当前聊天的 Action。"""

# Copyright (C) 2026 Lycoris radiata
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import base64
from typing import Annotated

from src.app.plugin_system.api.send_api import send_image
from src.app.plugin_system.base import BaseAction
from src.kernel.logger import get_logger

from .config import MofoxNaiConfig
from .service import MofoxNaiService

logger = get_logger("mofox_nai.action")


class GenerateNaiImageAction(BaseAction):
    """按自然语言或 NAI Tag 生图并分享给当前聊天。"""

    name = "generate_nai_image"
    description = (
        "根据用户当前聊天语境生成并发送一张图片。参数可写中文自然语言或逗号分隔的 "
        "NovelAI/Danbooru Tag；用户明确希望看图、画图或要求生成图片时调用。"
    )
    associated_types: list[str] = ["image"]

    async def execute(
        self,
        prompt: Annotated[str, "图片内容；支持自然语言描述或逗号分隔的 NovelAI/Danbooru Tag"],
        negative_prompt: Annotated[str, "可选的负面提示词"] = "",
        watermark_text: Annotated[str, "可选水印文字；全局水印开启时留空使用配置文字"] = "",
    ) -> tuple[bool, str]:
        """生成图片、套用可选水印并发送到 Action 所在聊天。"""

        config = self.plugin.config
        if not isinstance(config, MofoxNaiConfig):
            return False, "mofox_nai 配置尚未加载"
        try:
            image_data, final_prompt = await MofoxNaiService(config).generate_image(
                prompt=prompt,
                negative_prompt=negative_prompt,
                watermark_text=watermark_text or None,
            )
        except Exception as error:
            logger.exception("LLM 生图 Action 失败")
            return False, f"生图失败：{error}"

        context = getattr(self.chat_stream, "context", None)
        history = getattr(context, "history_messages", []) if context else []
        reply_to = history[-1].message_id if history else None
        sent = await send_image(
            image_data=base64.b64encode(image_data).decode("ascii"),
            stream_id=self.chat_stream.stream_id,
            platform=self.chat_stream.platform,
            processed_plain_text=f"[AI 图片: {prompt[:120]}]",
            reply_to=reply_to,
        )
        return (True, f"图片已生成并发送。Tag: {final_prompt[:500]}") if sent else (False, "图片已生成，但发送失败")
