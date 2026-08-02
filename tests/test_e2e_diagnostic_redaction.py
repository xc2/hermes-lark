"""Tests for credential redaction in E2E failure diagnostics."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REDACTION_SCRIPT = ROOT / "tests" / "e2e" / "redact_diagnostics.sed"


class E2EDiagnosticRedactionTests(unittest.TestCase):
    """Ensure common structured and unstructured credentials never reach logs."""

    def test_redacts_credentials_without_removing_safe_diagnostics(self) -> None:
        """JSON, YAML, dotenv, URL, header, and log forms are redacted."""
        sed = shutil.which("sed")
        if sed is None:
            self.skipTest("sed is unavailable")

        sensitive_values = (
            "json-app-secret",
            "json-access-token",
            "python-refresh-token",
            "tuple-access-token",
            "yaml secret with spaces",
            "dotenv-secret",
            "query-token",
            "cookie-secret",
            "bearer-secret",
            "basic-secret",
            "custom-scheme-secret",
            "plain-ticket",
            "device-code-secret",
            "oauth-code-secret",
            "oauth-state-secret",
        )
        diagnostic_input = "\n".join(
            (
                '{"app_secret":"json-app-secret","safe":"visible-json"}',
                '{"access_token": "json-access-token"}',
                "{'refresh_token': 'python-refresh-token'}",
                "headers=[('access_token', 'tuple-access-token')]",
                "app_secret: yaml secret with spaces",
                "FEISHU_APP_SECRET=dotenv-secret",
                "GET https://example.test/callback?safe=visible-query"
                "&access_token=query-token&mode=test",
                "GET https://example.test/oauth?code=oauth-code-secret"
                "&state=oauth-state-secret&safe=visible-state",
                "gateway | Cookie: session=cookie-secret; theme=dark",
                "gateway | Authorization: Bearer bearer-secret",
                'headers={"Authorization": "Basic basic-secret"}',
                "gateway | Authorization: Custom custom-scheme-secret",
                "gateway | ticket=plain-ticket",
                "device_code=device-code-secret status=pending",
                "gateway | code=230001 chat_id=oc_safe status=failed",
            )
        )

        completed = subprocess.run(
            [sed, "-E", "-f", str(REDACTION_SCRIPT)],
            input=diagnostic_input,
            text=True,
            capture_output=True,
            check=True,
        )

        for sensitive in sensitive_values:
            self.assertNotIn(sensitive, completed.stdout)
        self.assertIn("<redacted>", completed.stdout)
        self.assertIn("visible-json", completed.stdout)
        self.assertIn("visible-query", completed.stdout)
        self.assertIn("visible-state", completed.stdout)
        self.assertIn("code=230001", completed.stdout)
        self.assertIn("chat_id=oc_safe", completed.stdout)


if __name__ == "__main__":
    unittest.main()
