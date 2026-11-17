"""In-process CLI runs must not leak their SIGINT/SIGTERM handlers (Issue #6175).

``run_optimize_placement`` installed ``_handle_placement_interrupt`` for both
signals and restored them only at the end of its happy path, so every early
``return`` left it installed.  A later SIGTERM in the same process (the
``--lint-gate`` promotion test, an MCP server) then hit ``sys.exit(130)``.
"""

from __future__ import annotations

import signal
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


@pytest.fixture
def default_signals():
    saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        yield saved
    finally:
        for signum, handler in saved.items():
            signal.signal(signum, handler)


def test_optimize_placement_early_return_restores_handlers(tmp_path, default_signals):
    from kicad_tools.cli.optimize_placement_cmd import run_optimize_placement

    sigint = signal.getsignal(signal.SIGINT)
    board = tmp_path / "empty.kicad_pcb"
    board.write_text('(kicad_pcb (version 20240108) (generator "test"))\n')
    rc = run_optimize_placement(str(board), dry_run=True, quiet=True)
    assert rc != 0  # no components: an early return
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
    assert signal.getsignal(signal.SIGINT) is sigint


def test_optimize_placement_raise_restores_handlers(tmp_path, default_signals, monkeypatch):
    from kicad_tools.cli import optimize_placement_cmd as cmd

    def boom(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(cmd, "_parse_weights", boom)
    board = tmp_path / "empty.kicad_pcb"
    board.write_text('(kicad_pcb (version 20240108) (generator "test"))\n')
    with pytest.raises(RuntimeError, match="boom"):
        cmd.run_optimize_placement(str(board), weights_json="{}", quiet=True)
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL


def test_foreign_handler_installed_mid_run_is_kept(tmp_path, default_signals, monkeypatch):
    from kicad_tools.cli import optimize_placement_cmd as cmd

    def foreign(signum, frame):  # pragma: no cover - never delivered
        pass

    def install_foreign(*_a, **_kw):
        signal.signal(signal.SIGTERM, foreign)
        raise RuntimeError("stop")

    monkeypatch.setattr(cmd, "_parse_weights", install_foreign)
    board = tmp_path / "empty.kicad_pcb"
    board.write_text('(kicad_pcb (version 20240108) (generator "test"))\n')
    with pytest.raises(RuntimeError):
        cmd.run_optimize_placement(str(board), weights_json="{}", quiet=True)
    assert signal.getsignal(signal.SIGTERM) is foreign


def test_route_process_guard_restores_sigterm(default_signals):
    from kicad_tools.cli import route_cmd

    with pytest.raises(RuntimeError):
        with route_cmd._process_state_guard():
            signal.signal(signal.SIGTERM, route_cmd._handle_interrupt)
            raise RuntimeError("escalation flow raised before restoring")
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
