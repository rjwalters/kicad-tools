"""
KiCad Schematic Internal Helper Functions

Internal utility functions for string similarity, pin aliases, and formatting.
These are used by exception classes and other modules.
"""

from difflib import SequenceMatcher


def _string_similarity(a: str, b: str) -> float:
    """Calculate similarity ratio between two strings (0.0 to 1.0)."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _find_similar(
    target: str, candidates: list[str], threshold: float = 0.4, max_results: int = 5
) -> list[str]:
    """Find similar strings from a list of candidates.

    Args:
        target: The string to match against
        candidates: List of candidate strings
        threshold: Minimum similarity score (0.0 to 1.0)
        max_results: Maximum number of suggestions to return

    Returns:
        List of similar strings, sorted by similarity (best first)
    """
    scored = []
    target_lower = target.lower()

    for candidate in candidates:
        # Exact prefix match gets highest score
        if candidate.lower().startswith(target_lower):
            score = 0.9 + (len(target) / len(candidate)) * 0.1
        elif target_lower.startswith(candidate.lower()):
            score = 0.85
        else:
            score = _string_similarity(target, candidate)

        if score >= threshold:
            scored.append((candidate, score))

    scored.sort(key=lambda x: -x[1])
    return [s[0] for s in scored[:max_results]]


# Common pin name aliases (maps alternate names to canonical names)
# This helps agents find pins even when using slightly different naming conventions
PIN_ALIASES = {
    # Power pins
    "vcc": ["vdd", "v+", "vin", "vcc", "vbat", "vsup", "vpwr", "avcc", "dvcc", "vddio"],
    "vdd": ["vcc", "v+", "vin", "vdd", "vbat", "vsup", "vpwr", "avdd", "dvdd", "vddio"],
    "gnd": ["vss", "v-", "gnda", "gndd", "agnd", "dgnd", "ground", "com", "vee", "pgnd"],
    "vss": ["gnd", "v-", "gnda", "gndd", "agnd", "dgnd", "ground", "vee"],
    "avcc": ["avdd", "vcc", "vdd", "va"],
    "dvcc": ["dvdd", "vcc", "vdd", "vd"],
    "agnd": ["gnda", "gnd", "vss", "va-"],
    "dgnd": ["gndd", "gnd", "vss", "vd-"],
    # Enable/Chip Select pins
    "en": ["enable", "ena", "ce", "chip_enable", "~en", "en/", "oe", "stby"],
    "enable": ["en", "ena", "ce", "oe"],
    "ce": ["en", "enable", "cs", "chip_enable"],
    "cs": ["~cs", "cs/", "ncs", "ss", "~ss", "nss", "ce", "chip_select"],
    "ss": ["~ss", "nss", "cs", "~cs", "ncs", "slave_select"],
    "oe": ["~oe", "noe", "output_enable", "en"],
    # I2C pins
    "sda": ["data", "sdio", "i2c_sda", "twi_sda", "ser_data"],
    "scl": ["i2c_scl", "twi_scl", "i2c_clk", "ser_clk"],
    # SPI pins
    "sck": ["sclk", "clk", "clock", "spi_clk", "ser_clk"],
    "sclk": ["sck", "clk", "clock", "spi_clk"],
    "mosi": ["sdi", "din", "data_in", "si", "spi_mosi", "dout"],
    "miso": ["sdo", "dout", "data_out", "so", "spi_miso", "din"],
    "sdi": ["mosi", "din", "si", "data_in"],
    "sdo": ["miso", "dout", "so", "data_out"],
    # UART/Serial pins
    "tx": ["txd", "uart_tx", "ser_tx", "dout", "td"],
    "rx": ["rxd", "uart_rx", "ser_rx", "din", "rd"],
    "txd": ["tx", "uart_tx", "dout"],
    "rxd": ["rx", "uart_rx", "din"],
    "rts": ["~rts", "nrts", "uart_rts"],
    "cts": ["~cts", "ncts", "uart_cts"],
    # Clock pins
    "clk": ["clock", "sclk", "sck", "bclk", "mclk", "clkin", "xin", "osc_in"],
    "mclk": ["master_clk", "clk", "clock", "xtal"],
    "bclk": ["bit_clk", "sclk", "i2s_bclk"],
    "lrclk": ["wclk", "ws", "lrck", "i2s_lrclk", "frame_sync", "fs"],
    "wclk": ["lrclk", "ws", "lrck", "word_clk"],
    # Reset pins
    "rst": ["reset", "~reset", "nreset", "~rst", "rstn", "mrst", "por"],
    "reset": ["rst", "~reset", "nreset", "~rst", "rstn"],
    "nreset": ["~reset", "rstn", "rst", "reset"],
    # Interrupt pins
    "int": ["~int", "irq", "~irq", "interrupt", "intr"],
    "irq": ["int", "~int", "interrupt", "~irq"],
    # Audio I2S pins
    "dout": ["sdo", "data_out", "i2s_dout", "sdout"],
    "din": ["sdi", "data_in", "i2s_din", "sdin"],
}


def _expand_pin_aliases(name: str) -> list[str]:
    """Get a list of possible alias names for a pin."""
    name_lower = name.lower().replace("~", "").replace("/", "")
    aliases = PIN_ALIASES.get(name_lower, [])
    return [name] + [a for a in aliases if a != name_lower]


def _group_pins_by_type(pins: list) -> dict[str, list]:
    """Group pins by their electrical type for organized display."""
    groups = {
        "power": [],
        "input": [],
        "output": [],
        "bidirectional": [],
        "passive": [],
        "other": [],
    }

    type_mapping = {
        "power_in": "power",
        "power_out": "power",
        "input": "input",
        "output": "output",
        "bidirectional": "bidirectional",
        "tri_state": "output",
        "passive": "passive",
        "unspecified": "other",
        "open_collector": "output",
        "open_emitter": "output",
        "no_connect": "other",
    }

    for pin in pins:
        group = type_mapping.get(pin.pin_type, "other")
        groups[group].append(pin)

    # Remove empty groups
    return {k: v for k, v in groups.items() if v}


def _format_pin_list(pins: list, indent: str = "  ") -> str:
    """Format a list of pins for display in error messages."""
    if not pins:
        return f"{indent}(none)"

    lines = []
    for pin in pins:
        if pin.name and pin.name != pin.number:
            lines.append(f"{indent}{pin.name} (pin {pin.number})")
        else:
            lines.append(f"{indent}pin {pin.number}")
    return "\n".join(lines)


def _fmt_coord(val: float) -> str:
    """Format a coordinate value with consistent precision.

    Rounds to 2 decimal places and removes trailing zeros for cleaner output.
    This ensures wire endpoints match pin positions exactly.
    """
    rounded = round(val, 2)
    # Format with up to 2 decimal places, remove trailing zeros
    if rounded == int(rounded):
        return str(int(rounded))
    else:
        return f"{rounded:.2f}".rstrip("0").rstrip(".")
