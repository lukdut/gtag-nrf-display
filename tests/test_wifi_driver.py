"""Trace the shared Wi-Fi driver, including real Base64 and flash read paths."""
import base64
import ctypes
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import zlib

from saleae_reference import reference_words

ROOT = Path(__file__).resolve().parents[1]


class HeaderIsolationTests(unittest.TestCase):
    def test_esphome_can_include_the_wifi_header_on_nrf52840(self):
        # ESPHome adds every component header to esphome.h, including headers
        # for inactive platforms. The Wi-Fi one must expose no symbols/deps.
        with tempfile.TemporaryDirectory(prefix="gtag-header-platform-") as temporary:
            build = Path(temporary)
            defines = build / "esphome/core/defines.h"
            defines.parent.mkdir(parents=True)
            defines.write_text("#define USE_NRF52\n#define USE_ZEPHYR\n")
            source = build / "headers.cpp"
            source.write_text('#include "gtag_display_wifi.h"\n'
                              'namespace esphome::gtag_display {\n'
                              'enum class BootPattern { LOGO };\n'
                              'class GTagDisplay {};\n}\n')
            subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-fsyntax-only",
                            "-I", str(build), "-I", str(ROOT / "config/esphome/components/gtag_display"),
                            str(source)], check=True)


class WifiDriverTests(unittest.TestCase):
    esp8266 = False

    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="gtag-wifi-native-")
        build = Path(cls.temporary.name)
        for name in ("esphome/core/defines.h", "esphome/core/component.h", "esphome/core/gpio.h",
                     "esphome/core/hal.h", "esphome/core/log.h", "pgmspace.h"):
            target = build / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('#include "native_wifi_stubs.h"\n')
        output = build / "wifi.so"
        subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-fPIC", "-shared",
                        *(["-DUSE_ESP8266"] if cls.esp8266 else []),
                        "-I", str(build), "-I", str(ROOT / "tests"),
                        "-I", str(ROOT / "config/esphome/components/gtag_display"),
                        str(ROOT / "tests/native_wifi_driver.cpp"), "-o", str(output)], check=True)
        cls.fw = ctypes.CDLL(str(output))
        cls.fw.wifi_submit.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        cls.fw.wifi_submit.restype = cls.fw.wifi_confirm.restype = cls.fw.wifi_rendered.restype = ctypes.c_bool
        cls.fw.wifi_confirm.argtypes = [ctypes.c_char_p] * 3
        cls.fw.wifi_rendered.argtypes = [ctypes.c_char_p] * 2
        cls.fw.wifi_error.restype = ctypes.c_char_p

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.fw.wifi_create()
        self.assertLess(self.fw.wifi_run(200), 4000)

    def words(self):
        return [self.fw.wifi_word(i) for i in range(self.fw.wifi_word_count())]

    def submit(self, raw, timeout=0):
        self.crc = f"{zlib.crc32(raw):08x}".encode()
        return self.fw.wifi_submit(base64.b64encode(raw), 1, 0, b'00000001', self.crc, timeout)

    def test_reference_protocol_and_nonblocking_slices(self):
        words = reference_words(ROOT / "esp32-diagram/ESP32-gtag.sal")
        raw = bytes(word & 255 for word in words[-4096:])
        self.assertTrue(self.submit(raw))
        self.assertFalse(self.fw.wifi_rendered(b'00000001', self.crc))
        self.assertLess(self.fw.wifi_run(100), 4000)
        self.assertTrue(self.fw.wifi_rendered(b'00000001', self.crc))
        self.assertEqual(self.words(), words)

    def test_corrupt_or_busy_frame_keeps_last_good_image(self):
        self.assertTrue(self.submit(b'\xff' * 4096))
        self.assertFalse(self.submit(b'\x00' * 4096))
        self.assertEqual(self.fw.wifi_error(), b'display_busy')
        self.fw.wifi_run(100)
        old = self.words()
        self.assertFalse(self.fw.wifi_submit(base64.b64encode(b'\x00' * 4096), 1, 0, b'00000002', b'ffffffff', 0))
        self.assertEqual(self.fw.wifi_error(), b'crc_mismatch')
        self.fw.wifi_run(100)
        self.assertEqual(self.words(), old)

    def test_compressed_frame_and_malformed_input(self):
        crc = f"{zlib.crc32(b'\xff' * 4096):08x}".encode()
        self.assertTrue(self.fw.wifi_submit(base64.b64encode(b'\xff' * 32), 1, 1, b'00000001', crc, 0))
        self.fw.wifi_run(100)
        self.assertEqual(self.words()[-4096:], [0x1ff] * 4096)
        for payload, codec, version in ((b'!', 0, 1), (b'AA==', 0, 1), (b'AA==', 15, 1), (b'AA==', 0, 2)):
            self.assertFalse(self.fw.wifi_submit(payload, version, codec, b'00000002', crc, 0))

    def test_freshness_icon_and_recovery(self):
        self.assertTrue(self.submit(b'\xff' * 4096, timeout=1))
        self.fw.wifi_run(250)
        stale = self.words()[-4096:]
        self.assertNotEqual(stale, [0x1ff] * 4096)
        self.assertFalse(self.fw.wifi_confirm(b'00000002', self.crc, b'00000001'))
        self.assertTrue(self.fw.wifi_confirm(b'00000001', self.crc, b'00000001'))
        self.fw.wifi_run(64)
        self.assertEqual(self.words()[-4096:], [0x1ff] * 4096)

    def test_template_is_rendered_and_bad_revision_preserves_display(self):
        golden = json.loads((ROOT / "tests/fixtures/three-values-v1.json").read_text())
        raw, payload = bytes.fromhex(golden["raw"]), bytes.fromhex(golden["payload"])
        crc = f"{zlib.crc32(raw):08x}".encode()
        self.assertTrue(self.fw.wifi_submit(base64.b64encode(payload), 1, 2, b'00000001', crc, 0))
        self.fw.wifi_run(100)
        self.assertTrue(self.fw.wifi_rendered(b'00000001', crc))
        self.assertEqual(bytes(word & 255 for word in self.words()[-4096:]), raw)
        shown = self.words()
        for bad, expected in ((b'\x01\x02' + payload[2:], b'decode_error'),
                              (payload[:-1], b'decode_error')):
            self.assertFalse(self.fw.wifi_submit(base64.b64encode(bad), 1, 2, b'00000002', crc, 0))
            self.assertEqual(self.fw.wifi_error(), expected)
            self.fw.wifi_run(100)
            self.assertEqual(self.words(), shown)

    def test_simple_templates_crc_and_codec_id_preserve_previous_frame(self):
        fixtures = json.loads((ROOT / "tests/fixtures/simple-templates-v1.json").read_text())
        for golden in fixtures:
            raw, payload = bytes.fromhex(golden["raw"]), bytes.fromhex(golden["payload"])
            crc = golden["crc"].encode()
            self.assertTrue(self.fw.wifi_submit(base64.b64encode(payload), 1, 3, b'00000001', crc, 0))
            self.fw.wifi_run(100)
            self.assertTrue(self.fw.wifi_rendered(b'00000001', crc))
            self.assertEqual(bytes(word & 255 for word in self.words()[-4096:]), raw)
            shown = self.words()
            for bad, codec, checksum in ((payload, 2, crc), (payload, 3, b'00000000'),
                                          (payload[:1] + b'\x02' + payload[2:], 3, crc)):
                self.assertFalse(self.fw.wifi_submit(base64.b64encode(bad), 1, codec, b'00000002', checksum, 0))
                self.fw.wifi_run(100)
                self.assertEqual(self.words(), shown)

    def test_base64_padding_and_boundaries_preserve_previous_frame(self):
        self.assertTrue(self.submit(b'\xff' * 4096))
        self.fw.wifi_run(100)
        shown = self.words()
        encoded = base64.b64encode(b'\xff' * 4096)
        for bad in (b'', b'A', b'====', b'AA=A', b'AA==AAAA', b'AB==', b'AAB=',
                    encoded[:-1], encoded + b'\n', encoded[:-3] + b'x==',
                    b'!' + encoded[1:], b'\xff' + encoded[1:],
                    base64.b64encode(b'\x00' * 4097), b'AAAA' * 1366):
            with self.subTest(payload=bad[:20]):
                self.assertFalse(self.fw.wifi_submit(bad, 1, 1, b'00000002', self.crc, 0))
                self.fw.wifi_run(100)
                self.assertEqual(self.words(), shown)
                self.assertTrue(self.fw.wifi_rendered(b'00000001', self.crc))

    def test_raw_frame_uses_two_persistent_buffers(self):
        self.assertLess(self.fw.wifi_component_size(), 9000)

    def test_boot_logo_matches_generated_frame(self):
        import re
        expected = bytes(int(v, 16) for v in re.findall(r'0x([0-9A-F]{2})',
            (ROOT / 'config/esphome/components/gtag_display/boot_logo.h').read_text()))
        self.fw.wifi_logo()
        self.fw.wifi_run(250)
        self.assertEqual(bytes(word & 255 for word in self.words()[-4096:]), expected)
        if self.esp8266:
            self.assertGreaterEqual(self.fw.wifi_flash_reads(), 4096)


class Esp8266WifiDriverTests(WifiDriverTests):
    esp8266 = True

    def test_template_reads_flash_and_yields_to_wifi(self):
        self.test_simple_templates_crc_and_codec_id_preserve_previous_frame()
        self.assertGreater(self.fw.wifi_flash_reads(), 1000)
        self.assertGreater(self.fw.wifi_yields(), 10)


if __name__ == "__main__":
    unittest.main()
