"""Advisory run lock for ``kct route`` outputs (issue #9628)."""

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time

import pytest

from kicad_tools.cli import route_deadline
from kicad_tools.core import route_run_lock as lock_mod
from kicad_tools.core.route_run_lock import (
    LOCK_HELD_ENV,
    LOCK_POLICY_ENV,
    RouteLockError,
    acquire_route_lock,
    extend_lock_owner,
    held_env_value,
    lock_path_for,
    release_route_lock,
    route_run_lock,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(LOCK_POLICY_ENV, raising=False)
    monkeypatch.delenv(LOCK_HELD_ENV, raising=False)
    lock_mod._held.clear()
    yield
    lock_mod._held.clear()


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _write_lock(output, **fields):
    record = {"pid": os.getpid(), "hostname": socket.gethostname(), "started_at": "t"}
    record.update(fields)
    path = lock_path_for(output)
    path.write_text(json.dumps(record))
    return path


def _foreign_live_pid_lock(output):
    # A live pid that is not ours: our parent.
    return _write_lock(output, pid=os.getppid())


def test_lock_name_is_distinct_from_kicad_marker(tmp_path):
    out = tmp_path / "board_routed.kicad_pcb"
    assert lock_path_for(out) == tmp_path / ".board_routed.kicad_pcb.kct-route.lock"


def test_acquire_release_roundtrip(tmp_path):
    out = tmp_path / "b.kicad_pcb"
    handle = acquire_route_lock(out)
    assert handle.acquired
    data = json.loads(handle.path.read_text())
    assert data["pid"] == os.getpid() and data["hostname"] == socket.gethostname()
    release_route_lock(handle)
    assert not handle.path.exists()


def test_overlapping_acquire_refuses_and_leaves_output(tmp_path):
    out = tmp_path / "b.kicad_pcb"
    out.write_bytes(b"original")
    lock = _foreign_live_pid_lock(out)
    before = lock.read_bytes()
    with pytest.raises(RouteLockError, match=str(lock.name)):
        acquire_route_lock(out)
    assert out.read_bytes() == b"original"
    assert lock.read_bytes() == before


def test_refusal_names_holder(tmp_path):
    out = tmp_path / "b.kicad_pcb"
    _foreign_live_pid_lock(out)
    with pytest.raises(RouteLockError) as info:
        acquire_route_lock(out)
    assert f"pid {os.getppid()}" in str(info.value)
    assert socket.gethostname() in str(info.value)


def test_dead_pid_lock_is_stolen(tmp_path):
    out = tmp_path / "b.kicad_pcb"
    _write_lock(out, pid=_dead_pid())
    handle = acquire_route_lock(out)
    assert handle.acquired
    assert json.loads(handle.path.read_text())["pid"] == os.getpid()


def test_foreign_host_lock_is_refused(tmp_path):
    out = tmp_path / "b.kicad_pcb"
    lock = _write_lock(out, pid=_dead_pid(), hostname="some-other-host")
    with pytest.raises(RouteLockError):
        acquire_route_lock(out)
    assert json.loads(lock.read_text())["hostname"] == "some-other-host"


@pytest.mark.parametrize("content", ["", "not json", "[1]", '{"pid": "x"}', '{"pid": 1}'])
def test_malformed_lock_is_refused_not_stolen(tmp_path, content):
    out = tmp_path / "b.kicad_pcb"
    lock = lock_path_for(out)
    lock.write_text(content)
    with pytest.raises(RouteLockError):
        acquire_route_lock(out)
    assert lock.read_text() == content


def test_relative_and_absolute_paths_share_a_lock(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    handle = acquire_route_lock("b.kicad_pcb")
    with pytest.raises(RouteLockError):
        # Different process identity needed, otherwise reentrancy applies.
        lock_mod._held.clear()
        _write_lock(tmp_path / "b.kicad_pcb", pid=os.getppid())
        acquire_route_lock(str(tmp_path / "b.kicad_pcb"))
    assert handle.path == lock_path_for(tmp_path / "b.kicad_pcb")


def test_reentrant_acquire_in_one_process_is_noop(tmp_path):
    out = tmp_path / "b.kicad_pcb"
    outer = acquire_route_lock(out)
    inner = acquire_route_lock(out)
    assert outer.acquired and not inner.acquired
    release_route_lock(inner)
    assert outer.path.exists()
    release_route_lock(outer)
    assert not outer.path.exists()


def test_context_manager_releases_on_exception(tmp_path):
    out = tmp_path / "b.kicad_pcb"
    with pytest.raises(RuntimeError, match="boom"):
        with route_run_lock(out) as handle:
            assert handle.path.exists()
            raise RuntimeError("boom")
    assert not lock_path_for(out).exists()


def test_release_leaves_a_lock_someone_else_took(tmp_path):
    out = tmp_path / "b.kicad_pcb"
    handle = acquire_route_lock(out)
    handle.path.write_text(
        json.dumps({"pid": os.getppid(), "hostname": socket.gethostname(), "started_at": "x"})
    )
    release_route_lock(handle)
    assert handle.path.exists()


def test_child_with_env_for_live_parent_is_noop(tmp_path, monkeypatch):
    out = tmp_path / "b.kicad_pcb"
    lock = _write_lock(out, pid=os.getppid())
    monkeypatch.setenv(LOCK_HELD_ENV, held_env_value(lock, os.getppid()))
    handle = acquire_route_lock(out)
    assert not handle.acquired and handle.path == lock
    release_route_lock(handle)
    assert lock.exists()


def test_env_naming_another_path_or_pid_does_not_bypass(tmp_path, monkeypatch):
    out = tmp_path / "b.kicad_pcb"
    lock = _write_lock(out, pid=os.getppid())
    monkeypatch.setenv(LOCK_HELD_ENV, held_env_value(lock, os.getppid() + 1))
    with pytest.raises(RouteLockError):
        acquire_route_lock(out)
    monkeypatch.setenv(LOCK_HELD_ENV, held_env_value(tmp_path / "other.lock", os.getppid()))
    with pytest.raises(RouteLockError):
        acquire_route_lock(out)


@pytest.mark.parametrize(
    "policy,expect_refuse,expect_warn",
    [("error", True, False), ("warn", False, True), ("ignore", False, False)],
)
def test_policies(tmp_path, monkeypatch, capsys, policy, expect_refuse, expect_warn):
    out = tmp_path / "b.kicad_pcb"
    lock = _foreign_live_pid_lock(out)
    before = lock.read_bytes()
    monkeypatch.setenv(LOCK_POLICY_ENV, policy)
    if expect_refuse:
        with pytest.raises(RouteLockError):
            acquire_route_lock(out)
    else:
        handle = acquire_route_lock(out)
        assert not handle.acquired
    assert ("Warning:" in capsys.readouterr().err) == expect_warn
    assert lock.read_bytes() == before


def test_ignore_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv(LOCK_POLICY_ENV, "ignore")
    acquire_route_lock(tmp_path / "b.kicad_pcb")
    assert not list(tmp_path.glob(".*"))


def test_invalid_policy_raises_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv(LOCK_POLICY_ENV, "bogus")
    with pytest.raises(ValueError, match=LOCK_POLICY_ENV):
        acquire_route_lock(tmp_path / "b.kicad_pcb")


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX non-root")
def test_unwritable_directory_is_a_clear_error(tmp_path, monkeypatch, capsys):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        with pytest.raises(RouteLockError, match="cannot create route lock"):
            acquire_route_lock(ro / "b.kicad_pcb")
        monkeypatch.setenv(LOCK_POLICY_ENV, "warn")
        assert not acquire_route_lock(ro / "b.kicad_pcb").acquired
        assert "cannot create route lock" in capsys.readouterr().err
    finally:
        ro.chmod(0o700)


def test_missing_output_directory_is_created(tmp_path):
    out = tmp_path / "new" / "dir" / "b.kicad_pcb"
    handle = acquire_route_lock(out)
    assert handle.path.exists()
    release_route_lock(handle)


# --- run_attempt integration -------------------------------------------------


def _patch_in_process(monkeypatch, seen):
    from kicad_tools.cli import route_cmd

    def fake(argv):
        seen.append(sorted(p.name for p in lock_mod._held))
        return 0

    monkeypatch.setattr(route_cmd, "_in_process_main", fake)


def test_run_attempt_holds_lock_during_run_and_releases(tmp_path, monkeypatch):
    seen = []
    _patch_in_process(monkeypatch, seen)
    out = tmp_path / "o.kicad_pcb"
    result = route_deadline.run_attempt([str(tmp_path / "in.kicad_pcb"), "-o", str(out)])
    assert result.exit_code == 0
    assert seen == [[lock_path_for(out).name]]
    assert not lock_path_for(out).exists()


def test_run_attempt_default_output_name(tmp_path, monkeypatch):
    seen = []
    _patch_in_process(monkeypatch, seen)
    route_deadline.run_attempt([str(tmp_path / "in.kicad_pcb")])
    assert seen == [[".in_routed.kicad_pcb.kct-route.lock"]]


def test_run_attempt_releases_on_exception(tmp_path, monkeypatch):
    from kicad_tools.cli import route_cmd

    def boom(argv):
        raise RuntimeError("boom")

    monkeypatch.setattr(route_cmd, "_in_process_main", boom)
    out = tmp_path / "o.kicad_pcb"
    with pytest.raises(RuntimeError):
        route_deadline.run_attempt([str(tmp_path / "in.kicad_pcb"), "-o", str(out)])
    assert not lock_path_for(out).exists()


def test_run_attempt_refuses_overlap_and_never_runs(tmp_path, monkeypatch, capsys):
    seen = []
    _patch_in_process(monkeypatch, seen)
    out = tmp_path / "o.kicad_pcb"
    out.write_bytes(b"original")
    _foreign_live_pid_lock(out)
    result = route_deadline.run_attempt([str(tmp_path / "in.kicad_pcb"), "-o", str(out)])
    assert result.exit_code == 1
    assert seen == []
    assert out.read_bytes() == b"original"
    assert "another kct route run" in capsys.readouterr().err


def test_run_attempt_invalid_policy_is_exit_1(tmp_path, monkeypatch, capsys):
    seen = []
    _patch_in_process(monkeypatch, seen)
    monkeypatch.setenv(LOCK_POLICY_ENV, "bogus")
    result = route_deadline.run_attempt([str(tmp_path / "in.kicad_pcb")])
    assert result.exit_code == 1 and seen == []
    assert LOCK_POLICY_ENV in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv", [["--help"], ["-h"], ["--bogus-flag"], ["in.kicad_pcb", "--dry-run"]]
)
def test_no_lock_for_help_bad_args_or_dry_run(tmp_path, monkeypatch, argv):
    seen = []
    _patch_in_process(monkeypatch, seen)
    monkeypatch.chdir(tmp_path)
    route_deadline.run_attempt(argv)
    assert seen == [[]]
    assert not list(tmp_path.glob(".*.lock"))


def test_supervised_run_holds_lock_exports_env_and_releases(tmp_path, monkeypatch):
    out = tmp_path / "o.kicad_pcb"
    observed = {}

    def fake_supervise(command, budget, control, **kwargs):
        lock = lock_path_for(out)
        observed["exists"] = lock.exists()
        observed["env"] = held_env_value()
        return 0

    monkeypatch.setattr(route_deadline, "_supervise", fake_supervise)
    result = route_deadline.run_attempt(
        [str(tmp_path / "in.kicad_pcb"), "-o", str(out), "--timeout", "30"]
    )
    assert result.exit_code == 0
    assert observed["exists"] is True
    assert observed["env"] == f"{lock_path_for(out)}:{os.getpid()}"
    assert not lock_path_for(out).exists()


def test_supervise_exports_held_lock_to_worker(tmp_path):
    out = tmp_path / "o.kicad_pcb"
    probe = tmp_path / "env.txt"
    command = [
        sys.executable,
        "-c",
        f"import os; open({str(probe)!r}, 'w').write(os.environ.get({LOCK_HELD_ENV!r}, ''))",
    ]
    with route_run_lock(out):
        route_deadline._supervise(command, 30, tmp_path / "control.json")
    assert probe.read_text() == f"{lock_path_for(out)}:{os.getpid()}"


def test_supervised_timeout_releases_lock(tmp_path):
    """A real worker that outlives ``--timeout``: lock is gone afterwards."""
    out = tmp_path / "o.kicad_pcb"
    control = tmp_path / "control.json"
    control.write_text(json.dumps({"stage": "startup"}))
    with route_run_lock(out):
        code = route_deadline._supervise(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            0.2,
            control,
            save_seconds=0.2,
        )
        assert lock_path_for(out).exists()
    assert code == route_deadline.TIMEOUT_EXIT
    assert not lock_path_for(out).exists()


def test_sigterm_to_supervisor_releases_lock_and_stops_worker(tmp_path):
    """SIGTERM on a supervised run unwinds like Ctrl-C: worker killed, lock removed."""
    if os.name != "posix":
        pytest.skip("POSIX signals")
    out = tmp_path / "o.kicad_pcb"
    script = tmp_path / "driver.py"
    script.write_text(
        "import sys, time\n"
        "from pathlib import Path\n"
        "from kicad_tools.cli import route_deadline as rd\n"
        "out = Path(sys.argv[1])\n"
        "ctl = out.parent / 'control.json'\n"
        "ctl.write_text('{}')\n"
        "def body():\n"
        "    print('ready', flush=True)\n"
        "    return rd._supervise([sys.executable, '-c', 'import time; time.sleep(60)'], 60, ctl)\n"
        "print(rd._with_run_lock(out, body))\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-I", str(script), str(out)],
        stdout=subprocess.PIPE,
        text=True,
        env=dict(os.environ),
    )
    try:
        # -I drops cwd/PYTHONPATH from sys.path, so rely on the installed package.
        assert proc.stdout.readline().strip() == "ready"
        assert lock_path_for(out).exists()
        proc.terminate()
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert not lock_path_for(out).exists()


