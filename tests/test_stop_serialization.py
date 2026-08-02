"""Concurrency tests for Feishu's immediate stop dispatch path."""

from __future__ import annotations

import asyncio
import sys
import unittest
from collections import OrderedDict
from types import SimpleNamespace
from typing import Any

from tests.test_ask_user_question_adapter import _MISSING_MODULE, _load_modules


class StopSerializationTests(unittest.IsolatedAsyncioTestCase):
    """Verify only /stop bypasses one active Feishu thread queue."""

    @classmethod
    def setUpClass(cls) -> None:
        """Load the adapter against the repository's offline Hermes seam."""
        _, cls.adapter_module, cls.previous_modules = _load_modules()

    @classmethod
    def tearDownClass(cls) -> None:
        """Restore modules replaced by the offline adapter loader."""
        for name, previous in cls.previous_modules.items():
            if previous is _MISSING_MODULE:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    def _event(self, text: str, *, command: bool = False) -> Any:
        """Build one event in a shared Feishu thread."""
        return self.adapter_module.MessageEvent(
            text=text,
            message_type=(
                self.adapter_module.MessageType.COMMAND
                if command
                else self.adapter_module.MessageType.TEXT
            ),
            source=SimpleNamespace(chat_id="oc_chat", thread_id="om_root"),
        )

    async def test_stop_reaches_hermes_while_same_thread_turn_is_active(self) -> None:
        """An admitted /stop does not wait behind the active thread turn."""
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter._chat_locks = OrderedDict()
        active_started = asyncio.Event()
        stop_dispatched = asyncio.Event()
        release_active = asyncio.Event()
        order: list[str] = []

        async def handle_message(event: Any) -> None:
            if event.text == "/stop":
                order.append("stop")
                stop_dispatched.set()
                return
            order.append("active:start")
            active_started.set()
            await release_active.wait()
            order.append("active:end")

        adapter.handle_message = handle_message
        active_task = asyncio.create_task(
            adapter._handle_message_with_guards(self._event("long turn"))
        )
        await active_started.wait()
        stop_task = asyncio.create_task(
            adapter._handle_message_with_guards(
                self._event("/stop", command=True)
            )
        )

        try:
            await asyncio.wait_for(stop_dispatched.wait(), timeout=0.5)
        finally:
            release_active.set()
            await asyncio.gather(active_task, stop_task)

        self.assertEqual(order, ["active:start", "stop", "active:end"])

    async def test_non_stop_command_waits_for_same_thread_turn(self) -> None:
        """Every command other than /stop retains per-thread serialization."""
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter._chat_locks = OrderedDict()
        active_started = asyncio.Event()
        queued_dispatched = asyncio.Event()
        release_active = asyncio.Event()
        order: list[str] = []

        async def handle_message(event: Any) -> None:
            if event.text == "/help":
                order.append("help")
                queued_dispatched.set()
                return
            order.append("active:start")
            active_started.set()
            await release_active.wait()
            order.append("active:end")

        adapter.handle_message = handle_message
        active_task = asyncio.create_task(
            adapter._handle_message_with_guards(self._event("long turn"))
        )
        await active_started.wait()
        queued_task = asyncio.create_task(
            adapter._handle_message_with_guards(
                self._event("/help", command=True)
            )
        )
        await asyncio.sleep(0)

        self.assertFalse(queued_dispatched.is_set())
        release_active.set()
        await asyncio.gather(active_task, queued_task)
        self.assertEqual(order, ["active:start", "active:end", "help"])

    async def test_exact_stop_triggers_normalize_and_bypass_the_thread(self) -> None:
        """Exact English, multilingual, and punctuated triggers become /stop."""
        multilingual_trigger = next(
            trigger
            for trigger in self.adapter_module._MULTILINGUAL_STOP_EXACT_TRIGGERS
            if not trigger.isascii()
        )
        for trigger in ("stop", multilingual_trigger, "STOP!!!"):
            with self.subTest(trigger=trigger):
                adapter = object.__new__(self.adapter_module.FeishuAdapter)
                adapter._chat_locks = OrderedDict()
                active_started = asyncio.Event()
                stop_dispatched = asyncio.Event()
                release_active = asyncio.Event()
                order: list[str] = []
                dispatched_event: list[Any] = []

                async def handle_message(event: Any) -> None:
                    if event.text == "/stop":
                        order.append("stop")
                        dispatched_event.append(event)
                        stop_dispatched.set()
                        return
                    order.append("active:start")
                    active_started.set()
                    await release_active.wait()
                    order.append("active:end")

                adapter.handle_message = handle_message
                adapter._enqueue_text_event = adapter._handle_message_with_guards
                active_task = asyncio.create_task(
                    adapter._handle_message_with_guards(self._event("long turn"))
                )
                await active_started.wait()
                stop_task = asyncio.create_task(
                    adapter._dispatch_inbound_event(self._event(trigger))
                )

                try:
                    await asyncio.wait_for(stop_dispatched.wait(), timeout=0.5)
                finally:
                    release_active.set()
                    await asyncio.gather(active_task, stop_task)

                self.assertEqual(order, ["active:start", "stop", "active:end"])
                self.assertEqual(dispatched_event[0].text, "/stop")
                self.assertIs(
                    dispatched_event[0].message_type,
                    self.adapter_module.MessageType.COMMAND,
                )

    async def test_broad_stop_phrase_remains_serialized_text(self) -> None:
        """A phrase containing stop does not become an authoritative command."""
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter._chat_locks = OrderedDict()
        active_started = asyncio.Event()
        phrase_dispatched = asyncio.Event()
        release_active = asyncio.Event()
        order: list[str] = []
        dispatched_event: list[Any] = []

        async def handle_message(event: Any) -> None:
            if event.text == "stop talking about X":
                order.append("phrase")
                dispatched_event.append(event)
                phrase_dispatched.set()
                return
            order.append("active:start")
            active_started.set()
            await release_active.wait()
            order.append("active:end")

        adapter.handle_message = handle_message
        adapter._enqueue_text_event = adapter._handle_message_with_guards
        active_task = asyncio.create_task(
            adapter._handle_message_with_guards(self._event("long turn"))
        )
        await active_started.wait()
        phrase_task = asyncio.create_task(
            adapter._dispatch_inbound_event(self._event("stop talking about X"))
        )
        await asyncio.sleep(0)

        self.assertFalse(phrase_dispatched.is_set())
        release_active.set()
        await asyncio.gather(active_task, phrase_task)
        self.assertEqual(order, ["active:start", "active:end", "phrase"])
        self.assertIs(
            dispatched_event[0].message_type,
            self.adapter_module.MessageType.TEXT,
        )


if __name__ == "__main__":
    unittest.main()
