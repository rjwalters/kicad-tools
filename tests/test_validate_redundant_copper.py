"""Tests for the redundant-copper advisory rule (Issue #8218)."""

from __future__ import annotations

from dataclasses import dataclass, field

from kicad_tools.validate.rules.redundant_copper import RedundantCopperRule


@dataclass
class _Seg:
    start: tuple[float, float]
    end: tuple[float, float]
    width: float = 0.2
    layer: str = "F.Cu"
    net_number: int = 1
    net_name: str = "SIG"


@dataclass
class _Via:
    position: tuple[float, float]
    layers: tuple[str, ...] = ("F.Cu", "B.Cu")
    net_number: int = 1
    net_name: str = "SIG"
    size: float = 0.6


@dataclass
class _PCB:
    segments: list = field(default_factory=list)
    vias: list = field(default_factory=list)
    board_origin: tuple[float, float] = (0.0, 0.0)


def _run(segments=(), vias=()):
    return RedundantCopperRule().check(_PCB(list(segments), list(vias)), None)


def _ids(results):
    return [v.rule_id for v in results.violations]


def test_clean_board_has_no_findings():
    res = _run([_Seg((0, 0), (5, 0)), _Seg((5, 0), (5, 5))])
    assert res.violations == []


def test_fifteen_duplicates_report_fifteen():
    segs = [_Seg((0, 0), (5, 0))] + [_Seg((5, 0), (0, 0)) for _ in range(15)]
    res = _run(segs)
    assert _ids(res).count("redundant_copper_duplicate") == 15
    assert all(v.severity == "warning" and v.location for v in res.violations)


def test_different_width_is_not_duplicate():
    res = _run([_Seg((0, 0), (5, 0)), _Seg((0, 0), (5, 0), width=0.3)])
    assert "redundant_copper_duplicate" not in _ids(res)


def test_fragment_run():
    segs = [_Seg((i * 0.08, 0), ((i + 1) * 0.08, 0)) for i in range(4)]
    res = _run(segs)
    assert _ids(res) == ["redundant_copper_fragments"]


def test_two_fragments_below_threshold():
    segs = [_Seg((i * 0.08, 0), ((i + 1) * 0.08, 0)) for i in range(2)]
    assert _run(segs).violations == []


def test_detour():
    segs = [_Seg((0, 0), (0, 5)), _Seg((0, 5), (1, 5)), _Seg((1, 5), (1, 0))]
    res = _run(segs)
    assert _ids(res) == ["redundant_copper_detour"]


def test_straight_chain_not_detour():
    segs = [_Seg((0, 0), (3, 0)), _Seg((3, 0), (6, 0))]
    assert _run(segs).violations == []


def test_via_offcentre_flagged():
    res = _run([_Seg((0, 0), (4.8, 0))], [_Via((5.0, 0))])
    assert _ids(res) == ["redundant_copper_via_offcentre"]


def test_via_centred_and_other_net_not_flagged():
    assert _run([_Seg((0, 0), (5, 0))], [_Via((5.0, 0))]).violations == []
    other = _Via((5.0, 0), net_number=2)
    assert _run([_Seg((0, 0), (4.8, 0))], [other]).violations == []


def test_via_centred_entry_with_bend_not_flagged():
    # Track enters the via centre with a 45-degree bend ~0.35 mm away; the
    # bend vertex is shared by two segments and must not be flagged.
    segs = [
        _Seg((0, 0), (4.75, 0)),
        _Seg((4.75, 0), (5.0, 0.25)),  # ends exactly on the via centre
    ]
    assert _run(segs, [_Via((5.0, 0.25))]).violations == []


def test_via_offcentre_unshared_end_flagged_when_via_not_centred():
    # Free end 0.2 mm short of the via, nothing else on that layer.
    res = _run([_Seg((0, 0), (4.8, 0))], [_Via((5.0, 0))])
    assert _ids(res) == ["redundant_copper_via_offcentre"]


def test_covered_segment_reported():
    res = _run([_Seg((0, 0), (10, 0)), _Seg((2, 0), (5, 0))])
    ids = _ids(res)
    assert ids.count("redundant_copper_covered") == 1
    v = next(v for v in res.violations if v.rule_id == "redundant_copper_covered")
    assert v.severity == "warning" and "kct pcb prune" in v.message
    assert "within the copper" in v.message
    assert v.location == (2, 0)


