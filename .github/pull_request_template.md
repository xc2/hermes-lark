## Summary

Describe the user-visible problem and the exact behavior changed.

## Validation

- [ ] `python -m unittest discover -s tests -t . -v`
- [ ] `python -m ruff check .`
- [ ] `python scripts/check_english.py`
- [ ] `python scripts/check_project.py`
- [ ] Package build/checks, when packaging changed
- [ ] Live-tenant E2E, when required (state why it was not run if omitted)

## Risk review

- [ ] No credentials, message content, document content, or private logs are included
- [ ] Authorization and account/thread isolation are unchanged or explicitly explained
- [ ] Permission-scope changes are documented
- [ ] Upstream parity impact is documented
- [ ] Generated bridge and third-party notices were refreshed when applicable
