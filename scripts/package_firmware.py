#!/usr/bin/env python3
"""Package a built application UF2 and matching standalone ESPHome sources."""
import argparse
from pathlib import Path
import shutil
import struct
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]


def validate_uf2(path: Path, *, app_start: int = 0x26000) -> None:
    if app_start not in (0x26000, 0x27000):
        raise ValueError("Unsupported application start")
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
                or not app_start <= address < address + size <= 0xE9000):
            raise ValueError(f"Invalid or non-application UF2 block {offset // 512}")
        addresses.append(address)
    if min(addresses) != app_start or len(addresses) != len(set(addresses)):
        raise ValueError("Invalid UF2 application addresses")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=("ble", "zigbee"))
    parser.add_argument("--uf2", type=Path, required=True)
    parser.add_argument("--board", choices=("promicro", "super52840"), default="promicro")
    args = parser.parse_args()
    if args.board == "super52840" and args.profile != "zigbee":
        parser.error("The packaged Super52840 configuration uses Zigbee")
    validate_uf2(args.uf2, app_start=0x27000 if args.board == "super52840" else 0x26000)
    config = ROOT / "config/esphome"
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    name = "gtag-display" if args.profile == "ble" else "gtag-zigbee"
    local_name = "nrf-gtag-display.yaml" if args.profile == "ble" else "nrf-gtag-zigbee.yaml"
    public_name = f"gtag-{args.profile}.yaml"
    if args.board == "super52840":
        name = "gtag-super52840-zigbee"
        local_name = "nrf-gtag-super52840-zigbee.yaml"
        public_name = "gtag-super52840-zigbee.yaml"
    shutil.copyfile(args.uf2, dist / f"{name}.uf2")
    shutil.copyfile(config / public_name, dist / public_name)
    files = [config / local_name, config / "packages/board.yaml", config / f"packages/{args.profile}-base.yaml"]
    if args.profile == "zigbee":
        files.append(config / "packages/zigbee-no-battery.yaml")
    if args.board == "super52840":
        files.append(config / "packages/super52840.yaml")
    files += sorted(p for p in (config / "components/gtag_display").iterdir()
                    if p.is_file() and p.suffix in {".py", ".h", ".cpp"})
    with ZipFile(dist / f"{name}-esphome.zip", "w", ZIP_DEFLATED) as archive:
        archive.write(ROOT / "LICENSE", "LICENSE")
        for path in files:
            archive.write(path, path.relative_to(config))
    if args.profile == "zigbee":
        shutil.copyfile(ROOT / "zigbee2mqtt/gtag-display.mjs", dist / "gtag-display.mjs")
    print(f"Packaged {args.profile}: {name}.uf2, {name}-esphome.zip, {public_name}")


if __name__ == "__main__":
    main()
