"""Unit + CLI tests for :class:`ConnectivityRule` (Issue #3041).

The rule fires when a multi-pad net is not fully routed -- the original
gap was that ``kct check`` reported ``DRC PASS`` on partial-route PCBs
because no rule cross-referenced the netlist against actual copper
connectivity.

Test strategy
-------------

We exercise the rule against synthetic on-disk PCB fixtures because:

* ``NetStatusAnalyzer`` consumes the loaded ``PCB`` object directly,
  so an in-memory ``PCB.create()`` would work for the unit cases.  We
  prefer on-disk fixtures here so each scenario doubles as an
  end-to-end CLI smoke test (the ``kct check`` dispatcher path).
* The fixtures are minimal-but-real KiCad S-expression sources.  They
  declare nets, footprints with pads referencing those nets, and
  (where relevant) segments / zones to control the connectivity
  outcome.  This is the same approach used by
  ``tests/test_cli_check_single_pad_net.py``.

Scenarios
---------

1. **2-pad net with no segments -> 1 error.**  The simplest possible
   case: a signal net that connects exactly two pads, with no copper
   between them.  Must produce a single ``connectivity`` error.
2. **3-pad net with 1 of 3 pads connected -> 1 error.**  A multi-pad
   net where one segment connects two of three pads, leaving one
   stranded.  Must produce a single ``incomplete``-flavored error.
3. **GND pour-net WITH copper zone covering pads -> 0 errors.**
   Validates that pour-net suppression works: ``NetStatusAnalyzer``
   recognises pads inside a same-net filled polygon as connected, so
   the connectivity rule sees ``status == "complete"`` and skips.
4. **GND pour-named net WITHOUT zone -> 1 error.**  A net named ``GND``
   but with no copper zone and no traces is still structurally
   disconnected.  The rule does NOT special-case pour names; the
   zone-based suppression is connectivity-driven, not name-driven.
5. **Fully-routed 2-pad net -> 0 errors.**  Sanity check that the rule
   does NOT fire on a happy-path board.
"""

from __future__ import annotations

import json
from pathlib import Path

# Minimal PCB skeleton.  Footprints / nets / segments / zones are
# injected by the per-test builders below.  The layer block must match
# what NetStatusAnalyzer expects (``F.Cu`` / ``B.Cu`` plus mask/silk).
_PCB_HEADER = """(kicad_pcb (version 20240108) (generator "test_fixture")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (32 "B.Adhes" user "B.Adhesive")
    (33 "F.Adhes" user "F.Adhesive")
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (40 "Dwgs.User" user "User.Drawings")
    (41 "Cmts.User" user "User.Comments")
    (42 "Eco1.User" user "User.Eco1")
    (43 "Eco2.User" user "User.Eco2")
    (44 "Edge.Cuts" user)
    (45 "Margin" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user)
    (49 "F.Fab" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
"""


def _two_pad_unrouted_pcb(net_name: str = "VIN") -> str:
    """Two pads on the same named net, no segments between them.

    Models a board where the schematic asserts a two-pad signal net
    (e.g. ``VIN``) but the router never connected the pads.  This is
    the simplest scenario the connectivity rule must catch.
    """
    return (
        _PCB_HEADER
        + f"""
  (net 0 "")
  (net 1 "{net_name}")
  (footprint "Resistor_SMD:R_0805" (layer "F.Cu")
    (at 100 100)
    (property "Reference" "R1" (at 0 -2 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000001"))
    (property "Value" "10k" (at 0 2 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000002"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "{net_name}") (uuid "00000000-0000-0000-0000-000000000003"))
  )
  (footprint "Resistor_SMD:R_0805" (layer "F.Cu")
    (at 110 100)
    (property "Reference" "R2" (at 0 -2 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000004"))
    (property "Value" "10k" (at 0 2 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000005"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "{net_name}") (uuid "00000000-0000-0000-0000-000000000006"))
  )
)
"""
    )


