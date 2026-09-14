"""Native-parity regression for same-zone fill-fragment bonding (Issue #5362).

Background
----------
#5358's Board03 repair produced a board where native KiCad reported one
``unconnected_items`` result between the R13/R14 VCC supply copper while this
repo's connectivity analyzers reported the board fully connected.  The
responsible defect was in how both analyzers bonded a zone's
``filled_polygon`` fragments to each other:

* :meth:`NetStatusAnalyzer._build_fill_island_groups` accumulated every
  fragment of a ``zone`` object into one pad group, and
* :meth:`ConnectivityValidator._connect_pour_pads_label_free` unioned every
  pad bonded to any fragment of a ``zone``,

both on the premise (stated verbatim in the latter's docstring) that "all
fragments of one ``zone`` are the same net ... DRC guarantees retained
fragments are electrically bonded".  That premise is false.

Native ground truth
-------------------
Measured with ``kicad-cli pcb drc --format json`` under
``kicad/kicad:10.0`` digest ``sha256:182c8005cb77...`` (KiCad **10.0.5**, the
image digest recorded in #5358/#5362), with **no** ``--refill-zones`` and no
``--save-board``; the board SHA256 was identical before and after every run.
Two same-zone fill fragments carrying one pad each, ``min_thickness 0.25``:

=============================  ==============  ==================  =========
``filled_areas_thickness``     ``(version N)``  fragment gap (mm)   unconn.
=============================  ==============  ==================  =========
absent                         <= 20250209     0.0 .. 0.25         0
absent                         <= 20250209     0.26 .. 2.0         1
absent                         >= 20250210     0.0 .. 2.0          1
``no``                         any             0.0 .. 2.0          1
=============================  ==============  ==================  =========

Two things decide it, in KiCad's own resolution order:

1. **Is the fill stroked?**  The parser initialises ``isStrokedFill =
   m_requiredVersion < 20250210`` and then overrides it from an explicit
   token.  So an *absent* ``filled_areas_thickness`` is version-dependent,
   while an explicit ``no`` is always solid.
2. **If stroked**, the stored outline is the centre-line of
   ``min_thickness``-wide copper, so two fragments are one piece of metal
   exactly when their outlines are within ``min_thickness``.  If solid, the
   stored outline *is* the copper and native KiCad bonds no two fill outlines
   of one zone to each other -- not at a shared corner, not along a shared
   edge, and not across a genuinely overlapping band.

In every row a real conductor (a pad, via or track whose copper reaches into
both fragments) does bond them; the version gates fill adjacency only.

The retained #5362 witness is a ``(version 20260206)`` board whose zones omit
the token -- row 3 -- which is why reading "absent" as unconditionally stroked
kept it reporting false-clean.  It is pinned at the bottom of this module.

The fixtures below are that measurement series, one board per row, asserted
against both analyzers.  Each case's ``native_unconnected`` field records the
measured count for the identical bytes this module generates.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from kicad_tools.analysis.net_status import NetStatusAnalyzer
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.connectivity import ConnectivityValidator

pytest.importorskip("shapely")


_BOARD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup)
  (net 0 "")
  (net 1 "GND")
  (gr_line (start 5 5) (end 45 5) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 45 5) (end 45 45) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 45 45) (end 5 45) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 5 45) (end 5 5) (layer "Edge.Cuts") (width 0.05))
  (footprint "R" (layer "F.Cu") (uuid "10000000-0000-0000-0000-000000000001")
    (at {p1x} {p1y})
    (property "Reference" "R13" (at 0 0) (layer "F.Cu")
      (uuid "20000000-0000-0000-0000-000000000001")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "GND")
      (uuid "30000000-0000-0000-0000-000000000001"))
  )
  (footprint "R" (layer "F.Cu") (uuid "10000000-0000-0000-0000-000000000002")
    (at {p2x} {p2y})
    (property "Reference" "R14" (at 0 0) (layer "F.Cu")
      (uuid "20000000-0000-0000-0000-000000000002")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "GND")
      (uuid "30000000-0000-0000-0000-000000000002"))
  )
{extra}  (zone
    (net 1)
    (net_name "GND")
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (hatch edge 0.5)
    (connect_pads (clearance 0))
    (min_thickness {min_thickness})
{fat}    (fill yes (thermal_gap 0.2) (thermal_bridge_width 0.2))
    (polygon (pts (xy 8 8) (xy 42 8) (xy 42 42) (xy 8 42)))
{fills}  )
)
"""

