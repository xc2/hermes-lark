"""Behavioral tests for the CardKit conversational response model."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "hermes_lark" / "cardkit.py"


def _load_module() -> ModuleType:
    """Load CardKit helpers without importing the Hermes plugin runtime."""
    spec = importlib.util.spec_from_file_location(
        "hermes_lark_cardkit_test",
        MODULE_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CardKitStreamingTests(unittest.IsolatedAsyncioTestCase):
    """Verify the public CardKit state, cards, and opt-in trace seam."""

    @classmethod
    def setUpClass(cls) -> None:
        """Load the isolated CardKit module once."""
        cls.module = _load_module()

    def test_streaming_requires_opt_in_and_obeys_reply_mode(self) -> None:
        """Reply mode selects DM-only, all-chat, or disabled CardKit streaming."""
        enabled = self.module.cardkit_streaming_enabled

        self.assertFalse(enabled({}, chat_type="p2p"))
        self.assertFalse(enabled({"streaming": False}, chat_type="p2p"))
        self.assertTrue(enabled({"streaming": True}, chat_type="p2p"))
        self.assertFalse(enabled({"streaming": True}, chat_type="group"))
        self.assertTrue(
            enabled(
                {"streaming": True, "replyMode": "streaming"},
                chat_type="group",
            )
        )
        self.assertFalse(
            enabled(
                {"streaming": True, "replyMode": "static"},
                chat_type="p2p",
            )
        )
        self.assertTrue(
            enabled(
                {"streaming": True, "reply_mode": "streaming"},
                chat_type="group",
            )
        )
        self.assertTrue(
            enabled(
                {"streaming": True, "replyMode": "auto"},
                chat_type="dm",
            )
        )
        self.assertFalse(
            enabled(
                {"streaming": True, "replyMode": "auto"},
                chat_type="group",
            )
        )
        scene_modes = {
            "streaming": True,
            "replyMode": {
                "direct": "static",
                "group": "streaming",
                "default": "auto",
            },
        }
        self.assertFalse(enabled(scene_modes, chat_type="p2p"))
        self.assertTrue(enabled(scene_modes, chat_type="group"))
        self.assertTrue(
            enabled(
                {
                    "streaming": True,
                    "replyMode": {"default": "streaming"},
                },
                chat_type="p2p",
            )
        )

    def test_initial_card_is_a_streamable_thinking_card(self) -> None:
        """A new turn is visibly Processing and exposes the stream element."""
        card = self.module.build_initial_card()

        self.assertEqual(card["schema"], "2.0")
        self.assertIs(card["config"]["streaming_mode"], True)
        self.assertEqual(card["config"]["summary"]["content"], "Processing...")
        self.assertEqual(
            card["config"]["summary"]["i18n_content"],
            {"zh_cn": "Processing...", "en_us": "Processing..."},
        )
        elements = card["body"]["elements"]
        lifecycle = next(
            element
            for element in elements
            if element.get("element_id") == "lifecycle_status"
        )
        self.assertEqual(
            lifecycle["content"],
            "💭 **Thinking...**",
        )
        self.assertEqual(
            lifecycle["i18n_content"],
            {
                "zh_cn": "💭 **Thinking...**",
                "en_us": "💭 **Thinking...**",
            },
        )
        self.assertEqual(
            next(
                element
                for element in elements
                if element.get("element_id") == "streaming_content"
            )["content"],
            "",
        )
        self.assertTrue(
            any(element.get("element_id") == "loading_icon" for element in elements)
        )

    async def test_conversation_state_serializes_sequences_and_tracks_tools(self) -> None:
        """Concurrent updates reserve unique API sequences on one card."""
        state = self.module.CardKitConversationState(
            chat_id="oc_chat",
            thread_id="om_root",
            card_id="card-1",
            message_id="om_card",
        )

        async def reserve_sequence() -> int:
            """Serialize one sequence reservation like an adapter API write."""
            async with state.lock:
                return state.next_sequence()

        sequences = await asyncio.gather(*(reserve_sequence() for _ in range(20)))
        state.content = "answer so far"
        state.update_tool(
            "call-1",
            name="terminal",
            status="running",
            detail="safe command",
        )
        state.update_tool(
            "call-1",
            name="terminal",
            status="success",
            detail="exit 0",
        )
        state.closed = True

        self.assertEqual(sorted(sequences), list(range(1, 21)))
        self.assertEqual(state.sequence, 20)
        self.assertEqual(state.content, "answer so far")
        self.assertEqual(set(state.tools), {"call-1"})
        self.assertEqual(state.tools["call-1"].name, "terminal")
        self.assertEqual(state.tools["call-1"].status, "success")
        self.assertEqual(state.tools["call-1"].detail, "exit 0")
        self.assertTrue(state.closed)
        self.assertFalse(state.lock.locked())

    def test_card_lifecycle_shows_generation_tools_and_terminal_state(self) -> None:
        """Full-card updates expose Generating, tool status, Complete, and Error."""
        running = {
            "call-1": {
                "name": "terminal",
                "status": "running",
                "detail": "printf hello",
            }
        }

        generating = self.module.build_generating_card(
            "partial answer",
            tools=running,
        )
        self.assertIs(generating["config"]["streaming_mode"], True)
        self.assertEqual(
            generating["config"]["summary"]["content"],
            "Generating...",
        )
        self.assertEqual(
            generating["config"]["summary"]["i18n_content"],
            {"zh_cn": "Generating...", "en_us": "Generating..."},
        )
        generating_elements = generating["body"]["elements"]
        self.assertEqual(
            generating_elements[0]["content"],
            "✍️ **Generating...**",
        )
        self.assertEqual(
            generating_elements[0]["i18n_content"],
            {
                "zh_cn": "✍️ **Generating...**",
                "en_us": "✍️ **Generating...**",
            },
        )
        self.assertEqual(
            next(
                element["content"]
                for element in generating_elements
                if element.get("element_id") == "streaming_content"
            ),
            "partial answer",
        )
        tool_panel = next(
            element
            for element in generating_elements
            if element.get("tag") == "collapsible_panel"
        )
        self.assertEqual(
            tool_panel["elements"][0]["text"]["content"],
            "**terminal** · <font color='turquoise'>Running</font>",
        )
        self.assertEqual(
            tool_panel["elements"][1]["text"]["content"],
            "printf hello",
        )
        self.assertEqual(
            tool_panel["header"]["title"]["i18n_content"],
            {"zh_cn": "🛠️ Tool use", "en_us": "🛠️ Tool use"},
        )
        self.assertTrue(
            any(
                element.get("element_id") == "loading_icon"
                for element in generating_elements
            )
        )

        succeeded = {
            "call-1": self.module.CardKitToolStatus(
                tool_call_id="call-1",
                name="terminal",
                status="success",
                detail="exit 0",
            )
        }
        complete = self.module.build_complete_card(
            "final answer",
            tools=succeeded,
        )
        self.assertIs(complete["config"]["streaming_mode"], False)
        self.assertEqual(complete["config"]["summary"]["content"], "Complete")
        self.assertEqual(
            complete["config"]["summary"]["i18n_content"],
            {"zh_cn": "Complete", "en_us": "Complete"},
        )
        complete_elements = complete["body"]["elements"]
        self.assertEqual(complete_elements[0]["content"], "✅ **Complete**")
        self.assertEqual(
            complete_elements[0]["i18n_content"],
            {"zh_cn": "✅ **Complete**", "en_us": "✅ **Complete**"},
        )
        self.assertEqual(
            next(
                element["content"]
                for element in complete_elements
                if element.get("element_id") == "streaming_content"
            ),
            "final answer",
        )
        self.assertFalse(
            any(
                element.get("element_id") == "loading_icon"
                for element in complete_elements
            )
        )

        stopped = self.module.build_stopped_card("partial answer", tools=running)
        self.assertIs(stopped["config"]["streaming_mode"], False)
        self.assertEqual(stopped["config"]["summary"]["content"], "Stopped")
        self.assertEqual(stopped["body"]["elements"][0]["content"], "⏹️ **Stopped**")

        error = self.module.build_error_card("model failed", tools=running)
        self.assertIs(error["config"]["streaming_mode"], False)
        self.assertEqual(error["config"]["summary"]["content"], "Error")
        self.assertEqual(
            error["config"]["summary"]["i18n_content"],
            {"zh_cn": "Error", "en_us": "Error"},
        )
        self.assertEqual(error["body"]["elements"][0]["content"], "❌ **Error**")
        self.assertEqual(
            error["body"]["elements"][0]["i18n_content"],
            {"zh_cn": "❌ **Error**", "en_us": "❌ **Error**"},
        )
        self.assertEqual(
            next(
                element["content"]
                for element in error["body"]["elements"]
                if element.get("element_id") == "streaming_content"
            ),
            "model failed",
        )
        self.assertFalse(
            any(
                element.get("element_id") == "loading_icon"
                for element in error["body"]["elements"]
            )
        )

    async def test_trace_is_structured_and_only_written_for_explicit_path(self) -> None:
        """E2E diagnostics append one API-result JSON object only when enabled."""
        state = self.module.CardKitConversationState(
            chat_id="oc_chat",
            thread_id="om_root",
            card_id="card-1",
            message_id="om_card",
            content="partial answer",
            sequence=7,
        )
        state.update_tool(
            "call-1",
            name="terminal",
            status="running",
            detail="printf hello",
        )
        card = self.module.build_generating_card(
            state.content,
            tools=state.tools,
        )

        record = await state.record_trace(
            operation="card.update",
            ok=True,
            code=0,
            sequence=7,
            state="generating",
            card=card,
        )
        self.assertIsNone(record)

        with tempfile.TemporaryDirectory() as directory:
            trace_path = Path(directory) / "cardkit.jsonl"
            state.trace_path = trace_path
            written_record = await state.record_trace(
                operation="cardElement.content",
                ok=True,
                code=0,
                sequence=7,
                state="generating",
                content="partial answer plus more",
            )
            lines = trace_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(
            written_record,
            {
                "operation": "cardElement.content",
                "status": "generating",
                "ok": True,
                "code": 0,
                "card_id": "card-1",
                "message_id": "om_card",
                "chat_id": "oc_chat",
                "thread_id": "om_root",
                "sequence": 7,
                "content": "partial answer plus more",
                "card": None,
                "tool_status": {
                    "call-1": {
                        "name": "terminal",
                        "status": "running",
                        "detail": "printf hello",
                    }
                },
            },
        )
        self.assertEqual(len(lines), 1)
        written = json.loads(lines[0])
        self.assertEqual(written["operation"], "cardElement.content")
        self.assertEqual(written["status"], "generating")
        self.assertIs(written["ok"], True)
        self.assertEqual(written["code"], 0)
        self.assertEqual(written["sequence"], 7)
        self.assertEqual(written["content"], "partial answer plus more")
        self.assertIsNone(written["card"])

    def test_silent_and_empty_terminal_text_never_leaks_into_a_card(self) -> None:
        """Silent prefixes stay buffered and terminal cards receive a fallback."""
        should_buffer = self.module.should_buffer_silent_reply
        terminal_content = self.module.terminal_cardkit_content

        self.assertTrue(should_buffer("N"))
        self.assertTrue(should_buffer("NO_REPLY"))
        self.assertFalse(should_buffer("NO_REPLY", visible_content="answer"))
        self.assertFalse(should_buffer("normal answer"))
        self.assertEqual(terminal_content(""), "Done.")
        self.assertEqual(terminal_content("NO_REPLY"), "Done.")
        self.assertEqual(
            terminal_content("NO_REPLY", visible_fallback="prior answer"),
            "prior answer",
        )
        self.assertEqual(terminal_content("normal answer"), "normal answer")

    def test_terminal_card_downgrades_tables_beyond_feishu_limit(self) -> None:
        """Only renderable tables consume the three-table terminal budget."""
        table = "| Name | Value |\n| --- | --- |\n| A | 1 |"
        fenced = f"```markdown\n{table}\n```"
        content = "\n\n".join([fenced, table, table, table, table])

        terminal = self.module.build_complete_card(content)
        visible = next(
            element["content"]
            for element in terminal["body"]["elements"]
            if element.get("element_id") == "streaming_content"
        )

        self.assertIn(fenced, visible)
        self.assertEqual(visible.count("```\n| Name | Value |"), 1)
        self.assertEqual(
            len(
                list(
                    self.module._CARDKIT_MARKDOWN_TABLE_RE.finditer(
                        self.module._CARDKIT_FENCED_CODE_RE.sub("", visible)
                    )
                )
            ),
            3,
        )

    async def test_image_resolver_deduplicates_and_notifies_one_reflush(self) -> None:
        """Duplicate URLs share one upload and resolve every Markdown reference."""
        image_url = "https://cdn.example.test/image.png?token=private"
        source = (
            f"before ![first]({image_url}) middle "
            f"![second]({image_url}) after"
        )
        upload_started = asyncio.Event()
        release_upload = asyncio.Event()
        uploads: list[str] = []
        notifications: list[str] = []

        async def upload(url: str) -> str:
            """Hold the one upload until both resolve passes are observable."""
            uploads.append(url)
            upload_started.set()
            await release_upload.wait()
            return "img_resolved"

        resolver = self.module.CardKitImageResolver(
            upload,
            on_resolved=lambda: notifications.append("resolved"),
        )
        first = resolver.resolve_images(source)
        second = resolver.resolve_images(source)
        self.assertNotIn(image_url, first)
        self.assertNotIn(image_url, second)

        await upload_started.wait()
        terminal = asyncio.create_task(
            resolver.resolve_images_await(source, timeout_seconds=1)
        )
        release_upload.set()
        resolved = await terminal

        self.assertEqual(uploads, [image_url])
        self.assertEqual(notifications, ["resolved"])
        self.assertNotIn(image_url, resolved)
        self.assertEqual(resolved.count("(img_resolved)"), 2)

    async def test_image_resolver_failure_is_redacted_and_never_retried(self) -> None:
        """A failed remote image stays stripped without exposing its signed URL."""
        image_url = "https://cdn.example.test/fail.png?secret=do-not-log"
        source = f"before ![private]({image_url}) after"
        uploads: list[str] = []
        notifications: list[str] = []

        async def fail_upload(url: str) -> str:
            """Fail with an error that itself contains the sensitive URL."""
            uploads.append(url)
            raise RuntimeError(f"download failed for {url}")

        resolver = self.module.CardKitImageResolver(
            fail_upload,
            on_resolved=lambda: notifications.append("resolved"),
        )
        with self.assertLogs(self.module._LOGGER.name, level="WARNING") as logs:
            resolved = await resolver.resolve_images_await(
                source,
                timeout_seconds=1,
            )
        repeated = resolver.resolve_images(source)

        self.assertEqual(uploads, [image_url])
        self.assertEqual(notifications, [])
        self.assertNotIn(image_url, resolved)
        self.assertNotIn(image_url, repeated)
        self.assertNotIn(image_url, "\n".join(logs.output))

    async def test_image_resolver_timeout_strips_url_then_allows_late_success(
        self,
    ) -> None:
        """Terminal waiting is bounded without cancelling the shared upload."""
        image_url = "https://cdn.example.test/slow.png"
        source = f"![slow]({image_url})"
        release_upload = asyncio.Event()
        resolved_event = asyncio.Event()

        async def slow_upload(_url: str) -> str:
            """Complete only after the bounded terminal wait returns."""
            await release_upload.wait()
            return "img_late"

        resolver = self.module.CardKitImageResolver(
            slow_upload,
            on_resolved=resolved_event.set,
        )
        timed_out = await resolver.resolve_images_await(
            source,
            timeout_seconds=0.001,
        )
        self.assertEqual(timed_out, "")
        self.assertNotIn(image_url, timed_out)

        release_upload.set()
        await asyncio.wait_for(resolved_event.wait(), timeout=1)
        self.assertEqual(resolver.resolve_images(source), "![slow](img_late)")

    async def test_image_resolver_keeps_keys_and_strips_non_remote_sources(
        self,
    ) -> None:
        """Only HTTP URLs can start uploads and existing Feishu keys survive."""
        uploads: list[str] = []

        async def upload(url: str) -> str:
            """Record an unexpected upload candidate."""
            uploads.append(url)
            return "img_unexpected"

        resolver = self.module.CardKitImageResolver(
            upload,
            on_resolved=lambda: None,
        )
        resolved = resolver.resolve_images(
            "![ready](img_existing) ![local](./image.png) "
            "![inline](data:image/png;base64,AAAA)"
        )
        await asyncio.sleep(0)

        self.assertEqual(resolved, "![ready](img_existing)  ")
        self.assertEqual(uploads, [])

    async def test_flush_controller_coalesces_pending_requests(self) -> None:
        """Rapid requests produce one flush and terminal completion cancels timers."""
        calls: list[int] = []
        controller = self.module.CardKitFlushController(
            lambda: self._record_flush(calls),
            throttle_seconds=0.005,
            long_gap_seconds=1,
            batch_after_gap_seconds=0.005,
        )
        controller.mark_ready()
        controller.request()
        controller.request()
        controller.request()
        self.assertTrue(controller.pending)
        await asyncio.sleep(0.02)
        self.assertEqual(calls, [1])

        controller.request()
        await controller.complete()
        self.assertFalse(controller.pending)
        self.assertEqual(calls, [1])

    @staticmethod
    async def _record_flush(calls: list[int]) -> None:
        """Record one flush-controller callback for scheduling tests."""
        calls.append(len(calls) + 1)


if __name__ == "__main__":
    unittest.main()
