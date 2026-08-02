# Live Feishu tenant E2E tests

This suite observes real Feishu messages, reactions, `root_id`, `parent_id`, and
`thread_id` values through OpenAPI. It reads canonical session rows and
transcripts through Hermes' public `SessionDB` API to verify the fixed
Slack-style thread model. Session assertions do not depend on adapter-private
state or query SQLite tables directly.

Feishu's IM get/list APIs return only the CardKit snapshot that was originally
sent, not subsequent entity updates. The test configuration therefore enables
an additional JSONL trace. Each successful CardKit request records its
operation, sequence, response code, and sent payload. This trace is
observational only and does not influence runtime behavior.

Ordinary unit tests never connect to a tenant. The repository runner enables
the live suite internally.

## Coverage

- A top-level DM needs no mention. Each top-level message creates an isolated
  thread and Hermes session.
- An admitted DM caller can invoke `/feishu help` through Hermes' command
  authorization gate, and the command response stays in that message thread.
- Every DM, regular group, and thread-mode group root is checked for its session
  ID, public session key, `chat_id`, `chat_type`, canonical `om_*` root, and
  persisted transcript.
- Follow-ups in the same DM thread retain context. A new top-level DM does not
  inherit another session. After a complete gateway stop/start, a follow-up in
  the original thread keeps its session ID, key, and context.
- A new root in a regular group requires a mention. Follow-ups in an active
  thread need no mention and retain context.
- When a human-only thread already contains several messages, its first bot
  mention creates a session in that native thread and imports the earlier
  thread messages as context. No session may exist before that mention.
- Each run creates a fresh group with `group_message_type=thread` and applies
  the same group mention, thread, and context cases.
- Quoting a DM root creates a new thread/session rooted at the quote message; it
  does not inherit the quoted message's root.
- Complex Markdown is streamed through three provider deltas released by the
  test. The suite requires one real CardKit entity and one thread message,
  observes Thinking, Generating, three cumulative `cardElement.content`
  updates, closing `streaming_mode`, and the Complete full-card update, with no
  duplicate partial messages.
- A deterministic remote PNG is initially stripped from a CardKit frame, then
  re-flushed as an `img_*` key after Feishu upload. The raw URL may not reach a
  card request or its E2E trace, and terminal completion must retain that key.
- A deterministic provider emits real `reasoning_content`, followed by a
  terminal tool call. The suite checks running/completed tool status on the card
  and matching assistant `tool_calls`, tool result, and reasoning fields in
  `SessionDB`.
- A sensitive terminal command must produce an approval card in the same
  thread, with Allow Once, Session, Always, and Deny actions. The command may not
  execute before approval. The test sends `/deny` in that thread through
  Hermes' blocking approval resolver, then checks that the sentinel remains and
  the tool row is denied.
- Long Markdown is forced into at least three chunks. All chunks must stay in
  the same root/thread and reconstruct the provider output exactly after
  removing the `(n/N)` display markers.
- While a provider barrier is blocked, the user's message must have the app's
  `Typing` reaction. The reaction must be removed after a successful response.
- A `THUMBSUP` added by the test user to a bot reply must create one synthetic
  turn in the same thread and persisted Hermes session.
- While a provider barrier is blocked, `/stop` in the active DM thread must
  reach Hermes and produce its stopped response before the provider is released.
- A deterministic PNG and text file are uploaded and sent as the test user.
  The suite compares Feishu's resource bytes, the adapter cache, the
  model-visible native image, and the persisted thread session. The file
  follow-up must reuse the image root's session without byte drift.
- Deterministic gateway-local PNG and text fixtures are returned through two
  `MEDIA:` directives. The bot must upload both, reply inside the originating
  thread, and expose resources whose downloaded bytes match the fixtures.
- After recalling the canonical DM root, delivery must fail closed instead of
  falling back to a top-level message. A barrier controls this race without a
  fixed sleep.

Feishu distinguishes these group types:

- `chat_mode=group` with `group_message_type=thread` is a thread-mode group. It
  can be created through OpenAPI and is the recommended Open Platform form.
- `chat_mode=topic` is a topic group and cannot be created through the chat
  creation API.

The live suite uses the first form so every run can provision and inspect it
without a pre-existing chat ID. The adapter routes both forms through the same
thread-capable path; offline unit tests cover the event shape of a topic group.

