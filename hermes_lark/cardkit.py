"""CardKit 2.0 conversational card state and payload builders."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# Card element updated by the CardKit streaming-content API.
STREAMING_ELEMENT_ID = "streaming_content"


# Card element containing interim commentary and long-running status text.
PROGRESS_ELEMENT_ID = "progress_content"


# Stable element IDs used to observe the CardKit lifecycle in live E2E traces.
LIFECYCLE_ELEMENT_ID = "lifecycle_status"
LOADING_ELEMENT_ID = "loading_icon"


# OpenClaw-compatible streaming timing and terminal fallbacks.
CARDKIT_STREAM_THROTTLE_SECONDS = 0.1
CARDKIT_LONG_GAP_SECONDS = 2.0
CARDKIT_BATCH_AFTER_GAP_SECONDS = 0.3
CARDKIT_RATE_LIMIT_BACKOFF_SECONDS = 0.5
CARDKIT_EMPTY_REPLY_FALLBACK = "Done."
CARDKIT_SILENT_REPLY_TOKEN = "NO_REPLY"
CARDKIT_MARKDOWN_TABLE_LIMIT = 3
CARDKIT_IMAGE_RESOLUTION_TIMEOUT_SECONDS = 15.0


# Markdown tables inside fenced code are not rendered as CardKit table elements.
_CARDKIT_FENCED_CODE_RE = re.compile(r"```[\s\S]*?```")
_CARDKIT_MARKDOWN_TABLE_RE = re.compile(
    r"\|.+\|[\r\n]+\|[-:| ]+\|[\s\S]*?(?=\n\n|\n(?!\|)|$)"
)


# Complete Markdown image references accepted by Feishu cards.
_CARDKIT_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")


# Module logger for non-fatal remote-image resolution failures.
_LOGGER = logging.getLogger(__name__)


class CardKitImageResolver:
    """Replace remote Markdown image URLs with uploaded Feishu image keys."""

    def __init__(
        self,
        upload: Callable[[str], Awaitable[Optional[str]]],
        *,
        on_resolved: Callable[[], None],
    ) -> None:
        self._upload = upload
        self._on_resolved = on_resolved
        self._resolved: dict[str, str] = {}
        self._pending: dict[str, asyncio.Task[Optional[str]]] = {}
        self._failed: set[str] = set()

    def resolve_images(self, content: str) -> str:
        """Resolve cached images and start uploads for new remote URLs."""
        text = str(content or "")
        if "![" not in text:
            return text

        def replace(match: re.Match[str]) -> str:
            """Resolve one complete Markdown image reference."""
            alt_text, value = match.groups()
            if value.startswith("img_"):
                return match.group(0)
            if not value.startswith(("http://", "https://")):
                return ""
            image_key = self._resolved.get(value)
            if image_key:
                return f"![{alt_text}]({image_key})"
            if value in self._failed or value in self._pending:
                return ""
            self._pending[value] = asyncio.create_task(self._upload_image(value))
            return ""

        return _CARDKIT_MARKDOWN_IMAGE_RE.sub(replace, text)

    async def resolve_images_await(
        self,
        content: str,
        *,
        timeout_seconds: float = CARDKIT_IMAGE_RESOLUTION_TIMEOUT_SECONDS,
    ) -> str:
        """Wait a bounded time for pending image uploads before resolving."""
        text = str(content or "")
        self.resolve_images(text)
        pending = tuple(self._pending.values())
        if pending:
            done, _unfinished = await asyncio.wait(
                pending,
                timeout=max(0.0, float(timeout_seconds)),
            )
            for task in done:
                try:
                    task.result()
                except Exception:
                    pass
        return self.resolve_images(text)

    async def _upload_image(self, url: str) -> Optional[str]:
        """Upload one URL once and notify the active card on success."""
        try:
            image_key = str(await self._upload(url) or "").strip()
            if not image_key.startswith("img_"):
                raise ValueError("Feishu image upload omitted a valid image key")
            self._resolved[url] = image_key
            self._on_resolved()
            return image_key
        except Exception as error:
            self._failed.add(url)
            _LOGGER.warning(
                "CardKit remote image resolution failed (url_sha256=%s, error=%s)",
                hashlib.sha256(
                    url.encode("utf-8", errors="replace")
                ).hexdigest()[:12],
                type(error).__name__,
            )
            return None
        finally:
            self._pending.pop(url, None)


class CardKitFlushController:
    """Coalesce cumulative CardKit writes behind one throttled flush task."""

    def __init__(
        self,
        flush: Callable[[], Awaitable[None]],
        *,
        throttle_seconds: float = CARDKIT_STREAM_THROTTLE_SECONDS,
        long_gap_seconds: float = CARDKIT_LONG_GAP_SECONDS,
        batch_after_gap_seconds: float = CARDKIT_BATCH_AFTER_GAP_SECONDS,
    ) -> None:
        self._flush = flush
        self._throttle_seconds = max(0.0, float(throttle_seconds))
        self._long_gap_seconds = max(0.0, float(long_gap_seconds))
        self._batch_after_gap_seconds = max(
            0.0,
            float(batch_after_gap_seconds),
        )
        self._task: Optional[asyncio.Task[None]] = None
        self._flush_started = False
        self._needs_reflush = False
        self._minimum_reflush_delay = 0.0
        self._last_update_at: Optional[float] = None
        self._completed = False

    @property
    def pending(self) -> bool:
        """Return whether a delayed or in-flight flush still exists."""
        return self._task is not None and not self._task.done()

    def mark_ready(self) -> None:
        """Start the throttle clock once the CardKit message is visible."""
        self._last_update_at = asyncio.get_running_loop().time()

    def request(self, *, minimum_delay: float = 0.0) -> None:
        """Request a cumulative write, coalescing it with pending work."""
        if self._completed:
            return
        requested_delay = max(0.0, float(minimum_delay))
        if self.pending:
            if self._flush_started:
                self._needs_reflush = True
                self._minimum_reflush_delay = max(
                    self._minimum_reflush_delay,
                    requested_delay,
                )
            return

        loop = asyncio.get_running_loop()
        now = loop.time()
        last_update_at = self._last_update_at
        if last_update_at is None:
            self._last_update_at = now
            delay = self._throttle_seconds
        else:
            elapsed = now - last_update_at
            if elapsed > self._long_gap_seconds:
                delay = self._batch_after_gap_seconds
            else:
                delay = max(0.0, self._throttle_seconds - elapsed)
        self._task = loop.create_task(
            self._run_after(max(delay, requested_delay))
        )

    async def complete(self) -> None:
        """Cancel a pending timer and wait for any API write already in flight."""
        self.stop()
        task = self._task
        if task is None:
            return
        if task is asyncio.current_task():
            return
        if not self._flush_started and not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if self._task is task:
            self._task = None

    def stop(self) -> None:
        """Reject future flush requests without waiting for current work."""
        self._completed = True

    async def _run_after(self, delay: float) -> None:
        """Wait for the throttle window and execute one serialized flush."""
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            if self._completed:
                return
            self._flush_started = True
            await self._flush()
            self._last_update_at = asyncio.get_running_loop().time()
        finally:
            self._flush_started = False
            current = asyncio.current_task()
            if self._task is current:
                self._task = None
            if self._needs_reflush and not self._completed:
                delay = self._minimum_reflush_delay
                self._needs_reflush = False
                self._minimum_reflush_delay = 0.0
                self.request(minimum_delay=delay)


@dataclass(frozen=True)
class CardKitToolStatus:
    """User-visible state for one Hermes tool call."""

    tool_call_id: str
    name: str
    status: str
    detail: str = ""


@dataclass
class CardKitConversationState:
    """Mutable state and write serialization for one conversational card."""

    chat_id: str
    thread_id: str
    card_id: str = ""
    message_id: str = ""
    content: str = ""
    progress_content: str = ""
    heartbeat_content: str = ""
    last_flushed_content: str = ""
    tools: dict[str, Any] = field(default_factory=dict)
    closed: bool = False
    unavailable: bool = False
    streaming_disabled: bool = False
    full_update_pending: bool = False
    stream_retry_count: int = 0
    sequence: int = 0
    trace_path: Optional[Path] = None
    image_resolver: Optional[CardKitImageResolver] = field(
        default=None,
        repr=False,
        compare=False,
    )
    flush_controller: Optional[CardKitFlushController] = field(
        default=None,
        repr=False,
        compare=False,
    )
    lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        repr=False,
        compare=False,
    )

    def next_sequence(self) -> int:
        """Reserve the next API sequence while the caller holds ``lock``."""
        self.sequence += 1
        return self.sequence

    def update_tool(
        self,
        tool_call_id: str,
        *,
        name: str,
        status: str,
        detail: str = "",
    ) -> CardKitToolStatus:
        """Create or replace the visible status of one tool call."""
        tool = CardKitToolStatus(
            tool_call_id=tool_call_id,
            name=name,
            status=status,
            detail=detail,
        )
        self.tools[tool_call_id] = tool
        return tool

    async def record_trace(
        self,
        operation: str,
        *,
        ok: bool,
        sequence: int,
        state: str,
        content: Optional[str] = None,
        card: Optional[Mapping[str, Any]] = None,
        code: Any = None,
    ) -> Optional[dict[str, Any]]:
        """Append one E2E API observation when an explicit trace path exists."""
        if self.trace_path is None:
            return None
        record = build_trace_record(
            self,
            operation=operation,
            ok=ok,
            sequence=sequence,
            state=state,
            content=content,
            card=card,
            code=code,
        )
        trace_path = Path(self.trace_path)

        def append_json_line() -> None:
            """Perform the blocking append outside the event loop."""
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            with trace_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")

        await asyncio.to_thread(append_json_line)
        return record


def should_buffer_silent_reply(
    content: str,
    *,
    visible_content: str = "",
) -> bool:
    """Keep a leading silent token out of a card until its intent is known."""
    normalized = str(content or "").strip()
    return (
        not str(visible_content or "").strip()
        and bool(normalized)
        and CARDKIT_SILENT_REPLY_TOKEN.startswith(normalized)
    )


def terminal_cardkit_content(
    content: str,
    *,
    visible_fallback: str = "",
) -> str:
    """Return terminal text without leaking a silent token or an empty card."""
    normalized = str(content or "").strip()
    if not normalized or CARDKIT_SILENT_REPLY_TOKEN.startswith(normalized):
        return str(visible_fallback or "").strip() or CARDKIT_EMPTY_REPLY_FALLBACK
    return str(content)


def sanitize_terminal_cardkit_markdown(
    content: str,
    *,
    table_limit: int = CARDKIT_MARKDOWN_TABLE_LIMIT,
) -> str:
    """Render tables beyond CardKit's limit as code instead of failing."""
    text = str(content or "")
    fenced_ranges = [
        (match.start(), match.end())
        for match in _CARDKIT_FENCED_CODE_RE.finditer(text)
    ]
    tables = [
        match
        for match in _CARDKIT_MARKDOWN_TABLE_RE.finditer(text)
        if not any(start <= match.start() < end for start, end in fenced_ranges)
    ]
    keep_count = max(0, int(table_limit))
    if len(tables) <= keep_count:
        return text
    for match in reversed(tables[keep_count:]):
        raw = match.group(0)
        replacement = f"```\n{raw}\n```"
        text = text[: match.start()] + replacement + text[match.end() :]
    return text


