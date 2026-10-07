"""``kct route --lint-gate`` under SIGTERM / SIGKILL (Issue #6090).

The gate used to judge ``--output`` in place, so a kill outside the windows
that convert SIGTERM into an exception (``timeout``, a CI job cancellation)
left an unjudged board at ``--output``.  Routing now writes a staging file
next to ``--output`` and the judged board is promoted with ``os.replace``, so
``--output`` is either its pre-run bytes or absent -- never an unjudged board
-- whatever signal ends the run.

The signal tests run the real CLI in a subprocess with the router (or the
candidate lint) replaced by a stub that writes a board and then blocks, so the
signal lands exactly where the issue's repro put it.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.cli import main as kct_main
from kicad_tools.cli import route_cmd
from kicad_tools.cli.route_lint_gate import (
    LINT_GATE_EXIT,
    SIGTERM_EXIT,
    LintGate,
    _raise_terminated,
)

FIXTURE = Path(__file__).parent / "fixtures" / "projects"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")

# A GND trace crossing the NET1 trace: two new clearance errors.
SHORT = (
    '\t(segment (start 110 48) (end 110 52) (width 0.25) (layer "F.Cu") (net 2) (uuid "seed-1"))\n'
)

_DRIVER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from unittest.mock import patch

    from kicad_tools.cli import check_cmd, route_cmd
    from kicad_tools.cli import main as kct_main

    mode, ready = sys.argv[1], Path(sys.argv[2])
    argv = sys.argv[3:]
    SEG = {seg!r}

    def fake_route(args, parser, argv_):
        src, out = Path(args.pcb), Path(args.output)
        text = src.read_text()
        i = text.rstrip().rfind(")")
        out.write_text(text[:i] + SEG + text[i:])
        if mode == "route":
            ready.write_text(str(out))
            time.sleep(300)
        return 0

    real_check = check_cmd.main

    def slow_check(a):
        if mode == "lint" and ".lint-gate-" in a[0]:
            ready.write_text(a[0])
            time.sleep(300)
        return real_check(a)

    with patch.object(route_cmd, "_run_main_impl", fake_route), patch.object(
        check_cmd, "main", slow_check
    ):
        sys.exit(kct_main(argv))
    """
).format(seg=SHORT)


@pytest.fixture
def board(tmp_path: Path) -> Path:
    for name in ("test_project.kicad_pcb", "test_project.kicad_pro"):
        shutil.copy(FIXTURE / name, tmp_path / name)
    return tmp_path / "test_project.kicad_pcb"


def _staging_dirs(directory: Path) -> list[Path]:
    return [p for p in directory.iterdir() if ".lint-gate-" in p.name]


