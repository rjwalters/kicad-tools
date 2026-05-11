"""Match-group length-skew DRC rule (N-trace generalization).

Validates that a declared / detected match group's per-member routed-length
skew (``max(L) - min(L)`` across the group) does not exceed the per-class
``length_match_tolerance_mm`` floor.  Direct N>=3 generalization of the
N=2 :class:`~kicad_tools.validate.rules.diffpair_length_skew.DiffPairLengthSkewRule`
(Epic #2556 Phase 3J, PR #2662).

Part of Epic #2661 Phase 2G (Issue #2702):

- Phase 1A (#2687, merged) -- ``NetClassRouting.length_match_tolerance_mm``
  schema field + ``effective_length_match_tolerance(default=0.5)``
  accessor.  This rule's ``DEFAULT_MATCH_GROUP_TOLERANCE_MM`` constant
  is calibrated byte-for-byte against that accessor default (the
  drift-prevention test at the bottom of
  ``tests/test_validate_match_group_length_skew.py`` enforces this).
- Phase 1B (#2693, merged) -- :class:`~kicad_tools.router.match_group_length.MatchGroup`
  dataclass + :meth:`~kicad_tools.router.match_group_length.MatchGroupTracker.get_all_skews`
  whose return shape ``dict[str, float]`` (name-keyed) matches this
  rule's :attr:`group_skew_data` parameter shape exactly.  No
  transformation needed at the caller's seam.
- Phase 2E (#2700, parallel) -- N-trace serpentine tuner.  Phase 2G is
  **independent** of 2E: this rule fires on routed-as-found geometry
  regardless of whether the tuner ran.  The validator-for-externally-
  routed-boards case (Freerouting / KiCad's own router / manual layout)
  is explicitly in scope.
- Phase 2G (this rule) -- fires when a recorded group's skew exceeds
  its per-group tolerance.

Dependency injection contract
=============================

Mirroring the
:class:`~kicad_tools.validate.rules.diffpair_length_skew.DiffPairLengthSkewRule`
pattern (Issue #2649 / PR #2662), the rule **consumes** skew data from
the caller; it does NOT re-derive it.  This decouples the rule from
:class:`~kicad_tools.router.match_group_length.MatchGroupTracker`
internals and prevents the #2521-class drift failure mode where the
router and DRC compute the same quantity by different code paths and
silently diverge.

The intended call site is the autorouter consumer (after the per-group
length tracker has run ``record_routes``) or the future Phase 2.5G
``derive_group_skew_data`` producer (a separate follow-up issue) which
re-derives the skew from a routed PCB + ``net_class_map`` sidecar at
``kct check --net-class-map`` time.

The DRC rule itself does NOT import :class:`MatchGroupTracker`, does
NOT call ``detect_match_groups``, and does NOT re-derive skew -- it
consumes whatever the caller provides.

Graceful degradation
====================

When ``group_skew_data`` is ``None`` or empty (e.g., running ``kct
check`` standalone with no router context), the rule is a conservative
no-op (``rules_checked == 0``, no violations).  This matches the
standalone-``kct check`` graceful-no-op contract used by
``diffpair_length_skew`` and is the explicit "validator-for-externally-
routed-boards" case: a board routed by Freerouting (no ``kct route``
involvement, no Phase 1B tracker context) MUST NOT have spurious
skew violations -- because the caller had no way to compute the skew.
The rule fires only when skew data was deliberately injected.

Scope (out of scope -- explicitly):

- Computing the skew (Phase 1B's
  :class:`~kicad_tools.router.match_group_length.MatchGroupTracker`
  domain).
- Detecting groups (Phase 1C's layered detector domain, PR #2694).
- Fixing the skew (Phase 2E's ``tune_match_group_v2`` domain, issue
  #2700 -- this rule does NOT consult any "tuned" flag).
- Cross-group lane-matching skew (a future ``match_group_inter_skew``
  rule, out of this epic).
- Propagation-delay-aware skew (geometric only here; delay-aware lives
  in a Phase 5+ rule per Epic #2556).
- Producer-side ``derive_group_skew_data`` wiring -- separate Phase
  2.5G follow-up (see ``check_match_group_length_skew`` docstring in
  ``validate/checker.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.router.match_group_length import MatchGroup
    from kicad_tools.schema.pcb import PCB


# Module-level default match-group length-skew tolerance in mm.  MUST
# equal the default arg of
# :meth:`NetClassRouting.effective_length_match_tolerance` (Phase 1A,
# Issue #2687, PR #2691).  The drift-prevention test in
# ``tests/test_validate_match_group_length_skew.py`` imports both and
# asserts byte-for-byte equality -- if a future change moves one but
# not the other this test fires.  The Phase 1A docstring at
# ``router/rules.py:761-763`` explicitly forward-references this
# constant name; renaming the constant would orphan that cross-
# reference.
#
# Calibration (mirrors the curator note on #2702):
#   DDR3 / DDR4 byte-lane DQ-strobe : ~0.5 mm budget
#   MIPI D-PHY / CSI / DSI lane     : ~1 mm budget
#   DDR5 / fast SerDes              : tighten via per-class override
#   USB 2.0 HS bus group (rare)     : loose (>3 mm) -- set explicitly
#
# 0.5 mm is a sane middle ground that fires on egregious failures while
# not nagging on routine bus work.  Tighter classes (DDR5, PCIe Gen3+
# lane groups) MUST set
# :attr:`NetClassRouting.length_match_tolerance_mm` explicitly.
DEFAULT_MATCH_GROUP_TOLERANCE_MM = 0.5


class MatchGroupLengthSkewRule(DRCRule):
    """Validate length-match skew for declared / detected match groups.

    The rule consumes caller-supplied ``group_skew_data`` (the per-group
    skews computed by Phase 1B's
    :meth:`~kicad_tools.router.match_group_length.MatchGroupTracker.get_all_skews`)
    and a ``tracker_match_groups`` list (the set of declared groups the
    caller wants checked).  A violation fires when, for a declared
    group, ``skew_mm > tolerance``; groups absent from
    ``tracker_match_groups`` are deliberately not checked (the rule
    applies only to groups the designer / detector opted into).

    Attributes:
        rule_id: ``"match_group_length_skew"`` -- MUST exactly match
            the alias-table key in :mod:`kicad_tools.drc.violation`.
        group_skew_data: ``{group_name -> skew_mm}`` map (mirrors
            :meth:`MatchGroupTracker.get_all_skews` return shape).
            ``None`` or empty -> the rule is a no-op (graceful
            degradation when no router context is available, e.g.,
            ``kct check`` standalone).
        tracker_match_groups: List of :class:`MatchGroup` instances to
            validate.  Groups whose name is not in ``group_skew_data``
            are silently skipped (no skew measured = no validation
            possible; mirrors the diff-pair partial-routing silencing).
            ``None`` or empty -> the rule is a no-op.
        threshold_map: ``{group_name -> tolerance_mm}`` map of per-group
            tolerance overrides keyed on group **name** (NOT a tuple --
            Phase 1B uses name as identity).  Groups missing from the
            map use the constructor's ``default_tolerance_mm``.
        default_tolerance_mm: Fallback tolerance (mm) when no per-group
            override is provided.  Defaults to
            :data:`DEFAULT_MATCH_GROUP_TOLERANCE_MM` (0.5).
    """

    rule_id = "match_group_length_skew"
    name = "Match-Group Length Skew"
    description = (
        "Validates that declared N-trace match groups have a routed-length "
        "skew (max(L) - min(L) across the group) at or below the per-class "
        "length-match tolerance."
    )

    def __init__(
        self,
        group_skew_data: dict[str, float] | None = None,
        tracker_match_groups: list["MatchGroup"] | None = None,
        threshold_map: dict[str, float] | None = None,
        default_tolerance_mm: float = DEFAULT_MATCH_GROUP_TOLERANCE_MM,
    ) -> None:
        """Initialize the rule.

        Args:
            group_skew_data: ``{group_name -> skew_mm}`` map from Phase
                1B's
                :meth:`MatchGroupTracker.get_all_skews`.  Keys are the
                group's :attr:`MatchGroup.name`.  ``None`` or empty ->
                the rule is a no-op (graceful degradation when no
                router context is available).
            tracker_match_groups: List of declared / detected
                :class:`MatchGroup` instances to validate.  ``None`` or
                empty -> the rule is a no-op.
            threshold_map: Optional per-group tolerance overrides (mm),
                keyed on group **name** (NOT an id tuple -- groups are
                name-identified per Phase 1B's identity convention).
                Missing groups use ``default_tolerance_mm``.
            default_tolerance_mm: Fallback when no per-group tolerance
                is set.  Defaults to
                :data:`DEFAULT_MATCH_GROUP_TOLERANCE_MM` (0.5).
        """
        self._group_skew_data: dict[str, float] = dict(group_skew_data) if group_skew_data else {}

        # Keep a name-keyed lookup of declared groups so check() can
        # iterate ``group_skew_data`` (the upstream "what's measured"
        # view) and resolve each entry back to its declaration in O(1).
        self._tracker_groups_by_name: dict[str, "MatchGroup"] = {}
        if tracker_match_groups:
            for grp in tracker_match_groups:
                self._tracker_groups_by_name[grp.name] = grp

        self._threshold_map: dict[str, float] = dict(threshold_map) if threshold_map else {}
        self._default_tolerance_mm = default_tolerance_mm

    def check(
        self,
        pcb: PCB,  # noqa: ARG002 (unused; signature mirrors peer rules)
        design_rules: DesignRules,  # noqa: ARG002 (unused; signature mirrors peer rules)
    ) -> DRCResults:
        """Check length-skew for all declared groups with recorded skew.

        Args:
            pcb: The PCB to check.  Unused by this rule -- the rule
                consumes caller-supplied ``group_skew_data`` keyed on
                group names (not net ids), so no PCB-side lookup is
                required.  Kept in the signature for consistency with
                other :class:`DRCRule` subclasses.
            design_rules: Active manufacturer design rules.  Unused by
                this rule -- the tolerance is per-group-explicit -- but
                kept in the signature for consistency with other
                :class:`DRCRule` subclasses.

        Returns:
            :class:`DRCResults` with one violation per declared group
            whose recorded skew exceeds its tolerance.  Empty result
            when ``group_skew_data`` is empty OR
            ``tracker_match_groups`` is empty (graceful no-op; matches
            the standalone-``kct check`` contract).
        """
        results = DRCResults()

        # Graceful degradation: empty either input -> no-op.  This is
        # the explicit "Freerouting / external router" case from the
        # issue body -- without a tracker context to populate
        # group_skew_data AND a producer-side declared-group list, the
        # rule has nothing to validate.
        if not self._group_skew_data or not self._tracker_groups_by_name:
            return results

        # Iterate the (sorted) group_skew_data entries deterministically
        # so violations appear in a stable order across runs.  Phase
        # 1B's get_all_skews() already returns name-sorted, but
        # caller-supplied data may be in arbitrary order so we re-sort
        # here defensively.
        for group_name, skew_mm in sorted(self._group_skew_data.items()):
            grp = self._tracker_groups_by_name.get(group_name)
            if grp is None:
                # Skew recorded for a group not in the declared list --
                # per the rule's declaration-gating contract, do not
                # check.  Mirrors the diffpair_length_skew engagement
                # gate (skew_data entries for un-engaged pairs are
                # ignored).
                continue

            # This group counts toward rules_checked (one check per
            # declared group with measured skew).
            results.rules_checked += 1
            results.rules_checked_by_rule["match_group_length_skew"] = (
                results.rules_checked_by_rule.get("match_group_length_skew", 0) + 1
            )

            tolerance_mm = self._threshold_map.get(group_name, self._default_tolerance_mm)
            if skew_mm > tolerance_mm:
                results.add(
                    self._make_violation(
                        group=grp,
                        skew_mm=skew_mm,
                        tolerance_mm=tolerance_mm,
                    )
                )

        return results

    def _make_violation(
        self,
        group: "MatchGroup",
        skew_mm: float,
        tolerance_mm: float,
    ) -> DRCViolation:
        """Build a DRCViolation for an over-skew match group."""
        # Gather member identifiers for the items/nets fields.  Net
        # *names* are not available here (the rule is name-of-group
        # based, not name-of-net based) -- callers needing per-net
        # detail can correlate via ``group.net_ids`` + ``pcb.nets``
        # downstream.  We surface the group name in items[0] to give
        # downstream JSON consumers a stable handle.
        member_count = len(group.net_ids) + 2 * len(group.pair_ids)
        return DRCViolation(
            rule_id=self.rule_id,
            severity="error",
            message=(
                f"Match group {group.name!r} ({member_count} member"
                f"{'s' if member_count != 1 else ''}) length-skew "
                f"{skew_mm:.3f} mm exceeds tolerance {tolerance_mm:.3f} mm"
            ),
            location=None,
            layer=None,
            actual_value=round(skew_mm, 4),
            required_value=round(tolerance_mm, 4),
            items=(group.name,),
            nets=(),
        )
