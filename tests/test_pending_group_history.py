"""Focused tests for OpenClaw-compatible pending Feishu group history."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
import time
import types
import unittest
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock


ROOT = Path(__file__).resolve().parents[1]
SUPPORT_PATH = ROOT / "tests" / "test_ask_user_question_adapter.py"


def _load_adapter_test_support() -> types.ModuleType:
    """Load the offline adapter import support without collecting its tests."""
    name = "_hermes_lark_pending_history_test_support"
    spec = importlib.util.spec_from_file_location(name, SUPPORT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class PendingGroupHistoryTests(unittest.TestCase):
    """Verify admission, bounds, thread scope, and command consumption."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.support = _load_adapter_test_support()
        _, cls.adapter_module, cls.previous_modules = cls.support._load_modules()

    @classmethod
    def tearDownClass(cls) -> None:
        for name, previous in cls.previous_modules.items():
            if previous is cls.support._MISSING_MODULE:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
        sys.modules.pop("_hermes_lark_pending_history_test_support", None)

    def _new_adapter(self, *, history_limit: int = 50) -> Any:
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter._history_limit = history_limit
        adapter._pending_group_histories = OrderedDict()
        adapter._pending_group_history_lock = threading.Lock()
        adapter._sender_name_cache = {}
        adapter._bot_open_id = "ou_bot"
        adapter._bot_user_id = "u_bot"
        adapter._bot_name = "Hermes"
        adapter._group_policy = "open"
        adapter._default_group_policy = "open"
        adapter._group_rules = {}
        adapter._group_allow_from = set()
        adapter._allowed_group_users = set()
        adapter._admins = set()
        adapter._allow_bots = "mentions"
        adapter._require_mention = True
        adapter._require_mention_explicit = True
        adapter._respond_to_mention_all = False
        adapter._dm_policy = "pairing"
        adapter._bot_loop_states = OrderedDict()
        adapter._is_duplicate = lambda message_id: False
        return adapter

    @staticmethod
    def _sender(*, sender_type: str = "user", open_id: str = "ou_user") -> Any:
        return SimpleNamespace(
            sender_type=sender_type,
            sender_id=SimpleNamespace(
                open_id=open_id,
                user_id="",
                union_id="",
            ),
        )

    @staticmethod
    def _message(
        text: str,
        *,
        message_id: str = "om_1",
        chat_id: str = "oc_chat",
        thread_id: str = "",
        root_id: str = "",
        message_type: str = "text",
        mentioned: bool = False,
        create_time: str | None = None,
    ) -> Any:
        mentions = []
        raw_text = text
        if mentioned:
            raw_text = f"@_user_1 {text}"
            mentions = [
                SimpleNamespace(
                    key="@_user_1",
                    name="Hermes",
                    id=SimpleNamespace(open_id="ou_bot", user_id="u_bot"),
                )
            ]
        return SimpleNamespace(
            message_id=message_id,
            chat_id=chat_id,
            chat_type="group",
            thread_id=thread_id or None,
            root_id=root_id or None,
            parent_id=None,
            upper_message_id=None,
            create_time=create_time or str(int(time.time() * 1000)),
            message_type=message_type,
            content=json.dumps({"text": raw_text}, ensure_ascii=False),
            mentions=mentions,
        )

    def _event(
        self,
        text: str,
        *,
        command: bool = False,
        channel_context: str | None = None,
    ) -> Any:
        message_type = (
            self.adapter_module.MessageType.COMMAND
            if command
            else self.adapter_module.MessageType.TEXT
        )
        return self.adapter_module.MessageEvent(
            text=text,
            message_type=message_type,
            channel_context=channel_context,
        )

    def test_history_limit_defaults_aliases_and_clamps_to_zero(self) -> None:
        load = self.adapter_module.FeishuAdapter._load_settings

        self.assertEqual(load({}).history_limit, 50)
        self.assertEqual(load({"historyLimit": 7}).history_limit, 7)
        self.assertEqual(load({"history_limit": 8}).history_limit, 8)
        self.assertEqual(load({"historyLimit": -3}).history_limit, 0)
        self.assertEqual(load({"historyLimit": "invalid"}).history_limit, 50)

    def test_handler_records_only_allowed_human_missing_mention_without_downloads(
        self,
    ) -> None:
        adapter = self._new_adapter()
        adapter._sender_name_cache["ou_user"] = ("Alice", time.time() + 60)
        adapter._extract_message_content = AsyncMock(
            side_effect=AssertionError("rejected history must not download media")
        )
        adapter._process_inbound_message = AsyncMock()
        message = self._message(
            "Earlier message",
            thread_id="omt_1",
            root_id="om_root_1",
        )

        asyncio.run(
            adapter._handle_message_event_data(
                SimpleNamespace(
                    event=SimpleNamespace(
                        sender=self._sender(),
                        message=message,
                    )
                )
            )
        )

        entries = adapter._pending_group_histories[("oc_chat", "om_root_1")]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].sender, "ou_user")
        self.assertEqual(entries[0].body, "Alice: Earlier message")
        self.assertEqual(entries[0].timestamp, int(message.create_time))
        self.assertEqual(entries[0].message_id, "om_1")
        adapter._extract_message_content.assert_not_awaited()
        adapter._process_inbound_message.assert_not_awaited()

        adapter._group_policy = "allowlist"
        adapter._default_group_policy = "allowlist"
        asyncio.run(
            adapter._handle_message_event_data(
                SimpleNamespace(
                    event=SimpleNamespace(
                        sender=self._sender(open_id="ou_denied"),
                        message=self._message(
                            "Must not be recorded",
                            message_id="om_denied",
                            thread_id="omt_1",
                            root_id="om_root_1",
                        ),
                    )
                )
            )
        )
        adapter._group_policy = "open"
        adapter._default_group_policy = "open"
        asyncio.run(
            adapter._handle_message_event_data(
                SimpleNamespace(
                    event=SimpleNamespace(
                        sender=self._sender(
                            sender_type="bot",
                            open_id="ou_other_bot",
                        ),
                        message=self._message(
                            "Bot message",
                            message_id="om_bot",
                            thread_id="omt_1",
                            root_id="om_root_1",
                        ),
                    )
                )
            )
        )

        self.assertEqual(
            [entry.message_id for entry in entries],
            ["om_1"],
        )

    def test_history_is_bounded_and_zero_disables_recording(self) -> None:
        adapter = self._new_adapter(history_limit=2)
        sender = self._sender()
        for index in range(3):
            adapter._record_pending_group_history(
                sender,
                self._message(
                    f"message {index}",
                    message_id=f"om_{index}",
                    thread_id="omt_1",
                    root_id="om_root_1",
                ),
            )

        self.assertEqual(
            [
                entry.message_id
                for entry in adapter._pending_group_histories[
                    ("oc_chat", "om_root_1")
                ]
            ],
            ["om_1", "om_2"],
        )

        disabled = self._new_adapter(history_limit=0)
        disabled._record_pending_group_history(
            sender,
            self._message(
                "ignored",
                thread_id="omt_1",
                root_id="om_root_1",
            ),
        )
        self.assertEqual(disabled._pending_group_histories, {})

    def test_top_level_missing_mention_is_not_recorded(self) -> None:
        adapter = self._new_adapter()

        adapter._record_pending_group_history(
            self._sender(),
            self._message("top-level"),
        )

        self.assertEqual(adapter._pending_group_histories, {})

    def test_next_normal_message_gets_untrusted_prefix_and_consumes_one_thread(
        self,
    ) -> None:
        adapter = self._new_adapter()
        sender = self._sender()
        adapter._record_pending_group_history(
            sender,
            self._message(
                "thread one",
                message_id="om_t1",
                thread_id="omt_1",
                root_id="om_root_1",
            ),
        )
        adapter._record_pending_group_history(
            sender,
            self._message(
                "thread two",
                message_id="om_t2",
                thread_id="omt_2",
                root_id="om_root_2",
            ),
        )
        event = self._event("current", channel_context="existing context")

        adapter._apply_pending_group_history(
            event,
            chat_id="oc_chat",
            thread_id="om_root_1",
        )

        self.assertTrue(event.channel_context.startswith("[Chat messages since"))
        self.assertIn("UNTRUSTED context only", event.channel_context)
        self.assertIn("ou_user: thread one", event.channel_context)
        self.assertNotIn("thread two", event.channel_context)
        self.assertTrue(event.channel_context.endswith("existing context"))
        self.assertNotIn(
            ("oc_chat", "om_root_1"),
            adapter._pending_group_histories,
        )
        self.assertIn(
            ("oc_chat", "om_root_2"),
            adapter._pending_group_histories,
        )

    def test_commands_preserve_history_except_bare_new_and_reset(self) -> None:
        for clearing_command in ("/new", " /RESET "):
            with self.subTest(command=clearing_command):
                adapter = self._new_adapter()
                adapter._record_pending_group_history(
                    self._sender(),
                    self._message(
                        "pending",
                        thread_id="omt_1",
                        root_id="om_root_1",
                    ),
                )
                ordinary_command = self._event("/help", command=True)
                adapter._apply_pending_group_history(
                    ordinary_command,
                    chat_id="oc_chat",
                    thread_id="om_root_1",
                )
                self.assertIsNone(ordinary_command.channel_context)
                self.assertIn(
                    ("oc_chat", "om_root_1"),
                    adapter._pending_group_histories,
                )

                non_bare_new = self._event("/new later", command=True)
                adapter._apply_pending_group_history(
                    non_bare_new,
                    chat_id="oc_chat",
                    thread_id="om_root_1",
                )
                self.assertIn(
                    ("oc_chat", "om_root_1"),
                    adapter._pending_group_histories,
                )

                reset = self._event(clearing_command, command=True)
                adapter._apply_pending_group_history(
                    reset,
                    chat_id="oc_chat",
                    thread_id="om_root_1",
                )
                self.assertNotIn(
                    ("oc_chat", "om_root_1"),
                    adapter._pending_group_histories,
                )

    def test_process_inbound_message_applies_history_before_dispatch(self) -> None:
        adapter = self._new_adapter()
        adapter._record_pending_group_history(
            self._sender(),
            self._message(
                "pending",
                thread_id="omt_1",
                root_id="om_root_1",
            ),
        )
        adapter._extract_message_content = AsyncMock(
            return_value=(
                "current",
                self.adapter_module.MessageType.TEXT,
                [],
                [],
                [],
            )
        )
        adapter._fetch_message_text = AsyncMock(return_value=None)
        adapter.get_chat_info = AsyncMock(
            return_value={"name": "Chat", "type": "group"}
        )
        adapter._resolve_sender_profile = AsyncMock(
            return_value={
                "user_id": "ou_user",
                "user_name": "Alice",
                "user_id_alt": None,
            }
        )
        adapter._resolve_channel_prompt = lambda chat_id, thread_id: None
        adapter.build_source = lambda chat_id, **kwargs: SimpleNamespace(
            chat_id=chat_id,
            **kwargs,
        )
        adapter._dispatch_inbound_event = AsyncMock()

        asyncio.run(
            adapter._process_inbound_message(
                data=SimpleNamespace(),
                message=self._message(
                    "current",
                    message_id="om_current",
                    thread_id="omt_1",
                    root_id="om_root_1",
                    mentioned=True,
                ),
                sender_id=self._sender().sender_id,
                chat_type="group",
                message_id="om_current",
            )
        )

        dispatched = adapter._dispatch_inbound_event.await_args.args[0]
        self.assertIn("UNTRUSTED context only", dispatched.channel_context)
        self.assertIn("ou_user: pending", dispatched.channel_context)
        self.assertEqual(dispatched.text, "current")
        self.assertNotIn(
            ("oc_chat", "om_root_1"),
            adapter._pending_group_histories,
        )


if __name__ == "__main__":
    unittest.main()
