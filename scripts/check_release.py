#!/usr/bin/env python3
"""Check that public ESPHome packages and release notes match the HA version."""
import argparse
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="Also require this Git tag to match")
    args = parser.parse_args()
    version = json.loads((ROOT / "custom_components/gtag_ble_test/manifest.json").read_text())["version"]
    if (ROOT / "LICENSE").read_bytes() != (ROOT / "custom_components/gtag_ble_test/LICENSE.txt").read_bytes():
        raise SystemExit("The integration license must match the project LICENSE")
    tag = f"v{version}"
    if args.tag is not None and args.tag != tag:
        raise SystemExit(f"Expected release tag {tag}, got {args.tag}")
    header = (ROOT / "config/esphome/components/gtag_display/firmware_info.h").read_text()
    firmwares = re.findall(r'char VERSION\[\] = "([^"]+)";', header)
    if not firmwares or any(not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", firmware)
                            or len(firmware) > 19 for firmware in firmwares):
        raise SystemExit("Invalid or missing firmware version")
    # Firmware can keep its own version during HA-only releases, but a stable
    # release must not accidentally ship a development/prerelease firmware.
    if args.tag is not None and "-" not in version and any("-" in firmware for firmware in firmwares):
        raise SystemExit(f"Stable release cannot ship prerelease firmware {', '.join(firmwares)}")
    if not (ROOT / f"docs/releases/{version}.md").is_file():
        raise SystemExit(f"Missing release notes for {version}")
    for profile in ("ble", "zigbee", "wifi"):
        public_name = "gtag-esp32-c3-wifi" if profile == "wifi" else f"gtag-{profile}"
        public = ROOT / f"config/esphome/{public_name}.yaml"
        package = ROOT / f"config/esphome/packages/{profile}.yaml"
        if f"packages/{profile}.yaml@{tag}" not in public.read_text():
            raise SystemExit(f"Wrong package pin in {public}")
        if f"ref: {tag}\n" not in package.read_text():
            raise SystemExit(f"Wrong component pin in {package}")
    wizard = (ROOT / "custom_components/gtag_ble_test/firmware_config.py").read_text()
    if f'WIFI_FIRMWARE_TAG = "{tag}"' not in wizard:
        raise SystemExit("The Wi-Fi wizard must use the release package tag")
    public = ROOT / "config/esphome/gtag-super52840-zigbee.yaml"
    for package in ("zigbee", "super52840", "zigbee-no-battery"):
        if f"packages/{package}.yaml@{tag}" not in public.read_text():
            raise SystemExit(f"Wrong {package} package pin in {public}")
    print(f"Release metadata matches {tag}; firmware {', '.join(firmwares)}")


if __name__ == "__main__":
    main()
