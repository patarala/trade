export type AnalysisLink = {
  simulationId: number;
  runId: number;
  analysisId: string;
  instrumentId: string | null;
};

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const invalid = "Ссылка на анализ некорректна.";

/** The fragment carries resource identities only; authorization stays in HTTP headers. */
export function parseAnalysisLink(hash: string): { link: AnalysisLink | null; error: string | null } {
  if (!hash || hash === "#") return { link: null, error: null };
  if (hash.length > 2048) return { link: null, error: invalid };
  const match = /^#\/simulations\/([1-9]\d*)\/runs\/([1-9]\d*)\/analyses\/([^?]+)(?:\?(.*))?$/.exec(hash);
  if (!match || !UUID.test(match[3])) return { link: null, error: invalid };
  const simulationId = Number(match[1]), runId = Number(match[2]);
  const query = new URLSearchParams(match[4] ?? "");
  const instrumentId = query.get("instrument");
  if (![simulationId, runId].every(Number.isSafeInteger)
    || [...query.keys()].some((key) => key !== "instrument") || query.getAll("instrument").length > 1
    || (instrumentId !== null && (!instrumentId.length || Array.from(instrumentId).length > 128))) {
    return { link: null, error: invalid };
  }
  return { link: { simulationId, runId, analysisId: match[3].toLowerCase(), instrumentId }, error: null };
}

export function analysisLink(link: AnalysisLink): string {
  const hash = `#/simulations/${link.simulationId}/runs/${link.runId}/analyses/${link.analysisId}`
    + (link.instrumentId === null ? "" : `?instrument=${encodeURIComponent(link.instrumentId)}`);
  if (!parseAnalysisLink(hash).link) throw new Error(invalid);
  return hash;
}

export function subscribeAnalysisLink(listener: () => void): () => void {
  window.addEventListener("hashchange", listener);
  return () => window.removeEventListener("hashchange", listener);
}

export function currentAnalysisLink(): string {
  return window.location.hash;
}

export function clearAnalysisLink(): void {
  if (window.location.hash) window.location.hash = "";
}
