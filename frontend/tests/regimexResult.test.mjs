import assert from "node:assert/strict";
import test, { after } from "node:test";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import ts from "typescript";
import { expectedIdentity, previewPayloads } from "./regimex-fixtures.mjs";

const directory = await mkdtemp(join(tmpdir(), "trade-regimex-result-"));
after(() => rm(directory, { recursive: true, force: true }));
const source = await readFile(new URL("../src/regimexResult.ts", import.meta.url), "utf8");
const module = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2023 },
}).outputText;
await writeFile(join(directory, "result.mjs"), module);
const { createAnalysisPreview, InvalidAnalysisPreview } = await import(pathToFileURL(join(directory, "result.mjs")));
const project = ({ result, input }) => createAnalysisPreview(result, input, expectedIdentity);

test("projection preserves each instrument's full original grid, observed zero activity and carried prices", () => {
  const payloads = previewPayloads();
  const before = structuredClone(payloads);
  const preview = project(payloads);
  assert.equal(preview.analysisId, expectedIdentity.analysisId);
  assert.equal(preview.modelVersion, "tcn-preview");
  assert.deepEqual(preview.instruments.map((item) => item.id), ["BTC/USD", "ETH/USD"]);
  for (const instrument of preview.instruments) {
    assert.deepEqual(instrument.steps, [0, 1, 2, 3]);
    assert.deepEqual(instrument.prices, [100, 100, 100, 100]);
    assert.deepEqual(instrument.priceStates, ["carried", "carried", "carried", "carried"]);
    assert.deepEqual(instrument.intervals, [{ start: 0, stop: 4, direction: null, status: "no_trades", stats: {
      priceChangePercent: 0, firstPriceStep: 0, lastPriceStep: 3, tradeCount: 0,
      inactiveStepShare: 1, inactiveSteps: 4, activityObservedSteps: 4, stepCount: 4,
    } }]);
    assert.equal(instrument.observations[0].tradeVolume, 0);
    assert.equal(instrument.observations[0].bestAsk, null);
    assert.equal(instrument.observations[0].askState, "unknown");
  }
  assert.deepEqual(payloads, before, "projection must not modify the source payloads");
});

test("unknown and no-execution prices stay null and distinct from zero activity", () => {
  const payloads = previewPayloads(4, 1);
  const [first, second, third, fourth] = payloads.input.observations;
  Object.assign(first, { last_price: 105, price_state: "observed", trade_count: 1, trade_volume: 2 });
  Object.assign(second, { last_price: 105 });
  Object.assign(third, { last_price: null, price_state: "unknown", trade_count: null, trade_volume: null });
  Object.assign(fourth, { last_price: null, price_state: "no_execution" });
  payloads.result.sequences[0].intervals = [
    { instrument_id: "BTC/USD", start: 0, stop: 2, direction: "growth", status: "observed" },
    { instrument_id: "BTC/USD", start: 2, stop: 3, direction: null, status: "insufficient_data" },
    { instrument_id: "BTC/USD", start: 3, stop: 4, direction: null, status: "no_trades" },
  ];
  const instrument = project(payloads).instruments[0];
  assert.deepEqual(instrument.prices, [105, 105, null, null]);
  assert.deepEqual(instrument.priceStates, ["observed", "carried", "unknown", "no_execution"]);
  assert.deepEqual(instrument.intervals.map(({ start, stop }) => [start, stop]), [[0, 2], [2, 3], [3, 4]]);
});

