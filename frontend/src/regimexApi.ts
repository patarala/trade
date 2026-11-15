import { createAnalysisPreview, InvalidAnalysisPreview } from "./regimexResult";
import type { AnalysisPreview } from "./regimexResult";

export type AnalysisState = "queued" | "running" | "cancel_requested" | "succeeded" | "failed" | "cancelled";
export const analysisStateLabels: Record<AnalysisState, string> = {
  queued: "В очереди", running: "Выполняется", cancel_requested: "Останавливается",
  succeeded: "Готов", failed: "Ошибка анализа", cancelled: "Отменён",
};

export type Analysis = {
  analysis_id: string;
  run_id: number;
  state: AnalysisState;
  stage: string | null;
  model_version_id: string;
  created_at: string;
  failure: { code: string; message: string; retryable?: boolean } | null;
  result_available: boolean;
  report_available: boolean;
};

export type RegimexStatus = {
  enabled: boolean;
  eligible: boolean;
  reason: string | null;
  analysis: Analysis | null;
};

export type DownloadKind = "report" | "export";
export type AnalysisHistory = { items: Analysis[]; limit: number; offset: number; has_more: boolean };

const MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const STATES = new Set<AnalysisState>(["queued", "running", "cancel_requested", "succeeded", "failed", "cancelled"]);

export class RegimexRequestError extends Error {
  status: number;
  retryAfter: number | null;

