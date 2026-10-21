"""Sub-sheet path resolution in ``build_hierarchy`` (issue #7614).

A relative ``Sheetfile`` is resolved against the folder of the sheet that
contains the sheet symbol, never against the working directory. Every
``HierarchyNode.path`` is absolute, and a sub-sheet that cannot be loaded logs
a warning.

All trees are built in ``tmp_path`` from copies of the repo's own
``tests/fixtures/hierarchical`` files.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

from kicad_tools.schema import hierarchy as hierarchy_mod
from kicad_tools.schema.bom import extract_bom
from kicad_tools.schema.hierarchy import HierarchyBuilder, build_hierarchy

FIXTURES = Path(__file__).parent / "fixtures" / "hierarchical"

# root.kicad_sch -> SubSheetA (sub_a.kicad_sch) -> Nested (nested.kicad_sch)
#                -> SubSheetB (sub_b.kicad_sch)
ROOT_TREE = ("root.kicad_sch", "sub_a.kicad_sch", "sub_b.kicad_sch", "nested.kicad_sch")


def _copy(names: tuple[str, ...], dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copy(FIXTURES / name, dest / name)


def _rewrite(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new))


def _summary(root) -> list[tuple[str, str, str, int]]:
    return [(n.get_path_string(), n.path, n.uuid, len(n.children)) for n in root.all_nodes()]


def _refs(root_path: Path) -> list[str]:
    return sorted(item.reference for item in extract_bom(str(root_path)).items)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "project"
    _copy(ROOT_TREE, proj)
    return proj


def test_decoy_in_cwd_is_not_read(project: Path, tmp_path: Path, monkeypatch) -> None:
    """A same-named file in the cwd must not replace the real sub-sheet."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    # A different schematic under the sub-sheet's file name.
    shutil.copy(FIXTURES / "child_with_hlabels.kicad_sch", elsewhere / "sub_b.kicad_sch")

    monkeypatch.chdir(project)
    expected = _summary(build_hierarchy(str(project / "root.kicad_sch")))
    expected_refs = _refs(project / "root.kicad_sch")

    monkeypatch.chdir(elsewhere)
    root = build_hierarchy(str(project / "root.kicad_sch"))
    sub_b = root.find_by_path("/SubSheetB")
    assert sub_b is not None
    assert sub_b.uuid == "sub-b-uuid-0001"
    assert sub_b.path == str((project / "sub_b.kicad_sch").resolve())
    assert _summary(root) == expected
    assert _refs(project / "root.kicad_sch") == expected_refs


def test_nested_sheet_in_subfolder_resolves_against_its_parent(
    project: Path, tmp_path: Path, monkeypatch
) -> None:
    """``sub/sub_a.kicad_sch`` -> ``nested.kicad_sch`` means ``sub/nested.kicad_sch``."""
    sub = project / "sub"
    sub.mkdir()
    (project / "sub_a.kicad_sch").rename(sub / "sub_a.kicad_sch")
    (project / "nested.kicad_sch").rename(sub / "nested.kicad_sch")
    _rewrite(project / "root.kicad_sch", '"sub_a.kicad_sch"', '"sub/sub_a.kicad_sch"')

    monkeypatch.chdir(tmp_path)
    root = build_hierarchy(str(project / "root.kicad_sch"))

    nested = root.find_by_path("/SubSheetA/Nested")
    assert nested is not None
    assert nested.path == str((sub / "nested.kicad_sch").resolve())
    assert nested.uuid == "nested-uuid-0001"
    sub_a = root.find_by_path("/SubSheetA")
    assert sub_a is not None and len(sub_a.children) == 1

    # The BOM sees the nested sheet's parts too.
    flat = tmp_path / "flat"
    _copy(ROOT_TREE, flat)
    assert _refs(project / "root.kicad_sch") == _refs(flat / "root.kicad_sch")


def test_paths_are_absolute_and_cwd_independent(project: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(project)
    from_project = _summary(build_hierarchy("root.kicad_sch"))

    monkeypatch.chdir(tmp_path)
    from_tmp = _summary(build_hierarchy(str(project / "root.kicad_sch")))
    from_tmp_relative = _summary(build_hierarchy("project/root.kicad_sch"))

    assert from_project == from_tmp == from_tmp_relative
    for _, path, _, _ in from_tmp:
        assert Path(path).is_absolute()
        assert Path(path) == Path(path).resolve()


def test_absolute_sheetfile_is_used_as_is(project: Path, tmp_path: Path, monkeypatch) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (project / "sub_b.kicad_sch").rename(other / "sub_b.kicad_sch")
    _rewrite(
        project / "root.kicad_sch",
        '"sub_b.kicad_sch"',
        f'"{(other / "sub_b.kicad_sch").as_posix()}"',
    )
    monkeypatch.chdir(tmp_path)
    root = build_hierarchy(str(project / "root.kicad_sch"))
    sub_b = root.find_by_path("/SubSheetB")
    assert sub_b is not None
    assert sub_b.uuid == "sub-b-uuid-0001"
    assert sub_b.path == str((other / "sub_b.kicad_sch").resolve())


def test_missing_subsheet_logs_warning(tmp_path: Path, monkeypatch, caplog) -> None:
    proj = tmp_path / "shared"
    _copy(("root_shared.kicad_sch", "sub_b.kicad_sch", "empty.kicad_sch"), proj)
    monkeypatch.chdir(tmp_path)

    with caplog.at_level(logging.WARNING, logger="kicad_tools.schema.hierarchy"):
        root = build_hierarchy(str(proj / "root_shared.kicad_sch"))

    missing = root.find_by_path("/Missing")
    assert missing is not None
    assert missing.uuid == "" and missing.children == []
    expected_path = str((proj / "does_not_exist.kicad_sch").resolve())
    assert missing.path == expected_path

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    hits = [m for m in messages if "Missing" in m and expected_path in m]
    assert len(hits) == 1, messages
    # Loadable sheets do not warn.
    assert not [m for m in messages if "SubB_" in m or "Empty" in m]


def test_sheet_used_twice_is_parsed_once(tmp_path: Path, monkeypatch) -> None:
    """Two placements of one file (spelled differently) share one parse."""
    proj = tmp_path / "shared"
    _copy(("root_shared.kicad_sch", "sub_b.kicad_sch", "empty.kicad_sch"), proj)
    # Second placement spells the same file with a "./" prefix.
    root_file = proj / "root_shared.kicad_sch"
    text = root_file.read_text()
    first = text.index('"sub_b.kicad_sch"')
    second = text.index('"sub_b.kicad_sch"', first + 1)
    root_file.write_text(
        text[:second] + '"./sub_b.kicad_sch"' + text[second + len('"sub_b.kicad_sch"') :]
    )

    calls: list[int] = []
    real_parse = hierarchy_mod.parse_string

    def counting_parse(text: str):
        calls.append(1)
        return real_parse(text)

    monkeypatch.setattr(hierarchy_mod, "parse_string", counting_parse)
    monkeypatch.chdir(tmp_path)

    builder = HierarchyBuilder(str(proj))
    root = builder.build(str(root_file))

    sub_b_path = str((proj / "sub_b.kicad_sch").resolve())
    n1 = root.find_by_path("/SubB_1")
    n2 = root.find_by_path("/SubB_2")
    assert n1 is not None and n2 is not None
    assert n1.path == n2.path == sub_b_path
    assert n1.uuid == n2.uuid == "sub-b-uuid-0001"
    assert list(builder.loaded_files).count(sub_b_path) == 1
    # root + sub_b + empty; the missing sheet is never parsed.
    assert len(calls) == 3
