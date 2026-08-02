"""Sentinel targets for service events that do not originate in IM chats."""

from __future__ import annotations


# Prefix reserved for non-IM gateway sessions whose output needs special routing.
SYNTHETIC_TARGET_PREFIX = "synthetic:"


# Stable chat sentinel for ``vc.bot.meeting_invited_v1`` agent turns.
SYNTHETIC_VC_CHAT_ID = "synthetic:vc-invited"


def is_synthetic_target(chat_id: str) -> bool:
    """Return whether a chat ID names a non-IM service-event target."""
    return str(chat_id or "").startswith(SYNTHETIC_TARGET_PREFIX)
