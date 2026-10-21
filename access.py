"""群聊与私聊白名单/黑名单判断。"""

# Copyright (C) 2026 Lycoris radiata
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from typing import Any

from .config import MofoxNaiConfig


def _get(value: Any, key: str, default: Any = None) -> Any:
    """兼容 MoFox Message 对象与测试/适配器字典。"""

    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _list_values(values: Any) -> set[str]:
    """规范化配置中的 ID 列表。"""

    if not isinstance(values, (list, tuple, set)):
        return set()
    return {str(value).strip().casefold() for value in values if str(value).strip()}


def chat_access(config: MofoxNaiConfig, message: Any) -> tuple[bool, str]:
    """按聊天类型和原始群号/用户号检查插件访问权限。

    白名单为空时不启用白名单限制；黑名单始终生效，且优先于白名单。
    无法识别的聊天类型不受群/私聊名单影响。
    """

    chat_type = str(_get(message, "chat_type", "") or "").strip().casefold()
    if chat_type not in {"group", "private"}:
        return True, ""

    extra = _get(message, "extra", {})
    if not isinstance(extra, dict):
        extra = {}
    if chat_type == "group":
        target_id = next(
            (
                str(value).strip()
                for value in (
                    _get(message, "group_id", ""),
                    extra.get("group_id", ""),
                    extra.get("target_group_id", ""),
                )
                if str(value or "").strip()
            ),
            "",
        )
        whitelist = _list_values(config.plugin.group_whitelist)
        blacklist = _list_values(config.plugin.group_blacklist)
        label = "群聊"
    else:
        target_id = next(
            (
                str(value).strip()
                for value in (
                    _get(message, "sender_id", ""),
                    extra.get("target_user_id", ""),
                    extra.get("user_id", ""),
                )
                if str(value or "").strip()
            ),
            "",
        )
        whitelist = _list_values(config.plugin.private_whitelist)
        blacklist = _list_values(config.plugin.private_blacklist)
        label = "私聊"

    normalized_id = target_id.casefold()
    if normalized_id and normalized_id in blacklist:
        return False, f"当前{label}已被 MoFox NAI 屏蔽。"
    if whitelist and (not normalized_id or normalized_id not in whitelist):
        return False, f"当前{label}不在 MoFox NAI 白名单中。"
    return True, ""
