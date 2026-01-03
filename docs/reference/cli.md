# CLI Reference

Complete reference for the `kct` (kicad-tools) command-line interface.

---

## Global Options

```bash
kct [--help] [--version] <command> [options]
```

| Option | Description |
|--------|-------------|
| `--help`, `-h` | Show help message |
| `--version`, `-V` | Show version number |

---

## Commands Overview

| Category | Command | Description |
|----------|---------|-------------|
| **Analysis** | `symbols` | List and query schematic symbols |
| | `nets` | Trace and analyze nets |
| | `sch` | Schematic analysis tools |
| | `pcb` | PCB query tools |
| **Validation** | `erc` | Parse ERC reports |
| | `drc` | Parse DRC reports with manufacturer rules |
| | `check` | Pure Python DRC (no kicad-cli) |
| | `validate` | Schematic-to-PCB sync validation |
| **Manufacturing** | `bom` | Generate bill of materials |
| | `mfr` | Manufacturer tools and rules |
| **Libraries** | `lib` | Symbol library tools |
| | `footprint` | Footprint generation tools |
| | `parts` | LCSC parts lookup and search |
| | `datasheet` | Datasheet search and PDF parsing |
| **PCB Operations** | `route` | Autoroute a PCB |
| | `zones` | Add copper pour zones |
| | `placement` | Detect and fix placement conflicts |
| | `optimize-traces` | Optimize PCB traces |
| **AI Integration** | `reason` | LLM-driven PCB layout reasoning |
| **Utilities** | `config` | View/manage configuration |
| | `interactive` | Launch interactive REPL mode |

---

## Analysis Commands

### `symbols`

List and query schematic symbols.

```bash
kct symbols <schematic> [options]
```

| Option | Description |
|--------|-------------|
| `--format {table,json,csv}` | Output format (default: table) |
| `--filter PATTERN` | Filter by reference pattern (e.g., "R*") |
| `--lib LIB_ID` | Filter by library ID |
| `--verbose`, `-v` | Show additional details |

**Examples:**
```bash
kct symbols project.kicad_sch
kct symbols project.kicad_sch --format json
kct symbols project.kicad_sch --filter "C*"  # All capacitors
```

---

### `nets`

Trace and analyze nets in a schematic.

```bash
kct nets <schematic> [options]
```

| Option | Description |
|--------|-------------|
| `--format {table,json}` | Output format (default: table) |
| `--net NAME` | Show only specified net |
| `--stats` | Show net statistics |

**Examples:**
```bash
kct nets project.kicad_sch
kct nets project.kicad_sch --net VCC
kct nets project.kicad_sch --stats --format json
```

---

### `sch`

Schematic analysis subcommands.

```bash
kct sch <subcommand> <schematic> [options]
```

| Subcommand | Description |
|------------|-------------|
| `summary` | Show schematic summary |
| `symbols` | List symbols with details |
| `labels` | List all labels |
| `wires` | List wire segments |
| `hierarchy` | Show sheet hierarchy |
| `validate` | Validate schematic structure |
| `pin-positions` | Get symbol pin positions |
| `check-connections` | Check for unconnected pins |
| `find-unconnected` | Find unconnected nets |

**Examples:**
```bash
kct sch summary project.kicad_sch
kct sch hierarchy project.kicad_sch --format json
kct sch find-unconnected project.kicad_sch
```

---

### `pcb`

PCB query subcommands.

```bash
kct pcb <subcommand> <pcb_file> [options]
```

| Subcommand | Description |
|------------|-------------|
| `query` | Query footprints and tracks |
| `modify` | Modify PCB elements |

**Examples:**
```bash
kct pcb query board.kicad_pcb --footprints
kct pcb query board.kicad_pcb --tracks --net GND
```

---

## Validation Commands

### `erc`

Parse and analyze ERC (Electrical Rule Check) reports.

```bash
kct erc <report> [options]
```

| Option | Description |
|--------|-------------|
| `--format {table,json}` | Output format (default: table) |
| `--errors-only` | Show only errors, not warnings |
| `--type TYPE` | Filter by error type |
| `--sheet SHEET` | Filter by sheet name |

