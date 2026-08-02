"""Focused parity tests for Feishu message conversion and synthetic events."""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from collections import OrderedDict
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

from tests.test_ask_user_question_adapter import _MISSING_MODULE, _load_modules


class _FakeCallbackValue:
    """Minimal callback model used for synchronous toast assertions."""

    def __init__(self) -> None:
        self.type = None
        self.content = None
        self.toast = None
        self.card = None
        self.data = None


class MessageParityTests(unittest.TestCase):
    """Verify the latest upstream message-path compatibility behavior."""

    @classmethod
    def setUpClass(cls) -> None:
        _, cls.adapter_module, cls.previous_modules = _load_modules()
        cls.previous_callback_types = (
            cls.adapter_module.P2CardActionTriggerResponse,
            cls.adapter_module.CallBackToast,
            cls.adapter_module.CallBackCard,
        )
        cls.adapter_module.P2CardActionTriggerResponse = _FakeCallbackValue
        cls.adapter_module.CallBackToast = _FakeCallbackValue
        cls.adapter_module.CallBackCard = _FakeCallbackValue

    @classmethod
    def tearDownClass(cls) -> None:
        (
            cls.adapter_module.P2CardActionTriggerResponse,
            cls.adapter_module.CallBackToast,
            cls.adapter_module.CallBackCard,
        ) = cls.previous_callback_types
        for name, previous in cls.previous_modules.items():
            if previous is _MISSING_MODULE:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    def _mention_adapter(self) -> Any:
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter._account_id = "work"
        adapter._namespace_account = False
        adapter._client = object()
        adapter._outbound_mention_registry = OrderedDict()
        adapter._outbound_mention_snapshots = OrderedDict()
        return adapter

    def _card_adapter(self) -> Any:
        settings = self.adapter_module.FeishuAdapter._load_settings(
            {
                "appId": "cli_test",
                "appSecret": "secret",
                "dmPolicy": "open",
                "allowFrom": ["*"],
                "groupPolicy": "open",
                "requireMention": True,
            }
        )
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter._apply_settings(settings)
        adapter.platform = self.adapter_module.Platform.FEISHU
        adapter._account_id = "work"
        adapter._namespace_account = False
        adapter._bot_open_id = "ou_bot"
        adapter._bot_user_id = "u_bot"
        adapter._bot_name = "Hermes"
        adapter._card_action_tokens = {}
        adapter._thread_routes_by_message = OrderedDict()
        adapter._dedup_cache_size = 100
        adapter._outbound_mention_registry = OrderedDict()
        adapter._outbound_mention_snapshots = OrderedDict()
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
                            root_id="om_root",
                            thread_id="omt_native",
                        )
                    ]
                ),
            )

        adapter._run_blocking = run_blocking
        adapter.get_chat_info = AsyncMock(
            return_value={
                "name": "Group",
                "type": "group",
                "raw_type": "group",
            }
        )
        adapter._resolve_sender_profile = AsyncMock(
            return_value={
                "user_id": "u_user",
                "user_name": "Alice",
                "user_id_alt": "on_user",
            }
        )
        adapter._resolve_channel_prompt = lambda *_args: None
        adapter._handle_message_with_guards = AsyncMock()
        adapter._loop = SimpleNamespace(is_closed=lambda: False)
        return adapter

    @staticmethod
    def _card_data(
        *,
        event_id: str = "evt_click",
        operator_open_id: str = "ou_user",
        operator_user_id: str = "u_user",
        action_value: Any = None,
    ) -> Any:
        return SimpleNamespace(
            event=SimpleNamespace(
                event_id=event_id,
                token="token_click",
                context=SimpleNamespace(
                    open_chat_id="oc_chat",
                    open_message_id="om_card",
                ),
                operator=SimpleNamespace(
                    open_id=operator_open_id,
                    user_id=operator_user_id,
                    union_id="on_user",
                ),
                action=SimpleNamespace(
                    tag="button",
                    value=(
                        action_value
                        if action_value is not None
                        else {
                            "action": "inject_prompt",
                            "prompt": "Run the report",
                        }
                    ),
                ),
            )
        )

    def test_post_prefers_nonempty_content_v2(self) -> None:
        """Rich posts select content_v2 and retain legacy fallback support."""
        payload = json.dumps(
            {
                "zh_cn": {
                    "content": [[{"tag": "text", "text": "legacy"}]],
                    "content_v2": [[{"tag": "text", "text": "v2"}]],
                }
            }
        )

        parsed = self.adapter_module.normalize_feishu_message(
            message_type="post",
            raw_content=payload,
        )

        self.assertEqual(parsed.text_content, "v2")

    def test_merge_forward_expands_one_flat_api_tree_recursively(self) -> None:
        """Nested merged forwards are rebuilt from upper_message_id links."""
        adapter = self._mention_adapter()
        adapter._bot_open_id = "ou_bot"
        adapter._bot_user_id = "u_bot"
        adapter._bot_name = "Hermes"
        adapter._fetch_merge_forward_items = AsyncMock(
            return_value=[
                {
                    "message_id": "om_root",
                    "msg_type": "merge_forward",
                    "body": {"content": "{}"},
                    "sender": {"id": "ou_alice", "sender_type": "user"},
                },
                {
                    "message_id": "om_last",
                    "msg_type": "text",
                    "create_time": "3000",
                    "body": {"content": json.dumps({"text": "last"})},
                    "sender": {"id": "ou_bob", "sender_type": "user"},
                },
                {
                    "message_id": "om_nested",
                    "msg_type": "merge_forward",
                    "create_time": "2000",
                    "body": {"content": "{}"},
                    "sender": {"id": "ou_alice", "sender_type": "user"},
                },
                {
                    "message_id": "om_nested_child",
                    "upper_message_id": "om_nested",
                    "msg_type": "post",
                    "create_time": "2500",
                    "body": {
                        "content": json.dumps(
                            {
                                "content": [
                                    [{"tag": "text", "text": "legacy"}]
                                ],
                                "content_v2": [
                                    [{"tag": "text", "text": "inside v2"}]
                                ],
                            }
                        )
                    },
                    "sender": {"id": "ou_bob", "sender_type": "user"},
                },
                {
                    "message_id": "om_first",
                    "msg_type": "text",
                    "create_time": "1000",
                    "body": {"content": json.dumps({"text": "first"})},
                    "sender": {"id": "ou_alice", "sender_type": "user"},
                },
            ]
        )

        async def resolve_name(sender_id: str, *, is_bot: bool = False) -> str:
            del is_bot
            return {"ou_alice": "Alice", "ou_bob": "Bob"}[sender_id]

        adapter._resolve_sender_name_from_api = resolve_name
        fallback = self.adapter_module.FeishuNormalizedMessage(
            raw_type="merge_forward",
            text_content="payload preview",
            relation_kind="merge_forward",
        )

        expanded = asyncio.run(
            adapter._expand_merge_forward_message("om_root", fallback)
        )

        self.assertTrue(expanded.metadata["api_expanded"])
        self.assertIn("<forwarded_messages>", expanded.text_content)
        self.assertIn("inside v2", expanded.text_content)
        self.assertLess(
            expanded.text_content.index("first"),
            expanded.text_content.index("inside v2"),
        )
        self.assertLess(
            expanded.text_content.index("inside v2"),
            expanded.text_content.index("last"),
        )

    def test_merge_forward_keeps_payload_fallback_when_api_fails(self) -> None:
        """The pure event-payload normalizer remains the failure fallback."""
        adapter = self._mention_adapter()
        adapter._fetch_merge_forward_items = AsyncMock(
            side_effect=RuntimeError("unavailable")
        )
        fallback = self.adapter_module.FeishuNormalizedMessage(
            raw_type="merge_forward",
            text_content="payload preview",
            relation_kind="merge_forward",
        )

        expanded = asyncio.run(
            adapter._expand_merge_forward_message("om_root", fallback)
        )

        self.assertIs(expanded, fallback)

    def test_inject_prompt_returns_toast_and_routes_plain_user_text(self) -> None:
        """A generic inject_prompt button follows the regular text pipeline."""
        adapter = self._card_adapter()
        scheduled: list[Any] = []
        adapter._submit_on_loop = (
            lambda _loop, coroutine: scheduled.append(coroutine) or True
        )
        data = self._card_data()

        response = adapter._on_card_action_trigger(data)
        self.assertEqual(response.toast.type, "info")
        self.assertIn("Processing", response.toast.content)
        asyncio.run(scheduled.pop())

        event = adapter._handle_message_with_guards.await_args.args[0]
        self.assertEqual(event.text, "Run the report")
        self.assertIs(event.message_type, self.adapter_module.MessageType.TEXT)
        self.assertEqual(event.message_id, "evt_click")
        self.assertEqual(event.source.thread_id, "om_root")
        self.assertEqual(event.reply_to_message_id, "om_card")

    def test_inject_prompt_rejects_schema_two_user_id_only(self) -> None:
        """Prompt injection keeps upstream's strict open-ID requirement."""
        adapter = self._card_adapter()
        adapter._submit_on_loop = lambda *args: self.fail("must not schedule")

        response = adapter._on_card_action_trigger(
            self._card_data(operator_open_id="")
        )

        self.assertEqual(response.toast.type, "error")
        self.assertIn("could not be processed", response.toast.content)
        adapter._handle_message_with_guards.assert_not_awaited()

    def test_generic_card_action_accepts_schema_two_user_id(self) -> None:
        """Generic actions retain user IDs in their own namespace."""
        adapter = self._card_adapter()
        scheduled: list[Any] = []
        adapter._submit_on_loop = (
            lambda _loop, coroutine: scheduled.append(coroutine) or True
        )
        data = self._card_data(
            operator_open_id="",
            action_value={"action": "reports:refresh"},
        )

        adapter._on_card_action_trigger(data)
        asyncio.run(scheduled.pop())

        sender_id = adapter._resolve_sender_profile.await_args.args[0]
        self.assertEqual(sender_id.open_id, "")
        self.assertEqual(sender_id.user_id, "u_user")
        event = adapter._handle_message_with_guards.await_args.args[0]
        self.assertIs(event.message_type, self.adapter_module.MessageType.COMMAND)
        self.assertIn("reports:refresh", event.text)

    def test_reaction_callback_uses_one_stable_dedup_key(self) -> None:
        """A replayed WebSocket reaction schedules only one synthetic turn."""
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter._reaction_notifications = "own"
        adapter._loop = SimpleNamespace(is_closed=lambda: False)
        seen: set[str] = set()

        def duplicate(key: str) -> bool:
            if key in seen:
                return True
            seen.add(key)
            return False

        scheduled: list[Any] = []
        adapter._is_duplicate = duplicate
        adapter._submit_on_loop = (
            lambda _loop, coroutine: scheduled.append(coroutine) or True
        )
        data = SimpleNamespace(
            event=SimpleNamespace(
                action_time=None,
                message_id="om_bot_reply",
                operator_type="user",
                user_id=SimpleNamespace(open_id="ou_alice"),
                reaction_type=SimpleNamespace(emoji_type="THUMBSUP"),
            )
        )

        adapter._on_reaction_event("im.message.reaction.created_v1", data)
        adapter._on_reaction_event("im.message.reaction.created_v1", data)

        self.assertEqual(
            seen,
            {"om_bot_reply:reaction:THUMBSUP:ou_alice"},
        )
        self.assertEqual(len(scheduled), 1)
        scheduled.pop().close()

    def test_event_ownership_accepts_missing_or_matching_app_id(self) -> None:
        """The authenticated transport may omit app_id but cannot name a peer app."""
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter._app_id = "cli_current"
        adapter._account_id = "work"

        self.assertTrue(adapter._is_event_ownership_valid(SimpleNamespace()))
        self.assertTrue(
            adapter._is_event_ownership_valid({"app_id": "cli_current"})
        )
        self.assertTrue(
            adapter._is_event_ownership_valid(
                SimpleNamespace(
                    header=SimpleNamespace(app_id="cli_current")
                )
            )
        )
        self.assertFalse(
            adapter._is_event_ownership_valid(
                SimpleNamespace(app_id="cli_other")
            )
        )
        self.assertFalse(
            adapter._is_event_ownership_valid(
                {"header": {"app_id": "cli_other"}}
            )
        )

        adapter._app_id = ""
        self.assertTrue(
            adapter._is_event_ownership_valid({"app_id": "cli_other"})
        )

    def test_mismatched_app_event_stops_before_callback_state_changes(self) -> None:
        """Every stateful callback rejects a flattened peer application envelope."""
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter._app_id = "cli_current"
        adapter._account_id = "work"
        adapter._loop = SimpleNamespace(is_closed=lambda: False)
        adapter._chat_info_cache = {"oc_chat": {"name": "Cached"}}
        adapter._submit_on_loop = Mock()
        adapter._is_duplicate = Mock(return_value=False)
        adapter._resolve_ask_user_action_token = Mock(return_value=(False, None))
        mismatched = SimpleNamespace(
            header=SimpleNamespace(app_id="cli_other"),
            event=SimpleNamespace(chat_id="oc_chat"),
        )

        adapter._on_message_event(mismatched)
        adapter._on_reaction_event(
            "im.message.reaction.created_v1",
            mismatched,
        )
        adapter._on_bot_added_to_chat(mismatched)
        adapter._on_bot_removed_from_chat(mismatched)
        adapter._on_drive_comment_event(mismatched)
        adapter._on_meeting_invited_event(mismatched)
        adapter._on_card_action_trigger(mismatched)

        self.assertEqual(
            adapter._chat_info_cache,
            {"oc_chat": {"name": "Cached"}},
        )
        adapter._submit_on_loop.assert_not_called()
        adapter._is_duplicate.assert_not_called()
        adapter._resolve_ask_user_action_token.assert_not_called()

    def test_outbound_mentions_resolve_names_and_mask_non_mentions(self) -> None:
        """Plain names become structured mentions without touching code or email."""
        adapter = self._mention_adapter()
        adapter._record_outbound_mention_target(
            "oc_chat",
            "ou_alice",
            "Alice",
        )

        normalized = asyncio.run(
            adapter._normalize_outbound_mentions(
                "Hi @Alice, `@Alice`, alice@example.com, and @everyone. "
                "<at open_id='ou_bob'>Bob</at>",
                "oc_chat",
            )
        )

        self.assertIn('<at user_id="ou_alice">Alice</at>', normalized)
        self.assertIn('`@Alice`', normalized)
        self.assertIn("alice@example.com", normalized)
        self.assertIn('<at user_id="all">Everyone</at>', normalized)
        self.assertIn('<at user_id="ou_bob">Bob</at>', normalized)

    def test_outbound_mentions_prefetch_and_preserve_ambiguous_names(self) -> None:
        """Chat members fill cache misses while duplicate display names stay plain."""
        adapter = self._mention_adapter()
        requested: list[str] = []

        async def tenant_get_json(
            uri: str,
            _queries: Any = (),
        ) -> dict[str, Any]:
            requested.append(uri)
            if uri.endswith("/members/bots"):
                return {"code": 0, "data": {"items": []}}
            return {
                "code": 0,
                "data": {
                    "items": [
                        {"member_id": "ou_sam_1", "name": "Sam"},
                        {"member_id": "ou_sam_2", "name": "Sam"},
                        {"member_id": "ou_zoe", "name": "Zoe"},
                    ]
                },
            }

        adapter._tenant_get_json = tenant_get_json
        normalized = asyncio.run(
            adapter._normalize_outbound_mentions(
                "Ask @Sam and @Zoe",
                "oc_chat",
            )
        )

        self.assertEqual(
            requested,
            [
                "/open-apis/im/v1/chats/oc_chat/members/bots",
                "/open-apis/im/v1/chats/oc_chat/members",
            ],
        )
        self.assertIn("@Sam", normalized)
        self.assertIn('<at user_id="ou_zoe">Zoe</at>', normalized)


if __name__ == "__main__":
    unittest.main()
