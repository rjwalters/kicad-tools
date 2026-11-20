"""Interval differencing in the route-time pairwise tables (issue #9602).

``load_signed_voltage_map`` collapses each net's ``[lo, hi]`` range to its
largest-magnitude endpoint *with its sign* (#4411 interim contract).  On a
range-bearing map -- notably a ``kct voltage-map-merge`` envelope (#9582) -- a
sign-flipping net and its opposite bank can collapse to the **same** endpoint:
the route-time ``|ΔV|`` then scores 0 V and the pair drops out of the widening
matrix entirely, while the census (``kct creepage``) differences the intervals
end-to-end and sees 300 V.  ``_pair_bounds`` cannot rescue it (bounds only ever
lower), so the loaders/consumers must carry the intervals themselves.

These tests pin the #9602 contract at every site that rebuilds ΔV:

1. the sign-flipping repro: ``LINE``/``F1`` both ``-150..150`` appear in the
   route table at the census 300 V requirement (3.2 mm, IEC 60664-1 basic);
2. asymmetric ranges and range-vs-scalar pairs use the worst endpoint
   combination ``max(|a.hi - b.lo|, |b.hi - a.lo|)``;
3. ``_pair_bounds`` still only ever lowers an interval pair;
4. scalar-only maps give byte-identical ``required_by_pair`` (the regression
   guard), through the degenerate-interval collapse; and
5. ``kct route``'s preload and ``route-link``'s unmapped-net fallback thread
   the intervals through.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from kicad_tools.creepage.engine import pair_bounds_from_dict
from kicad_tools.creepage.standards import get_standard
from kicad_tools.placement.hv_domains import build_required_by_domain_pair
from kicad_tools.router.pairwise_clearance import (
    build_pairwise_clearance_table,
    load_signed_voltage_map,
    load_voltage_intervals,
    subthreshold_coverage_gap,
)

DRU = 0.2

# The issue's motivating topology: LINE swings +/-150 V against an
# opposite-polarity follower bank; both banks span the same interval.
SIGNFLIP_MAP = {"LINE": {"min": -150, "max": 150}, "F1": {"min": -150, "max": 150}}
SIGNFLIP_INTERVALS = {"LINE": (-150.0, 150.0), "F1": (-150.0, 150.0)}


def _census_dv(dv: float) -> float:
    """The census/basic creepage requirement at *dv* V (PD2 / IIIa, the defaults)."""
    return get_standard("iec60664").required_creepage(dv, 2, "IIIa", insulation="basic")[0]


def _scalar_collapsed(map_payload: dict) -> tuple[dict[str, float], dict[str, tuple[float, float]]]:
    """(signed collapse, intervals) as both loaders see the same sidecar."""
    from kicad_tools.creepage.engine import voltage_map_from_dict

    intervals = voltage_map_from_dict(map_payload)[0]
    scalars = {
        name: (iv.hi if abs(iv.hi) >= abs(iv.lo) else iv.lo) for name, iv in intervals.items()
    }
    return scalars, {name: (iv.lo, iv.hi) for name, iv in intervals.items()}


def test_load_voltage_intervals_parses_scalars_and_ranges(tmp_path: Path) -> None:
    p = tmp_path / "vm.json"
    p.write_text(
        json.dumps(
            {
                "_comment": "metadata is skipped",
                "_edge_voltage": 0,
                "LINE": {"max": 150, "min": -150},
                "F1": 150,
                "GND": 0,
            }
        )
    )
    assert load_voltage_intervals(p) == {
        "LINE": (-150.0, 150.0),
        "F1": (150.0, 150.0),
        "GND": (0.0, 0.0),
    }
    # The scalar loader reads the same file and keeps its contract.
    assert load_signed_voltage_map(p) == {"LINE": 150.0, "F1": 150.0, "GND": 0.0}


def test_load_voltage_intervals_rejects_malformed(tmp_path: Path) -> None:
    not_obj = tmp_path / "list.json"
    not_obj.write_text("[1, 2]")
    with pytest.raises(ValueError):
        load_voltage_intervals(not_obj)
    bad_range = tmp_path / "bad.json"
    bad_range.write_text(json.dumps({"A": {"min": "x", "max": 1}}))
    with pytest.raises(ValueError):
        load_voltage_intervals(bad_range)


def test_sign_flipping_range_pair_matches_the_census() -> None:
    """The headline repro: collapsed scalars drop the pair; intervals restore it."""
    scalars, intervals = _scalar_collapsed(SIGNFLIP_MAP)
    assert scalars == {"LINE": 150.0, "F1": 150.0}  # collapse -> 0 V span

    collapsed = build_pairwise_clearance_table(scalars, dru=DRU)
    assert ("F1", "LINE") not in collapsed.required_by_pair  # the #4867 failure mode

    table = build_pairwise_clearance_table(scalars, dru=DRU, net_intervals=intervals)
    assert table.required_by_pair == {("F1", "LINE"): pytest.approx(_census_dv(300.0))}
    # 300 V under IEC 60664-1 basic / PD2 / IIIa is 3.2 mm (the census value).
    assert table.required_clearance("LINE", "F1") == pytest.approx(3.2)
    assert table.required_clearance("LINE", "F1") == pytest.approx(_census_dv(300.0))


def test_asymmetric_ranges_use_worst_endpoint_combination() -> None:
    scalars, intervals = _scalar_collapsed(
        {"A": {"min": 0, "max": 100}, "B": {"min": -50, "max": 50}}
    )
    table = build_pairwise_clearance_table(scalars, dru=DRU, net_intervals=intervals)
    # max(|100 - (-50)|, |50 - 0|) = 150 V
    assert table.required_by_pair == {("A", "B"): pytest.approx(_census_dv(150.0))}


def test_range_vs_scalar_pair() -> None:
    scalars, intervals = _scalar_collapsed({"A": {"min": 120, "max": 150}, "B": 30})
    table = build_pairwise_clearance_table(scalars, dru=DRU, net_intervals=intervals)
    # max(|150 - 30|, |30 - 120|) = 120 V -- not the collapsed 150 V
    assert table.required_by_pair == {("A", "B"): pytest.approx(_census_dv(120.0))}


def test_scalar_only_map_is_byte_identical(tmp_path: Path) -> None:
    """Degenerate intervals must not move today's scalar ``required_by_pair``."""
    scalars, intervals = _scalar_collapsed({"FUSED_LINE": 150, "SRC_NEG": -150})
    without = build_pairwise_clearance_table(scalars, dru=DRU)
    with_degenerate = build_pairwise_clearance_table(scalars, dru=DRU, net_intervals=intervals)
    assert with_degenerate.required_by_pair == without.required_by_pair
    assert without.required_by_pair == build_required_by_domain_pair(scalars)
    # ...and the raw (un-normalised-key) loader form agrees too.
    p = tmp_path / "vm.json"
    p.write_text(json.dumps({"/FUSED_LINE": 150, "/SRC_NEG": -150}))
    raw = load_voltage_intervals(p)
    assert (
        build_pairwise_clearance_table(
            load_signed_voltage_map(p), dru=DRU, net_intervals=raw
        ).required_by_pair
        == build_pairwise_clearance_table(load_signed_voltage_map(p), dru=DRU).required_by_pair
    )


