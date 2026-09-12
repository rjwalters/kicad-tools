import { afterEach, expect, it } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { loadBenchmarkRuns, attemptDuration, outcomeExplanation } from "./benchmarkRuns.ts";
import type { BenchmarkReport } from "./benchmarkTypes.ts";
const roots: string[] = [];
afterEach(() => roots.splice(0).forEach(root => rmSync(root, {recursive: true, force: true})));
const report = JSON.parse(readFileSync(new URL("../../../benchmarks/external/results/pocketbeagle.zero-touch.json", import.meta.url), "utf8")) as BenchmarkReport;
it("retains separate dated runs and protocols without loading the historical archive or manifest", () => {
  const root = mkdtempSync(join(tmpdir(), "dated-runs-")); roots.push(root);
  for (const date of ["2026-09-11", "2026-09-12"]) {
    mkdirSync(join(root, date));
    for (const protocol of ["zero-touch", "tuned"]) writeFileSync(join(root,date,`strf.${protocol}.json`),JSON.stringify({...report,board_id:"strf",protocol,path:"forged.json"}));
    writeFileSync(join(root,date,"run-provenance.json"), JSON.stringify({runs:[]}));
  }
  writeFileSync(join(root,"historical.json"),JSON.stringify(report));
  const runs = loadBenchmarkRuns(root);
  expect(runs.map(r=>r.path)).toEqual(["2026-09-12/strf.tuned.json","2026-09-12/strf.zero-touch.json","2026-09-11/strf.tuned.json","2026-09-11/strf.zero-touch.json"]);
  expect(loadBenchmarkRuns(join(root,"missing"))).toEqual([]);
});
it.each(["completed","partial","stopped_before_routing","failed","timeout","unknown"])("explains %s without deriving outcome from connectivity", outcome => {
 const r = {...report, route_outcome:{outcome,artifact_source:"fallback_input",exit_code:3,reason:"fixture"}};
 expect(outcomeExplanation(r).length).toBeGreaterThan(40);
 if (outcome !== "completed") expect(outcomeExplanation(r)).not.toContain("attempt completed.");
});
it("keeps unknown and refused timing distinct from completed routing speed", () => {
 expect(attemptDuration({...report,timing:{wall_clock_s:4,valid:true,refusal_reason:null}})).toContain("unknown phase");
 expect(attemptDuration({...report,timing:{wall_clock_s:4,valid:false,refusal_reason:"backend unavailable"}})).toBe("Not reported: backend unavailable");
 expect(attemptDuration({...report,timing:{wall_clock_s:4,valid:true,refusal_reason:null,measured_phase:"timeout"}})).toBe("4.0 s — timeout phase");
});
