# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project intends to follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- Require Hermes Agent 0.22.5 or newer, declared through `requires_hermes`
  in `plugin.yaml`; Hermes Agent 0.19 is no longer supported.
- Declare Python dependencies as bounded ranges that resolve with Hermes
  Agent's own pins, drop the upper Python bound that broke Hermes' plugin
  dependency lock on Python 3.14, and move development tools from the `dev`
  extra to `requirements-dev.txt`.

### Fixed

- Load on Hermes Agent 0.22.5: use the relocated scoped-lock release, the
  renamed SSRF redirect guard, and Hermes' package manager instead of the
  retired lazy-dependency installer.
- Report lark-oapi availability without installing it from status and
  configuration checks; install it only when the gateway starts Feishu.
- Enable Feishu from `FEISHU_APP_ID` and `FEISHU_APP_SECRET` in the
  environment again, including the home channel and its thread.
- Answer ordinary Feishu messages under Hermes Agent 0.22's durable message
  admission: keep Feishu-private per-message data off `MessageEvent.metadata`,
  which Hermes refused, and re-check group and open-DM grants against the
  current policy through `reauthorize_native_roles`.
- Restore what Hermes Agent 0.22 now reads from the platform entry: native
  approval cards instead of the text `/approve` prompt, the Feishu system-prompt
  hint, the medium display tier, and explicit `oc_`/`ou_` send targets.
- Revalidate stored Feishu user authorization against the remote grant before
  completing batch authorization, so revoked credentials re-enter Device Flow.
- Distinguish standalone `/feishu auth` from tool-triggered OAuth, resume the
  previous Hermes session only for tool continuations, and consistently clean
  up terminal authorization state.
- Run every `feishu_doc_comments` action with the requesting user's access
  token, including both reply payload variants, as promised by its tool
  description.
- Hydrate a new Hermes session from every preceding message and downloadable
  resource in an existing Feishu thread, and fail visibly when the snapshot is
  incomplete.
- Preserve physical thread ordering after an accepted Steer or a blocking
  interaction by freezing the prior CardKit segment and continuing below the
  timeline boundary.
- Put the answer before lifecycle and collapsed tool details on completed
  CardKit cards, and use the answer for the Feishu chat-list preview.
- Cover imported thread media, repeated Steer boundaries, tool completion,
  questions, approvals, media replies, and cancellation in live-tenant E2E.

## [1.0.0] - 2026-08-04

### Added

- Initial standalone Hermes Agent Feishu/Lark platform plugin.
- Fixed thread/session routing, CardKit streaming, OAuth, tools, skills, and
  Docker-based live-tenant validation.

[Unreleased]: https://github.com/xc2/hermes-lark/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/xc2/hermes-lark/releases/tag/v1.0.0
