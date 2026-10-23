"""Differential-pair length-skew DRC rule.

Validates that an *engaged* differential pair's routed-length skew
(``|L_p - L_n|``) does not exceed the per-class ``skew_tolerance_mm``
floor.  An engaged pair (per Epic #2556 Phase 2E, #2638) is one whose
net class has ``coupled_routing == True`` and which passed the
engagement-layer single-ended refusal check; un-engaged pairs are
intentionally not checked.

The rule is part of Epic #2556 Phase 3J (Issue #2649):

- Phase 3H (#2647, merged): added the
  :class:`~kicad_tools.router.diffpair_length.DiffPairLengthTracker`
  which produces ``get_all_skews()`` -> ``{(p_net_name, n_net_name) ->
  skew_mm}``, plus the per-class accessor
  :meth:`~kicad_tools.router.rules.NetClassRouting.effective_skew_tolerance`
  (default 0.5 mm).
- Phase 3I (#2648, parallel): inserts serpentine traces to *fix*
  out-of-tolerance pairs.  Phase 3J is independent of Phase 3I --
  this rule fires on routed-as-found geometry regardless of whether
  serpentine tuning ran (the validator-for-externally-routed-boards
  case explicitly in scope).
- Phase 3J (this rule): the DRC rule that fires when a routed pair's
  skew exceeds its per-class tolerance.

Dependency injection contract
=============================

Mirroring the
:class:`~kicad_tools.validate.rules.diffpair_routing_continuity.DiffPairRoutingContinuityRule`
pattern (Issue #2640), the rule **consumes** skew data from the caller;
it does NOT re-derive it.  This decouples the rule from
:class:`~kicad_tools.router.diffpair_length.DiffPairLengthTracker`
internals and prevents the #2521-class drift failure mode where the
router and DRC compute the same quantity by different code paths and
silently diverge.

The intended call site is the autorouter consumer (after the per-pair
length tracker has run ``record_routes``).  The DRC rule itself does
NOT import :class:`DiffPairLengthTracker`, does NOT call
:func:`~kicad_tools.router.diffpair.should_engage_coupled`, and does
NOT call :func:`~kicad_tools.router.diffpair_detection.detect_diff_pairs`
-- it consumes whatever the caller provides.

Graceful degradation
====================

When ``skew_data`` is ``None`` or empty (e.g., running ``kct check``
standalone with no router context), the rule is a conservative no-op
(``rules_checked == 0``, no violations).  This matches the standalone-
``kct check`` graceful-no-op contract used by
``diffpair_routing_continuity`` and is the explicit
"validator-for-externally-routed-boards" case from the issue body: a
board routed by Freerouting (no ``kct route`` involvement, no Phase 3H
tracker context) MUST NOT have spurious skew violations -- because the
caller had no way to compute the skew.  The rule fires only when length
data was deliberately injected.

Scope (out of scope -- explicitly):

- Computing the skew (Phase 3H's domain).
- Fixing the skew (Phase 3I's domain -- this rule does NOT consult any
  ``serpentine_inserted`` flag; it fires on routed-as-found geometry).
- Cross-pair lane-matching skew (future ``diffpair_lane_skew`` rule).

Time domain (Issue #7892)
=========================

When the caller supplies ``measurements``
(:func:`~kicad_tools.validate.diffpair_skew.derive_pair_skews`), a pair is
judged in **picoseconds**: the difference of its legs' propagation delays,
each trimmed track run at its own layer's delay and each via barrel at the
via delay, against the derived budget of
:func:`kicad_tools.physics.skew_budget.resolve_skew_budget`.  The finding
reports the skew in ps, the budget, and the budget's millimetre form at the
slowest layer.  A pair whose class still sets ``skew_tolerance_mm`` is also
held to that length cap (until #7894 removes those class fields).  Pairs with
no derivable budget keep the length check below.
- Modifying the sibling ``diffpair_routing_continuity`` or
  ``diffpair_clearance_intra`` rules (orthogonal concerns).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from collections.abc import Sequence

    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.diffpair_skew import ChainSkew, PairSkewMeasurement


# Module-level default skew tolerance in mm.  MUST equal the default arg
# of :meth:`NetClassRouting.effective_skew_tolerance` (Phase 3H).  The
# drift-prevention test in
# ``tests/test_validate_diffpair_length_skew.py`` imports both and
# asserts byte-for-byte equality -- if a future change moves one but
# not the other this test fires.
#
# Calibration (mirrors the curator note on #2649):
#   USB 3.0 / PCIe Gen 2+ : ~0.5-1 mm budget
#   MIPI D-PHY             : ~1 mm budget
#   DDR4 DQ-strobe         : ~0.5 mm budget
#   USB 2.0 FS / HS        : 2.5 mm / 1.27 mm -- declare ``pair_protocol``
#                            (Issue #6443, ``router/pair_protocols.py``)
#
# 0.5 mm is a sane middle ground that fires on egregious failures while
# not nagging on routine USB HS work.  Tighter classes (PCIe Gen 3+,
# DDR5) MUST set ``NetClassRouting.skew_tolerance_mm`` explicitly.
DEFAULT_SKEW_TOLERANCE_MM = 0.5

# Float-comparison slack for the skew-vs-tolerance test.  A pair routed to
# *exactly* the tolerance (e.g. skew 0.500 mm against a 0.500 mm budget)
# can measure as 0.5000000001 mm after summing many segment lengths in
# floating point, which would spuriously fire ``skew_mm > tolerance_mm``.
# We treat any skew within this epsilon of the tolerance as in-bounds
# (issue #3543: "exact-tolerance rounding 0.501 vs 0.500").  1 micron is
# far below fab resolution (typical min trace/space ~0.1 mm) so this never
# masks a real, manufacturable over-skew.
SKEW_TOLERANCE_EPSILON_MM = 1e-6


def _normalize_name_pair(a: str, b: str) -> tuple[str, str]:
    """Return the lexicographically-sorted (low, high) name tuple."""
    return (a, b) if a <= b else (b, a)


def budget_exceeded(m: PairSkewMeasurement) -> bool:
    """Whether the pair's time skew breaks its derived budget (Issue #7914)."""
    from kicad_tools.physics.skew_budget import SKEW_BUDGET_EPSILON_PS

    budget = m.limits.budget
    return (
        budget is not None
        and m.skew_ps is not None
        and m.skew_ps > budget.budget_ps + SKEW_BUDGET_EPSILON_PS
    )


