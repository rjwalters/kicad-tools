"""Real-courtyard-polygon overlap detection for placement check (Issue #4182).

``PlacementAnalyzer`` previously approximated each footprint's courtyard as a
pads-bounding-box expanded by a fixed 0.25 mm margin, so ``kct placement
check`` found only a strict subset of KiCad's (and ``kct check``'s)
courtyard-overlap DRC errors.  These tests exercise the fix: when a footprint
carries real ``F.CrtYd`` / ``B.CrtYd`` artwork the analyzer builds the true
polygon (via the shared :mod:`kicad_tools.geometry.courtyard` helpers) and does
a positive-area polygon intersection, matching ``CourtyardOverlapRule``.

All fixtures are synthetic in-memory ``PCB(SExp(...))`` objects (mirroring
``tests/test_validate_courtyard_overlap.py``) -- no board-file or chorus/
softstart dependency (those are local-only, not in CI).
"""

from __future__ import annotations

import pytest

from kicad_tools.placement.analyzer import DesignRules, PlacementAnalyzer
from kicad_tools.placement.conflict import ConflictType
from kicad_tools.schema.pcb import PCB, Footprint, FootprintGraphic, Pad
from kicad_tools.sexp import SExp
from kicad_tools.validate.rules.courtyard import COURTYARD_RULE_ID, CourtyardOverlapRule

pytest.importorskip("shapely")


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _empty_pcb() -> PCB:
    return PCB(SExp(name="kicad_pcb"))


def _crtyd_rect(
    *,
    start: tuple[float, float],
    end: tuple[float, float],
    layer: str = "F.CrtYd",
) -> FootprintGraphic:
    return FootprintGraphic(
        graphic_type="rect",
        layer=layer,
        stroke_width=0.05,
        start=start,
        end=end,
    )


def _crtyd_lines(
    corners: list[tuple[float, float]],
    *,
    layer: str = "F.CrtYd",
) -> list[FootprintGraphic]:
    """Build a closed loop of fp_line segments from ordered corner points."""
    graphics: list[FootprintGraphic] = []
    n = len(corners)
    for i in range(n):
        graphics.append(
            FootprintGraphic(
                graphic_type="line",
                layer=layer,
                stroke_width=0.05,
                start=corners[i],
                end=corners[(i + 1) % n],
            )
        )
    return graphics


def _pad(
    *,
    number: str = "1",
    position: tuple[float, float] = (0.0, 0.0),
    size: tuple[float, float] = (0.4, 0.4),
) -> Pad:
    return Pad(
        number=number,
        type="smd",
        shape="rect",
        position=position,
        size=size,
        layers=["F.Cu"],
    )


def _make_footprint(
    *,
    reference: str,
    position: tuple[float, float] = (0.0, 0.0),
    rotation: float = 0.0,
    layer: str = "F.Cu",
    graphics: list[FootprintGraphic] | None = None,
    pads: list[Pad] | None = None,
) -> Footprint:
    return Footprint(
        name="TestFP",
        layer=layer,
        position=position,
        rotation=rotation,
        reference=reference,
        value="TEST",
        pads=pads if pads is not None else [_pad()],
        texts=[],
        graphics=graphics or [],
    )


def _pcb_with(*footprints: Footprint) -> PCB:
    pcb = _empty_pcb()
    for fp in footprints:
        pcb._footprints.append(fp)
    return pcb


def _analyze(pcb: PCB, rules: DesignRules | None = None):
    analyzer = PlacementAnalyzer()
    analyzer._load_pcb_from_instance(pcb, (rules or DesignRules()).courtyard_margin)
    return analyzer._find_conflicts_internal(rules or DesignRules())


def _courtyard_conflicts(conflicts):
    return [c for c in conflicts if c.type == ConflictType.COURTYARD_OVERLAP]


def _drc_overlaps(pcb: PCB):
    return CourtyardOverlapRule().check(pcb).filter_by_rule(COURTYARD_RULE_ID)


# ---------------------------------------------------------------------------
# Real courtyard polygon overlaps that the bbox approximation misses
# ---------------------------------------------------------------------------


