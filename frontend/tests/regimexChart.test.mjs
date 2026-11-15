import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import ts from "typescript";

const source = await readFile(new URL("../src/regimexChart.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2023 },
}).outputText;
const chart = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);

function instrument(prices, states = prices.map((price) => price === null ? "unknown" : "observed"), start = 0) {
  return { id: "instrument", baseAsset: "BTC", quoteAsset: "USD", prices, priceStates: states,
    steps: prices.map((_, index) => start + index), intervals: [], };
}

test("price centers and interval boundaries share the original step grid", () => {
  const data = instrument([2, 4, 3, 5], undefined, 10);
  const range = chart.stepRange(data.steps);
  assert.deepEqual(range, { start: 10, stop: 14 });
  const prices = chart.priceChart(data, range, 400, 160);
  assert.deepEqual(prices.points.map((point) => point.x), [50, 150, 250, 350]);
  assert.deepEqual(chart.intervalGeometry({ start: 10, stop: 12 }, range, 400), { x: 0, width: 200 });
  assert.deepEqual(chart.intervalGeometry({ start: 12, stop: 14 }, range, 400), { x: 200, width: 200 });
  assert.equal(chart.stepLabel(range), "11–14");
  assert.deepEqual(chart.stepTicks(range, 400), [10, 11, 12, 13]);
});

test("missing prices split the line while available prices remain points", () => {
  const data = instrument([1, 2, null, 3, 1]);
  const result = chart.priceChart(data, chart.stepRange(data.steps), 500, 160);
  assert.equal(result.points[0].price, 1);
  assert.notEqual(result.points[0].y, null);
  assert.equal(result.points[2].y, null);
  assert.equal(result.paths.observed.split("M").length - 1, 2);
  assert.equal(result.paths.observed.split("L").length - 1, 2);
  assert.equal(result.paths.carried, "");
});

test("discontinuous original steps cannot be joined across an absent sample", () => {
  const data = { ...instrument([10, 12]), steps: [0, 4] };
  const result = chart.priceChart(data, chart.stepRange(data.steps), 500, 160);
  assert.deepEqual(result.points.map((point) => point.x), [50, 450]);
  assert.equal(result.paths.observed.split("M").length - 1, 2);
  assert.equal(result.paths.observed.includes("L"), false);
});

test("carried prices remain available and have a distinct connected path", () => {
  const data = instrument([10, 10, 12], ["observed", "carried", "observed"]);
  const result = chart.priceChart(data, chart.stepRange(data.steps), 300, 160);
  assert.equal(result.points[1].price, 10);
  assert.equal(result.points[1].y, result.points[0].y);
  assert.match(result.paths.carried, /^M50\.00,148\.00L150\.00,148\.00$/);
  assert.match(result.paths.observed, /M150\.00,148\.00L250\.00,12\.00$/);
});

test("one point and constant prices are finite and centered", () => {
  for (const prices of [[12], [12, 12, 12]]) {
    const data = instrument(prices);
    const result = chart.priceChart(data, chart.stepRange(data.steps), 300, 160);
    assert.ok(result.points.every((point) => point.y === 80 && Number.isFinite(point.x)));
    assert.equal(result.ticks.length, 1);
    assert.doesNotMatch(result.paths.observed, /NaN|Infinity/);
  }
  assert.equal(chart.stepLabel({ start: 0, stop: 1 }), "1");
  assert.deepEqual(chart.stepTicks({ start: 0, stop: 1 }, 200), [0]);
});

test("all-null prices have no invented line or price ticks", () => {
  const data = instrument([null, null], ["unknown", "no_execution"]);
  const result = chart.priceChart(data, chart.stepRange(data.steps), 200, 160);
  assert.deepEqual(result.paths, { observed: "", carried: "" });
  assert.deepEqual(result.ticks, []);
  assert.ok(result.points.every((point) => point.y === null));
  assert.deepEqual(result.points.map((point) => point.state), ["unknown", "no_execution"]);
});

test("focus clips to original bounds without changing interval or final-step labels", () => {
  const full = { start: 0, stop: 256 };
  assert.deepEqual(chart.focusRange({ start: 100, stop: 120 }, full), { start: 100, stop: 120 });
  assert.deepEqual(chart.focusRange({ start: -5, stop: 300 }, full), full);
  assert.deepEqual(chart.focusRange({ start: 300, stop: 320 }, full), full);
  assert.equal(chart.stepLabel({ start: 240, stop: 256 }), "241–256");
  assert.deepEqual(chart.intervalGeometry({ start: 100, stop: 200 }, { start: 120, stop: 160 }, 400), { x: 0, width: 400 });
  assert.equal(chart.intervalGeometry({ start: 0, stop: 120 }, { start: 120, stop: 160 }, 400), null);
});

