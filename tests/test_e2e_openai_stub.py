"""Tests for the deterministic live-E2E model protocol."""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from tests.e2e import openai_stub
from tests.e2e import test_live_thread_model


class E2EOpenAIStubTests(unittest.TestCase):
    """Keep streaming fixtures deterministic and visibly incremental."""

    def _request_stream_events(
        self,
        payload: dict[str, object],
    ) -> list[dict[str, object]]:
        """Request one SSE completion and decode its JSON events."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), openai_stub._Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        request_payload = {**payload, "stream": True}
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            data=json.dumps(request_payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                lines = response.read().decode().splitlines()
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
        return [
            json.loads(line.removeprefix("data: "))
            for line in lines
            if line.startswith("data: {")
        ]

    def test_tool_fixture_streams_reasoning_and_terminal_call_first(self) -> None:
        """The first model round asks Hermes to execute a real terminal tool."""
        marker = "tool-run-123"

        events = self._request_stream_events(
            {
                "model": "hermes-lark-e2e",
                "messages": [
                    {
                        "role": "user",
                        "content": f"HERMES_E2E_TOOL:{marker}",
                    }
                ],
            }
        )

        deltas = [event["choices"][0]["delta"] for event in events]
        finish_reasons = [
            event["choices"][0]["finish_reason"] for event in events
        ]
        self.assertIn(
            f"HERMES_E2E_REASONING:{marker}",
            "".join(str(delta.get("reasoning_content") or "") for delta in deltas),
        )
        tool_calls = [
            call
            for delta in deltas
            for call in delta.get("tool_calls", [])
        ]
        self.assertEqual(tool_calls[0]["type"], "function")
        self.assertEqual(tool_calls[0]["function"]["name"], "terminal")
        arguments = "".join(
            str(call.get("function", {}).get("arguments") or "")
            for call in tool_calls
        )
        self.assertEqual(
            json.loads(arguments),
            {
                "command": (
                    "printf 'HERMES_E2E_TOOL_EXECUTED:"
                    f"{marker}\\n'"
                )
            },
        )
        self.assertEqual(finish_reasons[-1], "tool_calls")

    def test_tool_fixture_finishes_only_after_matching_tool_result(self) -> None:
        """The second model round observes role=tool before streaming its answer."""
        marker = "tool-run-123"

        events = self._request_stream_events(
            {
                "model": "hermes-lark-e2e",
                "messages": [
                    {
                        "role": "user",
                        "content": f"HERMES_E2E_TOOL:{marker}",
                    },
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-test",
                                "type": "function",
                                "function": {
                                    "name": "terminal",
                                    "arguments": json.dumps(
                                        {
                                            "command": (
                                                "printf 'HERMES_E2E_TOOL_EXECUTED:"
                                                f"{marker}\\n'"
                                            )
                                        }
                                    ),
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call-test",
                        "name": "terminal",
                        "content": f"HERMES_E2E_TOOL_EXECUTED:{marker}",
                    },
                ],
            }
        )

        deltas = [event["choices"][0]["delta"] for event in events]
        content = "".join(str(delta.get("content") or "") for delta in deltas)
        self.assertIn(f"HERMES_E2E_TOOL_FINAL:{marker}", content)
        self.assertIn(f"HERMES_E2E_TOOL_RESULT:OBSERVED:{marker}", content)
        self.assertFalse(any(delta.get("tool_calls") for delta in deltas))
        self.assertEqual(events[-1]["choices"][0]["finish_reason"], "stop")

    def test_approval_fixture_requests_removal_then_observes_denial(self) -> None:
        """An isolated sentinel target exercises Hermes' real approval gate."""
        marker = "approval-run-123"
        user_message = {
            "role": "user",
            "content": f"HERMES_E2E_APPROVAL:{marker}",
        }

        first_events = self._request_stream_events(
            {
                "model": "hermes-lark-e2e",
                "messages": [user_message],
            }
        )
        first_deltas = [
            event["choices"][0]["delta"] for event in first_events
        ]
        calls = [
            call
            for delta in first_deltas
            for call in delta.get("tool_calls", [])
        ]
        self.assertEqual(calls[0]["function"]["name"], "terminal")
        self.assertEqual(
            json.loads(
                "".join(
                    str(call.get("function", {}).get("arguments") or "")
                    for call in calls
                )
            ),
            {
                "command": (
                    "rm -rf /opt/data/hermes-lark-e2e-approval-"
                    f"{marker}"
                )
            },
        )
        self.assertEqual(
            first_events[-1]["choices"][0]["finish_reason"],
            "tool_calls",
        )

        final_events = self._request_stream_events(
            {
                "model": "hermes-lark-e2e",
                "messages": [
                    user_message,
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-approval-test",
                                "type": "function",
                                "function": {
                                    "name": "terminal",
                                    "arguments": json.dumps(
                                        {
                                            "command": (
                                                "rm -f /tmp/"
                                                "hermes-lark-e2e-approval-"
                                                f"{marker}"
                                            )
                                        }
                                    ),
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call-approval-test",
                        "name": "terminal",
                        "content": "Command denied by user",
                    },
                ],
            }
        )
        final_content = "".join(
            str(event["choices"][0]["delta"].get("content") or "")
            for event in final_events
        )
        self.assertIn(
            f"HERMES_E2E_APPROVAL_DENIED:{marker}",
            final_content,
        )
        self.assertEqual(
            final_events[-1]["choices"][0]["finish_reason"],
            "stop",
        )

    def test_complex_stream_is_split_into_three_marked_markdown_deltas(self) -> None:
        """The live suite can observe two CardKit writes before completion."""
        marker = "run-123"
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": f"HERMES_E2E_STREAM:{marker}",
                }
            ]
        }

        chunks = openai_stub._response_chunks(payload)

        self.assertEqual(len(chunks), 3)
        self.assertIn(f"HERMES_E2E_STREAM_STAGE_1:{marker}", chunks[0])
        self.assertIn(f"HERMES_E2E_STREAM_STAGE_2:{marker}", chunks[1])
        self.assertIn(f"HERMES_E2E_STREAM_FINAL:{marker}", chunks[2])
        final = "".join(chunks)
        self.assertIn("## Streaming verification", final)
        self.assertIn("| State | Marker |", final)

    def test_cardkit_image_stream_exposes_one_remote_fixture_before_final(
        self,
    ) -> None:
        """The first held delta contains the only remote image reference."""
        marker = "cardkit-image-run-123"
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": f"HERMES_E2E_CARDKIT_IMAGE:{marker}",
                }
            ]
        }

        chunks = openai_stub._response_chunks(payload)

        self.assertEqual(len(chunks), 2)
        self.assertIn(
            f"HERMES_E2E_CARDKIT_IMAGE_STAGE_1:{marker}",
            chunks[0],
        )
        self.assertIn(openai_stub.CARDKIT_E2E_IMAGE_URL, chunks[0])
        self.assertIn(
            f"HERMES_E2E_CARDKIT_IMAGE_FINAL:{marker}",
            chunks[1],
        )
        self.assertNotIn(openai_stub.CARDKIT_E2E_IMAGE_URL, chunks[1])

    def test_cardkit_image_endpoint_serves_the_exact_png_fixture(self) -> None:
        """The isolated gateway can download deterministic valid PNG bytes."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), openai_stub._Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            with urllib.request.urlopen(
                (
                    f"http://127.0.0.1:{server.server_port}"
                    "/e2e/cardkit-image.png"
                ),
                timeout=5,
            ) as response:
                content_type = response.headers.get_content_type()
                image_bytes = response.read()
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

        self.assertEqual(content_type, "image/png")
        self.assertEqual(image_bytes, openai_stub.CARDKIT_E2E_IMAGE_BYTES)
        self.assertTrue(image_bytes.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_normal_reply_remains_one_delta(self) -> None:
        """Ordinary thread scenarios stay fast while streaming is enabled."""
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": "HERMES_E2E_EXPECT:plain-marker",
                }
            ]
        }

        self.assertEqual(
            openai_stub._response_chunks(payload),
            ("HERMES_E2E_EXPECT:plain-marker",),
        )

    def test_native_image_reply_reports_the_exact_payload_digest(self) -> None:
        """The live provider proves that original image bytes reached it."""
        image_bytes = b"deterministic-image-bytes"
        encoded = base64.b64encode(image_bytes).decode()
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": ""},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{encoded}",
                            },
                        },
                    ],
                }
            ]
        }

        self.assertEqual(
            openai_stub._response_chunks(payload),
            (
                "HERMES_E2E_IMAGE_SHA256:"
                f"{hashlib.sha256(image_bytes).hexdigest()}",
            ),
        )

    def test_media_return_reply_emits_exact_gateway_directives(self) -> None:
        """The live gateway receives deterministic local image and file paths."""
        marker = "media-run-123"
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": f"HERMES_E2E_MEDIA_RETURN:{marker}",
                }
            ]
        }

        self.assertEqual(
            openai_stub._response_chunks(payload),
            (
                "\n".join(
                    (
                        f"HERMES_E2E_MEDIA_RETURNED:{marker}",
                        f"MEDIA:/opt/data/e2e-outbound-{marker}.png",
                        f"MEDIA:/opt/data/e2e-outbound-{marker}.txt",
                    )
                ),
            ),
        )

    def test_existing_human_thread_probe_reads_root_and_pending_history(
        self,
    ) -> None:
        """The provider proves both existing-thread context sources arrived."""
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": "HERMES_E2E_EXISTING_ROOT:root-marker",
                },
                {
                    "role": "user",
                    "content": "HERMES_E2E_EXISTING_HISTORY:history-marker",
                },
                {
                    "role": "user",
                    "content": "HERMES_E2E_EXISTING_CONTEXT_PROBE",
                },
            ]
        }

        self.assertEqual(
            openai_stub._response_chunks(payload),
            (
                "HERMES_E2E_EXISTING_CONTEXT:"
                "ROOT=YES;HISTORY=YES",
            ),
        )

    def test_long_output_is_losslessly_split_into_filterable_deltas(self) -> None:
        """The live suite can reassemble a sizeable exact Markdown response."""
        marker = "long-run-123"
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": f"HERMES_E2E_LONG:{marker}",
                }
            ]
        }

        expected = openai_stub._long_response_text(marker)
        chunks = openai_stub._response_chunks(payload)

        self.assertGreaterEqual(len(chunks), 3)
        self.assertTrue(all(chunks))
        self.assertEqual("".join(chunks), expected)
        self.assertGreaterEqual(len(expected), 2400)
        self.assertLessEqual(len(expected), 2600)
        self.assertIn("## Long output verification", expected)
        self.assertIn("**Lossless ordered delivery**", expected)
        self.assertIn(f"HERMES_E2E_LONG_RESULT:{marker}", expected)
        self.assertEqual(expected.count(marker), 1)

    def test_stream_barrier_releases_each_stage_explicitly(self) -> None:
        """The live observer, rather than timing, controls every next delta."""
        marker = "barrier-run-123"
        released = threading.Event()
        openai_stub._start_stream(marker)

        waiter = threading.Thread(
            target=lambda: (
                openai_stub._wait_for_stream_stage(marker, 2),
                released.set(),
            )
        )
        waiter.start()
        try:
            self.assertFalse(released.wait(0.05))
            self.assertTrue(openai_stub._advance_stream(marker, 2))
            self.assertTrue(released.wait(1))
        finally:
            openai_stub._finish_stream(marker)
            waiter.join(timeout=1)

    def test_delay_barrier_is_observable_and_released_over_http(self) -> None:
        """A fail-closed model response waits for an explicit HTTP release."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), openai_stub._Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        marker = "fail-closed-run-123"
        completion: dict[str, object] = {}
        completion_done = threading.Event()

        def request_json(method: str, path: str, body: object | None = None) -> dict:
            payload = None if body is None else json.dumps(body).encode()
            request = urllib.request.Request(
                f"{base_url}{path}",
                data=payload,
                headers={"Content-Type": "application/json"},
                method=method,
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.loads(response.read().decode())

        def request_completion() -> None:
            try:
                completion["response"] = request_json(
                    "POST",
                    "/v1/chat/completions",
                    {
                        "model": "hermes-lark-e2e",
                        "messages": [
                            {
                                "role": "user",
                                "content": (
                                    f"HERMES_E2E_DELAY_BARRIER:{marker}\n"
                                    "HERMES_E2E_EXPECT:released-response"
                                ),
                            }
                        ],
                    },
                )
            except BaseException as error:
                completion["error"] = error
            finally:
                completion_done.set()

        completion_thread = threading.Thread(target=request_completion, daemon=True)
        completion_thread.start()
        try:
            deadline = time.monotonic() + 2
            active = False
            while time.monotonic() < deadline:
                status = request_json(
                    "GET",
                    f"/e2e/delay-barrier-active?marker={marker}",
                )
                active = status.get("active") is True
                if active:
                    break
                time.sleep(0.01)

            self.assertTrue(active)
            self.assertFalse(completion_done.is_set())
            self.assertEqual(
                request_json(
                    "POST",
                    f"/e2e/delay-barrier-release?marker={marker}",
                ),
                {"released": True},
            )
            self.assertTrue(completion_done.wait(2))
            self.assertNotIn("error", completion)
            response = completion["response"]
            self.assertIsInstance(response, dict)
            self.assertEqual(
                response["choices"][0]["message"]["content"],
                "HERMES_E2E_EXPECT:released-response",
            )
            self.assertEqual(
                request_json(
                    "GET",
                    f"/e2e/delay-barrier-active?marker={marker}",
                ),
                {"active": False},
            )
        finally:
            try:
                request_json(
                    "POST",
                    f"/e2e/delay-barrier-release?marker={marker}",
                )
            except Exception:
                pass
            completion_thread.join(timeout=2)
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)


