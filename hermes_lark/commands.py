"""In-channel Feishu commands aligned with ``larksuite/openclaw-lark``."""

from __future__ import annotations

import asyncio
import contextvars
import json
import platform as runtime_platform
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata
from typing import Any, Mapping, Optional

from . import openclaw_tools


_command_ticket: contextvars.ContextVar[
    Optional[openclaw_tools.ToolTicket]
] = contextvars.ContextVar(
    "hermes_lark_command_ticket",
    default=None,
)


@dataclass(frozen=True)
class _AccountConfig:
    """Normalized Feishu account values needed by command diagnostics."""

    account_id: str
    name: str
    enabled: bool
    app_id: str
    app_secret: str
    brand: str

    @property
    def configured(self) -> bool:
        """Return whether both application credentials are present."""
        return bool(self.app_id and self.app_secret)


def bind_gateway_command_ticket(
    ticket: Optional[openclaw_tools.ToolTicket],
) -> None:
    """Bind the authoritative inbound ticket to this gateway message task."""
    _command_ticket.set(ticket)


def current_gateway_command_ticket() -> Optional[openclaw_tools.ToolTicket]:
    """Return the ticket captured by this task's pre-dispatch hook."""
    return _command_ticket.get()


def _plugin_version() -> str:
    """Return the installed distribution version for command output."""
    try:
        return metadata.version("hermes-lark")
    except metadata.PackageNotFoundError:
        return "0.1.0"


def _command_ticket_or_error() -> tuple[
    Optional[openclaw_tools.ToolTicket],
    Optional[str],
]:
    """Require a complete Feishu identity instead of guessing CLI context."""
    ticket = current_gateway_command_ticket()
    if (
        ticket is None
        or not ticket.message_id
        or not ticket.chat_id
        or not ticket.sender_open_id
    ):
        return (
            None,
            "❌ Unable to resolve the current Feishu message identity. "
            "Send the command again in a Feishu conversation.",
        )
    return ticket, None


def _read_mapping(value: Any) -> Mapping[str, Any]:
    """Return a mapping view for configuration values."""
    return value if isinstance(value, Mapping) else {}


def _account_from_values(
    account_id: str,
    values: Mapping[str, Any],
) -> _AccountConfig:
    """Normalize one shallow-merged OpenClaw-style account definition."""
    app_id = str(values.get("appId") or values.get("app_id") or "").strip()
    app_secret = str(
        values.get("appSecret") or values.get("app_secret") or ""
    ).strip()
    configured = bool(app_id and app_secret)
    return _AccountConfig(
        account_id=str(account_id or "default").strip().lower() or "default",
        name=str(values.get("name") or "").strip(),
        enabled=bool(values.get("enabled", configured)),
        app_id=app_id,
        app_secret=app_secret,
        brand=str(
            values.get("domain") or values.get("brand") or "feishu"
        ).strip()
        or "feishu",
    )


def _load_accounts() -> dict[str, _AccountConfig]:
    """Resolve top-level and per-account configuration using upstream merging."""
    config = openclaw_tools._bridge_config()
    channels = _read_mapping(config.get("channels"))
    feishu = dict(_read_mapping(channels.get("feishu")))
    account_values = _read_mapping(feishu.pop("accounts", None))

    accounts: dict[str, _AccountConfig] = {}
    base_id = str(feishu.get("_account_id") or "default").strip().lower()
    has_base_identity = bool(
        feishu.get("appId")
        or feishu.get("app_id")
        or feishu.get("appSecret")
        or feishu.get("app_secret")
    )
    if has_base_identity or not account_values:
        base = _account_from_values(base_id, feishu)
        accounts[base.account_id] = base

    for raw_account_id, raw_override in account_values.items():
        if not isinstance(raw_override, Mapping):
            continue
        account_id = str(raw_account_id or "").strip().lower()
        if not account_id:
            continue
        merged = dict(feishu)
        for key, value in raw_override.items():
            if value is None:
                continue
            current = merged.get(key)
            if isinstance(current, Mapping) and isinstance(value, Mapping):
                merged[key] = {**current, **value}
            else:
                merged[key] = value
        accounts[account_id] = _account_from_values(account_id, merged)
    return accounts


def _help_text() -> str:
    """Return the unified command help using the upstream command names."""
    return (
        f"Feishu Hermes Plugin v{_plugin_version()}\n\n"
        "Usage:\n"
        "  /feishu start - Validate plugin configuration and connection status\n"
        "  /feishu auth - Authorize the current user's permissions in bulk\n"
        "  /feishu doctor - Diagnose the current Feishu account\n"
        "  /feishu help - Show this help"
    )


