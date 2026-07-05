"""KiCad CLI round-trip smoke tests.

Closes a regression class where kicad-tools' PCB writer produces files that
``kicad-cli pcb drc`` rejects with "Failed to load board" (exit 3). Two such
bugs shipped to ``main`` recently and broke every demo board's manufacturing
export until they were diagnosed by hand-bisecting the emitted files (see
fix in commit ``30256d40``):

1. ``generator_version`` was emitted unquoted (``(generator_version 9.0)``)
   because the SExp serializer's ``_needs_quoting`` returned False for
   strings that parse as numeric. Fixed via
   ``_FORCE_QUOTED_STRING_VALUE = {"generator_version"}`` in
   ``src/kicad_tools/sexp/parser.py``.

2. ``SilkscreenGenerator`` embedded a ``(kct_marking "kct:name")`` subfield
   inside ``gr_text`` for re-run idempotency. KiCad's parser rejects
   unknown subfields inside ``gr_text``. Fixed in
   ``src/kicad_tools/silkscreen/generator.py``.

The test in this file builds an in-suite PCB spec that exercises both
regression vectors plus a footprint, a net, and a zone. It then calls
``PCB.save()`` and round-trips the output through ``kicad-cli pcb drc``,
asserting the file *loads* (DRC violations are not the concern here —
only that kicad-cli accepts the syntax).

If the load fails, the assertion message names the producing kicad-tools
component, prints the verbatim kicad-cli stderr, and points at the emitted
file so the failure is debuggable from the CI log alone.

When ``find_kicad_cli()`` returns ``None`` the entire module is skipped
(matching the existing convention at
``tests/test_pcb_import_from_schematic.py:296``).
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from kicad_tools.cli.runner import find_kicad_cli, run_drc
from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp.builders import gr_text_node, sheet_instances, zone_node
from kicad_tools.sexp.parser import parse_string
from kicad_tools.silkscreen.generator import SilkscreenGenerator

pytestmark = pytest.mark.skipif(find_kicad_cli() is None, reason="kicad-cli not installed")

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_load_failure(
    producer: str,
    pcb_path: Path,
    return_code: int,
    stderr: str,
) -> str:
    """Build a clear, attributed error message for a kicad-cli load failure.

    Per the issue's acceptance criteria, the message must:
    - Name the producing kicad-tools component.
    - Include the PCB path so the file can be inspected.
    - Print the verbatim kicad-cli stderr (this was the critical signal that
      made the recent regression fix tractable).
    """
    return (
        "kicad-cli rejected PCB emitted by kicad-tools writer.\n"
        f"  Producer: {producer}\n"
        f"  PCB: {pcb_path}\n"
        f"  kicad-cli return code: {return_code}\n"
        "  kicad-cli stderr:\n"
        f"    {stderr.strip() or '<empty>'}\n"
        "\n"
        f"Inspect the emitted file: head -20 {pcb_path}"
    )


def _assert_kicad_cli_loads(
    pcb_path: Path,
    producer: str,
    tmp_path: Path,
) -> None:
    """Assert that kicad-cli successfully loads the given PCB.

    Uses ``run_drc`` as the load primitive — kicad-cli only writes a DRC
    report when the file loads, so report-presence is the load signal.
    Exit code 3 specifically indicates "Failed to load board".

    DRC violations themselves are NOT a failure for this test — we only
    care that the file is well-formed enough for kicad-cli to parse it.
    """
    output_path = tmp_path / f"{pcb_path.stem}_drc.rpt"
    result = run_drc(
        pcb_path,
        output_path=output_path,
        format="report",
        # Disable schematic parity — we have no schematic for this synthetic
        # PCB, and the load-check doesn't need it.
        schematic_parity=False,
    )

    # ``run_drc`` returns success=True iff the report was produced, which is
    # exactly the load-success condition.
    assert result.success, _format_load_failure(
        producer, pcb_path, result.return_code, result.stderr
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPCBSaveRoundtrip:
    """``PCB.create()`` + ``PCB.save()`` must produce a kicad-cli-loadable file.

    Targets the ``generator_version`` quoting regression: ``PCB.create()``
    writes ``(generator_version "10.0")`` via the library writer, and
    ``PCB.save()`` round-trips through the SExp serializer.
    """

    def test_blank_pcb_roundtrip(self, tmp_path: Path) -> None:
        """A minimal blank PCB from ``PCB.create()`` must load via kicad-cli."""
        pcb = PCB.create(
            width=50.0,
            height=50.0,
            title="kicad-cli roundtrip blank",
            revision="A",
        )
        pcb_path = tmp_path / "blank.kicad_pcb"
        pcb.save(pcb_path)

        # Sanity: the writer must emit the field that triggered the regression
        # in quoted form. If this fails, the rest of the test is meaningless.
        contents = pcb_path.read_text()
        assert '(generator_version "10.0")' in contents, (
            'Regression guard: PCB.save() must emit (generator_version "10.0") '
            "with the value quoted. Bare numeric form is rejected by kicad-cli "
            "with 'Failed to load board' (exit 3). See "
            "src/kicad_tools/sexp/parser.py:_FORCE_QUOTED_STRING_VALUE."
        )

        _assert_kicad_cli_loads(pcb_path, producer="PCB.save (blank)", tmp_path=tmp_path)

    def test_gr_line_outline_and_version_roundtrip(self, tmp_path: Path) -> None:
        """Issue #3805: gr_line outline + 20241229 version must load in KiCad 10.

        Guards against the future version stamp (20260206) and the gr_rect
        outline that previously needed manual fixups before opening in KiCad.
        """
        pcb = PCB.create(width=65.0, height=56.0, title="issue-3805 roundtrip")
        pcb_path = tmp_path / "issue3805.kicad_pcb"
        pcb.save(pcb_path)

        contents = pcb_path.read_text()
        assert "(version 20241229)" in contents
        assert "20260206" not in contents
        assert "gr_rect" not in contents
        assert contents.count("(gr_line") == 4

        _assert_kicad_cli_loads(
            pcb_path, producer="PCB.save (issue-3805 outline)", tmp_path=tmp_path
        )

    def test_pcb_with_silkscreen_text_roundtrip(self, tmp_path: Path) -> None:
        """A PCB with ``gr_text`` silkscreen markings must load via kicad-cli.

        Targets the ``kct_marking`` subfield regression — KiCad's parser
        rejects unknown subfields inside ``gr_text``. Exercises the same
        ``gr_text_node`` builder used by ``SilkscreenGenerator``.
        """
        pcb = PCB.create(width=60.0, height=40.0, title="silkscreen roundtrip")
        # Two silkscreen texts — one a project-name-style marking, one a
        # date-style marking. Mirrors what SilkscreenGenerator.add_board_markings
        # produces.
        pcb._sexp.append(gr_text_node("kicad-tools v0.1", 5.0, 5.0, uuid_str="rt-name"))
        pcb._sexp.append(gr_text_node("2026-05-04", 5.0, 7.0, uuid_str="rt-date"))

        pcb_path = tmp_path / "silkscreen.kicad_pcb"
        pcb.save(pcb_path)

        # Regression guard: the previous bug embedded (kct_marking ...) inside
        # gr_text. If the writer ever reintroduces it, the file will fail to
        # load. We assert the field is absent before invoking kicad-cli so the
        # test fails with a clear, attributed message even on systems where
        # kicad-cli's stderr is unhelpful.
        contents = pcb_path.read_text()
        assert "kct_marking" not in contents, (
            "Regression guard: emitted PCB must not contain (kct_marking ...) "
            "subfields inside gr_text. KiCad's parser rejects them with "
            "'Failed to load board'. See src/kicad_tools/silkscreen/generator.py."
        )

        _assert_kicad_cli_loads(pcb_path, producer="PCB.save + gr_text_node", tmp_path=tmp_path)

    def test_pcb_with_net_and_zone_roundtrip(self, tmp_path: Path) -> None:
        """A PCB with a named net + a copper zone must load via kicad-cli.

        Exercises ``PCB.add_net()`` and the ``zone_node`` builder in addition
        to the basic save path, broadening writer coverage beyond the two
        recent regression vectors.
        """
        pcb = PCB.create(width=80.0, height=60.0, title="net+zone roundtrip")
        gnd = pcb.add_net("GND")

        # A modest GND zone covering most of the board on F.Cu.
        pcb._sexp.append(
            zone_node(
                net=gnd.number,
                net_name="GND",
                layer="F.Cu",
                points=[(10.0, 10.0), (70.0, 10.0), (70.0, 50.0), (10.0, 50.0)],
                uuid_str="rt-zone",
            )
        )

        pcb_path = tmp_path / "zone.kicad_pcb"
        pcb.save(pcb_path)

        _assert_kicad_cli_loads(pcb_path, producer="PCB.save + zone_node", tmp_path=tmp_path)

    def test_full_writer_surface_roundtrip(self, tmp_path: Path) -> None:
        """A PCB exercising silkscreen + net + zone simultaneously.

        This is the closest in-suite analog to a real demo-board emit. If any
        single writer path regresses, this test will fire alongside the more
        targeted ones above, but with a more diagnostic name.
        """
        pcb = PCB.create(width=100.0, height=80.0, title="full surface roundtrip")
        gnd = pcb.add_net("GND")
        pcb.add_net("VCC")

        pcb._sexp.append(
            gr_text_node("kicad-tools full Rev A", 10.0, 10.0, uuid_str="rt-full-name")
        )
        pcb._sexp.append(gr_text_node("2026-05-04", 10.0, 12.0, uuid_str="rt-full-date"))
        pcb._sexp.append(
            zone_node(
                net=gnd.number,
                net_name="GND",
                layer="B.Cu",
                points=[(15.0, 15.0), (85.0, 15.0), (85.0, 65.0), (15.0, 65.0)],
                uuid_str="rt-full-zone",
            )
        )

        pcb_path = tmp_path / "full_surface.kicad_pcb"
        pcb.save(pcb_path)

        _assert_kicad_cli_loads(
            pcb_path, producer="PCB.save (full writer surface)", tmp_path=tmp_path
        )


class TestRotatedFootprintPadGeometry:
    """Rotated footprints must NOT produce phantom pad-overlap DRC violations.

    Issue #3902: the writer used to emit pad ``(at x y ANGLE)`` as the LOCAL
    angle. In KiCad the angle is ABSOLUTE (footprint rotation folded in), so
    elongated pads (e.g. 1.475 x 0.4 mm) rendered unrotated across the pin row,
    overlapping neighbours and generating phantom ``shorting_items`` /
    ``solder_mask_bridge`` violations on a footprints-only board.

    This is the manufacturing-correctness gate: with the writer emitting
    absolute angles, a footprints-only board with rotated elongated-pad parts
    is clean of the phantom-overlap violation classes.
    """

    FIXTURES_DIR = Path(__file__).resolve().parents[0] / "fixtures"
    TEST_PRETTY_DIR = FIXTURES_DIR / "Test_Library.pretty"

    # Violation classes that a footprints-only board of well-separated parts
    # must never emit. Phantom pad geometry manifests as these.
    _PHANTOM_CLASSES = {"shorting_items", "solder_mask_bridge"}

    def test_rotated_footprints_no_phantom_overlaps(self, tmp_path: Path) -> None:
        """kicad-cli DRC reports zero phantom overlaps for rotated footprints."""
        import json

        pcb = PCB.create(width=60.0, height=40.0, title="issue-3902 rotated pads")

        # Two elongated-pad footprints, well separated, each placed at a
        # cardinal rotation. Before the fix, each pad's shape rendered
        # unrotated and overlapped its neighbour along the pin row.
        pcb.add_footprint_from_file(
            kicad_mod_path=self.TEST_PRETTY_DIR / "RotatedPad_Test.kicad_mod",
            reference="U1",
            x=15.0,
            y=20.0,
            rotation=90.0,
        )
        pcb.add_footprint_from_file(
            kicad_mod_path=self.TEST_PRETTY_DIR / "RotatedPad_Test.kicad_mod",
            reference="U2",
            x=45.0,
            y=20.0,
            rotation=-90.0,
        )

        pcb_path = tmp_path / "rotated_pads.kicad_pcb"
        pcb.save(pcb_path)

        # Sanity: the writer must have folded fp rotation into the pad angles.
        text = pcb_path.read_text()
        assert "120" in text, (
            "Expected an absolute pad angle (local 30 + fp 90 = 120) in the "
            f"emitted board; writer may have regressed to LOCAL angles.\n{text}"
        )

        # First: the board must LOAD (guards against unrelated syntax breaks).
        _assert_kicad_cli_loads(
            pcb_path, producer="PCB.save (rotated footprints)", tmp_path=tmp_path
        )

        # Then: inspect the JSON DRC report for phantom-overlap classes.
        report = tmp_path / "rotated_pads_drc.json"
        result = run_drc(
            pcb_path,
            output_path=report,
            format="json",
            schematic_parity=False,
        )
        assert result.success, (
            "kicad-cli DRC did not produce a report for the rotated-footprint "
            f"board.\n  return code: {result.return_code}\n  stderr: {result.stderr}"
        )

        data = json.loads(report.read_text())
        violations = data.get("violations", [])
        phantom = [v for v in violations if v.get("type") in self._PHANTOM_CLASSES]
        assert not phantom, (
            "Rotated footprints produced phantom pad-overlap DRC violations "
            "(issue #3902 regression). The writer must emit ABSOLUTE pad "
            f"angles.\n  Offending violations: {phantom}\n"
            f"  Inspect: {pcb_path}"
        )


class TestSilkscreenGeneratorRoundtrip:
    """``SilkscreenGenerator.add_board_markings()`` must emit loadable files.

    This is the direct regression gate for the ``kct_marking`` bug — the
    fix lives in ``src/kicad_tools/silkscreen/generator.py``, so the test
    that drives it must call into ``SilkscreenGenerator``, not just the
    underlying ``gr_text_node`` builder.
    """

    def _minimal_pcb_with_outline(self, tmp_path: Path) -> Path:
        """Write a minimal-but-valid PCB containing an Edge.Cuts outline.

        ``SilkscreenGenerator._get_marking_position`` reads the Edge.Cuts
        outline to position markings; without one it falls back to a
        default. Including the outline exercises a slightly larger surface.
        """
        pcb_text = textwrap.dedent("""\
            (kicad_pcb
                (version 20240108)
                (generator "kicad_tools")
                (generator_version "9.0")
                (general
                    (thickness 1.6)
                    (legacy_teardrops no)
                )
                (paper "A4")
                (layers
                    (0 "F.Cu" signal)
                    (31 "B.Cu" signal)
                    (36 "B.SilkS" user "B.Silkscreen")
                    (37 "F.SilkS" user "F.Silkscreen")
                    (44 "Edge.Cuts" user)
                )
                (setup)
                (net 0 "")
                (gr_line (start 10 10) (end 60 10) (stroke (width 0.05) (type solid)) (layer "Edge.Cuts") (uuid "edge-1"))
                (gr_line (start 60 10) (end 60 50) (stroke (width 0.05) (type solid)) (layer "Edge.Cuts") (uuid "edge-2"))
                (gr_line (start 60 50) (end 10 50) (stroke (width 0.05) (type solid)) (layer "Edge.Cuts") (uuid "edge-3"))
                (gr_line (start 10 50) (end 10 10) (stroke (width 0.05) (type solid)) (layer "Edge.Cuts") (uuid "edge-4"))
            )
        """)
        # Validate the input fixture itself parses (catches typos in the test).
        parse_string(pcb_text)
        path = tmp_path / "silk_input.kicad_pcb"
        path.write_text(pcb_text)
        return path

    def test_silkscreen_generator_add_markings_roundtrip(self, tmp_path: Path) -> None:
        """``add_board_markings()`` output must load via kicad-cli."""
        input_path = self._minimal_pcb_with_outline(tmp_path)

        gen = SilkscreenGenerator(input_path)
        result = gen.add_board_markings(
            name="kicad-tools roundtrip",
            revision="A",
            date="2026-05-04",
        )
        # At least one marking should have been added — sanity check that the
        # generator did its job (otherwise the round-trip is meaningless).
        assert result.markings_added >= 1, (
            f"Expected SilkscreenGenerator to add markings, got "
            f"{result.markings_added}. Messages: {result.messages}"
        )

        out_path = tmp_path / "silk_output.kicad_pcb"
        gen.save(out_path)

        # Regression guard: the bug we are gating against was the embedding
        # of (kct_marking "kct:name") subfields inside gr_text. If the
        # generator ever reintroduces them, this assertion fires with a
        # clear attribution before kicad-cli is even invoked.
        contents = out_path.read_text()
        assert "kct_marking" not in contents, (
            "Regression guard: SilkscreenGenerator must not emit "
            "(kct_marking ...) subfields. KiCad rejects them with "
            "'Failed to load board'. Fix is in "
            "src/kicad_tools/silkscreen/generator.py."
        )

        _assert_kicad_cli_loads(
            out_path, producer="SilkscreenGenerator.add_board_markings", tmp_path=tmp_path
        )


class TestFootprintWithGrTextRoundtrip:
    """A PCB containing a footprint with ``fp_text`` properties must load.

    The recent regressions touched the silkscreen-adjacent code path; a
    footprint-with-text spec exercises a third writer surface (footprint
    serialization, including its own text effects).
    """

    def test_footprint_with_silkscreen_property_roundtrip(self, tmp_path: Path) -> None:
        """A footprint carrying an ``fp_text`` reference must round-trip."""
        # Build a PCB with one inline footprint. We construct the SExp
        # directly rather than going through ``add_footprint`` because the
        # latter requires KiCad's footprint libraries to be installed, which
        # is not the regression class under test.
        pcb_text = textwrap.dedent("""\
            (kicad_pcb
                (version 20240108)
                (generator "kicad_tools")
                (generator_version "9.0")
                (general
                    (thickness 1.6)
                    (legacy_teardrops no)
                )
                (paper "A4")
                (layers
                    (0 "F.Cu" signal)
                    (31 "B.Cu" signal)
                    (36 "B.SilkS" user "B.Silkscreen")
                    (37 "F.SilkS" user "F.Silkscreen")
                    (44 "Edge.Cuts" user)
                )
                (setup)
                (net 0 "")
                (footprint "Test:R_0805"
                    (layer "F.Cu")
                    (uuid "fp-uuid-1")
                    (at 30 30)
                    (fp_text reference "R1" (at 0 -2) (layer "F.SilkS")
                        (uuid "fp-text-1")
                        (effects (font (size 1 1) (thickness 0.15)))
                    )
                    (fp_text value "10k" (at 0 2) (layer "F.Fab")
                        (uuid "fp-text-2")
                        (effects (font (size 1 1) (thickness 0.15)))
                    )
                )
            )
        """)
        # Round-trip through our parser/serializer to make sure the writer
        # path is exercised (this is what catches generator_version-style
        # bugs in the serializer).
        doc = parse_string(pcb_text)
        path = tmp_path / "fp.kicad_pcb"
        path.write_text(doc.to_string())

        _assert_kicad_cli_loads(
            path, producer="parse_string + SExp.to_string (footprint)", tmp_path=tmp_path
        )


class TestLoadFailureMessage:
    """Verify that load-failures produce clear, attributed error messages.

    Constructs a deliberately broken PCB (unquoted ``generator_version``,
    matching the recent regression) and confirms that ``run_drc`` reports
    the failure in a way that surfaces the offending kicad-cli stderr.
    The goal is to guarantee that, when the gate fires in CI, maintainers
    can diagnose the failure from the CI log without re-running locally.
    """

    def test_unquoted_generator_version_reproduces_failure(self, tmp_path: Path) -> None:
        """An unquoted ``generator_version`` must trip kicad-cli's loader.

        This is the canary test: if it ever stops failing, kicad-cli has
        changed its parser strictness and the regression class has shifted
        — a signal to revisit ``_FORCE_QUOTED_STRING_VALUE``.
        """
        broken = textwrap.dedent("""\
            (kicad_pcb
                (version 20240108)
                (generator "kicad_tools")
                (generator_version 9.0)
                (general
                    (thickness 1.6)
                    (legacy_teardrops no)
                )
                (paper "A4")
                (layers
                    (0 "F.Cu" signal)
                    (31 "B.Cu" signal)
                    (44 "Edge.Cuts" user)
                )
                (setup)
                (net 0 "")
            )
        """)
        broken_path = tmp_path / "broken.kicad_pcb"
        broken_path.write_text(broken)

        output_path = tmp_path / "broken_drc.rpt"
        result = run_drc(
            broken_path,
            output_path=output_path,
            format="report",
            schematic_parity=False,
        )

        # The file should fail to load (canary test). If this assertion ever
        # fails — i.e., kicad-cli accepts the unquoted form — the regression
        # class has changed and the test infrastructure should be revisited.
        assert not result.success, (
            "Canary failed: kicad-cli accepted an unquoted generator_version. "
            "The regression class this test gates against has shifted; "
            "review src/kicad_tools/sexp/parser.py:_FORCE_QUOTED_STRING_VALUE "
            "and decide whether the force-quote set is still needed."
        )

        # And the failure message must contain enough context to debug.
        message = _format_load_failure(
            "PCB.save (canary)",
            broken_path,
            result.return_code,
            result.stderr,
        )
        assert str(broken_path) in message
        assert "kicad-cli return code:" in message
        assert "Producer: PCB.save (canary)" in message


# ---------------------------------------------------------------------------
# Locked-footprint save form (issue #3457)
# ---------------------------------------------------------------------------
#
# KiCad 10's kicad-cli rejects boards whose footprints carry the legacy
# KiCad-6 in-attr ``locked`` token (``(attr smd locked)``) with "Failed to
# load board" (exit 3). ``Footprint._sync_attr_node`` previously re-emitted
# that token whenever ``fp.locked`` was set and the board was saved through
# the schema layer (``kct pcb lock-footprints``, optimize-placement flows),
# silently breaking zone fill / DRC / gerber export downstream. The save
# path now emits a top-level ``(locked yes)`` child instead, and a legacy
# file is migrated on load -> save. These tests gate that contract against
# the real kicad-cli parser.


def _locked_fp_pcb_text(footprint_extra: str) -> str:
    """Minimal kicad-cli-loadable PCB with one footprint + ``footprint_extra``."""
    return textwrap.dedent(f"""\
        (kicad_pcb
            (version 20240108)
            (generator "kicad_tools")
            (generator_version "9.0")
            (general
                (thickness 1.6)
                (legacy_teardrops no)
            )
            (paper "A4")
            (layers
                (0 "F.Cu" signal)
                (31 "B.Cu" signal)
                (44 "Edge.Cuts" user)
            )
            (setup)
            (net 0 "")
            (footprint "Test:R_0805"
                (layer "F.Cu")
                (uuid "fp-uuid-locked-1")
                (at 30 30)
        {footprint_extra}
                (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
                (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
                (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 0 ""))
                (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 0 ""))
            )
        )
    """)


class TestLockedFootprintRoundtrip:
    """Locked footprints saved through the schema layer must load in kicad-cli."""

    def test_schema_locked_save_loads_in_kicad_cli(self, tmp_path: Path) -> None:
        """``fp.locked = True`` + ``PCB.save()`` must produce a loadable board."""
        pcb_path = tmp_path / "locked.kicad_pcb"
        pcb_path.write_text(_locked_fp_pcb_text("        (attr smd)"))

        board = PCB.load(pcb_path)
        board.get_footprint("R1").locked = True
        board.save(pcb_path)

        # Regression guard: the legacy in-attr token must be absent before
        # we even invoke kicad-cli, so the failure is attributed clearly.
        contents = pcb_path.read_text()
        assert "(attr smd locked" not in contents, (
            "Regression guard: PCB.save() re-emitted the legacy in-attr "
            "'locked' token. KiCad 10's kicad-cli rejects '(attr smd locked)' "
            "with 'Failed to load board'. See Footprint._sync_attr_node in "
            "src/kicad_tools/schema/pcb.py (issue #3457)."
        )
        assert "(locked yes)" in contents, (
            "PCB.save() dropped the lock entirely -- expected a top-level "
            "(locked yes) child on the footprint."
        )

        _assert_kicad_cli_loads(pcb_path, producer="PCB.save (locked footprint)", tmp_path=tmp_path)

    def test_legacy_locked_migration_loads_in_kicad_cli(self, tmp_path: Path) -> None:
        """A KiCad-6 legacy-form board must be migrated by load -> save."""
        pcb_path = tmp_path / "legacy_migrated.kicad_pcb"
        pcb_path.write_text(_locked_fp_pcb_text("        (attr smd locked)"))

        board = PCB.load(pcb_path)
        assert board.get_footprint("R1").locked is True
        board.save(pcb_path)

        contents = pcb_path.read_text()
        assert "(attr smd locked" not in contents, (
            "Load -> save round-trip echoed the legacy in-attr 'locked' token "
            "back via raw-sexp passthrough instead of migrating it. See "
            "PCB._link_footprint_sexp_nodes (issue #3457)."
        )
        assert "(locked yes)" in contents

        _assert_kicad_cli_loads(
            pcb_path,
            producer="PCB.load + PCB.save (legacy locked migration)",
            tmp_path=tmp_path,
        )

    def test_legacy_in_attr_locked_reproduces_failure(self, tmp_path: Path) -> None:
        """The legacy ``(attr smd locked)`` form must trip kicad-cli's loader.

        Canary test: if this ever stops failing, kicad-cli has relaxed its
        parser and the regression class this module gates against has
        shifted -- a signal to revisit the locked-form migration logic.
        """
        pcb_path = tmp_path / "legacy_raw.kicad_pcb"
        pcb_path.write_text(_locked_fp_pcb_text("        (attr smd locked)"))

        result = run_drc(
            pcb_path,
            output_path=tmp_path / "legacy_raw_drc.rpt",
            format="report",
            schematic_parity=False,
        )

        assert not result.success, (
            "Canary failed: kicad-cli accepted the legacy in-attr 'locked' "
            "token ('(attr smd locked)'). The regression class this test "
            "gates against has shifted; review the locked-form handling in "
            "src/kicad_tools/schema/pcb.py."
        )


# ---------------------------------------------------------------------------
# Schematic load gate (issue #3587)
# ---------------------------------------------------------------------------
#
# Boards 06/07 shipped .kicad_sch files whose sheet_instances block contained
# an UNQUOTED page number — ``(page 1)`` instead of ``(page "1")``. KiCad 10's
# kicad-cli rejects the bare-numeric form with "Failed to load schematic"
# (exit 3), which silently dropped schematic figures from those boards'
# manufacturing reports (#3583) and breaks any kicad-cli-based schematic
# tooling (ERC, netlist export).
#
# The generator was fixed in PR #2785 (``sheet_instances`` builder now emits
# ``SExp.quoted_atom(str(page))``), but boards 06/07 were generated one day
# before that fix landed and the stale artifacts were never re-validated.
# These tests close both gaps:
#   1. Every committed schematic in boards/*/output must load in kicad-cli,
#      so stale-artifact drift fails CI.
#   2. A canary documents the regression class (bare-numeric page rejected,
#      quoted accepted) so we notice if kicad-cli's strictness shifts.


def _committed_schematics() -> list[Path]:
    """All .kicad_sch files committed under boards/*/output."""
    return sorted(
        list(REPO_ROOT.glob("boards/*/output/*.kicad_sch"))
        + list(REPO_ROOT.glob("boards/external/*/output/*.kicad_sch"))
    )


def _run_sch_export_svg(sch_path: Path, out_dir: Path) -> subprocess.CompletedProcess:
    """Load ``sch_path`` via ``kicad-cli sch export svg``.

    SVG export is the cheapest kicad-cli operation that exercises the full
    schematic parser (exit 3 == "Failed to load schematic"). It is also the
    exact operation the #3583 report-figure pipeline performs.
    """
    kicad_cli = find_kicad_cli()
    assert kicad_cli is not None  # guarded by module-level skipif
    return subprocess.run(
        [str(kicad_cli), "sch", "export", "svg", "--output", str(out_dir), str(sch_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestCommittedSchematicsLoad:
    """Every committed boards/*/output/*.kicad_sch must load in kicad-cli."""

    @pytest.mark.parametrize(
        "sch_path",
        _committed_schematics(),
        ids=lambda p: str(p.relative_to(REPO_ROOT)),
    )
    def test_schematic_loads_in_kicad_cli(self, sch_path: Path, tmp_path: Path) -> None:
        out_dir = tmp_path / "svg"
        result = _run_sch_export_svg(sch_path, out_dir)

        assert result.returncode == 0, (
            "kicad-cli rejected a committed schematic.\n"
            f"  Schematic: {sch_path}\n"
            f"  kicad-cli return code: {result.returncode}\n"
            "  kicad-cli stderr:\n"
            f"    {result.stderr.strip() or '<empty>'}\n"
            "\n"
            "If the schematic was regenerated, the generator has drifted from\n"
            "KiCad's strict parser. Diff the sheet_instances / property blocks\n"
            "against a loading board (e.g. boards/01-voltage-divider) and fix\n"
            "the writer (src/kicad_tools/sexp/builders.py, schematic models),\n"
            "not just the committed file. See issue #3587 for the diagnosis\n"
            "workflow (delta-debugging by top-level tag group)."
        )

        svgs = list(out_dir.glob("*.svg"))
        assert svgs, (
            f"kicad-cli exited 0 but produced no SVG for {sch_path} — "
            "load-success signal is unreliable; investigate."
        )

    def test_no_schematics_glob_regression(self) -> None:
        """The glob must keep finding the demo-board schematics.

        Guards against a repo reorganisation silently emptying the
        parametrized sweep above (pytest would report 0 tests rather
        than failing).
        """
        found = {p.name for p in _committed_schematics()}
        assert {"diffpair_test.kicad_sch", "matchgroup_test.kicad_sch"} <= found, (
            f"Expected boards 06/07 schematics in sweep, found only: {sorted(found)}"
        )


class TestSheetInstancesPageQuoting:
    """Regression class for issue #3587 / PR #2785.

    KiCad 10 requires the sheet page to be a quoted string: ``(page "1")``.
    The bare-numeric form ``(page 1)`` fails the whole schematic load.
    """

    MINIMAL_SCH = textwrap.dedent("""\
        (kicad_sch
            (version 20231120)
            (generator "eeschema")
            (generator_version "9.0")
            (uuid "00000000-0000-0000-0000-000000000001")
            (paper "A4")
            (lib_symbols)
            (sheet_instances
                (path "/00000000-0000-0000-0000-000000000001" (page {page}))
            )
        )
    """)

    def test_unquoted_page_reproduces_failure(self, tmp_path: Path) -> None:
        """Canary: bare-numeric page must trip kicad-cli's loader.

        If this stops failing, kicad-cli has relaxed its parser and the
        quoted-page constraint in ``sheet_instances`` (builders.py) can be
        revisited.
        """
        sch = tmp_path / "unquoted_page.kicad_sch"
        sch.write_text(self.MINIMAL_SCH.format(page="1"))
        result = _run_sch_export_svg(sch, tmp_path / "out")
        assert result.returncode != 0, (
            "Canary failed: kicad-cli accepted a bare-numeric (page 1). "
            "The regression class this test gates against has shifted; "
            "review sheet_instances() in src/kicad_tools/sexp/builders.py."
        )

    def test_quoted_page_loads(self, tmp_path: Path) -> None:
        """Control: the quoted form must load."""
        sch = tmp_path / "quoted_page.kicad_sch"
        sch.write_text(self.MINIMAL_SCH.format(page='"1"'))
        result = _run_sch_export_svg(sch, tmp_path / "out")
        assert result.returncode == 0, (
            f"Minimal quoted-page schematic failed to load:\n{result.stderr}"
        )

    def test_sheet_instances_builder_emits_quoted_page(self) -> None:
        """Generator-level guard: the builder must serialize page quoted."""
        node = sheet_instances("/00000000-0000-0000-0000-000000000001", "1")
        text = node.to_string()
        assert '(page "1")' in text, f"sheet_instances builder emitted: {text}"


# ---------------------------------------------------------------------------
# Numeric footprint property quoting (issue #3802)
# ---------------------------------------------------------------------------
#
# create-pcb / PCBFromSchematic embed Reference/Value via
# PCB.add_footprint_from_file(). When a value parses as a float (e.g. a
# unit-less resistor value "470"), the serializer's textual heuristic used to
# emit the bare token (property "Value" 470) -- which kicad-cli rejects with
# "Failed to load board" (exit 3), silently breaking the entire schematic ->
# PCB -> manufacturing path. The fix forces Reference/Value (and the
# synthesized-property fallback) through SExp.quoted_atom() so they always
# serialize quoted. These tests gate the load contract against the real
# kicad-cli parser; the kicad-cli-free unit assertions live in
# tests/test_pcb.py:TestAddFootprintNumericPropertyQuoting.

_FIXTURE_FP = (
    REPO_ROOT / "tests" / "fixtures" / "Test_Library.pretty" / "C_0402_1005Metric.kicad_mod"
)


class TestNumericPropertyValueRoundtrip:
    """A board with a unit-less numeric footprint Value must load in kicad-cli."""

    @pytest.mark.parametrize("value", ["470", "0", "100"])
    def test_numeric_value_loads_in_kicad_cli(self, value: str, tmp_path: Path) -> None:
        """add_footprint_from_file with a numeric Value -> loadable board."""
        pcb = PCB.create(width=50.0, height=50.0, title="numeric value roundtrip")
        pcb.add_footprint_from_file(
            kicad_mod_path=_FIXTURE_FP,
            reference="R1",
            x=25.0,
            y=25.0,
            value=value,
        )
        pcb_path = tmp_path / f"numeric_{value}.kicad_pcb"
        pcb.save(pcb_path)

        # Regression guard: the value must be quoted before kicad-cli is even
        # invoked, so the failure is attributed clearly on runners where
        # kicad-cli's stderr is unhelpful.
        contents = pcb_path.read_text()
        assert f'(property "Value" "{value}"' in contents, (
            f"Regression guard: a numeric Value {value!r} must serialize as "
            f'(property "Value" "{value}") -- quoted. The bare numeric form is '
            "rejected by kicad-cli with 'Failed to load board' (exit 3). See "
            "PCB.add_footprint_from_file in src/kicad_tools/schema/pcb.py "
            "(issue #3802)."
        )
        assert f'(property "Value" {value}' not in contents

        _assert_kicad_cli_loads(
            pcb_path,
            producer="PCB.add_footprint_from_file (numeric Value)",
            tmp_path=tmp_path,
        )

    def test_numeric_reference_loads_in_kicad_cli(self, tmp_path: Path) -> None:
        """A numeric-looking Reference designator must not break the load."""
        pcb = PCB.create(width=50.0, height=50.0, title="numeric reference roundtrip")
        pcb.add_footprint_from_file(
            kicad_mod_path=_FIXTURE_FP,
            reference="1",
            x=25.0,
            y=25.0,
            value="10k",
        )
        pcb_path = tmp_path / "numeric_ref.kicad_pcb"
        pcb.save(pcb_path)

        contents = pcb_path.read_text()
        assert '(property "Reference" "1"' in contents
        assert '(property "Reference" 1' not in contents

        _assert_kicad_cli_loads(
            pcb_path,
            producer="PCB.add_footprint_from_file (numeric Reference)",
            tmp_path=tmp_path,
        )
