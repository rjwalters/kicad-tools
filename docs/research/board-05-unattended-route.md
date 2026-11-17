# Board 05 routed from scratch, unattended (Issue #7834)

Measured 2026-10-22 on `origin/main` @ `2de924703` (after #7835 turned
`--reserve-pour-only-passives` on by default and #7838 chained the
`--complete` lattice pass into `kct route`). Part of epic #4410.

## Why this note exists

Board 05's shipping recipe (`design.py` -> `redesign/build.py`) replays
reviewed copper from `redesign/routing.json`. The `board-05-routing-regression`
job in `ci.yml` runs that recipe, so its "0 blocking" says nothing about the
router. `scripts/ci/board_05_unattended.py` is the from-scratch proof. It
writes the unrouted board with `redesign/hardware.generate` (the step before
`build.py` applies `routing.json`), routes it with `kct route`, and gates the
result. `.github/workflows/nightly-board-05-unattended.yml` runs it daily and
on dispatch, one matrix job per seed (42, 7, 1337).

Route flags (the #6508 protocol from the #7831 evidence; `--reserve-pour-only-passives`
is the default now, so it is not passed):

```
kct route <unrouted> -o <routed> --timeout 4500 --no-cache \
  --no-auto-layers --deterministic-budget --per-net-iterations 1000000 \
  --no-placement-delta-feedback --deterministic-rescue \
  --no-fallback-bidirectional --seed <S>
```

Gates, all required and none allowlisted: route exit 0 and a final (not
deadline) artifact; the stranded-pour oracle answered (#7836); every net
complete; kicad-cli `pcb drc --refill-zones --all-track-errors` 0
error-severity violations and 0 unconnected items; kicad-cli `sch erc` 0
violations; `kct check --mfr jlcpcb` 0 errors; copper and label LVS clean.

## Results

Host: macOS arm64, C++ backend built. kicad-cli was the pinned 10.0.6 image
(`KCT_KICAD_DOCKER=1`), with the board's `fp-lib-table` and `board05_revB.pretty`
beside the routed board. The seeds ran one after another.

| Seed | Nets | kicad-cli DRC err | Unconnected | ERC | kct check err | LVS (copper / label) | Vias | Wall s | Load avg (start) | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| 42 | 37/37 | 0 | 0 | 0 | 0 | clean / clean | 124 | 242.2 | 2.56 | PASS |
| 7 | 37/37 | 0 | 0 | 0 | 0 | clean / clean | 124 | 244.1 | 4.28 | PASS |
| 1337 | 37/37 | 0 | 0 | 0 | 0 | clean / clean | 124 | 242.1 | 6.00 | PASS |

**The outcome does not depend on the seed.** All three routed boards are
byte-identical (sha1 `b80c221d49d6d5d9274e603db66c731ef786a0ef`). They are also
identical to the #7838 Judge's harness run at seed 42, whose input went through
`expand_route_experiment.py`'s re-serialisation. The main pass reaches 32/33
signal nets and leaves VCP open; the chained `--complete` pass closes it.

Linux (the pinned `kicad/kicad` image, amd64): see "CI / Linux" below.

### Negative run

The gate was checked against a known-bad board. The seed-42 board had all 20
SPEED segments and vias deleted, and `--grade-only` was run on it. The script
exited 1 with `nets 36/37 complete; kicad-cli unconnected items: 2; kct check
--mfr jlcpcb errors: 1 (connectivity); copper LVS dirty`. Run against the
host's Homebrew kicad-cli 10.0.7 without the Docker toolchain, it exits 2
(`REFUSED (KiCad environment, Issue #6962)`) and grades nothing.

## Which outcomes depend on load

`--deterministic-budget` and `--deterministic-rescue` make the A* loop and the
relief-rescue sub-searches iteration-bounded. Two wall clocks are still live in
this route, and the route log reports both:

* **Post-negotiation sweep**: `60.0s whole-sweep / 10.0s per-net wall clock`
  (`sweep_wall_clock: true`, #4159 / #6329).
* **Chained `--complete` lattice pass**: `per-link budget 60s` (netset deadline
  = 60 s x link count). It closes VCP, the one net the main pass leaves open.

Neither bound was reached at a load average of 2.6-6.0 on this host, so the
boards came out byte-identical. A much slower runner could hit either one. If
it hit the completion budget, VCP would stay open and the gate would fail. It
would fail loudly as `nets 36/37`, with kicad-cli unconnected > 0, never as a
pass. So a red nightly with VCP open and no router change is a load signal
first. Read the uploaded `route.log` before assuming a regression.

## Gated: warnings

The nightly gates warnings too (#8073, finishing #7848 AC 2), matching the
no-warning gate in the shipping `build.py` (#7298): `kct check` warnings and
kicad-cli DRC warnings must both be 0, and a count that did not run fails like
the error counters.

This was not always so. The unrouted board from `hardware.generate` used to
carry 136 `silkscreen_line_width` and 1 `silk_overlap` (`kct check`) plus 4
`silk_over_copper` and 1 `silk_overlap` (kicad-cli), because the DFM silk
corrections (0.15 mm strokes, the R4/R11/RV1 `REFERENCE_OFFSETS`) lived only in
`redesign/routing.py:apply_routing`. #7848 moved them into
`hardware.generate`, and the nightly of 2026-10-28 (run 40705286766) reported
0 warnings in both engines on seeds 42, 7 and 1337.

## CI / Linux

Host and CI diverge on board 05 (#3822), so seed 42 was also run on Linux.
The job's steps were replayed inside the pinned `kicad/kicad@sha256:186935...`
image (amd64, emulated on the arm64 host, `--memory 7g`): `uv sync --frozen`,
`kct build-native` (`router_cpp.cpython-312-x86_64-linux-gnu.so`), then
`board_05_unattended.py --seeds 42`, with kicad-cli native to the image.

| Seed | Nets | kicad-cli DRC err | Unconnected | ERC | kct check err | LVS | Vias | Wall s | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| 42 | 37/37 | 0 | 0 | 0 | 0 | clean / clean | 124 | 431.1 | PASS |

The Linux board passes every gate, with the same main-pass 32/33 and the same
chained completion of VCP. It is not byte-identical to the macOS board (sha1
`50fa6559...` vs `b80c221d...`), which is the #3822 host-vs-Linux divergence,
but every graded number matches. Emulation made this run about 1.8x slower than
the host, and it still did not reach either live wall clock.

This was a local container, not a GitHub-hosted runner. A new workflow cannot
be dispatched until it is on the default branch, so the first hosted-runner
numbers come from the first `workflow_dispatch` (or the 07:30 UTC schedule)
after merge. Add them here when they exist.