def _run_and_signal(tmp_path: Path, mode: str, argv: list[str], sig: int) -> tuple[int, str, Path]:
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER)
    ready = tmp_path / "ready"
    proc = subprocess.Popen(
        [sys.executable, str(driver), mode, str(ready), *argv],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 120
        while not ready.exists():
            if proc.poll() is not None:
                out, err = proc.communicate()
                pytest.fail(f"driver exited early ({proc.returncode}):\n{out}\n{err}")
            if time.monotonic() > deadline:
                pytest.fail("driver never reached the signal point")
            time.sleep(0.05)
        proc.send_signal(sig)
        _out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    return proc.returncode, err, Path(ready.read_text())


def _rejected(output: Path) -> Path:
    return output.with_name(f"{output.stem}.lint-rejected.kicad_pcb")


@pytest.mark.parametrize("existing", [False, True], ids=["new-output", "existing-output"])
def test_sigterm_while_routing_never_leaves_an_unjudged_output(board, tmp_path, existing):
    out = tmp_path / "out.kicad_pcb"
    if existing:
        out.write_text("previous result\n")
    argv = ["route", str(board), "-o", str(out), "--lint-gate", "--no-current-paths"]

    rc, err, written = _run_and_signal(tmp_path, "route", argv, signal.SIGTERM)

    assert rc == SIGTERM_EXIT, err
    assert written != out, "routing must write a staging file, not --output"
    if existing:
        assert out.read_text() == "previous result\n"
    else:
        assert not out.exists()
    rejected = _rejected(out)
    assert rejected.exists() and "seed-1" in rejected.read_text()
    assert "[lint-gate]" in err and "SIGTERM" in err
    assert _staging_dirs(tmp_path) == []


def test_sigterm_while_linting_the_routed_board(board, tmp_path):
    out = tmp_path / "out.kicad_pcb"
    out.write_text("previous result\n")
    argv = ["route", str(board), "-o", str(out), "--lint-gate", "--no-current-paths"]

    rc, err, linted = _run_and_signal(tmp_path, "lint", argv, signal.SIGTERM)

    assert rc == SIGTERM_EXIT, err
    assert ".lint-gate-" in str(linted), "the candidate lint judges the staged board"
    assert out.read_text() == "previous result\n"
    assert "seed-1" in _rejected(out).read_text()
    assert "while the routed board was being linted" in err
    assert _staging_dirs(tmp_path) == []


def test_sigterm_in_place_route_keeps_the_input_byte_for_byte(board, tmp_path):
    before = board.read_bytes()
    argv = ["route", str(board), "-o", str(board), "--lint-gate", "--no-current-paths"]

    rc, err, _ = _run_and_signal(tmp_path, "route", argv, signal.SIGTERM)

    assert rc == SIGTERM_EXIT, err
    assert board.read_bytes() == before
    assert "seed-1" in _rejected(board).read_text()


@pytest.mark.parametrize("mode", ["route", "lint"])
@pytest.mark.parametrize("existing", [False, True], ids=["new-output", "existing-output"])
def test_sigkill_never_leaves_an_unjudged_output(board, tmp_path, mode, existing):
    """No handler runs on SIGKILL: only staging keeps --output clean."""
    out = tmp_path / "out.kicad_pcb"
    if existing:
        out.write_text("previous result\n")
    argv = ["route", str(board), "-o", str(out), "--lint-gate", "--no-current-paths"]

    rc, _err, _ = _run_and_signal(tmp_path, mode, argv, signal.SIGKILL)

    assert rc == -signal.SIGKILL
    if existing:
        assert out.read_text() == "previous result\n"
    else:
        assert not out.exists()
    # The unjudged board stays in the hidden staging directory.
    [staging] = _staging_dirs(tmp_path)
    assert "seed-1" in (staging / out.name).read_text()


# -- in-process behaviour of the staging -------------------------------------


def _stub(extra: str, *, publish: bool = False, emit_json: bool = False):
    def fake(args, parser, argv):
        if publish:
            from kicad_tools.cli.route_receipt import configure

            configure(args)  # what the real _run_main_impl does first
        src = Path(args.pcb)
        out = Path(args.output)
        text = src.read_text()
        if extra:
            i = text.rstrip().rfind(")")
            text = text[:i] + extra + text[i:]
        else:
            text += "\n"
        out.write_text(text)
        out.with_suffix(".kicad_dru").write_text("(version 1)\n")
        if publish:
            from kicad_tools.cli.route_receipt import record_publication

            record_publication(out)
        if emit_json:
            from kicad_tools.json_stdout import json_stdout

            print(json.dumps({"exit_code": 0, "output": str(out)}), file=json_stdout())
        return 0

    return fake


def test_pass_promotes_the_staged_board_and_its_artifacts(board, tmp_path, capsys):
    out = tmp_path / "out.kicad_pcb"
    with patch.object(route_cmd, "_run_main_impl", _stub("", publish=True, emit_json=True)):
        rc = kct_main(
            ["route", str(board), "-o", str(out), "--lint-gate", "--no-current-paths"]
            + ["--format", "json"]
        )
    assert rc == 0, capsys.readouterr().err
    doc = json.loads(capsys.readouterr().out)
    assert doc["output"] == str(out), "staging paths are rewritten in the JSON document"
    assert doc["lint_gate"]["status"] == "pass"
    assert out.exists()
    assert out.with_suffix(".kicad_dru").exists(), "derived artifacts follow the board"
    receipt = json.loads(out.with_suffix(".route.json").read_text())
    assert receipt["files"]["board"]["path"] == out.name
    from kicad_tools.cli.route_receipt import verify_route_receipt

    assert verify_route_receipt(out.with_suffix(".route.json")) == []
    assert _staging_dirs(tmp_path) == []


def test_sigterm_during_promotion_never_half_promotes_output(board, tmp_path):
    """SIGTERM mid-promotion must not delete ``--output``'s project file (#6090)."""
    import os

    from kicad_tools.cli import route_lint_gate

    out = tmp_path / "out.kicad_pcb"
    out.write_text("prevpcb")
    out.with_suffix(".kicad_pro").write_text('{"prev":"pro"}')
    out.with_suffix(".kicad_dru").write_text("(prev)\n")
    real_replace = os.replace
    sent = []

    def replace_then_term(src, dst, *a, **kw):
        real_replace(src, dst, *a, **kw)
        if Path(dst) == out.with_suffix(".kicad_dru") and not sent:
            sent.append(dst)
            os.kill(os.getpid(), signal.SIGTERM)

    with (
        patch.object(route_cmd, "_run_main_impl", _stub("", publish=False)),
        patch.object(route_lint_gate.os, "replace", replace_then_term),
        pytest.raises(SystemExit) as exc,
    ):
        kct_main(["route", str(board), "-o", str(out), "--lint-gate", "--no-current-paths"])

    assert sent, "SIGTERM was never sent mid-promotion"
    assert exc.value.code == SIGTERM_EXIT
    assert out.read_text() != "prevpcb", "the judged board arrived (promotion completed)"
    assert out.with_suffix(".kicad_dru").read_text() == "(version 1)\n"
    assert out.with_suffix(".kicad_pro").exists(), "pre-existing project file must survive"
    assert not _rejected(out).exists()
    assert _staging_dirs(tmp_path) == []


def test_rollback_writes_no_receipt_and_leaves_output_dir_rules_alone(board, tmp_path):
    out = tmp_path / "out.kicad_pcb"
    with patch.object(route_cmd, "_run_main_impl", _stub(SHORT, publish=True)):
        rc = kct_main(["route", str(board), "-o", str(out), "--lint-gate", "--no-current-paths"])
    assert rc == LINT_GATE_EXIT
    assert not out.exists()
    assert not out.with_suffix(".route.json").exists()
    assert not out.with_suffix(".kicad_dru").exists()
    rejected = _rejected(out)
    assert rejected.with_suffix(".kicad_dru").exists(), "the rejected board keeps its rules"
    assert _staging_dirs(tmp_path) == []


def test_checkpoint_at_the_output_path_is_refused(board, tmp_path, capsys):
    out = tmp_path / "out.kicad_pcb"
    calls: list = []

    def fake(args, parser, argv):
        calls.append(args)
        return 0

    with patch.object(route_cmd, "_run_main_impl", fake):
        rc = kct_main(
            ["route", str(board), "-o", str(out), "--checkpoint", str(out)]
            + ["--lint-gate", "--no-current-paths"]
        )
    assert rc == 1
    assert calls == []
    assert "--checkpoint" in capsys.readouterr().err


def test_sigterm_handler_installed_only_over_the_default(board, tmp_path):
    previous = signal.getsignal(signal.SIGTERM)
    try:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        gate = LintGate(board, tmp_path / "out.kicad_pcb", check_flags=["--no-current-paths"])
        assert gate.begin() is None
        assert gate.stage() is not None
        assert signal.getsignal(signal.SIGTERM) is _raise_terminated
        gate.finish()
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL

        # A supervisor deadline handler (or any other owner) is left alone.
        def owner(signum, frame):
            raise AssertionError

        signal.signal(signal.SIGTERM, owner)
        gate = LintGate(board, tmp_path / "out.kicad_pcb", check_flags=["--no-current-paths"])
        assert gate.begin() is None
        assert gate.stage() is not None
        assert signal.getsignal(signal.SIGTERM) is owner
        gate.finish()
        assert signal.getsignal(signal.SIGTERM) is owner
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_resume_from_the_output_path_keeps_the_checkpoint_on_rollback(board, tmp_path):
    """``--resume out -o out``: the checkpoint is read, never overwritten unjudged."""
    out = tmp_path / "out.kicad_pcb"
    shutil.copy(board, out)
    before = out.read_bytes()
    seen: list = []

    def fake(args, parser, argv):
        seen.append((args.resume, args.output))
        Path(args.output).write_text(Path(args.resume).read_text().rstrip()[:-1] + SHORT + ")\n")
        return 0

    with patch.object(route_cmd, "_run_main_impl", fake):
        rc = kct_main(
            ["route", str(board), "--resume", str(out), "-o", str(out)]
            + ["--lint-gate", "--no-current-paths"]
        )
    assert rc == LINT_GATE_EXIT
    assert seen and seen[0][0] == str(out) and seen[0][1] != str(out)
    assert out.read_bytes() == before


def test_route_auto_stages_and_promotes(board, tmp_path):
    out = tmp_path / "auto_out.kicad_pcb"
    seen: list = []

    def fake_route_net_auto(*, pcb_path, net_name, output_path, **_kw):
        seen.append(output_path)
        Path(output_path).write_text(Path(pcb_path).read_text() + "\n")
        return {"success": True, "partial": False, "net_name": net_name, "warnings": []}

    argv = ["route-auto", str(board), "--net", "GND", "-o", str(out), "--lint-gate"]
    with patch("kicad_tools.mcp.tools.routing.route_net_auto", fake_route_net_auto):
        assert kct_main(argv) == 0
    assert seen and all(".lint-gate-" in str(p) for p in seen)
    assert out.exists()
    assert _staging_dirs(tmp_path) == []


def test_deadline_worker_handler_is_preserved_in_a_supervised_run(board, tmp_path):
    """The supervised ``--timeout`` worker owns SIGTERM; the gate must not take it."""
    from kicad_tools.cli import route_deadline

    assert route_deadline._deadline_signal is not _raise_terminated
    # _output_identity reports the requested output, not the staging file.
    args = route_cmd._route_parser().parse_args([str(board), "-o", str(tmp_path / "o.kicad_pcb")])
    args._lint_gate_output = args.output
    args.output = str(tmp_path / ".o.kicad_pcb.lint-gate-1-x" / "o.kicad_pcb")
    identity = route_deadline._output_identity(args)
    assert identity["output"] == str((tmp_path / "o.kicad_pcb").absolute())


# -- SIGTERM during promotion / rollback (Issue #6090 review) ----------------

_NEW_PRO_KEY = "lint_gate_test"


def _promotion_stub(args, parser, argv, *, new_pro: bool = True):
    """A passing route that rewrites the project files and leaves linter debris."""
    from kicad_tools.cli.route_receipt import configure, record_publication

    configure(args)
    src, out = Path(args.pcb), Path(args.output)
    out.write_text(src.read_text() + "\n")
    if new_pro:
        pro = json.loads(src.with_suffix(".kicad_pro").read_text())
        pro[_NEW_PRO_KEY] = "new"
        out.with_suffix(".kicad_pro").write_text(json.dumps(pro))
    else:
        out.with_suffix(".kicad_pro").write_bytes(src.with_suffix(".kicad_pro").read_bytes())
    out.with_suffix(".kicad_dru").write_text("(version 1)\n")
    out.with_name(f"~{out.stem}.kicad_pro.lck").write_text("lock\n")
    out.with_name(f"{out.stem}.routing_plan.json").write_text(
        json.dumps({"source": {"pcb": str(out)}})
    )
    record_publication(out)
    return 0


def _seed_previous_output(work: Path) -> tuple[Path, dict[str, bytes]]:
    out = work / "out.kicad_pcb"
    out.write_text("previous board\n")
    pro = json.loads((work / "test_project.kicad_pro").read_text())
    pro["prev"] = "pro"
    out.with_suffix(".kicad_pro").write_text(json.dumps(pro))
    out.with_suffix(".kicad_dru").write_text("(version 1)\n(rule prev)\n")
    previous = {p.name: p.read_bytes() for p in work.iterdir() if p.name.startswith("out.")}
    return out, previous


def _run_with_sigterm_on_replace(work: Path, out: Path, stub, should_fire) -> tuple[int, list]:
    """Run a gated route; send SIGTERM right after the moves ``should_fire`` picks.

    ``should_fire(index)`` is asked after each ``os.replace`` out of the
    staging directory has happened; ``index`` counts those moves from 0.
    """
    real_replace = os.replace
    moves: list[Path] = []
    fired: list[tuple[Path, Path]] = []

    def replace(src, dst, *a, **kw):
        real_replace(src, dst, *a, **kw)
        if ".lint-gate-" not in str(src):
            return
        moves.append(Path(dst))
        if signal.getsignal(signal.SIGTERM) in (signal.SIG_DFL, signal.SIG_IGN, None):
            return  # never kill the test process
        if should_fire(len(moves) - 1):
            fired.append((Path(src), Path(dst)))
            os.kill(os.getpid(), signal.SIGTERM)

    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        with patch.object(route_cmd, "_run_main_impl", stub), patch.object(os, "replace", replace):
            try:
                rc = kct_main(
                    ["route", str(work / "test_project.kicad_pcb"), "-o", str(out)]
                    + ["--lint-gate", "--no-current-paths"]
                )
            except SystemExit as exc:
                rc = exc.code
    finally:
        signal.signal(signal.SIGTERM, previous)
    return rc, fired


def _fresh_work(tmp_path: Path, name: str) -> Path:
    work = tmp_path / name
    work.mkdir()
    for fixture in ("test_project.kicad_pcb", "test_project.kicad_pro"):
        shutil.copy(FIXTURE / fixture, work / fixture)
    return work


def test_sigterm_at_every_promotion_step_keeps_output_consistent(tmp_path):
    """The judge's repro: SIGTERM after each promotion move, sidecars and board.

    Promotion is held against SIGTERM, so it always completes (all new) and
    the run then exits 143; it must never mix old and new files or delete one
    the user had before.
    """
    step = 0
    destinations: list[Path] = []
    while True:
        work = _fresh_work(tmp_path, f"step{step}")
        out, previous = _seed_previous_output(work)

        rc, fired = _run_with_sigterm_on_replace(
            work, out, _promotion_stub, lambda index, step=step: index == step
        )
        if not fired:
            break  # past the last promotion move
        destinations.append(fired[0][1])

        assert rc == SIGTERM_EXIT, f"step {step}"
        # Nothing the user had is gone.
        for name in previous:
            assert (work / name).exists(), f"step {step}: {name} was deleted"
        board = out.read_text()
        pro = json.loads(out.with_suffix(".kicad_pro").read_text())
        dru = out.with_suffix(".kicad_dru").read_text()
        all_new = board != "previous board\n" and _NEW_PRO_KEY in pro and dru == "(version 1)\n"
        all_old = {n: (work / n).read_bytes() for n in previous} == previous
        assert all_new or all_old, f"step {step}: mixed old/new output {board!r} {pro} {dru!r}"
        assert all_new, f"step {step}: a held SIGTERM must let the promotion finish"
        assert not _rejected(out).exists()
        assert _staging_dirs(work) == []
        step += 1
    assert step >= 3, f"expected sidecar moves and the board move, saw {destinations}"
    # The board is the last (committing) move.
    assert destinations[-1].name == "out.kicad_pcb"
    assert all(d.name != "out.kicad_pcb" for d in destinations[:-1])


def test_repeated_sigterm_during_rollback_does_not_escape(tmp_path):
    """A second SIGTERM mid-rollback must not cut the rollback short."""
    work = _fresh_work(tmp_path, "w")
    out, previous = _seed_previous_output(work)

    def killed_route(args, parser, argv):
        _promotion_stub(args, parser, argv)
        os.kill(os.getpid(), signal.SIGTERM)  # the first SIGTERM, while routing
        time.sleep(5)
        return 0

    # A SIGTERM after every move of the rollback.
    rc, fired = _run_with_sigterm_on_replace(work, out, killed_route, lambda index: True)
    assert fired, "the rollback moved nothing out of staging"
    assert rc == SIGTERM_EXIT
    assert {n: (work / n).read_bytes() for n in previous} == previous
    assert _NEW_PRO_KEY in _rejected(out).with_suffix(".kicad_pro").read_text()
    assert _staging_dirs(work) == []


def test_promotion_relocates_json_sidecars_and_drops_kicad_locks(tmp_path):
    work = _fresh_work(tmp_path, "w")
    out = work / "out.kicad_pcb"

    def stub(args, parser, argv):
        return _promotion_stub(args, parser, argv, new_pro=False)

    with patch.object(route_cmd, "_run_main_impl", stub):
        rc = kct_main(
            ["route", str(work / "test_project.kicad_pcb"), "-o", str(out)]
            + ["--lint-gate", "--no-current-paths"]
        )
    assert rc == 0
    plan = json.loads((work / "out.routing_plan.json").read_text())
    assert plan["source"]["pcb"] == str(out.absolute())
    assert not list(work.glob("~*.lck"))
    from kicad_tools.cli.route_receipt import verify_route_receipt

    assert verify_route_receipt(out.with_suffix(".route.json")) == []


def test_stale_staging_of_a_dead_run_is_reaped(board, tmp_path):
    out = tmp_path / "out.kicad_pcb"
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    stale = tmp_path / f".out.kicad_pcb.lint-gate-{dead.pid}-abc123"
    stale.mkdir()
    (stale / "out.kicad_pcb").write_text("unjudged\n")
    live = tmp_path / f".out.kicad_pcb.lint-gate-{os.getppid()}-def456"
    live.mkdir()
    other = tmp_path / f".other.kicad_pcb.lint-gate-{dead.pid}-ghi789"
    other.mkdir()

    with patch.object(route_cmd, "_run_main_impl", _stub("")):
        rc = kct_main(["route", str(board), "-o", str(out), "--lint-gate", "--no-current-paths"])
    assert rc == 0
    assert not stale.exists(), "a dead run's staging directory is removed"
    assert live.exists(), "a live process's staging directory is left alone"
    assert other.exists(), "another output's staging directory is not ours"
