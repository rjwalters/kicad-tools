"""#4281: geometric post-passes (optimize + DRC nudge) are grid-engine-only.

The lattice and mesh engines commit copper by appending to ``router.routes``
without calling ``_mark_route``, so the grid's obstacle model (cell occupancy
+ per-layer segment R-tree) contains no route copper after a non-grid run.
The CLI's post-route TraceOptimizer validates every move against that empty
model (everything is "clear"), and the DRC-nudge repair has no
foreign-segment destination gate -- both passes corrupted correct lattice
copper into cross-net shorts, which the #3989/#4208 backstop then demoted
(board 02: 7/8 instead of 8/8; LINE_A demoted even under ``--no-optimize``
because the nudge is not covered by that flag).

Fix under test: one shared predicate, ``_engine_post_passes_enabled``, gates
BOTH passes at all four optimize+nudge call-site pairs; the #4208 backstop
stays unconditional (defense in depth).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from kicad_tools.cli.route_cmd import _engine_post_passes_enabled

_REPO = Path(__file__).resolve().parents[3]
_CHARLIEPLEX = _REPO / "boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb"
_VOLTAGE_DIVIDER = _REPO / "boards/01-voltage-divider/output/voltage_divider.kicad_pcb"


# ---------------------------------------------------------------------------
# Shared predicate unit behavior.
# ---------------------------------------------------------------------------


def test_grid_enables_post_passes_silently(capsys: pytest.CaptureFixture) -> None:
    args = argparse.Namespace(route_engine="grid")
    assert _engine_post_passes_enabled(args) is True
    # Grid must be a strict no-op: no notice printed (byte-identical output).
    assert capsys.readouterr().out == ""


def test_missing_route_engine_defaults_to_grid() -> None:
    # Callers that never defined --route-engine keep today's behavior.
    assert _engine_post_passes_enabled(argparse.Namespace()) is True
    assert _engine_post_passes_enabled(argparse.Namespace(route_engine=None)) is True


@pytest.mark.parametrize("engine", ["lattice", "mesh"])
def test_non_grid_engines_disable_post_passes(engine: str, capsys: pytest.CaptureFixture) -> None:
    args = argparse.Namespace(route_engine=engine)
    assert _engine_post_passes_enabled(args) is False
    out = capsys.readouterr().out
    # One-line notice so users know why no optimize pass ran.
    assert "Skipping optimize/nudge post-passes" in out
    assert engine in out
    assert "#4281" in out


@pytest.mark.parametrize("engine", ["lattice", "mesh"])
def test_quiet_suppresses_skip_notice(engine: str, capsys: pytest.CaptureFixture) -> None:
    args = argparse.Namespace(route_engine=engine)
    assert _engine_post_passes_enabled(args, quiet=True) is False
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# CLI tail integration: lattice copper never reaches optimize/nudge; the
# unconditional #4208 backstop finds nothing to demote (board 02 is 8/8).
# ---------------------------------------------------------------------------


def _spy_post_passes(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Wrap the two post-pass entry points with call counters.

    ``route_cmd`` imports both lazily (``from ... import ...`` inside the
    gated blocks), so patching the source modules observes exactly what the
    CLI tail executes.
    """
    import kicad_tools.router.drc_nudge as drc_nudge_mod
    import kicad_tools.router.optimizer as optimizer_mod

    calls = {"optimize": 0, "nudge": 0}
    real_optimize = optimizer_mod.optimize_routes_grid_synced
    real_nudge = drc_nudge_mod.drc_verify_and_nudge

    def spy_optimize(*args: object, **kwargs: object) -> object:
        calls["optimize"] += 1
        return real_optimize(*args, **kwargs)

    def spy_nudge(*args: object, **kwargs: object) -> object:
        calls["nudge"] += 1
        return real_nudge(*args, **kwargs)

    monkeypatch.setattr(optimizer_mod, "optimize_routes_grid_synced", spy_optimize)
    monkeypatch.setattr(drc_nudge_mod, "drc_verify_and_nudge", spy_nudge)
    return calls


@pytest.mark.parametrize("extra", [[], ["--no-optimize"]], ids=["default", "no-optimize"])
def test_cli_lattice_skips_both_passes_and_demotes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    extra: list[str],
) -> None:
    """Board-02 lattice run: 8/8, no optimize, no nudge, no backstop demotion.

    Covers both holes: the optimizer pass (NODE_A short, default flags) and
    the nudge pass, which is NOT covered by ``--no-optimize`` (the LINE_A
    demotion) -- hence the parametrization.
    """
    from kicad_tools.cli import route_cmd

    calls = _spy_post_passes(monkeypatch)
    out_pcb = tmp_path / "routed.kicad_pcb"
    rc = route_cmd.main(
        [
            str(_CHARLIEPLEX),
            "--route-engine",
            "lattice",
            "--strategy",
            "basic",
            "--seed",
            "42",
            "--skip-drc",
            "-o",
            str(out_pcb),
            *extra,
        ]
    )
    out = capsys.readouterr().out

    assert rc == 0
    assert calls == {"optimize": 0, "nudge": 0}, "post-passes must not touch lattice copper"
    assert "Skipping optimize/nudge post-passes for --route-engine lattice" in out
    # The unconditional #4208 backstop ran and found nothing to demote
    # (pre-fix: 'Post-optimize backstop demoted 1 net(s) ...' -> 7/8).
    assert "Post-optimize backstop demoted" not in out
    assert "Nets routed:     8/8" in out
    assert out_pcb.exists()


def test_cli_grid_still_runs_both_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Default (grid) engine keeps today's tail: optimize AND nudge both run."""
    from kicad_tools.cli import route_cmd

    calls = _spy_post_passes(monkeypatch)
    out_pcb = tmp_path / "routed.kicad_pcb"
    rc = route_cmd.main(
        [
            str(_VOLTAGE_DIVIDER),
            "--strategy",
            "basic",
            "--seed",
            "42",
            "--skip-drc",
            "-o",
            str(out_pcb),
        ]
    )
    out = capsys.readouterr().out

    assert rc == 0
    assert calls["optimize"] >= 1, "grid engine must still optimize"
    assert calls["nudge"] >= 1, "grid engine must still run the DRC nudge"
    assert "Skipping optimize/nudge post-passes" not in out
    assert out_pcb.exists()
