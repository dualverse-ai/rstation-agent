# Configuration and troubleshooting

The launcher reads `.env` automatically. Exported shell settings take precedence, including blank values. Use the exact API base URL supplied by each provider; Claude, Codex and Gemini endpoints need not share the same path suffix. Primary settings are required for each researcher family you select. Backup keys are optional; omit a backup URL to reuse the primary endpoint.

The default roster is one researcher per family. Set a family’s count to zero to omit it. `--url` selects another rStation server. `--codex-model`, `--claude-model` and `--gemini-model` select models your provider supports.

Coder uses Codex CLI and the Codex API settings even when the main researcher is Claude or Gemini. Its default model is `gpt-6.2-sol`; `--coder-model` overrides it. Use `--no-coder` when you do not want it. Coder works directly in each researcher’s workspace, with automatic backup rotation.

Council uses the available configured adviser families. It requires a family other than the researcher’s own; use `--no-council` for a single-family setup. It gives scientific advice and reads research files, while coder implements changes. `--supervisor` explicitly adds research supervision; it is off by default.

## Inspect and control a run

```bash
./agents status RUN_NAME
./agents logs RUN_NAME gemini_1 --lines 40
./agents attach RUN_NAME
./agents send RUN_NAME all "Continue with the strongest comparison."
./agents pause RUN_NAME claude_1
./agents resume RUN_NAME --worker claude_1
```

Data lives in `agent_data/RUN_NAME/`. Set `RSTATION_AGENT_DATA` before launching to use another data directory. Workspaces retain research files, registered accounts and native conversation history. `./agents stop RUN_NAME` stops the group and its experiments. `./agents resume RUN_NAME` resumes the saved work. After a controller interruption, `resume RUN_NAME --keep-experiments` preserves independent running experiments.

Startup checks the selected providers with a brief model greeting. `--check` runs that existing check without starting research; greetings consume provider credits. For API failures, check the model name, key and endpoint. Runtime retries use the configured backup and preserve saved conversations. Install the selected native CLIs on PATH, or set `CODEX_BIN_PATH`, `CLAUDE_BIN_PATH` or `ANTIGRAVITY_BIN_PATH`.

## Invite-only problems

Public problems need no invitation. For an invite-only problem, obtain one invitation per researcher and save a local JSON file mapping worker names to codes. Start with `--invitation-codes-file /path/to/invitations.json`. An optional supervisor needs its own invitation through `--supervisor-invitations-file`. Existing approved accounts retain access.

## Gemini streaming compatibility

For a provider sending SSE comment heartbeats that the native Gemini CLI rejects, set `gemini_sse_compat` to `true` in the affected run’s `run.json`. The next live wrapper launch normalizes those stream comments and line endings, preserving the selected model, upstream HTTPS verification and native conversation. This setting is off by default.

## Manage one researcher

Read the worker’s current settings, then submit a change using that revision:

```bash
./agents worker RUN_NAME gemini_1 status
./agents worker RUN_NAME gemini_1 update --expect-revision 0 --model gemini-4-carbon --provider-route 2
./agents worker RUN_NAME gemini_1 status
```

Routes are numbered from one in the existing controller profile. Use `--provider-route auto` to restore automatic rotation. To restart the saved conversation without changing its model, use `./agents worker RUN_NAME gemini_1 restart --expect-revision 1`. Use the revision returned by `status`; the examples assume no earlier changes. The operator shell must load the same workload profile as the controller. Credentials remain in that private profile.

The controller waits for this researcher to finish its current turn and for its tools and GPU jobs to finish. Other researchers continue working. The session, workspace, operator messages and context budget are preserved. An explicitly paused researcher stays paused; use the existing worker resume command when ready. A stopped controller must first be resumed.

Status reports the pending request and its applied or rejected receipt. Repeating the same request at the same expected revision returns that receipt. Supply a new `--request-id UUID` to explicitly retry a rejected request after resolving its cause. A changed session, configuration or provider list rejects the pending change. Missing sessions and model refusals remain blocked for operator resolution. After a change, provider outages retry the saved session; an unexplained or permanent failure holds it for inspection.

Worker overrides are saved with that worker’s runtime state. Editing the roster model in `run.json` does not replace an applied worker override; manage that researcher through this command. Adding credentials or endpoints to a running controller is a separate profile rollout.
