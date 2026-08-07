"""Hermes bridge for the tools shipped by ``larksuite/openclaw-lark``.

The 37 request/response tools execute in a one-shot Node subprocess built from
the pinned upstream TypeScript. The two tools whose OpenClaw implementation
depends on daemon-owned card callbacks use an explicit pending-interaction
contract so the Hermes host can complete the lifecycle without pretending that
the callback happened inside the subprocess.
"""

from __future__ import annotations

import contextlib
import contextvars
import copy
import json
import os
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Union


_PACKAGE_DIR = Path(__file__).resolve().parent
_TOOL_INVENTORY_PATH = _PACKAGE_DIR / "data" / "openclaw-tools.json"
_BRIDGE_PATH = _PACKAGE_DIR / "node" / "openclaw_tools_bridge.mjs"
_DIRECT_TOOL_COUNT = 37
_INTERACTIVE_TOOLS = frozenset(
    {
        "feishu_oauth_batch_auth",
        "feishu_ask_user_question",
    }
)
_TOOL_CATEGORY_DEFAULTS = {
    "doc": True,
    "wiki": True,
    "drive": True,
    "scopes": True,
    "perm": False,
    "mail": True,
    "sheets": True,
    "okr": False,
}
_TOOL_CATEGORIES = {
    "feishu_search_doc_wiki": "doc",
    "feishu_fetch_doc": "doc",
    "feishu_create_doc": "doc",
    "feishu_update_doc": "doc",
    "feishu_drive_file": "drive",
    "feishu_doc_comments": "drive",
    "feishu_doc_media": "drive",
    "feishu_wiki_space": "wiki",
    "feishu_wiki_space_node": "wiki",
    "feishu_sheet": "sheets",
}
_ASK_USER_TTL_SECONDS = 5 * 60
_OAUTH_TTL_SECONDS = 15 * 60
_DEFAULT_BRIDGE_TIMEOUT_SECONDS: Optional[float] = None
_ticket_context: contextvars.ContextVar[Optional["ToolTicket"]] = contextvars.ContextVar(
    "hermes_lark_openclaw_ticket",
    default=None,
)
_session_tickets: Dict[str, "ToolTicket"] = {}
_pending_interactions: Dict[str, "PendingInteraction"] = {}
_state_lock = threading.RLock()
_bridge_config_snapshot: Optional[Dict[str, Any]] = None
_bridge_config_snapshots: Dict[str, Dict[str, Any]] = {}
_yaml_backed_bridge_scopes: set[str] = set()


@dataclass(frozen=True)
class ToolTicket:
    """Message identity required by user-scoped upstream tool calls."""

    session_id: str
    message_id: str
    chat_id: str
    account_id: str = "default"
    profile: str = "default"
    profile_scope: str = ""
    sender_open_id: str = ""
    sender_user_id: str = ""
    sender_union_id: str = ""
    chat_type: Optional[str] = None
    thread_id: Optional[str] = None
    session_thread_id: Optional[str] = None

    def to_bridge_dict(self) -> Dict[str, Any]:
        """Return the OpenClaw ticket shape accepted by the Node bridge."""
        result: Dict[str, Any] = {
            "messageId": self.message_id,
            "chatId": self.chat_id,
            "accountId": self.account_id,
        }
        if self.sender_open_id:
            result["senderOpenId"] = self.sender_open_id
        if self.chat_type in {"p2p", "group"}:
            result["chatType"] = self.chat_type
        if self.thread_id:
            result["threadId"] = self.thread_id
        return result


@dataclass(frozen=True)
class UserAccessToken:
    """Ephemeral UAT supplied by the Hermes host for one invocation."""

    access_token: str
    refresh_token: str = ""
    scope: str = ""
    expires_at: Optional[int] = None
    refresh_expires_at: Optional[int] = None

    def to_bridge_dict(self) -> Dict[str, Any]:
        """Return the token shape consumed by the bundled upstream store."""
        result: Dict[str, Any] = {"accessToken": self.access_token}
        if self.refresh_token:
            result["refreshToken"] = self.refresh_token
        if self.scope:
            result["scope"] = self.scope
        if self.expires_at is not None:
            result["expiresAt"] = self.expires_at
        if self.refresh_expires_at is not None:
            result["refreshExpiresAt"] = self.refresh_expires_at
        return result