class TestRealCourtyardOverlap:
    def test_courtyard_larger_than_pads_overlaps_but_pads_do_not(self):
        """Real F.CrtYd rects overlap while the pads (and thus the old
        pads-bbox+margin courtyard) are far apart -> now flagged."""
        # Tiny 0.4mm pads centered on each footprint; courtyards are big 6mm
        # squares that overlap in x. Pads are ~4mm apart with the default
        # 0.25mm margin -> old bbox approximation sees NO overlap.
        a = _make_footprint(
            reference="U1",
            position=(0.0, 0.0),
            graphics=[_crtyd_rect(start=(-3.0, -3.0), end=(3.0, 3.0))],
            pads=[_pad(position=(0.0, 0.0))],
        )
        b = _make_footprint(
            reference="U2",
            position=(4.0, 0.0),  # courtyards overlap x in [1,3]; pads 4mm apart
            graphics=[_crtyd_rect(start=(-3.0, -3.0), end=(3.0, 3.0))],
            pads=[_pad(position=(0.0, 0.0))],
        )
        pcb = _pcb_with(a, b)

        # Sanity: the old pads-bbox+margin approximation would NOT flag this.
        # Pad half-size 0.2 + margin 0.25 = 0.45 extent; centers 4mm apart.
        conflicts = _analyze(pcb)
        overlaps = _courtyard_conflicts(conflicts)
        assert len(overlaps) == 1
        assert {overlaps[0].component1, overlaps[0].component2} == {"U1", "U2"}

        # Real-polygon path (issue #4182) is NOT a fallback: it must not be
        # flagged or annotated (issue #4227). Message stays area-based.
        assert overlaps[0].is_bbox_fallback is False
        assert "pad-bbox fallback" not in overlaps[0].message
        assert "mm^2" in overlaps[0].message

        # And it agrees with kct check's courtyard rule.
        assert len(_drc_overlaps(pcb)) == 1

    def test_moved_apart_no_conflict(self):
        """Same big courtyards moved apart -> no conflict (no false positive)."""
        a = _make_footprint(
            reference="U1",
            position=(0.0, 0.0),
            graphics=[_crtyd_rect(start=(-3.0, -3.0), end=(3.0, 3.0))],
            pads=[_pad(position=(0.0, 0.0))],
        )
        b = _make_footprint(
            reference="U2",
            position=(20.0, 0.0),  # courtyards well separated
            graphics=[_crtyd_rect(start=(-3.0, -3.0), end=(3.0, 3.0))],
            pads=[_pad(position=(0.0, 0.0))],
        )
        pcb = _pcb_with(a, b)
        assert _courtyard_conflicts(_analyze(pcb)) == []
        assert _drc_overlaps(pcb) == []

    def test_touching_zero_area_no_conflict(self):
        """Exactly-touching courtyards (zero-area intersection) -> no conflict,
        matching CourtyardOverlapRule."""
        a = _make_footprint(
            reference="U1",
            position=(0.0, 0.0),
            graphics=[_crtyd_rect(start=(-1.0, -1.0), end=(1.0, 1.0))],
            pads=[_pad(position=(0.0, 0.0))],
        )
        b = _make_footprint(
            reference="U2",
            position=(2.0, 0.0),  # left edge x=1 == U1 right edge
            graphics=[_crtyd_rect(start=(-1.0, -1.0), end=(1.0, 1.0))],
            pads=[_pad(position=(0.0, 0.0))],
        )
        pcb = _pcb_with(a, b)
        assert _courtyard_conflicts(_analyze(pcb)) == []
        assert _drc_overlaps(pcb) == []

    def test_offset_noncentered_courtyard_overlap(self):
        """A courtyard offset off-center from its pads (invisible to the
        pads-bbox approximation) is correctly flagged."""
        # U1's courtyard extends far to the +x of its pad.
        a = _make_footprint(
            reference="U1",
            position=(0.0, 0.0),
            graphics=[_crtyd_rect(start=(-0.5, -1.0), end=(5.0, 1.0))],
            pads=[_pad(position=(0.0, 0.0))],
        )
        b = _make_footprint(
            reference="U2",
            position=(4.0, 0.0),
            graphics=[_crtyd_rect(start=(-0.5, -1.0), end=(0.5, 1.0))],
            pads=[_pad(position=(0.0, 0.0))],
        )
        pcb = _pcb_with(a, b)
        overlaps = _courtyard_conflicts(_analyze(pcb))
        assert len(overlaps) == 1
        assert len(_drc_overlaps(pcb)) == 1