def _three_pad_partial_pcb() -> str:
    """Three pads on net ``SIG``, with a single segment connecting two of them.

    Pads land at world coordinates (99, 100), (109, 100), (119, 100).
    A trace runs from (99, 100) -> (109, 100), so R1.1 and R2.1 are on
    the routed island, R3.1 is stranded.  The rule must report one
    ``incomplete`` error.
    """
    return (
        _PCB_HEADER
        + """
  (net 0 "")
  (net 1 "SIG")
  (footprint "Resistor_SMD:R_0805" (layer "F.Cu")
    (at 100 100)
    (property "Reference" "R1" (at 0 -2 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000001"))
    (property "Value" "10k" (at 0 2 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000002"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SIG") (uuid "00000000-0000-0000-0000-000000000003"))
  )
  (footprint "Resistor_SMD:R_0805" (layer "F.Cu")
    (at 110 100)
    (property "Reference" "R2" (at 0 -2 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000004"))
    (property "Value" "10k" (at 0 2 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000005"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SIG") (uuid "00000000-0000-0000-0000-000000000006"))
  )
  (footprint "Resistor_SMD:R_0805" (layer "F.Cu")
    (at 120 100)
    (property "Reference" "R3" (at 0 -2 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000007"))
    (property "Value" "10k" (at 0 2 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000008"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SIG") (uuid "00000000-0000-0000-0000-000000000009"))
  )
  (segment (start 99 100) (end 109 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "00000000-0000-0000-0000-00000000000a"))
)
"""
    )


def _three_pad_pour_with_zone_pcb() -> str:
    """Three GND pads with a filled F.Cu zone covering them.

    A multi-pad ``GND`` net with a copper zone that has filled polygons
    covering every pad position is fully connected (each pad is inside
    the same-net filled polygon).  The rule must NOT fire.

    The zone's filled polygon is a large rectangle that covers all
    three pad positions (98..121, 99..101).
    """
    return (
        _PCB_HEADER
        + """
  (net 0 "")
  (net 1 "GND")
  (footprint "Resistor_SMD:R_0805" (layer "F.Cu")
    (at 100 100)
    (property "Reference" "R1" (at 0 -2 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000001"))
    (property "Value" "10k" (at 0 2 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000002"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND") (uuid "00000000-0000-0000-0000-000000000003"))
  )
  (footprint "Resistor_SMD:R_0805" (layer "F.Cu")
    (at 110 100)
    (property "Reference" "R2" (at 0 -2 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000004"))
    (property "Value" "10k" (at 0 2 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000005"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND") (uuid "00000000-0000-0000-0000-000000000006"))
  )
  (footprint "Resistor_SMD:R_0805" (layer "F.Cu")
    (at 120 100)
    (property "Reference" "R3" (at 0 -2 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000007"))
    (property "Value" "10k" (at 0 2 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000008"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND") (uuid "00000000-0000-0000-0000-000000000009"))
  )
  (zone (net 1) (net_name "GND") (layer "F.Cu") (uuid "00000000-0000-0000-0000-00000000000a") (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.25)
    (filled_areas_thickness no)
    (polygon (pts (xy 95 95) (xy 125 95) (xy 125 105) (xy 95 105)))
    (filled_polygon
      (layer "F.Cu")
      (pts (xy 95 95) (xy 125 95) (xy 125 105) (xy 95 105))
    )
  )
)
"""
    )


