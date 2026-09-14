"""Constructed-fixture regression tests for ``_audit_pour_nets`` (issue #5176).

``boards/06-diffpair-test/generate_design.py``'s ``_audit_pour_nets`` is the
recipe's own geometric pour-connectivity gate -- it is what decides whether
step 10b's repair loop needs to run again, and it is what the CI log's
``POUR CONNECTIVITY: PASS/FAIL`` line reports.  Issue #5176 found that its
via-copper modelling assumed every via bridges *all* copper layers
regardless of its declared span, unlike
``kicad_tools.validate.connectivity.ConnectivityValidator`` (the
independent-LVS reference this audit is meant to approximate), which
expands a via's declared endpoints across the board's *physical* copper
stack (``_via_bridged_layers``).  A via that does not reach the layer a
pour lives on can therefore be mis-treated as reaching it anyway, fusing
copper that is not actually joined -- a false ``POUR CONNECTIVITY: PASS``.

These fixtures are constructed directly (mirroring
``tests/test_connectivity.py``'s ``ConnectivityValidator`` fixtures) rather
than requiring a full board-06 regeneration, per the issue's own test-plan
instruction.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "06-diffpair-test"


def _load_module(name: str, path: Path):
    """Load a board script as a module from its absolute path."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module {name!r} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generate_design_mod():
    """Load ``boards/06-diffpair-test/generate_design.py`` as a module."""
    gp = _load_module("board_06_generate_pcb_pour", BOARD_DIR / "generate_pcb.py")
    sys.modules["generate_pcb"] = gp
    gs = _load_module("board_06_generate_schematic_pour", BOARD_DIR / "generate_schematic.py")
    sys.modules["generate_schematic"] = gs
    return _load_module("board_06_generate_design_pour", BOARD_DIR / "generate_design.py")