def test_exact_duplicate_not_reported_as_covered():
    res = _run([_Seg((0, 0), (5, 0)), _Seg((5, 0), (0, 0))])
    assert _ids(res) == ["redundant_copper_duplicate"]


def test_covered_negatives():
    base = _Seg((0, 0), (10, 0))
    for other in (
        _Seg((8, 0), (12, 0)),  # partial overlap
        _Seg((2, 0), (5, 0), layer="B.Cu"),  # different layer
        _Seg((2, 0), (5, 0), net_number=2, net_name="OTHER"),  # different net
        _Seg((2, 0), (5, 1)),  # non-collinear
    ):
        res = _run([base, other])
        assert "redundant_copper_covered" not in _ids(res)


def test_narrower_container_not_covering():
    res = _run([_Seg((0, 0), (10, 0), width=0.1), _Seg((2, 0), (5, 0), width=0.2)])
    assert "redundant_copper_covered" not in _ids(res)


# --- redundant_copper_staircase (Issue #8831) -------------------------------

_STAIR_PTS = [
    (0, 0),
    (4, 0),
    (6, 2),
    (10, 2),
    (12, 4),
    (16, 4),
    (18, 6),
    (22, 6),
    (22, 8),
    (24, 10),
]


def _staircase(width=2.6):
    return [
        _Seg(a, b, width=width, net_name="PWR")
        for a, b in zip(_STAIR_PTS, _STAIR_PTS[1:], strict=False)
    ]


def _stair_findings(res):
    return [v for v in res.violations if v.rule_id == "redundant_copper_staircase"]


def test_staircase_with_free_straight_path_is_flagged_once():
    found = _stair_findings(_run(_staircase()))
    assert len(found) == 1
    assert found[0].severity == "warning"
    assert found[0].location == (0, 0)
    assert "(24.00, 10.00)" in found[0].message and "saves" in found[0].message


def test_staircase_not_flagged_when_alternative_hits_foreign_copper():
    # Foreign track sits on the diagonal/horizontal replacement, clear of the
    # original staircase's own legs.
    blocker = _Seg((12, 9), (20, 9), width=0.2, net_number=2, net_name="OTHER")
    # replacement diag (0,0)->(10,10) then H to (24,10): blocker at y=9 is hit
    # by the H leg's 2.6 mm width; the orthogonal-first path hits it too.
    blocker2 = _Seg((5, 0.5), (5, 3), width=0.2, net_number=2, net_name="OTHER")
    res = _run([*_staircase(), blocker, blocker2])
    assert _stair_findings(res) == []


def test_serpentine_is_not_a_staircase():
    pts = [(0, 0), (0, 5), (2, 5), (2, 1), (4, 1), (4, 6)]
    segs = [_Seg(a, b) for a, b in zip(pts, pts[1:], strict=False)]
    assert _stair_findings(_run(segs)) == []


def test_already_optimal_path_is_not_a_staircase():
    res = _run([_Seg((0, 0), (3, 3)), _Seg((3, 3), (9, 3)), _Seg((9, 3), (12, 3))])
    assert _stair_findings(res) == []


def test_staircase_message_uses_sheet_absolute_coordinates_with_origin():
    """Issue #8868: message coordinates share the frame of ``location``."""
    from kicad_tools.validate.checker import DRCChecker

    origin = (68.5, 55.0)
    pcb = _PCB(_staircase(), [], origin)
    # Same path DRCChecker.check_redundant_copper takes.
    checker = DRCChecker.__new__(DRCChecker)
    checker.pcb = pcb
    res = checker._absolutize(RedundantCopperRule().check(pcb, None))
    (v,) = _stair_findings(res)
    assert v.location == (68.5, 55.0)
    assert f"from ({v.location[0]:.2f}, {v.location[1]:.2f})" in v.message
    assert "(92.50, 65.00)" in v.message
    assert "(24.00, 10.00)" not in v.message


def test_staircase_message_unchanged_at_zero_origin():
    (v,) = _stair_findings(_run(_staircase()))
    assert "from (0.00, 0.00) to (24.00, 10.00)" in v.message
