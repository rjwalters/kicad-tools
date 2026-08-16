"""Issue #4875: legacy top-level ``(net_class …)`` parsing in ``schema.pcb``.

Pre-KiCad-6 boards declare their design rules (clearance, track width, via
sizes) in the ``.kicad_pcb`` itself, as top-level ``(net_class …)`` nodes that
are **siblings** of ``(setup …)`` -- not children of it, so ``_parse_setup``
never saw them and nothing in this repo parsed them at all before #4875.

``kct route`` gates its board-derived clearance on
:attr:`PCB.net_classes` being non-empty, so these tests pin both halves of
that signal: legacy boards parse, and modern boards (including everything
this repo's own writer emits) stay empty.
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.schema import PCB, BoardNetClass

_LEGACY_HEADER = """(kicad_pcb (version 20171130) (host pcbnew 5.1.9)
  (general (thickness 1.6))
  (page A4)
  (layers (0 F.Cu signal) (31 B.Cu signal) (44 Edge.Cuts user))
  (setup (pad_to_mask_clearance 0.05) (aux_axis_origin 0 0))
  (net 0 "")
  (net 1 GND)
"""

_LEGACY_FOOTER = """  (gr_line (start 0 0) (end 50 0) (layer Edge.Cuts) (width 0.1))
)
"""


def _write_board(tmp_path: Path, body: str, name: str = "legacy.kicad_pcb") -> Path:
    pcb = tmp_path / name
    pcb.write_text(_LEGACY_HEADER + body + _LEGACY_FOOTER)
    return pcb


def test_legacy_default_net_class_is_parsed(tmp_path):
    """Every declared dimension lands on the dataclass."""
    pcb_path = _write_board(
        tmp_path,
        """  (net_class Default "This is the default net class."
    (clearance 0.2)
    (trace_width 0.25)
    (via_dia 0.8)
    (via_drill 0.4)
    (uvia_dia 0.3)
    (uvia_drill 0.1)
    (diff_pair_width 0.2)
    (diff_pair_gap 0.25)
    (add_net GND)
    (add_net /SIG)
  )
""",
    )

    net_classes = PCB.load(pcb_path).net_classes
    assert list(net_classes) == ["Default"]

    default = net_classes["Default"]
    assert isinstance(default, BoardNetClass)
    assert default.name == "Default"
    assert default.description == "This is the default net class."
    assert default.clearance == 0.2
    assert default.trace_width == 0.25
    assert default.via_dia == 0.8
    assert default.via_drill == 0.4
    assert default.uvia_dia == 0.3
    assert default.uvia_drill == 0.1
    assert default.diff_pair_width == 0.2
    assert default.diff_pair_gap == 0.25
    assert default.nets == ["GND", "/SIG"]


def test_multiple_net_classes_are_keyed_by_name(tmp_path):
    """A board may declare several classes; all are surfaced."""
    pcb_path = _write_board(
        tmp_path,
        """  (net_class Default "" (clearance 0.2) (trace_width 0.25))
  (net_class "Power" "wide rails" (clearance 0.3) (trace_width 0.8) (add_net VBUS))
""",
    )

    net_classes = PCB.load(pcb_path).net_classes
    assert set(net_classes) == {"Default", "Power"}
    assert net_classes["Power"].clearance == 0.3
    assert net_classes["Power"].trace_width == 0.8
    assert net_classes["Power"].nets == ["VBUS"]
    # Absent tokens stay None so "not declared" is distinguishable from 0.
    assert net_classes["Default"].via_dia is None
    assert net_classes["Default"].description == ""


def test_absent_net_class_block_yields_empty_mapping(tmp_path):
    """A modern-format board (rules live in the .kicad_pro) declares nothing.

    This is the pilot-board case from the corpus benchmark work: a stripped
    single-file import whose ``(setup …)`` carries no rule tokens at all.
    """
    pcb_path = _write_board(tmp_path, "")
    assert PCB.load(pcb_path).net_classes == {}


def test_written_board_never_emits_net_class(tmp_path):
    """This repo's own writer emits no ``net_class`` -- the fleet's guard.

    ``kct route``'s board-derived clearance path is gated on a non-empty
    ``net_classes``, so a writer that cannot produce the token is what makes
    the demo-board fleet structurally immune to the #4875 behavior change.
    """
    pcb = PCB.create(width=50.0, height=40.0)
    out = tmp_path / "written.kicad_pcb"
    pcb.save(out)

    assert "(net_class" not in out.read_text()
    assert PCB.load(out).net_classes == {}


def test_net_class_survives_reparse_without_duplicating(tmp_path):
    """``_parse`` appends; the name-keyed mapping must not accumulate."""
    pcb_path = _write_board(
        tmp_path,
        '  (net_class Default "" (clearance 0.2))\n',
    )
    pcb = PCB.load(pcb_path)
    assert len(pcb.net_classes) == 1

    pcb._parse()  # idempotent re-parse (resize() takes this path)
    assert len(pcb.net_classes) == 1
    assert pcb.net_classes["Default"].clearance == 0.2
