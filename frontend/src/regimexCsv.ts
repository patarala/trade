/** Tabular derivatives of checked observations; no filtering or new classification. */
import type { AnalysisPreview, PreviewInstrument } from "./regimexResult";

export type CsvRange = { start: number; stop: number };
type Cell = string | number | null;

function cell(value: Cell): string {
  if (value === null) return "";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("CSV values must be finite.");
    return String(value);
  }
  // Quoting alone does not prevent spreadsheet formula evaluation. Prefix only
  // untrusted text; negative numeric measurements keep their numeric meaning.
  const formula = /^[\s\p{Cc}]*[=+\-@＝＋－＠]/u.test(value);
  const controlPrefix = /^[\s\p{Cc}]*[\t\r\n]/u.test(value);
  const safe = formula || controlPrefix ? "'" + value : value;
  return '"' + safe.replaceAll('"', '""') + '"';
}

function table(headers: string[], rows: Cell[][]): string {
  return "\uFEFF" + [headers, ...rows].map((row) => row.map(cell).join(",")).join("\r\n") + "\r\n";
}

function selectedInstrument(preview: AnalysisPreview, instrumentId: string): PreviewInstrument {
  const instrument = preview.instruments.find((item) => item.id === instrumentId);
  if (!instrument) throw new Error("The selected instrument does not belong to this analysis.");
  return instrument;
}

const metadataHeaders = [
  "analysis_id", "snapshot_id", "snapshot_sha256", "model_version_id", "model_bundle_sha256",
  "inference_policy_sha256", "source_id", "source_version", "configuration_id", "run_id",
  "source_snapshot_id", "state_revision", "instrument_id", "base_asset", "quote_asset",
  "price_unit", "volume_unit", "price_decimal_places", "volume_decimal_places", "initial_price",
  "time_unit", "interval_bounds",
];

function metadata(preview: AnalysisPreview, instrument: PreviewInstrument): Cell[] {
  const { source } = preview, { units } = instrument;
  return [
    preview.analysisId, preview.snapshotId, preview.snapshotSha256, preview.modelVersion,
    preview.modelBundleSha256, preview.inferencePolicySha256,
    source.sourceId, source.sourceVersion, source.configurationId, source.runId,
    source.snapshotId, source.stateRevision, instrument.id, instrument.baseAsset, instrument.quoteAsset,
    units.priceUnit, units.volumeUnit, units.priceDecimalPlaces, units.volumeDecimalPlaces,
    instrument.initialPrice, "simulation_step", "[start,stop)",
  ];
}

/** Every saved interval of this instrument, independent of chart focus/filters. */
export function intervalsCsv(preview: AnalysisPreview, instrumentId: string): string {
  const instrument = selectedInstrument(preview, instrumentId);
  const identity = metadata(preview, instrument);
  return table([...metadataHeaders,
    "start", "stop", "display_step_start", "display_step_end", "direction", "status",
    "price_change_percent", "first_price_step", "last_price_step",
    "first_price_display_step", "last_price_display_step", "trade_count",
    "inactive_step_share_of_known_counts", "inactive_steps", "activity_observed_steps", "step_count",
  ], instrument.intervals.map((interval) => {
    const { stats } = interval;
    return [...identity, interval.start, interval.stop, interval.start + 1, interval.stop,
      interval.direction, interval.status, stats.priceChangePercent, stats.firstPriceStep, stats.lastPriceStep,
      stats.firstPriceStep === null ? null : stats.firstPriceStep + 1,
      stats.lastPriceStep === null ? null : stats.lastPriceStep + 1,
      stats.tradeCount, stats.inactiveStepShare, stats.inactiveSteps, stats.activityObservedSteps, stats.stepCount,
    ];
  }));
}

/** Original, unfilled observations inside a checked half-open focus range. */
export function observationsCsv(preview: AnalysisPreview, instrumentId: string, range: CsvRange): string {
  const instrument = selectedInstrument(preview, instrumentId);
  if (!Number.isSafeInteger(range.start) || !Number.isSafeInteger(range.stop)
    || range.start < 0 || range.start >= range.stop || range.stop > instrument.observations.length) {
    throw new Error("Choose an observation range inside this instrument's original grid.");
  }
  const identity = metadata(preview, instrument);
  return table([...metadataHeaders,
    "focus_start", "focus_stop", "step", "display_step", "last_price", "price_state",
    "trade_count", "trade_volume", "buy_order_count", "buy_order_volume",
    "sell_order_count", "sell_order_volume", "best_bid", "best_ask", "bid_state", "ask_state",
  ], instrument.observations.slice(range.start, range.stop).map((row) => [
    ...identity, range.start, range.stop, row.step, row.step + 1, row.lastPrice, row.priceState,
    row.tradeCount, row.tradeVolume, row.buyOrderCount, row.buyOrderVolume,
    row.sellOrderCount, row.sellOrderVolume, row.bestBid, row.bestAsk, row.bidState, row.askState,
  ]));
}