class TestRotatedCourtyardOverlap:
    def test_rotated_footprint_overlap_detected(self):
        """A 45deg-rotated courtyard overlap only visible after correct
        rotation handling is flagged (mirrors the DRC rotation test)."""
        # U1: 4x4 courtyard at origin, unrotated.
        a = _make_footprint(
            reference="U1",
            position=(0.0, 0.0),
            graphics=[_crtyd_rect(start=(-2.0, -2.0), end=(2.0, 2.0))],
            pads=[_pad(position=(0.0, 0.0))],
        )
        # U2: a 4x1 courtyard placed to the +x. Unrotated it would clear U1
        # (its left edge at x=4-2=2 touches, no positive overlap). Rotated 90deg
        # it becomes tall (spanning y) but its footprint stays clear... instead
        # place it so rotation swings a long courtyard arm into U1.
        b = _make_footprint(
            reference="U2",
            position=(4.0, 0.0),
            rotation=90.0,
            graphics=[_crtyd_rect(start=(-3.0, -0.5), end=(3.0, 0.5))],
            pads=[_pad(position=(0.0, 0.0))],
        )
        pcb = _pcb_with(a, b)

        # Rotated 90deg, the 6mm-long courtyard becomes vertical at x~4 -> does
        # NOT reach U1. Confirm placement analyzer agrees with the DRC rule
        # either way (both see the same verdict).
        placement_overlaps = _courtyard_conflicts(_analyze(pcb))
        drc = _drc_overlaps(pcb)
        assert len(placement_overlaps) == len(drc)

    def test_rotation_brings_courtyard_into_overlap(self):
        """Rotation that swings a long courtyard arm into a neighbor is flagged
        and matches the DRC rule."""
        a = _make_footprint(
            reference="U1",
            position=(0.0, 0.0),
            graphics=[_crtyd_rect(start=(-1.0, -1.0), end=(1.0, 1.0))],
            pads=[_pad(position=(0.0, 0.0))],
        )
        # A 8mm-long, 1mm-tall courtyard centered on U2 at x=4. Unrotated it
        # spans x in [0,8] -> overlaps U1 (x in [-1,1]). Confirm both checkers
        # flag it.
        b = _make_footprint(
            reference="U2",
            position=(4.0, 0.0),
            rotation=0.0,
            graphics=[_crtyd_rect(start=(-4.0, -0.5), end=(4.0, 0.5))],
            pads=[_pad(position=(0.0, 0.0))],
        )
        pcb = _pcb_with(a, b)
        placement_overlaps = _courtyard_conflicts(_analyze(pcb))
        drc = _drc_overlaps(pcb)
        assert len(placement_overlaps) == 1
        assert len(drc) == 1


