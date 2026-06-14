"""Daemonless automation: file-watch snapshot/diff/debounce and the cron helper."""

import os
import sys
import threading
import time

import pytest

from loopeng.triggers import (
    DEFAULT_IGNORE_DIRS,
    build_cron_entry,
    diff_snapshots,
    snapshot,
    upsert_cron,
    watch,
    _is_excluded,
)

PY = sys.executable or "python3"

# An "agent" that appends one byte to the counter file each time it runs. Counting
# bytes (not parsing an int) is race-free even if two runs ever overlapped.
AGENT = "import sys\nopen(sys.argv[1], 'ab').write(b'x')\n"


def _write(path, text="x"):
    path.write_text(text, encoding="utf-8")


# --- snapshot + diff ---------------------------------------------------------

def test_snapshot_and_diff_detect_a_change(tmp_path):
    f = tmp_path / "a.txt"
    _write(f)
    pattern = str(tmp_path / "**" / "*.txt")

    before = snapshot([pattern])
    assert str(f.resolve()) in before

    # Force a strictly different mtime so the change is unambiguous regardless of
    # filesystem mtime granularity.
    new_mtime = before[str(f.resolve())] + 5
    os.utime(f, (new_mtime, new_mtime))

    after = snapshot([pattern])
    assert diff_snapshots(before, after) == {str(f.resolve())}