def _discontinuous_pour_pcb() -> str:
    """A GND pour whose fill reaches only one of its two pads.

    The zone boundary (95..125, 95..105) contains both GND pads, but the
    single filled polygon covers only the left island (95..105): R1.1 at
    (99, 100) lands on real fill copper while R2.1 at (119, 100) sits in the
    boundary-only gap with no copper reaching it.  Under the island-aware
    connectivity model (#3914) this net is genuinely ``incomplete`` -- R2.1 is
    stranded on the manufactured board.  Because the net owns real filled pour
    copper, the residual is advisory (a stitching gap, not a missing signal
    trace), so ``ConnectivityRule`` must NOT fire a hard error.  This is the
    boards 03/04/06 "GND N of M pads stranded" false positive.
    """
    return (
        _PCB_HEADER
        + """
  (net 0 "")
  (net 1 "GND")
  (footprint "Resistor_SMD:R_0805" (layer "F.Cu")
    (at 100 100)
    (property "Reference" "R1" (at 0 -2 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000001"))
    (property "Value" "10k" (at 0 2 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000002"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND") (uuid "00000000-0000-0000-0000-000000000003"))
  )
  (footprint "Resistor_SMD:R_0805" (layer "F.Cu")
    (at 120 100)
    (property "Reference" "R2" (at 0 -2 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000004"))
    (property "Value" "10k" (at 0 2 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000005"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND") (uuid "00000000-0000-0000-0000-000000000006"))
  )
  (zone (net 1) (net_name "GND") (layer "F.Cu") (uuid "00000000-0000-0000-0000-00000000000a") (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.25)
    (filled_areas_thickness no)
    (polygon (pts (xy 95 95) (xy 125 95) (xy 125 105) (xy 95 105)))
    (filled_polygon
      (layer "F.Cu")
      (pts (xy 95 95) (xy 105 95) (xy 105 105) (xy 95 105))
    )
  )
)
"""
    )


def _three_pad_pour_no_zone_pcb() -> str:
    """Three GND pads, no copper zone, no traces -- still an error.

    A net named ``GND`` is conventionally a pour net, but the rule
    does NOT special-case names.  Without an actual copper zone (or
    traces) connecting the pads, the net is structurally disconnected
    and must be flagged.
    """
    return (
        _PCB_HEADER
        + """
  (net 0 "")
  (net 1 "GND")
  (footprint "Resistor_SMD:R_0805" (layer "F.Cu")
    (at 100 100)
    (property "Reference" "R1" (at 0 -2 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000001"))
    (property "Value" "10k" (at 0 2 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000002"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND") (uuid "00000000-0000-0000-0000-000000000003"))
  )
  (footprint "Resistor_SMD:R_0805" (layer "F.Cu")
    (at 110 100)
    (property "Reference" "R2" (at 0 -2 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000004"))
    (property "Value" "10k" (at 0 2 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000005"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND") (uuid "00000000-0000-0000-0000-000000000006"))
  )
  (footprint "Resistor_SMD:R_0805" (layer "F.Cu")
    (at 120 100)
    (property "Reference" "R3" (at 0 -2 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000007"))
    (property "Value" "10k" (at 0 2 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000008"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND") (uuid "00000000-0000-0000-0000-000000000009"))
  )
)
"""
    )


def _two_pad_routed_pcb() -> str:
    """Two pads on net ``SIG`` connected by a trace -- no error expected."""
    return (
        _PCB_HEADER
        + """
  (net 0 "")
  (net 1 "SIG")
  (footprint "Resistor_SMD:R_0805" (layer "F.Cu")
    (at 100 100)
    (property "Reference" "R1" (at 0 -2 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000001"))
    (property "Value" "10k" (at 0 2 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000002"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SIG") (uuid "00000000-0000-0000-0000-000000000003"))
  )
  (footprint "Resistor_SMD:R_0805" (layer "F.Cu")
    (at 110 100)
    (property "Reference" "R2" (at 0 -2 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000004"))
    (property "Value" "10k" (at 0 2 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000005"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SIG") (uuid "00000000-0000-0000-0000-000000000006"))
  )
  (segment (start 99 100) (end 109 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "00000000-0000-0000-0000-000000000007"))
)
"""
    )


def _run_check_only_connectivity(pcb_path: Path) -> tuple[int, str]:
    """Drive ``kct check --only connectivity`` end-to-end and return (rc, stdout)."""
    from kicad_tools.cli.check_cmd import main

    rc = main([str(pcb_path), "--only", "connectivity"])
    return rc, ""  # capsys handled by individual tests


