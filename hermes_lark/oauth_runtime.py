"""OAuth Device Flow runtime aligned with ``larksuite/openclaw-lark``.

The adapter owns presentation and callback delivery. This module owns the
security-sensitive protocol: requesting and polling device codes, validating
the authorizing identity, refreshing credentials, and persisting them through
the bundled Node credential-store bridge.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol


_BUNDLE_PATH = Path(__file__).resolve().parent / "node" / "openclaw_tools_bridge.mjs"
_DEFAULT_HTTP_TIMEOUT_SECONDS = 30.0
_DEFAULT_BRIDGE_TIMEOUT_SECONDS = 45.0
_MAX_POLL_INTERVAL_SECONDS = 60
_MAX_POLL_ATTEMPTS = 200
_REFRESH_AHEAD_MS = 5 * 60 * 1000
_REFRESH_SERVER_ERROR = 20050
_REFRESH_LOCK_RETRY_SECONDS = 0.05
_REFRESH_LOCK_STALE_SECONDS = 10 * 60
MAX_SCOPES_PER_BATCH = 100
SENSITIVE_BATCH_SCOPES = frozenset(
    {
        "im:message.send_as_user",
        "space:document:delete",
        "calendar:calendar.event:delete",
        "base:table:delete",
    }
)


@dataclass(frozen=True)
class OAuthAccount:
    """Configured Feishu/Lark application used for a user authorization."""

    app_id: str
    app_secret: str
    brand: str = "feishu"
    account_id: str = "default"


@dataclass(frozen=True)
class OAuthApplicationInfo:
    """Owner identity and user scopes resolved from the Open Platform."""

    effective_owner_open_id: Optional[str]
    user_scopes: Sequence[str]


@dataclass(frozen=True)
class OAuthEndpoints:
    """Device and token endpoints derived from the configured Lark brand."""

    device_authorization: str
    token: str


@dataclass(frozen=True)
class DeviceAuthorization:
    """Device code and browser URL returned by the authorization endpoint."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class OAuthTokenGrant:
    """Unpersisted token grant returned after successful device polling."""

    access_token: str
    refresh_token: str
    expires_in: int
    refresh_expires_in: int
    scope: str


@dataclass(frozen=True)
class DevicePollResult:
    """Terminal result of polling one OAuth device code."""

    ok: bool
    token: Optional[OAuthTokenGrant] = None
    error: Optional[str] = None
    message: str = ""


@dataclass(frozen=True)
class IdentityCheck:
    """Result of binding a token to the expected Feishu open_id."""

    valid: bool
    actual_open_id: Optional[str] = None


