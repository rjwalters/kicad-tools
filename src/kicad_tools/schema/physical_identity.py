"""Internal footprint keys, independent of ambiguous displayed references."""

from collections import Counter
from collections.abc import Mapping, Sequence

from kicad_tools.schema.pcb import Footprint, Pad


def footprint_keys(footprints: Sequence[Footprint]) -> list[str]:
    """Keep unambiguous legacy lookups; disambiguate every repeated label.

    Existing UUIDs identify repeated-reference footprints, including repeated
    empty labels. A unique empty label retains its legacy empty key. Legacy
    files without unique UUIDs use their footprint occurrence in the source
    document. Callers must carry these keys through reconstruction, rather
    than recomputing them from a filtered pad population. No key is written
    into the authored Reference property.
    """
    refs = Counter(fp.reference for fp in footprints)
    uuids = Counter(getattr(fp, "uuid", "") for fp in footprints)
    reserved = set(refs)
    result = []
    for index, fp in enumerate(footprints):
        if refs[fp.reference] == 1:
            key = fp.reference
        else:
            uuid = getattr(fp, "uuid", "")
            identity = f"uuid:{uuid}" if uuid and uuids[uuid] == 1 else f"index:{index}"
            key = f"@kct-footprint:{identity}"
            while key in reserved:
                key = "@" + key
        reserved.add(key)
        result.append(key)
    return result


def component_keys(components: Sequence[dict]) -> list[str]:
    """Assign keys before filtering generic routing components; retain carried IDs."""
    refs = Counter(comp["ref"] for comp in components)
    reserved = {comp["ref"] for comp in components}
    reserved.update(
        comp["component_id"] for comp in components if comp.get("component_id") is not None
    )
    result: list[str] = []
    for index, comp in enumerate(components):
        if comp.get("component_id") is not None:
            key = comp["component_id"]
        elif refs[comp["ref"]] == 1:
            key = comp["ref"]
        else:
            key = f"@kct-footprint:index:{index}"
            while key in reserved:
                key = "@" + key
        if key in result:
            raise ValueError(f"Duplicate physical component identity {key!r}")
        reserved.add(key)
        result.append(key)
    return result


def validate_netlist_selectors(
    footprints: Sequence[Footprint], overrides: Mapping[str, str]
) -> None:
    """Reject a reference/pin override that names multiple physical footprints."""
    owners: dict[str, set[int]] = {}
    for index, fp in enumerate(footprints):
        for pad in fp.pads:
            owners.setdefault(f"{fp.reference}.{pad.number}", set()).add(index)
    ambiguous = sorted(key for key in overrides if len(owners.get(key, set())) > 1)
    if ambiguous:
        raise ValueError(f"Ambiguous netlist terminal selectors: {', '.join(ambiguous)}")


def physical_pad_position(fp: Footprint, pad: Pad) -> tuple[float, float]:
    """Transform an actual pad occurrence without a reference/pin lookup."""
    from kicad_tools.core.geometry import rotate_pad_offset

    dx, dy = rotate_pad_offset(pad.position[0], pad.position[1], fp.rotation)
    return fp.position[0] + dx, fp.position[1] + dy
