"""Bounded local recovery must preserve the signal cut's external contacts."""

import json
from pathlib import Path

import pytest
from shapely import wkt
from shapely.geometry import Point

from kicad_tools.zones import local_detour as detour
from kicad_tools.zones.pour_escape import EscapeRules


@pytest.fixture
def captured():
    data = json.loads((Path(__file__).parent / "fixtures/board07_local_detour.json").read_text())
    return {
        "start": tuple(data["start"]),
        "net": data["net"],
        "layer": data["layer"],
        "bounds": tuple(data["bounds"]),
        "rules": EscapeRules(),
        "signal_rules": {"TMDS_D1_N": EscapeRules()},
        "tracks": [
            detour.Track(**{**t, "start": tuple(t["start"]), "end": tuple(t["end"])})
            for t in data["tracks"]
        ],
        "pads": [(wkt.loads(g), n, set(l), d, tuple(c)) for g, n, l, d, c in data["pads"]],
        "vias": [(Point(c), n, r, d) for c, n, r, d in data["vias"]],
        "primary": [(wkt.loads(g), set(l), n) for g, l, n in data["primary"]],
    }


def test_captured_enclosure_has_a_bounded_anchored_detour(captured, monkeypatch):
    before = repr(captured["tracks"]), [p[0].wkb for p in captured["pads"]]
    calls = []
    original = detour.find_escape

    def observed(*args, **kwargs):
        calls.append(kwargs["node_budget"])
        return original(*args, **kwargs)

    monkeypatch.setattr(detour, "find_escape", observed)
    plan = detour.plan_local_detour(**captured)
    assert plan is not None
    assert plan.signal_net == "TMDS_D1_N"
    assert len(plan.removed_tracks) == 8
    assert [layer for layer, _ in plan.signal_paths] == ["F.Cu", "F.Cu", "B.Cu"]
    assert sum(path.via for _, path in plan.signal_paths) == 2
    assert plan.power.points[0] == pytest.approx(captured["start"], abs=1e-6)
    assert sum(calls) <= 200_000
    assert before == (repr(captured["tracks"]), [p[0].wkb for p in captured["pads"]])


def test_unlisted_net_cannot_be_cut(captured):
    captured["signal_rules"] = {}
    assert detour.plan_local_detour(**captured) is None


def test_unanchored_cut_is_rejected(captured):
    # Remove the pad at one cut terminal; a route into empty space is invalid.
    captured["pads"] = [p for p in captured["pads"] if p[1] != "TMDS_D1_N"]
    assert detour.plan_local_detour(**captured) is None


def test_disjoint_cycle_cannot_accompany_a_simple_cut():
    edges = [
        ((0, 0), (1, 0)),
        ((1, 0), (2, 0)),
        ((3, 0), (4, 0)),
        ((4, 0), (4, 1)),
        ((4, 1), (3, 0)),
    ]
    tracks = [detour.Track(str(i), "N", "F.Cu", a, b, 0.2) for i, (a, b) in enumerate(edges)]
    assert detour._path_ends(tracks[:2]) == ((0, 0), (2, 0))
    assert detour._path_ends(tracks) is None


def test_duplicate_identifiers_are_rejected(captured):
    captured["tracks"].append(captured["tracks"][0])
    with pytest.raises(ValueError, match="unique"):
        detour.plan_local_detour(**captured)


def test_pair_matching_includes_new_via_length_and_preserves_endpoints():
    import math

    from kicad_tools.zones.pour_escape import Escape

    rules = EscapeRules()
    tracks = [
        detour.Track("n", "N", "F.Cu", (0, 0), (10, 0), 0.2),
        detour.Track("p", "P", "F.Cu", (0, 5), (8, 5), 0.2),
    ]
    plan = detour.LocalDetour(
        "N",
        ("n",),
        Escape(((20, 20), (21, 20)), False, rules),
        (
            ("F.Cu", Escape(((0, 0), (0, 1)), True, rules)),
            ("F.Cu", Escape(((10, 0), (10, 1)), True, rules)),
            ("B.Cu", Escape(((0, 1), (10, 1)), False, rules)),
        ),
    )
    result = detour.match_detour_pair(
        plan,
        power_net="VCC",
        power_layer="F.Cu",
        partner_net="P",
        tracks=tracks,
        pads=[],
        vias=[],
        initial_lengths_mm={"N": 10, "P": 8},
        new_via_length_mm=1.6,
        tolerance_mm=0.075,
        bounds=(-1, -1, 25, 25),
        rules=rules,
    )
    assert result is not None and result.adjustment is not None
    adjustment = result.adjustment
    assert adjustment.net == "P" and adjustment.removed_track == "p"
    points = adjustment.replacement.points
    assert points[0] == tracks[1].start and points[-1] == tracks[1].end
    assert sum(math.dist(a, b) for a, b in zip(points, points[1:], strict=False)) == pytest.approx(
        15.2
    )
    assert result.skew_mm <= 0.075


def test_pair_matching_rejects_a_stale_cut(captured):
    plan = detour.plan_local_detour(**captured)
    assert plan is not None
    remaining = [track for track in captured["tracks"] if track.uuid != plan.removed_tracks[0]]
    with pytest.raises(ValueError, match="no longer matches"):
        detour.match_detour_pair(
            plan,
            power_net=captured["net"],
            power_layer=captured["layer"],
            partner_net="TMDS_D1_P",
            tracks=remaining,
            pads=captured["pads"],
            vias=captured["vias"],
            initial_lengths_mm={},
            new_via_length_mm=1.6,
            tolerance_mm=0.075,
            bounds=captured["bounds"],
            rules=captured["rules"],
        )
