"""Tests for the hermes-state-db-recovery optional skill.

Validates:
  - SKILL.md frontmatter conforms to the authoring standards (≤60-char
    description, required fields, human contributor credited first)
  - The Linux platform gate matches what the shipped script actually uses
    (``/proc`` descriptor inspection)
  - ``scripts/corruption-watch.sh`` is silent on a healthy database and
    captures forensics, with a non-zero exit, on a corrupt one

Everything runs against tmp_path databases built with the stdlib ``sqlite3``
module. No network, no touching the real ``~/.hermes``.
"""
from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
import yaml

SKILL_DIR = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "devops"
    / "hermes-state-db-recovery"
)
SCRIPT = SKILL_DIR / "scripts" / "corruption-watch.sh"

MARKETING = re.compile(
    r"\b(powerful|comprehensive|seamless|revolutionary|cutting-edge|state-of-the-art)\b",
    re.I,
)


@pytest.fixture(scope="module")
def skill_source() -> str:
    return (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def frontmatter(skill_source) -> dict:
    m = re.search(r"^---\n(.*?)\n---", skill_source, re.DOTALL)
    assert m, "SKILL.md missing YAML frontmatter"
    return yaml.safe_load(m.group(1))


# --------------------------------------------------------------------------
# Frontmatter / authoring standards
# --------------------------------------------------------------------------


def test_skill_dir_exists() -> None:
    assert SKILL_DIR.is_dir(), f"missing skill dir: {SKILL_DIR}"


def test_name_matches_directory(frontmatter) -> None:
    assert frontmatter["name"] == SKILL_DIR.name


def test_has_required_frontmatter_fields(frontmatter) -> None:
    for field in ("name", "description", "version", "author", "license", "platforms"):
        assert field in frontmatter, f"missing required field: {field}"


def test_description_hardline(frontmatter) -> None:
    desc = frontmatter["description"]
    assert len(desc) <= 60, f"description is {len(desc)} chars (limit ≤60): {desc!r}"
    assert desc.rstrip().endswith("."), "description must end with a period"
    assert not MARKETING.search(desc), f"marketing word in description: {desc!r}"


def test_credits_human_contributor_first(frontmatter) -> None:
    author = str(frontmatter["author"])
    assert "Tyler Gray" in author, "author must credit the human contributor first"
    assert author.index("Tyler Gray") < author.index("Hermes Agent"), (
        f"human contributor must come before 'Hermes Agent': {author!r}"
    )


def test_platform_gate_matches_script_primitives(frontmatter) -> None:
    # The watchdog reads /proc/<pid>/fd to distinguish dead-descriptor writes
    # from real corruption, which is Linux-only — so the skill must not
    # advertise itself on macOS.
    assert "linux" in frontmatter["platforms"]
    assert "/proc" in SCRIPT.read_text(encoding="utf-8"), (
        "script no longer uses /proc; revisit the platform gate"
    )


def test_related_skills_are_not_dangling(frontmatter) -> None:
    hermes_md = (frontmatter.get("metadata") or {}).get("hermes") or {}
    assert hermes_md.get("related_skills") in (None, [])


def test_sections_follow_authoring_order(skill_source) -> None:
    order = [
        "# ",
        "## When to Use",
        "## Prerequisites",
        "## How to Run",
        "## Quick Reference",
        "## Procedure",
        "## Pitfalls",
        "## Verification",
    ]
    positions = [skill_source.index(h) for h in order]
    assert positions == sorted(positions), f"section order violated: {order}"


def test_references_exist_and_are_linked(skill_source) -> None:
    for rel in ("references/restore-recipe.md", "references/recurrence-incident.md"):
        assert (SKILL_DIR / rel).is_file(), f"missing {rel}"
        assert Path(rel).name in skill_source, f"{rel} not referenced from SKILL.md"


def test_script_is_shipped_and_executable() -> None:
    assert SCRIPT.is_file(), f"missing {SCRIPT}"
    assert SCRIPT.stat().st_mode & 0o111, "corruption-watch.sh is not executable"


# --------------------------------------------------------------------------
# corruption-watch.sh behaviour
# --------------------------------------------------------------------------


def _run(args, capture_dir=None):
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "CAPTURE_DIR": str(capture_dir)}
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _make_healthy_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
    conn.executemany(
        "INSERT INTO messages (body) VALUES (?)", [(f"m{i}",) for i in range(2000)]
    )
    conn.commit()
    conn.close()


