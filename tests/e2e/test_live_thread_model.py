"""Live Feishu/Lark acceptance tests for the fixed Slack-style thread model.

The suite is inert unless ``FEISHU_E2E=1`` is set. It observes Feishu OpenAPI
messages, the model-provider boundary, and Hermes' public session database API.
A user access token drives every scenario automatically.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import mimetypes
import os
import re
import secrets
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tests.e2e.openai_stub import CARDKIT_E2E_IMAGE_URL, _long_response_text


_LIVE_ENABLED = os.environ.get("FEISHU_E2E") == "1"
_TINY_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FeishuApiError(AssertionError):
    """Describe one sanitized Feishu OpenAPI failure."""


class FeishuOpenApi:
    """Call the small Feishu OpenAPI surface used by live acceptance tests."""

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        domain: str,
        user_access_token: str | None,
    ) -> None:
        normalized_domain = domain.strip().lower()
        if normalized_domain not in {"feishu", "lark"}:
            raise AssertionError("FEISHU_DOMAIN must be either feishu or lark")
        self.app_id = app_id
        self._app_secret = app_secret
        self._user_access_token = user_access_token
        self._tenant_access_token = ""
        self._base_url = (
            "https://open.larksuite.com"
            if normalized_domain == "lark"
            else "https://open.feishu.cn"
        )

    def authenticate(self) -> None:
        """Acquire an app-scoped tenant token without exposing credentials."""
        response = self._request(
            "POST",
            "/open-apis/auth/v3/tenant_access_token/internal",
            body={
                "app_id": self.app_id,
                "app_secret": self._app_secret,
            },
        )
        self._assert_success(response, "tenant token")
        token = str(response.get("tenant_access_token") or "")
        if not token:
            raise FeishuApiError("tenant token response omitted tenant_access_token")
        self._tenant_access_token = token

    def bot_info(self) -> dict[str, Any]:
        """Return the authenticated app bot's public identity."""
        response = self._request(
            "GET",
            "/open-apis/bot/v3/info",
            token=self._tenant_access_token,
        )
        self._assert_success(response, "bot info")
        bot = response.get("bot")
        if not isinstance(bot, dict) or not bot.get("open_id"):
            raise FeishuApiError("bot info response omitted bot.open_id")
        return bot

    def user_info(self) -> dict[str, Any]:
        """Return the public identity represented by the user token."""
        response = self._request(
            "GET",
            "/open-apis/authen/v1/user_info",
            token=self._require_user_token(),
        )
        self._assert_success(response, "user info")
        data = response.get("data")
        if not isinstance(data, dict) or not str(data.get("open_id") or "").startswith(
            "ou_"
        ):
            raise FeishuApiError("user info response omitted data.open_id")
        return data

    def resolve_dm_chat_id(self, user_open_id: str, run_id: str) -> str:
        """Resolve the canonical bot-user P2P chat with one setup message."""
        response = self._request(
            "POST",
            "/open-apis/im/v1/messages",
            token=self._tenant_access_token,
            query={"receive_id_type": "open_id"},
            body={
                "receive_id": user_open_id,
                "msg_type": "text",
                "content": json.dumps(
                    {"text": f"Hermes E2E {run_id}: initialize DM tests"},
                    ensure_ascii=False,
                ),
                "uuid": str(uuid.uuid4()),
            },
        )
        message = self._message_from_write(response, "resolve DM chat")
        chat_id = str(message.get("chat_id") or "")
        if not chat_id.startswith("oc_"):
            raise FeishuApiError("resolve DM chat response omitted data.chat_id")
        return chat_id

    def create_test_group(
        self,
        user_open_id: str,
        run_id: str,
        *,
        group_message_type: str = "chat",
    ) -> str:
        """Create one private app-owned group for the requested message mode."""
        if group_message_type not in {"chat", "thread"}:
            raise AssertionError("group_message_type must be chat or thread")
        name_suffix = " Thread" if group_message_type == "thread" else ""
        response = self._request(
            "POST",
            "/open-apis/im/v1/chats",
            token=self._tenant_access_token,
            query={
                "user_id_type": "open_id",
                "uuid": str(uuid.uuid4()),
            },
            body={
                "name": f"Hermes E2E{name_suffix} {run_id}"[:60],
                "description": "Temporary group for hermes-lark E2E",
                "user_id_list": [user_open_id],
                "group_message_type": group_message_type,
                "chat_mode": "group",
                "chat_type": "private",
                "join_message_visibility": "not_anyone",
                "leave_message_visibility": "not_anyone",
            },
        )
        self._assert_success(response, "create E2E group")
        data = response.get("data")
        chat_id = str(data.get("chat_id") or "") if isinstance(data, dict) else ""
        if not chat_id.startswith("oc_"):
            raise FeishuApiError("create E2E group response omitted data.chat_id")
        return chat_id

    def delete_chat(self, chat_id: str) -> None:
        """Dissolve one app-owned temporary group."""
        response = self._request(
            "DELETE",
            f"/open-apis/im/v1/chats/{urllib.parse.quote(chat_id, safe='')}",
            token=self._tenant_access_token,
        )
        self._assert_success(response, "delete E2E group")

    def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """Read back one temporary group's server-side message mode."""
        response = self._request(
            "GET",
            f"/open-apis/im/v1/chats/{urllib.parse.quote(chat_id, safe='')}",
            token=self._tenant_access_token,
            query={"user_id_type": "open_id"},
        )
        self._assert_success(response, "get E2E group")
        data = response.get("data")
        if not isinstance(data, dict):
            raise FeishuApiError("get E2E group response omitted data")
        return data

    def create_text_message(self, chat_id: str, text: str) -> dict[str, Any]:
        """Send one top-level human message with the configured user token."""
        response = self._request(
            "POST",
            "/open-apis/im/v1/messages",
            token=self._require_user_token(),
            query={"receive_id_type": "chat_id"},
            body={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "uuid": str(uuid.uuid4()),
            },
        )
        return self._message_from_write(response, "create message")

    def upload_image(self, content: bytes, file_name: str) -> str:
        """Upload one message image through the app-owned resource API."""
        response = self._request_multipart(
            "/open-apis/im/v1/images",
            token=self._tenant_access_token,
            fields={"image_type": "message"},
            file_field="image",
            file_name=file_name,
            content_type=mimetypes.guess_type(file_name)[0] or "image/png",
            content=content,
        )
        self._assert_success(response, "upload E2E image")
        data = response.get("data")
        image_key = str(data.get("image_key") or "") if isinstance(data, dict) else ""
        if not image_key.startswith("img_"):
            raise FeishuApiError("upload E2E image response omitted data.image_key")
        return image_key

    def upload_file(self, content: bytes, file_name: str) -> str:
        """Upload one generic message file through the app-owned resource API."""
        response = self._request_multipart(
            "/open-apis/im/v1/files",
            token=self._tenant_access_token,
            fields={"file_type": "stream", "file_name": file_name},
            file_field="file",
            file_name=file_name,
            content_type=mimetypes.guess_type(file_name)[0]
            or "application/octet-stream",
            content=content,
        )
        self._assert_success(response, "upload E2E file")
        data = response.get("data")
        file_key = str(data.get("file_key") or "") if isinstance(data, dict) else ""
        if not file_key:
            raise FeishuApiError("upload E2E file response omitted data.file_key")
        return file_key

    def create_user_media_message(
        self,
        chat_id: str,
        *,
        msg_type: str,
        resource_key: str,
    ) -> dict[str, Any]:
        """Send one top-level image or file message as the test user."""
        if msg_type == "image":
            content = {"image_key": resource_key}
        elif msg_type == "file":
            content = {"file_key": resource_key}
        else:
            raise AssertionError("msg_type must be image or file")
        response = self._request(
            "POST",
            "/open-apis/im/v1/messages",
            token=self._require_user_token(),
            query={"receive_id_type": "chat_id"},
            body={
                "receive_id": chat_id,
                "msg_type": msg_type,
                "content": json.dumps(content, ensure_ascii=False),
                "uuid": str(uuid.uuid4()),
            },
        )
        return self._message_from_write(response, f"create {msg_type} message")

    def reply_user_media_in_thread(
        self,
        root_message_id: str,
        *,
        msg_type: str,
        resource_key: str,
    ) -> dict[str, Any]:
        """Reply with one image or file as the test user in a native thread."""
        if msg_type == "image":
            content = {"image_key": resource_key}
        elif msg_type == "file":
            content = {"file_key": resource_key}
        else:
            raise AssertionError("msg_type must be image or file")
        response = self._request(
            "POST",
            (
                "/open-apis/im/v1/messages/"
                f"{urllib.parse.quote(root_message_id, safe='')}/reply"
            ),
            token=self._require_user_token(),
            body={
                "msg_type": msg_type,
                "content": json.dumps(content, ensure_ascii=False),
                "reply_in_thread": True,
                "uuid": str(uuid.uuid4()),
            },
        )
        return self._message_from_write(response, f"reply with {msg_type}")

    def create_bot_text_message(
        self,
        chat_id: str,
        text: str,
    ) -> dict[str, Any]:
        """Send one top-level app message as a neutral quote anchor."""
        response = self._request(
            "POST",
            "/open-apis/im/v1/messages",
            token=self._tenant_access_token,
            query={"receive_id_type": "chat_id"},
            body={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "uuid": str(uuid.uuid4()),
            },
        )
        return self._message_from_write(response, "create bot message")

    def reply_text_in_thread(
        self,
        root_message_id: str,
        text: str,
    ) -> dict[str, Any]:
        """Send one human reply into the native thread rooted at a message."""
        return self.reply_text_to_message(
            root_message_id,
            text,
            reply_in_thread=True,
        )

    def reply_text_to_message(
        self,
        message_id: str,
        text: str,
        *,
        reply_in_thread: bool,
    ) -> dict[str, Any]:
        """Send one human quote or native-thread reply to a message."""
        response = self._request(
            "POST",
            (
                "/open-apis/im/v1/messages/"
                f"{urllib.parse.quote(message_id, safe='')}/reply"
            ),
            token=self._require_user_token(),
            body={
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "reply_in_thread": reply_in_thread,
                "uuid": str(uuid.uuid4()),
            },
        )
        return self._message_from_write(response, "reply message")

    def recall_message(self, message_id: str) -> None:
        """Recall one message as the configured test user."""
        response = self._request(
            "DELETE",
            (
                "/open-apis/im/v1/messages/"
                f"{urllib.parse.quote(message_id, safe='')}"
            ),
            token=self._require_user_token(),
        )
        self._assert_success(response, "recall message")

    def get_message(self, message_id: str) -> dict[str, Any]:
        """Read one message by its ID with the app token."""
        response = self._request(
            "GET",
            (
                "/open-apis/im/v1/messages/"
                f"{urllib.parse.quote(message_id, safe='')}"
            ),
            token=self._tenant_access_token,
            query={"card_msg_content_type": "user_card_content"},
        )
        self._assert_success(response, "get message")
        data = response.get("data")
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise FeishuApiError("get message response omitted data.items[0]")
        return self._with_cardkit_trace(items[0])

    def list_message_reactions(self, message_id: str) -> list[dict[str, Any]]:
        """List the reactions currently visible on one message."""
        response = self._request(
            "GET",
            (
                "/open-apis/im/v1/messages/"
                f"{urllib.parse.quote(message_id, safe='')}/reactions"
            ),
            token=self._tenant_access_token,
        )
        self._assert_success(response, "list message reactions")
        data = response.get("data")
        items = data.get("items") if isinstance(data, dict) else None
        if items is None:
            return []
        if not isinstance(items, list) or not all(
            isinstance(item, dict) for item in items
        ):
            raise FeishuApiError(
                "list message reactions response has invalid data.items"
            )
        return items

    def create_message_reaction(
        self,
        message_id: str,
        emoji_type: str,
    ) -> dict[str, Any]:
        """Add one reaction as the test user and return its server record."""
        response = self._request(
            "POST",
            (
                "/open-apis/im/v1/messages/"
                f"{urllib.parse.quote(message_id, safe='')}/reactions"
            ),
            token=self._require_user_token(),
            body={"reaction_type": {"emoji_type": emoji_type}},
        )
        self._assert_success(response, "create message reaction")
        data = response.get("data")
        if not isinstance(data, dict) or not str(data.get("reaction_id") or ""):
            raise FeishuApiError(
                "create message reaction response omitted data.reaction_id"
            )
        return data

    def download_message_resource(
        self,
        message_id: str,
        resource_key: str,
        resource_type: str,
    ) -> tuple[bytes, str]:
        """Download one image or file attached to a visible IM message."""
        if resource_type not in {"image", "file"}:
            raise AssertionError("resource_type must be image or file")
        return self._request_binary(
            "GET",
            (
                "/open-apis/im/v1/messages/"
                f"{urllib.parse.quote(message_id, safe='')}/resources/"
                f"{urllib.parse.quote(resource_key, safe='')}"
            ),
            token=self._tenant_access_token,
            query={"type": resource_type},
        )

    def list_messages(
        self,
        *,
        container_type: str,
        container_id: str,
        start_time: int | None = None,
    ) -> list[dict[str, Any]]:
        """List a bounded set of messages from one chat or native thread."""
        items: list[dict[str, Any]] = []
        page_token = ""
        for _ in range(10):
            query: dict[str, str] = {
                "container_id_type": container_type,
                "container_id": container_id,
                "sort_type": "ByCreateTimeAsc",
                "page_size": "50",
                "card_msg_content_type": "user_card_content",
            }
            if start_time is not None and container_type == "chat":
                query["start_time"] = str(start_time)
            if page_token:
                query["page_token"] = page_token
            response = self._request(
                "GET",
                "/open-apis/im/v1/messages",
                token=self._tenant_access_token,
                query=query,
            )
            self._assert_success(response, "list messages")
            data = response.get("data")
            if not isinstance(data, dict):
                raise FeishuApiError("list messages response omitted data")
            page_items = data.get("items") or []
            if not isinstance(page_items, list):
                raise FeishuApiError("list messages response has invalid data.items")
            items.extend(
                self._with_cardkit_trace(item)
                for item in page_items
                if isinstance(item, dict)
            )
            if not data.get("has_more"):
                return items
            page_token = str(data.get("page_token") or "")
            if not page_token:
                raise FeishuApiError(
                    "list messages response set has_more without page_token"
                )
        raise FeishuApiError("list messages exceeded the 500-message safety bound")

    def _with_cardkit_trace(self, message: dict[str, Any]) -> dict[str, Any]:
        """Attach current CardKit text that Feishu's IM read API cannot return."""
        if message.get("msg_type") != "interactive":
            return message
        message_id = str(message.get("message_id") or "")
        if not message_id:
            return message
        trace_path = (
            Path(os.environ.get("HERMES_HOME", "/opt/data"))
            / "feishu_cardkit_e2e_trace.jsonl"
        )
        current = "\n".join(
            _cardkit_trace_text(entry)
            for entry in _read_cardkit_trace(trace_path)
            if str(entry.get("message_id") or "") == message_id
            and entry.get("ok") is True
        )
        if not current:
            return message
        hydrated = dict(message)
        hydrated["_cardkit_current_text"] = current
        return hydrated

    def _message_from_write(
        self,
        response: dict[str, Any],
        operation: str,
    ) -> dict[str, Any]:
        """Extract one message object from a successful write response."""
        self._assert_success(response, operation)
        data = response.get("data")
        if not isinstance(data, dict) or not data.get("message_id"):
            raise FeishuApiError(f"{operation} response omitted data.message_id")
        return data

    def _require_user_token(self) -> str:
        """Return the configured user token or fail without printing its value."""
        if not self._user_access_token:
            raise FeishuApiError("a live-E2E user access token is required")
        return self._user_access_token

    def _assert_success(
        self,
        response: dict[str, Any],
        operation: str,
    ) -> None:
        """Raise a sanitized error for a non-zero Feishu response code."""
        if response.get("code") in {None, 0}:
            return
        code = response.get("code", "unknown")
        message = self._sanitize(str(response.get("msg") or "unknown error"))
        details = self._sanitize(
            json.dumps(response, ensure_ascii=False, sort_keys=True)
        )
        raise FeishuApiError(
            f"{operation} failed: code={code}, msg={message}, response={details}"
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Perform one JSON request while keeping all credentials out of errors."""
        url = f"{self._base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = (
            json.dumps(body, ensure_ascii=False).encode("utf-8")
            if body is not None
            else None
        )
        request = urllib.request.Request(
            url,
            data=payload,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raw = error.read()
            parsed = self._decode_json(raw)
            code = parsed.get("code", "unknown")
            message = self._sanitize(str(parsed.get("msg") or "unknown error"))
            details = self._sanitize(
                json.dumps(parsed, ensure_ascii=False, sort_keys=True)
            )
            log_id = str(error.headers.get("X-Tt-Logid") or "")
            log_suffix = f", log_id={log_id}" if log_id else ""
            raise FeishuApiError(
                f"{method} {path} failed: HTTP {error.code}, "
                f"code={code}, msg={message}{log_suffix}, response={details}"
            ) from None
        except urllib.error.URLError as error:
            reason_name = type(error.reason).__name__
            raise FeishuApiError(
                f"{method} {path} failed before response ({reason_name})"
            ) from None
        return self._decode_json(raw)

    def _request_multipart(
        self,
        path: str,
        *,
        token: str,
        fields: dict[str, str],
        file_field: str,
        file_name: str,
        content_type: str,
        content: bytes,
    ) -> dict[str, Any]:
        """Perform one bounded multipart upload with credential-safe errors."""
        if not content:
            raise AssertionError("multipart test fixture must not be empty")
        for value in (*fields.keys(), *fields.values(), file_field, file_name):
            if any(character in value for character in '\r\n\0"'):
                raise AssertionError("multipart field contains an unsafe character")
        boundary = f"hermes-lark-e2e-{uuid.uuid4().hex}"
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.extend(
                (
                    f"--{boundary}\r\n".encode(),
                    (
                        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    ).encode(),
                    value.encode("utf-8"),
                    b"\r\n",
                )
            )
        chunks.extend(
            (
                f"--{boundary}\r\n".encode(),
                (
                    "Content-Disposition: form-data; "
                    f'name="{file_field}"; filename="{file_name}"\r\n'
                ).encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                content,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            )
        )
        raw, _ = self._request_raw(
            "POST",
            path,
            token=token,
            body=b"".join(chunks),
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        return self._decode_json(raw)

    def _request_binary(
        self,
        method: str,
        path: str,
        *,
        token: str,
        query: dict[str, str] | None = None,
    ) -> tuple[bytes, str]:
        """Perform one binary OpenAPI request and return bytes plus MIME type."""
        raw, headers = self._request_raw(
            method,
            path,
            token=token,
            query=query,
        )
        if not raw:
            raise FeishuApiError(f"{method} {path} returned an empty resource")
        return raw, str(headers.get("Content-Type") or "")

    def _request_raw(
        self,
        method: str,
        path: str,
        *,
        token: str,
        query: dict[str, str] | None = None,
        body: bytes | None = None,
        content_type: str = "application/octet-stream",
    ) -> tuple[bytes, Any]:
        """Perform one raw OpenAPI request while redacting known credentials."""
        url = f"{self._base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": content_type,
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read(), response.headers
        except urllib.error.HTTPError as error:
            raw = error.read()
            parsed = self._decode_json_or_empty(raw)
            code = parsed.get("code", "unknown")
            message = self._sanitize(str(parsed.get("msg") or "unknown error"))
            details = self._sanitize(
                json.dumps(parsed, ensure_ascii=False, sort_keys=True)
            )
            log_id = str(error.headers.get("X-Tt-Logid") or "")
            log_suffix = f", log_id={log_id}" if log_id else ""
            raise FeishuApiError(
                f"{method} {path} failed: HTTP {error.code}, "
                f"code={code}, msg={message}{log_suffix}, response={details}"
            ) from None
        except urllib.error.URLError as error:
            reason_name = type(error.reason).__name__
            raise FeishuApiError(
                f"{method} {path} failed before response ({reason_name})"
            ) from None

    @staticmethod
    def _decode_json_or_empty(raw: bytes) -> dict[str, Any]:
        """Decode an error object without exposing a binary response body."""
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _decode_json(self, raw: bytes) -> dict[str, Any]:
        """Decode an OpenAPI JSON object without including raw data in errors."""
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise FeishuApiError("Feishu OpenAPI returned invalid JSON") from None
        if not isinstance(parsed, dict):
            raise FeishuApiError("Feishu OpenAPI returned a non-object JSON value")
        return parsed

    def _sanitize(self, value: str) -> str:
        """Redact every credential known to this client from a diagnostic."""
        sanitized = value
        for sensitive in (
            self._app_secret,
            self._user_access_token,
            self._tenant_access_token,
        ):
            if sensitive:
                sanitized = sanitized.replace(sensitive, "<redacted>")
        return sanitized


def _wait_until(
    observe: Callable[[], Any | None],
    *,
    timeout_seconds: float,
    description: str,
    interval_seconds: float = 1.0,
) -> Any:
    """Poll an observable public state until it produces a value."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        value = observe()
        if value is not None:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"timed out after {timeout_seconds:g}s waiting for {description}"
            )
        time.sleep(interval_seconds)


