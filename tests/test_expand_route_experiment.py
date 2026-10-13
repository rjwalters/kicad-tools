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
        "cache_hit": False,
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


# ---------------------------------------------------------------------------
# Issue #6312: sidecar facts, load/seed, vias per net, cluster-rule flags
# ---------------------------------------------------------------------------


def test_timeout_sidecar_facts_are_copied_and_missing_keys_are_not_invented(tmp_path):
    import json

    routed = tmp_path / "b_routed.kicad_pcb"
    assert ere.timeout_sidecar_facts(routed) == {}  # no deadline: no sidecar

    sidecar = tmp_path / "b_routed.timeout.json"
    sidecar.write_text(
        json.dumps(
            {
                "stage": "partial-save",
                "snapshot_state": "last-accepted",
                "snapshot_placement": "source",
                "snapshot_discarded_trial": "C4 nudge",
                "status": "partial",
                "timeout_seconds": 1200,
            }
        )
    )
    assert ere.timeout_sidecar_facts(routed) == {
        "timeout_sidecar": True,
        "timeout_stage": "partial-save",
        "snapshot_state": "last-accepted",
        "snapshot_placement": "source",
        "snapshot_discarded_trial": "C4 nudge",
    }

    # A sidecar from before PR #6323 has no snapshot fields. Their absence must
    # stay an absence: defaulting to "live" would vouch for an unchecked board.
    sidecar.write_text(json.dumps({"stage": "routing"}))
    assert ere.timeout_sidecar_facts(routed) == {
        "timeout_sidecar": True,
        "timeout_stage": "routing",
    }
    # The worker was killed before it published the partial-save stage: a
    # _partial board may exist, but nothing says what it holds.
    sidecar.write_text(json.dumps({"stage": "placement-delta-feedback", "snapshot_saved": False}))
    assert ere.timeout_sidecar_facts(routed) == {
        "timeout_sidecar": True,
        "timeout_stage": "placement-delta-feedback",
        "snapshot_saved": False,
    }
    # An explicit null (no trial discarded) is a fact and is kept.
    sidecar.write_text(json.dumps({"stage": "routing", "snapshot_discarded_trial": None}))
    assert ere.timeout_sidecar_facts(routed)["snapshot_discarded_trial"] is None

    sidecar.write_text("not json")
    assert ere.timeout_sidecar_facts(routed) == {}
    sidecar.write_text("[1, 2]")
    assert ere.timeout_sidecar_facts(routed) == {}


def test_vias_are_counted_per_net(tmp_path):
    vias = (
        '\n  (via (at 5 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1 "A"))'
        '\n  (via (at 6 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1 "A"))'
        '\n  (via (at 7 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2 "B"))'
    )
    text = _board(TWO_PARTS, vias).replace('(net 1 "A")\n', '(net 1 "A") (net 2 "B")\n', 1)
    assert ere.vias_by_net(_pcb(tmp_path, "v.kicad_pcb", text)) == {"A": 2, "B": 1}
    assert ere.vias_by_net(_pcb(tmp_path, "n.kicad_pcb", _board(TWO_PARTS))) == {}


def test_vias_per_connection_divides_by_routed_connections(tmp_path, monkeypatch):
    """The ratio is over *routed* connections, and is None when there are none.

    A deadline row can lose a third of its connections (board 05 ``rigid``
    1.5x routed 88 of 142, Issue #6312); dividing by the total would make such
    a row look via-sparse instead of saying what its routes cost.
    """
    from types import SimpleNamespace

    from kicad_tools.benchmark.external import metrics

    routed_connections = {"n": 4}

    def as_dict(**fields):
        return SimpleNamespace(to_dict=lambda: fields)

    monkeypatch.setattr(
        metrics,
        "measure_completion",
        lambda _routed: as_dict(
            nets_complete=1,
            nets_total=2,
            nets_blocking_incomplete=1,
            connections_routed=routed_connections["n"],
            connections_total=10,
        ),
    )
    monkeypatch.setattr(
        metrics, "measure_copper", lambda _routed: as_dict(via_count=6, wirelength_mm=12.34)
    )
    monkeypatch.setattr(
        metrics,
        "run_kct_check",
        lambda _routed, **_kw: SimpleNamespace(ran=True, error_count=0, errors_by_rule={}),
    )
    monkeypatch.setattr(
        metrics,
        "run_kicad_cli_drc",
        lambda _routed, **_kw: SimpleNamespace(ran=True, violation_count=0, by_type={}, note=""),
    )
    routed = tmp_path / "r.kicad_pcb"
    routed.write_text(_board(TWO_PARTS))
    board = ere.Board("x", "r.kicad_pcb")

    row = ere.grade(routed, board)
    assert row["via_count"] == 6
    assert (row["connections_routed"], row["connections_total"]) == (4, 10)
    assert row["vias_per_connection"] == 1.5  # 6 / 4 routed, not 6 / 10 total

    routed_connections["n"] = 0
    assert ere.grade(routed, board)["vias_per_connection"] is None


