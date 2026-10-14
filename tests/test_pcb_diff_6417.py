"""``kct pcb diff OLD NEW``: geometric board diff (issue #6417).

Fixture tests mutate a scratch copy of board 01's routed PCB (and the copper
arc fixture) by text substitution, so each test controls exactly which uuids
and coordinates change.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid as uuidlib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from kicad_tools.cli import main as kct_main
from kicad_tools.cli.pcb_diff import run_pcb_diff
from kicad_tools.pcb.diff import DiffItem, diff_items, diff_pcbs, extract_items, to_json
from kicad_tools.schema.pcb import PCB

REPO = Path(__file__).resolve().parents[1]
BOARD01 = REPO / "boards/01-voltage-divider/output/voltage_divider_routed.kicad_pcb"
BOARD05 = REPO / "boards/05-bldc-motor-controller/output/bldc_controller_routed.kicad_pcb"
BOARD03 = REPO / "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb"
ARC = REPO / "tests/fixtures/copper_arc_routed.kicad_pcb"

_BLOCK = re.compile(r"^\t\((segment|via)\n.*?^\t\)\n", re.M | re.S)
_UUID = re.compile(r'\(uuid "([^"]+)"\)')


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _copy(tmp_path: Path, src: Path = BOARD01, name: str = "old.kicad_pcb") -> Path:
    dst = tmp_path / name
    shutil.copyfile(src, dst)
    return dst


def _write(tmp_path: Path, text: str, name: str = "new.kicad_pcb") -> Path:
    dst = tmp_path / name
    dst.write_text(text)
    return dst


def _blocks(text: str, kind: str) -> list[re.Match[str]]:
    return [m for m in _BLOCK.finditer(text) if m.group(1) == kind]


def _net_of(block: str) -> int:
    m = re.search(r"\(net (\d+)\)", block)
    assert m
    return int(m.group(1))


def _diff(old: Path, new: Path):
    return diff_pcbs(PCB.load(old), PCB.load(new))


def _changes(result):
    return [e for e in result.entries if e.status != "unchanged"]


def _segment(start, end, net: int, layer: str = "F.Cu", uid: str | None = None) -> str:
    uid = uid or str(uuidlib.uuid4())
    return (
        f"\t(segment\n\t\t(start {start[0]} {start[1]})\n\t\t(end {end[0]} {end[1]})\n"
        f'\t\t(width 0.5)\n\t\t(layer "{layer}")\n\t\t(uuid "{uid}")\n\t\t(net {net})\n\t)\n'
    )


def _via(at, net: int) -> str:
    return (
        f"\t(via\n\t\t(at {at[0]} {at[1]})\n\t\t(size 0.8)\n\t\t(drill 0.3)\n"
        f'\t\t(layers "F.Cu" "B.Cu")\n\t\t(uuid "{uuidlib.uuid4()}")\n\t\t(net {net})\n\t)\n'
    )


# ---------------------------------------------------------------------------
# diff_items unit tests
# ---------------------------------------------------------------------------


def _item(kind="segment", identity="", content=("a",), net="N", layers=("F.Cu",)):
    return DiffItem(kind=kind, identity=identity, content=content, net=net, layers=layers)


def test_content_multiset_pairs_duplicates_one_to_one():
    old = [_item(content=("x",)), _item(content=("x",))]
    new = [_item(content=("x",))]
    counts = diff_items(old, new).counts()
    assert counts == {"removed": 1, "added": 0, "changed": 0, "unchanged": 1}


def test_identity_pass_requires_matching_net_for_uuid_kinds():
    old = [_item(identity="u1", content=("a",), net="GND")]
    new = [_item(identity="u1", content=("b",), net="VBUS")]
    assert diff_items(old, new).counts() == {
        "removed": 1,
        "added": 1,
        "changed": 0,
        "unchanged": 0,
    }
    new_same_net = [_item(identity="u1", content=("b",), net="GND")]
    assert diff_items(old, new_same_net).counts()["changed"] == 1


def test_identity_pass_ignores_net_for_pads():
    old = [_item(kind="pad", identity="R1/1", content=("a",), net="GND")]
    new = [_item(kind="pad", identity="R1/1", content=("b",), net="VIN")]
    assert diff_items(old, new).counts()["changed"] == 1


def test_content_pass_runs_before_identity_pass():
    # NEW reuses uuid "u1" for a different item but also contains OLD's exact
    # content under another uuid: content must win.
    old = [_item(identity="u1", content=("a",))]
    new = [_item(identity="u2", content=("a",)), _item(identity="u1", content=("b",))]
    counts = diff_items(old, new).counts()
    assert counts == {"removed": 0, "added": 1, "changed": 0, "unchanged": 1}


def test_identity_requires_same_kind():
    old = [_item(kind="segment", identity="u1", content=("a",))]
    new = [_item(kind="via", identity="u1", content=("b",))]
    assert diff_items(old, new).counts()["changed"] == 0


def test_reference_kinds_pair_by_identity_before_content():
    # R1 and R2 (same content shape) trade places: content-first matching
    # would pair them with each other as unchanged.
    a, b = ("R_0805", "F.Cu", (0.0, 0.0), 0.0, "10k"), ("R_0805", "F.Cu", (0.0, 9.0), 0.0, "10k")
    old = [
        _item(kind="footprint", identity="R1", content=a),
        _item(kind="footprint", identity="R2", content=b),
    ]
    new = [
        _item(kind="footprint", identity="R1", content=b),
        _item(kind="footprint", identity="R2", content=a),
    ]
    result = diff_items(old, new)
    assert result.counts() == {"removed": 0, "added": 0, "changed": 2, "unchanged": 0}
    assert {e.key for e in result.changes()} == {"R1", "R2"}


def test_renamed_reference_is_removed_plus_added():
    c = ("R_0805", "F.Cu", (0.0, 0.0), 0.0, "10k")
    old = [_item(kind="footprint", identity="R1", content=c)]
    new = [_item(kind="footprint", identity="R9", content=c)]
    assert diff_items(old, new).counts() == {"removed": 1, "added": 1, "changed": 0, "unchanged": 0}


def test_duplicate_content_reports_the_uuid_that_disappeared():
    old = [_item(identity="a", content=("x",)), _item(identity="b", content=("x",))]
    new = [_item(identity="b", content=("x",))]
    result = diff_items(old, new)
    [removed] = result.changes()
    assert (removed.status, removed.key) == ("removed", "a")


def test_zone_ring_ignores_start_vertex_and_winding():
    from kicad_tools.pcb.diff import _canonical_ring

    ring = [(0, 0), (10, 0), (10, 5), (0, 5)]
    assert _canonical_ring(ring) == _canonical_ring(ring[2:] + ring[:2])
    assert _canonical_ring(ring) == _canonical_ring(ring[::-1])
    assert _canonical_ring(ring) != _canonical_ring([(0, 0), (10, 0), (10, 6), (0, 5)])


# ---------------------------------------------------------------------------
# self-diff and normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("board", [BOARD01, ARC])
def test_self_diff_is_empty(board: Path, capsys):
    assert run_pcb_diff(str(board), str(board)) == 0
    out = capsys.readouterr().out
    assert "removed 0  added 0  changed 0" in out


def test_self_diff_board05_smoke():
    result = _diff(BOARD05, BOARD05)
    assert result.counts()["unchanged"] > 1000
    assert not result.has_changes


def test_board01_item_inventory():
    kinds: dict[str, int] = {}
    for item in extract_items(PCB.load(BOARD01)):
        kinds[item.kind] = kinds.get(item.kind, 0) + 1
    assert kinds["segment"] == 14
    assert kinds["via"] == 4
    assert kinds["footprint"] == 4
    assert kinds["graphic"] >= 1


def _reformat(text: str) -> str:
    text = _UUID.sub(lambda m: f'(uuid "{uuidlib.uuid4()}")', text)
    text = re.sub(r"\(tstamp [^)]+\)", lambda m: f"(tstamp {uuidlib.uuid4()})", text)
    # Floats like 1.5 -> 1.500000 (only bare numeric tokens, never inside strings).
    text = re.sub(
        r"(?<=[\s(])(-?\d+\.\d+)(?=[\s)])",
        lambda m: f"{float(m.group(1)):.6f}",
        text,
    )
    return text.replace("\t", "  ")


def test_regenerated_uuids_and_reformatting_report_nothing(tmp_path: Path):
    text = BOARD01.read_text()
    new_text = _reformat(text)
    assert "1.500000" not in text and "0.500000" in new_text and "\t" not in new_text
    assert not set(_UUID.findall(new_text)) & set(_UUID.findall(text))
    old = _copy(tmp_path)
    new = _write(tmp_path, new_text)
    result = _diff(old, new)
    assert _changes(result) == []
    assert run_pcb_diff(str(old), str(new), fmt="json") == 0


# ---------------------------------------------------------------------------
# mutations
# ---------------------------------------------------------------------------


def test_shifted_deterministic_uuids_report_one_added(tmp_path: Path):
    text = BOARD01.read_text()
    segs = _blocks(text, "segment")
    uuids = [(_UUID.search(m.group(0)) or [None, None])[1] for m in segs]
    with_uuid = [i for i, u in enumerate(uuids) if u]
    pool = [uuids[i] for i in with_uuid] + [str(uuidlib.uuid4())]
    # Renumber positionally: the k-th uuid-bearing segment now gets the
    # (k+1)-th uuid, as a seeded re-route would after one early insertion.
    out = []
    pos = 0
    k = 1
    for idx, m in enumerate(segs):
        out.append(text[pos : m.start()])
        block = m.group(0)
        if idx in with_uuid:
            block = _UUID.sub(f'(uuid "{pool[k]}")', block)
            k += 1
        out.append(block)
        pos = m.end()
    out.append(text[pos:])
    renumbered = "".join(out)
    first = _blocks(renumbered, "segment")[0].start()
    extra = _segment((120.0, 120.0), (121.0, 120.0), net=1, uid=pool[0])
    new_text = renumbered[:first] + extra + renumbered[first:]
    assert _UUID.findall(new_text) != _UUID.findall(text)  # uuids really shifted

    result = _diff(_copy(tmp_path), _write(tmp_path, new_text))
    counts = result.counts()
    assert counts["added"] == 1
    assert counts["changed"] == 0
    assert counts["removed"] == 0


def test_moved_via_same_uuid_reports_one_changed(tmp_path: Path):
    text = BOARD01.read_text()
    via = _blocks(text, "via")[0]
    block = via.group(0)
    at = re.search(r"\(at ([\d.]+) ([\d.]+)\)", block)
    assert at
    x, y = float(at.group(1)), float(at.group(2))
    moved = block.replace(at.group(0), f"(at {x + 0.5} {y})")
    new = _write(tmp_path, text[: via.start()] + moved + text[via.end() :])

    changes = _changes(_diff(_copy(tmp_path), new))
    assert len(changes) == 1
    (entry,) = changes
    assert (entry.status, entry.kind, entry.net) == ("changed", "via", "VOUT")
    before = entry.before.geometry["position"]
    after = entry.after.geometry["position"]
    assert after[0] - before[0] == pytest.approx(0.5)
    assert after[1] == pytest.approx(before[1])


def _reroute_net(text: str, net: int) -> tuple[str, int]:
    """Replace every segment of ``net`` with two fresh segments."""
    segs = [m for m in _blocks(text, "segment") if _net_of(m.group(0)) == net]
    assert segs
    for m in reversed(segs):
        text = text[: m.start()] + text[m.end() :]
    first = _blocks(text, "segment")[0].start()
    fresh = _segment((130.0, 70.0), (135.0, 70.0), net) + _segment(
        (135.0, 70.0), (135.0, 75.0), net, layer="B.Cu"
    )
    return text[:first] + fresh + text[first:], len(segs)


def test_rerouted_net_reports_only_removed_and_added_on_that_net(tmp_path: Path):
    new_text, n_old = _reroute_net(BOARD01.read_text(), net=1)
    result = _diff(_copy(tmp_path), _write(tmp_path, new_text))
    changes = _changes(result)
    assert {e.net for e in changes} == {"VIN"}
    assert {e.kind for e in changes} == {"segment"}
    counts = result.counts()
    assert counts["changed"] == 0
    assert counts["removed"] == n_old
    assert counts["added"] == 2


def _mixed(text: str) -> str:
    segs = [m for m in _blocks(text, "segment") if _UUID.search(m.group(0))]
    moved, deleted = segs[0], segs[-1]
    block = moved.group(0)
    end = re.search(r"\(end ([\d.]+) ([\d.]+)\)", block)
    assert end
    new_block = block.replace(end.group(0), f"(end {float(end.group(1)) + 1.0} {end.group(2)})")
    text = (
        text[: deleted.start()]
        + _via((150.0, 80.0), net=3)
        + text[deleted.end() :]  # delete one track
    )
    return text[: moved.start()] + new_block + text[moved.end() :]


def test_mixed_fixture_counts_and_attribution(tmp_path: Path):
    text = BOARD01.read_text()
    segs = [m for m in _blocks(text, "segment") if _UUID.search(m.group(0))]
    moved_block, deleted_block = segs[0].group(0), segs[-1].group(0)
    result = _diff(_copy(tmp_path), _write(tmp_path, _mixed(text)))
    counts = result.counts()
    assert (counts["changed"], counts["added"], counts["removed"]) == (1, 1, 1)

    pcb = PCB.load(BOARD01)
    names = {n: net.name for n, net in pcb.nets.items()}
    by_status = {e.status: e for e in _changes(result)}
    changed, added, removed = by_status["changed"], by_status["added"], by_status["removed"]
    assert changed.kind == "segment"
    assert changed.net == names[_net_of(moved_block)]
    assert changed.layer == re.search(r'\(layer "([^"]+)"\)', moved_block).group(1)
    assert (added.kind, added.net, added.layer) == ("via", "GND", "F.Cu-B.Cu")
    assert removed.kind == "segment"
    assert removed.net == names[_net_of(deleted_block)]
    assert removed.layer == re.search(r'\(layer "([^"]+)"\)', deleted_block).group(1)


def test_moved_pad_changes_pad_not_footprint(tmp_path: Path):
    text = BOARD01.read_text()
    m = re.search(r'(\t\t\(pad "1" [^\n]*\n\t\t\t\(at )([-\d.]+) ([-\d.]+)', text)
    assert m
    new_text = (
        text[: m.start()] + f"{m.group(1)}{float(m.group(2)) + 0.3} {m.group(3)}" + text[m.end() :]
    )
    changes = _changes(_diff(_copy(tmp_path), _write(tmp_path, new_text)))
    assert [(e.status, e.kind) for e in changes] == [("changed", "pad")]
    assert changes[0].key.endswith("/1")


def _swap_refs(text: str, a: str = "R1", b: str = "R2") -> str:
    """Swap two footprints' Reference properties (so the parts trade places)."""
    ta, tb = f'"Reference" "{a}"', f'"Reference" "{b}"'
    assert ta in text and tb in text
    return text.replace(ta, "\0").replace(tb, ta).replace("\0", tb)


