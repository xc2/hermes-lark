"""Deterministic OpenAI-compatible model used by the Docker E2E gateway."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


_EXPECT_RE = re.compile(r"HERMES_E2E_EXPECT:([A-Za-z0-9_.:-]+)")
_REMEMBER_RE = re.compile(r"HERMES_E2E_REMEMBER:([A-Za-z0-9_.:-]+)")
_DELAY_BARRIER_RE = re.compile(
    r"HERMES_E2E_DELAY_BARRIER:([A-Za-z0-9_.:-]+)"
)
_LONG_RE = re.compile(r"HERMES_E2E_LONG:([A-Za-z0-9_.:-]+)")
_STREAM_RE = re.compile(r"HERMES_E2E_STREAM:([A-Za-z0-9_.:-]+)")
_CARDKIT_IMAGE_RE = re.compile(
    r"HERMES_E2E_CARDKIT_IMAGE:([A-Za-z0-9_.:-]+)"
)
_TOOL_RE = re.compile(r"HERMES_E2E_TOOL:([A-Za-z0-9_.:-]+)")
_APPROVAL_RE = re.compile(r"HERMES_E2E_APPROVAL:([A-Za-z0-9_.:-]+)")
_MEDIA_RETURN_RE = re.compile(
    r"HERMES_E2E_MEDIA_RETURN:([A-Za-z0-9_.:-]+)"
)
_REQUEST_STARTS_LIMIT = 1000
_DELAY_BARRIERS: OrderedDict[str, threading.Event] = OrderedDict()
_DELAY_BARRIERS_LOCK = threading.Lock()
_STREAM_STAGES: OrderedDict[str, int] = OrderedDict()
_STREAM_STAGES_CONDITION = threading.Condition()


# Valid one-pixel PNG served to the gateway for CardKit upload coverage.
CARDKIT_E2E_IMAGE_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
CARDKIT_E2E_IMAGE_URL = "http://model-stub:8000/e2e/cardkit-image.png"


def _wait_for_delay_barrier(marker: str) -> bool:
    """Register one active request and wait for its explicit release."""
    release = threading.Event()
    with _DELAY_BARRIERS_LOCK:
        previous = _DELAY_BARRIERS.pop(marker, None)
        if previous is not None:
            previous.set()
        _DELAY_BARRIERS[marker] = release
        while len(_DELAY_BARRIERS) > _REQUEST_STARTS_LIMIT:
            _, evicted = _DELAY_BARRIERS.popitem(last=False)
            evicted.set()
    try:
        return release.wait(timeout=120)
    finally:
        with _DELAY_BARRIERS_LOCK:
            if _DELAY_BARRIERS.get(marker) is release:
                _DELAY_BARRIERS.pop(marker, None)


def _delay_barrier_active(marker: str) -> bool:
    """Return whether one marked model request is waiting for release."""
    with _DELAY_BARRIERS_LOCK:
        return marker in _DELAY_BARRIERS


def _release_delay_barrier(marker: str) -> bool:
    """Release one active marked model request."""
    with _DELAY_BARRIERS_LOCK:
        release = _DELAY_BARRIERS.pop(marker, None)
    if release is None:
        return False
    release.set()
    return True


def _start_stream(marker: str) -> None:
    """Register one stream with only its first delta released."""
    with _STREAM_STAGES_CONDITION:
        _STREAM_STAGES.pop(marker, None)
        _STREAM_STAGES[marker] = 1
        while len(_STREAM_STAGES) > _REQUEST_STARTS_LIMIT:
            _STREAM_STAGES.popitem(last=False)
        _STREAM_STAGES_CONDITION.notify_all()


def _advance_stream(marker: str, stage: int) -> bool:
    """Release one requested stream stage if the stream is active."""
    with _STREAM_STAGES_CONDITION:
        if marker not in _STREAM_STAGES:
            return False
        _STREAM_STAGES[marker] = max(_STREAM_STAGES[marker], stage)
        _STREAM_STAGES_CONDITION.notify_all()
        return True


def _wait_for_stream_stage(marker: str, stage: int) -> bool:
    """Wait until the live observer releases a specific stream stage."""
    with _STREAM_STAGES_CONDITION:
        return _STREAM_STAGES_CONDITION.wait_for(
            lambda: _STREAM_STAGES.get(marker, 0) >= stage,
            timeout=120,
        )


def _finish_stream(marker: str) -> None:
    """Discard one completed stream barrier."""
    with _STREAM_STAGES_CONDITION:
        _STREAM_STAGES.pop(marker, None)
        _STREAM_STAGES_CONDITION.notify_all()


def _message_text(message: Any) -> str:
    """Flatten one OpenAI chat message into text."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _message_image_bytes(message: Any) -> list[bytes]:
    """Decode native image data URLs from one OpenAI chat message."""
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        return []
    decoded: list[bytes] = []
    for item in message["content"]:
        if not isinstance(item, dict) or item.get("type") != "image_url":
            continue
        image_url = item.get("image_url")
        if isinstance(image_url, dict):
            image_url = image_url.get("url")
        if not isinstance(image_url, str) or ";base64," not in image_url:
            continue
        encoded = image_url.split(";base64,", 1)[1]
        try:
            decoded.append(base64.b64decode(encoded, validate=True))
        except (binascii.Error, ValueError):
            continue
    return decoded


