"""Fabrication process model for via-in-pad and other advanced processes.

Issue #5009: ``DesignRules.via_in_pad_supported`` /
``MfrLimits.via_in_pad_supported`` (see
``kicad_tools.router.mfr_limits``) are bare capability booleans -- they
say a manufacturer *offers* via-in-pad somewhere in its catalog, but
carry no drill range, layer-count floor, annular-ring floor,
component-hole clearance, or filled/capped requirement.  A board can
therefore trip a real vendor's actual eligibility window (e.g. JLCPCB's
Plated-Over Filled Via / POFV process requires >=4 copper layers and a
0.2-0.5 mm drill) while the bare boolean happily reports "supported".

:class:`FabricationProcess` models one *specific, orderable* process
offering, distinct from the general :class:`~kicad_tools.manufacturers.base.DesignRules`
capability floors (trace width, clearance, via drill, etc. -- the whole
board's baseline).  A process is attached to a particular layer/copper
configuration via ``DesignRules.via_in_pad_process_id`` (a key into
:data:`FABRICATION_PROCESSES`); :mod:`kicad_tools.validate.rules.via_in_pad`
resolves the attached process and validates the board's actual via
geometry against it before suppressing a via-in-pad finding.

This is deliberately data attached to the manufacturer *profile*
(``jlcpcb_tier1.yaml`` etc.) rather than a separate per-board "opt in"
call: a layer/copper configuration only carries a
``via_in_pad_process_id`` when the vendor has a real, documented process
for that configuration.  JLCPCB's Capability Plus tier illustrates the
exact bug this closes -- its 2-layer configurations set
``via_in_pad_supported: true`` (Capability Plus is available on 2-layer
orders) but JLCPCB's *via-in-pad-specific* POFV process publishes a
4-layer minimum; the 2-layer configs therefore carry no
``via_in_pad_process_id`` and any 2-layer via-in-pad geometry now fails
closed instead of silently passing.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FabricationProcess:
    """A specific, orderable fabrication process a board can rely on.

    All geometric fields are in millimeters.

    Attributes:
        process_id: Stable machine identifier, used as the
            ``DesignRules.via_in_pad_process_id`` value and as a
            :data:`FABRICATION_PROCESSES` registry key
            (e.g. ``"jlcpcb-tier1-pofv-4l"``).
        name: Human-readable name, used verbatim in generated ordering
            instructions and README/report prose.
        min_layer_count: Minimum copper-layer count this process
            requires (e.g. JLCPCB's POFV is a 4+ layer offering).
        min_via_drill_mm: Minimum via drill diameter this process
            permits.
        max_via_drill_mm: Maximum via drill diameter this process
            permits.
        min_annular_ring_mm: Minimum annular ring width
            (``(via.size - via.drill) / 2``) for a via using this
            process.
        requires_filled_and_capped: Whether the process requires
            epoxy-filled, copper-capped (plated-over) vias.  True for
            every via-in-pad process modeled here -- a via-in-pad
            offering that is not filled and capped is not a real
            fabrication option (solder would wick into an open drill).
        min_component_hole_distance_mm: Minimum edge-to-edge distance
            an in-pad via must keep from any *other* component's
            drilled hole (a PTH pad or mounting hole), per the vendor's
            published spec.
        source: URL (or citation) documenting where these numbers came
            from, surfaced verbatim in ordering instructions for
            provenance.
    """

    process_id: str
    name: str
    min_layer_count: int
    min_via_drill_mm: float
    max_via_drill_mm: float
    min_annular_ring_mm: float
    requires_filled_and_capped: bool
    min_component_hole_distance_mm: float
    source: str

    def to_dict(self) -> dict[str, object]:
        """Convert to a machine-readable dict for export/report artifacts."""
        return {
            "process_id": self.process_id,
            "name": self.name,
            "min_layer_count": self.min_layer_count,
            "min_via_drill_mm": self.min_via_drill_mm,
            "max_via_drill_mm": self.max_via_drill_mm,
            "min_annular_ring_mm": self.min_annular_ring_mm,
            "requires_filled_and_capped": self.requires_filled_and_capped,
            "min_component_hole_distance_mm": self.min_component_hole_distance_mm,
            "source": self.source,
        }

    def ordering_instructions(self) -> str:
        """Human-readable factory ordering instructions derived from this process.

        Generalizes the hand-typed README prose boards/03-usb-joystick
        wrote for its reviewed four-layer POFV contract ("Order the
        exact four-layer stackup and Epoxy-filled & Capped POFV/VIPPO
        process...") into a machine-derived statement any board carrying
        this process can reuse verbatim.
        """
        fill_note = (
            "epoxy-filled and copper-capped (plated-over) vias"
            if self.requires_filled_and_capped
            else "standard via processing"
        )
        return (
            f"Order the {self.name} process ({self.process_id}): "
            f"a >= {self.min_layer_count}-layer stackup with {fill_note}, "
            f"via drills between {self.min_via_drill_mm:g} and "
            f"{self.max_via_drill_mm:g} mm, annular ring >= "
            f"{self.min_annular_ring_mm:g} mm, and in-pad vias kept >= "
            f"{self.min_component_hole_distance_mm:g} mm from any other "
            f"component's drilled hole. Do not substitute ordinary open or "
            f"merely tented vias. Source: {self.source}"
        )


# JLCPCB Capability Plus's via-in-pad-specific offering: Plated-Over
# Filled Via (POFV).  Requires >=4 copper layers -- distinct from (and
# stricter than) the tier's general 2-layer availability implied by the
# bare ``via_in_pad_supported: true`` flag on 2-layer configs.
#
# Drill/ring floors mirror JLCPCB's published 4-layer advanced-PCB
# capability (min_via_drill_mm 0.2, min_annular_ring_mm 0.10 on
# jlcpcb_tier1.yaml's 4layer_1oz/4layer_2oz configs); the 0.5 mm ceiling
# and component-hole distance mirror the fab's general drill-to-drill
# floor (``min_hole_to_hole_mm``, canonically 0.5 mm).
#
# Source: https://jlcpcb.com/news/free-via-in-pad-6-20-layer-pcbs-pofv
JLCPCB_TIER1_POFV_4L = FabricationProcess(
    process_id="jlcpcb-tier1-pofv-4l",
    name="JLCPCB Capability Plus -- Plated-Over Filled Via (POFV), 4+ layer",
    min_layer_count=4,
    min_via_drill_mm=0.2,
    max_via_drill_mm=0.5,
    min_annular_ring_mm=0.10,
    requires_filled_and_capped=True,
    min_component_hole_distance_mm=0.5,
    source="https://jlcpcb.com/news/free-via-in-pad-6-20-layer-pcbs-pofv",
)

# PCBWay publishes via-in-pad (epoxy-filled, plated-over) as a standard
# option at every layer count in its capability table (unlike JLCPCB's
# tier-gated 4+ layer POFV), so this process attaches to every PCBWay
# layer/copper configuration.
#
# Source: https://www.pcbway.com/capabilities.html
PCBWAY_VIA_IN_PAD = FabricationProcess(
    process_id="pcbway-via-in-pad",
    name="PCBWay Via-in-Pad (epoxy-filled & capped)",
    min_layer_count=2,
    min_via_drill_mm=0.15,
    max_via_drill_mm=0.5,
    min_annular_ring_mm=0.15,
    requires_filled_and_capped=True,
    min_component_hole_distance_mm=0.5,
    source="https://www.pcbway.com/capabilities.html",
)

# Registry keyed by ``process_id`` -- the value stored in
# ``DesignRules.via_in_pad_process_id``.
FABRICATION_PROCESSES: dict[str, FabricationProcess] = {
    JLCPCB_TIER1_POFV_4L.process_id: JLCPCB_TIER1_POFV_4L,
    PCBWAY_VIA_IN_PAD.process_id: PCBWAY_VIA_IN_PAD,
}


def get_fabrication_process(process_id: str | None) -> FabricationProcess | None:
    """Look up a :class:`FabricationProcess` by id.

    Returns ``None`` for ``None``/empty/unknown ids so callers can treat
    "no id" and "unrecognized id" identically (both mean "no valid
    process selection").
    """
    if not process_id:
        return None
    return FABRICATION_PROCESSES.get(process_id)


def describe_selection(design_rules: object) -> dict[str, object] | None:
    """Resolve ``design_rules.via_in_pad_process_id`` into an export-ready dict.

    Returns ``None`` when the design rules carry no
    ``via_in_pad_process_id`` attribute, no id, or an id this module does
    not recognize -- callers (export bundle / check-report writers)
    should omit the ``fabrication_process`` key entirely in that case
    rather than emit a misleading placeholder.

    The returned dict is the machine-readable artifact this issue asks
    for: process parameters plus a derived ``ordering_instructions``
    string, suitable for embedding in a manufacturing bundle or the
    ``kct check`` JSON report so readiness evidence can bind to it.
    """
    process_id = getattr(design_rules, "via_in_pad_process_id", None)
    process = get_fabrication_process(process_id)
    if process is None:
        return None
    data = process.to_dict()
    data["ordering_instructions"] = process.ordering_instructions()
    return data
