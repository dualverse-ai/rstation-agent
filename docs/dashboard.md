# Agent pool dashboard

A private, read-only view of researcher activity. It listens only on 127.0.0.1:8911 and has no worker-control endpoints. It uses existing SSH access to H4, H6 and H7 and unauthenticated public rStation profile APIs. No H1 access or provider request is needed.

Run from the launcher directory:

```
python3 fleet_dashboard.py --config /PRIVATE/PATH/fleet.json --data /PRIVATE/PATH/fleet-history
```

The private config contains `hosts` (host alias to a list of existing toolkit directories) and optional `accounts` (host/run/worker to a public handle and verified stop reason). A stopped slot is intentional only with explicit owner evidence, never from a PAUSE/STOP marker alone. Do not put keys in this config. The toolkit reads account files locally on each host only to extract a public handle; it never exports their other fields. Errors are allowlisted categories and HTTP status numbers, never raw logs.

Host collection runs every two minutes; public account data refreshes separately, at most hourly per account. The page refreshes every 30 seconds. Missing or older-than-five-minute host evidence is Unknown. A process being alive does not establish health. Seven-day history and 24-hour observed totals start with this collector; gaps remain gaps. The SQLite history retains eight days. Keep the data directory private and outside Git.

Field sources:

- Run/model/route/state: run.json and runtime state; per-worker effective model overrides are included.
- Real response/completed tool: native activity timestamps recorded by the launcher.
- GPU: live owned GPU helper/job receipts. These do not establish that a job is foreground, so job presence alone never upgrades health to green; disk: most recent recorded workspace size and allowance.
- Owner messages: queue status and recorded owner receipt time; message bodies are excluded.
- Handle: known account-file fields; registration and published-paper counts: public profile APIs.
- Last public activity: public posts/comments/papers. This is NOT the complete interaction time. Full platform interaction and local platform receipt remain Unknown where no trustworthy structured source is available.
- Intentional stop: explicit owner-message mapping. Unverified stopped slots remain visible. Retired slots are not inferred merely from a successor name.

The first version enumerates run.json in configured toolkit roots. Removed/archived rosters without a remaining run.json cannot be reconstructed from this view. Hosts outside the configured installations, complete interaction history, reflection due state and native session/result-file details need additional collection. Profile failures leave unknown or visibly aged account evidence. No page contains keys, transcripts or operator-message text.

Tests: `python -m pytest tests/test_fleet_dashboard.py`. Includes health from work rather than process presence, stale evidence, history gaps, concurrent SQLite reads/writes, secret exclusion, account parsing, independent profile fetch and read-only loopback HTTP access.

The page uses the live GPU Pool dashboard (http://localhost:8910/) as its visual reference: the same typography, light/dark colours, compact header and bordered cards. The default table shows health, problem, worker, model, last response and a plain-language reason. Select a worker for the other measurements and history. Retired slots are hidden initially; the checkbox reveals them. Provider observations are expandable. The top status line includes unknown and retired counts as well as working, slow and attention states. Theme selection is saved only in the browser.

Provider summary tiles show Gemini, Claude and Codex separately. Responding means at least one non-retired worker produced a real model response within ten minutes, using host evidence no older than five minutes. A completed tool or live process alone does not establish provider response. A family can be responding while some of its workers need attention; both counts are shown. Missing or stale evidence is Unknown, not a provider failure. The worker table puts workers needing attention first. The favicon and header share a navy, teal and gold grid mark.
