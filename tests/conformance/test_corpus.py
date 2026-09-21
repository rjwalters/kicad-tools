"""Each wired adapter, measured against kicad-cli over the smoke corpus.

``test_corpus_truth.py`` proved the *harness* is honest: the geometry the
generator intends is the geometry KiCad measures.  This module is the layer
above it -- the same seeded cases, but now asking each in-tree clearance
consumer the question kicad-cli already answered.

**Every adapter-vs-truth row is report-only.**  Each carries
``@pytest.mark.consumer``, and ``conftest.py``'s collection hook turns that
into ``xfail(strict=False)``.  A measured disagreement must not redden the
build: consumers are switched to the shared kernel in their own Epic #5509
phase, and only then does a row become a merge gate.  That mechanism landed
untested in PR #5532 (there were no consumer items to exercise it), so
:func:`test_every_consumer_item_carries_xfail` asserts it over the whole
collected suite.

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

from tests.conformance.adapters import ConsumerAdapter, Verdict
from tests.conformance.adapters.kernel import KERNEL_GROUP
from tests.conformance.board import write_case
from tests.conformance.conftest import requires_adapter, requires_kicad_cli
from tests.conformance.generator import PairKind, generate_case
from tests.conformance.oracle import run_oracle
from tests.conformance.report import ADAPTERS, main

_ADAPTERS_BY_GROUP = {adapter.group: adapter for adapter in ADAPTERS}

# Kept smaller than ``test_corpus_truth.CI_SEEDS``: this module multiplies its
# seeds by every wired adapter, and every item launches its own kicad-cli.
CI_SEEDS = (0, 1, 2)

# The Epic #5509 section-1 groups Phase 1c leaves unmeasured, with their
# reasons recorded in ``report.NOT_MEASURED_REASONS``: 7 is unexposed (a
# lambda inside the C++ coupled search), and 8-10 need engine plumbing
# (``Autorouter`` + ``DiffPairRouter``, ``LatticePathfinder.from_board``,
# ``MeshPathfinder.from_board``) that is this phase's deferred slice.
UNWIRED_GROUPS = {7, 8, 9, 10}

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
        declared = set(case.nets)
        for verdict in adapter.verdicts(case):
            assert isinstance(verdict, Verdict)
            assert verdict.nets <= declared, (
                f"{adapter.name} on seed {seed} named nets outside the case: "
                f"{sorted(verdict.nets - declared)}"
            )


# ---------------------------------------------------------------------------
# The measurement itself -- report-only, auto-xfail
# ---------------------------------------------------------------------------


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
    rounding.  This mirrors ``report.measure_corpus`` exactly, so a red (well,
    xfailed) item here corresponds to a non-zero cell in the published table.
    """
    case = generate_case(seed)
    board = write_case(case, tmp_path)
    result = run_oracle(board.pcb_path, refill=False, work_dir=tmp_path)

    truth = {v.nets for v in result.without_zones()}
    consumer = {v.nets for v in adapter.verdicts(case)}

    over: list[str] = []
    under: list[str] = []
    for pair in case.pairs:
        if pair.kind not in adapter.pair_kinds or pair.boundary:
            continue
        in_truth = pair.nets in truth
        in_consumer = pair.nets in consumer
        if in_consumer and not in_truth:
            over.append(f"{pair.kind} {sorted(pair.nets)} gap={pair.target_gap_mm:.4f}")
        elif in_truth and not in_consumer:
            under.append(f"{pair.kind} {sorted(pair.nets)} gap={pair.target_gap_mm:.4f}")

    assert not over and not under, (
        f"{adapter.name} (group {adapter.group}) disagrees with kicad-cli on seed {seed}\n"
        f"  over-rejected (consumer flags, KiCad clean): {over}\n"
        f"  under-rejected (KiCad flags, consumer clean): {under}\n"
        f"{result.describe()}"
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
        if pair.boundary:
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
# The auto-xfail mechanism is itself under test
# ---------------------------------------------------------------------------


def test_every_consumer_item_carries_xfail(request: pytest.FixtureRequest) -> None:
    """``conftest.pytest_collection_modifyitems`` really fired.

    The hook is the *only* thing standing between a measured disagreement and
    a red build, and it shipped with no item to exercise it.  This walks the
    session's collected items rather than trusting the marker: every
    ``@pytest.mark.consumer`` item must also carry an ``xfail``, and that
    ``xfail`` must be non-strict (a strict one would fail the moment a
    consumer starts agreeing, which is the outcome the epic is working
    towards).
    """
    consumer_items = [
        item for item in request.session.items if item.get_closest_marker("consumer") is not None
    ]
    if not consumer_items:
        pytest.skip(
            "no @pytest.mark.consumer items were collected -- this assertion "
            "is only meaningful for a whole-suite run (uv run pytest tests/conformance)"
        )

    missing = [item.nodeid for item in consumer_items if item.get_closest_marker("xfail") is None]
    assert not missing, f"consumer items collected without an auto-xfail: {missing}"

    strict = [
        item.nodeid
        for item in consumer_items
        if item.get_closest_marker("xfail").kwargs.get("strict") is not False
    ]
    assert not strict, f"consumer items whose xfail is strict: {strict}"
