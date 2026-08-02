"""Two-phase live acceptance test for Feishu session restart continuity.

The prepare phase creates a real DM thread and persists a credential-free
checkpoint under ``/opt/data``.  A host-side runner restarts the gateway, then
the verify phase proves that the same Hermes session and transcript resumed.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import tempfile
import time
import unittest
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.e2e import test_live_thread_model as live


_LIVE_ENABLED = os.environ.get("FEISHU_E2E") == "1"
_CHECKPOINT_PATH = Path("/opt/data/hermes-lark-e2e-gateway-restart.json")
_CHECKPOINT_VERSION = 1
_CHECKPOINT_FIELDS = frozenset(
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
    }
)


@dataclass(frozen=True)
class RestartCheckpoint:
    """Describe the durable, credential-free hand-off between test phases."""

    version: int
    run_id: str
    chat_id: str
    root_id: str
    thread_id: str
    session_id: str
    session_key: str
    root_marker: str
    root_ack_marker: str
    context_value: str
    prepared_at_ms: int

    def to_dict(self) -> dict[str, str | int]:
        """Return the exact JSON-safe checkpoint schema."""
        return {
            "version": self.version,
            "run_id": self.run_id,
            "chat_id": self.chat_id,
            "root_id": self.root_id,
            "thread_id": self.thread_id,
            "session_id": self.session_id,
            "session_key": self.session_key,
            "root_marker": self.root_marker,
            "root_ack_marker": self.root_ack_marker,
            "context_value": self.context_value,
            "prepared_at_ms": self.prepared_at_ms,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> RestartCheckpoint:
        """Validate and construct one checkpoint from decoded JSON."""
        fields = set(payload)
        unexpected = sorted(fields - _CHECKPOINT_FIELDS)
        if unexpected:
            raise AssertionError(
                "restart checkpoint has unexpected fields: "
                + ", ".join(unexpected)
            )
        missing = sorted(_CHECKPOINT_FIELDS - fields)
        if missing:
            raise AssertionError(
                "restart checkpoint is missing fields: " + ", ".join(missing)
            )

        if type(payload["version"]) is not int:
            raise AssertionError("restart checkpoint version must be an integer")
        if payload["version"] != _CHECKPOINT_VERSION:
            raise AssertionError("unsupported restart checkpoint version")
        if type(payload["prepared_at_ms"]) is not int or payload["prepared_at_ms"] <= 0:
            raise AssertionError(
                "restart checkpoint prepared_at_ms must be a positive integer"
            )
        string_fields = _CHECKPOINT_FIELDS - {"version", "prepared_at_ms"}
        invalid_strings = sorted(
            field
            for field in string_fields
            if not isinstance(payload[field], str) or not payload[field]
        )
        if invalid_strings:
            raise AssertionError(
                "restart checkpoint has invalid string fields: "
                + ", ".join(invalid_strings)
            )

        return cls(
            version=payload["version"],
            run_id=payload["run_id"],
            chat_id=payload["chat_id"],
            root_id=payload["root_id"],
            thread_id=payload["thread_id"],
            session_id=payload["session_id"],
            session_key=payload["session_key"],
            root_marker=payload["root_marker"],
            root_ack_marker=payload["root_ack_marker"],
            context_value=payload["context_value"],
            prepared_at_ms=payload["prepared_at_ms"],
        )


@dataclass(frozen=True)
class PersistedSession:
    """Expose the persisted session facts required by restart assertions."""

    session_id: str
    session_key: str
    chat_id: str
    chat_type: str
    root_id: str
    transcript: str


def write_checkpoint(path: Path, checkpoint: RestartCheckpoint) -> None:
    """Atomically persist one private restart checkpoint."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor = -1
        with stream:
            json.dump(
                checkpoint.to_dict(),
                stream,
                ensure_ascii=False,
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def read_checkpoint(path: Path) -> RestartCheckpoint:
    """Read one private checkpoint without accepting additional fields."""
    try:
        metadata = path.lstat()
    except OSError as error:
        raise AssertionError(
            f"restart checkpoint is unavailable ({type(error).__name__})"
        ) from None
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise AssertionError("restart checkpoint must be a regular file")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise AssertionError("restart checkpoint permissions must be 0600")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AssertionError(
            f"restart checkpoint is not readable JSON ({type(error).__name__})"
        ) from None
    if not isinstance(payload, dict):
        raise AssertionError("restart checkpoint JSON must be an object")
    return RestartCheckpoint.from_mapping(payload)


def phase_is_enabled(
    expected: str,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Select one phase while permitting explicit unittest method runs."""
    if expected not in {"prepare", "verify"}:
        raise AssertionError("expected phase must be prepare or verify")
    source = os.environ if environ is None else environ
    configured = str(source.get("FEISHU_E2E_RESTART_PHASE") or "").strip().lower()
    if not configured:
        return True
    if configured not in {"prepare", "verify"}:
        raise AssertionError(
            "FEISHU_E2E_RESTART_PHASE must be prepare or verify"
        )
    return configured == expected


def assert_restart_continuity(
    checkpoint: RestartCheckpoint,
    persisted: PersistedSession,
    *,
    required_markers: tuple[str, ...],
) -> None:
    """Assert that phase two resumed the phase-one session and transcript."""
    if persisted.session_id != checkpoint.session_id:
        raise AssertionError("session_id changed across the gateway restart")
    if persisted.session_key != checkpoint.session_key:
        raise AssertionError("session_key changed across the gateway restart")
    if persisted.chat_id != checkpoint.chat_id:
        raise AssertionError("chat_id changed across the gateway restart")
    if persisted.chat_type != "dm":
        raise AssertionError("chat_type changed across the gateway restart")
    if persisted.root_id != checkpoint.root_id:
        raise AssertionError("thread root changed across the gateway restart")
    markers = (
        checkpoint.root_marker,
        checkpoint.root_ack_marker,
        *required_markers,
    )
    missing = [marker for marker in markers if marker not in persisted.transcript]
    if missing:
        raise AssertionError(
            "persisted transcript is missing restart markers: "
            + ", ".join(missing)
        )


def _wait_for_persisted_session(
    *,
    db_path: Path,
    chat_id: str,
    root_id: str,
    transcript_markers: tuple[str, ...],
    timeout_seconds: float,
) -> PersistedSession:
    """Read a committed DM session through Hermes' public state API."""
    if not db_path.is_file():
        raise AssertionError(f"Hermes session database is missing at {db_path}")

    from gateway.config import Platform
    from gateway.session import SessionSource, build_session_key
    from hermes_state import SessionDB

    source = SessionSource(
        platform=Platform.FEISHU,
        chat_id=chat_id,
        chat_type="dm",
        thread_id=root_id,
    )
    expected_key = build_session_key(source)

    def observe() -> PersistedSession | None:
        database = SessionDB(db_path=db_path, read_only=True)
        try:
            session_id = database.find_session_by_origin(
                platform="feishu",
                chat_id=chat_id,
                thread_id=root_id,
            )
            if not session_id:
                return None
            row = database.get_session(session_id)
            if not isinstance(row, dict):
                return None
            messages = database.get_messages(session_id)
        finally:
            database.close()

        transcript = "\n".join(
            live._hermes_message_text(message)
            for message in messages
            if isinstance(message, dict)
        )
        if any(marker not in transcript for marker in transcript_markers):
            return None
        if row.get("source") != "feishu":
            raise AssertionError("persisted session source is not feishu")
        if row.get("chat_id") != chat_id or row.get("chat_type") != "dm":
            raise AssertionError("persisted session chat identity changed")
        if row.get("thread_id") != root_id:
            raise AssertionError("persisted session thread root changed")
        if row.get("session_key") != expected_key:
            raise AssertionError("persisted session_key differs from public builder")
        return PersistedSession(
            session_id=str(session_id),
            session_key=str(row["session_key"]),
            chat_id=str(row["chat_id"]),
            chat_type=str(row["chat_type"]),
            root_id=str(row["thread_id"]),
            transcript=transcript,
        )

    return live._wait_until(
        observe,
        timeout_seconds=timeout_seconds,
        description=f"persisted Hermes session for root {root_id}",
        interval_seconds=0.25,
    )


def _wait_for_bot_reply(
    *,
    api: live.FeishuOpenApi,
    root_id: str,
    expected_text: str,
    after_ms: int,
    timeout_seconds: float,
) -> tuple[dict[str, Any], str]:
    """Wait for one app reply under the canonical Feishu thread root."""
    observed_thread_id = ""

    def observe() -> tuple[dict[str, Any], str] | None:
        nonlocal observed_thread_id
        root = api.get_message(root_id)
        observed_thread_id = str(root.get("thread_id") or observed_thread_id)
        if not observed_thread_id:
            return None
        messages = api.list_messages(
            container_type="thread",
            container_id=observed_thread_id,
        )
        for message in messages:
            if (
                live._sender_type(message) == "app"
                and live._sender_id(message) == api.app_id
                and str(message.get("root_id") or "") == root_id
                and live._message_time_ms(message) >= after_ms
                and expected_text in live._message_text(message)
            ):
                return message, observed_thread_id
        return None

    return live._wait_until(
        observe,
        timeout_seconds=timeout_seconds,
        description=f"bot reply containing {expected_text} under root {root_id}",
    )


def _assert_reply_thread_ids(
    reply: Mapping[str, Any],
    *,
    root_id: str,
    thread_id: str,
) -> None:
    """Assert the fixed Slack-style IDs exposed by one Feishu reply."""
    if str(reply.get("root_id") or "") != root_id:
        raise AssertionError("bot reply root_id differs from the canonical root")
    if str(reply.get("parent_id") or "") != root_id:
        raise AssertionError("bot reply parent_id differs from the canonical root")
    if str(reply.get("thread_id") or "") != thread_id:
        raise AssertionError("bot reply thread_id changed")
    if not thread_id.startswith("omt_"):
        raise AssertionError("Feishu thread_id is not a native omt_ identifier")
    if not str(reply.get("message_id") or "").startswith("om_"):
        raise AssertionError("Feishu reply message_id is invalid")


@unittest.skipUnless(
    _LIVE_ENABLED,
    "live tenant test; set FEISHU_E2E=1 explicitly",
)
class LiveGatewayRestartTests(unittest.TestCase):
    """Prove a real Feishu DM session survives a gateway process restart."""

    @classmethod
    def setUpClass(cls) -> None:
        required = ("FEISHU_APP_ID", "FEISHU_APP_SECRET")
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise AssertionError(
                "FEISHU_E2E=1 but required variables are missing: "
                + ", ".join(missing)
            )
        user_token = live._read_user_access_token()
        if not user_token:
            raise AssertionError("run acquire_user_access_token.py before live E2E")

        cls.timeout_seconds = live._positive_float_env(
            "FEISHU_E2E_TIMEOUT_SECONDS",
            120,
        )
        cls.session_db_path = (
            Path(os.environ.get("HERMES_HOME", "/opt/data")) / "state.db"
        )
        cls.checkpoint_path = _CHECKPOINT_PATH
        cls.api = live.FeishuOpenApi(
            app_id=os.environ["FEISHU_APP_ID"],
            app_secret=os.environ["FEISHU_APP_SECRET"],
            domain=os.environ.get("FEISHU_DOMAIN", "feishu"),
            user_access_token=user_token,
        )
        cls.api.authenticate()

    def test_prepare_restart_checkpoint(self) -> None:
        """Create a real thread and record its committed pre-restart session."""
        if not phase_is_enabled("prepare"):
            self.skipTest("verify phase selected")

        configured_run_id = str(os.environ.get("FEISHU_E2E_RUN_ID") or "").strip()
        run_id = configured_run_id or (
            f"{time.strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"
        )
        if re.fullmatch(r"[A-Za-z0-9_.:-]+", run_id) is None:
            raise AssertionError(
                "FEISHU_E2E_RUN_ID may contain only letters, digits, dot, colon, "
                "underscore, and hyphen"
            )

        user = self.api.user_info()
        chat_id = self.api.resolve_dm_chat_id(str(user["open_id"]), run_id)
        root_marker = f"HERMES-E2E-{run_id}-RESTART-ROOT"
        root_ack_marker = f"HERMES-E2E-{run_id}-RESTART-ACK"
        context_value = f"HERMES-E2E-{run_id}-RESTART-CONTEXT"
        root = self.api.create_text_message(
            chat_id,
            "\n".join(
                (
                    root_marker,
                    f"HERMES_E2E_REMEMBER:{context_value}",
                    f"HERMES_E2E_EXPECT:{root_ack_marker}",
                    f"Remember {context_value}. Reply exactly {root_ack_marker}.",
                )
            ),
        )
        root_id = str(root.get("message_id") or "")
        if not root_id.startswith("om_"):
            raise AssertionError("prepare phase did not create a Feishu root message")
        root_reply, thread_id = _wait_for_bot_reply(
            api=self.api,
            root_id=root_id,
            expected_text=root_ack_marker,
            after_ms=live._message_time_ms(root),
            timeout_seconds=self.timeout_seconds,
        )
        _assert_reply_thread_ids(
            root_reply,
            root_id=root_id,
            thread_id=thread_id,
        )
        persisted = _wait_for_persisted_session(
            db_path=self.session_db_path,
            chat_id=chat_id,
            root_id=root_id,
            transcript_markers=(root_marker, root_ack_marker),
            timeout_seconds=self.timeout_seconds,
        )
        checkpoint = RestartCheckpoint(
            version=_CHECKPOINT_VERSION,
            run_id=run_id,
            chat_id=chat_id,
            root_id=root_id,
            thread_id=thread_id,
            session_id=persisted.session_id,
            session_key=persisted.session_key,
            root_marker=root_marker,
            root_ack_marker=root_ack_marker,
            context_value=context_value,
            prepared_at_ms=int(time.time() * 1000),
        )
        write_checkpoint(self.checkpoint_path, checkpoint)
        print(
            "[E2E restart prepare] "
            f"chat_id={chat_id}; root_id={root_id}; thread_id={thread_id}; "
            f"session_id={persisted.session_id}",
            flush=True,
        )

    def test_verify_restart_continuity(self) -> None:
        """Resume the exact thread and transcript after the host restart."""
        if not phase_is_enabled("verify"):
            self.skipTest("prepare phase selected")

        checkpoint = read_checkpoint(self.checkpoint_path)
        follow_up_marker = (
            f"HERMES-E2E-{checkpoint.run_id}-RESTART-FOLLOW-UP"
        )
        expected_context = f"HERMES_E2E_CONTEXT:{checkpoint.context_value}"
        follow_up = self.api.reply_text_in_thread(
            checkpoint.root_id,
            "\n".join(
                (
                    follow_up_marker,
                    "HERMES_E2E_RECALL",
                    "Recall the HERMES_E2E_REMEMBER value from the root message. "
                    "Reply exactly HERMES_E2E_CONTEXT:<that value>.",
                )
            ),
        )
        reply, thread_id = _wait_for_bot_reply(
            api=self.api,
            root_id=checkpoint.root_id,
            expected_text=expected_context,
            after_ms=live._message_time_ms(follow_up),
            timeout_seconds=self.timeout_seconds,
        )
        if thread_id != checkpoint.thread_id:
            raise AssertionError("native thread_id changed across gateway restart")
        _assert_reply_thread_ids(
            reply,
            root_id=checkpoint.root_id,
            thread_id=checkpoint.thread_id,
        )
        persisted = _wait_for_persisted_session(
            db_path=self.session_db_path,
            chat_id=checkpoint.chat_id,
            root_id=checkpoint.root_id,
            transcript_markers=(
                checkpoint.root_marker,
                checkpoint.root_ack_marker,
                follow_up_marker,
                expected_context,
            ),
            timeout_seconds=self.timeout_seconds,
        )
        assert_restart_continuity(
            checkpoint,
            persisted,
            required_markers=(follow_up_marker, expected_context),
        )
        self.checkpoint_path.unlink()
        print(
            "[E2E restart verify] "
            f"chat_id={checkpoint.chat_id}; root_id={checkpoint.root_id}; "
            f"thread_id={checkpoint.thread_id}; session_id={persisted.session_id}",
            flush=True,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
