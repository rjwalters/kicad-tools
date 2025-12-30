# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2024-12-30

### Added

#### Manufacturing Readiness (Planned Features)

- **LCSC Parts Integration** (`kicad_tools.parts`)
  - `LCSCClient` for direct part lookups from JLCPCB's LCSC database
  - Part search with filtering by stock, category, and specifications
  - Local caching for offline use and reduced API calls
  - CLI: `kct parts lookup`, `kct parts search`, `kct parts cache`

- **Assembly Package Export** (`kicad_tools.export`)
  - Complete manufacturing packages (Gerbers + BOM + CPL) for fabrication
  - Multi-manufacturer format support (JLCPCB, PCBWay, OSHPark, Seeed)
  - CLI: `kct export assembly`, `kct export gerbers`, `kct export pnp`

- **Fluent Query API** (`kicad_tools.query`)
  - Django-ORM style filtering for symbols and footprints
  - `sch.symbols.filter(value="100nF")`, `pcb.footprints.by_reference("U1")`
  - Chainable filters with field lookups

- **Project Class** (`kicad_tools.project`)
  - Cross-reference schematics to PCBs
  - Unified project-level queries for finding unplaced components

#### Bonus Features (Beyond Roadmap)

- **A* Autorouter with Obstacle Awareness** (`kicad_tools.router`)
  - Intelligent obstacle detection and avoidance
  - Net-class aware routing strategies (power, clock, audio, digital)
  - Multi-layer support with automatic via placement
  - Negotiated and greedy routing strategies
  - `CommandInterpreter` for high-level routing commands
  - CLI: `kct route`

- **Trace Optimizer** (`kicad_tools.optim`)
  - Post-routing trace optimization
  - Length matching support for differential pairs

- **Manufacturer DRC Configuration**
  - Configurable design rules per manufacturer
  - Rule comparison tools between manufacturers
  - CLI: `kct mfr compare`

- **Footprint Validation & Repair** (`kicad_tools.footprints`)
  - Detect pad spacing issues, overlaps, courtyard violations
  - Automatic repair with configurable minimum gaps
  - CLI: `kct validate-footprints`, `kct fix-footprints`

- **Placement Conflict Detection** (`kicad_tools.optim`)
  - Detect component overlaps and courtyard violations
  - Conflict resolution suggestions

### Changed

- CLI version now dynamically reads from package metadata instead of hardcoded value

### Fixed

- Test for `LCSCClient.close()` method now patches at correct module level

## [0.1.0] - 2024-12-29

### Added

- **Core S-expression parser** with round-trip editing support
- **Schematic parsing** - symbols, wires, labels, hierarchy traversal
- **PCB parsing** - footprints, nets, traces, vias, zones
- **Symbol library parsing** - read and query KiCad symbol libraries
- **ERC report parsing** - parse KiCad Electrical Rules Check reports
- **DRC report parsing** - parse KiCad Design Rules Check reports
- **Manufacturer profiles** - design rules for JLCPCB, OSHPark, PCBWay, Seeed
- **PCB autorouter** - A* pathfinding with pluggable heuristics
  - Net class awareness (power, clock, audio, digital)
  - Multi-layer support with via management
  - Congestion-aware routing
- **Unified CLI** (`kct` or `kicad-tools`) with subcommands:
  - `kct symbols` - list symbols in schematics
  - `kct nets` - trace and analyze nets
  - `kct bom` - generate bill of materials
  - `kct erc` - run/parse ERC reports
  - `kct drc` - run/parse DRC reports with manufacturer rules
- **PCB tools** - `kicad-pcb-query` and `kicad-pcb-modify`
- **Library tools** - `kicad-lib-symbols`
- JSON output for all CLI commands

### Dependencies

- Python 3.10+
- numpy >= 1.20

[0.2.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.2.0
[0.1.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.1.0
