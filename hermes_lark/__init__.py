"""Registration surface for the Hermes Feishu/Lark plugin."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .adapter import register as _register_platform
from . import openclaw_tools

# Skill identifiers bundled with the plugin distribution.
_SKILL_NAMES = (
    "feishu-bitable",
    "feishu-calendar",
    "feishu-channel-rules",
    "feishu-create-doc",
    "feishu-fetch-doc",
    "feishu-im-read",
    "feishu-task",
    "feishu-troubleshoot",
    "feishu-update-doc",
)

# Recent gateway events bridge Hermes' lifecycle hooks to message-scoped tools.
_RECENT_EVENTS: deque[
    tuple[
        float,
        frozenset[str],
        str,
        str,
        Any,
        openclaw_tools.ToolTicket,
    ]
] = deque(maxlen=1024)
_RECENT_EVENTS_LOCK = threading.RLock()
_RECENT_EVENT_TTL_SECONDS = 5 * 60
# Exact turn bindings keep late parallel tool notifications on their original
# conversational card instead of whichever turn last touched the session.
_CARD_TURN_TICKETS: dict[tuple[str, str], openclaw_tools.ToolTicket] = {}


def _notify_cardkit(ticket: Any, **payload: Any) -> bool:
    """Forward one lifecycle event when the loaded adapter supports CardKit."""
    try:
        from .adapter import notify_cardkit_lifecycle
    except ImportError:
        return False
    return bool(notify_cardkit_lifecycle(ticket, **payload))


def _capture_gateway_event(
    event: Any,
    gateway: Any = None,
    session_store: Any = None,
    **_: Any,
) -> dict[str, str] | None:
    """Remember one Feishu event until Hermes assigns its session ID."""
    from .commands import bind_gateway_command_ticket

    bind_gateway_command_ticket(None)
    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", None)
    if platform != "feishu":
        return
    ticket = openclaw_tools.ticket_from_event(event)
    bind_gateway_command_ticket(ticket)
    source_user_id = (
        source.get("user_id")
        if isinstance(source, Mapping)
        else getattr(source, "user_id", None)
    )
    source_user_id_alt = (
        source.get("user_id_alt")
        if isinstance(source, Mapping)
        else getattr(source, "user_id_alt", None)
    )
    sender_ids = frozenset(
        str(value)
        for value in (
            ticket.sender_open_id,
            source_user_id,
            source_user_id_alt,
        )
        if value not in (None, "")
    )
    text = str(getattr(event, "text", "") or "")
    if not sender_ids:
        return
    session_key = ""
    session_key_resolver = getattr(gateway, "_session_key_for_source", None)
    if callable(session_key_resolver) and source is not None:
        try:
            session_key = str(session_key_resolver(source) or "")
        except Exception:
            session_key = ""
    now = time.time()
    with _RECENT_EVENTS_LOCK:
        while _RECENT_EVENTS and now - _RECENT_EVENTS[0][0] > _RECENT_EVENT_TTL_SECONDS:
            _RECENT_EVENTS.popleft()
        _RECENT_EVENTS.append(
            (now, sender_ids, text, session_key, session_store, ticket)
        )
    parts = text.lstrip().split(maxsplit=1)
    if parts:
        canonical = {
            "/feishu_auth": "/feishu-auth",
            "/feishu_diagnose": "/feishu-diagnose",
            "/feishu_doctor": "/feishu-doctor",
        }.get(parts[0].lower())
        if canonical is not None:
            rewritten = (
                f"{canonical} {parts[1]}"
                if len(parts) > 1
                else canonical
            )
            return {"action": "rewrite", "text": rewritten}
    return None


def _bind_pre_llm_ticket(
    session_id: str,
    turn_id: str = "",
    sender_id: str = "",
    user_message: str = "",
    platform: str = "",
    **_: Any,
) -> None:
    """Bind the latest matching Feishu event to the active Hermes session."""
    if platform != "feishu" or not session_id:
        return
    now = time.time()
    selected: openclaw_tools.ToolTicket | None = None
    with _RECENT_EVENTS_LOCK:
        while _RECENT_EVENTS and now - _RECENT_EVENTS[0][0] > _RECENT_EVENT_TTL_SECONDS:
            _RECENT_EVENTS.popleft()
        for (
            _created_at,
            _event_sender,
            _event_text,
            session_key,
            session_store,
            ticket,
        ) in reversed(_RECENT_EVENTS):
            entries = getattr(session_store, "_entries", None)
            entry = entries.get(session_key) if isinstance(entries, dict) else None
            if str(getattr(entry, "session_id", "") or "") == session_id:
                selected = ticket
                break
        for (
            _created_at,
            event_senders,
            event_text,
            _session_key,
            _session_store,
            ticket,
        ) in reversed(_RECENT_EVENTS):
            if selected is not None:
                break
            if sender_id and sender_id not in event_senders:
                continue
            if user_message and event_text and event_text != user_message:
                continue
            selected = ticket
            break
        if selected is None:
            for (
                _created_at,
                event_senders,
                _event_text,
                _session_key,
                _session_store,
                ticket,
            ) in reversed(_RECENT_EVENTS):
                if not sender_id or sender_id in event_senders:
                    selected = ticket
                    break
    if selected is not None:
        ticket = openclaw_tools.bind_session_ticket(session_id, selected) or selected
        with _RECENT_EVENTS_LOCK:
            _CARD_TURN_TICKETS[(session_id, str(turn_id or ""))] = ticket
        _notify_cardkit(
            ticket,
            kind="turn_bound",
            session_id=session_id,
            turn_id=str(turn_id or ""),
            wait=True,
        )


def _cardkit_ticket(
    session_id: str,
    turn_id: str,
) -> openclaw_tools.ToolTicket | None:
    """Resolve the immutable ticket captured for one exact Hermes turn."""
    with _RECENT_EVENTS_LOCK:
        ticket = _CARD_TURN_TICKETS.get(
            (str(session_id or ""), str(turn_id or ""))
        )
    return ticket or openclaw_tools.get_tool_ticket(session_id=session_id)


def _notify_cardkit_tool_started(
    tool_name: str,
    session_id: str = "",
    turn_id: str = "",
    tool_call_id: str = "",
    **_: Any,
) -> None:
    """Render one tool as running on the active Feishu response card."""
    ticket = _cardkit_ticket(session_id, turn_id)
    if ticket is None:
        return
    _notify_cardkit(
        ticket,
        kind="tool",
        session_id=session_id,
        turn_id=turn_id,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        status="running",
        wait=True,
    )


def _notify_cardkit_tool_completed(
    tool_name: str,
    session_id: str = "",
    turn_id: str = "",
    tool_call_id: str = "",
    status: str = "ok",
    error_message: str = "",
    **_: Any,
) -> None:
    """Render one terminal tool status on the active Feishu response card."""
    ticket = _cardkit_ticket(session_id, turn_id)
    if ticket is None:
        return
    _notify_cardkit(
        ticket,
        kind="tool",
        session_id=session_id,
        turn_id=turn_id,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        status=status,
        detail=str(error_message or "")[:160],
        wait=True,
    )


def _mark_cardkit_turn_terminal(
    session_id: str = "",
    turn_id: str = "",
    **_: Any,
) -> None:
    """Allow the next Hermes stream finalize to close this exact card."""
    ticket = _cardkit_ticket(session_id, turn_id)
    if ticket is None:
        return
    _notify_cardkit(
        ticket,
        kind="turn_terminal",
        session_id=session_id,
        turn_id=turn_id,
        wait=True,
    )


def _unbind_session_ticket(
    session_id: str = "",
    turn_id: str = "",
    **_: Any,
) -> None:
    """Release message identity after a Hermes turn ends."""
    if not session_id:
        return
    key = (session_id, str(turn_id or ""))
    with _RECENT_EVENTS_LOCK:
        ended_ticket = _CARD_TURN_TICKETS.pop(key, None)
        has_newer_turn = any(
            bound_session == session_id
            for bound_session, _bound_turn in _CARD_TURN_TICKETS
        )
    if (
        not has_newer_turn
        and ended_ticket is not None
        and openclaw_tools.get_tool_ticket(session_id=session_id) == ended_ticket
    ):
        openclaw_tools.unbind_session_ticket(session_id)


def _enforce_tool_policy(
    tool_name: str,
    session_id: str = "",
    task_id: str = "",
    **_: Any,
) -> dict[str, str] | None:
    """Apply OpenClaw group tool restrictions to the current Feishu turn."""
    ticket = openclaw_tools.get_tool_ticket(
        session_id=str(session_id or task_id or "")
    )
    if ticket is None:
        return None
    reason = openclaw_tools.evaluate_tool_policy(tool_name, ticket)
    if reason == "channel_deny" and not str(tool_name).startswith("feishu_"):
        return None
    if reason not in {
        "category_disabled",
        "channel_deny",
        "group_deny",
        "group_allowlist",
    }:
        return None
    return {
        "action": "block",
        "message": (
            f"Tool {tool_name} is blocked by the Feishu tool policy "
            f"({reason})."
        ),
    }


def register(ctx: Any) -> None:
    """Register the Feishu platform, tools, hooks, and bundled skills."""
    from .commands import register as register_commands

    _register_platform(ctx)
    openclaw_tools.register(ctx)
    register_commands(ctx)
    ctx.register_hook("pre_gateway_dispatch", _capture_gateway_event)
    ctx.register_hook("pre_llm_call", _bind_pre_llm_ticket)
    ctx.register_hook("pre_tool_call", _enforce_tool_policy)
    ctx.register_hook("pre_tool_call", _notify_cardkit_tool_started)
    ctx.register_hook("post_tool_call", _notify_cardkit_tool_completed)
    ctx.register_hook("post_llm_call", _mark_cardkit_turn_terminal)
    ctx.register_hook("on_session_end", _mark_cardkit_turn_terminal)
    ctx.register_hook("on_session_end", _unbind_session_ticket)

    package_dir = Path(__file__).resolve().parent
    skills_dir = package_dir / "skills"
    if not skills_dir.is_dir():
        skills_dir = package_dir.parent / "skills"

    for skill_name in _SKILL_NAMES:
        ctx.register_skill(
            name=skill_name,
            path=skills_dir / skill_name / "SKILL.md",
        )


# Public surface consumed by the Hermes pip entry-point loader.
__all__ = ["register"]
