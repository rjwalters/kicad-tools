"""Regression test: board 05 ships >=4 filled copper-pour zones in its routed PCB.

Issue #2899 (umbrella #2746 child) — ``boards/05-bldc-motor-controller`` is
the canonical regression vehicle for the zone-generation pipeline.  Also
serves acceptance criteria #1 and #2 of issue #2901 (zone count >=4 and
required nets VMOTOR/+5V/+3.3V/GND each owning a zone) -- both ACs are
already covered by the tests in this module; #2901 extends coverage to
the DRC allowlist (``tests/test_board_05_drc_allowlist.py``), thermal
stitching (``tests/test_board_05_thermal_stitch.py``), and export
preflight (``tests/test_board_05_export.py``).

Prior to issue #2899 the committed routed PCB carried zero ``(zone ...)`` blocks
because ``design.py``'s self-contained pipeline ran the router straight
after PCB creation, never invoking the zone generator.  The build also
could not patch over the gap: ``design.py`` registers as both the
schematic and PCB generator, so ``kct build``'s SCHEMATIC step runs it
once, the build either short-circuits subsequent steps (when the script
exits 0) or aborts before the ZONES step (when it exits 1 on ERC/DRC
failure -- which is the steady-state for this board today).

The fix adds an explicit ``create_zones_for_pcb`` step inside
``design.py``, between PCB generation and routing, and a follow-up
``fill_zones_in_routed_pcb`` step after routing.  The router preserves
zones via raw-text concatenation (the write path fixed in #2770), so the
on-disk routed PCB ends up with the same zone definitions the unrouted
PCB carries, plus the ``filled_polygon`` blocks produced by
``kicad-cli pcb drc``.

This test pins the post-fix state so a future regression that drops the
zones step from design.py -- or breaks zone preservation in the router
write path -- is caught loudly without requiring a full end-to-end
``kct build`` rebuild.

The test consumes the committed ``output/bldc_controller_routed.kicad_pcb``
directly; it does not re-run design.py (that takes ~5 minutes on the
default routing budget and would be flaky on CI).  Re-generation happens
manually via ``uv run python boards/05-bldc-motor-controller/design.py``
when the source schematic or placement changes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "05-bldc-motor-controller"
UNROUTED_PCB = BOARD_DIR / "output" / "bldc_controller.kicad_pcb"
ROUTED_PCB = BOARD_DIR / "output" / "bldc_controller_routed.kicad_pcb"

# Issue #2899 acceptance criterion 1: at least 4 zones in the committed
# routed PCB.  Five nets currently get zones in the routed artifact
# (VIN, +24V, +5V, +3V3, GND); a stricter assertion would over-fit to
# today's placement.
# Note: rails renamed VMOTOR -> +24V and +3.3V -> +3V3 in PR #3393
# (closes #3384) to match stock ``power:+24V`` / ``power:+3V3`` symbols.
# Note: the PWR_LED zone was removed from BOTH artifacts in issue
# #3513 -- its 0.65mm-wide strip was geometrically incapable of filling
# (narrower than its own pads plus min_thickness/clearance shrink, so
# KiCad produced zero filled_polygons -> R3.1 was an open circuit).
# PWR_LED is now connected by an F.Cu trace instead, and the net-name
# classifier no longer treats PWR_LED as POWER, so auto-pour will not
# recreate the dead zone on regen (see TestBoard05PwrLedShortRegression).
MIN_REQUIRED_ZONES = 4

# Issue #2899 acceptance criterion 2 (subset): +24V, +5V, +3V3, and
# GND must each have a zone.  PWR_LED is allowed to disappear (small
# net, may not survive an all-power-board guard or a future filter).
REQUIRED_NETS = {"+24V", "+5V", "+3V3", "GND"}


def _count_zones(pcb_path: Path) -> int:
    """Count ``(zone ...)`` blocks in *pcb_path* via regex.

    Uses a regex rather than ``PCB.load`` so the test catches both the
    write-path-preservation regression and the parser regression
    simultaneously.  The KiCad serializer wraps zones as ``(zone\\n``
    (newline after the opening token); we match either form for
    robustness against future formatting churn.
    """
    text = pcb_path.read_text()
    # Match (zone at the start of a line followed by whitespace/newline.
    return len(re.findall(r"\(zone[\s\n]", text))


def _zone_net_names(pcb_path: Path) -> set[str]:
    """Return the set of net names that own a zone in *pcb_path*.

    Parses both the legacy ``(net_name "GND")`` and KiCad-9
    ``(net "GND")`` forms inside zone blocks.  The match is tolerant of
    whitespace so it survives both the original ``(zone (net_name ...))``
    one-line form and the multi-line form produced by ``kicad-cli pcb
    drc`` after a zone fill.
    """
    text = pcb_path.read_text()
    names: set[str] = set()
    # Find each (zone ...) block and extract the first (net "...") inside.
    # Use a non-greedy capture so each zone is matched independently.
    for zm in re.finditer(r"\(zone\b.*?(?=\(zone\b|\Z)", text, re.DOTALL):
        block = zm.group(0)
        # Try the KiCad-9 (net "NAME") form first (used after kicad-cli fill).
        nm = re.search(r'\(net\s+"([^"]+)"\)', block)
        if not nm:
            # Fall back to the legacy (net_name "NAME") form.
            nm = re.search(r'\(net_name\s+"([^"]+)"\)', block)
        if nm:
            names.add(nm.group(1))
    return names


@pytest.fixture(scope="module")
def routed_pcb_path() -> Path:
    """Resolve the committed routed PCB or skip if absent.

    The PCB lives under ``boards/05-bldc-motor-controller/output/`` and
    is committed to git so this test is self-contained.  If the file is
    missing (someone wiped output/), skip with a regen hint rather than
    fail spuriously.
    """
    if not ROUTED_PCB.exists():
        pytest.skip(
            f"Board 05 routed PCB not found at {ROUTED_PCB!s}; "
            "regenerate via "
            "`uv run python boards/05-bldc-motor-controller/design.py`"
        )
    return ROUTED_PCB


@pytest.fixture(scope="module")
def unrouted_pcb_path() -> Path:
    """Resolve the committed unrouted PCB or skip if absent."""
    if not UNROUTED_PCB.exists():
        pytest.skip(
            f"Board 05 unrouted PCB not found at {UNROUTED_PCB!s}; "
            "regenerate via "
            "`uv run python boards/05-bldc-motor-controller/design.py`"
        )
    return UNROUTED_PCB


class TestBoard05ZonePreservation:
    """Pin the post-#2899 zone state of board 05's committed PCBs."""

    def test_routed_pcb_has_min_zones(self, routed_pcb_path: Path) -> None:
        """Acceptance criterion 1: routed PCB carries >=4 zones.

        Counts ``(zone ...)`` blocks via regex; both the original
        single-line and the multi-line form produced by ``kicad-cli pcb
        drc`` (after zone fill) are matched.  A failure here means
        either:

        * ``design.py`` no longer creates zones before routing (the fix
          this issue installed), or
        * The router's write path dropped the zones again (regression of
          PR #2770), or
        * Someone wiped the output/ dir without rerunning design.py.
        """
        count = _count_zones(routed_pcb_path)
        assert count >= MIN_REQUIRED_ZONES, (
            f"Board 05 routed PCB has {count} zone(s), expected "
            f">={MIN_REQUIRED_ZONES}.  Either design.py stopped calling "
            f"create_zones_for_pcb (issue #2899 fix) or the router write "
            f"path is dropping zones again (regression of PR #2770).  "
            f"Regenerate via `uv run python "
            f"boards/05-bldc-motor-controller/design.py`."
        )

    def test_routed_pcb_zones_include_required_nets(
        self,
        routed_pcb_path: Path,
    ) -> None:
        """Acceptance criterion 2: VMOTOR, +5V, +3.3V, GND must each have a zone.

        These four nets carry the power and ground rails the BLDC
        controller cannot operate without -- the audit in umbrella issue
        #2746 lists 20+ pads on VMOTOR alone that depend on plane
        copper.  PWR_LED is intentionally omitted from the required set
        because it is a small auxiliary net that may legitimately get
        skipped by future tighter filters.
        """
        zoned_nets = _zone_net_names(routed_pcb_path)
        missing = REQUIRED_NETS - zoned_nets
        assert not missing, (
            f"Board 05 routed PCB is missing zones for: {sorted(missing)}.  "
            f"Found zones for: {sorted(zoned_nets)}.  Each of the four "
            f"power/ground nets must have a zone for the routed board to be "
            f"electrically complete (umbrella #2746)."
        )

    def test_unrouted_pcb_has_min_zones(self, unrouted_pcb_path: Path) -> None:
        """The unrouted PCB also carries the zones (precondition for routing).

        The route step preserves zones via raw-text concatenation -- if the
        unrouted PCB does not carry them in the first place, the routed PCB
        cannot either.  This split is purely diagnostic: a failure here
        means design.py's ``create_zones_for_pcb`` step did not run or did
        not write the file, narrowing the search space when the routed-PCB
        assertion fails.
        """
        count = _count_zones(unrouted_pcb_path)
        assert count >= MIN_REQUIRED_ZONES, (
            f"Board 05 unrouted PCB has {count} zone(s), expected "
            f">={MIN_REQUIRED_ZONES}.  design.py's create_zones_for_pcb "
            f"step did not run or did not modify the PCB.  Check whether "
            f"auto_pour_if_missing skipped the board (all-power guard, "
            f"#2740) or failed silently."
        )

    def test_zones_distributed_across_layers(
        self,
        routed_pcb_path: Path,
    ) -> None:
        """Acceptance criterion 3: zones must use both copper layers.

        On a 2-layer stackup the zone allocator (#2771) puts GND on
        ``B.Cu`` and the power nets on ``F.Cu``.  If every zone lands on
        the same layer the allocator regressed -- KiCad's fill resolver
        would then award the entire shared region to the
        highest-priority zone, leaving the rest with zero copper despite
        their definitions surviving.

        Test passes when at least one zone is on ``F.Cu`` AND at least
        one is on ``B.Cu`` -- exact assignments may shift as the
        allocator evolves.
        """
        text = routed_pcb_path.read_text()
        # Match (zone ...) blocks and extract the (layer "X") inside.
        # Use lookahead to bound each block at the next zone or EOF.
        layers_seen: set[str] = set()
        for zm in re.finditer(r"\(zone\b.*?(?=\(zone\b|\Z)", text, re.DOTALL):
            block = zm.group(0)
            lm = re.search(r'\(layer\s+"([^"]+)"\)', block)
            if lm:
                layers_seen.add(lm.group(1))

        assert "F.Cu" in layers_seen and "B.Cu" in layers_seen, (
            f"Board 05 routed-PCB zones use layers: {sorted(layers_seen)}.  "
            f"Expected both F.Cu and B.Cu (issue #2771 invariant: zones "
            f"must distribute across copper layers, not stack on F.Cu).  "
            f"GND should land on B.Cu; VMOTOR / +5V / +3.3V on F.Cu."
        )


class TestBoard05PwrLedShortRegression:
    """Issue #3513 follow-up: PWR_LED trace must not short into zone fills.

    The first fix for #3513 added a D3.1->R3.1 trace but left the +3V3
    F.Cu zone's stale ``filled_polygon`` in place -- the fill predated
    the trace and poured straight through the corridor, shorting
    PWR_LED to +3V3 for ~3.1mm.  The standard DRC gate cannot see
    segment-vs-foreign-zone-fill overlap (tracked in #3527), so this
    test pins the geometric invariant directly:

    * no PWR_LED segment centerline point may fall inside a foreign-net
      ``filled_polygon`` on the same layer, and
    * the PWR_LED net must not be POWER-classified (otherwise auto-pour
      recreates the geometrically-unfillable zone on the next regen and
      the router auto-skips the net, reintroducing the original open).
    """

    def test_pwr_led_not_classified_as_power(self) -> None:
        """The net-name classifier must not treat PWR_LED as a rail.

        ``PWR_LED`` previously matched the ``^(...|PWR|POWER|...)``
        prefix pattern, so ``auto_pour_if_missing`` (called by both
        design.py and ``kct route``) emitted a pour zone for a 2-pad
        indicator-signal net and the router auto-skipped it.
        """
        from kicad_tools.router.net_class import NetClass, classify_net

        for name in ("PWR_LED", "POWER_LED", "PWRLED"):
            assert classify_net(name).net_class is not NetClass.POWER, (
                f"{name} must not classify as POWER -- indicator-LED nets are "
                f"2-pad signals; POWER classification makes auto-pour emit an "
                f"unfillable zone and skip routing (issue #3513)."
            )

    def test_unrouted_pcb_has_no_pwr_led_zone(self, unrouted_pcb_path: Path) -> None:
        """The committed unrouted PCB must not carry the dead PWR_LED zone."""
        assert "PWR_LED" not in _zone_net_names(unrouted_pcb_path), (
            "Board 05 unrouted PCB carries a PWR_LED zone again.  Its "
            "0.65mm strip is geometrically unfillable and its presence makes "
            "the router auto-skip the net, shipping an open circuit "
            "(issue #3513)."
        )

    def test_pwr_led_trace_clear_of_foreign_fills(self, routed_pcb_path: Path) -> None:
        """No PWR_LED segment centerline may lie inside a foreign zone fill.

        This is the check that caught the +3V3 short: sample points along
        every PWR_LED segment and assert none falls inside a same-layer
        ``filled_polygon`` belonging to a different net.  See
        ``scripts/check_trace_vs_zone_fills.py`` for the full-board,
        clearance-aware version of this check.
        """
        from kicad_tools.sexp import parse_file

        doc = parse_file(str(routed_pcb_path))

        net_names: dict[int, str] = {}
        for net in doc.find_all("net"):
            atoms = net.get_atoms()
            if len(atoms) > 1 and str(atoms[1]):
                net_names[int(atoms[0])] = str(atoms[1])
        pwr_led_id = next((k for k, v in net_names.items() if v == "PWR_LED"), None)
        assert pwr_led_id is not None, "PWR_LED net missing from routed PCB"

        # Collect foreign-net fills per layer.
        fills: list[tuple[str, str, list[tuple[float, float]]]] = []
        for zone in doc.find_all("zone"):
            znet_node = zone.find("net")
            if znet_node is None:
                continue
            zatom = znet_node.get_atoms()[0]
            zname = net_names.get(zatom, str(zatom)) if isinstance(zatom, int) else str(zatom)
            if zname == "PWR_LED":
                continue
            for fp in zone.find_all("filled_polygon"):
                layer_node = fp.find("layer")
                pts_node = fp.find("pts")
                if layer_node is None or pts_node is None:
                    continue
                layer = str(layer_node.get_atoms()[0])
                poly = [
                    (float(xy.get_atoms()[0]), float(xy.get_atoms()[1]))
                    for xy in pts_node.find_all("xy")
                ]
                if len(poly) >= 3:
                    fills.append((zname, layer, poly))
        assert fills, "Routed PCB has no zone fills -- zones were not filled"

        def _inside(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
            inside = False
            for i in range(len(poly)):
                x1, y1 = poly[i]
                x2, y2 = poly[(i + 1) % len(poly)]
                if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                    inside = not inside
            return inside

        segments_checked = 0
        for seg in doc.find_all("segment"):
            net_node = seg.find("net")
            if net_node is None or int(net_node.get_atoms()[0]) != pwr_led_id:
                continue
            layer = str(seg.find("layer").get_atoms()[0])
            x1, y1 = (float(a) for a in seg.find("start").get_atoms()[:2])
            x2, y2 = (float(a) for a in seg.find("end").get_atoms()[:2])
            segments_checked += 1
            for i in range(101):
                t = i / 100
                px, py = x1 + t * (x2 - x1), y1 + t * (y2 - y1)
                for zname, zlayer, poly in fills:
                    if zlayer != layer:
                        continue
                    assert not _inside(px, py, poly), (
                        f"PWR_LED segment point ({px:.3f}, {py:.3f}) on {layer} "
                        f"lies INSIDE the '{zname}' zone fill -- inter-net short "
                        f"(issue #3513 / checker gap #3527).  Re-fill zones with "
                        f"`kct zones fill` so fills knock out around the trace."
                    )
        assert segments_checked >= 1, (
            "PWR_LED has no segments in the routed PCB -- the net is an open "
            "circuit again (original #3513 defect)."
        )