def _message_text(message: dict[str, Any]) -> str:
    """Flatten the user-visible string values in a message body."""
    body = message.get("body")
    raw_content = body.get("content") if isinstance(body, dict) else ""
    try:
        content = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
    except json.JSONDecodeError:
        return str(raw_content or "")

    values: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)

    collect(content)
    collect(message.get("_cardkit_current_text"))
    return "\n".join(values)


def _message_body_content(message: dict[str, Any]) -> Any:
    """Decode one message body's raw JSON content."""
    body = message.get("body")
    raw_content = body.get("content") if isinstance(body, dict) else ""
    if not isinstance(raw_content, str):
        return raw_content
    try:
        return json.loads(raw_content)
    except json.JSONDecodeError:
        return raw_content


def _message_rendered_text(message: dict[str, Any]) -> str:
    """Return one text or post body's primary rendered text without mirrors."""
    body = message.get("body")
    raw_content = body.get("content") if isinstance(body, dict) else ""
    try:
        content = (
            json.loads(raw_content)
            if isinstance(raw_content, str)
            else raw_content
        )
    except json.JSONDecodeError:
        return str(raw_content or "")
    if not isinstance(content, dict):
        return str(content or "")
    if message.get("msg_type") == "text":
        return str(content.get("text") or "")

    preferred = content.get("content_v2") or content.get("content") or []
    values: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            text = value.get("text")
            if isinstance(text, str):
                values.append(text)
            else:
                collect(value.get("content"))

    collect(preferred)
    return "\n".join(values)


def _sender_type(message: dict[str, Any]) -> str:
    """Return the normalized sender type for one OpenAPI message."""
    sender = message.get("sender")
    return str(sender.get("sender_type") or "") if isinstance(sender, dict) else ""


def _sender_id(message: dict[str, Any]) -> str:
    """Return the sender ID for one OpenAPI message."""
    sender = message.get("sender")
    return str(sender.get("id") or "") if isinstance(sender, dict) else ""


def _message_time_ms(message: dict[str, Any]) -> int:
    """Return one message creation timestamp as milliseconds."""
    try:
        return int(str(message.get("create_time") or "0"))
    except ValueError:
        return 0


def _message_update_time_ms(message: dict[str, Any]) -> int:
    """Return one message update timestamp as milliseconds."""
    try:
        return int(str(message.get("update_time") or "0"))
    except ValueError:
        return 0


def _reaction_emoji(reaction: dict[str, Any]) -> str:
    """Return the normalized emoji type from one reaction object."""
    reaction_type = reaction.get("reaction_type")
    return (
        str(reaction_type.get("emoji_type") or "")
        if isinstance(reaction_type, dict)
        else ""
    )


