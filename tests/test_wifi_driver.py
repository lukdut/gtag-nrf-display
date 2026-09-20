"""Trace the production ESP32 driver with GPIO/time/base64 boundaries replaced."""
import base64
import ctypes
from pathlib import Path
import subprocess
import tempfile
import unittest
import zlib

from saleae_reference import reference_words

ROOT = Path(__file__).resolve().parents[1]


class WifiDriverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="gtag-wifi-native-")
        build = Path(cls.temporary.name)
        for name in ("esphome/core/defines.h", "esphome/core/component.h", "esphome/core/gpio.h",
                     "esphome/core/hal.h", "esphome/core/log.h", "mbedtls/base64.h"):
            target = build / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('#include "native_wifi_stubs.h"\n')
        output = build / "wifi.so"
        subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-fPIC", "-shared",
                        "-I", str(build), "-I", str(ROOT / "tests"),
                        "-I", str(ROOT / "config/esphome/components/gtag_display"),
                        str(ROOT / "tests/native_wifi_driver.cpp"), "-o", str(output)], check=True)
        cls.fw = ctypes.CDLL(str(output))
        cls.fw.wifi_submit.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        cls.fw.wifi_submit.restype = cls.fw.wifi_confirm.restype = cls.fw.wifi_rendered.restype = ctypes.c_bool
        cls.fw.wifi_confirm.argtypes = [ctypes.c_char_p] * 3
        cls.fw.wifi_rendered.argtypes = [ctypes.c_char_p] * 2
        cls.fw.wifi_error.restype = ctypes.c_char_p
        decode_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t,
                                      ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t)
        @decode_type
        def decode(out, size, length, data, data_size):
            try:
                value = base64.b64decode(ctypes.string_at(data, data_size), validate=True)
                if len(value) > size:
                    return -1
                ctypes.memmove(out, value, len(value))
                length[0] = len(value)
                return 0
            except ValueError:
                return -1
        cls.decoder = decode
        cls.fw.wifi_decoder.argtypes = [decode_type]
        cls.fw.wifi_decoder(decode)

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


if __name__ == "__main__":
    unittest.main()
