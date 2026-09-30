"""Unified clearance-rule resolver (Epic #5509, Phase 2).

Phase 1 extracted the exact-geometry clearance kernel and built the
``kicad-cli`` conformance oracle.  The kernel answers *"how far apart is this
copper"* and *"is that at least ``required_mm``"* -- it carries no rule values
at all.  **This module owns ``required_mm``.**

Deliberately no import of, and no reference by name to, the kernel module:
nothing here measures geometry, and Phase 1's parity suite holds a "no
consumer switched to the kernel" gate -- a *textual* one -- until Phase 3 and
Phase 4 move consumers over one at a time.

Why it exists
-------------
Before Phase 2 the answer to "which clearance applies to this pair" was
re-derived independently in at least four places:

* ``cli/route_cmd.py:_decide_route_clearance`` -- the ``kct route`` precedence
  chain (#4875): explicit ``--clearance`` > explicit ``--manufacturer`` >
  legacy board ``(net_class …)`` clamped to the fab floor > a flat default.
  It read **only** the legacy top-level ``(net_class …)`` block of the
  ``.kicad_pcb``, which KiCad 6+ does not write -- so a modern board's
  ``.kicad_pro`` board minimum and its ``.kicad_dru`` rules were invisible
  to it.
* :class:`~kicad_tools.router.rules.DesignRules` -- the scalar
  ``trace_clearance`` / ``via_clearance`` pair, plus ``component_clearances``
  and per-class :class:`~kicad_tools.router.rules.NetClassRouting.clearance`.
* :func:`kicad_tools.router.mfr_limits.resolve_clearance` -- the fab-tier
  floor, consulted by the CLI but by no other consumer.
* :meth:`~kicad_tools.router.pairwise_clearance.PairwiseClearanceTable.required_clearance`
  -- the HV/creepage net-pair matrix (#4431), resolved in complete isolation
  from every layer above.

That is the shape of the defect the epic names first: on the ``#5398`` board
the project requires ``0.20`` mm, ``kicad-cli pcb drc`` measures against
``0.20`` mm, and the route resolver returned ``0.15`` mm -- so the router
happily produced 0.16-0.20 mm gaps and the final DRC gate rejected 76 of
them, every one of them a track-vs-via record where the two consumers also
disagreed about whether ``trace_clearance`` or ``via_clearance`` applied
(step 9 below).

The project netclass, and why it took two steps (#5654)
-------------------------------------------------------
``.kicad_pro`` ``net_settings.classes[].clearance`` is the value KiCad's own
``clearance`` DRC test measures a copper-to-copper violation against, so a
board routed below it fails its own gate.  Phase 2 nonetheless shipped
without reading it: KiCad's stock project template ships ``Default`` at
``0.20`` mm and every project this repo wrote inherited it verbatim, making
the field a *template default* rather than a statement about the board.
Honouring it then would have re-spaced the whole demo fleet for no DRC
benefit -- measured on board 04 at ``--mfr jlcpcb-tier1``, the post-route
violation count moved from 24 to 28 while connecting the same 9/9 nets.

#5654 closed that gap from the writer end first: ``core/project_file.py``
now emits ``DEFAULT_NETCLASS_CLEARANCE_MM`` (the ``kct route`` target the
board is actually routed at, ``0.15`` mm) rather than the stock ``0.20`` mm,
and the manufacturer-profile rewrite path has always emitted the fab tier's
own floor (``manufacturers/project_generator.build_default_netclass``).  With
the field carrying information, it is read here -- as a **floor**, on exactly
the same "raise, never replace" footing as ``rules.min_clearance``
(:attr:`DeclaredClearanceRules.net_class_clearance_mm`), which is what keeps
a board that declares *less* than the router's target from being loosened.

What is still deliberately **not** read: ``.kicad_dru`` rules carrying a
``(condition …)`` (see :attr:`DeclaredClearanceRules.dru_mm`), and KiCad's
``physical_clearance`` constraint, which is a different rule family.

Precedence
----------
:func:`resolve_base_clearance` composes the board-wide base in one
deterministic order.  ``target_mm`` is the router's own spacing target
(``kct route``'s ``--clearance``, default 0.15 mm):

1. **Explicit operator target wins outright.**  An explicit ``--clearance``
   is authoritative and is never moved -- the operator opted in, exactly as
   #4875 defined it.  A warning is surfaced when it undercuts a declared
   rule; the value is not changed.
2. A legacy ``.kicad_pcb`` top-level ``(net_class …)`` clearance **replaces**
   the target, in both directions.  This is #4875's rule, unchanged: a board
   that carries that node states the spacing it was designed at.  KiCad 6+
   never writes it, so only pre-6 imports reach this layer.
3. The **project-derived minima** then apply as ``max`` floors, never as
   replacements: the board's ``.kicad_dru`` unconditional
   ``(constraint clearance (min …))``, the ``.kicad_pro`` board minimum
   (``design_settings.rules.min_clearance``, or the ``defaults.clearance_min``
   KiCad writes alongside it), and the applied ``.kicad_pro`` netclass
   (``net_settings.classes[].clearance``, added by #5654 -- see "The project
   netclass" above).  The first two layers are new in Phase 2; all three are
   *minima* in KiCad's own model, which is why they can only raise the
   requirement: flooring rather than replacing is what keeps a board whose
   project declares **less** than the router's target from being silently
   loosened, which would be the #5398 divergence in the other direction.
   This is the layer that fixes #5398 -- the board declares ``0.20`` there
   and the pre-Phase-2 resolver, which read the ``.kicad_pcb`` alone,
   returned ``0.15``.
4. The **fab-tier floor**
   (:func:`~kicad_tools.router.mfr_limits.resolve_clearance`) is the last
   ``max``: it is a hard minimum at the fab, so nothing can undercut it.  A
   declared value below it is raised *and* warned about, never honoured
   silently (#4875's "ambiguity #2" decision, preserved).
5. With nothing declared, ``target_mm`` stands -- byte-identical to the
   pre-#4875 path.
6. An explicit ``--manufacturer`` selects which fab floor applies and is
   named as the provenance of that term, but -- unlike #4875, where it
   suppressed the board's declared rules entirely -- it no longer waives a
   declared requirement.  Waiving it is what produced #5398: copper routed
   at 0.15 mm on a board whose own rules require 0.20 mm fails that board's
   DRC no matter which profile the operator named.  #4875 chose the
   suppression explicitly, as a blast-radius shield ("no invocation that
   names a manufacturer … changes its clearance because of this issue") --
   which is precisely the per-invocation divergence this phase exists to
   remove.  ``--clearance`` remains the one deliberate waiver.

:meth:`ClearanceResolver.resolve` then applies the **pair-level** layers on
top of that base, each a strictly-widening ``max`` and each symmetric in its
two arguments by construction:

7. **Per-net-class overrides** -- ``max`` over the two nets'
   :attr:`~kicad_tools.router.rules.NetClassRouting.clearance` values.
8. **Pair matrix** -- :meth:`PairwiseClearanceTable.required_clearance`,
   whose keys are already order-independent (sorted, ``/``-stripped).
9. **Trace/via symmetry** -- when *either* side of the pair is a via, the
   requirement rises to ``via_clearance``.  This is the asymmetry #5398
   recorded: ``kct route`` builds its ``DesignRules`` with
   ``trace_clearance`` set from ``route_cmd.DEFAULT_ROUTE_CLEARANCE_MM``
   (0.15mm) while ``via_clearance`` keeps the dataclass default of 0.20mm
   (:mod:`kicad_tools.router.rules` ships *both* fields at 0.2 -- the 0.15
   never comes from the dataclass), and consumers disagreed about which
   applied to a trace-vs-via pair depending on which object they were called
   *about*.  Here it is a property of the unordered pair, so it cannot depend
   on argument order.

Order symmetry
--------------
``resolve(a, b) == resolve(b, a)`` is guaranteed twice over: every term above
is a symmetric function of the unordered pair, *and*
:meth:`ClearanceQuery.canonical` sorts the two sides before any term is
evaluated.  ``tests/router/test_clearance_resolver.py`` asserts both orders
agree over a parametrised matrix, including the ``#5398`` fixture.

Scope (Phase 2)
---------------
This module resolves thresholds; it does not change any.  No value in
``mfr_limits.py``, no ``rules.py`` constant and no board ``.kicad_dru`` is
modified by this phase.  Search-time consumers (grid halos, A* acceptance)
are Phase 3; post-route consumers (optimizer, ``kct check``) are Phase 4 --
they keep their own arithmetic for now and are moved onto
:meth:`ClearanceResolver.required_mm` there.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .pairwise_clearance import PairwiseClearanceTable
    from .rules import NetClassRouting

__all__ = [
    "ClearanceQuery",
    "ClearanceResolver",
    "ClearanceRuleSet",
    "CopperKind",
    "DeclaredClearanceRules",
    "ResolvedClearance",
    "RuleSource",
    "read_declared_clearance_rules",
    "resolve_base_clearance",
    "strictest_net_class_clearance",
]


class CopperKind(Enum):
    """The copper object kinds a clearance requirement can be resolved for.

    Deliberately coarser than the Phase 1 kernel's five shape classes: rule
    selection distinguishes only the kinds KiCad's own rule model does.
    ``EDGE`` is present so a caller can *name* a
    copper-to-board-edge query, but edge clearance is a separate constraint
    (:func:`~kicad_tools.router.mfr_limits.resolve_edge_clearance`) and is
    not resolved here -- see :meth:`ClearanceResolver.resolve`.
    """

    TRACE = "trace"
    VIA = "via"
    PAD = "pad"
    ZONE = "zone"
    EDGE = "edge"


class RuleSource(Enum):
    """Which precedence layer produced a resolved value.

    Declaration order is the deterministic tie-break when two layers report
    the same number: the earlier member wins, so provenance never flips
    between runs on an exact tie.
    """

    EXPLICIT_TARGET = "explicit-target"
    PROJECT_DRU = "project-dru"
    BOARD_NET_CLASS = "board-net-class"
    PROJECT_MIN_CLEARANCE = "project-min-clearance"
    PROJECT_NET_CLASS = "project-net-class"
    FAB_FLOOR = "fab-floor"
    MANUFACTURER_OVERRIDE = "manufacturer-override"
    TARGET_DEFAULT = "target-default"
    NET_CLASS_OVERRIDE = "net-class-override"
    PAIR_MATRIX = "pair-matrix"
    VIA_CLEARANCE = "via-clearance"


#: Ordering used to break exact ties between equal-valued terms.
_SOURCE_RANK: dict[RuleSource, int] = {source: i for i, source in enumerate(RuleSource)}

#: Two requirements closer together than this are the same requirement.
#: A pure tie-break tolerance for *provenance* selection, never a clearance
#: verdict tolerance -- verdicts use the kernel's ``CLEARANCE_EPSILON_MM``.
RESOLVER_TIE_EPSILON_MM = 1e-9


@dataclass(frozen=True)
class DeclaredClearanceRules:
    """What a board's own files declare about copper-to-copper clearance.

    Every field is ``None`` when the corresponding source is absent, which is
    what makes the precedence in :func:`resolve_base_clearance` a genuine
    *order* rather than a ``max`` of everything: the ``.kicad_dru`` rule
    outranks the ``.kicad_pro`` board minimum, and the legacy
    ``.kicad_pcb`` layer is applied on a different footing from both (see
    :meth:`board_requirement`).

    Attributes:
        dru_mm: Strictest unconditional ``(constraint clearance (min …))``
            in the board's ``.kicad_dru`` sidecar.  **Conditional** rules
            (those carrying a ``(condition …)``) are deliberately ignored --
            honouring them needs a DRC-expression evaluator this repo does
            not have, and guessing would be worse than naming the gap.
        project_min_clearance_mm: ``.kicad_pro``
            ``board.design_settings.rules.min_clearance`` (falling back to
            ``design_settings.defaults.clearance_min``) -- KiCad's hard board
            minimum, which clamps every netclass upward.  This is the layer
            the ``#5398`` board declares ``0.20`` mm in.
        net_class_clearance_mm: The applied ``.kicad_pro``
            ``net_settings.classes[].clearance`` -- ``Default`` when the
            project declares one, else the strictest named class (see
            :func:`strictest_net_class_clearance`).  This is the value KiCad's
            own ``clearance`` DRC test measures a copper-to-copper violation
            against, so a board routed below it fails its own DRC gate.
            Promoted to a rule input by #5654, once the project writer stopped
            emitting KiCad's stock ``0.20`` mm template value for it (see
            ``core/project_file.py:DEFAULT_NETCLASS_CLEARANCE_MM``); before
            that it carried no information on this repo's own fleet.
        net_class_name: The class :attr:`net_class_clearance_mm` came from.
        board_net_class_mm: Strictest positive clearance across the legacy
            top-level ``(net_class …)`` blocks of the ``.kicad_pcb`` (#4875).
        board_net_class_name: The class :attr:`board_net_class_mm` came from.
    """

    dru_mm: float | None = None
    project_min_clearance_mm: float | None = None
    net_class_clearance_mm: float | None = None
    net_class_name: str | None = None
    board_net_class_mm: float | None = None
    board_net_class_name: str | None = None

    def project_requirement(self) -> tuple[float, RuleSource, str | None] | None:
        """The winning **project-derived** minimum, or ``None`` when there is none.

        Precedence within this layer: the ``.kicad_dru`` rule when the board
        ships one, then :attr:`project_min_clearance_mm`, then
        :attr:`net_class_clearance_mm`.  Whichever applies is raised by any
        other that is stricter -- all three are minima and KiCad enforces all
        three, so the largest wins and the declaration order above is only the
        tie-break (it matches :class:`RuleSource`'s own order, which is what
        keeps provenance stable across runs).

        Every source here is a *minimum* in KiCad's own model, which is why
        :func:`resolve_base_clearance` applies the result as a **floor** on
        the router's target rather than as a replacement for it -- see that
        function's docstring for why the legacy ``.kicad_pcb`` layer is the
        one exception.

        Returns:
            ``(value_mm, source, class_name)`` or ``None``.
        """
        winner: tuple[float, RuleSource, str | None] | None = None
        if self.dru_mm is not None:
            winner = (self.dru_mm, RuleSource.PROJECT_DRU, None)

        candidates: tuple[tuple[float | None, RuleSource, str | None], ...] = (
            (self.project_min_clearance_mm, RuleSource.PROJECT_MIN_CLEARANCE, None),
            (self.net_class_clearance_mm, RuleSource.PROJECT_NET_CLASS, self.net_class_name),
        )
        for value, source, label in candidates:
            if value is None or value <= 0:
                continue
            if winner is None or value > winner[0] + RESOLVER_TIE_EPSILON_MM:
                winner = (value, source, label)
        return winner

    def board_requirement(self) -> tuple[float, RuleSource, str | None] | None:
        """The legacy top-level ``.kicad_pcb`` ``(net_class …)`` requirement.

        Kept separate from :meth:`project_requirement` because #4875 gave it
        *replacement* semantics -- a board that carries this node states the
        spacing it was designed at, in both directions -- and Phase 2 does
        not revisit that decision.
        """
        if self.board_net_class_mm is None:
            return None
        return (
            self.board_net_class_mm,
            RuleSource.BOARD_NET_CLASS,
            self.board_net_class_name,
        )

    def strictest_requirement(self) -> tuple[float, RuleSource, str | None] | None:
        """The largest requirement any layer declares, for warning text."""
        candidates = [
            item for item in (self.board_requirement(), self.project_requirement()) if item
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])

    def is_empty(self) -> bool:
        """True when no file declared any copper clearance requirement."""
        return self.strictest_requirement() is None


@dataclass(frozen=True)
class ResolvedClearance:
    """A resolved requirement plus the provenance of every contributing term.

    Attributes:
        required_mm: The requirement in mm -- what a caller hands to
            the Phase 1 kernel's ``clear`` predicate.
        source: The layer that produced :attr:`required_mm`.  On an exact tie
            the earliest :class:`RuleSource` member wins, so provenance is
            stable.
        terms: Every term the resolver evaluated, in evaluation order,
            including the ones that lost.  Kept so a banner or a test can
            explain *why* a number is what it is instead of asserting it.
        warning: A human-readable note when a layer was deliberately not
            applied -- today only the "explicit target undercuts a declared
            requirement" and "declared requirement undercuts the fab floor"
            cases.  ``None`` when nothing is worth saying.
        source_label: The netclass or rule name :attr:`source` came from,
            when it has one (``"Default"``, …).  Carried so a banner does not
            have to re-derive which layer won.
    """

    required_mm: float
    source: RuleSource
    terms: tuple[tuple[RuleSource, float], ...] = ()
    warning: str | None = None
    source_label: str | None = None

    def with_term(
        self, source: RuleSource, value: float, label: str | None = None
    ) -> ResolvedClearance:
        """Fold one more strictly-widening term in, keeping provenance.

        A term only moves :attr:`source` when it is *strictly* larger than
        the incumbent by more than :data:`RESOLVER_TIE_EPSILON_MM`, or equal
        and earlier in :class:`RuleSource` declaration order.
        """
        terms = (*self.terms, (source, value))
        if value > self.required_mm + RESOLVER_TIE_EPSILON_MM:
            return ResolvedClearance(value, source, terms, self.warning, label)
        if (
            abs(value - self.required_mm) <= RESOLVER_TIE_EPSILON_MM
            and _SOURCE_RANK[source] < _SOURCE_RANK[self.source]
        ):
            return ResolvedClearance(self.required_mm, source, terms, self.warning, label)
        return ResolvedClearance(
            self.required_mm, self.source, terms, self.warning, self.source_label
        )


@dataclass(frozen=True)
class ClearanceQuery:
    """One "what clearance applies here" question, as an *unordered* pair.

    Attributes:
        net_a: One side's net name (``""`` when the caller has none).
        net_b: The other side's net name.
        kind_a: One side's copper kind.
        kind_b: The other side's copper kind.
        layer: Copper layer index, carried for callers and future
            layer-scoped rules.  No current term reads it: KiCad's own
            ``clearance`` constraint is layer-agnostic unless a conditional
            DRU rule scopes it, and conditional rules are out of scope (see
            :attr:`DeclaredClearanceRules.dru_mm`).
    """

    net_a: str = ""
    net_b: str = ""
    kind_a: CopperKind = CopperKind.TRACE
    kind_b: CopperKind = CopperKind.TRACE
    layer: int | None = None

    def canonical(self) -> ClearanceQuery:
        """The same query with its two sides in a deterministic order.

        Belt and braces: every term in :meth:`ClearanceResolver.resolve` is
        already symmetric, so this cannot change an answer -- it makes the
        symmetry structural rather than a property each term must remember
        to preserve.
        """
        left = (self.net_a, self.kind_a.value)
        right = (self.net_b, self.kind_b.value)
        if left <= right:
            return self
        return ClearanceQuery(
            net_a=self.net_b,
            net_b=self.net_a,
            kind_a=self.kind_b,
            kind_b=self.kind_a,
            layer=self.layer,
        )

    @property
    def involves_via(self) -> bool:
        """True when either side is a via -- the trace/via symmetry predicate."""
        return CopperKind.VIA in (self.kind_a, self.kind_b)

    @property
    def involves_edge(self) -> bool:
        """True when either side is the board outline."""
        return CopperKind.EDGE in (self.kind_a, self.kind_b)


@dataclass(frozen=True)
class ClearanceRuleSet:
    """Every rule input the resolver needs, resolved once per run.

    Attributes:
        base: The board-wide base requirement and its provenance, as produced
            by :func:`resolve_base_clearance`.
        via_mm: ``DesignRules.via_clearance`` -- the trace/via symmetry term.
            ``None`` disables the term (it can then never widen a pair).
        net_classes: ``{net_name: NetClassRouting}`` -- the router's own
            per-class overrides, keyed exactly as
            :func:`~kicad_tools.router.rules.create_net_class_map` keys them.
        pair_table: The #4431 HV/creepage net-pair matrix, or ``None`` (the
            dormant no-``--voltage-map`` case, which is the overwhelming
            majority of boards).
    """

    base: ResolvedClearance
    via_mm: float | None = None
    net_classes: Mapping[str, NetClassRouting] = field(default_factory=dict)
    pair_table: PairwiseClearanceTable | None = None

    @classmethod
    def from_design_rules(
        cls,
        rules: object,
        *,
        base: ResolvedClearance | None = None,
        net_classes: Mapping[str, NetClassRouting] | None = None,
    ) -> ClearanceRuleSet:
        """Build a rule set from a :class:`~kicad_tools.router.rules.DesignRules`.

        The bridge every Phase 3 / Phase 4 consumer uses: it already holds a
        ``DesignRules``, and this turns that into the one resolver without
        the consumer re-deriving any layer itself.

        Args:
            rules: A ``DesignRules`` (duck-typed to keep this module free of
                an import cycle with :mod:`~kicad_tools.router.rules`).
            base: Override the board-wide base.  Defaults to
                ``rules.trace_clearance`` tagged
                :attr:`RuleSource.TARGET_DEFAULT` -- i.e. whatever the run
                already resolved through :func:`resolve_base_clearance` and
                stored on the design rules.
            net_classes: Per-net class overrides.  Defaults to ``{}``.

        Returns:
            The rule set.
        """
        trace = float(getattr(rules, "trace_clearance", 0.0))
        if base is None:
            base = ResolvedClearance(
                trace,
                RuleSource.TARGET_DEFAULT,
                ((RuleSource.TARGET_DEFAULT, trace),),
            )
        via = getattr(rules, "via_clearance", None)
        return cls(
            base=base,
            via_mm=float(via) if via is not None else None,
            net_classes=dict(net_classes or {}),
            pair_table=getattr(rules, "pairwise_clearance", None),
        )


class ClearanceResolver:
    """The single resolver: one requirement per unordered copper pair.

    Construct it once per run from a :class:`ClearanceRuleSet` and call
    :meth:`required_mm` wherever a consumer used to reach for
    ``rules.trace_clearance``, ``rules.via_clearance``,
    ``PairwiseClearanceTable.required_clearance`` or
    :func:`~kicad_tools.router.mfr_limits.resolve_clearance` directly.
    """

    __slots__ = ("_rules",)

    def __init__(self, rules: ClearanceRuleSet) -> None:
        self._rules = rules

    @property
    def rules(self) -> ClearanceRuleSet:
        """The rule set this resolver was built from."""
        return self._rules

    def resolve(self, query: ClearanceQuery) -> ResolvedClearance:
        """Resolve one pair, with full provenance.

        Applies the pair-level layers (steps 7-9 of the module docstring) on
        top of :attr:`ClearanceRuleSet.base`.  Every one is a widening
        ``max`` and a symmetric function of the unordered pair, so the answer
        is independent of argument order.

        Args:
            query: The pair.  A board-edge side (:attr:`CopperKind.EDGE`) is
                answered with the base alone: copper-to-edge is a separate
                KiCad constraint, resolved by
                :func:`~kicad_tools.router.mfr_limits.resolve_edge_clearance`,
                and silently folding it in here would report a
                copper-to-copper number for an edge question.

        Returns:
            The resolved requirement and its provenance.
        """
        q = query.canonical()
        resolved = self._rules.base

        if q.involves_edge:
            return resolved

        net_class_term = self._net_class_term(q)
        if net_class_term is not None:
            resolved = resolved.with_term(RuleSource.NET_CLASS_OVERRIDE, net_class_term)

        table = self._rules.pair_table
        if table is not None and q.net_a and q.net_b:
            resolved = resolved.with_term(
                RuleSource.PAIR_MATRIX, table.required_clearance(q.net_a, q.net_b)
            )

        if q.involves_via and self._rules.via_mm is not None:
            resolved = resolved.with_term(RuleSource.VIA_CLEARANCE, self._rules.via_mm)

        return resolved

    def required_mm(
        self,
        net_a: str = "",
        net_b: str = "",
        kind_a: CopperKind = CopperKind.TRACE,
        kind_b: CopperKind = CopperKind.TRACE,
        layer: int | None = None,
    ) -> float:
        """:meth:`resolve`'s value only -- the form most call sites want.

        Args:
            net_a: One side's net name.
            net_b: The other side's net name.
            kind_a: One side's copper kind.
            kind_b: The other side's copper kind.
            layer: Copper layer index (carried, not yet read).

        Returns:
            The requirement in mm, ready for
            the Phase 1 kernel's ``clear`` predicate.
        """
        return self.resolve(
            ClearanceQuery(net_a=net_a, net_b=net_b, kind_a=kind_a, kind_b=kind_b, layer=layer)
        ).required_mm

    def _net_class_term(self, query: ClearanceQuery) -> float | None:
        """``max`` of the two nets' per-class clearance overrides, if any.

        Symmetric by construction.  A same-net query collapses to the one
        class, which is the same answer either way round.
        """
        classes = self._rules.net_classes
        if not classes:
            return None
        values: list[float] = []
        for net in (query.net_a, query.net_b):
            if not net:
                continue
            net_class = classes.get(net)
            if net_class is None:
                continue
            value = getattr(net_class, "clearance", None)
            if value is None:
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if numeric > 0:
                values.append(numeric)
        if not values:
            return None
        return max(values)


# ---------------------------------------------------------------------------
# Reading the board's own declared rules
# ---------------------------------------------------------------------------


def strictest_net_class_clearance(
    classes: Mapping[str, object],
) -> tuple[str, float] | None:
    """KiCad's ``Default`` class if it declares one, else the strictest.

    The one implementation of "which declared class is the board-wide base",
    shared by ``cli/route_cmd.py:_board_base_clearance`` (#4875).  ``Default``
    is KiCad's board-wide class so it wins when present; on a board that
    declares only named classes the *largest* declared clearance would
    under-constrain the other classes, so the smallest is taken as the
    board-wide base and the per-class widening is left to
    :meth:`ClearanceResolver._net_class_term`.
    """
    candidates: list[tuple[str, float]] = []
    for name, obj in classes.items():
        raw = obj.get("clearance") if isinstance(obj, Mapping) else getattr(obj, "clearance", None)
        if raw is None:
            continue
        numeric = _parse_length_mm(raw)
        if numeric is not None and numeric > 0:
            candidates.append((str(name), numeric))

    if not candidates:
        return None
    for name, value in candidates:
        if name == "Default":
            return (name, value)
    return min(candidates, key=lambda item: (item[1], item[0]))


def _read_project_clearances(
    project_path: Path,
) -> tuple[float | None, tuple[str, float] | None]:
    """Both ``.kicad_pro`` clearance layers, from one read of the file.

    Returns ``(board_minimum_mm, (net_class_name, net_class_mm))``, either
    half ``None`` when the project does not declare it:

    * the board minimum is ``board.design_settings.rules.min_clearance``,
      falling back to the ``board.design_settings.defaults.clearance_min``
      KiCad writes beside it;
    * the netclass is ``net_settings.classes[]``, resolved by
      :func:`strictest_net_class_clearance` (``Default`` wins when present).
      This is what KiCad's own ``clearance`` DRC test measures against, and
      is a rule input since #5654 -- see :class:`DeclaredClearanceRules`.

    Rule derivation is an enhancement, never a new failure mode: an
    unreadable or malformed project yields ``(None, None)`` and the caller
    falls back to the router's own target.
    """
    try:
        data = json.loads(project_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return (None, None)
    if not isinstance(data, dict):
        return (None, None)

    minimum: float | None = None
    board = data.get("board")
    if isinstance(board, dict):
        settings = board.get("design_settings")
        if isinstance(settings, dict):
            for block, key in (("rules", "min_clearance"), ("defaults", "clearance_min")):
                section = settings.get(block)
                if not isinstance(section, dict):
                    continue
                candidate = _parse_length_mm(section.get(key))
                if candidate is not None and candidate > 0:
                    minimum = candidate if minimum is None else max(minimum, candidate)

    return (minimum, _project_net_class(data))


def _project_net_class(data: dict) -> tuple[str, float] | None:
    """The applied ``net_settings.classes[]`` clearance of a parsed project.

    KiCad stores the classes as a *list* of objects each carrying its own
    ``name``; :func:`strictest_net_class_clearance` takes a name-keyed
    mapping, so the list is re-keyed here.  A malformed or classless
    ``net_settings`` contributes nothing.
    """
    net_settings = data.get("net_settings")
    if not isinstance(net_settings, dict):
        return None
    classes = net_settings.get("classes")
    if not isinstance(classes, list):
        return None
    by_name: dict[str, object] = {}
    for entry in classes:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        by_name[name] = entry
    if not by_name:
        return None
    return strictest_net_class_clearance(by_name)


def _read_dru_clearance(dru_path: Path) -> float | None:
    """Strictest **unconditional** ``clearance`` minimum in a ``.kicad_dru``.

    A rule carrying a ``(condition …)`` is skipped: evaluating a KiCad DRC
    expression is out of scope for this phase, and applying a conditional
    rule board-wide would over-constrain every pair the condition excludes.
    ``physical_clearance`` is a different KiCad constraint and is likewise
    not read here.

    Never raises -- a malformed sidecar yields ``None``.
    """
    try:
        text = dru_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if "clearance" not in text:
        return None

    try:
        from kicad_tools.sexp import parse_string

        tree = parse_string("(rules " + text + ")")
    except Exception:
        return None

    best: float | None = None
    for rule in tree.find_all("rule"):
        if rule.find_child("condition") is not None:
            continue
        for constraint in rule.find_all("constraint"):
            atoms = constraint.get_atoms()
            if not atoms or str(atoms[0]) != "clearance":
                continue
            minimum = constraint.find_child("min")
            if minimum is None:
                continue
            value = _parse_length_mm(minimum.get_first_atom())
            if value is None or value <= 0:
                continue
            if best is None or value > best:
                best = value
    return best


def _parse_length_mm(raw: object) -> float | None:
    """Parse a KiCad DRC length literal (``0.2mm``, ``0.2``) into mm."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().lower()
    if text.endswith("mm"):
        text = text[:-2]
    elif text.endswith("in"):
        try:
            return float(text[:-2]) * 25.4
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def read_declared_clearance_rules(
    pcb_path: str | Path,
    *,
    board_net_classes: Mapping[str, object] | None = None,
) -> DeclaredClearanceRules:
    """Read every clearance requirement a board's own files declare.

    Reads the sibling ``.kicad_pro`` and ``.kicad_dru`` alongside the
    ``.kicad_pcb`` -- the same triple ``kicad-cli pcb drc`` consumes, which
    is exactly why the pre-Phase-2 resolver's PCB-only view could return
    ``0.15`` for a board KiCad measures at ``0.20`` (#5398).

    Args:
        pcb_path: Path to the ``.kicad_pcb``.  The project and rule sidecars
            are located by suffix substitution, as KiCad does.
        board_net_classes: Already-parsed legacy top-level ``(net_class …)``
            blocks, as ``cli/route_cmd.py:_board_declared_net_classes``
            produces them.  Passed in rather than re-parsed so the CLI does
            not pay for a second board parse.

    Returns:
        The declared rules.  Missing or malformed files contribute nothing;
        this function never raises.
    """
    path = Path(pcb_path)

    project_minimum, project_net_class = _read_project_clearances(path.with_suffix(".kicad_pro"))
    dru_mm = _read_dru_clearance(path.with_suffix(".kicad_dru"))

    board_net_class = (
        strictest_net_class_clearance(board_net_classes) if board_net_classes else None
    )

    return DeclaredClearanceRules(
        dru_mm=dru_mm,
        project_min_clearance_mm=project_minimum,
        net_class_clearance_mm=project_net_class[1] if project_net_class else None,
        net_class_name=project_net_class[0] if project_net_class else None,
        board_net_class_mm=board_net_class[1] if board_net_class else None,
        board_net_class_name=board_net_class[0] if board_net_class else None,
    )


# ---------------------------------------------------------------------------
# The board-wide base
# ---------------------------------------------------------------------------


def resolve_base_clearance(
    *,
    target_mm: float,
    declared: DeclaredClearanceRules | None = None,
    fab_floor_mm: float | None = None,
    explicit_target: bool = False,
    explicit_manufacturer: bool = False,
    manufacturer: str | None = None,
) -> ResolvedClearance:
    """Resolve the board-wide base requirement (steps 1-6 of the precedence).

    Args:
        target_mm: The router's own spacing target -- ``kct route``'s
            ``--clearance`` value, explicit or defaulted.
        declared: What the board's files declare
            (:func:`read_declared_clearance_rules`).  ``None`` is the
            "nothing declared" case and behaves identically to an empty
            :class:`DeclaredClearanceRules`.
        fab_floor_mm: The fab tier's absolute minimum, from
            :func:`~kicad_tools.router.mfr_limits.resolve_clearance`.
            ``None`` when no manufacturer is configured.
        explicit_target: True when the operator passed ``--clearance``.  The
            value is then authoritative and is never raised; a declared
            requirement above it produces a warning instead.
        explicit_manufacturer: True when the operator passed
            ``--manufacturer`` / ``--mfr``.  Names the fab-floor term
            :attr:`RuleSource.MANUFACTURER_OVERRIDE` instead of
            :attr:`RuleSource.FAB_FLOOR`; it does **not** suppress a
            stricter declared requirement (see the module docstring, step 6).
        manufacturer: Manufacturer name, for the warning text only.

    Returns:
        The base requirement, its provenance, every term evaluated, and a
        warning when a layer was deliberately not applied.
    """
    target = float(target_mm)
    target_source = RuleSource.EXPLICIT_TARGET if explicit_target else RuleSource.TARGET_DEFAULT
    resolved = ResolvedClearance(target, target_source, ((target_source, target),))
    declared = declared or DeclaredClearanceRules()
    legacy = declared.board_requirement()
    project = declared.project_requirement()
    floor_source = (
        RuleSource.MANUFACTURER_OVERRIDE if explicit_manufacturer else RuleSource.FAB_FLOOR
    )

    if explicit_target:
        # The operator's value is authoritative -- say so when it undercuts a
        # rule rather than silently overriding the board.
        strictest = declared.strictest_requirement()
        if strictest is not None and strictest[0] > target + RESOLVER_TIE_EPSILON_MM:
            where = strictest[2] or strictest[1].value
            return ResolvedClearance(
                resolved.required_mm,
                resolved.source,
                resolved.terms,
                (
                    f"WARNING: explicit --clearance {target:.4g}mm is below the "
                    f"{strictest[0]:.4g}mm this board declares ({where}); copper "
                    f"routed at {target:.4g}mm can fail the board's own DRC."
                ),
            )
        return resolved

    if legacy is not None:
        # #4875, unchanged: a legacy top-level ``(net_class …)`` block states
        # the spacing the board was designed at, so it REPLACES the target in
        # both directions rather than flooring it.  Nothing KiCad 6+ writes
        # reaches this branch -- it exists for boards imported from KiCad 5
        # and earlier.
        value, source, class_name = legacy
        resolved = ResolvedClearance(
            value, source, (*resolved.terms, (source, value)), None, class_name
        )

    if project is not None:
        # Every project-derived layer is a MINIMUM in KiCad's own model
        # (``min_clearance``, a ``(constraint clearance (min …))`` rule, the
        # netclass floor a DRC violation is measured against), so it can only
        # raise the requirement.  Flooring rather than replacing is what keeps
        # this phase from silently *loosening* a board whose project declares
        # less than the router's own target -- the opposite of the #5398
        # defect, and just as much a per-invocation divergence.
        resolved = resolved.with_term(project[1], project[0], project[2])

    # The strictest declared requirement is what the fab-floor warning is
    # about: a value no configured tier can etch.
    requirement = legacy if legacy is not None else project
    warning: str | None = None
    if (
        requirement is not None
        and fab_floor_mm is not None
        and resolved.required_mm < fab_floor_mm - RESOLVER_TIE_EPSILON_MM
    ):
        value, source, class_name = requirement
        where = class_name or source.value
        warning = (
            f'WARNING: the board declares clearance {value:.4g}mm ("{where}"), '
            f"which is below the {manufacturer} minimum clearance "
            f"{fab_floor_mm:.4g}mm -- no configured fab tier can produce it. "
            f"Resolving at the {fab_floor_mm:.4g}mm floor instead; pass "
            f"--clearance {value:.4g} to route at the board's declared value anyway."
        )

    if fab_floor_mm is not None and fab_floor_mm > 0:
        resolved = resolved.with_term(floor_source, fab_floor_mm)

    if warning is not None:
        resolved = ResolvedClearance(
            resolved.required_mm,
            resolved.source,
            resolved.terms,
            warning,
            resolved.source_label,
        )
    return resolved
