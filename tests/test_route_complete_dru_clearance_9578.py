"""``kct route --complete`` vs the board's own clearance rules (Issue #9578).

Two directions, one test each, plus the edge cases the AC names:

* **Direction 1** -- preserved copper that KiCad passes was reported as an
  input defect (exit 8).  The board's footprint-scoped rules
  (``A.memberOfFootprint('REF')``) were invisible to the evaluator, so an
  unknown rule's conservative reading (the *largest* value it could resolve
  to) flagged a legal 0.16 mm gap next to a fine-pitch part against the
  0.20 mm default.  ``ItemProps`` now carries the footprint reference and
  the footprint's library ID (``memberOfFootprint`` is case-sensitive with
  wildcards and also matches an exact library ID, probed on ``kicad-cli``
  10.0.6 -- #9588 review), and a pair whose verdict still hinges on an
  undecided rule is reported as unknown -- naming the rule -- never as an
  input defect.
* **Direction 2** -- new copper was committed below a net-class
  ``.kicad_dru`` rule.  ``_read_dru_clearances`` skipped every conditional
  clearance rule, so a ``A.NetClass == 'HV'`` rule requiring 0.4 mm never
  reached the run's trace/via floors and vias landed 0.1 mm apart.  The
  strictest conditional minimum now floors both resolutions
  (``PROJECT_DRU_CONDITIONAL``), with ``(severity ignore)`` rules excluded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.route_cmd import _audit_kept_copper_clearance
from kicad_tools.router.board_clearance_rules import (
    CLEARANCE,
    BoardClearanceRules,
    DruRule,
    ItemProps,
    evaluate_condition,
    item_props_for_type,
    parse_dru,
)
from kicad_tools.router.clearance_resolver import (
    DeclaredClearanceRules,
    RuleSource,
    _read_dru_clearances,
    read_declared_clearance_rules,
    resolve_base_clearance,
    resolve_via_clearance,
)
from kicad_tools.router.core import Autorouter
from kicad_tools.router.io import kept_copper_clearance_violations
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules


def _router() -> Autorouter:
    return Autorouter(
        width=40,
        height=40,
        rules=DesignRules(trace_width=0.2, trace_clearance=0.2, grid_resolution=0.1),
    )


def _kept_trace() -> Route:
    """Horizontal +3V3 trace along y=10, x 5..19 (edges at y=9.875 / 10.125)."""
    return Route(
        net=1,
        net_name="+3V3",
        segments=[Segment(x1=5.0, y1=10.0, x2=19.0, y2=10.0, layer=Layer.F_CU, width=0.25, net=1)],
        vias=[],
    )


def _pad(
    net: int = 2,
    name: str = "GND",
    x: float = 12.0,
    y: float = 10.7,
    footprint_name: str = "",
) -> Pad:
    """A pad whose F.Cu edge sits at y = ``y - 0.5`` (0.075 mm under the trace)."""
    return Pad(
        x=x,
        y=y,
        width=1.0,
        height=1.0,
        net=net,
        net_name=name,
        layer=Layer.F_CU,
        ref="J3",
        pin="1",
        footprint_name=footprint_name,
    )


def _footprint_rule(
    condition: str = "A.memberOfFootprint('J3')",
    name: str = "fine pitch exempt",
    minimum: float = 0.1,
) -> DruRule:
    return DruRule(name, condition, None, ((CLEARANCE, minimum),))


# ---------------------------------------------------------------- Direction 1


class TestFootprintScopedRuleOnPreservedCopper:
    """A gap legal only under a footprint-scoped rule is not reported."""

    @staticmethod
    def _router_with_gap(gap: float = 0.15) -> Autorouter:
        router = _router()
        router.pads[("J3", "1")] = _pad(y=10.125 + gap + 0.5)
        router.existing_routes.append(_kept_trace())
        return router

    def test_member_of_footprint_rule_is_not_reported(self) -> None:
        # 0.1 mm <= gap 0.15 mm < 0.2 mm default: legal only under J3's rule.
        rules = BoardClearanceRules(dru_rules=[_footprint_rule()])

        near = kept_copper_clearance_violations(self._router_with_gap(), board_rules=rules)

        assert near == [], "the 0.15 mm gap is legal under J3's 0.1 mm rule"

    def test_condition_matches_in_either_order(self) -> None:
        """The pad may be ``A`` or ``B``: the evaluator tries both orders."""
        rules = BoardClearanceRules(dru_rules=[_footprint_rule("B.memberOfFootprint('J3')")])

        assert kept_copper_clearance_violations(self._router_with_gap(), board_rules=rules) == []

    def test_wildcards_match_but_case_does_not(self) -> None:
        """Probed against ``kicad-cli`` 10.0.6 (#9588 review): ``'J*'`` still
        selects the footprint, but the argument is **case-sensitive** --
        ``'j?'`` and ``'j3'`` do not match reference ``J3``, so KiCad's
        0.2 mm default applies and the 0.15 mm gap is reported."""
        for condition, reported in (
            ("A.memberOfFootprint('J*')", False),
            ("A.memberOfFootprint('j?')", True),
            ("A.memberOfFootprint('j3')", True),
        ):
            rules = BoardClearanceRules(dru_rules=[_footprint_rule(condition)])
            near = kept_copper_clearance_violations(self._router_with_gap(), board_rules=rules)
            assert (len(near) == 1) == reported, condition
            if reported:
                assert near[0].required == pytest.approx(0.2), condition
                assert near[0].unknown_rules == (), condition

    def test_board_copper_is_never_a_footprint_member(self) -> None:
        """``memberOfFootprint`` is decisively false on a kept track/via."""
        trace = item_props_for_type("track", "+3V3", layer="F.Cu", footprint_ref="")
        assert (
            evaluate_condition(
                "A.memberOfFootprint('J3')", trace, ItemProps("Pad", "GND", footprint_ref="J3")
            )
            is False
        )

    def test_wildcard_does_not_match_board_copper(self) -> None:
        """A wildcard matches footprint *names*; board copper has none (#9588 review)."""
        trace = item_props_for_type("track", "+3V3", layer="F.Cu", footprint_ref="")
        assert (
            evaluate_condition(
                "A.memberOfFootprint('*')", trace, ItemProps("Pad", "GND", footprint_ref="J3")
            )
            is False
        )

    def test_a_rule_the_gap_actually_breaks_is_still_reported(self) -> None:
        """KiCad-exact: 0.075 mm is below the exempt rule's own 0.1 mm."""
        rules = BoardClearanceRules(dru_rules=[_footprint_rule()])

        near = kept_copper_clearance_violations(self._router_with_gap(0.075), board_rules=rules)

        assert len(near) == 1 and near[0].required == pytest.approx(0.1)

    def test_unknown_reference_stays_conservative(self) -> None:
        """An item with no footprint reference leaves the rule undecided."""
        pad = item_props_for_type("pad", "GND")
        assert (
            evaluate_condition("A.memberOfFootprint('J3')", pad, ItemProps("Track", "+3V3")) is None
        )
        rules = BoardClearanceRules(dru_rules=[_footprint_rule()])
        # ...and the requirement keeps its largest-possible-value reading.
        assert rules.required(CLEARANCE, pad, ItemProps("Track", "+3V3")) == pytest.approx(0.2)


class TestUnknownRuleIsNamedNotAnInputDefect:
    """A rule the evaluator cannot decide counts as unknown, never a defect."""

    @staticmethod
    def _rules() -> BoardClearanceRules:
        return BoardClearanceRules(
            dru_rules=[
                DruRule("keepout area", "A.insideArea('HV zone')", None, ((CLEARANCE, 0.25),))
            ]
        )

    @staticmethod
    def _dru_text() -> str:
        return """(version 1)
(rule "keepout area"
  (condition "A.insideArea('HV zone')")
  (constraint clearance (min 0.25mm)))
"""

    @staticmethod
    def _router_with_gap(gap: float) -> Autorouter:
        router = _router()
        router.pads[("J3", "1")] = _pad(y=10.125 + gap + 0.5)
        router.existing_routes.append(_kept_trace())
        return router

    def test_pair_in_the_unknown_band_is_tagged_with_the_rule_name(self) -> None:
        # 0.2 mm < gap 0.21 mm < 0.25 mm: provably legal under the default,
        # possibly illegal under the area rule.
        near = kept_copper_clearance_violations(
            self._router_with_gap(0.21), board_rules=self._rules()
        )

        assert len(near) == 1
        assert near[0].unknown_rules == ("keepout area",)
        assert near[0].required == pytest.approx(0.25)

    def test_pair_below_the_provable_floor_is_a_definite_defect(self) -> None:
        # 0.15 mm < 0.2 mm default: a violation whichever way the rule goes.
        near = kept_copper_clearance_violations(
            self._router_with_gap(0.15), board_rules=self._rules()
        )

        assert len(near) == 1
        assert near[0].unknown_rules == ()

    def test_new_copper_gating_keeps_the_conservative_reading(self) -> None:
        assert self._rules().required(
            CLEARANCE, ItemProps("Track", "A"), ItemProps("Track", "B")
        ) == pytest.approx(0.25)

    def test_cli_gate_does_not_fail_on_unknown_pairs(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "out.kicad_pcb"
        out.write_text("(kicad_pcb)")
        out.with_suffix(".kicad_dru").write_text(self._dru_text())
        # The unknown-band pair alone: no error returns, the rule is named.
        errors = _audit_kept_copper_clearance(
            self._router_with_gap(0.21), out, quiet=False, source_pcb=None
        )
        captured = capsys.readouterr().out

        assert errors == []
        assert "UNKNOWN" in captured
        assert "keepout area" in captured

    def test_cli_gate_still_fails_on_a_definite_pair(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "out.kicad_pcb"
        out.write_text("(kicad_pcb)")
        out.with_suffix(".kicad_dru").write_text(self._dru_text())
        errors = _audit_kept_copper_clearance(
            self._router_with_gap(0.15), out, quiet=False, source_pcb=None
        )

        assert len(errors) == 1
        assert "UNKNOWN" not in capsys.readouterr().out


class TestMemberOfFootprintSemanticsMatchKicad:
    """The argument semantics of ``memberOfFootprint``, probed black-box
    against the pinned ``kicad-cli`` 10.0.6 image (#9588 review): a
    case-sensitive reference match with ``*`` / ``?`` wildcards, plus exact
    library-ID arguments.  Each test names its probe row."""

    J3_PAD = ItemProps("Pad", "GND", footprint_ref="J3", footprint_lib_id="Conn:Hdr")
    TRACK = ItemProps("Track", "+3V3", footprint_ref="", footprint_lib_id="")

    @staticmethod
    def _pad_with(lib_id: str | None) -> ItemProps:
        return ItemProps("Pad", "GND", footprint_ref="J3", footprint_lib_id=lib_id)

    def test_reference_exact_and_wildcard_select_the_pad(self) -> None:
        for pattern in ("J3", "J*", "J?"):
            assert (
                evaluate_condition(f"A.memberOfFootprint('{pattern}')", self.J3_PAD, self.TRACK)
                is True
            ), pattern

    def test_reference_match_is_case_sensitive(self) -> None:
        # 'j3' and 'j?' select nothing: KiCad flags the pair at the default.
        for pattern in ("j3", "j?"):
            assert (
                evaluate_condition(f"A.memberOfFootprint('{pattern}')", self.J3_PAD, self.TRACK)
                is False
            ), pattern

    def test_exact_library_id_selects_the_pad(self) -> None:
        assert (
            evaluate_condition("A.memberOfFootprint('Conn:Hdr')", self.J3_PAD, self.TRACK) is True
        )

    def test_library_id_match_is_case_sensitive(self) -> None:
        assert (
            evaluate_condition("A.memberOfFootprint('conn:hdr')", self.J3_PAD, self.TRACK) is False
        )

    def test_wildcard_on_a_library_id_stays_undecided(self) -> None:
        # 'Conn:*' was not probed against KiCad: not assumed either way, so
        # the condition stays unknown and the rule keeps its floor.
        assert evaluate_condition("A.memberOfFootprint('Conn:*')", self.J3_PAD, self.TRACK) is None

    def test_unknown_library_id_stays_undecided(self) -> None:
        assert (
            evaluate_condition("A.memberOfFootprint('Conn:Hdr')", self._pad_with(None), self.TRACK)
            is None
        )

    def test_a_known_different_library_id_decides_false(self) -> None:
        assert (
            evaluate_condition(
                "A.memberOfFootprint('Conn:Hdr')", self._pad_with("Other:Foo"), self.TRACK
            )
            is False
        )

    def test_footprint_without_library_association_never_matches_an_id(self) -> None:
        # An embedded footprint has no library ID: an ID argument cannot
        # select it -- under KiCad's reading or ours.
        assert (
            evaluate_condition("A.memberOfFootprint('Conn:Hdr')", self._pad_with(""), self.TRACK)
            is False
        )

    def test_either_side_of_the_pair_may_be_the_member(self) -> None:
        assert (
            evaluate_condition("B.memberOfFootprint('Conn:Hdr')", self.TRACK, self.J3_PAD) is True
        )

    def test_board_copper_is_decisive_even_with_an_unknown_id(self) -> None:
        assert evaluate_condition("A.memberOfFootprint('*')", self.TRACK, self.J3_PAD) is False
        bare_track = ItemProps("Track", "+3V3", footprint_ref="", footprint_lib_id=None)
        assert evaluate_condition("A.memberOfFootprint('*')", bare_track, self.J3_PAD) is False

    def test_unknown_reference_stays_undecided(self) -> None:
        pad = item_props_for_type("pad", "GND")
        assert evaluate_condition("A.memberOfFootprint('J3')", pad, self.TRACK) is None


class TestFootprintRuleArgumentsJudgeTheGapLikeKicad:
    """The two audit cases the #9588 review measured against KiCad: a
    case-variant exemption must not shield the gap, and a library-ID rule
    must select the pad's footprint."""

    @staticmethod
    def _router_with_gap(gap: float, footprint_name: str = "") -> Autorouter:
        router = _router()
        router.pads[("J3", "1")] = _pad(y=10.125 + gap + 0.5, footprint_name=footprint_name)
        router.existing_routes.append(_kept_trace())
        return router

    def test_case_variant_exemption_does_not_shield_the_gap(self) -> None:
        # KiCad: 'j3' selects nothing, the 0.2 mm default flags the 0.15 mm
        # gap.  The pre-review evaluator case-folded and passed the board.
        rules = BoardClearanceRules(dru_rules=[_footprint_rule("A.memberOfFootprint('j3')")])

        near = kept_copper_clearance_violations(self._router_with_gap(0.15), board_rules=rules)

        assert len(near) == 1
        assert near[0].required == pytest.approx(0.2)
        assert near[0].unknown_rules == ()

    def test_library_id_rule_selects_the_pad(self) -> None:
        # KiCad: 'Conn:Hdr' selects J3's footprint, so its own 0.5 mm
        # minimum flags the 0.3 mm gap the 0.2 mm default would pass.
        rules = BoardClearanceRules(
            dru_rules=[_footprint_rule("A.memberOfFootprint('Conn:Hdr')", minimum=0.5)]
        )

        near = kept_copper_clearance_violations(
            self._router_with_gap(0.3, footprint_name="Conn:Hdr"), board_rules=rules
        )

        assert len(near) == 1
        assert near[0].required == pytest.approx(0.5)
        assert near[0].unknown_rules == ()

    def test_footprint_without_library_association_passes_an_id_rule(self) -> None:
        # No library ID, no ID match -- KiCad passes this board and so does
        # the audit (decisively, not via the conservative reading).
        rules = BoardClearanceRules(
            dru_rules=[_footprint_rule("A.memberOfFootprint('Conn:Hdr')", minimum=0.5)]
        )

        assert kept_copper_clearance_violations(self._router_with_gap(0.3), board_rules=rules) == []


# ---------------------------------------------------------------- Direction 2


HV_DRU = """(version 1)
(rule "HV clearance"
  (condition "A.NetClass == 'HV' || B.NetClass == 'HV'")
  (constraint clearance (min 0.4mm)))
(rule "ignored clearance"
  (condition "A.NetClass == 'IgnoreMe' || B.NetClass == 'IgnoreMe'")
  (constraint clearance (min 0.9mm))
  (severity ignore))
(rule "via clearance"
  (condition "A.Type == 'Via' || B.Type == 'Via'")
  (constraint clearance (min 0.35mm)))
(rule "unconditional clearance"
  (constraint clearance (min 0.15mm)))
"""


class TestConditionalNetClassRuleFloorsNewCopper:
    @staticmethod
    def _board(tmp_path: Path, dru: str = HV_DRU) -> Path:
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        pcb.with_suffix(".kicad_dru").write_text(dru)
        return pcb

    def test_layers_are_classified(self, tmp_path: Path) -> None:
        pcb = self._board(tmp_path)

        assert _read_dru_clearances(pcb.with_suffix(".kicad_dru")) == (
            0.15,  # unconditional
            0.35,  # via-scoped (#6280)
            0.4,  # conditional netclass (#9578); the 0.9 ignore rule is skipped
        )

    def test_trace_and_via_floors_honour_the_net_class_rule(self, tmp_path: Path) -> None:
        declared = read_declared_clearance_rules(self._board(tmp_path))

        assert declared.dru_conditional_mm == pytest.approx(0.4)
        trace = declared.project_requirement()
        via = declared.via_requirement()
        assert trace == (pytest.approx(0.4), RuleSource.PROJECT_DRU_CONDITIONAL, None)
        assert via == (pytest.approx(0.4), RuleSource.PROJECT_DRU_CONDITIONAL, None)

    def test_resolve_base_clearance_floors_at_the_rule(self, tmp_path: Path) -> None:
        declared = read_declared_clearance_rules(self._board(tmp_path))

        # The default --clearance target (0.15 mm) is raised to the rule.
        resolved = resolve_base_clearance(target_mm=0.15, declared=declared, fab_floor_mm=None)
        assert resolved.required_mm == pytest.approx(0.4)
        assert resolved.source is RuleSource.PROJECT_DRU_CONDITIONAL
        assert resolve_via_clearance(
            declared=declared, trace_clearance_mm=0.15
        ).required_mm == pytest.approx(0.4)

    def test_explicit_clearance_warns_instead_of_moving(self, tmp_path: Path) -> None:
        declared = read_declared_clearance_rules(self._board(tmp_path))

        resolved = resolve_base_clearance(
            target_mm=0.2, declared=declared, explicit_target=True, fab_floor_mm=None
        )

        assert resolved.required_mm == pytest.approx(0.2), "an explicit --clearance is a waiver"
        assert resolved.warning is not None
        assert "0.4" in resolved.warning

    def test_severity_ignore_rule_does_not_floor(self, tmp_path: Path) -> None:
        pcb = self._board(
            tmp_path,
            """(version 1)
(rule "ignored clearance"
  (condition "A.NetClass == 'HV' || B.NetClass == 'HV'")
  (constraint clearance (min 0.9mm))
  (severity ignore))
""",
        )

        declared = read_declared_clearance_rules(pcb)

        assert declared.dru_conditional_mm is None
        assert declared.project_requirement() is None
        assert declared.via_requirement() is None

    def test_via_scoped_rule_stays_out_of_the_boardwide_floor(self, tmp_path: Path) -> None:
        pcb = self._board(
            tmp_path,
            """(version 1)
(rule "via clearance"
  (condition "A.Type == 'Via' || B.Type == 'Via'")
  (constraint clearance (min 0.35mm)))
""",
        )

        declared = read_declared_clearance_rules(pcb)

        assert declared.dru_conditional_mm is None
        assert declared.dru_via_mm == pytest.approx(0.35)
        assert declared.project_requirement() is None
        assert declared.via_requirement() == (pytest.approx(0.35), RuleSource.PROJECT_DRU_VIA, None)

    def test_nothing_declared_is_unchanged(self, tmp_path: Path) -> None:
        declared = read_declared_clearance_rules(self._board(tmp_path, "(version 1)\n"))

        assert declared == DeclaredClearanceRules()
        assert declared.is_empty()
        assert resolve_base_clearance(
            target_mm=0.15, declared=declared
        ).required_mm == pytest.approx(0.15)

    def test_nonclearance_constraints_do_not_floor(self, tmp_path: Path) -> None:
        pcb = self._board(
            tmp_path,
            """(version 1)
(rule "PTH hole"
  (condition "A.Pad_Type == 'Through-hole' || B.Pad_Type == 'Through-hole'")
  (constraint hole_clearance (min 0.28mm)))
""",
        )

        assert read_declared_clearance_rules(pcb).dru_conditional_mm is None


# ------------------------------------------- Direction 2, #9588-review scoping


ZONE_DRU = """(version 1)
(rule "Clearance - jlcpcb"
  (constraint clearance (min 0.127mm)))
(rule "SMD Pad Clearance - jlcpcb"
  (condition "A.Type == 'Pad' && B.Type == 'Pad' && A.Pad_Type == 'SMD' && B.Pad_Type == 'SMD' \
&& A.Net != B.Net && A.Reference != B.Reference")
  (constraint clearance (min 0.2mm)))
(rule "Zone clearance - GND (B.Cu)"
  (layer "B.Cu")
  (condition "(A.Type == 'Zone' && A.NetName == 'GND') || (B.Type == 'Zone' \
&& B.NetName == 'GND')")
  (constraint clearance (min 0.3mm)))
(rule "Reviewed clearance - Power"
  (condition "(A.NetClass == 'Power' || B.NetClass == 'Power') && !(A.Type == 'Pad' \
&& B.Type == 'Pad' && A.Reference == B.Reference) && !A.isCoupledDiffPair()")
  (constraint clearance (min 0.2mm)))
"""

_PAD_PAD_DRU = """(version 1)
(rule "SMD Pad Clearance - jlcpcb"
  (condition "A.Type == 'Pad' && B.Type == 'Pad' && A.Pad_Type == 'SMD' && B.Pad_Type == 'SMD' \
&& A.Net != B.Net && A.Reference != B.Reference")
  (constraint clearance (min 0.2mm)))
"""


def _expected_conditional_floor(dru_path: Path) -> float | None:
    """Re-derive the conditional floor through a different parse path.

    ``parse_dru`` + the probe shapes, so a bucketing bug in the resolver's
    own ``.kicad_dru`` walk (a scoped rule leaking into another layer, as in
    the first cut of the #9588-review fix) shows up as a mismatch.
    """
    from kicad_tools.router.clearance_resolver import (
        _condition_may_select_routed_pair,
        _is_via_scoped_condition,
    )

    rules, error = parse_dru(dru_path)
    assert error is None, f"{dru_path}: {error}"
    best: float | None = None
    for rule in rules:
        value = rule.value("clearance")
        if value is None or rule.ignored:
            continue
        if rule.condition is None or _is_via_scoped_condition(rule.condition):
            continue
        if _condition_may_select_routed_pair(rule.condition):
            best = value if best is None or value > best else best
    return best


class TestBoardWideConditionalFloorScopesToRoutedPairs:
    """#9588 review: the board-wide conditional floor exists for the pairs
    ``kct route`` commits, so a rule whose condition is provably false on
    every (Track, Track) / (Track, Via) / (Via, Via) pair -- and on those
    pairs against the board's existing pads -- floors nothing, and one that
    might still apply keeps the conservative floor."""

    @staticmethod
    def _board(tmp_path: Path, dru: str = ZONE_DRU) -> Path:
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        pcb.with_suffix(".kicad_dru").write_text(dru)
        return pcb

    def test_zone_and_pad_rules_floor_nothing(self, tmp_path: Path) -> None:
        """The 0.3 mm zone rule and the 0.2 mm pad-to-pad rule are decided
        false on every track/via pair; the undecided net-class rule keeps the
        floor -- and nothing leaks into the unconditional bucket."""
        pcb = self._board(tmp_path)

        assert _read_dru_clearances(pcb.with_suffix(".kicad_dru")) == (
            0.127,  # unconditional: no condition
            None,  # via-scoped
            0.2,  # conditional: the net-class rule only
        )
        declared = read_declared_clearance_rules(pcb)
        assert declared.dru_mm == pytest.approx(0.127)
        assert declared.dru_conditional_mm == pytest.approx(0.2)
        assert declared.project_requirement() == (
            pytest.approx(0.2),
            RuleSource.PROJECT_DRU_CONDITIONAL,
            None,
        )

    def test_zone_rule_alone_floors_nothing(self, tmp_path: Path) -> None:
        pcb = self._board(
            tmp_path,
            """(version 1)
(rule "Zone clearance - GND (B.Cu)"
  (layer "B.Cu")
  (condition "(A.Type == 'Zone' && A.NetName == 'GND') || (B.Type == 'Zone' \
&& B.NetName == 'GND')")
  (constraint clearance (min 0.3mm)))
""",
        )

        declared = read_declared_clearance_rules(pcb)

        assert declared.dru_mm is None
        assert declared.dru_via_mm is None
        assert declared.dru_conditional_mm is None
        assert declared.project_requirement() is None
        assert declared.via_requirement() is None
        assert declared.is_empty()

    def test_pad_to_pad_rule_alone_floors_nothing(self, tmp_path: Path) -> None:
        declared = read_declared_clearance_rules(self._board(tmp_path, _PAD_PAD_DRU))

        assert declared.dru_mm is None
        assert declared.dru_via_mm is None
        assert declared.dru_conditional_mm is None
        assert declared.project_requirement() is None
        assert declared.via_requirement() is None
        assert declared.is_empty()

    def test_pad_to_routed_track_rule_keeps_the_floor(self, tmp_path: Path) -> None:
        """Non-blocking #9588-review gap closed: routed tracks and vias are
        placed next to the board's pads, so a rule scoped to a pad/track
        (or via/pad) pair selects a pair the router commits and floors it."""
        pcb = self._board(
            tmp_path,
            """(version 1)
(rule "pad to routed track"
  (condition "A.Type == 'Pad' && B.Type == 'Track'")
  (constraint clearance (min 0.3mm)))
""",
        )

        assert read_declared_clearance_rules(pcb).dru_conditional_mm == pytest.approx(0.3)

    def test_undecidable_condition_keeps_the_floor(self, tmp_path: Path) -> None:
        """``insideArea`` is not modelled: the probe is undecided, so the
        rule keeps its floor -- the conservative reading."""
        pcb = self._board(
            tmp_path,
            """(version 1)
(rule "keepout band"
  (condition "A.insideArea('X')")
  (constraint clearance (min 0.25mm)))
""",
        )

        assert read_declared_clearance_rules(pcb).dru_conditional_mm == pytest.approx(0.25)

    def test_condition_true_on_a_routed_shape_keeps_the_floor(self, tmp_path: Path) -> None:
        pcb = self._board(
            tmp_path,
            """(version 1)
(rule "not a pad"
  (condition "A.Type != 'Pad'")
  (constraint clearance (min 0.9mm)))
""",
        )

        assert read_declared_clearance_rules(pcb).dru_conditional_mm == pytest.approx(0.9)

    def test_routed_example_boards_floor_only_at_routable_rules(self) -> None:
        """Every routed example board's conditional floor equals the strictest
        rule the probe still keeps -- the #9588-review measurement, pinned:
        the zone-only 0.3 mm rules on 01/03 and the pad-to-pad 0.2 mm rule on
        06 no longer win anything."""
        boards = sorted(
            Path(__file__).resolve().parents[1].glob("boards/*/output/*_routed.kicad_pcb")
        )
        if not boards:
            pytest.skip("routed example boards not present")
        for pcb in boards:
            declared = read_declared_clearance_rules(pcb)
            expected = _expected_conditional_floor(pcb.with_suffix(".kicad_dru"))
            if expected is None:
                assert declared.dru_conditional_mm is None, pcb
            else:
                assert declared.dru_conditional_mm == pytest.approx(expected), pcb

    @pytest.mark.parametrize(
        ("board_dir", "expected_mm"),
        [
            ("01-voltage-divider", 0.2),  # zone rules (0.3) dropped; Power rule wins
            ("03-usb-joystick", 0.2),  # zone rules (0.3) dropped; the 3x jump is gone
            ("06-diffpair-test", 0.2),  # pad-to-pad and zone rules dropped; Power wins
            ("07-matchgroup-test", 0.175),  # AUX/SDRAM net-class rules keep their floor
        ],
    )
    def test_measured_boards(self, board_dir: str, expected_mm: float) -> None:
        pcbs = list(
            (Path(__file__).resolve().parents[1] / "boards" / board_dir / "output").glob(
                "*_routed.kicad_pcb"
            )
        )
        if not pcbs:
            pytest.skip(f"{board_dir} routed board not present")
        assert read_declared_clearance_rules(pcbs[0]).dru_conditional_mm == pytest.approx(
            expected_mm
        )
