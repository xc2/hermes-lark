# Security policy

## Supported versions

Security fixes are applied to the latest release and the `main` branch. Older
releases are not supported.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use
[GitHub private vulnerability reporting](https://github.com/xc2/hermes-lark/security/advisories/new).
If that form is unavailable, open a content-free issue requesting a private
maintainer contact. Do not disclose the vulnerability in that issue.

Include only the minimum information needed to reproduce the problem:

- affected version or commit;
- deployment mode and operating system;
- security boundary that was crossed;
- sanitized reproduction steps;
- expected and actual behavior;
- suggested mitigation, if known.

Never include a live application secret, access token, refresh token, OAuth
code, session database, message body, document content, or raw production log.
Replace identifiers and credentials with clearly marked placeholders.

You should receive an acknowledgement within seven days. Maintainers will
coordinate validation, a fix, and disclosure timing through the private
advisory.

## Security-sensitive areas

Extra review is expected for changes involving:

- DM and group admission policy;
- thread and account isolation;
- user access token selection, storage, refresh, and revocation;
- card-action operator validation;
- application-owner and permission checks;
- tool visibility and invocation policy;
- local file and remote URL handling;
- webhook or WebSocket event authentication;
- logs, diagnostics, and E2E artifact redaction;
- fail-closed message delivery.

## Deployment guidance

- Every Feishu command uses Hermes' ordinary channel authorization gate before
  its plugin handler runs, matching upstream `requireAuth: true`. The plugin
  does not add a diagnostics-only or DM-only bypass.
- `/feishu auth` additionally requires an authoritative Feishu sender ticket
  and verifies that sender as the application owner before starting Device
  Flow. Other commands do not inherit this owner-only restriction.
- Hermes' optional slash-command administrator ACL is an additional host-wide
  restriction and can be enabled independently.
- Use a dedicated application with the smallest practical permission set.
- Keep application availability scoped to intended users.
- Prefer `dmPolicy: pairing` or an explicit allowlist.
- Do not enable unrestricted DM and group access simultaneously without an
  independent authorization boundary.
- Keep `.env`, `.hermes-secrets/`, `.hermes-validation/`, and gateway state out
  of source control and backups intended for public sharing.
- Rotate credentials immediately if they may have appeared in logs or issues.
