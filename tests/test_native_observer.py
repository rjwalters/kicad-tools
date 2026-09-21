"""Diagnostics controls; no real KiCad or production scheduling is modified."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts/ci"
spec = importlib.util.spec_from_file_location("observer_under_test", SCRIPTS / "native_observer.py")
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)


def process(root, pid, parent, tick, argv, owner, token="run", rss=10):
    directory = root / str(pid)
    directory.mkdir(exist_ok=True)
    fields = ["S", str(parent)] + ["0"] * 22
    fields[19], fields[21] = str(tick), str(rss)
    (directory / "stat").write_text(f"{pid} (name with ) paren) " + " ".join(fields))
    (directory / "cmdline").write_bytes(b"\0".join(x.encode() for x in argv) + b"\0")
    (directory / "environ").write_bytes(
        f"KCT_NATIVE_OBSERVER_TOKEN={token}\0PYTEST_CURRENT_TEST={owner}\0PYTEST_XDIST_WORKER=gw2\0SECRET=never-record\0".encode()
    )
    return directory


def test_nested_native_ownership_survives_parent_death_and_pid_reuse(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    process(
        proc,
        10,
        1,
        100,
        ["python", "-m", "kicad_tools.cli", "--password", "never-record"],
        "test.py::test_a (setup)",
    )
    native = process(
        proc, 11, 10, 110, ["kicad-cli", "pcb", "drc", "never-record"], "test.py::test_a (setup)"
    )
    process(
        proc,
        12,
        10,
        120,
        ["python3", "/x/_fixed_fill_worker.py", "never-record"],
        "test.py::test_a (setup)",
    )
    records, errors = observer.snapshot(proc, "run")
    assert errors == 0
    assert {r["category"] for r in records} == {"python-kct", "kicad-cli", "kicad-python-fill"}
    assert all(r["owner"] == "test.py::test_a (setup)" and r["worker"] == "gw2" for r in records)
    recorder = observer.Recorder(tmp_path / "samples.jsonl")
    recorder.sample(records)
    # Reparent old child and reuse former parent's PID for another test.
    process(proc, 11, 1, 110, ["kicad-cli"], "test.py::test_a (setup)", rss=15)
    process(proc, 10, 1, 200, ["python"], "test.py::test_b (call)")
    recorder.sample(observer.snapshot(proc, "run")[0])
    assert recorder.seen[(11, 110)]["owner"] == "test.py::test_a (setup)"
    assert recorder.seen[(11, 110)]["sampled_peak_rss_bytes"] == 15 * os.sysconf("SC_PAGE_SIZE")
    assert recorder.seen[(10, 200)]["owner"] == "test.py::test_b (call)"
    data = (tmp_path / "samples.jsonl").read_text()
    assert "never-record" not in data
    events = [json.loads(x) for x in data.splitlines()]
    assert any(
        e["event"] == "process_unobserved" and e["start_ticks"] == 100 and e["exit_code"] is None
        for e in events
    )
    # Process races are missing observations, never fabricated zero memory.
    (native / "stat").unlink()
    assert observer.snapshot(proc, "run")[1] == 1


def test_cgroup_namespace_mapping_and_missing_counters(tmp_path):
    proc = tmp_path / "proc/self"
    proc.mkdir(parents=True)
    mount = tmp_path / "cg"
    mount.mkdir()
    (proc / "cgroup").write_text("0::/docker/job\n")
    (proc / "mountinfo").write_text(f"1 2 0:1 /docker/job {mount} rw - cgroup2 cgroup rw\n")
    assert observer.cgroup_directory(proc.parent) == mount
    for name, content in {
        "memory.current": "42",
        "memory.max": "12884901888",
        "memory.events": "oom 2\noom_kill 1",
    }.items():
        (mount / name).write_text(content)
    data = observer.memory(mount)
    assert data["memory.max"] == 12 * 1024**3
    assert data["memory.events"] == {"oom": 2, "oom_kill": 1}
    assert data["memory.peak"] is None
    assert observer.memory(None)["available"] is False
    (proc / "cgroup").write_text("5:memory:/docker/job\n")
    assert observer.cgroup_directory(proc.parent) is None


def test_plugin_records_fast_launches_phases_failures_and_redacts(tmp_path):
    output = tmp_path / "events"
    output.mkdir()
    (tmp_path / "test_fixture.py").write_text("""
import os, subprocess, sys, pytest
@pytest.fixture
def resource():
    subprocess.run([sys.executable, "-c", "pass", "secret-argument"], check=True)
    yield
    subprocess.run([sys.executable, "-c", "pass"], check=True)
@pytest.mark.parametrize("value", ["secret-parameter"])
def test_probe(resource, value):
    p = subprocess.run([sys.executable, "-c", "raise SystemExit(7)"], env={"PATH": os.environ["PATH"]})
    assert p.returncode == 7
    assert False, "intentional failure"
