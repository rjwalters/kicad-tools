"""Issue #5864: an unsatisfiable auto-pour degrades instead of aborting the run.

``kct route`` runs an internal auto-pour (``--auto-pour``, the default).
On the normalized Arduino Leonardo (``srj18_arduino_leonardo``) that pour
hit a net -- ``AGND`` on ``B.Cu`` -- whose pad region is fully covered by
the higher-priority ``GND`` pour the board already carries, so the outline
allocator could not derive a non-empty disjoint outline for it.  It raised
:class:`~kicad_tools.zones.generator.ZonePartitionError`, nothing caught it
between ``_compute_pour_outlines`` and the CLI, and the whole route aborted
with exit 1 and **no board written** after ~28 s of work.

The fix is scoped to ``kct route``'s auto-pour path: it opts into a
degrade-gracefully mode where an unsatisfiable net is skipped (no zone, so
the router routes it as ordinary traces), the skip is reported as a
warning, and every other pour net still gets its zone.  ``kct zones`` and
``kct build`` keep the hard, actionable ``ZonePartitionError`` -- those
callers are asserting an authoritative pour and a silent skip would be the
wrong answer there (that contract is #3240's and is asserted below too).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from kicad_tools.router.net_class import NetClass
from kicad_tools.schema.pcb import PCB
from kicad_tools.zones import ZonePartitionError

# --------------------------------------------------------------------------
# Fixture board
# --------------------------------------------------------------------------

_HEADER = """\
(kicad_pcb
  (version 20240108)
  (generator "kicad")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "AGND")
  (net 3 "+5V")
  (net 4 "SIG1")
  (net 5 "SIG2")
  (gr_rect
    (start 0 0)
    (end 60 40)
    (stroke (width 0.15) (type solid))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "edge-uuid")
  )
"""


def _footprint(ref: str, x: float, y: float, pads: list[tuple[str, int, str]]) -> str:
    """Render a footprint with one SMD pad per ``(pad_name, net_num, net_name)``."""
    pad_lines = "\n".join(
        f'    (pad "{name}" smd rect (at {i * 0.0} 0) (size 1 1) '
        f'(layers "F.Cu") (net {num} "{net}"))'
        for i, (name, num, net) in enumerate(pads)
    )
    return f"""  (footprint "Test:{ref}"
    (layer "F.Cu")
    (at {x} {y})
    (uuid "fp-{ref}-uuid")
    (property "Reference" "{ref}"
      (at 0 -2 0) (layer "F.SilkS") (uuid "{ref}-ref-uuid")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (property "Value" "T"
      (at 0 2 0) (layer "F.Fab") (uuid "{ref}-val-uuid")
      (effects (font (size 1 1) (thickness 0.15)))
    )
{pad_lines}
  )"""


def _authored_zone(uuid: str, net_num: int, net_name: str, layer: str, priority: int) -> str:
    """A hand-authored full-board pour, as a real board carries."""
    return f"""  (zone
    (net {net_num})
    (net_name "{net_name}")
    (layer "{layer}")
    (uuid "{uuid}")
    (hatch edge 0.5)
    (priority {priority})
    (connect_pads (clearance 0.4))
    (min_thickness 0.25)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon
      (pts (xy 1 1) (xy 59 1) (xy 59 39) (xy 1 39))
    )
  )"""


def _write_split_ground_board(path: Path) -> Path:
    """A 2-layer board whose AGND pour cannot be partitioned from GND.

    Two ground domains (``GND``/``AGND``) land on ``B.Cu`` together --
    the only ground layer a 2-layer stackup has -- and their pads are
    **coincident**, so no carve, pad-safe fallback or numerical-slack
    retry can hand ``AGND`` any exclusive copper.  That is the Leonardo
    failure shape in miniature.

    The board also carries an **authored zone pair** (``+5V`` on both
    ``F.Cu`` and ``B.Cu``), so the #5590 incumbent-occupied-layer path
    runs: ``B.Cu`` counts as shared and the new ground pours are
    staggered above the incumbent's priority.
    """
    rows = [
        # GND and AGND pads at the SAME location -> no partition exists.
        _footprint("U1", 30, 20, [("1", 1, "GND")]),
        _footprint("U2", 30, 20, [("1", 2, "AGND")]),
        # Power + signal pads elsewhere so the board is not all-power
        # (the all-power guard would otherwise suppress the pour entirely).
        _footprint("U3", 10, 10, [("1", 3, "+5V")]),
        _footprint("U4", 50, 10, [("1", 4, "SIG1")]),
        _footprint("U5", 50, 30, [("1", 5, "SIG2")]),
        _footprint("U6", 10, 30, [("1", 4, "SIG1")]),
        _footprint("U7", 20, 30, [("1", 5, "SIG2")]),
    ]
    zones = [
        _authored_zone("zone-5v-fcu", 3, "+5V", "F.Cu", 0),
        _authored_zone("zone-5v-bcu", 3, "+5V", "B.Cu", 0),
    ]
    path.write_text(_HEADER + "\n".join(rows) + "\n" + "\n".join(zones) + "\n)\n")
    return path


@pytest.fixture
def split_ground_board(tmp_path: Path) -> Path:
    return _write_split_ground_board(tmp_path / "unsatisfiable_pour.kicad_pcb")


# --------------------------------------------------------------------------
# Unit level: the generator's opt-in skip mode
# --------------------------------------------------------------------------


class TestGeneratorSkipMode:
    """``auto_create_zones_for_pour_nets`` degrades only when asked to."""

    POUR_NETS = [
        ("GND", NetClass.GROUND),
        ("AGND", NetClass.GROUND),
        ("+5V", NetClass.POWER),
    ]

    def test_raises_by_default(self, split_ground_board: Path) -> None:
        """Without ``skipped_out`` the #3240 hard-error contract is unchanged."""
        from kicad_tools.zones.generator import auto_create_zones_for_pour_nets

        with pytest.raises(ZonePartitionError) as exc_info:
            auto_create_zones_for_pour_nets(split_ground_board, self.POUR_NETS)

        assert exc_info.value.failing_net == "AGND"
        assert exc_info.value.layer == "B.Cu"
        assert "GND" in exc_info.value.covering_nets

    def test_skipped_out_records_and_continues(self, split_ground_board: Path) -> None:
        """With ``skipped_out`` the failing net is recorded, the rest still pour."""
        from kicad_tools.zones.generator import auto_create_zones_for_pour_nets

        skipped: list[ZonePartitionError] = []
        count = auto_create_zones_for_pour_nets(
            split_ground_board, self.POUR_NETS, skipped_out=skipped
        )

        assert [err.failing_net for err in skipped] == ["AGND"]
        assert skipped[0].layer == "B.Cu"
        assert "GND" in skipped[0].covering_nets

        # The satisfiable nets still got their zones, and AGND got none.
        assert count > 0
        pcb = PCB.load(str(split_ground_board))
        poured = {z.net_name for z in pcb.zones}
        assert "GND" in poured, "the satisfiable ground pour must still be created"
        assert "AGND" not in poured, (
            "the skipped net must NOT fall through to a full-board outline -- "
            "that is the silent zero-copper overlap the allocator refused to emit"
        )

    def test_no_spurious_skips_on_a_satisfiable_board(self, tmp_path: Path) -> None:
        """A board whose pours all partition cleanly records nothing."""
        from kicad_tools.zones.generator import auto_create_zones_for_pour_nets

        path = tmp_path / "satisfiable.kicad_pcb"
        rows = [
            _footprint("U1", 10, 20, [("1", 1, "GND")]),
            _footprint("U2", 50, 20, [("1", 3, "+5V")]),
            _footprint("U3", 30, 10, [("1", 4, "SIG1")]),
            _footprint("U4", 30, 30, [("1", 5, "SIG2")]),
        ]
        path.write_text(_HEADER + "\n".join(rows) + "\n)\n")

        skipped: list[ZonePartitionError] = []
        count = auto_create_zones_for_pour_nets(
            path,
            [("GND", NetClass.GROUND), ("+5V", NetClass.POWER)],
            skipped_out=skipped,
        )

        assert skipped == [], f"no pour should have been skipped, got {skipped}"
        assert count == 2