test("availability takes precedence over direction without inferring sideways", () => {
  assert.equal(chart.intervalKind({ status: "observed", direction: "growth" }), "growth");
  assert.equal(chart.intervalKind({ status: "no_trades", direction: null }), "no_trades");
  assert.equal(chart.intervalKind({ status: "insufficient_data", direction: null }), "insufficient_data");
  assert.equal(chart.intervalKind({ status: "observed", direction: null }), "insufficient_data");
});

test("small changes in a large price remain distinct in hover values and axis labels", () => {
  for (const prices of [[100000.01, 100000.02], [10000000.01, 10000000.02], [0.00000001, 0.00000002], [1, 1 + Number.EPSILON], [9007199254740990, 9007199254740991]]) {
    assert.notEqual(chart.priceDetail(prices[0]), chart.priceDetail(prices[1]));
    const data = instrument(prices);
    const result = chart.priceChart(data, chart.stepRange(data.steps), 300, 160);
    assert.equal(new Set(result.ticks.map((tick) => tick.label)).size, result.ticks.length);
  }
});

test("percentage baseline uses the first available whole-run price and remains fixed on focus", () => {
  const data = instrument([null, 100, 110, null, 120], ["unknown", "carried", "observed", "unknown", "observed"], 10);
  const full = chart.priceChart(data, chart.stepRange(data.steps), 500, 160, "change");
  const focused = chart.priceChart(data, { start: 12, stop: 15 }, 300, 160, "change");
  assert.deepEqual(full.baseline, { price: 100, step: 11, state: "carried" });
  assert.deepEqual(focused.baseline, full.baseline);
  assert.deepEqual(full.points.map((point) => point.value), [null, 0, 10, null, 20]);
  assert.deepEqual(focused.points.map((point) => point.value), [10, null, 20]);
  assert.deepEqual(focused.points.map((point) => point.step), [12, 13, 14]);
  assert.equal(focused.paths.observed.split("M").length - 1, 2);
  assert.equal(focused.paths.observed.includes("L"), false);
});

test("percentage charts share a domain so different amplitudes remain comparable", () => {
  const data = [instrument([100, 101]), instrument([200, 300])];
  const result = chart.alignedCharts(data, { start: 0, stop: 2 }, 600, 160, "change");
  const [small, large] = result.series.map((series) => series.chart);
  assert.deepEqual(small.ticks, large.ticks);
  assert.deepEqual(small.ticks.map((tick) => tick.value), [0, 25, 50]);
  assert.equal(small.points[0].y, large.points[0].y);
  assert.ok(small.points[1].y > large.points[1].y);
  assert.deepEqual(small.points.map((point) => point.x), large.points.map((point) => point.x));
  const same = chart.alignedCharts([instrument([10, 11]), instrument([10000, 11000])],
    { start: 0, stop: 2 }, 600, 160, "change");
  assert.deepEqual(same.series[0].chart.points.map((point) => point.y), same.series[1].chart.points.map((point) => point.y));
});

test("absolute charts reserve a common left margin while retaining individual price scales", () => {
  const result = chart.alignedCharts([instrument([1, 2]), instrument([100000.01, 100000.02])],
    { start: 0, stop: 2 }, 600, 160, "price");
  const [first, second] = result.series.map((series) => series.chart);
  assert.ok(result.left >= 64);
  assert.equal(result.plotWidth + result.left + 12, 600);
  assert.deepEqual(first.points.map((point) => point.x), second.points.map((point) => point.x));
  assert.notDeepEqual(first.ticks.map((tick) => tick.value), second.ticks.map((tick) => tick.value));
});

test("empty comparison data does not invent a baseline, line, or ticks", () => {
  const data = [instrument([null, null]), instrument([null, null])];
  const result = chart.alignedCharts(data, { start: 0, stop: 2 }, 300, 160, "change");
  for (const { chart: item } of result.series) {
    assert.equal(item.baseline, null);
    assert.equal(item.unavailableChange, false);
    assert.deepEqual(item.paths, { observed: "", carried: "" });
    assert.deepEqual(item.ticks, []);
    assert.ok(item.points.every((point) => point.value === null && point.y === null));
  }
  const mixed = chart.alignedCharts([data[0], instrument([1, 2])], { start: 0, stop: 2 }, 300, 160, "change");
  assert.deepEqual(mixed.series[0].chart.ticks, []);
  assert.ok(mixed.series[1].chart.ticks.length > 0);
});

test("unrepresentable percentage values remain explicit gaps without nonfinite geometry", () => {
  for (const prices of [[0, 1], [Number.MIN_VALUE, Number.MAX_VALUE]]) {
    const data = instrument(prices);
    const result = chart.priceChart(data, chart.stepRange(data.steps), 300, 160, "change");
    assert.equal(result.unavailableChange, true);
    assert.equal(result.points[1].value, null);
    assert.equal(result.points[1].y, null);
    assert.ok(result.ticks.every((tick) => Number.isFinite(tick.value) && Number.isFinite(tick.y)));
    assert.doesNotMatch(result.paths.observed, /NaN|Infinity/);
  }
});
