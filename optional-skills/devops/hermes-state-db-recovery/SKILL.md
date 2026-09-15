---
name: hermes-state-db-recovery
description: Recover a corrupt Hermes state.db without re-corrupting it.
version: 1.0.0
author: Tyler Gray (Peetbog69), Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [sqlite, state-db, corruption, recovery, gateway, cron, watchdog, devops]
    category: devops
    requires_toolsets: [terminal]
    related_skills: []
---

# Hermes state.db Recovery

Diagnose and recover a Hermes profile whose `state.db` (SQLite, WAL mode) has failed. Covers four failure modes that look alike and are treated differently, the stop → purge → swap → verify sequence, the fresh-rebuild path for profiles with no clean snapshot, and a silent watchdog that captures forensics when the trigger is unknown. It does not cover recovering a profile's *transcripts* — a bad DB means those are gone; it recovers the profile.

## When to Use

- `database disk image is malformed (11)` from an integrity check, or session DB creation failing
- Cron jobs stopping with "the turn was stopped because session storage could not be written"
- `sqlite3.OperationalError: disk I/O error` on writes, at `BEGIN IMMEDIATE`
- A gateway that is "active" but has stopped processing messages
- Profiles that keep re-corrupting after clean restores
- Setting up monitoring for the above

That storage guardrail message is the system working as designed — never bypass it. Fix the DB.

## Prerequisites

- `sqlite3` on `PATH` (the check and the CLI both use it)
- Permission to stop and start the profile's gateway — as the profile's owner, or via `sudo env HERMES_HOME=<profile>`
- A disk with room for a copy of the corrupt DB before you delete anything
- `scripts/corruption-watch.sh` from this skill, for the watchdog section

## How to Run

The recurring actions are shell commands; run them through `terminal`. The diagnostic sequence a profile follows is:

1. **Classify** the failure mode (table below) — three of the four are *not* plain corruption, and running the restore on them makes things worse.
2. **Diagnose** with `PRAGMA integrity_check` and the profile's `errors.log`.
3. **Back up** the corrupt `state.db` and its `-wal`/`-shm` before deleting anything.
4. **Stop the gateway**, purge all three DB files, lay in a clean source, start, verify.

Ready-made watchdog: run `scripts/corruption-watch.sh --profile-dir <profile-dir>` from the skill directory (it prints its own `--help`), and wire it into cron at a `*/2` cadence.

## Quick Reference