def _contains_markdown_element(value: Any) -> bool:
    """Return whether one Feishu post body contains a markdown element."""
    if isinstance(value, list):
        return any(_contains_markdown_element(item) for item in value)
    if not isinstance(value, dict):
        return False
    if value.get("tag") == "md":
        return True
    return any(_contains_markdown_element(item) for item in value.values())


def _hermes_message_text(message: dict[str, Any]) -> str:
    """Flatten one persisted Hermes transcript message into text."""
    values: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)

    collect(message.get("content"))
    return "\n".join(values)


def _read_cardkit_trace(path: Path) -> list[dict[str, Any]]:
    """Read successful and failed CardKit API observations from JSONL."""
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise AssertionError(
            f"failed to read CardKit E2E trace ({type(error).__name__})"
        ) from None
    entries: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            raise AssertionError(
                f"CardKit E2E trace line {line_number} is invalid JSON"
            ) from None
        if not isinstance(entry, dict):
            raise AssertionError(
                f"CardKit E2E trace line {line_number} is not an object"
            )
        entries.append(entry)
    return entries


def _cardkit_trace_text(entry: dict[str, Any]) -> str:
    """Flatten the user-visible content captured for one CardKit write."""
    values: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)

    collect(entry.get("content"))
    collect(entry.get("card"))
    return "\n".join(values)


def _cardkit_trace_card(entry: dict[str, Any]) -> dict[str, Any]:
    """Decode the full card snapshot captured by one trace entry."""
    card = entry.get("card")
    if isinstance(card, str):
        try:
            card = json.loads(card)
        except json.JSONDecodeError:
            return {}
    return card if isinstance(card, dict) else {}


def _cardkit_trace_state(entry: dict[str, Any]) -> str:
    """Return the lifecycle state from either stable trace spelling."""
    return str(entry.get("state") or entry.get("status") or "")


def _positive_float_env(name: str, default: float) -> float:
    """Read one finite positive duration from the environment."""
    raw = os.environ.get(name)
    try:
        value = float(raw) if raw is not None else default
    except ValueError:
        raise AssertionError(f"{name} must be a positive number") from None
    if value <= 0 or not math.isfinite(value):
        raise AssertionError(f"{name} must be a positive finite number")
    return value


def _read_user_access_token() -> str:
    """Read one generated token without mixing it into static dotenv state."""
    configured_path = str(
        os.environ.get("FEISHU_E2E_USER_ACCESS_TOKEN_FILE") or ""
    ).strip()
    if not configured_path:
        return ""
    path = Path(configured_path)
    if not path.exists():
        return ""
    if path.is_symlink() or not path.is_file():
        raise AssertionError(
            "FEISHU_E2E_USER_ACCESS_TOKEN_FILE must be a regular file"
        )
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise AssertionError(
            "failed to read FEISHU_E2E_USER_ACCESS_TOKEN_FILE "
            f"({type(error).__name__})"
        ) from None
    lines = content.splitlines()
    if len(lines) != 1 or not lines[0] or lines[0] != lines[0].strip():
        raise AssertionError(
            "FEISHU_E2E_USER_ACCESS_TOKEN_FILE must contain exactly one token"
        )
    return lines[0]


