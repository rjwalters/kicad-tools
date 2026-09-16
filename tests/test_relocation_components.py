"""Alternate airwire witnesses must preserve copper, not just counts or pads."""

import copy

import pytest

from kicad_tools.cli.relocation_components import compare_components


def state():
    return {
        "inventory": {key: {"kind": "PCB_TRACK", "net": "GND"} for key in "abcd"},
        "islands": {},
        "groups": [["a", "b"], ["c", "d"]],
    }


def test_equal_count_padless_split_and_merge_refused():
    before = state()
    after = copy.deepcopy(before)
    after["groups"] = [["a"], ["b", "c", "d"]]
    with pytest.raises(ValueError, match="complete native copper"):
        compare_components(before, after, set())


def test_reordering_is_not_a_connectivity_change():
    before = state()
    after = copy.deepcopy(before)
    after["groups"] = [["d", "c"], ["b", "a"]]
    compare_components(before, after, set())


def with_islands():
    result = state()
    for key, x in [("left", 0), ("right", 10)]:
        result["inventory"][key] = {"kind": "ZONE", "net": "GND"}
        result["islands"][key] = {
            "zone": "one-parent",
            "layer": 0,
            "outer": [[x, 0], [x + 2, 0], [x + 2, 2], [x, 2]],
            "holes": [],
        }
        result["groups"].append([key])
    return result


def test_disconnected_islands_under_same_zone_are_preserved():
    before = with_islands()
    after = copy.deepcopy(before)
    after["groups"] = before["groups"][:2] + [["left", "right"]]
    with pytest.raises(ValueError, match="complete native copper"):
        compare_components(before, after, set())


def test_island_order_uses_geometry_not_index_identity():
    before = with_islands()
    after = copy.deepcopy(before)
    after["islands"]["left"], after["islands"]["right"] = (
        after["islands"]["right"],
        after["islands"]["left"],
    )
    compare_components(before, after, set())


@pytest.mark.parametrize("change", ["split", "merge", "layer", "hole", "invalid"])
def test_island_changes_refuse(change):
    before = with_islands()
    after = copy.deepcopy(before)
    if change == "split":
        after["islands"]["right"]["outer"] = [[1, 0], [2, 0], [2, 2], [1, 2]]
    elif change == "merge":
        after["islands"]["left"]["outer"] = [[0, 0], [12, 0], [12, 2], [0, 2]]
    elif change == "layer":
        after["islands"]["left"]["layer"] = 2
    elif change == "hole":
        # The old island is entirely within a new hole, so bounding-box
        # overlap is not evidence of surviving physical copper.
        after["islands"]["left"]["outer"] = [[-2, -2], [4, -2], [4, 4], [-2, 4]]
        after["islands"]["left"]["holes"] = [[[-1, -1], [3, -1], [3, 3], [-1, 3]]]
    else:
        after["islands"]["left"]["outer"] = [[0, 0], [2, 2], [2, 0], [0, 2]]
    with pytest.raises(ValueError):
        compare_components(before, after, set())


@pytest.mark.parametrize("detached", [False, True])
def test_added_stub_must_join_relocated_via(detached):
    before = state()
    before["inventory"]["a"]["kind"] = "PCB_VIA"
    after = copy.deepcopy(before)
    after["inventory"]["stub"] = {"kind": "PCB_TRACK", "net": "GND"}
    after["groups"][1 if detached else 0].append("stub")
    if detached:
        with pytest.raises(ValueError, match="detached"):
            compare_components(before, after, {"a"})
    else:
        compare_components(before, after, {"a"})


@pytest.mark.parametrize("change", ["missing", "duplicate", "net", "kind", "unbound_zone"])
def test_malformed_or_changed_inventory_refuses(change):
    before = state()
    after = copy.deepcopy(before)
    if change == "missing":
        del after["inventory"]["a"]
    elif change == "duplicate":
        after["groups"][1].append("a")
    elif change == "net":
        after["inventory"]["a"]["net"] = "OTHER"
    elif change == "kind":
        after["inventory"]["a"]["kind"] = "UNKNOWN"
    else:
        after["inventory"]["a"]["kind"] = "ZONE"
    with pytest.raises(ValueError):
        compare_components(before, after, set())
