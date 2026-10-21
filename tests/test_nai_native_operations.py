"""NAI 原生参考、重绘、放大和引用消息的离线回归测试。"""

from __future__ import annotations

import base64
import inspect
import sqlite3
import tempfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
from PIL import Image

from plugins.mofox_nai.cards import render_card
from plugins.mofox_nai.commands import NaiCommand
from plugins.mofox_nai.config import MofoxNaiConfig, archive_is_enabled
from plugins.mofox_nai.archive import list_core_image_messages
from plugins.mofox_nai.service import MofoxNaiService
from plugins.mofox_nai.watermark import _font_candidates, apply_watermark


class _Response:
    def __init__(
        self,
        payload: dict | None = None,
        *,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
        status_code: int = 200,
        text: str = "",
    ) -> None:
        self._payload = payload
        self.content = content
        self.headers = headers or {"content-type": "application/json"}
        self.status_code = status_code
        self.text = text
        self.reason_phrase = {404: "Not Found", 502: "Bad Gateway"}.get(status_code, "")
        self.request = httpx.Request("POST", "https://nai.test/ai/generate-image")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            response = httpx.Response(self.status_code, request=self.request)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=self.request, response=response
            )

    def json(self) -> dict:
        if self._payload is None:
            raise AssertionError("Response does not contain JSON")
        return self._payload


class _Client:
    requests: list[tuple[str, dict]] = []
    responses: list[_Response] = []

    def __init__(self, **_: object) -> None:
        pass

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def post(self, url: str, **kwargs: dict) -> _Response:
        self.requests.append((url, kwargs))
        return self.responses.pop(0)

    async def get(self, url: str, **kwargs: dict) -> _Response:
        self.requests.append((url, kwargs))
        return self.responses.pop(0)


def _native_image_response(data: bytes = b"generated-image") -> _Response:
    encoded = base64.b64encode(data).decode("ascii")
    return _Response({"images": [{"image": encoded, "index": 0, "seed": 17}]})


def _run_immediate(coroutine):
    """Drive coroutines backed only by immediately resolved mock operations."""

    sent_value = None
    while True:
        try:
            pending = coroutine.send(sent_value)
        except StopIteration as result:
            return result.value
        if inspect.iscoroutine(pending):
            sent_value = _run_immediate(pending)
        elif pending is None:
            sent_value = None
        else:
            raise AssertionError(f"Unexpected async suspension in offline test: {pending!r}")


async def _async_immediate(function, *args):
    return function(*args)


class NativeOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        _Client.requests = []
        _Client.responses = []
        self.config = MofoxNaiConfig()
        self.config.generation.api_format = "novelai"
        self.config.generation.base_url = "https://nai.test"
        self.config.generation.model = "nai-diffusion-5-full"
        self.config.generation.timeout = 2
        self.config.watermark.enabled = False

    def test_reference_mode_sends_native_vibe_reference_fields(self) -> None:
        _Client.responses = [_native_image_response()]
        service = MofoxNaiService(self.config)

        with patch("plugins.mofox_nai.service.httpx.AsyncClient", _Client):
            result, _ = _run_immediate(
                service.generate_image(
                    "1girl",
                    operation="reference",
                    source_image=b"quoted-reference",
                    reference_strength=0.72,
                    reference_information=0.63,
                )
            )

        url, request = _Client.requests[0]
        params = request["json"]["parameters"]
        self.assertEqual(url, "https://nai.test/ai/generate-image")
        self.assertEqual(params["reference_image"], base64.b64encode(b"quoted-reference").decode())
        self.assertEqual(params["reference_strength"], 0.72)
        self.assertEqual(params["reference_information_extracted"], 0.63)
        self.assertNotIn("image", params)
        self.assertEqual(result, b"generated-image")

    def test_vibe_transfer_supports_multiple_images_from_one_quoted_message(self) -> None:
        _Client.responses = [_native_image_response()]
        with patch("plugins.mofox_nai.service.httpx.AsyncClient", _Client):
            _run_immediate(
                MofoxNaiService(self.config).generate_image(
                    "1girl",
                    operation="reference",
                    reference_images=[b"style-one", b"style-two"],
                    reference_strength=0.7,
                    reference_information=0.8,
                )
            )

        params = _Client.requests[0][1]["json"]["parameters"]
        self.assertEqual(
            params["reference_image_multiple"],
            [base64.b64encode(b"style-one").decode(), base64.b64encode(b"style-two").decode()],
        )
        self.assertEqual(params["reference_strength_multiple"], [0.7, 0.7])
        self.assertEqual(params["reference_information_extracted_multiple"], [0.8, 0.8])
        self.assertNotIn("reference_image", params)

    def test_precise_reference_sends_native_character_and_style_fields(self) -> None:
        self.config.generation.model = "nai-diffusion-4-5-full"
        self.config.generation.precise_reference_strength = 0.75
        self.config.generation.precise_reference_fidelity = 0.4
        _Client.responses = [_native_image_response()]

        with patch("plugins.mofox_nai.service.httpx.AsyncClient", _Client):
            _run_immediate(
                MofoxNaiService(self.config).generate_image(
                    "1girl, blue_hair",
                    operation="precise_reference",
                    reference_images=[b"character-ref", b"style-ref"],
                    precise_reference_type="character&style",
                )
            )

        params = _Client.requests[0][1]["json"]["parameters"]
        self.assertEqual(
            params["director_reference_images"],
            [base64.b64encode(b"character-ref").decode(), base64.b64encode(b"style-ref").decode()],
        )
        self.assertEqual(
            [item["caption"]["base_caption"] for item in params["director_reference_descriptions"]],
            ["character&style", "character&style"],
        )
        self.assertEqual(params["director_reference_information_extracted"], [1.0, 1.0])
        self.assertEqual(params["director_reference_strength_values"], [0.75, 0.75])
        self.assertEqual(params["director_reference_secondary_strength_values"], [0.4, 0.4])

    def test_precise_reference_is_rejected_for_v5_before_network_request(self) -> None:
        with self.assertRaisesRegex(ValueError, "仅支持 NAI 4.5"):
            _run_immediate(
                MofoxNaiService(self.config).generate_image(
                    "1girl", operation="precise_reference", source_image=b"reference"
                )
            )
        self.assertEqual(_Client.requests, [])

    def test_redraw_and_inpaint_send_source_and_mask_images(self) -> None:
        _Client.responses = [_native_image_response(), _native_image_response()]
        service = MofoxNaiService(self.config)

        with patch("plugins.mofox_nai.service.httpx.AsyncClient", _Client):
            _run_immediate(
                service.generate_image(
                    "red_dress",
                    operation="redraw",
                    source_image=b"quoted-source",
                    redraw_strength=0.4,
                    redraw_noise=0.25,
                )
            )
            _run_immediate(
                service.generate_image(
                    "blue_coat",
                    operation="inpaint",
                    source_image=b"quoted-source",
                    mask_image=b"quoted-mask",
                    redraw_strength=0.5,
                    redraw_noise=0.3,
                )
            )

        redraw = _Client.requests[0][1]["json"]["parameters"]
        inpaint = _Client.requests[1][1]["json"]["parameters"]
        encoded_source = base64.b64encode(b"quoted-source").decode()
        self.assertEqual(redraw["image"], encoded_source)
        self.assertEqual(redraw["strength"], 0.4)
        self.assertEqual(redraw["noise"], 0.25)
        self.assertEqual(redraw["img2img"]["strength"], 0.4)
        self.assertEqual(inpaint["image"], encoded_source)
        self.assertEqual(inpaint["mask"], base64.b64encode(b"quoted-mask").decode())
        self.assertEqual(inpaint["img2img"]["noise"], 0.3)

    def test_inpaint_without_mask_fails_before_request(self) -> None:
        service = MofoxNaiService(self.config)
        with patch("plugins.mofox_nai.service.httpx.AsyncClient", _Client):
            with self.assertRaisesRegex(ValueError, "需要在命令消息中附加遮罩"):
                _run_immediate(
                    service.generate_image(
                        "blue_coat", operation="inpaint", source_image=b"source"
                    )
                )
        self.assertEqual(_Client.requests, [])

    def test_native_upscale_uses_documented_endpoint_and_accepts_zip(self) -> None:
        archive_data = BytesIO()
        with ZipFile(archive_data, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("image/0.png", b"upscaled-image")
        _Client.responses = [
            _Response(
                content=archive_data.getvalue(),
                headers={"content-type": "application/zip"},
            )
        ]

        with patch("plugins.mofox_nai.service.httpx.AsyncClient", _Client):
            result = _run_immediate(MofoxNaiService(self.config).upscale_image(b"source-image"))

        url, request = _Client.requests[0]
        self.assertEqual(url, "https://nai.test/ai/upscale")
        self.assertEqual(request["headers"]["Accept"], "application/json")
        self.assertEqual(request["json"]["image"], base64.b64encode(b"source-image").decode())
        self.assertEqual(request["json"]["model"], self.config.generation.model)
        self.assertEqual(result, b"upscaled-image")

    def test_director_tools_uses_native_augment_endpoint(self) -> None:
        archive_data = BytesIO()
        with ZipFile(archive_data, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("image/0.png", b"director-result")
        _Client.responses = [
            _Response(
                content=archive_data.getvalue(),
                headers={"content-type": "application/zip"},
            )
        ]

        with patch("plugins.mofox_nai.service.httpx.AsyncClient", _Client):
            result, final_prompt = _run_immediate(
                MofoxNaiService(self.config).augment_image(
                    b"quoted-source",
                    request_type="custom.tool/type",
                    prompt="red_hair",
                    defry=8,
                )
            )

        url, request = _Client.requests[0]
        self.assertEqual(url, "https://nai.test/ai/augment-image")
        self.assertEqual(request["headers"]["Accept"], "application/zip")
        self.assertEqual(request["json"]["req_type"], "custom.tool/type")
        self.assertEqual(request["json"]["defry"], 8)
        self.assertEqual(request["json"]["prompt"], "red_hair")
        self.assertEqual(request["json"]["image"], base64.b64encode(b"quoted-source").decode())
        self.assertEqual(result, b"director-result")
        self.assertEqual(final_prompt, "red_hair")

    def test_native_404_explains_route_configuration(self) -> None:
        _Client.responses = [_Response(status_code=404)]
        with patch("plugins.mofox_nai.service.httpx.AsyncClient", _Client):
            with self.assertRaisesRegex(ValueError, "已发送 NovelAI 原生格式"):
                _run_immediate(MofoxNaiService(self.config).generate_image("1girl"))
        self.assertEqual(_Client.requests[0][0], "https://nai.test/ai/generate-image")

    def test_native_502_includes_gateway_response_for_diagnosis(self) -> None:
        _Client.responses = [
            _Response(status_code=502, text='{"error":"upstream unavailable"}')
        ]
        with patch("plugins.mofox_nai.service.httpx.AsyncClient", _Client):
            with self.assertRaisesRegex(ValueError, "HTTP 502 Bad Gateway.*upstream unavailable"):
                _run_immediate(MofoxNaiService(self.config).generate_image("1girl"))
        self.assertEqual(_Client.requests[0][0], "https://nai.test/ai/generate-image")

    def test_auto_protocol_uses_native_endpoint_for_bare_nai_model_url(self) -> None:
        self.config.generation.api_format = "auto"
        self.config.generation.base_url = "https://nai.test"
        _Client.responses = [_native_image_response()]

        with patch("plugins.mofox_nai.service.httpx.AsyncClient", _Client):
            _run_immediate(MofoxNaiService(self.config).generate_image("1girl"))

        url, request = _Client.requests[0]
        self.assertEqual(url, "https://nai.test/ai/generate-image")
        self.assertEqual(request["json"]["action"], "generate")
        self.assertEqual(request["json"]["model"], "nai-diffusion-5-full")
        self.assertIn("parameters", request["json"])

    def test_work_notice_uses_core_persona_and_text_model_output(self) -> None:
        self.config.generation.prompt_model = "translator"
        self.config.models.base_url = "https://models.test/v1"
        _Client.responses = [
            _Response({"choices": [{"message": {"content": "收到，我正在帮你画。"}}]})
        ]
        core_config = SimpleNamespace(
            personality=SimpleNamespace(
                nickname="小狐狸",
                identity="绘画助手",
                personality_core="温柔、简短",
                personality_side="喜欢用轻松的语气",
            )
        )

        with (
            patch("plugins.mofox_nai.service.httpx.AsyncClient", _Client),
            patch("src.core.config.get_core_config", return_value=core_config),
        ):
            notice = _run_immediate(
                MofoxNaiService(self.config).in_character_notice(
                    queued=True, activity="参考图生成"
                )
            )

        self.assertEqual(notice, "收到，我正在帮你画。")
        self.assertEqual(_Client.requests[0][0], "https://models.test/v1/chat/completions")
        system_prompt = _Client.requests[0][1]["json"]["messages"][0]["content"]
        self.assertIn("小狐狸", system_prompt)
        self.assertIn("温柔、简短", system_prompt)
        self.assertIn("串行生成队列", system_prompt)
        self.assertIn("参考图生成", system_prompt)

    def test_work_notice_failure_prevents_generation(self) -> None:
        command = SimpleNamespace(
            _config=lambda: self.config,
            _notify_work=AsyncMock(side_effect=RuntimeError("persona notice unavailable")),
            _reply=AsyncMock(return_value=True),
            _message=SimpleNamespace(platform="qq"),
            stream_id="status-stream",
            message_id="command-message",
        )
        with patch(
            "plugins.mofox_nai.commands.MofoxNaiService.generate_image",
            new=AsyncMock(),
        ) as generate:
            ok, message = _run_immediate(NaiCommand._generate(command, "1girl"))

        self.assertFalse(ok)
        self.assertIn("persona notice unavailable", message)
        generate.assert_not_awaited()
        command._reply.assert_awaited_once()

    def test_work_notice_send_failure_aborts_the_task(self) -> None:
        service = SimpleNamespace(
            is_queued_mode=lambda: False,
            generation_queue_locked=lambda: False,
            in_character_notice=AsyncMock(return_value="我正在帮你处理这张图。"),
        )
        command = SimpleNamespace(_reply_text=AsyncMock(return_value=False))

        with self.assertRaisesRegex(RuntimeError, "状态消息发送失败"):
            _run_immediate(NaiCommand._notify_work(command, service, "生成图片"))

        service.in_character_notice.assert_awaited_once_with(queued=False, activity="生成图片")

    def test_base_command_dispatch_returns_success_for_root_help_and_settings(self) -> None:
        cases = [
            ("/nai", "", "MoFox NAI"),
            ("/nai 帮助", "帮助", "MoFox NAI"),
            ("/nai 设置", "设置", "当前生图设置"),
        ]
        for message_text, routed_text, title in cases:
            with self.subTest(message_text=message_text):
                command = NaiCommand(
                    plugin=SimpleNamespace(config=self.config),
                    stream_id="command-stream",
                    message=SimpleNamespace(content=message_text, platform="qq"),
                )
                with patch(
                    "plugins.mofox_nai.commands.send_image",
                    new=AsyncMock(return_value=True),
                ) as send:
                    ok, _ = _run_immediate(command.execute(routed_text))

                self.assertTrue(ok)
                self.assertEqual(send.await_args.kwargs["processed_plain_text"], f"[MoFox NAI · {title}]")

    def test_reverse_tags_sends_the_referenced_image_to_the_vision_model(self) -> None:
        self.config.models.base_url = "https://models.test/v1"
        self.config.models.vision_model = "vision-model"
        png = BytesIO()
        Image.new("RGB", (2, 2), "#F0A0C0").save(png, format="PNG")
        _Client.responses = [
            _Response(
                {
                    "choices": [
                        {
                            "message": {
                                "content": "Tags: 1girl, pink_hair, smile\nCaption: 粉发女孩正在微笑。"
                            }
                        }
                    ]
                }
            )
        ]

        with patch("plugins.mofox_nai.service.httpx.AsyncClient", _Client):
            tags, caption = _run_immediate(
                MofoxNaiService(self.config).reverse_image_bytes(png.getvalue())
            )

        self.assertEqual(tags, "1girl, pink_hair, smile")
        self.assertEqual(caption, "粉发女孩正在微笑。")
        request = _Client.requests[0][1]["json"]
        image_url = request["messages"][1]["content"][1]["image_url"]["url"]
        self.assertTrue(image_url.startswith("data:image/png;base64,"))
        self.assertEqual(request["model"], "vision-model")

    def test_active_style_is_applied_and_queue_mode_is_configurable(self) -> None:
        self.config.generation.active_style = "柔光"
        service = MofoxNaiService(self.config)
        prompt = _run_immediate(service.prepare_prompt("1girl, blue_hair"))
        self.assertIn("soft lighting", prompt)
        self.assertFalse(service.is_queued_mode())
        self.config.generation.request_mode = "queue"
        self.assertTrue(service.is_queued_mode())

    def test_protocol_setting_command_updates_live_config(self) -> None:
        command = SimpleNamespace(
            _config=lambda: self.config,
            _remainder=lambda _: "协议 novelai",
            _is_settings_admin=lambda _: True,
            _update_config=lambda config, target, field, value: setattr(target, field, value),
            _reply=AsyncMock(return_value=True),
        )

        ok, _ = _run_immediate(NaiCommand.handle_settings(command))

        self.assertTrue(ok)
        self.assertEqual(self.config.generation.api_format, "novelai")
        command._reply.assert_awaited_once()

    def test_director_command_replies_to_the_quoted_source(self) -> None:
        command = SimpleNamespace(
            _config=lambda: self.config,
            _remainder=lambda _: "colorize 8 red_hair",
            _message=SimpleNamespace(platform="qq"),
            _replied_image=AsyncMock(
                return_value=(b"quoted-source", "quoted-message", object(), ".png")
            ),
            _notify_work=AsyncMock(),
            stream_id="director-stream",
        )
        with (
            patch(
                "plugins.mofox_nai.commands.MofoxNaiService.augment_image",
                new=AsyncMock(return_value=(b"director-result", "red_hair")),
            ) as augment,
            patch(
                "plugins.mofox_nai.commands.send_image",
                new=AsyncMock(return_value=True),
            ) as send,
        ):
            ok, _ = _run_immediate(NaiCommand.handle_director_tool(command))

        self.assertTrue(ok)
        augment.assert_awaited_once_with(
            image_data=b"quoted-source",
            request_type="colorize",
            prompt="red_hair",
            defry=8,
        )
        self.assertEqual(send.await_args.kwargs["reply_to"], "quoted-message")

    def test_precise_reference_command_uses_all_images_from_exact_reply(self) -> None:
        self.config.generation.model = "nai-diffusion-4-5-full"
        command = SimpleNamespace(
            _config=lambda: self.config,
            _remainder=lambda _: "style 1girl, blue_hair",
            _message=SimpleNamespace(platform="qq"),
            _replied_images=AsyncMock(
                return_value=(
                    [(b"quoted-char", ".png"), (b"quoted-style", ".png")],
                    "quoted-message",
                    object(),
                )
            ),
            _notify_work=AsyncMock(),
            stream_id="precise-stream",
        )
        with (
            patch(
                "plugins.mofox_nai.commands.MofoxNaiService.generate_image",
                new=AsyncMock(return_value=(b"generated", "1girl, blue_hair")),
            ) as generate,
            patch(
                "plugins.mofox_nai.commands.send_image",
                new=AsyncMock(return_value=True),
            ) as send,
        ):
            ok, _ = _run_immediate(NaiCommand.handle_precise_reference(command))

        self.assertTrue(ok)
        generate.assert_awaited_once_with(
            "1girl, blue_hair",
            operation="precise_reference",
            reference_images=[b"quoted-char", b"quoted-style"],
            precise_reference_type="style",
        )
        self.assertEqual(send.await_args.kwargs["reply_to"], "quoted-message")

    def test_replied_images_reads_only_the_exact_reply_target(self) -> None:
        config = self.config
        target = SimpleNamespace(message_id="quoted-id")
        command = SimpleNamespace(
            _config=lambda: config,
            _message=SimpleNamespace(reply_to="quoted-id"),
            stream_id="group-stream",
        )
        messages = [SimpleNamespace(message_id="newer-image"), target]
        with (
            patch(
                "plugins.mofox_nai.commands.get_stream_messages",
                new=AsyncMock(return_value=messages),
            ) as get_messages,
            patch(
                "plugins.mofox_nai.commands.message_images",
                return_value=[(b"quoted-only", ".png")],
            ) as extract_images,
            patch(
                "plugins.mofox_nai.commands.asyncio.to_thread",
                new=_async_immediate,
            ),
        ):
            result = _run_immediate(NaiCommand._replied_images(command))

        self.assertEqual(result[:2], ([(b"quoted-only", ".png")], "quoted-id"))
        self.assertIs(result[2], target)
        get_messages.assert_awaited_once_with("group-stream", limit=1000, offset=0)
        extract_images.assert_called_once_with(target, max(1024, config.archive.max_image_bytes))

    def test_unquoted_image_command_never_scans_for_a_recent_fallback(self) -> None:
        command = SimpleNamespace(
            _config=lambda: self.config,
            _message=SimpleNamespace(reply_to=""),
            stream_id="group-stream",
        )
        with patch("plugins.mofox_nai.commands.get_stream_messages", new=AsyncMock()) as get_messages:
            with self.assertRaisesRegex(LookupError, "回复目标图片"):
                _run_immediate(NaiCommand._replied_images(command))
        get_messages.assert_not_awaited()

    def test_result_card_is_a_readable_png(self) -> None:
        card = Image.open(
            BytesIO(
                render_card(
                    "NAI 原生协议",
                    "请求将使用 NovelAI 原生生图接口。",
                    status="READY",
                    details=[("协议", "novelai"), ("模型", "nai-diffusion-5-full")],
                )
            )
        )
        self.assertEqual(card.format, "PNG")
        self.assertEqual(card.width, 960)
        self.assertGreaterEqual(card.height, 340)

    def test_sqlite_tag_lookup_uses_configured_read_only_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "tags.sqlite3"
            connection = sqlite3.connect(database_path)
            connection.execute(
                """CREATE TABLE catalog_tags (
                    name TEXT, display_name TEXT, category INTEGER, post_count INTEGER,
                    deprecated INTEGER, description TEXT, aliases TEXT, source TEXT, source_url TEXT
                )"""
            )
            connection.execute(
                "CREATE VIRTUAL TABLE catalog_search USING fts5(name, display_name, description, aliases, tokenize='trigram')"
            )
            cursor = connection.execute(
                """INSERT INTO catalog_tags VALUES (
                    'blue_hair', '蓝发', 0, 123456, 0, 'blue hair color', 'azure hair', 'test', 'https://tags.test/blue_hair'
                )"""
            )
            connection.execute(
                "INSERT INTO catalog_search(rowid, name, display_name, description, aliases) VALUES (?, ?, ?, ?, ?)",
                (cursor.lastrowid, "blue_hair", "蓝发", "blue hair color", "azure hair"),
            )
            connection.commit()
            connection.close()
            self.config.database.sqlite_path = str(database_path)

            with patch("asyncio.to_thread", new=_async_immediate):
                rows = _run_immediate(
                    MofoxNaiService(self.config).search_database("blue hair", "stream", 5)
                )

        self.assertEqual(rows[0]["name"], "blue_hair")
        self.assertEqual(rows[0]["display_name"], "蓝发")
        self.assertEqual(rows[0]["category_label"], "general")

    def test_qdrant_lookup_uses_embedding_and_stream_scope(self) -> None:
        self.config.database.qdrant_url = "https://qdrant.test"
        self.config.database.collection = "mofox_images"
        self.config.database.scope_to_stream = True
        self.config.models.embedding_model = "bge-m3"
        self.config.models.embedding_base_url = "https://embeddings.test/v1"
        _Client.responses = [
            _Response({"data": [{"embedding": [0.1, 0.2]}]}),
            _Response(
                {
                    "result": {
                        "points": [
                            {
                                "score": 0.97,
                                "payload": {
                                    "record_id": 9,
                                    "message_id": "image-message",
                                    "stream_id": "target-stream",
                                    "caption": "blue hair",
                                    "tags": "1girl, blue_hair",
                                },
                            }
                        ]
                    }
                }
            ),
        ]

        with patch("plugins.mofox_nai.service.httpx.AsyncClient", _Client):
            rows = _run_immediate(
                MofoxNaiService(self.config).search_database("blue hair", "target-stream", 5)
            )

        self.assertEqual(_Client.requests[0][0], "https://embeddings.test/v1/embeddings")
        self.assertEqual(_Client.requests[1][0], "https://qdrant.test/collections/mofox_images/points/query")
        self.assertEqual(
            _Client.requests[1][1]["json"]["filter"]["must"][0]["key"], "stream_id"
        )
        self.assertEqual(rows[0]["tags"], "1girl, blue_hair")

    def test_danbooru_query_forces_safe_rating(self) -> None:
        _Client.responses = [
            _Response(
                [
                    {
                        "id": 10,
                        "rating": "s",
                        "tag_string": "1girl blue_hair",
                        "file_url": "https://danbooru.test/image.png",
                    }
                ]
            )
        ]
        self.config.danbooru.safe_only = True

        with patch("plugins.mofox_nai.service.httpx.AsyncClient", _Client):
            posts = _run_immediate(
                MofoxNaiService(self.config).search_danbooru(
                    "blue_hair rating:e", 3
                )
            )

        self.assertIn("rating:s", _Client.requests[0][1]["params"]["tags"])
        self.assertNotIn("rating:e", _Client.requests[0][1]["params"]["tags"])
        self.assertEqual(posts[0]["rating"], "s")

    def test_archive_off_disables_local_image_persistence(self) -> None:
        self.config.archive.enabled = True
        self.config.archive.mode = "off"
        self.assertFalse(archive_is_enabled(self.config))

    def test_archive_off_reads_core_history_image_in_memory(self) -> None:
        self.config.archive.enabled = True
        self.config.archive.mode = "off"
        message = SimpleNamespace(
            message_id="history-image",
            stream_id="history-stream",
            platform="qq",
            sender_id="sender",
            sender_name="user",
            sender_role="user",
            processed_plain_text="[图片]",
            content="",
            time=123.0,
        )

        with (
            patch(
                "plugins.mofox_nai.archive.get_stream_messages",
                new=AsyncMock(return_value=[message]),
            ) as get_messages,
            patch(
                "plugins.mofox_nai.archive.message_images",
                return_value=[(b"history-image-bytes", ".png")],
            ),
            patch("asyncio.to_thread", new=_async_immediate),
        ):
            rows = _run_immediate(
                list_core_image_messages("history-stream", limit=10)
            )

        get_messages.assert_awaited_once_with("history-stream", limit=10)
        self.assertEqual(rows[0]["message_id"], "history-image")
        self.assertEqual(rows[0]["_images"], [(b"history-image-bytes", ".png")])
        self.assertFalse(archive_is_enabled(self.config))

    def test_watermark_supports_all_four_corners(self) -> None:
        font_path = next(
            (path for path in _font_candidates("", "Noto Sans SC") if path.is_file()),
            None,
        )
        if font_path is None:
            self.skipTest("没有已安装的 Noto Sans SC 字体")
        source = BytesIO()
        Image.new("RGB", (320, 200), "#101820").save(source, format="PNG")
        original = source.getvalue()
        self.config.watermark.enabled = True
        self.config.watermark.text = "Lycoris radiata"
        self.config.watermark.font_path = str(font_path)

        outputs = []
        for position in ("bottom_left", "bottom_right", "top_left", "top_right"):
            self.config.watermark.position = position
            result = apply_watermark(original, self.config)
            self.assertNotEqual(result, original)
            with Image.open(BytesIO(result)) as rendered:
                self.assertEqual(rendered.size, (320, 200))
            outputs.append(result)

        self.assertEqual(len(set(outputs)), 4)


if __name__ == "__main__":
    unittest.main()
