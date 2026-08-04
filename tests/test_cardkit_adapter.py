"""Focused transport tests for Feishu CardKit conversational streaming."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tests.test_ask_user_question_adapter import _MISSING_MODULE, _load_modules


class CardKitAdapterTests(unittest.TestCase):
    """Verify one CardKit entity owns status, tools, stream, and completion."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tools, cls.adapter_module, cls.previous_modules = _load_modules()

    @classmethod
    def tearDownClass(cls) -> None:
        for name, previous in cls.previous_modules.items():
            if previous is _MISSING_MODULE:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    def _adapter(self) -> tuple[Any, list[tuple[Any, ...]]]:
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter._client = object()
        adapter._account_id = "default"
        adapter._namespace_account = False
        adapter._profile_scope_key = "profile"
        adapter._cardkit_config = {"streaming": True, "replyMode": "streaming"}
        adapter._cardkit_trace_path = ""
        adapter._cardkit_states_by_route = {}
        adapter._cardkit_states_by_message = {}
        adapter._thread_routes_by_message = __import__("collections").OrderedDict()
        adapter._text_chunk_limit = 4000
        adapter._chunk_mode = "none"
        adapter.format_message = lambda content: content
        adapter.truncate_message = lambda content, _limit: [content]
        adapter._drive_comment_target = lambda *_args, **_kwargs: None

        calls: list[tuple[Any, ...]] = []

        async def create(card: dict[str, Any]) -> Any:
            calls.append(("create", card))
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(card_id="card-1"),
            )

        async def send_with_retry(**kwargs: Any) -> Any:
            calls.append(("send", kwargs))
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id="om_card"),
            )

        async def content(state: Any, text: str, sequence: int) -> Any:
            calls.append(("content", text, sequence))
            return SimpleNamespace(success=lambda: True, data=SimpleNamespace())

        async def settings(state: Any, streaming_mode: bool, sequence: int) -> Any:
            calls.append(("settings", streaming_mode, sequence))
            return SimpleNamespace(success=lambda: True, data=SimpleNamespace())

        async def update(state: Any, card: dict[str, Any], sequence: int) -> Any:
            calls.append(("update", card, sequence))
            return SimpleNamespace(success=lambda: True, data=SimpleNamespace())

        adapter._cardkit_create = create
        adapter._feishu_send_with_retry = send_with_retry
        adapter._cardkit_content = content
        adapter._cardkit_settings = settings
        adapter._cardkit_update = update
        return adapter, calls

    @staticmethod
    def _event() -> Any:
        return SimpleNamespace(
            metadata={},
            source=SimpleNamespace(
                chat_id="oc_chat",
                chat_type="dm",
                thread_id="om_root",
            ),
            message_id="om_root",
            reply_to_message_id=None,
        )

    def test_start_stream_and_finalize_reuse_one_card_message(self) -> None:
        adapter, calls = self._adapter()

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())
            self.assertIsNotNone(state)
            self.assertEqual(state.card_id, "card-1")
            self.assertEqual(state.message_id, "om_card")

            first = await adapter.send(
                "oc_chat",
                "first",
                reply_to="om_root",
                metadata={"thread_id": "om_root", "expect_edits": True},
            )
            second = await adapter.edit_message(
                "oc_chat",
                "om_card",
                "first second",
                metadata={"thread_id": "om_root"},
            )
            state.turn_terminal = True
            final = await adapter.edit_message(
                "oc_chat",
                "om_card",
                "first second final",
                finalize=True,
                metadata={"thread_id": "om_root"},
            )

            self.assertEqual(first.message_id, "om_card")
            self.assertEqual(second.message_id, "om_card")
            self.assertEqual(final.message_id, "om_card")
            self.assertTrue(state.closed)

        asyncio.run(scenario())

        send_call = next(call for call in calls if call[0] == "send")
        self.assertEqual(send_call[1]["msg_type"], "interactive")
        self.assertEqual(send_call[1]["reply_to"], "om_root")
        self.assertEqual(send_call[1]["metadata"]["thread_id"], "om_root")
        self.assertEqual(
            json.loads(send_call[1]["payload"]),
            {"type": "card", "data": {"card_id": "card-1"}},
        )
        sequenced = [
            call[-1]
            for call in calls
            if call[0] in {"content", "settings", "update"}
        ]
        self.assertEqual(sequenced, sorted(set(sequenced)))
        self.assertEqual(
            [call[1] for call in calls if call[0] == "content"],
            ["first second final"],
        )
        settings_call = next(call for call in calls if call[0] == "settings")
        self.assertFalse(settings_call[1])
        update_call = [call for call in calls if call[0] == "update"][-1]
        self.assertIn("Complete", json.dumps(update_call[1], ensure_ascii=False))
        self.assertNotIn("loading", json.dumps(update_call[1], ensure_ascii=False))
        self.assertEqual(
            update_call[1]["config"]["summary"]["i18n_content"],
            {"zh_cn": "Complete", "en_us": "Complete"},
        )
        self.assertEqual(
            update_call[1]["body"]["elements"][0]["i18n_content"],
            {"zh_cn": "✅ **Complete**", "en_us": "✅ **Complete**"},
        )

    def test_tool_lifecycle_updates_the_active_conversation_card(self) -> None:
        adapter, calls = self._adapter()
        ticket = self.tools.ToolTicket(
            session_id="session-1",
            message_id="om_root",
            chat_id="oc_chat",
            account_id="default",
            profile_scope="profile",
            chat_type="p2p",
            session_thread_id="om_root",
        )

        async def scenario() -> None:
            await adapter._start_cardkit_turn(self._event())
            self.assertTrue(
                await adapter._update_cardkit_tool_for_ticket(
                    ticket,
                    tool_name="terminal",
                    tool_call_id="call-1",
                    status="running",
                )
            )
            self.assertTrue(
                await adapter._update_cardkit_tool_for_ticket(
                    ticket,
                    tool_name="terminal",
                    tool_call_id="call-1",
                    status="ok",
                )
            )

        asyncio.run(scenario())

        updates = [call for call in calls if call[0] == "update"]
        self.assertEqual(len(updates), 2)
        running = json.dumps(updates[0][1], ensure_ascii=False)
        complete = json.dumps(updates[1][1], ensure_ascii=False)
        self.assertIn("terminal", running)
        self.assertIn("Running", running)
        self.assertIn("Succeeded", complete)

    def test_notify_send_does_not_finalize_an_active_conversation_card(self) -> None:
        adapter, calls = self._adapter()
        adapter._finalize_send_result = lambda *_args: SimpleNamespace(
            success=True,
            message_id="om_notify",
        )

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())
            result = await adapter.send(
                "oc_chat",
                "Command denied.",
                reply_to="om_root",
                metadata={"thread_id": "om_root", "notify": True},
            )

            self.assertTrue(result.success)
            self.assertFalse(state.closed)

        asyncio.run(scenario())

        sends = [call for call in calls if call[0] == "send"]
        self.assertEqual(len(sends), 2)
        self.assertEqual(sends[-1][1]["msg_type"], "text")
        self.assertFalse(any(call[0] == "settings" for call in calls))
        self.assertFalse(any(call[0] == "update" for call in calls))

    def test_progress_sends_update_the_active_card_without_plain_messages(self) -> None:
        adapter, calls = self._adapter()
        adapter._cardkit_stream_throttle_seconds = 0.001
        adapter._finalize_send_result = lambda *_args: SimpleNamespace(
            success=True,
            message_id="om_plain",
        )
        ticket = self.tools.ToolTicket(
            session_id="session-1",
            message_id="om_root",
            chat_id="oc_chat",
            account_id="default",
            profile_scope="profile",
            chat_type="p2p",
            session_thread_id="om_root",
        )

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())
            token = self.adapter_module._CARDKIT_PROGRESS_DELIVERY_CONTEXT.set(
                "commentary"
            )
            try:
                first = await adapter.send(
                    "oc_chat",
                    "Checking GitHub authentication.",
                    metadata={"thread_id": "om_root"},
                )
                second = await adapter.send(
                    "oc_chat",
                    "Reading the issue and production errors.",
                    metadata={"thread_id": "om_root"},
                )
            finally:
                self.adapter_module._CARDKIT_PROGRESS_DELIVERY_CONTEXT.reset(
                    token
                )
            old_heartbeat = await adapter.send(
                "oc_chat",
                "⏳ Working — 2 min — iteration 5/60",
                metadata={"thread_id": "om_root"},
            )
            latest_heartbeat = await adapter.send(
                "oc_chat",
                "⏳ Working — 3 min — iteration 7/60",
                metadata={"thread_id": "om_root"},
            )
            self.assertTrue(
                await adapter._update_cardkit_tool_for_ticket(
                    ticket,
                    tool_name="terminal",
                    tool_call_id="call-1",
                    status="running",
                )
            )
            await asyncio.sleep(0.03)

            self.assertTrue(first.success)
            self.assertTrue(second.success)
            self.assertTrue(old_heartbeat.success)
            self.assertTrue(latest_heartbeat.success)
            self.assertFalse(first.message_id)
            self.assertFalse(second.message_id)
            self.assertFalse(old_heartbeat.message_id)
            self.assertFalse(latest_heartbeat.message_id)
            self.assertFalse(state.closed)
            self.assertEqual(state.content, "")
            self.assertEqual(
                state.progress_content,
                (
                    "Checking GitHub authentication.\n\n"
                    "Reading the issue and production errors."
                ),
            )
            self.assertEqual(
                state.heartbeat_content,
                "⏳ Working — 3 min — iteration 7/60",
            )

            streamed = await adapter.send(
                "oc_chat",
                "Final answer",
                metadata={
                    "thread_id": "om_root",
                    "expect_edits": True,
                },
            )
            state.turn_terminal = True
            finalized = await adapter.edit_message(
                "oc_chat",
                "om_card",
                "Final answer",
                finalize=True,
                metadata={"thread_id": "om_root"},
            )

            self.assertTrue(streamed.success)
            self.assertTrue(finalized.success)
            self.assertEqual(state.content, "Final answer")

        asyncio.run(scenario())

        sends = [call for call in calls if call[0] == "send"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][1]["msg_type"], "interactive")
        generating_cards = [
            call[1]
            for call in calls
            if call[0] == "update"
            and call[1]["config"]["summary"]["content"] == "Generating..."
        ]
        generating_json = json.dumps(generating_cards[-1], ensure_ascii=False)
        self.assertIn("Checking GitHub authentication.", generating_json)
        self.assertIn("Reading the issue and production errors.", generating_json)
        self.assertIn("⏳ Working — 3 min — iteration 7/60", generating_json)
        self.assertIn("terminal", generating_json)
        self.assertNotIn(
            "⏳ Working — 2 min — iteration 5/60",
            generating_json,
        )
        terminal_card = [call for call in calls if call[0] == "update"][-1][1]
        terminal_json = json.dumps(terminal_card, ensure_ascii=False)
        self.assertIn("Final answer", terminal_json)
        self.assertNotIn("Checking GitHub authentication.", terminal_json)
        self.assertNotIn("⏳ Working", terminal_json)

    def test_progress_only_silent_reply_finishes_as_done(self) -> None:
        adapter, calls = self._adapter()
        adapter._cardkit_stream_throttle_seconds = 0.001

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())
            token = self.adapter_module._CARDKIT_PROGRESS_DELIVERY_CONTEXT.set(
                "commentary"
            )
            try:
                await adapter.send(
                    "oc_chat",
                    "Still checking the source.",
                    metadata={"thread_id": "om_root"},
                )
            finally:
                self.adapter_module._CARDKIT_PROGRESS_DELIVERY_CONTEXT.reset(
                    token
                )
            await asyncio.sleep(0.03)
            state.turn_terminal = True
            result = await adapter._finalize_cardkit(state, "NO_REPLY")

            self.assertTrue(result.success)
            self.assertEqual(state.last_flushed_content, "Done.")

        asyncio.run(scenario())

        terminal_card = [call for call in calls if call[0] == "update"][-1][1]
        terminal_json = json.dumps(terminal_card, ensure_ascii=False)
        self.assertIn("Done.", terminal_json)
        self.assertNotIn("Still checking the source.", terminal_json)

    def test_progress_does_not_consume_the_bot_peer_final_mention(self) -> None:
        adapter, _calls = self._adapter()

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())
            turn = self.adapter_module.FeishuBotPeerTurn(
                account_id="default",
                chat_id="oc_chat",
                thread_id="om_root",
                reply_anchors=frozenset({"om_root"}),
                peer_open_id="ou_peer",
                peer_name="Peer Bot",
            )
            turn_token = self.adapter_module._BOT_PEER_TURN_CONTEXT.set(turn)
            try:
                progress_token = (
                    self.adapter_module._CARDKIT_PROGRESS_DELIVERY_CONTEXT.set(
                        "commentary"
                    )
                )
                try:
                    await adapter.send(
                        "oc_chat",
                        "Checking now.",
                        metadata={
                            "thread_id": "om_root",
                            "reply_to_message_id": "om_root",
                        },
                    )
                finally:
                    self.adapter_module._CARDKIT_PROGRESS_DELIVERY_CONTEXT.reset(
                        progress_token
                    )
                self.assertFalse(turn.mentioned)
                await adapter.send(
                    "oc_chat",
                    "Final answer",
                    metadata={
                        "thread_id": "om_root",
                        "reply_to_message_id": "om_root",
                        "expect_edits": True,
                    },
                )
            finally:
                self.adapter_module._BOT_PEER_TURN_CONTEXT.reset(turn_token)

            self.assertTrue(turn.mentioned)
            self.assertIn("om_card", turn.mentioned_message_ids)
            self.assertTrue(
                state.content.startswith(
                    '<at user_id="ou_peer">Peer Bot</at> '
                )
            )

        asyncio.run(scenario())

    def test_plain_turn_message_is_not_absorbed_as_card_progress(self) -> None:
        adapter, calls = self._adapter()
        adapter._finalize_send_result = lambda *_args: SimpleNamespace(
            success=True,
            message_id="om_clarify",
        )

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())
            result = await adapter.send(
                "oc_chat",
                "Which environment should I inspect?",
                metadata={"thread_id": "om_root"},
            )

            self.assertTrue(result.success)
            self.assertEqual(result.message_id, "om_clarify")
            self.assertEqual(state.progress_content, "")
            self.assertEqual(state.heartbeat_content, "")

        asyncio.run(scenario())

        sends = [call for call in calls if call[0] == "send"]
        self.assertEqual(len(sends), 2)
        self.assertEqual(sends[-1][1]["msg_type"], "text")

    def test_terminal_plain_send_is_not_absorbed_as_card_progress(self) -> None:
        adapter, calls = self._adapter()
        adapter._finalize_send_result = lambda *_args: SimpleNamespace(
            success=True,
            message_id="om_final_fallback",
        )

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())
            state.turn_terminal = True
            result = await adapter.send(
                "oc_chat",
                "Final fallback",
                metadata={"thread_id": "om_root"},
            )

            self.assertTrue(result.success)
            self.assertEqual(result.message_id, "om_final_fallback")
            self.assertEqual(state.progress_content, "")
            self.assertEqual(state.heartbeat_content, "")

        asyncio.run(scenario())

        sends = [call for call in calls if call[0] == "send"]
        self.assertEqual(len(sends), 2)
        self.assertEqual(sends[-1][1]["msg_type"], "text")

    def test_marked_progress_drains_after_terminal_signal_without_late_bubbles(
        self,
    ) -> None:
        adapter, calls = self._adapter()

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())
            state.turn_terminal = True
            token = self.adapter_module._CARDKIT_PROGRESS_DELIVERY_CONTEXT.set(
                "commentary"
            )
            try:
                draining = await adapter.send(
                    "oc_chat",
                    "Draining queued commentary.",
                    metadata={"thread_id": "om_root"},
                )
                state.closed = True
                late = await adapter.send(
                    "oc_chat",
                    "Already closed commentary.",
                    metadata={"thread_id": "om_root"},
                )
            finally:
                self.adapter_module._CARDKIT_PROGRESS_DELIVERY_CONTEXT.reset(
                    token
                )

            self.assertTrue(draining.success)
            self.assertTrue(late.success)
            self.assertFalse(draining.message_id)
            self.assertFalse(late.message_id)
            self.assertEqual(
                state.progress_content,
                "Draining queued commentary.",
            )

        asyncio.run(scenario())

        sends = [call for call in calls if call[0] == "send"]
        self.assertEqual(len(sends), 1)

    def test_card_commentary_does_not_suppress_the_matching_final_answer(
        self,
    ) -> None:
        adapter, calls = self._adapter()
        adapter._finalize_send_result = lambda *_args: SimpleNamespace(
            success=True,
            message_id="om_plain",
        )
        observed: list[str] = []

        class GatewayStreamConsumer:
            """Minimal Hermes consumer exposing its commentary send seam."""

            def __init__(self) -> None:
                self.adapter = adapter
                self.chat_id = "oc_chat"
                self.metadata = {"thread_id": "om_root"}
                self._delivered_commentary_texts: list[str] = []

            async def _send_commentary(self, text: str) -> bool:
                observed.append(
                    self_adapter._CARDKIT_PROGRESS_DELIVERY_CONTEXT.get()
                )
                result = await self.adapter.send(
                    self.chat_id,
                    text,
                    metadata=self.metadata,
                )
                if result.success:
                    self._delivered_commentary_texts.append(text)
                return result.success

        self_adapter = self.adapter_module
        module = types.ModuleType("gateway.stream_consumer")
        module.GatewayStreamConsumer = GatewayStreamConsumer
        previous = sys.modules.get("gateway.stream_consumer", _MISSING_MODULE)
        sys.modules["gateway.stream_consumer"] = module
        try:
            self.adapter_module._install_cardkit_commentary_bridge()
            installed = GatewayStreamConsumer._send_commentary
            self.adapter_module._install_cardkit_commentary_bridge()
            self.assertIs(GatewayStreamConsumer._send_commentary, installed)

            async def scenario() -> tuple[bool, list[str]]:
                state = await adapter._start_cardkit_turn(self._event())
                consumer = GatewayStreamConsumer()
                commentary_sent = await consumer._send_commentary(
                    "Final answer"
                )
                self.assertEqual(consumer._delivered_commentary_texts, [])
                self.assertEqual(state.content, "")
                self.assertEqual(state.progress_content, "Final answer")

                state.turn_terminal = True
                final = await adapter.send(
                    "oc_chat",
                    "Final answer",
                    reply_to="om_root",
                    metadata={
                        "thread_id": "om_root",
                        "notify": True,
                    },
                )
                self.assertTrue(final.success)
                self.assertEqual(final.message_id, "om_card")
                self.assertEqual(state.content, "Final answer")
                self.assertFalse(state.closed)
                attachment_notice = await adapter.send(
                    "oc_chat",
                    "⚠️ Couldn't deliver the file attachment.",
                    metadata={
                        "thread_id": "om_root",
                        "notify": True,
                    },
                )
                self.assertTrue(attachment_notice.success)
                self.assertEqual(attachment_notice.message_id, "om_plain")
                self.assertEqual(state.content, "Final answer")
                await adapter._finalize_cardkit(
                    state,
                    state.content,
                )
                adapter._forget_cardkit_turn(state)
                await consumer._send_commentary("Ordinary commentary")
                return commentary_sent, consumer._delivered_commentary_texts

            result, delivered = asyncio.run(scenario())
        finally:
            if previous is _MISSING_MODULE:
                sys.modules.pop("gateway.stream_consumer", None)
            else:
                sys.modules["gateway.stream_consumer"] = previous

        self.assertTrue(result)
        self.assertEqual(delivered, ["Ordinary commentary"])
        self.assertEqual(observed, ["commentary", "commentary"])
        self.assertEqual(
            self.adapter_module._CARDKIT_PROGRESS_DELIVERY_CONTEXT.get(),
            "",
        )
        self.assertFalse(
            self.adapter_module._CARDKIT_PROGRESS_CAPTURED_CONTEXT.get()
        )
        terminal_card = [call for call in calls if call[0] == "update"][-1][1]
        terminal_json = json.dumps(terminal_card, ensure_ascii=False)
        self.assertIn("Final answer", terminal_json)
        self.assertNotIn("Done.", terminal_json)

    def test_rapid_partials_coalesce_to_the_latest_cumulative_content(self) -> None:
        adapter, calls = self._adapter()
        adapter._cardkit_stream_throttle_seconds = 0.005

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())
            await adapter._stream_cardkit_content(state, "one")
            await adapter._stream_cardkit_content(state, "one two")
            await adapter._stream_cardkit_content(state, "one two three")
            await asyncio.sleep(0.03)
            self.assertEqual(state.last_flushed_content, "one two three")
            await adapter._finalize_cardkit(state, state.content)

        asyncio.run(scenario())

        self.assertEqual(
            [call[1] for call in calls if call[0] == "content"],
            ["one two three"],
        )

    def test_rate_limited_stream_retries_the_latest_cumulative_content(self) -> None:
        adapter, calls = self._adapter()
        adapter._cardkit_stream_throttle_seconds = 0.001
        adapter._cardkit_rate_limit_backoff_seconds = 0.001
        attempts = 0

        async def rate_limited_content(
            state: Any,
            text: str,
            sequence: int,
        ) -> Any:
            nonlocal attempts
            attempts += 1
            calls.append(("content", text, sequence))
            if attempts == 1:
                return SimpleNamespace(
                    success=lambda: False,
                    code=230020,
                    msg="rate limited",
                )
            return SimpleNamespace(success=lambda: True, data=SimpleNamespace())

        adapter._cardkit_content = rate_limited_content

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())
            await adapter._stream_cardkit_content(state, "latest cumulative")
            await asyncio.sleep(0.03)
            self.assertEqual(state.last_flushed_content, "latest cumulative")
            self.assertEqual(state.stream_retry_count, 0)
            await adapter._finalize_cardkit(state, state.content)

        asyncio.run(scenario())

        self.assertEqual(attempts, 2)
        self.assertEqual(
            [call[1] for call in calls if call[0] == "content"],
            ["latest cumulative", "latest cumulative"],
        )

    def test_remote_image_resolution_reflushes_without_url_leakage(self) -> None:
        adapter, calls = self._adapter()
        adapter._cardkit_stream_throttle_seconds = 0.001
        image_url = "https://cdn.example.test/card.png?secret=private"
        source = f"before ![chart]({image_url}) after"
        events: dict[str, asyncio.Event] = {}
        uploads: list[str] = []

        async def upload(url: str) -> str:
            """Delay resolution until the stripped streaming frame is written."""
            uploads.append(url)
            events["started"].set()
            await events["release"].wait()
            return "img_cardkit"

        adapter._upload_cardkit_image_url = upload

        async def scenario(trace_path: Path) -> None:
            events["started"] = asyncio.Event()
            events["release"] = asyncio.Event()
            adapter._cardkit_trace_path = str(trace_path)
            state = await adapter._start_cardkit_turn(self._event())
            await adapter._stream_cardkit_content(state, source)
            await events["started"].wait()

            for _ in range(100):
                visible_writes = [
                    call[1]
                    for call in calls
                    if call[0] == "content"
                ]
                if visible_writes:
                    break
                await asyncio.sleep(0.002)
            self.assertEqual(visible_writes, ["before  after"])
            self.assertNotIn(image_url, visible_writes[0])

            events["release"].set()
            for _ in range(100):
                visible_writes = [
                    call[1]
                    for call in calls
                    if call[0] == "content"
                ]
                if any("img_cardkit" in text for text in visible_writes):
                    break
                await asyncio.sleep(0.002)
            self.assertEqual(uploads, [image_url])
            self.assertIn("![chart](img_cardkit)", visible_writes[-1])
            self.assertNotIn(image_url, visible_writes[-1])

            result = await adapter._finalize_cardkit(state, state.content)
            self.assertTrue(result.success)

        with tempfile.TemporaryDirectory() as directory:
            trace_path = Path(directory) / "cardkit.jsonl"
            asyncio.run(scenario(trace_path))
            trace_text = trace_path.read_text(encoding="utf-8")

        self.assertNotIn(image_url, trace_text)
        self.assertIn("img_cardkit", trace_text)
        self.assertNotIn(image_url, json.dumps(calls, default=str))

    def test_terminal_image_resolution_uses_the_upstream_fifteen_second_wait(
        self,
    ) -> None:
        adapter, calls = self._adapter()
        observed_timeouts: list[float] = []

        class TerminalResolver:
            """Record the timeout passed by terminal CardKit finalization."""

            async def resolve_images_await(
                self,
                content: str,
                *,
                timeout_seconds: float,
            ) -> str:
                observed_timeouts.append(timeout_seconds)
                return f"{content} ![ready](img_terminal)"

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())
            state.image_resolver = TerminalResolver()
            result = await adapter._finalize_cardkit(state, "answer")
            self.assertTrue(result.success)

        asyncio.run(scenario())

        self.assertEqual(observed_timeouts, [15.0])
        terminal_card = [call for call in calls if call[0] == "update"][-1][1]
        self.assertIn("img_terminal", json.dumps(terminal_card))

    def test_unavailable_card_stops_all_future_stream_writes(self) -> None:
        adapter, calls = self._adapter()
        adapter._cardkit_stream_throttle_seconds = 0.001

        async def unavailable_content(
            state: Any,
            text: str,
            sequence: int,
        ) -> Any:
            calls.append(("content", text, sequence))
            return SimpleNamespace(
                success=lambda: False,
                code=230011,
                msg="message recalled",
            )

        adapter._cardkit_content = unavailable_content

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())
            first = await adapter._stream_cardkit_content(state, "first")
            self.assertTrue(first.success)
            await asyncio.sleep(0.02)
            self.assertTrue(state.closed)
            self.assertTrue(state.unavailable)
            second = await adapter.send(
                "oc_chat",
                "second",
                reply_to="om_root",
                metadata={"thread_id": "om_root", "expect_edits": True},
            )
            self.assertFalse(second.success)

        asyncio.run(scenario())

        self.assertEqual(
            [call[1] for call in calls if call[0] == "content"],
            ["first"],
        )

    def test_terminal_retry_hides_silent_reply_and_closes_streaming(self) -> None:
        adapter, calls = self._adapter()
        adapter._cardkit_terminal_retry_base_seconds = 0
        settings_attempts = 0

        async def rate_limited_settings(
            state: Any,
            streaming_mode: bool,
            sequence: int,
        ) -> Any:
            nonlocal settings_attempts
            settings_attempts += 1
            calls.append(("settings", streaming_mode, sequence))
            if settings_attempts == 1:
                return SimpleNamespace(
                    success=lambda: False,
                    code=230020,
                    msg="rate limited",
                )
            return SimpleNamespace(success=lambda: True, data=SimpleNamespace())

        adapter._cardkit_settings = rate_limited_settings

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())
            await adapter._stream_cardkit_content(state, "NO_REPLY")
            result = await adapter._finalize_cardkit(state, state.content)
            self.assertTrue(result.success)
            self.assertTrue(state.closed)
            self.assertEqual(state.phase, "complete")
            self.assertEqual(state.content, "Done.")

        asyncio.run(scenario())

        self.assertEqual(settings_attempts, 2)
        content_calls = [call for call in calls if call[0] == "content"]
        self.assertEqual([call[1] for call in content_calls], ["Done."])
        terminal_card = [call for call in calls if call[0] == "update"][-1][1]
        terminal_json = json.dumps(terminal_card, ensure_ascii=False)
        self.assertIn("Done.", terminal_json)
        self.assertNotIn("NO_REPLY", terminal_json)

    def test_shutdown_finalizes_active_cards_as_stopped(self) -> None:
        adapter, calls = self._adapter()

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())
            await adapter._stream_cardkit_content(state, "partial answer")
            await adapter._finalize_open_cardkit_turns()
            self.assertTrue(state.closed)
            self.assertEqual(state.phase, "stopped")
            self.assertEqual(adapter._cardkit_states_by_route, {})
            self.assertEqual(adapter._cardkit_states_by_message, {})

        asyncio.run(scenario())

        terminal_card = [call for call in calls if call[0] == "update"][-1][1]
        self.assertIn("Stopped", json.dumps(terminal_card, ensure_ascii=False))
        self.assertFalse(terminal_card["config"]["streaming_mode"])


if __name__ == "__main__":
    unittest.main()
