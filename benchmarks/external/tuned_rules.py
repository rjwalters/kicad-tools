"""Declared netclass/diff-pair config for `kct bench external --tuned`.

Epic #4932 Phase 2, issue #4943 ("Issue E"). This is the *tuned* protocol's
input data: a per-board `--net-class-map` sidecar (see
`docs/guides/match-groups/07-cli-and-sidecar.md`) mirroring DeepPCB's own
published case-study setup for a board, as opposed to the "zero-touch"
protocol (issue #4941, `bench.py`) which routes with rules as-shipped and
applies no tuning at all. The two protocols are reported separately, never
merged or averaged (Epic #4932's stated risk register).

Lives outside the installed `kicad_tools` package for the same reason
`fetch_boards.py` / `normalize.py` do -- this is a repository-local
development tool, not a feature of a standalone pip install (see
`benchmarks/external/README.md`).

STRF configuration
-------------------
Values are taken directly from DeepPCB's own published STRF case-study
report (https://deeppcb.ai/benchmark/mixed-signal-rf-board-routing/,
verified 2026-08-25), which the epic explicitly cites as the source of
the "declared netclass/diff-pair config" this protocol reproduces:

    "Calibrate the netclasses: tighten the USB diff-pair gap to 0.15 mm,
    and give the SPI bus its own class with a smaller 0.4 mm / 0.2 mm via."

    "The USB 2.0 data lines (USB_D+/USB_D- and USB_CONN_D+/USB_CONN_D-)
    run as edge-coupled microstrips on the outer layers ... Track width
    (W) 200 um (0.20 mm), unchanged. Trace gap (S) tightened from 250 um
    (0.25 mm) to 150 um (0.15 mm) for the 90 ohm USB target."

    "It also gives the SPI bus (SPI3_SCK, SPI3_MOSI, SPI3_MISO, SPI3_!CS)
    its own netclass. Instead of the default 0.8 mm drill / 0.4 mm
    annular via, the SPI class uses a smaller 0.4 mm drill / 0.2 mm
    annular via ... to keep routing channels open around the dense QFN
    pin field."

Net names (`USB_D+`, `USB_D-`, `USB_CONN_D+`, `USB_CONN_D-`, `SPI3_SCK`,
`SPI3_MOSI`, `SPI3_MISO`, `SPI3_!CS`) were verified directly against the
pinned `STRF.kicad_pcb` (commit `0525ef655e460ff6d91d770582b47925e7852e7a`,
`boards.toml`) -- ``grep -n '^\\s*(net [0-9]' STRF.kicad_pcb``.

``NetClassRouting`` (``kicad_tools.router.rules``) models diff-pair
geometry as an explicit ``(trace_width, intra_pair_clearance)`` pair, not
as a target impedance to solve for -- so the USB entries below set the
literal 0.20 mm / 0.15 mm values DeepPCB's case study landed on directly,
rather than setting ``target_diff_impedance=90`` and letting a
stackup-dependent physics calculation re-derive a (possibly different)
gap. This keeps the tuned protocol's inputs exactly reproducible
regardless of this repo's own impedance-calculator accuracy, matching the
epic's "declared netclass/diff-pair config" framing.

``NetClassRouting`` also has no separate via-drill field -- only a single
``via_size`` (outer diameter). DeepPCB's SPI figure names a drill (0.4 mm)
and an annular ring (0.2 mm), which are not independently overridable
through this project's per-netclass schema. The SPI entries below set
``via_size=0.4`` (the smaller of DeepPCB's two SPI-class numbers) as the
closest reproducible mapping onto this project's schema, and are
deliberately NOT a claim that the resulting via geometry matches DeepPCB's
0.4/0.2 mm drill/annular pair exactly -- the report's `notes` records this
caveat so a reader never mistakes the mapping for byte-identical config.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "TUNED_DIFF_PAIRS",
    "build_tuned_net_class_map",
    "diff_pairs_for",
]

# (net_p, net_n) pairs DeepPCB's STRF case study re-tuned to a 0.20 mm
# track / 0.15 mm gap for the 90 ohm USB 2.0 target.
_STRF_USB_DIFF_PAIRS: tuple[tuple[str, str], ...] = (
    ("USB_D+", "USB_D-"),
    ("USB_CONN_D+", "USB_CONN_D-"),
)

# SPI bus nets DeepPCB's STRF case study gave their own compact-via netclass.
_STRF_SPI_NETS: tuple[str, ...] = ("SPI3_SCK", "SPI3_MOSI", "SPI3_MISO", "SPI3_!CS")

# Per-board declared diff-pair sets, exposed to the CLI layer so
# `collect_report(diff_pairs=...)` can report per-pair completion for the
# EXACT pairs this protocol tuned, rather than relying on suffix-based
# auto-detection.
TUNED_DIFF_PAIRS: dict[str, tuple[tuple[str, str], ...]] = {
    "strf": _STRF_USB_DIFF_PAIRS,
}


def _usb_pair_entries(net_p: str, net_n: str) -> dict[str, dict[str, Any]]:
    """One ``NetClassRouting``-shaped dict per net of a tuned USB diff pair."""
    common = {
        "name": "USB",
        "trace_width": 0.20,
        "intra_pair_clearance": 0.15,
        "coupled_routing": True,
    }
    return {
        net_p: {**common, "diffpair_partner": net_n},
        net_n: {**common, "diffpair_partner": net_p},
    }


def _spi_entries(nets: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """One ``NetClassRouting``-shaped dict per SPI bus net, compact via only."""
    return {
        net: {
            "name": "SPI",
            # See module docstring: the closest reproducible mapping of
            # DeepPCB's "0.4 mm drill / 0.2 mm annular via" onto this
            # project's single via_size (diameter) field.
            "via_size": 0.4,
        }
        for net in nets
    }


def build_tuned_net_class_map(slug: str) -> dict[str, dict[str, Any]] | None:
    """Return the declared `--net-class-map` sidecar dict for ``slug``.

    Returns ``None`` when no tuned config is defined for this board --
    issue #4943's scope is STRF only; other boards fall through to a clear
    per-board error at the CLI layer (`bench.py`) rather than silently
    reusing STRF's config or guessing at net names that may not exist on
    an unrelated board.
    """
    if slug != "strf":
        return None
    merged: dict[str, dict[str, Any]] = {}
    for net_p, net_n in _STRF_USB_DIFF_PAIRS:
        merged.update(_usb_pair_entries(net_p, net_n))
    merged.update(_spi_entries(_STRF_SPI_NETS))
    return merged


def diff_pairs_for(slug: str) -> tuple[tuple[str, str], ...] | None:
    """Return the declared diff pairs for ``slug``, or ``None`` if undefined."""
    return TUNED_DIFF_PAIRS.get(slug)
