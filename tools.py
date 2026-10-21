"""供 LLM 查询当前聊天图库、配置的向量库和 Danbooru 的工具。"""

# Copyright (C) 2026 Lycoris radiata
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from typing import Annotated

from src.app.plugin_system.base import BaseTool
from src.kernel.logger import get_logger

from .access import chat_access
from .archive import list_core_image_messages, search_local_images
from .config import MofoxNaiConfig, archive_is_enabled
from .service import MofoxNaiService

logger = get_logger("mofox_nai.tools")


def _config(plugin: object) -> MofoxNaiConfig | None:
    """返回当前插件配置。"""

    value = getattr(plugin, "config", None)
    return value if isinstance(value, MofoxNaiConfig) else None


def _access_error(tool: BaseTool, config: MofoxNaiConfig) -> str | None:
    """检查当前工具调用是否来自允许的群聊或私聊。"""

    allowed, reason = chat_access(config, getattr(tool, "trigger_message", None))
    return None if allowed else reason


class SearchImageHistoryTool(BaseTool):
    """按描述或 Tag 搜索当前聊天中的图片记录或核心历史。"""

    name = "search_image_history"
    description = "在当前聊天流里搜索图片描述、Tag 和原消息文本；免保存模式只读 MoFox 核心历史，不复制图片。"

    async def execute(
        self,
        query: Annotated[str, "图片内容或相关关键词"],
        limit: Annotated[int, "返回条数，1 到 10"] = 5,
    ) -> tuple[bool, str | dict]:
        """返回当前会话内匹配的图片记录。"""

        config = _config(self.plugin)
        if config is None:
            return False, "mofox_nai 配置尚未加载"
        denied = _access_error(self, config)
        if denied:
            return False, denied
        try:
            if archive_is_enabled(config):
                rows = search_local_images(config, self.get_current_stream_id(), query, limit)
            else:
                rows = await list_core_image_messages(
                    self.get_current_stream_id(),
                    limit=config.archive.history_scan_limit,
                    max_bytes=config.archive.max_image_bytes,
                )
                terms = [part.casefold() for part in query.split() if part]
                rows = [
                    row for row in rows
                    if any(term in row["source_text"].casefold() for term in terms)
                ][: max(1, min(limit, 10))]
        except Exception as error:
            logger.error("本地图片历史搜索失败", exc_info=error)
            return False, f"图库搜索失败：{error}"
        return True, {"count": len(rows), "images": _format_records(rows)}


class SearchImageDatabaseTool(BaseTool):
    """使用嵌入模型检索 Qdrant，或查询 SQLite Tag/图片目录。"""

    name = "search_image_database"
    description = (
        "查询配置的图片/Tag 数据库。配置 Qdrant 与嵌入模型后会做向量语义检索；"
        "否则若 database.sqlite_path 指向 NAI Tag SQLite 目录，则用 FTS5 搜索标签、别名和描述；"
        "都未配置时搜索本地图片库或当前聊天的 MoFox 核心历史文本。"
    )

    async def execute(
        self,
        query: Annotated[str, "自然语言描述或图片 Tag"],
        limit: Annotated[int, "返回条数，1 到 10"] = 5,
    ) -> tuple[bool, str | dict]:
        """查询配置的本地图片数据库并返回命中元数据。"""

        config = _config(self.plugin)
        if config is None:
            return False, "mofox_nai 配置尚未加载"
        denied = _access_error(self, config)
        if denied:
            return False, denied
        try:
            rows = await MofoxNaiService(config).search_database(
                query=query,
                stream_id=self.get_current_stream_id(),
                limit=max(1, min(limit, 10)),
            )
        except Exception as error:
            logger.error("图片数据库查询失败", exc_info=error)
            return False, f"图片数据库查询失败：{error}"
        return True, {"count": len(rows), "images": _format_records(rows)}


class SearchDanbooruTool(BaseTool):
    """按 Danbooru Tag 查询公开帖子。"""

    name = "search_danbooru"
    description = "使用 Danbooru Posts API 查询 Tag、预览地址和帖子链接；默认强制 rating:s 安全分级。"

    async def execute(
        self,
        tags: Annotated[str, "空格分隔的 Danbooru Tag，例如 1girl blue_hair outdoors"],
        limit: Annotated[int, "返回条数，1 到 10"] = 5,
    ) -> tuple[bool, str | dict]:
        """向配置的 Danbooru 实例发送只读查询。"""

        config = _config(self.plugin)
        if config is None:
            return False, "mofox_nai 配置尚未加载"
        denied = _access_error(self, config)
        if denied:
            return False, denied
        try:
            posts = await MofoxNaiService(config).search_danbooru(tags, limit)
        except Exception as error:
            logger.error("Danbooru 查询失败", exc_info=error)
            return False, f"Danbooru 查询失败：{error}"
        return True, {"count": len(posts), "posts": posts}


def _format_records(rows: list[dict]) -> list[dict]:
    """限制返回给 LLM 的图片元数据字段和文本长度。"""

    formatted: list[dict] = []
    for row in rows:
        item = {
            "id": row.get("id", row.get("record_id")),
            "message_id": str(row.get("message_id", ""))[:120],
            "created_at": row.get("created_at"),
            "sender": str(row.get("sender_name", ""))[:100],
            "caption": str(row.get("display_name") or row.get("caption", ""))[:500],
            "tags": str(row.get("name") or row.get("tags", ""))[:800],
            "text": str(row.get("source_text", ""))[:500],
            "score": row.get("score"),
        }
        for key, limit in (("category_label", 40), ("description", 1000), ("aliases", 500), ("source", 100), ("source_url", 500)):
            value = row.get(key)
            if value:
                item[key] = str(value)[:limit]
        if row.get("post_count") is not None:
            item["post_count"] = row["post_count"]
        if row.get("deprecated") is not None:
            item["deprecated"] = row["deprecated"]
        formatted.append(item)
    return formatted
