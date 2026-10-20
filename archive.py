"""SQLite 图片归档，保存聊天图片文件和可搜索的消息元数据。"""

# Copyright (C) 2026 Lycoris radiata
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import mimetypes
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from src.app.plugin_system.api.stream_api import get_stream_messages

from .config import MofoxNaiConfig


def resolve_path(path: str) -> Path:
    """解析相对 MoFox 工作目录的持久化路径。"""

    resolved = Path(path).expanduser()
    return resolved if resolved.is_absolute() else Path.cwd() / resolved


def _connect(database_path: Path) -> sqlite3.Connection:
    """打开带 WAL 的 SQLite 连接。"""

    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@contextmanager
def _database(database_path: Path) -> Iterator[sqlite3.Connection]:
    """保证 SQLite 连接在事务结束后关闭。"""

    connection = _connect(database_path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _initialize_sync(config: MofoxNaiConfig) -> None:
    """创建图片归档表和查询索引。"""

    database_path = resolve_path(config.archive.database_path)
    resolve_path(config.archive.image_dir).mkdir(parents=True, exist_ok=True)
    with _database(database_path) as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL,
                stream_id TEXT NOT NULL,
                platform TEXT NOT NULL DEFAULT '',
                sender_id TEXT NOT NULL DEFAULT '',
                sender_name TEXT NOT NULL DEFAULT '',
                direction TEXT NOT NULL DEFAULT 'incoming',
                created_at REAL NOT NULL,
                file_path TEXT NOT NULL,
                media_hash TEXT NOT NULL,
                source_text TEXT NOT NULL DEFAULT '',
                caption TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '',
                UNIQUE(message_id, stream_id, media_hash)
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_mofox_nai_stream_time "
            "ON images(stream_id, created_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_mofox_nai_message "
            "ON images(stream_id, message_id)"
        )


async def initialize_archive(config: MofoxNaiConfig) -> None:
    """在线程池中初始化归档文件和数据库。"""

    await asyncio.to_thread(_initialize_sync, config)


