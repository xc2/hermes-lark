"""Focused parity tests for Feishu bot-peer mention delivery."""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from types import SimpleNamespace
from typing import Any

from tests.test_ask_user_question_adapter import _MISSING_MODULE, _load_modules


class BotPeerMentionTests(unittest.TestCase):
    """Verify peer selection, mention de-duplication, and reply routing."""

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

    def tearDown(self) -> None:
        self.adapter_module._BOT_PEER_TURN_CONTEXT.set(None)

    def _new_adapter(
        self,
        *,
        account_id: str = "work",
        extra: dict[str, Any] | None = None,
    ) -> Any:
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter._account_id = account_id
        adapter._namespace_account = False
        adapter._client = object()
        adapter._bot_open_id = "ou_self"
        adapter._bot_user_id = ""
        adapter._bot_name = "SelfBot"
        adapter._group_rules = {}
        adapter._history_limit = 0
        adapter.config = self.adapter_module.PlatformConfig(extra=extra or {})
        adapter.platform = self.adapter_module.Platform.FEISHU
        adapter._reactions_enabled = lambda: False
        adapter.format_message = lambda content: content
        adapter.truncate_message = lambda content, _limit: [content]
        return adapter

    def _mention(
        self,
        open_id: str,
        name: str,
        *,
        is_self: bool = False,
    ) -> Any:
        return self.adapter_module.FeishuMentionRef(
            name=name,
            open_id=open_id,
            is_self=is_self,
        )

    def _event(
        self,
        *,
        peer_open_id: str,
        peer_name: str,
        chat_id: str = "oc_chat",
        thread_id: str = "",
        message_id: str = "om_origin",
        reply_to_message_id: str = "",
    ) -> Any:
        return SimpleNamespace(
            metadata={
                "feishu_bot_peer": {
                    "open_id": peer_open_id,
                    "name": peer_name,
                }
            },
            source=SimpleNamespace(
                chat_id=chat_id,
                thread_id=thread_id or None,
            ),
            message_id=message_id,
            reply_to_message_id=reply_to_message_id or None,
        )

    def _configure_send(self, adapter: Any) -> list[dict[str, Any]]:
        sent: list[dict[str, Any]] = []

        async def send_with_retry(**kwargs: Any) -> Any:
            sent.append(kwargs)
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id=f"om_sent_{len(sent)}"),
            )

        adapter._feishu_send_with_retry = send_with_retry
        adapter._finalize_send_result = lambda response, _message: (
            self.adapter_module.SendResult(
                success=bool(response and response.success()),
                message_id=(
                    getattr(getattr(response, "data", None), "message_id", None)
                    if response
                    else None
                ),
            )
        )
        return sent

    def test_peer_resolution_uses_only_sender_and_mention_open_ids(self) -> None:
        adapter = self._new_adapter()

        bot_peer = adapter._resolve_bot_peer_for_turn(
            is_group=True,
            is_bot_sender=True,
            sender_id=SimpleNamespace(open_id="ou_peer", user_id="u_peer"),
            sender_name="PeerBot",
            mentions=[],
            text="continue",
        )
        kickoff_peer = adapter._resolve_bot_peer_for_turn(
            is_group=True,
            is_bot_sender=False,
            sender_id=SimpleNamespace(open_id="ou_human"),
            sender_name="Human",
            mentions=[
                self._mention("ou_self", "SelfBot", is_self=True),
                self._mention("ou_peer", "PeerBot"),
                self._mention("ou_peer", "PeerBot"),
            ],
            text="Discuss this together",
        )
        name_only = adapter._resolve_bot_peer_for_turn(
            is_group=True,
            is_bot_sender=False,
            sender_id=SimpleNamespace(open_id="ou_human"),
            sender_name="Human",
            mentions=[self._mention("", "PeerBot")],
            text="Discuss this together",
        )
        ambiguous = adapter._resolve_bot_peer_for_turn(
            is_group=True,
            is_bot_sender=False,
            sender_id=SimpleNamespace(open_id="ou_human"),
            sender_name="Human",
            mentions=[
                self._mention("ou_a", "A"),
                self._mention("ou_b", "B"),
            ],
            text="Discuss this together",
        )

        self.assertEqual(
            bot_peer,
            {"open_id": "ou_peer", "name": "PeerBot"},
        )
        self.assertEqual(
            kickoff_peer,
            {"open_id": "ou_peer", "name": "PeerBot"},
        )
        self.assertIsNone(name_only)
        self.assertIsNone(ambiguous)

    def test_stop_intent_never_resolves_a_forced_peer(self) -> None:
        adapter = self._new_adapter()
        stop_phrases = (
            "\u4e2d\u65ad\u5bf9\u8bdd",
            "\u4f60\u4eec\u5148\u505c\u4e00\u4e0b",
            "\u522b\u804a\u4e86",
            "\u7ec8\u6b62\u8fa9\u8bba",
            "\u6253\u4f4f",
            "\u4e0d\u8981\u7ee7\u7eed\u4e86",
            "\u5230\u6b64\u4e3a\u6b62\u5427",
            "stop talking",
            "please stop the conversation",
            "knock it off",
            "stand down",
            "/stop",
        )

        for text in stop_phrases:
            with self.subTest(text=text):
                peer = adapter._resolve_bot_peer_for_turn(
                    is_group=True,
                    is_bot_sender=False,
                    sender_id=SimpleNamespace(open_id="ou_human"),
                    sender_name="Human",
                    mentions=[self._mention("ou_peer", "PeerBot")],
                    text=text,
                )
                self.assertIsNone(peer)

        for text in (
            "\u8bf7\u7ee7\u7eed\u8fa9\u8bba",
            "\u6211\u4eec\u505c\u8f66\u573a\u89c1\u9762\u5427",
            "\u8ba8\u8bba\u7ee7\u7eed\u8fdb\u884c",
        ):
            with self.subTest(normal=text):
                self.assertIsNotNone(
                    adapter._resolve_bot_peer_for_turn(
                        is_group=True,
                        is_bot_sender=False,
                        sender_id=SimpleNamespace(open_id="ou_human"),
                        sender_name="Human",
                        mentions=[self._mention("ou_peer", "PeerBot")],
                        text=text,
                    )
                )

    def test_first_chunk_mentions_once_and_existing_at_is_idempotent(self) -> None:
        asyncio.run(self._exercise_chunk_dedup())

    async def _exercise_chunk_dedup(self) -> None:
        adapter = self._new_adapter()
        sent = self._configure_send(adapter)
        event = self._event(peer_open_id="ou_peer", peer_name="PeerBot")

        await adapter.on_processing_start(event)
        await adapter.send("oc_chat", "First segment", reply_to="om_origin")
        await adapter.send("oc_chat", "Second segment", reply_to="om_origin")
        await adapter.on_processing_complete(
            event,
            self.adapter_module.ProcessingOutcome.SUCCESS,
        )

        first = json.loads(sent[0]["payload"])["text"]
        second = json.loads(sent[1]["payload"])["text"]
        self.assertEqual(
            first,
            '<at user_id="ou_peer">PeerBot</at> First segment',
        )
        self.assertEqual(second, "Second segment")

        sent.clear()
        second_event = self._event(
            peer_open_id="ou_peer",
            peer_name="PeerBot",
            message_id="om_second",
        )
        await adapter.on_processing_start(second_event)
        await adapter.send(
            "oc_chat",
            'Received <at user_id="ou_peer">Any display name</at>, continue',
            reply_to="om_second",
        )
        existing = json.loads(sent[0]["payload"])["text"]
        self.assertEqual(existing.count('<at user_id="ou_peer">'), 1)

    def test_stream_edit_keeps_the_injected_mention_in_its_message(self) -> None:
        asyncio.run(self._exercise_stream_edit())

    async def _exercise_stream_edit(self) -> None:
        adapter = self._new_adapter()
        self._configure_send(adapter)
        event = self._event(peer_open_id="ou_peer", peer_name="PeerBot")
        await adapter.on_processing_start(event)
        await adapter.send("oc_chat", "Preview", reply_to="om_origin")

        update_method = object()
        adapter._client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(
                    message=SimpleNamespace(update=update_method),
                )
            )
        )
        updates: list[dict[str, Any]] = []
        adapter._build_update_message_body = lambda **kwargs: kwargs
        adapter._build_update_message_request = (
            lambda message_id, request_body: {
                "message_id": message_id,
                **request_body,
            }
        )

        async def run_blocking(method: Any, request: dict[str, Any]) -> Any:
            self.assertIs(method, update_method)
            updates.append(request)
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id="om_sent_1"),
            )

        adapter._run_blocking = run_blocking
        await adapter.edit_message(
            "oc_chat",
            "om_sent_1",
            "Expanded preview",
            metadata={"reply_to_message_id": "om_origin"},
        )

        edited = json.loads(updates[0]["content"])["text"]
        self.assertEqual(
            edited,
            '<at user_id="ou_peer">PeerBot</at> Expanded preview',
        )

    def test_context_isolated_by_chat_thread_account_and_cleaned_up(self) -> None:
        asyncio.run(self._exercise_context_isolation())

    async def _exercise_context_isolation(self) -> None:
        adapter = self._new_adapter()
        sent = self._configure_send(adapter)

        async def one_turn(thread_id: str, peer_open_id: str) -> None:
            event = self._event(
                peer_open_id=peer_open_id,
                peer_name=peer_open_id,
                thread_id=thread_id,
                message_id=f"om_{thread_id}",
                reply_to_message_id=f"root_{thread_id}",
            )
            await adapter.on_processing_start(event)
            await asyncio.sleep(0)
            await adapter.send(
                "oc_chat",
                f"reply-{thread_id}",
                reply_to=f"root_{thread_id}",
                metadata={"thread_id": thread_id},
            )
            await adapter.on_processing_complete(
                event,
                self.adapter_module.ProcessingOutcome.SUCCESS,
            )

        await asyncio.gather(
            one_turn("omt_a", "ou_a"),
            one_turn("omt_b", "ou_b"),
        )

        delivered = {
            kwargs["metadata"]["thread_id"]: json.loads(kwargs["payload"])["text"]
            for kwargs in sent
        }
        self.assertTrue(delivered["omt_a"].startswith('<at user_id="ou_a">'))
        self.assertTrue(delivered["omt_b"].startswith('<at user_id="ou_b">'))

        sent.clear()
        await adapter.send(
            "oc_chat",
            "cron",
            reply_to="root_omt_a",
            metadata={"thread_id": "omt_a"},
        )
        self.assertEqual(json.loads(sent[0]["payload"])["text"], "cron")

        event = self._event(
            peer_open_id="ou_work",
            peer_name="Work",
            message_id="om_work",
        )
        await adapter.on_processing_start(event)
        other_account = self._new_adapter(account_id="personal")
        other_sent = self._configure_send(other_account)
        await other_account.send("oc_chat", "personal", reply_to="om_work")
        self.assertEqual(
            json.loads(other_sent[0]["payload"])["text"],
            "personal",
        )

    def test_inbound_bot_uses_the_same_canonical_thread_session(self) -> None:
        asyncio.run(self._exercise_inbound_thread_routing())

    async def _exercise_inbound_thread_routing(self) -> None:
        adapter = self._new_adapter()
        captured: list[Any] = []
        adapter._extract_message_content = self._async_value(
            (
                "Continue the discussion",
                self.adapter_module.MessageType.TEXT,
                [],
                [],
                [self._mention("ou_self", "SelfBot", is_self=True)],
            )
        )
        adapter._fetch_message_text = self._async_value(None)
        adapter.get_chat_info = self._async_value(
            {"name": "Group", "type": "group"}
        )
        adapter._resolve_sender_profile = self._async_value(
            {
                "user_id": "u_peer",
                "user_name": "PeerBot",
                "user_id_alt": None,
            }
        )
        adapter._resolve_channel_prompt = lambda *_args: None

        async def dispatch(event: Any) -> None:
            captured.append(event)

        adapter._dispatch_inbound_event = dispatch
        message = SimpleNamespace(
            chat_id="oc_chat",
            chat_type="group",
            thread_id="omt_hidden",
            root_id="om_root",
            parent_id=None,
            upper_message_id=None,
        )
        await adapter._process_inbound_message(
            data=SimpleNamespace(),
            message=message,
            sender_id=SimpleNamespace(
                open_id="ou_peer",
                user_id="u_peer",
                union_id=None,
            ),
            chat_type="group",
            message_id="om_inbound",
            is_bot=True,
        )

        event = captured[0]
        self.assertEqual(event.source.thread_id, "om_root")
        self.assertEqual(event.source.feishu_thread_id, "omt_hidden")
        self.assertEqual(
            event.metadata["feishu_session_thread_id"],
            "om_root",
        )
        self.assertEqual(
            event.metadata["feishu_bot_peer"],
            {"open_id": "ou_peer", "name": "PeerBot"},
        )

    @staticmethod
    def _async_value(value: Any) -> Any:
        async def resolve(*_args: Any, **_kwargs: Any) -> Any:
            return value

        return resolve


if __name__ == "__main__":
    unittest.main()
