"""Hermes registry contracts for dependency probes and env enablement."""

from __future__ import annotations

import sys
import types
import unittest
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from tests.test_ask_user_question_adapter import (
    PACKAGE_DIR,
    _MISSING_MODULE,
    _load_modules,
    _load_package_module,
)


class HermesRegistrationTests(unittest.TestCase):
    """Verify the callables the plugin hands to Hermes' platform registry."""

    @classmethod
    def setUpClass(cls) -> None:
        _, cls.adapter_module, cls.previous_modules = _load_modules()

    @classmethod
    def tearDownClass(cls) -> None:
        for name, previous in cls.previous_modules.items():
            if previous is _MISSING_MODULE:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    @staticmethod
    def _package_manager(**members: Any) -> types.ModuleType:
        module = types.ModuleType("pm")
        module.available = members.get("available", Mock(return_value=False))
        module.ensure_import = members.get("ensure_import", Mock())
        return module

    def test_passive_probe_never_installs(self) -> None:
        """The registry check_fn reports availability without installing."""
        package_manager = self._package_manager()
        with (
            patch.object(self.adapter_module, "FEISHU_AVAILABLE", False),
            patch.dict(sys.modules, {"pm": package_manager}),
        ):
            self.assertFalse(self.adapter_module.feishu_deps_present())

        package_manager.available.assert_called_once_with("feishu")
        package_manager.ensure_import.assert_not_called()

    def test_installer_installs_feishu_extra_and_binds_sdk(self) -> None:
        """The registry ensure_deps_fn installs the extra, then binds the SDK."""
        package_manager = self._package_manager()
        sdk = object()
        module = self.adapter_module
        with (
            patch.object(module, "FEISHU_AVAILABLE", False),
            patch.object(module, "lark", None),
            patch.object(
                module,
                "_import_lark_oapi",
                return_value={"lark": sdk, "FEISHU_AVAILABLE": True},
            ),
            patch.dict(sys.modules, {"pm": package_manager}),
        ):
            self.assertTrue(module.check_feishu_requirements())
            self.assertIs(module.lark, sdk)
            self.assertTrue(module.FEISHU_AVAILABLE)

        package_manager.ensure_import.assert_called_once_with("feishu")

    def test_installer_failure_leaves_sdk_unbound(self) -> None:
        """A failed install reports the platform unavailable."""
        package_manager = self._package_manager(
            ensure_import=Mock(side_effect=RuntimeError("offline")),
        )
        module = self.adapter_module
        importer = Mock()
        with (
            patch.object(module, "FEISHU_AVAILABLE", False),
            patch.object(module, "_import_lark_oapi", importer),
            patch.dict(sys.modules, {"pm": package_manager}),
            self.assertLogs(module.logger, level="WARNING"),
        ):
            self.assertFalse(module.check_feishu_requirements())
            self.assertFalse(module.FEISHU_AVAILABLE)

        importer.assert_not_called()

    def _env_enablement(self, secrets: dict[str, str]) -> tuple[Any, Mock]:
        shared = types.ModuleType("gateway.platforms._shared")
        shared.get_scoped_secret = lambda name, default=None: secrets.get(name, default)
        seed = Mock(
            side_effect=lambda spec, *, home_env: {
                **{
                    key: secrets[env]
                    for env, key, _conv in spec
                    if secrets.get(env)
                },
                **(
                    {"home_channel": {"chat_id": secrets[home_env], "name": "Home"}}
                    if secrets.get(home_env)
                    else {}
                ),
            }
        )
        shared.seed_extra_from_env = seed
        with patch.dict(sys.modules, {"gateway.platforms._shared": shared}):
            return self.adapter_module._env_enablement(), seed

    def test_env_enablement_requires_both_credentials(self) -> None:
        """An app ID without its secret does not enable the platform."""
        result, seed = self._env_enablement({"FEISHU_APP_ID": "cli_app"})

        self.assertIsNone(result)
        seed.assert_not_called()

    def test_env_enablement_seeds_core_keys_and_home_thread(self) -> None:
        """Env-only setups get the extras Hermes core used to bridge itself."""
        result, seed = self._env_enablement(
            {
                "FEISHU_APP_ID": "cli_app",
                "FEISHU_APP_SECRET": "secret",
                "FEISHU_DOMAIN": "lark",
                "FEISHU_HOME_CHANNEL": "oc_home",
                "FEISHU_HOME_CHANNEL_THREAD_ID": "omt_thread",
            }
        )

        spec, kwargs = seed.call_args.args[0], seed.call_args.kwargs
        self.assertEqual(
            {(env, key) for env, key, _conv in spec},
            {
                ("FEISHU_APP_ID", "app_id"),
                ("FEISHU_APP_SECRET", "app_secret"),
                ("FEISHU_DOMAIN", "domain"),
                ("FEISHU_CONNECTION_MODE", "connection_mode"),
                ("FEISHU_ENCRYPT_KEY", "encrypt_key"),
                ("FEISHU_VERIFICATION_TOKEN", "verification_token"),
            },
        )
        self.assertEqual(kwargs, {"home_env": "FEISHU_HOME_CHANNEL"})
        self.assertEqual(
            result,
            {
                "app_id": "cli_app",
                "app_secret": "secret",
                "domain": "lark",
                "home_channel": {
                    "chat_id": "oc_home",
                    "name": "Home",
                    "thread_id": "omt_thread",
                },
            },
        )
        self.assertTrue(self.adapter_module._is_connected(types.SimpleNamespace(extra=result)))


