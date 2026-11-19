"""Multi-scenario voltage-map merging (``merge_voltage_scenarios`` + CLI, #9582).

A board whose nets take different potentials per operating scenario needs one
``--voltage-map`` per scenario; every consumer takes only one.  The merge
builds the envelope map: per-net ``{min, max}`` spans, plus generated
``_pair_bounds`` entries that cap each pair back to its worst per-scenario
|ΔV| -- restoring the correlations the envelope forgets (two nets that ride
together are NOT ``range_a - range_b`` volts apart).
"""

from __future__ import annotations

import json
from itertools import combinations
from types import SimpleNamespace

import pytest

from kicad_tools.cli.commands.voltage_map_merge import run_voltage_map_merge_command
from kicad_tools.creepage.engine import (
    merge_voltage_scenarios,
    pair_bounds_from_dict,
    voltage_map_from_dict,
)
from kicad_tools.placement.hv_domains import build_required_by_domain_pair
from kicad_tools.router.pairwise_clearance import build_pairwise_clearance_table

DRU = 0.2
THRESH = 30.0

# The issue's motivating topology: the line node swings both ways against an
# opposite-polarity bank of followers.  In every scenario F1/F2 ride exactly
# with LINE; only LINE's sign flips between scenarios.
SCEN_A = {"/LINE": 150, "/F1": -150, "/F2": -150, "/AUX": 0}
SCEN_B = {"/LINE": -150, "/F1": 150, "/F2": 150, "/AUX": 0}


def _pair_interval_dv(m: dict, a: str, b: str, absent_zero: bool = True) -> float:
    """Interval worst-case |ΔV| for a pair from a parsed map.

    Net names are matched normalised (one leading ``/`` stripped), matching
    the merge output's keys.  ``absent_zero`` mirrors the consumers'
    "unmapped nets default to 0 V" rule.
    """
    from kicad_tools.creepage.engine import _norm_net_key

    intervals, _ = voltage_map_from_dict(m)
    norm = {_norm_net_key(k): iv for k, iv in intervals.items()}
    zero = (0.0, 0.0)
    (alo, ahi) = (norm[a].lo, norm[a].hi) if a in norm else zero
    (blo, bhi) = (norm[b].lo, norm[b].hi) if b in norm else zero
    return max(abs(ahi - blo), abs(bhi - alo))


def _merged_required_by_pair(merged: dict) -> dict[tuple[str, str], float]:
    """Per-pair requirement table built from the merged map.

    The merged map's envelope intervals give each pair its envelope |ΔV|; the
    generated ``_pair_bounds`` cap it back to the worst per-scenario |ΔV|; the
    shared :func:`build_required_by_domain_pair` builder (the SAME builder the
    router/placement tables and the census consume) maps |ΔV| -> mm.
    """
    intervals, _ = voltage_map_from_dict(merged)
    bounds = pair_bounds_from_dict(merged)
    out: dict[tuple[str, str], float] = {}
    for a, b in combinations(sorted(intervals), 2):
        dv = _pair_interval_dv(merged, a, b, absent_zero=False)
        bound = bounds.get(frozenset((a, b)))
        if bound is not None:
            dv = min(dv, bound[0])
        if dv >= THRESH:
            req = build_required_by_domain_pair(
                {"a": dv, "b": 0.0},
                hv_threshold=THRESH,
            )
            out[(a, b)] = req[("a", "b")]
    return out


def test_sign_flip_with_follower_group_worst_difference_across_maps():
    merged = merge_voltage_scenarios([SCEN_A, SCEN_B])

    # Envelope spans: LINE/F1/F2 each span both signs; AUX stays a scalar.
    assert merged["LINE"] == {"min": -150.0, "max": 150.0}
    assert merged["F1"] == {"min": -150.0, "max": 150.0}
    assert merged["AUX"] == 0.0
    assert merged["_edge_voltage"] == 0.0

    # The generated bound: exactly the follower group, capped to 0 V
    # ("always the same potential" -- #9582).  LINE-F1/LINE-F2 need none:
    # their envelope |ΔV| already equals the per-scenario worst (300 V).
    bounds = pair_bounds_from_dict(merged)
    assert set(bounds) == {frozenset({"F1", "F2"})}
    assert bounds[frozenset({"F1", "F2"})][0] == 0.0
    assert "scenario" in bounds[frozenset({"F1", "F2"})][1]

    # Per pair, the table built from the merged map equals the max of the
    # per-scenario tables (the issue's "worst difference across the maps").
    t_a = build_pairwise_clearance_table(SCEN_A, dru=DRU, hv_threshold=THRESH).required_by_pair
    t_b = build_pairwise_clearance_table(SCEN_B, dru=DRU, hv_threshold=THRESH).required_by_pair
    t_merged = _merged_required_by_pair(merged)

    all_pairs = set(t_a) | set(t_b)
    assert set(t_merged) == all_pairs
    for pair in all_pairs:
        assert t_merged[pair] == pytest.approx(max(t_a[pair], t_b[pair]))

    # The follower pair is NOT widened to the full range difference: without
    # the bound the envelope alone would read F1-F2 at 300 V; per scenario
    # they are 0 V apart, so the pair drops out of the widening matrix.
    assert ("F1", "F2") not in t_merged
    assert _pair_interval_dv(merged, "F1", "F2", absent_zero=False) == 300.0


