"""Tests for binding Feishu message identity to Hermes sessions."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "hermes_lark" / "__init__.py"


@dataclass(frozen=True)
class _Ticket:
    """Minimal ticket shape consumed by the registration hooks."""

    sender_open_id: str


class TicketBindingTests(unittest.TestCase):
    """Verify Hermes and Feishu user identifiers correlate reliably."""

    def setUp(self) -> None:
        """Load the package hooks with isolated adapter and tool stubs."""
        package_name = f"_ticket_binding_subject_{id(self)}"
        adapter = types.ModuleType(f"{package_name}.adapter")
        adapter.register = lambda _context: None
        self.lifecycle: list[tuple[_Ticket, dict[str, Any]]] = []
        adapter.notify_cardkit_lifecycle = lambda ticket, **payload: (
            self.lifecycle.append((ticket, payload)) or True
        )
        tools = types.ModuleType(f"{package_name}.openclaw_tools")
        tools.ToolTicket = _Ticket
        tools.ticket_from_event = lambda event: event.ticket
        self.bound: list[tuple[str, _Ticket]] = []
        def bind_session_ticket(session_id: str, ticket: _Ticket) -> _Ticket:
            self.bound.append((session_id, ticket))
            return ticket

        tools.bind_session_ticket = bind_session_ticket
        tools.unbind_session_ticket = lambda _session_id: None
        tools.get_tool_ticket = lambda session_id="": None
        tools.evaluate_tool_policy = lambda _tool_name, _ticket: None
        tools.register = lambda _context: None

        sys.modules[f"{package_name}.adapter"] = adapter
        sys.modules[f"{package_name}.openclaw_tools"] = tools
        spec = importlib.util.spec_from_file_location(
            package_name,
            MODULE_PATH,
            submodule_search_locations=[str(MODULE_PATH.parent)],
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[package_name] = module
        spec.loader.exec_module(module)
        self.module = module
        self.module._RECENT_EVENTS.clear()

    def _event(
        self,
        *,
        open_id: str,
        user_id: str,
        union_id: str = "",
        text: str = "hello",
    ) -> Any:
        """Build one event with Feishu's three distinct user identifiers."""
        source = SimpleNamespace(
            platform=SimpleNamespace(value="feishu"),
            user_id=user_id,
            user_id_alt=union_id,
        )
        return SimpleNamespace(
            source=source,
            text=text,
            ticket=_Ticket(sender_open_id=open_id),
        )

    def test_fallback_matches_hermes_primary_user_id(self) -> None:
        """The pre-LLM sender ID can differ from the ticket's open ID."""
        ticket_event = self._event(
            open_id="ou_open",
            user_id="on_tenant",
            union_id="un_union",
        )

        self.module._capture_gateway_event(ticket_event)
        self.module._bind_pre_llm_ticket(
            session_id="session-1",
            sender_id="on_tenant",
            user_message="hello",
            platform="feishu",
        )

        self.assertEqual(self.bound, [("session-1", ticket_event.ticket)])
        self.assertEqual(
            self.lifecycle,
            [
                (
                    ticket_event.ticket,
                    {
                        "kind": "turn_bound",
                        "session_id": "session-1",
                        "turn_id": "",
                        "wait": True,
                    },
                )
            ],
        )

    def test_fallback_matches_union_id_alias(self) -> None:
        """Stable union IDs remain valid correlation aliases."""
        ticket_event = self._event(
            open_id="ou_open",
            user_id="on_tenant",
            union_id="un_union",
        )

        self.module._capture_gateway_event(ticket_event)
        self.module._bind_pre_llm_ticket(
            session_id="session-2",
            sender_id="un_union",
            user_message="hello",
            platform="feishu",
        )

        self.assertEqual(self.bound, [("session-2", ticket_event.ticket)])

    def test_pre_dispatch_binds_same_task_command_ticket_without_bypassing_auth(
        self,
    ) -> None:
        """The command handler gets the event ticket while Hermes keeps dispatch."""
        ticket_event = self._event(
            open_id="ou_open",
            user_id="on_tenant",
            union_id="un_union",
        )

        result = self.module._capture_gateway_event(ticket_event)
        command_module = sys.modules[f"{self.module.__name__}.commands"]

        self.assertIsNone(result)
        self.assertIs(
            command_module.current_gateway_command_ticket(),
            ticket_event.ticket,
        )

    def test_pre_dispatch_rewrites_upstream_command_before_hermes_gate(
        self,
    ) -> None:
        """The upstream spelling becomes Hermes' registered internal key."""
        ticket_event = self._event(
            open_id="ou_open",
            user_id="on_tenant",
            text="/feishu_doctor",
        )

        result = self.module._capture_gateway_event(ticket_event)

        self.assertEqual(
            result,
            {"action": "rewrite", "text": "/feishu-doctor"},
        )

    def test_package_registers_in_channel_commands_with_canonical_names(
        self,
    ) -> None:
        """The package entry point exposes all upstream chat commands."""
        commands: dict[str, Any] = {}
        hooks: dict[str, list[Any]] = {}

        class Context:
            """Minimal plugin context used by the package entry point."""

            def register_command(
                self,
                name: str,
                handler: Any,
                **metadata: Any,
            ) -> None:
                commands[name] = (handler, metadata)

            def register_hook(self, name: str, handler: Any) -> None:
                hooks.setdefault(name, []).append(handler)

            def register_skill(self, **_: Any) -> None:
                return None

        self.module.register(Context())

        self.assertEqual(
            set(commands),
            {
                "feishu",
                "feishu-auth",
                "feishu-diagnose",
                "feishu-doctor",
            },
        )
        self.assertIn(
            self.module._capture_gateway_event,
            hooks["pre_gateway_dispatch"],
        )

    def test_group_tool_policy_blocks_any_hermes_tool(self) -> None:
        """A group allow/deny rule applies beyond the Feishu toolset."""
        ticket = _Ticket(sender_open_id="ou_open")
        self.module.openclaw_tools.get_tool_ticket = lambda session_id="": ticket
        self.module.openclaw_tools.evaluate_tool_policy = (
            lambda tool_name, resolved: "group_deny"
        )

        result = self.module._enforce_tool_policy(
            "terminal",
            session_id="session-3",
        )

        self.assertEqual(result["action"], "block")
        self.assertIn("group_deny", result["message"])

    def test_channel_registration_deny_does_not_block_unrelated_tools(self) -> None:
        """Channel-level deny only suppresses the plugin's Feishu tools."""
        ticket = _Ticket(sender_open_id="ou_open")
        self.module.openclaw_tools.get_tool_ticket = lambda session_id="": ticket
        self.module.openclaw_tools.evaluate_tool_policy = (
            lambda tool_name, resolved: "channel_deny"
        )

        unrelated = self.module._enforce_tool_policy(
            "terminal",
            session_id="session-4",
        )
        feishu = self.module._enforce_tool_policy(
            "feishu_calendar_event",
            session_id="session-4",
        )

        self.assertIsNone(unrelated)
        self.assertEqual(feishu["action"], "block")

    def test_tool_and_terminal_hooks_target_the_exact_bound_turn(self) -> None:
        """Tool lifecycle callbacks stay on the card bound before the LLM call."""
        ticket_event = self._event(
            open_id="ou_open",
            user_id="on_tenant",
        )
        self.module._capture_gateway_event(ticket_event)
        self.module._bind_pre_llm_ticket(
            session_id="session-card",
            turn_id="turn-card",
            sender_id="on_tenant",
            user_message="hello",
            platform="feishu",
        )

        self.module._notify_cardkit_tool_started(
            tool_name="terminal",
            session_id="session-card",
            turn_id="turn-card",
            tool_call_id="call-card",
        )
        self.module._notify_cardkit_tool_completed(
            tool_name="terminal",
            session_id="session-card",
            turn_id="turn-card",
            tool_call_id="call-card",
            status="ok",
        )
        self.module._mark_cardkit_turn_terminal(
            session_id="session-card",
            turn_id="turn-card",
        )

        payloads = [payload for _ticket, payload in self.lifecycle]
        self.assertEqual(
            [payload["kind"] for payload in payloads],
            ["turn_bound", "tool", "tool", "turn_terminal"],
        )
        self.assertTrue(all(payload["wait"] for payload in payloads))
        self.assertEqual(payloads[1]["status"], "running")
        self.assertEqual(payloads[2]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
