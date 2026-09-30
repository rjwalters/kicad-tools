"""Mask-to-copper source-mutation guard: both sides share one canonicalization.

Issue #5814.  ``PCB.load()`` synthesizes the top-level ``(net N "name")`` table
for a KiCad 10 name-only-net board, so comparing the loaded object against a
*plain* re-parse of the saved bytes always differed -- and an untouched board
was rejected with ``PCB object differs from current source bytes`` before native
mask geometry inspection ever ran.  The guard now canonicalizes the saved bytes
through the same ``PCB`` construction, so only genuine edits differ.

Follow-up to #5063 and #5137 / PR #5255, which introduced the guard: these tests
pin the invariant it protects (a loaded-but-unsaved edit, or a source changed
underneath a loaded object, is never analyzed as if it were the saved board)
alongside the false-positive fix.
"""

import hashlib

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string
from kicad_tools.validate import DRCChecker
from kicad_tools.validate.checker import canonical_source_text
from kicad_tools.validate.mask_copper import (
    MaskCopperAssessment,
    MaskCopperPolicy,
    MaskCopperRequest,
    MaskSourceBinding,
)

SOURCE_GUARD_REASON = "PCB object differs from current source bytes"

# The issue's fixture: KiCad 10 name-only nets -- every inline reference is
# ``(net "NAME")`` and there is no top-level ``(net N "name")`` table at all,
# which is what ``kicad-cli pcb drc --save-board`` writes back.
NAME_ONLY_NET_BOARD = """(kicad_pcb (version 20260115) (generator "pcbnew")
      (general (thickness 1.6)) (paper "A4")
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal)
        (44 "Edge.Cuts" user)) (setup (pad_to_mask_clearance 0))
      (gr_rect (start 100 70) (end 190 160)
        (stroke (width 0.05) (type default)) (fill none)
        (layer "Edge.Cuts") (uuid "e4af335a-9b4d-5c17-90e3-7320110fa3bc"))
      (footprint
\t"NetTie-2_SMD_Pad0.5mm"
\t(layer "F.Cu")
\t(uuid "80f92f63-7d6e-418b-978d-20000011cde0")
\t(at 140.852007 85.488825 -90)
\t(descr "Net tie, 2 pin, 0.5mm square SMD pads")
\t(tags "net tie")
\t(property "Reference" "NT3"
\t\t(at 0 -1.2 0)
\t\t(layer "F.SilkS")
\t\t(hide yes)
\t\t(uuid "ed86465d-16b2-4087-88d6-25aa25885dfd")
\t\t(effects
\t\t\t(font
\t\t\t\t(size 1 1)
\t\t\t\t(thickness 0.15)
\t\t\t)
\t\t)
\t)
\t(property "Value" "BOOT0_TIE"
\t\t(at 0 1.2 0)
\t\t(layer "F.Fab")
\t\t(hide yes)
\t\t(uuid "55decc6c-ccba-49a9-9c58-e53b55d22ed2")
\t\t(effects
\t\t\t(font
\t\t\t\t(size 1 1)
\t\t\t\t(thickness 0.15)
\t\t\t)
\t\t)
\t)
\t(attr exclude_from_pos_files exclude_from_bom allow_missing_courtyard)
\t(net_tie_pad_groups "1, 2")
\t(duplicate_pad_numbers_are_jumpers no)
\t(fp_poly (pts (xy -0.5 -0.25) (xy 0.5 -0.25) (xy 0.5 0.25) (xy -0.5 0.25))
\t\t(stroke
\t\t\t(width 0)
\t\t\t(type solid)
\t\t)
\t\t(fill yes)
\t\t(layer "F.Cu")
\t\t(uuid "4c587801-35de-4fc4-895b-9942aca7f89f")
\t)
\t(pad "1" smd circle
\t\t(at -0.5 0 270)
\t\t(size 0.5 0.5)
\t\t(layers "F.Cu")
\t\t(net "SWCLK")
\t\t(uuid "1745e8e7-cb05-4e0c-be57-60ff31465fb9")
\t)
\t(pad "2" smd circle
\t\t(at 0.5 0 270)
\t\t(size 0.5 0.5)
\t\t(layers "F.Cu")
\t\t(net "BOOT0")
\t\t(uuid "b531e0f4-5d47-4a72-8a54-a3eaafb85b86")
\t)
\t(embedded_fonts no)
))
"""

