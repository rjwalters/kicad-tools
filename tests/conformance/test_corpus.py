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
from tests.conformance.board import write_case
from tests.conformance.conftest import requires_cpp, requires_kicad_cli
from tests.conformance.generator import PairKind, generate_case
from tests.conformance.oracle import run_oracle
from tests.conformance.report import ADAPTERS

# Kept smaller than ``test_corpus_truth.CI_SEEDS``: this module multiplies its
# seeds by five adapters, and every item launches its own kicad-cli.
CI_SEEDS = (0, 1, 2)

# The groups Epic #5509 section 1 assigns to the five wired consumers.
WIRED_GROUPS = {1, 4, 12, 13, 18}

_ADAPTER_PARAMS = [
    pytest.param(
        adapter,
        id=adapter.name,
        marks=(requires_cpp,) if adapter.name == "grid_cpp" else (),
    )
    for adapter in ADAPTERS
]


# ---------------------------------------------------------------------------
# Adapter contract -- hard assertions, no kicad-cli needed
# ---------------------------------------------------------------------------


def test_adapters_cover_the_five_wired_groups() -> None:
    """``ADAPTERS`` wires exactly the groups this phase set out to measure."""
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
