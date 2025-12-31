"""
File I/O utilities for KiCad S-expression files.
"""

from pathlib import Path

from kicad_tools.exceptions import FileFormatError
from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.sexp import SExp, parse_sexp, serialize_sexp


def load_schematic(path: str | Path) -> SExp:
    """
    Load a KiCad schematic file.

    Args:
        path: Path to .kicad_sch file

    Returns:
        Parsed SExp tree

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If file is not a valid schematic
    """
    path = Path(path)
    if not path.exists():
        raise KiCadFileNotFoundError(
            "Schematic file not found",
            context={"file": str(path)},
            suggestions=[
                "Check that the file path is correct",
                "Ensure the file has a .kicad_sch extension",
            ],
        )

    text = path.read_text(encoding="utf-8")
    sexp = parse_sexp(text)

    if sexp.tag != "kicad_sch":
        raise FileFormatError(
            "Not a KiCad schematic file",
            context={"file": str(path), "expected": "kicad_sch", "got": sexp.tag},
            suggestions=["This file appears to be a different KiCad file type"],
        )

    return sexp


def save_schematic(sexp: SExp, path: str | Path) -> None:
    """
    Save a KiCad schematic file.

    Args:
        sexp: The schematic SExp tree
        path: Path to save to

    Raises:
        ValueError: If sexp is not a valid schematic
    """
    if sexp.tag != "kicad_sch":
        raise FileFormatError(
            "Not a KiCad schematic",
            context={"expected": "kicad_sch", "got": sexp.tag},
        )

    path = Path(path)
    text = serialize_sexp(sexp)
    path.write_text(text, encoding="utf-8")


def load_symbol_lib(path: str | Path) -> SExp:
    """
    Load a KiCad symbol library file.

    Args:
        path: Path to .kicad_sym file

    Returns:
        Parsed SExp tree
    """
    path = Path(path)
    if not path.exists():
        raise KiCadFileNotFoundError(
            "Symbol library not found",
            context={"file": str(path)},
            suggestions=[
                "Check that the file path is correct",
                "Ensure the file has a .kicad_sym extension",
            ],
        )

    text = path.read_text(encoding="utf-8")
    sexp = parse_sexp(text)

    if sexp.tag != "kicad_symbol_lib":
        raise FileFormatError(
            "Not a KiCad symbol library",
            context={"file": str(path), "expected": "kicad_symbol_lib", "got": sexp.tag},
            suggestions=["This file appears to be a different KiCad file type"],
        )

    return sexp


def save_symbol_lib(sexp: SExp, path: str | Path) -> None:
    """
    Save a KiCad symbol library file.

    Args:
        sexp: The symbol library SExp tree
        path: Path to save to
    """
    if sexp.tag != "kicad_symbol_lib":
        raise FileFormatError(
            "Not a KiCad symbol library",
            context={"expected": "kicad_symbol_lib", "got": sexp.tag},
        )

    path = Path(path)
    text = serialize_sexp(sexp)
    path.write_text(text, encoding="utf-8")


def load_pcb(path: str | Path) -> SExp:
    """
    Load a KiCad PCB file.

    Args:
        path: Path to .kicad_pcb file

    Returns:
        Parsed SExp tree

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If file is not a valid PCB
    """
    path = Path(path)
    if not path.exists():
        raise KiCadFileNotFoundError(
            "PCB file not found",
            context={"file": str(path)},
            suggestions=[
                "Check that the file path is correct",
                "Ensure the file has a .kicad_pcb extension",
            ],
        )

    text = path.read_text(encoding="utf-8")
    sexp = parse_sexp(text)

    if sexp.tag != "kicad_pcb":
        raise FileFormatError(
            "Not a KiCad PCB file",
            context={"file": str(path), "expected": "kicad_pcb", "got": sexp.tag},
            suggestions=["This file appears to be a different KiCad file type"],
        )

    return sexp


def save_pcb(sexp: SExp, path: str | Path) -> None:
    """
    Save a KiCad PCB file.

    Args:
        sexp: The PCB SExp tree
        path: Path to save to

    Raises:
        ValueError: If sexp is not a valid PCB
    """
    if sexp.tag != "kicad_pcb":
        raise FileFormatError(
            "Not a KiCad PCB",
            context={"expected": "kicad_pcb", "got": sexp.tag},
        )

    path = Path(path)
    text = serialize_sexp(sexp)
    path.write_text(text, encoding="utf-8")


