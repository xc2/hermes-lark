#!/usr/bin/env python3
"""Check public-project metadata, permissions, and generated provenance."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUNDLE_PATH = ROOT / "hermes_lark" / "node" / "openclaw_tools_bridge.mjs"
BUNDLE_DIGEST_PATH = BUNDLE_PATH.with_suffix(f"{BUNDLE_PATH.suffix}.sha256")
PARITY_FIXTURE_PATH = ROOT / "tests" / "fixtures" / "openclaw_lark_parity.json"
THIRD_PARTY_NOTICES_PATH = ROOT / "THIRD_PARTY_NOTICES.md"
UPSTREAM_COMMIT = "dde0be3680d6fd5443cab426c8f4b3216266346a"


def project_version() -> str:
    """Read the canonical Python project version."""
    with (ROOT / "pyproject.toml").open("rb") as stream:
        return str(tomllib.load(stream)["project"]["version"])


def manifest_version() -> str:
    """Read the simple top-level version scalar without a YAML dependency."""
    content = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    match = re.search(r"(?m)^version:\s*([^\s#]+)", content)
    if not match:
        raise AssertionError("plugin.yaml has no top-level version")
    return match.group(1).strip('"\'')


def docker_expected_version() -> str:
    """Read the expected hermes-lark version from the Docker build assertion."""
    content = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    match = re.search(r"'hermes-lark':'([^']+)'", content)
    if not match:
        raise AssertionError("Dockerfile does not assert the hermes-lark version")
    return match.group(1)


def commands_fallback_version() -> str:
    """Read the source-checkout fallback used by the command module."""
    content = (ROOT / "hermes_lark" / "commands.py").read_text(encoding="utf-8")
    match = re.search(
        r'except metadata\.PackageNotFoundError:\s+return "([^"]+)"',
        content,
    )
    if not match:
        raise AssertionError("commands.py has no package-version fallback")
    return match.group(1)


def check_versions() -> None:
    """Require every public version declaration to match."""
    versions = {
        "pyproject.toml": project_version(),
        "plugin.yaml": manifest_version(),
        "Dockerfile": docker_expected_version(),
        "commands.py fallback": commands_fallback_version(),
    }
    if len(set(versions.values())) != 1:
        raise AssertionError(f"project versions differ: {versions}")


def check_permissions() -> None:
    """Require the E2E permission manifest to remain a production superset."""
    production = json.loads(
        (ROOT / "permissions" / "production.json").read_text(encoding="utf-8")
    )["scopes"]
    e2e = json.loads(
        (ROOT / "permissions" / "e2e.json").read_text(encoding="utf-8")
    )["scopes"]
    for identity in ("tenant", "user"):
        missing = set(production[identity]) - set(e2e[identity])
        if missing:
            raise AssertionError(
                f"E2E {identity} scopes omit production values: {sorted(missing)}"
            )


def check_bundle() -> None:
    """Verify the generated bridge checksum and pinned provenance banner."""
    bundle = BUNDLE_PATH.read_bytes()
    digest = hashlib.sha256(bundle).hexdigest()
    expected = BUNDLE_DIGEST_PATH.read_text(encoding="ascii").split()[0]
    if digest != expected:
        raise AssertionError(
            "generated Node bridge checksum differs; rebuild it through "
            "scripts/rebuild_bridge.sh"
        )
    banner = bundle[:512].decode("utf-8", errors="replace")
    if UPSTREAM_COMMIT not in banner:
        raise AssertionError("generated Node bridge banner has the wrong upstream commit")

    fixture = json.loads(PARITY_FIXTURE_PATH.read_text(encoding="utf-8"))
    fixture_commit = fixture["upstream"]["commit"]
    if fixture_commit != UPSTREAM_COMMIT:
        raise AssertionError("parity fixture has the wrong upstream commit")
    fixture_digest = fixture["generated_bridge"]["sha256"]
    if digest != fixture_digest:
        raise AssertionError("parity fixture has the wrong generated bridge checksum")

    notices = THIRD_PARTY_NOTICES_PATH.read_text(encoding="utf-8")
    if digest not in notices or UPSTREAM_COMMIT not in notices:
        raise AssertionError("third-party notices have stale bundle provenance")


def main() -> int:
    """Run all project consistency checks."""
    try:
        check_versions()
        check_permissions()
        check_bundle()
    except (AssertionError, KeyError, OSError, ValueError) as error:
        print(f"Project check failed: {error}", file=sys.stderr)
        return 1
    print("Project metadata and provenance checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
