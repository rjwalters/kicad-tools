"""`scripts/research/expand_route_experiment.py` geometry and bookkeeping (Issue #6304).

The numbers in ``docs/research/expand-route-shrink-experiment.md`` are only as
good as three pieces of arithmetic, pinned here without routing anything:

* the placement scale moves footprints, the outline and zone outlines -- and
  nothing inside a footprint -- and refuses boards it cannot scale honestly;
* rigid clustering keeps a decoupling cap with the IC it is wired to;
* the Phase 2 gap count charges a gap only for copper that crosses it on a
  layer both obstacles occupy.

The script lives under ``scripts/`` (not ``src/``), so it is loaded by path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "research" / "expand_route_experiment.py"


def _load():
    spec = importlib.util.spec_from_file_location("expand_route_experiment_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ere = _load()


def _footprint(ref: str, x: float, y: float, pads: list[tuple[str, float, float, str]]) -> str:
    """A footprint with 1x1 mm SMD pads on F.Cu; pads are (number, dx, dy, net)."""
    body = "".join(
        f'\n    (pad "{num}" smd rect (at {dx} {dy}) (size 1 1) '
        f'(layers "F.Cu" "F.Paste" "F.Mask") (net 1 "{net}"))'
        for num, dx, dy, net in pads
    )
    return (
        f'\n  (footprint "lib:{ref}" (layer "F.Cu") (at {x} {y})'
        f'\n    (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS")){body}\n  )'
    )


def _board(footprints: str, extra: str = "") -> str:
    return (
        '(kicad_pcb (version 20240108) (generator "test")\n'
        '  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))\n'
        '  (net 0 "") (net 1 "A")\n'
        "  (gr_rect (start 0 0) (end 40 20) (stroke (width 0.1) (type default)) (fill none)"
        ' (layer "Edge.Cuts"))'
        f"{footprints}{extra}\n)\n"
    )


def _tree(text: str):
    from kicad_tools.sexp import parse_string

    return parse_string(text)


def _pcb(tmp_path: Path, name: str, text: str):
    from kicad_tools.schema.pcb import PCB

    path = tmp_path / name
    path.write_text(text)
    return PCB.load(str(path))


def _at(tree, index: int) -> tuple[float, float]:
    fps = [c for c in tree.iter_children() if c.tag == "footprint"]
    at = fps[index].find_child("at")
    return float(at.get_value(0)), float(at.get_value(1))


TWO_PARTS = _footprint("U1", 10, 10, [("1", -1, 0, "A"), ("2", 1, 0, "B")]) + _footprint(
    "R1", 30, 14, [("1", -1, 0, "A"), ("2", 1, 0, "C")]
)


# ---------------------------------------------------------------------------
# Placement scaling
# ---------------------------------------------------------------------------


def test_uniform_scale_moves_footprints_about_the_outline_centre():
    tree = _tree(_board(TWO_PARTS))
    report = ere.scale_board_tree(tree, 2.0)

    # Outline 40x20 centred on (20, 10): (10,10) -> (0,10), (30,14) -> (40,18).
    assert _at(tree, 0) == pytest.approx((0.0, 10.0))
    assert _at(tree, 1) == pytest.approx((40.0, 18.0))
    assert report.origin == pytest.approx((20.0, 10.0))
    assert report.footprints_moved == 2
    assert report.outline_mm == pytest.approx((80.0, 40.0))
    assert ere.outline_bbox(tree) == pytest.approx((-20.0, -10.0, 60.0, 30.0))


def test_scaling_leaves_everything_inside_a_footprint_alone():
    """Pad pitch is the thing that must NOT scale -- it is the experiment's control."""
    tree = _tree(_board(TWO_PARTS))
    ere.scale_board_tree(tree, 2.0)

    fp = next(c for c in tree.iter_children() if c.tag == "footprint")
    pad_ats = [
        (float(p.find_child("at").get_value(0)), float(p.find_child("at").get_value(1)))
        for p in fp.find_children("pad")
    ]
    assert pad_ats == [(-1.0, 0.0), (1.0, 0.0)]
    assert fp.find_child("pad").find_child("size").get_value(0) == 1