def _tool_status_snapshot(tools: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """Normalize dataclass and mapping tool states for cards and traces."""
    snapshot: dict[str, dict[str, str]] = {}
    for tool_call_id, tool in tools.items():
        if isinstance(tool, Mapping):
            name = str(tool.get("name") or tool_call_id)
            status = str(tool.get("status") or "running")
            detail = str(tool.get("detail") or "")
        else:
            name = str(getattr(tool, "name", tool_call_id))
            status = str(getattr(tool, "status", "running"))
            detail = str(getattr(tool, "detail", ""))
        snapshot[str(tool_call_id)] = {
            "name": name,
            "status": status,
            "detail": detail,
        }
    return snapshot


def build_trace_record(
    conversation: CardKitConversationState,
    *,
    operation: str,
    ok: bool,
    sequence: int,
    state: str,
    content: Optional[str] = None,
    card: Optional[Mapping[str, Any]] = None,
    code: Any = None,
) -> dict[str, Any]:
    """Build the stable JSONL record observed by live CardKit E2E tests."""
    return {
        "operation": operation,
        "status": state,
        "ok": ok,
        "code": code,
        "card_id": conversation.card_id,
        "message_id": conversation.message_id,
        "chat_id": conversation.chat_id,
        "thread_id": conversation.thread_id,
        "sequence": sequence,
        "content": conversation.content if content is None else content,
        "card": dict(card) if card is not None else None,
        "tool_status": _tool_status_snapshot(conversation.tools),
    }


def cardkit_streaming_enabled(
    config: Mapping[str, Any],
    *,
    chat_type: str,
) -> bool:
    """Return whether one chat should use conversational CardKit streaming."""
    if config.get("streaming") is not True:
        return False
    normalized_chat_type = str(chat_type).strip().lower()
    configured_mode = config.get("replyMode") or config.get("reply_mode") or "auto"
    if isinstance(configured_mode, Mapping):
        scene = "group" if normalized_chat_type == "group" else "direct"
        configured_mode = (
            configured_mode.get(scene)
            or configured_mode.get("default")
            or "auto"
        )
    reply_mode = str(configured_mode).strip().lower()
    if reply_mode == "static":
        return False
    if reply_mode == "streaming":
        return True
    return normalized_chat_type in {"dm", "p2p"}


def build_initial_card() -> dict[str, Any]:
    """Build the Processing/Thinking card shown before model output arrives."""
    return {
        "schema": "2.0",
        "config": {
            "streaming_mode": True,
            "locales": ["zh_cn", "en_us"],
            "summary": {
                "content": "Processing...",
                "i18n_content": {
                    "zh_cn": "Processing...",
                    "en_us": "Processing...",
                },
            },
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "💭 **Thinking...**",
                    "i18n_content": {
                        "zh_cn": "💭 **Thinking...**",
                        "en_us": "💭 **Thinking...**",
                    },
                    "text_size": "notation",
                    "element_id": LIFECYCLE_ELEMENT_ID,
                },
                {
                    "tag": "markdown",
                    "content": "",
                    "text_align": "left",
                    "text_size": "normal_v2",
                    "margin": "0px 0px 0px 0px",
                    "element_id": STREAMING_ELEMENT_ID,
                },
                {
                    "tag": "markdown",
                    "content": " ",
                    "icon": {
                        "tag": "custom_icon",
                        "img_key": (
                            "img_v3_02vb_496bec09-4b43-4773-ad6b-0cdd103cd2bg"
                        ),
                        "size": "16px 16px",
                    },
                    "element_id": LOADING_ELEMENT_ID,
                },
            ]
        },
    }