def _stub_route(monkeypatch, *, sidecar: dict | None = None, graded: list | None = None):
    """Replace the router and the graders so ``run_one`` can be tested offline."""
    import json

    def fake_route(inp, out, extra, timeout, log):
        log.write_text("  Coarse grid: 0.065mm\n")
        if sidecar is None:
            out.write_text(inp.read_text())
        else:
            out.with_name(f"{out.stem}_partial.kicad_pcb").write_text(inp.read_text())
            out.with_suffix(".timeout.json").write_text(json.dumps(sidecar))
        return {"cmd": " ".join(extra), "exit_code": 0 if sidecar is None else 124, "wall_s": 0.1}

    def fake_grade(routed, board):
        if graded is not None:
            graded.append(routed.name)
        return {"nets_complete": 1, "nets_total": 1}

    monkeypatch.setattr(ere, "run_route", fake_route)
    monkeypatch.setattr(ere, "grade", fake_grade)


def test_row_records_seed_and_load_at_both_ends(tmp_path, monkeypatch):
    _stub_route(monkeypatch)
    loads = iter([(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)])
    monkeypatch.setattr(ere.os, "getloadavg", lambda: next(loads))
    src = tmp_path / "b.kicad_pcb"
    src.write_text(_board(TWO_PARTS))

    row = ere.run_one(ere.Board("x", "b.kicad_pcb"), src, 1.5, "uniform", tmp_path / "w", 43, 60)

    assert row["seed"] == 43
    assert "--seed 43" in row["cmd"]
    assert row["loadavg_start"] == [1.0, 2.0, 3.0]
    assert row["loadavg_end"] == [4.0, 5.0, 6.0]
    assert row["artifact"] == "final"
    assert "snapshot_state" not in row and "timeout_sidecar" not in row


def test_deadline_row_carries_the_sidecar_snapshot_state(tmp_path, monkeypatch):
    graded: list[str] = []
    _stub_route(
        monkeypatch,
        sidecar={
            "stage": "partial-save",
            "snapshot_state": "inconsistent",
            "snapshot_placement": "moved",
            "snapshot_discarded_trial": None,
        },
        graded=graded,
    )
    src = tmp_path / "b.kicad_pcb"
    src.write_text(_board(TWO_PARTS))

    row = ere.run_one(ere.Board("x", "b.kicad_pcb"), src, 1.0, "uniform", tmp_path / "w", 42, 60)

    # Read even though a _partial board exists -- the case the harness used to
    # skip, and the one where the snapshot can be inconsistent (Issue #6314).
    assert graded == ["b_routed_partial.kicad_pcb"]
    assert row["artifact"] == "deadline"
    assert row["snapshot_state"] == "inconsistent"
    assert row["snapshot_placement"] == "moved"
    assert row["snapshot_discarded_trial"] is None
    assert row["timeout_stage"] == "partial-save"


def _cluster_parts() -> str:
    """A 6-pad IC, a 4-pad regulator, and one wired cap beside each."""
    reg = _footprint("U2", 30, 10, [(str(i + 1), -1.5 + i, 0.0, f"R{i}") for i in range(4)])
    return (
        _ic("U1", 10, 10, "N")
        + reg
        + _footprint("C1", 10, 12, [("1", -0.6, 0, "N0"), ("2", 0.6, 0, "GND")])
        + _footprint("C2", 30, 12, [("1", -0.6, 0, "R0"), ("2", 0.6, 0, "GND")])
        + _footprint("C3", 30, 16, [("1", -0.6, 0, "R1"), ("2", 0.6, 0, "GND")])
    )


