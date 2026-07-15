"""Tests for the differential-pair within-pair clearance DRC rule.

Mirrors the stub-PCB pattern from tests/test_validate_single_pad_net.py.
Covers:

- The structural-correctness no-fire case (within-pair separation tighter
  than the manufacturer's inter-pair clearance but >= intra threshold).
- The fire case (within-pair separation < intra threshold).
- Cross-rule no-fire: ClearanceRule must NOT fire on the same edge that
  DiffPairClearanceIntraRule is OK with (the double-violation guard).
- Single-ended exclusion (USB_CC1/USB_CC2): not detected as a pair, so
  the new rule does NOT fire.
- Pads-out-of-scope: a segment-vs-pad gap below intra threshold does not
  fire (the new rule is segment-segment only).
- The to_dict() round-trip trap test that catches the fuzzy-fallback
  miscategorization (Issue #2521 / #2560 critical risk).
- Per-pair threshold lookup (different pairs may carry different
  intra_pair_clearance values).
- Empty-PCB / no-segment edge cases.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kicad_tools.manufacturers import DesignRules
from kicad_tools.validate.rules.clearance import ClearanceRule
from kicad_tools.validate.rules.diffpair_clearance_intra import (
    DiffPairClearanceIntraRule,
)

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class _StubLayer:
    name: str
    type: str = "signal"


@dataclass
class _StubNet:
    number: int
    name: str


@dataclass
class _StubSegment:
    start: tuple[float, float]
    end: tuple[float, float]
    width: float = 0.2
    layer: str = "F.Cu"
    net_number: int = 0
    net_name: str = ""
    uuid: str = ""


@dataclass
class _StubPad:
    number: str
    net_number: int = 0
    net_name: str = ""
    position: tuple[float, float] = (0.0, 0.0)
    size: tuple[float, float] = (1.0, 1.0)
    layers: list[str] = field(default_factory=lambda: ["F.Cu"])
    type: str = "smd"
    shape: str = "rect"
    drill: float = 0.0


@dataclass
class _StubFootprint:
    reference: str = "U1"
    name: str = ""
    position: tuple[float, float] = (0.0, 0.0)
    rotation: float = 0.0
    pads: list[_StubPad] = field(default_factory=list)


@dataclass
class _StubPCB:
    """Minimal PCB stub implementing the subset used by the rule."""

    _nets: dict[int, _StubNet] = field(default_factory=dict)
    _segments: list[_StubSegment] = field(default_factory=list)
    _vias: list[object] = field(default_factory=list)
    _footprints: list[_StubFootprint] = field(default_factory=list)
    _layers: list[_StubLayer] = field(default_factory=lambda: [_StubLayer("F.Cu")])

    @property
    def nets(self) -> dict[int, _StubNet]:
        return self._nets

    @property
    def copper_layers(self) -> list[_StubLayer]:
        return self._layers

    @property
    def vias(self) -> list[object]:
        return self._vias

    @property
    def footprints(self) -> list[_StubFootprint]:
        return self._footprints

    def segments_on_layer(self, layer: str):
        for seg in self._segments:
            if seg.layer == layer:
                yield seg


def _design_rules(min_clearance_mm: float = 0.127) -> DesignRules:
    """Build a real DesignRules with a configurable inter-pair clearance.

    Default 0.127 mm matches the JLCPCB 4-layer min for the regression
    scenarios in the issue's test plan.
    """
    return DesignRules(
        min_trace_width_mm=0.1,
        min_clearance_mm=min_clearance_mm,
        min_via_drill_mm=0.3,
        min_via_diameter_mm=0.6,
        min_annular_ring_mm=0.075,
    )


def _parallel_segments(
    *,
    net_a: int,
    name_a: str,
    net_b: int,
    name_b: str,
    gap_mm: float,
    width_mm: float = 0.2,
) -> list[_StubSegment]:
    """Build two parallel horizontal segments with the requested edge gap.

    Both segments are 2 mm long along the X axis, centered at y=0 and
    y=(width_mm + gap_mm), so the edge-to-edge separation is exactly
    ``gap_mm``.
    """
    y_b = (width_mm / 2) + gap_mm + (width_mm / 2)
    return [
        _StubSegment(
            start=(0.0, 0.0),
            end=(2.0, 0.0),
            width=width_mm,
            net_number=net_a,
            net_name=name_a,
            uuid=f"seg-{name_a}",
        ),
        _StubSegment(
            start=(0.0, y_b),
            end=(2.0, y_b),
            width=width_mm,
            net_number=net_b,
            net_name=name_b,
            uuid=f"seg-{name_b}",
        ),
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDiffPairClearanceIntraRule:
    """Tests for DiffPairClearanceIntraRule.check()."""

    def test_no_diffpair_no_violation(self):
        """Two unrelated signal nets -- diff-pair detection returns empty."""
        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                1: _StubNet(1, "DATA"),
                2: _StubNet(2, "CLK"),
            },
            _segments=_parallel_segments(
                net_a=1, name_a="DATA", net_b=2, name_b="CLK", gap_mm=0.05
            ),
        )
        rule = DiffPairClearanceIntraRule(intra_pair_clearance_map={(1, 2): 0.075})
        results = rule.check(pcb, _design_rules())
        # Even though the explicit map names them, suffix detection does
        # not see them as a pair, so the rule must not fire.
        assert len(results.violations) == 0

    def test_diffpair_within_intra_threshold(self):
        """Within-pair gap >= intra_pair_clearance: NO violation.

        This is the structural correctness case: the gap is tighter than
        the manufacturer's ``min_clearance_mm`` (0.127 mm), but >= the
        diff-pair's ``intra_pair_clearance`` (0.075 mm).  Allowed.
        """
        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                3: _StubNet(3, "USB_D+"),
                4: _StubNet(4, "USB_D-"),
            },
            _segments=_parallel_segments(
                net_a=3, name_a="USB_D+", net_b=4, name_b="USB_D-", gap_mm=0.090
            ),
        )
        rule = DiffPairClearanceIntraRule(intra_pair_clearance_map={(3, 4): 0.075})
        results = rule.check(pcb, _design_rules(min_clearance_mm=0.127))
        assert len(results.violations) == 0

    def test_diffpair_below_intra_threshold(self):
        """Within-pair gap < intra_pair_clearance: ONE violation."""
        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                3: _StubNet(3, "USB_D+"),
                4: _StubNet(4, "USB_D-"),
            },
            _segments=_parallel_segments(
                net_a=3, name_a="USB_D+", net_b=4, name_b="USB_D-", gap_mm=0.060
            ),
        )
        rule = DiffPairClearanceIntraRule(intra_pair_clearance_map={(3, 4): 0.075})
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 1

        v = results.violations[0]
        assert v.rule_id == "diffpair_clearance_intra"
        assert v.severity == "error"
        # Both nets in the violation.
        assert "USB_D+" in v.nets
        assert "USB_D-" in v.nets
        # Both nets in the message.
        assert "USB_D+" in v.message
        assert "USB_D-" in v.message
        # Gap and threshold present in the message.
        assert "0.060" in v.message or "0.06" in v.message
        assert "0.075" in v.message
        # Numeric fields populated.
        assert v.required_value == 0.075
        assert v.actual_value is not None
        assert abs(v.actual_value - 0.060) < 1e-3

    def test_clearance_rule_skips_same_pair(self):
        """Cross-rule: ClearanceRule must NOT fire on a same-pair edge.

        Without the same-pair skip in ClearanceRule._check_layer the user
        would see a clearance_segment_segment violation AND the new rule
        either fires or stays silent depending on the threshold -- which
        defeats the whole point of the diff-pair rule.

        Setup: gap = 0.090, inter-pair clearance = 0.127 -> would normally
        fire ClearanceRule.  But the two segments are USB_D+/USB_D-, a
        suffix-detected diff pair, so the skip kicks in.
        """
        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                3: _StubNet(3, "USB_D+"),
                4: _StubNet(4, "USB_D-"),
            },
            _segments=_parallel_segments(
                net_a=3, name_a="USB_D+", net_b=4, name_b="USB_D-", gap_mm=0.090
            ),
        )
        # Both rules see a 0.090 mm gap with min_clearance_mm = 0.127.
        rules = _design_rules(min_clearance_mm=0.127)

        # New rule: gap 0.090 >= intra threshold 0.075 -> no violation.
        diff_rule = DiffPairClearanceIntraRule(intra_pair_clearance_map={(3, 4): 0.075})
        diff_results = diff_rule.check(pcb, rules)
        assert len(diff_results.violations) == 0

        # Existing clearance rule: would normally fire (0.090 < 0.127),
        # but the same-pair skip suppresses it.
        clearance_rule = ClearanceRule()
        clearance_results = clearance_rule.check(pcb, rules)
        assert len(clearance_results.violations) == 0, (
            "ClearanceRule should skip same-pair segment edges when both "
            "segments are halves of a detected diff pair."
        )

    def test_single_ended_pair_excluded(self):
        """USB_CC1 / USB_CC2 are NOT a diff pair (per #2558 refusal list)."""
        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                5: _StubNet(5, "USB_CC1"),
                6: _StubNet(6, "USB_CC2"),
            },
            _segments=_parallel_segments(
                net_a=5, name_a="USB_CC1", net_b=6, name_b="USB_CC2", gap_mm=0.060
            ),
        )
        # Even with an explicit intra map entry, suffix detection refuses
        # CC1/CC2 -- they're not in the diff-pair set.
        rule = DiffPairClearanceIntraRule(intra_pair_clearance_map={(5, 6): 0.075})
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 0

    def test_segment_vs_pad_out_of_scope(self):
        """Segment-vs-pad edges are out of scope -- the new rule is segments-only."""
        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                3: _StubNet(3, "USB_D+"),
                4: _StubNet(4, "USB_D-"),
            },
            _segments=[
                _StubSegment(
                    start=(0.0, 0.0),
                    end=(2.0, 0.0),
                    width=0.2,
                    net_number=3,
                    net_name="USB_D+",
                    uuid="seg-usbdp",
                ),
            ],
            _footprints=[
                _StubFootprint(
                    reference="J1",
                    pads=[
                        _StubPad(
                            number="1",
                            net_number=4,
                            net_name="USB_D-",
                            position=(1.0, 0.16),
                            size=(0.8, 0.2),
                            layers=["F.Cu"],
                        ),
                    ],
                ),
            ],
        )
        rule = DiffPairClearanceIntraRule(
            intra_pair_clearance_map={(3, 4): 0.5}  # Aggressive, would fire if scoped wide.
        )
        results = rule.check(pcb, _design_rules())
        # No violation -- pads are intentionally not iterated.
        assert len(results.violations) == 0

    def test_to_dict_resolves_violation_type(self):
        """The fuzzy-fallback miscategorization trap (Issue #2521 / #2560).

        ``"diffpair_clearance_intra"`` contains the substring
        ``"clearance"``.  Without the alias entry in
        ``drc/violation.py::ViolationType._ALIASES`` the keyword fuzzy
        fallback would silently match generic ``CLEARANCE`` -- not
        ``UNKNOWN`` like the single-pad-net case.  This test asserts the
        EXACT type string round-trips, which is the only defense.
        """
        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                3: _StubNet(3, "USB_D+"),
                4: _StubNet(4, "USB_D-"),
            },
            _segments=_parallel_segments(
                net_a=3, name_a="USB_D+", net_b=4, name_b="USB_D-", gap_mm=0.060
            ),
        )
        rule = DiffPairClearanceIntraRule(intra_pair_clearance_map={(3, 4): 0.075})
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 1

        d = results.violations[0].to_dict()
        assert d["rule_id"] == "diffpair_clearance_intra"
        # The trap: substring "clearance" would match the fuzzy fallback
        # and resolve to "clearance" without the alias entry.  The test
        # asserts the EXACT new type string.
        assert d["type"] == "diffpair_clearance_intra", (
            "type field must round-trip to 'diffpair_clearance_intra' "
            "(alias entry in drc/violation.py is missing or wrong)"
        )
        assert d["severity"] == "error"

    def test_per_pair_threshold(self):
        """Two diff pairs on the same board, each with its own threshold.

        Pair A (USB_D+/USB_D-) at 0.090 mm with intra=0.075 -> NO fire.
        Pair B (HDMI_TX0_P/HDMI_TX0_N) at 0.090 mm with intra=0.100 -> FIRE.
        """
        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                10: _StubNet(10, "USB_D+"),
                11: _StubNet(11, "USB_D-"),
                12: _StubNet(12, "HDMI_TX0_P"),
                13: _StubNet(13, "HDMI_TX0_N"),
            },
            _segments=[
                # Pair A at y=0 / y=0.290 (gap 0.090, width 0.2 each)
                *_parallel_segments(
                    net_a=10, name_a="USB_D+", net_b=11, name_b="USB_D-", gap_mm=0.090
                ),
                # Pair B at y=10 / y=10.290 (offset by +10 in Y to keep
                # the two pairs spatially separate)
                _StubSegment(
                    start=(0.0, 10.0),
                    end=(2.0, 10.0),
                    width=0.2,
                    net_number=12,
                    net_name="HDMI_TX0_P",
                    uuid="seg-hdmip",
                ),
                _StubSegment(
                    start=(0.0, 10.290),
                    end=(2.0, 10.290),
                    width=0.2,
                    net_number=13,
                    net_name="HDMI_TX0_N",
                    uuid="seg-hdmin",
                ),
            ],
        )
        rule = DiffPairClearanceIntraRule(
            intra_pair_clearance_map={
                (10, 11): 0.075,  # USB: pair A passes
                (12, 13): 0.100,  # HDMI: pair B fails
            }
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 1
        v = results.violations[0]
        # Only the HDMI pair fires.
        assert "HDMI_TX0_P" in v.nets
        assert "HDMI_TX0_N" in v.nets
        assert "USB_D+" not in v.nets
        assert "USB_D-" not in v.nets

    def test_rules_checked_count(self):
        """rules_checked increments per layer (matches ClearanceRule)."""
        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                3: _StubNet(3, "USB_D+"),
                4: _StubNet(4, "USB_D-"),
            },
            _segments=[],
            _layers=[_StubLayer("F.Cu"), _StubLayer("B.Cu")],
        )
        rule = DiffPairClearanceIntraRule()
        results = rule.check(pcb, _design_rules())
        assert results.rules_checked == 2

    def test_passes_with_no_segments(self):
        """Empty PCB -> zero violations and no crashes."""
        pcb = _StubPCB(_nets={0: _StubNet(0, "")})
        rule = DiffPairClearanceIntraRule()
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 0
        assert results.rules_checked == 1


