"""Tests for the native-subprocess concurrency gate (Issue #5501).

The gate has to hold across *processes* (four xdist workers plus the ``kct``
children they spawn), so the enforcement tests launch real fake-native
executables from real subprocesses and reconstruct the overlap from timestamps
the fake executable itself writes. A control run with the gate disabled proves
the same workload really does overlap, so a passing bounded run is evidence and
not a scheduling accident.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from kicad_tools import native_concurrency

REPO_ROOT = Path(__file__).resolve().parent.parent

FAKE_NATIVE = """\
#!{python}
import fcntl, os, sys, time
log = os.environ["FAKE_NATIVE_LOG"]
hold = float(os.environ.get("FAKE_NATIVE_HOLD", "0.4"))
exit_code = int(os.environ.get("FAKE_NATIVE_EXIT", "0"))


def mark(kind):
    with open(log, "a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.write("%s %.6f\\n" % (kind, time.monotonic()))


mark("start")
time.sleep(hold)
mark("end")
sys.exit(exit_code)
"""

LAUNCHER = """\
import subprocess, sys, threading
from kicad_tools.native_concurrency import install_from_environment

install_from_environment()
executable, count = sys.argv[1], int(sys.argv[2])
errors = []


def launch():
    try:
        subprocess.run([executable], check=False)
    except Exception as exc:  # pragma: no cover - surfaced through exit status
        errors.append(repr(exc))


threads = [threading.Thread(target=launch) for _ in range(count)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
if errors:
    print("\\n".join(errors), file=sys.stderr)
    sys.exit(3)
"""


def _load_observer_module():
    """Load the standalone CI observer, which is not an importable package."""
    path = REPO_ROOT / "scripts" / "ci" / "native_observer.py"
    spec = importlib.util.spec_from_file_location("native_observer_for_tests", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_analyzer_module():
    path = REPO_ROOT / "scripts" / "ci" / "analyze_native_observations.py"
    spec = importlib.util.spec_from_file_location("analyze_native_observations_for_tests", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_native(tmp_path):
    """An executable literally named ``kicad-cli`` that records its lifetime."""
    directory = tmp_path / "bin"
    directory.mkdir()
    script = directory / "kicad-cli"
    script.write_text(FAKE_NATIVE.format(python=sys.executable))
    script.chmod(0o755)
    return script


def _overlap(log: Path) -> int:
    """Maximum number of fake-native executions alive at the same instant."""
    events = []
    for line in log.read_text().splitlines():
        kind, stamp = line.split()
        events.append((float(stamp), 1 if kind == "start" else -1))
    # An end that shares a timestamp with a start must not read as an overlap.
    events.sort(key=lambda item: (item[0], item[1]))
    live = peak = 0
    for _stamp, delta in events:
        live += delta
        peak = max(peak, live)
    return peak


def _run_workload(fake_native, tmp_path, *, processes, per_process, limit, hold="0.4", **extra):
    launcher = tmp_path / "launcher.py"
    launcher.write_text(LAUNCHER)
    log = tmp_path / f"native-{limit or 'off'}-{time.monotonic_ns()}.log"
    environment = {
        **os.environ,
        "FAKE_NATIVE_LOG": str(log),
        "FAKE_NATIVE_HOLD": hold,
        native_concurrency.ENV_SLOT_DIR: str(tmp_path / "slots"),
        "PYTHONPATH": os.pathsep.join(
            filter(None, [str(REPO_ROOT / "src"), os.environ.get("PYTHONPATH")])
        ),
        **extra,
    }
    environment.pop(native_concurrency.ENV_LIMIT, None)
    environment.pop(native_concurrency.ENV_HELD, None)
    if limit is not None:
        environment[native_concurrency.ENV_LIMIT] = str(limit)
    children = [
        subprocess.Popen(
            [sys.executable, str(launcher), str(fake_native), str(per_process)],
            env=environment,
            stderr=subprocess.PIPE,
        )
        for _ in range(processes)
    ]
    for child in children:
        _stdout, stderr = child.communicate(timeout=120)
        assert child.returncode == 0, stderr.decode()
    return log


@pytest.mark.parametrize("limit", [1, 2])
def test_gate_bounds_native_launches_across_processes(fake_native, tmp_path, limit):
    log = _run_workload(fake_native, tmp_path, processes=3, per_process=2, limit=limit)
    assert len(log.read_text().splitlines()) == 12  # every launch still ran
    assert _overlap(log) <= limit


def test_control_workload_really_overlaps_without_the_gate(fake_native, tmp_path):
    """Negative control: the same workload exceeds the bound when ungated."""
    log = _run_workload(fake_native, tmp_path, processes=3, per_process=2, limit=None)
    assert len(log.read_text().splitlines()) == 12
    assert _overlap(log) > 2


def test_gate_is_inert_without_configuration(monkeypatch):
    monkeypatch.delenv(native_concurrency.ENV_LIMIT, raising=False)
    assert native_concurrency.configured_limit() is None
    monkeypatch.setenv(native_concurrency.ENV_LIMIT, "0")
    assert native_concurrency.configured_limit() is None
    monkeypatch.setenv(native_concurrency.ENV_LIMIT, "not-a-number")
    assert native_concurrency.configured_limit() is None
    monkeypatch.setenv(native_concurrency.ENV_LIMIT, "3")
    assert native_concurrency.configured_limit() == 3


def test_install_does_not_patch_subprocess_when_unconfigured(tmp_path):
    """A default (non-CI) interpreter must keep the stdlib ``Popen``."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        textwrap.dedent(
            """
            import subprocess, sys
            import kicad_tools  # installs the gate only when configured
            from kicad_tools.native_concurrency import install_from_environment
            print(subprocess.Popen.__module__ == "subprocess", install_from_environment())
            """
        )
    )
    environment = {**os.environ}
    environment.pop(native_concurrency.ENV_LIMIT, None)
    result = subprocess.run(
        [sys.executable, str(probe)], env=environment, capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True False"


@pytest.mark.parametrize(
    ("argv", "native"),
    [
        (["kicad-cli", "pcb", "drc"], True),
        (["/usr/bin/kicad-cli", "pcb", "export", "svg"], True),
        (["python3", "/x/kicad_tools/zones/_fixed_fill_worker.py", "board"], True),
        (["python3.12", "-m", "kicad_tools.cli", "route"], False),
        (["python3", "-c", "print(1)"], False),
        (["uv", "run", "pytest"], False),
        (["kicad-cli-wrapper"], False),
        ([], False),
        ("kicad-cli pcb drc", False),
    ],
)
def test_native_classification(argv, native):
    assert native_concurrency.is_native_launch(argv) is native


def test_classification_matches_the_independent_observer():
    """The gate and the /proc observer must never disagree about 'native'."""
    observer = _load_observer_module()
    cases = [
        ["kicad-cli", "pcb", "drc"],
        ["/opt/kicad/bin/kicad-cli", "version"],
        ["python3", "/x/kicad_tools/zones/_fixed_fill_worker.py"],
        ["python3.12", "-m", "kicad_tools.cli", "route"],
        ["python3", "-c", "pass"],
        ["uv", "run", "pytest"],
        ["git", "status"],
    ]
    for argv in cases:
        expected = observer.category(argv) in ("kicad-cli", "kicad-python-fill")
        assert native_concurrency.is_native_launch(argv) is expected, argv


def test_executable_keyword_is_classified(tmp_path):
    assert native_concurrency.is_native_launch(["ignored"], executable="/usr/bin/kicad-cli")


def _hold_every_slot(directory: Path, limit: int):
    """Hold the whole semaphore from this process, as a peer worker would."""
    import fcntl

    directory.mkdir(parents=True, exist_ok=True)
    handles = []
    for index in range(limit):
        # Deliberately not a context manager: the flock must outlive this
        # function, exactly as a peer worker's held permit would.
        handle = open(directory / f"slot-{index}", "a+")  # noqa: SIM115
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handles.append(handle)
    return handles


def test_nested_native_launch_reuses_the_subtree_permit(fake_native, tmp_path, monkeypatch):
    """A permitted child's own native launch must not deadlock behind itself."""
    slots = tmp_path / "slots"
    handles = _hold_every_slot(slots, 1)
    try:
        monkeypatch.setenv(native_concurrency.ENV_LIMIT, "1")
        monkeypatch.setenv(native_concurrency.ENV_SLOT_DIR, str(slots))
        monkeypatch.setenv(native_concurrency.ENV_HELD, "1")
        monkeypatch.setenv("FAKE_NATIVE_LOG", str(tmp_path / "nested.log"))
        monkeypatch.setenv("FAKE_NATIVE_HOLD", "0.05")
        started = time.monotonic()
        with native_concurrency._GatedPopen([str(fake_native)]) as child:
            child.wait(timeout=30)
        assert child.returncode == 0
        assert time.monotonic() - started < 10
    finally:
        for handle in handles:
            handle.close()


def test_non_native_launch_is_never_gated(tmp_path, monkeypatch):
    slots = tmp_path / "slots"
    handles = _hold_every_slot(slots, 1)
    try:
        monkeypatch.setenv(native_concurrency.ENV_LIMIT, "1")
        monkeypatch.setenv(native_concurrency.ENV_SLOT_DIR, str(slots))
        monkeypatch.delenv(native_concurrency.ENV_HELD, raising=False)
        started = time.monotonic()
        with native_concurrency._GatedPopen([sys.executable, "-c", "pass"]) as child:
            child.wait(timeout=30)
        assert child.returncode == 0
        assert time.monotonic() - started < 10
    finally:
        for handle in handles:
            handle.close()


def test_permit_is_released_after_failure_timeout_and_kill(fake_native, tmp_path, monkeypatch):
    """Every terminal path must return the slot, or the pool drains to zero."""
    slots = tmp_path / "slots"
    monkeypatch.setenv(native_concurrency.ENV_LIMIT, "1")
    monkeypatch.setenv(native_concurrency.ENV_SLOT_DIR, str(slots))
    monkeypatch.setenv(native_concurrency.ENV_WAIT_SECONDS, "5")
    monkeypatch.delenv(native_concurrency.ENV_HELD, raising=False)
    monkeypatch.setenv("FAKE_NATIVE_LOG", str(tmp_path / "terminal.log"))

    monkeypatch.setenv("FAKE_NATIVE_HOLD", "0.05")
    monkeypatch.setenv("FAKE_NATIVE_EXIT", "7")
    with native_concurrency._GatedPopen([str(fake_native)]) as failed:
        assert failed.wait(timeout=30) == 7

    monkeypatch.setenv("FAKE_NATIVE_HOLD", "30")
    monkeypatch.setenv("FAKE_NATIVE_EXIT", "0")
    original = subprocess.Popen
    subprocess.Popen = native_concurrency._GatedPopen
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            subprocess.run([str(fake_native)], timeout=0.5)
    finally:
        subprocess.Popen = original

    killed = native_concurrency._GatedPopen([str(fake_native)])
    killed.kill()
    killed.wait(timeout=30)

    # All three terminal paths returned their slot, so this still acquires
    # quickly instead of burning the 5s fail-open ceiling.
    monkeypatch.setenv("FAKE_NATIVE_HOLD", "0.05")
    started = time.monotonic()
    with native_concurrency._GatedPopen([str(fake_native)]) as last:
        assert last.wait(timeout=30) == 0
    assert time.monotonic() - started < 4


def test_wait_ceiling_launches_unbounded_instead_of_hanging(fake_native, tmp_path, monkeypatch):
    slots = tmp_path / "slots"
    handles = _hold_every_slot(slots, 1)
    log = tmp_path / "records"
    try:
        monkeypatch.setenv(native_concurrency.ENV_LIMIT, "1")
        monkeypatch.setenv(native_concurrency.ENV_SLOT_DIR, str(slots))
        monkeypatch.setenv(native_concurrency.ENV_WAIT_SECONDS, "0.5")
        monkeypatch.setenv(native_concurrency.ENV_LOG, str(log))
        monkeypatch.delenv(native_concurrency.ENV_HELD, raising=False)
        monkeypatch.setenv("FAKE_NATIVE_LOG", str(tmp_path / "ceiling.log"))
        monkeypatch.setenv("FAKE_NATIVE_HOLD", "0.05")
        started = time.monotonic()
        with native_concurrency._GatedPopen([str(fake_native)]) as child:
            assert child.wait(timeout=30) == 0
        waited = time.monotonic() - started
    finally:
        for handle in handles:
            handle.close()
    assert 0.5 <= waited < 20
    records = [
        json.loads(line)
        for path in log.glob("slots-*.jsonl")
        for line in path.read_text().splitlines()
    ]
    assert [r["reason"] for r in records if r["event"] == "unbounded"] == ["wait_ceiling"]


def test_permit_records_are_written_and_redacted(fake_native, tmp_path, monkeypatch):
    log = tmp_path / "records"
    monkeypatch.setenv(native_concurrency.ENV_LIMIT, "1")
    monkeypatch.setenv(native_concurrency.ENV_SLOT_DIR, str(tmp_path / "slots"))
    monkeypatch.setenv(native_concurrency.ENV_LOG, str(log))
    monkeypatch.delenv(native_concurrency.ENV_HELD, raising=False)
    monkeypatch.setenv("FAKE_NATIVE_LOG", str(tmp_path / "records.log"))
    monkeypatch.setenv("FAKE_NATIVE_HOLD", "0.05")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/t.py::test_x[secret-token] (call)")
    with native_concurrency._GatedPopen([str(fake_native)]) as child:
        assert child.wait(timeout=30) == 0
    records = [
        json.loads(line)
        for path in log.glob("slots-*.jsonl")
        for line in path.read_text().splitlines()
    ]
    assert [record["event"] for record in records] == ["acquire", "release"]
    assert all("secret-token" not in json.dumps(record) for record in records)
    assert records[0]["owner"].startswith("tests/t.py::test_x[redacted-")


def test_native_slot_context_manager_is_reentrant_and_inert(tmp_path, monkeypatch):
    monkeypatch.setenv(native_concurrency.ENV_SLOT_DIR, str(tmp_path / "slots"))
    monkeypatch.delenv(native_concurrency.ENV_LIMIT, raising=False)
    with native_concurrency.native_slot() as permit:
        assert permit.bounded is False
    monkeypatch.setenv(native_concurrency.ENV_LIMIT, "1")
    with native_concurrency.native_slot() as permit:
        assert permit.bounded is True
        monkeypatch.setenv(native_concurrency.ENV_HELD, "1")
        with native_concurrency.native_slot() as nested:
            assert nested.bounded is False


def _write_group(root: Path, name: str, *, samples, events):
    directory = root / name
    directory.mkdir(parents=True)
    with (directory / "processes.jsonl").open("w") as stream:
        for sample in samples:
            stream.write(json.dumps({"event": "process_sample", **sample}) + "\n")
    with (directory / "observer.jsonl").open("w") as stream:
        memory = {
            "available": True,
            "memory.current": 1 << 30,
            "memory.peak": 2 << 30,
            "memory.max": 12 << 30,
            "memory.events": events,
        }
        stream.write(json.dumps({"time_ns": 0, "event": "memory_sample", "memory": memory}) + "\n")
        stream.write(
            json.dumps(
                {
                    "time_ns": 1,
                    "event": "finish",
                    "exit_code": 0,
                    "memory": memory,
                    "memory_events_delta": events,
                    "diagnostics_degraded": False,
                }
            )
            + "\n"
        )
    return directory


def _native_sample(pid, time_ns, owner):
    return {
        "time_ns": time_ns,
        "pid": pid,
        "start_ticks": pid,
        "ppid": 1,
        "rss_bytes": 2 << 30,
        "sampled_peak_rss_bytes": 2 << 30,
        "category": "kicad-cli",
        "owner": owner,
        "worker": "gw0",
    }


NO_EVENTS = {"low": 0, "high": 0, "max": 0, "oom": 0, "oom_kill": 0, "oom_group_kill": 0}


def test_analyzer_flags_overlap_oom_and_passes_a_bounded_run(tmp_path, capsys):
    analyzer = _load_analyzer_module()
    root = tmp_path / "native-observer"
    # Two native processes whose sample windows overlap.
    _write_group(
        root,
        "bulk",
        samples=[
            _native_sample(11, 1_000_000_000, "tests/a.py::test_a (call)"),
            _native_sample(11, 3_000_000_000, "tests/a.py::test_a (call)"),
            _native_sample(12, 2_000_000_000, "tests/b.py::test_b (call)"),
            _native_sample(12, 4_000_000_000, "tests/b.py::test_b (call)"),
        ],
        events=NO_EVENTS,
    )
    assert analyzer.main([str(root), "--max-concurrency", "1"]) == 1
    assert "exceeds the configured bound 1" in capsys.readouterr().err
    assert analyzer.main([str(root), "--max-concurrency", "2"]) == 0

    # Serialised windows pass at a bound of 1, and an OOM kill never does.
    serial = tmp_path / "serial"
    _write_group(
        serial,
        "bulk",
        samples=[
            _native_sample(11, 1_000_000_000, "tests/a.py::test_a (call)"),
            _native_sample(11, 2_000_000_000, "tests/a.py::test_a (call)"),
            _native_sample(12, 3_000_000_000, "tests/b.py::test_b (call)"),
            _native_sample(12, 4_000_000_000, "tests/b.py::test_b (call)"),
        ],
        events=NO_EVENTS,
    )
    assert analyzer.main([str(serial), "--max-concurrency", "1"]) == 0
    killed = tmp_path / "killed"
    _write_group(
        killed,
        "bulk",
        samples=[_native_sample(11, 1_000_000_000, "tests/a.py::test_a (call)")],
        events={**NO_EVENTS, "oom_kill": 1},
    )
    assert analyzer.main([str(killed), "--max-concurrency", "1"]) == 1
    assert "oom_kill" in capsys.readouterr().err


def test_analyzer_reports_attribution_and_unbounded_gate_launches(tmp_path):
    analyzer = _load_analyzer_module()
    root = tmp_path / "native-observer"
    directory = _write_group(
        root,
        "bulk",
        samples=[
            _native_sample(11, 1_000_000_000, "tests/a.py::test_a (call)"),
            _native_sample(11, 2_000_000_000, "tests/a.py::test_a (call)"),
        ],
        events=NO_EVENTS,
    )
    (directory / "slots-99.jsonl").write_text(
        json.dumps({"time_ns": 1, "pid": 99, "event": "acquire", "slot": 0, "waited_s": 0.5})
        + "\n"
        + json.dumps({"time_ns": 2, "pid": 99, "event": "release", "slot": 0})
        + "\n"
        + json.dumps({"time_ns": 3, "pid": 99, "event": "unbounded", "reason": "wait_ceiling"})
        + "\n"
    )
    out = tmp_path / "attribution.json"
    assert analyzer.main([str(root), "--max-concurrency", "1", "--json", str(out)]) == 1
    summary = json.loads(out.read_text())
    group = summary["groups"][0]
    assert group["native_processes"] == 1
    assert group["top_modules_by_native_seconds"][0]["module"] == "tests/a.py"
    assert group["top_tests_by_native_processes"][0]["nodeid"] == "tests/a.py::test_a (call)"
    assert group["memory"]["memory_max"] == [12 << 30]
    assert group["gate"]["unbounded_launches"] == 1
    assert group["gate"]["wait_seconds_max"] == 0.5


def _worker_sample(pid, time_ns, owner, worker):
    return {**_native_sample(pid, time_ns, owner), "worker": worker}


def test_analyzer_simulates_what_a_bound_would_have_cost(tmp_path):
    """The bound was chosen from this model, so the model itself is tested.

    Two workers each run one 1 s native process at the same instant. A bound of
    2 costs nothing; a bound of 1 must serialise them, adding exactly one
    process' duration of wait and the same amount of span.
    """
    analyzer = _load_analyzer_module()
    root = tmp_path / "native-observer"
    second = 1_000_000_000
    _write_group(
        root,
        "bulk",
        samples=[
            _worker_sample(11, 10 * second, "tests/a.py::test_a (call)", "gw0"),
            _worker_sample(11, 11 * second, "tests/a.py::test_a (call)", "gw0"),
            _worker_sample(12, 10 * second, "tests/b.py::test_b (call)", "gw1"),
            _worker_sample(12, 11 * second, "tests/b.py::test_b (call)", "gw1"),
        ],
        events=NO_EVENTS,
    )
    out = tmp_path / "attribution.json"
    # --max-concurrency 2 passes (the observations really do show 2), while the
    # simulation independently reports what 1 would have cost that same run.
    assert (
        analyzer.main(
            [str(root), "--max-concurrency", "2", "--simulate-bound", "1", "--json", str(out)]
        )
        == 0
    )
    group = json.loads(out.read_text())["groups"][0]
    (model,) = group["simulated_bounds"]
    assert model["bound"] == 1
    assert model["observed_native_span_seconds"] == pytest.approx(1.0)
    assert model["simulated_native_span_seconds"] == pytest.approx(2.0)
    assert model["added_span_seconds"] == pytest.approx(1.0)
    assert model["total_added_wait_seconds"] == pytest.approx(1.0)
    assert model["longest_single_wait_seconds"] == pytest.approx(1.0)
    assert model["worst_tests_by_added_wait"][0]["added_wait_seconds"] == pytest.approx(1.0)

    # The bound the run already satisfied must cost nothing in the model.
    assert analyzer.main([str(root), "--simulate-bound", "2", "--json", str(out)]) == 0
    (unconstrained,) = json.loads(out.read_text())["groups"][0]["simulated_bounds"]
    assert unconstrained["added_span_seconds"] == pytest.approx(0.0)
    assert unconstrained["total_added_wait_seconds"] == pytest.approx(0.0)


def test_analyzer_rejects_a_non_positive_simulated_bound(tmp_path):
    analyzer = _load_analyzer_module()
    with pytest.raises(SystemExit):
        analyzer.main([str(tmp_path), "--simulate-bound", "0"])


def test_analyzer_requires_observations(tmp_path, capsys):
    analyzer = _load_analyzer_module()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert analyzer.main([str(empty)]) == 2
    assert "no observer output" in capsys.readouterr().err
