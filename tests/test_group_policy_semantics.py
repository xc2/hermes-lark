"""Focused tests for OpenClaw-compatible Feishu group policy semantics."""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from typing import Any

from tests.test_ask_user_question_adapter import _MISSING_MODULE, _load_modules


class GroupPolicySemanticsTests(unittest.TestCase):
    """Verify sender, group, wildcard, admin, and legacy policy precedence."""

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
            {
                "appId": "cli_test",
                "appSecret": "secret",
                **extra,
            }
        )
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter._apply_settings(settings)
        return adapter

    @staticmethod
    def _sender(open_id: str, user_id: str = "") -> Any:
        return SimpleNamespace(open_id=open_id, user_id=user_id)

    def test_group_allow_from_does_not_inherit_dm_allow_from(self) -> None:
        settings = self.adapter_module.FeishuAdapter._load_settings(
            {
                "allowFrom": ["OU_DM_ONLY"],
            }
        )

        self.assertEqual(settings.allowed_group_users, frozenset({"ou_dm_only"}))
        self.assertEqual(settings.group_allow_from, frozenset())
        self.assertEqual(settings.legacy_group_allow_chats, frozenset())

    def test_exact_group_does_not_union_wildcard_allow_from(self) -> None:
        adapter = self._adapter(
            {
                "groupPolicy": "allowlist",
                "groups": {
                    "*": {
                        "groupPolicy": "allowlist",
                        "allowFrom": ["OU_WILDCARD"],
                    },
                    "OC_EXACT": {},
                },
            }
        )

        exact_rule = adapter._group_rule_for("oc_exact")
        fallback_rule = adapter._group_rule_for("oc_other")

        self.assertIsNotNone(exact_rule)
        self.assertEqual(exact_rule.allowlist, set())
        self.assertEqual(fallback_rule.allowlist, {"ou_wildcard"})
        self.assertFalse(
            adapter._allow_group_message(
                self._sender("ou_wildcard"),
                "oc_exact",
            )
        )
        self.assertTrue(
            adapter._allow_group_message(
                self._sender("OU_WILDCARD"),
                "OC_OTHER",
            )
        )

    def test_hard_group_denials_apply_before_admin_override(self) -> None:
        sender = self._sender("ou_admin")

        disabled_rule = self._adapter(
            {
                "admins": ["OU_ADMIN"],
                "groupPolicy": "open",
                "groups": {"oc_disabled": {"enabled": False}},
            }
        )
        disabled_policy = self._adapter(
            {
                "admins": ["OU_ADMIN"],
                "groupPolicy": "disabled",
            }
        )
        unlisted_group = self._adapter(
            {
                "admins": ["OU_ADMIN"],
                "groupPolicy": "open",
                "groups": {"oc_listed": {}},
            }
        )

        self.assertFalse(
            disabled_rule._allow_group_message(sender, "oc_disabled")
        )
        self.assertFalse(
            disabled_policy._allow_group_message(sender, "oc_any")
        )
        self.assertFalse(
            unlisted_group._allow_group_message(sender, "oc_unlisted")
        )

    def test_sender_and_admin_comparisons_are_case_insensitive(self) -> None:
        global_allow = self._adapter(
            {
                "groupPolicy": "allowlist",
                "groupAllowFrom": ["OU_GLOBAL"],
            }
        )
        exact_allow = self._adapter(
            {
                "groupPolicy": "allowlist",
                "groups": {
                    "OC_EXACT": {
                        "groupPolicy": "allowlist",
                        "allowFrom": ["OU_EXACT"],
                    }
                },
            }
        )
        admin_only = self._adapter(
            {
                "admins": ["OU_ADMIN"],
                "groupPolicy": "admin_only",
            }
        )

        self.assertTrue(
            global_allow._allow_group_message(
                self._sender("ou_global"),
                "OC_ANY",
            )
        )
        self.assertTrue(
            exact_allow._allow_group_message(
                self._sender("ou_exact"),
                "oc_exact",
            )
        )
        self.assertTrue(
            admin_only._allow_group_message(
                self._sender("ou_admin"),
                "oc_any",
            )
        )

    def test_legacy_oc_group_allow_from_admits_chat_not_sender(self) -> None:
        legacy = self._adapter(
            {
                "groupPolicy": "allowlist",
                "groupAllowFrom": ["OC_LEGACY"],
            }
        )

        self.assertEqual(legacy._group_allow_from, set())
        self.assertEqual(legacy._legacy_group_allow_chats, {"oc_legacy"})
        self.assertTrue(
            legacy._allow_group_message(
                self._sender("ou_any_sender"),
                "oc_legacy",
            )
        )
        self.assertFalse(
            legacy._allow_group_message(
                self._sender("oc_legacy"),
                "oc_other",
            )
        )

    def test_explicit_sender_or_group_policy_supersedes_legacy_chat_admit(
        self,
    ) -> None:
        sender_filtered = self._adapter(
            {
                "groupPolicy": "allowlist",
                "groupAllowFrom": ["OC_LEGACY", "OU_ALLOWED"],
            }
        )
        exact_rule = self._adapter(
            {
                "groupPolicy": "allowlist",
                "groupAllowFrom": ["OC_LEGACY"],
                "groups": {
                    "oc_legacy": {
                        "groupPolicy": "allowlist",
                        "allowFrom": ["OU_ALLOWED"],
                    }
                },
            }
        )
        disabled = self._adapter(
            {
                "groupPolicy": "disabled",
                "groupAllowFrom": ["OC_LEGACY"],
                "admins": ["OU_ADMIN"],
            }
        )

        for adapter in (sender_filtered, exact_rule):
            self.assertTrue(
                adapter._allow_group_message(
                    self._sender("ou_allowed"),
                    "oc_legacy",
                )
            )
            self.assertFalse(
                adapter._allow_group_message(
                    self._sender("ou_other"),
                    "oc_legacy",
                )
            )
        self.assertFalse(
            disabled._allow_group_message(
                self._sender("ou_admin"),
                "oc_legacy",
            )
        )


if __name__ == "__main__":
    unittest.main()
