"""Pad copper is oriented by the pad's absolute angle (Issue #6460).

KiCad stores a pad's ``(at x y ANGLE)`` angle as an *absolute* orientation.
Strict trace-to-pad contact (``NetStatusAnalyzer._pad_polys``) and the
pour-bond / partition callers in ``ConnectivityValidator`` used to orient
every pad outline by the footprint rotation only.  On a non-square pad
rotated relative to its footprint that swaps width and height, which:

* invents contact for a trace or pour that ends next to the real copper
  (a hidden open: kicad-cli reports it unconnected), and
* misses contact for a trace or pour that lands on the real copper
  (a false open: kicad-cli reports it connected).

Every board below was checked against ``kicad-cli pcb drc`` (KiCad 10.0.6);
the expected results in the tests are kicad-cli's ``unconnected_items``.
The optional ``test_*_agrees_with_kicad_cli`` cases re-run that check when
kicad-cli is installed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from kicad_tools.analysis.net_status import NetStatusAnalyzer
from kicad_tools.validate.connectivity import ConnectivityValidator

# U1 sits at rotation ``fp_rot``; its pad 1 is a 2.0 x 0.4 rect whose absolute
# angle is ``fp_rot + 90``, so the real copper is 0.4 wide across the
# footprint's X axis and 2.0 tall along its Y axis.  At ``fp_rot == 0`` that is
# x 9.8..10.2, y 9..11.  The footprint-only model swapped it to x 9..11,
# y 9.8..10.2.
_BOARD = """(kicad_pcb (version 20240108) (generator "t") (generator_version "8.0")
  (general (thickness 1.6)) (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0)) (net 0 "") (net 1 "SIG")
  (footprint "T:A" (layer "F.Cu") (uuid "u1") (at 10 10 {fp_rot})
    (property "Reference" "U1" (at 0 -3 0) (layer "F.SilkS") (uuid "r1"))
    (pad "1" smd rect (at 0 0 {pad_rot}) (size 2.0 0.4) (layers "F.Cu") (net 1 "SIG")
      (uuid "p1")))
  (footprint "T:B" (layer "F.Cu") (uuid "u2") (at 20 {u2y} 0)
    (property "Reference" "U2" (at 0 -3 0) (layer "F.SilkS") (uuid "r2"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "SIG") (uuid "p2")))
{copper}
)
"""


def _seg(a: tuple[float, float], b: tuple[float, float]) -> str:
    return (
        f"  (segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width 0.2) "
        f'(layer "F.Cu") (net 1) (uuid "s{a[0]}_{a[1]}_{b[0]}_{b[1]}"))'
    )


def _fill(x0: float, y0: float, x1: float, y1: float) -> str:
    pts = f"(xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1}) (xy {x0} {y1})"
    return f"""  (zone (net 1) (net_name "SIG") (layer "F.Cu") (uuid "z1")
    (connect_pads yes (clearance 0.2)) (min_thickness 0.2)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts {pts}))
    (filled_polygon (layer "F.Cu") (pts {pts})))"""


# name -> (u2y, copper, kicad-cli says U1.1 and U2.1 are connected)
_CASES: dict[str, tuple[float, str, bool]] = {
    # Trace ends at x=10.7: 0.4 mm right of the real pad edge (x=10.2) but
    # inside the swapped box (x<=11).
    "trace_false_contact": (10, _seg((10.7, 10), (20, 10)), False),
    # Trace ends at y=10.8 on the real pad's long axis (y<=11) but outside the
    # swapped box (y<=10.2).
    "trace_false_open": (15, _seg((10, 10.8), (10, 15)) + "\n" + _seg((10, 15), (20, 15)), True),
    # Fill starts at x=10.5: past the real copper (x<=10.2) but inside the
    # swapped box.
    "pour_false_contact": (10, _fill(10.5, 8, 22, 12), False),
    # Fill starts at y=10.5: overlaps the real copper (y<=11) but not the
    # swapped box (y<=10.2).
    "pour_false_open": (13, _fill(5, 10.5, 22, 14), True),
}


def _board(tmp_path: Path, name: str, *, fp_rot: float = 0.0) -> tuple[Path, bool]:
    u2y, copper, connected = _CASES[name]
    path = tmp_path / f"{name}.kicad_pcb"
    path.write_text(_BOARD.format(fp_rot=fp_rot, pad_rot=fp_rot + 90, u2y=u2y, copper=copper))
    return path, connected


def _strict_islands(path: Path) -> tuple[int, str]:
    status = NetStatusAnalyzer(path, strict=True).analyze().get_net("SIG")
    assert status is not None
    return status.island_count, status.status


@pytest.mark.parametrize("name", sorted(_CASES))
def test_strict_net_status_uses_absolute_pad_rotation(tmp_path: Path, name: str):
    path, connected = _board(tmp_path, name)
    islands, status = _strict_islands(path)
    if connected:
        assert (islands, status) == (1, "complete")
    else:
        assert (islands, status) == (2, "incomplete")


@pytest.mark.parametrize("name", sorted(_CASES))
def test_pad_partition_uses_absolute_pad_rotation(tmp_path: Path, name: str):
    """The stitch / LVS partition shares the same pad geometry."""
    path, connected = _board(tmp_path, name)
    partition = ConnectivityValidator(path).extract_pad_partition()
    joined = frozenset({"U1.1", "U2.1"}) in partition
    assert joined is connected


def test_footprint_rotation_alone_does_not_orient_pad(tmp_path: Path):
    """A pad whose absolute angle equals the footprint's is unaffected.

    With the footprint itself at 90 and the pad at absolute 90, the pad is
    *not* rotated relative to its footprint: the real copper is 0.4 wide in X
    and 2.0 tall in Y, so a trace ending at y=10.8 still lands on it.
    """
    text = _BOARD.format(
        fp_rot=90,
        pad_rot=90,
        u2y=15,
        copper=_seg((10, 10.8), (10, 15)) + "\n" + _seg((10, 15), (20, 15)),
    )
    path = tmp_path / "aligned.kicad_pcb"
    path.write_text(text)
    assert _strict_islands(path) == (1, "complete")


def _kicad_cli() -> Path | None:
    from kicad_tools.cli.runner import find_kicad_cli

    return find_kicad_cli()


@pytest.mark.skipif(_kicad_cli() is None, reason="kicad-cli not installed")
@pytest.mark.parametrize("name", sorted(_CASES))
def test_cases_agree_with_kicad_cli(tmp_path: Path, name: str):
    path, connected = _board(tmp_path, name)
    out = tmp_path / f"{name}.json"
    subprocess.run(
        [
            str(_kicad_cli()),
            "pcb",
            "drc",
            "--format",
            "json",
            "--severity-all",
            "-o",
            str(out),
            str(path),
        ],
        capture_output=True,
        check=False,
    )
    unconnected = json.loads(out.read_text()).get("unconnected_items", [])
    assert (len(unconnected) == 0) is connected
    islands, _ = _strict_islands(path)
    assert islands - 1 == len(unconnected)
