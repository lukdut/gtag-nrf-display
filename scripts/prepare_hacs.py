#!/usr/bin/env python3
"""Set real publication metadata after a GitHub repository has been chosen.

This changes local files only. It never creates a repository or pushes commits.
"""
import argparse
import json
from pathlib import Path
import re


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", help="GitHub owner/repository (must already be chosen)")
    parser.add_argument("--codeowner", required=True, help="Maintainer's GitHub username, without @")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", args.repository):
        parser.error("repository must have the form owner/repository")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", args.codeowner):
        parser.error("codeowner must be a GitHub username without @")
    root = Path(__file__).resolve().parents[1]
    path = root / "custom_components/gtag_ble_test/manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    url = f"https://github.com/{args.repository}"
    manifest.update(documentation=f"{url}#readme", issue_tracker=f"{url}/issues",
                    codeowners=[f"@{args.codeowner}"])
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Updated local manifest for {url}. Nothing has been published.")


if __name__ == "__main__":
    main()
