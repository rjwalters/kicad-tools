"""Regression tests for via-barrel clearance on every spanned copper layer.

Issue #3487: ``clearance_segment_via`` only compared vias against
segments on layers explicitly listed in the via's ``(layers ...)``
node.  KiCad declares only the endpoint pair (a through-via on a
4-layer board reads ``(layers "F.Cu" "B.Cu")``), so barrel-vs-segment
conflicts on inner layers were silently skipped -- the softstart rev B
board shipped 3 real cross-net barrel shorts that ``kct check`` could
not see (found by PR #3481's step-10d geometric sweep).

These tests verify:

1.  **Through-via vs inner-layer segment**: violations now fire on
    In1.Cu / In2.Cu (the primary bug case).
2.  **Endpoint layers unchanged**: F.Cu / B.Cu behavior is identical
    to before (regression guard).
3.  **Blind/micro vias**: the barrel only spans its declared range --
    a segment on a layer OUTSIDE the range must not be flagged.
4.  **Same-net and far-away segments**: no over-reporting.
5.  **Non-segment pairs**: via-via / pad-via pair counts are unchanged
    (no duplicate reports on spanned inner layers).
6.  **Co-location skip (#2706)**: still applies on inner layers.
7.  **`via_spans_layer` helper**: unit coverage of the shared
    predicate in ``kicad_tools.core.layers``.
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.core.layers import COPPER_LAYER_ORDER, via_spans_layer

# ---------------------------------------------------------------------------
# Synthetic 4-layer PCB fixtures
# ---------------------------------------------------------------------------

_HEADER_4L = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (4 "In1.Cu" signal)
    (6 "In2.Cu" signal)
    (2 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG1")
  (net 2 "SIG2")
"""


def _pcb_via_vs_segment(layer: str, seg_x: float, via_sexp: str | None = None) -> str:
    """4-layer board: one net-1 via at (100, 100), one net-2 vertical segment.

    The segment runs x=``seg_x``, y=99..101 on ``layer``.  With via size
    0.6 (radius 0.3) and trace width 0.2 (half-width 0.1), the
    edge-to-edge clearance is ``|seg_x - 100| - 0.4`` -- e.g. seg_x =
    100.3 gives -0.1 mm (hard overlap), seg_x = 101 gives +0.6 mm (clean).
    """
    via = via_sexp or (
        "(via (at 100 100) (size 0.6) (drill 0.3)"
        ' (layers "F.Cu" "B.Cu") (net 1 "SIG1") (uuid "via-1"))'
    )
    return (
        _HEADER_4L
        + f"  {via}\n"
        + f"  (segment (start {seg_x} 99) (end {seg_x} 101) (width 0.2)"
        + f' (layer "{layer}") (net 2 "SIG2") (uuid "seg-1"))\n'
        + ")\n"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _check_clearances(pcb_content: str, tmp_path: Path):
    """Write a PCB fixture and return the clearance check results."""
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate import DRCChecker

    pcb_path = tmp_path / "test.kicad_pcb"
    pcb_path.write_text(pcb_content)
    pcb = PCB.load(pcb_path)

    checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=4, copper_oz=1.0)
    return checker.check_clearances()


def _segment_via_violations(results) -> list:
    return [v for v in results.violations if v.rule_id == "clearance_segment_via"]


def _via_via_violations(results) -> list:
    return [v for v in results.violations if v.rule_id == "clearance_via_via"]


# ---------------------------------------------------------------------------
# Tests: the shared via_spans_layer predicate
# ---------------------------------------------------------------------------


