"""
Extracted table data model and utilities.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass


@dataclass
class ExtractedTable:
    """
    Represents a table extracted from a PDF datasheet.

    Attributes:
        page: Page number where the table was found (1-indexed)
        headers: List of column headers
        rows: List of rows, each row is a list of cell values
        bbox: Bounding box coordinates (x0, y0, x1, y1) if available
    """

    page: int
    headers: list[str]
    rows: list[list[str]]
    bbox: tuple[float, float, float, float] | None = None
    _index: int = field(default=0, repr=False)  # Table index on page

    @property
    def cols(self) -> int:
        """Number of columns in the table."""
        if self.headers:
            return len(self.headers)
        if self.rows:
            return max(len(row) for row in self.rows)
        return 0

    @property
    def row_count(self) -> int:
        """Number of data rows (excluding header)."""
        return len(self.rows)

    def to_markdown(self) -> str:
        """
        Convert the table to markdown format.

        Returns:
            Markdown-formatted table string
        """
        if not self.headers and not self.rows:
            return ""

        lines = []

        # Use headers if available, otherwise use first row
        header_row = self.headers if self.headers else (self.rows[0] if self.rows else [])
        data_rows = self.rows if self.headers else self.rows[1:]

        if header_row:
            # Header row
            lines.append("| " + " | ".join(str(h) for h in header_row) + " |")
            # Separator
            lines.append("| " + " | ".join("---" for _ in header_row) + " |")

        # Data rows
        for row in data_rows:
            # Pad row if shorter than headers
            padded_row = list(row) + [""] * (len(header_row) - len(row))
            lines.append("| " + " | ".join(str(c) for c in padded_row[: len(header_row)]) + " |")

        return "\n".join(lines)

    def to_csv(self) -> str:
        """
        Convert the table to CSV format.

        Returns:
            CSV-formatted string
        """
        output = io.StringIO()
        writer = csv.writer(output)

        if self.headers:
            writer.writerow(self.headers)

        for row in self.rows:
            writer.writerow(row)

        return output.getvalue()

    def to_dict(self) -> dict[str, Any]:
        """
        Convert the table to a dictionary.

        Returns:
            Dictionary with 'headers', 'rows', and 'page' keys
        """
        return {
            "page": self.page,
            "headers": self.headers,
            "rows": self.rows,
        }

    def to_json(self) -> str:
        """
        Convert the table to JSON format.

        Returns:
            JSON-formatted string
        """
        return json.dumps(self.to_dict(), indent=2)

    def to_dataframe(self) -> Any:
        """
        Convert the table to a pandas DataFrame.

        Returns:
            pandas DataFrame

        Raises:
            ImportError: If pandas is not installed
        """
        try:
            import pandas as pd
        except ImportError as e:
            raise ImportError(
                "pandas is required for DataFrame conversion. Install with: pip install pandas"
            ) from e

        if self.headers:
            return pd.DataFrame(self.rows, columns=self.headers)
        return pd.DataFrame(self.rows)

    def __repr__(self) -> str:
        return f"ExtractedTable(page={self.page}, {self.row_count} rows x {self.cols} cols)"
