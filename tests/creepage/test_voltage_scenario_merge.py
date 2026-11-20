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
from kicad_tools.router.pairwise_clearance import (
    build_pairwise_clearance_table,
    load_pair_bounds,
    load_signed_voltage_map,
    route_understated_pairs,
)

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
    """Per-pair requirement table built from the merged map (the CENSUS side).

    The merged map's envelope intervals give each pair its envelope |ΔV| (the
    interval-aware census model: ``voltage_map_from_dict`` plus the documented
    worst-case endpoint combination); the generated ``_pair_bounds`` cap it
    back to the worst per-scenario |ΔV|; the shared
    :func:`build_required_by_domain_pair` builder (the SAME builder the census
    consumes) maps |ΔV| -> mm.

    This is NOT the route path: ``kct route`` / ``kct pcb`` read the map
    through the signed-collapse loader (:func:`load_signed_voltage_map`,
    #4411) and can understate a sign-flipping pair -- that consumer path is
    pinned separately by
    :func:`test_route_loader_path_pins_actual_table_and_cli_warns`.
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


@pytest.mark.parametrize(
    "scenario",
    [
        {"/VCC": 48, "VCC": 5},
        {"VCC": 5, "/VCC": 48},
        {"/VCC": 5, "VCC": 5},
    ],
    ids=["slash-first", "bare-first", "equal-voltages"],
)
def test_within_scenario_normalisation_collision_raises(scenario):
    """Two spellings of one net inside a single scenario fail loud (#9665).

    The per-scenario dict comprehension this replaces kept whichever spelling
    came last in insertion order, silently dropping the other declared
    potential from the envelope -- with no error and no warning, unlike every
    other malformed-input path in the function.
    """
    with pytest.raises(ValueError) as ei:
        merge_voltage_scenarios([scenario, {"VCC": 5}])
    msg = str(ei.value)
    # The diagnostic quotes both authored spellings (either insertion order,
    # even when the voltages agree).
    assert "'/VCC'" in msg
    assert "'VCC'" in msg


def test_cross_scenario_spellings_still_merge():
    """The collision guard is per scenario: spellings may differ ACROSS maps."""
    merged = merge_voltage_scenarios([{"/VCC": 48}, {"VCC": 5}])
    assert merged["VCC"] == {"min": 5.0, "max": 48.0}


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

    # The merged file is an ordinary map: the interval-aware loader round-trips
    # it (the route-time signed-collapse loader is pinned separately below).
    loaded = json.loads(out.read_text())
    intervals, edge = voltage_map_from_dict(loaded)
    assert edge == 0.0
    assert intervals["LINE"].lo == -150.0 and intervals["LINE"].hi == 150.0


def test_route_understated_pairs_flags_sign_flipping_pairs():
    merged = merge_voltage_scenarios([SCEN_A, SCEN_B])
    flagged = route_understated_pairs(merged)
    # LINE/F1/F2 all collapse to the same +150 V endpoint: the two 300 V pairs
    # vanish at route time; the follower pair is capped to 0 V by its bound on
    # both sides, so it is not understated.
    assert set(flagged) == {frozenset({"LINE", "F1"}), frozenset({"LINE", "F2"})}
    for census_dv, route_dv in flagged.values():
        assert census_dv == pytest.approx(300.0)
        assert route_dv == 0.0


def test_route_understated_pairs_slash_prefixed_map_honours_bounds():
    """``/``-prefixed maps must key ``_pair_bounds`` and normalise results (#9608).

    ``voltage_map_from_dict`` keeps as-authored keys while
    ``pair_bounds_from_dict`` normalises them, so the detector's bounds lookup
    missed on a KiCad-style map and a declared same-potential pair was falsely
    reported as understated.
    """
    slashed = {
        "/A": {"min": -170, "max": 170},
        "/B": {"min": -170, "max": 170},
        "_pair_bounds": [{"nets": ["/A", "/B"], "max_dv": 0, "why": "same potential"}],
    }
    assert route_understated_pairs(slashed) == {}

    bare = {
        "A": {"min": -170, "max": 170},
        "B": {"min": -170, "max": 170},
        "_pair_bounds": [{"nets": ["A", "B"], "max_dv": 0, "why": "same potential"}],
    }
    assert route_understated_pairs(bare) == {}

    # Without the bound the pair is genuinely understated, and the returned
    # keys are normalised (dropping the leading ``/``), per the docstring.
    flagged = route_understated_pairs(
        {"/A": {"min": -170, "max": 170}, "/B": {"min": -170, "max": 170}}
    )
    assert set(flagged) == {frozenset({"A", "B"})}
    assert flagged[frozenset({"A", "B"})] == (pytest.approx(340.0), pytest.approx(0.0))


def test_route_understated_pairs_clean_without_sign_spans():
    # All-scalar maps: the collapse is the identity, nothing is understated.
    assert route_understated_pairs({"/A": 150, "/B": -150}) == {}
    # Ranges that never cross zero collapse to an endpoint that still
    # reproduces the census worst case.
    assert (
        route_understated_pairs(
            merge_voltage_scenarios([{"/A": 100, "/B": 0}, {"/A": 90, "/B": 0}])
        )
        == {}
    )


def test_route_loader_path_pins_actual_table_and_cli_warns(tmp_path, capsys):
    """The REAL route path: ``load_signed_voltage_map`` + ``load_pair_bounds``.

    Pins what ``kct route`` / ``kct pcb`` actually build from the CLI-written
    envelope (the #4411 signed collapse), so the regression the merge enables
    -- HV pairs dropping out of the route widening matrix -- is recorded in
    the suite instead of rediscovered in copper, and the CLI's stderr warning
    is pinned to name exactly the affected pairs.
    """
    a = tmp_path / "fwd.json"
    b = tmp_path / "rev.json"
    out = tmp_path / "env.json"
    a.write_text(json.dumps(SCEN_A))
    b.write_text(json.dumps(SCEN_B))

    rc = run_voltage_map_merge_command(SimpleNamespace(inputs=[str(a), str(b)], output=str(out)))
    assert rc == 0

    loaded = load_signed_voltage_map(out)
    bounds = load_pair_bounds(out)
    t_route = build_pairwise_clearance_table(
        loaded, dru=DRU, hv_threshold=THRESH, pair_bounds=bounds
    ).required_by_pair

    # LINE/F1/F2 collapse to the same +150 V endpoint, so the 300 V pairs
    # (LINE-F1, LINE-F2) drop out entirely; F1-F2 stays out via its
    # max_dv: 0 bound; only the 150 V AUX pairs survive -- the documented gap
    # against the census envelope's 300 V entries for the LINE pairs.
    assert set(t_route) == {("AUX", "F1"), ("AUX", "F2"), ("AUX", "LINE")}
    t_a = build_pairwise_clearance_table(SCEN_A, dru=DRU, hv_threshold=THRESH).required_by_pair
    for pair, req in t_route.items():
        assert req == pytest.approx(t_a[pair])  # each survivor matches its per-scenario value

    # The census side (interval-aware) DOES carry the dropped pairs; the
    # signed collapse is exactly what loses them.
    t_census = _merged_required_by_pair(merge_voltage_scenarios([SCEN_A, SCEN_B]))
    for pair in (("F1", "LINE"), ("F2", "LINE")):
        assert pair in t_census
        assert pair not in t_route

    # The CLI warned on stderr, naming exactly the understated pairs.
    err = capsys.readouterr().err
    assert "understated" in err
    assert "F1 <-> LINE" in err and "F2 <-> LINE" in err
    assert "F1 <-> F2" not in err  # the capped follower pair is not understated
    assert "per-scenario" in err


def test_cli_format_json_emits_document(tmp_path, capsys):
    """#4674 idiom: ``--format json`` owns stdout with one parseable document.

    The understated pairs the text mode only warns about on stderr are part
    of the document, so machine consumers need not scrape stderr.
    """
    a = tmp_path / "fwd.json"
    b = tmp_path / "rev.json"
    out = tmp_path / "env.json"
    a.write_text(json.dumps(SCEN_A))
    b.write_text(json.dumps(SCEN_B))

    rc = run_voltage_map_merge_command(
        SimpleNamespace(inputs=[str(a), str(b)], output=str(out), format="json")
    )
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)  # exactly one JSON document
    assert doc["success"] is True
    assert doc["command"] == "voltage-map-merge"
    assert doc["nets"] == 4 and doc["pair_bounds"] == 1
    assert doc["output"] == str(out)
    flagged = {tuple(e["nets"]): e for e in doc["route_understated_pairs"]}
    assert set(flagged) == {("F1", "LINE"), ("F2", "LINE")}
    for entry in flagged.values():
        assert entry["census_dv"] == pytest.approx(300.0)
        assert entry["route_dv"] == pytest.approx(0.0)
    # The envelope artifact itself is the same file the text mode writes.
    assert json.loads(out.read_text())["F1"] == {"min": -150.0, "max": 150.0}


def test_cli_format_json_errors_are_documents(tmp_path, capsys):
    """Failure paths under ``--format json``: ``{"error": ...}``, same codes."""
    a = tmp_path / "a.json"
    a.write_text(json.dumps(SCEN_A))

    rc = run_voltage_map_merge_command(
        SimpleNamespace(inputs=[str(a)], output=str(tmp_path / "o.json"), format="json")
    )
    assert rc == 1  # exit codes unchanged
    doc = json.loads(capsys.readouterr().out)
    assert doc["success"] is False
    assert "at least two" in doc["error"]

    rc = run_voltage_map_merge_command(
        SimpleNamespace(
            inputs=[str(a), str(tmp_path / "gone.json")],
            output=str(tmp_path / "o.json"),
            format="json",
        )
    )
    assert rc == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["success"] is False
    assert "not found" in doc["error"]


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
