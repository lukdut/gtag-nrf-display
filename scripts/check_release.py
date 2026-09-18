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
    match = re.search(r'char VERSION\[\] = "([^"]+)";', header)
    firmware = match[1] if match else ""
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", firmware) or len(firmware) > 19:
        raise SystemExit("Invalid or missing firmware version")
    # Firmware can keep its own version during HA-only releases, but a stable
    # release must not accidentally ship a development/prerelease firmware.
    if args.tag is not None and "-" not in version and "-" in firmware:
        raise SystemExit(f"Stable release cannot ship prerelease firmware {firmware}")
    if not (ROOT / f"docs/releases/{version}.md").is_file():
        raise SystemExit(f"Missing release notes for {version}")
    for profile in ("ble", "zigbee"):
        public = ROOT / f"config/esphome/gtag-{profile}.yaml"
        package = ROOT / f"config/esphome/packages/{profile}.yaml"
        if f"packages/{profile}.yaml@{tag}" not in public.read_text():
            raise SystemExit(f"Wrong package pin in {public}")
        if f"ref: {tag}\n" not in package.read_text():
            raise SystemExit(f"Wrong component pin in {package}")
    public = ROOT / "config/esphome/gtag-super52840-zigbee.yaml"
    for package in ("zigbee", "super52840", "zigbee-no-battery"):
        if f"packages/{package}.yaml@{tag}" not in public.read_text():
            raise SystemExit(f"Wrong {package} package pin in {public}")
    print(f"Release metadata matches {tag}; firmware {firmware}")


if __name__ == "__main__":
    main()