## Test application

The test application must:

- have a published bot capability;
- subscribe to `im.message.receive_v1` over WebSocket;
- subscribe to `im.message.reaction.created_v1` over WebSocket;
- have [`permissions/e2e.json`](../../permissions/e2e.json) imported in the
  Open Platform console;
- include the test user in its availability scope; and
- be consumed by only this validation gateway during the run, so an old
  OpenClaw process or another gateway does not take the WebSocket events.

The E2E permission manifest is a superset of the production manifest. App-level
`im:chat:create`, `im:chat:read`, and `im:chat:delete` scopes create, verify, and
remove temporary groups. User-level `im:message`,
`im:message.send_as_user`, and `im:message:recall` scopes let the runner send and
recall messages as the test user. `im:message.group_msg` ensures that negative
group cases without a mention still reach the gateway and exercise the mention
gate.

Feishu's [image upload](https://open.feishu.cn/document/server-docs/im-v1/image/create)
and [file upload](https://open.feishu.cn/document/server-docs/im-v1/file/create)
APIs accept a tenant access token, not a user access token. The media cases
therefore upload fixtures as the test app, then use the existing user access
token to send the returned resource keys as the human test user. This identity
split is fully automated and does not require another scope or fixture ID.

The validation gateway fixes `dmPolicy` and `groupPolicy` to `open`. The
top-level group mention gate and thread session behavior have no configuration
switches.

## Persistent credentials

Only Docker is required on the host; Hermes does not need to be installed.
Store the stable test app credentials in the repository-root `.env`, separate
from the rebuildable `.hermes-validation/` directory:

```bash
cp .env.example .env
chmod 600 .env
```

```dotenv
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=replace-locally
FEISHU_DOMAIN=feishu
FEISHU_CONNECTION_MODE=websocket
```

Use `FEISHU_DOMAIN=lark` for the international service. Never put an app secret
or token in a command-line argument, chat message, or version-controlled file.

## Acquire a user access token

After importing the E2E permissions and publishing the app, run:

```bash
mkdir -p .hermes-validation .hermes-secrets
chmod 700 .hermes-validation .hermes-secrets
docker compose -f compose.validation.yaml build gateway
docker compose -f compose.validation.yaml run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  --entrypoint /opt/hermes/.venv/bin/python gateway \
  /opt/hermes-lark/tests/e2e/acquire_user_access_token.py
```

The script uses OAuth Device Flow and asks only for browser authorization by
the test user. It requests the three user scopes required by the live suite,
then calls `/authen/v1/user_info` with the token to derive and display the
user's name and `open_id`. No separate user ID setting or second terminal
confirmation is required.

Only the short-lived access token is written to the ignored
`.hermes-secrets/user-access-token` file, with mode `0600`. The script does not
request `offline_access`, persist a refresh token, or print any access token,
refresh token, or app secret. Run it again after the token expires.

## Run the suite

After acquiring the token, run:

```bash
./tests/e2e/run.sh
```

The runner builds the images and deterministic gateway configuration, starts
the model stub and gateway, waits for a real Feishu WebSocket handshake,
performs restart preparation, fully stops and starts the gateway, waits for a
new connection using only logs after the current container `StartedAt`, verifies
restart continuity, and then executes the message matrix. It stops this run's
Compose services at the end.

The runner enables live tests internally; `FEISHU_E2E` does not belong in
`.env`. It does not remove `.hermes-validation/`, `.hermes-secrets/`, or the
saved token. On failure it emits bounded, credential-redacted gateway and model
stub diagnostics before teardown.

Temporary regular and thread-mode groups are deleted by default. To retain both
groups for manual inspection in the Feishu client, run:

```bash
./tests/e2e/run.sh --keep-chats
```

During development, run only the live cases that exercise a focused change by
repeating `--test` with method names from `LiveThreadModelTests`:

```bash
./tests/e2e/run.sh --keep-chats \
  --test test_dm_authorized_feishu_command_replies_in_its_thread \
  --test test_dm_user_reaction_creates_a_turn_in_the_same_session
```

Focused mode still builds, configures, starts, waits for, and tears down the
real gateway. It skips the separate restart-continuity phase and runs the
selected methods in one provisioned test class.

`--keep-chats` prints and retains both `chat_id` values even when a test fails. If
class setup fails before provisioning is complete, any partially created groups
are still cleaned up. The runner never deletes test messages from the DM.

The runner derives the user's `open_id` from the access token, sends a bootstrap
message to that user, and reads the canonical P2P `chat_id` from the response.
It creates new regular and thread-mode groups on every run. It also reads the
latter back and asserts `chat_mode=group` and `group_message_type=thread`. No DM,
group, message, bot, root, or thread IDs need to be configured.

The deterministic model stub supports these protocols:

- `HERMES_E2E_EXPECT:<marker>` returns the complete marker verbatim.
- `HERMES_E2E_REMEMBER:<value>` followed by `HERMES_E2E_RECALL` reads the value
  from the request history and returns `HERMES_E2E_CONTEXT:<value>`.
- `HERMES_E2E_STREAM:<marker>` emits three Markdown deltas, released through
  `/e2e/stream-advance`.
- `HERMES_E2E_CARDKIT_IMAGE:<marker>` emits a held Markdown image URL followed
  by a final marker. The gateway downloads the deterministic PNG from the
  model stub, uploads it to Feishu, and the test releases the final delta only
  after observing the resolved `img_*` re-flush.
- `HERMES_E2E_TOOL:<marker>` first emits reasoning and a terminal tool call,
  then streams the final marker only after receiving the corresponding
  `role=tool` row.
- `HERMES_E2E_APPROVAL:<marker>` asks to remove a unique sentinel in an isolated
  data directory, reliably triggering Hermes' dangerous-command approval. It
  returns the final marker after receiving a denied tool row.
- `HERMES_E2E_LONG:<marker>` returns approximately 2,500 characters of Markdown
  that can be reconstructed exactly.
- `HERMES_E2E_DELAY_BARRIER:<marker>` blocks inside the provider. The test
  confirms entry through `/e2e/delay-barrier-active` and releases it through
  `/e2e/delay-barrier-release`.
- A native image produces `HERMES_E2E_IMAGE_SHA256:<digest>` from the exact
  base64 payload received at the model boundary.
- `HERMES_E2E_MEDIA_RETURN:<marker>` returns deterministic image and file
  `MEDIA:` directives rooted in the shared E2E data directory.

The E2E gateway explicitly enables `streaming: true`, `replyMode: auto`, and
processing reactions. It marks the deterministic model as vision-capable so
the image-integrity case observes the original native payload at the provider
boundary. It also permits private URLs only inside the isolated validation
gateway so the CardKit resolver can fetch the Compose-local model-stub PNG;
production configuration remains fail-closed. DMs therefore exercise CardKit while groups exercise the static
chunking path. Hermes' separate Feishu tool progress is disabled so the CardKit
tool panel owns status display. The test-only `textChunkLimit` is 1,000 to force
real chunking; the production default remains 4,000.
`session_reset.mode` is fixed to `none` so time-based policy cannot disturb
restart continuity.

DM cases run first. If one times out before its root acquires a `thread_id`,
check the gateway log for Feishu error `230071`. That means the tenant does not
support `reply_in_thread=true` for P2P messages. The runner will not hide the
problem with a top-level fallback.

Only slow tenants should need larger timeouts:

```dotenv
FEISHU_E2E_TIMEOUT_SECONDS=120
FEISHU_E2E_QUIET_SECONDS=15
```

## Live boundaries not yet covered

The matrix prioritizes thread/session behavior and conversational delivery. A
passing run does not validate these remaining live boundaries:

- real bidirectional audio and video transfer and resource integrity;
- synthetic turns triggered by a physical Feishu-client click on approval or
  question cards, Drive comments, and meeting invitations;
- tenant API execution of all 39 registered tools; or
- duplicate delivery of the same server-originated WebSocket event.

The four approval action-to-resolver mappings have offline behavior tests.
Open Platform provides no public API for clicking a card as a user, so this
suite does not present a synthetic callback as a live click. Likewise, sending
the same API request `uuid` is deduplicated by Feishu and cannot reliably cause
the adapter to receive the same `message_id` twice.

## Verify the default offline skip

Without loading credentials, verify that the suite stays offline:

```bash
docker run --rm \
  --workdir /opt/hermes-lark \
  hermes-lark:validation \
  /opt/hermes/.venv/bin/python -m unittest -v \
  tests.e2e.test_live_thread_model
```

Every live test should be reported as `skipped`.
