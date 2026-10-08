"""Deterministic UUIDs for generated schematics (Issue #6076).

Covers :mod:`kicad_tools.core.schematic_uuids` and its use by the schematic
model (``kicad_tools.schematic.models``), the file-editing schematic API
(``kicad_tools.schema.schematic``), the PCB builder (``kicad_tools.schema.pcb``)
and ``Project.create``:

* the same build writes the same bytes in every process, whatever the
  ``PYTHONHASHSEED`` (pattern from #6070);
* duplicate keys never collide;
* UUIDs loaded from a file are never rewritten, and newly minted ones never
  reuse one the file already holds (the #6109 ``keep`` principle);
* the UUID a caller reads off an in-memory object is the one in the file.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

from kicad_tools.core.schematic_uuids import (
    SCHEMATIC_UUID_NAMESPACE,
    ProvisionalUuid,
    UuidMinter,
    UuidSequence,
    is_provisional,
    pin_uuid,
    provisional_uuid,
    root_sheet_uuid,
    stable_uuid,
    uuids_in_text,
)
from kicad_tools.schematic.models.elements import Junction, Label, Wire
from kicad_tools.schematic.models.schematic import Schematic

_UUID_RE = re.compile(r'\(uuid "([0-9a-f-]{36})"\)')

_LIB = """(kicad_symbol_lib
\t(version 20231120)
\t(generator "test")
\t(symbol "WIDGET"
\t\t(property "Reference" "U" (at 0 5.08 0) (effects (font (size 1.27 1.27))))
\t\t(property "Value" "WIDGET" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
\t\t(symbol "WIDGET_1_1"
\t\t\t(pin input line (at -7.62 2.54 0) (length 2.54) (name "IN" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
\t\t\t(pin output line (at 7.62 0 180) (length 2.54) (name "OUT" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
\t\t)
\t)
)
"""


def _lib(tmp_path: Path) -> Path:
    path = tmp_path / "widgets.kicad_sym"
    path.write_text(_LIB)
    return path


def _build(lib_path: Path) -> Schematic:
    """A schematic exercising every element kind, with deliberate duplicates."""
    sch = Schematic(title="UUID test", project_name="uuidtest", local_symbol_libs=[lib_path])
    sch.add_symbol("widgets:WIDGET", 50.8, 50.8, "U1", "WIDGET")
    sch.add_symbol("widgets:WIDGET", 101.6, 50.8, "U?", "WIDGET")
    sch.add_symbol("widgets:WIDGET", 152.4, 50.8, "U?", "WIDGET")  # duplicate ref
    sch.add_pwr_symbol("VCC", 25.4, 25.4)
    sch.add_wire((25.4, 25.4), (50.8, 25.4))
    sch.add_wire((25.4, 25.4), (50.8, 25.4), warn_on_collision=False)  # duplicate
    sch.add_junction(25.4, 25.4)
    sch.add_no_connect(76.2, 76.2)
    sch.add_label("NET_A", 50.8, 25.4)
    sch.add_label("NET_A", 50.8, 25.4)  # duplicate
    sch.add_global_label("G", 50.8, 25.4)
    sch.add_hier_label("H", 25.4, 25.4)
    sch.add_text("note", 10.16, 10.16)
    sch.add_text("note", 10.16, 10.16)  # duplicate
    # Built directly, bypassing the add_* helpers.
    sch.wires.append(Wire(x1=0.0, y1=0.0, x2=2.54, y2=0.0))
    return sch


# --- the scheme itself ------------------------------------------------------


def test_namespace_is_pinned():
    # Changing it renumbers every generated schematic; this is deliberate.
    assert uuid.UUID("6f0a7e3c-6076-5d1b-8a2e-4c5d6e076076") == SCHEMATIC_UUID_NAMESPACE
    assert stable_uuid("sheet", "", "p", "t", "1") == str(
        uuid.uuid5(SCHEMATIC_UUID_NAMESPACE, "sheet\x1f\x1fp\x1ft\x1f1")
    )
    assert root_sheet_uuid("p", "t") == stable_uuid("sheet", "", "p", "t", "1")


def test_stable_uuid_is_version_5_and_normalizes_numbers():
    value = stable_uuid("wire", 1, 2.0, 3.00000000001)
    assert uuid.UUID(value).version == 5
    assert value == stable_uuid("wire", 1.0, 2, 3)
    assert stable_uuid("wire", -0.0) == stable_uuid("wire", 0)
    # Separator keeps differently split keys apart.
    assert stable_uuid("a b", "c") != stable_uuid("a", "b c")


def test_minter_duplicate_keys_never_collide():
    minter = UuidMinter()
    minted = [minter.mint("wire", 0, 0, 1, 1) for _ in range(200)]
    minted += [minter.mint("wire", 0, 0, 1, 2) for _ in range(200)]
    assert len(set(minted)) == len(minted)
    # ...and the sequence is reproducible.
    again = UuidMinter()
    assert [again.mint("wire", 0, 0, 1, 1) for _ in range(200)] == minted[:200]


def test_minter_skips_reserved():
    first = UuidMinter().mint("k")
    minter = UuidMinter([first.upper()])
    assert minter.mint("k") != first
    assert minter.is_taken(first)


def test_uuid_sequence_reset_and_scopes():
    seq = UuidSequence("board/a")
    run1 = [seq() for _ in range(5)]
    seq.reset()
    assert [seq() for _ in range(5)] == run1
    assert len(set(run1)) == 5
    assert UuidSequence("board/b")() != run1[0]
    assert UuidSequence("board/a", reserved=[run1[0]])() != run1[0]


def test_provisional_uuid_marks_but_behaves_like_str():
    value = provisional_uuid()
    assert isinstance(value, ProvisionalUuid) and is_provisional(value)
    assert value == str(value) and hash(value) == hash(str(value))
    assert not is_provisional(str(value))
    assert uuid.UUID(value).version == 4


def test_pin_uuid_separates_stacked_pins():
    sym = stable_uuid("x")
    assert pin_uuid(sym, "1") != pin_uuid(sym, "1", 1) != pin_uuid(sym, "2")


# --- the schematic model ----------------------------------------------------


_SUBPROCESS = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, sys.argv[2])
    from test_schematic_uuids_6076 import _build
    print(_build(Path(sys.argv[1])).to_sexp())
    """
)


def _run(seed: str, lib_path: Path) -> str:
    env = {**os.environ, "PYTHONHASHSEED": seed}
    return subprocess.run(
        [sys.executable, "-c", _SUBPROCESS, str(lib_path), str(Path(__file__).parent)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_schematic_output_is_hashseed_independent(tmp_path):
    lib_path = _lib(tmp_path)
    outs = {_run(seed, lib_path) for seed in ("0", "1", "12345")}
    assert len(outs) == 1
    # Same bytes in this process too.
    assert _build(lib_path).to_sexp() + "\n" == outs.pop()


def test_every_uuid_in_output_is_unique_and_deterministic(tmp_path):
    text = _build(_lib(tmp_path)).to_sexp()
    found = _UUID_RE.findall(text)
    # root + 3 symbols (+2 pins each) + power (+1 pin) + 2 wires + direct wire
    # + junction + no-connect + 2 labels + global + hier + 2 notes
    assert len(found) == 1 + 3 * 3 + 2 + 3 + 1 + 1 + 2 + 1 + 1 + 2
    assert len(set(found)) == len(found)
    assert all(uuid.UUID(u).version == 5 for u in found)


def test_in_memory_uuids_match_the_written_file(tmp_path):
    sch = _build(_lib(tmp_path))
    u1 = sch.symbols[0]
    before = u1.uuid_str  # read before writing: must already be final
    assert not is_provisional(before)
    out = tmp_path / "t.kicad_sch"
    sch.write(out)
    text = out.read_text()
    assert u1.uuid_str == before
    assert f'(uuid "{before}")' in text
    assert f'(path "/{sch.sheet_uuid}"' in text
    # Provisional element UUIDs were written back onto the elements.
    for elem in [*sch.wires, *sch.labels, *sch.junctions, *sch.no_connects]:
        assert not is_provisional(elem.uuid_str)
        assert f'(uuid "{elem.uuid_str}")' in text
    # A second write is byte-identical.
    sch.write(out)
    assert out.read_text() == text


def test_explicit_uuid_is_kept(tmp_path):
    sch = Schematic(title="t")
    explicit = str(uuid.uuid4())
    sch.wires.append(Wire(x1=0, y1=0, x2=1, y2=0, uuid_str=explicit))
    assert f'(uuid "{explicit}")' in sch.to_sexp()
    assert sch.wires[0].uuid_str == explicit


def test_loaded_uuids_are_never_rewritten(tmp_path):
    """Load-modify-save of a user schematic keeps every UUID it was loaded with."""
    lib_path = _lib(tmp_path)
    src = Schematic(title="User", local_symbol_libs=[lib_path], sheet_uuid=str(uuid.uuid4()))
    sym = src.add_symbol("widgets:WIDGET", 50.8, 50.8, "U1", "WIDGET")
    sym.uuid_str = str(uuid.uuid4())  # as KiCad would have written it
    src.wires.append(Wire(x1=0, y1=0, x2=2.54, y2=0, uuid_str=str(uuid.uuid4())))
    src.junctions.append(Junction(x=0, y=0, uuid_str=str(uuid.uuid4())))
    src.labels.append(Label(text="L", x=0, y=0, uuid_str=str(uuid.uuid4())))
    path = tmp_path / "user.kicad_sch"
    src.write(path)
    original = set(_UUID_RE.findall(path.read_text()))

    loaded = Schematic.load(path, local_symbol_libs=[lib_path])
    loaded.add_wire((2.54, 0), (5.08, 0))
    loaded.write(path)
    after = set(_UUID_RE.findall(path.read_text()))
    assert original <= after
    new = after - original
    assert len(new) == 1 and uuid.UUID(new.pop()).version == 5


def test_new_element_never_reuses_a_loaded_uuid(tmp_path):
    """A regenerated file's own uuid5 values are reserved on load."""
    lib_path = _lib(tmp_path)
    first = Schematic(title="Gen", local_symbol_libs=[lib_path])
    first.add_wire((0, 0), (2.54, 0))
    first.add_symbol("widgets:WIDGET", 50.8, 50.8, "U1", "WIDGET")
    path = tmp_path / "gen.kicad_sch"
    first.write(path)
    existing = set(_UUID_RE.findall(path.read_text()))

    loaded = Schematic.load(path, local_symbol_libs=[lib_path])
    wire = loaded.add_wire((0, 0), (2.54, 0), warn_on_collision=False)  # same content
    sym = loaded.add_symbol("widgets:WIDGET", 50.8, 101.6, "U1", "WIDGET")  # same ref
    loaded.add_text("n", 1.27, 1.27)
    text = loaded.to_sexp()
    found = _UUID_RE.findall(text)
    assert len(found) == len(set(found))
    loaded.write(path)
    assert wire.uuid_str not in existing
    assert sym.uuid_str not in existing


def test_default_sheet_uuid_is_derived_from_identity():
    assert Schematic(title="A").sheet_uuid == Schematic(title="A").sheet_uuid
    assert Schematic(title="A").sheet_uuid != Schematic(title="B").sheet_uuid
    assert (
        Schematic(title="A", project_name="p").sheet_uuid
        != Schematic(title="A", project_name="q").sheet_uuid
    )
    assert Schematic(title="A", sheet_uuid="explicit").sheet_uuid == "explicit"


def test_symbol_pin_uuids_derive_from_symbol_uuid(tmp_path):
    sch = Schematic(title="P", local_symbol_libs=[_lib(tmp_path)])
    sym = sch.add_symbol("widgets:WIDGET", 50.8, 50.8, "U1", "WIDGET")
    text = sch.to_sexp()
    for number in ("1", "2"):
        assert f'(uuid "{pin_uuid(sym.uuid_str, number)}")' in text


# --- the file-editing schematic API (kicad_tools.schema) --------------------


def test_schema_schematic_adds_are_deterministic_and_collision_free(tmp_path):
    from kicad_tools.schema.schematic import Schematic as DocSchematic

    path = tmp_path / "doc.kicad_sch"
    Schematic(title="Doc", sheet_uuid=str(uuid.uuid4())).write(path)

    def edit() -> str:
        doc = DocSchematic.load(path)
        doc.add_wire((0, 0), (2.54, 0))
        doc.add_wire((0, 0), (2.54, 0))  # duplicate content
        doc.add_label("N", (0, 0))
        doc.add_junction((0, 0))
        doc.add_symbol("Device:R", "R1", "1k", "", (10, 10), pin_numbers=["1", "1", "2"])
        return doc.sexp.to_string()

    first, second = edit(), edit()
    assert first == second
    found = _UUID_RE.findall(first)
    assert len(found) == len(set(found))


# --- PCB builder and Project.create -----------------------------------------


def test_pcb_builder_uuids_are_deterministic():
    from kicad_tools.schema.pcb import PCB

    def build() -> str:
        pcb = PCB.create(width=40, height=30, board_date="2026-01-01")
        pcb.add_trace((1, 1), (5, 1), net="A")
        pcb.add_trace((1, 1), (5, 1), net="A", dedupe=False)  # duplicate copper
        pcb.add_via(5, 1, net="A")
        return pcb._sexp.to_string()

    first = build()
    assert first == build()
    found = _UUID_RE.findall(first)
    assert len(found) == len(set(found)) and len(found) >= 4 + 2 + 1


def test_pcb_minted_uuid_avoids_existing_board_uuids():
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.create(width=40, height=30, board_date="2026-01-01")
    taken = uuids_in_text(pcb._sexp.to_string())
    pcb.add_via(5, 1, net="A")
    via_uuid = pcb._sexp.to_string()
    assert len(uuids_in_text(via_uuid) - taken) == 1


def test_project_create_is_deterministic(tmp_path):
    from kicad_tools.project import Project

    a, b = tmp_path / "a", tmp_path / "b"
    Project.create("demo", directory=a)
    Project.create("demo", directory=b)
    for suffix in (".kicad_pro", ".kicad_sch", ".kicad_pcb"):
        assert (a / f"demo{suffix}").read_bytes() == (b / f"demo{suffix}").read_bytes()


@pytest.mark.parametrize("kind", ["wire", "junction"])
def test_from_sexp_without_uuid_gets_deterministic_uuid_on_load(tmp_path, kind):
    body = "(wire (pts (xy 0 0) (xy 2.54 0)))" if kind == "wire" else "(junction (at 0 0))"
    path = tmp_path / "nouuid.kicad_sch"
    path.write_text(
        f'(kicad_sch (version 20250114) (generator "eeschema") (uuid "{uuid.uuid4()}")'
        f' (paper "A4") {body})'
    )
    one = Schematic.load(path)
    two = Schematic.load(path)
    elem = (one.wires or one.junctions)[0]
    assert not is_provisional(elem.uuid_str)
    assert elem.uuid_str == (two.wires or two.junctions)[0].uuid_str
