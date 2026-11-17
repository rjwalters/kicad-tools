"""Tests for ``kct zones hv-keepout`` -- HV plane pour-keepouts (issue #4372).

The command carves geometric rule-area keepouts (Approach A) so inner copper
pours void around HV nets by a required clearance.  These tests exercise the
pure geometry (no ``kicad-cli`` needed), the shared HV-net classification with
``kct creepage``, the CLI round-trip, and the enumerated edge cases.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.cli.zones_cmd import main as zones_main
from kicad_tools.creepage.engine import resolve_hv_nets
from kicad_tools.schema.pcb import PCB
from kicad_tools.zones.hv_keepout import build_hv_keepout_plan

pytest.importorskip("shapely")

# kicad-cli is used for the end-to-end load test below; skip when absent.
# find_kicad_cli() checks PATH plus common non-PATH install locations, so
# this can't silently disagree with the rest of the suite about which
# kicad-cli (if any) is available (#5714).
KICAD_CLI = find_kicad_cli()


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
    """A GND-plane keepout conservatively encloses the requested offset."""
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

    # The offset must enclose the old inscribed buffer, including its chords.
    assert void_poly.covers(expected)
    assert void_poly.boundary.distance(hv_geom) >= clearance
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


@pytest.mark.skipif(
    KICAD_CLI is None,
    reason="find_kicad_cli() found no kicad-cli install (checked PATH and common install locations)",
)
def test_emitted_keepout_loads_in_kicad(tmp_path: Path) -> None:
    """Regression (Issue #4430): the emitted keepout must load in kicad-cli.

    v0.19.0 wrote quoted disposition tokens (``(tracks "allowed")``) plus a bare
    ``(fill yes)`` node, which KiCad 10 rejects as a hard parse error ->
    ``Failed to load board``.  This is the construction-path twin of #4185.

    We inject an actual ``keepout_node()`` (the exact builder used by
    ``kct zones hv-keepout``) into a real, loadable demo board and assert
    ``kicad-cli pcb drc`` still loads it.  The minimal in-module ``_BOARD``
    fixtures are intentionally partial and do not load in kicad-cli on their
    own, so a full real board is required for a meaningful load test (mirrors
    ``tests/test_sexp_roundtrip.py::test_keepout_board_loads_in_kicad``).
    """
    from kicad_tools.sexp.builders import keepout_node
    from kicad_tools.sexp.parser import parse_file

    demo = (
        Path(__file__).parent.parent
        / "boards"
        / "00-simple-led"
        / "output"
        / "simple_led.kicad_pcb"
    )
    if not demo.exists():
        pytest.skip(f"Demo board not found: {demo}")

    doc = parse_file(demo)
    keepout = keepout_node(
        points=[(50, 50), (70, 50), (70, 70), (50, 70)],
        layers=["F.Cu", "B.Cu"],
        no_tracks=False,
        no_vias=False,
        no_pour=True,
        uuid_str="hv-keepout-4430-test",
    )
    doc.children.append(keepout)

    output = doc.to_string()
    # Disposition tokens must be BARE, and no bare (fill yes) on a rule area.
    assert '"not_allowed"' not in output
    assert '"allowed"' not in output
    assert "(copperpour not_allowed)" in output
    assert "(fill yes)" not in output

    out_path = tmp_path / "keepout_board.kicad_pcb"
    out_path.write_text(output)

    # kicad-cli returns 0 on a clean load (or if DRC violations are found); a
    # load failure prints "Failed to load board" and returns non-zero.
    result = subprocess.run(
        [KICAD_CLI, "pcb", "drc", str(out_path), "-o", str(tmp_path / "drc.json")],
        capture_output=True,
        text=True,
    )
    assert "Failed to load board" not in result.stderr, (
        f"kicad-cli failed to load a board carrying the emitted keepout.\n"
        f"rc={result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.returncode == 0, (
        f"kicad-cli pcb drc returned {result.returncode} on the keepout board.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


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


@pytest.mark.parametrize("kind", ["circle", "oval", "via", "trace", "rect", "roundrect"])
@pytest.mark.parametrize("clearance", [0.45, 1.6, 3.2])
@pytest.mark.parametrize("shift,angle", [(0.0, 0.0), (0.0037, 37.0)])
def test_serialized_keepout_preserves_physical_gap(tmp_path, kind, clearance, shift, angle):
    """Measure against analytic circular/capsule copper, including rounding phases."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    board, core, radius = _curved_board(tmp_path, kind, shift, angle)
    pcb = PCB.load(board)
    plan = build_hv_keepout_plan(pcb, {1: "AC_LINE"}, clearance)
    # Test the actual serialized vertices, not just the in-memory buffer.
    polygons = []
    for node in plan.zone_nodes():
        points = node.find("polygon").find("pts")
        polygons.append(Polygon([(p.get_float(0), p.get_float(1)) for p in points.children[1:]]))
    void = unary_union(polygons)
    assert void.contains(core)
    assert void.boundary.distance(core) - radius >= clearance


def _curved_board(tmp_path, kind, shift=0.0037, angle=37.0):
    from math import cos, radians, sin

    from shapely.geometry import LineString, Point

    x, y = 125 + shift, 120 + shift
    radius = 1.0
    dx, dy = cos(radians(angle)), -sin(radians(angle))
    core = Point(x, y)
    if kind == "via":
        copper = f'(via (at {x} {y}) (size 2) (drill 0.6) (layers "F.Cu" "B.Cu") (net 1))'
    elif kind == "trace":
        core = LineString([(x - dx, y - dy), (x + dx, y + dy)])
        copper = f'(segment (start {x - dx} {y - dy}) (end {x + dx} {y + dy}) (width 2) (layer "F.Cu") (net 1))'
    else:
        width = 4 if kind == "oval" else 2
        if kind in ("rect", "roundrect"):
            from shapely.affinity import rotate, translate
            from shapely.geometry import box

            radius = 0.5 if kind == "roundrect" else 0.0
            h = 1 - radius
            core = translate(rotate(box(-h, -h, h, h), -angle), x, y)
        if kind == "oval":
            core = LineString([(x - dx, y - dy), (x + dx, y + dy)])
        copper = f'(footprint "test" (layer "F.Cu") (at {x} {y}) (pad "1" thru_hole {kind} (at 0 0 {angle}) (size {width} 2) (drill 0.6) (roundrect_rratio 0.25) (layers "*.Cu" "*.Mask") (net 1 "AC_LINE")))'
    source = _BOARD.replace('(1 "In1.Cu" signal)', "").replace("In1.Cu", "B.Cu")
    source = source.replace("gnd-plane-uuid", "9a347a26-a984-45ca-b927-9fa431ceebac")
    source = source.replace(
        '(segment (start 110 110) (end 150 110) (width 0.5) (layer "F.Cu") (net 1))', copper
    )
    # Native filler must retain the isolated test pour to permit measurement.
    source = source.replace(
        "(fill yes (thermal_gap", "(fill yes (island_removal_mode 0) (thermal_gap"
    )
    return _write(tmp_path, source), core, radius


@pytest.mark.skipif(
    KICAD_CLI is None,
    reason="find_kicad_cli() found no kicad-cli install (checked PATH and common install locations)",
)
@pytest.mark.parametrize(
    "kind,clearance",
    [
        ("circle", 1.6),
        ("oval", 0.45),
        ("via", 0.8),
        ("trace", 3.2),
        ("rect", 1.6),
        ("roundrect", 1.6),
    ],
)
def test_native_refill_preserves_physical_gap(tmp_path, kind, clearance):
    """Actual native-filled copper must clear an analytic rotated pad/via/trace."""
    from shapely.ops import unary_union

    from kicad_tools.validate.connectivity import ConnectivityValidator

    board, core, radius = _curved_board(tmp_path, kind)
    assert zones_main(["hv-keepout", str(board), "--clearance", str(clearance), "-q"]) == 0
    result = subprocess.run(
        [
            KICAD_CLI,
            "pcb",
            "drc",
            str(board),
            "--output",
            str(tmp_path / "drc.json"),
            "--format",
            "json",
            "--refill-zones",
            "--save-board",
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    pcb = PCB.load(board)
    ox, oy = pcb.board_origin
    from shapely.affinity import translate

    fills = [
        ConnectivityValidator._fill_solid_region(pts)
        for zone in pcb.zones
        if zone.net_name == "GND"
        for pts in zone.filled_polygons
    ]
    assert fills, "Native refill must produce real copper, not vacuous clearance"
    fill = translate(unary_union(fills), ox, oy)
    assert fill.area > 100, "The surrounding pour must survive"
    assert fill.distance(core) - radius >= clearance


def test_rounding_and_inscribed_buffer_are_distinct_error_sources(tmp_path):
    """A straight boundary loses precision; an unrounded curved buffer loses sagitta."""
    from shapely.geometry import Point, Polygon

    from kicad_tools.sexp.builders import keepout_node

    # A straight-sided exact offset at a non-centimal phase: no curved
    # approximation is involved, yet the shared serializer moves it inward.
    node = keepout_node([(0, 0), (2.0037, 0), (2.0037, 2), (0, 2)], ["F.Cu"])
    pts = node.find("polygon").find("pts")
    saved = Polygon([(p.get_float(0), p.get_float(1)) for p in pts.children[1:]])
    assert saved.bounds[2] == 2.0
    assert saved.bounds[2] < 2.0037

    # Even before serialization, a point's buffer is inscribed in the true
    # circle. This separately witnesses the curvature term of the bound.
    disc = Point(0, 0).buffer(1.6, quad_segs=16)
    assert disc.boundary.distance(Point(0, 0)) < 1.6
