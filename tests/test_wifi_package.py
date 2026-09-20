"""Release archives must exclude local credentials and generated build files."""
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("package_wifi", ROOT / "scripts/package_wifi.py")
packager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packager)


class WifiPackageTests(unittest.TestCase):
    def test_standalone_sources_without_local_secrets_or_builds(self):
        with tempfile.TemporaryDirectory(prefix="gtag-wifi-package-") as temporary:
            root = Path(temporary)
            config = root / "config/esphome"
            for name in ("esp32-gtag-display.yaml", "gtag-esp32-c3-wifi.yaml",
                         "packages/wifi-base.yaml", "examples/wifi-led-strip.yaml"):
                dest = config / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / "config/esphome" / name, dest)
            shutil.copytree(ROOT / "config/esphome/components/gtag_display",
                            config / "components/gtag_display",
                            ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copyfile(ROOT / "LICENSE", root / "LICENSE")
            for name in ("secrets.yaml", ".esphome/build/firmware.bin",
                         "components/gtag_display/private.json",
                         "components/gtag_display/__pycache__/secret.pyc"):
                path = config / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("PRIVATE_SENTINEL")
            original_root = packager.ROOT
            try:
                packager.ROOT = root
                archive_path = packager.package_wifi(root / "output")
            finally:
                packager.ROOT = original_root
            with ZipFile(archive_path) as archive:
                names = set(archive.namelist())
                self.assertTrue({"LICENSE", "README.txt", "secrets.example.yaml",
                                 "esp32-gtag-display.yaml", "packages/wifi-base.yaml",
                                 "components/gtag_display/gtag_display_esp32.cpp",
                                 "components/gtag_display/frame_codec.h"} <= names)
                for name in names:
                    self.assertNotIn(b"PRIVATE_SENTINEL", archive.read(name))
                    self.assertFalse(name.endswith((".bin", ".pyc")))
                    self.assertNotEqual(name, "secrets.yaml")
            self.assertEqual((root / "output" / packager.PUBLIC_NAME).read_bytes(),
                             (config / packager.PUBLIC_NAME).read_bytes())


if __name__ == "__main__":
    unittest.main()
