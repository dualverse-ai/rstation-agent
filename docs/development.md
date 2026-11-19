# Development

```bash
./setup.sh
.venv/bin/python -m pytest tests -q
```

Tests use disposable workspaces and mock providers. Native integration tests are opt-in through `RSTATION_TEST_CODEX_BIN`, `RSTATION_TEST_CLAUDE_BIN` and the relevant Gemini test binary setting documented in each test. They require installed CLIs; ordinary unit tests do not need API credentials.

The launcher communicates with rStation over HTTP and does not import its server application. `vendor/station/` contains the bundled CLI lifecycle and GPU coordination code; [its provenance](../vendor/README.md) remains with the source. Credentials, native CLI homes, generated research and logs stay out of Git.
