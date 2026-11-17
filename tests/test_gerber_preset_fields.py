"""Gerber manufacturer presets: no dead config, and kicad-cli naming (Issue #6163).

``GerberManufacturerPreset.layer_rename`` used to be declared and populated
for JLCPCB and Seeed, but nothing ever read it, so a preset implied a file
naming it never produced.  The map was removed (every supported fab accepts
kicad-cli's names; see the ``GerberManufacturerPreset`` docstring).  These
tests keep it that way:

* every field of ``GerberConfig`` / ``GerberManufacturerPreset`` must be read
  by the exporter in ``export/gerber.py`` -- a new field that nothing consumes
  fails here instead of silently doing nothing;
* a real kicad-cli export through each preset yields kicad-cli's own
  ``<board>-<Layer>.<protel ext>`` names, keeps the X2 ``%TF.FileFunction``
  attributes, and the ``.gbrjob`` only references files that are in the zip;
* each preset's drill files come out merged or split as it declares
  (Issue #6167).
"""

from __future__ import annotations

import ast
import dataclasses
import json
import re
import warnings
import zipfile
from pathlib import Path

import pytest

import kicad_tools.export.gerber as gerber_module
from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.export.gerber import (
    MANUFACTURER_PRESETS,
    GerberConfig,
    GerberExporter,
    GerberManufacturerPreset,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SIMPLE_LED = REPO_ROOT / "boards" / "00-simple-led" / "output" / "simple_led_routed.kicad_pcb"


def _attributes_read_in_functions(source: str, receivers: set[str]) -> set[str]:
    """Attribute names read as ``<receiver>.<attr>`` inside any function body."""
    read: set[str] = set()
    for func in ast.walk(ast.parse(source)):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Load)
                and isinstance(node.value, ast.Name)
                and node.value.id in receivers
            ):
                read.add(node.attr)
    return read


@pytest.mark.parametrize(
    ("cls", "receiver"),
    [(GerberConfig, "config"), (GerberManufacturerPreset, "preset")],
    ids=["GerberConfig", "GerberManufacturerPreset"],
)
def test_every_preset_field_is_consumed_by_the_exporter(cls, receiver):
    source = Path(gerber_module.__file__).read_text()
    read = _attributes_read_in_functions(source, {receiver})
    dead = [f.name for f in dataclasses.fields(cls) if f.name not in read]
    assert not dead, (
        f"{cls.__name__} field(s) {dead} are never read as `{receiver}.<field>` in "
        "export/gerber.py -- wire them into the export or delete them (Issue #6163)."
    )


def test_layer_rename_is_gone():
    names = {f.name for f in dataclasses.fields(GerberManufacturerPreset)}
    assert "layer_rename" not in names


_GERBER_RE = re.compile(r"-(?P<layer>[A-Za-z0-9_]+)\.(?:gtl|gbl|gts|gbs|gto|gbo|gtp|gbp|gm1|g\d+)$")


@pytest.mark.skipif(find_kicad_cli() is None, reason="kicad-cli not installed")
@pytest.mark.parametrize("manufacturer", sorted(MANUFACTURER_PRESETS))
def test_preset_export_uses_kicad_cli_names(manufacturer, tmp_path):
    zip_path = GerberExporter(SIMPLE_LED).export_for_manufacturer(manufacturer, tmp_path / "out")
    stem = SIMPLE_LED.stem

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        gerbers = {n for n in names if _GERBER_RE.search(n)}
        jobs = {n for n in names if n.endswith(".gbrjob")}
        drills = {n for n in names if n.endswith(".drl")}

        # Nothing outside kicad-cli's naming scheme (no renamed files).
        assert names == gerbers | jobs | drills, sorted(names - gerbers - jobs - drills)
        assert all(n.startswith(f"{stem}-") for n in gerbers | jobs)
        assert jobs == {f"{stem}-job.gbrjob"}
        # Issue #6167: the preset's PTH/NPTH choice reaches kicad-cli.  OSH
        # Park asks for one merged drill file; the others get KiCad's default
        # separate plated / non-plated files.
        if MANUFACTURER_PRESETS[manufacturer].config.merge_pth_npth:
            assert drills == {f"{stem}.drl"}
        else:
            assert drills == {f"{stem}-PTH.drl", f"{stem}-NPTH.drl"}
        plated = zf.read(f"{stem}.drl" if len(drills) == 1 else f"{stem}-PTH.drl")
        assert plated.startswith(b"M48") and b"\nT1" in plated

        try:
            from gerbonara import ExcellonFile
        except ImportError:
            ExcellonFile = None
        if ExcellonFile is not None:
            for name in drills:
                with warnings.catch_warnings():
                    # gerbonara notes KiCad's G90 after the header; harmless.
                    warnings.simplefilter("ignore", SyntaxWarning)
                    ExcellonFile.from_string(zf.read(name).decode(), filename=name)

        layers = {_GERBER_RE.search(n)["layer"] for n in gerbers}
        assert {"F_Cu", "B_Cu", "F_Mask", "B_Mask", "Edge_Cuts"} <= layers
        assert f"{stem}-Edge_Cuts.gm1" in names
        assert f"{stem}-F_Cu.gtl" in names

        # X2 file-function attributes survive -- fabs that sniff them still work.
        for name in gerbers:
            assert b"%TF.FileFunction," in zf.read(name), name

        # The job file references exactly the Gerbers that ship in the zip.
        job = json.loads(zf.read(f"{stem}-job.gbrjob"))
        referenced = {entry["Path"] for entry in job["FilesAttributes"]}
        assert referenced == gerbers