def _select_account(
    ticket: openclaw_tools.ToolTicket,
) -> tuple[Optional[_AccountConfig], Optional[str]]:
    """Select only the account named by the authoritative inbound ticket."""
    try:
        accounts = _load_accounts()
    except Exception as error:
        return None, f"❌ Unable to read the Feishu configuration: {error}"
    account_id = str(ticket.account_id or "default").strip().lower() or "default"
    account = accounts.get(account_id)
    if account is not None:
        return account, None
    available = ", ".join(sorted(accounts)) or "none"
    return (
        None,
        f'❌ Feishu account "{account_id}" was not found. '
        f"Configured accounts: {available}",
    )


async def _probe_account(account: _AccountConfig) -> Optional[dict[str, Any]]:
    """Probe Feishu bot connectivity without blocking the gateway loop."""
    from .adapter import probe_bot

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                probe_bot,
                account.app_id,
                account.app_secret,
                account.brand,
            ),
            timeout=20,
        )
    except Exception:
        return None


async def _handle_start(_raw_args: str) -> str:
    """Validate the current account without claiming an unverified startup."""
    ticket, error = _command_ticket_or_error()
    if error is not None or ticket is None:
        return str(error)
    account, error = _select_account(ticket)
    if error is not None or account is None:
        return str(error)
    if not account.configured:
        return (
            "❌ The Feishu Hermes Plugin configuration is incomplete: "
            f"account {account.account_id} is missing appId or appSecret."
        )
    if not account.enabled:
        return f"❌ Feishu account {account.account_id} is disabled."
    if not openclaw_tools.interaction_host_available(
        account.account_id,
        profile_scope=ticket.profile_scope or ticket.profile,
    ):
        return (
            f"⚠️ Feishu Hermes Plugin v{_plugin_version()} is loaded, but "
            f"the live adapter for account {account.account_id} is not connected."
        )
    return (
        f"✅ Feishu Hermes Plugin v{_plugin_version()} is running "
        f"(account: {account.account_id})"
    )


def _auth_result_text(raw_result: str) -> str:
    """Translate the lifecycle result without fabricating OAuth completion."""
    try:
        result = json.loads(raw_result)
    except (TypeError, json.JSONDecodeError):
        return (
            "❌ Authorization failed to start: the interaction host returned "
            "an invalid result."
        )
    if not isinstance(result, Mapping):
        return (
            "❌ Authorization failed to start: the interaction host returned "
            "an invalid result."
        )
    if (
        result.get("status") == "pending"
        and result.get("error") == "authorization_pending"
    ):
        return (
            "⏳ Bulk authorization has started but is not complete. "
            "Follow the authorization card in Feishu to continue."
        )
    raw_error = result.get("error")
    if isinstance(raw_error, Mapping):
        code = str(raw_error.get("code") or "authorization_failed")
        message = str(raw_error.get("message") or code)
    else:
        code = str(raw_error or "authorization_failed")
        message = str(result.get("message") or code)
    return f"❌ Authorization did not start ({code}): {message}"


async def _handle_auth(_raw_args: str) -> str:
    """Start the live ``oauth_batch_auth`` continuation for this message."""
    ticket, error = _command_ticket_or_error()
    if error is not None or ticket is None:
        return str(error)
    try:
        result = openclaw_tools.invoke_openclaw_tool(
            "feishu_oauth_batch_auth",
            {},
            ticket=ticket,
        )
    except Exception as invoke_error:
        return f"❌ Authorization failed to start: {invoke_error}"
    return _auth_result_text(result)


