"""Run the Hermes durable-admission integration probe in isolation."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "hermes_admission_probe.py"


class HermesAdmissionProcessTests(unittest.TestCase):
    """Keep real Hermes imports out of the offline test process."""

    def test_real_hermes_durable_admission_contract(self) -> None:
        """Admit, replay, and answer Feishu turns through real Hermes."""
        with tempfile.TemporaryDirectory(prefix="hermes-lark-admission-") as home:
            environment = os.environ.copy()
            environment["HERMES_HOME"] = home
            result = subprocess.run(
                [sys.executable, str(PROBE), "-v"],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )

        output = f"{result.stdout}\n{result.stderr}".strip()
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("Ran 3 tests", output)
        self.assertIn("OK", output)
