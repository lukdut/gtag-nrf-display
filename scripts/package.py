#!/usr/bin/env python3
"""Package the HA integration, including its bundled fonts and service metadata."""
from hashlib import sha256
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    component = root / "custom_components" / "gtag_ble_test"
    for required in ("manifest.json", "LICENSE.txt", "services.yaml", "layouts.py", "battery.py", "zigbee.py",
                     "firmware_config.py", "firmware_flow.py", "firmware_download.py",
                     "layout_transfer.py", "layout_flow.py", "layout_download.py",
                     "fonts/DejaVuSans.ttf", "fonts/LICENSE.txt",
                     "translations/en.json", "translations/ru.json", "brand/icon.png"):
        if not (component / required).is_file():
            raise SystemExit(f"Missing required package file: {required}")
    dist = root / "dist"
    dist.mkdir(exist_ok=True)
    archive = dist / "gtag-ha-integration.zip"
    temporary = archive.with_suffix(".zip.tmp")
    files = sorted(path for path in component.rglob("*")
                   if path.is_file() and "__pycache__" not in path.parts
                   and path.suffix in {".py", ".json", ".yaml", ".ttf", ".txt", ".png"})
    with ZipFile(temporary, "w", ZIP_DEFLATED, compresslevel=9) as output:
        for path in files:
            output.write(path, path.relative_to(root).as_posix())
    temporary.replace(archive)
    artifacts = [archive]
    if (firmware := dist / "gtag-display.uf2").is_file():
        artifacts.append(firmware)
    (dist / "SHA256SUMS").write_text("".join(
        f"{sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in artifacts
    ), encoding="utf-8")
    version = json.loads((component / "manifest.json").read_text())["version"]
    print(f"GTag Display {version}: {archive} ({len(files)} files, {archive.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