@dataclass(frozen=True)
class PendingInteraction:
    """Host-owned continuation for an interaction OpenClaw keeps in its daemon."""

    token: str
    kind: str
    tool_name: str
    session_id: str
    ticket: ToolTicket
    request: Dict[str, Any]
    context: Dict[str, Any]
    created_at: float
    expires_at: float

    def public_dict(self) -> Dict[str, Any]:
        """Return the non-secret continuation state exposed to the host."""
        return {
            "token": self.token,
            "kind": self.kind,
            "tool_name": self.tool_name,
            "session_id": self.session_id,
            "ticket": asdict(self.ticket),
            "request": copy.deepcopy(self.request),
            "context": copy.deepcopy(self.context),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


TokenProvider = Callable[
    [ToolTicket],
    Optional[Union[UserAccessToken, Mapping[str, Any]]],
]
InteractionHost = Callable[[PendingInteraction], bool]
InteractionExpiryHost = Callable[[PendingInteraction], bool]
_token_provider: Optional[TokenProvider] = None
_interaction_hosts: Dict[tuple[str, str], InteractionHost] = {}
_interaction_expiry_hosts: Dict[tuple[str, str], InteractionExpiryHost] = {}
_interaction_expiry_timers: Dict[str, threading.Timer] = {}


def _read_value(value: Any, *names: str) -> Any:
    """Read the first populated key or attribute from an event-like value."""
    for name in names:
        if isinstance(value, Mapping):
            candidate = value.get(name)
        else:
            candidate = getattr(value, name, None)
        if candidate not in (None, ""):
            return candidate
    return None


def _nested_value(value: Any, *path: str) -> Any:
    """Read one nested event value without requiring SDK event classes."""
    current = value
    for name in path:
        current = _read_value(current, name)
        if current is None:
            return None
    return current


def ticket_from_event(event: Any, session_id: str = "") -> ToolTicket:
    """Build a tool ticket from a Hermes ``MessageEvent`` or Feishu payload."""
    if isinstance(event, ToolTicket):
        if session_id and event.session_id != session_id:
            return ToolTicket(
                session_id=session_id,
                message_id=event.message_id,
                chat_id=event.chat_id,
                account_id=event.account_id,
                profile=event.profile,
                profile_scope=event.profile_scope,
                sender_open_id=event.sender_open_id,
                sender_user_id=event.sender_user_id,
                sender_union_id=event.sender_union_id,
                chat_type=event.chat_type,
                thread_id=event.thread_id,
                session_thread_id=event.session_thread_id,
            )
        return event

    raw = _read_value(event, "raw_message", "rawMessage") or event
    raw_event = _read_value(raw, "event") or raw
    message = _read_value(raw_event, "message") or _read_value(event, "message") or event
    source = _read_value(event, "source")
    sender = _read_value(raw_event, "sender") or _read_value(event, "sender")
    sender_id = _read_value(sender, "sender_id", "senderId") or sender
    continuation = _read_value(
        raw,
        "openclaw_continuation",
        "openclawContinuation",
    )
    continuation_event = (
        _read_value(continuation, "synthetic_event", "syntheticEvent")
        or continuation
    )
    continuation_ticket = _read_value(continuation_event, "ticket") or {}
    operator = _read_value(raw_event, "operator")
    reaction_user_id = _read_value(raw_event, "user_id", "userId")
    inviter = _read_value(raw_event, "inviter")
    inviter_id = _read_value(inviter, "id") or inviter
    transport_ref = _read_value(source, "_transport_adapter_ref")
    transport_adapter = transport_ref() if callable(transport_ref) else None
    metadata = _read_value(event, "metadata") or {}
    comment_metadata = (
        _read_value(metadata, "feishu_drive_comment", "feishuDriveComment")
        or {}
    )

    message_id = str(
        _read_value(event, "message_id", "messageId")
        or _read_value(message, "message_id", "messageId")
        or ""
    )
    chat_id = str(
        _read_value(source, "chat_id_alt", "chatIdAlt")
        or _read_value(source, "chat_id", "chatId")
        or _read_value(message, "chat_id", "chatId")
        or ""
    )
    sender_open_id = str(
        _read_value(comment_metadata, "sender_open_id", "senderOpenId")
        or _read_value(continuation_ticket, "sender_open_id", "senderOpenId")
        or _read_value(sender_id, "open_id", "openId")
        or _read_value(operator, "open_id", "openId")
        or _read_value(reaction_user_id, "open_id", "openId")
        or _read_value(inviter_id, "open_id", "openId")
        or _read_value(source, "user_id_alt", "userIdAlt")
        or _read_value(source, "user_id", "userId")
        or ""
    )
    sender_user_id = str(
        _read_value(continuation_ticket, "sender_user_id", "senderUserId")
        or _read_value(sender_id, "user_id", "userId")
        or _read_value(operator, "user_id", "userId")
        or _read_value(reaction_user_id, "user_id", "userId")
        or _read_value(inviter_id, "user_id", "userId")
        or _read_value(source, "feishu_user_id", "feishuUserId")
        or _read_value(source, "user_id", "userId")
        or ""
    )
    sender_union_id = str(
        _read_value(continuation_ticket, "sender_union_id", "senderUnionId")
        or _read_value(sender_id, "union_id", "unionId")
        or _read_value(operator, "union_id", "unionId")
        or _read_value(reaction_user_id, "union_id", "unionId")
        or _read_value(inviter_id, "union_id", "unionId")
        or _read_value(
            source,
            "feishu_user_id_alt",
            "feishuUserIdAlt",
        )
        or _read_value(source, "user_id_alt", "userIdAlt")
        or ""
    )
    chat_type = _read_value(message, "chat_type", "chatType")
    if chat_type not in {"p2p", "group"}:
        source_chat_type = str(_read_value(source, "chat_type", "chatType") or "")
        chat_type = "p2p" if source_chat_type in {"dm", "direct", "p2p"} else "group"
    session_thread_id = str(
        _read_value(
            source,
            "feishu_session_thread_id",
            "feishuSessionThreadId",
        )
        or _read_value(source, "thread_id", "threadId")
        or ""
    )
    thread_id = str(
        _read_value(source, "feishu_thread_id", "feishuThreadId")
        or _read_value(message, "thread_id", "threadId")
        or ""
    )
    if not _read_value(
        source,
        "feishu_session_thread_id",
        "feishuSessionThreadId",
    ):
        thread_id = str(
            thread_id
            or _read_value(message, "root_id", "rootId")
            or _read_value(source, "thread_id", "threadId")
            or ""
        )
    account_id = str(
        _read_value(event, "account_id", "accountId")
        or _read_value(source, "scope_id", "scopeId")
        or _nested_value(raw, "account_id")
        or "default"
    )
    profile = str(
        _read_value(source, "profile")
        or _read_value(continuation_ticket, "profile")
        or "default"
    )
    profile_scope = str(
        _read_value(transport_adapter, "_profile_scope_key")
        or _read_value(continuation_ticket, "profile_scope", "profileScope")
        or ""
    )
    resolved_session = str(
        session_id
        or _read_value(event, "session_id", "sessionId")
        or _read_value(source, "session_id", "sessionId")
        or ""
    )
    return ToolTicket(
        session_id=resolved_session,
        message_id=message_id,
        chat_id=chat_id,
        account_id=account_id,
        profile=profile,
        profile_scope=profile_scope,
        sender_open_id=sender_open_id,
        sender_user_id=sender_user_id,
        sender_union_id=sender_union_id,
        chat_type=str(chat_type),
        thread_id=thread_id or None,
        session_thread_id=session_thread_id or None,
    )


def bind_session_ticket(session_id: str, event_or_ticket: Any) -> ToolTicket:
    """Associate a Hermes session with its latest Feishu message ticket."""
    normalized_session = str(session_id or "").strip()
    if not normalized_session:
        raise ValueError("session_id is required")
    ticket = ticket_from_event(event_or_ticket, normalized_session)
    with _state_lock:
        _session_tickets[normalized_session] = ticket
    return ticket


def unbind_session_ticket(session_id: str) -> None:
    """Remove a session ticket when the Hermes session is discarded."""
    with _state_lock:
        _session_tickets.pop(str(session_id or ""), None)


@contextlib.contextmanager
def tool_ticket(ticket_or_event: Any, session_id: str = "") -> Iterator[ToolTicket]:
    """Temporarily bind a ticket through nested or asynchronous tool dispatch."""
    ticket = ticket_from_event(ticket_or_event, session_id)
    token = _ticket_context.set(ticket)
    try:
        yield ticket
    finally:
        _ticket_context.reset(token)


def get_tool_ticket(
    session_id: Optional[str] = None,
    event: Any = None,
    ticket: Any = None,
) -> Optional[ToolTicket]:
    """Resolve a tool ticket from explicit, contextual, or session state."""
    normalized_session = str(session_id or "")
    if ticket is not None:
        return ticket_from_event(ticket, normalized_session)
    if event is not None:
        return ticket_from_event(event, normalized_session)
    contextual = _ticket_context.get()
    if contextual is not None:
        return contextual
    if normalized_session:
        with _state_lock:
            return _session_tickets.get(normalized_session)
    return None


def configure_token_provider(provider: Optional[TokenProvider]) -> Optional[TokenProvider]:
    """Set the host callback that supplies user tokens without persisting them."""
    global _token_provider
    previous = _token_provider
    _token_provider = provider
    return previous


def _interaction_host_key(profile_scope: str, account_id: str) -> tuple[str, str]:
    """Normalize one profile-owned Feishu account host key."""
    normalized_profile = str(profile_scope or "default").strip() or "default"
    normalized_account = str(account_id or "default").strip().lower() or "default"
    return normalized_profile, normalized_account


def register_interaction_host(
    account_id: str,
    host: InteractionHost,
    *,
    profile_scope: str = "default",
    expiry_host: Optional[InteractionExpiryHost] = None,
) -> None:
    """Register the live adapter that can deliver interactive Feishu cards."""
    key = _interaction_host_key(profile_scope, account_id)
    with _state_lock:
        _interaction_hosts[key] = host
        if expiry_host is None:
            _interaction_expiry_hosts.pop(key, None)
        else:
            _interaction_expiry_hosts[key] = expiry_host


def unregister_interaction_host(
    account_id: str,
    host: Optional[InteractionHost] = None,
    *,
    profile_scope: str = "default",
) -> None:
    """Remove an adapter interaction host without clobbering its replacement."""
    key = _interaction_host_key(profile_scope, account_id)
    with _state_lock:
        current = _interaction_hosts.get(key)
        if host is None or current is host:
            _interaction_hosts.pop(key, None)
            _interaction_expiry_hosts.pop(key, None)


def interaction_host_available(
    account_id: str,
    *,
    profile_scope: str = "default",
) -> bool:
    """Return whether the account currently has a connected interaction host."""
    key = _interaction_host_key(profile_scope, account_id)
    with _state_lock:
        return key in _interaction_hosts


def _optional_int(value: Any) -> Optional[int]:
    """Parse an optional integer timestamp from configuration."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _token_from_value(value: Any) -> Optional[UserAccessToken]:
    """Normalize a provider or environment token into the bridge model."""
    if value is None:
        return None
    if isinstance(value, UserAccessToken):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("token provider must return UserAccessToken, a mapping, or None")
    access_token = str(value.get("access_token") or value.get("accessToken") or "")
    if not access_token:
        return None
    return UserAccessToken(
        access_token=access_token,
        refresh_token=str(value.get("refresh_token") or value.get("refreshToken") or ""),
        scope=str(value.get("scope") or ""),
        expires_at=_optional_int(value.get("expires_at") or value.get("expiresAt")),
        refresh_expires_at=_optional_int(
            value.get("refresh_expires_at") or value.get("refreshExpiresAt")
        ),
    )


def _resolve_user_token(ticket: Optional[ToolTicket]) -> Optional[UserAccessToken]:
    """Resolve a UAT from the configured provider or process environment."""
    if ticket is not None and _token_provider is not None:
        return _token_from_value(_token_provider(ticket))
    access_token = str(
        _read_profile_env("FEISHU_USER_ACCESS_TOKEN") or ""
    ).strip()
    if not access_token:
        return None
    return UserAccessToken(
        access_token=access_token,
        refresh_token=str(
            _read_profile_env("FEISHU_USER_REFRESH_TOKEN") or ""
        ).strip(),
        scope=str(
            _read_profile_env("FEISHU_USER_ACCESS_TOKEN_SCOPES") or ""
        ).strip(),
        expires_at=_optional_int(
            _read_profile_env("FEISHU_USER_ACCESS_TOKEN_EXPIRES_AT")
        ),
        refresh_expires_at=_optional_int(
            _read_profile_env("FEISHU_USER_REFRESH_TOKEN_EXPIRES_AT")
        ),
    )


def configure_bridge_config(
    config: Optional[Mapping[str, Any]],
    *,
    yaml_backed: bool = False,
) -> None:
    """Replace the process-local OpenClaw config used by tool calls."""
    global _bridge_config_snapshot
    snapshot = copy.deepcopy(dict(config)) if config is not None else None
    scope_key = _bridge_scope_key()
    with _state_lock:
        _bridge_config_snapshot = snapshot
        if snapshot is None:
            _bridge_config_snapshots.clear()
            _yaml_backed_bridge_scopes.clear()
        else:
            _bridge_config_snapshots[scope_key] = snapshot
            if yaml_backed:
                _yaml_backed_bridge_scopes.add(scope_key)
            else:
                _yaml_backed_bridge_scopes.discard(scope_key)
    try:
        from tools.registry import invalidate_check_fn_cache

        invalidate_check_fn_cache()
    except (ImportError, ModuleNotFoundError):
        pass


def _bridge_scope_key() -> str:
    """Return the active Hermes home used to isolate profile snapshots."""
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        return ""
    try:
        return str(Path(get_hermes_home()).expanduser().resolve())
    except (OSError, RuntimeError, TypeError, ValueError):
        return ""


def _read_profile_env(name: str) -> Optional[str]:
    """Read one bridge setting without crossing a Hermes profile scope."""
    try:
        from agent.secret_scope import (
            current_secret_scope,
            get_secret,
            is_multiplex_active,
        )
    except ImportError:
        return os.environ.get(name)
    if current_secret_scope() is not None:
        return get_secret(name, None)
    if is_multiplex_active():
        return None
    return os.environ.get(name)


def _yaml_feishu_config_active() -> Optional[bool]:
    """Return whether the current profile still has an enabled Feishu block."""
    try:
        from hermes_cli.config import load_config_readonly
    except ImportError:
        return None
    try:
        config = load_config_readonly()
    except Exception:
        return False
    if not isinstance(config, Mapping):
        return False

    candidate: Any = config.get("feishu")
    if not isinstance(candidate, Mapping):
        gateway = config.get("gateway")
        gateway_map = gateway if isinstance(gateway, Mapping) else {}
        gateway_platforms = gateway_map.get("platforms")
        gateway_platform_map = (
            gateway_platforms
            if isinstance(gateway_platforms, Mapping)
            else {}
        )
        candidate = gateway_platform_map.get("feishu")
    if not isinstance(candidate, Mapping):
        platforms = config.get("platforms")
        platform_map = platforms if isinstance(platforms, Mapping) else {}
        candidate = platform_map.get("feishu")
    if not isinstance(candidate, Mapping):
        return False
    enabled = candidate.get("enabled", True)
    return not (
        enabled is False
        or str(enabled).strip().lower() in {"false", "0", "no", "off"}
    )


def _normalize_feishu_bridge_values(values: Mapping[str, Any]) -> Dict[str, Any]:
    """Flatten Hermes extras and expose transport keys in OpenClaw form."""
    normalized = dict(values)
    nested_extra = normalized.pop("extra", None)
    if isinstance(nested_extra, Mapping):
        for key, value in nested_extra.items():
            normalized.setdefault(str(key), value)
    aliases = {
        "app_id": "appId",
        "app_secret": "appSecret",
        "connection_mode": "connectionMode",
        "encrypt_key": "encryptKey",
        "verification_token": "verificationToken",
        "webhook_host": "webhookHost",
        "webhook_path": "webhookPath",
        "webhook_port": "webhookPort",
    }
    for source, target in aliases.items():
        if source in normalized and target not in normalized:
            normalized[target] = normalized[source]
    return normalized


@lru_cache(maxsize=1)
def load_tool_manifest() -> Dict[str, Any]:
    """Load and validate the exact tool inventory extracted from upstream."""
    with _TOOL_INVENTORY_PATH.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    tools = manifest.get("tools")
    if manifest.get("channels") != ["feishu"] or not isinstance(tools, list):
        raise RuntimeError("invalid openclaw-lark tool inventory")
    names = [tool.get("name") for tool in tools]
    if len(tools) != 39 or len(set(names)) != 39:
        raise RuntimeError("openclaw-lark tool inventory must contain 39 unique tools")
    return manifest


def _bridge_config() -> Dict[str, Any]:
    """Build the OpenClaw-shaped config consumed by the upstream tool code."""
    scope_key = _bridge_scope_key()
    with _state_lock:
        snapshot = _bridge_config_snapshots.get(scope_key)
        yaml_backed = scope_key in _yaml_backed_bridge_scopes
    if yaml_backed and _yaml_feishu_config_active() is False:
        with _state_lock:
            _bridge_config_snapshots.pop(scope_key, None)
            _yaml_backed_bridge_scopes.discard(scope_key)
        snapshot = None
    with _state_lock:
        config = copy.deepcopy(snapshot) or {}
    raw_override = str(
        _read_profile_env("FEISHU_OPENCLAW_CONFIG_JSON") or ""
    ).strip()
    explicit_override = bool(raw_override)
    if raw_override:
        override = json.loads(raw_override)
        if not isinstance(override, dict):
            raise ValueError("FEISHU_OPENCLAW_CONFIG_JSON must contain a JSON object")
        config = override

    channels = dict(config.get("channels") or {})
    raw_feishu = channels.get("feishu")
    feishu = _normalize_feishu_bridge_values(
        raw_feishu if isinstance(raw_feishu, Mapping) else {}
    )
    raw_accounts = feishu.get("accounts")
    if isinstance(raw_accounts, Mapping):
        feishu["accounts"] = {
            raw_account_id: _normalize_feishu_bridge_values(account)
            if isinstance(account, Mapping)
            else account
            for raw_account_id, account in raw_accounts.items()
        }

    feishu.setdefault("enabled", True)
    accounts_only = bool(feishu.get("_accounts_only"))
    for key, env_name in (
        ("appId", "FEISHU_APP_ID"),
        ("appSecret", "FEISHU_APP_SECRET"),
        ("domain", "FEISHU_DOMAIN"),
    ):
        if accounts_only:
            continue
        env_value = str(_read_profile_env(env_name) or "").strip()
        if not env_value:
            continue
        if explicit_override:
            feishu.setdefault(key, env_value)
        else:
            feishu[key] = env_value
    feishu.setdefault("appId", "")
    feishu.setdefault("appSecret", "")
    if not str(feishu.get("domain") or "").strip():
        feishu["domain"] = "feishu"
    channels["feishu"] = feishu
    config["channels"] = channels

    plugins = dict(config.get("plugins") or {})
    entries = dict(plugins.get("entries") or {})
    legacy_entry = dict(entries.get("feishu") or {})
    legacy_entry["enabled"] = False
    entries["feishu"] = legacy_entry
    plugins["entries"] = entries
    config["plugins"] = plugins
    return config


def _matches_tool_pattern(tool_name: str, values: Any) -> bool:
    """Match exact names and OpenClaw's trailing-star policy patterns."""
    if not isinstance(values, (list, tuple)):
        return False
    for raw_pattern in values:
        if not isinstance(raw_pattern, str):
            continue
        pattern = raw_pattern.strip()
        if not pattern:
            continue
        if pattern == "*":
            return True
        if pattern.endswith("*"):
            if tool_name.startswith(pattern[:-1]):
                return True
        elif tool_name == pattern:
            return True
    return False


def _merge_account_feishu_config(
    config: Mapping[str, Any],
    account_id: str,
) -> Dict[str, Any]:
    """Apply OpenClaw's one-level account override semantics."""
    channels = config.get("channels")
    channel_map = channels if isinstance(channels, Mapping) else {}
    raw_feishu = channel_map.get("feishu")
    feishu = dict(raw_feishu) if isinstance(raw_feishu, Mapping) else {}
    accounts = feishu.pop("accounts", None)
    normalized_account = str(account_id or "default").strip().lower() or "default"
    if normalized_account == "default" or not isinstance(accounts, Mapping):
        return feishu

    override: Optional[Mapping[str, Any]] = None
    for raw_id, candidate in accounts.items():
        if str(raw_id).strip().lower() == normalized_account and isinstance(
            candidate, Mapping
        ):
            override = candidate
            break
    if override is None:
        return feishu

    merged = dict(feishu)
    for key, value in override.items():
        if value is None:
            continue
        base_value = merged.get(key)
        if isinstance(base_value, Mapping) and isinstance(value, Mapping):
            merged[key] = {**base_value, **value}
        else:
            merged[key] = value
    return merged


def _enabled_account_tool_configs(
    feishu: Mapping[str, Any],
) -> List[Mapping[str, Any]]:
    """Return enabled account configs using OpenClaw's shallow inheritance."""
    base = dict(feishu)
    raw_accounts = base.pop("accounts", None)
    configs: List[Mapping[str, Any]] = []
    has_base_credentials = bool(
        (base.get("appId") or base.get("app_id"))
        and (base.get("appSecret") or base.get("app_secret"))
    )
    if has_base_credentials and base.get("enabled", True) is not False:
        configs.append(base)
    if isinstance(raw_accounts, Mapping):
        for raw_override in raw_accounts.values():
            if not isinstance(raw_override, Mapping):
                continue
            merged = dict(base)
            for key, value in raw_override.items():
                if value is None:
                    continue
                current = merged.get(key)
                if isinstance(current, Mapping) and isinstance(value, Mapping):
                    merged[key] = {**current, **value}
                else:
                    merged[key] = value
            if merged.get("enabled", True) is False:
                continue
            if not (
                (merged.get("appId") or merged.get("app_id"))
                and (merged.get("appSecret") or merged.get("app_secret"))
            ):
                continue
            configs.append(merged)
    return configs


def _tool_category_enabled(
    tool_name: str,
    feishu: Mapping[str, Any],
) -> bool:
    """Apply one resolved account's category configuration."""
    category = _TOOL_CATEGORIES.get(tool_name)
    if category is None:
        return True
    raw_tools = feishu.get("tools")
    tools = raw_tools if isinstance(raw_tools, Mapping) else {}
    return tools.get(category, _TOOL_CATEGORY_DEFAULTS[category]) is not False


def _tool_category_registered(
    tool_name: str,
    feishu: Mapping[str, Any],
) -> bool:
    """Expose a category globally when any enabled account provides it."""
    category = _TOOL_CATEGORIES.get(tool_name)
    if category is None:
        return True
    accounts = _enabled_account_tool_configs(feishu)
    if not accounts:
        return _tool_category_enabled(tool_name, feishu)
    for account in accounts:
        if _tool_category_enabled(tool_name, account):
            return True
    return False


def _evaluate_tool_policy(
    tool_name: str,
    ticket: Optional[ToolTicket],
    config: Mapping[str, Any],
) -> Optional[str]:
    """Evaluate channel and group policy against an already parsed config."""
    feishu = _merge_account_feishu_config(
        config,
        ticket.account_id if ticket is not None else "default",
    )
    tools = feishu.get("tools")
    channel_tools = tools if isinstance(tools, Mapping) else {}
    if _matches_tool_pattern(tool_name, channel_tools.get("deny")):
        return "channel_deny"
    if not _tool_category_enabled(tool_name, feishu):
        return "category_disabled"

    if ticket is None or ticket.chat_type != "group" or not ticket.chat_id:
        return None
    groups = feishu.get("groups")
    if not isinstance(groups, Mapping):
        return None
    lowered_chat_id = ticket.chat_id.strip().lower()
    group_config: Optional[Mapping[str, Any]] = None
    for raw_group_id, candidate in groups.items():
        if (
            str(raw_group_id).strip().lower() == lowered_chat_id
            and isinstance(candidate, Mapping)
        ):
            group_config = candidate
            break
    if group_config is None:
        return None

    raw_policy = group_config.get("tools")
    policy = raw_policy if isinstance(raw_policy, Mapping) else {}
    if _matches_tool_pattern(tool_name, policy.get("deny")):
        return "group_deny"
    allow = policy.get("allow")
    if isinstance(allow, (list, tuple)) and allow:
        if not _matches_tool_pattern(tool_name, allow):
            return "group_allowlist"
    return None


def evaluate_tool_policy(
    tool_name: str,
    ticket: Any = None,
) -> Optional[str]:
    """Return a stable denial reason for any Hermes tool, or ``None``."""
    resolved_ticket: Optional[ToolTicket]
    if ticket is None:
        resolved_ticket = get_tool_ticket()
    else:
        resolved_ticket = ticket_from_event(ticket)
    try:
        config = _bridge_config()
    except (TypeError, ValueError, json.JSONDecodeError):
        return "config_invalid"
    return _evaluate_tool_policy(str(tool_name or ""), resolved_ticket, config)


def _bridge_timeout_seconds() -> Optional[float]:
    """Return the optional host deadline without shortening upstream calls."""
    raw = str(os.getenv("FEISHU_OPENCLAW_TOOL_TIMEOUT_SECONDS", "") or "").strip()
    if not raw or raw.lower() in {"none", "off", "disabled", "0"}:
        return _DEFAULT_BRIDGE_TIMEOUT_SECONDS
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_BRIDGE_TIMEOUT_SECONDS
    return timeout if timeout > 0 else _DEFAULT_BRIDGE_TIMEOUT_SECONDS


def _run_bridge(request: Dict[str, Any]) -> Dict[str, Any]:
    """Execute one bundled upstream tool request over the stdin/stdout protocol."""
    if not _BRIDGE_PATH.is_file():
        return {
            "ok": False,
            "error": {
                "code": "bridge_not_built",
                "message": f"OpenClaw tool bridge is missing: {_BRIDGE_PATH}",
            },
        }
    try:
        completed = subprocess.run(
            ["node", str(_BRIDGE_PATH)],
            input=json.dumps(request, ensure_ascii=False, separators=(",", ":")),
            capture_output=True,
            check=False,
            text=True,
            timeout=_bridge_timeout_seconds(),
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "error": {
                "code": "node_not_found",
                "message": "Node.js 22 or newer is required for openclaw-lark tools",
            },
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": {
                "code": "bridge_timeout",
                "message": "The Feishu tool request exceeded its configured timeout",
            },
        }

    stdout = completed.stdout.strip()
    if completed.returncode != 0 or not stdout:
        diagnostic = completed.stderr.strip()
        if len(diagnostic) > 2000:
            diagnostic = diagnostic[-2000:]
        return {
            "ok": False,
            "error": {
                "code": "bridge_process_error",
                "message": diagnostic or f"Node bridge exited with status {completed.returncode}",
            },
        }
    try:
        response = json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": {
                "code": "bridge_protocol_error",
                "message": "Node bridge returned a non-JSON response",
            },
        }
    if not isinstance(response, dict):
        return {
            "ok": False,
            "error": {
                "code": "bridge_protocol_error",
                "message": "Node bridge response must be a JSON object",
            },
        }
    return response


