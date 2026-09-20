#!/usr/bin/env python3
"""Build the wizard's ESP32-C3 configuration plus a user-added WS2812 strip.

Uses deliberately public test credentials, never a user's secrets.yaml.
Artifacts verify compilation and partition sizes; do not flash them as a
configured installation. Run with the pinned ESPHome Python environment.
"""
import argparse
import importlib.util
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path)
    args = parser.parse_args()
    folder = (args.build_dir or Path(tempfile.mkdtemp(prefix="gtag-wifi-build-"))).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("firmware_wizard", ROOT / "custom_components/gtag_ble_test/firmware_config.py")
    wizard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wizard)
    settings = {"board": "esp32c3_supermini", "transport": "wifi", "name": "gtag-esp32-display",
                "friendly_name": "GTag compile check", **wizard.hardware_defaults("esp32c3_supermini"),
                "wifi_ssid": "build-test", "wifi_password": "build-test-password",
                "api_key": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=", "ota_password": "build-test-ota-password"}
    config = ROOT / "config/esphome"
    yaml = re.sub(r"github://[^\n]+wifi.yaml@[^\s]+", f"!include {config}/packages/wifi-base.yaml",
                  wizard.render_firmware_yaml(settings))
    yaml = yaml.replace("packages:\n", f"packages:\n  leds: !include {config}/examples/wifi-led-strip.yaml\n")
    yaml += (f"\nexternal_components:\n  - source:\n      type: local\n      path: {config}/components\n"
             f"    components: [gtag_display]\nesphome:\n  build_path: {folder}/build\n")
    path = folder / "device.yaml"
    path.write_text(yaml)
    subprocess.run([sys.executable, "-m", "esphome", "compile", str(path)], check=True)


if __name__ == "__main__":
    main()
