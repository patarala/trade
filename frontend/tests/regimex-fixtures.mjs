export const analysisId = "00000000-0000-4000-8000-000000000001";
export const expectedIdentity = { analysisId, simulationId: 3, runId: 7 };

export function previewPayloads(count = 4, instrumentCount = 2) {
  const instruments = ["BTC", "ETH"].slice(0, instrumentCount).map((asset) => ({
    instrument_id: `${asset}/USD`, base_asset: asset, quote_asset: "USD", initial_price: 100,
    units: { price_unit: "quote_asset_per_base_asset", volume_unit: "base_asset",
      price_decimal_places: 8, volume_decimal_places: 8 },
  }));
  const input = {
    schema_version: "2.1", content_sha256: "a".repeat(64),
    source: { source_id: "trade-preview", source_version: "engine-3", configuration_id: "3", run_id: "7",
      snapshot_id: "run-7-observations-v1", state_revision: `completed-${count}`,
      run_status: "completed", consistency: "atomic_completed_snapshot" },
    time_grid: { origin: 0, bin_width: 1, completed_horizon: count, step_count: count,
      unit: "simulation_step", interval_bounds: "[start,stop)", terminal_bin: "full_bins_only" },
    instruments,
    observations: instruments.flatMap((instrument) => Array.from({ length: count }, (_, step) => ({
      instrument_id: instrument.instrument_id, step, last_price: 100, price_state: "carried",
      trade_count: 0, trade_volume: 0, buy_order_count: 0, buy_order_volume: 0,
      sell_order_count: 0, sell_order_volume: 0, best_bid: null, best_ask: null,
      bid_state: "unknown", ask_state: "unknown",
    }))),
  };
  const result = {
    result_version: "2.0", analysis_id: analysisId,
    snapshot_id: "00000000-0000-4000-8000-000000000009", snapshot_sha256: input.content_sha256,
    source: structuredClone(input.source), time_grid: structuredClone(input.time_grid),
    instruments: structuredClone(instruments), class_names: ["sideways", "growth", "decline"],
    model: { model_version_id: "tcn-preview", bundle_sha256: "b".repeat(64), inference_policy_sha256: "c".repeat(64) },
    sequences: instruments.map((instrument) => ({
      instrument_id: instrument.instrument_id,
      probabilities: Array.from({ length: count }, () => [1, 0, 0]),
      intervals: [{ instrument_id: instrument.instrument_id, start: 0, stop: count,
        direction: null, status: "no_trades" }],
    })),
  };
  return { result, input };
}
