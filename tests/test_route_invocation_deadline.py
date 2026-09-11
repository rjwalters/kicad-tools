"""Real subprocess regressions for post-routing work beyond the total deadline."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.cli import route_cmd, route_deadline


@pytest.mark.parametrize("adaptive_handler", [False, True])
def test_expensive_optimizer_unwinds_and_saves_only_raw_partial(tmp_path, adaptive_handler):
    source = tmp_path / "input.kicad_pcb"
    source.write_text("(kicad_pcb (version 20240108))")
    output = tmp_path / "output.kicad_pcb"
    output.write_text("previous successful output")
    script = f"""
from pathlib import Path
import signal
from types import SimpleNamespace
from kicad_tools.cli import route_cmd, route_deadline
from kicad_tools.router.optimizer import TraceOptimizer
signal.signal(signal.SIGTERM, route_cmd._handle_interrupt if {adaptive_handler!r} else route_deadline._deadline_signal)
def expensive_cleanup(self, *args):
    while True:
        sum(range(10000))
TraceOptimizer.optimize_route = expensive_cleanup
def work(argv):
    route_deadline.configure_output(SimpleNamespace(pcb={str(source)!r}, output={str(output)!r}))
    route_cmd._interrupt_state.update(router=SimpleNamespace(routes=[1], to_sexp=lambda **kw: '(segment (start 0 0) (end 1 1) (width 0.2) (layer "F.Cu") (net 1))'), output_path=Path({str(output)!r}), pcb_path=Path({str(source)!r}), best_completed_attempt=True)
    route_deadline.record_stage("optimization")
    try:
        TraceOptimizer().optimize_route(None)
    except Exception:
        raise AssertionError("timeout swallowed by ordinary fallback")
    raise AssertionError("cleanup must not complete")
route_cmd._main_impl = work
raise SystemExit(route_cmd._in_process_main([]))
"""
    started = time.monotonic()
    assert (
        route_deadline._supervise(
            [sys.executable, "-c", script], 1.5, tmp_path / "control.json", save_seconds=1
        )
        == 124
    )
    assert time.monotonic() - started < 4
    report = json.loads(output.with_suffix(".timeout.json").read_text())
    assert report["status"] == "partial"
    assert report["stage"] == "optimization"
    assert report["interrupted_function"] == "expensive_cleanup"
    assert report["snapshot_saved"] is True
    assert report["manufacturing_ready"] is False
    assert not output.exists()
    assert Path(report["unverified_output"]).read_text() == "previous successful output"
    assert "(segment" in Path(report["snapshot"]).read_text()
    assert source.read_text() == "(kicad_pcb (version 20240108))"


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group regression")
def test_unresponsive_work_and_descendant_are_killed_within_save_window(tmp_path):
    sentinel = tmp_path / "descendant-wrote"
    output = tmp_path / "output.kicad_pcb"
    descendant = f'import time; from pathlib import Path; time.sleep(1.3); Path({str(sentinel)!r}).write_text("bad")'
    script = f"""
import signal, subprocess, sys, time, json, os
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(os.environ["KCT_ROUTE_DEADLINE_CONTROL"]).write_text(json.dumps(dict(stage="native-conversion", output={str(output)!r}, snapshot_saved=False)))
subprocess.Popen([sys.executable, "-c", {descendant!r}])
while True: time.sleep(10)
"""
    started = time.monotonic()
    assert (
        route_deadline._supervise(
            [sys.executable, "-c", script], 0.4, tmp_path / "control.json", save_seconds=0.2
        )
        == 124
    )
    assert time.monotonic() - started < 1.5
    report = json.loads(output.with_suffix(".timeout.json").read_text())
    assert report["stage"] == "native-conversion"
    assert report["snapshot_saved"] is False
    time.sleep(0.9)
    assert not sentinel.exists()


def test_unresponsive_serialization_does_not_publish_truncated_snapshot(tmp_path):
    script = """
