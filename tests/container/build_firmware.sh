#!/usr/bin/env bash
# Compile the unchanged YAML downloaded from a real HA wizard, without caches.
set -euo pipefail
cd "$(dirname "$0")/../.."
: "${ESPHOME_IMAGE:=ghcr.io/esphome/esphome:2026.9.0}"
: "${ARTIFACT_DIR:=$PWD/dist/firmware-check}"
: "${WIZARD_YAML:?Set WIZARD_YAML to the downloaded YAML}"
export ESPHOME_IMAGE ARTIFACT_DIR
mkdir -p "$ARTIFACT_DIR"
work=$(mktemp -d /tmp/gtag-build-XXXXXX)
mkdir "$work/config" "$work/sdk"
cp "$WIZARD_YAML" "$work/config/gtag-container-check.yaml"
cp "$WIZARD_YAML" "$ARTIFACT_DIR/gtag-container-check.yaml"
docker pull "$ESPHOME_IMAGE"
docker image inspect "$ESPHOME_IMAGE" --format '{{json .}}' > "$ARTIFACT_DIR/esphome-image.json"
python3 - "$ARTIFACT_DIR/esphome-image.json" <<'PY'
import json, os, platform, sys
info = json.load(open(sys.argv[1]))
host = {'x86_64': 'amd64', 'aarch64': 'arm64'}[platform.machine()]
assert info['Architecture'] == host, (info['Architecture'], host)
assert os.environ.get('EXPECTED_ARCH', host) == host
print(f'Native ESPHome: linux/{host}; empty SDK and build directories')
PY
docker run --rm -v "$work/config:/config" -v "$work/sdk:/sdk" \
  -e ESPHOME_SDK_NRF_PREFIX=/sdk "$ESPHOME_IMAGE" \
  compile /config/gtag-container-check.yaml 2>&1 | tee "$ARTIFACT_DIR/build.log"
docker run --rm --entrypoint python3 -v "$work/sdk:/sdk:ro" "$ESPHOME_IMAGE" -c '
import json, platform, struct, subprocess
from pathlib import Path
compiler, = Path("/sdk").glob("**/bin/arm-zephyr-eabi-gcc")
header = compiler.read_bytes()[:20]
assert header[:4] == b"\x7fELF"
machine = struct.unpack_from("<H", header, 18)[0]
assert machine == {"x86_64": 62, "aarch64": 183}[platform.machine()], machine
print(json.dumps({"compiler_host": platform.machine(), "elf_machine": machine,
    "compiler_version": subprocess.check_output([str(compiler), "--version"], text=True)}, indent=2))
' > "$ARTIFACT_DIR/compiler.json"
python3 tests/container/record_build.py "$work/config"
