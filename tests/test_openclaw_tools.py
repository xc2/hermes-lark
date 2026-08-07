"""Behavioral tests for the pinned openclaw-lark tool bridge."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "hermes_lark" / "openclaw_tools.py"
MANIFEST_PATH = ROOT / "hermes_lark" / "data" / "openclaw-tools.json"
BUNDLE_PATH = ROOT / "hermes_lark" / "node" / "openclaw_tools_bridge.mjs"
AUTO_AUTH_SHIM_PATH = ROOT / "hermes_lark" / "node" / "auto-auth-shim.ts"


def _load_module() -> ModuleType:
    """Load only the tool module without importing Hermes adapter dependencies."""
    spec = importlib.util.spec_from_file_location(
        "hermes_lark_openclaw_tools_test",
        MODULE_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeContext:
    """Capture Hermes tool registrations for assertions."""

    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = []

    def register_tool(self, **kwargs: Any) -> None:
        """Record one tool registration."""
        self.tools.append(kwargs)


class OpenClawToolBridgeTests(unittest.TestCase):
    """Verify registration, ticket propagation, and the bridge protocol."""

    @classmethod
    def setUpClass(cls) -> None:
        """Load the isolated bridge module once."""
        cls.module = _load_module()

    def setUp(self) -> None:
        """Isolate process configuration and pending interaction state."""
        previous_config = os.environ.pop("FEISHU_OPENCLAW_CONFIG_JSON", None)
        self.addCleanup(self._restore_config, previous_config)
        previous_home = os.environ.pop("HERMES_HOME", None)
        self.addCleanup(self._restore_env, "HERMES_HOME", previous_home)
        self.module.configure_bridge_config(None)
        self.addCleanup(self.module.configure_bridge_config, None)
        self.module.unregister_interaction_host("default")
        for interaction in self.module.list_pending_interactions():
            self.module.cancel_interaction(interaction["token"])

    @staticmethod
    def _restore_env(name: str, previous: str | None) -> None:
        """Restore one process setting changed for test isolation."""
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous

    def _restore_config(self, previous: str | None) -> None:
        """Restore the caller's OpenClaw config override."""
        self._restore_env("FEISHU_OPENCLAW_CONFIG_JSON", previous)

    def _ticket(
        self,
        *,
        account_id: str = "default",
        chat_id: str = "oc_chat",
        chat_type: str = "p2p",
        profile: str = "default",
        profile_scope: str = "",
    ) -> Any:
        """Build one complete tool ticket."""
        return self.module.ToolTicket(
            session_id="session-1",
            message_id="om_message",
            chat_id=chat_id,
            account_id=account_id,
            profile=profile,
            profile_scope=profile_scope,
            sender_open_id="ou_user",
            chat_type=chat_type,
        )

    def test_ticket_prefers_explicit_synthetic_open_ids(self) -> None:
        """Synthetic callback identities must not degrade to a union ID."""
        source = SimpleNamespace(
            chat_id="oc_chat",
            chat_type="dm",
            user_id="u_tenant",
            user_id_alt="on_union",
            scope_id="work",
            profile="coder",
            _transport_adapter_ref=lambda: SimpleNamespace(
                _profile_scope_key="/hermes/profiles/coder"
            ),
        )
        cases = [
            SimpleNamespace(
                openclaw_continuation={
                    "ticket": {"sender_open_id": "ou_continuation"}
                }
            ),
            SimpleNamespace(operator=SimpleNamespace(open_id="ou_operator")),
            SimpleNamespace(
                event=SimpleNamespace(
                    user_id=SimpleNamespace(open_id="ou_reaction")
                )
            ),
            {
                "event": {
                    "inviter": {
                        "id": {"open_id": "ou_inviter"}
                    }
                }
            },
        ]

        self.assertEqual(
            [
                self.module.ticket_from_event(
                    SimpleNamespace(
                        raw_message=raw,
                        source=source,
                        message_id=f"synthetic-{index}",
                    )
                ).sender_open_id
                for index, raw in enumerate(cases)
            ],
            [
                "ou_continuation",
                "ou_operator",
                "ou_reaction",
                "ou_inviter",
            ],
        )
        ticket = self.module.ticket_from_event(
            SimpleNamespace(
                raw_message=cases[0],
                source=source,
                message_id="synthetic-profile",
            )
        )
        self.assertEqual(ticket.profile, "coder")
        self.assertEqual(
            ticket.profile_scope,
            "/hermes/profiles/coder",
        )

    def test_ticket_separates_session_root_from_native_thread_id(self) -> None:
        """Hermes routing uses an om_* root while tools receive the omt_* ID."""
        source = SimpleNamespace(
            chat_id="oc_chat",
            chat_type="group",
            user_id="u_user",
            thread_id="om_root",
            feishu_session_thread_id="om_root",
            feishu_thread_id="omt_native",
        )
        raw = SimpleNamespace(
            event=SimpleNamespace(
                message=SimpleNamespace(
                    message_id="om_reply",
                    chat_id="oc_chat",
                    chat_type="group",
                    root_id="om_root",
                    thread_id="omt_native",
                )
            )
        )

        ticket = self.module.ticket_from_event(
            SimpleNamespace(
                raw_message=raw,
                source=source,
                message_id="om_reply",
            )
        )

        self.assertEqual(ticket.session_thread_id, "om_root")
        self.assertEqual(ticket.thread_id, "omt_native")
        self.assertEqual(ticket.to_bridge_dict()["threadId"], "omt_native")

    def test_ticket_preserves_raw_sender_ids_without_account_prefixes(
        self,
    ) -> None:
        """Synthetic pairing turns reuse the original Feishu identity tiers."""
        source = SimpleNamespace(
            chat_id="work::oc_chat",
            chat_id_alt="oc_chat",
            chat_type="dm",
            user_id="work::u_tenant",
            user_id_alt="on_union",
            feishu_user_id="u_tenant",
            feishu_user_id_alt="on_union",
            scope_id="work",
        )
        raw = SimpleNamespace(
            event=SimpleNamespace(
                sender=SimpleNamespace(
                    sender_id=SimpleNamespace(
                        open_id="ou_open",
                        user_id="u_tenant",
                        union_id="on_union",
                    )
                ),
                message=SimpleNamespace(
                    message_id="om_message",
                    chat_id="oc_chat",
                    chat_type="p2p",
                ),
            )
        )

        ticket = self.module.ticket_from_event(
            SimpleNamespace(
                raw_message=raw,
                source=source,
                message_id="om_message",
            )
        )

        self.assertEqual(ticket.sender_open_id, "ou_open")
        self.assertEqual(ticket.sender_user_id, "u_tenant")
        self.assertEqual(ticket.sender_union_id, "on_union")

    def test_registers_all_exact_manifest_schemas(self) -> None:
        """All 39 upstream schemas reach Hermes registration unchanged."""
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        context = _FakeContext()

        self.module.register(context)

        self.assertEqual(
            [item["name"] for item in context.tools],
            [item["name"] for item in manifest["tools"]],
        )
        self.assertEqual(len(context.tools), 39)
        for registered, upstream in zip(context.tools, manifest["tools"]):
            self.assertEqual(registered["toolset"], "feishu")
            self.assertEqual(
                registered["schema"],
                {
                    "name": upstream["name"],
                    "description": upstream["description"],
                    "parameters": upstream["parameters"],
                },
            )
            self.assertTrue(callable(registered["handler"]))
            self.assertTrue(callable(registered["check_fn"]))

    def test_registration_checks_apply_category_flags(self) -> None:
        """Disabled categories are hidden from Hermes tool definitions."""
        self.module.configure_bridge_config(
            {
                "channels": {
                    "feishu": {
                        "appId": "cli_default",
                        "appSecret": "secret",
                        "tools": {
                            "doc": False,
                            "drive": False,
                            "wiki": False,
                            "sheets": False,
                        },
                    }
                }
            }
        )
        context = _FakeContext()
        self.module.register(context)
        checks = {
            item["name"]: item["check_fn"]
            for item in context.tools
        }

        for tool_name in (
            "feishu_search_doc_wiki",
            "feishu_fetch_doc",
            "feishu_create_doc",
            "feishu_update_doc",
            "feishu_drive_file",
            "feishu_doc_comments",
            "feishu_doc_media",
            "feishu_wiki_space",
            "feishu_wiki_space_node",
            "feishu_sheet",
        ):
            self.assertFalse(checks[tool_name](), tool_name)
        self.assertTrue(checks["feishu_calendar_event"]())

    def test_registration_checks_union_enabled_account_categories(self) -> None:
        """One enabled account exposes a category globally, as upstream does."""
        self.module.configure_bridge_config(
            {
                "channels": {
                    "feishu": {
                        "tools": {"doc": False, "drive": False},
                        "accounts": {
                            "disabled": {
                                "enabled": False,
                                "appId": "cli_disabled",
                                "appSecret": "secret",
                                "tools": {"doc": True, "drive": True},
                            },
                            "docs": {
                                "appId": "cli_docs",
                                "appSecret": "secret",
                                "tools": {"doc": True},
                            },
                            "drive": {
                                "appId": "cli_drive",
                                "appSecret": "secret",
                                "tools": {"drive": True},
                            },
                        },
                    }
                }
            }
        )
        context = _FakeContext()
        self.module.register(context)
        checks = {
            item["name"]: item["check_fn"]
            for item in context.tools
        }

        self.assertTrue(checks["feishu_fetch_doc"]())
        self.assertTrue(checks["feishu_drive_file"]())

    def test_registration_checks_apply_channel_deny(self) -> None:
        """Exact and trailing-star deny entries hide tools before invocation."""
        self.module.configure_bridge_config(
            {
                "channels": {
                    "feishu": {
                        "appId": "cli_default",
                        "appSecret": "secret",
                        "tools": {
                            "deny": [
                                "feishu_get_user",
                                "feishu_calendar_*",
                            ]
                        },
                    }
                }
            }
        )
        context = _FakeContext()
        self.module.register(context)
        checks = {
            item["name"]: item["check_fn"]
            for item in context.tools
        }

        self.assertFalse(checks["feishu_get_user"]())
        self.assertFalse(checks["feishu_calendar_event"]())
        self.assertTrue(checks["feishu_task_task"]())

    def test_interactive_tool_exposes_resumable_host_contract(self) -> None:
        """AskUserQuestion reaches its host and retains callback state."""
        ticket = self._ticket()
        arguments = {
            "questions": [
                {
                    "question": "Continue?",
                    "header": "Confirm",
                    "options": [],
                    "multiSelect": False,
                }
            ]
        }
        delivered: list[Any] = []
        self.module.register_interaction_host(
            "default",
            lambda interaction: not delivered.append(interaction),
        )
        try:
            with self.module.tool_ticket(ticket):
                result = json.loads(
                    self.module.invoke_openclaw_tool(
                        "feishu_ask_user_question",
                        arguments,
                    )
                )
        finally:
            self.module.unregister_interaction_host("default")

        self.assertEqual(result["status"], "pending")
        token = result["question_id"]
        self.assertEqual(len(delivered), 1)
        pending = self.module.get_pending_interaction(token)
        self.assertEqual(pending["request"], arguments)

        resumed = self.module.resume_interaction(token, {"answer": "yes"})
        self.assertTrue(resumed["ok"])
        self.assertEqual(resumed["synthetic_event"]["session_id"], "session-1")
        self.assertIsNone(self.module.get_pending_interaction(token))

    def test_ask_user_expiry_consumes_state_and_notifies_live_host(self) -> None:
        """Question TTL actively drives the host's terminal card lifecycle."""
        expired: list[Any] = []
        expired_event = threading.Event()

        def expire(interaction: Any) -> bool:
            """Capture the interaction delivered by the timer thread."""
            expired.append(interaction)
            expired_event.set()
            return True

        self.module.register_interaction_host(
            "default",
            lambda _interaction: True,
            expiry_host=expire,
        )
        self.addCleanup(self.module.unregister_interaction_host, "default")
        arguments = {
            "questions": [
                {
                    "question": "Continue?",
                    "header": "Confirm",
                    "options": [],
                    "multiSelect": False,
                }
            ]
        }

        with patch.object(self.module, "_ASK_USER_TTL_SECONDS", 0.05):
            result = json.loads(
                self.module.invoke_openclaw_tool(
                    "feishu_ask_user_question",
                    arguments,
                    ticket=self._ticket(),
                )
            )

        self.assertTrue(expired_event.wait(2.0))
        self.assertEqual([item.token for item in expired], [result["question_id"]])
        self.assertIsNone(
            self.module.get_pending_interaction(result["question_id"])
        )

    def test_same_account_interactions_route_to_the_exact_profile_host(self) -> None:
        """Multiplexed profiles sharing ``default`` never cross-deliver."""
        delivered: dict[str, list[Any]] = {"coder": [], "reviewer": []}
        profile_scopes = {
            "coder": "/hermes/profiles/coder",
            "reviewer": "/hermes/profiles/reviewer",
        }
        for profile, profile_scope in profile_scopes.items():
            self.module.register_interaction_host(
                "default",
                lambda interaction, target=profile: (
                    delivered[target].append(interaction) is None
                ),
                profile_scope=profile_scope,
            )
            self.addCleanup(
                self.module.unregister_interaction_host,
                "default",
                profile_scope=profile_scope,
            )

        arguments = {
            "questions": [
                {
                    "question": "Continue?",
                    "header": "Confirm",
                    "options": [],
                    "multiSelect": False,
                }
            ]
        }
        for profile, profile_scope in profile_scopes.items():
            result = json.loads(
                self.module.invoke_openclaw_tool(
                    "feishu_ask_user_question",
                    arguments,
                    ticket=self._ticket(
                        profile=profile,
                        profile_scope=profile_scope,
                    ),
                )
            )
            self.assertEqual(result["status"], "pending")
            self.addCleanup(
                self.module.cancel_interaction,
                result["question_id"],
            )

        self.assertEqual(
            [item.ticket.profile for item in delivered["coder"]],
            ["coder"],
        )
        self.assertEqual(
            [item.ticket.profile for item in delivered["reviewer"]],
            ["reviewer"],
        )

        unavailable = json.loads(
            self.module.invoke_openclaw_tool(
                "feishu_ask_user_question",
                arguments,
                ticket=self._ticket(
                    profile="unserved",
                    profile_scope="/hermes/profiles/unserved",
                ),
            )
        )
        self.assertEqual(
            unavailable["error"]["code"],
            "interaction_host_unavailable",
        )
        self.assertEqual(len(delivered["coder"]), 1)
        self.assertEqual(len(delivered["reviewer"]), 1)

    def test_batch_oauth_requires_and_calls_a_live_host(self) -> None:
        """Batch OAuth never reports success without adapter ownership."""
        ticket = self._ticket()
        unavailable = json.loads(
            self.module.invoke_openclaw_tool(
                "feishu_oauth_batch_auth",
                {"scope": "calendar:calendar"},
                ticket=ticket,
            )
        )
        self.assertFalse(unavailable["ok"])
        self.assertEqual(
            unavailable["error"]["code"],
            "interaction_host_unavailable",
        )
        self.assertEqual(self.module.list_pending_interactions(), [])

        delivered: list[Any] = []
        self.module.register_interaction_host(
            "default",
            lambda interaction: not delivered.append(interaction),
        )
        self.addCleanup(self.module.unregister_interaction_host, "default")
        pending = json.loads(
            self.module.invoke_openclaw_tool(
                "feishu_oauth_batch_auth",
                {"scope": "calendar:calendar"},
                ticket=ticket,
            )
        )

        self.assertFalse(pending["ok"])
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["error"], "authorization_pending")
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0].kind, "oauth_batch_auth")
        self.assertNotIn(
            "resume_previous_operation",
            delivered[0].context,
        )
        self.assertTrue(
            self.module.cancel_interaction(pending["follow_up"]["token"])
        )

    def test_auto_auth_continuation_requires_and_calls_a_live_host(self) -> None:
        """Upstream auto-auth follow-ups are blocked unless delivered."""
        bridge_response = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": "authorization required"}],
                "details": {
                    "error": "host_callback_required",
                    "required_scope": "calendar:calendar",
                    "follow_up": {"kind": "oauth"},
                },
            },
        }
        ticket = self._ticket()

        blocked = json.loads(
            self.module._format_bridge_result(
                bridge_response,
                "feishu_calendar_event",
                {"action": "list"},
                ticket,
            )
        )
        self.assertEqual(blocked["follow_up"]["status"], "blocked")
        self.assertEqual(
            blocked["follow_up"]["error"],
            "interaction_host_unavailable",
        )
        self.assertEqual(self.module.list_pending_interactions(), [])

        delivered: list[Any] = []
        self.module.register_interaction_host(
            "default",
            lambda interaction: not delivered.append(interaction),
        )
        self.addCleanup(self.module.unregister_interaction_host, "default")
        pending = json.loads(
            self.module._format_bridge_result(
                bridge_response,
                "feishu_calendar_event",
                {"action": "list"},
                ticket,
            )
        )

        self.assertEqual(pending["follow_up"]["status"], "pending")
        self.assertEqual(len(delivered), 1)
        self.assertEqual(
            delivered[0].context["authorization"]["required_scope"],
            "calendar:calendar",
        )
        self.assertTrue(
            self.module.cancel_interaction(pending["follow_up"]["token"])
        )

    def test_auto_auth_continuation_preserves_deferred_user_scopes(self) -> None:
        """App permission handoff retains every later user OAuth scope."""
        bridge_response = {
            "ok": True,
            "result": {
                "content": [{"type": "text", "text": "authorization required"}],
                "details": {
                    "error": "host_callback_required",
                    "source_error": "AppScopeMissingError",
                    "missing_scopes": ["calendar:calendar"],
                    "all_required_scopes": [
                        "calendar:calendar",
                        "calendar:calendar.event:read",
                    ],
                    "deferred_scopes": [
                        "calendar:calendar",
                        "calendar:calendar.event:read",
                    ],
                    "user_auth_deferred": True,
                    "follow_up": {
                        "kind": "app_permission",
                        "deferred_scopes": [
                            "calendar:calendar",
                            "calendar:calendar.event:read",
                        ],
                    },
                },
            },
        }
        delivered: list[Any] = []
        self.module.register_interaction_host(
            "default",
            lambda interaction: not delivered.append(interaction),
        )
        self.addCleanup(self.module.unregister_interaction_host, "default")

        result = json.loads(
            self.module._format_bridge_result(
                bridge_response,
                "feishu_calendar_event",
                {"action": "list"},
                self._ticket(),
            )
        )

        self.assertEqual(result["follow_up"]["status"], "pending")
        self.assertEqual(
            delivered[0].context["authorization"]["all_required_scopes"],
            ["calendar:calendar", "calendar:calendar.event:read"],
        )
        self.assertEqual(
            delivered[0].context["authorization"]["deferred_scopes"],
            ["calendar:calendar", "calendar:calendar.event:read"],
        )
        self.module.cancel_interaction(result["follow_up"]["token"])

    def test_auto_auth_shim_maps_every_upstream_scope_field(self) -> None:
        """The bundled shim cannot drop structured authorization metadata."""
        source = AUTO_AUTH_SHIM_PATH.read_text(encoding="utf-8")

        for field in (
            "requiredScopes",
            "missingScopes",
            "allRequiredScopes",
            "appScopeVerified",
            "scopeNeedType",
            "tokenType",
            "all_required_scopes",
            "deferred_scopes",
        ):
            self.assertIn(field, source)

    def test_bridge_tool_timeout_is_disabled_unless_configured(self) -> None:
        """The host does not ambiguously abort long-running upstream calls."""
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(self.module._bridge_timeout_seconds())
        with patch.dict(
            os.environ,
            {"FEISHU_OPENCLAW_TOOL_TIMEOUT_SECONDS": "120"},
            clear=True,
        ):
            self.assertEqual(self.module._bridge_timeout_seconds(), 120.0)
        for disabled in ("0", "off", "none", "disabled"):
            with patch.dict(
                os.environ,
                {"FEISHU_OPENCLAW_TOOL_TIMEOUT_SECONDS": disabled},
                clear=True,
            ):
                self.assertIsNone(self.module._bridge_timeout_seconds())

    def test_revoke_is_a_direct_upstream_tool(self) -> None:
        """OAuth revoke executes in the Node bridge with sender identity."""
        ticket = self._ticket()
        response = {
            "ok": True,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"success": True}),
                    }
                ]
            },
        }
        with patch.object(
            self.module,
            "_run_bridge",
            return_value=response,
        ) as run_bridge:
            result = json.loads(
                self.module.invoke_openclaw_tool(
                    "feishu_oauth",
                    {"action": "revoke"},
                    ticket=ticket,
                )
            )

        self.assertTrue(result["success"])
        request = run_bridge.call_args.args[0]
        self.assertEqual(request["action"], "invoke")
        self.assertEqual(request["tool"], "feishu_oauth")
        self.assertEqual(request["ticket"]["senderOpenId"], "ou_user")
        self.assertEqual(self.module.list_pending_interactions(), [])

    def test_channel_and_account_tool_policy(self) -> None:
        """Exact, wildcard, and one-level account policies match upstream."""
        os.environ["FEISHU_OPENCLAW_CONFIG_JSON"] = json.dumps(
            {
                "channels": {
                    "feishu": {
                        "tools": {
                            "deny": [
                                "feishu_get_user",
                                "feishu_calendar_*",
                            ],
                            "allow": ["unchanged"],
                        },
                        "accounts": {
                            "WoRk": {
                                "tools": {
                                    "deny": ["terminal"],
                                }
                            }
                        },
                    }
                }
            }
        )
        default_ticket = self._ticket()
        work_ticket = self._ticket(account_id="work")

        self.assertEqual(
            self.module.evaluate_tool_policy("feishu_get_user", default_ticket),
            "channel_deny",
        )
        self.assertEqual(
            self.module.evaluate_tool_policy(
                "feishu_calendar_event",
                default_ticket,
            ),
            "channel_deny",
        )
        self.assertIsNone(
            self.module.evaluate_tool_policy("feishu_calendar", default_ticket)
        )
        self.assertEqual(
            self.module.evaluate_tool_policy("terminal", work_ticket),
            "channel_deny",
        )
        self.assertIsNone(
            self.module.evaluate_tool_policy("feishu_get_user", work_ticket)
        )

    def test_category_policy_isolated_per_account_at_invocation(self) -> None:
        """One account cannot enable a category for a restricted account."""
        os.environ["FEISHU_OPENCLAW_CONFIG_JSON"] = json.dumps(
            {
                "channels": {
                    "feishu": {
                        "appId": "cli_default",
                        "appSecret": "secret",
                        "tools": {"drive": True},
                        "accounts": {
                            "restricted": {
                                "tools": {"drive": False},
                            },
                            "enabled": {
                                "tools": {"drive": True},
                            },
                        },
                    }
                }
            }
        )

        self.assertEqual(
            self.module.evaluate_tool_policy(
                "feishu_drive_file",
                self._ticket(account_id="restricted"),
            ),
            "category_disabled",
        )
        self.assertIsNone(
            self.module.evaluate_tool_policy(
                "feishu_drive_file",
                self._ticket(account_id="enabled"),
            )
        )

    def test_runtime_bridge_config_is_replaced_on_reload(self) -> None:
        """A newer plugin config cannot inherit policy or accounts from the old one."""
        self.module.configure_bridge_config(
            {
                "channels": {
                    "feishu": {
                        "tools": {"deny": ["feishu_get_user"]},
                        "accounts": {
                            "old": {
                                "tools": {"deny": ["terminal"]},
                            }
                        },
                    }
                }
            }
        )
        self.assertEqual(
            self.module.evaluate_tool_policy("feishu_get_user", self._ticket()),
            "channel_deny",
        )
        self.assertEqual(
            self.module.evaluate_tool_policy(
                "terminal",
                self._ticket(account_id="old"),
            ),
            "channel_deny",
        )

        self.module.configure_bridge_config(
            {
                "channels": {
                    "feishu": {
                        "tools": {"deny": ["feishu_calendar_*"]},
                        "accounts": {
                            "new": {
                                "tools": {"deny": ["browser"]},
                            }
                        },
                    }
                }
            }
        )

        self.assertIsNone(
            self.module.evaluate_tool_policy("feishu_get_user", self._ticket())
        )
        self.assertIsNone(
            self.module.evaluate_tool_policy(
                "terminal",
                self._ticket(account_id="old"),
            )
        )
        self.assertEqual(
            self.module.evaluate_tool_policy(
                "browser",
                self._ticket(account_id="new"),
            ),
            "channel_deny",
        )

    def test_bridge_config_flattens_hermes_extra_and_account_credentials(
        self,
    ) -> None:
        """Hermes snake-case extras become OpenClaw values for Node tools."""
        self.module.configure_bridge_config(
            {
                "channels": {
                    "feishu": {
                        "extra": {
                            "app_id": "cli_default",
                            "app_secret": "secret_default",
                            "connection_mode": "webhook",
                            "webhook_path": "/default-hook",
                            "accounts": {
                                "work": {
                                    "extra": {
                                        "app_id": "cli_work",
                                        "app_secret": "secret_work",
                                        "webhook_port": 9988,
                                    }
                                }
                            },
                        }
                    }
                }
            }
        )

        with patch.dict(os.environ, {}, clear=True):
            config = self.module._bridge_config()

        feishu = config["channels"]["feishu"]
        self.assertNotIn("extra", feishu)
        self.assertEqual(feishu["appId"], "cli_default")
        self.assertEqual(feishu["appSecret"], "secret_default")
        self.assertEqual(feishu["connectionMode"], "webhook")
        self.assertEqual(feishu["webhookPath"], "/default-hook")
        self.assertNotIn("extra", feishu["accounts"]["work"])
        self.assertEqual(feishu["accounts"]["work"]["appId"], "cli_work")
        self.assertEqual(
            feishu["accounts"]["work"]["appSecret"],
            "secret_work",
        )
        self.assertEqual(feishu["accounts"]["work"]["webhookPort"], 9988)

    def test_accounts_only_bridge_ignores_single_account_env(self) -> None:
        """Global single-account env cannot create or retarget account children."""
        self.module.configure_bridge_config(
            {
                "channels": {
                    "feishu": {
                        "_accounts_only": True,
                        "domain": "feishu",
                        "accounts": {
                            "work": {
                                "app_id": "cli_work",
                                "app_secret": "secret_work",
                                "domain": "lark",
                            }
                        },
                    }
                }
            }
        )
        env = {
            "FEISHU_APP_ID": "cli_stale",
            "FEISHU_APP_SECRET": "secret_stale",
            "FEISHU_DOMAIN": "stale.example",
        }

        with patch.dict(os.environ, env, clear=True):
            config = self.module._bridge_config()

        feishu = config["channels"]["feishu"]
        self.assertEqual(feishu["appId"], "")
        self.assertEqual(feishu["appSecret"], "")
        self.assertEqual(feishu["domain"], "feishu")
        self.assertEqual(feishu["accounts"]["work"]["appId"], "cli_work")
        self.assertEqual(
            feishu["accounts"]["work"]["appSecret"],
            "secret_work",
        )
        self.assertEqual(feishu["accounts"]["work"]["domain"], "lark")

    def test_profile_env_switches_without_stale_process_fallback(self) -> None:
        """Bridge credentials follow the active scope and fail closed without it."""
        self.module.configure_bridge_config(
            {
                "channels": {
                    "feishu": {
                        "appId": "cli_yaml",
                        "appSecret": "secret_yaml",
                        "domain": "feishu",
                    }
                }
            }
        )
        active_scope: dict[str, dict[str, str] | None] = {
            "value": {
                "FEISHU_APP_ID": "cli_profile_a",
                "FEISHU_DOMAIN": "lark",
            }
        }
        secret_scope = ModuleType("agent.secret_scope")
        secret_scope.current_secret_scope = lambda: active_scope["value"]
        secret_scope.get_secret = lambda name, default=None: (
            (active_scope["value"] or {}).get(name, default)
        )
        secret_scope.is_multiplex_active = lambda: True
        agent = ModuleType("agent")
        agent.__path__ = []
        stale_env = {
            "FEISHU_APP_ID": "cli_stale",
            "FEISHU_APP_SECRET": "secret_stale",
            "FEISHU_DOMAIN": "stale.example",
            "FEISHU_OPENCLAW_CONFIG_JSON": json.dumps(
                {"channels": {"feishu": {"appId": "cli_stale_json"}}}
            ),
        }

        with (
            patch.dict(os.environ, stale_env, clear=True),
            patch.dict(
                sys.modules,
                {
                    "agent": agent,
                    "agent.secret_scope": secret_scope,
                },
            ),
        ):
            profile_a = self.module._bridge_config()["channels"]["feishu"]
            active_scope["value"] = {
                "FEISHU_APP_ID": "cli_profile_b",
                "FEISHU_DOMAIN": "feishu",
            }
            profile_b = self.module._bridge_config()["channels"]["feishu"]
            active_scope["value"] = None
            unscoped = self.module._bridge_config()["channels"]["feishu"]

        self.assertEqual(profile_a["appId"], "cli_profile_a")
        self.assertEqual(profile_a["appSecret"], "secret_yaml")
        self.assertEqual(profile_a["domain"], "lark")
        self.assertEqual(profile_b["appId"], "cli_profile_b")
        self.assertEqual(profile_b["appSecret"], "secret_yaml")
        self.assertEqual(profile_b["domain"], "feishu")
        self.assertEqual(unscoped["appId"], "cli_yaml")
        self.assertEqual(unscoped["appSecret"], "secret_yaml")
        self.assertEqual(unscoped["domain"], "feishu")

    def test_user_token_env_is_profile_scoped_and_fails_closed_unscoped(
        self,
    ) -> None:
        """A process-global UAT cannot cross a multiplex profile boundary."""
        active_scope: dict[str, dict[str, str] | None] = {
            "value": {
                "FEISHU_USER_ACCESS_TOKEN": "uat-profile-a",
                "FEISHU_USER_REFRESH_TOKEN": "urt-profile-a",
                "FEISHU_USER_ACCESS_TOKEN_SCOPES": "scope.a",
            }
        }
        secret_scope = ModuleType("agent.secret_scope")
        secret_scope.current_secret_scope = lambda: active_scope["value"]
        secret_scope.get_secret = lambda name, default=None: (
            (active_scope["value"] or {}).get(name, default)
        )
        secret_scope.is_multiplex_active = lambda: True
        agent = ModuleType("agent")
        agent.__path__ = []
        stale_env = {
            "FEISHU_USER_ACCESS_TOKEN": "uat-process-global",
            "FEISHU_USER_REFRESH_TOKEN": "urt-process-global",
            "FEISHU_USER_ACCESS_TOKEN_SCOPES": "scope.global",
        }

        with (
            patch.dict(os.environ, stale_env, clear=True),
            patch.dict(
                sys.modules,
                {
                    "agent": agent,
                    "agent.secret_scope": secret_scope,
                },
            ),
        ):
            profile_a = self.module._resolve_user_token(self._ticket())
            active_scope["value"] = {
                "FEISHU_USER_ACCESS_TOKEN": "uat-profile-b",
            }
            profile_b = self.module._resolve_user_token(self._ticket())
            active_scope["value"] = None
            unscoped = self.module._resolve_user_token(self._ticket())

        self.assertIsNotNone(profile_a)
        self.assertEqual(profile_a.access_token, "uat-profile-a")
        self.assertEqual(profile_a.refresh_token, "urt-profile-a")
        self.assertEqual(profile_a.scope, "scope.a")
        self.assertIsNotNone(profile_b)
        self.assertEqual(profile_b.access_token, "uat-profile-b")
        self.assertEqual(profile_b.refresh_token, "")
        self.assertIsNone(unscoped)

    def test_bridge_snapshots_are_isolated_by_hermes_profile_home(self) -> None:
        """One profile reload cannot replace another profile's tool policy."""
        active_home = {"value": Path("/profiles/alpha")}
        constants = ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: active_home["value"]

        with (
            patch.dict(
                sys.modules,
                {"hermes_constants": constants},
            ),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.module.configure_bridge_config(
                {
                    "channels": {
                        "feishu": {
                            "appId": "cli_alpha",
                            "tools": {"deny": ["terminal"]},
                        }
                    }
                }
            )
            active_home["value"] = Path("/profiles/beta")
            self.module.configure_bridge_config(
                {
                    "channels": {
                        "feishu": {
                            "appId": "cli_beta",
                            "tools": {"deny": ["browser"]},
                        }
                    }
                }
            )

            active_home["value"] = Path("/profiles/alpha")
            alpha = self.module._bridge_config()["channels"]["feishu"]
            active_home["value"] = Path("/profiles/beta")
            beta = self.module._bridge_config()["channels"]["feishu"]
            active_home["value"] = Path("/profiles/missing")
            missing = self.module._bridge_config()["channels"]["feishu"]

        self.assertEqual(alpha["appId"], "cli_alpha")
        self.assertEqual(alpha["tools"]["deny"], ["terminal"])
        self.assertEqual(beta["appId"], "cli_beta")
        self.assertEqual(beta["tools"]["deny"], ["browser"])
        self.assertEqual(missing["appId"], "")
        self.assertNotIn("tools", missing)

    def test_explicit_env_config_overrides_runtime_snapshot(self) -> None:
        """The operator-owned JSON override remains the highest precedence."""
        self.module.configure_bridge_config(
            {
                "channels": {
                    "feishu": {
                        "appId": "cli_snapshot",
                        "tools": {"deny": ["feishu_get_user"]},
                    }
                }
            }
        )
        os.environ["FEISHU_OPENCLAW_CONFIG_JSON"] = json.dumps(
            {
                "channels": {
                    "feishu": {
                        "appId": "cli_json",
                        "tools": {"deny": ["terminal"]},
                    }
                }
            }
        )

        with patch.dict(
            os.environ,
            {"FEISHU_APP_ID": "cli_operator"},
            clear=False,
        ):
            self.assertEqual(
                self.module._bridge_config()["channels"]["feishu"]["appId"],
                "cli_json",
            )
            self.assertIsNone(
                self.module.evaluate_tool_policy(
                    "feishu_get_user",
                    self._ticket(),
                )
            )
            self.assertEqual(
                self.module.evaluate_tool_policy("terminal", self._ticket()),
                "channel_deny",
            )

    def test_group_tool_policy_is_case_insensitive_and_deny_first(self) -> None:
        """Group deny wins and a nonempty allow list denies unlisted tools."""
        os.environ["FEISHU_OPENCLAW_CONFIG_JSON"] = json.dumps(
            {
                "channels": {
                    "feishu": {
                        "groups": {
                            "*": {
                                "tools": {
                                    "deny": ["feishu_calendar_event"],
                                }
                            },
                            "OC_GrOuP": {
                                "tools": {
                                    "allow": ["terminal", "feishu_calendar_*"],
                                    "deny": ["terminal"],
                                }
                            }
                        }
                    }
                }
            }
        )
        group_ticket = self._ticket(
            chat_id="oc_group",
            chat_type="group",
        )
        direct_ticket = self._ticket(
            chat_id="oc_group",
            chat_type="p2p",
        )

        self.assertEqual(
            self.module.evaluate_tool_policy("terminal", group_ticket),
            "group_deny",
        )
        self.assertEqual(
            self.module.evaluate_tool_policy("browser", group_ticket),
            "group_allowlist",
        )
        self.assertIsNone(
            self.module.evaluate_tool_policy(
                "feishu_calendar_event",
                group_ticket,
            )
        )
        self.assertIsNone(
            self.module.evaluate_tool_policy("browser", direct_ticket)
        )

    def test_internal_invoke_self_checks_policy_before_bridge(self) -> None:
        """Denied tools return the stable error without entering Node."""
        os.environ["FEISHU_OPENCLAW_CONFIG_JSON"] = json.dumps(
            {
                "channels": {
                    "feishu": {
                        "tools": {"deny": ["feishu_get_user"]},
                    }
                }
            }
        )
        with patch.object(self.module, "_run_bridge") as run_bridge:
            result = json.loads(
                self.module.invoke_openclaw_tool(
                    "feishu_get_user",
                    {},
                    ticket=self._ticket(),
                )
            )

        self.assertEqual(result["error"]["code"], "tool_policy_denied")
        self.assertEqual(result["error"]["reason"], "channel_deny")
        run_bridge.assert_not_called()

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_bundle_contains_all_37_one_shot_tools(self) -> None:
        """The self-contained artifact registers every non-daemon tool."""
        request = {
            "action": "list",
            "config": {
                "channels": {
                    "feishu": {
                        "enabled": True,
                        "appId": "test",
                        "appSecret": "test",
                    }
                },
                "plugins": {"entries": {"feishu": {"enabled": False}}},
            },
        }
        completed = subprocess.run(
            ["node", str(BUNDLE_PATH)],
            input=json.dumps(request),
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        response = json.loads(completed.stdout)
        self.assertTrue(response["ok"])
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            response["result"],
            [item["name"] for item in manifest["tools"][:37]],
        )

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_bundle_defaults_tokens_to_hermes_home(self) -> None:
        """The default credential store is private to Hermes and this plugin."""
        token = {
            "userOpenId": "ou_hermes_home",
            "appId": "cli_hermes_home",
            "accessToken": "default-store-access-token",
            "refreshToken": "default-store-refresh-token",
            "expiresAt": 1_900_000_000_000,
            "refreshExpiresAt": 1_910_000_000_000,
            "scope": "offline_access",
            "grantedAt": 1_800_000_000_000,
        }

        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "HERMES_HOME": directory}
            environment.pop("HERMES_LARK_TOKEN_STORE_DIR", None)
            completed = subprocess.run(
                ["node", str(BUNDLE_PATH)],
                input=json.dumps(
                    {
                        "action": "token_set",
                        "config": {},
                        "storedToken": token,
                    }
                ),
                capture_output=True,
                check=False,
                text=True,
                timeout=10,
                env=environment,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(json.loads(completed.stdout)["ok"])
            token_directory = Path(directory) / "hermes-lark" / "tokens"
            self.assertTrue((token_directory / "master.key").is_file())
            raw_store = b"".join(
                path.read_bytes()
                for path in token_directory.iterdir()
                if path.is_file()
            )
            self.assertNotIn(token["accessToken"].encode(), raw_store)
            self.assertNotIn(token["refreshToken"].encode(), raw_store)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_bundle_persists_encrypted_tokens_across_processes(self) -> None:
        """The internal protocol safely gets, sets, and removes credentials."""
        token = {
            "userOpenId": "ou_persistent",
            "appId": "cli_persistent",
            "accessToken": "access-token-secret-value",
            "refreshToken": "refresh-token-secret-value",
            "expiresAt": 1_900_000_000_000,
            "refreshExpiresAt": 1_910_000_000_000,
            "scope": "calendar:calendar offline_access",
            "grantedAt": 1_800_000_000_000,
        }

        with tempfile.TemporaryDirectory() as directory:
            environment = {
                **os.environ,
                "HERMES_LARK_TOKEN_STORE_DIR": directory,
            }

            def invoke(request: dict[str, Any]) -> dict[str, Any]:
                """Execute one isolated token-store bridge process."""
                completed = subprocess.run(
                    ["node", str(BUNDLE_PATH)],
                    input=json.dumps({"config": {}, **request}),
                    capture_output=True,
                    check=False,
                    text=True,
                    timeout=10,
                    env=environment,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                response = json.loads(completed.stdout)
                self.assertTrue(response["ok"], response)
                return response["result"]

            self.assertTrue(
                invoke(
                    {
                        "action": "token_set",
                        "storedToken": token,
                    }
                )["stored"]
            )
            fetched = invoke(
                {
                    "action": "token_get",
                    "credential": {
                        "appId": token["appId"],
                        "userOpenId": token["userOpenId"],
                    },
                }
            )
            self.assertTrue(fetched["found"])
            self.assertEqual(fetched["token"], token)

            raw_store = b"".join(
                path.read_bytes()
                for path in Path(directory).iterdir()
                if path.is_file()
            )
            self.assertNotIn(token["accessToken"].encode(), raw_store)
            self.assertNotIn(token["refreshToken"].encode(), raw_store)

            self.assertTrue(
                invoke(
                    {
                        "action": "token_remove",
                        "credential": {
                            "appId": token["appId"],
                            "userOpenId": token["userOpenId"],
                        },
                    }
                )["removed"]
            )
            self.assertFalse(
                invoke(
                    {
                        "action": "token_get",
                        "credential": {
                            "appId": token["appId"],
                            "userOpenId": token["userOpenId"],
                        },
                    }
                )["found"]
            )

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_bundle_serializes_rotating_refreshes_across_workers(self) -> None:
        """Concurrent one-shot workers consume a rotating refresh token once."""
        refresh_started = threading.Event()
        release_refresh = threading.Event()
        second_application_request = threading.Event()
        state_lock = threading.Lock()
        state = {"application_requests": 0, "refresh_requests": 0}
        required_scopes = [
            "im:chat:read",
            "im:message:readonly",
            "im:message.group_msg:get_as_user",
            "im:message.p2p_msg:get_as_user",
            "contact:contact.base:readonly",
            "contact:user.base:readonly",
            "offline_access",
        ]

        class RefreshHandler(BaseHTTPRequestHandler):
            """Serve the Open Platform calls made before the test tool returns."""

            def log_message(self, _format: str, *args: Any) -> None:
                """Keep the unit-test output free of HTTP access logs."""

            def do_GET(self) -> None:
                """Handle one mocked GET request."""
                self._respond()

            def do_POST(self) -> None:
                """Handle one mocked POST request."""
                self._respond()

            def _respond(self) -> None:
                """Return the minimum valid response for each requested path."""
                content_length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(content_length)
                if "tenant_access_token" in self.path:
                    payload: dict[str, Any] = {
                        "code": 0,
                        "tenant_access_token": "tenant-token",
                        "expire": 7200,
                    }
                elif "/application/v6/applications/" in self.path:
                    with state_lock:
                        state["application_requests"] += 1
                        if state["application_requests"] >= 2:
                            second_application_request.set()
                    payload = {
                        "code": 0,
                        "data": {
                            "app": {
                                "scopes": [
                                    {
                                        "scope": scope,
                                        "token_types": ["user"],
                                    }
                                    for scope in required_scopes
                                ],
                                "owner": {
                                    "owner_id": "ou_refresh_user",
                                    "owner_type": 2,
                                },
                            }
                        },
                    }
                elif "/authen/v2/oauth/token" in self.path:
                    with state_lock:
                        state["refresh_requests"] += 1
                    refresh_started.set()
                    release_refresh.wait(timeout=10)
                    payload = {
                        "code": 0,
                        "access_token": "rotated-access",
                        "refresh_token": "rotated-refresh",
                        "expires_in": 7200,
                        "refresh_token_expires_in": 604800,
                        "scope": " ".join(required_scopes),
                    }
                else:
                    payload = {
                        "code": 0,
                        "data": {"items": [], "has_more": False},
                    }
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), RefreshHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        with (
            tempfile.TemporaryDirectory() as store_directory,
            tempfile.TemporaryDirectory() as lock_directory,
        ):
            environment = {
                **os.environ,
                "HERMES_LARK_TOKEN_STORE_DIR": store_directory,
                "HERMES_LARK_UAT_LOCK_DIR": lock_directory,
            }
            now = int(time.time() * 1000)
            stored_token = {
                "userOpenId": "ou_refresh_user",
                "appId": "cli_refresh",
                "accessToken": "old-access",
                "refreshToken": "old-refresh",
                "expiresAt": now + 1000,
                "refreshExpiresAt": now + 24 * 60 * 60 * 1000,
                "scope": " ".join(required_scopes),
                "grantedAt": now - 1000,
            }
            config = {
                "channels": {
                    "feishu": {
                        "enabled": True,
                        "appId": "cli_refresh",
                        "appSecret": "test-secret",
                        "domain": f"http://127.0.0.1:{server.server_port}",
                    }
                },
                "plugins": {"entries": {"feishu": {"enabled": False}}},
            }

            def bridge_request(request: dict[str, Any]) -> dict[str, Any]:
                """Run one complete bridge request and decode its response."""
                completed = subprocess.run(
                    ["node", str(BUNDLE_PATH)],
                    input=json.dumps({"config": config, **request}),
                    capture_output=True,
                    check=False,
                    text=True,
                    timeout=15,
                    env=environment,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                return json.loads(completed.stdout)

            seeded = bridge_request(
                {"action": "token_set", "storedToken": stored_token}
            )
            self.assertTrue(seeded["ok"])
            invocation = {
                "action": "invoke",
                "tool": "feishu_im_user_get_messages",
                "arguments": {"chat_id": "oc_refresh"},
                "ticket": {
                    "messageId": "om_refresh",
                    "chatId": "oc_refresh",
                    "accountId": "default",
                    "senderOpenId": "ou_refresh_user",
                    "chatType": "p2p",
                },
            }
            results: list[dict[str, Any]] = []
            failures: list[BaseException] = []

            def invoke_worker() -> None:
                """Capture a bridge result without losing thread failures."""
                try:
                    results.append(bridge_request(invocation))
                except BaseException as error:
                    failures.append(error)

            first = threading.Thread(target=invoke_worker)
            second: threading.Thread | None = None
            try:
                first.start()
                self.assertTrue(refresh_started.wait(timeout=10))
                lock_entries = list(Path(lock_directory).iterdir())
                self.assertEqual(len(lock_entries), 1)
                self.assertNotIn("cli_refresh", lock_entries[0].name)
                self.assertNotIn("ou_refresh_user", lock_entries[0].name)
                owner_record = (lock_entries[0] / "owner.json").read_text(
                    encoding="utf-8"
                )
                self.assertNotIn("old-access", owner_record)
                self.assertNotIn("old-refresh", owner_record)
                second = threading.Thread(target=invoke_worker)
                second.start()
                self.assertTrue(second_application_request.wait(timeout=10))
                time.sleep(0.2)
                with state_lock:
                    self.assertEqual(state["refresh_requests"], 1)
            finally:
                release_refresh.set()
                if first.ident is not None:
                    first.join(timeout=15)
                if second is not None and second.ident is not None:
                    second.join(timeout=15)
            self.assertFalse(first.is_alive())
            self.assertIsNotNone(second)
            assert second is not None
            self.assertFalse(second.is_alive())
            if failures:
                raise failures[0]

            self.assertEqual(len(results), 2)
            self.assertTrue(all(result["ok"] for result in results))
            with state_lock:
                self.assertEqual(state["refresh_requests"], 1)
            fetched = bridge_request(
                {
                    "action": "token_get",
                    "credential": {
                        "appId": "cli_refresh",
                        "userOpenId": "ou_refresh_user",
                    },
                }
            )
            self.assertEqual(
                fetched["result"]["token"]["accessToken"],
                "rotated-access",
            )
            self.assertEqual(
                fetched["result"]["token"]["refreshToken"],
                "rotated-refresh",
            )


if __name__ == "__main__":
    unittest.main()
