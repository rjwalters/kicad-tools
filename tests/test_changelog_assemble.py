"""Tests for ``scripts/changelog_assemble.py`` (issue #5775, epic #5774 Phase 1).

The assembler turns ``changelog.d/<issue>.<kind>.md`` fragments (plus any
existing ``[Unreleased]`` bullets) into a ``## [X.Y.Z] - YYYY-MM-DD`` section.
The Phase 2 release workflow runs it unattended, so the output must be
byte-stable and the fragment-name validation strict.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "changelog_assemble.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("changelog_assemble", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["changelog_assemble"] = module
    spec.loader.exec_module(module)
    return module


assemble_mod = _load_module()


_CHANGELOG = """# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Fixed

- **Hand-written unreleased fix** (Issue #4000).

### Removed

- **Dropped a flag** (Issue #4001).

## [0.22.0] - 2026-09-28

### Fixed

- **Old fix** (Issue #3000).

[0.22.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.22.0
[0.21.1]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.21.1
"""

_EXPECTED = """# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

## [0.23.0] - 2026-10-01

### Summary

A summary line.

### Upgrade notes / behaviour changes

- **`kct check --strict` can newly fail** (Issue #5100).

### Fixed

- **Hand-written unreleased fix** (Issue #4000).

- **Fix A** (Issue #5001).

- **Fix B, first** (Issue #5002).

- **Fix B, second** (Issue #5002).
  Second line.

### Added

- **A feature** (Issue #5100).
  With a continuation paragraph.

### Performance

- **Faster** (Issue #5200).

### Removed

- **Dropped a flag** (Issue #4001).

## [0.22.0] - 2026-09-28

### Fixed

- **Old fix** (Issue #3000).

[0.23.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.23.0
[0.22.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.22.0
[0.21.1]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.21.1
"""


def _write_fixture(root: Path) -> tuple[Path, Path]:
    changelog = root / "CHANGELOG.md"
    changelog.write_text(_CHANGELOG, encoding="utf-8")
    frags = root / "changelog.d"
    frags.mkdir()
    (frags / "README.md").write_text("# Fragments\n")
    (frags / ".gitkeep").write_text("")
    (frags / "5100.added.md").write_text(
        "- **A feature** (Issue #5100).\n  With a continuation paragraph.\n\n"
    )
    (frags / "5100.upgrade.md").write_text(
        "- **`kct check --strict` can newly fail** (Issue #5100).\n"
    )
    # A body without a leading "- " is made into one bullet.
    (frags / "5002.fixed.b.md").write_text("**Fix B, second** (Issue #5002).\nSecond line.\n")
    (frags / "5002.fixed.a.md").write_text("- **Fix B, first** (Issue #5002).   \n")
    (frags / "5001.fixed.md").write_text("- **Fix A** (Issue #5001).\n")
    (frags / "5200.performance.md").write_text("- **Faster** (Issue #5200).\n")
    return changelog, frags


def test_fixture_assembles_to_the_exact_expected_changelog(tmp_path: Path) -> None:
    changelog, frags = _write_fixture(tmp_path)
    fragments = assemble_mod.collect_fragments(frags)
    out = assemble_mod.assemble(
        changelog.read_text(), fragments, "0.23.0", "2026-10-01", summary="A summary line.\n"
    )
    assert out == _EXPECTED


def test_assembly_is_byte_stable_across_runs(tmp_path: Path) -> None:
    changelog, frags = _write_fixture(tmp_path)
    runs = {
        assemble_mod.assemble(
            changelog.read_text(), assemble_mod.collect_fragments(frags), "0.23.0", "2026-10-01"
        )
        for _ in range(3)
    }
    assert len(runs) == 1


def test_one_heading_per_kind(tmp_path: Path) -> None:
    changelog, frags = _write_fixture(tmp_path)
    out = assemble_mod.assemble(
        changelog.read_text(), assemble_mod.collect_fragments(frags), "0.23.0", "2026-10-01"
    )
    section = out.split("## [0.23.0]")[1].split("## [0.22.0]")[0]
    for title in assemble_mod.SECTION_TITLES.values():
        assert section.count(f"### {title}\n") <= 1, title
    # No Summary heading when neither --summary nor [Unreleased] provides one.
    assert "### Summary" not in section
    # Changed has no entries, so no empty heading.
    assert "### Changed" not in section


def test_cli_writes_changelog_and_deletes_consumed_fragments(tmp_path: Path, capsys) -> None:
    changelog, frags = _write_fixture(tmp_path)
    summary = tmp_path / "summary.md"
    summary.write_text("A summary line.\n")
    rc = assemble_mod.main(
        [
            "--version",
            "0.23.0",
            "--date",
            "2026-10-01",
            "--summary-file",
            str(summary),
            "--changelog",
            str(changelog),
            "--fragments-dir",
            str(frags),
        ]
    )
    assert rc == 0
    assert changelog.read_text() == _EXPECTED
    assert sorted(p.name for p in frags.iterdir()) == [".gitkeep", "README.md"]
    assert "6 fragment(s)" in capsys.readouterr().out


def test_cli_dry_run_writes_and_deletes_nothing(tmp_path: Path, capsys) -> None:
    changelog, frags = _write_fixture(tmp_path)
    before = sorted(p.name for p in frags.iterdir())
    rc = assemble_mod.main(
        [
            "--version",
            "0.23.0",
            "--date",
            "2026-10-01",
            "--dry-run",
            "--changelog",
            str(changelog),
            "--fragments-dir",
            str(frags),
        ]
    )
    assert rc == 0
    assert changelog.read_text() == _CHANGELOG
    assert sorted(p.name for p in frags.iterdir()) == before
    printed = capsys.readouterr().out
    assert printed.startswith("## [0.23.0] - 2026-10-01\n")
    section = _EXPECTED.split("\n\n## [0.22.0]")[0].split("## [Unreleased]\n\n")[1] + "\n"
    # No --summary-file here, so the expected section minus its Summary block.
    assert printed == section.replace("### Summary\n\nA summary line.\n\n", "")


def test_nothing_to_assemble_exits_1(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n\n## [Unreleased]\n\n## [0.1.0] - 2026-01-01\n")
    rc = assemble_mod.main(
        [
            "--version",
            "0.2.0",
            "--changelog",
            str(changelog),
            "--fragments-dir",
            str(tmp_path / "x"),
        ]
    )
    assert rc == 1


def test_existing_version_is_refused(tmp_path: Path) -> None:
    changelog, frags = _write_fixture(tmp_path)
    with pytest.raises(assemble_mod.FragmentError, match="already has"):
        assemble_mod.assemble(
            changelog.read_text(), assemble_mod.collect_fragments(frags), "0.22.0", "2026-10-01"
        )


@pytest.mark.parametrize(
    "name",
    ["5001.fix.md", "fixed.md", "5001.fixed.txt", "PR5001.fixed.md", "5001.fixed.bad slug.md"],
)
def test_bad_fragment_names_are_rejected(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_text("- x\n")
    with pytest.raises(assemble_mod.FragmentError, match="not a fragment name"):
        assemble_mod.collect_fragments(tmp_path)


def test_empty_fragment_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "5001.fixed.md").write_text("\n  \n")
    with pytest.raises(assemble_mod.FragmentError, match="empty fragment"):
        assemble_mod.collect_fragments(tmp_path)


def test_parse_fragment_name() -> None:
    assert assemble_mod.parse_fragment_name("5775.fixed.md") == (5775, "fixed", None)
    assert assemble_mod.parse_fragment_name("5775.upgrade.cli-flag.md") == (
        5775,
        "upgrade",
        "cli-flag",
    )
    assert assemble_mod.parse_fragment_name("README.md") is None


def test_fragment_issue_numbers_include_body_citations(tmp_path: Path) -> None:
    (tmp_path / "10.added.md").write_text("- x (Issue #10, follows #9).\n")
    assert assemble_mod.fragment_issue_numbers(assemble_mod.collect_fragments(tmp_path)) == {9, 10}


def test_check_and_list_modes(tmp_path: Path, capsys) -> None:
    _, frags = _write_fixture(tmp_path)
    assert assemble_mod.main(["--check", "--fragments-dir", str(frags)]) == 0
    capsys.readouterr()
    assert assemble_mod.main(["--list", "--fragments-dir", str(frags)]) == 0
    import json

    listed = json.loads(capsys.readouterr().out)
    assert [(e["issue"], e["kind"]) for e in listed] == [
        (5100, "upgrade"),
        (5001, "fixed"),
        (5002, "fixed"),
        (5002, "fixed"),
        (5100, "added"),
        (5200, "performance"),
    ]
    (frags / "oops.md").write_text("x\n")
    assert assemble_mod.main(["--check", "--fragments-dir", str(frags)]) == 2


def test_repo_changelog_d_is_valid() -> None:
    """The committed fragment directory itself must always validate."""
    assemble_mod.collect_fragments(REPO_ROOT / "changelog.d")
