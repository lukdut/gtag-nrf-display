"""Verify every glyph and every kerning pair against the immutable v1 font."""
import ctypes
from hashlib import sha256
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CompactFontTests(unittest.TestCase):
    def test_lossless_storage_and_golden_pixels(self):
        font = (ROOT / "custom_components/gtag_ble_test/fonts/template_v1.bin").read_bytes()
        self.assertEqual(sha256(font).hexdigest(), "0022b94a2d8ec2b652d560332f631908ede808197d70f7cfbdf1547e93d091d3")
        count = struct.unpack_from("<H", font, 10)[0]
        expected = bytearray(font[12:12 + count * 2])
        large = (ROOT / "custom_components/gtag_ble_test/fonts/template_large_v1.bin").read_bytes()
        self.assertEqual(sha256(large).hexdigest(), "64398aeca7886857e9d5e04c86876aad5bf7e85f0c53b1fc493050089a07a4f5")
        for font, sizes in ((font, range(23)), (large, (0, 2))):
            for size in sizes:
                glyphs, pairs = struct.unpack_from("<II", font, 12 + count * 2 + size * 8)
                for index in range(count):
                    record = glyphs + index * 10
                    offset, width, height = struct.unpack_from("<IBB", font, record)
                    expected.extend(font[record + 4:record + 10])
                    expected.extend(font[offset:offset + (width * height + 7) // 8])
                pair_count = struct.unpack_from("<H", font, pairs)[0]
                table = dict(struct.unpack_from("<Ih", font, pairs + 2 + i * 6) for i in range(pair_count))
                expected.extend(struct.pack(f"<{count * count}h", *(table.get(i, 0) for i in range(count * count))))
        with tempfile.TemporaryDirectory(prefix="gtag-font-") as directory:
            path = Path(directory)
            source = path / "font.cpp"
            source.write_text('''#include "template_render.h"
using namespace esphome::gtag_display;
extern "C" size_t font_dump(uint8_t *out) {
  auto *start = out;
  const template_render::Font first(8);
  std::memcpy(out, first.data + 12, first.count * 2); out += first.count * 2;
  for (int size = 8; size <= 46; ++size) {
    if (size > 30 && size != 44 && size != 46) continue;
    const template_render::Font font(size);
    for (int index = 0; index < font.count; ++index) {
      const auto *glyph = font.glyph(index);
      std::memcpy(out, glyph + 4, 6); out += 6;
      const size_t length = (glyph[4] * glyph[5] + 7) / 8;
      uint8_t scratch[512];
      const auto *bitmap = font.bitmap(glyph, scratch, sizeof(scratch));
      if (!bitmap) return 0;
      std::memcpy(out, bitmap, length); out += length;
    }
    for (int a = 0; a < font.count; ++a)
      for (int b = 0; b < font.count; ++b) {
        const uint16_t value = uint16_t(font.kern(a, b));
        *out++ = value & 255; *out++ = value >> 8;
      }
  }
  return out - start;
}
extern "C" bool render(const uint8_t *in, size_t size, uint8_t *out) {
  return template_render::render(in, size, out, 4096);
}
extern "C" unsigned font_size() { return sizeof(template_font_v1::DATA) + sizeof(template_font_large_v1::DATA); }
''')
            library = path / "font.so"
            subprocess.run(["g++", "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", "-fPIC", "-shared",
                            "-I", str(ROOT / "config/esphome/components/gtag_display"),
                            str(source), "-o", str(library)], check=True)
            native = ctypes.CDLL(str(library))
            native.font_dump.argtypes = [ctypes.c_void_p]
            native.font_dump.restype = ctypes.c_size_t
            result = ctypes.create_string_buffer(len(expected))
            self.assertEqual(native.font_dump(result), len(expected))
            self.assertEqual(result.raw, bytes(expected))
            self.assertLess(native.font_size(), 250000)
            native.render.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p]
            native.render.restype = ctypes.c_bool
            golden = json.loads((ROOT / "tests/fixtures/three-values-v1.json").read_text())
            payload = bytes.fromhex(golden["payload"])
            raw = ctypes.create_string_buffer(4096)
            self.assertTrue(native.render(payload, len(payload), raw))
            self.assertEqual(raw.raw, bytes.fromhex(golden["raw"]))