def test_footprint_swap_reports_both_parts_moved(tmp_path: Path):
    # R1 (148.5, 83) and R2 (148.5, 92) are both R_0805 / 10k.
    changes = _changes(_diff(_copy(tmp_path), _write(tmp_path, _swap_refs(BOARD01.read_text()))))
    fps = {e.key: e for e in changes if e.kind == "footprint"}
    assert set(fps) == {"R1", "R2"}
    assert all(e.status == "changed" for e in fps.values())
    assert fps["R1"].before.geometry["position"] == fps["R2"].after.geometry["position"]
    assert fps["R1"].after.geometry["position"] == fps["R2"].before.geometry["position"]
    pads = {e.key for e in changes if e.kind == "pad"}
    assert pads == {"R1/1", "R1/2", "R2/1", "R2/2"}
    assert {e.status for e in changes} == {"changed"}


def test_same_net_identical_part_swap_is_not_silent(tmp_path: Path, capsys):
    # Put R2 on R1's nets, so the two parts are fully interchangeable: equal
    # footprint, value and pad nets.  Swapping them must still be reported.
    text = BOARD01.read_text()
    r2 = text.index('"Reference" "R2"')
    end = text.index("\n\t)\n", r2)
    r2_block = text[r2:end].replace('(net 3 "GND")', '(net 2 "VOUT")', 1)
    r2_block = r2_block.replace('(net 2 "VOUT")', '(net 1 "VIN")', 1)
    base = text[:r2] + r2_block + text[end:]
    old = _write(tmp_path, base, name="old.kicad_pcb")
    new = _write(tmp_path, _swap_refs(base))
    changes = _changes(_diff(old, new))
    assert {(e.kind, e.key) for e in changes if e.kind == "footprint"} == {
        ("footprint", "R1"),
        ("footprint", "R2"),
    }
    assert {e.status for e in changes} == {"changed"}
    assert run_pcb_diff(str(old), str(new)) == 2
    capsys.readouterr()


