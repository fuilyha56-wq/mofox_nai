"""群聊和私聊访问名单测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from plugins.mofox_nai.access import chat_access
from plugins.mofox_nai.commands import NaiCommand
from plugins.mofox_nai.config import MofoxNaiConfig
from plugins.mofox_nai.handlers import ArchiveChatImagesHandler
from src.kernel.event import EventDecision


def _message(chat_type: str, **fields: object) -> SimpleNamespace:
    return SimpleNamespace(chat_type=chat_type, extra={}, **fields)


def _run_without_suspension(coroutine):
    """Run guard paths that return before performing any asynchronous I/O."""

    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError("The denied access path unexpectedly performed asynchronous I/O")


def test_empty_access_lists_leave_groups_and_private_chats_available() -> None:
    config = MofoxNaiConfig()

    assert chat_access(config, _message("group", group_id="1094860211")) == (True, "")
    assert chat_access(config, _message("private", sender_id="123456")) == (True, "")


def test_group_whitelist_and_blacklist_are_independent_and_blacklist_wins() -> None:
    config = MofoxNaiConfig()
    config.plugin.group_whitelist = ["1094860211", "123"]
    config.plugin.group_blacklist = ["123"]

    assert chat_access(config, _message("group", group_id="1094860211")) == (True, "")
    allowed, reason = chat_access(config, _message("group", group_id="456"))
    assert allowed is False
    assert "白名单" in reason
    allowed, reason = chat_access(config, _message("group", group_id="123"))
    assert allowed is False
    assert "屏蔽" in reason


def test_private_whitelist_uses_sender_id_and_rejects_missing_ids() -> None:
    config = MofoxNaiConfig()
    config.plugin.private_whitelist = ["123456"]

    assert chat_access(config, _message("private", sender_id="123456")) == (True, "")
    allowed, reason = chat_access(config, _message("private", sender_id=""))
    assert allowed is False
    assert "白名单" in reason


def test_group_id_can_be_read_from_mofox_message_extra() -> None:
    config = MofoxNaiConfig()
    config.plugin.group_whitelist = ["1094860211"]
    message = SimpleNamespace(chat_type="group", extra={"target_group_id": "1094860211"})

    assert chat_access(config, message) == (True, "")


def test_unrelated_chat_types_are_not_treated_as_private_chats() -> None:
    config = MofoxNaiConfig()
    config.plugin.private_whitelist = ["123456"]

    assert chat_access(config, _message("discuss", sender_id="other")) == (True, "")


def test_nai_command_is_stopped_before_route_dispatch_when_group_is_not_allowed() -> None:
    config = MofoxNaiConfig()
    config.plugin.group_whitelist = ["1094860211"]
    command = NaiCommand(
        plugin=SimpleNamespace(config=config),
        stream_id="stream-hash",
        message=SimpleNamespace(
            content="/nai 1girl",
            chat_type="group",
            group_id="other-group",
            extra={},
        ),
    )

    with patch(
        "plugins.mofox_nai.commands.BaseCommand.execute",
        new=AsyncMock(return_value=(True, "routed")),
    ) as route:
        ok, message = _run_without_suspension(command.execute("1girl"))

    assert ok is True
    assert "白名单" in message
    route.assert_not_awaited()


def test_disallowed_group_images_are_not_archived() -> None:
    config = MofoxNaiConfig()
    config.plugin.group_whitelist = ["1094860211"]
    config.archive.mode = "persistent"
    handler = SimpleNamespace(plugin=SimpleNamespace(config=config))
    message = _message("group", group_id="other-group")

    with patch("plugins.mofox_nai.handlers.asyncio.to_thread") as extract:
        decision, _ = _run_without_suspension(
            ArchiveChatImagesHandler.execute(handler, "ON_MESSAGE_RECEIVED", {"message": message})
        )

    assert decision == EventDecision.PASS
    extract.assert_not_called()
