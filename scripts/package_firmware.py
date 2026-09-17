#!/usr/bin/env python3
"""Package a built application UF2 and matching standalone ESPHome sources."""
import argparse
from pathlib import Path
import shutil
import struct
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]


def validate_uf2(path: Path) -> None:
    raw = path.read_bytes()
    if not raw or len(raw) % 512:
        raise ValueError("Invalid UF2 length")
    addresses = []
    for offset in range(0, len(raw), 512):
        block = raw[offset:offset + 512]
        magic0, magic1, flags, address, size, number, total, family = struct.unpack_from("<8I", block)
        if ((magic0, magic1) != (0x0A324655, 0x9E5D5157)
                or struct.unpack_from("<I", block, 508)[0] != 0x0AB16F30
                or flags != 0x2000 or family != 0xADA52840
                or size != 256 or number != offset // 512 or total != len(raw) // 512
                or not 0x26000 <= address < address + size <= 0xE9000):
            raise ValueError(f"Invalid or non-application UF2 block {offset // 512}")
        addresses.append(address)
    if min(addresses) != 0x26000 or len(addresses) != len(set(addresses)):
        raise ValueError("Invalid UF2 application addresses")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=("ble", "zigbee"))
    parser.add_argument("--uf2", type=Path, required=True)
    args = parser.parse_args()
    validate_uf2(args.uf2)
    config = ROOT / "config/esphome"
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    name = "gtag-display" if args.profile == "ble" else "gtag-zigbee"
    local_name = "nrf-gtag-display.yaml" if args.profile == "ble" else "nrf-gtag-zigbee.yaml"
    shutil.copyfile(args.uf2, dist / f"{name}.uf2")
    shutil.copyfile(config / f"gtag-{args.profile}.yaml", dist / f"gtag-{args.profile}.yaml")
    files = [config / local_name, config / "packages/board.yaml", config / f"packages/{args.profile}-base.yaml"]
    files += sorted(p for p in (config / "components/gtag_display").iterdir()
                    if p.is_file() and p.suffix in {".py", ".h", ".cpp"})
    with ZipFile(dist / f"{name}-esphome.zip", "w", ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(config))
    if args.profile == "zigbee":
        shutil.copyfile(ROOT / "zigbee2mqtt/gtag-display.mjs", dist / "gtag-display.mjs")
    print(f"Packaged {args.profile}: {name}.uf2, {name}-esphome.zip, gtag-{args.profile}.yaml")


if __name__ == "__main__":
    main()
