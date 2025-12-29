"""Tests for PCB parsing."""

from pathlib import Path

from kicad_tools import load_pcb
from kicad_tools.schema import PCB


def test_load_pcb(minimal_pcb: Path):
    """Load a PCB file."""
    doc = load_pcb(str(minimal_pcb))
    assert doc is not None
    assert doc.tag == "kicad_pcb"


def test_parse_pcb(minimal_pcb: Path):
    """Parse PCB into structured data."""
    doc = load_pcb(str(minimal_pcb))
    pcb = PCB(doc)

    # Check layers (dict of layer_num -> Layer)
    assert len(pcb.layers) > 0
    layer_names = [l.name for l in pcb.layers.values()]
    assert "F.Cu" in layer_names
    assert "B.Cu" in layer_names


def test_pcb_nets(minimal_pcb: Path):
    """Parse PCB nets."""
    doc = load_pcb(str(minimal_pcb))
    pcb = PCB(doc)

    # nets is a dict of net_num -> Net
    assert len(pcb.nets) >= 2
    net_names = [n.name for n in pcb.nets.values()]
    assert "GND" in net_names
    assert "+3.3V" in net_names


def test_pcb_footprints(minimal_pcb: Path):
    """Parse PCB footprints."""
    doc = load_pcb(str(minimal_pcb))
    pcb = PCB(doc)

    assert len(pcb.footprints) == 1
    fp = pcb.footprints[0]
    assert fp.name == "Resistor_SMD:R_0402_1005Metric"
    assert len(fp.pads) == 2


def test_pcb_traces(minimal_pcb: Path):
    """Parse PCB traces."""
    doc = load_pcb(str(minimal_pcb))
    pcb = PCB(doc)

    assert len(pcb.segments) == 1
    seg = pcb.segments[0]
    assert seg.net_number == 1
    assert seg.layer == "F.Cu"
