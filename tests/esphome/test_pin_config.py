"""Run using Python with ESPHome 2026.9.0 installed; no SDK or radio required."""
from pathlib import Path
from contextlib import contextmanager
import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import yaml as yaml_lib

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config/esphome"


def localize_packages(text, folder):
    """Resolve our pinned GitHub packages against the candidate checkout."""
    return re.sub(
        r"github://lukdut/gtag-nrf-display/config/esphome/packages/([^@\s]+)@[^\s]+",
        lambda match: f"!include {folder}/packages/{match[1]}", text,
    )


@contextmanager
def public_packages():
    # Preserve the public YAML and package contents; substitute only source
    # locations so a candidate tag can be checked before publishing it.
    with tempfile.TemporaryDirectory(prefix="gtag-public-config-") as temporary:
        folder = Path(temporary)
        shutil.copytree(CONFIG / "packages", folder / "packages")
        for profile in ("ble", "zigbee"):
            package = folder / "packages" / f"{profile}.yaml"
            text, count = re.subn(
                r"      type: git\n      url: https://github.com/lukdut/gtag-nrf-display.git\n"
                r"      ref: [^\n]+\n      path: config/esphome/components\n",
                f"      type: local\n      path: {CONFIG / 'components'}\n", package.read_text(),
            )
            if count != 1:
                raise AssertionError(f"Unexpected public component source in {package.name}")
            package.write_text(text)
        yield folder


