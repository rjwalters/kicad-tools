# Reviewed physical routing

`sdram_demo.kicad_pcb` is the explicit routing/length-tuning artifact for the
43-part, 216-binding real SDRAM circuit. `manifest.json` binds its SHA-256 and
the exact authored source manifest. It includes the completed copper and
operator silkscreen; it is not a generic autorouter cache entry.

Run `../build.py OUTPUT` through `uv run python` to regenerate the independent
electrical source and repeat every design gate. A new logical circuit, physical
package placement or source-rule manifest requires a new reviewed routing
artifact and fresh validation; updating a hash alone is not a design review.

The build intentionally distinguishes design validation from manufacturing
export. The final order package must be generated afterward and its manifest
must pass before manufacturing readiness is claimed. Hardware has not yet been
tested.