# ---------------------------------------------------------------------------
# duplicate reference keys (J1/SH x4, REF**): content breaks the tie
# ---------------------------------------------------------------------------

_SH_PAD = re.compile(r'^\t\t\(pad "SH" .*?^\t\t\)\n', re.M | re.S)


def _sh_pads(text: str) -> list[re.Match[str]]:
    pads = list(_SH_PAD.finditer(text))
    assert len(pads) == 4, "board 03's J1 (USB-C) has four SH shield pads"
    return pads


def test_reordered_duplicate_pads_report_nothing(tmp_path: Path):
    text = BOARD03.read_text()
    first, *_, last = _sh_pads(text)
    assert first.group(0) != last.group(0)
    reordered = (
        text[: first.start()]
        + last.group(0)
        + text[first.end() : last.start()]
        + first.group(0)
        + text[last.end() :]
    )
    assert reordered != text
    result = _diff(_copy(tmp_path, BOARD03), _write(tmp_path, reordered))
    assert _changes(result) == []


def test_deleting_one_duplicate_pad_removes_exactly_that_pad(tmp_path: Path):
    text = BOARD03.read_text()
    first = _sh_pads(text)[0]
    old = _copy(tmp_path, BOARD03)
    old_pads = [e.before for e in _diff(old, old).entries if e.kind == "pad" and e.key == "J1/SH"]
    changes = _changes(_diff(old, _write(tmp_path, text[: first.start()] + text[first.end() :])))
    assert [(e.status, e.kind, e.key) for e in changes] == [("removed", "pad", "J1/SH")]
    # J1 sits at (149.475, 63.3) rotated 180, so the first SH pad, local
    # (-1.35, 0.98), lands at (150.825, 62.32); it is listed first in OLD.
    assert changes[0].before.content == old_pads[0].content
    assert changes[0].before.geometry["position"] == pytest.approx((150.825, 62.32))


