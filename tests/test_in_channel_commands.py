"""Focused tests for OpenClaw-compatible Feishu in-channel commands."""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.test_ask_user_question_adapter import (
    PACKAGE_DIR,
    _MISSING_MODULE,
    _load_modules,
    _load_package_module,
)


class FeishuInChannelCommandTests(unittest.TestCase):
    """Verify routing, identity binding, host delivery, and diagnostics."""

    @classmethod
    def setUpClass(cls) -> None:
        """Load the real command and tool modules against offline Hermes stubs."""
        cls.previous_commands = sys.modules.get(
            "hermes_lark.commands",
            _MISSING_MODULE,
        )
        cls.tools, cls.adapter, cls.previous_modules = _load_modules()
        sys.modules.pop("hermes_lark.commands", None)
        cls.commands = _load_package_module(
            "hermes_lark.commands",
            PACKAGE_DIR / "commands.py",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        """Restore modules replaced by the offline loader."""
        if cls.previous_commands is _MISSING_MODULE:
            sys.modules.pop("hermes_lark.commands", None)
        else:
            sys.modules["hermes_lark.commands"] = cls.previous_commands
        for name, previous in cls.previous_modules.items():
            if previous is _MISSING_MODULE:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    def setUp(self) -> None:
        """Install two deterministic account definitions and clear host state."""
        with self.tools._state_lock:
            self.tools._pending_interactions.clear()
            self.tools._interaction_hosts.clear()
        self.tools.configure_bridge_config(
            {
                "channels": {
                    "feishu": {
                        "accounts": {
                            "work": {
                                "enabled": True,
                                "appId": "cli_work",
                                "appSecret": "secret_work",
                                "domain": "feishu",
                            },
                            "archive": {
                                "enabled": False,
                                "appId": "cli_archive",
                                "appSecret": "secret_archive",
                                "domain": "lark",
                            },
                        }
                    }
                }
            }
        )
        self.commands.bind_gateway_command_ticket(None)

    def tearDown(self) -> None:
        """Release command continuations and configuration after each test."""
        with self.tools._state_lock:
            self.tools._pending_interactions.clear()
            self.tools._interaction_hosts.clear()
        self.tools.configure_bridge_config(None)
        self.commands.bind_gateway_command_ticket(None)

    def _ticket(
        self,
        *,
        account_id: str = "work",
        chat_id: str = "oc_work",
        sender_open_id: str = "ou_work",
    ) -> object:
        """Build one complete inbound command ticket."""
        return self.tools.ToolTicket(
            session_id="session-work",
            message_id=f"om_{account_id}",
            chat_id=chat_id,
            account_id=account_id,
            sender_open_id=sender_open_id,
            chat_type="p2p",
        )

    def test_commands_load_account_local_hermes_extras(self) -> None:
        """Command diagnostics see normalized accounts without global env bleed."""
        self.tools.configure_bridge_config(
            {
                "channels": {
                    "feishu": {
                        "extra": {
                            "_accounts_only": True,
                            "domain": "feishu",
                            "accounts": {
                                "work": {
                                    "extra": {
                                        "app_id": "cli_work_extra",
                                        "app_secret": "secret_work_extra",
                                        "domain": "lark",
                                    }
                                }
                            },
                        }
                    }
                }
            }
        )
        stale_env = {
            "FEISHU_APP_ID": "cli_stale",
            "FEISHU_APP_SECRET": "secret_stale",
            "FEISHU_DOMAIN": "stale.example",
        }

        with patch.dict(os.environ, stale_env, clear=True):
            accounts = self.commands._load_accounts()

        self.assertEqual(set(accounts), {"work"})
        self.assertEqual(accounts["work"].app_id, "cli_work_extra")
        self.assertEqual(accounts["work"].app_secret, "secret_work_extra")
        self.assertEqual(accounts["work"].brand, "lark")

    def test_registers_internal_hyphen_keys_for_upstream_underscore_commands(
        self,
    ) -> None:
        """Hermes resolves every upstream underscore command through its key."""
        registered: dict[str, object] = {}
        metadata: dict[str, dict[str, object]] = {}

        class Context:
            """Capture register_command calls."""

            def register_command(
                self,
                name: str,
                handler: object,
                **details: object,
            ) -> None:
                registered[name] = handler
                metadata[name] = details

        self.commands.register(Context())

        self.assertEqual(
            set(registered),
            {
                "feishu",
                "feishu-auth",
                "feishu-diagnose",
                "feishu-doctor",
            },
        )
        for typed in (
            "feishu_auth",
            "feishu_diagnose",
            "feishu_doctor",
        ):
            self.assertIn(typed.replace("_", "-"), registered)
        self.assertEqual(
            metadata["feishu"]["args_hint"],
            "[auth|doctor|start|help]",
        )

    def test_unified_command_routes_subcommands_and_falls_back_to_help(
        self,
    ) -> None:
        """The /feishu entry point covers aliases and upstream help behavior."""

        async def exercise() -> None:
            with (
                patch.object(
                    self.commands,
                    "_handle_auth",
                    new=AsyncMock(return_value="auth"),
                ) as auth,
                patch.object(
                    self.commands,
                    "_handle_doctor",
                    new=AsyncMock(return_value="doctor"),
                ) as doctor,
                patch.object(
                    self.commands,
                    "_handle_diagnose",
                    new=AsyncMock(return_value="diagnose"),
                ) as diagnose,
                patch.object(
                    self.commands,
                    "_handle_start",
                    new=AsyncMock(return_value="start"),
                ) as start,
            ):
                self.assertEqual(
                    await self.commands._handle_feishu("auth"),
                    "auth",
                )
                self.assertEqual(
                    await self.commands._handle_feishu("onboarding"),
                    "auth",
                )
                self.assertEqual(
                    await self.commands._handle_feishu("doctor"),
                    "doctor",
                )
                self.assertEqual(
                    await self.commands._handle_feishu("start"),
                    "start",
                )
                self.assertIn(
                    "/feishu auth",
                    await self.commands._handle_feishu("unknown"),
                )
                self.assertIn(
                    "/feishu help",
                    await self.commands._handle_feishu("diagnose"),
                )
                self.assertIn(
                    "/feishu help",
                    await self.commands._handle_feishu(""),
                )
                self.assertEqual(auth.await_count, 2)
                doctor.assert_awaited_once()
                diagnose.assert_not_awaited()
                start.assert_awaited_once()

        asyncio.run(exercise())

    def test_auth_uses_live_host_with_authoritative_account_chat_and_sender(
        self,
    ) -> None:
        """Batch OAuth reaches only the host selected by the inbound ticket."""
        calls: list[object] = []
        ticket = self._ticket()
        self.tools.register_interaction_host(
            "work",
            lambda interaction: calls.append(interaction) is None,
        )
        self.commands.bind_gateway_command_ticket(ticket)

        result = asyncio.run(self.commands._handle_auth(""))

        self.assertIn("not complete", result)
        self.assertEqual(len(calls), 1)
        interaction = calls[0]
        self.assertEqual(interaction.kind, "oauth_batch_auth")
        self.assertEqual(interaction.ticket.account_id, "work")
        self.assertEqual(interaction.ticket.chat_id, "oc_work")
        self.assertEqual(interaction.ticket.sender_open_id, "ou_work")
        self.assertEqual(interaction.context["oauth_intent"], "standalone")

    def test_auth_routes_sources_from_hosts_without_transport_provenance(
        self,
    ) -> None:
        """The Feishu adapter supplies provenance missing from older hosts."""
        adapter = object.__new__(self.adapter.FeishuAdapter)
        adapter.platform = self.adapter.Platform.FEISHU
        adapter._account_id = "work"
        adapter._namespace_account = False
        adapter._profile_scope_key = "/hermes/profiles/coder"

        source = adapter.build_source(
            "oc_work",
            chat_type="dm",
            user_id="ou_work",
            scope_id="work",
        )
        ticket = self.tools.ticket_from_event(
            SimpleNamespace(
                source=source,
                message_id="om_work",
                raw_message=SimpleNamespace(
                    sender=SimpleNamespace(
                        sender_id=SimpleNamespace(open_id="ou_work")
                    )
                ),
            ),
            "session-work",
        )
        calls: list[object] = []
        self.tools.register_interaction_host(
            "work",
            lambda interaction: calls.append(interaction) is None,
            profile_scope="/hermes/profiles/coder",
        )
        self.commands.bind_gateway_command_ticket(ticket)

        result = asyncio.run(self.commands._handle_auth(""))

        self.assertIs(source._transport_adapter_ref(), adapter)
        self.assertEqual(ticket.profile_scope, "/hermes/profiles/coder")
        self.assertIn("not complete", result)
        self.assertEqual(len(calls), 1)

    def test_auth_without_live_host_reports_failure_not_success(self) -> None:
        """An unavailable interaction host cannot be described as authorized."""
        self.commands.bind_gateway_command_ticket(self._ticket())

        result = asyncio.run(self.commands._handle_auth(""))

        self.assertIn("did not start", result)
        self.assertIn("interaction_host_unavailable", result)
        self.assertNotIn("Authorization complete", result)

    def test_start_distinguishes_connected_and_loaded_only_states(self) -> None:
        """Startup output is based on a live account host, not config alone."""
        self.commands.bind_gateway_command_ticket(self._ticket())

        disconnected = asyncio.run(self.commands._handle_start(""))
        self.assertIn("not connected", disconnected)
        self.assertNotIn("✅", disconnected)

        self.tools.register_interaction_host("work", lambda _: True)
        connected = asyncio.run(self.commands._handle_start(""))
        self.assertIn("✅", connected)
        self.assertIn("work", connected)

    def test_doctor_reports_live_application_owner_and_oauth_checks(self) -> None:
        """Doctor reports actual check inputs for the current account only."""
        self.commands.bind_gateway_command_ticket(self._ticket())
        self.tools.register_interaction_host("work", lambda _: True)
        application = SimpleNamespace(
            effective_owner_open_id="ou_work",
            user_scopes=("calendar:calendar",),
        )

        async def exercise() -> str:
            with (
                patch.object(
                    self.commands,
                    "_probe_account",
                    new=AsyncMock(
                        return_value={
                            "bot_name": "Work Bot",
                            "bot_open_id": "ou_bot",
                        }
                    ),
                ),
                patch.object(
                    self.commands,
                    "_inspect_application",
                    new=AsyncMock(
                        return_value=(
                            application,
                            frozenset(
                                {
                                    "calendar:calendar",
                                    "offline_access",
                                }
                            ),
                        )
                    ),
                ),
                patch.object(
                    self.commands,
                    "_oauth_status",
                    new=AsyncMock(
                        return_value=("pass", "Valid with 1 recorded scope")
                    ),
                ),
            ):
                return await self.commands._handle_doctor("")

        result = asyncio.run(exercise())

        self.assertIn("Work Bot", result)
        self.assertIn("current user verified", result)
        self.assertIn("2 total", result)
        self.assertIn("HEALTHY", result)
        self.assertNotIn("archive", result)

    def test_diagnose_marks_failed_probe_unhealthy(self) -> None:
        """Diagnosis cannot report healthy when a configured API probe fails."""
        self.commands.bind_gateway_command_ticket(self._ticket())

        async def exercise() -> tuple[str, int]:
            with patch.object(
                self.commands,
                "_probe_account",
                new=AsyncMock(return_value=None),
            ) as probe:
                result = await self.commands._handle_diagnose("")
                return result, probe.await_count

        result, probe_count = asyncio.run(exercise())

        self.assertEqual(probe_count, 1)
        self.assertIn("[Account: work]", result)
        self.assertIn("[Account: archive]", result)
        self.assertIn("[FAIL] API connectivity", result)
        self.assertIn("UNHEALTHY", result)

    def test_concurrent_commands_keep_account_and_identity_isolated(self) -> None:
        """ContextVar tickets do not cross two concurrent gateway tasks."""
        tickets = (
            self._ticket(
                account_id="work",
                chat_id="oc_a",
                sender_open_id="ou_a",
            ),
            self._ticket(
                account_id="archive",
                chat_id="oc_b",
                sender_open_id="ou_b",
            ),
        )
        observed: list[tuple[str, str, str]] = []

        def invoke(
            _tool_name: str,
            _params: object,
            *,
            ticket: object,
            oauth_intent: str,
        ) -> str:
            self.assertEqual(oauth_intent, "standalone")
            observed.append(
                (
                    ticket.account_id,
                    ticket.chat_id,
                    ticket.sender_open_id,
                )
            )
            return (
                '{"ok": false, "status": "pending", '
                '"error": "authorization_pending"}'
            )

        async def exercise() -> list[str]:
            ready = (asyncio.Event(), asyncio.Event())
            release = asyncio.Event()

            async def worker(index: int) -> str:
                self.commands.bind_gateway_command_ticket(tickets[index])
                ready[index].set()
                await ready[1 - index].wait()
                await release.wait()
                return await self.commands._handle_auth("")

            tasks = [
                asyncio.create_task(worker(0)),
                asyncio.create_task(worker(1)),
            ]
            await ready[0].wait()
            await ready[1].wait()
            release.set()
            return await asyncio.gather(*tasks)

        with patch.object(
            self.commands.openclaw_tools,
            "invoke_openclaw_tool",
            side_effect=invoke,
        ):
            results = asyncio.run(exercise())

        self.assertTrue(all("not complete" in result for result in results))
        self.assertCountEqual(
            observed,
            [
                ("work", "oc_a", "ou_a"),
                ("archive", "oc_b", "ou_b"),
            ],
        )

    def test_non_feishu_cli_context_cannot_start_identity_bound_commands(
        self,
    ) -> None:
        """Direct CLI invocation has no authoritative Feishu sender ticket."""
        with patch.dict(os.environ, {}, clear=True):
            result = asyncio.run(self.commands._handle_auth(""))

        self.assertIn("Unable to resolve the current Feishu message identity", result)


if __name__ == "__main__":
    unittest.main()