def _build_tool_panel(
    tools: Mapping[str, Any],
    *,
    expanded: bool,
) -> dict[str, Any]:
    """Build the collapsible tool-call status panel."""
    status_styles = {
        "running": ("Running", "turquoise"),
        "success": ("Succeeded", "green"),
        "error": ("Failed", "red"),
    }
    elements: list[dict[str, Any]] = []
    for tool in _tool_status_snapshot(tools).values():
        label, color = status_styles.get(
            tool["status"],
            (tool["status"].capitalize(), "grey"),
        )
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**{tool['name']}** · "
                        f"<font color='{color}'>{label}</font>"
                    ),
                    "text_size": "notation",
                },
            }
        )
        if tool["detail"]:
            elements.append(
                {
                    "tag": "div",
                    "margin": "0px 0px 0px 22px",
                    "text": {
                        "tag": "plain_text",
                        "content": tool["detail"],
                        "text_color": "grey",
                        "text_size": "notation",
                    },
                }
            )
    return {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "🛠️ Tool use",
                "i18n_content": {
                    "zh_cn": "🛠️ Tool use",
                    "en_us": "🛠️ Tool use",
                },
                "text_color": "grey",
                "text_size": "notation",
            },
            "vertical_align": "center",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "color": "grey",
                "size": "16px 16px",
            },
            "icon_position": "right",
            "icon_expanded_angle": -180,
        },
        "border": {"color": "grey", "corner_radius": "5px"},
        "vertical_spacing": "4px",
        "padding": "8px 8px 8px 8px",
        "elements": elements,
    }


