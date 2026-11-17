#!/usr/bin/env python3
"""Minimal Hugging Face dataset access for the corpus tooling (issue #4830).

Extracted from ``probe_open_schematics.py`` (slice 1) so the probe, the
manifest builder, and the manifest runner all speak to the dataset through one
implementation of the retry/backoff rules.

**stdlib + ``requests`` only.** The Hugging Face ``datasets`` library is
deliberately not used and must not be added as a dependency: the dataset is
published as ~1,700 parquet shards (no per-record blobs to list, smallest shard
still hundreds of MB), while the *datasets-server* HTTP API serves the same
dataset row by row -- which is what makes "download only the N records we
sampled" possible.

This module does network I/O, so nothing under ``tests/`` may import it.
"""

from __future__ import annotations

import time
from typing import Any

DEFAULT_DATASET = "bshada/open-schematics"
DATASET_LICENSE = "CC-BY-4.0"
DATASETS_SERVER = "https://datasets-server.huggingface.co"
HUB_API = "https://huggingface.co/api/datasets"
USER_AGENT = "kicad-tools-corpus-probe/1 (+https://github.com/rjwalters/kicad-tools)"


class HubError(RuntimeError):
    """A dataset request failed after all retries."""


def _requests_module() -> Any:
    try:
        # Imported lazily: offline paths (--dry-run, --offline) must work
        # without it.
        import requests
    except ImportError as exc:  # pragma: no cover - dev envs always have it
        raise HubError(
            "the 'requests' package is required for network mode; "
            "run the script via `uv run python scripts/corpus/<script>.py`, "
            "or use the offline flag"
        ) from exc
    return requests


def hub_get_json(url: str, params: dict[str, Any], timeout: float, retries: int) -> dict[str, Any]:
    """GET a JSON endpoint with bounded retries and backoff."""
    requests = _requests_module()
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=timeout,
                headers={"User-Agent": USER_AGENT},
            )
            if response.status_code == 200:
                payload = response.json()
                if not isinstance(payload, dict):
                    raise HubError(f"{url}: expected a JSON object, got {type(payload).__name__}")
                return payload
            # 429/5xx are transient (datasets-server warms shards on demand).
            if response.status_code in (429, 500, 502, 503, 504):
                last = HubError(f"{url}: HTTP {response.status_code}")
            else:
                raise HubError(f"{url}: HTTP {response.status_code}: {response.text[:200]}")
        except HubError:
            raise
        except Exception as exc:  # requests raises a wide family of transport errors
            last = exc
        if attempt < retries:
            time.sleep(min(2.0 * attempt, 10.0))
    raise HubError(f"{url}: failed after {retries} attempts: {last}")


def dataset_revision(dataset: str, timeout: float, retries: int) -> str | None:
    """Resolve the dataset's current commit sha, so a report is reproducible."""
    try:
        info = hub_get_json(f"{HUB_API}/{dataset}", {}, timeout, retries)
    except HubError:
        return None
    sha = info.get("sha")
    return str(sha) if sha else None


def dataset_row_count(dataset: str, config: str, split: str, timeout: float, retries: int) -> int:
    """Total number of rows in the split (the sampling universe)."""
    info = hub_get_json(f"{DATASETS_SERVER}/info", {"dataset": dataset}, timeout, retries)
    configs: dict[str, Any] = info.get("dataset_info", {})
    entry: dict[str, Any] = configs.get(config) or next(iter(configs.values()), {})
    splits: dict[str, Any] = entry.get("splits", {})
    split_info: dict[str, Any] = splits.get(split) or next(iter(splits.values()), {})
    count = int(split_info.get("num_examples", 0))
    if count <= 0:
        raise HubError(f"{dataset}: could not determine row count for split '{split}'")
    return count


def row_url(dataset: str, config: str, split: str, offset: int) -> str:
    """The exact datasets-server URL that serves one record.

    Recorded verbatim in the manifest so an entry is fetchable by hand (curl,
    browser) without reverse-engineering the API from the field names.
    """
    return (
        f"{DATASETS_SERVER}/rows?dataset={dataset.replace('/', '%2F')}"
        f"&config={config}&split={split}&offset={offset}&length=1"
    )


def fetch_row(
    dataset: str, config: str, split: str, offset: int, timeout: float, retries: int
) -> dict[str, Any]:
    """Fetch a single dataset row (one record) by absolute offset."""
    payload = hub_get_json(
        f"{DATASETS_SERVER}/rows",
        {
            "dataset": dataset,
            "config": config,
            "split": split,
            "offset": offset,
            "length": 1,
        },
        timeout,
        retries,
    )
    rows = payload.get("rows") or []
    if not rows:
        raise HubError(f"{dataset}: no row at offset {offset}")
    row = rows[0]
    return {
        "row": row.get("row", {}),
        "truncated_cells": list(row.get("truncated_cells") or []),
    }
