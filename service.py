"""模型 API、图片生成、反推与向量数据库访问逻辑。"""

# Copyright (C) 2026 Lycoris radiata
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import base64
import asyncio
import binascii
import hashlib
import re
from io import BytesIO
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
from zipfile import BadZipFile, ZipFile

import httpx

from .archive import (
    get_image_record,
    list_core_image_messages,
    read_image_file,
    search_local_images,
    update_image_description,
)
from .config import MofoxNaiConfig, archive_is_enabled
from .tag_database import search_sqlite_catalog
from .watermark import apply_watermark
from .wd14 import tag_image as wd14_tag_image


_GENERATION_QUEUE_LOCK = asyncio.Lock()


class MofoxNaiService:
    """提供与插件配置绑定的轻量异步服务。"""

    def __init__(self, config: MofoxNaiConfig) -> None:
        """保存本次调用使用的配置快照。"""

        self.config = config

    @staticmethod
    def _headers(api_key: str) -> dict[str, str]:
        """构造可选 Bearer Token 请求头。"""

        return {"Authorization": f"Bearer {api_key}"} if api_key.strip() else {}

    def is_queued_mode(self) -> bool:
        """返回配置是否启用全局串行生成队列。"""

        return self.config.generation.request_mode == "queue"

    @staticmethod
    def generation_queue_locked() -> bool:
        """返回是否有图片任务正在占用串行队列。"""

        return _GENERATION_QUEUE_LOCK.locked()

    async def in_character_notice(self, queued: bool = False, activity: str = "生成图片") -> str:
        """读取 Core 人格配置，并用提示词模型现写一条生图状态消息。"""

        from src.core.config import get_core_config

        model = self.config.generation.prompt_model.strip() or self.config.models.vision_model.strip()
        base_url = self.config.models.base_url.strip()
        if not model or not base_url:
            raise ValueError("生图状态提示需要配置 [models].base_url 和翻译/视觉模型")
        personality = get_core_config().personality
        identity = "\n".join(
            value
            for value in [
                f"称呼：{personality.nickname}",
                f"身份：{personality.identity}",
                f"核心人格：{personality.personality_core}",
                f"人格补充：{personality.personality_side}",
            ]
            if value.split("：", 1)[1].strip()
        )
        queue_state = "请求已经进入串行生成队列，可能需要等待。" if queued else "现在开始处理生成请求。"
        async with httpx.AsyncClient(timeout=self.config.models.timeout) as client:
            response = await client.post(
                self._api_url(base_url, "/chat/completions"),
                headers=self._headers(self.config.models.api_key),
                json={
                    "model": model,
                    "temperature": 0.8,
                    "max_tokens": 100,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "你现在代表下面配置的人格，在群聊里自然地发一条很短的中文状态消息。"
                                "让用户知道刚收到了一条图片生成请求，并正在处理；若队列状态说明正在排队。"
                                "遵循人格语气，像自然聊天，不要解释、不复述提示词、不添加引号、标签或换行。"
                                "请现场组织措辞，不要套用固定模板。\n\n"
                                f"人格配置：\n{identity}\n\n当前状态：{queue_state}"
                                f"\n当前任务：{activity}"
                            ),
                        },
                        {"role": "user", "content": "请写这条状态消息。"},
                    ],
                },
            )
            response.raise_for_status()
            result = response.json()
        value = self._message_text(result).strip().strip("\"'“”‘’` ")
        value = " ".join(value.split())
        if not value:
            raise ValueError("状态提示模型没有生成消息")
        return value[:120]

    def _active_style_prompt(self) -> str:
        """读取已选画风预设的补充提示词。"""

        active_name = self.config.generation.active_style.strip().casefold()
        if not active_name:
            return ""
        for preset in self.config.presets.style_presets:
            if str(preset.get("name", "")).strip().casefold() == active_name:
                return str(preset.get("prompt", "")).strip()
        return ""

    @staticmethod
    def _api_url(base_url: str, suffix: str) -> str:
        """拼接 API 路径，同时保留根地址上的前缀与查询参数。"""

        parts = urlsplit(base_url.strip())
        base_path = parts.path.rstrip("/")
        suffix_path = "/" + suffix.strip("/")
        if base_path.casefold().endswith(suffix_path.casefold()):
            path = base_path
        else:
            path = f"{base_path}{suffix_path}"
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))

    def _generation_api_format(self) -> str:
        """解析生图协议；NAI 原生端点或裸 NAI 模型地址自动使用原生请求体。"""

        generation = self.config.generation
        configured = generation.api_format
        if configured != "auto":
            return configured
        base = generation.base_url.strip().casefold()
        path = urlsplit(base).path.rstrip("/")
        if path.endswith("/ai/generate-image"):
            return "novelai"
        has_openai_version = bool(re.search(r"/v\d+(?:/|$)", path))
        is_nai_model = generation.model.strip().casefold().startswith("nai-diffusion-")
        return "novelai" if is_nai_model and not has_openai_version else "openai"

    def supports_precise_reference(self) -> bool:
        """Precise Reference is only available on NovelAI Diffusion V4.5 models."""

        return bool(re.search(r"(?:4[._ -]?5|v4[._ -]?5)", self.config.generation.model, re.IGNORECASE))

    @staticmethod
    def _native_image_request(
        model: str,
        prompt: str,
        negative_prompt: str,
        width: int,
        height: int,
        steps: int,
        scale: float,
        sampler: str = "k_euler_ancestral",
        noise_schedule: str = "karras",
        operation: str = "generate",
        source_image: bytes | None = None,
        mask_image: bytes | None = None,
        reference_strength: float = 0.6,
        reference_information: float = 0.6,
        redraw_strength: float = 0.65,
        redraw_noise: float = 0.2,
        reference_images: list[bytes] | None = None,
        precise_reference_type: str = "character",
        precise_reference_strength: float = 1.0,
        precise_reference_fidelity: float = 1.0,
        seed: int = -1,
    ) -> dict[str, Any]:
        """构造 NovelAI 图像 API 使用的原生请求结构。"""

        parameters: dict[str, Any] = {
            "params_version": 3,
            "width": width,
            "height": height,
            "steps": steps,
            "scale": scale,
            "sampler": sampler,
            "noise_schedule": noise_schedule,
            "n_samples": 1,
            "ucPreset": 0,
            "qualityToggle": True,
            "sm": False,
            "sm_dyn": False,
            "dynamic_thresholding": False,
            "cfg_rescale": 0,
            "legacy": False,
            "negative_prompt": negative_prompt,
            "prompt": prompt,
        }
        if seed >= 0:
            parameters["seed"] = seed
        if operation == "reference":
            references = reference_images or ([source_image] if source_image is not None else [])
            if not references:
                raise ValueError("Vibe Transfer 需要至少一张明确引用的图片")
            if len(references) > 16:
                raise ValueError("NovelAI Vibe Transfer 一次最多支持 16 张参考图")
            encoded_references = [base64.b64encode(image).decode("ascii") for image in references]
            if len(encoded_references) == 1:
                parameters.update(
                    {
                        "reference_image": encoded_references[0],
                        "reference_strength": reference_strength,
                        "reference_information_extracted": reference_information,
                    }
                )
            else:
                parameters.update(
                    {
                        "reference_image_multiple": encoded_references,
                        "reference_strength_multiple": [reference_strength] * len(references),
                        "reference_information_extracted_multiple": [reference_information] * len(references),
                    }
                )
        elif operation == "precise_reference":
            references = reference_images or ([source_image] if source_image is not None else [])
            if not references:
                raise ValueError("Precise Reference 需要至少一张明确引用的图片")
            if precise_reference_type not in {"character", "style", "character&style"}:
                raise ValueError("精准参考类型必须为 character、style 或 character&style")
            if not 0 <= precise_reference_strength <= 1 or not 0 <= precise_reference_fidelity <= 1:
                raise ValueError("精准参考强度与忠实度必须在 0–1 之间")
            parameters.update(
                {
                    "director_reference_images": [
                        base64.b64encode(image).decode("ascii") for image in references
                    ],
                    "director_reference_descriptions": [
                        {
                            "caption": {
                                "base_caption": precise_reference_type,
                                "char_captions": [],
                            }
                        }
                        for _ in references
                    ],
                    "director_reference_information_extracted": [1.0] * len(references),
                    "director_reference_strength_values": [
                        precise_reference_strength
                    ] * len(references),
                    "director_reference_secondary_strength_values": [
                        precise_reference_fidelity
                    ] * len(references),
                }
            )
        elif operation in {"redraw", "inpaint"}:
            if source_image is None:
                raise ValueError(f"{operation} 操作需要引用图片")
            encoded_image = base64.b64encode(source_image).decode("ascii")
            parameters.update(
                {
                    "image": encoded_image,
                    "strength": redraw_strength,
                    "noise": redraw_noise,
                    "img2img": {
                        "strength": redraw_strength,
                        "noise": redraw_noise,
                        "color_correct": True,
                    },
                }
            )
            if operation == "inpaint":
                if mask_image is None:
                    raise ValueError("局部重绘需要在命令消息中附加遮罩图片")
                parameters["mask"] = base64.b64encode(mask_image).decode("ascii")
        elif operation != "generate":
            raise ValueError(f"不支持的原生图片操作：{operation}")
        if re.search(r"nai[-_ ]?diffusion[-_ ]?(?:4(?:[-_. ]?5)?|5)", model, re.IGNORECASE):
            parameters["v4_prompt"] = {
                "caption": {"base_caption": prompt, "char_captions": []},
                "use_coords": False,
                "use_order": False,
                "legacy_uc": False,
            }
            parameters["v4_negative_prompt"] = {
                "caption": {"base_caption": negative_prompt, "char_captions": []},
                "use_coords": False,
                "use_order": False,
                "legacy_uc": False,
            }
        return {
            "action": "generate",
            "input": prompt,
            "model": model,
            "parameters": parameters,
        }

    @staticmethod
    def _looks_like_tags(prompt: str) -> bool:
        """识别常见的 Danbooru/NAI Tag 格式，避免再次翻译。"""

        lowered = prompt.lower()
        return bool(
            lowered.startswith("tag:")
            or re.search(r"\b\d+(?:girl|boy)s?\b|\b(?:rating|artist|character|copyright):", lowered)
            or re.search(r"\b[a-z0-9]+_[a-z0-9_]+\b", lowered)
        )

    def _translation_format(self) -> str:
        """按 NAI 模型与配置选择提示词整理格式。"""

        model_name = self.config.generation.model.casefold()
        is_nai_45 = bool(re.search(r"(?:4[._ -]?5|v4[._ -]?5)", model_name))
        if is_nai_45:
            return "tags"

        configured = self.config.generation.prompt_format
        if configured != "auto":
            return configured

        is_nai_v5 = bool(
            re.search(
                r"(?:nai[-_ ]?diffusion[-_ ]?v?5|(?:^|[-_ ])v?5(?:$|[-_ ])|5-full)",
                model_name,
            )
        )
        return "v5_choice" if is_nai_v5 else "tags"

    def _with_active_style(self, prompt: str) -> str:
        """将当前画风预设追加到最终提示词。"""

        style_prompt = self._active_style_prompt()
        return f"{prompt}, {style_prompt}" if style_prompt else prompt

    async def prepare_prompt(self, prompt: str) -> str:
        """保留直接输入的 Tag，将自然语言翻译为当前 NAI 模型适用的提示词。"""

        clean_prompt = prompt.strip()
        model = (
            self.config.generation.prompt_model.strip()
            or self.config.models.vision_model.strip()
        )
        if clean_prompt.lower().startswith("tag:"):
            return self._with_active_style(clean_prompt[4:].strip())
        if self._looks_like_tags(clean_prompt):
            return self._with_active_style(clean_prompt)
        if not model:
            raise ValueError(
                "自然语言生图需要配置 [generation].prompt_model 或 [models].vision_model，以及 [models].base_url；"
                "直接输入 Danbooru/NAI Tag 不需要翻译模型。"
            )
        base_url = self.config.models.base_url.strip()
        if not base_url:
            raise ValueError("自然语言生图需要配置 [models].base_url")

        output_format = self._translation_format()
        if output_format == "tags":
            format_instruction = (
                "Return only concise comma-separated English NovelAI/Danbooru-style tags. "
                "Never return prose."
            )
        elif output_format == "natural_language":
            format_instruction = (
                "Return only one concise, fluent English natural-language image description. "
                "Do not return a tag list."
            )
        else:
            format_instruction = (
                "This is an NAI V5 model. Choose whichever representation best preserves the request: "
                "either a concise English natural-language image description or concise comma-separated "
                "English NovelAI/Danbooru-style tags. Return only the chosen prompt, with no label or explanation."
            )

        headers = self._headers(self.config.models.api_key)
        async with httpx.AsyncClient(timeout=self.config.models.timeout) as client:
            response = await client.post(
                self._api_url(base_url, "/chat/completions"),
                headers=headers,
                json={
                    "model": model,
                    "temperature": 0.2,
                    "max_tokens": 600,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Translate the user's visual request into a prompt suitable for the configured "
                                f"image model ({self.config.generation.model}). {format_instruction} "
                                "Preserve the described subject, composition, style, and requested details. "
                                "Do not invent identities or visual details."
                            ),
                        },
                        {"role": "user", "content": clean_prompt},
                    ],
                },
            )
            response.raise_for_status()
            data = response.json()
        value = self._message_text(data)
        translated = value.strip().strip("`").strip()
        translated = re.sub(r"^(?:tags?|prompt)\s*:\s*", "", translated, flags=re.IGNORECASE)
        if not translated:
            raise ValueError("提示词翻译模型没有返回可用提示词")
        return self._with_active_style(translated)

    async def generate_image(
        self,
        prompt: str,
        negative_prompt: str = "",
        watermark_text: str | None = None,
        operation: str = "generate",
        source_image: bytes | None = None,
        mask_image: bytes | None = None,
        reference_strength: float | None = None,
        reference_information: float | None = None,
        redraw_strength: float | None = None,
        redraw_noise: float | None = None,
        reference_images: list[bytes] | None = None,
        precise_reference_type: str = "character",
    ) -> tuple[bytes, str]:
        """调用选定的生图协议并应用可选水印。"""

        generation = self.config.generation
        if not generation.base_url.strip() or not generation.model.strip():
            raise ValueError("请先配置 [generation].base_url 和 [generation].model")
        prepared_prompt = await self.prepare_prompt(prompt)
        final_negative_prompt = negative_prompt.strip() or generation.negative_prompt
        api_format = self._generation_api_format()
        if operation != "generate" and api_format != "novelai":
            raise ValueError("参考图、重绘和局部重绘需要 [generation].api_format = 'novelai'")
        if operation == "precise_reference" and not self.supports_precise_reference():
            raise ValueError("NAI Precise Reference 仅支持 NAI 4.5 模型；Vibe Transfer 可使用 /nai 参考")
        if api_format == "novelai":
            endpoint = self._api_url(generation.base_url, "/ai/generate-image")
            headers = {
                **self._headers(generation.api_key),
                "Accept": "application/json",
            }
            body = self._native_image_request(
                model=generation.model,
                prompt=prepared_prompt,
                negative_prompt=final_negative_prompt,
                width=generation.width,
                height=generation.height,
                steps=generation.steps,
                scale=generation.scale,
                sampler=generation.sampler,
                noise_schedule=generation.noise_schedule,
                operation=operation,
                source_image=source_image,
                mask_image=mask_image,
                reference_strength=(
                    generation.reference_strength if reference_strength is None else reference_strength
                ),
                reference_information=(
                    generation.reference_information
                    if reference_information is None
                    else reference_information
                ),
                redraw_strength=(
                    generation.redraw_strength if redraw_strength is None else redraw_strength
                ),
                redraw_noise=generation.redraw_noise if redraw_noise is None else redraw_noise,
                reference_images=reference_images,
                precise_reference_type=precise_reference_type,
                precise_reference_strength=generation.precise_reference_strength,
                precise_reference_fidelity=generation.precise_reference_fidelity,
                seed=generation.seed,
            )
        else:
            endpoint = self._api_url(generation.base_url, "/images/generations")
            headers = self._headers(generation.api_key)
            body = {
                "model": generation.model,
                "prompt": prepared_prompt,
                "negative_prompt": final_negative_prompt,
                "width": generation.width,
                "height": generation.height,
                "size": f"{generation.width}x{generation.height}",
                "steps": generation.steps,
                "scale": generation.scale,
                "sampler": generation.sampler,
                "noise_schedule": generation.noise_schedule,
                "response_format": "b64_json",
            }
            if generation.seed >= 0:
                body["seed"] = generation.seed
        async def send_request() -> bytes:
            async with httpx.AsyncClient(timeout=generation.timeout) as client:
                response = await client.post(endpoint, headers=headers, json=body)
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as error:
                    if response.status_code == 404 and api_format == "novelai":
                        raise ValueError(
                            "NAI 原生生图端点返回 404。插件已发送 NovelAI 原生格式，请检查 base_url 是否为 API 根地址，"
                            "并确认服务提供 /ai/generate-image；若刚更新插件，请重启 MoFox 后重试。"
                        ) from error
                    if response.status_code == 404 and api_format == "openai":
                        raise ValueError(
                            "OpenAI 兼容生图端点返回 404。若服务使用 NAI 原生接口，请执行 /nai 设置 协议 novelai，"
                            "或在配置中设置 api_format = \"novelai\" 后重启 MoFox。"
                        ) from error
                    detail = " ".join(str(getattr(response, "text", "") or "").split())[:1200]
                    if api_format == "novelai":
                        reason = str(getattr(response, "reason_phrase", "") or "").strip()
                        status = f"HTTP {response.status_code} {reason}".strip()
                        message = f"NovelAI 原生生图接口返回 {status}"
                        if response.status_code == 502:
                            message += "；网关未能从后端取得有效响应，请检查代理上游和生图服务状态"
                        if detail:
                            message += f"。服务端响应：{detail}"
                        raise ValueError(message) from error
                    raise
                if api_format == "novelai":
                    return await self._extract_native_image(response, client)
                return await self._extract_generated_image(response.json(), client)

        if self.is_queued_mode():
            async with _GENERATION_QUEUE_LOCK:
                image_data = await send_request()
        else:
            image_data = await send_request()
        processed = apply_watermark(image_data, self.config, watermark_text)
        return processed, prepared_prompt

    async def upscale_image(self, image_data: bytes) -> bytes:
        """使用 NAI 原生独立放大端点处理被引用图片。"""

        generation = self.config.generation
        if self._generation_api_format() != "novelai":
            raise ValueError("NAI 放大需要 [generation].api_format = 'novelai'")
        if not image_data:
            raise ValueError("没有可放大的引用图片")
        endpoint = self._api_url(generation.base_url, "/ai/upscale")
        headers = {**self._headers(generation.api_key), "Accept": "application/json"}
        body = {
            "image": base64.b64encode(image_data).decode("ascii"),
            "model": generation.model,
        }

        async def send_request() -> bytes:
            async with httpx.AsyncClient(timeout=generation.timeout) as client:
                response = await client.post(endpoint, headers=headers, json=body)
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as error:
                    if response.status_code == 404:
                        raise ValueError(
                            "NAI 原生放大端点返回 404；请检查 base_url，并确认服务提供 /ai/upscale。"
                        ) from error
                    raise
                return await self._extract_native_image(response, client)

        if self.is_queued_mode():
            async with _GENERATION_QUEUE_LOCK:
                result = await send_request()
        else:
            result = await send_request()
        return apply_watermark(result, self.config)

    async def augment_image(
        self,
        image_data: bytes,
        request_type: str,
        prompt: str = "",
        defry: int = 0,
    ) -> tuple[bytes, str]:
        """使用 NAI 原生 Director Tools 端点处理被引用图片。"""

        generation = self.config.generation
        if self._generation_api_format() != "novelai":
            raise ValueError("NAI Director Tools 需要 [generation].api_format = 'novelai'")
        if not image_data:
            raise ValueError("没有可处理的引用图片")
        clean_type = request_type.strip()
        if (
            not clean_type
            or len(clean_type) > 80
            or any(ord(character) < 32 or ord(character) == 127 for character in clean_type)
        ):
            raise ValueError("req_type 不能为空、不能超过 80 个字符，也不能包含控制字符")
        final_prompt = await self.prepare_prompt(prompt) if prompt.strip() else ""
        endpoint = self._api_url(generation.base_url, "/ai/augment-image")
        headers = {**self._headers(generation.api_key), "Accept": "application/zip"}
        body: dict[str, Any] = {
            "image": base64.b64encode(image_data).decode("ascii"),
            "req_type": clean_type,
            "width": generation.width,
            "height": generation.height,
        }
        if final_prompt:
            body["prompt"] = final_prompt
        if defry:
            body["defry"] = defry

        async def send_request() -> bytes:
            async with httpx.AsyncClient(timeout=generation.timeout) as client:
                response = await client.post(endpoint, headers=headers, json=body)
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as error:
                    if response.status_code == 404:
                        raise ValueError(
                            "NAI Director Tools 端点返回 404；请检查 base_url，并确认服务提供 /ai/augment-image。"
                        ) from error
                    raise
                return await self._extract_native_image(response, client)

        if self.is_queued_mode():
            async with _GENERATION_QUEUE_LOCK:
                result = await send_request()
        else:
            result = await send_request()
        return apply_watermark(result, self.config), final_prompt

    async def _extract_native_image(self, response: httpx.Response, client: httpx.AsyncClient) -> bytes:
        """读取 NovelAI 原生接口的 JSON、ZIP 或直接图片响应。"""

        content = response.content
        content_type = response.headers.get("content-type", "").casefold()
        if "zip" in content_type or content.startswith(b"PK\x03\x04"):
            return self._extract_zip_image(content)
        is_webp = content.startswith(b"RIFF") and content[8:12] == b"WEBP"
        if content_type.startswith("image/") or content.startswith(
            (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")
        ) or is_webp:
            return self._validate_image_bytes(content)
        return await self._extract_generated_image(response.json(), client)

    def _validate_image_bytes(self, image_data: bytes) -> bytes:
        """检查响应图片非空且在插件配置的大小限制内。"""

        if not image_data or len(image_data) > self.config.archive.max_image_bytes:
            raise ValueError("生成图片为空或超出 archive.max_image_bytes 限制")
        return image_data

    def _extract_zip_image(self, content: bytes) -> bytes:
        """从 NovelAI ZIP 响应中安全读取第一张图片，不解压到磁盘。"""

        try:
            with ZipFile(BytesIO(content)) as archive:
                members = [name for name in archive.namelist() if not name.endswith("/")]
                image_names = [
                    name for name in members if name.casefold().endswith((".png", ".webp", ".jpg", ".jpeg"))
                ]
                if not image_names:
                    image_names = members[:1]
                if not image_names:
                    raise ValueError("NovelAI ZIP 响应中没有图片文件")
                with archive.open(image_names[0]) as image_file:
                    image_data = image_file.read(self.config.archive.max_image_bytes + 1)
        except BadZipFile as error:
            raise ValueError("NovelAI 返回了无效的 ZIP 图片数据") from error
        return self._validate_image_bytes(image_data)

    async def _extract_generated_image(
        self, response_data: dict[str, Any], client: httpx.AsyncClient
    ) -> bytes:
        """兼容 OpenAI b64_json 与 NovelAI images[].image Base64 响应。"""

        items = response_data.get("data") or response_data.get("images") or []
        if not isinstance(items, list) or not items:
            raise ValueError("生图接口响应缺少 data[0] 或 images[0]")
        if isinstance(items[0], str):
            items = [{"image": items[0]}]
        if not isinstance(items[0], dict):
            raise ValueError("生图接口响应中的图片条目格式无效")
        item = items[0]
        encoded = item.get("b64_json") or item.get("image")
        if isinstance(encoded, str):
            if encoded.startswith("data:"):
                encoded = encoded.partition(",")[2]
            try:
                image_data = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as error:
                raise ValueError("生图接口返回的 Base64 图片无效") from error
            return self._validate_image_bytes(image_data)
        image_url = item.get("url")
        if isinstance(image_url, str) and image_url.startswith(("https://", "http://")):
            image_response = await client.get(image_url)
            image_response.raise_for_status()
            image_data = image_response.content
            return self._validate_image_bytes(image_data)
        raise ValueError("生图接口响应中既没有图片 Base64，也没有可访问的 url")

    @staticmethod
    def _message_text(response_data: dict[str, Any]) -> str:
        """从 OpenAI 兼容 Chat Completion 中读取文本。"""

        try:
            content = response_data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") in {"text", "output_text"}
            )
        return ""

    @staticmethod
    def _image_data_url(image_data: bytes) -> str:
        """为视觉模型构造 MIME 正确的图片 Data URL。"""

        from PIL import Image

        with Image.open(BytesIO(image_data)) as image:
            image_format = (image.format or "PNG").lower()
        mime_type = {"jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(
            image_format, f"image/{image_format}"
        )
        return f"data:{mime_type};base64,{base64.b64encode(image_data).decode('ascii')}"

    async def _vision_request(self, image_data: bytes, instruction: str) -> str:
        """向配置的视觉模型发送单图请求。"""

        model = self.config.models.vision_model.strip()
        base_url = self.config.models.base_url.strip()
        if not model or not base_url:
            raise ValueError("请配置 [models].vision_model 和 [models].base_url")
        payload = {
            "model": model,
            "temperature": 0.2,
            "max_tokens": 1200,
            "messages": [
                {"role": "system", "content": instruction},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请只分析这张图片。"},
                        {"type": "image_url", "image_url": {"url": self._image_data_url(image_data)}},
                    ],
                },
            ],
        }
        async with httpx.AsyncClient(timeout=self.config.models.timeout) as client:
            response = await client.post(
                self._api_url(base_url, "/chat/completions"),
                headers=self._headers(self.config.models.api_key),
                json=payload,
            )
            response.raise_for_status()
            result = response.json()
        content = self._message_text(result).strip()
        if not content:
            raise ValueError("视觉模型没有返回文本")
        return content

    async def reverse_tags(self, record: dict[str, Any]) -> tuple[str, str]:
        """为归档图片生成简洁的可编辑 Tag 列表与描述。"""

        image_data = await asyncio.to_thread(read_image_file, record)
        tags, caption = await self.reverse_image_bytes(image_data)
        stored_caption = caption or str(record.get("caption", ""))
        update_image_description(self.config, int(record["id"]), stored_caption, tags)
        if self.config.database.qdrant_url.strip():
            refreshed = get_image_record(
                self.config, record_id=int(record["id"]), stream_id=str(record["stream_id"])
            )
            if refreshed:
                await self.index_record(refreshed)
        return tags, caption

    async def reverse_image_bytes(self, image_data: bytes) -> tuple[str, str]:
        """只在内存中分析图片并返回 Tag，不更新本地或远端图库。"""

        if self.config.models.reverse_backend == "wd14":
            tags = await asyncio.to_thread(
                wd14_tag_image,
                image_data,
                self.config.models.wd14_model_path,
                self.config.models.wd14_tags_path,
                self.config.models.wd14_general_threshold,
                self.config.models.wd14_character_threshold,
            )
            return tags, ""

        response = await self._vision_request(
            image_data,
            "You are an image tagging assistant. Describe only visible content. "
            "Return two lines exactly: 'Tags: ' followed by concise comma-separated English "
            "Danbooru-style tags; then 'Caption: ' followed by one short Chinese sentence. "
            "Do not guess real identities, hidden details, or an exact artist.",
        )
        return self._parse_reverse_result(response)

    @staticmethod
    def _parse_reverse_result(value: str) -> tuple[str, str]:
        """从模型输出中提取 Tags 与 Caption 行。"""

        tags = ""
        caption = ""
        for line in value.splitlines():
            normalized = line.strip().strip("`*- ")
            if normalized.lower().startswith("tags:"):
                tags = normalized.split(":", 1)[1].strip()
            elif normalized.lower().startswith("caption:"):
                caption = normalized.split(":", 1)[1].strip()
        if not tags:
            tags = value.strip().strip("`")
        tags = re.sub(r"\s+", " ", tags).strip(" ,")[:3000]
        return tags, caption[:1000]

    async def describe_image(self, image_data: bytes) -> str:
        """生成仅包含可见内容的短中文图片描述。"""

        return await self._vision_request(
            image_data,
            "请用简洁中文描述图片中可见的主体、动作、场景、构图和主要颜色。"
            "不要推测身份或图片中没有明确显示的信息，只返回描述。",
        )

    async def embedding(self, text: str) -> list[float]:
        """从配置的 OpenAI 兼容接口获取文本向量。"""

        model = self.config.models.embedding_model.strip()
        base_url = (self.config.models.embedding_base_url or self.config.models.base_url).strip()
        api_key = self.config.models.embedding_api_key or self.config.models.api_key
        if not model or not base_url:
            raise ValueError("Qdrant 检索需要配置 [models].embedding_model 和 Embeddings API 地址")
        async with httpx.AsyncClient(timeout=self.config.models.timeout) as client:
            response = await client.post(
                self._api_url(base_url, "/embeddings"),
                headers=self._headers(api_key),
                json={"model": model, "input": text[:4000]},
            )
            response.raise_for_status()
            result = response.json()
        vector = result.get("data", [{}])[0].get("embedding")
        if not isinstance(vector, list) or not vector:
            raise ValueError("嵌入模型响应中没有 embedding 向量")
        return [float(value) for value in vector]

    async def index_record(self, record: dict[str, Any]) -> bool:
        """将一条带描述的归档图片写入已存在的 Qdrant Collection。"""

        if not self.config.database.qdrant_url.strip():
            return False
        source_text = str(record.get("source_text", "")).strip()
        if re.fullmatch(r"\[(?:图片|表情包)(?:\([^)]*\))?(?::[^\]]*)?\]", source_text):
            source_text = ""
        searchable_text = "\n".join(
            value
            for value in [str(record.get("caption", "")), str(record.get("tags", "")), source_text]
            if value.strip()
        ).strip()
        if not searchable_text:
            return False
        vector = await self.embedding(searchable_text)
        payload = {
            "record_id": int(record["id"]),
            "message_id": str(record.get("message_id", "")),
            "stream_id": str(record.get("stream_id", "")),
            "platform": str(record.get("platform", "")),
            "sender_name": str(record.get("sender_name", "")),
            "created_at": float(record.get("created_at", 0)),
            "caption": str(record.get("caption", "")),
            "tags": str(record.get("tags", "")),
            "source_text": str(record.get("source_text", "")),
        }
        point_id = int(
            hashlib.sha256(
                f"{payload['stream_id']}:{payload['message_id']}:{record['media_hash']}".encode()
            ).hexdigest()[:15],
            16,
        )
        url = self._qdrant_points_url()
        headers = self._qdrant_headers()
        async with httpx.AsyncClient(timeout=self.config.models.timeout) as client:
            response = await client.put(
                url,
                headers=headers,
                json={"points": [{"id": point_id, "vector": vector, "payload": payload}]},
            )
            response.raise_for_status()
        return True

    def _qdrant_headers(self) -> dict[str, str]:
        """返回 Qdrant 可选 API Key 请求头。"""

        key = self.config.database.qdrant_api_key.strip()
        return {"api-key": key} if key else {}

    def _qdrant_points_url(self) -> str:
        """返回当前 Collection 的 points API 地址。"""

        base = self.config.database.qdrant_url.rstrip("/")
        collection = quote(self.config.database.collection.strip(), safe="")
        if not collection:
            raise ValueError("[database].collection 不能为空")
        return f"{base}/collections/{collection}/points"

    async def search_database(
        self, query: str, stream_id: str, limit: int
    ) -> list[dict[str, Any]]:
        """查询 Qdrant、外部 Tag SQLite，或当前流图片历史。"""

        qdrant_url = self.config.database.qdrant_url.strip()
        if not qdrant_url:
            sqlite_path = self.config.database.sqlite_path.strip()
            if sqlite_path:
                return await search_sqlite_catalog(sqlite_path, query, limit)
            if archive_is_enabled(self.config):
                return search_local_images(self.config, stream_id, query, limit)
            rows = await list_core_image_messages(
                stream_id,
                limit=self.config.archive.history_scan_limit,
                max_bytes=self.config.archive.max_image_bytes,
            )
            terms = [part.casefold() for part in query.split() if part]
            return [
                row
                for row in rows
                if any(term in row["source_text"].casefold() for term in terms)
            ][: max(1, min(limit, 20))]
        vector = await self.embedding(query)
        body: dict[str, Any] = {
            "query": vector,
            "limit": max(1, min(limit, 10)),
            "with_payload": True,
        }
        if self.config.database.scope_to_stream:
            if not stream_id:
                return []
            body["filter"] = {"must": [{"key": "stream_id", "match": {"value": stream_id}}]}
        async with httpx.AsyncClient(timeout=self.config.models.timeout) as client:
            response = await client.post(
                f"{self._qdrant_points_url()}/query",
                headers=self._qdrant_headers(),
                json=body,
            )
            response.raise_for_status()
            result = response.json().get("result", {})
        points = result.get("points", []) if isinstance(result, dict) else result
        if not isinstance(points, list):
            return []
        hits: list[dict[str, Any]] = []
        for point in points:
            if not isinstance(point, dict):
                continue
            payload = point.get("payload") or {}
            hits.append(
                {
                    "score": point.get("score"),
                    "record_id": payload.get("record_id"),
                    "message_id": payload.get("message_id", ""),
                    "stream_id": payload.get("stream_id", ""),
                    "caption": payload.get("caption", ""),
                    "tags": payload.get("tags", ""),
                    "source_text": payload.get("source_text", ""),
                }
            )
        return hits

    async def search_danbooru(self, tags: str, limit: int) -> list[dict[str, Any]]:
        """使用 Danbooru Posts API 查询安全标签结果。"""

        options = self.config.danbooru
        if not options.enabled:
            raise ValueError("Danbooru 查询已在配置中关闭")
        query_tags = re.sub(r"[\r\n\x00-\x1f]", " ", tags).strip()[:500]
        if not query_tags:
            raise ValueError("请提供至少一个 Danbooru Tag")
        if options.safe_only:
            query_tags = re.sub(r"(?:^|\s)rating:[a-z](?=$|\s)", " ", query_tags)
            query_tags = f"{query_tags.strip()} rating:s"
        auth = None
        if options.username.strip() and options.api_key.strip():
            auth = (options.username.strip(), options.api_key.strip())
        base_url = options.base_url.rstrip("/")
        headers = {"User-Agent": "MoFox-NAI/0.1.4 (plugin; contact repository maintainer)"}
        async with httpx.AsyncClient(timeout=self.config.models.timeout) as client:
            response = await client.get(
                f"{base_url}/posts.json",
                params={"tags": query_tags, "limit": max(1, min(limit, 10))},
                auth=auth,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, list):
            raise ValueError("Danbooru 返回了无法识别的数据")
        return [
            {
                "id": post.get("id"),
                "rating": post.get("rating"),
                "tags": str(post.get("tag_string", ""))[:1200],
                "page_url": post.get("id") and f"{base_url}/posts/{post['id']}",
                "preview_url": post.get("large_file_url") or post.get("file_url") or post.get("preview_file_url"),
                "source": post.get("source"),
            }
            for post in data
            if isinstance(post, dict)
        ]

    async def reverse_record(
        self, record: dict[str, Any], stream_id: str
    ) -> tuple[str, str]:
        """为指定记录反推 Tag；此方法用于保持工具侧流范围校验。"""

        if str(record.get("stream_id", "")) != stream_id:
            raise ValueError("图片记录不属于当前聊天")
        return await self.reverse_tags(record)

