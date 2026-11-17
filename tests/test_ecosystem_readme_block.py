"""README ecosystem-block drift gate (Issue #5839).

The README's Related Projects section is generated from
``src/kicad_tools/ecosystem/data/projects.toml``.  This module is the ratchet
that makes editing the registry without re-rendering a loud CI failure rather
than silent drift -- the same contract as ``TestBoardsReadmeUpdated`` in
``tests/test_board_07_matchgroup_test.py``.

Negative controls for this suite:

* add a project to the registry and do not re-render -- ``test_committed_block_matches_render`` goes red;
* hand-edit a bullet inside the markers -- same test goes red;
* delete a marker -- ``test_markers_are_present`` goes red.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
RENDER_SCRIPT = REPO_ROOT / "scripts" / "ecosystem_render.py"


@pytest.fixture(scope="module")
def render_module():
    """Import ``scripts/ecosystem_render.py`` as a module."""
    spec = importlib.util.spec_from_file_location("ecosystem_render", RENDER_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["ecosystem_render"] = module
    spec.loader.exec_module(module)
    return module


def test_render_script_exists() -> None:
    assert RENDER_SCRIPT.is_file(), "the README block has no renderer"


def test_markers_are_present() -> None:
    """Both markers, in order, exactly once each."""
    text = README.read_text(encoding="utf-8")
    assert text.count("<!-- BEGIN kct:ecosystem -->") == 1
    assert text.count("<!-- END kct:ecosystem -->") == 1
    assert text.index("<!-- BEGIN kct:ecosystem -->") < text.index("<!-- END kct:ecosystem -->")


def test_committed_block_matches_render(render_module) -> None:
    """The committed README block equals a fresh render of the registry."""
    from kicad_tools.ecosystem import load_registry

    rendered = render_module.render_block(load_registry())
    text = README.read_text(encoding="utf-8")
    begin = text.index(render_module.BEGIN_MARKER)
    end = text.index(render_module.END_MARKER) + len(render_module.END_MARKER)
    committed = text[begin:end]

    assert committed == rendered, (
        "README.md's ecosystem block is out of date with the registry.\n"
        "Re-run: uv run python scripts/ecosystem_render.py --write"
    )


def test_check_mode_passes_on_the_committed_tree(render_module) -> None:
    """``ecosystem_render.py`` with no flags exits 0 on a clean tree."""
    assert render_module.main([]) == 0


def test_check_mode_fails_on_a_tampered_readme(render_module, tmp_path: Path) -> None:
    """The gate actually fails -- a tautological gate protects nothing."""
    tampered = tmp_path / "README.md"
    text = README.read_text(encoding="utf-8")
    tampered.write_text(
        text.replace("### Autorouters", "### Autorouters (hand-edited)", 1), encoding="utf-8"
    )
    assert render_module.main(["--readme", str(tampered)]) == 1


def test_every_project_appears_in_the_block() -> None:
    """No registry entry may be dropped from the README by the render."""
    from kicad_tools.ecosystem import load_registry

    text = README.read_text(encoding="utf-8")
    begin = text.index("<!-- BEGIN kct:ecosystem -->")
    end = text.index("<!-- END kct:ecosystem -->")
    block = text[begin:end]

    for project in load_registry():
        assert project.repo_url in block, f"{project.project_id} is missing from the README"


def test_every_research_doc_is_linked_from_the_block() -> None:
    """Our own evaluations must be reachable from the README.

    This is the regression the whole issue started from: eight evaluation
    notes existed in docs/research/ and the README linked none of them.
    """
    from kicad_tools.ecosystem import load_registry

    text = README.read_text(encoding="utf-8")
    begin = text.index("<!-- BEGIN kct:ecosystem -->")
    end = text.index("<!-- END kct:ecosystem -->")
    block = text[begin:end]

    for project in load_registry():
        for doc in project.research_docs:
            assert doc in block, f"{project.project_id}: {doc} is not linked from the README"


def test_verdicts_survive_the_render() -> None:
    """Each non-complementary verdict is visible to a README reader."""
    from kicad_tools.ecosystem import load_registry

    text = README.read_text(encoding="utf-8")
    registry = load_registry()

    for verdict, marker in (
        ("benchmarked", "**Benchmarked against us.**"),
        ("ideas-adopted", "**Ideas adopted.**"),
        ("evaluated-not-adopted", "**Evaluated and not adopted.**"),
    ):
        if registry.filter(verdict=verdict):
            assert marker in text, f"verdict {verdict!r} is not rendered anywhere"

    if registry.filter(verdict="watch"):
        assert "### Watching" in text


def test_block_has_no_line_number_citations() -> None:
    """The README block must not carry ``.py:NNN`` citations that rot."""
    import re

    text = README.read_text(encoding="utf-8")
    begin = text.index("<!-- BEGIN kct:ecosystem -->")
    end = text.index("<!-- END kct:ecosystem -->")
    assert not re.search(r"\.py:\d+", text[begin:end])