# Traditional numbered nets: a full header table and numeric inline references.
# ``PCB.load()`` synthesizes nothing here, so the guard behaved correctly before
# the fix -- these cases exist to prove it still does.
NUMBERED_NET_BOARD = """(kicad_pcb (version 20260115) (generator "pcbnew")
  (general (thickness 1.6)) (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SWCLK")
  (net 2 "BOOT0")
  (gr_rect (start 100 70) (end 190 160)
    (stroke (width 0.05) (type default)) (fill none)
    (layer "Edge.Cuts") (uuid "e4af335a-9b4d-5c17-90e3-7320110fa3bc"))
  (footprint "R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "80f92f63-7d6e-418b-978d-200000220000")
    (at 140 100 0)
    (property "Reference" "R1" (at 0 -1.2 0) (layer "F.SilkS")
      (uuid "ed86465d-16b2-4087-88d6-25aa25880001")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "SWCLK")
      (uuid "1745e8e7-cb05-4e0c-be57-60ff31460001"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 2 "BOOT0")
      (uuid "1745e8e7-cb05-4e0c-be57-60ff31460002")))
  (segment (start 140.51 100) (end 145 100) (width 0.25) (layer "F.Cu") (net 2)
    (uuid "c0ffee00-0000-4000-8000-000000000001"))
)
"""

# Mixed dialect: a surviving header table *and* a name-only inline pad
# reference, the shape a partially-rewritten board has.
MIXED_NET_BOARD = """(kicad_pcb (version 20260115) (generator "pcbnew")
  (general (thickness 1.6)) (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 2 "BOOT0")
  (gr_rect (start 100 70) (end 190 160)
    (stroke (width 0.05) (type default)) (fill none)
    (layer "Edge.Cuts") (uuid "e4af335a-9b4d-5c17-90e3-7320110fa3bd"))
  (footprint "R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "80f92f63-7d6e-418b-978d-200000330000")
    (at 140 100 0)
    (property "Reference" "R2" (at 0 -1.2 0) (layer "F.SilkS")
      (uuid "ed86465d-16b2-4087-88d6-25aa25880002")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net "SWCLK")
      (uuid "1745e8e7-cb05-4e0c-be57-60ff31460003"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 2 "BOOT0")
      (uuid "1745e8e7-cb05-4e0c-be57-60ff31460004")))
  (segment (start 140.51 100) (end 145 100) (width 0.25) (layer "F.Cu") (net "BOOT0")
    (uuid "c0ffee00-0000-4000-8000-000000000002"))
)
"""

BOARDS = {
    "name_only": NAME_ONLY_NET_BOARD,
    "numbered": NUMBERED_NET_BOARD,
    "mixed": MIXED_NET_BOARD,
}

POLICY = MaskCopperPolicy(0.1, "fab order", "process", "revision")


