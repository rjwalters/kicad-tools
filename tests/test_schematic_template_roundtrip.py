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

Issue #6051 raises the bar from "same netlist" to "same file": a load + save
of an unedited schematic must reproduce the original s-expression tree
exactly (whitespace aside) -- label justification, text font size and UUID,
power-symbol field positions, the format ``version``, ``embedded_fonts``,
``polyline`` and every other construct the model does not track.  The
allowlist of tolerated differences is empty.  Edits still regenerate only
the edited element.

The KiCad-backed tests skip when KiCad or its bundled templates are absent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_tools.core.version import KICAD_SCH_FORMAT_VERSION
from kicad_tools.schematic.models import Schematic
from kicad_tools.schematic.models.elements import GlobalLabel, HierarchicalLabel, Label, PowerSymbol
from kicad_tools.sexp import parse_file, parse_string
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


# --- source-preserving save (issue #6051, no KiCad needed) -------------------

_REPO = Path(__file__).resolve().parents[1]

# A KiCad 9 style sheet exercising each construct #6051 reported lost.
_KICAD9_SCH = """(kicad_sch
  (version 20250114)
  (generator "eeschema")
  (generator_version "9.0")
  (uuid "0cbb3bbb-8297-40d4-b3a9-e92b3d8d375c")
  (paper "A4")
  (title_block (title "Demo") (rev "1"))
  (lib_symbols)
  (text "Note\\nsecond line" (exclude_from_sim no) (at 245.11 66.04 0)
    (effects (font (size 2.54 2.54)) (justify left bottom))
    (uuid "c47a68c0-ec3e-4aaa-803c-2ea9d781766e"))
  (polyline (pts (xy 242.57 12.7) (xy 242.57 68.58))
    (stroke (width 0) (type dash))
    (uuid "2012905f-749f-46db-b07c-d64ef3599d24"))
  (junction (at 21.59 132.08) (diameter 1.016) (color 0 0 0 0)
    (uuid "127679a9-3981-4934-815e-896a4e3ff56e"))
  (wire (pts (xy 62.23 77.47) (xy 74.93 77.47))
    (stroke (width 0) (type solid))
    (uuid "010ba307-2067-49d3-b0fa-6414143f3fc2"))
  (label "+IN-2" (at 257.81 35.56 180)
    (effects (font (size 1.27 1.27)) (justify right bottom))
    (uuid "2440d962-c057-42bd-93c2-c47111a5c311"))
  (label "SC_LINK" (at 257.81 27.94 180)
    (effects (font (size 1.27 1.27)) (justify right bottom))
    (uuid "5d97dcd9-09a3-4fac-9dba-e9b66af562da"))
  (symbol (lib_id "power:+48V") (at 257.81 50.8 90) (unit 1)
    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)
    (uuid "00000000-0000-0000-0000-00005ffc5cf6")
    (property "Reference" "#PWR0102" (at 261.62 50.8 0)
      (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Value" "+48V" (at 254.5588 50.419 90)
      (effects (font (size 1.27 1.27)) (justify left)))
    (pin "1" (uuid "06301014-7222-4948-969d-d91f20ce12fd"))
    (instances (project "Demo"
      (path "/0cbb3bbb-8297-40d4-b3a9-e92b3d8d375c" (reference "#PWR0102") (unit 1)))))
  (sheet_instances (path "/" (page "1")))
  (embedded_fonts no)
)
"""


def _tree(text: str) -> str:
    """Canonical (whitespace-free) rendering of a parsed tree."""
    return parse_string(text).to_string()


def _load_text(tmp_path: Path, text: str) -> Schematic:
    src = tmp_path / "in.kicad_sch"
    src.write_text(text)
    return Schematic.load(src)


def _children(text: str) -> list[str]:
    return [child.to_string() for child in parse_string(text).children]


def test_unedited_save_reproduces_kicad9_tree(tmp_path: Path):
    out = _load_text(tmp_path, _KICAD9_SCH).to_sexp()
    assert _tree(out) == _tree(_KICAD9_SCH)


def test_unedited_save_keeps_each_reported_construct(tmp_path: Path):
    out = _load_text(tmp_path, _KICAD9_SCH).to_sexp()
    root = parse_string(out)
    # Data loss: embedded_fonts and the power-symbol field positions.
    assert root.get("embedded_fonts") is not None
    pwr = root.find("symbol")
    value = next(p for p in pwr.find_all("property") if p.get_first_atom() == "Value")
    assert value["at"].get_atoms() == [254.5588, 50.419, 90]
    # Display: label alignment, text font size and UUID.
    for label in root.find_all("label"):
        assert label["effects"]["justify"].get_atoms() == ["right", "bottom"]
    text = root.find("text")
    assert text["effects"]["font"]["size"].get_atoms() == [2.54, 2.54]
    assert text["uuid"].get_first_atom() == "c47a68c0-ec3e-4aaa-803c-2ea9d781766e"
    # Cosmetic: the source format version is kept, not restamped.
    assert root["version"].get_first_atom() == 20250114
    assert root["generator_version"].get_first_atom() == "9.0"


def test_edited_element_is_regenerated_in_place_others_kept(tmp_path: Path):
    sch = _load_text(tmp_path, _KICAD9_SCH)
    moved = next(lbl for lbl in sch.labels if lbl.text == "SC_LINK")
    moved.x = 260.35
    before, after = _children(_KICAD9_SCH), _children(sch.to_sexp())
    assert len(after) == len(before)
    changed = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    assert len(changed) == 1
    assert "SC_LINK" in after[changed[0]] and "260.35" in after[changed[0]]