_FAT_NO = "    (filled_areas_thickness no)\n"
_FAT_YES = "    (filled_areas_thickness yes)\n"


def _fill(points: list[tuple[float, float]]) -> str:
    pts = " ".join(f"(xy {x} {y})" for x, y in points)
    return f'    (filled_polygon (layer "F.Cu") (pts {pts}))\n'


def _rect(x0: float, x1: float, y0: float = 20, y1: float = 30) -> list[tuple[float, float]]:
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


@dataclass(frozen=True)
class Case:
    """One measured board: geometry plus its native ``unconnected_items`` count."""

    name: str
    fills: str
    native_unconnected: int
    min_thickness: float = 0.25
    stroke_encoded: bool = False  # True => omit (filled_areas_thickness no)
    # True => write an explicit ``(filled_areas_thickness yes)`` instead of
    # omitting the token.  Takes priority over ``stroke_encoded``.  Native
    # KiCad 10.0.6 measures this identically to the token being absent at
    # every version -- confirmed down to the parser source
    # (``pcb_io_kicad_sexpr_parser.cpp``'s ``T_filled_areas_thickness``
    # handler only ever *clears* ``isStrokedFill``; there is no branch that
    # sets it, so an explicit ``yes`` cannot force-stroke a >= 20250210 file,
    # unlike an earlier revision of :meth:`Zone.is_stroked_fill` assumed;
    # Issue #5382).
    explicit_yes: bool = False
    p1: tuple[float, float] = (12, 25)
    p2: tuple[float, float] = (28, 25)
    extra: str = ""
    # ``(version N)``.  An omitted ``filled_areas_thickness`` means "stroked"
    # only below KiCad's 20250210 boundary, so the version is part of the
    # fixture's identity, not boilerplate (Issue #5362).
    version: int = 20240108

    @property
    def connected(self) -> bool:
        """Native verdict: the two supply pads share one copper component."""
        return self.native_unconnected == 0

    def board(self) -> str:
        if self.explicit_yes:
            fat = _FAT_YES
        elif self.stroke_encoded:
            fat = ""
        else:
            fat = _FAT_NO
        return _BOARD.format(
            p1x=self.p1[0],
            p1y=self.p1[1],
            p2x=self.p2[0],
            p2y=self.p2[1],
            min_thickness=self.min_thickness,
            fat=fat,
            fills=self.fills,
            extra=self.extra,
        ).replace("(version 20240108)", f"(version {self.version})")


_TWO_RECTS_GAP = {
    gap: _fill(_rect(10, 20)) + _fill(_rect(20 + gap, 30))
    for gap in (0.0, 0.05, 0.1, 0.2, 0.24, 0.25, 0.26, 0.3, 0.5, 2.0)
}

