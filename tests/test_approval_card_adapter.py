"""Behavioral tests for Feishu approval and update-prompt cards."""

from __future__ import annotations

import asyncio
import json
import sys
import types
import unittest
from collections import OrderedDict
from itertools import count
from types import SimpleNamespace
from typing import Any

from tests.test_ask_user_question_adapter import (
    _FakeCallbackValue,
    _MISSING_MODULE,
    _load_modules,
)


class ApprovalCardAdapterTests(unittest.TestCase):
    """Verify approval buttons reach Hermes' blocking approval resolver."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tools, cls.adapter_module, cls.previous_modules = _load_modules()
        cls.adapter_module.P2CardActionTriggerResponse = _FakeCallbackValue
        cls.adapter_module.CallBackCard = _FakeCallbackValue

    @classmethod
    def tearDownClass(cls) -> None:
        for name, previous in cls.previous_modules.items():
            if previous is _MISSING_MODULE:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    def _adapter(self) -> tuple[Any, list[dict[str, Any]]]:
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter._client = object()
        adapter._approval_counter = count(1)
        adapter._approval_state = {}
        adapter._update_prompt_counter = count(1)
        adapter._update_prompt_state = {}
        adapter._admins = frozenset()
        adapter._allowed_group_users = frozenset()
        adapter._loop = SimpleNamespace(is_closed=lambda: False)
        adapter._allow_group_message = lambda *_args, **_kwargs: True
        adapter._get_cached_sender_name = lambda _open_id: "Test User"
        adapter._interactive_operator_for_send = (
            lambda _chat_id, _metadata: "ou_test_user"
        )
        adapter._format_exec_approval = (
            lambda command, description, _smart_denied: (
                f"```plain_text\n{command}\n```**Reason:** {description}"
            )
        )
        payloads: list[dict[str, Any]] = []

        async def send_with_retry(**kwargs: Any) -> Any:
            payloads.append(json.loads(kwargs["payload"]))
            return object()

        adapter._feishu_send_with_retry = send_with_retry
        adapter._finalize_send_result = lambda *_args: self.adapter_module.SendResult(
            success=True,
            message_id=f"om_approval_{len(payloads)}",
        )
        return adapter, payloads

    def test_buttons_map_to_blocking_gateway_approval_choices(self) -> None:
        adapter, payloads = self._adapter()
        expected = {
            "approve_once": "once",
            "approve_session": "session",
            "approve_always": "always",
            "deny": "deny",
        }
        resolved: list[tuple[str, str]] = []
        scheduled: list[Any] = []

        approval_package = types.ModuleType("tools")
        approval_package.__path__ = []
        approval_module = types.ModuleType("tools.approval")
        approval_module.resolve_gateway_approval = (
            lambda session_key, choice: resolved.append((session_key, choice)) or 1
        )
        previous_tools = sys.modules.get("tools", _MISSING_MODULE)
        previous_approval = sys.modules.get("tools.approval", _MISSING_MODULE)
        sys.modules["tools"] = approval_package
        sys.modules["tools.approval"] = approval_module

        try:
            for action_name, choice in expected.items():
                with self.subTest(action=action_name):
                    asyncio.run(
                        adapter.send_exec_approval(
                            "oc_chat",
                            "rm -rf /tmp/example",
                            f"session-{action_name}",
                            metadata={"thread_id": "om_root"},
                        )
                    )
                    card = payloads[-1]
                    buttons = card["elements"][1]["actions"]
                    button = next(
                        item
                        for item in buttons
                        if item["value"]["hermes_action"] == action_name
                    )
                    approval_id = button["value"]["approval_id"]
                    self.assertEqual(
                        adapter._approval_state[approval_id]["thread_id"],
                        "om_root",
                    )

                    adapter._submit_on_loop = (
                        lambda _loop, coroutine: scheduled.append(coroutine) or True
                    )
                    event = SimpleNamespace(
                        action=SimpleNamespace(value=button["value"]),
                        operator=SimpleNamespace(
                            open_id="ou_test_user",
                            user_id="on_test_user",
                        ),
                        context=SimpleNamespace(
                            open_chat_id="oc_chat",
                            open_message_id=f"om_approval_{len(payloads)}",
                        ),
                    )
                    response = adapter._on_card_action_trigger(
                        SimpleNamespace(event=event)
                    )
                    asyncio.run(scheduled.pop())

                    self.assertEqual(
                        resolved[-1],
                        (f"session-{action_name}", choice),
                    )
                    self.assertNotIn(approval_id, adapter._approval_state)
                    self.assertEqual(
                        response.card.data["header"]["template"],
                        "red" if choice == "deny" else "green",
                    )
        finally:
            if previous_tools is _MISSING_MODULE:
                sys.modules.pop("tools", None)
            else:
                sys.modules["tools"] = previous_tools
            if previous_approval is _MISSING_MODULE:
                sys.modules.pop("tools.approval", None)
            else:
                sys.modules["tools.approval"] = previous_approval

    def test_approval_does_not_treat_user_id_as_open_id(self) -> None:
        """Hermes approvals keep their strict app-scoped operator identity."""
        adapter, payloads = self._adapter()
        asyncio.run(
            adapter.send_exec_approval(
                "oc_chat",
                "rm -rf /tmp/example",
                "session-user-id-only",
                metadata={"thread_id": "om_root"},
            )
        )
        button = payloads[-1]["elements"][1]["actions"][0]
        approval_id = button["value"]["approval_id"]
        adapter._submit_on_loop = lambda *args: self.fail("must not schedule")
        event = SimpleNamespace(
            action=SimpleNamespace(value=button["value"]),
            operator=SimpleNamespace(user_id="u_test_user"),
            context=SimpleNamespace(open_chat_id="oc_chat"),
        )

        response = adapter._on_card_action_trigger(
            SimpleNamespace(event=event)
        )

        self.assertIn(approval_id, adapter._approval_state)
        self.assertIsNone(response.card)

    def test_approval_fails_closed_without_exact_card_route(self) -> None:
        """Approval callbacks must match both the originating chat and card."""
        adapter, payloads = self._adapter()
        asyncio.run(
            adapter.send_exec_approval(
                "oc_chat",
                "rm -rf /tmp/example",
                "session-route-binding",
            )
        )
        button = payloads[-1]["elements"][1]["actions"][0]
        approval_id = button["value"]["approval_id"]
        adapter._submit_on_loop = lambda *args: self.fail("must not schedule")
        cases = (
            ("oc_chat", "om_approval_1", "ou_other"),
            ("", "om_approval_1", "ou_test_user"),
            ("oc_other", "om_approval_1", "ou_test_user"),
            ("oc_chat", "", "ou_test_user"),
            ("oc_chat", "om_other", "ou_test_user"),
        )

        for chat_id, message_id, open_id in cases:
            with self.subTest(
                chat_id=chat_id,
                message_id=message_id,
                open_id=open_id,
            ):
                event = SimpleNamespace(
                    action=SimpleNamespace(value=button["value"]),
                    operator=SimpleNamespace(open_id=open_id),
                    context=SimpleNamespace(
                        open_chat_id=chat_id,
                        open_message_id=message_id,
                    ),
                )
                response = adapter._on_card_action_trigger(
                    SimpleNamespace(event=event)
                )

                self.assertIn(approval_id, adapter._approval_state)
                self.assertIsNone(response.card)

    def test_update_prompt_callback_resolves_only_its_card(self) -> None:
        """Update prompt buttons bind to an identified operator, chat, and card."""
        adapter, payloads = self._adapter()
        responses: list[str] = []
        adapter._write_update_prompt_response = responses.append
        asyncio.run(
            adapter.send_update_prompt(
                "oc_chat",
                "Install the update?",
                session_key="session-update",
            )
        )
        button = payloads[-1]["elements"][1]["actions"][0]
        prompt_id = button["value"]["update_prompt_id"]
        scheduled: list[Any] = []
        adapter._submit_on_loop = (
            lambda _loop, coroutine: scheduled.append(coroutine) or True
        )
        event = SimpleNamespace(
            action=SimpleNamespace(value=button["value"]),
            operator=SimpleNamespace(open_id="ou_test_user"),
            context=SimpleNamespace(
                open_chat_id="oc_chat",
                open_message_id="om_approval_1",
            ),
        )

        response = adapter._on_card_action_trigger(SimpleNamespace(event=event))
        asyncio.run(scheduled.pop())

        self.assertEqual(responses, ["y"])
        self.assertNotIn(prompt_id, adapter._update_prompt_state)
        self.assertEqual(response.card.data["header"]["template"], "green")

    def test_update_prompt_fails_closed_without_operator_or_exact_route(self) -> None:
        """Malformed update callbacks cannot resolve a pending prompt."""
        adapter, payloads = self._adapter()
        asyncio.run(
            adapter.send_update_prompt(
                "oc_chat",
                "Install the update?",
                session_key="session-update",
            )
        )
        button = payloads[-1]["elements"][1]["actions"][0]
        prompt_id = button["value"]["update_prompt_id"]
        adapter._submit_on_loop = lambda *args: self.fail("must not schedule")
        cases = (
            ("", "oc_chat", "om_approval_1"),
            ("ou_other", "oc_chat", "om_approval_1"),
            ("ou_test_user", "", "om_approval_1"),
            ("ou_test_user", "oc_other", "om_approval_1"),
            ("ou_test_user", "oc_chat", ""),
            ("ou_test_user", "oc_chat", "om_other"),
        )

        for open_id, chat_id, message_id in cases:
            with self.subTest(
                open_id=open_id,
                chat_id=chat_id,
                message_id=message_id,
            ):
                event = SimpleNamespace(
                    action=SimpleNamespace(value=button["value"]),
                    operator=SimpleNamespace(open_id=open_id),
                    context=SimpleNamespace(
                        open_chat_id=chat_id,
                        open_message_id=message_id,
                    ),
                )
                response = adapter._on_card_action_trigger(
                    SimpleNamespace(event=event)
                )

                self.assertIn(prompt_id, adapter._update_prompt_state)
                self.assertIsNone(response.card)

    def test_cards_are_not_sent_without_an_identified_initiator(self) -> None:
        """Interactive security prompts fail before creating unowned cards."""
        adapter, payloads = self._adapter()
        adapter._interactive_operator_for_send = lambda *_args: ""

        approval = asyncio.run(
            adapter.send_exec_approval(
                "oc_chat",
                "rm -rf /tmp/example",
                "session-without-owner",
            )
        )

        self.assertFalse(approval.success)
        with self.assertRaisesRegex(RuntimeError, "initiator identity"):
            asyncio.run(
                adapter.send_update_prompt(
                    "oc_chat",
                    "Install the update?",
                    session_key="session-without-owner",
                )
            )
        self.assertEqual(payloads, [])

    def test_initiator_lookup_prefers_the_active_thread_turn(self) -> None:
        """A stale root sender cannot own a later thread turn's prompt."""
        adapter, _payloads = self._adapter()
        del adapter._interactive_operator_for_send
        adapter._account_id = ""
        adapter._namespace_account = False
        adapter._interactive_operators_by_message = OrderedDict(
            {"om_root": "ou_root_sender"}
        )
        adapter._interactive_operators_by_route = OrderedDict(
            {("oc_chat", "om_root"): "ou_current_sender"}
        )

        operator = adapter._interactive_operator_for_send(
            "oc_chat",
            {
                "thread_id": "om_root",
                "reply_to_message_id": "om_root",
            },
        )

        self.assertEqual(operator, "ou_current_sender")


if __name__ == "__main__":
    unittest.main()
