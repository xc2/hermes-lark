"""Regression tests for account-isolated Feishu outbound routing."""

from __future__ import annotations

import asyncio
import sys
import unittest
from types import SimpleNamespace
from typing import Any

from tests.test_ask_user_question_adapter import _MISSING_MODULE, _load_modules


class MultiAccountOutboundTests(unittest.TestCase):
    """Verify child adapters never send Hermes namespaced chat IDs to Feishu."""

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

    def _new_adapter(self) -> tuple[Any, list[dict[str, Any]]]:
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter._account_id = "work"
        adapter._namespace_account = True
        create = object()
        adapter._client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(
                    message=SimpleNamespace(create=create),
                )
            )
        )
        adapter._build_create_message_body = lambda **kwargs: kwargs
        adapter._build_create_message_request = (
            lambda receive_id_type, body: {
                "receive_id_type": receive_id_type,
                "body": body,
            }
        )
        captured: list[dict[str, Any]] = []

        async def run_blocking(method: Any, request: dict[str, Any]) -> Any:
            self.assertIs(method, create)
            captured.append(request)
            return SimpleNamespace(success=lambda: True)

        adapter._run_blocking = run_blocking
        return adapter, captured

    def test_matching_account_namespace_is_removed_at_api_boundary(self) -> None:
        """A child receives the internal ID but Feishu receives the raw chat ID."""
        adapter, captured = self._new_adapter()

        asyncio.run(
            adapter._send_raw_message(
                chat_id="work::oc_chat",
                msg_type="file",
                payload='{"file_key":"file-key"}',
                reply_to=None,
                metadata=None,
            )
        )

        self.assertEqual(captured[0]["receive_id_type"], "chat_id")
        self.assertEqual(captured[0]["body"]["receive_id"], "oc_chat")

    def test_raw_and_other_account_chat_ids_are_not_rewritten(self) -> None:
        """Only the exact current-account prefix is safe to strip."""
        adapter, captured = self._new_adapter()

        for chat_id in ("oc_chat", "other::oc_chat"):
            asyncio.run(
                adapter._send_raw_message(
                    chat_id=chat_id,
                    msg_type="text",
                    payload='{"text":"hello"}',
                    reply_to=None,
                    metadata=None,
                )
            )

        self.assertEqual(
            [request["body"]["receive_id"] for request in captured],
            ["oc_chat", "other::oc_chat"],
        )


if __name__ == "__main__":
    unittest.main()