def _build_lifecycle_card(
    *,
    lifecycle_content: str,
    lifecycle_content_zh: str,
    summary: str,
    summary_zh: str,
    content: str,
    streaming: bool,
    tools: Optional[Mapping[str, Any]],
    progress_content: str = "",
    heartbeat_content: str = "",
) -> dict[str, Any]:
    """Build one full CardKit lifecycle snapshot."""
    visible_content = (
        content if streaming else sanitize_terminal_cardkit_markdown(content)
    )
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": lifecycle_content,
            "i18n_content": {
                "zh_cn": lifecycle_content_zh,
                "en_us": lifecycle_content,
            },
            "text_size": "notation",
            "element_id": LIFECYCLE_ELEMENT_ID,
        }
    ]
    if tools:
        elements.append(_build_tool_panel(tools, expanded=streaming))
    progress_parts = [
        value.strip()
        for value in (progress_content, heartbeat_content)
        if value.strip()
    ]
    if streaming and progress_parts:
        elements.append(
            {
                "tag": "markdown",
                "content": "\n\n".join(progress_parts),
                "text_align": "left",
                "text_size": "notation",
                "margin": "0px 0px 0px 0px",
                "element_id": PROGRESS_ELEMENT_ID,
            }
        )
    elements.append(
        {
            "tag": "markdown",
            "content": visible_content,
            "text_align": "left",
            "text_size": "normal_v2",
            "margin": "0px 0px 0px 0px",
            "element_id": STREAMING_ELEMENT_ID,
        }
    )
    if streaming:
        elements.append(
            {
                "tag": "markdown",
                "content": " ",
                "icon": {
                    "tag": "custom_icon",
                    "img_key": (
                        "img_v3_02vb_496bec09-4b43-4773-ad6b-0cdd103cd2bg"
                    ),
                    "size": "16px 16px",
                },
                "element_id": LOADING_ELEMENT_ID,
            }
        )
    return {
        "schema": "2.0",
        "config": {
            "streaming_mode": streaming,
            "locales": ["zh_cn", "en_us"],
            "summary": {
                "content": summary,
                "i18n_content": {
                    "zh_cn": summary_zh,
                    "en_us": summary,
                },
            },
        },
        "body": {"elements": elements},
    }