def _ref_fp(x: float, ref: str = "REF**"):
    return _item(kind="footprint", identity=ref, content=("MountingHole", (x, 0.0)), net="")


def test_deleting_first_of_duplicate_references_removes_that_one():
    old = [_ref_fp(0.0), _ref_fp(10.0), _ref_fp(20.0)]
    changes = diff_items(old, old[1:]).changes()
    assert [(e.status, e.key) for e in changes] == [("removed", "REF**")]
    assert changes[0].before.content == ("MountingHole", (0.0, 0.0))


def test_reordered_duplicate_references_report_nothing():
    old = [_ref_fp(0.0), _ref_fp(10.0), _ref_fp(20.0)]
    assert not diff_items(old, old[::-1]).has_changes


def test_duplicate_references_pair_leftovers_in_order_as_changed():
    # One of three REF** holes moved: the two exact matches stay unchanged
    # and only the moved one is changed, not a cascade along file order.
    old = [_ref_fp(0.0), _ref_fp(10.0), _ref_fp(20.0)]
    new = [_ref_fp(0.0), _ref_fp(15.0), _ref_fp(20.0)]
    changes = diff_items(old, new).changes()
    assert [(e.status, e.before.content[1], e.after.content[1]) for e in changes] == [
        ("changed", (10.0, 0.0), (15.0, 0.0))
    ]


