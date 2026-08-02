#!/usr/bin/env python3
"""Smoke-test the checked-in Node bridge inventory protocol."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "hermes_lark" / "node" / "openclaw_tools_bridge.mjs"
MANIFEST = ROOT / "hermes_lark" / "data" / "openclaw-tools.json"
HOST_INTERACTIVE_TOOLS = {
    "feishu_ask_user_question",
    "feishu_oauth_batch_auth",
}


def main() -> int:
    """Require the bridge to list every non-interactive manifest tool."""
    request = {
        "action": "list",
        "config": {
            "channels": {
                "feishu": {
                    "enabled": True,
                    "appId": "test",
                    "appSecret": "test",
                }
            }
        },
    }
    completed = subprocess.run(
        ["node", str(BUNDLE)],
        input=json.dumps(request),
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        print(completed.stderr, file=sys.stderr)
        return completed.returncode or 1
    response = json.loads(completed.stdout)
    if not response.get("ok"):
        print(response, file=sys.stderr)
        return 1

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    expected = [
        tool["name"]
        for tool in manifest["tools"]
        if tool["name"] not in HOST_INTERACTIVE_TOOLS
    ]
    if response.get("result") != expected:
        print("Node bridge inventory differs from the tool manifest.", file=sys.stderr)
        return 1
    print(f"Node bridge registered {len(expected)} one-shot tools.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
