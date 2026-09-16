"""Focused transport tests for Feishu CardKit conversational streaming."""

from __future__ import annotations

import asyncio
import itertools
import json
import sys
import tempfile
import threading
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
        self.assertNotIn("Complete", json.dumps(update_call[1], ensure_ascii=False))
        self.assertNotIn(
            "✅ **Complete**",
            json.dumps(update_call[1], ensure_ascii=False),
        )
        self.assertNotIn("loading", json.dumps(update_call[1], ensure_ascii=False))
        self.assertEqual(
            update_call[1]["config"]["summary"],
            {"content": ""},
        )
        self.assertFalse(
            any(
                element.get("element_id") == "lifecycle_status"
                for element in update_call[1]["body"]["elements"]
            )
        )
        self.assertEqual(
            update_call[1]["body"]["elements"][0]["element_id"],
            "streaming_content",
        )

    def test_steer_freezes_the_old_card_and_streams_below_the_user_message(
        self,
    ) -> None:
        """A same-turn steer rolls CardKit forward instead of editing above it."""
        adapter, calls = self._adapter()
        card_number = 0

        async def create(card: dict[str, Any]) -> Any:
            nonlocal card_number
            card_number += 1
            calls.append(("create", card))
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(card_id=f"card-{card_number}"),
            )

        async def send_with_retry(**kwargs: Any) -> Any:
            calls.append(("send", kwargs))
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id=f"om_card_{card_number}"),
            )

        adapter._cardkit_create = create
        adapter._feishu_send_with_retry = send_with_retry
        steer = self._event()
        steer.message_id = "om_steer"

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())
            await adapter._stream_cardkit_content(state, "partial answer")
            await asyncio.sleep(0.01)

            continued = await adapter._start_cardkit_turn(steer)

            self.assertIs(continued, state)
            self.assertEqual(state.message_id, "om_card_2")
            state.turn_terminal = True
            final = await adapter.edit_message(
                "oc_chat",
                "om_card_1",
                "corrected final answer",
                finalize=True,
                metadata={"thread_id": "om_root"},
            )
            self.assertTrue(final.success)
            self.assertEqual(final.message_id, "om_card_2")
            self.assertNotIn(
                ("oc_chat", "om_root"),
                adapter._cardkit_states_by_route,
            )

        asyncio.run(scenario())

        card_sends = [
            call[1]
            for call in calls
            if call[0] == "send"
            and json.loads(call[1]["payload"]).get("type") == "card"
        ]
        self.assertEqual(
            [call["reply_to"] for call in card_sends],
            ["om_root", "om_steer"],
        )
        boundary_cards = [
            call[1]
            for call in calls
            if call[0] == "update"
            and "Continued below" in json.dumps(call[1], ensure_ascii=False)
        ]
        self.assertEqual(len(boundary_cards), 1)
        self.assertFalse(boundary_cards[0]["config"]["streaming_mode"])
        self.assertNotIn(
            "loading",
            json.dumps(boundary_cards[0], ensure_ascii=False),
        )
        terminal_card = [call[1] for call in calls if call[0] == "update"][-1]
        self.assertIn(
            "corrected final answer",
            json.dumps(terminal_card, ensure_ascii=False),
        )

    def test_silent_accepted_steer_still_opens_a_new_segment(self) -> None:
        """Ack suppression and cooldown cannot break timeline ordering."""
        adapter, calls = self._adapter()
        adapter._chat_locks = __import__("collections").OrderedDict()
        adapter._pending_messages = {}
        adapter._remember_interactive_operator = lambda _event: None
        card_number = 0

        async def create(card: dict[str, Any]) -> Any:
            nonlocal card_number
            card_number += 1
            calls.append(("create", card))
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(card_id=f"card-{card_number}"),
            )

        async def send_with_retry(**kwargs: Any) -> Any:
            calls.append(("send", kwargs))
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id=f"om_card_{card_number}"),
            )

        class BusyOwner:
            """Expose the pinned Hermes busy-mode observations."""

            _draining = False
            _busy_input_mode = "steer"

            @staticmethod
            def _session_key_for_source(_source: Any) -> str:
                return "session-1"

            @staticmethod
            def _is_user_authorized(_source: Any) -> bool:
                return True

            @staticmethod
            def _peek_session_state(_session_key: str) -> Any:
                return None

            async def handle(self, _event: Any, _session_key: str) -> bool:
                return True

        owner = BusyOwner()
        adapter._busy_session_handler = owner.handle
        adapter._cardkit_create = create
        adapter._feishu_send_with_retry = send_with_retry

        async def handle_message(_event: Any) -> None:
            if getattr(_event, "message_id", "") == "om_queued_steer":
                adapter._pending_messages["session-1"] = _event
            return None

        adapter.handle_message = handle_message
        steer = self._event()
        steer.text = "Only change the presentation."
        steer.message_type = self.adapter_module.MessageType.TEXT
        steer.message_id = "om_silent_steer"
        steer.is_command = lambda: False
        queued = self._event()
        queued.text = "This could not be steered."
        queued.message_type = self.adapter_module.MessageType.TEXT
        queued.message_id = "om_queued_steer"
        queued.is_command = lambda: False

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())

            await adapter._handle_message_with_guards(steer)

            self.assertEqual(state.message_id, "om_card_2")
            self.assertEqual(
                state.active_input_message_id,
                "om_silent_steer",
            )

            await adapter._handle_message_with_guards(queued)

            self.assertEqual(state.message_id, "om_card_2")
            self.assertEqual(
                state.active_input_message_id,
                "om_silent_steer",
            )

        asyncio.run(scenario())

        card_sends = [
            call[1]
            for call in calls
            if call[0] == "send"
            and json.loads(call[1]["payload"]).get("type") == "card"
        ]
        self.assertEqual(
            [call["reply_to"] for call in card_sends],
            ["om_root", "om_silent_steer"],
        )

    def test_origin_completion_closes_the_latest_steer_segment(
        self,
    ) -> None:
        """The originating dispatch owns completion for its steered run."""
        adapter, calls = self._adapter()
        adapter._reactions_enabled = lambda: False
        card_number = 0

        async def create(card: dict[str, Any]) -> Any:
            nonlocal card_number
            card_number += 1
            calls.append(("create", card))
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(card_id=f"card-{card_number}"),
            )

        async def send_with_retry(**kwargs: Any) -> Any:
            calls.append(("send", kwargs))
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id=f"om_card_{card_number}"),
            )

        adapter._cardkit_create = create
        adapter._feishu_send_with_retry = send_with_retry
        origin = self._event()
        steer = self._event()
        steer.message_id = "om_steer"

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(origin)
            await adapter._start_cardkit_turn(steer)
            await adapter.on_processing_complete(
                origin,
                self.adapter_module.ProcessingOutcome.FAILURE,
            )

            self.assertTrue(state.closed)
            self.assertEqual(state.message_id, "om_card_2")
            self.assertNotIn(
                ("oc_chat", "om_root"),
                adapter._cardkit_states_by_route,
            )

        asyncio.run(scenario())

        terminal_card = [call[1] for call in calls if call[0] == "update"][-1]
        self.assertIn("Error", json.dumps(terminal_card, ensure_ascii=False))
        self.assertIn(
            "The request failed before a response completed.",
            json.dumps(terminal_card, ensure_ascii=False),
        )

    def test_terminal_output_during_segment_creation_is_delivered(self) -> None:
        """A final answer racing continuation creation remains terminal."""
        for delivery in ("edit", "send", "fallback"):
            with self.subTest(delivery=delivery):
                adapter, calls = self._adapter()
                adapter._reactions_enabled = lambda: False
                card_number = 0
                continuation_started = asyncio.Event()
                release_continuation = asyncio.Event()
                ordinary_messages: dict[str, str] = {}

                async def create(card: dict[str, Any]) -> Any:
                    nonlocal card_number
                    card_number += 1
                    calls.append(("create", card))
                    if card_number == 2:
                        continuation_started.set()
                        await release_continuation.wait()
                    return SimpleNamespace(
                        success=lambda: not (
                            delivery == "fallback" and card_number == 2
                        ),
                        data=SimpleNamespace(
                            card_id=f"card-{card_number}"
                        ),
                        code=500,
                        msg="create failed",
                    )

                async def send_with_retry(**kwargs: Any) -> Any:
                    calls.append(("send", kwargs))
                    payload = json.loads(kwargs["payload"])
                    is_card = payload.get("type") == "card"
                    message_id = (
                        f"om_card_{card_number}"
                        if is_card
                        else "om_fallback"
                    )
                    if not is_card:
                        ordinary_messages[message_id] = kwargs["payload"]
                    return SimpleNamespace(
                        success=lambda: True,
                        data=SimpleNamespace(message_id=message_id),
                    )

                adapter._cardkit_create = create
                adapter._feishu_send_with_retry = send_with_retry
                real_edit_message = adapter.edit_message

                async def edit_message(
                    chat_id: str,
                    message_id: str,
                    content: str,
                    **kwargs: Any,
                ) -> Any:
                    if message_id == "om_fallback":
                        ordinary_messages[message_id] = content
                        return self.adapter_module.SendResult(
                            success=True,
                            message_id=message_id,
                        )
                    return await real_edit_message(
                        chat_id,
                        message_id,
                        content,
                        **kwargs,
                    )

                adapter.edit_message = edit_message

                async def scenario() -> None:
                    origin = self._event()
                    state = await adapter._start_cardkit_turn(origin)
                    steer_task = asyncio.create_task(
                        adapter.send(
                            "oc_chat",
                            (
                                "⏩ Steered into current run. Your message "
                                "arrives after the next tool call."
                            ),
                            reply_to="om_steer",
                            metadata={"thread_id": "om_root"},
                        )
                    )
                    await continuation_started.wait()

                    state.turn_terminal = True
                    if delivery == "send":
                        final = await adapter.send(
                            "oc_chat",
                            "important final answer",
                            reply_to="om_root",
                            metadata={
                                "thread_id": "om_root",
                                "notify": True,
                            },
                        )
                    else:
                        final = await adapter.edit_message(
                            "oc_chat",
                            "om_card_1",
                            "important final answer",
                            finalize=True,
                            metadata={"thread_id": "om_root"},
                        )
                    await adapter.on_processing_complete(
                        origin,
                        self.adapter_module.ProcessingOutcome.SUCCESS,
                    )
                    release_continuation.set()
                    await steer_task

                    self.assertTrue(final.success)
                    self.assertTrue(state.closed)
                    self.assertEqual(
                        state.content,
                        "important final answer",
                    )
                    self.assertEqual(state.phase, "complete")
                    if delivery == "fallback":
                        fallback_content = ordinary_messages.get("om_fallback")
                        if fallback_content and fallback_content.startswith("{"):
                            fallback_content = json.loads(fallback_content)["text"]
                        self.assertEqual(
                            fallback_content,
                            "important final answer",
                        )
                    else:
                        terminal_cards = [
                            call[1]
                            for call in calls
                            if call[0] == "update"
                            and "important final answer" in json.dumps(
                                call[1],
                                ensure_ascii=False,
                            )
                        ]
                        self.assertTrue(terminal_cards)

                asyncio.run(scenario())

    def test_failed_steer_segment_falls_back_to_a_regular_message(self) -> None:
        """A failed continuation card does not swallow subsequent output."""
        adapter, calls = self._adapter()
        create_count = 0

        async def create(card: dict[str, Any]) -> Any:
            nonlocal create_count
            create_count += 1
            calls.append(("create", card))
            if create_count == 2:
                return SimpleNamespace(
                    success=lambda: False,
                    code=500,
                    msg="create failed",
                )
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(card_id="card-1"),
            )

        async def send_with_retry(**kwargs: Any) -> Any:
            calls.append(("send", kwargs))
            payload = json.loads(kwargs["payload"])
            message_id = (
                "om_card"
                if payload.get("type") == "card"
                else "om_fallback"
            )
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id=message_id),
            )

        adapter._cardkit_create = create
        adapter._feishu_send_with_retry = send_with_retry
        adapter._finalize_send_result = lambda response, _error: (
            self.adapter_module.SendResult(
                success=True,
                message_id=response.data.message_id,
            )
        )
        original_edit_message = adapter.edit_message

        async def edit_message(
            chat_id: str,
            message_id: str,
            content: str,
            **kwargs: Any,
        ) -> Any:
            if message_id == "om_fallback":
                calls.append(("regular_edit", content))
                return self.adapter_module.SendResult(
                    success=True,
                    message_id=message_id,
                )
            return await original_edit_message(
                chat_id,
                message_id,
                content,
                **kwargs,
            )

        adapter.edit_message = edit_message
        steer = self._event()
        steer.message_id = "om_steer"

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())
            steer_ack = await adapter.send(
                "oc_chat",
                (
                    "⏩ Steered into current run. Your message arrives "
                    "after the next tool call."
                ),
                reply_to=steer.message_id,
                metadata={"thread_id": "om_root"},
            )

            self.assertTrue(steer_ack.success)
            self.assertFalse(state.segment_open)
            self.assertEqual(state.suspension_reason, "fallback")
            fallback = await adapter.send(
                "oc_chat",
                "fallback answer",
                reply_to="om_steer",
                metadata={"thread_id": "om_root", "expect_edits": True},
            )
            self.assertTrue(fallback.success)
            self.assertEqual(fallback.message_id, "om_card")

            state.turn_terminal = True
            final = await adapter.edit_message(
                "oc_chat",
                fallback.message_id,
                "final fallback answer",
                finalize=True,
                metadata={"thread_id": "om_root"},
            )

            self.assertTrue(final.success)
            self.assertEqual(final.message_id, "om_card")
            self.assertTrue(state.closed)
            self.assertNotIn(
                ("oc_chat", "om_root"),
                adapter._cardkit_states_by_route,
            )

        asyncio.run(scenario())

        regular_sends = [
            call[1]
            for call in calls
            if call[0] == "send"
            and json.loads(call[1]["payload"]).get("type") != "card"
        ]
        self.assertEqual(len(regular_sends), 1)
        self.assertEqual(regular_sends[0]["reply_to"], "om_steer")
        self.assertIn(("regular_edit", "final fallback answer"), calls)

    def test_repeated_failed_steers_create_fallbacks_at_each_boundary(
        self,
    ) -> None:
        """Each failed Steer continuation falls back below its own input."""
        adapter, calls = self._adapter()
        create_count = 0
        regular_sends: list[tuple[str, str | None]] = []
        regular_edits: list[tuple[str, str]] = []

        async def create(card: dict[str, Any]) -> Any:
            nonlocal create_count
            create_count += 1
            calls.append(("create", card))
            return SimpleNamespace(
                success=lambda: create_count == 1,
                data=SimpleNamespace(card_id="card-1"),
                code=500,
                msg="create failed",
            )

        async def send_with_retry(**kwargs: Any) -> Any:
            payload = json.loads(kwargs["payload"])
            if payload.get("type") == "card":
                message_id = "om_card"
            else:
                message_id = f"om_fallback_{len(regular_sends) + 1}"
                regular_sends.append((message_id, kwargs["reply_to"]))
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id=message_id),
            )

        adapter._cardkit_create = create
        adapter._feishu_send_with_retry = send_with_retry
        original_edit_message = adapter.edit_message

        async def edit_message(
            chat_id: str,
            message_id: str,
            content: str,
            **kwargs: Any,
        ) -> Any:
            if message_id.startswith("om_fallback_"):
                regular_edits.append((message_id, content))
                return self.adapter_module.SendResult(
                    success=True,
                    message_id=message_id,
                )
            return await original_edit_message(
                chat_id,
                message_id,
                content,
                **kwargs,
            )

        adapter.edit_message = edit_message

        async def scenario() -> None:
            await adapter._start_cardkit_turn(self._event())
            for number in (1, 2):
                steer_message_id = f"om_steer_{number}"
                await adapter.send(
                    "oc_chat",
                    (
                        "⏩ Steered into current run. Your message arrives "
                        "after the next tool call."
                    ),
                    reply_to=steer_message_id,
                    metadata={"thread_id": "om_root"},
                )
                await adapter.send(
                    "oc_chat",
                    f"answer after steer {number}",
                    reply_to=steer_message_id,
                    metadata={
                        "thread_id": "om_root",
                        "expect_edits": True,
                    },
                )

        asyncio.run(scenario())

        self.assertEqual(
            regular_sends,
            [
                ("om_fallback_1", "om_steer_1"),
                ("om_fallback_2", "om_steer_2"),
            ],
        )
        self.assertEqual(
            regular_edits,
            [
                ("om_fallback_1", "answer after steer 1"),
                ("om_fallback_2", "answer after steer 2"),
            ],
        )

    def test_question_suspends_stream_and_answer_resumes_below_question(
        self,
    ) -> None:
        """An AskUserQuestion card is a boundary for subsequent streaming."""
        adapter, calls = self._adapter()
        adapter._openclaw_interaction_messages = {}
        adapter._openclaw_submitted_lock = threading.Lock()
        card_number = 0

        async def create(card: dict[str, Any]) -> Any:
            nonlocal card_number
            card_number += 1
            calls.append(("create", card))
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(card_id=f"card-{card_number}"),
            )

        async def send_with_retry(**kwargs: Any) -> Any:
            calls.append(("send", kwargs))
            payload = json.loads(kwargs["payload"])
            message_id = (
                "om_question"
                if payload.get("schema") == "2.0"
                else f"om_card_{card_number}"
            )
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id=message_id),
            )

        adapter._cardkit_create = create
        adapter._feishu_send_with_retry = send_with_retry
        interaction = SimpleNamespace(
            token="question-1",
            request={
                "questions": [
                    {
                        "question": "Which path?",
                        "header": "Path",
                        "options": [
                            {"label": "Device", "description": "Use a device"}
                        ],
                        "multiSelect": False,
                    }
                ]
            },
            ticket=SimpleNamespace(
                chat_id="oc_chat",
                message_id="om_root",
                thread_id="om_root",
                session_thread_id="om_root",
            ),
        )
        resumed_event = self._event()
        resumed_event.message_id = "om_root:ask-user-answer:question-1"

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())
            await adapter._stream_cardkit_content(state, "investigation so far")
            await asyncio.sleep(0.01)

            delivered = await adapter._send_openclaw_interaction_card(interaction)

            self.assertTrue(delivered)
            self.assertFalse(state.segment_open)
            self.assertEqual(state.resume_anchor_message_id, "om_question")

            resumed = await adapter._start_cardkit_turn(resumed_event)

            self.assertIs(resumed, state)
            self.assertTrue(state.segment_open)
            self.assertEqual(state.message_id, "om_card_2")

        asyncio.run(scenario())

        waiting_cards = [
            call[1]
            for call in calls
            if call[0] == "update"
            and "Waiting for your answer" in json.dumps(
                call[1], ensure_ascii=False
            )
        ]
        self.assertEqual(len(waiting_cards), 1)
        self.assertFalse(waiting_cards[0]["config"]["streaming_mode"])
        card_sends = [
            call[1]
            for call in calls
            if call[0] == "send"
            and json.loads(call[1]["payload"]).get("type") == "card"
        ]
        self.assertEqual(
            [call["reply_to"] for call in card_sends],
            ["om_root", "om_question"],
        )

    def test_question_send_failure_closes_the_active_segment_as_error(
        self,
    ) -> None:
        """A missing Question card cannot leave the response generating."""
        adapter, calls = self._adapter()
        adapter._openclaw_interaction_messages = {}
        adapter._openclaw_submitted_lock = threading.Lock()

        async def send_with_retry(**kwargs: Any) -> Any:
            calls.append(("send", kwargs))
            payload = json.loads(kwargs["payload"])
            if payload.get("schema") == "2.0":
                return SimpleNamespace(
                    success=lambda: False,
                    code=500,
                    msg="question failed",
                )
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id="om_card"),
            )

        adapter._feishu_send_with_retry = send_with_retry
        interaction = SimpleNamespace(
            token="question-1",
            request={
                "questions": [
                    {
                        "question": "Which path?",
                        "header": "Path",
                        "options": [],
                        "multiSelect": False,
                    }
                ]
            },
            ticket=SimpleNamespace(
                chat_id="oc_chat",
                message_id="om_root",
                thread_id="om_root",
                session_thread_id="om_root",
            ),
        )

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())

            delivered = await adapter._send_openclaw_interaction_card(interaction)

            self.assertFalse(delivered)
            self.assertTrue(state.closed)
            self.assertEqual(state.phase, "error")

        asyncio.run(scenario())

        terminal_card = [call[1] for call in calls if call[0] == "update"][-1]
        self.assertIn("Error", json.dumps(terminal_card, ensure_ascii=False))
        self.assertIn(
            "Unable to send the question card",
            json.dumps(terminal_card, ensure_ascii=False),
        )

    def test_question_response_without_message_id_closes_the_active_segment(
        self,
    ) -> None:
        """An untrackable Question card cannot leave an active stream."""
        adapter, calls = self._adapter()
        adapter._openclaw_interaction_messages = {}
        adapter._openclaw_submitted_lock = threading.Lock()

        async def send_with_retry(**kwargs: Any) -> Any:
            calls.append(("send", kwargs))
            payload = json.loads(kwargs["payload"])
            if payload.get("type") == "card":
                return SimpleNamespace(
                    success=lambda: True,
                    data=SimpleNamespace(message_id="om_card"),
                )
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(),
            )

        adapter._feishu_send_with_retry = send_with_retry
        interaction = SimpleNamespace(
            token="question-1",
            request={
                "questions": [
                    {
                        "question": "Which path?",
                        "header": "Path",
                        "options": [],
                        "multiSelect": False,
                    }
                ]
            },
            ticket=SimpleNamespace(
                chat_id="oc_chat",
                message_id="om_root",
                thread_id="om_root",
                session_thread_id="om_root",
            ),
        )

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())

            delivered = await adapter._send_openclaw_interaction_card(
                interaction
            )

            self.assertFalse(delivered)
            self.assertTrue(state.closed)
            self.assertEqual(state.phase, "error")

        asyncio.run(scenario())

        terminal_card = [call[1] for call in calls if call[0] == "update"][-1]
        self.assertIn("Error", json.dumps(terminal_card, ensure_ascii=False))

    def test_question_answer_redirect_continues_below_the_question_card(
        self,
    ) -> None:
        """A synthetic busy redirect uses the visible interaction as anchor."""
        adapter, calls = self._adapter()
        adapter._openclaw_interaction_messages = {}
        adapter._openclaw_submitted_lock = threading.Lock()
        card_number = 0

        async def create(card: dict[str, Any]) -> Any:
            nonlocal card_number
            card_number += 1
            calls.append(("create", card))
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(card_id=f"card-{card_number}"),
            )

        async def send_with_retry(**kwargs: Any) -> Any:
            calls.append(("send", kwargs))
            payload = json.loads(kwargs["payload"])
            message_id = (
                "om_question"
                if payload.get("schema") == "2.0"
                else f"om_card_{card_number}"
            )
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id=message_id),
            )

        adapter._cardkit_create = create
        adapter._feishu_send_with_retry = send_with_retry
        interaction = SimpleNamespace(
            token="question-1",
            request={
                "questions": [
                    {
                        "question": "Which path?",
                        "header": "Path",
                        "options": [],
                        "multiSelect": False,
                    }
                ]
            },
            ticket=SimpleNamespace(
                chat_id="oc_chat",
                message_id="om_root",
                thread_id="om_root",
                session_thread_id="om_root",
            ),
        )

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())
            await adapter._send_openclaw_interaction_card(interaction)

            redirected = await adapter.send(
                "oc_chat",
                "↪ Redirected current run. I'll use your answer.",
                reply_to="om_root:ask-user-answer:question-1",
                metadata={"thread_id": "om_root"},
            )

            self.assertTrue(redirected.success)
            self.assertTrue(state.segment_open)
            self.assertEqual(state.message_id, "om_card_2")
            self.assertEqual(
                state.active_input_message_id,
                "om_root:ask-user-answer:question-1",
            )

        asyncio.run(scenario())

        card_sends = [
            call[1]
            for call in calls
            if call[0] == "send"
            and json.loads(call[1]["payload"]).get("type") == "card"
        ]
        self.assertEqual(
            [call["reply_to"] for call in card_sends],
            ["om_root", "om_question"],
        )

    def test_question_answer_after_steer_reuses_the_latest_segment(self) -> None:
        """A synthetic answer cannot move output above a newer user message."""
        adapter, calls = self._adapter()
        adapter._openclaw_interaction_messages = {}
        adapter._openclaw_submitted_lock = threading.Lock()
        card_number = 0

        async def create(card: dict[str, Any]) -> Any:
            nonlocal card_number
            card_number += 1
            calls.append(("create", card))
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(card_id=f"card-{card_number}"),
            )

        async def send_with_retry(**kwargs: Any) -> Any:
            calls.append(("send", kwargs))
            payload = json.loads(kwargs["payload"])
            message_id = (
                "om_question"
                if payload.get("schema") == "2.0"
                else f"om_card_{card_number}"
            )
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id=message_id),
            )

        adapter._cardkit_create = create
        adapter._feishu_send_with_retry = send_with_retry
        interaction = SimpleNamespace(
            token="question-1",
            request={
                "questions": [
                    {
                        "question": "Which path?",
                        "header": "Path",
                        "options": [],
                        "multiSelect": False,
                    }
                ]
            },
            ticket=SimpleNamespace(
                chat_id="oc_chat",
                message_id="om_root",
                thread_id="om_root",
                session_thread_id="om_root",
            ),
        )

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())
            await adapter._send_openclaw_interaction_card(interaction)
            await adapter.send(
                "oc_chat",
                "↪ Steered into current run.",
                reply_to="om_steer",
                metadata={"thread_id": "om_root"},
            )

            redirected = await adapter.send(
                "oc_chat",
                "↪ Redirected current run. I'll use your answer.",
                reply_to="om_root:ask-user-answer:question-1",
                metadata={"thread_id": "om_root"},
            )

            self.assertTrue(redirected.success)
            self.assertEqual(state.message_id, "om_card_2")
            self.assertEqual(
                state.active_input_message_id,
                "om_root:ask-user-answer:question-1",
            )

        asyncio.run(scenario())

        card_sends = [
            call[1]
            for call in calls
            if call[0] == "send"
            and json.loads(call[1]["payload"]).get("type") == "card"
        ]
        self.assertEqual(
            [call["reply_to"] for call in card_sends],
            ["om_root", "om_steer"],
        )

    def test_question_wait_survives_the_originating_dispatch_completion(self) -> None:
        """The pending question owns the route after its original turn returns."""
        adapter, calls = self._adapter()
        adapter._openclaw_interaction_messages = {}
        adapter._openclaw_submitted_lock = threading.Lock()
        adapter._reactions_enabled = lambda: False
        interaction = SimpleNamespace(
            token="question-1",
            request={
                "questions": [
                    {
                        "question": "Which path?",
                        "header": "Path",
                        "options": [],
                        "multiSelect": False,
                    }
                ]
            },
            ticket=SimpleNamespace(
                chat_id="oc_chat",
                message_id="om_root",
                thread_id="om_root",
                session_thread_id="om_root",
            ),
        )

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())
            delivered = await adapter._send_openclaw_interaction_card(interaction)
            self.assertTrue(delivered)
            update_count = len([call for call in calls if call[0] == "update"])

            state.turn_terminal = True
            hidden_final = await adapter.edit_message(
                "oc_chat",
                "om_card",
                "Question card sent.",
                finalize=True,
                metadata={"thread_id": "om_root"},
            )
            await adapter.on_processing_complete(
                self._event(),
                self.adapter_module.ProcessingOutcome.SUCCESS,
            )

            self.assertTrue(hidden_final.success)
            self.assertFalse(state.segment_open)
            self.assertIs(
                adapter._cardkit_states_by_route[("oc_chat", "om_root")],
                state,
            )
            self.assertEqual(
                len([call for call in calls if call[0] == "update"]),
                update_count,
            )

        asyncio.run(scenario())

    def test_expired_question_releases_its_suspended_cardkit_route(self) -> None:
        """An expired interaction cannot retain a dead logical turn forever."""
        adapter, _calls = self._adapter()
        adapter._openclaw_interaction_messages = {
            "question-1": "om_question",
        }
        adapter._openclaw_submitted_tokens = {"question-1"}
        adapter._openclaw_submitted_lock = threading.Lock()

        async def update_question(
            _question_id: str,
            _card: dict[str, Any],
        ) -> bool:
            return True

        adapter._update_openclaw_question_card = update_question
        ticket = SimpleNamespace(
            chat_id="oc_chat",
            message_id="om_root",
            thread_id="om_root",
            session_thread_id="om_root",
        )

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())
            await adapter._suspend_cardkit_segment(
                state,
                reason="question",
                resume_anchor_message_id="om_question",
            )

            expired = await adapter._expire_openclaw_question_card(
                "question-1",
                [{"question": "Continue?", "header": "Confirm"}],
                ticket=ticket,
            )

            self.assertTrue(expired)
            self.assertTrue(state.closed)
            self.assertNotIn(
                ("oc_chat", "om_root"),
                adapter._cardkit_states_by_route,
            )
            self.assertNotIn("om_card", adapter._cardkit_states_by_message)

        asyncio.run(scenario())

    def test_approval_resumes_streaming_below_the_resolved_card(self) -> None:
        """Blocking approval output lazily opens a continuation segment."""
        adapter, calls = self._adapter()
        adapter._approval_counter = itertools.count(1)
        adapter._approval_state = {}
        adapter._interactive_operator_for_send = lambda *_args, **_kwargs: "ou_user"
        adapter._format_exec_approval = (
            lambda command, description, _smart_denied: (
                f"```plain_text\n{command}\n```\n{description}"
            )
        )
        adapter._finalize_send_result = lambda response, _error: (
            self.adapter_module.SendResult(
                success=True,
                message_id=response.data.message_id,
            )
        )
        card_number = 0

        async def create(card: dict[str, Any]) -> Any:
            nonlocal card_number
            card_number += 1
            calls.append(("create", card))
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(card_id=f"card-{card_number}"),
            )

        async def send_with_retry(**kwargs: Any) -> Any:
            calls.append(("send", kwargs))
            payload = json.loads(kwargs["payload"])
            message_id = (
                "om_approval"
                if "header" in payload
                else f"om_card_{card_number}"
            )
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id=message_id),
            )

        adapter._cardkit_create = create
        adapter._feishu_send_with_retry = send_with_retry
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
            state.session_id = "session-1"
            state.turn_id = "turn-1"
            await adapter._update_cardkit_tool_for_ticket(
                ticket,
                tool_name="terminal",
                tool_call_id="call-1",
                status="running",
                session_id="session-1",
                turn_id="turn-1",
            )

            approval = await adapter.send_exec_approval(
                "oc_chat",
                "rm -rf /tmp/example",
                "session-1",
                metadata={"thread_id": "om_root"},
            )

            self.assertTrue(approval.success)
            self.assertFalse(state.segment_open)
            self.assertEqual(state.suspension_reason, "approval")
            self.assertEqual(state.resume_anchor_message_id, "om_approval")

            updated = await adapter._update_cardkit_tool_for_ticket(
                ticket,
                tool_name="terminal",
                tool_call_id="call-1",
                status="ok",
                session_id="session-1",
                turn_id="turn-1",
            )

            self.assertTrue(updated)
            self.assertTrue(state.segment_open)
            self.assertEqual(state.message_id, "om_card_2")

        asyncio.run(scenario())

        waiting_cards = [
            call[1]
            for call in calls
            if call[0] == "update"
            and "Waiting for your approval" in json.dumps(
                call[1], ensure_ascii=False
            )
        ]
        self.assertEqual(len(waiting_cards), 1)
        self.assertFalse(waiting_cards[0]["config"]["streaming_mode"])
        card_sends = [
            call[1]
            for call in calls
            if call[0] == "send"
            and json.loads(call[1]["payload"]).get("type") == "card"
        ]
        self.assertEqual(
            [call["reply_to"] for call in card_sends],
            ["om_root", "om_approval"],
        )

    def test_cancelled_approval_cannot_resume_from_a_late_tool_callback(
        self,
    ) -> None:
        """Cancellation retires a waiting approval CardKit route."""
        adapter, calls = self._adapter()
        adapter._reactions_enabled = lambda: False

        async def scenario() -> None:
            event = self._event()
            state = await adapter._start_cardkit_turn(event)
            await adapter._suspend_cardkit_for_boundary(
                chat_id="oc_chat",
                thread_id="om_root",
                message_id="om_approval",
                reason="approval",
            )
            await adapter.on_processing_complete(
                event,
                SimpleNamespace(value="cancelled"),
            )
            creates_before_callback = sum(
                call[0] == "create" for call in calls
            )
            updated = await adapter._update_cardkit_tool_for_ticket(
                SimpleNamespace(
                    chat_id="oc_chat",
                    session_thread_id="om_root",
                ),
                tool_name="terminal",
                tool_call_id="approval-tool",
                status="cancelled",
            )

            self.assertFalse(updated)
            self.assertTrue(state.closed)
            self.assertEqual(state.phase, "stopped")
            self.assertNotIn(
                ("oc_chat", "om_root"),
                adapter._cardkit_states_by_route,
            )
            self.assertEqual(
                sum(call[0] == "create" for call in calls),
                creates_before_callback,
            )

        asyncio.run(scenario())

    def test_artifact_boundary_resumes_streaming_below_the_attachment(self) -> None:
        """A native attachment becomes the anchor for later assistant text."""
        adapter, calls = self._adapter()
        card_number = 0

        async def create(card: dict[str, Any]) -> Any:
            nonlocal card_number
            card_number += 1
            calls.append(("create", card))
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(card_id=f"card-{card_number}"),
            )

        async def send_with_retry(**kwargs: Any) -> Any:
            calls.append(("send", kwargs))
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id=f"om_card_{card_number}"),
            )

        adapter._cardkit_create = create
        adapter._feishu_send_with_retry = send_with_retry

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())

            artifact = await adapter._finish_artifact_send(
                self.adapter_module.SendResult(
                    success=True,
                    message_id="om_report",
                ),
                chat_id="oc_chat",
                reply_to="om_root",
                metadata={"thread_id": "om_root"},
            )
            streamed = await adapter._stream_cardkit_content(
                state,
                "The report is ready.",
            )

            self.assertTrue(artifact.success)
            self.assertTrue(streamed.success)
            self.assertTrue(state.segment_open)
            self.assertEqual(state.message_id, "om_card_2")

        asyncio.run(scenario())

        card_sends = [
            call[1]
            for call in calls
            if call[0] == "send"
            and json.loads(call[1]["payload"]).get("type") == "card"
        ]
        self.assertEqual(
            [call["reply_to"] for call in card_sends],
            ["om_root", "om_report"],
        )
        continued = [
            call[1]
            for call in calls
            if call[0] == "update"
            and "Continued below" in json.dumps(call[1], ensure_ascii=False)
        ]
        self.assertEqual(len(continued), 1)

    def test_concurrent_artifact_resumes_share_one_continuation_card(
        self,
    ) -> None:
        """Heartbeat and tool completion serialize one artifact resume."""
        adapter, calls = self._adapter()
        adapter._reactions_enabled = lambda: False
        created = 0
        first_resume_started = asyncio.Event()
        release_first_resume = asyncio.Event()
        sent_cards: list[str] = []
        streaming_updates: list[tuple[str, bool]] = []

        async def create(card: dict[str, Any]) -> Any:
            nonlocal created
            created += 1
            card_number = created
            if card_number == 2:
                first_resume_started.set()
                await release_first_resume.wait()
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(card_id=f"card-{card_number}"),
            )

        async def send_with_retry(**kwargs: Any) -> Any:
            card_id = json.loads(kwargs["payload"])["data"]["card_id"]
            sent_cards.append(card_id)
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id=f"om_{card_id}"),
            )

        async def update(
            state: Any,
            card: dict[str, Any],
            sequence: int,
        ) -> Any:
            del sequence
            streaming_updates.append(
                (state.card_id, card["config"]["streaming_mode"])
            )
            return SimpleNamespace(success=lambda: True, data=SimpleNamespace())

        adapter._cardkit_create = create
        adapter._feishu_send_with_retry = send_with_retry
        adapter._cardkit_update = update

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())
            await adapter._suspend_cardkit_for_boundary(
                chat_id="oc_chat",
                thread_id="om_root",
                message_id="om_artifact",
                reason="artifact",
            )
            ticket = SimpleNamespace(
                chat_id="oc_chat",
                session_thread_id="om_root",
            )
            tool_update = asyncio.create_task(
                adapter._update_cardkit_tool_for_ticket(
                    ticket,
                    tool_name="tool_a",
                    tool_call_id="a",
                    status="success",
                )
            )
            await first_resume_started.wait()
            heartbeat = asyncio.create_task(
                adapter.send(
                    "oc_chat",
                    "⏳ Working — 3 min — send_message",
                    metadata={"thread_id": "om_root"},
                )
            )
            await asyncio.sleep(0)
            release_first_resume.set()
            await asyncio.gather(tool_update, heartbeat)

            state.turn_terminal = True
            final = await adapter.edit_message(
                "oc_chat",
                state.message_id,
                "final answer",
                finalize=True,
                metadata={"thread_id": "om_root"},
            )

            self.assertTrue(final.success)
            self.assertTrue(state.closed)
            self.assertEqual(state.card_id, "card-2")
            self.assertEqual(list(state.tools), ["a"])

        asyncio.run(scenario())

        self.assertEqual(sent_cards, ["card-1", "card-2"])
        streaming_cards = {
            card_id for card_id, streaming in streaming_updates if streaming
        }
        closed_cards = {
            card_id for card_id, streaming in streaming_updates if not streaming
        }
        self.assertLessEqual(streaming_cards, closed_cards)

    def test_artifact_boundary_resumes_before_terminal_send(self) -> None:
        """A terminal text send continues below an earlier attachment."""
        adapter, calls = self._adapter()
        adapter._reactions_enabled = lambda: False
        card_number = 0

        async def create(card: dict[str, Any]) -> Any:
            nonlocal card_number
            card_number += 1
            calls.append(("create", card))
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(card_id=f"card-{card_number}"),
            )

        async def send_with_retry(**kwargs: Any) -> Any:
            calls.append(("send", kwargs))
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(
                    message_id=f"om_card_{card_number}"
                ),
            )

        adapter._cardkit_create = create
        adapter._feishu_send_with_retry = send_with_retry

        async def scenario() -> None:
            origin = self._event()
            state = await adapter._start_cardkit_turn(origin)
            state.turn_terminal = True
            artifact = await adapter._finish_artifact_send(
                self.adapter_module.SendResult(
                    success=True,
                    message_id="om_voice",
                ),
                chat_id="oc_chat",
                reply_to="om_root",
                metadata={"thread_id": "om_root", "notify": True},
            )
            final = await adapter.send(
                "oc_chat",
                "complete written answer",
                reply_to="om_root",
                metadata={"thread_id": "om_root", "notify": True},
            )
            await adapter.on_processing_complete(
                origin,
                self.adapter_module.ProcessingOutcome.SUCCESS,
            )

            self.assertTrue(artifact.success)
            self.assertTrue(final.success)
            self.assertTrue(state.closed)
            self.assertEqual(state.message_id, "om_card_2")
            self.assertEqual(state.content, "complete written answer")

        asyncio.run(scenario())

        card_sends = [
            call[1]
            for call in calls
            if call[0] == "send"
            and json.loads(call[1]["payload"]).get("type") == "card"
        ]
        self.assertEqual(
            [call["reply_to"] for call in card_sends],
            ["om_root", "om_voice"],
        )
        terminal_cards = [
            call[1]
            for call in calls
            if call[0] == "update"
            and "complete written answer" in json.dumps(
                call[1],
                ensure_ascii=False,
            )
        ]
        self.assertTrue(terminal_cards)

    def test_direct_plugin_commands_skip_cardkit_without_disabling_skill_commands(
        self,
    ) -> None:
        adapter, calls = self._adapter()
        adapter._finalize_send_result = lambda *_args: SimpleNamespace(
            success=True,
            message_id="om_diagnostics",
        )

        async def scenario() -> None:
            for command in (
                "/feishu",
                "/feishu-auth",
                "/feishu-diagnose",
                "/feishu-doctor",
                "/feishu_auth",
                "/feishu_diagnose",
                "/feishu_doctor",
            ):
                event = self._event()
                event.text = f"{command} details"
                event.message_type = self.adapter_module.MessageType.COMMAND
                self.assertIsNone(await adapter._start_cardkit_turn(event))

            normalized_command = self._event()
            normalized_command.text = "  /FEISHU_DOCTOR@bot details"
            normalized_command.message_type = self.adapter_module.MessageType.TEXT
            self.assertIsNone(await adapter._start_cardkit_turn(normalized_command))

            result = await adapter.send(
                "oc_chat",
                "### Feishu Plugin Diagnostics\n\nOverall status: **HEALTHY**",
                reply_to="om_root",
                metadata={"thread_id": "om_root", "notify": True},
            )
            self.assertTrue(result.success)

            agent_command = self._event()
            agent_command.text = "/feishu-task list my tasks"
            agent_command.message_type = self.adapter_module.MessageType.COMMAND
            self.assertIsNotNone(await adapter._start_cardkit_turn(agent_command))

        asyncio.run(scenario())

        sends = [call for call in calls if call[0] == "send"]
        self.assertEqual(len(sends), 2)
        self.assertEqual(sends[0][1]["msg_type"], "post")
        self.assertIn("Feishu Plugin Diagnostics", sends[0][1]["payload"])
        self.assertEqual(sends[1][1]["msg_type"], "interactive")
        self.assertFalse(any(call[0] == "update" for call in calls))

    def test_unknown_gateway_command_reuses_its_card_for_command_reply(self) -> None:
        adapter, calls = self._adapter()
        adapter._reactions_enabled = lambda: False
        adapter._finalize_send_result = lambda *_args: SimpleNamespace(
            success=True,
            message_id="om_command_reply",
        )
        event = self._event()
        event.text = "/reload"
        event.message_type = self.adapter_module.MessageType.COMMAND
        command_reply = (
            "Unknown command `/reload`. Type /commands to see what's available, "
            "or resend without the leading slash to send as a regular message."
        )

        async def scenario() -> None:
            await adapter.on_processing_start(event)
            result = await adapter.send(
                "oc_chat",
                command_reply,
                reply_to="om_root",
                metadata={"thread_id": "om_root", "notify": True},
            )
            self.assertTrue(result.success)
            await adapter.on_processing_complete(
                event,
                self.adapter_module.ProcessingOutcome.SUCCESS,
            )

        asyncio.run(scenario())

        sends = [call[1] for call in calls if call[0] == "send"]
        terminal_cards = [call[1] for call in calls if call[0] == "update"]
        self.assertEqual(
            {
                "message_types": [call["msg_type"] for call in sends],
                "card_contains_done": any(
                    "Done." in json.dumps(card, ensure_ascii=False)
                    for card in terminal_cards
                ),
                "card_contains_unknown": any(
                    "Unknown command" in json.dumps(card, ensure_ascii=False)
                    for card in terminal_cards
                ),
            },
            {
                "message_types": ["interactive"],
                "card_contains_done": False,
                "card_contains_unknown": True,
            },
        )
        self.assertFalse(adapter._cardkit_states_by_route)

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

    def test_steer_ack_and_compaction_status_stay_in_the_active_segment(self) -> None:
        """Known runtime status text does not create loose thread messages."""
        adapter, calls = self._adapter()
        card_number = 0

        async def create(card: dict[str, Any]) -> Any:
            nonlocal card_number
            card_number += 1
            calls.append(("create", card))
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(card_id=f"card-{card_number}"),
            )

        async def send_with_retry(**kwargs: Any) -> Any:
            calls.append(("send", kwargs))
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id=f"om_card_{card_number}"),
            )

        adapter._cardkit_create = create
        adapter._feishu_send_with_retry = send_with_retry

        async def scenario() -> None:
            state = await adapter._start_cardkit_turn(self._event())
            steer_ack = await adapter.send(
                "oc_chat",
                "⏩ Steered into current run (iteration 2/10).",
                reply_to="om_steer",
                metadata={"thread_id": "om_root"},
            )
            compaction = await adapter.send(
                "oc_chat",
                "🗜️ Compacting context — summarizing earlier conversation...",
                metadata={"thread_id": "om_root"},
            )

            self.assertTrue(steer_ack.success)
            self.assertTrue(compaction.success)
            self.assertEqual(state.message_id, "om_card_2")
            self.assertEqual(state.active_input_message_id, "om_steer")
            self.assertEqual(
                state.progress_content,
                "⏩ Steered into current run (iteration 2/10).",
            )
            self.assertEqual(
                state.heartbeat_content,
                "🗜️ Compacting context — summarizing earlier conversation...",
            )

        asyncio.run(scenario())

        self.assertEqual(len([call for call in calls if call[0] == "send"]), 2)
        self.assertEqual(
            [
                call[1]["reply_to"]
                for call in calls
                if call[0] == "send"
            ],
            ["om_root", "om_steer"],
        )

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
                self.assertTrue(state.closed)
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
