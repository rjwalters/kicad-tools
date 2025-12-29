"""
File I/O utilities for KiCad S-expression files.
"""

from pathlib import Path
from typing import Union

from .sexp import SExp, parse_sexp, serialize_sexp


def load_schematic(path: Union[str, Path]) -> SExp:
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
        raise FileNotFoundError(f"Schematic not found: {path}")

    text = path.read_text(encoding="utf-8")
    sexp = parse_sexp(text)

    if sexp.tag != "kicad_sch":
        raise ValueError(f"Not a KiCad schematic: expected 'kicad_sch', got '{sexp.tag}'")

    return sexp


def save_schematic(sexp: SExp, path: Union[str, Path]) -> None:
    """
    Save a KiCad schematic file.

    Args:
        sexp: The schematic SExp tree
        path: Path to save to

    Raises:
        ValueError: If sexp is not a valid schematic
    """
    if sexp.tag != "kicad_sch":
        raise ValueError(f"Not a KiCad schematic: expected 'kicad_sch', got '{sexp.tag}'")

    path = Path(path)
    text = serialize_sexp(sexp)
    path.write_text(text, encoding="utf-8")


def load_symbol_lib(path: Union[str, Path]) -> SExp:
    """
    Load a KiCad symbol library file.

    Args:
        path: Path to .kicad_sym file

    Returns:
        Parsed SExp tree
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Symbol library not found: {path}")

    text = path.read_text(encoding="utf-8")
    sexp = parse_sexp(text)

    if sexp.tag != "kicad_symbol_lib":
        raise ValueError(
            f"Not a KiCad symbol library: expected 'kicad_symbol_lib', got '{sexp.tag}'"
        )

    return sexp


def save_symbol_lib(sexp: SExp, path: Union[str, Path]) -> None:
    """
    Save a KiCad symbol library file.

    Args:
        sexp: The symbol library SExp tree
        path: Path to save to
    """
    if sexp.tag != "kicad_symbol_lib":
        raise ValueError(
            f"Not a KiCad symbol library: expected 'kicad_symbol_lib', got '{sexp.tag}'"
        )

    path = Path(path)
    text = serialize_sexp(sexp)
    path.write_text(text, encoding="utf-8")


def load_pcb(path: Union[str, Path]) -> SExp:
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
        raise FileNotFoundError(f"PCB not found: {path}")

    text = path.read_text(encoding="utf-8")
    sexp = parse_sexp(text)

    if sexp.tag != "kicad_pcb":
        raise ValueError(f"Not a KiCad PCB: expected 'kicad_pcb', got '{sexp.tag}'")

    return sexp


def save_pcb(sexp: SExp, path: Union[str, Path]) -> None:
    """
    Save a KiCad PCB file.

    Args:
        sexp: The PCB SExp tree
        path: Path to save to

    Raises:
        ValueError: If sexp is not a valid PCB
    """
    if sexp.tag != "kicad_pcb":
        raise ValueError(f"Not a KiCad PCB: expected 'kicad_pcb', got '{sexp.tag}'")

    path = Path(path)
    text = serialize_sexp(sexp)
    path.write_text(text, encoding="utf-8")
