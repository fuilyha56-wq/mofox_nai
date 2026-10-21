"""Offline tests for local WD14 ONNX tagging."""

from __future__ import annotations

import csv
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image

from plugins.mofox_nai.wd14 import _prepare_image, tag_image


class _FakeSession:
    def __init__(self, probabilities: list[float]) -> None:
        self.probabilities = np.asarray([probabilities], dtype=np.float32)
        self.image_tensor: np.ndarray | None = None

    def get_inputs(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(name="input", shape=[1, 4, 4, 3])]

    def get_outputs(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(name="output")]

    def run(
        self, output_names: list[str] | None, inputs: dict[str, np.ndarray]
    ) -> list[np.ndarray]:
        self.image_tensor = inputs["input"]
        return [self.probabilities]


class Wd14TaggerTests(unittest.TestCase):
    def _model_files(self, folder: Path) -> tuple[Path, Path]:
        model = folder / "model.onnx"
        model.write_bytes(b"fake onnx model")
        labels = folder / "selected_tags.csv"
        with labels.open("w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(["tag_id", "name", "category", "count"])
            writer.writerows(
                [
                    [0, "general", 9, 100],
                    [1, "1girl", 0, 100],
                    [2, "pink_hair", 0, 100],
                    [3, "character_name", 4, 100],
                ]
            )
        return model, labels

    def test_tag_image_uses_thresholds_and_ignores_rating_tags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model, _ = self._model_files(Path(directory))
            image = BytesIO()
            Image.new("RGB", (2, 4), "white").save(image, format="PNG")
            session = _FakeSession([0.99, 0.36, 0.9, 0.86])

            tags = tag_image(image.getvalue(), str(model), session=session)

        self.assertEqual(tags, "character_name, pink_hair, 1girl")
        self.assertIsNotNone(session.image_tensor)
        self.assertEqual(session.image_tensor.shape, (1, 4, 4, 3))

    def test_preparation_composites_transparency_on_white_and_converts_rgb_to_bgr(
        self,
    ) -> None:
        image = Image.new("RGBA", (4, 2), (255, 0, 0, 255))
        image.putpixel((3, 0), (0, 0, 0, 0))
        buffer = BytesIO()
        image.save(buffer, format="PNG")

        tensor = _prepare_image(buffer.getvalue(), [1, 4, 4, 3])

        self.assertEqual(tensor.shape, (1, 4, 4, 3))
        self.assertTrue(np.array_equal(tensor[0, 0, 0], [255, 255, 255]))
        self.assertTrue(np.array_equal(tensor[0, 1, 0], [0, 0, 255]))
        self.assertTrue(np.array_equal(tensor[0, 1, 3], [255, 255, 255]))

    def test_missing_model_files_report_configuration_hint(self) -> None:
        with self.assertRaisesRegex(ValueError, "wd14_model_path"):
            tag_image(b"image", "")


if __name__ == "__main__":
    unittest.main()
