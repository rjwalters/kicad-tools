"""Each wired adapter, measured against kicad-cli over the smoke corpus.

``test_corpus_truth.py`` proved the *harness* is honest: the geometry the
generator intends is the geometry KiCad measures.  This module is the layer
above it -- the same seeded cases, but now asking each in-tree clearance
consumer the question kicad-cli already answered.

**An adapter-vs-truth row is report-only until its consumer is switched.**
Each carries ``@pytest.mark.consumer``, and ``conftest.py``'s collection hook
turns that into ``xfail(strict=False)``.  A measured disagreement must not
redden the build: consumers are switched to the shared kernel in their own
Epic #5509 phase, and only then does a row become a merge gate.  That
mechanism landed untested in PR #5532 (there were no consumer items to
exercise it), so :func:`test_every_unmigrated_consumer_item_carries_xfail`
asserts it over the whole collected suite.

**A row whose group is in ``report.MIGRATED_GROUPS`` is a merge gate.**  Its
consumer has had its Phase 3/4 PR, the hook leaves the item un-``xfail``\\ ed,
and :func:`test_adapter_agrees_with_kicad_cli` asserts agreement for real.
The gated reading drives that same unmodified consumer at the clearance
**kicad-cli itself applies** (``adapter.verdicts_at_project_rules``), so the
gate fails on a geometry disagreement -- what a migration changes -- and not
on the rule value the consumer resolves, which is Phase 2's axis and stays
measured by the published table.
:func:`test_migrated_consumer_items_are_hard_gates` asserts the flip really
reached the collected items.

**Hard assertions here are about the adapters, not the consumers.**  An
adapter must satisfy the protocol, must be deterministic, and must only ever
speak about nets the case actually declares.  Those are properties of the
harness and a failure in one is a real bug -- so they carry no ``consumer``
marker and no ``xfail``.

One test item per ``(seed, adapter)``, never a loop over seeds inside one
item: the CI ``test`` job runs ``--timeout=60``, and a batched item would both
risk the reaper and report one failure for the whole corpus instead of naming
the seed that broke.  The smoke corpus is deliberately tiny -- each item costs
a kicad-cli process -- while the published table is measured out-of-band over
seeds 0-199 by ``python -m tests.conformance.report``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conformance.adapters import BOARD_EDGE, ConsumerAdapter, Verdict
from tests.conformance.adapters.kernel import KERNEL_GROUP
from tests.conformance.board import write_case
from tests.conformance.conftest import item_group, requires_adapter, requires_kicad_cli
from tests.conformance.generator import PairKind, generate_case
from tests.conformance.oracle import run_oracle
from tests.conformance.report import ADAPTERS, MIGRATED_GROUPS, main

_ADAPTERS_BY_GROUP = {adapter.group: adapter for adapter in ADAPTERS}

# Kept smaller than ``test_corpus_truth.CI_SEEDS``: this module multiplies its
# seeds by every wired adapter, and every item launches its own kicad-cli.
CI_SEEDS = (0, 1, 2)

# The Epic #5509 section-1 groups Phase 1c leaves unmeasured, with their
# reasons recorded in ``report.NOT_MEASURED_REASONS``.  Exactly one: group 7's
# ``CoupledPathfinder::rail_clear`` is a lambda inside the C++ coupled search
# loop, so no Python entry point exists to wrap.  That is the single
# *unexposed* entry the epic's acceptance criterion permits, and keeping this
# set a literal ``{7}`` is what makes "only one" a test rather than a claim --
# wiring a group without removing it here, or losing a group's adapter, both
# fail ``test_adapters_cover_the_wired_groups``.
UNWIRED_GROUPS = {7}

# Every group row the table measures, plus the Phase 1b kernel's control row.
WIRED_GROUPS = {n for n in range(1, 20) if n not in UNWIRED_GROUPS} | {KERNEL_GROUP}

_ADAPTER_PARAMS = [
    pytest.param(adapter, id=adapter.name, marks=(requires_adapter(adapter),))
    for adapter in ADAPTERS
]


# ---------------------------------------------------------------------------
# Adapter contract -- hard assertions, no kicad-cli needed
# ---------------------------------------------------------------------------


def test_adapters_cover_the_wired_groups() -> None:
    """``ADAPTERS`` wires exactly the groups this phase set out to measure.

    Both directions matter.  A *missing* group is coverage that quietly went
    away; an *extra* one is a group wired without a row in the epic's
    inventory, which the report's 19-row invariant would then have nowhere to
    put.
    """
    assert {adapter.group for adapter in ADAPTERS} == WIRED_GROUPS
    assert len({adapter.name for adapter in ADAPTERS}) == len(ADAPTERS)


@pytest.mark.parametrize("adapter", _ADAPTER_PARAMS)
def test_adapter_satisfies_the_protocol(adapter: ConsumerAdapter) -> None:
    assert isinstance(adapter, ConsumerAdapter)
    assert isinstance(adapter.name, str) and adapter.name
    assert adapter.group in WIRED_GROUPS
    assert adapter.pair_kinds <= frozenset(PairKind.ALL)
    assert adapter.pair_kinds, f"{adapter.name} declares no pair kinds at all"
    assert isinstance(adapter.available(), bool)


def test_only_the_kernel_answers_pad_pad_pairs() -> None:
    """``pad-pad`` is placement copper: no router-side consumer sees it.

    The kind exists for group 19's incremental placement DRC, whose entry
    point takes two whole footprints.  Any *other* adapter claiming it would
    be scored on pairs its consumer is never consulted about in production,
    which is the exact error ``ConsumerAdapter.pair_kinds`` exists to prevent
    -- and the reason ``_support.ALL_PAIR_KINDS`` is ``PairKind.ROUTING``
    rather than ``PairKind.ALL``.
    """
    claimants = {a.group for a in ADAPTERS if PairKind.PAD_PAD in a.pair_kinds}
    assert claimants == {19, KERNEL_GROUP}, (
        "pad-pad may only be claimed by group 19 (drc_cpp) and the kernel "
        f"control row; also claimed by groups {sorted(claimants - {19, KERNEL_GROUP})}"
    )


def test_only_zone_and_edge_aware_consumers_answer_those_pairs() -> None:
    """The #5644 kinds are claimed by the two groups that model them, and no more.

    A consumer with no zone-fill or board-outline term in its arithmetic is
    never consulted about one in production, so scoring it on those pairs
    would manufacture an under-rejection rate out of a question nobody asks
    it -- the same error ``pair_kinds`` exists to prevent for ``pad-pad``.
    Group 18 drives four rules that really read that geometry; group 10's
    ``is_clear`` really has a ``pours`` and an ``outline`` branch (and no via
    candidate, which is why it claims ``seg-zone`` but not ``via-zone``).
    """
    zone_claimants = {a.group for a in ADAPTERS if set(PairKind.ZONE) & a.pair_kinds}
    edge_claimants = {a.group for a in ADAPTERS if set(PairKind.EDGE) & a.pair_kinds}
    assert zone_claimants == {10, 18}, sorted(zone_claimants)
    assert edge_claimants == {10, 18}, sorted(edge_claimants)

    mesh = next(a for a in ADAPTERS if a.group == 10)
    assert PairKind.VIA_ZONE not in mesh.pair_kinds, (
        "ObstacleModel.is_clear takes two points and no width, so it has no "
        "via candidate to answer about"
    )
    kernel = _ADAPTERS_BY_GROUP[KERNEL_GROUP]
    assert not (set(PairKind.ZONE) | set(PairKind.EDGE)) & kernel.pair_kinds, (
        "the kernel control row is scored on the same copper the consumer "
        "rows are; modelling a pour by its declared boundary while kicad-cli "
        "measures the filled polygon would make its 0%/0% criterion fiction"
    )


@pytest.mark.parametrize("adapter", _ADAPTER_PARAMS)
def test_adapter_is_deterministic(adapter: ConsumerAdapter) -> None:
    """Two runs over the same case give the same answer.

    A consumer driven through a grid, a temp file or a compiled extension has
    plenty of places for run-to-run variation to creep in; a table regenerated
    from a non-deterministic adapter would not be reproducible evidence.
    """
    case = generate_case(3)
    assert adapter.verdicts(case) == adapter.verdicts(case)


@pytest.mark.parametrize("adapter", _ADAPTER_PARAMS)
def test_adapter_only_speaks_about_declared_nets(adapter: ConsumerAdapter) -> None:
    """Verdict nets come from the case, never from somewhere else.

    Guards the translation in ``adapters/_support.py``: a net name an adapter
    invented (or a net id it mapped back wrongly) would silently miss every
    pair and read as a 0% disagreement rate.
    """
    for seed in CI_SEEDS:
        case = generate_case(seed)
        # ``BOARD_EDGE`` is the one name that is legitimately not a declared
        # net: it is the reserved pseudo-net both the oracle and the adapters
        # pair a one-sided ``copper_edge_clearance`` finding with, so that
        # every verdict stays a symmetric two-element set.
        declared = set(case.nets) | {BOARD_EDGE}
        for verdict in adapter.verdicts(case):
            assert isinstance(verdict, Verdict)
            assert verdict.nets <= declared, (
                f"{adapter.name} on seed {seed} named nets outside the case: "
                f"{sorted(verdict.nets - declared)}"
            )


# ---------------------------------------------------------------------------
# The measurement itself -- report-only until the consumer is switched
# ---------------------------------------------------------------------------


def _consumer_verdicts(adapter: ConsumerAdapter, case) -> set[frozenset[str]]:
    """The reading this item scores the adapter on.

    An **unmigrated** group is read exactly as the published table reads it:
    the consumer at its own rule values, with the result xfailed.  Both halves
    of a disagreement -- geometry and rule selection -- are evidence for that
    consumer's own phase, and separating them there would be premature.

    A **migrated** group is read at the clearance kicad-cli itself applies.
    Its migration PR changed *geometry* and nothing else (Epic #5509 scope
    guard #1 forbids touching a rule value), so gating it on the consumer's
    own resolved clearance would redden this build for the rule defect
    #5398 / #5654 tracks, which no Phase 3 PR is allowed to fix.  Pinning the
    rule axis to ground truth's own value is what makes the gate a statement
    about the thing the migration actually moved.
    """
    if adapter.group in MIGRATED_GROUPS:
        at_project = getattr(adapter, "verdicts_at_project_rules", None)
        assert at_project is not None, (
            f"{adapter.name} (group {adapter.group}) is in MIGRATED_GROUPS but "
            "does not implement verdicts_at_project_rules(case); a gated row "
            "must be drivable at kicad-cli's own rule value"
        )
        return {v.nets for v in at_project(case)}
    return {v.nets for v in adapter.verdicts(case)}


@requires_kicad_cli
@pytest.mark.consumer
@pytest.mark.parametrize("seed", CI_SEEDS)
@pytest.mark.parametrize("adapter", _ADAPTER_PARAMS)
def test_adapter_agrees_with_kicad_cli(adapter: ConsumerAdapter, seed: int, tmp_path: Path) -> None:
    """One consumer, one seed, against ground truth.

    Compared on pair identity only -- *did this model flag this pair at all?*
    Gaps are never compared across models: each computes its own, and the
    difference would be arithmetic noise rather than the question under test.

    Only the pairs the adapter declares itself in scope for
    (``ConsumerAdapter.pair_kinds``) are compared, and boundary-band pairs are
    excluded: within 1 um of the requirement the two models are arguing about
    rounding.  For an unmigrated group this mirrors ``report.measure_corpus``
    exactly, so a red (well, xfailed) item here corresponds to a non-zero cell
    in the published table.

    For a **migrated** group (``report.MIGRATED_GROUPS``) the item carries no
    ``xfail`` -- that is the flip its epic phase exists to make -- and the
    consumer is driven at kicad-cli's own rule value; see
    :func:`_consumer_verdicts` for why the two readings differ.  A gated
    adapter must compare something on at least one of ``CI_SEEDS``, or the
    gate is vacuous.

    Zone pairs are the one exception, and for the same reason the report
    scores them only on its refilled run: this item measures the *as-is* board,
    where an unfilled pour contributes no copper to kicad-cli at all, so a zone
    comparison here would score the fill state rather than the model.
    :func:`test_zone_pairs_are_measured_on_a_refilled_run` covers them.
    """
    case = generate_case(seed)
    board = write_case(case, tmp_path)
    result = run_oracle(board.pcb_path, refill=False, work_dir=tmp_path)

    truth = {v.nets for v in result.without_zones()}
    consumer = _consumer_verdicts(adapter, case)

    over: list[str] = []
    under: list[str] = []
    compared = 0
    for pair in case.pairs:
        if pair.kind not in adapter.pair_kinds or pair.boundary:
            continue
        if pair.kind in PairKind.ZONE:
            continue
        compared += 1
        in_truth = pair.nets in truth
        in_consumer = pair.nets in consumer
        if in_consumer and not in_truth:
            over.append(f"{pair.kind} {sorted(pair.nets)} gap={pair.target_gap_mm:.4f}")
        elif in_truth and not in_consumer:
            under.append(f"{pair.kind} {sorted(pair.nets)} gap={pair.target_gap_mm:.4f}")

    if adapter.group in MIGRATED_GROUPS and not compared:
        # A gate over an empty denominator is green for the wrong reason.  An
        # xfailed row can afford to be vacuous (the published table carries
        # the real denominator); a gated one cannot, because "no in-scope pair
        # on this seed" and "this consumer agrees" would look identical.
        #
        # The statement is made over ``CI_SEEDS`` rather than over this one
        # seed, because a consumer's ``pair_kinds`` can be rare enough that no
        # single seed carries one: group 6 sees only static placement copper
        # (``pad-seg`` / ``pad-via``) and seed 2 places no pad pair at all.
        # Requiring *every* seed to contribute would force CI_SEEDS to grow --
        # a kicad-cli process per seed per adapter -- to say something the
        # union already says.  Counting pairs needs no oracle, so this stays
        # cheap.
        elsewhere = sum(
            1
            for other in CI_SEEDS
            for pair in generate_case(other).pairs
            if pair.kind in adapter.pair_kinds
            and not pair.boundary
            and pair.kind not in PairKind.ZONE
        )
        assert elsewhere, (
            f"{adapter.name} (group {adapter.group}) is gated but no seed in "
            f"{CI_SEEDS} offers an in-scope pair to compare "
            f"(pair_kinds={sorted(adapter.pair_kinds)}) -- the gate is vacuous; "
            "re-pin CI_SEEDS or widen the scope"
        )

    gated = (
        " (GATED: this consumer is on the shared kernel)"
        if adapter.group in MIGRATED_GROUPS
        else ""
    )
    assert not over and not under, (
        f"{adapter.name} (group {adapter.group}) disagrees with kicad-cli on seed {seed}{gated}\n"
        f"  over-rejected (consumer flags, KiCad clean): {over}\n"
        f"  under-rejected (KiCad flags, consumer clean): {under}\n"
        f"{result.describe()}"
    )


#: One seed per consumer that claims a zone kind, chosen so each one really
#: carries a zone pair: seed 7 draws a ``via-zone`` pair (group 18 only) and
#: seed 22 a ``seg-zone`` one (groups 18 and 10).  Pinned rather than searched
#: for, so a generator change that stopped placing a kind fails loudly instead
#: of silently skipping the only item that measures it.
ZONE_PAIR_SEEDS = (7, 22)


@requires_kicad_cli
@pytest.mark.consumer
@pytest.mark.parametrize("seed", ZONE_PAIR_SEEDS)
def test_zone_pairs_are_measured_on_a_refilled_run(seed: int, tmp_path: Path) -> None:
    """The zone half of the table, exercised end to end on one seed per kind.

    Everything the report does differently for a zone pair is here: the oracle
    runs with ``refill=True``, ground truth comes from the verdicts that
    *involve* zone copper (rather than from ``without_zones()``), and only the
    adapters that claim a zone kind are asked.  Without this item the whole
    refilled-run path would be exercised only by the out-of-band table
    regeneration, which no PR runs.

    Report-only like every other consumer row -- a disagreement is evidence
    for group 18's or group 10's own epic phase, not a red build.
    """
    case = generate_case(seed)
    zone_pairs = [p for p in case.pairs if p.kind in PairKind.ZONE]
    assert zone_pairs, f"seed {seed} no longer carries a zone pair -- re-pin ZONE_PAIR_SEEDS"

    board = write_case(case, tmp_path)
    result = run_oracle(board.pcb_path, refill=True, work_dir=tmp_path)
    truth = {v.nets for v in result.verdicts if v.zone}

    findings: list[str] = []
    measured = 0
    for adapter in ADAPTERS:
        in_scope = [p for p in zone_pairs if p.kind in adapter.pair_kinds and not p.boundary]
        if not in_scope or not adapter.available():
            continue
        consumer = {v.nets for v in adapter.verdicts(case)}
        for pair in in_scope:
            measured += 1
            in_truth = pair.nets in truth
            in_consumer = pair.nets in consumer
            if in_consumer != in_truth:
                direction = "over" if in_consumer else "under"
                findings.append(
                    f"{adapter.name} (group {adapter.group}) {direction}-rejects "
                    f"{pair.kind} {sorted(pair.nets)} gap={pair.target_gap_mm:.4f}"
                )

    assert measured, (
        f"seed {seed}: no adapter claimed a zone pair -- the zone cells of the "
        "published table would be an empty denominator"
    )
    assert not findings, (
        f"zone-pair disagreements with kicad-cli on seed {seed} "
        f"(refilled):\n  " + "\n  ".join(findings) + f"\n{result.describe()}"
    )


# ---------------------------------------------------------------------------
# The kernel is NOT report-only -- it is a hard gate
# ---------------------------------------------------------------------------


@requires_kicad_cli
@pytest.mark.parametrize("seed", CI_SEEDS)
def test_kernel_agrees_with_kicad_cli(seed: int, tmp_path: Path) -> None:
    """The Phase 1b kernel, against ground truth, with **no** ``xfail``.

    Every consumer row above is deliberately non-strict-xfailed: a measured
    disagreement is evidence for a later phase, not a broken build.  The
    kernel is the exception, and the asymmetry is the point.  It is not a
    consumer awaiting migration -- it is the model every consumer is to be
    migrated *onto*, so a disagreement here is not a finding about a consumer,
    it is a **Phase 1b bug**: unifying onto a model that cannot match
    kicad-cli would replace nineteen wrong answers with one.

    Deliberately redundant with the ``consumer``-marked item above (the kernel
    is in ``ADAPTERS`` and is measured there too, for the table).  The
    duplicate costs no extra kicad-cli process worth caring about and buys the
    one thing the xfailed item cannot: a **red** build if the control row
    stops being zero.

    Per Epic #5509's scope guard, the fix for a failure here is a fixture
    under ``tests/fixtures/conformance/`` plus a kernel PR through
    ``tests/router/test_clearance_kernel_parity.py`` -- never a patch to the
    kernel from inside this phase, and never a relaxation of this assertion.
    """
    kernel = _ADAPTERS_BY_GROUP[KERNEL_GROUP]
    case = generate_case(seed)
    board = write_case(case, tmp_path)
    result = run_oracle(board.pcb_path, refill=False, work_dir=tmp_path)

    truth = {v.nets for v in result.without_zones()}
    verdicts = {v.nets for v in kernel.verdicts(case)}

    over: list[str] = []
    under: list[str] = []
    for pair in case.pairs:
        if pair.boundary or pair.kind not in kernel.pair_kinds:
            # The zone and edge kinds are out of this row's scope (#5644):
            # see ``KernelAdapter.pair_kinds`` on why widening it needs a
            # fill-aware kernel shape rather than a wider comparison here.
            continue
        in_truth = pair.nets in truth
        in_kernel = pair.nets in verdicts
        if in_kernel and not in_truth:
            over.append(f"{pair.kind} {sorted(pair.nets)} gap={pair.target_gap_mm:.4f}")
        elif in_truth and not in_kernel:
            under.append(f"{pair.kind} {sorted(pair.nets)} gap={pair.target_gap_mm:.4f}")

    assert not over and not under, (
        f"the Phase 1b clearance kernel disagrees with kicad-cli on seed {seed} "
        "-- this is a KERNEL bug, not a consumer finding\n"
        f"  over-rejected (kernel flags, KiCad clean): {over}\n"
        f"  under-rejected (KiCad flags, kernel clean): {under}\n"
        f"{result.describe()}\n"
        "Capture this seed as a fixture under tests/fixtures/conformance/ and "
        "fix the kernel in its own PR through "
        "tests/router/test_clearance_kernel_parity.py."
    )


# ---------------------------------------------------------------------------
# The published table regenerates deterministically
# ---------------------------------------------------------------------------


@requires_kicad_cli
def test_report_regenerates_byte_identically(tmp_path: Path) -> None:
    """Two runs over the same seed range produce identical bytes.

    Epic #5509 Phase 1c's determinism criterion, asserted **inside the
    suite**.  It cannot be a post-hoc diff of a CI artifact against
    ``docs/``: ``ci.yml``'s ``code`` path filter excludes ``docs/**`` and
    ``**/*.md`` (``:57-66``), so a PR that only regenerates the table never
    runs the ``test`` job at all and the diff would never be computed.  A
    check that does not run on the change it guards is not a gate.

    Kept to two seeds -- this is a determinism property of the generator, the
    oracle and every adapter, and it either holds on two cases or it does not
    hold at all.  Widening the range would multiply kicad-cli processes for no
    extra discrimination.
    """
    seeds = range(0, 2)
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"

    for out in (first, second):
        rc = main(["--seeds", f"{seeds.start}-{seeds.stop - 1}", "--out", str(out)])
        assert rc == 0, "the report generator refused to write"

    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8"), (
        "the conformance report is not reproducible: two runs over seeds "
        f"{seeds.start}-{seeds.stop - 1} produced different documents. Either an "
        "adapter, the generator or the oracle has run-to-run variation, and the "
        "committed table stops being evidence anyone can reproduce."
    )


# ---------------------------------------------------------------------------
# The auto-xfail mechanism -- and its migrated-group exception -- under test
# ---------------------------------------------------------------------------


def test_migrated_groups_are_wired_and_drivable_at_ground_truths_rule_value() -> None:
    """``MIGRATED_GROUPS`` names real, gated, project-rule-drivable adapters.

    Three static properties, none of which needs kicad-cli, so a migration
    registered without the plumbing to back it fails on any laptop:

    * every migrated group is one of the epic's nineteen (never the kernel
      control row, which was never a consumer to migrate);
    * every migrated group actually has an adapter -- listing an unwired group
      would flip a gate that has no row to gate;
    * that adapter implements ``verdicts_at_project_rules``, the reading
      ``_consumer_verdicts`` gates on.  Without it the gate would silently
      fall back to the consumer's own rule values and start failing for the
      #5654 rule defect instead of for a geometry disagreement.
    """
    consumer_groups = {g for g in WIRED_GROUPS if g != KERNEL_GROUP}
    assert not (MIGRATED_GROUPS - consumer_groups), (
        "MIGRATED_GROUPS names groups that are not wired consumer groups: "
        f"{sorted(MIGRATED_GROUPS - consumer_groups)}"
    )
    for group in sorted(MIGRATED_GROUPS):
        adapter = _ADAPTERS_BY_GROUP[group]
        assert hasattr(adapter, "verdicts_at_project_rules"), (
            f"{adapter.name} (group {group}) is migrated but cannot be driven "
            "at the project netclass clearance"
        )


def _consumer_items(request: pytest.FixtureRequest) -> list[pytest.Item]:
    items = [
        item for item in request.session.items if item.get_closest_marker("consumer") is not None
    ]
    if not items:
        pytest.skip(
            "no @pytest.mark.consumer items were collected -- this assertion "
            "is only meaningful for a whole-suite run (uv run pytest tests/conformance)"
        )
    return items


def test_every_unmigrated_consumer_item_carries_xfail(request: pytest.FixtureRequest) -> None:
    """``conftest.pytest_collection_modifyitems`` really fired.

    The hook is the *only* thing standing between a measured disagreement and
    a red build, and it shipped with no item to exercise it.  This walks the
    session's collected items rather than trusting the marker: every
    ``@pytest.mark.consumer`` item whose group is not yet migrated must also
    carry an ``xfail``, and that ``xfail`` must be non-strict (a strict one
    would fail the moment a consumer starts agreeing, which is the outcome the
    epic is working towards).
    """
    candidates = [i for i in _consumer_items(request) if item_group(i) not in MIGRATED_GROUPS]

    missing = [item.nodeid for item in candidates if item.get_closest_marker("xfail") is None]
    assert not missing, f"consumer items collected without an auto-xfail: {missing}"

    strict = [
        item.nodeid
        for item in candidates
        if item.get_closest_marker("xfail").kwargs.get("strict") is not False
    ]
    assert not strict, f"consumer items whose xfail is strict: {strict}"


def test_migrated_consumer_items_are_hard_gates(request: pytest.FixtureRequest) -> None:
    """The flip reached the collected items, not just the registry.

    The mirror image of the test above, and the assertion that makes "group N
    is no longer report-only" checkable rather than declared.  Registering a
    group in ``MIGRATED_GROUPS`` while the hook still xfails its items would
    leave the epic's Phase 3/4 acceptance criterion unmet *and* invisible --
    the suite would stay green either way.

    Skips rather than fails when the whole suite was not collected: with
    ``-k`` or a single-module run there may be no migrated item present, which
    says nothing about the mechanism.
    """
    gated = [i for i in _consumer_items(request) if item_group(i) in MIGRATED_GROUPS]
    if not gated:
        pytest.skip(
            "no migrated-group consumer items collected -- run the whole "
            "module (uv run pytest tests/conformance/test_corpus.py)"
        )

    still_xfailed = [item.nodeid for item in gated if item.get_closest_marker("xfail") is not None]
    assert not still_xfailed, (
        "these consumer items belong to a group in MIGRATED_GROUPS but are "
        f"still auto-xfailed, so their disagreement cannot redden a build: {still_xfailed}"
    )
