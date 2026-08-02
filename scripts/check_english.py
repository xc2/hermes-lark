#!/usr/bin/env python3
"""Reject non-Latin prose outside approved machine-data exceptions."""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERATED_EXCEPTIONS = {
    Path("hermes_lark/node/openclaw_tools_bridge.mjs"),
}
MULTILINGUAL_INPUT_EXCEPTIONS = {
    Path("hermes_lark/data/multilingual-stop-intents.json"),
    Path("tests/test_bot_peer_mentions.py"),
}
IGNORED_DIRECTORIES = {
    ".git",
    ".hermes-secrets",
    ".hermes-validation",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "node_modules",
}


def contains_non_latin_letter(value: str) -> bool:
    """Return whether text contains a letter from a non-Latin script."""
    for character in value:
        if ord(character) < 128 or not character.isalpha():
            continue
        if "LATIN" not in unicodedata.name(character, ""):
            return True
    return False


def contains_escaped_non_latin_letter(value: str) -> bool:
    """Return whether Unicode escapes encode letters from a non-Latin script."""
    for group in re.findall(r"(?:\\u[0-9A-Fa-f]{4})+", value):
        characters = "".join(
            chr(int(codepoint, 16))
            for codepoint in re.findall(r"\\u([0-9A-Fa-f]{4})", group)
        )
        if contains_non_latin_letter(characters):
            return True
    return False


def candidate_files() -> list[Path]:
    """Return repository files that may contain authored UTF-8 text."""
    candidates: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if any(part in IGNORED_DIRECTORIES for part in relative.parts):
            continue
        if relative in GENERATED_EXCEPTIONS:
            continue
        if path.name == ".env" or (
            path.name.startswith(".env.") and path.name != ".env.example"
        ):
            continue
        candidates.append(path)
    return sorted(candidates)


def main() -> int:
    """Print every violation and return a nonzero status when any exist."""
    violations: list[str] = []
    for path in candidate_files():
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(content.splitlines(), start=1):
            relative = path.relative_to(ROOT)
            if contains_non_latin_letter(line) or (
                relative not in MULTILINGUAL_INPUT_EXCEPTIONS
                and contains_escaped_non_latin_letter(line)
            ):
                excerpt = line.strip()
                if len(excerpt) > 160:
                    excerpt = f"{excerpt[:157]}..."
                violations.append(f"{relative}:{line_number}: {excerpt}")

    if violations:
        print("Non-English script found outside approved machine-data exceptions:")
        print("\n".join(violations))
        return 1

    print("English-content policy passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
