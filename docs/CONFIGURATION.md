# Configuration reference

`hermes-lark` accepts OpenClaw-style camelCase Feishu configuration under
`gateway.platforms.feishu`. Hermes-style snake_case values may also be placed
under `extra`.

## Minimal configuration

```yaml
gateway:
  platforms:
    feishu:
      enabled: true
      appId: cli_xxxxxxxxxxxxxxxx
      appSecret: replace-with-app-secret
      domain: feishu
      connectionMode: websocket
```

Credentials may instead be stored in `~/.hermes/.env`:

```dotenv
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=replace-with-app-secret
FEISHU_DOMAIN=feishu
FEISHU_CONNECTION_MODE=websocket
```

Never store a real secret in a repository configuration file.

## Defaults

| Setting | Default | Behavior |
| --- | --- | --- |
| `connectionMode` | `websocket` | Uses the Feishu/Lark long connection |
| `dmPolicy` | `pairing` | Unknown DM users must complete Hermes pairing |
| `groupPolicy` | `open` | Group members may trigger the bot, subject to the root mention gate |
| `allowBots` | `mentions` | Accepts another bot only when it explicitly mentions this bot |
| `reactionNotifications` | `own` | Injects reactions only for messages sent by this bot |
| `historyLimit` | `50` | Adds at most 50 earlier human messages from an unactivated native thread |
| `textChunkLimit` | `4000` | Maximum characters in one outbound text chunk |
| `chunkMode` | `none` | Accepts the pinned upstream values `newline`, `paragraph`, and `none` |
| `mediaMaxMb` | `30` | Maximum inbound media size before caching |
| `streaming` | unset | CardKit streaming is enabled only by explicit `true` |
| `replyMode` | `auto` | With streaming enabled, DMs use CardKit and groups use static replies |
| `dedup.maxEntries` | `5000` | Maximum persisted message deduplication keys |
| `dedup.ttlMs` | `43200000` | Twelve-hour message deduplication window |

`requireMention` remains accepted for migration compatibility, but it cannot
disable the fixed group-root mention gate. `threadSession` and `replyInThread`
are ignored because the plugin always uses its fixed root-message thread model.

The pinned upstream schema names for `chunkMode` contain a compatibility quirk.
Its OpenClaw 2026.4.9 runtime checks only for `newline`, which splits at safe
blank-line paragraph boundaries before applying `textChunkLimit`. Both
`paragraph` and `none` fall through to the same hard length chunking. This
plugin preserves that behavior; omitting `chunkMode` is equivalent to `none`.

## Access policy

### Direct messages

`dmPolicy` accepts:

- `pairing`: unknown users receive the Hermes pairing flow.
- `allowlist`: only users in `allowFrom` are admitted.
- `open`: all users are admitted. Use only with an explicit risk review.
- `disabled`: DMs are ignored.

An environment-only allowlist can be configured with:

```dotenv
FEISHU_ALLOWED_USERS=ou_user_1,ou_user_2
FEISHU_ALLOW_ALL_USERS=false
```

Do not set `FEISHU_ALLOW_ALL_USERS=true` in a normal production deployment.

### Groups

`groupPolicy` accepts:

| Policy | Behavior |
| --- | --- |
| `open` | Any member may use the bot; a top-level message must still mention it |
| `allowlist` | Only users in `allowFrom` are admitted |
| `blacklist` | Every user except entries in `blacklist` is admitted |
| `admin_only` | Only users in the global `admins` list are admitted |
| `disabled` | The group is ignored |

Per-group rules use the chat ID as the key. `groups."*"` is the fallback for
groups without an explicit entry. If explicit group IDs exist without a `"*"`
entry, unlisted groups are rejected.

```yaml
gateway:
  platforms:
    feishu:
      enabled: true
      dmPolicy: pairing
      groupPolicy: open
      allowBots: mentions
      reactionNotifications: own

      groups:
        "*":
          systemPrompt: "Use concise answers in general group chats."
        oc_project_room:
          enabled: true
          groupPolicy: allowlist
          allowFrom:
            - ou_user_1
            - ou_user_2
          systemPrompt: "Handle only Project Alpha in this group."
          tools:
            allow:
              - feishu_*
            deny:
              - shell
```

`allowBots` accepts `none`, `mentions`, or `all`. Enabling bot-originated
messages increases bot-loop risk.

Per-group `systemPrompt`, access policy, and `tools.allow`/`tools.deny` are
active at runtime. The wildcard group's tool policy is intentionally not used
as a fallback; security-sensitive tool restrictions must be configured on each
specific chat. Per-group `skills` are parsed but cannot currently be applied at
reply time by the Hermes 0.19 plugin interface.