def test_removed_and_added_elements(tmp_path: Path):
    sch = _load_text(tmp_path, _KICAD9_SCH)
    sch.wires.clear()
    sch.text_notes.append(("added", 10.16, 10.16))
    out = parse_string(sch.to_sexp())
    names = [child.name for child in out.children]
    assert "wire" not in names
    # The new note lands before the trailer; the original note is untouched.
    assert names[-2:] == ["sheet_instances", "embedded_fonts"]
    notes = out.find_all("text")
    assert [n.get_first_atom() for n in notes][-1] == "added"
    assert notes[0]["uuid"].get_first_atom() == "c47a68c0-ec3e-4aaa-803c-2ea9d781766e"
    assert out.find("polyline") is not None


def test_deepcopy_removed_elements_stay_removed(tmp_path: Path):
    """Issue #6071: a deepcopy must not resurrect elements removed from it."""
    import copy

    sch = _load_text(tmp_path, _KICAD9_SCH)
    dup = copy.deepcopy(sch)
    dup.wires.clear()
    dup.labels.clear()
    out = parse_string(dup.to_sexp())
    assert out.find("wire") is None
    assert out.find("label") is None
    # The original is unaffected by edits to the copy.
    assert _children(sch.to_sexp()) == _children(_KICAD9_SCH)


def test_deepcopy_and_original_save_independently(tmp_path: Path):
    import copy

    sch = _load_text(tmp_path, _KICAD9_SCH)
    dup = copy.deepcopy(sch)
    sch.wires.clear()
    next(lbl for lbl in dup.labels if lbl.text == "SC_LINK").x = 260.35
    a, b = parse_string(sch.to_sexp()), parse_string(dup.to_sexp())
    assert a.find("wire") is None and b.find("wire") is not None
    assert "260.35" not in a.to_string() and "260.35" in b.to_string()


def test_pickle_roundtrip_saves_like_original(tmp_path: Path):
    import pickle

    sch = _load_text(tmp_path, _KICAD9_SCH)
    dup = pickle.loads(pickle.dumps(sch))
    assert _children(dup.to_sexp()) == _children(sch.to_sexp()) == _children(_KICAD9_SCH)
    dup.wires.clear()
    assert parse_string(dup.to_sexp()).find("wire") is None
    assert parse_string(sch.to_sexp()).find("wire") is not None


def test_header_edit_regenerates_only_that_node(tmp_path: Path):
    sch = _load_text(tmp_path, _KICAD9_SCH)
    sch.title = "Renamed"
    out = parse_string(sch.to_sexp())
    assert out["title_block"]["title"].get_first_atom() == "Renamed"
    assert out["version"].get_first_atom() == 20250114
    assert out["sheet_instances"]["path"].get_first_atom() == "/"


def test_older_format_is_regenerated_at_kct_version(tmp_path: Path):
    """Files older than kct's format are rewritten wholesale, as before.

    Re-emitting pre-``KICAD_SCH_FORMAT_VERSION`` nodes verbatim under the
    version kct stamps would skip KiCad's legacy-format conversions, and
    keeping the old version would misdescribe the regenerated nodes.
    """
    old = _KICAD9_SCH.replace("(version 20250114)", "(version 20230121)")
    root = parse_string(_load_text(tmp_path, old).to_sexp())
    assert root["version"].get_first_atom() == KICAD_SCH_FORMAT_VERSION
    assert root.get("polyline") is None


def _fleet_schematics() -> list[Path]:
    return sorted((_REPO / "boards").glob("**/*.kicad_sch"))


_FLEET = _fleet_schematics()


@pytest.mark.parametrize(
    "sch_path",
    _FLEET or [pytest.param(None, marks=pytest.mark.skip(reason="no fleet schematics"))],
    ids=[str(p.relative_to(_REPO / "boards")) for p in _FLEET] or ["no-fleet"],
)
def test_fleet_schematic_load_save_is_tree_identical(sch_path: Path):
    out = Schematic.load(sch_path).to_sexp()
    assert parse_string(out).to_string() == parse_file(sch_path).to_string()


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


@pytest.mark.parametrize(
    "template",
    _TEMPLATES or [pytest.param(None, marks=pytest.mark.skip(reason="no KiCad templates"))],
    ids=[p.parent.name for p in _TEMPLATES] or ["no-templates"],
)
def test_template_load_save_is_tree_identical(template: Path):
    """Unedited load + save reproduces the template's tree (issue #6051).

    Allowlist of tolerated differences: none (whitespace is not part of the
    parsed tree).
    """
    out = Schematic.load(template).to_sexp()
    assert parse_string(out).to_string() == parse_file(template).to_string()


@pytest.mark.slow
@pytest.mark.parametrize(
    "template",
    _TEMPLATES or [pytest.param(None, marks=pytest.mark.skip(reason="no KiCad templates"))],
    ids=[p.parent.name for p in _TEMPLATES] or ["no-templates"],
)
def test_edited_template_still_loads_in_kicad(template: Path, tmp_path: Path):
    """Regenerated nodes mixed with verbatim newer-format ones stay loadable.

    The saved file keeps the template's (newer) format ``version`` while an
    edited element is written by kct's builders.
    """
    cli = _kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not installed")

    rt_dir = tmp_path / "rt"
    shutil.copytree(template.parent, rt_dir)
    sch = Schematic.load(str(template))
    for label in sch.labels[:1]:
        label.x += 2.54
    sch.text_notes.append(("kct edit", 12.7, 12.7))
    sch.write(str(rt_dir / template.name), auto_size_paper=False)

    _netlist(cli, rt_dir / template.name, tmp_path / "after.net")
