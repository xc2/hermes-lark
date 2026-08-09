# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project intends to follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- Revalidate stored Feishu user authorization against the remote grant before
  completing batch authorization, so revoked credentials re-enter Device Flow.
- Distinguish standalone `/feishu auth` from tool-triggered OAuth, resume the
  previous Hermes session only for tool continuations, and consistently clean
  up terminal authorization state.
- Run every `feishu_doc_comments` action with the requesting user's access
  token, including both reply payload variants, as promised by its tool
  description.

## [1.0.0] - 2026-08-04

### Added

- Initial standalone Hermes Agent Feishu/Lark platform plugin.
- Fixed thread/session routing, CardKit streaming, OAuth, tools, skills, and
  Docker-based live-tenant validation.

[Unreleased]: https://github.com/xc2/hermes-lark/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/xc2/hermes-lark/releases/tag/v1.0.0