class TestConnectivityRuleUnit:
    """Direct invocation of :class:`ConnectivityRule.check`."""

    def test_two_pad_unrouted_fires_one_error(self, tmp_path: Path) -> None:
        from kicad_tools.manufacturers import get_profile
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.rules.connectivity import ConnectivityRule

        pcb_path = tmp_path / "two_pad_unrouted.kicad_pcb"
        pcb_path.write_text(_two_pad_unrouted_pcb())
        pcb = PCB.load(pcb_path)
        rules = get_profile("jlcpcb").get_design_rules(2, 1.0)

        results = ConnectivityRule().check(pcb, rules)

        assert results.error_count == 1
        v = results.errors[0]
        assert v.rule_id == "connectivity"
        assert v.severity == "error"
        assert "VIN" in v.message
        assert v.nets == ("VIN",)

    def test_three_pad_partial_fires_one_error(self, tmp_path: Path) -> None:
        from kicad_tools.manufacturers import get_profile
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.rules.connectivity import ConnectivityRule

        pcb_path = tmp_path / "three_pad_partial.kicad_pcb"
        pcb_path.write_text(_three_pad_partial_pcb())
        pcb = PCB.load(pcb_path)
        rules = get_profile("jlcpcb").get_design_rules(2, 1.0)

        results = ConnectivityRule().check(pcb, rules)

        assert results.error_count == 1
        v = results.errors[0]
        assert v.nets == ("SIG",)
        # "incomplete" wording, not "unrouted"
        assert "partially routed" in v.message
        assert "1 of 3" in v.message  # 1 stranded of 3 total

    def test_pour_net_with_zone_no_error(self, tmp_path: Path) -> None:
        from kicad_tools.manufacturers import get_profile
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.rules.connectivity import ConnectivityRule

        pcb_path = tmp_path / "pour_with_zone.kicad_pcb"
        pcb_path.write_text(_three_pad_pour_with_zone_pcb())
        pcb = PCB.load(pcb_path)
        rules = get_profile("jlcpcb").get_design_rules(2, 1.0)

        results = ConnectivityRule().check(pcb, rules)

        # All three GND pads are inside the same-net filled polygon, so
        # NetStatusAnalyzer reports status="complete" and the rule
        # produces zero violations.
        assert results.error_count == 0

    def test_discontinuous_pour_does_not_fire(self, tmp_path: Path) -> None:
        """A pour net with real fill but a stitching residual is advisory (#3914).

        GND owns a filled F.Cu zone, but the fill reaches only R1.1; R2.1 is
        stranded in the boundary-only gap.  ``NetStatusAnalyzer`` correctly
        reports the net ``incomplete`` (R2.1 is genuinely off-copper), but
        because the net owns real filled pour copper the incompleteness is a
        stitching residual -- advisory, not a missing-trace defect -- so the
        rule must produce zero errors.  This is the false positive removed by
        the ``has_filled_zone`` guard.
        """
        from kicad_tools.analysis.net_status import NetStatusAnalyzer
        from kicad_tools.manufacturers import get_profile
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.rules.connectivity import ConnectivityRule

        pcb_path = tmp_path / "discontinuous_pour.kicad_pcb"
        pcb_path.write_text(_discontinuous_pour_pcb())
        pcb = PCB.load(pcb_path)
        rules = get_profile("jlcpcb").get_design_rules(2, 1.0)

        # Precondition: the net really is incomplete-but-advisory, so the
        # zero-error result below exercises the suppression path (not the
        # "complete" fast path).
        gnd = NetStatusAnalyzer(pcb).analyze().get_net("GND")
        assert gnd is not None
        assert gnd.status == "incomplete"
        assert gnd.has_filled_zone is True
        assert gnd.is_advisory_incomplete is True

        results = ConnectivityRule().check(pcb, rules)
        assert results.error_count == 0

    def test_pour_named_net_without_zone_fires(self, tmp_path: Path) -> None:
        """A GND-named net with no copper anywhere is still an error.

        The rule does NOT name-suppress -- the zone-based escape hatch
        is connectivity-driven (``NetStatusAnalyzer.status == "complete"``
        because filled polygons cover the pads), not name-driven.  A
        ``GND`` net without zones AND without traces is structurally
        just as disconnected as a signal net.
        """
        from kicad_tools.manufacturers import get_profile
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.rules.connectivity import ConnectivityRule

        pcb_path = tmp_path / "pour_no_zone.kicad_pcb"
        pcb_path.write_text(_three_pad_pour_no_zone_pcb())
        pcb = PCB.load(pcb_path)
        rules = get_profile("jlcpcb").get_design_rules(2, 1.0)

        results = ConnectivityRule().check(pcb, rules)

        assert results.error_count == 1
        v = results.errors[0]
        assert v.nets == ("GND",)

    def test_fully_routed_no_error(self, tmp_path: Path) -> None:
        from kicad_tools.manufacturers import get_profile
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.rules.connectivity import ConnectivityRule

        pcb_path = tmp_path / "two_pad_routed.kicad_pcb"
        pcb_path.write_text(_two_pad_routed_pcb())
        pcb = PCB.load(pcb_path)
        rules = get_profile("jlcpcb").get_design_rules(2, 1.0)

        results = ConnectivityRule().check(pcb, rules)

        assert results.error_count == 0

    def test_rules_checked_counter(self, tmp_path: Path) -> None:
        """The per-rule counter must increment exactly once per check."""
        from kicad_tools.manufacturers import get_profile
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.rules.connectivity import ConnectivityRule

        pcb_path = tmp_path / "fully_routed.kicad_pcb"
        pcb_path.write_text(_two_pad_routed_pcb())
        pcb = PCB.load(pcb_path)
        rules = get_profile("jlcpcb").get_design_rules(2, 1.0)

        results = ConnectivityRule().check(pcb, rules)
        assert results.rules_checked == 1
        assert results.rules_checked_by_rule.get("connectivity") == 1


