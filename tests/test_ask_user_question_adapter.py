"""Behavioral tests for the native AskUserQuestion adapter lifecycle."""

from __future__ import annotations

import asyncio
import enum
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "hermes_lark"
_MISSING_MODULE = object()
_MANAGED_MODULES = (
    "gateway",
    "gateway.config",
    "gateway.platforms",
    "gateway.platforms.base",
    "gateway.status",
    "hermes_constants",
    "utils",
    "hermes_lark",
    "hermes_lark.openclaw_tools",
    "hermes_lark.adapter",
)


def _install_hermes_stubs() -> None:
    """Install the small Hermes surface needed to import the adapter offline."""

    class Platform(enum.Enum):
        FEISHU = "feishu"

    @dataclass
    class PlatformConfig:
        extra: dict[str, Any] = field(default_factory=dict)

    class MessageType(enum.Enum):
        TEXT = "text"
        COMMAND = "command"
        PHOTO = "photo"
        VIDEO = "video"
        DOCUMENT = "document"
        AUDIO = "audio"
        VOICE = "voice"

    class ProcessingOutcome(enum.Enum):
        SUCCESS = "success"
        FAILURE = "failure"

    @dataclass
    class SendResult:
        success: bool
        message_id: str | None = None
        error: str | None = None

    @dataclass
    class MessageEvent:
        text: str
        message_type: MessageType = MessageType.TEXT
        source: Any = None
        raw_message: Any = None
        message_id: str | None = None
        media_urls: list[str] = field(default_factory=list)
        media_types: list[str] = field(default_factory=list)
        reply_to_message_id: str | None = None
        reply_to_text: str | None = None
        channel_prompt: str | None = None
        channel_context: str | None = None
        timestamp: Any = None

        def is_command(self) -> bool:
            return self.message_type is MessageType.COMMAND

    class BasePlatformAdapter:
        def __init__(self, config: PlatformConfig, platform: Platform):
            self.config = config
            self.platform = platform
            self._running = False

        def build_source(self, chat_id: str, **kwargs: Any) -> Any:
            return SimpleNamespace(chat_id=chat_id, platform=self.platform, **kwargs)

        def _mark_connected(self) -> None:
            self._running = True

        def _mark_disconnected(self) -> None:
            self._running = False

    async def cache_value(*args: Any, **kwargs: Any) -> None:
        return None

    gateway = types.ModuleType("gateway")
    gateway.__path__ = []
    config = types.ModuleType("gateway.config")
    config.Platform = Platform
    config.PlatformConfig = PlatformConfig
    platforms = types.ModuleType("gateway.platforms")
    platforms.__path__ = []
    base = types.ModuleType("gateway.platforms.base")
    base.BasePlatformAdapter = BasePlatformAdapter
    base.MessageEvent = MessageEvent
    base.MessageType = MessageType
    base.ProcessingOutcome = ProcessingOutcome
    base.SendResult = SendResult
    base.SUPPORTED_DOCUMENT_TYPES = {".pdf": "application/pdf"}
    base.cache_document_from_bytes = cache_value
    base.cache_image_from_url = cache_value
    base.cache_audio_from_bytes = cache_value
    base.cache_image_from_bytes = cache_value
    status = types.ModuleType("gateway.status")
    status.acquire_scoped_lock = lambda *args, **kwargs: (True, None)
    status.release_scoped_lock = lambda *args, **kwargs: None

    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: Path(tempfile.gettempdir())
    utils = types.ModuleType("utils")
    utils.atomic_json_write = lambda *args, **kwargs: None
    utils.env_float = lambda name, default, **kwargs: default
    utils.env_int = lambda name, default, **kwargs: default

    sys.modules.update(
        {
            "gateway": gateway,
            "gateway.config": config,
            "gateway.platforms": platforms,
            "gateway.platforms.base": base,
            "gateway.status": status,
            "hermes_constants": constants,
            "utils": utils,
        }
    )