# ---------------------------------------------------------------------------
# filters and exit codes
# ---------------------------------------------------------------------------


def test_net_filter_reports_only_that_net(tmp_path: Path, capsys):
    new_text, _ = _reroute_net(BOARD01.read_text(), net=1)
    old, new = _copy(tmp_path), _write(tmp_path, new_text)
    assert run_pcb_diff(str(old), str(new), fmt="json", nets=["VIN"]) == 2
    doc = json.loads(capsys.readouterr().out)
    assert {i["net"] for i in doc["items"]} == {"VIN"}
    assert set(doc["by_net"]) == {"VIN"}
    assert doc["filters"] == {"layer": [], "net": ["VIN"]}

    # An unrelated net filter sees nothing and exits 0.
    assert run_pcb_diff(str(old), str(new), fmt="json", nets=["VOUT"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["items"] == []
    assert doc["summary"]["removed"] == doc["summary"]["added"] == 0
    assert doc["summary"]["unchanged"] > 0


def test_layer_filter_excludes_other_layers(tmp_path: Path, capsys):
    new_text, _ = _reroute_net(BOARD01.read_text(), net=1)
    old, new = _copy(tmp_path), _write(tmp_path, new_text)
    assert run_pcb_diff(str(old), str(new), fmt="json", layers=["B.Cu"]) == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc["items"], "the fresh B.Cu segment must be reported"
    assert all("B.Cu" in i["layer"] for i in doc["items"])
    assert doc["summary"]["added"] == 1  # only one of the two new segments is on B.Cu


def test_via_matches_every_layer_in_its_span():
    entries = _diff(BOARD01, BOARD01).filtered(layers=["B.Cu"]).entries
    assert any(e.kind == "via" for e in entries)


def test_exit_codes(tmp_path: Path, capsys):
    old = _copy(tmp_path)
    assert run_pcb_diff(str(old), str(old)) == 0
    new = _write(tmp_path, _mixed(BOARD01.read_text()))
    assert run_pcb_diff(str(old), str(new)) == 2
    bad = _write(tmp_path, "(kicad_pcb (version", name="bad.kicad_pcb")
    assert run_pcb_diff(str(old), str(bad)) == 1
    assert "cannot load" in capsys.readouterr().err
    assert run_pcb_diff(str(old), str(tmp_path / "missing.kicad_pcb")) == 1


def test_cli_entry_point(tmp_path: Path, capsys):
    new = _write(tmp_path, _mixed(BOARD01.read_text()))
    code = kct_main(["pcb", "diff", str(_copy(tmp_path)), str(new), "--format", "json"])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["summary"]["changed"] == 1


# ---------------------------------------------------------------------------
# REV:path
# ---------------------------------------------------------------------------


def test_rev_path_side(tmp_path: Path, monkeypatch, capsys):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    board = repo / "board.kicad_pcb"
    shutil.copyfile(BOARD01, board)
    env = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), *env, "commit", "-qm", "r1"], check=True)
    (repo / "notes.txt").write_text("r2")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), *env, "commit", "-qm", "r2"], check=True)
    board.write_text(_mixed(BOARD01.read_text()))
    monkeypatch.chdir(repo)

    assert run_pcb_diff("HEAD~1:board.kicad_pcb", "board.kicad_pcb", fmt="json") == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc["old"] == {"file": "board.kicad_pcb", "spec": "HEAD~1:board.kicad_pcb"}
    assert doc["summary"]["changed"] == 1

    assert run_pcb_diff("HEAD~1:board.kicad_pcb", "HEAD:board.kicad_pcb") == 0
    capsys.readouterr()

    assert run_pcb_diff("nosuchrev:board.kicad_pcb", "board.kicad_pcb") == 1
    assert "unknown git revision 'nosuchrev'" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# JSON and SVG