def _response_text(payload: dict[str, Any]) -> str:
    """Build a deterministic reply from the current conversation."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        messages = []
    user_texts = [
        _message_text(message)
        for message in messages
        if isinstance(message, dict) and message.get("role") == "user"
    ]
    latest = user_texts[-1] if user_texts else ""
    latest_user = next(
        (
            message
            for message in reversed(messages)
            if isinstance(message, dict) and message.get("role") == "user"
        ),
        {},
    )

    expected = _EXPECT_RE.search(latest)
    parts: list[str] = []
    if expected is not None:
        parts.append(f"HERMES_E2E_EXPECT:{expected.group(1)}")

    if "HERMES_E2E_RECALL" in latest:
        remembered = None
        for prior in reversed(user_texts[:-1]):
            remembered = _REMEMBER_RE.search(prior)
            if remembered is not None:
                break
        parts.append(
            (
                f"HERMES_E2E_CONTEXT:{remembered.group(1)}"
                if remembered is not None
                else "HERMES_E2E_CONTEXT:MISSING"
            )
        )

    for image_bytes in _message_image_bytes(latest_user):
        parts.append(
            "HERMES_E2E_IMAGE_SHA256:"
            f"{hashlib.sha256(image_bytes).hexdigest()}"
        )

    media_return = _MEDIA_RETURN_RE.search(latest)
    if media_return is not None:
        marker = media_return.group(1)
        parts.extend(
            (
                f"HERMES_E2E_MEDIA_RETURNED:{marker}",
                f"MEDIA:/opt/data/e2e-outbound-{marker}.png",
                f"MEDIA:/opt/data/e2e-outbound-{marker}.txt",
            )
        )

    if "HERMES_E2E_EXISTING_CONTEXT_PROBE" in latest:
        visible_context = "\n".join(user_texts)
        root_present = "HERMES_E2E_EXISTING_ROOT:" in visible_context
        history_present = "HERMES_E2E_EXISTING_HISTORY:" in visible_context
        parts.append(
            "HERMES_E2E_EXISTING_CONTEXT:"
            f"ROOT={'YES' if root_present else 'NO'};"
            f"HISTORY={'YES' if history_present else 'NO'}"
        )

    if not parts:
        parts.append("HERMES_E2E_OK")
    return "\n".join(parts)


def _long_response_text(marker: str) -> str:
    """Build the exact sizeable Markdown fixture expected by live tests."""
    lines = [
        "## Long output verification",
        "",
        (
            "**Lossless ordered delivery** is verified by reconstructing every "
            "deterministic segment."
        ),
        "",
        f"HERMES_E2E_LONG_RESULT:{marker}",
    ]
    for index in range(1, 16):
        lines.extend(
            [
                "",
                (
                    f"Segment {index:02d}: Every character in this deterministic "
                    "payload must remain in order across streamed updates, including "
                    "punctuation, spaces, and Markdown boundaries."
                ),
            ]
        )
    return "\n".join(lines)


def _response_chunks(payload: dict[str, Any]) -> tuple[str, ...]:
    """Return deterministic deltas for normal, long, or rich-stream replies."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        messages = []
    user_texts = [
        _message_text(message)
        for message in messages
        if isinstance(message, dict) and message.get("role") == "user"
    ]
    latest = user_texts[-1] if user_texts else ""
    tool = _tool_fixture(payload)
    if tool is not None and tool[2]:
        kind, marker, _ = tool
        if kind == "approval":
            return (f"HERMES_E2E_APPROVAL_DENIED:{marker}",)
        return (
            (
                "## Tool execution verification\n\n"
                f"HERMES_E2E_TOOL_FINAL_STAGE_1:{marker}"
            ),
            f"\n\nHERMES_E2E_TOOL_RESULT:OBSERVED:{marker}",
            f"\n\nHERMES_E2E_TOOL_FINAL:{marker}",
        )
    delay_barrier = _DELAY_BARRIER_RE.search(latest)
    if delay_barrier is not None:
        _wait_for_delay_barrier(delay_barrier.group(1))
    long_output = _LONG_RE.search(latest)
    if long_output is not None:
        expected = _long_response_text(long_output.group(1))
        first_boundary = len(expected) // 3
        second_boundary = len(expected) * 2 // 3
        return (
            expected[:first_boundary],
            expected[first_boundary:second_boundary],
            expected[second_boundary:],
        )
    cardkit_image = _CARDKIT_IMAGE_RE.search(latest)
    if cardkit_image is not None:
        marker = cardkit_image.group(1)
        return (
            (
                f"HERMES_E2E_CARDKIT_IMAGE_STAGE_1:{marker}\n\n"
                f"![deterministic pixel]({CARDKIT_E2E_IMAGE_URL})"
            ),
            f"\n\nHERMES_E2E_CARDKIT_IMAGE_FINAL:{marker}",
        )
    stream = _STREAM_RE.search(latest)
    if stream is None:
        return (_response_text(payload),)

    marker = stream.group(1)
    return (
        (
            "## Streaming verification\n\n"
            f"HERMES_E2E_STREAM_STAGE_1:{marker}"
        ),
        (
            "\n\n- **Incremental edit:** "
            f"HERMES_E2E_STREAM_STAGE_2:{marker}"
        ),
        (
            "\n\n| State | Marker |\n"
            "| --- | --- |\n"
            f"| Final | HERMES_E2E_STREAM_FINAL:{marker} |"
        ),
    )


