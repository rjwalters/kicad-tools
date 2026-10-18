"""Retune selected groups/pairs on an already-routed PCB (Issue #6885).

Small synthetic boards only: no full-board regeneration, no compute launch.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.pcb_retune import RetuneError, retune_pcb
from kicad_tools.router.rules import NetClassRouting, net_class_map_to_dict

ADDR = {"A0": (10, 5, 45), "A1": (14, 5, 41), "A2": (18, 5, 39.2), "A3": (22, 5, 38)}
ZONE = (
    '  (zone (net 0) (net_name "") (layer "B.Cu") (uuid "cccccccc-0000-0000-0000-000000000001") '
    "(hatch edge 0.5) (polygon (pts (xy 1 1) (xy 79 1) (xy 79 49) (xy 1 49))))\n"
)


def board(
    nets: dict[str, tuple[float, float, float]],
    *,
    thickness: bool = True,
    extra: str = "",
    locked: str | None = None,
) -> str:
    """A routed 80x50 board: one straight F.Cu track per net, pad to pad."""
    ids = {n: i + 1 for i, n in enumerate(nets)}
    out = [
        "(kicad_pcb",
        "  (version 20240108)",
        '  (generator "test")',
        "  (general (thickness 1.6))" if thickness else "  (general)",
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))',
        "  (setup (pad_to_mask_clearance 0))",
        '  (net 0 "")',
    ]
    out += [f'  (net {i} "{n}")' for n, i in ids.items()]
    out.append(
        "  (gr_rect (start 0 0) (end 80 50) (stroke (width 0.1) (type default)) "
        '(fill none) (layer "Edge.Cuts"))'
    )
    k = 0
    for n, (y, x0, x1) in nets.items():
        for side, x in (("A", x0), ("B", x1)):
            k += 1
            out.append(
                f'  (footprint "R" (layer "F.Cu") (uuid "00000000-0000-0000-0000-{k:012d}") '
                f"(at {x} {y}) "
                f'(property "Reference" "{side}{ids[n]}" (at 0 0 0) (layer "F.SilkS")) '
                f'(property "Value" "x" (at 0 0 0) (layer "F.Fab")) '
                f'(pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask") '
                f'(net {ids[n]} "{n}")))'
            )
    out.append(ZONE.rstrip("\n"))
    for n, (y, x0, x1) in nets.items():
        out.append(
            f'  (segment (start {x0} {y}) (end {x1} {y}) (width 0.2) (layer "F.Cu") '
            f'(net {ids[n]}) (uuid "aaaaaaaa-0000-0000-0000-{ids[n]:012d}"))'
        )
    if locked:
        out.append(locked)
    if extra:
        out.append(extra)
    out.append(")")
    return "\n".join(out) + "\n"


def addr_map(tolerance: float = 0.5, nets=ADDR) -> dict[str, NetClassRouting]:
    nc = NetClassRouting(
        name="ADDR",
        length_match_group="ADDR_BUS",
        length_match_tolerance_mm=tolerance,
        length_critical=True,
    )
    return dict.fromkeys(nets, nc)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def src(tmp_path: Path) -> Path:
    path = tmp_path / "in.kicad_pcb"
    path.write_text(board(ADDR | {"OTHER": (30, 5, 40)}))
    return path


def run(src: Path, name: str = "out.kicad_pcb", **kw):
    kw.setdefault("groups", ["ADDR_BUS"])
    kw.setdefault("net_class_map", addr_map())
    return retune_pcb(src, src.parent / name, **kw)


def assert_refused(src: Path, code: str, **kw):
    before = digest(src)
    with pytest.raises(RetuneError) as err:
        run(src, **kw)
    assert err.value.code == code
    assert digest(src) == before, "input must be untouched"
    assert not (src.parent / "out.kicad_pcb").exists()
    assert not (src.parent / "out.kicad_pcb.retune.json").exists()
    return err.value


class TestTune:
    def test_group_reaches_checker_limit_and_input_is_untouched(self, src):
        before = digest(src)
        report = run(src)
        assert digest(src) == before
        out = src.parent / "out.kicad_pcb"
        assert out.is_file() and digest(out) == report.output_sha256
        (target,) = report.targets
        assert target.name == "ADDR_BUS" and target.kind == "match_group"
        assert target.before_skew_mm == pytest.approx(7.0)
        assert target.after_skew_mm <= 0.5 and target.achieved
        assert target.tolerance_mm == 0.5
        assert target.tolerance_source == "class:ADDR.length_match_tolerance_mm"
        assert report.status == "tuned"
        assert report.changed_nets == ["A1", "A2", "A3"]

    def test_achieved_is_remeasured_from_the_written_board(self, src):
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.match_group_skew import derive_group_skew_data

        report = run(src)
        pcb = PCB.load(str(src.parent / "out.kicad_pcb"))
        skew, _g, _t = derive_group_skew_data(pcb, addr_map())
        assert skew["ADDR_BUS"] == pytest.approx(report.targets[0].after_skew_mm)

    def test_provenance_record(self, src):
        report = run(src)
        prov = json.loads((src.parent / "out.kicad_pcb.retune.json").read_text())
        assert prov["input_sha256"] == digest(src) == report.input_sha256
        assert prov["output_sha256"] == digest(src.parent / "out.kicad_pcb")
        assert prov["settings"]["selected_groups"] == ["ADDR_BUS"]
        assert prov["settings"]["uuid_seed"] == 6885
        assert prov["tool_version"]
        lengths = prov["net_lengths_mm"]
        assert lengths["A3"]["before"] == pytest.approx(33.0)
        assert lengths["A3"]["after"] > lengths["A3"]["before"]
        assert "OTHER" not in lengths
        assert prov["changed_nets"] == ["A1", "A2", "A3"]

    def test_repeatable(self, src):
        a = run(src, "one.kicad_pcb")
        b = run(src, "two.kicad_pcb")
        assert a.output_sha256 == b.output_sha256
        assert digest(src.parent / "one.kicad_pcb") == digest(src.parent / "two.kicad_pcb")

    def test_already_satisfied_is_an_honest_noop(self, src):
        report = run(src, net_class_map=addr_map(tolerance=10.0))
        assert report.status == "already_satisfied"
        assert report.targets[0].status == "already_satisfied"
        assert report.changed_nets == []
        assert digest(src.parent / "out.kicad_pcb") == digest(src)

    def test_name_only_board_is_tuned(self, tmp_path):
        """KiCad 10 boards reference nets by NAME on copper (Issue #5798).

        The routing loader files name-only copper under net 0, which hid every
        selected member from the tuner: board 07's committed routed artifact
        (KiCad 10, name-only) came back ``unattainable`` with every group
        reported "not tuned (a member is unrouted)" and zero copper changed.
        """
        nets = ADDR | {"OTHER": (30, 5, 40)}
        ids = {n: i + 1 for i, n in enumerate(nets)}
        text = board(nets).replace(ZONE.rstrip("\n") + "\n", "")
        # KiCad 10 ``--save-board`` shape: no header net table at all.
        text = text.replace("(version 20240108)", "(version 20260206)")
        text = text.replace('  (net 0 "")\n', "")
        for name, i in ids.items():
            text = text.replace(f'  (net {i} "{name}")\n', "")
            text = text.replace(f'(net {i} "{name}")', f'(net "{name}")')
            text = text.replace(f"(net {i}) (uuid", f'(net "{name}") (uuid')
        assert not any(f"(net {i}" in text for i in ids.values()), "fixture must be name-only"
        path = tmp_path / "in.kicad_pcb"
        path.write_text(text)
        report = run(path)
        (target,) = report.targets
        assert target.before_skew_mm == pytest.approx(7.0)
        assert target.achieved and target.after_skew_mm <= 0.5
        assert report.changed_nets == ["A1", "A2", "A3"]
        out = (tmp_path / "out.kicad_pcb").read_text()
        # New copper keeps the board's own dialect: no numeric net references.
        assert not any(f"(net {i})" in out for i in ids.values())

    def test_diffpair_reports_gate_and_stretch_separately(self, tmp_path):
        nets = {"USB_D+": (10, 5, 45), "USB_D-": (11, 5, 41.5)}
        path = tmp_path / "usb.kicad_pcb"
        path.write_text(board(nets))
        nc = NetClassRouting(name="USB", skew_tolerance_mm=2.5, length_critical=True)
        report = retune_pcb(
            path,
            tmp_path / "usb_out.kicad_pcb",
            diffpairs=[("USB_D+", "USB_D-")],
            net_class_map=dict.fromkeys(nets, nc),
        )
        (target,) = report.targets
        assert target.kind == "diffpair"
        assert target.before_skew_mm == pytest.approx(3.5)
        assert target.tolerance_mm == 2.5
        assert target.after_skew_mm <= 2.5 and target.achieved
        assert target.stretch_target_mm == 0.5
        assert target.stretch_achieved == (target.after_skew_mm <= 0.5)


class TestMixedGroup:
    def test_single_ended_plus_pair_member_group(self, tmp_path):
        nets = {"CLK": (10, 5, 45), "D0_P": (20, 5, 41), "D0_N": (21, 5, 41.4)}
        path = tmp_path / "in.kicad_pcb"
        path.write_text(board(nets))
        nc = NetClassRouting(
            name="LANES",
            length_match_group="LANES",
            length_match_tolerance_mm=0.5,
            length_critical=True,
        )
        report = retune_pcb(
            path,
            tmp_path / "out.kicad_pcb",
            groups=["LANES"],
            net_class_map=dict.fromkeys(nets, nc),
        )
        (target,) = report.targets
        assert target.before_skew_mm == pytest.approx(3.8)
        assert target.achieved and target.after_skew_mm <= 0.5
        assert report.changed_nets == ["D0_N", "D0_P"]  # the reference CLK is untouched


class TestGates:
    """A successful retune of the fixture passes both checkers (not a board receipt)."""

    def test_kct_check_reports_no_drc_errors(self, src):
        run(src)
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        pcb = PCB.load(str(src.parent / "out.kicad_pcb"))
        results = DRCChecker(pcb, manufacturer="jlcpcb", layers=2).check_all()
        assert results.error_count == 0

    def test_kicad_cli_drc_refill_zones_is_clean(self, src):
        import shutil
        import subprocess

        cli = shutil.which("kicad-cli")
        if cli is None:
            pytest.skip("kicad-cli not installed")
        run(src)
        report = src.parent / "drc.json"
        subprocess.run(
            [
                cli,
                "pcb",
                "drc",
                "--refill-zones",
                "--severity-error",
                "--format",
                "json",
                "-o",
                str(report),
                str(src.parent / "out.kicad_pcb"),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        doc = json.loads(report.read_text())
        assert doc["violations"] == [] and doc["unconnected_items"] == []


class TestPreservation:
    def test_unselected_geometry_and_everything_else_is_byte_identical(self, tmp_path):
        manual = (
            '  (segment (start 5 40) (end 30 40) (width 0.25) (layer "B.Cu") (locked yes) '
            '(net 5) (uuid "bbbbbbbb-0000-0000-0000-000000000005"))'
        )
        path = tmp_path / "in.kicad_pcb"
        path.write_text(board(ADDR | {"OTHER": (30, 5, 40)}, locked=manual))
        run(path)
        old = path.read_text().splitlines()
        new = (tmp_path / "out.kicad_pcb").read_text().splitlines()

        def tuned_copper(line: str) -> bool:
            return line.lstrip().startswith("(segment") and any(
                f"(net {n})" in line for n in (2, 3, 4)
            )

        # Everything the retune did not own survives, in order.
        kept = [line for line in old if not tuned_copper(line)]
        assert [line for line in new if line in set(kept)] == kept
        # Unselected copper (net 5), zone, footprints, outline: unchanged lines.
        for needle in ("(net 5)", "(zone", "(footprint", "gr_rect", "(locked yes)"):
            assert [ln for ln in old if needle in ln] == [ln for ln in new if needle in ln]
        # Selected nets lose nothing but what they were tuned from.
        assert any("(locked yes)" in ln for ln in new)

    def test_unchanged_selected_copper_keeps_its_original_text(self, src):
        run(src)
        # A0 is the reference (longest); its authored block, uuid included, survives.
        original = [line for line in src.read_text().splitlines() if "(net 1)" in line]
        assert original
        out_lines = (src.parent / "out.kicad_pcb").read_text().splitlines()
        assert all(line in out_lines for line in original)

    def test_tuning_sees_unselected_copper_as_a_fixed_obstacle(self, tmp_path):
        """A blocker on the bump side must stop the meander, not be crossed."""
        nets = ADDR | {"WALL": (24, 5, 60)}
        # A3 (y=22) is the shortest; its bumps go to one side.  WALL hugs y=24.
        path = tmp_path / "in.kicad_pcb"
        path.write_text(board(nets))
        report = run(path, net_class_map=addr_map(nets=ADDR))
        assert "WALL" not in report.changed_nets
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(tmp_path / "out.kicad_pcb"))
        wall = next(i for i, n in pcb.nets.items() if n.name == "WALL")
        wall_y = {s.start[1] for s in pcb.segments_in_net(wall)}
        assert wall_y == {24.0}
        a3 = next(i for i, n in pcb.nets.items() if n.name == "A3")
        for seg in pcb.segments_in_net(a3):
            for y in (seg.start[1], seg.end[1]):
                assert abs(y - 24.0) >= 0.2 + 0.2 - 1e-6  # width/2 + width/2 + clearance floor


class TestRefusals:
    def test_unknown_group(self, src):
        err = assert_refused(src, "unknown_group", groups=["NOPE"])
        assert "ADDR_BUS" in err.message

    def test_unknown_diffpair(self, src):
        assert_refused(src, "unknown_diffpair", groups=[], diffpairs=[("A0", "A1")])

    def test_no_selection_means_no_whole_board_fallback(self, src):
        assert_refused(src, "no_selection", groups=[])

    def test_missing_rules(self, src):
        assert_refused(src, "missing_rules", net_class_map=None)
        assert_refused(src, "missing_rules", net_class_map={})

    def test_missing_stackup_thickness(self, tmp_path):
        path = tmp_path / "in.kicad_pcb"
        path.write_text(board(ADDR, thickness=False))
        assert_refused(path, "missing_stackup")

    def test_selected_net_with_arc_copper_is_unsupported(self, tmp_path):
        arc = (
            '  (arc (start 5 10) (mid 6 11) (end 7 10) (width 0.2) (layer "F.Cu") (net 1) '
            '(uuid "dddddddd-0000-0000-0000-000000000001"))'
        )
        path = tmp_path / "in.kicad_pcb"
        path.write_text(board(ADDR, extra=arc))
        assert_refused(path, "unsupported_pcb")

    def test_selected_net_must_be_routed(self, tmp_path):
        path = tmp_path / "in.kicad_pcb"
        text = board(ADDR)
        path.write_text("\n".join(ln for ln in text.splitlines() if "(net 4) (uuid" not in ln))
        before = digest(path)
        with pytest.raises(RetuneError) as err:
            run(path)
        assert err.value.code in {"no_geometry", "unmeasurable"}
        assert digest(path) == before

    def test_output_may_not_be_the_input(self, src):
        before = digest(src)
        with pytest.raises(RetuneError) as err:
            retune_pcb(src, src, groups=["ADDR_BUS"], net_class_map=addr_map())
        assert err.value.code == "output_is_input"
        assert digest(src) == before

    def test_existing_output_is_never_overwritten(self, src):
        existing = src.parent / "out.kicad_pcb"
        existing.write_text("committed fixture")
        with pytest.raises(RetuneError) as err:
            run(src)
        assert err.value.code == "output_exists"
        assert existing.read_text() == "committed fixture"


class TestFailureLeavesNothing:
    def test_unattainable_target_writes_no_output(self, src, monkeypatch):
        monkeypatch.setattr(Autorouter, "apply_match_group_tuning", lambda self, *a, **k: {})
        err = assert_refused(src, "unattainable")
        assert "ADDR_BUS" in err.message and "7.0000" in err.message

    def test_timeout_writes_no_output(self, src, monkeypatch):
        monkeypatch.setattr(
            Autorouter, "apply_match_group_tuning", lambda self, *a, **k: time.sleep(5) or {}
        )
        started = time.monotonic()
        assert_refused(src, "timeout", timeout_s=0.2)
        assert time.monotonic() - started < 4

    def test_tuner_exception_writes_no_output(self, src, monkeypatch):
        def boom(self, *a, **k):
            raise RuntimeError("tuner exploded")

        monkeypatch.setattr(Autorouter, "apply_match_group_tuning", boom)
        before = digest(src)
        with pytest.raises(RuntimeError):
            run(src)
        assert digest(src) == before
        assert not (src.parent / "out.kicad_pcb").exists()

    def test_scope_violation_is_detected(self, src, monkeypatch):
        """A tuner that edits an unselected net must abort the run."""

        def sneaky(self, groups, **kw):
            other = next(r for r in self.routes if r.net_name == "OTHER")
            other.segments[0].y1 += 0.5
            other.segments[0].y2 += 0.5
            return {}

        monkeypatch.setattr(Autorouter, "apply_match_group_tuning", sneaky)
        assert_refused(src, "scope_violation")


class TestCli:
    def _map_file(self, tmp_path: Path) -> Path:
        path = tmp_path / "ncm.json"
        path.write_text(json.dumps(net_class_map_to_dict(addr_map())))
        return path

    def test_cli_success_json(self, src, tmp_path, capsys):
        from kicad_tools.cli import main

        out = tmp_path / "cli_out.kicad_pcb"
        code = main(
            [
                "retune-lengths",
                str(src),
                "-o",
                str(out),
                "--net-class-map",
                str(self._map_file(tmp_path)),
                "--group",
                "ADDR_BUS",
                "--format",
                "json",
            ]
        )
        assert code == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["ok"] is True and doc["status"] == "tuned"
        assert doc["targets"][0]["achieved"] is True
        assert out.is_file()

    def test_cli_failure_exits_nonzero_without_output(self, src, tmp_path, capsys):
        from kicad_tools.cli import main

        out = tmp_path / "cli_out.kicad_pcb"
        code = main(
            [
                "retune-lengths",
                str(src),
                "-o",
                str(out),
                "--net-class-map",
                str(self._map_file(tmp_path)),
                "--group",
                "NOPE",
            ]
        )
        assert code == 1
        assert "unknown_group" in capsys.readouterr().err
        assert not out.exists()
