"""``route --analyze --dry-run`` never routes, on any flow (Issue #6648).

After #6563 / PR #6633, ``--analyze --dry-run`` wrote no files, but the
escalation flows (default ``--auto-layers``, ``--auto-pcb-size``,
``--auto-mfr-tier``, ``--adaptive-rules``) ignored ``--analyze``: they routed
the whole ladder in memory and printed no analysis.  The single-attempt
analysis also swallowed every exception and fell through to a real route.

These tests assert, for each flow: the COMPLEXITY and ROUTABILITY analysis
blocks are printed, no escalation wrapper or ``Autorouter.route_all*`` method
is reached, and nothing is written.  If the analysis raises, the command
exits non-zero, still without routing.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.analysis import ComplexityAnalyzer
from kicad_tools.cli import route_cmd
from kicad_tools.router import RoutabilityAnalyzer
from kicad_tools.router.core import Autorouter

from .test_route_dry_run_escalation_6563 import (
    _ROUTER_ENTRY_POINTS,
    _snapshot,
    _write_board,
)

_AUTOROUTER_ROUTE_METHODS = tuple(
    name for name in dir(Autorouter) if name.startswith("route_all") or name == "route_net"
)

_FLOWS = [
    ([], "default-auto-layers"),
    (["--no-auto-layers", "--layers", "2"], "no-auto-layers-layers-2"),
    (["--auto-pcb-size"], "auto-pcb-size"),
    (["--auto-mfr-tier"], "auto-mfr-tier"),
    (["--adaptive-rules"], "adaptive-rules"),
    (["--auto-layers", "--adaptive-rules"], "auto-layers-adaptive-rules"),
]


def _no_routing(stack: ExitStack) -> list[str]:
    """Make every router entry point record its call and raise if reached.

    The returned list is asserted empty: the CLI may catch the raised error
    and turn it into a non-zero exit, which would mask a real route.
    """
    reached: list[str] = []

    def _trap(label: str):
        def _raise(*_args, **_kwargs):
            reached.append(label)
            raise AssertionError(f"{label} reached")

        return _raise

    for name in _ROUTER_ENTRY_POINTS:
        stack.enter_context(patch.object(route_cmd, name, side_effect=_trap(name)))
    for name in _AUTOROUTER_ROUTE_METHODS:
        stack.enter_context(patch.object(Autorouter, name, side_effect=_trap(f"Autorouter.{name}")))
    return reached


def _run(tmp_path: Path, extra: list[str]) -> tuple[int, Path]:
    pcb = _write_board(tmp_path / "board.kicad_pcb")
    output = tmp_path / "out.kicad_pcb"
    rc = route_cmd.main(
        [
            str(pcb),
            "--analyze",
            "--dry-run",
            "--grid",
            "0.1",
            "--no-cache",
            "--skip-drc",
            "-o",
            str(output),
            *extra,
        ]
    )
    return rc, output


def test_autorouter_route_methods_are_patched():
    assert "route_all" in _AUTOROUTER_ROUTE_METHODS
    assert "route_all_negotiated" in _AUTOROUTER_ROUTE_METHODS


@pytest.mark.parametrize("extra", [f[0] for f in _FLOWS], ids=[f[1] for f in _FLOWS])
def test_analyze_dry_run_prints_analysis_and_never_routes(tmp_path, capsys, extra):
    _write_board(tmp_path / "board.kicad_pcb")
    stale = tmp_path / "out_4layer.kicad_pcb"
    stale.write_text("USER FILE")
    before = _snapshot(tmp_path)
    with ExitStack() as stack:
        reached = _no_routing(stack)
        rc, output = _run(tmp_path, extra)
    assert reached == []
    assert rc == 0
    out = capsys.readouterr().out
    assert "COMPLEXITY ANALYSIS" in out
    assert "ROUTABILITY ANALYSIS" in out
    assert not output.exists()
    assert stale.read_text() == "USER FILE"
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "target",
    [
        (ComplexityAnalyzer, "analyze"),
        (RoutabilityAnalyzer, "analyze"),
    ],
    ids=["complexity-raises", "routability-raises"],
)
@pytest.mark.parametrize(
    "extra",
    [[], ["--no-auto-layers", "--layers", "2"]],
    ids=["default-auto-layers", "layers-2"],
)
def test_analyze_dry_run_exits_nonzero_when_analysis_raises(tmp_path, capsys, target, extra):
    """An analysis failure under ``--dry-run`` must not fall through to a route."""
    _write_board(tmp_path / "board.kicad_pcb")
    before = _snapshot(tmp_path)
    with ExitStack() as stack:
        reached = _no_routing(stack)
        stack.enter_context(patch.object(*target, side_effect=RuntimeError("analysis exploded")))
        rc, output = _run(tmp_path, extra)
    assert reached == []
    assert rc != 0
    err = capsys.readouterr().err
    assert "analysis exploded" in err
    assert not output.exists()
    assert _snapshot(tmp_path) == before


def test_dry_run_help_says_analyze_never_routes():
    help_text = route_cmd._route_parser().format_help()
    flat = " ".join(help_text.split())
    assert "still routes in memory" not in flat
    assert "never routes" in flat


@pytest.mark.parametrize("entry", ["route_with_layer_escalation", "route_with_combined_escalation"])
def test_escalation_wrappers_skip_stale_cleanup_under_dry_run(tmp_path, entry):
    """Defence in depth (ported from 893e4d7d6): called directly with
    ``dry_run``, the escalation wrappers pass ``dry_run=True`` to the
    stale-sibling cleanup."""
    seen: list[bool] = []

    def _fake_cleanup(output_path, quiet=False, *, dry_run=False):
        seen.append(dry_run)
        raise RuntimeError("stop after cleanup")

    args = MagicMock()
    args.dry_run = True
    with patch.object(route_cmd, "_cleanup_stale_layer_artifacts", side_effect=_fake_cleanup):
        with pytest.raises(RuntimeError, match="stop after cleanup"):
            getattr(route_cmd, entry)(
                pcb_path=tmp_path / "board.kicad_pcb",
                output_path=tmp_path / "out.kicad_pcb",
                args=args,
                quiet=True,
            )
    assert seen == [True]
