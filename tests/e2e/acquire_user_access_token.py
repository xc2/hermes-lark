"""Acquire a one-shot Feishu user access token for live E2E tests."""

from __future__ import annotations

import asyncio
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from hermes_lark.oauth_runtime import OAuthAccount, OAuthProtocolError, OAuthRuntime


_REQUIRED_SCOPES = (
    "im:message",
    "im:message.send_as_user",
    "im:message:recall",
)
_TOKEN_FILE_KEY = "FEISHU_E2E_USER_ACCESS_TOKEN_FILE"


class AcquisitionError(RuntimeError):
    """Report an actionable failure that is safe to show to an operator."""


@dataclass(frozen=True)
class AcquisitionSettings:
    """Local files and application identity used by the one-shot flow."""

    app_id: str
    app_secret: str
    brand: str
    output_path: Path


def _load_settings() -> AcquisitionSettings:
    """Resolve credentials without accepting secrets as command arguments."""
    def setting(name: str) -> str:
        return str(os.environ.get(name) or "").strip()

    app_id = setting("FEISHU_APP_ID")
    app_secret = setting("FEISHU_APP_SECRET")
    missing = [
        name
        for name, value in (
            ("FEISHU_APP_ID", app_id),
            ("FEISHU_APP_SECRET", app_secret),
        )
        if not value
    ]
    if missing:
        raise AcquisitionError(
            "missing required setting names in the environment: "
            + ", ".join(missing)
        )

    token_file = setting(_TOKEN_FILE_KEY)
    if not token_file:
        raise AcquisitionError(f"missing required setting name: {_TOKEN_FILE_KEY}")

    brand = setting("FEISHU_DOMAIN") or "feishu"
    if brand not in {"feishu", "lark"}:
        raise AcquisitionError("FEISHU_DOMAIN must be either feishu or lark")
    return AcquisitionSettings(
        app_id=app_id,
        app_secret=app_secret,
        brand=brand,
        output_path=Path(token_file),
    )


async def _fetch_user_identity(
    runtime: OAuthRuntime,
    access_token: str,
) -> tuple[str, str]:
    """Resolve a recognizable authorizing identity without exposing tokens."""
    base_url = (
        "https://open.larksuite.com"
        if runtime.account.brand == "lark"
        else "https://open.feishu.cn"
    )
    response = await runtime.http.request_json(
        "GET",
        f"{base_url}/open-apis/authen/v1/user_info",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    data = response.payload.get("data")
    if (
        response.status < 200
        or response.status >= 300
        or response.payload.get("code") != 0
        or not isinstance(data, dict)
    ):
        raise AcquisitionError("authorized user lookup failed")
    open_id = str(data.get("open_id") or "")
    if not re.fullmatch(r"ou_[A-Za-z0-9_-]+", open_id):
        raise AcquisitionError(
            "authorized user lookup returned an invalid open_id"
        )
    display_name = "".join(
        character
        for character in str(data.get("name") or "")
        if character.isprintable()
    )
    return open_id, " ".join(display_name.split())[:80]


def _write_token(path: Path, access_token: str) -> None:
    """Atomically replace the single-value user token file."""
    if not access_token or any(character in access_token for character in "\r\n\0"):
        raise AcquisitionError("OAuth response contained an invalid access token")
    if path.is_symlink():
        raise AcquisitionError(f"refusing to replace symlink: {path}")

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    serialized = access_token + "\n"

    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path.exists():
            temporary_path.unlink()


async def _acquire(settings: AcquisitionSettings) -> None:
    """Run Device Flow and save only the confirmed short-lived token."""
    runtime = OAuthRuntime(
        OAuthAccount(
            app_id=settings.app_id,
            app_secret=settings.app_secret,
            brand=settings.brand,
        )
    )
    authorization = await runtime.request_device_authorization(
        " ".join(_REQUIRED_SCOPES),
        include_offline_access=False,
    )
    print(
        "Open the following URL in a browser on the host, then authorize "
        "with the test user:"
    )
    print(authorization.verification_uri_complete)
    print(f"If prompted, enter this user code: {authorization.user_code}")
    print(
        f"Authorization expires in {authorization.expires_in} seconds; "
        "waiting for completion..."
    )

    result = await runtime.poll_device_token(authorization)
    if not result.ok or result.token is None:
        if result.error == "access_denied":
            raise AcquisitionError("the user denied authorization; no token was saved")
        if result.error == "expired_token":
            raise AcquisitionError(
                "the authorization code expired or polling timed out; "
                "no token was saved"
            )
        raise AcquisitionError("authorization failed; no token was saved")
    granted_scopes = frozenset(result.token.scope.split())
    missing_scopes = [
        scope for scope in _REQUIRED_SCOPES if scope not in granted_scopes
    ]
    if missing_scopes:
        raise AcquisitionError(
            "authorized token is missing required scopes: "
            + ", ".join(missing_scopes)
        )

    actual_open_id, display_name = await _fetch_user_identity(
        runtime,
        result.token.access_token,
    )
    identity = (
        f"{display_name} ({actual_open_id})"
        if display_name
        else actual_open_id
    )
    print(f"Authorized user: {identity}")
    _write_token(settings.output_path, result.token.access_token)
    print(
        f"Saved the user access token to {settings.output_path}; "
        f"it expires in approximately {result.token.expires_in} seconds."
    )


def main() -> None:
    """Run the one-shot flow with concise, credential-safe errors."""
    try:
        asyncio.run(_acquire(_load_settings()))
    except KeyboardInterrupt:
        print("Authorization cancelled; no token was saved.", file=sys.stderr)
        raise SystemExit(130) from None
    except AcquisitionError as error:
        print(f"Failed to acquire a user access token: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except OAuthProtocolError:
        print(
            "Failed to acquire a user access token: the OAuth service "
            "rejected the request or returned an invalid response; "
            "no token was saved.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except Exception:
        print(
            "Failed to acquire a user access token: an unexpected error "
            "occurred; no token was saved.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
