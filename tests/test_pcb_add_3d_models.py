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
    ResolvedModels,
    _apply_offset_delta,
    _pad_anchor,
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
# Cross-library substitution tier (curated lib_id -> lib_id equivalents)
# --------------------------------------------------------------------------

# A synthetic lib id with no exact match and no same-library variant, but a
# curated substitute in a *different* library.
SUBSTITUTION_PCB_TEXT = """(kicad_pcb
\t(version 20240108)
\t(footprint "Connector_FFC:FFC_4P_0.5mm"
\t\t(layer "F.Cu")
\t\t(at 100 100)
\t)
)
"""


def _make_substitution_library(tmp_path: Path) -> LibraryPaths:
    """Build a library where only the *substitute* library exists.

    The requested ``Connector_FFC`` library is entirely absent (mirrors the
    real upstream rename to ``Connector_FFC-FPC``), so only the cross-library
    substitution tier can resolve it.
    """
    root = tmp_path / "footprints"
    lib = root / "Connector_FFC-FPC.pretty"
    lib.mkdir(parents=True)
    name = "Amphenol_F32Q-1A7x1-11004_1x04-1MP_P0.5mm_Horizontal"
    model = (
        f'(footprint "{name}"\n'
        '\t(layer "F.Cu")\n'
        f'\t(model "${{KICAD10_3DMODEL_DIR}}/Connector_FFC-FPC.3dshapes/{name}.step"\n'
        "\t\t(offset\n\t\t\t(xyz 0 0 0)\n\t\t)\n"
        "\t)\n"
        ")\n"
    )
    (lib / f"{name}.kicad_mod").write_text(model)
    return LibraryPaths(footprints_path=root, source="config")


class TestCrossLibrarySubstitution:
    def test_substitution_match_used_and_reported(self, tmp_path):
        lib = _make_substitution_library(tmp_path)
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(SUBSTITUTION_PCB_TEXT)
        report = add_model_refs(pcb, library_paths=lib)
        assert report.patched == ["Connector_FFC:FFC_4P_0.5mm"]
        # Reported as a cross-library substitution, NOT a same-library variant.
        assert report.substitution_matches == {
            "Connector_FFC:FFC_4P_0.5mm": (
                "Connector_FFC-FPC:Amphenol_F32Q-1A7x1-11004_1x04-1MP_P0.5mm_Horizontal"
            )
        }
        assert report.variant_matches == {}
        assert "Connector_FFC-FPC.3dshapes" in pcb.read_text()

    def test_substitution_disabled_leaves_unresolved(self, tmp_path):
        lib = _make_substitution_library(tmp_path)
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(SUBSTITUTION_PCB_TEXT)
        report = add_model_refs(pcb, library_paths=lib, allow_substitutions=False)
        assert report.patched == []
        assert report.unresolved == ["Connector_FFC:FFC_4P_0.5mm"]
        assert pcb.read_text() == SUBSTITUTION_PCB_TEXT

    def test_exact_match_never_redirected_to_substitution(self, tmp_path):
        """A lib id that resolves exactly must NOT hit the substitution tier."""
        lib = _make_library(tmp_path)
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(PCB_TEXT)
        report = add_model_refs(pcb, library_paths=lib)
        assert report.patched == ["Resistor_SMD:R_0805_2012Metric"]
        assert report.substitution_matches == {}

    def test_variant_beats_substitution(self, tmp_path):
        """When a same-library variant exists, it wins over substitution.

        Uses a lib id in the substitution table but provides a same-library
        variant; the resolver must report a variant match, not a substitution.
        """
        root = tmp_path / "footprints"
        lib = root / "Package_BGA.pretty"
        lib.mkdir(parents=True)
        name = "BGA-49_5.0x5.0mm_Layout7x7_P0.5mm_ExtraSuffix"
        model = (
            f'(footprint "{name}"\n'
            '\t(layer "F.Cu")\n'
            f'\t(model "${{KICAD10_3DMODEL_DIR}}/Package_BGA.3dshapes/{name}.step"\n'
            "\t\t(offset\n\t\t\t(xyz 0 0 0)\n\t\t)\n"
            "\t)\n"
            ")\n"
        )
        (lib / f"{name}.kicad_mod").write_text(model)
        paths = LibraryPaths(footprints_path=root, source="config")
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(
            SUBSTITUTION_PCB_TEXT.replace(
                "Connector_FFC:FFC_4P_0.5mm",
                "Package_BGA:BGA-49_5.0x5.0mm_Layout7x7_P0.5mm",
            )
        )
        report = add_model_refs(pcb, library_paths=paths)
        assert report.patched == ["Package_BGA:BGA-49_5.0x5.0mm_Layout7x7_P0.5mm"]
        assert report.variant_matches == {"Package_BGA:BGA-49_5.0x5.0mm_Layout7x7_P0.5mm": name}
        assert report.substitution_matches == {}

    def test_substitution_reported_in_cli_json(self, tmp_path, capsys):
        from kicad_tools.cli.pcb_add_3d_models import run_add_3d_models

        _make_substitution_library(tmp_path)
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(SUBSTITUTION_PCB_TEXT)
        rc = run_add_3d_models(pcb, lib_path=tmp_path / "footprints", output_format="json")
        assert rc == 0
        import json

        payload = json.loads(capsys.readouterr().out)
        assert payload["patched"] == ["Connector_FFC:FFC_4P_0.5mm"]
        assert payload["substitution_matches"] == {
            "Connector_FFC:FFC_4P_0.5mm": (
                "Connector_FFC-FPC:Amphenol_F32Q-1A7x1-11004_1x04-1MP_P0.5mm_Horizontal"
            )
        }


