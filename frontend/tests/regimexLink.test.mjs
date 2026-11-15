import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import ts from "typescript";

const source = await readFile(new URL("../src/regimexLink.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2023 },
}).outputText;
const { analysisLink, parseAnalysisLink } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);
const analysisId = "00000000-0000-4000-8000-000000000001";
const base = `#/simulations/3/runs/7/analyses/${analysisId}`;

test("saved links preserve the exact analysis, run and arbitrary Unicode instrument", () => {
  for (const instrumentId of [null, "BTC/USD", "株/円", "A&B / £", "<asset>/€"]) {
    const link = { simulationId: 3, runId: 7, analysisId, instrumentId };
    const hash = analysisLink(link);
    assert.deepEqual(parseAnalysisLink(hash), { link, error: null });
    assert.ok(!hash.includes("token"));
    assert.ok(hash.startsWith(base));
  }
});

test("invalid or ambiguous links are errors rather than alternate navigation", () => {
  for (const hash of [
    "https://other.example/", `${base}?token=secret`, `${base}?instrument=BTC&instrument=ETH`,
    `${base}?instrument=`, `${base}/report`, base.replace("/3/", "/-3/"),
    base.replace("/3/", "/9007199254740992/"), base.replace(analysisId, "not-an-id"),
    `${base}?instrument=${"x".repeat(129)}`, `${base}#another-fragment`,
  ]) {
    const result = parseAnalysisLink(hash);
    assert.equal(result.link, null, hash);
    assert.ok(result.error, hash);
    assert.ok(!result.error.includes("secret"));
  }
  assert.deepEqual(parseAnalysisLink(""), { link: null, error: null });
  assert.deepEqual(parseAnalysisLink("#"), { link: null, error: null });
});
