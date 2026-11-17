"""Side-by-side wirelength estimator reporting (issue #4831, M5).

M1 made the optimizer objective able to score wirelength at real pads, but
left it opt-in behind ``--pad-anchored-wirelength`` because flipping the
default needs evidence from our own boards rather than pcbplace's
second-hand 59.5 -> 12.7 mm number. M5 supplies that evidence channel: every
``kct optimize-placement --dry-run`` and every MCP ``evaluate_placement``
response now reports *both* estimators for the same layout.

These tests pin down the property that makes the feature safe -- it is
report-only:

* the reported ``centre_anchored_mm`` / ``pad_anchored_mm`` pair is measured
  with the *same* estimator, so the only difference is the anchoring;
* ``Net.weight`` is honoured on both legs (the audit's M1 counter-note);
* whichever estimator the objective scored is the one that shows up in the
  scored breakdown -- reporting the other one changes nothing;
* boards decoded without pad geometry are flagged, not silently reported as
  a zero delta that looks like agreement.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.placement.cost import Net
from kicad_tools.placement.vector import (
    ComponentDef,
    PadDef,
    PlacedComponent,
    TransformedPad,
    decode,
    encode,
)
from kicad_tools.placement.wirelength import (
    WirelengthEstimatorReport,
    compare_wirelength_estimators,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _placed(
    reference: str,
    x: float,
    y: float,
    pads: list[tuple[str, float, float]],
    rotation: float = 0.0,
) -> PlacedComponent:
    """A PlacedComponent whose pads are already in absolute board coordinates."""
    return PlacedComponent(
        reference=reference,
        x=x,
        y=y,
        rotation=rotation,
        side=0,
        pads=tuple(
            TransformedPad(name=n, x=px, y=py, size_x=0.5, size_y=0.5) for n, px, py in pads
        ),
    )


# Two 10x10 parts, centres 20 mm apart, wired between their *facing* pads
# (10 mm apart). Same geometry as the M1 suite, so the two files agree on
# what the numbers mean.
FACING_PADS = [
    _placed("U1", 0.0, 0.0, [("1", 5.0, 0.0), ("2", -5.0, 0.0)]),
    _placed("U2", 20.0, 0.0, [("1", 15.0, 0.0), ("2", 25.0, 0.0)]),
]
FACING_NET = Net(name="SIG", pins=[("U1", "1"), ("U2", "1")])


# ---------------------------------------------------------------------------
# compare_wirelength_estimators: the measurement itself
# ---------------------------------------------------------------------------


def test_reports_both_estimators_for_one_layout() -> None:
    report = compare_wirelength_estimators(FACING_PADS, [FACING_NET])

    assert report.centre_anchored_mm == pytest.approx(20.0)
    assert report.pad_anchored_mm == pytest.approx(10.0)
    assert report.delta_mm == pytest.approx(-10.0)
    assert report.delta_pct == pytest.approx(-50.0)
    assert report.pads_available is True
    assert report.pad_count == 4


def test_scored_defaults_to_centre_and_is_recorded_verbatim() -> None:
    """The report says which estimator the caller's objective actually used."""
    assert compare_wirelength_estimators(FACING_PADS, [FACING_NET]).scored == "centre"
    assert compare_wirelength_estimators(FACING_PADS, [FACING_NET], scored="pad").scored == "pad"


def test_scored_only_labels_and_never_alters_the_measurements() -> None:
    centre = compare_wirelength_estimators(FACING_PADS, [FACING_NET], scored="centre")
    pad = compare_wirelength_estimators(FACING_PADS, [FACING_NET], scored="pad")

    assert centre.centre_anchored_mm == pad.centre_anchored_mm
    assert centre.pad_anchored_mm == pad.pad_anchored_mm
    assert centre.delta_mm == pad.delta_mm


def test_unknown_scored_value_is_rejected() -> None:
    with pytest.raises(ValueError, match="scored must be"):
        compare_wirelength_estimators(FACING_PADS, [FACING_NET], scored="pads")


def test_pad_anchoring_can_read_longer_than_centres() -> None:
    """The delta is a measurement, not a guaranteed discount."""
    placements = [
        _placed("U1", 0.0, 0.0, [("1", -5.0, 0.0)]),
        _placed("U2", 20.0, 0.0, [("1", 25.0, 0.0)]),
    ]
    report = compare_wirelength_estimators(placements, [Net("SIG", [("U1", "1"), ("U2", "1")])])

    assert report.centre_anchored_mm == pytest.approx(20.0)
    assert report.pad_anchored_mm == pytest.approx(30.0)
    assert report.delta_mm == pytest.approx(10.0)
    assert report.delta_pct == pytest.approx(50.0)


