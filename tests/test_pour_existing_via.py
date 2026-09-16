"""Reuse a destination barrel without waiving new copper's physical rules."""

from shapely.geometry import LineString, Point, box

from kicad_tools.zones import pour_escape


def bridge(*, segments=(), vias=(), destination_layers=frozenset({"B.Cu"})):
    existing = (Point(4, 0), "power", 0.25, 0.3)
    return pour_escape.find_bridge_to_via(
        box(-2, -2, 0, 2),
        (4, 0),
        destination_layers,
        "power",
        [],
        list(segments),
        [existing, *vias],
        (-5, -5, 8, 5),
        pour_escape.EscapeRules(),
    )


def test_reuses_existing_barrel_instead_of_colliding_with_its_drill():
    result = bridge()
    assert result is not None
    assert result.points[-1] == (4, 0)
    assert result.points[0] == (-0.5, 0)
    assert result.layer == "B.Cu"
    assert result.rules.diameter == 0.5
    assert result.rules.drill == 0.3


def test_blocked_destination_span_cannot_escape_on_an_unspanned_layer():
    barrier = (LineString([(2, -4), (2, 4)]).buffer(0.1), "foreign", "B.Cu")
    assert bridge(segments=[barrier]) is None
    assert bridge(segments=[barrier], destination_layers=frozenset({"In1.Cu"})) is not None


def test_foreign_barrel_blocks_every_bridge_layer():
    assert bridge(vias=[(Point(2, 0), "foreign", 0.25, 0.3)]) is None


def test_all_new_via_offsets_obey_same_net_drill_spacing():
    obstacles = [(Point(x, 0), "power", 0.25, 0.3) for x in (-0.5, -0.9, -1.4)]
    assert bridge(vias=obstacles) is None


def test_destination_must_be_an_existing_same_net_via():
    assert (
        pour_escape.find_bridge_to_via(
            box(-2, -2, 0, 2),
            (4, 0),
            frozenset({"B.Cu"}),
            "power",
            [],
            [],
            [],
            (-5, -5, 8, 5),
            pour_escape.EscapeRules(),
        )
        is None
    )


def test_authored_clearance_rejects_gap_accepted_by_legacy_point15_floor():
    # The emitted 0.2 mm track would leave only 0.18 mm copper gap.
    obstacle = (LineString([(1, 0.38), (5, 0.38)]).buffer(0.1), "foreign", "B.Cu")
    assert bridge(segments=[obstacle]) is None
