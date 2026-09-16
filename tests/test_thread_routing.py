"""Focused tests for the fixed Slack-style Feishu thread model."""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from collections import OrderedDict
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from tests.test_ask_user_question_adapter import _MISSING_MODULE, _load_modules


class ThreadRoutingTests(unittest.TestCase):
    """Verify every admitted IM conversation is rooted in one thread session."""

    @classmethod
    def setUpClass(cls) -> None:
        _, cls.adapter_module, cls.previous_modules = _load_modules()

    @classmethod
    def tearDownClass(cls) -> None:
        for name, previous in cls.previous_modules.items():
            if previous is _MISSING_MODULE:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    def _adapter(
        self,
        *,
        chat_info: dict[str, Any],
        extra: dict[str, Any] | None = None,
    ) -> Any:
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter._account_id = "work"
        adapter._namespace_account = False
        adapter._client = object()
        adapter._bot_open_id = "ou_self"
        adapter._bot_user_id = ""
        adapter._bot_name = "Hermes"
        adapter._group_rules = {}
        adapter._history_limit = 0
        adapter._dedup_cache_size = 10
        adapter._thread_routes_by_message = OrderedDict()
        adapter.config = self.adapter_module.PlatformConfig(extra=extra or {})
        adapter.platform = self.adapter_module.Platform.FEISHU
        adapter._extract_message_content = self._async_value(
            (
                "hello",
                self.adapter_module.MessageType.TEXT,
                [],
                [],
                [],
            )
        )
        adapter._fetch_message_text = self._async_value(None)
        adapter.get_chat_info = self._async_value(chat_info)
        adapter._resolve_sender_profile = self._async_value(
            {
                "user_id": "u_user",
                "user_name": "Alice",
                "user_id_alt": "on_user",
            }
        )
        adapter._resolve_channel_prompt = lambda *_args: None
        adapter._apply_pending_group_history = lambda *_args, **_kwargs: None
        return adapter

    async def _inbound(
        self,
        adapter: Any,
        *,
        chat_type: str,
        thread_id: str | None,
        root_id: str | None,
        parent_id: str | None = None,
        upper_message_id: str | None = None,
        message_id: str = "om_inbound",
        create_time: str = "300",
        hydrate_thread_history: bool = False,
    ) -> Any | None:
        captured: list[Any] = []

        async def dispatch(event: Any) -> None:
            captured.append(event)

        adapter._dispatch_inbound_event = dispatch
        await adapter._process_inbound_message(
            data=SimpleNamespace(),
            message=SimpleNamespace(
                message_id=message_id,
                chat_id="oc_chat",
                chat_type=chat_type,
                thread_id=thread_id,
                root_id=root_id,
                parent_id=parent_id,
                upper_message_id=upper_message_id,
                create_time=create_time,
            ),
            sender_id=SimpleNamespace(
                open_id="ou_user",
                user_id="u_user",
                union_id="on_user",
            ),
            chat_type=chat_type,
            message_id=message_id,
            hydrate_thread_history=hydrate_thread_history,
        )
        return captured[0] if captured else None

    def test_group_top_level_uses_message_id_as_thread_root(self) -> None:
        adapter = self._adapter(
            chat_info={
                "name": "Group",
                "type": "group",
                "chat_mode": "group",
            }
        )

        event = asyncio.run(
            self._inbound(
                adapter,
                chat_type="group",
                thread_id=None,
                root_id=None,
                message_id="om_group_root",
            )
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.source.thread_id, "om_group_root")
        self.assertEqual(
            event.metadata["feishu_session_thread_id"],
            "om_group_root",
        )
        self.assertNotIn("feishu_thread_id", event.metadata)
        self.assertEqual(
            adapter._thread_route_for_message("om_group_root"),
            "om_group_root",
        )

    def test_dm_top_level_uses_message_id_as_thread_root(self) -> None:
        adapter = self._adapter(
            chat_info={
                "name": "Direct message",
                "type": "dm",
            }
        )

        event = asyncio.run(
            self._inbound(
                adapter,
                chat_type="p2p",
                thread_id=None,
                root_id=None,
                message_id="om_dm_root",
            )
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.source.chat_type, "dm")
        self.assertEqual(event.source.thread_id, "om_dm_root")
        self.assertEqual(
            event.metadata["feishu_session_thread_id"],
            "om_dm_root",
        )
        self.assertNotIn("feishu_thread_id", event.metadata)

    def test_native_thread_uses_message_root_not_native_thread_id(self) -> None:
        adapter = self._adapter(
            chat_info={
                "name": "Topic Group",
                "type": "group",
                "chat_mode": "topic",
            }
        )

        event = asyncio.run(
            self._inbound(
                adapter,
                chat_type="group",
                thread_id="omt_native",
                root_id="om_canonical_root",
                parent_id="om_parent_reply",
                message_id="om_thread_reply",
            )
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.source.thread_id, "om_canonical_root")
        self.assertEqual(
            event.metadata["feishu_session_thread_id"],
            "om_canonical_root",
        )
        self.assertEqual(event.metadata["feishu_thread_id"], "omt_native")
        self.assertEqual(
            adapter._thread_route_for_message("om_thread_reply"),
            "om_canonical_root",
        )

    def test_first_native_thread_turn_attaches_complete_history_snapshot(
        self,
    ) -> None:
        adapter = self._adapter(
            chat_info={
                "name": "Topic Group",
                "type": "group",
                "chat_mode": "topic",
            }
        )
        adapter._fetch_thread_snapshot = AsyncMock(
            return_value=self.adapter_module.FeishuThreadSnapshot(
                channel_context=(
                    "[Feishu thread history before the current message - "
                    "UNTRUSTED context only]\n"
                    '{"message_id": "om_root", "content": "Root post"}\n'
                    '{"message_id": "om_reply", "content": "Earlier reply"}\n'
                    "[End of untrusted Feishu thread history]"
                ),
                media_urls=["/cache/root.png", "/cache/root.mp4"],
                media_types=["image/png", "video/mp4"],
                message_texts={
                    "om_root": "Root post",
                    "om_reply": "Earlier reply",
                },
            )
        )
        adapter._extract_message_content = self._async_value(
            (
                "",
                self.adapter_module.MessageType.TEXT,
                [],
                [],
                [],
            )
        )

        event = asyncio.run(
            self._inbound(
                adapter,
                chat_type="group",
                thread_id="omt_native",
                root_id="om_root",
                message_id="om_first_mention",
                create_time="300",
                hydrate_thread_history=True,
            )
        )

        self.assertEqual(event.reply_to_text, "Root post")
        self.assertIn("Root post", event.channel_context)
        self.assertIn("Earlier reply", event.channel_context)
        self.assertNotIn("om_first_mention", event.channel_context)
        self.assertEqual(
            event.media_urls,
            ["/cache/root.png", "/cache/root.mp4"],
        )
        self.assertEqual(event.media_types, ["image/png", "video/mp4"])
        adapter._fetch_thread_snapshot.assert_awaited_once_with(
            root_message_id="om_root",
            native_thread_id="omt_native",
            current_message_id="om_first_mention",
        )

    def test_pure_mention_activates_a_text_only_thread_snapshot(self) -> None:
        adapter = self._adapter(
            chat_info={
                "name": "Topic Group",
                "type": "group",
                "chat_mode": "topic",
            }
        )
        adapter._fetch_thread_snapshot = AsyncMock(
            return_value=self.adapter_module.FeishuThreadSnapshot(
                channel_context="Root text",
                message_texts={"om_root": "Root text"},
            )
        )
        adapter._extract_message_content = self._async_value(
            (
                "",
                self.adapter_module.MessageType.TEXT,
                [],
                [],
                [],
            )
        )

        event = asyncio.run(
            self._inbound(
                adapter,
                chat_type="group",
                thread_id="omt_native",
                root_id="om_root",
                message_id="om_first_mention",
                hydrate_thread_history=True,
            )
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.text, "")
        self.assertEqual(event.channel_context, "Root text")

    def test_thread_snapshot_paginates_and_keeps_every_prior_message(
        self,
    ) -> None:
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        get_message = object()
        list_messages = object()
        adapter._client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(
                    message=SimpleNamespace(
                        get=get_message,
                        list=list_messages,
                    ),
                )
            )
        )
        adapter._message_text_cache = OrderedDict()
        adapter._sender_name_cache = OrderedDict()
        adapter._bot_open_id = "ou_self"
        adapter._bot_user_id = ""
        adapter._bot_name = "Hermes"
        adapter._app_id = "cli_self"

        def item(
            message_id: str,
            create_time: str,
            content: str,
            *,
            msg_type: str = "text",
            sender: str = "ou_alice",
            sender_name: str = "Alice",
            sender_type: str = "user",
        ) -> Any:
            return SimpleNamespace(
                message_id=message_id,
                thread_id="omt_native",
                create_time=create_time,
                thread_message_position=int(create_time),
                deleted=False,
                msg_type=msg_type,
                body=SimpleNamespace(content=content),
                mentions=[],
                sender=SimpleNamespace(
                    id=sender,
                    sender_type=sender_type,
                    sender_name=sender_name,
                ),
            )

        root = item(
            "om_root",
            "100",
            (
                '{"title":"Root","content":['
                '[{"tag":"img","image_key":"img_root"}]]}'
            ),
            msg_type="post",
        )
        reply_one = item(
            "om_reply_one",
            "200",
            '{"text":"First"}',
            sender="cli_other",
            sender_name="Other bot",
            sender_type="app",
        )
        reply_two = item(
            "om_reply_two",
            "250",
            '{"file_key":"file_video","file_name":"clip.mp4"}',
            msg_type="media",
            sender="ou_bob",
            sender_name="Bob",
        )
        current = item("om_current", "300", '{"text":"@Hermes help"}')
        later = item("om_later", "400", '{"text":"After activation"}')
        adapter._run_blocking = AsyncMock(
            side_effect=(
                SimpleNamespace(
                    success=lambda: True,
                    data=SimpleNamespace(items=[root]),
                ),
                SimpleNamespace(
                    success=lambda: True,
                    data=SimpleNamespace(
                        items=[root, reply_one],
                        has_more=True,
                        page_token="next-page",
                    ),
                ),
                SimpleNamespace(
                    success=lambda: True,
                    data=SimpleNamespace(
                        items=[reply_two, current, later],
                        has_more=False,
                        page_token="",
                    ),
                ),
            )
        )

        async def download_resources(
            *,
            message_id: str,
            normalized: Any,
        ) -> tuple[list[str], list[str]]:
            del normalized
            if message_id == "om_root":
                return ["/cache/root.png"], ["image/png"]
            if message_id == "om_reply_two":
                return ["/cache/clip.mp4"], ["video/mp4"]
            return [], []

        adapter._download_feishu_message_resources = AsyncMock(
            side_effect=download_resources
        )

        snapshot = asyncio.run(
            adapter._fetch_thread_snapshot(
                root_message_id="om_root",
                native_thread_id=None,
                current_message_id="om_current",
            )
        )

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        context = snapshot.channel_context
        self.assertLess(context.index("om_root"), context.index("om_reply_one"))
        self.assertLess(
            context.index("om_reply_one"),
            context.index("om_reply_two"),
        )
        self.assertNotIn("om_current", context)
        self.assertNotIn("om_later", context)
        self.assertIn('"sender": "Other bot"', context)
        self.assertIn('"sender_type": "app"', context)
        self.assertEqual(
            snapshot.media_urls,
            ["/cache/root.png", "/cache/clip.mp4"],
        )
        self.assertEqual(
            snapshot.media_types,
            ["image/png", "video/mp4"],
        )
        self.assertEqual(
            snapshot.message_texts,
            {
                "om_root": "Root\n[Image]",
                "om_reply_one": "First",
                "om_reply_two": "[Attachment: clip.mp4]",
            },
        )
        self.assertEqual(adapter._run_blocking.await_count, 3)
        first_list_request = adapter._run_blocking.await_args_list[1].args[1]
        second_list_request = adapter._run_blocking.await_args_list[2].args[1]
        self.assertEqual(first_list_request.container_id_type, "thread")
        self.assertEqual(first_list_request.container_id, "omt_native")
        self.assertEqual(first_list_request.sort_type, "ByCreateTimeAsc")
        self.assertEqual(first_list_request.page_size, 50)
        self.assertEqual(second_list_request.page_token, "next-page")

    def test_first_thread_turn_fails_closed_when_snapshot_is_incomplete(
        self,
    ) -> None:
        adapter = self._adapter(
            chat_info={
                "name": "Topic Group",
                "type": "group",
                "chat_mode": "topic",
            }
        )
        adapter._fetch_thread_snapshot = AsyncMock(return_value=None)
        adapter.send = AsyncMock()

        event = asyncio.run(
            self._inbound(
                adapter,
                chat_type="group",
                thread_id="omt_native",
                root_id="om_root",
                message_id="om_first_mention",
                hydrate_thread_history=True,
            )
        )

        self.assertIsNone(event)
        adapter.send.assert_awaited_once()
        self.assertIn(
            "complete thread history",
            adapter.send.await_args.args[1],
        )
        self.assertIn(
            "im:message.group_msg",
            adapter.send.await_args.args[1],
        )
        self.assertIn("im:resource", adapter.send.await_args.args[1])

    def test_thread_snapshot_is_incomplete_when_historical_media_is_missing(
        self,
    ) -> None:
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter._client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(
                    message=SimpleNamespace(get=object(), list=object()),
                )
            )
        )
        adapter._bot_open_id = "ou_self"
        adapter._bot_user_id = ""
        adapter._bot_name = "Hermes"
        root = SimpleNamespace(
            message_id="om_root",
            thread_id="omt_native",
            create_time="100",
            thread_message_position=0,
            deleted=False,
            msg_type="image",
            body=SimpleNamespace(content='{"image_key":"img_root"}'),
            mentions=[],
            sender=SimpleNamespace(
                id="ou_alice",
                sender_type="user",
                sender_name="Alice",
            ),
        )
        adapter._run_blocking = AsyncMock(
            side_effect=(
                SimpleNamespace(
                    success=lambda: True,
                    data=SimpleNamespace(items=[root]),
                ),
                SimpleNamespace(
                    success=lambda: True,
                    data=SimpleNamespace(
                        items=[
                            SimpleNamespace(message_id="om_current")
                        ],
                        has_more=False,
                    ),
                ),
            )
        )
        adapter._download_feishu_message_resources = AsyncMock(
            return_value=([], [])
        )

        snapshot = asyncio.run(
            adapter._fetch_thread_snapshot(
                root_message_id="om_root",
                native_thread_id="omt_native",
                current_message_id="om_current",
            )
        )

        self.assertIsNone(snapshot)

    def test_thread_snapshot_preserves_complete_card_and_forward_text(
        self,
    ) -> None:
        payloads = {
            "merge_forward": {
                "title": "Forwarded discussion",
                "messages": [
                    {"text": f"Important item {index}"}
                    for index in range(15)
                ],
            },
            "interactive": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": f"Important item {index}",
                    }
                    for index in range(15)
                ],
            },
        }

        for message_type, payload in payloads.items():
            with self.subTest(message_type=message_type):
                adapter = object.__new__(self.adapter_module.FeishuAdapter)
                adapter._client = SimpleNamespace(
                    im=SimpleNamespace(
                        v1=SimpleNamespace(
                            message=SimpleNamespace(list=object())
                        )
                    )
                )
                adapter._bot_open_id = "ou_self"
                adapter._bot_user_id = ""
                adapter._bot_name = "Hermes"
                adapter._sender_name_cache = OrderedDict()
                root = SimpleNamespace(
                    message_id="om_root",
                    thread_id="omt_native",
                    msg_type=message_type,
                    body=SimpleNamespace(content=json.dumps(payload)),
                    mentions=[],
                    sender=SimpleNamespace(
                        id="ou_user",
                        sender_type="user",
                    ),
                )
                adapter._fetch_message_item = AsyncMock(return_value=root)
                adapter._run_blocking = AsyncMock(
                    return_value=SimpleNamespace(
                        success=lambda: True,
                        data=SimpleNamespace(
                            items=[
                                root,
                                SimpleNamespace(message_id="om_current"),
                            ],
                            has_more=False,
                        ),
                    )
                )
                adapter._download_feishu_message_resources = AsyncMock(
                    return_value=([], [])
                )
                expanded_text = "\n".join(
                    f"Important item {index}" for index in range(15)
                )
                adapter._expand_merge_forward_message = AsyncMock(
                    return_value=self.adapter_module.FeishuNormalizedMessage(
                        raw_type="merge_forward",
                        text_content=expanded_text,
                        relation_kind="merge_forward",
                        metadata={"api_expanded": True},
                    )
                )

                snapshot = asyncio.run(
                    adapter._fetch_thread_snapshot(
                        root_message_id="om_root",
                        native_thread_id="omt_native",
                        current_message_id="om_current",
                    )
                )

                self.assertIsNotNone(snapshot)
                assert snapshot is not None
                self.assertIn(
                    "Important item 14",
                    snapshot.message_texts["om_root"],
                )
                if message_type == "merge_forward":
                    adapter._expand_merge_forward_message.assert_awaited_once()
                else:
                    adapter._expand_merge_forward_message.assert_not_awaited()

    def test_thread_snapshot_downloads_forwarded_resources(self) -> None:
        adapter = object.__new__(self.adapter_module.FeishuAdapter)
        adapter._client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(message=SimpleNamespace(list=object()))
            )
        )
        adapter._bot_open_id = "ou_self"
        adapter._bot_user_id = ""
        adapter._bot_name = "Hermes"
        adapter._sender_name_cache = OrderedDict()
        root = SimpleNamespace(
            message_id="om_root",
            thread_id="omt_native",
            msg_type="merge_forward",
            body=SimpleNamespace(content="{}"),
            mentions=[],
            sender=SimpleNamespace(id="ou_user", sender_type="user"),
        )
        adapter._fetch_message_item = AsyncMock(return_value=root)
        adapter._run_blocking = AsyncMock(
            return_value=SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(
                    items=[root, SimpleNamespace(message_id="om_current")],
                    has_more=False,
                ),
            )
        )
        adapter._fetch_merge_forward_items = AsyncMock(
            return_value=[
                {
                    "message_id": "om_root",
                    "msg_type": "merge_forward",
                    "body": {"content": "{}"},
                },
                {
                    "message_id": "om_photo",
                    "upper_message_id": "om_root",
                    "msg_type": "image",
                    "body": {
                        "content": json.dumps(
                            {"image_key": "img_photo"}
                        )
                    },
                },
                {
                    "message_id": "om_file",
                    "upper_message_id": "om_root",
                    "msg_type": "file",
                    "body": {
                        "content": json.dumps(
                            {
                                "file_key": "file_pdf",
                                "file_name": "report.pdf",
                            }
                        )
                    },
                },
            ]
        )
        adapter._download_feishu_image = AsyncMock(
            return_value=("/cache/photo.png", "image/png")
        )
        adapter._download_feishu_message_resource = AsyncMock(
            return_value=("/cache/report.pdf", "application/pdf")
        )

        snapshot = asyncio.run(
            adapter._fetch_thread_snapshot(
                root_message_id="om_root",
                native_thread_id="omt_native",
                current_message_id="om_current",
            )
        )

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(
            snapshot.media_urls,
            ["/cache/photo.png", "/cache/report.pdf"],
        )
        self.assertEqual(
            snapshot.media_types,
            ["image/png", "application/pdf"],
        )
        self.assertEqual(
            adapter._download_feishu_image.await_args.kwargs["message_id"],
            "om_photo",
        )
        self.assertEqual(
            adapter._download_feishu_message_resource.await_args.kwargs[
                "message_id"
            ],
            "om_file",
        )

        adapter._download_feishu_image.return_value = ("", "")
        incomplete_snapshot = asyncio.run(
            adapter._fetch_thread_snapshot(
                root_message_id="om_root",
                native_thread_id="omt_native",
                current_message_id="om_current",
            )
        )
        self.assertIsNone(incomplete_snapshot)

    def test_handler_hydrates_history_only_before_thread_session_exists(
        self,
    ) -> None:
        async def exercise(
            *,
            session_active: bool,
            thread_id: str | None = "omt_native",
        ) -> bool:
            chat_info = {
                "name": "Topic Group",
                "type": "group",
                "chat_mode": "topic",
            }
            adapter = self._adapter(
                chat_info=chat_info,
            )
            adapter._chat_info_cache = {"oc_chat": chat_info}
            adapter._is_duplicate = lambda _message_id: False
            adapter._admit = lambda _sender, _message: None
            adapter._has_active_session_for_thread = (
                lambda _sender, _message: session_active
            )
            adapter._bot_loop_states = OrderedDict()
            adapter._process_inbound_message = AsyncMock()
            message = SimpleNamespace(
                message_id="om_mention",
                chat_id="oc_chat",
                chat_type="group",
                thread_id=thread_id,
                root_id="om_root",
                create_time=None,
            )

            await adapter._handle_message_event_data(
                SimpleNamespace(
                    event=SimpleNamespace(
                        message=message,
                        sender=SimpleNamespace(
                            sender_type="user",
                            sender_id=SimpleNamespace(
                                open_id="ou_user",
                                user_id="u_user",
                                union_id="on_user",
                            ),
                        ),
                    )
                )
            )

            return bool(
                adapter._process_inbound_message.await_args.kwargs[
                    "hydrate_thread_history"
                ]
            )

        self.assertTrue(asyncio.run(exercise(session_active=False)))
        self.assertFalse(asyncio.run(exercise(session_active=True)))
        self.assertTrue(
            asyncio.run(
                exercise(session_active=False, thread_id=None)
            )
        )

    def test_existing_dm_thread_session_skips_history_hydration(self) -> None:
        adapter = self._adapter(chat_info={"name": "DM", "type": "dm"})
        adapter._dm_policy = "open"
        adapter._chat_info_cache = {
            "oc_chat": {"name": "DM", "type": "dm"}
        }
        looked_up_chat_types: list[str] = []

        def session_key(source: Any) -> str:
            looked_up_chat_types.append(source.chat_type)
            return f"{source.chat_type}:{source.chat_id}:{source.thread_id}"

        adapter._session_store = SimpleNamespace(
            _ensure_loaded=lambda: None,
            _generate_session_key=session_key,
            _entries={
                "dm:oc_chat:om_root": SimpleNamespace(suspended=False)
            },
            _should_reset=lambda *_args: False,
        )
        adapter._is_duplicate = lambda _message_id: False
        adapter._admit = lambda *_args: None
        adapter._bot_loop_states = OrderedDict()
        adapter._process_inbound_message = AsyncMock()

        asyncio.run(
            adapter._handle_message_event_data(
                SimpleNamespace(
                    event=SimpleNamespace(
                        sender=SimpleNamespace(
                            sender_type="user",
                            sender_id=SimpleNamespace(
                                open_id="ou_user",
                                user_id="u_user",
                                union_id="on_user",
                            ),
                        ),
                        message=SimpleNamespace(
                            chat_id="oc_chat",
                            chat_type="p2p",
                            message_id="om_followup",
                            root_id="om_root",
                            thread_id="omt_native",
                            create_time=None,
                        ),
                    )
                )
            )
        )

        self.assertFalse(
            adapter._process_inbound_message.await_args.kwargs[
                "hydrate_thread_history"
            ]
        )
        self.assertEqual(looked_up_chat_types, ["dm"])

    def test_thread_capable_group_root_uses_its_own_message_id(self) -> None:
        """Both topic-style roots start a session despite carrying thread_id."""
        chat_infos = (
            {
                "name": "Thread Group",
                "type": "group",
                "chat_mode": "group",
                "group_message_type": "thread",
            },
            {
                "name": "Topic Group",
                "type": "forum",
                "chat_mode": "topic",
            },
        )

        for chat_info in chat_infos:
            with self.subTest(chat_info=chat_info):
                adapter = self._adapter(chat_info=chat_info)
                event = asyncio.run(
                    self._inbound(
                        adapter,
                        chat_type="group",
                        thread_id="omt_native",
                        root_id=None,
                        parent_id=None,
                        message_id="om_thread_root",
                    )
                )

                self.assertIsNotNone(event)
                self.assertEqual(event.source.thread_id, "om_thread_root")
                self.assertEqual(
                    event.metadata["feishu_session_thread_id"],
                    "om_thread_root",
                )
                self.assertEqual(
                    event.metadata["feishu_thread_id"],
                    "omt_native",
                )
                self.assertEqual(
                    adapter._thread_route_for_message("om_thread_root"),
                    "om_thread_root",
                )

    def test_top_level_quote_does_not_inherit_quoted_root(self) -> None:
        adapter = self._adapter(
            chat_info={
                "name": "Group",
                "type": "group",
                "chat_mode": "group",
            }
        )

        event = asyncio.run(
            self._inbound(
                adapter,
                chat_type="group",
                thread_id=None,
                root_id="om_quoted_root",
                parent_id="om_quoted_message",
                message_id="om_new_root",
            )
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.source.thread_id, "om_new_root")
        self.assertEqual(
            event.metadata["feishu_session_thread_id"],
            "om_new_root",
        )
        self.assertNotIn("feishu_thread_id", event.metadata)
        self.assertEqual(event.reply_to_message_id, "om_quoted_message")

    def test_legacy_thread_options_do_not_change_routing(self) -> None:
        legacy_options = (
            {
                "threadSession": False,
                "replyInThread": False,
                "thread_session": False,
                "reply_in_thread": False,
            },
            {
                "threadSession": True,
                "replyInThread": True,
                "thread_session": True,
                "reply_in_thread": True,
            },
        )

        for extra in legacy_options:
            with self.subTest(extra=extra):
                adapter = self._adapter(
                    chat_info={
                        "name": "Plain Group",
                        "type": "group",
                        "chat_mode": "group",
                    },
                    extra=extra,
                )
                top_level = asyncio.run(
                    self._inbound(
                        adapter,
                        chat_type="group",
                        thread_id=None,
                        root_id=None,
                        message_id="om_fixed_root",
                    )
                )
                native = asyncio.run(
                    self._inbound(
                        adapter,
                        chat_type="group",
                        thread_id="omt_native",
                        root_id="om_native_root",
                        message_id="om_native_reply",
                    )
                )

                self.assertEqual(top_level.source.thread_id, "om_fixed_root")
                self.assertEqual(native.source.thread_id, "om_native_root")

    def test_yaml_bridge_discards_legacy_thread_options(self) -> None:
        config = {
            "appId": "cli_test",
            "appSecret": "secret",
            "threadSession": False,
            "reply_in_thread": False,
            "groups": {
                "*": {"replyInThread": False},
            },
            "accounts": {
                "work": {
                    "thread_session": True,
                    "replyInThread": True,
                    "groups": {
                        "oc_chat": {"reply_in_thread": True},
                    },
                }
            },
        }

        normalized = self.adapter_module._apply_yaml_config({}, config)

        self.assertNotIn("threadSession", normalized)
        self.assertNotIn("reply_in_thread", normalized)
        self.assertNotIn(
            "replyInThread",
            normalized["groups"]["*"],
        )
        self.assertNotIn(
            "thread_session",
            normalized["accounts"]["work"],
        )
        self.assertNotIn(
            "replyInThread",
            normalized["accounts"]["work"],
        )
        self.assertNotIn(
            "reply_in_thread",
            normalized["accounts"]["work"]["groups"]["oc_chat"],
        )
        self.assertIn("threadSession", config)
        self.assertIn("replyInThread", config["groups"]["*"])

    def test_native_thread_without_root_fails_closed(self) -> None:
        adapter = self._adapter(
            chat_info={
                "name": "Ordinary Group",
                "type": "group",
                "chat_mode": "group",
            }
        )

        event = asyncio.run(
            self._inbound(
                adapter,
                chat_type="group",
                thread_id="omt_without_root",
                root_id=None,
                message_id="om_orphan_reply",
            )
        )

        self.assertIsNone(event)
        self.assertIsNone(
            adapter._thread_route_for_message("om_orphan_reply")
        )

    def test_native_thread_without_root_uses_a_remembered_parent_route(
        self,
    ) -> None:
        adapter = self._adapter(
            chat_info={
                "name": "Topic Group",
                "type": "group",
                "chat_mode": "topic",
            }
        )
        adapter._remember_thread_route("om_parent", "om_canonical_root")

        event = asyncio.run(
            self._inbound(
                adapter,
                chat_type="group",
                thread_id="omt_native",
                root_id=None,
                parent_id="om_parent",
                message_id="om_reply",
            )
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.source.thread_id, "om_canonical_root")

    def test_canonical_root_overrides_reply_anchor(self) -> None:
        adapter, reply_method, create_method, captured = (
            self._outbound_adapter()
        )

        asyncio.run(
            adapter._send_raw_message(
                chat_id="oc_chat",
                msg_type="text",
                payload='{"text":"hello"}',
                reply_to="om_quoted_message",
                metadata={"thread_id": "om_canonical_root"},
            )
        )

        self.assertIs(captured["method"], reply_method)
        self.assertIsNot(captured["method"], create_method)
        self.assertEqual(captured["request"]["message_id"], "om_canonical_root")
        self.assertTrue(captured["request"]["reply_in_thread"])

    def test_metadata_root_without_reply_anchor_still_uses_reply_api(self) -> None:
        adapter, reply_method, create_method, captured = (
            self._outbound_adapter()
        )

        asyncio.run(
            adapter._send_raw_message(
                chat_id="oc_chat",
                msg_type="text",
                payload='{"text":"hello"}',
                reply_to=None,
                metadata={"thread_id": "om_canonical_root"},
            )
        )

        self.assertIs(captured["method"], reply_method)
        self.assertIsNot(captured["method"], create_method)
        self.assertEqual(captured["request"]["message_id"], "om_canonical_root")
        self.assertTrue(captured["request"]["reply_in_thread"])

    def test_thread_reply_failure_never_falls_back_to_top_level(self) -> None:
        adapter = self._adapter(
            chat_info={"name": "Group", "type": "group"}
        )
        failure_code = next(iter(self.adapter_module._FEISHU_REPLY_FALLBACK_CODES))
        failure = SimpleNamespace(
            success=lambda: False,
            code=failure_code,
            msg="missing",
        )
        adapter._send_raw_message = AsyncMock(return_value=failure)

        result = asyncio.run(
            adapter._feishu_send_with_retry(
                chat_id="oc_chat",
                msg_type="text",
                payload='{"text":"hello"}',
                reply_to="om_child",
                metadata={"thread_id": "om_canonical_root"},
            )
        )

        self.assertIs(result, failure)
        adapter._send_raw_message.assert_awaited_once_with(
            chat_id="oc_chat",
            msg_type="text",
            payload='{"text":"hello"}',
            reply_to="om_child",
            metadata={"thread_id": "om_canonical_root"},
        )

    def _outbound_adapter(
        self,
    ) -> tuple[Any, object, object, dict[str, Any]]:
        adapter = self._adapter(
            chat_info={"name": "Group", "type": "group"}
        )
        reply_method = object()
        create_method = object()
        adapter._client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(
                    message=SimpleNamespace(
                        reply=reply_method,
                        create=create_method,
                    ),
                )
            )
        )
        captured: dict[str, Any] = {}
        adapter._build_reply_message_body = lambda **kwargs: dict(kwargs)
        adapter._build_reply_message_request = (
            lambda message_id, request_body: {
                "message_id": message_id,
                **request_body,
            }
        )

        async def run_blocking(method: Any, request: dict[str, Any]) -> Any:
            captured["method"] = method
            captured["request"] = request
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id="om_outbound"),
            )

        adapter._run_blocking = run_blocking
        return adapter, reply_method, create_method, captured

    @staticmethod
    def _async_value(value: Any) -> Any:
        async def resolve(*_args: Any, **_kwargs: Any) -> Any:
            return value

        return resolve


if __name__ == "__main__":
    unittest.main()
