/** Build the real page with explicitly synthetic cases; never publish fixture evidence. */
import { afterAll, beforeAll, expect, it } from "vitest";
import { cpSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";
const site=resolve(dirname(fileURLToPath(import.meta.url)),"..");
const outcomes=["completed","partial","stopped_before_routing","failed","timeout","unknown"];
let root:string; let html:string;
beforeAll(()=>{
 root=mkdtempSync(join(tmpdir(),"benchmark-render-"));
 const fixtureSite=join(root,"site"), results=join(root,"results"), dated=join(results,"2026-09-12");
 mkdirSync(fixtureSite); mkdirSync(dated,{recursive:true});mkdirSync(join(root,"boards"));
 cpSync(join(site,"..","pyproject.toml"),join(root,"pyproject.toml"));
 for(const entry of ["src","scripts","astro.config.mjs","package.json","tsconfig.json"])cpSync(join(site,entry),join(fixtureSite,entry),{recursive:true});
 // Share installed packages, but keep Vite's mutable cache private to this build.
 const configPath=join(fixtureSite,"astro.config.mjs");
 writeFileSync(configPath,readFileSync(configPath,"utf8").replace('output: "static",',`output: "static", vite: { cacheDir: ${JSON.stringify(join(fixtureSite,".vite-cache"))} },`));
 symlinkSync(join(site,"node_modules"),join(fixtureSite,"node_modules"),"dir");
 const archived=join(site,"../benchmarks/external/results/pocketbeagle.zero-touch.json");
 cpSync(archived,join(results,"pocketbeagle.zero-touch.json"));
 const original=JSON.parse(readFileSync(archived,"utf8"));
 for(const outcome of outcomes){
  const r={...original,board_id:`fixture-${outcome}`,generated_at:"2026-09-12T00:00:00Z",tool_commit:"fixture-tool",route_outcome:{outcome,artifact_source:outcome==="completed"?"router_output":"fallback_input",exit_code:outcome==="completed"?0:3,reason:`Synthetic ${outcome} reason`},pre_route_completion:{connections_routed:2,connections_total:10,completion_pct:20},newly_routed_connections:outcome==="completed"?8:outcome==="partial"?-2:0,completion:{connections_routed:outcome==="completed"?10:2,connections_total:10,completion_pct:outcome==="completed"?100:20},timing:{valid:true,wall_clock_s:5,refusal_reason:null,measured_phase:outcome},notes:["Synthetic test fixture, not a real board run", "DeepPCB published reference: fixture vendor value"]};
  delete r.kct_check; delete r.kicad_cli_drc; delete r.diff_pairs;
  if(outcome==="failed") r.kct_check={ran:false,passed:null,error_count:null,warning_count:null,note:"Unsupported custom pad fixture"};
  if(outcome==="unknown"){delete r.route_outcome;delete r.pre_route_completion;delete r.newly_routed_connections;}
  writeFileSync(join(dated,`${r.board_id}.zero-touch.json`),JSON.stringify(r));
 }
 execFileSync(process.execPath,[join(site,"node_modules/astro/bin/astro.mjs"),"build"],{cwd:fixtureSite,env:{...process.env,KCT_BENCHMARKS_DIR:results,KCT_BOARDS_DIR:join(root,"boards"),ASTRO_TELEMETRY_DISABLED:"1"},timeout:60000,stdio:"pipe"});
 html=readFileSync(join(fixtureSite,"dist/benchmarks/index.html"),"utf8");
},120000);
afterAll(()=>{if(root)rmSync(root,{recursive:true,force:true});});
it.each(outcomes)("renders the real %s case with a raw source and keyboard disclosure", outcome=>{
 const article=html.match(new RegExp(`<article[^>]*aria-label="fixture-${outcome}[^]*?</article>`))?.[0];
 expect(article).toBeDefined();
 expect(article).toContain("<details");expect(article).toContain("<summary");
 expect(article).toContain(`2026-09-12/fixture-${outcome}.zero-touch.json`);
 expect(article).toContain("fixture-tool");expect(article).toContain("not run");
 expect(article).not.toContain("fixture vendor value");
 if(outcome==="failed") expect(article).toContain("Unsupported custom pad fixture");
 if(outcome==="unknown"){expect(article).toContain("unknown (legacy)");expect(article).toContain("Not recorded");}
 else {expect(article).toContain(`Synthetic ${outcome} reason`);expect(article).toContain("Input connectivity before routing");expect(article).toContain(outcome==="partial"?"Measured connectivity change":"Newly routed connections");
 if(outcome==="partial"){expect(article).toContain("-2 (negative change, not routing improvement)");expect(article).not.toContain("Newly routed connections");}}
 if(outcome==="completed")expect(article).toContain("Measured final connectivity");
 else if(outcome!=="unknown")expect(article).toContain("Measured fallback-input connectivity");
});
it("preserves historical records and explicitly represents both missing STRF protocols",()=>{
 expect(html).toContain("2026-08-25");expect(html).toContain("636fd368");expect(html).toContain("unknown (legacy)");
 expect(html).toContain("STRF RF mixed-signal · zero-touch: not tested");expect(html).toContain("STRF RF mixed-signal · tuned: not tested");
 expect(html).toContain("Historical measurements, scroll horizontally");
});