test("identity, source, model, instrument metadata and original time grid must agree", () => {
  const changes = [
    ({ result }) => { result.analysis_id = "00000000-0000-4000-8000-000000000002"; },
    ({ input, result }) => { input.source.run_id = result.source.run_id = "8"; },
    ({ input, result }) => { input.source.configuration_id = result.source.configuration_id = "4"; },
    ({ input, result }) => { input.source.snapshot_id = result.source.snapshot_id = "different"; },
    ({ result }) => { result.source.source_id = "another-source"; },
    ({ result }) => { result.snapshot_sha256 = "f".repeat(64); },
    ({ result }) => { result.snapshot_id = "not-a-uuid"; },
    ({ result }) => { result.model.bundle_sha256 = "invalid"; },
    ({ result }) => { result.model.model_version_id = null; },
    ({ input }) => { input.schema_version = "2.0"; },
    ({ result }) => { result.result_version = "1.0"; },
    ({ result }) => { result.class_names.reverse(); },
    ({ input, result }) => { input.time_grid.origin = result.time_grid.origin = 1; },
    ({ input, result }) => { input.time_grid.bin_width = result.time_grid.bin_width = 2; },
    ({ input, result }) => { input.time_grid.completed_horizon = result.time_grid.completed_horizon = 3.5; },
    ({ result }) => { result.instruments[0].base_asset = "DOGE"; },
    ({ input, result }) => { input.instruments[1] = structuredClone(input.instruments[0]); result.instruments = structuredClone(input.instruments); },
    ({ result }) => { result.sequences.reverse(); },
  ];
  for (const change of changes) {
    const payloads = previewPayloads();
    change(payloads);
    assert.throws(() => project(payloads), InvalidAnalysisPreview, change.toString());
  }
});

test("observations cannot be omitted, duplicated, reordered or imputed", () => {
  const changes = [
    ({ input }) => { input.observations.pop(); },
    ({ input }) => { input.observations[1].step = 0; },
    ({ input }) => { input.observations[0].step = 1; },
    ({ input }) => { input.observations[0].instrument_id = "unknown"; },
    ({ input }) => { input.observations[0].last_price = null; },
    ({ input }) => { input.observations[0].last_price = 0; },
    ({ input }) => { input.observations[0].last_price = Number.POSITIVE_INFINITY; },
    ({ input }) => { input.observations[0].last_price = true; },
    ({ input }) => { input.observations[0].last_price = 101; },
    ({ input }) => { input.observations[0].price_state = "unknown"; },
    ({ input }) => { input.observations[0].price_state = "missing"; },
    ({ input }) => { input.observations[0].price_state = "observed"; },
    ({ input }) => { input.observations[0].trade_count = 1; input.observations[0].trade_volume = 1; },
    ({ input }) => { input.observations[0].trade_volume = -1; },
    ({ input }) => { input.observations[0].trade_count = false; },
    ({ input }) => { input.observations[0].buy_order_count = 1; },
    ({ input }) => { input.observations[0].sell_order_volume = Number.NaN; },
    ({ input }) => { input.observations[0].best_bid = 100; },
    ({ input }) => { input.observations[0].bid_state = "observed"; },
    ({ input }) => { input.observations[0].ask_state = "missing"; },
    ({ input }) => { Object.assign(input.observations[0], { bid_state: "observed", best_bid: 0 }); },
    ({ input }) => { Object.assign(input.observations[0], { bid_state: "observed", best_bid: Number.POSITIVE_INFINITY }); },
    ({ input }) => { Object.assign(input.observations[0], { bid_state: "observed", best_bid: 101, ask_state: "observed", best_ask: 100 }); },
  ];
  for (const change of changes) {
    const payloads = previewPayloads();
    change(payloads);
    assert.throws(() => project(payloads), InvalidAnalysisPreview, change.toString());
  }
});

test("summaries use available endpoint evidence and distinguish partial activity from full totals", () => {
  const payloads = previewPayloads(5, 1);
  const rows = payloads.input.observations;
  Object.assign(rows[0], { last_price: null, price_state: "unknown", trade_count: null, trade_volume: null });
  Object.assign(rows[1], { last_price: 100, price_state: "observed", trade_count: 2, trade_volume: 0.25 });
  Object.assign(rows[2], { best_bid: 99, bid_state: "observed", best_ask: null, ask_state: "empty" });
  Object.assign(rows[3], { last_price: 125, price_state: "observed", trade_count: 3, trade_volume: 0.5 });
  Object.assign(rows[4], { last_price: null, price_state: "unknown", trade_count: null, trade_volume: null });
  const before = structuredClone(payloads);
  const instrument = project(payloads).instruments[0];
  assert.deepEqual(instrument.intervals[0].stats, {
    priceChangePercent: 25, firstPriceStep: 1, lastPriceStep: 3, tradeCount: null,
    inactiveStepShare: 1 / 3, inactiveSteps: 1, activityObservedSteps: 3, stepCount: 5,
  });
  assert.deepEqual(instrument.observations[2], {
    step: 2, lastPrice: 100, priceState: "carried", tradeCount: 0, tradeVolume: 0,
    buyOrderCount: 0, buyOrderVolume: 0, sellOrderCount: 0, sellOrderVolume: 0,
    bestBid: 99, bestAsk: null, bidState: "observed", askState: "empty",
  });
  assert.deepEqual(payloads, before);
});

