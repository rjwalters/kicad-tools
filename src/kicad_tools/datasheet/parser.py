"""
PDF datasheet parser with markdown conversion, image extraction, and table extraction.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from .images import ExtractedImage, classify_image
from .pin_inference import (
    apply_type_overrides,
    identify_column_type,
    infer_pin_type,
    is_pin_table,
)
from .pins import ExtractedPin, PinTable
from .tables import ExtractedTable

if TYPE_CHECKING:
    from fitz import Page as FitzPage


def _check_markitdown() -> None:
    """Check if markitdown is available."""
    try:
        import markitdown  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "markitdown is required for PDF to markdown conversion. "
            "Install with: pip install kicad-tools[datasheet]"
        ) from e


def _check_pymupdf() -> None:
    """Check if PyMuPDF is available."""
    try:
        import fitz  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "PyMuPDF is required for image extraction. "
            "Install with: pip install kicad-tools[datasheet]"
        ) from e


def _check_pdfplumber() -> None:
    """Check if pdfplumber is available."""
    try:
        import pdfplumber  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "pdfplumber is required for table extraction. "
            "Install with: pip install kicad-tools[datasheet]"
        ) from e


@dataclass
class ParsedDatasheet:
    """
    Container for all parsed datasheet content.

    Attributes:
        path: Path to the original PDF file
        page_count: Total number of pages in the PDF
        markdown: Full markdown conversion of the PDF
        images: List of extracted images
        tables: List of extracted tables
    """

    path: Path
    page_count: int
    markdown: str
    images: list[ExtractedImage]
    tables: list[ExtractedTable]


class DatasheetParser:
    """
    Parser for PDF datasheets with markdown conversion and content extraction.

    Usage:
        parser = DatasheetParser("STM32F103.pdf")

        # Convert to markdown
        markdown = parser.to_markdown()

        # Extract images
        images = parser.extract_images()
        for img in images:
            img.save(f"output/{img.suggested_filename}")

        # Extract tables
        tables = parser.extract_tables()
        for table in tables:
            print(table.to_markdown())
    """

    def __init__(self, path: str | Path) -> None:
        """
        Initialize the parser with a PDF file.

        Args:
            path: Path to the PDF file

        Raises:
            FileNotFoundError: If the PDF file does not exist
            ValueError: If the path is not a PDF file
        """
        self.path = Path(path)

        if not self.path.exists():
            raise FileNotFoundError(f"PDF file not found: {self.path}")

        if self.path.suffix.lower() != ".pdf":
            raise ValueError(f"Expected a PDF file, got: {self.path.suffix}")

        self._page_count: int | None = None
        self._markdown_cache: dict[str, str] = {}

    @property
    def page_count(self) -> int:
        """Get the total number of pages in the PDF."""
        if self._page_count is None:
            _check_pymupdf()
            import fitz

            with fitz.open(self.path) as doc:
                self._page_count = len(doc)

        return self._page_count

    def to_markdown(
        self,
        pages: Iterable[int] | None = None,
    ) -> str:
        """
        Convert the PDF to markdown format.

        Uses Microsoft's markitdown library for conversion.

        Args:
            pages: Optional page numbers to convert (1-indexed).
                   If None, converts the entire document.
                   Can be a list [1, 2, 3] or range(1, 10).

        Returns:
            Markdown-formatted string

        Note:
            Page filtering is done by extracting specific pages to a temp PDF.
            For large documents, consider using page ranges for memory efficiency.
        """
        _check_markitdown()
        from markitdown import MarkItDown

        # Generate cache key
        if pages is None:
            cache_key = "all"
        else:
            page_list = sorted(set(pages))
            cache_key = ",".join(str(p) for p in page_list)

        # Check cache
        if cache_key in self._markdown_cache:
            return self._markdown_cache[cache_key]

        md = MarkItDown()

        if pages is None:
            # Convert entire document
            result = md.convert(str(self.path))
            markdown = result.text_content
        else:
            # Convert specific pages by extracting to temp PDF
            page_list = sorted(set(pages))
            markdown = self._convert_pages_to_markdown(md, page_list)

        self._markdown_cache[cache_key] = markdown
        return markdown

    def _convert_pages_to_markdown(
        self,
        md: Any,
        pages: list[int],
    ) -> str:
        """Convert specific pages to markdown using a temp PDF."""
        _check_pymupdf()
        import tempfile

        import fitz

        # Create temp PDF with only selected pages
        with fitz.open(self.path) as src_doc:
            with fitz.open() as temp_doc:
                for page_num in pages:
                    # Convert 1-indexed to 0-indexed
                    idx = page_num - 1
                    if 0 <= idx < len(src_doc):
                        temp_doc.insert_pdf(src_doc, from_page=idx, to_page=idx)

                # Save to temp file and convert
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    temp_doc.save(tmp.name)
                    tmp_path = tmp.name

        try:
            result = md.convert(tmp_path)
            return result.text_content
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def extract_images(
        self,
        pages: Iterable[int] | None = None,
        min_width: int = 0,
        min_height: int = 0,
    ) -> list[ExtractedImage]:
        """
        Extract images from the PDF.

        Uses PyMuPDF (fitz) for image extraction.

        Args:
            pages: Optional page numbers to extract from (1-indexed).
                   If None, extracts from all pages.
            min_width: Minimum image width in pixels (filters small images)
            min_height: Minimum image height in pixels (filters small icons)

        Returns:
            List of ExtractedImage objects
        """
        _check_pymupdf()
        import fitz

        images: list[ExtractedImage] = []

        with fitz.open(self.path) as doc:
            # Determine which pages to process
            if pages is None:
                page_nums = range(1, len(doc) + 1)
            else:
                page_nums = pages

            for page_num in page_nums:
                # Convert to 0-indexed
                idx = page_num - 1
                if idx < 0 or idx >= len(doc):
                    continue

                page = doc[idx]
                img_list = page.get_images(full=True)

                for img_idx, img_info in enumerate(img_list):
                    xref = img_info[0]

                    try:
                        base_image = doc.extract_image(xref)
                    except Exception:
                        continue

                    width = base_image.get("width", 0)
                    height = base_image.get("height", 0)

                    # Apply size filter
                    if width < min_width or height < min_height:
                        continue

                    # Get image format
                    ext = base_image.get("ext", "png")
                    if ext == "jpeg":
                        ext = "jpg"

                    # Get image data
                    img_data = base_image.get("image", b"")
                    if not img_data:
                        continue

                    # Try to find caption (text near the image)
                    caption = self._find_image_caption(page, img_idx)

                    # Classify the image
                    classification = classify_image(width, height, caption)

                    images.append(
                        ExtractedImage(
                            page=page_num,
                            index=img_idx,
                            width=width,
                            height=height,
                            format=ext,
                            data=img_data,
                            caption=caption,
                            classification=classification,
                            _xref=xref,
                        )
                    )

        return images

    def _find_image_caption(
        self,
        page: FitzPage,
        img_idx: int,
    ) -> str | None:
        """Try to find caption text near an image."""
        # This is a simplified heuristic - looks for "Figure" or "Fig" text
        text = page.get_text("text")
        lines = text.split("\n")

        for line in lines:
            line_lower = line.lower().strip()
            if line_lower.startswith(("figure", "fig.", "fig ")):
                return line.strip()

        return None

    def extract_tables(
        self,
        pages: Iterable[int] | None = None,
    ) -> list[ExtractedTable]:
        """
        Extract tables from the PDF.

        Uses pdfplumber for table detection and extraction.

        Args:
            pages: Optional page numbers to extract from (1-indexed).
                   If None, extracts from all pages.

        Returns:
            List of ExtractedTable objects
        """
        _check_pdfplumber()
        import pdfplumber

        tables: list[ExtractedTable] = []

        with pdfplumber.open(self.path) as pdf:
            # Determine which pages to process
            if pages is None:
                page_nums = range(1, len(pdf.pages) + 1)
            else:
                page_nums = pages

            for page_num in page_nums:
                # Convert to 0-indexed
                idx = page_num - 1
                if idx < 0 or idx >= len(pdf.pages):
                    continue

                page = pdf.pages[idx]
                page_tables = page.extract_tables()

                for table_idx, raw_table in enumerate(page_tables):
                    if not raw_table:
                        continue

                    # Clean up the table data
                    cleaned_rows = []
                    for row in raw_table:
                        if row:
                            cleaned_row = [
                                str(cell).strip() if cell is not None else "" for cell in row
                            ]
                            cleaned_rows.append(cleaned_row)

                    if not cleaned_rows:
                        continue

                    # Try to detect headers (first row with content)
                    headers = cleaned_rows[0] if cleaned_rows else []
                    data_rows = cleaned_rows[1:] if len(cleaned_rows) > 1 else []

                    # Check if first row looks like headers
                    # (contains text that looks like labels rather than data)
                    if headers and all(
                        cell and not cell.replace(".", "").replace("-", "").isdigit()
                        for cell in headers
                        if cell
                    ):
                        # First row is likely headers
                        pass
                    else:
                        # First row is data, no headers detected
                        headers = []
                        data_rows = cleaned_rows

                    tables.append(
                        ExtractedTable(
                            page=page_num,
                            headers=headers,
                            rows=data_rows,
                            _index=table_idx,
                        )
                    )

        return tables

    def extract_pins(
        self,
        pages: Iterable[int] | None = None,
        package: str | None = None,
        type_overrides: dict[str, str] | None = None,
        as_dict: bool = False,
    ) -> PinTable | list[dict[str, Any]]:
        """
        Extract pin definitions from the datasheet.

        Searches for pin tables in the PDF and extracts pin information
        including automatic type inference based on pin names.

        Args:
            pages: Optional page numbers to search (1-indexed).
                   If None, searches all pages.
            package: Optional package name to filter results (e.g., "LQFP48").
                     If None, returns pins from the first/default pin table.
            type_overrides: Optional dictionary mapping pin numbers to KiCad types
                           to override automatic inference.
            as_dict: If True, return list of dictionaries instead of PinTable.

        Returns:
            PinTable object containing extracted pins, or list of dicts if as_dict=True.

        Example:
            >>> parser = DatasheetParser("STM32F103.pdf")
            >>> pins = parser.extract_pins()
            >>> for pin in pins:
            ...     print(f"Pin {pin.number}: {pin.name} ({pin.type})")
        """
        tables = self.extract_tables(pages)

        # Find pin tables
        pin_tables: list[tuple[ExtractedTable, float]] = []
        for table in tables:
            is_pin, confidence = is_pin_table(table.headers)
            if is_pin:
                pin_tables.append((table, confidence))

        if not pin_tables:
            # No pin tables found
            result = PinTable(
                pins=[],
                package=package,
                source_pages=[],
                extraction_method="table",
                confidence=0.0,
            )
            return [p.to_dict() for p in result.pins] if as_dict else result

        # Sort by confidence and use the best one
        pin_tables.sort(key=lambda x: x[1], reverse=True)
        best_table, table_confidence = pin_tables[0]

        # Extract pins from the table
        pins = self._extract_pins_from_table(best_table)

        # Apply type overrides if provided
        if type_overrides:
            apply_type_overrides(pins, type_overrides)

        result = PinTable(
            pins=pins,
            package=package,
            source_pages=[best_table.page],
            extraction_method="table",
            confidence=table_confidence,
        )

        return [p.to_dict() for p in result.pins] if as_dict else result

    def _extract_pins_from_table(
        self,
        table: ExtractedTable,
    ) -> list[ExtractedPin]:
        """
        Extract pin information from a single table.

        Args:
            table: ExtractedTable containing pin definitions

        Returns:
            List of ExtractedPin objects
        """
        # Identify column indices
        column_map: dict[str, int] = {}
        for idx, header in enumerate(table.headers):
            col_type = identify_column_type(header)
            if col_type and col_type not in column_map:
                column_map[col_type] = idx

        # We need at least number and name columns
        if "number" not in column_map or "name" not in column_map:
            return []

        pins: list[ExtractedPin] = []

        for row in table.rows:
            if not row:
                continue

            # Extract pin number
            num_idx = column_map["number"]
            if num_idx >= len(row):
                continue
            pin_number = str(row[num_idx]).strip()
            if not pin_number:
                continue

            # Extract pin name
            name_idx = column_map["name"]
            if name_idx >= len(row):
                continue
            pin_name = str(row[name_idx]).strip()
            if not pin_name:
                continue

            # Extract optional fields
            electrical_type = None
            if "type" in column_map:
                type_idx = column_map["type"]
                if type_idx < len(row):
                    electrical_type = str(row[type_idx]).strip() or None

            description = ""
            if "description" in column_map:
                desc_idx = column_map["description"]
                if desc_idx < len(row):
                    description = str(row[desc_idx]).strip()

            alt_functions: list[str] = []
            if "alt_functions" in column_map:
                alt_idx = column_map["alt_functions"]
                if alt_idx < len(row):
                    alt_str = str(row[alt_idx]).strip()
                    if alt_str:
                        # Split by common delimiters
                        alt_functions = [
                            f.strip()
                            for f in alt_str.replace("/", ",").replace(";", ",").split(",")
                            if f.strip()
                        ]

            # Infer pin type
            type_match = infer_pin_type(
                name=pin_name,
                electrical_type=electrical_type,
                description=description,
            )

            pins.append(
                ExtractedPin(
                    number=pin_number,
                    name=pin_name,
                    type=type_match.pin_type,
                    type_confidence=type_match.confidence,
                    type_source="inferred" if not electrical_type else "datasheet",
                    description=description,
                    alt_functions=alt_functions,
                    electrical_type=electrical_type,
                    source_page=table.page,
                )
            )

        return pins

    def list_packages(
        self,
        pages: Iterable[int] | None = None,
    ) -> list[str]:
        """
        List available packages mentioned in the datasheet.

        Searches for common package naming patterns (LQFP, BGA, QFN, etc.)
        in table headers and content.

        Args:
            pages: Optional page numbers to search (1-indexed).
                   If None, searches all pages.

        Returns:
            List of package names found (e.g., ["LQFP48", "LQFP64", "BGA100"])
        """
        import re

        tables = self.extract_tables(pages)
        packages: set[str] = set()

        # Common package patterns
        package_pattern = re.compile(
            r"\b(LQFP|QFP|TQFP|BGA|WLCSP|QFN|DFN|SOIC|SOP|SSOP|TSSOP|"
            r"MSOP|DIP|PDIP|PLCC|LGA|WSON|SON|SOT|TO-?\d+|"
            r"TSOP|PQFP|CQFP|CERDIP|CERQUAD)[\s-]?\d+",
            re.IGNORECASE,
        )

        for table in tables:
            # Check headers
            for header in table.headers:
                matches = package_pattern.findall(header)
                packages.update(m.upper() for m in matches)

            # Check first few rows
            for row in table.rows[:5]:
                for cell in row:
                    matches = package_pattern.findall(str(cell))
                    packages.update(m.upper() for m in matches)

        return sorted(packages)

    def extract_pins_dataframe(
        self,
        pages: Iterable[int] | None = None,
        package: str | None = None,
    ) -> Any:
        """
        Extract pins and return as a pandas DataFrame.

        This is a convenience method for users who prefer working with pandas.

        Args:
            pages: Optional page numbers to search (1-indexed).
            package: Optional package name to filter results.

        Returns:
            pandas DataFrame with pin data

        Raises:
            ImportError: If pandas is not installed
        """
        try:
            import pandas as pd
        except ImportError as e:
            raise ImportError(
                "pandas is required for DataFrame conversion. Install with: pip install pandas"
            ) from e

        pin_table = self.extract_pins(pages=pages, package=package)
        data = [p.to_dict() for p in pin_table.pins]
        return pd.DataFrame(data)

    def parse_all(
        self,
        pages: Iterable[int] | None = None,
        min_image_width: int = 100,
        min_image_height: int = 100,
    ) -> ParsedDatasheet:
        """
        Parse the entire datasheet, extracting all content.

        This is a convenience method that calls to_markdown(),
        extract_images(), and extract_tables().

        Args:
            pages: Optional page numbers to parse (1-indexed).
                   If None, parses the entire document.
            min_image_width: Minimum image width for extraction
            min_image_height: Minimum image height for extraction

        Returns:
            ParsedDatasheet containing all extracted content
        """
        markdown = self.to_markdown(pages)
        images = self.extract_images(pages, min_image_width, min_image_height)
        tables = self.extract_tables(pages)

        return ParsedDatasheet(
            path=self.path,
            page_count=self.page_count,
            markdown=markdown,
            images=images,
            tables=tables,
        )

    def __repr__(self) -> str:
        return f"DatasheetParser({self.path.name!r}, pages={self.page_count})"
