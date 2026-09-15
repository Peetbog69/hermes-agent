# Worked incident: multi-profile recurrence (the systemic case)

Reference for the "Mode 4: recurrence is a systemic bug" section. Proves that clean restores are not durable when the install is behind upstream, and shows what the fingerprint looks like in the logs.

## The picture

Five profiles on one host re-corrupted repeatedly across a single morning despite full stop→purge→swap→start restores, roughly 30–60 minutes apart. Three other profiles on the same host stayed stable across the same window. One headless profile (no `state.db` at all) was never affected.

The host's Hermes build was **398 commits behind upstream**.

## The fingerprint in errors.log

```text
hermes_state: state.db connection for <profile>/state.db was closed while a write
  was still in flight — reopening (teardown/worker race, #94736)
... ERROR cron.scheduler: Job ... failed: RuntimeError: ⚠️ No reply: the turn was
  stopped because session storage could not be written ...
... Session DB append_message failed: FOREIGN KEY constraint failed
```

That `#94736` line paired with `FOREIGN KEY constraint failed` is the fingerprint of the worker-race corruption. Not every corruption shows it — but when it appears, you are looking at the code bug, not a botched restore.

## Why one clean run is not exculpatory

The hypothesis "cron LLM jobs spawn a second hermes process that corrupts the DB" was tested and **disproven**: a top-priority profile's scheduled job fired against a clean DB and ran `ok`, with no corruption. Corruption under #94736 is opportunistic — writes colliding — so a clean run only means no collision happened that time. It does not clear the install.

Practical consequence: **do not rip out working cron jobs chasing a concurrent-writer theory.** The concurrent writer is the race, not the job.

## What each profile's case taught

- **The loud one** re-corrupted three times in one morning; some cron fires survived. Frequency is evidence of high write volume, not of a uniquely broken profile.
- **The quiet one** was textbook dormant corruption: `integrity_check` returned `ok` while the gateway hung silently (DB mtime frozen since the prior day), then went `malformed` the moment it restarted. A full swap fixed it. This is the case that proves a clean read on a hung gateway is unreliable.
- **The one with a deleted-fd grep** was genuinely corrupt and the swap fixed it — but deleted fds also appeared on the *healthy* profiles, so the grep was a red herring here. This is why the deleted-fd signal is only diagnostic when paired with active write failures.
- **The two that re-corrupted ~2 hours after a fix** were the decisive evidence: swaps alone cannot hold an install that is behind upstream.

## Artifacts that worked

- **Silent watchdog:** a `*/2` cron entry running a read-only integrity check across the at-risk profiles, capturing the corrupt DB plus process list and recent errors on the first failure. It caught the race red-handed instead of leaving someone guessing. Cadence is deliberately inside the 30–60 min recurrence window so a fire is never missed. (See `scripts/corruption-watch.sh` in this skill.)
- **Corrupt DB backups** kept in a timestamped dir before every purge, so there is always a route back and an artifact for analysis.

## Non-factors, so you don't chase them

- A messaging platform "enabled but not paired (no credentials, session dir absent)" — pre-existing config on the host, unrelated to corruption.
- A periodic read-only health-check timer firing at the same moment as a corruption — it checked gateway units and disk and never wrote to `state.db`. Hourly correlation, innocent.

## How it was resolved

A **staged update**, run by the owner: update one low-stakes profile, verify 24h clean, then roll the rest. The install had already taken a fleet-wide update shortly before the wave started — so an update is simultaneously the cure and a known trigger. Never fire one unilaterally.
