"""Tests for (model ...) 3D reference patching (kct pcb add-3d-models).

Root cause covered: board generators emit footprints without (model ...)
nodes, so `kicad-cli pcb render` shows a bare board. The patcher copies the
canonical model refs from the installed KiCad .kicad_mod sources into the
.kicad_pcb as a pure text insertion (never touching copper bytes).
"""

from __future__ import annotations

import difflib
from pathlib import Path

from kicad_tools.footprints.library_path import LibraryPaths
from kicad_tools.pcb.models3d import (
    add_model_refs,
    add_model_refs_to_text,
    extract_model_blocks,
    iter_footprint_blocks,
    make_library_resolver,
)

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

MOD_WITH_MODEL = """(footprint "R_0805_2012Metric"
\t(version 20240108)
\t(layer "F.Cu")
\t(descr "Resistor SMD 0805")
\t(pad "1" smd roundrect
\t\t(at -0.9125 0)
\t\t(size 1.025 1.4)
\t\t(layers "F.Cu" "F.Mask" "F.Paste")
\t)
\t(model "${KICAD10_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0805_2012Metric.step"
\t\t(offset
\t\t\t(xyz 0 0 0)
\t\t)
\t\t(scale
\t\t\t(xyz 1 1 1)
\t\t)
\t\t(rotate
\t\t\t(xyz 0 0 0)
\t\t)
\t)
)
"""

MOD_WITHOUT_MODEL = """(footprint "MountingHole_3.2mm_M3"
\t(layer "F.Cu")
\t(pad "" np_thru_hole circle
\t\t(at 0 0)
\t\t(size 3.2 3.2)
\t\t(drill 3.2)
\t\t(layers "*.Cu" "*.Mask")
\t)
)
"""

PCB_TEXT = """(kicad_pcb
\t(version 20240108)
\t(generator "kicad_tools")
\t(net 0 "")
\t(net 1 "VCC")
\t(footprint "Resistor_SMD:R_0805_2012Metric"
\t\t(layer "F.Cu")
\t\t(uuid "36c2dbbf-1398-4b65-b95d-148013d5564f")
\t\t(at 112.5 108)
\t\t(property "Reference" "R1"
\t\t\t(at 0 -1.5 0)
\t\t\t(layer "F.SilkS")
\t\t)
\t\t(pad "1" smd roundrect
\t\t\t(at -1 0)
\t\t\t(size 1 1.3)
\t\t\t(layers "F.Cu" "F.Mask" "F.Paste")
\t\t\t(net 1 "VCC")
\t\t)
\t\t(embedded_fonts no)
\t)
\t(footprint "MountingHole:MountingHole_3.2mm_M3"
\t\t(layer "F.Cu")
\t\t(at 100 100)
\t)
\t(footprint "Custom_Lib:Not_A_Standard_Part"
\t\t(layer "F.Cu")
\t\t(at 90 90)
\t)
\t(segment
\t\t(start 111.5 108)
\t\t(end 105 108)
\t\t(width 0.25)
\t\t(layer "F.Cu")
\t\t(net 1)
\t)
)
"""


def _make_library(tmp_path: Path) -> LibraryPaths:
    """Build a fake installed-KiCad footprints tree."""
    root = tmp_path / "footprints"
    (root / "Resistor_SMD.pretty").mkdir(parents=True)
    (root / "Resistor_SMD.pretty" / "R_0805_2012Metric.kicad_mod").write_text(MOD_WITH_MODEL)
    (root / "MountingHole.pretty").mkdir()
    (root / "MountingHole.pretty" / "MountingHole_3.2mm_M3.kicad_mod").write_text(MOD_WITHOUT_MODEL)
    return LibraryPaths(footprints_path=root, source="config")


# --------------------------------------------------------------------------
# Block scanning / extraction
# --------------------------------------------------------------------------


class TestScanning:
    def test_iter_footprint_blocks_finds_all(self):
        blocks = iter_footprint_blocks(PCB_TEXT)
        assert [b.lib_id for b in blocks] == [
            "Resistor_SMD:R_0805_2012Metric",
            "MountingHole:MountingHole_3.2mm_M3",
            "Custom_Lib:Not_A_Standard_Part",
        ]
        # Spans are balanced blocks.
        for b in blocks:
            assert PCB_TEXT[b.start] == "("
            assert PCB_TEXT[b.end] == ")"

    def test_footprint_token_inside_string_ignored(self):
        text = '(kicad_pcb\n\t(gr_text "(footprint \\"fake\\"" (at 0 0))\n)\n'
        assert iter_footprint_blocks(text) == []

    def test_extract_model_blocks(self):
        blocks = extract_model_blocks(MOD_WITH_MODEL)
        assert len(blocks) == 1
        assert blocks[0].startswith('(model "${KICAD10_3DMODEL_DIR}/Resistor_SMD.3dshapes/')
        # Dedented: continuation lines are relative to the node.
        assert "\n\t(offset" in blocks[0]
        assert blocks[0].endswith("\n)")

    def test_extract_model_blocks_none(self):
        assert extract_model_blocks(MOD_WITHOUT_MODEL) == []


