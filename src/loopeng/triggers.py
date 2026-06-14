"""Daemonless automation: a foreground file watcher and a cron-line helper.

Two ways to fire ``loopeng run`` without a long-lived service:

* ``watch`` — a single-threaded, polling file watcher. It snapshots the mtimes of
  the files matched by a set of globs, and when any change settles (a debounce
  window of quiet after the last change) it fires the run command once via
  ``subprocess.run``. No background threads, no inotify dependency: just a poll
  loop you Ctrl-C to stop. Debouncing collapses an editor's save burst (or a
  format-on-save touching many files) into a single run instead of one per write.

* the ``build_cron_entry`` / ``upsert_cron`` / ``install_crontab`` helpers — emit
  and idempotently install a single ``crontab`` line tagged with a
  ``# loopeng:<marker>`` comment, so ``loopeng`` schedules itself through the
  system cron rather than running its own scheduler daemon. ``upsert_cron``
  replaces an existing same-marker line in place, so re-installing never
  accumulates duplicates.
"""

from __future__ import annotations

import glob
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set

# Directories whose contents must never trigger a run: loopeng's own state, VCS
# internals, byte-code caches, and the virtualenv. A write under any of these
# (e.g. the ledger/heartbeat we write *because* of a run) would otherwise feed
# back into the watcher and loop forever.
DEFAULT_IGNORE_DIRS = (".loopeng", ".git", "__pycache__", ".venv")

CRON_MARKER_PREFIX = "# loopeng:"


# --------------------------------------------------------------------------- #
# Watch
# --------------------------------------------------------------------------- #

def _is_excluded(path, ignore_dirs: Iterable[str]) -> bool:
    """True when any path *component* exactly matches an ignored directory name.

    Component-level exact match (not substring): ``.git`` excludes ``a/.git/b``
    but not a sibling file literally named ``.gitignore``.
    """
    ignore = set(ignore_dirs)
    if not ignore:
        return False
    return any(part in ignore for part in Path(path).parts)


def snapshot(
    patterns: Iterable[str],
    ignore_dirs: Iterable[str] = DEFAULT_IGNORE_DIRS,
) -> Dict[str, float]:
    """Map absolute path -> mtime for every file matched by ``patterns``.

    Each pattern is expanded with ``glob.glob(p, recursive=True)`` (so ``**`` spans
    nested directories), paths are resolved to absolute form, and any path with an
    excluded component is dropped. A file that vanishes mid-scan (stat raising
    ``OSError``) is simply skipped — a deleted file is correctly absent from the
    snapshot, which ``diff_snapshots`` then reports as a removal.
    """
    out: Dict[str, float] = {}
    for pattern in patterns:
        for match in glob.glob(pattern, recursive=True):
            abspath = os.path.abspath(match)
            if _is_excluded(abspath, ignore_dirs):
                continue
            try:
                out[abspath] = os.stat(match).st_mtime
            except OSError:
                continue  # deleted between glob and stat; treated as absent
    return out


def diff_snapshots(old: Dict[str, float], new: Dict[str, float]) -> Set[str]:
    """Absolute paths that were added, removed, or had their mtime change."""
    changed: Set[str] = set()
    changed.update(set(new) - set(old))  # added
    changed.update(set(old) - set(new))  # removed
    for path in set(old) & set(new):
        if old[path] != new[path]:
            changed.add(path)
    return changed


def watch(
    patterns: Sequence[str],
    run_args: Sequence[str],
    *,
    poll_interval: float = 0.5,
    debounce_quiet: float = 0.3,
    ignore_dirs: Iterable[str] = DEFAULT_IGNORE_DIRS,
    run_on_start: bool = False,
    max_runs: Optional[int] = None,
) -> int:
    """Poll ``patterns`` and fire ``run_args`` once per settled change burst.

    The loop is single-threaded. A single ``pending_since`` timestamp tracks an
    unfired change: every observed change (re)sets it to *now*, and only once the
    files have been quiet for ``debounce_quiet`` seconds does the command fire and
    ``pending_since`` reset to ``None``. This collapses a multi-file save burst
    into one run instead of one run per file.

    ``debounce_quiet`` is clamped up to ``poll_interval``: a quiet window shorter
    than the poll cadence could never be observed as quiet (the next poll is
    already past it), so it is raised to ``poll_interval``.

    Returns an exit code: 0 on a clean stop (only reachable when ``max_runs`` is
    not set, the loop otherwise runs forever until interrupted), 1 when
    ``max_runs`` is reached, and 130 on SIGINT / KeyboardInterrupt (128 + SIGINT).
    """
    if poll_interval <= 0:
        raise ValueError("poll_interval must be > 0")
    # max_runs < 1 means "fire zero times": the budget is already exhausted, so stop
    # before firing even once (and before run_on_start).
    if max_runs is not None and max_runs < 1:
        return 1
    # A quiet window shorter than the poll cadence is unobservable; clamp it up.
    debounce_quiet = max(debounce_quiet, poll_interval)

    runs = 0
    state = snapshot(patterns, ignore_dirs)
    pending_since: Optional[float] = None

    def fire() -> bool:
        """Run the command; return True if the run budget is now exhausted."""
        nonlocal runs
        subprocess.run(list(run_args), check=False)
        runs += 1
        return max_runs is not None and runs >= max_runs

    try:
        if run_on_start:
            if fire():
                return 1

        while True:
            time.sleep(poll_interval)
            current = snapshot(patterns, ignore_dirs)
            if diff_snapshots(state, current):
                state = current
                pending_since = time.monotonic()  # change seen -> (re)start debounce
                continue
            # No change this tick. If a debounce is pending and the files have
            # been quiet long enough, fire exactly once.
            if pending_since is not None and (time.monotonic() - pending_since) >= debounce_quiet:
                pending_since = None
                if fire():
                    return 1
    except KeyboardInterrupt:
        return 130


