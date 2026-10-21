"""``build_hierarchy`` on a sheet file placed more than once (issue #7616).

Every placement gets its own node with its full subtree, and each node carries
its KiCad instance path. A true cycle still terminates. Callers that act on
sheet *files* (sync-hierarchy, rename-signal, validation) handle a repeated
sheet once, not once per placement.

Fixture ``tests/fixtures/sheet_qualified_lvs/nested_repeat_root.kicad_sch``::

    root -> MID_A (nested_repeat_mid) -> LEAF (nested_repeat_leaf)
         -> MID_B (nested_repeat_mid) -> LEAF (nested_repeat_leaf)
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

from kicad_tools.schema import hierarchy as hierarchy_mod
from kicad_tools.schema.bom import extract_bom
from kicad_tools.schema.hierarchy import build_hierarchy
from kicad_tools.schema.hierarchy_validation import ValidationIssueType, validate_hierarchy

_SQ = Path(__file__).parent / "fixtures" / "sheet_qualified_lvs"
_HIER = Path(__file__).parent / "fixtures" / "hierarchical"
_NESTED = (
    "nested_repeat_root.kicad_sch",
    "nested_repeat_mid.kicad_sch",
    "nested_repeat_leaf.kicad_sch",
)
_NESTED_ROOT = _SQ / _NESTED[0]
_REPEAT_ROOT = _SQ / "repeat_root.kicad_sch"

_ROOT_UUID = "76160001-0000-4000-8000-000000007616"
_MID_A_SHEET = "76160002-0000-4000-8000-000000007616"
_MID_B_SHEET = "76160003-0000-4000-8000-000000007616"
_LEAF_SHEET = "76160011-0000-4000-8000-000000007616"


def _copy(src: Path, names: tuple[str, ...], dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copy(src / name, dest / name)
    return dest


def _rewrite(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new))


# --- one node per placement ------------------------------------------------


def test_nested_child_appears_under_every_placement():
    root = build_hierarchy(str(_NESTED_ROOT))

    paths = [n.get_path_string() for n in root.all_nodes()]
    assert paths == ["/", "/MID_A", "/MID_A/LEAF", "/MID_B", "/MID_B/LEAF"]

    leaf_a = root.find_by_path("/MID_A/LEAF")
    leaf_b = root.find_by_path("/MID_B/LEAF")
    assert leaf_a is not None and leaf_b is not None
    assert leaf_a is not leaf_b
    assert leaf_a.path == leaf_b.path == str((_SQ / _NESTED[2]).resolve())
    assert leaf_a.hierarchical_labels == leaf_b.hierarchical_labels == ["SIG"]
    assert leaf_a.parent is root.find_by_path("/MID_A")
    assert leaf_b.parent is root.find_by_path("/MID_B")
    assert leaf_b.depth == 2

    mid_b = root.find_by_path("/MID_B")
    assert mid_b is not None
    assert [s.name for s in mid_b.sheets] == ["LEAF"]
    assert not mid_b.is_leaf


def test_each_node_has_its_instance_path():
    root = build_hierarchy(str(_NESTED_ROOT))
    by_path = {n.get_path_string(): n.instance_path for n in root.all_nodes()}
    assert by_path == {
        "/": f"/{_ROOT_UUID}",
        "/MID_A": f"/{_ROOT_UUID}/{_MID_A_SHEET}",
        "/MID_A/LEAF": f"/{_ROOT_UUID}/{_MID_A_SHEET}/{_LEAF_SHEET}",
        "/MID_B": f"/{_ROOT_UUID}/{_MID_B_SHEET}",
        "/MID_B/LEAF": f"/{_ROOT_UUID}/{_MID_B_SHEET}/{_LEAF_SHEET}",
    }


def test_instance_path_matches_symbol_instances_block():
    """The node instance path is the key KiCad writes in ``(instances (path ...))``."""
    root = build_hierarchy(str(_NESTED_ROOT))
    leaf_text = (_SQ / _NESTED[2]).read_text()
    for node in (root.find_by_path("/MID_A/LEAF"), root.find_by_path("/MID_B/LEAF")):
        assert node is not None
        assert f'(path "{node.instance_path}"' in leaf_text


def test_repeated_file_is_parsed_once(monkeypatch):
    calls: list[int] = []
    real_parse = hierarchy_mod.parse_string

    def counting_parse(text: str):
        calls.append(1)
        return real_parse(text)

    monkeypatch.setattr(hierarchy_mod, "parse_string", counting_parse)
    root = build_hierarchy(str(_NESTED_ROOT))
    assert len(root.all_nodes()) == 5
    assert len(calls) == 3  # root, mid, leaf


def test_unique_file_nodes_keeps_first_placement():
    root = build_hierarchy(str(_NESTED_ROOT))
    unique = root.unique_file_nodes()
    assert [n.get_path_string() for n in unique] == ["/", "/MID_A", "/MID_A/LEAF"]


def test_flat_repeat_fixture_has_both_placements():
    root = build_hierarchy(str(_REPEAT_ROOT))
    assert [c.name for c in root.children] == ["MCU_A", "MCU_B"]
    a, b = root.children
    assert a.path == b.path
    assert a.instance_path != b.instance_path


def test_missing_sub_sheet_is_empty_under_every_placement(tmp_path):
    proj = _copy(_SQ, _NESTED[:2], tmp_path / "p")  # leaf file left out
    root = build_hierarchy(str(proj / _NESTED[0]))
    for path in ("/MID_A/LEAF", "/MID_B/LEAF"):
        leaf = root.find_by_path(path)
        assert leaf is not None
        assert leaf.uuid == ""
        assert leaf.children == []
        assert leaf.instance_path.endswith(_LEAF_SHEET)


def test_strict_root_still_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        build_hierarchy(str(tmp_path / "missing.kicad_sch"), strict_root=True)


# --- trees without repeated sheets are unchanged ------------------------------


def test_non_repeating_tree_is_unchanged():
    root = build_hierarchy(str(_HIER / "root.kicad_sch"))
    paths = [n.get_path_string() for n in root.all_nodes()]
    assert paths == ["/", "/SubSheetA", "/SubSheetA/Nested", "/SubSheetB"]
    assert root.unique_file_nodes() == root.all_nodes()
    assert all(n.instance_path.startswith(f"/{root.uuid}") for n in root.all_nodes())


# --- cycles still terminate ---------------------------------------------------


def test_self_including_sheet_terminates(tmp_path, caplog):
    proj = _copy(_SQ, _NESTED, tmp_path / "p")
    # The leaf places itself: copy MID's sheet symbol (-> leaf file) into it.
    leaf = proj / _NESTED[2]
    text = leaf.read_text()
    mid_sheet = (proj / _NESTED[1]).read_text()
    start = mid_sheet.index("\t(sheet\n")
    end = mid_sheet.rindex(")")
    leaf.write_text(text[: text.rindex(")")] + mid_sheet[start:end] + ")\n")
    with caplog.at_level(logging.WARNING, logger="kicad_tools.schema.hierarchy"):
        root = build_hierarchy(str(proj / _NESTED[0]))
    inner = root.find_by_path("/MID_A/LEAF/LEAF")
    assert inner is not None
    assert inner.children == []
    assert inner.hierarchical_labels == ["SIG"]
    assert root.find_by_path("/MID_B/LEAF/LEAF") is not None
    assert any("own ancestor" in r.getMessage() for r in caplog.records)


def test_ancestor_cycle_terminates(tmp_path):
    proj = _copy(_SQ, _NESTED, tmp_path / "p")
    # LEAF places MID, which places LEAF: MID -> LEAF -> MID is a cycle.
    leaf = proj / _NESTED[2]
    mid_sheet = (proj / _NESTED[1]).read_text()
    start = mid_sheet.index("\t(sheet\n")
    end = mid_sheet.rindex(")")
    back = mid_sheet[start:end].replace(
        '"nested_repeat_leaf.kicad_sch"', '"nested_repeat_mid.kicad_sch"'
    )
    back = back.replace('"LEAF"', '"BACK"')
    text = leaf.read_text()
    leaf.write_text(text[: text.rindex(")")] + back + ")\n")

    root = build_hierarchy(str(proj / _NESTED[0]))
    back_node = root.find_by_path("/MID_A/LEAF/BACK")
    assert back_node is not None
    assert back_node.children == []
    assert len(root.all_nodes()) == 7  # root + 2 x (MID, LEAF, BACK)


# --- callers -------------------------------------------------------------------


def test_bom_does_not_double_count():
    refs = sorted(i.reference for i in extract_bom(str(_NESTED_ROOT)).items)
    assert refs == ["R1", "R101"]
    flat = [i.reference for i in extract_bom(str(_REPEAT_ROOT)).items]
    assert sorted(flat) == ["R1", "R11", "R12", "R2"]


def test_validation_reports_a_nested_mismatch_once(tmp_path):
    proj = _copy(_SQ, _NESTED, tmp_path / "p")
    _rewrite(proj / _NESTED[2], '(hierarchical_label "SIG"', '(hierarchical_label "OTHER"')
    result = validate_hierarchy(str(proj / _NESTED[0]))
    missing = [i for i in result.issues if i.issue_type == ValidationIssueType.MISSING_LABEL]
    assert [(i.sheet_name, i.pin_name) for i in missing] == [("LEAF", "SIG")]
    assert result.sheets_checked == 3


def test_sync_hierarchy_adds_a_label_to_a_repeated_file_once(tmp_path):
    from kicad_tools.cli.sch_sync_hierarchy import execute_add_labels

    proj = _copy(_SQ, _NESTED, tmp_path / "p")
    mid = proj / _NESTED[1]
    leaf = proj / _NESTED[2]
    _rewrite(mid, '(hierarchical_label "SIG"', '(hierarchical_label "OTHER"')
    _rewrite(leaf, '(hierarchical_label "SIG"', '(hierarchical_label "OTHER"')

    results = execute_add_labels(str(proj / _NESTED[0]))
    assert all(r.success for r in results), [r.message for r in results]
    # MID_A and MID_B both miss SIG, but it is one file: one label added.
    assert mid.read_text().count('(hierarchical_label "SIG"') == 1
    assert leaf.read_text().count('(hierarchical_label "SIG"') == 1
    assert len(results) == 2


def test_rename_signal_lists_each_file_occurrence_once():
    from kicad_tools.cli.sch_rename_signal import find_signal_occurrences

    changes = find_signal_occurrences(str(_NESTED_ROOT), "SIG")
    keys = [(c.file_path, c.element_type, c.line_number) for c in changes]
    assert len(keys) == len(set(keys))
    leaf_labels = [
        c
        for c in changes
        if c.file_path.endswith(_NESTED[2]) and c.element_type == "hierarchical_label"
    ]
    assert len(leaf_labels) == 1
