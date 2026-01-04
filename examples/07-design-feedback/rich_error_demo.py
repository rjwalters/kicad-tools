#!/usr/bin/env python3
"""
Rich Error Diagnostics Demo

Demonstrates how kicad-tools v0.7.0 provides compiler-style error reporting
with source positions, code snippets, and actionable fix suggestions.
"""

from pathlib import Path

from kicad_tools.exceptions import (
    ErrorAccumulator,
    KiCadDiagnostic,
    KiCadToolsError,
    SExpSnippetExtractor,
    SourcePosition,
    ValidationErrorGroup,
)


def main():
    """Run the rich error diagnostics demo."""
    print("=" * 60)
    print("Rich Error Diagnostics Demo (v0.7.0)")
    print("=" * 60)
    print()

    # Demo 1: Source Position Tracking
    demo_source_positions()

    # Demo 2: S-expression Snippet Extraction
    demo_snippet_extraction()

    # Demo 3: Error Accumulation
    demo_error_accumulation()

    # Demo 4: Rich Terminal Output
    demo_rich_output()


def demo_source_positions():
    """Demonstrate source position tracking."""
    print("1. Source Position Tracking")
    print("-" * 40)
    print()

    # Create a diagnostic with full position information
    pos = SourcePosition(
        file="my_board.kicad_pcb",
        line=1234,
        column=15,
        element_type="pad",
        reference="U1",
        board_x=45.5,
        board_y=32.0,
        layer="F.Cu",
    )

    diag = KiCadDiagnostic(
        message="Clearance violation: pad too close to trace",
        position=pos,
        severity="error",
        code="DRC001",
        suggestion="Move pad 0.15mm away or reduce trace width",
    )

    print(f"Error location: {diag.position.file}:{diag.position.line}:{diag.position.column}")
    print(f"Board coords: ({diag.position.board_x}, {diag.position.board_y})mm on {diag.position.layer}")
    print(f"Component: {diag.position.reference} ({diag.position.element_type})")
    print()
    print(f"[{diag.code}] {diag.message}")
    print(f"Suggestion: {diag.suggestion}")
    print()


def demo_snippet_extraction():
    """Demonstrate S-expression snippet extraction."""
    print("2. S-expression Snippet Extraction")
    print("-" * 40)
    print()

    # Example S-expression content (simplified KiCad format)
    content = '''(kicad_pcb (version 20230101)
  (general (thickness 1.6))
  (footprint "Package_SO:SOIC-8"
    (at 45.5 32.0)
    (pad 1 smd rect (at -2.7 0) (size 1.5 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask")
    )
    (pad 2 smd rect (at -2.7 1.27) (size 1.5 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask")
    )
  )
)'''

    extractor = SExpSnippetExtractor(content)

    # Extract snippet around line 6 (the problematic pad)
    snippet = extractor.extract_lines(line=6, context=2)

    print("Code snippet around error location:")
    print()
    for line in snippet:
        print(f"  {line}")
    print()

    print("The snippet shows exact context with line numbers,")
    print("making it easy to locate and fix the issue.")
    print()


def demo_error_accumulation():
    """Demonstrate collecting multiple errors."""
    print("3. Error Accumulation")
    print("-" * 40)
    print()

    # Create an error accumulator for batch validation
    accumulator = ErrorAccumulator()

    # Simulate finding multiple errors during validation
    errors = [
        ("DRC001", "Clearance violation at U1 pad 3", "Move component 0.2mm"),
        ("DRC002", "Via too close to board edge", "Move via inward by 0.5mm"),
        ("DRC003", "Trace width below minimum (0.15mm)", "Increase to 0.2mm"),
        ("ERC001", "Unconnected pin: U2.VCC", "Add connection to power net"),
    ]

    for code, msg, suggestion in errors:
        accumulator.add_error(
            KiCadToolsError(
                message=msg,
                error_code=code,
                suggestion=suggestion,
            )
        )

    print(f"Errors collected: {len(accumulator.errors)}")
    print()

    # Display all errors at once
    for i, error in enumerate(accumulator.errors, 1):
        print(f"  {i}. [{error.error_code}] {error.message}")
        print(f"     Fix: {error.suggestion}")
    print()

    print("Error accumulation allows batch validation instead of")
    print("stopping at the first error, giving a complete picture.")
    print()


def demo_rich_output():
    """Demonstrate rich terminal rendering."""
    print("4. Rich Terminal Output")
    print("-" * 40)
    print()

    print("When running via CLI, errors are displayed with:")
    print()
    print("  - Color-coded severity (red=error, yellow=warning)")
    print("  - Syntax-highlighted code snippets")
    print("  - Visual markers pointing to exact location")
    print("  - Grouped by file for easy navigation")
    print()

    # Show example of what rich output looks like (in plain text)
    print("Example CLI output:")
    print()
    print("  \033[1;31merror[DRC001]\033[0m: Clearance violation")
    print("    \033[36m--> my_board.kicad_pcb:1234:15\033[0m")
    print("       |")
    print("  1233 |   (pad 1 smd rect (at -2.7 0) (size 1.5 0.6)")
    print("  \033[1;31m1234\033[0m | \033[1;31m    (layers \"F.Cu\" \"F.Paste\" \"F.Mask\")\033[0m")
    print("       | \033[1;31m              ^^^^^\033[0m")
    print("  1235 |   )")
    print("       |")
    print("    \033[32m= suggestion\033[0m: Move pad 0.15mm away from trace")
    print()

    print("Run `kct check board.kicad_pcb` to see rich error output.")
    print()


if __name__ == "__main__":
    main()
