"""Tests for ``kct zones hv-keepout`` -- HV plane pour-keepouts (issue #4372).

The command carves geometric rule-area keepouts (Approach A) so inner copper
pours void around HV nets by a required clearance.  These tests exercise the
pure geometry (no ``kicad-cli`` needed), the shared HV-net classification with
``kct creepage``, the CLI round-trip, and the enumerated edge cases.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.zones_cmd import main as zones_main
from kicad_tools.creepage.engine import resolve_hv_nets
from kicad_tools.schema.pcb import PCB
from kicad_tools.zones.hv_keepout import build_hv_keepout_plan

pytest.importorskip("shapely")


# A minimal-but-real board:
#  * Outline gr_line rectangle (100,100)->(160,140) -> board origin (100,100).
#  * net 1 AC_LINE carries a horizontal F.Cu trace from (110,110) to (150,110).
#  * net 2 GND is a filled pour on the inner layer In1.Cu covering the board.
_BOARD = """\
(kicad_pcb
  (version 20240108)
  (generator "test_hv_keepout")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "AC_LINE")
  (net 2 "GND")
  (gr_line (start 100 100) (end 160 100) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 160 100) (end 160 140) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 160 140) (end 100 140) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 100 140) (end 100 100) (layer "Edge.Cuts") (width 0.1))
  (segment (start 110 110) (end 150 110) (width 0.5) (layer "F.Cu") (net 1))
  (zone
    (net 2)
    (net_name "GND")
    (layer "In1.Cu")
    (uuid "gnd-plane-uuid")
    (hatch edge 0.5)
    (priority 0)
    (connect_pads (clearance 0.25))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.4) (thermal_bridge_width 0.35))
    (polygon
      (pts
        (xy 101 101)
        (xy 159 101)
        (xy 159 139)
        (xy 101 139)
      )
    )
    (filled_polygon
      (layer "In1.Cu")
      (pts
        (xy 101 101)
        (xy 159 101)
        (xy 159 139)
        (xy 101 139)
      )
    )
  )
)
"""

# An HV net (AC_LINE) that pours on its OWN inner layer, plus a GND pour on the
# same layer -- exercises edge case (b): the HV net's own pour layer must be
# excluded from the void target set.
_BOARD_HV_POUR = """\
(kicad_pcb
  (version 20240108)
  (generator "test_hv_keepout")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "AC_LINE")
  (net 2 "GND")
  (gr_line (start 100 100) (end 160 100) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 160 100) (end 160 140) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 160 140) (end 100 140) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 100 140) (end 100 100) (layer "Edge.Cuts") (width 0.1))
  (zone
    (net 1)
    (net_name "AC_LINE")
    (layer "F.Cu")
    (uuid "hv-pour-uuid")
    (hatch edge 0.5)
    (priority 0)
    (connect_pads (clearance 0.25))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.4) (thermal_bridge_width 0.35))
    (polygon (pts (xy 105 105) (xy 130 105) (xy 130 130) (xy 105 130)))
    (filled_polygon
      (layer "F.Cu")
      (pts (xy 105 105) (xy 130 105) (xy 130 130) (xy 105 130))
    )
  )
  (zone
    (net 2)
    (net_name "GND")
    (layer "In1.Cu")
    (uuid "gnd-plane-uuid")
    (hatch edge 0.5)
    (priority 0)
    (connect_pads (clearance 0.25))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.4) (thermal_bridge_width 0.35))
    (polygon (pts (xy 101 101) (xy 159 101) (xy 159 139) (xy 101 139)))
    (filled_polygon
      (layer "In1.Cu")
      (pts (xy 101 101) (xy 159 101) (xy 159 139) (xy 101 139))
    )
  )
)
"""


def _write(tmp_path: Path, source: str, name: str = "board.kicad_pcb") -> Path:
    path = tmp_path / name
    path.write_text(source)
    return path


# ---------------------------------------------------------------------------
# Geometry (Approach A) -- no kicad-cli needed
# ---------------------------------------------------------------------------


def test_keepout_buffers_hv_copper_by_clearance(tmp_path: Path) -> None:
    """A GND-plane keepout buffers the HV trace by exactly ``--clearance``."""
    from shapely.geometry import Polygon

    from kicad_tools.geometry.copper import segment_copper_polygon

    pcb = PCB.load(str(_write(tmp_path, _BOARD)))
    hv_nets = resolve_hv_nets(pcb, "HV", None)
    assert hv_nets  # AC_LINE resolves via the mains-name fallback

    clearance = 1.6
    plan = build_hv_keepout_plan(pcb, hv_nets, clearance_mm=clearance)

    # One void region on the sole inner plane layer.
    assert plan.keepout_count == 1
    assert plan.plane_layers == ["In1.Cu"]

    void = plan.voids[0]
    assert void.layers == ["In1.Cu"]

    # Reconstruct the void in board-relative coordinates and compare against the
    # HV trace buffered by the clearance.  The board origin is (100, 100).
    ox, oy = pcb.board_origin
    void_poly = Polygon([(x - ox, y - oy) for x, y in void.points])

    # HV trace board-relative: (10,10)->(50,10), width 0.5.
    hv_geom = segment_copper_polygon((10.0, 10.0), (50.0, 10.0), 0.5)
    expected = hv_geom.buffer(clearance)

    # The emitted void polygon should match buffer(HV, clearance) closely.
    assert void_poly.symmetric_difference(expected).area < 1e-3
    # And its boundary sits at least ``clearance`` from the HV copper.
    assert void_poly.contains(hv_geom)


def test_clearance_scales_the_void(tmp_path: Path) -> None:
    """A larger clearance produces a strictly larger void polygon."""
    from shapely.geometry import Polygon

    pcb = PCB.load(str(_write(tmp_path, _BOARD)))
    hv_nets = resolve_hv_nets(pcb, "HV", None)

    def _void_area(clearance: float) -> float:
        plan = build_hv_keepout_plan(pcb, hv_nets, clearance_mm=clearance)
        return Polygon(plan.voids[0].points).area

    assert _void_area(2.0) > _void_area(0.8)


# ---------------------------------------------------------------------------
# Shared HV classification with kct creepage
# ---------------------------------------------------------------------------


def test_net_class_map_matches_creepage_classification(tmp_path: Path) -> None:
    """The HV set is selected identically to ``kct creepage`` given a map."""
    from kicad_tools.router.rules import net_class_map_from_dict

    pcb = PCB.load(str(_write(tmp_path, _BOARD)))
    ncm = net_class_map_from_dict({"AC_LINE": {"name": "HV"}})
    # resolve_hv_nets is the shared entry point both commands call.
    via_map = resolve_hv_nets(pcb, "HV", ncm)
    via_fallback = resolve_hv_nets(pcb, "HV", None)
    assert set(via_map.values()) == {"AC_LINE"}
    assert set(via_fallback.values()) == {"AC_LINE"}


# ---------------------------------------------------------------------------
# CLI round-trip
# ---------------------------------------------------------------------------


def test_cli_writes_keepout_zone(tmp_path: Path) -> None:
    """The CLI appends a persistent keepout zone (net 0, copperpour off)."""
    board = _write(tmp_path, _BOARD)
    rc = zones_main(
        ["hv-keepout", str(board), "--clearance", "1.6", "--plane-layers", "In1.Cu", "-q"]
    )
    assert rc == 0

    text = board.read_text()
    assert "keepout" in text
    assert "copperpour" in text and "not_allowed" in text
    assert '(layers "In1.Cu")' in text

    # Re-parse: exactly one new keepout zone (net 0) was added.
    pcb = PCB.load(str(board))
    keepouts = [z for z in pcb.zones if z.net_number == 0]
    assert len(keepouts) == 1


def test_cli_dry_run_writes_nothing(tmp_path: Path) -> None:
    """``--dry-run`` reports the plan but leaves the input untouched."""
    board = _write(tmp_path, _BOARD)
    before = board.read_text()
    rc = zones_main(["hv-keepout", str(board), "--clearance", "1.6", "--dry-run"])
    assert rc == 0
    assert board.read_text() == before


def test_cli_output_flag_leaves_input_untouched(tmp_path: Path) -> None:
    """``-o`` writes to the alternate path; the input stays pristine."""
    board = _write(tmp_path, _BOARD)
    before = board.read_text()
    out = tmp_path / "out.kicad_pcb"
    rc = zones_main(["hv-keepout", str(board), "--clearance", "1.6", "-o", str(out), "-q"])
    assert rc == 0
    assert board.read_text() == before
    assert out.exists()
    pcb = PCB.load(str(out))
    assert any(z.net_number == 0 for z in pcb.zones)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_no_hv_nets_is_clean_noop(tmp_path: Path, capsys) -> None:
    """Edge (a): no HV nets -> informative no-op, exit 0, no write."""
    source = _BOARD.replace('(net 1 "AC_LINE")', '(net 1 "SIG")').replace(
        "(net 1))",
        "(net 1))",  # segment stays on the now-non-HV net
    )
    board = _write(tmp_path, source, "nohv.kicad_pcb")
    before = board.read_text()
    rc = zones_main(["hv-keepout", str(board), "--clearance", "1.6"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "No 'HV' nets found" in out
    assert board.read_text() == before  # nothing written


def test_hv_own_pour_layer_excluded(tmp_path: Path) -> None:
    """Edge (b): an HV net's own pour layer is excluded from the void set."""
    pcb = PCB.load(str(_write(tmp_path, _BOARD_HV_POUR)))
    hv_nets = resolve_hv_nets(pcb, "HV", None)
    assert set(hv_nets.values()) == {"AC_LINE"}

    # Target all pour-carrying layers explicitly (F.Cu is the HV pour layer).
    plan = build_hv_keepout_plan(pcb, hv_nets, clearance_mm=1.6, plane_layers=["F.Cu", "In1.Cu"])
    assert "F.Cu" in plan.excluded_layers  # HV pours there -> excluded
    assert plan.plane_layers == ["In1.Cu"]
    for void in plan.voids:
        assert "F.Cu" not in void.layers


