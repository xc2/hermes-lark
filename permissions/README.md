# Permission manifests

This directory contains importable Feishu/Lark permission manifests for the
production baseline, bundled skills, and isolated E2E application.

## `production.json`

Use this baseline manifest as the starting point for a normal application. It
contains the always-on channel, CardKit, command, and authorization-bootstrap
scopes used by the production runtime. Remove scopes for features that your
deployment will not enable.

The manifest intentionally does not pre-enable every user scope used by all 39
tools. When a tool needs a scope that the application does not yet expose, the
plugin gives the application owner a card containing the exact missing scopes.
The owner must request those scopes, publish and approve a new application
version, and then let the requesting user complete OAuth. This keeps a normal
application at least privilege instead of granting every document, calendar,
task, Base, Drive, and IM operation up front.

`/feishu auth` and `feishu_oauth_batch_auth` authorize user scopes that are
already enabled for the application; they do not add new application
permissions. The checked-in `offline_access` user scope allows the normal
refresh-token flow once a user grants an operational scope.

## `skills.json`

Import this optional supplement after `production.json` when every operation
referenced by the bundled skills should be available without adding
application scopes on demand. It contains only operational user scopes that
are absent from the production user-scope baseline; `offline_access` remains
in `production.json`.

Permissions are identity-specific. A scope string that already appears under
`production.json`'s `tenant` array may therefore also appear under this file's
`user` array without duplicating the same grant.

The supplement follows the complete tool families referenced by the skills.
Remove scopes for skill actions that your deployment will not use.

## `e2e.json`

Use this manifest only for a dedicated test application. It is a strict
superset of the production manifest and adds authority used by the E2E driver
to create/delete temporary chats and to send/recall messages as the test user.

Do not import the E2E manifest into a production application without an
explicit permission review.

## Operational rules

- Publish a new application version after changing permissions.
- Keep the application availability scope limited to intended users.
- Never place an application secret, user access token, or refresh token in a
  manifest.
- Treat user-identity scopes as authority granted to the agent under the
  authorizing user's identity.
- Re-run `/feishu doctor` after changing the permission set.

The manifests are checked by the offline test suite to ensure the E2E manifest
remains a documented superset rather than silently changing the production
trust boundary.
