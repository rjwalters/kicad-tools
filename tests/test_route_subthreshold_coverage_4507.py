"""``--hv-threshold`` coverage gap is reported, not silent (issue #4507).

The #4507 T4 criterion asks softstart rev-C to route with **0 board-level
``kct creepage`` fails**.  Re-scoring T4 on top of the #4867 signed-potential
fix surfaced a second, independent reason that target is unreachable at
default settings -- and this one is not a search problem at all:

``build_pairwise_clearance_table`` drops every pair whose ``|ΔV|`` is below
``--hv-threshold`` (30 V by default), leaving it at the scalar DRU floor.  The
census has no threshold: it looks IEC 60664-1 up at the pair's actual ``|ΔV|``,
and the standard's low-voltage rows (0.40-0.53 mm at PD2 / material group IIIa)
are *above* a typical 0.2 mm fab floor.  On softstart rev-C that is 405 pairs
the router will not widen and the census will score.

The threshold itself is deliberate policy (it stops LV<->LV pairs being
over-segregated), so this suite does not change the default.  It pins that the
run *says so*, that the ``--hv-threshold 0`` escape hatch the message
advertises actually enforces those pairs end to end, and that a map with
nothing hidden stays quiet.

Board fixture is a synthetic S-expression string, deliberately mirroring
``test_route_signed_voltage_map_4867.py``: the softstart rev-C board is
local-only and must never become a CI dependency.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli.route_cmd import main as route_main
from kicad_tools.router.pairwise_clearance import subthreshold_coverage_gap

# A +/-10 V map is a real 20 V span -- below the 30 V threshold, so the router
# holds it at the DRU floor, while IEC 60664-1 PD2/IIIa requires 0.48 mm at
# 20 V.  The fixture's copper is 0.4 mm apart: inside the fab floor, outside
# the standard.  That is the whole gap in one pair.
SPAN_VOLTS = 10.0
REQUIRED_AT_20V = 0.48


def _pad(number: str, x: float, y: float, net: int, net_name: str) -> str:
    return (
        f'(pad "{number}" smd rect (at {x} {y}) (size 0.4 0.4) '
        f'(layers "F.Cu" "F.Paste" "F.Mask") (net {net} "{net_name}"))'
    )


def _fp(ref: str, uid: int, x: float, y: float, pads: str) -> str:
    return f"""  (footprint "Resistor_SMD:R_0603_1608Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000{uid:02d}")
    (at {x} {y})
    (property "Reference" "{ref}" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    {pads}
  )
"""


def _preserved_board() -> str:
    """Preserved ``/BANK_POS`` / ``/BANK_NEG`` copper 0.4 mm edge-to-edge."""
    parts = [
        _fp("R1", 10, 103.0, 120.0, _pad("1", 0, 0, 3, "/SIG_A")),
        _fp("R2", 11, 127.0, 120.0, _pad("1", 0, 0, 3, "/SIG_A")),
    ]
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "/BANK_POS")
  (net 2 "/BANK_NEG")
  (net 3 "/SIG_A")
  (gr_rect (start 100 100) (end 130 124)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (segment (start 105 105) (end 125 105) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 105 105.6) (end 125 105.6) (width 0.2) (layer "F.Cu") (net 2))
{"".join(parts)})
"""


def _write_map(tmp_path: Path, payload: dict, name: str = "vmap.json") -> Path:
    vmap = tmp_path / name
    vmap.write_text(json.dumps(payload))
    return vmap


def _route_args(pcb: Path, out: Path, vmap: Path | None, *extra: str) -> list[str]:
    args = [
        str(pcb),
        "-o",
        str(out),
        "--route-engine",
        "lattice",
        "--strategy",
        "basic",
        "--skip-drc",
        "--preserve-existing",
    ]
    if vmap is not None:
        args += [
            "--voltage-map",
            str(vmap),
            "--creepage-standard",
            "iec60664",
            "--pollution-degree",
            "2",
            "--material-group",
            "IIIa",
        ]
    return args + list(extra)


@pytest.fixture
def board(tmp_path: Path) -> Path:
    pcb = tmp_path / "subthreshold.kicad_pcb"
    pcb.write_text(_preserved_board())
    return pcb


# ---------------------------------------------------------------------------
# The measurement helper
# ---------------------------------------------------------------------------


