from datetime import datetime
from io import BytesIO
import re
from xml.etree import ElementTree

from PIL import Image
import pytest
import voluptuous as vol

from custom_components.gtag_ble_test.render import ICONS, clock_layout, from_raw, preview_svg, render_layout


def test_pixel_packing_matches_lcd_and_preview():
    frame = render_layout({"elements": [
        {"type": "line", "x": 0, "y": 0, "x2": 0, "y2": 0},
        {"type": "line", "x": 7, "y": 0, "x2": 8, "y2": 0},
        {"type": "line", "x": 255, "y": 127, "x2": 255, "y2": 127},
    ]})
    assert frame.raw[:2] == bytes([0x7e, 0xfe])
    assert frame.raw[-1] == 0x7f
    assert len(frame.raw) == 4096
    image = Image.open(BytesIO(frame.png))
    for y in range(128):
        for x in range(256):
            assert bool(frame.raw[y * 32 + x // 8] & (1 << (x % 8))) == bool(image.getpixel((x, y)))
    assert from_raw(frame.raw).png == frame.png


def test_cyrillic_and_all_icons():
    cyrillic = render_layout({"elements": [
        {"type": "text", "x": 8, "y": 8, "text": "Привет, мир! Ёжик", "size": 20},
        *[{"type": "icon", "name": name, "x": 8 + i * 45, "y": 64, "size": 32}
          for i, name in enumerate(ICONS)],
    ]})
    assert len(cyrillic.raw) == 4096
    assert cyrillic.raw != b"\xff" * 4096
    # Distinct Cyrillic characters must not fall back to the same missing glyph.
    first = render_layout({"elements": [{"type": "text", "x": 0, "y": 0, "text": "Ж"}]})
    second = render_layout({"elements": [{"type": "text", "x": 0, "y": 0, "text": "Я"}]})
    assert first.raw != second.raw


def test_clock_changes_at_minute_and_day_boundaries():
    before = render_layout(clock_layout(datetime(2026, 9, 17, 23, 59)))
    same_minute = render_layout(clock_layout(datetime(2026, 9, 17, 23, 59, 50)))
    after = render_layout(clock_layout(datetime(2026, 9, 18, 0, 0)))
    assert before == same_minute
    assert before.raw != after.raw


def test_multiline_text_keeps_alignment_and_line_spacing():
    base = {"type": "text", "x": 128, "y": 8, "size": 20, "align": "center"}
    combined = render_layout({"elements": [{**base, "text": "Две\nстроки"}]})
    separate = render_layout({"elements": [
        {**base, "text": "Две"}, {**base, "y": 30, "text": "строки"},
    ]})
    assert combined == separate


def test_preview_paths_match_every_display_pixel():
    frame = render_layout(clock_layout(datetime(2026, 9, 17, 12, 34)))
    root = ElementTree.fromstring(preview_svg(frame.raw))
    paths = root.find("{http://www.w3.org/2000/svg}path").attrib["d"]
    reconstructed = bytearray(b"\xff" * 4096)
    for x, y, length, back in re.findall(r"M(\d+) (\d+)h(\d+)v1h-(\d+)z", paths):
        x, y, length = int(x), int(y), int(length)
        assert int(back) == length
        for offset in range(x, x + length):
            reconstructed[y * 32 + offset // 8] &= ~(1 << (offset % 8))
    assert bytes(reconstructed) == frame.raw


def test_long_text_fits_its_column():
    frame = render_layout({"elements": [{
        "type": "text", "x": 8, "y": 8, "text": "Очень длинная подпись " * 20,
        "size": 32, "max_width": 100,
    }]})
    image = Image.open(BytesIO(frame.png))
    assert image.crop((8, 8, 108, 48)).getextrema() == (0, 255)
    assert image.crop((108, 0, 256, 128)).getextrema() == (255, 255)


@pytest.mark.parametrize("layout", [
    {"elements": [{"type": "text", "x": 256, "y": 0, "text": "bad"}]},
    {"elements": [{"type": "text", "x": 0, "y": 0, "text": "bad", "size": 500}]},
    {"elements": [{"type": "icon", "x": 0, "y": 0, "name": "unknown"}]},
    {"elements": [{"type": "text", "x": 0, "y": 0, "text": "X" * 1025}]},
    {"elements": [{"type": "rectangle", "x": 20, "y": 20, "x2": 10, "y2": 10}]},
])
def test_reject_invalid_layouts(layout):
    with pytest.raises(vol.Invalid):
        render_layout(layout)
