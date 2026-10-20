"""只读查询外部 SQLite NAI Tag 目录。"""

# Copyright (C) 2026 Lycoris radiata
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any


_CATEGORY_LABELS = {
    0: "general",
    1: "artist",
    3: "copyright",
    4: "character",
    5: "meta",
}


def _search_sync(database_path: str, query: str, limit: int) -> list[dict[str, Any]]:
    """使用数据库内的 FTS5 trigram 索引搜索 Tag，不修改数据库。"""

    path = Path(database_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"找不到 Tag SQLite 数据库：{path}")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        clean_query = query.strip()[:200]
        if not clean_query:
            return []

        # Quote every token so user text is treated as search terms, not FTS operators.
        tokens = re.findall(r"[^\W_]+(?:[_'-][^\W_]+)*", clean_query, flags=re.UNICODE)
        tokens = [token for token in tokens if len(token) >= 3][:8]
        if tokens:
            # Put an exact canonical tag first when the query itself is a tag.
            exact = connection.execute(
                """SELECT name, display_name, category, post_count, deprecated,
                          description, aliases, source, source_url, 0.0 AS score
                   FROM catalog_tags WHERE name = ? COLLATE NOCASE LIMIT 1""",
                (clean_query.casefold(),),
            ).fetchone()
            fts_query = " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
            matches = connection.execute(
                """SELECT rowid, bm25(catalog_search) AS score
                   FROM catalog_search
                   WHERE catalog_search MATCH ?
                   ORDER BY score ASC
                   LIMIT ?""",
                (fts_query, max(1, min(limit, 20))),
            ).fetchall()
            rows = [
                connection.execute(
                    """SELECT name, display_name, category, post_count, deprecated,
                              description, aliases, source, source_url, ? AS score
                       FROM catalog_tags WHERE rowid = ?""",
                    (float(match["score"] or 0.0), int(match["rowid"])),
                ).fetchone()
                for match in matches
            ]
            rows = [row for row in rows if row is not None]
            if exact is not None:
                exact_name = str(exact["name"]).casefold()
                rows = [exact] + [row for row in rows if str(row["name"]).casefold() != exact_name]
        else:
            exact_term = clean_query.casefold()
            rows = connection.execute(
                """SELECT t.name, t.display_name, t.category, t.post_count,
                          t.deprecated, t.description, t.aliases, t.source,
                          t.source_url, 0.0 AS score
                   FROM catalog_terms AS terms
                   JOIN catalog_tags AS t ON t.rowid = terms.tag_rowid
                   WHERE terms.term = ? COLLATE NOCASE OR t.name = ? COLLATE NOCASE
                   ORDER BY t.post_count DESC
                   LIMIT ?""",
                (exact_term, exact_term, max(1, min(limit, 20))),
            ).fetchall()

        rows = rows[: max(1, min(limit, 20))]
        results: list[dict[str, Any]] = []
        for row in rows:
            category = int(row["category"] or 0)
            results.append(
                {
                    "name": str(row["name"] or ""),
                    "display_name": str(row["display_name"] or ""),
                    "category": category,
                    "category_label": _CATEGORY_LABELS.get(category, "unknown"),
                    "post_count": int(row["post_count"] or 0),
                    "deprecated": bool(row["deprecated"]),
                    "description": str(row["description"] or "")[:2000],
                    "aliases": str(row["aliases"] or "")[:1000],
                    "source": str(row["source"] or ""),
                    "source_url": str(row["source_url"] or ""),
                    "score": float(row["score"] or 0.0),
                }
            )
        return results
    finally:
        connection.close()


async def search_sqlite_catalog(
    database_path: str, query: str, limit: int = 5
) -> list[dict[str, Any]]:
    """在线程池中执行 SQLite FTS 查询，避免阻塞 MoFox 事件循环。"""

    import asyncio

    return await asyncio.to_thread(_search_sync, database_path, query, limit)