def test_pair_bounds_still_only_lower_a_range_pair() -> None:
    scalars, intervals = _scalar_collapsed(SIGNFLIP_MAP)
    uncapped = build_pairwise_clearance_table(scalars, dru=DRU, net_intervals=intervals)
    big = pair_bounds_from_dict(
        {"_pair_bounds": [{"nets": ["LINE", "F1"], "max_dv": 1000, "why": "no-op"}]}
    )
    assert (
        build_pairwise_clearance_table(
            scalars, dru=DRU, net_intervals=intervals, pair_bounds=big
        ).required_by_pair
        == uncapped.required_by_pair
    )
    capped = pair_bounds_from_dict(
        {"_pair_bounds": [{"nets": ["LINE", "F1"], "max_dv": 100, "why": "Q2 V_DS"}]}
    )
    lowered = build_pairwise_clearance_table(
        scalars, dru=DRU, net_intervals=intervals, pair_bounds=capped
    )
    assert lowered.required_by_pair == {("F1", "LINE"): pytest.approx(_census_dv(100.0))}
    assert lowered.required_by_pair[("F1", "LINE")] < uncapped.required_by_pair[("F1", "LINE")]


def test_subthreshold_coverage_gap_uses_intervals() -> None:
    # Two nets both swinging +/-14 V collapse to the same scalar (0 V span) and
    # vanish from the gap; the interval view is a 28 V span under the 30 V
    # threshold that the census scores above a typical DRU floor.
    scalars, intervals = _scalar_collapsed(
        {"A": {"min": -14, "max": 14}, "B": {"min": -14, "max": 14}}
    )
    scalar_gap = subthreshold_coverage_gap(scalars, dru=DRU)
    assert scalar_gap.pair_count == 0
    gap = subthreshold_coverage_gap(scalars, dru=DRU, net_intervals=intervals)
    assert gap.pair_count == 1
    assert gap.worst_pair == ("A", "B")
    assert gap.worst_delta_v == pytest.approx(28.0)
    assert gap.max_required_mm > DRU
    # A 300 V interval span is *above* the threshold: not a sub-threshold gap.
    s3, i3 = _scalar_collapsed(SIGNFLIP_MAP)
    assert subthreshold_coverage_gap(s3, dru=DRU, net_intervals=i3).pair_count == 0


