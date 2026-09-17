"""Deterministic 256x128 drawing and native row-lsb framebuffer packing.

Call rendering functions in an executor: font I/O and Pillow are synchronous.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
import voluptuous as vol

WIDTH, HEIGHT = 256, 128
FONT_PATH = Path(__file__).with_name("fonts") / "DejaVuSans.ttf"
BIT_REVERSE = bytes(int(f"{value:08b}"[::-1], 2) for value in range(256))
ICONS = ("clock", "calendar", "thermometer", "humidity", "home")
COLOR = vol.In(("black", "white"))
X = vol.All(int, vol.Range(min=0, max=WIDTH - 1))
Y = vol.All(int, vol.Range(min=0, max=HEIGHT - 1))

BASE = {
    vol.Required("x"): X,
    vol.Required("y"): Y,
    vol.Optional("color", default="black"): COLOR,
}


def _rectangle_bounds(item: dict[str, Any]) -> dict[str, Any]:
    if item["x2"] < item["x"] or item["y2"] < item["y"]:
        raise vol.Invalid("Rectangle x2/y2 must be at or after x/y")
    return item


TEXT_SCHEMA = vol.Schema({
    **BASE,
    vol.Required("type"): "text",
    # HA action templates can yield native numbers, e.g. states('sensor.x').
    vol.Required("text"): vol.All(vol.Any(str, int, float), vol.Coerce(str), vol.Length(max=1024)),
    vol.Optional("size", default=20): vol.All(int, vol.Range(min=8, max=64)),
    vol.Optional("align", default="left"): vol.In(("left", "center", "right")),
    vol.Optional("max_width"): vol.All(int, vol.Range(min=1, max=WIDTH)),
})
LINE_SCHEMA = vol.Schema({
    **BASE,
    vol.Required("type"): "line",
    vol.Required("x2"): X,
    vol.Required("y2"): Y,
    vol.Optional("width", default=1): vol.All(int, vol.Range(min=1, max=8)),
})
RECTANGLE_SCHEMA = vol.All(vol.Schema({
    **BASE,
    vol.Required("type"): "rectangle",
    vol.Required("x2"): X,
    vol.Required("y2"): Y,
    vol.Optional("width", default=1): vol.All(int, vol.Range(min=1, max=8)),
    vol.Optional("filled", default=False): bool,
}), _rectangle_bounds)
ICON_SCHEMA = vol.Schema({
    **BASE,
    vol.Required("type"): "icon",
    vol.Required("name"): vol.In(ICONS),
    vol.Optional("size", default=24): vol.All(int, vol.Range(min=8, max=64)),
})
LAYOUT_SCHEMA = vol.Schema({
    vol.Optional("background", default="white"): COLOR,
    vol.Required("elements"): vol.All(
        [vol.Any(TEXT_SCHEMA, LINE_SCHEMA, RECTANGLE_SCHEMA, ICON_SCHEMA)],
        vol.Length(max=64),
    ),
})


@dataclass(frozen=True)
class RenderedFrame:
    raw: bytes
    png: bytes


def _png(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def from_raw(raw: bytes) -> RenderedFrame:
    if len(raw) != WIDTH * HEIGHT // 8:
        raise ValueError("A framebuffer must contain exactly 4096 bytes")
    image = Image.frombytes("1", (WIDTH, HEIGHT), raw.translate(BIT_REVERSE))
    return RenderedFrame(raw, _png(image))


def preview_svg(raw: bytes) -> str:
    """A pixel-exact inline preview for HA's SVG-enabled options dialog.

    Use paths, not data: URLs (the HA options dialog blocks those). The output
    contains only framebuffer pixels, never user text or external resources.
    """
    if len(raw) != WIDTH * HEIGHT // 8:
        raise ValueError("A framebuffer must contain exactly 4096 bytes")
    paths = []
    for y in range(HEIGHT):
        x = 0
        while x < WIDTH:
            if raw[y * 32 + x // 8] & (1 << (x % 8)):
                x += 1
                continue
            start = x
            while x < WIDTH and not raw[y * 32 + x // 8] & (1 << (x % 8)):
                x += 1
            paths.append(f"M{start} {y}h{x - start}v1h{start - x}z")
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 128" '
            'width="100%" role="img" shape-rendering="crispEdges">'
            '<rect width="256" height="128" fill="white"/>'
            '<path fill="black" d="' + "".join(paths) + '"/></svg>')


def _icon(draw: ImageDraw.ImageDraw, item: dict[str, Any], color: int) -> None:
    x, y, size = item["x"], item["y"], item["size"]
    width = max(1, size // 12)

    def points(*coords):
        return tuple(round((x if i % 2 == 0 else y) + value * (size - 1) / 24)
                     for i, value in enumerate(coords))

    name = item["name"]
    if name == "clock":
        draw.ellipse(points(1, 1, 23, 23), outline=color, width=width)
        draw.line(points(12, 5, 12, 12, 17, 15), fill=color, width=width)
    elif name == "calendar":
        draw.rectangle(points(2, 4, 22, 22), outline=color, width=width)
        draw.line(points(2, 9, 22, 9), fill=color, width=width)
        for column in (7, 17):
            draw.line(points(column, 1, column, 6), fill=color, width=width)
        for row in (13, 18):
            for column in (7, 12, 17):
                draw.rectangle(points(column, row, column + 1, row + 1), fill=color)
    elif name == "thermometer":
        draw.rounded_rectangle(points(8, 1, 16, 18), radius=width * 2,
                               outline=color, width=width)
        draw.ellipse(points(6, 13, 18, 23), outline=color, width=width)
        draw.line(points(12, 7, 12, 19), fill=color, width=width)
    elif name == "humidity":
        draw.polygon(points(12, 1, 3, 14, 4, 20, 8, 23, 16, 23, 20, 20, 21, 14),
                     outline=color, width=width)
        draw.arc(points(7, 12, 17, 20), 10, 100, fill=color, width=width)
    elif name == "home":
        draw.line(points(1, 11, 12, 1, 23, 11), fill=color, width=width)
        draw.line(points(4, 9, 4, 23, 20, 23, 20, 9), fill=color, width=width)
        draw.rectangle(points(9, 15, 15, 23), outline=color, width=width)


def render_layout(layout: dict[str, Any]) -> RenderedFrame:
    layout = LAYOUT_SCHEMA(layout)
    image = Image.new("1", (WIDTH, HEIGHT), int(layout["background"] == "white"))
    draw = ImageDraw.Draw(image)
    fonts = {}
    for item in layout["elements"]:
        color = int(item["color"] == "white")
        kind = item["type"]
        if kind == "text":
            size = item["size"]
            lines = item["text"].split("\n")
            limit = item.get("max_width")
            while True:
                if size not in fonts:
                    fonts[size] = ImageFont.truetype(str(FONT_PATH), size=size)
                font = fonts[size]
                if limit is None or size <= 8 or all(font.getlength(line) <= limit for line in lines):
                    break
                size -= 1
            anchor = {"left": "lt", "center": "mt", "right": "rt"}[item["align"]]
            for index, line in enumerate(lines):
                if limit is not None and font.getlength(line) > limit:
                    while line and font.getlength(line + "…") > limit:
                        line = line[:-1]
                    line = line + "…" if font.getlength("…") <= limit else ""
                draw.text((item["x"], item["y"] + index * (size + 2)), line,
                          font=font, fill=color, anchor=anchor)
        elif kind in ("line", "rectangle"):
            coords = (item["x"], item["y"], item["x2"], item["y2"])
            if kind == "line":
                draw.line(coords, fill=color, width=item["width"])
            else:
                draw.rectangle(coords, outline=color, width=item["width"],
                               fill=color if item["filled"] else None)
        else:
            _icon(draw, item, color)
    return RenderedFrame(image.tobytes().translate(BIT_REVERSE), _png(image))


def clock_layout(now: datetime) -> dict[str, Any]:
    weekday = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")[now.weekday()]
    return {
        "elements": [
            {"type": "icon", "name": "clock", "x": 8, "y": 8, "size": 20},
            {"type": "text", "x": 36, "y": 10, "text": "СЕЙЧАС", "size": 16},
            {"type": "text", "x": 248, "y": 10, "text": weekday, "size": 16, "align": "right"},
            {"type": "line", "x": 8, "y": 35, "x2": 247, "y2": 35},
            {"type": "text", "x": 128, "y": 44, "text": now.strftime("%H:%M"),
             "size": 46, "align": "center"},
            {"type": "text", "x": 128, "y": 103, "text": now.strftime("%d.%m.%Y"),
             "size": 18, "align": "center"},
        ],
    }
