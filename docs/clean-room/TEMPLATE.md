# Behaviour spec: <topic>

<!--
Copy this file to `docs/clean-room/<topic>.md` and fill every field.
The reader fills the provenance block and the behaviour sections.
The spec reviewer checks the provenance is complete and that the
behaviour sections leak no expression (no code, no structure-following
pseudo-code, no internal identifiers beyond public file-format and CLI
names). The writer does not edit this file.
-->

## Provenance

| Field | Value |
|---|---|
| Topic | |
| Source project and license | e.g. KiCad (GPL) |
| Source version and commit studied | e.g. 10.0.7 @ <git sha> |
| Reader agent id | |
| Spec reviewer agent id | |
| Writer agent id (filled when the implementing PR opens) | |
| Spec date | |
| Implementing PR | |

## Behaviour

### Inputs

What the behaviour consumes: file-format tokens, CLI arguments, values with
units.

### Outputs

What it produces, with units and formats.

### Rules

Statements of behaviour only — ordering of *behaviour*, never of the
original's code. No code, no pseudo-code that follows the original's
structure, no internal identifiers beyond public file-format and CLI names.

### Edge cases

Boundary values, empty and degenerate inputs, rounding, endianness.

### Error cases

Every rejected input and the observable outcome (message, exit code,
dropped data) — as the black box presents it.

## Test vectors

Input and expected output pairs the writer may implement against and the
probes may assert.

| # | Input | Expected output | Source of expectation |
|---|---|---|---|
| 1 | | | e.g. pinned-image probe, documented format |

## Review record

The spec reviewer records the check for leaked expression before handover:

- Reviewed by (agent id):
- Checked: no code; no structure-following pseudo-code; no internal
  identifiers beyond public file-format and CLI names.
- Verdict:
- Date:
