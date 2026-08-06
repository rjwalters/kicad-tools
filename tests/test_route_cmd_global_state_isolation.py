"""Issue #4559 regression: ``route_cmd.main()`` must not leak process-global state.

``route_cmd.main()`` is invoked **in-process** by ``kct route``
(``commands/routing.py``) and by dozens of test files.  Before the fix it
stamped four kinds of process-global state and restored none of them:

1. ``os.environ`` -- the ``KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE`` /
   ``KICAD_TOOLS_MICRO_VIA_IN_PAD_FALLBACK`` (+ ``_SIZE``/``_DRILL``)
   escalation stamps, read lazily in ``EscapeRouter.__init__`` -- a leaked
   ``=1`` silently changes routed copper for every subsequent in-process
   invocation (the pytest-xdist worker-poisoning bug from PR #4556).
2. the global ``random`` seed (``--seed``).
3. the ``SIGINT`` handler installed for the Ctrl+C partial-save path.
4. ``_interrupt_state["router"]`` -- pinning an ``Autorouter`` + grid alive.

The fix wraps the ``main()`` boundary in ``_process_state_guard`` (a
snapshot/restore context manager).  Stickiness *within* one invocation is
deliberately preserved -- the restore runs strictly at the outermost exit.

These tests were verified to FAIL on the pre-fix parent commit and PASS
with the guard in place (see the PR body for the exact verification runs).
"""

from __future__ import annotations

import os
import random
import signal
from pathlib import Path

import pytest

from kicad_tools.cli import route_cmd

_REPO = Path(__file__).resolve().parents[1]
_VOLTAGE_DIVIDER = _REPO / "boards/01-voltage-divider/output/voltage_divider.kicad_pcb"

#: Every sticky env var ``route_cmd`` stamps (issue #4559 AC5 enumeration --
#: re-verify with:  git grep -nE 'environ\[[^]]+\][[:space:]]*=' src/kicad_tools/cli/
_STICKY_VARS = (
    "KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE",
    "KICAD_TOOLS_MICRO_VIA_IN_PAD_FALLBACK",
    "KICAD_TOOLS_MICRO_VIA_SIZE",
    "KICAD_TOOLS_MICRO_VIA_DRILL",
)