class CardKitTraceFixtureTests(unittest.TestCase):
    """Keep the live CardKit observer strict and independent of IM caching."""

    def test_trace_reader_filters_blank_lines_and_flattens_payloads(self) -> None:
        """Successful CardKit writes remain observable as append-only JSONL."""
        with tempfile.TemporaryDirectory() as directory:
            trace_path = Path(directory) / "cardkit-trace.jsonl"
            trace_path.write_text(
                "\n".join(
                    (
                        json.dumps(
                            {
                                "operation": "content",
                                "status": "generating",
                                "ok": True,
                                "sequence": 2,
                                "content": "stage one",
                            }
                        ),
                        "",
                        json.dumps(
                            {
                                "operation": "update",
                                "status": "complete",
                                "ok": True,
                                "sequence": 4,
                                "card": {
                                    "config": {"summary": {"content": ""}},
                                    "body": {
                                        "elements": [
                                            {
                                                "tag": "markdown",
                                                "content": "final marker",
                                            }
                                        ]
                                    },
                                },
                            }
                        ),
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            entries = test_live_thread_model._read_cardkit_trace(trace_path)

        self.assertEqual(
            [entry["operation"] for entry in entries],
            ["content", "update"],
        )
        self.assertEqual(
            test_live_thread_model._cardkit_trace_text(entries[0]),
            "stage one",
        )
        self.assertEqual(
            test_live_thread_model._cardkit_trace_state(entries[0]),
            "generating",
        )
        final_text = test_live_thread_model._cardkit_trace_text(entries[1])
        self.assertNotIn("Complete", final_text)
        self.assertIn("final marker", final_text)


if __name__ == "__main__":
    unittest.main()
