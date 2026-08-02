"""Pure unit tests for encrypted Feishu webhook handling."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SUPPORT_PATH = ROOT / "tests" / "test_ask_user_question_adapter.py"


def _load_adapter_test_support() -> types.ModuleType:
    """Load the offline adapter import support without collecting its tests."""
    name = "_hermes_lark_adapter_test_support"
    spec = importlib.util.spec_from_file_location(name, SUPPORT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _FakeRequestContent:
    """Expose aiohttp's bounded ``readexactly`` behavior for one body."""

    def __init__(self, body: bytes):
        self.body = body

    async def readexactly(self, size: int) -> bytes:
        if len(self.body) < size:
            raise asyncio.IncompleteReadError(self.body, size)
        return self.body[:size]


class _FakeWeb:
    """Capture aiohttp response data without opening a server."""

    @staticmethod
    def Response(*, status: int, text: str) -> Any:
        return SimpleNamespace(status=status, text=text, json=None)

    @staticmethod
    def json_response(data: Any, status: int = 200) -> Any:
        return SimpleNamespace(status=status, text=None, json=data)


class _FakeAESCipher:
    """Return test plaintexts while recording the SDK-compatible call shape."""

    plaintexts: dict[str, str] = {}
    calls: list[tuple[str, str]] = []

    def __init__(self, key: str):
        self.key = key

    def decrypt_str(self, encrypted: str) -> str:
        self.calls.append((self.key, encrypted))
        value = self.plaintexts[encrypted]
        if isinstance(value, Exception):
            raise value
        return value


