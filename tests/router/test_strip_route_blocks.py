"""Tests for Issue #2976: ``_strip_route_blocks`` prevents same-net via overlap.

Background
----------

Running ``kct route`` on board 05 with the proven 2-layer recipe produced
14 ``hole_to_hole_clearance`` errors reading "-0.300mm < minimum
0.127mm" between two vias on the **same** logical net (e.g. HALL_A vs
HALL_A).  Negative clearance == two same-net drills literally on top of
each other -- a manufacturing fault that breaks the drill file.

Root cause: ``_stage_input_for_auto_pour`` aliases ``pcb_path`` to
``output_path`` so ``auto_pour`` doesn't mutate the user's input file.
After that aliasing, every subsequent ``_write_routed_pcb`` call re-reads
the previous write's output instead of the original input, and the new
route s-expression is *appended* on top of stale segments and vias from
the prior write.  Two checkpoint writes + one final write yields three
copies of every via in the output PCB.  The duplicates carry distinct
UUIDs but identical (net, x, y, drill) tuples, so the DRC validator sees
overlapping drills on the same net and flags ``hole_to_hole_clearance``.

Fix: ``_insert_sexp_before_closing`` now calls ``_strip_route_blocks``
before inserting the new route s-expression.  The strip removes any
top-level ``(segment ...)`` and ``(via ...)`` forms from the PCB content
so the inserted s-expression is the only source of routed elements.

These tests verify:

1. ``_strip_route_blocks`` removes top-level ``(segment ...)`` and
   ``(via ...)`` blocks and leaves footprints, zones, and other
   structure intact.
2. ``_insert_sexp_before_closing`` round-trips: applying it twice with
   the same route s-expression yields the same number of vias as
   applying it once.  This is the property that fails without the
   strip step.
3. A foreign-net via pair at the same coordinate is *not* deduplicated
   by the strip itself (the strip operates on text, not on net IDs);
   it only removes pre-existing routed elements that the new s-expression
   is about to replace.
"""

from __future__ import annotations

from kicad_tools.cli.route_cmd import (
    _insert_sexp_before_closing,
    _strip_route_blocks,
)

# A minimal-but-valid KiCad PCB content fragment.  Includes a footprint
# (which contains nested (pad ...) forms -- those must NOT be stripped),
# a (zone ...) block (must not be stripped), and a (segment ...) and
# (via ...) block (must be stripped).
PCB_HEADER = """(kicad_pcb
\t(version 20240108)
\t(generator "test")
\t(general
\t\t(thickness 1.6)
\t)
\t(footprint "Capacitor_SMD:C_0805"
\t\t(layer "F.Cu")
\t\t(at 10.0 10.0)
\t\t(pad "1" smd roundrect
\t\t\t(at -0.95 0.0)
\t\t\t(size 1.0 1.25)
\t\t\t(layers "F.Cu")
\t\t)
\t\t(pad "2" smd roundrect
\t\t\t(at 0.95 0.0)
\t\t\t(size 1.0 1.25)
\t\t\t(layers "F.Cu")
\t\t)
\t)
\t(zone
\t\t(net 1)
\t\t(net_name "GND")
\t\t(layer "F.Cu")
\t\t(polygon
\t\t\t(pts (xy 0 0) (xy 100 0) (xy 100 100) (xy 0 100))
\t\t)
\t)
\t(segment
\t\t(start 10.0 10.0)
\t\t(end 20.0 10.0)
\t\t(width 0.2)
\t\t(layer "F.Cu")
\t\t(uuid "aaa")
\t\t(net 2)
\t)
\t(via
\t\t(at 15.0 15.0)
\t\t(size 0.6)
\t\t(drill 0.3)
\t\t(layers "F.Cu" "B.Cu")
\t\t(uuid "bbb")
\t\t(net 2)
\t)
)
"""


# Route s-expression that would be inserted into the PCB.  Same coords
# as the stale via in PCB_HEADER -- this is the realistic case: the
# router re-emits the same via on a re-route, and without the strip
# both copies survive in the output.
NEW_ROUTE_SEXP = """\t(segment
\t\t(start 10.0 10.0)
\t\t(end 20.0 10.0)
\t\t(width 0.2)
\t\t(layer "F.Cu")
\t\t(uuid "ccc")
\t\t(net 2)
\t)
\t(via
\t\t(at 15.0 15.0)
\t\t(size 0.6)
\t\t(drill 0.3)
\t\t(layers "F.Cu" "B.Cu")
\t\t(uuid "ddd")
\t\t(net 2)
\t)"""