# --- issue #9628 review: dry-run timeouts, ownership and recovery ------------


def _try_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def test_timed_dry_run_leaves_preexisting_board_and_live_lock(tmp_path):
    """A --dry-run with --timeout quarantines nothing (issue #9628 review).

    The dry run takes no lock, so a canonical board that another run owns (its
    live lock planted here) must survive a timeout byte-identically, with no
    ``*_timeout_unverified_*`` sibling.
    """
    out = tmp_path / "o.kicad_pcb"
    out.write_bytes(b"canonical from another run")
    out.with_suffix(".kicad_pro").write_text("pro")
    lock = _foreign_live_pid_lock(out)
    before = lock.read_bytes()
    result = route_deadline.run_attempt(
        [str(tmp_path / "in.kicad_pcb"), "-o", str(out), "--dry-run", "--timeout", "0.001"]
    )
    assert result.exit_code == route_deadline.TIMEOUT_EXIT
    assert out.read_bytes() == b"canonical from another run"
    assert out.with_suffix(".kicad_pro").read_text() == "pro"
    assert lock.read_bytes() == before
    assert not list(tmp_path.glob("*_timeout_unverified_*"))
    report = json.loads(out.with_suffix(".timeout.json").read_text())
    assert "unverified_output" not in report and "snapshot" not in report


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_lock_outlives_supervisor_until_worker_group_exits(tmp_path):
    """A killed supervisor's live worker keeps the lock held (#9628 review)."""
    out = tmp_path / "o.kicad_pcb"
    lock = lock_path_for(out)
    script = tmp_path / "driver.py"
    script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "from kicad_tools.cli import route_deadline as rd\n"
        "out = Path(sys.argv[1])\n"
        "ctl = out.parent / 'control.json'\n"
        "ctl.write_text('{}')\n"
        "def body():\n"
        "    return rd._supervise([sys.executable, '-c', 'import time; time.sleep(60)'], 60, ctl)\n"
        "rd._with_run_lock(out, body)\n"
    )
    proc = subprocess.Popen([sys.executable, "-I", str(script), str(out)])
    pgid = None
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            holder = _try_json(lock)
            if holder and holder.get("owner_pgid"):
                pgid = holder["owner_pgid"]
                break
            time.sleep(0.02)
        assert pgid, "lock never named the worker group as co-owner"
        proc.kill()  # abrupt supervisor death after worker launch
        proc.wait(timeout=30)  # ... and reaped
        with pytest.raises(RouteLockError):
            acquire_route_lock(out)  # the live worker group keeps it held
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)
        if pgid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)
    # With every process able to write the output gone, it is stealable again.
    handle = None
    deadline = time.monotonic() + 10
    while handle is None:
        try:
            handle = acquire_route_lock(out)
        except RouteLockError:
            if time.monotonic() > deadline:
                pytest.fail("lock stayed live after the worker group died")
            time.sleep(0.05)
    release_route_lock(handle)
    assert not lock.exists()