## CardKit replies

```yaml
gateway:
  platforms:
    feishu:
      enabled: true
      streaming: true
      replyMode:
        direct: streaming
        group: static
        default: auto
```

`replyMode` accepts `auto`, `static`, `streaming`, or a mapping with `direct`,
`group`, and `default` values. Streaming remains disabled unless `streaming` is
explicitly `true`.

When CardKit is enabled, disable Hermes' separate tool-progress messages to
avoid duplicate status output:

```yaml
display:
  platforms:
    feishu:
      tool_progress: "off"
```

## Tool policy

Channel-level category switches and per-group tool rules affect both tool
visibility and invocation authorization. Supported upstream category switches
include `doc`, `wiki`, `drive`, `perm`, and `scopes`.

```yaml
gateway:
  platforms:
    feishu:
      enabled: true
      tools:
        doc: true
        wiki: true
        drive: false
```

## Multi-account configuration

Each account owns an independent connection, policy set, deduplication file,
OAuth identity, and routing namespace:

```yaml
gateway:
  platforms:
    feishu:
      enabled: true
      connectionMode: websocket
      dmPolicy: pairing
      groupPolicy: open

      accounts:
        cn:
          enabled: true
          appId: cli_cn_xxxxxxxxx
          appSecret: replace-with-cn-secret
          domain: feishu
        global:
          enabled: true
          appId: cli_global_xxxxxx
          appSecret: replace-with-global-secret
          domain: lark
```

Account maps inherit top-level scalar values. Nested maps are merged one level
deep. Inbound chat IDs are represented internally as
`<account_id>::<chat_id>`, and outbound replies are routed back to the source
account automatically. Standalone and cron delivery accept the same namespaced
address and fail closed for an unknown account.

Multi-account behavior has offline coverage but still requires tenant-specific
acceptance before production use.

## User access tokens

Most document, calendar, task, Base, and user-identity IM operations require a
user access token. Start Device Flow with `/feishu auth` or the
`feishu_oauth_batch_auth` tool.

The production permission manifest is a least-privilege baseline, not an
all-tools grant. Tool-specific user scopes must first be enabled and published
for the application; the plugin presents the exact missing scopes to the
application owner when an operation requires them. User OAuth can grant only
scopes that the application already exposes.

The secure token key is `appId:userOpenId`. The plugin uses its own AES-256-GCM
encrypted store at `$HERMES_HOME/hermes-lark/tokens` by default. Set
`HERMES_LARK_TOKEN_STORE_DIR` to select another private directory, such as a
persistent container mount. The plugin never reads or writes OpenClaw's token
store.

The bridge refreshes expiring access tokens, handles refresh-token rotation,
and removes records that the provider reports as revoked. Refreshes are
serialized per application and user across one-shot Node workers. Set
`HERMES_LARK_UAT_LOCK_DIR` to an absolute private directory only when the
system temporary directory is not shared by all local gateway workers.

The following environment variables exist only for controlled single-user
tests and are unsuitable for a multi-user production gateway:

```dotenv
FEISHU_USER_ACCESS_TOKEN=u-xxxxxxxx
FEISHU_USER_REFRESH_TOKEN=
FEISHU_USER_ACCESS_TOKEN_SCOPES=
FEISHU_USER_ACCESS_TOKEN_EXPIRES_AT=
FEISHU_USER_REFRESH_TOKEN_EXPIRES_AT=
```

Embedded hosts may instead provide message-scoped tokens through
`hermes_lark.openclaw_tools.configure_token_provider(...)`.

## Tool execution deadline

The bridge does not impose an outer tool timeout by default because upstream
operations have their own operation-specific deadlines. An operator may add a
host deadline when required:

```dotenv
FEISHU_OPENCLAW_TOOL_TIMEOUT_SECONDS=120
```

Unset, `0`, `off`, `none`, and `disabled` leave the outer deadline disabled.
When a configured deadline expires, the remote operation may already have
succeeded, so retry only after checking its state.

## Hermes-style snake_case

Snake-case values may be placed under `extra`:

```yaml
gateway:
  platforms:
    feishu:
      enabled: true
      extra:
        app_id: cli_xxxxxxxxxxxxxxxx
        app_secret: replace-with-app-secret
        connection_mode: websocket
        dm_policy: pairing
        group_policy: open
```

OpenClaw-style camelCase is preferred for configurations migrated from the
upstream plugin.
