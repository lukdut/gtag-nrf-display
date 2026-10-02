#!/usr/bin/env python3
"""Build the wizard's Wi-Fi configuration (ESP32 also adds a WS2812 strip).

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
    parser.add_argument("--board", choices=("esp32c3_supermini", "d1_mini"), default="esp32c3_supermini")
    args = parser.parse_args()
    folder = (args.build_dir or Path(tempfile.mkdtemp(prefix="gtag-wifi-build-"))).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("firmware_wizard", ROOT / "custom_components/gtag_ble_test/firmware_config.py")
    wizard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wizard)
    settings = {"board": args.board, "transport": "wifi", "name": "gtag-wifi-check",
                "friendly_name": "GTag compile check", **wizard.hardware_defaults(args.board),
                "wifi_ssid": "build-test", "wifi_password": "build-test-password",
                "api_key": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=", "ota_password": "build-test-ota-password"}
    config = ROOT / "config/esphome"
    package = wizard.BOARDS[args.board]["wifi_package"]
    yaml = re.sub(r"github://[^\n]+wifi.yaml@[^\s]+", f"!include {config}/packages/{package}-base.yaml",
                  wizard.render_firmware_yaml(settings))
    if args.board == "esp32c3_supermini":
        yaml = yaml.replace("packages:\n", f"packages:\n  leds: !include {config}/examples/wifi-led-strip.yaml\n")
    yaml += (f"\nexternal_components:\n  - source:\n      type: local\n      path: {config}/components\n"
             f"    components: [gtag_display]\nesphome:\n  build_path: {folder}/build\n")
    path = folder / "device.yaml"
    path.write_text(yaml)
    subprocess.run([sys.executable, "-m", "esphome", "compile", str(path)], check=True)


if __name__ == "__main__":
    main()