def test_envelope_never_understates_any_scenario():
    scenarios = [
        {"/A": 100, "/B": -50, "/C": 0},
        {"/A": -20, "/B": 70, "/C": 5},
        {"/A": {"min": -60, "max": 40}, "/B": 0, "/C": -10},
        {"/A": 0, "/D": 25},  # /D present in one scenario only
    ]
    merged = merge_voltage_scenarios(scenarios)
    for a, b in combinations(sorted({"/A", "/B", "/C", "/D"}), 2):
        env_dv = _pair_interval_dv(merged, a, b)
        for s in scenarios:
            assert env_dv >= _pair_interval_dv(s, a, b)


def test_identical_scenarios_yield_scalars_and_no_bounds():
    m = {"/A": 42, "/B": -7}
    merged = merge_voltage_scenarios([dict(m), dict(m), dict(m)])
    assert merged == {"A": 42.0, "B": -7.0, "_edge_voltage": 0.0}
    assert "_pair_bounds" not in merged


def test_net_present_in_one_scenario_spans_zero():
    merged = merge_voltage_scenarios([{"/NEW": 100}, {"/OLD": -40}])
    # Absent == 0 V in the other scenario (the consumers' unmapped default).
    assert merged["NEW"] == {"min": 0.0, "max": 100.0}
    assert merged["OLD"] == {"min": -40.0, "max": 0.0}
    # NEW-OLD: envelope 140 V vs worst per-scenario 100 V -> generated bound.
    bounds = pair_bounds_from_dict(merged)
    assert bounds[frozenset({"NEW", "OLD"})][0] == 100.0


def test_differing_edge_voltage_raises():
    with pytest.raises(ValueError, match="_edge_voltage"):
        merge_voltage_scenarios([{"/A": 1, "_edge_voltage": 0}, {"/A": 2, "_edge_voltage": 5}])


def test_agreed_edge_voltage_is_carried():
    merged = merge_voltage_scenarios(
        [{"/A": 1, "_edge_voltage": 3.5}, {"/A": 2, "_edge_voltage": 3.5}]
    )
    assert merged["_edge_voltage"] == 3.5


def test_input_pair_bounds_are_rejected():
    bad = {"/A": 1, "_pair_bounds": [{"nets": ["/A", "/B"], "max_dv": 5, "why": "x"}]}
    with pytest.raises(ValueError, match="_pair_bounds"):
        merge_voltage_scenarios([bad, {"/A": 2, "/B": 3}])


def test_too_few_maps_and_name_mismatch_raise():
    with pytest.raises(ValueError, match="at least two"):
        merge_voltage_scenarios([{"/A": 1}])
    with pytest.raises(ValueError, match="one per scenario"):
        merge_voltage_scenarios([{"/A": 1}, {"/A": 2}], names=["only"])


def test_non_dict_scenario_raises_type_error():
    with pytest.raises(TypeError, match="must be a dict"):
        merge_voltage_scenarios([{"/A": 1}, "nope"])


def test_why_names_the_scenarios():
    merged = merge_voltage_scenarios(
        [{"/A": 10, "/B": 10}, {"/A": 0, "/B": 0}],
        names=["fwd.json", "rev.json"],
    )
    why = pair_bounds_from_dict(merged)[frozenset({"A", "B"})][1]
    assert "fwd.json" in why and "rev.json" in why


def test_normalised_net_names_are_merged_and_emitted():
    merged = merge_voltage_scenarios([{"/LINE": 5}, {"LINE": 10}])
    assert {k for k in merged if not k.startswith("_")} == {"LINE"}
    assert merged["LINE"] == {"min": 5.0, "max": 10.0}


def test_cli_writes_envelope(tmp_path, capsys):
    a = tmp_path / "fwd.json"
    b = tmp_path / "rev.json"
    out = tmp_path / "env.json"
    a.write_text(json.dumps(SCEN_A))
    b.write_text(json.dumps(SCEN_B))

    rc = run_voltage_map_merge_command(SimpleNamespace(inputs=[str(a), str(b)], output=str(out)))
    assert rc == 0
    merged = json.loads(out.read_text())
    assert merged["F1"] == {"min": -150.0, "max": 150.0}
    assert merged["_pair_bounds"][0]["nets"] == ["F1", "F2"]
    assert "1 generated pair bound" in capsys.readouterr().out

    # The merged file is an ordinary map: the existing loader round-trips it.
    loaded = json.loads(out.read_text())
    intervals, edge = voltage_map_from_dict(loaded)
    assert edge == 0.0
    assert intervals["LINE"].lo == -150.0 and intervals["LINE"].hi == 150.0


def test_cli_errors(tmp_path, capsys):
    a = tmp_path / "a.json"
    a.write_text(json.dumps(SCEN_A))
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")

    # single input
    assert (
        run_voltage_map_merge_command(
            SimpleNamespace(inputs=[str(a)], output=str(tmp_path / "o.json"))
        )
        == 1
    )
    assert "at least two" in capsys.readouterr().err

    # unreadable JSON
    assert (
        run_voltage_map_merge_command(
            SimpleNamespace(inputs=[str(a), str(bad)], output=str(tmp_path / "o.json"))
        )
        == 1
    )
    assert "cannot read" in capsys.readouterr().err

    # missing file
    assert (
        run_voltage_map_merge_command(
            SimpleNamespace(
                inputs=[str(a), str(tmp_path / "gone.json")],
                output=str(tmp_path / "o.json"),
            )
        )
        == 1
    )
    assert "not found" in capsys.readouterr().err
