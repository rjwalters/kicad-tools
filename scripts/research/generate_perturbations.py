#!/usr/bin/env python3
"""Generate perturbed PCB placements + route/check labels for the FOM Phase 0 study.

Issue #3187: a Phase-0 signal-existence test for a learned routability residual.

The pipeline for one *sample* is:

    1. Load an unrouted seed PCB.
    2. Jitter each non-locked footprint's (x, y) by an independent Gaussian draw
       (sigma controlled by --sigma; default 4 mm).  Optionally apply a random
       +/-90 degree rotation per footprint (--rotate-prob, default 0.3).
    3. Save the perturbed PCB to a per-sample file.
    4. Run kct route (Python backend; --skip-drc for speed when --label is off)
       to produce a routed file.
    5. Run kct check on the routed file to get a DRC violation count.
    6. Extract the Phase 0 feature vector from the *unrouted* perturbed PCB
       (placement-only features so the predictor can score before routing).
    7. Label = 1 iff routing finished AND DRC reported zero errors.

The script is incremental: each sample's row is appended to the labels file
as soon as it's done, so an interrupted run can be resumed by re-running with
the same --output (already-recorded sample IDs are skipped) and same --seed.

Examples
--------
Quick smoke test (5 samples on board 01, no routing) ::

    python scripts/research/generate_perturbations.py \\
        --boards boards/01-voltage-divider/output/voltage_divider.kicad_pcb \\
        --samples-per-seed 5 \\
        --no-route \\
        --output data/research/fom_phase0/labels.parquet

Full Phase 0 corpus (~30 hours wall clock) ::

    python scripts/research/generate_perturbations.py \\
        --boards-auto \\
        --samples-per-seed 75 \\
        --seed 42 \\
        --output data/research/fom_phase0/labels.parquet
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Project imports
from kicad_tools.optim.fom_features import (
    PHASE0_FEATURE_NAMES,
    extract_phase0_features_from_pcb,
)
from kicad_tools.schema.pcb import PCB

logger = logging.getLogger("fom_phase0.perturb")

# Boards in this repo we treat as Phase 0 seeds when --boards-auto is set.
DEFAULT_SEEDS = (
    "boards/01-voltage-divider/output/voltage_divider.kicad_pcb",
    "boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb",
    "boards/03-usb-joystick/output/usb_joystick.kicad_pcb",
    "boards/04-stm32-devboard/output/stm32_devboard.kicad_pcb",
    "boards/05-bldc-motor-controller/output/bldc_controller.kicad_pcb",
    "boards/06-diffpair-test/output/diffpair_test.kicad_pcb",
    "boards/07-matchgroup-test/output/matchgroup_test.kicad_pcb",
)


# ---------------------------------------------------------------------------
# Sample record
# ---------------------------------------------------------------------------


@dataclass
class SampleRecord:
    """One row in the labels file.

    Stored as JSONL on disk so the file can be appended to incrementally
    without needing a parquet writer that supports appends.  The notebook
    converts JSONL to parquet at load time.
    """

    sample_id: str
    seed_name: str
    seed_path: str
    perturbed_path: str
    routed_path: str | None
    sigma_mm: float
    rotate_prob: float
    rng_seed: int
    n_footprints: int
    n_moved: int
    n_rotated: int
    route_ok: bool
    route_seconds: float
    drc_errors: int  # -1 if not run
    check_ok: bool
    label: int  # 1 if routed AND drc errors == 0
    notes: str = ""
    features: dict[str, float] = field(default_factory=dict)

    def to_jsonl(self) -> str:
        d = self.__dict__.copy()
        return json.dumps(d, sort_keys=True)


# ---------------------------------------------------------------------------
# Perturbation
# ---------------------------------------------------------------------------


def perturb_pcb(
    pcb: PCB,
    rng: random.Random,
    sigma_mm: float,
    rotate_prob: float,
) -> tuple[int, int]:
    """Mutate a PCB in place with positional jitter + optional rotation.

    Locked footprints are left untouched (they're typically connectors /
    mounting holes whose positions are mechanically fixed).

    Returns:
        (n_moved, n_rotated): counts of footprints actually changed.
    """
    n_moved = 0
    n_rotated = 0
    for fp in pcb.footprints:
        if fp.locked:
            continue
        # Skip footprints whose ref starts with J/MH/TP/X — same heuristic
        # as the FOM "fixed footprint" classifier (these are mechanical
        # constraints in the original board and perturbing them mostly
        # produces uninteresting "every sample is broken" labels).
        ref = (fp.reference or "").upper()
        if ref.startswith(("J", "MH", "MK", "TP", "X")):
            continue

        dx = rng.gauss(0.0, sigma_mm)
        dy = rng.gauss(0.0, sigma_mm)
        ox, oy = fp.position
        fp.position = (ox + dx, oy + dy)
        n_moved += 1

        if rng.random() < rotate_prob:
            # +/-90 degrees only -- arbitrary angles confuse the router
            # more than is interesting for "is placement plausible" labels.
            delta = rng.choice([-90.0, 90.0])
            fp.rotation = (fp.rotation + delta) % 360.0
            n_rotated += 1

    return n_moved, n_rotated


# ---------------------------------------------------------------------------
# Routing + checking
# ---------------------------------------------------------------------------


def run_route(
    src_pcb: Path,
    out_pcb: Path,
    timeout_sec: int,
) -> tuple[bool, float, str]:
    """Run ``kct route`` as a subprocess.

    Returns (ok, seconds, notes).  ok is True iff the subprocess exited
    with status 0 *and* produced the output file.
    """
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "uv",
        "run",
        "kct",
        "route",
        str(src_pcb),
        "-o",
        str(out_pcb),
        "--skip-drc",
        "-q",
        "--no-sync-check",
        "--no-placement-feedback",
        "--no-optimize",
        "--backend",
        "cpp",
        "--timeout",
        str(min(timeout_sec, 120)),
    ]
    t0 = time.perf_counter()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return False, time.perf_counter() - t0, "route timeout"
    except Exception as exc:  # pragma: no cover -- defensive
        return False, time.perf_counter() - t0, f"route subprocess exc: {exc}"
    seconds = time.perf_counter() - t0
    if result.returncode != 0 or not out_pcb.exists():
        # Try to extract the last informative line for diagnostics.
        tail = (result.stderr or result.stdout or "").splitlines()[-1:] or [""]
        return False, seconds, f"route exit {result.returncode}: {tail[0][:120]}"
    return True, seconds, ""


def run_check(pcb: Path, mfr: str = "jlcpcb") -> tuple[int, str]:
    """Run ``kct check`` and return (error_count, notes).

    error_count is -1 if the subprocess failed; 0 == clean; positive == violations.
    """
    cmd = [
        "uv",
        "run",
        "kct",
        "check",
        str(pcb),
        "--format",
        "json",
        "--mfr",
        mfr,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:  # pragma: no cover -- defensive
        return -1, f"check subprocess exc: {exc}"
    # kct check exits non-zero on violations but still prints JSON.
    # Parse the JSON body irrespective of exit status.
    text = result.stdout or ""
    # The JSON block starts after possibly-emitted warnings; find the
    # first '{' as a heuristic.
    idx = text.find("{")
    if idx < 0:
        return -1, f"check no json (exit {result.returncode})"
    try:
        data = json.loads(text[idx:])
    except json.JSONDecodeError as exc:
        return -1, f"check json error: {exc}"
    summary = data.get("summary") or {}
    return int(summary.get("errors", -1)), ""


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def existing_sample_ids(out_path: Path) -> set[str]:
    """Read sample IDs already present in *out_path* (JSONL)."""
    if not out_path.exists():
        return set()
    ids: set[str] = set()
    with out_path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = d.get("sample_id")
            if sid:
                ids.add(sid)
    return ids


def write_record(out_path: Path, record: SampleRecord) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a") as fh:
        fh.write(record.to_jsonl())
        fh.write("\n")


def process_one_sample(
    seed_path: Path,
    sample_idx: int,
    out_dir: Path,
    args: argparse.Namespace,
    master_rng: random.Random,
) -> SampleRecord:
    """Generate, route, and label a single perturbed sample."""
    seed_name = seed_path.stem
    sample_id = f"{seed_name}__s{sample_idx:04d}"
    perturbed_path = out_dir / "perturbed" / f"{sample_id}.kicad_pcb"
    routed_path = out_dir / "routed" / f"{sample_id}_routed.kicad_pcb"

    # Per-sample RNG seed derived deterministically from master + sample id.
    rng_seed = master_rng.randint(0, 2**31 - 1)
    rng = random.Random(rng_seed)

    # Load and perturb.
    pcb = PCB.load(seed_path)
    n_footprints = len(list(pcb.footprints))
    n_moved, n_rotated = perturb_pcb(pcb, rng, args.sigma, args.rotate_prob)

    perturbed_path.parent.mkdir(parents=True, exist_ok=True)
    pcb.save(perturbed_path)

    # Feature extraction on the *unrouted* perturbed PCB.
    try:
        features = extract_phase0_features_from_pcb(pcb)
    except Exception as exc:  # pragma: no cover -- defensive
        features = dict.fromkeys(PHASE0_FEATURE_NAMES, 0.0)
        logger.warning("feature extraction failed on %s: %s", sample_id, exc)

    if args.no_route:
        return SampleRecord(
            sample_id=sample_id,
            seed_name=seed_name,
            seed_path=str(seed_path),
            perturbed_path=str(perturbed_path),
            routed_path=None,
            sigma_mm=args.sigma,
            rotate_prob=args.rotate_prob,
            rng_seed=rng_seed,
            n_footprints=n_footprints,
            n_moved=n_moved,
            n_rotated=n_rotated,
            route_ok=False,
            route_seconds=0.0,
            drc_errors=-1,
            check_ok=False,
            label=0,
            notes="no-route mode",
            features=features,
        )

    # Route.
    route_ok, route_seconds, route_notes = run_route(
        perturbed_path, routed_path, timeout_sec=args.route_timeout
    )
    drc_errors = -1
    check_ok = False
    notes = route_notes

    if route_ok:
        drc_errors, check_notes = run_check(routed_path, mfr=args.mfr)
        check_ok = drc_errors == 0
        if check_notes:
            notes = (notes + " | " + check_notes).strip(" |")

    label = 1 if (route_ok and check_ok) else 0

    return SampleRecord(
        sample_id=sample_id,
        seed_name=seed_name,
        seed_path=str(seed_path),
        perturbed_path=str(perturbed_path),
        routed_path=str(routed_path) if route_ok else None,
        sigma_mm=args.sigma,
        rotate_prob=args.rotate_prob,
        rng_seed=rng_seed,
        n_footprints=n_footprints,
        n_moved=n_moved,
        n_rotated=n_rotated,
        route_ok=route_ok,
        route_seconds=route_seconds,
        drc_errors=drc_errors,
        check_ok=check_ok,
        label=label,
        notes=notes,
        features=features,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--boards",
        nargs="*",
        default=None,
        help="Explicit seed PCB paths. If omitted and --boards-auto is set, "
        "uses the in-repo board fallback corpus.",
    )
    parser.add_argument(
        "--boards-auto",
        action="store_true",
        help="Use the in-repo board fallback corpus.",
    )
    parser.add_argument(
        "--samples-per-seed",
        type=int,
        default=30,
        help="Number of perturbed samples per seed (default: 30).",
    )
    parser.add_argument(
        "--sigma",
        type=float,
        default=4.0,
        help="Gaussian sigma for x/y jitter in mm (default: 4.0).",
    )
    parser.add_argument(
        "--rotate-prob",
        type=float,
        default=0.3,
        help="Probability of a +/-90 rotation per footprint (default: 0.3).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Master RNG seed (default: 42).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/research/fom_phase0/labels.jsonl"),
        help="Output labels JSONL path. Notebook converts to parquet.",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path("data/research/fom_phase0/work"),
        help="Where perturbed + routed PCBs are written.",
    )
    parser.add_argument(
        "--no-route",
        action="store_true",
        help="Skip routing/DRC (just emits perturbation + features). Useful "
        "for quick feature-extraction-only runs.",
    )
    parser.add_argument(
        "--route-timeout",
        type=int,
        default=600,
        help="Per-sample routing timeout in seconds (default: 600).",
    )
    parser.add_argument(
        "--mfr",
        default="jlcpcb",
        help="Manufacturer profile for kct check (default: jlcpcb).",
    )
    parser.add_argument(
        "--start-index",
        type=int,
        default=0,
        help="Skip samples with idx < this. Lets a long run be split across processes.",
    )
    parser.add_argument(
        "--max-samples",
        type=int,
        default=None,
        help="Stop after this many samples (across all seeds).",
    )
    parser.add_argument(
        "--cleanup-perturbed",
        action="store_true",
        help="Delete perturbed/routed PCBs after labelling (saves disk).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (DEBUG/INFO/WARNING).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Resolve seed paths.
    if args.boards:
        seed_paths = [Path(p) for p in args.boards]
    elif args.boards_auto:
        seed_paths = [Path(p) for p in DEFAULT_SEEDS]
    else:
        parser.error("Provide --boards <paths> or --boards-auto")

    seed_paths = [p for p in seed_paths if p.exists()]
    if not seed_paths:
        parser.error("No seed PCBs found.")

    logger.info(
        "Phase 0 perturbation: %d seeds x %d samples each", len(seed_paths), args.samples_per_seed
    )
    logger.info("Output -> %s", args.output)

    already_done = existing_sample_ids(args.output)
    if already_done:
        logger.info(
            "Found %d existing samples in %s; will skip duplicates.", len(already_done), args.output
        )

    master_rng = random.Random(args.seed)
    written = 0
    skipped_existing = 0
    failed = 0
    success = 0

    global_idx = -1
    for seed_path in seed_paths:
        for sample_idx in range(args.samples_per_seed):
            global_idx += 1
            if global_idx < args.start_index:
                continue
            if args.max_samples is not None and written >= args.max_samples:
                logger.info("Reached --max-samples=%d, stopping.", args.max_samples)
                _print_summary(written, skipped_existing, failed, success)
                return 0

            sample_id = f"{seed_path.stem}__s{sample_idx:04d}"
            if sample_id in already_done:
                skipped_existing += 1
                continue

            try:
                rec = process_one_sample(seed_path, sample_idx, args.work_dir, args, master_rng)
            except Exception as exc:
                logger.exception("Sample %s crashed: %s", sample_id, exc)
                failed += 1
                continue

            if rec.label == 1:
                success += 1

            write_record(args.output, rec)
            written += 1
            logger.info(
                "[%d/%d] %s  route_ok=%s drc=%d label=%d (%.1fs)  notes=%s",
                written,
                len(seed_paths) * args.samples_per_seed,
                sample_id,
                rec.route_ok,
                rec.drc_errors,
                rec.label,
                rec.route_seconds,
                rec.notes[:60],
            )

            if args.cleanup_perturbed:
                _try_unlink(Path(rec.perturbed_path))
                if rec.routed_path:
                    _try_unlink(Path(rec.routed_path))

    _print_summary(written, skipped_existing, failed, success)
    return 0


def _try_unlink(p: Path) -> None:
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        pass


def _print_summary(written: int, skipped: int, failed: int, success: int) -> None:
    logger.info("=" * 60)
    logger.info(
        "Done. Wrote=%d  Skipped=%d  Failed=%d  Successful labels=%d",
        written,
        skipped,
        failed,
        success,
    )
    if written:
        logger.info("Pass rate: %.1f%%", 100.0 * success / written)


if __name__ == "__main__":
    sys.exit(main())
