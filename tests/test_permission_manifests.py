"""Contracts for the importable Feishu permission manifests."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any


class PermissionManifestTests(unittest.TestCase):
    """Keep production, skill, and live-E2E application scopes intentional."""

    @classmethod
    def setUpClass(cls) -> None:
        """Load the manifests once for contract comparisons."""
        cls.production = cls._read_manifest("production.json")
        cls.skills = cls._read_manifest("skills.json")
        cls.e2e = cls._read_manifest("e2e.json")

    @staticmethod
    def _read_manifest(name: str) -> dict[str, list[str]]:
        """Read one Feishu bulk-import manifest with a strict shape."""
        path = Path(__file__).resolve().parents[1] / "permissions" / name
        document: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or set(document) != {"scopes"}:
            raise AssertionError(f"{path} must only contain a scopes object")
        scopes = document["scopes"]
        if not isinstance(scopes, dict) or set(scopes) != {"tenant", "user"}:
            raise AssertionError(
                f"{path} scopes must only contain tenant and user arrays"
            )
        for identity, values in scopes.items():
            if not isinstance(values, list) or not all(
                isinstance(value, str) and value for value in values
            ):
                raise AssertionError(f"{path} scopes.{identity} must be strings")
        return scopes

    def test_scope_arrays_are_sorted_and_unique(self) -> None:
        """Keep imports deterministic and reviewable."""
        for manifest in (self.production, self.skills, self.e2e):
            for scopes in manifest.values():
                self.assertEqual(scopes, sorted(set(scopes)))

    def test_skills_supplement_is_user_only_and_disjoint(self) -> None:
        """Keep the optional skill grant separate from the production baseline."""
        self.assertEqual(self.skills["tenant"], [])
        self.assertTrue(self.skills["user"])
        self.assertNotIn("offline_access", self.skills["user"])
        for identity in ("tenant", "user"):
            self.assertFalse(
                set(self.skills[identity]) & set(self.production[identity])
            )

    def test_production_includes_pinned_upstream_runtime_scopes(self) -> None:
        """Cover the app scopes required by the pinned OpenClaw source."""
        required = {
            "application:application:self_manage",
            "cardkit:card:read",
            "cardkit:card:write",
            "contact:contact.base:readonly",
            "docx:document:readonly",
            "im:chat:read",
            "im:chat:update",
            "im:message.group_at_msg:readonly",
            "im:message.p2p_msg:readonly",
            "im:message.pins:read",
            "im:message.pins:write_only",
            "im:message.reactions:read",
            "im:message.reactions:write_only",
            "im:message:readonly",
            "im:message:recall",
            "im:message:send_as_bot",
            "im:message:send_multi_users",
            "im:message:send_sys_msg",
            "im:message:update",
            "im:resource",
        }
        self.assertFalse(required - set(self.production["tenant"]))
        self.assertIn("im:message.group_msg", self.production["tenant"])

    def test_e2e_only_adds_test_driver_permissions(self) -> None:
        """Prevent test-only impersonation and cleanup scopes leaking to prod."""
        self.assertEqual(
            set(self.e2e["tenant"]) - set(self.production["tenant"]),
            {"im:chat:delete"},
        )
        self.assertEqual(
            set(self.e2e["user"]) - set(self.production["user"]),
            {"im:message", "im:message.send_as_user", "im:message:recall"},
        )
        self.assertFalse(
            set(self.production["tenant"]) - set(self.e2e["tenant"])
        )
        self.assertFalse(set(self.production["user"]) - set(self.e2e["user"]))


if __name__ == "__main__":
    unittest.main()
