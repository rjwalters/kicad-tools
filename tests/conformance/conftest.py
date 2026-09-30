"""Shared gating for the conformance suite.

Two things happen here, and both are load-bearing for the epic's "report-only"
guarantee.

**kicad-cli gating is a skip, never a pass.**  A test that needs ground truth
cannot conclude anything without it, so it skips with a visible reason rather
than trivially passing.  Same pattern as ``tests/test_kicad_cli_roundtrip.py``,
exported here as :data:`requires_kicad_cli` so each module opts in explicitly.
Modules that genuinely do not need the tool -- the canonicaliser unit test
against a checked-in report, the table renderer, the determinism check -- run
everywhere, including on a laptop with no KiCad installed.

**Consumer rows are automatically ``xfail(strict=False)`` -- until their
consumer is switched.**  Every adapter-vs-truth test carries
``@pytest.mark.consumer``; the collection hook below attaches a non-strict
``xfail`` to each of them.  That is the mechanism, not a convention -- a
future adapter test cannot accidentally turn a *measured* disagreement into a
red build before its consumer has been switched to the shared kernel in its
own epic phase.  kicad-cli-truth assertions carry no such marker and are hard
failures.

The hook's **one exception** is a group listed in
:data:`~tests.conformance.report.MIGRATED_GROUPS`: that consumer has had its
Phase 3/4 PR, so its rows are deliberately left un-``xfail``\\ ed and a
disagreement reddens the build.  The exception is keyed off the item's own
``adapter`` parameter rather than off a second marker, so a migrated group
cannot be flipped in one place and forgotten in the other -- there is exactly
one registry.

``test_corpus.py`` asserts both halves over the whole collected suite
(``test_every_unmigrated_consumer_item_carries_xfail`` and
``test_migrated_consumer_items_are_hard_gates``), so the mechanism is itself
under test rather than assumed.

What hardens for a migrated group is exactly Epic #5509's Phase 3 goal: *a
search-time reject of a candidate the validator accepts must become a test
failure*.  The rule axis does not harden with it -- the gated reading drives
the consumer at ``case.rules.project_clearance``, kicad-cli's own value, so a
disagreement can only be geometry.  An under-rejection at the consumer's
*own* ``trace_clearance`` is a rule-resolution gap Phase 2's resolver owns,
and one a migration phase may not close by changing a rule value (scope guard
#1); it stays in the published table's percentages rather than in the gate.

**A migrated group may carry a quantisation ledger** -- and only groups 1 and
2 do.  Their consumer answers with a *cell set* (grid occupancy, switched onto
the kernel's exact dilation by #5660, Phase 3a), and a cell-quantised model
over-rejects by construction: its halo radius rounds outwards, and the
adapter dilates the candidate as well as the existing copper.  Demanding zero
over-rejection there would demand the row be deleted rather than gated, so
:func:`assert_migrated_group_agrees` holds such a row to *zero
under-rejection, always* plus *exactly* the over-rejection its ledger records
(``test_corpus.QUANTISATION_LEDGER`` / ``test_named_fixtures.FIXTURE_QUANTISATION_LEDGER``).
A group with no ledger entry is held to an empty one, which is the plain
zero-disagreement gate above -- so the ledger widens nothing for groups 6-10.
"""

from __future__ import annotations

import pytest

from tests.conformance.adapters.grid_cpp import GridCppAdapter
from tests.conformance.oracle import kicad_cli_available
from tests.conformance.report import MIGRATED_GROUPS

requires_kicad_cli = pytest.mark.skipif(not kicad_cli_available(), reason="kicad-cli not installed")
"""Module-level gate for tests that need ``kicad-cli`` as ground truth."""

requires_cpp = pytest.mark.skipif(
    not GridCppAdapter().available(), reason="C++ router backend not built"
)
"""Gate for rows that drive ``router_cpp.Grid3D`` (the ``grid_cpp`` adapter).

The capability probe is the adapter's own :meth:`available`, so a machine
where the extension is missing skips exactly the rows the report would have
rendered ``not measured`` -- one answer, not two.  Build it with
``uv run kct build-native``.
"""


def requires_adapter(adapter: object) -> pytest.MarkDecorator:
    """Skip mark derived from *this* adapter's own capability probe.

    The generalisation of :data:`requires_cpp` to every adapter, and the
    reason Phase 1c does not accumulate one hand-written ``skipif`` per
    native consumer.  Three adapters now need a compiled extension and they
    do not all need the *same* one -- ``drc_cpp`` probes
    ``drc/cpp_backend.is_cpp_available`` while the grid adapters probe
    ``router_cpp`` -- so hard-coding a single router-extension mark would
    skip the wrong rows on a half-built tree.

    Keeping the probe as ``adapter.available()`` preserves the invariant that
    matters: a row the *report* would render ``not measured`` is exactly a row
    the *suite* skips.  One answer, not two.
    """
    probe = getattr(adapter, "available", None)
    available = True if probe is None else bool(probe())
    name = getattr(adapter, "name", repr(adapter))
    return pytest.mark.skipif(
        not available,
        reason=f"adapter {name} is unavailable here (run `uv run kct build-native`)",
    )