class TestConnectivityRuleCli:
    """End-to-end CLI tests via :func:`check_cmd.main`."""

    def test_only_connectivity_reports_errors(self, capsys, tmp_path: Path) -> None:
        from kicad_tools.cli.check_cmd import main

        pcb_path = tmp_path / "two_pad_unrouted.kicad_pcb"
        pcb_path.write_text(_two_pad_unrouted_pcb())

        rc = main([str(pcb_path), "--only", "connectivity"])
        assert rc == 2  # Errors found.

        captured = capsys.readouterr()
        assert "connectivity" in captured.out
        # The unrouted VIN net must surface by name.
        assert "VIN" in captured.out

    def test_skip_connectivity_excludes_rule(self, capsys, tmp_path: Path) -> None:
        """``--skip connectivity`` is the documented escape hatch."""
        from kicad_tools.cli.check_cmd import main

        pcb_path = tmp_path / "two_pad_unrouted.kicad_pcb"
        pcb_path.write_text(_two_pad_unrouted_pcb())

        main([str(pcb_path), "--skip", "connectivity"])

        captured = capsys.readouterr()
        # The connectivity rule_id must NOT appear in the BY RULE block.
        # We assert on the rule_id token rather than the literal word
        # because the section header uses different framing.
        assert "[X] connectivity" not in captured.out

    def test_json_output_resolves_type(self, capsys, tmp_path: Path) -> None:
        """JSON output round-trips rule_id -> type without 'unknown'."""
        from kicad_tools.cli.check_cmd import main

        pcb_path = tmp_path / "two_pad_unrouted.kicad_pcb"
        pcb_path.write_text(_two_pad_unrouted_pcb())

        rc = main(
            [
                str(pcb_path),
                "--only",
                "connectivity",
                "--format",
                "json",
            ]
        )
        assert rc == 2

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["summary"]["errors"] >= 1
        assert data["summary"]["passed"] is False

        violations = data["violations"]
        assert len(violations) >= 1
        for v in violations:
            assert v["rule_id"] == "connectivity"
            # Critical: must NOT resolve to 'unknown' -- verifies the
            # ViolationType enum + alias entry are wired correctly
            # (#3041 mirrors the #2521 precedent).
            assert v["type"] == "connectivity"
            assert v["severity"] == "error"

    def test_check_all_includes_connectivity(self, capsys, tmp_path: Path) -> None:
        """Running ``kct check`` (no --only) catches connectivity errors.

        This is the headline regression for #3041: a board with
        unrouted nets must NOT report ``DRC PASS`` from the default
        ``kct check`` invocation.
        """
        from kicad_tools.cli.check_cmd import main

        pcb_path = tmp_path / "two_pad_unrouted.kicad_pcb"
        pcb_path.write_text(_two_pad_unrouted_pcb())

        rc = main([str(pcb_path)])
        # Exit 2 = errors found.  The "DRC PASS" misleading message
        # from before #3041 would have produced exit 0 here.
        assert rc == 2

        captured = capsys.readouterr()
        assert "DRC PASSED" not in captured.out
        assert "connectivity" in captured.out