# --------------------------------------------------------------------------
# Substitution table integrity
# --------------------------------------------------------------------------


class TestSubstitutionTable:
    def test_table_covers_required_lib_ids(self):
        from kicad_tools.pcb.model_substitutions import MODEL_SUBSTITUTIONS

        required = {
            "Connector_FFC:FFC_4P_0.5mm",
            "Connector_FFC:FFC_6P_1.0mm",
            "Connector_USB:USB_C_Receptacle_USB2.0",
            "Connector_Video:HDMI_A_Receptacle",
            "Package_BGA:BGA-49_5.0x5.0mm_Layout7x7_P0.5mm",
        }
        assert required <= set(MODEL_SUBSTITUTIONS)

    def test_substitute_lib_id_helper(self):
        from kicad_tools.pcb.model_substitutions import substitute_lib_id

        assert substitute_lib_id("Connector_FFC:FFC_4P_0.5mm") is not None
        assert substitute_lib_id("Nonexistent:Thing") is None


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


# --------------------------------------------------------------------------
# Origin-convention offset (issue #4034)
# --------------------------------------------------------------------------
#
# kct-generated footprints reuse canonical KiCad names but place pads on an
# *origin-centered* convention, while the library footprint (and its STEP
# model) uses pad-1-at-origin.  Copying the model with a zero offset leaves
# the body shifted by the pad-centroid delta (half the pitch for a 2-pad
# part).  The patcher must add ``target_centroid - source_centroid`` into the
# model ``(offset (xyz ...))`` -- with the model-frame Y negated relative to
# the footprint 2D frame -- so the body lands on the target's pads.

