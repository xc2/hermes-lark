"""Tests for the one-shot live-E2E user token bootstrap."""

from __future__ import annotations

import importlib.util
import io
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tests" / "e2e" / "acquire_user_access_token.py"


def _load_module() -> ModuleType:
    """Load the script as a testable module without running its main function."""
    spec = importlib.util.spec_from_file_location(
        "hermes_lark_e2e_user_token_test",
        MODULE_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class E2EUserAccessTokenTests(unittest.IsolatedAsyncioTestCase):
    """Verify token acquisition orchestration and local file safety."""

    @classmethod
    def setUpClass(cls) -> None:
        """Load the script once inside the installed plugin environment."""
        cls.module = _load_module()

    def _settings(self, output_path: Path, **overrides: object) -> object:
        """Build safe acquisition settings for one test."""
        values = {
            "app_id": "cli_test",
            "app_secret": "secret-test-value",
            "brand": "feishu",
            "output_path": output_path,
        }
        values.update(overrides)
        return self.module.AcquisitionSettings(**values)

    def _runtime(self, *, scopes: str | None = None) -> object:
        """Build a scripted runtime without network or persistence."""
        authorization = SimpleNamespace(
            device_code="device-secret-value",
            user_code="ABCD-EFGH",
            verification_uri="https://accounts.feishu.cn/device",
            verification_uri_complete=(
                "https://accounts.feishu.cn/device?code=ABCD-EFGH"
            ),
            expires_in=240,
            interval=5,
        )
        grant = SimpleNamespace(
            access_token="access-secret-value",
            refresh_token="refresh-secret-value",
            expires_in=3600,
            refresh_expires_in=3600,
            scope=(
                scopes
                if scopes is not None
                else (
                    "im:message im:message.send_as_user im:message:recall"
                )
            ),
        )
        runtime = Mock()
        runtime.account.brand = "feishu"
        runtime.request_device_authorization = AsyncMock(
            return_value=authorization
        )
        runtime.poll_device_token = AsyncMock(
            return_value=SimpleNamespace(
                ok=True,
                token=grant,
                error=None,
                message="",
            )
        )
        runtime.http.request_json = AsyncMock(
            return_value=SimpleNamespace(
                status=200,
                payload={
                    "code": 0,
                    "data": {
                        "open_id": "ou_expected",
                        "name": "Test User",
                    },
                },
            )
        )
        return runtime

    def test_writer_replaces_token_only_file_and_secures_it(self) -> None:
        """Generated credentials cannot retain unrelated static settings."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "state" / "user-access-token"
            output.parent.mkdir()
            output.write_text(
                "old-token\nunexpected-static-setting\n",
                encoding="utf-8",
            )

            self.module._write_token(output, "new-token")

            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "new-token\n",
            )
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(output.parent.stat().st_mode), 0o700)

    def test_writer_rejects_a_symlink_without_touching_its_target(self) -> None:
        """A crafted output symlink cannot redirect credential writes."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.env"
            target.write_text("unchanged\n", encoding="utf-8")
            output = root / "user-access-token"
            output.symlink_to(target)

            with self.assertRaisesRegex(RuntimeError, "symlink"):
                self.module._write_token(output, "new-token")

            self.assertEqual(target.read_text(encoding="utf-8"), "unchanged\n")

    async def test_acquire_requests_all_e2e_scopes_and_never_prints_tokens(
        self,
    ) -> None:
        """The successful flow saves the token without logging credentials."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "user-access-token"
            runtime = self._runtime()
            captured = io.StringIO()
            with patch.object(self.module, "OAuthRuntime", return_value=runtime):
                with redirect_stdout(captured):
                    await self.module._acquire(self._settings(output))

            runtime.request_device_authorization.assert_awaited_once_with(
                "im:message im:message.send_as_user im:message:recall",
                include_offline_access=False,
            )
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "access-secret-value\n",
            )
            log = captured.getvalue()
            self.assertIn("Test User (ou_expected)", log)
            self.assertNotIn("access-secret-value", log)
            self.assertNotIn("refresh-secret-value", log)
            self.assertNotIn("device-secret-value", log)
            self.assertNotIn("secret-test-value", log)

    def test_main_does_not_echo_secrets_from_oauth_errors(self) -> None:
        """Server-controlled OAuth errors are replaced with safe diagnostics."""
        reflected = (
            "secret-test-value device-secret-value "
            "access-secret-value refresh-secret-value"
        )
        error = self.module.OAuthProtocolError(reflected)
        captured = io.StringIO()
        with patch.object(
            self.module,
            "_load_settings",
            return_value=self._settings(Path("user-access-token")),
        ):
            with patch.object(
                self.module,
                "_acquire",
                AsyncMock(side_effect=error),
            ):
                with redirect_stderr(captured):
                    with self.assertRaisesRegex(SystemExit, "1"):
                        self.module.main()

        log = captured.getvalue()
        self.assertIn("OAuth service rejected the request", log)
        for secret in reflected.split():
            self.assertNotIn(secret, log)

    async def test_missing_scope_leaves_existing_file_unchanged(self) -> None:
        """An under-scoped token fails before replacing prior credentials."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "user-access-token"
            original = b"old-token\n"
            output.write_bytes(original)
            runtime = self._runtime(scopes="im:message")

            with patch.object(self.module, "OAuthRuntime", return_value=runtime):
                with self.assertRaisesRegex(RuntimeError, "missing required scopes"):
                    await self.module._acquire(self._settings(output))

            self.assertEqual(output.read_bytes(), original)

    async def test_identity_is_resolved_without_a_configured_user_id(self) -> None:
        """The granted token itself supplies the test user's open ID."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "user-access-token"
            runtime = self._runtime()
            captured = io.StringIO()

            with patch.object(self.module, "OAuthRuntime", return_value=runtime):
                with patch("builtins.input", side_effect=AssertionError("prompted")):
                    with redirect_stdout(captured):
                        await self.module._acquire(self._settings(output))

            self.assertEqual(output.read_text(encoding="utf-8"), "access-secret-value\n")
            self.assertIn("Test User (ou_expected)", captured.getvalue())

    def test_settings_read_persistent_values_from_process_environment(self) -> None:
        """Compose credentials stay separate from disposable gateway state."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                "HERMES_HOME": str(root),
                "FEISHU_APP_ID": "cli_file",
                "FEISHU_APP_SECRET": "secret-file",
                "FEISHU_DOMAIN": "lark",
                "FEISHU_E2E_USER_ACCESS_TOKEN_FILE": str(
                    root / "user-access-token"
                ),
            }
            with patch.dict(os.environ, environment, clear=True):
                settings = self.module._load_settings()

            self.assertEqual(settings.app_id, "cli_file")
            self.assertEqual(settings.app_secret, "secret-file")
            self.assertEqual(settings.brand, "lark")
            self.assertEqual(settings.output_path, root / "user-access-token")


if __name__ == "__main__":
    unittest.main()
