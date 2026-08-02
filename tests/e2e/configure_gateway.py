"""Configure the isolated Docker gateway for deterministic Feishu E2E runs."""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml


def main() -> None:
    """Replace disposable state with the isolated E2E configuration."""
    from hermes_cli.config import DEFAULT_CONFIG

    hermes_home = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    config_path = hermes_home / "config.yaml"
    config = {
        "_config_version": DEFAULT_CONFIG["_config_version"],
        "model": {
            "default": "hermes-lark-e2e",
            "provider": "custom",
            "base_url": "http://model-stub:8000/v1",
            "api_key": "no-key-required",
            "api_mode": "chat_completions",
            "supports_vision": True,
        },
        "plugins": {"enabled": ["platforms/feishu"]},
        "gateway": {
            "platforms": {
                "feishu": {
                    "enabled": True,
                    "connectionMode": "websocket",
                    "dmPolicy": "open",
                    "groupPolicy": "open",
                    "allowBots": "mentions",
                    "textChunkLimit": 1000,
                    "chunkMode": "none",
                    "streaming": True,
                    "replyMode": "auto",
                    "cardkitE2ETracePath": (
                        "/opt/data/feishu_cardkit_e2e_trace.jsonl"
                    ),
                }
            }
        },
        "streaming": {
            "enabled": True,
            "transport": "edit",
            "edit_interval": 0.2,
            "buffer_threshold": 1,
        },
        "display": {
            "platforms": {
                "feishu": {
                    "streaming": True,
                    "tool_progress": "off",
                    "show_reasoning": True,
                }
            }
        },
        "memory": {
            "memory_enabled": False,
            "user_profile_enabled": False,
        },
        "compression": {"enabled": False},
        "session_reset": {"mode": "none"},
        "approvals": {"mode": "manual", "timeout": 120},
    }

    config_path.parent.mkdir(parents=True, exist_ok=True)
    (hermes_home / "feishu_cardkit_e2e_trace.jsonl").unlink(
        missing_ok=True
    )
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    (hermes_home / "gateway_state.json").write_text(
        json.dumps(
            {
                "gateway_state": "stopped",
                "desired_state": "stopped",
                "active_agents": 0,
                "restart_requested": False,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    missing = [
        name
        for name in ("FEISHU_APP_ID", "FEISHU_APP_SECRET")
        if not os.environ.get(name)
    ]
    print(f"Configured deterministic E2E gateway at {config_path}")
    if missing:
        print(
            "Add the missing names to the repository .env before starting: "
            + ", ".join(missing)
        )


if __name__ == "__main__":
    main()