def build_generating_card(
    content: str,
    *,
    tools: Optional[Mapping[str, Any]] = None,
    progress_content: str = "",
    heartbeat_content: str = "",
) -> dict[str, Any]:
    """Build a streaming full-card snapshot while output is being generated."""
    return _build_lifecycle_card(
        lifecycle_content="✍️ **Generating...**",
        lifecycle_content_zh="✍️ **Generating...**",
        summary="Generating...",
        summary_zh="Generating...",
        content=content,
        streaming=True,
        tools=tools,
        progress_content=progress_content,
        heartbeat_content=heartbeat_content,
    )


def build_complete_card(
    content: str,
    *,
    tools: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build the closed successful card without a loading indicator."""
    return _build_lifecycle_card(
        lifecycle_content="✅ **Complete**",
        lifecycle_content_zh="✅ **Complete**",
        summary="Complete",
        summary_zh="Complete",
        content=content,
        streaming=False,
        tools=tools,
    )


def build_stopped_card(
    content: str,
    *,
    tools: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build the closed card used when an active turn is interrupted."""
    return _build_lifecycle_card(
        lifecycle_content="⏹️ **Stopped**",
        lifecycle_content_zh="⏹️ **Stopped**",
        summary="Stopped",
        summary_zh="Stopped",
        content=content,
        streaming=False,
        tools=tools,
    )


def build_error_card(
    message: str,
    *,
    tools: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build the closed failed card without a loading indicator."""
    return _build_lifecycle_card(
        lifecycle_content="❌ **Error**",
        lifecycle_content_zh="❌ **Error**",
        summary="Error",
        summary_zh="Error",
        content=message,
        streaming=False,
        tools=tools,
    )
