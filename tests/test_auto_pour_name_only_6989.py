"""Issue #6989: the pour pre-route stage handles KiCad-10 name-only boards.

A KiCad-10 board can use ``(net "NAME")`` on every pad, segment, via and
zone, with no ``(net N "NAME")`` header table at all.  ``auto_skip_pour_nets``
and ``auto_pour_if_missing`` used to read nets only from the numbered form,
so on such a board they found no nets and returned early: zoned pour nets were
not auto-skipped, pour nets without a zone were not reported, no pours were
created, and ``kct route`` skipped the plane-via reservation (#6401).  The
post-fill net-format check also called the board corrupt.

Each test runs a numbered board and its name-only twin and requires the same
result from both.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.test_auto_pour import _make_pcb

_NETS = [(1, "GND"), (2, "+3V3"), (3, "+5V"), (4, "SDA"), (5, "SCL")]


def _zone(net_id: int, name: str) -> str:
    return (
        f'(zone (net {net_id}) (net_name "{name}") (layer "B.Cu") (hatch edge 0.5) '
        "(polygon (pts (xy 0 0) (xy 50 0) (xy 50 50) (xy 0 50))))"
    )


def _to_name_only(text: str) -> str:
    """Rewrite a numbered board in the KiCad-10 name-only dialect."""
    # Drop the header net table (KiCad 10 writes none).
    text = re.sub(r'^  \(net \d+ "[^"]*"\)\n', "", text, flags=re.M)
    # Zones: ``(net N) (net_name "X")`` -> ``(net "X")``.
    text = re.sub(r'\(net \d+\) \(net_name ("[^"]*")\)', r"(net \1)", text)
    # Pads: ``(net N "X")`` -> ``(net "X")``.
    text = re.sub(r'\(net \d+ ("[^"]*")\)', r"(net \1)", text)
    assert not re.search(r"\(net \d", text)
    return text


def _twins(tmp_path: Path, zones: list[str] | None = None) -> tuple[Path, Path]:
    numbered = _make_pcb(net_defs=_NETS, pad_nets=_NETS + [(4, "SDA"), (5, "SCL")], zones=zones)
    num_path = tmp_path / "numbered.kicad_pcb"
    name_path = tmp_path / "named.kicad_pcb"
    num_path.write_text(numbered)
    name_path.write_text(_to_name_only(numbered))
    return num_path, name_path


def _zone_nets(pcb_path: Path) -> list[str]:
    from kicad_tools.schema.pcb import PCB

    return sorted(z.net_name for z in PCB.load(str(pcb_path)).zones)


def test_board_net_names_resolves_name_only_board(tmp_path: Path):
    from kicad_tools.router.auto_pour import board_net_names

    num_path, name_path = _twins(tmp_path)
    assert board_net_names(num_path.read_text()) == dict(_NETS)
    assert sorted(board_net_names(name_path.read_text()).values()) == sorted(n for _, n in _NETS)


def test_board_net_names_ignores_net_inside_a_string():
    """A ``(net N "X")`` inside a quoted string is not a net (quote-aware scan)."""
    from kicad_tools.router.auto_pour import board_net_names

    text = (
        "(kicad_pcb\n"
        '  (footprint "X" (descr "see (net 9 \\"FAKE\\")")\n'
        '    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "GND")))\n'
        ")\n"
    )
    assert list(board_net_names(text).values()) == ["GND"]


@pytest.mark.parametrize("dialect", ["numbered", "named"])
def test_auto_skip_pour_nets_name_only_matches_numbered(tmp_path: Path, dialect: str, capsys):
    from kicad_tools.router.auto_pour import auto_skip_pour_nets

    num_path, name_path = _twins(tmp_path, zones=[_zone(1, "GND"), _zone(2, "+3V3")])
    pcb_path = num_path if dialect == "numbered" else name_path

    skip: list[str] = []
    auto_skipped, no_zone = auto_skip_pour_nets(pcb_path, skip)

    assert sorted(auto_skipped) == ["+3V3", "GND"]
    assert sorted(skip) == ["+3V3", "GND"]
    assert no_zone == ["+5V"]
    out = capsys.readouterr().out
    assert "Auto-skip: +3V3, GND (pour nets" in out
    assert "Routing: +5V (power nets without zones)" in out


@pytest.mark.parametrize("dialect", ["numbered", "named"])
def test_auto_pour_if_missing_name_only_matches_numbered(tmp_path: Path, dialect: str):
    from kicad_tools.router.auto_pour import auto_pour_if_missing

    num_path, name_path = _twins(tmp_path)
    pcb_path = num_path if dialect == "numbered" else name_path

    count, names = auto_pour_if_missing(pcb_path, quiet=True)

    assert count == 3
    assert sorted(names) == ["+3V3", "+5V", "GND"]
    assert _zone_nets(pcb_path) == ["+3V3", "+5V", "GND"]
    if dialect == "named":
        # The new pours follow the board's dialect: no numeric ids appear.
        assert not re.search(r"\(net \d", pcb_path.read_text())


def test_auto_pour_if_missing_name_only_is_idempotent(tmp_path: Path):
    """Existing name-only zones are seen, so no duplicate pours are made."""
    from kicad_tools.router.auto_pour import auto_pour_if_missing

    _num_path, name_path = _twins(tmp_path, zones=[_zone(1, "GND"), _zone(2, "+3V3")])
    count, names = auto_pour_if_missing(name_path, quiet=True)
    assert (count, names) == (1, ["+5V"])


class TestNetFormatCheckOnNameOnlyBoards:
    """``validate_net_format`` does not call a name-only board corrupt."""

    def test_name_only_board_is_valid(self, tmp_path: Path):
        from kicad_tools.cli.runner import validate_net_format

        pcb = tmp_path / "named.kicad_pcb"
        pcb.write_text(
            "(kicad_pcb (version 20250000) (generator test)\n"
            '  (footprint "R" (property "Reference" "R1")\n'
            '    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "GND")))\n'
            '  (segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net "GND"))\n'
            '  (via (at 1 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "GND"))\n'
            ")\n"
        )
        report = validate_net_format(pcb)
        assert report.valid is True
        assert report.name_only_dialect is True
        assert report.total_corrupt == 0

    def test_name_only_reference_on_numbered_board_still_flagged(self, tmp_path: Path):
        from kicad_tools.cli.runner import validate_net_format

        pcb = tmp_path / "mixed.kicad_pcb"
        pcb.write_text(
            '(kicad_pcb (version 20240108) (generator test)\n  (net 0 "")\n  (net 1 "GND")\n'
            '  (segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net "GND"))\n'
            ")\n"
        )
        report = validate_net_format(pcb)
        assert report.valid is False
        assert report.name_only_dialect is False
        assert report.name_only_segments == 1

    def test_empty_net_on_name_only_board_still_flagged(self, tmp_path: Path):
        from kicad_tools.cli.runner import validate_net_format

        pcb = tmp_path / "named_empty.kicad_pcb"
        pcb.write_text(
            "(kicad_pcb (version 20250000) (generator test)\n"
            '  (segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net "GND"))\n'
            '  (segment (start 1 0) (end 2 0) (width 0.2) (layer "F.Cu") (net ""))\n'
            ")\n"
        )
        report = validate_net_format(pcb)
        assert report.valid is False
        assert report.name_only_segments == 0
        assert report.empty_net_segments == 1
