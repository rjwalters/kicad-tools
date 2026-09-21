"""The conformance document's shape is an acceptance criterion in itself.

Epic #5509 section 1 inventories nineteen implementation groups that carry
their own clearance arithmetic. The table must show **all nineteen** -- groups
with no adapter read ``not measured``, never disappear. An omitted row reads
as "this one agrees", and we do not know that; that gap is precisely how a
stream of one-site "router: preserve physical X" fixes stayed invisible as a
class.

These tests pin the shape (rows, columns, markers) against a stub measurement,
so the contract holds before any adapter exists and keeps holding after.

No kicad-cli needed.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from tests.conformance.report import (
    ADAPTERS,
    DOC_PATH,
    GROUPS,
    KERNEL_GROUP,
    KERNEL_TABLE_BEGIN,
    KERNEL_TABLE_END,
    NOT_MEASURED,
    NOT_MEASURED_REASONS,
    NOTES,
    TABLE_BEGIN,
    TABLE_COLUMNS,
    TABLE_END,
    AdapterMeasurement,
    CorpusStats,
    adapter_for_group,
    main,
    render_document,
    render_kernel_table,
    render_table,
)

_STUB_STATS = CorpusStats(
    seed_range="0-23",
    case_count=24,
    pair_count=72,
    boundary_count=0,
    flagged_pair_count=31,
    fill_states=("as-is", "refilled"),
    oracle_runs=48,
)


def _table_rows(markdown: str) -> list[str]:
    body = markdown.split(TABLE_BEGIN, 1)[1].split(TABLE_END, 1)[0]
    lines = [line for line in body.strip().splitlines() if line.startswith("|")]
    # Drop the header and the separator.
    return lines[2:]


def test_groups_are_the_nineteen_from_the_epic() -> None:
    assert len(GROUPS) == 19
    assert [g.number for g in GROUPS] == list(range(1, 20))
    assert len({g.label for g in GROUPS}) == 19


def test_table_has_one_row_per_group() -> None:
    rows = _table_rows(render_table(_STUB_STATS))
    assert len(rows) == len(GROUPS) == 19


def test_table_columns_match_the_specified_seven() -> None:
    """Six data columns plus ``notes``.

    ``notes`` is not decoration: Epic #5509 Phase 1c's acceptance criterion is
    that every row is *either* measured *or* states its reason, and the column
    is where that reason lives.  Without it, "not measured" is
    indistinguishable from "nobody looked" -- exactly the invisibility the
    document exists to remove.
    """
    table = render_table(_STUB_STATS)
    header = table.splitlines()[1]
    columns = [c.strip() for c in header.strip("|").split("|")]
    assert columns == list(TABLE_COLUMNS)
    assert columns == [
        "adapter",
        "corpus (seed range)",
        "over-reject %",
        "under-reject %",
        "boundary",
        "fill state",
        "notes",
    ]
    for row in _table_rows(table):
        assert len(row.strip("|").split("|")) == len(TABLE_COLUMNS)


def test_groups_without_an_adapter_say_not_measured() -> None:
    """The unmeasured groups are named explicitly, not inferred."""
    measured = {
        12: AdapterMeasurement(
            group=12,
            adapter_name="grid_py",
            pairs_compared=72,
            over_reject=3,
            under_reject=5,
            boundary=0,
            fill_states=("as-is", "refilled"),
        )
    }
    rows = _table_rows(render_table(_STUB_STATS, measured))
    by_group = {int(re.match(r"\|\s*(\d+)\.", row).group(1)): row for row in rows}  # type: ignore[union-attr]

    assert "grid_py" in by_group[12]
    assert "4.2% (3/72)" in by_group[12]  # over-reject
    assert "6.9% (5/72)" in by_group[12]  # under-reject

    for group in (2, 3, 5, 6, 7, 8, 9, 10, 11, 14, 15, 16, 17, 19):
        # Five data columns read ``not measured``; the sixth (notes) carries
        # the reason, which is why the count is five and not six.
        assert by_group[group].count(NOT_MEASURED) >= 5, (
            f"group {group} must read '{NOT_MEASURED}' in all five data columns"
        )
    # Group 18 has no adapter in the stub either, and must still be present.
    assert NOT_MEASURED in by_group[18]


def test_every_unmeasured_row_states_a_reason() -> None:
    """No row may read a bare ``not measured``.

    The render-time half of the criterion: whatever the cause -- no adapter
    wired at all, or one wired but unavailable on this machine -- the
    ``notes`` cell is non-empty and is not itself the string ``not measured``.
    """
    rows = _table_rows(render_table(_STUB_STATS))
    for row in rows:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        note = cells[-1]
        assert note, f"row without a stated reason: {row}"
        assert note != NOT_MEASURED, f"row whose reason is itself the placeholder: {row}"


def test_every_unwired_group_states_its_reason() -> None:
    """The *static* half: an unwired group needs an explicit, authored reason.

    Deliberately independent of any measurement, so it fails on a laptop with
    no KiCad and no compiled extension -- i.e. wherever a new unwired group
    would be introduced.  ``not_measured_reason``'s machine-fact fallback is
    only legitimate for a group that *has* an adapter; a group with none must
    justify itself in ``NOT_MEASURED_REASONS``.
    """
    unwired = [g.number for g in GROUPS if adapter_for_group(g.number) is None]
    missing = [n for n in unwired if n not in NOT_MEASURED_REASONS]
    assert not missing, (
        f"groups {missing} have no adapter and no entry in NOT_MEASURED_REASONS; "
        'a bare "not measured" is indistinguishable from nobody having looked'
    )
    # Every recorded reason must classify itself, so a reader can tell an
    # unreachable consumer from a deferred one at a glance.
    for number, reason in NOT_MEASURED_REASONS.items():
        assert any(
            kind in reason for kind in ("unexposed", "needs engine plumbing", "out of corpus scope")
        ), f"group {number}'s reason does not classify the gap: {reason}"


def test_group_seven_is_the_only_unexposed_consumer() -> None:
    """Epic #5509's Phase 1c criterion, asserted rather than asserted-about.

    ``rail_clear`` is a lambda inside ``coupled_pathfinder.cpp``'s search
    loop, so no binding can reach it without adding C++ -- which this phase
    forbids.  Every *other* gap must be something else (deferred plumbing, a
    corpus-scope limit); if a second "unexposed" reason ever appears, either a
    binding regressed or the classification is being used to excuse a gap
    that has another cause.
    """
    unexposed = [n for n, reason in NOT_MEASURED_REASONS.items() if "unexposed" in reason]
    assert unexposed == [7], f"expected only group 7 to be unexposed, got {unexposed}"


def test_adapters_are_registered_for_the_wired_groups() -> None:
    """``ADAPTERS`` wires one consumer per measured group, and no more.

    Succeeds ``test_phase_1a_registers_no_adapters`` (#5513 / PR #5532), which
    pinned ``ADAPTERS == ()`` while only the truth side existed, and the
    five-group pin that replaced it in #5533.  Each was a *phase* assertion:
    Phase 1c wires eleven more consumer groups plus the Phase 1b kernel, and
    the remaining four (7-10) are stated gaps.

    The invariant the original test really protected -- that the harness does
    not import the code it measures -- is unchanged and still enforced, one
    test down, by ``test_truth_side_does_not_import_the_code_it_measures``.
    """
    consumer_groups = {adapter.group for adapter in ADAPTERS} - {KERNEL_GROUP}
    assert consumer_groups == {1, 2, 3, 4, 5, 6, 11, 12, 13, 14, 15, 16, 17, 18, 19}
    assert consumer_groups | set(NOT_MEASURED_REASONS) == {g.number for g in GROUPS}, (
        "every group is either wired or has a stated reason -- no group may be both or neither"
    )
    assert [adapter.group for adapter in ADAPTERS] == sorted(a.group for a in ADAPTERS), (
        "keep ADAPTERS in group order so the table's measured rows read top-to-bottom"
    )
    assert len({adapter.name for adapter in ADAPTERS}) == len(ADAPTERS)


def test_the_kernel_renders_in_its_own_table() -> None:
    """The kernel is measured, but it is not a twentieth group row.

    Mixing it into the group table would break the one invariant that makes
    the table auditable -- exactly nineteen rows, one per epic group -- and
    would read as if the kernel were another consumer to be migrated rather
    than the thing they are migrated *onto*.
    """
    measured = {
        KERNEL_GROUP: AdapterMeasurement(
            group=KERNEL_GROUP,
            adapter_name="clearance_kernel",
            pairs_compared=596,
            over_reject=0,
            under_reject=0,
            boundary=0,
            fill_states=("as-is", "refilled"),
        )
    }
    assert len(_table_rows(render_table(_STUB_STATS, measured))) == 19

    kernel = render_kernel_table(_STUB_STATS, measured)
    assert KERNEL_TABLE_BEGIN in kernel and KERNEL_TABLE_END in kernel
    body = kernel.split(KERNEL_TABLE_BEGIN, 1)[1].split(KERNEL_TABLE_END, 1)[0]
    rows = [line for line in body.strip().splitlines() if line.startswith("|")][2:]
    assert len(rows) == 1
    assert "clearance_kernel" in rows[0]
    assert "0.0% (0/596)" in rows[0]

    doc = render_document(_STUB_STATS, measured)
    assert KERNEL_TABLE_BEGIN in doc
    assert NOTES[KERNEL_GROUP].split(":")[0] in doc


def test_truth_side_does_not_import_the_code_it_measures() -> None:
    """The oracle must not depend on any consumer, checked mechanically.

    A conformance oracle that imports the implementations under test can drift
    towards them: a shared constant, a shared helper, a shared bug. The rule is
    that the truth side sees only KiCad's answer and the case it generated.

    This is the split line for the adapters too -- an adapter module is
    exactly the place where ``import kicad_tools.router`` becomes legitimate,
    so this test is scoped to the harness modules and deliberately exempts the
    ``adapters/`` package.  ``report.py`` imports the adapter *modules* (to
    populate ``ADAPTERS``) and that is fine: the forbidden thing is a harness
    module reaching into a consumer directly, not a harness module composing
    the wrappers.
    """
    package = Path(__file__).resolve().parent
    forbidden = ("kicad_tools.router", "kicad_tools.validate")
    offenders: list[str] = []

    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if any(name == f or name.startswith(f + ".") for f in forbidden):
                    offenders.append(f"{path.name}:{node.lineno} imports {name}")

    assert not offenders, (
        "the conformance harness must not import the consumers it measures; "
        f"move these into an adapter module: {offenders}"
    )


def test_document_mentions_every_group_and_both_failure_directions() -> None:
    doc = render_document(_STUB_STATS)
    assert doc.count(TABLE_BEGIN) == 1 and doc.count(TABLE_END) == 1
    assert len(_table_rows(doc)) == 19
    for group in GROUPS:
        assert group.label in doc
    assert "over-reject" in doc and "under-reject" in doc
    assert "report-only" in doc
    assert "Seed range: `0-23`" in doc
    assert "24 generated cases" in doc


def test_committed_document_is_up_to_date_in_shape() -> None:
    """The checked-in doc has the same 19 rows and columns as the renderer.

    Shape only, deliberately: the numbers are regenerated out-of-band over
    seeds 0-199 (~400 kicad-cli invocations) and this test runs on every PR,
    including on machines with no KiCad.  Their *reproducibility* is what
    ``test_corpus.test_report_regenerates_byte_identically`` asserts.
    """
    assert DOC_PATH.exists(), f"missing {DOC_PATH}"
    doc = DOC_PATH.read_text(encoding="utf-8")
    rows = _table_rows(doc)
    assert len(rows) == 19
    numbers = [int(re.match(r"\|\s*(\d+)\.", row).group(1)) for row in rows]  # type: ignore[union-attr]
    assert numbers == list(range(1, 20))

    header = doc.split(TABLE_BEGIN, 1)[1].strip().splitlines()[0]
    assert [c.strip() for c in header.strip("|").split("|")] == list(TABLE_COLUMNS), (
        "the committed table's columns have drifted from the renderer; "
        "regenerate with `uv run python -m tests.conformance.report --seeds 0-199`"
    )
    for row in rows:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        assert len(cells) == len(TABLE_COLUMNS)
        assert cells[-1], f"committed row with an empty notes cell: {row}"


def test_committed_document_carries_the_kernel_control_row() -> None:
    """The kernel's 0 %/0 % row is in the committed document, not just in code.

    Epic #5509 Phase 1c's central claim -- that one exact-geometry model *can*
    agree with kicad-cli everywhere -- is only evidence if it is written down
    where a reader of the epic will find it.  A regeneration that silently
    dropped the section (an adapter that stopped being ``available()`` on the
    generating machine, say) would leave the claim unsupported while every
    other check stayed green.
    """
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert KERNEL_TABLE_BEGIN in doc and KERNEL_TABLE_END in doc, (
        "the committed document has no kernel section; regenerate it on a "
        "machine where the `clearance_kernel` adapter is available"
    )
    body = doc.split(KERNEL_TABLE_BEGIN, 1)[1].split(KERNEL_TABLE_END, 1)[0]
    rows = [line for line in body.strip().splitlines() if line.startswith("|")][2:]
    assert len(rows) == 1, f"expected exactly one kernel row, got {len(rows)}"
    (row,) = rows
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    over, under = cells[2], cells[3]
    assert over.startswith("0.0%"), (
        f"the kernel over-rejects in the committed table ({over}) -- that is a "
        "Phase 1b bug, not a Phase 1c finding: capture a fixture and fix the "
        "kernel through tests/router/test_clearance_kernel_parity.py"
    )
    assert under.startswith("0.0%"), (
        f"the kernel under-rejects in the committed table ({under}) -- see above"
    )


def test_report_refuses_to_write_without_ground_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A table generated with no kicad-cli would read as "zero disagreement".

    That is a confident wrong answer, so the generator exits non-zero with the
    machine-readable ``kicad_cli_absent`` reason and writes nothing.
    """
    monkeypatch.setattr("tests.conformance.report.kicad_cli_available", lambda: False)
    out = tmp_path / "clearance-conformance.md"
    rc = main(["--seeds", "0-1", "--out", str(out)])
    assert rc != 0
    assert not out.exists()
