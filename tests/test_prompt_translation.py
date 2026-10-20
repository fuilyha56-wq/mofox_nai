"""Prompt translation behavior for NAI 4.5 and V5."""

from __future__ import annotations

import base64
import inspect
import unittest
from unittest.mock import patch

from plugins.mofox_nai.config import MofoxNaiConfig
from plugins.mofox_nai.service import MofoxNaiService


def _run_immediate(coroutine):
    """Drive coroutines that only await immediate mocked operations."""

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
            raise AssertionError(f"Unexpected async suspension in unit test: {pending!r}")


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
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


class PromptTranslationTests(unittest.TestCase):
    def setUp(self) -> None:
        _Client.requests = []
        _Client.responses = []
        self.config = MofoxNaiConfig()
        self.config.models.base_url = "http://models.test/v1"
        self.config.models.vision_model = "vision-translator"
        self.config.generation.prompt_model = ""

    def _translation_response(self, content: str) -> _Response:
        return _Response({"choices": [{"message": {"content": content}}]})

    def test_direct_tags_bypass_translation_model(self) -> None:
        service = MofoxNaiService(self.config)
        with patch("plugins.mofox_nai.service.httpx.AsyncClient", _Client):
            result = _run_immediate(service.prepare_prompt("1girl, blue_hair, looking at viewer"))
        self.assertEqual(result, "1girl, blue_hair, looking at viewer")
        self.assertEqual(_Client.requests, [])

    def test_nai_45_always_requests_tag_translation(self) -> None:
        self.config.generation.model = "nai-diffusion-4-5-full"
        self.config.generation.prompt_format = "natural_language"
        _Client.responses = [self._translation_response("1girl, blue_hair, outdoors")]
        service = MofoxNaiService(self.config)

        with patch("plugins.mofox_nai.service.httpx.AsyncClient", _Client):
            result = _run_immediate(service.prepare_prompt("一个蓝头发女孩站在户外"))

        self.assertEqual(result, "1girl, blue_hair, outdoors")
        request = _Client.requests[0][1]["json"]
        self.assertEqual(request["model"], "vision-translator")
        self.assertIn("Never return prose", request["messages"][0]["content"])

    def test_nai_v5_auto_allows_translator_to_choose_format(self) -> None:
        self.config.generation.model = "nai-diffusion-5-full"
        _Client.responses = [self._translation_response("A girl with blue hair standing outdoors.")]
        service = MofoxNaiService(self.config)

        with patch("plugins.mofox_nai.service.httpx.AsyncClient", _Client):
            result = _run_immediate(service.prepare_prompt("一个蓝头发女孩站在户外"))

        self.assertEqual(result, "A girl with blue hair standing outdoors.")
        instruction = _Client.requests[0][1]["json"]["messages"][0]["content"]
        self.assertIn("This is an NAI V5 model", instruction)
        self.assertIn("natural-language image description", instruction)
        self.assertIn("NovelAI/Danbooru-style tags", instruction)

    def test_generate_image_sends_translated_prompt(self) -> None:
        self.config.generation.model = "nai-diffusion-5-full"
        image = base64.b64encode(b"test-image-bytes").decode("ascii")
        _Client.responses = [
            self._translation_response("A girl with blue hair outdoors."),
            _Response({"data": [{"b64_json": image}]}),
        ]
        service = MofoxNaiService(self.config)

        with patch("plugins.mofox_nai.service.httpx.AsyncClient", _Client):
            image_data, final_prompt = _run_immediate(service.generate_image("一个蓝头发女孩在户外"))

        self.assertEqual(image_data, b"test-image-bytes")
        self.assertEqual(final_prompt, "A girl with blue hair outdoors.")
        self.assertEqual(_Client.requests[1][1]["json"]["prompt"], final_prompt)

    def test_natural_language_requires_a_configured_text_model(self) -> None:
        self.config.models.vision_model = ""
        service = MofoxNaiService(self.config)
        with self.assertRaisesRegex(ValueError, "prompt_model 或 \\[models\\].vision_model"):
            _run_immediate(service.prepare_prompt("一个蓝头发女孩"))


if __name__ == "__main__":
    unittest.main()
