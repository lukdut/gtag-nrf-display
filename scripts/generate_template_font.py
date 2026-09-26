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


def generate() -> bytes:
    from PIL import ImageFont

    count = len(POINTS)
    data = bytearray(struct.pack("<8sBBH", b"GTFONT1\0", 8, 30, count))
    data.extend(struct.pack(f"<{count}H", *POINTS))
    table = len(data)
    data.extend(bytes(23 * 8))
    bitmaps = {}
    for size in range(8, 31):
        font = ImageFont.truetype(str(FONTS / "DejaVuSans.ttf"), size, layout_engine=ImageFont.Layout.RAQM)
        advances = [round(font.getlength(chr(cp)) * 64) for cp in POINTS]
        glyphs = len(data)
        data.extend(bytes(count * 10))
        for index, cp in enumerate(POINTS):
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
            for second, other in enumerate(POINTS):
                delta = round(font.getlength(chr(cp) + chr(other)) * 64) - advances[first] - advances[second]
                if delta:
                    pairs.append((first * count + second, delta))
        data.extend(struct.pack("<H", len(pairs)))
        for key, delta in pairs:
            data.extend(struct.pack("<Ih", key, delta))
        struct.pack_into("<II", data, table + (size - 8) * 8, glyphs, kern)
        print(f"Size {size}: {count} glyphs, {len(pairs)} pairs", flush=True)
    return bytes(data)


def compact(data: bytes) -> tuple[bytes, int]:
    """Losslessly share kerning curves; keep the published HA font immutable."""
    count = struct.unpack_from("<H", data, 10)[0]
    table = 12 + count * 2
    packed = bytearray(data[:table + 23 * 8])
    bitmaps = {}
    fonts = []
    glyph_tables = []
    for size in range(23):
        glyphs, pairs = struct.unpack_from("<II", data, table + size * 8)
        target = len(packed)
        glyph_tables.append(target)
        packed.extend(bytes(count * 10))
        for index in range(count):
            offset, width, height, left, top, advance = struct.unpack_from("<IBBbbH", data, glyphs + index * 10)
            bitmap = data[offset:offset + (width * height + 7) // 8]
            if bitmap not in bitmaps:
                bitmaps[bitmap] = len(packed)
                packed.extend(bitmap)
            struct.pack_into("<IBBbbH", packed, target + index * 10,
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
        packed.extend(struct.pack("<23h", *curve))
    for size, glyphs in enumerate(glyph_tables):
        struct.pack_into("<II", packed, table + size * 8, glyphs, pairs)
    return bytes(packed), curves_offset


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack-only", action="store_true", help="Repack the frozen font without Pillow")
    args = parser.parse_args()
    data = OUTPUT.read_bytes() if args.pack_only else generate()
    if OUTPUT.exists() and OUTPUT.read_bytes() != data:
        raise SystemExit("Font revision 1 differs. Do not overwrite a published revision; use a new revision.")
    if not args.pack_only:
        OUTPUT.write_bytes(data)
    packed, curves_offset = compact(data)
    license_text = (FONTS / "LICENSE.txt").read_text()
    header = ["// Generated by scripts/generate_template_font.py; do not edit.",
              f"// Frozen source SHA256: {sha256(data).hexdigest()}",
              "// Lossless storage: all sizes share pair keys and identical kerning curves.", "/*", license_text, "*/",
              "#pragma once", "#include <cstdint>",
              "namespace esphome::gtag_display::template_font_v1 {",
              f"inline constexpr uint32_t KERN_CURVES_OFFSET = {curves_offset};",
              "inline constexpr uint8_t DATA[] = {"]
    header.extend("  " + ",".join(f"0x{b:02x}" for b in packed[i:i + 24]) + "," for i in range(0, len(packed), 24))
    header.extend(["};", "}  // namespace esphome::gtag_display::template_font_v1", ""])
    HEADER.write_text("\n".join(header))
    print(f"Font: {len(data)} bytes; MCU storage: {len(packed)} bytes; source SHA256 {sha256(data).hexdigest()}")
