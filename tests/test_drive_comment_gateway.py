"""Focused tests for Drive comments routed through the Hermes gateway."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from tests.test_ask_user_question_adapter import (
    PACKAGE_DIR,
    _MISSING_MODULE,
    _load_modules,
    _load_package_module,
)


class DriveCommentGatewayTests(unittest.IsolatedAsyncioTestCase):
    """Verify gateway admission, session identity, and comment delivery."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.previous_comment_module = sys.modules.get(
            "hermes_lark.feishu_comment",
            _MISSING_MODULE,
        )
        cls.tools, cls.adapter_module, cls.previous_modules = _load_modules()
        cls.comment_module = _load_package_module(
            "hermes_lark.feishu_comment",
            PACKAGE_DIR / "feishu_comment.py",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.previous_comment_module is _MISSING_MODULE:
            sys.modules.pop("hermes_lark.feishu_comment", None)
        else:
            sys.modules[
                "hermes_lark.feishu_comment"
            ] = cls.previous_comment_module
        for name, previous in cls.previous_modules.items():
            if previous is _MISSING_MODULE:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    def _new_adapter(self, *, dm_policy: str = "pairing") -> Any:
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter.platform = self.adapter_module.Platform.FEISHU
        adapter._account_id = "work"
        adapter._namespace_account = True
        adapter._client = object()
        adapter._bot_open_id = "ou_bot"
        adapter._bot_user_id = ""
        adapter._dm_policy = dm_policy
        adapter._allowed_group_users = set()
        adapter._drive_comment_failed_targets = set()
        adapter._pending_processing_reactions = {}
        adapter._is_duplicate = Mock(return_value=False)
        adapter._resolve_sender_profile = AsyncMock(
            return_value={
                "user_id": "ou_user",
                "user_name": "Commenter",
                "user_id_alt": None,
            }
        )
        adapter._handle_message_with_guards = AsyncMock()
        adapter._reactions_enabled = lambda: True
        return adapter

    @staticmethod
    def _event() -> Any:
        return SimpleNamespace(
            header=SimpleNamespace(event_id="evt_comment_1"),
            event={
                "comment_id": "comment_1",
                "reply_id": "reply_1",
                "is_mentioned": True,
                "notice_meta": {
                    "file_token": "doc_token",
                    "file_type": "docx",
                    "notice_type": "add_reply",
                    "from_user_id": {"open_id": "ou_user"},
                    "to_user_id": {"open_id": "ou_bot"},
                },
            },
        )

    async def test_api_logs_only_request_shape_and_response_size(self) -> None:
        """Request bodies and raw response content stay out of diagnostics."""
        request_content = "confidential-request-content"
        response_content = "confidential-response-content"
        raw_response = (
            '{"data":{"document":"' + response_content + '"}}'
        )
        response = SimpleNamespace(
            code=230001,
            msg="request rejected",
            data=None,
            raw=SimpleNamespace(content=raw_response),
        )
        client = SimpleNamespace(request=Mock(return_value=response))

        with (
            patch.object(
                self.comment_module,
                "_build_request",
                return_value=object(),
            ),
            self.assertLogs(
                self.comment_module.logger,
                level="INFO",
            ) as captured,
        ):
            code, _message, data = await self.comment_module._exec_request(
                client,
                "POST",
                "/open-apis/example",
                paths={"comment_id": "comment_1"},
                body={"content": {"text": request_content}},
            )

        output = "\n".join(captured.output)
        self.assertEqual(code, 230001)
        self.assertEqual(data, {"document": response_content})
        self.assertIn("comment_1", output)
        self.assertIn("body_keys=['content']", output)
        self.assertIn("code=230001", output)
        self.assertIn(f"response_bytes={len(raw_response)}", output)
        self.assertNotIn(request_content, output)
        self.assertNotIn(response_content, output)

    async def test_comment_context_logs_only_counts_and_identifiers(self) -> None:
        """Document titles, URLs, quotes, replies, and prompts are not logged."""
        adapter = self._new_adapter()
        sensitive_title = "Confidential acquisition plan"
        sensitive_url = "https://example.test/private-document"
        sensitive_quote = "Private quoted paragraph"
        sensitive_reply = "Private reply from the document"
        reply = {
            "reply_id": "reply_1",
            "user_id": {"open_id": "ou_user"},
            "content": {
                "elements": [
                    {
                        "type": "text_run",
                        "text_run": {"text": sensitive_reply},
                    }
                ]
            },
        }

        with (
            patch.object(
                self.comment_module,
                "query_document_meta",
                AsyncMock(
                    return_value={
                        "title": sensitive_title,
                        "url": sensitive_url,
                    }
                ),
            ),
            patch.object(
                self.comment_module,
                "batch_query_comment",
                AsyncMock(
                    return_value={
                        "is_whole": False,
                        "quote": sensitive_quote,
                    }
                ),
            ),
            patch.object(
                self.comment_module,
                "list_comment_replies",
                AsyncMock(return_value=[reply]),
            ),
            self.assertLogs(
                self.comment_module.logger,
                level="DEBUG",
            ) as captured,
        ):
            await self.comment_module.handle_drive_comment_event(
                adapter,
                self._event(),
            )

        output = "\n".join(captured.output)
        self.assertIn("comment=comment_1", output)
        self.assertIn("quote_chars=24", output)
        self.assertIn("root_chars=31", output)
        self.assertIn("target_chars=31", output)
        self.assertIn("Prompt built", output)
        for sensitive in (
            sensitive_title,
            sensitive_url,
            sensitive_quote,
            sensitive_reply,
        ):
            self.assertNotIn(sensitive, output)

    async def test_outbound_comment_logs_only_text_lengths(self) -> None:
        """Outbound comment bodies are represented by character counts only."""
        reply_content = "confidential reply body"
        whole_content = "confidential whole-comment body"
        request = AsyncMock(return_value=(0, "ok", {}))

        with (
            patch.object(self.comment_module, "_exec_request", request),
            self.assertLogs(
                self.comment_module.logger,
                level="INFO",
            ) as captured,
        ):
            await self.comment_module.reply_to_comment(
                object(),
                "file_1",
                "docx",
                "comment_1",
                reply_content,
            )
            await self.comment_module.add_whole_comment(
                object(),
                "file_1",
                "docx",
                whole_content,
            )

        output = "\n".join(captured.output)
        self.assertIn(f"text_chars={len(reply_content)}", output)
        self.assertIn(f"text_chars={len(whole_content)}", output)
        self.assertIn("comment_id=comment_1", output)
        self.assertIn("file_token=file_1", output)
        self.assertNotIn(reply_content, output)
        self.assertNotIn(whole_content, output)

    async def test_comment_enters_account_scoped_gateway_session(self) -> None:
        adapter = self._new_adapter()
        reply = {
            "reply_id": "reply_1",
            "user_id": {"open_id": "ou_user"},
            "content": {
                "elements": [
                    {
                        "type": "text_run",
                        "text_run": {"text": "Please explain this"},
                    }
                ]
            },
        }

        with (
            patch.object(
                self.comment_module,
                "query_document_meta",
                AsyncMock(
                    return_value={
                        "title": "Design",
                        "url": "https://example.test/doc",
                    }
                ),
            ),
            patch.object(
                self.comment_module,
                "batch_query_comment",
                AsyncMock(return_value={"is_whole": False, "quote": "Quoted"}),
            ),
            patch.object(
                self.comment_module,
                "list_comment_replies",
                AsyncMock(return_value=[reply]),
            ),
        ):
            await self.comment_module.handle_drive_comment_event(
                adapter,
                self._event(),
            )

        adapter._handle_message_with_guards.assert_awaited_once()
        gateway_event = adapter._handle_message_with_guards.await_args.args[0]
        self.assertIn("Please explain this", gateway_event.text)
        self.assertEqual(gateway_event.source.chat_type, "dm")
        self.assertEqual(gateway_event.source.user_id, "work::ou_user")
        self.assertEqual(gateway_event.source.scope_id, "work")
        self.assertEqual(gateway_event.source.thread_id, "comment_1")
        self.assertFalse(gateway_event.source.role_authorized)
        self.assertTrue(
            gateway_event.source.chat_id.startswith(
                "work::feishu-comment:"
            )
        )
        self.assertEqual(
            gateway_event.metadata["feishu_drive_comment"]["sender_open_id"],
            "ou_user",
        )

        ticket = self.tools.ticket_from_event(gateway_event)
        self.assertEqual(ticket.account_id, "work")
        self.assertEqual(ticket.sender_open_id, "ou_user")
        self.assertEqual(ticket.chat_type, "p2p")
        self.assertEqual(ticket.thread_id, "comment_1")

    async def test_comment_respects_dm_admission_before_fetch(self) -> None:
        adapter = self._new_adapter(dm_policy="disabled")
        query_meta = AsyncMock()
        query_comment = AsyncMock()

        with (
            patch.object(
                self.comment_module,
                "query_document_meta",
                query_meta,
            ),
            patch.object(
                self.comment_module,
                "batch_query_comment",
                query_comment,
            ),
        ):
            await self.comment_module.handle_drive_comment_event(
                adapter,
                self._event(),
            )

        query_meta.assert_not_awaited()
        query_comment.assert_not_awaited()
        adapter._handle_message_with_guards.assert_not_awaited()

    async def test_stream_progress_is_suppressed_and_final_is_delivered(self) -> None:
        adapter = self._new_adapter()
        target = self.comment_module.build_drive_comment_chat_id(
            file_token="doc_token",
            file_type="docx",
            comment_id="comment_1",
            is_whole=False,
        )
        deliver = AsyncMock(return_value=True)

        with patch.object(
            self.comment_module,
            "deliver_comment_reply",
            deliver,
        ):
            preview = await adapter.send(
                target,
                "partial",
                metadata={
                    "thread_id": "comment_1",
                    "expect_edits": True,
                },
            )
            progress = await adapter.send(
                target,
                "tool progress",
                metadata={"thread_id": "comment_1"},
            )
            final = await adapter.send(
                target,
                "final answer",
                metadata={"thread_id": "comment_1", "notify": True},
            )

        self.assertFalse(preview.success)
        self.assertIsNone(preview.message_id)
        self.assertFalse(progress.success)
        self.assertTrue(final.success)
        deliver.assert_awaited_once_with(
            adapter._client,
            "doc_token",
            "docx",
            "comment_1",
            "final answer",
            False,
        )

    async def test_pairing_notice_routes_without_delivery_metadata(self) -> None:
        adapter = self._new_adapter()
        target = self.comment_module.build_drive_comment_chat_id(
            file_token="doc_token",
            file_type="docx",
            comment_id="comment_1",
            is_whole=False,
        )
        deliver = AsyncMock(return_value=True)

        with patch.object(
            self.comment_module,
            "deliver_comment_reply",
            deliver,
        ):
            result = await adapter.send(target, "Pairing code: 123456")

        self.assertTrue(result.success)
        deliver.assert_awaited_once()

    async def test_comment_lifecycle_uses_comment_reactions_and_error_send(
        self,
    ) -> None:
        adapter = self._new_adapter()
        target = self.comment_module.build_drive_comment_chat_id(
            file_token="doc_token",
            file_type="docx",
            comment_id="comment_1",
            is_whole=False,
        )
        event = SimpleNamespace(
            source=SimpleNamespace(
                chat_id=f"work::{target}",
                chat_id_alt=target,
                thread_id="comment_1",
            ),
            message_id="reply_1",
            reply_to_message_id=None,
            metadata={
                "feishu_drive_comment": {
                    "reply_id": "reply_1",
                    "sender_open_id": "ou_user",
                }
            },
        )
        adapter._add_reaction = AsyncMock()
        adapter._remove_reaction = AsyncMock()
        add_comment_reaction = AsyncMock(return_value=True)
        delete_comment_reaction = AsyncMock(return_value=True)
        deliver = AsyncMock(return_value=True)

        with (
            patch.object(
                self.comment_module,
                "add_comment_reaction",
                add_comment_reaction,
            ),
            patch.object(
                self.comment_module,
                "delete_comment_reaction",
                delete_comment_reaction,
            ),
            patch.object(
                self.comment_module,
                "deliver_comment_reply",
                deliver,
            ),
        ):
            await adapter.on_processing_start(event)
            await adapter.on_processing_complete(
                event,
                self.adapter_module.ProcessingOutcome.FAILURE,
            )
            result = await adapter.send(
                f"work::{target}",
                "Sorry, processing failed",
                metadata={"thread_id": "comment_1"},
            )

        self.assertTrue(result.success)
        add_comment_reaction.assert_awaited_once()
        delete_comment_reaction.assert_awaited_once()
        adapter._add_reaction.assert_not_awaited()
        adapter._remove_reaction.assert_not_awaited()
        deliver.assert_awaited_once()
        self.assertFalse(adapter._drive_comment_failed_targets)

    def test_comment_module_does_not_create_a_second_agent(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "hermes_lark"
            / "feishu_comment.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("AIAgent", source)
        self.assertNotIn("_run_comment_agent", source)


if __name__ == "__main__":
    unittest.main()