# --------------------------------------------------------------------------
# Patching semantics
# --------------------------------------------------------------------------


class TestPatching:
    def test_patch_inserts_model_before_closing_paren(self, tmp_path):
        lib = _make_library(tmp_path)
        resolver = make_library_resolver(lib)
        new_text, report = add_model_refs_to_text(PCB_TEXT, resolver)

        assert report.patched == ["Resistor_SMD:R_0805_2012Metric"]
        assert report.no_model_in_library == ["MountingHole:MountingHole_3.2mm_M3"]
        assert report.unresolved == ["Custom_Lib:Not_A_Standard_Part"]
        assert (
            '\t\t(model "${KICAD10_3DMODEL_DIR}/Resistor_SMD.3dshapes/'
            'R_0805_2012Metric.step"' in new_text
        )
        # Model node inserted inside the footprint block, after embedded_fonts.
        assert new_text.index("(embedded_fonts no)") < new_text.index("(model ")
        assert new_text.index("(model ") < new_text.index("(segment")

    def test_patch_is_pure_insertion(self, tmp_path):
        """The diff must contain only added (model ...) lines — no other bytes."""
        lib = _make_library(tmp_path)
        resolver = make_library_resolver(lib)
        new_text, _ = add_model_refs_to_text(PCB_TEXT, resolver)

        diff = list(difflib.unified_diff(PCB_TEXT.splitlines(), new_text.splitlines(), n=0))
        removed = [l for l in diff if l.startswith("-") and not l.startswith("---")]
        added = [l for l in diff if l.startswith("+") and not l.startswith("+++")]
        assert removed == []
        assert added  # something was inserted
        for line in added:
            body = line[1:].strip()
            assert body.startswith(("(model", "(offset", "(scale", "(rotate", "(xyz", ")"))

    def test_patch_is_idempotent(self, tmp_path):
        lib = _make_library(tmp_path)
        resolver = make_library_resolver(lib)
        once, report1 = add_model_refs_to_text(PCB_TEXT, resolver)
        twice, report2 = add_model_refs_to_text(once, resolver)
        assert twice == once
        assert report2.patched == []
        assert "Resistor_SMD:R_0805_2012Metric" in report2.already_present

    def test_existing_model_untouched(self, tmp_path):
        lib = _make_library(tmp_path)
        resolver = make_library_resolver(lib)
        text = PCB_TEXT.replace(
            "\t\t(embedded_fonts no)\n",
            '\t\t(model "custom.step"\n\t\t\t(offset (xyz 0 0 0))\n\t\t)\n'
            "\t\t(embedded_fonts no)\n",
        )
        new_text, report = add_model_refs_to_text(text, resolver)
        assert report.patched == []
        assert new_text == text
        assert new_text.count("custom.step") == 1


# --------------------------------------------------------------------------
# Same-library variant fallback (visual model lookup)
# --------------------------------------------------------------------------

QFN_PCB_TEXT = """(kicad_pcb
\t(version 20240108)
\t(footprint "Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm"
\t\t(layer "F.Cu")
\t\t(at 100 100)
\t)
)
"""


def _make_variant_library(tmp_path: Path) -> LibraryPaths:
    root = tmp_path / "footprints"
    lib = root / "Package_DFN_QFN.pretty"
    lib.mkdir(parents=True)
    model = (
        '(footprint "{name}"\n'
        '\t(layer "F.Cu")\n'
        '\t(model "${{KICAD10_3DMODEL_DIR}}/Package_DFN_QFN.3dshapes/{name}.step"\n'
        "\t\t(offset\n\t\t\t(xyz 0 0 0)\n\t\t)\n"
        "\t)\n"
        ")\n"
    )
    for name in (
        "QFN-24-1EP_4x4mm_P0.5mm_EP2.6x2.6mm",
        "QFN-24-1EP_4x4mm_P0.5mm_EP2.6x2.6mm_ThermalVias",
        "QFN-24-1EP_4x4mm_P0.5mm_EP2.15x2.15mm",
    ):
        (lib / f"{name}.kicad_mod").write_text(model.format(name=name))
    return LibraryPaths(footprints_path=root, source="config")