class TestStripRouteBlocks:
    """Verify the strip helper removes only top-level segments/vias."""

    def test_strips_top_level_segment_and_via(self):
        stripped, segs, vias = _strip_route_blocks(PCB_HEADER)
        # The original had one segment and one via at the top level.
        assert segs == 1
        assert vias == 1
        assert "(segment" not in stripped
        # Footprint pads contain "(at" but never "(via"; double-check
        # we haven't accidentally stripped pads.
        assert "(via" not in stripped
        # Footprint and zone must survive.
        assert "(footprint" in stripped
        assert "(zone" in stripped
        # Pads inside the footprint must survive too.
        assert '(pad "1"' in stripped
        assert '(pad "2"' in stripped

    def test_leaves_clean_pcb_untouched(self):
        clean = """(kicad_pcb (version 20240108) (footprint "X" (at 0 0) (pad "1" smd (at 0 0))))"""
        stripped, segs, vias = _strip_route_blocks(clean)
        assert segs == 0
        assert vias == 0
        # The clean PCB is unchanged.
        assert stripped == clean

    def test_handles_quoted_parens(self):
        # Quoted strings containing "(" or ")" must not affect depth.
        content = """(kicad_pcb
\t(layer "Some (weird) Layer")
\t(segment (start 0 0) (end 1 1) (width 0.1) (net 1))
)"""
        stripped, segs, vias = _strip_route_blocks(content)
        assert segs == 1
        assert vias == 0
        assert "Some (weird) Layer" in stripped


class TestInsertSexpBeforeClosing:
    """Verify the insert + strip composition is idempotent."""

    def test_no_via_accumulation_on_repeat_writes(self):
        """Reproduces issue #2976: writing twice must not duplicate vias.

        Before the fix, the second insert would re-read the first write
        (after ``_stage_input_for_auto_pour`` aliased pcb_path == output_path)
        and append NEW_ROUTE_SEXP on top of the existing route content,
        yielding 2 vias for what should be 1.
        """
        # First write: clean PCB header (with stale 1 via + 1 seg).
        first_write = _insert_sexp_before_closing(PCB_HEADER, NEW_ROUTE_SEXP)
        first_via_count = first_write.count("(via\n")
        first_seg_count = first_write.count("(segment\n")
        assert first_via_count == 1, f"Expected 1 via in first write, got {first_via_count}"
        assert first_seg_count == 1, f"Expected 1 segment in first write, got {first_seg_count}"

        # Second write: read first write's output, insert same sexp.
        # This is the alias-pcb_path case from #2976.
        second_write = _insert_sexp_before_closing(first_write, NEW_ROUTE_SEXP)
        second_via_count = second_write.count("(via\n")
        second_seg_count = second_write.count("(segment\n")
        assert second_via_count == 1, (
            f"Issue #2976 regression: second write produced {second_via_count} vias "
            "instead of 1 -- the stale via from the first write was not stripped"
        )
        assert second_seg_count == 1, (
            f"Issue #2976 regression: second write produced {second_seg_count} segments"
        )

    def test_third_write_still_one_via(self):
        """Three writes must still produce one via (the same coord)."""
        c1 = _insert_sexp_before_closing(PCB_HEADER, NEW_ROUTE_SEXP)
        c2 = _insert_sexp_before_closing(c1, NEW_ROUTE_SEXP)
        c3 = _insert_sexp_before_closing(c2, NEW_ROUTE_SEXP)
        assert c3.count("(via\n") == 1
        assert c3.count("(segment\n") == 1

    def test_footprints_and_zones_preserved_after_insert(self):
        out = _insert_sexp_before_closing(PCB_HEADER, NEW_ROUTE_SEXP)
        assert "(footprint" in out
        assert "(zone" in out
        assert '(pad "1"' in out
        assert '(pad "2"' in out

    def test_new_sexp_present_after_insert(self):
        out = _insert_sexp_before_closing(PCB_HEADER, NEW_ROUTE_SEXP)
        # The new UUIDs must be present; the stale UUIDs must be gone.
        assert "ccc" in out, "New segment UUID missing"
        assert "ddd" in out, "New via UUID missing"
        assert "aaa" not in out, "Stale segment UUID survived"
        assert "bbb" not in out, "Stale via UUID survived"

    def test_empty_route_sexp_strips_stale_content(self):
        """Writing with an empty route_sexp still strips stale routes."""
        out = _insert_sexp_before_closing(PCB_HEADER, "")
        assert "(via" not in out
        assert "(segment" not in out
        # But footprints and zones survive.
        assert "(footprint" in out
        assert "(zone" in out


class TestForeignNetViasNotMerged:
    """Confirm the strip does not erase the new sexp's own vias.

    This test protects against an over-aggressive strip that would remove
    everything matching ``(via`` -- if the strip ran on the inserted sexp
    instead of the existing content, two foreign-net vias inserted at
    nearby coordinates would silently lose one.  The strip only operates
    on ``pcb_content`` BEFORE insertion, so both inserted vias must
    survive.
    """

    def test_two_foreign_net_vias_both_present(self):
        # No stale content.
        clean_pcb = """(kicad_pcb
\t(version 20240108)
\t(footprint "X" (at 0 0) (pad "1" smd (at 0 0)))
)"""
        two_via_sexp = """\t(via
\t\t(at 10.0 10.0)
\t\t(size 0.6)
\t\t(drill 0.3)
\t\t(layers "F.Cu" "B.Cu")
\t\t(uuid "eee")
\t\t(net 1)
\t)
\t(via
\t\t(at 10.1 10.0)
\t\t(size 0.6)
\t\t(drill 0.3)
\t\t(layers "F.Cu" "B.Cu")
\t\t(uuid "fff")
\t\t(net 2)
\t)"""
        out = _insert_sexp_before_closing(clean_pcb, two_via_sexp)
        # Both foreign-net vias must survive the insert.
        assert out.count("(via\n") == 2
        assert "eee" in out
        assert "fff" in out