| Symptom | Mode | Action |
| --- | --- | --- |
| `malformed (11)` from `PRAGMA integrity_check` | 1. Corruption | Stop → purge → swap → start |
| `disk I/O error` on writes, but `integrity_check` = `ok` | 2. Dead file descriptors | Restart the gateway; do **not** restore |
| `integrity_check` = `ok`, gateway silently hung | 3. Dormant corruption | Stop → swap → start is the test |
| Re-corrupts after a clean restore | 4. Upstream race (#94736) | Staged `hermes update` |

| Command | Purpose |
| --- | --- |
| `sqlite3 <profile>/state.db 'PRAGMA integrity_check;'` | Health check; `ok` is the only pass |
| `sqlite3 <profile>/state.db 'PRAGMA foreign_key_check;'` | Second signal when main is corrupt |
| `rm -f state.db state.db-wal state.db-shm` | Purge — all three, always together |
| `sudo env HERMES_HOME=<profile> hermes --profile <name> gateway stop` | Guard-safe stop |
| `systemctl is-active hermes-gateway-<name>.service` | Is the unit up (not: is it healthy) |

## Procedure

### 1. Classify the failure mode

Read the table above and pick one before touching anything. The three traps:

**Mode 2 — dead file descriptors.** `disk I/O error` on every write while `integrity_check` returns `ok` is *not* corruption. The running gateway held open descriptors to `state.db-wal`/`-shm` and a swap removed those files from under it, so SQLite writes through dead inodes. Do not run the corrupt-restore procedure on it; that makes things worse.

- Diagnose: list the gateway's open descriptors under `/proc/<gateway-pid>/fd/` with `terminal` and look for `state.db-wal (deleted)` / `state.db-shm (deleted)`.
- Trap: a CLI write to the same DB succeeds (fresh descriptors) while the gateway fails. Don't conclude the DB is fine, or that it is re-corrupting.
- Fix: stop the gateway, confirm no gateway process remains, start it, confirm the live descriptors point at non-deleted files.
- Do not over-alarm on a bare deleted-descriptor listing — it also appears on healthy gateways as a routine WAL lifecycle artifact. It is diagnostic only when paired with active write failures in `errors.log`.

**Mode 3 — dormant corruption.** A DB can pass `integrity_check` while the gateway is quietly hung, then go `malformed` the instant it restarts. So `integrity_check = ok` is not proof of health.

- Check gateway activity recency in `<profile>/logs/gateway.log` with `read_file` — is it processing, or is a process merely alive?
- Check the DB's mtime and `-wal` growth. `systemctl is-active` proves the unit is up, not that it is healthy.
- Stop → swap → start is the reliable test: if it goes malformed on restart, it was dormant-corrupt.

**Mode 4 — recurrence.** If profiles keep re-corrupting after clean restores, this is upstream issue **#94736**, a teardown/worker race, not a bad restore. See step 6.

### 2. Diagnose

1. `sqlite3 <profile>/state.db 'PRAGMA integrity_check;'` — corruption returns `Error: stepping, database disk image is malformed (11)` plus `btreeInitPage() returns error code 11` for the bad pages.
2. `PRAGMA foreign_key_check;` fails the same way when main is corrupt.
3. Search `<profile>/logs/errors.log` for `malformed` with `search_files` to find *when* it first went bad — this separates "corrupted by an event" from "corrupted during normal running".
4. Disk is usually a red herring; a corrupt DB exists with plenty of free space.
5. Rule out the host before blaming the app: check `journalctl` around the first error timestamp and `dmesg` for storage errors and for a reboot in the window. A clean host with an app-level `disk I/O error` means the application, not the disk.

### 3. Restore from a snapshot (the common path)

The clean source is the profile's pre-update snapshot at `<profile>/state-snapshots/*-pre-update/state.db`. Verify it reports `ok` **and** that no `-wal`/`-shm` shipped with it — a snapshot carrying a stale WAL is not clean.

**The critical lesson:** copying a clean `state.db` over a corrupt one while leaving the corrupt-era `-wal`/`-shm` behind does NOT fix it. On next open, SQLite replays the stale WAL into the fresh file and re-corrupts it within seconds — a restore that silently never took. Symptom: `ok` right after the swap, malformed again after restart. Always remove all three together; deleting only the WAL is not enough either, because the corruption gets checkpointed into the main file.

The sequence, in order — stop, purge, swap, verify, start:

1. Copy the corrupt `state.db` and any `-wal`/`-shm` to a timestamped backup directory. Never delete a corrupt DB without a backup.
2. Stop the gateway **first**, so it is not holding the DB open. Inside a running gateway session `systemctl stop` is blocked by a local guard (it would signal your own process) — use the sanctioned form `sudo env HERMES_HOME=<profile> hermes --profile <name> gateway stop`, then confirm the unit is inactive.
3. Remove `state.db`, `state.db-wal`, and `state.db-shm`, then copy the verified snapshot in as `state.db`. Correct ownership and mode (`chmod 644`). Confirm `integrity_check` reports `ok` and that no `-wal`/`-shm` are present.
4. Start the gateway, wait for warmup, and re-verify `integrity_check` still reports `ok`. Then confirm no fresh `malformed` lines appear in `errors.log` after the restart timestamp.

Run the CLI as the profile's owner, or via `sudo env HERMES_HOME=<profile>`. A bare `hermes --profile <name>` as root fails with `Profile '<name>' does not exist`, because root's default home does not see other users' profiles. Full command recipe: `references/restore-recipe.md`.

### 4. Fresh rebuild when no clean snapshot exists

A profile that corrupted within hours of creation, or that never had a snapshot taken, has no clean source. Two traps:

- **A backup taken "before restoring" is not a usable restore source.** Copying a running or corrupt profile copies the corrupt DB *and* its dirty WAL. Verify any candidate with an integrity check and confirm no `-wal`/`-shm` shipped with it, or you get false confidence and still need a rebuild.
- Salvage with `sqlite3 <corrupt.db> '.recover'` may produce near-empty output when corruption is deep. Writes had been failing since the first corruption, so little was persisting anyway. Don't over-invest in salvage.

Rebuild: stop the gateway the same guard-safe way, remove all three DB files, then start — Hermes re-inits a clean `state.db` on boot. Verify as in step 3.

**Rebuild vs restore:** for a young profile whose transcript history is low-value, rebuilding costs little. `SOUL.md`, `config.yaml`, `.env`, and gateway state are separate files, untouched by a DB rebuild, so the persona and bot pairing survive; only transcript history is lost. For an older profile with irreplaceable history, exhaust the snapshot path first.

### 5. Catch an unknown root cause: silent watchdog

When profiles are re-corrupting and the trigger is not yet identified, don't keep swapping and guessing. Run `scripts/corruption-watch.sh` on a `*/2` cron cadence: it is silent while healthy and, the instant an integrity check fails, snapshots the corrupt DB plus the process list, the gateway's open descriptors, and recent `errors.log` lines into a capture directory. On the next fire you have evidence — which process held the DB, what wrote, the exact error — instead of a re-corruption someone noticed too late.

- `*/2` is inside the typical 30–60 minute recurrence window, so a fire is not missed.
- It is read-only with respect to the database: it never opens `state.db` for writing, so it cannot itself trigger the corruption it watches for.
- It emits output only on a hit, so cron stays quiet.
- Retire or narrow it once the root cause is fixed.

### 6. Recurrence means a code bug, not a restore failure

If multiple profiles re-corrupt within ~30–60 minutes of clean restores, stop treating each as isolated. The known cause is upstream **#94736** — one process (cron worker, kanban, teardown) closing the shared connection while another is mid-write:

```text
hermes_state: state.db connection ... was closed while a write was still in flight
  — reopening (teardown/worker race, #94736)
... Session DB append_message failed: FOREIGN KEY constraint failed
```

Corruption is opportunistic — writes colliding — not deterministic, and concentrates where write volume is highest. **A single clean run proves nothing**, and cron jobs are not the culprit: a scheduled job against a clean DB runs green. Do not rip out working jobs chasing that theory.

The real fix is a **staged `hermes update`**. Before burning hours re-swapping: check `hermes --version` against upstream (a build hundreds of commits behind almost certainly lacks the fix), surface the `#94736` evidence to the user, and recommend updating one low-stakes profile, verifying 24h clean, then rolling the rest. **Never fire a fleet-wide update unilaterally** — an update landing mid-write is itself a corruption trigger, so it is both the cure and a known trigger. Worked incident: `references/recurrence-incident.md`.

## Pitfalls

- **Restoring without stopping the gateway first.** This is the entire dead-descriptor bug class, and it can also re-poison a fresh DB. Stop first, always.
- **Purging only the WAL.** The corruption is checkpointed into the main file; replace all three.
- **Trusting a live backup you just took.** It carries the corruption. Verify before treating anything as a restore source.
- **Treating `integrity_check = ok` as proof of health.** A hung gateway reads clean and goes malformed on restart.
- **Diagnosing off a deleted-descriptor listing alone.** Normal WAL lifecycle on healthy gateways; only meaningful alongside active write failures.
- **Fixing only the loudest profile.** Corruption arrives in batches — integrity-check every profile on the host and fix the batch.
- **Reading the wrong profile path.** Same-named profiles can exist under different users (`/root/.hermes/profiles/<name>` vs `/home/<user>/.hermes/profiles/<name>`). Operate on the path the systemd unit uses; check the unit's `WorkingDirectory`/`HERMES_HOME`.
- **Assuming a rollback is free.** The snapshot predates the corruption, so state rolls back to it. Usually acceptable, since writes had been failing anyway — but confirm the snapshot date before committing.
- **Running a fleet-wide update to "just fix it".** It is the known trigger as well as the cure. Stage it.

## Verification

The recovery is complete when all of these hold:

1. `sqlite3 <profile>/state.db 'PRAGMA integrity_check;'` reports `ok` — and still does after warmup, not just immediately after the swap.
2. No `state.db-wal` or `state.db-shm` linger from the purge (the gateway recreates them as it writes).
3. `systemctl is-active hermes-gateway-<name>.service` reports `active`.
4. `errors.log` shows no fresh `malformed` or `disk I/O error` lines after the restart timestamp.
5. The gateway is genuinely processing — recent inbound/response activity in `gateway.log`, not merely a live process.
6. For a rebuild, `gateway_state.json` shows the platform reconnecting (`connecting` → `connected`).

To verify the watchdog itself: run `scripts/corruption-watch.sh --profile-dir <healthy-profile>` and confirm it exits 0 with no output, then point it at a deliberately corrupted copy and confirm it writes a capture directory and exits 1.
