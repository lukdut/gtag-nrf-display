"""Template codec 2: immutable three-values template 1/revision 1.

Payload: template:u8, revision:u8, then five (UTF-8 length:u16le, text) fields:
header, left label, right label, left value, right value. No state lives across
packets. Font metrics and monochrome glyphs are shared with the C++ renderer.
"""
from functools import lru_cache
from pathlib import Path
import struct

TEMPLATE_ID = 1
TEMPLATE_REVISION = 1
MAX_CHARS = 256
SLOTS = ((128, 13, 28, 240, True), (8, 54, 16, 112, False),
         (140, 54, 16, 108, False), (8, 84, 30, 112, False), (140, 84, 30, 108, False))


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
    return data, {chr(point): index for index, point in enumerate(points)}, fonts


def encode_strings(strings: list[str]) -> bytes:
    if len(strings) != 5:
        raise ValueError("Five template strings required")
    payload = bytearray((TEMPLATE_ID, TEMPLATE_REVISION))
    for text in strings:
        if len(text) > MAX_CHARS:
            raise ValueError("Template text too long")
        encoded = text.encode("utf-8")
        payload.extend(struct.pack("<H", len(encoded)))
        payload.extend(encoded)
    if len(payload) > 4096:
        raise ValueError("Template payload too long")
    return bytes(payload)


def decode_strings(payload: bytes) -> list[str]:
    if not 12 <= len(payload) <= 4096 or payload[:2] != bytes((TEMPLATE_ID, TEMPLATE_REVISION)):
        raise ValueError("Unsupported template or revision")
    strings = []
    offset = 2
    for _ in range(5):
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

    for x in range(8, 248):
        pixel(x, 42)
    for y in range(53, 120):
        pixel(128, y)
    for text, (x, y, size, limit, center) in zip(strings, SLOTS, strict=True):
        try:
            indexes = [points[char] for char in text]
        except KeyError as err:
            raise ValueError("Character not in template font") from err

        def measure(items):
            return sum(glyphs[index][5] for index in items) + sum(
                kern.get(first * count + second, 0) for first, second in zip(items, items[1:]))

        while True:
            glyphs, kern = fonts[size]
            advance = measure(indexes)
            if advance <= limit * 64 or size == 8:
                break
            size -= 1
        if advance > limit * 64:
            ellipsis = points["…"]
            while indexes and measure([*indexes, ellipsis]) > limit * 64:
                indexes.pop()
            indexes.append(ellipsis)
            advance = measure(indexes)
        if center:
            x -= (advance + 64) // 128
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
    if layout["background"] != "white" or len(elements) != 7:
        return None
    strings = []
    for index, (x, y, size, width, center) in zip((0, 3, 4, 5, 6), SLOTS, strict=True):
        element = elements[index]
        text = element.get("text", "")
        expected = dict(type="text", x=x, y=y, size=size, max_width=width,
                        align="center" if center else "left", color="black", text=text)
        if element != expected:
            return None
        strings.append(text)
    for index, coordinates in ((1, (8, 42, 247, 42)), (2, (128, 53, 128, 119))):
        x, y, x2, y2 = coordinates
        if elements[index] != dict(type="line", x=x, y=y, x2=x2, y2=y2, color="black", width=1):
            return None
    try:
        payload = encode_strings(strings)
        return payload if render_payload(payload) == raw else None
    except (ValueError, OSError):
        return None
