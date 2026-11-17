"""Tests for stackup-derived copper layer selection in Gerber export (issue #3559).

``GerberExporter._get_default_layers`` previously hardcoded ``["F.Cu", "B.Cu"]``
so any 4-layer board exported through ``kct export`` shipped Gerbers with no
inner plane copper (softstart's In1.Cu GND and In2.Cu +3.3V planes were absent
from gerbers.zip while the .gbrjob declared LayerNumber 4).

The fix derives the copper layer set from the PCB's actual ``(layers ...)``
table via :func:`kicad_tools.export.gerber._pcb_copper_layers`.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.export.gerber import (
    GerberConfig,
    GerberExporter,
    _pcb_copper_layers,
)

# ---------------------------------------------------------------------------
# Synthetic PCB layer tables
# ---------------------------------------------------------------------------

# KiCad 9+ numbering: F.Cu=0, B.Cu=2, inner layers at even ordinals 4, 6, ...
FOUR_LAYER_KICAD9 = """(kicad_pcb
\t(version 20241229)
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(4 "In1.Cu" signal)
\t\t(6 "In2.Cu" signal)
\t\t(2 "B.Cu" signal)
\t\t(1 "F.Mask" user)
\t\t(3 "B.Mask" user)
\t\t(25 "Edge.Cuts" user)
\t)
\t(net 0 "")
)
"""

# Legacy numbering: F.Cu=0, inner layers 1..30, B.Cu=31.
FOUR_LAYER_LEGACY = """(kicad_pcb
  (version 20240108)
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" power)
    (31 "B.Cu" signal)
    (39 "F.Mask" user)
    (44 "Edge.Cuts" user)
  )
)
"""

TWO_LAYER = """(kicad_pcb
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
)
"""


class TestPcbCopperLayers:
    """Unit tests for the stackup text-scan."""

    def test_two_layer_board(self, tmp_path):
        pcb = tmp_path / "two.kicad_pcb"
        pcb.write_text(TWO_LAYER)
        assert _pcb_copper_layers(pcb) == ["F.Cu", "B.Cu"]

    def test_four_layer_kicad9_numbering(self, tmp_path):
        """KiCad 9+ files number B.Cu=2 and inner layers 4, 6, ...

        Plot order must still be front-to-back by *name*, not by ordinal.
        """
        pcb = tmp_path / "four9.kicad_pcb"
        pcb.write_text(FOUR_LAYER_KICAD9)
        assert _pcb_copper_layers(pcb) == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]

    def test_four_layer_legacy_numbering(self, tmp_path):
        pcb = tmp_path / "four_legacy.kicad_pcb"
        pcb.write_text(FOUR_LAYER_LEGACY)
        assert _pcb_copper_layers(pcb) == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]

    def test_inner_layers_sort_numerically(self, tmp_path):
        """In10.Cu must sort after In2.Cu (numeric, not lexicographic)."""
        pcb = tmp_path / "many.kicad_pcb"
        pcb.write_text(
            "(kicad_pcb (layers\n"
            '  (0 "F.Cu" signal)\n'
            '  (4 "In1.Cu" signal)\n'
            '  (24 "In10.Cu" signal)\n'
            '  (6 "In2.Cu" signal)\n'
            '  (2 "B.Cu" signal)\n'
            "))\n"
        )
        assert _pcb_copper_layers(pcb) == [
            "F.Cu",
            "In1.Cu",
            "In2.Cu",
            "In10.Cu",
            "B.Cu",
        ]

    def test_pad_layer_lists_are_ignored(self, tmp_path):
        """Per-pad ``(layers "F.Cu" ...)`` lists must not pollute the result."""
        pcb = tmp_path / "pads.kicad_pcb"
        pcb.write_text(
            TWO_LAYER.rstrip()[:-1]  # strip trailing close paren
            + '\n  (footprint "R_0402"\n'
            '    (pad "1" smd rect (layers "F.Cu" "F.Paste" "F.Mask"))\n'
            '    (pad "2" thru_hole circle (layers "In7.Cu"))\n'
            "  )\n"
            ")\n"
        )
        assert _pcb_copper_layers(pcb) == ["F.Cu", "B.Cu"]

    def test_missing_file_falls_back_to_two_layer(self, tmp_path):
        assert _pcb_copper_layers(tmp_path / "missing.kicad_pcb") == ["F.Cu", "B.Cu"]

    def test_no_layer_table_falls_back_to_two_layer(self, tmp_path):
        pcb = tmp_path / "empty.kicad_pcb"
        pcb.write_text('(kicad_pcb (version 20240108) (net 0 ""))\n')
        assert _pcb_copper_layers(pcb) == ["F.Cu", "B.Cu"]

    def test_power_and_mixed_layer_types_detected(self, tmp_path):
        """Inner planes are often declared as ``power`` rather than ``signal``."""
        pcb = tmp_path / "power.kicad_pcb"
        pcb.write_text(
            "(kicad_pcb (layers\n"
            '  (0 "F.Cu" signal)\n'
            '  (4 "In1.Cu" power)\n'
            '  (6 "In2.Cu" mixed)\n'
            '  (2 "B.Cu" signal)\n'
            "))\n"
        )
        assert _pcb_copper_layers(pcb) == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]

    def test_generated_pcb_create_four_layer(self, tmp_path):
        """A board produced by our own ``PCB.create(layers=4)`` is detected."""
        from kicad_tools.schema.pcb import PCB

        pcb_path = tmp_path / "generated.kicad_pcb"
        PCB.create(width=20, height=20, layers=4, title="t").save(pcb_path)
        assert _pcb_copper_layers(pcb_path) == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


# ---------------------------------------------------------------------------
# _get_default_layers integration
# ---------------------------------------------------------------------------


def _make_exporter(pcb_path: Path) -> GerberExporter:
    with patch.object(GerberExporter, "__init__", lambda self, path: None):
        exporter = GerberExporter.__new__(GerberExporter)
        exporter.pcb_path = pcb_path
        exporter.kicad_cli = Path("/usr/bin/kicad-cli")
        return exporter


class TestGetDefaultLayers:
    def test_four_layer_board_includes_inner_copper(self, tmp_path):
        pcb = tmp_path / "four.kicad_pcb"
        pcb.write_text(FOUR_LAYER_KICAD9)
        exporter = _make_exporter(pcb)

        layers = exporter._get_default_layers(GerberConfig())

        # Copper layers first, in front-to-back order.
        assert layers[:4] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
        # Non-copper defaults still present.
        assert "F.SilkS" in layers
        assert "F.Mask" in layers
        assert "Edge.Cuts" in layers

    def test_two_layer_board_unchanged(self, tmp_path):
        pcb = tmp_path / "two.kicad_pcb"
        pcb.write_text(TWO_LAYER)
        exporter = _make_exporter(pcb)

        layers = exporter._get_default_layers(GerberConfig())

        assert layers[:2] == ["F.Cu", "B.Cu"]
        assert not any(layer.startswith("In") for layer in layers)

    def test_explicit_config_layers_take_precedence(self, tmp_path):
        """config.layers overrides stackup detection (existing contract)."""
        pcb = tmp_path / "four.kicad_pcb"
        pcb.write_text(FOUR_LAYER_KICAD9)
        exporter = _make_exporter(pcb)

        captured: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            idx = cmd.index("--layers")
            captured.append(cmd[idx + 1].split(","))

            class R:
                stdout = ""

            return R()

        config = GerberConfig(layers=["F.Cu", "Edge.Cuts"])
        with patch("kicad_tools.export.gerber.subprocess.run", side_effect=fake_run):
            exporter._export_gerbers_impl(config, tmp_path, pcb)

        assert captured == [["F.Cu", "Edge.Cuts"]]

    def test_export_impl_passes_inner_layers_to_kicad_cli(self, tmp_path):
        """The --layers argument handed to kicad-cli must include inner copper."""
        pcb = tmp_path / "four.kicad_pcb"
        pcb.write_text(FOUR_LAYER_KICAD9)
        exporter = _make_exporter(pcb)

        captured: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            idx = cmd.index("--layers")
            captured.append(cmd[idx + 1].split(","))

            class R:
                stdout = ""

            return R()

        with patch("kicad_tools.export.gerber.subprocess.run", side_effect=fake_run):
            exporter._export_gerbers_impl(GerberConfig(), tmp_path, pcb)

        assert len(captured) == 1
        assert captured[0][:4] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


# ---------------------------------------------------------------------------
# End-to-end: real kicad-cli export of a synthetic 4-layer board
# ---------------------------------------------------------------------------


def _kicad_cli_available() -> bool:
    from kicad_tools.cli.runner import find_kicad_cli

    return find_kicad_cli() is not None


@pytest.mark.skipif(not _kicad_cli_available(), reason="kicad-cli not installed")
class TestFourLayerExportEndToEnd:
    """Export a synthetic 4-layer board and verify the shipped artifacts."""

    @pytest.fixture(scope="class")
    def exported_zip(self, tmp_path_factory) -> Path:
        from kicad_tools.schema.pcb import PCB

        tmp = tmp_path_factory.mktemp("gerber_4layer")
        pcb_path = tmp / "four_layer.kicad_pcb"
        PCB.create(width=20, height=20, layers=4, title="4L test").save(pcb_path)

        exporter = GerberExporter(pcb_path)
        return exporter.export_for_manufacturer("jlcpcb", tmp / "out")

    def test_zip_contains_all_copper_layers(self, exported_zip):
        with zipfile.ZipFile(exported_zip) as zf:
            names = zf.namelist()

        for fragment in ("F_Cu", "In1_Cu", "In2_Cu", "B_Cu"):
            assert any(fragment in n for n in names), (
                f"copper layer {fragment} missing from {sorted(names)}"
            )

    def test_gbrjob_layer_count_matches_copper_files(self, exported_zip):
        """The .gbrjob LayerNumber must match the copper files actually shipped."""
        with zipfile.ZipFile(exported_zip) as zf:
            job_names = [n for n in zf.namelist() if n.endswith(".gbrjob")]
            assert len(job_names) == 1, f"expected one .gbrjob, got {job_names}"
            job = json.loads(zf.read(job_names[0]))

            declared = job["GeneralSpecs"]["LayerNumber"]
            copper_files = [
                f for f in job["FilesAttributes"] if f.get("FileFunction", "").startswith("Copper")
            ]
            # Every copper file the job declares must actually be in the zip.
            names = set(zf.namelist())
            missing = [f["Path"] for f in copper_files if f["Path"] not in names]

        assert declared == 4
        assert len(copper_files) == 4, (
            f"gbrjob declares {declared} layers but lists {len(copper_files)} copper files"
        )
        assert missing == [], f"gbrjob references copper files absent from zip: {missing}"
