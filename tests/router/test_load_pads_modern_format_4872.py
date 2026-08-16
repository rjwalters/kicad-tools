"""Regression tests for footprint references on modern-format boards (#4872).

``load_pads_for_analysis`` used to read the footprint reference only from the
legacy ``(fp_text reference U1 ...)`` spelling.  KiCad 7+ writes the reference
as ``(property "Reference" "U1" ...)``, so on any modern board every pad came
back with ``ref=""`` — the pads were positionally correct but anonymous, and
every per-component consumer (``identify_fine_pitch_components`` and therefore
multi-resolution / adaptive-grid escape planning) silently no-opped.

These tests pin both spellings, their precedence, and the downstream
per-component bucketing.
"""

from pathlib import Path

import pytest

from kicad_tools.router.adaptive_grid import identify_fine_pitch_components
from kicad_tools.router.io import load_pads_for_analysis

REPO_ROOT = Path(__file__).resolve().parents[2]
MODERN_BOARD = (
    REPO_ROOT / "boards" / "02-charlieplex-led" / "output" / "charlieplex_3x3_routed.kicad_pcb"
)


def _modern_pcb_text() -> str:
    """Two footprints in the KiCad 7+ ``(property "Reference" ...)`` spelling.

    ``U1`` is a 0.5 mm-pitch (fine-pitch) 4-pad part; ``R1`` is a 2 mm-pitch
    0805 that must NOT be classified fine-pitch.  Structure mirrors a real
    pcbnew-resaved file: the footprint ``(at ...)`` precedes the properties,
    and each property carries its own nested ``(at ...)``.
    """
    return """(kicad_pcb
	(version 20241229)
	(generator "pcbnew")
	(footprint "Package_SO:TSSOP-4"
		(layer "F.Cu")
		(uuid "aaaaaaaa-0000-0000-0000-000000000001")
		(at 100 100)
		(property "Reference" "U1"
			(at 0 -2 0)
			(layer "F.SilkS")
			(effects (font (size 1 1) (thickness 0.15)))
		)
		(property "Value" "TSSOP-4"
			(at 0 2 0)
			(layer "F.Fab")
			(effects (font (size 1 1) (thickness 0.15)))
		)
		(pad "1" smd rect
			(at -0.75 0)
			(size 0.3 1.0)
			(layers "F.Cu" "F.Mask" "F.Paste")
			(net 1 "NET_A")
		)
		(pad "2" smd rect
			(at -0.25 0)
			(size 0.3 1.0)
			(layers "F.Cu" "F.Mask" "F.Paste")
			(net 2 "NET_B")
		)
		(pad "3" smd rect
			(at 0.25 0)
			(size 0.3 1.0)
			(layers "F.Cu" "F.Mask" "F.Paste")
			(net 3 "NET_C")
		)
		(pad "4" smd rect
			(at 0.75 0)
			(size 0.3 1.0)
			(layers "F.Cu" "F.Mask" "F.Paste")
			(net 4 "NET_D")
		)
	)
	(footprint "Resistor_SMD:R_0805_2012Metric"
		(layer "F.Cu")
		(uuid "aaaaaaaa-0000-0000-0000-000000000002")
		(at 110 100)
		(property "Reference" "R1"
			(at 0 -1.5 0)
			(layer "F.SilkS")
			(effects (font (size 1 1) (thickness 0.15)))
		)
		(property "Value" "330R"
			(at 0 1.5 0)
			(layer "F.Fab")
			(effects (font (size 1 1) (thickness 0.15)))
		)
		(pad "1" smd roundrect
			(at -1 0)
			(size 1 1.3)
			(layers "F.Cu" "F.Mask" "F.Paste")
			(net 4 "NET_D")
		)
		(pad "2" smd roundrect
			(at 1 0)
			(size 1 1.3)
			(layers "F.Cu" "F.Mask" "F.Paste")
			(net 5 "NET_E")
		)
	)
)"""


def _legacy_pcb_text() -> str:
    """The same two parts in the pre-KiCad-7 ``(fp_text reference ...)`` spelling."""
    return """(kicad_pcb (version 20221018)
	(footprint "Package_SO:TSSOP-4" (at 100 100)
		(fp_text reference "U1" (at 0 -2))
		(fp_text value "TSSOP-4" (at 0 2))
		(pad "1" smd rect (at -0.75 0) (size 0.3 1.0) (layers "F.Cu") (net 1 "NET_A"))
		(pad "2" smd rect (at -0.25 0) (size 0.3 1.0) (layers "F.Cu") (net 2 "NET_B"))
		(pad "3" smd rect (at 0.25 0) (size 0.3 1.0) (layers "F.Cu") (net 3 "NET_C"))
		(pad "4" smd rect (at 0.75 0) (size 0.3 1.0) (layers "F.Cu") (net 4 "NET_D"))
	)
	(footprint "Resistor_SMD:R_0805_2012Metric" (at 110 100)
		(fp_text reference "R1" (at 0 -1.5))
		(pad "1" smd roundrect (at -1 0) (size 1 1.3) (layers "F.Cu") (net 4 "NET_D"))
		(pad "2" smd roundrect (at 1 0) (size 1 1.3) (layers "F.Cu") (net 5 "NET_E"))
	)
)"""


