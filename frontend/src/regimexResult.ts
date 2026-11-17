/** A checked projection of the owned export and its saved classification. */
export type Direction = "sideways" | "growth" | "decline";
export type PriceState = "observed" | "carried" | "no_execution" | "unknown";
export type QuoteState = "observed" | "empty" | "unknown";
export type IntervalStatus = "observed" | "no_trades" | "insufficient_data";
export type IntervalStats = {
  priceChangePercent: number | null;
  firstPriceStep: number | null;
  lastPriceStep: number | null;
  tradeCount: number | null;
  inactiveStepShare: number | null;
  inactiveSteps: number;
  activityObservedSteps: number;
  stepCount: number;
};
export type PreviewInterval = {
  start: number;
  stop: number;
  direction: Direction | null;
  status: IntervalStatus;
  stats: IntervalStats;
};
export type PreviewObservation = {
  step: number;
  lastPrice: number | null;
  priceState: PriceState;
  tradeCount: number | null;
  tradeVolume: number | null;
  buyOrderCount: number | null;
  buyOrderVolume: number | null;
  sellOrderCount: number | null;
  sellOrderVolume: number | null;
  bestBid: number | null;
  bestAsk: number | null;
  bidState: QuoteState;
  askState: QuoteState;
};
export type InstrumentUnits = {
  priceUnit: "quote_asset_per_base_asset";
  volumeUnit: "base_asset";
  priceDecimalPlaces: number;
  volumeDecimalPlaces: number;
};
export type PreviewInstrument = {
  id: string;
  baseAsset: string;
  quoteAsset: string;
  initialPrice: number;
  units: InstrumentUnits;
  steps: number[];
  prices: (number | null)[];
  priceStates: PriceState[];
  observations: PreviewObservation[];
  intervals: PreviewInterval[];
};
export type AnalysisPreview = {
  analysisId: string;
  modelVersion: string;
  modelBundleSha256: string;
  inferencePolicySha256: string;
  snapshotId: string;
  snapshotSha256: string;
  source: {
    sourceId: string;
    sourceVersion: string;
    configurationId: string;
    runId: string;
    snapshotId: string;
    stateRevision: string;
  };
  instruments: PreviewInstrument[];
};

type Identity = { analysisId: string; simulationId: number; runId: number };
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const DIGEST = /^[0-9a-f]{64}$/;
const DIRECTIONS = ["sideways", "growth", "decline"];
const PRICE_STATES = ["observed", "carried", "no_execution", "unknown"];
const QUOTE_STATES = ["observed", "empty", "unknown"];
const STATUSES = ["observed", "no_trades", "insufficient_data"];

export class InvalidAnalysisPreview extends Error {
  constructor() {
    super("Не удалось проверить данные классификации для этого запуска.");
    this.name = "InvalidAnalysisPreview";
  }
}

function requireValue(condition: unknown): asserts condition {
  if (!condition) throw new InvalidAnalysisPreview();
}

function object(value: unknown): Record<string, unknown> {
  requireValue(value !== null && typeof value === "object" && !Array.isArray(value));
  return value as Record<string, unknown>;
}

function identifier(value: unknown): string {
  requireValue(typeof value === "string" && value.length > 0 && Array.from(value).length <= 128);
  return value;
}

function number(value: unknown, minimum = 0): number {
  requireValue(typeof value === "number" && Number.isFinite(value) && value >= minimum);
  return value;
}

function integer(value: unknown, minimum: number, maximum: number): number {
  const result = number(value, minimum);
  requireValue(Number.isSafeInteger(result) && result <= maximum);
  return result;
}

function positive(value: unknown): number {
  const result = number(value);
  requireValue(result > 0);
  return result;
}

function sameMetadata(left: unknown, right: unknown, depth = 0): boolean {
  if (left === right) return true;
  if (depth > 4 || left === null || right === null
    || typeof left !== "object" || typeof right !== "object") return false;
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left) && Array.isArray(right) && left.length === right.length
      && left.every((value, index) => sameMetadata(value, right[index], depth + 1));
  }
  const a = left as Record<string, unknown>, b = right as Record<string, unknown>;
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length
    && keys.every((key) => Object.hasOwn(b, key) && sameMetadata(a[key], b[key], depth + 1));
}

type IntervalBounds = Omit<PreviewInterval, "stats">;

function intervals(value: unknown, instrumentId: string, count: number): IntervalBounds[] {
  requireValue(Array.isArray(value) && value.length > 0 && value.length <= count);
  let previous = 0;
  const result = value.map((raw) => {
    const row = object(raw);
    const start = integer(row.start, 0, count - 1), stop = integer(row.stop, 1, count);
    requireValue(row.instrument_id === instrumentId && start === previous && stop > start);
    requireValue(STATUSES.includes(row.status as string));
    requireValue(row.status === "observed"
      ? DIRECTIONS.includes(row.direction as string) : row.direction === null);
    previous = stop;
    return { start, stop, direction: row.direction as Direction | null, status: row.status as IntervalStatus };
  });
  requireValue(previous === count);
  return result;
}

