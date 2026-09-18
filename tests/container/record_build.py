"""Check UF2 address range and record precisely which public firmware was built."""
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from package_firmware import validate_uf2

config = Path(sys.argv[1])
output = Path(os.environ["ARTIFACT_DIR"])
uf2 = config / ".esphome/build/gtag-container-check/.pioenvs/gtag-container-check/zephyr/zephyr.uf2"
validate_uf2(uf2, app_start=0x26000)
shutil.copyfile(uf2, output / "gtag-promicro-zigbee.uf2")
document = yaml.safe_load((config / "gtag-container-check.yaml").read_text())
revisions = []
for git_dir in sorted(config.glob(".esphome/**/.git")):
    revisions.append({"path": str(git_dir.parent.relative_to(config)), "commit": subprocess.check_output(
        ["git", "-c", "safe.directory=" + str(git_dir.parent), "-C", str(git_dir.parent), "rev-parse", "HEAD"], text=True).strip()})
assert revisions, "No downloaded public package/component repositories found"
report = {
    "esphome_image": os.environ["ESPHOME_IMAGE"], "host_arch": platform.machine(),
    "test_commit": os.environ.get("GITHUB_SHA"), "firmware_package": document["packages"]["gtag"],
    "downloaded_revisions": revisions, "cold_build": True, "app_start": "0x26000",
    "uf2_sha256": sha256(uf2.read_bytes()).hexdigest(), "uf2_bytes": uf2.stat().st_size,
    "yaml_sha256": sha256((config / "gtag-container-check.yaml").read_bytes()).hexdigest(),
    "physical_flash_test": False,
}
(output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
