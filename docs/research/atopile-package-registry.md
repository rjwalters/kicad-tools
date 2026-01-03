# Atopile Package Registry & Dependency Management

Research document investigating atopile's package ecosystem for potential application to kicad-tools.

## Overview

Atopile provides a complete package registry and dependency management system for sharing reusable circuit modules. The system is hosted at https://packages.atopile.io/ and integrated with the `ato` CLI.

## Key Components

### 1. Package Registry API

**Location**: `vendor/atopile/src/faebryk/libs/backend/packages/api.py`

The registry API provides:

- **Package queries** (`/v1/packages?query=`) - Search for packages by identifier or summary
- **Package info** (`/v1/package/{identifier}`) - Get package metadata
- **Release info** (`/v1/package/{identifier}/releases/{version}`) - Get specific release details
- **Publishing** (`/v1/publish`) - Upload new packages (GitHub Actions OIDC auth)

### 2. Package Metadata Schema

```python
class PackageReleaseInfo:
    identifier: str          # e.g., "atopile/addressable-leds"
    version: str             # semver: "1.2.3"
    repository: str          # GitHub repo URL
    authors: list[Author]    # name + email
    license: str             # SPDX license identifier
    summary: str             # Short description
    homepage: str | None     # Project homepage
    readme_url: str | None   # README location
    requires_atopile: str    # Version spec (e.g., "^0.3.0")
    stats: PackageStats      # Download counts
    hashes: FileHashes       # SHA256 integrity hash
    dependencies: PackageDependencies  # Transitive deps
    artifacts: ArtifactsInfo | None    # Build artifacts (gerbers, etc.)
    layouts: LayoutsInfo | None        # KiCAD layouts
    builds: list[str] | None           # Available build targets
    yanked_at: str | None    # If package was yanked
```

### 3. Package Configuration (`ato.yaml`)

```yaml
requires-atopile: "^0.3.0"

package:
  identifier: "owner/package-name"  # GitHub org/repo format
  version: "1.0.0"                  # Semver
  repository: "https://github.com/..."
  authors:
    - name: "Author Name"
      email: "email@example.com"
  license: "MIT"
  summary: "Short description"

dependencies:
  - type: registry
    identifier: "atopile/power-supply"
    release: "1.2.0"
  - type: git
    repo_url: "https://github.com/example/dep.git"
    ref: "v1.0.0"
  - type: file
    path: "./local-module"
```

### 4. Dependency Types

| Type | Format | Use Case |
|------|--------|----------|
| `registry` | `atopile/package@1.0.0` | Published packages |
| `git` | `git://github.com/org/repo.git#tag` | Unpublished/dev packages |
| `file` | `file://./local-path` | Local development |

### 5. Version Specifiers

Atopile uses npm-style version specifiers:

| Operator | Example | Meaning |
|----------|---------|---------|
| `^` | `^1.2.3` | >=1.2.3, <2.0.0 (compatible) |
| `~` | `~1.2.3` | >=1.2.3, <1.3.0 (patch updates) |
| `>=,<` | `>=1.0.0,<2.0.0` | Range |
| `*` | `*` | Any version |

### 6. Dependency Resolution

**Location**: `vendor/atopile/src/faebryk/libs/project/dependencies.py`

Resolution uses BFS (breadth-first search) to build a DAG:

1. Start with direct dependencies from `ato.yaml`
2. For each dependency, load dist (from cache, registry, or git)
3. Recursively resolve transitive dependencies
4. Detect and error on cycles
5. Handle version conflicts (must be exact matches currently)

Key features:
- **DAG-based resolution** - Prevents circular dependencies
- **Version pinning** - Registry deps must be pinned for publishing
- **Conflict detection** - Fails on incompatible version specs
- **Cache management** - Stored in `.ato/modules/.cache`

### 7. Package Distribution Format

Packages are distributed as `.zip` files with:

- `ato.yaml` - Package manifest
- Source files (`.ato`, `.py`)
- `layouts/` - KiCAD PCB files
- `parts/` - Component definitions
- `README.md` - Documentation

Validation requires:
- Pinned dependency versions
- Required files present
- Version increment over registry
- No build warnings (strict mode)
- 3D models resolved (strict mode)

### 8. CLI Commands

```bash
# Search packages
ato search <query>

# Add dependency
ato add atopile/power-supply@1.0.0
ato add git://github.com/example/dep.git#v1.0.0
ato add file://./local-module

# Sync dependencies
ato sync

# List dependencies
ato list

# Remove dependency
ato remove atopile/power-supply

# Publish package
ato package publish --version 1.0.0

# Verify package
ato package verify --strict
```

### 9. Publishing Workflow

1. **GitHub Actions OIDC** - Only auth method supported
2. **Validation** - Runs all package validators
3. **Build dist** - Creates zip with manifest
4. **Request upload** - Gets presigned S3 URL
5. **Upload package** - Upload to S3
6. **Upload artifacts** - Optional build artifacts
7. **Confirm** - Finalize release

## Potential Improvements for kicad-tools

### Short-term

1. **Block format specification** - Define metadata schema for circuit blocks
2. **Local caching** - Cache downloaded dependencies locally
3. **Version compatibility checks** - Verify kicad-tools version compatibility

### Medium-term

1. **Dependency declaration** - Add `dependencies` field to block config
2. **DAG resolution** - Implement BFS dependency resolver
3. **Git dependencies** - Support git:// URLs for unpublished blocks

### Long-term

1. **Package registry** - Build or leverage existing registry (npm/pypi style)
2. **Publishing workflow** - GitHub Actions integration
3. **Symbol/footprint dependencies** - Track external library requirements
4. **Block discovery** - Search and browse available blocks

## Example: kicad-tools Block Package

```yaml
# kicad-tools-block.yaml (proposed)
name: stm32-minimal
version: 1.0.0
description: "Minimal STM32 circuit block with power, crystal, and debug"
author: "community"
license: "MIT"

requires-kicad-tools: ">=0.6.0"

dependencies:
  - identifier: "community/usb-c-power"
    version: "1.0.0"
  - identifier: "community/crystal-oscillator"
    version: "2.1.0"

blocks:
  - name: STM32MinimalBlock
    entry: "stm32_minimal.py"

footprints:
  - "STM32F103C8T6.kicad_mod"

symbols:
  - "STM32F103C8T6.kicad_sym"
```

## References

- **Package API**: `vendor/atopile/src/faebryk/libs/backend/packages/api.py`
- **Config**: `vendor/atopile/src/atopile/config.py`
- **Dependencies**: `vendor/atopile/src/faebryk/libs/project/dependencies.py`
- **CLI**: `vendor/atopile/src/atopile/cli/install.py`
- **Version matching**: `vendor/atopile/src/atopile/version.py`
- **Distribution**: `vendor/atopile/src/faebryk/libs/package/dist.py`
- **MCP tools**: `vendor/atopile/src/atopile/mcp/tools/packages.py`