def test_scale_of_one_is_the_identity():
    tree = _tree(_board(TWO_PARTS))
    ere.scale_board_tree(tree, 1.0)
    assert _at(tree, 0) == pytest.approx((10.0, 10.0))
    assert _at(tree, 1) == pytest.approx((30.0, 14.0))
    assert ere.outline_bbox(tree) == pytest.approx((0.0, 0.0, 40.0, 20.0))


def test_zone_outline_scales_and_stale_fill_is_dropped():
    zone = (
        '\n  (zone (net 1) (net_name "A") (layer "F.Cu")'
        "\n    (polygon (pts (xy 0 0) (xy 40 0) (xy 40 20) (xy 0 20)))"
        '\n    (filled_polygon (layer "F.Cu") (pts (xy 1 1) (xy 39 1) (xy 39 19) (xy 1 19)))\n  )'
    )
    tree = _tree(_board(TWO_PARTS, zone))
    report = ere.scale_board_tree(tree, 1.5)

    zone_node = next(c for c in tree.iter_children() if c.tag == "zone")
    pts = [
        (float(xy.get_value(0)), float(xy.get_value(1)))
        for xy in zone_node.find_child("polygon").find_child("pts").find_children("xy")
    ]
    assert pts == pytest.approx([(-10, -5), (50, -5), (50, 25), (-10, 25)])
    assert zone_node.find_child("filled_polygon") is None
    assert (report.zones_scaled, report.fills_dropped) == (1, 1)


def test_board_with_copper_is_refused():
    """Trace endpoints sit on pads, which do not scale -- an affine map tears them off."""
    seg = '\n  (segment (start 9 10) (end 29 14) (width 0.2) (layer "F.Cu") (net 1))'
    with pytest.raises(ere.ScaleError, match="routed copper"):
        ere.scale_board_tree(_tree(_board(TWO_PARTS, seg)), 2.0)


def test_unrecognised_top_level_node_is_refused_not_guessed():
    with pytest.raises(ere.ScaleError, match="unrecognised"):
        ere.scale_board_tree(_tree(_board(TWO_PARTS, "\n  (mystery (at 1 2))")), 2.0)


def test_round_outline_refuses_a_non_uniform_scale():
    circle = (
        "\n  (gr_circle (center 20 10) (end 25 10) (stroke (width 0.1) (type default))"
        ' (layer "Edge.Cuts"))'
    )
    tree = _tree(_board(TWO_PARTS, circle))
    with pytest.raises(ere.ScaleError, match="non-uniformly"):
        ere.scale_board_tree(tree, 2.0, 1.0)
    ere.scale_board_tree(_tree(_board(TWO_PARTS, circle)), 2.0)  # uniform is fine


def test_non_positive_scale_and_bad_mode_are_refused():
    with pytest.raises(ere.ScaleError):
        ere.scale_board_tree(_tree(_board(TWO_PARTS)), 0.0)
    with pytest.raises(ere.ScaleError, match="unknown mode"):
        ere.scale_board_tree(_tree(_board(TWO_PARTS)), 2.0, mode="elastic")


def test_rigid_mode_translates_a_cluster_without_stretching_it():
    tree = _tree(_board(TWO_PARTS + _footprint("C1", 12, 10, [("1", 0, 0, "A")])))
    # U1 (index 0) and C1 (index 2) ride together; R1 (index 1) is alone.
    report = ere.scale_board_tree(tree, 2.0, mode="rigid", clusters=[[0, 2], [1]])

    u1, r1, c1 = _at(tree, 0), _at(tree, 1), _at(tree, 2)
    assert (c1[0] - u1[0], c1[1] - u1[1]) == pytest.approx((2.0, 0.0))  # 1x spacing kept
    # Cluster centroid (11, 10) maps to (2, 10): a translation of (-9, 0).
    assert u1 == pytest.approx((1.0, 10.0))
    assert r1 == pytest.approx((40.0, 18.0))  # a cluster of one scales as in uniform mode
    assert (report.clusters, report.clustered_footprints) == (2, 2)