def _load_package_module(name: str, path: Path) -> types.ModuleType:
    """Load one repository module beneath a lightweight package object."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_modules() -> tuple[
    types.ModuleType,
    types.ModuleType,
    dict[str, object],
]:
    """Load the tool registry and adapter without installing Hermes."""
    previous = {
        name: sys.modules.get(name, _MISSING_MODULE) for name in _MANAGED_MODULES
    }
    _install_hermes_stubs()
    package = types.ModuleType("hermes_lark")
    package.__path__ = [str(PACKAGE_DIR)]
    sys.modules["hermes_lark"] = package
    tools = _load_package_module(
        "hermes_lark.openclaw_tools",
        PACKAGE_DIR / "openclaw_tools.py",
    )
    adapter = _load_package_module(
        "hermes_lark.adapter",
        PACKAGE_DIR / "adapter.py",
    )
    return tools, adapter, previous


class _FakeCallbackValue:
    """Minimal lark-oapi callback model for response assertions."""

    def __init__(self) -> None:
        self.type = None
        self.content = None
        self.data = None
        self.toast = None
        self.card = None


class AskUserQuestionAdapterTests(unittest.TestCase):
    """Verify card rendering, callback security, and synthetic injection."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tools, cls.adapter_module, cls.previous_modules = _load_modules()
        cls.adapter_module.P2CardActionTriggerResponse = _FakeCallbackValue
        cls.adapter_module.CallBackToast = _FakeCallbackValue
        cls.adapter_module.CallBackCard = _FakeCallbackValue

    @classmethod
    def tearDownClass(cls) -> None:
        for name, previous in cls.previous_modules.items():
            if previous is _MISSING_MODULE:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    def setUp(self) -> None:
        with self.tools._state_lock:
            for timer in self.tools._interaction_expiry_timers.values():
                timer.cancel()
            self.tools._pending_interactions.clear()
            self.tools._interaction_hosts.clear()
            self.tools._interaction_expiry_hosts.clear()
            self.tools._interaction_expiry_timers.clear()

    def _new_adapter(self) -> Any:
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter._account_id = "work"
        adapter._namespace_account = False
        adapter._openclaw_interaction_host = adapter._begin_openclaw_interaction
        adapter._openclaw_submitted_tokens = set()
        adapter._openclaw_interaction_messages = {}
        adapter._openclaw_submitted_lock = __import__("threading").Lock()
        adapter.platform = self.adapter_module.Platform.FEISHU
        return adapter

    def _store_interaction(
        self,
        *,
        chat_id: str = "oc_chat",
        sender: str = "ou_user",
        sender_user_id: str = "u_user",
    ) -> Any:
        questions = [
            {
                "question": "Choose a fruit",
                "header": "Fruit",
                "options": [
                    {"label": "Apple", "description": "Apple"},
                    {"label": "Banana", "description": "Banana"},
                ],
                "multiSelect": False,
            }
        ]
        ticket = self.tools.ToolTicket(
            session_id="session-1",
            message_id="om_origin",
            chat_id=chat_id,
            account_id="work",
            sender_open_id=sender,
            sender_user_id=sender_user_id,
            chat_type="p2p",
            thread_id="omt_thread",
            session_thread_id="om_origin",
        )
        return self.tools._store_interaction(
            "ask_user_question",
            "feishu_ask_user_question",
            {"questions": questions},
            ticket,
            300,
        )

    def test_builds_v2_form_controls_matching_upstream_names(self) -> None:
        questions = [
            {
                "question": "Notes",
                "header": "Notes",
                "options": [],
                "multiSelect": False,
            },
            {
                "question": "Color",
                "header": "Color",
                "options": [{"label": "Red", "description": ""}],
                "multiSelect": False,
            },
            {
                "question": "Fruit",
                "header": "Fruit",
                "options": [{"label": "Apple", "description": ""}],
                "multiSelect": True,
            },
            {
                "question": "Toggle",
                "header": "Toggle",
                "options": [{"label": "Enabled", "description": ""}],
                "multiSelect": True,
                "selectStyle": "checkbox",
            },
        ]

        card = self.adapter_module.FeishuAdapter._build_ask_user_question_card(
            questions,
            "question-1",
        )
        form = card["body"]["elements"][0]
        controls = {
            element.get("name"): element
            for element in self._walk_dicts(form)
            if element.get("name")
        }

        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(form["tag"], "form")
        self.assertEqual(controls["answer_0"]["tag"], "input")
        self.assertEqual(controls["selection_1"]["tag"], "select_static")
        self.assertEqual(controls["selection_2"]["tag"], "multi_select_static")
        self.assertEqual(controls["selection_3_0"]["tag"], "checker")
        submit = controls["ask_user_submit_question-1"]
        self.assertEqual(submit["form_action_type"], "submit")
        self.assertEqual(
            submit["value"],
            {"action": "ask_user_submit", "operation_id": "question-1"},
        )

    def test_parse_all_supported_answer_shapes(self) -> None:
        questions = [
            {"question": "Text", "header": "Text", "options": [], "multiSelect": False},
            {
                "question": "Single select",
                "header": "Single select",
                "options": [{"label": "A", "description": ""}],
                "multiSelect": False,
            },
            {
                "question": "Multi-select",
                "header": "Multi-select",
                "options": [{"label": "A", "description": ""}],
                "multiSelect": True,
            },
            {
                "question": "Checkboxes",
                "header": "Checkboxes",
                "options": [
                    {"label": "A", "description": ""},
                    {"label": "B", "description": ""},
                ],
                "multiSelect": True,
                "selectStyle": "checkbox",
            },
        ]
        answers, unanswered = self.adapter_module.FeishuAdapter._parse_ask_user_answers(
            questions,
            {
                "answer_0": " hello ",
                "selection_1": "A",
                "selection_2": '["A", "B"]',
                "selection_3_0": True,
                "selection_3_1": "true",
            },
        )

        self.assertEqual(
            answers,
            {
                "Text": "hello",
                "Single select": "A",
                "Multi-select": "A, B",
                "Checkboxes": "A, B",
            },
        )
        self.assertEqual(unanswered, [])

    def test_callback_validates_identity_then_injects_and_consumes(self) -> None:
        asyncio.run(self._exercise_valid_callback())

    def test_connect_and_disconnect_manage_live_interaction_host(self) -> None:
        asyncio.run(self._exercise_interaction_host_lifecycle())

    def test_interaction_host_sends_card_as_reply_in_thread(self) -> None:
        asyncio.run(self._exercise_interaction_host_delivery())

    async def _exercise_interaction_host_delivery(self) -> None:
        interaction = self._store_interaction()
        adapter = self._new_adapter()
        adapter._loop = asyncio.get_running_loop()
        adapter._client = object()
        sent: list[dict[str, Any]] = []

        async def send_with_retry(**kwargs: Any) -> Any:
            sent.append(kwargs)
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id="om_question_card"),
            )

        adapter._feishu_send_with_retry = send_with_retry
        delivered = await adapter._loop.run_in_executor(
            None,
            adapter._begin_openclaw_interaction,
            interaction,
        )

        self.assertTrue(delivered)
        self.assertEqual(sent[0]["chat_id"], "oc_chat")
        self.assertEqual(sent[0]["msg_type"], "interactive")
        self.assertEqual(sent[0]["reply_to"], "om_origin")
        self.assertEqual(sent[0]["metadata"], {"thread_id": "om_origin"})
        self.assertIn('"schema": "2.0"', sent[0]["payload"])
        self.assertEqual(
            adapter._openclaw_interaction_messages[interaction.token],
            "om_question_card",
        )

    async def _exercise_interaction_host_lifecycle(self) -> None:
        adapter = self._new_adapter()
        adapter._sdk_executor_closing = True
        adapter._profile_scope_key = "/hermes/profiles/coder"
        adapter._app_id = "cli_app"
        adapter._app_secret = "secret"
        adapter._connection_mode = "websocket"
        adapter._verification_token = ""
        adapter._encrypt_key = ""
        adapter._domain_name = "feishu"
        adapter._app_lock_identity = None
        adapter._pending_text_batch_tasks = {}
        adapter._pending_media_batch_tasks = {}
        adapter._ws_client = None
        adapter._ws_thread_loop = None
        adapter._ws_future = None
        adapter._event_handler = None
        adapter._connect_with_retry = self._async_noop
        adapter._reset_batch_buffers = lambda: None
        adapter._disable_websocket_auto_reconnect = lambda: None
        adapter._stop_webhook_server = self._async_noop
        adapter._shutdown_sdk_executor = lambda: None
        adapter._persist_seen_message_ids = lambda: None
        adapter._release_app_lock = self._async_noop
        self.adapter_module.FEISHU_AVAILABLE = True

        connected = await adapter.connect()

        self.assertTrue(connected)
        self.assertIs(
            self.tools._interaction_hosts[
                ("/hermes/profiles/coder", "work")
            ],
            adapter._openclaw_interaction_host,
        )
        self.assertEqual(
            self.tools._interaction_expiry_hosts[
                ("/hermes/profiles/coder", "work")
            ],
            adapter._expire_openclaw_interaction,
        )

        await adapter.disconnect()

        self.assertNotIn(
            ("/hermes/profiles/coder", "work"),
            self.tools._interaction_hosts,
        )
        self.assertNotIn(
            ("/hermes/profiles/coder", "work"),
            self.tools._interaction_expiry_hosts,
        )

    async def _exercise_valid_callback(self) -> None:
        interaction = self._store_interaction()
        adapter = self._new_adapter()
        adapter._loop = asyncio.get_running_loop()
        scheduled: list[asyncio.Task[Any]] = []
        captured: list[Any] = []
        updated: list[Any] = []

        def submit_on_loop(loop: Any, coroutine: Any) -> bool:
            scheduled.append(loop.create_task(coroutine))
            return True

        async def resolve_sender(sender_id: Any) -> dict[str, str]:
            return {
                "user_id": sender_id.open_id,
                "user_name": "User",
                "user_id_alt": sender_id.open_id,
            }

        async def get_chat_info(chat_id: str) -> dict[str, str]:
            return {"name": "Chat", "type": "dm"}

        async def dispatch(message: Any) -> None:
            captured.append(message)

        async def run_blocking(function: Any, request: Any) -> Any:
            updated.append(request)
            return SimpleNamespace(success=lambda: True)

        adapter._submit_on_loop = submit_on_loop
        adapter._resolve_sender_profile = resolve_sender
        adapter.get_chat_info = get_chat_info
        adapter.build_source = lambda chat_id, **kwargs: SimpleNamespace(
            chat_id=chat_id,
            **kwargs,
        )
        adapter._resolve_source_chat_type = lambda **kwargs: "dm"
        adapter._resolve_channel_prompt = lambda *args: None
        adapter._admit_synthetic_user_action = (
            lambda *args, **kwargs: SimpleNamespace(chat_type="p2p")
        )
        adapter._role_authorized_for_admitted_message = lambda _message: True
        adapter._handle_message_with_guards = dispatch
        adapter._client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(
                    message=SimpleNamespace(update=object()),
                )
            )
        )
        adapter._run_blocking = run_blocking
        adapter._openclaw_interaction_messages[
            interaction.token
        ] = "om_question_card"

        event = SimpleNamespace(
            operator=SimpleNamespace(open_id="ou_user"),
            context=SimpleNamespace(open_chat_id="oc_chat"),
            action=SimpleNamespace(
                tag="button",
                name=f"ask_user_submit_{interaction.token}",
                value={
                    "action": "ask_user_submit",
                    "operation_id": interaction.token,
                },
                form_value={"selection_0": "Apple"},
            ),
        )
        response = adapter._on_card_action_trigger(SimpleNamespace(event=event))
        await asyncio.gather(*scheduled)

        self.assertEqual(response.toast.type, "success")
        self.assertEqual(response.card.data["header"]["template"], "turquoise")
        self.assertEqual(len(captured), 1)
        self.assertEqual(
            captured[0].text,
            "The user answered your questions:\n- Choose a fruit: Apple",
        )
        self.assertEqual(
            captured[0].message_id,
            f"om_origin:ask-user-answer:{interaction.token}",
        )
        self.assertEqual(captured[0].reply_to_message_id, "om_origin")
        self.assertEqual(len(updated), 1)
        self.assertEqual(updated[0].message_id, "om_question_card")
        answered_card = json.loads(updated[0].request_body.content)
        self.assertEqual(answered_card["header"]["template"], "green")
        self.assertEqual(
            answered_card["header"]["text_tag_list"][0]["text"]["content"],
            "Complete",
        )
        self.assertIsNone(self.tools.get_pending_interaction(interaction.token))
        self.assertNotIn(
            interaction.token,
            adapter._openclaw_interaction_messages,
        )

    def test_callback_rejects_wrong_account_chat_and_operator(self) -> None:
        cases = [
            ("other", "oc_chat", "ou_user", "different Feishu account"),
            ("work", "oc_other", "ou_user", "chat where you received"),
            ("work", "oc_chat", "ou_other", "received the question"),
        ]
        for account, chat_id, operator, expected_text in cases:
            with self.subTest(account=account, chat_id=chat_id, operator=operator):
                interaction = self._store_interaction()
                adapter = self._new_adapter()
                adapter._account_id = account
                adapter._loop = SimpleNamespace(is_closed=lambda: False)
                adapter._submit_on_loop = lambda *args: self.fail("must not schedule")
                event = SimpleNamespace(
                    operator=SimpleNamespace(open_id=operator),
                    context=SimpleNamespace(open_chat_id=chat_id),
                    action=SimpleNamespace(
                        tag="button",
                        name=f"ask_user_submit_{interaction.token}",
                        value={
                            "action": "ask_user_submit",
                            "operation_id": interaction.token,
                        },
                        form_value={"selection_0": "Apple"},
                    ),
                )

                response = adapter._on_card_action_trigger(SimpleNamespace(event=event))

                self.assertEqual(response.toast.type, "warning")
                self.assertIn(expected_text, response.toast.content)
                self.assertIsNotNone(
                    self.tools.get_pending_interaction(interaction.token)
                )
                self.tools.cancel_interaction(interaction.token)

    def test_schema_two_user_id_matches_the_ticket_namespace(self) -> None:
        """A Schema 2 operator is accepted only through its ticket-bound user ID."""
        interaction = self._store_interaction()
        adapter = self._new_adapter()
        adapter._loop = SimpleNamespace(is_closed=lambda: False)
        scheduled: list[Any] = []
        adapter._submit_on_loop = (
            lambda _loop, coroutine: scheduled.append(coroutine) or True
        )
        event = SimpleNamespace(
            operator=SimpleNamespace(user_id="u_user"),
            context=SimpleNamespace(open_chat_id="oc_chat"),
            action=SimpleNamespace(
                tag="button",
                name=f"ask_user_submit_{interaction.token}",
                value={
                    "action": "ask_user_submit",
                    "operation_id": interaction.token,
                },
                form_value={"selection_0": "Apple"},
            ),
        )

        response = adapter._on_card_action_trigger(SimpleNamespace(event=event))

        self.assertEqual(response.toast.type, "success")
        self.assertEqual(len(scheduled), 1)
        scheduled.pop().close()
        adapter._openclaw_submitted_tokens.discard(interaction.token)
        self.tools.cancel_interaction(interaction.token)

    def test_schema_two_user_id_cannot_cross_ticket_namespaces(self) -> None:
        """A user ID never substitutes for the ticket's app-scoped open ID."""
        interaction = self._store_interaction(sender_user_id="")
        adapter = self._new_adapter()
        self.assertFalse(
            adapter._card_action_operator_matches_ticket(
                SimpleNamespace(
                    operator=SimpleNamespace(
                        open_id="ou_other",
                        user_id="u_user",
                    )
                ),
                {
                    "sender_open_id": "ou_user",
                    "sender_user_id": "u_user",
                },
            )
        )
        adapter._loop = SimpleNamespace(is_closed=lambda: False)
        adapter._submit_on_loop = lambda *args: self.fail("must not schedule")
        event = SimpleNamespace(
            operator=SimpleNamespace(user_id="ou_user"),
            context=SimpleNamespace(open_chat_id="oc_chat"),
            action=SimpleNamespace(
                tag="button",
                name=f"ask_user_submit_{interaction.token}",
                value={
                    "action": "ask_user_submit",
                    "operation_id": interaction.token,
                },
                form_value={"selection_0": "Apple"},
            ),
        )

        response = adapter._on_card_action_trigger(SimpleNamespace(event=event))

        self.assertEqual(response.toast.type, "warning")
        self.assertIn("received the question", response.toast.content)
        self.tools.cancel_interaction(interaction.token)

    def test_three_injection_failures_restore_submittable_card(self) -> None:
        asyncio.run(self._exercise_failed_injection())

    async def _exercise_failed_injection(self) -> None:
        interaction = self._store_interaction()
        pending = self.tools.get_pending_interaction(interaction.token)
        assert pending is not None
        adapter = self._new_adapter()
        adapter._openclaw_submitted_tokens.add(interaction.token)
        adapter._openclaw_interaction_messages[
            interaction.token
        ] = "om_question_card"
        attempts = 0
        updated: list[Any] = []

        async def resolve_sender(sender_id: Any) -> dict[str, str]:
            return {
                "user_id": sender_id.open_id,
                "user_name": "User",
                "user_id_alt": sender_id.open_id,
            }

        async def get_chat_info(chat_id: str) -> dict[str, str]:
            return {"name": "Chat", "type": "dm"}

        async def dispatch(message: Any) -> None:
            nonlocal attempts
            attempts += 1
            raise RuntimeError("agent dispatch failed")

        async def run_blocking(function: Any, request: Any) -> Any:
            updated.append(request)
            return SimpleNamespace(success=lambda: True)

        async def no_sleep(delay: float) -> None:
            return None

        adapter._resolve_sender_profile = resolve_sender
        adapter.get_chat_info = get_chat_info
        adapter.build_source = lambda chat_id, **kwargs: SimpleNamespace(
            chat_id=chat_id,
            **kwargs,
        )
        adapter._resolve_source_chat_type = lambda **kwargs: "dm"
        adapter._resolve_channel_prompt = lambda *args: None
        adapter._admit_synthetic_user_action = (
            lambda *args, **kwargs: SimpleNamespace(chat_type="p2p")
        )
        adapter._role_authorized_for_admitted_message = lambda _message: True
        adapter._handle_message_with_guards = dispatch
        adapter._client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(
                    message=SimpleNamespace(update=object()),
                )
            )
        )
        adapter._run_blocking = run_blocking

        original_sleep = self.adapter_module.asyncio.sleep
        self.adapter_module.asyncio.sleep = no_sleep
        try:
            await adapter._dispatch_ask_user_answer(
                question_id=interaction.token,
                pending=pending,
                answers={"Choose a fruit": "Apple"},
                callback_event=SimpleNamespace(),
            )
        finally:
            self.adapter_module.asyncio.sleep = original_sleep

        self.assertEqual(attempts, 3)
        self.assertEqual(len(updated), 1)
        restored = json.loads(updated[0].request_body.content)
        self.assertEqual(restored["header"]["template"], "blue")
        self.assertEqual(
            restored["header"]["text_tag_list"][0]["text"]["content"],
            "Awaiting response",
        )
        self.assertIsNotNone(
            self.tools.get_pending_interaction(interaction.token)
        )
        self.assertNotIn(
            interaction.token,
            adapter._openclaw_submitted_tokens,
        )
        self.assertEqual(
            adapter._openclaw_interaction_messages[interaction.token],
            "om_question_card",
        )

    def test_message_update_failure_is_non_fatal(self) -> None:
        asyncio.run(self._exercise_non_fatal_update_failure())

    def test_expired_card_is_terminal_and_forgets_callback_state(self) -> None:
        asyncio.run(self._exercise_question_expiry())

    async def _exercise_question_expiry(self) -> None:
        adapter = self._new_adapter()
        adapter._openclaw_interaction_messages["question-1"] = "om_card"
        adapter._openclaw_submitted_tokens.add("question-1")
        updated: list[dict[str, Any]] = []

        async def update(question_id: str, card: dict[str, Any]) -> bool:
            """Capture the terminal card without using the Feishu SDK."""
            self.assertEqual(question_id, "question-1")
            updated.append(card)
            return True

        adapter._update_openclaw_question_card = update
        result = await adapter._expire_openclaw_question_card(
            "question-1",
            [{"question": "Continue?", "header": "Confirm"}],
        )

        self.assertTrue(result)
        self.assertEqual(updated[0]["header"]["template"], "grey")
        self.assertEqual(
            updated[0]["header"]["text_tag_list"][0]["text"]["content"],
            "Expired",
        )
        self.assertNotIn("question-1", adapter._openclaw_interaction_messages)
        self.assertNotIn("question-1", adapter._openclaw_submitted_tokens)

    async def _exercise_non_fatal_update_failure(self) -> None:
        adapter = self._new_adapter()
        adapter._openclaw_interaction_messages["question-1"] = "om_card"
        adapter._client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(
                    message=SimpleNamespace(update=object()),
                )
            )
        )

        async def fail_update(function: Any, request: Any) -> Any:
            raise RuntimeError("update unavailable")

        adapter._run_blocking = fail_update
        updated = await adapter._update_openclaw_question_card(
            "question-1",
            {"schema": "2.0"},
        )

        self.assertFalse(updated)

    @staticmethod
    def _walk_dicts(value: Any) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if isinstance(value, dict):
            result.append(value)
            for nested in value.values():
                result.extend(AskUserQuestionAdapterTests._walk_dicts(nested))
        elif isinstance(value, list):
            for nested in value:
                result.extend(AskUserQuestionAdapterTests._walk_dicts(nested))
        return result

    @staticmethod
    async def _async_noop(*args: Any, **kwargs: Any) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
