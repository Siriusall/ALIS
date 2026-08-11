#!/usr/bin/env python3
"""Inventory version-like protocol identifiers without treating them all as release gates."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.system_release_center import CONTRACT_CATALOG  # noqa: E402

VERSION_PATTERN = re.compile(r"\b(?:alis|point_in_time)_[a-z0-9_]+_v\d+\b")


def inventory() -> dict:
    occurrences: dict[str, list[str]] = {}
    for path in sorted((ROOT / "app").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for value in VERSION_PATTERN.findall(text):
            occurrences.setdefault(value, []).append(str(path.relative_to(ROOT)))
    critical_versions = {str(item["version"]) for item in CONTRACT_CATALOG.values()}
    rows = []
    for value, files in sorted(occurrences.items()):
        rows.append({
            "identifier": value,
            "classification": "release_critical" if value in critical_versions else "internal_protocol",
            "files": sorted(set(files)),
        })
    return {
        "schema_version": "alis_contract_identifier_inventory_v1",
        "catalogued_release_contracts": len(CONTRACT_CATALOG),
        "unique_version_identifiers": len(rows),
        "items": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = inventory()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"release contracts: {result['catalogued_release_contracts']}")
    print(f"version identifiers: {result['unique_version_identifiers']}")
    for row in result["items"]:
        print(f"{row['classification']:<18} {row['identifier']}")


if __name__ == "__main__":
    main()
