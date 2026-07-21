"""Tests for the net-reference dialect detector used by the route write path.

Issue #4416: ``kct route`` (and its checkpoint / interrupt save paths) must
re-emit route-added copper in the SAME net-reference dialect the input board
used, so a KiCad-10 name-only board (``(net "SDA")``) does not flip to numeric
``(net 12)`` and ping-pong with kicad-cli's ``--save-board`` name-only rewrite.

The detector (:func:`_board_uses_name_only_dialect`) keys off inline name-only
references and must NOT be fooled by the header ``(net N "name")`` table, which
every board (numeric or name-only) carries.
"""

from __future__ import annotations

from kicad_tools.cli.route_cmd import _board_uses_name_only_dialect

_NAME_ONLY_BOARD = """\
(kicad_pcb
  (version 20260206)
  (generator "pcbnew")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (net 0 "")
  (net 1 "GND")
  (net 2 "VCC")
  (segment (start 0 0) (end 1 0) (width 0.25) (layer "F.Cu") (net "GND") (uuid "s1"))
)
"""

_NUMERIC_BOARD = """\
(kicad_pcb
  (version 20260206)
  (generator "pcbnew")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (net 0 "")
  (net 1 "GND")
  (net 2 "VCC")
  (segment (start 0 0) (end 1 0) (width 0.25) (layer "F.Cu") (net 1) (uuid "s1"))
)
"""

_NUMERIC_FULL_FORM_BOARD = """\
(kicad_pcb
  (version 20260206)
  (generator "pcbnew")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (net 0 "")
  (net 1 "GND")
  (footprint "FP" (layer "F.Cu") (uuid "fp")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "GND"))
  )
)
"""


def test_name_only_board_detected(tmp_path):
    p = tmp_path / "name.kicad_pcb"
    p.write_text(_NAME_ONLY_BOARD)
    assert _board_uses_name_only_dialect(p) is True


def test_numeric_board_not_detected(tmp_path):
    p = tmp_path / "numeric.kicad_pcb"
    p.write_text(_NUMERIC_BOARD)
    assert _board_uses_name_only_dialect(p) is False


def test_numeric_full_form_board_not_detected(tmp_path):
    """A (net N "name") full-form board -- and its header table -- is numeric.

    The header ``(net 1 "GND")`` must not be mistaken for a name-only ref.
    """
    p = tmp_path / "full.kicad_pcb"
    p.write_text(_NUMERIC_FULL_FORM_BOARD)
    assert _board_uses_name_only_dialect(p) is False


def test_missing_file_defaults_to_numeric(tmp_path):
    """An unreadable path falls back to numeric (today's behavior)."""
    assert _board_uses_name_only_dialect(tmp_path / "nope.kicad_pcb") is False