async def _handle_diagnose(_raw_args: str) -> str:
    """Run real configuration, live-host, and API probes for every account."""
    ticket, error = _command_ticket_or_error()
    if error is not None or ticket is None:
        return str(error)
    try:
        accounts = _load_accounts()
    except Exception as config_error:
        return f"Diagnostics failed: {config_error}"

    lines = [
        "====================================",
        "  Feishu Plugin Diagnostic Report",
        f"  {datetime.now(timezone.utc).isoformat()}",
        "====================================",
        "",
        "[Environment]",
        f"  Python:      {sys.version.split()[0]}",
        f"  Plugin:      {_plugin_version()}",
        f"  System:      {runtime_platform.system()} {runtime_platform.machine()}",
        "",
        "[Global checks]",
    ]
    configured_accounts = [
        account for account in accounts.values() if account.configured
    ]
    enabled_accounts = [
        account
        for account in configured_accounts
        if account.enabled
    ]
    statuses: list[str] = []
    if enabled_accounts:
        lines.append(
            f"  [PASS] Feishu accounts: {len(enabled_accounts)} enabled and configured"
        )
        statuses.append("pass")
    else:
        lines.append(
            "  [FAIL] Feishu accounts: no enabled account has complete credentials"
        )
        statuses.append("fail")
    try:
        tool_count = len(openclaw_tools.load_tool_manifest()["tools"])
    except Exception as manifest_error:
        lines.append(f"  [FAIL] Tool manifest: {manifest_error}")
        statuses.append("fail")
    else:
        lines.append(
            f"  [PASS] Tool manifest: loaded {tool_count} upstream tool definitions"
        )
        statuses.append("pass")
    lines.append("")

    for account in accounts.values():
        lines.append(f"[Account: {account.account_id}]")
        if account.name:
            lines.append(f"  Name:     {account.name}")
        lines.append(f"  App ID:   {account.app_id or '(not set)'}")
        lines.append(f"  Brand:    {account.brand}")
        if account.configured:
            lines.append("  [PASS] Credentials: appId and appSecret are set")
            statuses.append("pass")
        else:
            lines.append("  [FAIL] Credentials: appId or appSecret is missing")
            statuses.append("fail")
        if account.enabled:
            lines.append("  [PASS] Account status: enabled")
            statuses.append("pass")
        else:
            lines.append("  [WARN] Account status: disabled")
            statuses.append("warn")
        if openclaw_tools.interaction_host_available(
            account.account_id,
            profile_scope=ticket.profile_scope or ticket.profile,
        ):
            lines.append("  [PASS] Live adapter: connected")
            statuses.append("pass")
        else:
            lines.append("  [WARN] Live adapter: not connected")
            statuses.append("warn")
        if not account.configured or not account.enabled:
            lines.append(
                "  [SKIP] API connectivity: account is disabled or "
                "credentials are incomplete"
            )
        else:
            probe = await _probe_account(account)
            if probe is None:
                lines.append(
                    "  [FAIL] API connectivity: probe failed "
                    "(check credentials, network access, and bot configuration)"
                )
                statuses.append("fail")
            else:
                bot_name = str(probe.get("bot_name") or "name unavailable")
                bot_open_id = str(probe.get("bot_open_id") or "ID unavailable")
                lines.append(
                    f"  [PASS] API connectivity: {bot_name} ({bot_open_id})"
                )
                statuses.append("pass")
        lines.append("")

    if not accounts:
        lines.append("[Accounts]")
        lines.append("  [FAIL] No Feishu account configuration was found")
        lines.append("")
        statuses.append("fail")
    overall = (
        "UNHEALTHY (one or more checks failed)"
        if "fail" in statuses
        else "DEGRADED (one or more checks have warnings)"
        if "warn" in statuses
        else "HEALTHY"
    )
    lines.extend(
        [
            "====================================",
            f"  Overall status: {overall}",
            "====================================",
        ]
    )
    return "\n".join(lines)


async def _inspect_application(
    account: _AccountConfig,
) -> Optional[tuple[Any, frozenset[str]]]:
    """Read current app owner and scope state through the official SDK."""
    from .adapter import (
        FeishuAdapter,
        _build_onboard_client,
    )

    def inspect() -> tuple[Any, frozenset[str]]:
        client = _build_onboard_client(
            account.app_id,
            account.app_secret,
            account.brand,
        )
        request = FeishuAdapter._build_get_application_request(
            app_id=account.app_id,
            lang="zh_cn",
        )
        response = client.application.v6.application.get(request)
        return FeishuAdapter._parse_openclaw_application_response(response)

    try:
        return await asyncio.wait_for(asyncio.to_thread(inspect), timeout=20)
    except Exception:
        return None


async def _oauth_status(
    account: _AccountConfig,
    sender_open_id: str,
) -> tuple[str, str]:
    """Read the persisted OAuth record without exposing credential contents."""
    from .oauth_runtime import NodeTokenStore, token_status

    try:
        token = await NodeTokenStore().get(account.app_id, sender_open_id)
    except Exception as error:
        return "warn", f"Credential store check failed: {error}"
    if token is None:
        return (
            "warn",
            "The current user has not authorized access; "
            "send /feishu auth to begin",
        )
    status = token_status(token)
    scope_count = len([scope for scope in token.scope.split() if scope])
    if status == "valid":
        return "pass", f"Valid with {scope_count} recorded scopes"
    if status == "needs_refresh":
        return "warn", f"Refresh required with {scope_count} recorded scopes"
    return "fail", "Authorization has expired; send /feishu auth to authorize again"


