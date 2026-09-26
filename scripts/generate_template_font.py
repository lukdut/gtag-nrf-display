#!/usr/bin/env python3
"""Freeze monochrome DejaVu glyphs/metrics shared by HA and template codec v1.

Requires Pillow with RAQM. Existing revision bytes are immutable: changing the
font, metrics or rasterizer requires a new template revision/codec contract.
"""
from hashlib import sha256
import argparse
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parents[1]
FONTS = ROOT / "custom_components/gtag_ble_test/fonts"
OUTPUT = FONTS / "template_v1.bin"
HEADER = ROOT / "config/esphome/components/gtag_display/template_font_v1.h"
POINTS = sorted(set(range(32, 127)) | set(range(160, 256)) | set(range(0x400, 0x460)) |
                set(range(0x2080, 0x208A)) | {0x2013, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D,
                                           0x2026, 0x20AC, 0x2116, 0x2212})


def generate(minimum=8, maximum=30) -> bytes:
    from PIL import ImageFont

    count = len(POINTS)
    data = bytearray(struct.pack("<8sBBH", b"GTFONT1\0", minimum, maximum, count))
    data.extend(struct.pack(f"<{count}H", *POINTS))
    table = len(data)
    data.extend(bytes((maximum - minimum + 1) * 8))
    bitmaps = {}
    for size in range(minimum, maximum + 1):
        # Large text that needs intermediate sizes or unsupported glyphs uses
        # bitmap fallback. The clock only needs digits and a colon at size 46.
        supported = set(POINTS) if size <= 44 else (set(map(ord, "0123456789:")) if size == 46 else set())
        font = ImageFont.truetype(str(FONTS / "DejaVuSans.ttf"), size, layout_engine=ImageFont.Layout.RAQM)
        advances = [round(font.getlength(chr(cp)) * 64) for cp in POINTS]
        glyphs = len(data)
        data.extend(bytes(count * 10))
        for index, cp in enumerate(POINTS):
            if cp not in supported:
                struct.pack_into("<IBBbbH", data, glyphs + index * 10, 0, 0, 0, 0, 0, 65535)
                continue
            mask, (left, top) = font.getmask2(chr(cp), mode="1", anchor="ls")
            width, height = mask.size
            pixels = bytes(mask)
            bitmap = bytes(sum((1 << bit) if offset + bit < len(pixels) and pixels[offset + bit] else 0
                               for bit in range(8)) for offset in range(0, len(pixels), 8))
            if bitmap not in bitmaps:
                bitmaps[bitmap] = len(data)
                data.extend(bitmap)
            struct.pack_into("<IBBbbH", data, glyphs + index * 10,
                             bitmaps[bitmap], width, height, left, top, advances[index])
        kern = len(data)
        pairs = []
        for first, cp in enumerate(POINTS):
            if cp not in supported:
                continue
            for second, other in enumerate(POINTS):
                if other not in supported:
                    continue
                delta = round(font.getlength(chr(cp) + chr(other)) * 64) - advances[first] - advances[second]
                if delta:
                    pairs.append((first * count + second, delta))
        data.extend(struct.pack("<H", len(pairs)))
        for key, delta in pairs:
            data.extend(struct.pack("<Ih", key, delta))
        struct.pack_into("<II", data, table + (size - minimum) * 8, glyphs, kern)
        print(f"Size {size}: {len(supported)} glyphs, {len(pairs)} pairs", flush=True)
    return bytes(data)


def compress_bitmap(data: bytes) -> bytes:
    """Per-glyph LZSS: flag bits select literals or (distance, length) pairs."""
    out = bytearray()
    pos = 0
    while pos < len(data):
        flags = len(out)
        out.append(0)
        for bit in range(8):
            if pos == len(data):
                break
            best = distance = 0
            low, end = max(0, pos - 255), pos + 2
            if pos + 3 <= len(data):
                match = data.rfind(data[pos:pos + 3], low, end)
                while match >= low:
                    size = 3
                    while size < 255 and pos + size < len(data) and data[match + size] == data[pos + size]:
                        size += 1
                    if size > best:
                        best, distance = size, pos - match
                    end = match + 2
                    match = data.rfind(data[pos:pos + 3], low, end)
            if best >= 3:
                out[flags] |= 1 << bit
                out.extend((distance, best))
                pos += best
            else:
                out.append(data[pos])
                pos += 1
    return bytes(out)