# ---------------------------------------------------------------------------


def test_json_is_sorted_and_byte_stable(tmp_path: Path, capsys):
    old = _copy(tmp_path)
    new = _write(tmp_path, _mixed(BOARD01.read_text()))
    run_pcb_diff(str(old), str(new), fmt="json")
    first = capsys.readouterr().out
    run_pcb_diff(str(old), str(new), fmt="json")
    second = capsys.readouterr().out
    assert first == second
    doc = json.loads(first)
    assert first.rstrip("\n") == json.dumps(doc, indent=2, sort_keys=True)
    assert set(doc) == {
        "by_kind",
        "by_layer",
        "by_net",
        "filters",
        "items",
        "new",
        "old",
        "summary",
    }
    for item in doc["items"]:
        assert set(item) == {"after", "before", "key", "kind", "layer", "net", "status"}
        assert (item["before"] is None) == (item["status"] == "added")
        assert (item["after"] is None) == (item["status"] == "removed")
        for side in (item["before"], item["after"]):
            assert side is None or not any(k.startswith("_") for k in side)


def test_to_json_counts_cover_unchanged():
    result = _diff(BOARD01, BOARD01)
    doc = to_json(result)
    assert doc["summary"]["unchanged"] == len(result.entries)
    assert doc["by_kind"]["segment"]["unchanged"] == 14


