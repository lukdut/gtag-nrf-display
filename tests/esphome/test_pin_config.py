"""Run using Python with ESPHome 2026.9.0 installed; no SDK or radio required."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config/esphome"


class PinConfigurationTests(unittest.TestCase):
    def run_config(self, settings="", *, profile="ble", extra="", generate=False):
        with tempfile.TemporaryDirectory(prefix="gtag-pin-config-") as temporary:
            folder = Path(temporary)
            yaml = folder / "device.yaml"
            yaml.write_text(f"""packages:
  gtag: !include {CONFIG / 'packages' / (profile + '-base.yaml')}
external_components:
  - source:
      type: local
      path: {CONFIG / 'components'}
    components: [gtag_display]
esphome:
  build_path: {folder / 'build'}
gtag_display:
{settings}
{extra}
""")
            args = [sys.executable, "-m", "esphome"]
            args += ["compile", "--only-generate"] if generate else ["config"]
            result = subprocess.run([*args, str(yaml)], text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, timeout=60)
            main = folder / "build/src/main.cpp"
            return result, main.read_text() if main.exists() else ""

    def test_defaults_and_remapped_codegen_for_both_transports(self):
        for profile in ("ble", "zigbee"):
            with self.subTest(profile=profile, pins="default"):
                result, code = self.run_config(profile=profile, generate=True)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn("->set_boot_pattern(gtag_display::BootPattern::LOGO);", code)
                for setter, number in (("dio_pin", 11), ("clk_pin", 36), ("cs_pin", 38),
                                       ("reset_pin", 45), ("battery_pin", 31)):
                    self.assertIn(f"->set_{setter}({number});", code)
            with self.subTest(profile=profile, pins="custom"):
                result, code = self.run_config("""  dio_pin: P1.00
  clk_pin: P0.08
  cs_pin: 47
  reset_pin: P0.17
  battery_voltage:
    pin: P0.04
""", profile=profile, generate=True)
                self.assertEqual(result.returncode, 0, result.stdout)
                for setter, number in (("dio_pin", 32), ("clk_pin", 8), ("cs_pin", 47),
                                       ("reset_pin", 17), ("battery_pin", 4)):
                    self.assertIn(f"->set_{setter}({number});", code)

    def test_invalid_assignments_are_rejected(self):
        cases = [
            ("  clk_pin: P0.11", "already used by dio_pin"),
            ("  dio_pin: P0.31", "already used by dio_pin"),
            ("  battery_voltage:\n    pin: P1.02", "Battery ADC requires"),
            ("  dio_pin: P1.16", "Expected P0.00"),
            ("  dio_pin: P0.32", "Expected P0.00"),
            ("  dio_pin: 48", "external GPIO"),
            ("  dio_pin: 1.5", "Use a GPIO number"),
            ("  dio_pin: P0.00", "reserved"),
            ("  dio_pin: P0.09", "reserved"),
            ("  dio_pin: P0.18", "reserved"),
            ("  dio_pin:\n    number: P0.06\n    inverted: true", "Use a GPIO number"),
        ]
        for settings, error in cases:
            with self.subTest(settings=settings):
                result, _ = self.run_config(settings)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(error, result.stdout)

    def test_other_components_cannot_reuse_lcd_gpio(self):
        result, _ = self.run_config(extra="""output:
  - platform: gpio
    id: conflicting_output
    pin: P0.11
""")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("used in multiple places", result.stdout)

    def test_disabling_battery_frees_adc_pin_for_lcd(self):
        result, code = self.run_config("""  dio_pin: P0.31
  battery_voltage: !remove
""", generate=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("->set_dio_pin(31);", code)
        self.assertNotIn("->set_battery_pin(", code)

    def test_super52840_no_battery_endpoints_and_bootloader(self):
        with tempfile.TemporaryDirectory(prefix="gtag-super-config-") as temporary:
            folder = Path(temporary)
            yaml = folder / "device.yaml"
            yaml.write_text(f"""packages:
  gtag: !include {CONFIG / 'nrf-gtag-super52840-zigbee.yaml'}
esphome:
  build_path: {folder / 'build'}
""")
            result = subprocess.run([sys.executable, "-m", "esphome", "compile", "--only-generate", str(yaml)],
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout)
            code = (folder / "build/src/main.cpp").read_text()
            defines = (folder / "build/src/esphome/core/defines.h").read_text()
            self.assertNotIn("USE_GTAG_BATTERY", defines)
            self.assertNotIn("gtag_battery_sensor", code)
            self.assertIn("->set_boot_pattern(gtag_display::BootPattern::LOGO);", code)
            for setter, number in (("dio_pin", 47), ("clk_pin", 45), ("cs_pin", 46), ("reset_pin", 44)):
                self.assertIn(f"->set_{setter}({number});", code)
            self.assertIn("zigbee_zigbeesensor_id->set_endpoint(1)", code)
            self.assertIn("zigbee_zigbeenumber_id->set_endpoint(2)", code)
            self.assertIn("gtag_display_gtagzigbee_id->set_endpoint(3)", code)
            self.assertIn("GTag_Display_Frame_NoBat", code)
            self.assertIn("adafruit_nrf52_sd140_v7", code)


if __name__ == "__main__":
    unittest.main()
