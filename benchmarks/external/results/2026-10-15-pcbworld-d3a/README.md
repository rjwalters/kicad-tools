# PCBWorld D3-A baseline: run status, 2026-10-15 (issue #6332)

**Status: not run.** None of the 99 D3-A boards has been prepared, routed or
scored. This directory holds only the evidence that *was* produced and the
exact command to finish the run. It has no `results.json`, because there are
no D3-A attempt records to commit, and no CP number is stated or
extrapolated. See [`verdict.md`](verdict.md).

| File | What it is |
|---|---|
| `verdict.md` | Hand-written verdict: what ran, what did not, and why |
| `seed_variation.json` | Real measurement: does `kct route --seed` change routed geometry under the C++ backend? (5 in-tree boards, seeds 1-5 plus a repeat of seed 1) |

## Why the D3-A run did not happen on the 2026-10-15 host

The run was attempted on a shared Loom dispatch worker (16 vCPU reported,
61 GB RAM, Linux 6.17 x86_64), with the C++ router backend built
(`kct build-native --check`: `available (version 1.0.0)`, build version 55).
It stopped at the toolchain gate:

```
$ PCBWORLD_PYTHON=... KICAD_CLI=/usr/bin/kicad-cli PCBNEW_PYTHON=/usr/bin/python3 \
    uv run kct bench external --suite pcbworld --split d3a --full-split ...
error: KICAD_CLI reports KiCad '10.0.7', but PCBWorld D3 requires KiCad 9.x. A Clean
Pass from another major version is not comparable to published numbers; point
KICAD_CLI at a KiCad 9 install.
```

A D3 scoring host needs three things this worker does not have. Worker
policy forbids installing host-wide tools, so none of them was installed:

1. **KiCad 9** `kicad-cli` and a Python that can `import pcbnew` from
   KiCad 9. The worker has KiCad 10.0.7 only. Both PCBWorld's prep
   (`convert_v9`, `drc_fix_v9`, `make_guide`) and its scorer need KiCad 9.
2. **PCBWorld's own environment** (`environment.yml`, conda) for
   `PCBWORLD_PYTHON`.
3. **PCBWorld's engine build** (`kicad_rl_router`, GPL-3.0, built by
   PCBWorld's `tools/setup/setup_all.sh`). `eval/metrics.py` computes the
   potential through it, so it also computes `potential_gain`, the @5
   selection key. Without it the scorer cannot run.

This is a worker-provisioning gap, not a router result.

## Seed-variation measurement (real, committed)

Command (sequential, in-process, 30 routes; 25 min 29 s wall time; peak RSS
2.5 GB). The router code is kicad-tools `52481a96`. The script is this PR's
`benchmarks/external/seed_variation.py`.

```bash
uv run python benchmarks/external/seed_variation.py --seeds 1 2 3 4 5 --timeout 180 \
  -o seed_variation.json \
  boards/00-simple-led/output/simple_led.kicad_pcb \
  boards/01-voltage-divider/output/voltage_divider.kicad_pcb \
  boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb \
  boards/03-usb-joystick/output/usb_joystick.kicad_pcb \
  boards/04-stm32-devboard/output/stm32_devboard.kicad_pcb
```

| Board | Exit | Distinct geometries over seeds 1-5 | Same seed twice identical |
|---|---|---|---|
| simple_led | 0 | 1 | yes |
| voltage_divider | 0 | **3** | yes |
| charlieplex_3x3 | 0 | 1 | yes |
| usb_joystick | 2 (wall-clock timeouts in detailed routing) | **4** | **no** |
| stm32_devboard | 0 | 1 | yes |

These are in-tree boards, not D3 boards. They are a proxy, because no D3
board could be prepared on this host. Rerun the script on 5 prepared d3a
`*_unrouted.kicad_pcb` boards once a KiCad 9 host exists.

## Exact rerun command (on a KiCad 9 + PCBWorld-engine host)

```bash
uv run kct build-native --check     # must say: C++ backend: available
export PCBWORLD_PYTHON=/path/to/pcbworld-env/bin/python
export KICAD_CLI=/path/to/kicad9/kicad-cli
export PCBNEW_PYTHON=/path/to/python-with-kicad9-pcbnew

# Five declared rollouts (seeds 42..46), in bounded sittings; repeat until the
# report says "Complete: yes". Each invocation attempts at most 50 routes.
uv run kct bench external --suite pcbworld --split d3a --full-split \
  --rollouts 5 --route-timeout 600 --max-attempts 50 --resume \
  --output-dir .cache/kct-benchmarks/external/d3a-2026-10

# Commit only the compact data + the regenerated report, then write verdict.md
cp .cache/kct-benchmarks/external/d3a-2026-10/{results.json,report.d3.md} \
   benchmarks/external/results/<date>-pcbworld-d3a/
uv run kct bench external --render-d3 benchmarks/external/results/<date>-pcbworld-d3a/results.json
git diff --exit-code   # the re-render must be byte-identical
```

Never commit `rollout-*/`, `routed/`, `prepared/`, `pcbworld-scoring/` or
`attempts/` from the output dir. They contain (copies of) third-party boards
under mixed licenses.
