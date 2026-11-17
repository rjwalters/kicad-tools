"""Issue #8051: a synthesized pour that reaches none of its pads must not strand the net.

``auto_pour_if_missing`` runs before ``auto_skip_pour_nets``; the latter skips
any pour-classified net that has a zone.  If the synthesized zone's outline
contains none of the net's pads, the net would be skipped by the router yet
carry no copper.  The zone is withdrawn (with a warning) so the router routes
the net as traces, the same handling as the #5864 unsatisfiable-pour case.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.router.auto_pour import auto_pour_if_missing, auto_skip_pour_nets
from kicad_tools.schema.pcb import PCB
from tests.test_auto_pour_unsatisfiable_5864 import (
    _HEADER,
    _authored_zone,
    _footprint,
)

_HEADER_4L = _HEADER.replace(
    '(0 "F.Cu" signal)\n    (31 "B.Cu" signal)',
    '(0 "F.Cu" signal)\n    (1 "In1.Cu" signal)\n    (2 "In2.Cu" signal)\n    (31 "B.Cu" signal)',
).replace(
    '(net 1 "GND")\n  (net 2 "AGND")\n  (net 3 "+5V")\n  (net 4 "SIG1")\n  (net 5 "SIG2")',
    '(net 1 "GNDD")\n  (net 2 "GNDA")\n  (net 3 "+5V")\n  (net 4 "+3.3V")\n  (net 5 "+3.3VA")\n'
    '  (net 6 "SIG1")\n  (net 7 "SIG2")',
)


@pytest.fixture
def board(tmp_path: Path) -> Path:
    rows = [
        _footprint("U1", 10, 10, [("1", 1, "GNDD")]),
        _footprint("U2", 50, 30, [("1", 1, "GNDD")]),
        _footprint("U3", 30, 10, [("1", 2, "GNDA")]),
        _footprint("U4", 10, 30, [("1", 3, "+5V")]),
        _footprint("U5", 50, 10, [("1", 3, "+5V")]),
        _footprint("U6", 20, 20, [("1", 4, "+3.3V")]),
        _footprint("U7", 40, 20, [("1", 5, "+3.3VA")]),
        _footprint("U8", 30, 30, [("1", 6, "SIG1")]),
        _footprint("U9", 35, 35, [("1", 7, "SIG2")]),
        _footprint("U10", 5, 5, [("1", 6, "SIG1")]),
        _footprint("U11", 55, 35, [("1", 7, "SIG2")]),
    ]
    zones = [
        _authored_zone("z1", 1, "GNDD", "In1.Cu", 1),
        _authored_zone("z2", 2, "GNDA", "In2.Cu", 1),
        _authored_zone("z3", 4, "+3.3V", "F.Cu", 1),
        _authored_zone("z4", 5, "+3.3VA", "B.Cu", 1),
    ]
    path = tmp_path / "four_layer.kicad_pcb"
    path.write_text(_HEADER_4L + "\n".join(rows) + "\n" + "\n".join(zones) + "\n)\n")
    return path


def test_zone_covering_pads_is_kept(board: Path) -> None:
    count, names = auto_pour_if_missing(board, quiet=True)
    assert names == ["+5V"]
    assert count == 1
    assert [z for z in PCB.load(str(board)).zones if z.net_name == "+5V"]


def test_zone_reaching_no_pads_is_withdrawn(
    board: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    import kicad_tools.zones.generator as gen

    real = gen._compute_pour_outlines

    def far_away(*args, **kwargs):
        outlines = real(*args, **kwargs)
        outlines["+5V"] = [(55.0, 36.0), (58.0, 36.0), (58.0, 38.0), (55.0, 38.0)]
        return outlines

    monkeypatch.setattr(gen, "_compute_pour_outlines", far_away)

    count, names = auto_pour_if_missing(board)

    out = capsys.readouterr().out
    assert "skipped pour for '+5V'" in out
    assert "+5V" not in names
    assert count == len(names) == 0
    assert not [z for z in PCB.load(str(board)).zones if z.net_name == "+5V"]

    # The router is handed the net as a signal instead of skipping it.
    skipped, no_zone = auto_skip_pour_nets(board, [], quiet=True)
    assert "+5V" not in skipped
    assert "+5V" in no_zone