def test_rigid_mode_requires_an_exact_partition():
    with pytest.raises(ere.ScaleError, match="partition"):
        ere.scale_board_tree(_tree(_board(TWO_PARTS)), 2.0, mode="rigid", clusters=[[0]])
    with pytest.raises(ere.ScaleError, match="needs clusters"):
        ere.scale_board_tree(_tree(_board(TWO_PARTS)), 2.0, mode="rigid")


# ---------------------------------------------------------------------------
# Rigid clustering
# ---------------------------------------------------------------------------


def _ic(ref: str, x: float, y: float, net_prefix: str) -> str:
    pads = [(str(i + 1), -2.5 + i, 0.0, f"{net_prefix}{i}") for i in range(6)]
    return _footprint(ref, x, y, pads)


def test_cap_joins_the_ic_it_is_wired_to_and_only_that_one(tmp_path):
    parts = (
        _ic("U1", 10, 10, "N")
        + _footprint("C1", 10, 12, [("1", -0.6, 0, "N0"), ("2", 0.6, 0, "GND")])  # wired, near
        + _footprint("R1", 12, 12.5, [("1", -0.6, 0, "X"), ("2", 0.6, 0, "Y")])  # near, unwired
        + _footprint("C2", 30, 10, [("1", -0.6, 0, "N1"), ("2", 0.6, 0, "GND")])  # wired, far
    )
    clusters = ere.rigid_clusters(_pcb(tmp_path, "c.kicad_pcb", _board(parts)))
    assert clusters == [[0, 1], [2], [3]]


def test_cap_between_two_ics_joins_the_nearer_one(tmp_path):
    parts = (
        _ic("U1", 10, 5, "N")
        + _ic("U2", 10, 15, "N")
        + _footprint("C1", 10, 12.6, [("1", -0.6, 0, "N0"), ("2", 0.6, 0, "GND")])
    )
    clusters = ere.rigid_clusters(_pcb(tmp_path, "c.kicad_pcb", _board(parts)))
    assert clusters == [[0], [1, 2]]


# ---------------------------------------------------------------------------
# Gap geometry
# ---------------------------------------------------------------------------


def test_rect_gap_and_cut():
    a, b = (0, 0, 2, 2), (5, 1, 7, 3)
    assert ere.rect_gap(a, b) == pytest.approx(3.0)
    assert ere.rect_cut(a, b) == ((2, 1.5), (5, 1.5))  # middle of the y overlap [1, 2]
    diagonal = (5, 6, 7, 8)
    assert ere.rect_gap(a, diagonal) == pytest.approx(5.0)
    assert ere.rect_cut(a, diagonal) == ((2, 2), (5, 6))
    assert ere.rect_gap(a, (1, 1, 3, 3)) == 0.0


def test_demand_charges_a_clearance_each_side_and_between_traces():
    assert ere._demand_mm([], 0.15) == 0.0
    assert ere._demand_mm([0.2], 0.15) == pytest.approx(0.5)
    assert ere._demand_mm([0.2, 0.2, 0.2], 0.15) == pytest.approx(1.2)


def _gap_board(spacing: float, segments: str = "") -> str:
    """Two single-pad parts whose 1x1 pads face each other across ``spacing - 1`` mm."""
    left = _footprint("P1", 10, 10, [("1", 0, 0, "A")])
    right = _footprint("P2", 10 + spacing, 10, [("1", 0, 0, "B")])
    return _board(left + right, segments)


def _vertical(x: float, net: int, name: str, layer: str = "F.Cu") -> str:
    return f'\n  (segment (start {x} 5) (end {x} 15) (width 0.2) (layer "{layer}") (net {net} "{name}"))'