class TestVariantFallback:
    def test_variant_match_used_and_reported(self, tmp_path):
        lib = _make_variant_library(tmp_path)
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(QFN_PCB_TEXT)
        report = add_model_refs(pcb, library_paths=lib)
        assert report.patched == ["Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm"]
        # Deterministic pick: non-ThermalVias variants first, then shortest name.
        assert report.variant_matches == {
            "Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm": ("QFN-24-1EP_4x4mm_P0.5mm_EP2.6x2.6mm")
        }
        assert "EP2.6x2.6mm.step" in pcb.read_text()
        assert "ThermalVias" not in pcb.read_text()

    def test_exact_mode_skips_variants(self, tmp_path):
        lib = _make_variant_library(tmp_path)
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(QFN_PCB_TEXT)
        report = add_model_refs(pcb, library_paths=lib, allow_variants=False)
        assert report.patched == []
        assert report.unresolved == ["Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm"]
        assert pcb.read_text() == QFN_PCB_TEXT

    def test_variant_requires_separator_boundary(self, tmp_path):
        """A name that merely shares a prefix (no _/- boundary) must not match."""
        root = tmp_path / "footprints"
        lib = root / "Package_DFN_QFN.pretty"
        lib.mkdir(parents=True)
        (lib / "QFN-241EP.kicad_mod").write_text('(footprint "QFN-241EP")\n')
        paths = LibraryPaths(footprints_path=root, source="config")
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(QFN_PCB_TEXT.replace("QFN-24-1EP_4x4mm_P0.5mm", "QFN-24"))
        report = add_model_refs(pcb, library_paths=paths)
        assert report.patched == []


# --------------------------------------------------------------------------
# File-level API + parseability
# --------------------------------------------------------------------------


class TestFileAPI:
    def test_add_model_refs_writes_in_place(self, tmp_path):
        lib = _make_library(tmp_path)
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(PCB_TEXT)
        report = add_model_refs(pcb, library_paths=lib)
        assert report.changed
        assert "(model " in pcb.read_text()

    def test_dry_run_leaves_file_untouched(self, tmp_path):
        lib = _make_library(tmp_path)
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(PCB_TEXT)
        report = add_model_refs(pcb, library_paths=lib, dry_run=True)
        assert report.changed
        assert pcb.read_text() == PCB_TEXT

    def test_output_path(self, tmp_path):
        lib = _make_library(tmp_path)
        pcb = tmp_path / "board.kicad_pcb"
        out = tmp_path / "patched.kicad_pcb"
        pcb.write_text(PCB_TEXT)
        add_model_refs(pcb, output_path=out, library_paths=lib)
        assert pcb.read_text() == PCB_TEXT
        assert "(model " in out.read_text()

    def test_patched_pcb_still_parses_and_roundtrips_models(self, tmp_path):
        """PCB.load/save must preserve the inserted model nodes (writer
        round-trip): later kct rewrites must not drop the 3D refs."""
        from kicad_tools.schema.pcb import PCB

        lib = _make_library(tmp_path)
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(PCB_TEXT)
        add_model_refs(pcb_path, library_paths=lib)

        pcb = PCB.load(pcb_path)
        assert len(pcb.footprints) == 3
        out = tmp_path / "resaved.kicad_pcb"
        pcb.save(out)
        assert out.read_text().count("(model ") == 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


class TestCLI:
    def test_run_add_3d_models(self, tmp_path, capsys):
        from kicad_tools.cli.pcb_add_3d_models import run_add_3d_models

        _make_library(tmp_path)
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(PCB_TEXT)

        rc = run_add_3d_models(pcb, lib_path=tmp_path / "footprints", output_format="json")
        assert rc == 0
        import json

        payload = json.loads(capsys.readouterr().out)
        assert payload["patched"] == ["Resistor_SMD:R_0805_2012Metric"]
        assert payload["unresolved"] == ["Custom_Lib:Not_A_Standard_Part"]
        assert "(model " in pcb.read_text()

    def test_run_add_3d_models_missing_library(self, tmp_path, monkeypatch, capsys):
        import kicad_tools.footprints.library_path as lp
        from kicad_tools.cli.pcb_add_3d_models import run_add_3d_models

        monkeypatch.setattr(
            lp,
            "detect_kicad_library_path",
            lambda config_override=None: lp.LibraryPaths(footprints_path=None, source="auto"),
        )
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(PCB_TEXT)
        rc = run_add_3d_models(pcb, output_format="text")
        assert rc == 1
        assert "not found" in capsys.readouterr().err
