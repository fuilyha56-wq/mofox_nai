"""MoFox NAI 的 /nai 命令与图片历史操作。"""

# Copyright (C) 2026 Lycoris radiata
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import asyncio
import base64
from datetime import datetime
from typing import Any

from src.app.plugin_system.api.send_api import send_image, send_text
from src.app.plugin_system.api.stream_api import get_stream_messages
from src.app.plugin_system.base import BaseCommand, cmd_route
from src.app.plugin_system.types import PermissionLevel
from src.kernel.logger import get_logger

from .archive import (
    archive_image,
    get_image_record,
    list_core_image_messages,
    list_recent_images,
    message_images,
    read_image_file,
)
from .config import MofoxNaiConfig, archive_is_enabled
from .service import MofoxNaiService

logger = get_logger("mofox_nai.command")


class NaiCommand(BaseCommand):
    """生图、图片反推与当前聊天图片历史命令。"""

    name = "nai"
    description = "自然语言或 Tag 生图、对图片反推 Tag、查看聊天图片历史"
    command_prefix = "/"
    permission_level = PermissionLevel.USER

    async def execute(self, message_text: str) -> tuple[bool, str]:
        """保留命令基类的 Trie 路由行为。"""

        return await super().execute(message_text)

    def _config(self) -> MofoxNaiConfig | None:
        """读取当前插件配置。"""

        config = self.plugin.config
        return config if isinstance(config, MofoxNaiConfig) else None

    def _full_text(self) -> str:
        """获取完整原始命令，保留自然语言中的空格和标点。"""

        message = self._message
        content = getattr(message, "content", "") if message is not None else ""
        if isinstance(content, str) and content.strip():
            return content.strip()
        plain_text = getattr(message, "processed_plain_text", "") if message is not None else ""
        return str(plain_text or "").strip()

    def _remainder(self, subcommand: str = "") -> str:
        """从命令原文中提取生图提示词或子命令参数。"""

        text = self._full_text()
        if text.startswith("/"):
            text = text[1:]
        parts = text.split(maxsplit=1)
        text = parts[1] if len(parts) > 1 else ""
        if subcommand and text.startswith(subcommand):
            text = text[len(subcommand) :]
        return text.strip()

    async def _reply(self, text: str, reply_to: str | None = None) -> bool:
        """发送命令文本回应。"""

        return await send_text(
            content=text,
            stream_id=self.stream_id,
            platform=getattr(self._message, "platform", None),
            reply_to=reply_to or self.message_id or None,
        )

    async def _generate(self, prompt: str) -> tuple[bool, str]:
        """生成并回复发送图片。"""

        config = self._config()
        if config is None:
            await self._reply("mofox_nai 配置尚未加载。")
            return False, "mofox_nai 配置尚未加载"
        if not prompt.strip():
            help_text = (
                "用法：/nai <中文描述或逗号分隔的 Tag>\n"
                "/nai 反推：反推回复图片或本聊天最近一张图片\n"
                "/nai 历史 [数量]：查看本聊天图片记录\n"
                "/nai 查看 <记录ID或消息ID>：发送图片\n"
                "/nai 搜索 <关键词>：搜索本聊天图片描述和 Tag"
            )
            await self._reply(help_text)
            return True, help_text
        try:
            image_data, final_prompt = await MofoxNaiService(config).generate_image(prompt)
            sent = await send_image(
                image_data=base64.b64encode(image_data).decode("ascii"),
                stream_id=self.stream_id,
                platform=getattr(self._message, "platform", None),
                processed_plain_text=f"[NAI 图片: {prompt[:120]}]",
                reply_to=self.message_id or None,
            )
            if not sent:
                raise RuntimeError("平台没有成功发送生成图片")
        except Exception as error:
            logger.exception("/nai 生图失败")
            await self._reply(f"生图失败：{error}")
            return False, str(error)
        return True, f"图片已发送。使用 Tag：{final_prompt[:500]}"

    async def _target_record(self) -> dict[str, Any] | None:
        """按当前附图、引用消息或当前流最近记录的顺序选择图片。"""

        config = self._config()
        if config is None:
            return None
        if not archive_is_enabled(config):
            return None
        await self._backfill_history(config)
        if self._message is not None:
            images = await asyncio.to_thread(
                message_images, self._message, max(1024, config.archive.max_image_bytes)
            )
            for image_data, extension in images:
                record = await archive_image(
                    config, self._message, image_data, extension, "incoming"
                )
                if record:
                    return record
            reply_to = str(getattr(self._message, "reply_to", "") or "")
            if reply_to:
                record = get_image_record(config, stream_id=self.stream_id, message_id=reply_to)
                if record:
                    return record
                return None
        return get_image_record(
            config,
            stream_id=self.stream_id,
            message_id="",
        )

    async def _backfill_history(self, config: MofoxNaiConfig) -> None:
        """将核心聊天数据库中最近消息的媒体补录到插件图片库。"""

        if not archive_is_enabled(config):
            return
        limit = max(1, min(config.archive.history_scan_limit, 1000))
        try:
            messages = await get_stream_messages(self.stream_id, limit=limit)
        except Exception:
            logger.exception("读取 MoFox 历史消息失败")
            return
        for message in messages:
            direction = "outgoing" if getattr(message, "sender_role", "") == "bot" else "incoming"
            images = await asyncio.to_thread(
                message_images, message, max(1024, config.archive.max_image_bytes)
            )
            for image_data, extension in images:
                try:
                    await archive_image(config, message, image_data, extension, direction)
                except Exception:
                    logger.exception("历史消息图片补录失败")

    async def _target_core_image(self) -> tuple[bytes, str] | None:
        """在免保存模式下从消息对象或核心历史内存读取目标图片。"""

        config = self._config()
        if config is None:
            return None
        images = await asyncio.to_thread(
            message_images, self._message, max(1024, config.archive.max_image_bytes)
        ) if self._message is not None else []
        if images:
            return images[0][0], str(self.message_id or "")

        reply_to = str(getattr(self._message, "reply_to", "") or "") if self._message else ""
        rows = await list_core_image_messages(
            self.stream_id,
            limit=config.archive.history_scan_limit,
            max_bytes=config.archive.max_image_bytes,
            exclude_message_id=self.message_id,
        )
        if reply_to:
            target = next((row for row in rows if row["message_id"] == reply_to), None)
            if target and target["_images"]:
                return target["_images"][0][0], target["message_id"]
            return None
        if rows and rows[0]["_images"]:
            return rows[0]["_images"][0][0], rows[0]["message_id"]
        return None

    @cmd_route()
    async def handle_generate(self) -> tuple[bool, str]:
        """把 /nai 后的任意自然语言或 Tag 交给生图流程。"""

        return await self._generate(self._remainder())

    @cmd_route("生图")
    async def handle_generate_alias(self) -> tuple[bool, str]:
        """显式以 /nai 生图 开始生成。"""

        return await self._generate(self._remainder("生图"))

    @cmd_route("生成")
    async def handle_generate_word(self) -> tuple[bool, str]:
        """显式以 /nai 生成 开始生成。"""

        return await self._generate(self._remainder("生成"))

    @cmd_route("反推")
    async def handle_reverse(self) -> tuple[bool, str]:
        """反推图片 Tag，并在引用原图片的消息中发送结果。"""

        config = self._config()
        if config is None:
            return False, "mofox_nai 配置尚未加载"
        try:
            service = MofoxNaiService(config)
            if archive_is_enabled(config):
                record = await self._target_record()
                if not record:
                    message = "没有找到可反推的图片。请回复一张图片后发送 /nai 反推，或检查本聊天是否已有图片历史。"
                    await self._reply(message)
                    return False, message
                tags, caption = await service.reverse_record(record, self.stream_id)
                original_message_id = str(record.get("message_id", ""))
            else:
                target = await self._target_core_image()
                if not target:
                    message = "没有找到可反推的图片。请回复一张图片后发送 /nai 反推，或确认该图片仍在 MoFox 核心聊天历史中。"
                    await self._reply(message)
                    return False, message
                image_data, original_message_id = target
                tags, caption = await service.reverse_image_bytes(image_data)
            content = f"反推 Tag：\n{tags}"
            if caption:
                content += f"\n\n图片描述：{caption}"
            sent = await self._reply(content, reply_to=original_message_id or None)
            return (True, content) if sent else (False, "Tag 已反推，但消息发送失败")
        except Exception as error:
            logger.exception("/nai 反推失败")
            message = f"图片反推失败：{error}"
            await self._reply(message)
            return False, message

    @cmd_route("历史")
    async def handle_history(self, limit: int = 10) -> tuple[bool, str]:
        """列出当前聊天中最近归档的图片记录。"""

        config = self._config()
        if config is None:
            return False, "mofox_nai 配置尚未加载"
        if archive_is_enabled(config):
            await self._backfill_history(config)
            rows = list_recent_images(
                config, self.stream_id, max(1, min(limit, 30)), exclude_message_id=self.message_id
            )
        else:
            rows = await list_core_image_messages(
                self.stream_id,
                limit=config.archive.history_scan_limit,
                max_bytes=config.archive.max_image_bytes,
                exclude_message_id=self.message_id,
            )
            rows = rows[: max(1, min(limit, 30))]
        if not rows:
            message = "MoFox 核心聊天历史中没有找到图片。" if not archive_is_enabled(config) else "本聊天还没有已归档的图片。"
        else:
            lines = ["本聊天最近图片（时间顺序由近到远）："]
            for row in rows:
                timestamp = datetime.fromtimestamp(float(row["created_at"])).strftime("%m-%d %H:%M")
                description = str(row.get("caption") or row.get("tags") or row.get("source_text") or "无描述")
                identifier = row.get("id") if archive_is_enabled(config) else row.get("message_id")
                suffix = f" · {row.get('image_count', 1)} 张" if not archive_is_enabled(config) else ""
                lines.append(f"#{identifier} · {timestamp} · {row.get('sender_name') or '未知发送者'} · {description[:100]}{suffix}")
            ref_label = "记录ID" if archive_is_enabled(config) else "消息ID"
            lines.append(f"使用 /nai 查看 <{ref_label}> 发送图片，或回复图片后使用 /nai 反推。")
            message = "\n".join(lines)
        sent = await self._reply(message)
        return (True, message) if sent else (False, "历史图片列表发送失败")

    @cmd_route("查看")
    async def handle_show(self, record_ref: str) -> tuple[bool, str]:
        """把当前聊天里指定的归档图片或核心历史图片重新发送。"""

        config = self._config()
        if config is None:
            return False, "mofox_nai 配置尚未加载"
        try:
            if archive_is_enabled(config):
                if not record_ref.isdigit():
                    raise ValueError("本地归档模式下请使用 /nai 历史 显示的数字记录 ID")
                record_id = int(record_ref)
                record = get_image_record(config, record_id=record_id, stream_id=self.stream_id)
                if not record:
                    raise LookupError("没有找到该图片记录，或它不属于当前聊天。")
                image_data_list = [await asyncio.to_thread(read_image_file, record)]
                label = f"[归档图片 #{record_id}]"
            else:
                rows = await list_core_image_messages(
                    self.stream_id,
                    limit=config.archive.history_scan_limit,
                    max_bytes=config.archive.max_image_bytes,
                    exclude_message_id=self.message_id,
                )
                record = next((row for row in rows if row["message_id"] == record_ref), None)
                if not record:
                    raise LookupError("MoFox 核心聊天历史中没有找到该消息图片。")
                image_data_list = [data for data, _ in record["_images"]]
                label = f"[历史图片 {record_ref}]"
            sent = True
            for image_data in image_data_list:
                sent = await send_image(
                    image_data=base64.b64encode(image_data).decode("ascii"),
                    stream_id=self.stream_id,
                    platform=getattr(self._message, "platform", None),
                    processed_plain_text=label,
                    reply_to=self.message_id or None,
                ) and sent
        except Exception as error:
            logger.exception("归档图片读取失败")
            message = f"图片读取失败：{error}"
            await self._reply(message)
            return False, message
        return (True, f"已发送图片 {record_ref}") if sent else (False, "归档图片发送失败")

    @cmd_route("搜索")
    async def handle_search(self) -> tuple[bool, str]:
        """搜索当前聊天的本地图片描述、Tag 和来源文本。"""

        config = self._config()
        query = self._remainder("搜索").strip()
        if config is None or not query:
            message = "用法：/nai 搜索 <关键词>"
            await self._reply(message)
            return False, message
        if archive_is_enabled(config):
            rows = list_recent_images(config, self.stream_id, 100)
        else:
            rows = await list_core_image_messages(
                self.stream_id,
                limit=config.archive.history_scan_limit,
                max_bytes=config.archive.max_image_bytes,
                exclude_message_id=self.message_id,
            )
        query_terms = [part.casefold() for part in query.split() if part]
        matched = [
            row
            for row in rows
            if any(
                term in " ".join(
                    str(row.get(key, "")) for key in ("caption", "tags", "source_text")
                ).casefold()
                for term in query_terms
            )
        ][:10]
        if matched:
            message = "\n".join(
                f"#{row.get('id') if archive_is_enabled(config) else row.get('message_id')} · "
                f"{row.get('caption') or row.get('tags') or row.get('source_text') or '无描述'}"
                for row in matched
            )
        else:
            message = "当前聊天没有找到匹配图片。"
        sent = await self._reply(message)
        return (True, message) if sent else (False, "图片搜索结果发送失败")

    @cmd_route("帮助")
    @cmd_route("help")
    async def handle_help(self) -> tuple[bool, str]:
        """显示 /nai 命令帮助。"""

        message = (
            "/nai <自然语言或 Tag>：生成图片\n"
            "/nai 反推：反推回复图片或本聊天最近图片，并引用原图片发送 Tag\n"
            "/nai 历史 [数量]：查看本聊天图片（免保存模式只读核心历史）\n"
            "/nai 查看 <记录ID或消息ID>：重新发送图片\n"
            "/nai 搜索 <关键词>：搜索本聊天图片记录"
        )
        sent = await self._reply(message)
        return (True, message) if sent else (False, "命令帮助发送失败")
