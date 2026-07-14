"""
Base interface for datasheet sources.
"""

from __future__ import annotations

import logging
import os
import tempfile
from abc import ABC, abstractmethod
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, TypeVar

from kicad_tools.utils import ensure_parent_dir

if TYPE_CHECKING:
    from ..models import DatasheetResult

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable)

# PDF files begin with the magic bytes "%PDF-" (per the PDF spec these must
# appear within the first 1024 bytes; in practice every source this project
# talks to places them at byte 0). We validate this before treating a
# streamed response body as a successful download so that HTML wrapper/SPA
# pages returned with HTTP 200 are never written to disk as ".pdf".
PDF_MAGIC = b"%PDF-"


def _validate_pdf_payload(
    leading_bytes: bytes,
    source_name: str,
    url: str,
    content_type: str | None = None,
) -> None:
    """
    Validate that a downloaded payload is actually a PDF.

    Args:
        leading_bytes: The first bytes of the response body (at least the first
            chunk). Only the leading portion is needed to check the magic bytes.
        source_name: Name of the source the payload came from (for error text).
        url: The URL the payload was fetched from (for error text).
        content_type: The response ``Content-Type`` header, if available. Used
            only to enrich the error message; the magic-byte check is the
            authoritative signal.

    Raises:
        DatasheetDownloadError: If the payload does not start with the PDF
            magic bytes.
    """
    from ..exceptions import DatasheetDownloadError

    if leading_bytes[: len(PDF_MAGIC)] == PDF_MAGIC:
        return

    snippet = leading_bytes[:32].decode("utf-8", errors="replace").replace("\n", " ")
    ct_part = f"Content-Type: {content_type}, " if content_type else ""
    raise DatasheetDownloadError(
        f"{source_name} returned non-PDF content from {url} ({ct_part}first bytes: {snippet!r})"
    )


