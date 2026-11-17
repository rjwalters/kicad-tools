"""Tests for the pre-route bundle crossing report (Issue #6339).

Synthetic boards are written as minimal ``.kicad_pcb`` text so the analysis is
exercised end to end (PCB load -> report) without any routing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.router.bundle_crossings import (
    compute_bundle_crossings,
    format_report_text,
)
from kicad_tools.schema.pcb import PCB

Pad = tuple[str, float, float]  # (net name, dx, dy)


def _board(tmp_path: Path, parts: dict[str, tuple[float, float, list[Pad]]]) -> PCB:
    names: list[str] = []
    for _x, _y, pads in parts.values():
        for net, _dx, _dy in pads:
            if net not in names:
                names.append(net)
    lines = [
        "(kicad_pcb (version 20240108) (generator test)",
        "  (general (thickness 1.6))",
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))',
        '  (net 0 "")',
    ]
    for i, n in enumerate(names, 1):
        lines.append(f'  (net {i} "{n}")')
    for ref, (x, y, pads) in parts.items():
        lines.append(f'  (footprint "T:{ref}" (layer "F.Cu") (at {x} {y} 0)')
        lines.append(f'    (fp_text reference "{ref}" (at 0 0) (layer "F.SilkS"))')
        for k, (net, dx, dy) in enumerate(pads, 1):
            lines.append(
                f'    (pad "{k}" smd rect (at {dx} {dy}) (size 0.5 0.5) '
                f'(layers "F.Cu") (net {names.index(net) + 1} "{net}"))'
            )
        lines.append("  )")
    lines.append(")")
    path = tmp_path / "t.kicad_pcb"
    path.write_text("\n".join(lines))
    return PCB.load(str(path))


def _column(order: list[str], x: float, y0: float = 0.0, pitch: float = 1.0) -> list[Pad]:
    return [(n, x, y0 + i * pitch) for i, n in enumerate(order)]


def _row(order: list[str], y: float, x0: float = 0.0, pitch: float = 1.0) -> list[Pad]:
    return [(n, x0 + i * pitch, y) for i, n in enumerate(order)]


NETS4 = ["N0", "N1", "N2", "N3"]


def test_planar_facing_columns_report_zero(tmp_path):
    pcb = _board(
        tmp_path,
        {"A": (0, 0, _column(NETS4, 0)), "B": (30, 0, _column(NETS4, 0))},
    )
    rep = compute_bundle_crossings(pcb, 2)
    assert len(rep.bundles) == 1
    b = rep.bundles[0]
    assert b.min_inversions == 0 and not b.oversized
    assert b.entries == {"aligned": 0}
    assert rep.flagged_bundles == []


def test_single_transposition(tmp_path):
    swapped = ["N0", "N2", "N1", "N3"]
    pcb = _board(
        tmp_path,
        {"A": (0, 0, _column(NETS4, 0)), "B": (30, 0, _column(swapped, 0))},
    )
    b = compute_bundle_crossings(pcb, 2).bundles[0]
    assert b.min_inversions == 1
    assert b.inverted_pairs == [("N1", "N2")]
    assert b.min_pin_swaps == 1
    assert not b.oversized


def test_full_reversal_oversized_depends_on_layers(tmp_path):
    parts = {
        "A": (0, 0, _column(NETS4, 0)),
        "B": (30, 0, _column(list(reversed(NETS4)), 0)),
    }
    pcb = _board(tmp_path, parts)
    two = compute_bundle_crossings(pcb, 2).bundles[0]
    assert two.min_inversions == 6  # C(4, 2)
    assert two.chromatic_lower == two.chromatic_upper == 4
    assert two.oversized
    assert not compute_bundle_crossings(pcb, 4).bundles[0].oversized


def test_three_net_reversal_two_vs_three_layers(tmp_path):
    n3 = NETS4[:3]
    pcb = _board(
        tmp_path,
        {"A": (0, 0, _column(n3, 0)), "B": (30, 0, _column(n3[::-1], 0))},
    )
    assert compute_bundle_crossings(pcb, 2).bundles[0].oversized
    assert not compute_bundle_crossings(pcb, 3).bundles[0].oversized


def test_two_nets_is_not_a_bundle(tmp_path):
    pcb = _board(
        tmp_path,
        {"A": (0, 0, _column(NETS4[:2], 0)), "B": (30, 0, _column(NETS4[:2][::-1], 0))},
    )
    assert compute_bundle_crossings(pcb, 2).bundles == []


def test_l_shaped_same_order_is_planar_at_correct_side(tmp_path):
    # A: vertical column on the left; B: horizontal row below-right.
    pcb = _board(
        tmp_path,
        {"A": (0, 0, _column(NETS4, 0)), "B": (10, 20, _row(NETS4, 0))},
    )
    b = compute_bundle_crossings(pcb, 2).bundles[0]
    assert b.axis_a != b.axis_b
    assert b.entries == {"aligned": 0, "mirrored": 6}
    assert b.min_entry == "aligned" and b.min_inversions == 0


def test_l_shaped_transposition_reports_both_sides(tmp_path):
    swapped = ["N0", "N2", "N1", "N3"]
    pcb = _board(
        tmp_path,
        {"A": (0, 0, _column(NETS4, 0)), "B": (10, 20, _row(swapped, 0))},
    )
    b = compute_bundle_crossings(pcb, 2).bundles[0]
    assert b.entries == {"aligned": 1, "mirrored": 5}
    assert b.inverted_pairs == [("N1", "N2")]


def test_power_and_ground_nets_are_not_bundle_members(tmp_path):
    a = _column(["S0", "S1", "GND", "+5V"], 0)
    b = _column(["S0", "S1", "GND", "+5V"], 0)
    pcb = _board(tmp_path, {"A": (0, 0, a), "B": (30, 0, b)})
    assert compute_bundle_crossings(pcb, 2).bundles == []  # only 2 signal nets


def test_multi_pad_net_uses_centroid(tmp_path):
    # N1 has two pads on A, straddling N2's position; centroid keeps order.
    a = [("N0", 0, 0), ("N1", 0, 1), ("N1", 0, 3), ("N2", 0, 2.0), ("N3", 0, 4)]
    pcb = _board(tmp_path, {"A": (0, 0, a), "B": (30, 0, _column(NETS4, 0))})
    b = compute_bundle_crossings(pcb, 2).bundles[0]
    # N1 centroid y=2, tied with N2 (y=2): name tiebreak keeps N1 before N2.
    assert b.order_a == NETS4
    assert b.min_inversions == 0


def test_multi_part_net_is_skipped_not_bundled(tmp_path):
    parts = {
        "A": (0, 0, _column(NETS4, 0)),
        "B": (30, 0, _column(NETS4, 0)),
        "C": (60, 0, [("N0", 0, 0)]),
    }
    rep = compute_bundle_crossings(_board(tmp_path, parts), 2)
    assert rep.multi_part_nets == 1
    assert len(rep.bundles[0].order_a) == 3


def test_hub_interleaved_exit_directions(tmp_path):
    # Hub column: P0 leaves left, G leaves down, P1 leaves left, Q down.
    hub = _column(["P0", "G", "P1", "Q"], 0)
    parts = {
        "H": (50, 50, hub),
        "L0": (0, 50, [("P0", 0, 0)]),
        "L1": (0, 52, [("P1", 0, 0)]),
        "D0": (50, 100, [("G", 0, 0)]),
        "D1": (51, 100, [("Q", 0, 0)]),
    }
    rep = compute_bundle_crossings(_board(tmp_path, parts), 2)
    hubs = [h for h in rep.hubs if h.ref == "H"]
    assert len(hubs) == 1
    item = next(i for i in hubs[0].interleaved if i["exit"] == "left")
    assert item["nets"] == ["P0", "P1"] and item["separated_by"] == ["G"]


def test_hub_contiguous_exits_not_flagged(tmp_path):
    hub = _column(["P0", "P1", "G", "Q"], 0)
    parts = {
        "H": (50, 50, hub),
        "L0": (0, 50, [("P0", 0, 0)]),
        "L1": (0, 52, [("P1", 0, 0)]),
        "D0": (50, 100, [("G", 0, 0)]),
        "D1": (51, 100, [("Q", 0, 0)]),
    }
    rep = compute_bundle_crossings(_board(tmp_path, parts), 2)
    assert [h for h in rep.hubs if h.ref == "H" and h.interleaved] == []


def test_json_and_text_shape(tmp_path):
    swapped = ["N0", "N2", "N1", "N3"]
    pcb = _board(
        tmp_path,
        {"A": (0, 0, _column(NETS4, 0)), "B": (30, 0, _column(swapped, 0))},
    )
    rep = compute_bundle_crossings(pcb, 2)
    d = rep.to_dict()
    assert set(d) == {"signal_layers", "bundles", "hubs", "skipped"}
    assert set(d["bundles"][0]) >= {
        "ref_a", "ref_b", "order_a", "order_b", "entries", "min_entry",
        "min_inversions", "inverted_pairs", "min_pin_swaps", "oversized",
    }  # fmt: skip
    text = format_report_text(rep)
    assert "N1/N2" in text and "forced crossings" in text
    assert rep.filtered("N0").bundles and not rep.filtered("nope").bundles


@pytest.mark.parametrize("layers", [None, 0])
def test_default_signal_layers_from_board(tmp_path, layers):
    pcb = _board(tmp_path, {"A": (0, 0, _column(NETS4, 0)), "B": (30, 0, _column(NETS4, 0))})
    assert compute_bundle_crossings(pcb, layers).signal_layers == 2
