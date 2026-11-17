/**
 * Emit one KiCad project per example in `examples.mjs` (Issue #5847).
 *
 * Run from the gitignored cache workspace that `run_gate.py` provisions -- this
 * file and `examples.mjs` are copied there so `import "@tscircuit/core"`
 * resolves against the cache's `node_modules`, and so nothing tscircuit
 * generates or installs ever lands in a tracked path.
 *
 *     node generate.mjs <output-dir>
 *
 * Writes `<output-dir>/<slug>/<slug>.kicad_pcb|.kicad_sch|.kicad_pro` plus a
 * `<slug>/emit.json` sidecar recording the Circuit JSON element histogram and
 * any error, and a top-level `generate.json` summary.
 */

import { mkdirSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

import { Circuit } from "@tscircuit/core";
import {
  CircuitJsonToKicadPcbConverter,
  CircuitJsonToKicadProConverter,
  CircuitJsonToKicadSchConverter,
} from "circuit-json-to-kicad";

import { EXAMPLES } from "./examples.mjs";

const outDir = resolve(process.argv[2] ?? "exports");

/** Element-type histogram, so the sidecar records what the renderer produced. */
function histogram(circuitJson) {
  const counts = {};
  for (const element of circuitJson) {
    counts[element.type] = (counts[element.type] ?? 0) + 1;
  }
  return Object.fromEntries(Object.entries(counts).sort());
}

async function emitOne(example) {
  const dir = join(outDir, example.slug);
  mkdirSync(dir, { recursive: true });

  const circuit = new Circuit();
  circuit.add(example.build());
  await circuit.renderUntilSettled();
  const circuitJson = circuit.getCircuitJson();

  const schConverter = new CircuitJsonToKicadSchConverter(circuitJson);
  schConverter.runUntilFinished();
  const schFiles = schConverter.getOutputFiles({
    schematicFilename: `${example.slug}.kicad_sch`,
  });
  for (const file of schFiles) {
    writeFileSync(join(dir, file.filename), file.content);
  }

  const pcbConverter = new CircuitJsonToKicadPcbConverter(circuitJson, {
    projectName: example.slug,
  });
  pcbConverter.runUntilFinished();
  writeFileSync(join(dir, `${example.slug}.kicad_pcb`), pcbConverter.getOutputString());

  const proConverter = new CircuitJsonToKicadProConverter(circuitJson, {
    projectName: example.slug,
    schematicFilename: `${example.slug}.kicad_sch`,
    pcbFilename: `${example.slug}.kicad_pcb`,
    schematicSheetPlan: schConverter.schematicSheetPlan,
  });
  proConverter.runUntilFinished();
  writeFileSync(join(dir, `${example.slug}.kicad_pro`), proConverter.getOutputString());

  // tscircuit reports its own problems as `*_warning` / `*_error` elements in
  // Circuit JSON; surface them so an export's defects are not misread as ours.
  const diagnostics = circuitJson
    .filter((e) => e.type.endsWith("_warning") || e.type.endsWith("_error"))
    .map((e) => ({ type: e.type, message: e.message ?? null }));

  return {
    slug: example.slug,
    title: example.title,
    ok: true,
    schematic_files: schFiles.map((f) => f.filename),
    circuit_json_histogram: histogram(circuitJson),
    diagnostics,
  };
}

const results = [];
let failures = 0;
for (const example of EXAMPLES) {
  try {
    const result = await emitOne(example);
    results.push(result);
    console.log(`ok    ${example.slug}`);
  } catch (error) {
    failures += 1;
    results.push({
      slug: example.slug,
      title: example.title,
      ok: false,
      error: String(error?.stack ?? error),
    });
    console.log(`FAIL  ${example.slug}: ${error?.message ?? error}`);
  }
}

mkdirSync(outDir, { recursive: true });
for (const result of results) {
  mkdirSync(join(outDir, result.slug), { recursive: true });
  writeFileSync(
    join(outDir, result.slug, "emit.json"),
    `${JSON.stringify(result, null, 2)}\n`,
  );
}
writeFileSync(
  join(outDir, "generate.json"),
  `${JSON.stringify({ node: process.version, results }, null, 2)}\n`,
);

process.exit(failures === 0 ? 0 : 1);