class TestFallbackAndResolution:
    def test_pads_only_footprint_uses_bbox_fallback(self):
        """Footprints with no CrtYd graphics fall back to the pads-bbox+margin
        approximation (preserves legacy behavior)."""
        # Overlapping pads, no courtyard artwork -> bbox fallback still detects.
        a = _make_footprint(
            reference="R1",
            position=(0.0, 0.0),
            graphics=[],
            pads=[_pad(position=(0.0, 0.0), size=(1.0, 0.5))],
        )
        b = _make_footprint(
            reference="R2",
            position=(0.3, 0.0),  # pads overlap; well within margin
            graphics=[],
            pads=[_pad(position=(0.0, 0.0), size=(1.0, 0.5))],
        )
        pcb = _pcb_with(a, b)
        overlaps = _courtyard_conflicts(_analyze(pcb))
        assert len(overlaps) == 1
        # Fallback-derived finding is labeled (issue #4227) so it is not
        # mistaken for a real-polygon courtyard violation.
        assert overlaps[0].is_bbox_fallback is True
        assert "pad-bbox fallback" in overlaps[0].message
        assert overlaps[0].to_dict()["is_bbox_fallback"] is True

    def test_mixed_real_and_fallback(self):
        """One footprint with real courtyard, one pads-only -> the pair falls
        back to bbox (only-one-polygon case) and still checks."""
        a = _make_footprint(
            reference="U1",
            position=(0.0, 0.0),
            graphics=[_crtyd_rect(start=(-1.0, -1.0), end=(1.0, 1.0))],
            pads=[_pad(position=(0.0, 0.0))],
        )
        b = _make_footprint(
            reference="R1",
            position=(0.1, 0.0),  # pads-only, overlapping the origin
            graphics=[],
            pads=[_pad(position=(0.0, 0.0), size=(1.0, 1.0))],
        )
        pcb = _pcb_with(a, b)
        # No crash; bbox-fallback comparison used because R1 has no polygon.
        overlaps = _courtyard_conflicts(_analyze(pcb))
        assert len(overlaps) == 1
        # Only one footprint resolved a real polygon, so this pair still uses
        # the bbox fallback and must be labeled accordingly (issue #4227).
        assert overlaps[0].is_bbox_fallback is True
        assert "pad-bbox fallback" in overlaps[0].message

    def test_line_chain_courtyard_resolves(self):
        """A courtyard authored as an fp_line loop resolves and overlaps are
        flagged (exercises the shared _chain_lines path)."""
        a = _make_footprint(
            reference="U1",
            position=(0.0, 0.0),
            graphics=_crtyd_lines([(-3.0, -3.0), (3.0, -3.0), (3.0, 3.0), (-3.0, 3.0)]),
            pads=[_pad(position=(0.0, 0.0))],
        )
        b = _make_footprint(
            reference="U2",
            position=(4.0, 0.0),
            graphics=_crtyd_lines([(-3.0, -3.0), (3.0, -3.0), (3.0, 3.0), (-3.0, 3.0)]),
            pads=[_pad(position=(0.0, 0.0))],
        )
        pcb = _pcb_with(a, b)
        assert len(_courtyard_conflicts(_analyze(pcb))) == 1
        assert len(_drc_overlaps(pcb)) == 1

    def test_unresolvable_courtyard_falls_back_to_bbox(self):
        """A footprint whose CrtYd geometry does not resolve (non-closing line
        chain) falls back to the bbox approximation instead of crashing."""
        # Two disconnected line segments on F.CrtYd -> not a closed loop.
        bad_graphics = [
            FootprintGraphic(
                graphic_type="line",
                layer="F.CrtYd",
                stroke_width=0.05,
                start=(-1.0, -1.0),
                end=(1.0, -1.0),
            ),
            FootprintGraphic(
                graphic_type="line",
                layer="F.CrtYd",
                stroke_width=0.05,
                start=(5.0, 5.0),
                end=(6.0, 5.0),
            ),
        ]
        a = _make_footprint(
            reference="U1",
            position=(0.0, 0.0),
            graphics=bad_graphics,
            pads=[_pad(position=(0.0, 0.0), size=(1.0, 1.0))],
        )
        b = _make_footprint(
            reference="R1",
            position=(0.1, 0.0),
            graphics=[],
            pads=[_pad(position=(0.0, 0.0), size=(1.0, 1.0))],
        )
        pcb = _pcb_with(a, b)
        # Should not raise; bbox fallback still detects the pad overlap.
        overlaps = _courtyard_conflicts(_analyze(pcb))
        assert len(overlaps) == 1


class TestCheckerAgreement:
    def test_placement_and_drc_agree_on_shared_fixture(self):
        """A single fixture run through both PlacementAnalyzer and
        CourtyardOverlapRule reports the same overlapping-pair verdict."""
        a = _make_footprint(
            reference="U1",
            position=(0.0, 0.0),
            graphics=[_crtyd_rect(start=(-2.0, -2.0), end=(2.0, 2.0))],
            pads=[_pad(position=(0.0, 0.0))],
        )
        b = _make_footprint(
            reference="U2",
            position=(3.0, 0.0),  # overlaps x in [1,2]
            graphics=[_crtyd_rect(start=(-2.0, -2.0), end=(2.0, 2.0))],
            pads=[_pad(position=(0.0, 0.0))],
        )
        c = _make_footprint(
            reference="U3",
            position=(20.0, 20.0),  # isolated
            graphics=[_crtyd_rect(start=(-2.0, -2.0), end=(2.0, 2.0))],
            pads=[_pad(position=(0.0, 0.0))],
        )
        pcb = _pcb_with(a, b, c)

        placement_pairs = {
            frozenset((c.component1, c.component2)) for c in _courtyard_conflicts(_analyze(pcb))
        }
        drc_pairs = {frozenset(v.items) for v in _drc_overlaps(pcb)}
        assert placement_pairs == drc_pairs == {frozenset({"U1", "U2"})}