# A minimal 4-layer PCB: R1's GND pad sits alone on F.Cu, tied only to a via
# at its own location; R2's GND pad sits on B.Cu directly under a filled GND
# zone (the pour "anchor").  The via's declared layer span is filled in per
# test via ``{via_layers}`` -- a blind ``F.Cu``/``In1.Cu`` via never reaches
# the B.Cu pour, while a through ``F.Cu``/``B.Cu`` via does.
_FIXTURE_TEMPLATE = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS") (uuid "ref-r1"))
    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "GND"))
  )
  (footprint "Resistor_SMD:R_0402"
    (layer "B.Cu")
    (uuid "fp-r2")
    (at 130 100)
    (property "Reference" "R2" (at 0 0 0) (layer "B.SilkS") (uuid "ref-r2"))
    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "B.Cu") (net 1 "GND"))
  )
  (zone (net 1) (net_name "GND") (layer "B.Cu")
    (uuid "zone-gnd")
    (fill yes)
    (polygon
      (pts
        (xy 95 95)
        (xy 135 95)
        (xy 135 105)
        (xy 95 105)
      )
    )
    (filled_polygon
      (layer "B.Cu")
      (pts
        (xy 95 95)
        (xy 135 95)
        (xy 135 105)
        (xy 95 105)
      )
    )
  )
  (via (at 100 100) (size 0.6) (drill 0.3) (layers {via_layers}) (net 1)
    (uuid "via-1"))
)
"""


def _fixture(tmp_path: Path, via_layers: str) -> Path:
    pcb_path = tmp_path / "pour_audit_fixture.kicad_pcb"
    pcb_path.write_text(_FIXTURE_TEMPLATE.format(via_layers=via_layers))
    return pcb_path


class TestViaLayerSpanAwarePourAudit:
    """``_audit_pour_nets`` must not assume a via bridges every copper layer."""

    def test_blind_via_does_not_falsely_bridge_a_far_layer_pour(
        self, generate_design_mod, tmp_path: Path
    ) -> None:
        """A blind F.Cu/In1.Cu via must NOT fuse an F.Cu pad to a B.Cu pour.

        R1.1 (F.Cu) sits only on a via declared ``(layers "F.Cu" "In1.Cu")``
        -- it never physically reaches B.Cu, so it cannot bond to the B.Cu
        GND pour R2.1 sits directly under.  Before issue #5176's fix, every
        via was treated as spanning all 4 copper layers regardless of its
        declared span, so this fixture would falsely report
        ``connected: True`` (2 pads fused via a layer the via never
        touches) -- the modelling gap this test guards against.
        """
        pytest.importorskip("shapely")
        pcb_path = _fixture(tmp_path, '"F.Cu" "In1.Cu"')

        audit = generate_design_mod._audit_pour_nets(pcb_path, ["GND"])
        info = audit["GND"]

        assert info["connected"] is False, (
            f"blind via falsely bridged R1.1 to the B.Cu pour: pad_groups={info['pad_groups']}"
        )
        stranded_names = {name for name, _ in info["stranded_pads"]}
        assert "R1.1" in stranded_names

    def test_through_via_still_bridges_pour(self, generate_design_mod, tmp_path: Path) -> None:
        """Sanity: a genuine through via (F.Cu/B.Cu) still bonds the pad.

        Same layout as above but the via's declared span is the board's two
        outer layers, which -- expanded across the physical stack -- covers
        every copper layer including B.Cu.  The fix must not regress this
        legitimate case.
        """
        pytest.importorskip("shapely")
        pcb_path = _fixture(tmp_path, '"F.Cu" "B.Cu"')

        audit = generate_design_mod._audit_pour_nets(pcb_path, ["GND"])
        info = audit["GND"]

        assert info["connected"] is True, (
            f"through via unexpectedly failed to bridge the pour: pad_groups={info['pad_groups']}"
        )

    def test_unparseable_via_span_fails_safe_not_all_layers(
        self, generate_design_mod, tmp_path: Path
    ) -> None:
        """A via whose ``(layers ...)`` can't be parsed must bridge NOTHING.

        A degenerate via declaring only one layer name (``(layers "F.Cu")``)
        does not match ``_via_layer_span``'s two-endpoint regex, so it falls
        back to the "can't resolve the span" path. The fallback must assume
        the via bridges no copper (a false disconnect, which fails the audit
        safe) rather than ``all_layers`` (a false ``POUR CONNECTIVITY: PASS``
        -- the exact defect class issue #5176 fixes).
        """
        pytest.importorskip("shapely")
        pcb_path = _fixture(tmp_path, '"F.Cu"')

        audit = generate_design_mod._audit_pour_nets(pcb_path, ["GND"])
        info = audit["GND"]

        assert info["connected"] is False, (
            f"unparseable via span falsely bridged R1.1 to the B.Cu pour: "
            f"pad_groups={info['pad_groups']}"
        )
        stranded_names = {name for name, _ in info["stranded_pads"]}
        assert "R1.1" in stranded_names


@pytest.mark.parametrize("blocked", [False, True])
def test_pour_repair_finds_narrow_j1_escape(generate_design_mod, tmp_path, blocked):
    """Board06 J1.B8 needs a ray between the eight compass directions.

    The fixture retains J1 and nearby tracks/vias from issue5223's routed
    snapshot, with a simple B.Cu anchor replacing the full board's pour.
    The original search leaves B8 stranded. A foreign F.Cu track across
    the remaining corridor must still prevent repair.
    """
    text = (REPO_ROOT / "tests/fixtures/board06_pour_escape.kicad_pcb").read_text()
    if blocked:
        text = (
            text.rstrip()[:-1]
            + """
  (segment (start 111.5 57.9) (end 112.6 57.9) (width 0.2)
    (layer "F.Cu") (net 0))
)
"""
        )
    pcb = tmp_path / "escape.kicad_pcb"
    pcb.write_text(text)
    before = generate_design_mod._audit_pour_nets(pcb, ["+3V3"])["+3V3"]
    assert ("J1.B8", False) in before["stranded_pads"]

    generate_design_mod._repair_pour_connectivity(pcb, ["+3V3"])

    after = generate_design_mod._audit_pour_nets(pcb, ["+3V3"])["+3V3"]
    if blocked:
        assert ("J1.B8", False) in after["stranded_pads"]
        assert not after["connected"]
    else:
        assert after["connected"]
        assert not after["stranded_pads"]
        assert "(via (at 112.325 58.063)" in pcb.read_text()

        # Check the emitted witness against physical foreign trace copper,
        # independently of the repair's candidate selection and audit.
        import re

        from shapely.geometry import LineString, Point

        emitted = pcb.read_text()
        net_id = re.search(r'\(net (\d+) "\+3V3"\)', emitted).group(1)
        vias = generate_design_mod._find_sexp_blocks(emitted, "\n  (via")
        added_via = next(v for v in vias if f"(net {net_id})" in v and "(at 112.325 58.063)" in v)
        center = tuple(map(float, re.search(r"\(at ([\d.-]+) ([\d.-]+)\)", added_via).groups()))
        diameter = float(re.search(r"\(size ([\d.]+)\)", added_via).group(1))
        via = Point(center).buffer(diameter / 2)
        segments = generate_design_mod._find_sexp_blocks(emitted, "\n\t(segment")
        segments += generate_design_mod._find_sexp_blocks(emitted, "\n  (segment")
        added_stub = next(
            s for s in segments if f"(net {net_id})" in s and "(start 112.000 57.500)" in s
        )
        stub_start = tuple(
            map(float, re.search(r"\(start ([\d.-]+) ([\d.-]+)\)", added_stub).groups())
        )
        stub_end = tuple(map(float, re.search(r"\(end ([\d.-]+) ([\d.-]+)\)", added_stub).groups()))
        stub_width = float(re.search(r"\(width ([\d.]+)\)", added_stub).group(1))
        assert stub_start == (112, 57.5)
        assert stub_end == center
        assert '(layer "F.Cu")' in added_stub
        stub = LineString([stub_start, stub_end]).buffer(stub_width / 2)
        checked = 0
        for segment in segments:
            if re.search(r"\(net (\d+)\)", segment).group(1) == net_id:
                continue
            start = tuple(map(float, re.search(r"\(start ([\d.-]+) ([\d.-]+)\)", segment).groups()))
            end = tuple(map(float, re.search(r"\(end ([\d.-]+) ([\d.-]+)\)", segment).groups()))
            width = float(re.search(r"\(width ([\d.]+)\)", segment).group(1))
            copper = LineString([start, end]).buffer(width / 2)
            assert via.distance(copper) >= 0.15
            if '(layer "F.Cu")' in segment:
                assert stub.distance(copper) >= 0.15
            checked += 1
        assert checked >= 40