def test_diff_reports_added_and_removed(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    _write(a)
    pattern = str(tmp_path / "*.txt")

    s1 = snapshot([pattern])
    _write(b)  # add b
    a.unlink()  # remove a
    s2 = snapshot([pattern])

    assert diff_snapshots(s1, s2) == {str(a.resolve()), str(b.resolve())}


def test_write_inside_dot_loopeng_is_excluded_from_diff(tmp_path):
    state = tmp_path / ".loopeng"
    state.mkdir()
    src = tmp_path / "src.txt"
    _write(src)
    pattern = str(tmp_path / "**" / "*.txt")

    before = snapshot([pattern])
    # Write *inside* .loopeng/ — this is loopeng's own state dir and must be
    # invisible to the watcher (otherwise the ledger/heartbeat would self-trigger).
    _write(state / "ledger.txt", "ledger")
    after = snapshot([pattern])

    assert diff_snapshots(before, after) == set()
    assert all(".loopeng" not in p for p in after)


def test_write_inside_pycache_is_excluded_from_diff(tmp_path):
    # Unlike .loopeng/.git (dot-dirs that glob '**' already hides), __pycache__ is
    # a NON-dot dir that recursive glob DOES descend into — so here _is_excluded is
    # the *only* line of defense, making this the load-bearing exclusion case.
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    src = tmp_path / "src.txt"
    _write(src)
    pattern = str(tmp_path / "**" / "*.txt")

    before = snapshot([pattern])
    assert str(src.resolve()) in before
    _write(cache / "compiled.txt", "bytecode")  # write into the non-dot ignored dir
    after = snapshot([pattern])

    assert diff_snapshots(before, after) == set()
    assert all("__pycache__" not in p for p in after)


def test_is_excluded_is_component_exact_not_substring(tmp_path):
    assert _is_excluded("/proj/.git/config", DEFAULT_IGNORE_DIRS) is True
    assert _is_excluded("/proj/src/__pycache__/x.pyc", DEFAULT_IGNORE_DIRS) is True
    # ".gitignore" is a file named like an ignored dir but is NOT a component match.
    assert _is_excluded("/proj/.gitignore", DEFAULT_IGNORE_DIRS) is False
    assert _is_excluded("/proj/src/app.py", DEFAULT_IGNORE_DIRS) is False


# --- watch debounce ----------------------------------------------------------

def _run_count(counter):
    return len(counter.read_bytes()) if counter.exists() else 0


def test_burst_of_writes_fires_exactly_one_run(tmp_path):
    counter = tmp_path / "runs.bin"
    watched = tmp_path / "watched.txt"
    _write(watched, "0")
    pattern = str(tmp_path / "watched.txt")
    run_args = [PY, "-c", AGENT, str(counter)]

    rc = {}

    # The burst must SPAN MANY POLL TICKS while staying inside ONE debounce window,
    # otherwise the test can't tell coalescing apart from firing-per-change:
    #   poll_interval=0.02  -> ~15 polls observe the burst as it unfolds
    #   debounce_quiet=0.4  -> the whole 0.32s burst fits inside one quiet window
    # max_runs=2 leaves headroom so a broken (un-debounced) watcher could fire
    # MORE than once during the burst (a max_runs=1 cap would mask that).
    def runner():
        rc["code"] = watch(
            [pattern],
            run_args,
            poll_interval=0.02,
            debounce_quiet=0.4,
            max_runs=2,
        )

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()

    # Eight writes spaced 0.04s apart (~0.32s total). Each write resets the debounce,
    # so a correct watcher fires NOTHING until the burst is over; an un-debounced one
    # fires on every poll tick that sees a fresh mtime.
    for i in range(8):
        _write(watched, str(i))
        time.sleep(0.04)

    # Immediately after the last write, the 0.4s quiet window has NOT yet elapsed,
    # so a correctly-debounced watcher has fired zero times. (A per-change watcher
    # would already have fired several times — and with max_runs=2 likely exited.)
    assert _run_count(counter) == 0, (
        f"watcher fired mid-burst (ran {_run_count(counter)}x): debounce not coalescing"
    )

    # Let the quiet window expire: the coalesced burst now fires exactly one run.
    time.sleep(0.6)
    assert _run_count(counter) == 1, (
        f"expected exactly one run for the burst, agent ran {_run_count(counter)} time(s)"
    )

    # A second, distinct change produces run #2 and lets the loop reach max_runs.
    _write(watched, "final")
    thread.join(timeout=5)
    assert not thread.is_alive(), "watch did not return within timeout"
    assert rc["code"] == 1  # max_runs reached -> exit code 1
    assert _run_count(counter) == 2


def test_write_only_inside_dot_git_does_not_trigger(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    counter = tmp_path / "runs.bin"
    pattern = str(tmp_path / "**" / "*")
    run_args = [PY, "-c", AGENT, str(counter)]

    rc = {}

    def runner():
        rc["code"] = watch(
            [pattern],
            run_args,
            poll_interval=0.05,
            debounce_quiet=0.1,
            max_runs=1,
        )

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()

    # Only ever touch files under .git/ — an excluded component, so the watcher
    # sees no change and never fires.
    for i in range(5):
        _write(git_dir / "index", str(i))
        time.sleep(0.02)

    thread.join(timeout=1.0)
    assert thread.is_alive(), "watch fired/returned despite only-ignored writes"
    assert not counter.exists(), "agent ran on an ignored-only change"

    # Now touch a real watched file to prove the watcher is live, then let it exit.
    _write(tmp_path / "real.txt", "real")
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert counter.exists() and len(counter.read_bytes()) == 1


def test_run_on_start_fires_immediately_then_honors_max_runs(tmp_path):
    counter = tmp_path / "runs.bin"
    pattern = str(tmp_path / "*.txt")
    run_args = [PY, "-c", AGENT, str(counter)]

    # run_on_start with max_runs=1 should fire once at startup and return 1 without
    # ever needing a file change.
    rc = watch(
        [pattern],
        run_args,
        poll_interval=0.05,
        debounce_quiet=0.1,
        run_on_start=True,
        max_runs=1,
    )
    assert rc == 1
    assert counter.exists() and len(counter.read_bytes()) == 1


# --- cron helper -------------------------------------------------------------

def test_build_cron_entry_contains_marker_and_joined_command():
    entry = build_cron_entry(
        "*/10 * * * *",
        ["loopeng", "run", "--spec", "loop.yaml"],
        marker="ci",
        workdir="/srv/app",
    )
    assert entry.endswith("# loopeng:ci")
    assert "loopeng run --spec loop.yaml" in entry
    assert "cd /srv/app &&" in entry
    assert entry.startswith("*/10 * * * *")


def test_build_cron_entry_quotes_spaces():
    entry = build_cron_entry(
        "0 0 * * *",
        ["loopeng", "run", "--spec", "my loop.yaml"],
        marker="m",
        workdir="/tmp/has space",
    )
    assert "'my loop.yaml'" in entry
    assert "'/tmp/has space'" in entry


def test_upsert_cron_is_idempotent_and_appends_once():
    entry = build_cron_entry("0 3 * * *", ["loopeng", "run"], marker="nightly")

    once = upsert_cron("", entry, "nightly")
    assert once.count("# loopeng:nightly") == 1
    assert entry in once

    # Applying the same entry again must not create a second line.
    twice = upsert_cron(once, entry, "nightly")
    assert twice.count("# loopeng:nightly") == 1
    assert once == twice


def test_upsert_cron_replaces_same_marker_line_in_place():
    old = build_cron_entry("0 3 * * *", ["loopeng", "run"], marker="nightly")
    new = build_cron_entry("0 5 * * *", ["loopeng", "run", "--max-iterations", "3"], marker="nightly")
    existing = f"# a user comment\n{old}\n0 0 * * * other-job\n"

    merged = upsert_cron(existing, new, "nightly")

    assert merged.count("# loopeng:nightly") == 1
    assert new in merged
    assert old not in merged
    # Surrounding, unrelated lines are preserved.
    assert "# a user comment" in merged
    assert "0 0 * * * other-job" in merged


def test_upsert_cron_preserves_unrelated_entries_when_appending():
    entry = build_cron_entry("0 3 * * *", ["loopeng", "run"], marker="nightly")
    existing = "0 0 * * * unrelated-job\n"
    merged = upsert_cron(existing, entry, "nightly")
    assert "0 0 * * * unrelated-job" in merged
    assert entry in merged
    assert merged.count("# loopeng:nightly") == 1


def test_build_cron_entry_rejects_multiline_marker():
    """A newline in the marker would inject a second crontab line and break the
    single-line tag upsert_cron keys on. Reject it."""
    with pytest.raises(ValueError, match="single line"):
        build_cron_entry("*/30 * * * *", ["loopeng", "run"], marker="nightly\nEVIL * * * * * sh -c x")


def test_build_cron_entry_rejects_wrong_field_count():
    """A non-5-field cron_expr would shift `cd` into the schedule and mangle the command."""
    with pytest.raises(ValueError, match="5 whitespace-separated fields"):
        build_cron_entry("* * * * * touch /tmp/x;", ["loopeng", "run"], marker="m")  # 6 tokens
    with pytest.raises(ValueError, match="5 whitespace-separated fields"):
        build_cron_entry("*/30 * * *", ["loopeng", "run"], marker="m")  # 4 fields


def test_build_cron_entry_normalizes_whitespace():
    entry = build_cron_entry("  0   3 * * *  ", ["loopeng", "run"], marker="m")
    assert entry.startswith("0 3 * * * cd ")


def test_build_cron_entry_rejects_empty_marker():
    with pytest.raises(ValueError, match="non-empty"):
        build_cron_entry("*/30 * * * *", ["loopeng", "run"], marker="   ")


def test_watch_max_runs_zero_fires_nothing(tmp_path):
    from loopeng.triggers import watch

    sentinel = tmp_path / "ran.log"
    cmd = [sys.executable, "-c", f"open({str(sentinel)!r}, 'a').write('x')"]
    rc = watch([str(tmp_path / "*.txt")], cmd, max_runs=0, run_on_start=True, poll_interval=0.05)
    assert rc == 1
    assert not sentinel.exists()  # zero runs, not one


def test_watch_rejects_nonpositive_poll_interval(tmp_path):
    from loopeng.triggers import watch

    with pytest.raises(ValueError, match="poll_interval"):
        watch([str(tmp_path / "*.txt")], ["true"], poll_interval=0)
