"""Lifecycle propagation tests for the multi-account Feishu adapter."""

from __future__ import annotations

import enum
import importlib.util
import sys
import types
import unittest
from asyncio import run
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "hermes_lark"
_MISSING_MODULE = object()
_MANAGED_MODULES = (
    "gateway",
    "gateway.config",
    "gateway.platforms",
    "gateway.platforms.base",
    "hermes_lark",
    "hermes_lark.adapter",
    "hermes_lark.multi_account",
)


def _load_multi_account_module() -> tuple[types.ModuleType, type, dict[str, object]]:
    """Load the multi-account module against a focused Hermes test double."""
    previous = {
        name: sys.modules.get(name, _MISSING_MODULE) for name in _MANAGED_MODULES
    }

    class Platform(enum.Enum):
        FEISHU = "feishu"

    @dataclass
    class PlatformConfig:
        enabled: bool = False
        extra: dict[str, Any] = field(default_factory=dict)

    class BasePlatformAdapter:
        def __init__(self, config: PlatformConfig, platform: Platform) -> None:
            self.config = config
            self.platform = platform
            self._fatal_error_handler = None
            self._session_store = None
            self._busy_session_handler = None
            self._reaction_handler = None
            self._authorization_check = None

        def set_message_handler(self, handler: Any) -> None:
            self._message_handler = handler

        def set_topic_recovery_fn(self, fn: Any) -> None:
            self._topic_recovery_fn = fn

        def set_fatal_error_handler(self, handler: Any) -> None:
            self._fatal_error_handler = handler

        def set_session_store(self, session_store: Any) -> None:
            self._session_store = session_store

        def set_busy_session_handler(self, handler: Any) -> None:
            self._busy_session_handler = handler

        def set_reaction_handler(self, handler: Any) -> None:
            self._reaction_handler = handler

        def set_authorization_check(self, callback: Any) -> None:
            self._authorization_check = callback

    class FeishuAdapter(BasePlatformAdapter):
        def __init__(self, config: PlatformConfig) -> None:
            super().__init__(config, Platform.FEISHU)

    gateway = types.ModuleType("gateway")
    gateway.__path__ = []
    config = types.ModuleType("gateway.config")
    config.Platform = Platform
    config.PlatformConfig = PlatformConfig
    platforms = types.ModuleType("gateway.platforms")
    platforms.__path__ = []
    base = types.ModuleType("gateway.platforms.base")
    base.BasePlatformAdapter = BasePlatformAdapter
    base.MessageEvent = object
    base.ProcessingOutcome = object
    base.SendResult = object
    package = types.ModuleType("hermes_lark")
    package.__path__ = [str(PACKAGE_DIR)]
    adapter = types.ModuleType("hermes_lark.adapter")
    adapter.FeishuAdapter = FeishuAdapter
    sys.modules.update(
        {
            "gateway": gateway,
            "gateway.config": config,
            "gateway.platforms": platforms,
            "gateway.platforms.base": base,
            "hermes_lark": package,
            "hermes_lark.adapter": adapter,
        }
    )

    name = "hermes_lark.multi_account"
    spec = importlib.util.spec_from_file_location(
        name,
        PACKAGE_DIR / "multi_account.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module, PlatformConfig, previous


class MultiAccountLifecycleTests(unittest.TestCase):
    """Verify gateway lifecycle dependencies reach every account adapter."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module, cls.PlatformConfig, cls.previous_modules = (
            _load_multi_account_module()
        )

    @classmethod
    def tearDownClass(cls) -> None:
        for name, previous in cls.previous_modules.items():
            if previous is _MISSING_MODULE:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    def _new_adapter(self) -> Any:
        config = self.PlatformConfig(
            enabled=True,
            extra={
                "_accounts_only": True,
                "accounts": {
                    "work": {"appId": "cli_work", "appSecret": "secret_work"},
                    "personal": {
                        "appId": "cli_personal",
                        "appSecret": "secret_personal",
                    },
                },
            },
        )
        return self.module.MultiAccountFeishuAdapter(config)

    def test_gateway_lifecycle_setters_update_parent_and_children(self) -> None:
        """All gateway-owned handlers and stores remain identical on children."""
        adapter = self._new_adapter()
        fatal_handler = object()
        session_store = object()
        busy_handler = object()
        reaction_handler = object()
        authorization_check = object()

        adapter.set_fatal_error_handler(fatal_handler)
        adapter.set_session_store(session_store)
        adapter.set_busy_session_handler(busy_handler)
        adapter.set_reaction_handler(reaction_handler)
        adapter.set_authorization_check(authorization_check)

        expected = {
            "_fatal_error_handler": fatal_handler,
            "_session_store": session_store,
            "_busy_session_handler": busy_handler,
            "_reaction_handler": reaction_handler,
            "_authorization_check": authorization_check,
        }
        for attribute, value in expected.items():
            self.assertIs(getattr(adapter, attribute), value)
            for child in adapter._children.values():
                self.assertIs(getattr(child, attribute), value)

    def test_optional_lifecycle_handlers_can_be_cleared(self) -> None:
        """Clearing optional handlers also clears every child adapter."""
        adapter = self._new_adapter()
        adapter.set_busy_session_handler(object())
        adapter.set_reaction_handler(object())
        adapter.set_authorization_check(object())

        adapter.set_busy_session_handler(None)
        adapter.set_reaction_handler(None)
        adapter.set_authorization_check(None)

        for attribute in (
            "_busy_session_handler",
            "_reaction_handler",
            "_authorization_check",
        ):
            self.assertIsNone(getattr(adapter, attribute))
            for child in adapter._children.values():
                self.assertIsNone(getattr(child, attribute))

    def test_unknown_explicit_account_namespace_fails_closed(self) -> None:
        """A typo cannot silently route through the default Feishu account."""
        adapter = self._new_adapter()

        child, chat_id = adapter._route("unknown::oc_chat")

        self.assertIsNone(child)
        self.assertEqual(chat_id, "unknown::oc_chat")

    def test_partial_connection_failure_disconnects_every_account(self) -> None:
        """One failed child cannot leave a silently partial multi-account gateway."""
        adapter = self._new_adapter()
        children = list(adapter._children.values())
        disconnected: list[Any] = []
        for index, child in enumerate(children):
            async def connect(
                *,
                is_reconnect: bool = False,
                succeeds: bool = index == 0,
            ) -> bool:
                return succeeds

            async def disconnect(current: Any = child) -> None:
                disconnected.append(current)

            child.connect = connect
            child.disconnect = disconnect

        self.assertFalse(run(adapter.connect()))
        self.assertCountEqual(disconnected, children)

    def test_message_limit_routes_to_selected_account(self) -> None:
        """Gateway pre-chunking uses the same per-account limit as delivery."""
        adapter = self._new_adapter()
        adapter._children["work"]._text_chunk_limit = 1200
        adapter._children["personal"]._text_chunk_limit = 2800

        self.assertEqual(adapter.max_message_length_for_chat("work::oc_chat"), 1200)
        self.assertEqual(
            adapter.max_message_length_for_chat("personal::oc_chat"),
            2800,
        )
        self.assertEqual(adapter.max_message_length_for_chat("unknown::oc_chat"), 4000)

    def test_stream_finalize_and_metadata_reach_the_selected_child(self) -> None:
        """CardKit finalization keeps account and native-thread routing intact."""
        adapter = self._new_adapter()
        captured: list[tuple[Any, ...]] = []

        async def edit_message(*args: Any, **kwargs: Any) -> Any:
            captured.append((*args, kwargs))
            return "edited"

        adapter._children["work"].edit_message = edit_message
        metadata = {"account_id": "work", "thread_id": "om_root"}

        result = run(
            adapter.edit_message(
                "work::oc_chat",
                "om_card",
                "complete",
                finalize=True,
                metadata=metadata,
            )
        )

        self.assertEqual(result, "edited")
        self.assertEqual(
            captured,
            [
                (
                    "oc_chat",
                    "om_card",
                    "complete",
                    {"finalize": True, "metadata": metadata},
                )
            ],
        )
        self.assertIs(adapter.REQUIRES_EDIT_FINALIZE, True)


if __name__ == "__main__":
    unittest.main()