class TestConnectivityRuleBoard01:
    """Integration test against the committed board 01 routed PCB.

    Board 01 (voltage-divider) was the headline #3041 example: it had
    been routing only 1 of 2 nets while ``kct check`` reported PASS. The
    fix landed and the connectivity rule was wired into ``kct check``.
    Since #3291 the board now routes cleanly (3/3 nets, 0 connectivity
    errors), so this test pins the post-fix invariant: connectivity is
    OK on the committed routed PCB.

    To exercise the rule's failure path itself, see
    ``TestConnectivityRuleCLI`` above, which builds a synthetic 2-pad
    unrouted PCB and asserts the rule fires.
    """

    def test_board_01_routed_pcb_passes_connectivity(self, capsys) -> None:
        from kicad_tools.cli.check_cmd import main

        board_pcb = (
            Path(__file__).resolve().parent.parent
            / "boards"
            / "01-voltage-divider"
            / "output"
            / "voltage_divider_routed.kicad_pcb"
        )
        if not board_pcb.exists():
            # Skip rather than fail: the routed PCB is a committed
            # artifact whose presence depends on the boards/01 build
            # state.  Conditional skip preserves the test as a
            # regression sentinel without flaking when the artifact is
            # absent.
            import pytest

            pytest.skip(f"{board_pcb} not present in this checkout")

        rc = main([str(board_pcb), "--only", "connectivity", "--mfr", "jlcpcb"])
        assert rc == 0, (
            "Board 01 must route cleanly with no connectivity errors "
            "after #3291 (gold-standard fleet board). Pre-#3291 this "
            "intentionally returned 2 to pin the #3041 regression."
        )

        captured = capsys.readouterr()
        assert "DRC PASSED" in captured.out


class TestConnectivityRuleRegistry:
    """Registry-level checks: the rule is properly wired into both
    :attr:`DRCChecker.CHECK_ALL_METHODS` and the CLI dispatcher."""

    def test_connectivity_in_check_all(self) -> None:
        from kicad_tools.validate import DRCChecker

        assert "check_connectivity" in DRCChecker.CHECK_ALL_METHODS

    def test_connectivity_in_cli_categories(self) -> None:
        from kicad_tools.cli import check_cmd

        assert "connectivity" in check_cmd.CHECK_CATEGORIES

    def test_violation_type_alias(self) -> None:
        """Rule ID 'connectivity' must resolve to ViolationType.CONNECTIVITY."""
        from kicad_tools.drc.violation import ViolationType

        assert ViolationType.from_string("connectivity") is ViolationType.CONNECTIVITY
