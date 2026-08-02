"""Focused authorization tests for Feishu synthetic inbound events."""

from __future__ import annotations

import asyncio
import importlib
import sys
import threading
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from tests.test_ask_user_question_adapter import _MISSING_MODULE, _load_modules


class SyntheticEventAuthorizationTests(unittest.TestCase):
    """Verify cards and meeting invites cannot bypass account policy."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tools, cls.adapter_module, cls.previous_modules = _load_modules()
        cls.previous_meeting_module = sys.modules.pop(
            "hermes_lark.feishu_meeting_invite",
            _MISSING_MODULE,
        )
        cls.meeting_module = importlib.import_module(
            "hermes_lark.feishu_meeting_invite"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        sys.modules.pop("hermes_lark.feishu_meeting_invite", None)
        if cls.previous_meeting_module is not _MISSING_MODULE:
            sys.modules[
                "hermes_lark.feishu_meeting_invite"
            ] = cls.previous_meeting_module
        for name, previous in cls.previous_modules.items():
            if previous is _MISSING_MODULE:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    def setUp(self) -> None:
        with self.tools._state_lock:
            self.tools._pending_interactions.clear()

    def _adapter(
        self,
        *,
        dm_policy: str = "pairing",
        allow_from: list[str] | None = None,
        group_policy: str = "open",
        group_allow_from: list[str] | None = None,
        groups: dict[str, Any] | None = None,
        account_id: str = "work",
    ) -> Any:
        settings = self.adapter_module.FeishuAdapter._load_settings(
            {
                "appId": "cli_test",
                "appSecret": "secret",
                "dmPolicy": dm_policy,
                "allowFrom": allow_from or [],
                "groupPolicy": group_policy,
                "groupAllowFrom": group_allow_from or [],
                "groups": groups or {},
                "requireMention": True,
            }
        )
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter._apply_settings(settings)
        adapter.platform = self.adapter_module.Platform.FEISHU
        adapter._account_id = account_id
        adapter._namespace_account = bool(account_id)
        adapter._bot_open_id = "ou_bot"
        adapter._bot_user_id = "u_bot"
        adapter._bot_name = "Hermes"
        adapter._card_action_tokens = {}
        get_message = object()
        adapter._client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(
                    message=SimpleNamespace(get=get_message),
                )
            )
        )
        adapter._build_get_message_request = (
            lambda message_id: SimpleNamespace(message_id=message_id)
        )

        async def run_blocking(method: Any, request: Any) -> Any:
            self.assertIs(method, get_message)
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(
                    items=[
                        SimpleNamespace(
                            message_id=request.message_id,
                            chat_id="oc_chat",
                            root_id=None,
                            thread_id=None,
                        )
                    ]
                ),
            )

        adapter._run_blocking = run_blocking
        adapter._is_duplicate = lambda _key: False
        adapter._resolve_channel_prompt = lambda *_args: None
        adapter._resolve_sender_profile = AsyncMock(
            return_value={
                "user_id": "u_user",
                "user_name": "Alice",
                "user_id_alt": "on_user",
            }
        )
        adapter._handle_message_with_guards = AsyncMock()
        adapter._openclaw_submitted_lock = threading.Lock()
        adapter._openclaw_submitted_tokens = set()
        adapter._openclaw_interaction_messages = {}
        return adapter

    def _store_interaction(
        self,
        kind: str,
        *,
        chat_type: str = "p2p",
        sender_user_id: str = "",
        sender_union_id: str = "",
    ) -> Any:
        ticket = self.tools.ToolTicket(
            session_id=f"session-{kind}",
            message_id=f"om_{kind}",
            chat_id="oc_chat",
            account_id="work",
            sender_open_id="ou_user",
            sender_user_id=sender_user_id,
            sender_union_id=sender_union_id,
            chat_type=chat_type,
            session_thread_id=f"om_{kind}",
        )
        return self.tools._store_interaction(
            kind,
            "feishu_calendar_event",
            {
                "questions": [
                    {
                        "question": "Continue?",
                        "header": "Confirm",
                        "options": [],
                        "multiSelect": False,
                    }
                ]
            },
            ticket,
            300,
        )

    @staticmethod
    def _card_data(
        *,
        token: str,
        chat_id: str = "oc_chat",
        open_id: str = "ou_user",
    ) -> Any:
        return SimpleNamespace(
            event=SimpleNamespace(
                token=token,
                context=SimpleNamespace(
                    open_chat_id=chat_id,
                    open_message_id=f"om_{token}",
                ),
                operator=SimpleNamespace(
                    open_id=open_id,
                    user_id="u_user",
                    union_id="on_user",
                ),
                action=SimpleNamespace(
                    tag="button",
                    value={"action": "continue"},
                ),
            )
        )

    @staticmethod
    def _meeting_data(
        open_id: str = "ou_user",
        *,
        call_id: str = "call-123",
        bot_open_id: str = "ou_bot",
    ) -> dict[str, Any]:
        return {
            "header": {"event_id": "evt_meeting"},
            "event": {
                "meeting": {
                    "id": "meeting_id",
                    "meeting_no": "123456789",
                    "topic": "Review",
                },
                "inviter": {
                    "id": {
                        "open_id": open_id,
                        "user_id": "u_user",
                        "union_id": "on_user",
                    },
                    "user_name": "Alice",
                },
                "invite_time": "123",
                "call_id": call_id,
                "bot": {"id": {"open_id": bot_open_id}},
            },
        }

    def test_group_card_action_obeys_disabled_and_allowlist_policy(self) -> None:
        disabled = self._adapter(group_policy="disabled")
        disabled.get_chat_info = AsyncMock(
            return_value={"name": "Group", "type": "group", "raw_type": "group"}
        )
        allowlist = self._adapter(
            group_policy="allowlist",
            group_allow_from=["ou_allowed"],
        )
        allowlist.get_chat_info = AsyncMock(
            return_value={"name": "Group", "type": "group", "raw_type": "group"}
        )

        asyncio.run(
            disabled._handle_card_action_event(
                self._card_data(token="disabled")
            )
        )
        asyncio.run(
            allowlist._handle_card_action_event(
                self._card_data(token="denied")
            )
        )

        disabled._handle_message_with_guards.assert_not_awaited()
        allowlist._handle_message_with_guards.assert_not_awaited()
        disabled._resolve_sender_profile.assert_not_awaited()
        allowlist._resolve_sender_profile.assert_not_awaited()

    def test_authorized_group_card_action_carries_adapter_grant(self) -> None:
        adapter = self._adapter(
            group_policy="allowlist",
            group_allow_from=["OU_USER"],
        )
        adapter.get_chat_info = AsyncMock(
            return_value={"name": "Group", "type": "group", "raw_type": "group"}
        )

        asyncio.run(
            adapter._handle_card_action_event(
                self._card_data(token="group-allowed")
            )
        )

        event = adapter._handle_message_with_guards.await_args.args[0]
        self.assertEqual(event.source.chat_type, "group")
        self.assertEqual(event.source.thread_id, "om_group-allowed")
        self.assertEqual(event.reply_to_message_id, "om_group-allowed")
        self.assertTrue(event.source.role_authorized)

    def test_card_action_recovers_native_thread_root_from_card_message(
        self,
    ) -> None:
        adapter = self._adapter(group_policy="open")
        adapter.get_chat_info = AsyncMock(
            return_value={"name": "Group", "type": "group", "raw_type": "group"}
        )

        async def resolve_card(_method: Any, _request: Any) -> Any:
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(
                    items=[
                        SimpleNamespace(
                            chat_id="oc_chat",
                            root_id="om_root",
                            thread_id="omt_native",
                        )
                    ]
                ),
            )

        adapter._run_blocking = resolve_card

        asyncio.run(
            adapter._handle_card_action_event(
                self._card_data(token="thread-card")
            )
        )

        event = adapter._handle_message_with_guards.await_args.args[0]
        self.assertEqual(event.source.thread_id, "om_root")
        self.assertEqual(event.source.feishu_thread_id, "omt_native")
        self.assertEqual(event.reply_to_message_id, "om_thread-card")

    def test_card_action_drops_when_chat_type_lookup_is_not_authoritative(
        self,
    ) -> None:
        adapter = self._adapter(
            dm_policy="open",
            group_policy="disabled",
        )
        adapter.get_chat_info = AsyncMock(
            return_value={"name": "oc_chat", "type": "dm"}
        )

        asyncio.run(
            adapter._handle_card_action_event(
                self._card_data(token="unknown-chat-type")
            )
        )

        adapter._handle_message_with_guards.assert_not_awaited()
        adapter._resolve_sender_profile.assert_not_awaited()

    def test_dm_card_action_obeys_disabled_and_allowlist_policy(self) -> None:
        disabled = self._adapter(dm_policy="disabled")
        disabled.get_chat_info = AsyncMock(
            return_value={"name": "DM", "type": "dm", "raw_type": "p2p"}
        )
        allowlist = self._adapter(
            dm_policy="allowlist",
            allow_from=["ou_allowed"],
        )
        allowlist.get_chat_info = AsyncMock(
            return_value={"name": "DM", "type": "dm", "raw_type": "p2p"}
        )

        asyncio.run(
            disabled._handle_card_action_event(
                self._card_data(token="dm-disabled")
            )
        )
        asyncio.run(
            allowlist._handle_card_action_event(
                self._card_data(token="dm-denied")
            )
        )

        disabled._handle_message_with_guards.assert_not_awaited()
        allowlist._handle_message_with_guards.assert_not_awaited()

    def test_pairing_card_action_reaches_hermes_without_role_grant(self) -> None:
        adapter = self._adapter(dm_policy="pairing", account_id="account-a")
        adapter.get_chat_info = AsyncMock(
            return_value={"name": "DM", "type": "dm", "raw_type": "p2p"}
        )

        asyncio.run(
            adapter._handle_card_action_event(
                self._card_data(token="dm-pairing")
            )
        )

        event = adapter._handle_message_with_guards.await_args.args[0]
        self.assertEqual(event.source.chat_type, "dm")
        self.assertEqual(event.source.thread_id, "om_dm-pairing")
        self.assertEqual(event.source.user_id, "account-a::u_user")
        self.assertFalse(event.source.role_authorized)

    def test_allowlisted_card_action_carries_adapter_grant(self) -> None:
        adapter = self._adapter(
            dm_policy="allowlist",
            allow_from=["OU_USER"],
        )
        adapter.get_chat_info = AsyncMock(
            return_value={"name": "DM", "type": "dm", "raw_type": "p2p"}
        )

        asyncio.run(
            adapter._handle_card_action_event(
                self._card_data(token="dm-allowed")
            )
        )

        event = adapter._handle_message_with_guards.await_args.args[0]
        self.assertTrue(event.source.role_authorized)

    def test_meeting_invite_obeys_disabled_and_allowlist_policy(self) -> None:
        disabled = self._adapter(dm_policy="disabled")
        allowlist = self._adapter(
            dm_policy="allowlist",
            allow_from=["ou_allowed"],
        )

        asyncio.run(
            self.meeting_module.handle_meeting_invited_event(
                disabled,
                self._meeting_data(),
            )
        )
        asyncio.run(
            self.meeting_module.handle_meeting_invited_event(
                allowlist,
                self._meeting_data(),
            )
        )

        disabled._handle_message_with_guards.assert_not_awaited()
        allowlist._handle_message_with_guards.assert_not_awaited()
        disabled._resolve_sender_profile.assert_not_awaited()
        allowlist._resolve_sender_profile.assert_not_awaited()

    def test_pairing_meeting_invite_keeps_account_scoped_gateway_gate(self) -> None:
        adapter = self._adapter(dm_policy="pairing", account_id="account-b")

        asyncio.run(
            self.meeting_module.handle_meeting_invited_event(
                adapter,
                self._meeting_data(),
            )
        )

        event = adapter._handle_message_with_guards.await_args.args[0]
        self.assertEqual(event.source.user_id, "account-b::u_user")
        self.assertFalse(event.source.role_authorized)

    def test_allowlisted_meeting_invite_carries_adapter_grant(self) -> None:
        adapter = self._adapter(
            dm_policy="allowlist",
            allow_from=["OU_USER"],
        )

        asyncio.run(
            self.meeting_module.handle_meeting_invited_event(
                adapter,
                self._meeting_data(),
            )
        )

        event = adapter._handle_message_with_guards.await_args.args[0]
        self.assertTrue(event.source.role_authorized)
        self.assertEqual(event.source.chat_id, "work::synthetic:vc-invited")
        self.assertEqual(event.source.chat_id_alt, "ou_user")
        self.assertEqual(event.source.thread_id, "vc-invited:event:evt_meeting")
        self.assertEqual(event.message_id, "vc-invited:event:evt_meeting")
        self.assertIn('call_id="call-123"', event.text)
        self.assertEqual(event.metadata["vc_call_id"], "call-123")
        self.assertEqual(
            adapter._synthetic_vc_targets,
            {"vc-invited:event:evt_meeting": "ou_user"},
        )

    def test_meeting_invite_for_another_bot_is_rejected(self) -> None:
        adapter = self._adapter(dm_policy="open")

        asyncio.run(
            self.meeting_module.handle_meeting_invited_event(
                adapter,
                self._meeting_data(bot_open_id="ou_other_bot"),
            )
        )

        adapter._handle_message_with_guards.assert_not_awaited()
        adapter._resolve_sender_profile.assert_not_awaited()

    def test_synthetic_meeting_output_drops_previews_and_sends_one_final_dm(
        self,
    ) -> None:
        adapter = self._adapter(dm_policy="open")
        route_id = "vc-invited:event:evt_meeting"
        adapter._register_synthetic_vc_target(route_id, "ou_user")
        adapter.send = AsyncMock(
            return_value=self.adapter_module.SendResult(
                success=True,
                message_id="om_final",
            )
        )

        async def scenario() -> None:
            preview = await adapter._deliver_synthetic_vc_output(
                "Joining...",
                reply_to=route_id,
                metadata={"thread_id": route_id, "expect_edits": True},
            )
            final = await adapter._deliver_synthetic_vc_output(
                "Joined successfully.",
                reply_to=route_id,
                metadata={"thread_id": route_id, "notify": True},
            )
            repeated = await adapter._deliver_synthetic_vc_output(
                "Repeated result",
                reply_to=route_id,
                metadata={"thread_id": route_id, "notify": True},
            )
            self.assertTrue(preview.success)
            self.assertEqual(preview.message_id, "")
            self.assertTrue(final.success)
            self.assertEqual(final.message_id, "om_final")
            self.assertTrue(repeated.success)
            self.assertEqual(repeated.message_id, "")

        asyncio.run(scenario())

        adapter.send.assert_awaited_once_with(
            "ou_user",
            "Joined successfully.",
            metadata={"synthetic_vc_final": True},
        )
        self.assertEqual(adapter._synthetic_vc_targets, {})

    def test_oauth_continuation_rechecks_policy_and_preserves_open_id(
        self,
    ) -> None:
        denied_interaction = self._store_interaction("oauth")
        denied = self._adapter(dm_policy="disabled")
        denied.get_chat_info = AsyncMock(
            return_value={"name": "DM", "type": "dm", "raw_type": "p2p"}
        )

        denied_result = asyncio.run(
            denied._resume_and_inject_openclaw_continuation(
                denied_interaction.token,
                text="authorized",
                message_suffix="auth-complete",
                payload={"authorized": True},
            )
        )

        self.assertFalse(denied_result)
        denied._handle_message_with_guards.assert_not_awaited()

        allowed_interaction = self._store_interaction(
            "oauth",
            sender_user_id="u_tenant",
            sender_union_id="on_union",
        )
        allowed = self._adapter(dm_policy="pairing")
        allowed._resolve_sender_profile = AsyncMock(
            side_effect=lambda sender_id: {
                "user_id": sender_id.user_id or sender_id.open_id,
                "user_name": "Alice",
                "user_id_alt": sender_id.union_id,
            }
        )
        allowed.get_chat_info = AsyncMock(
            return_value={"name": "DM", "type": "dm", "raw_type": "p2p"}
        )

        allowed_result = asyncio.run(
            allowed._resume_and_inject_openclaw_continuation(
                allowed_interaction.token,
                text="authorized",
                message_suffix="auth-complete",
                payload={"authorized": True},
            )
        )

        self.assertTrue(allowed_result)
        event = allowed._handle_message_with_guards.await_args.args[0]
        self.assertEqual(event.source.user_id, "work::u_tenant")
        self.assertEqual(event.source.user_id_alt, "on_union")
        self.assertEqual(event.source.thread_id, "om_oauth")
        self.assertFalse(event.source.role_authorized)
        self.assertEqual(
            self.tools.ticket_from_event(event).sender_open_id,
            "ou_user",
        )

    def test_ask_user_answer_rechecks_group_policy_and_sets_grant(self) -> None:
        denied_interaction = self._store_interaction(
            "ask_user_question",
            chat_type="group",
        )
        denied_pending = self.tools.get_pending_interaction(
            denied_interaction.token
        )
        self.assertIsNotNone(denied_pending)
        denied = self._adapter(group_policy="disabled")
        denied.get_chat_info = AsyncMock(
            return_value={
                "name": "Group",
                "type": "group",
                "raw_type": "group",
            }
        )
        denied._update_openclaw_question_card = AsyncMock(return_value=True)

        asyncio.run(
            denied._dispatch_ask_user_answer(
                question_id=denied_interaction.token,
                pending=denied_pending,
                answers={"Continue?": "yes"},
                callback_event=SimpleNamespace(
                    operator=SimpleNamespace(open_id="ou_user")
                ),
            )
        )

        denied._handle_message_with_guards.assert_not_awaited()
        self.assertIsNotNone(
            self.tools.get_pending_interaction(denied_interaction.token)
        )

        allowed_interaction = self._store_interaction(
            "ask_user_question",
            chat_type="group",
        )
        allowed_pending = self.tools.get_pending_interaction(
            allowed_interaction.token
        )
        self.assertIsNotNone(allowed_pending)
        allowed = self._adapter(group_policy="open")
        allowed.get_chat_info = AsyncMock(
            return_value={
                "name": "Group",
                "type": "group",
                "raw_type": "group",
            }
        )
        allowed._update_openclaw_question_card = AsyncMock(return_value=True)

        asyncio.run(
            allowed._dispatch_ask_user_answer(
                question_id=allowed_interaction.token,
                pending=allowed_pending,
                answers={"Continue?": "yes"},
                callback_event=SimpleNamespace(
                    operator=SimpleNamespace(
                        open_id="ou_user",
                        user_id="u_callback",
                        union_id="on_callback",
                    )
                ),
            )
        )

        event = allowed._handle_message_with_guards.await_args.args[0]
        self.assertTrue(event.source.role_authorized)
        resolved_sender = allowed._resolve_sender_profile.await_args.args[0]
        self.assertEqual(resolved_sender.user_id, "u_callback")
        self.assertEqual(resolved_sender.union_id, "on_callback")
        self.assertEqual(
            self.tools.ticket_from_event(event).sender_open_id,
            "ou_user",
        )

    def test_reaction_rechecks_group_policy_and_sets_grant(self) -> None:
        def reaction_data() -> Any:
            return SimpleNamespace(
                event=SimpleNamespace(
                    message_id="om_bot_message",
                    user_id=SimpleNamespace(
                        open_id="ou_user",
                        user_id="u_user",
                        union_id="on_user",
                    ),
                    reaction_type=SimpleNamespace(emoji_type="THUMBSUP"),
                )
            )

        message = SimpleNamespace(
            sender=SimpleNamespace(sender_type="app", id="cli_test"),
            chat_id="oc_chat",
            chat_type="group",
            root_id="om_root",
            thread_id="omt_native",
            body=SimpleNamespace(content='{"text":"bot reply"}'),
            msg_type="text",
            mentions=[],
        )
        response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(items=[message]),
        )

        denied = self._adapter(group_policy="disabled")
        denied._app_id = "cli_test"
        denied._client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(
                    message=SimpleNamespace(get=object()),
                )
            )
        )
        denied._reaction_notifications = "own"
        denied._build_get_message_request = lambda message_id: message_id
        denied._run_blocking = AsyncMock(return_value=response)
        denied.get_chat_info = AsyncMock(
            return_value={
                "name": "Group",
                "type": "group",
                "raw_type": "group",
            }
        )
        asyncio.run(
            denied._handle_reaction_event("reaction_created", reaction_data())
        )
        denied._handle_message_with_guards.assert_not_awaited()

        allowed = self._adapter(group_policy="open")
        allowed._app_id = "cli_test"
        allowed._client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(
                    message=SimpleNamespace(get=object()),
                )
            )
        )
        allowed._reaction_notifications = "own"
        allowed._build_get_message_request = lambda message_id: message_id
        allowed._run_blocking = AsyncMock(return_value=response)
        allowed.get_chat_info = AsyncMock(
            return_value={
                "name": "Group",
                "type": "group",
                "raw_type": "group",
            }
        )
        asyncio.run(
            allowed._handle_reaction_event("reaction_created", reaction_data())
        )

        event = allowed._handle_message_with_guards.await_args.args[0]
        self.assertTrue(event.source.role_authorized)
        self.assertEqual(event.source.thread_id, "om_root")
        self.assertEqual(event.source.feishu_thread_id, "omt_native")
        self.assertEqual(
            self.tools.ticket_from_event(event).sender_open_id,
            "ou_user",
        )


if __name__ == "__main__":
    unittest.main()