def write_board(tmp_path, text, name="board.kicad_pcb"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def checker_for(path, *, policy=POLICY):
    """A checker over a freshly loaded board with an explicit mask policy."""
    return DRCChecker(PCB.load(path), mask_copper_request=MaskCopperRequest(policy=policy))


def install_native_spy(monkeypatch):
    """Replace the native mask check with a recorder returning a bound pass.

    Reaching this stand-in is the observable signal that the source guard let
    the board through to mask geometry inspection.
    """
    from kicad_tools.validate import mask_copper

    calls = []

    def spy(source, policy=None, intents=(), **native_options):
        calls.append(source)
        return MaskCopperAssessment(
            coverage="complete",
            policy=policy,
            binding=MaskSourceBinding(
                hashlib.sha256(source.read_bytes()).hexdigest(), None, None, "a" * 64
            ),
        )

    monkeypatch.setattr(mask_copper, "check_mask_to_copper", spy)
    return calls


# ---------------------------------------------------------------------------
# The asymmetry itself, and the canonicalization that removes it
# ---------------------------------------------------------------------------


def test_name_only_net_board_is_normalized_in_memory_without_touching_the_file(tmp_path):
    """The issue's repro: 0 declarations on disk, 3 in the loaded object."""
    path = write_board(tmp_path, NAME_ONLY_NET_BOARD)
    before = path.read_bytes()

    pcb = PCB.load(str(path))

    assert len(parse_string(before.decode()).find_children("net")) == 0
    assert len(pcb._sexp.find_children("net")) == 3
    # Loading is read-only: the normalization lives only in the object.
    assert path.read_bytes() == before


@pytest.mark.parametrize("dialect", sorted(BOARDS))
def test_canonical_source_text_matches_a_freshly_loaded_object(tmp_path, dialect):
    """The issue's expression-level control, exercised directly."""
    path = write_board(tmp_path, BOARDS[dialect])
    before = path.read_bytes()
    pcb = PCB.load(str(path))

    assert pcb._sexp.to_string() == canonical_source_text(before, path)
    # ...and the plain re-parse the guard used to compare against does NOT
    # match for the name-only dialect, which is the whole defect.
    plain = parse_string(before.decode()).to_string()
    assert (pcb._sexp.to_string() == plain) is (dialect != "name_only")


@pytest.mark.parametrize("dialect", sorted(BOARDS))
def test_canonical_source_text_still_exposes_a_real_edit(tmp_path, dialect):
    path = write_board(tmp_path, BOARDS[dialect])
    before = path.read_bytes()
    edited = PCB(parse_string(before.decode()), path=path)
    edited._sexp.find_child("general").find_child("thickness").set_atom(0, 1.5)

    assert edited._sexp.to_string() != canonical_source_text(before, path)


@pytest.mark.parametrize("payload", [b"(kicad_pcb (general", b"\xff\xfe not text"])
def test_unparseable_source_bytes_fail_closed(payload):
    """An unreadable source must raise, never canonicalize to "unchanged"."""
    with pytest.raises(ValueError):
        canonical_source_text(payload)


# ---------------------------------------------------------------------------
# Public checker path: the false positive is gone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dialect", sorted(BOARDS))
def test_freshly_loaded_board_reaches_mask_geometry_inspection(tmp_path, monkeypatch, dialect):
    path = write_board(tmp_path, BOARDS[dialect])
    calls = install_native_spy(monkeypatch)

    result = checker_for(path).check_mask_to_copper()

    assert calls == [path], "source guard blocked an untouched board"
    assessment = result.mask_copper_assessments[0]
    assert assessment.coverage == "complete"
    assert assessment.reasons == []
    assert result.passed


def test_name_only_net_board_without_policy_stops_at_the_policy_gate(tmp_path):
    """Without a spy the real native entry point is reached and gates on policy.

    Pre-fix this reported the source guard's reason instead, proving the board
    never got as far as the mask check.
    """
    path = write_board(tmp_path, NAME_ONLY_NET_BOARD)
    result = DRCChecker(PCB.load(str(path))).check_mask_to_copper()

    assessment = result.mask_copper_assessments[0]
    assert not result.passed
    assert assessment.coverage == "not_run"
    assert SOURCE_GUARD_REASON not in assessment.reasons
    assert "explicit process-specific" in assessment.reasons[0]


# ---------------------------------------------------------------------------
# ...and the guard still rejects everything it is meant to reject
# ---------------------------------------------------------------------------


def edit_board_thickness(pcb):
    pcb._sexp.find_child("general").find_child("thickness").set_atom(0, 1.5)


def edit_mask_clearance(pcb):
    pcb._sexp.find_child("setup").find_child("pad_to_mask_clearance").set_atom(0, 0.075)


def edit_pad_position(pcb):
    pcb.footprints[0].pads[0].position = (1.5, 2.5)


def edit_net_declaration(pcb):
    pcb._sexp.find_children("net")[-1].set_atom(1, "RENAMED")


def edit_add_copper(pcb):
    pcb.add_trace((120.0, 90.0), (125.0, 90.0), 0.25, "F.Cu", net="BOOT0")


IN_MEMORY_EDITS = {
    "board": edit_board_thickness,
    "mask": edit_mask_clearance,
    "pad": edit_pad_position,
    "net": edit_net_declaration,
    "copper": edit_add_copper,
}


@pytest.mark.parametrize("dialect", sorted(BOARDS))
@pytest.mark.parametrize("edit", sorted(IN_MEMORY_EDITS))
def test_unsaved_in_memory_edit_is_still_rejected(tmp_path, monkeypatch, dialect, edit):
    path = write_board(tmp_path, BOARDS[dialect])
    checker = checker_for(path)
    calls = install_native_spy(monkeypatch)

    IN_MEMORY_EDITS[edit](checker.pcb)
    result = checker.check_mask_to_copper()

    assert calls == [], "edited-but-unsaved board reached native mask analysis"
    assessment = result.mask_copper_assessments[0]
    assert not result.passed
    assert assessment.coverage == "incomplete"
    assert assessment.reasons == [SOURCE_GUARD_REASON]


@pytest.mark.parametrize("edit", sorted(IN_MEMORY_EDITS))
def test_saving_the_edit_clears_the_guard(tmp_path, monkeypatch, edit):
    path = write_board(tmp_path, NAME_ONLY_NET_BOARD)
    checker = checker_for(path)
    calls = install_native_spy(monkeypatch)

    IN_MEMORY_EDITS[edit](checker.pcb)
    assert checker.check_mask_to_copper().mask_copper_assessments[0].reasons == [
        SOURCE_GUARD_REASON
    ]

    checker.pcb.save()
    result = checker.check_mask_to_copper()

    assert calls == [path]
    assert result.passed


@pytest.mark.parametrize("dialect", sorted(BOARDS))
def test_source_changed_on_disk_after_load_is_rejected(tmp_path, monkeypatch, dialect):
    path = write_board(tmp_path, BOARDS[dialect])
    checker = checker_for(path)
    calls = install_native_spy(monkeypatch)

    path.write_text(BOARDS[dialect].replace("(thickness 1.6)", "(thickness 0.8)"))
    result = checker.check_mask_to_copper()

    assert calls == []
    assert result.mask_copper_assessments[0].reasons == [SOURCE_GUARD_REASON]


def test_unreadable_source_after_load_is_typed_incomplete(tmp_path):
    path = write_board(tmp_path, NAME_ONLY_NET_BOARD)
    checker = checker_for(path)

    path.write_bytes(b"(kicad_pcb (general")
    result = checker.check_mask_to_copper()

    assessment = result.mask_copper_assessments[0]
    assert not result.passed
    assert assessment.coverage == "incomplete"
    assert assessment.reasons


def test_hash_bound_provenance_still_catches_a_swap_during_native_capture(tmp_path, monkeypatch):
    """The sha256 binding check survives the canonicalization change."""
    from kicad_tools.validate import mask_copper

    path = write_board(tmp_path, NAME_ONLY_NET_BOARD)
    checker = checker_for(path)

    def replace_source_during_check(source, policy=None, intents=(), **native_options):
        source.write_bytes(source.read_bytes() + b"\n")
        return MaskCopperAssessment(
            coverage="complete",
            policy=policy,
            binding=MaskSourceBinding(
                hashlib.sha256(source.read_bytes()).hexdigest(), None, None, "a" * 64
            ),
        )

    monkeypatch.setattr(mask_copper, "check_mask_to_copper", replace_source_during_check)
    result = checker.check_mask_to_copper()

    assessment = result.mask_copper_assessments[0]
    assert not result.passed
    assert assessment.coverage == "incomplete"
    assert "Source changed after checker object validation" in assessment.reasons