def test_extend_lock_owner_keeps_dead_pid_lock_live(tmp_path):
    out = tmp_path / "b.kicad_pcb"
    handle = acquire_route_lock(out)
    worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    try:
        extend_lock_owner(handle.path, pgid=worker.pid)
        record = json.loads(handle.path.read_text())
        assert record["owner_pgid"] == worker.pid
        dead_owner = {
            "pid": _dead_pid(),
            "hostname": record["hostname"],
            "started_at": "t",
            "owner_pgid": worker.pid,
        }
        assert not lock_mod._holder_is_dead(dead_owner)
        release_route_lock(handle)  # extended record is still ours to remove
        assert not handle.path.exists()
    finally:
        worker.kill()
        worker.wait()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_worker_names_its_group_on_inherited_lock(tmp_path):
    """The worker itself covers a supervisor that died before extending."""
    out = tmp_path / "o.kicad_pcb"
    probe = tmp_path / "extended"
    with route_run_lock(out):
        env = dict(os.environ, **{LOCK_HELD_ENV: held_env_value()})
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from kicad_tools.cli.route_deadline import _extend_inherited_lock_owner\n"
                "_extend_inherited_lock_owner()\n"
                f"from pathlib import Path; Path({str(probe)!r}).write_text('x')\n"
                "import time; time.sleep(30)\n",
            ],
            env=env,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not probe.exists():
                time.sleep(0.02)
            assert probe.exists(), "worker never ran the extension"
            record = json.loads(lock_path_for(out).read_text())
            assert record["owner_pgid"] == proc.pid
        finally:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=30)
    assert not lock_path_for(out).exists()


