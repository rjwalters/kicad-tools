"""
Specctra SES (Session) import for KiCad PCB files.

Parses a Freerouting .ses file and merges the routed traces back into
a KiCad .kicad_pcb file.

The SES format contains:
- (routes ...) with (wire ...) and (via ...) elements
- Net names map back to the DSN net names
- Coordinates are in the same resolution as the DSN export

Usage::

    from kicad_tools.export.ses import SESToKiCadImporter

    importer = SESToKiCadImporter("board.ses")
    importer.merge_into("board.kicad_pcb", "board_routed.kicad_pcb")
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


def um_to_mm(um: float) -> float:
    """Convert micrometers to millimeters."""
    return um / 1000.0


@dataclass
class SESWire:
    """A routed wire from the SES file."""

    net_name: str
    layer: str
    width: float  # in SES units (um)
    points: list[tuple[float, float]] = field(default_factory=list)  # in SES units


@dataclass
class SESVia:
    """A via from the SES file."""

    net_name: str
    padstack_name: str
    x: float  # in SES units (um)
    y: float  # in SES units (um)


class SESToKiCadImporter:
    """Import a Specctra SES file and merge routes into a KiCad PCB.

    Args:
        ses_path: Path to the .ses file from Freerouting.
    """

    def __init__(self, ses_path: str | Path) -> None:
        self.ses_path = Path(ses_path)
        self._wires: list[SESWire] = []
        self._vias: list[SESVia] = []
        self._resolution: float = 10.0  # um per unit (from DSN resolution)
        self._parsed = False

    def parse(self) -> None:
        """Parse the SES file and extract wires and vias."""
        content = self.ses_path.read_text(encoding="utf-8")
        self._parse_ses_content(content)
        self._parsed = True
        logger.info(
            "Parsed SES: %d wires, %d vias",
            len(self._wires),
            len(self._vias),
        )

    def _parse_ses_content(self, content: str) -> None:
        """Parse the SES S-expression content.

        We use a lightweight hand-written parser here rather than the full
        sexp parser because SES files are simpler and we only need specific
        sections.
        """
        # Extract resolution if present
        res_match = re.search(r"\(resolution\s+\w+\s+(\d+)\)", content)
        if res_match:
            self._resolution = float(res_match.group(1))

        # Find the (routes ...) section
        routes_start = content.find("(routes")
        if routes_start < 0:
            logger.warning("No (routes ...) section found in SES file")
            return

        # Extract the routes section using bracket matching
        routes_content = _extract_balanced(content, routes_start)
        if not routes_content:
            return

        self._parse_routes_section(routes_content)

    def _parse_routes_section(self, content: str) -> None:
        """Parse the (routes ...) section for wires and vias."""
        pos = 0
        while pos < len(content):
            # Find next (wire ...) or (via ...)
            wire_pos = content.find("(wire", pos)
            via_pos = content.find("(via", pos)

            if wire_pos < 0 and via_pos < 0:
                break

            if wire_pos >= 0 and (via_pos < 0 or wire_pos < via_pos):
                wire_content = _extract_balanced(content, wire_pos)
                if wire_content:
                    wire = self._parse_wire(wire_content)
                    if wire:
                        self._wires.append(wire)
                    pos = wire_pos + len(wire_content)
                else:
                    pos = wire_pos + 1
            else:
                via_content = _extract_balanced(content, via_pos)
                if via_content:
                    via = self._parse_via(via_content)
                    if via:
                        self._vias.append(via)
                    pos = via_pos + len(via_content)
                else:
                    pos = via_pos + 1

    def _parse_wire(self, content: str) -> SESWire | None:
        """Parse a single (wire ...) element.

        Example:
            (wire
              (path F.Cu 250.0 105000.0 111230.0 114000.0 108000.0)
              (net VIN)
            )
        """
        # Extract net name
        net_match = re.search(r'\(net\s+"?([^")]+)"?\s*\)', content)
        if not net_match:
            return None
        net_name = net_match.group(1)

        # Extract path: (path <layer> <width> <x1> <y1> <x2> <y2> ...)
        path_match = re.search(
            r'\(path\s+"?([^"\s)]+)"?\s+([\d.]+)\s+(.*?)\)',
            content,
            re.DOTALL,
        )
        if not path_match:
            return None

        layer = path_match.group(1)
        width = float(path_match.group(2))
        coords_str = path_match.group(3).strip()

        # Parse coordinate pairs
        nums = re.findall(r"[-+]?[\d.]+(?:e[-+]?\d+)?", coords_str)
        points: list[tuple[float, float]] = []
        for i in range(0, len(nums) - 1, 2):
            points.append((float(nums[i]), float(nums[i + 1])))

        if len(points) < 2:
            return None

        return SESWire(
            net_name=net_name,
            layer=layer,
            width=width,
            points=points,
        )

    def _parse_via(self, content: str) -> SESVia | None:
        """Parse a single (via ...) element.

        Example:
            (via
              "Via[0-1]_Pad800_um" 115000.0 112000.0
              (net VIN)
            )
        """
        # Extract net name
        net_match = re.search(r'\(net\s+"?([^")]+)"?\s*\)', content)
        if not net_match:
            return None
        net_name = net_match.group(1)

        # Extract padstack name and coordinates
        # Pattern: (via "padstack_name" x y ... or (via padstack_name x y ...
        via_match = re.search(
            r'\(via\s+"?([^"\s)]+)"?\s+([-+]?[\d.]+)\s+([-+]?[\d.]+)',
            content,
        )
        if not via_match:
            return None

        return SESVia(
            net_name=net_name,
            padstack_name=via_match.group(1),
            x=float(via_match.group(2)),
            y=float(via_match.group(3)),
        )

    def merge_into(
        self,
        pcb_path: str | Path,
        output_path: str | Path | None = None,
        *,
        default_trace_width: float = 0.25,
        default_via_size: float = 0.8,
        default_via_drill: float = 0.4,
    ) -> str:
        """Merge SES routes into a KiCad PCB file.

        Args:
            pcb_path: Path to the original .kicad_pcb file.
            output_path: Path for the output file. If None, overwrites the
                input file.
            default_trace_width: Default trace width in mm if SES width is 0.
            default_via_size: Via pad diameter in mm.
            default_via_drill: Via drill diameter in mm.

        Returns:
            The merged PCB content as a string.
        """
        if not self._parsed:
            self.parse()

        pcb_path = Path(pcb_path)
        pcb_content = pcb_path.read_text(encoding="utf-8")

        # Build net name-to-number mapping from the PCB
        net_map = _build_net_map(pcb_content)

        # Generate KiCad segments and vias
        route_sexp = self._generate_kicad_routes(
            net_map, default_trace_width, default_via_size, default_via_drill
        )

        if not route_sexp:
            logger.info("No routes to merge")
            return pcb_content

        # Merge into PCB using the same approach as merge_routes_into_pcb
        merged = _merge_sexp_into_pcb(pcb_content, route_sexp)

        if output_path is not None:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(merged, encoding="utf-8")
            logger.info("Wrote merged PCB to %s", output_path)
        else:
            pcb_path.write_text(merged, encoding="utf-8")
            logger.info("Updated PCB in place: %s", pcb_path)

        return merged

    def _generate_kicad_routes(
        self,
        net_map: dict[str, int],
        default_trace_width: float,
        default_via_size: float,
        default_via_drill: float,
    ) -> str:
        """Convert SES wires/vias to KiCad S-expression segments/vias."""
        parts: list[str] = []

        # DSN layer -> KiCad layer (identity for our export)
        layer_map = {
            "F.Cu": "F.Cu",
            "B.Cu": "B.Cu",
            "In1.Cu": "In1.Cu",
            "In2.Cu": "In2.Cu",
            "In3.Cu": "In3.Cu",
            "In4.Cu": "In4.Cu",
            "In5.Cu": "In5.Cu",
            "In6.Cu": "In6.Cu",
        }

        for wire in self._wires:
            net_num = net_map.get(wire.net_name, 0)
            if net_num == 0:
                logger.warning("Unknown net %r in SES, skipping wire", wire.net_name)
                continue

            kicad_layer = layer_map.get(wire.layer, wire.layer)

            # Convert width from um to mm
            width_mm = um_to_mm(wire.width)
            if width_mm <= 0:
                width_mm = default_trace_width

            # Generate segments between consecutive points
            for i in range(len(wire.points) - 1):
                x1_mm = um_to_mm(wire.points[i][0])
                y1_mm = um_to_mm(wire.points[i][1])
                x2_mm = um_to_mm(wire.points[i + 1][0])
                y2_mm = um_to_mm(wire.points[i + 1][1])

                seg_uuid = str(uuid.uuid4())
                parts.append(
                    f"  (segment (start {x1_mm:.4f} {y1_mm:.4f}) "
                    f"(end {x2_mm:.4f} {y2_mm:.4f}) "
                    f"(width {width_mm:.4f}) "
                    f'(layer "{kicad_layer}") '
                    f"(net {net_num}) "
                    f'(uuid "{seg_uuid}"))'
                )

        for via in self._vias:
            net_num = net_map.get(via.net_name, 0)
            if net_num == 0:
                logger.warning("Unknown net %r in SES, skipping via", via.net_name)
                continue

            x_mm = um_to_mm(via.x)
            y_mm = um_to_mm(via.y)
            via_uuid = str(uuid.uuid4())

            parts.append(
                f"  (via (at {x_mm:.4f} {y_mm:.4f}) "
                f"(size {default_via_size}) "
                f"(drill {default_via_drill}) "
                f'(layers "F.Cu" "B.Cu") '
                f"(net {net_num}) "
                f'(uuid "{via_uuid}"))'
            )

        return "\n".join(parts)

    @property
    def wires(self) -> list[SESWire]:
        """Return parsed wires."""
        return list(self._wires)

    @property
    def vias(self) -> list[SESVia]:
        """Return parsed vias."""
        return list(self._vias)


# -- Helpers --


def _extract_balanced(text: str, start: int) -> str | None:
    """Extract a balanced parenthesized expression starting at 'start'."""
    if start >= len(text) or text[start] != "(":
        return None

    depth = 0
    in_string = False
    i = start
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == '"':
                in_string = False
            elif ch == "\\":
                i += 1  # skip escaped char
        else:
            if ch == '"':
                in_string = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        i += 1
    return None


def _build_net_map(pcb_content: str) -> dict[str, int]:
    """Build a mapping of net name -> net number from PCB content."""
    net_map: dict[str, int] = {}
    # Match (net <number> "<name>") with quoted names (may contain parens)
    # or (net <number> <name>) with unquoted names
    for match in re.finditer(
        r'^\s*\(net\s+(\d+)\s+"([^"]*)"\s*\)',
        pcb_content,
        re.MULTILINE,
    ):
        num = int(match.group(1))
        name = match.group(2)
        if name:
            net_map[name] = num
    # Also match unquoted net names
    for match in re.finditer(
        r'^\s*\(net\s+(\d+)\s+([^"\s)]+)\s*\)',
        pcb_content,
        re.MULTILINE,
    ):
        num = int(match.group(1))
        name = match.group(2)
        if name and name not in net_map:
            net_map[name] = num
    return net_map


def _merge_sexp_into_pcb(pcb_content: str, route_sexp: str) -> str:
    """Merge route S-expressions into PCB content.

    Inserts the routes before the final closing parenthesis.
    This follows the same pattern as merge_routes_into_pcb() in router/io.py.
    """
    if not route_sexp:
        return pcb_content

    content = pcb_content.rstrip()
    if content.endswith(")"):
        content = content[:-1].rstrip()

    result = content + "\n\n"
    result += route_sexp + "\n"
    result += ")\n"

    return result