def test_default_plane_layers_targets_pour_layers(tmp_path: Path) -> None:
    """Edge (c): omitting --plane-layers targets all plane-pour layers."""
    pcb = PCB.load(str(_write(tmp_path, _BOARD)))
    hv_nets = resolve_hv_nets(pcb, "HV", None)
    plan = build_hv_keepout_plan(pcb, hv_nets, clearance_mm=1.6, plane_layers=None)
    # The only net-bound pour is GND on In1.Cu.
    assert plan.plane_layers == ["In1.Cu"]
    assert plan.keepout_count == 1


def test_hv_net_with_no_copper_no_keepout(tmp_path: Path) -> None:
    """Edge (d): an HV net with no copper yet -> no keepout, no crash."""
    # Drop the AC_LINE trace so the HV net carries no copper.
    source = _BOARD.replace(
        '  (segment (start 110 110) (end 150 110) (width 0.5) (layer "F.Cu") (net 1))\n',
        "",
    )
    pcb = PCB.load(str(_write(tmp_path, source, "empty_hv.kicad_pcb")))
    hv_nets = resolve_hv_nets(pcb, "HV", None)
    assert set(hv_nets.values()) == {"AC_LINE"}
    plan = build_hv_keepout_plan(pcb, hv_nets, clearance_mm=1.6)
    assert plan.keepout_count == 0


def test_nonpositive_clearance_rejected(tmp_path: Path) -> None:
    """A non-positive --clearance is rejected before any write."""
    board = _write(tmp_path, _BOARD)
    before = board.read_text()
    rc = zones_main(["hv-keepout", str(board), "--clearance", "0"])
    assert rc == 1
    assert board.read_text() == before


def test_missing_net_class_map_errors(tmp_path: Path) -> None:
    """A missing --net-class-map file is a loud error, not a silent pass."""
    board = _write(tmp_path, _BOARD)
    rc = zones_main(
        [
            "hv-keepout",
            str(board),
            "--clearance",
            "1.6",
            "--net-class-map",
            str(tmp_path / "does-not-exist.json"),
        ]
    )
    assert rc == 1
