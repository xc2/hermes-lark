"""Offline tests for the two-phase live gateway restart acceptance case."""

from __future__ import annotations

import json
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests.e2e import test_live_gateway_restart as restart


class GatewayRestartCheckpointTests(unittest.TestCase):
    """Specify the durable hand-off between prepare and verify phases."""

    def _checkpoint(self) -> restart.RestartCheckpoint:
        """Return one credential-free checkpoint fixture."""
        return restart.RestartCheckpoint(
            version=1,
            run_id="run-123",
            chat_id="oc_dm",
            root_id="om_root",
            thread_id="omt_thread",
            session_id="20260731_120000_abcdef12",
            session_key="agent:main:feishu:dm:oc_dm:om_root",
            root_marker="HERMES-E2E-run-123-RESTART-ROOT",
            root_ack_marker="HERMES-E2E-run-123-RESTART-ACK",
            context_value="HERMES-E2E-run-123-RESTART-CONTEXT",
            prepared_at_ms=1_785_500_000_000,
        )

    def test_checkpoint_round_trips_atomically_with_private_permissions(self) -> None:
        """The phase hand-off is complete, private, and credential-free."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-restart.json"
            path.write_text("old-incomplete-state", encoding="utf-8")

            restart.write_checkpoint(path, self._checkpoint())

            self.assertEqual(restart.read_checkpoint(path), self._checkpoint())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                set(payload),
                {
                    "version",
                    "run_id",
                    "chat_id",
                    "root_id",
                    "thread_id",
                    "session_id",
                    "session_key",
                    "root_marker",
                    "root_ack_marker",
                    "context_value",
                    "prepared_at_ms",
                },
            )
            serialized = json.dumps(payload, sort_keys=True).lower()
            for credential_name in (
                "access_token",
                "app_secret",
                "authorization",
                "refresh_token",
            ):
                self.assertNotIn(credential_name, serialized)
            self.assertEqual(
                [item.name for item in Path(directory).iterdir()],
                [path.name],
            )

    def test_checkpoint_reader_rejects_unexpected_credential_fields(self) -> None:
        """A hand-written checkpoint cannot smuggle credentials into phase two."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-restart.json"
            payload = {
                **self._checkpoint().to_dict(),
                "access_token": "u-test-credential",
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            path.chmod(0o600)

            with self.assertRaisesRegex(AssertionError, "unexpected fields"):
                restart.read_checkpoint(path)

    def test_phase_environment_selects_one_method_or_allows_named_method(self) -> None:
        """An env phase filters module runs while named methods need no flag."""
        self.assertTrue(restart.phase_is_enabled("prepare", {}))
        self.assertTrue(restart.phase_is_enabled("verify", {}))
        self.assertTrue(
            restart.phase_is_enabled(
                "prepare",
                {"FEISHU_E2E_RESTART_PHASE": "prepare"},
            )
        )
        self.assertFalse(
            restart.phase_is_enabled(
                "verify",
                {"FEISHU_E2E_RESTART_PHASE": "prepare"},
            )
        )
        with self.assertRaisesRegex(AssertionError, "prepare or verify"):
            restart.phase_is_enabled(
                "prepare",
                {"FEISHU_E2E_RESTART_PHASE": "invalid"},
            )

    def test_continuity_requires_the_same_session_key_root_and_transcript(self) -> None:
        """Phase two proves durable reuse instead of merely receiving a reply."""
        checkpoint = self._checkpoint()
        persisted = restart.PersistedSession(
            session_id=checkpoint.session_id,
            session_key=checkpoint.session_key,
            chat_id=checkpoint.chat_id,
            chat_type="dm",
            root_id=checkpoint.root_id,
            transcript="\n".join(
                (
                    checkpoint.root_marker,
                    checkpoint.root_ack_marker,
                    "HERMES-E2E-run-123-RESTART-FOLLOW-UP",
                    f"HERMES_E2E_CONTEXT:{checkpoint.context_value}",
                )
            ),
        )

        restart.assert_restart_continuity(
            checkpoint,
            persisted,
            required_markers=(
                "HERMES-E2E-run-123-RESTART-FOLLOW-UP",
                f"HERMES_E2E_CONTEXT:{checkpoint.context_value}",
            ),
        )

        for changed, message in (
            (replace(persisted, session_id="new-session"), "session_id"),
            (replace(persisted, session_key="new-key"), "session_key"),
            (replace(persisted, root_id="om_other"), "root"),
            (replace(persisted, transcript="missing"), "transcript"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(AssertionError, message):
                    restart.assert_restart_continuity(
                        checkpoint,
                        changed,
                        required_markers=(
                            "HERMES-E2E-run-123-RESTART-FOLLOW-UP",
                        ),
                    )


if __name__ == "__main__":
    unittest.main()
