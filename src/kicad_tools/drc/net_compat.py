"""KiCad 8/9 net format compatibility helpers.

KiCad 8 uses integer net IDs in element attributes: ``(net 5)``
KiCad 9 uses name-only strings: ``(net "GND")``

This module provides a single helper to safely parse net atoms regardless
of format.
"""

from __future__ import annotations


def resolve_net_atom(
    atom: str | None,
    nets: dict[int, str] | None = None,
    net_names: dict[str, int] | None = None,
) -> tuple[int, str]:
    """Resolve a net atom that may be an integer ID or a name string.

    Args:
        atom: The raw atom value from ``net_node.get_first_atom()``.
              May be an integer string (``"5"``), a net name (``"GND"``),
              an empty string, or ``None``.
        nets: Mapping of net number -> net name (for resolving int IDs to names).
        net_names: Mapping of net name -> net number (for resolving names to IDs).

    Returns:
        Tuple of ``(net_num, net_name)``.  If the atom cannot be resolved,
        returns ``(0, "")``.
    """
    if atom is None or atom == "":
        return (0, "")

    # Try integer parse first (KiCad 8 format)
    try:
        net_num = int(atom)
        net_name = nets.get(net_num, "") if nets else ""
        return (net_num, net_name)
    except (ValueError, TypeError):
        pass

    # KiCad 9 name-only format -- look up the integer from name
    net_name = str(atom)
    net_num = net_names.get(net_name, 0) if net_names else 0
    return (net_num, net_name)