def test_cluster_report_shows_the_funnel_and_the_shared_nets(tmp_path):
    pcb = _pcb(tmp_path, "c.kicad_pcb", _board(_cluster_parts()))

    default = ere.cluster_report(pcb)
    assert default["rule"] == {"anchor_min_pads": 6, "satellite_max_pads": 2, "max_gap_mm": 3.0}
    assert default["pad_count_histogram"] == {"2": 3, "4": 1, "6": 1}
    assert (default["footprints"], default["anchors"], default["small_parts"]) == (5, 1, 3)
    # Under the default rule the 4-pad regulator is neither anchor nor satellite.
    assert default["small_parts_near_and_sharing_a_net"] == 1
    assert (default["multi_part_clusters"], default["cluster_sizes"]) == (1, [2])
    assert default["clusters"] == [{"anchor": "U1", "anchor_pads": 6, "satellites": {"C1": ["N0"]}}]

    # Lowering the anchor threshold admits the regulator and its near cap ...
    lowered = ere.cluster_report(pcb, ere.ClusterRule(anchor_min_pads=3))
    assert (lowered["anchors"], lowered["multi_part_clusters"]) == (2, 2)
    assert lowered["footprints_in_multi_part_clusters"] == 4
    # ... and widening the gap admits the far one.
    wide = ere.cluster_report(pcb, ere.ClusterRule(anchor_min_pads=3, max_gap_mm=6.0))
    assert wide["cluster_sizes"] == [3, 2]
    assert wide["clusters"][1]["satellites"] == {"C2": ["R0"], "C3": ["R1"]}
    # The report and the clustering the scaler applies must agree.
    assert (
        sum(1 for g in ere.rigid_clusters(pcb, anchor_min_pads=3, max_gap_mm=6.0) if len(g) > 1)
        == 2
    )

    table = ere.render_clusters(
        [{"board": "x", **wide}, {"board": "y", "error": "input not found"}]
    )
    assert "| x | 3, 2, 6 | 5 | 2 | 3 | 3 | 3 | 2 (3, 2) | 5 |" in table
    assert "| y | -- | ERROR: input not found |" in table


def test_rigid_scaling_uses_and_records_the_cluster_rule(tmp_path):
    src = tmp_path / "c.kicad_pcb"
    src.write_text(_board(_cluster_parts()))

    _, default = ere.write_scaled_board(src, tmp_path / "a", 1.5, "rigid")
    assert default.clustered_footprints == 2
    assert default.to_dict()["cluster_rule"] == ere.ClusterRule().to_dict()

    rule = ere.ClusterRule(anchor_min_pads=3, max_gap_mm=6.0)
    _, revised = ere.write_scaled_board(src, tmp_path / "b", 1.5, "rigid", rule)
    assert revised.clustered_footprints == 5
    assert revised.to_dict()["cluster_rule"] == {
        "anchor_min_pads": 3,
        "satellite_max_pads": 2,
        "max_gap_mm": 6.0,
    }
    # No rule is applied in uniform mode, so none is claimed.
    _, uniform = ere.write_scaled_board(src, tmp_path / "c", 1.5, "uniform", rule)
    assert uniform.to_dict()["cluster_rule"] is None