def decode_image_data(value: str, max_bytes: int) -> tuple[bytes, str] | None:
    """解码消息中的 Base64 图片并确认格式和大小。"""

    from io import BytesIO

    import PIL.Image

    raw_value = value.strip()
    mime_type = "image/png"
    if raw_value.startswith("data:"):
        header, separator, payload = raw_value.partition(",")
        if not separator or ";base64" not in header.lower():
            return None
        mime_type = header[5:].split(";", 1)[0].lower() or mime_type
        raw_value = payload
    elif raw_value.startswith("base64|"):
        raw_value = raw_value[7:]

    try:
        image_data = base64.b64decode(raw_value, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not image_data or len(image_data) > max_bytes:
        return None

    try:
        with PIL.Image.open(BytesIO(image_data)) as image:
            image.verify()
            image_format = (image.format or "PNG").lower()
    except Exception:
        return None

    extension = {
        "jpeg": ".jpg",
        "jpg": ".jpg",
        "png": ".png",
        "webp": ".webp",
        "gif": ".gif",
        "bmp": ".bmp",
    }.get(image_format)
    if extension is None:
        extension = Path(mimetypes.guess_extension(mime_type) or ".img").suffix
    return image_data, extension


def message_images(message: Any, max_bytes: int) -> list[tuple[bytes, str]]:
    """从统一 Message 对象中提取图片二进制内容。"""

    content = getattr(message, "content", None)
    media_items: list[Any] = []
    if isinstance(content, dict):
        raw_media = content.get("media")
        if isinstance(raw_media, list):
            media_items.extend(raw_media)
    extra_media = getattr(message, "extra", {}).get("media", [])
    if isinstance(extra_media, list):
        media_items.extend(extra_media)
    if isinstance(content, str) and getattr(message, "message_type", None) in {"image", "emoji"}:
        media_items.append({"type": "image", "data": content})

    images: list[tuple[bytes, str]] = []
    seen_data: set[str] = set()
    for item in media_items:
        if not isinstance(item, dict):
            continue
        if str(item.get("type", "image")).lower() not in {"image", "emoji"}:
            continue
        data = item.get("data")
        if not isinstance(data, str):
            continue
        if data in seen_data:
            continue
        seen_data.add(data)
        decoded = decode_image_data(data, max_bytes=max_bytes)
        if decoded is not None:
            images.append(decoded)
    return images


async def list_core_image_messages(
    stream_id: str,
    limit: int = 200,
    max_bytes: int = 20 * 1024 * 1024,
    exclude_message_id: str = "",
) -> list[dict[str, Any]]:
    """从 MoFox 核心聊天历史临时提取图片，不写入插件目录或数据库。"""

    messages = await get_stream_messages(stream_id, limit=max(1, min(limit, 1000)))
    rows: list[dict[str, Any]] = []
    for message in reversed(messages):
        message_id = str(getattr(message, "message_id", "") or "")
        if not message_id or message_id == exclude_message_id:
            continue
        images = await asyncio.to_thread(message_images, message, max(1024, max_bytes))
        if not images:
            continue
        content = getattr(message, "content", "")
        source_text = str(
            getattr(message, "processed_plain_text", "")
            or (content if isinstance(content, str) else "")
            or ""
        )[:4000]
        rows.append(
            {
                "message_id": message_id,
                "stream_id": str(getattr(message, "stream_id", "") or stream_id),
                "platform": str(getattr(message, "platform", "") or ""),
                "sender_id": str(getattr(message, "sender_id", "") or ""),
                "sender_name": str(getattr(message, "sender_name", "") or ""),
                "direction": "outgoing" if getattr(message, "sender_role", "") == "bot" else "incoming",
                "created_at": _message_timestamp(message),
                "source_text": source_text,
                "caption": "",
                "tags": "",
                "image_count": len(images),
                # Bytes exist only in this request's memory and are ignored by metadata formatters.
                "_images": images,
            }
        )
    return rows


def _message_timestamp(message: Any) -> float:
    """获取统一格式的消息时间戳。"""

    value = getattr(message, "time", None)
    if isinstance(value, (int, float)):
        return float(value)
    if hasattr(value, "timestamp"):
        return float(value.timestamp())
    return time.time()


def _archive_sync(
    config: MofoxNaiConfig,
    message: Any,
    image_data: bytes,
    extension: str,
    direction: str,
) -> dict[str, Any]:
    """将一张图片写入磁盘并登记到 SQLite。"""

    digest = hashlib.sha256(image_data).hexdigest()
    image_root = resolve_path(config.archive.image_dir)
    image_root.mkdir(parents=True, exist_ok=True)
    file_path = image_root / f"{digest}{extension}"
    if not file_path.exists():
        file_path.write_bytes(image_data)

    message_id = str(getattr(message, "message_id", "") or f"local-{digest[:16]}")
    stream_id = str(getattr(message, "stream_id", "") or "")
    source_text = str(
        getattr(message, "processed_plain_text", "")
        or (getattr(message, "content", "") if isinstance(getattr(message, "content", ""), str) else "")
        or ""
    )[:4000]
    database_path = resolve_path(config.archive.database_path)
    with _database(database_path) as connection:
        cursor = connection.execute(
            """INSERT OR IGNORE INTO images (
                message_id, stream_id, platform, sender_id, sender_name, direction,
                created_at, file_path, media_hash, source_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                message_id,
                stream_id,
                str(getattr(message, "platform", "") or ""),
                str(getattr(message, "sender_id", "") or ""),
                str(getattr(message, "sender_name", "") or ""),
                direction,
                _message_timestamp(message),
                str(file_path),
                digest,
                source_text,
            ),
        )
        if cursor.rowcount:
            record_id = int(cursor.lastrowid)
        else:
            row = connection.execute(
                "SELECT id FROM images WHERE message_id=? AND stream_id=? AND media_hash=?",
                (message_id, stream_id, digest),
            ).fetchone()
            record_id = int(row["id"])
        _prune_sync(connection, image_root, max(1, config.archive.max_records))
        row = connection.execute("SELECT * FROM images WHERE id=?", (record_id,)).fetchone()
        return dict(row) if row is not None else {}


def _prune_sync(connection: sqlite3.Connection, image_root: Path, max_records: int) -> None:
    """删除超出保留上限的最旧记录并清理无引用图片文件。"""

    stale_rows = connection.execute(
        "SELECT id, file_path FROM images ORDER BY created_at DESC, id DESC LIMIT -1 OFFSET ?",
        (max_records,),
    ).fetchall()
    if not stale_rows:
        return
    connection.executemany("DELETE FROM images WHERE id=?", [(int(row["id"]),) for row in stale_rows])
    still_used = {
        str(row[0]) for row in connection.execute("SELECT DISTINCT file_path FROM images").fetchall()
    }
    for row in stale_rows:
        stale_path = Path(str(row["file_path"]))
        if str(stale_path) not in still_used and stale_path.parent == image_root:
            stale_path.unlink(missing_ok=True)


async def archive_image(
    config: MofoxNaiConfig,
    message: Any,
    image_data: bytes,
    extension: str,
    direction: str,
) -> dict[str, Any]:
    """在线程池中保存单张聊天图片。"""

    return await asyncio.to_thread(
        _archive_sync, config, message, image_data, extension, direction
    )


def list_recent_images(
    config: MofoxNaiConfig,
    stream_id: str,
    limit: int = 10,
    exclude_message_id: str = "",
) -> list[dict[str, Any]]:
    """按当前聊天流列出最近图片记录。"""

    count = max(1, min(limit, 50))
    with _database(resolve_path(config.archive.database_path)) as connection:
        rows = connection.execute(
            """SELECT * FROM images WHERE stream_id=? AND message_id!=?
            ORDER BY created_at DESC, id DESC LIMIT ?""",
            (stream_id, exclude_message_id, count),
        ).fetchall()
    return [dict(row) for row in rows]


def get_image_record(
    config: MofoxNaiConfig,
    record_id: int | None = None,
    stream_id: str = "",
    message_id: str = "",
) -> dict[str, Any] | None:
    """按记录 ID 或消息 ID 获取当前流中的图片记录。"""

    with _database(resolve_path(config.archive.database_path)) as connection:
        if record_id is not None:
            row = connection.execute(
                "SELECT * FROM images WHERE id=? AND stream_id=?", (record_id, stream_id)
            ).fetchone()
        elif message_id:
            row = connection.execute(
                "SELECT * FROM images WHERE message_id=? AND stream_id=? ORDER BY id DESC LIMIT 1",
                (message_id, stream_id),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM images WHERE stream_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
                (stream_id,),
            ).fetchone()
    return dict(row) if row is not None else None


def search_local_images(
    config: MofoxNaiConfig,
    stream_id: str,
    query: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """在当前流的图片描述、Tag 和消息文本中执行本地检索。"""

    query_text = query.strip()[:200]
    if not query_text:
        return []
    escaped = query_text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    with _database(resolve_path(config.archive.database_path)) as connection:
        rows = connection.execute(
            """SELECT * FROM images WHERE stream_id=? AND
            (caption LIKE ? ESCAPE '\\' COLLATE NOCASE OR
             tags LIKE ? ESCAPE '\\' COLLATE NOCASE OR
             source_text LIKE ? ESCAPE '\\' COLLATE NOCASE)
            ORDER BY created_at DESC, id DESC LIMIT ?""",
            (stream_id, pattern, pattern, pattern, max(1, min(limit, 20))),
        ).fetchall()
    return [dict(row) for row in rows]


def read_image_file(record: dict[str, Any]) -> bytes:
    """读取归档记录指向的本地图片。"""

    path = Path(str(record.get("file_path", ""))).resolve()
    if not path.is_file():
        raise FileNotFoundError("图片文件不存在")
    return path.read_bytes()


def update_image_description(
    config: MofoxNaiConfig,
    record_id: int,
    caption: str,
    tags: str,
) -> dict[str, Any] | None:
    """写入视觉描述与反推 Tag。"""

    with _database(resolve_path(config.archive.database_path)) as connection:
        connection.execute(
            "UPDATE images SET caption=?, tags=? WHERE id=?",
            (caption[:4000], tags[:4000], record_id),
        )
        row = connection.execute("SELECT * FROM images WHERE id=?", (record_id,)).fetchone()
    return dict(row) if row is not None else None
