"""Subprocess integration probe for command authorization in Hermes 0.19.1."""

from __future__ import annotations

import os
import unittest
from importlib.metadata import version
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import hermes_lark
from hermes_lark import commands as feishu_commands
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


_COMMAND_NAMES = frozenset(
    {
        "feishu",
        "feishu-auth",
        "feishu-diagnose",
        "feishu-doctor",
    }
)


class HermesCommandAuthorizationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Exercise Feishu commands through Hermes' real gateway dispatcher."""

    @classmethod
    def setUpClass(cls) -> None:
        """Pin the host behavior asserted by this integration contract."""
        host_version = version("hermes-agent")
        if host_version != "0.19.1":
            raise AssertionError(
                f"expected Hermes Agent 0.19.1, found {host_version}"
            )

    def setUp(self) -> None:
        """Create a real, unstarted gateway runner for isolated dispatch."""
        self.runner = GatewayRunner()
        self.auth_env = patch.dict(
            os.environ,
            {
                "FEISHU_ALLOWED_USERS": "",
                "FEISHU_ALLOW_ALL_USERS": "",
                "GATEWAY_ALLOWED_USERS": "",
                "GATEWAY_ALLOW_ALL_USERS": "",
            },
        )
        self.auth_env.start()

    def tearDown(self) -> None:
        """Restore process authorization settings after each dispatch."""
        self.auth_env.stop()

    async def asyncTearDown(self) -> None:
        """Close the real Hermes session database opened by the runner."""
        await self.runner._session_db.close()
        self.runner.session_store._db.close()

    @staticmethod
    def _source(*, chat_type: str, role_authorized: bool) -> SessionSource:
        """Build one Feishu source carrying the adapter's access decision."""
        return SessionSource(
            platform=Platform.FEISHU,
            chat_id="oc_command_auth",
            chat_type=chat_type,
            user_id="ou_command_auth",
            user_id_alt="on_command_auth",
            role_authorized=role_authorized,
        )

    @staticmethod
    def _invoke_plugin_hook(name: str, **kwargs: object) -> list[object]:
        """Run this plugin's pre-dispatch hook through Hermes' hook seam."""
        if name != "pre_gateway_dispatch":
            return []
        result = hermes_lark._capture_gateway_event(**kwargs)
        return [result] if result is not None else []

    async def _dispatch(
        self,
        text: str,
        source: SessionSource,
        handler: AsyncMock,
    ) -> tuple[str | None, list[str]]:
        """Dispatch one command while observing Hermes' plugin lookup."""
        lookups: list[str] = []

        def resolve_handler(name: str) -> AsyncMock | None:
            lookups.append(name)
            return handler if name in _COMMAND_NAMES else None

        event = MessageEvent(
            text=text,
            source=source,
            message_id="om_command_auth",
        )
        with (
            patch(
                "hermes_cli.lifecycle.invoke_hook",
                side_effect=self._invoke_plugin_hook,
            ),
            patch(
                "hermes_cli.commands.is_gateway_known_command",
                side_effect=lambda name: name in _COMMAND_NAMES,
            ),
            patch(
                "hermes_cli.plugins.get_plugin_command_handler",
                side_effect=resolve_handler,
            ),
        ):
            result = await self.runner._handle_message(event)
        return result, lookups

    def test_commands_register_through_the_exact_hermes_plugin_api(self) -> None:
        """No unsupported OpenClaw ``requireAuth`` keyword reaches Hermes."""
        manager = PluginManager()
        context = PluginContext(
            PluginManifest(name="feishu-command-auth-integration"),
            manager,
        )

        feishu_commands.register(context)

        self.assertEqual(set(manager._plugin_commands), _COMMAND_NAMES)

    async def test_pairing_dm_cannot_reach_any_feishu_handler(self) -> None:
        """An unpaired DM stops after the upstream name is rewritten."""

        class PairingStore:
            """Unapproved pairing state used by the real Hermes auth gate."""

            profile = "default"

            @staticmethod
            def is_approved(_platform: str, _user_id: str) -> bool:
                return False

            @staticmethod
            def _is_rate_limited(_platform: str, _user_id: str) -> bool:
                return False

            @staticmethod
            def generate_code(_platform: str, _user_id: str, _name: str) -> str:
                return "PAIR-CODE"

        handler = AsyncMock(return_value="handler reached")
        adapter = SimpleNamespace(send=AsyncMock())
        pairing_store = PairingStore()
        with (
            patch.object(
                self.runner,
                "_pairing_store_for",
                return_value=pairing_store,
            ),
            patch.object(
                self.runner,
                "_get_unauthorized_dm_behavior",
                return_value="pair",
            ),
            patch.object(
                self.runner,
                "_adapter_for_source",
                return_value=adapter,
            ),
            patch.object(
                self.runner,
                "_is_user_authorized",
                wraps=self.runner._is_user_authorized,
            ) as authorize,
        ):
            result, lookups = await self._dispatch(
                "/feishu_auth",
                self._source(chat_type="dm", role_authorized=False),
                handler,
            )

        self.assertIsNone(result)
        authorize.assert_called()
        handler.assert_not_awaited()
        self.assertEqual(lookups, [])
        adapter.send.assert_awaited_once()

    async def test_group_policy_denial_cannot_reach_any_feishu_handler(self) -> None:
        """A denied group sender is blocked by both adapter and host policy."""
        settings = hermes_lark.adapter.FeishuAdapter._load_settings(
            {
                "appId": "cli_test",
                "appSecret": "secret",
                "groupPolicy": "allowlist",
                "groupAllowFrom": ["ou_allowed"],
            }
        )
        adapter = object.__new__(hermes_lark.adapter.FeishuAdapter)
        adapter._apply_settings(settings)
        sender = SimpleNamespace(
            sender_type="user",
            sender_id=SimpleNamespace(
                open_id="ou_command_auth",
                user_id="u_command_auth",
                union_id="on_command_auth",
            ),
        )
        message = SimpleNamespace(
            chat_id="oc_command_auth",
            chat_type="group",
            mentions=[],
        )

        self.assertEqual(adapter._admit(sender, message), "group_policy_rejected")

        handler = AsyncMock(return_value="handler reached")
        with patch.object(
            self.runner,
            "_is_user_authorized",
            wraps=self.runner._is_user_authorized,
        ) as authorize:
            result, lookups = await self._dispatch(
                "/feishu_doctor",
                self._source(chat_type="group", role_authorized=False),
                handler,
            )

        self.assertIsNone(result)
        authorize.assert_called()
        handler.assert_not_awaited()
        self.assertEqual(lookups, [])

    async def test_authenticated_callers_reach_upstream_names_and_host_keys(self) -> None:
        """The ordinary channel grant admits upstream names and host keys."""
        handler = AsyncMock(return_value="handler reached")
        commands = (
            "/feishu help",
            "/feishu-auth",
            "/feishu-diagnose",
            "/feishu-doctor",
            "/feishu_auth",
            "/feishu_diagnose",
            "/feishu_doctor",
        )
        observed_lookups: list[str] = []

        with patch.object(
            self.runner,
            "_is_user_authorized",
            wraps=self.runner._is_user_authorized,
        ) as authorize:
            for index, command in enumerate(commands):
                with self.subTest(command=command):
                    chat_type = "dm" if index % 2 == 0 else "group"
                    result, lookups = await self._dispatch(
                        command,
                        self._source(
                            chat_type=chat_type,
                            role_authorized=True,
                        ),
                        handler,
                    )
                    self.assertEqual(result, "handler reached")
                    observed_lookups.extend(lookups)

        self.assertEqual(handler.await_count, len(commands))
        self.assertGreaterEqual(authorize.call_count, len(commands))
        self.assertEqual(
            observed_lookups,
            [
                "feishu",
                "feishu-auth",
                "feishu-diagnose",
                "feishu-doctor",
                "feishu-auth",
                "feishu-diagnose",
                "feishu-doctor",
            ],
        )


if __name__ == "__main__":
    unittest.main()
