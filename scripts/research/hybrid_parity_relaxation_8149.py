#!/usr/bin/env python3
"""Hybrid-parity relaxation study for board 03 ``USB_D`` (Issue #8149).

Measurement only: what bounded extra length / skew would admit a via-equal
hybrid leg pair?  The production filter in
``router/lattice/pathfinder.py`` (``_hybrid_parity_*``) is **not** edited; this
script patches it in the ``kct route`` *child process* through a
``sitecustomize`` on ``PYTHONPATH`` (``--hook`` mode below), only when
``HP8149_OUT`` is set.

Protocol (one routed state per seed, then one finish per distinct admitted pair)::

    KCT_KICAD_DOCKER=1 uv run python scripts/research/hybrid_parity_relaxation_8149.py \\
        run --seeds 42,43 --timeout 600 --work-dir /tmp/hp8149 --out-dir /tmp/hp8149/out

1. *record* run: the production pick is kept; ``_hybrid_parity_pick`` logs every
   via-equal candidate (against the finished board and against the trunk alone)
   with its skew, total length and leg lengths, next to the pick's own rank.
2. The 4x4 grid (extra total length fraction x extra skew mm) is evaluated
   offline from that log: a cell admits a candidate iff
   ``skew <= pick_skew + dS`` and ``total <= pick_total * (1 + f)``.
3. For each distinct pair admitted by some cell, one *finish* run replays the
   route with the bounds of the smallest such cell and is graded with
   ``kct check --mfr jlcpcb-tier1`` and kicad-cli DRC (``diffpair_bench``).

Cells whose DRC cannot finish are reported as ``DRC not completed (#8148)``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
LENGTH_FRACS = (0.0, 0.05, 0.10, 0.20)
SKEW_MM = (0.0, 0.1, 0.25, 0.5)
PAIR = "USB_D"
CAP_SCALES = (1.25, 1.5, 2.0, 3.0)


# ---------------------------------------------------------------------------
# Child-process hook (installed by sitecustomize)
# ---------------------------------------------------------------------------


def install_hook() -> None:
    """Patch ``_hybrid_parity_pick`` to log candidates / apply a relaxed bound."""
    out = os.environ.get("HP8149_OUT")
    if not out:
        return
    from kicad_tools.router.lattice import pathfinder as pf

    cls = next(
        c for c in vars(pf).values() if isinstance(c, type) and hasattr(c, "_hybrid_parity_pick")
    )
    orig = cls._hybrid_parity_pick
    frac = os.environ.get("HP8149_FRAC")
    dskew = os.environ.get("HP8149_SKEW")

    def sig(kind, apres, alens, rank):
        return {
            "kind": kind,
            "vias": [len(r.vias) for r in apres.routes],
            "skew": rank[0],
            "total": rank[1],
            "legs": [round(alens[0], 4), round(alens[1], 4)],
        }

    import dataclasses

    orig_result = cls._hybrid_result
    orig_legs = cls._hybrid_parity_legs
    orig_joint = cls._hybrid_joint_legs
    trace: list[dict] = []

    def traced_result(self, *a, **kw):
        res = orig_result(self, *a, **kw)
        trace.append(
            {
                "reason": res[1],
                "legs": [round(res[2][0], 4), round(res[2][1], 4)],
                "vias": [len(kw_legs.route.vias) for kw_legs in (a[4]["P"], a[4]["N"])],
                "cap": kw.get("cap"),
            }
        )
        return res

    def counted(name, fn):
        def wrapper(self, *a, **kw):
            out_ = list(fn(self, *a, **kw))
            trace.append({"searched": name, "alternatives": len(out_)})
            return out_

        return wrapper

    def enumerate_(self, ctx, committed):
        trace.clear()
        cands = self._hybrid_parity_candidates(ctx, committed=committed)
        return [sig(k, a, l, self._hybrid_rank(a)) for k, a, l in cands], list(trace), cands

    def patched(self, ctx, *, committed):
        pick = self._hybrid_rank(ctx.pres)
        rec = {
            "pair": str(ctx.pc.key),
            "pick": {
                "vias": [len(r.vias) for r in ctx.pres.routes],
                "skew": pick[0],
                "total": pick[1],
                "cap": ctx.cap,
                "max_vias": ctx.max_vias,
                "vias_used": ctx.vias_used,
            },
        }
        rec["finished"], rec["finished_trace"], cands = enumerate_(self, ctx, committed)
        rec["trunk_only"], rec["trunk_only_trace"], _ = enumerate_(
            self, ctx, self._fresh_committed()
        )
        # Leg-budget diagnostic: the same enumeration with the declared cap scaled.
        rec["cap_scaled"] = {}
        for scale in CAP_SCALES:
            c2 = dataclasses.replace(ctx, cap=ctx.cap * scale)
            fin, tr, _ = enumerate_(self, c2, committed)
            tro, _t, _ = enumerate_(self, c2, self._fresh_committed())
            rec["cap_scaled"][str(scale)] = {"finished": fin, "trunk_only": tro, "trace": tr}
        with open(out, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        if frac is None or dskew is None:
            return orig(self, ctx, committed=committed)
        best = None
        for kind, apres, alens in cands:
            skew, total = self._hybrid_rank(apres)
            if (
                skew <= pick[0] + float(dskew) + 1e-9
                and total <= pick[1] * (1 + float(frac)) + 1e-9
            ):
                if best is None or (skew, total) < best[0]:
                    best = ((skew, total), kind, apres, alens)
        if best is None:
            return ctx.pres, "ok", None
        return best[2], f"ok-{best[1]}", best[3]

    orig_leg = cls._search_hybrid_leg

    def traced_leg(self, item, *a, **kw):
        res, why = orig_leg(self, item, *a, **kw)
        trace.append(
            {
                "leg_search": str(item[0]),
                "found": res is not None,
                "why": why,
                "vias": None if res is None else len(res.route.vias),
                "mirror": kw.get("mirror") is not None,
                "guided": kw.get("guide") is not None,
            }
        )
        return res, why

    cls._search_hybrid_leg = traced_leg
    cls._hybrid_result = traced_result
    cls._hybrid_parity_legs = counted("mirrored", orig_legs)
    cls._hybrid_joint_legs = counted("joint", orig_joint)
    cls._hybrid_parity_pick = patched


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def admitted(rec: dict, key: str, frac: float, dskew: float) -> list[dict]:
    pick = rec["pick"]
    return [
        c
        for c in rec[key]
        if c["skew"] <= pick["skew"] + dskew + 1e-9
        and c["total"] <= pick["total"] * (1 + frac) + 1e-9
    ]


def best_of(cands: list[dict]) -> dict | None:
    return min(cands, key=lambda c: (c["skew"], c["total"])) if cands else None


def load_rec(path: Path) -> dict | None:
    """The record of the last hook call (board 03 has one hybrid pair: USB_D J1.A6->U2.3)."""
    if not path.exists():
        return None
    recs = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return recs[-1] if recs else None


def route(
    seed: int,
    work: Path,
    timeout: float,
    hook_dir: Path,
    cell: tuple[float, float] | None,
    tag: str,
):
    import diffpair_bench as db
    import expand_route_experiment as ere

    log = work / f"records-s{seed}-{tag}.jsonl"
    log.unlink(missing_ok=True)
    os.environ["HP8149_OUT"] = str(log)
    os.environ["PYTHONPATH"] = str(hook_dir) + os.pathsep + os.environ.get("PYTHONPATH", "")
    if cell is None:
        os.environ.pop("HP8149_FRAC", None)
        os.environ.pop("HP8149_SKEW", None)
    else:
        os.environ["HP8149_FRAC"], os.environ["HP8149_SKEW"] = str(cell[0]), str(cell[1])
    board = ere.BOARDS["03"]
    row = db.run_cell(board, "lattice", seed, timeout, work / tag)
    return row, load_rec(log)


def summarize_row(row: dict) -> dict:
    pair = next(
        (p for p in row.get("pairs", []) if PAIR in str(p.get("pair", p.get("name", "")))), {}
    )
    return {
        "valid": row.get("valid"),
        "exit_code": row.get("exit_code"),
        "wall_s": row.get("wall_s"),
        "pair": pair,
        "kct_errors_by_rule": row.get("kct_errors_by_rule"),
        "kicad_cli_errors_by_type": row.get("kicad_cli_errors_by_type"),
        "manufacturable": row.get("manufacturable"),
        "oracle_unverified": row.get("oracle_unverified"),
        "invalid_reasons": row.get("invalid_reasons"),
    }


def cmd_run(args) -> int:
    sys.path.insert(0, str(HERE))
    import expand_route_experiment as ere

    work, out = Path(args.work_dir), Path(args.out_dir)
    work.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    hook_dir = work / "hook"
    hook_dir.mkdir(exist_ok=True)
    (hook_dir / "sitecustomize.py").write_text(
        f"import sys\nsys.path.insert(0, {str(HERE)!r})\n"
        "import hybrid_parity_relaxation_8149 as h\nh.install_hook()\n"
    )
    result: dict = {"issue": 8149, "pair": PAIR, "seeds": {}}
    with ere.kicad_toolchain_session(
        [work.resolve(), out.resolve()], allow_unpinned=args.allow_unpinned_kicad
    ):
        for seed in (int(s) for s in args.seeds.split(",")):
            row, rec = route(seed, work, args.timeout, hook_dir, None, f"s{seed}-record")
            seed_res: dict = {
                "record_row": summarize_row(row),
                "record": rec,
                "cells": {},
                "finishes": {},
            }
            result["seeds"][str(seed)] = seed_res
            if rec is None:
                continue
            chosen: dict[tuple, tuple[float, float]] = {}
            for f in LENGTH_FRACS:
                for s in SKEW_MM:
                    c = best_of(admitted(rec, "finished", f, s))
                    t = best_of(admitted(rec, "trunk_only", f, s))
                    seed_res["cells"][f"{f}/{s}"] = {"finished": c, "trunk_only": t}
                    if c is not None:
                        k = (c["kind"], round(c["skew"], 4), round(c["total"], 4))
                        if k not in chosen or (f + s) < sum(chosen[k]):
                            chosen[k] = (f, s)
            for k, cell in chosen.items():
                frow, _ = route(
                    seed, work, args.timeout, hook_dir, cell, f"s{seed}-f{cell[0]}-d{cell[1]}"
                )
                seed_res["finishes"][f"{k}@{cell}"] = summarize_row(frow)
            (out / "hybrid_parity_relaxation_8149.json").write_text(json.dumps(result, indent=2))
    print(render(result))
    return 0


def render(result: dict) -> str:
    lines = [
        "| seed | extra length | extra skew (mm) | via-equal pair vs finished board | vs trunk alone |",
        "|---|---|---|---|---|",
    ]

    def fmt(c):
        return (
            "none"
            if c is None
            else (
                f"{c['kind']} vias {c['vias'][0]}/{c['vias'][1]} skew {c['skew']:.3f} "
                f"total {c['total']:.3f} legs {c['legs'][0]}/{c['legs'][1]}"
            )
        )

    for seed, res in result["seeds"].items():
        rec = res.get("record")
        if rec:
            p = rec["pick"]
            lines.append(
                f"| {seed} | pick | | vias {p['vias'][0]}/{p['vias'][1]} skew {p['skew']:.3f} total {p['total']:.3f} | |"
            )
        for cell, v in res["cells"].items():
            f, s = cell.split("/")
            lines.append(
                f"| {seed} | +{float(f) * 100:.0f} % | +{s} | {fmt(v['finished'])} | {fmt(v['trunk_only'])} |"
            )
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--seeds", default="42,43")
    r.add_argument("--timeout", type=float, default=600)
    r.add_argument("--work-dir", required=True)
    r.add_argument("--out-dir", required=True)
    r.add_argument("--allow-unpinned-kicad", action="store_true")
    r.set_defaults(fn=cmd_run)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
