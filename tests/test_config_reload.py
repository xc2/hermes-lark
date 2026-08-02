"""Configuration isolation tests for Feishu plugin reloads and accounts."""

from __future__ import annotations

import dataclasses
import importlib
import os
import sys
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tests.test_ask_user_question_adapter import _MISSING_MODULE, _load_modules


@dataclasses.dataclass
class _PlatformConfig:
    """Hermes-compatible config used by the real multi-account constructor."""

    enabled: bool = True
    extra: dict[str, Any] = dataclasses.field(default_factory=dict)


class FeishuConfigReloadTests(unittest.TestCase):
    """Verify YAML remains config-local while operator env keeps precedence."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tools, cls.module, cls.previous_modules = _load_modules()
        cls.gateway_config = sys.modules["gateway.config"]
        cls.previous_platform_config = cls.gateway_config.PlatformConfig
        cls.gateway_config.PlatformConfig = _PlatformConfig
        cls.previous_multi = sys.modules.pop(
            "hermes_lark.multi_account",
            _MISSING_MODULE,
        )
        cls.multi_module = importlib.import_module("hermes_lark.multi_account")

    @classmethod
    def tearDownClass(cls) -> None:
        sys.modules.pop("hermes_lark.multi_account", None)
        if cls.previous_multi is not _MISSING_MODULE:
            sys.modules["hermes_lark.multi_account"] = cls.previous_multi
        cls.gateway_config.PlatformConfig = cls.previous_platform_config
        for name, previous in cls.previous_modules.items():
            if previous is _MISSING_MODULE:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    def tearDown(self) -> None:
        self.tools.configure_bridge_config(None)

    def test_adapter_captures_the_profile_scoped_hermes_home(self) -> None:
        """Two multiplex construction scopes produce distinct host identities."""
        config = _PlatformConfig(
            extra={
                "appId": "cli_profile",
                "appSecret": "secret_profile",
            }
        )
        homes = (
            Path("/hermes/profiles/coder"),
            Path("/hermes/profiles/reviewer"),
        )

        adapters = []
        for home in homes:
            with patch.object(self.module, "get_hermes_home", return_value=home):
                adapters.append(self.module.FeishuAdapter(config))

        self.assertEqual(
            [adapter._profile_scope_key for adapter in adapters],
            [str(home.resolve()) for home in homes],
        )

    def test_yaml_reload_does_not_write_feishu_process_env(self) -> None:
        first = {
            "appId": "cli_first",
            "appSecret": "secret_first",
            "domain": "lark",
            "connectionMode": "webhook",
            "encryptKey": "encrypt_first",
            "verificationToken": "verify_first",
            "webhookPath": "/first",
            "webhookPort": 8765,
        }
        second = {
            "appId": "cli_second",
            "appSecret": "secret_second",
            "domain": "feishu",
            "connectionMode": "websocket",
            "encryptKey": "encrypt_second",
            "verificationToken": "verify_second",
            "webhookPath": "/second",
            "webhookPort": 9876,
        }
        env_names = {
            "FEISHU_APP_ID",
            "FEISHU_APP_SECRET",
            "FEISHU_DOMAIN",
            "FEISHU_CONNECTION_MODE",
            "FEISHU_ENCRYPT_KEY",
            "FEISHU_VERIFICATION_TOKEN",
            "FEISHU_WEBHOOK_HOST",
            "FEISHU_WEBHOOK_PATH",
            "FEISHU_WEBHOOK_PORT",
        }

        with patch.dict(os.environ, {}, clear=True):
            first_result = self.module._apply_yaml_config({}, first)
            second_result = self.module._apply_yaml_config({}, second)

            self.assertTrue(env_names.isdisjoint(os.environ))

        self.assertEqual(first_result["app_id"], "cli_first")
        self.assertEqual(first_result["webhook_path"], "/first")
        self.assertEqual(second_result["app_id"], "cli_second")
        self.assertEqual(second_result["webhook_path"], "/second")
        self.assertEqual(
            self.tools._bridge_config_snapshot,
            {"channels": {"feishu": second_result}},
        )

    def test_deleted_yaml_block_invalidates_only_the_yaml_snapshot(self) -> None:
        live_config: dict[str, dict[str, Any]] = {
            "value": {
                "feishu": {
                    "enabled": True,
                }
            }
        }
        hermes_cli = types.ModuleType("hermes_cli")
        hermes_cli.__path__ = []
        hermes_config = types.ModuleType("hermes_cli.config")
        hermes_config.load_config_readonly = lambda: live_config["value"]
        yaml_config = {
            "appId": "cli_yaml",
            "appSecret": "secret_yaml",
            "tools": {"deny": ["terminal"]},
        }

        with (
            patch.dict(
                sys.modules,
                {
                    "hermes_cli": hermes_cli,
                    "hermes_cli.config": hermes_config,
                },
            ),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.module._apply_yaml_config({}, yaml_config)
            before = self.tools._bridge_config()["channels"]["feishu"]
            live_config["value"] = {"feishu": {"enabled": False}}
            disabled = self.tools._bridge_config()["channels"]["feishu"]
            live_config["value"] = {"feishu": {"enabled": True}}
            self.module._apply_yaml_config({}, yaml_config)
            live_config["value"] = {"plugins": {"enabled": ["platforms/feishu"]}}
            removed = self.tools._bridge_config()["channels"]["feishu"]
            os.environ.update(
                {
                    "FEISHU_APP_ID": "cli_env",
                    "FEISHU_APP_SECRET": "secret_env",
                    "FEISHU_DOMAIN": "lark",
                }
            )
            env_only = self.tools._bridge_config()["channels"]["feishu"]

        self.assertEqual(before["appId"], "cli_yaml")
        self.assertEqual(before["tools"]["deny"], ["terminal"])
        self.assertEqual(disabled["appId"], "")
        self.assertNotIn("tools", disabled)
        self.assertEqual(removed["appId"], "")
        self.assertNotIn("tools", removed)
        self.assertEqual(env_only["appId"], "cli_env")
        self.assertEqual(env_only["appSecret"], "secret_env")
        self.assertEqual(env_only["domain"], "lark")

    def test_pure_yaml_top_level_builds_complete_adapter_settings(self) -> None:
        yaml_config = {
            "extra": {
                "app_id": "cli_yaml",
                "app_secret": "secret_yaml",
                "domain": "https://open.example.com/lark/",
                "connection_mode": "webhook",
                "encrypt_key": "encrypt_yaml",
                "verification_token": "verify_yaml",
                "webhook_host": "127.0.0.8",
                "webhook_path": "/yaml-hook",
                "webhook_port": 8899,
            }
        }

        with patch.dict(os.environ, {}, clear=True):
            normalized = self.module._apply_yaml_config({}, yaml_config)
            adapter = self.module.FeishuAdapter(
                _PlatformConfig(extra=normalized)
            )
            bridge_feishu = self.tools._bridge_config()["channels"]["feishu"]

        self.assertTrue(self.module._is_connected(adapter.config))
        self.assertEqual(adapter._app_id, "cli_yaml")
        self.assertEqual(adapter._app_secret, "secret_yaml")
        self.assertEqual(
            adapter._domain_name,
            "https://open.example.com/lark",
        )
        self.assertEqual(adapter._connection_mode, "webhook")
        self.assertEqual(adapter._encrypt_key, "encrypt_yaml")
        self.assertEqual(adapter._verification_token, "verify_yaml")
        self.assertEqual(adapter._webhook_host, "127.0.0.8")
        self.assertEqual(adapter._webhook_path, "/yaml-hook")
        self.assertEqual(adapter._webhook_port, 8899)
        self.assertEqual(bridge_feishu["appId"], "cli_yaml")
        self.assertEqual(bridge_feishu["appSecret"], "secret_yaml")
        self.assertEqual(bridge_feishu["connectionMode"], "webhook")
        self.assertEqual(bridge_feishu["webhookPath"], "/yaml-hook")
        self.assertEqual(bridge_feishu["webhookPort"], 8899)

    def test_pure_yaml_accounts_build_only_account_local_children(self) -> None:
        yaml_config = {
            "domain": "feishu",
            "connectionMode": "webhook",
            "webhookPath": "/shared-hook",
            "webhookPort": 8765,
            "accounts": {
                "cn": {
                    "appId": "cli_cn",
                    "appSecret": "secret_cn",
                },
                "global": {
                    "appId": "cli_global",
                    "appSecret": "secret_global",
                    "domain": "lark",
                    "webhookPort": 9876,
                },
            },
        }

        with patch.dict(os.environ, {}, clear=True):
            normalized = self.module._apply_yaml_config({}, yaml_config)
            adapter = self.multi_module.MultiAccountFeishuAdapter(
                _PlatformConfig(extra=normalized)
            )

            self.assertNotIn("FEISHU_APP_ID", os.environ)
            self.assertNotIn("FEISHU_APP_SECRET", os.environ)

        self.assertTrue(self.module._is_connected(adapter.config))
        self.assertEqual(set(adapter._children), {"cn", "global"})
        self.assertEqual(adapter._children["cn"]._app_id, "cli_cn")
        self.assertEqual(adapter._children["cn"]._domain_name, "feishu")
        self.assertEqual(adapter._children["cn"]._webhook_port, 8765)
        self.assertEqual(
            adapter._children["global"]._app_id,
            "cli_global",
        )
        self.assertEqual(adapter._children["global"]._domain_name, "lark")
        self.assertEqual(adapter._children["global"]._webhook_port, 9876)

    def test_operator_env_overrides_single_adapter_but_not_account_children(
        self,
    ) -> None:
        env = {
            "FEISHU_APP_ID": "cli_env",
            "FEISHU_APP_SECRET": "secret_env",
            "FEISHU_DOMAIN": "lark",
            "FEISHU_CONNECTION_MODE": "webhook",
            "FEISHU_ENCRYPT_KEY": "encrypt_env",
            "FEISHU_VERIFICATION_TOKEN": "verify_env",
            "FEISHU_WEBHOOK_HOST": "127.0.0.9",
            "FEISHU_WEBHOOK_PATH": "/env-hook",
            "FEISHU_WEBHOOK_PORT": "9999",
            "FEISHU_ALLOWED_USERS": "ou_env",
            "FEISHU_GROUP_POLICY": "open",
            "FEISHU_REQUIRE_MENTION": "false",
            "FEISHU_ALLOW_BOTS": "all",
            "FEISHU_ALLOW_ALL_USERS": "true",
            "FEISHU_BOT_OPEN_ID": "ou_bot_env",
            "FEISHU_BOT_NAME": "Env Bot",
        }
        yaml_config = {
            "appId": "cli_yaml",
            "appSecret": "secret_yaml",
            "domain": "feishu",
            "connectionMode": "websocket",
            "encryptKey": "encrypt_yaml",
            "verificationToken": "verify_yaml",
            "webhook_host": "127.0.0.8",
            "webhookPath": "/yaml-hook",
            "webhookPort": 8888,
            "allowFrom": ["ou_yaml"],
            "groupPolicy": "disabled",
            "requireMention": True,
            "allowBots": "none",
            "allowAllUsers": False,
            "botOpenId": "ou_bot_yaml",
            "botName": "YAML Bot",
            "dmPolicy": "allowlist",
        }

        with patch.dict(os.environ, env, clear=True):
            normalized = self.module._apply_yaml_config({}, yaml_config)
            adapter = self.module.FeishuAdapter(
                _PlatformConfig(extra=normalized)
            )
            account = self.module.FeishuAdapter(
                _PlatformConfig(
                    extra={
                        **normalized,
                        "appId": "cli_account",
                        "appSecret": "secret_account",
                        "domain": "feishu",
                        "webhookPath": "/account-hook",
                        "webhookPort": 7777,
                        "allowFrom": ["ou_account"],
                        "groupPolicy": "allowlist",
                        "requireMention": True,
                        "allowBots": "none",
                        "allowAllUsers": False,
                        "botOpenId": "ou_bot_account",
                        "botName": "Account Bot",
                        "_account_id": "account",
                        "_namespace_account": True,
                    }
                )
            )

        self.assertEqual(adapter._app_id, "cli_env")
        self.assertEqual(adapter._app_secret, "secret_env")
        self.assertEqual(adapter._domain_name, "lark")
        self.assertEqual(adapter._connection_mode, "webhook")
        self.assertEqual(adapter._encrypt_key, "encrypt_env")
        self.assertEqual(adapter._verification_token, "verify_env")
        self.assertEqual(adapter._webhook_host, "127.0.0.9")
        self.assertEqual(adapter._webhook_path, "/env-hook")
        self.assertEqual(adapter._webhook_port, 9999)
        self.assertEqual(adapter._allowed_group_users, {"ou_env"})
        self.assertEqual(adapter._group_policy, "open")
        self.assertEqual(adapter._default_group_policy, "open")
        self.assertFalse(adapter._require_mention)
        self.assertTrue(adapter._require_mention_explicit)
        self.assertEqual(adapter._allow_bots, "all")
        self.assertTrue(adapter._allow_all_users)
        self.assertEqual(adapter._bot_open_id, "ou_bot_env")
        self.assertEqual(adapter._bot_name, "Env Bot")

        self.assertEqual(account._app_id, "cli_account")
        self.assertEqual(account._app_secret, "secret_account")
        self.assertEqual(account._domain_name, "feishu")
        self.assertEqual(account._webhook_path, "/account-hook")
        self.assertEqual(account._webhook_port, 7777)
        self.assertEqual(account._allowed_group_users, {"ou_account"})
        self.assertEqual(account._group_policy, "allowlist")
        self.assertEqual(account._default_group_policy, "allowlist")
        self.assertTrue(account._require_mention)
        self.assertTrue(account._require_mention_explicit)
        self.assertEqual(account._allow_bots, "none")
        self.assertFalse(account._allow_all_users)
        self.assertEqual(account._bot_open_id, "ou_bot_account")
        self.assertEqual(account._bot_name, "Account Bot")

        sender = types.SimpleNamespace(
            sender_type="user",
            sender_id=types.SimpleNamespace(
                open_id="ou_unknown",
                user_id=None,
                union_id=None,
            ),
        )
        message = types.SimpleNamespace(chat_type="p2p", chat_id="oc_dm")
        self.assertIsNone(adapter._admit(sender, message))
        self.assertEqual(
            account._admit(sender, message),
            "dm_policy_rejected",
        )

    def test_profile_scoped_env_switches_without_process_env_fallback(
        self,
    ) -> None:
        yaml_config = {
            "appId": "cli_yaml",
            "appSecret": "secret_yaml",
            "domain": "feishu",
            "webhookPath": "/yaml-hook",
            "allowFrom": ["ou_yaml"],
            "groupPolicy": "disabled",
            "requireMention": True,
        }
        active_scope: dict[str, dict[str, str] | None] = {
            "value": {
                "FEISHU_APP_ID": "cli_profile_a",
                "FEISHU_DOMAIN": "lark",
                "FEISHU_WEBHOOK_PATH": "/profile-a",
                "FEISHU_ALLOWED_USERS": "ou_profile_a",
                "FEISHU_GROUP_POLICY": "open",
                "FEISHU_REQUIRE_MENTION": "false",
            }
        }
        secret_scope = types.ModuleType("agent.secret_scope")
        secret_scope.current_secret_scope = lambda: active_scope["value"]
        secret_scope.get_secret = lambda name, default=None: (
            (active_scope["value"] or {}).get(name, default)
        )
        secret_scope.is_multiplex_active = lambda: True
        agent = types.ModuleType("agent")
        agent.__path__ = []
        stale_process_env = {
            "FEISHU_APP_ID": "cli_stale",
            "FEISHU_APP_SECRET": "secret_stale",
            "FEISHU_DOMAIN": "lark",
            "FEISHU_WEBHOOK_PATH": "/stale",
            "FEISHU_ALLOWED_USERS": "ou_stale",
            "FEISHU_GROUP_POLICY": "open",
            "FEISHU_REQUIRE_MENTION": "false",
        }

        with (
            patch.dict(os.environ, stale_process_env, clear=True),
            patch.dict(
                sys.modules,
                {
                    "agent": agent,
                    "agent.secret_scope": secret_scope,
                },
            ),
        ):
            normalized = self.module._apply_yaml_config({}, yaml_config)
            profile_a = self.module.FeishuAdapter(
                _PlatformConfig(extra=normalized)
            )
            active_scope["value"] = {
                "FEISHU_APP_ID": "cli_profile_b",
                "FEISHU_DOMAIN": "feishu",
                "FEISHU_WEBHOOK_PATH": "/profile-b",
                "FEISHU_ALLOWED_USERS": "ou_profile_b",
                "FEISHU_GROUP_POLICY": "allowlist",
                "FEISHU_REQUIRE_MENTION": "true",
            }
            profile_b = self.module.FeishuAdapter(
                _PlatformConfig(extra=normalized)
            )
            active_scope["value"] = None
            unscoped = self.module.FeishuAdapter(
                _PlatformConfig(extra=normalized)
            )

        self.assertEqual(profile_a._app_id, "cli_profile_a")
        self.assertEqual(profile_a._domain_name, "lark")
        self.assertEqual(profile_a._webhook_path, "/profile-a")
        self.assertEqual(profile_a._allowed_group_users, {"ou_profile_a"})
        self.assertEqual(profile_a._group_policy, "open")
        self.assertFalse(profile_a._require_mention)
        self.assertEqual(profile_b._app_id, "cli_profile_b")
        self.assertEqual(profile_b._domain_name, "feishu")
        self.assertEqual(profile_b._webhook_path, "/profile-b")
        self.assertEqual(profile_b._allowed_group_users, {"ou_profile_b"})
        self.assertEqual(profile_b._group_policy, "allowlist")
        self.assertTrue(profile_b._require_mention)
        self.assertEqual(unscoped._app_id, "cli_yaml")
        self.assertEqual(unscoped._app_secret, "secret_yaml")
        self.assertEqual(unscoped._domain_name, "feishu")
        self.assertEqual(unscoped._webhook_path, "/yaml-hook")
        self.assertEqual(unscoped._allowed_group_users, {"ou_yaml"})
        self.assertEqual(unscoped._group_policy, "disabled")
        self.assertTrue(unscoped._require_mention)


if __name__ == "__main__":
    unittest.main()
