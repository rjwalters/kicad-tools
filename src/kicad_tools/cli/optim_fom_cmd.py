"""``kct optim fom-debug`` command.

Issue #3186 acceptance criterion 8: a CLI subcommand that prints the
per-term FOM breakdown for an existing routed (or pre-routed) placement.

This is a diagnostic tool -- it does not modify the PCB.  Useful for
investigating "why did the GA pick this placement?" and for tuning
weights interactively.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def run_optim_fom_debug(
    pcb_path: str,
    weights_path: str | None = None,
    output_format: str = "text",
    verbose: bool = False,
) -> int:
    """Run ``kct optim fom-debug <pcb>``.

    Args:
        pcb_path: Path to the routed PCB (.kicad_pcb).
        weights_path: Optional path to a weights YAML.  When ``None``,
            uniform 1.0 weights are used.
        output_format: ``"text"`` (default) or ``"json"``.
        verbose: When True, also dump the raw feature counts
            (footprint count, pad count, net count) above the FOM
            breakdown.

    Returns:
        Process exit code (0 on success, non-zero on error).
    """
    from kicad_tools.optim.fom import (
        SOFT_TERM_NAMES,
        compute_fom,
        default_weights,
        load_weights_from_yaml,
    )
    from kicad_tools.schema.pcb import PCB

    try:
        pcb = PCB.load(pcb_path)
    except FileNotFoundError:
        print(f"error: PCB not found: {pcb_path}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"error: could not load PCB '{pcb_path}': {exc}", file=sys.stderr)
        return 2

    if weights_path:
        try:
            weights = load_weights_from_yaml(weights_path)
        except FileNotFoundError:
            print(f"error: weights file not found: {weights_path}", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001
            print(f"error: could not load weights '{weights_path}': {exc}", file=sys.stderr)
            return 2
    else:
        weights = default_weights()

    result = compute_fom(pcb, weights=weights, pcb_path=pcb_path)

    if output_format == "json":
        out = {
            "pcb": str(Path(pcb_path).resolve()),
            "score": result.score,
            "soft_score": result.soft_score,
            "hard_gate_passed": result.hard_gate_passed,
            "hard_failures": result.hard_failures,
            "soft_terms": result.soft_terms,
            "weighted_soft_terms": result.weighted_soft_terms,
            "weights": weights.as_dict(),
            "predictor_value": result.predictor_value,
            "beta": result.beta,
        }
        if verbose and result.feature_cache is not None:
            f = result.feature_cache
            out["feature_summary"] = {
                "footprint_count": len(f.footprints),
                "pad_count": f.total_pad_count,
                "net_count": len(f.nets_to_pads),
                "segment_count": sum(len(v) for v in f.segments_by_net.values()),
                "via_count": sum(len(v) for v in f.vias_by_net.values()),
            }
        print(json.dumps(out, indent=2))
        return 0

    # Text format
    print(f"FOM breakdown for {pcb_path}")
    print(f"  score:           {result.score:.6f}")
    print(f"  soft_score:      {result.soft_score:.6f}")
    print(f"  hard_gate:       {'PASS' if result.hard_gate_passed else 'FAIL'}")
    if result.hard_failures:
        print(f"  hard_failures:   {', '.join(result.hard_failures)}")
    print(f"  predictor*beta:  {result.predictor_value:.4f} ** {result.beta}")
    if verbose and result.feature_cache is not None:
        f = result.feature_cache
        print("")
        print("Features:")
        print(f"  footprints:      {len(f.footprints)}")
        print(f"  pads:            {f.total_pad_count}")
        print(f"  nets (with pads): {len(f.nets_to_pads)}")
        print(f"  segments:        {sum(len(v) for v in f.segments_by_net.values())}")
        print(f"  vias:            {sum(len(v) for v in f.vias_by_net.values())}")
    print("")
    print(f"{'Term':<32} {'Raw':>12} {'Weight':>10} {'Weighted':>12}")
    print("-" * 70)
    for name in SOFT_TERM_NAMES:
        raw = result.soft_terms.get(name, 0.0)
        w = weights.as_dict()[name]
        weighted = result.weighted_soft_terms.get(name, 0.0)
        print(f"{name:<32} {raw:>12.4f} {w:>10.3f} {weighted:>12.4f}")
    print("-" * 70)
    total_weighted = sum(result.weighted_soft_terms.values())
    print(f"{'TOTAL':<32} {'':>12} {'':>10} {total_weighted:>12.4f}")
    return 0
