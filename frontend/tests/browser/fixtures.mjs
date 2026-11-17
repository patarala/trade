import { analysisId, previewPayloads } from "../regimex-fixtures.mjs";

export const olderId = "00000000-0000-4000-8000-000000000002";
export const link = (id = analysisId, instrument = "BTC/USD") =>
  `/tests/browser/index.html#/simulations/3/runs/7/analyses/${id}?instrument=${encodeURIComponent(instrument)}`;
export { analysisId };

export function analysis(id) {
  return { analysis_id: id, run_id: 7, state: "succeeded", stage: "complete", failure: null,
    model_version_id: id === olderId ? "baseline-saved" : "🧪".repeat(65),
    created_at: id === olderId ? "2026-01-01T12:00:00Z" : "2026-01-02T12:00:00Z",
    result_available: true, report_available: true };
}

export function observations(id = analysisId) {
  const payload = previewPayloads(12);
  payload.result.analysis_id = id;
  payload.result.model.model_version_id = analysis(id).model_version_id;
  for (const [index, sequence] of payload.result.sequences.entries()) {
    sequence.intervals = [
      { start: 0, stop: 4, status: "observed", direction: "growth" },
      { start: 4, stop: 8, status: "insufficient_data", direction: null },
      { start: 8, stop: 12, status: "observed", direction: "decline" },
    ].map((interval) => ({ ...interval, instrument_id: sequence.instrument_id }));
    for (let step = 0; step < 12; step += 1) {
      const row = payload.input.observations[index * 12 + step];
      Object.assign(row, { last_price: step < 8 ? 100 + step * (index + 1) : 108 - (step - 8) * (index + 1),
        price_state: "observed", trade_count: 1, trade_volume: 2 });
      if (step === 4) Object.assign(row, { last_price: null, price_state: "unknown", trade_count: null, trade_volume: null });
      if (step === 7) Object.assign(row, { last_price: null, price_state: "unknown" });
      if (step === 6) Object.assign(row, { last_price: 105 + 5 * index, price_state: "carried", trade_count: 0, trade_volume: 0 });
    }
  }
  return payload;
}

/** Reject any unexpected network request, POST or overlapping admission. */
export async function installPeer(page, { previewFailures = 0, capacity = 1 } = {}) {
  const state = { calls: [], errors: [], busy: 0, peak: 0, previews: 0 };
  await page.route("**/api/**", async (route) => {
    const request = route.request(), url = new URL(request.url());
    state.calls.push(url.pathname + url.search);
    if (request.method() !== "GET" || request.headers().authorization !== "Bearer browser-fixture-token"
      || url.searchParams.get("run_id") !== "7") {
      state.errors.push("Invalid method, identity or run");
      return route.fulfill({ status: 400, json: { detail: "Invalid fixture request" } });
    }
    if (state.busy >= capacity) {
      state.errors.push("Admission overlapped");
      return route.fulfill({ status: 503, headers: { "Retry-After": "2" },
        json: { detail: "Analysis capacity is busy", code: "analysis_busy", retryable: true } });
    }
    state.busy += 1;
    state.peak = Math.max(state.peak, state.busy);
    try {
      // Keep admission occupied across a browser task to expose concurrent reads.
      await new Promise((resolve) => setTimeout(resolve, 15));
      const suffix = url.pathname.replace("/api/simulations/3/regimex", "");
      const id = suffix.includes(olderId) ? olderId : analysisId;
      if (!suffix) return await route.fulfill({ json: { enabled: true, eligible: true, reason: null, analysis: analysis(analysisId) } });
      if (suffix === "/analyses") return await route.fulfill({ json: {
        items: [analysis(analysisId), analysis(olderId)], limit: 10, offset: 0, has_more: false,
      } });
      if (suffix === `/analyses/${id}`) return await route.fulfill({ json: analysis(id) });
      if (suffix === `/analyses/${id}/preview`) {
        state.previews += 1;
        if (state.previews <= previewFailures) return await route.fulfill({ status: 503, json: { detail: "Preview temporarily unavailable" } });
        return await route.fulfill({ json: observations(id) });
      }
      if (suffix === "/export") return await route.fulfill({ json: observations().input });
      if (suffix === `/analyses/${id}/report`) return await route.fulfill({ contentType: "text/html", body: "<!doctype html><title>Saved fixture report</title>" });
      state.errors.push(`Unexpected endpoint: ${suffix}`);
      return await route.fulfill({ status: 404, json: { detail: "Unexpected fixture endpoint" } });
    } finally {
      state.busy -= 1;
    }
  });
  return state;
}
