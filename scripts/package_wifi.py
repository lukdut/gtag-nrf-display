#!/usr/bin/env python3
"""Package standalone ESP32 sources without credentials or test firmware."""
import argparse
from pathlib import Path
import shutil
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_NAME = "gtag-esp32-c3-wifi-esphome.zip"
PUBLIC_NAME = "gtag-esp32-c3-wifi.yaml"
README = """GTag Display — ESP32-C3 Super Mini / Wi-Fi

This archive contains standalone ESPHome sources, not a prebuilt firmware.
Requires ESPHome 2026.9.0 and Home Assistant 2026.9.2 (tested versions).

1. Extract the archive, preserving packages/, components/ and examples/.
2. Copy secrets.example.yaml to secrets.yaml and fill in your Wi-Fi SSID and
   password, a random 32-byte base64 API key, and your own OTA password.
   Generate the API key with: openssl rand -base64 32
3. Set a unique device name in esp32-gtag-display.yaml, for example:
   substitutions:
     gtag_name: gtag-kitchen
     gtag_friendly_name: Kitchen display
   Check dio_pin, clk_pin, cs_pin and reset_pin for your wiring.
4. First flash over USB: esphome run esp32-gtag-display.yaml
   Subsequent uploads can use the same configuration over Wi-Fi (ESPHome OTA).
5. Add the device to the standard ESPHome integration in HA with its API key,
   then choose GTag Display → Wi-Fi (ESPHome) and select the device.

USB powers the ESP32. Preserve the display's original power and DisplayCLK/S1
clock; never connect USB 5 V to LCD signal pins. This profile has no battery ADC
or deep sleep. See examples/wifi-led-strip.yaml for an optional WS2812 strip.

The public gtag-esp32-c3-wifi.yaml asset instead fetches a pinned GitHub package.
Do not share filled-in secrets.yaml or a wizard YAML containing credentials.

Wi-Fi, the HA weather layout, OTA and reconnection were verified on a physical
ESP32-C3 Super Mini. The WS2812 example was compile-tested only.
For Auth Expired, try uncommenting power_save_mode and output_power in the YAML,
then rebuild and flash over USB. Reduced TX power may reduce range.
Full instructions: https://github.com/lukdut/gtag-nrf-display/blob/v1.1.0/docs/esp32-wifi.md
"""


def package_wifi(dist: Path | None = None) -> Path:
    config = ROOT / "config/esphome"
    dist = dist if dist is not None else ROOT / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    files = [config / "esp32-gtag-display.yaml", config / "packages/wifi-base.yaml",
             config / "examples/wifi-led-strip.yaml"]
    files += sorted(p for p in (config / "components/gtag_display").iterdir()
                    if p.is_file() and p.suffix in {".py", ".h", ".cpp"})
    path = dist / ARCHIVE_NAME
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.write(ROOT / "LICENSE", "LICENSE")
        archive.writestr("README.txt", README)
        archive.writestr("secrets.example.yaml", 'wifi_ssid: ""\nwifi_password: ""\n'
                         'gtag_api_key: ""\ngtag_ota_password: ""\n')
        for file in files:
            archive.write(file, file.relative_to(config))
    shutil.copyfile(config / PUBLIC_NAME, dist / PUBLIC_NAME)
    print(f"Packaged Wi-Fi: {path.name}, {PUBLIC_NAME}")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, help="Override the output directory")
    package_wifi(parser.parse_args().dist)
