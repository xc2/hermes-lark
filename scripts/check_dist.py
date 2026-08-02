#!/usr/bin/env python3
"""Inspect wheel and sdist contents required by a standalone installation."""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path


REQUIRED_WHEEL_SUFFIXES = {
    "hermes_lark/__init__.py",
    "hermes_lark/adapter.py",
    "hermes_lark/data/multilingual-stop-intents.json",
    "hermes_lark/data/openclaw-tools.json",
    "hermes_lark/node/openclaw_tools_bridge.mjs",
    "hermes_lark/node/openclaw_tools_bridge.mjs.sha256",
    "hermes_lark/permissions/e2e.json",
    "hermes_lark/permissions/production.json",
    "hermes_lark/plugin.yaml",
    "hermes_lark/skills/feishu-channel-rules/SKILL.md",
    "hermes_lark/THIRD_PARTY_NOTICES.md",
}
REQUIRED_SDIST_SUFFIXES = {
    ".dockerignore",
    ".env.example",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "Dockerfile",
    "LICENSE",
    "NOTICE",
    "README.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "compose.validation.yaml",
    "docs/CONFIGURATION.md",
    "docs/MIGRATION.md",
    "docs/PARITY.md",
    "permissions/e2e.json",
    "permissions/production.json",
    "tests/e2e/README.md",
    "tests/test_parity_contract.py",
}


def find_one(directory: Path, pattern: str) -> Path:
    """Return the single artifact matching a distribution glob."""
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        raise AssertionError(f"expected one {pattern} artifact, found {len(matches)}")
    return matches[0]


def check_suffixes(names: set[str], required: set[str], label: str) -> None:
    """Require every normalized suffix to exist in one archive."""
    missing = {
        suffix
        for suffix in required
        if not any(name == suffix or name.endswith(f"/{suffix}") for name in names)
    }
    if missing:
        raise AssertionError(f"{label} omits required files: {sorted(missing)}")


def main() -> int:
    """Inspect the artifacts in the supplied directory."""
    directory = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
    try:
        wheel = find_one(directory, "*.whl")
        sdist = find_one(directory, "*.tar.gz")
        with zipfile.ZipFile(wheel) as archive:
            wheel_names = set(archive.namelist())
            check_suffixes(wheel_names, REQUIRED_WHEEL_SUFFIXES, "wheel")
            entry_points = [
                name for name in wheel_names if name.endswith(".dist-info/entry_points.txt")
            ]
            if len(entry_points) != 1:
                raise AssertionError("wheel has no unique entry_points.txt")
            content = archive.read(entry_points[0]).decode("utf-8")
            if "platforms/feishu = hermes_lark" not in content:
                raise AssertionError("wheel omits the platforms/feishu entry point")
        with tarfile.open(sdist, "r:gz") as archive:
            check_suffixes(set(archive.getnames()), REQUIRED_SDIST_SUFFIXES, "sdist")
    except (AssertionError, OSError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f"Distribution check failed: {error}", file=sys.stderr)
        return 1
    print("Wheel and sdist contents passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
