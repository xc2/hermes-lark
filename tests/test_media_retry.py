"""Focused parity tests for transient media-download retries."""

from __future__ import annotations

import asyncio
import io
import sys
import types
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from tests.test_ask_user_question_adapter import _MISSING_MODULE, _load_modules


class _HttpError(RuntimeError):
    """HTTP-shaped error used without coupling tests to one client library."""

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"HTTP {status}")


class MediaRetryTests(unittest.TestCase):
    """Verify upstream's two bounded retries and permanent-error behavior."""

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

    def _adapter(self) -> Any:
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter._client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(
                    message_resource=SimpleNamespace(get=object()),
                )
            )
        )
        adapter._media_max_bytes = 1024
        adapter._build_message_resource_request = lambda **values: values
        return adapter

    @staticmethod
    def _response(payload: bytes, file_name: str, content_type: str) -> Any:
        return SimpleNamespace(
            success=lambda: True,
            file=io.BytesIO(payload),
            file_name=file_name,
            raw=SimpleNamespace(headers={"Content-Type": content_type}),
        )

    def test_image_download_retries_503_then_succeeds(self) -> None:
        """An image is not lost after one transient SDK failure."""
        adapter = self._adapter()
        response = self._response(b"png", "image.png", "image/png")
        adapter._run_blocking = AsyncMock(
            side_effect=[_HttpError(503), response]
        )

        with (
            patch.object(
                self.adapter_module.asyncio,
                "sleep",
                new_callable=AsyncMock,
            ) as sleep,
            patch.object(
                self.adapter_module,
                "cache_image_from_bytes",
                new=Mock(return_value="/cache/image.png"),
            ),
        ):
            result = asyncio.run(
                adapter._download_feishu_image(
                    message_id="om_image",
                    image_key="img_key",
                )
            )

        self.assertEqual(result, ("/cache/image.png", "image/png"))
        self.assertEqual(adapter._run_blocking.await_count, 2)
        sleep.assert_awaited_once_with(1.0)

    def test_resource_download_retries_twice_then_succeeds(self) -> None:
        """The exact 1s and 2s upstream schedule is bounded at two retries."""
        adapter = self._adapter()
        response = self._response(
            b"pdf",
            "document.pdf",
            "application/pdf",
        )
        adapter._run_blocking = AsyncMock(
            side_effect=[_HttpError(502), _HttpError(504), response]
        )

        with (
            patch.object(
                self.adapter_module.asyncio,
                "sleep",
                new_callable=AsyncMock,
            ) as sleep,
            patch.object(
                self.adapter_module,
                "cache_document_from_bytes",
                new=Mock(return_value="/cache/document.pdf"),
            ),
        ):
            result = asyncio.run(
                adapter._download_feishu_message_resource(
                    message_id="om_file",
                    file_key="file_key",
                    resource_type="file",
                    fallback_filename="fallback.pdf",
                )
            )

        self.assertEqual(
            result,
            ("/cache/document.pdf", "application/pdf"),
        )
        self.assertEqual(adapter._run_blocking.await_count, 3)
        self.assertEqual(
            [call.args for call in sleep.await_args_list],
            [(1.0,), (2.0,)],
        )

    def test_permanent_resource_error_is_not_retried(self) -> None:
        """A non-transient SDK failure falls through without a delay."""
        adapter = self._adapter()
        adapter._run_blocking = AsyncMock(side_effect=_HttpError(400))

        with patch.object(
            self.adapter_module.asyncio,
            "sleep",
            new_callable=AsyncMock,
        ) as sleep:
            result = asyncio.run(
                adapter._download_feishu_message_resource(
                    message_id="om_file",
                    file_key="file_key",
                    resource_type="file",
                    fallback_filename="fallback.pdf",
                )
            )

        self.assertEqual(result, ("", ""))
        self.assertEqual(adapter._run_blocking.await_count, 1)
        sleep.assert_not_awaited()

    def test_remote_document_retries_without_bypassing_ssrf_client(self) -> None:
        """Remote documents retry inside the existing SSRF-safe context."""
        adapter = self._adapter()

        class Response:
            """Stream one bounded response body."""

            headers = {"Content-Type": "application/pdf"}

            @staticmethod
            def raise_for_status() -> None:
                return None

            async def aiter_bytes(self, *, chunk_size: int) -> Any:
                self.chunk_size = chunk_size
                yield b"pdf"

        class StreamContext:
            """Raise or return one HTTP stream response."""

            def __init__(self, value: Any) -> None:
                self.value = value

            async def __aenter__(self) -> Any:
                if isinstance(self.value, Exception):
                    raise self.value
                return self.value

            async def __aexit__(self, *_args: Any) -> None:
                return None

        response = Response()
        client = SimpleNamespace(
            stream=Mock(
                side_effect=[
                    StreamContext(_HttpError(504)),
                    StreamContext(response),
                ]
            )
        )

        class ClientContext:
            """Async context manager returning the test HTTP client."""

            async def __aenter__(self) -> Any:
                return client

            async def __aexit__(self, *_args: Any) -> None:
                return None

        url_safety = types.ModuleType("tools.url_safety")
        url_safety.is_safe_url = Mock(return_value=True)
        url_safety.create_ssrf_safe_async_client = Mock(
            return_value=ClientContext()
        )
        base = sys.modules["gateway.platforms.base"]
        base._ssrf_redirect_guard = Mock()

        with (
            patch.dict(sys.modules, {"tools.url_safety": url_safety}),
            patch.object(
                self.adapter_module.asyncio,
                "sleep",
                new_callable=AsyncMock,
            ) as sleep,
            patch.object(
                self.adapter_module,
                "cache_document_from_bytes",
                new=Mock(return_value="/cache/document.pdf"),
            ),
        ):
            result = asyncio.run(
                adapter._download_remote_document(
                    "https://cdn.example.test/document.pdf",
                    default_ext=".pdf",
                    preferred_name="document.pdf",
                )
        )

        self.assertEqual(result, ("/cache/document.pdf", "document.pdf"))
        self.assertEqual(client.stream.call_count, 2)
        sleep.assert_awaited_once_with(1.0)
        url_safety.is_safe_url.assert_called_once()

    def test_remote_document_rejects_declared_or_streamed_oversize(self) -> None:
        """Remote document downloads stop before buffering beyond mediaMaxMb."""
        adapter = self._adapter()
        adapter._media_max_bytes = 4

        class Response:
            """Expose a configurable length and streamed body."""

            def __init__(self, declared: str, chunks: list[bytes]) -> None:
                self.headers = {
                    "Content-Type": "application/pdf",
                    "Content-Length": declared,
                }
                self.chunks = chunks
                self.iterated = False

            @staticmethod
            def raise_for_status() -> None:
                return None

            async def aiter_bytes(self, *, chunk_size: int) -> Any:
                self.iterated = True
                self.chunk_size = chunk_size
                for chunk in self.chunks:
                    yield chunk

        class StreamContext:
            """Return one response through an async context manager."""

            def __init__(self, response: Response) -> None:
                self.response = response

            async def __aenter__(self) -> Response:
                return self.response

            async def __aexit__(self, *_args: Any) -> None:
                return None

        url_safety = types.ModuleType("tools.url_safety")
        url_safety.is_safe_url = Mock(return_value=True)
        base = sys.modules["gateway.platforms.base"]
        base._ssrf_redirect_guard = Mock()

        for response in (
            Response("5", [b"must-not-read"]),
            Response("", [b"1234", b"5"]),
        ):
            with self.subTest(declared=response.headers["Content-Length"]):
                client = SimpleNamespace(
                    stream=Mock(return_value=StreamContext(response))
                )

                class ClientContext:
                    """Return the current bounded-stream test client."""

                    async def __aenter__(self) -> Any:
                        return client

                    async def __aexit__(self, *_args: Any) -> None:
                        return None

                url_safety.create_ssrf_safe_async_client = Mock(
                    return_value=ClientContext()
                )
                with (
                    patch.dict(sys.modules, {"tools.url_safety": url_safety}),
                    patch.object(
                        self.adapter_module,
                        "cache_document_from_bytes",
                    ) as cache_document,
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "mediaMaxMb limit",
                    ):
                        asyncio.run(
                            adapter._download_remote_document(
                                "https://cdn.example.test/document.pdf",
                                default_ext=".pdf",
                                preferred_name="document.pdf",
                            )
                        )

                cache_document.assert_not_called()
                if response.headers["Content-Length"] == "5":
                    self.assertFalse(response.iterated)


if __name__ == "__main__":
    unittest.main()