class DurableAdmissionTests(unittest.IsolatedAsyncioTestCase):
    """Verify the contracts Hermes' durable message admission relies on."""

    @classmethod
    def setUpClass(cls) -> None:
        _, cls.adapter_module, cls.previous_modules = _load_modules()

    @classmethod
    def tearDownClass(cls) -> None:
        for name, previous in cls.previous_modules.items():
            if previous is _MISSING_MODULE:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    def _adapter(self, extra: dict[str, Any]) -> Any:
        settings = self.adapter_module.FeishuAdapter._load_settings(
            {"appId": "cli_test", "appSecret": "secret", **extra}
        )
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter._apply_settings(settings)
        return adapter

    @staticmethod
    def _inbound(
        *,
        message_id: str,
        chat_id: str,
        chat_type: str,
        open_id: str,
        user_id: str,
    ) -> Any:
        return types.SimpleNamespace(
            message_id=message_id,
            raw_message=types.SimpleNamespace(
                event=types.SimpleNamespace(
                    sender=types.SimpleNamespace(
                        sender_id=types.SimpleNamespace(
                            open_id=open_id,
                            user_id=user_id,
                            union_id="on_union",
                        )
                    ),
                    message=types.SimpleNamespace(
                        message_id=message_id,
                        chat_id=chat_id,
                        chat_type=chat_type,
                    ),
                )
            ),
            source=types.SimpleNamespace(
                chat_id=chat_id,
                chat_type="dm" if chat_type == "p2p" else "group",
                user_id=user_id,
                user_id_alt="on_union",
            ),
        )

    @staticmethod
    def _snapshot_source(**fields: Any) -> Any:
        """Model the SessionSource Hermes restores without Feishu payloads."""
        values = {
            "chat_id": "",
            "chat_id_alt": None,
            "chat_type": "dm",
            "user_id": None,
            "user_id_alt": None,
            "message_id": None,
            "scope_id": None,
            "is_bot": False,
        }
        values.update(fields)
        return types.SimpleNamespace(**values)

    async def test_dm_allowlist_grant_uses_the_stashed_open_id(self) -> None:
        """A tenant user ID in the snapshot still matches an open ID allowlist."""
        adapter = self._adapter({"dmPolicy": "allowlist", "allowFrom": ["ou_allowed"]})
        adapter._remember_admission_ticket(
            self._inbound(
                message_id="om_dm",
                chat_id="oc_dm",
                chat_type="p2p",
                open_id="ou_allowed",
                user_id="tenant_user",
            )
        )
        source = self._snapshot_source(
            chat_id="oc_dm",
            user_id="tenant_user",
            user_id_alt="on_union",
            message_id="om_dm",
        )

        self.assertTrue(await adapter.reauthorize_native_roles(source))

        adapter._apply_settings(
            adapter._load_settings(
                {"appId": "cli_test", "appSecret": "secret", "dmPolicy": "pairing"}
            )
        )
        self.assertFalse(await adapter.reauthorize_native_roles(source))

    async def test_group_grant_follows_current_group_policy(self) -> None:
        """A group grant is revoked when the sender leaves the group allowlist."""
        adapter = self._adapter(
            {"groupPolicy": "allowlist", "groupAllowFrom": ["ou_member"]}
        )
        adapter._remember_admission_ticket(
            self._inbound(
                message_id="om_group",
                chat_id="oc_group",
                chat_type="group",
                open_id="ou_member",
                user_id="",
            )
        )
        source = self._snapshot_source(
            chat_id="oc_group",
            chat_type="group",
            user_id="ou_member",
            message_id="om_group",
        )

        self.assertTrue(await adapter.reauthorize_native_roles(source))

        adapter._apply_settings(
            adapter._load_settings(
                {
                    "appId": "cli_test",
                    "appSecret": "secret",
                    "groupPolicy": "allowlist",
                    "groupAllowFrom": ["ou_other"],
                }
            )
        )
        self.assertFalse(await adapter.reauthorize_native_roles(source))

    async def test_restarted_turn_falls_back_to_snapshot_ids(self) -> None:
        """Without a stashed ticket the snapshot's own IDs are re-checked."""
        adapter = self._adapter(
            {"groupPolicy": "allowlist", "groupAllowFrom": ["ou_member"]}
        )

        self.assertTrue(
            await adapter.reauthorize_native_roles(
                self._snapshot_source(
                    chat_id="work::oc_group",
                    chat_id_alt="oc_group",
                    chat_type="group",
                    user_id="ou_member",
                    message_id="om_unknown",
                )
            )
        )
        self.assertFalse(
            await adapter.reauthorize_native_roles(
                self._snapshot_source(chat_type="group", chat_id="oc_group")
            )
        )

    async def test_multi_account_routes_the_check_to_the_account(self) -> None:
        """The account wrapper delegates to the child that admitted the turn."""
        previous = sys.modules.get("hermes_lark.multi_account", _MISSING_MODULE)
        multi_module = _load_package_module(
            "hermes_lark.multi_account",
            PACKAGE_DIR / "multi_account.py",
        )
        self.addCleanup(
            lambda: sys.modules.pop("hermes_lark.multi_account", None)
            if previous is _MISSING_MODULE
            else sys.modules.__setitem__("hermes_lark.multi_account", previous)
        )
        wrapper = object.__new__(multi_module.MultiAccountFeishuAdapter)
        child = types.SimpleNamespace(reauthorize_native_roles=AsyncMock(return_value=True))
        wrapper._children = {"work": child}
        wrapper._default_account_id = "work"
        source = self._snapshot_source(chat_id="work::oc_group", scope_id="work")

        self.assertTrue(await wrapper.reauthorize_native_roles(source))
        child.reauthorize_native_roles.assert_awaited_once_with(source)

    def test_adapter_declares_native_approval_buttons(self) -> None:
        """Hermes only sends approval cards to adapters that say they render them."""
        self.assertTrue(self.adapter_module.FeishuAdapter.supports_exec_approval_buttons())

    def test_send_targets_accept_only_deliverable_ids(self) -> None:
        """Explicit send targets name chats or open IDs, optionally scoped."""
        pattern = self.adapter_module._SEND_TARGET_RE
        self.assertEqual(pattern.fullmatch("oc_chat").groups(), ("oc_chat", None))
        self.assertEqual(
            pattern.fullmatch("work::oc_chat:om_root").groups(),
            ("work::oc_chat", "om_root"),
        )
        self.assertEqual(pattern.fullmatch(" ou_user ").groups(), ("ou_user", None))
        for target in ("on_union", "chat_name", "work:oc_chat", "oc_a:om_b:extra"):
            self.assertIsNone(pattern.fullmatch(target), target)

    def test_private_metadata_stays_out_of_hermes_metadata(self) -> None:
        """Hermes refuses inbound events whose metadata it does not own."""
        event = types.SimpleNamespace(metadata={})
        setattr(event, self.adapter_module.FEISHU_METADATA_ATTR, {"feishu_bot_peer": {}})

        self.assertEqual(event.metadata, {})
        self.assertEqual(
            self.adapter_module._feishu_metadata(event),
            {"feishu_bot_peer": {}},
        )
        self.assertEqual(self.adapter_module._feishu_metadata(types.SimpleNamespace()), {})


if __name__ == "__main__":
    unittest.main()