# --- the measured series (see module docstring for provenance) ---------------
CASES: list[Case] = [
    # Stroke-encoded fills (KiCad format default): fragments bond iff their
    # gap is within min_thickness (0.25 mm here).
    *[
        Case(f"stroke_gap{gap}", _TWO_RECTS_GAP[gap], 0, stroke_encoded=True)
        for gap in (0.0, 0.05, 0.1, 0.2, 0.24, 0.25)
    ],
    *[
        Case(f"stroke_gap{gap}", _TWO_RECTS_GAP[gap], 1, stroke_encoded=True)
        for gap in (0.26, 0.3, 0.5, 2.0)
    ],
    # (filled_areas_thickness no): the stored outline IS the copper, and no
    # amount of fragment-to-fragment adjacency bonds them.
    *[
        Case(f"solid_gap{gap}", _TWO_RECTS_GAP[gap], 1)
        for gap in (0.0, 0.05, 0.1, 0.2, 0.24, 0.25, 0.26, 0.3, 0.5, 2.0)
    ],
    # Degenerate contacts, solid-encoded: a shared corner and a shared edge
    # are not copper, and neither is a genuinely overlapping band.
    Case(
        "solid_point_touch",
        _fill(_rect(10, 18, 20, 25)) + _fill(_rect(18, 30, 25, 30)),
        1,
        p1=(14, 22),
        p2=(26, 28),
    ),
    Case("solid_edge_touch", _fill(_rect(10, 18)) + _fill(_rect(18, 30)), 1),
    Case("solid_overlap_band", _fill(_rect(10, 19)) + _fill(_rect(17, 30)), 1),
    # Genuinely-connected controls that must keep working.
    Case("single_fragment", _fill(_rect(10, 30)), 0, p1=(14, 25), p2=(26, 25)),
    Case("identical_fragments", _fill(_rect(10, 30)) * 2, 0, p1=(14, 25), p2=(26, 25)),
    Case(
        "bridged_by_track",
        _fill(_rect(10, 18)) + _fill(_rect(20, 30)),
        0,
        p1=(14, 25),
        p2=(26, 25),
        extra=(
            '  (segment (start 17 25) (end 21 25) (width 0.3) (layer "F.Cu") (net 1)\n'
            '    (uuid "40000000-0000-0000-0000-000000000001"))\n'
        ),
    ),
    Case(
        "pad_bridges_fragments",
        _fill(_rect(10, 20)) + _fill(_rect(22, 32)),
        0,
        p1=(14, 25),
        p2=(26, 25),
        extra=(
            '  (footprint "TP" (layer "F.Cu")\n'
            '    (uuid "10000000-0000-0000-0000-000000000003") (at 21 25)\n'
            '    (property "Reference" "TP1" (at 0 0) (layer "F.Cu")\n'
            '      (uuid "20000000-0000-0000-0000-000000000003")\n'
            "      (effects (font (size 1 1) (thickness 0.15))))\n"
            '    (pad "1" smd rect (at 0 0) (size 3 3) (layers "F.Cu") (net 1 "GND")\n'
            '      (uuid "30000000-0000-0000-0000-000000000003"))\n'
            "  )\n"
        ),
    ),
    # --- file-version boundary (Issue #5362) --------------------------------
    # An omitted ``filled_areas_thickness`` is NOT unconditionally "stroked":
    # KiCad's parser initialises ``isStrokedFill = m_requiredVersion <
    # 20250210``.  Below the boundary the stored outline is a centre-line and
    # adjacent fragments are continuous; at or above it the stored outline is
    # already solid copper and they are not.  The #5362 witness is a
    # ``(version 20260206)`` board with the token absent, which is exactly why
    # a version-blind "absent means stroked" reading kept it false-clean.
    # An EXPLICIT ``no`` is solid at every version (the rows above pin that).
    *[
        Case(f"absent_v{ver}_gap0.0", _TWO_RECTS_GAP[0.0], native, stroke_encoded=True, version=ver)
        for ver, native in (
            (20240108, 0),
            (20250209, 0),
            (20250210, 1),
            (20260101, 1),
            (20260206, 1),
        )
    ],
    *[
        Case(f"absent_v{ver}_gap0.2", _TWO_RECTS_GAP[0.2], native, stroke_encoded=True, version=ver)
        for ver, native in (
            (20240108, 0),
            (20250209, 0),
            (20250210, 1),
            (20260101, 1),
            (20260206, 1),
        )
    ],
    # An EXPLICIT ``yes`` measures identically to the token being absent at
    # every version -- it does NOT force-stroke a >= 20250210 file.  Native
    # KiCad 10.0.6, same board/gap templates as the absent series above but
    # with a literal ``(filled_areas_thickness yes)`` token (Issue #5382):
    *[
        Case(
            f"explicit_yes_v{ver}_gap0.0",
            _TWO_RECTS_GAP[0.0],
            native,
            explicit_yes=True,
            version=ver,
        )
        for ver, native in (
            (20240108, 0),
            (20250209, 0),
            (20250210, 1),
            (20260101, 1),
            (20260206, 1),
        )
    ],
    *[
        Case(
            f"explicit_yes_v{ver}_gap0.2",
            _TWO_RECTS_GAP[0.2],
            native,
            explicit_yes=True,
            version=ver,
        )
        for ver, native in (
            (20240108, 0),
            (20250209, 0),
            (20250210, 1),
            (20260101, 1),
            (20260206, 1),
        )
    ],
    # Control: a conductor bridge stays connected on both sides of the
    # boundary -- the version gates fill-adjacency only, never real copper.
    *[
        Case(
            f"bridged_by_track_v{ver}",
            _fill(_rect(10, 18)) + _fill(_rect(20, 30)),
            0,
            p1=(14, 25),
            p2=(26, 25),
            version=ver,
            extra=(
                '  (segment (start 17 25) (end 21 25) (width 0.3) (layer "F.Cu") (net 1)\n'
                '    (uuid "40000000-0000-0000-0000-000000000001"))\n'
            ),
        )
        for ver in (20250209, 20260206)
    ],
]


