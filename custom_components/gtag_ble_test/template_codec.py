"""Immutable device templates: codec 2/id 1 and codec 3/ids 2–4.

Payload: template:u8, revision:u8, then (UTF-8 length:u16le, text) fields.
No state lives across packets. Fonts and pixels are shared with the C++ renderer.
"""
from functools import lru_cache
from pathlib import Path
import struct

TEMPLATE_ID = 1
TEMPLATE_REVISION = 1
MAX_CHARS = 256
SLOTS = ((128, 13, 28, 240, "center"), (8, 54, 16, 112, "left"),
         (140, 54, 16, 108, "left"), (8, 84, 30, 112, "left"), (140, 84, 30, 108, "left"))
TEMPLATES = {
    1: (SLOTS, (0, 3, 4, 5, 6), ((1, (8, 42, 247, 42)), (2, (128, 53, 128, 119)))),
    2: (((248, 10, 16, None, "right"), (128, 44, 46, None, "center"),
         (128, 103, 18, None, "center")), (2, 4, 5), ((3, (8, 35, 247, 35)),)),
    3: (((8, 13, 28, 112, "left"), (248, 23, 16, 122, "right"), *SLOTS[1:]),
        (0, 1, 4, 5, 6, 7), ((2, (8, 42, 247, 42)), (3, (128, 53, 128, 119)))),
    4: (((128, 10, 20, 240, "center"), (128, 64, 44, 240, "center")),
        (0, 2), ((1, (8, 38, 247, 38)),)),
}
# Frozen 20x20 clock icon, row-major black bits, as drawn by render._icon.
CLOCK_ICON = bytes.fromhex("000000f80160600001080804414020040422404002042440400204248041022044002004008200101080000606801f000000")
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


@lru_cache(maxsize=1)
def _weekdays():
    data = (Path(__file__).with_name("fonts") / "template_clock_v1.bin").read_bytes()
    if len(data) != 455:
        raise ValueError("Unsupported clock font")
    return data


@lru_cache(maxsize=1)
def _font():
    data = (Path(__file__).with_name("fonts") / "template_v1.bin").read_bytes()
    magic, minimum, maximum, count = struct.unpack_from("<8sBBH", data)
    if (magic, minimum, maximum) != (b"GTFONT1\0", 8, 30):
        raise ValueError("Unsupported template font")
    points = struct.unpack_from(f"<{count}H", data, 12)
    fonts = {}
    for size in range(8, 31):
        records, pairs = struct.unpack_from("<II", data, 12 + count * 2 + (size - 8) * 8)
        glyphs = [struct.unpack_from("<IBBbbH", data, records + index * 10) for index in range(count)]
        pair_count, = struct.unpack_from("<H", data, pairs)
        kern = dict(struct.unpack_from("<Ih", data, pairs + 2 + index * 6) for index in range(pair_count))
        fonts[size] = (glyphs, kern)
    large = (Path(__file__).with_name("fonts") / "template_large_v1.bin").read_bytes()
    if struct.unpack_from("<8sBBH", large) != (b"GTFONT1\0", 44, 46, count) or large[12:12 + count * 2] != data[12:12 + count * 2]:
        raise ValueError("Unsupported large template font")
    for size in (44, 46):
        records, pairs = struct.unpack_from("<II", large, 12 + count * 2 + (size - 44) * 8)
        glyphs = []
        for index in range(count):
            offset, *metrics = struct.unpack_from("<IBBbbH", large, records + index * 10)
            glyphs.append((offset + len(data), *metrics))
        pair_count, = struct.unpack_from("<H", large, pairs)
        kern = dict(struct.unpack_from("<Ih", large, pairs + 2 + index * 6) for index in range(pair_count))
        fonts[size] = (glyphs, kern)
    return data + large, {chr(point): index for index, point in enumerate(points)}, fonts


def encode_strings(strings: list[str], template_id: int = TEMPLATE_ID) -> bytes:
    if template_id not in TEMPLATES or len(strings) != len(TEMPLATES[template_id][0]):
        raise ValueError("Incorrect template field count")
    payload = bytearray((template_id, TEMPLATE_REVISION))
    for text in strings:
        if len(text) > MAX_CHARS:
            raise ValueError("Template text too long")
        encoded = text.encode("utf-8")
        payload.extend(struct.pack("<H", len(encoded)))
        payload.extend(encoded)
    if len(payload) > 4096:
        raise ValueError("Template payload too long")
    return bytes(payload)


def codec_for_payload(payload: bytes) -> int:
    if len(payload) < 2 or payload[0] not in TEMPLATES or payload[1] != TEMPLATE_REVISION:
        raise ValueError("Unsupported template or revision")
    return 2 if payload[0] == 1 else 3


