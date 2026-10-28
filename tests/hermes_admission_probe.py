"""Subprocess integration probe for Hermes' durable message admission."""

from __future__ import annotations

import asyncio
import os
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session_authority import initialize_session_authority
from gateway.session_results import execution_result
from hermes_cli.plugins_manifest import running_hermes_version, version_satisfies

from hermes_lark import adapter as adapter_module


_STUB_REPLY = "stub turn reply"


class HermesAdmissionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Send Feishu events through Hermes' real admission, replay, and delivery."""

    @classmethod
    def setUpClass(cls) -> None:
        """Pin the host behavior asserted by this integration contract."""
        manifest = (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text(
            encoding="utf-8"
        )
        match = re.search(r'(?m)^requires_hermes:\s*"?([^"\n]+)"?', manifest)
        if match is None:
            raise AssertionError("plugin.yaml declares no requires_hermes")
        required = match.group(1).strip()
        host_version = running_hermes_version()
        if not version_satisfies(required, host_version):
            raise AssertionError(
                f"expected Hermes Agent {required}, found {host_version}"
            )

    async def asyncSetUp(self) -> None:
        """Wire one unconnected adapter to a real runner and session authority."""
        self.auth_env = patch.dict(
            os.environ,
            {
                "FEISHU_ALLOWED_USERS": "",
                "FEISHU_ALLOW_ALL_USERS": "",
                "GATEWAY_ALLOWED_USERS": "",
                "GATEWAY_ALLOW_ALL_USERS": "",
            },
        )
        self.auth_env.start()
        self.adapter = adapter_module.FeishuAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "app_id": "cli_admission",
                    "app_secret": "secret",
                    "dmPolicy": "open",
                    "groupPolicy": "allowlist",
                    "groupAllowFrom": ["ou_member"],
                },
            )
        )
        self.runner = GatewayRunner(
            GatewayConfig(sessions_dir=Path(os.environ["HERMES_HOME"]) / "sessions")
        )
        self.adapter.gateway_runner = self.runner
        self.runner.adapters = {Platform.FEISHU: self.adapter}
        self.authority = await initialize_session_authority(
            self.runner,
            profile_id="default",
            instance_id="hermes-lark-admission",
        )
        self.runner._wire_adapter_handlers(self.adapter)
        self.sent: list[tuple[str, str, dict]] = []
        self.turns: list[MessageEvent] = []

        async def send(chat_id, content, reply_to=None, metadata=None, **_kwargs):
            self.sent.append((chat_id, str(content), dict(metadata or {})))
            return SimpleNamespace(
                success=True,
                message_id=f"om_reply_{len(self.sent)}",
                error=None,
                raw_response=None,
                retryable=False,
            )

        async def turn(event, *_args, **_kwargs):
            self.turns.append(event)
            execution_result.get()["result"] = {
                "final_response": _STUB_REPLY,
                "messages": [],
            }
            return _STUB_REPLY

        self.adapter.send = send
        self.runner._handle_message_with_agent = turn

    async def asyncTearDown(self) -> None:
        """Close the session store and restore process authorization."""
        self.authority.db.close()
        self.auth_env.stop()

    def _event(self, *, message_id: str, chat_id: str, chat_type: str) -> MessageEvent:
        """Build an inbound event in the shape the adapter dispatches."""
        is_group = chat_type != "p2p"
        thread_id = f"{message_id}_root" if is_group else message_id
        source = self.adapter.build_source(
            chat_id=chat_id,
            chat_name="Admission",
            chat_type="group" if is_group else "dm",
            user_id="u_tenant",
            user_name="Member",
            thread_id=thread_id,
            user_id_alt="on_union",
            message_id=message_id,
            role_authorized=self.adapter._role_authorized_for_admitted_message(
                SimpleNamespace(chat_type=chat_type)
            ),
        )
        event = MessageEvent(
            text=f"hello {message_id}",
            message_type=MessageType.TEXT,
            source=source,
            raw_message=SimpleNamespace(
                event=SimpleNamespace(
                    sender=SimpleNamespace(
                        sender_type="user",
                        sender_id=SimpleNamespace(
                            open_id="ou_member",
                            user_id="u_tenant",
                            union_id="on_union",
                        ),
                    ),
                    message=SimpleNamespace(
                        message_id=message_id,
                        chat_id=chat_id,
                        chat_type=chat_type,
                    ),
                )
            ),
            message_id=message_id,
        )
        setattr(
            event,
            adapter_module.FEISHU_METADATA_ATTR,
            {"feishu_session_thread_id": thread_id},
        )
        return event

    async def _dispatch(self, event: MessageEvent) -> None:
        """Enter through the adapter and wait for its reply delivery."""
        await self.adapter._handle_message_with_guards(event)
        for _ in range(200):
            if self.sent:
                break
            await asyncio.sleep(0.05)

    async def test_role_granted_group_message_replays_in_its_thread(self) -> None:
        """An allowlisted group sender's turn is admitted, replayed, and answered."""
        await self._dispatch(
            self._event(message_id="om_group", chat_id="oc_group", chat_type="group")
        )

        self.assertEqual(len(self.turns), 1)
        replayed = self.turns[0]
        self.assertEqual(replayed.message_id, "om_group")
        self.assertEqual(replayed.source.thread_id, "om_group_root")
        self.assertTrue(replayed.source.role_authorized)
        self.assertEqual(
            [(chat, content, meta.get("thread_id")) for chat, content, meta in self.sent],
            [("oc_group", _STUB_REPLY, "om_group_root")],
        )

    async def test_open_dm_role_grant_is_reauthorized(self) -> None:
        """An open-policy DM passes the adapter's durable role re-check."""
        await self._dispatch(
            self._event(message_id="om_dm", chat_id="oc_dm", chat_type="p2p")
        )

        self.assertEqual(len(self.turns), 1)
        self.assertEqual(self.sent[0][1], _STUB_REPLY)

    async def test_role_grant_requires_the_adapter_recheck(self) -> None:
        """Without the re-check hook Hermes refuses the role-granted turn."""
        with patch.object(
            adapter_module.FeishuAdapter,
            "reauthorize_native_roles",
            None,
        ):
            await self._dispatch(
                self._event(
                    message_id="om_unchecked",
                    chat_id="oc_unchecked",
                    chat_type="group",
                )
            )

        self.assertEqual(self.turns, [])
        self.assertNotIn(_STUB_REPLY, [content for _chat, content, _meta in self.sent])


if __name__ == "__main__":
    unittest.main()