@pytest.mark.skipif(os.name != "posix", reason="flock-serialized stale recovery")
def test_stale_recovery_is_serialized_across_processes(tmp_path):
    """Exactly one contender may steal one stale lock (issue #9628 review)."""
    out = tmp_path / "o.kicad_pcb"
    _write_lock(out, pid=_dead_pid())
    total = 12
    ready = tmp_path / "ready"
    ready.mkdir()
    results = tmp_path / "results"
    results.mkdir()
    go = tmp_path / "go"
    contender = """
import sys, time
from pathlib import Path
from kicad_tools.core.route_run_lock import acquire_route_lock, release_route_lock

out, ready, results, go = (Path(p) for p in sys.argv[1:5])
total = int(sys.argv[5])
name = sys.argv[6]
(ready / name).write_text("")
while len(list(ready.iterdir())) < int(total) and not go.exists():
    time.sleep(0.002)
try:
    handle = acquire_route_lock(out)
except Exception:
    (results / name).write_text("refused")
    sys.exit(0)
(results / name).write_text("acquired")
while not go.exists():
    time.sleep(0.002)
release_route_lock(handle)
"""
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                contender,
                str(out),
                str(ready),
                str(results),
                str(go),
                str(total),
                str(index),
            ]
        )
        for index in range(total)
    ]
    try:
        deadline = time.monotonic() + 60
        while len(list(results.iterdir())) < total and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(list(results.iterdir())) == total, "contenders did not finish"
        verdicts = [path.read_text() for path in sorted(results.iterdir())]
        assert verdicts.count("acquired") == 1
        go.write_text("")
        for proc in procs:
            proc.wait(timeout=30)
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
    assert not lock_path_for(out).exists()


