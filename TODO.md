# TODO

## Configurable Feishu API identity

Allow Hermes configuration to choose whether most eligible Feishu tool actions
run with tenant identity or user identity. The choice applies to an API action,
not directly to a permission scope, because each Feishu API defines which token
types it supports.

### Goals

- Preserve a safe default identity for every tool action, including the current
  user-identity contract of `feishu_doc_comments`.
- Support account-level configuration and narrower per-tool or per-action
  overrides, with a documented precedence order.
- Reject an identity that the target API does not support and return a clear
  configuration error.
- Keep operations that inherently require a specific identity non-configurable.
- Route user identity through the existing UAT authorization and recovery flow,
  and tenant identity through the application TAT flow.
- Centralize identity selection in the invocation policy instead of maintaining
  source-level overrides for individual tools.
- Document the configuration schema and cover defaults, overrides, account
  isolation, missing UAT, and unsupported identity choices with tests.

### Open decisions

- Define which tool actions are eligible for identity selection.
- Decide whether a global default is useful in addition to account, tool, and
  action overrides.
- Choose the final Hermes configuration keys after checking how they compose
  with the existing OpenClaw-compatible configuration.
