"""Round-trip tests for ``Footprint`` ``(attr ...)`` block fields.

Regression coverage for issue #2827: ``PCB.save()`` previously dropped
Python-side mutations to ``Footprint.attr``, ``locked``, ``dnp``,
``exclude_from_pos_files``, and ``exclude_from_bom``. The fix extends
``Footprint.__setattr__`` to rebuild the ``(attr ...)`` child of
``_sexp_node`` whenever any of those fields change, while preserving
unknown tokens (``board_only``, ``allow_missing_courtyard``,
``allow_soldermask_bridges``, ...) that the parser does not yet model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.schema import PCB

# Minimal PCB fixture with a single footprint that has NO existing
# (attr ...) block. Tests that need a pre-populated attr block use the
# helper ``_inject_attr_tokens`` to splice tokens into the raw text
# before reload.
_BASE_PCB = """(kicad_pcb
\t(version 20240108)
\t(generator "pytest")
\t(generator_version "8.0")
\t(general (thickness 1.6) (legacy_teardrops no))
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(31 "B.Cu" signal)
\t\t(37 "F.SilkS" user "F.Silkscreen")
\t\t(44 "Edge.Cuts" user)
\t\t(49 "F.Fab" user)
\t)
\t(setup (pad_to_mask_clearance 0))
\t(net 0 "")
\t(net 1 "GND")
\t(gr_rect (start 100 100) (end 150 150)
\t\t(stroke (width 0.1) (type default))
\t\t(fill none)
\t\t(layer "Edge.Cuts")
\t)
\t(footprint "Resistor_SMD:R_0402_1005Metric"
\t\t(layer "F.Cu")
\t\t(uuid "00000000-0000-0000-0000-000000000010")
\t\t(at 125 125)
\t\t(property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS")
\t\t\t(effects (font (size 1.0 1.0) (thickness 0.15)))
\t\t\t(uuid "00000000-0000-0000-0000-000000000011"))
\t\t(property "Value" "10k" (at 0 1.5 0) (layer "F.Fab")
\t\t\t(uuid "00000000-0000-0000-0000-000000000012"))
\t\t(pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
\t\t\t(layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(roundrect_rratio 0.25)
\t\t\t(net 1 "GND"))
\t\t(pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
\t\t\t(layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(roundrect_rratio 0.25)
\t\t\t(net 1 "GND"))
\t)
)
"""


def _write_base_pcb(tmp_path: Path) -> Path:
    path = tmp_path / "attr_roundtrip.kicad_pcb"
    path.write_text(_BASE_PCB)
    return path


def _write_pcb_with_attr(tmp_path: Path, attr_inner: str) -> Path:
    """Write a copy of _BASE_PCB with a hand-crafted ``(attr ...)`` block.

    ``attr_inner`` is the literal token string between ``(attr`` and ``)``,
    e.g. ``"smd locked"`` or ``"smd board_only allow_missing_courtyard"``.
    The block is inserted immediately after the footprint's ``(at ...)``
    line.
    """
    needle = '\t\t(at 125 125)\n'
    replacement = needle + f'\t\t(attr {attr_inner})\n'
    text = _BASE_PCB.replace(needle, replacement, 1)
    path = tmp_path / "attr_roundtrip_seeded.kicad_pcb"
    path.write_text(text)
    return path


def _file_text(path: Path) -> str:
    return path.read_text()


# ---------------------------------------------------------------------------
# Round-trip behaviour for each modeled flag
# ---------------------------------------------------------------------------


def test_locked_flag_round_trips(tmp_path: Path):
    """Setting ``fp.locked = True`` survives a save/load cycle."""
    src = _write_base_pcb(tmp_path)

    pcb = PCB.load(str(src))
    fp = pcb.get_footprint("R1")
    assert fp is not None
    assert fp.locked is False

    fp.locked = True

    out = tmp_path / "out.kicad_pcb"
    pcb.save(out)

    text = _file_text(out)
    assert "locked" in text, "locked token missing from saved PCB"

    reloaded = PCB.load(str(out))
    fp2 = reloaded.get_footprint("R1")
    assert fp2 is not None
    assert fp2.locked is True


def test_locked_flag_can_be_cleared(tmp_path: Path):
    """Setting ``fp.locked = False`` removes the ``(locked)`` token."""
    src = _write_pcb_with_attr(tmp_path, "smd locked")

    pcb = PCB.load(str(src))
    fp = pcb.get_footprint("R1")
    assert fp is not None
    assert fp.locked is True
    assert fp.attr == "smd"

    fp.locked = False

    out = tmp_path / "out.kicad_pcb"
    pcb.save(out)

    # Reload and confirm the flag is gone -- the source-of-truth check.
    reloaded = PCB.load(str(out))
    fp2 = reloaded.get_footprint("R1")
    assert fp2 is not None
    assert fp2.locked is False
    # And that the smd type token survived.
    assert fp2.attr == "smd"


def test_dnp_flag_round_trips(tmp_path: Path):
    """``dnp`` flag survives mutation + save + reload."""
    src = _write_base_pcb(tmp_path)

    pcb = PCB.load(str(src))
    fp = pcb.get_footprint("R1")
    assert fp is not None
    assert fp.dnp is False

    fp.dnp = True

    out = tmp_path / "out.kicad_pcb"
    pcb.save(out)

    reloaded = PCB.load(str(out))
    fp2 = reloaded.get_footprint("R1")
    assert fp2 is not None
    assert fp2.dnp is True


def test_exclude_from_pos_files_round_trips(tmp_path: Path):
    """``exclude_from_pos_files`` flag survives mutation + save + reload."""
    src = _write_base_pcb(tmp_path)

    pcb = PCB.load(str(src))
    fp = pcb.get_footprint("R1")
    assert fp is not None
    assert fp.exclude_from_pos_files is False

    fp.exclude_from_pos_files = True

    out = tmp_path / "out.kicad_pcb"
    pcb.save(out)

    reloaded = PCB.load(str(out))
    fp2 = reloaded.get_footprint("R1")
    assert fp2 is not None
    assert fp2.exclude_from_pos_files is True


def test_exclude_from_bom_round_trips(tmp_path: Path):
    """``exclude_from_bom`` flag survives mutation + save + reload."""
    src = _write_base_pcb(tmp_path)

    pcb = PCB.load(str(src))
    fp = pcb.get_footprint("R1")
    assert fp is not None
    assert fp.exclude_from_bom is False

    fp.exclude_from_bom = True

    out = tmp_path / "out.kicad_pcb"
    pcb.save(out)

    reloaded = PCB.load(str(out))
    fp2 = reloaded.get_footprint("R1")
    assert fp2 is not None
    assert fp2.exclude_from_bom is True


def test_attr_type_round_trips(tmp_path: Path):
    """Changing ``attr`` (smd <-> through_hole) survives round-trip.

    Also exercises the "no prior (attr ...) block" -> "block now exists"
    transition by starting from a footprint with no attr block.
    """
    src = _write_base_pcb(tmp_path)

    pcb = PCB.load(str(src))
    fp = pcb.get_footprint("R1")
    assert fp is not None
    assert fp.attr == ""

    fp.attr = "smd"

    out = tmp_path / "out_smd.kicad_pcb"
    pcb.save(out)

    reloaded = PCB.load(str(out))
    fp2 = reloaded.get_footprint("R1")
    assert fp2 is not None
    assert fp2.attr == "smd"

    # Now flip to through_hole.
    fp2.attr = "through_hole"
    out2 = tmp_path / "out_th.kicad_pcb"
    reloaded.save(out2)

    reloaded2 = PCB.load(str(out2))
    fp3 = reloaded2.get_footprint("R1")
    assert fp3 is not None
    assert fp3.attr == "through_hole"


# ---------------------------------------------------------------------------
# Unknown-token preservation
# ---------------------------------------------------------------------------


def test_attr_unknown_tokens_preserved(tmp_path: Path):
    """Unknown ``(attr ...)`` tokens (e.g. ``board_only``) survive round-trip.

    The parser does not currently model ``board_only``,
    ``allow_missing_courtyard``, or ``allow_soldermask_bridges``.
    The fix captures these into a private ``_attr_unknown_tokens``
    list at parse time and re-emits them whenever the ``(attr ...)``
    block is rebuilt — otherwise mutating any other attr field would
    silently strip them.
    """
    src = _write_pcb_with_attr(
        tmp_path, "smd board_only allow_missing_courtyard"
    )

    pcb = PCB.load(str(src))
    fp = pcb.get_footprint("R1")
    assert fp is not None
    assert fp.attr == "smd"
    assert fp.locked is False
    # Unknown tokens are captured verbatim.
    assert fp._attr_unknown_tokens == [
        "board_only",
        "allow_missing_courtyard",
    ]

    # Mutate a modeled flag -- this triggers a rebuild of (attr ...).
    fp.locked = True

    out = tmp_path / "out.kicad_pcb"
    pcb.save(out)

    text = _file_text(out)
    assert "board_only" in text, "unknown token board_only was dropped"
    assert "allow_missing_courtyard" in text, (
        "unknown token allow_missing_courtyard was dropped"
    )
    assert "locked" in text, "newly-set locked token did not appear"

    # Round-trip back through the parser to confirm tokens are still
    # captured and the modeled flag survives.
    reloaded = PCB.load(str(out))
    fp2 = reloaded.get_footprint("R1")
    assert fp2 is not None
    assert fp2.attr == "smd"
    assert fp2.locked is True
    assert "board_only" in fp2._attr_unknown_tokens
    assert "allow_missing_courtyard" in fp2._attr_unknown_tokens


# ---------------------------------------------------------------------------
# Combinations and edge cases
# ---------------------------------------------------------------------------


def test_multi_flag_combinations(tmp_path: Path):
    """Setting several flags at once survives round-trip."""
    src = _write_base_pcb(tmp_path)

    pcb = PCB.load(str(src))
    fp = pcb.get_footprint("R1")
    assert fp is not None

    fp.attr = "smd"
    fp.locked = True
    fp.dnp = True
    fp.exclude_from_pos_files = True
    fp.exclude_from_bom = True

    out = tmp_path / "out.kicad_pcb"
    pcb.save(out)

    reloaded = PCB.load(str(out))
    fp2 = reloaded.get_footprint("R1")
    assert fp2 is not None
    assert fp2.attr == "smd"
    assert fp2.locked is True
    assert fp2.dnp is True
    assert fp2.exclude_from_pos_files is True
    assert fp2.exclude_from_bom is True


def test_no_attr_block_when_all_default(tmp_path: Path):
    """Clearing every flag removes the ``(attr ...)`` block entirely.

    Acceptance criteria documents either "no block" or "valid empty
    block KiCad accepts" as acceptable. We pick the no-block form
    because that is what KiCad emits when no flags are set, and it
    minimises diff churn against pristine boards.
    """
    src = _write_pcb_with_attr(tmp_path, "smd locked dnp")

    pcb = PCB.load(str(src))
    fp = pcb.get_footprint("R1")
    assert fp is not None
    assert fp.attr == "smd"
    assert fp.locked is True
    assert fp.dnp is True

    # Clear everything.
    fp.attr = ""
    fp.locked = False
    fp.dnp = False
    fp.exclude_from_pos_files = False
    fp.exclude_from_bom = False
    # Also clear any unknown tokens for the all-default form.
    fp._attr_unknown_tokens = []

    out = tmp_path / "out.kicad_pcb"
    pcb.save(out)

    text = _file_text(out)
    # No `(attr` substring should remain anywhere in the footprint
    # block. (We use a precise pattern to avoid matching e.g.
    # ``(attr ...)`` text accidentally appearing in property values.)
    # The base PCB has no other (attr substrings, so a blanket check
    # is safe here.
    assert "(attr" not in text, (
        "(attr ...) block should be omitted when no flags set"
    )

    reloaded = PCB.load(str(out))
    fp2 = reloaded.get_footprint("R1")
    assert fp2 is not None
    assert fp2.attr == ""
    assert fp2.locked is False
    assert fp2.dnp is False
    assert fp2.exclude_from_pos_files is False
    assert fp2.exclude_from_bom is False


# ---------------------------------------------------------------------------
# Sanity: existing position/rotation/layer round-trips still work
# ---------------------------------------------------------------------------


def test_attr_sync_does_not_break_position_roundtrip(tmp_path: Path):
    """Regression guard: attr sync must not interfere with position sync."""
    src = _write_base_pcb(tmp_path)

    pcb = PCB.load(str(src))
    fp = pcb.get_footprint("R1")
    assert fp is not None

    # Mutate position AND attr in the same flow.
    new_x = fp.position[0] + 10.0
    new_y = fp.position[1] - 5.0
    fp.position = (new_x, new_y)
    fp.locked = True

    out = tmp_path / "out.kicad_pcb"
    pcb.save(out)

    reloaded = PCB.load(str(out))
    fp2 = reloaded.get_footprint("R1")
    assert fp2 is not None
    assert fp2.position[0] == pytest.approx(new_x)
    assert fp2.position[1] == pytest.approx(new_y)
    assert fp2.locked is True