""")
    env = dict(
        os.environ,
        PYTHONPATH=str(SCRIPTS),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        KCT_NATIVE_OBSERVER_TOKEN="run",
        KCT_NATIVE_OBSERVER_OUTPUT=str(output),
        PYTEST_XDIST_WORKER="gw3",
    )
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "native_observer_pytest", "-q", "test_fixture.py"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1, completed.stdout + completed.stderr
    data = "".join(p.read_text() for p in output.glob("*.jsonl"))
    assert "secret-argument" not in data and "secret-parameter" not in data
    events = [json.loads(line) for line in data.splitlines()]
    launches = [e for e in events if e["event"] == "launch_attempt"]
    assert len(launches) == 3
    assert [e["owner"].rsplit(" ", 1)[-1] for e in launches] == ["(setup)", "(call)", "(teardown)"]
    assert all(e["worker"] == "gw3" for e in launches)
    assert [e["child_tracking_inherited"] for e in launches] == [True, False, True]
    reports = [e for e in events if e["event"] == "test_report"]
    assert [(e["phase"], e["outcome"]) for e in reports] == [
        ("setup", "passed"),
        ("call", "failed"),
        ("teardown", "passed"),
    ]


def test_owner_parameter_ids_are_redacted():
    assert "password" not in observer.owner_id("test.py::test_x[password] (call)")
    assert observer.owner_id(None) is None


@pytest.mark.parametrize(
    "program,expected",
    [("raise SystemExit(7)", 7), ("import os, signal; os.kill(os.getpid(), signal.SIGKILL)", 137)],
)
def test_observer_preserves_child_failure_and_retains_terminal_memory(
    tmp_path, monkeypatch, program, expected
):
    # Exercise the real supervisory subprocess on every platform; only the
    # Linux kernel observation layer is replaced with deterministic samples.
    original_exists = Path.exists
    monkeypatch.setattr(
        Path, "exists", lambda p: True if str(p) == "/proc/self/stat" else original_exists(p)
    )
    monkeypatch.setattr(observer, "snapshot", lambda proc, token: ([], 0))
    monkeypatch.setattr(observer, "cgroup_directory", lambda: None)
    out = tmp_path / "retained"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "observer",
            "--output",
            str(out),
            "--interval",
            "0.01",
            "--",
            sys.executable,
            "-c",
            program,
        ],
    )
    assert observer.main() == expected
    rows = [json.loads(x) for x in (out / "observer.jsonl").read_text().splitlines()]
    assert rows[-1]["exit_code"] == (expected if expected == 7 else -9)
    assert rows[-1]["memory_events_delta"] is None
    assert rows[0]["memory"]["available"] is False
    # Never silently reuse an earlier run's diagnostics.
    with pytest.raises(FileExistsError):
        observer.main()


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="Linux /proc integration")
def test_linux_observer_owns_direct_nested_and_fill_children(tmp_path):
    out = tmp_path / "retained"
    native = tmp_path / "kicad-cli"
    native.symlink_to(sys.executable)
    fill = tmp_path / "_fixed_fill_worker.py"
    fill.write_text("import time; time.sleep(0.8)\n")
    (tmp_path / "test_native.py").write_text(f"""
import subprocess, sys

def test_children():
    direct = subprocess.Popen([{str(native)!r}, "-c", "import time; time.sleep(0.8)"])
    fill = subprocess.Popen([sys.executable, {str(fill)!r}])
    nested_code = "import subprocess; subprocess.run([" + repr({str(native)!r}) + ", '-c', 'import time; time.sleep(0.8)'])"
    nested = subprocess.Popen([sys.executable, "-c", nested_code])
    assert direct.wait() == fill.wait() == nested.wait() == 0
""")
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "native_observer.py"),
            "--output",
            str(out),
            "--interval",
            "0.02",
            "--",
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "test_native.py",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    rows = [json.loads(x) for x in (out / "processes.jsonl").read_text().splitlines()]
    native_rows = [r for r in rows if r.get("category") == "kicad-cli"]
    assert len({r["pid"] for r in native_rows}) == 2
    assert all(r["owner"] == "test_native.py::test_children (call)" for r in native_rows)
    assert any(
        r.get("category") == "kicad-python-fill"
        and r["owner"] == "test_native.py::test_children (call)"
        for r in rows
    )


def test_four_workers_keep_distinct_launch_ownership(tmp_path):
    output = tmp_path / "events"
    output.mkdir()
    (tmp_path / "test_parallel.py").write_text("""
import subprocess, sys, pytest
@pytest.mark.parametrize("case", range(12))
def test_native(case):
    subprocess.run([sys.executable, "-c", "import time; time.sleep(.03)"], check=True)
""")
    env = dict(
        os.environ,
        PYTHONPATH=str(SCRIPTS),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        KCT_NATIVE_OBSERVER_TOKEN="run",
        KCT_NATIVE_OBSERVER_OUTPUT=str(output),
    )
    env.pop("PYTEST_XDIST_WORKER", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "xdist.plugin",
            "-p",
            "native_observer_pytest",
            "-n",
            "4",
            "-q",
            "test_parallel.py",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    events = [
        json.loads(line) for p in output.glob("*.jsonl") for line in p.read_text().splitlines()
    ]
    launches = [
        e
        for e in events
        if e["event"] == "launch_attempt"
        and e.get("owner")
        and e["owner"].startswith("test_parallel.py::test_native[")
    ]
    assert len(launches) == 12
    assert len({e["owner"] for e in launches}) == 12
    assert {e["worker"] for e in launches} == {"gw0", "gw1", "gw2", "gw3"}


def test_embedded_pytest_does_not_leave_active_audit_hooks(tmp_path):
    output = tmp_path / "events"
    output.mkdir()
    (tmp_path / "test_embedded.py").write_text(
        "import subprocess, sys\ndef test_run():\n    subprocess.run([sys.executable, '-c', 'pass'], check=True)\n"
    )
    program = """
