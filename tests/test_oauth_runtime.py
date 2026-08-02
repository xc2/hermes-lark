"""Pure protocol tests for the headless Feishu OAuth runtime."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "hermes_lark" / "oauth_runtime.py"


def _load_module() -> ModuleType:
    """Load the OAuth module without importing Hermes adapter dependencies."""
    spec = importlib.util.spec_from_file_location(
        "hermes_lark_oauth_runtime_test",
        MODULE_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeHTTP:
    """Script OAuth HTTP responses and capture each request."""

    def __init__(self, module: ModuleType, *responses: Any) -> None:
        self.module = module
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Any = None,
        form: Any = None,
    ) -> Any:
        """Return the next response or raise its scripted exception."""
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "form": dict(form) if form is not None else None,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        status, payload = response
        return self.module.JsonHTTPResponse(status=status, payload=payload)


class _FakeStore:
    """In-memory token store used only as a protocol test double."""

    def __init__(self) -> None:
        self.tokens: dict[tuple[str, str], Any] = {}
        self.get_calls: list[tuple[str, str]] = []
        self.set_calls: list[Any] = []
        self.remove_calls: list[tuple[str, str]] = []

    async def get(self, app_id: str, user_open_id: str) -> Any:
        """Read one token."""
        self.get_calls.append((app_id, user_open_id))
        return self.tokens.get((app_id, user_open_id))

    async def set(self, token: Any) -> None:
        """Persist one token."""
        self.tokens[(token.app_id, token.user_open_id)] = token
        self.set_calls.append(token)

    async def remove(self, app_id: str, user_open_id: str) -> None:
        """Remove one token."""
        self.tokens.pop((app_id, user_open_id), None)
        self.remove_calls.append((app_id, user_open_id))


class OAuthRuntimeTests(unittest.IsolatedAsyncioTestCase):
    """Verify Device Flow, identity, refresh, and persistence semantics."""

    @classmethod
    def setUpClass(cls) -> None:
        """Load the runtime once for all pure tests."""
        cls.module = _load_module()

    def _account(self, brand: str = "feishu") -> Any:
        """Build one configured OAuth account."""
        return self.module.OAuthAccount(
            app_id="cli_test",
            app_secret="secret_test",
            brand=brand,
        )

    def _stored_token(self, **overrides: Any) -> Any:
        """Build one complete stored credential."""
        values = {
            "user_open_id": "ou_user",
            "app_id": "cli_test",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at": 1_100_000,
            "refresh_expires_at": 9_000_000,
            "scope": "calendar:calendar",
            "granted_at": 500_000,
        }
        values.update(overrides)
        return self.module.StoredOAuthToken(**values)

    def test_scope_planning_fails_closed_for_missing_or_wrong_owner(self) -> None:
        """Authorization cannot start without an exact effective owner."""
        missing_owner = self.module.OAuthApplicationInfo(
            effective_owner_open_id=None,
            user_scopes=("calendar:calendar",),
        )
        wrong_owner = self.module.OAuthApplicationInfo(
            effective_owner_open_id="ou_owner",
            user_scopes=("calendar:calendar",),
        )

        with self.assertRaises(
            self.module.OAuthOwnerAccessDeniedError
        ) as unavailable:
            self.module.plan_authorization_scopes(
                missing_owner,
                "ou_user",
                "calendar:calendar",
                is_batch=False,
            )
        with self.assertRaises(
            self.module.OAuthOwnerAccessDeniedError
        ) as mismatch:
            self.module.plan_authorization_scopes(
                wrong_owner,
                "ou_user",
                "calendar:calendar",
                is_batch=False,
            )

        self.assertEqual(unavailable.exception.reason, "owner_unavailable")
        self.assertEqual(mismatch.exception.reason, "owner_mismatch")
        self.assertNotIn("ou_owner", str(mismatch.exception))

    def test_batch_scope_plan_filters_sensitive_grants_and_caps_at_100(
        self,
    ) -> None:
        """Batch planning follows upstream filtering and pagination rules."""
        safe_scopes = tuple(f"scope:{index:03d}" for index in range(104))
        sensitive_scopes = (
            "im:message.send_as_user",
            "space:document:delete",
            "calendar:calendar.event:delete",
            "base:table:delete",
        )
        application = self.module.OAuthApplicationInfo(
            effective_owner_open_id="ou_owner",
            user_scopes=(
                safe_scopes[:2]
                + sensitive_scopes[:2]
                + safe_scopes[2:]
                + sensitive_scopes[2:]
            ),
        )

        plan = self.module.plan_authorization_scopes(
            application,
            "ou_owner",
            "ignored:batch-request",
            is_batch=True,
            granted_scope=f"{safe_scopes[0]} {safe_scopes[3]}",
        )

        self.assertEqual(plan.total_app_scopes, 104)
        self.assertEqual(plan.total, 104)
        self.assertEqual(plan.already, 2)
        self.assertEqual(plan.missing, 102)
        self.assertEqual(plan.batch_size, 100)
        self.assertEqual(plan.remaining, 2)
        self.assertEqual(
            plan.excluded_sensitive_scopes,
            sensitive_scopes,
        )
        self.assertTrue(
            self.module.SENSITIVE_BATCH_SCOPES.isdisjoint(
                plan.scopes_to_authorize
            )
        )
        self.assertNotIn("ignored:batch-request", plan.scope)
        self.assertEqual(
            plan.to_display_dict(),
            {
                "is_batch": True,
                "scope": plan.scope,
                "total_app_scopes": 104,
                "total": 104,
                "already": 2,
                "missing": 102,
                "batch_size": 100,
                "remaining": 2,
                "unavailable_scopes": [],
                "excluded_sensitive_scopes": list(sensitive_scopes),
                "complete": False,
            },
        )

    def test_normal_scope_plan_filters_unavailable_and_reports_progress(
        self,
    ) -> None:
        """Normal planning keeps only opened scopes and reports each subset."""
        application = self.module.OAuthApplicationInfo(
            effective_owner_open_id="ou_owner",
            user_scopes=(
                "calendar:calendar",
                "task:task",
                "contact:user:readonly",
            ),
        )

        plan = self.module.plan_authorization_scopes(
            application,
            "ou_owner",
            (
                "calendar:calendar unavailable:scope "
                "calendar:calendar task:task"
            ),
            is_batch=False,
            granted_scope="calendar:calendar offline_access",
        )

        self.assertEqual(
            plan.requested_scopes,
            (
                "calendar:calendar",
                "unavailable:scope",
                "task:task",
            ),
        )
        self.assertEqual(
            plan.available_scopes,
            ("calendar:calendar", "task:task"),
        )
        self.assertEqual(
            plan.unavailable_scopes,
            ("unavailable:scope",),
        )
        self.assertEqual(
            plan.already_granted_scopes,
            ("calendar:calendar",),
        )
        self.assertEqual(plan.missing_scopes, ("task:task",))
        self.assertEqual(
            plan.scopes_to_authorize,
            ("calendar:calendar", "task:task"),
        )
        self.assertEqual(plan.total_app_scopes, 3)
        self.assertEqual((plan.total, plan.already, plan.missing), (2, 1, 1))

    async def test_runtime_scope_planning_reads_stored_grants_after_owner_check(
        self,
    ) -> None:
        """The runtime supplies persistent grants without bypassing owner policy."""
        store = _FakeStore()
        stored = self._stored_token(
            user_open_id="ou_owner",
            scope="calendar:calendar",
        )
        store.tokens[(stored.app_id, stored.user_open_id)] = stored
        runtime = self.module.OAuthRuntime(
            self._account(),
            http=_FakeHTTP(self.module),
            store=store,
        )
        application = self.module.OAuthApplicationInfo(
            effective_owner_open_id="ou_owner",
            user_scopes=("calendar:calendar", "task:task"),
        )

        plan = await runtime.plan_authorization(
            application,
            "ou_owner",
            is_batch=True,
        )

        self.assertEqual(
            store.get_calls,
            [("cli_test", "ou_owner")],
        )
        self.assertEqual(plan.already_granted_scopes, ("calendar:calendar",))
        self.assertEqual(plan.missing_scopes, ("task:task",))

        denied_application = self.module.OAuthApplicationInfo(
            effective_owner_open_id="ou_other",
            user_scopes=("calendar:calendar",),
        )
        with self.assertRaises(self.module.OAuthOwnerAccessDeniedError):
            await runtime.plan_authorization(
                denied_application,
                "ou_owner",
                is_batch=True,
            )
        self.assertEqual(
            store.get_calls,
            [("cli_test", "ou_owner")],
        )

    async def test_device_authorization_uses_basic_auth_and_offline_scope(self) -> None:
        """Device requests use confidential-client auth and refresh scope."""
        http = _FakeHTTP(
            self.module,
            (
                200,
                {
                    "device_code": "device-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://accounts.feishu.cn/device",
                    "verification_uri_complete": (
                        "https://accounts.feishu.cn/device?code=ABCD-EFGH"
                    ),
                    "expires_in": 240,
                    "interval": 5,
                },
            ),
        )
        runtime = self.module.OAuthRuntime(
            self._account(),
            http=http,
            store=_FakeStore(),
        )

        authorization = await runtime.request_device_authorization(
            "calendar:calendar"
        )

        self.assertEqual(authorization.device_code, "device-code")
        request = http.requests[0]
        expected_basic = base64.b64encode(
            b"cli_test:secret_test"
        ).decode("ascii")
        self.assertEqual(
            request["headers"]["Authorization"],
            f"Basic {expected_basic}",
        )
        self.assertEqual(
            request["form"]["scope"],
            "calendar:calendar offline_access",
        )
        self.assertEqual(
            request["url"],
            "https://accounts.feishu.cn/oauth/v1/device_authorization",
        )

    async def test_device_authorization_can_omit_offline_scope(self) -> None:
        """One-shot callers can avoid requesting refresh-token access."""
        http = _FakeHTTP(
            self.module,
            (
                200,
                {
                    "device_code": "device-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://accounts.feishu.cn/device",
                    "expires_in": 240,
                    "interval": 5,
                },
            ),
        )
        runtime = self.module.OAuthRuntime(
            self._account(),
            http=http,
            store=_FakeStore(),
        )

        await runtime.request_device_authorization(
            "im:message im:message.send_as_user",
            include_offline_access=False,
        )

        self.assertEqual(
            http.requests[0]["form"]["scope"],
            "im:message im:message.send_as_user",
        )

    async def test_poll_handles_pending_slow_down_and_missing_refresh_token(
        self,
    ) -> None:
        """Polling backs off and bounds a non-refreshable token grant."""
        http = _FakeHTTP(
            self.module,
            (200, {"error": "authorization_pending"}),
            (200, {"error": "slow_down"}),
            (
                200,
                {
                    "access_token": "new-access",
                    "expires_in": 3600,
                    "scope": "calendar:calendar",
                },
            ),
        )
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            """Capture intervals without delaying the test."""
            sleeps.append(seconds)

        runtime = self.module.OAuthRuntime(
            self._account(),
            http=http,
            store=_FakeStore(),
            sleep=fake_sleep,
            clock_ms=lambda: 1_000_000,
        )
        authorization = self.module.DeviceAuthorization(
            device_code="device-code",
            user_code="ABCD-EFGH",
            verification_uri="https://accounts.feishu.cn/device",
            verification_uri_complete="https://accounts.feishu.cn/device?code=x",
            expires_in=240,
            interval=5,
        )

        result = await runtime.poll_device_token(authorization)

        self.assertTrue(result.ok)
        self.assertEqual(sleeps, [5, 5, 10])
        self.assertEqual(result.token.refresh_token, "")
        self.assertEqual(result.token.refresh_expires_in, 3600)
        self.assertEqual(
            http.requests[-1]["form"]["grant_type"],
            "urn:ietf:params:oauth:grant-type:device_code",
        )

    async def test_poll_failure_messages_are_english(self) -> None:
        """Terminal Device Flow failures return stable English guidance."""
        authorization = self.module.DeviceAuthorization(
            device_code="device-code",
            user_code="ABCD-EFGH",
            verification_uri="https://accounts.feishu.cn/device",
            verification_uri_complete="https://accounts.feishu.cn/device?code=x",
            expires_in=240,
            interval=5,
        )

        async def no_sleep(_seconds: float) -> None:
            """Advance a scripted poll without a wall-clock delay."""

        for error, expected in (
            ("access_denied", "The user denied authorization"),
            ("expired_token", "The authorization code expired; start again"),
        ):
            with self.subTest(error=error):
                runtime = self.module.OAuthRuntime(
                    self._account(),
                    http=_FakeHTTP(self.module, (200, {"error": error})),
                    store=_FakeStore(),
                    sleep=no_sleep,
                    clock_ms=lambda: 1_000_000,
                )

                result = await runtime.poll_device_token(authorization)

                self.assertFalse(result.ok)
                self.assertEqual(result.message, expected)

        clock_values = iter((0, 1_001))
        timeout_runtime = self.module.OAuthRuntime(
            self._account(),
            http=_FakeHTTP(self.module),
            store=_FakeStore(),
            sleep=no_sleep,
            clock_ms=lambda: next(clock_values),
        )
        timeout_authorization = self.module.DeviceAuthorization(
            device_code="device-code",
            user_code="ABCD-EFGH",
            verification_uri="https://accounts.feishu.cn/device",
            verification_uri_complete="https://accounts.feishu.cn/device?code=x",
            expires_in=1,
            interval=1,
        )

        timeout_result = await timeout_runtime.poll_device_token(
            timeout_authorization
        )

        self.assertFalse(timeout_result.ok)
        self.assertEqual(
            timeout_result.message,
            "Authorization timed out; start again",
        )

    async def test_identity_mismatch_never_persists_token(self) -> None:
        """A token authorized by another user fails closed."""
        http = _FakeHTTP(
            self.module,
            (
                200,
                {
                    "code": 0,
                    "data": {"open_id": "ou_other"},
                },
            ),
        )
        store = _FakeStore()
        runtime = self.module.OAuthRuntime(
            self._account(),
            http=http,
            store=store,
            clock_ms=lambda: 1_000_000,
        )
        grant = self.module.OAuthTokenGrant(
            access_token="new-access",
            refresh_token="new-refresh",
            expires_in=7200,
            refresh_expires_in=604800,
            scope="calendar:calendar",
        )

        with self.assertRaises(
            self.module.OAuthIdentityMismatchError
        ) as raised:
            await runtime.complete_authorization("ou_user", grant)

        self.assertEqual(raised.exception.actual_open_id, "ou_other")
        self.assertEqual(store.set_calls, [])

    async def test_identity_success_persists_complete_token(self) -> None:
        """A matching identity is persisted with absolute expiry times."""
        http = _FakeHTTP(
            self.module,
            (
                200,
                {
                    "code": 0,
                    "data": {"open_id": "ou_user"},
                },
            ),
        )
        store = _FakeStore()
        runtime = self.module.OAuthRuntime(
            self._account(),
            http=http,
            store=store,
            clock_ms=lambda: 1_000_000,
        )
        grant = self.module.OAuthTokenGrant(
            access_token="new-access",
            refresh_token="new-refresh",
            expires_in=7200,
            refresh_expires_in=604800,
            scope="calendar:calendar",
        )

        stored = await runtime.complete_authorization("ou_user", grant)

        self.assertEqual(stored.expires_at, 8_200_000)
        self.assertEqual(stored.refresh_expires_at, 605_800_000)
        self.assertEqual(store.set_calls, [stored])
        self.assertEqual(
            http.requests[0]["headers"]["Authorization"],
            "Bearer new-access",
        )

    async def test_refresh_retries_server_error_and_rotates_credentials(
        self,
    ) -> None:
        """Refresh code 20050 retries once and stores the rotated token."""
        http = _FakeHTTP(
            self.module,
            (200, {"code": 20050, "msg": "retry"}),
            (
                200,
                {
                    "code": 0,
                    "access_token": "rotated-access",
                    "refresh_token": "rotated-refresh",
                    "expires_in": 7200,
                    "refresh_token_expires_in": 604800,
                    "scope": "calendar:calendar task:task",
                },
            ),
        )
        store = _FakeStore()
        existing = self._stored_token()
        store.tokens[(existing.app_id, existing.user_open_id)] = existing
        runtime = self.module.OAuthRuntime(
            self._account(),
            http=http,
            store=store,
            clock_ms=lambda: 1_000_000,
        )

        refreshed = await runtime.refresh("ou_user")

        self.assertEqual(len(http.requests), 2)
        self.assertEqual(
            [request["form"]["refresh_token"] for request in http.requests],
            ["old-refresh", "old-refresh"],
        )
        self.assertEqual(refreshed.access_token, "rotated-access")
        self.assertEqual(refreshed.refresh_token, "rotated-refresh")
        self.assertEqual(refreshed.granted_at, existing.granted_at)
        self.assertEqual(store.set_calls, [refreshed])

    async def test_terminal_refresh_failure_clears_token(self) -> None:
        """Invalid or revoked refresh credentials are removed immediately."""
        http = _FakeHTTP(
            self.module,
            (200, {"code": 20064, "msg": "revoked"}),
        )
        store = _FakeStore()
        existing = self._stored_token()
        store.tokens[(existing.app_id, existing.user_open_id)] = existing
        runtime = self.module.OAuthRuntime(
            self._account(),
            http=http,
            store=store,
            clock_ms=lambda: 1_000_000,
        )

        refreshed = await runtime.refresh("ou_user")

        self.assertIsNone(refreshed)
        self.assertEqual(store.remove_calls, [("cli_test", "ou_user")])
        self.assertNotIn(("cli_test", "ou_user"), store.tokens)

    async def test_concurrent_runtimes_refresh_one_rotating_token_once(self) -> None:
        """Cross-process lock protocol prevents duplicate refresh consumption."""

        class GatedHTTP:
            """Hold the first refresh open while a second runtime contends."""

            def __init__(self, module: ModuleType) -> None:
                self.module = module
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.requests: list[dict[str, Any]] = []

            async def request_json(
                self,
                method: str,
                url: str,
                *,
                headers: Any = None,
                form: Any = None,
            ) -> Any:
                """Return one rotated grant after both callers can contend."""
                self.requests.append(
                    {
                        "method": method,
                        "url": url,
                        "headers": dict(headers or {}),
                        "form": dict(form or {}),
                    }
                )
                self.started.set()
                await self.release.wait()
                return self.module.JsonHTTPResponse(
                    status=200,
                    payload={
                        "code": 0,
                        "access_token": "rotated-access",
                        "refresh_token": "rotated-refresh",
                        "expires_in": 7200,
                        "refresh_token_expires_in": 604800,
                        "scope": "calendar:calendar",
                    },
                )

        store = _FakeStore()
        existing = self._stored_token()
        store.tokens[(existing.app_id, existing.user_open_id)] = existing
        http = GatedHTTP(self.module)
        first_runtime = self.module.OAuthRuntime(
            self._account(),
            http=http,
            store=store,
            clock_ms=lambda: 1_000_000,
        )
        second_runtime = self.module.OAuthRuntime(
            self._account(),
            http=http,
            store=store,
            clock_ms=lambda: 1_000_000,
        )

        with tempfile.TemporaryDirectory() as lock_directory, patch.dict(
            os.environ,
            {"HERMES_LARK_UAT_LOCK_DIR": lock_directory},
            clear=False,
        ):
            first = asyncio.create_task(first_runtime.get_valid_token("ou_user"))
            await http.started.wait()
            second = asyncio.create_task(second_runtime.get_valid_token("ou_user"))
            await asyncio.sleep(0.1)
            self.assertEqual(len(http.requests), 1)
            lock_entries = list(Path(lock_directory).iterdir())
            self.assertEqual(len(lock_entries), 1)
            self.assertNotIn("cli_test", lock_entries[0].name)
            self.assertNotIn("ou_user", lock_entries[0].name)
            owner = (lock_entries[0] / "owner.json").read_text(encoding="utf-8")
            self.assertNotIn("old-refresh", owner)
            self.assertNotIn("old-access", owner)
            http.release.set()
            first_token, second_token = await asyncio.gather(first, second)

        self.assertEqual(len(http.requests), 1)
        self.assertEqual(first_token.access_token, "rotated-access")
        self.assertEqual(second_token.access_token, "rotated-access")
        self.assertEqual(second_token.refresh_token, "rotated-refresh")

    def test_endpoint_resolution_and_token_status_match_upstream(self) -> None:
        """Brand mapping and the five-minute refresh window remain exact."""
        lark = self.module.resolve_oauth_endpoints("lark")
        custom = self.module.resolve_oauth_endpoints(
            "https://open.example.com/"
        )

        self.assertEqual(
            lark.token,
            "https://open.larksuite.com/open-apis/authen/v2/oauth/token",
        )
        self.assertEqual(
            custom.device_authorization,
            "https://accounts.example.com/oauth/v1/device_authorization",
        )
        self.assertEqual(
            custom.token,
            "https://open.example.com/open-apis/authen/v2/oauth/token",
        )
        valid = self._stored_token(
            expires_at=1_300_001,
            refresh_expires_at=2_000_000,
        )
        needs_refresh = self._stored_token(
            expires_at=1_300_000,
            refresh_expires_at=2_000_000,
        )
        expired = self._stored_token(
            expires_at=1_100_000,
            refresh_expires_at=1_000_000,
        )
        self.assertEqual(
            self.module.token_status(valid, 1_000_000),
            "valid",
        )
        self.assertEqual(
            self.module.token_status(needs_refresh, 1_000_000),
            "needs_refresh",
        )
        self.assertEqual(
            self.module.token_status(expired, 1_000_000),
            "expired",
        )


if __name__ == "__main__":
    unittest.main()
