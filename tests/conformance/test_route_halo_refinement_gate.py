"""A merge gate for #5410's search-time refinement, on ground truth.

Issue #5410 repaired one specific failure: the A* search rejected *legal*
geometry because a routed net's conservative square halo had swallowed a
candidate that the final geometric validator -- and `kicad-cli pcb drc` --
both accept.  The repair is ``RouteHaloGeometry.clear`` (group 4) and its
native twin ``Grid3D::route_*_geometry_clear`` (group 5): when the raster says
a cell is blocked, these ask the *actual* object-pair geometry whether the
block is real.

``docs/clearance-conformance.md`` already **measures** those two groups, but
every unmigrated consumer row in ``test_corpus.py`` is automatically
``xfail(strict=False)`` (see :mod:`tests.conformance.conftest`), so nothing
reddens if the refinement regresses.  That is correct for a row whose
disagreement is still evidence for a future phase -- and wrong for this one,
because the disagreement #5410 is *about* has already been driven to zero and
has no later phase left to own it.  This module is the missing gate.

Two readings, and the difference between them is the whole point
-------------------------------------------------------------------

The published table drives these consumers at the **router's own**
``trace_clearance`` (0.15 mm in this corpus) while ``kicad-cli`` applies the
project's ``Default`` netclass (0.20 mm).  Measured over seeds 0-29, that
single rule difference accounts for the row's *entire* under-rejection cell:
every under-rejected pair is a ``seg-seg`` one whose gap falls in the
0.15-0.20 mm band -- the #5398 / #5654 rule-resolution defect, which no
refinement change may close and which this module therefore does **not** gate.

Pinned to ``case.rules.project_clearance`` -- ground truth's own number, via
:func:`~tests.conformance.adapters._support.project_rules` -- the same
unmodified consumers agree with ``kicad-cli`` in *both* directions.  With the
rule axis held fixed, a disagreement can only be geometry, so:

* :func:`test_refinement_agrees_with_kicad_cli_at_the_project_clearance` gates
  it hard.  **Over-rejection** is #5410's own failure mode (a legal candidate
  refused, burning A* expansions until the budget is exhausted).
  **Under-rejection** is the refinement's safety direction -- the issue's
  *"a candidate may pass only when the applicable object-pair geometry and
  electrical/manufacturing requirements permit it"* -- so a refinement that
  bought routability by waving copper through fails here too.
* :func:`test_refinement_under_rejection_is_the_rule_band_not_geometry` keeps
  the published row's own reading honest: it asserts the attribution above
  rather than leaving it as prose in the ``notes`` column.  If a *geometry*
  under-rejection ever appears at the consumer's own rule values, the note is
  wrong and this reddens.

Deliberately **not** ``@pytest.mark.consumer``
-----------------------------------------------

That marker is what :func:`tests.conformance.conftest.pytest_collection_modifyitems`
keys the automatic ``xfail`` off.  Applying it here would silently convert
this gate back into a report-only row -- the exact defeat this module exists
to prevent -- so :func:`test_the_gate_is_hard_not_report_only` asserts the
absence.

This also does not put groups 4 and 5 into ``report.MIGRATED_GROUPS``: that
registry means *"switched onto the Phase 1b clearance kernel"*, which is Epic
#5509 Phase 3b's (#5661) job and has not happened.  A group can be conformant
without having been migrated, and conflating the two would both misreport the
epic's progress and collide with #5661's diff.
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
from tests.conformance.report import (
    ADAPTERS,
    MIGRATED_GROUPS,
    REFINEMENT_GATED_GROUPS,
    render_document,
)
from tests.conformance.test_corpus import CI_SEEDS
from tests.conformance.test_report_shape import _STUB_STATS

#: The two consumer groups that *are* #5410's repair: the Python refinement and
#: its native twin.
#:
#: Read from ``report.REFINEMENT_GATED_GROUPS`` rather than restated here, so
#: the published table's "gated without having been migrated" prose and this
#: module's actual coverage cannot drift apart -- there is one registry, and
#: :func:`test_the_gated_groups_are_the_refinement_groups` checks this module
#: against it.
REFINEMENT_GROUPS = tuple(sorted(REFINEMENT_GATED_GROUPS))

#: Seeds the gate runs on.  Shared with ``test_corpus.CI_SEEDS`` so the corpus
#: has one pinning, and small for the same reason that module states: every
#: item launches its own ``kicad-cli``.  The truth fixture below caches by
#: seed, so this whole module costs one oracle run per seed, not one per item.
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


def _gated_verdicts(adapter: ConsumerAdapter, case: CopperCase) -> set[frozenset[str]]:
    """The adapter's reading with the rule axis pinned to kicad-cli's own value.

    Resolved through ``getattr`` rather than called directly, for the same
    reason ``test_corpus._consumer_verdicts`` does: ``verdicts_at_project_rules``
    is an *optional* part of the :class:`ConsumerAdapter` protocol (only a
    gated adapter needs it), so an adapter that dropped it must produce a
    named failure here rather than an ``AttributeError`` mid-assertion.
    """
    at_project = getattr(adapter, "verdicts_at_project_rules", None)
    assert at_project is not None, (
        f"{adapter.name} (group {adapter.group}) does not implement "
        "verdicts_at_project_rules(case); without it the rule axis cannot be pinned "
        "and this gate would redden for #5398/#5654 instead of for geometry"
    )
    return {v.nets for v in at_project(case)}


@pytest.fixture(scope="module")
def truth_for(tmp_path_factory: pytest.TempPathFactory) -> Callable[[int], set[frozenset[str]]]:
    """``seed -> kicad-cli's flagged pairs``, one oracle run per seed.

    Module-scoped and memoised on purpose.  Four items share each seed (two
    adapters x two readings) and the oracle's answer does not depend on which
    consumer is about to be compared against it, so running it per item would
    quadruple this module's ``kicad-cli`` fan-out to say the same thing.  CI
    runs the conformance suite unparallelised precisely because that fan-out
    is what exhausted the runner's cgroup (see ``.github/workflows/ci.yml``).

    Measured **as-is**, not refilled, matching
    ``test_corpus.test_adapter_agrees_with_kicad_cli``: neither refinement
    predicate has a zone-fill term, and no pair kind in
    :data:`REFINEMENT_GROUPS`' scope is a zone pair.
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
# The gate
# ---------------------------------------------------------------------------


