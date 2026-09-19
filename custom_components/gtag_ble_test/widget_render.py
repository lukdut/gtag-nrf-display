"""Monochrome weather symbols and history plots, drawn in the executor."""
from __future__ import annotations

from itertools import groupby

from PIL import ImageDraw

from .render import ICONS, render_layout


def weather_icon(draw, name, points, color, width):
    if name == "sunny":
        draw.ellipse(points(7, 7, 17, 17), outline=color, width=width)
        for ray in ((12, 0, 12, 4), (12, 20, 12, 24), (0, 12, 4, 12), (20, 12, 24, 12),
                    (3, 3, 6, 6), (18, 18, 21, 21), (3, 21, 6, 18), (18, 6, 21, 3)):
            draw.line(points(*ray), fill=color, width=width)
    elif name == "clear-night":
        draw.arc(points(2, 1, 23, 23), 45, 285, fill=color, width=width)
        draw.arc(points(10, -2, 26, 17), 70, 270, fill=color, width=width)
    elif name in ("windy", "windy-variant", "fog"):
        for y in (7, 12, 17):
            draw.line(points(2, y, 21, y), fill=color, width=width)
        if name != "fog":
            draw.arc(points(15, 2, 23, 10), 180, 350, fill=color, width=width)
    elif name in ("unknown", "exceptional"):
        draw.polygon(points(12, 1, 1, 22, 23, 22), outline=color, width=width)
        draw.line(points(12, 8, 12, 14), fill=color, width=width)
        draw.ellipse(points(11, 17, 13, 19), fill=color)
    else:
        if name == "partlycloudy":
            draw.arc(points(1, 1, 14, 14), 170, 355, fill=color, width=width)
        draw.arc(points(6, 5, 18, 17), 180, 340, fill=color, width=width)
        draw.arc(points(1, 10, 11, 19), 100, 270, fill=color, width=width)
        draw.arc(points(15, 10, 23, 19), 265, 90, fill=color, width=width)
        draw.line(points(5, 19, 19, 19), fill=color, width=width)
        if name in ("rainy", "pouring", "lightning-rainy", "snowy-rainy"):
            for x in (5, 12, 19):
                draw.line(points(x, 21, x - 1, 24), fill=color, width=width)
        if name in ("snowy", "snowy-rainy", "hail"):
            for x in (6, 17):
                draw.line(points(x - 2, 22, x + 2, 22), fill=color, width=width)
                draw.line(points(x, 20, x, 24), fill=color, width=width)
        if name in ("lightning", "lightning-rainy"):
            draw.line(points(13, 13, 10, 20, 14, 20, 11, 24), fill=color, width=width)


def text(x, y, value, size=16, width=240, align="left"):
    return {"type": "text", "x": x, "y": y, "text": str(value), "size": size,
            "max_width": width, "align": align}


def icon(x, y, condition, size):
    return {"type": "icon", "x": x, "y": y, "name": condition if condition in ICONS else "unknown", "size": size}


def format_number(value, decimals="original"):
    if value is None:
        return "—"
    if decimals != "original":
        return f"{round(value, int(decimals)) + 0.0:.{int(decimals)}f}"
    return f"{value:g}"


def graph_segments(points, start, end, left=44, right=247):
    """Keep gaps and the min/max of every pixel column, including narrow peaks."""
    segments, current = [], []
    previous = None
    for stamp, value in sorted(points, key=lambda point: point[0]):
        if stamp < start:
            previous = value
            continue
        if stamp > end:
            break
        if not current and previous is not None:
            current.append((left, previous))
            previous = None
        if value is None:
            if current:
                segments.append(current)
            current = []
        else:
            x = max(left, min(right, round(left + (right - left) * (stamp - start) / (end - start))))
            current.append((x, value))
    if current:
        segments.append(current)
    reduced = []
    for segment in segments:
        output = []
        for x, values in groupby(segment, key=lambda p: p[0]):
            values = list(values)
            indices = {0, len(values) - 1, min(range(len(values)), key=lambda i: values[i][1]),
                       max(range(len(values)), key=lambda i: values[i][1])}
            output.extend(values[i] for i in sorted(indices))
        reduced.append(output)
    return reduced


def draw_widget(image, item, data):
    elements = [text(8, 6, data["label"], 16, 175),
                text(248, 8, data["now"].strftime("%H:%M"), 14, 60, "right")]
    if item["type"] == "weather":
        unit = data["unit"]
        value = format_number(data["temperature"], "0")
        apparent = data.get("apparent_temperature")
        elements += [icon(8, 27, data["condition"], 36),
                     text(57, 26 if apparent is not None else 29,
                          value + (f" {unit}" if data["temperature"] is not None else ""),
                          28 if apparent is not None else 32, 147),
                     text(248, 44, format_number(data["humidity"], "0") + ("%" if data["humidity"] is not None else ""), 14, 44, "right"),
                     {"type": "line", "x": 8, "y": 66, "x2": 247, "y2": 66}]
        if apparent is not None:
            elements.append(text(57, 53, "Ощущается: " + format_number(apparent, "0")
                                 + (f" {unit}" if unit else ""), 12, 147))
        for i in range(6):
            entry = data["forecast"][i] if i < len(data["forecast"]) else {}
            center = 24 + 42 * i
            elements += [text(center, 73, entry["time"].strftime("%H:%M") if entry else "—", 12, 40, "center"),
                         icon(center - 10, 89, entry.get("condition"), 20),
                         text(center, 111, format_number(entry.get("temperature"), "0")
                              + ("°" if entry.get("temperature") is not None else ""), 13, 40, "center")]
    else:
        unit = item["unit"].strip()
        unit = ("" if unit == "-" else unit) if unit else data["unit"]
        value = format_number(data["value"], item["decimals"])
        if item["decimals"] == "original" and data["value"] is not None:
            value = str(data.get("state", value))[:128]
        unit = str(unit)[:16]
        elements.append(text(248, 27, value + (f" {unit}" if data["value"] is not None and unit else ""), 26, 240, "right"))
        segments = graph_segments(data["points"], data["start"].timestamp(), data["now"].timestamp())
        values = [value for segment in segments for _, value in segment]
        if values:
            low, high = min(values), max(values)
            elements += [text(39, 57, f"{high:.4g}", 10, 38, "right"),
                         text(39, 97, f"{low:.4g}", 10, 38, "right")]
        else:
            elements.append(text(145, 75, "—", 20, 185, "center"))
        time_format = "%H:%M" if item["hours"] < 24 else "%d.%m %H:%M"
        elements += [text(44, 112, data["start"].strftime(time_format), 10, 100),
                     text(247, 112, data["now"].strftime(time_format), 10, 100, "right"),
                     text(8, 112, f"{item['hours']}h" + ("…" if data["truncated"] else ""), 9, 33)]
    frame = render_layout({"elements": elements})
    from PIL import Image
    from .render import BIT_REVERSE
    image.paste(Image.frombytes("1", (256, 128), frame.raw.translate(BIT_REVERSE)))
    if item["type"] == "history_graph":
        draw = ImageDraw.Draw(image)
        draw.line((44, 56, 44, 107, 247, 107), fill=0)
        if values:
            span = high / 2 - low / 2
            for segment in segments:
                pixels = [(x, 81 if not span else round(104 - 46 * ((value / 2 - low / 2) / span)))
                          for x, value in segment]
                if len(pixels) > 1:
                    draw.line(pixels, fill=0, width=1)
                else:
                    draw.point(pixels[0], fill=0)