def test_net_weight_is_honoured_on_both_legs() -> None:
    """Both numbers come from compute_wirelength, so neither drops Net.weight.

    Pairing ``compute_wirelength`` against ``compute_hpwl`` would have made
    the delta a mixture of "anchoring changed" and "weighting vanished".
    """
    weighted = Net(name="SIG", pins=[("U1", "1"), ("U2", "1")], weight=3.0)
    report = compare_wirelength_estimators(FACING_PADS, [weighted])

    assert report.centre_anchored_mm == pytest.approx(60.0)
    assert report.pad_anchored_mm == pytest.approx(30.0)
    assert report.delta_pct == pytest.approx(-50.0)


def test_delta_pct_is_none_when_the_centre_estimate_is_zero() -> None:
    """Coincident centres make the ratio undefined -- report null, not inf."""
    placements = [_placed("U1", 0.0, 0.0, [("1", -5.0, 0.0), ("2", 5.0, 2.0)])]
    report = compare_wirelength_estimators(placements, [Net("LOOP", [("U1", "1"), ("U1", "2")])])

    assert report.centre_anchored_mm == pytest.approx(0.0)
    assert report.pad_anchored_mm == pytest.approx(12.0)
    assert report.delta_pct is None


def test_a_board_without_pad_geometry_is_flagged_not_reported_as_agreement() -> None:
    padless = [
        PlacedComponent("U1", 0.0, 0.0, 0.0, 0, ()),
        PlacedComponent("U2", 20.0, 0.0, 0.0, 0, ()),
    ]
    report = compare_wirelength_estimators(padless, [FACING_NET])

    assert report.pads_available is False
    assert report.pad_count == 0
    assert report.pad_anchored_mm == pytest.approx(report.centre_anchored_mm)
    assert "not measurable" in report.summary_line()


def test_empty_net_list_is_a_zero_report() -> None:
    report = compare_wirelength_estimators(FACING_PADS, [])

    assert report.centre_anchored_mm == pytest.approx(0.0)
    assert report.pad_anchored_mm == pytest.approx(0.0)
    assert report.delta_pct is None
    assert report.pads_available is True


def test_rotation_moves_only_the_pad_anchored_number() -> None:
    """The divergence the report exists to expose: rotation is invisible to
    centres and real to pads."""
    parts = [
        ComponentDef(
            reference="U1",
            pads=(PadDef(name="1", local_x=5.0, local_y=0.0, size_x=0.5, size_y=0.5),),
            width=10.0,
            height=10.0,
        ),
        ComponentDef(
            reference="U2",
            pads=(PadDef(name="1", local_x=0.0, local_y=0.0, size_x=0.5, size_y=0.5),),
            width=2.0,
            height=2.0,
        ),
    ]
    net = Net(name="SIG", pins=[("U1", "1"), ("U2", "1")])

    def report(rotation: float) -> WirelengthEstimatorReport:
        vector = encode(
            [
                PlacedComponent("U1", 0.0, 0.0, rotation, 0, ()),
                PlacedComponent("U2", 20.0, 0.0, 0.0, 0, ()),
            ]
        )
        return compare_wirelength_estimators(decode(vector, parts), [net])

    at_0, at_180 = report(0.0), report(180.0)

    assert (
        at_0.centre_anchored_mm == pytest.approx(at_180.centre_anchored_mm) == pytest.approx(20.0)
    )
    assert at_0.pad_anchored_mm == pytest.approx(15.0)
    assert at_180.pad_anchored_mm == pytest.approx(25.0)


def test_report_is_deterministic() -> None:
    """Same inputs, same frozen dataclass -- no ordering or float drift."""
    first = compare_wirelength_estimators(FACING_PADS, [FACING_NET])
    second = compare_wirelength_estimators(FACING_PADS, [FACING_NET])

    assert first == second


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_as_dict_is_json_serializable_with_the_documented_keys() -> None:
    payload = compare_wirelength_estimators(FACING_PADS, [FACING_NET]).as_dict()

    assert set(payload) == {
        "centre_anchored_mm",
        "pad_anchored_mm",
        "delta_mm",
        "delta_pct",
        "scored",
        "pads_available",
        "pad_count",
    }
    assert json.loads(json.dumps(payload))["delta_mm"] == pytest.approx(-10.0)


def test_as_dict_rounds_only_when_asked() -> None:
    placements = [
        _placed("U1", 0.0, 0.0, [("1", 0.123456, 0.0)]),
        _placed("U2", 10.0, 0.0, [("1", 9.876543, 0.0)]),
    ]
    report = compare_wirelength_estimators(placements, [Net("SIG", [("U1", "1"), ("U2", "1")])])

    assert report.as_dict()["pad_anchored_mm"] == pytest.approx(9.753087)
    assert report.as_dict(ndigits=4)["pad_anchored_mm"] == pytest.approx(9.7531)