def compact(data: bytes, sparse=False) -> tuple[bytes, int]:
    """Losslessly pack bitmap bytes and kerning; keep frozen HA assets immutable."""
    count = struct.unpack_from("<H", data, 10)[0]
    sizes = data[9] - data[8] + 1
    table = 12 + count * 2
    packed = bytearray(data[:table + sizes * 8])
    bitmaps = {}
    fonts = []
    glyph_tables = []
    for size in range(sizes):
        glyphs, pairs = struct.unpack_from("<II", data, table + size * 8)
        target = len(packed)
        glyph_tables.append(target)
        indexes = [i for i in range(count) if not sparse or struct.unpack_from("<H", data, glyphs + i * 10 + 8)[0] != 65535]
        if sparse:
            packed.extend(struct.pack("<H", len(indexes)))
        packed.extend(bytes(len(indexes) * (12 if sparse else 10)))
        for position, index in enumerate(indexes):
            offset, width, height, left, top, advance = struct.unpack_from("<IBBbbH", data, glyphs + index * 10)
            bitmap = data[offset:offset + (width * height + 7) // 8]
            if len(bitmap) > 512:
                raise ValueError("Glyph exceeds the MCU scratch buffer")
            if bitmap not in bitmaps:
                compressed = compress_bitmap(bitmap)
                use_compressed = len(compressed) < len(bitmap)
                bitmaps[bitmap] = len(packed) | (0x80000000 if use_compressed else 0)
                packed.extend(compressed if use_compressed else bitmap)
            record = target + (2 + position * 12 if sparse else position * 10)
            if sparse:
                struct.pack_into("<H", packed, record, index)
                record += 2
            struct.pack_into("<IBBbbH", packed, record,
                             bitmaps[bitmap], width, height, left, top, advance)
        pair_count = struct.unpack_from("<H", data, pairs)[0]
        fonts.append(dict(struct.unpack_from("<Ih", data, pairs + 2 + i * 6) for i in range(pair_count)))
    keys = sorted(set().union(*fonts))
    curves = {}
    pairs = len(packed)
    packed.extend(struct.pack("<H", len(keys)))
    for key in keys:
        curve = tuple(font.get(key, 0) for font in fonts)
        curve_id = curves.setdefault(curve, len(curves))
        packed.extend(struct.pack("<IH", key, curve_id))
    curves_offset = len(packed)
    for curve in curves:
        packed.extend(struct.pack(f"<{sizes}h", *curve))
    for size, glyphs in enumerate(glyph_tables):
        struct.pack_into("<II", packed, table + size * 8, glyphs, pairs)
    return bytes(packed), curves_offset


def clock_weekdays() -> bytes:
    """Freeze whole weekday runs: Pillow's monochrome bearings depend on context."""
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(str(FONTS / "DejaVuSans.ttf"), 16, layout_engine=ImageFont.Layout.RAQM)
    result = bytearray()
    for text in ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"):
        image = Image.new("1", (26, 20), 0)
        ImageDraw.Draw(image).text((26, 0), text, font=font, fill=1, anchor="rt")
        pixels = list(image.get_flattened_data())
        result.extend(sum(bool(pixels[i + bit]) << bit for bit in range(8)) for i in range(0, 520, 8))
    return bytes(result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack-only", action="store_true", help="Repack the frozen font without Pillow")
    parser.add_argument("--large", action="store_true", help="Generate the separate codec-3 large font")
    args = parser.parse_args()
    if args.large:
        OUTPUT = FONTS / "template_large_v1.bin"
        HEADER = HEADER.with_name("template_font_large_v1.h")
    data = OUTPUT.read_bytes() if args.pack_only else generate(*( (44, 46) if args.large else (8, 30)))
    if OUTPUT.exists() and OUTPUT.read_bytes() != data:
        raise SystemExit("Font revision 1 differs. Do not overwrite a published revision; use a new revision.")
    if not args.pack_only:
        OUTPUT.write_bytes(data)
    packed, curves_offset = compact(data, sparse=args.large)
    license_text = (FONTS / "LICENSE.txt").read_text()
    header = ["// Generated by scripts/generate_template_font.py; do not edit.",
              f"// Frozen source SHA256: {sha256(data).hexdigest()}",
              "// Lossless storage: LZSS glyphs, shared pair keys and kerning curves.", "/*", license_text, "*/",
              "#pragma once", "#include <cstdint>",
              f"namespace esphome::gtag_display::{HEADER.stem} {{",
              f"inline constexpr uint32_t KERN_CURVES_OFFSET = {curves_offset};",
              "inline constexpr uint8_t DATA[] = {"]
    header.extend("  " + ",".join(f"0x{b:02x}" for b in packed[i:i + 24]) + "," for i in range(0, len(packed), 24))
    header.extend(["};", f"}}  // namespace esphome::gtag_display::{HEADER.stem}", ""])
    HEADER.write_text("\n".join(header))
    if args.large:
        asset = FONTS / "template_clock_v1.bin"
        weekdays = asset.read_bytes() if args.pack_only else clock_weekdays()
        if asset.exists() and asset.read_bytes() != weekdays:
            raise SystemExit("Clock revision 1 differs; use a new revision.")
        if not args.pack_only:
            asset.write_bytes(weekdays)
        lines = ["// Generated by scripts/generate_template_font.py --large; do not edit.",
                 f"// Frozen source SHA256: {sha256(weekdays).hexdigest()}",
                 "#pragma once", "#include <cstdint>",
                 "namespace esphome::gtag_display::template_clock_v1 {", "inline constexpr uint8_t WEEKDAYS[] = {"]
        lines.extend("  " + ",".join(f"0x{b:02x}" for b in weekdays[i:i + 24]) + "," for i in range(0, len(weekdays), 24))
        lines.extend(["};", "}", ""])
        HEADER.with_name("template_clock_v1.h").write_text("\n".join(lines))
    print(f"Font: {len(data)} bytes; MCU storage: {len(packed)} bytes; source SHA256 {sha256(data).hexdigest()}")