CONSUMER_XFAIL_REASON = "consumer verdict is report-only until that consumer's epic phase (#5509)"


def item_group(item: pytest.Item) -> int | None:
    """The Epic #5509 group an item measures, from its adapter parameter.

    Two parametrisation styles are in use and both resolve: ``test_corpus.py``
    passes the adapter *object* as ``adapter`` (so ``.group`` is right there),
    while ``test_named_fixtures.py`` passes the adapter's *name* as
    ``adapter_name``, because its params double as readable test ids.

    ``None`` for an item that is not parametrised by a single adapter -- the
    refilled-zone row, for instance, loops over every adapter inside one item,
    so it belongs to no single group and stays report-only regardless.

    Exported (rather than private) because ``test_corpus.py`` walks the
    collected session with the *same* rule to assert the hook fired; two
    different notions of "which group is this item" would let the assertion
    pass while the gate it checks was never applied.
    """
    callspec = getattr(item, "callspec", None)
    if callspec is None:
        return None
    group = getattr(callspec.params.get("adapter"), "group", None)
    if isinstance(group, int):
        return group
    name = callspec.params.get("adapter_name")
    if isinstance(name, str):
        from tests.conformance.report import ADAPTERS

        for adapter in ADAPTERS:
            if adapter.name == name:
                return adapter.group
    return None


def assert_migrated_group_agrees(
    adapter: object,
    *,
    over: list[str],
    under: list[str],
    expected_over: tuple[str, ...] = (),
    what: str,
    context: str = "",
) -> None:
    """The gate a migrated group's rows are held to, in place of the xfail.

    Two clauses, deliberately asymmetric because the two directions of
    disagreement mean opposite things:

    * **Under-rejection is never permitted.**  The consumer accepting copper
      kicad-cli flags is the failure mode the whole epic exists to remove, and
      no quantisation argument excuses it -- a coarse model errs outwards.
      There is no ledger entry that can allow one.
    * **Over-rejection is permitted only where it is already recorded.**
      *expected_over* is empty for every group except the cell-quantised
      occupancy rows (groups 1 and 2), so for the rest this is exactly the
      zero-disagreement gate.  Pinning the exact set rather than a budget means
      any *new* over-rejection, and any that silently disappears, turns the
      build red with the evidence that has to be re-measured.

    Args:
        adapter: The adapter under test; used for the failure message only.
        over: Over-rejection descriptions, as the caller formats them.
        under: Under-rejection descriptions, same format.
        expected_over: The pinned over-rejection set for this (group, case).
        what: Human label for the case, e.g. ``"seed 2"``.
        context: Optional extra provenance appended to a failure.
    """
    group = getattr(adapter, "group", None)
    name = getattr(adapter, "name", repr(adapter))
    suffix = f"\n{context}" if context else ""

    assert not under, (
        f"{name} (group {group}) UNDER-rejects on {what}: it accepts copper "
        f"kicad-cli flags (GATED: this consumer is on the shared kernel).\n"
        f"  under-rejected (KiCad flags, consumer clean): {under}\n"
        f"  over-rejected (consumer flags, KiCad clean): {over}\n"
        "An under-rejection is a real defect, never a ledger entry."
        f"{suffix}"
    )
    assert sorted(over) == sorted(expected_over), (
        f"{name} (group {group}) over-rejects differently than recorded on {what} "
        "(GATED: this consumer is on the shared kernel).\n"
        f"  measured: {sorted(over)}\n"
        f"  recorded: {sorted(expected_over)}\n"
        "A new entry means the consumer grew; a missing one means it shrank. "
        "Either way re-measure, re-pin the quantisation ledger (groups 1/2 "
        "only -- every other migrated group is held to none), and regenerate "
        f"docs/clearance-conformance.md in the same PR.{suffix}"
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "consumer: an adapter-vs-kicad-cli comparison; report-only (auto-xfail) "
        "unless its group is in report.MIGRATED_GROUPS",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-``xfail`` every ``consumer`` item whose group is not yet migrated."""
    del config
    for item in items:
        if item.get_closest_marker("consumer") is None:
            continue
        if item_group(item) in MIGRATED_GROUPS:
            # Switched to the kernel in its own epic phase: the whole point of
            # that phase is that this row stops being allowed to disagree.
            continue
        item.add_marker(pytest.mark.xfail(strict=False, reason=CONSUMER_XFAIL_REASON))
