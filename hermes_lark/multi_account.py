"""Multi-account routing for the Hermes Feishu platform plugin."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    ProcessingOutcome,
    SendResult,
)

from .adapter import FeishuAdapter


class MultiAccountFeishuAdapter(BasePlatformAdapter):
    """Run one isolated Feishu connection per configured account."""

    splits_long_messages = True
    REQUIRES_EDIT_FINALIZE = True
    MAX_MESSAGE_LENGTH = 4000

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.FEISHU)
        self._children = self._build_children(config)
        self._default_account_id = next(iter(self._children), "")

    @staticmethod
    def _build_children(config: PlatformConfig) -> Dict[str, FeishuAdapter]:
        extra = dict(config.extra or {})
        account_map = extra.pop("accounts", {})
        accounts = account_map if isinstance(account_map, dict) else {}
        accounts_only = bool(extra.pop("_accounts_only", False))
        base = {
            key: value
            for key, value in extra.items()
            if not key.startswith("_account")
        }
        if accounts_only:
            for credential_key in ("appId", "appSecret", "app_id", "app_secret"):
                base.pop(credential_key, None)
        definitions: Dict[str, Dict[str, Any]] = {}
        has_base_credentials = bool(
            (base.get("appId") or base.get("app_id"))
            and (base.get("appSecret") or base.get("app_secret"))
        )
        if has_base_credentials and not accounts_only:
            definitions["default"] = dict(base)
        for raw_account_id, raw_override in accounts.items():
            if not isinstance(raw_override, dict):
                continue
            account_id = str(raw_account_id).strip().lower()
            if not account_id:
                continue
            if account_id == "default" and "default" in definitions:
                continue
            merged = dict(base)
            for key, value in raw_override.items():
                if value is None:
                    continue
                current = merged.get(key)
                if (
                    isinstance(value, dict)
                    and isinstance(current, dict)
                ):
                    merged[key] = {**current, **value}
                else:
                    merged[key] = value
            definitions[account_id] = merged

        children: Dict[str, FeishuAdapter] = {}
        for account_id, account_extra in definitions.items():
            app_id = account_extra.get("appId") or account_extra.get("app_id")
            app_secret = account_extra.get("appSecret") or account_extra.get("app_secret")
            enabled = account_extra.get("enabled", bool(app_id and app_secret))
            if not enabled or not app_id or not app_secret:
                continue
            account_extra["_account_id"] = account_id
            account_extra["_namespace_account"] = True
            child_config = dataclasses.replace(
                config,
                enabled=True,
                extra=account_extra,
            )
            children[account_id] = FeishuAdapter(child_config)
        return children

    def set_message_handler(self, handler: Any) -> None:
        """Install the gateway handler on every account connection."""
        super().set_message_handler(handler)
        for child in self._children.values():
            child.set_message_handler(handler)

    def set_topic_recovery_fn(self, fn: Any) -> None:
        """Keep account adapters aligned with the gateway recovery hook."""
        super().set_topic_recovery_fn(fn)
        for child in self._children.values():
            child.set_topic_recovery_fn(fn)

    def set_fatal_error_handler(self, handler: Any) -> None:
        """Install the gateway fatal-error handler on every account."""
        super().set_fatal_error_handler(handler)
        for child in self._children.values():
            child.set_fatal_error_handler(handler)

    def set_session_store(self, session_store: Any) -> None:
        """Share the gateway session store with every account."""
        super().set_session_store(session_store)
        for child in self._children.values():
            child.set_session_store(session_store)

    def set_busy_session_handler(self, handler: Any) -> None:
        """Install the busy-session handler on every account."""
        super().set_busy_session_handler(handler)
        for child in self._children.values():
            child.set_busy_session_handler(handler)

    def set_reaction_handler(self, handler: Any) -> None:
        """Install the reaction handler on every account."""
        super().set_reaction_handler(handler)
        for child in self._children.values():
            child.set_reaction_handler(handler)

    def set_authorization_check(self, callback: Any) -> None:
        """Install the authorization check on every account."""
        super().set_authorization_check(callback)
        for child in self._children.values():
            child.set_authorization_check(callback)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect all accounts, or tear down the partial result."""
        if not self._children:
            self._set_fatal_error(
                "feishu_accounts",
                "No enabled Feishu account has both appId and appSecret.",
                retryable=False,
            )
            return False
        gateway_runner = getattr(self, "gateway_runner", None)
        for child in self._children.values():
            if gateway_runner is not None:
                child.gateway_runner = gateway_runner
        results = await asyncio.gather(
            *(
                child.connect(is_reconnect=is_reconnect)
                for child in self._children.values()
            ),
            return_exceptions=True,
        )
        connected = bool(results) and all(result is True for result in results)
        if connected:
            self._mark_connected()
            return True
        await asyncio.gather(
            *(child.disconnect() for child in self._children.values()),
            return_exceptions=True,
        )
        return False

    async def disconnect(self) -> None:
        """Disconnect every account connection."""
        await asyncio.gather(
            *(child.disconnect() for child in self._children.values()),
            return_exceptions=True,
        )
        self._mark_disconnected()

    def _route(
        self,
        chat_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> tuple[Optional[FeishuAdapter], str]:
        target = str(chat_id or "")
        account_hint = str(
            (metadata or {}).get("account_id")
            or (metadata or {}).get("accountId")
            or ""
        ).strip().lower()
        raw_chat_id = target
        if "::" in target:
            prefix, candidate = target.split("::", 1)
            normalized_prefix = prefix.strip().lower()
            if normalized_prefix not in self._children:
                return None, target
            account_hint = normalized_prefix
            raw_chat_id = candidate
        account_id = account_hint or self._default_account_id
        return self._children.get(account_id), raw_chat_id

    def _route_event(self, event: MessageEvent) -> Optional[FeishuAdapter]:
        source = getattr(event, "source", None)
        child, _ = self._route(
            getattr(source, "chat_id", "") if source else "",
            {"account_id": getattr(source, "scope_id", "")} if source else None,
        )
        return child

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Route a text send to the account encoded in the Hermes chat ID."""
        child, raw_chat_id = self._route(chat_id, metadata)
        if child is None:
            return SendResult(success=False, error="Unknown Feishu account")
        return await child.send(raw_chat_id, content, reply_to=reply_to, metadata=metadata)

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Route an edit to the originating account."""
        child, raw_chat_id = self._route(chat_id, metadata)
        if child is None:
            return SendResult(success=False, error="Unknown Feishu account")
        return await child.edit_message(
            raw_chat_id,
            message_id,
            content,
            finalize=finalize,
            metadata=metadata,
        )

    async def send_exec_approval(self, chat_id: str, *args: Any, **kwargs: Any) -> SendResult:
        """Route an execution-approval card to the originating account."""
        child, raw_chat_id = self._route(chat_id, kwargs.get("metadata"))
        if child is None:
            return SendResult(success=False, error="Unknown Feishu account")
        return await child.send_exec_approval(raw_chat_id, *args, **kwargs)

    async def send_update_prompt(self, chat_id: str, *args: Any, **kwargs: Any) -> SendResult:
        """Route an update prompt to the originating account."""
        child, raw_chat_id = self._route(chat_id, kwargs.get("metadata"))
        if child is None:
            return SendResult(success=False, error="Unknown Feishu account")
        return await child.send_update_prompt(raw_chat_id, *args, **kwargs)

    async def send_voice(self, chat_id: str, *args: Any, **kwargs: Any) -> SendResult:
        """Route voice delivery to the originating account."""
        child, raw_chat_id = self._route(chat_id, kwargs.get("metadata"))
        if child is None:
            return SendResult(success=False, error="Unknown Feishu account")
        return await child.send_voice(raw_chat_id, *args, **kwargs)

    async def send_document(self, chat_id: str, *args: Any, **kwargs: Any) -> SendResult:
        """Route document delivery to the originating account."""
        child, raw_chat_id = self._route(chat_id, kwargs.get("metadata"))
        if child is None:
            return SendResult(success=False, error="Unknown Feishu account")
        return await child.send_document(raw_chat_id, *args, **kwargs)

    async def send_video(self, chat_id: str, *args: Any, **kwargs: Any) -> SendResult:
        """Route video delivery to the originating account."""
        child, raw_chat_id = self._route(chat_id, kwargs.get("metadata"))
        if child is None:
            return SendResult(success=False, error="Unknown Feishu account")
        return await child.send_video(raw_chat_id, *args, **kwargs)

    async def send_image_file(self, chat_id: str, *args: Any, **kwargs: Any) -> SendResult:
        """Route a local image to the originating account."""
        child, raw_chat_id = self._route(chat_id, kwargs.get("metadata"))
        if child is None:
            return SendResult(success=False, error="Unknown Feishu account")
        return await child.send_image_file(raw_chat_id, *args, **kwargs)

    async def send_image(self, chat_id: str, *args: Any, **kwargs: Any) -> SendResult:
        """Route a remote image to the originating account."""
        child, raw_chat_id = self._route(chat_id, kwargs.get("metadata"))
        if child is None:
            return SendResult(success=False, error="Unknown Feishu account")
        return await child.send_image(raw_chat_id, *args, **kwargs)

    async def send_animation(self, chat_id: str, *args: Any, **kwargs: Any) -> SendResult:
        """Route an animation to the originating account."""
        child, raw_chat_id = self._route(chat_id, kwargs.get("metadata"))
        if child is None:
            return SendResult(success=False, error="Unknown Feishu account")
        return await child.send_animation(raw_chat_id, *args, **kwargs)

    async def send_typing(
        self,
        chat_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Route typing state to the originating account."""
        child, raw_chat_id = self._route(chat_id, metadata)
        if child is not None:
            await child.send_typing(raw_chat_id, metadata=metadata)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Read chat metadata through the account encoded in the target."""
        child, raw_chat_id = self._route(chat_id)
        if child is None:
            return {"chat_id": chat_id, "name": chat_id, "type": "dm"}
        return await child.get_chat_info(raw_chat_id)

    async def on_processing_start(self, event: MessageEvent) -> None:
        """Apply processing state through the event's account."""
        child = self._route_event(event)
        if child is not None:
            await child.on_processing_start(event)

    async def on_processing_complete(
        self,
        event: MessageEvent,
        outcome: ProcessingOutcome,
    ) -> None:
        """Complete processing state through the event's account."""
        child = self._route_event(event)
        if child is not None:
            await child.on_processing_complete(event, outcome)

    def format_message(self, content: str) -> str:
        """Use the native Feishu formatter for shared gateway paths."""
        child = self._children.get(self._default_account_id)
        return child.format_message(content) if child else content.strip()

    def max_message_length_for_chat(self, chat_id: str) -> int:
        """Return the selected account's configured outbound chunk limit."""
        child, _ = self._route(chat_id)
        return (
            int(getattr(child, "_text_chunk_limit", self.MAX_MESSAGE_LENGTH))
            if child is not None
            else self.MAX_MESSAGE_LENGTH
        )
