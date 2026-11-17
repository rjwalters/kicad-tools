"""CLI-level tests for signed ``--voltage-map`` potentials (issue #4867).

``kct route --voltage-map`` read its sidecar through placement's
magnitude-only loader and then took ``abs()`` again, so on a **signed** map --
the normal encoding for a bipolar HV bank, and the encoding ``kct creepage``
itself reads -- a ``+150 V`` net and a ``-150 V`` net differenced to ``0 V``.
Below the 30 V threshold, such a pair was not merely under-required: it was
**absent from the matrix entirely**, therefore invisible to search-time
avoidance *and* to the post-route audit, which can only report on pairs its own
table holds.  The run printed a reassuring ``HV pairwise clearance: N mapped
nets, M cross-pairs`` banner (with ``M`` silently short by every bank pair) and
exited 0 over +/-300 V copper at the DRU floor.

These tests pin the CLI end of the fix:

1. a bipolar map on a board with preserved +/-150 V copper 0.4 mm apart is a
   hard FAIL, not a silent SUCCESS;
2. the banner's cross-pair count is the signed count;
3. a signed span *below* ``--hv-threshold`` still yields no pairs (no
   over-correction); and
4. an all-positive map behaves exactly as before.

The board is a fully synthetic S-expression string (same approach as
``test_route_pairwise_gate_4588.py``) -- the softstart rev-C board from the
original report is local-only and must never be a CI dependency.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli.route_cmd import main as route_main

# +/-150 V about the map's reference: a 300 V span requiring 3.2 mm of creepage
# under IEC 60664-1 / PD2 / material group IIIa.  Either leg alone against 0 V
# needs only 1.6 mm -- so the span is the binding requirement, and it is exactly
# the one the magnitude collapse erased.
BANK_VOLTS = 150.0


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


def _bipolar_preserved_board() -> str:
    """Preserved +/-150 V copper 0.6 mm apart (0.4 mm edge-to-edge).

    ``/BANK_POS`` and ``/BANK_NEG`` carry hand-placed ``(segment ...)`` copper
    and have no pads at all, so the router never re-routes them: with
    ``--preserve-existing`` that copper is re-emitted verbatim and the
    post-route audit (#4699) scans it.  Fixing the geometry at author time
    keeps this test independent of any search outcome.  ``/SIG_A`` is an
    ordinary two-pad link in the far corner that gives the run something to do.
    """
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


def _route_args(pcb: Path, out: Path, vmap: Path | None) -> list[str]:
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
    return args


@pytest.fixture
def bipolar_board(tmp_path: Path) -> Path:
    pcb = tmp_path / "bipolar.kicad_pcb"
    pcb.write_text(_bipolar_preserved_board())
    return pcb


def test_signed_map_no_longer_false_passes_bipolar_copper(
    bipolar_board: Path, tmp_path: Path, capsys
) -> None:
    """The headline regression: +/-150 V at 0.4 mm must FAIL the run."""
    vmap = _write_map(tmp_path, {"/BANK_POS": BANK_VOLTS, "/BANK_NEG": -BANK_VOLTS})
    out = tmp_path / "bipolar_routed.kicad_pcb"

    rc = route_main(_route_args(bipolar_board, out, vmap))
    captured = capsys.readouterr().out

    assert rc != 0, f"expected a non-zero exit, got {rc}\n{captured}"
    assert "SUCCESS:" not in captured
    assert "pairwise HV clearance violations" in captured
    assert "/BANK_POS vs /BANK_NEG" in captured or "/BANK_NEG vs /BANK_POS" in captured


def test_banner_reports_the_signed_cross_pair_count(
    bipolar_board: Path, tmp_path: Path, capsys
) -> None:
    """``M cross-pairs`` must count the pair, not silently omit it."""
    vmap = _write_map(tmp_path, {"/BANK_POS": BANK_VOLTS, "/BANK_NEG": -BANK_VOLTS})
    out = tmp_path / "banner.kicad_pcb"

    route_main(_route_args(bipolar_board, out, vmap))
    captured = capsys.readouterr().out

    assert "HV pairwise clearance: 2 mapped nets, 1 cross-pairs" in captured


def test_signed_span_below_the_threshold_stays_dormant(
    bipolar_board: Path, tmp_path: Path, capsys
) -> None:
    """No over-correction: +/-10 V is a real 20 V span, still under 30 V."""
    vmap = _write_map(tmp_path, {"/BANK_POS": 10.0, "/BANK_NEG": -10.0})
    out = tmp_path / "lowv.kicad_pcb"

    rc = route_main(_route_args(bipolar_board, out, vmap))
    captured = capsys.readouterr().out

    assert rc == 0, captured
    assert "HV pairwise clearance: 2 mapped nets, 0 cross-pairs" in captured
    assert "pairwise HV clearance violations" not in captured


def test_all_positive_map_is_unchanged(bipolar_board: Path, tmp_path: Path, capsys) -> None:
    """The common non-bipolar case must behave exactly as before (#4867 AC).

    150 V vs 0 V needs 1.6 mm; the planted copper is 0.4 mm apart, so this map
    failed pre-#4867 and must still fail -- with the same single cross-pair.
    """
    vmap = _write_map(tmp_path, {"/BANK_POS": BANK_VOLTS, "/BANK_NEG": 0.0})
    out = tmp_path / "positive.kicad_pcb"

    rc = route_main(_route_args(bipolar_board, out, vmap))
    captured = capsys.readouterr().out

    assert rc != 0, captured
    assert "HV pairwise clearance: 2 mapped nets, 1 cross-pairs" in captured
    assert "pairwise HV clearance violations" in captured


def test_no_voltage_map_is_still_a_strict_noop(bipolar_board: Path, tmp_path: Path, capsys) -> None:
    """Dormancy: without ``--voltage-map`` nothing about this board changes."""
    out = tmp_path / "noflag.kicad_pcb"

    rc = route_main(_route_args(bipolar_board, out, None))
    captured = capsys.readouterr().out

    assert rc == 0, captured
    assert "SUCCESS:" in captured
    assert "HV pairwise clearance:" not in captured