# ---------------------------------------------------------------------------
# Issue #4178: diff-pair clearance-skip false-exemption regression.
#
# ClearanceRule._build_diff_pair_set() previously exempted ANY suffix-inferred
# +/- pair from same-layer segment-segment clearance checking.  That silently
# skipped genuine short detection between current-sense Kelvin pairs
# (ISENSE_A+/ISENSE_A-) -- +/- named but NOT differential signals -- while
# kicad-cli, which has no such heuristic, would flag them.  The fix requires
# CORROBORATING evidence (geometric coupling or an explicit declaration)
# before exempting a pair, so a Kelvin pair with a real same-layer short is
# now flagged while a genuinely coupled diff pair stays exempt.
# ---------------------------------------------------------------------------


def _crossing_segments(
    *,
    net_a: int,
    name_a: str,
    net_b: int,
    name_b: str,
    width_mm: float = 0.2,
) -> list[_StubSegment]:
    """Two DIFFERENT-net segments that physically cross (a genuine short).

    The two segments intersect near (1, 1) at ~90 degrees, so they are NOT
    parallel and NOT coupled -- the pattern of a real ``tracks_crossing``
    short between two nets that route to separate destinations (a Kelvin
    sense pair), the opposite of an intentionally-coupled diff pair.
    """
    return [
        _StubSegment(
            start=(0.0, 0.0),
            end=(2.0, 2.0),
            width=width_mm,
            net_number=net_a,
            net_name=name_a,
            uuid=f"seg-{name_a}",
        ),
        _StubSegment(
            start=(0.0, 2.0),
            end=(2.0, 0.0),
            width=width_mm,
            net_number=net_b,
            net_name=name_b,
            uuid=f"seg-{name_b}",
        ),
    ]