def test_nested_path_alias_shares_outer_lock_and_keeps_it(tmp_path, monkeypatch):
    """``out`` and ``sub/../out`` are one lock; the alias cannot steal (review)."""
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path)
    assert lock_path_for("sub/../b.kicad_pcb") == lock_path_for("b.kicad_pcb")
    outer = acquire_route_lock("b.kicad_pcb")
    inner = acquire_route_lock("sub/../b.kicad_pcb")
    assert outer.acquired and not inner.acquired
    assert inner.path == outer.path
    release_route_lock(inner)
    assert outer.path.exists()
    # A separate process still refuses while the outer scope holds the lock.
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "from kicad_tools.core.route_run_lock import acquire_route_lock\n"
            "try:\n"
            "    acquire_route_lock(sys.argv[1])\n"
            "except Exception:\n"
            "    sys.exit(3)\n"
            "sys.exit(0)\n",
            str(tmp_path / "b.kicad_pcb"),
        ],
        timeout=60,
    )
    assert probe.returncode == 3
    release_route_lock(outer)
    assert not outer.path.exists()


def test_early_sigterm_to_unsupervised_run_releases_lock(tmp_path):
    """SIGTERM between acquisition and the routing handler cleans up (#9628 review)."""
    if os.name != "posix":
        pytest.skip("POSIX signals")
    out = tmp_path / "o.kicad_pcb"
    script = tmp_path / "driver.py"
    script.write_text(
        "import sys, time\n"
        "from pathlib import Path\n"
        "from kicad_tools.cli import route_deadline as rd\n"
        "out = Path(sys.argv[1])\n"
        "def body():\n"
        "    print('ready', flush=True)\n"
        "    time.sleep(60)\n"
        "    return 0\n"
        "sys.exit(rd._with_run_lock(out, body).exit_code)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-I", str(script), str(out)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "ready"
        assert lock_path_for(out).exists()  # the pid record is in place
        proc.terminate()  # ... and SIGTERM arrives before any routing handler
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 130
    assert not lock_path_for(out).exists()


@pytest.mark.parametrize("pid", [2**32, -(2**31) - 1])
def test_unrepresentable_pid_is_malformed_holder(tmp_path, monkeypatch, capsys, pid):
    """An out-of-range pid must not escape the policy (issue #9628 review)."""
    out = tmp_path / "b.kicad_pcb"
    lock = _write_lock(out, pid=pid)
    before = lock.read_bytes()
    with pytest.raises(RouteLockError):
        acquire_route_lock(out)
    monkeypatch.setenv(LOCK_POLICY_ENV, "warn")
    assert not acquire_route_lock(out).acquired
    assert "Warning:" in capsys.readouterr().err
    assert lock.read_bytes() == before
