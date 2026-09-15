#!/usr/bin/env bash
# corruption-watch.sh — silent-when-healthy sentinel for Hermes state.db corruption.
#
# Runs PRAGMA integrity_check against one or more Hermes profile state.db files.
# Silent on health. On failure it captures forensics (the corrupt DB + its WAL/shm,
# the running process list, and recent errors.log lines) so the next recurrence is
# evidence instead of a mystery.
#
# Read-only with respect to the database: it never opens state.db for writing, so it
# cannot itself trigger the corruption it is watching for.
#
# Usage:
#   corruption-watch.sh --profile-dir /home/agent/.hermes/profiles/robert [--profile-dir ...]
#   corruption-watch.sh --profiles-file /etc/corruption-watch.profiles
#
# Profiles file: one profile directory per line; blank lines and #-comments ignored.
#
# Env:
#   CAPTURE_DIR   where forensics land (default: /var/backups/hermes-corruption-catch)
#   ERRORS_TAIL   lines of errors.log to capture per hit (default: 80)
#   PS_MATCH      extra pgrep/pkill match string per profile (default: the profile basename)
#
# Exit code: 0 normally; 1 if any profile failed integrity_check.

set -uo pipefail

CAPTURE_DIR="${CAPTURE_DIR:-/var/backups/hermes-corruption-catch}"
ERRORS_TAIL="${ERRORS_TAIL:-80}"
PROFILE_DIRS=()
PROFILES_FILE=""

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile-dir)   PROFILE_DIRS+=("$2"); shift 2 ;;
    --profiles-file) PROFILES_FILE="$2"; shift 2 ;;
    --capture-dir)   CAPTURE_DIR="$2"; shift 2 ;;
    -h|--help)       usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

if [[ -n "$PROFILES_FILE" ]]; then
  if [[ ! -r "$PROFILES_FILE" ]]; then
    echo "corruption-watch: cannot read profiles file: $PROFILES_FILE" >&2
    exit 2
  fi
  while IFS= read -r line; do
    line="${line%%#*}"; line="$(echo "$line" | xargs)"
    [[ -n "$line" ]] && PROFILE_DIRS+=("$line")
  done < "$PROFILES_FILE"
fi

if [[ ${#PROFILE_DIRS[@]} -eq 0 ]]; then
  echo "corruption-watch: no profiles given" >&2
  exit 2
fi

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "corruption-watch: sqlite3 not found on PATH" >&2
  exit 2
fi

hits=0

for dir in "${PROFILE_DIRS[@]}"; do
  db="$dir/state.db"
  [[ -f "$db" ]] || continue

  result="$(sqlite3 "$db" 'PRAGMA integrity_check;' 2>&1)"
  [[ "$result" == "ok" ]] && continue

  # ---- corruption detected ----
  hits=$((hits + 1))
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  name="$(basename "$dir")"
  dest="$CAPTURE_DIR/${name}-${ts}"
  mkdir -p "$dest"

  echo "[$ts] CORRUPTION: $db"
  echo "[$ts]   integrity_check: $(echo "$result" | head -3 | tr '\n' ' ')"

  # Snapshot the corrupt trio (db + wal + shm) — preserve the exact on-disk state.
  cp -a "$db" "$dest/" 2>/dev/null
  cp -a "$db-wal" "$dest/" 2>/dev/null || true
  cp -a "$db-shm" "$dest/" 2>/dev/null || true

  # Who was running against it at the moment of detection.
  pgrep -af "hermes.*${PS_MATCH:-$name}" > "$dest/processes.txt" 2>/dev/null || \
    ps aux > "$dest/processes.txt" 2>/dev/null || true

  # Recent errors for the exact failure text.
  if [[ -f "$dir/logs/errors.log" ]]; then
    tail -n "$ERRORS_TAIL" "$dir/logs/errors.log" > "$dest/errors.log.tail" 2>/dev/null || true
  fi

  # Live fd state — distinguishes dead-FD writes from real corruption.
  gwpid="$(pgrep -f "hermes.*--profile[= ]${name}.*gateway" 2>/dev/null | head -1)"
  if [[ -n "$gwpid" ]]; then
    ls -l "/proc/$gwpid/fd" 2>/dev/null | grep -i 'state.db' > "$dest/gateway-fds.txt" 2>/dev/null || true
  fi

  echo "[$ts]   captured -> $dest"
done

exit $(( hits > 0 ? 1 : 0 ))
