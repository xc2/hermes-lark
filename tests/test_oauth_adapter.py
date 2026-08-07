"""Behavioral tests for live OAuth and app-permission continuations."""

from __future__ import annotations

import asyncio
import contextlib
import io
import sys
import threading
import unittest
from types import SimpleNamespace
from typing import Any, Sequence

from tests.test_ask_user_question_adapter import (
    _FakeCallbackValue,
    _MISSING_MODULE,
    _load_modules,
)


class _FakeScopePlan:
    """Small scope-plan object matching the adapter-facing runtime contract."""

    def __init__(
        self,
        scopes: Sequence[str],
        *,
        available: Sequence[str] | None = None,
        total: int | None = None,
        already: int = 0,
        unavailable: Sequence[str] = (),
    ) -> None:
        self.available_scopes = tuple(scopes if available is None else available)
        self.scopes_to_authorize = tuple(scopes)
        self.already_granted_scopes = ()
        self.unavailable_scopes = tuple(unavailable)
        self.total_app_scopes = len(scopes) if total is None else total
        self.total = len(scopes) if total is None else total
        self.already = already
        self.missing = len(scopes)
        self.complete = not scopes
        self.scope = " ".join(scopes)


class _FakeOAuthRuntime:
    """Deterministic OAuth runtime used by adapter lifecycle tests."""

    def __init__(
        self,
        plan: _FakeScopePlan,
        *,
        poll_result: Any | None = None,
        complete_error: BaseException | None = None,
        poll_gate: asyncio.Event | None = None,
        refresh_result: Any | None = None,
    ) -> None:
        self.plan = plan
        self.poll_result = poll_result or SimpleNamespace(
            ok=True,
            token=SimpleNamespace(scope=plan.scope),
            message="",
        )
        self.complete_error = complete_error
        self.poll_gate = poll_gate
        self.refresh_result = refresh_result
        self.plan_calls: list[tuple[Any, str, list[str], bool]] = []
        self.refresh_calls: list[str] = []
        self.requested_device_scopes: list[str] = []

    async def plan_authorization(
        self,
        application: Any,
        sender: str,
        requested: Sequence[str],
        *,
        is_batch: bool,
    ) -> Any:
        """Record and return the configured scope plan."""
        self.plan_calls.append(
            (application, sender, list(requested), is_batch)
        )
        return self.plan

    async def get_valid_token(self, sender: str) -> None:
        """Represent an account without an existing reusable token."""
        return None

    async def refresh(self, sender: str) -> Any | None:
        """Record one authoritative refresh of an existing user grant."""
        self.refresh_calls.append(sender)
        return self.refresh_result

    async def request_device_authorization(self, scope: str) -> Any:
        """Return one bounded Device Flow challenge."""
        self.requested_device_scopes.append(scope)
        return SimpleNamespace(
            device_code="device",
            user_code="user",
            verification_uri="https://accounts.example/verify",
            verification_uri_complete="https://accounts.example/verify?code=user",
            expires_in=240,
            interval=1,
        )

    async def poll_device_token(self, authorization: Any) -> Any:
        """Wait when requested, then return the configured terminal result."""
        if self.poll_gate is not None:
            await self.poll_gate.wait()
        return self.poll_result

    async def complete_authorization(self, sender: str, grant: Any) -> Any:
        """Persist a safe token surrogate or raise the configured error."""
        if self.complete_error is not None:
            raise self.complete_error
        return SimpleNamespace(scope=str(getattr(grant, "scope", "") or "scope.a"))


class _RevokedOAuthRuntime(_FakeOAuthRuntime):
    """Expose a stale complete plan until remote refresh clears it."""

    def __init__(self) -> None:
        super().__init__(
            _FakeScopePlan(
                (),
                available=("scope.a",),
                total=1,
                already=1,
            )
        )
        self.events: list[str] = []

    async def plan_authorization(
        self,
        application: Any,
        sender: str,
        requested: Sequence[str],
        *,
        is_batch: bool,
    ) -> Any:
        """Record which side of refresh produced the current plan."""
        self.events.append("plan")
        return await super().plan_authorization(
            application,
            sender,
            requested,
            is_batch=is_batch,
        )

    async def refresh(self, sender: str) -> Any | None:
        """Model remote revocation by clearing the stale local grant."""
        self.events.append("refresh")
        self.plan = _FakeScopePlan(["scope.a"], total=1)
        return await super().refresh(sender)