def test_gap_counts_only_pairs_the_threshold_hides() -> None:
    """A 20 V span needs 0.48 mm but sits below the 30 V threshold."""
    gap = subthreshold_coverage_gap({"/BANK_POS": SPAN_VOLTS, "/BANK_NEG": -SPAN_VOLTS}, dru=0.2)
    assert gap.pair_count == 1
    assert gap.max_required_mm == pytest.approx(REQUIRED_AT_20V)
    assert gap.worst_pair == ("BANK_NEG", "BANK_POS")
    assert gap.worst_delta_v == pytest.approx(2 * SPAN_VOLTS)


def test_gap_excludes_pairs_the_matrix_already_holds() -> None:
    """Above the threshold the pair is enforced, so it is not a *gap*."""
    gap = subthreshold_coverage_gap({"/HV": 150.0, "/GND": 0.0}, dru=0.2)
    assert gap == (0, 0.0, None, 0.0)


def test_gap_excludes_same_potential_pairs() -> None:
    """The census requires 0 mm of a same-potential pair; so does the router."""
    gap = subthreshold_coverage_gap({"/A": 12.0, "/B": 12.0}, dru=0.2)
    assert gap.pair_count == 0


def test_gap_respects_the_dru_floor() -> None:
    """A fab floor wider than the standard's low-voltage row hides nothing."""
    assert subthreshold_coverage_gap({"/A": 10.0, "/B": -10.0}, dru=0.6).pair_count == 0


def test_gap_is_empty_when_the_threshold_is_disarmed() -> None:
    """``--hv-threshold 0`` leaves nothing on the wrong side of the threshold."""
    gap = subthreshold_coverage_gap(
        {"/BANK_POS": SPAN_VOLTS, "/BANK_NEG": -SPAN_VOLTS}, dru=0.2, hv_threshold=0.0
    )
    assert gap.pair_count == 0


# ---------------------------------------------------------------------------
# The CLI surface
# ---------------------------------------------------------------------------


def test_run_reports_the_hidden_pairs(board: Path, tmp_path: Path, capsys) -> None:
    """The banner's ``0 cross-pairs`` no longer stands alone and reassuring."""
    vmap = _write_map(tmp_path, {"/BANK_POS": SPAN_VOLTS, "/BANK_NEG": -SPAN_VOLTS})

    rc = route_main(_route_args(board, tmp_path / "note.kicad_pcb", vmap))
    captured = capsys.readouterr().out

    assert rc == 0, captured
    assert "HV pairwise clearance: 2 mapped nets, 0 cross-pairs" in captured
    assert "1 further pair(s) sit below the 30V --hv-threshold" in captured
    assert "BANK_NEG<->BANK_POS at 20V -> 0.480mm" in captured
    assert "--hv-threshold 0" in captured


def test_disarming_the_threshold_enforces_the_hidden_pair(
    board: Path, tmp_path: Path, capsys
) -> None:
    """The advertised escape hatch must actually work end to end.

    With the threshold at 0 the 20 V pair enters the matrix, the note has
    nothing left to report, and the 0.4 mm copper now fails the #4588 audit at
    the standard's 0.48 mm -- i.e. the router and the census finally agree.
    """
    vmap = _write_map(tmp_path, {"/BANK_POS": SPAN_VOLTS, "/BANK_NEG": -SPAN_VOLTS})

    rc = route_main(_route_args(board, tmp_path / "armed.kicad_pcb", vmap, "--hv-threshold", "0"))
    captured = capsys.readouterr().out

    assert "HV pairwise clearance: 2 mapped nets, 1 cross-pairs" in captured
    assert "sit below the" not in captured
    assert rc != 0, captured
    assert "pairwise HV clearance violations" in captured


def test_no_note_when_the_threshold_hides_nothing(board: Path, tmp_path: Path, capsys) -> None:
    """A map whose only pair is already enforced prints no advisory."""
    vmap = _write_map(tmp_path, {"/BANK_POS": 150.0, "/BANK_NEG": 0.0})

    route_main(_route_args(board, tmp_path / "quiet.kicad_pcb", vmap))
    captured = capsys.readouterr().out

    assert "HV pairwise clearance: 2 mapped nets, 1 cross-pairs" in captured
    assert "sit below the" not in captured


def test_dormant_without_a_voltage_map(board: Path, tmp_path: Path, capsys) -> None:
    """No ``--voltage-map`` -> no banner and no advisory (byte-identical path)."""
    route_main(_route_args(board, tmp_path / "dormant.kicad_pcb", None))
    captured = capsys.readouterr().out

    assert "HV pairwise clearance:" not in captured
    assert "sit below the" not in captured
