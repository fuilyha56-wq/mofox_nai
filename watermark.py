"""使用 Noto Sans SC 为图片绘制可选的四角文字水印。"""

# Copyright (C) 2026 Lycoris radiata
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import MofoxNaiConfig


def _font_candidates(configured_path: str, font_family: str) -> list[Path]:
    """列出配置字体文件、字体族名称和常见 Noto Sans SC 安装路径。"""

    candidates: list[Path] = []
    if configured_path.strip():
        candidates.append(Path(configured_path).expanduser())
    primary_family = font_family.split(",", maxsplit=1)[0].strip().strip("'\"")
    if primary_family:
        candidates.append(Path(primary_family))
    candidates.extend(
        [
            Path("C:/Windows/Fonts/NotoSansSC-Regular.otf"),
            Path("C:/Windows/Fonts/NotoSansSC-Regular.ttf"),
            Path("C:/Windows/Fonts/NotoSansSC-VF.ttf"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc"),
            Path("/System/Library/Fonts/Supplemental/NotoSansSC-Regular.otf"),
        ]
    )
    font_directories = [
        Path("C:/Windows/Fonts"),
        Path.home() / "AppData/Local/Microsoft/Windows/Fonts",
        Path("/usr/share/fonts/truetype/noto"),
        Path("/usr/share/fonts/opentype/noto"),
        Path("/usr/share/fonts/google-noto-cjk"),
        Path("/System/Library/Fonts/Supplemental"),
    ]
    for directory in font_directories:
        if directory.is_dir():
            candidates.extend(directory.glob("*Noto*Sans*SC*"))
            candidates.extend(directory.glob("*NotoSansCJK*"))
    return candidates


def apply_watermark(image_data: bytes, config: "MofoxNaiConfig", text: str | None = None) -> bytes:
    """按配置在图片四角添加文字并返回 PNG 字节。"""

    watermark = config.watermark
    label = (text if text is not None else watermark.text).strip()
    if not watermark.enabled or not label:
        return image_data

    from PIL import Image, ImageColor, ImageDraw, ImageFont
    from io import BytesIO

    font_path = next(
        (
            path
            for path in _font_candidates(watermark.font_path, watermark.font_family)
            if path.is_file()
        ),
        None,
    )
    if font_path is None:
        raise FileNotFoundError(
            "未找到 Noto Sans SC 字体；请安装该字体或在 [watermark].font_path 中配置字体文件路径"
        )

    with Image.open(BytesIO(image_data)) as source:
        canvas = source.convert("RGBA")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype(str(font_path), size=max(8, watermark.font_size))
    red, green, blue = ImageColor.getrgb(watermark.color)
    alpha = max(0, min(255, watermark.opacity))
    left, top, right, bottom = draw.textbbox((0, 0), label, font=font, stroke_width=0)
    width, height = right - left, bottom - top
    margin = max(0, watermark.margin)
    x = margin if watermark.position.endswith("left") else canvas.width - width - margin
    y = margin if watermark.position.startswith("top") else canvas.height - height - margin
    x = max(margin, min(x, canvas.width - width - margin))
    y = max(margin, min(y, canvas.height - height - margin))

    draw_args = {
        "font": font,
        "fill": (red, green, blue, alpha),
        "stroke_width": max(1, watermark.font_size // 16) if watermark.shadow else 0,
    }
    if watermark.shadow:
        draw_args["stroke_fill"] = (0, 0, 0, alpha)
    draw.text((x - left, y - top), label, **draw_args)
    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()
