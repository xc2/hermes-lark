"""Focused tests for the inbound Feishu media size limit."""

from __future__ import annotations

import asyncio
import io
import sys
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from tests.test_ask_user_question_adapter import _MISSING_MODULE, _load_modules


class MediaMaxMbTests(unittest.TestCase):
    """Verify configuration and pre-cache enforcement for every binary path."""

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

    def _adapter(self, *, limit: int) -> Any:
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter._client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(
                    message_resource=SimpleNamespace(get=object()),
                )
            )
        )
        adapter._media_max_bytes = limit
        adapter._build_message_resource_request = lambda **kwargs: kwargs
        return adapter

    @staticmethod
    def _response(
        payload: bytes,
        *,
        content_type: str,
        file_name: str,
    ) -> Any:
        return SimpleNamespace(
            success=lambda: True,
            file=io.BytesIO(payload),
            file_name=file_name,
            raw=SimpleNamespace(headers={"Content-Type": content_type}),
        )

    def test_defaults_aliases_negative_and_invalid_values(self) -> None:
        load = self.adapter_module.FeishuAdapter._load_settings
        mib = 1024 * 1024

        self.assertEqual(load({}).media_max_bytes, 30 * mib)
        self.assertEqual(
            load({"mediaMaxMb": 2.5}).media_max_bytes,
            int(2.5 * mib),
        )
        self.assertEqual(load({"media_max_mb": 3}).media_max_bytes, 3 * mib)
        self.assertEqual(load({"mediaMaxMb": -1}).media_max_bytes, 0)
        for invalid in ("invalid", True, None, float("nan"), float("inf")):
            with self.subTest(invalid=invalid):
                self.assertEqual(
                    load({"mediaMaxMb": invalid}).media_max_bytes,
                    30 * mib,
                )

    def test_oversized_image_is_rejected_before_cache_write(self) -> None:
        adapter = self._adapter(limit=4)
        response = self._response(
            b"12345",
            content_type="image/png",
            file_name="private-image.png",
        )
        adapter._run_blocking = self._async_value(response)

        with (
            patch.object(
                self.adapter_module,
                "cache_image_from_bytes",
            ) as cache_image,
            self.assertLogs(self.adapter_module.logger, level="WARNING") as logs,
        ):
            result = asyncio.run(
                adapter._download_feishu_image(
                    message_id="private-message-id",
                    image_key="private-image-key",
                )
            )

        self.assertEqual(result, ("", ""))
        cache_image.assert_not_called()
        joined = "\n".join(logs.output)
        self.assertNotIn("private-message-id", joined)
        self.assertNotIn("private-image-key", joined)
        self.assertNotIn("private-image.png", joined)

    def test_oversized_file_is_rejected_before_cache_write(self) -> None:
        adapter = self._adapter(limit=4)
        response = self._response(
            b"12345",
            content_type="application/pdf",
            file_name="private-document.pdf",
        )
        adapter._run_blocking = self._async_value(response)

        with (
            patch.object(
                self.adapter_module,
                "cache_document_from_bytes",
            ) as cache_document,
            self.assertLogs(self.adapter_module.logger, level="WARNING") as logs,
        ):
            result = asyncio.run(
                adapter._download_feishu_message_resource(
                    message_id="private-message-id",
                    file_key="private-file-key",
                    resource_type="file",
                    fallback_filename="private-fallback.pdf",
                )
            )

        self.assertEqual(result, ("", ""))
        cache_document.assert_not_called()
        joined = "\n".join(logs.output)
        self.assertNotIn("private-message-id", joined)
        self.assertNotIn("private-file-key", joined)
        self.assertNotIn("private-document.pdf", joined)
        self.assertNotIn("private-fallback.pdf", joined)

    def test_exact_boundary_is_accepted_for_image_and_file(self) -> None:
        image_adapter = self._adapter(limit=4)
        image_adapter._run_blocking = self._async_value(
            self._response(
                b"1234",
                content_type="image/png",
                file_name="image.png",
            )
        )
        with patch.object(
            self.adapter_module,
            "cache_image_from_bytes",
            new_callable=Mock,
            return_value="/cache/image.png",
        ) as cache_image:
            image_result = asyncio.run(
                image_adapter._download_feishu_image(
                    message_id="om_image",
                    image_key="img_key",
                )
            )

        self.assertEqual(image_result, ("/cache/image.png", "image/png"))
        cache_image.assert_called_once_with(b"1234", ext=".png")

        file_adapter = self._adapter(limit=4)
        file_adapter._run_blocking = self._async_value(
            self._response(
                b"1234",
                content_type="application/pdf",
                file_name="document.pdf",
            )
        )
        with patch.object(
            self.adapter_module,
            "cache_document_from_bytes",
            new_callable=Mock,
            return_value="/cache/document.pdf",
        ) as cache_document:
            file_result = asyncio.run(
                file_adapter._download_feishu_message_resource(
                    message_id="om_file",
                    file_key="file_key",
                    resource_type="file",
                    fallback_filename="fallback.pdf",
                )
            )

        self.assertEqual(
            file_result,
            ("/cache/document.pdf", "application/pdf"),
        )
        cache_document.assert_called_once_with(b"1234", "document.pdf")

    @staticmethod
    def _async_value(value: Any) -> Any:
        async def resolve(*_args: Any, **_kwargs: Any) -> Any:
            return value

        return resolve


if __name__ == "__main__":
    unittest.main()