class TestClearanceRuleDiffPairFalseExemption:
    """ClearanceRule must NOT exempt suffix-only +/- pairs (Issue #4178)."""

    def test_kelvin_sense_pair_short_is_flagged(self):
        """A crossing ISENSE_A+/ISENSE_A- short is a real short, not exempt.

        The two segments cross (clearance 0) and are NOT geometrically
        coupled, so the suffix-only diff-pair inference must not exempt
        them from ClearanceRule -- the short is flagged.
        """
        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                19: _StubNet(19, "/ISENSE_A+"),
                20: _StubNet(20, "/ISENSE_A-"),
            },
            _segments=_crossing_segments(
                net_a=19, name_a="/ISENSE_A+", net_b=20, name_b="/ISENSE_A-"
            ),
        )
        clearance_rule = ClearanceRule()
        results = clearance_rule.check(pcb, _design_rules(min_clearance_mm=0.127))
        assert len(results.violations) >= 1, (
            "A crossing Kelvin-sense (ISENSE_A+/ISENSE_A-) short must be "
            "flagged -- suffix-only +/- naming is not sufficient to exempt "
            "it from the generic clearance rule (Issue #4178)."
        )
        v = results.violations[0]
        assert v.rule_id.startswith("clearance")
        assert "/ISENSE_A+" in v.nets
        assert "/ISENSE_A-" in v.nets

    def test_kelvin_sense_pair_close_parallel_short_is_flagged(self):
        """A short parallel Kelvin run below the coupling-length floor fires.

        Two ISENSE segments that briefly parallel at a tight gap (0.05 mm)
        but only for 0.5 mm -- below the 1.0 mm coupling-length floor -- are
        NOT a coupled diff pair, so the sub-clearance gap is flagged.
        """
        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                21: _StubNet(21, "/ISENSE_B+"),
                22: _StubNet(22, "/ISENSE_B-"),
            },
            _segments=[
                _StubSegment(
                    start=(0.0, 0.0),
                    end=(0.5, 0.0),  # only 0.5 mm long -> below 1.0 mm floor
                    width=0.2,
                    net_number=21,
                    net_name="/ISENSE_B+",
                    uuid="seg-isb-p",
                ),
                _StubSegment(
                    start=(0.0, 0.25),  # gap = 0.05 mm (< 0.127 inter-clearance)
                    end=(0.5, 0.25),
                    width=0.2,
                    net_number=22,
                    net_name="/ISENSE_B-",
                    uuid="seg-isb-n",
                ),
            ],
        )
        clearance_rule = ClearanceRule()
        results = clearance_rule.check(pcb, _design_rules(min_clearance_mm=0.127))
        assert len(results.violations) >= 1, (
            "A short (below coupling-length floor) parallel Kelvin run at a "
            "sub-clearance gap must be flagged -- it is not a coupled diff "
            "pair (Issue #4178)."
        )

    def test_real_coupled_diffpair_stays_exempt(self):
        """A genuinely coupled USB_D+/USB_D- run remains exempt (no regression).

        The two segments run parallel and closely spaced for 2 mm (well
        above the coupling-length floor), so ClearanceRule still skips the
        same-pair edge and delegates to DiffPairClearanceIntraRule -- no
        false positive introduced for real differential pairs.
        """
        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                3: _StubNet(3, "USB_D+"),
                4: _StubNet(4, "USB_D-"),
            },
            _segments=_parallel_segments(
                net_a=3, name_a="USB_D+", net_b=4, name_b="USB_D-", gap_mm=0.090
            ),
        )
        clearance_rule = ClearanceRule()
        results = clearance_rule.check(pcb, _design_rules(min_clearance_mm=0.127))
        assert len(results.violations) == 0, (
            "A genuinely coupled diff pair (USB_D+/USB_D- parallel, tightly "
            "coupled) must stay exempt from the generic clearance rule "
            "(delegated to DiffPairClearanceIntraRule) -- Issue #4178 must "
            "not introduce a false positive for real diff pairs."
        )

    def test_declared_pair_stays_exempt_even_without_coupling(self):
        """An explicitly-declared pair is exempt even if not coupled geometry.

        When a pair is corroborated by an explicit declaration (net-class /
        router diffpair engagement, passed as ``declared_pairs``), it is
        exempt regardless of geometric coupling -- the declaration is
        itself the corroborating evidence.
        """
        from kicad_tools.validate.rules.clearance import _build_diff_pair_set

        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                7: _StubNet(7, "CLK_POS"),
                8: _StubNet(8, "CLK_NEG"),
            },
            # Crossing (uncoupled) geometry -- would NOT be corroborated by
            # coupling alone.
            _segments=_crossing_segments(net_a=7, name_a="CLK_POS", net_b=8, name_b="CLK_NEG"),
        )
        # Without a declaration and without coupling: NOT exempt.
        auto = _build_diff_pair_set(pcb)
        assert (7, 8) not in auto

        # With an explicit declaration: exempt.
        declared = _build_diff_pair_set(pcb, declared_pairs={(7, 8)})
        assert (7, 8) in declared
