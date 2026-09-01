#!/usr/bin/env python3
"""Build a deterministic, stdlib-only Obsidian public-executor bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo

MODULE_ROOT = Path(__file__).resolve(strict=True).parent
REPOSITORY_ROOT = MODULE_ROOT.parents[1]
PROFILE_PATH = MODULE_ROOT / "public-executor-profile.json"
FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def build(output: Path) -> dict[str, object]:
    """Build one content-addressed pyz and return its safe receipt."""

    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    if profile.get("schema") != "trans-hub.public-executor-profile.v1":
        raise ValueError("public_executor_profile_invalid")
    files = profile.get("artifact", {}).get("files")
    if not isinstance(files, list) or not all(isinstance(name, str) for name in files):
        raise ValueError("public_executor_profile_invalid")
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_STORED, strict_timestamps=True) as archive:
        for name in sorted(files):
            payload = (
                b"from adapters.obsidian.component_bridge import public_main\npublic_main()\n"
                if name == "__main__.py"
                else (REPOSITORY_ROOT / name).read_bytes()
            )
            entry = ZipInfo(name, date_time=FIXED_TIMESTAMP)
            entry.compress_type = ZIP_STORED
            entry.external_attr = 0o100755 << 16
            archive.writestr(entry, payload)
    artifact = output.read_bytes()
    return {
        "artifactDigest": hashlib.sha256(artifact).hexdigest(),
        "artifactSize": len(artifact),
        "profileDigest": hashlib.sha256(canonical_json(profile)).hexdigest(),
        "protocol": profile["bridgeProtocol"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(canonical_json(build(args.output)).decode("utf-8"))


if __name__ == "__main__":
    main()