class TestViaSpansLayerHelper:
    """Unit coverage for kicad_tools.core.layers.via_spans_layer."""

    def test_through_via_spans_inner_layers(self):
        layers = ["F.Cu", "B.Cu"]
        assert via_spans_layer(layers, "F.Cu")
        assert via_spans_layer(layers, "In1.Cu")
        assert via_spans_layer(layers, "In2.Cu")
        assert via_spans_layer(layers, "In30.Cu")
        assert via_spans_layer(layers, "B.Cu")

    def test_blind_via_spans_only_declared_range(self):
        layers = ["F.Cu", "In1.Cu"]
        assert via_spans_layer(layers, "F.Cu")
        assert via_spans_layer(layers, "In1.Cu")
        assert not via_spans_layer(layers, "In2.Cu")
        assert not via_spans_layer(layers, "B.Cu")

    def test_buried_via_spans_inner_range(self):
        layers = ["In1.Cu", "In3.Cu"]
        assert not via_spans_layer(layers, "F.Cu")
        assert via_spans_layer(layers, "In1.Cu")
        assert via_spans_layer(layers, "In2.Cu")
        assert via_spans_layer(layers, "In3.Cu")
        assert not via_spans_layer(layers, "In4.Cu")
        assert not via_spans_layer(layers, "B.Cu")

    def test_layer_order_is_reversal_agnostic(self):
        """Span is min..max regardless of declaration order."""
        assert via_spans_layer(["B.Cu", "F.Cu"], "In2.Cu")

    def test_unknown_layers_are_rejected(self):
        assert not via_spans_layer(["F.Cu", "B.Cu"], "Edge.Cuts")
        assert not via_spans_layer(["Edge.Cuts"], "In1.Cu")
        assert not via_spans_layer([], "F.Cu")

    def test_explicit_listing_always_matches(self):
        # Single-entry layer lists still match the listed layer.
        assert via_spans_layer(["In1.Cu"], "In1.Cu")
        assert not via_spans_layer(["In1.Cu"], "In2.Cu")

    def test_canonical_order_shape(self):
        assert COPPER_LAYER_ORDER[0] == "F.Cu"
        assert COPPER_LAYER_ORDER[-1] == "B.Cu"
        assert len(COPPER_LAYER_ORDER) == 32  # F.Cu + In1..In30 + B.Cu

    def test_net_status_delegates_to_shared_helper(self):
        """The NetStatusAnalyzer class attribute mirrors the shared order."""
        from kicad_tools.analysis.net_status import NetStatusAnalyzer

        assert list(COPPER_LAYER_ORDER) == NetStatusAnalyzer._COPPER_LAYER_ORDER


# ---------------------------------------------------------------------------
# Tests: through-via barrel vs inner-layer segments (the #3487 bug)
# ---------------------------------------------------------------------------


class TestThroughViaInnerLayerSegments:
    """Through-via barrels must be checked against inner-layer segments."""

    def test_in1_overlap_fires(self, tmp_path: Path):
        """Barrel-on-trace on In1.Cu (the softstart hard-short case)."""
        results = _check_clearances(_pcb_via_vs_segment("In1.Cu", 100.3), tmp_path)
        viols = _segment_via_violations(results)
        assert len(viols) == 1, (
            "Through-via barrel overlapping a cross-net In1.Cu segment "
            "must produce a clearance_segment_via violation (Issue #3487 "
            "blind spot)."
        )
        assert viols[0].actual_value < 0  # hard overlap
        assert viols[0].layer == "In1.Cu"

    def test_in2_near_miss_fires(self, tmp_path: Path):
        """Sub-minimum (but positive) clearance on In2.Cu fires too."""
        # seg_x = 100.45 -> edge clearance 0.05 mm < 0.127 mm minimum.
        results = _check_clearances(_pcb_via_vs_segment("In2.Cu", 100.45), tmp_path)
        viols = _segment_via_violations(results)
        assert len(viols) == 1
        assert 0 < viols[0].actual_value < 0.127
        assert viols[0].layer == "In2.Cu"

    def test_bottom_layer_still_fires(self, tmp_path: Path):
        """Regression: B.Cu (explicitly listed) behavior unchanged."""
        results = _check_clearances(_pcb_via_vs_segment("B.Cu", 100.3), tmp_path)
        assert len(_segment_via_violations(results)) == 1

    def test_front_layer_still_fires(self, tmp_path: Path):
        """Regression: F.Cu (explicitly listed) behavior unchanged."""
        results = _check_clearances(_pcb_via_vs_segment("F.Cu", 100.3), tmp_path)
        assert len(_segment_via_violations(results)) == 1

    def test_inner_segment_with_clean_clearance_passes(self, tmp_path: Path):
        """An inner-layer segment well clear of the barrel is not flagged."""
        results = _check_clearances(_pcb_via_vs_segment("In1.Cu", 101.0), tmp_path)
        assert len(_segment_via_violations(results)) == 0

    def test_same_net_inner_segment_not_flagged(self, tmp_path: Path):
        """Same-net copper may touch -- including on inner layers."""
        pcb = (
            _HEADER_4L + "  (via (at 100 100) (size 0.6) (drill 0.3)"
            ' (layers "F.Cu" "B.Cu") (net 1 "SIG1") (uuid "via-1"))\n'
            "  (segment (start 100.3 99) (end 100.3 101) (width 0.2)"
            ' (layer "In1.Cu") (net 1 "SIG1") (uuid "seg-1"))\n'
            ")\n"
        )
        results = _check_clearances(pcb, tmp_path)
        assert len(_segment_via_violations(results)) == 0

    def test_colocation_skip_applies_on_inner_layers(self, tmp_path: Path):
        """The #2706 endpoint-at-via-center skip also applies on In1.Cu.

        The router's in-pad escape invariant places inner-layer stub
        endpoints exactly at via centers; the cross-net co-location
        suppression must keep working now that inner layers are scanned.
        """
        pcb = (
            _HEADER_4L + "  (via (at 100 100) (size 0.6) (drill 0.3)"
            ' (layers "F.Cu" "B.Cu") (net 1 "SIG1") (uuid "via-1"))\n'
            "  (segment (start 100 100) (end 101 100) (width 0.2)"
            ' (layer "In1.Cu") (net 2 "SIG2") (uuid "seg-1"))\n'
            ")\n"
        )
        results = _check_clearances(pcb, tmp_path)
        assert len(_segment_via_violations(results)) == 0


