# Restore command recipe (verified)

Command-level recipe for the stop → purge → swap → verify → start procedure. Commands are shown run remotely over SSH against a profile owned by another user; drop the `ssh <host>` wrapper and the `sudo env` for a local profile owned by you.

Set two variables for the whole session:

```bash
PROFILE_DIR=/home/<user>/.hermes/profiles/<name>
PROFILE_NAME=<name>
```

## 1. Back up the corrupt files first

```bash
TS=$(date +%Y%m%d-%H%M%S)
DEST=/root/backups/wal-recorrupt-$TS
mkdir -p "$DEST"
cp -a "$PROFILE_DIR/state.db"      "$DEST/${PROFILE_NAME}-state.db-corr"
cp -a "$PROFILE_DIR/state.db-wal"  "$DEST/${PROFILE_NAME}-state.db-wal-corr" 2>/dev/null
cp -a "$PROFILE_DIR/state.db-shm"  "$DEST/${PROFILE_NAME}-state.db-shm-corr" 2>/dev/null
```

Never delete a corrupt DB without this.

## 2. Find and verify the clean snapshot

```bash
snap=$(ls "$PROFILE_DIR"/state-snapshots/*-pre-update/state.db | head -1)
sqlite3 "$snap" 'PRAGMA integrity_check;'   # must be: ok
ls "$snap"-wal "$snap"-shm 2>&1             # must be: No such file
```

If the snapshot fails integrity or ships a `-wal`, it is not a clean source. Go to the fresh-rebuild path in SKILL.md.

## 3. Stop the gateway (guard-safe form)

Inside a running gateway session, `systemctl stop/restart` is blocked by a local guard — it would SIGTERM your own process. Use the sanctioned command, which auto-approves and handles the system service:

```bash
sudo env HERMES_HOME="$PROFILE_DIR" HOME=/root \
  hermes --profile "$PROFILE_NAME" gateway stop

# confirm it is actually down before continuing
systemctl is-active "hermes-gateway-${PROFILE_NAME}.service"   # -> inactive/failed
```

Note: a bare `hermes --profile <name>` run as root fails with `Profile '<name>' does not exist` — root's default `HERMES_HOME` does not see other users' profiles. Setting `HERMES_HOME` to the profile dir fixes it. `hermes gateway restart` may also complain "requires root" → run it under `sudo`.

## 4. Purge + swap (the actual fix — purge the WAL/shm too)

```bash
cd "$PROFILE_DIR" && \
  rm -f state.db state.db-wal state.db-shm && \
  cp -a state-snapshots/*-pre-update/state.db state.db && \
  chown <user>:<user> state.db && chmod 644 state.db && \
  sqlite3 state.db 'PRAGMA integrity_check;'    # must be: ok
```

After this step there must be **no** `state.db-wal` or `state.db-shm` present.

## 5. Start and verify after warmup

```bash
sudo env HERMES_HOME="$PROFILE_DIR" HOME=/root \
  hermes --profile "$PROFILE_NAME" gateway start

sleep 10
sqlite3 "$PROFILE_DIR/state.db" 'PRAGMA integrity_check;'        # ok
systemctl is-active "hermes-gateway-${PROFILE_NAME}.service"     # active
```

Then grep `$PROFILE_DIR/logs/errors.log` for fresh `malformed` lines AFTER the restart timestamp — there should be none.

## Gotchas

- **The gateway recreates `state.db-wal`/`-shm` on every brief start**, so a transient restart before the swap re-poisons the file. Always: stop, THEN purge+swap, THEN start.
- **Same-name profiles can live under different users** (`/root/.hermes/profiles/<name>` vs `/home/<user>/.hermes/profiles/<name>`). Always operate on the path the systemd unit uses — read `/etc/systemd/system/hermes-gateway-<name>.service` for `WorkingDirectory` / `HERMES_HOME`.
- **A rollback loses state back to the snapshot.** Usually acceptable: writes had been failing since first corruption, so little was persisting anyway. Confirm the snapshot date is recent enough before committing to it.
- **Corruption arrives in batches.** When one profile is corrupt, integrity-check ALL profiles on the host and fix the whole batch, not just the loudest one.

## Follow-up: dead-FD `disk I/O error` after a clean restore

Even after a clean restore, the profile may still fail writes with `sqlite3.OperationalError: disk I/O error` at `BEGIN IMMEDIATE` while `PRAGMA integrity_check` returns `ok`. This is NOT corruption — it is stale/deleted file descriptors:

- **Cause:** the running gateway held open FDs to `state.db-wal`/`state.db-shm`, and the swap `rm`'d those files out from under it. SQLite writes through the dead inodes → `disk I/O error`.
- **Trap:** a CLI write to the same DB succeeds (fresh FDs) while the gateway fails — don't conclude the DB is fine or re-corrupting.
- **Diagnose:** `ls -l /proc/<gwpid>/fd/ | grep -i 'state.db'` shows `state.db-wal (deleted)` / `state.db-shm (deleted)`, AND `errors.log` shows live `disk I/O error` writes.
- **Fix:** `hermes gateway stop`, confirm `pgrep` shows no gateway running, `hermes gateway start`, then confirm the live FDs point at non-deleted `state.db-wal`/`state.db-shm` and the WAL is being written.
- **Prevention:** always stop the gateway BEFORE swapping `state.db` or its WAL/shm.