def test_gap_overflows_when_expanded_traffic_would_not_fit_at_1x(tmp_path):
    # 1x: pads 1.6 mm apart -> a 0.6 mm gap. Expanded: 4.6 mm apart -> 3.6 mm gap
    # carrying three 0.2 mm traces, which need 3*0.2 + 4*0.15 = 1.2 mm.
    one_x = _pcb(tmp_path, "one.kicad_pcb", _gap_board(1.6))
    traffic = _vertical(11.5, 2, "T1") + _vertical(12.3, 3, "T2") + _vertical(13.1, 4, "T3")
    routed = _pcb(tmp_path, "big.kicad_pcb", _gap_board(4.6, traffic))

    report = ere.gap_overflow(one_x, routed, clearance_mm=0.15)

    assert (report.gaps_examined, len(report.loaded)) == (1, 1)
    assert len(report.overflow) == 1 and report.through_overflow == 1
    finding = report.overflow[0]
    assert (finding.a, finding.b, finding.layer, finding.traces) == ("P1", "P2", "F.Cu", 3)
    assert finding.gap_1x_mm == pytest.approx(0.6)
    assert finding.demand_mm == pytest.approx(1.2)


def test_gap_pairs_are_matched_by_reference_not_document_order(tmp_path):
    """`kct route` re-emits footprints in a different order than it read them.

    Matching by index paired each 1x footprint with an unrelated routed one and
    reported 14 "over-capacity" gaps on a DRC-clean 1x board measured against
    itself -- which is how the bug was caught.
    """
    one_x = _pcb(tmp_path, "one.kicad_pcb", _gap_board(1.6))
    left = _footprint("P1", 10, 10, [("1", 0, 0, "A")])
    right = _footprint("P2", 14.6, 10, [("1", 0, 0, "B")])
    far = _footprint("P3", 30, 3, [("1", 0, 0, "C")])
    reordered = _board(far + right + left, _vertical(12.3, 2, "T1"))
    report = ere.gap_overflow(one_x, _pcb(tmp_path, "big.kicad_pcb", reordered), clearance_mm=0.15)
    assert (report.gaps_examined, len(report.loaded)) == (1, 1)
    assert report.overflow == []


def test_gap_that_still_fits_at_1x_is_not_flagged(tmp_path):
    one_x = _pcb(tmp_path, "one.kicad_pcb", _gap_board(1.6))
    routed = _pcb(tmp_path, "big.kicad_pcb", _gap_board(4.6, _vertical(12.3, 2, "T1")))
    report = ere.gap_overflow(one_x, routed, clearance_mm=0.15)
    assert len(report.loaded) == 1
    assert report.overflow == [] and report.through_overflow == 0  # 0.5 mm fits in 0.6 mm


def test_traffic_on_a_layer_the_obstacles_do_not_occupy_is_free(tmp_path):
    """Two SMD parts do not constrict B.Cu, however much runs under them."""
    one_x = _pcb(tmp_path, "one.kicad_pcb", _gap_board(1.6))
    under = "".join(_vertical(11.5 + 0.8 * i, 2 + i, f"T{i}", "B.Cu") for i in range(3))
    routed = _pcb(tmp_path, "big.kicad_pcb", _gap_board(4.6, under))
    report = ere.gap_overflow(one_x, routed, clearance_mm=0.15)
    assert report.loaded == [] and report.overflow == []


def test_own_net_traffic_is_separated_from_through_traffic(tmp_path):
    """A gap over capacity only because of its own escapes is not a re-topology gap."""
    one_x = _pcb(tmp_path, "one.kicad_pcb", _gap_board(1.6))
    own = _vertical(11.5, 1, "A") + _vertical(12.3, 2, "B") + _vertical(13.1, 3, "T1")
    routed = _pcb(tmp_path, "big.kicad_pcb", _gap_board(4.6, own))
    report = ere.gap_overflow(one_x, routed, clearance_mm=0.15)
    assert len(report.overflow) == 1
    assert report.overflow[0].through_traces == 1
    assert report.through_overflow == 0


