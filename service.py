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
from urllib.parse import quote

import httpx

from .archive import (
    get_image_record,
    list_core_image_messages,
    read_image_file,
    search_local_images,
    update_image_description,
)
from .config import MofoxNaiConfig, archive_is_enabled
from .watermark import apply_watermark


class MofoxNaiService:
    """提供与插件配置绑定的轻量异步服务。"""

    def __init__(self, config: MofoxNaiConfig) -> None:
        """保存本次调用使用的配置快照。"""

        self.config = config

    @staticmethod
    def _headers(api_key: str) -> dict[str, str]:
        """构造可选 Bearer Token 请求头。"""

        return {"Authorization": f"Bearer {api_key}"} if api_key.strip() else {}

    @staticmethod
    def _api_url(base_url: str, suffix: str) -> str:
        """拼接 OpenAI 兼容 API 路径。"""

        return f"{base_url.rstrip('/')}/{suffix.lstrip('/')}"

    @staticmethod
    def _looks_like_tags(prompt: str) -> bool:
        """识别常见的 Danbooru/NAI Tag 格式，避免再次翻译。"""

        lowered = prompt.lower()
        return bool(
            lowered.startswith("tag:")
            or re.search(r"\b\d+(?:girl|boy)s?\b|\b(?:rating|artist|character|copyright):", lowered)
            or re.search(r"\b[a-z0-9]+_[a-z0-9_]+\b", lowered)
        )

    async def prepare_prompt(self, prompt: str) -> str:
        """必要时把自然语言整理为适合生图模型的英文 Tag。"""

        clean_prompt = prompt.strip()
        model = self.config.generation.prompt_model.strip()
        if clean_prompt.lower().startswith("tag:"):
            return clean_prompt[4:].strip()
        if not model or self._looks_like_tags(clean_prompt):
            return clean_prompt
        base_url = self.config.models.base_url.strip()
        if not base_url:
            return clean_prompt

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
                                "Convert the user's visual request into concise English NovelAI/Danbooru-style "
                                "image tags. Preserve the described subject and composition. Do not invent identity "
                                "or details. Return only comma-separated tags."
                            ),
                        },
                        {"role": "user", "content": clean_prompt},
                    ],
                },
            )
            response.raise_for_status()
            data = response.json()
        value = self._message_text(data)
        translated = value.strip().strip("`").removeprefix("tags:").strip()
        return translated or clean_prompt

    async def generate_image(
        self,
        prompt: str,
        negative_prompt: str = "",
        watermark_text: str | None = None,
    ) -> tuple[bytes, str]:
        """调用 OpenAI 兼容图像生成接口并应用可选水印。"""

        generation = self.config.generation
        if not generation.base_url.strip() or not generation.model.strip():
            raise ValueError("请先配置 [generation].base_url 和 [generation].model")
        prepared_prompt = await self.prepare_prompt(prompt)
        body = {
            "model": generation.model,
            "prompt": prepared_prompt,
            "negative_prompt": negative_prompt.strip() or generation.negative_prompt,
            "width": generation.width,
            "height": generation.height,
            "size": f"{generation.width}x{generation.height}",
            "steps": generation.steps,
            "scale": generation.scale,
            "response_format": "b64_json",
        }
        async with httpx.AsyncClient(timeout=generation.timeout) as client:
            response = await client.post(
                self._api_url(generation.base_url, "/images/generations"),
                headers=self._headers(generation.api_key),
                json=body,
            )
            response.raise_for_status()
            result = response.json()
            image_data = await self._extract_generated_image(result, client)
        processed = apply_watermark(image_data, self.config, watermark_text)
        return processed, prepared_prompt

    async def _extract_generated_image(
        self, response_data: dict[str, Any], client: httpx.AsyncClient
    ) -> bytes:
        """兼容 OpenAI b64_json、data URL 和结果 URL。"""

        items = response_data.get("data") or response_data.get("images") or []
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise ValueError("生图接口响应缺少 data[0]")
        item = items[0]
        encoded = item.get("b64_json") or item.get("image")
        if isinstance(encoded, str):
            if encoded.startswith("data:"):
                encoded = encoded.partition(",")[2]
            try:
                image_data = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as error:
                raise ValueError("生图接口返回的 Base64 图片无效") from error
            if not image_data or len(image_data) > self.config.archive.max_image_bytes:
                raise ValueError("生成图片为空或超出 archive.max_image_bytes 限制")
            return image_data
        image_url = item.get("url")
        if isinstance(image_url, str) and image_url.startswith(("https://", "http://")):
            image_response = await client.get(image_url)
            image_response.raise_for_status()
            image_data = image_response.content
            if not image_data or len(image_data) > self.config.archive.max_image_bytes:
                raise ValueError("生成图片为空或超出归档大小限制")
            return image_data
        raise ValueError("生图接口响应中既没有 b64_json，也没有可访问的 url")

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
        update_image_description(self.config, int(record["id"]), caption, tags)
        if self.config.database.qdrant_url.strip():
            refreshed = get_image_record(
                self.config, record_id=int(record["id"]), stream_id=str(record["stream_id"])
            )
            if refreshed:
                await self.index_record(refreshed)
        return tags, caption

    async def reverse_image_bytes(self, image_data: bytes) -> tuple[str, str]:
        """只在内存中分析图片并返回 Tag，不更新本地或远端图库。"""

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
        """查询 Qdrant 语义图库，未配置时退回当前流 SQLite 文本搜索。"""

        qdrant_url = self.config.database.qdrant_url.strip()
        if not qdrant_url:
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
        headers = {"User-Agent": "MoFox-NAI/0.1.1 (plugin; contact repository maintainer)"}
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