# --------------------------------------------------------------------------- #
# Schedule (cron helper — no daemon)
# --------------------------------------------------------------------------- #

def build_cron_entry(
    cron_expr: str,
    run_args: List[str],
    *,
    marker: str,
    workdir: str = ".",
) -> str:
    """Build a single crontab line tagged with a ``# loopeng:<marker>`` comment.

    Shape: ``<cron_expr> cd <workdir> && <shell-joined run_args> # loopeng:<marker>``.
    ``workdir`` and every ``run_args`` token are ``shlex.quote``-escaped so paths
    with spaces or shell metacharacters survive cron's ``/bin/sh -c`` evaluation.
    The marker comment is what ``upsert_cron`` keys on to update the line in place.

    Inputs are validated so a multi-line value can't inject a *second* crontab line
    or break upsert_cron's single-line marker matching: ``marker``/``workdir`` may not
    contain a newline, and ``cron_expr`` must be exactly five whitespace-separated
    fields (a wrong count would otherwise shift ``cd`` into the schedule and mangle the
    command). Raises ``ValueError`` otherwise.
    """
    if not marker.strip():
        raise ValueError("schedule marker must be a non-empty single-line string")
    for label, value in (("marker", marker), ("workdir", workdir)):
        if any(ch in value for ch in "\r\n"):
            raise ValueError(f"schedule {label} must be a single line (no newline)")
    fields = cron_expr.split()
    if len(fields) != 5 or any(ch in cron_expr for ch in "\r\n"):
        raise ValueError(
            f"cron expression must be exactly 5 whitespace-separated fields, got {cron_expr!r}"
        )
    cron_norm = " ".join(fields)
    command = shlex.join(run_args)  # shlex.quote each token (Python 3.8+)
    return f"{cron_norm} cd {shlex.quote(workdir)} && {command} {CRON_MARKER_PREFIX}{marker}"


def current_crontab() -> str:
    """The installed crontab text, or ``""`` if there is none / ``crontab`` fails."""
    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, check=False
        )
    except OSError:
        return ""  # no crontab binary on this host
    if result.returncode != 0:
        return ""  # "no crontab for <user>" exits non-zero
    return result.stdout


def upsert_cron(existing: str, entry: str, marker: str) -> str:
    """Idempotently merge ``entry`` into ``existing`` keyed on its marker.

    If a line already ends with ``# loopeng:<marker>`` it is replaced in place
    (preserving the surrounding lines and their order); otherwise ``entry`` is
    appended. Applying the same entry twice yields exactly one matching line.
    """
    tag = f"{CRON_MARKER_PREFIX}{marker}"
    lines = existing.splitlines()
    replaced = False
    out: List[str] = []
    for line in lines:
        if line.rstrip().endswith(tag):
            if not replaced:
                out.append(entry)  # replace the first match in place
                replaced = True
            # drop any further duplicate same-marker lines (self-heal)
        else:
            out.append(line)
    if not replaced:
        out.append(entry)
    text = "\n".join(out)
    # crontab files conventionally end with a trailing newline.
    return text + "\n" if text else text


def install_crontab(text: str) -> None:
    """Replace the user's crontab with ``text`` by piping it to ``crontab -``.

    Raises ``subprocess.CalledProcessError`` if ``crontab`` rejects the input;
    lets ``FileNotFoundError`` propagate if the binary is absent.
    """
    subprocess.run(["crontab", "-"], input=text, text=True, check=True)