def test_cluster_flags_reach_the_route_and_clusters_commands(tmp_path, monkeypatch, capsys):
    seen: list = []

    def fake_run_one(*args):
        seen.append(args)
        return {"board": "05", "scale": 1.5, "mode": "rigid", "error": "stub"}

    monkeypatch.setattr(ere, "run_one", fake_run_one)
    argv = ["route", "--boards", "05", "--scales", "1.5", "--modes", "rigid"]
    argv += ["--work-dir", str(tmp_path), "--seed", "44", "--anchor-min-pads", "3"]
    # A single-token --extra must use the = form; argparse rejects the other.
    argv += ["--max-gap-mm", "5", "--extra=--no-placement-delta-feedback"]
    assert ere.main(argv) == 0
    (call,) = seen
    assert call[5] == 44
    assert call[-2] == ("--no-placement-delta-feedback",)
    assert call[-1] == ere.ClusterRule(3, 2, 5.0)
    with pytest.raises(SystemExit):
        ere.main([*argv[:-1], "--extra", "--no-placement-delta-feedback"])
    capsys.readouterr()

    # The no-route report on the in-tree boards, at the rule the note used.
    out = tmp_path / "clusters.json"
    assert ere.main(["clusters", "--boards", "03,05", "--json", str(out), "--members"]) == 0
    printed = capsys.readouterr().out
    assert "| 03 | 6, 2, 3 | 38 | 5 | 27 | 12 | 12 | 4 (9, 3, 2, 2) | 16 |" in printed
    assert "| 05 | 6, 2, 3 | 42 | 5 | 35 | 10 | 10 | 4 (5, 5, 2, 2) | 14 |" in printed
    assert "05: U1 (32 pads) <- " in printed
    import json

    rows = json.loads(out.read_text())["rows"]
    assert [r["footprints_in_multi_part_clusters"] for r in rows] == [16, 14]
    with pytest.raises(SystemExit, match="unknown board"):
        ere.main(["clusters", "--boards", "nope"])


def test_quoted_deadline_marker_in_the_startup_warning_is_not_a_timeout():
    """Every --deterministic-budget run prints this warning; none of them timed out for it."""
    warning = (
        "[deterministic-budget] WARNING: --timeout 1200s is set.  If it fires, the run is no "
        "longer machine-independent; a '[deterministic-budget] stage deadline fired' log line "
        "will mark exactly when that happens.\n"
    )
    assert ere.parse_route_log(warning)["hit_timeout"] is False
    fired = warning + "[deterministic-budget] stage deadline fired -- run is not reproducible\n"
    assert ere.parse_route_log(fired)["hit_timeout"] is True


def test_a_cache_replay_is_flagged_and_the_harness_asks_for_a_fresh_route(tmp_path, monkeypatch):
    replay = "--- Checking routing cache ---\n  Cache HIT: 56 nets routed\n"
    assert ere.parse_route_log(replay)["cache_hit"] is True
    assert ere.parse_route_log("  Cache MISS (key: 1f46...)\n")["cache_hit"] is False

    seen: list[list[str]] = []

    class _Done:
        returncode = 0

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, **kwargs):
        seen.append(cmd)
        return _Done()

    monkeypatch.setattr(ere.subprocess, "Popen", fake_popen)
    ere.run_route(tmp_path / "in.kicad_pcb", tmp_path / "out.kicad_pcb", [], 60, tmp_path / "log")
    assert "--no-cache" in seen[0]


