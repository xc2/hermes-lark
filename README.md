# hermes-lark

[![CI](https://github.com/xc2/hermes-lark/actions/workflows/ci.yml/badge.svg)](https://github.com/xc2/hermes-lark/actions/workflows/ci.yml)
[![Python 3.11-3.13](https://img.shields.io/badge/python-3.11--3.13-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://github.com/xc2/hermes-lark/blob/main/LICENSE)

`hermes-lark` is an independent Feishu/Lark platform plugin for
[Hermes Agent](https://github.com/NousResearch/hermes-agent). It replaces the
built-in Feishu channel and adapts the behavior and integrations from the
official [`larksuite/openclaw-lark`](https://github.com/larksuite/openclaw-lark)
plugin to Hermes.

This is an independent community project and is not affiliated with or
endorsed by Lark Technologies, ByteDance, or Nous Research.

The current compatibility baseline is
[`openclaw-lark@dde0be3`](https://github.com/larksuite/openclaw-lark/commit/dde0be3680d6fd5443cab426c8f4b3216266346a).

> [!IMPORTANT]
> This project is under active development. The core WebSocket, thread/session,
> CardKit, tool, OAuth, and approval paths have real-tenant E2E coverage, but the
> plugin does not yet provide full OpenClaw behavior parity. Review
> [the parity contract](https://github.com/xc2/hermes-lark/blob/main/docs/PARITY.md) before using it as a production
> replacement.

## Highlights

- WebSocket event delivery for Feishu and Lark self-built applications.
- A fixed Slack-style thread model for DMs and groups.
- One Hermes session per root message, with thread-scoped context and restart
  continuity.
- CardKit streaming states, cumulative response updates, tool status, and
  Hermes approval cards.
- Thirty-nine Feishu tools covering IM, documents, Base, Sheets, Calendar,
  Tasks, Drive, Wiki, OAuth, and interactive questions.
- Nine English-localized Feishu skills derived from the pinned upstream plugin.
- Per-user OAuth Device Flow with encrypted token persistence and refresh.
- DM and group access policies, per-group prompts, and tool allow/deny rules.
- Multi-account routing with isolated credentials, sessions, OAuth state, and
  deduplication state.
- Reproducible Docker-based validation, including a credentialed live-tenant
  E2E suite.

## Requirements

- Python 3.11 through 3.13
- Hermes Agent `>=0.19.1,<0.20.0`
- Node.js 22 or newer
- A published Feishu or Lark self-built application with bot capability

The plugin itself runs in Python. Thirty-seven upstream request/response tools
run through the checked-in Node.js bridge, so Node.js remains a runtime
requirement even when the gateway is otherwise Python-only.

## Installation

Install the project from a reviewed checkout into the same Python environment
that provides the `hermes` executable:

```bash
git clone --branch v1.0.0 https://github.com/xc2/hermes-lark.git
cd hermes-lark
python -m pip install .
hermes plugins enable platforms/feishu --no-allow-tool-override
hermes gateway restart
```

For an editable development checkout, replace the install command with:

```bash
python -m pip install -e '.[dev]'
```

Confirm that Hermes loads `platforms/feishu` from this package rather than its
built-in lazy plugin:

```bash
HERMES_PLUGINS_DEBUG=1 hermes plugins list
hermes gateway status
```

The plugin keeps the platform name `feishu`, so existing Hermes session keys,
cron destinations, and `feishu:<chat_id>` addresses do not need to be renamed.

## Feishu/Lark application setup

1. Create a self-built application in
   [Feishu Open Platform](https://open.feishu.cn/) or
   [Lark Developer](https://open.larksuite.com/).
2. Enable bot capability.
3. Import the appropriate permission manifest:
   - [`permissions/production.json`](https://github.com/xc2/hermes-lark/blob/main/permissions/production.json) for normal
     deployments. Tool-specific user scopes are enabled only when needed.
   - Optionally import [`permissions/skills.json`](https://github.com/xc2/hermes-lark/blob/main/permissions/skills.json)
     after the production baseline to pre-enable the user scopes used by all
     bundled skills.
   - [`permissions/e2e.json`](https://github.com/xc2/hermes-lark/blob/main/permissions/e2e.json) only for the isolated test
     application.
4. Subscribe to the events and callback listed below for the features you use.
5. Select long-connection delivery for both events and callbacks. Webhook
   delivery is not part of the public plugin contract.
6. Publish an application version and include every intended user in its
   availability scope.

| Type | Identifier | Purpose |
| --- | --- | --- |
| Event | `im.message.receive_v1` | Inbound DM and group messages; required |
| Event | `im.message.message_read_v1` | Upstream-compatible read notification registration |
| Event | `im.message.reaction.created_v1` | Reaction notifications and triggers |
| Event | `im.message.reaction.deleted_v1` | Registered for parity; removals do not create turns |
| Event | `im.chat.member.bot.added_v1` | Bot-added lifecycle handling |
| Event | `im.chat.member.bot.deleted_v1` | Bot-removed lifecycle handling |
| Event | `im.chat.access_event.bot_p2p_chat_entered_v1` | P2P entry lifecycle notification |
| Event | `im.message.recalled_v1` | Recall lifecycle notification |
| Event | `drive.notice.comment_add_v1` | Drive comment turns, when enabled |
| Event | `vc.bot.meeting_invited_v1` | Meeting invitation turns, when enabled |
| Callback | `card.action.trigger` | Approval, permission, and interactive-question buttons |

`card.action.trigger` is configured under **Callbacks**, not the ordinary event
subscription list. It is received through the same WebSocket long connection.

The E2E manifest adds user-identity permissions that allow the test driver to
send and recall messages. Do not copy those permissions into a production
application without reviewing the additional authority. See
[`permissions/README.md`](https://github.com/xc2/hermes-lark/blob/main/permissions/README.md) for the exact distinction.

## Quick start

Run the interactive setup:

```bash
hermes gateway setup
```

Or configure the minimum environment manually in `~/.hermes/.env`:

```dotenv
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=replace-with-app-secret
FEISHU_DOMAIN=feishu
FEISHU_CONNECTION_MODE=websocket
```

`FEISHU_DOMAIN` accepts `feishu` for mainland China or `lark` for the
international service.

A minimal YAML configuration is:

```yaml
gateway:
  platforms:
    feishu:
      enabled: true
      appId: cli_xxxxxxxxxxxxxxxx
      appSecret: replace-with-app-secret
      domain: feishu
      connectionMode: websocket
      dmPolicy: pairing
      groupPolicy: open
```

Do not commit application secrets, OAuth tokens, session data, or E2E state.
The repository ignores `.env`, `.hermes-secrets/`, and `.hermes-validation/`.

Detailed policy, streaming, group, and multi-account settings are
documented in [`docs/CONFIGURATION.md`](https://github.com/xc2/hermes-lark/blob/main/docs/CONFIGURATION.md).
For an existing OpenClaw deployment, follow
[`docs/MIGRATION.md`](https://github.com/xc2/hermes-lark/blob/main/docs/MIGRATION.md).

## Thread and session model

The plugin intentionally uses one non-configurable thread model:

- A top-level group message must mention the bot. A top-level DM does not.
- Every admitted top-level message becomes a new Feishu thread root and a new
  Hermes session.
- Follow-up messages in an active thread reuse that root session and do not
  need another mention.
- A group thread whose session is missing, suspended, idle-expired, or
  daily-expired must mention the bot again. DM threads remain mention-exempt.
- Every conversational response stays in the corresponding thread.
- If a thread reply fails, delivery fails closed; the plugin does not create a
  top-level fallback message.

The legacy `threadSession`, `replyInThread`, and `requireMention: false`
settings cannot change this contract.

## CardKit streaming

CardKit streaming is opt-in:

```yaml
streaming: true
replyMode:
  direct: streaming
  group: static
  default: auto
```

With `replyMode: auto`, DMs use CardKit and groups use static replies. One agent
turn creates one CardKit entity and one thread message. The card moves through
Thinking, Generating, tool-running/tool-complete, and terminal success/error
states while the response body is updated cumulatively. Successful cards omit
the fixed completion banner and leave the summary empty so Feishu can derive
the chat-list preview from the card.

While the turn is active, Hermes interim assistant messages and the latest
default `⏳ Working` notification appear in the Generating body instead of
creating separate Feishu messages. The final answer replaces that progress
narration when the turn succeeds.

Dangerous Hermes commands use a separate card in the same thread with Allow
Once, Session, Always, and Deny actions. To avoid duplicate progress UI, disable
Hermes' separate Feishu tool-progress messages when CardKit streaming is on:

```yaml
display:
  platforms:
    feishu:
      tool_progress: "off"
```

Hermes 0.19 does not expose reasoning deltas to platform plugins. Reasoning is
stored in the session transcript but is not streamed into the card. Other
known CardKit differences are listed in [`docs/PARITY.md`](https://github.com/xc2/hermes-lark/blob/main/docs/PARITY.md).

## Pairing and OAuth

The default DM policy is `pairing`. Approve a request with:

```bash
hermes pairing list
hermes pairing approve feishu <CODE>
```

Users can then start Device Flow from Feishu:

```text
/feishu auth
```

Tokens are keyed by application ID and user `open_id`. The plugin stores them
in its own AES-256-GCM encrypted directory under
`$HERMES_HOME/hermes-lark/tokens` (normally
`~/.hermes/hermes-lark/tokens`). Set `HERMES_LARK_TOKEN_STORE_DIR` to choose a
different private directory. OpenClaw credentials are never imported or
modified.

## Commands

The upstream user-facing command surface is:

- `/feishu start`
- `/feishu auth` or `/feishu onboarding`
- `/feishu doctor`
- `/feishu help`
- `/feishu_auth`
- `/feishu_doctor`
- `/feishu_diagnose`

Hermes stores the three underscore command names under internal hyphenated keys.
The corresponding hyphen spellings remain accepted when exposed by the host,
but they are not the primary upstream command names. `/feishu diagnose` is not
an upstream subcommand; use `/feishu_diagnose` for the all-account report.

Identity-dependent commands fail closed when invoked outside a Feishu message
because a CLI invocation has no authoritative sender identity.

The upstream commands are registered with `requireAuth: true`. Hermes 0.19 has
no per-plugin equivalent flag because its gateway applies ordinary channel
authorization before invoking any plugin command handler. The mapping therefore
uses that host gate for every upstream spelling and internal key. Hermes' optional
slash-command administrator ACL can further restrict commands when configured;
that additional host-wide policy is an accepted host difference, not a Feishu
DM-only or diagnostics-only restriction.

## Docker validation

The validation image pins Hermes Agent 0.19.1. A host installation of Hermes is
not required:

```bash
cp .env.example .env
chmod 600 .env
docker compose -f compose.validation.yaml build
docker compose -f compose.validation.yaml run --rm --no-deps \
  --entrypoint /opt/hermes/.venv/bin/python gateway \
  /opt/hermes-lark/tests/e2e/configure_gateway.py
docker compose -f compose.validation.yaml run --rm gateway \
  plugins list --plain --no-bundled
```

The credentialed E2E suite requires a dedicated test application and a user
access token. After acquiring the token, run:

```bash
./tests/e2e/run.sh
```

Use `./tests/e2e/run.sh --keep-chats` to preserve the generated group chats for
manual inspection. The complete setup, permissions, assertions, and cleanup
rules are in [`tests/e2e/README.md`](https://github.com/xc2/hermes-lark/blob/main/tests/e2e/README.md).

## Development

Install development dependencies and run the offline suite:

```bash
git clone --filter=blob:none https://github.com/NousResearch/hermes-agent.git ../hermes-agent
git -C ../hermes-agent checkout --detach cc4cab2f592e60a197e796506de9168f74baf3ea
python -m pip install -e ../hermes-agent
python -m pip install -e '.[dev]'
python -m ruff check .
python scripts/check_english.py
python scripts/check_project.py
python -m unittest discover -s tests -t . -v
python -m build
python -m twine check dist/*
```

The generated Node bridge is intentionally checked in so installed wheels do
not need a TypeScript build toolchain. Its pinned-source rebuild procedure is
documented in [`hermes_lark/node/README.md`](https://github.com/xc2/hermes-lark/blob/main/hermes_lark/node/README.md).

See [`CONTRIBUTING.md`](https://github.com/xc2/hermes-lark/blob/main/CONTRIBUTING.md) before changing tool schemas, skills,
the generated bridge, permission manifests, or live E2E behavior.

## Security

Never post an application secret, user access token, refresh token, OAuth code,
session database, or raw gateway log in an issue. Review
[`SECURITY.md`](https://github.com/xc2/hermes-lark/blob/main/SECURITY.md) for private reporting instructions.

## Project status

The repository tracks both implemented behavior and known differences from the
pinned upstream release in [`docs/PARITY.md`](https://github.com/xc2/hermes-lark/blob/main/docs/PARITY.md). In particular,
tool registration does not by itself prove tenant permissions, OAuth grants,
network delivery, or API runtime behavior.

## Contributing

Contributions are welcome. Please read [`CONTRIBUTING.md`](https://github.com/xc2/hermes-lark/blob/main/CONTRIBUTING.md) and
the [`CODE_OF_CONDUCT.md`](https://github.com/xc2/hermes-lark/blob/main/CODE_OF_CONDUCT.md) before opening a pull request.

## License

This project is licensed under the MIT License. It contains derived work from
`larksuite/openclaw-lark` and `NousResearch/hermes-agent`; see [`NOTICE`](https://github.com/xc2/hermes-lark/blob/main/NOTICE)
and [`LICENSE`](https://github.com/xc2/hermes-lark/blob/main/LICENSE) for attribution and license terms. Licenses for
dependencies included in the generated Node bundle are reproduced in
[`THIRD_PARTY_NOTICES.md`](https://github.com/xc2/hermes-lark/blob/main/THIRD_PARTY_NOTICES.md).