function probabilities(value: unknown, count: number): void {
  requireValue(Array.isArray(value) && value.length === count);
  for (const row of value) {
    requireValue(Array.isArray(row) && row.length === DIRECTIONS.length);
    const values = row.map((item) => number(item));
    requireValue(values.every((item) => item <= 1)
      && Math.abs(values.reduce((sum, item) => sum + item, 0) - 1) <= 1e-6);
  }
}

function activity(row: Record<string, unknown>): Pick<PreviewObservation,
  "tradeCount" | "tradeVolume" | "buyOrderCount" | "buyOrderVolume" | "sellOrderCount" | "sellOrderVolume"> {
  for (const prefix of ["trade", "buy_order", "sell_order"]) {
    const count = row[`${prefix}_count`], volume = row[`${prefix}_volume`];
    if (count !== null) integer(count, 0, 2 ** 31 - 1);
    if (volume !== null) number(volume);
    if (count !== null && volume !== null) requireValue((count === 0) === (volume === 0));
  }
  return {
    tradeCount: row.trade_count as number | null, tradeVolume: row.trade_volume as number | null,
    buyOrderCount: row.buy_order_count as number | null, buyOrderVolume: row.buy_order_volume as number | null,
    sellOrderCount: row.sell_order_count as number | null, sellOrderVolume: row.sell_order_volume as number | null,
  };
}

function quotes(row: Record<string, unknown>): Pick<PreviewObservation, "bestBid" | "bestAsk" | "bidState" | "askState"> {
  for (const side of ["bid", "ask"]) {
    const state = row[`${side}_state`], value = row[`best_${side}`];
    requireValue(QUOTE_STATES.includes(state as string));
    if (state === "observed") positive(value);
    else requireValue(value === null);
  }
  const bestBid = row.best_bid as number | null, bestAsk = row.best_ask as number | null;
  requireValue(bestBid === null || bestAsk === null || bestBid <= bestAsk);
  return { bestBid, bestAsk, bidState: row.bid_state as QuoteState, askState: row.ask_state as QuoteState };
}

function summarize(rows: PreviewObservation[], interval: IntervalBounds): IntervalStats {
  const selected = rows.slice(interval.start, interval.stop);
  const prices = selected.flatMap((row) => row.lastPrice === null ? [] : [{ step: row.step, price: row.lastPrice }]);
  const first = prices[0], last = prices.at(-1);
  // Match the report: compare available prices without filling absent endpoints.
  // A single available price provides no measured change, even if carried.
  const priceChangePercent = first && last && first.step !== last.step
    ? 100 * (last.price / first.price - 1) : null;
  requireValue(priceChangePercent === null || Number.isFinite(priceChangePercent));
  const known = selected.flatMap((row) => row.tradeCount === null ? [] : [row.tradeCount]);
  const inactiveSteps = known.filter((count) => count === 0).length;
  return {
    priceChangePercent, firstPriceStep: first?.step ?? null, lastPriceStep: last?.step ?? null,
    // Never label a known-only partial sum as the interval's complete total.
    tradeCount: known.length === selected.length ? known.reduce((sum, count) => sum + count, 0) : null,
    inactiveStepShare: known.length ? inactiveSteps / known.length : null,
    inactiveSteps, activityObservedSteps: known.length, stepCount: selected.length,
  };
}