  constructor(message: string, status: number, retryAfter: number | null = null) {
    super(message);
    this.name = "RegimexRequestError";
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

function object(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw invalidResponse();
  return value as Record<string, unknown>;
}

function invalidResponse() {
  return new RegimexRequestError("Не удалось проверить ответ сервиса анализа.", 502);
}

function analysis(value: unknown, runId: number, expectedId?: string): Analysis {
  const item = object(value);
  if (typeof item.analysis_id !== "string" || !UUID.test(item.analysis_id)
    || (expectedId !== undefined && item.analysis_id !== expectedId)
    || item.run_id !== runId || !STATES.has(item.state as AnalysisState)
    || !(item.stage === null || typeof item.stage === "string")
    || typeof item.model_version_id !== "string" || !item.model_version_id.length || item.model_version_id.length > 128
    || typeof item.created_at !== "string" || item.created_at.length > 64 || !/(Z|[+-]\d{2}:\d{2})$/.test(item.created_at)
    || !Number.isFinite(Date.parse(item.created_at))
    || typeof item.result_available !== "boolean" || typeof item.report_available !== "boolean") {
    throw invalidResponse();
  }
  if (item.failure !== null) {
    const failure = object(item.failure);
    if (typeof failure.code !== "string" || typeof failure.message !== "string"
      || !(failure.retryable === undefined || typeof failure.retryable === "boolean")) throw invalidResponse();
  }
  return item as Analysis;
}

async function readBody(response: Response, limit: number): Promise<Uint8Array<ArrayBuffer>> {
  const advertised = response.headers.get("Content-Length");
  if (advertised !== null && (!/^\d+$/.test(advertised) || Number(advertised) > limit)) {
    await response.body?.cancel();
    throw new RegimexRequestError("Ответ сервиса анализа превышает допустимый размер.", 502);
  }
  const reader = response.body?.getReader();
  if (!reader) throw invalidResponse();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > limit) throw new RegimexRequestError("Ответ сервиса анализа превышает допустимый размер.", 502);
      chunks.push(value);
    }
  } catch (cause) {
    await reader.cancel().catch(() => undefined);
    throw cause;
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

async function responseError(response: Response): Promise<RegimexRequestError> {
  let message = `Сервис анализа недоступен (${response.status}).`;
  try {
    const value = object(JSON.parse(new TextDecoder().decode(await readBody(response, 16 * 1024))));
    if (typeof value.detail === "string") message = value.detail;
    else if (value.error && typeof value.error === "object" && "message" in value.error
      && typeof value.error.message === "string") message = value.error.message;
  } catch { /* An invalid error body must not replace the HTTP failure. */ }
  const header = response.headers.get("Retry-After");
  const retryAfter = header && /^\d+$/.test(header) ? Math.min(Number(header), 3600) : null;
  if (response.status === 429 && retryAfter) message += ` Повторите через ${retryAfter} с.`;
  return new RegimexRequestError(message, response.status, retryAfter);
}

export class RegimexApi {
  private base: string;
  private simulationId: number;
  private runId: number;
  private token: string;

  constructor(simulationId: number, runId: number, token: string) {
    if (![simulationId, runId].every((id) => Number.isSafeInteger(id) && id > 0)) throw invalidResponse();
    this.base = `/api/simulations/${simulationId}/regimex`;
    this.simulationId = simulationId;
    this.runId = runId;
    this.token = token;
  }

  private async request(suffix: string, signal: AbortSignal, body?: object): Promise<Response> {
    const headers = new Headers({ Authorization: `Bearer ${this.token}` });
    if (body !== undefined) headers.set("Content-Type", "application/json");
    const response = await fetch(`${this.base}${suffix}${suffix.includes("?") ? "&" : "?"}run_id=${this.runId}`, {
      method: body === undefined ? "GET" : "POST",
      headers,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal: AbortSignal.any([signal, AbortSignal.timeout(45_000)]),
      cache: "no-store",
      redirect: "error",
      mode: "same-origin",
    });
    if (!response.ok) throw await responseError(response);
    return response;
  }

  private async json(suffix: string, signal: AbortSignal, body?: object, limit = 128 * 1024): Promise<unknown> {
    const response = await this.request(suffix, signal, body);
    if (response.headers.get("Content-Type")?.split(";", 1)[0].trim() !== "application/json") {
      await response.body?.cancel();
      throw invalidResponse();
    }
    try {
      return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(await readBody(response, limit)));
    } catch (cause) {
      if (cause instanceof SyntaxError || cause instanceof TypeError) throw invalidResponse();
      throw cause;
    }
  }

  async status(signal: AbortSignal): Promise<RegimexStatus> {
    const value = object(await this.json("", signal));
    if (typeof value.enabled !== "boolean" || typeof value.eligible !== "boolean"
      || !(value.reason === null || typeof value.reason === "string")) throw invalidResponse();
    return {
      enabled: value.enabled,
      eligible: value.eligible,
      reason: value.reason,
      analysis: value.analysis === null ? null : analysis(value.analysis, this.runId),
    };
  }

  async poll(id: string, signal: AbortSignal): Promise<Analysis> {
    if (!UUID.test(id)) throw invalidResponse();
    return analysis(await this.json(`/analyses/${id}`, signal), this.runId, id);
  }

  async history(offset: number, signal: AbortSignal, limit = 10): Promise<AnalysisHistory> {
    if (!Number.isSafeInteger(offset) || offset < 0 || offset > 10000
      || !Number.isSafeInteger(limit) || limit < 1 || limit > 20) throw invalidResponse();
    const value = object(await this.json(`/analyses?limit=${limit}&offset=${offset}`, signal));
    if (!Array.isArray(value.items) || value.items.length > limit || value.limit !== limit || value.offset !== offset
      || typeof value.has_more !== "boolean" || (value.has_more && value.items.length !== limit)) throw invalidResponse();
    const items = value.items.map((item) => analysis(item, this.runId));
    if (new Set(items.map((item) => item.analysis_id)).size !== items.length) throw invalidResponse();
    return { items, limit, offset, has_more: value.has_more };
  }

  async submit(retryOf: string | null, signal: AbortSignal): Promise<Analysis> {
    if (retryOf !== null && !UUID.test(retryOf)) throw invalidResponse();
    return analysis(await this.json("/analyses", signal, retryOf ? { retry_of: retryOf } : {}), this.runId);
  }

  async preview(id: string, signal: AbortSignal): Promise<AnalysisPreview> {
    if (!UUID.test(id)) throw invalidResponse();
    const pending = new AbortController();
    const requestSignal = AbortSignal.any([signal, pending.signal]);
    try {
      const [result, input] = await Promise.all([
        this.json(`/analyses/${id}/result`, requestSignal, undefined, MAX_DOWNLOAD_BYTES),
        this.json("/export", requestSignal, undefined, MAX_DOWNLOAD_BYTES),
      ]);
      requestSignal.throwIfAborted();
      return createAnalysisPreview(result, input, {
        analysisId: id, simulationId: this.simulationId, runId: this.runId,
      });
    } catch (cause) {
      pending.abort();
      if (cause instanceof InvalidAnalysisPreview) throw new RegimexRequestError(cause.message, 502);
      throw cause;
    }
  }

  async download(kind: DownloadKind, id: string | null, signal: AbortSignal): Promise<Blob> {
    if (kind === "report" && (!id || !UUID.test(id))) throw invalidResponse();
    const suffix = kind === "export" ? "/export" : `/analyses/${id}/report`;
    const response = await this.request(suffix, signal);
    const expected = kind === "export" ? "application/json" : "text/html";
    if (response.headers.get("Content-Type")?.split(";", 1)[0].trim() !== expected) {
      await response.body?.cancel();
      throw invalidResponse();
    }
    return new Blob([await readBody(response, MAX_DOWNLOAD_BYTES)], { type: expected });
  }
}

export function saveDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  try { link.click(); }
  finally {
    link.remove();
    // Give the browser time to consume the download before releasing its URL.
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}