def test_summary_counts_unfinished_nets_with_and_without_the_excluded_ones(tmp_path, capsys):
    import json

    row = {
        "board": "05",
        "scale": 1.5,
        "mode": "rigid",
        "seed": 43,
        "cmd": "-m kicad_tools.cli route in -o out --no-placement-delta-feedback --seed 43",
        "nets_complete": 30,
        "kct_check_errors": 5,
        "kicad_drc_errors": 0,
        "via_count": 120,
        "vias_per_connection": 0.896,
        "coarse_grid_mm": 0.065,
        "cell_estimate": 10577933,
        "exit_code": 2,
        "hit_timeout": False,
        "artifact": "final",
        "cache_hit": False,
        "wall_s": 610.2,
        "loadavg_start": [5.5, 6.0, 7.0],
        "loadavg_end": [8.25, 7.0, 7.0],
        "stuck_nets": {
            "EN_B": "congestion_saturated",
            "PHASE_A": "budget_starved",
            "PHASE_C": "budget_starved",
            "GND": "pour_discontinuous",
            "VM": "pour_discontinuous",  # power-typed, but there is no VM zone
        },
    }
    phase = frozenset({"PHASE_A", "PHASE_B", "PHASE_C"})
    zones = frozenset({"GND", "+5V"})
    # The classifier's reading: both pour-classed nets dropped.
    assert ere.unfinished_signal_nets(row) == ["EN_B", "PHASE_A", "PHASE_C"]
    assert ere.unfinished_signal_nets(row, phase) == ["EN_B"]
    # Zone-checked: VM has no pour to be discontinuous, so it is unfinished.
    assert ere.unfinished_signal_nets(row, zone_nets=zones) == ["EN_B", "PHASE_A", "PHASE_C", "VM"]
    assert ere.unfinished_signal_nets(row, phase, zones) == ["EN_B", "VM"]
    assert ere.unfinished_signal_nets({"error": "no routed output"}) is None
    # A row's own record beats the command-line fallback.
    assert ere.row_zone_nets({"zone_nets": ["GND"]}, zones) == frozenset({"GND"})
    assert ere.row_zone_nets({}, zones) == zones and ere.row_zone_nets({}) is None

    path = tmp_path / "05_off_s43.json"
    replay = {**row, "scale": 1.0, "mode": "uniform", "cmd": "route --seed 43", "cache_hit": True}
    replay["zone_nets"] = ["GND", "VM"]
    failed = {"board": "05", "scale": 1.25, "mode": "rigid", "error": "no routed output"}
    path.write_text(json.dumps({"rows": [row, replay, failed]}))
    argv = ["summary", str(path), "--exclude-nets", "PHASE_A,PHASE_B,PHASE_C", "--nets"]
    assert ere.main(argv) == 0
    out = capsys.readouterr().out
    assert "...without PHASE_A/PHASE_B/PHASE_C" in out
    # No zone information for this row and none supplied: the zone-checked cells stay empty.
    assert (
        "| 05_off_s43 | 1.5 | rigid | off | 43 | 3 | 1 | -- | -- | 5 | 0 | 120 | 0.896 | 0.065 "
        "| 10,577,933 | 2 | no | no | 610.2 | 5.5 -> 8.25 | -- |" in out
    )
    # Feedback left at its default, a cache replay, and a row that recorded a VM zone.
    assert (
        "| 05_off_s43 | 1 | uniform | default | 43 | 3 | 1 | 3 | 1 | 5 | 0 | 120 | 0.896 | 0.065 "
        "| 10,577,933 | 2 | no | yes |" in out
    )
    assert "| 05_off_s43 | 1.25 | rigid | ERROR: no routed output |" in out
    assert "05_off_s43 rigid 1.5: EN_B (congestion_saturated), PHASE_A (budget_starved)" in out

    assert ere.main([*argv, "--zone-nets", "GND,+5V"]) == 0
    out = capsys.readouterr().out
    assert "| 05_off_s43 | 1.5 | rigid | off | 43 | 3 | 1 | 4 | 2 | 5 | 0 |" in out
    assert (
        "| 05_off_s43 | 1 | uniform | default | 43 | 3 | 1 | 3 | 1 | 5 | 0 |" in out
    )  # own record wins
    assert (
        "05_off_s43 rigid 1.5: EN_B (congestion_saturated), PHASE_A (budget_starved), PHASE_C (budget_starved), VM (pour_discontinuous)"
        in out
    )


def test_grid_plan_is_read_per_zone_not_just_at_the_coarse_pitch():
    """Two rows at one coarse pitch are not grid-matched if a part's fine zone differs."""
    log = (
        "Multi-Resolution Grid Plan (adaptive)\n  Coarse grid: 0.065mm\n  Fine zones: 3\n"
        "    U1: 0.0400mm (12.3x12.3mm, ~95,481 cells) offset=(0.0200,0.0000)\n"
        "    U2: 0.0050mm (9.7x12.4mm, ~4,845,540 cells)\n"
        "    C2: 0.0635mm (5.9x4.0mm, ~5,859 cells) offset=(0.0575,0.0000)\n"
        "  Total cell estimate: 10,577,933\n"
    )
    plan = ere.parse_grid_plan(log)
    assert plan["coarse_grid_mm"] == 0.065 and plan["cell_estimate"] == 10577933
    assert plan["fine_zones"] == {
        "U1": {"pitch_mm": 0.04, "cells": 95481},
        "U2": {"pitch_mm": 0.005, "cells": 4845540},
        "C2": {"pitch_mm": 0.0635, "cells": 5859},
    }
    assert ere.parse_grid_plan("no plan here") == {"fine_zones": {}}

    rows = [
        {"board": "05", "mode": "rigid", "scale": 1.5, **plan},
        {"board": "05", "mode": "rigid", "scale": 2.0, "error": "could not scale: copper"},
    ]
    table = ere.render_plans(rows, 0.03)
    assert "| 05 | rigid | 1.5 | 0.065 | 10,577,933 | 3 | U2 0.005, 4,845,540 |" in table
    assert "| 05 | rigid | 2 | ERROR: could not scale: copper |" in table


