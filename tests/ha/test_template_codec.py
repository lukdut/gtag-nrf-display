"""Cross-language pixel agreement and strict parsing of untrusted template data."""
import ctypes
from hashlib import sha256
import json
from pathlib import Path
import random
import struct
import subprocess
import zlib

import pytest

from custom_components.gtag_ble_test.frame_codec import encode_best
from custom_components.gtag_ble_test.render import render_layout
from custom_components.gtag_ble_test.template_codec import encode_strings, render_payload

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = json.loads((ROOT / "tests/fixtures/three-values-v1.json").read_text())


@pytest.fixture(scope="module")
def native_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("native-template")
    source = path / "template.cpp"
    source.write_text('''#include "frame_protocol.h"
using namespace esphome::gtag_display;
extern "C" bool render(const uint8_t *in, size_t size, uint8_t *out) {
  return template_render::render(in, size, out, 4096);
}
extern "C" unsigned font_size() { return sizeof(template_font_v1::DATA); }
extern "C" int commit(const uint8_t *in, size_t size, unsigned crc) {
  frame::Receiver receiver;
  frame::Descriptor d;
  d.codec = frame::Codec::THREE_VALUES_V1; d.encoded_size = size; d.raw_crc32 = crc;
  if (receiver.begin(d) == frame::BeginResult::REJECTED) return int(receiver.error());
  receiver.write(0, in, size);
  uint8_t raw[4096];
  receiver.commit(raw, sizeof(raw));
  return int(receiver.error());
}
''')
    library = path / "template.so"
    subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-fPIC", "-shared",
                    "-I", str(ROOT / "config/esphome/components/gtag_display"),
                    str(source), "-o", str(library)], check=True)
    native = ctypes.CDLL(str(library))
    native.render.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p]
    native.render.restype = ctypes.c_bool
    native.font_size.restype = ctypes.c_uint32
    native.commit.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint32]
    return native


def test_frozen_font_and_approved_screen_match_on_device(native_template):
    font = (ROOT / "custom_components/gtag_ble_test/fonts/template_v1.bin").read_bytes()
    assert sha256(font).hexdigest() == "0022b94a2d8ec2b652d560332f631908ede808197d70f7cfbdf1547e93d091d3"
    assert native_template.font_size() < 230000  # Fits the legacy nice!nano application region.
    payload, expected = bytes.fromhex(GOLDEN["payload"]), bytes.fromhex(GOLDEN["raw"])
    assert encode_strings(GOLDEN["strings"]) == payload
    raw = ctypes.create_string_buffer(4096)
    assert native_template.render(payload, len(payload), raw)
    assert raw.raw == render_payload(payload) == expected
    assert native_template.commit(payload, len(payload), zlib.crc32(expected)) == 0
    assert native_template.commit(payload, len(payload), zlib.crc32(expected) ^ 1) == 5


def test_shrinking_clipping_kerning_and_unicode_match_native(native_template):
    rng = random.Random(285)
    samples = ["", " ", "___", "Ёжик", "CO₂: 684 ppm", "−23.40 °C", "AV fi ffi", "№ 12", "…", "X" * 256,
               "Температура в гостиной " * 10, "   Влажность   "]
    alphabet = "AbgKMVW01234 ,.°µ²₂ЁёЖДЦщъійє—…"
    samples += ["".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 257))) for _ in range(25)]
    for sample in samples:
        payload = encode_strings([sample] * 5)
        expected = render_payload(payload)
        raw = ctypes.create_string_buffer(4096)
        assert native_template.render(payload, len(payload), raw), repr(sample)
        assert raw.raw == expected, repr(sample)


def test_malformed_packets_rejected_before_display(native_template):
    payload = bytes.fromhex(GOLDEN["payload"])
    invalid = [payload[:i] for i in range(len(payload))]
    invalid += [payload + b"x", b"\x02" + payload[1:], b"\x01\x02" + payload[2:],
                payload[:2] + b"\xff\xff" + payload[4:], b"\x00" * 4097]
    for bad in (b"\x80", b"\xc0\xaf", b"\xe0\x80\x80", b"\xed\xa0\x80", b"\xf4\x90\x80\x80", b"\xf0\x9f"):
        invalid.append(b"\x01\x01" + struct.pack("<H", len(bad)) + bad + b"\x00\x00" * 4)
    invalid.extend(encode_strings([text, "", "", "", ""]) for text in ("\n", "\x00", "🙂", "漢字"))
    invalid.append(b"\x01\x01\x01\x01" + b"A" * 257 + b"\x00\x00" * 4)
    for bad in invalid:
        out = ctypes.create_string_buffer(4096)
        assert not native_template.render(bad, len(bad), out), bad.hex()
        with pytest.raises(ValueError):
            render_payload(bad)


def test_only_exact_layout_and_matching_pixels_get_a_template_candidate():
    layout = json.loads((ROOT / "docs/design/three-values-layout.json").read_text())
    frame = render_layout(layout)
    assert frame.template_payload == bytes.fromhex(GOLDEN["payload"])
    for text in ("🙂", "漢字", "office", "Первая\nВторая"):
        layout["elements"][0]["text"] = text
        assert render_layout(layout).template_payload is None
    layout["elements"][0]["text"] = "Гостиная"
    layout["elements"][0]["x"] = 120
    assert render_layout(layout).template_payload is None


def test_codec_selection_preserves_bitmap_fallback_and_limits():
    raw, payload = bytes.fromhex(GOLDEN["raw"]), bytes.fromhex(GOLDEN["payload"])
    assert encode_best(raw, 7, 4096, payload).codec == 2
    assert encode_best(raw, 3, 4096, payload).codec == 1
    assert encode_best(raw, 1, 4096, payload).codec == 0
    assert encode_best(raw, 7, len(payload), payload).codec == 2
    with pytest.raises(ValueError):
        encode_best(raw, 7, len(payload) - 1, payload)
    assert encode_best(raw, 7, 4096, payload[:-1]).codec == 1
    assert encode_best(b"\xff" * 4096, 7, 4096, payload).codec == 1
