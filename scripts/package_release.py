#!/usr/bin/env python3
"""Complete the release bundle after packaging both firmware profiles."""
from hashlib import sha256
from pathlib import Path

from package import main as package_integration
from package_firmware import validate_uf2

ARTIFACTS = (
    "gtag-ha-integration.zip", "gtag-display.uf2", "gtag-zigbee.uf2",
    "gtag-display-esphome.zip", "gtag-zigbee-esphome.zip",
    "gtag-ble.yaml", "gtag-zigbee.yaml", "gtag-display.mjs",
)


def main() -> None:
    dist = Path(__file__).resolve().parents[1] / "dist"
    for name in ARTIFACTS[1:]:
        if not (dist / name).is_file():
            raise SystemExit(f"Missing release artifact: {name}")
    for name in ("gtag-display.uf2", "gtag-zigbee.uf2"):
        validate_uf2(dist / name)
    package_integration()
    (dist / "SHA256SUMS").write_text("".join(
        f"{sha256((dist / name).read_bytes()).hexdigest()}  {name}\n" for name in ARTIFACTS
    ), encoding="utf-8")
    print(f"Complete release: {len(ARTIFACTS)} artifacts + SHA256SUMS")


if __name__ == "__main__":
    main()
