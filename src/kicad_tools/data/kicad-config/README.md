# Pinned KiCad library tables (Issue #6962)

`<major.minor>/fp-lib-table` and `sym-lib-table` are flat copies of the stock
tables in CI's pinned `kicad/kicad` image (`KICAD_IMAGE` in
`.github/workflows/ci.yml`; `/usr/share/kicad/template/`). They are
byte-identical to the KiCad 10.0.7 macOS app's template. Every URI goes through
`${KICAD10_FOOTPRINT_DIR}` / `${KICAD10_SYMBOL_DIR}`, so each kicad-cli resolves
them to its own installed libraries.

`kicad_tools.cli.kicad_toolchain` copies them into a scratch
`KICAD_CONFIG_HOME` for every evidence producer and CI job. Refresh them when
the image pin moves: see `docs/contributing/kicad-image-pin.md`.