class EncryptedWebhookTests(unittest.TestCase):
    """Verify decrypt, validation, challenge, signature, and dispatch order."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.support = _load_adapter_test_support()
        cls.tools, cls.adapter_module, cls.previous_modules = cls.support._load_modules()
        cls.original_web = cls.adapter_module.web
        cls.original_cipher = cls.adapter_module.AESCipher
        cls.adapter_module.web = _FakeWeb
        cls.adapter_module.AESCipher = _FakeAESCipher

    @classmethod
    def tearDownClass(cls) -> None:
        cls.adapter_module.web = cls.original_web
        cls.adapter_module.AESCipher = cls.original_cipher
        for name, previous in cls.previous_modules.items():
            if previous is cls.support._MISSING_MODULE:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
        sys.modules.pop("_hermes_lark_adapter_test_support", None)

    def setUp(self) -> None:
        _FakeAESCipher.plaintexts = {}
        _FakeAESCipher.calls = []
        self.dispatched: list[Any] = []
        self.p2p_chat_entered: list[Any] = []
        self.message_recalled: list[Any] = []
        self.anomalies: list[str] = []
        self.adapter = object.__new__(self.adapter_module.FeishuAdapter)
        self.adapter._app_id = "cli_app"
        self.adapter._webhook_path = "/feishu/webhook"
        self.adapter._encrypt_key = "encrypt-key"
        self.adapter._verification_token = "verification-token"
        self.adapter._check_webhook_rate_limit = lambda key: True
        self.adapter._record_webhook_anomaly = (
            lambda remote, status: self.anomalies.append(status)
        )
        self.adapter._clear_webhook_anomaly = lambda remote: None
        self.adapter._on_message_event = self.dispatched.append
        self.adapter._on_message_read_event = lambda data: None
        self.adapter._on_bot_added_to_chat = lambda data: None
        self.adapter._on_bot_removed_from_chat = lambda data: None
        self.adapter._on_p2p_chat_entered = self.p2p_chat_entered.append
        self.adapter._on_message_recalled = self.message_recalled.append
        self.adapter._on_reaction_event = lambda event_type, data: None
        self.adapter._on_card_action_trigger = lambda data: None
        self.adapter._on_drive_comment_event = lambda data: None
        self.adapter._on_meeting_invited_event = lambda data: None

    def test_encrypted_challenge_decrypts_before_token_check(self) -> None:
        _FakeAESCipher.plaintexts["encrypted-challenge"] = json.dumps(
            {
                "type": "url_verification",
                "token": "verification-token",
                "challenge": "challenge-code",
            }
        )
        response = asyncio.run(
            self.adapter._handle_webhook_request(
                self._request(
                    {"encrypt": "encrypted-challenge", "token": "wrong-outer-token"},
                    signed=True,
                )
            )
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.json, {"challenge": "challenge-code"})
        self.assertEqual(
            _FakeAESCipher.calls,
            [("encrypt-key", "encrypted-challenge")],
        )

    def test_invalid_signature_is_rejected_before_json_parse(self) -> None:
        response = asyncio.run(
            self.adapter._handle_webhook_request(
                self._raw_request(
                    b'{"malformed"',
                    signed=True,
                    signature_body=b"{}",
                )
            )
        )

        self.assertEqual(response.status, 401)
        self.assertEqual(response.text, "Invalid signature")
        self.assertEqual(self.anomalies, ["401-sig"])

    def test_invalid_signature_is_rejected_before_decryption(self) -> None:
        _FakeAESCipher.plaintexts["must-not-decrypt"] = json.dumps(
            {
                "schema": "2.0",
                "header": {
                    "event_type": "im.message.receive_v1",
                    "token": "verification-token",
                },
                "event": {},
            }
        )
        response = asyncio.run(
            self.adapter._handle_webhook_request(
                self._request(
                    {"encrypt": "must-not-decrypt"},
                    signed=True,
                    signature_body=b"{}",
                )
            )
        )

        self.assertEqual(response.status, 401)
        self.assertEqual(_FakeAESCipher.calls, [])
        self.assertEqual(self.dispatched, [])
        self.assertEqual(self.anomalies, ["401-sig"])

    def test_invalid_signature_is_rejected_before_token_and_challenge(self) -> None:
        response = asyncio.run(
            self.adapter._handle_webhook_request(
                self._request(
                    {
                        "type": "url_verification",
                        "token": "wrong-token",
                        "challenge": "must-not-be-reflected",
                    },
                    signed=True,
                    signature_body=b"{}",
                )
            )
        )

        self.assertEqual(response.status, 401)
        self.assertIsNone(response.json)
        self.assertEqual(self.anomalies, ["401-sig"])

    def test_encrypted_event_uses_raw_envelope_signature_and_dispatches(self) -> None:
        decrypted = {
            "schema": "2.0",
            "header": {
                "event_type": "im.message.receive_v1",
                "token": "verification-token",
            },
            "event": {"message": {"message_id": "om_encrypted"}},
        }
        _FakeAESCipher.plaintexts["encrypted-event"] = json.dumps(decrypted)
        envelope = {"encrypt": "encrypted-event"}

        wrong_signature = self._request(
            envelope,
            signed=True,
            signature_body=json.dumps(decrypted).encode("utf-8"),
        )
        rejected = asyncio.run(self.adapter._handle_webhook_request(wrong_signature))
        accepted = asyncio.run(
            self.adapter._handle_webhook_request(
                self._request(envelope, signed=True)
            )
        )

        self.assertEqual(rejected.status, 401)
        self.assertEqual(accepted.status, 200)
        self.assertEqual(accepted.json, {"code": 0, "msg": "ok"})
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(
            self.dispatched[0].event.message.message_id,
            "om_encrypted",
        )
        self.assertEqual(
            _FakeAESCipher.calls,
            [("encrypt-key", "encrypted-event")],
        )

    def test_decrypted_verification_token_is_enforced(self) -> None:
        _FakeAESCipher.plaintexts["bad-token"] = json.dumps(
            {
                "schema": "2.0",
                "header": {
                    "event_type": "im.message.receive_v1",
                    "token": "wrong-token",
                },
                "event": {},
            }
        )
        response = asyncio.run(
            self.adapter._handle_webhook_request(
                self._request(
                    {"encrypt": "bad-token", "token": "verification-token"},
                    signed=True,
                )
            )
        )

        self.assertEqual(response.status, 401)
        self.assertEqual(self.dispatched, [])
        self.assertIn("401-token", self.anomalies)

    def test_invalid_encrypted_plaintext_returns_400_without_dispatch(self) -> None:
        _FakeAESCipher.plaintexts["broken"] = ValueError("invalid PKCS7 padding")
        response = asyncio.run(
            self.adapter._handle_webhook_request(
                self._request({"encrypt": "broken"}, signed=True)
            )
        )

        self.assertEqual(response.status, 400)
        self.assertEqual(
            response.json,
            {"code": 400, "msg": "invalid encrypted payload"},
        )
        self.assertEqual(self.dispatched, [])
        self.assertIn("400-encrypted", self.anomalies)

    def test_webhook_dispatches_p2p_chat_entered(self) -> None:
        response = asyncio.run(
            self.adapter._handle_webhook_request(
                self._request(
                    {
                        "schema": "2.0",
                        "header": {
                            "event_type": "im.chat.access_event.bot_p2p_chat_entered_v1",
                            "token": "verification-token",
                        },
                        "event": {"chat_id": "oc_p2p"},
                    },
                    signed=True,
                )
            )
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.p2p_chat_entered), 1)
        self.assertEqual(self.p2p_chat_entered[0].event.chat_id, "oc_p2p")
        self.assertEqual(self.message_recalled, [])

    def test_webhook_dispatches_message_recalled(self) -> None:
        response = asyncio.run(
            self.adapter._handle_webhook_request(
                self._request(
                    {
                        "schema": "2.0",
                        "header": {
                            "event_type": "im.message.recalled_v1",
                            "token": "verification-token",
                        },
                        "event": {"message_id": "om_recalled"},
                    },
                    signed=True,
                )
            )
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.message_recalled), 1)
        self.assertEqual(
            self.message_recalled[0].event.message_id,
            "om_recalled",
        )
        self.assertEqual(self.p2p_chat_entered, [])

    def _request(
        self,
        payload: dict[str, Any],
        *,
        signed: bool,
        signature_body: bytes | None = None,
    ) -> Any:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return self._raw_request(
            body,
            signed=signed,
            signature_body=signature_body,
        )

    def _raw_request(
        self,
        body: bytes,
        *,
        signed: bool,
        signature_body: bytes | None = None,
    ) -> Any:
        headers = {"Content-Type": "application/json"}
        if signed:
            timestamp = "1700000000"
            nonce = "nonce-1"
            signed_body = body if signature_body is None else signature_body
            digest = hashlib.sha256(
                f"{timestamp}{nonce}{self.adapter._encrypt_key}".encode("utf-8")
                + signed_body
            ).hexdigest()
            headers.update(
                {
                    "x-lark-request-timestamp": timestamp,
                    "x-lark-request-nonce": nonce,
                    "x-lark-signature": digest,
                }
            )
        return SimpleNamespace(
            remote="127.0.0.1",
            content_length=len(body),
            headers=headers,
            content=_FakeRequestContent(body),
        )


if __name__ == "__main__":
    unittest.main()
