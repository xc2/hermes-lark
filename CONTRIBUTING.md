# Contributing to hermes-lark

Thank you for helping improve `hermes-lark`. This project adapts a security-
sensitive messaging integration, so changes should be small, reviewable, and
backed by the narrowest useful test.

By participating, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Before opening an issue

- Search existing issues and the [parity contract](docs/PARITY.md).
- Do not include application secrets, access tokens, refresh tokens, OAuth
  codes, session databases, message bodies, document content, or unredacted
  gateway logs.
- Use a dedicated test tenant when reproducing authorization or policy bugs.
- Report security vulnerabilities privately as described in
  [`SECURITY.md`](SECURITY.md).

## Development environment

Supported versions are Python 3.11 through 3.13 and Node.js 22 or newer.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
git clone --filter=blob:none https://github.com/NousResearch/hermes-agent.git ../hermes-agent
git -C ../hermes-agent checkout --detach cc4cab2f592e60a197e796506de9168f74baf3ea
python -m pip install -e ../hermes-agent
python -m pip install -e '.[dev]'
```

The normal test suite is offline and does not require Feishu credentials:

```bash
python -m unittest discover -s tests -t . -v
python -m ruff check .
python scripts/check_english.py
python scripts/check_project.py
```

Build and inspect the distributable artifacts before submitting packaging
changes:

```bash
python -m build
python -m twine check dist/*
python scripts/check_dist.py dist
```

Credentialed E2E is intentionally separate. Follow
[`tests/e2e/README.md`](tests/e2e/README.md) and never run it with a production
application.

## Change guidelines

- Follow YAGNI. Do not introduce an abstraction until it organizes substantial
  logic or has more than one real consumer.
- Preserve the fixed thread/session contract unless the change explicitly
  updates the project specification.
- Keep top-level JavaScript and TypeScript members documented with concise
  block comments. Interface and object-type members should also have concise
  block comments.
- Keep repository-authored prose, comments, source strings, tests, and skills in
  English. The generated upstream bridge and explicitly isolated multilingual
  input-recognition data are the only language-policy exceptions.
- Add or update tests for behavior changes.
- Do not silently weaken authorization, tenant isolation, callback identity
  checks, or fail-closed delivery.
- Do not amend an existing commit unless a maintainer explicitly asks you to.

## Upstream parity changes

The pinned upstream revision is recorded in [`docs/PARITY.md`](docs/PARITY.md).
When adopting another revision:

1. Review the upstream diff, not only generated schema output.
2. Update tool names and structural schemas deliberately.
3. Translate new agent-facing prose into English without changing parameter or
   operational semantics.
4. Update the parity fixture and provenance records.
5. Rebuild the Node bridge through `scripts/rebuild_bridge.sh`.
6. Refresh third-party notices if the bundle dependency graph changed.
7. Run the offline suite and the complete live-tenant E2E matrix.

Do not update local expectations from local generated files alone. The pinned
upstream checkout remains the independent source of truth.

## Generated Node bridge

`hermes_lark/node/openclaw_tools_bridge.mjs` is a generated, checked-in runtime
artifact. Do not edit it manually. Rebuild it only from the reviewed upstream
commit:

```bash
scripts/rebuild_bridge.sh /path/to/openclaw-lark
```

The script verifies the upstream commit and refreshes the adjacent checksum.
Review the generated diff and third-party notices before merging it.

## Pull requests

Keep pull requests focused. Include:

- the user-visible problem;
- the exact behavior changed;
- offline tests run;
- whether live-tenant E2E was run;
- permission, OAuth, data-retention, and compatibility impact;
- any intentional difference from the pinned upstream plugin.

Maintainers may ask for a smaller change when unrelated cleanup makes a
security-sensitive diff harder to review.