def test_svg_overlay_classes(tmp_path: Path, capsys):
    old = _copy(tmp_path)
    new = _write(tmp_path, _mixed(BOARD01.read_text()))
    out = tmp_path / "overlay.svg"
    assert run_pcb_diff(str(old), str(new), svg=str(out)) == 2
    capsys.readouterr()
    root = ET.parse(out).getroot()
    classes: set[str] = set()
    for el in root.iter():
        classes.update((el.get("class") or "").split())
    assert {"removed", "added", "changed", "unchanged", "before", "edge-cuts"} <= classes
    assert root.get("viewBox")


# ---------------------------------------------------------------------------
# arcs
# ---------------------------------------------------------------------------


def test_arc_reversed_endpoints_unchanged(tmp_path: Path):
    text = ARC.read_text()
    swapped = text.replace(
        "(start 10 10) (mid 15 5) (end 20 10)", "(start 20 10) (mid 15 5) (end 10 10)"
    )
    assert swapped != text
    result = _diff(_copy(tmp_path, ARC), _write(tmp_path, swapped))
    assert _changes(result) == []


def test_arc_moved_mid_same_uuid_changed(tmp_path: Path):
    text = ARC.read_text()
    moved = text.replace("(mid 15 5)", "(mid 15 4)")
    assert moved != text
    changes = _changes(_diff(_copy(tmp_path, ARC), _write(tmp_path, moved)))
    assert [(e.status, e.kind) for e in changes] == [("changed", "arc")]
    assert changes[0].before.geometry["mid"] == (15.0, 5.0)