def test_gap_blocked_by_a_third_part_at_1x_is_not_a_gap(tmp_path):
    def board(spacing: float, extra: str = "") -> str:
        parts = (
            _footprint("P1", 10, 10, [("1", 0, 0, "A")])
            + _footprint("P2", 10 + spacing, 10, [("1", 0, 0, "B")])
            + _footprint("P3", 10 + spacing / 2, 10, [("1", 0, 0, "C")])
        )
        return _board(parts, extra)

    report = ere.gap_overflow(
        _pcb(tmp_path, "one.kicad_pcb", board(4.0)),
        _pcb(tmp_path, "big.kicad_pcb", board(8.0)),
        clearance_mm=0.15,
    )
    # P1-P3 and P3-P2 are gaps; P1-P2 has P3 in the way and is not examined.
    assert report.gaps_examined == 2


# ---------------------------------------------------------------------------
# Run bookkeeping
# ---------------------------------------------------------------------------


def test_grid_policies():
    # pinned: the same explicit grid at every scale, 1x included.
    assert ere.grid_args(1.0, "pinned", 0.05) == ["--grid", "0.05"]
    assert ere.grid_args(2.0, "pinned", 0.065) == ["--grid", "0.065"]
    with pytest.raises(ValueError, match="needs --grid-mm"):
        ere.grid_args(2.0, "pinned")
    # area: cells per mm^2 held constant; nothing extra at 1x.
    assert ere.grid_args(1.0, "area") == []
    assert ere.grid_args(2.0, "area") == ["--max-cells", "2000000"]
    assert ere.grid_args(1.5, "area") == ["--max-cells", "1125000"]
    assert ere.grid_args(2.0, "default") == []
    with pytest.raises(ValueError, match="unknown grid policy"):
        ere.grid_args(2.0, "bogus")


def test_route_log_facts_are_extracted():
    log = (
        'Clearance: 0.15mm (rules: project netclass "Default")\n'
        "  Coarse grid: 0.050mm\n  Total cell estimate: 2,124,454\n"
        "  Nets routed:     8/33\n  Nets routed: 18/33 (55%)\n"
        "PARTIAL: routing deadline exceeded during routing; report: x.json\n  Layer count: 4\n"
    )
    facts = ere.parse_route_log(log)
    assert facts == {
        "clearance_mm": 0.15,
        "coarse_grid_mm": 0.05,
        "cell_estimate": 2124454,
        "layers_used": 4,
        "router_nets": "18/33",
        "hit_timeout": True,
    }
    assert ere.parse_route_log("nothing relevant")["hit_timeout"] is False
    # A late stage cut short by the budget is a timeout too, even on exit 4.
    cut = "  23 -> 23 unconnected pour link(s) (deadline)\n"
    assert ere.parse_route_log(cut)["hit_timeout"] is True


def test_deadline_best_so_far_is_graded_and_labelled(tmp_path):
    import json

    routed = tmp_path / "b_routed.kicad_pcb"
    assert ere.pick_artifact(routed) == (None, None)

    partial = tmp_path / "b_routed_partial.kicad_pcb"
    partial.write_text("(kicad_pcb)")
    assert ere.pick_artifact(routed) == (partial, "deadline")

    # A raw partial (deadline inside the routing stage) wins over the sidecar's
    # named snapshot, which in that case carries no copper at all.
    sidecar = tmp_path / "b_routed.timeout.json"
    named = tmp_path / "b_routed_timeout_unverified_abc.kicad_pcb"
    named.write_text("(kicad_pcb)")
    sidecar.write_text(json.dumps({"unverified_output": str(named)}))
    assert ere.pick_artifact(routed) == (partial, "deadline")
    # Deadline in a later stage: no partial, so the sidecar's checkpoint is it.
    partial.unlink()
    assert ere.pick_artifact(routed) == (named, "deadline")
    named.unlink()
    assert ere.pick_artifact(routed) == (None, None)  # sidecar names a missing file
    named.write_text("(kicad_pcb)")

    routed.write_text("(kicad_pcb)")
    assert ere.pick_artifact(routed) == (routed, "final")  # the real output wins
    assert ere._timeout_cell({"artifact": "deadline"}) == "yes (graded best-so-far)"
    assert ere._timeout_cell({"hit_timeout": True, "artifact": "final"}) == "yes"
    assert ere._timeout_cell({"artifact": "final"}) == "no"


