# Generated OpenClaw tool bridge

`openclaw_tools_bridge.mjs` is a generated, self-contained ESM artifact used to
run the request/response Feishu tools from the pinned `openclaw-lark` source.
It is checked in so a normal Python package installation does not require a
TypeScript build toolchain.

Do not edit the generated file manually.

## Provenance

- Upstream repository: `https://github.com/larksuite/openclaw-lark`
- Upstream commit: `dde0be3680d6fd5443cab426c8f4b3216266346a`
- Build configuration: `tsdown.bridge.config.ts`
- Source entry point: `bridge-entry.ts`
- Expected digest: `openclaw_tools_bridge.mjs.sha256`

The bridge also includes third-party packages used by the upstream tool
implementation. Their attribution is recorded in the repository-level
`THIRD_PARTY_NOTICES.md` file.

`auto-auth-shim.ts` hands interactive authorization state back to the live
Python adapter. `uat-client-shim.ts` retains upstream token behavior while
adding the cross-process refresh lock required by one-shot workers.

## Rebuild

From the repository root:

```bash
scripts/rebuild_bridge.sh /path/to/openclaw-lark
```

The script refuses to build from a different upstream commit, installs the
pinned upstream lockfile, regenerates the bundle, and refreshes its SHA-256
file. Review both the JavaScript diff and third-party notices before accepting
a regenerated artifact.
