"""Symlinked library members must not escape the project zip (#8763)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from kicad_tools.cli import readiness_cmd
from kicad_tools.export.manufacturing import _project_footprint_files


@pytest.fixture
def project(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    (outside / "ext.pretty").mkdir(parents=True)
    (outside / "ext.pretty" / "ext.kicad_mod").write_text("(footprint ext)")
    (outside / "secret.kicad_mod").write_text("(footprint secret)")
    (outside / "inner_target").mkdir()

    p = tmp_path / "p"
    (p / "local.pretty").mkdir(parents=True)
    (p / "local.pretty" / "ok.kicad_mod").write_text("(footprint ok)")
    (p / "local.pretty" / "leak.kicad_mod").symlink_to(outside / "secret.kicad_mod")
    (p / "local.pretty" / "broken.kicad_mod").symlink_to(tmp_path / "nope")
    (p / "ext.pretty").symlink_to(outside / "ext.pretty")
    (p / "real").mkdir()
    (p / "real" / "inside.kicad_mod").write_text("(footprint inside)")
    (p / "local.pretty" / "alias.kicad_mod").symlink_to(p / "real" / "inside.kicad_mod")
    (p / "b.kicad_pcb").write_text("(kicad_pcb)")
    return p


def _names(files, root: Path) -> set[str]:
    return {Path(f).relative_to(root).as_posix() for f in files}


def test_outside_members_excluded_and_inside_symlink_kept(project, caplog):
    root = project.resolve()
    with caplog.at_level(logging.WARNING):
        files = _project_footprint_files(project)
    names = _names(files, root)
    assert "local.pretty/ok.kicad_mod" in names
    assert "local.pretty/alias.kicad_mod" in names
    assert "local.pretty/leak.kicad_mod" not in names
    assert not any(n.startswith("ext.pretty/") for n in names)
    assert "leak.kicad_mod" in caplog.text and "ext.kicad_mod" in caplog.text


def test_readiness_and_exporter_agree(project):
    root = project.resolve()
    pcb = root / "b.kicad_pcb"
    ready = {p for p in readiness_cmd._project_dependencies(pcb, root) if p.suffix == ".kicad_mod"}
    export = {p for p in _project_footprint_files(project) if p.suffix == ".kicad_mod"}
    assert _names(ready, root) == _names(export, root)
    assert not any("leak" in str(p) or "ext.kicad_mod" in str(p) for p in ready)


def test_symlink_loop_is_skipped(project):
    os.symlink(project / "local.pretty", project / "local.pretty" / "loop")
    names = _names(_project_footprint_files(project), project.resolve())
    assert "local.pretty/ok.kicad_mod" in names