def test_stuck_transitions_follow_each_net_by_its_1x_class():
    rows = [
        {
            "board": "x",
            "mode": "uniform",
            "scale": 1.0,
            "stuck_nets": {"a": "ESCAPE_BLOCKED", "b": "PLACEMENT_BOUND", "c": "PLACEMENT_BOUND"},
        },
        {
            "board": "x",
            "mode": "uniform",
            "scale": 2.0,
            "stuck_nets": {"a": "ESCAPE_BLOCKED", "c": "BUDGET_STARVED", "d": "BUDGET_STARVED"},
        },
        {"board": "y", "mode": "uniform", "scale": 2.0, "stuck_nets": {}},  # no 1x row: skipped
        {"board": "x", "mode": "uniform", "scale": 1.5, "error": "no routed output"},
    ]
    (only,) = ere.stuck_transitions(rows)
    assert only["by_1x_class"] == {
        "ESCAPE_BLOCKED": {"still stuck": 1},
        "PLACEMENT_BOUND": {"completed": 1, "still stuck": 1},
    }
    assert only["newly_stuck"] == {"d": "BUDGET_STARVED"}
    table = ere.render_transitions([only])
    # "Newly stuck" belongs to the board at that scale: printed once, not per class.
    assert "| x | uniform | 2 | ESCAPE_BLOCKED | 1 | 0 | 1 | BUDGET_STARVED 1 |" in table
    assert "| x | uniform | 2 | PLACEMENT_BOUND | 2 | 1 | 1 |  |" in table


def test_route_table_renders_errors_without_inventing_numbers():
    rows = [
        {"board": "x", "mode": "uniform", "scale": 2.0, "error": "could not scale: copper"},
        {
            "board": "x",
            "mode": "uniform",
            "scale": 1.0,
            "scale_report": {"outline_mm": [40.0, 20.0]},
            "nets_complete": 3,
            "nets_total": 4,
            "connections_routed": 7,
            "connections_total": 9,
            "kct_check_errors": 0,
            "kicad_drc_errors": None,
            "via_count": 2,
            "wirelength_mm": 12.5,
            "coarse_grid_mm": 0.05,
            "wall_s": 3.2,
            "stuck_counts": {"ESCAPE_BLOCKED": 1, "PLACEMENT_BOUND": 0},  # zeros are dropped
            "signal_nets_unfinished": 1,
        },
    ]
    table = ere.render_route(rows)
    assert "ERROR: could not scale: copper" in table
    # An engine that did not run renders as None, never as a clean 0.
    assert (
        "| 3/4 | 1 | -- | 7/9 | 0 | None | 2 | 12.5 | 0.05 | no | 3.2 | ESCAPE_BLOCKED 1 |" in table
    )


def test_in_tree_boards_resolve_and_chorus_override_is_honoured(tmp_path):
    assert ere.resolve_board_path(ere.BOARDS["04"]).name == "stm32_devboard.kicad_pcb"
    fake = tmp_path / "chorus.kicad_pcb"
    assert ere.resolve_board_path(ere.BOARDS["chorus"], fake) is None  # missing override
    fake.write_text("(kicad_pcb)")
    assert ere.resolve_board_path(ere.BOARDS["chorus"], fake) == fake


def test_fleet_boards_scale_without_error():
    """Every in-tree board the note reports on must actually be scalable."""
    from kicad_tools.core.sexp_file import load_pcb

    for key in ("00", "01", "02", "03", "04", "05"):
        tree = load_pcb(ere.resolve_board_path(ere.BOARDS[key]))
        report = ere.scale_board_tree(tree, 2.0)
        assert report.footprints_moved > 0, key