class PinConfigurationTests(unittest.TestCase):
    def test_firmware_wizard_yaml_matches_esphome(self):
        # Load the pure generator without importing the HA integration on the
        # ESPHome Python version. Validate its real output, including package
        # merging, bootloader selection and disabled-battery endpoints.
        spec = importlib.util.spec_from_file_location(
            "firmware_config", ROOT / "custom_components/gtag_ble_test/firmware_config.py",
        )
        wizard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wizard)
        with public_packages() as folder:
            for board, transport in (("promicro", "ble"), ("promicro", "zigbee"), ("super52840", "zigbee")):
                for battery in (False, True):
                    with self.subTest(board=board, transport=transport, battery=battery):
                        settings = {
                            **wizard.hardware_defaults(board), "board": board, "transport": transport,
                            "name": "gtag-kitchen", "friendly_name": 'Экран "кухни": #1',
                            "battery_enabled": battery,
                            "dio_pin": "P0.06", "clk_pin": "P0.08", "cs_pin": "P0.15", "reset_pin": "P0.17",
                            "battery_pin": "P0.04", "calibration": 0.955,
                            "empty_voltage": 3.2, "full_voltage": 4.19, "recovery_voltage": 3.4, "indicator": False,
                        }
                        rendered = wizard.render_firmware_yaml(settings)
                        path = folder / "device.yaml"
                        # Override build_path through a wrapper, preserving the
                        # generated YAML as the complete device configuration.
                        (folder / "generated.yaml").write_text(localize_packages(rendered, folder))
                        path.write_text(f"packages:\n  device: !include generated.yaml\nesphome:\n  build_path: {folder / 'build'}\n")
                        result = subprocess.run([sys.executable, "-m", "esphome", "compile", "--only-generate", str(path)],
                                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
                        self.assertEqual(result.returncode, 0, result.stdout)
                        code = (folder / "build/src/main.cpp").read_text()
                        self.assertIn("sd140_v7" if board == "super52840" else "sd140_v6", code)
                        for field, pin in (("dio_pin", 6), ("clk_pin", 8), ("cs_pin", 15), ("reset_pin", 17)):
                            self.assertIn(f"->set_{field}({pin});", code)
                        self.assertEqual("->set_battery_pin(4);" in code, battery)
                        if battery:
                            self.assertIn("->set_battery_voltage_range(3200, 4190);", code)
                            self.assertIn("->set_battery_protection(3200, 3400);", code)
                        if transport == "zigbee":
                            self.assertIn("GTag_Display_Frame_V1" if battery else "GTag_Display_Frame_NoBat", code)
                            self.assertIn("->set_rendered_frames_sensor(gtag_rendered_sensor);", code)
                            self.assertEqual("->set_battery_sensor(gtag_battery_sensor);" in code, battery)
                            for sensor_id in (["gtag_battery_sensor"] if battery else []) + ["gtag_rendered_sensor"]:
                                self.assertIn(f"{sensor_id}->set_update_interval(4294967295UL);", code)
                                self.assertNotIn(f"{sensor_id}->set_template(", code)

    def test_public_release_configurations(self):
        with public_packages() as folder:
            for profile in ("ble", "zigbee", "super52840-zigbee"):
                for battery in ((True, False) if profile != "super52840-zigbee" else (False,)):
                    with self.subTest(profile=profile, battery=battery):
                        text = (CONFIG / f"gtag-{profile}.yaml").read_text()
                        if not battery and profile != "super52840-zigbee":
                            text = re.sub(r"  battery_voltage:\n(?:    [^\n]*\n)+",
                                          "  battery_voltage:\n    enabled: false\n", text)
                            if profile == "zigbee":
                                # Insert after the main package, just as documented.
                                text = re.sub(r"(  gtag: [^\n]+\n)",
                                              r"\1  no_battery: !include " + str(folder / "packages/zigbee-no-battery.yaml") + "\n", text)
                        path = folder / "device.yaml"
                        path.write_text(localize_packages(text, folder) + f"\nesphome:\n  build_path: {folder / 'build'}\n")
                        result = subprocess.run([sys.executable, "-m", "esphome", "compile", "--only-generate", str(path)],
                                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
                        self.assertEqual(result.returncode, 0, result.stdout)
                        defines = (folder / "build/src/esphome/core/defines.h").read_text()
                        self.assertEqual("#define USE_GTAG_BATTERY" in defines, battery)
                        if profile != "ble":
                            code = (folder / "build/src/main.cpp").read_text()
                            self.assertIn("GTag_Display_Frame_V1" if battery else "GTag_Display_Frame_NoBat", code)

    def run_config(self, settings="", *, profile="ble", extra="", generate=False, omit=()):
        config = dict(dio_pin="P0.11", clk_pin="P1.04", cs_pin="P1.06", reset_pin="P1.13",
                      battery_voltage=dict(enabled=True, pin="P0.31", calibration=1.0,
                                           empty_voltage="3.306V", full_voltage="4.19V", recovery_voltage="3.45V"))
        for key, value in (yaml_lib.safe_load(settings) or {}).items():
            if key == "battery_voltage" and isinstance(value, dict):
                config[key].update(value)
            else:
                config[key] = value
        for key in omit:
            if key.startswith("battery_voltage."):
                del config["battery_voltage"][key.split(".")[1]]
            else:
                del config[key]
        settings = "\n".join("  " + line for line in yaml_lib.safe_dump(config).splitlines())
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
                self.assertIn("->set_battery_indicator(true);", code)
                self.assertIn("->set_battery_voltage_range(3306, 4190);", code)
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
    indicator: false
""", profile=profile, generate=True)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn("->set_battery_indicator(false);", code)
                for setter, number in (("dio_pin", 32), ("clk_pin", 8), ("cs_pin", 47),
                                       ("reset_pin", 17), ("battery_pin", 4)):
                    self.assertIn(f"->set_{setter}({number});", code)

    def test_hardware_settings_are_required(self):
        for key in ("dio_pin", "clk_pin", "cs_pin", "reset_pin", "battery_voltage", "battery_voltage.enabled",
                    "battery_voltage.pin", "battery_voltage.calibration", "battery_voltage.empty_voltage",
                    "battery_voltage.full_voltage", "battery_voltage.recovery_voltage"):
            with self.subTest(key=key):
                result, _ = self.run_config(omit=(key,))
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("required option", result.stdout)
        for recovery in ("3.306V", "3.0V", "4.2V"):
            result, _ = self.run_config(f"  battery_voltage:\n    recovery_voltage: {recovery}")
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("recovery_voltage must", result.stdout)

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

    def test_battery_curve_endpoints(self):
        result, code = self.run_config("""  battery_voltage:
    empty_voltage: 3.2V
    full_voltage: 4.18V
""", generate=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("->set_battery_voltage_range(3200, 4180);", code)
        for empty, full in ((4.19, 4.19), (4.2, 3.3), (3.3001, 3.3002), (2.4, 4.19), (3.3, 4.6)):
            with self.subTest(empty=empty, full=full):
                result, _ = self.run_config(f"  battery_voltage:\n    empty_voltage: {empty}V\n    full_voltage: {full}V")
                self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_disabling_battery_frees_adc_pin_for_lcd(self):
        result, code = self.run_config("""  dio_pin: P0.31
  battery_voltage:
    enabled: false
""", generate=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("->set_dio_pin(31);", code)
        self.assertNotIn("->set_battery_pin(", code)
        self.assertNotIn("->set_battery_indicator(", code)
        self.assertNotIn("->set_battery_voltage_range(", code)

    def test_complete_documentation_example(self):
        document = (ROOT / "docs/device-configuration.md").read_text()
        example = document.split("```yaml\n", 1)[1].split("```", 1)[0]
        with public_packages() as folder:
            path = folder / "device.yaml"
            path.write_text(localize_packages(example, folder))
            result = subprocess.run([sys.executable, "-m", "esphome", "config", str(path)],
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout)

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
