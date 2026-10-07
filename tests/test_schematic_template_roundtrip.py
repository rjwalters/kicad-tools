"""Load + save round trip of the schematic model stays KiCad-loadable (issue #6048).

The model rewrites the whole file, so a byte-identical save is not expected.
What *is* required: the saved file still loads in ``kicad-cli`` and its
netlist keeps the same nets with the same connections.

Two defects broke that on the KiCad 10 templates:

* Labels named with a numeric-looking string (``"4"``, ``"13"`` -- every
  Arduino digital pin) were written as bare atoms ``(label 4 ...)``, which
  ``kicad-cli`` rejects with "Failed to load schematic" (Arduino_Uno,
  Arduino_Mega).
* Power symbols dropped their ``Value`` property and re-derived it from the
  lib_id, renaming the net (``power:+3.3V`` placed with Value ``+3V3``;
  ``Proj-rescue:+16V-power`` with Value ``+16V``).

The KiCad-backed tests skip when KiCad or its bundled templates are absent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_tools.schematic.models import Schematic
from kicad_tools.schematic.models.elements import GlobalLabel, HierarchicalLabel, Label, PowerSymbol
from kicad_tools.sexp import parse_string
from kicad_tools.sexp.builders import text_node

# --- unit checks (no KiCad needed) -------------------------------------------


@pytest.mark.parametrize(
    ("node", "text"),
    [
        (Label("4", 0, 0).to_sexp_node(), "4"),
        (HierarchicalLabel("13", 0, 0).to_sexp_node(), "13"),
        (GlobalLabel("3.3", 0, 0).to_sexp_node(), "3.3"),
        (text_node("5", 0, 0, "u"), "5"),
    ],
    ids=["label", "hierarchical_label", "global_label", "text"],
)
def test_numeric_looking_label_text_is_quoted(node, text):
    # The text is the node's first atom; KiCad rejects it unquoted.
    tokens = node.to_string().split()
    assert tokens[1] == f'"{text}"', tokens[:3]


def test_power_symbol_keeps_value_distinct_from_lib_id():
    node = parse_string(
        """(symbol (lib_id "power:+3.3V") (at 10 20 0) (unit 1)
            (uuid "00000000-0000-0000-0000-000000000001")
            (property "Reference" "#PWR04" (at 10 22 0))
            (property "Value" "+3V3" (at 10 18 0)))"""
    )
    pwr = PowerSymbol.from_sexp(node)
    assert pwr.reference == "#PWR04"
    assert pwr.net_name == "+3V3"
    written = pwr.to_sexp_node("proj", "/").to_string()
    assert '(property "Value" "+3V3"' in written


def test_programmatic_power_symbol_value_defaults_to_lib_name():
    pwr = PowerSymbol(lib_id="power:GND", x=0, y=0)
    assert pwr.net_name == "GND"
    assert '(property "Value" "GND"' in pwr.to_sexp_node("proj", "/").to_string()


# --- KiCad-backed round trip over every bundled template ---------------------


def _kicad_cli() -> Path | None:
    from kicad_tools.cli.runner import find_kicad_cli

    return find_kicad_cli()


def _template_dirs() -> list[Path]:
    candidates = [
        os.environ.get("KICAD10_TEMPLATE_DIR"),
        os.environ.get("KICAD9_TEMPLATE_DIR"),
        "/Applications/KiCad/KiCad.app/Contents/SharedSupport/template",
        "/usr/share/kicad/template",
        "/usr/local/share/kicad/template",
    ]
    return [Path(c) for c in candidates if c and Path(c).is_dir()]


def _template_schematics() -> list[Path]:
    for base in _template_dirs():
        found = []
        for project in sorted(base.glob("*/*.kicad_pro")):
            sch = project.with_suffix(".kicad_sch")
            if sch.is_file():
                found.append(sch)
        if found:
            return found
    return []


def _netlist(cli: Path, sch: Path, out: Path) -> dict[str, frozenset[tuple[str, str]]]:
    result = subprocess.run(
        [str(cli), "sch", "export", "netlist", "-o", str(out), str(sch)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"kicad-cli could not load {sch.name} (exit {result.returncode}): "
        f"{result.stdout.strip()} {result.stderr.strip()}"
    )
    root = parse_string(out.read_text())
    nets: dict[str, frozenset[tuple[str, str]]] = {}
    for net in root["nets"].find_all("net"):
        name = str(net["name"].get_first_atom())
        nets[name] = frozenset(
            (str(n["ref"].get_first_atom()), str(n["pin"].get_first_atom()))
            for n in net.find_all("node")
        )
    return nets


_TEMPLATES = _template_schematics()


@pytest.mark.slow
@pytest.mark.parametrize(
    "template",
    _TEMPLATES or [pytest.param(None, marks=pytest.mark.skip(reason="no KiCad templates"))],
    ids=[p.parent.name for p in _TEMPLATES] or ["no-templates"],
)
def test_template_load_save_keeps_kicad_netlist(template: Path, tmp_path: Path):
    cli = _kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not installed")

    # Copy the whole project so hierarchical sheets and the project's
    # sym-lib-table resolve exactly as they do for the original.
    orig_dir = tmp_path / "orig"
    rt_dir = tmp_path / "rt"
    shutil.copytree(template.parent, orig_dir)
    shutil.copytree(template.parent, rt_dir)

    before = _netlist(cli, orig_dir / template.name, tmp_path / "before.net")

    Schematic.load(str(template)).write(str(rt_dir / template.name), auto_size_paper=False)

    after = _netlist(cli, rt_dir / template.name, tmp_path / "after.net")

    assert sorted(after) == sorted(before), "net names changed"
    changed = {name for name in before if before[name] != after[name]}
    assert not changed, f"connections changed on nets: {sorted(changed)}"
