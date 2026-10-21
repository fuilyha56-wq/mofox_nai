"""供 LLM 生成图片并发送到当前聊天的 Action。"""

# Copyright (C) 2026 Lycoris radiata
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import base64
from typing import Annotated, Literal

from src.app.plugin_system.api.send_api import send_image
from src.app.plugin_system.base import BaseAction
from src.kernel.logger import get_logger

from .access import chat_access
from .config import MofoxNaiConfig
from .archive import message_images
from .service import MofoxNaiService

logger = get_logger("mofox_nai.action")


class GenerateNaiImageAction(BaseAction):
    """按自然语言或 NAI Tag 生图并分享给当前聊天。"""

    name = "generate_nai_image"
    description = (
        "按当前聊天需求生成图片，也支持基于用户明确回复的图片做 NAI 风格参考或重绘。"
        "引用图像模式必须从被引用消息读取图片，不能从更早的聊天消息猜选目标。"
    )
    associated_types: list[str] = ["image"]

    async def execute(
        self,
        prompt: Annotated[str, "图片内容；支持自然语言描述或逗号分隔的 NovelAI/Danbooru Tag"],
        negative_prompt: Annotated[str, "可选的负面提示词"] = "",
        watermark_text: Annotated[str, "可选水印文字；全局水印开启时留空使用配置文字"] = "",
        image_mode: Annotated[Literal["generate", "reference", "redraw"], "generate 普通生成；reference 使用被引用图作为风格参考；redraw 使用被引用图进行 img2img"] = "generate",
        reference_strength: Annotated[float, "reference 模式的强度，范围 0–1"] = 0.6,
        redraw_strength: Annotated[float, "redraw 模式保留原图的强度，范围 0–1"] = 0.65,
    ) -> tuple[bool, str]:
        """生成图片、套用可选水印并发送到 Action 所在聊天。"""

        config = self.plugin.config
        if not isinstance(config, MofoxNaiConfig):
            return False, "mofox_nai 配置尚未加载"
        context = getattr(self.chat_stream, "context", None)
        current_message = getattr(context, "current_message", None) if context else None
        allowed, reason = chat_access(config, current_message)
        if not allowed:
            return False, reason
        try:
            source_image = None
            source_message_id = None
            if image_mode != "generate":
                reply_to = str(getattr(current_message, "reply_to", "") or "").strip()
                target = self._find_context_message(reply_to) if reply_to else None
                if target is None or not reply_to:
                    raise ValueError("参考或重绘模式需要用户明确回复一条含图片的消息")
                images = message_images(target, max(1024, config.archive.max_image_bytes))
                if not images:
                    raise ValueError("被引用的消息没有可读取的图片")
                source_image = images[0][0]
                source_message_id = reply_to
            image_data, final_prompt = await MofoxNaiService(config).generate_image(
                prompt=prompt,
                negative_prompt=negative_prompt,
                watermark_text=watermark_text or None,
                operation=image_mode,
                source_image=source_image,
                reference_strength=reference_strength,
                redraw_strength=redraw_strength,
            )
        except Exception as error:
            logger.error("LLM 生图 Action 失败", exc_info=error)
            return False, f"生图失败：{error}"

        context = getattr(self.chat_stream, "context", None)
        history = getattr(context, "history_messages", []) if context else []
        reply_to = source_message_id or (history[-1].message_id if history else None)
        sent = await send_image(
            image_data=base64.b64encode(image_data).decode("ascii"),
            stream_id=self.chat_stream.stream_id,
            platform=self.chat_stream.platform,
            processed_plain_text=f"[AI 图片: {prompt[:120]}]",
            reply_to=reply_to,
        )
        return (True, f"图片已生成并发送。提示词: {final_prompt[:500]}") if sent else (False, "图片已生成，但发送失败")