def _prune_pending(now: Optional[float] = None) -> None:
    """Discard continuations whose callback window has elapsed."""
    cutoff = time.time() if now is None else now
    expired = [
        token
        for token, interaction in _pending_interactions.items()
        if interaction.expires_at <= cutoff
    ]
    expired_interactions: List[PendingInteraction] = []
    for token in expired:
        timer = _interaction_expiry_timers.pop(token, None)
        if timer is not None:
            timer.cancel()
        interaction = _pending_interactions.pop(token, None)
        if interaction is not None:
            expired_interactions.append(interaction)
    for interaction in expired_interactions:
        _deliver_interaction_expiry(interaction)


def _store_interaction(
    kind: str,
    tool_name: str,
    params: Mapping[str, Any],
    ticket: ToolTicket,
    ttl_seconds: float,
    context: Optional[Mapping[str, Any]] = None,
) -> PendingInteraction:
    """Create one opaque continuation token for the Hermes host."""
    now = time.time()
    interaction = PendingInteraction(
        token=uuid.uuid4().hex,
        kind=kind,
        tool_name=tool_name,
        session_id=ticket.session_id,
        ticket=ticket,
        request=dict(params),
        context=dict(context or {}),
        created_at=now,
        expires_at=now + ttl_seconds,
    )
    with _state_lock:
        _prune_pending(now)
        _pending_interactions[interaction.token] = interaction
    return interaction


