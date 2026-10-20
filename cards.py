"""深色仪表盘风格的聊天结果卡片。"""

# Copyright (C) 2026 Lycoris radiata
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw, ImageFilter, ImageFont


WIDTH = 960
BACKGROUND = "#090D13"
PANEL = "#111821"
PANEL_ALT = "#151D28"
STROKE = "#2B3848"
TEXT = "#F2F3F8"
MUTED = "#9AA8BA"
PINK = "#FF5CA8"
CYAN = "#64D9FF"
GREEN = "#39D9A0"
RED = "#FF668E"


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = (
        [
            "C:/Windows/Fonts/msyhbd.ttc",
            "C:/Windows/Fonts/msyh.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        ]
        if bold
        else [
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    )
    for name in names:
        path = Path(name)
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                pass
    return ImageFont.load_default()


def _wrap(text: str, font: ImageFont.ImageFont, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    lines: list[str] = []
    for paragraph in str(text).splitlines() or [""]:
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for char in paragraph:
            candidate = current + char
            if current and draw.textlength(candidate, font=font) > max_width:
                lines.append(current)
                current = char
            else:
                current = candidate
        lines.append(current)
    return lines or [""]


def render_card(
    title: str,
    body: str = "",
    *,
    subtitle: str = "MoFox NAI · IMAGE STUDIO",
    status: str = "READY",
    details: Sequence[tuple[str, str]] = (),
    accent: str = PINK,
) -> bytes:
    """将短文本和配置项渲染为可在聊天中阅读的 PNG 卡片。"""

    eyebrow_font = _font(15, bold=True)
    title_font = _font(35, bold=True)
    subtitle_font = _font(16)
    body_font = _font(19)
    label_font = _font(14, bold=True)
    value_font = _font(21, bold=True)
    footer_font = _font(13)

    probe = Image.new("RGB", (WIDTH, 64), BACKGROUND)
    probe_draw = ImageDraw.Draw(probe)
    body_lines = _wrap(body, body_font, WIDTH - 112, probe_draw) if body.strip() else []
    detail_rows: list[list[tuple[str, str]]] = [
        list(details[index : index + 2]) for index in range(0, len(details), 2)
    ]
    body_height = len(body_lines) * 30
    height = 300 + body_height + len(detail_rows) * 112
    height = max(340, min(height, 1700))

    image = Image.new("RGB", (WIDTH, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse((-140, -180, 460, 190), fill=(255, 92, 168, 35))
    glow_draw.ellipse((650, -190, 1170, 150), fill=(74, 184, 255, 16))
    image = Image.alpha_composite(image.convert("RGBA"), glow.filter(ImageFilter.GaussianBlur(80))).convert("RGB")
    draw = ImageDraw.Draw(image)

    # Brand tile and masthead
    draw.rounded_rectangle((40, 34, 96, 90), radius=16, fill="#241727", outline=accent, width=2)
    draw.rounded_rectangle((53, 49, 83, 76), radius=7, outline=accent, width=3)
    draw.line((59, 57, 66, 63, 59, 69), fill=accent, width=3)
    draw.line((69, 70, 78, 70), fill=accent, width=3)
    draw.text((118, 35), subtitle.upper(), font=eyebrow_font, fill=accent)
    draw.text((118, 57), title[:64], font=title_font, fill=TEXT)
    status_width = int(draw.textlength(status[:24], font=label_font)) + 28
    status_left = WIDTH - 42 - status_width
    draw.rounded_rectangle((status_left, 56, WIDTH - 42, 86), radius=9, fill="#151F2A", outline=STROKE, width=1)
    draw.ellipse((status_left + 10, 67, status_left + 18, 75), fill=GREEN if status.upper() in {"READY", "OK", "完成", "已更新"} else accent)
    draw.text((status_left + 24, 63), status[:24], font=label_font, fill=TEXT)
    subtitle_y = max(101, 63 + int(title_font.size * 1.2))
    draw.text((118, subtitle_y), "聊天图片 · 生成 · 参考 · 绘制", font=subtitle_font, fill=MUTED)
    draw.line((40, 144, WIDTH - 40, 144), fill=STROKE, width=1)

    y = 170
    if body_lines:
        panel_bottom = y + max(95, 57 + body_height)
        draw.rounded_rectangle((40, y, WIDTH - 40, panel_bottom), radius=16, fill=PANEL, outline=STROKE, width=1)
        draw.text((60, y + 16), "RESPONSE", font=label_font, fill=accent)
        text_y = y + 42
        for line in body_lines:
            draw.text((60, text_y), line, font=body_font, fill=TEXT)
            text_y += 30
        y = panel_bottom + 12

    for row in detail_rows:
        gap = 14
        tile_width = (WIDTH - 80 - gap) // 2
        row_bottom = y + 100
        for index, (label, value) in enumerate(row):
            left = 40 + index * (tile_width + gap)
            right = left + tile_width
            draw.rounded_rectangle((left, y, right, row_bottom), radius=14, fill=PANEL_ALT, outline=STROKE, width=1)
            draw.rectangle((left + 1, y + 13, left + 4, row_bottom - 13), fill=accent)
            draw.text((left + 18, y + 15), str(label)[:40], font=label_font, fill=MUTED)
            value_lines = _wrap(str(value), value_font, tile_width - 40, draw)
            value_y = y + 41
            for line in value_lines[:2]:
                draw.text((left + 18, value_y), line, font=value_font, fill=TEXT)
                value_y += 27
        y = row_bottom + 12

    footer_y = height - 34
    draw.line((40, footer_y - 12, WIDTH - 40, footer_y - 12), fill=STROKE, width=1)
    draw.text((42, footer_y), "MOFOX NAI  /  CREATIVE CONSOLE", font=footer_font, fill=MUTED)
    draw.text((WIDTH - 42 - int(draw.textlength("IMAGE WORKFLOW", font=footer_font)), footer_y), "IMAGE WORKFLOW", font=footer_font, fill=accent)

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
