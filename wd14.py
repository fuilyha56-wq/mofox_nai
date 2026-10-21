"""Local WD14 ONNX image tagging."""

# Copyright (C) 2026 Lycoris radiata
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import csv
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageOps


def _resolve_model_files(model_path: str, tags_path: str) -> tuple[Path, Path]:
    """Resolve a model file or model directory and its matching label CSV."""

    raw_model_path = model_path.strip()
    if not raw_model_path:
        raise ValueError("WD14 反推需要配置 [models].wd14_model_path")

    model = Path(raw_model_path).expanduser()
    if model.is_dir():
        model = model / "model.onnx"
    if not model.is_file():
        raise FileNotFoundError(f"找不到 WD14 ONNX 模型文件：{model}")

    raw_tags_path = tags_path.strip()
    tags = (
        Path(raw_tags_path).expanduser()
        if raw_tags_path
        else model.parent / "selected_tags.csv"
    )
    if not tags.is_file():
        raise FileNotFoundError(
            f"找不到 WD14 标签文件：{tags}；请配置与模型版本匹配的 selected_tags.csv"
        )
    return model.resolve(), tags.resolve()


@lru_cache(maxsize=2)
def _load_session(model_path: str) -> Any:
    """Load and cache one CPU ONNX Runtime session per model path."""

    try:
        import onnxruntime
    except ImportError as error:
        raise RuntimeError(
            "WD14 反推需要 onnxruntime；请安装插件依赖后重启 MoFox"
        ) from error
    return onnxruntime.InferenceSession(model_path, providers=["CPUExecutionProvider"])


def _load_tags(tags_path: Path) -> list[tuple[str, int]]:
    """Read tag names and WD14 category IDs from selected_tags.csv."""

    tags: list[tuple[str, int]] = []
    with tags_path.open("r", encoding="utf-8-sig", newline="") as tag_file:
        reader = csv.DictReader(tag_file)
        if not reader.fieldnames:
            raise ValueError(f"WD14 标签 CSV 没有表头：{tags_path}")
        fields = {field.strip().lower(): field for field in reader.fieldnames if field}
        name_field = fields.get("name")
        category_field = fields.get("category")
        if not name_field or not category_field:
            raise ValueError("WD14 selected_tags.csv 必须包含 name 和 category 列")
        for row in reader:
            name = (row.get(name_field) or "").strip()
            try:
                category = int(row.get(category_field, ""))
            except (TypeError, ValueError):
                continue
            if name:
                tags.append((name, category))
    if not any(category in {0, 4} for _, category in tags):
        raise ValueError("WD14 标签 CSV 中没有普通 Tag 或角色 Tag")
    return tags


def _shape_dimension(value: Any, fallback: int = 448) -> int:
    return value if isinstance(value, int) and value > 0 else fallback


def _prepare_image(image_data: bytes, input_shape: list[Any]) -> np.ndarray:
    """Resize onto WD14's white square canvas and return an NHWC/NCHW tensor."""

    if len(input_shape) != 4:
        raise ValueError(f"无法识别 WD14 模型的输入形状：{input_shape}")
    if input_shape[-1] == 3:
        height = _shape_dimension(input_shape[1])
        width = _shape_dimension(input_shape[2])
        channels_first = False
    elif input_shape[1] == 3:
        height = _shape_dimension(input_shape[2])
        width = _shape_dimension(input_shape[3])
        channels_first = True
    else:
        raise ValueError(f"WD14 模型必须接收 3 通道图片，实际输入形状：{input_shape}")

    with Image.open(BytesIO(image_data)) as opened:
        image = ImageOps.exif_transpose(opened)
        if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            image = Image.alpha_composite(background, rgba).convert("RGB")
        else:
            image = image.convert("RGB")

    ratio = min(width / image.width, height / image.height)
    resized_size = (
        max(1, round(image.width * ratio)),
        max(1, round(image.height * ratio)),
    )
    image = image.resize(resized_size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    offset = ((width - resized_size[0]) // 2, (height - resized_size[1]) // 2)
    canvas.paste(image, offset)

    # SmilingWolf's WD14 ONNX exports expect float32 BGR pixels in [0, 255].
    pixels = np.asarray(canvas, dtype=np.float32)[:, :, ::-1]
    if channels_first:
        pixels = pixels.transpose(2, 0, 1)
    return np.ascontiguousarray(np.expand_dims(pixels, axis=0))


def tag_image(
    image_data: bytes,
    model_path: str,
    tags_path: str = "",
    general_threshold: float = 0.35,
    character_threshold: float = 0.85,
    *,
    session: Any | None = None,
) -> str:
    """Infer Danbooru-style general and character tags for one image."""

    model_file, tag_file = _resolve_model_files(model_path, tags_path)
    inference_session = (
        session if session is not None else _load_session(str(model_file))
    )
    model_input = inference_session.get_inputs()[0]
    tensor = _prepare_image(image_data, model_input.shape)
    output_names = [output.name for output in inference_session.get_outputs()]
    probabilities = np.asarray(
        inference_session.run(output_names or None, {model_input.name: tensor})[0]
    )
    if probabilities.ndim == 0 or probabilities.size == 0:
        raise ValueError("WD14 模型没有返回 Tag 概率")
    probabilities = probabilities.reshape(-1)
    if not np.all(np.isfinite(probabilities)):
        raise ValueError("WD14 模型返回了无效的 Tag 概率")
    if float(probabilities.min()) < 0 or float(probabilities.max()) > 1:
        probabilities = 1.0 / (1.0 + np.exp(-np.clip(probabilities, -80, 80)))

    labels = _load_tags(tag_file)
    if len(labels) != probabilities.size:
        raise ValueError(
            f"WD14 模型输出 {probabilities.size} 个标签，但 CSV 中有 {len(labels)} 个可识别标签；"
            "请确认 model.onnx 和 selected_tags.csv 来自同一模型版本"
        )

    character_tags: list[tuple[str, float]] = []
    general_tags: list[tuple[str, float]] = []
    for (name, category), probability in zip(labels, probabilities, strict=True):
        if category not in {0, 4}:
            continue
        threshold = character_threshold if category == 4 else general_threshold
        if probability >= threshold:
            (character_tags if category == 4 else general_tags).append(
                (name, float(probability))
            )
    character_tags.sort(key=lambda item: item[1], reverse=True)
    general_tags.sort(key=lambda item: item[1], reverse=True)
    return ", ".join(name for name, _ in character_tags + general_tags)