@pytest.fixture(autouse=True)
def _clean_sticky_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Normalize the ambient env BEFORE each test (mirrors the PR #4556 pins).

    This is *pre-cleaning only*.  The leak assertions below snapshot with a
    plain ``dict(os.environ)`` inside the test body -- never via monkeypatch
    -- because monkeypatch's teardown restore would mask the very leak under
    test (the trap called out in issue #4559's test plan).
    """
    for var in _STICKY_VARS:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Fast leak tests: a nonexistent board still reaches the env stamps and the
# --seed seeding (both run BEFORE board loading in ``_main_impl``), then
# returns 1 -- so these exercise the leak without routing anything.
# ---------------------------------------------------------------------------


def test_env_and_rng_restored_after_flag_stamping_run() -> None:
    """AC1(a)+(c): exact environ equality and untouched RNG after ``main()``."""
    before_env = dict(os.environ)
    before_rng = random.getstate()

    rc = route_cmd.main(
        [
            "definitely-missing-board-4559.kicad_pcb",
            "--micro-via-in-pad-fallback",
            "--strict-in-pad-clearance",
            "--seed",
            "42",
        ]
    )

    assert rc == 1  # file not found -- but the stamps already happened
    assert dict(os.environ) == before_env
    for var in _STICKY_VARS:
        assert var not in os.environ, f"{var} leaked out of route_cmd.main()"
    assert random.getstate() == before_rng


def test_second_call_not_poisoned_by_first() -> None:
    """AC4 order-dependence shape: two ``main()`` calls in one process.

    Call 1 opts into the micro-via fallback; between the calls (and after
    call 2, which does NOT opt in) the env channel that
    ``EscapeRouter.__init__`` reads lazily must be empty, so any
    ``EscapeRouter`` constructed by a later in-process invocation sees the
    default-off state.
    """
    rc1 = route_cmd.main(["definitely-missing-board-4559.kicad_pcb", "--micro-via-in-pad-fallback"])
    assert rc1 == 1
    assert os.environ.get("KICAD_TOOLS_MICRO_VIA_IN_PAD_FALLBACK") is None
    assert os.environ.get("KICAD_TOOLS_MICRO_VIA_SIZE") is None
    assert os.environ.get("KICAD_TOOLS_MICRO_VIA_DRILL") is None

    rc2 = route_cmd.main(["definitely-missing-board-4559.kicad_pcb"])
    assert rc2 == 1
    assert os.environ.get("KICAD_TOOLS_MICRO_VIA_IN_PAD_FALLBACK") is None


# ---------------------------------------------------------------------------
# Guard unit behavior (exception path, seeded-vs-unseeded RNG, foreign
# SIGINT handler).
# ---------------------------------------------------------------------------


def test_env_restored_when_main_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1: the restore runs on the raise path, and vars *created* during the
    run are removed (not merely reverted)."""

    def boom(argv: list[str] | None = None) -> int:
        os.environ["KICAD_TOOLS_MICRO_VIA_IN_PAD_FALLBACK"] = "1"
        os.environ["KICAD_TOOLS_4559_SENTINEL"] = "1"  # created, must be removed
        raise RuntimeError("boom")

    monkeypatch.setattr(route_cmd, "_main_impl", boom)
    before_env = dict(os.environ)

    with pytest.raises(RuntimeError, match="boom"):
        route_cmd.main([])

    assert dict(os.environ) == before_env
    assert "KICAD_TOOLS_4559_SENTINEL" not in os.environ


def test_guard_leaves_unseeded_rng_untouched() -> None:
    """AC1(c) negative half: without ``--seed`` the guard must NOT roll the
    global RNG back -- unseeded consumption inside the guarded region stays
    visible to the caller (current behavior, preserved)."""
    random.seed(1234)
    state_before = random.getstate()

    with route_cmd._process_state_guard():
        random.random()  # unseeded consumption inside the guarded region

    assert random.getstate() != state_before, (
        "guard rolled back the global RNG even though the run never seeded it"
    )


def test_guard_does_not_clobber_foreign_sigint_handler() -> None:
    """The SIGINT restore only fires if the handler is still
    ``_handle_interrupt`` on exit -- a handler installed by someone else
    mid-run must survive."""
    before = signal.getsignal(signal.SIGINT)

    def foreign_handler(signum, frame):  # pragma: no cover - never invoked
        pass

    try:
        with route_cmd._process_state_guard():
            signal.signal(signal.SIGINT, route_cmd._handle_interrupt)
            signal.signal(signal.SIGINT, foreign_handler)
        assert signal.getsignal(signal.SIGINT) is foreign_handler
    finally:
        signal.signal(signal.SIGINT, before)


# ---------------------------------------------------------------------------
# Full-run test: a real (tiny) board routed through the single-attempt path,
# which installs the SIGINT handler and pins the router in _interrupt_state.
# ---------------------------------------------------------------------------


def test_full_route_restores_sigint_handler_env_and_router_pin(
    tmp_path: Path,
) -> None:
    """AC1(a)+(b) on a run that reaches the SIGINT install site, plus the
    memory half: no ``Autorouter`` (and grid) left pinned in
    ``_interrupt_state`` after a completed run."""
    assert _VOLTAGE_DIVIDER.exists(), f"fixture board missing: {_VOLTAGE_DIVIDER}"

    before_env = dict(os.environ)
    before_handler = signal.getsignal(signal.SIGINT)
    out_pcb = tmp_path / "routed.kicad_pcb"

    rc = route_cmd.main(
        [
            str(_VOLTAGE_DIVIDER),
            "--strategy",
            "basic",
            "--seed",
            "42",
            "--skip-drc",
            "--micro-via-in-pad-fallback",
            "-o",
            str(out_pcb),
        ]
    )

    assert rc == 0
    assert out_pcb.exists()
    assert dict(os.environ) == before_env
    assert signal.getsignal(signal.SIGINT) is before_handler
    assert route_cmd._interrupt_state["router"] is None
    assert route_cmd._interrupt_state["output_path"] is None
    assert route_cmd._interrupt_state["pcb_path"] is None
