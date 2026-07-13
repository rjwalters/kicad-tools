"""Programmatic builder for a tiny jlcparts-schema fixture SQLite.

The real ``yaqwsx/jlcparts`` dataset is hundreds of MB and must never be
committed or downloaded in CI. Instead, tests build a small hand-curated
SQLite database (a handful of rows) that mirrors the ``jlc_components`` schema
the offline-catalog reader targets. This keeps the fixture in code (reviewable,
diffable, no binary blob) while exercising the real column-to-``Part``
translation path.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

# A few hand-curated rows spanning basic/preferred/extended library types,
# SMD and through-hole packages, and present/absent price + datasheet fields.
SAMPLE_ROWS = [
    {
        "lcsc": 25804,  # -> C25804
        "mfr": "RC0402FR-0710KL",
        "package": "0402",
        "manufacturer": "YAGEO",
        "library_type": "basic",
        "description": "10kOhms 1% 0402 Chip Resistor",
        "datasheet": "https://example.com/rc0402.pdf",
        "stock": 500000,
        "price": json.dumps(
            [
                {"qFrom": 10, "qTo": 100, "price": 0.005},
                {"qFrom": 100, "qTo": None, "price": 0.002},
            ]
        ),
    },
    {
        "lcsc": 1525,  # -> C1525
        "mfr": "CL10B104KB8NNNC",
        "package": "0402",
        "manufacturer": "Samsung",
        "library_type": "basic",
        "description": "100nF 50V X7R 0402 Multilayer Ceramic Capacitor MLCC",
        "datasheet": "https://example.com/cl10b104.pdf",
        "stock": 1000000,
        "price": json.dumps([{"qFrom": 20, "qTo": None, "price": 0.0018}]),
    },
    {
        "lcsc": 8734,  # -> C8734
        "mfr": "STM32F103C8T6",
        "package": "LQFP-48",
        "manufacturer": "STMicroelectronics",
        "library_type": "extended",
        "description": "ARM Cortex-M3 MCU 32-bit Microcontroller LQFP-48",
        "datasheet": "https://example.com/stm32f103.pdf",
        "stock": 1200,
        "price": json.dumps([{"qFrom": 1, "qTo": None, "price": 1.85}]),
    },
    {
        "lcsc": 100,  # -> C100, preferred, no prices, no datasheet
        "mfr": "GENERIC-PREF",
        "package": "SOT-23",
        "manufacturer": "GenericCo",
        "library_type": "preferred",
        "description": "NPN Transistor SOT-23",
        "datasheet": "",
        "stock": 0,
        "price": None,
    },
]


def build_fixture(dest: Path, rows: list[dict] | None = None) -> Path:
    """Create a jlcparts-schema fixture SQLite at ``dest``.

    Args:
        dest: Path to write the SQLite database to.
        rows: Override the sample rows (defaults to :data:`SAMPLE_ROWS`).

    Returns:
        The path that was written (``dest``).
    """
    rows = rows if rows is not None else SAMPLE_ROWS
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()

    conn = sqlite3.connect(str(dest))
    try:
        conn.execute(
            """
            CREATE TABLE jlc_components (
                lcsc INTEGER PRIMARY KEY,
                mfr TEXT,
                package TEXT,
                manufacturer TEXT,
                library_type TEXT,
                description TEXT,
                datasheet TEXT,
                stock INTEGER,
                price TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO jlc_components
                (lcsc, mfr, package, manufacturer, library_type,
                 description, datasheet, stock, price)
            VALUES
                (:lcsc, :mfr, :package, :manufacturer, :library_type,
                 :description, :datasheet, :stock, :price)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()

    return dest


def build_split_zip_dataset(dir_path: Path, sqlite_path: Path) -> None:
    """Create a mock split-zip dataset mirroring the jlcparts publish layout.

    Writes ``cache.z01`` (first segment) and ``cache.zip`` (final segment) whose
    concatenation is a valid zip archive containing ``cache.sqlite3``. Used to
    exercise ``sync_catalog`` end-to-end without any real network access.

    Args:
        dir_path: Directory to write the segment files into.
        sqlite_path: The SQLite database to embed in the archive.
    """
    import zipfile

    dir_path.mkdir(parents=True, exist_ok=True)
    archive = dir_path / "combined.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(sqlite_path, arcname="cache.sqlite3")

    data = archive.read_bytes()
    archive.unlink()

    # Split into two segments: the first half becomes cache.z01, the remainder
    # becomes cache.zip (the split-archive convention: .zNN first, .zip last).
    midpoint = max(1, len(data) // 2)
    (dir_path / "cache.z01").write_bytes(data[:midpoint])
    (dir_path / "cache.zip").write_bytes(data[midpoint:])
