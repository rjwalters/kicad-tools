# tscircuit interop gate

Runs tscircuit-emitted KiCad projects through this repo's manufacturability bar
(Issue #5847): `kct check --mfr` (which carries DRC + ERC + LVS),
`kicad-cli pcb drc --refill-zones` as the independent second engine, and then
the same boards again with their copper ripped up and re-routed by `kct route`.

The measured results live in
[`docs/research/tscircuit-evaluation.md`](../../../docs/research/tscircuit-evaluation.md)
— this directory is only the harness that produces them.

Like the rest of `benchmarks/`, **nothing here runs under pytest or CI**
(`pyproject.toml` sets `testpaths = ["tests"]`). These are scripts a human (or
an agent working a follow-up) runs deliberately.

## Why nothing generated is committed

tscircuit is MIT, so reuse with attribution would be allowed — but the same
reasoning as [`benchmarks/external/`](../../external/README.md) applies for a
different reason: these boards are **derived artifacts of a specific upstream
version**, and a committed copy immediately stops matching the pinned tool. The
harness therefore installs the pinned npm packages and emits the projects at
run time into a gitignored cache (`.cache/kct-benchmarks/tscircuit/`, covered by
`.gitignore`'s `.cache/kct-benchmarks/` entry), and nothing third-party ever
reaches a tracked path.

What *is* tracked here is our own source: the five circuit definitions in
`examples.mjs`, the emitter driver `generate.mjs`, and the gate driver
`run_gate.py`.

## Contents

| File | Kind | What it is |
|---|---|---|
| `examples.mjs` | tracked source | Five tscircuit designs written with `createElement` (no JSX, so plain `node` runs them with no transpiler): a trivial RC, an LED indicator, a QFN-32 MCU board, a two-layer copper-pour board with a keepout, and a SOIC-8 op-amp. |
| `generate.mjs` | driver | Renders each design with `@tscircuit/core`, converts it with `circuit-json-to-kicad`, and writes `<slug>.kicad_pcb` / `.kicad_sch` / `.kicad_pro` plus an `emit.json` sidecar recording the Circuit JSON histogram and tscircuit's own diagnostics. |
| `run_gate.py` | driver | Provisions the npm workspace at pinned versions, runs `generate.mjs`, then gates every export twice (as emitted, and kct-routed) and writes `results/results.json` + `results/results.md`. |

## Running it

```bash
uv run python benchmarks/interop/tscircuit/run_gate.py
```

Useful flags:

| Flag | Effect |
|---|---|
| `--only rc_lowpass,qfn_mcu` | Gate a subset. |
| `--skip-install` / `--skip-emit` | Reuse the installed `node_modules` / the previous exports. |
| `--skip-route` | Only gate the tscircuit-routed copper. |
| `--mfr pcbway` | Use a different manufacturer profile for `kct check --mfr`. |
| `--kicad-cli PATH` | Use a specific `kicad-cli` instead of auto-discovery. |
| `--no-docker` | Fail rather than fall back to a container. |

### `kicad-cli` resolution, and the macOS gotcha

The gate prefers a `kicad-cli` on `PATH` (or `--kicad-cli`). If there is none it
falls back to a container image (`--kicad-docker-image`, default
`kicad/kicad:10.0`, run with `--platform linux/amd64` because the published
image has no arm64 manifest).

**That fallback is not a convenience, it is the only working path on this
project's own macOS host.** The installed `kicad-cli` 10.0.1 there blocks
*forever* during `APP_KICAD_CLI::OnInit()` →
`LIBRARY_MANAGER::LoadGlobalTables()` → `wxDir::Open()`, because that scan
touches `~/Documents`, and reading `~/Documents` from a process without macOS
TCC consent blocks on a consent dialog that no headless agent can answer
(`ls ~/Documents` blocks identically, and overriding `$HOME` does not help —
wxWidgets resolves the documents directory through Cocoa, not `$HOME`).
Only KiCad ≥ 10 supports `--refill-zones`; the gate probes
`kicad-cli pcb drc --help` and records whether it was used.

### The two passes are not enforcing an identical rule set

`kct route` writes a sibling `<board>.kicad_dru`, which KiCad's DRC picks up
automatically. The kct-routed pass therefore enforces **our** emitted rules on
top of the project's own, which is deliberate (it is the design-rule sync this
repo wants) but means its violation counts are not directly comparable with the
as-emitted pass. `results.json` records `kicad_dru_present` per pass so this is
visible rather than silently folded into a number.
