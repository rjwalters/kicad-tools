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
    NOT_MEASURED,
    TABLE_BEGIN,
    TABLE_END,
    AdapterMeasurement,
    CorpusStats,
    main,
    render_document,
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


def test_table_columns_match_the_specified_six() -> None:
    table = render_table(_STUB_STATS)
    header = table.splitlines()[1]
    columns = [c.strip() for c in header.strip("|").split("|")]
    assert columns == [
        "adapter",
        "corpus (seed range)",
        "over-reject %",
        "under-reject %",
        "boundary",
        "fill state",
    ]
    for row in _table_rows(table):
        assert len(row.strip("|").split("|")) == 6


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

    assert NOT_MEASURED not in by_group[12]
    assert "grid_py" in by_group[12]
    assert "4.2% (3/72)" in by_group[12]  # over-reject
    assert "6.9% (5/72)" in by_group[12]  # under-reject

    for group in (2, 3, 5, 6, 7, 8, 9, 10, 11, 14, 15, 16, 17, 19):
        assert by_group[group].count(NOT_MEASURED) == 5, (
            f"group {group} must read '{NOT_MEASURED}' in all five data columns"
        )
    # Group 18 has no adapter in the stub either, and must still be present.
    assert NOT_MEASURED in by_group[18]


def test_adapters_are_registered_for_the_five_wired_groups() -> None:
    """``ADAPTERS`` wires one consumer per measured group, and no more.

    Succeeds ``test_phase_1a_registers_no_adapters`` (#5513 / PR #5532), which
    pinned ``ADAPTERS == ()`` while only the truth side existed.  That pin was
    a *phase* assertion, not a shape assertion: populating ``ADAPTERS`` is the
    whole content of #5533, so the check becomes "exactly the five intended
    groups are wired" rather than "none is".

    The invariant the old test really protected -- that the harness does not
    import the code it measures -- is unchanged and still enforced, one test
    down, by ``test_truth_side_does_not_import_the_code_it_measures``.
    """
    assert {adapter.group for adapter in ADAPTERS} == {1, 4, 12, 13, 18}
    assert [adapter.group for adapter in ADAPTERS] == sorted(a.group for a in ADAPTERS), (
        "keep ADAPTERS in group order so the table's measured rows read top-to-bottom"
    )
    assert len({adapter.name for adapter in ADAPTERS}) == len(ADAPTERS)


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
    """The checked-in doc has the same 19 rows and columns as the renderer."""
    assert DOC_PATH.exists(), f"missing {DOC_PATH}"
    doc = DOC_PATH.read_text(encoding="utf-8")
    rows = _table_rows(doc)
    assert len(rows) == 19
    numbers = [int(re.match(r"\|\s*(\d+)\.", row).group(1)) for row in rows]  # type: ignore[union-attr]
    assert numbers == list(range(1, 20))


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
