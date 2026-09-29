"""`scripts/research/krt_compare.py` timeout semantics (Issue #5790).

The benchmark drives every kct run as ``uv run kct route ...``, so the process
it starts is ``uv`` and the router is a *grandchild*. ``subprocess.run(
timeout=...)`` only kills the direct child, which left the router alive and at
full CPU past the cap -- contending with, and inflating, every row measured
after the first timeout. These tests pin the two properties that fixes it:
the timeout is reported as a timeout, and nothing survives it.

The research script is not importable as a package module (it lives under
``scripts/``, not ``src/``), so it is loaded by path.
"""

from __future__ import annotations

import importlib.util
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "research" / "krt_compare.py"


def _load_krt_compare():
    spec = importlib.util.spec_from_file_location("krt_compare_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # `@dataclass` resolves annotations through sys.modules[cls.__module__];
    # registering first is what keeps `Board`'s definition from blowing up.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def krt_compare():
    return _load_krt_compare()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - not expected in-tree
        return True
    return True


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups required")
def test_timeout_kills_the_whole_process_tree(krt_compare, tmp_path: Path) -> None:
    """A grandchild that outlives its parent must not outlive the cap.

    The shell stands in for ``uv``: it backgrounds a long-running python
    grandchild (the stand-in for ``kct route``) and then waits. Killing only
    the direct child -- what ``subprocess.run(timeout=...)`` does -- reaps the
    shell and reparents the grandchild, which then runs to its own completion.
    Only a process-GROUP signal reaches it.
    """
    pidfile = tmp_path / "grandchild.pid"
    grandchild = (
        f"import os,time,pathlib;"
        f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()));"
        f"time.sleep(600)"
    )
    script = f"{shlex.quote(sys.executable)} -c {shlex.quote(grandchild)} & sleep 600"
    cmd = ["sh", "-c", script]

    log = tmp_path / "run.log"
    t0 = time.monotonic()
    rc, timed_out = krt_compare._run(cmd, tmp_path, log, timeout=3.0)
    elapsed = time.monotonic() - t0

    assert timed_out is True, "a run killed at the cap must be reported as a timeout"
    assert rc == -1
    assert elapsed < 60, f"the cap must bound the call, took {elapsed:.1f}s"
    assert "TIMEOUT" in log.read_text()

    for _ in range(50):  # the kill is asynchronous; give the OS a moment
        if pidfile.exists():
            break
        time.sleep(0.1)
    assert pidfile.exists(), "grandchild never started -- test would prove nothing"
    grandchild_pid = int(pidfile.read_text())

    for _ in range(50):
        if not _alive(grandchild_pid):
            break
        time.sleep(0.1)
    if _alive(grandchild_pid):
        os.kill(grandchild_pid, signal.SIGKILL)  # do not leak it out of the test
        pytest.fail(f"grandchild {grandchild_pid} survived the timeout")


def test_run_returns_exit_code_and_does_not_flag_a_clean_run(krt_compare, tmp_path: Path) -> None:
    """A command that finishes inside the cap reports its own status, not a timeout."""
    log = tmp_path / "run.log"
    rc, timed_out = krt_compare._run(
        [sys.executable, "-c", "raise SystemExit(7)"], tmp_path, log, 60
    )
    assert (rc, timed_out) == (7, False)
    assert "TIMEOUT" not in log.read_text()


def test_each_run_gets_its_own_process_group(krt_compare, tmp_path: Path) -> None:
    """``start_new_session`` is what makes the group kill target only this run.

    Without it the group id is the *benchmark's* own, and signalling it would
    take down the benchmark (and its siblings) instead of the run under the cap.
    """
    log = tmp_path / "run.log"
    out = tmp_path / "pgid.txt"
    code = f"import os,pathlib;pathlib.Path({str(out)!r}).write_text(str(os.getpgrp()))"
    rc, timed_out = krt_compare._run([sys.executable, "-c", code], tmp_path, log, 60)
    assert (rc, timed_out) == (0, False)
    assert int(out.read_text()) != os.getpgrp()