# --------------------------------------------------------------------------
# Router entry point: auto_pour_if_missing warns instead of raising
# --------------------------------------------------------------------------


class TestAutoPourDegrades:
    def test_auto_pour_warns_and_continues(
        self, split_ground_board: Path, capsys: pytest.CaptureFixture
    ) -> None:
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        count, names = auto_pour_if_missing(split_ground_board)

        out = capsys.readouterr()
        combined = out.out + out.err
        assert "skipped pour for 'AGND'" in combined, (
            f"the skipped pour must be reported to the user; got:\n{combined}"
        )
        assert "B.Cu" in combined

        # The skipped net is not reported as having received a zone.
        assert "AGND" not in names
        assert count == len(names)
        assert "GND" in names, "the satisfiable ground pour must still be created"

    def test_skipped_net_is_left_for_the_router(self, split_ground_board: Path) -> None:
        """No zone for the skipped net, so the router routes it as traces."""
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        auto_pour_if_missing(split_ground_board, quiet=True)

        pcb = PCB.load(str(split_ground_board))
        assert not [z for z in pcb.zones if z.net_name == "AGND"]


# --------------------------------------------------------------------------
# End-to-end: `kct route` completes instead of aborting
# --------------------------------------------------------------------------


class TestRouteCompletes:
    def test_kct_route_completes_and_reports_the_skipped_pour(
        self, split_ground_board: Path, tmp_path: Path
    ) -> None:
        """Issue #5864 acceptance criterion.

        Before the fix this exited 1 with an unhandled ``ZonePartitionError``
        and wrote no output board at all.
        """
        out_path = tmp_path / "routed.kicad_pcb"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "kicad_tools.cli",
                "route",
                str(split_ground_board),
                "-o",
                str(out_path),
                "--layers",
                "2",
                "--grid",
                "auto",
                # The fixture deliberately stacks the GND and AGND pads at
                # one point to make the partition unsatisfiable, which also
                # makes the board un-manufacturable.  Post-routing DRC is
                # not what this test is about -- #5864 is about the run
                # aborting *before* any routing happened.
                "--skip-drc",
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
        combined = proc.stdout + proc.stderr

        assert "ZonePartitionError" not in combined, (
            f"kct route must not abort on an unsatisfiable pour:\n{combined}"
        )
        assert proc.returncode == 0, f"kct route exited {proc.returncode}:\n{combined}"
        assert out_path.exists(), f"kct route wrote no board:\n{combined}"
        assert "skipped pour for 'AGND'" in combined, (
            f"kct route must report the skipped pour:\n{combined}"
        )