class TestModernReferenceSpelling:
    """``(property "Reference" ...)`` must yield real refs, not empty strings."""

    def test_modern_property_reference_is_parsed(self):
        pads = load_pads_for_analysis(_modern_pcb_text())

        assert len(pads) == 6
        assert sorted({p.ref for p in pads}) == ["R1", "U1"]
        assert all(p.ref for p in pads), "no pad may carry an empty ref"

    def test_modern_refs_map_to_the_right_pads(self):
        pads = load_pads_for_analysis(_modern_pcb_text())
        by_ref: dict[str, list] = {}
        for pad in pads:
            by_ref.setdefault(pad.ref, []).append(pad)

        assert sorted(p.pin for p in by_ref["U1"]) == ["1", "2", "3", "4"]
        assert sorted(p.pin for p in by_ref["R1"]) == ["1", "2"]
        # U1 sits at (100, 100); R1 at (110, 100) — refs are not shuffled.
        assert all(99.0 < p.x < 101.0 for p in by_ref["U1"])
        assert all(108.0 < p.x < 112.0 for p in by_ref["R1"])

    def test_legacy_fp_text_reference_still_parsed(self):
        """The legacy spelling must keep working unchanged."""
        pads = load_pads_for_analysis(_legacy_pcb_text())

        assert len(pads) == 6
        assert sorted({p.ref for p in pads}) == ["R1", "U1"]

    def test_unquoted_legacy_fp_text_reference_still_parsed(self):
        """KiCad 5-era files wrote the reference unquoted."""
        text = (
            "(kicad_pcb (version 20171130)\n"
            '  (module "Package_SO:TSSOP-4" (at 100 100)\n'
            "    (fp_text reference U1 (at 0 -2))\n"
            '    (pad "1" smd rect (at 0 0) (size 0.3 1.0) (layers "F.Cu") (net 1 "NET_A"))\n'
            "  )\n"
            ")"
        )
        # Rewritten as (footprint ...) — this loader only splits on (footprint.
        pads = load_pads_for_analysis(text.replace("(module ", "(footprint "))

        assert len(pads) == 1
        assert pads[0].ref == "U1"

    def test_property_takes_precedence_over_fp_text(self):
        """A transitional file may carry both; the property spelling wins.

        Matches ``load_pcb_for_routing``'s existing precedence order.
        """
        text = """(kicad_pcb (version 20241229)
	(footprint "Package_SO:TSSOP-4"
		(layer "F.Cu")
		(at 100 100)
		(property "Reference" "U7"
			(at 0 -2 0)
			(layer "F.SilkS")
		)
		(fp_text reference "U1" (at 0 -2) (layer "F.SilkS"))
		(pad "1" smd rect (at 0 0) (size 0.3 1.0) (layers "F.Cu") (net 1 "NET_A"))
	)
)"""
        pads = load_pads_for_analysis(text)

        assert len(pads) == 1
        assert pads[0].ref == "U7"


class TestFinePitchBucketingOnModernBoards:
    """The concrete downstream consumer that silently no-opped before #4872."""

    def test_fine_pitch_components_keyed_by_real_ref(self):
        pads = load_pads_for_analysis(_modern_pcb_text())

        fine = identify_fine_pitch_components(pads, coarse_resolution=0.2)

        # U1's 0.5mm pitch trips the 0.8mm default threshold; R1's 2mm does not.
        assert "U1" in fine, "fine-pitch escape never fires when refs are empty"
        assert "R1" not in fine
        assert "" not in fine
        assert fine["U1"] > 0.0

    def test_fine_pitch_bucketing_matches_legacy_spelling(self):
        """Modern and legacy spellings of the same board classify identically."""
        modern = identify_fine_pitch_components(
            load_pads_for_analysis(_modern_pcb_text()), coarse_resolution=0.2
        )
        legacy = identify_fine_pitch_components(
            load_pads_for_analysis(_legacy_pcb_text()), coarse_resolution=0.2
        )

        assert modern == legacy


class TestCommittedModernBoardFixture:
    """Ground truth from a real pcbnew-resaved board committed to this repo."""

    def test_charlieplex_routed_board_yields_real_refs(self):
        if not MODERN_BOARD.exists():  # pragma: no cover - fixture is committed
            pytest.skip(f"fixture not present: {MODERN_BOARD}")

        pads = load_pads_for_analysis(MODERN_BOARD)
        refs = {p.ref for p in pads}

        # 14 footprints / 34 pads, cross-checked against kicad_tools.schema.PCB.
        assert len(pads) == 34
        assert len(refs) == 14
        assert "" not in refs

    def test_charlieplex_refs_agree_with_schema_loader(self):
        if not MODERN_BOARD.exists():  # pragma: no cover - fixture is committed
            pytest.skip(f"fixture not present: {MODERN_BOARD}")

        from kicad_tools.schema import PCB

        pcb = PCB.load(MODERN_BOARD)
        schema_refs = {fp.reference for fp in pcb.footprints}
        io_refs = {p.ref for p in load_pads_for_analysis(MODERN_BOARD)}

        assert io_refs == schema_refs
