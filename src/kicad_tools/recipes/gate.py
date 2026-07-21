"""Shared pipeline success gate for board recipes (issue #3912).

Every board recipe under ``boards/`` used to hand-roll its own success
gate: a ``route_pcb()`` bool, a ``run_drc()`` bool, and a ``main()`` that
computed the process exit code from an *ad-hoc* AND of *some subset* of
those bools, plus a separately-printed ``SUMMARY`` block.  Because the
exit-code expression and the SUMMARY were authored independently per board
they drifted -- and both drifted away from ground truth:

* **board-06** dropped the DRC leg entirely (``return 0 if route_success
  else 1`` while the SUMMARY printed ``DRC: FAIL``), so a board with 18
  differential-pair errors exited 0.
* **board-05** dropped route-completion (81% routed still exited 0) and
  gated its DRC leg on ``kct check --drc-only``, which evaluates the
  *stale* on-disk zone fills and therefore missed the 2 real copper shorts
  that ``kicad-cli pcb drc --refill-zones`` reports.

This module extracts the gold-standard gate (board-04, issues #3839 /
#4066) into ONE shared helper, :func:`evaluate_pipeline_gate`, that returns
a single :class:`PipelineGateResult` from which BOTH the SUMMARY and the
``main()`` exit code are derived.  They can no longer disagree.

Two complementary DRC engines
-----------------------------
The DRC leg's *authoritative core* is
:func:`kicad_tools.drc.geometric.run_geometric_drc`, which shells
``kicad-cli pcb drc --refill-zones`` (issue #3969) -- exactly the command
a fab house (and the operator's cross-gate process rule) uses.  It
re-fills the copper pours from scratch before evaluating clearance, so it
catches connectivity defects (``shorting_items``) that the internal
``kct check`` engine is structurally blind to when it trusts stale
persisted zone-fill polygons.

kicad-cli is, however, blind to a few *kct-internal* rule families that
have no KiCad-native expression -- notably the differential-pair and
match-group length-skew / continuity rules, which only fire under
``kct check --net-class-map``.  Board-06's 18 errors are exactly this
class.  So the helper accepts an optional caller-supplied
``supplemental_drc_ok`` verdict (the board's existing ``run_drc`` result)
and ANDs it into ``drc_ok``.  The union of the two engines is what makes
the gate catch board-05's shorts AND board-06's diffpair skew.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.drc.geometric import GeometricDRCResult, run_geometric_drc

__all__ = [
    "DEFAULT_ADVISORY_DRC_TYPES",
    "PipelineGateResult",
    "evaluate_pipeline_gate",
]


# kicad-cli error-severity violation ``type`` strings that the DRC leg
# treats as ADVISORY (surfaced but not gating), mirroring the audit
# pipeline's advisory-rule classification (issue #3074).
#
# ``unconnected_items`` is advisory here because route completeness is a
# SEPARATE, first-class leg of this gate (``route_ok``): an unrouted net
# shows up as ``unconnected_items`` under kicad-cli, and counting it as a
# blocking DRC error too would double-penalise the same fact.  A board that
# is legitimately PARTIAL declares a ``route_allowance`` on the route leg;
# it should not additionally trip the DRC leg for the same missing copper.
#
# Hard connectivity SHORTS (``shorting_items``) are deliberately NOT in
# this set -- shipping a shorted board is never safe, so a short always
# blocks (this is the board-05 defect the whole issue is about).
DEFAULT_ADVISORY_DRC_TYPES: frozenset[str] = frozenset({"unconnected_items"})


@dataclass(frozen=True)
class PipelineGateResult:
    """A single structured verdict for a board recipe's success gate.

    Both the recipe's printed ``SUMMARY`` block and its ``main()`` exit
    code MUST be derived from ONE instance of this class so they cannot
    diverge (issue #3912).  Use :meth:`summary_lines` for the SUMMARY and
    :meth:`exit_code` for the process exit code; both read the same
    :attr:`passed` property.

    Attributes:
        route_ok: ``True`` when the routed board is complete within its
            declared ``route_allowance`` (see :func:`evaluate_pipeline_gate`).
        drc_ok: ``True`` when neither the authoritative geometric DRC leg
            (``kicad-cli pcb drc --refill-zones``) nor the optional
            supplemental verdict reports a blocking error.
        lvs_ok: ``True``/``False`` for the copper-LVS verdict, or ``None``
            when LVS was not run for this board (an LVS-not-run board is
            NOT failed by the gate).
        nets_routed / nets_total / route_allowance: route-leg inputs,
            retained for reporting.  ``nets_*`` are ``None`` when the
            caller passed a pre-computed ``route_ok`` bool instead of
            counts.
        drc_ran: ``True`` when ``kicad-cli`` actually executed the
            geometric DRC.  ``False`` on every skip path (kicad-cli absent,
            timeout, crash); see ``require_drc``.
        drc_blocking: ``{type_str: count}`` of the geometric error-severity
            violations that EXCEEDED their allowance (i.e. the ones that
            gate).  Empty on a clean geometric run.
        drc_top_types: the most-frequent geometric error types (for logs).
        supplemental_drc_ok: the optional caller-supplied verdict (e.g. the
            board's ``kct check --net-class-map`` result for diffpair /
            match-group rules kicad-cli cannot express), or ``None`` when
            not provided.
        reasons: human-readable explanations for every failing / skipped
            leg, suitable for printing under the SUMMARY.
    """

    route_ok: bool
    drc_ok: bool
    lvs_ok: bool | None
    nets_routed: int | None = None
    nets_total: int | None = None
    route_allowance: int = 0
    drc_ran: bool = False
    drc_blocking: dict[str, int] = field(default_factory=dict)
    drc_top_types: list[tuple[str, int]] = field(default_factory=list)
    supplemental_drc_ok: bool | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Overall verdict: all gating legs pass.

        ``lvs_ok is not False`` treats ``None`` (LVS not run) as
        non-blocking, so an LVS-not-run board is not failed by the gate,
        while a genuine LVS failure (``False``) is.
        """
        return self.route_ok and self.drc_ok and (self.lvs_ok is not False)

    def exit_code(self) -> int:
        """Process exit code derived from :attr:`passed` (0 pass / 1 fail)."""
        return 0 if self.passed else 1

    def overall_status(self) -> str:
        """``"PASS"``/``"FAIL"`` derived from :attr:`passed`."""
        return "PASS" if self.passed else "FAIL"

    def route_status(self) -> str:
        """``"SUCCESS"``/``"PARTIAL"`` for the ``Routing:`` SUMMARY line."""
        return "SUCCESS" if self.route_ok else "PARTIAL"

    def drc_status(self) -> str:
        """``"PASS"``/``"FAIL"`` for the ``DRC:`` SUMMARY line."""
        return "PASS" if self.drc_ok else "FAIL"

    def lvs_status(self) -> str:
        """``"PASS"``/``"FAIL"``/``"n/a"`` for the ``LVS:`` SUMMARY line."""
        if self.lvs_ok is None:
            return "n/a"
        return "PASS" if self.lvs_ok else "FAIL"

    def summary_lines(self) -> list[str]:
        """Render the canonical ``Results:`` block for the recipe SUMMARY.

        Every line here is derived from this same result object, so the
        printed status and the :meth:`exit_code` cannot diverge -- the
        divergence-guard invariant issue #3912 requires.
        """
        lines = [
            f"  Routing: {self.route_status()}",
            f"  DRC:     {self.drc_status()}",
            f"  LVS:     {self.lvs_status()}",
            f"  Overall: {self.overall_status()}",
        ]
        for reason in self.reasons:
            lines.append(f"    - {reason}")
        return lines


def evaluate_pipeline_gate(
    routed_pcb: Path | str,
    *,
    nets_routed: int | None = None,
    nets_total: int | None = None,
    route_ok: bool | None = None,
    route_allowance: int = 0,
    lvs_ok: bool | None = None,
    advisory_types: Collection[str] = DEFAULT_ADVISORY_DRC_TYPES,
    rule_allowances: Mapping[str, int] | None = None,
    supplemental_drc_ok: bool | None = None,
    supplemental_reason: str = "",
    require_drc: bool = True,
    drc_timeout: int = 180,
    _drc_result: GeometricDRCResult | None = None,
) -> PipelineGateResult:
    """Evaluate a board recipe's success gate into one structured verdict.

    The DRC leg's authoritative engine is
    :func:`kicad_tools.drc.geometric.run_geometric_drc`, which runs
    ``kicad-cli pcb drc --refill-zones`` (issue #3969) -- NOT
    ``kct check --drc-only`` (which trusts stale on-disk zone fills and so
    misses real copper shorts, the board-05 defect this issue fixes).

    Route completion is a **first-class leg**: a board that is legitimately
    PARTIAL (e.g. board-07's seed-invariant nets #3438, board-06's USB3
    escape #2677) passes an explicit non-zero ``route_allowance`` rather
    than silently dropping the term from the exit code.

    Args:
        routed_pcb: Path to the routed ``.kicad_pcb`` to check.
        nets_routed: Number of signal nets fully routed.  When both this
            and ``nets_total`` are given, ``route_ok`` is computed as
            ``(nets_total - nets_routed) <= route_allowance``.
        nets_total: Total number of signal nets.
        route_ok: A pre-computed route verdict, used only when
            ``nets_routed``/``nets_total`` are not both supplied (for
            recipes whose ``route_pcb`` returns only a bool).
        route_allowance: Permitted per-board route shortfall (default 0 =
            fully routed).  Only meaningful with the counts path.
        lvs_ok: Copper-LVS verdict, or ``None`` when LVS was not run for
            this board (``None`` does not fail the gate).
        advisory_types: kicad-cli error ``type`` strings excluded from the
            geometric blocking count (defaults to
            :data:`DEFAULT_ADVISORY_DRC_TYPES`).
        rule_allowances: Per-``type_str`` grandfathered allowance for the
            geometric leg (e.g. ``{"hole_clearance": 2}`` for board-04's
            two legacy drill-clearance errors).  A type is blocking only
            when its count exceeds its allowance (default 0).
        supplemental_drc_ok: Optional additional DRC verdict ANDed into
            ``drc_ok`` -- for rule families kicad-cli cannot express
            (differential-pair / match-group skew via
            ``kct check --net-class-map``).  ``None`` means "not supplied"
            and never fails the gate.
        supplemental_reason: Message recorded when ``supplemental_drc_ok``
            is ``False``.
        require_drc: When ``True`` (default) a geometric DRC that did NOT
            run (kicad-cli absent/timeout/crash) fails ``drc_ok`` -- the
            gate refuses to certify a board it could not authoritatively
            check.  Set ``False`` only for environments that legitimately
            lack kicad-cli and accept an unverified DRC leg.
        drc_timeout: Seconds before the kicad-cli DRC run is abandoned.
        _drc_result: Test seam -- inject a pre-built
            :class:`GeometricDRCResult` instead of shelling kicad-cli.

    Returns:
        A :class:`PipelineGateResult` whose :meth:`~PipelineGateResult.passed`
        drives both the SUMMARY and the exit code.
    """
    routed_pcb = Path(routed_pcb)
    allowances = dict(rule_allowances or {})
    advisory = frozenset(advisory_types)
    reasons: list[str] = []

    # ---- Route leg -------------------------------------------------------
    if nets_routed is not None and nets_total is not None:
        shortfall = nets_total - nets_routed
        route_ok_final = shortfall <= route_allowance
        if not route_ok_final:
            reasons.append(
                f"route incomplete: {nets_routed}/{nets_total} nets "
                f"({shortfall} short, allowance {route_allowance})"
            )
    elif route_ok is not None:
        route_ok_final = route_ok
        if not route_ok_final:
            reasons.append("route incomplete (recipe route_pcb returned partial)")
    else:
        # No route information supplied: do not block on a leg we cannot
        # evaluate, but record that it was not checked.
        route_ok_final = True
        reasons.append("route completion not evaluated (no counts or route_ok supplied)")

    # ---- DRC leg (authoritative: kicad-cli pcb drc --refill-zones) -------
    drc = (
        _drc_result
        if _drc_result is not None
        else run_geometric_drc(routed_pcb, timeout=drc_timeout)
    )

    drc_blocking: dict[str, int] = {}
    if not drc.ran:
        if require_drc:
            geometric_ok = False
            reasons.append(
                f"geometric DRC did not run ({drc.note or drc.reason}); "
                "cannot certify board clean (require_drc=True)"
            )
        else:
            geometric_ok = True
            reasons.append(
                f"geometric DRC skipped ({drc.note or drc.reason}); "
                "DRC leg unverified (require_drc=False)"
            )
    else:
        for type_str, count in drc.by_type.items():
            if type_str in advisory:
                continue
            if count > allowances.get(type_str, 0):
                drc_blocking[type_str] = count
        geometric_ok = not drc_blocking
        if drc_blocking:
            detail = ", ".join(f"{t}={c}" for t, c in sorted(drc_blocking.items()))
            reasons.append(f"geometric DRC blocking errors (kicad-cli --refill-zones): {detail}")

    drc_ok = geometric_ok and (supplemental_drc_ok is not False)
    if supplemental_drc_ok is False:
        reasons.append(
            supplemental_reason
            or "supplemental DRC verdict failed (kct check rule families kicad-cli cannot express)"
        )

    # ---- LVS leg ---------------------------------------------------------
    if lvs_ok is False:
        reasons.append("copper-LVS failed (short/open vs schematic)")

    return PipelineGateResult(
        route_ok=route_ok_final,
        drc_ok=drc_ok,
        lvs_ok=lvs_ok,
        nets_routed=nets_routed,
        nets_total=nets_total,
        route_allowance=route_allowance,
        drc_ran=drc.ran,
        drc_blocking=drc_blocking,
        drc_top_types=drc.top_types(),
        supplemental_drc_ok=supplemental_drc_ok,
        reasons=reasons,
    )