class OAuthAdapterTests(unittest.IsolatedAsyncioTestCase):
    """Verify OAuth polling, permission callbacks, and security boundaries."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.previous_oauth_runtime = sys.modules.pop(
            "hermes_lark.oauth_runtime",
            _MISSING_MODULE,
        )
        cls.tools, cls.adapter_module, cls.previous_modules = _load_modules()
        cls.adapter_module.P2CardActionTriggerResponse = _FakeCallbackValue
        cls.adapter_module.CallBackToast = _FakeCallbackValue
        cls.adapter_module.CallBackCard = _FakeCallbackValue
        from hermes_lark import oauth_runtime

        cls.oauth_runtime = oauth_runtime

    @classmethod
    def tearDownClass(cls) -> None:
        sys.modules.pop("hermes_lark.oauth_runtime", None)
        if cls.previous_oauth_runtime is not _MISSING_MODULE:
            sys.modules["hermes_lark.oauth_runtime"] = cls.previous_oauth_runtime
        for name, previous in cls.previous_modules.items():
            if previous is _MISSING_MODULE:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    async def asyncSetUp(self) -> None:
        with self.tools._state_lock:
            for timer in self.tools._interaction_expiry_timers.values():
                timer.cancel()
            self.tools._pending_interactions.clear()
            self.tools._interaction_hosts.clear()
            self.tools._interaction_expiry_hosts.clear()
            self.tools._interaction_expiry_timers.clear()

    def test_device_authorization_qr_uses_installed_renderer(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rendered = self.adapter_module._render_qr(
                "https://accounts.example/verify?code=user"
            )

        self.assertTrue(rendered)
        self.assertGreater(len(output.getvalue().strip()), 100)

    def _new_adapter(self) -> Any:
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter._account_id = "work"
        adapter._namespace_account = False
        adapter._app_id = "cli_app"
        adapter._app_secret = "secret"
        adapter._domain_name = "feishu"
        adapter._client = object()
        adapter._loop = asyncio.get_running_loop()
        adapter._openclaw_interaction_host = adapter._begin_openclaw_interaction
        adapter._openclaw_submitted_tokens = set()
        adapter._openclaw_interaction_messages = {}
        adapter._openclaw_oauth_tasks = {}
        adapter._openclaw_oauth_flow_tokens = {}
        adapter._openclaw_oauth_flow_scopes = {}
        adapter._openclaw_submitted_lock = threading.Lock()
        adapter.platform = self.adapter_module.Platform.FEISHU
        return adapter

    def _store_interaction(
        self,
        kind: str,
        *,
        scopes: Sequence[str] = (),
        oauth_intent: str | None = None,
        sender: str = "ou_owner",
        sender_user_id: str = "u_owner",
        chat_id: str = "oc_chat",
    ) -> Any:
        ticket = self.tools.ToolTicket(
            session_id=f"session-{kind}",
            message_id=f"om_{kind}",
            chat_id=chat_id,
            account_id="work",
            sender_open_id=sender,
            sender_user_id=sender_user_id,
            chat_type="p2p",
            thread_id="omt_thread",
            session_thread_id=f"om_{kind}",
        )
        context: dict[str, Any] = {
            "authorization": {
                "scopes": list(scopes),
                "app_id": "cli_app",
            },
        }
        if kind == "oauth_batch_auth":
            context["oauth_intent"] = oauth_intent or "resume"
        return self.tools._store_interaction(
            kind,
            "feishu_calendar_event",
            {},
            ticket,
            900,
            context=context,
        )

    def _install_delivery_stubs(self, adapter: Any) -> tuple[list[Any], list[Any]]:
        sent: list[Any] = []
        updates: list[Any] = []

        async def send_with_retry(**kwargs: Any) -> Any:
            sent.append(kwargs)
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id=f"om_card_{len(sent)}"),
            )

        async def update_card(token: str, card: dict[str, Any]) -> bool:
            updates.append((token, card))
            return True

        adapter._feishu_send_with_retry = send_with_retry
        adapter._update_openclaw_interaction_card = update_card
        return sent, updates

    def _install_synthetic_stubs(self, adapter: Any) -> list[Any]:
        captured: list[Any] = []

        async def resolve_sender(sender_id: Any) -> dict[str, str]:
            return {
                "user_id": sender_id.open_id,
                "user_name": "Owner",
                "user_id_alt": sender_id.open_id,
            }

        async def get_chat_info(chat_id: str) -> dict[str, str]:
            return {"name": "Chat", "type": "dm"}

        async def dispatch(message: Any) -> None:
            captured.append(message)

        adapter._resolve_sender_profile = resolve_sender
        adapter.get_chat_info = get_chat_info
        adapter.build_source = lambda chat_id, **kwargs: SimpleNamespace(
            chat_id=chat_id,
            **kwargs,
        )
        adapter._resolve_source_chat_type = lambda **kwargs: "dm"
        adapter._resolve_channel_prompt = lambda *args: None
        adapter._admit_synthetic_user_action = (
            lambda *args, **kwargs: SimpleNamespace(chat_type="p2p")
        )
        adapter._role_authorized_for_admitted_message = lambda _message: True
        adapter._handle_message_with_guards = dispatch
        return captured

    async def test_normal_oauth_success_updates_resumes_and_injects(self) -> None:
        interaction = self._store_interaction("oauth", scopes=["scope.a"])
        adapter = self._new_adapter()
        runtime = _FakeOAuthRuntime(_FakeScopePlan(["scope.a"]))
        sent, updates = self._install_delivery_stubs(adapter)
        captured = self._install_synthetic_stubs(adapter)
        application = SimpleNamespace(
            effective_owner_open_id="ou_owner",
            user_scopes=("scope.a",),
        )

        async def fetch_application() -> tuple[Any, frozenset[str]]:
            return application, frozenset({"scope.a", "offline_access"})

        adapter._fetch_openclaw_application_info = fetch_application
        adapter._create_openclaw_oauth_runtime = lambda: runtime

        started = await adapter._start_openclaw_oauth_interaction(interaction)
        task = adapter._openclaw_oauth_tasks[interaction.token]
        await task

        self.assertTrue(started)
        self.assertEqual(runtime.requested_device_scopes, ["scope.a"])
        self.assertEqual(runtime.plan_calls[0][2], ["scope.a"])
        self.assertEqual(sent[0]["reply_to"], "om_oauth")
        self.assertEqual(
            __import__("json").loads(sent[0]["payload"])["header"]["template"],
            "blue",
        )
        self.assertEqual(updates[-1][1]["header"]["template"], "green")
        self.assertEqual(len(captured), 1)
        self.assertEqual(
            captured[0].text,
            "I have authorized my Feishu account. Please continue the previous "
            "operation.",
        )
        self.assertEqual(captured[0].message_id, "om_oauth:auth-complete")
        self.assertIsNone(self.tools.get_pending_interaction(interaction.token))
        self.assertEqual(adapter._openclaw_oauth_flow_tokens, {})

    async def test_failed_tool_oauth_reauthorizes_before_resuming_session(
        self,
    ) -> None:
        """A failed user tool must not trust its stale locally granted scope."""
        interaction = self._store_interaction("oauth", scopes=["scope.a"])
        adapter = self._new_adapter()
        runtime = _FakeOAuthRuntime(
            _FakeScopePlan(
                (),
                available=("scope.a",),
                total=1,
                already=1,
            )
        )
        self._install_delivery_stubs(adapter)
        captured = self._install_synthetic_stubs(adapter)
        adapter._fetch_openclaw_application_info = self._application_fetch(
            user_scopes=("scope.a",)
        )
        adapter._create_openclaw_oauth_runtime = lambda: runtime

        started = await adapter._start_openclaw_oauth_interaction(interaction)
        poll_task = adapter._openclaw_oauth_tasks[interaction.token]
        await poll_task

        self.assertTrue(started)
        self.assertEqual(runtime.requested_device_scopes, ["scope.a"])
        self.assertEqual(len(captured), 1)
        self.assertEqual(
            captured[0].text,
            "I have authorized my Feishu account. Please continue the previous "
            "operation.",
        )

    async def test_identity_mismatch_fails_closed_without_injection(self) -> None:
        interaction = self._store_interaction("oauth", scopes=["scope.a"])
        adapter = self._new_adapter()
        mismatch = self.oauth_runtime.OAuthIdentityMismatchError(
            "ou_owner",
            "ou_other",
        )
        runtime = _FakeOAuthRuntime(
            _FakeScopePlan(["scope.a"]),
            complete_error=mismatch,
        )
        _sent, updates = self._install_delivery_stubs(adapter)
        captured = self._install_synthetic_stubs(adapter)
        adapter._fetch_openclaw_application_info = self._application_fetch(
            user_scopes=("scope.a",)
        )
        adapter._create_openclaw_oauth_runtime = lambda: runtime

        await adapter._start_openclaw_oauth_interaction(interaction)
        await adapter._openclaw_oauth_tasks[interaction.token]

        self.assertEqual(updates[-1][1]["header"]["template"], "red")
        self.assertEqual(captured, [])
        self.assertIsNone(self.tools.get_pending_interaction(interaction.token))

    async def test_application_lookup_failure_cancels_oauth_fail_closed(
        self,
    ) -> None:
        interaction = self._store_interaction("oauth", scopes=["scope.a"])
        adapter = self._new_adapter()
        runtime = _FakeOAuthRuntime(_FakeScopePlan(["scope.a"]))
        sent, updates = self._install_delivery_stubs(adapter)

        async def fail_application() -> tuple[Any, frozenset[str]]:
            raise RuntimeError("application info denied")

        adapter._fetch_openclaw_application_info = fail_application
        adapter._create_openclaw_oauth_runtime = lambda: runtime

        started = await adapter._start_openclaw_oauth_interaction(interaction)

        self.assertFalse(started)
        self.assertEqual(updates, [])
        self.assertEqual(len(sent), 1)
        card = __import__("json").loads(sent[0]["payload"])
        self.assertEqual(card["header"]["template"], "orange")
        self.assertIn(
            "application:application:self_manage",
            card["body"]["elements"][0]["content"],
        )
        links = [
            node["multi_url"]["url"]
            for node in self._walk_dicts(card)
            if "multi_url" in node
        ]
        self.assertIn(
            "q=application%3Aapplication%3Aself_manage",
            links[0],
        )
        self.assertIn("token_type=tenant", links[0])
        self.assertEqual(runtime.requested_device_scopes, [])
        self.assertIsNone(self.tools.get_pending_interaction(interaction.token))
        self.assertEqual(adapter._openclaw_oauth_flow_tokens, {})

    async def test_missing_offline_access_blocks_device_flow_with_link(
        self,
    ) -> None:
        """A nonempty app scope set must expose the Device Flow prerequisite."""
        interaction = self._store_interaction(
            "oauth_batch_auth",
            oauth_intent="standalone",
        )
        adapter = self._new_adapter()
        runtime = _FakeOAuthRuntime(_FakeScopePlan(["scope.a"]))
        sent, updates = self._install_delivery_stubs(adapter)
        application = SimpleNamespace(
            effective_owner_open_id="ou_owner",
            user_scopes=("scope.a",),
        )

        async def fetch_application() -> tuple[Any, frozenset[str]]:
            return application, frozenset({"scope.a"})

        adapter._fetch_openclaw_application_info = fetch_application
        adapter._create_openclaw_oauth_runtime = lambda: runtime

        started = await adapter._start_openclaw_oauth_interaction(interaction)

        self.assertFalse(started)
        self.assertEqual(len(runtime.plan_calls), 1)
        self.assertEqual(runtime.requested_device_scopes, [])
        self.assertEqual(updates, [])
        self.assertEqual(len(sent), 1)
        card = __import__("json").loads(sent[0]["payload"])
        self.assertEqual(card["header"]["template"], "orange")
        self.assertIn(
            "offline_access",
            card["body"]["elements"][0]["content"],
        )
        links = [
            node["multi_url"]["url"]
            for node in self._walk_dicts(card)
            if "multi_url" in node
        ]
        self.assertIn("q=offline_access", links[0])
        self.assertIn("token_type=user", links[0])
        self.assertIsNone(self.tools.get_pending_interaction(interaction.token))

    async def test_poll_failure_marks_card_and_cancels_continuation(self) -> None:
        interaction = self._store_interaction("oauth", scopes=["scope.a"])
        adapter = self._new_adapter()
        runtime = _FakeOAuthRuntime(
            _FakeScopePlan(["scope.a"]),
            poll_result=SimpleNamespace(
                ok=False,
                token=None,
                message="The authorization code expired. Please start again.",
            ),
        )
        _sent, updates = self._install_delivery_stubs(adapter)
        adapter._fetch_openclaw_application_info = self._application_fetch(
            user_scopes=("scope.a",)
        )
        adapter._create_openclaw_oauth_runtime = lambda: runtime

        await adapter._start_openclaw_oauth_interaction(interaction)
        await adapter._openclaw_oauth_tasks[interaction.token]

        self.assertEqual(updates[-1][1]["header"]["template"], "yellow")
        self.assertIsNone(self.tools.get_pending_interaction(interaction.token))

    async def test_batch_uses_live_app_plan_and_never_requests_blank_scope(self) -> None:
        interaction = self._store_interaction("oauth_batch_auth")
        adapter = self._new_adapter()
        runtime = _FakeOAuthRuntime(
            _FakeScopePlan(["scope.a", "scope.b"], total=3),
            poll_result=SimpleNamespace(ok=False, token=None, message="expired"),
        )
        self._install_delivery_stubs(adapter)
        application = SimpleNamespace(
            effective_owner_open_id="ou_owner",
            user_scopes=("scope.a", "scope.b", "scope.c"),
        )

        async def fetch_application() -> tuple[Any, frozenset[str]]:
            return application, frozenset(
                (*application.user_scopes, "offline_access")
            )

        adapter._fetch_openclaw_application_info = fetch_application
        adapter._create_openclaw_oauth_runtime = lambda: runtime

        await adapter._start_openclaw_oauth_interaction(interaction)
        await adapter._openclaw_oauth_tasks[interaction.token]

        self.assertIs(runtime.plan_calls[0][0], application)
        self.assertEqual(runtime.plan_calls[0][2], [])
        self.assertTrue(runtime.plan_calls[0][3])
        self.assertEqual(
            runtime.requested_device_scopes,
            ["scope.a scope.b"],
        )

    async def test_command_auth_refreshes_remote_and_does_not_resume_session(
        self,
    ) -> None:
        """A standalone auth command must verify remotely without resuming work."""
        interaction = self._store_interaction(
            "oauth_batch_auth",
            oauth_intent="standalone",
        )
        adapter = self._new_adapter()
        runtime = _FakeOAuthRuntime(
            _FakeScopePlan((), total=1, already=1),
            refresh_result=SimpleNamespace(scope="scope.a"),
        )
        sent, _updates = self._install_delivery_stubs(adapter)
        captured = self._install_synthetic_stubs(adapter)
        adapter._fetch_openclaw_application_info = self._application_fetch(
            user_scopes=("scope.a",)
        )
        adapter._create_openclaw_oauth_runtime = lambda: runtime

        started = await adapter._start_openclaw_oauth_interaction(interaction)

        self.assertTrue(started)
        self.assertEqual(runtime.refresh_calls, ["ou_owner"])
        self.assertEqual(len(runtime.plan_calls), 2)
        self.assertEqual(runtime.requested_device_scopes, [])
        self.assertEqual(captured, [])
        card = __import__("json").loads(sent[-1]["payload"])
        self.assertEqual(card["header"]["template"], "green")
        content = card["body"]["elements"][0]["content"]
        self.assertIn("You can now use tools", content)
        self.assertNotIn("Continuing with your request", content)
        self.assertIsNone(self.tools.get_pending_interaction(interaction.token))

    async def test_direct_batch_auth_reauthorizes_revoked_grant_before_resuming(
        self,
    ) -> None:
        """A direct OAuth tool must verify remotely before continuation."""
        interaction = self._store_interaction(
            "oauth_batch_auth",
            oauth_intent="resume",
        )
        adapter = self._new_adapter()
        runtime = _RevokedOAuthRuntime()
        _sent, updates = self._install_delivery_stubs(adapter)
        captured = self._install_synthetic_stubs(adapter)
        adapter._fetch_openclaw_application_info = self._application_fetch(
            user_scopes=("scope.a",)
        )
        adapter._create_openclaw_oauth_runtime = lambda: runtime

        started = await adapter._start_openclaw_oauth_interaction(interaction)
        await adapter._openclaw_oauth_tasks[interaction.token]

        self.assertTrue(started)
        self.assertEqual(runtime.events, ["plan", "refresh", "plan"])
        self.assertEqual(runtime.refresh_calls, ["ou_owner"])
        self.assertEqual(runtime.requested_device_scopes, ["scope.a"])
        self.assertEqual(len(captured), 1)
        self.assertIn(
            "continue the previous operation",
            captured[0].text,
        )
        content = updates[-1][1]["body"]["elements"][0]["content"]
        self.assertIn("Continuing with your request", content)
        self.assertIsNone(self.tools.get_pending_interaction(interaction.token))

    async def test_command_auth_rejects_non_owner_before_refreshing_credentials(
        self,
    ) -> None:
        """A non-owner standalone command must not mutate stored credentials."""
        owner_denied_error = self.oauth_runtime.OAuthOwnerAccessDeniedError

        class OwnerDeniedRuntime(_FakeOAuthRuntime):
            """Reject the initial authorization plan as an owner mismatch."""

            async def plan_authorization(
                self,
                application: Any,
                sender: str,
                requested: Sequence[str],
                *,
                is_batch: bool,
            ) -> Any:
                """Record the denied plan without allowing a refresh."""
                self.plan_calls.append(
                    (application, sender, list(requested), is_batch)
                )
                raise owner_denied_error("owner_mismatch")

        interaction = self._store_interaction(
            "oauth_batch_auth",
            oauth_intent="standalone",
        )
        adapter = self._new_adapter()
        runtime = OwnerDeniedRuntime(_FakeScopePlan((), total=1, already=1))
        sent, updates = self._install_delivery_stubs(adapter)
        adapter._fetch_openclaw_application_info = self._application_fetch(
            user_scopes=("scope.a",)
        )
        adapter._create_openclaw_oauth_runtime = lambda: runtime

        started = await adapter._start_openclaw_oauth_interaction(interaction)

        self.assertFalse(started)
        self.assertEqual(len(runtime.plan_calls), 1)
        self.assertEqual(runtime.refresh_calls, [])
        self.assertEqual(runtime.requested_device_scopes, [])
        self.assertEqual(updates, [])
        card = __import__("json").loads(sent[-1]["payload"])
        self.assertIn(
            "Only the app owner",
            card["body"]["elements"][0]["content"],
        )
        self.assertIsNone(self.tools.get_pending_interaction(interaction.token))

    async def test_command_auth_reauthorizes_revoked_grant_without_resuming_session(
        self,
    ) -> None:
        """A revoked standalone grant must enter Device Flow and stop there."""
        interaction = self._store_interaction(
            "oauth_batch_auth",
            oauth_intent="standalone",
        )
        adapter = self._new_adapter()
        runtime = _RevokedOAuthRuntime()
        _sent, updates = self._install_delivery_stubs(adapter)
        captured = self._install_synthetic_stubs(adapter)
        adapter._fetch_openclaw_application_info = self._application_fetch(
            user_scopes=("scope.a",)
        )
        adapter._create_openclaw_oauth_runtime = lambda: runtime

        started = await adapter._start_openclaw_oauth_interaction(interaction)
        poll_task = adapter._openclaw_oauth_tasks[interaction.token]
        await poll_task

        self.assertTrue(started)
        self.assertEqual(runtime.events, ["plan", "refresh", "plan"])
        self.assertEqual(runtime.refresh_calls, ["ou_owner"])
        self.assertEqual(runtime.requested_device_scopes, ["scope.a"])
        self.assertEqual(captured, [])
        self.assertEqual(updates[-1][1]["header"]["template"], "green")
        content = updates[-1][1]["body"]["elements"][0]["content"]
        self.assertIn("You can now use tools", content)
        self.assertNotIn("Continuing with your request", content)
        self.assertIsNone(self.tools.get_pending_interaction(interaction.token))

    async def test_interaction_host_routes_each_authorization_kind(self) -> None:
        oauth = self._store_interaction("oauth", scopes=["scope.a"])
        batch = self._store_interaction("oauth_batch_auth")
        app_permission = self._store_interaction(
            "app_permission",
            scopes=["scope.app"],
        )
        adapter = self._new_adapter()
        routed: list[tuple[str, str]] = []

        async def start(interaction: Any) -> bool:
            routed.append(("oauth", interaction.kind))
            return True

        async def send_app(interaction: Any) -> bool:
            routed.append(("app", interaction.kind))
            return True

        adapter._start_openclaw_oauth_interaction = start
        adapter._send_openclaw_app_permission_card = send_app
        loop = asyncio.get_running_loop()

        results = [
            await loop.run_in_executor(
                None,
                adapter._begin_openclaw_interaction,
                interaction,
            )
            for interaction in (oauth, batch, app_permission)
        ]

        self.assertEqual(results, [True, True, True])
        self.assertEqual(
            routed,
            [
                ("oauth", "oauth"),
                ("oauth", "oauth_batch_auth"),
                ("app", "app_permission"),
            ],
        )

    async def test_new_flow_supersedes_old_poll_and_merges_scopes(self) -> None:
        first = self._store_interaction("oauth", scopes=["scope.a"])
        second = self._store_interaction("oauth", scopes=["scope.b"])
        adapter = self._new_adapter()
        first_gate = asyncio.Event()
        second_gate = asyncio.Event()
        first_runtime = _FakeOAuthRuntime(
            _FakeScopePlan(["scope.a"]),
            poll_gate=first_gate,
        )
        second_runtime = _FakeOAuthRuntime(
            _FakeScopePlan(["scope.a", "scope.b"]),
            poll_gate=second_gate,
        )
        runtimes = iter((first_runtime, second_runtime))
        _sent, updates = self._install_delivery_stubs(adapter)
        adapter._fetch_openclaw_application_info = self._application_fetch(
            user_scopes=("scope.a", "scope.b")
        )
        adapter._create_openclaw_oauth_runtime = lambda: next(runtimes)

        await adapter._start_openclaw_oauth_interaction(first)
        first_task = adapter._openclaw_oauth_tasks[first.token]
        await adapter._start_openclaw_oauth_interaction(second)
        await asyncio.sleep(0)

        self.assertTrue(first_task.cancelled())
        self.assertIsNone(self.tools.get_pending_interaction(first.token))
        self.assertEqual(
            second_runtime.plan_calls[0][2],
            ["scope.a", "scope.b"],
        )
        self.assertTrue(
            any(
                token == first.token and card["header"]["template"] == "yellow"
                for token, card in updates
            )
        )
        second_task = adapter._openclaw_oauth_tasks[second.token]
        second_task.cancel()
        await asyncio.gather(second_task, return_exceptions=True)
        self.tools.cancel_interaction(second.token)

    async def test_app_permission_rechecks_all_scopes_not_only_user_scopes(
        self,
    ) -> None:
        interaction = self._store_interaction(
            "app_permission",
            scopes=["application:application:self_manage"],
        )
        adapter = self._new_adapter()
        _sent, updates = self._install_delivery_stubs(adapter)
        captured = self._install_synthetic_stubs(adapter)
        application = SimpleNamespace(
            effective_owner_open_id="ou_owner",
            user_scopes=(),
        )
        adapter._request_openclaw_application_info = lambda: (
            application,
            frozenset({"application:application:self_manage"}),
        )
        scheduled: list[asyncio.Task[Any]] = []

        def submit(loop: Any, coroutine: Any) -> bool:
            scheduled.append(loop.create_task(coroutine))
            return True

        adapter._submit_on_loop = submit
        event = self._app_permission_event(interaction.token)

        response = adapter._on_card_action_trigger(SimpleNamespace(event=event))
        await asyncio.gather(*scheduled)

        self.assertEqual(response.toast.type, "success")
        self.assertEqual(response.card.data["header"]["template"], "green")
        self.assertEqual(updates[-1][1]["header"]["template"], "green")
        self.assertEqual(
            captured[0].text,
            "App permissions are enabled. Please continue the previous operation.",
        )
        self.assertIsNone(self.tools.get_pending_interaction(interaction.token))

    async def test_app_permission_not_granted_keeps_pending_with_error_toast(
        self,
    ) -> None:
        interaction = self._store_interaction(
            "app_permission",
            scopes=["scope.required"],
        )
        adapter = self._new_adapter()
        application = SimpleNamespace(
            effective_owner_open_id="ou_owner",
            user_scopes=(),
        )
        adapter._request_openclaw_application_info = lambda: (
            application,
            frozenset({"scope.other"}),
        )
        adapter._submit_on_loop = lambda *args: self.fail("must not schedule")

        response = adapter._on_card_action_trigger(
            SimpleNamespace(event=self._app_permission_event(interaction.token))
        )

        self.assertEqual(response.toast.type, "error")
        self.assertIn("not enabled", response.toast.content)
        self.assertIsNotNone(
            self.tools.get_pending_interaction(interaction.token)
        )

    async def test_app_permission_card_uses_lark_domain_and_callback_token(
        self,
    ) -> None:
        adapter = self._new_adapter()
        adapter._domain_name = "lark"

        card = adapter._build_openclaw_app_permission_card(
            ["scope.required"],
            "operation-1",
        )
        nodes = self._walk_dicts(card)
        links = [node["multi_url"] for node in nodes if "multi_url" in node]
        values = [node["value"] for node in nodes if "value" in node]

        self.assertTrue(
            links[0]["url"].startswith(
                "https://open.larksuite.com/app/cli_app/auth?"
            )
        )
        self.assertIn(
            {
                "action": "app_auth_done",
                "operation_id": "operation-1",
            },
            values,
        )

    async def test_app_permission_user_scope_enters_forced_user_oauth(self) -> None:
        interaction = self._store_interaction(
            "app_permission",
            scopes=["calendar:calendar"],
        )
        adapter = self._new_adapter()
        self._install_delivery_stubs(adapter)
        application = SimpleNamespace(
            effective_owner_open_id="ou_owner",
            user_scopes=("calendar:calendar",),
        )
        adapter._request_openclaw_application_info = lambda: (
            application,
            frozenset({"calendar:calendar"}),
        )
        starts: list[Any] = []

        async def start_oauth(
            interaction_value: Any,
            *,
            requested_scopes: Sequence[str],
            force_device_flow: bool,
        ) -> bool:
            starts.append(
                (interaction_value, list(requested_scopes), force_device_flow)
            )
            return True

        adapter._start_openclaw_oauth_interaction = start_oauth
        scheduled: list[asyncio.Task[Any]] = []
        adapter._submit_on_loop = lambda loop, coroutine: (
            scheduled.append(loop.create_task(coroutine)) is None
        )

        response = adapter._on_card_action_trigger(
            SimpleNamespace(event=self._app_permission_event(interaction.token))
        )
        await asyncio.gather(*scheduled)

        self.assertEqual(response.toast.type, "success")
        self.assertEqual(starts[0][1], ["calendar:calendar"])
        self.assertTrue(starts[0][2])
        self.assertIsNotNone(
            self.tools.get_pending_interaction(interaction.token)
        )

    async def test_app_permission_handoff_uses_all_deferred_user_scopes(self) -> None:
        """App approval carries the operation's complete user scope set."""
        interaction = self._store_interaction(
            "app_permission",
            scopes=["scope.app_missing"],
        )
        authorization = interaction.context["authorization"]
        authorization["missing_scopes"] = ["scope.app_missing"]
        authorization["all_required_scopes"] = [
            "scope.app_missing",
            "scope.user_already_enabled",
        ]
        authorization["deferred_scopes"] = list(
            authorization["all_required_scopes"]
        )
        adapter = self._new_adapter()
        self._install_delivery_stubs(adapter)
        application = SimpleNamespace(
            effective_owner_open_id="ou_owner",
            user_scopes=("scope.app_missing", "scope.user_already_enabled"),
        )
        adapter._request_openclaw_application_info = lambda: (
            application,
            frozenset({"scope.app_missing", "scope.user_already_enabled"}),
        )
        starts: list[Any] = []

        async def start_oauth(
            interaction_value: Any,
            *,
            requested_scopes: Sequence[str],
            force_device_flow: bool,
        ) -> bool:
            starts.append(
                (interaction_value, list(requested_scopes), force_device_flow)
            )
            return True

        adapter._start_openclaw_oauth_interaction = start_oauth
        scheduled: list[asyncio.Task[Any]] = []
        adapter._submit_on_loop = lambda loop, coroutine: (
            scheduled.append(loop.create_task(coroutine)) is None
        )

        response = adapter._on_card_action_trigger(
            SimpleNamespace(event=self._app_permission_event(interaction.token))
        )
        await asyncio.gather(*scheduled)

        self.assertEqual(response.toast.type, "success")
        self.assertEqual(
            starts[0][1],
            ["scope.app_missing", "scope.user_already_enabled"],
        )
        self.assertTrue(starts[0][2])
        self.tools.cancel_interaction(interaction.token)

    async def test_app_permission_callback_validates_account_chat_and_operator(
        self,
    ) -> None:
        cases = [
            ("other", "oc_chat", "ou_owner", "different Feishu account"),
            ("work", "oc_other", "ou_owner", "chat where you received"),
            ("work", "oc_chat", "ou_other", "initiated the request"),
        ]
        for account, chat_id, operator, expected in cases:
            with self.subTest(account=account, chat_id=chat_id, operator=operator):
                interaction = self._store_interaction(
                    "app_permission",
                    scopes=["scope.required"],
                )
                adapter = self._new_adapter()
                adapter._account_id = account
                adapter._request_openclaw_application_info = lambda: self.fail(
                    "must not query before callback identity validation"
                )
                event = self._app_permission_event(
                    interaction.token,
                    chat_id=chat_id,
                    operator=operator,
                )

                response = adapter._on_card_action_trigger(
                    SimpleNamespace(event=event)
                )

                self.assertEqual(response.toast.type, "warning")
                self.assertIn(expected, response.toast.content)
                self.tools.cancel_interaction(interaction.token)

    async def test_app_permission_schema_two_operator_matches_ticket_user_id(
        self,
    ) -> None:
        """Schema 2 user IDs are accepted without entering the open-ID namespace."""
        interaction = self._store_interaction(
            "app_permission",
            scopes=["scope.required"],
        )
        adapter = self._new_adapter()
        application = SimpleNamespace(
            effective_owner_open_id="ou_owner",
            user_scopes=(),
        )
        adapter._request_openclaw_application_info = lambda: (
            application,
            frozenset({"scope.required"}),
        )
        scheduled: list[Any] = []
        adapter._submit_on_loop = (
            lambda _loop, coroutine: scheduled.append(coroutine) or True
        )

        response = adapter._on_card_action_trigger(
            SimpleNamespace(
                event=self._app_permission_event(
                    interaction.token,
                    operator="",
                    operator_user_id="u_owner",
                )
            )
        )

        self.assertEqual(response.toast.type, "success")
        self.assertEqual(len(scheduled), 1)
        scheduled.pop().close()
        adapter._openclaw_submitted_tokens.discard(interaction.token)
        self.tools.cancel_interaction(interaction.token)

    async def test_app_permission_schema_two_operator_stays_ticket_bound(
        self,
    ) -> None:
        """A callback user ID cannot impersonate an unrelated ticket owner."""
        interaction = self._store_interaction(
            "app_permission",
            scopes=["scope.required"],
        )
        adapter = self._new_adapter()
        adapter._request_openclaw_application_info = lambda: self.fail(
            "must not query before callback identity validation"
        )

        response = adapter._on_card_action_trigger(
            SimpleNamespace(
                event=self._app_permission_event(
                    interaction.token,
                    operator="",
                    operator_user_id="u_other",
                )
            )
        )

        self.assertEqual(response.toast.type, "warning")
        self.assertIn("initiated the request", response.toast.content)
        self.tools.cancel_interaction(interaction.token)

    async def test_app_permission_scope_need_type_matches_upstream(self) -> None:
        """Default and one need any scope while all requires every scope."""
        cases = [
            (None, None, "success"),
            ("scope_need_type", "one", "success"),
            ("scopeNeedType", "one", "success"),
            ("scope_need_type", "all", "error"),
            ("scopeNeedType", "all", "error"),
        ]
        for key, value, expected_type in cases:
            with self.subTest(key=key, value=value):
                interaction = self._store_interaction(
                    "app_permission",
                    scopes=["scope.a", "scope.b"],
                )
                authorization = interaction.context["authorization"]
                authorization["missing_scopes"] = ["scope.a", "scope.b"]
                if key is not None:
                    authorization[key] = value
                adapter = self._new_adapter()
                application = SimpleNamespace(
                    effective_owner_open_id="ou_owner",
                    user_scopes=(),
                )
                adapter._request_openclaw_application_info = lambda: (
                    application,
                    frozenset({"scope.b"}),
                )
                scheduled: list[Any] = []
                adapter._submit_on_loop = (
                    lambda _loop, coroutine: scheduled.append(coroutine) or True
                )

                response = adapter._on_card_action_trigger(
                    SimpleNamespace(
                        event=self._app_permission_event(interaction.token)
                    )
                )

                self.assertEqual(response.toast.type, expected_type)
                self.assertEqual(len(scheduled), int(expected_type == "success"))
                for coroutine in scheduled:
                    coroutine.close()
                adapter._openclaw_submitted_tokens.discard(interaction.token)
                self.tools.cancel_interaction(interaction.token)

    async def test_application_parser_preserves_all_and_user_scope_views(
        self,
    ) -> None:
        response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(
                app=SimpleNamespace(
                    creator_id="ou_creator",
                    owner=SimpleNamespace(type=2, owner_id="ou_owner"),
                    scopes=[
                        SimpleNamespace(
                            scope="scope.user",
                            token_types=["user"],
                        ),
                        SimpleNamespace(
                            scope="scope.tenant",
                            token_types=["tenant"],
                        ),
                        SimpleNamespace(
                            scope="scope.both",
                            token_types=None,
                        ),
                    ],
                )
            ),
        )

        application, all_scopes = (
            self.adapter_module.FeishuAdapter._parse_openclaw_application_response(
                response
            )
        )

        self.assertEqual(application.effective_owner_open_id, "ou_owner")
        self.assertEqual(
            tuple(application.user_scopes),
            ("scope.user", "scope.both"),
        )
        self.assertEqual(
            all_scopes,
            frozenset({"scope.user", "scope.tenant", "scope.both"}),
        )

    async def test_disconnect_cancels_oauth_poll_and_pending_interaction(
        self,
    ) -> None:
        interaction = self._store_interaction("oauth", scopes=["scope.a"])
        adapter = self._new_adapter()
        blocker = asyncio.Event()
        task = asyncio.create_task(blocker.wait())
        adapter._openclaw_oauth_tasks[interaction.token] = task
        adapter._openclaw_oauth_flow_tokens = {
            "cli_app:ou_owner": interaction.token
        }
        adapter._openclaw_oauth_flow_scopes = {
            "cli_app:ou_owner": ("scope.a",)
        }
        adapter._pending_text_batch_tasks = {}
        adapter._pending_media_batch_tasks = {}
        adapter._ws_client = None
        adapter._ws_thread_loop = None
        adapter._ws_future = None
        adapter._event_handler = None
        adapter._reset_batch_buffers = lambda: None
        adapter._disable_websocket_auto_reconnect = lambda: None
        adapter._stop_webhook_server = self._async_noop
        adapter._shutdown_sdk_executor = lambda: None
        adapter._persist_seen_message_ids = lambda: None
        adapter._release_app_lock = self._async_noop
        adapter._mark_disconnected = lambda: None

        await adapter.disconnect()

        self.assertTrue(task.cancelled())
        self.assertIsNone(self.tools.get_pending_interaction(interaction.token))
        self.assertEqual(adapter._openclaw_oauth_flow_tokens, {})
        self.assertEqual(adapter._openclaw_oauth_flow_scopes, {})

    def _application_fetch(
        self,
        *,
        user_scopes: Sequence[str],
    ) -> Any:
        application = SimpleNamespace(
            effective_owner_open_id="ou_owner",
            user_scopes=tuple(user_scopes),
        )

        async def fetch() -> tuple[Any, frozenset[str]]:
            return application, frozenset((*user_scopes, "offline_access"))

        return fetch

    @staticmethod
    def _app_permission_event(
        token: str,
        *,
        chat_id: str = "oc_chat",
        operator: str = "ou_owner",
        operator_user_id: str = "",
    ) -> Any:
        return SimpleNamespace(
            operator=SimpleNamespace(
                open_id=operator,
                user_id=operator_user_id,
            ),
            context=SimpleNamespace(open_chat_id=chat_id),
            action=SimpleNamespace(
                tag="button",
                name="",
                value={
                    "action": "app_auth_done",
                    "operation_id": token,
                },
            ),
        )

    @staticmethod
    async def _async_noop(*args: Any, **kwargs: Any) -> None:
        return None

    @staticmethod
    def _walk_dicts(value: Any) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if isinstance(value, dict):
            result.append(value)
            for nested in value.values():
                result.extend(OAuthAdapterTests._walk_dicts(nested))
        elif isinstance(value, list):
            for nested in value:
                result.extend(OAuthAdapterTests._walk_dicts(nested))
        return result


if __name__ == "__main__":
    unittest.main()
