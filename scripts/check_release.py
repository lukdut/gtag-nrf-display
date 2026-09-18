#!/usr/bin/env python3
"""Check that public ESPHome packages and release notes match the HA version."""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="Also require this Git tag to match")
    args = parser.parse_args()
    version = json.loads((ROOT / "custom_components/gtag_ble_test/manifest.json").read_text())["version"]
    tag = f"v{version}"
    if args.tag is not None and args.tag != tag:
        raise SystemExit(f"Expected release tag {tag}, got {args.tag}")
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
    print(f"Release metadata matches {tag}")


if __name__ == "__main__":
    main()