def length_cap_exceeded(m: PairSkewMeasurement) -> bool:
    """Whether the pair's length skew breaks an active length cap (Issue #7914)."""
    cap = m.limits.length_cap_mm
    return cap is not None and m.skew_mm > cap + SKEW_TOLERANCE_EPSILON_MM


def measured_skew_verdict(m: PairSkewMeasurement) -> str | None:
    """The verdict ``kct check`` reaches for one measured pair (Issue #7914).

    ``fail`` / ``warn`` / ``pass`` -- the worse of the budget and the length
    cap, each at its own severity -- or ``None`` when neither limit applies.
    ``DiffPairLengthSkewRule._check_measured`` judges with the same two
    predicates, so the research bench has no second resolution.
    """
    limits = m.limits
    judged = (
        limits.budget is not None and m.skew_ps is not None
    ) or limits.length_cap_mm is not None
    if not judged:
        return None
    severities = []
    if budget_exceeded(m):
        severities.append(limits.budget.severity)
    if length_cap_exceeded(m):
        severities.append(limits.length_severity)
    if not severities:
        return "pass"
    return "fail" if "error" in severities else "warn"


class DiffPairLengthSkewRule(DRCRule):
    """Validate length-match skew for engaged diff pairs.

    The rule consumes caller-supplied ``skew_data`` (the per-pair skews
    computed by Phase 3H's
    :meth:`~kicad_tools.router.diffpair_length.DiffPairLengthTracker.get_all_skews`)
    and an ``engaged_pairs`` set (the producer-side filter from
    :func:`~kicad_tools.router.diffpair.should_engage_coupled` -- Phase
    2E #2638).  A violation fires when, for an engaged pair,
    ``skew_mm > tolerance``; pairs not in ``engaged_pairs`` are
    deliberately not checked (the rule applies only to pairs the
    designer opted into coupled routing for).

    Attributes:
        rule_id: ``"diffpair_length_skew"`` -- MUST exactly match the
            alias-table key in :mod:`kicad_tools.drc.violation`.
        skew_data: ``{(p_net_name, n_net_name) -> skew_mm}`` map.  Keys
            are normalized to a lexicographically-sorted name tuple
            internally so caller order does not matter.  ``None`` or
            empty -> the rule is a no-op (graceful degradation when no
            router context is available, e.g., ``kct check``
            standalone).
        engaged_pairs: ``{(min_net_id, max_net_id)}`` set of pairs to
            validate.  Pairs whose net-ids (resolved via ``pcb.nets``)
            are not in this set are not checked.  ``None`` is
            equivalent to an empty set (rule is a no-op).
        threshold_map: ``{(min_net_id, max_net_id) -> tolerance_mm}``
            map of per-pair tolerance overrides.  Pairs missing from
            the map use the constructor's ``default_tolerance_mm``.
        default_tolerance_mm: Fallback tolerance (mm) when no per-pair
            override is provided.  Defaults to
            :data:`DEFAULT_SKEW_TOLERANCE_MM` (0.5).
    """

    rule_id = "diffpair_length_skew"
    name = "Differential-Pair Length Skew"
    description = (
        "Validates that engaged differential pairs have a propagation-delay "
        "skew within their derived budget (ps) and a routed-length skew "
        "(|L_p - L_n|) at or below any length tolerance that applies."
    )

    def __init__(
        self,
        skew_data: dict[tuple[str, str], float] | None = None,
        engaged_pairs: set[tuple[int, int]] | None = None,
        threshold_map: dict[tuple[int, int], float] | None = None,
        default_tolerance_mm: float = DEFAULT_SKEW_TOLERANCE_MM,
        emit_info: bool = False,
        severity_map: dict[tuple[int, int], str] | None = None,
        source_map: dict[tuple[int, int], str] | None = None,
        chains: Sequence[ChainSkew] | None = None,
        measurements: Sequence[PairSkewMeasurement] | None = None,
    ) -> None:
        """Initialize the rule.

        Args:
            skew_data: ``{(p_net_name, n_net_name) -> skew_mm}`` from
                Phase 3H's
                :meth:`DiffPairLengthTracker.get_all_skews`.  Keys are
                normalized to a sorted-by-name tuple internally so the
                caller does not have to maintain a P/N ordering
                convention.  ``None`` or empty -> the rule is a no-op
                (graceful degradation when no router context is
                available).
            engaged_pairs: ``{(min_net_id, max_net_id)}`` set of pairs
                to validate (each tuple's order is normalized to
                ``(min, max)``).  Pairs whose net-ids (resolved via
                ``pcb.nets``) are not in this set are not checked.
                ``None`` or empty -> the rule is a no-op.
            threshold_map: Optional per-pair tolerance overrides (mm).
                Keys are normalized to ``(min, max)`` net-id tuples.
                Missing pairs use ``default_tolerance_mm``.
            default_tolerance_mm: Fallback when no per-pair tolerance
                is set.  Defaults to
                :data:`DEFAULT_SKEW_TOLERANCE_MM` (0.5).
            emit_info: When True, emit an advisory ``info``-severity
                :class:`DRCViolation` for each engaged pair that *passes*
                its skew tolerance, carrying the measured skew.  Lets
                ``kct check --verbose`` surface per-pair measurements even
                on a clean board (Issue #3917 AC5).  Defaults to False
                (no advisory noise on the default, non-verbose path).
            severity_map: Optional per-pair severity of an over-skew
                finding (``"error"`` or ``"warning"``), keyed like
                ``threshold_map``.  Missing pairs are errors.  A limit
                taken from an ``uncited`` protocol default is a warning
                (Issue #6443): a number nobody sourced must not gate.
            source_map: Optional per-pair provenance of the limit (e.g.
                ``"protocol:USB2_FS (Renesas ...)"``), appended to the
                finding's message.
            chains: Optional chain (XNet) skew measurements (Issue #6447),
                from :func:`~kicad_tools.validate.diffpair_skew.derive_chain_skew`.
                Each chain is reported under this rule as an ``info``
                metric when ``emit_info`` is set, and as an ``error`` when
                its class declares ``chain_skew_tolerance_mm`` and the
                end-to-end skew exceeds it.
            measurements: Issue #7892 -- pairs measured in length and time
                with their resolved limits
                (:func:`~kicad_tools.validate.diffpair_skew.derive_pair_skews`).
                A measured pair is judged against its derived time budget
                (ps) and, where one applies, its length cap (mm); it ignores
                ``skew_data`` / ``threshold_map`` for that pair.
        """
        self._chains = list(chains or [])
        self._measurements: dict[tuple[str, str], PairSkewMeasurement] = {
            _normalize_name_pair(m.p_name, m.n_name): m for m in (measurements or [])
        }
        # Normalize skew_data keys -- name tuples sorted lexicographically
        # so the caller does not have to maintain P/N ordering.
        self._skew_data: dict[tuple[str, str], float] = {}
        if skew_data:
            for (a, b), skew in skew_data.items():
                self._skew_data[_normalize_name_pair(a, b)] = skew

        # Normalize engaged_pairs ordering so callers don't have to.
        self._engaged: set[tuple[int, int]] = set()
        if engaged_pairs:
            for a, b in engaged_pairs:
                self._engaged.add((a, b) if a <= b else (b, a))

        self._threshold_map: dict[tuple[int, int], float] = {}
        if threshold_map:
            for (a, b), thr in threshold_map.items():
                key = (a, b) if a <= b else (b, a)
                self._threshold_map[key] = thr

        self._default_tolerance_mm = default_tolerance_mm
        self._emit_info = emit_info

        def _norm(src: dict[tuple[int, int], str] | None) -> dict[tuple[int, int], str]:
            return {((a, b) if a <= b else (b, a)): v for (a, b), v in (src or {}).items()}

        self._severity_map = _norm(severity_map)
        self._source_map = _norm(source_map)

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,  # noqa: ARG002 (unused; signature mirrors peer rules)
    ) -> DRCResults:
        """Check length-skew for all engaged pairs with recorded skew.

        Args:
            pcb: The PCB to check.  Consulted only for ``pcb.nets`` to
                resolve net names to ids.
            design_rules: Active manufacturer design rules.  Unused by
                this rule -- the tolerance is per-pair-explicit -- but
                kept in the signature for consistency with other
                :class:`DRCRule` subclasses.

        Returns:
            :class:`DRCResults` with one violation per engaged pair
            whose recorded skew exceeds its tolerance.  Empty result
            when ``skew_data`` is empty OR ``engaged_pairs`` is empty
            (graceful no-op; matches the standalone-``kct check``
            contract).
        """
        results = DRCResults()

        # Graceful degradation: empty either input -> no-op.  This is
        # the explicit "Freerouting / external router" case from the
        # issue body -- without a tracker context to populate skew_data
        # AND a producer-side engagement set, the rule has nothing to
        # validate.
        self._check_chains(results)
        if (not self._skew_data and not self._measurements) or not self._engaged:
            return results

        # Build a {net_name -> net_id} lookup for resolving skew_data
        # name tuples to engaged_pairs id tuples.
        name_to_id: dict[str, int] = {}
        for net in pcb.nets.values():
            if net.name:
                name_to_id[net.name] = net.number

        # Iterate the (sorted) skew_data entries deterministically so
        # violations appear in a stable order across runs.
        entries = dict(self._skew_data)
        for names, m in self._measurements.items():
            entries[names] = m.skew_mm
        for (name_a, name_b), skew_mm in sorted(entries.items()):
            id_a = name_to_id.get(name_a)
            id_b = name_to_id.get(name_b)
            if id_a is None or id_b is None:
                # Name not in the PCB's net table -- the caller passed
                # a skew entry for a net the PCB doesn't know about.
                # Conservative: skip silently rather than fire a
                # spurious violation.  (A future enhancement could log
                # at debug level.)
                continue

            key = (id_a, id_b) if id_a <= id_b else (id_b, id_a)
            if key not in self._engaged:
                # Pair is recorded but not engaged -- per the rule's
                # engagement-gating contract, do not check.  Matches
                # the diffpair_routing_continuity rule's behaviour
                # exactly.
                continue

            # This pair counts toward rules_checked (one check per
            # engaged pair with measured skew).  Phase 4N (#2660): also
            # bump the per-rule counter so the CI gate can confirm the
            # rule was exercised on at least one pair.
            results.rules_checked += 1
            results.rules_checked_by_rule["diffpair_length_skew"] = (
                results.rules_checked_by_rule.get("diffpair_length_skew", 0) + 1
            )

            measured = self._measurements.get((name_a, name_b))
            if measured is not None:
                self._check_measured(results, measured)
                continue

            tolerance_mm = self._threshold_map.get(key, self._default_tolerance_mm)
            if skew_mm > tolerance_mm + SKEW_TOLERANCE_EPSILON_MM:
                results.add(
                    self._make_violation(
                        name_a=name_a,
                        name_b=name_b,
                        skew_mm=skew_mm,
                        tolerance_mm=tolerance_mm,
                        severity=self._severity_map.get(key, "error"),
                        source=self._source_map.get(key),
                    )
                )
            elif self._emit_info:
                # Passing pair -- surface the measured skew as an advisory
                # ``info`` finding so ``kct check --verbose`` reports the
                # per-pair measurement even on a clean board (Issue #3917).
                results.add(
                    self._make_info(
                        name_a=name_a,
                        name_b=name_b,
                        skew_mm=skew_mm,
                        tolerance_mm=tolerance_mm,
                        source=self._source_map.get(key),
                    )
                )

        return results

    def _check_measured(self, results: DRCResults, m: PairSkewMeasurement) -> None:
        """Judge one pair against its time budget and length cap (Issue #7892)."""
        first, second = sorted([m.p_name, m.n_name])
        limits = m.limits
        budget = limits.budget
        findings: list[DRCViolation] = []
        passes: list[str] = []
        if budget is not None and m.skew_ps is not None:
            eq = limits.budget_mm_equivalent
            eq_text = f" = {eq:.3f} mm at the slowest layer" if eq is not None else ""
            text = (
                f"propagation-delay skew {m.skew_ps:.2f} ps (length skew {m.skew_mm:.3f} mm) "
                f"{{verdict}} budget {budget.budget_ps:.2f} ps{eq_text} [{budget.provenance}]"
            )
            if budget_exceeded(m):
                kct_only = m.skew_mm <= limits.kicad_skew_mm + SKEW_TOLERANCE_EPSILON_MM
                note = (
                    " -- kct-only: kicad-cli checks length, and this pair's legs run on "
                    "layers of different delay"
                    if kct_only
                    else ""
                )
                findings.append(
                    DRCViolation(
                        rule_id=self.rule_id,
                        severity=budget.severity,
                        message=f"Engaged differential pair {first}/{second} "
                        + text.format(verdict="exceeds")
                        + note,
                        location=None,
                        layer=None,
                        actual_value=round(m.skew_ps, 3),
                        required_value=round(budget.budget_ps, 3),
                        items=(first, second),
                        nets=(m.p_name, m.n_name),
                    )
                )
            else:
                passes.append(text.format(verdict="within"))
        if limits.length_cap_mm is not None:
            source = f" [{limits.length_source}]" if limits.length_source else ""
            if length_cap_exceeded(m):
                findings.append(
                    self._make_violation(
                        name_a=m.p_name,
                        name_b=m.n_name,
                        skew_mm=m.skew_mm,
                        tolerance_mm=limits.length_cap_mm,
                        severity=limits.length_severity,
                        source=limits.length_source or None,
                    )
                )
            else:
                passes.append(
                    f"length-skew {m.skew_mm:.3f} mm within tolerance "
                    f"{limits.length_cap_mm:.3f} mm{source}"
                )
        for finding in findings:
            results.add(finding)
        if self._emit_info and passes and not findings:
            results.add(
                DRCViolation(
                    rule_id=self.rule_id,
                    severity="info",
                    message=f"Engaged differential pair {first}/{second} " + "; ".join(passes),
                    location=None,
                    layer=None,
                    actual_value=round(m.skew_ps if m.skew_ps is not None else m.skew_mm, 4),
                    required_value=None,
                    items=(),
                    nets=(first, second),
                )
            )

    def _check_chains(self, results: DRCResults) -> None:
        """Report each chain's end-to-end skew (Issue #6447)."""
        for chain in self._chains:
            label = " -> ".join(f"{p}/{n}" for p, n in chain.segments)
            nets = tuple(net for seg in chain.segments for net in seg)
            skew = chain.skew_mm
            over = chain.limit_mm is not None and skew > chain.limit_mm + SKEW_TOLERANCE_EPSILON_MM
            if chain.limit_mm is not None:
                results.rules_checked += 1
                results.rules_checked_by_rule["diffpair_length_skew"] = (
                    results.rules_checked_by_rule.get("diffpair_length_skew", 0) + 1
                )
            if not over and not self._emit_info:
                continue
            limit = (
                f"exceeds chain tolerance {chain.limit_mm:.3f} mm"
                if over
                else (
                    "chain skew (no chain limit declared)"
                    if chain.limit_mm is None
                    else f"within chain tolerance {chain.limit_mm:.3f} mm"
                )
            )
            results.add(
                DRCViolation(
                    rule_id=self.rule_id,
                    severity="error" if over else "info",
                    message=f"Differential-pair chain {label} end-to-end length-skew "
                    f"{skew:.3f} mm {limit}",
                    location=None,
                    layer=None,
                    actual_value=round(skew, 4),
                    required_value=round(chain.limit_mm, 4) if chain.limit_mm is not None else None,
                    items=(),
                    nets=nets,
                )
            )

    def _make_info(
        self,
        name_a: str,
        name_b: str,
        skew_mm: float,
        tolerance_mm: float,
        source: str | None = None,
    ) -> DRCViolation:
        """Build an ``info`` DRCViolation for a passing engaged pair."""
        first, second = sorted([name_a, name_b])
        suffix = f" [{source}]" if source else ""
        return DRCViolation(
            rule_id=self.rule_id,
            severity="info",
            message=(
                f"Engaged differential pair {first}/{second} length-skew "
                f"{skew_mm:.3f} mm within tolerance {tolerance_mm:.3f} mm{suffix}"
            ),
            location=None,
            layer=None,
            actual_value=round(skew_mm, 4),
            required_value=round(tolerance_mm, 4),
            items=(),
            nets=(first, second),
        )

    def _make_violation(
        self,
        name_a: str,
        name_b: str,
        skew_mm: float,
        tolerance_mm: float,
        severity: str = "error",
        source: str | None = None,
    ) -> DRCViolation:
        """Build a DRCViolation for an over-skew engaged pair."""
        # Stable lexicographic naming so reports don't flap.
        first, second = sorted([name_a, name_b])
        suffix = f" [{source}]" if source else ""
        return DRCViolation(
            rule_id=self.rule_id,
            severity=severity,
            message=(
                f"Engaged differential pair {first}/{second} length-skew "
                f"{skew_mm:.3f} mm exceeds tolerance {tolerance_mm:.3f} mm{suffix}"
            ),
            location=None,
            layer=None,
            actual_value=round(skew_mm, 4),
            required_value=round(tolerance_mm, 4),
            items=(first, second),
            nets=(name_a, name_b),
        )
