"""Tests for live-E2E chat provisioning through Feishu OpenAPI."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.e2e import test_live_thread_model as live


class E2ELiveProvisioningTests(unittest.TestCase):
    """Verify each acceptance run receives isolated public chat resources."""

    def _api(self) -> live.FeishuOpenApi:
        """Build a credential-safe API client without issuing network calls."""
        api = live.FeishuOpenApi(
            app_id="cli_test",
            app_secret="secret-test-value",
            domain="feishu",
            user_access_token="user-token-value",
        )
        api._tenant_access_token = "tenant-token-value"
        return api

    def test_dm_chat_is_resolved_by_sending_to_the_test_user(self) -> None:
        """The canonical bot-user P2P chat ID comes from a real app message."""
        api = self._api()
        api._request = Mock(
            return_value={
                "code": 0,
                "data": {
                    "message_id": "om_setup",
                    "chat_id": "oc_dm",
                },
            }
        )

        chat_id = api.resolve_dm_chat_id("ou_test", "run-123")

        self.assertEqual(chat_id, "oc_dm")
        request = api._request.call_args
        self.assertEqual(request.args, ("POST", "/open-apis/im/v1/messages"))
        self.assertEqual(request.kwargs["token"], "tenant-token-value")
        self.assertEqual(
            request.kwargs["query"],
            {"receive_id_type": "open_id"},
        )
        body = request.kwargs["body"]
        self.assertEqual(body["receive_id"], "ou_test")
        self.assertEqual(body["msg_type"], "text")
        self.assertIn("run-123", json.loads(body["content"])["text"])

    def test_group_is_created_for_this_run_with_bot_as_owner(self) -> None:
        """A fresh private group contains the user and the creating app bot."""
        api = self._api()
        api._request = Mock(
            return_value={"code": 0, "data": {"chat_id": "oc_group"}}
        )

        chat_id = api.create_test_group("ou_test", "run-123")

        self.assertEqual(chat_id, "oc_group")
        request = api._request.call_args
        self.assertEqual(request.args, ("POST", "/open-apis/im/v1/chats"))
        self.assertEqual(request.kwargs["token"], "tenant-token-value")
        self.assertEqual(request.kwargs["query"]["user_id_type"], "open_id")
        self.assertTrue(request.kwargs["query"]["uuid"])
        body = request.kwargs["body"]
        self.assertEqual(body["user_id_list"], ["ou_test"])
        self.assertEqual(body["chat_mode"], "group")
        self.assertEqual(body["chat_type"], "private")
        self.assertEqual(body["group_message_type"], "chat")
        self.assertNotIn("owner_id", body)
        self.assertNotIn("bot_id_list", body)

    def test_thread_message_group_is_created_for_this_run(self) -> None:
        """A fresh thread-message group covers Feishu's topic-style mode."""
        api = self._api()
        api._request = Mock(
            return_value={"code": 0, "data": {"chat_id": "oc_thread_group"}}
        )

        chat_id = api.create_test_group(
            "ou_test",
            "run-123",
            group_message_type="thread",
        )

        self.assertEqual(chat_id, "oc_thread_group")
        body = api._request.call_args.kwargs["body"]
        self.assertEqual(body["chat_mode"], "group")
        self.assertEqual(body["group_message_type"], "thread")
        self.assertIn("Thread", body["name"])

    def test_thread_message_group_mode_is_read_back_from_feishu(self) -> None:
        """Provisioning verifies that Feishu created the requested group mode."""
        api = self._api()
        api._request = Mock(
            return_value={
                "code": 0,
                "data": {
                    "chat_mode": "group",
                    "group_message_type": "thread",
                },
            }
        )

        info = api.get_chat_info("oc_thread_group")

        self.assertEqual(info["chat_mode"], "group")
        self.assertEqual(info["group_message_type"], "thread")
        api._request.assert_called_once_with(
            "GET",
            "/open-apis/im/v1/chats/oc_thread_group",
            token="tenant-token-value",
            query={"user_id_type": "open_id"},
        )

    def test_message_reactions_are_read_with_the_app_identity(self) -> None:
        """Lifecycle assertions observe reactions on the exact user message."""
        api = self._api()
        reactions = [
            {
                "reaction_id": "reaction-1",
                "reaction_type": {"emoji_type": "Typing"},
            }
        ]
        api._request = Mock(
            return_value={"code": 0, "data": {"items": reactions}}
        )

        self.assertEqual(api.list_message_reactions("om_user"), reactions)
        api._request.assert_called_once_with(
            "GET",
            "/open-apis/im/v1/messages/om_user/reactions",
            token="tenant-token-value",
        )

    def test_reaction_turn_is_triggered_with_the_user_identity(self) -> None:
        """The live driver adds a real human reaction with its user token."""
        api = self._api()
        reaction = {
            "reaction_id": "reaction-user-1",
            "action_time": "1730000000000",
            "reaction_type": {"emoji_type": "THUMBSUP"},
        }
        api._request = Mock(return_value={"code": 0, "data": reaction})

        self.assertEqual(
            api.create_message_reaction("om_bot", "THUMBSUP"),
            reaction,
        )
        api._request.assert_called_once_with(
            "POST",
            "/open-apis/im/v1/messages/om_bot/reactions",
            token="user-token-value",
            body={"reaction_type": {"emoji_type": "THUMBSUP"}},
        )

    def test_media_resources_are_uploaded_as_app_then_sent_as_user(self) -> None:
        """The API-supported identity split needs no extra manual fixture."""
        api = self._api()
        api._request_multipart = Mock(
            side_effect=(
                {"code": 0, "data": {"image_key": "img_test"}},
                {"code": 0, "data": {"file_key": "file_test"}},
            )
        )
        api._request = Mock(
            side_effect=(
                {
                    "code": 0,
                    "data": {"message_id": "om_image", "chat_id": "oc_dm"},
                },
                {
                    "code": 0,
                    "data": {"message_id": "om_file", "chat_id": "oc_dm"},
                },
            )
        )

        image_key = api.upload_image(b"png-bytes", "fixture.png")
        file_key = api.upload_file(b"file-bytes", "fixture.txt")
        image = api.create_user_media_message(
            "oc_dm",
            msg_type="image",
            resource_key=image_key,
        )
        file_message = api.reply_user_media_in_thread(
            "om_image",
            msg_type="file",
            resource_key=file_key,
        )

        self.assertEqual(image["message_id"], "om_image")
        self.assertEqual(file_message["message_id"], "om_file")
        self.assertEqual(
            [call.kwargs["token"] for call in api._request_multipart.call_args_list],
            ["tenant-token-value", "tenant-token-value"],
        )
        self.assertEqual(
            [call.kwargs["token"] for call in api._request.call_args_list],
            ["user-token-value", "user-token-value"],
        )
        self.assertEqual(
            json.loads(api._request.call_args_list[0].kwargs["body"]["content"]),
            {"image_key": "img_test"},
        )
        self.assertEqual(
            json.loads(api._request.call_args_list[1].kwargs["body"]["content"]),
            {"file_key": "file_test"},
        )
        self.assertIs(
            api._request.call_args_list[1].kwargs["body"]["reply_in_thread"],
            True,
        )

    def test_multipart_upload_preserves_fixture_bytes(self) -> None:
        """The stdlib upload body carries exact bytes and a bounded filename."""
        api = self._api()
        api._request_raw = Mock(
            return_value=(
                json.dumps(
                    {"code": 0, "data": {"image_key": "img_test"}}
                ).encode(),
                {},
            )
        )
        fixture = b"\x89PNG\r\n\x1a\nfixture-bytes"

        self.assertEqual(api.upload_image(fixture, "fixture.png"), "img_test")

        request = api._request_raw.call_args
        self.assertEqual(request.args, ("POST", "/open-apis/im/v1/images"))
        self.assertEqual(request.kwargs["token"], "tenant-token-value")
        self.assertIn("multipart/form-data; boundary=", request.kwargs["content_type"])
        body = request.kwargs["body"]
        self.assertIn(b'name="image_type"', body)
        self.assertIn(b'filename="fixture.png"', body)
        self.assertEqual(body.count(fixture), 1)

    def test_message_resource_download_uses_the_app_identity(self) -> None:
        """Integrity reads use the bot identity that shares the conversation."""
        api = self._api()
        api._request_binary = Mock(return_value=(b"fixture", "text/plain"))

        self.assertEqual(
            api.download_message_resource("om_test", "file_test", "file"),
            (b"fixture", "text/plain"),
        )
        api._request_binary.assert_called_once_with(
            "GET",
            "/open-apis/im/v1/messages/om_test/resources/file_test",
            token="tenant-token-value",
            query={"type": "file"},
        )

    def test_top_level_quote_is_sent_without_thread_reply_mode(self) -> None:
        """The live quote case creates a new surface instead of a thread follow-up."""
        api = self._api()
        api._request = Mock(
            return_value={
                "code": 0,
                "data": {"message_id": "om_quote", "chat_id": "oc_dm"},
            }
        )

        message = api.reply_text_to_message(
            "om_bot_reply",
            "quoted follow-up",
            reply_in_thread=False,
        )

        self.assertEqual(message["message_id"], "om_quote")
        request = api._request.call_args
        self.assertEqual(
            request.args,
            ("POST", "/open-apis/im/v1/messages/om_bot_reply/reply"),
        )
        self.assertEqual(request.kwargs["token"], "user-token-value")
        self.assertEqual(
            request.kwargs["body"]["reply_in_thread"],
            False,
        )
        self.assertEqual(
            json.loads(request.kwargs["body"]["content"])["text"],
            "quoted follow-up",
        )

    def test_bot_can_create_a_neutral_top_level_quote_anchor(self) -> None:
        """The quote scenario starts from an app-owned non-thread message."""
        api = self._api()
        api._request = Mock(
            return_value={
                "code": 0,
                "data": {"message_id": "om_anchor", "chat_id": "oc_dm"},
            }
        )

        message = api.create_bot_text_message("oc_dm", "quote anchor")

        self.assertEqual(message["message_id"], "om_anchor")
        request = api._request.call_args
        self.assertEqual(request.args, ("POST", "/open-apis/im/v1/messages"))
        self.assertEqual(request.kwargs["token"], "tenant-token-value")
        self.assertEqual(
            request.kwargs["query"],
            {"receive_id_type": "chat_id"},
        )
        self.assertEqual(request.kwargs["body"]["receive_id"], "oc_dm")
        self.assertEqual(
            json.loads(request.kwargs["body"]["content"])["text"],
            "quote anchor",
        )

    def test_temporary_group_is_dissolved_by_the_owner_bot(self) -> None:
        """Cleanup uses the app token against the exact created chat."""
        api = self._api()
        api._request = Mock(return_value={"code": 0, "msg": "success"})

        api.delete_chat("oc_group")

        api._request.assert_called_once_with(
            "DELETE",
            "/open-apis/im/v1/chats/oc_group",
            token="tenant-token-value",
        )

    def test_token_file_rejects_static_env_content(self) -> None:
        """Generated token state can never override persistent dotenv keys."""
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "user-access-token"
            token_file.write_text(
                "user-token-value\nFEISHU_APP_ID=unexpected\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"FEISHU_E2E_USER_ACCESS_TOKEN_FILE": str(token_file)},
                clear=True,
            ):
                with self.assertRaisesRegex(AssertionError, "exactly one token"):
                    live._read_user_access_token()

    def test_suite_resolves_user_and_manages_both_temporary_groups(self) -> None:
        """The token identity provisions and cleans both group modes."""
        api = Mock()
        api.bot_info.return_value = {
            "open_id": "ou_bot",
            "app_name": "Hermes",
        }
        api.user_info.return_value = {
            "open_id": "ou_test",
            "name": "Test User",
        }
        api.resolve_dm_chat_id.return_value = "oc_dm"
        api.create_test_group.side_effect = ["oc_group", "oc_thread_group"]
        api.get_chat_info.return_value = {
            "chat_mode": "group",
            "group_message_type": "thread",
        }
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "user-access-token"
            token_file.write_text("user-token-value\n", encoding="utf-8")
            environment = {
                "FEISHU_APP_ID": "cli_test",
                "FEISHU_APP_SECRET": "secret-test-value",
                "FEISHU_E2E_USER_ACCESS_TOKEN_FILE": str(token_file),
                "FEISHU_E2E_RUN_ID": "run-123",
            }

            with patch.dict(os.environ, environment, clear=True):
                with patch.object(live, "FeishuOpenApi", return_value=api):
                    live.LiveThreadModelTests.setUpClass()
                    try:
                        self.assertEqual(
                            live.LiveThreadModelTests.dm_chat_id,
                            "oc_dm",
                        )
                        self.assertEqual(
                            live.LiveThreadModelTests.group_chat_id,
                            "oc_group",
                        )
                        self.assertEqual(
                            live.LiveThreadModelTests.thread_group_chat_id,
                            "oc_thread_group",
                        )
                        api.user_info.assert_called_once_with()
                        api.resolve_dm_chat_id.assert_called_once_with(
                            "ou_test",
                            "run-123",
                        )
                        self.assertEqual(
                            api.create_test_group.call_args_list,
                            [
                                unittest.mock.call("ou_test", "run-123"),
                                unittest.mock.call(
                                    "ou_test",
                                    "run-123",
                                    group_message_type="thread",
                                ),
                            ],
                        )
                        api.get_chat_info.assert_called_once_with(
                            "oc_thread_group"
                        )
                    finally:
                        live.LiveThreadModelTests.doClassCleanups()

        self.assertEqual(
            api.delete_chat.call_args_list,
            [
                unittest.mock.call("oc_thread_group"),
                unittest.mock.call("oc_group"),
            ],
        )

    def test_keep_chats_retains_both_groups_after_completed_setup(self) -> None:
        """An explicit inspection run leaves its temporary chats available."""
        api = Mock()
        api.bot_info.return_value = {
            "open_id": "ou_bot",
            "app_name": "Hermes",
        }
        api.user_info.return_value = {
            "open_id": "ou_test",
            "name": "Test User",
        }
        api.resolve_dm_chat_id.return_value = "oc_dm"
        api.create_test_group.side_effect = ["oc_group", "oc_thread_group"]
        api.get_chat_info.return_value = {
            "chat_mode": "group",
            "group_message_type": "thread",
        }
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "user-access-token"
            token_file.write_text("user-token-value\n", encoding="utf-8")
            environment = {
                "FEISHU_APP_ID": "cli_test",
                "FEISHU_APP_SECRET": "secret-test-value",
                "FEISHU_E2E_KEEP_CHATS": "1",
                "FEISHU_E2E_USER_ACCESS_TOKEN_FILE": str(token_file),
                "FEISHU_E2E_RUN_ID": "run-123",
            }

            with patch.dict(os.environ, environment, clear=True):
                with patch.object(live, "FeishuOpenApi", return_value=api):
                    live.LiveThreadModelTests.setUpClass()
                    live.LiveThreadModelTests.doClassCleanups()

        api.delete_chat.assert_not_called()

    def test_setup_failure_after_provisioning_still_cleans_both_groups(self) -> None:
        """Registered cleanup survives an exception late in class setup."""
        api = Mock()
        api.bot_info.return_value = {
            "open_id": "ou_bot",
            "app_name": "Hermes",
        }
        api.user_info.return_value = {
            "open_id": "ou_test",
            "name": "Test User",
        }
        api.resolve_dm_chat_id.return_value = "oc_dm"
        api.create_test_group.side_effect = ["oc_group", "oc_thread_group"]
        api.get_chat_info.return_value = {
            "chat_mode": "group",
            "group_message_type": "chat",
        }
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "user-access-token"
            token_file.write_text("user-token-value\n", encoding="utf-8")
            environment = {
                "FEISHU_APP_ID": "cli_test",
                "FEISHU_APP_SECRET": "secret-test-value",
                "FEISHU_E2E_KEEP_CHATS": "1",
                "FEISHU_E2E_USER_ACCESS_TOKEN_FILE": str(token_file),
                "FEISHU_E2E_RUN_ID": "run-123",
            }

            with patch.dict(os.environ, environment, clear=True):
                with patch.object(live, "FeishuOpenApi", return_value=api):
                    with self.assertRaisesRegex(
                        AssertionError,
                        "did not create",
                    ):
                        live.LiveThreadModelTests.setUpClass()
                    live.LiveThreadModelTests.doClassCleanups()

        self.assertEqual(
            api.delete_chat.call_args_list,
            [
                unittest.mock.call("oc_thread_group"),
                unittest.mock.call("oc_group"),
            ],
        )

    def test_fail_closed_case_is_part_of_the_standard_live_suite(self) -> None:
        """Root-recall coverage starts without a separate opt-in flag."""
        case = live.LiveThreadModelTests(
            "test_recalled_root_never_falls_back_to_top_level"
        )
        case.run_id = "run-123"
        case.dm_chat_id = "oc_dm"
        case.api = Mock()
        case.api.create_text_message.side_effect = RuntimeError("case started")

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "case started"):
                case.test_recalled_root_never_falls_back_to_top_level()

    def test_standard_e2e_has_no_manual_fixture_variables(self) -> None:
        """The standard run has no required fixture IDs or manual gates."""
        root = Path(__file__).resolve().parents[1]
        paths = (
            root / ".env.example",
            root / "tests" / "e2e" / "README.md",
            root / "tests" / "e2e" / "acquire_user_access_token.py",
            root / "tests" / "e2e" / "test_live_thread_model.py",
        )
        removed_names = (
            "FEISHU_E2E_EXPECTED_USER_OPEN_ID",
            "FEISHU_E2E_TOPIC_CHAT_ID",
            "FEISHU_E2E_MANUAL",
            "FEISHU_E2E_FAIL_CLOSED",
        )

        for path in paths:
            content = path.read_text(encoding="utf-8")
            for name in removed_names:
                with self.subTest(path=path.name, name=name):
                    self.assertIsNone(re.search(rf"\b{re.escape(name)}\b", content))

    def test_one_command_runner_owns_gateway_and_live_switch(self) -> None:
        """The checked-in wrapper performs the whole post-token lifecycle."""
        root = Path(__file__).resolve().parents[1]
        runner = root / "tests" / "e2e" / "run.sh"

        self.assertTrue(runner.is_file())
        self.assertTrue(os.access(runner, os.X_OK))
        content = runner.read_text(encoding="utf-8")
        self.assertIn("configure_gateway.py", content)
        self.assertIn("up -d", content)
        self.assertIn("FEISHU_E2E=1", content)
        self.assertIn("FEISHU_E2E_RESTART_PHASE=prepare", content)
        self.assertIn("FEISHU_E2E_RESTART_PHASE=verify", content)
        self.assertIn("tests.e2e.test_live_gateway_restart", content)
        self.assertIn("stop --timeout 30 gateway", content)
        self.assertIn("start gateway", content)
        self.assertIn(".State.StartedAt", content)
        self.assertIn("tests.e2e.test_live_thread_model", content)
        self.assertIn("--test", content)
        self.assertIn("LiveThreadModelTests.${test_name}", content)
        self.assertIn("down --remove-orphans", content)
        self.assertIn("E2E diagnostics", content)
        self.assertIn("logs --no-color", content)


if __name__ == "__main__":
    unittest.main()