# ---------------------------------------------------------------------------
# Tests: blind / micro vias only span their declared range
# ---------------------------------------------------------------------------


class TestPartialSpanVias:
    """Blind/micro vias must NOT be flagged against layers outside the span."""

    def test_micro_via_does_not_reach_in2(self, tmp_path: Path):
        via = (
            "(via micro (at 100 100) (size 0.6) (drill 0.3)"
            ' (layers "F.Cu" "In1.Cu") (net 1 "SIG1") (uuid "via-1"))'
        )
        results = _check_clearances(_pcb_via_vs_segment("In2.Cu", 100.3, via_sexp=via), tmp_path)
        assert len(_segment_via_violations(results)) == 0, (
            "A micro via F.Cu->In1.Cu has no barrel copper on In2.Cu; "
            "flagging it would be a false positive."
        )

    def test_micro_via_does_not_reach_bottom(self, tmp_path: Path):
        via = (
            "(via micro (at 100 100) (size 0.6) (drill 0.3)"
            ' (layers "F.Cu" "In1.Cu") (net 1 "SIG1") (uuid "via-1"))'
        )
        results = _check_clearances(_pcb_via_vs_segment("B.Cu", 100.3, via_sexp=via), tmp_path)
        assert len(_segment_via_violations(results)) == 0

    def test_micro_via_fires_inside_span(self, tmp_path: Path):
        via = (
            "(via micro (at 100 100) (size 0.6) (drill 0.3)"
            ' (layers "F.Cu" "In1.Cu") (net 1 "SIG1") (uuid "via-1"))'
        )
        results = _check_clearances(_pcb_via_vs_segment("In1.Cu", 100.3, via_sexp=via), tmp_path)
        assert len(_segment_via_violations(results)) == 1

    def test_buried_via_fires_on_intermediate_layer(self, tmp_path: Path):
        """A buried In1->B.Cu via spans In2.Cu even though not listed."""
        via = (
            "(via buried (at 100 100) (size 0.6) (drill 0.3)"
            ' (layers "In1.Cu" "B.Cu") (net 1 "SIG1") (uuid "via-1"))'
        )
        results = _check_clearances(_pcb_via_vs_segment("In2.Cu", 100.3, via_sexp=via), tmp_path)
        assert len(_segment_via_violations(results)) == 1

    def test_buried_via_does_not_reach_front(self, tmp_path: Path):
        via = (
            "(via buried (at 100 100) (size 0.6) (drill 0.3)"
            ' (layers "In1.Cu" "B.Cu") (net 1 "SIG1") (uuid "via-1"))'
        )
        results = _check_clearances(_pcb_via_vs_segment("F.Cu", 100.3, via_sexp=via), tmp_path)
        assert len(_segment_via_violations(results)) == 0


# ---------------------------------------------------------------------------
# Tests: non-segment pair counts are unchanged
# ---------------------------------------------------------------------------


class TestNonSegmentPairCountsUnchanged:
    """Via-via (and pad-via) pairs must not be duplicated on inner layers.

    Two overlapping cross-net through-vias on a 4-layer board were
    historically reported once per explicitly-declared layer (F.Cu +
    B.Cu = 2 reports).  Collecting barrels on spanned inner layers must
    NOT inflate that to 4 -- the pair's geometry is layer-independent
    and the endpoint-layer scans already cover it.
    """

    def test_via_via_overlap_reported_twice_not_four_times(self, tmp_path: Path):
        pcb = (
            _HEADER_4L + "  (via (at 100 100) (size 0.6) (drill 0.3)"
            ' (layers "F.Cu" "B.Cu") (net 1 "SIG1") (uuid "via-1"))\n'
            "  (via (at 100.5 100) (size 0.6) (drill 0.3)"
            ' (layers "F.Cu" "B.Cu") (net 2 "SIG2") (uuid "via-2"))\n'
            ")\n"
        )
        results = _check_clearances(pcb, tmp_path)
        viols = _via_via_violations(results)
        assert len(viols) == 2, (
            f"Expected the pre-#3487 count of 2 via_via reports (one per "
            f"declared endpoint layer), got {len(viols)}: "
            f"{[v.layer for v in viols]}"
        )
        assert sorted(v.layer for v in viols) == ["B.Cu", "F.Cu"]
