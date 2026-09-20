"""ESP32-C3 config, user extensions and wizard output against real ESPHome."""
from pathlib import Path
import importlib.util
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config/esphome"


class WifiConfigurationTests(unittest.TestCase):
    def check(self, extra="", pins=None):
        with tempfile.TemporaryDirectory(prefix="gtag-wifi-config-") as temporary:
            path = Path(temporary) / "device.yaml"
            pins = pins or dict(dio_pin="GPIO0", clk_pin="GPIO1", cs_pin="GPIO3", reset_pin="GPIO4")
            path.write_text(f"""packages:
  gtag: !include {CONFIG}/packages/wifi-base.yaml
external_components:
  - source:
      type: local
      path: {CONFIG}/components
    components: [gtag_display]
wifi:
  ssid: test-network
  password: test-password
gtag_display:
""" + "".join(f"  {key}: {value}\n" for key, value in pins.items()) + extra)
            return subprocess.run([sys.executable, "-m", "esphome", "config", str(path)],
                                  text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)

    def test_lights_sensors_and_user_api_actions_can_coexist(self):
        extra = (CONFIG / "examples/wifi-led-strip.yaml").read_text() + """
i2c:
  sda: GPIO6
  scl: GPIO7
binary_sensor:
  - platform: gpio
    pin: GPIO10
    name: User button
api:
  actions:
    - action: user_action
      then:
        - logger.log: User action
"""
        result = self.check(extra)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("gtag_frame", result.stdout)
        self.assertIn("user_action", result.stdout)

    def test_shared_gpio_rejected(self):
        extra = (CONFIG / "examples/wifi-led-strip.yaml").read_text().replace("GPIO5", "GPIO4")
        result = self.check(extra)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("multiple", result.stdout)

    def test_battery_mode_rejected(self):
        result = self.check(pins=dict(dio_pin="GPIO0", clk_pin="GPIO1", cs_pin="GPIO3", reset_pin="GPIO4",
                                     battery_voltage="{enabled: true}"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("USB power", result.stdout)

    def test_wizard_gpio_validation(self):
        spec = importlib.util.spec_from_file_location("wifi_wizard", ROOT / "custom_components/gtag_ble_test/firmware_config.py")
        wizard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wizard)
        settings = {"board": "esp32c3_supermini", "transport": "wifi", "name": "gtag-test",
                    "friendly_name": "Test display", **wizard.hardware_defaults("esp32c3_supermini"),
                    "wifi_ssid": "test-network", "wifi_password": "test-password",
                    "api_key": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=", "ota_password": "test-ota-password"}
        for pin in ("GPIO18", "GPIO19", "GPIO2", "GPIO8", "GPIO9", "GPIO12", "GPIO99"):
            with self.subTest(pin=pin), self.assertRaises(wizard.FirmwareConfigError):
                wizard.render_firmware_yaml({**settings, "dio_pin": pin})
        text = wizard.render_firmware_yaml(settings)
        with tempfile.TemporaryDirectory(prefix="gtag-wifi-wizard-") as temporary:
            path = Path(temporary) / "device.yaml"
            text = re.sub(r"github://[^\n]+wifi\.yaml@[^\s]+", f"!include {CONFIG}/packages/wifi-base.yaml", text)
            text += f"\nexternal_components:\n  - source:\n      type: local\n      path: {CONFIG}/components\n    components: [gtag_display]\n"
            path.write_text(text)
            result = subprocess.run([sys.executable, "-m", "esphome", "config", str(path)],
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
