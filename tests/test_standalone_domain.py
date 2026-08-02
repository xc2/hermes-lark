"""Focused parity tests for standalone accounts and SDK domain routing."""

from __future__ import annotations

import asyncio
import dataclasses
import sys
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from tests.test_ask_user_question_adapter import (
    _MISSING_MODULE,
    _load_modules,
)


@dataclasses.dataclass
class _PlatformConfig:
    """Hermes-compatible platform config used by multi-account construction."""

    enabled: bool = True
    extra: dict[str, Any] = dataclasses.field(default_factory=dict)


class StandaloneAccountTests(unittest.IsolatedAsyncioTestCase):
    """Verify standalone delivery selects and initializes one account child."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.previous_multi = sys.modules.pop(
            "hermes_lark.multi_account",
            _MISSING_MODULE,
        )
        _, cls.module, cls.previous_modules = _load_modules()
        sys.modules["gateway.config"].PlatformConfig = _PlatformConfig
        cls.module.PlatformConfig = _PlatformConfig
        cls.module.FEISHU_AVAILABLE = True

    @classmethod
    def tearDownClass(cls) -> None:
        sys.modules.pop("hermes_lark.multi_account", None)
        if cls.previous_multi is not _MISSING_MODULE:
            sys.modules["hermes_lark.multi_account"] = cls.previous_multi
        for name, previous in cls.previous_modules.items():
            if previous is _MISSING_MODULE:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    async def test_namespaced_text_and_media_use_selected_child_raw_chat_id(
        self,
    ) -> None:
        config = _PlatformConfig(
            extra={
                "accounts": {
                    "cn": {
                        "enabled": True,
                        "appId": "cli_cn",
                        "appSecret": "secret_cn",
                        "domain": "feishu",
                    },
                    "global": {
                        "enabled": True,
                        "appId": "cli_global",
                        "appSecret": "secret_global",
                        "domain": "https://Open.Example.com/lark/",
                    },
                }
            }
        )
        clients: list[tuple[str, str, Any]] = []
        deliveries: list[tuple[str, str, str]] = []

        def build_client(adapter: Any, domain: Any) -> Any:
            clients.append((adapter._account_id, adapter._app_id, domain))
            return object()

        async def send(
            adapter: Any,
            chat_id: str,
            content: str,
            **kwargs: Any,
        ) -> Any:
            deliveries.append(("text", adapter._account_id, chat_id))
            return self.module.SendResult(success=True, message_id="om_text")

        async def send_image(
            adapter: Any,
            chat_id: str,
            file_path: str,
            **kwargs: Any,
        ) -> Any:
            deliveries.append(("image", adapter._account_id, chat_id))
            return self.module.SendResult(success=True, message_id="om_image")

        with (
            patch.object(
                self.module.FeishuAdapter,
                "_build_lark_client",
                build_client,
            ),
            patch.object(self.module.FeishuAdapter, "send", send),
            patch.object(
                self.module.FeishuAdapter,
                "send_image_file",
                send_image,
            ),
            patch.object(self.module.os.path, "exists", return_value=True),
        ):
            result = await self.module._standalone_send(
                config,
                "global::oc_chat",
                "hello",
                media_files=[("/tmp/image.png", False)],
            )

        self.assertTrue(result["success"])
        self.assertEqual(
            clients,
            [
                (
                    "global",
                    "cli_global",
                    "https://Open.Example.com/lark",
                )
            ],
        )
        self.assertEqual(
            deliveries,
            [
                ("text", "global", "oc_chat"),
                ("image", "global", "oc_chat"),
            ],
        )
        self.assertEqual(result["chat_id"], "global::oc_chat")

    async def test_raw_chat_defaults_to_first_enabled_account(self) -> None:
        config = _PlatformConfig(
            extra={
                "accounts": {
                    "disabled": {
                        "enabled": False,
                        "appId": "cli_disabled",
                        "appSecret": "secret_disabled",
                    },
                    "first": {
                        "enabled": True,
                        "appId": "cli_first",
                        "appSecret": "secret_first",
                    },
                    "second": {
                        "enabled": True,
                        "appId": "cli_second",
                        "appSecret": "secret_second",
                    },
                }
            }
        )
        selected: list[tuple[str, str]] = []

        def build_client(adapter: Any, domain: Any) -> Any:
            selected.append((adapter._account_id, adapter._app_id))
            return object()

        async def send(
            adapter: Any,
            chat_id: str,
            content: str,
            **kwargs: Any,
        ) -> Any:
            selected.append((adapter._account_id, chat_id))
            return self.module.SendResult(success=True, message_id="om_text")

        with (
            patch.object(
                self.module.FeishuAdapter,
                "_build_lark_client",
                build_client,
            ),
            patch.object(self.module.FeishuAdapter, "send", send),
        ):
            result = await self.module._standalone_send(
                config,
                "oc_chat",
                "hello",
            )

        self.assertTrue(result["success"])
        self.assertEqual(
            selected,
            [
                ("first", "cli_first"),
                ("first", "oc_chat"),
            ],
        )

    async def test_unknown_explicit_namespace_fails_closed(self) -> None:
        config = _PlatformConfig(
            extra={
                "accounts": {
                    "work": {
                        "enabled": True,
                        "appId": "cli_work",
                        "appSecret": "secret_work",
                    }
                }
            }
        )

        with patch.object(
            self.module.FeishuAdapter,
            "_build_lark_client",
            side_effect=AssertionError("unknown account must not initialize"),
        ):
            result = await self.module._standalone_send(
                config,
                "typo::oc_chat",
                "hello",
            )

        self.assertEqual(result, {"error": "Unknown Feishu account"})

    async def test_namespace_is_rejected_for_single_account_config(self) -> None:
        config = _PlatformConfig(
            extra={
                "appId": "cli_single",
                "appSecret": "secret_single",
            }
        )

        with patch.object(
            self.module.FeishuAdapter,
            "_build_lark_client",
            side_effect=AssertionError("invalid target must not initialize"),
        ):
            result = await self.module._standalone_send(
                config,
                "unknown::oc_chat",
                "hello",
            )

        self.assertEqual(result, {"error": "Unknown Feishu account"})


class DomainRoutingTests(unittest.IsolatedAsyncioTestCase):
    """Verify aliases and custom HTTPS domains reach every SDK transport."""

    @classmethod
    def setUpClass(cls) -> None:
        _, cls.module, cls.previous_modules = _load_modules()

    @classmethod
    def tearDownClass(cls) -> None:
        for name, previous in cls.previous_modules.items():
            if previous is _MISSING_MODULE:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    def _adapter(self, domain: str) -> Any:
        return self.module.FeishuAdapter(
            self.module.PlatformConfig(
                extra={
                    "appId": "cli_app",
                    "appSecret": "secret",
                    "domain": domain,
                }
            )
        )

    async def test_settings_preserve_custom_url_and_aliases_resolve(self) -> None:
        custom = "https://Open.Example.com/base/"
        adapter = self._adapter(custom)
        with (
            patch.object(self.module, "FEISHU_DOMAIN", "FEISHU"),
            patch.object(self.module, "LARK_DOMAIN", "LARK"),
        ):
            self.assertEqual(
                self.module._resolve_feishu_sdk_domain("feishu"),
                "FEISHU",
            )
            self.assertEqual(
                self.module._resolve_feishu_sdk_domain("LARK"),
                "LARK",
            )
            self.assertEqual(
                self.module._resolve_feishu_sdk_domain(custom),
                "https://Open.Example.com/base",
            )
        self.assertEqual(
            adapter._domain_name,
            "https://Open.Example.com/base",
        )

    async def test_websocket_and_webhook_receive_custom_sdk_domain(self) -> None:
        custom = "https://Open.Example.com/base"
        websocket_adapter = self._adapter(f"{custom}/")
        webhook_adapter = self._adapter(f"{custom}/")
        client_domains: list[Any] = []
        websocket_domains: list[Any] = []

        def build_client(domain: Any) -> Any:
            client_domains.append(domain)
            return object()

        async def hydrate() -> None:
            return None

        class FakeWSClient:
            """Capture the official WebSocket client's configured endpoint."""

            def __init__(self, **kwargs: Any) -> None:
                websocket_domains.append(kwargs["domain"])

        class FakeRouter:
            """Accept one webhook route registration."""

            def add_post(self, *args: Any) -> None:
                return None

        class FakeApplication:
            """Minimal aiohttp application used by webhook setup."""

            def __init__(self, **kwargs: Any) -> None:
                self.router = FakeRouter()

        class FakeRunner:
            """Minimal aiohttp runner used by webhook setup."""

            def __init__(self, app: Any) -> None:
                self.app = app

            async def setup(self) -> None:
                return None

        class FakeSite:
            """Minimal aiohttp TCP site used by webhook setup."""

            def __init__(self, *args: Any) -> None:
                return None

            async def start(self) -> None:
                return None

        fake_web = SimpleNamespace(
            Application=FakeApplication,
            AppRunner=FakeRunner,
            TCPSite=FakeSite,
        )
        fake_lark = SimpleNamespace(
            LogLevel=SimpleNamespace(INFO="info", WARNING="warning")
        )
        for adapter in (websocket_adapter, webhook_adapter):
            adapter._build_lark_client = build_client
            adapter._build_event_handler = lambda: object()
            adapter._hydrate_bot_identity = hydrate
        websocket_adapter._loop = asyncio.get_running_loop()

        with (
            patch.object(self.module, "FEISHU_WEBSOCKET_AVAILABLE", True),
            patch.object(self.module, "FEISHU_WEBHOOK_AVAILABLE", True),
            patch.object(self.module, "FeishuWSClient", FakeWSClient),
            patch.object(self.module, "web", fake_web),
            patch.object(self.module, "lark", fake_lark),
            patch.object(
                self.module,
                "_run_official_feishu_ws_client",
                lambda *args: None,
            ),
        ):
            await websocket_adapter._connect_websocket()
            await websocket_adapter._ws_future
            await webhook_adapter._connect_webhook()

        self.assertEqual(client_domains, [custom, custom])
        self.assertEqual(websocket_domains, [custom])

    async def test_onboarding_sdk_and_http_probe_use_custom_open_domain(
        self,
    ) -> None:
        domains: list[Any] = []

        class FakeBuilder:
            """Record the domain passed to the onboarding SDK client."""

            def app_id(self, value: str) -> "FakeBuilder":
                return self

            def app_secret(self, value: str) -> "FakeBuilder":
                return self

            def domain(self, value: Any) -> "FakeBuilder":
                domains.append(value)
                return self

            def log_level(self, value: Any) -> "FakeBuilder":
                return self

            def build(self) -> object:
                return object()

        fake_lark = SimpleNamespace(
            Client=SimpleNamespace(builder=lambda: FakeBuilder()),
            LogLevel=SimpleNamespace(WARNING="warning"),
        )
        custom = "https://Open.Example.com/base/"
        with patch.object(self.module, "lark", fake_lark):
            self.module._build_onboard_client("cli_app", "secret", custom)

        self.assertEqual(domains, ["https://Open.Example.com/base"])
        self.assertEqual(
            self.module._onboard_open_base_url(custom),
            "https://Open.Example.com/base",
        )


if __name__ == "__main__":
    unittest.main()