import pytest, subprocess, sys
for iteration in range(2):
    assert pytest.main(['-p', 'native_observer_pytest', '-q', 'test_embedded.py']) == 0
    subprocess.run([sys.executable, '-c', 'pass'], check=True)
"""
    env = dict(
        os.environ,
        PYTHONPATH=str(SCRIPTS),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        KCT_NATIVE_OBSERVER_TOKEN="run",
        KCT_NATIVE_OBSERVER_OUTPUT=str(output),
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    rows = [json.loads(x) for p in output.glob("*.jsonl") for x in p.read_text().splitlines()]
    assert len([r for r in rows if r["event"] == "launch_attempt"]) == 2


def test_pid_reuse_during_sample_is_unavailable_not_misattributed(tmp_path, monkeypatch):
    directory = process(tmp_path, 11, 10, 100, ["kicad-cli"], "test.py::test_old (call)")
    original = Path.read_text
    calls = 0

    def raced_read(path, *args, **kwargs):
        nonlocal calls
        value = original(path, *args, **kwargs)
        if path == directory / "stat":
            calls += 1
            if calls > 1:
                return value.replace(" 100 ", " 200 ")
        return value

    monkeypatch.setattr(Path, "read_text", raced_read)
    with pytest.raises(OSError, match="PID changed"):
        observer.read_process(directory, "run")


@pytest.mark.parametrize("failure", ["sample_write", "sample_read", "terminal_write"])
def test_postlaunch_diagnostic_failure_waits_for_real_child(tmp_path, monkeypatch, capsys, failure):
    import errno

    original_exists = Path.exists
    monkeypatch.setattr(
        Path, "exists", lambda p: True if str(p) == "/proc/self/stat" else original_exists(p)
    )
    monkeypatch.setattr(observer, "cgroup_directory", lambda: None)
    finished = tmp_path / "child-finished"
    failed = False
    failed_before_completion = False
    original_append = observer.append

    def fail_sample_read(proc, token):
        nonlocal failed, failed_before_completion
        if failure == "sample_read":
            failed = True
            failed_before_completion = not finished.exists()
            raise OSError(errno.EIO, "secret-path-must-not-appear")
        return [], 0

    def fail_write(path, event):
        nonlocal failed, failed_before_completion
        target = "memory_sample" if failure == "sample_write" else "finish"
        if failure != "sample_read" and event["event"] == target:
            failed = True
            if failure == "sample_write":
                failed_before_completion = not finished.exists()
            raise OSError(errno.ENOSPC, "secret-path-must-not-appear")
        original_append(path, event)

    monkeypatch.setattr(observer, "snapshot", fail_sample_read)
    monkeypatch.setattr(observer, "append", fail_write)
    out = tmp_path / "retained"
    program = (
        "import time; from pathlib import Path; time.sleep(.15); "
        f"Path({str(finished)!r}).write_text('done'); raise SystemExit(7)"
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "observer",
            "--output",
            str(out),
            "--interval",
            "0.01",
            "--",
            sys.executable,
            "-c",
            program,
        ],
    )
    assert observer.main() == 7
    assert failed and finished.read_text() == "done"
    stderr = capsys.readouterr().err
    assert "diagnostics are incomplete" in stderr
    assert "secret-path-must-not-appear" not in stderr
    events = [json.loads(x) for x in (out / "observer.jsonl").read_text().splitlines()]
    if failure != "terminal_write":
        assert failed_before_completion
        assert events[-1]["event"] == "finish"
        assert events[-1]["exit_code"] == 7
        assert events[-1]["diagnostics_degraded"] is True
    else:
        assert not any(e["event"] == "finish" for e in events)


def test_startup_diagnostic_failure_does_not_launch_workload(tmp_path, monkeypatch):
    original_exists = Path.exists
    monkeypatch.setattr(
        Path, "exists", lambda p: True if str(p) == "/proc/self/stat" else original_exists(p)
    )
    monkeypatch.setattr(observer, "cgroup_directory", lambda: None)

    def fail_append(*args):
        raise OSError("startup storage unavailable")

    def forbidden_launch(*args, **kwargs):
        pytest.fail("workload launched before startup diagnostics succeeded")

    monkeypatch.setattr(observer, "append", fail_append)
    monkeypatch.setattr(observer.subprocess, "Popen", forbidden_launch)
    monkeypatch.setattr(
        sys,
        "argv",
        ["observer", "--output", str(tmp_path / "new"), "--", sys.executable, "-c", "pass"],
    )
    with pytest.raises(OSError, match="startup storage"):
        observer.main()
