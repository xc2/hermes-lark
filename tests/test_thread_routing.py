"""Focused tests for the fixed Slack-style Feishu thread model."""

from __future__ import annotations

import asyncio
import sys
import unittest
from collections import OrderedDict
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from tests.test_ask_user_question_adapter import _MISSING_MODULE, _load_modules


class ThreadRoutingTests(unittest.TestCase):
    """Verify every admitted IM conversation is rooted in one thread session."""

    @classmethod
    def setUpClass(cls) -> None:
        _, cls.adapter_module, cls.previous_modules = _load_modules()

    @classmethod
    def tearDownClass(cls) -> None:
        for name, previous in cls.previous_modules.items():
            if previous is _MISSING_MODULE:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    def _adapter(
        self,
        *,
        chat_info: dict[str, Any],
        extra: dict[str, Any] | None = None,
    ) -> Any:
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter._account_id = "work"
        adapter._namespace_account = False
        adapter._client = object()
        adapter._bot_open_id = "ou_self"
        adapter._bot_user_id = ""
        adapter._bot_name = "Hermes"
        adapter._group_rules = {}
        adapter._history_limit = 0
        adapter._dedup_cache_size = 10
        adapter._thread_routes_by_message = OrderedDict()
        adapter.config = self.adapter_module.PlatformConfig(extra=extra or {})
        adapter.platform = self.adapter_module.Platform.FEISHU
        adapter._extract_message_content = self._async_value(
            (
                "hello",
                self.adapter_module.MessageType.TEXT,
                [],
                [],
                [],
            )
        )
        adapter._fetch_message_text = self._async_value(None)
        adapter.get_chat_info = self._async_value(chat_info)
        adapter._resolve_sender_profile = self._async_value(
            {
                "user_id": "u_user",
                "user_name": "Alice",
                "user_id_alt": "on_user",
            }
        )
        adapter._resolve_channel_prompt = lambda *_args: None
        adapter._apply_pending_group_history = lambda *_args, **_kwargs: None
        return adapter

    async def _inbound(
        self,
        adapter: Any,
        *,
        chat_type: str,
        thread_id: str | None,
        root_id: str | None,
        parent_id: str | None = None,
        upper_message_id: str | None = None,
        message_id: str = "om_inbound",
    ) -> Any | None:
        captured: list[Any] = []

        async def dispatch(event: Any) -> None:
            captured.append(event)

        adapter._dispatch_inbound_event = dispatch
        await adapter._process_inbound_message(
            data=SimpleNamespace(),
            message=SimpleNamespace(
                message_id=message_id,
                chat_id="oc_chat",
                chat_type=chat_type,
                thread_id=thread_id,
                root_id=root_id,
                parent_id=parent_id,
                upper_message_id=upper_message_id,
            ),
            sender_id=SimpleNamespace(
                open_id="ou_user",
                user_id="u_user",
                union_id="on_user",
            ),
            chat_type=chat_type,
            message_id=message_id,
        )
        return captured[0] if captured else None

    def test_group_top_level_uses_message_id_as_thread_root(self) -> None:
        adapter = self._adapter(
            chat_info={
                "name": "Group",
                "type": "group",
                "chat_mode": "group",
            }
        )

        event = asyncio.run(
            self._inbound(
                adapter,
                chat_type="group",
                thread_id=None,
                root_id=None,
                message_id="om_group_root",
            )
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.source.thread_id, "om_group_root")
        self.assertEqual(
            event.metadata["feishu_session_thread_id"],
            "om_group_root",
        )
        self.assertNotIn("feishu_thread_id", event.metadata)
        self.assertEqual(
            adapter._thread_route_for_message("om_group_root"),
            "om_group_root",
        )

    def test_dm_top_level_uses_message_id_as_thread_root(self) -> None:
        adapter = self._adapter(
            chat_info={
                "name": "Direct message",
                "type": "dm",
            }
        )

        event = asyncio.run(
            self._inbound(
                adapter,
                chat_type="p2p",
                thread_id=None,
                root_id=None,
                message_id="om_dm_root",
            )
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.source.chat_type, "dm")
        self.assertEqual(event.source.thread_id, "om_dm_root")
        self.assertEqual(
            event.metadata["feishu_session_thread_id"],
            "om_dm_root",
        )
        self.assertNotIn("feishu_thread_id", event.metadata)

    def test_native_thread_uses_message_root_not_native_thread_id(self) -> None:
        adapter = self._adapter(
            chat_info={
                "name": "Topic Group",
                "type": "group",
                "chat_mode": "topic",
            }
        )

        event = asyncio.run(
            self._inbound(
                adapter,
                chat_type="group",
                thread_id="omt_native",
                root_id="om_canonical_root",
                parent_id="om_parent_reply",
                message_id="om_thread_reply",
            )
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.source.thread_id, "om_canonical_root")
        self.assertEqual(
            event.metadata["feishu_session_thread_id"],
            "om_canonical_root",
        )
        self.assertEqual(event.metadata["feishu_thread_id"], "omt_native")
        self.assertEqual(
            adapter._thread_route_for_message("om_thread_reply"),
            "om_canonical_root",
        )

    def test_thread_capable_group_root_uses_its_own_message_id(self) -> None:
        """Both topic-style roots start a session despite carrying thread_id."""
        chat_infos = (
            {
                "name": "Thread Group",
                "type": "group",
                "chat_mode": "group",
                "group_message_type": "thread",
            },
            {
                "name": "Topic Group",
                "type": "forum",
                "chat_mode": "topic",
            },
        )

        for chat_info in chat_infos:
            with self.subTest(chat_info=chat_info):
                adapter = self._adapter(chat_info=chat_info)
                event = asyncio.run(
                    self._inbound(
                        adapter,
                        chat_type="group",
                        thread_id="omt_native",
                        root_id=None,
                        parent_id=None,
                        message_id="om_thread_root",
                    )
                )

                self.assertIsNotNone(event)
                self.assertEqual(event.source.thread_id, "om_thread_root")
                self.assertEqual(
                    event.metadata["feishu_session_thread_id"],
                    "om_thread_root",
                )
                self.assertEqual(
                    event.metadata["feishu_thread_id"],
                    "omt_native",
                )
                self.assertEqual(
                    adapter._thread_route_for_message("om_thread_root"),
                    "om_thread_root",
                )

    def test_top_level_quote_does_not_inherit_quoted_root(self) -> None:
        adapter = self._adapter(
            chat_info={
                "name": "Group",
                "type": "group",
                "chat_mode": "group",
            }
        )

        event = asyncio.run(
            self._inbound(
                adapter,
                chat_type="group",
                thread_id=None,
                root_id="om_quoted_root",
                parent_id="om_quoted_message",
                message_id="om_new_root",
            )
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.source.thread_id, "om_new_root")
        self.assertEqual(
            event.metadata["feishu_session_thread_id"],
            "om_new_root",
        )
        self.assertNotIn("feishu_thread_id", event.metadata)
        self.assertEqual(event.reply_to_message_id, "om_quoted_message")

    def test_legacy_thread_options_do_not_change_routing(self) -> None:
        legacy_options = (
            {
                "threadSession": False,
                "replyInThread": False,
                "thread_session": False,
                "reply_in_thread": False,
            },
            {
                "threadSession": True,
                "replyInThread": True,
                "thread_session": True,
                "reply_in_thread": True,
            },
        )

        for extra in legacy_options:
            with self.subTest(extra=extra):
                adapter = self._adapter(
                    chat_info={
                        "name": "Plain Group",
                        "type": "group",
                        "chat_mode": "group",
                    },
                    extra=extra,
                )
                top_level = asyncio.run(
                    self._inbound(
                        adapter,
                        chat_type="group",
                        thread_id=None,
                        root_id=None,
                        message_id="om_fixed_root",
                    )
                )
                native = asyncio.run(
                    self._inbound(
                        adapter,
                        chat_type="group",
                        thread_id="omt_native",
                        root_id="om_native_root",
                        message_id="om_native_reply",
                    )
                )

                self.assertEqual(top_level.source.thread_id, "om_fixed_root")
                self.assertEqual(native.source.thread_id, "om_native_root")

    def test_yaml_bridge_discards_legacy_thread_options(self) -> None:
        config = {
            "appId": "cli_test",
            "appSecret": "secret",
            "threadSession": False,
            "reply_in_thread": False,
            "groups": {
                "*": {"replyInThread": False},
            },
            "accounts": {
                "work": {
                    "thread_session": True,
                    "replyInThread": True,
                    "groups": {
                        "oc_chat": {"reply_in_thread": True},
                    },
                }
            },
        }

        normalized = self.adapter_module._apply_yaml_config({}, config)

        self.assertNotIn("threadSession", normalized)
        self.assertNotIn("reply_in_thread", normalized)
        self.assertNotIn(
            "replyInThread",
            normalized["groups"]["*"],
        )
        self.assertNotIn(
            "thread_session",
            normalized["accounts"]["work"],
        )
        self.assertNotIn(
            "replyInThread",
            normalized["accounts"]["work"],
        )
        self.assertNotIn(
            "reply_in_thread",
            normalized["accounts"]["work"]["groups"]["oc_chat"],
        )
        self.assertIn("threadSession", config)
        self.assertIn("replyInThread", config["groups"]["*"])

    def test_native_thread_without_root_fails_closed(self) -> None:
        adapter = self._adapter(
            chat_info={
                "name": "Ordinary Group",
                "type": "group",
                "chat_mode": "group",
            }
        )

        event = asyncio.run(
            self._inbound(
                adapter,
                chat_type="group",
                thread_id="omt_without_root",
                root_id=None,
                message_id="om_orphan_reply",
            )
        )

        self.assertIsNone(event)
        self.assertIsNone(
            adapter._thread_route_for_message("om_orphan_reply")
        )

    def test_native_thread_without_root_uses_a_remembered_parent_route(
        self,
    ) -> None:
        adapter = self._adapter(
            chat_info={
                "name": "Topic Group",
                "type": "group",
                "chat_mode": "topic",
            }
        )
        adapter._remember_thread_route("om_parent", "om_canonical_root")

        event = asyncio.run(
            self._inbound(
                adapter,
                chat_type="group",
                thread_id="omt_native",
                root_id=None,
                parent_id="om_parent",
                message_id="om_reply",
            )
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.source.thread_id, "om_canonical_root")

    def test_canonical_root_overrides_reply_anchor(self) -> None:
        adapter, reply_method, create_method, captured = (
            self._outbound_adapter()
        )

        asyncio.run(
            adapter._send_raw_message(
                chat_id="oc_chat",
                msg_type="text",
                payload='{"text":"hello"}',
                reply_to="om_quoted_message",
                metadata={"thread_id": "om_canonical_root"},
            )
        )

        self.assertIs(captured["method"], reply_method)
        self.assertIsNot(captured["method"], create_method)
        self.assertEqual(captured["request"]["message_id"], "om_canonical_root")
        self.assertTrue(captured["request"]["reply_in_thread"])

    def test_metadata_root_without_reply_anchor_still_uses_reply_api(self) -> None:
        adapter, reply_method, create_method, captured = (
            self._outbound_adapter()
        )

        asyncio.run(
            adapter._send_raw_message(
                chat_id="oc_chat",
                msg_type="text",
                payload='{"text":"hello"}',
                reply_to=None,
                metadata={"thread_id": "om_canonical_root"},
            )
        )

        self.assertIs(captured["method"], reply_method)
        self.assertIsNot(captured["method"], create_method)
        self.assertEqual(captured["request"]["message_id"], "om_canonical_root")
        self.assertTrue(captured["request"]["reply_in_thread"])

    def test_thread_reply_failure_never_falls_back_to_top_level(self) -> None:
        adapter = self._adapter(
            chat_info={"name": "Group", "type": "group"}
        )
        failure_code = next(iter(self.adapter_module._FEISHU_REPLY_FALLBACK_CODES))
        failure = SimpleNamespace(
            success=lambda: False,
            code=failure_code,
            msg="missing",
        )
        adapter._send_raw_message = AsyncMock(return_value=failure)

        result = asyncio.run(
            adapter._feishu_send_with_retry(
                chat_id="oc_chat",
                msg_type="text",
                payload='{"text":"hello"}',
                reply_to="om_child",
                metadata={"thread_id": "om_canonical_root"},
            )
        )

        self.assertIs(result, failure)
        adapter._send_raw_message.assert_awaited_once_with(
            chat_id="oc_chat",
            msg_type="text",
            payload='{"text":"hello"}',
            reply_to="om_child",
            metadata={"thread_id": "om_canonical_root"},
        )

    def _outbound_adapter(
        self,
    ) -> tuple[Any, object, object, dict[str, Any]]:
        adapter = self._adapter(
            chat_info={"name": "Group", "type": "group"}
        )
        reply_method = object()
        create_method = object()
        adapter._client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(
                    message=SimpleNamespace(
                        reply=reply_method,
                        create=create_method,
                    ),
                )
            )
        )
        captured: dict[str, Any] = {}
        adapter._build_reply_message_body = lambda **kwargs: dict(kwargs)
        adapter._build_reply_message_request = (
            lambda message_id, request_body: {
                "message_id": message_id,
                **request_body,
            }
        )

        async def run_blocking(method: Any, request: dict[str, Any]) -> Any:
            captured["method"] = method
            captured["request"] = request
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id="om_outbound"),
            )

        adapter._run_blocking = run_blocking
        return adapter, reply_method, create_method, captured

    @staticmethod
    def _async_value(value: Any) -> Any:
        async def resolve(*_args: Any, **_kwargs: Any) -> Any:
            return value

        return resolve


if __name__ == "__main__":
    unittest.main()
