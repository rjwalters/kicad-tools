# kicad-tools agent guide

kicad-tools (`kct`) builds, checks and exports KiCad designs headlessly. This
primer is the same for every agent harness. Run `kct agent-guide --harness
<harness>` for skill invocation examples in your harness's syntax, and
`kct <command> --help` for any command's details.

## 1. Core workflow

Work board by board, and keep each step's output as a file you can re-check:

1. **Schematic**: write or generate the `.kicad_sch`, then run ERC: `kct erc <sch>`.
2. **PCB**: `kct create-pcb <sch>`. For an existing board, run
   `kct sync <pro> --analyze`, preview with `kct sync <pro> --apply --dry-run`,
   then apply with `kct sync <pro> --apply --confirm`.
3. **Place**: `kct placement check <pcb>`, then `kct optimize-placement <pcb>`.
4. **Route**: `kct route <pcb>`. Check what is left with `kct net-status <pcb>`.
5. **Fill zones**: `kct zones fill <pcb>`.
6. **Check, engine 1**: `kct check <pcb> --mfr <fab>`.
7. **Check, engine 2**: `kicad-cli pcb drc --refill-zones <pcb>`.
8. **LVS / sync**: `kct validate --sync --lvs <pro>`.
9. **Export**: `kct export <pcb> --mfr <fab>` writes Gerbers, BOM, CPL and a report.

## 2. The JSON contract

Every command that takes `--format json` writes exactly one JSON document to
stdout. Progress and warnings go to stderr. Parse stdout with a strict JSON
parser. Never scrape the text output, which is for humans and can change. An
empty stdout with a non-zero exit code means the command failed; the reason
is on stderr.

## 3. Sign-off: two engines, no partial credit

A board is manufacturable only when all of these hold:

- `kct check <pcb> --mfr <fab>` reports **0 errors**;
- `kicad-cli pcb drc --refill-zones <pcb>` reports **0 errors**;
- **100%** of nets are routed (`kct net-status <pcb>`);
- schematic and PCB have **0 sync drift** (`kct validate --sync --lvs <pro>`).

The two DRC engines catch different mistakes, so one clean run is not
enough. Never report 90% routed as done. If a gate cannot run, say which
one and why. Do not report the board as clean.

## 4. Wiring up a harness

Register the MCP server (`kct mcp serve`) with your harness. The command finds
the absolute `kct` path and merges into the existing config:

    kct mcp setup --client {claude-code,claude-desktop,codex,opencode}

Install the packaged `kct` skills, rendered for your harness
(`kct skills install --help` lists the supported harnesses):

    kct skills install --harness <harness>

<!-- per-harness -->
- `kct mcp setup --client <harness>` then `kct skills install --harness <harness>`
- skills land in `{{skills-dir}}`; invoke one as `{{skill:tapeout}} <board-path>`
- `{{skill:help}}` lists every skill; `{{skills-readme}}` explains the namespace
<!-- /per-harness -->

## 5. Pitfalls

- **Slow routing?** Run `kct build-native --check` first. Without the C++
  backend, routing is 10-100x slower. Build it with `kct build-native`.
- **A stale `kct` on `PATH`.** `which -a kct` and `kct --version` show which
  install answers. Inside a project, prefer `uv run kct ...`.
- **`kicad-cli` hangs on macOS** (#5877). KiCad 10 scans `~/Documents` at
  startup and blocks forever when the host denies access. Wrap calls in a
  timeout. A hang means the second engine did not run, so the board is not
  signed off. Grant the terminal Documents access, or run `kicad-cli` from the
  `kicad/kicad:10.0` container.