def _deliver_interaction(interaction: PendingInteraction) -> bool:
    """Ask the live account adapter to own one pending continuation."""
    key = _interaction_host_key(
        interaction.ticket.profile_scope or interaction.ticket.profile,
        interaction.ticket.account_id,
    )
    with _state_lock:
        host = _interaction_hosts.get(key)
    if host is None:
        return False
    try:
        return bool(host(interaction))
    except Exception:
        return False


def _deliver_interaction_expiry(interaction: PendingInteraction) -> bool:
    """Ask the live adapter to render one interaction's expired state."""
    key = _interaction_host_key(
        interaction.ticket.profile_scope or interaction.ticket.profile,
        interaction.ticket.account_id,
    )
    with _state_lock:
        host = _interaction_expiry_hosts.get(key)
    if host is None:
        return False
    try:
        return bool(host(interaction))
    except Exception:
        return False


def _expire_interaction(token: str) -> None:
    """Consume one timed-out interaction and notify its connected host."""
    with _state_lock:
        _interaction_expiry_timers.pop(token, None)
        interaction = _pending_interactions.pop(token, None)
    if interaction is not None:
        _deliver_interaction_expiry(interaction)


def _arm_interaction_expiry(interaction: PendingInteraction) -> None:
    """Arm the active expiry lifecycle used by AskUserQuestion cards."""
    delay = max(0.0, interaction.expires_at - time.time())
    timer = threading.Timer(delay, _expire_interaction, args=(interaction.token,))
    timer.daemon = True
    with _state_lock:
        if interaction.token not in _pending_interactions:
            return
        previous = _interaction_expiry_timers.pop(interaction.token, None)
        if previous is not None:
            previous.cancel()
        _interaction_expiry_timers[interaction.token] = timer
    timer.start()


