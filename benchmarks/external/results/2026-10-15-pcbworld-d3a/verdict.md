# Verdict: PCBWorld D3-A `kct route` baseline, 2026-10-15

**Partial. No D3-A number exists yet.** 0 of the 99 `d3a` test boards were
prepared, routed or scored, so CP@1, CP@5, the local bar, routability, vias,
wirelength and wall time are all unmeasured. No figure in this directory is
an estimate of them.

## What was established

1. **The D3-A denominator is 99 boards.** That is PCBWorld's `easy.test` at
   `b3d62f5c`, enumerated from its own `splits.json` / `d3.json` by
   `kct bench external`. `0096_*` is train-only. PCBWorld's "fair-95" list
   is a strict subset of those 99. It drops 0004, 0024, 0035 and 0077, and
   is reported as a separate denominator.
2. **`kct route` is *not* a deterministic baseline under the C++ backend.**
   `--seed` changed the routed geometry on 1 of the 4 boards that routed to
   completion (voltage_divider: 3 distinct layouts over seeds 1-5, each
   reproducible on repeat). The other three gave one layout for every seed.
   So the D3 run must use **five declared rollouts** with PCBWorld's
   max-potential-gain selection. Reporting CP@5 = CP@1 would be wrong.
3. **Budget-limited runs are not reproducible.** On usb_joystick, detailed
   routing hit its wall-clock timeouts (exit 2). The same seed run twice
   gave different geometry, and seeds 4, 5 and the repeat of seed 1 matched
   each other while seeds 1-3 did not. A D3 attempt that hits a time budget
   can therefore not be byte-reproduced from its seed. The attempt records
   keep the route outcome, so such attempts can be told apart.
   (`kct route --deterministic-budget` exists for this. The D3 protocol
   leaves router options at their defaults, so it is not applied.)
4. **#6331's suite would have refused every real D3 board.** It routed
   `processed_v9_guide_v3.kicad_pcb` and asserted that the file had no
   copper. PCBWorld's own `make_guide.md` says this is the *routed* guide
   board (PCBWorld strips it at `reset()`). Fixed in this PR: the suite now
   routes `processed_v9_guide_v3_unrouted.kicad_pcb`, and uses the routed
   guide as the human wirelength/via reference.

## What did not run, and why

The worker has KiCad 10.0.7, not KiCad 9. It also has no PCBWorld conda
environment and no PCBWorld engine build (`kicad_rl_router`, which the
scorer needs for potential gain). The suite correctly refused at its
KiCad-major gate. Provisioning those tools is a worker-spec change, not
something to do on a shared host. See [`README.md`](README.md).

Because nothing was routed, there are **no failure classes to summarize**
and no generic router issues were filed from this run. The two
observations above are about reproducibility, not routing failures. The
second (same-seed nondeterminism under wall-clock budgets) is the known
reason `--deterministic-budget` exists, so no new issue was filed for it.

## Published comparators (not re-scored here)

| Router | D3-A Clean Pass | Basis | Source |
|---|---|---|---|
| Freerouting | 0.80 | best-of-5 | PCBWorld paper (arXiv:2607.05915) Table 3. Full-99 vs fair-95 denominator not verified |
| KiCadRoutingTools | 0.74 | deterministic, @5 = @1 | PCBWorld paper Table 3 |
| TraceMaker | 0.99 | "Small (99)", single 60 s run | punkfab.com blog, third-party, self-reported, possibly tuned on these boards |
| `kct route` | **not measured** | -- | this directory |

## To finish

Run the command in [`README.md`](README.md) on a host with KiCad 9 and a
built PCBWorld environment. Commit its `results.json` and `report.d3.md`,
check that `--render-d3` reproduces the report byte-for-byte, and replace
this verdict. The registry entry `pcbworld` stays `watch` until #6333 also
completes D3-B.