import signal, time, json, os
from pathlib import Path
signal.signal(signal.SIGTERM, lambda *_: time.sleep(100))
Path(os.environ["KCT_ROUTE_DEADLINE_CONTROL"]).write_text(json.dumps(dict(stage="drc-nudge")))
time.sleep(100)
"""
    started = time.monotonic()
    assert (
        route_deadline._supervise(
            [sys.executable, "-c", script], 0.3, tmp_path / "control.json", save_seconds=0.2
        )
        == 124
    )
    assert time.monotonic() - started < 1.5


@pytest.mark.parametrize("timeout", [None, "0", "-1"])
def test_unbounded_calls_remain_in_process(monkeypatch, timeout):
    monkeypatch.setattr(route_cmd, "_main_impl", lambda argv: 17)
    args = ["board.kicad_pcb"] + ([] if timeout is None else ["--timeout", timeout])
    assert route_cmd.main(args) == 17


def test_finite_main_is_supervised_without_resetting_budget(monkeypatch):
    calls = []
    monkeypatch.setattr(
        route_deadline,
        "_supervise",
        lambda command, budget, control: calls.append((command, budget)) or 124,
    )
    assert route_cmd.main(["board.kicad_pcb", "--timeout", "1.25"]) == 124
    assert calls[0][1] == 1.25
    assert calls[0][0][:3] == [sys.executable, "-m", "kicad_tools.cli.route_deadline"]


def test_inner_routing_budget_cannot_exceed_parent_deadline(monkeypatch):
    monkeypatch.setenv(route_deadline.DEADLINE_ENV, "100.0")
    monkeypatch.setattr(route_cmd.time, "monotonic", lambda: 97.0)
    args = SimpleNamespace(timeout=90, auto_fix=False)
    route_cmd._set_wall_clock_deadline(args)
    assert args._wall_clock_deadline == 100
    assert args._routing_deadline == 100


def test_timed_in_place_output_is_rejected_without_writes(tmp_path, monkeypatch):
    source = tmp_path / "source.kicad_pcb"
    source.write_text("original")
    monkeypatch.setenv(route_deadline.CONTROL_ENV, str(tmp_path / "control.json"))
    with pytest.raises(ValueError, match="separate output"):
        route_deadline.configure_output(SimpleNamespace(pcb=str(source), output=str(source)))
    assert source.read_text() == "original"


def test_worker_and_standalone_entrypoints_do_not_recurse(tmp_path):
    for module in ["kicad_tools.cli.route_cmd", "kicad_tools.cli.route_deadline"]:
        result = subprocess.run(
            [sys.executable, "-m", module, str(tmp_path / "missing.kicad_pcb"), "--timeout", "5"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        assert result.returncode == 1
        assert "not found" in result.stderr.lower() or "does not exist" in result.stderr.lower()


def test_completed_worker_preserves_exit_code_and_canonical_output(tmp_path):
    output = tmp_path / "result.kicad_pcb"
    script = f'from pathlib import Path; Path({str(output)!r}).write_text("completed"); raise SystemExit(3)'
    assert (
        route_deadline._supervise([sys.executable, "-c", script], 2, tmp_path / "control.json") == 3
    )
    assert output.read_text() == "completed"
    assert not output.with_suffix(".timeout.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX interruption/process group control")
def test_parent_interruption_terminates_worker_group(tmp_path):
    import signal

    control = tmp_path / "control.json"
    sentinel = tmp_path / "late-write"
    descendant = f'from pathlib import Path; import time; time.sleep(1.2); Path({str(sentinel)!r}).write_text("bad")'
    child = f"""
import signal, subprocess, sys, time, os, json
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
subprocess.Popen([sys.executable, "-c", {descendant!r}])
Path(os.environ["KCT_ROUTE_DEADLINE_CONTROL"]).write_text(json.dumps(dict(stage="optimization")))
time.sleep(100)
"""
    launcher = f"""
from pathlib import Path
import sys
from kicad_tools.cli.route_deadline import _supervise
raise SystemExit(_supervise([sys.executable, "-c", {child!r}], 20, Path({str(control)!r}), save_seconds=.2))
"""
    process = subprocess.Popen([sys.executable, "-c", launcher])
    try:
        deadline = time.monotonic() + 5
        while not control.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert control.exists(), "worker did not start within bounded setup window"
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=2) == 130
        time.sleep(1.1)
        assert not sentinel.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_startup_timeout_cannot_leave_old_canonical_output(tmp_path):
    source = tmp_path / "input.kicad_pcb"
    source.write_text("(kicad_pcb)")
    output = source.with_stem("input_routed")
    output.write_text("old successful result")
    assert route_cmd.main([str(source), "--timeout", "0.001"]) == 124
    report = json.loads(output.with_suffix(".timeout.json").read_text())
    assert report["status"] == "partial"
    assert report["snapshot_saved"] is False
    assert not output.exists()
    assert Path(report["unverified_output"]).read_text() == "old successful result"
    assert source.read_text() == "(kicad_pcb)"