def decode_strings(payload: bytes) -> list[str]:
    codec_for_payload(payload)
    if len(payload) > 4096:
        raise ValueError("Template payload too long")
    strings = []
    offset = 2
    for _ in TEMPLATES[payload[0]][0]:
        if offset + 2 > len(payload):
            raise ValueError("Truncated template")
        length, = struct.unpack_from("<H", payload, offset)
        offset += 2
        if length > 1024 or offset + length > len(payload):
            raise ValueError("Invalid template string length")
        text = payload[offset:offset + length].decode("utf-8", errors="strict")
        if len(text) > MAX_CHARS:
            raise ValueError("Template text too long")
        strings.append(text)
        offset += length
    if offset != len(payload):
        raise ValueError("Trailing template data")
    return strings


def render_payload(payload: bytes) -> bytes:
    strings = decode_strings(payload)
    data, points, fonts = _font()
    count = len(points)
    raw = bytearray(b"\xff" * 4096)

    def pixel(x, y):
        if 0 <= x < 256 and 0 <= y < 128:
            raw[y * 32 + x // 8] &= ~(1 << (x % 8))

    slots, _, lines = TEMPLATES[payload[0]]
    for _, (x1, y1, x2, y2) in lines:
        for x in range(x1, x2 + 1):
            for y in range(y1, y2 + 1):
                pixel(x, y)
    if payload[0] == 2:
        for bit in range(400):
            if CLOCK_ICON[bit // 8] & (1 << (bit % 8)):
                pixel(8 + bit % 20, 8 + bit // 20)
        if strings[0] not in WEEKDAYS:
            raise ValueError("Unsupported weekday")
        weekday = _weekdays()[WEEKDAYS.index(strings[0]) * 65:][:65]
        for bit in range(520):
            if weekday[bit // 8] & (1 << (bit % 8)):
                pixel(222 + bit % 26, 10 + bit // 26)
        strings = ["СЕЙЧАС", *strings[1:]]
        slots = ((36, 10, 16, None, "left"), *slots[1:])
    for text, (x, y, size, limit, align) in zip(strings, slots, strict=True):
        try:
            indexes = [points[char] for char in text]
        except KeyError as err:
            raise ValueError("Character not in template font") from err

        def measure(items):
            return sum(glyphs[index][5] for index in items) + sum(
                kern.get(first * count + second, 0) for first, second in zip(items, items[1:]))

        while True:
            if size not in fonts:
                raise ValueError("Intermediate large font requires bitmap transfer")
            glyphs, kern = fonts[size]
            if any(glyphs[index][5] == 65535 for index in indexes):
                raise ValueError("Character unavailable at this size")
            advance = measure(indexes)
            if limit is None or advance <= limit * 64 or size == 8:
                break
            size -= 1
        if limit is not None and advance > limit * 64:
            ellipsis = points["…"]
            while indexes and measure([*indexes, ellipsis]) > limit * 64:
                indexes.pop()
            indexes.append(ellipsis)
            advance = measure(indexes)
        if align == "center":
            x -= (advance + 64) // 128
        elif align == "right":
            x -= (advance + 32) // 64
        top = min((glyphs[index][4] for index in indexes if glyphs[index][2]), default=0)
        pen = 0
        for position, index in enumerate(indexes):
            offset, width, height, left, bearing, step = glyphs[index]
            origin = x + (pen + 32) // 64 + left
            for row in range(height):
                for column in range(width):
                    bit = row * width + column
                    if data[offset + bit // 8] & (1 << (bit % 8)):
                        pixel(origin + column, y + bearing - top + row)
            pen += step
            if position + 1 < len(indexes):
                pen += kern.get(index * count + indexes[position + 1], 0)
    return bytes(raw)


def candidate_for_layout(layout: dict, raw: bytes) -> bytes | None:
    """Use native rendering only for this exact geometry AND identical pixels.

    Complex shaping, missing glyphs, modified layouts and future Pillow/font
    differences continue to use ordinary bitmap delivery without visual changes.
    """
    elements = layout["elements"]
    if layout["background"] != "white":
        return None
    for template_id in TEMPLATES:
        payload = _candidate(template_id, elements, raw)
        if payload is not None:
            return payload
    return None


def _candidate(template_id, elements, raw):
    slots, indexes, lines = TEMPLATES[template_id]
    if len(elements) != len(slots) + len(lines) + (2 if template_id == 2 else 0):
        return None
    if template_id == 2 and (elements[0] != dict(type="icon", name="clock", x=8, y=8, size=20, color="black") or
                            elements[1] != dict(type="text", x=36, y=10, size=16, align="left", color="black", text="СЕЙЧАС")):
        return None
    strings = []
    for index, (x, y, size, width, align) in zip(indexes, slots, strict=True):
        element = elements[index]
        text = element.get("text", "")
        expected = dict(type="text", x=x, y=y, size=size, align=align, color="black", text=text)
        if width is not None:
            expected["max_width"] = width
        if element != expected:
            return None
        strings.append(text)
    for index, coordinates in lines:
        x, y, x2, y2 = coordinates
        if elements[index] != dict(type="line", x=x, y=y, x2=x2, y2=y2, color="black", width=1):
            return None
    try:
        payload = encode_strings(strings, template_id)
        return payload if render_payload(payload) == raw else None
    except (ValueError, OSError):
        return None
