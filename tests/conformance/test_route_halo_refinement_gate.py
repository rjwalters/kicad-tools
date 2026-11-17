"""Rule-band attribution for #5410's search-time refinement, now migrated.

Issue #5410 repaired one specific failure: the A* search rejected *legal*
geometry because a routed net's conservative square halo had swallowed a
candidate that the final geometric validator -- and `kicad-cli pcb drc` --
both accept.  The repair is ``RouteHaloGeometry.clear`` (group 4) and its
native twin ``Grid3D::route_*_geometry_clear`` (group 5).

Epic #5509 Phase 3b (#5661) switched both onto the shared clearance kernel and
moved them into ``report.MIGRATED_GROUPS``, so the hard merge gate this module
used to carry -- over-rejection and under-rejection at the project's own
clearance, both held to zero -- is now ``test_corpus.py``'s standard
migrated-group gate (``conftest.assert_migrated_group_agrees``).  Running it
again here would be the same assertion twice, so that half of this module was
retired with the switch.

What this module still owns: the published table's own-rule-value reading
(``adapter.verdicts(case)``, *not* pinned to the project clearance) carries a
non-zero under-rejection cell that the table's notes (``report.NOTES[4]`` /
``[5]``) attribute entirely to a **rule** difference -- the router's own
``trace_clearance`` versus the project's ``Default`` netclass (#5398 / #5654)
-- rather than to geometry.  That attribution is a claim about *every*
under-rejected pair, not a percentage, so it is asserted pair-by-pair here
rather than trusted from the note's prose.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from tests.conformance.adapters import ConsumerAdapter
from tests.conformance.adapters.route_geometry_cpp import RouteGeometryCppAdapter
from tests.conformance.adapters.route_halo import RouteHaloAdapter
from tests.conformance.board import write_case
from tests.conformance.conftest import requires_adapter, requires_kicad_cli
from tests.conformance.generator import CopperCase, PairKind, generate_case
from tests.conformance.oracle import run_oracle
from tests.conformance.report import ADAPTERS, MIGRATED_GROUPS
from tests.conformance.test_corpus import CI_SEEDS

#: The two consumer groups that *are* #5410's repair: the Python refinement and
#: its native twin -- ``report.MIGRATED_GROUPS`` groups 4 and 5.
_REFINEMENT_GROUPS = (4, 5)

#: Seeds the attribution check runs on.  Shared with ``test_corpus.CI_SEEDS``
#: so the corpus has one pinning, and small for the same reason that module
#: states: every item launches its own ``kicad-cli``.
GATE_SEEDS = CI_SEEDS

#: The adapter instances this module drives, in group order.
_REFINEMENT_ADAPTERS: tuple[ConsumerAdapter, ...] = (
    RouteHaloAdapter(),
    RouteGeometryCppAdapter(),
)

_ADAPTER_PARAMS = [
    pytest.param(adapter, id=adapter.name, marks=(requires_adapter(adapter),))
    for adapter in _REFINEMENT_ADAPTERS
]


@pytest.fixture(scope="module")
def truth_for(tmp_path_factory: pytest.TempPathFactory) -> Callable[[int], set[frozenset[str]]]:
    """``seed -> kicad-cli's flagged pairs``, one oracle run per seed.

    Module-scoped and memoised on purpose: both items for a given seed (one
    per adapter) share the same oracle answer, so running it per item would
    double this module's ``kicad-cli`` fan-out to say the same thing.  CI runs
    the conformance suite unparallelised precisely because that fan-out is
    what exhausted the runner's cgroup (see ``.github/workflows/ci.yml``).

    Measured **as-is**, not refilled, matching
    ``test_corpus.test_adapter_agrees_with_kicad_cli``: neither refinement
    predicate has a zone-fill term, and no pair kind in scope here is a zone
    pair.
    """
    directory = tmp_path_factory.mktemp("route_halo_gate")
    cache: dict[int, set[frozenset[str]]] = {}

    def lookup(seed: int) -> set[frozenset[str]]:
        if seed not in cache:
            case = generate_case(seed)
            board = write_case(case, Path(directory))
            result = run_oracle(board.pcb_path, refill=False, work_dir=Path(directory))
            cache[seed] = {v.nets for v in result.without_zones()}
        return cache[seed]

    return lookup


def _scored_pairs(adapter: ConsumerAdapter, case: CopperCase) -> list:
    """The case's pairs this adapter is actually consulted about.

    Boundary-band pairs are dropped for the reason the report drops them:
    within 1 um of the requirement the two models are arguing about rounding.
    Zone pairs are dropped because an as-is board's unfilled pour contributes
    no copper to ``kicad-cli`` at all.
    """
    return [
        pair
        for pair in case.pairs
        if pair.kind in adapter.pair_kinds and not pair.boundary and pair.kind not in PairKind.ZONE
    ]


def _split(
    adapter: ConsumerAdapter,
    case: CopperCase,
    consumer: set[frozenset[str]],
    truth: set[frozenset[str]],
) -> tuple[list, list]:
    """``(over_rejected, under_rejected)`` pairs, compared on pair identity."""
    over: list = []
    under: list = []
    for pair in _scored_pairs(adapter, case):
        in_truth = pair.nets in truth
        in_consumer = pair.nets in consumer
        if in_consumer and not in_truth:
            over.append(pair)
        elif in_truth and not in_consumer:
            under.append(pair)
    return over, under


def _describe(pairs: list) -> str:
    return (
        ", ".join(f"{p.kind} {sorted(p.nets)} gap={p.target_gap_mm:.4f}" for p in pairs) or "none"
    )


# ---------------------------------------------------------------------------
# The attribution check
# ---------------------------------------------------------------------------


@requires_kicad_cli
@pytest.mark.parametrize("seed", GATE_SEEDS)
@pytest.mark.parametrize("adapter", _ADAPTER_PARAMS)
def test_refinement_under_rejection_is_the_rule_band_not_geometry(
    adapter: ConsumerAdapter,
    seed: int,
    truth_for: Callable[[int], set[frozenset[str]]],
) -> None:
    """The published row's under-rejection cell is attributable, pair by pair.

    ``docs/clearance-conformance.md``'s groups 4/5 notes claim that cell is a
    **rule** reading rather than a geometry one. This asserts the claim
    instead of trusting it: at the consumer's own rule values, every
    under-rejected pair must be a routed-copper pair whose gap sits in the
    ``[trace_clearance, project_clearance)`` band -- close enough for the
    project netclass to flag and far enough for the router's own value not to.

    A pair outside that band would mean the refinement is allowing copper that
    is illegal *by its own numbers*, which no rule difference can explain.
    Over-rejection is not checked here -- that is Epic #5509 Phase 3b's own
    geometry fix, hard-gated by ``test_corpus.py`` now that groups 4/5 are in
    ``report.MIGRATED_GROUPS``.
    """
    case = generate_case(seed)
    rules = case.rules
    consumer = {v.nets for v in adapter.verdicts(case)}
    _, under = _split(adapter, case, consumer, truth_for(seed))

    unexplained = [
        pair
        for pair in under
        if not (rules.trace_clearance <= pair.target_gap_mm < rules.project_clearance)
    ]
    assert not unexplained, (
        f"{adapter.name} (group {adapter.group}) under-rejects on seed {seed} OUTSIDE the "
        f"[{rules.trace_clearance}, {rules.project_clearance}) mm rule band, so the "
        "docs/clearance-conformance.md note attributing that cell to #5398/#5654 rule "
        f"resolution is no longer true: {_describe(unexplained)}"
    )


@requires_kicad_cli
@pytest.mark.parametrize("adapter", _ADAPTER_PARAMS)
def test_the_rule_band_attribution_is_not_vacuous(
    adapter: ConsumerAdapter,
    truth_for: Callable[[int], set[frozenset[str]]],
) -> None:
    """At least one under-rejection exists across :data:`GATE_SEEDS`.

    A check over an empty denominator is green for the wrong reason: with no
    under-rejected pair anywhere in the seed range,
    :func:`test_refinement_under_rejection_is_the_rule_band_not_geometry`
    would pass while asserting nothing, and the published row's non-zero
    percentage would go unexplained by this suite. Stated over the *union* of
    the seeds rather than per seed, because most single seeds carry none.
    """
    found = 0
    for seed in GATE_SEEDS:
        case = generate_case(seed)
        consumer = {v.nets for v in adapter.verdicts(case)}
        _, under = _split(adapter, case, consumer, truth_for(seed))
        found += len(under)

    assert found, (
        f"no {adapter.name} under-rejection anywhere in GATE_SEEDS={GATE_SEEDS}, so the rule-band "
        "attribution test is vacuous -- re-pin GATE_SEEDS onto a seed that carries one "
        "(seed 2 did when this gate was written), or, if the corpus genuinely no longer "
        "produces any, update the groups 4/5 notes in tests/conformance/report.py to match."
    )


# ---------------------------------------------------------------------------
# The module's own mechanism, asserted rather than assumed (no kicad-cli needed)
# ---------------------------------------------------------------------------


def test_the_refinement_groups_are_migrated() -> None:
    """Groups 4 and 5 are registered, wired, and in ``report.MIGRATED_GROUPS``.

    Guards the failure where this module quietly measures a consumer the
    published table no longer recognises, or re-litigates a hard gate
    ``test_corpus.py`` already owns because a future edit moved a group back
    out of ``MIGRATED_GROUPS`` without updating this module.
    """
    by_group = {adapter.group: adapter for adapter in ADAPTERS}
    missing = [group for group in _REFINEMENT_GROUPS if group not in by_group]
    assert not missing, (
        f"groups {missing} are no longer registered in report.ADAPTERS; this module would "
        "measure a consumer the table does not publish"
    )

    driven = {adapter.group for adapter in _REFINEMENT_ADAPTERS}
    assert driven == set(_REFINEMENT_GROUPS), (
        f"this module drives groups {sorted(driven)} but names {sorted(_REFINEMENT_GROUPS)}"
    )

    missing_migration = set(_REFINEMENT_GROUPS) - MIGRATED_GROUPS
    assert not missing_migration, (
        f"groups {sorted(missing_migration)} are not in report.MIGRATED_GROUPS -- #5410's "
        "repair is not migrated, so the hard over-/under-rejection gate this module used to "
        "carry has nowhere to live; restore it here rather than assuming test_corpus.py covers it"
    )
