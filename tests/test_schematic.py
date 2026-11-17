"""Tests for schematic parsing."""

from pathlib import Path

from kicad_tools import load_schematic
from kicad_tools.schema import Schematic


def test_load_schematic(minimal_schematic: Path):
    """Load a schematic file."""
    doc = load_schematic(str(minimal_schematic))
    assert doc is not None
    assert doc.tag == "kicad_sch"


def test_parse_schematic(minimal_schematic: Path):
    """Parse schematic into structured data."""
    doc = load_schematic(str(minimal_schematic))
    sch = Schematic(doc)

    # Check symbols
    assert len(sch.symbols) == 1
    symbol = sch.symbols[0]
    assert symbol.reference == "R1"
    assert symbol.value == "10k"


def test_schematic_wires(minimal_schematic: Path):
    """Parse schematic wires."""
    doc = load_schematic(str(minimal_schematic))
    sch = Schematic(doc)

    assert len(sch.wires) == 1
    wire = sch.wires[0]
    assert wire.start == (90, 100)
    assert wire.end == (100, 100)


def test_schematic_labels(minimal_schematic: Path):
    """Parse schematic labels."""
    doc = load_schematic(str(minimal_schematic))
    sch = Schematic(doc)

    assert len(sch.labels) == 1
    label = sch.labels[0]
    assert label.text == "NET1"
