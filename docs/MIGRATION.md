# Migrating from openclaw-lark

The compatibility baseline for this project is
[`openclaw-lark@dde0be3`](https://github.com/larksuite/openclaw-lark/commit/dde0be3680d6fd5443cab426c8f4b3216266346a).
Migration preserves most Feishu configuration names, but it does not migrate
running process state.

## Migration procedure

1. Record the current `channels.feishu` configuration, named accounts, and
   group rules.
2. Install `hermes-lark` into the Python environment that runs Hermes.
3. Enable `platforms/feishu` and confirm that it replaces the built-in plugin.
4. Move the contents of `channels.feishu` to
   `gateway.platforms.feishu`, then add `enabled: true`.
5. Keep an upstream `chunkMode` value unchanged. The accepted values are
   `newline`, `paragraph`, and `none`; see the compatibility note below.
6. Remove `threadSession` and `replyInThread`. The Hermes plugin always uses its
   fixed root-message thread/session model.
7. Run `/feishu auth` again. The plugin intentionally does not import OpenClaw
   OAuth credentials. A trusted host may instead provide a message-scoped
   token provider.
8. Restart the gateway and validate every enabled surface in a test tenant.
9. Stop the old OpenClaw gateway only after validation, so two consumers do not
   compete for the same application event stream.

## Example

OpenClaw:

```yaml
channels:
  feishu:
    appId: cli_xxxxxxxxxxxxxxxx
    appSecret: replace-with-app-secret
    domain: feishu
    connectionMode: websocket
    dmPolicy: pairing
    groupPolicy: open
```

Hermes:

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

## State that is not migrated

- OpenClaw conversation history
- CardKit entity state
- Pending OAuth operations
- Pending interactive questions
- In-flight tool calls

OpenClaw OAuth records are not migrated. Hermes stores newly authorized tokens
in its own encrypted credential directory.

## Chunk-mode compatibility

The pinned upstream schema accepts `newline`, `paragraph`, and `none`. Its
OpenClaw 2026.4.9 runtime only gives `newline` special handling, splitting at
blank-line paragraph boundaries. `paragraph` and `none` both use hard length
chunking. Hermes preserves this upstream quirk and always enforces
`textChunkLimit` so a migrated `none` setting cannot create an oversized send.

Early development versions of this Hermes plugin documented `length`. Replace
that value with `none`; the delivery behavior is unchanged.

## Known configuration differences

The following upstream settings do not currently have equivalent Hermes 0.19
runtime behavior:

- `blockStreaming`
- `toolUseDisplay.showFullPaths` and `/verbose` tool-result modes
- CardKit footer and reasoning-delta presentation
- Per-group `skills`

The upstream `blockStreamingCoalesce` key is accepted, but the pinned upstream
plugin itself does not consume it directly at runtime.

See [`PARITY.md`](PARITY.md) for the complete behavioral boundary.