@unittest.skipUnless(
    _LIVE_ENABLED,
    "live tenant test; set FEISHU_E2E=1 explicitly",
)
class LiveThreadModelTests(unittest.TestCase):
    """Verify thread and session behavior through a real Feishu tenant."""

    @classmethod
    def _cleanup_test_chat(cls, chat_id: str) -> None:
        """Delete an incomplete run's chat or retain a completed inspection run."""
        if cls.keep_chats and cls._suite_setup_complete:
            print(f"[E2E retained] chat_id={chat_id}", flush=True)
            return
        cls.api.delete_chat(chat_id)

    @classmethod
    def setUpClass(cls) -> None:
        cls.group_chat_id = ""
        cls.thread_group_chat_id = ""
        cls._suite_setup_complete = False
        cls.keep_chats = os.environ.get("FEISHU_E2E_KEEP_CHATS") == "1"
        required = ("FEISHU_APP_ID", "FEISHU_APP_SECRET")
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise AssertionError(
                "FEISHU_E2E=1 but required variables are missing: "
                + ", ".join(missing)
            )

        user_token = _read_user_access_token()
        if not user_token:
            raise AssertionError(
                "run acquire_user_access_token.py before live E2E"
            )

        cls.timeout_seconds = _positive_float_env(
            "FEISHU_E2E_TIMEOUT_SECONDS",
            120,
        )
        cls.quiet_seconds = _positive_float_env(
            "FEISHU_E2E_QUIET_SECONDS",
            15,
        )
        cls.model_stub_url = os.environ.get(
            "HERMES_E2E_STUB_URL",
            "http://model-stub:8000",
        ).rstrip("/")
        cls.session_db_path = (
            Path(os.environ.get("HERMES_HOME", "/opt/data")) / "state.db"
        )
        cls.cardkit_trace_path = (
            Path(os.environ.get("HERMES_HOME", "/opt/data"))
            / "feishu_cardkit_e2e_trace.jsonl"
        )
        cls.run_id = (
            os.environ.get("FEISHU_E2E_RUN_ID")
            or f"{time.strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"
        )
        cls.api = FeishuOpenApi(
            app_id=os.environ["FEISHU_APP_ID"],
            app_secret=os.environ["FEISHU_APP_SECRET"],
            domain=os.environ.get("FEISHU_DOMAIN", "feishu"),
            user_access_token=user_token,
        )
        cls.api.authenticate()
        bot = cls.api.bot_info()
        cls.bot_open_id = str(bot["open_id"])
        cls.bot_name = str(bot.get("app_name") or "Hermes")
        user = cls.api.user_info()
        user_open_id = str(user["open_id"])
        cls.dm_chat_id = cls.api.resolve_dm_chat_id(user_open_id, cls.run_id)
        cls.group_chat_id = cls.api.create_test_group(user_open_id, cls.run_id)
        cls.addClassCleanup(cls._cleanup_test_chat, cls.group_chat_id)
        cls.thread_group_chat_id = cls.api.create_test_group(
            user_open_id,
            cls.run_id,
            group_message_type="thread",
        )
        cls.addClassCleanup(
            cls._cleanup_test_chat,
            cls.thread_group_chat_id,
        )
        thread_group_info = cls.api.get_chat_info(cls.thread_group_chat_id)
        if (
            str(thread_group_info.get("chat_mode") or "").lower() != "group"
            or str(
                thread_group_info.get("group_message_type") or ""
            ).lower()
            != "thread"
        ):
            raise AssertionError(
                "Feishu did not create the E2E group in thread-message mode"
            )
        print(
            f"[E2E setup] DM chat_id={cls.dm_chat_id}; temporary group "
            f"chat_id={cls.group_chat_id}; thread-message group "
            f"chat_id={cls.thread_group_chat_id}",
            flush=True,
        )
        cls._suite_setup_complete = True

    def _wait_for_persisted_session(
        self,
        *,
        chat_id: str,
        chat_type: str,
        root_id: str,
        transcript_markers: tuple[str, ...],
    ) -> dict[str, Any]:
        """Read one committed session through Hermes' public state API."""
        if not self.session_db_path.is_file():
            raise AssertionError(
                f"Hermes session database is missing at {self.session_db_path}"
            )
        from gateway.config import Platform
        from gateway.session import SessionSource, build_session_key
        from hermes_state import SessionDB

        source = SessionSource(
            platform=Platform.FEISHU,
            chat_id=chat_id,
            chat_type=chat_type,
            thread_id=root_id,
        )
        expected_key = build_session_key(source)

        def observe() -> dict[str, Any] | None:
            database = SessionDB(db_path=self.session_db_path, read_only=True)
            try:
                session_id = database.find_session_by_origin(
                    platform="feishu",
                    chat_id=chat_id,
                    thread_id=root_id,
                )
                if not session_id:
                    return None
                row = database.get_session(session_id)
                if not isinstance(row, dict):
                    return None
                messages = database.get_messages(session_id)
            finally:
                database.close()

            transcript = "\n".join(
                _hermes_message_text(message)
                for message in messages
                if isinstance(message, dict)
            )
            if any(marker not in transcript for marker in transcript_markers):
                return None
            return {
                "id": session_id,
                "row": row,
                "messages": messages,
                "transcript": transcript,
            }

        session = _wait_until(
            observe,
            timeout_seconds=self.timeout_seconds,
            description=f"persisted Hermes session for root {root_id}",
            interval_seconds=0.25,
        )
        row = session["row"]
        self.assertEqual(row.get("source"), "feishu")
        self.assertEqual(row.get("chat_id"), chat_id)
        self.assertEqual(row.get("chat_type"), chat_type)
        self.assertEqual(row.get("thread_id"), root_id)
        self.assertEqual(row.get("session_key"), expected_key)
        self.assertTrue(str(session["id"]))
        return session

    def test_dm_top_level_starts_isolated_thread_with_context(self) -> None:
        """DM roots need no mention, retain thread context, and stay isolated."""
        chat_id = self.dm_chat_id
        secret = self._marker("DM-SECRET")
        root_ack = self._marker("DM-ROOT-ACK")
        root_marker = self._marker("DM-ROOT")
        root_text = "\n".join(
            (
                root_marker,
                f"HERMES_E2E_REMEMBER:{secret}",
                f"HERMES_E2E_EXPECT:{root_ack}",
                f"Remember {secret}. Reply exactly {root_ack}.",
            )
        )
        root = self.api.create_text_message(chat_id, root_text)
        root_id = str(root["message_id"])
        root_reply, thread_id = self._wait_for_bot_reply(
            root_id=root_id,
            expected_text=root_ack,
            after_ms=_message_time_ms(root),
        )
        self._assert_reply_is_in_root_thread(root_reply, root_id, thread_id)
        root_session = self._wait_for_persisted_session(
            chat_id=chat_id,
            chat_type="dm",
            root_id=root_id,
            transcript_markers=(root_marker, root_ack),
        )

        recall_marker = self._marker("DM-RECALL")
        recall_text = "\n".join(
            (
                recall_marker,
                "HERMES_E2E_RECALL",
                "Recall the HERMES_E2E_REMEMBER value from the root message. "
                "Reply exactly HERMES_E2E_CONTEXT:<that value>.",
            )
        )
        follow_up = self.api.reply_text_in_thread(root_id, recall_text)
        context_reply, observed_thread_id = self._wait_for_bot_reply(
            root_id=root_id,
            expected_text=f"HERMES_E2E_CONTEXT:{secret}",
            after_ms=_message_time_ms(follow_up),
        )
        self.assertEqual(observed_thread_id, thread_id)
        self._assert_reply_is_in_root_thread(context_reply, root_id, thread_id)
        active_session = self._wait_for_persisted_session(
            chat_id=chat_id,
            chat_type="dm",
            root_id=root_id,
            transcript_markers=(
                root_marker,
                recall_marker,
                f"HERMES_E2E_CONTEXT:{secret}",
            ),
        )
        self.assertEqual(active_session["id"], root_session["id"])

        fresh_marker = self._marker("DM-FRESH")
        fresh_text = "\n".join(
            (
                fresh_marker,
                f"HERMES_E2E_EXPECT:{fresh_marker}",
                "HERMES_E2E_RECALL",
                "This is a new top-level DM. Do not reuse another thread's "
                "memory. Reply with HERMES_E2E_CONTEXT:MISSING.",
            )
        )
        fresh_root = self.api.create_text_message(chat_id, fresh_text)
        fresh_root_id = str(fresh_root["message_id"])
        fresh_reply, fresh_thread_id = self._wait_for_bot_reply(
            root_id=fresh_root_id,
            expected_text="HERMES_E2E_CONTEXT:MISSING",
            after_ms=_message_time_ms(fresh_root),
        )
        self.assertNotEqual(fresh_root_id, root_id)
        self.assertNotEqual(fresh_thread_id, thread_id)
        self.assertIn(
            f"HERMES_E2E_EXPECT:{fresh_marker}",
            _message_text(fresh_reply),
        )
        self.assertNotIn(secret, _message_text(fresh_reply))
        self._assert_reply_is_in_root_thread(
            fresh_reply,
            fresh_root_id,
            fresh_thread_id,
        )
        fresh_session = self._wait_for_persisted_session(
            chat_id=chat_id,
            chat_type="dm",
            root_id=fresh_root_id,
            transcript_markers=(
                fresh_marker,
                "HERMES_E2E_CONTEXT:MISSING",
            ),
        )
        self.assertNotEqual(fresh_session["id"], root_session["id"])
        self.assertNotEqual(
            fresh_session["row"]["session_key"],
            root_session["row"]["session_key"],
        )
        self.assertNotIn(secret, fresh_session["transcript"])

    def test_dm_authorized_feishu_command_replies_in_its_thread(self) -> None:
        """An admitted caller reaches the plugin command behind Hermes auth."""
        root = self.api.create_text_message(self.dm_chat_id, "/feishu help")
        root_id = str(root["message_id"])
        reply, thread_id = self._wait_for_bot_reply(
            root_id=root_id,
            expected_text="Feishu Hermes Plugin v",
            after_ms=_message_time_ms(root),
        )

        self._assert_reply_is_in_root_thread(reply, root_id, thread_id)

    def test_dm_inbound_image_and_file_preserve_session_resources(self) -> None:
        """User media reaches one thread session without resource byte drift."""
        from gateway.platforms.base import (
            get_document_cache_dir,
            get_image_cache_dir,
        )

        image_digest = hashlib.sha256(_TINY_PNG_BYTES).hexdigest()
        image_cache_before = set(get_image_cache_dir().iterdir())
        image_key = self.api.upload_image(
            _TINY_PNG_BYTES,
            "hermes-lark-e2e-inbound.png",
        )
        root = self.api.create_user_media_message(
            self.dm_chat_id,
            msg_type="image",
            resource_key=image_key,
        )
        root_id = str(root["message_id"])
        uploaded_image, _ = self.api.download_message_resource(
            root_id,
            image_key,
            "image",
        )
        self.assertEqual(uploaded_image, _TINY_PNG_BYTES)

        image_reply, thread_id = self._wait_for_bot_reply(
            root_id=root_id,
            expected_text=f"HERMES_E2E_IMAGE_SHA256:{image_digest}",
            after_ms=_message_time_ms(root),
        )
        self._assert_reply_is_in_root_thread(image_reply, root_id, thread_id)
        image_session = self._wait_for_persisted_session(
            chat_id=self.dm_chat_id,
            chat_type="dm",
            root_id=root_id,
            transcript_markers=(f"HERMES_E2E_IMAGE_SHA256:{image_digest}",),
        )

        cached_images = set(get_image_cache_dir().iterdir()) - image_cache_before
        exact_cached_images = [
            path for path in cached_images if path.read_bytes() == _TINY_PNG_BYTES
        ]
        self.assertTrue(
            exact_cached_images,
            "the adapter did not retain the exact inbound image bytes",
        )
        self.assertTrue(
            any(str(path) in image_session["transcript"] for path in exact_cached_images),
            "the persisted user turn omitted its cached image reference",
        )
        self.assertIn("[screenshot]", image_session["transcript"])

        file_marker = self._marker("DM-INBOUND-FILE")
        file_name = f"e2e-inbound-{file_marker}.txt"
        file_bytes = (
            "Hermes Feishu inbound file integrity fixture\n"
            f"marker={file_marker}\n"
        ).encode()
        file_cache_before = set(get_document_cache_dir().iterdir())
        file_key = self.api.upload_file(file_bytes, file_name)
        file_message = self.api.reply_user_media_in_thread(
            root_id,
            msg_type="file",
            resource_key=file_key,
        )
        observed_file = self.api.get_message(str(file_message["message_id"]))
        observed_file_content = _message_body_content(observed_file)
        self.assertIsInstance(observed_file_content, dict)
        message_file_key = str(observed_file_content.get("file_key") or "")
        self.assertTrue(message_file_key)
        self.assertEqual(observed_file_content.get("file_name"), file_name)
        uploaded_file, _ = self.api.download_message_resource(
            str(file_message["message_id"]),
            message_file_key,
            "file",
        )
        self.assertEqual(uploaded_file, file_bytes)
        file_reply, observed_thread_id = self._wait_for_bot_reply(
            root_id=root_id,
            expected_text="HERMES_E2E_OK",
            after_ms=_message_time_ms(file_message),
        )
        self.assertEqual(observed_thread_id, thread_id)
        self._assert_reply_is_in_root_thread(file_reply, root_id, thread_id)
        file_session = self._wait_for_persisted_session(
            chat_id=self.dm_chat_id,
            chat_type="dm",
            root_id=root_id,
            transcript_markers=(file_name, "HERMES_E2E_OK"),
        )
        self.assertEqual(file_session["id"], image_session["id"])
        cached_files = set(get_document_cache_dir().iterdir()) - file_cache_before
        self.assertTrue(
            any(path.read_bytes() == file_bytes for path in cached_files),
            "the adapter did not retain the exact inbound file bytes",
        )

    def test_dm_outbound_image_and_file_preserve_thread_resources(self) -> None:
        """MEDIA directives deliver exact image and file bytes in the thread."""
        marker = self._marker("DM-OUTBOUND-MEDIA")
        image_path = Path(f"/opt/data/e2e-outbound-{marker}.png")
        file_path = Path(f"/opt/data/e2e-outbound-{marker}.txt")
        file_bytes = (
            "Hermes Feishu outbound file integrity fixture\n"
            f"marker={marker}\n"
        ).encode()
        image_path.write_bytes(_TINY_PNG_BYTES)
        file_path.write_bytes(file_bytes)
        try:
            root = self.api.create_text_message(
                self.dm_chat_id,
                "\n".join(
                    (
                        f"HERMES_E2E_MEDIA_RETURN:{marker}",
                        "Return both deterministic media fixtures.",
                    )
                ),
            )
            root_id = str(root["message_id"])
            after_ms = _message_time_ms(root)
            text_reply, thread_id = self._wait_for_bot_reply(
                root_id=root_id,
                expected_text=f"HERMES_E2E_MEDIA_RETURNED:{marker}",
                after_ms=after_ms,
            )
            self._assert_reply_is_in_root_thread(text_reply, root_id, thread_id)

            def observe_media() -> tuple[
                dict[str, Any],
                str,
                dict[str, Any],
                str,
            ] | None:
                messages = self.api.list_messages(
                    container_type="thread",
                    container_id=thread_id,
                )
                images: list[tuple[dict[str, Any], str]] = []
                files: list[tuple[dict[str, Any], str]] = []
                for message in messages:
                    if (
                        not self._is_bot_message(message)
                        or str(message.get("root_id") or "") != root_id
                        or _message_time_ms(message) < after_ms
                    ):
                        continue
                    content = _message_body_content(message)
                    if not isinstance(content, dict):
                        continue
                    if message.get("msg_type") == "image":
                        key = str(content.get("image_key") or "")
                        if key:
                            images.append((message, key))
                    elif (
                        message.get("msg_type") == "file"
                        and content.get("file_name") == file_path.name
                    ):
                        key = str(content.get("file_key") or "")
                        if key:
                            files.append((message, key))
                if len(images) > 1 or len(files) > 1:
                    raise AssertionError("duplicate outbound E2E media messages")
                if not images or not files:
                    return None
                return images[0][0], images[0][1], files[0][0], files[0][1]

            image_message, image_key, file_message, file_key = _wait_until(
                observe_media,
                timeout_seconds=self.timeout_seconds,
                description=f"outbound image and file under root {root_id}",
                interval_seconds=0.2,
            )
            self._assert_reply_is_in_root_thread(
                image_message,
                root_id,
                thread_id,
            )
            self._assert_reply_is_in_root_thread(
                file_message,
                root_id,
                thread_id,
            )
            downloaded_image, _ = self.api.download_message_resource(
                str(image_message["message_id"]),
                image_key,
                "image",
            )
            downloaded_file, _ = self.api.download_message_resource(
                str(file_message["message_id"]),
                file_key,
                "file",
            )
            self.assertEqual(downloaded_image, _TINY_PNG_BYTES)
            self.assertEqual(downloaded_file, file_bytes)
            self._wait_for_persisted_session(
                chat_id=self.dm_chat_id,
                chat_type="dm",
                root_id=root_id,
                transcript_markers=(f"HERMES_E2E_MEDIA_RETURNED:{marker}",),
            )
        finally:
            image_path.unlink(missing_ok=True)
            file_path.unlink(missing_ok=True)

    def test_dm_top_level_quote_starts_its_own_thread_session(self) -> None:
        """A non-thread quote becomes a new root instead of reusing its target."""
        anchor_marker = self._marker("DM-QUOTE-ANCHOR")
        anchor = self.api.create_bot_text_message(
            self.dm_chat_id,
            f"Hermes E2E neutral quote anchor: {anchor_marker}",
        )
        anchor_id = str(anchor["message_id"])
        observed_anchor = self.api.get_message(anchor_id)
        self.assertFalse(str(observed_anchor.get("thread_id") or ""))

        quote_marker = self._marker("DM-QUOTE-NEW-ROOT")
        quote = self.api.reply_text_to_message(
            anchor_id,
            "\n".join(
                (
                    f"HERMES_E2E_EXPECT:{quote_marker}",
                    f"Reply exactly {quote_marker}.",
                )
            ),
            reply_in_thread=False,
        )
        quote_id = str(quote["message_id"])
        observed_quote = self.api.get_message(quote_id)
        self.assertEqual(
            str(observed_quote.get("parent_id") or ""),
            anchor_id,
        )
        self.assertFalse(str(observed_quote.get("thread_id") or ""))

        quote_reply, quote_thread_id = self._wait_for_bot_reply(
            root_id=quote_id,
            expected_text=quote_marker,
            after_ms=_message_time_ms(quote),
        )
        self._assert_reply_is_in_root_thread(
            quote_reply,
            quote_id,
            quote_thread_id,
        )
        quote_session = self._wait_for_persisted_session(
            chat_id=self.dm_chat_id,
            chat_type="dm",
            root_id=quote_id,
            transcript_markers=(quote_marker,),
        )
        self.assertEqual(quote_session["row"]["thread_id"], quote_id)
        self.assertNotEqual(quote_id, anchor_id)

    def test_dm_complex_markdown_stream_updates_one_thread_message(self) -> None:
        """CardKit streams cumulative Markdown through its full lifecycle."""
        marker = self._marker("DM-RICH-STREAM")
        stage_1 = f"HERMES_E2E_STREAM_STAGE_1:{marker}"
        stage_2 = f"HERMES_E2E_STREAM_STAGE_2:{marker}"
        final_marker = f"HERMES_E2E_STREAM_FINAL:{marker}"
        root = self.api.create_text_message(
            self.dm_chat_id,
            "\n".join(
                (
                    marker,
                    f"HERMES_E2E_STREAM:{marker}",
                    "Return the deterministic rich streaming fixture.",
                )
            ),
        )
        root_id = str(root["message_id"])
        after_ms = _message_time_ms(root)

        try:
            card_message, thread_id = self._wait_for_cardkit_message(
                root_id=root_id,
                after_ms=after_ms,
            )
            message_id = str(card_message["message_id"])
            self._assert_reply_is_in_root_thread(
                card_message,
                root_id,
                thread_id,
            )

            def observe_first(
                entries: list[dict[str, Any]],
            ) -> tuple[dict[str, Any], dict[str, Any]] | None:
                failed = [entry for entry in entries if entry.get("ok") is False]
                if failed:
                    raise AssertionError("CardKit create or first content write failed")
                created = next(
                    (
                        entry
                        for entry in entries
                        if entry.get("operation") == "create"
                        and entry.get("ok") is True
                        and _cardkit_trace_state(entry) == "thinking"
                    ),
                    None,
                )
                first_content = next(
                    (
                        entry
                        for entry in entries
                        if entry.get("operation") == "content"
                        and entry.get("ok") is True
                        and _cardkit_trace_state(entry) == "generating"
                        and stage_1 in _cardkit_trace_text(entry)
                    ),
                    None,
                )
                if created is None or first_content is None:
                    return None
                return created, first_content

            created, first_content = self._wait_for_cardkit_trace(
                root_id=root_id,
                thread_id=thread_id,
                predicate=observe_first,
                description=f"Thinking and first CardKit content for {root_id}",
            )
            first_text = _cardkit_trace_text(first_content)
            self.assertNotIn(stage_2, first_text)
            self.assertNotIn(final_marker, first_text)

            self._advance_model_stream(marker, 2)

            def observe_second(
                entries: list[dict[str, Any]],
            ) -> dict[str, Any] | None:
                return next(
                    (
                        entry
                        for entry in entries
                        if entry.get("operation") == "content"
                        and entry.get("ok") is True
                        and stage_2 in _cardkit_trace_text(entry)
                    ),
                    None,
                )

            second_content = self._wait_for_cardkit_trace(
                root_id=root_id,
                thread_id=thread_id,
                predicate=observe_second,
                description=f"second cumulative CardKit content for {root_id}",
            )
            second_text = _cardkit_trace_text(second_content)
            self.assertIn(stage_1, second_text)
            self.assertNotIn(final_marker, second_text)

            self._advance_model_stream(marker, 3)

            def observe_complete(
                entries: list[dict[str, Any]],
            ) -> tuple[
                dict[str, Any],
                dict[str, Any],
                dict[str, Any],
                list[dict[str, Any]],
            ] | None:
                failed = [entry for entry in entries if entry.get("ok") is False]
                if failed:
                    raise AssertionError("CardKit finalization write failed")
                final_content = next(
                    (
                        entry
                        for entry in entries
                        if entry.get("operation") == "content"
                        and entry.get("ok") is True
                        and final_marker in _cardkit_trace_text(entry)
                    ),
                    None,
                )
                settings = next(
                    (
                        entry
                        for entry in entries
                        if entry.get("operation") == "settings"
                        and entry.get("ok") is True
                        and _cardkit_trace_state(entry) == "complete"
                    ),
                    None,
                )
                updated = next(
                    (
                        entry
                        for entry in entries
                        if entry.get("operation") == "update"
                        and entry.get("ok") is True
                        and _cardkit_trace_state(entry) == "complete"
                        and final_marker in _cardkit_trace_text(entry)
                    ),
                    None,
                )
                if final_content is None or settings is None or updated is None:
                    return None
                return final_content, settings, updated, entries

            final_content, settings, updated, entries = (
                self._wait_for_cardkit_trace(
                    root_id=root_id,
                    thread_id=thread_id,
                    predicate=observe_complete,
                    description=f"completed CardKit lifecycle for {root_id}",
                )
            )
        finally:
            self._advance_model_stream(marker, 2, required=False)
            self._advance_model_stream(marker, 3, required=False)

        final_content_text = _cardkit_trace_text(final_content)
        self.assertIn(stage_1, final_content_text)
        self.assertIn(stage_2, final_content_text)
        self.assertIn(final_marker, final_content_text)
        self.assertLess(entries.index(created), entries.index(first_content))
        self.assertLess(entries.index(first_content), entries.index(second_content))
        self.assertLess(entries.index(second_content), entries.index(final_content))
        self.assertLess(entries.index(final_content), entries.index(settings))
        self.assertLess(entries.index(settings), entries.index(updated))

        sequenced = [
            entry
            for entry in entries
            if entry.get("ok") is True
            and entry.get("operation") in {"content", "settings", "update"}
        ]
        sequences = [int(entry["sequence"]) for entry in sequenced]
        self.assertTrue(sequences)
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(len(sequences), len(set(sequences)))
        self.assertTrue(all(sequence > 0 for sequence in sequences))

        card_ids = {
            str(entry.get("card_id") or "")
            for entry in entries
            if entry.get("card_id")
        }
        message_ids = {
            str(entry.get("message_id") or "")
            for entry in entries
            if entry.get("message_id")
        }
        self.assertEqual(len(card_ids), 1)
        if message_ids:
            self.assertEqual(message_ids, {message_id})

        final_card = _cardkit_trace_card(updated)
        final_config = final_card.get("config")
        self.assertIsInstance(final_config, dict)
        self.assertIs(final_config.get("streaming_mode"), False)
        self.assertNotIn("loading_icon", _cardkit_trace_text(updated))
        self.assertEqual(final_config.get("summary"), {"content": ""})
        final_body = final_card.get("body")
        self.assertIsInstance(final_body, dict)
        self.assertFalse(
            any(
                element.get("element_id") == "lifecycle_status"
                for element in final_body.get("elements", [])
                if isinstance(element, dict)
            )
        )

        raw_card = _message_body_content(self.api.get_message(message_id))
        self.assertIsInstance(raw_card, dict)
        self.assertEqual(raw_card.get("schema"), "2.0")
        self.assertEqual(card_message.get("msg_type"), "interactive")
        self._assert_cardkit_message_remains_single(
            root_id=root_id,
            thread_id=thread_id,
            message_id=message_id,
            after_ms=after_ms,
        )
        self._wait_for_persisted_session(
            chat_id=self.dm_chat_id,
            chat_type="dm",
            root_id=root_id,
            transcript_markers=(marker, final_marker),
        )

    def test_dm_remote_markdown_image_reflushes_as_feishu_key(self) -> None:
        """A remote Markdown image appears only after its Feishu upload succeeds."""
        marker = self._marker("DM-CARDKIT-IMAGE")
        stage_1 = f"HERMES_E2E_CARDKIT_IMAGE_STAGE_1:{marker}"
        final_marker = f"HERMES_E2E_CARDKIT_IMAGE_FINAL:{marker}"
        root = self.api.create_text_message(
            self.dm_chat_id,
            "\n".join(
                (
                    marker,
                    f"HERMES_E2E_CARDKIT_IMAGE:{marker}",
                    "Return the deterministic remote-image fixture.",
                )
            ),
        )
        root_id = str(root["message_id"])
        after_ms = _message_time_ms(root)

        try:
            card_message, thread_id = self._wait_for_cardkit_message(
                root_id=root_id,
                after_ms=after_ms,
            )
            message_id = str(card_message["message_id"])
            self._assert_reply_is_in_root_thread(
                card_message,
                root_id,
                thread_id,
            )

            def observe_resolved(
                entries: list[dict[str, Any]],
            ) -> tuple[
                dict[str, Any],
                dict[str, Any],
                list[dict[str, Any]],
            ] | None:
                if any(entry.get("ok") is False for entry in entries):
                    raise AssertionError("CardKit remote-image write failed")
                serialized = json.dumps(entries, ensure_ascii=False)
                if CARDKIT_E2E_IMAGE_URL in serialized:
                    raise AssertionError("CardKit trace exposed the remote image URL")
                initial = next(
                    (
                        entry
                        for entry in entries
                        if entry.get("operation") == "content"
                        and entry.get("ok") is True
                        and stage_1 in _cardkit_trace_text(entry)
                        and "(img_" not in _cardkit_trace_text(entry)
                    ),
                    None,
                )
                resolved = next(
                    (
                        entry
                        for entry in entries
                        if entry.get("operation") == "content"
                        and entry.get("ok") is True
                        and stage_1 in _cardkit_trace_text(entry)
                        and "(img_" in _cardkit_trace_text(entry)
                    ),
                    None,
                )
                if initial is None or resolved is None:
                    return None
                return initial, resolved, entries

            initial, resolved, first_entries = self._wait_for_cardkit_trace(
                root_id=root_id,
                thread_id=thread_id,
                predicate=observe_resolved,
                description=f"resolved CardKit remote image for {root_id}",
            )
            self.assertLess(first_entries.index(initial), first_entries.index(resolved))
            resolved_text = _cardkit_trace_text(resolved)
            image_match = re.search(r"\((img_[^)\s]+)\)", resolved_text)
            self.assertIsNotNone(image_match)
            image_key = image_match.group(1)

            self._advance_model_stream(marker, 2)

            def observe_complete(
                entries: list[dict[str, Any]],
            ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
                if any(entry.get("ok") is False for entry in entries):
                    raise AssertionError("CardKit remote-image finalization failed")
                serialized = json.dumps(entries, ensure_ascii=False)
                if CARDKIT_E2E_IMAGE_URL in serialized:
                    raise AssertionError("CardKit trace exposed the remote image URL")
                completed = next(
                    (
                        entry
                        for entry in entries
                        if entry.get("operation") == "update"
                        and entry.get("ok") is True
                        and _cardkit_trace_state(entry) == "complete"
                        and final_marker in _cardkit_trace_text(entry)
                        and f"({image_key})" in _cardkit_trace_text(entry)
                    ),
                    None,
                )
                return (completed, entries) if completed is not None else None

            completed, final_entries = self._wait_for_cardkit_trace(
                root_id=root_id,
                thread_id=thread_id,
                predicate=observe_complete,
                description=f"completed CardKit remote image for {root_id}",
            )
        finally:
            self._advance_model_stream(marker, 2, required=False)

        completed_text = _cardkit_trace_text(completed)
        self.assertIn(stage_1, completed_text)
        self.assertIn(final_marker, completed_text)
        self.assertIn(f"({image_key})", completed_text)
        self.assertNotIn(
            CARDKIT_E2E_IMAGE_URL,
            json.dumps(final_entries, ensure_ascii=False),
        )
        self._assert_cardkit_message_remains_single(
            root_id=root_id,
            thread_id=thread_id,
            message_id=message_id,
            after_ms=after_ms,
        )

    def test_dm_reasoning_terminal_tool_updates_card_and_session(self) -> None:
        """A real tool turn exposes status and persists both structured rows."""
        marker = self._marker("DM-TOOL")
        final_marker = f"HERMES_E2E_TOOL_FINAL:{marker}"
        result_marker = f"HERMES_E2E_TOOL_EXECUTED:{marker}"
        reasoning_marker = f"HERMES_E2E_REASONING:{marker}"
        root = self.api.create_text_message(
            self.dm_chat_id,
            "\n".join(
                (
                    f"HERMES_E2E_TOOL:{marker}",
                    "Execute the deterministic terminal fixture, then report it.",
                )
            ),
        )
        root_id = str(root["message_id"])
        after_ms = _message_time_ms(root)
        card_message, thread_id = self._wait_for_cardkit_message(
            root_id=root_id,
            after_ms=after_ms,
        )

        def observe_complete(
            entries: list[dict[str, Any]],
        ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
            failed = [entry for entry in entries if entry.get("ok") is False]
            if failed:
                raise AssertionError("CardKit tool lifecycle write failed")
            completed = next(
                (
                    entry
                    for entry in entries
                    if entry.get("operation") == "update"
                    and entry.get("ok") is True
                    and _cardkit_trace_state(entry) == "complete"
                    and final_marker in _cardkit_trace_text(entry)
                ),
                None,
            )
            if completed is None:
                return None
            states = {_cardkit_trace_state(entry) for entry in entries}
            if not {"thinking", "tool_running", "tool_complete", "complete"}.issubset(
                states
            ):
                return None
            return completed, entries

        completed, trace_entries = self._wait_for_cardkit_trace(
            root_id=root_id,
            thread_id=thread_id,
            predicate=observe_complete,
            description=f"terminal tool CardKit lifecycle for {root_id}",
        )
        tool_trace = "\n".join(
            _cardkit_trace_text(entry)
            for entry in trace_entries
            if _cardkit_trace_state(entry) in {"tool_running", "tool_complete"}
        )
        self.assertIn("terminal", tool_trace.lower())
        self.assertIn(final_marker, _cardkit_trace_text(completed))

        session = self._wait_for_persisted_session(
            chat_id=self.dm_chat_id,
            chat_type="dm",
            root_id=root_id,
            transcript_markers=(marker, result_marker, final_marker),
        )
        messages = session["messages"]
        reasoning = "\n".join(
            str(message.get("reasoning_content") or message.get("reasoning") or "")
            for message in messages
            if isinstance(message, dict) and message.get("role") == "assistant"
        )
        self.assertIn(reasoning_marker, reasoning)

        tool_call_rows = [
            message
            for message in messages
            if isinstance(message, dict)
            and message.get("role") == "assistant"
            and message.get("tool_calls")
        ]
        self.assertEqual(len(tool_call_rows), 1)
        tool_calls = tool_call_rows[0]["tool_calls"]
        self.assertIsInstance(tool_calls, list)
        terminal_calls = [
            call
            for call in tool_calls
            if isinstance(call, dict)
            and isinstance(call.get("function"), dict)
            and call["function"].get("name") == "terminal"
        ]
        self.assertEqual(len(terminal_calls), 1)
        tool_call_id = str(terminal_calls[0].get("id") or "")
        self.assertTrue(tool_call_id)
        arguments = json.loads(terminal_calls[0]["function"]["arguments"])
        self.assertEqual(
            arguments,
            {"command": f"printf '{result_marker}\\n'"},
        )

        tool_rows = [
            message
            for message in messages
            if isinstance(message, dict)
            and message.get("role") == "tool"
            and message.get("tool_name") == "terminal"
            and message.get("tool_call_id") == tool_call_id
        ]
        self.assertEqual(len(tool_rows), 1)
        self.assertIn(result_marker, _hermes_message_text(tool_rows[0]))
        self._assert_reply_is_in_root_thread(
            card_message,
            root_id,
            thread_id,
        )
        self._assert_cardkit_message_remains_single(
            root_id=root_id,
            thread_id=thread_id,
            message_id=str(card_message["message_id"]),
            after_ms=after_ms,
        )

    def test_dm_sensitive_tool_approval_card_is_denied_in_thread(self) -> None:
        """A real approval card blocks execution until same-thread /deny."""
        marker = self._marker("DM-APPROVAL")
        target = Path(f"/opt/data/hermes-lark-e2e-approval-{marker}")
        final_marker = f"HERMES_E2E_APPROVAL_DENIED:{marker}"
        target.write_text("must survive denial\n", encoding="utf-8")
        try:
            root = self.api.create_text_message(
                self.dm_chat_id,
                "\n".join(
                    (
                        f"HERMES_E2E_APPROVAL:{marker}",
                        "Request the deterministic sensitive terminal operation.",
                    )
                ),
            )
            root_id = str(root["message_id"])
            after_ms = _message_time_ms(root)
            _, thread_id = self._wait_for_cardkit_message(
                root_id=root_id,
                after_ms=after_ms,
            )

            def observe_approval() -> dict[str, Any] | None:
                messages = self.api.list_messages(
                    container_type="thread",
                    container_id=thread_id,
                )
                matching = [
                    message
                    for message in messages
                    if self._is_bot_message(message)
                    and str(message.get("root_id") or "") == root_id
                    and _message_time_ms(message) >= after_ms
                    and message.get("msg_type") == "interactive"
                    and "Command Approval Required" in _message_text(message)
                    and str(target) in _message_text(message)
                ]
                if len(matching) > 1:
                    raise AssertionError("tool turn emitted duplicate approval cards")
                return matching[0] if matching else None

            approval = _wait_until(
                observe_approval,
                timeout_seconds=self.timeout_seconds,
                description=f"sensitive-command approval card for {root_id}",
                interval_seconds=0.2,
            )
            self._assert_reply_is_in_root_thread(approval, root_id, thread_id)
            approval_card = _message_body_content(approval)
            self.assertIsInstance(approval_card, dict)
            action_rows = [
                element
                for element in approval_card.get("elements", [])
                if element.get("tag") == "action"
            ]
            self.assertEqual(len(action_rows), 1)
            buttons = action_rows[0].get("actions", [])
            self.assertEqual(
                [button.get("text", {}).get("content") for button in buttons],
                ["✅ Allow Once", "✅ Session", "✅ Always", "❌ Deny"],
            )
            self.assertTrue(
                all(
                    button.get("custom_action_id")
                    and {behavior.get("type") for behavior in button.get("behaviors", [])}
                    == {"callback"}
                    for button in buttons
                )
            )
            self.assertTrue(target.is_file())
            self.assertFalse(
                any(
                    final_marker in _cardkit_trace_text(entry)
                    for entry in self._cardkit_entries(
                        root_id=root_id,
                        thread_id=thread_id,
                    )
                ),
                "agent completed before the user resolved its approval",
            )

            denial = self.api.reply_text_in_thread(root_id, "/deny")

            def observe_denied(
                entries: list[dict[str, Any]],
            ) -> dict[str, Any] | None:
                return next(
                    (
                        entry
                        for entry in entries
                        if entry.get("operation") == "update"
                        and entry.get("ok") is True
                        and _cardkit_trace_state(entry) == "complete"
                        and final_marker in _cardkit_trace_text(entry)
                    ),
                    None,
                )

            completed = self._wait_for_cardkit_trace(
                root_id=root_id,
                thread_id=thread_id,
                predicate=observe_denied,
                description=f"denied tool completion for {root_id}",
            )
            self.assertGreaterEqual(_message_time_ms(denial), after_ms)
            self.assertIn(final_marker, _cardkit_trace_text(completed))
            self.assertTrue(
                target.is_file(),
                "denied terminal command unexpectedly removed its sentinel",
            )

            session = self._wait_for_persisted_session(
                chat_id=self.dm_chat_id,
                chat_type="dm",
                root_id=root_id,
                transcript_markers=(marker, final_marker),
            )
            tool_call_rows = [
                message
                for message in session["messages"]
                if isinstance(message, dict)
                and message.get("role") == "assistant"
                and message.get("tool_calls")
            ]
            self.assertEqual(len(tool_call_rows), 1)
            tool_call_id = str(
                tool_call_rows[0]["tool_calls"][0].get("id") or ""
            )
            self.assertTrue(tool_call_id)
            tool_rows = [
                message
                for message in session["messages"]
                if isinstance(message, dict)
                and message.get("role") == "tool"
                and message.get("tool_name") == "terminal"
                and message.get("tool_call_id") == tool_call_id
            ]
            self.assertEqual(len(tool_rows), 1)
            self.assertIn("denied", _hermes_message_text(tool_rows[0]).lower())
        finally:
            target.unlink(missing_ok=True)

    def test_dm_processing_reaction_spans_the_model_request(self) -> None:
        """Typing exists while the provider is blocked and clears on success."""
        marker = self._marker("DM-REACTION")
        root = self.api.create_text_message(
            self.dm_chat_id,
            "\n".join(
                (
                    f"HERMES_E2E_DELAY_BARRIER:{marker}",
                    f"HERMES_E2E_EXPECT:{marker}",
                    f"After release, reply exactly {marker}.",
                )
            ),
        )
        root_id = str(root["message_id"])
        after_ms = _message_time_ms(root)

        try:
            self._wait_for_model_delay_barrier(marker)

            def observe_typing() -> dict[str, Any] | None:
                for reaction in self.api.list_message_reactions(root_id):
                    if _reaction_emoji(reaction) == "Typing":
                        return reaction
                return None

            typing = _wait_until(
                observe_typing,
                timeout_seconds=self.timeout_seconds,
                description=f"Typing reaction on user message {root_id}",
                interval_seconds=0.2,
            )
            self.assertTrue(str(typing.get("reaction_id") or ""))
            self._release_model_delay_barrier(marker)
            reply, thread_id = self._wait_for_bot_reply(
                root_id=root_id,
                expected_text=marker,
                after_ms=after_ms,
            )
        finally:
            self._release_model_delay_barrier(marker, required=False)

        self._assert_reply_is_in_root_thread(reply, root_id, thread_id)

        def observe_cleared() -> bool | None:
            reactions = self.api.list_message_reactions(root_id)
            return (
                True
                if all(_reaction_emoji(item) != "Typing" for item in reactions)
                else None
            )

        _wait_until(
            observe_cleared,
            timeout_seconds=self.timeout_seconds,
            description=f"Typing reaction removal from user message {root_id}",
            interval_seconds=0.2,
        )
        self._assert_stream_remains_single(
            chat_id=self.dm_chat_id,
            root_id=root_id,
            thread_id=thread_id,
            message_id=str(reply["message_id"]),
            marker=marker,
            start_time=int(after_ms / 1000) - 2,
        )
        self._wait_for_persisted_session(
            chat_id=self.dm_chat_id,
            chat_type="dm",
            root_id=root_id,
            transcript_markers=(marker,),
        )

    def test_dm_user_reaction_creates_a_turn_in_the_same_session(self) -> None:
        """A human reaction to the bot becomes one same-thread synthetic turn."""
        marker = self._marker("DM-USER-REACTION")
        expected = f"HERMES_E2E_EXPECT:{marker}"
        root = self.api.create_text_message(
            self.dm_chat_id,
            f"{expected}\nReply exactly {expected}.",
        )
        root_id = str(root["message_id"])
        first_reply, thread_id = self._wait_for_bot_reply(
            root_id=root_id,
            expected_text=expected,
            after_ms=_message_time_ms(root),
        )
        self._assert_reply_is_in_root_thread(first_reply, root_id, thread_id)

        reaction = self.api.create_message_reaction(
            str(first_reply["message_id"]),
            "THUMBSUP",
        )
        reaction_time_ms = int(str(reaction.get("action_time") or "0"))
        self.assertGreater(reaction_time_ms, 0)
        reaction_reply, observed_thread_id = self._wait_for_bot_reply(
            root_id=root_id,
            expected_text="HERMES_E2E_OK",
            after_ms=reaction_time_ms,
        )

        self.assertEqual(observed_thread_id, thread_id)
        self._assert_reply_is_in_root_thread(
            reaction_reply,
            root_id,
            thread_id,
        )
        session = self._wait_for_persisted_session(
            chat_id=self.dm_chat_id,
            chat_type="dm",
            root_id=root_id,
            transcript_markers=(
                expected,
                "reacted with THUMBSUP",
                "HERMES_E2E_OK",
            ),
        )
        self.assertIn("reacted with THUMBSUP", session["transcript"])

    def test_dm_stop_bypasses_the_active_thread_turn(self) -> None:
        """A thread /stop reaches Hermes before the blocked provider returns."""
        marker = self._marker("DM-STOP")
        root = self.api.create_text_message(
            self.dm_chat_id,
            "\n".join(
                (
                    f"HERMES_E2E_DELAY_BARRIER:{marker}",
                    f"HERMES_E2E_EXPECT:{marker}",
                    "Remain blocked until the test releases the provider.",
                )
            ),
        )
        root_id = str(root["message_id"])

        try:
            self._wait_for_model_delay_barrier(marker)
            stop_message = self.api.reply_text_in_thread(root_id, "/stop")
            stop_after_ms = _message_time_ms(stop_message)
            reply, thread_id = self._wait_for_bot_reply(
                root_id=root_id,
                expected_text="Stopped",
                after_ms=stop_after_ms,
            )
            self._assert_reply_is_in_root_thread(reply, root_id, thread_id)
        finally:
            self._release_model_delay_barrier(marker, required=False)

    def test_group_requires_mention_then_keeps_active_thread(self) -> None:
        """A group root requires mention but its active thread does not."""
        self._exercise_group(chat_id=self.group_chat_id, label="GROUP")

    def test_existing_human_thread_first_mention_creates_session(self) -> None:
        """The first bot mention activates an existing human-only thread."""
        root_marker = self._marker("EXISTING-THREAD-ROOT")
        root = self.api.create_text_message(
            self.group_chat_id,
            f"HERMES_E2E_EXISTING_ROOT:{root_marker}",
        )
        root_id = str(root["message_id"])

        history_marker = self._marker("EXISTING-THREAD-HISTORY")
        human_reply = self.api.reply_text_in_thread(
            root_id,
            f"HERMES_E2E_EXISTING_HISTORY:{history_marker}",
        )
        thread_id = str(human_reply.get("thread_id") or "")
        self.assertTrue(thread_id.startswith("omt_"))
        self._assert_no_bot_activity(
            chat_id=self.group_chat_id,
            root_message=root,
            marker=history_marker,
        )

        from hermes_state import SessionDB

        database = SessionDB(db_path=self.session_db_path, read_only=True)
        try:
            session_before_mention = database.find_session_by_origin(
                platform="feishu",
                chat_id=self.group_chat_id,
                thread_id=root_id,
            )
        finally:
            database.close()
        self.assertIsNone(session_before_mention)

        activation_marker = self._marker("EXISTING-THREAD-ACTIVATE")
        activation = self.api.reply_text_in_thread(
            root_id,
            (
                f'<at user_id="{self.bot_open_id}">{self.bot_name}</at> '
                f"{activation_marker}\n"
                "HERMES_E2E_EXISTING_CONTEXT_PROBE"
            ),
        )
        reply, observed_thread_id = self._wait_for_bot_reply(
            root_id=root_id,
            expected_text=(
                "HERMES_E2E_EXISTING_CONTEXT:"
                "ROOT=YES;HISTORY=YES"
            ),
            after_ms=_message_time_ms(activation),
        )
        self.assertEqual(observed_thread_id, thread_id)
        self._assert_reply_is_in_root_thread(reply, root_id, thread_id)
        self._wait_for_persisted_session(
            chat_id=self.group_chat_id,
            chat_type="group",
            root_id=root_id,
            transcript_markers=(
                activation_marker,
                "HERMES_E2E_EXISTING_CONTEXT:ROOT=YES;HISTORY=YES",
            ),
        )

    def test_group_long_markdown_is_losslessly_chunked_in_one_thread(self) -> None:
        """A sizeable rich answer is split without loss or top-level escape."""
        marker = self._marker("GROUP-LONG")
        expected = _long_response_text(marker)
        prompt = "\n".join(
            (
                f"HERMES_E2E_LONG:{marker}",
                "Return the deterministic long-output fixture.",
            )
        )
        root = self.api.create_text_message(
            self.group_chat_id,
            f'<at user_id="{self.bot_open_id}">{self.bot_name}</at> {prompt}',
        )
        root_id = str(root["message_id"])
        after_ms = _message_time_ms(root)
        _, thread_id = self._wait_for_bot_reply(
            root_id=root_id,
            expected_text="Segment 15:",
            after_ms=after_ms,
        )

        def observe_chunks() -> list[dict[str, Any]] | None:
            messages = self.api.list_messages(
                container_type="thread",
                container_id=thread_id,
            )
            chunks = [
                item
                for item in messages
                if self._is_bot_message(item)
                and str(item.get("root_id") or "") == root_id
                and _message_time_ms(item) >= after_ms
                and (
                    marker in _message_rendered_text(item)
                    or "Segment " in _message_rendered_text(item)
                )
            ]
            if len(chunks) < 3:
                return None
            visible_chunks = [
                re.sub(
                    r"\s*\(\d+/\d+\)\s*$",
                    "",
                    _message_rendered_text(item),
                )
                for item in chunks
            ]
            reconstructed = "\n\n".join(visible_chunks)
            if reconstructed != expected or any(
                "▉" in _message_rendered_text(item) for item in chunks
            ):
                return None
            return chunks

        chunks = _wait_until(
            observe_chunks,
            timeout_seconds=self.timeout_seconds,
            description=f"lossless long response under root {root_id}",
            interval_seconds=0.2,
        )
        self.assertGreaterEqual(len(chunks), 3)
        self.assertEqual(_message_rendered_text(chunks[0]).count(marker), 1)
        self.assertEqual(
            sum(_message_rendered_text(item).count(marker) for item in chunks),
            1,
        )
        self.assertEqual(chunks[0].get("msg_type"), "post")
        for chunk in chunks:
            self._assert_reply_is_in_root_thread(chunk, root_id, thread_id)
            self.assertIn(chunk.get("msg_type"), {"text", "post"})
            self.assertLessEqual(len(_message_rendered_text(chunk)), 1000)

        self._wait_for_persisted_session(
            chat_id=self.group_chat_id,
            chat_type="group",
            root_id=root_id,
            transcript_markers=(marker, "Segment 15:"),
        )

    def test_thread_message_group_uses_message_root_for_the_session(self) -> None:
        """The fresh topic-style group follows the same fixed thread model."""
        self._exercise_group(
            chat_id=self.thread_group_chat_id,
            label="THREAD-GROUP",
        )

    def test_recalled_root_never_falls_back_to_top_level(self) -> None:
        """A failed canonical-root reply must not create a top-level message."""
        chat_id = self.dm_chat_id
        root_ack = self._marker("FAIL-CLOSED-ROOT-ACK")
        root_marker = self._marker("FAIL-CLOSED-ROOT")
        root_text = "\n".join(
            (
                root_marker,
                f"HERMES_E2E_EXPECT:{root_ack}",
                f"Reply exactly {root_ack}.",
            )
        )
        root = self.api.create_text_message(chat_id, root_text)
        root_id = str(root["message_id"])
        _, thread_id = self._wait_for_bot_reply(
            root_id=root_id,
            expected_text=root_ack,
            after_ms=_message_time_ms(root),
        )

        fallback_marker = self._marker("MUST-NOT-FALLBACK")
        delayed_text = "\n".join(
            (
                self._marker("FAIL-CLOSED-TURN"),
                f"HERMES_E2E_DELAY_BARRIER:{fallback_marker}",
                f"HERMES_E2E_EXPECT:{fallback_marker}",
                f"Wait for the test barrier, then reply exactly {fallback_marker}.",
            )
        )
        follow_up = self.api.reply_text_in_thread(root_id, delayed_text)
        try:
            self._wait_for_model_delay_barrier(fallback_marker)
            early_messages = self.api.list_messages(
                container_type="thread",
                container_id=thread_id,
            )
            early_fallback = [
                message
                for message in early_messages
                if self._is_bot_message(message)
                and fallback_marker in _message_text(message)
            ]
            self.assertFalse(
                early_fallback,
                "provider replied before the deterministic barrier was released",
            )

            self.api.recall_message(root_id)
            self._release_model_delay_barrier(fallback_marker)
        finally:
            self._release_model_delay_barrier(fallback_marker, required=False)
        observe_start = int(_message_time_ms(follow_up) / 1000) - 2
        deadline = time.monotonic() + self.quiet_seconds
        escaped_replies: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            messages = self.api.list_messages(
                container_type="chat",
                container_id=chat_id,
                start_time=observe_start,
            )
            escaped_replies = [
                message
                for message in messages
                if self._is_bot_message(message)
                and fallback_marker in _message_text(message)
                and str(message.get("thread_id") or "") != thread_id
            ]
            try:
                thread_messages = self.api.list_messages(
                    container_type="thread",
                    container_id=thread_id,
                )
            except FeishuApiError as error:
                if "code=230110" not in str(error):
                    raise
                thread_messages = []
            escaped_replies.extend(
                message
                for message in thread_messages
                if self._is_bot_message(message)
                and fallback_marker in _message_text(message)
            )
            if escaped_replies:
                break
            time.sleep(1)
        self.assertFalse(
            escaped_replies,
            "reply to recalled root appeared outside or inside its deleted thread",
        )

    def _exercise_group(self, *, chat_id: str, label: str) -> None:
        """Exercise mention admission and active-thread context in one group."""
        ignored_marker = self._marker(f"{label}-NO-MENTION")
        ignored_text = "\n".join(
            (
                ignored_marker,
                f"HERMES_E2E_EXPECT:{ignored_marker}",
                f"Reply exactly {ignored_marker}.",
            )
        )
        ignored_root = self.api.create_text_message(chat_id, ignored_text)
        self._assert_no_bot_activity(
            chat_id=chat_id,
            root_message=ignored_root,
            marker=ignored_marker,
        )

        secret = self._marker(f"{label}-SECRET")
        root_ack = self._marker(f"{label}-ROOT-ACK")
        root_marker = self._marker(f"{label}-ROOT")
        prompt = "\n".join(
            (
                root_marker,
                f"HERMES_E2E_REMEMBER:{secret}",
                f"HERMES_E2E_EXPECT:{root_ack}",
                f"Remember {secret}. Reply exactly {root_ack}.",
            )
        )
        api_text = (
            f'<at user_id="{self.bot_open_id}">{self.bot_name}</at> {prompt}'
        )
        root = self.api.create_text_message(chat_id, api_text)
        root_id = str(root["message_id"])
        root_reply, thread_id = self._wait_for_bot_reply(
            root_id=root_id,
            expected_text=root_ack,
            after_ms=_message_time_ms(root),
        )
        self._assert_reply_is_in_root_thread(root_reply, root_id, thread_id)
        root_session = self._wait_for_persisted_session(
            chat_id=chat_id,
            chat_type="group",
            root_id=root_id,
            transcript_markers=(root_marker, root_ack),
        )

        recall_marker = self._marker(f"{label}-RECALL")
        recall_text = "\n".join(
            (
                recall_marker,
                "HERMES_E2E_RECALL",
                "Without mentioning the bot, recall the HERMES_E2E_REMEMBER "
                "value from this thread's root. Reply exactly "
                "HERMES_E2E_CONTEXT:<that value>.",
            )
        )
        follow_up = self.api.reply_text_in_thread(root_id, recall_text)
        context_reply, observed_thread_id = self._wait_for_bot_reply(
            root_id=root_id,
            expected_text=f"HERMES_E2E_CONTEXT:{secret}",
            after_ms=_message_time_ms(follow_up),
        )
        self.assertEqual(observed_thread_id, thread_id)
        self._assert_reply_is_in_root_thread(context_reply, root_id, thread_id)
        active_session = self._wait_for_persisted_session(
            chat_id=chat_id,
            chat_type="group",
            root_id=root_id,
            transcript_markers=(
                root_marker,
                recall_marker,
                f"HERMES_E2E_CONTEXT:{secret}",
            ),
        )
        self.assertEqual(active_session["id"], root_session["id"])

        fresh_marker = self._marker(f"{label}-FRESH")
        fresh_prompt = "\n".join(
            (
                fresh_marker,
                f"HERMES_E2E_EXPECT:{fresh_marker}",
                "HERMES_E2E_RECALL",
                "This is a new group root. Reply with "
                "HERMES_E2E_CONTEXT:MISSING.",
            )
        )
        fresh_api_text = (
            f'<at user_id="{self.bot_open_id}">{self.bot_name}</at> '
            f"{fresh_prompt}"
        )
        fresh_root = self.api.create_text_message(chat_id, fresh_api_text)
        fresh_root_id = str(fresh_root["message_id"])
        fresh_reply, fresh_thread_id = self._wait_for_bot_reply(
            root_id=fresh_root_id,
            expected_text="HERMES_E2E_CONTEXT:MISSING",
            after_ms=_message_time_ms(fresh_root),
        )
        self.assertNotEqual(fresh_root_id, root_id)
        self.assertNotEqual(fresh_thread_id, thread_id)
        self.assertIn(
            f"HERMES_E2E_EXPECT:{fresh_marker}",
            _message_text(fresh_reply),
        )
        self.assertNotIn(secret, _message_text(fresh_reply))
        self._assert_reply_is_in_root_thread(
            fresh_reply,
            fresh_root_id,
            fresh_thread_id,
        )
        fresh_session = self._wait_for_persisted_session(
            chat_id=chat_id,
            chat_type="group",
            root_id=fresh_root_id,
            transcript_markers=(
                fresh_marker,
                "HERMES_E2E_CONTEXT:MISSING",
            ),
        )
        self.assertNotEqual(fresh_session["id"], root_session["id"])
        self.assertNotEqual(
            fresh_session["row"]["session_key"],
            root_session["row"]["session_key"],
        )
        self.assertNotIn(secret, fresh_session["transcript"])

    def _assert_no_bot_activity(
        self,
        *,
        chat_id: str,
        root_message: dict[str, Any],
        marker: str,
    ) -> None:
        """Observe a quiet period after an unmentioned group root."""
        root_id = str(root_message["message_id"])
        start_time = int(_message_time_ms(root_message) / 1000) - 2
        deadline = time.monotonic() + self.quiet_seconds
        while True:
            refreshed_root = self.api.get_message(root_id)
            thread_id = str(
                refreshed_root.get("thread_id")
                or root_message.get("thread_id")
                or ""
            )
            threaded_bot_messages = (
                [
                    message
                    for message in self.api.list_messages(
                        container_type="thread",
                        container_id=thread_id,
                    )
                    if self._is_bot_message(message)
                    and str(message.get("root_id") or "") == root_id
                ]
                if thread_id
                else []
            )
            chat_bot_messages = [
                message
                for message in self.api.list_messages(
                    container_type="chat",
                    container_id=chat_id,
                    start_time=start_time,
                )
                if self._is_bot_message(message)
                and (
                    str(message.get("root_id") or "") == root_id
                    or marker in _message_text(message)
                )
            ]
            self.assertFalse(
                threaded_bot_messages or chat_bot_messages,
                "group root without @ unexpectedly triggered the bot",
            )
            if time.monotonic() >= deadline:
                return
            time.sleep(1)

    def _wait_for_bot_reply(
        self,
        *,
        root_id: str,
        expected_text: str,
        after_ms: int,
    ) -> tuple[dict[str, Any], str]:
        """Wait for an expected bot reply under one canonical message root."""
        observed_thread_id = ""

        def observe() -> tuple[dict[str, Any], str] | None:
            nonlocal observed_thread_id
            root = self.api.get_message(root_id)
            observed_thread_id = str(root.get("thread_id") or observed_thread_id)
            if not observed_thread_id:
                return None
            messages = self.api.list_messages(
                container_type="thread",
                container_id=observed_thread_id,
            )
            for message in messages:
                if (
                    self._is_bot_message(message)
                    and str(message.get("root_id") or "") == root_id
                    and _message_time_ms(message) >= after_ms
                    and expected_text in _message_text(message)
                ):
                    return message, observed_thread_id
            return None

        return _wait_until(
            observe,
            timeout_seconds=self.timeout_seconds,
            description=(
                f"bot reply containing {expected_text} under root {root_id}"
            ),
        )

    def _advance_model_stream(
        self,
        marker: str,
        stage: int,
        *,
        required: bool = True,
    ) -> bool:
        """Release one deterministic provider delta after live observation."""
        url = (
            f"{self.model_stub_url}/e2e/stream-advance?"
            f"{urllib.parse.urlencode({'marker': marker, 'stage': stage})}"
        )
        request = urllib.request.Request(url, data=b"", method="POST")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            if not required:
                return False
            raise AssertionError(
                "failed to advance deterministic model stream "
                f"({type(error).__name__})"
            ) from None
        if not isinstance(payload, dict) or payload.get("advanced") is not True:
            if not required:
                return False
            raise AssertionError(
                "deterministic model rejected the requested stream stage"
            )
        return True

    def _cardkit_entries(
        self,
        *,
        root_id: str,
        thread_id: str = "",
    ) -> list[dict[str, Any]]:
        """Return trace rows owned by one canonical Feishu thread."""
        identities = {root_id}
        if thread_id:
            identities.add(thread_id)
        return [
            entry
            for entry in _read_cardkit_trace(self.cardkit_trace_path)
            if {
                str(entry.get("root_id") or ""),
                str(entry.get("thread_id") or ""),
            }
            & identities
        ]

    def _wait_for_cardkit_trace(
        self,
        *,
        root_id: str,
        thread_id: str,
        predicate: Callable[[list[dict[str, Any]]], Any | None],
        description: str,
    ) -> Any:
        """Wait for a public CardKit trace condition under one root."""
        return _wait_until(
            lambda: predicate(
                self._cardkit_entries(
                    root_id=root_id,
                    thread_id=thread_id,
                )
            ),
            timeout_seconds=self.timeout_seconds,
            description=description,
            interval_seconds=0.1,
        )

    def _wait_for_cardkit_message(
        self,
        *,
        root_id: str,
        after_ms: int,
    ) -> tuple[dict[str, Any], str]:
        """Wait for the one schema-2.0 conversational card under a root."""
        observed_thread_id = ""

        def observe() -> tuple[dict[str, Any], str] | None:
            nonlocal observed_thread_id
            root = self.api.get_message(root_id)
            observed_thread_id = str(root.get("thread_id") or observed_thread_id)
            if not observed_thread_id:
                return None
            messages = self.api.list_messages(
                container_type="thread",
                container_id=observed_thread_id,
            )
            matching = [
                item
                for item in messages
                if self._is_bot_message(item)
                and str(item.get("root_id") or "") == root_id
                and _message_time_ms(item) >= after_ms
                and item.get("msg_type") == "interactive"
                and isinstance(_message_body_content(item), dict)
                and _message_body_content(item).get("schema") == "2.0"
            ]
            if len(matching) > 1:
                raise AssertionError(
                    "CardKit stream created duplicate conversational cards"
                )
            return (
                (matching[0], observed_thread_id)
                if matching
                else None
            )

        return _wait_until(
            observe,
            timeout_seconds=self.timeout_seconds,
            description=f"CardKit conversational card under root {root_id}",
            interval_seconds=0.2,
        )

    def _assert_cardkit_message_remains_single(
        self,
        *,
        root_id: str,
        thread_id: str,
        message_id: str,
        after_ms: int,
    ) -> None:
        """Ensure one turn keeps one conversational CardKit message ID."""
        deadline = time.monotonic() + 3
        while True:
            matching = [
                item
                for item in self.api.list_messages(
                    container_type="thread",
                    container_id=thread_id,
                )
                if self._is_bot_message(item)
                and str(item.get("root_id") or "") == root_id
                and _message_time_ms(item) >= after_ms
                and item.get("msg_type") == "interactive"
                and isinstance(_message_body_content(item), dict)
                and _message_body_content(item).get("schema") == "2.0"
            ]
            self.assertEqual(
                {str(item.get("message_id") or "") for item in matching},
                {message_id},
                "CardKit stream switched or duplicated its message ID",
            )
            if time.monotonic() >= deadline:
                return
            time.sleep(0.5)

    def _wait_for_stream_version(
        self,
        *,
        root_id: str,
        marker: str,
        required_marker: str,
        forbidden_markers: tuple[str, ...],
        after_ms: int,
        message_id: str = "",
        require_finalized: bool = False,
    ) -> tuple[dict[str, Any], str]:
        """Wait for exactly one externally visible revision of a stream."""
        observed_thread_id = ""

        def observe() -> tuple[dict[str, Any], str] | None:
            nonlocal observed_thread_id
            root = self.api.get_message(root_id)
            observed_thread_id = str(root.get("thread_id") or observed_thread_id)
            if not observed_thread_id:
                return None
            messages = self.api.list_messages(
                container_type="thread",
                container_id=observed_thread_id,
            )
            matching = [
                item
                for item in messages
                if self._is_bot_message(item)
                and str(item.get("root_id") or "") == root_id
                and _message_time_ms(item) >= after_ms
                and marker in _message_text(item)
            ]
            if len(matching) > 1:
                raise AssertionError(
                    "stream created duplicate bot messages under one root"
                )
            if not matching:
                return None
            current = matching[0]
            current_id = str(current.get("message_id") or "")
            if message_id and current_id != message_id:
                raise AssertionError(
                    "stream switched message_id instead of editing in place"
                )
            text = _message_text(current)
            if required_marker not in text:
                return None
            unexpected = [item for item in forbidden_markers if item in text]
            if unexpected:
                raise AssertionError(
                    "stream advanced before Feishu exposed the requested stage: "
                    + ", ".join(unexpected)
                )
            if require_finalized and "▉" in text:
                return None
            return current, observed_thread_id

        return _wait_until(
            observe,
            timeout_seconds=self.timeout_seconds,
            description=f"stream revision containing {required_marker}",
            interval_seconds=0.2,
        )

    def _assert_stream_remains_single(
        self,
        *,
        chat_id: str,
        root_id: str,
        thread_id: str,
        message_id: str,
        marker: str,
        start_time: int,
    ) -> None:
        """Ensure finalization creates no duplicate or top-level partial."""
        deadline = time.monotonic() + 3
        while True:
            observed: dict[str, dict[str, Any]] = {}
            for item in self.api.list_messages(
                container_type="thread",
                container_id=thread_id,
            ) + self.api.list_messages(
                container_type="chat",
                container_id=chat_id,
                start_time=start_time,
            ):
                if self._is_bot_message(item) and marker in _message_text(item):
                    observed[str(item.get("message_id") or "")] = item
            self.assertEqual(
                set(observed),
                {message_id},
                "stream left duplicate or top-level partial messages",
            )
            only = observed[message_id]
            self._assert_reply_is_in_root_thread(only, root_id, thread_id)
            if time.monotonic() >= deadline:
                return
            time.sleep(0.5)

    def _wait_for_model_delay_barrier(self, marker: str) -> None:
        """Wait until a model request is blocked before root recall."""
        url = (
            f"{self.model_stub_url}/e2e/delay-barrier-active?"
            f"{urllib.parse.urlencode({'marker': marker})}"
        )

        def observe() -> bool | None:
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
            ) as error:
                raise AssertionError(
                    "deterministic model status endpoint is unavailable "
                    f"({type(error).__name__})"
                ) from None
            if not isinstance(payload, dict):
                raise AssertionError(
                    "deterministic model status endpoint returned invalid JSON"
                )
            return True if payload.get("active") is True else None

        _wait_until(
            observe,
            timeout_seconds=self.timeout_seconds,
            description=f"active model delay barrier for {marker}",
            interval_seconds=0.2,
        )

    def _release_model_delay_barrier(
        self,
        marker: str,
        *,
        required: bool = True,
    ) -> bool:
        """Release one fail-closed model request after root recall."""
        url = (
            f"{self.model_stub_url}/e2e/delay-barrier-release?"
            f"{urllib.parse.urlencode({'marker': marker})}"
        )
        request = urllib.request.Request(url, data=b"", method="POST")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            if not required:
                return False
            raise AssertionError(
                "failed to release deterministic model delay barrier "
                f"({type(error).__name__})"
            ) from None
        if not isinstance(payload, dict) or payload.get("released") is not True:
            if not required:
                return False
            raise AssertionError(
                "deterministic model rejected the delay-barrier release"
            )
        return True

    def _assert_reply_is_in_root_thread(
        self,
        reply: dict[str, Any],
        root_id: str,
        thread_id: str,
    ) -> None:
        """Assert the observable Feishu IDs for one fixed-model reply."""
        self.assertEqual(str(reply.get("root_id") or ""), root_id)
        self.assertEqual(str(reply.get("parent_id") or ""), root_id)
        self.assertEqual(str(reply.get("thread_id") or ""), thread_id)
        self.assertTrue(thread_id.startswith("omt_"), thread_id)
        self.assertTrue(str(reply.get("message_id") or "").startswith("om_"))

    def _is_bot_message(self, message: dict[str, Any]) -> bool:
        """Return whether a message was sent by this app bot."""
        return (
            _sender_type(message) == "app"
            and _sender_id(message) == self.api.app_id
        )

    def _marker(self, label: str) -> str:
        """Build one non-secret marker unique to this test run."""
        return f"HERMES-E2E-{self.run_id}-{label}"


if __name__ == "__main__":
    unittest.main(verbosity=2)