def _tool_fixture(payload: dict[str, Any]) -> tuple[str, str, bool] | None:
    """Return the terminal fixture kind, marker, and result state."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return None
    kind = ""
    marker = ""
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _message_text(message)
        for candidate_kind, pattern in (
            ("tool", _TOOL_RE),
            ("approval", _APPROVAL_RE),
        ):
            match = pattern.search(text)
            if match is not None:
                kind = candidate_kind
                marker = match.group(1)
    if not marker:
        return None
    result_marker = (
        f"HERMES_E2E_TOOL_EXECUTED:{marker}"
        if kind == "tool"
        else ""
    )
    observed = any(
        isinstance(message, dict)
        and message.get("role") == "tool"
        and (not result_marker or result_marker in _message_text(message))
        for message in messages
    )
    return kind, marker, observed


def _tool_call(kind: str, marker: str) -> dict[str, Any]:
    """Build one deterministic OpenAI terminal tool call."""
    command = (
        f"rm -rf /opt/data/hermes-lark-e2e-approval-{marker}"
        if kind == "approval"
        else f"printf 'HERMES_E2E_TOOL_EXECUTED:{marker}\\n'"
    )
    return {
        "index": 0,
        "id": f"call_{uuid.uuid5(uuid.NAMESPACE_URL, marker).hex}",
        "type": "function",
        "function": {
            "name": "terminal",
            "arguments": json.dumps(
                {"command": command},
                separators=(",", ":"),
            ),
        },
    }


class _Handler(BaseHTTPRequestHandler):
    """Serve the subset of the OpenAI API used by Hermes chat completions."""

    server_version = "hermes-lark-e2e"

    def do_GET(self) -> None:
        """Return model inventory and health responses."""
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.rstrip("/") == "/health":
            self._write_json(200, {"status": "ok"})
            return
        if parsed.path.rstrip("/") == "/v1/models":
            self._write_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "hermes-lark-e2e",
                            "object": "model",
                            "owned_by": "hermes-lark",
                        }
                    ],
                },
            )
            return
        if parsed.path.rstrip("/") == "/e2e/cardkit-image.png":
            self._write_bytes(
                200,
                CARDKIT_E2E_IMAGE_BYTES,
                content_type="image/png",
            )
            return
        if parsed.path.rstrip("/") == "/e2e/delay-barrier-active":
            marker = urllib.parse.parse_qs(parsed.query).get("marker", [""])[0]
            if not marker:
                self._write_json(400, {"error": {"message": "marker is required"}})
                return
            self._write_json(200, {"active": _delay_barrier_active(marker)})
            return
        self._write_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        """Return one deterministic chat completion."""
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.rstrip("/") == "/e2e/delay-barrier-release":
            marker = urllib.parse.parse_qs(parsed.query).get("marker", [""])[0]
            if not marker:
                self._write_json(400, {"error": {"message": "marker is required"}})
                return
            if not _release_delay_barrier(marker):
                self._write_json(
                    404,
                    {"error": {"message": "delay barrier not active"}},
                )
                return
            self._write_json(200, {"released": True})
            return
        if parsed.path.rstrip("/") == "/e2e/stream-advance":
            query = urllib.parse.parse_qs(parsed.query)
            marker = query.get("marker", [""])[0]
            try:
                stage = int(query.get("stage", [""])[0])
            except ValueError:
                stage = 0
            if not marker or stage not in {2, 3}:
                self._write_json(400, {"error": {"message": "invalid stream stage"}})
                return
            if not _advance_stream(marker, stage):
                self._write_json(404, {"error": {"message": "stream not active"}})
                return
            self._write_json(200, {"advanced": True, "stage": stage})
            return
        if parsed.path.rstrip("/") != "/v1/chat/completions":
            self._write_json(404, {"error": {"message": "not found"}})
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 4 * 1024 * 1024)
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("request body must be an object")
        except (ValueError, json.JSONDecodeError) as exc:
            self._write_json(400, {"error": {"message": str(exc)}})
            return

        tool_fixture = _tool_fixture(payload)
        pending_tool_kind = (
            tool_fixture[0]
            if tool_fixture is not None and not tool_fixture[2]
            else ""
        )
        pending_tool_marker = (
            tool_fixture[1] if pending_tool_kind else ""
        )
        content_chunks = (
            () if pending_tool_marker else _response_chunks(payload)
        )
        content = "".join(content_chunks)
        model = str(payload.get("model") or "hermes-lark-e2e")
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())
        if payload.get("stream"):
            latest = ""
            messages = payload.get("messages")
            if isinstance(messages, list):
                user_texts = [
                    _message_text(message)
                    for message in messages
                    if isinstance(message, dict)
                    and message.get("role") == "user"
                ]
                latest = user_texts[-1] if user_texts else ""
            stream_match = _STREAM_RE.search(latest) or _CARDKIT_IMAGE_RE.search(
                latest
            )
            stream_marker = stream_match.group(1) if stream_match else ""
            if stream_marker:
                _start_stream(stream_marker)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if pending_tool_marker:
                chunks = [
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "reasoning_content": (
                                        "HERMES_E2E_REASONING:"
                                        f"{pending_tool_marker}"
                                    ),
                                },
                                "finish_reason": None,
                            }
                        ],
                    },
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        _tool_call(
                                            pending_tool_kind,
                                            pending_tool_marker,
                                        )
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ],
                    },
                ]
            else:
                chunks = [
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    **(
                                        {"role": "assistant"}
                                        if index == 0
                                        else {}
                                    ),
                                    "content": chunk,
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                    for index, chunk in enumerate(content_chunks)
                ]
            chunks.append(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": (
                                "tool_calls" if pending_tool_marker else "stop"
                            ),
                        }
                    ],
                }
            )
            try:
                for index, chunk in enumerate(chunks):
                    self.wfile.write(
                        f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
                    )
                    self.wfile.flush()
                    if (
                        stream_marker
                        and index < len(content_chunks) - 1
                        and not _wait_for_stream_stage(stream_marker, index + 2)
                    ):
                        break
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            finally:
                if stream_marker:
                    _finish_stream(stream_marker)
            return

        if pending_tool_marker:
            self._write_json(
                200,
                {
                    "id": completion_id,
                    "object": "chat.completion",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "reasoning_content": (
                                    "HERMES_E2E_REASONING:"
                                    f"{pending_tool_marker}"
                                ),
                                "tool_calls": [
                                    _tool_call(
                                        pending_tool_kind,
                                        pending_tool_marker,
                                    )
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                },
            )
            return

        self._write_json(
            200,
            {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": content,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    def log_message(self, _format: str, *_args: Any) -> None:
        """Keep prompts and credentials out of container logs."""

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        """Write one bounded JSON response."""
        body = json.dumps(payload, ensure_ascii=False).encode()
        self._write_bytes(status, body, content_type="application/json")

    def _write_bytes(
        self,
        status: int,
        body: bytes,
        *,
        content_type: str,
    ) -> None:
        """Write one bounded binary response."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    """Run the deterministic model server."""
    port = int(os.environ.get("HERMES_E2E_STUB_PORT", "8000"))
    ThreadingHTTPServer(("0.0.0.0", port), _Handler).serve_forever()


if __name__ == "__main__":
    main()