def get_pending_interaction(token: str) -> Optional[Dict[str, Any]]:
    """Return one live pending interaction without consuming it."""
    with _state_lock:
        _prune_pending()
        interaction = _pending_interactions.get(str(token or ""))
        return interaction.public_dict() if interaction is not None else None


def list_pending_interactions(session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """List live continuations, optionally restricted to one Hermes session."""
    with _state_lock:
        _prune_pending()
        interactions = sorted(
            _pending_interactions.values(),
            key=lambda item: item.created_at,
        )
        if session_id is not None:
            interactions = [
                item for item in interactions if item.session_id == str(session_id)
            ]
        return [item.public_dict() for item in interactions]


def cancel_interaction(token: str) -> bool:
    """Cancel one pending continuation without fabricating a callback."""
    with _state_lock:
        _prune_pending()
        normalized = str(token or "")
        timer = _interaction_expiry_timers.pop(normalized, None)
        if timer is not None:
            timer.cancel()
        return _pending_interactions.pop(normalized, None) is not None


def resume_interaction(token: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Consume a host callback and return the synthetic event to re-inject."""
    normalized_token = str(token or "")
    with _state_lock:
        _prune_pending()
        timer = _interaction_expiry_timers.pop(normalized_token, None)
        if timer is not None:
            timer.cancel()
        interaction = _pending_interactions.pop(normalized_token, None)
    if interaction is None:
        return {
            "ok": False,
            "error": {
                "code": "interaction_not_found",
                "message": "The interaction token is unknown or expired",
            },
        }
    return {
        "ok": True,
        "status": "resumed",
        "synthetic_event": {
            "type": f"feishu.{interaction.kind}.callback",
            "session_id": interaction.session_id,
            "tool_name": interaction.tool_name,
            "ticket": asdict(interaction.ticket),
            "payload": dict(payload),
        },
        "next_action": {
            "kind": "inject_synthetic_event",
            "retry_original_tool": interaction.kind in {"oauth", "app_permission"},
            "original_arguments": copy.deepcopy(interaction.request),
        },
    }


def _missing_ticket_result(tool_name: str) -> str:
    """Return a stable lifecycle error when no inbound Feishu event is bound."""
    return json.dumps(
        {
            "ok": False,
            "error": {
                "code": "missing_event_ticket",
                "message": (
                    f"{tool_name} requires a bound Feishu message ticket; "
                    "call bind_session_ticket() or use tool_ticket() during dispatch"
                ),
            },
        },
        ensure_ascii=False,
        indent=2,
    )


def _begin_interactive_tool(
    tool_name: str,
    params: Mapping[str, Any],
    ticket: Optional[ToolTicket],
    *,
    resume_previous_operation: bool = True,
) -> str:
    """Expose daemon-owned OAuth and card work as an honest host continuation."""
    if ticket is None or not ticket.message_id or not ticket.chat_id:
        return _missing_ticket_result(tool_name)
    if tool_name == "feishu_ask_user_question":
        kind = "ask_user_question"
        ttl_seconds = _ASK_USER_TTL_SECONDS
    else:
        kind = "oauth_batch_auth"
        ttl_seconds = _OAUTH_TTL_SECONDS
    context = (
        {"resume_previous_operation": resume_previous_operation}
        if kind == "oauth_batch_auth"
        else None
    )
    interaction = _store_interaction(
        kind,
        tool_name,
        params,
        ticket,
        ttl_seconds,
        context=context,
    )
    if tool_name == "feishu_ask_user_question":
        questions = params.get("questions")
        if not isinstance(questions, list) or not 1 <= len(questions) <= 6:
            cancel_interaction(interaction.token)
            return json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "invalid_questions",
                        "message": "questions must contain between 1 and 6 items",
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        delivered = _deliver_interaction(interaction)
        if not delivered:
            cancel_interaction(interaction.token)
            return json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "interaction_host_unavailable",
                        "message": "The active Feishu adapter could not send the question card",
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        _arm_interaction_expiry(interaction)
        return json.dumps(
            {
                "status": "pending",
                "question_id": interaction.token,
                "message": (
                    "Question card sent to the user. Their answers will arrive "
                    "as a follow-up message in the conversation."
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    delivered = _deliver_interaction(interaction)
    if not delivered:
        cancel_interaction(interaction.token)
        return json.dumps(
            {
                "ok": False,
                "error": {
                    "code": "interaction_host_unavailable",
                    "message": (
                        "The active Feishu adapter could not start the OAuth "
                        "authorization flow"
                    ),
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    return json.dumps(
        {
            "ok": False,
            "status": "pending",
            "error": "authorization_pending",
            "message": (
                "The live Feishu adapter accepted the OAuth request. "
                "Authorization has not completed yet."
            ),
            "follow_up": {
                "kind": kind,
                "token": interaction.token,
                "expires_at": interaction.expires_at,
                "callback_api": "hermes_lark.openclaw_tools.resume_interaction",
                "request": copy.deepcopy(dict(params)),
            },
        },
        ensure_ascii=False,
        indent=2,
    )


def _extract_follow_up(result: Any) -> Optional[Mapping[str, Any]]:
    """Find a structured authorization continuation in an upstream result."""
    if not isinstance(result, Mapping):
        return None
    details = result.get("details")
    if not isinstance(details, Mapping):
        return None
    follow_up = details.get("follow_up")
    return follow_up if isinstance(follow_up, Mapping) else None


def _format_bridge_result(
    response: Mapping[str, Any],
    tool_name: str,
    params: Mapping[str, Any],
    ticket: Optional[ToolTicket],
) -> str:
    """Convert the bridge envelope into the string Hermes tool handlers return."""
    if not response.get("ok"):
        return json.dumps(response, ensure_ascii=False, indent=2)
    result = response.get("result")
    follow_up = _extract_follow_up(result)
    if follow_up is not None:
        mutable = copy.deepcopy(result)
        details = mutable.setdefault("details", {})
        continuation = details.setdefault("follow_up", {})
        if ticket is None:
            continuation["status"] = "blocked"
            continuation["error"] = "missing_event_ticket"
            continuation["message"] = (
                "Bind the inbound Feishu event before retrying this tool"
            )
        else:
            kind = str(follow_up.get("kind") or "oauth")
            interaction = _store_interaction(
                kind,
                tool_name,
                params,
                ticket,
                _OAUTH_TTL_SECONDS,
                context={"authorization": copy.deepcopy(dict(details))},
            )
            if _deliver_interaction(interaction):
                continuation["status"] = "pending"
                continuation["token"] = interaction.token
                continuation["expires_at"] = interaction.expires_at
            else:
                cancel_interaction(interaction.token)
                continuation["status"] = "blocked"
                continuation["error"] = "interaction_host_unavailable"
                continuation["message"] = (
                    "The active Feishu adapter could not start the authorization flow"
                )
        content = mutable.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            content[0]["text"] = json.dumps(details, ensure_ascii=False, indent=2)
        result = mutable

    if isinstance(result, Mapping):
        content = result.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                    return str(item["text"])
    return json.dumps(result, ensure_ascii=False, indent=2)


def invoke_openclaw_tool(
    tool_name: str,
    params: Optional[Mapping[str, Any]] = None,
    *,
    session_id: Optional[str] = None,
    event: Any = None,
    ticket: Any = None,
    tool_call_id: Optional[str] = None,
    resume_previous_operation: bool = True,
) -> str:
    """Invoke one of the 39 pinned upstream tools by public name."""
    arguments = dict(params or {})
    tools_by_name = {
        tool["name"]: tool for tool in load_tool_manifest()["tools"]
    }
    if tool_name not in tools_by_name:
        return json.dumps(
            {
                "ok": False,
                "error": {
                    "code": "unknown_tool",
                    "message": f"Unknown openclaw-lark tool: {tool_name}",
                },
            },
            ensure_ascii=False,
            indent=2,
        )

    resolved_ticket = get_tool_ticket(
        session_id=session_id,
        event=event,
        ticket=ticket,
    )
    try:
        bridge_config = _bridge_config()
    except (TypeError, ValueError, json.JSONDecodeError):
        denial_reason = "config_invalid"
        bridge_config = {}
    else:
        denial_reason = _evaluate_tool_policy(
            tool_name,
            resolved_ticket,
            bridge_config,
        )
    if denial_reason is not None:
        return json.dumps(
            {
                "ok": False,
                "error": {
                    "code": "tool_policy_denied",
                    "message": f"Tool {tool_name} is denied by Feishu tool policy",
                    "reason": denial_reason,
                    "tool_name": tool_name,
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    if tool_name in _INTERACTIVE_TOOLS:
        return _begin_interactive_tool(
            tool_name,
            arguments,
            resolved_ticket,
            resume_previous_operation=resume_previous_operation,
        )

    request: Dict[str, Any] = {
        "action": "invoke",
        "tool": tool_name,
        "arguments": arguments,
        "config": bridge_config,
        "toolCallId": tool_call_id or uuid.uuid4().hex,
    }
    if resolved_ticket is not None:
        request["ticket"] = resolved_ticket.to_bridge_dict()
    user_token = _resolve_user_token(resolved_ticket)
    if user_token is not None:
        request["userToken"] = user_token.to_bridge_dict()
    response = _run_bridge(request)
    return _format_bridge_result(
        response,
        tool_name,
        arguments,
        resolved_ticket,
    )


def _registered_handler(tool_name: str) -> Callable[..., str]:
    """Create a Hermes handler bound to one manifest entry."""

    def handler(params: Optional[Mapping[str, Any]], **kwargs: Any) -> str:
        session_id = kwargs.get("session_id") or kwargs.get("task_id")
        return invoke_openclaw_tool(
            tool_name,
            params,
            session_id=str(session_id) if session_id is not None else None,
            event=kwargs.get("event"),
            ticket=kwargs.get("ticket"),
            tool_call_id=kwargs.get("tool_call_id"),
        )

    return handler


def _registered_tool_available(tool_name: str) -> Callable[[], bool]:
    """Create a runtime registry gate for one configured Feishu tool."""

    def available() -> bool:
        try:
            config = _bridge_config()
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        channels = config.get("channels")
        channel_map = channels if isinstance(channels, Mapping) else {}
        raw_feishu = channel_map.get("feishu")
        feishu = raw_feishu if isinstance(raw_feishu, Mapping) else {}
        raw_tools = feishu.get("tools")
        tools = raw_tools if isinstance(raw_tools, Mapping) else {}
        if _matches_tool_pattern(tool_name, tools.get("deny")):
            return False
        return _tool_category_registered(tool_name, feishu)

    return available


def register(ctx: Any) -> None:
    """Register all 39 upstream tool schemas and executable handlers."""
    manifest = load_tool_manifest()
    for tool in manifest["tools"]:
        schema = {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": copy.deepcopy(tool["parameters"]),
        }
        ctx.register_tool(
            name=tool["name"],
            toolset="feishu",
            schema=schema,
            handler=_registered_handler(tool["name"]),
            check_fn=_registered_tool_available(tool["name"]),
        )


__all__ = [
    "InteractionExpiryHost",
    "PendingInteraction",
    "ToolTicket",
    "UserAccessToken",
    "bind_session_ticket",
    "cancel_interaction",
    "configure_bridge_config",
    "configure_token_provider",
    "evaluate_tool_policy",
    "get_pending_interaction",
    "get_tool_ticket",
    "interaction_host_available",
    "invoke_openclaw_tool",
    "list_pending_interactions",
    "load_tool_manifest",
    "register_interaction_host",
    "register",
    "resume_interaction",
    "ticket_from_event",
    "tool_ticket",
    "unregister_interaction_host",
    "unbind_session_ticket",
]
