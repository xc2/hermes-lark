"""Focused tests for Feishu admission and Hermes authorization handoff."""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from collections import OrderedDict
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from tests.test_ask_user_question_adapter import _MISSING_MODULE, _load_modules


class FeishuAuthorizationPolicyTests(unittest.TestCase):
    """Verify adapter policy grants and pairing identities remain isolated."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tools, cls.module, cls.previous_modules = _load_modules()

    @classmethod
    def tearDownClass(cls) -> None:
        for name, previous in cls.previous_modules.items():
            if previous is _MISSING_MODULE:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    def _new_adapter(
        self,
        *,
        dm_policy: str = "pairing",
        account_id: str = "",
    ) -> Any:
        adapter = object.__new__(self.module.FeishuAdapter)
        adapter.platform = self.module.Platform.FEISHU
        adapter._account_id = account_id
        adapter._namespace_account = bool(account_id)
        adapter._dm_policy = dm_policy
        adapter._allowed_group_users = set()
        adapter._group_policy = "open"
        adapter._default_group_policy = "open"
        adapter._group_rules = {}
        adapter._group_allow_from = set()
        adapter._admins = set()
        adapter._allow_bots = "mentions"
        adapter._bot_open_id = "ou_bot"
        adapter._bot_user_id = "u_bot"
        adapter._bot_name = "Hermes"
        adapter._require_mention = False
        adapter._require_mention_explicit = True
        adapter._respond_to_mention_all = False
        adapter._bot_loop_states = OrderedDict()
        adapter._chat_info_cache = {}
        adapter._thread_routes_by_message = OrderedDict()
        return adapter

    @staticmethod
    def _sender(open_id: str = "ou_user") -> Any:
        return SimpleNamespace(
            sender_type="user",
            sender_id=SimpleNamespace(
                open_id=open_id,
                user_id="u_user",
                union_id="on_user",
            ),
        )

    @staticmethod
    def _message(*, chat_type: str = "p2p") -> Any:
        return SimpleNamespace(
            message_id="om_message",
            chat_id="oc_chat",
            chat_type=chat_type,
            thread_id=None,
            root_id=None,
            create_time=str(int(time.time() * 1000)),
            mentions=[],
        )

    def test_dm_open_does_not_require_allow_from_wildcard(self) -> None:
        adapter = self._new_adapter(dm_policy="open")

        with patch.dict(
            os.environ,
            {
                "FEISHU_ALLOW_ALL_USERS": "",
                "GATEWAY_ALLOW_ALL_USERS": "",
            },
        ):
            reason = adapter._admit(self._sender(), self._message())

        self.assertIsNone(reason)
        self.assertEqual(adapter._allowed_group_users, set())

    def test_only_adapter_authorized_policies_stamp_role_grant(self) -> None:
        dm = self._message()
        group = self._message(chat_type="group")

        for policy in ("open", "allowlist"):
            with self.subTest(policy=policy):
                adapter = self._new_adapter(dm_policy=policy)
                self.assertTrue(
                    adapter._role_authorized_for_admitted_message(dm)
                )

        pairing = self._new_adapter(dm_policy="pairing")
        self.assertFalse(pairing._role_authorized_for_admitted_message(dm))
        self.assertTrue(pairing._role_authorized_for_admitted_message(group))

    def test_admission_result_is_forwarded_to_source_construction(self) -> None:
        async def exercise(policy: str, expected: bool) -> None:
            adapter = self._new_adapter(dm_policy=policy)
            if policy == "allowlist":
                adapter._allowed_group_users = {"ou_user"}
            adapter._is_duplicate = lambda message_id: False
            adapter._process_inbound_message = AsyncMock()
            data = SimpleNamespace(
                event=SimpleNamespace(
                    message=self._message(),
                    sender=self._sender(),
                )
            )

            await adapter._handle_message_event_data(data)

            self.assertEqual(
                adapter._process_inbound_message.await_args.kwargs[
                    "role_authorized"
                ],
                expected,
            )

        with patch.dict(
            os.environ,
            {
                "FEISHU_ALLOW_ALL_USERS": "",
                "GATEWAY_ALLOW_ALL_USERS": "",
            },
        ):
            asyncio.run(exercise("open", True))
            asyncio.run(exercise("allowlist", True))
            asyncio.run(exercise("pairing", False))

    def test_process_inbound_message_sets_session_source_role_grant(self) -> None:
        adapter = self._new_adapter(dm_policy="open")
        adapter._extract_message_content = AsyncMock(
            return_value=(
                "hello",
                self.module.MessageType.TEXT,
                [],
                [],
                [],
            )
        )
        adapter._fetch_message_text = AsyncMock(return_value=None)
        adapter.get_chat_info = AsyncMock(
            return_value={"name": "Direct chat", "type": "dm"}
        )
        adapter._resolve_sender_profile = AsyncMock(
            return_value={
                "user_id": "u_user",
                "user_name": "Alice",
                "user_id_alt": "on_user",
            }
        )
        adapter._resolve_channel_prompt = lambda chat_id, thread_id=None: None
        adapter._dispatch_inbound_event = AsyncMock()

        asyncio.run(
            adapter._process_inbound_message(
                data=SimpleNamespace(),
                message=self._message(),
                sender_id=self._sender().sender_id,
                chat_type="p2p",
                message_id="om_message",
                role_authorized=True,
            )
        )

        event = adapter._dispatch_inbound_event.await_args.args[0]
        self.assertTrue(event.source.role_authorized)
        self.assertEqual(event.source.user_id, "u_user")
        self.assertEqual(event.source.thread_id, "om_message")

    def test_dm_top_level_never_requires_a_mention(self) -> None:
        adapter = self._new_adapter(dm_policy="open")
        adapter._require_mention = True

        reason = adapter._admit(self._sender(), self._message())

        self.assertIsNone(reason)

    def test_group_thread_skips_mention_only_for_an_active_session(self) -> None:
        adapter = self._new_adapter()
        adapter._require_mention = True
        message = self._message(chat_type="group")
        message.thread_id = "omt_native"
        message.root_id = "om_root"

        class Store:
            """Minimal SessionStore contract used by admission."""

            def __init__(self, reset_reason: str | None) -> None:
                self._entries = {
                    "group:om_root": SimpleNamespace(suspended=False)
                }
                self.reset_reason = reset_reason

            def _ensure_loaded(self) -> None:
                return None

            def _generate_session_key(self, source: Any) -> str:
                return f"{source.chat_type}:{source.thread_id}"

            def _should_reset(self, _entry: Any, _source: Any) -> Any:
                return self.reset_reason

        adapter._session_store = Store(None)
        self.assertIsNone(adapter._admit(self._sender(), message))

        message.thread_id = None
        adapter._chat_info_cache["oc_chat"] = {
            "type": "group",
            "chat_mode": "topic",
        }
        self.assertIsNone(adapter._admit(self._sender(), message))

        message.thread_id = "omt_native"
        adapter._session_store = Store("idle")
        self.assertEqual(
            adapter._admit(self._sender(), message),
            "no_mention",
        )

        adapter._session_store = Store(None)
        adapter._session_store._entries.clear()
        self.assertEqual(
            adapter._admit(self._sender(), message),
            "no_mention",
        )

    def test_group_top_level_still_requires_a_mention(self) -> None:
        adapter = self._new_adapter()
        adapter._require_mention = False
        adapter._group_rules = {
            "*": self.module.FeishuGroupRule(require_mention=False)
        }
        adapter._session_store = SimpleNamespace(
            _ensure_loaded=lambda: None,
            _entries={"group:om_message": SimpleNamespace()},
            _generate_session_key=lambda source: (
                f"{source.chat_type}:{source.thread_id}"
            ),
        )

        reason = adapter._admit(
            self._sender(),
            self._message(chat_type="group"),
        )

        self.assertEqual(reason, "no_mention")

    def test_pairing_identity_is_scoped_per_account_and_keeps_raw_ids(self) -> None:
        account_a = self._new_adapter(dm_policy="pairing", account_id="a")
        account_b = self._new_adapter(dm_policy="pairing", account_id="b")

        source_a = account_a.build_source(
            "oc_a",
            chat_type="dm",
            user_id="u_same",
            user_id_alt="on_same",
            role_authorized=False,
        )
        source_b = account_b.build_source(
            "oc_b",
            chat_type="dm",
            user_id="u_same",
            user_id_alt="on_same",
            role_authorized=False,
        )

        self.assertEqual(source_a.user_id, "a::u_same")
        self.assertEqual(source_b.user_id, "b::u_same")
        self.assertEqual(source_a.feishu_user_id, "u_same")
        self.assertEqual(source_b.feishu_user_id, "u_same")
        self.assertEqual(source_a.feishu_user_id_alt, "on_same")
        self.assertEqual(source_b.feishu_user_id_alt, "on_same")
        self.assertFalse(source_a.role_authorized)
        self.assertFalse(source_b.role_authorized)

        approved_pairing_ids = {source_a.user_id}
        self.assertIn(source_a.user_id, approved_pairing_ids)
        self.assertNotIn(source_b.user_id, approved_pairing_ids)

    def test_yaml_policy_does_not_mutate_process_authorization_env(self) -> None:
        authorization_env = {
            "FEISHU_ALLOWED_USERS",
            "FEISHU_ALLOW_ALL_USERS",
            "FEISHU_GROUP_POLICY",
            "FEISHU_ALLOW_BOTS",
            "FEISHU_REQUIRE_MENTION",
        }
        feishu_config = {
            "dmPolicy": "open",
            "allowFrom": ["*"],
            "groupPolicy": "open",
            "allowBots": "all",
            "requireMention": False,
        }

        with patch.dict(os.environ, {}, clear=True):
            normalized = self.module._apply_yaml_config({}, feishu_config)

            self.assertTrue(authorization_env.isdisjoint(os.environ))

        self.assertEqual(normalized["dm_policy"], "open")
        self.assertEqual(normalized["allow_from"], ["*"])
        self.assertEqual(normalized["group_policy"], "open")

    def test_top_level_open_cannot_bypass_account_pairing_policy(self) -> None:
        config = {
            "dmPolicy": "open",
            "allowFrom": ["*"],
            "accounts": {
                "a": {
                    "appId": "cli_a",
                    "appSecret": "secret_a",
                },
                "b": {
                    "appId": "cli_b",
                    "appSecret": "secret_b",
                    "dmPolicy": "pairing",
                },
            },
        }

        with patch.dict(os.environ, {}, clear=True):
            normalized = self.module._apply_yaml_config({}, config)
            account_overrides = normalized["accounts"]["b"]
            account_extra = {
                **{key: value for key, value in normalized.items() if key != "accounts"},
                **account_overrides,
                "_account_id": "b",
                "_namespace_account": True,
            }
            config_type = sys.modules["gateway.config"].PlatformConfig
            account_b = self.module.FeishuAdapter(
                config_type(extra=account_extra)
            )
            source_b = account_b.build_source(
                "oc_b",
                chat_type="dm",
                user_id="u_same",
                user_id_alt="on_same",
                role_authorized=account_b._role_authorized_for_admitted_message(
                    self._message()
                ),
            )

            self.assertNotIn("FEISHU_ALLOW_ALL_USERS", os.environ)
            self.assertNotIn("FEISHU_ALLOWED_USERS", os.environ)

        self.assertEqual(account_b._dm_policy, "pairing")
        self.assertFalse(source_b.role_authorized)
        self.assertEqual(source_b.user_id, "b::u_same")


if __name__ == "__main__":
    unittest.main()