def test_plan_command_dry_runs_the_scaled_board_with_the_rows_grid_arguments(
    tmp_path, monkeypatch, capsys
):
    import json

    calls: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = "  Coarse grid: 0.065mm\n    U2: 0.0500mm (9.7x12.4mm, ~48,555 cells)\n"
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Done()

    monkeypatch.setattr(ere.subprocess, "run", fake_run)
    out = tmp_path / "plan.json"
    argv = ["plan", "--boards", "05", "--scales", "1.0,1.5", "--modes", "uniform,rigid"]
    argv += ["--work-dir", str(tmp_path / "w"), "--json", str(out), "--extra", "--layers 4"]
    assert ere.main(argv) == 0
    capsys.readouterr()

    rows = json.loads(out.read_text())["rows"]
    # 1x once (under the first mode), then 1.5x in each mode.
    assert [(r["mode"], r["scale"]) for r in rows] == [
        ("uniform", 1.0),
        ("uniform", 1.5),
        ("rigid", 1.5),
    ]
    assert all("--dry-run" in c and "--no-cache" in c and "--layers" in c for c in calls)
    assert "--max-cells" not in calls[0]
    assert calls[1][calls[1].index("--max-cells") + 1] == "1125000"
    assert rows[2]["fine_zones"] == {"U2": {"pitch_mm": 0.05, "cells": 48555}}
    assert rows[2]["scale_report"]["clustered_footprints"] == 14


def test_net_matrix_follows_each_unfinished_net_across_rows():
    rows = [
        (
            "a",
            {
                "mode": "uniform",
                "scale": 1.0,
                "stuck_nets": {"PHASE_A": "budget_starved"},
                "vias_by_net": {"GND_SENSE": 6},
            },
        ),
        (
            "a",
            {
                "mode": "rigid",
                "scale": 1.25,
                "stuck_nets": {"GND_SENSE": "congestion_saturated"},
                "vias_by_net": {"PHASE_A": 1, "GND_SENSE": 4},
            },
        ),
        ("a", {"mode": "rigid", "scale": 1.5, "error": "no routed output"}),  # ungraded: no column
    ]
    table = ere.render_net_matrix(rows).splitlines()
    assert table[0] == "| Net | a uniform 1 | a rigid 1.25 |"
    assert table[2] == "| GND_SENSE | ok (6 v) | congestion_saturated (4 v) |"
    assert table[3] == "| PHASE_A | budget_starved (0 v) | ok (1 v) |"


# ---------------------------------------------------------------------------
# Grading parity with a manual ``kct check`` (Issue #6420)
# ---------------------------------------------------------------------------