def test_timeout_records_a_timeout_row_in_the_table(krt_compare) -> None:
    """AC #1 of #5790: a capped run is printed as TIMEOUT, not as a bare runtime.

    A tool that wrote a partial board before the cap still gets scored, so the
    row carries real metrics; the timeout has to be visible on the row itself.
    """
    scored_but_capped = {
        "board": "05",
        "tool": "kct",
        "wall_s": 1200.0,
        "timed_out": True,
    }
    assert krt_compare._runtime(scored_but_capped) == "1200.0 (TIMEOUT)"
    assert krt_compare._runtime({**scored_but_capped, "timed_out": False}) == "1200.0"
    assert krt_compare._runtime({"wall_s": None}) == "n/a"


def test_documented_reproduce_command_matches_the_measured_board_set(krt_compare) -> None:
    """AC #2 of #5790: the reproduce command cannot drift from what is measured.

    Two halves, both structural rather than prose:

    1. ``--boards`` defaults to every key in ``BOARDS``, so defining a board is
       the same edit as adding it to the default run.
    2. Every ``krt_compare.py`` invocation in the doc's ``## Reproduce`` **code
       block** passes no ``--boards``, so it inherits that default instead of
       pinning a list that can go stale. Only the runnable lines are checked --
       the surrounding prose is free to mention the flag.
    """
    assert 'ap.add_argument("--boards", default=",".join(BOARDS))' in SCRIPT.read_text()

    doc = (REPO_ROOT / "docs/research/kicad-routing-tools-comparison.md").read_text()
    reproduce = doc.split("## Reproduce", 1)[1].split("\n## ", 1)[0]
    blocks = reproduce.split("```")[1::2]
    invocations = [
        line
        for block in blocks
        for line in block.splitlines()
        if "krt_compare.py" in line and not line.lstrip().startswith("#")
    ]
    assert invocations, "the Reproduce section must actually show the command"
    for line in invocations:
        assert "--boards" not in line, (
            f"pinning a board list here is how the two drift: {line.strip()}"
        )

    for key in ("04", "04f", "05", "05f", "07a", "07m", "07t"):
        assert key in krt_compare.BOARDS, f"{key} must be in the default board set"


def test_both_tools_get_the_same_declared_skew_budget(krt_compare) -> None:
    """Fairness invariant: the length-match tolerance comes from the BOARD.

    kct reads ``length_match_tolerance_mm`` from its net-class-map sidecar;
    KRT's own default is 0.1 mm. Handing KRT the board's declared
    ``group_skew_mm`` is what makes 07m one experiment instead of two.
    """
    import json

    spec = json.loads((REPO_ROOT / krt_compare.SDRAM_GROUPS_JSON).read_text())
    flags = krt_compare.BOARDS["07m"].krt_steps[0][1]
    assert "--length-match-tolerance" in flags
    assert flags[flags.index("--length-match-tolerance") + 1] == str(spec["group_skew_mm"])

    sidecar = json.loads(
        (REPO_ROOT / "boards/07-matchgroup-test/output/net_class_map.json").read_text()
    )
    tolerances = {
        v["length_match_tolerance_mm"]
        for v in sidecar.values()
        if v.get("length_match_group") is not None
    }
    assert tolerances == {spec["group_skew_mm"]}, "kct and KRT must see one number"


def test_recipe_variants_do_not_redefine_the_shared_rows(krt_compare) -> None:
    """A KRT-only variant must not silently re-measure kct under a second key."""
    for key in ("04f", "05f", "07t"):
        board = krt_compare.BOARDS[key]
        assert board.tools == ("krt",), f"{key} is a KRT-only recipe variant"
        assert board.fab_from in krt_compare.BOARDS, f"{key} needs a real fab-referee source"
    assert krt_compare.BOARDS["07m"].tools == ("kct", "krt")


def test_krt_output_flag_substitution(krt_compare, tmp_path: Path) -> None:
    """``{out}`` in a step's extra args is where that step's output path goes.

    ``qfn_fanout.py`` takes ``-o OUT`` rather than a second positional, so the
    fanout steps (04f/05f) would otherwise write to the wrong place.
    """
    fanout_step = krt_compare.BOARDS["04f"].krt_steps[0]
    assert fanout_step[0].endswith("qfn_fanout.py")
    assert "{out}" in fanout_step[1]
    plain_step = krt_compare.BOARDS["07a"].krt_steps[0]
    assert not any("{out}" in a for a in plain_step[1])


def test_script_has_no_syntax_or_import_errors() -> None:
    """The research script is not in CI; at least keep it importable."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import py_compile;py_compile.compile({str(SCRIPT)!r}, doraise=True)",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