export function createAnalysisPreview(resultValue: unknown, exportValue: unknown, expected: Identity): AnalysisPreview {
  const result = object(resultValue), input = object(exportValue);
  requireValue(input.schema_version === "2.1" && result.result_version === "2.0");
  requireValue(UUID.test(expected.analysisId) && result.analysis_id === expected.analysisId
    && typeof result.snapshot_id === "string" && UUID.test(result.snapshot_id));
  requireValue(typeof input.content_sha256 === "string" && DIGEST.test(input.content_sha256)
    && result.snapshot_sha256 === input.content_sha256);
  // The gateway verifies the canonical digest. JSON numbers cannot be faithfully
  // reserialized to Python's canonical representation in a browser.
  const source = object(input.source);
  for (const key of ["source_id", "source_version", "configuration_id", "run_id", "snapshot_id", "state_revision"]) {
    identifier(source[key]);
  }
  requireValue(source.configuration_id === String(expected.simulationId)
    && source.run_id === String(expected.runId)
    && source.snapshot_id === `run-${expected.runId}-observations-v1`
    && source.run_status === "completed" && source.consistency === "atomic_completed_snapshot");
  requireValue(sameMetadata(result.source, source));

  const grid = object(input.time_grid);
  const count = integer(grid.step_count, 1, 4096);
  requireValue(grid.origin === 0 && grid.bin_width === 1 && grid.completed_horizon === count
    && grid.unit === "simulation_step" && grid.interval_bounds === "[start,stop)"
    && grid.terminal_bin === "full_bins_only" && sameMetadata(result.time_grid, grid));
  requireValue(sameMetadata(result.class_names, DIRECTIONS));
  const model = object(result.model), modelVersion = identifier(model.model_version_id);
  requireValue(typeof model.bundle_sha256 === "string" && DIGEST.test(model.bundle_sha256)
    && typeof model.inference_policy_sha256 === "string" && DIGEST.test(model.inference_policy_sha256));

  requireValue(Array.isArray(input.instruments) && input.instruments.length >= 1 && input.instruments.length <= 2
    && sameMetadata(result.instruments, input.instruments));
  requireValue(Array.isArray(result.sequences) && result.sequences.length === input.instruments.length);
  const sequences = result.sequences;
  const knownPrices = new Map<string, number | null>();
  const instruments = input.instruments.map((raw, index) => {
    const item = object(raw), id = identifier(item.instrument_id);
    const baseAsset = identifier(item.base_asset), quoteAsset = identifier(item.quote_asset);
    requireValue(baseAsset !== quoteAsset && !knownPrices.has(id));
    const initialPrice = positive(item.initial_price);
    knownPrices.set(id, initialPrice);
    const units = object(item.units);
    requireValue(units.price_unit === "quote_asset_per_base_asset" && units.volume_unit === "base_asset");
    const priceDecimalPlaces = integer(units.price_decimal_places, 0, 18);
    const volumeDecimalPlaces = integer(units.volume_decimal_places, 0, 18);
    const sequence = object(sequences[index]);
    requireValue(sequence.instrument_id === id);
    probabilities(sequence.probabilities, count);
    return { id, baseAsset, quoteAsset, initialPrice,
      units: { priceUnit: "quote_asset_per_base_asset", volumeUnit: "base_asset", priceDecimalPlaces, volumeDecimalPlaces } as InstrumentUnits,
      steps: [] as number[], prices: [] as (number | null)[], priceStates: [] as PriceState[],
      observations: [] as PreviewObservation[],
      intervals: intervals(sequence.intervals, id, count) };
  });
  requireValue(instruments.every((item) => item.quoteAsset === instruments[0].quoteAsset));
  const byId = new Map(instruments.map((item) => [item.id, item]));
  requireValue(Array.isArray(input.observations) && input.observations.length === count * instruments.length);
  for (const raw of input.observations) {
    const row = object(raw), id = identifier(row.instrument_id);
    const item = byId.get(id);
    requireValue(item !== undefined);
    const step = integer(row.step, 0, count - 1);
    requireValue(step === item.steps.length && PRICE_STATES.includes(row.price_state as string));
    const state = row.price_state as PriceState;
    const present = state === "observed" || state === "carried";
    const price = present ? positive(row.last_price) : null;
    if (!present) requireValue(row.last_price === null);
    const totals = activity(row);
    for (const total of [totals.tradeCount, totals.tradeVolume]) {
      if (total === null) continue;
      if (state === "observed") requireValue(total > 0);
      if (state === "carried" || state === "no_execution") requireValue(total === 0);
    }
    const previous = knownPrices.get(id);
    if (state === "carried" && previous !== null) requireValue(price === previous);
    if (price !== null || !(totals.tradeCount === 0 || totals.tradeVolume === 0)) knownPrices.set(id, price);
    item.steps.push(step);
    item.prices.push(price);
    item.priceStates.push(state);
    item.observations.push({ step, lastPrice: price, priceState: state, ...totals, ...quotes(row) });
  }
  requireValue(instruments.every((item) => item.steps.length === count));
  return {
    analysisId: expected.analysisId, modelVersion,
    modelBundleSha256: model.bundle_sha256 as string,
    inferencePolicySha256: model.inference_policy_sha256 as string,
    snapshotId: result.snapshot_id as string, snapshotSha256: input.content_sha256 as string,
    source: {
      sourceId: source.source_id as string, sourceVersion: source.source_version as string,
      configurationId: source.configuration_id as string, runId: source.run_id as string,
      snapshotId: source.snapshot_id as string, stateRevision: source.state_revision as string,
    },
    instruments: instruments.map((item): PreviewInstrument => ({
      ...item, intervals: item.intervals.map((interval) => ({
        ...interval, stats: summarize(item.observations, interval),
      })),
    })),
  };
}
