"""Shared gating for the conformance suite.

Three things happen here, and all of them are load-bearing for the epic's
"report-only until switched" guarantee.

**kicad-cli gating is a skip, never a pass.**  A test that needs ground truth
cannot conclude anything without it, so it skips with a visible reason rather
than trivially passing.  Same pattern as ``tests/test_kicad_cli_roundtrip.py``,
exported here as :data:`requires_kicad_cli` so each module opts in explicitly.
Modules that genuinely do not need the tool -- the canonicaliser unit test
against a checked-in report, the table renderer, the determinism check -- run
everywhere, including on a laptop with no KiCad installed.

**An unswitched consumer's rows are automatically ``xfail(strict=False)``.**
Every adapter-vs-truth test carries ``@pytest.mark.consumer``; the collection
hook below attaches a non-strict ``xfail`` to each of them.  That is the
mechanism, not a convention -- a future adapter test cannot accidentally turn a
*measured* disagreement into a red build before its consumer has been switched
to the shared kernel in its own epic phase.  kicad-cli-truth assertions carry
no such marker and are hard failures.

**A switched consumer's rows are gated.**  Epic #5509's scope guard #5 is
"report-only *until switched*", and until Phase 3a nothing implemented the
second half of that sentence: the hook applied its blanket ``xfail`` to every
consumer item, so a consumer that had been migrated would keep XPASSing
silently and a regression in it would stay invisible.  :data:`SWITCHED_GROUPS`
is the ledger of groups whose phase has landed; the hook skips them, and the
modules that own the measurement (``test_corpus.py``, ``test_named_fixtures.py``)
assert their verdicts against a pinned expectation instead.  Adding a group
here is how a Phase 3/4 PR makes its own consumer a merge gate.

``test_corpus.py``'s ``test_every_unswitched_consumer_item_carries_xfail`` and
``test_switched_group_items_are_not_xfailed`` assert both halves over the
whole collected suite, so the mechanism is itself under test rather than
assumed.
"""

from __future__ import annotations

import pytest

from tests.conformance.adapters.grid_cpp import GridCppAdapter
from tests.conformance.oracle import kicad_cli_available

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


SWITCHED_GROUPS: dict[int, str] = {
    1: "#5660 -- Epic #5509 Phase 3a: `router/grid.py` halo marking",
    2: "#5660 -- Epic #5509 Phase 3a: `router/cpp/src/grid.cpp` halo marking",
}
"""Epic #5509 group numbers whose consumer has been switched to the kernel.

The ledger a Phase 3/4 PR appends to when it migrates its consumer.  Membership
has exactly one effect and it is a big one: the group's rows stop being
``xfail(strict=False)`` and become merge gates, so from that PR onwards a
change to that consumer's clearance geometry has to be argued for in the
conformance suite rather than measured after the fact.

The value is the phase that switched the group, rendered into failure messages
so a reader who trips the gate knows which PR to read.
"""


def group_of_item(item: pytest.Item, group_by_adapter_name: dict[str, int]) -> int | None:
    """The Epic #5509 group a parametrised consumer item measures, if any.

    Two parametrisation styles are in use and both have to resolve:
    ``test_corpus.py`` passes the adapter *object* (so ``.group`` is right
    there), while ``test_named_fixtures.py`` passes the adapter's *name* as a
    string, because its params double as readable test ids.  Anything else --
    an unparametrised item, or a parametrised one that names no adapter --
    answers ``None`` and keeps the blanket report-only treatment.

    Args:
        item: A collected pytest item.
        group_by_adapter_name: ``adapter.name -> adapter.group``, passed in
            rather than imported at module scope so this helper stays free of
            an import cycle with ``report.py``.

    Returns:
        The group number, or ``None`` when the item names no adapter.
    """
    callspec = getattr(item, "callspec", None)
    if callspec is None:
        return None
    for value in callspec.params.values():
        group = getattr(value, "group", None)
        if isinstance(group, int):
            return group
        if isinstance(value, str) and value in group_by_adapter_name:
            return group_by_adapter_name[value]
    return None


def assert_switched_group_matches_ledger(
    adapter: object,
    *,
    over: list[str],
    under: list[str],
    expected: tuple[str, ...],
    what: str,
    context: str = "",
) -> None:
    """The gate a switched group's rows are held to, in place of the xfail.

    Two clauses, and they are deliberately asymmetric because the two
    directions of disagreement mean opposite things:

    * **Under-rejection is never permitted.**  The consumer accepting copper
      kicad-cli flags is the failure mode the whole epic exists to remove, and
      no quantisation argument excuses it -- a coarse model errs outwards.
      There is no ledger entry that can allow one.
    * **Over-rejection is permitted only where it is already recorded.**  A
      cell-quantised occupancy model over-rejects by construction (its halo
      radius rounds outwards, and the adapter dilates the candidate as well as
      the existing copper), so demanding zero here would demand the row be
      deleted rather than gated.  Pinning the exact set instead means any *new*
      over-rejection, and any that silently disappears, turns the build red
      with the evidence that has to be re-measured.

    Args:
        adapter: The adapter under test; used for the failure message only.
        over: Over-rejection descriptions, as the caller formats them.
        under: Under-rejection descriptions, same format.
        expected: The pinned over-rejection set for this (group, case).
        what: Human label for the case, e.g. ``"seed 2"``.
        context: Optional extra provenance appended to a failure.
    """
    group = getattr(adapter, "group", None)
    name = getattr(adapter, "name", repr(adapter))
    phase = SWITCHED_GROUPS.get(group, "") if isinstance(group, int) else ""
    suffix = f"\n{context}" if context else ""

    assert not under, (
        f"{name} (group {group}) UNDER-rejects on {what}: it accepts copper "
        f"kicad-cli flags.\n  {under}\n"
        f"This group was switched to the clearance kernel by {phase} and is a "
        "merge gate; an under-rejection is a real defect, never a ledger "
        f"entry.{suffix}"
    )
    assert sorted(over) == sorted(expected), (
        f"{name} (group {group}) over-rejects differently than recorded on {what}.\n"
        f"  measured: {sorted(over)}\n"
        f"  recorded: {sorted(expected)}\n"
        f"This group was switched to the clearance kernel by {phase}; the "
        "recorded set is the residual cell-quantisation over-rejection "
        "measured when it was switched. A new entry means the halo grew; a "
        "missing one means it shrank. Either way re-measure, re-pin the "
        "ledger, and regenerate docs/clearance-conformance.md in the same "
        f"PR.{suffix}"
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "consumer: an adapter-vs-kicad-cli comparison; report-only unless its "
        "group is in SWITCHED_GROUPS, in which case it is a gate",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-xfail every ``consumer`` item whose group is not yet switched.

    The exemption is deliberately keyed on the *group*, not on the test
    function: one switched consumer must become a gate without dragging the
    eighteen groups it shares a parametrised test with along with it.
    """
    del config
    from tests.conformance.report import ADAPTERS

    group_by_name = {adapter.name: adapter.group for adapter in ADAPTERS}
    for item in items:
        if item.get_closest_marker("consumer") is None:
            continue
        if group_of_item(item, group_by_name) in SWITCHED_GROUPS:
            continue
        item.add_marker(pytest.mark.xfail(strict=False, reason=CONSUMER_XFAIL_REASON))