# Library footprint: pad 1 at origin, pad 2 one 2.54mm pitch up in +Y
# (KiCad's convention for a vertical 2-pin header).  Centroid = (0, 1.27).
HEADER_LIB_MOD = """(footprint "PinHeader_1x02_P2.54mm_Vertical"
\t(layer "F.Cu")
\t(pad "1" thru_hole rect
\t\t(at 0 0)
\t\t(size 1.7 1.7)
\t\t(drill 1)
\t\t(layers "*.Cu" "*.Mask")
\t)
\t(pad "2" thru_hole oval
\t\t(at 0 2.54)
\t\t(size 1.7 1.7)
\t\t(drill 1)
\t\t(layers "*.Cu" "*.Mask")
\t)
\t(model "${KICAD10_3DMODEL_DIR}/Connector_PinHeader_2.54mm.3dshapes/PinHeader_1x02_P2.54mm_Vertical.step"
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

# Board footprint reusing that lib id but origin-centered: pads at (0, -1.27)
# and (0, +1.27).  Centroid = (0, 0).  Expected delta = (0, 0) - (0, 1.27) =
# (0, -1.27); model-frame offset = (0 + 0, 0 - (-1.27), 0) = (0, 1.27, 0).
HEADER_PCB_TEXT = """(kicad_pcb
\t(version 20240108)
\t(footprint "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"
\t\t(layer "F.Cu")
\t\t(at 100 100)
\t\t(pad "1" thru_hole rect
\t\t\t(at 0 -1.27)
\t\t\t(size 1.7 1.7)
\t\t\t(drill 1)
\t\t\t(layers "*.Cu" "*.Mask")
\t\t)
\t\t(pad "2" thru_hole oval
\t\t\t(at 0 1.27)
\t\t\t(size 1.7 1.7)
\t\t\t(drill 1)
\t\t\t(layers "*.Cu" "*.Mask")
\t\t)
\t)
)
"""


def _make_header_library(tmp_path: Path) -> LibraryPaths:
    root = tmp_path / "footprints"
    lib = root / "Connector_PinHeader_2.54mm.pretty"
    lib.mkdir(parents=True)
    (lib / "PinHeader_1x02_P2.54mm_Vertical.kicad_mod").write_text(HEADER_LIB_MOD)
    return LibraryPaths(footprints_path=root, source="config")


class TestPadAnchorHelper:
    def test_centroid_of_two_pads(self):
        block = (
            '(footprint "x"\n'
            '\t(pad "1" thru_hole rect (at 0 -1.27) (size 1 1))\n'
            '\t(pad "2" thru_hole oval (at 0 1.27) (size 1 1))\n'
            ")\n"
        )
        assert _pad_anchor(block) == (0.0, 0.0)

    def test_centroid_ignores_pad_rotation_angle(self):
        block = (
            '(footprint "x"\n'
            '\t(pad "1" smd rect (at 1 2 90) (size 1 1))\n'
            '\t(pad "2" smd rect (at 3 4 270) (size 1 1))\n'
            ")\n"
        )
        assert _pad_anchor(block) == (2.0, 3.0)

    def test_no_pads_returns_none(self):
        assert _pad_anchor('(footprint "x"\n\t(layer "F.Cu")\n)\n') is None

    def test_pad_token_in_string_ignored(self):
        block = '(footprint "x"\n\t(property "n" "(pad ...)")\n\t(pad "1" smd rect (at 5 0))\n)\n'
        assert _pad_anchor(block) == (5.0, 0.0)


class TestApplyOffsetDelta:
    MODEL = '(model "a.step"\n\t(offset\n\t\t(xyz 0 0 0)\n\t)\n\t(scale\n\t\t(xyz 1 1 1)\n\t)\n)'

    def test_y_is_negated_relative_to_footprint_frame(self):
        # footprint-local delta (0, -1.27) -> model offset (0, +1.27, 0)
        out = _apply_offset_delta(self.MODEL, 0.0, -1.27)
        assert "(xyz 0 1.27 0)" in out
        # only the xyz numbers changed; scale untouched
        assert "(xyz 1 1 1)" in out

    def test_x_follows_footprint_frame(self):
        out = _apply_offset_delta(self.MODEL, -1.27, 0.0)
        assert "(xyz -1.27 0 0)" in out

    def test_zero_delta_is_verbatim(self):
        assert _apply_offset_delta(self.MODEL, 0.0, 0.0) == self.MODEL

    def test_subnanometre_delta_is_verbatim(self):
        # Centroid-averaging FP noise below 1nm must not perturb the block.
        assert _apply_offset_delta(self.MODEL, 1e-9, -1e-9) == self.MODEL

    def test_existing_nonzero_offset_is_added_to(self):
        model = '(model "a.step"\n\t(offset\n\t\t(xyz 1 2 3)\n\t)\n)'
        out = _apply_offset_delta(model, 0.5, -0.5)
        # x: 1 + 0.5 = 1.5 ; y: 2 - (-0.5) = 2.5 ; z unchanged
        assert "(xyz 1.5 2.5 3)" in out


class TestOriginConventionOffset:
    def test_centered_header_gets_half_pitch_offset(self, tmp_path):
        """A synthetic origin-centered header resolved against a pad-1-at-
        origin library footprint gets the centroid-delta offset written."""
        lib = _make_header_library(tmp_path)
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(HEADER_PCB_TEXT)
        report = add_model_refs(pcb, library_paths=lib)
        assert report.patched == ["Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"]
        text = pcb.read_text()
        # Model-frame offset: (0, 1.27, 0) (Y negated vs the (0,-1.27) delta).
        assert "(xyz 0 1.27 0)" in text
        # The offset node specifically must no longer read the zero default
        # (scale/rotate legitimately keep their own 0/1 xyz nodes).
        import re

        offset_xyz = re.search(r"\(offset\s*\(xyz ([^)]+)\)", text)
        assert offset_xyz is not None
        assert offset_xyz.group(1).strip() == "0 1.27 0"

    def test_matched_centroid_gets_zero_offset(self, tmp_path):
        """R_0805-style part: library and board share centroid (0,0) despite
        different pad pitch -> zero offset, verbatim insertion."""
        lib = _make_library(tmp_path)
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(PCB_TEXT)  # R1 pads at (-1,0)/(1,0); lib at (-0.9125,0)/(0.9125,0)
        add_model_refs(pcb, library_paths=lib)
        text = pcb.read_text()
        # The inserted R_0805 model keeps its verbatim zero offset.
        assert "R_0805_2012Metric.step" in text
        assert "(xyz 0 0 0)" in text

    def test_offset_is_pure_metadata_no_copper_delta(self, tmp_path):
        """Only the newly-inserted model block bytes differ; every pad/at line
        of the original text is preserved verbatim."""
        lib = _make_header_library(tmp_path)
        new_text, _ = add_model_refs_to_text(HEADER_PCB_TEXT, make_library_resolver(lib))
        original_lines = HEADER_PCB_TEXT.splitlines()
        new_lines = new_text.splitlines()
        # No original line removed or reordered: original is a subsequence.
        it = iter(new_lines)
        assert all(line in it for line in original_lines), "an original line moved/was dropped"
        # Every added line is model metadata.
        added = set(new_lines) - set(original_lines)
        for line in added:
            body = line.strip()
            assert body.startswith(("(model", "(offset", "(scale", "(rotate", "(xyz", ")"))

    def test_offset_applies_through_substitution_tier(self, tmp_path):
        """The offset must be computed for cross-library substitution matches
        too -- keyed off the *target* footprint's own centroid, since the
        substitute is a different physical part."""
        root = tmp_path / "footprints"
        lib = root / "Connector_FFC-FPC.pretty"
        lib.mkdir(parents=True)
        # Substitute library part: pads at (0,0) and (0.5,0) -> centroid (0.25, 0).
        sub_name = "Amphenol_F32Q-1A7x1-11004_1x04-1MP_P0.5mm_Horizontal"
        sub_mod = (
            f'(footprint "{sub_name}"\n'
            '\t(pad "1" smd rect (at 0 0) (size 0.3 1))\n'
            '\t(pad "2" smd rect (at 0.5 0) (size 0.3 1))\n'
            f'\t(model "${{KICAD10_3DMODEL_DIR}}/Connector_FFC-FPC.3dshapes/{sub_name}.step"\n'
            "\t\t(offset\n\t\t\t(xyz 0 0 0)\n\t\t)\n"
            "\t)\n"
            ")\n"
        )
        (lib / f"{sub_name}.kicad_mod").write_text(sub_mod)
        paths = LibraryPaths(footprints_path=root, source="config")
        # Target footprint (no exact/variant match): centroid (2, 3).
        pcb_text = (
            "(kicad_pcb\n"
            '\t(footprint "Connector_FFC:FFC_4P_0.5mm"\n'
            '\t\t(layer "F.Cu")\n'
            '\t\t(pad "1" smd rect (at 1 3) (size 0.3 1))\n'
            '\t\t(pad "2" smd rect (at 3 3) (size 0.3 1))\n'
            "\t)\n"
            ")\n"
        )
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(pcb_text)
        report = add_model_refs(pcb, library_paths=paths)
        assert report.substitution_matches  # went through the substitution tier
        text = pcb.read_text()
        # delta = target(2,3) - source(0.25,0) = (1.75, 3) -> model (1.75, -3, 0)
        assert "(xyz 1.75 -3 0)" in text


class TestResolvedModels:
    def test_resolver_returns_source_anchor(self, tmp_path):
        lib = _make_header_library(tmp_path)
        resolver = make_library_resolver(lib)
        resolved = resolver("Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical")
        assert isinstance(resolved, ResolvedModels)
        assert resolved.source_anchor == (0.0, 1.27)
        assert len(resolved.models) == 1

    def test_legacy_list_resolver_still_supported(self):
        """A resolver returning a bare list[str] (no anchor) inserts verbatim."""
        model = '(model "x.step"\n\t(offset\n\t\t(xyz 0 0 0)\n\t)\n)'

        def resolver(lib_id: str):
            return [model] if lib_id.endswith("PinHeader_1x02_P2.54mm_Vertical") else None

        new_text, report = add_model_refs_to_text(HEADER_PCB_TEXT, resolver)
        assert report.patched
        # No anchor available -> zero offset applied (verbatim).
        assert "(xyz 0 0 0)" in new_text