@requires_kicad_cli
@pytest.mark.parametrize("seed", GATE_SEEDS)
@pytest.mark.parametrize("adapter", _ADAPTER_PARAMS)
def test_refinement_agrees_with_kicad_cli_at_the_project_clearance(
    adapter: ConsumerAdapter,
    seed: int,
    truth_for: Callable[[int], set[frozenset[str]]],
) -> None:
    """#5410's repair, driven at ground truth's own clearance, must agree exactly.

    Not ``xfail``: unlike the published rows, this reading has the rule axis
    pinned, so a disagreement is a geometry defect in the refinement itself
    and there is no later epic phase that owns fixing it.

    Both directions are asserted.  Over-rejection is the regression #5410
    repaired; under-rejection is the way a *future* "fix" for it could cheat,
    by relaxing the refinement until illegal copper passes.
    """
    case = generate_case(seed)
    over, under = _split(adapter, case, _gated_verdicts(adapter, case), truth_for(seed))

    assert not over and not under, (
        f"{adapter.name} (group {adapter.group}) disagrees with kicad-cli on seed {seed}, "
        "driven at the project netclass clearance kicad-cli itself applies -- so this is a "
        "GEOMETRY disagreement in #5410's search-time refinement, not the #5398/#5654 rule gap.\n"
        f"  over-rejected (refinement refuses, KiCad clean): {_describe(over)}\n"
        f"  under-rejected (KiCad flags, refinement allows): {_describe(under)}"
    )


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
    **rule** reading rather than a geometry one.  This asserts the claim
    instead of trusting it: at the consumer's own rule values, every
    under-rejected pair must be a routed-copper pair whose gap sits in the
    ``[trace_clearance, project_clearance)`` band -- close enough for the
    project netclass to flag and far enough for the router's own value not to.

    A pair outside that band would mean the refinement is allowing copper that
    is illegal *by its own numbers*, which no rule difference can explain.
    """
    case = generate_case(seed)
    rules = case.rules
    consumer = {v.nets for v in adapter.verdicts(case)}
    over, under = _split(adapter, case, consumer, truth_for(seed))

    assert not over, (
        f"{adapter.name} (group {adapter.group}) over-rejects on seed {seed} at its own rule "
        f"values -- #5410's failure mode, and the published table records 0.0% here: "
        f"{_describe(over)}"
    )

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

    A gate over an empty denominator is green for the wrong reason: with no
    under-rejected pair anywhere in the seed range,
    :func:`test_refinement_under_rejection_is_the_rule_band_not_geometry`
    would pass while asserting nothing, and the published row's non-zero
    percentage would go unexplained by this suite.  Stated over the *union* of
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
# The gate's own mechanism, asserted rather than assumed (no kicad-cli needed)
# ---------------------------------------------------------------------------


def test_the_gated_groups_are_the_refinement_groups() -> None:
    """:data:`REFINEMENT_GROUPS` still names the two adapters this module drives.

    Guards the failure where the gate quietly measures nothing: an adapter
    renumbered, renamed or dropped from ``report.ADAPTERS`` would otherwise
    leave this module passing over a consumer nobody publishes.
    """
    by_group = {adapter.group: adapter for adapter in ADAPTERS}
    missing = [group for group in REFINEMENT_GROUPS if group not in by_group]
    assert not missing, (
        f"groups {missing} are no longer registered in report.ADAPTERS; this gate would "
        "measure a consumer the table does not publish"
    )

    driven = {adapter.group for adapter in _REFINEMENT_ADAPTERS}
    assert driven == set(REFINEMENT_GROUPS), (
        f"this module drives groups {sorted(driven)} but claims to gate {sorted(REFINEMENT_GROUPS)}"
    )

    for group in REFINEMENT_GROUPS:
        adapter = by_group[group]
        assert hasattr(adapter, "verdicts_at_project_rules"), (
            f"{adapter.name} (group {group}) no longer offers the gated reading; "
            "without it the rule axis cannot be pinned to kicad-cli's own value and "
            "this gate would redden for #5398/#5654 instead of for geometry"
        )


def test_gating_these_groups_does_not_claim_they_were_migrated() -> None:
    """A conformant group is not the same thing as a kernel-switched one.

    ``report.MIGRATED_GROUPS`` means *"switched onto the Phase 1b clearance
    kernel"* and drives the table's "no longer report-only" prose.  Groups 4/5
    agree with ground truth today but still carry their own arithmetic; the
    switch is Epic #5509 Phase 3b (#5661).  Adding them here to get a hard gate
    would misreport the epic and collide with that phase's diff -- this module
    is how they get the gate without the false claim.
    """
    overlap = sorted(set(REFINEMENT_GROUPS) & set(MIGRATED_GROUPS))
    assert not overlap, (
        f"groups {overlap} are now in report.MIGRATED_GROUPS, so test_corpus.py already "
        "hard-gates them at the project clearance. Delete the duplicated gate in this "
        "module rather than running two copies of the same assertion."
    )


def test_the_published_table_says_these_rows_are_gated() -> None:
    """The reader of the table learns these two rows are a merge gate.

    The distinction this module rests on -- *conformant* is not *migrated* --
    only helps someone who can see it.  Without this, a reader meeting a 0.0%
    over-rejection cell on an unmigrated row has no way to tell whether it is
    protected or merely lucky this regeneration, which is the same
    "an omitted row reads as fine" failure the whole document exists to avoid.

    Rendered against stub stats, so it needs no ``kicad-cli`` and runs on
    every PR.
    """
    doc = render_document(_STUB_STATS)
    assert "Gated without having been migrated" in doc
    for group in REFINEMENT_GROUPS:
        assert f"group {group} ({REFINEMENT_GATED_GROUPS[group]})" in doc, (
            f"the rendered document does not announce group {group} as gated"
        )
    assert "test_route_halo_refinement_gate.py" in doc, (
        "the document names no gate module, so a reader cannot check the claim"
    )


def test_the_gate_is_hard_not_report_only() -> None:
    """No item in this module carries ``@pytest.mark.consumer``.

    That marker is exactly what ``conftest.pytest_collection_modifyitems``
    keys the automatic ``xfail(strict=False)`` off.  Adding it here would be a
    one-word change that silently turns this whole module back into a
    report-only row while every test still reads as passing -- the single most
    plausible way for #5410's gate to be defeated by accident.
    """
    import inspect
    import sys

    module = sys.modules[__name__]
    marked = [
        name
        for name, obj in inspect.getmembers(module, inspect.isfunction)
        if name.startswith("test_")
        and any(mark.name == "consumer" for mark in getattr(obj, "pytestmark", ()))
    ]
    assert not marked, (
        f"{marked} carry @pytest.mark.consumer, which conftest auto-xfails. These are "
        "#5410's merge gate and must stay hard failures."
    )