def _stream_to_validated_pdf(
    response: Any,
    output_path: Path,
    source_name: str,
    url: str,
    content_type: str | None = None,
    chunk_size: int = 8192,
) -> None:
    """
    Stream a response body to ``output_path`` only if it is a valid PDF.

    The payload is written to a temporary file first; its leading bytes are
    checked against the PDF magic bytes. Only if validation passes is the temp
    file atomically moved into place. A non-PDF payload raises
    ``DatasheetDownloadError`` and never leaves a file at ``output_path``.

    Args:
        response: A streaming ``requests`` response (supports ``iter_content``).
        output_path: Final destination for the validated PDF.
        source_name: Source name for error messages.
        url: Source URL for error messages.
        content_type: Response ``Content-Type`` header, if available.
        chunk_size: Chunk size for streaming.

    Raises:
        DatasheetDownloadError: If the payload is not a PDF.
    """
    output_path = Path(output_path)
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(output_path.parent),
        prefix=f".{output_path.name}.",
        suffix=".partial",
    )
    tmp_path = Path(tmp_name)
    validated = False
    try:
        leading = b""
        with os.fdopen(tmp_fd, "wb") as f:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                if not validated:
                    leading += chunk
                    if len(leading) >= len(PDF_MAGIC):
                        _validate_pdf_payload(leading, source_name, url, content_type)
                        validated = True
                f.write(chunk)

        # Payloads shorter than the magic-byte prefix (e.g. an empty body)
        # were never validated above — validate whatever we captured now.
        if not validated:
            _validate_pdf_payload(leading, source_name, url, content_type)

        os.replace(tmp_path, output_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                logger.debug("Failed to remove temp download file %s", tmp_path)


def requires_requests(func: F) -> F:
    """
    Decorator to check if requests library is available.

    Raises ImportError with installation instructions if requests is not installed.
    Used by datasheet sources that need HTTP functionality.
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            import requests  # noqa: F401
        except ImportError:
            raise ImportError(
                "The 'requests' library is required for datasheet operations. "
                "Install with: pip install kicad-tools[parts]"
            )
        return func(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


class DatasheetSource(ABC):
    """
    Abstract base class for datasheet sources.

    Each source implementation provides search and download functionality
    for a specific datasheet provider (LCSC, Octopart, DigiKey, etc.).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """The name of this datasheet source."""
        ...

    @abstractmethod
    def search(self, part_number: str) -> list[DatasheetResult]:
        """
        Search for datasheets matching the part number.

        Args:
            part_number: Part number or search query

        Returns:
            List of matching DatasheetResult objects
        """
        ...

    @abstractmethod
    def download(self, result: DatasheetResult, output_path: Path) -> Path:
        """
        Download a datasheet to the specified path.

        Args:
            result: The DatasheetResult to download
            output_path: Where to save the file

        Returns:
            Path to the downloaded file

        Raises:
            DatasheetDownloadError: If download fails
        """
        ...

    def _download_file(
        self,
        session: Any,
        url: str,
        output_path: Path,
        timeout: float,
    ) -> Path:
        """
        Download a file from a URL using the provided session.

        This is a shared helper method that handles the common HTTP download
        logic used by multiple datasheet sources.

        Args:
            session: A requests.Session object to use for the download
            url: The URL to download from
            output_path: Where to save the file
            timeout: Request timeout in seconds

        Returns:
            Path to the downloaded file

        Raises:
            DatasheetDownloadError: If download fails
        """
        import requests

        from ..exceptions import DatasheetDownloadError

        try:
            response = session.get(
                url,
                timeout=timeout,
                stream=True,
                allow_redirects=True,
            )
            response.raise_for_status()

            ensure_parent_dir(output_path)

            content_type = response.headers.get("Content-Type") if response.headers else None
            _stream_to_validated_pdf(
                response=response,
                output_path=output_path,
                source_name=self.name,
                url=url,
                content_type=content_type,
            )

            logger.info(f"Downloaded datasheet to {output_path}")
            return output_path

        except requests.RequestException as e:
            raise DatasheetDownloadError(f"Failed to download datasheet from {url}: {e}") from e

    def __str__(self) -> str:
        return self.name


class HTTPDatasheetSource(DatasheetSource):
    """
    Base class for HTTP-based datasheet sources.

    Provides common session management, download functionality,
    and context manager support for sources that use HTTP requests.

    Subclasses must implement:
        - name: The source name
        - search: Part number search logic
        - _get_default_headers: HTTP headers for requests
    """

    def __init__(self, timeout: float = 30.0):
        """
        Initialize the HTTP datasheet source.

        Args:
            timeout: Request timeout in seconds
        """
        self.timeout = timeout
        self._session = None

    @abstractmethod
    def _get_default_headers(self) -> dict[str, str]:
        """
        Get default HTTP headers for requests.

        Returns:
            Dictionary of HTTP headers
        """
        ...

    def _get_session(self):
        """Get or create requests session with default headers."""
        if self._session is None:
            import requests

            self._session = requests.Session()
            self._session.headers.update(self._get_default_headers())
        return self._session

    @requires_requests
    def download(self, result: DatasheetResult, output_path: Path) -> Path:
        """
        Download a datasheet to the specified path.

        Args:
            result: The DatasheetResult to download
            output_path: Where to save the file

        Returns:
            Path to the downloaded file

        Raises:
            DatasheetDownloadError: If download fails
        """
        import requests

        from ..exceptions import DatasheetDownloadError

        session = self._get_session()

        try:
            response = session.get(
                result.datasheet_url,
                timeout=self.timeout,
                stream=True,
                allow_redirects=True,
            )
            response.raise_for_status()

            # Ensure parent directory exists
            output_path.parent.mkdir(parents=True, exist_ok=True)

            # Validate the payload is actually a PDF before it lands on disk.
            # LCSC's SPA returns HTTP 200 with an HTML body; without this guard
            # it would be written as ".pdf" and reported as a success.
            content_type = response.headers.get("Content-Type") if response.headers else None
            _stream_to_validated_pdf(
                response=response,
                output_path=output_path,
                source_name=self.name,
                url=result.datasheet_url,
                content_type=content_type,
            )

            logger.info(f"Downloaded datasheet to {output_path}")
            return output_path

        except requests.RequestException as e:
            raise DatasheetDownloadError(
                f"Failed to download datasheet from {result.datasheet_url}: {e}"
            ) from e

    def close(self) -> None:
        """Close the HTTP session."""
        if self._session:
            self._session.close()
            self._session = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