def _corrupt_db(path: Path) -> None:
    """Overwrite a mid-file page so PRAGMA integrity_check fails."""
    with path.open("r+b") as fh:
        fh.seek(4096 * 2)
        fh.write(b"\xff" * 512)


def test_watchdog_syntax_is_valid() -> None:
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_watchdog_is_silent_on_a_healthy_db(tmp_path) -> None:
    profile = tmp_path / "profiles" / "healthy"
    _make_healthy_db(profile / "state.db")

    r = _run(["--profile-dir", str(profile)], tmp_path / "capture")

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "", f"expected silence when healthy, got: {r.stdout!r}"
    assert not (tmp_path / "capture").exists()


def test_watchdog_detects_corruption_and_captures(tmp_path) -> None:
    profile = tmp_path / "profiles" / "broken"
    _make_healthy_db(profile / "state.db")
    _corrupt_db(profile / "state.db")
    (profile / "logs").mkdir(parents=True, exist_ok=True)
    (profile / "logs" / "errors.log").write_text(
        "hermes_state: connection was closed while a write was still in flight "
        "(teardown/worker race, #94736)\n",
        encoding="utf-8",
    )

    capture = tmp_path / "capture"
    r = _run(["--profile-dir", str(profile)], capture)

    assert r.returncode == 1, r.stderr
    assert "CORRUPTION" in r.stdout
    assert "malformed" in r.stdout

    captures = list(capture.glob("broken-*"))
    assert len(captures) == 1, f"expected one capture dir, got {captures}"
    snap = captures[0] / "state.db"
    assert snap.is_file(), "corrupt DB was not snapshotted"
    assert snap.read_bytes() == (profile / "state.db").read_bytes(), (
        "snapshot is not a byte-exact copy of the corrupt DB"
    )
    assert (captures[0] / "processes.txt").is_file()
    tail = (captures[0] / "errors.log.tail").read_text(encoding="utf-8")
    assert "#94736" in tail


def test_watchdog_reads_profile_list_from_a_file(tmp_path) -> None:
    profile = tmp_path / "profiles" / "listed"
    _make_healthy_db(profile / "state.db")
    profiles_file = tmp_path / "profiles.txt"
    profiles_file.write_text(
        f"# comment line\n\n{profile}\n", encoding="utf-8"
    )

    r = _run(["--profiles-file", str(profiles_file)], tmp_path / "capture")

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ""


def test_watchdog_fails_loudly_without_profiles(tmp_path) -> None:
    r = _run([], tmp_path / "capture")
    assert r.returncode == 2
    assert "no profiles" in r.stderr


def test_watchdog_skips_profiles_without_a_state_db(tmp_path) -> None:
    empty = tmp_path / "profiles" / "headless"
    empty.mkdir(parents=True)

    r = _run(["--profile-dir", str(empty)], tmp_path / "capture")

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ""


def test_watchdog_does_not_write_to_the_database(tmp_path) -> None:
    profile = tmp_path / "profiles" / "healthy"
    _make_healthy_db(profile / "state.db")
    before = (profile / "state.db").read_bytes()

    _run(["--profile-dir", str(profile)], tmp_path / "capture")

    assert (profile / "state.db").read_bytes() == before, (
        "watchdog modified the database it was inspecting"
    )


def test_script_has_no_unused_sqlite_cli_dependency(tmp_path) -> None:
    """The script must fail loudly, not silently pass, without sqlite3."""
    bash = shutil.which("bash")
    assert bash, "bash is unavailable on this host"
    profile = tmp_path / "profiles" / "healthy"
    _make_healthy_db(profile / "state.db")

    r = subprocess.run(
        [bash, str(SCRIPT), "--profile-dir", str(profile)],
        capture_output=True,
        text=True,
        env={"PATH": "/nonexistent", "CAPTURE_DIR": str(tmp_path / "capture")},
        timeout=60,
    )

    assert r.returncode == 2, "expected a loud failure when sqlite3 is unavailable"
    assert "sqlite3 not found" in r.stderr


def test_no_heredocs_in_script() -> None:
    # A bash heredoc in a shipped script fights the platform gate.
    text = SCRIPT.read_text(encoding="utf-8")
    assert "<<'" not in text and '<<"' not in text


def test_script_is_pure_posix_bash(tmp_path) -> None:
    if shutil.which("bash") is None:  # pragma: no cover - bash is a CI given
        pytest.skip("bash unavailable")
    assert SCRIPT.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")