test("unknown trade counts do not become zero or get inferred from volumes and carried prices", () => {
  const payloads = previewPayloads(3, 1);
  for (const row of payloads.input.observations) {
    Object.assign(row, { last_price: null, price_state: "no_execution", trade_count: null, trade_volume: 0 });
  }
  const stats = project(payloads).instruments[0].intervals[0].stats;
  assert.deepEqual(stats, { priceChangePercent: null, firstPriceStep: null, lastPriceStep: null,
    tradeCount: null, inactiveStepShare: null, inactiveSteps: 0, activityObservedSteps: 0, stepCount: 3 });
});

test("one price gives comparison location but no measured price change; counts stay exact", () => {
  const payloads = previewPayloads(1, 1);
  Object.assign(payloads.input.observations[0], { price_state: "observed", trade_count: 2 ** 31 - 1, trade_volume: 0.25 });
  const stats = project(payloads).instruments[0].intervals[0].stats;
  assert.equal(stats.priceChangePercent, null);
  assert.equal(stats.firstPriceStep, 0);
  assert.equal(stats.lastPriceStep, 0);
  assert.equal(stats.tradeCount, 2 ** 31 - 1);
  assert.equal(stats.inactiveStepShare, 0);
});

test("adjacent interval summaries never read prices or activity outside their exact bounds", () => {
  const payloads = previewPayloads(4, 1);
  for (const [index, price] of [100, 125, 200, 100].entries()) {
    Object.assign(payloads.input.observations[index], { last_price: price, price_state: "observed", trade_count: index + 1, trade_volume: index + 1 });
  }
  payloads.result.sequences[0].intervals = [
    { instrument_id: "BTC/USD", start: 0, stop: 2, direction: "growth", status: "observed" },
    { instrument_id: "BTC/USD", start: 2, stop: 4, direction: "decline", status: "observed" },
  ];
  const intervals = project(payloads).instruments[0].intervals;
  assert.deepEqual(intervals.map((row) => [row.start, row.stop, row.stats.priceChangePercent, row.stats.tradeCount]),
    [[0, 2, 25, 3], [2, 4, -50, 7]]);
});

test("classification must cover the entire original grid with valid availability and probabilities", () => {
  const changes = [
    ({ result }) => { result.sequences[0].probabilities.pop(); },
    ({ result }) => { result.sequences[0].probabilities[0] = [true, 0, 0]; },
    ({ result }) => { result.sequences[0].probabilities[0] = [0.2, 0.2, 0.2]; },
    ({ result }) => { result.sequences[0].probabilities[0] = [Number.NaN, 0, 0]; },
    ({ result }) => { result.sequences[0].probabilities[0] = [-0.1, 0.1, 1]; },
    ({ result }) => { result.sequences[0].intervals = []; },
    ({ result }) => { result.sequences[0].intervals[0].start = 1; },
    ({ result }) => { result.sequences[0].intervals[0].stop = 3; },
    ({ result }) => { result.sequences[0].intervals[0].stop = 5; },
    ({ result }) => { result.sequences[0].intervals[0].stop = 3.5; },
    ({ result }) => { result.sequences[0].intervals[0].direction = "sideways"; },
    ({ result }) => { result.sequences[0].intervals[0].status = "observed"; },
    ({ result }) => { result.sequences[0].intervals[0].status = "missing"; },
    ({ result }) => { result.sequences[0].intervals[0].instrument_id = "ETH/USD"; },
    ({ result }) => { result.sequences[0].intervals.push(structuredClone(result.sequences[0].intervals[0])); },
  ];
  for (const change of changes) {
    const payloads = previewPayloads();
    change(payloads);
    assert.throws(() => project(payloads), InvalidAnalysisPreview, change.toString());
  }
});