@dataclass(frozen=True)
class StoredOAuthToken:
    """Complete credential record persisted outside the model boundary."""

    user_open_id: str
    app_id: str
    access_token: str
    refresh_token: str
    expires_at: int
    refresh_expires_at: int
    scope: str
    granted_at: int

    def to_bridge_dict(self) -> dict[str, Any]:
        """Return the camel-case record consumed by the Node bridge."""
        return {
            "userOpenId": self.user_open_id,
            "appId": self.app_id,
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
            "expiresAt": self.expires_at,
            "refreshExpiresAt": self.refresh_expires_at,
            "scope": self.scope,
            "grantedAt": self.granted_at,
        }

    @classmethod
    def from_bridge_dict(cls, value: Mapping[str, Any]) -> "StoredOAuthToken":
        """Validate and normalize one credential returned by the Node bridge."""
        try:
            token = cls(
                user_open_id=str(value["userOpenId"]),
                app_id=str(value["appId"]),
                access_token=str(value["accessToken"]),
                refresh_token=str(value.get("refreshToken") or ""),
                expires_at=int(value["expiresAt"]),
                refresh_expires_at=int(value["refreshExpiresAt"]),
                scope=str(value.get("scope") or ""),
                granted_at=int(value["grantedAt"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise OAuthProtocolError(
                "credential store returned an invalid token"
            ) from error
        if not token.user_open_id or not token.app_id or not token.access_token:
            raise OAuthProtocolError("credential store returned an incomplete token")
        return token


@dataclass(frozen=True)
class AuthorizationScopePlan:
    """Owner-validated scope selection ready for card presentation."""

    is_batch: bool
    requested_scopes: tuple[str, ...]
    app_scopes: tuple[str, ...]
    available_scopes: tuple[str, ...]
    unavailable_scopes: tuple[str, ...]
    excluded_sensitive_scopes: tuple[str, ...]
    already_granted_scopes: tuple[str, ...]
    missing_scopes: tuple[str, ...]
    scopes_to_authorize: tuple[str, ...]
    remaining_scopes: tuple[str, ...]
    total_app_scopes: int
    total: int
    already: int
    missing: int

    @property
    def scope(self) -> str:
        """Return the space-delimited scope accepted by Device Flow."""
        return " ".join(self.scopes_to_authorize)

    @property
    def batch_size(self) -> int:
        """Return the number of scopes selected for this request."""
        return len(self.scopes_to_authorize)

    @property
    def remaining(self) -> int:
        """Return the number of missing scopes deferred to later batches."""
        return len(self.remaining_scopes)

    @property
    def complete(self) -> bool:
        """Return whether no additional authorization is required."""
        return self.missing == 0

    def to_display_dict(self) -> dict[str, Any]:
        """Return non-secret progress fields suitable for a Feishu card."""
        return {
            "is_batch": self.is_batch,
            "scope": self.scope,
            "total_app_scopes": self.total_app_scopes,
            "total": self.total,
            "already": self.already,
            "missing": self.missing,
            "batch_size": self.batch_size,
            "remaining": self.remaining,
            "unavailable_scopes": list(self.unavailable_scopes),
            "excluded_sensitive_scopes": list(
                self.excluded_sensitive_scopes
            ),
            "complete": self.complete,
        }


@dataclass(frozen=True)
class JsonHTTPResponse:
    """Status code and decoded object returned by an OAuth HTTP request."""

    status: int
    payload: Mapping[str, Any]


class OAuthProtocolError(RuntimeError):
    """OAuth server or credential bridge returned an invalid response."""


class OAuthIdentityMismatchError(RuntimeError):
    """The user completing OAuth did not match the initiating message sender."""

    def __init__(self, expected_open_id: str, actual_open_id: Optional[str]) -> None:
        """Record safe identity metadata without retaining token values."""
        super().__init__("OAuth identity validation failed")
        self.expected_open_id = expected_open_id
        self.actual_open_id = actual_open_id


class OAuthOwnerAccessDeniedError(PermissionError):
    """The initiating user could not be verified as application owner."""

    def __init__(self, reason: str) -> None:
        """Expose only a stable denial reason, never the owner's open_id."""
        super().__init__(
            "Permission denied: Only the app owner may authorize user scopes."
        )
        self.reason = reason


@dataclass(frozen=True)
class _RefreshLockLease:
    """Ownership proof for one cross-process credential refresh lock."""

    path: Path
    nonce: str


def _refresh_lock_root() -> Path:
    """Return the private lock root shared with one-shot Node workers."""
    configured = str(os.environ.get("HERMES_LARK_UAT_LOCK_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    getuid = getattr(os, "getuid", None)
    if callable(getuid):
        user_namespace = str(getuid())
    else:
        user_namespace = hashlib.sha256(
            str(Path.home()).encode("utf-8")
        ).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"hermes-lark-uat-locks-{user_namespace}"


def _refresh_lock_path(app_id: str, user_open_id: str) -> Path:
    """Hash a credential identity so lock filenames expose no account IDs."""
    digest = hashlib.sha256(f"{app_id}:{user_open_id}".encode("utf-8")).hexdigest()
    return _refresh_lock_root() / digest


def _ensure_refresh_lock_root() -> Path:
    """Create and validate the private cross-process lock directory."""
    root = _refresh_lock_root()
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = root.lstat()
    if not root.is_dir() or root.is_symlink():
        raise OAuthProtocolError("UAT lock root must be a real directory")
    getuid = getattr(os, "getuid", None)
    if callable(getuid) and info.st_uid != getuid():
        raise OAuthProtocolError(
            "UAT lock root belongs to another operating-system user"
        )
    root.chmod(0o700)
    return root


def _read_refresh_lock_owner(path: Path) -> Optional[Mapping[str, Any]]:
    """Read a non-secret lock owner record or return ``None``."""
    try:
        value = json.loads((path / "owner.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping):
        return None
    if not isinstance(value.get("pid"), int) or not isinstance(
        value.get("nonce"), str
    ):
        return None
    return value


def _process_is_alive(pid: int) -> bool:
    """Return whether a local process still owns a refresh lock."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _refresh_lock_is_stale(path: Path) -> bool:
    """Return whether a crashed or abandoned refresh lock is recoverable."""
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return True
    owner = _read_refresh_lock_owner(path)
    if owner is not None and not _process_is_alive(int(owner["pid"])):
        return True
    return age >= _REFRESH_LOCK_STALE_SECONDS


def _remove_refresh_lock(path: Path) -> None:
    """Remove the two known entries that make up a refresh lock."""
    try:
        (path / "owner.json").unlink(missing_ok=True)
        path.rmdir()
    except FileNotFoundError:
        return
    except OSError:
        return


async def _acquire_refresh_lock(
    app_id: str,
    user_open_id: str,
) -> _RefreshLockLease:
    """Acquire one atomic directory lock without blocking the event loop."""
    _ensure_refresh_lock_root()
    path = _refresh_lock_path(app_id, user_open_id)
    nonce = secrets.token_hex(16)
    owner = {"pid": os.getpid(), "nonce": nonce}
    while True:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            if _refresh_lock_is_stale(path):
                _remove_refresh_lock(path)
                continue
            await asyncio.sleep(_REFRESH_LOCK_RETRY_SECONDS)
            continue
        try:
            owner_path = path / "owner.json"
            with owner_path.open("x", encoding="utf-8") as handle:
                json.dump(owner, handle, separators=(",", ":"))
            owner_path.chmod(0o600)
        except Exception:
            _remove_refresh_lock(path)
            raise
        return _RefreshLockLease(path=path, nonce=nonce)


def _release_refresh_lock(lease: _RefreshLockLease) -> None:
    """Release a refresh lock only while its nonce still matches."""
    owner = _read_refresh_lock_owner(lease.path)
    if (
        owner is not None
        and owner.get("pid") == os.getpid()
        and owner.get("nonce") == lease.nonce
    ):
        _remove_refresh_lock(lease.path)


class OAuthHTTPClient(Protocol):
    """Minimal asynchronous JSON transport required by the OAuth runtime."""

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        form: Optional[Mapping[str, str]] = None,
    ) -> JsonHTTPResponse:
        """Execute one request and decode a JSON object response."""


class OAuthTokenStore(Protocol):
    """Persistent token operations required by the OAuth runtime."""

    async def get(self, app_id: str, user_open_id: str) -> Optional[StoredOAuthToken]:
        """Read one stored token."""

    async def set(self, token: StoredOAuthToken) -> None:
        """Persist one complete token."""

    async def remove(self, app_id: str, user_open_id: str) -> None:
        """Remove one stored token."""


class UrllibOAuthHTTPClient:
    """Standard-library asynchronous transport for OAuth JSON endpoints."""

    def __init__(self, timeout_seconds: float = _DEFAULT_HTTP_TIMEOUT_SECONDS) -> None:
        """Configure the bounded HTTP timeout."""
        self._timeout_seconds = max(1.0, float(timeout_seconds))

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        form: Optional[Mapping[str, str]] = None,
    ) -> JsonHTTPResponse:
        """Execute blocking urllib work away from the event loop."""
        return await asyncio.to_thread(
            self._request_json,
            method,
            url,
            dict(headers or {}),
            dict(form) if form is not None else None,
        )

    def _request_json(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        form: Optional[Mapping[str, str]],
    ) -> JsonHTTPResponse:
        """Execute and decode one request in a worker thread."""
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers=dict(headers),
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self._timeout_seconds,
            ) as response:
                status = int(response.status)
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            status = int(error.code)
            raw = error.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise OAuthProtocolError(
                f"OAuth endpoint returned invalid JSON (HTTP {status})"
            ) from error
        if not isinstance(payload, Mapping):
            raise OAuthProtocolError("OAuth endpoint response must be a JSON object")
        return JsonHTTPResponse(status=status, payload=payload)


class NodeTokenStore:
    """Persistent OAuth store backed by openclaw-lark's secure Node store."""

    def __init__(
        self,
        bundle_path: Path = _BUNDLE_PATH,
        *,
        node_executable: str = "node",
        timeout_seconds: float = _DEFAULT_BRIDGE_TIMEOUT_SECONDS,
        environment: Optional[Mapping[str, str]] = None,
    ) -> None:
        """Configure the internal credential bridge subprocess."""
        self._bundle_path = Path(bundle_path)
        self._node_executable = node_executable
        self._timeout_seconds = max(1.0, float(timeout_seconds))
        self._environment = dict(environment or {})

    async def get(self, app_id: str, user_open_id: str) -> Optional[StoredOAuthToken]:
        """Read one credential without exposing it through a tool result."""
        result = await asyncio.to_thread(
            self._call,
            {
                "action": "token_get",
                "config": {},
                "credential": {
                    "appId": app_id,
                    "userOpenId": user_open_id,
                },
            },
        )
        if not result.get("found"):
            return None
        raw_token = result.get("token")
        if not isinstance(raw_token, Mapping):
            raise OAuthProtocolError(
                "credential store returned an invalid token envelope"
            )
        return StoredOAuthToken.from_bridge_dict(raw_token)

    async def set(self, token: StoredOAuthToken) -> None:
        """Persist one credential through the selected secure backend."""
        result = await asyncio.to_thread(
            self._call,
            {
                "action": "token_set",
                "config": {},
                "storedToken": token.to_bridge_dict(),
            },
        )
        if result.get("stored") is not True:
            raise OAuthProtocolError("credential store did not acknowledge the token")

    async def remove(self, app_id: str, user_open_id: str) -> None:
        """Remove one credential through the selected secure backend."""
        result = await asyncio.to_thread(
            self._call,
            {
                "action": "token_remove",
                "config": {},
                "credential": {
                    "appId": app_id,
                    "userOpenId": user_open_id,
                },
            },
        )
        if result.get("removed") is not True:
            raise OAuthProtocolError("credential store did not acknowledge removal")

    def _call(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Execute one private bridge action and validate its envelope."""
        if not self._bundle_path.is_file():
            raise OAuthProtocolError(
                f"OpenClaw tool bridge is missing: {self._bundle_path}"
            )
        environment = os.environ.copy()
        environment.update(self._environment)
        try:
            completed = subprocess.run(
                [self._node_executable, str(self._bundle_path)],
                input=json.dumps(request, ensure_ascii=False, separators=(",", ":")),
                capture_output=True,
                check=False,
                text=True,
                timeout=self._timeout_seconds,
                env=environment,
            )
        except FileNotFoundError as error:
            raise OAuthProtocolError(
                "Node.js is required for OAuth persistence"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise OAuthProtocolError("credential store bridge timed out") from error
        if completed.returncode != 0 or not completed.stdout.strip():
            raise OAuthProtocolError("credential store bridge process failed")
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise OAuthProtocolError(
                "credential store bridge returned invalid JSON"
            ) from error
        if not isinstance(response, Mapping):
            raise OAuthProtocolError(
                "credential store bridge returned an invalid envelope"
            )
        if not response.get("ok"):
            error = response.get("error")
            message = (
                str(error.get("message"))
                if isinstance(error, Mapping) and error.get("message")
                else "credential store bridge rejected the operation"
            )
            raise OAuthProtocolError(message)
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise OAuthProtocolError(
                "credential store bridge returned an invalid result"
            )
        return result


def resolve_oauth_endpoints(brand: str) -> OAuthEndpoints:
    """Resolve OAuth endpoints with the same rules as openclaw-lark."""
    normalized = str(brand or "feishu").rstrip("/")
    if normalized == "feishu":
        return OAuthEndpoints(
            device_authorization=(
                "https://accounts.feishu.cn/oauth/v1/device_authorization"
            ),
            token="https://open.feishu.cn/open-apis/authen/v2/oauth/token",
        )
    if normalized == "lark":
        return OAuthEndpoints(
            device_authorization=(
                "https://accounts.larksuite.com/oauth/v1/device_authorization"
            ),
            token="https://open.larksuite.com/open-apis/authen/v2/oauth/token",
        )

    accounts_base = normalized
    parsed = urllib.parse.urlsplit(normalized)
    if parsed.scheme and parsed.hostname and parsed.hostname.startswith("open."):
        host = parsed.hostname.replace("open.", "accounts.", 1)
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        accounts_base = urllib.parse.urlunsplit(
            (parsed.scheme, host, "", "", "")
        )
    return OAuthEndpoints(
        device_authorization=f"{accounts_base}/oauth/v1/device_authorization",
        token=f"{normalized}/open-apis/authen/v2/oauth/token",
    )


def token_status(token: StoredOAuthToken, now_ms: Optional[int] = None) -> str:
    """Classify a token with openclaw-lark's five-minute refresh window."""
    current = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if current < token.expires_at - _REFRESH_AHEAD_MS:
        return "valid"
    if current < token.refresh_expires_at:
        return "needs_refresh"
    return "expired"


def plan_authorization_scopes(
    application: OAuthApplicationInfo,
    initiating_user_open_id: str,
    requested_scope: str | Sequence[str] = "",
    *,
    is_batch: bool,
    granted_scope: str | Sequence[str] = "",
) -> AuthorizationScopePlan:
    """Validate owner access and select scopes with upstream semantics."""
    _assert_effective_owner(application, initiating_user_open_id)
    app_scopes = _normalize_scopes(application.user_scopes)
    requested_scopes = _normalize_scopes(requested_scope)
    granted_scopes = frozenset(_normalize_scopes(granted_scope))

    if is_batch:
        available_scopes = tuple(
            scope
            for scope in app_scopes
            if scope not in SENSITIVE_BATCH_SCOPES
        )
        excluded_sensitive = tuple(
            scope
            for scope in app_scopes
            if scope in SENSITIVE_BATCH_SCOPES
        )
        unavailable_scopes: tuple[str, ...] = ()
        already_granted = tuple(
            scope for scope in available_scopes if scope in granted_scopes
        )
        missing_scopes = tuple(
            scope for scope in available_scopes if scope not in granted_scopes
        )
        scopes_to_authorize = missing_scopes[:MAX_SCOPES_PER_BATCH]
        remaining_scopes = missing_scopes[MAX_SCOPES_PER_BATCH:]
        total_app_scopes = len(available_scopes)
    else:
        app_scope_set = frozenset(app_scopes)
        available_scopes = tuple(
            scope for scope in requested_scopes if scope in app_scope_set
        )
        unavailable_scopes = tuple(
            scope for scope in requested_scopes if scope not in app_scope_set
        )
        excluded_sensitive = ()
        already_granted = tuple(
            scope for scope in available_scopes if scope in granted_scopes
        )
        missing_scopes = tuple(
            scope for scope in available_scopes if scope not in granted_scopes
        )
        scopes_to_authorize = (
            available_scopes if missing_scopes else ()
        )
        remaining_scopes = ()
        total_app_scopes = len(app_scopes)

    return AuthorizationScopePlan(
        is_batch=is_batch,
        requested_scopes=requested_scopes,
        app_scopes=app_scopes,
        available_scopes=available_scopes,
        unavailable_scopes=unavailable_scopes,
        excluded_sensitive_scopes=excluded_sensitive,
        already_granted_scopes=already_granted,
        missing_scopes=missing_scopes,
        scopes_to_authorize=scopes_to_authorize,
        remaining_scopes=remaining_scopes,
        total_app_scopes=total_app_scopes,
        total=len(available_scopes),
        already=len(already_granted),
        missing=len(missing_scopes),
    )


class OAuthRuntime:
    """Complete headless OAuth protocol used by a live Feishu adapter."""

    def __init__(
        self,
        account: OAuthAccount,
        *,
        http: Optional[OAuthHTTPClient] = None,
        store: Optional[OAuthTokenStore] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock_ms: Optional[Callable[[], int]] = None,
    ) -> None:
        """Inject transport, persistence, and timing for deterministic use."""
        if not account.app_id or not account.app_secret:
            raise ValueError("OAuth account requires app_id and app_secret")
        self.account = account
        self.http = http or UrllibOAuthHTTPClient()
        self.store = store or NodeTokenStore()
        self._sleep = sleep
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    async def plan_authorization(
        self,
        application: OAuthApplicationInfo,
        initiating_user_open_id: str,
        requested_scope: str | Sequence[str] = "",
        *,
        is_batch: bool,
    ) -> AuthorizationScopePlan:
        """Load prior grants and produce an owner-validated scope plan."""
        _assert_effective_owner(application, initiating_user_open_id)
        stored = await self.store.get(
            self.account.app_id,
            initiating_user_open_id,
        )
        granted_scope = stored.scope if stored is not None else ""
        return plan_authorization_scopes(
            application,
            initiating_user_open_id,
            requested_scope,
            is_batch=is_batch,
            granted_scope=granted_scope,
        )

    async def request_device_authorization(
        self,
        scope: str = "",
        *,
        include_offline_access: bool = True,
    ) -> DeviceAuthorization:
        """Request a device code with optional refresh-token access."""
        scopes = [item for item in str(scope or "").split() if item]
        if include_offline_access and "offline_access" not in scopes:
            scopes.append("offline_access")
        normalized_scope = " ".join(scopes)
        credentials = f"{self.account.app_id}:{self.account.app_secret}".encode(
            "utf-8"
        )
        response = await self.http.request_json(
            "POST",
            resolve_oauth_endpoints(self.account.brand).device_authorization,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": (
                    f"Basic {base64.b64encode(credentials).decode('ascii')}"
                ),
            },
            form={
                "client_id": self.account.app_id,
                "scope": normalized_scope,
            },
        )
        data = response.payload
        if response.status < 200 or response.status >= 300 or data.get("error"):
            message = str(
                data.get("error_description")
                or data.get("error")
                or f"HTTP {response.status}"
            )
            raise OAuthProtocolError(f"Device authorization failed: {message}")
        try:
            authorization = DeviceAuthorization(
                device_code=str(data["device_code"]),
                user_code=str(data["user_code"]),
                verification_uri=str(data["verification_uri"]),
                verification_uri_complete=str(
                    data.get("verification_uri_complete")
                    or data["verification_uri"]
                ),
                expires_in=int(data.get("expires_in") or 240),
                interval=int(data.get("interval") or 5),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise OAuthProtocolError(
                "Device authorization returned an invalid response"
            ) from error
        if (
            not authorization.device_code
            or not authorization.user_code
            or not authorization.verification_uri
            or authorization.expires_in <= 0
            or authorization.interval <= 0
        ):
            raise OAuthProtocolError(
                "Device authorization returned an incomplete response"
            )
        return authorization

    async def poll_device_token(
        self,
        authorization: DeviceAuthorization,
    ) -> DevicePollResult:
        """Poll until authorization succeeds, fails, or expires."""
        interval = authorization.interval
        deadline = self._clock_ms() + authorization.expires_in * 1000
        endpoint = resolve_oauth_endpoints(self.account.brand).token
        attempts = 0
        while self._clock_ms() < deadline and attempts < _MAX_POLL_ATTEMPTS:
            attempts += 1
            await self._sleep(interval)
            try:
                response = await self.http.request_json(
                    "POST",
                    endpoint,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    form={
                        "grant_type": (
                            "urn:ietf:params:oauth:grant-type:device_code"
                        ),
                        "device_code": authorization.device_code,
                        "client_id": self.account.app_id,
                        "client_secret": self.account.app_secret,
                    },
                )
                data = response.payload
            except Exception:
                interval = min(interval + 1, _MAX_POLL_INTERVAL_SECONDS)
                continue

            error = str(data.get("error") or "")
            access_token = data.get("access_token")
            if not error and access_token:
                expires_in = _positive_int(data.get("expires_in"), 7200)
                refresh_token = str(data.get("refresh_token") or "")
                refresh_expires_in = _positive_int(
                    data.get("refresh_token_expires_in"),
                    604800,
                )
                if not refresh_token:
                    refresh_expires_in = expires_in
                return DevicePollResult(
                    ok=True,
                    token=OAuthTokenGrant(
                        access_token=str(access_token),
                        refresh_token=refresh_token,
                        expires_in=expires_in,
                        refresh_expires_in=refresh_expires_in,
                        scope=str(data.get("scope") or ""),
                    ),
                )
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval = min(interval + 5, _MAX_POLL_INTERVAL_SECONDS)
                continue
            if error == "access_denied":
                return DevicePollResult(
                    ok=False,
                    error="access_denied",
                    message="The user denied authorization",
                )
            if error in {"expired_token", "invalid_grant"}:
                return DevicePollResult(
                    ok=False,
                    error="expired_token",
                    message="The authorization code expired; start again",
                )
            return DevicePollResult(
                ok=False,
                error="expired_token",
                message=str(
                    data.get("error_description")
                    or error
                    or f"HTTP {response.status}"
                ),
            )
        return DevicePollResult(
            ok=False,
            error="expired_token",
            message="Authorization timed out; start again",
        )

    async def verify_identity(
        self,
        access_token: str,
        expected_open_id: str,
    ) -> IdentityCheck:
        """Fail closed unless the token belongs to the initiating user."""
        if self.account.brand == "lark":
            base_url = "https://open.larksuite.com"
        elif self.account.brand in {"", "feishu"}:
            base_url = "https://open.feishu.cn"
        else:
            base_url = str(self.account.brand).rstrip("/")
        try:
            response = await self.http.request_json(
                "GET",
                f"{base_url}/open-apis/authen/v1/user_info",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except Exception:
            return IdentityCheck(valid=False)
        data = response.payload
        nested = data.get("data")
        if (
            response.status < 200
            or response.status >= 300
            or data.get("code") != 0
            or not isinstance(nested, Mapping)
        ):
            return IdentityCheck(valid=False)
        actual = str(nested.get("open_id") or "")
        if not actual:
            return IdentityCheck(valid=False)
        return IdentityCheck(
            valid=actual == expected_open_id,
            actual_open_id=actual,
        )

    async def complete_authorization(
        self,
        user_open_id: str,
        grant: OAuthTokenGrant,
    ) -> StoredOAuthToken:
        """Validate token ownership, then persist the completed grant."""
        identity = await self.verify_identity(grant.access_token, user_open_id)
        if not identity.valid:
            raise OAuthIdentityMismatchError(
                expected_open_id=user_open_id,
                actual_open_id=identity.actual_open_id,
            )
        now = self._clock_ms()
        stored = StoredOAuthToken(
            user_open_id=user_open_id,
            app_id=self.account.app_id,
            access_token=grant.access_token,
            refresh_token=grant.refresh_token,
            expires_at=now + grant.expires_in * 1000,
            refresh_expires_at=now + grant.refresh_expires_in * 1000,
            scope=grant.scope,
            granted_at=now,
        )
        await self.store.set(stored)
        return stored

    async def refresh(self, user_open_id: str) -> Optional[StoredOAuthToken]:
        """Refresh one token with upstream rotation and clearing semantics."""
        observed = await self.store.get(self.account.app_id, user_open_id)
        if observed is None:
            return None
        return await self._refresh_observed(user_open_id, observed, force=True)

    async def _refresh_observed(
        self,
        user_open_id: str,
        observed: StoredOAuthToken,
        *,
        force: bool,
    ) -> Optional[StoredOAuthToken]:
        """Refresh under the cross-process lock after re-reading credentials."""
        lease = await _acquire_refresh_lock(self.account.app_id, user_open_id)
        try:
            stored = await self.store.get(self.account.app_id, user_open_id)
            if stored is None:
                return None
            rotated = (
                stored.access_token != observed.access_token
                or stored.refresh_token != observed.refresh_token
            )
            status = token_status(stored, self._clock_ms())
            if rotated and status != "expired":
                return stored
            if not force and status == "valid":
                return stored
            if status == "expired":
                await self.store.remove(self.account.app_id, user_open_id)
                return None

            data = await self._request_refresh(stored.refresh_token)
            if _oauth_response_failed(data):
                if _optional_int(data.get("code")) == _REFRESH_SERVER_ERROR:
                    data = await self._request_refresh(stored.refresh_token)
                if _oauth_response_failed(data):
                    await self.store.remove(self.account.app_id, user_open_id)
                    return None
            access_token = str(data.get("access_token") or "")
            if not access_token:
                raise OAuthProtocolError("Token refresh returned no access_token")

            now = self._clock_ms()
            refresh_expires = _optional_int(data.get("refresh_token_expires_in"))
            updated = StoredOAuthToken(
                user_open_id=stored.user_open_id,
                app_id=self.account.app_id,
                access_token=access_token,
                refresh_token=str(data.get("refresh_token") or stored.refresh_token),
                expires_at=now + _positive_int(data.get("expires_in"), 7200) * 1000,
                refresh_expires_at=(
                    now + refresh_expires * 1000
                    if refresh_expires is not None and refresh_expires > 0
                    else stored.refresh_expires_at
                ),
                scope=str(data.get("scope") or stored.scope),
                granted_at=stored.granted_at,
            )
            await self.store.set(updated)
            return updated
        finally:
            _release_refresh_lock(lease)

    async def get_valid_token(
        self,
        user_open_id: str,
    ) -> Optional[StoredOAuthToken]:
        """Return a valid token, refreshing or clearing it when required."""
        stored = await self.store.get(self.account.app_id, user_open_id)
        if stored is None:
            return None
        status = token_status(stored, self._clock_ms())
        if status == "valid":
            return stored
        if status == "needs_refresh":
            return await self._refresh_observed(
                user_open_id,
                stored,
                force=False,
            )
        return await self._refresh_observed(
            user_open_id,
            stored,
            force=False,
        )

    async def revoke(self, user_open_id: str) -> None:
        """Remove the current user's persistent OAuth credential."""
        await self.store.remove(self.account.app_id, user_open_id)

    async def _request_refresh(self, refresh_token: str) -> Mapping[str, Any]:
        """Call the v2 refresh endpoint once."""
        response = await self.http.request_json(
            "POST",
            resolve_oauth_endpoints(self.account.brand).token,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            form={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.account.app_id,
                "client_secret": self.account.app_secret,
            },
        )
        return response.payload


def _optional_int(value: Any) -> Optional[int]:
    """Parse one optional integer returned by Feishu."""
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _normalize_scopes(value: str | Sequence[str]) -> tuple[str, ...]:
    """Split, trim, and deduplicate scopes while preserving their order."""
    raw_values = value.split() if isinstance(value, str) else value
    scopes: list[str] = []
    seen: set[str] = set()
    for raw_scope in raw_values:
        for scope in str(raw_scope or "").split():
            if scope and scope not in seen:
                seen.add(scope)
                scopes.append(scope)
    return tuple(scopes)


def _assert_effective_owner(
    application: OAuthApplicationInfo,
    initiating_user_open_id: str,
) -> None:
    """Fail closed when effective owner identity is unavailable or differs."""
    owner_open_id = str(application.effective_owner_open_id or "").strip()
    user_open_id = str(initiating_user_open_id or "").strip()
    if not owner_open_id:
        raise OAuthOwnerAccessDeniedError("owner_unavailable")
    if not user_open_id or user_open_id != owner_open_id:
        raise OAuthOwnerAccessDeniedError("owner_mismatch")


def _positive_int(value: Any, fallback: int) -> int:
    """Parse one positive duration or use its upstream fallback."""
    parsed = _optional_int(value)
    return parsed if parsed is not None and parsed > 0 else fallback


def _oauth_response_failed(data: Mapping[str, Any]) -> bool:
    """Recognize both Feishu v2 and standard OAuth error envelopes."""
    code = _optional_int(data.get("code"))
    return (code is not None and code != 0) or bool(data.get("error"))


__all__ = [
    "AuthorizationScopePlan",
    "DeviceAuthorization",
    "DevicePollResult",
    "IdentityCheck",
    "JsonHTTPResponse",
    "MAX_SCOPES_PER_BATCH",
    "NodeTokenStore",
    "OAuthAccount",
    "OAuthApplicationInfo",
    "OAuthEndpoints",
    "OAuthHTTPClient",
    "OAuthIdentityMismatchError",
    "OAuthOwnerAccessDeniedError",
    "OAuthProtocolError",
    "OAuthRuntime",
    "OAuthTokenGrant",
    "OAuthTokenStore",
    "SENSITIVE_BATCH_SCOPES",
    "StoredOAuthToken",
    "UrllibOAuthHTTPClient",
    "plan_authorization_scopes",
    "resolve_oauth_endpoints",
    "token_status",
]
