#!/usr/bin/env python3
"""Generate artificially-broken negative-control placements for FOM Phase 0.

Issue #3187 risk #2: kicad-happy is a *positive-only* corpus and the
perturbed dataset's only negatives are jitter-style perturbations.  A
classifier could learn "committed-by-a-human != noise-added-by-a-script"
rather than "manufacturable != not."

To probe this confounder we generate a handful of placements that fail
DRC *by construction* and check that the trained model labels them with
low probability.  Three flavours:

1.  ``edge_violation``  -- shift every non-fixed footprint outside the
    board outline (so most pads sit beyond the edge).
2.  ``overlap_pile``    -- stack every non-fixed footprint at the board
    centre (massive courtyard overlaps).
3.  ``extreme_jitter``  -- sigma=20 mm Gaussian jitter (10x the corpus
    sigma); virtually guaranteed to fail.

Output rows are appended to a separate JSONL (``negatives.jsonl``) so the
training pipeline doesn't see them.  The notebook loads both files for
the qualitative analysis.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from kicad_tools.optim.fom_features import (
    PHASE0_FEATURE_NAMES,
    extract_phase0_features_from_pcb,
)
from kicad_tools.schema.pcb import PCB

logger = logging.getLogger("fom_phase0.negative_controls")


@dataclass
class NegativeRecord:
    sample_id: str
    seed_path: str
    flavour: str
    features: dict[str, float]


def make_edge_violation(pcb: PCB) -> None:
    """Shove every non-fixed footprint to (-100, -100) -- way off the board."""
    for fp in pcb.footprints:
        if fp.locked:
            continue
        ref = (fp.reference or "").upper()
        if ref.startswith(("J", "MH", "MK", "TP", "X")):
            continue
        fp.position = (-100.0, -100.0)


def make_overlap_pile(pcb: PCB, rng: random.Random) -> None:
    """Stack every non-fixed footprint at the board centre."""
    # Find bbox of fixed footprints to estimate the board centre.
    xs, ys = [], []
    for fp in pcb.footprints:
        xs.append(fp.position[0])
        ys.append(fp.position[1])
    if not xs:
        cx, cy = 0.0, 0.0
    else:
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)

    for fp in pcb.footprints:
        if fp.locked:
            continue
        ref = (fp.reference or "").upper()
        if ref.startswith(("J", "MH", "MK", "TP", "X")):
            continue
        # Pin every footprint within +/- 0.5 mm of the centre.
        fp.position = (cx + rng.uniform(-0.5, 0.5), cy + rng.uniform(-0.5, 0.5))


def make_extreme_jitter(pcb: PCB, rng: random.Random, sigma: float = 20.0) -> None:
    for fp in pcb.footprints:
        if fp.locked:
            continue
        ref = (fp.reference or "").upper()
        if ref.startswith(("J", "MH", "MK", "TP", "X")):
            continue
        ox, oy = fp.position
        fp.position = (ox + rng.gauss(0, sigma), oy + rng.gauss(0, sigma))


FLAVOURS = ("edge_violation", "overlap_pile", "extreme_jitter")


def apply_flavour(pcb: PCB, flavour: str, rng: random.Random) -> None:
    if flavour == "edge_violation":
        make_edge_violation(pcb)
    elif flavour == "overlap_pile":
        make_overlap_pile(pcb, rng)
    elif flavour == "extreme_jitter":
        make_extreme_jitter(pcb, rng)
    else:
        raise ValueError(f"Unknown flavour: {flavour}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--boards-auto",
        action="store_true",
        help="Use the in-repo board fallback corpus.",
    )
    parser.add_argument("--boards", nargs="*", default=None)
    parser.add_argument(
        "--samples-per-flavour",
        type=int,
        default=3,
        help="Negative controls per (board, flavour) (default: 3).",
    )
    parser.add_argument("--seed", type=int, default=99)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/research/fom_phase0/negatives.jsonl"),
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path("data/research/fom_phase0/work_negatives"),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from kicad_tools.optim.fom_features import PHASE0_FEATURE_NAMES as PFN  # noqa: F401

    DEFAULT_SEEDS = (
        "boards/01-voltage-divider/output/voltage_divider.kicad_pcb",
        "boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb",
        "boards/03-usb-joystick/output/usb_joystick.kicad_pcb",
        "boards/04-stm32-devboard/output/stm32_devboard.kicad_pcb",
        "boards/07-matchgroup-test/output/matchgroup_test.kicad_pcb",
    )
    if args.boards:
        seed_paths = [Path(p) for p in args.boards]
    elif args.boards_auto:
        seed_paths = [Path(p) for p in DEFAULT_SEEDS]
    else:
        seed_paths = [Path(p) for p in DEFAULT_SEEDS]

    seed_paths = [p for p in seed_paths if p.exists()]
    if not seed_paths:
        logger.error("No seed PCBs found.")
        return 1

    args.work_dir.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("")  # truncate

    master_rng = random.Random(args.seed)
    written = 0
    for seed_path in seed_paths:
        for flavour in FLAVOURS:
            for k in range(args.samples_per_flavour):
                sample_id = f"NEG__{seed_path.stem}__{flavour}__{k:02d}"
                rng = random.Random(master_rng.randint(0, 2**31 - 1))
                pcb = PCB.load(seed_path)
                apply_flavour(pcb, flavour, rng)
                out_path = args.work_dir / f"{sample_id}.kicad_pcb"
                pcb.save(out_path)
                try:
                    feats = extract_phase0_features_from_pcb(pcb)
                except Exception as exc:
                    logger.warning("feature extraction failed on %s: %s", sample_id, exc)
                    feats = dict.fromkeys(PHASE0_FEATURE_NAMES, 0.0)
                rec = NegativeRecord(
                    sample_id=sample_id,
                    seed_path=str(seed_path),
                    flavour=flavour,
                    features=feats,
                )
                with args.output.open("a") as fh:
                    fh.write(json.dumps(asdict(rec), sort_keys=True))
                    fh.write("\n")
                written += 1
                logger.info("Wrote %s", sample_id)

    logger.info("Wrote %d negative controls to %s", written, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
