# OpenClaw Lark parity contract

This project adapts
[`larksuite/openclaw-lark`](https://github.com/larksuite/openclaw-lark) for the
Hermes Agent plugin interface. The reviewed compatibility baseline is upstream
commit `dde0be3680d6fd5443cab426c8f4b3216266346a`.

Parity means preserving the relevant API and behavior contract. It does not
mean keeping byte-identical prose: repository-authored tool descriptions,
skills, diagnostics, cards, tests, and documentation are maintained in
English.

## Offline acceptance gate

Run the standard-library-only contract with:

```bash
python3 scripts/check_english.py
python3 scripts/check_project.py
python3 tests/test_parity_contract.py -v
```

The test parses source, manifests, skills, and the tool inventory. It also
registers every tool against a fake Hermes context without importing Hermes or
opening a Feishu connection.

The pinned fixture is
[`tests/fixtures/openclaw_lark_parity.json`](../tests/fixtures/openclaw_lark_parity.json).
It records upstream provenance independently from the implementation.

| Surface | Contract |
| --- | --- |
| Plugin | The `platforms/feishu` entry point registers the complete Hermes platform surface. |
| Tools | All 39 upstream names remain in registration order. Non-localized schema structure is pinned per tool. |
| Skills | Nine skills and four reference files retain their inventory, identity frontmatter, command/tool references, and Hermes registration. |
| Defaults | WebSocket transport, open group policy, mention-only bot admission, required group-root mention, bounded deduplication, and 12-hour message deduplication remain pinned. |
| Events | Message, read, reaction, card action, bot membership, P2P entry, recall, document comment, and meeting invitation registrations remain present. |
| Generated bridge | The checked-in Node bundle must match its reviewed SHA-256 digest and pinned upstream banner, including documented downstream overrides. |

Focused unit tests cover commands, OAuth continuations, policy, thread routing,
CardKit state, approvals, delivery, multi-account isolation, and callback
authorization. The credentialed suite covers the live tenant paths documented
in [`tests/e2e/README.md`](../tests/e2e/README.md).

## Document-comment identity override

The pinned upstream `feishu_doc_comments` description promises user-identity
execution, but its `list`, `list_replies`, `create`, and `reply` paths use tenant
identity while `patch` uses user identity. Hermes normalizes all six tenant call
sites in that module to user identity during bundling so every documented
action uses the requesting user's UAT. The two raw `reply` requests also receive
the SDK's UAT request options; changing only their identity selector would leave
the HTTP authorization on the tenant token. The build verifies the complete
reviewed upstream source digest before applying the override, then also checks
the expected selector and raw-request call-site counts. Any change to this
module therefore requires the override to be reviewed before rebuilding. Source
text is normalized to LF before all three operations so LF and CRLF checkouts
produce the same result. A focused mutation test verifies that dropping an SDK
request option is rejected even when the selector counts remain unchanged.

## Localized tool contract

Descriptions are translated to English, so they are deliberately excluded
from the structural digest. Each tool digest is calculated over canonical
UTF-8 JSON shaped as:

```json
{
  "name": "<registered tool name>",
  "parameters": {}
}
```

Before hashing, every string-valued JSON Schema `description` annotation is
removed. A tool input property whose name is literally `description` remains
covered, including its schema. All other structure remains covered, including
property names, actions, types, required fields, enums, defaults, bounds, array
shapes, and combinators. Object keys are sorted and JSON separators contain no
extra whitespace.

The test still requires every local tool description and every description
annotation that exists to be non-empty. This split allows English copy
improvements without weakening the machine-facing API contract.

## Localized skill contract

The fixture's `skill_files_upstream_sha256` values and
`skill_upstream_references` are immutable provenance for the original upstream
files; they are not expected to match the translated files. The active gate
instead verifies:

- the exact skill and reference-file inventory;
- each skill's frontmatter identity and non-empty description;
- the reviewed runtime command/tool identifier set for every skill;
- that each concrete runtime reference resolves to a registered tool or
  compatibility command, and each wildcard prefix matches a registered tool;
- registration of all nine skill names by the package entry point.

This prevents accidental omissions or identifier drift while allowing clear
English instructions and examples. Known stale upstream identifiers are
normalized to the names actually registered by this plugin. In particular, the
pinned Bitable attachment example references `feishu_drive_media`, but that
tool is not registered by the pinned upstream runtime; the localized skill
states the limitation instead of instructing the agent to call a nonexistent
tool.

## Runtime behavior matrix

| Surface | Implemented behavior | Remaining live boundary |
| --- | --- | --- |
| Transport | Feishu/Lark WebSocket connection and gateway restart/reconnect | Duplicate server event delivery and long-running connection soak |
| Admission | DM open/pairing/allowlist/disabled policies; group open/allowlist/blacklist/admin/disabled policies; group-root mention and bot-loop gates | Credentialed policy and bot-loop matrix |
| Commands | The unified `/feishu` command and three upstream underscore commands use Hermes' ordinary pre-handler channel authorization; Hermes' internal hyphen keys remain accepted, and identity-dependent handlers fail closed without an authoritative Feishu ticket | Hermes' optional host-wide slash-command administrator ACL may add restrictions because 0.19 has no plugin-level `requireAuth` flag |
| Sessions | Every admitted top-level DM or group message creates a thread root and isolated Hermes session; active thread follow-ups reuse it without a mention; existing human-only threads import earlier context on first mention | Migration of existing OpenClaw session history |
| Delivery | Conversational replies stay in their native thread and fail closed without a top-level fallback; static replies preserve lossless chunking; live image/file transfers preserve resource bytes | Live bidirectional audio/video resource integrity |
| CardKit | Thinking, Generating, tool running/success/failure, banner-free successful completion, Error, cumulative content streaming, and Hermes approval actions | Physical client clicks, footer parity, and reasoning-delta presentation |
| Tools | 37 one-shot upstream handlers plus live batch OAuth and interactive questions, with visibility and invocation policy | Tenant execution coverage for all 39 tools |
| OAuth | Device Flow, owner/sender checks, app-permission continuation, encrypted token storage, refresh, retry, and revoke | Credentialed scope and refresh matrix |
| Accounts | Inbound, reply, standalone, tool, OAuth, session, and deduplication state are account-scoped and fail closed on unknown namespaces | Credentialed multiple-account acceptance |

`chunkMode` preserves the pinned schema values and its runtime quirk:
`newline` uses safe blank-line paragraph splitting, while `paragraph` and
`none` both use the hard length fallback. All paths continue to enforce
`textChunkLimit`.

## Fixed Hermes thread model

The Hermes adapter intentionally uses one non-configurable conversation model:

- every admitted top-level DM and group message is a new root thread and
  session;
- a group root requires a bot mention, while a DM root does not;
- follow-ups in an active thread require no mention and retain that session's
  context; and
- every reply is sent inside the originating thread.

`threadSession`, `replyInThread`, and `FEISHU_REQUIRE_MENTION` may be accepted as
migration input, but they do not alter this model.

## Known gaps and compatibility-only settings

The project is not yet a complete drop-in replacement for every upstream path.
Notable gaps include live audio/video transfer coverage, every-tool tenant
execution, live tenant coverage for physical card clicks, CardKit
footer/reasoning-delta presentation, and per-group reply-time skill allowlists.

`dmHistoryLimit`, `configWrites`, `blockStreaming`,
`blockStreamingCoalesce`, and `toolUseDisplay.showFullPaths` are accepted where
needed for migration compatibility but do not currently provide equivalent
plugin-only behavior. WebSocket is the supported and tested transport; legacy
webhook-compatible code is outside the public acceptance contract.

## Updating the baseline

Do not refresh expectations from the local translated inventory. To adopt a new
upstream revision:

1. review the upstream source and behavior delta;
2. update the pinned commit in source, fixture, scripts, and documentation;
3. derive structural tool hashes and skill references from that upstream
   checkout;
4. rebuild the Node bridge with `scripts/rebuild_bridge.sh`;
5. regenerate the bundle's third-party inventory and license texts; and
6. run the complete offline and live acceptance suites.

The rebuild script refuses any source checkout whose `HEAD` does not match the
reviewed commit. Do not edit `openclaw_tools_bridge.mjs` by hand.