**Examples:**
```bash
kct erc project.erc
kct erc project.erc --errors-only --format json
kct erc project.erc --type "unconnected"
```

---

### `drc`

Parse DRC reports with optional manufacturer rule comparison.

```bash
kct drc <report> [options]
```

| Option | Description |
|--------|-------------|
| `--format {table,json}` | Output format (default: table) |
| `--errors-only` | Show only errors |
| `--type TYPE` | Filter by violation type |
| `--net NET` | Filter by net name |
| `--mfr {jlcpcb,oshpark,pcbway,seeed}` | Apply manufacturer rules |
| `--layers N` | Number of layers (default: 2) |
| `--compare` | Compare manufacturer rules |

**Examples:**
```bash
kct drc board.drc
kct drc board.kicad_pcb --mfr jlcpcb
kct drc --compare  # Show all manufacturer rules
```

---

### `check`

Pure Python DRC (no kicad-cli required).

```bash
kct check <pcb_file> [options]
```

| Option | Description |
|--------|-------------|
| `--format {table,json}` | Output format |
| `--mfr {jlcpcb,oshpark,pcbway}` | Manufacturer rules |
| `--rules RULES` | Custom rules file |

**Examples:**
```bash
kct check board.kicad_pcb
kct check board.kicad_pcb --mfr jlcpcb --format json
```

---

### `validate`

Validate schematic-to-PCB synchronization.

```bash
kct validate --sync <schematic> <pcb>
```

**Examples:**
```bash
kct validate --sync project.kicad_sch board.kicad_pcb
```

---

## Manufacturing Commands

### `bom`

Generate bill of materials.

```bash
kct bom <schematic> [options]
```

| Option | Description |
|--------|-------------|
| `--format {table,json,csv}` | Output format (default: table) |
| `--group` | Group identical components |
| `--exclude PATTERN` | Exclude by reference (can repeat) |
| `--include-dnp` | Include DNP (Do Not Place) parts |
| `--sort {reference,value,footprint}` | Sort order |
| `--output`, `-o` | Output file |

**Examples:**
```bash
kct bom project.kicad_sch
kct bom project.kicad_sch --format csv --group -o bom.csv
kct bom project.kicad_sch --exclude "TP*" --exclude "H*"
```

---

### `mfr`

Manufacturer tools and design rules.

```bash
kct mfr <subcommand> [options]
```

| Subcommand | Description |
|------------|-------------|
| `rules` | Show manufacturer design rules |
| `compare` | Compare multiple manufacturers |
| `dru` | Generate KiCad DRU file |

**Examples:**
```bash
kct mfr rules jlcpcb
kct mfr compare jlcpcb oshpark
kct mfr dru jlcpcb -o jlcpcb.dru
```

---

## Library Commands

### `lib`

Symbol library tools.

```bash
kct lib <subcommand> [options]
```

| Subcommand | Description |
|------------|-------------|
| `list` | List symbols in a library |
| `info` | Show symbol details |
| `create` | Create a new symbol |

**Examples:**
```bash
kct lib list Device.kicad_sym
kct lib info Device.kicad_sym R
```

---

### `footprint`

Footprint generation tools.

```bash
kct footprint <subcommand> [options]
```

| Subcommand | Description |
|------------|-------------|
| `generate` | Generate parametric footprints |
| `validate` | Validate footprint pad spacing |
| `fix` | Fix footprint issues |

```bash
kct footprint generate <type> [options]
```

| Type | Description |
|------|-------------|
| `soic` | SOIC packages |
| `qfp` | QFP packages |
| `qfn` | QFN packages |
| `dfn` | DFN packages |
| `bga` | BGA packages |
| `chip` | Chip resistors/capacitors |
| `sot` | SOT packages |
| `dip` | DIP packages |

**Examples:**
```bash
kct footprint generate soic --pins 8 --pitch 1.27
kct footprint generate qfn --pins 24 --size 4x4
kct footprint validate MyLib.kicad_mod
```

---

### `parts`

LCSC parts database lookup.

```bash
kct parts <subcommand> [options]
```

