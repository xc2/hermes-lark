"""Run the Hermes command-authorization integration probe in isolation."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "hermes_command_authorization_probe.py"


class HermesCommandAuthorizationProcessTests(unittest.TestCase):
    """Keep real Hermes imports out of the offline test process."""

    def test_real_hermes_gateway_command_authorization_contract(self) -> None:
        """Run every authenticated and rejected dispatch case in Hermes 0.19.1."""
        with tempfile.TemporaryDirectory(prefix="hermes-lark-command-auth-") as home:
            environment = os.environ.copy()
            environment["HERMES_HOME"] = home
            result = subprocess.run(
                [sys.executable, str(PROBE), "-v"],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )

        output = f"{result.stdout}\n{result.stderr}".strip()
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("Ran 4 tests", output)
        self.assertIn("OK", output)


if __name__ == "__main__":
    unittest.main()
