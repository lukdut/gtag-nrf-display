"""Prevent mixing UF2s for S140 v6/v7 or writing persistent/bootloader memory."""
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from package_firmware import validate_uf2


class FirmwarePackagingTests(unittest.TestCase):
    def validate(self, addresses, app_start):
        raw = bytearray()
        for number, address in enumerate(addresses):
            block = bytearray(512)
            struct.pack_into("<8I", block, 0, 0x0A324655, 0x9E5D5157, 0x2000,
                             address, 256, number, len(addresses), 0xADA52840)
            struct.pack_into("<I", block, 508, 0x0AB16F30)
            raw.extend(block)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "app.uf2"
            path.write_bytes(raw)
            validate_uf2(path, app_start=app_start)

    def test_only_matching_bootloader_start_is_accepted(self):
        for start in (0x26000, 0x27000):
            with self.subTest(start=start):
                self.validate([start, start + 256], start)
                with self.assertRaises(ValueError):
                    self.validate([start, start + 256], 0x27000 if start == 0x26000 else 0x26000)

    def test_persistent_memory_and_bootloader_are_never_packaged(self):
        for start in (0x26000, 0x27000):
            for address in (start - 256, 0xE8F80, 0xE9000, 0xF4000):
                with self.subTest(start=start, address=address), self.assertRaises(ValueError):
                    self.validate([start, address], start)

    def test_unknown_start_is_rejected(self):
        with self.assertRaises(ValueError):
            self.validate([0], 0)


if __name__ == "__main__":
    unittest.main()