| Subcommand | Description |
|------------|-------------|
| `search` | Search for parts |
| `lookup` | Get part details by LCSC number |
| `check` | Check part availability |

**Examples:**
```bash
kct parts search "STM32F103"
kct parts lookup C8734
kct parts check C8734 --quantity 100
```

---

### `datasheet`

Datasheet tools.

```bash
kct datasheet <subcommand> [options]
```

| Subcommand | Description |
|------------|-------------|
| `search` | Search for datasheets |
| `download` | Download a datasheet |
| `parse` | Extract pin info from PDF |

**Examples:**
```bash
kct datasheet search "ATmega328P"
kct datasheet download "ATmega328P" -o atmega328p.pdf
kct datasheet parse atmega328p.pdf --pins
```

---

## PCB Operation Commands

### `route`

Autoroute a PCB.

```bash
kct route <pcb_file> [options]
```

| Option | Description |
|--------|-------------|
| `--output`, `-o` | Output file |
| `--net NET` | Route specific net only |
| `--width WIDTH` | Trace width in mm |
| `--clearance CLEARANCE` | Clearance in mm |
| `--via-size SIZE` | Via size in mm |
| `--layers LAYERS` | Routing layers |

**Examples:**
```bash
kct route board.kicad_pcb -o routed.kicad_pcb
kct route board.kicad_pcb --net CLK --width 0.2
```

---

### `zones`

Add copper pour zones.

```bash
kct zones <subcommand> <pcb_file> [options]
```

| Subcommand | Description |
|------------|-------------|
| `add` | Add a copper zone |
| `list` | List existing zones |

**Examples:**
```bash
kct zones add board.kicad_pcb --net GND --layer B.Cu
kct zones list board.kicad_pcb
```

---

### `placement`

Detect and fix placement conflicts.

```bash
kct placement <subcommand> <pcb_file> [options]
```

| Subcommand | Description |
|------------|-------------|
| `check` | Check for placement conflicts |
| `optimize` | Optimize component placement |
| `suggestions` | Get placement suggestions |

**Examples:**
```bash
kct placement check board.kicad_pcb
kct placement optimize board.kicad_pcb -o optimized.kicad_pcb
kct placement suggestions board.kicad_pcb --format json
```

---

### `optimize-traces`

Optimize PCB traces.

```bash
kct optimize-traces <pcb_file> [options]
```

| Option | Description |
|--------|-------------|
| `--output`, `-o` | Output file |
| `--length-match` | Enable length matching |

---

## AI Integration Commands

### `reason`

LLM-driven PCB layout reasoning.

```bash
kct reason <pcb_file> [options]
```

| Option | Description |
|--------|-------------|
| `--interactive` | Interactive mode |
| `--export-state` | Export state for external LLM |
| `--auto-route` | Auto-route priority nets |
| `--output`, `-o` | Output file |

**Examples:**
```bash
kct reason board.kicad_pcb --interactive
kct reason board.kicad_pcb --export-state > state.json
kct reason board.kicad_pcb --auto-route -o routed.kicad_pcb
```

---

## Utility Commands

### `config`

View and manage configuration.

```bash
kct config [options]
```

| Option | Description |
|--------|-------------|
| `--show` | Show current configuration |
| `--set KEY VALUE` | Set a configuration value |
| `--reset` | Reset to defaults |

---

### `interactive`

Launch interactive REPL mode.

```bash
kct interactive [pcb_file]
```

Provides an interactive shell for exploring and modifying PCB files.

---

## Output Formats

Most commands support multiple output formats:

| Format | Description |
|--------|-------------|
| `table` | Human-readable table (default) |
| `json` | Machine-readable JSON |
| `csv` | Comma-separated values |

Use `--format` to specify:
```bash
kct symbols project.kicad_sch --format json
```

---

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Error (invalid input, file not found, etc.) |
| 2 | Invalid arguments |
| 130 | Interrupted (Ctrl+C) |

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `KICAD_TOOLS_CONFIG` | Config file path |
| `KICAD_CLI_PATH` | Path to kicad-cli binary |
| `LCSC_API_KEY` | LCSC API key for parts lookup |