def load_footprint(path: str | Path) -> SExp:
    """
    Load a KiCad footprint file.

    Args:
        path: Path to .kicad_mod file

    Returns:
        Parsed SExp tree

    Raises:
        FileNotFoundError: If file doesn't exist
        FileFormatError: If file is not a valid footprint

    Note:
        Supports both KiCad 5 ("module") and KiCad 6+ ("footprint") formats.
    """
    path = Path(path)
    if not path.exists():
        raise KiCadFileNotFoundError(
            "Footprint file not found",
            context={"file": str(path)},
            suggestions=[
                "Check that the file path is correct",
                "Ensure the file has a .kicad_mod extension",
            ],
        )

    text = path.read_text(encoding="utf-8")
    sexp = parse_sexp(text)

    # KiCad 5 uses "module", KiCad 6+ uses "footprint"
    if sexp.tag not in ("module", "footprint"):
        raise FileFormatError(
            "Not a KiCad footprint file",
            context={
                "file": str(path),
                "expected": "module or footprint",
                "got": sexp.tag,
            },
            suggestions=["This file appears to be a different KiCad file type"],
        )

    return sexp


def save_footprint(sexp: SExp, path: str | Path) -> None:
    """
    Save a KiCad footprint file.

    Args:
        sexp: The footprint SExp tree
        path: Path to save to

    Raises:
        FileFormatError: If sexp is not a valid footprint
    """
    # KiCad 5 uses "module", KiCad 6+ uses "footprint"
    if sexp.tag not in ("module", "footprint"):
        raise FileFormatError(
            "Not a KiCad footprint",
            context={"expected": "module or footprint", "got": sexp.tag},
        )

    path = Path(path)
    text = serialize_sexp(sexp)
    path.write_text(text, encoding="utf-8")


def load_design_rules(path: str | Path) -> SExp:
    """
    Load a KiCad design rules file.

    Args:
        path: Path to .kicad_dru file

    Returns:
        Parsed SExp tree containing design rules

    Raises:
        FileNotFoundError: If file doesn't exist
        FileFormatError: If file is not valid design rules format

    Note:
        Design rules files contain version and rule definitions.
        The root tag is "version" for the first element.
    """
    path = Path(path)
    if not path.exists():
        raise KiCadFileNotFoundError(
            "Design rules file not found",
            context={"file": str(path)},
            suggestions=[
                "Check that the file path is correct",
                "Ensure the file has a .kicad_dru extension",
            ],
        )

    text = path.read_text(encoding="utf-8")

    # DRU files are a sequence of S-expressions, wrap in a container
    # to parse as a single tree
    wrapped_text = f"(design_rules {text})"
    sexp = parse_sexp(wrapped_text)

    # Validate structure - should have version as first child
    if not sexp.values:
        raise FileFormatError(
            "Empty design rules file",
            context={"file": str(path)},
            suggestions=["Design rules file should contain at least a version"],
        )

    first_child = sexp.values[0]
    if not isinstance(first_child, SExp) or first_child.tag != "version":
        raise FileFormatError(
            "Invalid design rules file",
            context={"file": str(path), "expected": "version", "got": str(first_child)},
            suggestions=[
                "Design rules file should start with (version N)",
                "Use 'kct mfr export-dru' to generate a valid file",
            ],
        )

    return sexp


def save_design_rules(sexp: SExp, path: str | Path) -> None:
    """
    Save a KiCad design rules file.

    Args:
        sexp: The design rules SExp tree (with design_rules root)
        path: Path to save to

    Raises:
        FileFormatError: If sexp is not valid design rules
    """
    if sexp.tag != "design_rules":
        raise FileFormatError(
            "Not a design rules container",
            context={"expected": "design_rules", "got": sexp.tag},
        )

    path = Path(path)
    # Serialize without the wrapper - just the contents
    lines = []
    for child in sexp.values:
        lines.append(serialize_sexp(child))
    text = "\n".join(lines)
    path.write_text(text, encoding="utf-8")
