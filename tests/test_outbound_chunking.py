"""Focused tests for OpenClaw-compatible outbound chunk settings."""

from __future__ import annotations

import sys
import unittest
from typing import Any

from tests.test_ask_user_question_adapter import _MISSING_MODULE, _load_modules


class OutboundChunkingTests(unittest.TestCase):
    """Verify pinned openclaw-lark chunk names and runtime behavior."""

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

    def _adapter(self, *, limit: int, mode: str) -> Any:
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter._text_chunk_limit = limit
        adapter._chunk_mode = mode
        adapter.truncate_message = lambda text, size: [
            text[index : index + size]
            for index in range(0, len(text), size)
        ]
        return adapter

    def test_defaults_aliases_and_invalid_values(self) -> None:
        """Default and configured values match OpenClaw's reply fallback."""
        load = self.adapter_module.FeishuAdapter._load_settings

        self.assertEqual(load({}).text_chunk_limit, 4000)
        self.assertEqual(load({}).chunk_mode, "none")
        self.assertEqual(load({"textChunkLimit": 1200}).text_chunk_limit, 1200)
        self.assertEqual(load({"text_chunk_limit": 900}).text_chunk_limit, 900)
        for mode in ("newline", "paragraph", "none"):
            with self.subTest(mode=mode):
                self.assertEqual(load({"chunkMode": mode}).chunk_mode, mode)
        for invalid in (0, -1, True, "invalid", float("inf")):
            with self.subTest(invalid=invalid):
                self.assertEqual(
                    load({"textChunkLimit": invalid}).text_chunk_limit,
                    4000,
                )
        for invalid_mode in ("length", "other", ""):
            with self.subTest(invalid_mode=invalid_mode):
                self.assertEqual(
                    load({"chunkMode": invalid_mode}).chunk_mode,
                    "none",
                )

    def test_paragraph_and_none_use_the_pinned_length_fallback(self) -> None:
        """The two non-newline schema values follow OpenClaw's fallback quirk."""
        for mode in ("paragraph", "none"):
            with self.subTest(mode=mode):
                adapter = self._adapter(limit=4, mode=mode)

                self.assertEqual(
                    adapter._chunk_outbound_text("abcdefghij"),
                    ["abcd", "efgh", "ij"],
                )

    def test_newline_mode_splits_paragraphs_before_length(self) -> None:
        """Blank-line paragraphs are emitted separately before long splitting."""
        adapter = self._adapter(limit=6, mode="newline")

        self.assertEqual(
            adapter._chunk_outbound_text("alpha\n\nbravo charlie"),
            ["alpha", "bravo ", "charli", "e"],
        )

    def test_newline_mode_keeps_blank_lines_inside_code_fences(self) -> None:
        """Paragraph splitting never breaks a fenced code block."""
        adapter = self._adapter(limit=100, mode="newline")
        content = "```python\none\n\ntwo\n```\n\nafter"

        self.assertEqual(
            adapter._chunk_outbound_text(content),
            ["```python\none\n\ntwo\n```", "after"],
        )

    def test_migrated_openclaw_chunk_modes_remain_unchanged(self) -> None:
        """OpenClaw root and account chunk modes survive YAML migration."""
        for mode in ("newline", "paragraph", "none"):
            with self.subTest(mode=mode):
                normalized = self.adapter_module._apply_yaml_config(
                    {},
                    {
                        "appId": "cli_root",
                        "appSecret": "root-secret",
                        "chunkMode": mode,
                        "accounts": {
                            "work": {
                                "appId": "cli_work",
                                "appSecret": "work-secret",
                                "chunkMode": mode,
                            }
                        },
                    },
                )

                self.assertIsNotNone(normalized)
                assert normalized is not None
                self.assertEqual(normalized["chunk_mode"], mode)
                self.assertEqual(
                    normalized["accounts"]["work"]["chunkMode"],
                    mode,
                )
                self.assertEqual(
                    self.adapter_module.FeishuAdapter._load_settings(
                        normalized["accounts"]["work"]
                    ).chunk_mode,
                    mode,
                )


if __name__ == "__main__":
    unittest.main()
