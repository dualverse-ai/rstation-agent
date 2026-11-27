# Owner message history

Send an original owner instruction or a verified relay through the operator CLI:

```bash
./agents send RUN_NAME WORKER --file /private/owner-message.txt --owner --owner-reference desk:6:13328
```

Use the reference for the actual owner instruction. The explicit mark records operator attestation; it does not independently authenticate a Desk message. Ordinary `send` messages, coder/council output, supervisor messages and automatic timer notices do not enter the history. Source display labels and queue priority do not establish owner provenance.

The launcher records the full marked text in `user_instruction.txt` in the worker workspace after native receipt, with the message ID and UTC/HKT receipt times. A managed pointer in AGENTS.md, CLAUDE.md and GEMINI.md covers researchers and supervisors. Older entries can be outdated; no history is injected into the startup prompt. Normal recovery of an unfinished message keeps its existing delivery behavior and does not duplicate the history entry.

The durable record is `.runtime/owner_history.json`. History rendering is atomic and serialized with the existing message queue lock. Repeated receipt, backfill and synchronization are idempotent. Conflicting manual edits are retained as private `.runtime/user_instruction.conflict-<digest>.txt` files before repair. The launcher owns maintenance; workers should keep notes elsewhere. These files share the worker account and are not a security boundary against that account.

## Existing workers and backfill

Install a pointer and synchronize an existing worker without restarting it:

```bash
python owner_history.py sync /absolute/worker/workspace --install-pointer
python owner_history.py sync /absolute/worker/workspace --backfill /private/verified-messages.json
```

The backfill file is a reviewed JSON list. Each entry names `message_id`, `text_sha256` (UTF-8 of the recorded text), and `reference` to a verified owner instruction. Only messages with a recorded native receipt are eligible. All entries are checked before queue metadata is written. Unknown authorship is left out. Backfilled entries carry their original recorded receipt time and a historical label; the command never queues or re-delivers them. Old wrappers lacking a first-receipt field retain the earliest receipt observed by this feature; an earlier unrecorded receipt cannot be reconstructed.

For wrappers already loaded before installation, run a separate receipt monitor under the established host operator account:

```bash
python owner_history.py watch /private/workspaces.json --interval 5 --status /private/history-status.json
```

The manifest is a JSON list of existing absolute workspace paths, selected by the operator. The monitor reads the existing native receipt records every five seconds under the same lock. It does not signal workers, launch clients, change sessions, models or routes, or touch GPU reservations. Run it as a supervised host service with restart-on-failure; no worker restart is needed. Install pointers and apply reviewed backfill once before starting it. New workspaces must be enrolled in the manifest or use the new receipt hook.

`history-status.json` reports the monitor's loaded source hash, check time and per-worker entry counts/errors. Each worker also has `.runtime/owner_history_status.json`. Errors use fixed summaries without message bodies. Monitor freshness as well as errors. An old wrapper plus a fresh receipt monitor is operational coverage, not proof the wrapper loaded new code. Report installed hashes, monitor-loaded hashes and native wrapper adoption separately; native wrappers gain the inline receipt hook on their next ordinary launch. The monitor can remain active alongside that hook.

A history-write failure preserves the native receipt and is repaired by the monitor or next queue update. Retain the monitor until all relevant wrappers have adopted the hook. Roll back a failing monitor or hook without deleting the durable history or changing message delivery state.