#: Two USB_D nets the diff-pair rules recognise by name, deliberately uncoupled
#: (different lengths, 5 mm apart) so both diff-pair rules fire once a
#: net-class map engages them.
UNCOUPLED_USB_PAIR = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "USB_D+")
  (net 2 "USB_D-")
  (gr_rect (start 100 100) (end 150 150)
    (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
  (segment (start 110 120) (end 140 120) (width 0.2) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000020"))
  (segment (start 110 125) (end 120 125) (width 0.2) (layer "F.Cu") (net 2)
    (uuid "00000000-0000-0000-0000-000000000021"))
)
"""


def _usb_net_class_map() -> dict:
    return {
        net: {
            "name": "HighSpeed",
            "coupled_routing": True,
            "diffpair_partner": partner,
            "intra_pair_clearance": 0.075,
            "skew_tolerance_mm": 3.0,
        }
        for net, partner in (("USB_D+", "USB_D-"), ("USB_D-", "USB_D+"))
    }


def _grade_offline(monkeypatch, run_dir: Path, *, sidecar: bool, fab_mfr: str | None = None):
    """``grade()`` on the uncoupled-pair board with kicad-cli stubbed out."""
    import json
    from types import SimpleNamespace

    from kicad_tools.benchmark.external import metrics

    seen: dict = {}

    def fake_drc(_routed, **kw):
        seen.update(kw)
        return SimpleNamespace(
            ran=True, violation_count=0, by_type={}, note=None, unconnected_count=2
        )

    monkeypatch.setattr(metrics, "run_kicad_cli_drc", fake_drc)
    run_dir.mkdir(parents=True)
    routed = run_dir / "usb_routed.kicad_pcb"
    routed.write_text(UNCOUPLED_USB_PAIR)
    if sidecar:
        (run_dir / "net_class_map.json").write_text(json.dumps(_usb_net_class_map()))
    if fab_mfr is not None:
        (run_dir / "fab_profile.json").write_text(
            json.dumps({"mfr": fab_mfr, "source": "kct route --manufacturer"})
        )
    row = ere.grade(routed, ere.Board("x", "usb.kicad_pcb", layers=4, manufacturer="jlcpcb"))
    return row, seen


def test_grade_counts_diff_pair_errors_with_the_run_dirs_net_class_map(tmp_path, monkeypatch):
    row, seen = _grade_offline(monkeypatch, tmp_path / "run", sidecar=True)

    assert row["kct_check_by_rule"]["diffpair_length_skew"] == 1
    assert row["kct_check_by_rule"]["diffpair_routing_continuity"] == 1
    assert row["kct_check_errors_total"] == row["kct_check_errors"] >= 2
    assert row["kct_check_net_class_map"] == str(tmp_path / "run" / "net_class_map.json")
    assert row["kct_check_inactive_rules"] == []
    # kicad-cli: every error on a track, and the unconnected count is a column.
    assert seen.get("all_track_errors") is True
    assert row["kicad_drc_all_track_errors"] is True
    assert row["kicad_drc_unconnected"] == 2


def test_grade_without_a_sidecar_says_so_instead_of_passing(tmp_path, monkeypatch):
    row, _ = _grade_offline(monkeypatch, tmp_path / "run", sidecar=False)

    assert row["kct_check_net_class_map"] is None
    assert not any(rule.startswith("diffpair_") for rule in row["kct_check_by_rule"])
    assert set(row["kct_check_inactive_rules"]) == {
        "match_group_length_skew",
        "diffpair_length_skew",
        "diffpair_routing_continuity",
    }


def test_grade_uses_the_routes_own_manufacturer_tier(tmp_path, monkeypatch):
    row, _ = _grade_offline(monkeypatch, tmp_path / "a", sidecar=False, fab_mfr="jlcpcb-tier1")
    assert row["kct_check_mfr"] == "jlcpcb-tier1"  # not split to "jlcpcb"
    # No fab_profile.json: the board's declared manufacturer, unsplit.
    row, _ = _grade_offline(monkeypatch, tmp_path / "b", sidecar=False)
    assert row["kct_check_mfr"] == "jlcpcb"


def test_grade_records_null_unconnected_when_kicad_cli_did_not_run(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from kicad_tools.benchmark.external import metrics

    row, _ = _grade_offline(monkeypatch, tmp_path / "run", sidecar=True)
    monkeypatch.setattr(
        metrics,
        "run_kicad_cli_drc",
        lambda _r, **_kw: SimpleNamespace(
            ran=False, violation_count=None, by_type={}, note="absent"
        ),
    )
    routed = tmp_path / "run" / "usb_routed.kicad_pcb"
    row = ere.grade(routed, ere.Board("x", "usb.kicad_pcb", layers=4))
    assert row["kicad_drc_errors"] is None and row["kicad_drc_unconnected"] is None
    assert ere.manufacturable({**row, "exit_code": 0, "artifact": "final"}) is None


CLEAN_ROW = {
    "exit_code": 0,
    "artifact": "final",
    "kct_check_errors_total": 0,
    "kicad_drc_errors": 0,
    "kicad_drc_unconnected": 0,
}


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({}, True),
        ({"exit_code": 3}, False),
        ({"kct_check_errors_total": 2}, False),
        ({"kicad_drc_errors": 1}, False),
        ({"kicad_drc_unconnected": 4}, False),
        ({"artifact": "deadline"}, False),
        ({"kct_check_errors_total": None}, None),
        ({"kicad_drc_errors": None}, None),
        ({"kicad_drc_unconnected": None}, None),
        # A failing route with an engine that did not run is still "not checked".
        ({"exit_code": 4, "kicad_drc_errors": None}, None),
    ],
)
def test_manufacturable_verdict(change, expected):
    assert ere.manufacturable({**CLEAN_ROW, **change}) is expected


def test_manufacturable_falls_back_to_the_legacy_kct_column():
    legacy = {k: v for k, v in CLEAN_ROW.items() if k != "kct_check_errors_total"}
    assert ere.manufacturable({**legacy, "kct_check_errors": 0}) is True
    assert ere.manufacturable({**legacy, "kct_check_errors": 1}) is False


GRID = ["--route-engine", "grid", "--layers", "4", "--deterministic-budget"]


@pytest.mark.parametrize(
    ("extra", "rescued"),
    [
        (GRID, True),
        (["--per-net-iterations", "1000000"], True),  # default engine is grid
        (["--per-net-iterations=1000000"], True),
        (["--layers", "4"], False),  # not a deterministic run: nothing to make deterministic
        (["--route-engine", "lattice", "--deterministic-budget"], False),
        (["--route-engine=lattice", "--per-net-iterations", "5"], False),
    ],
)
def test_deterministic_grid_routes_get_deterministic_rescue(extra, rescued):
    out = ere.with_deterministic_rescue(extra)
    assert ("--deterministic-rescue" in out) is rescued
    assert out[: len(extra)] == extra  # only ever appended
    # Already present: never doubled.
    assert (
        ere.with_deterministic_rescue([*extra, "--deterministic-rescue"]).count(
            "--deterministic-rescue"
        )
        == 1
    )


def test_run_one_passes_and_records_deterministic_rescue(tmp_path, monkeypatch):
    _stub_route(monkeypatch)
    src = tmp_path / "b.kicad_pcb"
    src.write_text(_board(TWO_PARTS))
    board = ere.Board("x", "b.kicad_pcb")

    row = ere.run_one(
        board, src, 1.0, "uniform", tmp_path / "g", 42, 60, "default", extra_args=tuple(GRID)
    )
    assert row["deterministic_rescue"] is True
    assert row["cmd"].endswith("--deterministic-rescue")

    lattice = ("--route-engine", "lattice", "--deterministic-budget")
    row = ere.run_one(
        board, src, 1.0, "uniform", tmp_path / "l", 42, 60, "default", extra_args=lattice
    )
    assert row["deterministic_rescue"] is False
    assert "--deterministic-rescue" not in row["cmd"]


def test_route_log_records_which_wall_clocks_were_live():
    loose = (
        "[deterministic-budget] NOTE: relief-rescue probe / victim re-land sub-searches keep "
        "their 10s wall clock (Issue #4730), so ...\n"
        "  Post-negotiation sweep bound: 60.0s whole-sweep / 10.0s per-net wall clock -- x\n"
    )
    facts = ere.parse_route_log(loose)
    assert facts["rescue_wall_clock"] is True and facts["sweep_wall_clock"] is True

    tight = (
        "  Relief-rescue sub-search cap: iteration-bounded (per-net node-expansion cap; ...)\n"
        "  Post-negotiation sweep bound: iteration-bounded (per-net node-expansion cap; ...)\n"
    )
    facts = ere.parse_route_log(tight)
    assert facts["rescue_wall_clock"] is False and facts["sweep_wall_clock"] is False
    # Silent log (e.g. lattice): no claim either way.
    assert "rescue_wall_clock" not in ere.parse_route_log("Nets routed: 3/3\n")


def test_tables_render_unconnected_and_the_verdict():
    row = {
        "board": "05",
        "mode": "uniform",
        "scale": 1.0,
        "nets_complete": 33,
        "nets_total": 37,
        "connections_routed": 1,
        "connections_total": 2,
        "kct_check_errors": 3,
        "kct_check_errors_total": 3,
        "kicad_drc_errors": 0,
        "kicad_drc_unconnected": 4,
        "via_count": 120,
        "wirelength_mm": 1068.1,
        "wall_s": 2040.6,
        "exit_code": 4,
        "artifact": "final",
        "manufacturable": False,
    }
    assert ere.render_route([row]).splitlines()[-1].endswith("| 4 | no |")
    legacy = {k: v for k, v in row.items() if k not in ("manufacturable", "kicad_drc_unconnected")}
    assert ere.render_route([legacy]).splitlines()[-1].endswith("| None | unknown |")
