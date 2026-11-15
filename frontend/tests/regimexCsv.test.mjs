import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import ts from "typescript";
import { expectedIdentity, previewPayloads } from "./regimex-fixtures.mjs";

async function module(name) {
  const source = await readFile(new URL(`../src/${name}.ts`, import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2023 },
  }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);
}
const { createAnalysisPreview } = await module("regimexResult");
const { intervalsCsv, observationsCsv } = await module("regimexCsv");
function preview(payloads = previewPayloads()) {
  return createAnalysisPreview(payloads.result, payloads.input, expectedIdentity);
}

function parse(csv) {
  assert.equal(csv[0], "\uFEFF", "UTF-8 BOM supports spreadsheet imports");
  const rows = [];
  let row = [], field = "", quoted = false;
  for (let index = 1; index < csv.length; index += 1) {
    const char = csv[index];
    if (char === '"') {
      if (quoted && csv[index + 1] === '"') { field += '"'; index += 1; }
      else quoted = !quoted;
    } else if (!quoted && char === ",") { row.push(field); field = ""; }
    else if (!quoted && char === "\r" && csv[index + 1] === "\n") {
      row.push(field); rows.push(row); row = []; field = ""; index += 1;
    } else field += char;
  }
  assert.equal(quoted, false);
  assert.equal(field, "");
  const [headers, ...data] = rows;
  assert.equal(new Set(headers).size, headers.length);
  return data.map((values) => {
    assert.equal(values.length, headers.length);
    return Object.fromEntries(headers.map((header, index) => [header, values[index]]));
  });
}

test("interval CSV keeps exact full intervals, direction/availability and snapshot/model provenance", () => {
  const data = preview();
  const before = structuredClone(data);
  const rows = parse(intervalsCsv(data, "BTC/USD"));
  assert.equal(rows.length, 1);
  const row = rows[0];
  assert.equal(row.analysis_id, expectedIdentity.analysisId);
  assert.equal(row.snapshot_id, data.snapshotId);
  assert.equal(row.snapshot_sha256, "a".repeat(64));
  assert.equal(row.model_version_id, "tcn-preview");
  assert.equal(row.model_bundle_sha256, "b".repeat(64));
  assert.equal(row.inference_policy_sha256, "c".repeat(64));
  assert.equal(row.source_id, "trade-preview");
  assert.equal(row.source_version, "engine-3");
  assert.equal(row.configuration_id, "3");
  assert.equal(row.run_id, "7");
  assert.equal(row.source_snapshot_id, "run-7-observations-v1");
  assert.equal(row.state_revision, "completed-4");
  assert.equal(row.instrument_id, "BTC/USD");
  assert.equal(row.base_asset, "BTC");
  assert.equal(row.quote_asset, "USD");
  assert.equal(row.price_unit, "quote_asset_per_base_asset");
  assert.equal(row.volume_unit, "base_asset");
  assert.equal(row.price_decimal_places, "8");
  assert.equal(row.volume_decimal_places, "8");
  assert.equal(row.time_unit, "simulation_step");
  assert.equal(row.interval_bounds, "[start,stop)");
  assert.equal(row.start, "0");
  assert.equal(row.stop, "4");
  assert.equal(row.display_step_start, "1");
  assert.equal(row.display_step_end, "4");
  assert.equal(row.direction, "");
  assert.equal(row.status, "no_trades");
  assert.equal(row.trade_count, "0");
  assert.equal(row.inactive_step_share_of_known_counts, "1");
  assert.deepEqual(data, before);
});

test("observation CSV selects only original focus rows and preserves zero, unknown and quote states", () => {
  const payloads = previewPayloads(4, 1);
  Object.assign(payloads.input.observations[1], { last_price: null, price_state: "unknown", trade_count: null, trade_volume: null,
    buy_order_count: 2, buy_order_volume: 0.00000001, best_bid: 99.12345678, bid_state: "observed", ask_state: "empty" });
  Object.assign(payloads.input.observations[3], { last_price: null, price_state: "no_execution" });
  const data = preview(payloads);
  const rows = parse(observationsCsv(data, "BTC/USD", { start: 1, stop: 4 }));
  assert.deepEqual(rows.map((row) => [row.step, row.display_step]), [["1", "2"], ["2", "3"], ["3", "4"]]);
  assert.ok(rows.every((row) => row.focus_start === "1" && row.focus_stop === "4"));
  assert.equal(rows[0].last_price, "");
  assert.equal(rows[0].price_state, "unknown");
  assert.equal(rows[0].trade_count, "");
  assert.equal(rows[0].trade_volume, "");
  assert.equal(rows[0].buy_order_count, "2");
  assert.equal(Number(rows[0].buy_order_volume), 0.00000001);
  assert.equal(rows[0].best_bid, "99.12345678");
  assert.equal(rows[0].bid_state, "observed");
  assert.equal(rows[0].best_ask, "");
  assert.equal(rows[0].ask_state, "empty");
  assert.equal(rows[1].last_price, "100");
  assert.equal(rows[1].price_state, "carried");
  assert.equal(rows[1].trade_count, "0");
  assert.equal(rows[1].trade_volume, "0");
  assert.equal(rows[1].bid_state, "unknown");
  assert.equal(rows[1].ask_state, "unknown");
  assert.equal(rows[2].last_price, "");
  assert.equal(rows[2].price_state, "no_execution");
  assert.equal(rows[2].trade_count, "0");
  assert.equal(rows[2].trade_volume, "0");
  assert.equal(parse(intervalsCsv(data, "BTC/USD"))[0].trade_count, "");
  assert.equal(parse(intervalsCsv(data, "BTC/USD"))[0].activity_observed_steps, "3");
});

test("CSV rejects another instrument, out-of-grid or ambiguous focus instead of silently clipping", () => {
  const data = preview();
  assert.throws(() => intervalsCsv(data, "other"));
  assert.throws(() => observationsCsv(data, "other", { start: 0, stop: 1 }));
  for (const range of [{ start: -1, stop: 2 }, { start: 1, stop: 1 }, { start: 3, stop: 5 },
    { start: 0.5, stop: 2 }, { start: 0, stop: Infinity }, { start: NaN, stop: 3 }]) {
    assert.throws(() => observationsCsv(data, "BTC/USD", range));
  }
  assert.equal(parse(observationsCsv(data, "ETH/USD", { start: 3, stop: 4 }))[0].instrument_id, "ETH/USD");
});

test("CSV quotes Unicode, commas, embedded quotes and newlines without adding rows", () => {
  const data = preview();
  data.instruments[0].baseAsset = '资产, "монета"\nعملة';
  data.modelVersion = 'model,"version"';
  const row = parse(intervalsCsv(data, "BTC/USD"))[0];
  assert.equal(row.base_asset, '资产, "монета"\nعملة');
  assert.equal(row.model_version_id, 'model,"version"');
});

test("untrusted formula-looking strings are neutralized while negative numeric changes stay numeric", () => {
  const payloads = previewPayloads(2, 1);
  Object.assign(payloads.input.observations[1], { last_price: 75, price_state: "observed", trade_count: 1, trade_volume: 1 });
  const data = preview(payloads);
  for (const value of ["=1+1", "+SUM(1)", "-1+2", "@SUM(1)", "  =1", "\ttext", "\rtext", " \n=1", "\uFEFF=1", "\0=1", "\0\ttext", "＝1+1"]) {
    data.source.sourceId = value;
    const row = parse(intervalsCsv(data, "BTC/USD"))[0];
    assert.equal(row.source_id, "'" + value);
    assert.equal(row.price_change_percent, "-25");
  }
});