async def _handle_doctor(_raw_args: str) -> str:
    """Diagnose the current account and initiating user's OAuth state."""
    ticket, error = _command_ticket_or_error()
    if error is not None or ticket is None:
        return str(error)
    account, error = _select_account(ticket)
    if error is not None or account is None:
        return str(error)
    if not account.configured:
        return (
            "### Feishu Plugin Diagnostics\n\n"
            f"❌ Account {account.account_id} is missing appId or appSecret."
        )

    lines = [
        "### Feishu Plugin Diagnostics",
        "",
        f"Plugin version: {_plugin_version()}  |  "
        f"Diagnostic time: {datetime.now(timezone.utc).isoformat()}",
        "",
        "#### Environment and Connection",
        "",
        f"- [PASS] Credentials: appId `{account.app_id}` and appSecret are set",
        (
            "- [PASS] Account status: enabled"
            if account.enabled
            else "- [FAIL] Account status: disabled"
        ),
    ]
    statuses = ["pass", "pass" if account.enabled else "fail"]
    if openclaw_tools.interaction_host_available(
        account.account_id,
        profile_scope=ticket.profile_scope or ticket.profile,
    ):
        lines.append("- [PASS] Live adapter: connected")
        statuses.append("pass")
    else:
        lines.append("- [WARN] Live adapter: not connected")
        statuses.append("warn")

    probe = await _probe_account(account) if account.enabled else None
    if probe is None:
        lines.append("- [FAIL] API connectivity: probe failed or account is disabled")
        statuses.append("fail")
    else:
        lines.append(
            "- [PASS] API connectivity: "
            f"{probe.get('bot_name') or 'bot is online'}"
        )
        statuses.append("pass")

    lines.extend(["", "#### Application Permissions", ""])
    application_result = (
        await _inspect_application(account)
        if account.enabled and probe is not None
        else None
    )
    if application_result is None:
        lines.append(
            "- [WARN] Unable to read application permission status; "
            "check application:application:self_manage and network access"
        )
        statuses.append("warn")
    else:
        application, all_scopes = application_result
        user_scopes = tuple(getattr(application, "user_scopes", ()) or ())
        owner = str(
            getattr(application, "effective_owner_open_id", "") or ""
        )
        lines.append(
            f"- [PASS] Application permissions: {len(all_scopes)} total, "
            f"including {len(user_scopes)} user scopes"
        )
        statuses.append("pass")
        if owner and owner == ticket.sender_open_id:
            lines.append("- [PASS] Application owner: current user verified")
            statuses.append("pass")
        elif owner:
            lines.append("- [FAIL] Application owner: current user is not the owner")
            statuses.append("fail")
        else:
            lines.append("- [WARN] Application owner: unable to verify")
            statuses.append("warn")

    lines.extend(["", "#### User Authorization", ""])
    oauth_level, oauth_message = await _oauth_status(
        account,
        ticket.sender_open_id,
    )
    lines.append(f"- [{oauth_level.upper()}] {oauth_message}")
    statuses.append(oauth_level)

    overall = (
        "UNHEALTHY"
        if "fail" in statuses
        else "DEGRADED"
        if "warn" in statuses
        else "HEALTHY"
    )
    lines.extend(["", f"Overall status: **{overall}**"])
    return "\n".join(lines)


async def _handle_feishu(raw_args: str) -> str:
    """Route the unified command with OpenClaw-compatible subcommands."""
    subcommand = (raw_args.strip().split() or ["help"])[0].lower()
    if subcommand in {"auth", "onboarding"}:
        return await _handle_auth("")
    if subcommand == "doctor":
        return await _handle_doctor("")
    if subcommand == "start":
        return await _handle_start("")
    return _help_text()


def register(ctx: Any) -> None:
    """Register Hermes' hyphen keys for the upstream underscore commands."""
    ctx.register_command(
        "feishu-diagnose",
        _handle_diagnose,
        description=(
            "Run Feishu plugin diagnostics to check configuration, "
            "connectivity, and permissions"
        ),
    )
    ctx.register_command(
        "feishu-doctor",
        _handle_doctor,
        description="Run Feishu plugin diagnostics for the current account",
    )
    ctx.register_command(
        "feishu-auth",
        _handle_auth,
        description="Batch authorize user permissions for Feishu",
    )
    ctx.register_command(
        "feishu",
        _handle_feishu,
        description=(
            "Feishu plugin commands "
            "(subcommands: auth, doctor, start, help)"
        ),
        args_hint="[auth|doctor|start|help]",
    )


__all__ = [
    "bind_gateway_command_ticket",
    "current_gateway_command_ticket",
    "register",
]
