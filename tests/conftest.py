"""Shared pytest fixtures.

Isolate the default runs/ directory per test. ``AutopilotService`` defaults its ledger and learned-
skill store to ``$GR_AUTOPILOT_RUNS_DIR`` (falling back to ``cwd/runs``); pointing it at a per-test
tmp dir stops tests that omit an explicit ledger_path from sharing — and mutating — one repo-level
runs/session.jsonl, which previously coupled tests through a file and polluted the working tree.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_runs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GR_AUTOPILOT_RUNS_DIR", str(tmp_path / "runs"))


# --- make a silently reduced run impossible to miss ------------------------------------------
#
# The suite reports 844 passed under an interpreter with GNU Radio and 773 passed / 7 skipped
# under one without (plus one wiki test that needs `markdown`): ~64 tests quietly do not run, and
# nothing fails. That is exactly the shape of
# a green build that proves less than it claims, so the count is reported at the end of every run
# and can be made fatal.
#
#   GR_AUTOPILOT_REQUIRE_GNURADIO=1 python3 -m pytest      # CI: skipping is an error
#
# The radio worker inherits the daemon's interpreter for the same reason this matters: on a host
# with a system GNU Radio and a conda python earlier on PATH, `python3` is the wrong one.

def pytest_terminal_summary(terminalreporter, exitstatus, config):
    import os
    import sys

    skipped = terminalreporter.stats.get("skipped", [])
    if not skipped:
        return
    try:
        import gnuradio  # noqa: F401
        has_gr = True
    except ImportError:
        has_gr = False

    terminalreporter.write_sep("=", "reduced run", yellow=True)
    terminalreporter.write_line(
        f"{len(skipped)} test(s) skipped under {sys.executable}"
        f"{'' if has_gr else '  (GNU Radio NOT importable here)'}")
    if not has_gr:
        terminalreporter.write_line(
            "  This interpreter cannot exercise the DSP path. On a Debian/Ubuntu host with a "
            "system GNU Radio that is usually /usr/bin/python3, not a conda python earlier on PATH.")
    if os.environ.get("GR_AUTOPILOT_REQUIRE_GNURADIO") == "1":
        terminalreporter.write_line(
            "  GR_AUTOPILOT_REQUIRE_GNURADIO=1 -> failing the run rather than reporting green.")
        pytest.exit("skipped tests are errors when GR_AUTOPILOT_REQUIRE_GNURADIO=1", returncode=1)
