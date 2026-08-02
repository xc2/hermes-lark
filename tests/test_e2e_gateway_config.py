"""Tests for the isolated Docker E2E gateway configuration."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

from tests.e2e import configure_gateway


class E2EGatewayConfigTests(unittest.TestCase):
    """Verify every run receives a deterministic single-platform gateway."""

    def test_configuration_replaces_stale_plugins_platforms_and_tools(self) -> None:
        """Disposable state cannot carry unrelated integrations into live E2E."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "_config_version": 1,
                        "plugins": {"enabled": ["stale/plugin"]},
                        "gateway": {
                            "platforms": {
                                "slack": {"enabled": True},
                                "feishu": {"connectionMode": "webhook"},
                            }
                        },
                        "tools": {"enabled": ["external-side-effect"]},
                    }
                ),
                encoding="utf-8",
            )
            environment = {
                "HERMES_HOME": str(root),
                "FEISHU_APP_ID": "cli_test",
                "FEISHU_APP_SECRET": "secret-test-value",
            }

            with patch.dict(os.environ, environment, clear=True):
                with redirect_stdout(io.StringIO()):
                    configure_gateway.main()

            configured = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            runtime_state = json.loads(
                (root / "gateway_state.json").read_text(encoding="utf-8")
            )

        self.assertEqual(
            set(configured),
            {
                "_config_version",
                "model",
                "plugins",
                "gateway",
                "streaming",
                "display",
                "memory",
                "compression",
                "session_reset",
                "approvals",
            },
        )
        self.assertEqual(
            configured["plugins"],
            {"enabled": ["platforms/feishu"]},
        )
        self.assertEqual(
            set(configured["gateway"]["platforms"]),
            {"feishu"},
        )
        self.assertEqual(
            configured["gateway"]["platforms"]["feishu"],
            {
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
            },
        )
        self.assertEqual(
            configured["streaming"],
            {
                "enabled": True,
                "transport": "edit",
                "edit_interval": 0.2,
                "buffer_threshold": 1,
            },
        )
        self.assertEqual(
            configured["display"],
            {
                "platforms": {
                    "feishu": {
                        "streaming": True,
                        "tool_progress": "off",
                        "show_reasoning": True,
                    }
                }
            },
        )
        self.assertEqual(configured["session_reset"], {"mode": "none"})
        self.assertEqual(
            configured["approvals"],
            {"mode": "manual", "timeout": 120},
        )
        self.assertEqual(runtime_state["gateway_state"], "stopped")
        self.assertEqual(runtime_state["desired_state"], "stopped")
        self.assertEqual(runtime_state["active_agents"], 0)
        self.assertFalse(runtime_state["restart_requested"])

    def test_compose_enables_required_live_gateway_features(self) -> None:
        """The tenant gateway enables reactions and its isolated image fixture."""
        root = Path(__file__).resolve().parents[1]
        compose = yaml.safe_load(
            (root / "compose.validation.yaml").read_text(encoding="utf-8")
        )

        self.assertEqual(
            compose["services"]["gateway"]["environment"]["FEISHU_REACTIONS"],
            "true",
        )
        self.assertEqual(
            compose["services"]["gateway"]["environment"][
                "HERMES_ALLOW_PRIVATE_URLS"
            ],
            "true",
        )


if __name__ == "__main__":
    unittest.main()
