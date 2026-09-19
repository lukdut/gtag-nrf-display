#!/usr/bin/env bash
# Disposable real HA + broker, with a client outside HA exercising public APIs.
set -euo pipefail
cd "$(dirname "$0")/../.."
: "${HA_IMAGE:=ghcr.io/home-assistant/home-assistant:2026.9.2}"
: "${ARTIFACT_DIR:=$PWD/dist/container-check}"
export HA_IMAGE ARTIFACT_DIR
mkdir -p "$ARTIFACT_DIR"
work=$(mktemp -d /tmp/gtag-container-XXXXXX)
export GTAG_CONTAINER_CONFIG="$work/config"
mkdir "$GTAG_CONTAINER_CONFIG"
compose=(docker compose -p "gtag-check-$$" -f tests/container/compose.yaml)
export GTAG_COMPOSE_PROJECT="gtag-check-$$"
cleanup() {
  result=$?
  trap - EXIT
  "${compose[@]}" logs --no-color > "$ARTIFACT_DIR/containers.log" 2>&1 || true
  "${compose[@]}" down --volumes --remove-orphans || true
  # Do not upload /config: it contains the disposable owner's credentials.
  if [[ $result == 0 ]]; then
    python3 tests/container/check_logs.py "$ARTIFACT_DIR/containers.log" || result=$?
  fi
  exit "$result"
}
trap cleanup EXIT
python3 scripts/package.py
python3 - "$GTAG_CONTAINER_CONFIG" <<'PY'
from pathlib import Path
import sys
from zipfile import ZipFile
config = Path(sys.argv[1])
assert not (config / '.storage').exists()
with ZipFile('dist/gtag-ha-integration.zip') as archive:
    assert archive.testzip() is None
    archive.extractall(config)
# No default_config: no unrelated cloud/device discovery, Bluetooth or Supervisor.
# All GTag dependencies still have to be loaded by HA from the installed archive.
(config / 'configuration.yaml').write_text('''homeassistant:
  name: GTag Container Check
  time_zone: UTC
  latitude: 0
  longitude: 0
  elevation: 0
frontend:
config:
api:
http:
history:
recorder:
weather:
  - platform: template
    name: GTag Container Weather
    condition_template: partlycloudy
    temperature_template: "{{ states('input_number.gtag_temperature') }}"
    temperature_unit: "°C"
    humidity_template: "{{ states('input_number.gtag_humidity') }}"
    forecast_hourly_template: >-
      {% set result = namespace(items=[]) %}
      {% for hour in range(1, 7) %}
        {% set result.items = result.items + [{'datetime': (now() + timedelta(hours=hour)).isoformat(),
          'condition': 'rainy', 'temperature': 22 - hour}] %}
      {% endfor %}
      {{ result.items }}
logger:
  default: warning
input_number:
  gtag_temperature:
    name: Test temperature
    min: -50
    max: 100
    initial: 21.5
    step: 0.1
  gtag_humidity:
    name: Test humidity
    min: 0
    max: 100
    initial: 45
  gtag_replacement:
    name: Import destination
    min: -50
    max: 100
    initial: 23
''', encoding='utf-8')
PY
"${compose[@]}" pull
docker image inspect "$HA_IMAGE" --format '{{json .}}' > "$ARTIFACT_DIR/ha-image.json"
python3 - "$ARTIFACT_DIR/ha-image.json" <<'PY'
import json, os, platform, sys
info = json.load(open(sys.argv[1]))
host = {'x86_64': 'amd64', 'aarch64': 'arm64'}[platform.machine()]
assert info['Architecture'] == host, (info['Architecture'], host)
assert os.environ.get('EXPECTED_ARCH', host) == host
print(f'Native HA Container: linux/{host}')
PY
"${compose[@]}" up -d
python3 -u tests/container/smoke.py