@pytest.fixture(params=CASES, ids=lambda case: case.name)
def case(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[Case, Path]:
    board_case: Case = request.param
    path = tmp_path / f"{board_case.name}.kicad_pcb"
    path.write_text(board_case.board())
    return board_case, path


def test_net_status_matches_native_fill_fragment_bonding(
    case: tuple[Case, Path],
) -> None:
    """``NetStatusAnalyzer`` agrees with native KiCad on every measured board.

    Before #5362 every ``solid_*`` and ``stroke_gap>=0.26`` row reported
    ``complete`` (one island) because a zone's fragments were unioned by zone
    identity alone -- the net-status half of the R13/R14 VCC false negative.
    """
    board_case, path = case
    status = NetStatusAnalyzer(path, strict=True).analyze().get_net("GND")
    assert status is not None
    expected_islands = 1 if board_case.connected else 2
    assert status.island_count == expected_islands, (
        f"{board_case.name}: native kicad-cli reported "
        f"{board_case.native_unconnected} unconnected_items "
        f"(=> {expected_islands} copper islands) but net-status found "
        f"{status.island_count} (status={status.status})"
    )


def test_pad_partition_matches_native_fill_fragment_bonding(
    case: tuple[Case, Path],
) -> None:
    """``extract_pad_partition`` agrees with native KiCad on every measured board.

    This is the copper-LVS half: the same fragment-bonding question decides
    whether the two supply pads land in one physical component.
    """
    board_case, path = case
    partition = ConnectivityValidator(path).extract_pad_partition()
    component = next(c for c in partition if "R13.1" in c)
    assert ("R14.1" in component) is board_case.connected, (
        f"{board_case.name}: native kicad-cli reported "
        f"{board_case.native_unconnected} unconnected_items but the copper "
        f"partition gave {sorted(component)} (full partition={partition})"
    )


@pytest.mark.parametrize(
    ("stroke_encoded", "explicit_yes", "version", "token", "stroked", "inflation"),
    [
        # Explicit ``no``: solid at every version.
        (False, False, 20240108, False, False, 0.0),
        (False, False, 20260206, False, False, 0.0),
        # Token absent: resolved by the 20250210 parser boundary.
        (True, False, 20240108, None, True, 0.125),
        (True, False, 20250209, None, True, 0.125),
        (True, False, 20250210, None, False, 0.0),
        (True, False, 20260206, None, False, 0.0),
        # Explicit ``yes``: measured IDENTICAL to the token being absent at
        # every version, not unconditionally stroked (Issue #5382).  KiCad's
        # ``T_filled_areas_thickness`` handler is ``if (!parseBool())
        # isStrokedFill = false;`` -- there is no branch that sets the flag,
        # so ``yes`` can only ever leave the version-derived default alone.
        (False, True, 20240108, True, True, 0.125),
        (False, True, 20250209, True, True, 0.125),
        (False, True, 20250210, True, False, 0.0),
        (False, True, 20260206, True, False, 0.0),
    ],
)
def test_zone_fill_encoding_follows_token_then_file_version(
    tmp_path: Path,
    stroke_encoded: bool,
    explicit_yes: bool,
    version: int,
    token: bool | None,
    stroked: bool,
    inflation: float,
) -> None:
    """``Zone`` mirrors KiCad's ``isStrokedFill`` resolution order (#5362, #5382).

    The parser initialises ``isStrokedFill = m_requiredVersion < 20250210``
    and only then applies ``if (!parseBool()) isStrokedFill = false;`` for an
    explicit ``(filled_areas_thickness ...)`` token -- there is no branch that
    *sets* the flag, so an absent OR explicit-``yes`` token is equally
    version-dependent, while an explicit ``no`` always clears it. Reading the
    absent case as unconditionally stroked is what kept the #5362 witness --
    a ``(version 20260206)`` board with no token -- reporting false-clean;
    reading an explicit ``yes`` as unconditionally stroked would keep the
    same combination false-clean under an explicit token instead (#5382).
    """
    path = tmp_path / f"zone_v{version}_{stroke_encoded}_{explicit_yes}.kicad_pcb"
    path.write_text(
        Case(
            "encoding",
            _fill(_rect(10, 30)),
            0,
            stroke_encoded=stroke_encoded,
            explicit_yes=explicit_yes,
            version=version,
        ).board()
    )

    zone = PCB.load(path).zones[0]
    assert zone.file_version == version
    assert zone.filled_areas_thickness is token
    assert zone.is_stroked_fill() is stroked
    assert zone.fill_inflation() == pytest.approx(inflation)


# --- the retained #5362 witness ---------------------------------------------

WITNESS_DIR = Path(__file__).resolve().parent / "fixtures" / "issue-5362-witness"
WITNESS_PCB = WITNESS_DIR / "usb_joystick_routed.kicad_pcb"
WITNESS_SCH = WITNESS_DIR / "usb_joystick.kicad_sch"
WITNESS_PCB_SHA = "0141cb8e49f99aab13c005ca0b7431ca3e227bb934d7ed788b474b33578c6303"
WITNESS_SCH_SHA = "c715b4bd587bc4a2c36f8d58e9f40f4999cff6ca4891faaab65000e7369088d8"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def witness() -> tuple[Path, Path]:
    """The retained #5362 witness, pinned by hash (see the fixture README)."""
    assert _sha256(WITNESS_PCB) == WITNESS_PCB_SHA, (
        "the #5362 witness PCB must be byte-identical to the board measured in "
        "the issue; do not regenerate or reformat it"
    )
    assert _sha256(WITNESS_SCH) == WITNESS_SCH_SHA
    return WITNESS_SCH, WITNESS_PCB


def test_witness_zone_encoding_is_solid_despite_absent_token(
    witness: tuple[Path, Path],
) -> None:
    """The witness is `(version 20260206)` with no `filled_areas_thickness`.

    That combination is the whole defect: a version-blind "absent means
    stroked" reading inflates these fills by ``min_thickness / 2`` and fuses
    fragments native KiCad keeps apart.
    """
    _sch, pcb = witness
    zones = ConnectivityValidator(pcb).pcb.zones
    assert zones, "witness must carry pour zones"
    assert all(zone.file_version == 20260206 for zone in zones)
    assert all(zone.filled_areas_thickness is None for zone in zones)
    assert all(not zone.is_stroked_fill() for zone in zones)
    assert all(zone.fill_inflation() == 0.0 for zone in zones)


def test_witness_strict_net_status_reports_the_native_open(
    witness: tuple[Path, Path],
) -> None:
    """Strict net-status must find exactly the one open native DRC reports.

    Native ``kicad-cli pcb drc`` 10.0.5 on these exact bytes (no
    ``--refill-zones``, no ``--save-board``, hash unchanged) reports::

        Found 1 unconnected items
          Pad 1 [VCC] of R14 on F.Cu (155.675, 103.5)
            <-> Track [VCC] on F.Cu, length 1.9700 mm (151.68, 103.5)

    and native ``BuildConnectivity()`` places ``R13.1`` alone with that track
    (PR #5381 review). Before #5362 this reported ``total_unconnected_pads=0``
    -- the false negative the issue was filed for.
    """
    _sch, pcb = witness
    before = _sha256(pcb)

    report = NetStatusAnalyzer(pcb, strict=True).analyze()
    assert sum(net.unconnected_count for net in report.nets) == 1

    vcc = report.get_net("VCC")
    assert vcc is not None
    assert vcc.status != "complete"
    assert vcc.island_count == 2
    assert [f"{p.reference}.{p.pad_number}" for p in vcc.unconnected_pads] == ["R13.1"]

    assert _sha256(pcb) == before, "analysis must not rewrite the witness"


def test_witness_copper_lvs_reports_the_native_open(
    witness: tuple[Path, Path],
) -> None:
    """Copper-LVS must report the same single VCC open, and no shorts.

    Before #5362 this returned ``clean=True`` with ``bound_pad_count=129``
    on the same bytes.
    """
    from kicad_tools.lvs.copper_lvs import compare_copper_netlist

    sch, pcb = witness
    before = _sha256(pcb)

    result = compare_copper_netlist(sch, pcb)
    assert not result.vacuous
    assert result.bound_pad_count == 129
    assert result.shorts == ()
    assert [(m.net_a, m.pad_a, m.pad_b) for m in result.opens] == [("VCC", "C1.1", "R13.1")]
    assert result.clean is False

    assert _sha256(pcb) == before, "analysis must not rewrite the witness"