def test_as_dict_keeps_a_null_delta_pct_null_when_rounding() -> None:
    placements = [_placed("U1", 0.0, 0.0, [("1", -5.0, 0.0), ("2", 5.0, 2.0)])]
    report = compare_wirelength_estimators(placements, [Net("LOOP", [("U1", "1"), ("U1", "2")])])

    assert report.as_dict(ndigits=4)["delta_pct"] is None


# ---------------------------------------------------------------------------
# CLI: kct optimize-placement --dry-run
# ---------------------------------------------------------------------------

pytest.importorskip("cmaes", reason="cmaes not installed (optional 'placement'/'dev' extra)")

CLI_BOARD = Path(__file__).parent / "fixtures" / "routing-diagnostic.kicad_pcb"


@pytest.fixture
def board(tmp_path: Path) -> Path:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(CLI_BOARD.read_text())
    return pcb


def _dry_run_document(board: Path, capsys, *, pad_anchored: bool = False) -> dict:
    from kicad_tools.cli.commands.optimize_placement import run_optimize_placement_command
    from kicad_tools.cli.parser import create_parser

    argv = ["optimize-placement", str(board), "--dry-run", "--format", "json"]
    if pad_anchored:
        argv.append("--pad-anchored-wirelength")
    rc = run_optimize_placement_command(create_parser().parse_args(argv))
    assert rc == 0
    return json.loads(capsys.readouterr().out)


def test_dry_run_json_carries_both_estimators(board, capsys) -> None:
    payload = _dry_run_document(board, capsys)

    estimators = payload["wirelength_estimators"]
    assert set(estimators) == {
        "centre_anchored_mm",
        "pad_anchored_mm",
        "delta_mm",
        "delta_pct",
        "scored",
        "pads_available",
        "pad_count",
    }
    assert estimators["pads_available"] is True
    assert estimators["pad_count"] > 0


def test_dry_run_default_still_scores_the_centre_anchored_number(board, capsys) -> None:
    """The load-bearing invariant: reporting the other estimator changes
    nothing about what was scored."""
    payload = _dry_run_document(board, capsys)
    estimators = payload["wirelength_estimators"]

    assert estimators["scored"] == "centre"
    assert payload["scores"]["current"]["breakdown"]["wirelength"] == pytest.approx(
        estimators["centre_anchored_mm"]
    )


def test_dry_run_pad_anchored_flag_moves_which_number_is_scored(board, capsys) -> None:
    payload = _dry_run_document(board, capsys, pad_anchored=True)
    estimators = payload["wirelength_estimators"]

    assert estimators["scored"] == "pad"
    assert payload["scores"]["current"]["breakdown"]["wirelength"] == pytest.approx(
        estimators["pad_anchored_mm"]
    )


def test_the_flag_changes_the_score_but_not_the_reported_pair(board, capsys) -> None:
    """Both estimators are measured from the layout, not from the flag."""
    centre_doc = _dry_run_document(board, capsys)
    pad_doc = _dry_run_document(board, capsys, pad_anchored=True)

    for key in ("centre_anchored_mm", "pad_anchored_mm", "delta_mm", "pad_count"):
        assert centre_doc["wirelength_estimators"][key] == pytest.approx(
            pad_doc["wirelength_estimators"][key]
        )


def test_dry_run_prose_prints_the_comparison(board, capsys) -> None:
    from kicad_tools.cli.optimize_placement_cmd import run_optimize_placement

    assert run_optimize_placement(str(board), dry_run=True) == 0
    out = capsys.readouterr().out

    assert "Wirelength estimators:" in out
    assert "centre-anchored" in out
    assert "pad-anchored" in out
    assert "docs/placement-pad-anchoring-audit.md" in out


def test_dry_run_quiet_stays_quiet(board, capsys) -> None:
    from kicad_tools.cli.optimize_placement_cmd import run_optimize_placement

    assert run_optimize_placement(str(board), dry_run=True, quiet=True) == 0

    assert "Wirelength estimators:" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# MCP: evaluate_placement
# ---------------------------------------------------------------------------

VOLTAGE_DIVIDER_PCB = str(
    Path(__file__).parent.parent
    / "boards"
    / "01-voltage-divider"
    / "output"
    / "voltage_divider.kicad_pcb"
)


@pytest.mark.skipif(
    not Path(VOLTAGE_DIVIDER_PCB).exists(),
    reason="Voltage divider board not available",
)
def test_mcp_evaluate_placement_reports_both_estimators() -> None:
    from kicad_tools.mcp.tools.optimize_placement import evaluate_placement

    result = evaluate_placement(VOLTAGE_DIVIDER_PCB)

    assert result["success"] is True
    estimators = result["wirelength_estimators"]
    assert estimators["scored"] == "centre", "the MCP objective is still centre-anchored"
    assert estimators["pads_available"] is True
    # Report-only: the scored breakdown is the centre-anchored number.
    assert result["breakdown"]["wirelength"] == pytest.approx(
        estimators["centre_anchored_mm"], abs=1e-3
    )
