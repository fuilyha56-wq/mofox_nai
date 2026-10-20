"""MoFox NAI 的 /nai 命令与图片历史操作。"""

# Copyright (C) 2026 Lycoris radiata
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import asyncio
import base64
import os
import re
import shlex
from datetime import datetime
from pathlib import Path
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
from .cards import CYAN, GREEN, PINK, RED, render_card
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

    async def _reply_text(self, text: str, reply_to: str | None = None) -> bool:
        """发送必须即时可读的文本状态消息。"""

        return await send_text(
            content=text,
            stream_id=self.stream_id,
            platform=getattr(self._message, "platform", None),
            reply_to=reply_to or self.message_id or None,
        )

    async def _reply(
        self,
        text: str,
        reply_to: str | None = None,
        *,
        title: str = "MoFox NAI",
        status: str = "INFO",
        details: list[tuple[str, str]] | None = None,
        accent: str = PINK,
    ) -> bool:
        """以仪表盘风格的图片卡片回复命令结果。"""

        card = render_card(title, text, status=status, details=details or (), accent=accent)
        return await send_image(
            image_data=base64.b64encode(card).decode("ascii"),
            stream_id=self.stream_id,
            platform=getattr(self._message, "platform", None),
            processed_plain_text=f"[MoFox NAI · {title}]",
            reply_to=reply_to or self.message_id or None,
        )

    @staticmethod
    def _save_config(config: MofoxNaiConfig) -> None:
        """将当前插件配置原子写回 TOML，供参数命令持久生效。"""

        from src.kernel.config.core import _render_toml_with_signature

        path = config.get_default_path() or Path("config/plugins/mofox_nai/config.toml")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        rendered = _render_toml_with_signature(type(config), config.model_dump())
        temporary.write_text(rendered, encoding="utf-8")
        os.replace(temporary, path)

    def _update_config(self, config: MofoxNaiConfig, target: Any, field: str, value: Any) -> None:
        """更新 live 配置并写盘；写盘失败时恢复旧值。"""

        previous = getattr(target, field)
        setattr(target, field, value)
        try:
            self._save_config(config)
        except Exception:
            setattr(target, field, previous)
            raise

    def _is_settings_admin(self, config: MofoxNaiConfig) -> bool:
        """只允许配置中明确列出的用户修改全局生图参数。"""

        sender_id = str(getattr(self._message, "sender_id", "") or "").strip()
        return bool(sender_id and sender_id in {str(value).strip() for value in config.plugin.settings_admin_ids})

    async def _settings_denied(self) -> tuple[bool, str]:
        message = "当前账号没有修改全局生图设置的权限。"
        await self._reply(message, title="权限不足", status="DENIED", accent=RED)
        return False, message

    @cmd_route("设置")
    async def handle_settings(self) -> tuple[bool, str]:
        """查看配置，或实时修改尺寸、采样参数和请求调度。"""

        config = self._config()
        if config is None:
            return False, "mofox_nai 配置尚未加载"
        raw = self._remainder("设置")
        details = [
            ("模型", config.generation.model),
            ("生图协议", config.generation.api_format),
            ("分辨率", f"{config.generation.width} × {config.generation.height}"),
            ("采样步数", str(config.generation.steps)),
            ("CFG", f"{config.generation.scale:g}"),
            ("种子", "随机" if config.generation.seed < 0 else str(config.generation.seed)),
            ("采样器", config.generation.sampler),
            ("噪声调度", config.generation.noise_schedule),
            ("请求模式", "排队" if config.generation.request_mode == "queue" else "并行"),
            ("画风", config.generation.active_style or "默认"),
        ]
        if not raw:
            message = (
                "参数会立即作用于后续请求并写入插件配置。\n"
                "用法：/nai 设置 协议 novelai，或设置 分辨率、宽度、步数、CFG、种子、采样器、噪声调度、并发、参考强度、重绘强度、噪声、负面词。"
            )
            await self._reply(message, title="当前生图设置", status="LIVE", details=details, accent=CYAN)
            return True, message
        if not self._is_settings_admin(config):
            return await self._settings_denied()
        parts = raw.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            message = "用法：/nai 设置 <参数> <值>"
            await self._reply(message, title="设置用法", status="NEED INPUT", accent=CYAN)
            return False, message
        key, value = parts[0].casefold(), parts[1].strip()
        generation = config.generation
        aliases = {
            "api_format": "api_format", "协议": "api_format", "生图协议": "api_format",
            "resolution": "resolution", "分辨率": "resolution", "尺寸": "resolution",
            "width": "width", "宽度": "width", "height": "height", "高度": "height",
            "steps": "steps", "步数": "steps", "scale": "scale", "cfg": "scale", "引导": "scale",
            "sampler": "sampler", "采样器": "sampler", "noise_schedule": "noise_schedule", "噪声调度": "noise_schedule",
            "concurrency": "request_mode", "并发": "request_mode", "队列": "request_mode", "请求模式": "request_mode",
            "参考强度": "reference_strength", "reference_strength": "reference_strength",
            "参考信息": "reference_information", "reference_information": "reference_information",
            "重绘强度": "redraw_strength", "redraw_strength": "redraw_strength",
            "噪声": "redraw_noise", "redraw_noise": "redraw_noise",
            "seed": "seed", "种子": "seed",
            "negative": "negative_prompt", "负面词": "negative_prompt", "负面提示词": "negative_prompt",
        }
        normalized = aliases.get(key)
        if not normalized:
            message = "可设置：协议、分辨率、宽度、高度、步数、CFG、种子、采样器、噪声调度、并发、参考强度、参考信息、重绘强度、噪声、负面词。"
            await self._reply(message, title="未知参数", status="NOT FOUND", accent=RED)
            return False, message
        try:
            if normalized == "resolution":
                match = re.fullmatch(r"\s*(\d{2,4})\s*[x×X]\s*(\d{2,4})\s*", value)
                if not match:
                    raise ValueError("分辨率格式应为 832x1216")
                width, height = int(match.group(1)), int(match.group(2))
                if any(size < 64 or size > 2048 or size % 64 for size in (width, height)):
                    raise ValueError("宽高需为 64 的倍数，范围为 64–2048")
                previous_size = generation.width, generation.height
                generation.width, generation.height = width, height
                try:
                    self._save_config(config)
                except Exception:
                    generation.width, generation.height = previous_size
                    raise
                current = f"{width} × {height}"
            elif normalized in {"width", "height"}:
                size = int(value)
                if size < 64 or size > 2048 or size % 64:
                    raise ValueError("宽高需为 64 的倍数，范围为 64–2048")
                self._update_config(config, generation, normalized, size)
                current = str(size)
            elif normalized == "steps":
                steps = int(value)
                if not 1 <= steps <= 50:
                    raise ValueError("采样步数范围为 1–50")
                self._update_config(config, generation, normalized, steps)
                current = str(steps)
            elif normalized == "seed":
                seed = -1 if value.casefold() in {"random", "随机"} else int(value)
                if seed < -1 or seed > 2147483647:
                    raise ValueError("种子值范围为 -1–2147483647；-1 表示每次随机")
                self._update_config(config, generation, "seed", seed)
                current = "随机" if seed == -1 else str(seed)
            elif normalized in {"scale", "reference_strength", "reference_information", "redraw_strength", "redraw_noise"}:
                number = float(value)
                ceiling = 30 if normalized == "scale" else 1
                if not 0 <= number <= ceiling:
                    raise ValueError(f"{normalized} 范围为 0–{ceiling}")
                self._update_config(config, generation, normalized, number)
                current = f"{number:g}"
            elif normalized == "request_mode":
                mode = {"排队": "queue", "队列": "queue", "queue": "queue", "并行": "parallel", "parallel": "parallel"}.get(value.casefold())
                if mode is None:
                    raise ValueError("并发模式可选：排队 或 并行")
                self._update_config(config, generation, "request_mode", mode)
                current = "排队" if mode == "queue" else "并行"
            elif normalized == "api_format":
                protocol = {
                    "auto": "auto", "自动": "auto",
                    "openai": "openai", "兼容": "openai", "openai兼容": "openai",
                    "novelai": "novelai", "原生": "novelai", "nai原生": "novelai",
                }.get(value.casefold())
                if protocol is None:
                    raise ValueError("生图协议可选：novelai（NAI 原生）、openai（OpenAI 兼容）或 auto")
                self._update_config(config, generation, "api_format", protocol)
                current = protocol
            else:
                if len(value) > 4000:
                    raise ValueError("文本值最多 4000 个字符")
                self._update_config(config, generation, normalized, value)
                current = value[:100]
        except Exception as error:
            await self._reply(f"设置未应用：{error}", title="参数无效", status="ERROR", accent=RED)
            return False, str(error)
        message = f"{key} 已更新，新参数会用于后续请求。"
        await self._reply(
            message,
            title="参数已实时更新",
            status="已更新",
            details=[("已应用值", current), ("请求模式", "排队" if generation.request_mode == "queue" else "并行")],
            accent=GREEN,
        )
        return True, message

    @cmd_route("画风")
    async def handle_style_preset(self) -> tuple[bool, str]:
        """应用、添加、删除或查看可配置的画风预设。"""

        config = self._config()
        if config is None:
            return False, "mofox_nai 配置尚未加载"
        raw = self._remainder("画风").strip()
        presets = list(config.presets.style_presets)
        if not raw:
            details = [(str(item.get("name", "未命名")), str(item.get("prompt", ""))[:120]) for item in presets]
            message = f"当前画风：{config.generation.active_style or '默认'}。使用 /nai 画风 <名称> 切换，添加用 /nai 画风 添加 <名称> <提示词>。"
            await self._reply(message, title="画风预设", status="READY", details=details, accent=CYAN)
            return True, message
        if not self._is_settings_admin(config):
            return await self._settings_denied()
        try:
            parts = shlex.split(raw)
            action = parts[0].casefold()
            if action in {"添加", "add"}:
                if len(parts) < 3:
                    raise ValueError("用法：/nai 画风 添加 <名称> <提示词>")
                name, prompt = parts[1], " ".join(parts[2:])
                presets = [item for item in presets if item.get("name", "").casefold() != name.casefold()]
                presets.append({"name": name, "prompt": prompt})
                self._update_config(config, config.presets, "style_presets", presets)
                message = f"已保存画风预设「{name}」。"
            elif action in {"删除", "移除", "remove"}:
                name = " ".join(parts[1:]).strip()
                presets = [item for item in presets if item.get("name", "").casefold() != name.casefold()]
                self._update_config(config, config.presets, "style_presets", presets)
                if config.generation.active_style.casefold() == name.casefold():
                    self._update_config(config, config.generation, "active_style", "")
                message = f"已删除画风预设「{name}」。"
            elif action in {"清除", "默认", "clear"}:
                self._update_config(config, config.generation, "active_style", "")
                message = "已切回默认画风。"
            else:
                name = raw
                preset = next((item for item in presets if item.get("name", "").casefold() == name.casefold()), None)
                if not preset:
                    raise LookupError(f"没有找到画风预设「{name}」")
                self._update_config(config, config.generation, "active_style", str(preset["name"]))
                message = f"已切换到「{preset['name']}」画风。"
        except Exception as error:
            await self._reply(f"画风预设操作失败：{error}", title="画风预设", status="ERROR", accent=RED)
            return False, str(error)
        await self._reply(message, title="画风已应用", status="已更新", accent=GREEN)
        return True, message

    @cmd_route("模型")
    async def handle_model_preset(self) -> tuple[bool, str]:
        """应用、添加、删除或查看生图模型预设。"""

        config = self._config()
        if config is None:
            return False, "mofox_nai 配置尚未加载"
        raw = self._remainder("模型").strip()
        presets = list(config.presets.model_presets)
        if not raw:
            details = [(str(item.get("name", "未命名")), str(item.get("model", ""))) for item in presets]
            message = f"当前模型：{config.generation.model}。使用 /nai 模型 <名称> 切换，添加用 /nai 模型 添加 <名称> <模型 ID>。"
            await self._reply(message, title="模型预设", status="READY", details=details, accent=CYAN)
            return True, message
        if not self._is_settings_admin(config):
            return await self._settings_denied()
        try:
            parts = shlex.split(raw)
            action = parts[0].casefold()
            if action in {"添加", "add"}:
                if len(parts) < 3:
                    raise ValueError("用法：/nai 模型 添加 <名称> <模型 ID>")
                name, model = parts[1], " ".join(parts[2:])
                presets = [item for item in presets if item.get("name", "").casefold() != name.casefold()]
                presets.append({"name": name, "model": model})
                self._update_config(config, config.presets, "model_presets", presets)
                message = f"已保存模型预设「{name}」。"
            elif action in {"删除", "移除", "remove"}:
                name = " ".join(parts[1:]).strip()
                presets = [item for item in presets if item.get("name", "").casefold() != name.casefold()]
                self._update_config(config, config.presets, "model_presets", presets)
                message = f"已删除模型预设「{name}」。"
            elif action in {"添加", "add"}:
                raise ValueError("用法：/nai 模型 添加 <名称> <模型 ID>")
            else:
                preset = next((item for item in presets if item.get("name", "").casefold() == raw.casefold()), None)
                model = str(preset["model"]) if preset else raw
                if not model.strip():
                    raise ValueError("模型 ID 不能为空")
                self._update_config(config, config.generation, "model", model)
                message = f"当前生图模型已切换为 {model}。"
        except Exception as error:
            await self._reply(f"模型预设操作失败：{error}", title="模型预设", status="ERROR", accent=RED)
            return False, str(error)
        await self._reply(message, title="模型已应用", status="已更新", accent=GREEN)
        return True, message

    async def _generate(self, prompt: str) -> tuple[bool, str]:
        """生成并回复发送图片。"""

        config = self._config()
        if config is None:
            await self._reply("mofox_nai 配置尚未加载。")
            return False, "mofox_nai 配置尚未加载"
        if not prompt.strip():
            help_text = (
                "用法：/nai <中文描述或逗号分隔的 Tag>\n"
                "/nai 参考、重绘、放大：先回复目标图片消息\n"
                "/nai 导演 <req_type> [defry] [提示词]：调用原生 Director Tools\n"
                "/nai 反推：反推被回复图片的 Tag\n"
                "/nai 历史 [数量]：查看本聊天图片记录\n"
                "/nai 设置、画风、模型：查看和修改生图配置\n"
                "/nai 查看 <记录ID或消息ID>：发送图片\n"
                "/nai 搜索 <关键词>：搜索本聊天图片描述和 Tag"
            )
            await self._reply(help_text)
            return True, help_text
        try:
            service = MofoxNaiService(config)
            await self._notify_work(service, "生成图片")
            image_data, final_prompt = await service.generate_image(prompt)
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
            logger.error("/nai 生图失败", exc_info=error)
            await self._reply(f"生图失败：{error}", title="生成失败", status="ERROR", accent=RED)
            return False, str(error)
        return True, f"图片已发送。使用提示词：{final_prompt[:500]}"

    async def _replied_image(self) -> tuple[bytes, str, Any | None, str]:
        """只从当前命令明确引用的消息中读取图片。"""

        config = self._config()
        if config is None:
            raise RuntimeError("mofox_nai 配置尚未加载")
        reply_to = str(getattr(self._message, "reply_to", "") or "").strip()
        if not reply_to:
            raise LookupError("请先回复目标图片或包含图片的消息，再执行此图像操作。")

        scan_limit = 10_000
        for offset in range(0, scan_limit, 1000):
            page = await get_stream_messages(self.stream_id, limit=1000, offset=offset)
            target = next(
                (message for message in page if str(getattr(message, "message_id", "")) == reply_to),
                None,
            )
            if target is not None:
                images = await asyncio.to_thread(
                    message_images, target, max(1024, config.archive.max_image_bytes)
                )
                if images:
                    return images[0][0], reply_to, target, images[0][1]
                break
            if len(page) < 1000:
                break

        if archive_is_enabled(config):
            record = get_image_record(config, stream_id=self.stream_id, message_id=reply_to)
            if record:
                return await asyncio.to_thread(read_image_file, record), reply_to, None, Path(
                    str(record.get("file_path", "image.png"))
                ).suffix or ".png"
        raise LookupError("被引用的消息里没有可读取的图片；请直接回复原图片消息。")

    async def _notify_work(self, service: MofoxNaiService, activity: str) -> None:
        """发送由翻译模型依据当前 Core 人格临时撰写的状态消息。"""

        try:
            notice = await service.in_character_notice(
                queued=service.is_queued_mode() and service.generation_queue_locked(),
                activity=activity,
            )
            await self._reply_text(notice)
        except Exception as notice_error:
            logger.warning("图片任务状态消息生成失败，将继续执行", exc_info=notice_error)

    async def _run_referenced_generation(
        self,
        prompt: str,
        operation: str,
        *,
        mask_image: bytes | None = None,
    ) -> tuple[bool, str]:
        """针对被引用的原图执行参考、重绘或局部重绘。"""

        config = self._config()
        if config is None:
            return False, "mofox_nai 配置尚未加载"
        if not prompt.strip():
            labels = {
                "reference": "/nai 参考 <提示词>",
                "redraw": "/nai 重绘 <提示词>",
                "inpaint": "/nai 局部重绘 <提示词>（并附遮罩图）",
            }
            message = f"用法：{labels[operation]}；请回复目标图片消息。"
            await self._reply(message, title="图像操作", status="NEED INPUT", accent=CYAN)
            return False, message
        service = MofoxNaiService(config)
        try:
            source_image, source_message_id, _, _ = await self._replied_image()
            await self._notify_work(service, {"reference": "参考图生成", "redraw": "图片重绘", "inpaint": "局部重绘"}[operation])
            result, final_prompt = await service.generate_image(
                prompt,
                operation=operation,
                source_image=source_image,
                mask_image=mask_image,
            )
            sent = await send_image(
                image_data=base64.b64encode(result).decode("ascii"),
                stream_id=self.stream_id,
                platform=getattr(self._message, "platform", None),
                processed_plain_text=f"[NAI {operation}: {final_prompt[:120]}]",
                reply_to=source_message_id,
            )
            if not sent:
                raise RuntimeError("平台没有成功发送处理后的图片")
            return True, f"图片处理完成：{final_prompt[:300]}"
        except Exception as error:
            logger.error("NAI 引用图像操作失败", exc_info=error)
            await self._reply(f"图像处理失败：{error}", title="图像处理失败", status="ERROR", accent=RED)
            return False, str(error)

    async def _backfill_history(self, config: MofoxNaiConfig) -> None:
        """将核心聊天数据库中最近消息的媒体补录到插件图片库。"""

        if not archive_is_enabled(config):
            return
        limit = max(1, min(config.archive.history_scan_limit, 1000))
        try:
            messages = await get_stream_messages(self.stream_id, limit=limit)
        except Exception as error:
            logger.error("读取 MoFox 历史消息失败", exc_info=error)
            return
        for message in messages:
            direction = "outgoing" if getattr(message, "sender_role", "") == "bot" else "incoming"
            images = await asyncio.to_thread(
                message_images, message, max(1024, config.archive.max_image_bytes)
            )
            for image_data, extension in images:
                try:
                    await archive_image(config, message, image_data, extension, direction)
                except Exception as error:
                    logger.error("历史消息图片补录失败", exc_info=error)

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

    @cmd_route("参考")
    async def handle_reference(self) -> tuple[bool, str]:
        """引用目标图片做 Vibe Transfer 风格参考。"""

        return await self._run_referenced_generation(self._remainder("参考"), "reference")

    @cmd_route("重绘")
    async def handle_redraw(self) -> tuple[bool, str]:
        """引用目标图片进行 img2img 重绘。"""

        return await self._run_referenced_generation(self._remainder("重绘"), "redraw")

    @cmd_route("局部重绘")
    async def handle_inpaint(self) -> tuple[bool, str]:
        """引用原图并在当前命令消息中附加遮罩图片进行局部重绘。"""

        config = self._config()
        if config is None:
            return False, "mofox_nai 配置尚未加载"
        masks = await asyncio.to_thread(
            message_images,
            self._message,
            max(1024, config.archive.max_image_bytes),
        ) if self._message is not None else []
        if not masks:
            message = "请回复原图，并在 /nai 局部重绘 <提示词> 这条命令消息上附加黑白遮罩图。"
            await self._reply(message, title="局部重绘", status="NEED MASK", accent=CYAN)
            return False, message
        return await self._run_referenced_generation(
            self._remainder("局部重绘"), "inpaint", mask_image=masks[0][0]
        )

    @cmd_route("放大")
    async def handle_upscale(self) -> tuple[bool, str]:
        """调用 NovelAI 原生独立放大接口处理被引用图片。"""

        config = self._config()
        if config is None:
            return False, "mofox_nai 配置尚未加载"
        service = MofoxNaiService(config)
        try:
            image_data, source_message_id, _, _ = await self._replied_image()
            await self._notify_work(service, "图片放大")
            result = await service.upscale_image(image_data)
            sent = await send_image(
                image_data=base64.b64encode(result).decode("ascii"),
                stream_id=self.stream_id,
                platform=getattr(self._message, "platform", None),
                processed_plain_text="[NAI 图片放大]",
                reply_to=source_message_id,
            )
            if not sent:
                raise RuntimeError("平台没有成功发送放大后的图片")
            return True, "被引用图片已放大并发送"
        except Exception as error:
            logger.error("NAI 图片放大失败", exc_info=error)
            await self._reply(f"图片放大失败：{error}", title="图片放大", status="ERROR", accent=RED)
            return False, str(error)

    @cmd_route("导演")
    async def handle_director_tool(self) -> tuple[bool, str]:
        """把被引用图片交给 NAI 原生 Director Tools。"""

        config = self._config()
        if config is None:
            return False, "mofox_nai 配置尚未加载"
        raw = self._remainder("导演").strip()
        if not raw:
            message = "用法：/nai 导演 <req_type> [defry整数] [提示词]，并回复要处理的图片。req_type 使用当前 NAI 服务支持的工具类型。"
            await self._reply(message, title="NAI Director Tools", status="NEED INPUT", accent=CYAN)
            return False, message
        try:
            parts = shlex.split(raw)
            request_type = parts[0]
            defry = 0
            prompt_start = 1
            if len(parts) > 1 and re.fullmatch(r"-?\d+", parts[1]):
                defry = int(parts[1])
                prompt_start = 2
            prompt = " ".join(parts[prompt_start:])
            service = MofoxNaiService(config)
            image_data, source_message_id, _, _ = await self._replied_image()
            await self._notify_work(service, "NAI Director Tools 图片处理")
            result, final_prompt = await service.augment_image(
                image_data=image_data,
                request_type=request_type,
                prompt=prompt,
                defry=defry,
            )
            sent = await send_image(
                image_data=base64.b64encode(result).decode("ascii"),
                stream_id=self.stream_id,
                platform=getattr(self._message, "platform", None),
                processed_plain_text=f"[NAI Director Tools: {request_type}]",
                reply_to=source_message_id,
            )
            if not sent:
                raise RuntimeError("平台没有成功发送 Director Tools 处理后的图片")
            message = f"Director Tools「{request_type}」处理完成。"
            if final_prompt:
                message += f"提示词：{final_prompt[:240]}"
            return True, message
        except Exception as error:
            logger.error("NAI Director Tools 处理失败", exc_info=error)
            await self._reply(f"Director Tools 处理失败：{error}", title="图片处理失败", status="ERROR", accent=RED)
            return False, str(error)

    @cmd_route("描述")
    async def handle_describe(self) -> tuple[bool, str]:
        """调用视觉模型描述当前明确引用的图片。"""

        config = self._config()
        if config is None:
            return False, "mofox_nai 配置尚未加载"
        try:
            image_data, source_message_id, _, _ = await self._replied_image()
            service = MofoxNaiService(config)
            await self._notify_work(service, "读取图片内容")
            description = await service.describe_image(image_data)
            sent = await self._reply(
                description,
                reply_to=source_message_id,
                title="图片内容描述",
                status="ANALYZED",
                accent=CYAN,
            )
            return (True, description) if sent else (False, "图片描述卡片发送失败")
        except Exception as error:
            await self._reply(f"图片描述失败：{error}", title="图片描述", status="ERROR", accent=RED)
            return False, str(error)

    @cmd_route("反推")
    async def handle_reverse(self) -> tuple[bool, str]:
        """反推图片 Tag，并在引用原图片的消息中发送结果。"""

        config = self._config()
        if config is None:
            return False, "mofox_nai 配置尚未加载"
        try:
            service = MofoxNaiService(config)
            image_data, original_message_id, source_message, extension = await self._replied_image()
            await self._notify_work(service, "反推图片标签")
            if archive_is_enabled(config):
                record = get_image_record(
                    config, stream_id=self.stream_id, message_id=original_message_id
                )
                if not record and source_message is not None:
                    record = await archive_image(
                        config, source_message, image_data, extension, "incoming"
                    )
                if record:
                    tags, caption = await service.reverse_record(record, self.stream_id)
                else:
                    tags, caption = await service.reverse_image_bytes(image_data)
            else:
                tags, caption = await service.reverse_image_bytes(image_data)
            content = f"反推 Tag：\n{tags}"
            if caption:
                content += f"\n\n图片描述：{caption}"
            sent = await self._reply(
                content,
                reply_to=original_message_id,
                title="图片 Tag 反推",
                status="完成",
                details=[("Tag", tags[:120]), ("描述", caption[:120] or "未单独返回描述")],
                accent=CYAN,
            )
            return (True, content) if sent else (False, "Tag 已反推，但消息发送失败")
        except Exception as error:
            logger.error("/nai 反推失败", exc_info=error)
            message = f"图片反推失败：{error}"
            await self._reply(message, title="图片反推失败", status="ERROR", accent=RED)
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
            logger.error("归档图片读取失败", exc_info=error)
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
            "/nai 参考、重绘、局部重绘、放大：回复目标图片后处理\n"
            "/nai 导演 <req_type> [defry] [提示词]：调用 NAI 原生 Director Tools\n"
            "/nai 描述：描述回复的图片；/nai 反推：反推回复图片 Tag\n"
            "/nai 历史 [数量]：查看本聊天图片（免保存模式只读核心历史）\n"
            "/nai 查看 <记录ID或消息ID>：重新发送图片\n"
            "/nai 搜索 <关键词>：搜索本聊天图片记录"
        )
        sent = await self._reply(message)
        return (True, message) if sent else (False, "命令帮助发送失败")