def test_route_preload_builds_the_interval_table(tmp_path: Path) -> None:
    """``kct route``'s preload site: the stash carries the 300 V pair (AC repro)."""
    from kicad_tools.cli.route_cmd import _preload_voltage_map

    vm = tmp_path / "vm.json"
    vm.write_text(json.dumps(SIGNFLIP_MAP))
    args = argparse.Namespace(voltage_map=str(vm))
    assert _preload_voltage_map(args) == 0
    assert args._pairwise_intervals == {"LINE": (-150.0, 150.0), "F1": (-150.0, 150.0)}
    assert args._pairwise_required == {("F1", "LINE"): pytest.approx(_census_dv(300.0))}
    # Scalar-only map: the stash matches the legacy differencing exactly.
    vm2 = tmp_path / "scalar.json"
    vm2.write_text(json.dumps({"/LINE": 150, "/F1": -150}))
    args2 = argparse.Namespace(voltage_map=str(vm2))
    assert _preload_voltage_map(args2) == 0
    assert args2._pairwise_intervals == {"LINE": (150.0, 150.0), "F1": (-150.0, -150.0)}
    assert args2._pairwise_required == args._pairwise_required


def test_route_link_fallback_uses_intervals(tmp_path: Path) -> None:
    """An unmapped link net (0 V) against a range net still floors fail-closed (#8546).

    The fallback fires only when one side is absent from the map, and an absent
    net is the degenerate interval ``(0, 0)`` -- against which the interval
    span ``max(|hi|, |lo|)`` equals the scalar collapse exactly.  The floor is
    therefore census-consistent either way; this pins that the interval
    threading does not break the fail-closed rebuild on a range-bearing map.
    """

    from kicad_tools.cli.route_link_cmd import (
        _rules_for_link,
        _rules_from_args,
        add_route_link_arguments,
    )
    from kicad_tools.schema.pcb import PCB

    head = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG")
  (net 2 "LINE")
  (gr_rect (start 100 100) (end 130 120) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
"""
    pad = (
        '  (footprint "R" (layer "F.Cu") (uuid "00000000-0000-0000-0000-00000000000{0}") '
        "(at {1} {2})\n"
        '    (fp_text reference "R{0}" (at 0 -1.5) (layer "F.SilkS"))\n'
        '    (pad "1" smd rect (at 0 0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask") '
        '(net 1 "SIG")))\n'
    )
    body = pad.format(1, 105, 110) + pad.format(2, 125, 110)
    body += (
        '  (segment (start 108 112) (end 122 112) (width 0.2) (layer "F.Cu") (net 2) '
        '(uuid "00000000-0000-0000-0000-000000000101"))\n'
    )
    p = tmp_path / "b.kicad_pcb"
    p.write_text(head + body + ")\n")

    vm = tmp_path / "vm.json"
    vm.write_text(json.dumps({"LINE": {"min": -150, "max": 150}}))
    pcb = PCB.load(str(p))

    parser = argparse.ArgumentParser()
    add_route_link_arguments(parser)
    args = parser.parse_args([str(p), "--voltage-map", str(vm), "--link", "SIG R1.1 R2.1"])
    from kicad_tools.cli.route_cmd import _preload_voltage_map

    assert _preload_voltage_map(args) == 0
    rules = _rules_for_link(args, pcb, _rules_from_args(args, pcb), args.links[0])
    # max(|150|, |-150|) = 150 V against the unmapped 0 V net
    assert rules.clearance_for(2) == pytest.approx(_census_dv(150.0))
