"""KiCad schematic operations."""

from .net_ops import (
    Net,
    NetConnection,
    NetTracer,
    find_net,
    trace_nets,
)
from .netlist import (
    ComponentPin,
    Netlist,
    NetlistComponent,
    NetlistNet,
    export_netlist,
    find_kicad_cli,
)
from .pinmap import (
    MappingResult,
    Pin,
    PinMapping,
    compare_schematic_symbols,
    compare_symbols,
)
from .symbol_ops import (
    SymbolReplacement,
    add_symbol_pin,
    clear_symbol_pins,
    create_replacement_symbol,
    find_symbol_by_reference,
    get_symbol_lib_id,
    get_symbol_pins,
    replace_symbol_lib_id,
    update_symbol_pins,
)

__all__ = [
    # net_ops
    "Net",
    "NetConnection",
    "NetTracer",
    "find_net",
    "trace_nets",
    # netlist
    "ComponentPin",
    "Netlist",
    "NetlistComponent",
    "NetlistNet",
    "export_netlist",
    "find_kicad_cli",
    # pinmap
    "MappingResult",
    "Pin",
    "PinMapping",
    "compare_schematic_symbols",
    "compare_symbols",
    # symbol_ops
    "SymbolReplacement",
    "add_symbol_pin",
    "clear_symbol_pins",
    "create_replacement_symbol",
    "find_symbol_by_reference",
    "get_symbol_lib_id",
    "get_symbol_pins",
    "replace_symbol_lib_id",
    "update_symbol_pins",
]
